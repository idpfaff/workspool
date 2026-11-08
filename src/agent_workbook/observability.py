"""Bounded runtime aggregates, with measurement gaps kept explicit.

No source-size counterfactual or provider price is inferred. Content deltas
require a caller-verified, same-data single-representation comparison. Daily
rows expire after 90 UTC dates, with at most 128 labeled groups plus overflow
per date. This module never stores tool arguments or result bodies.
"""

import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta

from .coordination import authorize, authorize_capability, current_principal

RETENTION_DAYS = 90
MAX_GROUPS = 128
SAMPLE_LIMIT = 256
MEASUREMENT_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS observation_daily (
    day TEXT NOT NULL,
    tool TEXT NOT NULL,
    tokenizer TEXT NOT NULL,
    counters TEXT NOT NULL,
    PRIMARY KEY(day,tool,tokenizer)
);
"""
INTEGER_FIELDS = (
    "calls",
    "errors",
    "content_samples",
    "content_missing_calls",
    "content_tokens",
    "json_content_tokens",
    "content_tokens_reduced",
    "wire_samples",
    "wire_missing_calls",
    "wire_tokens",
    "legacy_token_calls_excluded",
    "legacy_formatting_samples_excluded",
    "cache_hits",
    "cache_misses",
    "cache_bypassed",
    "cache_samples",
    "calls_without_cache_measurement",
    "task_steps",
    "timed_steps",
    "steps_without_timing",
    "result_artifact_references",
    "retained_base_receipts",
    "recall_calls",
    "dense_coverage_samples",
    "dense_complete_calls",
    "lexical_fallback_calls",
    "indexed_chunks",
    "scored_chunks",
    "pending_chunks",
    "recall_without_dense_coverage",
    "overflow_calls",
)


def _today():
    return datetime.now(UTC).date()


def _table(db, name):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _empty():
    return dict.fromkeys(INTEGER_FIELDS, 0) | {
        "elapsed_seconds": 0.0,
        "max_elapsed_seconds": 0.0,
        "step_elapsed_seconds": 0.0,
    }


TOKEN_FIELDS = {
    "content_samples",
    "content_missing_calls",
    "content_tokens",
    "json_content_tokens",
    "content_tokens_reduced",
    "wire_samples",
    "wire_missing_calls",
    "wire_tokens",
}


def _read_counters(encoded):
    """Keep historical work counts, excluding incompatible legacy token totals."""
    previous = json.loads(encoded)
    result = _empty()
    for key in result.keys() - TOKEN_FIELDS:
        if key in previous:
            result[key] = previous[key]
    if previous.get("measurement_version") == MEASUREMENT_VERSION:
        for key in TOKEN_FIELDS:
            result[key] = previous.get(key, 0)
    else:
        result["legacy_token_calls_excluded"] += previous.get("calls", 0)
        result["legacy_formatting_samples_excluded"] += previous.get(
            "formatting_samples", 0
        )
        result["content_missing_calls"] = previous.get("calls", 0)
        result["wire_missing_calls"] = previous.get("calls", 0)
    return result


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10**15


def _count(value):
    return type(value) is int and 0 <= value <= 10**15


def _label(tokenizer):
    if tokenizer is None:
        return "unconfigured"
    if not isinstance(tokenizer, str) or not 1 <= len(tokenizer) <= 4096:
        raise ValueError("tokenizer must be a bounded configured identifier")
    if tokenizer in {"cl100k_base", "o200k_base"}:
        return tokenizer
    # Config identity only; caller may supply a content hash for local encodings.
    # Do not persist paths or read arbitrary caller-named files in a hook.
    return "configured:" + hashlib.sha256(tokenizer.encode()).hexdigest()[:24]


def _measure_result(result):
    counters = _empty()
    if not isinstance(result, dict):
        return counters
    steps = result.get("steps")
    records = [result]
    if isinstance(steps, list):
        records += [step for step in steps[:8] if isinstance(step, dict)]
        for step in records[1:]:
            counters["task_steps"] += 1
            if _number(step.get("elapsed_seconds")):
                counters["timed_steps"] += 1
                counters["step_elapsed_seconds"] += step["elapsed_seconds"]
            else:
                counters["steps_without_timing"] += 1
            if isinstance(step.get("artifact_id"), str):
                counters["result_artifact_references"] += 1
    for item in records:
        cache = item.get("cache")
        if isinstance(cache, dict) and type(cache.get("hit")) is bool:
            counters["cache_samples"] += 1
            if cache["hit"]:
                counters["cache_hits"] += 1
            elif cache.get("reason") == "configured_worker":
                counters["cache_bypassed"] += 1
            else:
                counters["cache_misses"] += 1
    counters["retained_base_receipts"] = int(
        result.get("delivery") == "unchanged"
        and result.get("requires_retained_base") is True
    )
    if "dense_complete" in result or "dense_coverage" in result:
        counters["recall_calls"] = 1
        counters["dense_complete_calls"] = int(result.get("dense_complete") is True)
        counters["lexical_fallback_calls"] = int(result.get("mode") == "lexical")
        coverage = result.get("dense_coverage")
        if isinstance(coverage, dict) and all(
            _count(coverage.get(key))
            for key in ("indexed_chunks", "scored_chunks", "pending_chunks")
        ):
            counters["dense_coverage_samples"] = 1
            for key in ("indexed_chunks", "scored_chunks", "pending_chunks"):
                counters[key] = coverage[key]
        else:
            counters["recall_without_dense_coverage"] = 1
    return counters


def _add(target, source):
    for key, value in source.items():
        if key == "max_elapsed_seconds":
            target[key] = max(target[key], value)
        else:
            target[key] += value


def record_tool(
    workbook,
    tool,
    elapsed_seconds,
    result=None,
    *,
    failed=False,
    tokenizer=None,
    content_tokens=None,
    json_content_tokens=None,
    wire_tokens=None,
    same_content=False,
):
    """Record one observed invocation; result is the decoded delivered dict.

    `same_content` is a trusted instrumentation assertion, not a tool argument.
    Signed content deltas can be negative. They never measure provider bills
    or the tokens that omitted source material might hypothetically have used.
    """
    authorize(current_principal(), "write")
    if not isinstance(tool, str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", tool
    ):
        raise ValueError("Invalid observed tool name")
    if tool in {"metrics", "verify"}:
        return False
    if (
        not _number(elapsed_seconds)
        or type(failed) is not bool
        or type(same_content) is not bool
    ):
        raise ValueError(
            "Observation requires finite nonnegative duration and boolean flags"
        )
    for value in (content_tokens, json_content_tokens, wire_tokens):
        if value is not None and not _count(value):
            raise ValueError("Token measurements must be nonnegative integers")
    label = _label(tokenizer)
    sample = _measure_result(result)
    if tool not in {"query", "task_run"}:
        # Reopening a stored run repeats its old telemetry; it is not another
        # execution. Keep read latency/formatting and recall coverage distinct.
        for key in (
            "cache_hits",
            "cache_misses",
            "cache_bypassed",
            "cache_samples",
            "task_steps",
            "timed_steps",
            "steps_without_timing",
            "step_elapsed_seconds",
            "result_artifact_references",
        ):
            sample[key] = 0
    sample |= {
        "calls": 1,
        "errors": int(failed),
        "elapsed_seconds": float(elapsed_seconds),
        "max_elapsed_seconds": float(elapsed_seconds),
    }
    sample["calls_without_cache_measurement"] = int(sample["cache_samples"] == 0)
    comparison = (
        same_content
        and tokenizer is not None
        and content_tokens is not None
        and json_content_tokens is not None
        and not failed
    )
    if comparison:
        sample |= {
            "content_samples": 1,
            "content_tokens": content_tokens,
            "json_content_tokens": json_content_tokens,
            "content_tokens_reduced": json_content_tokens - content_tokens,
        }
    else:
        sample["content_missing_calls"] = 1
    if tokenizer is not None and wire_tokens is not None:
        sample["wire_samples"] = 1
        sample["wire_tokens"] = wire_tokens
    else:
        sample["wire_missing_calls"] = 1
    day = _today()
    with workbook.connect() as db:
        db.executescript(SCHEMA)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM observation_daily WHERE day<?",
            ((day - timedelta(days=RETENTION_DAYS - 1)).isoformat(),),
        )
        row = db.execute(
            "SELECT counters FROM observation_daily WHERE day=? AND tool=? AND tokenizer=?",
            (day.isoformat(), tool, label),
        ).fetchone()
        if (
            row is None
            and db.execute(
                "SELECT count(*) FROM observation_daily WHERE day=? AND tool!='__overflow__'",
                (day.isoformat(),),
            ).fetchone()[0]
            >= MAX_GROUPS
        ):
            tool, label = "__overflow__", "untracked"
            sample["overflow_calls"] = 1
            # Different tokenizers cannot be combined into a meaningful token sum.
            for key in TOKEN_FIELDS:
                sample[key] = 0
            sample["content_missing_calls"] = 1
            sample["wire_missing_calls"] = 1
            row = db.execute(
                "SELECT counters FROM observation_daily WHERE day=? AND tool=? AND tokenizer=?",
                (day.isoformat(), tool, label),
            ).fetchone()
        total = _read_counters(row[0]) if row else _empty()
        _add(total, sample)
        db.execute(
            "INSERT INTO observation_daily VALUES(?,?,?,?) ON CONFLICT(day,tool,tokenizer) DO UPDATE SET counters=excluded.counters",
            (
                day.isoformat(),
                tool,
                label,
                json.dumps(
                    total | {"measurement_version": MEASUREMENT_VERSION},
                    separators=(",", ":"),
                ),
            ),
        )
    return True


def _retained_tasks(db):
    result = {
        "scope": "latest 256 retained run ledgers, independent of metric date filter",
        "runs_sampled": 0,
        "complete": True,
        "missing_timings": 0,
        "elapsed_seconds": 0.0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_bypassed": 0,
        "timed_steps": 0,
        "steps_without_timing": 0,
        "step_elapsed_seconds": 0.0,
        "result_artifacts_retained": 0,
        "missing_result_artifacts": 0,
        "unique_result_artifacts_retained": 0,
        "result_count_scope": "Per-step references; repeated reuse counts again except in unique_result_artifacts_retained",
    }
    if not _table(db, "task_runs"):
        result["available"] = False
        return result
    rows = db.execute(
        "SELECT run_id,started,finished FROM task_runs ORDER BY started DESC,run_id LIMIT ?",
        (SAMPLE_LIMIT + 1,),
    ).fetchall()
    result["complete"] = len(rows) <= SAMPLE_LIMIT
    result["available"] = True
    unique_results = set()
    for row in rows[:SAMPLE_LIMIT]:
        result["runs_sampled"] += 1
        if row["finished"] is not None and row["finished"] >= row["started"]:
            result["elapsed_seconds"] += row["finished"] - row["started"]
        else:
            result["missing_timings"] += 1
        steps = []
        for step in db.execute(
            "SELECT json_extract(value,'$.artifact_id') AS artifact_id, "
            "json_extract(value,'$.elapsed_seconds') AS elapsed_seconds, "
            "json_extract(value,'$.cache') AS cache "
            "FROM json_each((SELECT payload FROM task_runs WHERE run_id=?),'$.steps') LIMIT 8",
            (row["run_id"],),
        ):
            steps.append(
                dict(step)
                | {"cache": json.loads(step["cache"]) if step["cache"] else None}
            )
        measured = _measure_result({"steps": steps})
        for key in (
            "cache_hits",
            "cache_misses",
            "cache_bypassed",
            "timed_steps",
            "steps_without_timing",
            "step_elapsed_seconds",
        ):
            result[key] += measured[key]
        for step in steps:
            if not isinstance(step, dict) or not isinstance(
                step.get("artifact_id"), str
            ):
                continue
            retained = (
                db.execute(
                    "SELECT 1 FROM artifacts WHERE artifact_id=?",
                    (step["artifact_id"],),
                ).fetchone()
                is not None
            )
            result[
                "result_artifacts_retained" if retained else "missing_result_artifacts"
            ] += 1
            if retained:
                unique_results.add(step["artifact_id"])
    result["unique_result_artifacts_retained"] = len(unique_results)
    return result


def _usage(db):
    result = {
        "scope": "retained-session lifetime counters; no metric date filter",
        "sessions_sampled": 0,
        "complete": True,
        "host_usage_totals": {},
        "host_usage_reporting_events": {},
        "estimated_rendered_tokens": 0,
        "estimate_events": 0,
        "provider_usage_known": False,
        "monetary_cost": None,
        "cost_status": "No configured prices; usage is host-reported when present",
        "usage_coverage": "Only supplied usage fields are known; no automatic provider billing observation",
    }
    if not _table(db, "continuity_usage"):
        result["available"] = False
        return result
    rows = db.execute(
        "SELECT counters FROM continuity_usage ORDER BY session_id LIMIT ?",
        (SAMPLE_LIMIT + 1,),
    ).fetchall()
    result["available"] = True
    result["complete"] = len(rows) <= SAMPLE_LIMIT
    for row in rows[:SAMPLE_LIMIT]:
        counters = json.loads(row[0])
        result["sessions_sampled"] += 1
        for field in ("host_usage_totals", "host_usage_reporting_events"):
            for key, value in counters[field].items():
                result[field][key] = result[field].get(key, 0) + value
        for field in ("estimated_rendered_tokens", "estimate_events"):
            result[field] += counters[field]
    if _table(db, "continuity_retention"):
        result["expired_lifetime_usage"] = json.loads(
            db.execute(
                "SELECT retired_usage FROM continuity_retention WHERE singleton=1"
            ).fetchone()[0]
        )
    result["provider_usage_known"] = any(result["host_usage_reporting_events"].values())
    result["monetary_cost"] = None
    result["cost_status"] = "No configured prices; usage is host-reported when present"
    return result


def metrics(workbook, days=30, tool=None, *, limit=20, offset=0):
    """Read bounded daily aggregates plus clearly separated retained metadata.

    Pagination is a live aggregate view, not a repeatable snapshot. Totals from
    session/run samples must not be added to measured invocation counters.
    """
    authorize_capability(current_principal(), "read")
    if (
        type(days) is not int
        or not 1 <= days <= RETENTION_DAYS
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(offset) is not int
        or not 0 <= offset <= RETENTION_DAYS * (MAX_GROUPS + 1)
    ):
        raise ValueError("days must be 1–90, limit 1–100, and offset bounded")
    if tool is not None and (
        not isinstance(tool, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", tool)
    ):
        raise ValueError("Invalid tool filter")
    start = (_today() - timedelta(days=days - 1)).isoformat()
    groups = {}
    with workbook.connect() as db:
        db.execute("BEGIN")
        available = _table(db, "observation_daily")
        if available:
            rows = db.execute(
                "SELECT tool,tokenizer,counters FROM observation_daily WHERE day>=? AND day<=? AND (? IS NULL OR tool=?) ORDER BY day,tool,tokenizer",
                (start, _today().isoformat(), tool, tool),
            )
            for row in rows:
                key = row["tool"], row["tokenizer"]
                total = groups.setdefault(key, _empty())
                _add(total, _read_counters(row["counters"]))
        retained = _retained_tasks(db)
        usage = _usage(db)
    items = [
        {
            "tool": key[0],
            "tokenizer": key[1],
            **value,
            "mean_elapsed_seconds": value["elapsed_seconds"] / value["calls"]
            if value["calls"]
            else None,
        }
        for key, value in sorted(groups.items())
    ]
    complete = offset + limit >= len(items)
    return {
        "measurement_version": MEASUREMENT_VERSION,
        "period": {
            "from_utc": start,
            "through_utc": _today().isoformat(),
            "retention_days": RETENTION_DAYS,
        },
        "instrumented": available,
        "groups": items[offset : offset + limit],
        "groups_total": len(items),
        "complete": complete,
        "next_offset": None if complete else offset + limit,
        "coverage": "Observed opt-in calls only; zero samples do not establish zero work or cost",
        "history_scope": "Retained task/session samples are separate observations; do not add them to instrumented call counters",
        "cache_scope": "Execution reply step-result reuse (SQL cache or pinned resume); reopening stored reports does not count as execution",
        "content_scope": "One compact JSON encoding of the delivered data versus the sum of delivered text-block tokens; structured-only replies use the same JSON. Excludes MCP envelopes, escaping and duplicate representations; not provider savings",
        "wire_scope": "Actual serialized MCP result tokens, including any structuredContent duplicate; observed separately and never counted as content reduction",
        "legacy_scope": "Unversioned/v1 token comparisons used a double-representation control and are excluded from corrected token sums. Their call/latency/cache counts remain; excluded calls and samples are explicit",
        "embedding_scope": "Per-call scored/indexed chunk observations; repeated calls may count the same chunks",
        "retained_tasks": retained,
        "session_usage": usage,
    }
