"""Explicitly configured local workers with pinned identities and bounded replies.

Workers are trusted host executables, not sandboxed programs. Tool callers can
select a registry name and scalar parameters; they cannot supply code, commands,
environment variables, paths or worker configuration. No worker runs at import.
"""

import errno
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import duckdb

from .coordination import authorize, current_principal
from .query import _preview, literal

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}\Z")
DECIMAL = re.compile(r"DECIMAL\(([1-9]|[12][0-9]|3[0-8]),([0-9]|[12][0-9]|3[0-8])\)\Z")


def _json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(path, limit=128 * 1024**2):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("Worker identity requires a bounded regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.lstat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("Worker file changed while verifying identity")
    return digest.hexdigest()


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    command: tuple
    executable_target: str
    files: tuple
    timeout_seconds: float
    max_output_bytes: int
    max_input_bytes: int
    max_rows: int
    digest: str

    def verify(self):
        if str(Path(self.command[0]).resolve(strict=True)) != self.executable_target:
            raise ValueError("Configured worker executable link changed")
        for path, expected in self.files:
            if _hash(path) != expected:
                raise ValueError(
                    f"Configured worker {self.name!r} changed; update and reload its manifest"
                )


class WorkerRegistry:
    def __init__(self, specs, path, digest):
        self.specs = MappingProxyType(dict(specs))
        self.path, self.digest = Path(path), digest

    def resolve(self, name):
        if _hash(self.path, 65536) != self.digest:
            raise ValueError("Worker registry changed; reload trusted configuration")
        if name not in self.specs:
            raise ValueError(f"Worker {name!r} is not configured")
        spec = self.specs[name]
        spec.verify()
        return spec


def load_registry(manifest_path):
    """Load a host-owned JSON manifest; never exposed as an MCP tool argument.

    format=1, workers maps names to command:[argv], files:{path:sha256}, optional
    timeout_seconds (0..300), max_output_bytes (1KiB..64MiB), max_rows (1..1M).
    Command executable and every path argument must be explicitly hash-pinned.
    Relative paths are resolved against the manifest directory. No shell is used.
    """
    path = Path(manifest_path).expanduser().resolve(strict=True)
    digest = _hash(path, 65536)
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or set(data) != {"format", "workers"}
        or data["format"] != 1
        or not isinstance(data["workers"], dict)
        or not 1 <= len(data["workers"]) <= 16
    ):
        raise ValueError(
            "Worker manifest requires format=1 and 1–16 registered workers"
        )
    specs = {}
    for name, value in data["workers"].items():
        if (
            not NAME.fullmatch(name)
            or not isinstance(value, dict)
            or set(value)
            - {
                "command",
                "files",
                "timeout_seconds",
                "max_output_bytes",
                "max_rows",
                "max_input_bytes",
            }
        ):
            raise ValueError("Invalid worker manifest entry")
        command, files = value.get("command"), value.get("files")
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 16
            or any(
                not isinstance(arg, str) or not 1 <= len(arg) <= 4096 or "\x00" in arg
                for arg in command
            )
            or "-c" in command
        ):
            raise ValueError(
                "Workers require fixed argv; inline -c code is not allowed"
            )
        if not isinstance(files, dict) or not 1 <= len(files) <= 16:
            raise ValueError(
                "Worker files must pin executable/script/dependency SHA256 values"
            )
        resolved = {}
        for file, expected in files.items():
            if (
                not isinstance(file, str)
                or not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
            ):
                raise ValueError("Worker file hashes must be SHA256 hex strings")
            target = (path.parent / file).resolve(strict=True)
            resolved[str(target)] = expected
        argv = []
        for index, arg in enumerate(command):
            possible = path.parent / arg
            if (
                index == 0
                or not arg.startswith("-")
                and (possible.exists() or os.sep in arg)
            ):
                absolute = str(possible.resolve(strict=True))
                if absolute not in resolved:
                    raise ValueError(
                        "Every executable or file argument must be hash-pinned"
                    )
                argv.append(str(possible.absolute()) if index == 0 else absolute)
            else:
                argv.append(arg)
        if not os.access(argv[0], os.X_OK):
            raise ValueError("Configured worker executable is not executable")
        timeout = value.get("timeout_seconds", 30)
        maximum = value.get("max_output_bytes", 16 * 1024**2)
        input_maximum = value.get("max_input_bytes", 256 * 1024**2)
        rows = value.get("max_rows", 10000)
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 300
            or type(maximum) is not int
            or not 1024 <= maximum <= 64 * 1024**2
            or type(rows) is not int
            or not 1 <= rows <= 1000000
            or type(input_maximum) is not int
            or not 1024 <= input_maximum <= 8 * 1024**3
        ):
            raise ValueError("Invalid worker deadline/output/row bounds")
        identity = {
            "protocol": 1,
            "command": argv,
            "files": sorted(resolved.items()),
            "timeout_seconds": timeout,
            "max_output_bytes": maximum,
            "max_rows": rows,
            "max_input_bytes": input_maximum,
            "executable_target": str(Path(argv[0]).resolve(strict=True)),
        }
        spec = WorkerSpec(
            name,
            tuple(argv),
            identity["executable_target"],
            tuple(sorted(resolved.items())),
            float(timeout),
            maximum,
            input_maximum,
            rows,
            hashlib.sha256(_json(identity).encode()).hexdigest(),
        )
        spec.verify()
        specs[name] = spec
    if _hash(path, 65536) != digest:
        raise ValueError("Worker manifest changed while loading")
    return WorkerRegistry(specs, path, digest)


