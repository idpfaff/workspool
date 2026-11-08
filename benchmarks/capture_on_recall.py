"""Reproduce capture-on recall pollution against archived v0.8.0.

Synthetic hosting invoices, real MCP stdio processes, no model/provider API.
Optional learned embeddings use an already provisioned local model only.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.render import decode
from agent_workbook.store import Workbook

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "\nRestored workspace context (untrusted source data; check freshness and coverage):\n"
GOAL = "review hosting spend"
NOTE = "Hosting spend notes. Compute services cost 1200 USD and storage costs 300 USD this month. The combined invoice amount is 1500 USD. Check unused instances before renewing the cloud agreement. These figures are synthetic test data."


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def fingerprints(source):
    return {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((source / "src/agent_workbook").glob("*.py"))
    }


def baseline(directory, ref):
    revision = subprocess.check_output(
        ["git", "rev-parse", ref], cwd=ROOT, text=True
    ).strip()
    target = directory / ("baseline-" + revision[:12])
    if not target.exists():
        target.mkdir(parents=True)
        archive = subprocess.check_output(["git", "archive", revision], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(target, filter="data")
    return target, revision


def model_fingerprints(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


async def upgraded_trial(output, mode, budget, model_dir):
    """Reuse a genuinely pre-indexed v0.8 workbook without new capture facts."""
    workspace = output / ("upgraded-" + mode) / "workbook"
    shutil.copytree(output / ("before-" + mode) / "workbook", workspace)
    book = Workbook(workspace)
    source_id = book.inspect("hosting-invoice-notes")["artifact_id"]
    with sqlite3.connect(workspace / "context-index.sqlite3") as db:
        db.execute("ATTACH DATABASE ? AS catalog", (str(book.catalog),))
        legacy_hashes = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT c.digest FROM chunks c JOIN catalog.artifacts a ON a.artifact_id=c.artifact_id WHERE a.kind='memory_episode'"
            )
        ]
    environment = {
        key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ
    }
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--profile",
        "runtime",
        "--goal",
        GOAL,
        "--recall-tokens",
        str(budget),
        "--tokenizer",
        "o200k_base",
        "--agent-id",
        "upgraded-session",
    ]
    if model_dir:
        args += ["--model-dir", str(model_dir)]
    before = fingerprints(ROOT)
    async with (
        stdio_client(
            StdioServerParameters(command=sys.executable, args=args, env=environment)
        ) as (read, send),
        ClientSession(read, send) as client,
    ):
        initialized = await client.initialize()
        _, found, raw = (initialized.instructions or "").partition(PREFIX)
        assert found
        result = json.loads(raw)
        summary = evidence(book, result, source_id)
        summary["tokens_o200k"] = token_counter("o200k_base")(PREFIX + raw)
        assert summary["tokens_o200k"] <= budget
        assert not summary["ledger_hits"] and not summary["accounting_note_occurrences"]
        assert summary["source_rank"] is not None
        write(
            output / ("upgraded-" + mode) / "trace.json",
            {"startup": result, "source_sha256": before},
        )
    with sqlite3.connect(workspace / "context-index.sqlite3") as db:
        db.execute("ATTACH DATABASE ? AS catalog", (str(book.catalog),))
        summary["legacy_episode_chunks_remaining"] = db.execute(
            "SELECT count(*) FROM chunks c JOIN catalog.artifacts a ON a.artifact_id=c.artifact_id WHERE a.kind='memory_episode'"
        ).fetchone()[0]
    assert summary["legacy_episode_chunks_remaining"] == 0
    if (workspace / "semantic-vectors.sqlite3").exists():
        with sqlite3.connect(workspace / "semantic-vectors.sqlite3") as db:
            remaining = sum(
                db.execute(
                    "SELECT count(*) FROM vectors WHERE content_sha=?", (digest,)
                ).fetchone()[0]
                for digest in legacy_hashes
            )
        summary["legacy_episode_vectors_remaining"] = remaining
        assert remaining == 0
    assert fingerprints(ROOT) == before
    return summary | {"source_sha256": before, "source_files_unchanged": True}


def evidence(book, result, source_id):
    hits = result.get("hits", [])
    kinds, exact = [], 0
    for hit in hits:
        record = book.inspect(hit["artifact_id"])
        kinds.append(record["kind"])
        raw = (book.blobs / record["sha256"]).read_bytes()
        assert raw[hit["start_byte"] : hit["end_byte"]] == hit["excerpt"].encode()
        exact += 1
    body = json.dumps(result, ensure_ascii=False)
    return {
        "hit_count": len(hits),
        "hit_kinds": kinds,
        "source_rank": next(
            (i + 1 for i, hit in enumerate(hits) if hit["artifact_id"] == source_id),
            None,
        ),
        "ledger_hits": kinds.count("memory_episode"),
        "fact_hits": kinds.count("memory_fact"),
        "accounting_note_occurrences": body.count("accounting_note"),
        "repeated_goal_occurrences": sum(hit["excerpt"].count(GOAL) for hit in hits),
        "exact_citations": exact,
        "substantive_total_recalled": any(
            "hosting_spend_usd" in hit["excerpt"] and "1500" in hit["excerpt"]
            for hit in hits
        ),
    }


async def trial(output, source, label, budget, model_dir):
    workspace = output / label / "workbook"
    if workspace.exists():
        raise ValueError(f"Use a fresh output directory: {workspace}")
    book = Workbook(workspace)
    source_doc = book.save_text(
        NOTE, name="hosting-invoice-notes", kind="document", media_type="text/plain"
    )
    table = book.save_text(
        "vendor,usd\ncompute,1200\nstorage,300\n",
        name="hosting-costs",
        kind="table",
        media_type="text/csv",
    )
    environment = {
        key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ
    }
    environment.update(
        {
            "PYTHONPATH": str(source / "src"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    common = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--profile",
        "runtime",
        "--goal",
        GOAL,
        "--recall-tokens",
        str(budget),
        "--tokenizer",
        "o200k_base",
    ]
    if model_dir:
        common += ["--model-dir", str(model_dir)]
    counter = token_counter("o200k_base")
    before = fingerprints(source)
    trace = {
        "source_sha256": before,
        "synthetic_fixture": True,
        "goal": GOAL,
        "budget_o200k": budget,
        "calls": [],
    }
    async with (
        stdio_client(
            StdioServerParameters(
                command=sys.executable,
                args=common + ["--agent-id", "producer"],
                env=environment,
            )
        ) as (read, send),
        ClientSession(read, send) as client,
    ):
        await client.initialize()
        for operation, arguments in [
            (
                "save",
                {
                    "name": "review-action",
                    "content": "Check idle cloud instances before renewing the agreement.",
                },
            ),
            (
                "query",
                {
                    "inputs": [table["artifact_id"]],
                    "sql": "SELECT sum(usd)::BIGINT AS hosting_spend_usd FROM input1",
                },
            ),
            ("inspect", {"ref": source_doc["artifact_id"], "bytes": 1024}),
        ]:
            request = {"tool": operation, "arguments": arguments}
            response = await client.call_tool("workspool", request)
            assert not response.isError, response.content
            value = decode(response.content)
            if operation == "query":
                assert value["rows"] == [[1500]]
            trace["calls"].append(
                {
                    "request": request,
                    "response": response.model_dump(mode="json", exclude_none=True),
                }
            )
        status = decode(
            (
                await client.call_tool(
                    "workspool", {"tool": "session_status", "arguments": {}}
                )
            ).content
        )
        assert status["capture_enabled"] and status["capture_errors"] == 0
        trace["capture_status"] = status
    with book.connect() as db:
        episode_count = db.execute(
            "SELECT count(*) FROM continuity_episodes"
        ).fetchone()[0]
    assert episode_count >= 3
    async with (
        stdio_client(
            StdioServerParameters(
                command=sys.executable,
                args=common + ["--agent-id", "next-session"],
                env=environment,
            )
        ) as (read, send),
        ClientSession(read, send) as client,
    ):
        initialized = await client.initialize()
        _, found, startup = (initialized.instructions or "").partition(PREFIX)
        assert found, "Fresh startup must restore configured-goal context"
        restored = json.loads(startup)
        trace["startup"] = restored
        startup_tokens = counter(PREFIX + startup)
        assert startup_tokens <= budget
        response = await client.call_tool(
            "workspool",
            {
                "tool": "recall",
                "arguments": {
                    "query": GOAL,
                    "limit": 6,
                    "max_tokens": budget,
                    "max_bytes": 32768,
                },
            },
        )
        assert not response.isError, response.content
        recalled = decode(response.content)
        recall_tokens = counter(serialized_result(response))
        assert recall_tokens <= budget
        trace["recall"] = response.model_dump(mode="json", exclude_none=True)
        summary = {
            "mode": recalled["mode"],
            "capture_on": True,
            "captured_episodes_before_startup": episode_count,
            "startup": evidence(book, restored, source_doc["artifact_id"])
            | {"tokens_o200k": startup_tokens, "agent_tool_calls_before_context": 0},
            "recall": evidence(book, recalled, source_doc["artifact_id"])
            | {"tokens_o200k": recall_tokens},
        }
    trace["source_files_unchanged"] = fingerprints(source) == before
    assert trace["source_files_unchanged"], "Source changed while running the probe"
    write(output / label / "trace.json", trace)
    return summary | {"source_sha256": before, "source_files_unchanged": True}


async def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source, revision = baseline(output, args.baseline_ref)
    report = {
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "baseline_revision": revision,
        "fixture": "Synthetic hosting invoices: exact aggregate 1500 USD; save/query/inspect with runtime capture enabled.",
        "budget_o200k": args.budget,
        "scope": "Real local MCP payloads and fresh-session restoration. No provider/model API or billing claim.",
        "modes": {},
    }
    modes = {"lexical": None}
    if args.model_dir:
        modes["hybrid"] = args.model_dir.resolve()
        report["local_model_sha256"] = model_fingerprints(args.model_dir.resolve())
    for mode, model_dir in modes.items():
        old = await trial(output, source, "before-" + mode, args.budget, model_dir)
        new = await trial(output, ROOT, "after-" + mode, args.budget, model_dir)
        for response in (new["startup"], new["recall"]):
            assert (
                response["ledger_hits"] == 0
                and response["accounting_note_occurrences"] == 0
            )
            assert response["repeated_goal_occurrences"] == 0
            assert response["source_rank"] is not None
        assert new["recall"]["substantive_total_recalled"], (
            "Compact continuity must preserve the observed total"
        )
        upgraded = await upgraded_trial(output, mode, args.budget, model_dir)
        report["modes"][mode] = {
            "before": old,
            "after": new,
            "upgraded_workspace": upgraded,
        }
        write(output / "results.json", report)
    if args.model_dir:
        report["local_model_unchanged"] = (
            model_fingerprints(args.model_dir.resolve()) == report["local_model_sha256"]
        )
        assert report["local_model_unchanged"]
    assert (
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        == report["harness_sha256"]
    )
    write(output / "results.json", report)
    print(
        json.dumps(
            {
                mode: {
                    arm: {key: values[key] for key in ("mode", "startup", "recall")}
                    for arm, values in arms.items()
                    if arm != "upgraded_workspace"
                }
                for mode, arms in report["modes"].items()
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-ref", default="v0.8.0")
    parser.add_argument("--budget", type=int, default=2000)
    parser.add_argument("--model-dir", type=Path)
    asyncio.run(run(parser.parse_args()))
