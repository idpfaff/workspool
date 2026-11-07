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
    assert not (tmp_path / "host").exists()
    assert not (tmp_path / "actual-workspace").exists()


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
