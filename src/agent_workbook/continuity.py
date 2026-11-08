"""Explicit-hook session memory: compact observations, never a background monitor.

The trusted server creates Session with its configured Principal. Tool hooks may
mark successful execution as observed; external events are always unverified.
Neither classification verifies factual claims inside source/tool content.
Credentials are defensively redacted, but this is not a secret-detection service:
hosts must not submit arbitrary sensitive payloads. Bulk source bodies, SQL
literals, parameters, and arbitrary request fields are never captured.

An episode artifact is saved under a deterministic, unique name before its
ledger entry. The immutable ledger and coordination event commit together.
Concurrent retries reuse that artifact; a crash between save and publication
can leave an unlisted artifact, repaired by retry with the same event ID. This
module does not claim one transaction across Workbook.save_text and the ledger.
"""

import hashlib
import itertools
import json
import math
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from .coordination import Principal, acting_as, append_event, authorize
from .coordination import initialize as initialize_coordination

SCHEMA = """
CREATE TABLE IF NOT EXISTS continuity_sessions (
    session_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    goal TEXT NOT NULL,
    created_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS continuity_episodes (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES continuity_sessions(session_id),
    event_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    kind TEXT NOT NULL,
    verified INTEGER NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    captured_at REAL NOT NULL,
    UNIQUE(session_id,event_id)
);
CREATE TABLE IF NOT EXISTS continuity_usage (
    session_id TEXT PRIMARY KEY REFERENCES continuity_sessions(session_id),
    counters TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS continuity_activity (
    session_id TEXT PRIMARY KEY REFERENCES continuity_sessions(session_id) ON DELETE CASCADE,
    touched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS continuity_activity_age ON continuity_activity(touched_at);
CREATE INDEX IF NOT EXISTS continuity_episode_artifact ON continuity_episodes(artifact_id);
INSERT OR IGNORE INTO continuity_activity
    SELECT s.session_id, MAX(s.created_at, COALESCE(s.ended_at,0),
        COALESCE((SELECT MAX(e.captured_at) FROM continuity_episodes e WHERE e.session_id=s.session_id),0))
    FROM continuity_sessions s WHERE NOT EXISTS (
        SELECT 1 FROM continuity_activity a WHERE a.session_id=s.session_id);
CREATE TABLE IF NOT EXISTS continuity_facts (
    episode_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    fact_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    PRIMARY KEY(episode_artifact_id,fact_artifact_id)
);
CREATE INDEX IF NOT EXISTS continuity_fact_owners ON continuity_facts(fact_artifact_id);
CREATE TABLE IF NOT EXISTS continuity_retention (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    days INTEGER NOT NULL DEFAULT 30 CHECK(days>=0),
    retired_sessions INTEGER NOT NULL DEFAULT 0,
    retired_usage TEXT NOT NULL DEFAULT '{}'
);
INSERT OR IGNORE INTO continuity_retention(singleton) VALUES(1);
CREATE TRIGGER IF NOT EXISTS continuity_episodes_no_update
BEFORE UPDATE ON continuity_episodes
BEGIN SELECT RAISE(ABORT,'Continuity episodes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS continuity_episodes_retention_guard
BEFORE DELETE ON continuity_episodes
WHEN workspool_retention() != 1
BEGIN SELECT RAISE(ABORT,'Continuity episodes are immutable'); END;
DROP TRIGGER IF EXISTS continuity_episodes_no_delete;
CREATE TRIGGER IF NOT EXISTS continuity_episodes_no_replace
BEFORE INSERT ON continuity_episodes
WHEN EXISTS(SELECT 1 FROM continuity_episodes
    WHERE sequence=NEW.sequence OR (session_id=NEW.session_id AND event_id=NEW.event_id))
BEGIN SELECT RAISE(ABORT,'Continuity episodes are immutable'); END;
"""
ID = re.compile(r"[0-9a-f]{32}\Z")
ARTIFACT = re.compile(r"wb_[0-9a-f]{32}\Z")
SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|credential|authorization|cookie|privatekey|"
    r"apikey|accesskey|accesstoken|refreshtoken|authtoken|bearertoken|token)$",
    re.IGNORECASE,
)
SECRET_TEXT = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
    r"|(?i:bearer\s+[A-Za-z0-9._~+/=-]{8,})"
    r"|(?i:(?:api[_-]?key|access[_-]?token|password|secret|authorization)[\"']?\s*[:=]\s*[\"']?[^\s,;]+)"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"
)
BULK_KEYS = {
    "content",
    "body",
    "excerpt",
    "raw",
    "raw_source",
    "blob",
    "html",
    "document",
}
SCALAR_FACTS = {
    "artifact_id",
    "name",
    "version",
    "kind",
    "media_type",
    "size_bytes",
    "run_id",
    "summary",
    "definition_id",
    "state",
    "current",
    "latest_success",
    "is_latest",
    "stale_inputs",
    "delivery",
    "base_run",
    "requires_retained_base",
    "brief_complete",
    "complete",
    "row_count",
    "rows_returned",
    "column_count",
    "preview_truncated",
    "offset",
    "next_offset",
    "start_byte",
    "end_byte",
    "status",
    "error",
    "message",
    "reason",
    "hit",
    "key",
    "integer_string_columns",
    "materialized_type_conversions",
}
NESTED_FACTS = {
    "saved",
    "result",
    "cache",
    "steps",
    "sources",
    "artifacts",
    "tables",
    "citations",
    "omitted",
    "matches",
    "reads",
    "queries",
    "inputs",
    "dependencies",
}
USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
}
MAX_EPISODE_BYTES = 12288
READ_TOOLS = {
    "inspect",
    "find",
    "help",
    "describe",
    "search",
    "retrieve",
    "context",
    "recall",
    "task_status",
}


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _sensitive(key):
    normalized = re.sub(r"[^A-Za-z]", "", str(key))
    return bool(SECRET_KEY.search(normalized))


