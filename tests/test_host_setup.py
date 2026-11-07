"""Config preservation and actual stdio transport; no personal settings writes."""

import fcntl
import json
import os
import stat
import sys
import tomllib
from pathlib import Path

import pytest

from agent_workbook import host_setup as setup


def fragment(host, tmp_path, **options):
    return setup.generate(
        host, tmp_path / "workspace", goal="Review customer research", **options
    )


def entry(host, text):
    return (
        tomllib.loads(text)["mcp_servers"]["workspool"]
        if host == "codex"
        else json.loads(text)["mcpServers"]["workspool"]
    )


@pytest.mark.parametrize("host", setup.HOSTS)
def test_config_has_absolute_runtime_command_without_creating_workspace(tmp_path, host):
    config = entry(host, fragment(host, tmp_path))
    assert config["command"] == os.path.abspath(sys.executable)
    args = config["args"]
    assert args[:2] == ["-m", "agent_workbook.cli"]
    assert args[args.index("--workspace") + 1] == str(tmp_path / "workspace")
    assert args[args.index("--profile") + 1] == "runtime"
    assert args[args.index("--agent-id") + 1] == "host-" + host
    assert args[args.index("--goal") + 1] == "Review customer research"
    assert args[args.index("--tokenizer") + 1] == "o200k_base"
    assert not (tmp_path / "workspace").exists()
    if host == "codex":
        assert config["startup_timeout_sec"] == 60


def test_interpreter_symlink_is_not_resolved(tmp_path, monkeypatch):
    interpreter = tmp_path / "venv-python"
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(setup.sys, "executable", str(interpreter))
    assert entry("cursor", fragment("cursor", tmp_path))["command"] == str(interpreter)


def test_codex_unicode_and_spaces_round_trip(tmp_path, monkeypatch):
    interpreter = tmp_path / "Python 🐍 with spaces"
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(setup.sys, "executable", str(interpreter))
    config = setup.generate(
        "codex", tmp_path / "資料 space", goal="Review café research 🧪"
    )
    parsed = entry("codex", config)
    assert parsed["command"] == str(interpreter)
    assert "Review café research 🧪" in parsed["args"]
    assert str(tmp_path / "資料 space") in parsed["args"]


def test_default_paths_and_project_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(setup.sys, "platform", "darwin")
    assert (
        setup.default_config_path("claude-desktop")
        == tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"
    )
    monkeypatch.setattr(setup.sys, "platform", "linux")
    assert (
        setup.default_config_path("claude-desktop")
        == tmp_path / ".config/Claude/claude_desktop_config.json"
    )
    assert setup.default_config_path("cursor") == tmp_path / ".cursor/mcp.json"
    assert (
        setup.default_config_path("cursor", project=tmp_path / "project")
        == tmp_path / "project/.cursor/mcp.json"
    )
    assert setup.default_config_path("codex") == tmp_path / ".codex/config.toml"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert setup.default_config_path("codex") == tmp_path / "codex-home/config.toml"
    with pytest.raises(ValueError, match="only for Cursor"):
        setup.default_config_path("codex", project=tmp_path)
    monkeypatch.setattr(setup.sys, "platform", "win32")
    with pytest.raises(ValueError, match="macOS and Linux"):
        fragment("cursor", tmp_path)


@pytest.mark.parametrize("host", ["cursor", "claude-desktop"])
def test_json_merge_exact_backup_conflict_replace_and_noop(tmp_path, host):
    path = tmp_path / "settings.json"
    old = b'{"theme":"dark", "mcpServers":{"other":{"command":"elsewhere"}}}\n'
    path.write_bytes(old)
    result = setup.install(host, fragment(host, tmp_path), path=path)
    merged = json.loads(path.read_text())
    assert merged["theme"] == "dark"
    assert merged["mcpServers"]["other"] == {"command": "elsewhere"}
    assert Path(result["backup"]).read_bytes() == old
    assert stat.S_IMODE(Path(result["backup"]).stat().st_mode) == 0o600
    before = path.read_bytes()
    assert setup.install(host, fragment(host, tmp_path), path=path)["changed"] is False
    changed = fragment(host, tmp_path, agent_id="different-host")
    with pytest.raises(ValueError, match="Conflicting"):
        setup.install(host, changed, path=path)
    assert path.read_bytes() == before
    replaced = setup.install(host, changed, path=path, replace=True)
    assert Path(replaced["backup"]).read_bytes() == before
    assert json.loads(path.read_text())["mcpServers"]["workspool"] == entry(
        host, changed
    )
    assert json.loads(path.read_text())["theme"] == "dark"


def test_toml_append_preserves_comments_and_refuses_replacement(tmp_path):
    path = tmp_path / "config.toml"
    old = '# User comment\nmodel = "example"\n[mcp_servers.other]\ncommand="other"\n'
    path.write_text(old)
    generated = fragment("codex", tmp_path)
    setup.install("codex", generated, path=path)
    assert path.read_text().startswith(old)
    assert tomllib.loads(path.read_text())["model"] == "example"
    assert not setup.install("codex", generated, path=path)["changed"]
    with pytest.raises(ValueError, match="unsupported for TOML"):
        setup.install(
            "codex",
            fragment("codex", tmp_path, agent_id="new"),
            path=path,
            replace=True,
        )


