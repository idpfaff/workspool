"""Real catalog/blob transport, strict archive boundaries, and quiescent restore."""

import hashlib
import json
import os
import sqlite3
import stat
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_workbook import (
    continuity,
    coordination,
    observability,
    scheduler,
    skills,
    tasks,
)
from agent_workbook import workbook_bundle as bundles
from agent_workbook.store import Workbook


def save(book, text="Exact café 🧪\r\n", name="notes", **kwargs):
    return book.save_text(
        text, name=name, kind="note", media_type="text/plain", **kwargs
    )


@pytest.fixture
def basic(tmp_path):
    book = Workbook(tmp_path / "source")
    first = save(book)
    second = save(book, "updated", expected_version=1)
    save(
        book,
        "retained provenance",
        "proof",
        dependencies=[second["artifact_id"]],
        provenance=[first["artifact_id"]],
    )
    path = tmp_path / "workbook.zip"
    bundles.export_bundle(book, path)
    return book, path


def unpack(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def repack(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)


def alter_catalog(path, tmp_path, sql):
    members = unpack(path)
    catalog = tmp_path / "altered.sqlite3"
    catalog.write_bytes(members["workbook.sqlite3"])
    with sqlite3.connect(catalog) as db:
        db.executescript(sql)
    members["workbook.sqlite3"] = catalog.read_bytes()
    manifest = json.loads(members["manifest.json"])
    manifest["catalog"] = {
        "sha256": hashlib.sha256(members["workbook.sqlite3"]).hexdigest(),
        "bytes": len(members["workbook.sqlite3"]),
    }
    members["manifest.json"] = json.dumps(manifest).encode()
    repack(path, members)


def test_roundtrip_preserves_exact_history_ids_dependencies_and_audits(basic, tmp_path):
    original, path = basic
    restored_path = tmp_path / "restored"
    result = bundles.import_bundle(path, restored_path)
    assert not result["live_execution_restored"] and result["artifacts"] == 3
    restored = Workbook(restored_path)
    for table in (
        "artifacts",
        "dependencies",
        "provenance_links",
        "name_versions",
        "coordination_events",
    ):
        with original.connect() as left, restored.connect() as right:
            old_rows = [
                tuple(row)
                for row in left.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            new_rows = [
                tuple(row)
                for row in right.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            assert old_rows == new_rows[: len(old_rows)]
            assert len(new_rows) == len(old_rows) + int(table == "coordination_events")
    with restored.connect() as db:
        floor = coordination.approval_floor(db)
        event = db.execute(
            "SELECT * FROM coordination_events WHERE event_id=?", (floor,)
        ).fetchone()
        assert event["kind"] == "workbook.restored"
        assert (
            json.loads(event["details"])["archive_sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
    with original.connect() as db:
        for row in db.execute("SELECT artifact_id,digest FROM artifacts"):
            assert (original.blobs / row["digest"]).read_bytes() == (
                restored.blobs / row["digest"]
            ).read_bytes()
    assert save(restored, "third", expected_version=2)["version"] == 3


def test_bundle_preserves_skills_but_disarms_claims_sessions_runs_schedules(tmp_path):
    book = Workbook(tmp_path / "source")
    book.save_text("n\n2\n3\n", name="data", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "total",
        "Sum all values",
        {"data": "data"},
        [
            {
                "name": "sum",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            }
        ],
    )
    run = tasks.run_task(book, "total")
    skill = skills.distill(book, "sum-values", "total")
    session = continuity.start_session(
        book, coordination.Principal("worker"), "Preserve evidence"
    )
    continuity.capture_event(session, "decision", {"decision": "Use complete data"})
    ticket = coordination.claim(book, coordination.Principal("worker"), "task:total")
    scheduler.configure_schedule(book, "daily", "total", interval_seconds=60)
    observability.record_tool(book, "query", elapsed_seconds=0.1)
    with book.connect() as db:
        db.execute(
            "INSERT INTO task_runs VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "interrupted",
                "other",
                1,
                "missing",
                "missing",
                0,
                None,
                "running",
                '{"state":"running","steps":[]}',
            ),
        )
        db.execute(
            "UPDATE task_schedules SET lease_owner='runner',lease_until=9999999999"
        )
    for name in (
        "workers.json",
        ".env",
        "context-index.sqlite3",
        "semantic-vectors.sqlite3",
    ):
        (book.root / name).write_text("MUST_NOT_EXPORT_SECRET_OR_LAUNCH_CONFIG")
    path = tmp_path / "bundle.zip"
    bundles.export_bundle(book, path)
    assert all(
        "MUST_NOT_EXPORT" not in body.decode(errors="ignore")
        for body in unpack(path).values()
    )
    result = bundles.import_bundle(path, tmp_path / "restored")
    restored = Workbook(Path(result["path"]))
    assert (
        skills.inspect_skill(restored, "sum-values")["artifact_id"]
        == skill["artifact_id"]
    )
    assert tasks.status(restored, "total")["run_id"] == run["run_id"]
    owner = coordination.owner(restored, "task:total")
    assert not owner["active"] and owner["fence"] == ticket["fence"]
    assert (
        coordination.claim(restored, coordination.Principal("new"), "task:total")[
            "fence"
        ]
        == ticket["fence"] + 1
    )
    with restored.connect() as db:
        assert db.execute("SELECT count(*) FROM observation_daily").fetchone()[0] == 1
        assert (
            db.execute("SELECT ended_at FROM continuity_sessions").fetchone()[0]
            is not None
        )
        row = db.execute(
            "SELECT state,payload FROM task_runs WHERE run_id='interrupted'"
        ).fetchone()
        assert row[0] == "partial" and json.loads(row[1])["state"] == "partial"
        schedule = db.execute(
            "SELECT enabled,lease_owner,lease_until FROM task_schedules"
        ).fetchone()
        assert tuple(schedule) == (0, None, 0)
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    assert {p.name for p in restored.root.iterdir()} <= {
        "workbook.sqlite3",
        "workbook.sqlite3-wal",
        "workbook.sqlite3-shm",
        "blobs",
    }


def test_snapshot_serializes_against_save_delete_and_gc(tmp_path, monkeypatch):
    book = Workbook(tmp_path / "source")
    saved = save(book)
    entered, release = threading.Event(), threading.Event()
    original = bundles._copy_verified

    def paused(source, target, size, digest, deadline):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(source, target, size, digest, deadline)

    monkeypatch.setattr(bundles, "_copy_verified", paused)
    path = tmp_path / "bundle.zip"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(bundles.export_bundle, book, path)
        assert entered.wait(5)
        try:
            with (
                sqlite3.connect(book.catalog, timeout=0.01) as db,
                pytest.raises(sqlite3.OperationalError, match="locked"),
            ):
                db.execute("BEGIN IMMEDIATE")
        finally:
            release.set()
        future.result()
    book.delete("notes", expected_version=1)
    bundles.import_bundle(path, tmp_path / "restored")
    assert (
        Workbook(tmp_path / "restored").inspect(
            saved["artifact_id"], excerpt_bytes=100
        )["excerpt"]
        == "Exact café 🧪\r\n"
    )


@pytest.mark.parametrize("existing", ["directory", "file", "symlink"])
def test_existing_destination_is_never_overwritten(basic, tmp_path, existing):
    _, path = basic
    destination = tmp_path / "existing"
    if existing == "directory":
        destination.mkdir()
    elif existing == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="must not already exist"):
        bundles.import_bundle(path, destination)
    assert os.path.lexists(destination)


@pytest.mark.parametrize(
    "extra",
    ["../escape", "/absolute", "blobs/../escape", "workers.json", "blobs/not-a-digest"],
)
def test_unexpected_and_traversal_paths_are_rejected(basic, tmp_path, extra):
    _, path = basic
    members = unpack(path)
    members[extra] = b"bad"
    repack(path, members)
    with pytest.raises(ValueError, match="paths"):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_duplicate_members_links_and_compression_are_rejected(basic, tmp_path):
    _, original = basic
    members = unpack(original)
    for kind in ("duplicate", "symlink", "compressed"):
        path = tmp_path / (kind + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            for name, body in members.items():
                info = zipfile.ZipInfo(name)
                if kind == "symlink" and name == "workbook.sqlite3":
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                if kind == "compressed":
                    info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, body)
            if kind == "duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr("manifest.json", members["manifest.json"])
        with pytest.raises(ValueError):
            bundles.import_bundle(path, tmp_path / kind)
        assert not (tmp_path / kind).exists()


@pytest.mark.parametrize(
    "corrupt", ["blob", "missing", "catalog-hash", "catalog-format", "manifest-json"]
)
def test_corrupt_or_incomplete_bundles_leave_no_destination(basic, tmp_path, corrupt):
    _, path = basic
    members = unpack(path)
    name = next(name for name in members if name.startswith("blobs/"))
    if corrupt == "blob":
        members[name] = b"x" * len(members[name])
    elif corrupt == "missing":
        del members[name]
    elif corrupt == "catalog-hash":
        members["workbook.sqlite3"] = b"x" * len(members["workbook.sqlite3"])
    elif corrupt == "catalog-format":
        members["workbook.sqlite3"] = b"bad sqlite"
        manifest = json.loads(members["manifest.json"])
        manifest["catalog"] = {
            "bytes": 10,
            "sha256": hashlib.sha256(b"bad sqlite").hexdigest(),
        }
        members["manifest.json"] = json.dumps(manifest).encode()
    else:
        members["manifest.json"] = b'{"format":1,"format":2}'
    repack(path, members)
    with pytest.raises(ValueError):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TRIGGER malicious AFTER INSERT ON artifacts BEGIN DELETE FROM artifacts; END;",
        "UPDATE artifacts SET artifact_id='bad-id' WHERE rowid=1;",
        "UPDATE artifacts SET digest='../../outside' WHERE rowid=1;",
        "UPDATE artifacts SET version=0 WHERE rowid=1;",
        "UPDATE dependencies SET dependency_id='wb_00000000000000000000000000000000';",
    ],
)
def test_malformed_catalog_is_rejected_before_install(basic, tmp_path, sql):
    _, path = basic
    alter_catalog(path, tmp_path, sql)
    with pytest.raises(ValueError):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_limits_and_missing_corrupt_or_linked_source_blobs_fail(tmp_path):
    book = Workbook(tmp_path / "source")
    item = save(book)
    path = tmp_path / "bundle.zip"
    with pytest.raises(ValueError, match="limit"):
        bundles.export_bundle(book, path, max_bytes=1)
    assert not path.exists()
    blob = book.blobs / book.inspect(item["artifact_id"])["sha256"]
    blob.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="size or SHA256|exceeds"):
        bundles.export_bundle(book, path)
    blob.unlink()
    outside = tmp_path / "outside"
    outside.write_text("content")
    blob.symlink_to(outside)
    with pytest.raises((ValueError, OSError)):
        bundles.export_bundle(book, path)
    assert not path.exists()


