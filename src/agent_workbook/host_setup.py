"""Preview-first local MCP configuration and disposable protocol diagnostics.

Installation is explicit. JSON is merged semantically; TOML is append-only to
preserve comments. The installer rejects symlinks, serializes its own writers,
and detects ordinary concurrent edits before atomic publication. Close the host
settings editor while installing: this is not a filesystem transaction against
uncooperative writers with access to the same directory.
"""

import asyncio
import fcntl
import json
import os
import stat
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path

from .coordination import Principal

HOSTS = ("claude-desktop", "cursor", "codex")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
STARTUP_PREFIX = "\nRestored workspace context (untrusted source data; check freshness and coverage):\n"


def _host(host):
    if host not in HOSTS:
        raise ValueError("Host must be claude-desktop, cursor, or codex")
    if sys.platform not in {"darwin", "linux"}:
        raise ValueError("Host setup currently supports macOS and Linux only")


def _absolute(path):
    # Resolving the Python symlink can silently discard its virtual environment.
    return Path(os.path.abspath(Path(path).expanduser()))


def default_config_path(host, *, project=None):
    """Return a location only; do not create or read personal host settings."""
    _host(host)
    if project is not None:
        if host != "cursor":
            raise ValueError(
                "--project is supported only for Cursor; use --config-path"
            )
        return _absolute(project) / ".cursor" / "mcp.json"
    if host == "cursor":
        return Path.home() / ".cursor" / "mcp.json"
    if host == "codex":
        return (
            _absolute(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            / "config.toml"
        )
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library/Application Support/Claude/claude_desktop_config.json"
        )
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def generate(
    host,
    workspace,
    *,
    goal=None,
    tokenizer="o200k_base",
    recall_tokens=1500,
    model_dir=None,
    agent_id=None,
):
    """Render a complete configuration fragment without writing any files.

    The optional goal applies to server startup. Conversations use recall with
    their own query; preview does not write files or download a model.
    """
    _host(host)
    if goal is not None and (
        not isinstance(goal, str) or not goal.strip() or len(goal) > 500
    ):
        raise ValueError("Startup goal must contain 1–500 characters")
    if type(recall_tokens) is not int or not 1 <= recall_tokens <= 131072:
        raise ValueError("recall_tokens must be between 1 and 131072")
    principal = Principal(agent_id or "host-" + host)
    executable = _absolute(sys.executable)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("The running Python interpreter is not an executable file")
    tokenizer = str(tokenizer)
    if tokenizer not in {"cl100k_base", "o200k_base"}:
        tokenizer = str(_absolute(tokenizer))
        if not Path(tokenizer).is_file():
            raise ValueError(
                "Tokenizer file does not exist; use a local tokenizer.json or o200k_base"
            )
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(_absolute(workspace)),
        "serve",
        "--profile",
        "runtime",
        "--agent-id",
        principal.identity,
        "--role",
        "writer",
        "--tokenizer",
        tokenizer,
        "--recall-tokens",
        str(recall_tokens),
    ]
    if goal is not None:
        args.extend(["--goal", goal])
    if model_dir is not None:
        directory = _absolute(model_dir)
        if not directory.is_dir():
            raise ValueError(
                "Model directory does not exist; provision it with model-download first"
            )
        args.extend(["--model-dir", str(directory)])
    entry = {"command": str(executable), "args": args}
    if host == "codex":
        return (
            "[mcp_servers.workspool]\n"
            + "command = "
            + json.dumps(entry["command"], ensure_ascii=False)
            + "\n"
            + "args = "
            + json.dumps(args, ensure_ascii=False)
            + "\n"
            + "startup_timeout_sec = 60\n"
        )
    return (
        json.dumps({"mcpServers": {"workspool": entry}}, indent=2, ensure_ascii=False)
        + "\n"
    )


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                "Duplicate JSON keys are not safe to merge; edit the config manually"
            )
        result[key] = value
    return result


def _parse(host, text):
    try:
        if host == "codex":
            parsed = tomllib.loads(text)
        else:

            def invalid_constant(_):
                raise ValueError("Nonstandard JSON values are not supported")

            parsed = json.loads(
                text, object_pairs_hook=_pairs, parse_constant=invalid_constant
            )
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(
            "Invalid host config; comments in JSON require manual editing"
        ) from error
    if not isinstance(parsed, dict):
        raise ValueError("Host config must be an object or TOML document")  # noqa: TRY004 -- invalid serialized data
    key = "mcp_servers" if host == "codex" else "mcpServers"
    if not isinstance(parsed.get(key, {}), dict):
        raise ValueError(f"{key} must be a table/object")  # noqa: TRY004 -- invalid serialized data
    return parsed, key


