"""Deterministic, locally proven task packages; applying one never runs it.

Proof means a complete, published execution, not independent factual validation.
The catalog registry is the authority, not caller-supplied JSON or a run preview.
The filesystem/database owner remains inside the local trust boundary.
"""

import hashlib
import json
import stat
import time
from threading import Timer

import duckdb

from . import tasks
from .coordination import append_event, authorize, current_principal
from .query import literal
from .store import TABLE_TYPES

MAX_PACKAGE = 65536
SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_records (
    artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL
);
"""


def _initialize(workbook):
    tasks._initialize(workbook)
    with workbook.connect() as db:
        db.executescript(SCHEMA)


def _name(name):
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or name != name.strip()
        or name.startswith("wb_")
        or "\x00" in name
    ):
        raise ValueError("Skill name must contain 1–64 characters")
    return "_skills/" + name


def _get(workbook, db, ref):
    if not isinstance(ref, str) or not ref:
        raise ValueError("A skill name or artifact ID is required")
    return workbook._get(db, ref if ref.startswith(("wb_", "_skills/")) else _name(ref))


def _identity(row):
    return {
        "artifact_id": row["artifact_id"],
        "name": row["name"],
        "version": row["version"],
        "sha256": row["digest"],
    }


def _read(workbook, row, cap):
    path = workbook.blobs / row["digest"]
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_size != row["size_bytes"]
        or before.st_size > cap
    ):
        raise ValueError("Artifact is invalid, oversized, or not a regular file")
    with path.open("rb") as stream:
        data = stream.read(cap + 1)
    if len(data) > cap or hashlib.sha256(data).hexdigest() != row["digest"]:
        raise ValueError("Artifact integrity check failed")
    return data


def _verify_result(workbook, row):
    """Bounded streaming integrity check for a retained complete materialization."""
    path = workbook.blobs / row["digest"]
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size != row["size_bytes"]
        or info.st_size > 64 * 1024**2
    ):
        raise ValueError("Retained result exceeds the 64 MiB verification bound")
    digest, deadline = hashlib.sha256(), time.monotonic() + 15
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
            if time.monotonic() > deadline:
                raise ValueError("Retained result integrity deadline exceeded")
    if digest.hexdigest() != row["digest"]:
        raise ValueError("Retained result integrity check failed")


def _contract(workbook, row, csv_types=None):
    contract = {"media_type": row["media_type"]}
    if row["media_type"] not in TABLE_TYPES:
        return contract
    path = workbook.blobs / row["digest"]
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != row["size_bytes"]:
        raise ValueError("Source is not an intact regular artifact")
    settings = workbook.settings
    with duckdb.connect(
        config={
            "threads": str(settings.query_threads),
            "memory_limit": settings.query_memory,
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        }
    ) as db:
        db.execute("SET allowed_paths = [" + literal(str(path)) + "]")
        db.execute("SET enable_external_access = false")
        db.execute("SET lock_configuration = true")
        timer = Timer(min(settings.query_timeout, 30), db.interrupt)
        timer.daemon = True
        timer.start()
        try:
            reader = "read_parquet"
            options = ""
            if row["media_type"] == "text/csv":
                reader = "read_csv_auto"
                if csv_types:
                    options = (
                        ",types={"
                        + ",".join(
                            literal(key) + ":" + literal(value)
                            for key, value in csv_types.items()
                        )
                        + "}"
                    )
            result = db.sql(
                f"SELECT * FROM {reader}({literal(str(path))}{options}) LIMIT 0"
            )
            columns, types = list(result.columns), list(map(str, result.types))
        finally:
            timer.cancel()
            timer.join()
    if len(columns) > 256 or any(len(c) > 256 for c in columns):
        raise ValueError("Skill source schema exceeds 256 bounded columns")
    return contract | {"columns": columns, "types": types}


def _origin(workbook, db, task, run_id, *, require_current=False):
    tasks._name(task)
    if run_id is not None and (
        not isinstance(run_id, str) or not 1 <= len(run_id) <= 64
    ):
        raise ValueError("run_id must be a recorded task run ID")
    head = db.execute("SELECT * FROM task_heads WHERE name=?", (task,)).fetchone()
    if run_id is None:
        run_id = head["latest_success"] if head else None
    row = db.execute(
        "SELECT * FROM task_runs WHERE run_id=? AND name=? AND length(payload)<=1048576",
        (run_id, task),
    ).fetchone()
    if row is None or row["state"] != "complete" or row["finished"] is None:
        raise ValueError("Skill requires a finalized successful published task run")
    payload = json.loads(row["payload"])
    if (
        payload.get("state") != "complete"
        or payload.get("current") is not True
        or payload.get("latest_success") != run_id
        or payload.get("run_id") != run_id
        or payload.get("definition_id") != row["definition_id"]
        or payload.get("task") != task
        or payload.get("generation") != row["generation"]
    ):
        raise ValueError("Run was not successfully published as current")
    event = db.execute(
        "SELECT * FROM coordination_events WHERE kind='task.published' AND resource=? "
        "AND json_extract(details,'$.run_id')=? ORDER BY event_id LIMIT 1",
        ("task:" + task, run_id),
    ).fetchone()
    producer = payload.get("producer")
    if event is None or producer != {"identity": event["actor"], "role": event["role"]}:
        raise ValueError("Run lacks authoritative publication provenance")
    details = json.loads(event["details"])
    if any(
        details.get(key) != payload.get(key)
        for key in ("run_id", "artifact_id", "definition_id", "state", "current")
    ):
        raise ValueError("Run publication provenance does not match")
    current = bool(
        head
        and head["latest_success"] == run_id
        and tasks._current(workbook, db, payload)
    )
    if require_current and not current:
        raise ValueError(
            "Distill requires the latest successful run with current inputs"
        )
    definition = workbook._get(db, row["definition_id"])
    plan = tasks._read_plan(workbook, definition)
    if plan["format"] != 1:
        raise ValueError(
            "Reusable skills currently support unconditional SQL-only tasks; conditional, approval-gated and worker tasks cannot be distilled or replayed as skills"
        )
    if set(payload["sources"]) != set(plan["sources"]):
        raise ValueError("Run sources differ from its definition")
    sources = {}
    for alias in plan["sources"]:
        source = workbook._get(db, payload["sources"][alias]["artifact_id"])
        if source["digest"] != payload["sources"][alias]["sha256"]:
            raise ValueError("Run source provenance differs")
        sources[alias] = source
    aliases = {alias: source["artifact_id"] for alias, source in sources.items()}
    if len(payload["steps"]) != len(plan["steps"]):
        raise ValueError("Run has incomplete steps")
    results = {}
    for step, completed in zip(plan["steps"], payload["steps"], strict=True):
        if completed.get("state") != "complete" or step["name"] != completed.get(
            "name"
        ):
            raise ValueError("Run has incomplete or reordered steps")
        result = workbook._get(db, completed["artifact_id"])
        ordered_inputs = [aliases[alias] for alias in step["inputs"]]
        if (
            result["media_type"] != "application/vnd.apache.parquet"
            or workbook._dependencies(db, result["artifact_id"])
            != list(dict.fromkeys(ordered_inputs))
            or completed.get("result", {}).get("inputs") != ordered_inputs
        ):
            raise ValueError("Result lineage differs from the executed plan")
        results[step["name"]] = result
        aliases[step["name"]] = result["artifact_id"]
    omissions = payload.get("omitted", [])
    for omission in omissions:
        retained = results.get(omission.get("step"))
        if (
            set(omission) != {"step", "reason", "artifact_id"}
            or omission["reason"] != "rows_retained_in_artifact"
            or retained is None
            or omission["artifact_id"] != retained["artifact_id"]
        ):
            raise ValueError("Run has missing work or evidence; cannot distill it")
    report = workbook._get(db, payload["artifact_id"])
    anchor = workbook._get(db, row["anchor_id"])
    if (
        report["kind"] != "task_run"
        or workbook._dependencies(db, report["artifact_id"])
        != list(
            dict.fromkeys(
                [anchor["artifact_id"], *[r["artifact_id"] for r in results.values()]]
            )
        )
        or workbook._dependencies(db, anchor["artifact_id"])
        != list(
            dict.fromkeys(
                [
                    definition["artifact_id"],
                    *[source["artifact_id"] for source in sources.values()],
                ]
            )
        )
    ):
        raise ValueError("Run retention provenance is invalid")
    snapshot = json.loads(_read(workbook, report, 1024 * 1024))
    anchor_body = json.loads(_read(workbook, anchor, 32768))
    if (
        snapshot.get("record_type") != "computation_snapshot"
        or snapshot.get("state") != "computed"
        or snapshot.get("run_id") != run_id
        or snapshot.get("definition_id") != row["definition_id"]
        or snapshot.get("sources") != payload["sources"]
        or anchor_body.get("run_id") != run_id
        or anchor_body.get("definition_id") != row["definition_id"]
        or anchor_body.get("sources") != payload["sources"]
    ):
        raise ValueError("Retained run snapshot differs from published provenance")
    provenance = {
        "task": task,
        "run_id": run_id,
        "publication_event_id": event["event_id"],
        "producer": producer,
        "definition": _identity(definition),
        "run_artifact": _identity(report),
        "anchor": _identity(anchor),
        "sources": {alias: _identity(source) for alias, source in sources.items()},
        "results": {alias: _identity(result) for alias, result in results.items()},
        "retained_output_notices": omissions,
    }
    return plan, provenance, sources, results, not current


def _load(workbook, db, ref):
    row = _get(workbook, db, ref)
    proof = db.execute(
        "SELECT * FROM skill_records WHERE artifact_id=?", (row["artifact_id"],)
    ).fetchone()
    if row["kind"] != "skill" or proof is None or proof["digest"] != row["digest"]:
        raise ValueError("Not a registered, proven skill package")
    try:
        package = json.loads(_read(workbook, row, MAX_PACKAGE))
        if (
            set(package)
            != {"format", "description", "plan", "contracts", "origin", "distilled_by"}
            or package["format"] != 1
            or package["distilled_by"]
            != {"identity": proof["actor"], "role": proof["role"]}
            or package["origin"]["run_id"] != proof["run_id"]
        ):
            raise ValueError("Malformed skill package")
        plan, origin, _, _, stale = _origin(
            workbook, db, package["origin"]["task"], proof["run_id"]
        )
        if package["plan"] != plan or package["origin"] != origin:
            raise ValueError("Skill content differs from its proven execution")
        if set(package["contracts"]) != set(plan["sources"]):
            raise ValueError("Skill source contracts are incomplete")
        if workbook._dependencies(db, row["artifact_id"]) != [
            origin["run_artifact"]["artifact_id"]
        ]:
            raise ValueError("Skill is missing its retained provenance")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed skill package") from exc
    return row, package, stale


def _response(workbook, db, row, package, stale, *, full):
    result = (
        {key: row[key] for key in ("artifact_id", "name", "version")}
        | workbook._status(db, row["artifact_id"])
        | {
            "proof": "successful_execution",
            "origin_stale": stale,
            "description": package["description"],
            "origin_run": package["origin"]["run_id"],
            "sources": list(package["contracts"]),
            "steps": [step["name"] for step in package["plan"]["steps"]],
        }
    )
    if full:
        result["package"] = package
    return result


@tasks._as_actor
def distill(
    workbook,
    name,
    task,
    run_id=None,
    description=None,
    expected_version=None,
    principal=None,
):
    """Extract a strict reusable plan from a complete, currently published run."""
    principal = principal or current_principal()
    authorize(principal, "write")
    artifact_name = _name(name)
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        plan, origin, sources, results, _ = _origin(
            workbook, db, task, run_id, require_current=True
        )
    description = plan["objective"] if description is None else description
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 360:
        raise ValueError("Skill description must contain 1–360 characters")
    for result in results.values():
        _verify_result(workbook, result)
    contracts = {
        alias: _contract(workbook, row, plan.get("csv_types", {}).get(alias))
        for alias, row in sources.items()
    }
    package = {
        "format": 1,
        "description": description.strip(),
        "plan": plan,
        "contracts": contracts,
        "origin": origin,
        "distilled_by": {"identity": principal.identity, "role": principal.role},
    }
    body = tasks._json(package)
    if len(body.encode()) > MAX_PACKAGE:
        raise ValueError("Skill package exceeds 64 KiB")
    digest = hashlib.sha256(body.encode()).hexdigest()
    # The artifact may survive a crash before proof publication. It is never
    # accepted without the registry row. An exact same-actor retry can repair it.
    with workbook.connect() as db:
        pending = db.execute(
            "SELECT a.* FROM latest a LEFT JOIN skill_records p USING(artifact_id) "
            "WHERE a.name=? AND a.digest=? AND p.artifact_id IS NULL AND a.version=?",
            (artifact_name, digest, (expected_version or 0) + 1),
        ).fetchone()
        repair = (
            pending
            and db.execute(
                "SELECT 1 FROM coordination_events WHERE kind='artifact.created' "
                "AND resource=? AND actor=? AND role=?",
                (pending["artifact_id"], principal.identity, principal.role),
            ).fetchone()
        )
    saved = (
        {"artifact_id": pending["artifact_id"]}
        if repair
        else workbook.save_text(
            body,
            name=artifact_name,
            kind="skill",
            media_type="application/json",
            summary=package["description"],
            dependencies=[origin["run_artifact"]["artifact_id"]],
            expected_version=expected_version,
        )
    )
    with workbook.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        _, fresh_origin, _, _, _ = _origin(
            workbook, db, task, origin["run_id"], require_current=True
        )
        if fresh_origin != origin:
            raise ValueError("Run provenance changed while distilling")
        saved_row = workbook._get(db, saved["artifact_id"])
        if (
            saved_row["digest"] != digest
            or saved_row["kind"] != "skill"
            or saved_row["media_type"] != "application/json"
            or workbook._dependencies(db, saved["artifact_id"])
            != [origin["run_artifact"]["artifact_id"]]
            or _read(workbook, saved_row, MAX_PACKAGE) != body.encode()
        ):
            raise ValueError("Pending skill artifact does not match its proof")
        if db.execute(
            "SELECT 1 FROM skill_records WHERE artifact_id=?", (saved["artifact_id"],)
        ).fetchone():
            raise ValueError(
                "Skill was already published by a concurrent attempt; inspect its current version"
            )
        db.execute(
            "INSERT INTO skill_records VALUES(?,?,?,?,?)",
            (
                saved["artifact_id"],
                digest,
                origin["run_id"],
                principal.identity,
                principal.role,
            ),
        )
        append_event(
            db,
            principal,
            "skill.distilled",
            saved["artifact_id"],
            {
                "run_id": origin["run_id"],
                "task": task,
                "sha256": digest,
            },
        )
    with workbook.connect() as db:
        db.execute("BEGIN")
        row, package, stale = _load(workbook, db, saved["artifact_id"])
        return _response(workbook, db, row, package, stale, full=False)


def inspect_skill(workbook, ref):
    """Read an exact skill version and its verified historical execution proof."""
    authorize(current_principal(), "read")
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        row, package, stale = _load(workbook, db, ref)
        return _response(workbook, db, row, package, stale, full=True)


def find_skills(workbook, query="", limit=8):
    """Find latest registered skill summaries by literal name/description text."""
    authorize(current_principal(), "read")
    if (
        not isinstance(query, str)
        or len(query) > 500
        or type(limit) is not int
        or not 1 <= limit <= 20
    ):
        raise ValueError("Use a query of at most 500 characters and limit 1–20")
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        rows = db.execute(
            "SELECT a.artifact_id FROM latest a JOIN skill_records p USING(artifact_id) "
            "WHERE a.kind='skill' AND instr(casefold(a.name || ' ' || a.summary),casefold(?))>0 "
            "ORDER BY a.name LIMIT ?",
            (query, limit + 1),
        ).fetchall()
        found, invalid = [], 0
        for selected in rows[:limit]:
            try:
                row, package, stale = _load(workbook, db, selected["artifact_id"])
                found.append(_response(workbook, db, row, package, stale, full=False))
            except (ValueError, OSError):
                invalid += 1
        return {"skills": found, "complete": len(rows) <= limit, "invalid": invalid}


@tasks._as_actor
def use_skill(
    workbook,
    ref,
    task_name,
    bindings,
    parameters=None,
    expected_version=None,
    principal=None,
):
    """Validate explicit bindings and define a pinned task; never execute it."""
    principal = principal or current_principal()
    authorize(principal, "write")
    _initialize(workbook)
    with workbook.connect() as db:
        db.execute("BEGIN")
        row, package, _ = _load(workbook, db, ref)
        if not isinstance(bindings, dict) or set(bindings) != set(package["contracts"]):
            raise ValueError(
                "Explicit bindings must contain exactly every skill source alias"
            )
        if any(not isinstance(ref, str) or not ref for ref in bindings.values()):
            raise ValueError("Bindings must be artifact names or immutable IDs")
        resolved = {
            alias: workbook._get(db, value) for alias, value in bindings.items()
        }
    plan = json.loads(tasks._json(package["plan"]))
    for alias, source in resolved.items():
        if (
            _contract(workbook, source, plan.get("csv_types", {}).get(alias))
            != package["contracts"][alias]
        ):
            raise ValueError(
                f"Binding {alias!r} must match the exact source media type and ordered schema"
            )
    if parameters is not None:
        steps = {step["name"]: step for step in plan["steps"]}
        if not isinstance(parameters, dict) or set(parameters) - set(steps):
            raise ValueError("Parameter overrides must use existing step names")
        for name, values in parameters.items():
            if not isinstance(values, list) or len(values) != len(
                steps[name]["parameters"]
            ):
                raise ValueError(
                    "Parameter overrides must preserve each step's parameter arity"
                )
            steps[name]["parameters"] = values
    pinned = {alias: resolved[alias]["artifact_id"] for alias in plan["sources"]}
    result = tasks.define_task(
        workbook,
        task_name,
        plan["objective"],
        pinned,
        plan["steps"],
        searches=plan["searches"],
        expected_version=expected_version,
        csv_types=plan.get("csv_types"),
        pin_sources=True,
        principal=principal,
        provenance_refs=[row["artifact_id"]],
    )
    return result | {
        "skill": {key: row[key] for key in ("artifact_id", "name", "version")},
    }
