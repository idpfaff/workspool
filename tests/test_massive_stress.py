"""Independent decimal checks and a bounded 100k-row preparation stress test."""

import csv
import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
SPEC = importlib.util.spec_from_file_location(
    "massive_stress", Path(__file__).parents[1] / "benchmarks/massive_stress.py"
)
stress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stress)


def record(**changes):
    return dict(
        ticker="TÉST",
        t="1704186000000",
        o="1.25",
        h="2.5",
        l="0.5",
        c="1.75",
        v="0.125",
        n="",
        **changes,
    )


def test_decimal_precision_and_nulls_are_preserved_without_float_roundtrip():
    row = stress.canonical_row(record())
    assert row[0] == "TÉST" and row[1] == 1704186000000
    assert row[6] == Decimal("0.125") and row[7] is None
    large = Decimal("12345678901234567890123456.123456789012")
    assert stress.scalar(large) == str(large)
    original = record()
    original["n"] = "9007199254740993.125"
    assert stress.canonical_row(original)[7] == Decimal("9007199254740993.125")


@pytest.mark.parametrize(
    "field,value",
    [("v", "NaN"), ("t", "1.5"), ("o", "0.0000000000001"), ("ticker", "")],
)
def test_lossy_or_invalid_values_are_rejected(field, value):
    source = record()
    source[field] = value
    with pytest.raises(ValueError):
        stress.canonical_row(source)


def test_arrow_and_python_oracles_agree_on_nulls_and_timestamp_duplicates():
    first = record()
    second = record()
    second.update(v="", n="9007199254740993.125", c="1.0")
    rows = [stress.canonical_row(first), stress.canonical_row(second)]
    schema = pa.schema(
        [
            ("ticker", pa.string()),
            ("t", pa.int64()),
            *[(name, pa.decimal128(38, 12)) for name in stress.DECIMALS],
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([row[index] for row in rows], type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )
    python, arrow = stress.Oracle(), stress.Oracle()
    for row in rows:
        python.add(row)
    arrow.add_table(table)
    assert python.snapshot() == arrow.snapshot()
    assert arrow.snapshot()["nonincreasing_timestamp_count"] == 1
    assert arrow.snapshot()["per_ticker_rows"] == {"TÉST": 2}


@pytest.fixture(scope="module")
def prepared_100k(tmp_path_factory):
    directory = tmp_path_factory.mktemp("massive-100k")
    source = directory / "synthetic.csv"
    rows = 100_003
    with source.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(stress.COLUMNS)
        for index in range(rows):
            writer.writerow(
                [
                    f"T{index % 3}",
                    1704186000000 + index * 60000,
                    "1.25",
                    "2.5",
                    "0.5",
                    "1.5" if index % 2 == 0 else "1.0",
                    "0.25",
                    "" if index % 7 == 0 else str(9007199254740993 + index),
                ]
            )
    result = stress.prepare(
        {
            "prepared": str(directory / "prepared"),
            "sources": [str(source)],
            "rows": [rows],
            "batch_rows": 2048,
            "decimal_scale": 12,
        }
    )
    return result, directory, rows


def test_preparation_streams_100k_rows_with_exact_partial_boundary(prepared_100k):
    result, _, rows = prepared_100k
    assert result["source_rows_used"] == rows
    assert 0 < result["maximum_batch_rows"] <= 2048
    for size in (rows - 1, rows):
        snapshot = result["snapshots"][str(size)]
        expected_trades = sum(
            9007199254740993 + index for index in range(size) if index % 7
        )
        assert snapshot["oracle"]["aggregate"] == [
            size,
            3,
            1704186000000,
            1704186000000 + (size - 1) * 60000,
            stress.scalar(Decimal(size) / 4),
            str(expected_trades),
            "0.5",
            "2.5",
            (size + 1) // 2,
        ]
        assert snapshot["oracle"]["strictly_increasing_timestamps_per_ticker"]
        metadata = pq.ParquetFile(snapshot["path"]).metadata
        assert metadata.num_rows == size
        assert all(
            metadata.row_group(index).num_rows <= 2048
            for index in range(metadata.num_row_groups)
        )
        assert stress.digest_file(snapshot["path"]) == snapshot["sha256"]


def test_direct_query_and_saved_window_match_independent_100k_oracle(prepared_100k):
    result, directory, rows = prepared_100k
    snapshot = result["snapshots"][str(rows)]
    config = {
        "source": snapshot["path"],
        "query_threads": 2,
        "query_memory": "1GB",
        "query_timeout": 15,
    }
    aggregate = stress.direct_query(config | {"sql": stress.AGG_SQL})
    stress.check_aggregate(aggregate["result"], snapshot["oracle"])
    saved_path = directory / "direct-window.parquet"
    stress.direct_query(
        config | {"sql": stress.saved_sql(10_000), "save_path": str(saved_path)}
    )
    verified = stress.verify_saved(saved_path, snapshot["sample_oracle"], 1024)
    assert verified["rows"] > 1000 and verified["sequence_verified"]


def test_truncated_aggregate_cannot_pass_verification():
    expected = {"aggregate": [1] * len(stress.AGG_COLUMNS)}
    result = {
        "columns": list(stress.AGG_COLUMNS),
        "rows": [[1] * len(stress.AGG_COLUMNS)],
        "preview_truncated": True,
    }
    with pytest.raises(AssertionError, match="incomplete"):
        stress.check_aggregate(result, expected)


def test_insufficient_source_never_replicates_rows(tmp_path):
    source = tmp_path / "small.csv"
    with source.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=stress.COLUMNS)
        writer.writeheader()
        writer.writerow(record())
    with pytest.raises(ValueError, match="No replication"):
        stress.prepare(
            {
                "prepared": str(tmp_path / "prepared"),
                "sources": [str(source)],
                "rows": [3],
                "batch_rows": 32,
                "decimal_scale": 12,
            }
        )


def test_disk_preflight_refuses_unavailable_budget(tmp_path):
    (tmp_path / "payload").write_bytes(b"12345")
    assert (
        stress.resource_problem(tmp_path, disk_limit=4, free_minimum=0)
        == "output_disk_limit"
    )
    assert (
        stress.resource_problem(tmp_path, disk_limit=100, free_minimum=2**100)
        == "minimum_free_disk"
    )
