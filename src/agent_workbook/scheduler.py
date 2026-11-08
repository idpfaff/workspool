"""Durable, opt-in local triggers. An explicit runner polls; MCP never spawns it.

Triggers coalesce missed intervals and changed inputs. Claims serialize task
publication. Workers must tolerate retries after a runner crash; this is not an
exactly-once external-effects system.
"""

import hashlib
import json
import re
import threading
import time
from uuid import uuid4

from .coordination import (
    ClaimConflict,
    ClaimLost,
    acting_as,
    append_event,
    authorize_capability,
    claim,
    current_principal,
    release,
    renew,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS task_schedules (
    name TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    owner TEXT NOT NULL,
    revision INTEGER NOT NULL,
    interval_seconds INTEGER,
    on_change INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    next_due REAL NOT NULL,
    last_signature TEXT,
    last_approval_event INTEGER NOT NULL DEFAULT 0,
    last_run_id TEXT,
    last_state TEXT,
    last_attempt REAL,
    lease_owner TEXT,
    lease_until REAL NOT NULL DEFAULT 0
);
"""


def _heartbeat(workbook, principal, name, lease, ticket, stop):
    while not stop.wait(60):
        try:
            renew(workbook, principal, ticket, ttl=3600)
            with workbook.connect() as db:
                updated = db.execute(
                    "UPDATE task_schedules SET lease_until=? WHERE name=? AND lease_owner=? AND lease_until>?",
                    (time.time() + 3600, name, lease, time.time()),
                ).rowcount
            if not updated:
                return
        except Exception:  # noqa: BLE001 - publication checks the authoritative claim again
            return


def _initialize(workbook):
    with workbook.connect() as db:
        db.executescript(SCHEMA)


def _signature(workbook, task, run_id=None):
    from .tasks import _name, _read_plan

    with workbook.connect() as db:
        db.execute("BEGIN")
        definition = workbook._get(db, _name(task))
        plan = _read_plan(workbook, definition)
        sources = {
            alias: workbook._get(db, ref)["artifact_id"]
            for alias, ref in plan["sources"].items()
        }
        value = json.dumps([definition["artifact_id"], sources], sort_keys=True)
        event = 0
        if run_id:
            row = db.execute(
                "SELECT payload FROM task_runs WHERE run_id=? AND name=?",
                (run_id, task),
            ).fetchone()
            if row:
                refs = [
                    step["approval_ref"]
                    for step in json.loads(row[0]).get("steps", [])
                    if step.get("approval_ref")
                ]
                if refs:
                    event = db.execute(
                        "SELECT coalesce(max(event_id),0) FROM coordination_events "
                        "WHERE kind IN ('approval.accepted','approval.revoked') "
                        "AND resource IN (" + ",".join("?" for _ in refs) + ")",
                        refs,
                    ).fetchone()[0]
        return hashlib.sha256(value.encode()).hexdigest(), event


def configure_schedule(
    workbook,
    name,
    task,
    *,
    interval_seconds=None,
    on_change=False,
    enabled=True,
    expected_version=None,
    principal=None,
):
    principal = principal or current_principal()
    authorize_capability(principal, "schedule")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise ValueError("Schedule name must be 1–64 ASCII name characters")
    if interval_seconds is not None and (
        type(interval_seconds) is not int or not 60 <= interval_seconds <= 31536000
    ):
        raise ValueError("Schedule interval must be 60–31536000 seconds")
    if type(on_change) is not bool or type(enabled) is not bool:
        raise ValueError("on_change and enabled must be boolean")
    if interval_seconds is None and not on_change:
        raise ValueError("Choose an interval or on_change trigger")
    if expected_version is not None and (
        type(expected_version) is not int or expected_version < 0
    ):
        raise ValueError("expected_version must be a nonnegative integer")
    _signature(workbook, task)
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM task_schedules WHERE name=?", (name,)
        ).fetchone()
        if old and old["owner"] != principal.identity:
            raise PermissionError("Only the configured schedule owner can change it")
        version = old["revision"] if old else 0
        if version != (expected_version or 0):
            raise ValueError(
                f"Schedule is at version {version}; supply expected_version"
            )
        if old and old["lease_until"] > time.time():
            raise ValueError("Schedule has an active attempt; wait for it to finish")
        if (
            not old
            and db.execute("SELECT count(*) FROM task_schedules").fetchone()[0] >= 1000
        ):
            raise ValueError("At most 1000 schedules are supported")
        db.execute(
            "INSERT INTO task_schedules(name,task,owner,revision,interval_seconds,on_change,enabled,next_due) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET task=excluded.task, "
            "revision=excluded.revision,interval_seconds=excluded.interval_seconds,on_change=excluded.on_change, "
            "enabled=excluded.enabled,next_due=excluded.next_due,last_signature=NULL,last_approval_event=0, "
            "last_run_id=NULL,last_state=NULL,last_attempt=NULL,lease_owner=NULL,lease_until=0",
            (
                name,
                task,
                principal.identity,
                version + 1,
                interval_seconds,
                int(on_change),
                int(enabled),
                time.time(),
            ),
        )
        append_event(
            db,
            principal,
            "schedule.configured",
            "schedule:" + name,
            {"task": task, "version": version + 1, "enabled": enabled},
        )
    return {
        "name": name,
        "task": task,
        "owner": principal.identity,
        "version": version + 1,
        "enabled": enabled,
    }


def schedules(workbook, *, after="", limit=50, principal=None):
    principal = principal or current_principal()
    authorize_capability(principal, "read")
    if (
        not isinstance(after, str)
        or len(after) > 64
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("Schedule paging requires an after name and limit 1–100")
    _initialize(workbook)
    with workbook.connect() as db:
        rows = db.execute(
            "SELECT name,task,owner,revision AS version,interval_seconds,on_change,enabled,"
            "next_due,last_run_id,last_state,last_attempt,lease_until FROM task_schedules "
            "WHERE name>? ORDER BY name LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    return {
        "schedules": [dict(row) for row in rows[:limit]],
        "complete": len(rows) <= limit,
        "next_after": rows[limit - 1]["name"] if len(rows) > limit else None,
    }


def tick(workbook, *, workers=None, limit=10, after="", principal=None):
    """Check a bounded page and execute at most limit due, owner-matched tasks."""
    from .tasks import run_task

    principal = principal or current_principal()
    for capability in ("read", "schedule", "task:run", "claims"):
        authorize_capability(principal, capability)
    if (
        type(limit) is not int
        or not 1 <= limit <= 20
        or not isinstance(after, str)
        or len(after) > 64
    ):
        raise ValueError("Tick requires limit 1–20 and a schedule after name")
    _initialize(workbook)
    with workbook.connect() as db:
        rows = db.execute(
            "SELECT * FROM task_schedules WHERE owner=? AND enabled=1 AND name>? "
            "ORDER BY name LIMIT 101",
            (principal.identity, after),
        ).fetchall()
    outcomes, scanned, next_after = [], 0, None
    for row in rows[:100]:
        if len(outcomes) >= limit:
            next_after = rows[scanned - 1]["name"]
            break
        scanned += 1
        now = time.time()
        if row["lease_until"] > now:
            continue
        try:
            signature, approval_event = _signature(
                workbook, row["task"], row["last_run_id"]
            )
        except ValueError as error:
            outcomes.append(
                {"name": row["name"], "state": "invalid", "error": str(error)[:300]}
            )
            continue
        changed = signature != row["last_signature"]
        waiting = row["last_state"] == "awaiting_approval"
        approved = waiting and approval_event > row["last_approval_event"]
        due = row["last_attempt"] is None or (row["on_change"] and changed) or approved
        due |= bool(
            row["interval_seconds"]
            and row["next_due"] <= now
            and (not waiting or changed)
        )
        if not due:
            continue
        lease = uuid4().hex
        with workbook.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            acquired = db.execute(
                "UPDATE task_schedules SET lease_owner=?,lease_until=? WHERE name=? "
                "AND revision=? AND enabled=1 AND lease_until<=? "
                "AND last_attempt IS ? AND last_signature IS ? AND last_approval_event=? AND next_due=?",
                (
                    lease,
                    now + 3600,
                    row["name"],
                    row["revision"],
                    now,
                    row["last_attempt"],
                    row["last_signature"],
                    row["last_approval_event"],
                    row["next_due"],
                ),
            ).rowcount
        if not acquired:
            continue
        ticket, result, heartbeat = None, None, None
        stop = threading.Event()
        try:
            with acting_as(principal):
                ticket = claim(workbook, principal, "task:" + row["task"], ttl=3600)
                heartbeat = threading.Thread(
                    target=_heartbeat,
                    args=(workbook, principal, row["name"], lease, ticket, stop),
                    daemon=True,
                )
                heartbeat.start()
                result = run_task(
                    workbook,
                    row["task"],
                    workers=workers,
                    principal=principal,
                    claim_ticket=ticket,
                    resume_from=row["last_run_id"]
                    if approved and not changed
                    else None,
                )
        except ClaimConflict:
            outcomes.append({"name": row["name"], "state": "busy"})
        except Exception as error:  # noqa: BLE001 - one task must not stop unrelated schedules
            result = {"state": "failed", "error": str(error)[:300], "run_id": None}
        finally:
            stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=35)
            if ticket:
                try:
                    release(workbook, principal, ticket)
                except ClaimLost:
                    pass
            with workbook.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if result is None:
                    db.execute(
                        "UPDATE task_schedules SET lease_owner=NULL,lease_until=0 WHERE name=? AND lease_owner=?",
                        (row["name"], lease),
                    )
                else:
                    db.execute(
                        "UPDATE task_schedules SET lease_owner=NULL,lease_until=0,last_signature=?,"
                        "last_approval_event=?,last_run_id=?,last_state=?,last_attempt=?,next_due=? "
                        "WHERE name=? AND lease_owner=?",
                        (
                            signature,
                            approval_event,
                            result.get("run_id"),
                            result["state"],
                            now,
                            time.time() + (row["interval_seconds"] or 60),
                            row["name"],
                            lease,
                        ),
                    )
                    append_event(
                        db,
                        principal,
                        "schedule.attempted",
                        "schedule:" + row["name"],
                        {
                            "task": row["task"],
                            "run_id": result.get("run_id"),
                            "state": result["state"],
                        },
                    )
        if result is not None:
            outcomes.append(
                {
                    "name": row["name"],
                    **{
                        key: result[key]
                        for key in ("state", "run_id", "error")
                        if key in result
                    },
                }
            )
    if next_after is None and len(rows) > 100:
        next_after = rows[99]["name"]
    return {
        "attempts": outcomes,
        "scanned": scanned,
        "complete": next_after is None,
        "next_after": next_after,
    }
