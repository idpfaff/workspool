"""The host command must not edit settings until its launch check succeeds."""

import json
import sys

import pytest

from agent_workbook.cli import main


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


def command(monkeypatch, tmp_path, tokenizer, *extra):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "workspool",
            "--workspace",
            str(tmp_path / "actual-workspace"),
            "host-config",
            "cursor",
            "--goal",
            "Review customer research",
            "--tokenizer",
            str(tokenizer),
            "--config-path",
            str(tmp_path / "host/mcp.json"),
            *extra,
        ],
    )
    main()


def test_preview_does_not_run_a_check_or_create_settings(
    tmp_path, monkeypatch, capsys, local_tokenizer
):
    command(monkeypatch, tmp_path, local_tokenizer)
    config = json.loads(capsys.readouterr().out)
    assert "workspool" in config["mcpServers"]
    args = config["mcpServers"]["workspool"]["args"]
    assert "--experimental-lazy" not in args
    assert not any(arg.startswith("--tool-discovery") for arg in args)
    assert not (tmp_path / "host").exists()
    assert not (tmp_path / "actual-workspace").exists()


@pytest.mark.parametrize(
    "obsolete",
    [
        ["--tool-discovery", "eager"],
        ["--tool-discovery", "lazy"],
        ["--tool-discovery=eager"],
        ["--tool-discovery=lazy"],
        ["--tool-discovery"],
    ],
)
def test_removed_discovery_flag_has_clear_migration_error(
    tmp_path, monkeypatch, capsys, obsolete
):
    workspace = tmp_path / "unused-workspace"
    monkeypatch.setattr(
        sys, "argv", ["workspool", "--workspace", str(workspace), "serve", *obsolete]
    )
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert "--tool-discovery has been removed" in captured.err
    assert "default for every profile" in captured.err
    assert "--experimental-lazy" in captured.err
    assert "Traceback" not in captured.err and not captured.out
    assert not workspace.exists()


def test_serve_help_only_advertises_explicit_experiment(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["workspool", "serve", "--help"])
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--tool-discovery" not in output
    assert "--experimental-lazy" in output
    assert "eager tool schemas by default" in output


@pytest.mark.parametrize("profile", ["compatible", "efficient", "agent", "runtime"])
@pytest.mark.parametrize("experimental", [False, True])
def test_serve_passes_only_explicit_lazy_opt_in(
    tmp_path, monkeypatch, profile, experimental
):
    from agent_workbook import server

    called = {}

    class App:
        def run(self, *, transport):
            called["transport"] = transport

        def finish_session(self):
            called["finished"] = True

    def configured(workbook, port, **options):
        called.update(options)
        return App()

    monkeypatch.setattr(server, "server_for", configured)
    monkeypatch.setattr(
        sys,
        "argv",
        ["workspool", "--workspace", str(tmp_path), "serve", "--profile", profile]
        + (["--experimental-lazy"] if experimental else []),
    )
    main()
    assert called["experimental_lazy"] is experimental
    assert "tool_discovery" not in called
    assert called["profile"] == profile
    assert called["transport"] == "stdio" and called["finished"] is True


def test_failed_protocol_check_leaves_existing_host_file_unchanged(
    tmp_path, monkeypatch, local_tokenizer
):
    path = tmp_path / "host/mcp.json"
    path.parent.mkdir()
    original = '{"mcpServers":{"other":{"command":"other"}}}\n'
    path.write_text(original)
    with pytest.raises(SystemExit) as stopped:
        command(
            monkeypatch, tmp_path, local_tokenizer, "--install", "--recall-tokens", "1"
        )
    assert stopped.value.code == 1
    assert path.read_text() == original
    assert list(path.parent.iterdir()) == [path]
    assert not (tmp_path / "actual-workspace").exists()


def test_install_returns_successful_real_protocol_check(
    tmp_path, monkeypatch, capsys, local_tokenizer
):
    command(
        monkeypatch, tmp_path, local_tokenizer, "--install", "--recall-tokens", "4096"
    )
    result = json.loads(capsys.readouterr().out)
    assert result["changed"] is True
    assert result["protocol_check"]["ok"] is True
    assert all(result["protocol_check"]["checks"].values())
    assert (tmp_path / "host/mcp.json").exists()
    assert not (tmp_path / "actual-workspace").exists()
