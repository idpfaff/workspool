"""Full outputs and bounded preparation, independent of SQL previews."""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

SPEC = importlib.util.spec_from_file_location(
    "public_table_stress",
    Path(__file__).parents[1] / "benchmarks/public_table_stress.py",
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


def configuration(path, updated):
    return {
        "name": "public-summary",
        "sources": {"records": {"files": [str(path)], "updated_files": [str(updated)]}},
        "steps": [
            {
                "name": "groups",
                "inputs": ["records"],
                "sql": "SELECT category, count(*) AS records, count(value) AS populated, sum(value) AS total FROM input1 GROUP BY category ORDER BY category NULLS LAST",
                "oracle": {
                    "source": "records",
                    "group_by": ["category"],
                    "aggregates": [
                        {"name": "records", "op": "count_rows"},
                        {"name": "populated", "op": "count", "column": "value"},
                        {"name": "total", "op": "sum", "column": "value"},
                    ],
                    "sort": [{"column": "category"}],
                },
            },
            {
                "name": "top_groups",
                "inputs": ["groups"],
                "sql": "SELECT category, records, populated, total FROM input1 ORDER BY records DESC, category, populated, total LIMIT 2",
                "oracle": {
                    "source": "groups",
                    "sort": [
                        {"column": "records", "direction": "descending"},
                        {"column": "category"},
                        {"column": "populated"},
                        {"column": "total"},
                    ],
                    "limit": 2,
                },
            },
        ],
    }


def fixture(directory):
    source, updated = directory / "records.parquet", directory / "updated.parquet"
    table = pa.table(
        {
            "category": ["α", "α", "beta", None],
            "value": pa.array([9007199254740993, None, 2, 7], type=pa.int64()),
        }
    )
    pq.write_table(table, source)
    pq.write_table(table.slice(0, 3), updated)
    return source, updated


def test_streaming_merge_partial_batch_and_typed_projection(tmp_path):
    source = tmp_path / "source.parquet"
    pq.write_table(
        pa.table(
            {
                "group": [index % 7 for index in range(100003)],
                "value": [1] * 100003,
                "unused": ["x"] * 100003,
            }
        ),
        source,
    )
    target = tmp_path / "merged.parquet"
    result = bench.merge_parquet(
        [source, source],
        target,
        columns=["group", "value"],
        types={"value": "int64"},
        batch_rows=4096,
    )
    assert result["rows"] == 200006
    assert result["largest_batch_rows"] <= 4096
    assert pq.read_schema(target).names == ["group", "value"]
    assert pq.read_schema(target).field("value").type == pa.int64()
    with pytest.raises(ValueError, match="must be new"):
        bench.merge_parquet([source], target)


def test_arrow_oracle_exact_nulls_unicode_large_integers_and_order(tmp_path):
    source, updated = fixture(tmp_path)
    config = configuration(source, updated)
    actual = bench.expected_results({"records": source}, config["steps"])
    assert actual["groups"]["rows"] == [
        ["beta", 1, 1, 2],
        ["α", 2, 1, 9007199254740993],
        [None, 1, 1, 7],
    ]
    assert actual["top_groups"]["rows"] == [
        ["α", 2, 1, 9007199254740993],
        ["beta", 1, 1, 2],
    ]
    # Arrow aggregation is repeated across small batches to check exact combining.
    specification = config["steps"][0]["oracle"]
    assert (
        bench.arrow_oracle(source, specification, batch_rows=1)["rows"]
        == actual["groups"]["rows"]
    )


def test_decimal_oracle_and_float_sum_rejection(tmp_path):
    source = tmp_path / "decimal.parquet"
    pq.write_table(
        pa.table(
            {
                "value": pa.array(
                    [
                        Decimal("9007199254740993.000000000001"),
                        Decimal("2.000000000002"),
                    ],
                    type=pa.decimal128(38, 12),
                )
            }
        ),
        source,
    )
    specification = {
        "group_by": [],
        "aggregates": [{"name": "total", "op": "sum", "column": "value"}],
    }
    assert bench.arrow_oracle(source, specification, batch_rows=1)["rows"] == [
        ["9007199254740995.000000000003"]
    ]
    floating = tmp_path / "floating.parquet"
    pq.write_table(pa.table({"value": [0.1, 0.2]}), floating)
    with pytest.raises(ValueError, match="Normalize floating"):
        bench.arrow_oracle(floating, specification)

    integers = tmp_path / "integers.parquet"
    pq.write_table(pa.table({"value": [2**63 - 1, 2**63 - 1]}), integers)
    assert bench.arrow_oracle(integers, specification)["rows"] == [[2**64 - 2]]


def test_complete_lifecycle_persistent_control_and_real_mcp(tmp_path):
    source, updated = fixture(tmp_path)
    report = bench.run_suite(
        configuration(source, updated),
        tmp_path / "out",
        counters={"test_characters_not_tokens": len},
    )
    assert report["success"], report
    assert [item["direct"]["compute_executions"] for item in report["scenarios"]] == [
        2,
        0,
        0,
        2,
        2,
    ]
    assert [
        item["workspool"]["compute_executions"] for item in report["scenarios"]
    ] == [2, 0, 0, 2, 2]
    assert report["stale_after_update"] == {"current": False, "state": "superseded"}
    assert report["mcp"]["success"]
    assert report["mcp"]["totals"]["terminal_retained_freshness"]["calls"] == 1
    assert (
        report["scenarios"][0]["workspool"]["validation"]["full_results"]["groups"][
            "rows"
        ][1][-1]
        == 9007199254740993
    )
    with pytest.raises(ValueError, match="fresh output"):
        bench.run_suite(
            configuration(source, updated), tmp_path / "out", counters={"test": len}
        )


def test_oracle_mismatch_fails_even_when_preview_would_hide_bad_row(tmp_path):
    source = tmp_path / "many.parquet"
    pq.write_table(
        pa.table(
            {
                "category": [f"G{index:02}" for index in range(20)],
                "value": [1] * 19 + [5],
            }
        ),
        source,
    )
    config = configuration(source, source)
    config["sources"]["records"].pop("updated_files")
    config["steps"] = config["steps"][:1]
    config["steps"][0]["sql"] = (
        "SELECT category, count(*) AS records, count(value) AS populated, sum(CASE WHEN category='G19' THEN 6 ELSE value END) AS total FROM input1 GROUP BY category ORDER BY category"
    )
    report = bench.run_suite(
        config, tmp_path / "wrong", counters={"test": len}, measure_mcp=False
    )
    assert not report["success"]
    assert (
        "groups:oracle_mismatch"
        in report["scenarios"][0]["workspool"]["validation"]["failures"]
    )
