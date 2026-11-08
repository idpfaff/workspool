"""Only hash-pinned local adapters execute; test jobs use synthetic fixtures."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_workbook import tasks
from agent_workbook.coordination import Principal, append_event
from agent_workbook.store import Workbook
from agent_workbook.task_workers import load_registry

WORKER = """import csv,json,os,stat,sys
from pathlib import Path
request=json.load(sys.stdin)
path=Path(request["inputs"][0]["path"])
total=sum(int(row["n"]) for row in csv.DictReader(path.open()))
assert "/blobs/" not in str(path)
assert not (path.stat().st_mode & 0o222)
print(json.dumps({"format":1,"columns":["total","idempotency_key"],"types":["BIGINT","VARCHAR"],"rows":[[total+request["parameters"][0],request["idempotency_key"]]]}))
"""


def registry(tmp_path, script=WORKER, **settings):
    job = tmp_path / "job.py"
    job.write_text(script)
    python = Path(sys.executable).absolute()
    manifest = tmp_path / "workers.json"
    manifest.write_text(
        json.dumps(
            {
                "format": 1,
                "workers": {
                    "sum_rows": {
                        "command": [str(python), str(job)],
                        "files": {
                            str(python.resolve()): hashlib.sha256(
                                python.resolve().read_bytes()
                            ).hexdigest(),
                            str(job): hashlib.sha256(job.read_bytes()).hexdigest(),
                        },
                        **settings,
                    }
                },
            }
        )
    )
    return load_registry(manifest), job


def setup(tmp_path, *, approval=False):
    book = Workbook(tmp_path / "book")
    source = book.save_text(
        "n\n2\n3\n", name="data", kind="table", media_type="text/csv"
    )
    tasks.define_task(
        book,
        "job",
        "Run configured row adapter",
        {"data": "data"},
        [
            {
                "name": "computed",
                "kind": "worker",
                "worker": "sum_rows",
                "inputs": ["data"],
                "parameters": [4],
                **({"approval": True} if approval else {}),
            },
            {
                "name": "summary",
                "inputs": ["computed"],
                "sql": "SELECT sum(total)::BIGINT AS total FROM input1",
            },
        ],
    )
    return book, source


def approve(book, ref):
    artifact = book.inspect(ref)
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        append_event(
            db,
            Principal("reviewer", "approver"),
            "approval.accepted",
            ref,
            {"sha256": artifact["sha256"]},
        )


def test_registered_python_worker_uses_scratch_inputs_and_stable_idempotency_key(
    tmp_path,
):
    workers, _ = registry(tmp_path)
    book, source = setup(tmp_path)
    raw = book.inspect(source["artifact_id"], excerpt_bytes=1024)["excerpt"]
    first = tasks.run_task(book, "job", workers=workers)
    assert first["state"] == "complete", first
    assert first["steps"][-1]["result"]["rows"] == [[9]]
    worker = first["steps"][0]
    assert (
        worker["cache"]["hit"] is False
        and worker["cache"]["reason"] == "configured_worker"
    )
    assert worker["result"]["worker"]["digest"] == workers.resolve("sum_rows").digest
    assert worker["elapsed_seconds"] > 0
    second = tasks.run_task(book, "job", workers=workers)
    assert second["steps"][0]["cache"]["hit"] is False
    assert second["steps"][0]["result"]["rows"] == worker["result"]["rows"]
    assert book.inspect(source["artifact_id"], excerpt_bytes=1024)["excerpt"] == raw
    assert not list(book.root.glob("worker-*"))


def test_completed_worker_is_reused_only_for_explicit_resume(tmp_path, monkeypatch):
    workers, _ = registry(tmp_path)
    book, _ = setup(tmp_path)
    first = tasks.run_task(book, "job", workers=workers)
    assert first["state"] == "complete", first
    from agent_workbook import task_workers

    def no_repeat(*args, **kwargs):
        raise AssertionError("Completed worker must not execute during resume")

    monkeypatch.setattr(task_workers, "_run", no_repeat)
    resumed = tasks.run_task(book, "job", resume_from=first["run_id"], workers=workers)
    assert resumed["state"] == "complete", resumed
    assert resumed["steps"][0]["cache"]["reason"] == "resumed_materialization"
    assert resumed["steps"][0]["artifact_id"] == first["steps"][0]["artifact_id"]


def test_unknown_worker_and_missing_registry_are_recorded_without_orphan_running_run(
    tmp_path,
):
    book, _ = setup(tmp_path)
    result = tasks.run_task(book, "job")
    assert result["state"] == "partial"
    assert result["omitted"][0]["reason"] == "worker_configuration"
    with book.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM task_runs WHERE state='running'"
        ).fetchone()


def test_worker_capability_is_required_before_launch(tmp_path):
    workers, _ = registry(tmp_path)
    book, _ = setup(tmp_path)
    principal = Principal("limited", capabilities=frozenset({"task:run"}))
    with pytest.raises(PermissionError, match="workers"):
        tasks.run_task(book, "job", workers=workers, principal=principal)
    with book.connect() as db:
        assert not db.execute(
            "SELECT 1 FROM artifacts WHERE name LIKE '_task_worker_results/%'"
        ).fetchone()


def test_script_and_manifest_tampering_fail_closed(tmp_path):
    workers, job = registry(tmp_path)
    book, _ = setup(tmp_path)
    job.write_text(WORKER + "\n# changed\n")
    assert tasks.run_task(book, "job", workers=workers)["state"] == "partial"
    workers, _ = registry(tmp_path)
    workers.path.write_text(workers.path.read_text() + " ")
    assert tasks.run_task(book, "job", workers=workers)["state"] == "partial"


def test_changed_worker_identity_invalidates_approval_and_historical_resume(tmp_path):
    workers, _ = registry(tmp_path)
    book, _ = setup(tmp_path, approval=True)
    first = tasks.run_task(book, "job", workers=workers)
    assert first["state"] == "awaiting_approval"
    old_gate = first["steps"][0]["approval_ref"]
    approve(book, old_gate)
    changed, _ = registry(tmp_path, WORKER + "\n# new configured revision\n")
    resumed = tasks.run_task(book, "job", resume_from=first["run_id"], workers=changed)
    assert (
        resumed["state"] == "partial"
        and resumed["omitted"][0]["reason"] == "worker_configuration"
    )
    fresh = tasks.run_task(book, "job", workers=changed)
    assert fresh["state"] == "awaiting_approval"
    assert fresh["steps"][0]["approval_ref"] != old_gate
    approve(book, fresh["steps"][0]["approval_ref"])
    complete = tasks.run_task(book, "job", resume_from=fresh["run_id"], workers=changed)
    assert complete["state"] == "complete", complete


@pytest.mark.parametrize(
    "script,settings",
    [
        ("import time\ntime.sleep(3)\n", {"timeout_seconds": 0.2}),
        ("print('x'*100000)\n", {"max_output_bytes": 1024}),
        ("print('not json')\n", {}),
        (
            "import json\nprint(json.dumps({'format':1,'columns':['n'],'types':['BIGINT'],'rows':[[1],[2]]}))\n",
            {"max_rows": 1},
        ),
        (
            "import json\nprint(json.dumps({'format':1,'columns':['n'],'types':['BIGINT'],'rows':[[2**100]]}))\n",
            {},
        ),
        ("import json\nprint(json.dumps({'format':1,'parquet':'/etc/passwd'}))\n", {}),
    ],
)
def test_worker_deadline_output_protocol_and_numeric_bounds_fail_safely(
    tmp_path, script, settings
):
    workers, _ = registry(tmp_path, script, **settings)
    book, _ = setup(tmp_path)
    result = tasks.run_task(book, "job", workers=workers)
    assert result["state"] == "partial"
    assert result["steps"][0]["state"] == "failed"
    assert result["steps"][1]["state"] == "blocked"
    assert not list(book.root.glob("worker-*"))


def test_worker_decimal_output_is_lossless(tmp_path):
    script = "import json\nprint(json.dumps({'format':1,'columns':['n'],'types':['DECIMAL(38,12)'],'rows':[['9007199254740995.000000000003']]}))\n"
    workers, _ = registry(tmp_path, script)
    book = Workbook(tmp_path / "book")
    book.save_text("n\n1\n", name="data", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "decimal",
        "Exact decimal",
        {"data": "data"},
        [{"name": "value", "kind": "worker", "worker": "sum_rows", "inputs": ["data"]}],
    )
    result = tasks.run_task(book, "decimal", workers=workers)
    assert result["state"] == "complete", result
    assert result["steps"][0]["result"]["rows"] == [["9007199254740995.000000000003"]]


def test_worker_parquet_protocol_materializes_exact_output(tmp_path):
    script = """import json,sys
