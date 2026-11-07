"""Cache reuse must preserve exact data, bounded work, and process ownership."""

import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from agent_workbook.materialize import lossless_parquet_projection
from agent_workbook.query_cache import (
    CacheError,
    CacheInput,
    CacheLimitError,
    CacheSpec,
    CacheTimeout,
    Computed,
    QueryCache,
    classify_sql,
)
from agent_workbook.store import Workbook


@pytest.fixture
def workbook(tmp_path):
    wb = Workbook(tmp_path / "workbook")
    wb.save_text(
        "n\n9007199254740993\n2\n", name="data", kind="table", media_type="text/csv"
    )
    return wb


def spec_for(workbook, sql="SELECT count(*) AS n FROM input1", name="data", **kwargs):
    item, path = workbook.table_inputs([name], True)[0]
    return CacheSpec(sql, [CacheInput.from_table(item, path)], **kwargs)


def test_materialized_reuse_retains_exact_large_integer_and_type_conversion(
    workbook, tmp_path
):
    spec = spec_for(workbook, "SELECT sum(n)+100 AS total FROM input1")
    first = QueryCache(workbook.root).execute(
        spec, destination=tmp_path / "first.parquet"
    )
    second = QueryCache(workbook.root).execute(
        spec, destination=tmp_path / "second.parquet"
    )
    assert not first.hit and second.hit
    assert first.payload == second.payload
    assert first.payload["rows"] == [["9007199254741095"]]
    assert first.payload["integer_string_columns"] == [
        {"index": 0, "sql_type": "HUGEINT"}
    ]
    assert (
        first.materialized_type_conversions
        == second.materialized_type_conversions
        == [
            {
                "column": "total",
                "source_type": "HUGEINT",
                "stored_type": "DECIMAL(38,0)",
            }
        ]
    )
    assert first.materialized_sha256 == second.materialized_sha256
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT total FROM read_parquet(?)", [str(second.path)]
        ).fetchone()[0] == Decimal(9007199254741095)


def test_hugeint_outside_decimal_range_fails_instead_of_rounding(workbook, tmp_path):
    spec = spec_for(
        workbook,
        "SELECT 170141183460469231731687303715884105727::HUGEINT AS n FROM input1 ORDER BY ALL",
    )
    with pytest.raises(CacheError, match="DECIMAL|decimal"):
        QueryCache(workbook.root).execute(spec, destination=tmp_path / "unsafe.parquet")
    assert not (tmp_path / "unsafe.parquet").exists()


def test_nested_hugeint_requires_explicit_projection():
    with duckdb.connect() as db:
        relation = db.sql("SELECT {'value': 1::HUGEINT} AS nested")
        with pytest.raises(ValueError, match="nested HUGEINT"):
            lossless_parquet_projection(relation)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT random() FROM input1 ORDER BY ALL",
        "SELECT uuid() FROM input1 ORDER BY ALL",
        "SELECT current_timestamp FROM input1 ORDER BY ALL",
        "SELECT CAST('now' AS TIMESTAMP) FROM input1 ORDER BY ALL",
        "SELECT CAST('today' AS DATE) FROM input1 ORDER BY ALL",
        "SELECT n::TIMESTAMP FROM input1 ORDER BY ALL",
        "SELECT * FROM input1 LIMIT 1",
        "SELECT n,random() FROM input1 ORDER BY ALL",
        "SELECT * FROM read_parquet('unlisted.parquet') ORDER BY ALL",
        "SELECT row_number() OVER () FROM input1 ORDER BY ALL",
        "SELECT DISTINCT ON(n) n FROM input1 ORDER BY ALL",
    ],
)
def test_unproven_or_context_dependent_queries_are_not_reusable(sql):
    assert not classify_sql(sql)[0]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(n)+100 AS total FROM input1",
        "SELECT count(*) FILTER (WHERE n>100) AS total FROM input1",
        "SELECT n FROM input1 ORDER BY n",
        "SELECT n AS renamed FROM input1 ORDER BY renamed",
        "SELECT * FROM input1 ORDER BY ALL",
        "SELECT n,count(*) FROM input1 GROUP BY n ORDER BY n",
    ],
)
def test_literal_arithmetic_and_complete_ordering_are_supported(sql):
    assert classify_sql(sql)[0]


def test_typed_parameters_exact_sql_settings_and_input_order_bind_keys(workbook):
    cache = QueryCache(workbook.root)
    base = spec_for(workbook, "SELECT ? AS value", parameters=[1], materialize=False)
    integer = cache.execute(base)
    assert cache.execute(base).hit
    boolean = cache.execute(replace(base, parameters=[True]))
    floating = cache.execute(replace(base, parameters=[1.0]))
    string = cache.execute(replace(base, parameters=["1"]))
    whitespace = cache.execute(replace(base, sql="SELECT  ? AS value"))
    settings = cache.execute(replace(base, settings={"query_threads": 1}))
    assert (
        len(
            {
                item.key
                for item in (integer, boolean, floating, string, whitespace, settings)
            }
        )
        == 6
    )
    assert all(
        not item.hit for item in (boolean, floating, string, whitespace, settings)
    )
    workbook.save_text(
        "n\n1\n2\n3\n", name="other", kind="table", media_type="text/csv"
    )
    inputs = [
        CacheInput.from_table(*table)
        for table in workbook.table_inputs(["data", "other"], False)
    ]
    first = cache.execute(
        CacheSpec("SELECT count(*) AS n FROM input1", inputs, materialize=False)
    )
    second = cache.execute(
        CacheSpec(
            "SELECT count(*) AS n FROM input1",
            list(reversed(inputs)),
            materialize=False,
        )
    )
    assert (
        first.key != second.key
        and first.payload["rows"] == [[2]]
        and second.payload["rows"] == [[3]]
    )


