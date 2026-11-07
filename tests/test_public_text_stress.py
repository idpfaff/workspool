"""Small offline fixtures validate the real-corpus harness, not its performance."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from agent_workbook import context_index

SPEC = importlib.util.spec_from_file_location(
    "public_text_stress", Path(__file__).parents[1] / "benchmarks/public_text_stress.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def fixture(directory):
    directory.mkdir()
    records = []
    for position, subject in enumerate(
        ("astronomy", "hydrology", "phonology", "geology")
    ):
        ordinary = "Ordinary background context describes historical methods and public observations. 😀\n"
        anchor = f"{subject} calibration{position} methodology{position} uncertainty{position} instruments{position} reproducibility{position} observations{position}.\n"
        raw = (ordinary * 900 + anchor + ordinary * 900).encode()
        path = directory / f"page-{position}.txt"
        path.write_bytes(raw)
        records.append(
            {
                "path": path.name,
                "title": subject,
                "page_id": position,
                "revision_id": position + 10,
                "source_url": f"https://example.invalid/?oldid={position + 10}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    (directory / "manifest.json").write_text(
        json.dumps(
            {"documents": records, "source_archive": {"kind": "test-only fixture"}}
        )
    )
    return directory


def test_manifest_utf8_hash_size_and_path_validation(tmp_path):
    directory = fixture(tmp_path / "docs")
    rows, metadata = bench.corpus(directory, minimum_bytes=0)
    assert len(rows) == 4 and metadata["source_archive"]["kind"] == "test-only fixture"
    with pytest.raises(ValueError, match="minimum"):
        bench.corpus(directory)
    first = directory / "page-0.txt"
    first.write_bytes(first.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="mismatch"):
        bench.corpus(directory, minimum_bytes=0)
    first.write_bytes(b"bad utf8\xff")
    with pytest.raises(UnicodeDecodeError):
        bench.corpus(directory, minimum_bytes=0)
    first.unlink()
    first.symlink_to(directory / "page-1.txt")
    with pytest.raises(ValueError, match="regular files"):
        bench.corpus(directory, minimum_bytes=0)


def test_queries_come_from_exact_late_source_passages(tmp_path):
    rows, _ = bench.corpus(fixture(tmp_path / "docs"), minimum_bytes=0)
    queries = bench.derive_queries(rows, 4)
    assert len(queries) == 4
    by_name = {row["name"]: row for row in rows}
    for query in queries:
        source = Path(by_name[query["name"]]["path"]).read_bytes()
        assert query["beyond_64kib"]
        assert (
            source[query["start_byte"] : query["end_byte"]].decode() == query["literal"]
        )
        assert all(
            term in query["literal"].casefold() for term in query["query"].split()
        )
        assert all(count >= 1 for count in query["term_document_counts"].values())


def test_direct_index_is_persistent_incremental_and_exact(tmp_path):
    rows, _ = bench.corpus(fixture(tmp_path / "docs"), minimum_bytes=0)
    direct = bench.DirectIndex(tmp_path / "direct.sqlite3")
    assert direct.refresh(rows, max_versions=2)["pending_versions"] == 2
    assert direct.refresh(rows, max_versions=2)["index_complete"]
    assert bench.DirectIndex(direct.path).refresh(rows)["indexed_versions"] == 0
    query = bench.derive_queries(rows, 1)[0]
    hits = direct.search(query["query"])
    assert bench.verify_hits(hits, {row["name"]: row for row in rows})
    assert bench.target_found(hits, query)
    hits[0]["excerpt"] += "fabricated"
    with pytest.raises(AssertionError, match="Citation bytes"):
        bench.verify_hits(hits, {row["name"]: row for row in rows})


def test_full_smoke_coverage_reopen_update_task_reset_and_retained_receipt(
    tmp_path, monkeypatch
):
    import tiktoken

    encoding = tiktoken.Encoding(
        name="test-only-byte-encoding",
        pat_str=r"(?s:.)",
        mergeable_ranks={bytes([i]): i for i in range(256)},
        special_tokens={},
    )
    counter = lambda text: len(encoding.encode(text))
    monkeypatch.setattr(context_index, "MAX_INDEX_VERSIONS", 2)
    directory = fixture(tmp_path / "docs")
    report = bench.run_suite(
        directory,
        tmp_path / "output",
        minimum_bytes=0,
        counter=counter,
        query_count=4,
        bundle_probe=True,
        tokenizer="test-only-byte-encoding",
    )
    assert report["success"]
    assert report["document_count"] == 4
    assert (
        report["direct_cold_coverage_complete"]
        and report["workspool_cold_coverage_complete"]
    )
    assert (
        len([event for event in report["events"] if event["phase"] == "workspool_cold"])
        == 2
    )
    assert report["changed_source"]["workspool_indexed_versions"] == 1
    assert report["changed_source"]["direct_indexed_versions"] == 1
    assert report["changed_source"]["historical_target_found"]
    assert report["task_context"]["retained_delivery"] == "unchanged"
    assert report["task_context"]["reset_citations_exact"]
    assert report["bundle_probe"]["derived_not_original_document"]
    assert all(query["exact_citations"] for query in report["query_results"])
    assert all(
        query["literal_pinned"]["target_found"]
        for query in report["query_results"]
        if query["phase"] == "warm"
    )
    assert all(
        item["tokens"] <= item["budget"]
        for item in report["task_context"]["budget_checks"]
        if item["status"] == "ok"
    )
    with pytest.raises(ValueError, match="fresh output"):
        bench.run_suite(
            directory, tmp_path / "output", minimum_bytes=0, counter=counter
        )


def test_chunk_control_hashes_all_bytes_and_fails_on_corruption(tmp_path):
    path = tmp_path / "source.txt"
    raw = ("😀 café\r\n" * 400).encode()
    path.write_bytes(raw)
    chunks = list(bench.chunks(path, hashlib.sha256(raw).hexdigest()))
    assert chunks[0][0] == 0
    assert chunks[-1][0] + len(chunks[-1][1].encode()) == len(raw)
    for start, text in chunks:
        assert raw[start : start + len(text.encode())] == text.encode()
    with pytest.raises(ValueError, match="digest changed"):
        list(bench.chunks(path, "0" * 64))


def test_candidate_words_across_reads_and_overlong_words_are_not_split(tmp_path):
    path = tmp_path / "boundary.txt"
    path.write_text(
        " " * 65530 + "uncommonword " + "x" * 100000 + "hiddenterm ordinaryterm"
    )
    assert bench.document_terms(
        path, {"uncommonword", "hiddenterm", "ordinaryterm"}
    ) == {"uncommonword", "ordinaryterm"}
