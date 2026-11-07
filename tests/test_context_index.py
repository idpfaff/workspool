"""Persistent ranked context remains exact, versioned, bounded, and disposable."""

import hashlib
import io
import os
import sqlite3
from itertools import pairwise

import pytest

from agent_workbook import context_index as index
from agent_workbook.store import Workbook


def note(workbook, name, text, **kwargs):
    return workbook.save_text(
        text, name=name, kind="document", media_type="text/plain", **kwargs
    )


def cache_rows(workbook, table):
    with sqlite3.connect(workbook.root / "context-index.sqlite3") as db:
        return db.execute(f"SELECT * FROM {table}").fetchall()


def exact_hits(result, sources):
    for hit in result["hits"]:
        raw = sources[hit["artifact_id"]]
        assert hit["excerpt"].encode() == raw[hit["start_byte"] : hit["end_byte"]]
        assert hit["sha256"] == hashlib.sha256(raw).hexdigest()
        assert hit["chunk_id"].startswith("wc_")


def test_ranked_multi_term_evidence_and_persistent_incremental_versions(
    tmp_path, monkeypatch
):
    workbook = Workbook(tmp_path)
    note(workbook, "single", "red ordinary words")
    both = note(workbook, "both", "red blue ordinary words")
    first = index.ranked_search(workbook, "red blue")
    assert first["complete"] and first["indexed_versions"] == 2
    assert first["hits"][0]["artifact_id"] == both["artifact_id"]
    assert first["hits"][0]["score"] < first["hits"][1]["score"]
    expected_ids = [hit["chunk_id"] for hit in first["hits"]]
    original = index._index_version

    def forbid_rebuild(*args):
        raise AssertionError("unchanged version was indexed again")

    monkeypatch.setattr(index, "_index_version", forbid_rebuild)
    warm = index.ranked_search(Workbook(tmp_path), "red blue")
    assert warm["indexed_versions"] == warm["indexed_bytes"] == 0
    assert [hit["chunk_id"] for hit in warm["hits"]] == expected_ids
    monkeypatch.setattr(index, "_index_version", original)
    updated = note(workbook, "both", "green current evidence", expected_version=1)
    latest = index.ranked_search(workbook, "green")
    assert latest["indexed_versions"] == 1
    assert [hit["artifact_id"] for hit in latest["hits"]] == [updated["artifact_id"]]
    historical = index.ranked_search(workbook, "blue", refs=[both["artifact_id"]])
    assert historical["hits"][0]["is_latest"] is False
    assert historical["hits"][0]["chunk_id"] == expected_ids[0]


