"""A continuity ledger must never become recall evidence, even after upgrade."""

import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agent_workbook import context_index, semantic
from agent_workbook.continuity import capture_tool, start_session
from agent_workbook.coordination import Principal
from agent_workbook.memory_projection import project_memory_fact
from agent_workbook.store import Workbook

MODEL = {"key": "a" * 64, "model": "offline-contract-fixture", "dimension": 3}


def embed(spec, query, documents, seconds):
    return {"query": [1, 0, 0], "documents": [[1, 0, 0] for _ in documents]}


def save(book, name, body, kind="document"):
    return book.save_text(body, name=name, kind=kind, media_type="text/plain")


@pytest.mark.parametrize("query", ["demo", "", "___"])
def test_find_excludes_episode_ledgers_before_limiting_results(tmp_path, query):
    book = Workbook(tmp_path)
    document = save(book, "demo-source", "demo useful evidence")
    for index in range(12):
        save(book, f"_episodes/demo-{index}", "demo " * 20, "memory_episode")
    result = book.find(query, limit=1)
    assert [row["artifact_id"] for row in result] == [document["artifact_id"]]


def seed_legacy_index(book, ids):
    """Simulate v0.8's accepted episodes without restoring its retrieval policy."""
    with book.connect() as catalog, context_index._cache(book) as cache:
        for ref in ids:
            context_index._index_version(
                cache, book, book._get(catalog, ref), time.monotonic() + 5
            )
        rows = cache.execute("SELECT digest,body FROM chunks").fetchall()
    with semantic._vectors(book, 1024**2) as (db, _):
        semantic._store_vectors(
            db, MODEL, [dict(row) for row in rows], [[1, 0, 0] for _ in rows], 1024**2
        )


@pytest.mark.parametrize("dense", [False, True])
def test_default_and_explicit_scopes_exclude_preindexed_episode_ledgers(
    tmp_path, dense
):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "Hosting spend: compute 1200 USD; storage 300 USD.")
    ledger_body = json.dumps(
        {
            "goal": "review hosting spend",
            "accounting_note": "not evidence",
            "facts": {"status": "ok"},
        }
    )
    ledger = save(book, "_episodes/legacy", ledger_body, "memory_episode")
    seed_legacy_index(book, [source["artifact_id"], ledger["artifact_id"]])
    search = (
        (
            lambda refs=None: semantic.hybrid_search(
                book, "review hosting spend", refs, _model_spec=MODEL, _embedder=embed
            )
        )
        if dense
        else (
            lambda refs=None: context_index.ranked_search(
                book, "review hosting spend", refs
            )
        )
    )
    result = search()
    assert [hit["artifact_id"] for hit in result["hits"]] == [source["artifact_id"]]
    assert search([ledger["artifact_id"]])["hits"] == []
    with sqlite3.connect(book.root / "context-index.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM chunks WHERE artifact_id=?",
                (ledger["artifact_id"],),
            ).fetchone()[0]
            == 0
        )
    with sqlite3.connect(book.root / "semantic-vectors.sqlite3") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM vectors WHERE content_sha=?",
                (hashlib.sha256(ledger_body.encode()).hexdigest(),),
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1


