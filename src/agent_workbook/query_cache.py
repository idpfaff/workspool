"""Opt-in exact-query reuse with pinned inputs and bounded materialization.

The default executor uses a separate, time-limited DuckDB process. SQLite leases
coordinate processes; a fencing token prevents an expired owner from publishing.
Only conservatively classified deterministic queries are reusable. Every lookup
revalidates catalog freshness and the identity of immutable source files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

import duckdb

CACHE_VERSION = 2
MAX_PAYLOAD = 65536
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
INPUT = re.compile(r"input[1-8]\Z", re.IGNORECASE)
AGGREGATES = {
    "count",
    "count_star",
    "sum",
    "min",
    "max",
    "bool_and",
    "bool_or",
    "bit_and",
    "bit_or",
}
PURE_FUNCTIONS = AGGREGATES | {
    "+",
    "-",
    "*",
    "/",
    "//",
    "%",
    "abs",
    "coalesce",
    "ifnull",
    "nullif",
    "lower",
    "upper",
    "length",
    "char_length",
    "trim",
    "ltrim",
    "rtrim",
    "substring",
    "substr",
    "concat",
    "round",
    "floor",
    "ceil",
    "ceiling",
}
SESSION_COLUMNS = {
    "current_date",
    "current_time",
    "current_timestamp",
    "localtime",
    "localtimestamp",
    "current_user",
    "session_user",
    "system_user",
    "user",
    "current_catalog",
    "current_schema",
}


class CacheError(RuntimeError):
    pass


class CacheLimitError(CacheError):
    pass


class CacheTimeout(TimeoutError):
    pass


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CacheTimeout("Query-cache deadline exceeded")
    return remaining


def _hash_file(path, deadline):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _remaining(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _parameter(value):
    if value is None:
        return ["null", None]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        return ["float", value.hex()]
    if type(value) is str:
        return ["str", value]
    raise TypeError("Cache parameters must be null, bool, int, float, or str")


def _unparameter(value):
    kind, data = value
    return {
        "null": lambda: None,
        "bool": lambda: data,
        "int": lambda: int(data),
        "float": lambda: float.fromhex(data),
        "str": lambda: data,
    }[kind]()


@dataclass(frozen=True)
class CacheInput:
    artifact_id: str
    digest: str
    path: Path | str
    media_type: str
    csv_types: dict[str, str] | None = None

    @classmethod
    def from_table(cls, item, path):
        return cls(item["artifact_id"], Path(path).name, Path(path), item["media_type"])


@dataclass(frozen=True)
class CacheSpec:
    sql: str
    inputs: tuple[CacheInput, ...] | list[CacheInput]
    parameters: tuple | list = ()
    settings: dict = field(default_factory=dict)
    preview_rows: int = 5
    materialize: bool = True
    max_rows: int = 1_000_000
    allow_historical: bool = False


@dataclass
class CacheResult:
    payload: dict
    path: Path | None
    key: str
    hit: bool
    reason: str
    materialized_sha256: str | None = None
    row_count: int | None = None
    materialized_type_conversions: list[dict] = field(default_factory=list)

    def to_dict(self):
        result = asdict(self)
        result["path"] = str(self.path) if self.path is not None else None
        return result


@dataclass
class Computed:
    """Trusted callback result; callbacks must honor Execution's bounds themselves."""

    payload: dict
    path: Path | None = None
    row_count: int | None = None
    reusable: bool = True
    reason: str = "computed"
    type_conversions: list[dict] = field(default_factory=list)