def _text(value, limit=320):
    value = SECRET_TEXT.sub("[REDACTED]", str(value)[:4096])
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore") + (
        "…" if len(encoded) > limit else ""
    )


def _small(value, *, depth=0):
    if depth > 3:
        return "[omitted nested value]"
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else "[nonfinite]"
    if isinstance(value, str):
        return _text(value, 160)
    if isinstance(value, list):
        return [_small(item, depth=depth + 1) for item in value[:8]]
    if isinstance(value, dict):
        return {
            _text(key, 80): "[REDACTED]"
            if _sensitive(key)
            else "[omitted source content]"
            if key in BULK_KEYS
            else _small(item, depth=depth + 1)
            for key, item in itertools.islice(value.items(), 16)
        }
    return "[unsupported value]"


def _facts(value, *, depth=0):
    if depth > 4:
        return {"memory_omitted": "nesting limit"}
    if isinstance(value, list):
        return [_facts(item, depth=depth + 1) for item in value[:8]]
    if not isinstance(value, dict):
        return _small(value)
    result = {}
    for key, item in itertools.islice(value.items(), 100):
        if key in SCALAR_FACTS:
            result[key] = _small(item)
        elif key in NESTED_FACTS:
            if key == "sources" and isinstance(item, dict):
                result[key] = {
                    _text(alias, 40): _facts(source, depth=depth + 1)
                    for alias, source in itertools.islice(item.items(), 8)
                }
            else:
                result[key] = _facts(item, depth=depth + 1)
    columns, rows = value.get("columns"), value.get("rows")
    if isinstance(columns, list):
        labels = [
            column.get("name", "") if isinstance(column, dict) else str(column)
            for column in columns[:16]
        ]
        result["columns"] = [_small(column) for column in columns[:16]]
        if isinstance(value.get("types"), list):
            result["types"] = [_text(kind, 80) for kind in value["types"][:16]]
        if isinstance(rows, list):
            preview = []
            for row in rows[:4]:
                if isinstance(row, list):
                    preview.append(
                        [
                            "[REDACTED]"
                            if index < len(labels) and _sensitive(labels[index])
                            else "[omitted source content]"
                            if index < len(labels) and labels[index] in BULK_KEYS
                            else _small(cell)
                            for index, cell in enumerate(row[:16])
                        ]
                    )
                elif isinstance(row, dict):
                    preview.append(_small(row))
            result["rows"] = preview
            result["memory_preview"] = True
            result["memory_rows_omitted"] = max(0, len(rows) - len(preview))
        result["memory_columns_omitted"] = max(0, len(columns) - 16)
    return result


