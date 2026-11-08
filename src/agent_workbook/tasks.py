"""Durable task definitions and coherent, recoverable execution snapshots.

A run pins its definition and sources before doing work. Results are reusable
materializations with exact lineage; a late, failed or superseded attempt cannot
replace newer successful context. Every brief is self-contained by default.
"""

import hashlib
import json
import re
import time
from dataclasses import asdict
from functools import wraps
from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .store import TABLE_TYPES, is_text


def _as_actor(function):
    parameters = signature(function)

    @wraps(function)
    def execute(*args, **kwargs):
        from .coordination import acting_as, current_principal

        principal = (
            parameters.bind(*args, **kwargs).arguments.get("principal")
            or current_principal()
        )
        with acting_as(principal):
            return function(*args, **kwargs)

    return execute


ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS task_heads (
    name TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0,
    success_generation INTEGER NOT NULL DEFAULT 0,
    latest_success TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    generation INTEGER NOT NULL,
    definition_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    started REAL NOT NULL,
    finished REAL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(name, generation)
);
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _initialize(workbook):
    with workbook.connect() as db:
        db.executescript(SCHEMA)


def _name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Task name must contain 1–64 characters")
    if name != name.strip() or name.startswith("wb_") or "\x00" in name:
        raise ValueError("Invalid task name")
    return "_tasks/" + name + "/definition"


def _validate(objective, sources, steps, searches, csv_types=None):
    if not isinstance(objective, str) or not 1 <= len(objective.strip()) <= 360:
        raise ValueError("Task objective must contain 1–360 characters")
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 8:
        raise ValueError("Provide 1–8 named sources")
    for alias, ref in sources.items():
        if (
            not isinstance(alias, str)
            or not ALIAS.fullmatch(alias)
            or not isinstance(ref, str)
            or not ref
        ):
            raise ValueError("Sources require simple aliases and artifact references")
    if csv_types is not None and (
        not isinstance(csv_types, dict)
        or any(alias not in sources for alias in csv_types)
        or any(
            not isinstance(columns, dict)
            or not 1 <= len(columns) <= 256
            or any(
                not isinstance(column, str)
                or not isinstance(kind, str)
                or not 1 <= len(column) <= 256
                or not 1 <= len(kind) <= 128
                for column, kind in columns.items()
            )
            for columns in csv_types.values()
        )
    ):
        raise ValueError("CSV types must map source aliases to column/type mappings")
    from .task_flow import normalize_steps

    ordered = normalize_steps(sources, steps)
    searches = [] if searches is None else searches
    if (
        not isinstance(searches, list)
        or len(searches) > 8
        or any(
            not isinstance(q, str) or not 1 <= len(q.strip()) <= 500 for q in searches
        )
    ):
        raise ValueError("Provide at most eight nonempty evidence searches")
    plan = {
        "format": 2
        if any(set(step) & {"kind", "when", "approval"} for step in ordered)
        else 1,
        "objective": objective.strip(),
        "sources": sources,
        "steps": ordered,
        "searches": searches,
    }
    if csv_types:
        plan["csv_types"] = csv_types
    if len(_json(plan).encode()) > 32768:
        raise ValueError("Task definition exceeds 32 KiB")
    return plan