def test_capabilities_and_output_overwrite_guard(basic, tmp_path):
    book, path = basic
    reader = coordination.Principal("reader", "reader", ["read"])
    with pytest.raises(PermissionError, match="export"):
        bundles.export_bundle(book, tmp_path / "denied.zip", principal=reader)
    with pytest.raises(PermissionError, match="import"):
        bundles.import_bundle(path, tmp_path / "denied", principal=reader)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already exist"):
        bundles.export_bundle(book, path)
    assert path.read_bytes() == before


def test_deleted_content_is_not_copied_from_sqlite_free_pages(tmp_path):
    book = Workbook(tmp_path / "source")
    marker = "DELETED_PRIVATE_HISTORY_" * 1000
    save(book, marker)
    book.delete("notes", expected_version=1)
    path = tmp_path / "bundle.zip"
    bundles.export_bundle(book, path)
    assert b"DELETED_PRIVATE_HISTORY_" not in unpack(path)["workbook.sqlite3"]


@pytest.mark.parametrize(
    "payload", ["broken", "[]", "null", '{"steps":[1]}', '{"sources":[]}']
)
def test_finalized_task_payload_json_is_rejected_before_install(
    basic, tmp_path, payload
):
    _, path = basic
    encoded = payload.replace("'", "''")
    alter_catalog(
        path,
        tmp_path,
        tasks.SCHEMA
        + "INSERT INTO task_runs VALUES('run-test','done',1,'definition','anchor',0,1,'complete',"
        + f"'{encoded}');",
    )
    with pytest.raises(ValueError, match="JSON|payload"):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "details", ["broken", "[]", "null", '{"n":NaN}', '{"n":1e999}', '{"x":1,"x":2}']
)
def test_event_details_json_is_rejected_before_install(basic, tmp_path, details):
    _, path = basic
    encoded = details.replace("'", "''")
    alter_catalog(
        path,
        tmp_path,
        "INSERT INTO coordination_events(occurred_at,actor,role,kind,resource,details) "
        + f"VALUES(0,'local','writer','test','workbook','{encoded}');",
    )
    with pytest.raises(ValueError, match="JSON"):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE artifacts SET checkpoint='[]';",
        "UPDATE artifacts SET recipe='broken';",
        continuity.SCHEMA + "UPDATE continuity_retention SET retired_usage='null';",
        observability.SCHEMA
        + "INSERT INTO observation_daily VALUES('2026-11-08','query','unconfigured','[]');",
        continuity.SCHEMA
        + "INSERT INTO continuity_sessions VALUES('test','local','writer','goal',0,NULL);"
        + "INSERT INTO continuity_usage VALUES('test','broken');",
        continuity.SCHEMA
        + "INSERT INTO continuity_sessions VALUES('test','local','writer','goal',0,NULL);"
        + "INSERT INTO continuity_episodes(session_id,event_id,payload_hash,artifact_id,kind,verified,summary,payload,captured_at) "
        + "SELECT 'test','test','hash',artifact_id,'test',0,'test','[]',0 FROM artifacts LIMIT 1;",
    ],
)
def test_other_catalog_json_columns_require_objects(basic, tmp_path, sql):
    _, path = basic
    alter_catalog(path, tmp_path, sql)
    with pytest.raises(ValueError, match="JSON"):
        bundles.import_bundle(path, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_catalog_json_validation_obeys_size_and_deadline(basic, tmp_path, monkeypatch):
    _, path = basic
    monkeypatch.setattr(bundles, "MAX_CATALOG_JSON", 1)
    with pytest.raises(ValueError, match="oversized JSON"):
        bundles.import_bundle(path, tmp_path / "restored")
    monkeypatch.setattr(bundles, "MAX_CATALOG_JSON", 16 * 1024**2)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE observation_daily(counters TEXT)")
        db.execute("INSERT INTO observation_daily VALUES('{}')")
        with pytest.raises(ValueError, match="time limit"):
            bundles._validate_catalog_json(db, {"observation_daily"}, 0)
