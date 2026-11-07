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
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from .store import TABLE_TYPES, is_text

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
    if not isinstance(steps, list) or len(steps) > 8:
        raise ValueError("At most eight SQL steps are supported")
    known = set(sources)
    normalized = []
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "name",
            "inputs",
            "sql",
            "parameters",
        }:
            raise ValueError("A step supports name, inputs, sql and parameters only")
        name = step.get("name", "")
        if not isinstance(name, str) or not ALIAS.fullmatch(name) or name in known:
            raise ValueError("Step names must be distinct simple aliases")
        known.add(name)
        inputs = step.get("inputs")
        if (
            not isinstance(inputs, list)
            or not 1 <= len(inputs) <= 8
            or any(not isinstance(value, str) for value in inputs)
            or len(set(inputs)) != len(inputs)
        ):
            raise ValueError("Each step needs 1–8 distinct input aliases")
        sql = step.get("sql")
        if not isinstance(sql, str) or not 1 <= len(sql.strip()) <= 6000:
            raise ValueError("Each step needs SQL of at most 6000 characters")
        parameters = step.get("parameters", [])
        if not isinstance(parameters, list) or len(parameters) > 30:
            raise ValueError("At most 30 scalar parameters are supported")
        if any(
            type(p) not in (str, int, float, bool, type(None))
            or (isinstance(p, str) and len(p) > 1000)
            for p in parameters
        ):
            raise ValueError("Parameters must be bounded JSON scalars")
        _json(parameters)
        normalized.append(
            {"name": name, "inputs": inputs, "sql": sql, "parameters": parameters}
        )
    ordered, ready, pending = [], set(sources), list(normalized)
    while pending:
        available = [step for step in pending if set(step["inputs"]) <= ready]
        if not available:
            raise ValueError("Task has a dependency cycle or unknown input alias")
        for step in available:
            ordered.append(step)
            ready.add(step["name"])
            pending.remove(step)
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
        "format": 1,
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
):
    """Save a versioned plan; this does not execute queries or acquire sources."""
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
        or plan.get("format") != 1
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
            sources = {
                alias: _record(workbook, db, workbook._get(db, item["artifact_id"]))
                for alias, item in payload["sources"].items()
            }
        else:
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


def _finish(workbook, payload, anchor_id):
    failures = {
        "no_text_sources",
        "no_matching_evidence",
        "index_incomplete",
        "evidence_failed",
    }
    successful = all(
        step["state"] == "complete" for step in payload["steps"]
    ) and not any(item["reason"] in failures for item in payload["omitted"])
    payload["state"] = "complete" if successful else "partial"
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
                    {k: v for k, v in step.items() if k != "cache"}
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
                }
            )
            if len(_json(receipt).encode()) <= max_bytes and (
                fits is None or fits(receipt)
            ):
                return receipt
    return _brief(payload, max_bytes, fits)


def run_task(
    workbook,
    name,
    resume_from=None,
    force=False,
    max_bytes=16384,
    fits=None,
    base_run=None,
):
    _name(name)
    _initialize(workbook)
    if type(force) is not bool:
        raise ValueError("force must be boolean")
    if base_run is not None and (not isinstance(base_run, str) or len(base_run) > 64):
        raise ValueError("base_run must be a retained run ID")
    if type(max_bytes) is not int or not 256 <= max_bytes <= 131072:
        raise ValueError("Brief max_bytes must be between 256 and 131072")
    plan, payload, anchor_id = _pin(workbook, name, resume_from)
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
    for step in plan["steps"]:
        if any(alias not in aliases for alias in step["inputs"]):
            payload["steps"].append(
                {
                    "name": step["name"],
                    "state": "blocked",
                    "reason": "failed_dependency",
                }
            )
            payload["omitted"].append(
                {"step": step["name"], "reason": "failed_dependency"}
            )
            _progress(workbook, payload)
            continue
        try:
            execution_step = step | {
                "_csv_types": {
                    index: plan["csv_types"][alias]
                    for index, alias in enumerate(step["inputs"])
                    if alias in plan.get("csv_types", {})
                }
            }
            result = _execute_step(
                workbook,
                execution_step,
                [aliases[alias] for alias in step["inputs"]],
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
                }
            )
        except Exception as error:  # noqa: BLE001 - persist failures at the task boundary
            payload["steps"].append(
                {
                    "name": step["name"],
                    "state": "failed",
                    "error": str(error)[:500],
                    "error_type": type(error).__name__,
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
        workbook, _finish(workbook, payload, anchor_id), max_bytes, fits, base_run
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
    return _deliver(workbook, payload, max_bytes, fits, base_run)
