"""Fenced producers cannot publish after takeover; audit and roles stay explicit."""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_workbook import coordination as c
from agent_workbook.store import Workbook


@pytest.fixture
def book(tmp_path):
    return Workbook(tmp_path / "workbook")


@pytest.fixture
def alice():
    return c.Principal("alice", "writer")


def validate(book, principal, ticket, *, resource="task:review", required=True):
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return c.validate_claim(
            db, principal, ticket, resource=resource, required=required
        )


@pytest.mark.parametrize(
    "role,allowed",
    [
        ("reader", {"read"}),
        ("writer", {"read", "write"}),
        ("approver", {"read", "approve"}),
    ],
)
def test_roles_are_separate_and_server_identity_is_required(role, allowed):
    principal = c.Principal("worker-1", role)
    for action in ("read", "write", "approve"):
        if action in allowed:
            c.authorize(principal, action)
        else:
            with pytest.raises(PermissionError):
                c.authorize(principal, action)
    with pytest.raises(PermissionError):
        c.authorize({"identity": "admin", "role": "approver"}, "approve")


@pytest.mark.parametrize(
    "identity,role", [("", "writer"), ("a\nb", "writer"), ("user", "admin"), ([], [])]
)
def test_invalid_principal(identity, role):
    with pytest.raises(ValueError):
        c.Principal(identity, role)