def _arguments(args):
    if not isinstance(args, dict):
        return {}
    allowed = {
        "ref",
        "refs",
        "name",
        "save_as",
        "offset",
        "bytes",
        "limit",
        "force",
        "expected_version",
        "resume_from",
        "base_run",
    }
    result = {key: _small(args[key]) for key in allowed if key in args}
    if isinstance(args.get("sql"), str):
        if len(args["sql"]) <= 65536:
            result["sql_sha256"] = hashlib.sha256(args["sql"].encode()).hexdigest()
        else:
            result["sql_omitted"] = "statement exceeds capture limit"
    return result


def _accounting(usage, estimated_tokens):
    if usage is not None and (
        not isinstance(usage, dict)
        or set(usage) - USAGE_KEYS
        or any(
            value is not None and (type(value) is not int or not 0 <= value <= 10**15)
            for value in usage.values()
        )
    ):
        raise ValueError("usage requires nonnegative host-reported token counts")
    if estimated_tokens is not None and (
        type(estimated_tokens) is not int or not 0 <= estimated_tokens <= 10**15
    ):
        raise ValueError("estimated_tokens must be a nonnegative integer")
    return {
        "host_reported_usage": usage,
        "estimated_rendered_tokens": estimated_tokens,
    }


def initialize(workbook):
    initialize_coordination(workbook)
    with workbook.connect() as db:
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")


def _retention_days(days):
    if type(days) is not int or not 0 <= days <= 36500:
        raise ValueError("episode_retention must be 0–36500 days; 0 disables expiry")
    return days


def configure_retention(workbook, days=30):
    """Trusted persisted policy; 0 disables expiry, negatives are invalid."""
    from .coordination import current_principal

    authorize(current_principal(), "write")
    _retention_days(days)
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE continuity_retention SET days=? WHERE singleton=1", (days,))


def retention_policy(db):
    return db.execute(
        "SELECT days FROM continuity_retention WHERE singleton=1"
    ).fetchone()[0]


def _touch(db, session_id):
    db.execute(
        "INSERT INTO continuity_activity VALUES(?,?) ON CONFLICT(session_id) "
        "DO UPDATE SET touched_at=excluded.touched_at",
        (session_id, time.time()),
    )


