"""Versioned, local artifacts and a searchable workbook catalog."""

import codecs
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .settings import Settings

try:
    import fcntl
except ImportError:  # Windows keeps legacy staging; no dead-writer cleanup claim.
    fcntl = None

TABLE_TYPES = {"text/csv", "application/vnd.apache.parquet"}
MEDIA_BY_SUFFIX = {".csv": "text/csv", ".parquet": "application/vnd.apache.parquet"}
BLOB_NAME = re.compile(r"[0-9a-f]{64}")
STAGING_NAME = re.compile(r"\.workspool-stage-[0-9a-f]{32}-[a-z0-9_]+\.tmp")

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    source TEXT NOT NULL,
    digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    previous_id TEXT REFERENCES artifacts(artifact_id),
    checkpoint TEXT,
    recipe TEXT,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS dependencies (
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    dependency_id TEXT REFERENCES artifacts(artifact_id),
    position INTEGER NOT NULL,
    PRIMARY KEY (artifact_id, dependency_id)
);
CREATE VIEW IF NOT EXISTS latest AS
    SELECT a.* FROM artifacts a
    WHERE a.version = (SELECT MAX(b.version) FROM artifacts b WHERE b.name = a.name);
CREATE VIRTUAL TABLE IF NOT EXISTS artifact_search
    USING fts5(artifact_id UNINDEXED, name, summary, body);
CREATE TABLE IF NOT EXISTS name_versions (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version > 0)
);
INSERT INTO name_versions (name, version)
    SELECT name, MAX(version) FROM artifacts GROUP BY name
    ON CONFLICT (name) DO UPDATE SET
        version = MAX(name_versions.version, excluded.version);
CREATE TRIGGER IF NOT EXISTS artifacts_version_guard
BEFORE INSERT ON artifacts
WHEN NEW.version <= COALESCE(
    (SELECT version FROM name_versions WHERE name = NEW.name), 0
)
BEGIN
    SELECT RAISE(ABORT, 'Artifact versions must advance; reopen with an updated client');
END;
CREATE TRIGGER IF NOT EXISTS artifacts_version_record
AFTER INSERT ON artifacts
BEGIN
    INSERT INTO name_versions (name, version) VALUES (NEW.name, NEW.version)
    ON CONFLICT (name) DO UPDATE SET version = MAX(name_versions.version, excluded.version);
END;
"""


def default_root() -> Path:
    """WORKSPOOL_HOME wins; AGENT_WORKBOOK_HOME is still honoured."""
    for variable in ("WORKSPOOL_HOME", "AGENT_WORKBOOK_HOME"):
        if os.environ.get(variable):
            return Path(os.environ[variable]).expanduser()
    return Path("~/.agent-workbook/default").expanduser()


def guess_media(path: Path) -> str:
    """CSV and Parquet by suffix, otherwise the platform's guess, otherwise bytes."""
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return MEDIA_BY_SUFFIX.get(path.suffix.lower(), guessed)


