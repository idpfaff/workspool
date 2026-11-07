"""Configured hooks capture bounded facts without fabricating retained context."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

from agent_workbook import continuity as c
from agent_workbook.coordination import Principal, changes_since, why_stale
from agent_workbook.store import Workbook


@pytest.fixture
def session(tmp_path):
    return c.start_session(
        Workbook(tmp_path / "book"),
        Principal("agent-one"),
        "Investigate salmon migrations",
    )


def payload(session, receipt):
    result = session.workbook.inspect(receipt["artifact_id"], excerpt_bytes=16384)
    return json.loads(result["excerpt"])


def save(book, body, name, **kwargs):
    return book.save_text(
        body, name=name, kind="note", media_type="text/plain", **kwargs
    )


def test_session_reopens_with_trusted_owner_and_same_goal(session):
    reopened = c.start_session(
        Workbook(session.workbook.root),
        session.principal,
        "Investigate salmon migrations",
        session.session_id,
    )
    assert reopened.as_dict()["session_id"] == session.session_id
    assert len(changes_since(session.workbook)["events"]) == 1
    with pytest.raises(PermissionError):
        c.start_session(
            session.workbook,
            Principal("other-agent"),
            "Investigate salmon migrations",
            session.session_id,
        )
    with pytest.raises(ValueError, match="different goal"):
        c.start_session(
            session.workbook, session.principal, "Unrelated goal", session.session_id
        )


def test_query_capture_keeps_small_preview_refs_and_lineage_not_sql_literals(session):
    source = save(session.workbook, "upstream", "observations")
    receipt = c.capture_tool(
        session,
        "query",
        {
            "refs": [source["artifact_id"]],
            "sql": "SELECT secret FROM input1",
            "parameters": ["never persist this"],
        },
        {
            "columns": ["species", "count"],
            "rows": [["salmon", 42]],
            "inputs": [source["artifact_id"]],
        },
        elapsed=0.02,
    )
    episode = payload(session, receipt)
    assert receipt["verified"] and episode["facts"]["rows"] == [["salmon", 42]]
    assert episode["facts"]["memory_preview"]
    assert "sql_sha256" in episode["arguments"] and "sql" not in episode["arguments"]
    assert "never persist this" not in json.dumps(episode)
    metadata = session.workbook.inspect(receipt["artifact_id"])
    assert metadata["dependencies"] == [source["artifact_id"]]
    save(session.workbook, "changed", "observations", expected_version=1)
    assert why_stale(session.workbook, receipt["artifact_id"])["stale_inputs"]
    events = changes_since(session.workbook)["events"]
    creation = next(
        event
        for event in events
        if event["kind"] == "artifact.created"
        and event["details"].get("artifact_id") == receipt["artifact_id"]
    )
    assert creation["actor"] == session.principal.identity


def test_secrets_are_redacted_in_goals_cells_errors_and_external_events(tmp_path):
    secret = "sk-proj-" + "a" * 40
    session = c.start_session(
        Workbook(tmp_path / "book"), Principal("agent"), "Investigate api_key=" + secret
    )
    receipt = c.capture_tool(
        session,
        "query",
        {"api_key": secret, "headers": {"Authorization": "Bearer " + secret}},
        {
            "columns": ["password", "value", {"name": "access_token"}],
            "rows": [["unprefixed-sensitive-value", secret, "plain-access-token"]],
        },
        error="Authorization=Bearer " + secret,
    )
    external = c.capture_event(
        session,
        "decision",
        {
            "summary": "https://user:password@example.org/api?api_key=" + secret,
            "reason": {"client_secret": "plain-sensitive-secret"},
        },
    )
    serialized = json.dumps(
        [
            session.as_dict(),
            payload(session, receipt),
            payload(session, external),
            changes_since(session.workbook),
        ]
    )
    for value in (
        secret,
        "unprefixed-sensitive-value",
        "plain-access-token",
        "plain-sensitive-secret",
        "user:password",
    ):
        assert value not in serialized
    assert "[REDACTED]" in serialized
    assert not receipt["verified"]


def test_bulk_source_never_captured_even_inside_rows(session):
    source_text = "NEVER_CAPTURE_THE_BULK_SOURCE" * 100000
    receipt = c.capture_tool(
        session,
        "inspect",
        {"content": source_text, "path": "/private/file"},
        {
            "artifact_id": "wb_" + "a" * 32,
            "excerpt": source_text,
            "content": source_text,
            "columns": ["body", "content"],
            "rows": [[source_text, source_text]],
        },
    )
    episode = payload(session, receipt)
    assert "NEVER_CAPTURE_THE_BULK_SOURCE" not in json.dumps(episode)
    assert "[omitted source content]" in json.dumps(episode)
    assert session.workbook.inspect(receipt["artifact_id"])["dependencies"] == []


def test_compact_caps_preserve_saved_artifact_reference(session):
    saved = save(session.workbook, "retained result", "result")
    receipt = c.capture_tool(
        session,
        "query",
        {},
        {
            "columns": ["column" + str(i) for i in range(30)],
            "rows": [["value" * 100 for _ in range(30)] for _ in range(30)],
            "saved": saved,
            "steps": [{"result": {"columns": ["x"], "rows": [["z" * 10000]]}}] * 8,
        },
    )
    metadata = session.workbook.inspect(receipt["artifact_id"])
    assert metadata["size_bytes"] <= c.MAX_EPISODE_BYTES
    assert metadata["dependencies"] == [saved["artifact_id"]]
    episode = payload(session, receipt)
    assert "memory_omitted" in episode["facts"]


@pytest.mark.parametrize(
    "result,error",
    [
        ({"state": "partial"}, None),
        ({"isError": True}, None),
        ({"error": "bad query"}, None),
        (None, "failed"),
    ],
)
def test_failed_tool_is_never_verified(session, result, error):
    receipt = c.capture_tool(session, "task_run", {}, result, error=error)
    assert not receipt["verified"] and not payload(session, receipt)["verified"]


def test_external_event_cannot_spoof_verification_or_identity(session):
    receipt = c.capture_event(
        session,
        "tool_result",
        {
            "summary": "reported result",
            "verified": True,
            "actor": "admin",
            "role": "approver",
        },
    )
    episode = payload(session, receipt)
    assert (
        not episode["verified"]
        and episode["actor"] == "agent-one"
        and episode["role"] == "writer"
    )
    assert "admin" not in json.dumps(episode)


def test_embedded_json_credential_in_error_is_redacted(session):
    receipt = c.capture_tool(
        session,
        "query",
        {},
        error='upstream failed: {"api_key": "plain-unprefixed-key", "password":"another-private-value"}',
    )
    body = json.dumps(payload(session, receipt))
    assert "plain-unprefixed-key" not in body
    assert "another-private-value" not in body


def test_external_tool_result_has_useful_bounded_facts_but_is_unverified(session):
    receipt = c.capture_event(
        session,
        "tool_result",
        {
            "tool": "external_query",
            "args": {"name": "reported-data", "token": "private"},
            "result": {
                "columns": ["count"],
                "rows": [[7]],
                "body": "never store this body",
            },
            "verified": True,
        },
    )
    episode = payload(session, receipt)
    assert not episode["verified"]
    assert episode["facts"]["result"]["rows"] == [[7]]
    assert "never store this body" not in json.dumps(episode)
    assert "private" not in json.dumps(episode)


@pytest.mark.parametrize("role", ["reader", "approver"])
def test_nonwriters_cannot_start_or_capture(session, role):
    principal = Principal("agent-one", role)
    with pytest.raises(PermissionError):
        c.start_session(session.workbook, principal, "goal")
    forged = c.Session(session.workbook, principal, session.session_id)
    with pytest.raises(PermissionError):
        c.capture_event(forged, "decision", {"summary": "no"})
    with pytest.raises(PermissionError):
        c.capture_tool(forged, "query", {}, {"columns": ["x"], "rows": [[1]]})
    assert c.recent(session.workbook) == []


def test_idempotence_and_accounting_do_not_double_count(session):
    first = c.capture_tool(
        session,
        "query",
        {},
        {"columns": ["n"], "rows": [[12]]},
        event_id="call-one",
        usage={"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80},
        estimated_tokens=33,
    )
    second = c.capture_tool(
        session,
        "query",
        {},
        {"columns": ["n"], "rows": [[12]]},
        event_id="call-one",
        usage={"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80},
        estimated_tokens=33,
    )
    assert first["artifact_id"] == second["artifact_id"] and second["reused"]
    status = session.as_dict()
    assert (
        status["episode_count"] == 1
        and status["host_usage_totals"]["input_tokens"] == 100
    )
    assert (
        status["host_usage_totals"]["cached_input_tokens"] == 80
        and status["estimated_rendered_tokens"] == 33
    )
    assert status["host_usage_reporting_events"]["cache_write_tokens"] == 0
    with pytest.raises(ValueError, match="different captured content"):
        c.capture_tool(
            session,
            "query",
            {},
            {"rows": [[99]], "columns": ["n"]},
            event_id="call-one",
        )
    assert (
        len(
            changes_since(session.workbook, resource="session:" + session.session_id)[
                "events"
            ]
        )
        == 2
    )


def test_retry_timing_can_change_but_stored_episode_remains_identical(session):
    first = c.capture_tool(
        session, "inspect", {}, {"name": "source"}, event_id="timed", elapsed=0.1
    )
    second = c.capture_tool(
        session, "inspect", {}, {"name": "source"}, event_id="timed", elapsed=0.9
    )
    assert second["reused"] and first["artifact_id"] == second["artifact_id"]
    assert payload(session, second)["elapsed_seconds"] == 0.1
    assert c.recent(session.workbook)[0]["episode"] == payload(session, second)


def test_repeated_reads_deduplicate_but_calls_with_usage_remain_counted(session):
    first = c.capture_tool(
        session,
        "inspect",
        {"ref": "source"},
        {"name": "source", "version": 1},
        elapsed=0.1,
    )
    second = c.capture_tool(
        session,
        "inspect",
        {"ref": "source"},
        {"name": "source", "version": 1},
        elapsed=0.2,
    )
    assert second["reused"] and first["artifact_id"] == second["artifact_id"]
    changed = c.capture_tool(
        session, "inspect", {"ref": "source"}, {"name": "source", "version": 2}
    )
    assert not changed["reused"]
    for _ in range(2):
        c.capture_tool(
            session, "inspect", {}, {"name": "source"}, usage={"input_tokens": 10}
        )
    assert session.as_dict()["episode_count"] == 4
    assert session.as_dict()["host_usage_totals"]["input_tokens"] == 20


def test_incremental_accounting_avoids_history_scan_and_backfills_once(
    session, monkeypatch
):
    for index in range(3):
        c.capture_event(
            session,
            "decision",
            {"summary": str(index)},
            usage={"input_tokens": index},
            estimated_tokens=2,
        )
    with session.workbook.connect() as db:
        db.execute(
            "DELETE FROM continuity_usage WHERE session_id=?", (session.session_id,)
        )
    assert session.as_dict()["host_usage_totals"]["input_tokens"] == 3
    statements = []
    original = session.workbook.connect

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(statements.append)
            yield db

    monkeypatch.setattr(session.workbook, "connect", traced)
    status = session.as_dict()
    assert status["episode_count"] == 3 and status["estimated_rendered_tokens"] == 6
    assert not any(
        "SELECT payload FROM continuity_episodes" in sql for sql in statements
    )


def test_eight_concurrent_retries_publish_one_episode_and_artifact(session):
    barrier = threading.Barrier(8)

    def capture(_):
        barrier.wait(timeout=5)
        return c.capture_event(
            session,
            "decision",
            {"summary": "Prioritize salmon river surveys"},
            event_id="same-event",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(capture, range(8)))
    assert len({item["artifact_id"] for item in results}) == 1
    assert sum(not item["reused"] for item in results) == 1
    assert session.as_dict()["episode_count"] == 1
    with session.workbook.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE kind='memory_episode'"
            ).fetchone()[0]
            == 1
        )


def test_crash_before_ledger_is_repaired_on_retry(session, monkeypatch):
    append = c.append_event

    def failure(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(c, "append_event", failure)
    with pytest.raises(RuntimeError, match="simulated"):
        c.capture_event(
            session,
            "decision",
            {"summary": "keep exact citations"},
            event_id="repair-me",
        )
    assert c.recent(session.workbook) == [] and session.as_dict()["episode_count"] == 0
    monkeypatch.setattr(c, "append_event", append)
    receipt = c.capture_event(
        session, "decision", {"summary": "keep exact citations"}, event_id="repair-me"
    )
    assert not receipt["reused"] and session.as_dict()["episode_count"] == 1
    with session.workbook.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE kind='memory_episode'"
            ).fetchone()[0]
            == 1
        )


def test_external_end_is_idempotent_and_prevents_further_capture(session):
    receipt = c.capture_event(
        session, "session_end", {"summary": "Finished"}, event_id="end"
    )
    assert not receipt["verified"] and session.as_dict()["ended_at"] is not None
    assert c.capture_event(
        session, "session_end", {"summary": "Finished"}, event_id="end"
    )["reused"]
    with pytest.raises(ValueError, match="ended"):
        c.capture_tool(session, "inspect", {}, {})


def test_recent_relevant_episodes_and_session_filter(session):
    salmon = c.capture_event(
        session,
        "decision",
        {"summary": "Salmon spawning survey requires upstream counts"},
    )
    other = c.start_session(
        session.workbook, session.principal, "Compiler optimization"
    )
    compiler = c.capture_event(
        other, "decision", {"summary": "Vectorize matrix multiplication"}
    )
    assert (
        c.recent(session.workbook, limit=1)[0]["artifact_id"] == compiler["artifact_id"]
    )
    assert (
        c.recent(session.workbook, text="spawning", limit=2)[0]["artifact_id"]
        == salmon["artifact_id"]
    )
    assert len(c.recent(session.workbook, session_id=session.session_id)) == 1
    assert c.recent(session.workbook, text="nonexistentword") == []


def test_ledger_is_immutable(session):
    c.capture_event(session, "decision", {"summary": "retain"})
    for sql in (
        "DELETE FROM continuity_episodes",
        "UPDATE continuity_episodes SET verified=1",
        "INSERT OR REPLACE INTO continuity_episodes SELECT * FROM continuity_episodes",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="immutable"),
            session.workbook.connect() as db,
        ):
            db.execute(sql)


@pytest.mark.parametrize(
    "usage,estimate",
    [
        ({"input_tokens": True}, None),
        ({"input_tokens": -1}, None),
        ({"cost_dollars": 1}, None),
        (None, True),
        (None, -1),
    ],
)
def test_invalid_usage_is_rejected(session, usage, estimate):
    with pytest.raises(ValueError):
        c.capture_event(
            session,
            "decision",
            {"summary": "no"},
            usage=usage,
            estimated_tokens=estimate,
        )


def test_reference_caps_and_input_bounds(session):
    refs = [
        save(session.workbook, f"source {i}", f"source-{i}")["artifact_id"]
        for i in range(20)
    ]
    receipt = c.capture_tool(
        session,
        "inspect",
        {},
        {"sources": {str(i): {"artifact_id": ref} for i, ref in enumerate(refs)}},
    )
    assert len(session.workbook.inspect(receipt["artifact_id"])["dependencies"]) <= 16
    for arguments in (
        {"limit": 0},
        {"limit": 21},
        {"text": "x" * 501},
        {"session_id": "bad"},
    ):
        with pytest.raises(ValueError):
            c.recent(session.workbook, **arguments)
    with pytest.raises(ValueError):
        c.capture_tool(session, "query", {}, {}, elapsed=float("nan"))
    with pytest.raises(ValueError):
        c.capture_event(session, "approval", {"summary": "no"})
    with pytest.raises(ValueError):
        c.capture_event(session, "decision", {}, event_id="sk-proj-" + "x" * 40)
