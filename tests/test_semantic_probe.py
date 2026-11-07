"""Probe integrity and publication safety; no mocked semantic-quality claims."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "semantic_probe", Path(__file__).parents[1] / "benchmarks/semantic_probe.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def test_selection_is_fixed_manifest_order_and_gold_is_predeclared():
    documents = [{"title": case["target"]} for case in bench.CASES] + [
        {"title": f"Distractor {number}"} for number in range(50)
    ]
    assert bench.select_documents({"documents": documents}, 32) == documents[:32]
    missing = [{"title": f"Unrelated {number}"} for number in range(32)]
    with pytest.raises(ValueError, match="predeclared targets"):
        bench.select_documents({"documents": missing}, 32)
    assert len(bench.CASES) == 5
    assert all(
        case["target"].casefold() not in case["query"].casefold()
        for case in bench.CASES
    )


def test_citations_check_original_utf8_bytes_not_returned_metadata(tmp_path):
    path = tmp_path / "article.txt"
    raw = "before 😀 after".encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    sources = {"wb_id": {"path": str(path), "sha256": digest}}
    result = {
        "hits": [
            {
                "artifact_id": "wb_id",
                "start_byte": 7,
                "end_byte": 11,
                "excerpt": "😀",
                "sha256": digest,
            }
        ]
    }
    assert bench.exact_citations(result, sources) == 1
    result["hits"][0]["excerpt"] = "wrong"
    with pytest.raises(AssertionError, match="independently read"):
        bench.exact_citations(result, sources)


def test_article_recall_is_ranked_hit_position_and_keeps_misses():
    sources = {"a": {"title": "Other"}, "b": {"title": "Target"}}
    result = {
        "hits": [{"artifact_id": "a"}, {"artifact_id": "a"}, {"artifact_id": "b"}]
    }
    assert bench.ranks(result, sources, "Target")["target_rank"] == 3
    assert bench.ranks(result, sources, "Absent")["target_rank"] is None


def test_public_json_allowlist_excludes_raw_passages_and_private_paths():
    secret = "PRIVATE_PATH_OR_EXCERPT_SENTINEL"
    row = {
        "target_rank": 1,
        "returned_titles": ["Article"],
        "exact_citations": 1,
        "elapsed_seconds": 0.1,
        "mode": "hybrid",
        "index_complete": True,
        "dense_complete": True,
        "result": {"excerpt": secret},
    }
    query = {"query": "Question", "target": "Article", "lexical": row, "hybrid": row}
    recall = {
        "query": "Question",
        "target": "Article",
        "result_tokens": {"o200k_base": 800},
        "max_tokens": 1200,
        "exact_citations": 1,
        "elapsed_seconds_including_server_start": 1,
        "target_rank": 1,
        "returned_titles": ["Article"],
        "decoded": {"excerpt": secret},
    }
    startup = {
        "configured_goal": "Question",
        "target": "Article",
        "agent_tool_calls": 0,
        "restored_context_tokens_including_prefix": {"o200k_base": 900},
        "max_tokens": 1200,
        "exact_citations": 1,
        "elapsed_seconds_including_server_start": 1,
        "target_rank": 1,
        "returned_titles": ["Article"],
        "restored": {"excerpt": secret},
    }
    report = {
        "method": "Controlled comparison",
        "selection": {"count": 32},
        "documents": [
            {
                "title": "Article",
                "page_id": "1",
                "revision_id": "2",
                "url": "https://en.wikipedia.org/w/index.php?oldid=2",
                "sha256": "a" * 64,
                "bytes": 10,
                "path": secret,
            }
        ],
        "model": {
            "model": "BGE",
            "dimension": 384,
            "manifest_sha256": "b" * 64,
            "key": "c" * 64,
            "engines": {},
            "directory": secret,
        },
        "index_build": {},
        "summary": {},
        "queries": [query],
        "mcp": {
            "setup_tokens": {},
            "recall": recall,
            "startup": startup,
            "setup": {"instructions": secret},
        },
        "source_hashes": {},
        "source_files_unchanged": True,
        "model_files_unchanged": True,
        "corpus_files_unchanged": True,
        "validation_passed": True,
    }
    encoded = json.dumps(bench.sanitized(report))
    assert (
        secret not in encoded
        and "excerpt" not in encoded
        and '"directory"' not in encoded
    )