class Workbook:
    def __init__(self, root: Path, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.root = root.expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.catalog = self.root / "workbook.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.catalog, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, str.casefold, deterministic=True)
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db, ref: str):
        row = db.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (ref,)
        ).fetchone()
        if row is None:
            row = db.execute("SELECT * FROM latest WHERE name = ?", (ref,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown artifact: {ref}")
        return row

    @staticmethod
    def _dependencies(db, artifact_id: str) -> list[str]:
        return [
            row[0]
            for row in db.execute(
                "SELECT dependency_id FROM dependencies WHERE artifact_id = ? ORDER BY position",
                (artifact_id,),
            )
        ]

    @staticmethod
    def _check_lineage(db, name: str, ids: list[str]) -> None:
        if not ids:
            return
        # Follow pinned inputs as well as the current versions a refresh would use.
        slots = ", ".join("?" for _ in ids)
        cycle = db.execute(
            f"""
            WITH RECURSIVE lineage(id) AS (
                SELECT artifact_id FROM artifacts WHERE artifact_id IN ({slots})
                UNION
                SELECT d.dependency_id FROM dependencies d JOIN lineage l ON d.artifact_id = l.id
                UNION
                SELECT cur.artifact_id FROM lineage l
                JOIN artifacts a ON a.artifact_id = l.id
                JOIN latest cur ON cur.name = a.name
            )
            SELECT 1 FROM lineage l JOIN artifacts a ON a.artifact_id = l.id
            WHERE a.name = ? LIMIT 1
            """,
            (*ids, name),
        ).fetchone()
        if cycle:
            raise ValueError(f"Saving {name!r} would create a dependency cycle")

    @staticmethod
    def _status(db, artifact_id: str) -> dict:
        row = db.execute(
            """
            WITH RECURSIVE ancestors(id) AS (
                SELECT dependency_id FROM dependencies WHERE artifact_id = ?
                UNION
                SELECT d.dependency_id FROM dependencies d JOIN ancestors a ON d.artifact_id = a.id
            )
            SELECT
                EXISTS (SELECT 1 FROM latest WHERE artifact_id = ?) AS is_latest,
                EXISTS (
                    SELECT 1 FROM ancestors
                    WHERE NOT EXISTS (SELECT 1 FROM latest WHERE artifact_id = ancestors.id)
                ) AS stale_inputs
            """,
            (artifact_id, artifact_id),
        ).fetchone()
        return {key: bool(row[key]) for key in ("is_latest", "stale_inputs")}

    def _summary(self, db, row) -> dict:
        item = {
            key: row[key]
            for key in (
                "artifact_id",
                "name",
                "version",
                "kind",
                "media_type",
                "summary",
                "source",
                "created_at",
                "size_bytes",
            )
        }
        return item | self._status(db, row["artifact_id"])

    @contextmanager
    def _staging(self):
        """Create and lock a stage before GC can observe it.

        POSIX flock is held until the writer closes the file, including across
        promotion to its content hash. The catalog write lock closes the race
        between creating a visible stage and acquiring its file lock. SIGKILL
        releases both locks automatically. Platforms without flock retain the
        prior unmarked staging behavior, which GC deliberately does not touch.
        """
        with ExitStack() as files:
            staged = None
            try:
                if fcntl is None:
                    staged = files.enter_context(
                        tempfile.NamedTemporaryFile(dir=self.blobs, delete=False)
                    )
                else:
                    with self.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        staged = files.enter_context(
                            tempfile.NamedTemporaryFile(
                                dir=self.blobs,
                                prefix=f".workspool-stage-{uuid4().hex}-",
                                suffix=".tmp",
                                delete=False,
                            )
                        )
                        fcntl.flock(staged.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield staged
            finally:
                if staged is not None:
                    Path(staged.name).unlink(missing_ok=True)

    def _save(
        self,
        stream,
        *,
        name: str,
        kind: str,
        media_type: str,
        summary: str = "",
        source: str = "",
        dependencies: list[str] | None = None,
        expected_version: int | None = None,
        checkpoint: dict | None = None,
        recipe: dict | None = None,
    ) -> dict:
        name = name.strip()
        if not name or len(name) > 120 or name.startswith("wb_"):
            raise ValueError("Name must be 1–120 characters and cannot start with wb_")
        if len(summary) > 360 or len(source) > 360:
            raise ValueError("Summary and source must each be at most 360 characters")
        if len(dependencies or []) > 16:
            raise ValueError("At most 16 dependencies are supported")
        digest = hashlib.sha256()
        index_bytes = self.settings.index_bytes
        with self._staging() as staged:
            staged_path = Path(staged.name)
            try:
                size = 0
                preview = b""
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    staged.write(chunk)
                    if len(preview) < index_bytes:
                        preview += chunk[: index_bytes - len(preview)]
                staged.flush()
                os.fsync(staged.fileno())
                content_hash = digest.hexdigest()
                text = (
                    preview.decode("utf-8", errors="replace")
                    if is_text(media_type)
                    else ""
                )
                if checkpoint:
                    text = "\n".join(checkpoint.values()) + "\n" + text
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    previous = db.execute(
                        "SELECT * FROM latest WHERE name = ?", (name,)
                    ).fetchone()
                    actual_version = previous["version"] if previous else 0
                    if actual_version != (expected_version or 0):
                        raise ValueError(
                            f"{name!r} is at version {actual_version}; "
                            "pass its current version as expected_version to update it"
                        )
                    counter = db.execute(
                        "SELECT version FROM name_versions WHERE name = ?", (name,)
                    ).fetchone()
                    next_version = max(actual_version, counter[0] if counter else 0) + 1
                    parents = [self._get(db, ref) for ref in dependencies or []]
                    ids = list(
                        dict.fromkeys(parent["artifact_id"] for parent in parents)
                    )
                    self._check_lineage(db, name, ids)
                    blob = self.blobs / content_hash
                    if not blob.exists():
                        os.replace(staged_path, blob)
                    artifact_id = "wb_" + uuid4().hex
                    db.execute(
                        "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            artifact_id,
                            name,
                            next_version,
                            kind,
                            media_type,
                            summary,
                            source,
                            content_hash,
                            size,
                            datetime.now(UTC).isoformat(),
                            previous["artifact_id"] if previous else None,
                            json.dumps(checkpoint) if checkpoint else None,
                            json.dumps(recipe) if recipe else None,
                        ),
                    )
                    db.executemany(
                        "INSERT INTO dependencies VALUES (?, ?, ?)",
                        ((artifact_id, ref, pos) for pos, ref in enumerate(ids)),
                    )
                    db.execute(
                        "INSERT INTO artifact_search VALUES (?, ?, ?, ?)",
                        (artifact_id, name, summary + " " + source, text),
                    )
                    return self._summary(db, self._get(db, artifact_id))
            finally:
                staged_path.unlink(missing_ok=True)

    def save_text(self, content: str, **fields) -> dict:
        from io import BytesIO

        encoded = content.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError(
                "Use the local import command for content larger than 1 MiB"
            )
        return self._save(BytesIO(encoded), **fields)

    def save_file(self, path: Path, **fields) -> dict:
        with path.open("rb") as file:
            return self._save(file, **fields)

    def import_path(
        self,
        path: Path,
        *,
        name: str = "",
        summary: str = "",
        source: str = "",
        expected_version: int | None = None,
    ) -> dict:
        """Copy a local file into the workbook in its native format.

        Shared by the CLI `import` command and the MCP `import` tool, so both
        infer the same media type and kind from the file name.
        """
        path = path.expanduser()
        if not path.is_absolute():
            raise ValueError("Import requires an absolute path")
        if not path.is_file():
            raise ValueError(f"Not a readable file: {path}")
        media = guess_media(path)
        return self.save_file(
            path,
            name=name or path.stem,
            kind="table" if media in TABLE_TYPES else "file",
            media_type=media,
            summary=summary,
            source=source or path.name,
            expected_version=expected_version,
        )

    def _find(self, db, text: str, limit: int) -> list[dict]:
        words = re.findall(r"[^\W_]+", text[:500], flags=re.UNICODE)[:16]
        if words:
            match = " OR ".join(f'"{word}"' for word in words)
            rows = db.execute(
                """
                SELECT a.* FROM artifact_search JOIN latest a USING (artifact_id)
                WHERE artifact_search MATCH ?
                ORDER BY bm25(artifact_search, 0, 5, 3, 1), a.created_at DESC LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM latest ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._summary(db, row) for row in rows]

    def find(self, text: str = "", limit: int = 6) -> list[dict]:
        with self.connect() as db:
            db.execute("BEGIN")
            return self._find(db, text, max(1, min(limit, 10)))

    def inspect(
        self,
        ref: str,
        excerpt_bytes: int = 0,
        offset: int = 0,
        include_recipe: bool = False,
    ) -> dict:
        return self.inspect_many([ref], excerpt_bytes, offset, include_recipe)[0]

    def inspect_many(
        self,
        refs: list[str],
        excerpt_bytes: int = 0,
        offset: int = 0,
        include_recipe: bool = False,
    ) -> list[dict]:
        """Resolve up to 16 references in one catalog snapshot, then read their blobs."""
        if not 1 <= len(refs) <= 16 or len(set(refs)) != len(refs):
            raise ValueError("Provide 1-16 distinct artifact references")
        if offset < 0:
            raise ValueError("Offset cannot be negative")
        resolved = []
        seen = set()
        with self.connect() as db:
            db.execute("BEGIN")
            for ref in refs:
                row = self._get(db, ref)
                if row["artifact_id"] in seen:
                    raise ValueError("References must resolve to distinct versions")
                seen.add(row["artifact_id"])
                item = self._summary(db, row) | {
                    "sha256": row["digest"],
                    "previous_id": row["previous_id"],
                    "dependencies": self._dependencies(db, row["artifact_id"]),
                }
                if row["checkpoint"]:
                    item["checkpoint"] = json.loads(row["checkpoint"])
                if row["recipe"]:
                    item["has_recipe"] = True
                    if include_recipe:
                        item["recipe"] = json.loads(row["recipe"])
                resolved.append((row, item))
        for row, item in resolved:
            if excerpt_bytes > 0 and is_text(row["media_type"]):
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    with (self.blobs / row["digest"]).open("rb") as stream:
                        stream.seek(min(offset, row["size_bytes"]))
                        start = stream.tell()
                        chunk = stream.read(min(max(excerpt_bytes, 4), 8192))
                except FileNotFoundError:
                    raise ValueError(
                        f"{row['artifact_id']} was deleted by another session"
                    ) from None
                end = start + len(chunk)
                item["excerpt"] = decoder.decode(chunk, final=end >= row["size_bytes"])
                item["next_offset"] = (
                    end - len(decoder.getstate()[0])
                    if end < row["size_bytes"]
                    else None
                )
        return [item for _, item in resolved]

    @staticmethod
    def _checkpoints(db, ref: str):
        if ref:
            exact = db.execute(
                """
                SELECT * FROM artifacts WHERE artifact_id = ? AND checkpoint IS NOT NULL
                UNION ALL
                SELECT * FROM latest WHERE name = ? AND checkpoint IS NOT NULL
                LIMIT 1
                """,
                (ref, ref),
            ).fetchone()
            if exact is not None:
                return [exact]
        # A scope matches whole slash-delimited components, never fuzzy content.
        scope = ref.casefold().rstrip("/")
        return db.execute(
            """
            SELECT * FROM latest
            WHERE checkpoint IS NOT NULL
              AND (? = '' OR casefold(name) = ? OR instr(casefold(name), ?) = 1)
            ORDER BY created_at DESC, name LIMIT 6
            """,
            (ref, scope, scope + "/"),
        ).fetchall()

    def resume(self, text: str = "") -> dict:
        with self.connect() as db:
            db.execute("BEGIN")
            checkpoints = self._checkpoints(db, text.strip())
            if len(checkpoints) != 1:
                result = {"checkpoint": None, "artifacts": []}
                if checkpoints:
                    result["candidates"] = [
                        {key: row[key] for key in ("artifact_id", "name", "version")}
                        for row in checkpoints[:5]
                    ]
                    result["candidates_truncated"] = len(checkpoints) > 5
                return result
            checkpoint = checkpoints[0]
            refs = self._dependencies(db, checkpoint["artifact_id"])
            # A checkpoint pins at most 16 artifacts; preview every one of them.
            return {
                "checkpoint": (
                    self._summary(db, checkpoint)
                    | json.loads(checkpoint["checkpoint"])
                    | {"dependencies": refs}
                ),
                "artifacts": [self._summary(db, self._get(db, ref)) for ref in refs],
            }

    def table_inputs(
        self, refs: list[str], allow_historical: bool
    ) -> list[tuple[dict, Path]]:
        result = []
        with self.connect() as db:
            db.execute("BEGIN")
            for ref in refs:
                row = self._get(db, ref)
                item = self._summary(db, row)
                if row["media_type"] not in TABLE_TYPES:
                    raise ValueError(f"{ref} must be a CSV or Parquet artifact")
                if not allow_historical and (
                    item["stale_inputs"] or not item["is_latest"]
                ):
                    raise ValueError(
                        f"{ref} is historical or has stale inputs; use a current version"
                    )
                result.append((item, self.blobs / row["digest"]))
        return result

    def export(self, ref: str, path: Path) -> None:
        with self.connect() as db:
            digest = self._get(db, ref)["digest"]
        with (self.blobs / digest).open("rb") as source, path.open("xb") as target:
            shutil.copyfileobj(source, target)

    @staticmethod
    def _blockers(db, ids: list[str]) -> list[dict]:
        """Artifacts outside `ids` that still pin one of `ids` as a dependency."""
        slots = ", ".join("?" for _ in ids)
        rows = db.execute(
            f"""
            SELECT DISTINCT a.artifact_id, a.name, a.version
            FROM dependencies d JOIN artifacts a ON a.artifact_id = d.artifact_id
            WHERE d.dependency_id IN ({slots}) AND d.artifact_id NOT IN ({slots})
            ORDER BY a.name, a.version LIMIT 6
            """,
            (*ids, *ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def _remove(self, db, rows, *, splice: bool) -> list[dict]:
        """Delete catalog rows inside the caller's write transaction."""
        ids = [row["artifact_id"] for row in rows]
        blockers = self._blockers(db, ids)
        if blockers:
            pinned = ", ".join(f"{b['name']} v{b['version']}" for b in blockers)
            raise ValueError(f"Still pinned by {pinned}; delete those first")
        if splice:
            # Newest first: a row's own previous_id is untouched until its
            # successor has been pointed past it.
            for row in sorted(rows, key=lambda r: (r["name"], -r["version"])):
                db.execute(
                    "UPDATE artifacts SET previous_id = ? WHERE previous_id = ?",
                    (row["previous_id"], row["artifact_id"]),
                )
        slots = ", ".join("?" for _ in ids)
        for table in ("dependencies", "artifact_search", "artifacts"):
            db.execute(f"DELETE FROM {table} WHERE artifact_id IN ({slots})", ids)
        return [
            {key: row[key] for key in ("artifact_id", "name", "version")}
            for row in rows
        ]

    def _garbage(self, db) -> list[tuple[Path, int]]:
        """List unused regular blob files while the caller holds the write lock."""
        referenced = {
            row[0] for row in db.execute("SELECT DISTINCT digest FROM artifacts")
        }
        candidates = []
        with os.scandir(self.blobs) as entries:
            for entry in entries:
                if (
                    BLOB_NAME.fullmatch(entry.name)
                    and entry.name not in referenced
                    and entry.is_file(follow_symlinks=False)
                ):
                    candidates.append((Path(entry.path), entry.stat().st_size))
        return candidates

    def _staging_candidates(self):
        """Recognizable regular stages only; old unmarked tmp files stay alone."""
        if fcntl is not None:
            with os.scandir(self.blobs) as entries:
                for entry in entries:
                    if STAGING_NAME.fullmatch(entry.name) and entry.is_file(
                        follow_symlinks=False
                    ):
                        yield Path(entry.path)

    @staticmethod
    @contextmanager
    def _claim_stage(path):
        """Yield an exclusively locked abandoned stage, or None for a live one.

        The caller must hold the catalog write lock for the entire context.
        Writers acquire that same lock before stage creation and file locking.
        """
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            opened, current = os.fstat(stream.fileno()), path.lstat()
            if not stat.S_ISREG(current.st_mode) or (opened.st_dev, opened.st_ino) != (
                current.st_dev,
                current.st_ino,
            ):
                yield None
                return
            yield stream

    def _collect_garbage(self) -> dict:
        """Clean up after catalog commit, rechecking references under a new lock.

        A save may reuse a blob between the deletion commit and this transaction.
        Serializing with saves and checking the committed references protects it.
        Cleanup failures leave unused files for a later prune, never rolled-back
        catalog records pointing to already-removed content.
        """
        count = stages = freed = failures = 0
        errors = []

        def record_error(blob: str | None, exc: Exception) -> None:
            nonlocal failures
            failures += 1
            if len(errors) < 10:
                errors.append({"blob": blob, "error": str(exc)[:240]})

        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for path, size in self._garbage(db):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        record_error(path.name, exc)
                    else:
                        count += 1
                        freed += size
                for path in self._staging_candidates():
                    try:
                        with self._claim_stage(path) as staged:
                            if staged is None:
                                continue
                            size = os.fstat(staged.fileno()).st_size
                            path.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        record_error(path.name, exc)
                    else:
                        stages += 1
                        freed += size
        except (OSError, sqlite3.Error) as exc:
            record_error(None, exc)
        result = {
            "blobs_removed": count,
            "stages_removed": stages,
            "bytes_freed": freed,
        }
        if failures:
            result |= {
                "cleanup_pending": True,
                "gc_errors": errors,
                "gc_errors_truncated": failures > len(errors),
            }
        return result

    def delete(self, ref: str, expected_version: int | None = None) -> dict:
        """Remove one superseded version by ID, or every version of a name.

        Nothing another artifact still pins can be removed; the error names
        what blocks it. Deleting a name needs its current expected_version, the
        same check an update makes. Blobs nothing references any more are
        deleted from disk.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (ref,)
            ).fetchone()
            if row is not None:
                current = db.execute(
                    "SELECT 1 FROM latest WHERE artifact_id = ?", (ref,)
                ).fetchone()
                if current:
                    raise ValueError(
                        f"{ref} is the current version of {row['name']!r}; "
                        "delete the name to remove every version"
                    )
                removed = self._remove(db, [row], splice=True)
            else:
                rows = db.execute(
                    "SELECT * FROM artifacts WHERE name = ? ORDER BY version", (ref,)
                ).fetchall()
                if not rows:
                    raise ValueError(f"Unknown artifact: {ref}")
                current = rows[-1]["version"]
                if expected_version != current:
                    raise ValueError(
                        f"{ref!r} is at version {current}; pass its current "
                        "version as expected_version to delete it"
                    )
                removed = self._remove(db, rows, splice=False)
        return {"removed": removed} | self._collect_garbage()

    def prune(self, keep: int = 1, dry_run: bool = False) -> dict:
        """Drop superseded versions nothing pins, keeping the newest `keep` per name.

        Runs to a fixed point: once a superseded result is dropped, the old
        input version it pinned becomes droppable in the same pass. Current
        versions are never touched, so names and their freshness are unchanged.
        """
        if keep < 1:
            raise ValueError("Keep at least one version of each name")
        removed = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            while True:
                rows = db.execute(
                    """
                    SELECT a.* FROM artifacts a
                    WHERE (SELECT count(*) FROM artifacts b
                           WHERE b.name = a.name AND b.version > a.version) >= ?
                      AND NOT EXISTS (SELECT 1 FROM dependencies d
                                      WHERE d.dependency_id = a.artifact_id)
                    ORDER BY a.name, a.version
                    """,
                    (keep,),
                ).fetchall()
                if not rows:
                    break
                removed += self._remove(db, rows, splice=True)
            if dry_run:
                candidates = self._garbage(db)
                stages, stage_bytes = 0, 0
                for path in self._staging_candidates():
                    try:
                        with self._claim_stage(path) as staged:
                            if staged is not None:
                                stages += 1
                                stage_bytes += os.fstat(staged.fileno()).st_size
                    except FileNotFoundError:
                        continue
                garbage = {
                    "blobs_removed": len(candidates),
                    "stages_removed": stages,
                    "bytes_freed": sum(size for _, size in candidates) + stage_bytes,
                }
                db.execute("ROLLBACK")
        if not dry_run:
            garbage = self._collect_garbage()
        return {"removed": removed, "dry_run": dry_run} | garbage


def is_text(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in {
        "application/json",
        "application/xml",
        "application/x-ndjson",
    }