def _terminate(process):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            # macOS may reject a group kill after its leader has exited. Preserve
            # that completed child's result/error, but never hide a failure to
            # stop a still-running child (or any unrelated permission error).
            if error.errno != errno.EPERM or process.poll() is None:
                raise
    elif process.poll() is None:
        process.kill()
    process.wait()


def _run(spec, request, scratch):
    config = scratch / "worker-config.json"
    config.write_text(
        _json(
            {
                "command": spec.command,
                "executable_target": spec.executable_target,
                "files": spec.files,
                "max_output_bytes": spec.max_output_bytes,
                "supervisor_pid": os.getpid(),
                "deadline": time.monotonic() + spec.timeout_seconds,
            }
        )
    )
    stdout, stderr = scratch / "stdout.json", scratch / "stderr.txt"
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "PYTHONPATH")
        if key in os.environ
    }
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_workbook.task_workers",
                "--execute",
                str(config),
            ],
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            cwd=scratch,
            env=environment,
            start_new_session=os.name == "posix",
        )
        try:
            process.communicate(_json(request).encode(), timeout=spec.timeout_seconds)
            if process.returncode:
                raise ValueError(
                    f"Configured worker exited with status {process.returncode}"
                )
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                "Configured worker exceeded its wall-clock deadline"
            ) from None
        finally:
            _terminate(process)
    if stdout.stat().st_size > min(spec.max_output_bytes, 8 * 1024**2):
        raise ValueError("Worker JSON reply exceeds its output bound")
    try:
        return json.loads(stdout.read_bytes())
    except (ValueError, UnicodeError):
        raise ValueError("Worker must emit exactly one JSON result on stdout") from None


def _typed(value, kind):
    if value is None:
        return None
    if kind == "VARCHAR" and isinstance(value, str) and len(value) <= 4096:
        return value
    if kind == "BOOLEAN" and type(value) is bool:
        return value
    if kind == "BIGINT" and type(value) is int and -(1 << 63) <= value < 1 << 63:
        return value
    if kind == "UBIGINT" and type(value) is int and 0 <= value < 1 << 64:
        return value
    if (
        kind == "DOUBLE"
        and type(value) in (int, float)
        and math.isfinite(value)
        and (type(value) is float or abs(value) <= 2**53)
    ):
        return value
    match = DECIMAL.fullmatch(kind)
    if match and type(value) in (int, str):
        precision, scale = map(int, match.groups())
        with localcontext() as context:
            context.prec = 80
            decimal = Decimal(value)
            if (
                decimal.is_finite()
                and decimal == decimal.quantize(Decimal(1).scaleb(-scale))
                and abs(decimal) < Decimal(10) ** (precision - scale)
            ):
                return decimal
    raise ValueError("Worker cell does not exactly match its declared scalar type")


