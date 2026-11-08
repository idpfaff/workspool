"""Optional local learned embeddings fused with exact lexical source citations.

Only provision_model downloads assets. Retrieval uses a hash-verified local ONNX
model in a network-disabled subprocess. An absent model produces an explicit
lexical fallback; test fixtures are not evidence of semantic model quality.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

MODEL = "BAAI/bge-small-en-v1.5"
DIMENSION = 384
MANIFEST = "workspool-model.json"
VERSION = 1
RRF_K = 60
MAX_MODEL_BYTES = 512 * 1024**2


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def provision_model(destination):
    """Explicitly download the supported FastEmbed model and seal a local copy.

    The returned manifest identifies the actual model/tokenizer file revisions
    by SHA-256. Existing directories are refused. This is the only network-enabled
    operation in this module; callers must invoke provisioning intentionally.
    """
    from fastembed import TextEmbedding

    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("Model destination already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(
        prefix=".workspool-model-", dir=destination.parent
    ) as directory:
        staging = Path(directory)
        description = TextEmbedding._get_model_description(MODEL)
        downloaded = TextEmbedding.download_model(
            description, str(staging / "download")
        )
        assets = staging / "sealed"
        assets.mkdir()
        files, total = {}, 0
        for path in sorted(Path(downloaded).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(downloaded)
            # Hub snapshots use symlinks. Provisioning copies their bytes into
            # regular files; runtime never follows an external model symlink.
            target = assets / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            size = path.stat().st_size
            total += size
            if len(files) >= 256 or total > MAX_MODEL_BYTES:
                raise ValueError("Provisioned model exceeds the supported asset budget")
            shutil.copyfile(path, target)
            files[relative.as_posix()] = {"bytes": size, "sha256": _hash(target)}
        if not files or "model_optimized.onnx" not in files:
            raise ValueError("Downloaded model is missing its ONNX weights")
        manifest = {
            "format": VERSION,
            "model": MODEL,
            "dimension": DIMENSION,
            "files": files,
        }
        encoded = _json(manifest)
        (assets / MANIFEST).write_text(encoded + "\n")
        assets.rename(destination)
    return {
        "directory": str(destination),
        "model": MODEL,
        "manifest_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        "bytes": total,
        "files": len(files),
    }


def model_spec(directory, expected_manifest=None):
    """Verify all regular assets before trusting their content-based model key."""
    directory = Path(directory).expanduser().resolve()
    manifest_path = directory / MANIFEST
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or manifest_path.stat().st_size > 128 * 1024
    ):
        raise ValueError(
            "Missing or invalid local model manifest; explicitly provision the model"
        )
    manifest = json.loads(manifest_path.read_text())
    if (
        set(manifest) != {"format", "model", "dimension", "files"}
        or manifest["format"] != VERSION
        or manifest["model"] != MODEL
        or manifest["dimension"] != DIMENSION
    ):
        raise ValueError("Unsupported local model manifest")
    files = manifest["files"]
    if not isinstance(files, dict) or not 1 <= len(files) <= 256:
        raise ValueError("Invalid model asset list")
    actual = set()
    for index, path in enumerate(directory.rglob("*")):
        if index >= 1024:
            raise ValueError("Local model inventory exceeds the inspection budget")
        if path.is_symlink():
            raise ValueError("Local model assets must not be symbolic links")
        if path.is_file() and path.name != MANIFEST:
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(files):
        raise ValueError("Local model asset inventory differs from its manifest")
    total = 0
    for relative, metadata in files.items():
        path = directory / relative
        if (
            Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not stat.S_ISREG(path.lstat().st_mode)
        ):
            raise ValueError("Invalid local model asset path")
        total += path.stat().st_size
        if (
            total > MAX_MODEL_BYTES
            or path.stat().st_size != metadata["bytes"]
            or _hash(path) != metadata["sha256"]
        ):
            raise ValueError("Local model asset hash or size mismatch")
    revision = hashlib.sha256(_json(manifest).encode()).hexdigest()
    if expected_manifest is not None and revision != expected_manifest:
        raise ValueError("Local model manifest differs from the configured revision")
    engines = {
        name: importlib.metadata.version(name)
        for name in ("fastembed", "onnxruntime", "tokenizers", "numpy")
    }
    key = hashlib.sha256(
        _json(
            {
                "manifest": revision,
                "engines": engines,
                "format": VERSION,
                "query_mode": "query_embed",
                "document_mode": "passage_embed",
            }
        ).encode()
    ).hexdigest()
    return {
        "directory": str(directory),
        "model": MODEL,
        "dimension": DIMENSION,
        "manifest_sha256": revision,
        "key": key,
        "engines": engines,
    }


def _normalized(values, dimension):
    values = [float(value) for value in values]
    if len(values) != dimension or not all(math.isfinite(value) for value in values):
        raise ValueError("Embedding has invalid dimension or nonfinite coordinates")
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        raise ValueError("Embedding has zero norm")
    return [value / norm for value in values]


def _worker(config, output):
    # Defense in depth: the dependency is told to use local files, and outgoing
    # socket connects are denied inside this single-purpose worker as well.
    def offline(*args, **kwargs):
        raise RuntimeError("Network access is disabled in the embedding worker")

    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    parent = os.getppid()
    deadline = time.monotonic() + config["timeout"]

    def watchdog():
        while True:
            if os.getppid() != parent or time.monotonic() >= deadline:
                os._exit(124)
            time.sleep(0.05)

    threading.Thread(target=watchdog, daemon=True).start()
    from fastembed import TextEmbedding

    spec = model_spec(config["model"]["directory"], config["model"]["manifest_sha256"])
    if spec["key"] != config["model"]["key"]:
        raise ValueError("Embedding engine or model changed before execution")
    model = TextEmbedding(
        model_name=MODEL,
        specific_model_path=spec["directory"],
        cache_dir=spec["directory"],
        local_files_only=True,
        providers=["CPUExecutionProvider"],
        threads=2,
    )
    query = next(iter(model.query_embed(config["query"])))
    documents = (
        list(model.passage_embed(config["documents"], batch_size=16, parallel=None))
        if config["documents"]
        else []
    )
    result = {
        "query": _normalized(query, spec["dimension"]),
        "documents": [_normalized(value, spec["dimension"]) for value in documents],
    }
    Path(output).write_text(_json(result))


def _embed(spec, query, documents, seconds):
    with TemporaryDirectory(prefix="workspool-embed-") as directory:
        source, target = (
            Path(directory) / "request.json",
            Path(directory) / "result.json",
        )
        source.write_text(
            _json(
                {
                    "model": spec,
                    "query": query,
                    "documents": documents,
                    "timeout": seconds,
                }
            )
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "OPENAI_API_KEY",
                "MASSIVE_API_KEY",
                "HF_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
            }
        }
        environment["HF_HUB_OFFLINE"] = "1"
        try:
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_workbook.semantic",
                    "--embed-worker",
                    str(source),
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=seconds,
                env=environment,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("Local embedding time budget exceeded") from error
        if process.returncode or not target.is_file():
            raise ValueError("Local embedding worker failed: " + process.stderr[-500:])
        if target.stat().st_size > 16 * 1024**2:
            raise ValueError("Embedding worker returned an oversized result")
        result = json.loads(target.read_text())
        if len(result["documents"]) != len(documents):
            raise ValueError("Embedding worker omitted documents")
        return result


@contextmanager
def _vectors(workbook, maximum_bytes):
    path = workbook.root / "semantic-vectors.sqlite3"
    if path.is_symlink():
        raise ValueError("Vector cache must not be a symbolic link")
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        # SQLite refuses growth beyond this cap. Its transient rollback journal
        # may add up to one cache-sized copy while a transaction is active.
        pages = maximum_bytes // page_size
        current = db.execute("PRAGMA page_count").fetchone()[0]
        if current > pages:
            raise ValueError("Existing vector cache exceeds the configured disk budget")
        db.execute(f"PRAGMA max_page_count={pages}")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS vectors (
                model_key TEXT NOT NULL,
                content_sha TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                data BLOB NOT NULL,
                checksum TEXT NOT NULL,
                created REAL NOT NULL,
                PRIMARY KEY(model_key,content_sha)
            );
            CREATE INDEX IF NOT EXISTS vectors_oldest ON vectors(created);
        """)
        yield db, path
    finally:
        db.close()