def test_full_source_beyond_old_scan_limit_and_exact_utf8_offsets(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    path = tmp_path / "large.txt"
    prefix = b"ordinary source material\n" * 710000
    assert len(prefix) > 16 * 1024 * 1024
    raw = prefix + "Lateunique café Ελληνικά\r\nfinal\rline\x00".encode()
    path.write_bytes(raw)
    saved = workbook.import_path(path, name="large")
    result = index.ranked_search(workbook, "Lateunique café", refs=["large"])
    assert result["index_complete"] and result["indexed_bytes"] == len(raw)
    assert any("Lateunique" in hit["excerpt"] for hit in result["hits"])
    assert all(hit["start_byte"] > 16 * 1024 * 1024 for hit in result["hits"])
    exact_hits(result, {saved["artifact_id"]: raw})


def test_unicode_chunk_boundaries_mixed_newlines_and_chunk_ids_rebuild(tmp_path):
    workbook = Workbook(tmp_path)
    text = ("😀αβ café boundaryword\r\n" * 500) + "tail\r終\x00"
    saved = note(workbook, "unicode", text)
    first = index.ranked_search(
        workbook, "café boundaryword", limit=64, max_bytes=262144
    )
    exact_hits(first, {saved["artifact_id"]: text.encode()})
    assert first["complete"]
    chunks = cache_rows(workbook, "chunks")
    intervals = sorted((row[3], row[4]) for row in chunks)
    assert intervals[0][0] == 0 and intervals[-1][1] == len(text.encode())
    assert all(right[0] < left[1] for left, right in pairwise(intervals))
    ids = [hit["chunk_id"] for hit in first["hits"]]
    with sqlite3.connect(workbook.root / "context-index.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("DELETE FROM versions")
    again = index.ranked_search(
        workbook, "café boundaryword", limit=64, max_bytes=262144
    )
    assert [hit["chunk_id"] for hit in again["hits"]] == ids


def test_build_budgets_resume_at_verified_version_boundaries(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    for name in ("a", "b", "c"):
        note(workbook, name, "evidence " + name * 20)
    first = index.ranked_search(workbook, "evidence", max_index_bytes=30)
    assert first["indexed_versions"] == 1
    assert not first["index_complete"] and not first["complete"]
    assert first["pending_versions"] == 2
    assert all(item["reason"] == "index_byte_budget" for item in first["pending"])
    second = index.ranked_search(workbook, "evidence", max_index_bytes=30)
    assert second["indexed_versions"] == 1 and second["pending_versions"] == 1
    final = index.ranked_search(workbook, "evidence", max_index_bytes=30)
    assert final["complete"] and len(final["hits"]) == 3
    note(workbook, "large", "evidence " * 100)
    pending = index.ranked_search(
        workbook, "evidence", refs=["large"], max_index_bytes=30
    )
    assert pending["hits"] == [] and not pending["complete"]
    assert index.ranked_search(workbook, "evidence", refs=["large"])["complete"]
    monkeypatch.setattr(index, "MAX_INDEX_VERSIONS", 1)
    note(workbook, "d", "evidence")
    note(workbook, "e", "evidence")
    limited = index.ranked_search(workbook, "evidence")
    assert limited["pending"][0]["reason"] == "index_version_budget"


def test_interrupted_build_rolls_back_partial_chunks(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "evidence " * 500)
    original = index._chunks

    def interrupted(*args):
        yield next(original(*args))
        raise index._BuildLimit

    monkeypatch.setattr(index, "_chunks", interrupted)
    failed = index.ranked_search(workbook, "evidence")
    assert not failed["complete"] and failed["hits"] == []
    assert failed["pending"][0]["reason"] == "index_time_budget"
    assert cache_rows(workbook, "versions") == cache_rows(workbook, "chunks") == []
    monkeypatch.setattr(index, "_chunks", original)
    assert index.ranked_search(workbook, "evidence")["hits"]


def test_corruption_same_size_restored_mtime_and_malformed_utf8_fail(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "evidence original")
    index.ranked_search(workbook, "evidence")
    digest = workbook.inspect("source")["sha256"]
    path = workbook.blobs / digest
    stamp = path.stat()
    path.write_bytes(b"evidence tampered")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match="changed outside"):
        index.ranked_search(workbook, "absent")
    path.write_bytes(b"evidence original")
    assert index.ranked_search(workbook, "evidence")["hits"]
    workbook._save(
        io.BytesIO(b"valid prefix\n" * 500 + b"\xff"),
        name="malformed",
        kind="document",
        media_type="text/plain",
    )
    with pytest.raises(ValueError, match="UTF-8"):
        index.ranked_search(workbook, "valid", refs=["malformed"])
    assert len(cache_rows(workbook, "versions")) == 1


def test_cache_tampering_source_deletion_and_symlinks_never_supply_evidence(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    note(workbook, "source", "evidence original")
    index.ranked_search(workbook, "evidence")
    with sqlite3.connect(workbook.root / "context-index.sqlite3") as db:
        db.execute("UPDATE chunks SET body='evidence invented'")
    with pytest.raises(ValueError, match="exact source bytes"):
        index.ranked_search(workbook, "evidence")
    workbook.delete("source", expected_version=1)
    empty = index.ranked_search(workbook, "evidence")
    assert empty["complete"] and not empty["hits"]
    assert not cache_rows(workbook, "versions")
    note(workbook, "source", "evidence new")
    path = workbook.blobs / workbook.inspect("source")["sha256"]
    target = tmp_path / "target"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        index.ranked_search(workbook, "evidence")
    path.unlink()
    with pytest.raises(ValueError, match="deleted"):
        index.ranked_search(workbook, "evidence")


def test_one_catalog_snapshot_for_identity_and_freshness(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    source = note(workbook, "source", "source first")
    derived = note(
        workbook, "derived", "evidence first", dependencies=[source["artifact_id"]]
    )
    original = index._prepare

    def update_after_snapshot(*args):
        result = original(*args)
        note(workbook, "source", "source second", expected_version=1)
        note(workbook, "derived", "evidence second", expected_version=1)
        return result

    monkeypatch.setattr(index, "_prepare", update_after_snapshot)
    result = index.ranked_search(workbook, "evidence", refs=["derived"])
    hit = result["hits"][0]
    assert hit["artifact_id"] == derived["artifact_id"]
    assert hit["excerpt"] == "evidence first"
    assert hit["is_latest"] and not hit["stale_inputs"]
    monkeypatch.setattr(index, "_prepare", original)
    historical = index.ranked_search(
        workbook, "evidence", refs=[derived["artifact_id"]]
    )
    assert not historical["hits"][0]["is_latest"]
    assert historical["hits"][0]["stale_inputs"]


def test_output_limits_are_explicit_and_whole_reply_bounded(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "evidence ordinary words\n" * 500)
    limited = index.ranked_search(workbook, "evidence", limit=1)
    assert (
        limited["index_complete"] and not limited["complete"] and limited["truncated"]
    )
    budgeted = index.ranked_search(workbook, "evidence", limit=64, max_bytes=3000)
    assert len(index._json(budgeted).encode()) <= 3000
    assert budgeted["hits"] and budgeted["truncated"]
    with pytest.raises(ValueError, match="one exact chunk"):
        index.ranked_search(workbook, "evidence", max_bytes=500)
    with pytest.raises(ValueError, match="metadata"):
        index.ranked_search(workbook, "evidence", max_bytes=5)


def test_empty_binary_unknown_duplicate_and_literal_query_validation(tmp_path):
    workbook = Workbook(tmp_path)
    assert index.ranked_search(workbook, "evidence")["complete"]
    note(workbook, "empty", "")
    saved = note(workbook, "source", "evidence NEAR AND OR")
    workbook._save(
        io.BytesIO(b"bytes"),
        name="binary",
        kind="file",
        media_type="application/octet-stream",
    )
    assert index.ranked_search(workbook, '"evidence" OR (NEAR*)')["hits"]
    with pytest.raises(ValueError, match="not searchable"):
        index.ranked_search(workbook, "evidence", refs=["binary"])
    with pytest.raises(ValueError, match="Unknown"):
        index.ranked_search(workbook, "evidence", refs=["missing"])
    with pytest.raises(ValueError, match="distinct versions"):
        index.ranked_search(workbook, "evidence", refs=["source", saved["artifact_id"]])
    for refs in ([], [True], ["source"] * 2, "source", ["x"] * 17):
        with pytest.raises(ValueError, match="references"):
            index.ranked_search(workbook, "evidence", refs=refs)
    for query in (None, "", "***", "q" * 1001, " ".join(f"term{i}" for i in range(33))):
        with pytest.raises(ValueError, match="query"):
            index.ranked_search(workbook, query)
    for argument in ("limit", "max_bytes", "max_index_bytes"):
        for value in (True, 0, -1):
            with pytest.raises(ValueError):
                index.ranked_search(workbook, "evidence", **{argument: value})


def test_stream_chunks_use_bounded_reads_and_cover_all_bytes():
    raw = ("😀evidence\r\n" * 10000).encode()

    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= index.READ_BYTES
            return super().read(size)

    digest = hashlib.sha256()
    chunks = list(index._chunks(Bounded(raw), digest, float("inf")))
    assert digest.hexdigest() == hashlib.sha256(raw).hexdigest()
    assert chunks[0][0] == 0 and chunks[-1][0] + len(chunks[-1][1]) == len(raw)
    for start, chunk in chunks:
        assert chunk == raw[start : start + len(chunk)]
        assert chunk.decode().encode() == chunk
        assert len(chunk) <= index.CHUNK_BYTES


def test_cache_cleanup_is_bounded_and_rotates_over_deleted_history(
    tmp_path, monkeypatch
):
    workbook = Workbook(tmp_path)
    for position in range(7):
        note(workbook, f"source{position}", "evidence")
    index.ranked_search(workbook, "evidence")
    for position in range(7):
        workbook.delete(f"source{position}", expected_version=1)
    monkeypatch.setattr(index, "MAX_CLEANUP_VERSIONS", 2)
    for expected in (5, 3, 1, 0):
        result = index.ranked_search(workbook, "evidence")
        assert result["complete"] and not result["hits"]
        assert len(cache_rows(workbook, "versions")) == expected
