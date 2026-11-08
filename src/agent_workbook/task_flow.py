"""Strict conditional task flow and immutable approval snapshots."""

import hashlib
import json
import re
import stat
import time
from decimal import Decimal

from .query import _connection

ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")
OPERATORS = {"eq", "ne", "lt", "le", "gt", "ge", "is_null", "not_null"}


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def normalize_steps(sources, steps):
    if not isinstance(steps, list) or len(steps) > 8:
        raise ValueError("At most eight task steps are supported")
    normalized, known = [], set(sources)
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "name",
            "inputs",
            "sql",
            "parameters",
            "kind",
            "worker",
            "when",
            "approval",
        }:
            raise ValueError(
                "A step supports name, inputs, sql/worker, parameters, kind, when and approval"
            )
        name, inputs, kind = (
            step.get("name"),
            step.get("inputs"),
            step.get("kind", "sql"),
        )
        if not isinstance(name, str) or not ALIAS.fullmatch(name) or name in known:
            raise ValueError("Step names must be distinct simple aliases")
        known.add(name)
        if (
            not isinstance(inputs, list)
            or not 1 <= len(inputs) <= 8
            or any(not isinstance(value, str) for value in inputs)
            or len(set(inputs)) != len(inputs)
        ):
            raise ValueError("Each step needs 1–8 distinct input aliases")
        if kind not in ("sql", "worker"):
            raise ValueError("Step kind must be sql or worker")
        item = {"name": name, "inputs": inputs}
        if kind == "sql":
            sql = step.get("sql")
            if (
                not isinstance(sql, str)
                or not 1 <= len(sql.strip()) <= 6000
                or "worker" in step
            ):
                raise ValueError("SQL steps require bounded SQL and no worker name")
            item["sql"] = sql
        else:
            worker = step.get("worker")
            if (
                not isinstance(worker, str)
                or not ALIAS.fullmatch(worker)
                or "sql" in step
            ):
                raise ValueError(
                    "Worker steps require a configured worker name and no inline SQL/code"
                )
            item.update(kind="worker", worker=worker)
        parameters = step.get("parameters", [])
        if (
            not isinstance(parameters, list)
            or len(parameters) > 30
            or any(
                type(value) not in (str, int, float, bool, type(None))
                or isinstance(value, str)
                and len(value) > 1000
                for value in parameters
            )
        ):
            raise ValueError("Parameters must be at most 30 bounded JSON scalars")
        _json(parameters)
        item["parameters"] = parameters
        if "kind" in step:
            item["kind"] = kind
        if "approval" in step:
            if type(step["approval"]) is not bool:
                raise ValueError("approval must be boolean")
            item["approval"] = step["approval"]
        if "when" in step:
            condition = step["when"]
            if not isinstance(condition, dict) or set(condition) - {
                "step",
                "column",
                "op",
                "value",
            }:
                raise ValueError("when requires step, column, op and optional value")
            if (
                not isinstance(condition.get("step"), str)
                or not ALIAS.fullmatch(condition["step"])
                or not isinstance(condition.get("column"), str)
                or not 1 <= len(condition["column"]) <= 128
                or condition.get("op") not in OPERATORS
            ):
                raise ValueError("Invalid bounded scalar condition")
            unary = condition["op"] in ("is_null", "not_null")
            if unary != ("value" not in condition):
                raise ValueError(
                    "Null predicates take no value; other predicates require a value"
                )
            if not unary:
                value = condition["value"]
                if (
                    type(value) not in (str, int, float, bool)
                    or isinstance(value, str)
                    and len(value) > 1000
                ):
                    raise ValueError(
                        "Condition value must be a bounded non-null JSON scalar"
                    )
                _json(value)
            item["when"] = dict(condition)
        normalized.append(item)
    step_names = {step["name"] for step in normalized}
    if any(
        step["when"]["step"] not in step_names for step in normalized if "when" in step
    ):
        raise ValueError("Conditions must reference a materialized task step")
    ordered, ready, pending = [], set(sources), list(normalized)
    while pending:
        available = [
            step
            for step in pending
            if set(step["inputs"])
            | ({step["when"]["step"]} if "when" in step else set())
            <= ready
        ]
        if not available:
            raise ValueError("Task has a dependency cycle or unknown input alias")
        for step in available:
            ordered.append(step)
            ready.add(step["name"])
            pending.remove(step)
    return ordered


