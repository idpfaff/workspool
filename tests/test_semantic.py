"""Offline contracts with declared fake embeddings; not semantic-quality tests."""

import hashlib
import json
import sqlite3
import subprocess

import pytest

from agent_workbook import semantic
from agent_workbook.store import Workbook

TEST_MODEL = {"key": "a" * 64, "model": "test-only-vector-fixture", "dimension": 3}


def document(book, name, body, expected_version=None):
    return book.save_text(
        body,
        name=name,
        kind="document",
        media_type="text/plain",
        expected_version=expected_version,
    )


class FixtureEmbedder:
    """Explicit vector answers allow fusion/cache tests without learned assets."""

    def __init__(self):
        self.batches = []

    def __call__(self, spec, query, documents, seconds):
        self.batches.append(list(documents))
        return {
            "query": [1, 0, 0],
            "documents": [
                [1, 0, 0] if "pension" in text else [0, 1, 0] for text in documents
            ],
        }


def search(book, query="retirement", **kwargs):
    return semantic.hybrid_search(
        book,
        query,
        _model_spec=TEST_MODEL,
        _embedder=kwargs.pop("embedder", FixtureEmbedder()),
        **kwargs,
    )


def test_dense_reads_zero_lexical_overlap_and_exact_utf8_citations(tmp_path):
    book = Workbook(tmp_path)
    wanted = document(book, "benefits", "Staff receive a pension after service. 😀\n")
    document(book, "other", "Cafeteria hours are posted every morning.\n")
    result = search(book)
    assert result["mode"] == "hybrid" and result["dense_complete"]
    assert result["hits"][0]["artifact_id"] == wanted["artifact_id"]
    assert "lexical_rank" not in result["hits"][0]
    assert result["hits"][0]["dense_rank"] == 1
    hit = result["hits"][0]
    raw = (book.blobs / book.inspect(wanted["artifact_id"])["sha256"]).read_bytes()
    assert hit["excerpt"].encode() == raw[hit["start_byte"] : hit["end_byte"]]


def test_rrf_uses_both_rankers_and_deterministic_ties(tmp_path):
    book = Workbook(tmp_path)
    document(book, "a", "retirement pension policy")
    document(book, "b", "retirement pension policy")
    document(book, "c", "pension policy")
    result = search(book, limit=2)
    assert [hit["name"] for hit in result["hits"]] == ["a", "b"]
    assert all("lexical_rank" in hit and "dense_rank" in hit for hit in result["hits"])
    assert result["truncated"] and not result["complete"]
    for hit in result["hits"]:
        assert hit["score"] == pytest.approx(
            1 / (60 + hit["lexical_rank"]) + 1 / (60 + hit["dense_rank"])
        )


def test_cross_session_content_reuse_and_changed_model_revision(tmp_path):
    book, embedder = Workbook(tmp_path), FixtureEmbedder()
    document(book, "a", "pension policy")
    document(book, "b", "pension policy")
    assert search(book, embedder=embedder)["dense_complete"]
    assert len(embedder.batches[0]) == 1  # Exact bytes reuse across source identities.
    assert search(Workbook(tmp_path), embedder=embedder)["dense_complete"]
    assert embedder.batches[1] == []
    changed = TEST_MODEL | {"key": "b" * 64}
    semantic.hybrid_search(book, "retirement", _model_spec=changed, _embedder=embedder)
    assert len(embedder.batches[2]) == 1


def test_build_scan_budgets_disclose_partial_coverage_and_progress(tmp_path):
    book = Workbook(tmp_path)
    for number in range(3):
        document(book, f"source{number}", f"pension rule number {number}")
    first = search(book, max_embed_chunks=1)
    assert (
        first["dense_coverage"]["pending_chunks"] == 2 and not first["dense_complete"]
    )
    second = search(book, max_embed_chunks=1)
    assert second["dense_coverage"]["pending_chunks"] == 1
    third = search(book, max_embed_chunks=1)
    assert third["dense_complete"]
    bounded = search(book, max_search_vectors=1)
    assert (
        not bounded["dense_complete"] and not bounded["dense_coverage"]["scan_complete"]
    )


def test_names_follow_current_but_pinned_history_remains_explicit(tmp_path):
    book = Workbook(tmp_path)
    old = document(book, "policy", "pension policy old")
    search(book)
    new = document(book, "policy", "pension policy revised", expected_version=1)
    current = search(book)
    assert {hit["artifact_id"] for hit in current["hits"]} == {new["artifact_id"]}
    historical = search(book, refs=[old["artifact_id"]])
    assert historical["hits"][0]["artifact_id"] == old["artifact_id"]
    assert not historical["hits"][0]["is_latest"]