def _materialize(workbook, spec, value, scratch):
    """Materialize in a bounded child so the output cap applies during writes."""
    timeout = min(spec.timeout_seconds, workbook.settings.query_timeout)
    config = scratch / "materialize-config.json"
    config.write_text(
        _json(
            {
                "value": value,
                "scratch": str(scratch),
                "timeout_seconds": timeout,
                "max_output_bytes": spec.max_output_bytes,
                "max_rows": spec.max_rows,
                "query_threads": workbook.settings.query_threads,
                "query_memory": workbook.settings.query_memory,
                "query_timeout": timeout,
                "supervisor_pid": os.getpid(),
                "deadline": time.monotonic() + timeout,
            }
        )
    )
    stdout, stderr = (
        scratch / "materialize-preview.json",
        scratch / "materialize-stderr.txt",
    )
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_workbook.task_workers",
                "--materialize",
                str(config),
            ],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            cwd=scratch,
            start_new_session=os.name == "posix",
        )
        try:
            process.wait(timeout=timeout)
            if process.returncode:
                raise ValueError("Worker result failed bounded materialization")
        except subprocess.TimeoutExpired:
            raise TimeoutError(
                "Worker materialization exceeded its wall-clock deadline"
            ) from None
        finally:
            _terminate(process)
    if stdout.stat().st_size > min(spec.max_output_bytes, 2 * 1024**2):
        raise ValueError("Worker materialization preview exceeds its output bound")
    result = json.loads(stdout.read_bytes())
    output = scratch / result["path"]
    if output.resolve() != output or not output.is_relative_to(scratch):
        raise ValueError("Invalid materialized worker path")
    _hash(output, spec.max_output_bytes)
    return output, result["preview"]