def _expire(db, workbook, days=None):
    """Bounded transactional expiry. Caller holds the write/retention guard."""
    days = retention_policy(db) if days is None else _retention_days(days)
    report = {
        "days": days,
        "episodes_removed": 0,
        "facts_removed": 0,
        "sessions_removed": 0,
        "events_removed": 0,
        "retained_referenced_episodes": 0,
        "retained_referenced_facts": 0,
        "more_pending": False,
    }
    removed = []
    if days == 0:
        return report, removed
    cutoff = time.time() - days * 86400
    retired = db.execute(
        "SELECT retired_usage FROM continuity_retention WHERE singleton=1"
    ).fetchone()[0]
    counters = _counter_values(json.loads(retired))
    # Leaf-first passes release references among old episodes without deleting
    # anything a live artifact still pins. Each invocation removes at most 256.
    budget = 256
    while budget:
        rows = db.execute(
            """SELECT e.* FROM continuity_episodes e
            JOIN continuity_activity s ON s.session_id=e.session_id
            WHERE s.touched_at<? AND e.captured_at<?
              AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=e.artifact_id)
              AND NOT EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=e.artifact_id)
              AND NOT EXISTS(SELECT 1 FROM continuity_facts f WHERE f.episode_artifact_id=e.artifact_id
                AND (EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=f.fact_artifact_id)
                  OR EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=f.fact_artifact_id)))
            ORDER BY e.sequence DESC LIMIT ?""",
            (cutoff, cutoff, budget),
        ).fetchall()
        if not rows:
            break
        artifacts = [workbook._get(db, row["artifact_id"]) for row in rows]
        for row in rows:
            _count(counters, json.loads(row["payload"]))
            db.execute(
                "DELETE FROM continuity_facts WHERE episode_artifact_id=?",
                (row["artifact_id"],),
            )
            db.execute(
                "DELETE FROM continuity_episodes WHERE sequence=?", (row["sequence"],)
            )
        removed += workbook._remove(db, artifacts, splice=True)
        report["episodes_removed"] += len(rows)
        budget -= len(rows)
    # Include abandoned pre-ledger saves. A recently active session protects its
    # in-flight capture; missing sessions are not silently recreated by cleanup.
    orphaned = db.execute(
        """SELECT a.* FROM artifacts a WHERE a.kind='memory_episode'
        AND a.name GLOB '_episodes/*' AND julianday(a.created_at)<julianday(?,'unixepoch')
        AND NOT EXISTS(SELECT 1 FROM continuity_episodes e WHERE e.artifact_id=a.artifact_id)
        AND NOT EXISTS(SELECT 1 FROM continuity_activity s
            WHERE s.session_id=substr(a.name,11,32) AND s.touched_at>=?)
        AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=a.artifact_id)
        AND NOT EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=a.artifact_id)
        LIMIT ?""",
        (cutoff, cutoff, budget),
    ).fetchall()
    if orphaned:
        removed += workbook._remove(db, orphaned, splice=True)
        report["episodes_removed"] += len(orphaned)
        budget -= len(orphaned)
    facts = db.execute(
        """SELECT a.* FROM artifacts a WHERE a.kind='memory_fact'
        AND a.name GLOB '_memory_facts/*' AND julianday(a.created_at)<julianday(?,'unixepoch')
        AND NOT EXISTS(SELECT 1 FROM continuity_facts f WHERE f.fact_artifact_id=a.artifact_id)
        AND NOT EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=a.artifact_id)
        AND NOT EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=a.artifact_id)
        LIMIT 256""",
        (cutoff,),
    ).fetchall()
    if facts:
        removed += workbook._remove(db, facts, splice=True)
        report["facts_removed"] = len(facts)
    sessions = db.execute(
        """SELECT s.session_id FROM continuity_activity s WHERE touched_at<?
        AND NOT EXISTS(SELECT 1 FROM continuity_episodes e WHERE e.session_id=s.session_id)
        AND NOT EXISTS(SELECT 1 FROM artifacts a WHERE a.name GLOB '_episodes/' || s.session_id || '/*')
        LIMIT 256""",
        (cutoff,),
    ).fetchall()
    for row in sessions:
        db.execute("DELETE FROM continuity_usage WHERE session_id=?", (row[0],))
        db.execute("DELETE FROM continuity_sessions WHERE session_id=?", (row[0],))
    report["sessions_removed"] = len(sessions)
    db.execute(
        "UPDATE continuity_retention SET retired_sessions=retired_sessions+?, retired_usage=? WHERE singleton=1",
        (len(sessions), _json(counters)),
    )
    report["retained_referenced_episodes"] = db.execute(
        """SELECT count(*) FROM continuity_episodes e JOIN continuity_activity s USING(session_id)
        WHERE s.touched_at<? AND e.captured_at<? AND (
          EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=e.artifact_id) OR
          EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=e.artifact_id) OR
          EXISTS(SELECT 1 FROM continuity_facts f WHERE f.episode_artifact_id=e.artifact_id
            AND (EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=f.fact_artifact_id)
              OR EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=f.fact_artifact_id))))""",
        (cutoff, cutoff),
    ).fetchone()[0]
    report["retained_referenced_facts"] = db.execute(
        """SELECT count(*) FROM artifacts a WHERE a.kind='memory_fact'
        AND a.name GLOB '_memory_facts/*' AND julianday(a.created_at)<julianday(?,'unixepoch')
        AND NOT EXISTS(SELECT 1 FROM continuity_facts f WHERE f.fact_artifact_id=a.artifact_id)
        AND (EXISTS(SELECT 1 FROM dependencies d WHERE d.dependency_id=a.artifact_id)
             OR EXISTS(SELECT 1 FROM provenance_links p WHERE p.provenance_id=a.artifact_id))""",
        (cutoff,),
    ).fetchone()[0]
    from .coordination import _expire_events

    report["events_removed"] = _expire_events(db, cutoff)
    report["more_pending"] = (
        budget == 0
        or len(facts) == 256
        or len(sessions) == 256
        or report["events_removed"] == 512
    )
    report["expired_usage_totals"] = counters
    return report, removed


