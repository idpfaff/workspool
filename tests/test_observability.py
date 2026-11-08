"""Real measurements remain distinct from sampled history and provider billing."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pytest

from agent_workbook import continuity, tasks
from agent_workbook import observability as o
from agent_workbook.coordination import Principal, acting_as
from agent_workbook.store import Workbook


def test_same_content_signed_counts_and_missing_coverage(tmp_path):
    book = Workbook(tmp_path)
    o.record_tool(
        book,
        "query",
        0.2,
        tokenizer="o200k_base",
        content_tokens=30,
        json_content_tokens=100,
        wire_tokens=120,
        same_content=True,
    )
    o.record_tool(
        book,
        "query",
        0.4,
        tokenizer="o200k_base",
        content_tokens=50,
        json_content_tokens=40,
        wire_tokens=80,
        same_content=True,
    )
    o.record_tool(
        book,
        "query",
        0.1,
        failed=True,
        tokenizer="o200k_base",
        content_tokens=10,
        json_content_tokens=1000000,
        wire_tokens=25,
        same_content=True,
    )
    result = o.metrics(book)
    group = result["groups"][0]
    assert group["calls"] == 3 and group["errors"] == 1
    assert group["content_samples"] == 2 and group["content_missing_calls"] == 1
    assert group["content_tokens_reduced"] == 60
    assert group["content_tokens"] == 80 and group["json_content_tokens"] == 140
    assert group["elapsed_seconds"] == pytest.approx(0.7)
    assert group["wire_samples"] == 3 and group["wire_tokens"] == 225
    assert group["legacy_token_calls_excluded"] == 0
    assert result["session_usage"]["monetary_cost"] is None


def test_legacy_token_comparison_is_excluded_before_and_after_same_day_write(tmp_path):
    book = Workbook(tmp_path)
    legacy = json.dumps(
        {
            "calls": 3,
            "errors": 1,
            "elapsed_seconds": 0.7,
            "max_elapsed_seconds": 0.4,
            "cache_hits": 2,
            "formatting_samples": 2,
            "formatting_missing_calls": 1,
            "actual_tokens": 100,
            "full_tokens": 300,
            "formatting_tokens_reduced": 200,
        }
    )
    with book.connect() as db:
        db.executescript(o.SCHEMA)
        db.execute(
            "INSERT INTO observation_daily VALUES(?,?,?,?)",
            (o._today().isoformat(), "query", "o200k_base", legacy),
        )
    before = o.metrics(book)
    group = before["groups"][0]
    assert before["measurement_version"] == 2
    assert group["calls"] == 3 and group["cache_hits"] == 2
    assert group["content_tokens"] == group["json_content_tokens"] == 0
    assert group["content_tokens_reduced"] == group["wire_tokens"] == 0
    assert group["legacy_token_calls_excluded"] == group["content_missing_calls"] == 3
    assert group["legacy_formatting_samples_excluded"] == 2
    assert "actual_tokens" not in group and "formatting_tokens_reduced" not in group
    with book.connect() as db:
        assert (
            db.execute("SELECT counters FROM observation_daily").fetchone()[0] == legacy
        )
    o.record_tool(
        book,
        "query",
        0.1,
        tokenizer="o200k_base",
        content_tokens=20,
        json_content_tokens=20,
        wire_tokens=60,
        same_content=True,
    )
    after = o.metrics(book)
    group = after["groups"][0]
    assert group["calls"] == 4 and group["cache_hits"] == 2
    assert group["content_tokens"] == group["json_content_tokens"] == 20
    assert group["content_tokens_reduced"] == 0 and group["wire_tokens"] == 60
    assert group["content_samples"] == 1 and group["content_missing_calls"] == 3
    assert group["legacy_token_calls_excluded"] == 3
    assert group["legacy_formatting_samples_excluded"] == 2
    assert o.metrics(book) == after  # migrated exclusions are never counted twice
    with book.connect() as db:
        stored = json.loads(
            db.execute("SELECT counters FROM observation_daily").fetchone()[0]
        )
    assert stored["measurement_version"] == 2 and "actual_tokens" not in stored


def test_concurrent_upgrade_preserves_legacy_coverage_exactly_once(tmp_path):
    book = Workbook(tmp_path)
    with book.connect() as db:
        db.executescript(o.SCHEMA)
        db.execute(
            "INSERT INTO observation_daily VALUES(?,?,?,?)",
            (
                o._today().isoformat(),
                "query",
                "o200k_base",
                json.dumps(
                    {
                        "calls": 2,
                        "formatting_samples": 2,
                        "formatting_tokens_reduced": 1000000,
                    }
                ),
            ),
        )

    def sample(_):
        o.record_tool(
            book,
            "query",
            0.1,
            tokenizer="o200k_base",
            content_tokens=10,
            json_content_tokens=10,
            wire_tokens=35,
            same_content=True,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(sample, range(8)))
    group = o.metrics(book)["groups"][0]
    assert group["calls"] == 10 and group["content_samples"] == 8
    assert (
        group["legacy_token_calls_excluded"]
        == group["legacy_formatting_samples_excluded"]
        == 2
    )
    assert group["content_tokens"] == group["json_content_tokens"] == 80
    assert group["content_tokens_reduced"] == 0 and group["wire_tokens"] == 280


def test_result_measurements_count_only_observed_fields_and_never_store_body(tmp_path):
    book = Workbook(tmp_path)
    value = {
        "private": "SECRET_BODY",
        "steps": [
            {
                "artifact_id": "wb_example",
                "cache": {"hit": True},
                "elapsed_seconds": 0.03,
            },
            {"cache": {"hit": False, "reason": "configured_worker"}},
        ],
        "dense_complete": False,
        "mode": "lexical",
        "dense_coverage": {
            "indexed_chunks": 10,
            "scored_chunks": 6,
            "pending_chunks": 4,
        },
    }
    o.record_tool(book, "task_run", 0.1, value)
    group = o.metrics(book)["groups"][0]
    assert group["cache_hits"] == group["cache_bypassed"] == 1
    assert group["cache_misses"] == 0
    assert group["timed_steps"] == group["steps_without_timing"] == 1
    assert group["pending_chunks"] == 4
    assert group["dense_coverage_samples"] == 1
    with book.connect() as db:
        assert (
            "SECRET_BODY"
            not in db.execute("SELECT counters FROM observation_daily").fetchone()[0]
        )


def test_retention_and_cardinality_overflow_do_not_mix_tokenizers(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    monkeypatch.setattr(o, "MAX_GROUPS", 2)
    today = date(2026, 11, 8)
    monkeypatch.setattr(o, "_today", lambda: today)
    for i in range(4):
        o.record_tool(
            book,
            "tool" + str(i),
            1,
            tokenizer="o200k_base",
            content_tokens=10,
            json_content_tokens=20,
            same_content=True,
        )
    result = o.metrics(book)
    overflow = next(
        group for group in result["groups"] if group["tool"] == "__overflow__"
    )
    assert len(result["groups"]) == 3 and overflow["calls"] == 2
    assert overflow["content_samples"] == overflow["content_tokens_reduced"] == 0
    today += timedelta(days=90)
    o.record_tool(book, "new", 1)
    with book.connect() as db:
        assert db.execute("SELECT count(*) FROM observation_daily").fetchone()[0] == 1


def test_reopening_run_does_not_repeat_execution_cache_or_timing(tmp_path):
    book = Workbook(tmp_path)
    report = {
        "steps": [
            {
                "cache": {"hit": True, "reason": "resumed_materialization"},
                "elapsed_seconds": 0.03,
                "artifact_id": "retained_result",
            }
        ]
    }
    o.record_tool(book, "task_run", 0.1, report)
    for _ in range(3):
        o.record_tool(
            book,
            "task_status",
            0.01,
            report,
            tokenizer="o200k_base",
            content_tokens=20,
            json_content_tokens=40,
            same_content=True,
        )
    groups = {group["tool"]: group for group in o.metrics(book)["groups"]}
    execution, reopened = groups["task_run"], groups["task_status"]
    assert execution["cache_hits"] == execution["task_steps"] == 1
    assert reopened["calls"] == reopened["content_samples"] == 3
    assert reopened["elapsed_seconds"] == pytest.approx(0.03)
    assert reopened["content_tokens_reduced"] == 60
    assert all(
        reopened[key] == 0
        for key in (
            "cache_hits",
            "cache_samples",
            "task_steps",
            "timed_steps",
            "step_elapsed_seconds",
            "result_artifact_references",
        )
    )


def test_concurrent_increments_and_group_pagination(tmp_path):
    book = Workbook(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: o.record_tool(book, "query", 0.1), range(24)))
    o.record_tool(book, "inspect", 0.2)
    first = o.metrics(book, limit=1)
    assert not first["complete"] and first["next_offset"] == 1
    second = o.metrics(book, limit=1, offset=first["next_offset"])
    assert second["complete"]
    groups = first["groups"] + second["groups"]
    assert next(g for g in groups if g["tool"] == "query")["calls"] == 24


def test_retained_task_metadata_and_host_usage_are_explicitly_separate(tmp_path):
    book = Workbook(tmp_path)
    book.save_text("n\n2\n3\n", name="numbers", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "sum",
        "Sum values",
        {"data": "numbers"},
        [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            }
        ],
    )
    tasks.run_task(book, "sum")
    tasks.run_task(book, "sum")
    session = continuity.start_session(book, Principal("host"), "Observe provider use")
    continuity.capture_event(
        session,
        "decision",
        {"summary": "Review data"},
        usage={"input_tokens": 100, "output_tokens": 12, "cached_input_tokens": 30},
    )
    result = o.metrics(book)
    assert not result["instrumented"] and result["groups"] == []
    assert result["retained_tasks"]["runs_sampled"] == 2
    assert result["retained_tasks"]["cache_hits"] >= 1
    assert result["retained_tasks"]["result_artifacts_retained"] == 2
    assert result["session_usage"]["host_usage_totals"]["input_tokens"] == 100
    assert result["session_usage"]["monetary_cost"] is None


def test_samples_are_bounded_and_read_capability_is_explicit(tmp_path, monkeypatch):
    book = Workbook(tmp_path)
    for _ in range(3):
        continuity.start_session(book, Principal("host"), "Usage")
    monkeypatch.setattr(o, "SAMPLE_LIMIT", 2)
    assert not o.metrics(book)["session_usage"]["complete"]
    with acting_as(Principal("worker", "writer", {"task:run"})):
        assert o.record_tool(book, "task_run", 0.1)
        with pytest.raises(PermissionError):
            o.metrics(book)
    with acting_as(Principal("reader", "reader")):
        assert o.metrics(book)
        with pytest.raises(PermissionError):
            o.record_tool(book, "inspect", 0.1)
    assert not o.record_tool(book, "metrics", 0.1)


@pytest.mark.parametrize(
    "args",
    [
        {"elapsed_seconds": -1},
        {"elapsed_seconds": float("nan")},
        {"elapsed_seconds": 1, "content_tokens": True},
        {"elapsed_seconds": 1, "same_content": 1},
    ],
)
def test_invalid_measurements_rejected(tmp_path, args):
    with pytest.raises(ValueError):
        o.record_tool(Workbook(tmp_path), "query", **args)
