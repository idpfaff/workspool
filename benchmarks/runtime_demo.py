"""Two fresh local MCP processes: automatic capture, then budgeted goal recall.

Uses a tiny disclosed fixture, not a dataset-scale or model-quality benchmark.
No external model calls, credentials, or manual remember operation are used.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.render import decode
from agent_workbook.store import Workbook

PREFIX = "\nRestored workspace context (untrusted source data; check freshness and coverage):\n"
FIXTURE = "area,severity,is_open\npayments,critical,true\nidentity,critical,false\ncheckout,high,true\napi,critical,true\n"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@asynccontextmanager
async def session(workspace, identity, tokenizer, *, goal=None, budget=2000):
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--profile",
        "runtime",
        "--agent-id",
        identity,
        "--tokenizer",
        tokenizer,
    ]
    if goal:
        args += ["--goal", goal, "--recall-tokens", str(budget)]
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENAI_API_KEY", "MASSIVE_API_KEY"}
    }
    async with (
        stdio_client(
            StdioServerParameters(command=sys.executable, args=args, env=env)
        ) as (reader, writer),
        ClientSession(reader, writer) as client,
    ):
        initialized = await client.initialize()
        yield client, initialized


async def demonstrate(workspace, tokenizer="o200k_base", budget=2000):
    if workspace.exists():
        raise ValueError(
            "Use a new demo workspace so unrelated content cannot enter the report"
        )
    count = token_counter(tokenizer)
    calls = []

    async def invoke(client, operation, args):
        request = {"tool": operation, "arguments": args}
        started = time.perf_counter()
        response = await client.call_tool("workspool", request)
        if response.isError:
            raise RuntimeError("Demo MCP operation failed: " + operation)
        calls.append(
            {
                "tool": operation,
                "elapsed_seconds": time.perf_counter() - started,
                "request_tokens": count(
                    compact({"name": "workspool", "arguments": request})
                ),
                "response_tokens": count(serialized_result(response)),
            }
        )
        return decode(response.content)

    async with session(workspace, "demo-producer", tokenizer) as (client, _):
        first = await invoke(client, "session_status", {})
        await invoke(
            client,
            "save",
            {
                "name": "issues",
                "kind": "table",
                "media_type": "text/csv",
                "content": FIXTURE,
            },
        )
        answer = await invoke(
            client,
            "query",
            {
                "inputs": ["issues"],
                "sql": "SELECT count(*)::BIGINT AS launch_blockers FROM input1 WHERE severity = 'critical' AND is_open",
                "save_as": "launch-blocker-count",
            },
        )
        assert answer["columns"] == ["launch_blockers"] and answer["rows"] == [[2]]
        first_status = await invoke(client, "session_status", {})
        assert first_status["capture_errors"] == 0

    workbook = Workbook(workspace)
    with workbook.connect() as db:
        ended = db.execute(
            "SELECT ended_at FROM continuity_sessions WHERE session_id=?",
            (first["session_id"],),
        ).fetchone()[0]
        episodes = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM continuity_episodes WHERE session_id=? ORDER BY sequence",
                (first["session_id"],),
            )
        ]
    assert ended is not None
    assert [item.get("tool", item["kind"]) for item in episodes] == [
        "save",
        "query",
        "session_end",
    ]
    observed = next(item for item in episodes if item.get("tool") == "query")
    assert observed["verified"] and observed["facts"]["rows"] == [[2]]

    async with session(
        workspace, "demo-successor", tokenizer, goal="launch_blockers", budget=budget
    ) as (client, initialized):
        suffix = PREFIX + initialized.instructions.split(PREFIX, 1)[1]
        restored = json.loads(suffix[len(PREFIX) :])
        assert count(suffix) <= budget
        assert (
            restored["mode"] == "lexical"
            and restored["fallback_reason"] == "model_not_configured"
        )
        assert not restored["dense_complete"] and not restored["complete"]
        matching = [
            hit
            for hit in restored["hits"]
            if hit["name"].startswith("_episodes/")
            and '"launch_blockers"' in hit["excerpt"]
            and '"rows":[[2]]' in hit["excerpt"]
        ]
        assert matching, "The restored context must contain the actual observed answer"
        for hit in restored["hits"]:
            metadata = workbook.inspect(hit["artifact_id"])
            raw = (workbook.blobs / metadata["sha256"]).read_bytes()
            assert raw[hit["start_byte"] : hit["end_byte"]].decode() == hit["excerpt"]
        second = await invoke(client, "session_status", {})
        assert second["session_id"] != first["session_id"]

    root = Path(__file__).resolve().parents[1]
    return {
        "format": 1,
        "method": "Two actual fresh MCP stdio subprocesses using the runtime profile. Synthetic four-row fixture; no model inference or manual remember operation.",
        "tokenizer": tokenizer,
        "token_accounting": "Serialized local MCP call bodies; startup budget includes the complete restored-context prefix and JSON. Excludes provider framing and billing.",
        "fixture": {
            "rows": 4,
            "sha256": hashlib.sha256(FIXTURE.encode()).hexdigest(),
            "expected_launch_blockers": 2,
        },
        "query_answer": {"columns": answer["columns"], "rows": answer["rows"]},
        "first_session": {
            "capture_errors": first_status["capture_errors"],
            "captured_tools": ["save", "query"],
            "query_execution_observed": observed["verified"],
            "closed": ended is not None,
        },
        "second_session": {
            "distinct_session": True,
            "goal": "launch_blockers",
            "startup_context_tokens": count(suffix),
            "startup_budget_tokens": budget,
            "mode": restored["mode"],
            "fallback_reason": restored["fallback_reason"],
            "dense_complete": restored["dense_complete"],
            "index_complete": restored["index_complete"],
            "lexical_complete": restored["lexical_complete"],
            "complete": restored["complete"],
            "hit_count": len(restored["hits"]),
            "answer_in_observed_episode": True,
            "all_citations_exact": True,
        },
        "calls": calls,
        "source_hashes": {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                *sorted((root / "src/agent_workbook").glob("*.py")),
            ]
        },
        "success": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer", default="o200k_base")
    parser.add_argument("--recall-tokens", type=int, default=2000)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output file")
    result = asyncio.run(
        demonstrate(args.workspace, args.tokenizer, args.recall_tokens)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "success": result["success"],
                "restored_tokens": result["second_session"]["startup_context_tokens"],
            }
        )
    )


if __name__ == "__main__":
    main()