def test_csv_explicit_decimal_types_fix_precision_and_are_keyed(workbook, tmp_path):
    workbook.save_text(
        "v\n0.1\n0.2\n", name="decimal", kind="table", media_type="text/csv"
    )
    automatic = spec_for(workbook, "SELECT sum(v) AS total FROM input1", name="decimal")
    cache = QueryCache(workbook.root)
    first = cache.execute(automatic, destination=tmp_path / "auto1.parquet")
    second = cache.execute(automatic, destination=tmp_path / "auto2.parquet")
    assert not first.hit and not second.hit and first.key != second.key
    assert first.reason == "floating_aggregate"
    typed = replace(
        automatic,
        inputs=[replace(automatic.inputs[0], csv_types={"v": "DECIMAL(38,12)"})],
    )
    exact = cache.execute(typed, destination=tmp_path / "exact1.parquet")
    hit = cache.execute(typed, destination=tmp_path / "exact2.parquet")
    assert hit.hit and Decimal(exact.payload["rows"][0][0]) == Decimal("0.3")


def test_temporal_csv_parser_values_are_never_reused(workbook, tmp_path):
    workbook.save_text(
        "v\n2024-01-01 12:00:00\n", name="clock", kind="table", media_type="text/csv"
    )
    source = spec_for(workbook, "SELECT * FROM input1 ORDER BY ALL", name="clock")
    source = replace(
        source, inputs=[replace(source.inputs[0], csv_types={"v": "TIMESTAMP"})]
    )
    cache = QueryCache(workbook.root)
    first = cache.execute(source, destination=tmp_path / "clock1.parquet")
    second = cache.execute(source, destination=tmp_path / "clock2.parquet")
    assert not first.hit and not second.hit and first.key != second.key
    assert first.reason == second.reason == "temporal_csv_conversion"


def test_stale_inputs_and_source_changes_cannot_reuse_current_results(workbook):
    cache = QueryCache(workbook.root)
    old = spec_for(workbook, materialize=False)
    assert cache.execute(old).payload["rows"] == [[2]]
    workbook.save_text(
        "n\n1\n2\n3\n",
        name="data",
        kind="table",
        media_type="text/csv",
        expected_version=1,
    )
    with pytest.raises(ValueError, match="historical|stale"):
        cache.execute(old)
    current = cache.execute(spec_for(workbook, materialize=False))
    assert not current.hit and current.payload["rows"] == [[3]]
    historical = cache.execute(replace(old, allow_historical=True))
    assert historical.payload["rows"] == [[2]]


