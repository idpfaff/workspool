"""Scripted, observed-evidence workflows against bdc6561 and the current server.

Run from the repository root. The baseline is a separate checkout, not a
configuration of the modified server. No inference or provider billing occurs.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from efficiency import Counts, compact
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from token_reduction import document_fixture as review_document

from agent_workbook.render import decode as decode_content
from agent_workbook.store import Workbook


def sha(data):
    return hashlib.sha256(data).hexdigest()


def runtime_versions():
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("mcp", "duckdb", "tiktoken", "tokenizers", "pydantic")
        },
    }


def source_hashes(paths):
    return {
        str(path.resolve().relative_to(Path.cwd())): sha(path.read_bytes())
        for path in paths
    }


class Conversation:
    """Count canonical message payloads, with static schemas on each decision.

    Cumulative exposure sums per-message token counts in each retained prefix;
    it excludes unknown provider chat framing, caching, and hidden reasoning.
    Tool calls are assistant output that becomes input on subsequent decisions.
    """

    def __init__(self, name, counts):
        self.name, self.counts = name, counts
        self.setup, self.events, self.decisions, self.wire = [], [], [], []
        self.calls = 0

    def add(self, role, text):
        self.events.append(
            {"role": role, "text": text if isinstance(text, str) else compact(text)}
        )

    def decision(self):
        self.decisions.append(len(self.events))

    def answer(self, value):
        self.decision()
        self.add("assistant_answer", value)

    def measure(self):
        setup = self.counts.parts(self.setup)
        messages = [self.counts.parts([event["text"]]) for event in self.events]
        cumulative = dict.fromkeys(setup, 0)
        for end in self.decisions:
            for codec in setup:
                cumulative[codec] += setup[codec] + sum(
                    message[codec] for message in messages[:end]
                )
        return {
            "new_payload_tokens": {
                codec: setup[codec] + sum(message[codec] for message in messages)
                for codec in setup
            },
            "final_context_tokens": {
                codec: setup[codec] + sum(message[codec] for message in messages)
                for codec in setup
            },
            "cumulative_input_exposure": cumulative,
            "setup_tokens": setup,
            "tool_reply_tokens": self.counts.parts(
                [event["text"] for event in self.events if event["role"] == "tool"]
            ),
            "assistant_tool_call_tokens": self.counts.parts(
                [
                    event["text"]
                    for event in self.events
                    if event["role"] == "assistant_tool_call"
                ]
            ),
            "model_decisions": len(self.decisions),
            "tool_calls": self.calls,
        }

    def dump(self):
        return {
            "name": self.name,
            "setup": self.setup,
            "events": self.events,
            "decision_prefix_lengths": self.decisions,
            "wire_results": self.wire,
            "measured": self.measure(),
        }


def decode(result):
    if result.structuredContent is not None:
        return result.structuredContent
    try:
        return decode_content(result.content)
    except ValueError:
        # bdc6561's explicit text renderer predates adaptive envelopes.
        data = json.loads(result.content[0].text)
        rows = data.get("artifacts", [data])
        assert any("excerpt_block" in row for row in rows)
        for row in rows:
            if "excerpt_block" in row:
                row["excerpt"] = result.content[row.pop("excerpt_block")].text
        return data


@asynccontextmanager
async def connection(workspace, source, trace, names, response_format="compact"):
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--tools",
        *names,
        "--response-format",
        response_format,
        "--response-content",
        "text",
    ]
    env = dict(os.environ, PYTHONPATH=str(source / "src"))
    params = StdioServerParameters(command=sys.executable, args=args, env=env)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        definitions = await client.list_tools()
        assert {tool.name for tool in definitions.tools} == set(names)
        trace.setup = [
            initialized.instructions or "",
            compact(
                [
                    tool.model_dump(by_alias=True, exclude_none=True)
                    for tool in definitions.tools
                ]
            ),
        ]
        yield client


async def call(client, trace, name, arguments):
    trace.decision()
    trace.add("assistant_tool_call", {"name": name, "arguments": arguments})
    result = await client.call_tool(name, arguments)
    trace.calls += 1
    assert not result.isError, (name, result.content)
    assert all(part.type == "text" for part in result.content)
    trace.add("tool", "\n".join(part.text for part in result.content))
    trace.wire.append(result.model_dump(by_alias=True, exclude_none=True))
    return decode(result)


def listing(data):
    return data if isinstance(data, list) else data["result"]


def save(wb, name, text, **kwargs):
    return wb.save_text(
        text, name=name, kind="document", media_type="text/plain", **kwargs
    )


def prose(size, seed):
    rng = random.Random(seed)
    lines, length = [], 0
    while length < size:
        line = f"Record {rng.randrange(10000, 99999)}: Reconcile service allocations with confirmed delivery schedules.\n"
        lines.append(line)
        length += len(line)
    return "".join(lines)[:size]


def document_fixture(root, deep=False):
    wb = Workbook(root)
    sources, truth = [], {}
    keys = ["zephyr7"] if deep else ["zephyr7", "atlas4", "orion9"]
    positions = [82000] if deep else [12000, 28000, 40000]
    for index, (key, position) in enumerate(zip(keys, positions, strict=True)):
        name = f"archive/{random.Random(67 + index).randrange(100000, 999999)}"
        prefix = prose(position, 400 + index)
        if not deep:
            prefix = f"Account {key}\n" + prefix
        code = f"RC-{random.Random(891 + index).randrange(100000, 999999)}"
        text = prefix + f"\nRENEWAL {key}={code}\n" + prose(1800, 600 + index)
        item = save(wb, name, text)
        sources.append((item, text))
        truth[key] = {"code": code, "name": name, "version": 1}
        if not deep:
            for distractor in range(2):
                body = f"Account {key}\nPending review.\n" + prose(
                    1500, 800 + index * 2 + distractor
                )
                item = save(
                    wb,
                    f"archive/{random.Random(97 + index * 2 + distractor).randrange(100000, 999999)}",
                    body,
                )
                sources.append((item, body))
    # In the beyond-index probe the relevant oldest file is outside find's
    # ten-result fallback. The caller does not receive its name or position.
    for index in range(23 if deep else 6):
        text = prose(1300, 900 + index)
        item = save(wb, f"archive/{700000 + index}", text)
        sources.append((item, text))
    return sources, truth, keys


def raw_sources(sources):
    return "\n".join(
        f"SOURCE {item['name']} VERSION {item['version']}\n{text}"
        for item, text in sources
    )


async def baseline_document(client, trace, key):
    found = listing(await call(client, trace, "find", {"text": key, "limit": 10}))
    fallback = not found
    if fallback:
        found = listing(await call(client, trace, "find", {"limit": 10}))
    active = {item["artifact_id"]: {"offset": 0, "text": ""} for item in found}
    pattern = re.compile(r"RENEWAL " + re.escape(key) + r"=(RC-\d{6})")
    while active:
        offset = min(item["offset"] for item in active.values())
        refs = [ref for ref, state in active.items() if state["offset"] == offset]
        response = await call(
            client,
            trace,
            "inspect",
            {"ref": refs, "excerpt_bytes": 8192, "offset": offset},
        )
        for item in response["artifacts"]:
            state = active[item["artifact_id"]]
            state["text"] += item.get("excerpt", "")
            match = pattern.search(state["text"])
            if match:
                return {
                    "code": match[1],
                    "name": item["name"],
                    "version": item["version"],
                }
            if item.get("next_offset") is None:
                del active[item["artifact_id"]]
            else:
                state["offset"] = item["next_offset"]
    return None


async def candidate_document(client, trace, key):
    cursor = None
    pattern = re.compile(r"RENEWAL " + re.escape(key) + r"=(RC-\d{6})")
    while True:
        arguments = {"text": f"RENEWAL {key}", "context_bytes": 256, "limit": 5}
        if cursor:
            arguments["cursor"] = cursor
        result = await call(client, trace, "search", arguments)
        for item in result["hits"]:
            match = pattern.search(item["excerpt"])
            if match:
                return {
                    "code": match[1],
                    "name": item["name"],
                    "version": item["version"],
                }
        cursor = result.get("cursor")
        if not cursor:
            return None


def finish_case(name, trace, answers, expected, traces, extra=None):
    success = answers == expected
    record = {
        "case": name,
        "success": success,
        "answers": answers,
        "expected": expected,
        "measured": trace.measure(),
        **(extra or {}),
    }
    traces.append(trace.dump())
    return record


async def documents(root, sources_by_mode, counts, args, traces):
    cases = []
    for deep in (False, True):
        fixture = root / ("deep-fixture" if deep else "documents-fixture")
        sources, truth, keys = document_fixture(fixture, deep)
        name = "beyond-index" if deep else "unknown-deep-documents"
        question = (
            "Find the renewal codes for "
            + ", ".join(keys)
            + "; cite source names and current versions."
        )
        for mode, source in sources_by_mode.items():
            work = root / f"{name}-{mode}"
            shutil.copytree(fixture, work)
            trace = Conversation(f"{name}/{mode}", counts)
            trace.add("user", question)
            names = ["find", "inspect"] if mode == "baseline" else ["search"]
            async with connection(
                work, source, trace, names, "text" if mode == "baseline" else "adaptive"
            ) as client:
                answers = {}
                for key in keys:
                    operation = (
                        baseline_document if mode == "baseline" else candidate_document
                    )
                    answers[key] = await operation(client, trace, key)
                trace.answer(answers)
            cases.append(
                finish_case(
                    name + "/" + mode,
                    trace,
                    answers,
                    truth,
                    traces,
                    {
                        "source_bytes": sum(len(text.encode()) for _, text in sources),
                        "retrieval_plan": "Find the identifying key, or inspect the newest ten artifacts if it is unindexed; scan only observed references to EOF. Candidate searches complete local content for the requested marker.",
                        "failure_reason": "Bounded fallback could not discover the relevant unindexed artifact; this is an inconclusive retrieval, not proof of no match."
                        if answers != truth
                        else None,
                    },
                )
            )
        raw = Conversation(name + "/raw-retained", counts)
        raw.add("user", question + "\n" + raw_sources(sources))
        raw.answer(truth)
        cases.append(
            finish_case(
                name + "/raw-retained",
                raw,
                truth,
                truth,
                traces,
                {
                    "assumption": "Counterfactual exact answering from all supplied sources in one decision; no model reliability claimed."
                },
            )
        )
    return cases


def table_fixture(root):
    wb = Workbook(root)
    columns = [
        "row_id",
        *[f"feature_{i:02d}" for i in range(1, 34)],
        "actual_return_bp",
    ]
    rng = random.Random(623)
    rows = [
        [
            index,
            *[rng.randrange(-500, 501) for _ in range(33)],
            rng.randrange(-300, 301),
        ]
        for index in range(200)
    ]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    text = output.getvalue()
    item = wb.save_text(
        text,
        name="ledger/734901",
        kind="table",
        media_type="text/csv",
        summary="Monthly risk ledger",
    )
    returns = [row[-1] for row in rows]
    queries = [
        ("count", "count(*)", len(rows)),
        ("sum", "sum({column})", sum(returns)),
        ("minimum", "min({column})", min(returns)),
        ("maximum", "max({column})", max(returns)),
    ]
    queries += [
        (
            f"count above {threshold}",
            f"count(*) FILTER (WHERE {{column}} > {threshold})",
            sum(value > threshold for value in returns),
        )
        for threshold in (-100, 0, 100, 200)
    ]
    return item, text, columns, queries


async def baseline_schema(client, trace, ref):
    columns, types, offset = [], [], 0
    while True:
        sql = (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name='input1' ORDER BY ordinal_position "
            f"LIMIT 25 OFFSET {offset}"
        )
        result = await call(
            client, trace, "query", {"inputs": [ref], "sql": sql, "limit": 25}
        )
        assert result["columns"] == ["column_name", "data_type"]
        columns.extend(row[0] for row in result["rows"])
        types.extend(row[1] for row in result["rows"])
        if len(result["rows"]) < 25:
            break
        offset += 25
    return {"columns": columns, "types": types}


async def candidate_schema(client, trace, ref):
    columns, types, offset = [], [], 0
    while True:
        result = await call(
            client, trace, "describe", {"inputs": [ref], "offset": offset, "limit": 50}
        )
        assert "rows" not in result
        table = result["tables"][0]
        assert "rows" not in table
        assert table["artifact_id"] == ref
        assert len(table["columns"]) == len(table["types"])
        columns.extend(table["columns"])
        types.extend(table["types"])
        offset = table.get("next_offset")
        if offset is None:
            return {"columns": columns, "types": types}


def table_prompt(index, label):
    prompt = (
        f"In the monthly risk ledger, return the {label} of actual return basis points."
    )
    if index == 0:
        prompt = (
            "First return the complete column-name and type schema for forthcoming analysis, without sampling data rows. Then "
            + prompt
        )
    return prompt


async def tables(root, sources_by_mode, counts, traces):
    fixture = root / "table-fixture"
    source_item, text, expected_columns, questions = table_fixture(fixture)
    cases = []
    expected = [value for _, _, value in questions]
    expected_schema = {
        "columns": expected_columns,
        "types": ["BIGINT"] * len(expected_columns),
    }
    expected[0] = {"schema": expected_schema, "answer": expected[0]}
    for mode, source in sources_by_mode.items():
        work = root / f"table-{mode}"
        shutil.copytree(fixture, work)
        trace = Conversation("unknown-wide-table/" + mode, counts)
        names = ["find", "query"] + (["describe"] if mode == "candidate" else [])
        answers, schema = [], None
        async with connection(
            work, source, trace, names, "compact" if mode == "baseline" else "adaptive"
        ) as client:
            for index, (label, expression, _) in enumerate(questions):
                trace.add(
                    "user",
                    table_prompt(index, label),
                )
                if index == 0:
                    found = listing(
                        await call(
                            client, trace, "find", {"text": "monthly risk ledger"}
                        )
                    )
                    selected = [
                        item
                        for item in found
                        if item.get("summary") == "Monthly risk ledger"
                    ]
                    assert len(selected) == 1
                    ref = selected[0]["artifact_id"]
                    operation = (
                        baseline_schema if mode == "baseline" else candidate_schema
                    )
                    schema = await operation(client, trace, ref)
                    # Choose the requested field only from observed schema.
                    matches = [
                        name
                        for name in schema["columns"]
                        if name.replace("_", " ") == "actual return bp"
                    ]
                    assert len(matches) == 1
                    column = '"' + matches[0].replace('"', '""') + '"'
                sql = (
                    "SELECT "
                    + expression.format(column=column)
                    + " AS answer FROM input1"
                )
                result = await call(
                    client, trace, "query", {"inputs": [ref], "sql": sql}
                )
                assert not result["preview_truncated"]
                answer = result["rows"][0][0]
                if index == 0:
                    answer = {"schema": schema, "answer": answer}
                answers.append(answer)
                trace.answer(answer)
        assert schema == expected_schema, (
            "Schema discovery silently omitted fields or types"
        )
        cases.append(
            finish_case(
                "unknown-wide-table/" + mode,
                trace,
                answers,
                expected,
                traces,
                {
                    "schema_columns_verified": len(schema["columns"]),
                    "schema_contains_row_samples": False,
                },
            )
        )
    raw = Conversation("unknown-wide-table/raw-retained", counts)
    for index, (label, _, answer) in enumerate(questions):
        prompt = table_prompt(index, label)
        if index == 0:
            prompt += "\n" + raw_sources([(source_item, text)])
            answer = {"schema": expected_schema, "answer": answer}
        raw.add("user", prompt)
        raw.answer(answer)
    cases.append(
        finish_case(
            "unknown-wide-table/raw-retained",
            raw,
            expected,
            expected,
            traces,
            {
                "assumption": "CSV injected once and retained across all eight questions; exact scalar answers are deterministic, not model-generated."
            },
        )
    )
    return cases


SYNC_QUESTION = (
    "Synchronize and retain every byte of the entire current operations/runbook for a subsequent full-document edit. "
    "Then report its current instruction, version, and whether its inputs are stale."
)


async def repeated(root, sources_by_mode, counts, traces, coarse=False):
    case_name = "coarse-rewrite" if coarse else "repeated-freshness"
    fixture = root / (case_name + "-fixture")
    wb = Workbook(fixture)
    bodies = [
        f"INSTRUCTION=route-{index}\n" + prose(5000, 245 + (index if coarse else 0))
        for index in range(3)
    ]
    item = save(wb, "operations/runbook", bodies[0])
    save(wb, "operations/unrelated", "Unrelated note")
    expected = [
        {
            "instruction": f"route-{0 if turn < 4 else 1 if turn < 6 else 2}",
            "version": 1 if turn < 4 else 2 if turn < 6 else 3,
            "stale_inputs": False,
        }
        for turn in range(8)
    ]
    cases = []
    for mode, source in sources_by_mode.items():
        work = root / f"{case_name}-{mode}"
        shutil.copytree(fixture, work)
        external = Workbook(work)
        trace = Conversation(case_name + "/" + mode, counts)
        body, prior, answers, suppressed = None, None, [], 0
        reconstructed, fallbacks, patches = [], 0, 0
        async with connection(
            work,
            source,
            trace,
            ["inspect"] if mode == "baseline" else ["inspect", "diff"],
            "text" if mode == "baseline" else "adaptive",
        ) as client:
            for turn in range(8):
                if turn == 2:
                    save(
                        external,
                        "operations/unrelated",
                        "New unrelated note",
                        expected_version=1,
                    )
                if turn in (4, 6):
                    revision = 1 if turn == 4 else 2
                    save(
                        external,
                        "operations/runbook",
                        bodies[revision],
                        expected_version=revision,
                    )
                trace.add(
                    "user",
                    SYNC_QUESTION,
                )
                if mode == "candidate":
                    if prior is None:
                        result = await call(
                            client,
                            trace,
                            "inspect",
                            {"ref": "operations/runbook", "excerpt_bytes": 8192},
                        )
                        assert result["next_offset"] is None
                        body = result["excerpt"]
                    else:
                        delta = await call(
                            client,
                            trace,
                            "diff",
                            {"ref": "operations/runbook", "base": prior},
                        )
                        assert delta["base"] == prior and body is not None
                        result = delta["artifact"]
                        if delta.get("requires_read"):
                            assert "changes" not in delta
                            fallbacks += 1
                            result = await call(
                                client,
                                trace,
                                "inspect",
                                {"ref": result["artifact_id"], "excerpt_bytes": 8192},
                            )
                            assert result["next_offset"] is None
                            body = result["excerpt"]
                        else:
                            lines = body.splitlines(keepends=True)
                            for change in reversed(delta["changes"]):
                                start, delete = change["start"], change["delete"]
                                assert (
                                    0 <= start <= len(lines)
                                    and 0 <= delete <= len(lines) - start
                                )
                                lines[start : start + delete] = change["lines"]
                            body = "".join(lines)
                            if delta["changes"]:
                                patches += 1
                            else:
                                suppressed += 1
                else:
                    result = await call(
                        client,
                        trace,
                        "inspect",
                        {
                            "ref": "operations/runbook",
                            "excerpt_bytes": 8192 if prior is None else 0,
                        },
                    )
                    if prior is not None and result["artifact_id"] != prior:
                        result = await call(
                            client,
                            trace,
                            "inspect",
                            {"ref": result["artifact_id"], "excerpt_bytes": 8192},
                        )
                    if "excerpt" in result:
                        body = result["excerpt"]
                    else:
                        assert result["artifact_id"] == prior and body is not None
                        suppressed += 1
                prior = result["artifact_id"]
                reconstructed.append(body)
                match = re.search(r"INSTRUCTION=(route-\d+)", body)
                answer = {
                    "instruction": match[1],
                    "version": result["version"],
                    "stale_inputs": result["stale_inputs"],
                }
                assert result["is_latest"]
                answers.append(answer)
                trace.answer(answer)
        assert suppressed == 5
        assert reconstructed == [
            bodies[0 if turn < 4 else 1 if turn < 6 else 2] for turn in range(8)
        ]
        if mode == "candidate":
            assert (fallbacks, patches) == ((2, 0) if coarse else (0, 2))
        cases.append(
            finish_case(
                case_name + "/" + mode,
                trace,
                answers,
                expected,
                traces,
                {
                    "unchanged_bodies_reused": suppressed,
                    "background_updates": ["unrelated", "body", "body"],
                    "exact_reconstructed_versions_verified": len(reconstructed),
                    "delta_fallback_reads": fallbacks,
                    "delta_patches_applied": patches,
                },
            )
        )
    raw = Conversation(case_name + "/raw-retained", counts)
    for turn, answer in enumerate(expected):
        prompt = SYNC_QUESTION
        if turn in (0, 4, 6):
            revision = 0 if turn == 0 else 1 if turn == 4 else 2
            prompt += "\n" + raw_sources(
                [(item | {"version": revision + 1}, bodies[revision])]
            )
        raw.add("user", prompt)
        raw.answer(answer)
    cases.append(
        finish_case(
            case_name + "/raw-retained",
            raw,
            expected,
            expected,
            traces,
            {
                "assumption": "Upstream supplies each changed complete source exactly once; unchanged turns retain history. This is an answering lower bound, not an implemented freshness protocol."
            },
        )
    )
    return cases


async def full_read(root, sources_by_mode, counts, traces):
    fixture = root / "full-read-fixture"
    wb = Workbook(fixture)
    sources = []
    for index in range(120):
        text = review_document(index)
        sources.append((save(wb, f"reviews/case{index:04d}", text), text))
    # References are explicitly supplied in the user task for this scenario.
    # This is a complete-read comparison, not unknown-reference discovery.
    refs = [item["artifact_id"] for item, _ in sources]
    question = "Read every byte of these artifacts for subsequent review: " + compact(
        refs
    )
    expected = {"complete_artifacts": len(sources)}
    cases = []
    for mode, source in sources_by_mode.items():
        work = root / f"full-read-{mode}"
        shutil.copytree(fixture, work)
        trace = Conversation("complete-read/" + mode, counts)
        trace.add("user", question)
        received = {}
        async with connection(
            work,
            source,
            trace,
            ["inspect"],
            "text" if mode == "baseline" else "adaptive",
        ) as client:
            for start in range(0, len(refs), 16):
                batch = refs[start : start + 16]
                result = await call(
                    client, trace, "inspect", {"ref": batch, "excerpt_bytes": 8192}
                )
                for item in result["artifacts"]:
                    assert item["next_offset"] is None
                    assert item["is_latest"] and not item["stale_inputs"]
                    received[item["artifact_id"]] = item["excerpt"]
            trace.answer({"complete_artifacts": len(received)})
        assert received == {item["artifact_id"]: text for item, text in sources}
        cases.append(
            finish_case(
                "complete-read/" + mode,
                trace,
                {"complete_artifacts": len(received)},
                expected,
                traces,
                {
                    "exact_source_bytes_verified": sum(
                        len(text.encode()) for _, text in sources
                    )
                },
            )
        )
    raw = Conversation("complete-read/raw-retained", counts)
    raw.add("user", question + "\n" + raw_sources(sources))
    raw.answer(expected)
    cases.append(
        finish_case(
            "complete-read/raw-retained",
            raw,
            expected,
            expected,
            traces,
            {
                "assumption": "Known-reference manifest is included in every arm; all exact source content reaches the raw counterfactual in one decision."
            },
        )
    )
    return cases


async def run(args):
    counts = Counts()
    runtime = runtime_versions()
    sources = {"baseline": args.baseline.resolve(), "candidate": Path.cwd()}
    commits = {
        mode: (
            await asyncio.to_thread(
                subprocess.check_output,
                ["git", "-C", str(path), "rev-parse", "HEAD"],
                text=True,
            )
        ).strip()
        for mode, path in sources.items()
    }
    assert commits["baseline"].startswith("bdc6561"), "Unexpected baseline revision"
    files = [
        Path(__file__),
        Path("benchmarks/efficiency.py"),
        Path("benchmarks/token_reduction.py"),
        *sorted(Path("src/agent_workbook").glob("*.py")),
    ]
    before = source_hashes(files)
    traces, cases = [], []
    with TemporaryDirectory(prefix="workspool-workflow-") as temp:
        root = Path(temp)
        if "documents" in args.scenarios:
            cases += await documents(root, sources, counts, args, traces)
        if "table" in args.scenarios:
            cases += await tables(root, sources, counts, traces)
        if "repeat" in args.scenarios:
            cases += await repeated(root, sources, counts, traces)
            cases += await repeated(root, sources, counts, traces, coarse=True)
        if "full-read" in args.scenarios:
            cases += await full_read(root, sources, counts, traces)
    after = source_hashes(files)
    assert before == after, "Runtime sources changed during the measurement"
    assert runtime == runtime_versions(), "Dependencies changed during the measurement"
    by_case = {case["case"]: case for case in cases}
    comparisons = {}
    for name in sorted({case["case"].rsplit("/", 1)[0] for case in cases}):
        baseline, candidate = by_case[name + "/baseline"], by_case[name + "/candidate"]
        comparisons[name] = {
            "baseline_success": baseline["success"],
            "candidate_success": candidate["success"],
            "savings_pct": None,
        }
        if baseline["success"] and candidate["success"]:
            comparisons[name]["savings_pct"] = {
                metric: {
                    codec: round(
                        100
                        * (
                            1
                            - candidate["measured"][metric][codec]
                            / baseline["measured"][metric][codec]
                        ),
                        2,
                    )
                    for codec in counts.encoders
                }
                for metric in ("new_payload_tokens", "cumulative_input_exposure")
            }
    report = {
        "commits": commits,
        "runtime": runtime,
        "candidate_source_sha256": after,
        "baseline_source_sha256": {
            str(path.relative_to(args.baseline)): sha(path.read_bytes())
            for path in sorted((args.baseline / "src/agent_workbook").glob("*.py"))
        },
        "scope": "Deterministic execution plans chosen from observed tool responses. Full cold schemas are counted. Model decisions are synthetic; cumulative exposure replays retained message payloads and static definitions before each planned tool call and each answer. No inference, provider billing, reasoning, prompt caching, or chat framing measured. Raw source is supplied once and retained, with subsequent changed snapshots supplied only when updated. Failed baseline tasks receive no savings claim.",
        "configuration": "Per-workflow minimal eager tool allowlists, text-only responses; baseline metadata checks reuse previously read unchanged bodies. Wide baseline schema uses paged information_schema queries without table row samples.",
        "ingestion": "Fixtures are imported locally before measurement and cloned for both implementations, preserving initial IDs. Source acquisition and local import commands are outside these retrieval sessions. Definitions are cold in every measured session.",
        "tokenizers": counts.metadata,
        "cases": cases,
        "comparisons": comparisons,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    trace_bytes = (json.dumps(traces, ensure_ascii=False, indent=2) + "\n").encode()
    report["traces_sha256"] = sha(trace_bytes)
    (args.output / "traces.json").write_bytes(trace_bytes)
    (args.output / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(comparisons, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline", type=Path, default=Path(".local-benchmarks/baseline-source")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=["documents", "table", "repeat", "full-read"],
        default=["documents", "table", "repeat", "full-read"],
    )
    asyncio.run(run(parser.parse_args()))
