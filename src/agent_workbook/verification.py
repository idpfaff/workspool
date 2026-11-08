"""Read-only freshness checks over catalog provenance, without rerunning work.

Pages inspect latest published task heads and artifact.created checkpoints.
Each page has one catalog snapshot; continuation rejects relevant mutations
instead of silently combining generations. Approval is exact-version evidence,
and is never reported current after its artifact or inputs become stale.
"""

import base64
import hashlib
import json

from .coordination import approval_floor, authorize_capability, current_principal

LINEAGE_LIMIT = 128
CHANGE_LIMIT = 8


def _table(db, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _revision(db):
    # Deletes change the artifact count; creations and publication/approval
    # changes have atomic audit events. Unrelated claim renewals do not abort pages.
    return [
        db.execute("SELECT count(*) FROM artifacts").fetchone()[0],
        db.execute(
            "SELECT COALESCE(MAX(event_id),0) FROM coordination_events WHERE kind IN ('artifact.created','task.published','task.finished','workbook.restored') OR kind LIKE 'approval.%'"
        ).fetchone()[0],
    ]


def _freshness(workbook, db, ref):
    try:
        root = workbook._get(db, ref)
    except ValueError:
        return {
            "ref": ref,
            "exists": False,
            "current": False,
            "changed_sources": [{"artifact_id": ref, "reason": "artifact_missing"}],
            "lineage_complete": True,
            "current_approved": False,
            "action": "Locate a current source or rerun its producing task",
        }
    rows = db.execute(
        """WITH RECURSIVE lineage(id) AS (
            SELECT ? UNION SELECT d.dependency_id FROM dependencies d
            JOIN lineage l ON d.artifact_id=l.id LIMIT ?
        ) SELECT a.artifact_id,a.name,a.version,a.digest,
            cur.artifact_id AS current_artifact_id,cur.version AS current_version
        FROM lineage l JOIN artifacts a ON a.artifact_id=l.id
        LEFT JOIN latest cur ON cur.name=a.name
        ORDER BY a.name,a.version""",
        (root["artifact_id"], LINEAGE_LIMIT + 1),
    ).fetchall()
    complete = len(rows) <= LINEAGE_LIMIT
    changed = [
        {
            key: row[key]
            for key in (
                "artifact_id",
                "name",
                "version",
                "current_artifact_id",
                "current_version",
            )
        }
        | {
            "reason": "artifact_superseded"
            if row["artifact_id"] == root["artifact_id"]
            else "dependency_superseded"
        }
        for row in rows[:LINEAGE_LIMIT]
        if row["artifact_id"] != row["current_artifact_id"]
    ]
    current = False if changed else True if complete else None
    approved = db.execute(
        "SELECT event_id,actor,occurred_at,kind,json_extract(details,'$.sha256') AS sha256 "
        "FROM coordination_events WHERE kind IN ('approval.accepted','approval.revoked') "
        "AND role='approver' AND resource=? AND json_extract(details,'$.sha256')=? "
        "ORDER BY event_id DESC LIMIT 1",
        (root["artifact_id"], root["digest"]),
    ).fetchone()
    exact_approval = (
        dict(approved) if approved and approved["sha256"] == root["digest"] else None
    )
    accepted = bool(exact_approval and exact_approval["kind"] == "approval.accepted")
    reconfirm = bool(accepted and exact_approval["event_id"] <= approval_floor(db))
    result = {
        "ref": ref,
        "exists": True,
        "artifact_id": root["artifact_id"],
        "name": root["name"],
        "version": root["version"],
        "sha256": root["digest"],
        "current": current,
        "changed_sources": changed[:CHANGE_LIMIT],
        "changed_sources_truncated": len(changed) > CHANGE_LIMIT,
        "lineage_complete": complete,
        "current_approved": bool(current is True and accepted and not reconfirm),
        "exact_version_approval": exact_approval,
        "approval_requires_reconfirmation": reconfirm,
    }
    if current is False:
        result["action"] = (
            "Update or rerun the producer using current sources, then review the new result"
        )
    elif current is None or len(changed) > CHANGE_LIMIT:
        result["action"] = (
            "Inspect bounded provenance before deciding; lineage coverage is incomplete"
        )
    elif reconfirm:
        result["action"] = (
            "Review and approve again in this restored workspace; historical approval is not current authority"
        )
    elif exact_approval and not accepted:
        result["action"] = (
            "Approval was revoked; review this artifact before recording any new approval"
        )
    elif exact_approval is None:
        result["action"] = (
            "Review this current artifact before approve; approval requires the approver capability"
        )
    else:
        result["action"] = (
            "Current exact-version approval is recorded; no factual validation is inferred"
        )
    return result


def _task(workbook, db, row, after, through):
    source_data = json.loads(row["sources"] or "{}")
    roots = [("@definition", row["definition_id"])] + [
        (alias, value["artifact_id"])
        for alias, value in source_data.items()
        if isinstance(value, dict) and isinstance(value.get("artifact_id"), str)
    ]
    reasons, coverage, states = [], True, []
    for alias, ref in roots[:9]:
        item = _freshness(workbook, db, ref)
        states.append(item["current"])
        coverage = (
            coverage
            and item["lineage_complete"]
            and not item.get("changed_sources_truncated", False)
        )
        reasons.extend(
            change | {"source_alias": alias} for change in item["changed_sources"]
        )
    fresh = False if False in states else None if None in states else True
    changed_since = False
    for reason in reasons:
        current_id = reason.get("current_artifact_id")
        if (
            current_id
            and db.execute(
                "SELECT 1 FROM coordination_events WHERE kind='artifact.created' AND resource=? AND event_id>? AND event_id<=? LIMIT 1",
                (current_id, after, through),
            ).fetchone()
        ):
            changed_since = True
    report = (
        _freshness(workbook, db, row["report_id"])
        if row["report_id"]
        else {"exists": False, "current_approved": False}
    )
    result = {
        "task": row["name"],
        "run_id": row["run_id"],
        "definition_id": row["definition_id"],
        "result_artifact_id": row["report_id"],
        "result_available": report["exists"],
        "result_current": report.get("current"),
        "current": fresh,
        "affected_since_checkpoint": changed_since
        if changed_since
        or (
            coverage
            and all(change["reason"] != "artifact_missing" for change in reasons)
        )
        else None,
        "changed_sources": reasons[:CHANGE_LIMIT],
        "changes_complete": coverage and len(reasons) <= CHANGE_LIMIT,
        "current_approved": fresh is True and report["current_approved"],
        "exact_version_approval": report.get("exact_version_approval"),
        "approval_requires_reconfirmation": report.get(
            "approval_requires_reconfirmation", False
        ),
    }
    if fresh is None:
        result["suggestion"] = {
            "tool": "provenance",
            "arguments": {"ref": row["definition_id"]},
            "note": "Lineage coverage is incomplete; inspect before deciding",
        }
    elif not fresh or not report["exists"] or report.get("current") is False:
        result["suggestion"] = {
            "tool": "task_run",
            "arguments": {"name": row["name"]},
            "note": "Rerun with current sources; update deliberately pinned source bindings first and acquire a claim if required",
        }
    elif report.get("current") is None:
        result["suggestion"] = {
            "tool": "provenance",
            "arguments": {"ref": row["report_id"]},
            "note": "Result lineage coverage is incomplete; inspect before approval",
        }
    elif not result["current_approved"]:
        result["suggestion"] = {
            "tool": "approve",
            "arguments": {"ref": row["report_id"]},
            "note": "Review the current result first; an approver must record any approval",
        }
    return result


def _cursor(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid verification cursor")
    try:
        parsed = json.loads(
            base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        )
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid verification cursor") from None
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"v", "scope", "revision", "event", "task", "through"}
        or type(parsed["v"]) is not int
        or parsed["v"] != 1
        or not isinstance(parsed["task"], str)
        or len(parsed["task"]) > 64
        or any(
            type(parsed[key]) is not int or parsed[key] < 0
            for key in ("event", "through")
        )
    ):
        raise ValueError("Invalid verification cursor")
    return parsed


