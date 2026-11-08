"""Compare complete MCP payloads for small queries and exact document retrieval.

Run from the repository root, with benchmark dependencies and tiktoken installed:
    python benchmarks/efficiency.py --output .local-benchmarks/efficiency \
        --csv .local-benchmarks/massive-aapl-2025.csv

The optional CSV is a local Massive daily-bars export, never downloaded by this
script. Raw market rows are not copied to the report or traces. No inference is
performed. A tokenizer's payload count is not a provider's billed token count.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import io
import json
import platform
import shlex
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

import tiktoken
import token_reduction as fixtures
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Counts:
    def __init__(self, tokenizer=None):
        self.encoders = {}
        self.metadata = {}
        for name in ("cl100k_base", "o200k_base"):
            encoding = tiktoken.get_encoding(name)
            self.encoders[name] = lambda text, enc=encoding: len(
                enc.encode(text, disallowed_special=())
            )
            self.metadata[name] = {"package": "tiktoken"}
        if tokenizer:
            from tokenizers import Tokenizer

            encoding = Tokenizer.from_file(str(tokenizer))
            self.encoders["qwen2.5"] = lambda text: len(
                encoding.encode(text, add_special_tokens=False).ids
            )
            self.metadata["qwen2.5"] = {
                "package": "tokenizers",
                "tokenizer_sha256": digest(tokenizer.read_bytes()),
            }
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
    def __init__(self, name, counts):
        self.name = name
        self.counts = counts
        self.parts = {"setup": [], "prompts": [], "requests": [], "responses": []}
        self.result_json = []
        self.structured = []
        self.calls = 0
        self.seconds = 0.0

    def record(self, category, value):
        self.parts[category].append(value if isinstance(value, str) else compact(value))

    def measure(self):
        categories = {
            key: self.counts.parts(value) for key, value in self.parts.items()
        }
        before_responses = [
            part for key in ("setup", "prompts", "requests") for part in self.parts[key]
        ]
        return {
            "tokens": {
                codec: sum(values[codec] for values in categories.values())
                for codec in self.counts.encoders
            },
            "categories": categories,
            "full_result_json_tokens": self.counts.parts(
                before_responses + self.result_json
            ),
            "structured_only_tokens": (
                self.counts.parts(before_responses + self.structured)
                if len(self.structured) == self.calls
                else None
            ),
            "tool_calls": self.calls,
            "tool_seconds": round(self.seconds, 4),
        }

    def dump(self):
        return {
            "name": self.name,
            "parts": self.parts,
            "full_responses": self.result_json,
            "structured_responses": self.structured,
            "measured": self.measure(),
        }


PROFILES = {
    "default": {
        "response_format": "compact",
        "response_content": "both",
        "restricted": False,
    },
    "restricted": {
        "response_format": "compact",
        "response_content": "both",
        "restricted": True,
    },
    "text": {
        "response_format": "text",
        "response_content": "both",
        "restricted": False,
    },
    "text-restricted": {
        "response_format": "text",
        "response_content": "both",
        "restricted": True,
    },
    "text-single-copy": {
        "response_format": "text",
        "response_content": "text",
        "restricted": True,
    },
    "lazy": {
        "response_format": "compact",
        "response_content": "both",
        "restricted": False,
        "lazy": True,
    },
    "lazy-discover": {
        "response_format": "compact",
        "response_content": "both",
        "restricted": False,
        "lazy": True,
        "discover": True,
    },
    "lazy-text": {
        "response_format": "text",
        "response_content": "both",
        "restricted": False,
        "lazy": True,
    },
    "lazy-text-single-copy": {
        "response_format": "text",
        "response_content": "text",
        "restricted": False,
        "lazy": True,
    },
}


class Connection:
    def __init__(self, client, profile):
        self.client = client
        self.options = PROFILES[profile]
        self.discovered = set()


@asynccontextmanager
async def session(workspace, trace, profile, used_tools):
    options = PROFILES[profile]
    args = ["-m", "agent_workbook.cli", "--workspace", str(workspace), "serve"]
    # Omit default flags so the baseline runs on v0.3.0 as well.
    if options["response_format"] != "compact":
        args += ["--response-format", options["response_format"]]
    if options["response_content"] != "both":
        args += ["--response-content", options["response_content"]]
    if options["restricted"]:
        args += ["--tools", *used_tools]
    if options.get("lazy"):
        args += ["--experimental-lazy"]
    parameters = StdioServerParameters(command=sys.executable, args=args)
    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        definitions = await client.list_tools()
        trace.record("setup", initialized.instructions or "")
        trace.record(
            "setup",
            [
                tool.model_dump(by_alias=True, exclude_none=True)
                for tool in definitions.tools
            ],
        )
        yield Connection(client, profile)


def decode_result(result):
    """Decode either JSON text or the text renderer's explicit block references."""
    if result.structuredContent is not None:
        data = result.structuredContent
    else:
        data = json.loads(result.content[0].text)

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: expand(item) for key, item in value.items()}
        if "artifact_id" in value and "excerpt_block" in value:
            index = value.pop("excerpt_block")
            assert isinstance(index, int) and 0 < index < len(result.content)
            assert result.content[index].type == "text"
            value["excerpt"] = result.content[index].text
        return value

    data = expand(data)
    # When both encodings are available, verify the text representation too.
    if result.structuredContent is not None and result.content:
        text_data = expand(json.loads(result.content[0].text))
        assert text_data == data, "Text and structured representations disagree"
    return data