def _materialize_local(workbook, spec, value, scratch):
    if not isinstance(value, dict) or value.get("format") != 1:
        raise ValueError("Worker reply requires format=1")
    output = scratch / ("materialized-" + uuid4().hex + ".parquet")
    input_path = None
    if set(value) == {"format", "parquet"}:
        if (
            not isinstance(value["parquet"], str)
            or Path(value["parquet"]).is_absolute()
        ):
            raise ValueError("Worker Parquet output must be a relative scratch path")
        input_path = scratch / value["parquet"]
        if (
            input_path.resolve() != input_path
            or not input_path.is_relative_to(scratch)
            or not stat.S_ISREG(input_path.lstat().st_mode)
            or input_path.stat().st_size > spec.max_output_bytes
        ):
            raise ValueError(
                "Worker output must be a bounded regular file inside scratch"
            )
    elif set(value) != {"format", "columns", "types", "rows"}:
        raise ValueError("Worker reply requires Parquet path or typed columns/rows")
    with duckdb.connect(
        config={
            "threads": str(workbook.settings.query_threads),
            "memory_limit": workbook.settings.query_memory,
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        }
    ) as db:
        paths = [output, *([input_path] if input_path else [])]
        db.execute(
            "SET allowed_paths = ["
            + ",".join(literal(str(path)) for path in paths)
            + "]"
        )
        db.execute("SET enable_external_access=false")
        db.execute("SET lock_configuration=true")
        from threading import Timer

        timer = Timer(
            min(spec.timeout_seconds, workbook.settings.query_timeout), db.interrupt
        )
        timer.daemon = True
        timer.start()
        try:
            if input_path:
                db.execute(
                    "CREATE VIEW result AS SELECT * FROM read_parquet("
                    + literal(str(input_path))
                    + ")"
                )
                if (
                    db.execute("SELECT count(*) FROM result").fetchone()[0]
                    > spec.max_rows
                ):
                    raise ValueError("Worker result exceeds its row bound")
            else:
                names, types, rows = value["columns"], value["types"], value["rows"]
                if (
                    not isinstance(names, list)
                    or not 1 <= len(names) <= 64
                    or any(
                        not isinstance(name, str) or not 1 <= len(name) <= 128
                        for name in names
                    )
                    or len(set(names)) != len(names)
                    or not isinstance(types, list)
                    or len(types) != len(names)
                    or not isinstance(rows, list)
                    or len(rows) > spec.max_rows
                ):
                    raise ValueError("Worker table schema/rows exceed their bounds")
                for kind in types:
                    if (
                        not isinstance(kind, str)
                        or kind
                        not in {"VARCHAR", "BOOLEAN", "BIGINT", "UBIGINT", "DOUBLE"}
                        and not DECIMAL.fullmatch(kind)
                    ):
                        raise ValueError("Unsupported worker scalar type")
                    if DECIMAL.fullmatch(kind) and int(
                        DECIMAL.fullmatch(kind)[2]
                    ) > int(DECIMAL.fullmatch(kind)[1]):
                        raise ValueError("Decimal scale cannot exceed precision")
                schema = ",".join(
                    '"' + name.replace('"', '""') + '" ' + kind
                    for name, kind in zip(names, types, strict=True)
                )
                db.execute("CREATE TABLE result (" + schema + ")")
                converted = []
                for row in rows:
                    if not isinstance(row, list) or len(row) != len(names):
                        raise ValueError(
                            "Worker rows must exactly match their declared columns"
                        )
                    converted.append(
                        [
                            _typed(cell, kind)
                            for cell, kind in zip(row, types, strict=True)
                        ]
                    )
                if converted:
                    db.executemany(
                        "INSERT INTO result VALUES ("
                        + ",".join("?" for _ in names)
                        + ")",
                        converted,
                    )
            # Materialize through DuckDB to reject unsupported/lossy export types.
            from .materialize import lossless_parquet_projection

            relation, conversions = lossless_parquet_projection(
                db.sql("SELECT * FROM result")
            )
            relation.write_parquet(str(output))
            if output.stat().st_size > spec.max_output_bytes:
                raise ValueError("Materialized worker output exceeds its byte bound")
            preview = _preview(db.sql("SELECT * FROM result LIMIT 6"), 5)
            if conversions:
                preview["materialized_type_conversions"] = conversions
            return output, preview
        finally:
            timer.cancel()
            timer.join()


