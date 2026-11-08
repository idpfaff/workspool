"""Verified local workbook transport, not remote trust or filesystem isolation.

Only the catalog and referenced immutable blobs are exported. Caches, settings,
environment files, credentials, and executable worker configuration are never
collected from the workbook directory. Artifact bodies remain exact: a user who
stored a secret inside an artifact must treat the bundle as equally sensitive.
Restore accepts the installed version's known catalog schema, never arbitrary
SQLite triggers/views, and never merges into or overwrites an existing workbook.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import struct
import tempfile
import time
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import quote

from .coordination import append_event, authorize_capability, current_principal

FORMAT = "workspool-bundle-v1"
MAX_BYTES = 10 * 1024**3
MAX_MEMBERS = 100000
MAX_MANIFEST = 16 * 1024**2
MAX_ARTIFACTS = 100000
MAX_CATALOG_JSON = 16 * 1024**2
SECONDS = 120
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ARTIFACT = re.compile(r"wb_[0-9a-f]{32}\Z")


def _limits(max_bytes, max_members):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 1024**4:
        raise ValueError("max_bytes must be between 1 and 1 TiB")
    if type(max_members) is not int or not 2 <= max_members <= MAX_MEMBERS:
        raise ValueError("max_members must be between 2 and 100000")


def _deadline(deadline):
    if time.monotonic() > deadline:
        raise ValueError("Workbook bundle operation exceeded its time limit")


@contextmanager
def _regular(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(
                "Bundle inputs must be regular files, not links or devices"
            )
        yield source


def _copy_verified(source, target, size, digest, deadline):
    copied, checksum = 0, hashlib.sha256()
    while chunk := source.read(min(1024**2, size - copied + 1)):
        _deadline(deadline)
        copied += len(chunk)
        if copied > size:
            raise ValueError("Bundle member exceeds its declared size")
        checksum.update(chunk)
        target.write(chunk)
    if copied != size or checksum.hexdigest() != digest:
        raise ValueError("Bundle member size or SHA256 mismatch")


def _file_record(path):
    checksum = hashlib.sha256()
    with _regular(path) as source:
        for chunk in iter(lambda: source.read(1024**2), b""):
            checksum.update(chunk)
        size = source.tell()
    return {"bytes": size, "sha256": checksum.hexdigest()}


def _schema(db):
    return {
        row[0]: (row[1], row[2], row[3].strip() if row[3] else None)
        for row in db.execute("SELECT name,type,tbl_name,sql FROM sqlite_master")
    }


def _known_schema():
    # Construct schema in isolated memory, never execute imported schema SQL.
    from . import (
        continuity,
        coordination,
        observability,
        scheduler,
        skills,
        store,
        tasks,
    )

    with closing(sqlite3.connect(":memory:")) as db:
        db.create_function("workspool_retention", 0, lambda: 0)
        for module in (
            store,
            coordination,
            continuity,
            tasks,
            skills,
            scheduler,
            observability,
        ):
            db.executescript(module.SCHEMA)
        return _schema(db)


def _validate_catalog_json(db, tables, deadline):
    # Validate transport structure, not the truth of imported application data.
    # Streaming rows avoids materializing an unbounded event/run history; each
    # parse is capped, and the surrounding catalog has a SQLite progress timer.
    columns = (
        ("artifacts", "checkpoint", True),
        ("artifacts", "recipe", True),
        ("coordination_events", "details", False),
        ("task_runs", "payload", False),
        ("continuity_episodes", "payload", False),
        ("continuity_usage", "counters", False),
        ("continuity_retention", "retired_usage", False),
        ("observation_daily", "counters", False),
    )

    def reject_constant(value):
        raise ValueError("Non-finite JSON constant")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result

    for table, column, nullable in columns:
        if table not in tables:
            continue
        for (encoded,) in db.execute(f'SELECT "{column}" FROM "{table}"'):
            _deadline(deadline)
            if encoded is None and nullable:
                continue
            if not isinstance(encoded, str) or len(encoded.encode()) > MAX_CATALOG_JSON:
                raise ValueError(
                    f"Bundle catalog has invalid or oversized JSON in {table}.{column}"
                )
            try:
                value = json.loads(
                    encoded,
                    object_pairs_hook=_unique_object,
                    parse_constant=reject_constant,
                    parse_float=finite_float,
                )
            except (ValueError, RecursionError) as error:
                raise ValueError(
                    f"Bundle catalog has malformed JSON in {table}.{column}"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(  # noqa: TRY004 -- malformed serialized content
                    f"Bundle catalog requires a JSON object in {table}.{column}"
                )
            if table == "task_runs":
                for key in ("steps", "citations", "omitted"):
                    if key in value and (
                        not isinstance(value[key], list)
                        or any(not isinstance(item, dict) for item in value[key])
                    ):
                        raise ValueError(f"Bundle task payload has malformed {key}")
                for key in ("definition", "sources", "workers"):
                    if key in value and not isinstance(value[key], dict):
                        raise ValueError(f"Bundle task payload has malformed {key}")
            _deadline(deadline)


def _validate_catalog(path, deadline):
    known = _known_schema()
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64 * 1024**2)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024**2)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        actual = _schema(db)
        if (
            not {
                "artifacts",
                "dependencies",
                "provenance_links",
                "name_versions",
                "latest",
            }
            <= actual.keys()
        ):
            raise ValueError("Bundle catalog is missing required workbook tables")
        if any(known.get(name) != definition for name, definition in actual.items()):
            raise ValueError("Bundle catalog contains an unsupported or altered schema")
        if [tuple(row) for row in db.execute("PRAGMA quick_check")] != [("ok",)]:
            raise ValueError("Bundle catalog failed SQLite quick_check")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Bundle catalog contains broken foreign keys")
        _validate_catalog_json(db, actual, deadline)
        rows = db.execute(
            "SELECT artifact_id,name,version,digest,size_bytes FROM artifacts LIMIT ?",
            (MAX_ARTIFACTS + 1,),
        ).fetchall()
        if len(rows) > MAX_ARTIFACTS:
            raise ValueError("Bundle catalog exceeds the artifact count limit")
        blobs = {}
        for row in rows:
            if (
                not isinstance(row["artifact_id"], str)
                or not ARTIFACT.fullmatch(row["artifact_id"])
                or not isinstance(row["name"], str)
                or not 1 <= len(row["name"]) <= 120
                or type(row["version"]) is not int
                or not 1 <= row["version"] <= 2**63 - 1
                or not isinstance(row["digest"], str)
                or not DIGEST.fullmatch(row["digest"])
                or type(row["size_bytes"]) is not int
                or row["size_bytes"] < 0
            ):
                raise ValueError("Bundle catalog contains malformed artifact metadata")
            previous = blobs.setdefault(row["digest"], row["size_bytes"])
            if previous != row["size_bytes"]:
                raise ValueError("Bundle catalog disagrees about a blob's size")
        if db.execute(
            "SELECT 1 FROM artifacts a LEFT JOIN name_versions n ON n.name=a.name "
            "WHERE n.version IS NULL OR n.version<a.version LIMIT 1"
        ).fetchone():
            raise ValueError("Bundle catalog has invalid version counters")
        for name, version in db.execute("SELECT name,version FROM name_versions"):
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 120
                or type(version) is not int
                or version < 1
            ):
                raise ValueError("Bundle catalog has malformed version tombstones")
        if db.execute(
            "SELECT 1 FROM artifacts a JOIN artifacts p ON p.artifact_id=a.previous_id "
            "WHERE a.name!=p.name OR p.version>=a.version LIMIT 1"
        ).fetchone():
            raise ValueError("Bundle catalog has invalid version lineage")
    return blobs, len(rows)


def _snapshot(workbook, directory, max_bytes, max_members, deadline):
    catalog = directory / "workbook.sqlite3"
    (directory / "blobs").mkdir(mode=0o700)
    # A WAL reader alone does not protect referenced files from post-commit GC.
    # Hold the same catalog writer lock as save/delete/GC until blobs are copied.
    with workbook.connect() as guard:
        guard.execute("BEGIN IMMEDIATE")
        pages = guard.execute("PRAGMA page_count").fetchone()[0]
        page_size = guard.execute("PRAGMA page_size").fetchone()[0]
        if pages * page_size > max_bytes:
            raise ValueError("Workbook catalog exceeds the bundle size limit")
        with workbook.connect() as source, closing(sqlite3.connect(catalog)) as target:
            source.backup(target, pages=256, progress=lambda *args: _deadline(deadline))
        blobs, artifacts = _validate_catalog(catalog, deadline)
        total = catalog.stat().st_size
        if len(blobs) + 2 > max_members or total + sum(blobs.values()) > max_bytes:
            raise ValueError("Workbook exceeds the bundle size or member limit")
        for digest, size in sorted(blobs.items()):
            with (
                _regular(workbook.blobs / digest) as source,
                (directory / "blobs" / digest).open("xb") as target,
            ):
                _copy_verified(source, target, size, digest, deadline)
            total += size
    return blobs, artifacts, total


def _compact_catalog(path, deadline):
    # A raw SQLite backup can retain deleted content in free pages. Compact the
    # validated copy so expired rows and discarded FTS pages are not transported.
    with closing(sqlite3.connect(path)) as db:
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("VACUUM")


def export_bundle(
    workbook, path, *, principal=None, max_bytes=MAX_BYTES, max_members=MAX_MEMBERS
):
    """Create a new bundle, blocking catalog writers/GC during snapshot copying."""
    authorize_capability(principal or current_principal(), "export")
    _limits(max_bytes, max_members)
    path = Path(path).expanduser().absolute()
    if os.path.lexists(path):
        raise ValueError("Bundle output must not already exist")
    deadline = time.monotonic() + SECONDS
    with tempfile.TemporaryDirectory(
        prefix=".workspool-export-", dir=path.parent
    ) as directory:
        stage = Path(directory)
        blobs, artifacts, total = _snapshot(
            workbook, stage, max_bytes, max_members, deadline
        )
        _compact_catalog(stage / "workbook.sqlite3", deadline)
        manifest = {
            "format": FORMAT,
            "catalog": _file_record(stage / "workbook.sqlite3"),
            "blobs": [
                {"sha256": digest, "bytes": size}
                for digest, size in sorted(blobs.items())
            ],
            "artifacts": artifacts,
        }
        encoded = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
        if len(encoded) > MAX_MANIFEST or total + len(encoded) > max_bytes:
            raise ValueError("Workbook exceeds the bundle size limit")
        archive_path = stage / "bundle.zip"
        with zipfile.ZipFile(
            archive_path, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("manifest.json", encoded)
            for name in [
                "workbook.sqlite3",
                *("blobs/" + digest for digest in sorted(blobs)),
            ]:
                record = (
                    manifest["catalog"]
                    if name == "workbook.sqlite3"
                    else {"sha256": name[6:], "bytes": blobs[name[6:]]}
                )
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o600) << 16
                with (
                    _regular(stage / name) as source,
                    archive.open(info, "w", force_zip64=True) as target,
                ):
                    _copy_verified(
                        source, target, record["bytes"], record["sha256"], deadline
                    )
        os.chmod(archive_path, 0o600)
        # A competing output creator wins; never replace an existing archive.
        os.link(archive_path, path)
    return {
        "path": str(path),
        "artifacts": artifacts,
        "blobs": len(blobs),
        **_file_record(path),
    }


def _zip_bounds(source, max_bytes, max_members):
    """Bound central-directory allocation before zipfile reads it (also ZIP64)."""
    source.seek(0, os.SEEK_END)
    size = source.tell()
    if size > max_bytes + max_members * 512 + 65557:
        raise ValueError("Bundle archive exceeds the size limit")
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    position = tail.rfind(b"PK\x05\x06")
    if position < 0 or len(tail) - position < 22:
        raise ValueError("Bundle is not a complete ZIP archive")
    _, disk, start_disk, count_disk, count, length, offset, comment = (
        struct.unpack_from("<4s4H2LH", tail, position)
    )
    if comment or position + 22 != len(tail) or disk or start_disk:
        raise ValueError("Bundle ZIP comments and split archives are unsupported")
    if count == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        end = size - len(tail) + position
        source.seek(end - 20)
        locator = source.read(20)
        if len(locator) != 20:
            raise ValueError("Malformed ZIP64 bundle")
        signature, zip_disk, zip_offset, disks = struct.unpack("<4sLQL", locator)
        if signature != b"PK\x06\x07" or zip_disk or disks != 1:
            raise ValueError("Malformed ZIP64 bundle")
        source.seek(zip_offset)
        header = source.read(56)
        if len(header) != 56:
            raise ValueError("Malformed ZIP64 bundle")
        signature, _, _, _, disk, start_disk, count_disk, count, length, offset = (
            struct.unpack("<4sQ2H2L4Q", header)
        )
        if signature != b"PK\x06\x06" or disk or start_disk:
            raise ValueError("Malformed ZIP64 bundle")
    if (
        count != count_disk
        or count > max_members
        or length > max_members * 512
        or offset + length > size
    ):
        raise ValueError("Bundle central directory exceeds its limits")
    source.seek(0)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in workbook bundle")
        result[key] = value
    return result


def _manifest(archive, max_bytes, max_members):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) > max_members or len(set(names)) != len(names):
        raise ValueError("Bundle has duplicate or excessive archive members")
    for entry in entries:
        mode = entry.external_attr >> 16
        if (
            len(entry.filename) > 256
            or entry.compress_type != zipfile.ZIP_STORED
            or entry.flag_bits & 1
            or entry.is_dir()
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
        ):
            raise ValueError("Bundle members must be unencrypted regular stored files")
    if (
        "manifest.json" not in names
        or archive.getinfo("manifest.json").file_size > MAX_MANIFEST
    ):
        raise ValueError("Bundle manifest is missing or oversized")
    manifest = json.loads(
        archive.read("manifest.json"), object_pairs_hook=_unique_object
    )
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"format", "catalog", "blobs", "artifacts"}
        or manifest["format"] != FORMAT
    ):
        raise ValueError("Unsupported workbook bundle format")
    if (
        type(manifest["artifacts"]) is not int
        or not 0 <= manifest["artifacts"] <= MAX_ARTIFACTS
    ):
        raise ValueError("Invalid bundle artifact count")
    if (
        not isinstance(manifest["blobs"], list)
        or len(manifest["blobs"]) + 2 > max_members
    ):
        raise ValueError("Invalid bundle blob list")
    records = {"workbook.sqlite3": manifest["catalog"]}
    for record in [manifest["catalog"], *manifest["blobs"]]:
        if (
            not isinstance(record, dict)
            or set(record) != {"sha256", "bytes"}
            or not isinstance(record["sha256"], str)
            or not DIGEST.fullmatch(record["sha256"])
            or type(record["bytes"]) is not int
            or not 0 <= record["bytes"] <= max_bytes
        ):
            raise ValueError("Malformed bundle member metadata")
    for record in manifest["blobs"]:
        name = "blobs/" + record["sha256"]
        if name in records:
            raise ValueError("Duplicate blob in bundle manifest")
        records[name] = record
    if set(names) != {"manifest.json", *records}:
        raise ValueError("Bundle contains unexpected or missing member paths")
    if sum(entry.file_size for entry in entries) > max_bytes:
        raise ValueError("Bundle exceeds its expanded byte limit")
    for name, record in records.items():
        if archive.getinfo(name).file_size != record["bytes"]:
            raise ValueError("Bundle declared sizes do not match archive members")
    return manifest, records


def _deactivate(catalog, principal, archive_sha256):
    """A restored snapshot retains evidence, not live process authority."""
    with closing(sqlite3.connect(catalog)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        names = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "coordination_claims" in names:
            db.execute("UPDATE coordination_claims SET owner=NULL,expires_at=0")
        if "continuity_sessions" in names:
            db.execute(
                "UPDATE continuity_sessions SET ended_at=? WHERE ended_at IS NULL",
                (time.time(),),
            )
        if "task_runs" in names:
            rows = db.execute(
                "SELECT run_id,payload FROM task_runs WHERE state='running'"
            ).fetchall()
            for run_id, encoded in rows:
                payload = json.loads(encoded)
                if not isinstance(payload, dict):
                    raise TypeError("Malformed running task state in bundle")
                payload.update(
                    state="partial",
                    reason="Interrupted by workbook restore; resume explicitly",
                )
                db.execute(
                    "UPDATE task_runs SET state='partial',finished=?,payload=? WHERE run_id=?",
                    (time.time(), json.dumps(payload, separators=(",", ":")), run_id),
                )
        if "task_schedules" in names:
            db.execute(
                "UPDATE task_schedules SET enabled=0,lease_owner=NULL,lease_until=0"
            )
        append_event(
            db,
            principal,
            "workbook.restored",
            "workbook",
            {"archive_sha256": archive_sha256},
        )


def _import_bundle(
    path, destination, *, principal=None, max_bytes=MAX_BYTES, max_members=MAX_MEMBERS
):
    """Verify then atomically install into a newly reserved destination only."""
    principal = principal or current_principal()
    authorize_capability(principal, "import")
    _limits(max_bytes, max_members)
    destination = Path(destination).expanduser().absolute()
    if os.path.lexists(destination):
        raise ValueError("Restore destination must not already exist")
    deadline = time.monotonic() + SECONDS
    with tempfile.TemporaryDirectory(
        prefix=".workspool-restore-", dir=destination.parent
    ) as directory:
        stage = Path(directory) / "workbook"
        stage.mkdir(mode=0o700)
        (stage / "blobs").mkdir(mode=0o700)
        with _regular(Path(path)) as source:
            before = os.fstat(source.fileno())
            _zip_bounds(source, max_bytes, max_members)
            with zipfile.ZipFile(source) as archive:
                manifest, records = _manifest(archive, max_bytes, max_members)
                for name, record in records.items():
                    with (
                        archive.open(name) as member,
                        (stage / name).open("xb") as target,
                    ):
                        _copy_verified(
                            member, target, record["bytes"], record["sha256"], deadline
                        )
                    os.chmod(stage / name, 0o600)
            source.seek(0)
            checksum = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024**2), b""):
                _deadline(deadline)
                checksum.update(chunk)
            after = os.fstat(source.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("Bundle archive changed during restore")
            archive_sha256 = checksum.hexdigest()
        blobs, artifacts = _validate_catalog(stage / "workbook.sqlite3", deadline)
        if artifacts != manifest["artifacts"] or blobs != {
            row["sha256"]: row["bytes"] for row in manifest["blobs"]
        }:
            raise ValueError("Bundle manifest does not match catalog blob references")
        _deactivate(stage / "workbook.sqlite3", principal, archive_sha256)
        _validate_catalog(stage / "workbook.sqlite3", deadline)
        # Reserve with mkdir (never overwrite), then replace our own empty
        # reservation with the validated directory in one rename. A concurrent
        # importer loses mkdir; a writer filling the reservation prevents rename.
        destination.mkdir(mode=0o700)
        reservation = destination.stat()
        try:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) != (
                reservation.st_dev,
                reservation.st_ino,
            ):
                raise ValueError("Restore destination changed during installation")
            os.rename(stage, destination)
        except BaseException:
            current = destination.lstat()
            if (current.st_dev, current.st_ino) == (
                reservation.st_dev,
                reservation.st_ino,
            ):
                try:
                    destination.rmdir()
                except OSError:
                    pass
            raise
    return {
        "path": str(destination),
        "artifacts": artifacts,
        "blobs": len(blobs),
        "archive_sha256": archive_sha256,
        "live_execution_restored": False,
    }


def import_bundle(
    path, destination, *, principal=None, max_bytes=MAX_BYTES, max_members=MAX_MEMBERS
):
    """Restore a verified new workbook; reject malformed transport/catalog data."""
    try:
        return _import_bundle(
            path,
            destination,
            principal=principal,
            max_bytes=max_bytes,
            max_members=max_members,
        )
    except (
        zipfile.BadZipFile,
        sqlite3.Error,
        UnicodeError,
        EOFError,
        struct.error,
        TypeError,
    ) as error:
        raise ValueError("Malformed workbook bundle") from error
