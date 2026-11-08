"""Capture retention is bounded, reference-safe, and transactionally authorized."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_workbook import continuity as c
from agent_workbook import coordination as co
from agent_workbook.store import Workbook


def start(book, goal="A private session goal"):
    return c.start_session(book, co.Principal("worker"), goal)


def decision(session, text="Retain the exact supporting evidence", **kwargs):
    return c.capture_event(session, "decision", {"decision": text}, **kwargs)


def age(monkeypatch, days=31):
    current = time.time()
    monkeypatch.setattr(c.time, "time", lambda: current + days * 86400)


def counts(book):
    with book.connect() as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "artifacts",
                "continuity_episodes",
                "continuity_sessions",
                "continuity_usage",
                "continuity_activity",
                "continuity_facts",
                "coordination_events",
            )
        }


def test_capture_refresh_serializes_against_expired_session_cleanup(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    session = start(book)
    age(monkeypatch)
    resolved, release = threading.Event(), threading.Event()
    original = c._session

    def pause_after_read(db, current):
        result = original(db, current)
        if not resolved.is_set():
            resolved.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(c, "_session", pause_after_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decision, session)
        try:
            assert resolved.wait(5)
            with (
                sqlite3.connect(book.catalog, timeout=0.01) as competing,
                pytest.raises(sqlite3.OperationalError, match="locked"),
            ):
                competing.execute("BEGIN IMMEDIATE")
        finally:
            release.set()
        assert future.result()["artifact_id"]
    assert c.prune_episodes(book)["retention"]["episodes_removed"] == 0


def test_bounded_cleanup_continues_and_keeps_identity_tombstones(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    session = start(book)
    for index in range(257):
        c.capture_tool(session, "save", {}, {"name": "item-" + str(index)})
    age(monkeypatch)
    first = c.prune_episodes(book)["retention"]
    assert first["episodes_removed"] == 256 and first["more_pending"]
    second = c.prune_episodes(book)["retention"]
    assert second["episodes_removed"] == 1
    assert second["expired_usage_totals"]["episode_count"] == 257
    assert not c.prune_episodes(book)["retention"]["more_pending"]
    with book.connect() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM name_versions WHERE name LIKE '_episodes/%'"
            ).fetchone()[0]
            == 257
        )


def test_fact_reuse_retries_when_cleanup_wins_before_ownership_commit(
    tmp_path, monkeypatch
):
    from agent_workbook import memory_projection

    book = Workbook(tmp_path)
    decision(start(book))
    with book.connect() as db:
        old_fact = db.execute(
            "SELECT fact_artifact_id FROM continuity_facts"
        ).fetchone()[0]
    age(monkeypatch)
    new_session = start(book)
    projected, release = threading.Event(), threading.Event()
    original = memory_projection.project_memory_fact
    attempts = []

    def pause_after_projection(*args, **kwargs):
        fact = original(*args, **kwargs)
        attempts.append(fact["artifact_id"])
        if len(attempts) == 1:
            projected.set()
            assert release.wait(5)
        return fact

    monkeypatch.setattr(
        memory_projection, "project_memory_fact", pause_after_projection
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decision, new_session, event_id="same-observation")
        try:
            assert projected.wait(5)
            cleanup = c.prune_episodes(book)
            assert cleanup["retention"]["facts_removed"] == 1
        finally:
            release.set()
        receipt = future.result(timeout=5)
    assert attempts[0] == old_fact and attempts[1] != old_fact
    assert new_session.as_dict()["episode_count"] == 1
    with book.connect() as db:
        assert (
            db.execute(
                "SELECT fact_artifact_id FROM continuity_facts WHERE episode_artifact_id=?",
                (receipt["artifact_id"],),
            ).fetchone()[0]
            == attempts[1]
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert decision(new_session, event_id="same-observation")["reused"]


def test_repeated_fact_disappearance_is_bounded_and_retryable(tmp_path, monkeypatch):
    from agent_workbook import memory_projection

    book = Workbook(tmp_path)
    session = start(book)
    calls = []
    original = memory_projection.project_memory_fact

    def disappears(*args, **kwargs):
        calls.append(1)
        return {"artifact_id": "wb_" + "0" * 32}

    monkeypatch.setattr(memory_projection, "project_memory_fact", disappears)
    with pytest.raises(ValueError, match="retry with the same event_id"):
        decision(session, event_id="retry-me")
    assert len(calls) == 3 and session.as_dict()["episode_count"] == 0
    monkeypatch.setattr(memory_projection, "project_memory_fact", original)
    receipt = decision(session, event_id="retry-me")
    assert not receipt["reused"] and session.as_dict()["episode_count"] == 1


@pytest.mark.parametrize(
    "name,kind",
    [
        ("xmemory_facts/user-note", "memory_fact"),
        ("xepisodes/user-note", "memory_episode"),
        ("_Memory_facts/user-note", "memory_fact"),
    ],
)
def test_retention_namespace_prefixes_are_literal(tmp_path, monkeypatch, name, kind):
    book = Workbook(tmp_path)
    c.initialize(book)
    user = book.save_text("user content", name=name, kind=kind, media_type="text/plain")
    age(monkeypatch)
    assert c.prune_episodes(book)["removed"] == []
    assert (
        book.inspect(user["artifact_id"], excerpt_bytes=100)["excerpt"]
        == "user content"
    )


def test_managed_delete_error_and_superseded_prune_do_not_raise_sqlite_fk(tmp_path):
    book = Workbook(tmp_path)
    receipt = decision(start(book))
    item = book.inspect(receipt["artifact_id"])
    for ref, options in (
        (receipt["artifact_id"], {}),
        (item["name"], {"expected_version": 1}),
    ):
        with pytest.raises(ValueError, match="Managed continuity.*prune"):
            book.delete(ref, **options)
    book.save_text(
        "{}",
        name=item["name"],
        kind="memory_episode",
        media_type="application/json",
        expected_version=1,
    )
    assert book.prune()["removed"] == []


def test_expiry_removes_ledgers_facts_sessions_and_keeps_aggregate_usage(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    session = start(book)
    decision(
        session, usage={"input_tokens": 31, "output_tokens": 5}, estimated_tokens=13
    )
    c.capture_event(session, "session_end", {})
    cursor = co.changes_since(book)["through"]
    age(monkeypatch)
    result = c.prune_episodes(book)
    assert result["retention"]["episodes_removed"] == 2
    assert result["retention"]["facts_removed"] == 1
    assert result["retention"]["sessions_removed"] == 1
    assert all(value == 0 for value in counts(book).values())
    totals = result["retention"]["expired_usage_totals"]
    assert totals["host_usage_totals"]["input_tokens"] == 31
    assert totals["estimated_rendered_tokens"] == 13
    assert totals["episode_count"] == 2
    assert co.changes_since(book, after=cursor)["through"] == cursor
    assert co.changes_since(book)["history_pruned"] is True
    new = start(book)
    status = new.as_dict()
    assert status["episode_retention_days"] == 30 and status["expired_sessions"] == 1
    assert status["expired_usage_totals"] == totals


def test_active_session_protects_old_episodes_and_idle_open_session_expires(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    session = start(book)
    first = decision(session)
    age(monkeypatch)
    decision(session, "New activity")
    assert c.prune_episodes(book)["retention"]["episodes_removed"] == 0
    assert book.inspect(first["artifact_id"])
    age(monkeypatch)
    assert c.prune_episodes(book)["retention"]["episodes_removed"] == 2


def test_external_reference_retains_episode_then_release_allows_cleanup(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    session = start(book)
    receipt = decision(session)
    book.save_text(
        "proof",
        name="retainer",
        kind="note",
        media_type="text/plain",
        provenance=[receipt["artifact_id"]],
    )
    age(monkeypatch)
    result = c.prune_episodes(book)
    assert result["retention"]["retained_referenced_episodes"] == 1
    assert result["retention"]["episodes_removed"] == 0
    assert book.inspect(receipt["artifact_id"])
    book.delete("retainer", expected_version=1)
    assert c.prune_episodes(book)["retention"]["episodes_removed"] == 1


def test_deduplicated_fact_survives_until_last_owning_session_expires(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    first = decision(start(book))
    age(monkeypatch)
    second = decision(start(book))
    with book.connect() as db:
        facts = db.execute("SELECT fact_artifact_id FROM continuity_facts").fetchall()
    assert len(facts) == 2 and facts[0][0] == facts[1][0]
    fact_id = facts[0][0]
    result = c.prune_episodes(book)
    assert result["retention"]["episodes_removed"] == 1
    assert result["retention"]["facts_removed"] == 0
    assert book.inspect(fact_id)
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect(first["artifact_id"])
    assert book.inspect(second["artifact_id"])
    age(monkeypatch)
    assert c.prune_episodes(book)["retention"]["facts_removed"] == 1


def test_policy_zero_disables_negative_rejected_and_dry_run_is_reversible(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    decision(start(book))
    c.configure_retention(book, 0)
    age(monkeypatch)
    assert c.prune_episodes(book)["removed"] == []
    before = counts(book)
    dry = book.prune(dry_run=True, episode_retention=1)
    assert dry["retention"]["episodes_removed"] == 1
    assert counts(book) == before
    with book.connect() as db:
        assert c.retention_policy(db) == 0
    for invalid in (-1, True, 1.5, 36501):
        with pytest.raises(ValueError, match="0–36500"):
            c.configure_retention(book, invalid)
    assert book.prune(episode_retention=1)["retention"]["episodes_removed"] == 1
    with book.connect() as db:
        assert c.retention_policy(db) == 1


def test_episode_cleanup_does_not_prune_unrelated_artifact_versions(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    original = book.save_text(
        "old", name="ordinary", kind="note", media_type="text/plain"
    )
    book.save_text(
        "new", name="ordinary", kind="note", media_type="text/plain", expected_version=1
    )
    decision(start(book))
    age(monkeypatch)
    c.prune_episodes(book)
    assert book.inspect(original["artifact_id"])
    assert any(
        row["artifact_id"] == original["artifact_id"] for row in book.prune()["removed"]
    )


def test_retention_guard_is_connection_local_and_restored_after_failure(tmp_path):
    book = Workbook(tmp_path)
    decision(start(book))
    with book.connect() as first, book.connect() as other:
        first.execute("BEGIN IMMEDIATE")
        with (
            pytest.raises(RuntimeError, match="injected"),
            co._retention_write(first, co.Principal("maintenance")),
        ):
            assert first.execute("SELECT workspool_retention()").fetchone()[0] == 1
            assert other.execute("SELECT workspool_retention()").fetchone()[0] == 0
            raise RuntimeError("injected")
        for table in ("continuity_episodes", "coordination_events"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                first.execute(f"DELETE FROM {table}")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                first.execute(f"UPDATE {table} SET rowid=rowid")
    with co.acting_as(co.Principal("reader", "reader")):
        with pytest.raises(PermissionError):
            c.prune_episodes(book)
        with pytest.raises(PermissionError):
            c.configure_retention(book, 1)


def test_cleanup_failure_rolls_back_all_retention_state(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    decision(start(book))
    before = counts(book)
    age(monkeypatch)
    original = book._remove

    def failure(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(book, "_remove", failure)
    with pytest.raises(RuntimeError, match="injected"):
        c.prune_episodes(book)
    assert counts(book) == before
    with book.connect() as db:
        assert (
            db.execute("SELECT retired_sessions FROM continuity_retention").fetchone()[
                0
            ]
            == 0
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM continuity_episodes")


def test_live_proof_events_remain_while_unreferenced_old_audits_expire(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    live = book.save_text("proof", name="live", kind="note", media_type="text/plain")
    c.initialize(book)
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        co.append_event(
            db, co.Principal("approver", "approver"), "approval", live["artifact_id"]
        )
        co.append_event(
            db,
            co.Principal("worker"),
            "task.published",
            "task:review",
            {"artifact_id": live["artifact_id"], "run_id": "retained-run"},
        )
        co.append_event(db, co.Principal("worker"), "claim.released", "task:gone")
    age(monkeypatch)
    result = c.prune_episodes(book)
    assert result["retention"]["events_removed"] == 1
    kinds = {event["kind"] for event in co.changes_since(book)["events"]}
    assert kinds == {"artifact.created", "approval", "task.published"}


def test_payload_and_summary_exclude_goal_and_accounting_note(tmp_path):
    book = Workbook(tmp_path)
    session = start(book, "UNIQUE_SESSION_GOAL_SHOULD_NOT_BE_RECALLED")
    receipt = decision(session)
    item = book.inspect(receipt["artifact_id"], excerpt_bytes=8192)
    body = json.loads(item["excerpt"])
    assert "goal" not in body and "accounting_note" not in body
    assert "UNIQUE_SESSION_GOAL" not in item["summary"]
    assert "accounting_note" in session.as_dict()


def test_expired_orphan_preledger_episode_is_collected(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    session = start(book)
    orphan = book.save_text(
        "{}",
        name="_episodes/" + session.session_id + "/abandoned",
        kind="memory_episode",
        media_type="application/json",
    )
    age(monkeypatch)
    result = c.prune_episodes(book)
    assert any(row["artifact_id"] == orphan["artifact_id"] for row in result["removed"])
    assert result["retention"]["sessions_removed"] == 1


def test_external_fact_reference_retains_published_owner_proof(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    receipt = decision(start(book))
    with book.connect() as db:
        fact = db.execute("SELECT fact_artifact_id FROM continuity_facts").fetchone()[0]
    book.save_text(
        "retained conclusion",
        name="report",
        kind="note",
        media_type="text/plain",
        provenance=[fact],
    )
    age(monkeypatch)
    retained = c.prune_episodes(book)["retention"]
    assert retained["retained_referenced_episodes"] == 1
    assert retained["episodes_removed"] == retained["facts_removed"] == 0
    assert book.inspect(receipt["artifact_id"]) and book.inspect(fact)
    with book.connect() as db:
        assert db.execute("SELECT count(*) FROM continuity_facts").fetchone()[0] == 1
    book.delete("report", expected_version=1)
    expired = c.prune_episodes(book)["retention"]
    assert expired["episodes_removed"] == expired["facts_removed"] == 1


def test_legacy_catalog_without_fact_table_keeps_delete_actionable(tmp_path):
    book = Workbook(tmp_path)
    decision(start(book))
    with book.connect() as db:
        db.execute("DROP TABLE continuity_facts")
    item = book.save_text(
        "ordinary", name="ordinary", kind="note", media_type="text/plain"
    )
    book.delete("ordinary", expected_version=item["version"])


def test_explicit_prune_removes_only_fully_orphaned_finalized_task_rows(
    tmp_path, monkeypatch
):
    from agent_workbook import tasks

    book = Workbook(tmp_path)
    c.initialize(book)
    tasks._initialize(book)
    kept = book.save_text(
        "retained definition",
        name="definition",
        kind="task",
        media_type="application/json",
    )
    with book.connect() as db:
        for index, (run_id, definition, state) in enumerate(
            [
                ("orphan", "gone-definition", "complete"),
                ("kept", kept["artifact_id"], "complete"),
                ("running", "gone-running", "running"),
            ]
        ):
            db.execute("INSERT INTO task_heads VALUES(?,?,?,?)", (run_id, 1, 1, run_id))
            db.execute(
                "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    run_id,
                    index + 1,
                    definition,
                    "gone-anchor",
                    time.time(),
                    time.time() if state != "running" else None,
                    state,
                    '{"artifact_id":"gone-report"}',
                ),
            )
    age(monkeypatch)
    assert "orphan_runs_removed" not in c.prune_episodes(book)["retention"]
    result = book.prune()
    assert result["retention"]["orphan_runs_removed"] == 1
    with book.connect() as db:
        assert {r[0] for r in db.execute("SELECT run_id FROM task_runs")} == {
            "kept",
            "running",
        }
        assert (
            db.execute(
                "SELECT latest_success FROM task_heads WHERE name='orphan'"
            ).fetchone()[0]
            is None
        )