def test_vector_hash_shared_with_legitimate_document_is_preserved(tmp_path):
    book = Workbook(tmp_path)
    body = "Hosting spend is 1500 USD."
    real = save(book, "source", body)
    ledger = save(book, "_episodes/copied", body, "memory_episode")
    seed_legacy_index(book, [real["artifact_id"], ledger["artifact_id"]])
    counts = context_index.clean_recall_cache(book)
    assert counts["versions_removed"] == 1 and counts["chunks_removed"] == 1
    assert counts["vectors_removed"] == 0
    with sqlite3.connect(book.root / "semantic-vectors.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1


def test_episode_count_does_not_exhaust_default_source_scope(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    monkeypatch.setattr(context_index, "MAX_SCOPE_VERSIONS", 1)
    real = save(book, "source", "Hosting spend is 1500 USD.")
    for number in range(4):
        save(book, f"_episodes/{number}", "review hosting spend", "memory_episode")
    assert (
        context_index.ranked_search(book, "hosting")["hits"][0]["artifact_id"]
        == real["artifact_id"]
    )


def payload(source, *, verified=True):
    return {
        "kind": "tool_result",
        "tool": "query",
        "verified": verified,
        "goal": "review hosting spend",
        "accounting_note": "do not index this",
        "facts": {
            "columns": ["hosting_spend_usd"],
            "rows": [[1500]],
            "inputs": [source["artifact_id"]],
        },
    }


def test_projection_omits_ledger_boilerplate_deduplicates_and_tracks_source_freshness(
    tmp_path,
):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    fact = project_memory_fact(book, payload(source), principal=Principal("observer"))
    again = project_memory_fact(
        book,
        payload(source) | {"goal": "different goal", "elapsed_seconds": 99},
        principal=Principal("other"),
    )
    assert fact["artifact_id"] == again["artifact_id"]
    inspected = book.inspect(fact["artifact_id"], excerpt_bytes=4096)
    body = inspected["excerpt"]
    assert "hosting_spend_usd" in body and "1500" in body
    assert "execution_observed_not_independently_validated" in body
    assert (
        "accounting_note" not in body
        and "review hosting spend" not in body
        and "elapsed_seconds" not in body
    )
    assert inspected["dependencies"] == [source["artifact_id"]]
    book.save_text(
        "1700",
        name="invoices",
        kind="document",
        media_type="text/plain",
        expected_version=1,
    )
    assert book.inspect(fact["artifact_id"])["stale_inputs"] is True


@pytest.mark.parametrize(
    "tool",
    ["recall", "help", "task_status", "session_status", "inspect", "find", "save"],
)
def test_status_reads_and_self_recall_never_produce_facts(tmp_path, tool):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    assert (
        project_memory_fact(
            book, payload(source) | {"tool": tool}, principal=Principal("observer")
        )
        is None
    )


def test_explicit_decision_remains_unverified_and_failed_execution_is_not_projected(
    tmp_path,
):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    assert (
        project_memory_fact(
            book, payload(source, verified=False), principal=Principal("observer")
        )
        is None
    )
    fact = project_memory_fact(
        book,
        {
            "kind": "decision",
            "verified": False,
            "facts": {
                "decision": "Review unused cloud instances before renewal.",
                "refs": [source["artifact_id"]],
            },
        },
        principal=Principal("observer"),
    )
    assert (
        "unverified_external_report"
        in book.inspect(fact["artifact_id"], excerpt_bytes=4096)["excerpt"]
    )
    assert fact["kind"] == "memory_fact"


def test_unpublished_fact_is_not_recallable_even_with_explicit_ref(tmp_path):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    fact = project_memory_fact(book, payload(source), principal=Principal("observer"))
    seed_legacy_index(book, [fact["artifact_id"]])
    assert context_index.ranked_search(book, "hosting_spend_usd")["hits"] == []
    assert (
        context_index.ranked_search(book, "hosting_spend_usd", [fact["artifact_id"]])[
            "hits"
        ]
        == []
    )
    assert (
        semantic.hybrid_search(
            book,
            "hosting_spend_usd",
            [fact["artifact_id"]],
            _model_spec=MODEL,
            _embedder=embed,
        )["hits"]
        == []
    )


def test_failed_capture_publication_hides_fact_until_exact_retry(tmp_path):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    session = start_session(book, Principal("observer"), "review hosting spend")
    result = payload(source)["facts"]
    with book.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_capture BEFORE INSERT ON coordination_events WHEN NEW.kind='session.episode' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        capture_tool(session, "query", {}, result, event_id="same-query")
    assert context_index.ranked_search(book, "hosting_spend_usd")["hits"] == []
    with book.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM artifacts WHERE kind='memory_fact'"
            ).fetchone()[0]
            == 1
        )
        db.execute("DROP TRIGGER fail_capture")
    capture_tool(session, "query", {}, result, event_id="same-query")
    recalled = context_index.ranked_search(book, "hosting_spend_usd")
    assert len(recalled["hits"]) == 1
    assert json.loads(recalled["hits"][0]["excerpt"])["result"]["rows"] == [[1500]]


def test_repeated_task_results_deduplicate_across_new_run_reports(tmp_path):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    result = save(book, "result", "1500")
    first_report = save(book, "report1", "first run")
    second_report = save(book, "report2", "second run")
    content = {
        "state": "complete",
        "artifact_id": first_report["artifact_id"],
        "sources": {"invoices": {"artifact_id": source["artifact_id"]}},
        "steps": [
            {
                "name": "total",
                "state": "complete",
                "artifact_id": result["artifact_id"],
                "result": payload(source)["facts"],
            }
        ],
    }
    event = {
        "kind": "tool_result",
        "tool": "task_run",
        "verified": True,
        "facts": content,
    }
    first = project_memory_fact(book, event, principal=Principal("observer"))
    second = project_memory_fact(
        book,
        event | {"facts": content | {"artifact_id": second_report["artifact_id"]}},
        principal=Principal("observer"),
    )
    assert first["artifact_id"] == second["artifact_id"]
    assert (
        first_report["artifact_id"]
        not in book.inspect(first["artifact_id"])["dependencies"]
    )


def test_projection_retries_fact_deleted_after_duplicate_name_conflict(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    event, principal = payload(source), Principal("observer")
    original = project_memory_fact(book, event, principal=principal)
    save_text = book.save_text
    conflict, resume = Event(), Event()
    attempts = 0

    def interrupted(content, **fields):
        nonlocal attempts
        attempts += 1
        try:
            return save_text(content, **fields)
        except ValueError:
            conflict.set()
            assert resume.wait(5)
            raise

    monkeypatch.setattr(book, "save_text", interrupted)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(project_memory_fact, book, event, principal=principal)
        assert conflict.wait(5)
        try:
            book.delete(original["name"], expected_version=1)
        finally:
            resume.set()
        recreated = future.result(timeout=5)
    assert attempts == 2
    assert recreated["artifact_id"] != original["artifact_id"]
    assert recreated["version"] == 2
    assert book.inspect(recreated["artifact_id"], excerpt_bytes=4096)["excerpt"]


def test_projection_does_not_retry_other_validation_or_conflicting_content(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    source = save(book, "invoices", "1500")
    event, principal = payload(source), Principal("observer")
    original = project_memory_fact(book, event, principal=principal)
    book.save_text(
        "conflicting body",
        name=original["name"],
        kind="memory_fact",
        media_type="application/json",
        expected_version=1,
    )
    with pytest.raises(ValueError, match="version 2"):
        project_memory_fact(book, event, principal=principal)
    calls = []

    def invalid(*args, **kwargs):
        calls.append(1)
        raise ValueError("source integrity check failed")

    monkeypatch.setattr(book, "save_text", invalid)
    with pytest.raises(ValueError, match="source integrity"):
        project_memory_fact(book, event, principal=principal)
    assert len(calls) == 1