from pathlib import Path
import duckdb
request=json.load(sys.stdin)
output=Path(request["output_directory"])/"answer.parquet"
duckdb.sql("SELECT 9007199254740995::BIGINT AS total").write_parquet(str(output))
print(json.dumps({"format":1,"parquet":"answer.parquet"}))
"""
    workers, _ = registry(tmp_path, script)
    book, _ = setup(tmp_path)
    result = tasks.run_task(book, "job", workers=workers)
    assert result["state"] == "complete", result
    assert result["steps"][-1]["result"]["rows"] == [["9007199254740995"]]
    assert result["steps"][-1]["result"]["integer_string_columns"] == [
        {"index": 0, "sql_type": "BIGINT"}
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-size limits")
def test_materialization_is_capped_during_writes(tmp_path):
    from agent_workbook.task_workers import _materialize

    workers, _ = registry(tmp_path, max_output_bytes=2048)
    spec = workers.resolve("sum_rows")
    book, _ = setup(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    reply = {
        "format": 1,
        "columns": [f"c{index}" for index in range(64)],
        "types": ["BIGINT"] * 64,
        "rows": [[1] * 64],
    }
    assert len(json.dumps(reply).encode()) < 2048
    with pytest.raises(ValueError, match="bounded materialization"):
        _materialize(book, spec, reply, scratch)
    partial_files = list(scratch.glob("*.parquet"))
    assert partial_files and all(path.stat().st_size <= 2048 for path in partial_files)
    assert not list(book.root.glob("worker-*"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group watchdog")
def test_worker_is_stopped_when_its_task_server_dies(tmp_path):
    pid_path = tmp_path / "worker.pid"
    script = (
        "import os,time\nfrom pathlib import Path\n"
        + f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        + "time.sleep(30)\n"
    )
    workers, _ = registry(tmp_path, script, timeout_seconds=30)
    book, _ = setup(tmp_path)
    code = (
        "from agent_workbook.store import Workbook; "
        "from agent_workbook.tasks import run_task; "
        "from agent_workbook.task_workers import load_registry; "
        "import sys; from pathlib import Path; run_task(Workbook(Path(sys.argv[1])), 'job', "
        "workers=load_registry(sys.argv[2]))"
    )
    server = subprocess.Popen(
        [sys.executable, "-c", code, str(book.root), str(workers.path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker_pid = None
    try:
        deadline = time.monotonic() + 10
        while not pid_path.exists() and time.monotonic() < deadline:
            assert server.poll() is None
            time.sleep(0.02)
        assert pid_path.exists(), "Synthetic worker never started"
        worker_pid = int(pid_path.read_text())
        server.kill()
        server.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(worker_pid)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        else:
            pytest.fail("Worker remained running after its task server died")
    finally:
        if server.poll() is None:
            server.kill()
        server.wait(timeout=5)
        if worker_pid is not None:
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