@_as_actor
def define_task(
    workbook,
    name,
    objective,
    sources,
    steps,
    searches=None,
    expected_version=None,
    csv_types=None,
    pin_sources=False,
    principal=None,
    provenance_refs=None,
):
    """Save a versioned plan; this does not execute queries or acquire sources."""
    from .coordination import authorize, current_principal, record_event

    principal = principal or current_principal()
    authorize(principal, "task:define")
    if provenance_refs is not None and (
        not isinstance(provenance_refs, list)
        or len(provenance_refs) > 8
        or any(not isinstance(ref, str) or not ref for ref in provenance_refs)
    ):
        raise ValueError("Task provenance requires at most eight artifact references")
    artifact_name = _name(name)
    plan = _validate(objective, sources, steps, searches, csv_types)
    if type(pin_sources) is not bool:
        raise ValueError("pin_sources must be boolean")
    with workbook.connect() as db:
        db.execute("BEGIN")
        resolved = {alias: workbook._get(db, ref) for alias, ref in sources.items()}
        if plan["searches"] and not any(
            is_text(row["media_type"]) and row["media_type"] not in TABLE_TYPES
            for row in resolved.values()
        ):
            raise ValueError(
                "Evidence searches require a text source in sources; add the policy or document alias"
            )
        for alias in plan.get("csv_types", {}):
            if resolved[alias]["media_type"] != "text/csv":
                raise ValueError("CSV type overrides require a CSV source")
        plan["sources"] = {
            alias: row["artifact_id"] if pin_sources else row["name"]
            for alias, row in resolved.items()
        }
    plan = _validate(
        plan["objective"],
        plan["sources"],
        plan["steps"],
        plan["searches"],
        plan.get("csv_types"),
    )
    _initialize(workbook)
    saved = workbook.save_text(
        _json(plan),
        name=artifact_name,
        kind="task",
        media_type="application/json",
        summary=plan["objective"],
        expected_version=expected_version,
        dependencies=list(dict.fromkeys(plan["sources"].values()))
        if pin_sources
        else None,
        provenance=provenance_refs,
    )
    record_event(
        workbook,
        principal,
        "task.defined",
        saved["artifact_id"],
        {
            "task": name,
            "version": saved["version"],
            "sources": plan["sources"],
        },
    )
    return {
        "name": name,
        "artifact_id": saved["artifact_id"],
        "definition_id": saved["artifact_id"],
        "version": saved["version"],
        "steps": [s["name"] for s in plan["steps"]],
        "sources": plan["sources"],
        "source_mode": "pinned" if pin_sources else "latest",
    }


def _record(workbook, db, row):
    return (
        {key: row[key] for key in ("artifact_id", "name", "version", "media_type")}
        | {"sha256": row["digest"]}
        | workbook._status(db, row["artifact_id"])
    )


def _read_plan(workbook, row):
    if row["size_bytes"] > 32768 or row["kind"] != "task":
        raise ValueError("Task definition is invalid or oversized")
    with (workbook.blobs / row["digest"]).open("rb") as stream:
        data = stream.read(32769)
    if (
        len(data) > 32768
        or hashlib.sha256(data).hexdigest() != row["digest"]
        or row["kind"] != "task"
    ):
        raise ValueError("Task definition is invalid or corrupted")
    plan = json.loads(data)
    if (
        not isinstance(plan, dict)
        or plan.get("format") not in (1, 2)
        or set(plan) - {"csv_types"}
        != {"format", "objective", "sources", "steps", "searches"}
    ):
        raise ValueError("Unsupported or malformed task definition")
    return _validate(
        plan["objective"],
        plan["sources"],
        plan["steps"],
        plan["searches"],
        plan.get("csv_types"),
    )


def _pin(workbook, name, resume_from):
    with workbook.connect() as db:
        db.execute("BEGIN")
        if resume_from:
            previous = db.execute(
                "SELECT * FROM task_runs WHERE run_id=? AND name=?", (resume_from, name)
            ).fetchone()
            if previous is None:
                raise ValueError("Unknown run for this task")
            payload = json.loads(previous["payload"])
            definition = workbook._get(db, previous["definition_id"])
            pinned_workers = payload.get("workers", {})
            sources = {
                alias: _record(workbook, db, workbook._get(db, item["artifact_id"]))
                for alias, item in payload["sources"].items()
            }
        else:
            pinned_workers = None
            definition = workbook._get(db, _name(name))
            plan = _read_plan(workbook, definition)
            sources = {
                alias: _record(workbook, db, workbook._get(db, ref))
                for alias, ref in plan["sources"].items()
            }
        plan = _read_plan(workbook, definition)
        definition_id = definition["artifact_id"]
    run_id = "run_" + uuid4().hex
    # Existing dependency protection now prevents prune/delete racing the run.
    anchor = workbook.save_text(
        _json(
            {
                "task": name,
                "run_id": run_id,
                "definition_id": definition_id,
                "sources": sources,
            }
        ),
        name="_task_inputs/" + run_id,
        kind="task_inputs",
        media_type="application/json",
        dependencies=list(
            dict.fromkeys(
                [definition_id, *[s["artifact_id"] for s in sources.values()]]
            )
        ),
    )
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO task_heads(name) VALUES(?)", (name,))
        db.execute(
            "UPDATE task_heads SET generation=generation+1 WHERE name=?", (name,)
        )
        generation = db.execute(
            "SELECT generation FROM task_heads WHERE name=?", (name,)
        ).fetchone()[0]
        payload = {
            "run_id": run_id,
            "task": name,
            "generation": generation,
            "definition_id": definition_id,
            "objective": plan["objective"],
            "sources": sources,
            "steps": [],
            "citations": [],
            "omitted": [],
            "state": "running",
            "resume_from": resume_from,
        }
        if pinned_workers is not None:
            payload["workers"] = pinned_workers
        db.execute(
            "INSERT INTO task_runs VALUES(?,?,?,?,?,?,NULL,?,?)",
            (
                run_id,
                name,
                generation,
                definition_id,
                anchor["artifact_id"],
                time.time(),
                "running",
                _json(payload),
            ),
        )
    return plan, payload, anchor["artifact_id"]