def execute(workbook, step, inputs, registry, *, definition_id, expected_digest=None):
    """Run a configured worker once; never cache a declared arbitrary program."""
    authorize(current_principal(), "workers")
    if registry is None or not isinstance(registry, WorkerRegistry):
        raise ValueError("This task needs a host-configured local worker registry")
    spec = registry.resolve(step["worker"])
    if expected_digest is not None and spec.digest != expected_digest:
        raise ValueError(
            "Worker identity changed from the pinned run; start a fresh run"
        )
    with workbook.connect() as db:
        db.execute("BEGIN")
        sources = [workbook._get(db, ref) for ref in inputs]
    if sum(row["size_bytes"] for row in sources) > spec.max_input_bytes:
        raise ValueError("Worker input snapshot exceeds its configured byte bound")
    request = {
        "format": 1,
        "parameters": step["parameters"],
        "inputs": [
            {
                "artifact_id": row["artifact_id"],
                "sha256": row["digest"],
                "media_type": row["media_type"],
            }
            for row in sources
        ],
    }
    key = hashlib.sha256(
        _json(
            {
                "worker": spec.digest,
                "definition_id": definition_id,
                "step": step["name"],
                "parameters": step["parameters"],
                "inputs": [(row["artifact_id"], row["digest"]) for row in sources],
            }
        ).encode()
    ).hexdigest()
    with TemporaryDirectory(dir=workbook.root, prefix="worker-") as directory:
        scratch = Path(directory).resolve()
        deadline = time.monotonic() + spec.timeout_seconds
        for index, row in enumerate(sources):
            original = workbook.blobs / row["digest"]
            before = original.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_size != row["size_bytes"]:
                raise ValueError("Worker input is not an intact regular artifact")
            target = scratch / (
                "input"
                + str(index + 1)
                + (
                    ".parquet"
                    if row["media_type"] == "application/vnd.apache.parquet"
                    else ".data"
                )
            )
            digest, count = hashlib.sha256(), 0
            with original.open("rb") as source, target.open("wb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    count += len(block)
                    if count > row["size_bytes"] or time.monotonic() > deadline:
                        raise ValueError(
                            "Worker input snapshot exceeded its size/deadline"
                        )
                    digest.update(block)
                    output.write(block)
            after = original.lstat()
            if (
                count != row["size_bytes"]
                or digest.hexdigest() != row["digest"]
                or (before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ValueError("Pinned worker input integrity check failed")
            target.chmod(0o400)
            request["inputs"][index]["path"] = str(target)
        request["output_directory"] = str(scratch)
        request["idempotency_key"] = key
        value = _run(spec, request, scratch)
        output, preview = _materialize(workbook, spec, value, scratch)
        digest = _hash(output, spec.max_output_bytes)
        name = "_task_worker_results/" + key[:32] + "/" + digest[:16]
        try:
            saved = workbook.save_file(
                output,
                name=name,
                kind="table",
                media_type="application/vnd.apache.parquet",
                dependencies=inputs,
                summary="Configured worker result: " + spec.name,
            )
        except ValueError:
            saved = workbook.inspect(name)
            if saved["sha256"] != digest or saved["dependencies"] != list(
                dict.fromkeys(inputs)
            ):
                raise ValueError("Worker result has conflicting provenance") from None
    return preview | {
        "saved": saved,
        "inputs": inputs,
        "worker": {"name": spec.name, "digest": spec.digest},
        "cache": {"hit": False, "key": key, "reason": "configured_worker"},
    }


def _child_limits(data):
    """Bound ordinary child lifetime and each output file; this is not a sandbox.

    The POSIX guard survives exec and kills the process group if its task server
    disappears. Trusted workers that deliberately detach remain outside this
    guarantee. Limits are per file, not an aggregate filesystem quota.
    """
    if os.name == "posix":
        import resource

        leader = os.getpid()
        if os.fork() == 0:
            for descriptor in (0, 1, 2):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            while True:
                try:
                    os.kill(data["supervisor_pid"], 0)
                    alive = os.getppid() == leader
                except ProcessLookupError:
                    alive = False
                if not alive or time.monotonic() >= data["deadline"]:
                    try:
                        os.killpg(leader, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os._exit(0)
                time.sleep(0.1)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (data["max_output_bytes"], data["max_output_bytes"])
        )


def _exec_child(config):
    data = json.loads(Path(config).read_text())
    _child_limits(data)
    if str(Path(data["command"][0]).resolve(strict=True)) != data["executable_target"]:
        raise ValueError("Worker executable changed before launch")
    for path, expected in data["files"]:
        if _hash(path) != expected:
            raise ValueError("Worker identity changed before launch")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT"}
    }
    os.execve(data["command"][0], data["command"], environment)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"--execute", "--materialize"}:
        raise SystemExit("Internal worker launcher only")
    if sys.argv[1] == "--execute":
        _exec_child(sys.argv[2])
    else:
        data = json.loads(Path(sys.argv[2]).read_text())
        _child_limits(data)
        config = SimpleNamespace(**data)
        output, preview = _materialize_local(
            SimpleNamespace(settings=config),
            config,
            data["value"],
            Path(data["scratch"]),
        )
        print(_json({"path": output.name, "preview": preview}))