def test_mutated_or_symlinked_input_blob_is_rejected_before_hit(workbook, tmp_path):
    cache = QueryCache(workbook.root)
    spec = spec_for(workbook, materialize=False)
    cache.execute(spec)
    path = Path(spec.inputs[0].path)
    original = path.read_bytes()
    path.write_bytes(b"n\n9\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        cache.execute(spec)
    path.unlink()
    elsewhere = tmp_path / "copy.csv"
    elsewhere.write_bytes(original)
    path.symlink_to(elsewhere)
    with pytest.raises(ValueError, match="regular immutable"):
        cache.execute(spec)


def test_cache_file_corruption_recomputes_without_serving_wrong_bytes(
    workbook, tmp_path
):
    cache = QueryCache(workbook.root)
    spec = spec_for(workbook)
    first = cache.execute(spec, destination=tmp_path / "first.parquet")
    stored = next(cache.objects.glob("*.parquet"))
    stored.write_bytes(b"corrupt")
    recovered = cache.execute(spec, destination=tmp_path / "recovered.parquet")
    assert not recovered.hit and recovered.payload == first.payload
    assert recovered.materialized_sha256 == first.materialized_sha256


def test_force_recomputes_then_publishes_reusable_result(workbook):
    cache = QueryCache(workbook.root)
    spec = spec_for(workbook, materialize=False)
    calls = []

    def compute(_execution):
        calls.append(1)
        return Computed({"columns": ["n"], "rows": [[2]], "preview_truncated": False})

    assert not cache.execute(spec, compute).hit
    assert cache.execute(spec, compute).hit
    forced = cache.execute(spec, compute, force=True)
    assert not forced.hit and forced.reason == "forced" and len(calls) == 2
    assert cache.execute(spec, compute).hit


def test_row_file_and_deadline_limits_do_not_poison_later_queries(workbook, tmp_path):
    cache = QueryCache(workbook.root, entry_bytes=65536, max_bytes=262144)
    ordered = spec_for(workbook, "SELECT * FROM input1 ORDER BY ALL", max_rows=1)
    with pytest.raises(CacheLimitError, match="row limit"):
        cache.execute(ordered, destination=tmp_path / "too-many.parquet")
    rng = random.Random(42)
    rows = "value\n" + "\n".join(rng.randbytes(512).hex() for _ in range(1000)) + "\n"
    workbook.save_text(rows, name="wide", kind="table", media_type="text/csv")
    large = spec_for(workbook, "SELECT * FROM input1 ORDER BY ALL", name="wide")
    with pytest.raises(CacheLimitError):
        cache.execute(large, destination=tmp_path / "too-large.parquet")
    slow = spec_for(
        workbook,
        "SELECT sum(sin(a.i+b.i)) FROM range(1000000) a(i) CROSS JOIN range(1000000) b(i)",
        settings={"query_timeout": 0.1},
    )
    with pytest.raises(CacheTimeout):
        cache.execute(slow, destination=tmp_path / "too-slow.parquet", timeout=0.5)
    assert not list(cache.staging.iterdir())
    assert cache.execute(spec_for(workbook, materialize=False)).payload["rows"] == [[2]]


def test_eviction_and_crash_orphan_cleanup_stay_bounded(workbook):
    cache = QueryCache(
        workbook.root, entry_bytes=65536, max_bytes=262144, max_entries=2
    )
    for value in range(5):
        cache.execute(
            spec_for(workbook, "SELECT ? AS n", parameters=[value], materialize=False)
        )
    with sqlite3.connect(cache.database) as db:
        assert db.execute("SELECT count(*),sum(size) FROM entries").fetchone()[0] == 2
    orphan = cache.objects / ("f" * 32 + ".parquet")
    orphan.write_bytes(b"orphan from interrupted publication")
    staging = cache.staging / ("e" * 32)
    staging.mkdir()
    (staging / "partial").write_bytes(b"partial")
    QueryCache(workbook.root, entry_bytes=65536, max_bytes=262144, max_entries=2)
    assert not orphan.exists() and not staging.exists()


PROCESS_PROGRAM = """
import json,sys,time
from pathlib import Path
from agent_workbook.store import Workbook
from agent_workbook.query_cache import CacheInput,CacheSpec,Computed,QueryCache
root,counter,delay=sys.argv[1:]
w=Workbook(Path(root));item,path=w.table_inputs(['data'],False)[0]
cache=QueryCache(w.root,entry_bytes=65536,max_bytes=262144,lease_seconds=.3)
spec=CacheSpec('SELECT count(*) AS n FROM input1',[CacheInput.from_table(item,path)],materialize=False)
def compute(ctx):
 with Path(counter).open('a') as f:f.write('compute\\n')
 time.sleep(float(delay))
 return Computed({'columns':['n'],'rows':[[2]],'preview_truncated':False})
print(json.dumps(cache.execute(spec,compute,timeout=5).to_dict()))
"""


def test_cross_process_singleflight_and_reuse(workbook, tmp_path):
    counter = tmp_path / "computations"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                PROCESS_PROGRAM,
                str(workbook.root),
                str(counter),
                ".35",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    try:
        results = []
        for process in processes:
            output, errors = process.communicate(timeout=10)
            assert process.returncode == 0, errors
            results.append(json.loads(output))
        assert counter.read_text().splitlines() == ["compute"]
        assert sum(result["hit"] for result in results) == 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()


def test_dead_owner_lease_is_reclaimed_without_reusing_partial_output(
    workbook, tmp_path
):
    counter = tmp_path / "computations"
    process = subprocess.Popen(
        [sys.executable, "-c", PROCESS_PROGRAM, str(workbook.root), str(counter), "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not counter.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert counter.exists()
        process.kill()
        process.wait(timeout=2)
        cache = QueryCache(
            workbook.root, entry_bytes=65536, max_bytes=262144, lease_seconds=0.3
        )
        result = cache.execute(spec_for(workbook, materialize=False), timeout=3)
        assert not result.hit and result.payload["rows"] == [[2]]
        assert not list(cache.staging.iterdir())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(os.name != "posix", reason="POSIX bounded worker")
def test_parent_death_stops_materialization_worker(workbook):
    program = """
import sys
from pathlib import Path
from agent_workbook.store import Workbook
from agent_workbook.query_cache import CacheInput,CacheSpec,QueryCache
w=Workbook(Path(sys.argv[1]));item,path=w.table_inputs(['data'],False)[0]
s=CacheSpec('SELECT sum(sin(a.i+b.i)) FROM range(1000000) a(i) CROSS JOIN range(1000000) b(i)',[CacheInput.from_table(item,path)],settings={'query_timeout':20})
QueryCache(w.root).execute(s,destination=w.root/'never.parquet',timeout=25)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(workbook.root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            files = list((workbook.root / ".query-cache/staging").glob("*/worker.pid"))
            if files:
                child = int(files[0].read_text())
                break
            time.sleep(0.01)
        assert child is not None
        process.kill()
        process.wait(timeout=2)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("Bounded worker survived owner death")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