def _progress(workbook, payload):
    with workbook.connect() as db:
        db.execute(
            "UPDATE task_runs SET payload=? WHERE run_id=? AND state='running'",
            (_json(payload), payload["run_id"]),
        )


def _execute_step(workbook, step, inputs, force):
    """Materialize through the bounded cache, then retain exact versioned lineage."""
    from .query_cache import CacheInput, CacheSpec, QueryCache

    tables = workbook.table_inputs(inputs, allow_historical=True)
    spec = CacheSpec(
        sql=step["sql"],
        parameters=step["parameters"],
        inputs=[
            CacheInput(
                artifact_id=item["artifact_id"],
                digest=path.name,
                path=path,
                media_type=item["media_type"],
                csv_types=step.get("_csv_types", {}).get(index),
            )
            for index, (item, path) in enumerate(tables)
        ],
        settings=asdict(workbook.settings),
        preview_rows=5,
        materialize=True,
        max_rows=1_000_000,
        allow_historical=True,
    )
    with TemporaryDirectory(dir=workbook.root) as scratch:
        cached = QueryCache(workbook.root).execute(
            spec, destination=Path(scratch) / "result.parquet", force=force
        )
        materialized = Path(cached.path)
        digest = hashlib.sha256()
        with materialized.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        content_hash = digest.hexdigest()
        result_name = "_task_results/" + cached.key[:32] + "/" + content_hash[:16]
        try:
            saved = workbook.inspect(result_name)
        except ValueError:
            saved = None
        if saved is not None and (
            saved["sha256"] != content_hash or saved["dependencies"] != inputs
        ):
            raise ValueError("Retained task result has conflicting provenance")
        if saved is None:
            try:
                saved = workbook.save_file(
                    materialized,
                    name=result_name,
                    kind="table",
                    media_type="application/vnd.apache.parquet",
                    dependencies=inputs,
                    summary="Materialized task step: " + step["name"],
                )
            except ValueError:
                # Another run may have promoted the same fenced cache result.
                saved = workbook.inspect(result_name)
                if saved["sha256"] != content_hash or saved["dependencies"] != inputs:
                    raise ValueError(
                        "Concurrent task result has conflicting provenance"
                    ) from None
        return dict(cached.payload) | {
            "inputs": inputs,
            "saved": saved,
            "materialized_type_conversions": cached.materialized_type_conversions,
            "cache": {"hit": cached.hit, "key": cached.key, "reason": cached.reason},
        }


def _current(workbook, db, payload):
    try:
        definition = workbook._get(db, _name(payload["task"]))
        if definition["artifact_id"] != payload["definition_id"]:
            return False
        for item in payload["sources"].values():
            row = workbook._get(db, item["artifact_id"])
            state = workbook._status(db, row["artifact_id"])
            if not state["is_latest"] or state["stale_inputs"]:
                return False
        return True
    except ValueError:
        return False


def _evidence(workbook, plan, payload):
    from .context_index import ranked_search

    refs = list(
        dict.fromkeys(
            item["artifact_id"]
            for item in payload["sources"].values()
            if is_text(item["media_type"]) and item["media_type"] not in TABLE_TYPES
        )
    )
    queries = plan["searches"] or ([plan["objective"]] if refs else [])
    found = {}
    for index, text in enumerate(queries):
        if not refs:
            payload["omitted"].append({"search": index, "reason": "no_text_sources"})
            continue
        result = ranked_search(workbook, text, refs, limit=6, max_bytes=16384)
        if not result["hits"]:
            payload["omitted"].append(
                {"search": index, "reason": "no_matching_evidence"}
            )
        if not result["index_complete"]:
            payload["omitted"].append(
                {
                    "search": index,
                    "reason": "index_incomplete",
                    "pending_versions": result["pending_versions"],
                }
            )
        if result["truncated"]:
            payload["omitted"].append({"search": index, "reason": "ranked_limit"})
        for hit in result["hits"]:
            if hit["chunk_id"] not in found:
                found[hit["chunk_id"]] = dict(hit) | {"searches": []}
            found[hit["chunk_id"]]["searches"].append(index)
    payload["citations"] = list(found.values())