def condition_matches(workbook, condition, artifact_id):
    """Read exactly one scalar from an immutable materialization, never eval."""
    tables = workbook.table_inputs([artifact_id], allow_historical=True)
    if tables[0][0]["media_type"] != "application/vnd.apache.parquet":
        raise ValueError("Conditions require a materialized Parquet step")
    column = '"' + condition["column"].replace('"', '""') + '"'
    with _connection(workbook, tables) as db:
        from threading import Timer

        deadline = Timer(workbook.settings.query_timeout, db.interrupt)
        deadline.daemon = True
        deadline.start()
        try:
            rows = db.execute(f"SELECT {column} FROM input1 LIMIT 2").fetchall()
        finally:
            deadline.cancel()
            deadline.join()
    if len(rows) != 1:
        raise ValueError("A condition result must contain exactly one row")
    actual, operator = rows[0][0], condition["op"]
    if isinstance(actual, str) and len(actual) > 1000:
        raise ValueError("Condition scalar exceeds its bounded string size")
    if operator == "is_null":
        return actual is None
    if operator == "not_null":
        return actual is not None
    if actual is None:
        return False
    expected = condition["value"]
    if type(actual) in (int, float, Decimal) and type(expected) in (int, float):
        actual, expected = Decimal(str(actual)), Decimal(str(expected))
        if not actual.is_finite() or not expected.is_finite():
            raise ValueError("Conditions require finite numeric scalars")
    elif type(actual) is not type(expected) or type(actual) not in (str, bool):
        raise ValueError("Condition scalar types must match without coercion")
    if type(actual) is bool and operator not in ("eq", "ne"):
        raise ValueError("Boolean conditions support eq/ne only")
    return {
        "eq": lambda: actual == expected,
        "ne": lambda: actual != expected,
        "lt": lambda: actual < expected,
        "le": lambda: actual <= expected,
        "gt": lambda: actual > expected,
        "ge": lambda: actual >= expected,
    }[operator]()


def approval_status(workbook, db, artifact_id):
    from .coordination import approval_floor

    row = workbook._get(db, artifact_id)
    state = workbook._status(db, artifact_id)
    event = db.execute(
        "SELECT * FROM coordination_events WHERE resource=? AND event_id>? AND kind IN ('approval.accepted','approval.revoked') ORDER BY event_id DESC LIMIT 1",
        (artifact_id, approval_floor(db)),
    ).fetchone()
    valid = bool(
        event
        and event["role"] == "approver"
        and event["kind"] == "approval.accepted"
        and json.loads(event["details"]).get("sha256") == row["digest"]
        and state["is_latest"]
        and not state["stale_inputs"]
    )
    result = {"sha256": row["digest"], "approved": valid}
    if event:
        result.update(event_id=event["event_id"], approver=event["actor"])
    return result


def gate(workbook, payload, step):
    """A stable immutable approval request excludes per-attempt run identity."""
    with workbook.connect() as db:
        db.execute("BEGIN")
        ids = [
            payload["definition_id"],
            *[item["artifact_id"] for item in payload["sources"].values()],
            *[
                item["artifact_id"]
                for item in payload["steps"]
                if item.get("artifact_id")
            ],
        ]
        rows = [workbook._get(db, ref) for ref in dict.fromkeys(ids)]
    body = {
        "format": 1,
        "kind": "task_step_approval",
        "task": payload["task"],
        "step": step["name"],
        "definition_id": payload["definition_id"],
        "inputs": [
            {"artifact_id": row["artifact_id"], "sha256": row["digest"]} for row in rows
        ],
        "workers": payload.get("workers", {}),
    }
    encoded = _json(body)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    name = "_task_approvals/" + digest
    try:
        saved = workbook.save_text(
            encoded,
            name=name,
            kind="task_approval",
            media_type="application/json",
            summary="Approve exact task snapshot: "
            + payload["task"]
            + "/"
            + step["name"],
            dependencies=[row["artifact_id"] for row in rows],
        )
    except ValueError:
        saved = workbook.inspect(name)
        if (
            saved["kind"] != "task_approval"
            or saved["sha256"] != digest
            or saved["dependencies"] != [row["artifact_id"] for row in rows]
        ):
            raise ValueError("Approval snapshot has conflicting provenance") from None
    with workbook.connect() as db:
        db.execute("BEGIN")
        return saved["artifact_id"], approval_status(workbook, db, saved["artifact_id"])


def revalidate_approvals(workbook, db, payload):
    """Called under publication's write lock; revocation cannot race the commit."""
    valid = True
    for step in payload["steps"]:
        if step.get("approval_ref"):
            status = approval_status(workbook, db, step["approval_ref"])
            if not status["approved"]:
                valid = False
            step["approval"] = status
    return valid


def resumed_result(workbook, previous, inputs):
    """Reuse an explicitly resumed completed output, including volatile jobs.

    This retains the previous run's observation; it is not a cross-run cache claim.
    Changed input identities, lost lineage or corrupted output prevent reuse.
    """
    if (
        previous.get("state") != "complete"
        or previous.get("result", {}).get("inputs") != inputs
    ):
        return None
    with workbook.connect() as db:
        db.execute("BEGIN")
        row = workbook._get(db, previous["artifact_id"])
        if row[
            "media_type"
        ] != "application/vnd.apache.parquet" or workbook._dependencies(
            db, row["artifact_id"]
        ) != list(dict.fromkeys(inputs)):
            raise ValueError("Resumed materialization has conflicting lineage")
        saved = workbook._summary(db, row)
    path = workbook.blobs / row["digest"]
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size != row["size_bytes"]
        or info.st_size > 64 * 1024**2
    ):
        raise ValueError("Resumed materialization exceeds its integrity bound")
    digest, deadline = hashlib.sha256(), time.monotonic() + 15
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            if time.monotonic() > deadline:
                raise ValueError("Resumed materialization integrity deadline exceeded")
    if digest.hexdigest() != row["digest"]:
        raise ValueError("Resumed materialization integrity check failed")
    return json.loads(_json(previous["result"])) | {
        "saved": saved,
        "cache": {
            "hit": True,
            "key": previous["cache"]["key"],
            "reason": "resumed_materialization",
        },
    }
