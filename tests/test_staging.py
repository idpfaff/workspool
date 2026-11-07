"""Killed import writers leave collectible stages; live writers remain safe."""

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_workbook import store
from agent_workbook.store import Workbook

WRITER = r"""
import sys, time
from pathlib import Path
from agent_workbook import store
from agent_workbook.store import Workbook

root, ready, release, before_lock, allow_lock = map(Path, sys.argv[1:])
workbook = Workbook(root)
if str(before_lock) != "none":
    original = store.fcntl.flock
    def delayed_lock(*args):
        before_lock.write_text("stage created before its file lock")
        while not allow_lock.exists():
            time.sleep(0.01)
        return original(*args)
    store.fcntl.flock = delayed_lock

class Source:
    first = True
    def read(self, size):
        if self.first:
            self.first = False
            return b"unfinished exact source\n" * 10000
        ready.write_text("source read paused with a live locked stage")
        while not release.exists():
            time.sleep(0.01)
        return b""

workbook._save(Source(), name="incoming", kind="document", media_type="text/plain")
"""


def wait_for(path, process, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError(f"Writer exited early: {process.communicate()}")
        if time.monotonic() > deadline:
            raise AssertionError("Writer did not reach its test checkpoint")
        time.sleep(0.01)


def writer(tmp_path, workbook, *, delay_lock=False):
    ready, release = tmp_path / "ready", tmp_path / "release"
    before, allow = (
        (tmp_path / "before-lock", tmp_path / "allow-lock")
        if delay_lock
        else (Path("none"), Path("none"))
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            WRITER,
            str(workbook.root),
            str(ready),
            str(release),
            str(before),
            str(allow),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process, ready, release, before, allow


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


@pytest.mark.skipif(
    store.fcntl is None, reason="POSIX flock is required for orphan cleanup"
)
def test_killed_writer_stage_is_reclaimed_without_touching_retained_blobs(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    retained = workbook.save_text(
        "keep", name="retained", kind="note", media_type="text/plain"
    )
    process, ready, _, _, _ = writer(tmp_path, workbook)
    try:
        wait_for(ready, process)
        staged = list(workbook._staging_candidates())
        assert len(staged) == 1 and staged[0].stat().st_size > 0
        size = staged[0].stat().st_size
        stop(process)
        preview = workbook.prune(dry_run=True)
        assert preview["stages_removed"] == 1 and preview["bytes_freed"] == size
        assert staged[0].exists()
        result = workbook.prune()
        assert result["stages_removed"] == 1 and result["blobs_removed"] == 0
        assert result["bytes_freed"] == size and not staged[0].exists()
        assert workbook.inspect(retained["artifact_id"], 100)["excerpt"] == "keep"
        with pytest.raises(ValueError, match="Unknown"):
            workbook.inspect("incoming")
    finally:
        stop(process)


@pytest.mark.skipif(
    store.fcntl is None, reason="POSIX flock is required for orphan cleanup"
)
def test_live_writer_survives_prune_and_promotes_exact_source(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    process, ready, release, _, _ = writer(tmp_path, workbook)
    try:
        wait_for(ready, process)
        staged = list(workbook._staging_candidates())
        assert workbook.prune(dry_run=True)["stages_removed"] == 0
        assert workbook.prune()["stages_removed"] == 0
        assert staged[0].exists()
        release.touch()
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 0, (output, errors)
        artifact = workbook.inspect("incoming")
        assert (
            workbook.blobs / artifact["sha256"]
        ).read_bytes() == b"unfinished exact source\n" * 10000
        assert list(workbook._staging_candidates()) == []
    finally:
        stop(process)


@pytest.mark.skipif(
    store.fcntl is None, reason="POSIX flock is required for orphan cleanup"
)
def test_creation_before_file_lock_is_serialized_against_cleanup(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    process, ready, release, before, allow = writer(tmp_path, workbook, delay_lock=True)
    collector = None
    try:
        wait_for(before, process)
        staged = list(workbook._staging_candidates())
        assert len(staged) == 1
        collector = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import json,sys; from pathlib import Path; from agent_workbook.store import Workbook; print(json.dumps(Workbook(Path(sys.argv[1])).prune()))",
                str(workbook.root),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Cleanup cannot acquire the catalog lock in the pre-flock interval.
        time.sleep(0.1)
        assert collector.poll() is None and staged[0].exists()
        allow.touch()
        wait_for(ready, process)
        output, errors = collector.communicate(timeout=10)
        assert collector.returncode == 0, errors
        assert json.loads(output)["stages_removed"] == 0 and staged[0].exists()
        release.touch()
        process.communicate(timeout=10)
        assert process.returncode == 0
        assert workbook.inspect("incoming")["size_bytes"] > 0
    finally:
        stop(process)
        if collector is not None:
            stop(collector)


@pytest.mark.skipif(
    store.fcntl is None, reason="POSIX flock is required for orphan cleanup"
)
def test_killed_before_file_lock_leaves_safe_collectible_empty_stage(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    process, _, _, before, _ = writer(tmp_path, workbook, delay_lock=True)
    try:
        wait_for(before, process)
        stop(process)
        result = workbook.prune()
        assert result["stages_removed"] == 1 and result["bytes_freed"] == 0
        assert list(workbook._staging_candidates()) == []
    finally:
        stop(process)


def test_legacy_unknown_symlink_and_directory_names_are_never_removed(tmp_path):
    workbook = Workbook(tmp_path / "workbook")
    legacy = workbook.blobs / "tmp_legacy"
    legacy.write_bytes(b"unknown live or abandoned old-client data")
    directory = workbook.blobs / (".workspool-stage-" + "a" * 32 + "-directory.tmp")
    directory.mkdir()
    target = tmp_path / "valuable"
    target.write_bytes(b"never delete")
    link = workbook.blobs / (".workspool-stage-" + "a" * 32 + "-symlink.tmp")
    link.symlink_to(target)
    result = workbook.prune()
    assert result["stages_removed"] == 0
    assert legacy.exists() and directory.exists() and link.is_symlink()
    assert target.read_bytes() == b"never delete"


def test_lock_acquisition_failure_and_unsupported_platform_cleanup(
    tmp_path, monkeypatch
):
    workbook = Workbook(tmp_path / "workbook")
    if store.fcntl is not None:
        with monkeypatch.context() as patch:
            patch.setattr(
                store.fcntl,
                "flock",
                lambda *_: (_ for _ in ()).throw(OSError("lock failed")),
            )
            with pytest.raises(OSError, match="lock failed"):
                workbook.save_text(
                    "data", name="failed", kind="note", media_type="text/plain"
                )
        assert not list(workbook.blobs.iterdir())
    monkeypatch.setattr(store, "fcntl", None)
    with workbook._staging() as stream:
        assert not store.STAGING_NAME.fullmatch(Path(stream.name).name)
        stream.write(b"live fallback")
        stream.flush()
        assert workbook.prune()["stages_removed"] == 0
        assert Path(stream.name).exists()
    assert not list(workbook.blobs.iterdir())
    workbook._save(
        io.BytesIO(b"saved"), name="fallback", kind="note", media_type="text/plain"
    )
    assert workbook.inspect("fallback", 100)["excerpt"] == "saved"


@pytest.mark.skipif(
    store.fcntl is None, reason="POSIX flock is required for orphan cleanup"
)
def test_stage_cleanup_failure_is_reported_and_retryable(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    stage = workbook.blobs / (".workspool-stage-" + "a" * 32 + "-abandoned.tmp")
    stage.write_bytes(b"dead import")
    real_unlink = os.unlink

    def fail(path, *args, **kwargs):
        if Path(path) == stage:
            raise PermissionError("test staging cleanup failure")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(store.os, "unlink", fail)
        result = workbook.prune()
    assert result["stages_removed"] == 0 and result["cleanup_pending"]
    assert result["gc_errors"][0]["blob"] == stage.name and stage.exists()
    assert workbook.prune()["stages_removed"] == 1
    assert not stage.exists()
