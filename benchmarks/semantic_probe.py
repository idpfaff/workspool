"""Real local BGE retrieval on a fixed, manually labeled Wikipedia subset.

No downloads, network requests or model API calls. Private output keeps exact
traces; the optional public JSON contains aggregates and public source identities,
never article excerpts, artifact IDs, model directories or private local paths.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STARTUP_PREFIX = "\nRestored workspace context (untrusted source data; check freshness and coverage):\n"
# Authored after reading source passages, before executing any retrieval. Gold
# identifies articles, not a claim that every returned passage answers a question.
CASES = [
    {
        "target": "Albedo",
        "query": "Why do bright snowy landscapes send much more incoming sunshine back into the sky than dark road surfaces?",
    },
    {
        "target": "International Atomic Time",
        "query": "How do laboratories combine many highly precise clocks to maintain a continuous global time reference?",
    },
    {
        "target": "Altruism",
        "query": "What is the idea of benefiting someone else despite a personal sacrifice and without expecting repayment?",
    },
    {
        "target": "ASCII",
        "query": "Which computer text convention fits English letters, punctuation and nonprinting instructions into seven binary digits?",
    },
    {
        "target": "An American in Paris",
        "query": "What Gershwin orchestral work uses cab horns to evoke a tourist's walk through the French capital?",
    },
]


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def source_hashes():
    return {
        str(path.relative_to(ROOT)): digest(path)
        for path in [
            Path(__file__),
            *sorted((ROOT / "src/agent_workbook").glob("*.py")),
        ]
    }


def select_documents(manifest, count):
    if type(count) is not int or not 24 <= count <= 64:
        raise ValueError("Use a deterministic first 24–64 manifest articles")
    selected = manifest["documents"][:count]
    if len(selected) != count or len({item["title"] for item in selected}) != count:
        raise ValueError("Manifest subset is incomplete or contains duplicate titles")
    missing = {case["target"] for case in CASES} - {item["title"] for item in selected}
    if missing:
        raise ValueError(
            f"Selected subset omits predeclared targets: {sorted(missing)}"
        )
    return selected


def exact_citations(result, sources):
    for hit in result["hits"]:
        item = sources[hit["artifact_id"]]
        with Path(item["path"]).open("rb") as stream:
            stream.seek(hit["start_byte"])
            raw = stream.read(hit["end_byte"] - hit["start_byte"])
        if raw != hit["excerpt"].encode() or hit["sha256"] != item["sha256"]:
            raise AssertionError(
                "Citation differs from independently read original source bytes"
            )
    return len(result["hits"])


def ranks(result, sources, target):
    titles = [sources[hit["artifact_id"]]["title"] for hit in result["hits"]]
    return {
        "target_rank": next(
            (index for index, title in enumerate(titles, 1) if title == target), None
        ),
        "returned_titles": titles,
    }


def load_counters():
    spec = importlib.util.spec_from_file_location(
        "_semantic_probe_support", Path(__file__).with_name("task_stress.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name: module.load_counter(name) for name in ("cl100k_base", "o200k_base")}


async def mcp_probe(workspace, model_dir, sources, counters):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from agent_workbook.budget import serialized_result
    from agent_workbook.render import decode

    case = next(case for case in CASES if case["target"] == "ASCII")
    common = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--profile",
        "runtime",
        "--role",
        "reader",
        "--agent-id",
        "semantic-probe",
        "--model-dir",
        str(model_dir),
        "--tokenizer",
        "o200k_base",
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "OPENAI_API_KEY",
            "MASSIVE_API_KEY",
            "HF_TOKEN",
            "HUGGING_FACE_HUB_TOKEN",
        }
    }
    before = source_hashes()
    report = {
        "source_hashes": before,
        "scope": "Two fresh MCP processes: budgeted recall and initialize-only configured-goal restoration. Reuses the already built index; does not rerun index-build or five-question timings.",
    }
    params = StdioServerParameters(command=sys.executable, args=common, env=environment)
    started = time.perf_counter()
    async with (
        stdio_client(params) as (read, write_stream),
        ClientSession(read, write_stream) as client,
    ):
        initialized = await client.initialize()
        listed = await client.list_tools()
        request = {
            "name": "workspool",
            "arguments": {
                "tool": "recall",
                "arguments": {
                    "query": case["query"],
                    "limit": 5,
                    "max_bytes": 32768,
                    "max_tokens": 1200,
                },
            },
        }
        response = await client.call_tool(request["name"], request["arguments"])
        if response.isError:
            raise AssertionError(
                "Budgeted MCP recall returned an error: " + str(response.content)
            )
        decoded = decode(response.content)
        tokens = {
            name: count(serialized_result(response)) for name, count in counters.items()
        }
        if tokens["o200k_base"] > 1200:
            raise AssertionError(
                "Actual serialized MCP result exceeded its token budget"
            )
        report["recall"] = {
            "query": case["query"],
            "target": case["target"],
            "request": request,
            "response": response.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "decoded": decoded,
            "result_tokens": tokens,
            "max_tokens": 1200,
            "exact_citations": exact_citations(decoded, sources),
            "elapsed_seconds_including_server_start": time.perf_counter() - started,
            **ranks(decoded, sources, case["target"]),
        }
        setup = {
            "instructions": initialized.instructions,
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in listed.tools
            ],
        }
        report["setup"] = setup
        report["setup_tokens"] = {
            name: count(compact(setup)) for name, count in counters.items()
        }
    # A genuinely new process, with initialize only: no model/agent tool call
    # asks for context. The configured launch goal supplies the restoration hook.
    params = StdioServerParameters(
        command=sys.executable,
        args=common + ["--goal", case["query"], "--recall-tokens", "1200"],
        env=environment,
    )
    started = time.perf_counter()
    async with (
        stdio_client(params) as (read, write_stream),
        ClientSession(read, write_stream) as client,
    ):
        initialized = await client.initialize()
        _, found, text = (initialized.instructions or "").partition(STARTUP_PREFIX)
        if not found:
            raise AssertionError(
                "Fresh initialization did not restore configured-goal context"
            )
        restored = json.loads(text)
        tokens = {
            name: count(STARTUP_PREFIX + text) for name, count in counters.items()
        }
        if tokens["o200k_base"] > 1200:
            raise AssertionError(
                "Restored context exceeded its configured token budget"
            )
        report["startup"] = {
            "configured_goal": case["query"],
            "target": case["target"],
            "agent_tool_calls": 0,
            "restored": restored,
            "restored_context_tokens_including_prefix": tokens,
            "max_tokens": 1200,
            "exact_citations": exact_citations(restored, sources),
            "elapsed_seconds_including_server_start": time.perf_counter() - started,
            **ranks(restored, sources, case["target"]),
        }
    report["source_files_unchanged"] = source_hashes() == before
    return report


def sanitized(report):
    """Construct a publication allowlist, rather than redact private traces."""
    return {
        "format": 1,
        "method": report["method"],
        "selection": report["selection"],
        "documents": [
            {
                key: item[key]
                for key in ("title", "page_id", "revision_id", "url", "sha256", "bytes")
            }
            for item in report["documents"]
        ],
        "model": {
            key: report["model"][key]
            for key in ("model", "dimension", "manifest_sha256", "key", "engines")
        },
        "index_build": report["index_build"],
        "summary": report["summary"],
        "queries": [
            {
                "query": item["query"],
                "target": item["target"],
                **{
                    mode: {
                        key: item[mode][key]
                        for key in (
                            "target_rank",
                            "returned_titles",
                            "exact_citations",
                            "elapsed_seconds",
                            "mode",
                            "index_complete",
                            "dense_complete",
                        )
                    }
                    for mode in ("lexical", "hybrid")
                },
            }
            for item in report["queries"]
        ],
        "mcp": {
            **{
                key: report["mcp"][key]
                for key in ("source_hashes", "source_files_unchanged", "scope")
                if key in report["mcp"]
            },
            "setup_tokens": report["mcp"]["setup_tokens"],
            "recall": {
                key: report["mcp"]["recall"][key]
                for key in (
                    "query",
                    "target",
                    "result_tokens",
                    "max_tokens",
                    "exact_citations",
                    "elapsed_seconds_including_server_start",
                    "target_rank",
                    "returned_titles",
                )
            },
            "startup": {
                key: report["mcp"]["startup"][key]
                for key in (
                    "configured_goal",
                    "target",
                    "agent_tool_calls",
                    "restored_context_tokens_including_prefix",
                    "max_tokens",
                    "exact_citations",
                    "elapsed_seconds_including_server_start",
                    "target_rank",
                    "returned_titles",
                )
            },
        },
        "source_hashes": report["source_hashes"],
        "source_files_unchanged": report["source_files_unchanged"],
        "model_files_unchanged": report["model_files_unchanged"],
        "corpus_files_unchanged": report["corpus_files_unchanged"],
        "validation_passed": report["validation_passed"],
    }


def run(
    manifest_path,
    model_dir,
    output,
    *,
    count=32,
    max_build_calls=20,
    build_chunks=128,
    build_seconds=60,
    public_json=None,
):
    from agent_workbook.context_index import ranked_search
    from agent_workbook.semantic import hybrid_search, model_spec
    from agent_workbook.store import Workbook

    manifest_path, model_dir, output = (
        Path(manifest_path).resolve(),
        Path(model_dir).resolve(),
        Path(output).resolve(),
    )
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a fresh evaluation output directory")
    output.mkdir(parents=True, exist_ok=True)
    counters, before = load_counters(), source_hashes()
    manifest = json.loads(manifest_path.read_text())
    selected = select_documents(manifest, count)
    book, sources = Workbook(output / "workbook"), {}
    started = time.perf_counter()
    for item in selected:
        path = (manifest_path.parent / item["path"]).resolve()
        if digest(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            raise ValueError("Corpus source differs from its acquisition manifest")
        saved = book.import_path(path, name="wikipedia/" + item["page_id"])
        sources[saved["artifact_id"]] = item | {"path": str(path)}
    spec = model_spec(model_dir)
    report = {
        "method": "Five natural-language queries manually authored after reading the named source articles and before retrieval. Gold is a target article, not answer correctness. Fixed first32 acquisition-manifest articles; source is uncleaned Wikipedia wikitext. Real local quantized BGE ONNX; offline worker blocks socket connects. No API calls, billing claims, rank tuning, or model changes after seeing results.",
        "selection": {
            "rule": "first N acquisition-manifest entries in original order",
            "count": count,
            "available_articles": len(manifest["documents"]),
            "bytes": sum(item["bytes"] for item in selected),
            "selection_bias": "Early manifest/page-ID articles, mostly A titles; not a random or representative benchmark.",
        },
        "documents": selected,
        "model": spec,
        "imports_seconds": time.perf_counter() - started,
        "source_hashes": before,
        "index_build": {
            "calls": [],
            "max_calls": max_build_calls,
            "max_new_unique_chunks_per_call": build_chunks,
            "max_embedding_seconds_per_call": build_seconds,
        },
        "queries": [],
    }
    previous_pending = None
    for number in range(max_build_calls):
        started = time.perf_counter()
        result = hybrid_search(
            book,
            "Reference articles",
            model_dir=model_dir,
            expected_manifest=spec["manifest_sha256"],
            limit=1,
            max_bytes=131072,
            max_embed_chunks=build_chunks,
            max_embed_seconds=build_seconds,
        )
        coverage = result.get("dense_coverage", {})
        report["index_build"]["calls"].append(
            {
                "number": number + 1,
                "elapsed_seconds": time.perf_counter() - started,
                "mode": result["mode"],
                "dense_complete": result["dense_complete"],
                "index_complete": result["index_complete"],
                "coverage": coverage,
                "fallback_reason": result.get("fallback_reason"),
            }
        )
        print(compact(report["index_build"]["calls"][-1]), flush=True)
        if result["dense_complete"]:
            break
        pending = coverage.get("pending_chunks")
        if pending is None or pending == previous_pending:
            break
        previous_pending = pending
    report["index_build"]["complete"] = result["dense_complete"]
    for case in CASES:
        record = dict(case)
        for mode in ("lexical", "hybrid"):
            started = time.perf_counter()
            result = (
                ranked_search(book, case["query"], limit=5, max_bytes=131072)
                if mode == "lexical"
                else hybrid_search(
                    book,
                    case["query"],
                    model_dir=model_dir,
                    expected_manifest=spec["manifest_sha256"],
                    limit=5,
                    max_bytes=131072,
                    max_embed_chunks=0,
                )
            )
            record[mode] = {
                "result": result,
                "elapsed_seconds": time.perf_counter() - started,
                "exact_citations": exact_citations(result, sources),
                "mode": result.get("mode", "lexical"),
                "index_complete": result["index_complete"],
                "dense_complete": result.get("dense_complete"),
                **ranks(result, sources, case["target"]),
            }
        report["queries"].append(record)
        print(
            compact(
                {
                    "query": case["query"],
                    "lexical_rank": record["lexical"]["target_rank"],
                    "hybrid_rank": record["hybrid"]["target_rank"],
                }
            ),
            flush=True,
        )
    report["summary"] = {
        mode: {
            "queries": len(CASES),
            "article_recall_at_1": sum(
                item[mode]["target_rank"] == 1 for item in report["queries"]
            )
            / len(CASES),
            "article_recall_at_5": sum(
                item[mode]["target_rank"] is not None for item in report["queries"]
            )
            / len(CASES),
            "exact_citations_checked": sum(
                item[mode]["exact_citations"] for item in report["queries"]
            ),
        }
        for mode in ("lexical", "hybrid")
    }
    report["mcp"] = asyncio.run(mcp_probe(book.root, model_dir, sources, counters))
    report["source_files_unchanged"] = source_hashes() == before
    report["model_files_unchanged"] = (
        model_spec(model_dir, spec["manifest_sha256"]) == spec
    )
    report["corpus_files_unchanged"] = all(
        digest(item["path"]) == item["sha256"] for item in sources.values()
    )
    # Retrieval misses remain valid observations: validation must never be
    # conditioned on the proposed feature outperforming its lexical control.
    report["validation_passed"] = (
        report["source_files_unchanged"]
        and report["model_files_unchanged"]
        and report["corpus_files_unchanged"]
    )
    write(output / "results.json", report)
    if public_json is not None:
        Path(public_json).parent.mkdir(parents=True, exist_ok=True)
        write(public_json, sanitized(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-json", type=Path)
    parser.add_argument("--count", type=int, default=32)
    parser.add_argument("--build-chunks", type=int, default=128)
    parser.add_argument("--build-seconds", type=float, default=60)
    parser.add_argument("--max-build-calls", type=int, default=20)
    args = parser.parse_args()
    result = run(
        args.manifest,
        args.model_dir,
        args.output,
        count=args.count,
        build_chunks=args.build_chunks,
        build_seconds=args.build_seconds,
        max_build_calls=args.max_build_calls,
        public_json=args.public_json,
    )
    print(
        compact(
            {
                "validation_passed": result["validation_passed"],
                "summary": result["summary"],
            }
        ),
        flush=True,
    )
    raise SystemExit(0 if result["validation_passed"] else 1)


if __name__ == "__main__":
    main()