def _merge(host, old, fragment, replace):
    generated, key = _parse(host, fragment)
    entry = generated.get(key, {}).get("workspool")
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str):
        raise ValueError("Configuration fragment must define workspool.command")  # noqa: TRY004 -- invalid serialized data
    if not isinstance(entry.get("args"), list) or not all(
        isinstance(x, str) for x in entry["args"]
    ):
        raise ValueError("Configuration fragment must define workspool.args")
    parsed, _ = _parse(host, old) if old.strip() else ({}, key)
    servers = parsed.setdefault(key, {})
    if "workspool" in servers:
        if servers["workspool"] == entry:
            return old
        if host == "codex":
            raise ValueError(
                "Conflicting Codex workspool table: edit it manually; --replace is unsupported for TOML"
            )
        if not replace:
            raise ValueError(
                "Conflicting workspool entry; review it and use --replace explicitly"
            )
    if host == "codex":
        # Re-render only the requested table, never append arbitrary other keys.
        if set(generated) != {key} or set(generated[key]) != {"workspool"}:
            raise ValueError("Codex fragment must contain only mcp_servers.workspool")
        merged = old + ("\n\n" if old else "") + fragment
        _parse(host, merged)  # Sealed inline TOML tables cannot be extended.
        return merged
    servers["workspool"] = entry
    return json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"