def clean_vector_cache(workbook):
    """Discard vectors no longer backed by a searchable exact chunk.

    This runs only after the lexical cache transaction commits. Content hashes
    shared with legitimate documents stay reusable. A busy cache is retried on a
    later recall; excluded ledger artifacts never enter retrieval scope meanwhile.
    """
    path = workbook.root / "semantic-vectors.sqlite3"
    context = workbook.root / "context-index.sqlite3"
    if not path.exists() or not context.exists():
        return 0
    if path.is_symlink() or context.is_symlink():
        raise ValueError("Recall caches must not be symbolic links")
    db = sqlite3.connect(path, timeout=0.1)
    try:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vectors'"
        ).fetchone():
            return 0
        db.execute("ATTACH DATABASE ? AS context", (str(context),))
        deadline = time.monotonic() + 1
        db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        with db:
            cursor = db.execute(
                "DELETE FROM vectors WHERE (model_key,content_sha) IN ("
                "SELECT model_key,content_sha FROM vectors WHERE NOT EXISTS "
                "(SELECT 1 FROM context.chunks c WHERE c.digest=vectors.content_sha) LIMIT 4096)"
            )
            return cursor.rowcount
    except sqlite3.OperationalError as error:
        if "lock" in str(error).lower() or "interrupt" in str(error).lower():
            return 0
        raise
    finally:
        db.close()