def test_missing_model_is_truthful_lexical_fallback_and_never_embeds(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    document(book, "policy", "retirement pension policy")
    monkeypatch.setattr(
        semantic, "_embed", lambda *args: pytest.fail("unexpected embed call")
    )
    result = semantic.hybrid_search(book, "retirement")
    assert result["mode"] == "lexical" and result["hits"]
    assert result["fallback_reason"] == "model_not_configured"
    assert result["lexical_complete"] and not result["complete"]
    absent = semantic.hybrid_search(book, "retirement", model_dir=tmp_path / "missing")
    assert absent["mode"] == "lexical" and "local_model" in absent["fallback_reason"]


def test_worker_timeout_and_invalid_vectors_do_not_claim_hybrid(tmp_path):
    book = Workbook(tmp_path)
    document(book, "policy", "retirement pension policy")

    def timeout(*args):
        raise TimeoutError("fixture timeout")

    result = search(book, embedder=timeout)
    assert (
        result["mode"] == "lexical"
        and result["fallback_reason"] == "embedding_time_budget"
    )
    assert result["hits"] and not result["dense_complete"]

    def invalid(*args):
        return {"query": [1, 0, 0], "documents": [[float("nan"), 0, 0]]}

    result = search(book, embedder=invalid)
    assert (
        result["mode"] == "lexical"
        and result["fallback_reason"] == "embedding_or_cache_failed"
    )


def test_corrupt_vectors_are_not_ranked_and_are_rebuilt_next_call(tmp_path):
    book = Workbook(tmp_path)
    document(book, "policy", "pension policy")
    search(book)
    with sqlite3.connect(tmp_path / "semantic-vectors.sqlite3") as db:
        db.execute("UPDATE vectors SET data=?", (b"bad",))
    invalid = search(book)
    assert not invalid["dense_complete"] and invalid["invalidated_vectors"] == 1
    assert not invalid["hits"]
    repaired = search(book)
    assert repaired["dense_complete"] and repaired["hits"]


def test_source_tampering_fails_instead_of_citing_modified_bytes(tmp_path):
    book = Workbook(tmp_path)
    saved = document(book, "policy", "pension policy")
    search(book)
    path = book.blobs / book.inspect(saved["artifact_id"])["sha256"]
    path.write_text("different bytes")
    with pytest.raises(ValueError, match="outside the workbook"):
        search(book)


def test_render_budget_drops_whole_hits_and_marks_omission(tmp_path):
    book = Workbook(tmp_path)
    document(book, "a", "pension policy a")
    document(book, "b", "pension policy b")
    result = search(book, fits=lambda value: len(value["hits"]) <= 1)
    assert len(result["hits"]) == 1 and result["truncated"] and not result["complete"]
    with pytest.raises(ValueError, match="coverage metadata"):
        search(book, fits=lambda value: False)


def test_model_manifest_verifies_regular_files_and_revision(tmp_path, monkeypatch):
    assets = tmp_path / "model"
    assets.mkdir()
    data = b"test fixture, not executable ONNX weights"
    weights = assets / "model_optimized.onnx"
    weights.write_bytes(data)
    manifest = {
        "format": 1,
        "model": semantic.MODEL,
        "dimension": 384,
        "files": {
            weights.name: {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        },
    }
    (assets / semantic.MANIFEST).write_text(json.dumps(manifest))
    monkeypatch.setattr(semantic.importlib.metadata, "version", lambda _: "test-engine")
    spec = semantic.model_spec(assets)
    assert spec["dimension"] == 384
    assert semantic.model_spec(assets, spec["manifest_sha256"])["key"] == spec["key"]
    with pytest.raises(ValueError, match="configured revision"):
        semantic.model_spec(assets, "wrong")
    weights.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="hash or size"):
        semantic.model_spec(assets)
    weights.unlink()
    weights.symlink_to(assets / semantic.MANIFEST)
    with pytest.raises(ValueError, match="symbolic"):
        semantic.model_spec(assets)


def test_default_worker_has_hard_timeout_and_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("HF_TOKEN", "test-secret")

    def run(command, **kwargs):
        assert kwargs["timeout"] == 0.01
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert "OPENAI_API_KEY" not in kwargs["env"] and "HF_TOKEN" not in kwargs["env"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(semantic.subprocess, "run", run)
    with pytest.raises(TimeoutError, match="time budget"):
        semantic._embed(TEST_MODEL, "query", [], 0.01)


def test_sqlite_vector_disk_cap_precedes_oversized_batch(tmp_path):
    book = Workbook(tmp_path)
    spec = TEST_MODEL | {"dimension": 384}
    documents = [
        {"digest": hashlib.sha256(str(index).encode()).hexdigest()}
        for index in range(512)
    ]
    vectors = [[1.0] + [0.0] * 383] * len(documents)
    with semantic._vectors(book, 1024**2) as (db, path):
        with pytest.raises(ValueError, match="capacity"):
            semantic._store_vectors(db, spec, documents, vectors, 1024**2)
        assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
        assert path.stat().st_size <= 1024**2