def prune_episodes(workbook, dry_run=False):
    """Expire only managed capture history; no ordinary artifact version prune.

    Bounded passes may require another call when more_pending is true. A session
    is inactive once it has had no capture/start activity for the retention
    window. Existing external references always take precedence over expiry.
    """
    from .coordination import _retention_write, current_principal

    authorize(current_principal(), "write")
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        with _retention_write(db, current_principal()):
            report, removed = _expire(db, workbook)
        if dry_run:
            db.rollback()
    result = {"retention": report, "removed": removed, "dry_run": dry_run}
    if not dry_run:
        result |= workbook._collect_garbage()
    return result


@dataclass(frozen=True)
class Session:
    workbook: object
    principal: Principal
    session_id: str

    def as_dict(self):
        return session_status(self.workbook, self.session_id)


def _session(db, session):
    authorize(session.principal, "write")
    row = db.execute(
        "SELECT * FROM continuity_sessions WHERE session_id=?", (session.session_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Unknown continuity session")
    if row["actor"] != session.principal.identity:
        raise PermissionError("Session belongs to a different configured identity")
    return row


def _empty_counters():
    return {
        "episode_count": 0,
        "host_usage_totals": {key: 0 for key in USAGE_KEYS},
        "host_usage_reporting_events": {key: 0 for key in USAGE_KEYS},
        "estimated_rendered_tokens": 0,
        "estimate_events": 0,
    }


def _counter_values(value):
    """Keep accounting fields separate from session/retention report metadata.

    Older or imported counters may contain an embedded status response. Its
    expired totals are already cumulative, not additional usage to sum again.
    Project the known top-level counters without recursively absorbing reports.
    """
    counters = _empty_counters()
    for key in ("episode_count", "estimated_rendered_tokens", "estimate_events"):
        if key in value:
            counters[key] = value[key]
    for key in ("host_usage_totals", "host_usage_reporting_events"):
        counters[key].update(
            {
                field: count
                for field, count in value.get(key, {}).items()
                if field in USAGE_KEYS
            }
        )
    return counters


def _count(counters, payload):
    counters["episode_count"] += 1
    for key, value in (payload.get("host_reported_usage") or {}).items():
        if value is not None:
            counters["host_usage_totals"][key] += value
            counters["host_usage_reporting_events"][key] += 1
    estimate = payload.get("estimated_rendered_tokens")
    if estimate is not None:
        counters["estimated_rendered_tokens"] += estimate
        counters["estimate_events"] += 1


def _counters(db, session_id):
    row = db.execute(
        "SELECT counters FROM continuity_usage WHERE session_id=?", (session_id,)
    ).fetchone()
    if row:
        return _counter_values(json.loads(row[0]))
    counters = _empty_counters()
    # One-time migration for sessions created before incremental accounting.
    for episode in db.execute(
        "SELECT payload FROM continuity_episodes WHERE session_id=?", (session_id,)
    ):
        _count(counters, json.loads(episode[0]))
    db.execute(
        "INSERT INTO continuity_usage VALUES(?,?)", (session_id, _json(counters))
    )
    return counters


def start_session(workbook, principal, goal, session_id=None):
    authorize(principal, "write")
    if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 2048:
        raise ValueError("Session goal must contain 1–2048 characters")
    goal = _text(goal.strip(), 1000)
    session_id = uuid4().hex if session_id is None else session_id
    if not isinstance(session_id, str) or not ID.fullmatch(session_id):
        raise ValueError("session_id must be 32 lowercase hexadecimal characters")
    initialize(workbook)
    session = Session(workbook, principal, session_id)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT 1 FROM continuity_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if existing:
            if _session(db, session)["goal"] != goal:
                raise ValueError("Session already has a different goal")
        else:
            db.execute(
                "INSERT INTO continuity_sessions VALUES(?,?,?,?,?,NULL)",
                (session_id, principal.identity, principal.role, goal, time.time()),
            )
            append_event(
                db,
                principal,
                "session.started",
                "session:" + session_id,
                {"goal": goal},
            )
        _counters(db, session_id)
        if _session(db, session)["ended_at"] is None:
            _touch(db, session_id)
    return session


def _references(value):
    found = set()
    if isinstance(value, str) and ARTIFACT.fullmatch(value):
        found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_references(item))
    return found


def _receipt(row, *, reused):
    return {
        "session_id": row["session_id"],
        "event_id": row["event_id"],
        "sequence": row["sequence"],
        "artifact_id": row["artifact_id"],
        "verified": bool(row["verified"]),
        "reused": reused,
    }


def _existing(db, session, event_id, payload_hash):
    row = db.execute(
        "SELECT * FROM continuity_episodes WHERE session_id=? AND event_id=?",
        (session.session_id, event_id),
    ).fetchone()
    if (
        row
        and row["payload_hash"] != payload_hash
        and _identity_digest(json.loads(row["payload"])) != payload_hash
    ):
        raise ValueError("Event ID already has different captured content")
    return row


def _identity_digest(payload):
    # A retry can measure a different duration without changing its evidence.
    return hashlib.sha256(
        _json(
            {
                key: value
                for key, value in payload.items()
                if key not in {"elapsed_seconds", "goal", "accounting_note"}
            }
        ).encode()
    ).hexdigest()


def _publish(session, kind, payload, *, verified, event_id):
    authorize(session.principal, "write")
    event_id = uuid4().hex if event_id is None else event_id
    if (
        not isinstance(event_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,128}", event_id)
        or SECRET_TEXT.search(event_id)
    ):
        raise ValueError("event_id must be a bounded non-secret identifier")
    workbook = session.workbook
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        session_row = _session(db, session)
        if session_row["ended_at"] is None:
            _touch(db, session.session_id)
    payload = {
        "format": 1,
        "session_id": session.session_id,
        "event_id": event_id,
        "actor": session.principal.identity,
        "role": session.principal.role,
        "kind": kind,
        "verified": verified,
        "verification_scope": "Executed tool observed by configured hook; result content is not independently validated."
        if verified
        else "External report or failed execution; not verified evidence.",
        **payload,
    }
    if len(_json(payload).encode()) > MAX_EPISODE_BYTES:
        payload["facts"] = {
            "memory_omitted": "result exceeded compact episode budget",
            "artifact_refs": sorted(_references(payload.get("facts")))[:16],
        }
    body = _json(payload)
    if len(body.encode()) > MAX_EPISODE_BYTES:
        raise ValueError("Compact episode exceeds 12 KiB")
    payload_hash = _identity_digest(payload)
    with workbook.connect() as db:
        existing = _existing(db, session, event_id, payload_hash)
        if existing:
            return _receipt(existing, reused=True)
        if _session(db, session)["ended_at"] is not None:
            raise ValueError("Session has ended; start a new session")
        references = sorted(_references(payload))
        dependencies = [
            ref
            for ref in references
            if db.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id=?", (ref,)
            ).fetchone()
        ][:16]
    name = (
        "_episodes/"
        + session.session_id
        + "/"
        + hashlib.sha256(event_id.encode()).hexdigest()
    )
    summary = _text(
        kind
        + " | "
        + payload.get("tool", "")
        + " | "
        + _json(payload.get("facts", {})),
        357,
    )
    try:
        with acting_as(session.principal):
            saved = workbook.save_text(
                body,
                name=name,
                kind="memory_episode",
                media_type="application/json",
                summary=summary,
                source="Configured continuity hook",
                dependencies=dependencies,
            )
        artifact_id = saved["artifact_id"]
    except ValueError:
        with workbook.connect() as db:
            row = db.execute("SELECT * FROM latest WHERE name=?", (name,)).fetchone()
            if (
                row is None
                or row["kind"] != "memory_episode"
                or row["version"] != 1
                or workbook._dependencies(db, row["artifact_id"]) != dependencies
            ):
                raise
            with (workbook.blobs / row["digest"]).open("rb") as file:
                raw = file.read(MAX_EPISODE_BYTES + 1)
            if (
                len(raw) > MAX_EPISODE_BYTES
                or hashlib.sha256(raw).hexdigest() != row["digest"]
                or _identity_digest(json.loads(raw)) != payload_hash
            ):
                raise
            artifact_id = row["artifact_id"]
            body = raw.decode("utf-8")
            payload = json.loads(body)
    from .memory_projection import project_memory_fact

    for _ in range(3):
        fact = project_memory_fact(workbook, payload, principal=session.principal)
        with workbook.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = _existing(db, session, event_id, payload_hash)
            if existing:
                return _receipt(existing, reused=True)
            if _session(db, session)["ended_at"] is not None:
                raise ValueError(
                    "Session ended during capture; episode was not published"
                )
            if (
                fact is not None
                and not db.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id=?",
                    (fact["artifact_id"],),
                ).fetchone()
            ):
                # Projection can reuse an old fact whose last owner expired
                # before this write lock. Re-project outside the transaction;
                # existence and ownership publication must share this lock.
                continue
            counters = _counters(db, session.session_id)
            db.execute(
                "INSERT INTO continuity_episodes(session_id,event_id,payload_hash,artifact_id,kind,verified,summary,payload,captured_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session.session_id,
                    event_id,
                    payload_hash,
                    artifact_id,
                    kind,
                    int(verified),
                    summary,
                    body,
                    time.time(),
                ),
            )
            if fact is not None:
                db.execute(
                    "INSERT INTO continuity_facts VALUES(?,?)",
                    (artifact_id, fact["artifact_id"]),
                )
            append_event(
                db,
                session.principal,
                "session.episode",
                "session:" + session.session_id,
                {
                    "event_id": event_id,
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "verified": verified,
                },
            )
            _count(counters, payload)
            db.execute(
                "UPDATE continuity_usage SET counters=? WHERE session_id=?",
                (_json(counters), session.session_id),
            )
            if kind == "session_end":
                db.execute(
                    "UPDATE continuity_sessions SET ended_at=? WHERE session_id=?",
                    (time.time(), session.session_id),
                )
            _touch(db, session.session_id)
            return _receipt(
                _existing(db, session, event_id, payload_hash), reused=False
            )
    raise ValueError(
        "Projected memory changed repeatedly during capture; retry with the same event_id"
    )