@pytest.mark.parametrize(
    "old",
    [
        '{"mcpServers":{},"mcpServers":{}}',
        "{// comments\n}",
        '{"mcpServers": []}',
        "[1]",
        '{"value": NaN}',
    ],
)
def test_ambiguous_json_refused_without_rewrite(tmp_path, old):
    path = tmp_path / "settings.json"
    path.write_text(old)
    with pytest.raises(ValueError):
        setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert path.read_text() == old
    assert not list(tmp_path.glob("*.bak"))


def test_sealed_inline_toml_refused_without_rewrite(tmp_path):
    path = tmp_path / "config.toml"
    old = 'mcp_servers = {other = {command = "other"}}\n'
    path.write_text(old)
    with pytest.raises(ValueError):
        setup.install("codex", fragment("codex", tmp_path), path=path)
    assert path.read_text() == old


@pytest.mark.parametrize("kind", ["file", "parent", "lock", "hardlink", "fifo"])
def test_unusual_paths_are_not_followed(tmp_path, kind):
    real = tmp_path / "real"
    real.mkdir()
    path = real / "settings.json"
    path.write_text("{}")
    if kind == "file":
        target = tmp_path / "link.json"
        target.symlink_to(path)
    elif kind == "parent":
        directory = tmp_path / "linked"
        directory.symlink_to(real, target_is_directory=True)
        target = directory / "settings.json"
    elif kind == "lock":
        (real / ".settings.json.workspool.lock").symlink_to(path)
        target = path
    elif kind == "hardlink":
        target = tmp_path / "hardlink.json"
        os.link(path, target)
    else:
        target = tmp_path / "fifo"
        os.mkfifo(target)
    with pytest.raises((OSError, ValueError)):
        setup.install("cursor", fragment("cursor", tmp_path), path=target)
    assert path.read_text() == "{}"


def test_concurrent_edit_detected_before_replacement(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"original":true}')
    original_merge = setup._merge

    def edit_during_merge(host, old, generated, replace):
        if old:
            path.write_text('{"edited":true}')
        return original_merge(host, old, generated, replace)

    monkeypatch.setattr(setup, "_merge", edit_during_merge)
    with pytest.raises(ValueError, match="changed during installation"):
        setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert path.read_text() == '{"edited":true}'
    assert not list(tmp_path.glob("*.tmp"))


def test_new_config_private_and_existing_size_bounded(tmp_path):
    path = tmp_path / "new/config.json"
    result = setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert result["changed"] and result["backup"] is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_bytes(b" " * (setup.MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError, match="2 MiB"):
        setup.install("cursor", fragment("cursor", tmp_path), path=path)


def test_installers_do_not_wait_on_active_lock(tmp_path):
    path = tmp_path / "settings.json"
    lock = tmp_path / ".settings.json.workspool.lock"
    with lock.open("w") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="in progress"):
            setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert not path.exists()


def test_atomic_publication_failure_keeps_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text('{"theme":"original"}')

    def fail(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(setup.os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert path.read_text() == '{"theme":"original"}'
    assert next(tmp_path.glob("*.bak")).read_text() == path.read_text()
    assert not list(tmp_path.glob("*.tmp"))


def test_new_config_does_not_replace_concurrently_created_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    original_link = setup.os.link

    def competing_create(*args, **kwargs):
        path.write_text('{"from":"another process"}')
        return original_link(*args, **kwargs)

    monkeypatch.setattr(setup.os, "link", competing_create)
    with pytest.raises(FileExistsError):
        setup.install("cursor", fragment("cursor", tmp_path), path=path)
    assert path.read_text() == '{"from":"another process"}'


@pytest.fixture
def local_tokenizer(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(
            vocab={character: index for index, character in enumerate(alphabet)},
            merges=[],
        )
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(
        add_prefix_space=False
    )
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return path


@pytest.mark.parametrize("host", setup.HOSTS)
def test_generated_command_real_mcp_smoke(host, local_tokenizer):
    result = setup.doctor(
        host, tokenizer=str(local_tokenizer), recall_tokens=4096, timeout=30
    )
    assert result["ok"] is True
    assert all(result["checks"].values())
    assert "host application behavior is not tested" in result["scope"]


def test_doctor_invalid_tokenizer_and_failure_are_actionable(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="Tokenizer file does not exist"):
        setup.doctor("cursor", tokenizer=str(tmp_path / "missing.json"))

    async def fail(*args):
        raise RuntimeError("secret credential must not appear")

    monkeypatch.setattr(setup, "_smoke", fail)
    with pytest.raises(ValueError, match="MCP smoke failed") as error:
        setup.doctor("cursor")
    assert "secret credential" not in str(error.value)


def test_doctor_explicit_timeout_and_bad_checks_fail(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="timeout"):
        setup.doctor("cursor", timeout=0)

    async def bad_checks(*args):
        return {"ok": False, "checks": {"startup_context": False}}

    monkeypatch.setattr(setup, "_smoke", bad_checks)
    with pytest.raises(ValueError, match="startup_context"):
        setup.doctor("cursor")


@pytest.mark.parametrize(
    "options",
    [
        {"recall_tokens": True},
        {"recall_tokens": 0},
        {"agent_id": "unsafe owner"},
        {"model_dir": "/missing-model-directory"},
    ],
)
def test_invalid_options_fail_early(tmp_path, options):
    with pytest.raises(ValueError):
        fragment("cursor", tmp_path, **options)
