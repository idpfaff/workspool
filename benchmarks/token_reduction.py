"""Reproducible payload benchmark through real, independent MCP sessions.

Run from the repository root:
uv run --extra benchmark --with tiktoken python benchmarks/token_reduction.py \
    --tokenizer /path/to/Qwen/tokenizer.json --output benchmarks/results

No model inference or provider API calls are used. All fixtures are synthetic.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import platform
import random
import shlex
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tokenizers import Tokenizer


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Counts:
    def __init__(self, path):
        qwen = Tokenizer.from_file(str(path))
        self.encoders = {
            "qwen2.5": lambda text: len(qwen.encode(text, add_special_tokens=False).ids)
        }
        try:
            import tiktoken

            cl100k = tiktoken.get_encoding("cl100k_base")
            self.encoders["cl100k_base"] = lambda text: len(
                cl100k.encode(text, disallowed_special=())
            )
        except ImportError:
            pass
        self.text = lru_cache(maxsize=2048)(self._text)

    def _text(self, text):
        return {name: encode(text) for name, encode in self.encoders.items()}

    def parts(self, parts):
        total = dict.fromkeys(self.encoders, 0)
        for part in parts:
            for name, count in self.text(part).items():
                total[name] += count
        return total


class Trace:
    def __init__(self, name, counts, traces):
        self.name = name
        self.counts = counts
        self.traces = traces
        self.parts = {"setup": [], "prompts": [], "requests": [], "responses": []}
        self.full_responses = []
        self.calls = 0
        self.tool_seconds = 0.0

    def record(self, kind, value):
        text = value if isinstance(value, str) else compact(value)
        self.parts[kind].append(text)

    def measure(self):
        categories = {
            kind: self.counts.parts(parts) for kind, parts in self.parts.items()
        }
        total = {
            codec: sum(values[codec] for values in categories.values())
            for codec in self.counts.encoders
        }
        full = self.counts.parts(
            self.parts["setup"]
            + self.parts["prompts"]
            + self.parts["requests"]
            + self.full_responses
        )
        self.traces.append(
            {
                "name": self.name,
                "parts": self.parts,
                "full_responses": self.full_responses,
            }
        )
        return {
            "tokens": total,
            "full_result_json_tokens": full,
            "categories": categories,
            "tool_calls": self.calls,
            "tool_seconds": round(self.tool_seconds, 4),
        }


@asynccontextmanager
async def session(workspace, trace):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_workbook.cli", "--workspace", str(workspace), "serve"],
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        tools = await client.list_tools()
        trace.record("setup", initialized.instructions or "")
        trace.record(
            "setup",
            [t.model_dump(by_alias=True, exclude_none=True) for t in tools.tools],
        )
        yield client


async def call(client, trace, name, args, expect_error=False):
    trace.record(
        "requests",
        {"method": "tools/call", "params": {"name": name, "arguments": args}},
    )
    started = time.perf_counter()
    result = await client.call_tool(name, args)
    trace.tool_seconds += time.perf_counter() - started
    trace.calls += 1
    text = "\n".join(c.text for c in result.content if c.type == "text")
    trace.record("responses", text)
    trace.full_responses.append(
        compact(result.model_dump(by_alias=True, exclude_none=True))
    )
    if bool(result.isError) != expect_error:
        raise AssertionError(f"{name}: unexpected error status: {text}")
    return text if expect_error else result.structuredContent


def import_file(workspace, path, name, trace, expected_version=None):
    args = [
        sys.executable,
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "import",
        str(path),
        "--name",
        name,
        "--summary",
        "Deterministic synthetic benchmark fixture",
    ]
    if expected_version:
        args += ["--expected-version", str(expected_version)]
    trace.record("requests", {"command": shlex.join(args)})
    started = time.perf_counter()
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    trace.tool_seconds += time.perf_counter() - started
    trace.calls += 1
    trace.record("responses", result.stdout)
    trace.full_responses.append(result.stdout)
    return json.loads(result.stdout)


def plain_payload(counts, *parts):
    return counts.parts(parts)


def comparison(counts, raw, measured):
    return {
        name: {
            "raw_tokens": raw[name],
            "workspool_tokens": measured["tokens"][name],
            "reduction_pct": round(100 * (1 - measured["tokens"][name] / raw[name]), 2),
        }
        for name in counts.encoders
    }


def cumulative(counts, initial, sessions):
    return {
        codec: initial["tokens"][codec] + sum(s["tokens"][codec] for s in sessions)
        for codec in counts.encoders
    }


def csv_fixture(path, rows, revision=0):
    rng = random.Random(7741)
    expected = [rows, 0, 0, 0]
    with path.open("w") as stream:
        stream.write("row_id,sector,return_bp,volume\n")
        for i in range(rows):
            bp = rng.randrange(-250, 251) + revision
            volume = rng.randrange(100, 1_000_001)
            sector = ("technology", "health", "energy", "finance", "materials")[i % 5]
            expected[1] += bp > 0
            expected[2] += bp
            expected[3] += volume
            stream.write(f"{i},{sector},{bp},{volume}\n")
    return expected


SQL = (
    "SELECT count(*) AS row_count, "
    "count(*) FILTER (WHERE return_bp > 0) AS positive_rows, "
    "CAST(sum(return_bp) AS BIGINT) AS total_bp, "
    "CAST(sum(volume) AS BIGINT) AS total_volume FROM input1"
)
QUESTION = "Return the row count, positive-return row count, total return in bp, and total volume. Use every row."


def direct_sql_control(path, counts, expected):
    """Strong control: existing file and one generic local command, no workbook."""
    source = str(path).replace("'", "''")
    sql = SQL.replace("input1", f"read_csv_auto('{source}')")
    with duckdb.connect() as db:
        cursor = db.execute(sql)
        rows = [list(row) for row in cursor.fetchall()]
        columns = [col[0] for col in cursor.description]
    assert rows == [expected]
    # This is a canonical generic shell-tool payload, not a second MCP server.
    code = (
        "import duckdb,json; c=duckdb.connect().execute("
        + repr(sql)
        + "); print(json.dumps({'columns':[x[0] for x in c.description],'rows':c.fetchall()}))"
    )
    command = shlex.join([sys.executable, "-c", code])
    schema = {
        "name": "shell",
        "description": "Run a local command.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
    execution = subprocess.run(
        [sys.executable, "-c", code], check=True, capture_output=True, text=True
    )
    result = json.loads(execution.stdout)
    assert result == {"columns": columns, "rows": rows}
    return {
        "tokens": plain_payload(
            counts,
            compact(schema),
            QUESTION,
            compact({"command": command}),
            compact(result),
        ),
        "expected": expected,
        "scope": "Executed local DuckDB control; canonical shell schema and command/result payload, no model inference.",
    }


async def table_case(root, n, repeats, counts, traces):
    workspace = root / f"table-{n}"
    path = root / f"replay-{n}.csv"
    expected = csv_fixture(path, n)
    raw_text = path.read_text()
    raw = plain_payload(counts, QUESTION, raw_text)
    ingestion = Trace(f"table-{n}/local-import-and-checkpoint", counts, traces)
    source = import_file(workspace, path, "replay/bars", ingestion)
    async with session(workspace, ingestion) as client:
        await call(
            client,
            ingestion,
            "save",
            {
                "name": "replay/handoff",
                "kind": "checkpoint",
                "checkpoint": {
                    "objective": "Summarize a synthetic replay.",
                    "verified": f"Imported {n} synthetic rows.",
                    "next_step": QUESTION,
                },
                "dependencies": [source["artifact_id"]],
            },
        )
    setup = ingestion.measure()
    runs = []
    for i in range(repeats):
        trace = Trace(f"table-{n}/session-{i + 1}", counts, traces)
        trace.record("prompts", QUESTION)
        async with session(workspace, trace) as client:
            handoff = await call(client, trace, "resume", {"text": "replay/handoff"})
            result = await call(
                client,
                trace,
                "query",
                {
                    "inputs": handoff["checkpoint"]["dependencies"],
                    "sql": SQL,
                },
            )
            assert result["rows"] == [expected]
            assert result["preview_truncated"] is False
        runs.append(trace.measure())
    cumulative_runs = {}
    for k in sorted({1, repeats}):
        total = cumulative(counts, setup, runs[:k])
        cumulative_runs[str(k)] = {
            codec: {
                "raw_tokens": raw[codec] * k,
                "workspool_tokens": total[codec],
                "reduction_pct": round(100 * (1 - total[codec] / (raw[codec] * k)), 2),
            }
            for codec in counts.encoders
        }
    case = {
        "case": "table",
        "rows": n,
        "fixture_bytes": path.stat().st_size,
        "fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "expected": expected,
        "verified_sessions": repeats,
        "ingestion_and_checkpoint": setup,
        "sessions": runs,
        "warm_comparison": comparison(counts, raw, runs[0]),
        "cumulative_including_local_ingestion": cumulative_runs,
        "direct_local_sql_control": direct_sql_control(path, counts, expected),
    }
    # Also charge the entire inline source as a model-generated MCP argument.
    if n == 2000:
        cold = Trace("table-2000/inline-cold-start", counts, traces)
        cold.record("prompts", QUESTION)
        async with session(root / "inline", cold) as client:
            saved = await call(
                client,
                cold,
                "save",
                {
                    "name": "replay/bars",
                    "kind": "table",
                    "media_type": "text/csv",
                    "content": raw_text,
                },
            )
            result = await call(
                client, cold, "query", {"inputs": [saved["artifact_id"]], "sql": SQL}
            )
            assert result["rows"] == [expected]
            await call(
                client,
                cold,
                "save",
                {
                    "name": "replay/handoff",
                    "kind": "checkpoint",
                    "checkpoint": {
                        "objective": "Summarize a synthetic replay.",
                        "verified": f"Imported {n} synthetic rows.",
                        "next_step": QUESTION,
                    },
                    "dependencies": [saved["artifact_id"]],
                },
            )
        case["inline_cold_start"] = cold.measure()
        case["inline_cold_comparison"] = comparison(
            counts, raw, case["inline_cold_start"]
        )
        reused = []
        for i in range(1, repeats):
            reuse = Trace(f"table-2000/inline-reuse-session-{i + 1}", counts, traces)
            reuse.record("prompts", QUESTION)
            async with session(root / "inline", reuse) as client:
                handoff = await call(
                    client, reuse, "resume", {"text": "replay/handoff"}
                )
                result = await call(
                    client,
                    reuse,
                    "query",
                    {"inputs": handoff["checkpoint"]["dependencies"], "sql": SQL},
                )
                assert result["rows"] == [expected]
            reused.append(reuse.measure())
        cold_total = cumulative(counts, case["inline_cold_start"], reused)
        case["inline_reuse_sessions"] = reused
        case["inline_lifecycle_comparison"] = {
            codec: {
                "sessions": repeats,
                "raw_tokens": raw[codec] * repeats,
                "workspool_tokens": cold_total[codec],
                "reduction_pct": round(
                    100 * (1 - cold_total[codec] / (raw[codec] * repeats)), 2
                ),
            }
            for codec in counts.encoders
        }
    # Versioned update, stale rejection, and refresh are measured end to end.
    if n == 20000:
        change = Trace("table-20000/update-stale-reject-refresh", counts, traces)
        change.record(
            "prompts",
            "Update the saved aggregate to the new data version. Reject stale inputs.",
        )
        async with session(workspace, change) as client:
            saved = await call(
                client,
                change,
                "query",
                {"inputs": ["replay/bars"], "sql": SQL, "save_as": "replay/totals"},
            )
            assert saved["rows"] == [expected]
            revised = csv_fixture(path, n, revision=17)
            import_file(workspace, path, "replay/bars", change, expected_version=1)
            status = await call(client, change, "inspect", {"ref": "replay/totals"})
            assert status["stale_inputs"] is True
            error = await call(
                client,
                change,
                "query",
                {"inputs": [source["artifact_id"]], "sql": SQL},
                expect_error=True,
            )
            assert "historical" in error
            refreshed = await call(client, change, "refresh", {"ref": "replay/totals"})
            assert refreshed["rows"] == [revised]
            assert refreshed["saved"]["version"] == 2
        case["update_and_refresh"] = change.measure()
        case["updated_expected"] = revised
    print(f"Verified table {n:,} rows: {repeats} independent sessions", flush=True)
    return case


def document_fixture(index):
    rng = random.Random(619 + index)
    key = f"case{index:04d}"
    status = ("approved", "deferred", "in_review")[index % 3]
    lines = [
        f"# Engineering review {key}",
        f"Decision: {status}",
        f"Owner: team-{index % 11:02d}",
        f"Review code: {rng.randrange(100000, 999999)}",
    ]
    phrases = [
        "Validate the result against the recorded input version before reuse.",
        "Measure startup separately from steady state processing on the target machine.",
        "Retain the original source and record the reason for any revision.",
        "A retry must use the expected version to avoid overwriting another session.",
        "The acceptance check compares the output with an independent calculation.",
        "Review unresolved dependencies before scheduling the next milestone.",
        "Confirm that the evidence supports the decision and its stated scope.",
        "The next reviewer should reproduce the check using the pinned artifacts.",
    ]
    for j in range(36):
        lines.append(
            f"Record {j:02d}, evidence {rng.randrange(10000, 99999)}: {rng.choice(phrases)}"
        )
    return "\n".join(lines) + "\n"


async def document_case(root, count, repeats, counts, traces):
    workspace = root / "documents"
    selected = [7, count // 2, count - 1]
    texts = [document_fixture(i) for i in range(count)]
    corpus = "\n".join(texts)
    question = (
        "Return the exact decision, owner, and review code for case0007, "
        + f"case{selected[1]:04d}, and case{selected[2]:04d}, with saved version references."
    )
    raw = plain_payload(counts, question, corpus)
    ingest = Trace("documents/local-import-and-checkpoint", counts, traces)
    sources = []
    for i, content in enumerate(texts):
        path = root / f"case{i:04d}.md"
        path.write_text(content)
        sources.append(import_file(workspace, path, f"reviews/case{i:04d}", ingest))
    async with session(workspace, ingest) as client:
        await call(
            client,
            ingest,
            "save",
            {
                "name": "reviews/handoff",
                "kind": "checkpoint",
                "checkpoint": {
                    "objective": "Continue the engineering review.",
                    "verified": f"Imported {count} synthetic review documents.",
                    "next_step": question,
                },
                "dependencies": [sources[i]["artifact_id"] for i in selected],
            },
        )
    setup = ingest.measure()
    runs = []
    for i in range(repeats):
        trace = Trace(f"documents/session-{i + 1}", counts, traces)
        trace.record("prompts", question)
        async with session(workspace, trace) as client:
            handoff = await call(client, trace, "resume", {"text": "reviews/handoff"})
            assert handoff["checkpoint"]["dependencies"] == [
                sources[j]["artifact_id"] for j in selected
            ]
            for j in selected:
                result = await call(
                    client,
                    trace,
                    "inspect",
                    {"ref": sources[j]["artifact_id"], "excerpt_bytes": 200},
                )
                assert result["excerpt"].startswith(
                    "\n".join(texts[j].splitlines()[:4])
                )
        runs.append(trace.measure())
    # Discovery by indexed content, rather than checkpoint-selected evidence.
    search = Trace("documents/search-then-inspect", counts, traces)
    search.record("prompts", "Find the decision and evidence for case0042.")
    async with session(workspace, search) as client:
        found = await call(client, search, "find", {"text": "case0042"})
        found = found["result"]
        assert any(x["artifact_id"] == sources[42]["artifact_id"] for x in found)
        result = await call(
            client,
            search,
            "inspect",
            {"ref": sources[42]["artifact_id"], "excerpt_bytes": 200},
        )
        assert result["excerpt"].startswith("\n".join(texts[42].splitlines()[:4]))
    search_measure = search.measure()
    # Negative control: a task that needs every byte, not selected evidence.
    allread = Trace("documents/read-everything-negative-control", counts, traces)
    allread.record(
        "prompts", "Return every word of all review documents. Do not summarize."
    )
    async with session(workspace, allread) as client:
        for i, source in enumerate(sources):
            result = await call(
                client,
                allread,
                "inspect",
                {"ref": source["artifact_id"], "excerpt_bytes": 8192},
            )
            assert result["excerpt"] == texts[i] and result["next_offset"] is None
    allread_measure = allread.measure()
    full_raw = plain_payload(counts, allread.parts["prompts"][0], corpus)
    total = cumulative(counts, setup, runs)
    # Strong control: one local command returning the same three 200-byte excerpts.
    shell_schema = {
        "name": "shell",
        "description": "Run a local command.",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    }
    paths = [str(root / f"case{i:04d}.md") for i in selected]
    command = "python -c " + shlex.quote(
        "from pathlib import Path; "
        + "; ".join(f"print(Path({p!r}).read_text()[:200])" for p in paths)
    )
    execution = await asyncio.to_thread(
        subprocess.run, shlex.split(command), check=True, capture_output=True, text=True
    )
    assert execution.stdout == "".join(texts[i][:200] + "\n" for i in selected)
    targeted = plain_payload(
        counts,
        question,
        compact(shell_schema),
        compact({"command": command}),
        execution.stdout,
    )
    print(
        f"Verified {count} documents: {repeats} sessions, search, and complete reread",
        flush=True,
    )
    return {
        "case": "documents",
        "documents": count,
        "corpus_bytes": len(corpus.encode()),
        "fixture_sha256": hashlib.sha256(corpus.encode()).hexdigest(),
        "selected_documents": selected,
        "verified_sessions": repeats,
        "ingestion_and_checkpoint": setup,
        "sessions": runs,
        "warm_comparison": comparison(counts, raw, runs[0]),
        "cumulative_including_local_ingestion": {
            codec: {
                "raw_tokens": raw[codec] * repeats,
                "workspool_tokens": total[codec],
                "reduction_pct": round(
                    100 * (1 - total[codec] / (raw[codec] * repeats)), 2
                ),
            }
            for codec in counts.encoders
        },
        "search_then_inspect": search_measure,
        "complete_reread": allread_measure,
        "complete_reread_comparison": comparison(counts, full_raw, allread_measure),
        "targeted_file_control_tokens": targeted,
    }


async def varied_queries(root, counts, traces):
    """Independent Python oracles for groups, filtering/ranking, and a join."""
    path = root / "replay-200000.csv"
    workspace = root / "table-200000"
    with path.open() as stream:
        data = [
            {
                "row_id": int(r["row_id"]),
                "sector": r["sector"],
                "return_bp": int(r["return_bp"]),
                "volume": int(r["volume"]),
            }
            for r in csv.DictReader(stream)
        ]
    sectors = sorted({r["sector"] for r in data})
    thresholds = {s: 125 + i * 10 for i, s in enumerate(sectors)}
    dimension = root / "thresholds.csv"
    dimension.write_text(
        "sector,min_bp\n" + "".join(f"{s},{v}\n" for s, v in thresholds.items())
    )
    ingest = Trace("varied-queries/import-dimension", counts, traces)
    import_file(workspace, dimension, "replay/thresholds", ingest)
    ingestion = ingest.measure()
    groups = [
        [
            s,
            sum(r["sector"] == s for r in data),
            sum(r["return_bp"] for r in data if r["sector"] == s),
        ]
        for s in sectors
    ]
    ranked = sorted(
        (r for r in data if r["return_bp"] >= 200),
        key=lambda r: (-r["volume"], r["row_id"]),
    )[:5]
    ranking = [
        [r[k] for k in ("row_id", "sector", "return_bp", "volume")] for r in ranked
    ]
    joined = [
        [
            s,
            sum(r["sector"] == s and r["return_bp"] >= thresholds[s] for r in data),
            sum(
                r["volume"]
                for r in data
                if r["sector"] == s and r["return_bp"] >= thresholds[s]
            ),
        ]
        for s in sectors
    ]
    tasks = [
        (
            "sector-groups",
            "Return the row count and total return for each sector, sorted by sector.",
            "SELECT sector, count(*) AS rows, CAST(sum(return_bp) AS BIGINT) AS total_bp FROM input1 GROUP BY sector ORDER BY sector",
            groups,
            ["replay/bars"],
        ),
        (
            "filter-and-rank",
            "Return the five highest-volume rows with return_bp >= 200. Break volume ties by row_id ascending.",
            "SELECT row_id, sector, return_bp, volume FROM input1 WHERE return_bp >= 200 ORDER BY volume DESC, row_id ASC LIMIT 5",
            ranking,
            ["replay/bars"],
        ),
        (
            "threshold-join",
            "Join each sector to its minimum-return threshold. Return the qualifying row count and total volume per sector, sorted by sector.",
            "SELECT b.sector, count(*) AS rows, CAST(sum(b.volume) AS BIGINT) AS total_volume FROM input1 b JOIN input2 t ON b.sector = t.sector WHERE b.return_bp >= t.min_bp GROUP BY b.sector ORDER BY b.sector",
            joined,
            ["replay/bars", "replay/thresholds"],
        ),
    ]
    runs = []
    raw_text = path.read_text()
    for name, question, sql, expected, inputs in tasks:
        trace = Trace("varied-queries/" + name, counts, traces)
        trace.record("prompts", question)
        async with session(workspace, trace) as client:
            await call(client, trace, "resume", {"text": "replay/handoff"})
            result = await call(client, trace, "query", {"inputs": inputs, "sql": sql})
            assert result["rows"] == expected and not result["preview_truncated"]
        measured = trace.measure()
        baseline = plain_payload(
            counts, question, raw_text, dimension.read_text() if len(inputs) > 1 else ""
        )
        runs.append(
            {
                "task": name,
                "expected": expected,
                "measured": measured,
                "comparison": comparison(counts, baseline, measured),
            }
        )
    print(
        "Verified grouped aggregates, filtered ranking, and two-input join", flush=True
    )
    return {
        "case": "varied_queries",
        "rows": 200000,
        "verified_tasks": len(tasks),
        "dimension_ingestion": ingestion,
        "tasks": runs,
    }


def write_report(output, report, traces):
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    with (output / "mcp-traces.jsonl").open("w") as stream:
        for trace in traces:
            stream.write(compact(trace) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "scenarios": len(report["cases"]),
                "tokenizers": report["tokenizers"],
            }
        ),
        flush=True,
    )


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--rows", type=int, nargs="+", default=[20, 2000, 20000, 200000, 1000000]
    )
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--documents", type=int, default=120)
    args = parser.parse_args()
    if args.sessions < 1 or args.documents < 50 or min(args.rows) < 1:
        parser.error("Use at least one session, 50 documents, and positive row counts")
    counts = Counts(args.tokenizer)
    traces = []
    code_commit = await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
    )
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "code_commit": code_commit.strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "packages": {
            p: importlib.metadata.version(p) for p in ("mcp", "duckdb", "tokenizers")
        },
        "tokenizers": list(counts.encoders),
        "qwen_tokenizer_sha256": hashlib.sha256(
            args.tokenizer.read_bytes()
        ).hexdigest(),
        "accounting": {
            "tokens": "Sum of separately tokenized initialize instructions, full tool definitions (including output schemas), user prompts, tools/call request objects, and actual MCP text responses. Import commands and their stdout are counted in ingestion. Requests are model-output-like; responses and definitions are model-input-like. This is payload accounting, not measured provider billing.",
            "full_result_json_tokens": "Same accounting, replacing text responses with full CallToolResult JSON, including structuredContent duplication. Host behavior varies.",
            "raw_baseline": "User question plus every raw CSV row or document in each independent session. Counterfactual token volume; large cases may not fit a model context window.",
            "direct_controls": "Local DuckDB and targeted file access show that ordinary tools can also avoid putting raw data in context. Workspool's additional benefit is persisted references, checkpoints, versions, and freshness.",
            "excluded": "No LLM inference, model reasoning or final-answer tokens, chat role framing, prompt caching, SDK initialize/list_tools wire envelopes, host-specific wrappers, Mac/MLX speed or memory claims. No claims about end-to-end task quality from scripted tool selection.",
            "fixtures": "Deterministic synthetic data, fixed random seeds. Correct results checked independently in Python; no real market or personal data.",
        },
        "cases": [],
    }
    with TemporaryDirectory(prefix="workspool-bench-") as folder:
        root = Path(folder)
        for n in args.rows:
            report["cases"].append(
                await table_case(root, n, args.sessions, counts, traces)
            )
            write_report(args.output, report, traces)
        report["cases"].append(
            await document_case(root, args.documents, args.sessions, counts, traces)
        )
        if 200000 in args.rows:
            report["cases"].append(await varied_queries(root, counts, traces))
    report["completed_at"] = datetime.now(UTC).isoformat()
    write_report(args.output, report, traces)


if __name__ == "__main__":
    asyncio.run(main())
