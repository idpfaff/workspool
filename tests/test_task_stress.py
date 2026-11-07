"""The stress harness checks exact DAG reuse, not a predetermined speedup."""

import asyncio
import hashlib
import importlib.util
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent_workbook.query_cache import classify_sql
from agent_workbook.settings import Settings

SPEC = importlib.util.spec_from_file_location(
    "task_stress", Path(__file__).parents[1] / "benchmarks/task_stress.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def fixture(directory):
    schema = pa.schema(
        [
            ("ticker", pa.string()),
            ("t", pa.int64()),
            ("v", pa.decimal128(38, 12)),
            ("l", pa.decimal128(38, 12)),
            ("h", pa.decimal128(38, 12)),
        ]
    )
    rows = [
        {
            "ticker": f"T{ticker}",
            "t": 1704067200000 + minute * 86400000,
            "v": Decimal(ticker * 100 + minute) + Decimal("0.000000000001"),
            "l": Decimal("10.123456789012"),
            "h": Decimal("11.123456789012"),
        }
        for ticker in range(1, 9)
        for minute in range(6)
    ]
    original, updated = directory / "original.parquet", directory / "updated.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), original)
    pq.write_table(pa.Table.from_pylist(rows[:-1], schema=schema), updated)
    return original, updated


def test_all_stress_queries_are_proven_cacheable():
    for step in bench.STEPS:
        reusable, reason, _ = classify_sql(step["sql"])
        assert reusable, (step["name"], reason)


def test_retained_freshness_checks_only_terminal_results_and_policy():
    run = {
        "definition_id": "candidate-only-definition",
        "sources": {
            name: {"artifact_id": name + "-id"}
            for name in ("bars", "calendar", "policy")
        },
        "steps": [
            {"name": name, "artifact_id": name + "-id"}
            for name in ("symbols", "watchlist", "calendar_stats")
        ],
    }
    assert bench.terminal_retained_refs(run) == [
        "calendar_stats-id",
        "watchlist-id",
        "policy-id",
    ]


def test_arrow_oracle_matches_manual_decimal_and_day_counts(tmp_path):
    source, updated = fixture(tmp_path)
    oracle = bench.oracle(source)
    assert oracle["row_count"] == 48
    assert oracle["symbols"]["rows"][0] == [
        "T1",
        6,
        "615.000000000006",
        1704067200000,
        1704499200000,
        "10.123456789012",
        "11.123456789012",
    ]
    assert oracle["watchlist"]["rows"][0][0] == "T8"
    assert oracle["calendar_stats"]["rows"] == [[6, 48, 19723, 19728]]
    assert sum(row["minute_rows"] for row in oracle["calendar_rows"]) == 48
    assert bench.oracle(updated)["row_count"] == 47


def test_persistent_direct_control_reuses_results_and_rejects_corruption(tmp_path):
    source, _ = fixture(tmp_path)
    oracle = bench.oracle(source)
    calendar = tmp_path / "calendar.parquet"
    pq.write_table(pa.Table.from_pylist(oracle["calendar_rows"]), calendar)
    inputs = {
        name: {"id": name + ":1", "digest": bench.digest_file(path), "path": str(path)}
        for name, path in (("bars", source), ("calendar", calendar))
    }
    cache = bench.DirectCache(tmp_path / "cache", Settings())
    cold = cache.run(inputs)
    assert all(not step["cache_hit"] for step in cold)
    assert all(step["result"] == oracle[step["name"]] for step in cold)
    warm = bench.DirectCache(tmp_path / "cache", Settings()).run(inputs)
    assert all(step["cache_hit"] for step in warm)
    target = Path(cold[0]["artifact"]["path"])
    target.write_bytes(b"corrupted")
    repaired = cache.run(inputs)
    assert not repaired[0]["cache_hit"]
    assert all(step["result"] == oracle[step["name"]] for step in repaired)


def test_full_task_lifecycle_exact_answers_cache_counts_provenance_and_race(tmp_path):
    import tiktoken
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers

    # Test-only real byte encoding: production measurement loads explicit ranks.
    encoding = tiktoken.Encoding(
        name="task-stress-test-bytes",
        pat_str=r"(?s:.)",
        mergeable_ranks={bytes([n]): n for n in range(256)},
        special_tokens={},
    )
    counter = lambda text: len(encoding.encode(text))
    source, updated = fixture(tmp_path)
    report = bench.run_suite(
        source,
        updated,
        tmp_path / "output",
        counter=counter,
        tokenizer="test-only-byte-encoding",
    )
    assert report["success"]
    assert [
        item["workspool"]["validation"]["compute_executions"]
        for item in report["scenarios"]
    ] == [3, 0, 0, 2, 3, 2]
    assert report["source_update_during_step"]["correctly_superseded"]
    for scenario in report["scenarios"]:
        assert scenario["direct"]["success"]
        assert scenario["workspool"]["validation"]["success"]
        for output in scenario["budgeted_context"]:
            assert output["status"] in {"ok", "budget_error"}
            if output["status"] == "ok":
                assert output["tokens"] <= output["budget"]
    assert (
        report["inputs"]["source"]["sha256"]
        == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    # A local test-only byte tokenizer lets the real MCP subprocess enforce
    # budgets offline, without any provider rank-file cache or network access.
    tokenizer = Tokenizer(
        models.BPE(
            vocab={
                char: position
                for position, char in enumerate(
                    sorted(pre_tokenizers.ByteLevel.alphabet())
                )
            },
            merges=[],
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    tokenizer_path = tmp_path / "test-tokenizer.json"
    tokenizer.save(str(tokenizer_path))
    trace = asyncio.run(
        bench.mcp_trace(tmp_path / "output", str(tokenizer_path), counter, [1000, 2000])
    )
    warm = next(call for call in trace["calls"] if call["arm"] == "task_warm_run")
    assert not warm["is_error"]
    assert all(step["cache"]["hit"] for step in warm["decoded"]["steps"])
    assert trace["retained_validation"] == {
        "unchanged_receipts": True,
        "baseline_freshness_current": True,
    }
    assert trace["totals"]["prior_tools_retained_freshness"]["calls"] == 1
    assert trace["totals"]["prior_tools_retained_freshness"]["errors"] == 0
    freshness = next(
        call
        for call in trace["calls"]
        if call["arm"] == "prior_tools_retained_freshness"
    )
    assert len(freshness["decoded"]["artifacts"]) == 3
    assert trace["source_files_unchanged"]
    for arm in ("task_run_retained_receipt", "task_status_retained_receipt"):
        receipt = next(call for call in trace["calls"] if call["arm"] == arm)
        assert receipt["decoded"]["base_run"] == warm["decoded"]["run_id"]
        assert receipt["decoded"]["delivery"] == "unchanged"
        assert (
            "steps" not in receipt["decoded"] and "citations" not in receipt["decoded"]
        )
        assert receipt["response_tokens"] <= 1000
    assert trace["totals"]["prior_tools_retained_results"]["calls"] == 5
    assert trace["totals"]["prior_tools_retained_results"]["errors"] == 0
    with pytest.raises(ValueError, match="fresh output"):
        bench.run_suite(source, updated, tmp_path / "output", counter=counter)


def test_uncached_tokenizer_failure_restores_loader(tmp_path, monkeypatch):
    import tiktoken
    import tiktoken.load

    original = tiktoken.load.read_file
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(
        tiktoken,
        "get_encoding",
        lambda _: tiktoken.load.read_file_cached("https://example.invalid/ranks"),
    )
    with pytest.raises(ValueError, match="Pre-cache"):
        bench.load_counter("o200k_base")
    assert tiktoken.load.read_file is original