def _clean_ast(value):
    if isinstance(value, list):
        return [_clean_ast(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _clean_ast(item)
            for key, item in value.items()
            if key not in {"query_location", "alias"}
        }
    return value


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def classify_sql(sql):
    """Return (reusable, reason, parsed tree). Unsupported syntax is never guessed."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 6000:
        raise ValueError("Provide SQL of 1–6000 characters")
    try:
        with duckdb.connect(
            config={
                "autoload_known_extensions": "false",
                "autoinstall_known_extensions": "false",
                "threads": "1",
            }
        ) as db:
            parsed = json.loads(
                db.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
            )
    except (duckdb.Error, ValueError):
        return False, "unsupported_parser", None
    if parsed.get("error") or len(parsed.get("statements", [])) != 1:
        return False, "unsupported_statement", parsed
    node = parsed["statements"][0]["node"]
    if node.get("type") != "SELECT_NODE" or node.get("cte_map", {}).get("map"):
        return False, "unsupported_select_shape", parsed
    source = node.get("from_table", {})
    if source.get("type") not in {"BASE_TABLE", "EMPTY"}:
        return False, "unsupported_source", parsed
    if source.get("type") == "BASE_TABLE" and (
        not INPUT.fullmatch(source.get("table_name", ""))
        or source.get("schema_name")
        or source.get("catalog_name")
    ):
        return False, "non_input_source", parsed
    allowed_classes = {
        "FUNCTION",
        "COLUMN_REF",
        "CONSTANT",
        "PARAMETER",
        "COMPARISON",
        "CONJUNCTION",
        "OPERATOR",
        "CASE",
        "CAST",
        "STAR",
        "BETWEEN",
    }
    for item in _walk(node):
        if item.get("sample") is not None or item.get("at_clause") is not None:
            return False, "sample_or_time_travel", parsed
        if "class" in item and item["class"] not in allowed_classes:
            return False, "unsupported_expression", parsed
        if item.get("class") == "FUNCTION":
            name = item.get("function_name", "").lower()
            if (
                name not in PURE_FUNCTIONS
                or item.get("schema")
                or item.get("catalog")
                or item.get("export_state")
            ):
                return False, "unknown_or_volatile_function", parsed
        if item.get("class") == "COLUMN_REF" and any(
            name.lower() in SESSION_COLUMNS for name in item.get("column_names", [])
        ):
            return False, "session_value", parsed
        if any(
            isinstance(item.get(key), str) and item[key] in {"TIMESTAMP_TZ", "TIME_TZ"}
            for key in ("type", "id")
        ):
            return False, "timezone_dependent", parsed
        if item.get("class") == "CAST":
            target = item.get("cast_type", {}).get("id", "")
            if isinstance(target, str) and any(
                part in target for part in ("DATE", "TIME", "INTERVAL")
            ):
                return False, "temporal_cast", parsed
    if (
        node.get("qualify")
        or node.get("sample")
        or node.get("aggregate_handling") != "STANDARD_HANDLING"
    ):
        return False, "unsupported_modifier", parsed
    modifiers = node.get("modifiers", [])
    if any(
        item.get("type") == "DISTINCT_MODIFIER" and item.get("distinct_on_targets")
        for item in modifiers
    ):
        return False, "distinct_on_representative", parsed
    if any(
        item.get("type")
        not in {"ORDER_MODIFIER", "LIMIT_MODIFIER", "DISTINCT_MODIFIER"}
        for item in modifiers
    ):
        return False, "unsupported_modifier", parsed
    selected = node.get("select_list", [])
    has_aggregate = any(
        item.get("function_name", "").lower() in AGGREGATES
        for expression in selected
        for item in _walk(expression)
    )
    if source.get("type") == "EMPTY" or (
        has_aggregate and not node.get("group_expressions")
    ):
        return True, "scalar_result", parsed
    orders = [
        entry["expression"]
        for item in modifiers
        if item.get("type") == "ORDER_MODIFIER"
        for entry in item.get("orders", [])
    ]
    if any(item.get("class") == "STAR" and item.get("columns") for item in orders):
        return True, "ordered_all", parsed
    order_forms = {_json(_clean_ast(item)) for item in orders}
    aliases = {item.get("alias"): item for item in selected if item.get("alias")}
    for item in orders:
        names = item.get("column_names", [])
        if (
            item.get("class") == "COLUMN_REF"
            and len(names) == 1
            and names[0] in aliases
        ):
            order_forms.add(_json(_clean_ast(aliases[names[0]])))
        if item.get("class") == "CONSTANT":
            value = item.get("value", {}).get("value")
            if type(value) is int and 1 <= value <= len(selected):
                order_forms.add(_json(_clean_ast(selected[value - 1])))
    required = node.get("group_expressions") or selected
    if required and all(
        item.get("class") in {"CONSTANT", "PARAMETER"}
        or _json(_clean_ast(item)) in order_forms
        for item in required
    ):
        return True, "ordered_complete", parsed
    return False, "unproven_result_order", parsed


@dataclass
class Execution:
    spec: CacheSpec
    directory: Path
    deadline: float
    max_bytes: int

    @property
    def destination(self):
        return self.directory / "result.parquet"

    def materialize(self):
        config = {
            "sql": self.spec.sql,
            "parameters": [_parameter(value) for value in self.spec.parameters],
            "inputs": [
                {**asdict(item), "path": str(Path(item.path).resolve())}
                for item in self.spec.inputs
            ],
            "settings": self.spec.settings,
            "preview_rows": self.spec.preview_rows,
            "materialize": self.spec.materialize,
            "max_rows": self.spec.max_rows,
            "max_bytes": self.max_bytes,
            "timeout": _remaining(self.deadline),
            "parent_pid": os.getpid(),
            "output": str(self.destination),
            "report": str(self.directory / "worker.json"),
        }
        path = self.directory / "config.json"
        path.write_text(_json(config))
        with (self.directory / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agent_workbook.query_cache",
                    "--worker",
                    str(path),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            (self.directory / "worker.pid").write_text(str(process.pid))
            try:
                process.wait(timeout=_remaining(self.deadline))
            except subprocess.TimeoutExpired as error:
                _kill_worker(process)
                raise CacheTimeout("Bounded query worker deadline exceeded") from error
            except BaseException:
                _kill_worker(process)
                raise
            finally:
                if process.poll() is None:
                    _kill_worker(process)
        report = Path(config["report"])
        if not report.exists():
            if process.returncode == 124:
                raise CacheTimeout("Bounded query worker deadline exceeded")
            if process.returncode == -getattr(signal, "SIGXFSZ", 25):
                raise CacheLimitError("Materialized result exceeds the byte limit")
            raise CacheError(
                f"Bounded query worker exited without a result (status {process.returncode})"
            )
        value = json.loads(report.read_text())
        if value.get("error"):
            error = (
                CacheLimitError
                if value.get("kind") == "limit"
                else CacheTimeout
                if value.get("kind") == "timeout"
                else CacheError
            )
            raise error(value["error"])
        if process.returncode:
            raise CacheError(f"Query worker failed with status {process.returncode}")
        return Computed(
            value["payload"],
            self.destination if self.spec.materialize else None,
            value.get("row_count"),
            value["reusable"],
            value["reason"],
            value.get("type_conversions", []),
        )


def _kill_worker(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
 key TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT, lease REAL,
 payload TEXT, payload_digest TEXT, object TEXT, digest TEXT, size INTEGER NOT NULL,
 row_count INTEGER, accessed REAL NOT NULL
 ,conversions TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS integrity (fingerprint TEXT PRIMARY KEY, digest TEXT NOT NULL, accessed REAL NOT NULL);
CREATE INDEX IF NOT EXISTS entries_access ON entries(state, accessed);
"""


class QueryCache:
    def __init__(
        self,
        root,
        *,
        max_bytes=256 * 1024**2,
        entry_bytes=64 * 1024**2,
        max_entries=128,
        lease_seconds=30,
    ):
        if (
            not (65536 <= entry_bytes and entry_bytes + MAX_PAYLOAD <= max_bytes)
            or max_entries < 1
            or not 0.1 <= lease_seconds <= 3600
        ):
            raise ValueError("Invalid cache limits")
        self.root = Path(root).resolve()
        self.directory = self.root / ".query-cache"
        self.objects = self.directory / "objects"
        self.staging = self.directory / "staging"
        self.database = self.directory / "cache.sqlite3"
        self.max_bytes, self.entry_bytes = max_bytes, entry_bytes
        self.max_entries, self.lease_seconds = max_entries, lease_seconds
        self.objects.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.staging.mkdir(mode=0o700, exist_ok=True)
        deadline = time.monotonic() + 5
        while True:
            try:
                self._initialize()
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def _initialize(self):
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            if "conversions" not in {
                row[1] for row in db.execute("PRAGMA table_info(entries)")
            }:
                db.execute(
                    "ALTER TABLE entries ADD COLUMN conversions TEXT NOT NULL DEFAULT '[]'"
                )
            referenced = {
                row[0]
                for row in db.execute(
                    "SELECT object FROM entries WHERE object IS NOT NULL"
                )
            }
            owners = {
                row[0]
                for row in db.execute(
                    "SELECT owner FROM entries WHERE owner IS NOT NULL"
                )
            }
            for path in self.objects.glob("*.parquet"):
                if path.name not in referenced and path.stem not in owners:
                    path.unlink(missing_ok=True)
            for path in self.staging.iterdir():
                if path.is_dir() and path.name not in owners:
                    shutil.rmtree(path, ignore_errors=True)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=0.25)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_size_limit=1048576")
        db.execute("PRAGMA wal_autocheckpoint=64")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _inputs(self, spec, deadline):
        from .store import Workbook

        if not 1 <= len(spec.inputs) <= 8 or len(
            {item.artifact_id for item in spec.inputs}
        ) != len(spec.inputs):
            raise ValueError("Provide 1–8 distinct immutable inputs")
        fingerprints = []
        with sqlite3.connect(self.root / "workbook.sqlite3", timeout=0.25) as catalog:
            catalog.row_factory = sqlite3.Row
            catalog.execute("BEGIN")
            for item in spec.inputs:
                _remaining(deadline)
                row = catalog.execute(
                    "SELECT * FROM artifacts WHERE artifact_id=?", (item.artifact_id,)
                ).fetchone()
                if (
                    row is None
                    or row["digest"] != item.digest
                    or row["media_type"] != item.media_type
                ):
                    raise ValueError("Cache input does not match the workbook catalog")
                if item.media_type not in {
                    "text/csv",
                    "application/vnd.apache.parquet",
                }:
                    raise ValueError("Cache input must be CSV or Parquet")
                if item.csv_types is not None and (
                    item.media_type != "text/csv"
                    or not isinstance(item.csv_types, dict)
                    or len(item.csv_types) > 256
                    or any(
                        not isinstance(name, str)
                        or not isinstance(kind, str)
                        or not 1 <= len(name) <= 256
                        or not 1 <= len(kind) <= 128
                        for name, kind in item.csv_types.items()
                    )
                ):
                    raise ValueError("Invalid CSV type map")
                status = Workbook._status(catalog, item.artifact_id)
                if not spec.allow_historical and (
                    not status["is_latest"] or status["stale_inputs"]
                ):
                    raise ValueError("Cache input is historical or stale")
                original_path = Path(item.path).absolute()
                if not stat.S_ISREG(original_path.lstat().st_mode):
                    raise ValueError("Cache input must be a regular immutable file")
                path = original_path.resolve()
                if (
                    not DIGEST.fullmatch(item.digest)
                    or path != self.root / "blobs" / item.digest
                ):
                    raise ValueError(
                        "Cache input must reference its immutable workbook blob"
                    )
                fingerprint = _fingerprint(path)
                identity = _json([item.digest, str(path), fingerprint])
                with self._db() as db:
                    checked = db.execute(
                        "SELECT digest FROM integrity WHERE fingerprint=?", (identity,)
                    ).fetchone()
                if checked is None:
                    if (
                        _hash_file(path, deadline) != item.digest
                        or _fingerprint(path) != fingerprint
                    ):
                        raise ValueError(
                            "Immutable input digest mismatch or concurrent mutation"
                        )
                    with self._db() as db:
                        db.execute(
                            "INSERT OR REPLACE INTO integrity VALUES(?,?,?)",
                            (identity, item.digest, time.time()),
                        )
                        db.execute(
                            "DELETE FROM integrity WHERE fingerprint IN (SELECT fingerprint FROM integrity ORDER BY accessed DESC LIMIT -1 OFFSET ?)",
                            (self.max_entries * 8,),
                        )
                fingerprints.append(fingerprint)
        return fingerprints

    def _key(self, spec, fingerprints):
        value = {
            "version": CACHE_VERSION,
            "duckdb": duckdb.__version__,
            "python": [sys.version_info.major, sys.version_info.minor],
            "sql": spec.sql,
            "parameters": [_parameter(value) for value in spec.parameters],
            "inputs": [
                {
                    "id": item.artifact_id,
                    "digest": item.digest,
                    "type": item.media_type,
                    "stat": stat,
                    "csv_types": item.csv_types,
                }
                for item, stat in zip(spec.inputs, fingerprints, strict=True)
            ],
            "settings": spec.settings,
            "preview_rows": spec.preview_rows,
            "max_rows": spec.max_rows,
            "materialize": spec.materialize,
            "allow_historical": spec.allow_historical,
            "environment": {
                name: os.environ.get(name)
                for name in ("TZ", "LC_ALL", "LC_COLLATE", "LC_CTYPE", "LANG")
            },
            "timezone": list(time.tzname),
        }
        return _digest(_json(value).encode())

    def _remove(self, db, row):
        db.execute("DELETE FROM entries WHERE key=?", (row["key"],))
        if row["object"]:
            (self.objects / row["object"]).unlink(missing_ok=True)
        if row["owner"]:
            shutil.rmtree(self.staging / row["owner"], ignore_errors=True)
            (self.objects / (row["owner"] + ".parquet")).unlink(missing_ok=True)

    def _claim(self, key, owner, force):
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for expired in db.execute(
                "SELECT * FROM entries WHERE state='busy' AND lease<?", (now,)
            ).fetchall():
                self._remove(db, expired)
            existing = db.execute(
                "SELECT * FROM entries WHERE key=?", (key,)
            ).fetchone()
            if existing and existing["state"] == "ready" and not force:
                return "hit", dict(existing)
            if existing and existing["state"] == "busy":
                return "wait", None
            if existing:
                self._remove(db, existing)
            reserve = self.entry_bytes + MAX_PAYLOAD
            while True:
                count, occupied = db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM entries"
                ).fetchone()
                if count < self.max_entries and occupied + reserve <= self.max_bytes:
                    break
                oldest = db.execute(
                    "SELECT * FROM entries WHERE state='ready' ORDER BY accessed LIMIT 1"
                ).fetchone()
                if oldest is None:
                    return "wait", None
                self._remove(db, oldest)
            db.execute(
                "INSERT INTO entries(key,state,owner,lease,size,accessed) VALUES(?,'busy',?,?,?,?)",
                (key, owner, now + self.lease_seconds, reserve, now),
            )
            return "owner", None

    def _heartbeat(self, key, owner, stop):
        while not stop.wait(self.lease_seconds / 3):
            try:
                with self._db() as db:
                    changed = db.execute(
                        "UPDATE entries SET lease=? WHERE key=? AND owner=? AND state='busy'",
                        (time.time() + self.lease_seconds, key, owner),
                    ).rowcount
                    if not changed:
                        return
            except sqlite3.OperationalError:
                continue

    def _copy_hit(self, key, destination, deadline):
        stream = None
        try:
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM entries WHERE key=? AND state='ready'", (key,)
                ).fetchone()
                if row is None:
                    return None
                if _digest(row["payload"].encode()) != row["payload_digest"]:
                    self._remove(db, row)
                    return None
                if row["object"]:
                    try:
                        stream = (self.objects / row["object"]).open("rb")
                    except FileNotFoundError:
                        self._remove(db, row)
                        return None
                db.execute(
                    "UPDATE entries SET accessed=? WHERE key=?", (time.time(), key)
                )
                data = dict(row)
            if stream is not None:
                actual = self._copy(stream, destination, deadline)
                if actual != data["digest"]:
                    destination.unlink(missing_ok=True)
                    with self._db() as db:
                        current = db.execute(
                            "SELECT * FROM entries WHERE key=? AND object=?",
                            (key, data["object"]),
                        ).fetchone()
                        if current:
                            self._remove(db, current)
                    return None
            return CacheResult(
                json.loads(data["payload"]),
                destination if stream else None,
                key,
                True,
                "hit",
                data["digest"],
                data["row_count"],
                json.loads(data["conversions"]),
            )
        finally:
            if stream is not None:
                stream.close()

    def _copy(self, stream, destination, deadline):
        digest, size = hashlib.sha256(), 0
        created = False
        try:
            with destination.open("xb") as target:
                created = True
                while chunk := stream.read(1024 * 1024):
                    _remaining(deadline)
                    size += len(chunk)
                    if size > self.entry_bytes:
                        raise CacheLimitError("Cached file exceeds the entry limit")
                    target.write(chunk)
                    digest.update(chunk)
            return digest.hexdigest()
        except BaseException:
            if created:
                destination.unlink(missing_ok=True)
            raise

    def execute(
        self,
        spec: CacheSpec,
        compute: Callable[[Execution], Computed] | None = None,
        *,
        destination=None,
        force=False,
        timeout=None,
    ):
        if (
            not 1 <= spec.preview_rows <= 1000
            or not 1 <= spec.max_rows <= 100_000_000
            or len(spec.parameters) > 30
        ):
            raise ValueError("Invalid query-cache bounds")
        if spec.materialize and destination is None:
            raise ValueError("Materialization requires a caller-owned destination")
        destination = Path(destination) if destination is not None else None
        if destination is not None and destination.exists():
            raise FileExistsError(destination)
        duration = (
            timeout
            if timeout is not None
            else float(spec.settings.get("query_timeout", 15)) + 5
        )
        if not 0 < duration <= 3600:
            raise ValueError("Cache timeout must be between 0 and 3600 seconds")
        deadline = time.monotonic() + duration
        fingerprints = self._inputs(spec, deadline)
        reusable, reason, _ = classify_sql(spec.sql)
        if any(
            type(value) is float and not math.isfinite(value)
            for value in spec.parameters
        ):
            reusable, reason = False, "nonfinite_parameter"
        key = self._key(spec, fingerprints)
        if not reusable:
            key = _digest((key + uuid4().hex).encode())
        owner = uuid4().hex
        while True:
            _remaining(deadline)
            try:
                action, _ = self._claim(key, owner, force or not reusable)
                if action == "hit":
                    hit = self._copy_hit(key, destination, deadline)
                    if hit is not None:
                        return hit
                    continue
                if action == "owner":
                    break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
            time.sleep(min(0.05, _remaining(deadline)))
        directory = self.staging / owner
        directory.mkdir(mode=0o700)
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(key, owner, stop), daemon=True
        )
        heartbeat.start()
        try:
            execution = Execution(spec, directory, deadline, self.entry_bytes)
            result = (
                compute(execution) if compute is not None else execution.materialize()
            )
            _remaining(deadline)
            if self._inputs(spec, deadline) != fingerprints:
                raise ValueError("Input changed while the query was running")
            payload = _json(result.payload)
            if len(payload.encode()) > MAX_PAYLOAD:
                raise CacheLimitError("Preview payload exceeds the cache limit")
            digest, size = None, len(payload.encode())
            if spec.materialize:
                if (
                    result.path is None
                    or result.row_count is None
                    or result.row_count > spec.max_rows
                ):
                    raise CacheLimitError("Incomplete or oversized materialization")
                result.path = Path(result.path)
                if result.path.stat().st_size > self.entry_bytes:
                    raise CacheLimitError("Materialized result exceeds the byte limit")
                with result.path.open("rb") as stream:
                    digest = self._copy(stream, destination, deadline)
                size += result.path.stat().st_size
            reason = (
                "forced" if force and reusable else "computed" if reusable else reason
            )
            reusable = reusable and result.reusable
            if not result.reusable:
                reason = result.reason
            published = False
            if reusable:
                filename = owner + ".parquet" if spec.materialize else None
                with self._db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    current = db.execute(
                        "SELECT owner FROM entries WHERE key=? AND state='busy'", (key,)
                    ).fetchone()
                    if current and current["owner"] == owner:
                        if filename:
                            os.replace(result.path, self.objects / filename)
                        db.execute(
                            "UPDATE entries SET state='ready',owner=NULL,lease=NULL,payload=?,payload_digest=?,object=?,digest=?,size=?,row_count=?,accessed=?,conversions=? WHERE key=? AND owner=?",
                            (
                                payload,
                                _digest(payload.encode()),
                                filename,
                                digest,
                                size,
                                result.row_count,
                                time.time(),
                                _json(result.type_conversions),
                                key,
                                owner,
                            ),
                        )
                        published = True
            if not reusable or not published:
                key = _digest((key + owner).encode())
                if reusable:
                    reason = "lease_lost"
            return CacheResult(
                result.payload,
                destination if spec.materialize else None,
                key,
                False,
                reason,
                digest,
                result.row_count,
                result.type_conversions,
            )
        finally:
            stop.set()
            heartbeat.join(timeout=1)
            with self._db() as db:
                db.execute(
                    "DELETE FROM entries WHERE owner=? AND state='busy'", (owner,)
                )
            shutil.rmtree(directory, ignore_errors=True)