def capture_tool(
    session,
    tool,
    args,
    result=None,
    *,
    error=None,
    elapsed=None,
    event_id=None,
    usage=None,
    estimated_tokens=None,
):
    """Configured hooks only. Repeated reads without usage are compacted.

    Explicit event-ID retries may differ in elapsed time, but not facts/usage.
    Accounting-bearing calls remain distinct unless the host supplies a retry ID.
    """
    if not isinstance(tool, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", tool
    ):
        raise ValueError("Invalid tool name")
    if elapsed is not None and (
        type(elapsed) not in (int, float)
        or not math.isfinite(elapsed)
        or not 0 <= elapsed <= 86400
    ):
        raise ValueError("elapsed must be seconds between 0 and 86400")
    failed = error is not None or (
        isinstance(result, dict)
        and (
            result.get("isError") is True
            or bool(result.get("error"))
            or result.get("state") in ("failed", "error", "partial", "claim_lost")
        )
    )
    payload = {
        "tool": tool,
        "arguments": _arguments(args),
        "facts": _facts(result),
        "error": _text(error, 512) if error is not None else None,
        "elapsed_seconds": elapsed,
        **_accounting(usage, estimated_tokens),
    }
    if (
        event_id is None
        and tool in READ_TOOLS
        and not failed
        and usage is None
        and estimated_tokens is None
    ):
        event_id = "read-" + _identity_digest(payload)
    return _publish(
        session, "tool_result", payload, verified=not failed, event_id=event_id
    )