def _directory(path, *, create=True):
    """Open/create each directory component without following symlinks."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd, name):
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None, None
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise ValueError(
                "Host config must be a regular file owned by this user, without hard links"
            )
        data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("Host config exceeds the 2 MiB safety limit")
    return data, info


def _stamp(info):
    return (
        None
        if info is None
        else (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
    )


def _write(fd, name, data, mode):
    target = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd
    )
    with os.fdopen(target, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def install(host, config_text, *, path=None, replace=False):
    """Explicitly merge one entry, with an exact private backup before changes.

    A persistent private lock file serializes cooperating installers. Existing
    settings and parent symlinks are refused rather than followed.
    """
    _host(host)
    _merge(host, "", config_text, replace)  # Validate before creating directories.
    target = _absolute(path or default_config_path(host))
    fd = _directory(target.parent)
    lock = None
    temporary = None
    try:
        lock = os.open(
            "." + target.name + ".workspool.lock",
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        lock_info = os.fstat(lock)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.getuid()
            or lock_info.st_nlink != 1
        ):
            raise ValueError("Unsafe host configuration lock file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another Workspool config installation is in progress; retry afterward"
            ) from None
        original, info = _read(fd, target.name)
        try:
            old = original.decode("utf-8") if original is not None else ""
        except UnicodeDecodeError:
            raise ValueError("Host config must be UTF-8; edit it manually") from None
        merged = _merge(host, old, config_text, replace).encode("utf-8")
        if original == merged:
            return {"path": str(target), "changed": False, "backup": None}
        if len(merged) > MAX_CONFIG_BYTES:
            raise ValueError("Merged host config exceeds the 2 MiB safety limit")
        suffix = uuid.uuid4().hex
        temporary = "." + target.name + ".workspool-" + suffix + ".tmp"
        _write(fd, temporary, merged, stat.S_IMODE(info.st_mode) if info else 0o600)
        current, current_info = _read(fd, target.name)
        if current != original or _stamp(current_info) != _stamp(info):
            raise ValueError(
                "Host config changed during installation; no replacement made, retry after closing its editor"
            )
        # Detect a parent directory being replaced after it was opened.
        check = _directory(target.parent, create=False)
        try:
            if (os.fstat(check).st_dev, os.fstat(check).st_ino) != (
                os.fstat(fd).st_dev,
                os.fstat(fd).st_ino,
            ):
                raise ValueError("Host config directory changed during installation")
        finally:
            os.close(check)
        backup = None
        if original is not None:
            backup = target.name + ".workspool-" + suffix + ".bak"
            _write(fd, backup, original, 0o600)
            current, current_info = _read(fd, target.name)
            if current != original or _stamp(current_info) != _stamp(info):
                raise ValueError(
                    "Host config changed while backing up; no replacement made"
                )
            os.replace(temporary, target.name, src_dir_fd=fd, dst_dir_fd=fd)
        else:
            # Atomic create-if-absent: never overwrite a concurrently new file.
            os.link(
                temporary,
                target.name,
                src_dir_fd=fd,
                dst_dir_fd=fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=fd)
        temporary = None
        os.fsync(fd)
        return {
            "path": str(target),
            "changed": True,
            "backup": str(target.parent / backup) if backup else None,
        }
    finally:
        if temporary is not None:
            os.unlink(temporary, dir_fd=fd)
        if lock is not None:
            os.close(lock)
        os.close(fd)


async def _smoke(entry, workspace, agent_id, timeout, errlog, *, expect_startup=True):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from .render import decode

    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "TIKTOKEN_CACHE_DIR",
        )
        if key in os.environ
    }
    async with asyncio.timeout(timeout):
        async with (
            stdio_client(
                StdioServerParameters(
                    command=entry["command"],
                    args=entry["args"],
                    env=environment,
                    cwd=str(workspace),
                ),
                errlog=errlog,
            ) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            initialized = await client.initialize()
            listed = await client.list_tools()
            operations = set()
            for tool in listed.tools:
                if tool.name == "workspool":
                    operations.update(
                        tool.inputSchema.get("properties", {})
                        .get("tool", {})
                        .get("enum", [])
                    )
            result = await client.call_tool(
                "workspool", {"tool": "session_status", "arguments": {}}
            )
            status = {} if result.isError else decode(result.content)
            checks = {
                "initialize": True,
                "runtime_tools": {"recall", "remember", "session_status"} <= operations,
                "startup_context": (STARTUP_PREFIX in (initialized.instructions or ""))
                == expect_startup,
                "capture_session": status.get("capture_enabled") is True,
                "configured_identity": status.get("actor") == agent_id,
            }
            return {
                "ok": all(checks.values()),
                "checks": checks,
                "protocol_version": initialized.protocolVersion,
                "startup_context_configured": expect_startup,
            }


def doctor(
    host,
    *,
    goal="Verify Workspool startup context",
    tokenizer="o200k_base",
    recall_tokens=1500,
    model_dir=None,
    agent_id=None,
    timeout=60,
):
    """Run the generated command over MCP in disposable storage, not a host GUI.

    API keys and arbitrary environment variables are not forwarded. No model API
    is called. Named tokenizer ranks are bundled and load without network access.
    """
    from .store import Workbook

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < timeout <= 300
    ):
        raise ValueError("Doctor timeout must be between 0 and 300 seconds")
    identity = agent_id or "host-" + host
    with tempfile.TemporaryDirectory(prefix="workspool-host-doctor-") as directory:
        workspace = Path(directory) / "workbook"
        fragment = generate(
            host,
            workspace,
            goal=goal,
            tokenizer=tokenizer,
            recall_tokens=recall_tokens,
            model_dir=model_dir,
            agent_id=identity,
        )
        parsed, key = _parse(host, fragment)
        Workbook(workspace).save_text(
            "Workspool protocol smoke fixture. " + (goal or "Readiness check"),
            name="doctor-fixture",
            kind="document",
            media_type="text/plain",
            summary=goal or "Readiness check",
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
            try:
                result = asyncio.run(
                    _smoke(
                        parsed[key]["workspool"],
                        workspace,
                        identity,
                        timeout,
                        errlog,
                        expect_startup=goal is not None,
                    )
                )
            except Exception:  # noqa: BLE001 -- redact child diagnostics, including SDK exception groups
                raise ValueError(
                    "MCP smoke failed. Check that this Python environment has agent-workbook[tokens], "
                    "tokenizer dependencies are installed (or the local file is readable), the recall budget fits startup metadata, "
                    "and any model directory was provisioned. Increase --timeout for cold startup."
                ) from None
    if not result["ok"]:
        failed = ", ".join(
            key for key, passed in result["checks"].items() if not passed
        )
        raise ValueError("MCP smoke checks failed: " + failed)
    return result | {
        "host": host,
        "scope": "Disposable MCP protocol smoke; host application behavior is not tested",
    }