def _worker(config):
    import resource

    from .materialize import lossless_parquet_projection
    from .query import _preview, literal

    hard_deadline = time.monotonic() + config["timeout"]

    def watchdog():
        while True:
            if (
                os.getppid() != config["parent_pid"]
                or time.monotonic() >= hard_deadline
            ):
                os._exit(124)
            time.sleep(0.05)

    threading.Thread(target=watchdog, daemon=True).start()

    # OS-enforced per-file ceiling also bounds failed writes, not just publication.
    resource.setrlimit(
        resource.RLIMIT_FSIZE, (config["max_bytes"], config["max_bytes"])
    )
    settings = config["settings"]
    timeout = min(float(settings.get("query_timeout", 15)), config["timeout"])
    with duckdb.connect(
        config={
            "threads": str(settings.get("query_threads", 2)),
            "memory_limit": settings.get("query_memory", "1GB"),
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
            "max_temp_directory_size": "0B",
        }
    ) as db:
        timer = threading.Timer(timeout, db.interrupt)
        timer.start()
        try:
            allowed = [item["path"] for item in config["inputs"]] + [config["output"]]
            db.execute("SET allowed_paths=[" + ",".join(map(literal, allowed)) + "]")
            db.execute("SET enable_external_access=false")
            db.execute("SET lock_configuration=true")
            column_types = {}
            temporal_csv = False
            for index, item in enumerate(config["inputs"], 1):
                reader = (
                    "read_parquet"
                    if item["media_type"] == "application/vnd.apache.parquet"
                    else "read_csv_auto"
                )
                options = ""
                if item.get("csv_types"):
                    options = (
                        ",types={"
                        + ",".join(
                            literal(key) + ":" + literal(value)
                            for key, value in item["csv_types"].items()
                        )
                        + "}"
                    )
                db.execute(
                    f"CREATE VIEW input{index} AS SELECT * FROM {reader}({literal(item['path'])}{options})"
                )
                schema = db.sql(f"SELECT * FROM input{index} LIMIT 0")
                temporal_csv |= item["media_type"] == "text/csv" and any(
                    any(part in str(kind) for part in ("DATE", "TIME", "INTERVAL"))
                    for kind in schema.types
                )
                for name, kind in zip(schema.columns, schema.types, strict=True):
                    column_types[name.lower()] = str(kind)
            statements = db.extract_statements(config["sql"])
            if (
                len(statements) != 1
                or statements[0].type != duckdb.StatementType.SELECT
            ):
                raise ValueError("Only one SELECT is allowed")
            safe, reason, parsed = classify_sql(config["sql"])
            if temporal_csv:
                safe, reason = False, "temporal_csv_conversion"
            nodes = list(_walk(parsed)) if parsed else []

            def exact(expression):
                kind = expression.get("class")
                if kind == "COLUMN_REF":
                    sql_type = column_types.get(
                        expression.get("column_names", [""])[-1].lower(), ""
                    )
                    return (
                        "INT" in sql_type
                        or sql_type.startswith("DECIMAL")
                        or sql_type == "BOOLEAN"
                    )
                if kind == "CAST":
                    target = expression.get("cast_type", {}).get("id", "")
                    return "INT" in target or target in {"DECIMAL", "BOOLEAN"}
                if kind == "CONSTANT":
                    target = expression.get("value", {}).get("type", {}).get("id", "")
                    return "INT" in target or target in {
                        "DECIMAL",
                        "BOOLEAN",
                        "SQLNULL",
                    }
                if kind == "PARAMETER":
                    index = int(expression["identifier"]) - 1
                    return 0 <= index < len(config["parameters"]) and config[
                        "parameters"
                    ][index][0] in {"int", "bool", "null"}
                if kind == "FUNCTION" and expression.get("function_name") in {
                    "+",
                    "-",
                    "*",
                    "abs",
                    "coalesce",
                    "ifnull",
                    "nullif",
                }:
                    return all(exact(child) for child in expression.get("children", []))
                return False

            if any(
                item.get("function_name") == "sum"
                and not all(exact(child) for child in item.get("children", []))
                for item in nodes
            ):
                safe, reason = False, "floating_aggregate"
            parameters = [_unparameter(value) for value in config["parameters"]]
            relation = db.sql(config["sql"], params=parameters)
            row_count = None
            conversions = []
            if config["materialize"]:
                if len(set(relation.columns)) != len(relation.columns):
                    raise ValueError(
                        "Materialized results require unique output aliases"
                    )
                relation.limit(config["max_rows"] + 1).create(
                    "__workspool_cache_result"
                )
                row_count = db.execute(
                    "SELECT count(*) FROM __workspool_cache_result"
                ).fetchone()[0]
                if row_count > config["max_rows"]:
                    raise CacheLimitError(
                        "Query result exceeds the materialized row limit"
                    )
                payload = _preview(
                    db.execute(
                        "SELECT * FROM __workspool_cache_result LIMIT ?",
                        [config["preview_rows"] + 1],
                    ),
                    config["preview_rows"],
                )
                projected, conversions = lossless_parquet_projection(
                    db.table("__workspool_cache_result")
                )
                projected.write_parquet(config["output"])
            else:
                cursor = relation.limit(config["preview_rows"] + 1)
                payload = _preview(cursor, config["preview_rows"])
            return {
                "payload": payload,
                "row_count": row_count,
                "reusable": safe,
                "reason": reason,
                "type_conversions": conversions,
            }
        finally:
            timer.cancel()
            timer.join()


def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.worker.read_text())
    try:
        value = _worker(config)
    except (CacheError, CacheTimeout, duckdb.Error, ValueError, OSError) as error:
        message = str(error)
        kind = (
            "limit"
            if isinstance(error, CacheLimitError) or "file too large" in message.lower()
            else "timeout"
            if "interrupt" in message.lower()
            else "query"
        )
        value = {"error": message[:2000], "kind": kind}
    Path(config["report"]).write_text(_json(value))


if __name__ == "__main__":
    _main()