def capture_event(
    session, kind, data, *, event_id=None, usage=None, estimated_tokens=None
):
    """Explicit external hook. Caller-provided verified/actor/role fields are ignored."""
    if kind not in ("decision", "tool_result", "session_end") or not isinstance(
        data, dict
    ):
        raise ValueError(
            "External event requires decision, tool_result, or session_end and a JSON object"
        )
    allowed = {
        "summary",
        "decision",
        "reason",
        "text",
        "artifact_id",
        "refs",
        "state",
        "name",
    }
    facts = {key: _small(data[key]) for key in allowed if key in data}
    if kind == "tool_result":
        facts["result"] = _facts(data.get("result", {}))
        if isinstance(data.get("tool"), str):
            facts["tool"] = _text(data["tool"], 80)
        facts["arguments"] = _arguments(data.get("arguments", data.get("args", {})))
    return _publish(
        session,
        kind,
        {"facts": facts, **_accounting(usage, estimated_tokens)},
        verified=False,
        event_id=event_id,
    )


def recent(workbook, *, session_id=None, text="", limit=6):
    """Read compact published episodes; server enforces read authorization.

    This lexical shortlist complements, but does not implement, semantic recall.
    Truncated episode facts remain marked as previews and never replace sources.
    """
    if (
        type(limit) is not int
        or not 1 <= limit <= 20
        or not isinstance(text, str)
        or len(text) > 500
    ):
        raise ValueError("limit must be 1–20 and text at most 500 characters")
    if session_id is not None and (
        not isinstance(session_id, str) or not ID.fullmatch(session_id)
    ):
        raise ValueError("Invalid session_id")
    initialize(workbook)
    words = re.findall(r"[^\W_]+", text, re.UNICODE)[:12]
    query = "SELECT e.* FROM continuity_episodes e"
    conditions, args = [], []
    if words:
        query += " JOIN artifact_search ON artifact_search.artifact_id=e.artifact_id"
        conditions.append("artifact_search MATCH ?")
        args.append(" OR ".join('"' + word + '"' for word in words))
    if session_id is not None:
        conditions.append("e.session_id=?")
        args.append(session_id)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += (
        " ORDER BY "
        + ("bm25(artifact_search)," if words else "")
        + "e.sequence DESC LIMIT ?"
    )
    with workbook.connect() as db:
        rows = db.execute(query, (*args, limit)).fetchall()
    return [
        {
            "sequence": row["sequence"],
            "artifact_id": row["artifact_id"],
            "captured_at": row["captured_at"],
            "summary": row["summary"],
            "episode": json.loads(row["payload"]),
        }
        for row in rows
    ]


def session_status(workbook, session_id):
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM continuity_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown continuity session")
        counters = _counters(db, session_id)
        retained = db.execute(
            "SELECT count(*) FROM continuity_episodes WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        days = retention_policy(db)
        retired = dict(
            db.execute(
                "SELECT retired_sessions,retired_usage FROM continuity_retention WHERE singleton=1"
            ).fetchone()
        )
    result = (
        dict(row)
        | counters
        | {
            "episode_retention_days": days,
            "expired_sessions": retired["retired_sessions"],
            "expired_usage_totals": _counter_values(
                json.loads(retired["retired_usage"])
            ),
            "accounting_note": "Host-reported usage and rendered estimates are separate; neither is a savings claim.",
        }
    )
    if retained < counters["episode_count"]:
        result |= {
            "retained_episode_count": retained,
            "expired_episode_count": counters["episode_count"] - retained,
        }
    return result
