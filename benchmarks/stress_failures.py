"""Bounded fault probes derived from a local CSV; no downloads or scale claims.

Run: python benchmarks/stress_failures.py --source bars.csv --output results/faults
The six cases use at most 100,000 real rows, plus explicitly labelled synthetic
edge values. Every case runs in a subprocess with wall-time and sampled process-
tree RSS watchdogs. RSS polling is a safety stop, not an OS allocation guarantee.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import duckdb

from agent_workbook.query import query, refresh
from agent_workbook.settings import Settings
from agent_workbook.store import Workbook

CASES = (
    "csv_faults",
    "parquet_and_integers",
    "giant_cell",
    "timeout_recovery",
    "concurrency_lineage",
    "interrupted_import",
)
MAX_SAMPLE_BYTES = 64 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def prepare_sample(source, output, rows=100_000):
    """Stream actual rows without replication; hash the normalized sample only."""
    if not 1 <= rows <= 100_000:
        raise ValueError("sample rows must be between 1 and 100000")
    csv.field_size_limit(1024 * 1024)
    output.parent.mkdir(parents=True, exist_ok=True)
    with source.open(newline="", encoding="utf-8") as incoming:
        reader = csv.reader(incoming, strict=True)
        fields = next(reader)
        numeric = next(
            (name for name in ("v", "volume", "c", "close") if name in fields), None
        )
        if numeric is None or len(set(fields)) != len(fields):
            raise ValueError("CSV needs unique columns and v, volume, c, or close")
        index = fields.index(numeric)
        count = nonnull = 0
        with output.open("x", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow(fields)
            for row in reader:
                require(len(row) == len(fields), "source sample has malformed rows")
                if row[index]:
                    require(
                        Decimal(row[index]).is_finite(),
                        "source numeric value is not finite",
                    )
                    nonnull += 1
                writer.writerow(row)
                count += 1
                last = row
                if target.tell() > MAX_SAMPLE_BYTES:
                    raise ValueError("sample exceeds 64 MiB; request fewer rows")
                if count == rows:
                    break
    require(count > 0, "source CSV has no data rows")
    return {
        "source_name": source.name,
        "source_bytes": source.stat().st_size,
        "sample_rows": count,
        "sample_bytes": output.stat().st_size,
        "sample_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "numeric_column": numeric,
        "nonnull_numeric_rows": nonnull,
        "fields": fields,
        "last_row": last,
    }


def _spawn(kind, payload, folder):
    folder.mkdir(parents=True, exist_ok=False)
    config = folder / "config.json"
    write_json(config, payload | {"folder": str(folder), "kind": kind})
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    with (folder / "worker.log").open("wb") as log:
        return subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(config)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )


def _stop(process):
    import psutil

    try:
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except (psutil.Error, PermissionError):
            children = []
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    finally:
        # Monitoring/descendant cleanup must never prevent stopping our worker.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def run_job(
    kind, payload, folder, *, wall_seconds=30, memory_mib=2048, stop_when_ready=False
):
    import psutil

    started = time.monotonic()
    process = _spawn(kind, payload, folder)
    peak = 0
    stopped = None
    try:
        while process.poll() is None:
            try:
                main = psutil.Process(process.pid)
                members = [main, *main.children(recursive=True)]
                rss = 0
                for member in members:
                    try:
                        rss += member.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, rss)
            except psutil.NoSuchProcess:
                pass
            if peak > memory_mib * 1024 * 1024:
                stopped = "rss_watchdog"
            elif time.monotonic() - started > wall_seconds:
                stopped = "wall_watchdog"
            elif stop_when_ready and (folder / "ready").exists():
                stopped = "intentional_interruption"
            if stopped:
                _stop(process)
                break
            time.sleep(0.02)
    finally:
        if process.poll() is None:
            _stop(process)
    result_path = folder / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if stopped or process.returncode:
        result = {
            "status": "interrupted"
            if stopped == "intentional_interruption"
            else "fail",
            "reason": stopped or "worker_exit",
            "exit_code": process.returncode,
        }
    require(bool(result), "worker exited without a result")
    return result | {
        "wall_seconds": round(time.monotonic() - started, 4),
        "peak_tree_rss_bytes_sampled": peak,
        "limits": {
            "wall_seconds": wall_seconds,
            "tree_rss_mib": memory_mib,
            "poll_seconds": 0.02,
        },
    }


def _book(folder, sample, *, timeout=15):
    book = Workbook(
        folder / "book",
        Settings(query_memory="512MB", query_threads=1, query_timeout=timeout),
    )
    book.import_path(sample, name="bars")
    return book


def _rejected(book, ref, sql):
    try:
        query(book, [ref], sql)
    except duckdb.Error as exc:
        return {
            "rejected": True,
            "error_type": type(exc).__name__,
            "message": str(exc)[:500],
        }
    raise AssertionError("corrupt input was accepted by the validating query")


def csv_faults(config):
    folder, sample = Path(config["folder"]), Path(config["sample"])
    info = config["sample_info"]
    column = '"' + info["numeric_column"].replace('"', '""') + '"'
    position = info["fields"].index(info["numeric_column"])
    book = _book(folder, sample)
    observations = {}
    for kind in ("malformed", "nonnumeric", "null"):
        path = folder / f"{kind}.csv"
        shutil.copyfile(sample, path)
        row = list(info["last_row"])
        if kind == "malformed":
            row.append("unexpected_extra_field")
        else:
            row[position] = "not_a_number" if kind == "nonnumeric" else ""
        with path.open("a", newline="", encoding="utf-8") as file:
            csv.writer(file).writerow(row)
        book.import_path(path, name=kind)
        if kind == "null":
            result = query(
                book,
                [kind],
                f"SELECT count(*), count({column}), count(CAST({column} AS DOUBLE)) FROM input1",
            )
            expected = [
                [
                    info["sample_rows"] + 1,
                    info["nonnull_numeric_rows"],
                    info["nonnull_numeric_rows"],
                ]
            ]
            require(
                result["rows"] == expected,
                "null row was lost or changed numeric counts",
            )
            observations[kind] = {"rows": result["rows"], "expected": expected}
        else:
            observations[kind] = _rejected(
                book, kind, f"SELECT sum(CAST({column} AS DOUBLE)) FROM input1"
            )
    require(
        query(book, ["bars"], "SELECT count(*) FROM input1")["rows"]
        == [[info["sample_rows"]]],
        "original sample changed",
    )
    return {
        "status": "pass",
        "observations": observations,
        "fault_position": "one appended row after the sample",
    }


def parquet_and_integers(config):
    folder = Path(config["folder"])
    book = _book(folder, Path(config["sample"]))
    saved = query(book, ["bars"], "SELECT * FROM input1", save_as="valid_parquet")[
        "saved"
    ]
    path = folder / "truncated.parquet"
    book.export(saved["artifact_id"], path)
    with path.open("r+b") as file:
        file.truncate(path.stat().st_size - 8)
    book.import_path(path, name="truncated")
    rejected = _rejected(book, "truncated", "SELECT count(*) FROM input1")
    values = [-(2**53 + 1), 2**53 + 1, 2**63 - 1]
    book.save_text(
        "value\n" + "\n".join(map(str, values)) + "\n",
        name="integers",
        kind="table",
        media_type="text/csv",
    )
    result = query(
        book,
        ["integers"],
        "SELECT value FROM input1 ORDER BY value",
        save_as="saved_integers",
    )
    expected = [[str(value)] for value in values]
    require(
        result["rows"] == expected and bool(result.get("integer_string_columns")),
        "large integer preview lost precision or type metadata",
    )
    reread = query(book, ["saved_integers"], "SELECT value FROM input1 ORDER BY value")
    require(
        reread["rows"] == expected, "large integer Parquet roundtrip lost precision"
    )
    return {
        "status": "pass",
        "truncated_parquet": rejected,
        "synthetic_integer_values": values,
        "preview_and_saved_rows": expected,
    }


def giant_cell(config):
    book = _book(Path(config["folder"]), Path(config["sample"]))
    size = 16 * 1024 * 1024
    result = query(book, ["bars"], f"SELECT repeat('x', {size}) AS giant")
    require(
        result["preview_truncated"] and len(result["rows"][0][0]) == 240,
        "giant cell preview is not explicitly clipped",
    )
    reply_bytes = len(json.dumps(result).encode())
    require(reply_bytes < 8192, "giant cell reply exceeded preview budget")
    return {
        "status": "pass",
        "synthetic_cell_bytes": size,
        "reply_bytes": reply_bytes,
        "note": "Clipping bounds the reply, not the engine's full-cell allocation.",
    }


def timeout_recovery(config):
    book = _book(Path(config["folder"]), Path(config["sample"]), timeout=0.1)
    started = time.monotonic()
    rejected = _rejected(
        book,
        "bars",
        "SELECT sum(sin(a.i) * cos(b.i)) FROM range(1000000) a(i) CROSS JOIN range(1000000) b(i)",
    )
    require(
        rejected["error_type"] == "InterruptException",
        "query failed for a reason other than its timeout",
    )
    elapsed = time.monotonic() - started
    count = query(book, ["bars"], "SELECT count(*) FROM input1")["rows"]
    require(
        count == [[config["sample_info"]["sample_rows"]]], "next query did not recover"
    )
    return {
        "status": "pass",
        "configured_query_timeout_seconds": 0.1,
        "timed_query_wall_seconds": round(elapsed, 4),
        "timeout": rejected,
        "recovery_rows": count,
        "note": "Synthetic expensive SQL; subprocess deadline also covers setup outside the SQL timer.",
    }


def _wait_for(paths, seconds=10):
    deadline = time.monotonic() + seconds
    while not all(path.exists() for path in paths):
        if time.monotonic() > deadline:
            raise TimeoutError("worker coordination deadline exceeded")
        time.sleep(0.01)


def cas_writer(config):
    folder = Path(config["folder"])
    book = Workbook(Path(config["book"]))
    (folder / "ready").touch()
    _wait_for([Path(config["gate"])])
    try:
        saved = book.import_path(
            Path(config["sample"]), name="bars", expected_version=1
        )
    except ValueError as exc:
        require("expected_version" in str(exc), "writer failed for an unrelated reason")
        return {"status": "pass", "won": False}
    return {"status": "pass", "won": True, "version": saved["version"]}


def concurrency_lineage(config):
    folder, sample = Path(config["folder"]), Path(config["sample"])
    book = _book(folder, sample)
    children = []
    gate = folder / "start"
    try:
        for index in range(2):
            child_folder = folder / f"writer{index}"
            process = _spawn(
                "cas_writer",
                {"book": str(book.root), "sample": str(sample), "gate": str(gate)},
                child_folder,
            )
            children.append((process, child_folder))
        _wait_for([path / "ready" for _, path in children])
        gate.touch()
        for process, _ in children:
            process.wait(timeout=15)
        outcomes = [
            json.loads((path / "result.json").read_text()) for _, path in children
        ]
        require(
            all(result["status"] == "pass" for result in outcomes),
            "writer process failed",
        )
        require(
            sum(result["won"] for result in outcomes) == 1,
            "compare-and-swap did not have one winner",
        )
        require(
            book.inspect("bars")["version"] == 2,
            "concurrent writes skipped or duplicated versions",
        )
    finally:
        for process, _ in children:
            if process.poll() is None:
                _stop(process)
    original = book.table_inputs
    pinned_id = book.inspect("bars")["artifact_id"]
    updated = folder / "updated.csv"
    # A distinct real-row subset makes a mixed/new-source answer detectable.
    with (
        sample.open(newline="", encoding="utf-8") as incoming,
        updated.open("w", newline="", encoding="utf-8") as target,
    ):
        reader, writer = csv.reader(incoming), csv.writer(target)
        writer.writerow(next(reader))
        writer.writerow(next(reader))

    def update_after_snapshot(refs, historical):
        resolved = original(refs, historical)
        book.import_path(updated, name="bars", expected_version=2)
        return resolved

    with patch.object(book, "table_inputs", update_after_snapshot):
        result = query(
            book, ["bars"], "SELECT count(*) AS n FROM input1", save_as="count"
        )
    require(
        result["rows"] == [[config["sample_info"]["sample_rows"]]],
        "query did not read its pinned snapshot",
    )
    details = book.inspect("count")
    require(
        details["dependencies"] == [pinned_id] and details["stale_inputs"],
        "saved lineage did not expose the concurrent source update",
    )
    renewed = refresh(book, "count")
    require(
        renewed["rows"] == [[1]] and not renewed["saved"]["stale_inputs"],
        "refresh did not use the new source",
    )
    return {
        "status": "pass",
        "writers": outcomes,
        "pinned_query_rows": result["rows"],
        "stale_inputs_before_refresh": details["stale_inputs"],
        "refreshed_rows": renewed["rows"],
        "interleaving": "controlled source update after table_inputs snapshot, before DuckDB opens the pinned file",
    }


def blocked_import(config):
    folder = Path(config["folder"])
    book = Workbook(Path(config["book"]))
    with Path(config["sample"]).open("rb") as source:

        class InterruptedStream:
            first = True

            def read(self, size):
                if self.first:
                    self.first = False
                    return source.read(min(size, 65536))
                (folder / "ready").touch()
                while True:
                    time.sleep(1)

        book._save(
            InterruptedStream(), name="interrupted", kind="table", media_type="text/csv"
        )


def interrupted_import(config):
    folder = Path(config["folder"])
    root = folder / "book"
    book = Workbook(root)
    result = run_job(
        "blocked_import",
        {"book": str(root), "sample": config["sample"]},
        folder / "writer",
        wall_seconds=10,
        stop_when_ready=True,
    )
    require(
        result["reason"] == "intentional_interruption",
        "import was not interrupted at the staging checkpoint",
    )
    require(book.find() == [], "interrupted import published a catalog record")
    before = {
        path.name: path.stat().st_size
        for path in book.blobs.iterdir()
        if path.is_file()
    }
    cleanup = book.prune()
    after = {
        path.name: path.stat().st_size
        for path in book.blobs.iterdir()
        if path.is_file()
    }
    recovered = book.import_path(Path(config["sample"]), name="recovered")
    return {
        "status": "pass",
        "catalog_atomic": True,
        "interruption": result,
        "residue_before_prune": before,
        "residue_after_prune": after,
        "cleanup": cleanup,
        "recovery_version": recovered["version"],
        "limitation": "Unreferenced staging files remain after hard termination."
        if after
        else None,
    }


WORKERS = {name: globals()[name] for name in (*CASES, "cas_writer", "blocked_import")}


def publication_report(report):
    """Keep measured aggregates while removing raw error rows and local paths."""

    def clean(value, key=None):
        if isinstance(value, dict):
            return {name: clean(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            if key == "message":
                value = value.splitlines()[0] if value else value
            return re.sub(
                r"/(?:Users|home|private|tmp|var)/[^\s'\"<>]*", "<local-path>", value
            )
        return value

    result = clean(report)
    interrupted = result["cases"]["interrupted_import"]
    for key in ("residue_before_prune", "residue_after_prune"):
        residue = interrupted[key]
        interrupted[key] = {"files": len(residue), "bytes": sum(residue.values())}
    result["publication_notes"] = (
        "Error messages retain only their first line with local paths removed. "
        "Six passed correctness/recovery checks do not imply residue-free cleanup; "
        "interrupted-import staging residue is reported explicitly."
    )
    return result


def execute_worker(config_path):
    config = json.loads(config_path.read_text())
    try:
        result = WORKERS[config["kind"]](config)
    except Exception as exc:  # noqa: BLE001 -- isolate and record any worker failure
        result = {
            "status": "fail",
            "error_type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
    write_json(Path(config["folder"]) / "result.json", result)


def run(source, output, *, rows=100_000, wall_seconds=30, memory_mib=2048):
    if not 5 <= wall_seconds <= 300 or not 128 <= memory_mib <= 8192:
        raise ValueError("wall seconds must be 5–300 and memory MiB 128–8192")
    output.mkdir(parents=True, exist_ok=False)
    sample = output / "sample.csv"
    info = prepare_sample(source.resolve(), sample, rows)
    payload = {"sample": str(sample.resolve()), "sample_info": info}
    results = {
        name: run_job(
            name,
            payload,
            output / name,
            wall_seconds=wall_seconds,
            memory_mib=memory_mib,
        )
        for name in CASES
    }
    require(
        hashlib.sha256(sample.read_bytes()).hexdigest() == info["sample_sha256"],
        "fault probes changed the original sample",
    )
    report = {
        "scope": "Six failure/recovery probes on a bounded real CSV sample; synthetic faults and edge values are not large-data scale evidence.",
        "sample": {
            key: value
            for key, value in info.items()
            if key not in ("last_row", "fields")
        },
        "duckdb_version": duckdb.__version__,
        "all_checks_passed": all(case["status"] == "pass" for case in results.values()),
        "cases": results,
        "resource_notes": "512 MB DuckDB memory and one thread per normal worker; sampled RSS includes descendants, may overshoot between polls, and is not an allocation cap. Known cleanup residue is reported separately from catalog/recovery assertions.",
    }
    write_json(output / "results.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--wall-seconds", type=float, default=30)
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        execute_worker(args.worker)
        return
    if args.source is None or args.output is None:
        parser.error("--source and --output are required")
    report = run(
        args.source,
        args.output.resolve(),
        rows=args.rows,
        wall_seconds=args.wall_seconds,
        memory_mib=args.memory_mib,
    )
    print(
        json.dumps(
            {
                "all_checks_passed": report["all_checks_passed"],
                "results": str(args.output / "results.json"),
            }
        )
    )
    raise SystemExit(0 if report["all_checks_passed"] else 1)


if __name__ == "__main__":
    main()
