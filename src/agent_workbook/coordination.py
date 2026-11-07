"""Local identities, fenced leases, and append-only coordination provenance.

The server constructs Principal from trusted launch configuration, never tool
arguments. One HTTP bearer token represents that one configured identity; this
module does not authenticate remote users or isolate processes with filesystem
access. An approver can read and approve, but cannot perform ordinary writes.

Leases use host wall-clock time. Fences survive release and expiration. Before
publishing, validate_claim must run inside the SAME BEGIN IMMEDIATE transaction
as the publication CAS, immediately before writing, with no intervening external
work. This serializes publication against reacquisition. A check on a separate
connection is insufficient. Tickets are public fencing data, not credentials.
Append-only triggers prevent ordinary SQL updates/deletes of audit records;
they are not tamper-proof against the owner of the local database.
"""

import json
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS coordination_claims (
    resource TEXT PRIMARY KEY,
    owner TEXT,
    fence INTEGER NOT NULL CHECK(fence > 0),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS coordination_policy (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    require_claims INTEGER NOT NULL CHECK(require_claims IN (0,1))
);
INSERT OR IGNORE INTO coordination_policy VALUES(1,0);
CREATE TABLE IF NOT EXISTS coordination_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    resource TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coordination_events_resource
    ON coordination_events(resource, event_id);
CREATE TRIGGER IF NOT EXISTS coordination_events_no_update
BEFORE UPDATE ON coordination_events
BEGIN SELECT RAISE(ABORT, 'Coordination events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coordination_events_no_delete
BEFORE DELETE ON coordination_events
BEGIN SELECT RAISE(ABORT, 'Coordination events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS coordination_events_no_replace
BEFORE INSERT ON coordination_events
WHEN EXISTS(SELECT 1 FROM coordination_events WHERE event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'Coordination events are immutable'); END;
"""
PERMISSIONS = {
    "reader": frozenset({"read"}),
    "writer": frozenset({"read", "write"}),
    "approver": frozenset({"read", "approve"}),
}


@dataclass(frozen=True)
class Principal:
    """Trusted server configuration, not a schema for caller-supplied identity."""

    identity: str
    role: str = "writer"

    def __post_init__(self):
        if not isinstance(self.identity, str) or not re.fullmatch(
            r"[A-Za-z0-9_.@-]{1,128}", self.identity
        ):
            raise ValueError("Identity must be 1–128 ASCII name characters")
        if not isinstance(self.role, str) or self.role not in PERMISSIONS:
            raise ValueError("Role must be reader, writer, or approver")


_ACTING_PRINCIPAL = ContextVar("workspool_principal", default=None)


def current_principal():
    """Trusted execution identity, isolated across asynchronous call contexts."""
    return _ACTING_PRINCIPAL.get() or Principal("local")


@contextmanager
def acting_as(principal):
    """Bind trusted launch/session identity; never accept a tool-supplied actor.

    ContextVar state follows asyncio tasks and context-propagating worker calls.
    A shared Workbook has no mutable principal field. Plain new threads/processes
    start as local and need their trusted caller to propagate identity explicitly.
    """
    if not isinstance(principal, Principal):
        raise PermissionError("A server-configured principal is required")
    token = _ACTING_PRINCIPAL.set(principal)
    try:
        yield principal
    finally:
        _ACTING_PRINCIPAL.reset(token)


class ClaimConflict(ValueError):
    """An unexpired lease already belongs to a producer, including this owner."""


class ClaimLost(ValueError):
    """A ticket is expired, released, superseded, or belongs to another owner."""


def authorize(principal, action):
    if not isinstance(principal, Principal):
        raise PermissionError("A server-configured principal is required")
    if action not in ("read", "write", "approve"):
        raise ValueError("Unknown authorization action")
    if action not in PERMISSIONS[principal.role]:
        raise PermissionError(f"Role {principal.role!r} cannot {action}")


def initialize(workbook):
    """Create tables outside an existing publication transaction."""
    with workbook.connect() as db:
        db.executescript(SCHEMA)


def configure(workbook, *, require_claims):
    """Trusted local configuration only; never expose a tool to disable policy.

    Policy persists across server restarts and also applies to Python task runs.
    Like all local configuration, this is not protected from filesystem owners.
    """
    if type(require_claims) is not bool:
        raise ValueError("require_claims must be boolean")
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE coordination_policy SET require_claims=? WHERE singleton=1",
            (int(require_claims),),
        )


def policy(db):
    return bool(
        db.execute(
            "SELECT require_claims FROM coordination_policy WHERE singleton=1"
        ).fetchone()[0]
    )


def requires_claim(db, resource):
    """An active optional lease also requires a ticket, regardless of owner."""
    _resource(resource)
    return (
        policy(db)
        or db.execute(
            "SELECT 1 FROM coordination_claims WHERE resource=? AND owner IS NOT NULL AND expires_at>?",
            (resource, time.time()),
        ).fetchone()
        is not None
    )


def _resource(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("Resource must contain 1–256 non-control characters")
    return value


def _ttl(value):
    if type(value) is not int or not 1 <= value <= 3600:
        raise ValueError("Lease ttl must be an integer from 1 to 3600 seconds")
    return value


def _ticket(row):
    return {key: row[key] for key in ("resource", "owner", "fence", "expires_at")}


def append_event(db, principal, kind, resource, details=None):
    """Append within the caller's transaction; approvals require approver role.

    Details are recorded observations, not independently verified assertions.
    Use the reserved approval.* prefix for approval events. Actor and role always
    come from trusted Principal, regardless of fields inside details.
    """
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", kind):
        raise ValueError("Invalid event kind")
    authorize(
        principal,
        "approve" if kind == "approval" or kind.startswith("approval.") else "write",
    )
    _resource(resource)
    if details is not None and not isinstance(details, dict):
        raise ValueError("Event details must be a JSON object")
    body = json.dumps(
        details or {}, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(body.encode()) > 16384:
        raise ValueError("Event details exceed 16 KiB")
    if not db.in_transaction:
        raise ValueError("Appending an event requires an explicit transaction")
    return db.execute(
        "INSERT INTO coordination_events(occurred_at,actor,role,kind,resource,details) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), principal.identity, principal.role, kind, resource, body),
    ).lastrowid


def record_event(workbook, principal, kind, resource, details=None):
    """Standalone event; use append_event for atomic mutation + provenance."""
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return append_event(db, principal, kind, resource, details)


def claim(workbook, principal, resource, ttl=300):
    authorize(principal, "write")
    _resource(resource)
    _ttl(ttl)
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        now = time.time()
        row = db.execute(
            "SELECT * FROM coordination_claims WHERE resource=?", (resource,)
        ).fetchone()
        if row and row["owner"] is not None and row["expires_at"] > now:
            raise ClaimConflict(
                "Resource is already claimed; inspect owner or renew your ticket"
            )
        fence = row["fence"] + 1 if row else 1
        ticket = {
            "resource": resource,
            "owner": principal.identity,
            "fence": fence,
            "expires_at": now + ttl,
        }
        db.execute(
            "INSERT INTO coordination_claims VALUES(?,?,?,?) "
            "ON CONFLICT(resource) DO UPDATE SET owner=excluded.owner, "
            "fence=excluded.fence, expires_at=excluded.expires_at",
            tuple(ticket.values()),
        )
        append_event(db, principal, "claim.acquired", resource, ticket)
        return ticket


def validate_claim(db, principal, ticket, *, resource, required=True):
    """Validate immediately before publication in its catalog write transaction.

    BEGIN IMMEDIATE is recommended. A no-op write also acquires the write lock
    if the caller began a deferred transaction (an outdated snapshot fails).
    A supplied ticket is validated even when claims are optional. Persisted
    policy and active leases cannot be bypassed with required=False. Expiry is
    read from the database, never trusted from the submitted ticket.
    """
    authorize(principal, "write")
    _resource(resource)
    if type(required) is not bool:
        raise ValueError("required must be boolean")
    if not db.in_transaction:
        raise ValueError("Claim validation requires the publication write transaction")
    db.execute(
        "UPDATE coordination_claims SET fence=fence WHERE resource=?", (resource,)
    )
    required = required or requires_claim(db, resource)
    if ticket is None:
        if required:
            raise ClaimLost("A current claim ticket is required")
        return None
    if not isinstance(ticket, dict) or (
        ticket.get("resource") != resource
        or ticket.get("owner") != principal.identity
        or type(ticket.get("fence")) is not int
        or ticket["fence"] < 1
    ):
        raise ClaimLost("Claim ticket does not match the configured owner and resource")
    row = db.execute(
        "SELECT * FROM coordination_claims WHERE resource=?", (resource,)
    ).fetchone()
    if (
        not row
        or row["owner"] != principal.identity
        or row["fence"] != ticket["fence"]
        or row["expires_at"] <= time.time()
    ):
        raise ClaimLost(
            "Claim expired, was released, or was superseded; reacquire before publishing"
        )
    return _ticket(row)


def renew(workbook, principal, ticket, ttl=300):
    _ttl(ttl)
    resource = _resource(ticket.get("resource") if isinstance(ticket, dict) else None)
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = validate_claim(db, principal, ticket, resource=resource)
        current["expires_at"] = max(current["expires_at"], time.time() + ttl)
        db.execute(
            "UPDATE coordination_claims SET expires_at=? WHERE resource=?",
            (current["expires_at"], resource),
        )
        append_event(db, principal, "claim.renewed", resource, current)
        return current


def release(workbook, principal, ticket):
    resource = _resource(ticket.get("resource") if isinstance(ticket, dict) else None)
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = validate_claim(db, principal, ticket, resource=resource)
        db.execute(
            "UPDATE coordination_claims SET owner=NULL, expires_at=0 WHERE resource=?",
            (resource,),
        )
        append_event(db, principal, "claim.released", resource, current)
        return {"resource": resource, "fence": current["fence"], "released": True}


def owner(workbook, resource):
    _resource(resource)
    initialize(workbook)
    with workbook.connect() as db:
        row = db.execute(
            "SELECT * FROM coordination_claims WHERE resource=?", (resource,)
        ).fetchone()
        if row is None:
            return {"resource": resource, "owner": None, "fence": 0, "active": False}
        return _ticket(row) | {
            "active": row["owner"] is not None and row["expires_at"] > time.time()
        }


def changes_since(workbook, after=0, *, limit=50, through=None, resource=None):
    """Page immutable events to a fixed upper event ID; reuse returned through."""
    if (
        type(after) is not int
        or after < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("after must be nonnegative; limit must be 1–100")
    if through is not None and (type(through) is not int or through < after):
        raise ValueError("through must be an integer at least after")
    if resource is not None:
        _resource(resource)
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        head = db.execute(
            "SELECT COALESCE(MAX(event_id),0) FROM coordination_events"
        ).fetchone()[0]
        if after > head:
            raise ValueError("after is beyond the latest event in this workbook")
        through = head if through is None else min(through, head)
        query = "SELECT * FROM coordination_events WHERE event_id>? AND event_id<=?"
        args = [after, through]
        if resource is not None:
            query += " AND resource=?"
            args.append(resource)
        rows = db.execute(
            query + " ORDER BY event_id LIMIT ?", (*args, limit + 1)
        ).fetchall()
    complete = len(rows) <= limit
    events = [
        dict(row) | {"details": json.loads(row["details"])} for row in rows[:limit]
    ]
    return {
        "events": events,
        "through": through,
        "complete": complete,
        "next_after": max(after, through) if complete else events[-1]["event_id"],
    }


def why_stale(workbook, ref, *, limit=100):
    """Explain changed root/dependency versions from one catalog snapshot."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1–100")
    with workbook.connect() as db:
        db.execute("BEGIN")
        root = workbook._get(db, ref)
        rows = db.execute(
            "WITH RECURSIVE lineage(id) AS (SELECT ? UNION "
            "SELECT d.dependency_id FROM dependencies d JOIN lineage l ON d.artifact_id=l.id) "
            "SELECT a.artifact_id,a.name,a.version,c.artifact_id AS current_id, "
            "c.version AS current_version FROM lineage l JOIN artifacts a ON a.artifact_id=l.id "
            "LEFT JOIN latest c ON c.name=a.name WHERE c.artifact_id IS NULL OR c.artifact_id!=a.artifact_id "
            "ORDER BY a.name,a.version LIMIT ?",
            (root["artifact_id"], limit + 1),
        ).fetchall()
        status = workbook._status(db, root["artifact_id"])
        reasons = [
            dict(row)
            | {
                "reason": "artifact_superseded"
                if row["artifact_id"] == root["artifact_id"]
                else "dependency_superseded"
            }
            for row in rows[:limit]
        ]
        return {
            "artifact_id": root["artifact_id"],
            "name": root["name"],
            **status,
            "reasons": reasons,
            "complete": len(rows) <= limit,
        }