def _finish(workbook, payload, anchor_id, principal, claim_ticket):
    from .coordination import ClaimLost, append_event, validate_claim

    failures = {
        "no_text_sources",
        "no_matching_evidence",
        "index_incomplete",
        "evidence_failed",
    }
    successful = all(
        step["state"] in {"complete", "skipped"} for step in payload["steps"]
    ) and not any(item["reason"] in failures for item in payload["omitted"])
    waiting = any(
        step["state"] == "awaiting_approval"
        or step.get("reason") == "approval_dependency"
        for step in payload["steps"]
    )
    payload["state"] = (
        "complete" if successful else "awaiting_approval" if waiting else "partial"
    )
    snapshot = payload | {
        "record_type": "computation_snapshot",
        "state": "computed" if successful else "partial",
        "status_lookup": {
            "tool": "task_status",
            "name": payload["task"],
            "run_id": payload["run_id"],
        },
    }
    report = workbook.save_text(
        _json(snapshot),
        name="_task_runs/" + payload["run_id"],
        kind="task_run",
        media_type="application/json",
        summary=payload["objective"],
        dependencies=[
            anchor_id,
            *[
                step["artifact_id"]
                for step in payload["steps"]
                if step.get("artifact_id")
            ],
        ],
        provenance=[
            step["approval_ref"]
            for step in payload["steps"]
            if step.get("approval_ref")
        ],
    )
    payload["artifact_id"] = report["artifact_id"]
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        payload["current"] = _current(workbook, db, payload)
        head = db.execute(
            "SELECT success_generation FROM task_heads WHERE name=?", (payload["task"],)
        ).fetchone()
        if head["success_generation"] > payload["generation"]:
            payload["current"] = False
        from .task_flow import revalidate_approvals

        if not revalidate_approvals(workbook, db, payload):
            successful = False
            payload["state"] = "awaiting_approval"
            if not waiting:
                payload["omitted"].append({"reason": "approval_changed"})
        try:
            validate_claim(
                db,
                principal,
                claim_ticket,
                resource="task:" + payload["task"],
                required=False,
            )
        except ClaimLost:
            successful = False
            payload["state"] = "partial"
            payload["omitted"].append({"reason": "claim_lost"})
        if successful and not payload["current"]:
            payload["state"] = "superseded"
        if successful and payload["current"]:
            db.execute(
                "UPDATE task_heads SET latest_success=?, success_generation=? WHERE name=? AND success_generation < ?",
                (
                    payload["run_id"],
                    payload["generation"],
                    payload["task"],
                    payload["generation"],
                ),
            )
        payload["latest_success"] = db.execute(
            "SELECT latest_success FROM task_heads WHERE name=?", (payload["task"],)
        ).fetchone()[0]
        db.execute(
            "UPDATE task_runs SET state=?,payload=?,finished=? WHERE run_id=? AND state='running'",
            (payload["state"], _json(payload), time.time(), payload["run_id"]),
        )
        append_event(
            db,
            principal,
            "task.published" if successful and payload["current"] else "task.finished",
            "task:" + payload["task"],
            {
                "run_id": payload["run_id"],
                "artifact_id": report["artifact_id"],
                "definition_id": payload["definition_id"],
                "state": payload["state"],
                "current": payload["current"],
                "fence": claim_ticket["fence"] if claim_ticket else None,
            },
        )
        append_event(
            db,
            principal,
            "artifact.publication",
            report["artifact_id"],
            {
                "run_id": payload["run_id"],
                "kind": "computation_snapshot",
                "state": payload["state"],
                "inputs": [s["artifact_id"] for s in payload["sources"].values()],
            },
        )
    return payload