def _pack(values, dimension):
    normalized = _normalized(values, dimension)
    packed = struct.pack("<" + "f" * dimension, *normalized)
    return packed, hashlib.sha256(packed).hexdigest()


def _unpack(row, dimension):
    data = row["vector_data"]
    if (
        row["vector_dimension"] != dimension
        or len(data) != dimension * 4
        or hashlib.sha256(data).hexdigest() != row["vector_checksum"]
    ):
        raise ValueError("Stored embedding failed its integrity check")
    return _normalized(struct.unpack("<" + "f" * dimension, data), dimension)


def _store_vectors(db, spec, documents, vectors, maximum_bytes):
    if len(documents) != len(vectors):
        raise ValueError("Embedding provider omitted document vectors")
    rows = []
    for document, vector in zip(documents, vectors):
        data, checksum = _pack(vector, spec["dimension"])
        rows.append(
            (
                spec["key"],
                document["digest"],
                spec["dimension"],
                data,
                checksum,
                time.time(),
            )
        )
    # Reserve room for SQLite indexes/row headers, not just raw vector bytes.
    reservation = sum(len(row[3]) + 512 for row in rows)
    with db:
        db.execute("BEGIN IMMEDIATE")
        occupied = db.execute(
            "SELECT COALESCE(sum(length(data)+512),0) FROM vectors"
        ).fetchone()[0]
        while occupied + reservation > maximum_bytes // 2:
            oldest = db.execute(
                "SELECT model_key,content_sha,length(data)+512 AS bytes FROM vectors ORDER BY created LIMIT 1"
            ).fetchone()
            if oldest is None:
                raise ValueError("Vector build batch exceeds the cache capacity")
            db.execute(
                "DELETE FROM vectors WHERE model_key=? AND content_sha=?",
                (oldest["model_key"], oldest["content_sha"]),
            )
            occupied -= oldest["bytes"]
        db.executemany("INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?,?)", rows)


def _scope_table(cache, selected, excluded):
    cache.execute(
        "CREATE TEMP TABLE scope (artifact_id TEXT PRIMARY KEY,name TEXT,version INTEGER)"
    )
    cache.executemany(
        "INSERT INTO scope VALUES(?,?,?)",
        (
            (key, row[0]["name"], row[0]["version"])
            for key, row in selected.items()
            if key not in excluded
        ),
    )