def verify(workbook, refs=None, *, after=0, through=None, cursor=None, limit=20):
    """Inspect current published heads and source changes; never approve or run.

    Reuse cursor with identical refs/after/through until complete, then persist
    next_after. Changed source/task/approval state requires a fresh scan. Read
    budgets are explicit; incomplete lineage is never represented as current.
    """
    authorize_capability(current_principal(), "read")
    if refs is not None and (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= 16
        or any(not isinstance(ref, str) or not 1 <= len(ref) <= 256 for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError("refs must contain 1–16 distinct artifact references")
    if (
        type(after) is not int
        or after < 0
        or type(limit) is not int
        or not 1 <= limit <= 50
    ):
        raise ValueError("after must be nonnegative and limit 1–50")
    if through is not None and (type(through) is not int or through < after):
        raise ValueError("through must be an integer at least after")
    continuation = _cursor(cursor) if cursor is not None else None
    with workbook.connect() as db:
        db.execute("BEGIN")
        head = db.execute(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='coordination_events'),0)"
        ).fetchone()[0]
        if after > head or (through is not None and through > head):
            raise ValueError("Checkpoint exceeds this workbook's event history")
        through = (
            continuation["through"]
            if through is None and continuation
            else head
            if through is None
            else through
        )
        if not after <= through <= head:
            raise ValueError("Checkpoint exceeds this workbook's event history")
        scope = hashlib.sha256(
            json.dumps(
                [str(workbook.catalog), refs, after, through], separators=(",", ":")
            ).encode()
        ).hexdigest()[:32]
        revision = _revision(db)
        if continuation and (
            continuation["scope"] != scope
            or continuation["revision"] != revision
            or not after <= continuation["event"] <= through
        ):
            raise ValueError(
                "Verification scope or source snapshot changed; restart without cursor"
            )
        event_after = continuation["event"] if continuation else after
        task_after = continuation["task"] if continuation else ""
        events = db.execute(
            "SELECT event_id,resource,json_extract(details,'$.name') AS name,json_extract(details,'$.version') AS version FROM coordination_events WHERE kind='artifact.created' AND event_id>? AND event_id<=? ORDER BY event_id LIMIT ?",
            (event_after, through, limit + 1),
        ).fetchall()
        tasks = []
        if _table(db, "task_heads"):
            tasks = db.execute(
                "SELECT h.name,r.run_id,r.definition_id,json_extract(r.payload,'$.sources') AS sources,json_extract(r.payload,'$.artifact_id') AS report_id FROM task_heads h JOIN task_runs r ON r.run_id=h.latest_success WHERE h.name>? ORDER BY h.name LIMIT ?",
                (task_after, limit + 1),
            ).fetchall()
        checks = [_freshness(workbook, db, ref) for ref in refs or []]
        published = [_task(workbook, db, row, after, through) for row in tasks[:limit]]
        history = db.execute(
            "SELECT through_event_id FROM coordination_retention WHERE singleton=1"
        ).fetchone()[0]
        complete = len(events) <= limit and len(tasks) <= limit
        next_cursor = None
        if not complete:
            value = {
                "v": 1,
                "scope": scope,
                "revision": revision,
                "through": through,
                "event": events[min(len(events), limit) - 1]["event_id"]
                if events
                else through,
                "task": tasks[min(len(tasks), limit) - 1]["name"]
                if tasks
                else task_after,
            }
            next_cursor = (
                base64.urlsafe_b64encode(
                    json.dumps(value, separators=(",", ":")).encode()
                )
                .decode()
                .rstrip("=")
            )
    return {
        "sources": checks,
        "published_tasks": published,
        "changed_artifacts": [dict(row) for row in events[:limit]],
        "after": after,
        "through": through,
        "complete": complete,
        "next_after": through if complete else after,
        "cursor": next_cursor,
        "history_pruned": after < history,
        "pruned_through": history,
        "scope": "Current catalog metadata, latest published task heads, and artifact.created events; no source reread or factual verification",
        "coverage": "Exact listed differences; bounded lineage and paginated task/event scans are explicit",
    }