def _brief(payload, max_bytes, fits=None):
    if type(max_bytes) is not int or not 256 <= max_bytes <= 131072:
        raise ValueError("Brief max_bytes must be between 256 and 131072")
    result = json.loads(_json(payload))
    result["brief_complete"] = not result["omitted"] and result["state"] == "complete"

    def accepted():
        return len(_json(result).encode()) <= max_bytes and (
            fits is None or fits(result)
        )

    # Keep explicit provenance and status. Drop whole evidence units, never facts
    # from inside a row or excerpt while pretending the original is complete.
    while not accepted() and result["citations"]:
        dropped = result["citations"].pop()
        result["omitted"].append(
            {"chunk_id": dropped["chunk_id"], "reason": "brief_budget"}
        )
        result["brief_complete"] = False
    for step in reversed(result["steps"]):
        if accepted():
            break
        if "result" in step:
            del step["result"]
            result["omitted"].append(
                {
                    "step": step["name"],
                    "reason": "brief_budget",
                    "artifact_id": step.get("artifact_id"),
                }
            )
            result["brief_complete"] = False
    if not accepted():
        raise ValueError(
            "Brief budget cannot hold required run status and provenance; increase the budget"
        )
    return result


def _deliver(workbook, payload, max_bytes, fits, base_run):
    """Suppress repeated content only after an explicit caller retention claim."""
    if type(max_bytes) is not int or not 256 <= max_bytes <= 131072:
        raise ValueError("Brief max_bytes must be between 256 and 131072")
    if base_run is not None:
        if not isinstance(base_run, str) or len(base_run) > 64:
            raise ValueError("base_run must be a retained run ID")
        with workbook.connect() as db:
            previous = db.execute(
                "SELECT payload FROM task_runs WHERE run_id=? AND name=?",
                (base_run, payload["task"]),
            ).fetchone()

        def content(value):
            return {
                "definition": value["definition_id"],
                "sources": value["sources"],
                "steps": [
                    {
                        k: v
                        for k, v in step.items()
                        if k not in {"cache", "elapsed_seconds"}
                    }
                    for step in value["steps"]
                ],
                "citations": [
                    {k: v for k, v in hit.items() if k != "score"}
                    for hit in value["citations"]
                ],
                "omitted": value["omitted"],
            }

        base = json.loads(previous["payload"]) if previous else None
        if (
            base is not None
            and base["state"] == "complete"
            and payload["state"] == "complete"
            and payload.get("current")
            and content(base) == content(payload)
        ):
            receipt = {
                key: payload[key]
                for key in (
                    "task",
                    "run_id",
                    "generation",
                    "definition_id",
                    "state",
                    "current",
                    "latest_success",
                    "artifact_id",
                )
            }
            receipt.update(
                {
                    "delivery": "unchanged",
                    "base_run": base_run,
                    "requires_retained_base": True,
                    "brief_complete": False,
                    "omitted": payload["omitted"],
                    "producer": payload.get("producer"),
                }
            )
            if len(_json(receipt).encode()) <= max_bytes and (
                fits is None or fits(receipt)
            ):
                return receipt
    return _brief(payload, max_bytes, fits)