def _bound(result, max_bytes, fits):
    def accepted():
        return len(_json(result).encode()) <= max_bytes and (
            fits is None or fits(result)
        )

    while not accepted() and result["hits"]:
        result["hits"].pop()
        result["complete"], result["truncated"] = False, True
    if not accepted():
        raise ValueError("Hybrid output budget cannot fit required coverage metadata")
    return result


def _fallback(workbook, query, refs, limit, max_bytes, fits, reason):
    from .context_index import ranked_search

    result = ranked_search(workbook, query, refs, limit=limit, max_bytes=262144)
    result.update(
        {"mode": "lexical", "dense_complete": False, "fallback_reason": reason}
    )
    # Complete refers to the requested hybrid operation, even if lexical matches
    # happen to be exhaustive. Preserve that narrower fact under its own field.
    result["lexical_complete"] = result["complete"]
    result["complete"] = False
    return _bound(result, max_bytes, fits)


def hybrid_search(
    workbook,
    query,
    refs=None,
    *,
    model_dir=None,
    expected_manifest=None,
    limit=8,
    max_bytes=16384,
    fits=None,
    max_embed_chunks=128,
    max_embed_seconds=20,
    max_vector_bytes=128 * 1024**2,
    max_search_vectors=20000,
    _embedder=None,
    _model_spec=None,
):
    """Fuse FTS5 and learned local cosine ranks using reciprocal rank fusion.

    Names/scope and metadata come from one catalog snapshot. The full chunk
    store, including chunks with zero lexical overlap, supplies dense candidates.
    Existing context indexing has its separate 128-MiB/30-s build bounds. A call
    builds at most max_embed_chunks vectors in one time-limited worker, then
    scans at most max_search_vectors vectors / five seconds. Partial coverage is
    explicit; repeated calls build further chunks. Ranked output is not an
    exhaustive semantic answer or a guarantee that each model token was read.

    _embedder/_model_spec are offline contract-test seams, never public MCP args.
    """
    from . import context_index as index

    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
        raise ValueError("Hybrid query must contain 1–500 characters")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("Hybrid limit must be 1–64")
    if type(max_bytes) is not int or not 1 <= max_bytes <= 262144:
        raise ValueError("Hybrid max_bytes must be 1–262144")
    if type(max_embed_chunks) is not int or not 0 <= max_embed_chunks <= 512:
        raise ValueError("Embedding build count must be 0–512")
    if type(max_embed_seconds) not in (int, float) or not 0 < max_embed_seconds <= 120:
        raise ValueError(
            "Embedding time budget must be greater than zero and at most 120 seconds"
        )
    if type(max_vector_bytes) is not int or not 1024**2 <= max_vector_bytes <= 1024**3:
        raise ValueError("Vector cache disk budget must be 1 MiB–1 GiB")
    if type(max_search_vectors) is not int or not 1 <= max_search_vectors <= 100000:
        raise ValueError("Vector scan cap must be 1–100000")
    if _model_spec is not None:
        if _embedder is None:
            raise ValueError("A test model spec needs its explicit test embedder")
        spec = _model_spec
    else:
        if model_dir is None:
            return _fallback(
                workbook, query, refs, limit, max_bytes, fits, "model_not_configured"
            )
        try:
            spec = model_spec(model_dir, expected_manifest)
        except importlib.metadata.PackageNotFoundError:
            return _fallback(
                workbook,
                query,
                refs,
                limit,
                max_bytes,
                fits,
                "semantic_dependency_missing",
            )
        except (OSError, ValueError, TypeError, KeyError):
            return _fallback(
                workbook,
                query,
                refs,
                limit,
                max_bytes,
                fits,
                "local_model_invalid_or_missing",
            )
    embed = _embed if _embedder is None else _embedder
    try:
        vector_context = _vectors(workbook, max_vector_bytes)
        vector_db, vector_path = vector_context.__enter__()
    except (OSError, sqlite3.Error, ValueError):
        return _fallback(
            workbook, query, refs, limit, max_bytes, fits, "vector_cache_unavailable"
        )
    try:
        with workbook.connect() as catalog:
            catalog.execute("BEGIN")
            with index._cache(workbook) as cache:
                selected = index._scope(workbook, catalog, refs)
                built, indexed_bytes, pending = index._prepare(
                    cache, catalog, workbook, selected, index.MAX_INDEX_BYTES
                )
                excluded = {item["artifact_id"] for item in pending}
                _scope_table(cache, selected, excluded)
                cache.execute("ATTACH DATABASE ? AS semantic", (str(vector_path),))
                deadline = time.monotonic() + 5
                cache.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
                try:
                    lexical_rows = cache.execute(
                        """
                        SELECT c.*,v.stamp,bm25(chunk_search) AS score FROM chunk_search
                        JOIN chunks c ON c.id=chunk_search.rowid
                        JOIN versions v ON v.artifact_id=c.artifact_id
                        JOIN scope s ON s.artifact_id=c.artifact_id
                        WHERE chunk_search MATCH ? ORDER BY score,s.name,s.version,c.start_byte LIMIT 65
                    """,
                        (index._query(query),),
                    ).fetchall()
                    total_chunks = cache.execute(
                        "SELECT count(*) FROM chunks c JOIN scope s ON s.artifact_id=c.artifact_id"
                    ).fetchone()[0]
                    needed = cache.execute(
                        """
                        SELECT c.*,v.stamp,0.0 AS score FROM chunks c
                        JOIN versions v ON v.artifact_id=c.artifact_id
                        JOIN scope s ON s.artifact_id=c.artifact_id
                        WHERE NOT EXISTS (SELECT 1 FROM semantic.vectors e WHERE e.model_key=? AND e.content_sha=c.digest)
                        GROUP BY c.digest ORDER BY s.name,s.version,c.start_byte LIMIT ?
                    """,
                        (spec["key"], max_embed_chunks),
                    ).fetchall()
                    lexical = [
                        index._hit(workbook, selected, row) for row in lexical_rows[:64]
                    ]
                    documents = []
                    for row in needed:
                        exact = index._hit(workbook, selected, row)
                        if (
                            hashlib.sha256(exact["excerpt"].encode()).hexdigest()
                            != row["digest"]
                        ):
                            raise ValueError(
                                "Indexed embedding source differs from its content hash"
                            )
                        documents.append(
                            {"digest": row["digest"], "body": exact["excerpt"]}
                        )
                finally:
                    cache.set_progress_handler(None, 0)
            clean_vector_cache(workbook)
            fallback_reason, embeddings = None, None
            try:
                embeddings = embed(
                    spec, query, [item["body"] for item in documents], max_embed_seconds
                )
                query_vector = _normalized(embeddings["query"], spec["dimension"])
                _store_vectors(
                    vector_db,
                    spec,
                    documents,
                    embeddings["documents"],
                    max_vector_bytes,
                )
            except TimeoutError:
                fallback_reason = "embedding_time_budget"
            except (OSError, ValueError, KeyError, sqlite3.Error):
                fallback_reason = "embedding_or_cache_failed"
            dense, corrupt, scanned, scan_complete = [], set(), 0, True
            if fallback_reason is None:
                with index._cache(workbook) as cache:
                    _scope_table(cache, selected, excluded)
                    cache.execute("ATTACH DATABASE ? AS semantic", (str(vector_path),))
                    deadline = time.monotonic() + 5
                    rows = cache.execute(
                        """
                        SELECT c.*,v.stamp,0.0 AS score,e.dimension AS vector_dimension,e.data AS vector_data,
                        e.checksum AS vector_checksum,s.name,s.version FROM chunks c
                        JOIN versions v ON v.artifact_id=c.artifact_id
                        JOIN scope s ON s.artifact_id=c.artifact_id
                        JOIN semantic.vectors e ON e.content_sha=c.digest AND e.model_key=?
                        ORDER BY s.name,s.version,c.start_byte LIMIT ?
                    """,
                        (spec["key"], max_search_vectors + 1),
                    )
                    for row in rows:
                        if scanned >= max_search_vectors or time.monotonic() > deadline:
                            scan_complete = False
                            break
                        try:
                            vector = _unpack(row, spec["dimension"])
                        except ValueError:
                            corrupt.add(row["digest"])
                            continue
                        scanned += 1
                        score = sum(
                            left * right for left, right in zip(query_vector, vector)
                        )
                        dense.append(
                            (
                                score,
                                (row["name"], row["version"], row["start_byte"]),
                                dict(row),
                            )
                        )
                        dense.sort(key=lambda item: (-item[0], item[1]))
                        del dense[64:]
                    dense_hits = [
                        (score, index._hit(workbook, selected, row))
                        for score, _, row in dense
                    ]
                if corrupt:
                    with vector_db:
                        vector_db.executemany(
                            "DELETE FROM vectors WHERE model_key=? AND content_sha=?",
                            ((spec["key"], digest) for digest in corrupt),
                        )
            else:
                dense_hits = []
            fused = {}
            for rank, hit in enumerate(lexical, 1):
                fused[hit["chunk_id"]] = dict(hit) | {
                    "score": 1 / (RRF_K + rank),
                    "lexical_rank": rank,
                }
            for rank, (cosine, hit) in enumerate(dense_hits, 1):
                value = fused.setdefault(hit["chunk_id"], dict(hit) | {"score": 0.0})
                value["score"] += 1 / (RRF_K + rank)
                value.update({"dense_rank": rank, "cosine": cosine})
            ranked = sorted(
                fused.values(),
                key=lambda hit: (
                    -hit["score"],
                    hit["name"],
                    hit["version"],
                    hit["start_byte"],
                ),
            )
            dense_complete = (
                not pending
                and scan_complete
                and scanned == total_chunks
                and not corrupt
                and fallback_reason is None
            )
            truncated = len(ranked) > limit or scanned > 64 or len(lexical_rows) > 64
            result = {
                "hits": ranked[:limit],
                "mode": "hybrid" if dense_hits else "lexical",
                "index_complete": not pending,
                "dense_complete": dense_complete,
                "complete": dense_complete and not truncated,
                "truncated": truncated,
                "indexed_versions": built,
                "indexed_bytes": indexed_bytes,
                "pending_versions": len(pending),
                "pending": pending[:16],
                "dense_coverage": {
                    "indexed_chunks": total_chunks,
                    "scored_chunks": scanned,
                    "pending_chunks": max(0, total_chunks - scanned),
                    "built_unique_chunks": len(documents)
                    if fallback_reason is None
                    else 0,
                    "scan_complete": scan_complete,
                },
                "embedding_model": {
                    "name": spec["model"],
                    "revision": spec.get("manifest_sha256", spec["key"]),
                    "dimension": spec["dimension"],
                    "max_input_tokens": 512,
                    "chunk_inputs_may_truncate": True,
                },
                "fusion": {
                    "method": "reciprocal_rank",
                    "k": RRF_K,
                    "candidate_limit_per_ranker": 64,
                },
            }
            if fallback_reason:
                result["fallback_reason"] = fallback_reason
            elif not dense_hits:
                result["fallback_reason"] = "no_dense_candidates"
            if corrupt:
                result["invalidated_vectors"] = len(corrupt)
            return _bound(result, max_bytes, fits)
    finally:
        vector_context.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provision", type=Path)
    parser.add_argument("--embed-worker", nargs=2, metavar=("INPUT", "OUTPUT"))
    arguments = parser.parse_args()
    if arguments.provision is not None:
        print(_json(provision_model(arguments.provision)))
    elif arguments.embed_worker:
        source, destination = arguments.embed_worker
        _worker(json.loads(Path(source).read_text()), destination)
    else:
        parser.error("Choose explicit --provision or the internal worker entry point")


if __name__ == "__main__":
    main()
