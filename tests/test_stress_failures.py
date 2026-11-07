"""Fault harness checks must prove detection/recovery, not manufacture scale."""

import hashlib
import importlib.util
import json
from decimal import InvalidOperation
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "stress_failures", Path(__file__).parents[1] / "benchmarks/stress_failures.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.csv"
    path.write_text("ticker,t,v,c\nAAPL,1,20,1.25\nMSFT,2,30,2.50\nAAPL,3,40,1.50\n")
    return path


def test_sample_is_actual_prefix_and_never_repeated(source, tmp_path):
    sample = tmp_path / "sample.csv"
    info = bench.prepare_sample(source, sample, rows=100_000)
    assert info["sample_rows"] == 3
    assert info["sample_sha256"] == hashlib.sha256(sample.read_bytes()).hexdigest()
    shorter = tmp_path / "short.csv"
    info = bench.prepare_sample(source, shorter, rows=2)
    assert info["sample_rows"] == 2
    assert shorter.read_text().splitlines() == source.read_text().splitlines()[:3]


def test_invalid_sample_cannot_count_as_valid_real_data(tmp_path):
    source = tmp_path / "source.csv"
    source.write_text("ticker,v\nAAPL,not_numeric\n")
    with pytest.raises(InvalidOperation):
        bench.prepare_sample(source, tmp_path / "sample.csv")


def test_six_cases_report_errors_recovery_and_residue(source, tmp_path):
    pytest.importorskip("psutil")
    before = source.read_bytes()
    report = bench.run(source, tmp_path / "run", wall_seconds=30)
    assert report["all_checks_passed"], report
    assert set(report["cases"]) == set(bench.CASES)
    assert report["sample"]["sample_rows"] == 3
    assert source.read_bytes() == before
    cases = report["cases"]
    assert cases["csv_faults"]["observations"]["nonnumeric"]["rejected"]
    assert cases["csv_faults"]["observations"]["null"]["rows"] == [[4, 3, 3]]
    assert cases["parquet_and_integers"]["truncated_parquet"]["rejected"]
    assert cases["giant_cell"]["reply_bytes"] < 8192
    assert cases["timeout_recovery"]["timeout"]["error_type"] == "InterruptException"
    assert sum(row["won"] for row in cases["concurrency_lineage"]["writers"]) == 1
    assert cases["concurrency_lineage"]["stale_inputs_before_refresh"]
    assert cases["concurrency_lineage"]["refreshed_rows"] == [[1]]
    interruption = cases["interrupted_import"]
    assert interruption["catalog_atomic"] and interruption["recovery_version"] == 1
    # Cleanup behavior may improve; reporting must still disclose any residue.
    assert bool(interruption["residue_after_prune"]) == bool(interruption["limitation"])
    for case in cases.values():
        assert case["wall_seconds"] > 0
        assert case["peak_tree_rss_bytes_sampled"] > 0
        assert case["limits"]["wall_seconds"] == 30
    assert json.loads((tmp_path / "run/results.json").read_text()) == report


def test_wall_watchdog_cannot_be_reported_as_a_success(source, tmp_path):
    pytest.importorskip("psutil")
    result = bench.run_job(
        "blocked_import",
        {"book": str(tmp_path / "book"), "sample": str(source)},
        tmp_path / "worker",
        wall_seconds=1,
    )
    assert result["status"] == "fail" and result["reason"] == "wall_watchdog"
    assert result["wall_seconds"] < 10


def test_existing_output_is_never_overwritten(source, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        bench.run(source, output)


@pytest.mark.parametrize("error", [PermissionError, RuntimeError])
def test_monitor_failure_still_stops_owned_worker(monkeypatch, tmp_path, error):
    psutil = pytest.importorskip("psutil")

    class Process:
        pid = 123
        killed = waited = False

        def poll(self):
            return -9 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            assert self.killed
            self.waited = True

        def children(self, recursive):
            raise error("monitor unavailable")

    process = Process()
    monkeypatch.setattr(bench, "_spawn", lambda *args: process)
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    with pytest.raises(error, match="monitor unavailable"):
        bench.run_job("unused", {}, tmp_path)
    assert process.killed and process.waited


def test_publication_keeps_measurements_but_removes_raw_rows_and_paths():
    original = {
        "cases": {
            "csv_faults": {"message": "Bad CSV line 100002\nAAPL,private-row"},
            "parquet": {"message": "Bad file '/Users/person/data/file.parquet'\nSQL"},
            "interrupted_import": {
                "residue_before_prune": {"tmp123": 65536},
                "residue_after_prune": {"tmp123": 65536},
                "catalog_atomic": True,
            },
        }
    }
    published = bench.publication_report(original)
    serialized = json.dumps(published)
    assert "private-row" not in serialized and "/Users/" not in serialized
    assert published["cases"]["csv_faults"]["message"] == "Bad CSV line 100002"
    assert published["cases"]["interrupted_import"]["residue_after_prune"] == {
        "files": 1,
        "bytes": 65536,
    }
    assert "private-row" in original["cases"]["csv_faults"]["message"]
