"""Persistent lexical retrieval over complete, immutable UTF-8 sources.

The disposable cache is separate from the workbook catalog. A version becomes
searchable only after its entire source passes strict UTF-8 and SHA-256 checks.
Warm reads check device/inode/size/mtime/ctime and re-read exact returned chunks;
they do not rehash unchanged files. This assumes the local filesystem reports
changes honestly, as the workbook's immutable blob contract requires.

Queries are literal Unicode words joined by OR, ranked with SQLite FTS5 BM25.
Scores use the indexed corpus (including retained historical versions), not a
semantic model or a probability. Lower scores rank first; ties use name, version,
and byte offset. Ranked limits are explicitly distinct from indexing coverage.
"""

import hashlib
import json
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager

from .store import Workbook, is_text

CHUNK_BYTES = 2048
OVERLAP_BYTES = 256
READ_BYTES = 64 * 1024
MAX_INDEX_BYTES = 128 * 1024 * 1024
MAX_INDEX_VERSIONS = 128
MAX_SCOPE_VERSIONS = 10000
MAX_CLEANUP_VERSIONS = 256
INDEX_SECONDS = 30
QUERY_SECONDS = 5
CACHE_VERSION = 1
EXCLUDED_KINDS = frozenset({"memory_episode"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    artifact_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    stamp TEXT NOT NULL,
    chunks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES versions(artifact_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL UNIQUE,
    start_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL,
    digest TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_artifact ON chunks(artifact_id);
CREATE INDEX IF NOT EXISTS chunks_digest ON chunks(digest);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(
    body, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunk_search(rowid, body) VALUES (new.id, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunk_search(chunk_search, rowid, body) VALUES ('delete', old.id, old.body);
END;
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _stamp(info):
    return _json(
        [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    )


def _source_stamp(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValueError(
            "Indexed source was deleted; resolve the current version"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Indexed source must be a regular file, not a symbolic link")
    return _stamp(info)


@contextmanager
def _source(path):
    before = _source_stamp(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise ValueError("Indexed source is no longer readable") from None
    with os.fdopen(descriptor, "rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != before:
            raise ValueError("Indexed source changed outside the workbook")
        yield stream, before
        if _stamp(os.fstat(stream.fileno())) != before or _source_stamp(path) != before:
            raise ValueError("Indexed source changed outside the workbook")


@contextmanager
def _cache(workbook):
    path = workbook.root / "context-index.sqlite3"
    if path.is_symlink():
        raise ValueError("Context index must not be a symbolic link")
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, CACHE_VERSION):
            raise ValueError(
                "Unsupported context index version; rebuild the disposable cache"
            )
        db.executescript(SCHEMA)
        if version == 0:
            db.execute(f"PRAGMA user_version = {CACHE_VERSION}")
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield db
    finally:
        db.close()


def _scope(workbook, catalog, refs):
    if refs is not None and (
        not isinstance(refs, list)
        or not 1 <= len(refs) <= 16
        or any(not isinstance(ref, str) or not ref for ref in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError("Provide 1-16 distinct artifact references, or omit refs")
    fact_ledger = _has_fact_ledger(catalog)
    eligible = (
        "kind != 'memory_episode' AND (kind != 'memory_fact' OR EXISTS "
        "(SELECT 1 FROM continuity_facts f JOIN continuity_episodes e "
        "ON e.artifact_id=f.episode_artifact_id WHERE f.fact_artifact_id=latest.artifact_id))"
        if fact_ledger
        else "kind NOT IN ('memory_episode','memory_fact')"
    )
    rows = (
        [workbook._get(catalog, ref) for ref in refs]
        if refs is not None
        else catalog.execute(
            "SELECT * FROM latest WHERE " + eligible + " ORDER BY name LIMIT ?",
            (MAX_SCOPE_VERSIONS + 1,),
        ).fetchall()
    )
    if len(rows) > MAX_SCOPE_VERSIONS:
        raise ValueError(
            "Index scope is too large; provide explicit artifact references"
        )
    selected = {}
    for row in rows:
        if not _searchable(catalog, row["artifact_id"], row["kind"], fact_ledger):
            # Ledger inspection is explicit; repeating goals/accounting is not
            # evidence. Explicit refs cannot reintroduce this excluded kind.
            continue
        if not is_text(row["media_type"]):
            if refs is not None:
                raise ValueError(f"{row['name']} is not searchable text")
            continue
        artifact_id = row["artifact_id"]
        if artifact_id in selected:
            raise ValueError("References must resolve to distinct versions")
        selected[artifact_id] = (
            dict(row),
            {key: row[key] for key in ("artifact_id", "name", "version")}
            | workbook._status(catalog, artifact_id),
        )
    return selected


def _has_fact_ledger(catalog):
    return (
        catalog.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('continuity_facts','continuity_episodes')"
        ).fetchone()[0]
        == 2
    )


def _searchable(catalog, artifact_id, kind, fact_ledger):
    if kind in EXCLUDED_KINDS:
        return False
    if kind != "memory_fact":
        return True
    # Artifact creation precedes publication. Only an atomically published
    # episode owner makes the projection recallable; orphan retries stay hidden.
    return bool(
        fact_ledger
        and catalog.execute(
            "SELECT 1 FROM continuity_facts f JOIN continuity_episodes e "
            "ON e.artifact_id=f.episode_artifact_id WHERE f.fact_artifact_id=? LIMIT 1",
            (artifact_id,),
        ).fetchone()
    )


def _boundary(data, end):
    while 0 < end < len(data) and data[end] & 0xC0 == 0x80:
        end -= 1
    return end


class _BuildLimit(Exception):
    pass


def _chunks(stream, digest, deadline):
    """Yield bounded, overlapping exact byte ranges without buffering a source."""
    pending, offset = b"", 0
    while True:
        if time.monotonic() > deadline:
            raise _BuildLimit
        block = stream.read(READ_BYTES)
        digest.update(block)
        pending += block
        while len(pending) > CHUNK_BYTES + 3 or (
            not block and len(pending) > CHUNK_BYTES
        ):
            if time.monotonic() > deadline:
                raise _BuildLimit
            end = _boundary(pending, CHUNK_BYTES)
            newline = pending.rfind(b"\n", CHUNK_BYTES // 2, end)
            if newline >= 0:
                end = newline + 1
            body = pending[:end]
            yield offset, body
            advance = _boundary(pending, end - OVERLAP_BYTES)
            offset += advance
            pending = pending[advance:]
        if not block:
            if pending:
                yield offset, pending
            return


def _index_version(cache, workbook, row, deadline):
    artifact_id, expected = row["artifact_id"], row["digest"]
    path = workbook.blobs / expected
    with _source(path) as (stream, stamp):
        cache.execute("DELETE FROM versions WHERE artifact_id = ?", (artifact_id,))
        cache.execute(
            "INSERT INTO versions VALUES (?, ?, ?, 0)", (artifact_id, expected, stamp)
        )
        digest, count, end = hashlib.sha256(), 0, 0
        try:
            for start, raw in _chunks(stream, digest, deadline):
                body = raw.decode("utf-8", errors="strict")
                end = start + len(raw)
                if end > row["size_bytes"]:
                    raise ValueError("Indexed source changed outside the workbook")
                chunk_hash = hashlib.sha256(raw).hexdigest()
                chunk_id = (
                    "wc_"
                    + hashlib.sha256(
                        f"{CACHE_VERSION}:{artifact_id}:{expected}:{start}:{end}:{chunk_hash}".encode()
                    ).hexdigest()
                )
                cache.execute(
                    """INSERT INTO chunks
                    (artifact_id, chunk_id, start_byte, end_byte, digest, body)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (artifact_id, chunk_id, start, end, chunk_hash, body),
                )
                count += 1
        except UnicodeDecodeError:
            raise ValueError(f"{row['name']} is not valid UTF-8 text") from None
        if end != row["size_bytes"] or digest.hexdigest() != expected:
            raise ValueError("Indexed source changed outside the workbook")
        cache.execute(
            "UPDATE versions SET chunks = ? WHERE artifact_id = ?", (count, artifact_id)
        )


def _prune(cache, catalog):
    """Bound maintenance even when many historical versions remain cached."""
    cursor = cache.execute(
        "SELECT value FROM maintenance WHERE key = 'cleanup_cursor'"
    ).fetchone()
    saved_versions = cache.execute(
        "SELECT artifact_id FROM versions WHERE artifact_id > ? ORDER BY artifact_id LIMIT ?",
        (cursor[0] if cursor else "", MAX_CLEANUP_VERSIONS),
    ).fetchall()
    fact_ledger = _has_fact_ledger(catalog)
    for saved in saved_versions:
        source = catalog.execute(
            "SELECT kind FROM artifacts WHERE artifact_id = ?", (saved[0],)
        ).fetchone()
        if source is None or not _searchable(
            catalog, saved[0], source["kind"], fact_ledger
        ):
            cache.execute("DELETE FROM versions WHERE artifact_id = ?", (saved[0],))
    next_cursor = (
        saved_versions[-1][0] if len(saved_versions) == MAX_CLEANUP_VERSIONS else ""
    )
    cache.execute(
        "INSERT INTO maintenance VALUES ('cleanup_cursor', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (next_cursor,),
    )


def clean_recall_cache(workbook):
    """Bounded cleanup after retention commits, including legacy episode chunks.

    Selection excludes ledgers immediately. Cleanup visits at most 256 cached
    versions per call, so large existing caches are reclaimed incrementally.
    """
    path = workbook.root / "context-index.sqlite3"
    if not path.exists():
        return {"versions_removed": 0, "chunks_removed": 0, "vectors_removed": 0}
    with workbook.connect() as catalog, _cache(workbook) as cache:
        catalog.execute("BEGIN")
        before = cache.execute("SELECT count(*) FROM versions").fetchone()[0]
        chunks = cache.execute("SELECT count(*) FROM chunks").fetchone()[0]
        _prune(cache, catalog)
        versions_removed = (
            before - cache.execute("SELECT count(*) FROM versions").fetchone()[0]
        )
        chunks_removed = (
            chunks - cache.execute("SELECT count(*) FROM chunks").fetchone()[0]
        )
    from .semantic import clean_vector_cache

    return {
        "versions_removed": versions_removed,
        "chunks_removed": chunks_removed,
        "vectors_removed": clean_vector_cache(workbook),
    }


def _prepare(cache, catalog, workbook, selected, max_index_bytes):
    # Cleanup follows the same catalog snapshot as selection and freshness.
    deadline = time.monotonic() + INDEX_SECONDS
    _prune(cache, catalog)
    built, indexed_bytes, pending = 0, 0, []
    for artifact_id, (row, metadata) in selected.items():
        stamp = _source_stamp(workbook.blobs / row["digest"])
        saved = cache.execute(
            "SELECT * FROM versions WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if (
            saved is not None
            and saved["digest"] == row["digest"]
            and saved["stamp"] == stamp
        ):
            continue
        reason = None
        if row["size_bytes"] > max_index_bytes - indexed_bytes:
            reason = "index_byte_budget"
        elif built >= MAX_INDEX_VERSIONS:
            reason = "index_version_budget"
        elif time.monotonic() > deadline:
            reason = "index_time_budget"
        if reason is None:
            cache.execute("SAVEPOINT index_version")
            try:
                _index_version(cache, workbook, row, deadline)
            except _BuildLimit:
                cache.execute("ROLLBACK TO index_version")
                reason = "index_time_budget"
            finally:
                cache.execute("RELEASE index_version")
        if reason:
            pending.append(metadata | {"reason": reason})
        else:
            built += 1
            indexed_bytes += row["size_bytes"]
    return built, indexed_bytes, pending


def _query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ValueError("Ranked query must contain 1-1000 characters")
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query, re.UNICODE)))
    if not terms or len(terms) > 32:
        raise ValueError("Ranked query must contain 1-32 Unicode words")
    return " OR ".join('"' + term + '"' for term in terms)


def _hit(workbook, selected, row):
    source, metadata = selected[row["artifact_id"]]
    with _source(workbook.blobs / source["digest"]) as (stream, stamp):
        if stamp != row["stamp"]:
            raise ValueError("Indexed source changed outside the workbook")
        stream.seek(row["start_byte"])
        raw = stream.read(row["end_byte"] - row["start_byte"])
        if (
            hashlib.sha256(raw).hexdigest() != row["digest"]
            or raw != row["body"].encode()
        ):
            raise ValueError("Indexed chunk differs from its exact source bytes")
    return metadata | {
        "chunk_id": row["chunk_id"],
        "sha256": source["digest"],
        "start_byte": row["start_byte"],
        "end_byte": row["end_byte"],
        "excerpt": row["body"],
        "score": row["score"],
    }


def ranked_search(
    workbook: Workbook,
    query: str,
    refs: list[str] | None = None,
    *,
    limit: int = 8,
    max_bytes: int = 16384,
    max_index_bytes: int = MAX_INDEX_BYTES,
) -> dict:
    """Return at most 64 ranked chunks with exact immutable byte citations.

    refs may pin historical versions; omitted refs select current text versions.
    Each call indexes up to 128 MiB/128 versions/30 seconds. Whole verified
    versions are retained across calls; pending versions are never searched.
    A single larger source needs an explicit larger max_index_bytes (up to 8 GiB).
    max_bytes bounds the entire JSON reply; chunks are never silently cut.
    ``index_complete`` means the selected sources were fully indexed. ``complete``
    additionally means no matching chunks were omitted by result/output limits.
    There is no exhaustive continuation cursor; this API supplies ranked evidence.
    """
    match = _query(query)
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("Ranked limit must be between 1 and 64")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 256 * 1024:
        raise ValueError("Ranked max_bytes must be between 1 and 262144")
    if type(max_index_bytes) is not int or not 1 <= max_index_bytes <= 8 * 1024**3:
        raise ValueError("Index byte budget must be between 1 and 8 GiB")
    with workbook.connect() as catalog, _cache(workbook) as cache:
        catalog.execute("BEGIN")
        selected = _scope(workbook, catalog, refs)
        built, indexed_bytes, pending = _prepare(
            cache, catalog, workbook, selected, max_index_bytes
        )
        excluded = {item["artifact_id"] for item in pending}
        cache.execute(
            "CREATE TEMP TABLE scope (artifact_id TEXT PRIMARY KEY, name TEXT, version INTEGER)"
        )
        cache.executemany(
            "INSERT INTO scope VALUES (?, ?, ?)",
            (
                (key, row[0]["name"], row[0]["version"])
                for key, row in selected.items()
                if key not in excluded
            ),
        )
        deadline = time.monotonic() + QUERY_SECONDS
        cache.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        try:
            candidates = cache.execute(
                """SELECT c.*, v.stamp, bm25(chunk_search) AS score
                FROM chunk_search JOIN chunks c ON c.id = chunk_search.rowid
                JOIN scope s ON s.artifact_id = c.artifact_id
                JOIN versions v ON v.artifact_id = c.artifact_id
                WHERE chunk_search MATCH ?
                ORDER BY score, s.name, s.version, c.start_byte LIMIT ?""",
                (match, limit + 1),
            ).fetchall()
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise ValueError(
                    "Ranked query time budget exceeded; narrow the source scope"
                ) from None
            raise
        finally:
            cache.set_progress_handler(None, 0)
        result = {
            "hits": [],
            "index_complete": not pending,
            "complete": not pending and len(candidates) <= limit,
            "truncated": len(candidates) > limit,
            "indexed_versions": built,
            "indexed_bytes": indexed_bytes,
            "pending_versions": len(pending),
            "pending": pending[:16],
        }
        if len(_json(result).encode()) > max_bytes:
            raise ValueError("Ranked output budget cannot fit index status metadata")
        for candidate in candidates[:limit]:
            hit = _hit(workbook, selected, candidate)
            result["hits"].append(hit)
            if len(_json(result).encode()) > max_bytes:
                result["hits"].pop()
                result["complete"], result["truncated"] = False, True
                if not result["hits"]:
                    raise ValueError("Ranked output budget cannot fit one exact chunk")
                break
    # The lexical cache transaction must finish before the separate vector
    # cache is pruned, avoiding competing lock order with hybrid retrieval.
    from .semantic import clean_vector_cache

    clean_vector_cache(workbook)
    return result
