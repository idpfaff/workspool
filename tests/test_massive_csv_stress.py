"""CSV stress validates full streams against an independent Arrow oracle."""

import asyncio
import csv
import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "massive_csv_stress", Path(__file__).parents[1] / "benchmarks/massive_csv_stress.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


@pytest.fixture
def offline_tokenizers(monkeypatch):
    """Use real byte-level tiktoken only in tests, without downloaded rank files."""
    import tiktoken
    import tiktoken.load

    encoder = tiktoken.Encoding(
        name="test-only-utf8-bytes",
        pat_str=r"(?s:.)",
        mergeable_ranks={bytes([value]): value for value in range(256)},
        special_tokens={},
    )
    text = "ASCII, café, 漢字, 🌍\n"
    assert len(encoder.encode(text)) == len(text.encode("utf-8"))
    assert encoder.decode(encoder.encode(text)) == text
    original_read_file = tiktoken.load.read_file

    def get_encoding(name):
        assert name in {"cl100k_base", "o200k_base"}
        return encoder

    monkeypatch.setattr(tiktoken, "get_encoding", get_encoding)
    yield
    assert tiktoken.load.read_file is original_read_file


def test_offline_encoders_restore_loader_after_uncached_failure(tmp_path, monkeypatch):
    import tiktoken
    import tiktoken.load

    original_read_file = tiktoken.load.read_file
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-cache"))

    def uncached_encoding(name):
        return tiktoken.load.read_file_cached("https://example.invalid/uncached-ranks")

    monkeypatch.setattr(tiktoken, "get_encoding", uncached_encoding)
    with pytest.raises(ValueError, match="already be in the local cache"):
        bench.offline_encoders()
    assert tiktoken.load.read_file is original_read_file


def fixture(directory, rows=20):
    directory.mkdir()
    records = {}
    for ticker in ("AAPL", "MSFT"):
        path = directory / (ticker + ".csv")
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(bench.COLUMNS)
            for index in range(rows // 2):
                writer.writerow(
                    [
                        ticker,
                        1704205800000 + index * 60000,
                        "10.123456789012",
                        12,
                        9,
                        11,
                        "2.000000000001",
                        3,
                    ]
                )
        records[ticker] = {
            "ticker": ticker,
            "status": "complete",
            "file": path.name,
            "columns": list(bench.COLUMNS),
            "rows": rows // 2,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (directory / "manifest.json").write_text(
        json.dumps({"complete": True, "tickers": list(records), "symbols": records})
    )
    return directory


def test_concat_oracle_direct_and_actual_mcp_smoke(tmp_path, offline_tokenizers):
    source = tmp_path / "full.csv"
    data = fixture(tmp_path / "data")
    combined = bench.concatenate(data, source)
    assert combined["rows"] == 20
    assert source.read_text().count("ticker,t,o,h,l,c,v,n\n") == 1
    assert bench.concatenate(data, source)["reused"]
    oracle = bench.arrow_oracle(source, 20, block_size=1024)
    assert oracle["aggregate"][:2] == [20, 2]
    assert Decimal(oracle["aggregate"][4]) == Decimal("40.000000000020")
    config = {
        "csv": str(source),
        "memory": "1GB",
        "query_timeout": 15,
        "oracle": oracle,
        "workspace": str(tmp_path / "workbook"),
    }
    direct = bench.direct(config)
    result = asyncio.run(bench.mcp(config))
    assert direct["status"] == result["status"] == "ok"
    assert [call["tool"] for call in result["calls"]] == ["import", "query"]
    assert result["result"]["rows"] == direct["result"]["rows"]
    assert all(count > 0 for count in result["payload_tokens"].values())


def test_streaming_100000_rows_are_all_counted_with_bounded_batches(tmp_path):
    source = tmp_path / "full.csv"
    data = fixture(tmp_path / "data", rows=100000)
    bench.concatenate(data, source)
    oracle = bench.arrow_oracle(source, 100000, block_size=65536)
    assert oracle["rows"] == 100000 and oracle["maximum_batch_rows"] < 2000
    assert oracle["aggregate"][-1] == 100000
    assert Decimal(oracle["aggregate"][4]) == Decimal("200000.000000100000")
    assert Decimal(oracle["aggregate"][5]) == 300000
    with pytest.raises(ValueError, match="row count"):
        bench.arrow_oracle(source, 99999)


def test_incomplete_manifest_corruption_and_nonincreasing_rows_fail(tmp_path):
    data = fixture(tmp_path / "data")
    source = tmp_path / "full.csv"
    manifest_path = data / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps(manifest | {"complete": False}))
    with pytest.raises(ValueError, match="complete"):
        bench.concatenate(data, source)
    manifest_path.write_text(json.dumps(manifest))
    first = data / "AAPL.csv"
    first.write_text(first.read_text().replace("10.123456789012", "10.123456789013", 1))
    with pytest.raises(ValueError, match="digest"):
        bench.concatenate(data, source)
    assert not source.exists()
    source.write_text(
        "ticker,t,o,h,l,c,v,n\nAAPL,1,10,12,9,11,2,3\nAAPL,1,10,12,9,11,2,3\n"
    )
    with pytest.raises(ValueError, match="Nonincreasing"):
        bench.arrow_oracle(source)


def test_oracle_rejects_invalid_ohlc_and_preview_truncation(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_text("ticker,t,o,h,l,c,v,n\nAAPL,1,10,8,9,11,2,3\n")
    with pytest.raises(ValueError, match="OHLC"):
        bench.arrow_oracle(source)
    with pytest.raises(ValueError, match="Incomplete"):
        bench.check(
            {"columns": list(bench.AGG_COLUMNS), "rows": [], "preview_truncated": True},
            {"aggregate": []},
        )


def test_decimal_mismatch_retains_observed_direct_and_mcp_results(
    tmp_path, offline_tokenizers
):
    source = tmp_path / "precision.csv"
    source.write_text("ticker,t,o,h,l,c,v,n\nAAPL,1,10,12,9,11,10000000.001,3\n")
    oracle = bench.arrow_oracle(source, 1)
    config = {
        "csv": str(source),
        "memory": "1GB",
        "query_timeout": 15,
        "oracle": oracle,
        "workspace": str(tmp_path / "workbook"),
        "trace_path": str(tmp_path / "trace.json"),
    }
    direct = bench.direct(config)
    actual_mcp = asyncio.run(bench.mcp(config))
    for result in (direct, actual_mcp):
        assert result["status"] == "validation_failed"
        assert result["result"]["rows"][0][0] == 1
        assert result["validation"]["mismatches"] == [
            {
                "column": "total_volume",
                "actual": "10000000.000999999488",
                "expected": "10000000.001000000000",
                "difference": "-5.12E-10",
            }
        ]
    assert len(actual_mcp["calls"]) == 2 and actual_mcp["payload_tokens"]
    trace = json.loads((tmp_path / "trace.json").read_text())
    assert trace["calls"] == actual_mcp["calls"]


def test_nested_exception_group_keeps_leaf_failures():
    error = ExceptionGroup(
        "outer",
        [ValueError("first"), ExceptionGroup("inner", [RuntimeError("second")])],
    )
    assert bench.failure_details(error) == [
        {"error_type": "ValueError", "error": "first"},
        {"error_type": "RuntimeError", "error": "second"},
    ]