@_as_actor
def run_task(
    workbook,
    name,
    resume_from=None,
    force=False,
    max_bytes=16384,
    fits=None,
    base_run=None,
    principal=None,
    claim_ticket=None,
    workers=None,
):
    from .coordination import authorize, current_principal, initialize, validate_claim

    _name(name)
    principal = principal or current_principal()
    authorize(principal, "task:run")
    initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        validate_claim(
            db, principal, claim_ticket, resource="task:" + name, required=False
        )
    _initialize(workbook)
    if type(force) is not bool:
        raise ValueError("force must be boolean")
    if base_run is not None and (not isinstance(base_run, str) or len(base_run) > 64):
        raise ValueError("base_run must be a retained run ID")
    if type(max_bytes) is not int or not 256 <= max_bytes <= 131072:
        raise ValueError("Brief max_bytes must be between 256 and 131072")
    plan, payload, anchor_id = _pin(workbook, name, resume_from)
    payload["producer"] = {"identity": principal.identity, "role": principal.role}
    worker_steps = [step for step in plan["steps"] if step.get("kind") == "worker"]
    if worker_steps:
        try:
            authorize(principal, "workers")
            from .task_workers import WorkerRegistry

            if workers is None or not isinstance(workers, WorkerRegistry):
                raise ValueError(
                    "This task requires a host-configured local worker registry"
                )
            identities = {
                step["worker"]: workers.resolve(step["worker"]).digest
                for step in worker_steps
            }
            if resume_from and payload.get("workers") != identities:
                raise ValueError(
                    "Configured workers changed from the pinned run; start a fresh run"
                )
            payload["workers"] = identities
        except Exception as error:
            payload["steps"] = [
                {
                    "name": step["name"],
                    "state": "blocked",
                    "reason": "worker_configuration",
                    "elapsed_seconds": 0.0,
                }
                for step in plan["steps"]
            ]
            payload["omitted"].append(
                {"reason": "worker_configuration", "error": str(error)[:500]}
            )
            completed = _finish(workbook, payload, anchor_id, principal, claim_ticket)
            if isinstance(error, PermissionError):
                raise
            return _deliver(workbook, completed, max_bytes, fits, base_run)
    _progress(workbook, payload)
    try:
        if base_run is None:
            _brief(payload, max_bytes, fits)
    except ValueError:
        payload["omitted"].append({"reason": "brief_budget"})
        payload["state"] = "partial"
        with workbook.connect() as db:
            db.execute(
                "UPDATE task_runs SET state='partial', payload=?, finished=? WHERE run_id=?",
                (_json(payload), time.time(), payload["run_id"]),
            )
        raise ValueError(
            "Brief budget cannot hold required provenance; retry with a larger budget"
        ) from None
    aliases = {alias: item["artifact_id"] for alias, item in payload["sources"].items()}
    resume_steps = {}
    if resume_from and not force:
        with workbook.connect() as db:
            previous = db.execute(
                "SELECT payload FROM task_runs WHERE run_id=? AND name=?",
                (resume_from, name),
            ).fetchone()
        resume_steps = {
            step["name"]: step for step in json.loads(previous["payload"])["steps"]
        }
    for step in plan["steps"]:
        started = time.monotonic()
        required = [
            *step["inputs"],
            *([step["when"]["step"]] if "when" in step else []),
        ]
        if any(alias not in aliases for alias in required):
            missing = [
                entry
                for entry in payload["steps"]
                if entry["name"] in required and entry["name"] not in aliases
            ]
            waiting = any(
                entry["state"] == "awaiting_approval"
                or entry.get("reason") == "approval_dependency"
                for entry in missing
            )
            skipped = bool(missing) and all(
                entry["state"] == "skipped" for entry in missing
            )
            reason = (
                "approval_dependency"
                if waiting
                else "dependency_skipped"
                if skipped
                else "failed_dependency"
            )
            payload["steps"].append(
                {
                    "name": step["name"],
                    "state": "skipped" if skipped else "blocked",
                    "reason": reason,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            if not skipped:
                payload["omitted"].append({"step": step["name"], "reason": reason})
            _progress(workbook, payload)
            continue
        flow = {}
        try:
            from .task_flow import (
                approval_status,
                condition_matches,
                gate,
                resumed_result,
            )

            with workbook.connect() as db:
                db.execute("BEGIN")
                revoked = any(
                    not approval_status(workbook, db, prior["approval_ref"])["approved"]
                    for prior in payload["steps"]
                    if prior.get("approval_ref") and prior["state"] == "complete"
                )
            if revoked:
                payload["steps"].append(
                    {
                        "name": step["name"],
                        "state": "blocked",
                        "reason": "approval_dependency",
                        "elapsed_seconds": time.monotonic() - started,
                    }
                )
                payload["omitted"].append(
                    {"step": step["name"], "reason": "approval_dependency"}
                )
                _progress(workbook, payload)
                continue
            if "when" in step:
                condition_ref = aliases[step["when"]["step"]]
                matched = condition_matches(workbook, step["when"], condition_ref)
                flow = {"condition_ref": condition_ref, "condition_matched": matched}
                if not matched:
                    payload["steps"].append(
                        {
                            "name": step["name"],
                            "state": "skipped",
                            "reason": "condition_false",
                            "elapsed_seconds": time.monotonic() - started,
                            **flow,
                        }
                    )
                    _progress(workbook, payload)
                    continue
            if step.get("approval"):
                ref, approval = gate(workbook, payload, step)
                flow.update(approval_ref=ref, approval=approval)
                if not approval["approved"]:
                    payload["steps"].append(
                        {
                            "name": step["name"],
                            "state": "awaiting_approval",
                            "elapsed_seconds": time.monotonic() - started,
                            **flow,
                        }
                    )
                    payload["omitted"].append(
                        {"step": step["name"], "reason": "awaiting_approval"}
                    )
                    _progress(workbook, payload)
                    continue
            execution_step = step | {
                "_csv_types": {
                    index: plan["csv_types"][alias]
                    for index, alias in enumerate(step["inputs"])
                    if alias in plan.get("csv_types", {})
                }
            }
            step_inputs = [aliases[alias] for alias in step["inputs"]]
            result = (
                resumed_result(workbook, resume_steps[step["name"]], step_inputs)
                if step["name"] in resume_steps
                else None
            )
            if result is not None:
                pass
            elif step.get("kind") == "worker":
                from .task_workers import execute

                with workbook.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    validate_claim(
                        db,
                        principal,
                        claim_ticket,
                        resource="task:" + name,
                        required=False,
                    )
                result = execute(
                    workbook,
                    step,
                    step_inputs,
                    workers,
                    definition_id=payload["definition_id"],
                    expected_digest=payload["workers"][step["worker"]],
                )
            else:
                result = _execute_step(
                    workbook,
                    execution_step,
                    step_inputs,
                    force,
                )
            artifact_id = result["saved"]["artifact_id"]
            aliases[step["name"]] = artifact_id
            if result.get("preview_truncated"):
                payload["omitted"].append(
                    {
                        "step": step["name"],
                        "reason": "rows_retained_in_artifact",
                        "artifact_id": artifact_id,
                    }
                )
            payload["steps"].append(
                {
                    "name": step["name"],
                    "state": "complete",
                    "artifact_id": artifact_id,
                    "cache": result.pop("cache"),
                    "result": {k: v for k, v in result.items() if k != "saved"},
                    "elapsed_seconds": time.monotonic() - started,
                    **flow,
                }
            )
        except Exception as error:  # noqa: BLE001 - persist failures at the task boundary
            payload["steps"].append(
                {
                    "name": step["name"],
                    "state": "failed",
                    "error": str(error)[:500],
                    "error_type": type(error).__name__,
                    "elapsed_seconds": time.monotonic() - started,
                    **flow,
                }
            )
            payload["omitted"].append(
                {"step": step["name"], "reason": "execution_failed"}
            )
        _progress(workbook, payload)
    try:
        _evidence(workbook, plan, payload)
    except Exception as error:  # noqa: BLE001 - preserve completed work when evidence fails
        payload["omitted"].append(
            {"reason": "evidence_failed", "error": str(error)[:500]}
        )
    return _deliver(
        workbook,
        _finish(workbook, payload, anchor_id, principal, claim_ticket),
        max_bytes,
        fits,
        base_run,
    )


def status(workbook, name, run_id=None, max_bytes=16384, fits=None, base_run=None):
    _name(name)
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        head = db.execute("SELECT * FROM task_heads WHERE name=?", (name,)).fetchone()
        if run_id:
            row = db.execute(
                "SELECT * FROM task_runs WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM task_runs WHERE name=? ORDER BY generation DESC LIMIT 1",
                (name,),
            ).fetchone()
        if row is None:
            if run_id:
                raise ValueError("Unknown run for this task")
            definition = workbook._get(db, _name(name))
            return _brief(
                {
                    "task": name,
                    "definition_id": definition["artifact_id"],
                    "state": "not_run",
                    "latest_success": None,
                    "sources": {},
                    "steps": [],
                    "citations": [],
                    "omitted": [],
                },
                max_bytes,
                fits,
            )
        payload = json.loads(row["payload"])
        payload["current"] = _current(workbook, db, payload)
        if head["success_generation"] > payload["generation"]:
            payload["current"] = False
        payload["latest_success"] = head["latest_success"]
        if payload["state"] == "complete" and not payload["current"]:
            payload["state"] = "superseded"
        if any(step.get("approval_ref") for step in payload["steps"]):
            from .task_flow import revalidate_approvals

            payload["approval_current"] = revalidate_approvals(workbook, db, payload)
            if not payload["approval_current"]:
                payload["current"] = False
                if payload["state"] in {"complete", "superseded", "awaiting_approval"}:
                    payload["state"] = "awaiting_approval"
    return _deliver(workbook, payload, max_bytes, fits, base_run)
