"""Real task triggers, approval continuation, and competing poll snapshots."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_workbook import scheduler as s
from agent_workbook import tasks
from agent_workbook.coordination import Principal, claim, release
from agent_workbook.review import review_artifact
from agent_workbook.store import Workbook


@pytest.fixture
def book(tmp_path):
    book = Workbook(tmp_path / "book")
    book.save_text("n\n2\n3\n", name="numbers", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "total",
        "Review the total",
        {"data": "numbers"},
        [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            }
        ],
    )
    return book


def test_change_trigger_does_not_repeat_and_recalculates_new_source(book):
    s.configure_schedule(book, "daily", "total", on_change=True)
    first = s.tick(book)
    assert first["attempts"][0]["state"] == "complete"
    assert s.tick(book)["attempts"] == []
    book.save_text(
        "n\n7\n8\n",
        name="numbers",
        kind="table",
        media_type="text/csv",
        expected_version=1,
    )
    second = s.tick(book)
    assert second["attempts"][0]["run_id"] != first["attempts"][0]["run_id"]
    assert tasks.status(book, "total")["steps"][0]["result"]["rows"] == [[15]]
    assert s.tick(book)["attempts"] == []


def test_interval_coalesces_missed_ticks_and_error_is_not_hot_loop(book, monkeypatch):
    s.configure_schedule(book, "daily", "total", interval_seconds=60)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise ValueError("worker unavailable")

    monkeypatch.setattr(tasks, "run_task", fail)
    assert s.tick(book)["attempts"][0]["state"] == "failed"
    assert s.tick(book)["attempts"] == []
    with book.connect() as db:
        db.execute("UPDATE task_schedules SET next_due=0")
    assert s.tick(book)["attempts"][0]["state"] == "failed"
    assert len(calls) == 2


def test_two_stale_polls_cannot_consume_same_trigger_twice(book, monkeypatch):
    s.configure_schedule(book, "daily", "total", interval_seconds=3600)
    original = s._signature
    slow_ready, first_done = threading.Event(), threading.Event()
    executions = []

    def signature(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name.startswith("slow"):
            slow_ready.set()
            assert first_done.wait(10)
        return value

    real_run = tasks.run_task

    def execute(*args, **kwargs):
        executions.append(1)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(s, "_signature", signature)
    monkeypatch.setattr(tasks, "run_task", execute)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow") as pool:
        pending = pool.submit(s.tick, book)
        assert slow_ready.wait(10)
        assert s.tick(book)["attempts"][0]["state"] == "complete"
        first_done.set()
        assert pending.result()["attempts"] == []
    assert len(executions) == 1


def test_owner_capabilities_claims_and_disable(book):
    alice, bob = Principal("alice"), Principal("bob")
    saved = s.configure_schedule(
        book, "daily", "total", on_change=True, principal=alice
    )
    assert s.tick(book, principal=bob)["attempts"] == []
    with pytest.raises(PermissionError, match="owner"):
        s.configure_schedule(
            book, "daily", "total", on_change=True, expected_version=1, principal=bob
        )
    with pytest.raises(PermissionError):
        s.tick(
            book,
            principal=Principal("alice", capabilities=["read", "schedule", "task:run"]),
        )
    ticket = claim(book, bob, "task:total")
    assert s.tick(book, principal=alice)["attempts"] == [
        {"name": "daily", "state": "busy"}
    ]
    release(book, bob, ticket)
    assert s.tick(book, principal=alice)["attempts"][0]["state"] == "complete"
    s.configure_schedule(
        book,
        "daily",
        "total",
        on_change=True,
        enabled=False,
        expected_version=saved["version"],
        principal=alice,
    )
    assert s.tick(book, principal=alice)["attempts"] == []


def test_approval_event_resumes_and_input_change_requires_new_approval(book):
    tasks.define_task(
        book,
        "total",
        "Review the total",
        {"data": "numbers"},
        [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
                "approval": True,
            }
        ],
        expected_version=1,
    )
    s.configure_schedule(book, "daily", "total", on_change=True)
    first = s.tick(book)["attempts"][0]
    assert first["state"] == "awaiting_approval"
    assert s.tick(book)["attempts"] == []
    pending = tasks.status(book, "total")
    gate = pending["steps"][0]["approval_ref"]
    review_artifact(book, gate, principal=Principal("reviewer", "approver"))
    second = s.tick(book)["attempts"][0]
    assert second["state"] == "complete"
    resumed = tasks.status(book, "total")
    assert resumed["resume_from"] == first["run_id"]
    assert resumed["steps"][0]["result"]["rows"] == [[5]]
    book.save_text(
        "n\n10\n",
        name="numbers",
        kind="table",
        media_type="text/csv",
        expected_version=1,
    )
    assert s.tick(book)["attempts"][0]["state"] == "awaiting_approval"
    assert tasks.status(book, "total")["steps"][0]["approval_ref"] != gate


def test_paging_and_schedule_configuration_cas(book):
    for name in ("a", "b", "c"):
        s.configure_schedule(book, name, "total", on_change=True)
    first = s.tick(book, limit=1)
    assert first["next_after"] == "a" and not first["complete"]
    second = s.tick(book, limit=1, after=first["next_after"])
    assert second["next_after"] == "b"
    assert s.tick(book, after="b")["complete"]
    assert s.schedules(book, limit=2)["next_after"] == "b"
    with pytest.raises(ValueError, match="version"):
        s.configure_schedule(book, "a", "total", on_change=True)
    with pytest.raises(ValueError, match="interval"):
        s.configure_schedule(book, "bad", "total", interval_seconds=True)


def test_heartbeat_renews_the_existing_fence_and_schedule_lease(book):
    import time

    principal = Principal("runner")
    s.configure_schedule(book, "daily", "total", on_change=True, principal=principal)
    ticket = claim(book, principal, "task:total", ttl=60)
    with book.connect() as db:
        db.execute(
            "UPDATE task_schedules SET lease_owner='attempt',lease_until=?",
            (time.time() + 60,),
        )

    class OneHeartbeat:
        calls = 0

        def wait(self, timeout):
            self.calls += 1
            return self.calls > 1

    s._heartbeat(book, principal, "daily", "attempt", ticket, OneHeartbeat())
    with book.connect() as db:
        lease = db.execute(
            "SELECT fence,expires_at FROM coordination_claims WHERE resource='task:total'"
        ).fetchone()
        assert lease["fence"] == ticket["fence"]
        assert lease["expires_at"] > time.time() + 3500
        assert (
            db.execute("SELECT lease_until FROM task_schedules").fetchone()[0]
            > time.time() + 3500
        )
    release(book, principal, ticket)