async def call(connection, trace, name, arguments):
    if connection.options.get("lazy"):
        if connection.options.get("discover") and name not in connection.discovered:
            schema = await request(
                connection.client, trace, "workspool", {"tool": name}
            )
            assert schema["name"] == name and "inputSchema" in schema
            connection.discovered.add(name)
        return await request(
            connection.client,
            trace,
            "workspool",
            {"tool": name, "arguments": arguments},
        )
    return await request(connection.client, trace, name, arguments)


async def request(client, trace, name, arguments):
    trace.record(
        "requests",
        {"method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    started = time.perf_counter()
    result = await client.call_tool(name, arguments)
    trace.seconds += time.perf_counter() - started
    trace.calls += 1
    assert not result.isError, result.content
    assert all(block.type == "text" for block in result.content)
    trace.record("responses", "\n".join(block.text for block in result.content))
    trace.result_json.append(
        compact(result.model_dump(by_alias=True, exclude_none=True))
    )
    if result.structuredContent is not None:
        trace.structured.append(compact(result.structuredContent))
    return decode_result(result)


def import_file(workspace, path, name, counts):
    """Execute and count the one-time import command and full CLI receipt."""
    trace = Trace("import/" + name, counts)
    command = [
        sys.executable,
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "import",
        str(path),
        "--name",
        name,
    ]
    trace.record("requests", {"command": shlex.join(command)})
    started = time.perf_counter()
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    trace.seconds = time.perf_counter() - started
    trace.calls = 1
    trace.record("responses", result.stdout)
    trace.result_json.append(result.stdout)
    trace.structured.append(compact(json.loads(result.stdout)))
    return json.loads(result.stdout), trace


def comparison(counts, raw, measured, initial):
    return {
        codec: {
            "raw_tokens": raw[codec],
            "workspool_tokens": measured["tokens"][codec],
            "reduction_pct": round(
                100 * (1 - measured["tokens"][codec] / raw[codec]), 2
            ),
            "with_local_import_tokens": measured["tokens"][codec] + initial[codec],
        }
        for codec in counts.encoders
    }


MASSIVE_SQL = (
    "SELECT count(*) AS row_count, count(*) FILTER (WHERE c > o) AS up_days, "
    "CAST(sum(v) AS BIGINT) AS total_volume, max(h) AS highest_price FROM input1"
)
MASSIVE_QUESTION = "Return the row count, days closing above open, total volume, and highest high. Use every row."


def massive_cases(path, root):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        assert fields and {"ticker", "v", "o", "c", "h", "t"} <= set(fields)
        rows = list(reader)
    assert rows, "The market CSV must contain at least one bar"
    for n in sorted({1, min(5, len(rows)), min(20, len(rows)), len(rows)}):
        selected = rows[:n]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(selected)
        fixture = root / f"massive-{n}.csv"
        fixture.write_text(output.getvalue())
        volume = sum(Decimal(row["v"]) for row in selected)
        assert volume == volume.to_integral_value(), (
            "Benchmark expects integral share volume"
        )
        expected = [
            n,
            sum(Decimal(row["c"]) > Decimal(row["o"]) for row in selected),
            int(volume),
            float(max(Decimal(row["h"]) for row in selected)),
        ]
        yield f"massive-{n}", fixture, MASSIVE_SQL, MASSIVE_QUESTION, expected


async def table_cases(root, counts, args, traces, cases, imports, fixtures_report):
    work = root / "tables"
    tables = []
    for n in (20, 2000):
        path = root / f"synthetic-{n}.csv"
        expected = fixtures.csv_fixture(path, n)
        tables.append(
            (f"synthetic-{n}", path, fixtures.SQL, fixtures.QUESTION, expected)
        )
    if args.csv:
        tables.extend(massive_cases(args.csv, root))
    for case, path, sql, question, expected in tables:
        name = "bench/" + case
        source, imported = import_file(work, path, name, counts)
        traces.append(imported.dump())
        imports.append({"case": case, "measured": imported.measure()})
        question = f"Data: {name}. " + question
        raw = counts.parts([question, path.read_text()])
        fixtures_report.append(
            {
                "case": case,
                "sha256": digest(path.read_bytes()),
                "bytes": path.stat().st_size,
                "rows": expected[0],
                "expected": expected,
            }
        )
        for profile in args.profiles:
            trace = Trace(case + "/" + profile, counts)
            trace.record("prompts", question)
            async with session(work, trace, profile, ["query"]) as client:
                result = await call(
                    client, trace, "query", {"inputs": [name], "sql": sql}
                )
                assert result["rows"] == [expected], (case, profile, result, expected)
                assert not result["preview_truncated"]
                assert result["inputs"] == [source["artifact_id"]]
            measured = trace.measure()
            traces.append(trace.dump())
            cases.append(
                {
                    "case": case,
                    "profile": profile,
                    "measured": measured,
                    "comparison": comparison(
                        counts, raw, measured, imported.measure()["tokens"]
                    ),
                }
            )
            print(case, profile, compact(cases[-1]["comparison"]), flush=True)


async def document_cases(root, counts, args, traces, cases, imports, fixtures_report):
    workspace = root / "documents"
    texts = [fixtures.document_fixture(index) for index in range(120)]
    sources = []
    import_tokens = dict.fromkeys(counts.encoders, 0)
    for index, text in enumerate(texts):
        path = root / f"case{index:04d}.md"
        path.write_text(text)
        source, imported = import_file(
            workspace, path, f"reviews/case{index:04d}", counts
        )
        sources.append(source)
        traces.append(imported.dump())
        for codec, value in imported.measure()["tokens"].items():
            import_tokens[codec] += value
    imports.append(
        {"case": "documents-120", "tokens": import_tokens, "tool_calls": 120}
    )
    fixtures_report.append(
        {
            "case": "documents-120",
            "documents": 120,
            "sha256": digest("\n".join(texts).encode()),
            "bytes": len("\n".join(texts).encode()),
            "document_sha256": [digest(text.encode()) for text in texts],
        }
    )
    workflows = [
        (
            "documents-targeted",
            [7, 60, 119],
            200,
            "Return the exact decision, owner, and review code for case0007, case0060, and case0119, with saved version references.",
        ),
        (
            "documents-complete",
            list(range(120)),
            8192,
            "Return every word of all review documents. Do not summarize.",
        ),
    ]
    for case, selected, size, question in workflows:
        raw = counts.parts([question, "\n".join(texts)])
        for profile in args.profiles:
            trace = Trace(case + "/" + profile, counts)
            trace.record("prompts", question)
            async with session(workspace, trace, profile, ["inspect"]) as client:
                for start in range(0, len(selected), 16):
                    indices = selected[start : start + 16]
                    refs = [sources[index]["artifact_id"] for index in indices]
                    result = await call(
                        client, trace, "inspect", {"ref": refs, "excerpt_bytes": size}
                    )
                    assert len(result["artifacts"]) == len(indices)
                    for index, item in zip(indices, result["artifacts"], strict=True):
                        assert item["artifact_id"] == sources[index]["artifact_id"]
                        assert item["is_latest"] and not item["stale_inputs"]
                        expected = texts[index].encode()[:size].decode()
                        assert item["excerpt"] == expected
                        if case == "documents-complete":
                            assert item["next_offset"] is None
                        else:
                            assert item["excerpt"].startswith(
                                "\n".join(texts[index].splitlines()[:4])
                            )
            measured = trace.measure()
            traces.append(trace.dump())
            cases.append(
                {
                    "case": case,
                    "profile": profile,
                    "measured": measured,
                    "comparison": comparison(counts, raw, measured, import_tokens),
                }
            )
            print(case, profile, compact(cases[-1]["comparison"]), flush=True)


async def run(args):
    counts = Counts(args.tokenizer)
    traces, cases, imports, fixture_info = [], [], [], []
    files = [
        Path(__file__),
        Path(fixtures.__file__),
        *sorted(Path("src/agent_workbook").glob("*.py")),
    ]
    source_before = {
        str(path.relative_to(Path.cwd()) if path.is_absolute() else path): digest(
            path.read_bytes()
        )
        for path in files
    }
    args.output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="workspool-efficiency-") as temporary:
        root = Path(temporary)
        await table_cases(root, counts, args, traces, cases, imports, fixture_info)
        if not args.skip_documents:
            await document_cases(
                root, counts, args, traces, cases, imports, fixture_info
            )
    source_after = {
        str(path.relative_to(Path.cwd()) if path.is_absolute() else path): digest(
            path.read_bytes()
        )
        for path in files
    }
    versions = {
        package: importlib.metadata.version(package)
        for package in ("agent-workbook", "mcp", "duckdb", "tokenizers", "tiktoken")
    }
    commit = await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
    )
    status = await asyncio.to_thread(
        subprocess.check_output, ["git", "status", "--short"], text=True
    )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "code_commit": commit.strip(),
        "working_tree_status": status.splitlines(),
        "source_sha256": source_after,
        "source_sha256_at_start": source_before,
        "source_files_changed_during_run": [
            path for path in source_before if source_before[path] != source_after[path]
        ],
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": versions,
        },
        "scope": {
            "primary": "Warm independent MCP sessions: instructions, listed tool definitions, question, tools/call request, and all decoded text reply blocks joined with newlines. One copy of each is counted.",
            "alternatives": "structured_only_tokens replaces text replies with compact structuredContent; null means unavailable. full_result_json_tokens replaces replies with complete serialized CallToolResult objects, including any duplicated content.",
            "raw": "Identical question plus complete local CSV or complete document corpus, supplied directly as text. Raw market rows are neither copied to this report nor included in the traces.",
            "imports": "Executed CLI imports counted separately as command payload plus full receipt. No shell-tool definition, API data-fetch cost, or upstream source acquisition is included.",
            "documents": "Known artifact references, batched at 16, with exact excerpt or complete-content assertions. No checkpoint discovery or model tool-choice inference.",
            "lazy": "Lazy profiles without discover assume the caller already knows each operation's arguments. lazy-discover explicitly fetches each used operation's schema once per session and includes that cost.",
            "limits": "No provider billing, chat message envelopes, model reasoning, final answers, prompt caching, model reliability, or inference timing measured. Massive's own store_as/query_data workflow is not this raw replay baseline.",
        },
        "tokenizers": counts.metadata,
        "profiles": {name: PROFILES[name] for name in args.profiles},
        "market_source": (
            {
                "filename": args.csv.name,
                "sha256": digest(args.csv.read_bytes()),
                "description": "User-supplied local Massive aggregate CSV; not redistributed",
            }
            if args.csv
            else None
        ),
        "fixtures": fixture_info,
        "imports": imports,
        "cases": cases,
    }
    trace_bytes = (json.dumps(traces, ensure_ascii=False, indent=2) + "\n").encode()
    (args.output / "traces.json").write_bytes(trace_bytes)
    report["trace_sha256"] = digest(trace_bytes)
    (args.output / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv", type=Path, help="Optional local Massive aggregate CSV")
    parser.add_argument(
        "--tokenizer", type=Path, help="Optional Qwen2.5 tokenizer.json"
    )
    parser.add_argument(
        "--profiles", nargs="+", choices=PROFILES, default=list(PROFILES)
    )
    parser.add_argument(
        "--skip-documents", action="store_true", help="Only run table cases"
    )
    asyncio.run(run(parser.parse_args()))
