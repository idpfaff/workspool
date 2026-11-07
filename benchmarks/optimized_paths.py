"""Measure explicit batch reads and tool allowlists, with unchanged source evidence.

Run with the same environment and tokenizer as token_reduction.py. These paths
are reported separately from the unchanged-client benchmark.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import token_reduction as bench
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def session(workspace, trace, tools=None):
    args = ["-m", "agent_workbook.cli", "--workspace", str(workspace), "serve"]
    if tools:
        args += ["--tools", *tools]
    params = StdioServerParameters(command=sys.executable, args=args)
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        definitions = await client.list_tools()
        trace.record("setup", initialized.instructions or "")
        trace.record(
            "setup",
            [t.model_dump(by_alias=True, exclude_none=True) for t in definitions.tools],
        )
        yield client


async def run(args):
    counts = bench.Counts(args.tokenizer)
    traces = []
    cases = []
    with TemporaryDirectory(prefix="workspool-optimized-") as folder:
        root = Path(folder)
        workspace = root / "workbook"
        ingest = bench.Trace(
            "optimized/import-documents-and-checkpoint", counts, traces
        )
        texts = [bench.document_fixture(i) for i in range(120)]
        sources = []
        for i, text in enumerate(texts):
            path = root / f"case{i:04d}.md"
            path.write_text(text)
            sources.append(
                bench.import_file(workspace, path, f"reviews/case{i:04d}", ingest)
            )
        selected = [7, 60, 119]
        question = "Return the exact decision, owner, and review code for case0007, case0060, and case0119, with saved version references."
        async with session(workspace, ingest) as client:
            await bench.call(
                client,
                ingest,
                "save",
                {
                    "name": "reviews/handoff",
                    "kind": "checkpoint",
                    "checkpoint": {
                        "objective": "Continue the engineering review.",
                        "verified": "Imported 120 synthetic review documents.",
                        "next_step": question,
                    },
                    "dependencies": [sources[i]["artifact_id"] for i in selected],
                },
            )
        ingestion = ingest.measure()
        raw = bench.plain_payload(counts, question, "\n".join(texts))
        for tools in (None, ["resume", "inspect"]):
            label = "all-tools" if tools is None else "two-tools"
            trace = bench.Trace("optimized/targeted-batch/" + label, counts, traces)
            trace.record("prompts", question)
            async with session(workspace, trace, tools) as client:
                handoff = await bench.call(
                    client, trace, "resume", {"text": "reviews/handoff"}
                )
                refs = handoff["checkpoint"]["dependencies"]
                result = await bench.call(
                    client, trace, "inspect", {"ref": refs, "excerpt_bytes": 200}
                )
                for i, item in zip(selected, result["artifacts"], strict=True):
                    assert item["artifact_id"] == sources[i]["artifact_id"]
                    assert item["excerpt"].startswith(
                        "\n".join(texts[i].splitlines()[:4])
                    )
                    assert item["is_latest"] and not item["stale_inputs"]
            measured = trace.measure()
            cases.append(
                {
                    "case": "targeted_batch_" + label,
                    "measured": measured,
                    "comparison": bench.comparison(counts, raw, measured),
                }
            )
        question = "Return every word of all review documents. Do not summarize."
        for tools in (None, ["inspect"]):
            label = "all-tools" if tools is None else "one-tool"
            trace = bench.Trace(
                "optimized/complete-reread-batch/" + label, counts, traces
            )
            trace.record("prompts", question)
            async with session(workspace, trace, tools) as client:
                for start in range(0, len(sources), 16):
                    refs = [s["artifact_id"] for s in sources[start : start + 16]]
                    result = await bench.call(
                        client, trace, "inspect", {"ref": refs, "excerpt_bytes": 8192}
                    )
                    assert len(result["artifacts"]) == len(refs)
                    for index, item in enumerate(result["artifacts"], start):
                        assert item["excerpt"] == texts[index]
                        assert item["next_offset"] is None
                        assert item["is_latest"] and not item["stale_inputs"]
            measured = trace.measure()
            raw = bench.plain_payload(counts, question, "\n".join(texts))
            cases.append(
                {
                    "case": "complete_reread_batch_" + label,
                    "measured": measured,
                    "comparison": bench.comparison(counts, raw, measured),
                }
            )
        for n in (20, 2000):
            path = root / f"bars-{n}.csv"
            expected = bench.csv_fixture(path, n)
            ingest = bench.Trace(f"optimized/import-table-{n}", counts, traces)
            bench.import_file(workspace, path, f"replay/bars-{n}", ingest)
            initial = ingest.measure()
            question = f"Data: replay/bars-{n}. " + bench.QUESTION
            raw = bench.plain_payload(counts, question, path.read_text())
            for tools in (None, ["query"]):
                label = "all-tools" if tools is None else "one-tool"
                trace = bench.Trace(
                    f"optimized/known-input-{n}/" + label, counts, traces
                )
                trace.record("prompts", question)
                async with session(workspace, trace, tools) as client:
                    result = await bench.call(
                        client,
                        trace,
                        "query",
                        {"inputs": [f"replay/bars-{n}"], "sql": bench.SQL},
                    )
                    assert result["rows"] == [expected]
                measured = trace.measure()
                cases.append(
                    {
                        "case": f"known_input_{n}_" + label,
                        "measured": measured,
                        "comparison": bench.comparison(counts, raw, measured),
                        "separate_local_import": initial,
                    }
                )
    commit = await asyncio.to_thread(
        subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
    )
    report = {
        "code_commit": commit.strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Explicit client optimizations: batch inspect and tool allowlists. Same generated sources and exact outputs. Warm session counts; one-time local imports and checkpoint are recorded separately. No LLM inference.",
        "tokenizers": list(counts.encoders),
        "document_ingestion": ingestion,
        "cases": cases,
    }
    bench.write_report(args.output, report, traces)
    for case in cases:
        print(case["case"], json.dumps(case["comparison"]), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
