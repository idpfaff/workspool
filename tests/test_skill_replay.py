"""Independent oracle and accounting guards for the real-data replay harness."""

import importlib.util
from pathlib import Path

import pytest

from agent_workbook.store import Workbook
from agent_workbook.tasks import define_task, run_task

spec = importlib.util.spec_from_file_location(
    "skill_replay_benchmark", Path(__file__).parents[1] / "benchmarks/skill_replay.py"
)
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


@pytest.fixture
def parquet(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "month.parquet"
    pq.write_table(
        pa.table(
            {
                "PULocationID": pa.array([4, 1, 4, 2, 1, None], type=pa.int64()),
                "passenger_count": pa.array([2, None, 3, -1, 4, None], type=pa.int64()),
            }
        ),
        path,
    )
    return path


def test_independent_oracle_preserves_nulls_ties_and_negative_values(parquet):
    result = benchmark.expected(parquet)
    assert result == {
        "totals": {
            "columns": ["trips", "recorded_passenger_trips", "passengers"],
            "rows": [[6, 4, 8]],
        },
        "coverage": {
            "columns": [
                "missing_passenger_trips",
                "recorded_passenger_trips",
                "passengers",
            ],
            "rows": [[2, 4, 8]],
        },
        "top_zones": {
            "columns": ["PULocationID", "trips"],
            "rows": [[1, 2], [4, 2], [2, 1], [None, 1]],
        },
    }


def test_oracle_integer_sum_does_not_wrap_at_int64(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "large.parquet"
    pq.write_table(
        pa.table({"PULocationID": [1, 1, 1], "passenger_count": [2**62] * 3}), path
    )
    result = benchmark.expected(path)
    assert result["totals"]["rows"][0][2] == 3 * 2**62
    assert isinstance(result["totals"]["rows"][0][2], int)


def test_full_materializations_and_pinned_lineage_are_checked(parquet, tmp_path):
    book = Workbook(tmp_path / "book")
    source = book.import_path(parquet, name="month")
    definition = benchmark.plan("review", source["artifact_id"])
    define_task(book, **definition)
    result = run_task(book, "review")
    oracle = benchmark.expected(parquet)
    checks = benchmark.verify_run(book, result, oracle, source["artifact_id"])
    assert len(checks) == 3 and all(
        item["exact_result"] and item["exact_dependencies"] for item in checks
    )
    assert result["omitted"] == [] and result["brief_complete"]
    retained = run_task(book, "review", base_run=result["run_id"])
    assert retained["delivery"] == "unchanged"
    assert retained["base_run"] == result["run_id"]
    with pytest.raises(AssertionError, match="binding"):
        benchmark.verify_run(book, result, oracle, "different-id")
    bad_oracle = dict(
        oracle,
        coverage={"columns": oracle["coverage"]["columns"], "rows": [[999, 4, 8]]},
    )
    with pytest.raises(AssertionError, match="independent oracle"):
        benchmark.verify_run(book, result, bad_oracle, source["artifact_id"])
    with pytest.raises(AssertionError, match="unabridged"):
        benchmark.verify_run(
            book,
            result | {"omitted": [{"reason": "brief_budget"}]},
            oracle,
            source["artifact_id"],
        )


def test_all_selected_outputs_fit_the_distillation_contract(parquet, tmp_path):
    helper = benchmark.support()
    destination = tmp_path / "typed.parquet"
    prepared = helper.merge_parquet(
        [parquet],
        destination,
        columns=benchmark.COLUMNS,
        types={name: "int64" for name in benchmark.COLUMNS},
        batch_rows=2,
    )
    assert prepared["rows"] == 6 and prepared["largest_batch_rows"] == 2
    assert all(
        len(result["rows"]) <= 5 for result in benchmark.expected(destination).values()
    )
    assert benchmark.plan("next-month", "immutable-version")["pin_sources"] is True


def test_historical_skill_origin_does_not_stale_fresh_replay(tmp_path):
    from agent_workbook.skills import distill, inspect_skill, use_skill

    book = Workbook(tmp_path / "book")

    def source(name, value, **options):
        return book.save_text(
            f"n\n{value}\n", name=name, kind="table", media_type="text/csv", **options
        )

    source("january", 1)
    february = source("february", 2)
    define_task(
        book,
        "january-review",
        "Sum observations",
        {"data": "january"},
        [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            }
        ],
    )
    run_task(book, "january-review")
    skill = distill(book, "monthly-sum", "january-review")
    source("january", 3, expected_version=1)
    assert inspect_skill(book, skill["artifact_id"])["origin_stale"]
    definition = use_skill(
        book, skill["artifact_id"], "february-review", {"data": february["artifact_id"]}
    )
    replay = run_task(book, "february-review")
    assert replay["state"] == "complete" and replay["current"]
    assert replay["steps"][0]["result"]["rows"] == [[2]]
    assert book.inspect(definition["artifact_id"])["stale_inputs"] is False
    assert book.inspect(replay["artifact_id"])["stale_inputs"] is False
    assert book.inspect(replay["steps"][0]["artifact_id"])["stale_inputs"] is False
    assert inspect_skill(book, skill["artifact_id"])["origin_stale"]


def test_accounting_includes_requests_results_and_separate_errors():
    calls = [
        {
            "arm": "transfer",
            "is_error": False,
            "request_tokens": 30,
            "response_tokens": 70,
            "elapsed_seconds": 0.2,
        },
        {
            "arm": "transfer",
            "is_error": True,
            "request_tokens": 20,
            "response_tokens": 10,
            "elapsed_seconds": 0.1,
        },
        {
            "arm": "discovery",
            "is_error": False,
            "request_tokens": 999,
            "response_tokens": 999,
            "elapsed_seconds": 9,
        },
    ]
    total = benchmark.totals(calls, "transfer")
    assert total["calls"] == 2 and total["errors"] == 1
    assert total["request_tokens"] == 50 and total["response_tokens"] == 80
    assert total["elapsed_seconds"] == pytest.approx(0.3)
    assert benchmark.totals(calls, "absent")["calls"] == 0


@pytest.mark.parametrize(
    "setup,base,candidate,want",
    [(101, 100, 50, 3), (0, 100, 50, 0), (100, 50, 50, None), (100, 50, 80, None)],
)
def test_amortization_never_claims_breakeven_for_a_losing_replay(
    setup, base, candidate, want
):
    assert benchmark.break_even(setup, base, candidate) == want
