"""Recallable observations extracted from sanitized continuity payloads.

The immutable episode ledger remains the accounting/audit record. This module
stores only substantive bounded cells or explicit decisions, with exact source
IDs and an execution-observed/unverified label. It never copies session goals,
usage, timing, requests, source bodies, or status-only tool replies.
"""

import hashlib
import json
import re

from .coordination import acting_as, authorize

ARTIFACT = re.compile(r"wb_[0-9a-f]{32}\Z")
MAX_FACT_BYTES = 4096


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _refs(value):
    if isinstance(value, str):
        return {value} if ARTIFACT.fullmatch(value) else set()
    if isinstance(value, list):
        return set().union(*(_refs(item) for item in value)) if value else set()
    if isinstance(value, dict):
        return (
            set().union(*(_refs(item) for item in value.values())) if value else set()
        )
    return set()


def _table(value):
    if not isinstance(value, dict):
        return None
    columns, rows = value.get("columns"), value.get("rows")
    if (
        not isinstance(columns, list)
        or not columns
        or not isinstance(rows, list)
        or not rows
    ):
        return None
    names = [
        column.get("name") if isinstance(column, dict) else column for column in columns
    ]
    if any(not isinstance(name, str) or not name for name in names):
        return None
    selected = [
        row for row in rows[:5] if isinstance(row, list) and len(row) == len(names)
    ]
    if not selected:
        return None
    result = {"columns": names, "rows": selected}
    if value.get("types"):
        result["types"] = value["types"]
    if value.get("integer_string_columns"):
        result["integer_string_columns"] = value["integer_string_columns"]
    if (
        value.get("preview_truncated")
        or value.get("memory_rows_omitted")
        or value.get("memory_columns_omitted")
        or len(rows) > 5
    ):
        result["partial_preview"] = True
    return result


def project_memory_fact(workbook, payload, *, principal):
    """Return a stable memory_fact artifact or None, without ledger mutation.

    Only trusted continuity code passes an already-sanitized, bounded payload.
    The caller atomically records episode ownership of the returned artifact.
    """
    authorize(principal, "write")
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        return None
    body = {
        "verification": "execution_observed_not_independently_validated"
        if payload.get("verified") is True
        else "unverified_external_report"
    }
    selected = None
    if payload.get("kind") == "decision":
        selected = {
            key: facts[key]
            for key in ("decision", "reason", "summary", "text")
            if isinstance(facts.get(key), str) and facts[key].strip()
        }
        if not selected:
            return None
        body["decision"] = selected
        reference_source = facts
    elif payload.get("kind") == "tool_result":
        if "tool" in payload and payload.get("verified") is not True:
            return None  # Failed observed calls are audit records, not facts.
        tool = payload.get("tool", facts.get("tool"))
        content = facts.get("result", facts) if "tool" in facts else facts
        if tool == "query":
            selected = _table(content)
            if selected is None:
                return None
            body["result"] = selected
            reference_source = content.get("inputs", [])
            if isinstance(content.get("saved"), dict):
                reference_source = [
                    reference_source,
                    content["saved"].get("artifact_id"),
                ]
        elif (
            tool == "task_run"
            and isinstance(content, dict)
            and content.get("state") == "complete"
        ):
            selected = []
            for step in content.get("steps", [])[:8]:
                if isinstance(step, dict) and step.get("state") == "complete":
                    table = _table(step.get("result"))
                    if table:
                        selected.append({"step": step.get("name"), **table})
            if not selected:
                return None
            body["results"] = selected
            # Run/report IDs change even when materialized results and sources
            # do not. Keep exact input/result provenance, not per-run identity.
            reference_source = [
                content.get("sources", {}),
                [
                    {
                        "artifact_id": step.get("artifact_id"),
                        "inputs": step.get("result", {}).get("inputs", []),
                    }
                    for step in content.get("steps", [])[:8]
                    if isinstance(step, dict) and isinstance(step.get("result"), dict)
                ],
            ]
        else:
            return None
    else:
        return None
    # Do not resolve a mutable name after execution. Captured immutable IDs are
    # the only accepted provenance; absent references stay explicitly absent.
    with workbook.connect() as db:
        refs = [
            ref
            for ref in sorted(_refs(reference_source))
            if db.execute(
                "SELECT 1 FROM artifacts WHERE artifact_id=?", (ref,)
            ).fetchone()
        ]
    if len(refs) > 16:
        return None
    body["source_artifacts"] = refs
    encoded = _json(body)
    if len(encoded.encode()) > MAX_FACT_BYTES:
        return None  # Never silently make a partial row look complete.
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    name = "_memory_facts/" + digest
    with acting_as(principal):
        for _ in range(3):
            try:
                return workbook.save_text(
                    encoded,
                    name=name,
                    kind="memory_fact",
                    media_type="application/json",
                    dependencies=refs,
                )
            except ValueError as error:
                # Retry only the known duplicate-name conflict. Missing inputs,
                # corrupt data, and other validation failures must propagate.
                if not str(error).startswith(f"{name!r} is at version "):
                    raise
                with workbook.connect() as db:
                    db.execute("BEGIN")
                    existing = db.execute(
                        "SELECT * FROM latest WHERE name=?", (name,)
                    ).fetchone()
                    if existing is None:
                        # Retention removed the old fact after save observed
                        # its name. Recreate it, bounded against repeated churn.
                        continue
                    if (
                        existing["kind"] != "memory_fact"
                        or existing["digest"] != digest
                        or workbook._dependencies(db, existing["artifact_id"]) != refs
                    ):
                        raise
                    return workbook._summary(db, existing)
    raise ValueError("Fact changed repeatedly during retention cleanup; retry capture")