def test_renew_release_and_no_same_owner_duplicate(book, alice, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(c.time, "time", lambda: now[0])
    first = c.claim(book, alice, "task:review", ttl=10)
    assert first == {
        "resource": "task:review",
        "owner": "alice",
        "fence": 1,
        "expires_at": 110.0,
    }
    with pytest.raises(c.ClaimConflict):
        c.claim(book, alice, "task:review")
    now[0] = 105
    renewed = c.renew(book, alice, first, ttl=20)
    assert renewed["fence"] == 1 and renewed["expires_at"] == 125
    assert validate(book, alice, first) == renewed  # Old expiration is informational.
    assert c.release(book, alice, first)["released"]
    assert not c.owner(book, "task:review")["active"]
    with pytest.raises(c.ClaimLost):
        validate(book, alice, renewed)
    assert c.claim(book, alice, "task:review")["fence"] == 2
    assert [e["kind"] for e in c.changes_since(book)["events"]] == [
        "claim.acquired",
        "claim.renewed",
        "claim.released",
        "claim.acquired",
    ]


def test_expiry_takeover_fences_old_producer_and_untrusted_ticket(
    book, alice, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr(c.time, "time", lambda: now[0])
    first = c.claim(book, alice, "task:review", ttl=1)
    bob = c.Principal("bob")
    with pytest.raises(c.ClaimLost):
        validate(book, bob, first)
    with pytest.raises(c.ClaimLost):
        validate(book, alice, first, resource="task:another")
    now[0] = 101  # Expiration is exclusive, including the exact boundary.
    with pytest.raises(c.ClaimLost):
        validate(book, alice, first | {"expires_at": float("inf")})
    with pytest.raises(c.ClaimLost):
        c.renew(book, alice, first)
    second = c.claim(book, bob, "task:review")
    assert second["fence"] == 2 and c.owner(book, "task:review")["owner"] == "bob"
    for forged in (first, first | {"owner": "bob"}, first | {"fence": 2}):
        with pytest.raises(c.ClaimLost):
            validate(book, alice, forged)
    assert validate(book, bob, second) == second
    with pytest.raises(c.ClaimLost):
        c.release(book, alice, first)


def test_workspace_policy_and_active_optional_claim_cannot_be_bypassed(book, alice):
    c.initialize(book)
    assert validate(book, alice, None, required=False) is None
    ticket = c.claim(book, alice, "task:review")
    for principal in (alice, c.Principal("bob")):
        with pytest.raises(c.ClaimLost):
            validate(book, principal, None, required=False)
    c.release(book, alice, ticket)
    c.configure(book, require_claims=True)
    reopened = Workbook(book.root)
    with reopened.connect() as db:
        assert c.policy(db)
    with pytest.raises(c.ClaimLost):
        validate(reopened, alice, None, required=False)
    c.configure(book, require_claims=False)  # Trusted configuration only.
    assert validate(book, alice, None, required=False) is None


def test_publication_and_event_are_atomic_and_stale_ticket_cannot_publish(
    book, alice, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr(c.time, "time", lambda: now[0])
    first = c.claim(book, alice, "task:review", ttl=1)
    with book.connect() as db:
        db.execute("CREATE TABLE test_head (run TEXT)")
    now[0] = 102
    bob = c.Principal("bob")
    second = c.claim(book, bob, "task:review")
    with pytest.raises(c.ClaimLost), book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        c.validate_claim(db, alice, first, resource="task:review")
        db.execute("INSERT INTO test_head VALUES('stale')")
    with pytest.raises(RuntimeError), book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        c.validate_claim(db, bob, second, resource="task:review")
        db.execute("INSERT INTO test_head VALUES('rolled back')")
        c.append_event(db, bob, "task.published", "task:review", {"run": "rolled back"})
        raise RuntimeError("publication failed")
    with book.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM test_head").fetchone()[0] == 0
        db.execute("BEGIN IMMEDIATE")
        c.validate_claim(db, bob, second, resource="task:review")
        db.execute("INSERT INTO test_head VALUES('current')")
        c.append_event(
            db, bob, "task.published", "task:review", {"run": "current", "fence": 2}
        )
    events = c.changes_since(book)["events"]
    assert [
        e["details"].get("run") for e in events if e["kind"] == "task.published"
    ] == ["current"]


def test_publication_lock_blocks_takeover_until_commit(book, alice, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(c.time, "time", lambda: now[0])
    ticket = c.claim(book, alice, "task:review", ttl=1)
    ready, done = threading.Event(), threading.Event()
    result = []

    def acquire():
        ready.set()
        result.append(c.claim(book, c.Principal("bob"), "task:review"))
        done.set()

    thread = threading.Thread(target=acquire)
    with book.connect() as db:
        db.execute("BEGIN")  # The hook itself promotes this to a write transaction.
        c.validate_claim(db, alice, ticket, resource="task:review")
        now[0] = 102
        thread.start()
        assert ready.wait(2)
        assert not done.wait(0.05)
        c.append_event(db, alice, "task.published", "task:review", {"fence": 1})
    thread.join(5)
    assert not thread.is_alive() and result[0]["fence"] == 2
    assert [e["kind"] for e in c.changes_since(book)["events"]] == [
        "claim.acquired",
        "task.published",
        "claim.acquired",
    ]


def test_eight_threads_have_one_claim_winner(book):
    barrier = threading.Barrier(8)

    def attempt(number):
        barrier.wait(timeout=5)
        try:
            return c.claim(book, c.Principal(f"worker-{number}"), "resource:shared")
        except c.ClaimConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert len([ticket for ticket in results if ticket]) == 1
    assert len(c.changes_since(book)["events"]) == 1


def test_two_processes_have_one_claim_winner(book, tmp_path):
    code = """
import json, sys, time
from pathlib import Path
from agent_workbook.coordination import Principal, claim, ClaimConflict
from agent_workbook.store import Workbook
root, identity, ready, go = sys.argv[1:]
book = Workbook(Path(root))
Path(ready).touch()
deadline = time.monotonic() + 10
while not Path(go).exists():
    if time.monotonic() > deadline: raise RuntimeError('barrier timeout')
    time.sleep(.01)
try:
    print(json.dumps(claim(book, Principal(identity), 'task:review')))
except ClaimConflict:
    print('null')
"""
    go = tmp_path / "go"
    processes = []
    try:
        for number in range(2):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(book.root),
                        f"worker-{number}",
                        str(tmp_path / f"ready-{number}"),
                        str(go),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"ready-{n}").exists() for n in range(2)):
            assert time.monotonic() < deadline and all(
                p.poll() is None for p in processes
            )
            time.sleep(0.01)
        go.touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        assert sum(result is not None for result in results) == 1
        assert len(c.changes_since(book)["events"]) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


def test_immutable_events_include_actor_and_fixed_snapshot_pagination(book, alice):
    for index in range(4):
        c.record_event(
            book,
            alice,
            "artifact.saved",
            "artifact:data",
            {"index": index, "actor": "spoofed"},
        )
    first = c.changes_since(book, limit=2)
    c.record_event(book, alice, "artifact.saved", "artifact:other")
    second = c.changes_since(
        book, first["next_after"], limit=2, through=first["through"]
    )
    assert first["through"] == second["through"] == 4
    assert not first["complete"] and second["complete"] and second["next_after"] == 4
    assert [e["event_id"] for e in first["events"] + second["events"]] == [1, 2, 3, 4]
    assert all(e["actor"] == "alice" and e["role"] == "writer" for e in first["events"])
    assert len(c.changes_since(book, resource="artifact:other")["events"]) == 1
    for statement in (
        "UPDATE coordination_events SET actor='mallory' WHERE event_id=1",
        "DELETE FROM coordination_events WHERE event_id=1",
        "INSERT OR REPLACE INTO coordination_events SELECT * FROM coordination_events WHERE event_id=1",
    ):
        with (
            pytest.raises(sqlite3.IntegrityError, match="immutable"),
            book.connect() as db,
        ):
            db.execute(statement)


def test_approvals_are_separate_from_writes(book, alice):
    approver = c.Principal("reviewer", "approver")
    with pytest.raises(PermissionError):
        c.record_event(book, alice, "approval.granted", "task:review")
    c.record_event(
        book,
        approver,
        "approval.granted",
        "task:review",
        {"artifact_id": "specific immutable target"},
    )
    with pytest.raises(PermissionError):
        c.claim(book, approver, "task:review")
    with pytest.raises(PermissionError):
        c.record_event(book, approver, "artifact.saved", "artifact:data")
    with pytest.raises(PermissionError):
        c.record_event(
            book, c.Principal("guest", "reader"), "artifact.saved", "artifact:data"
        )
    assert len(c.changes_since(book)["events"]) == 1


def test_why_stale_explains_transitive_versions_in_one_snapshot(book):
    def save(name, body, dependencies=None, expected_version=None):
        return book.save_text(
            body,
            name=name,
            kind="note",
            media_type="text/plain",
            dependencies=dependencies,
            expected_version=expected_version,
        )

    source = save("source", "first")
    middle = save("middle", "derived", [source["artifact_id"]])
    final = save("final", "derived again", [middle["artifact_id"]])
    assert c.why_stale(book, final["artifact_id"])["reasons"] == []
    changed = save("source", "second", expected_version=1)
    result = c.why_stale(book, "final")
    assert result["is_latest"] and result["stale_inputs"] and result["complete"]
    assert result["reasons"] == [
        {
            "artifact_id": source["artifact_id"],
            "name": "source",
            "version": 1,
            "current_id": changed["artifact_id"],
            "current_version": 2,
            "reason": "dependency_superseded",
        }
    ]
    assert (
        c.why_stale(book, source["artifact_id"])["reasons"][0]["reason"]
        == "artifact_superseded"
    )
    save("middle", "new derived", [changed["artifact_id"]], expected_version=1)
    assert not c.why_stale(book, "final", limit=1)["complete"]


@pytest.mark.parametrize("ttl", [True, 0, -1, 3601, 1.5, None])
def test_invalid_ttl(book, alice, ttl):
    with pytest.raises(ValueError):
        c.claim(book, alice, "task:review", ttl=ttl)


def test_invalid_bounds_and_no_transaction_rejected(book, alice):
    c.initialize(book)
    with pytest.raises(ValueError):
        c.claim(book, alice, "bad\x00resource")
    with pytest.raises(ValueError):
        c.configure(book, require_claims="false")
    for arguments in ({"after": True}, {"after": 1}, {"limit": 0}, {"through": -1}):
        with pytest.raises(ValueError):
            c.changes_since(book, **arguments)
    with pytest.raises(ValueError):
        c.record_event(
            book, alice, "artifact.saved", "artifact:data", {"value": "x" * 16384}
        )
    with book.connect() as db:
        with pytest.raises(ValueError, match="transaction"):
            c.validate_claim(db, alice, None, resource="task:review", required=False)
        with pytest.raises(ValueError, match="transaction"):
            c.append_event(db, alice, "artifact.saved", "artifact:data")
