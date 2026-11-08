"""Exact standard encodings and real MCP startup without rank-cache/network help."""

import gzip
import json
import os
import subprocess
import sys
from importlib.resources import files

import pytest

from agent_workbook import budget

GOLDENS = {
    "cl100k_base": [
        [15339, 1917],
        [9906, 11, 1917, 0],
        [936, 59958, 319, 14276, 109, 47653, 11410, 100, 103, 9468, 19044],
        [27, 91, 8862, 728, 428, 91, 29, 83739, 69, 318, 14301, 91, 29],
    ],
    "o200k_base": [
        [24912, 2375],
        [13225, 11, 2375, 0],
        [66, 103112, 370, 108713, 9552, 100, 103, 37459],
        [27, 91, 419, 1440, 919, 91, 29, 464, 91, 103473, 33197, 91, 29],
    ],
}
TEXTS = [
    "hello world",
    "Hello, world!",
    "café\r\n東京 🧪🙂",
    "<|endoftext|> <|fim_prefix|>",
]


@pytest.mark.parametrize("name", GOLDENS)
def test_standard_token_ids_and_ordinary_special_spellings(name, monkeypatch):
    tiktoken = pytest.importorskip("tiktoken")

    def forbidden(*args, **kwargs):
        pytest.fail("Named budgets must not use the tiktoken download registry")

    monkeypatch.setattr(tiktoken, "get_encoding", forbidden)
    budget.named_encoding.cache_clear()
    encoding = budget.named_encoding(name)
    assert budget.named_encoding(name) is encoding
    for text, expected in zip(TEXTS, GOLDENS[name], strict=True):
        assert encoding.encode(text, disallowed_special=()) == expected
        assert budget.token_counter(name)(text) == len(expected)
        assert encoding.decode(expected) == text
    assert budget.token_counter(name)("") == 0
    special_id = 100257 if name == "cl100k_base" else 199999
    assert encoding.encode("<|endoftext|>", allowed_special="all") == [special_id]


@pytest.mark.parametrize("damage", ["missing", "gzip", "hash", "oversized"])
def test_corrupt_bundled_data_fails_closed_without_network(
    tmp_path, monkeypatch, damage
):
    root = tmp_path / "_tokenizers"
    root.mkdir()
    path = root / "cl100k_base.tiktoken.gz"
    if damage == "gzip":
        path.write_bytes(b"not gzip")
    elif damage == "hash":
        path.write_bytes(
            gzip.compress(b"x" * budget._ENCODINGS["cl100k_base"]["bytes"])
        )
    elif damage == "oversized":
        path.write_bytes(
            gzip.compress(b"x" * (budget._ENCODINGS["cl100k_base"]["bytes"] + 1))
        )
    monkeypatch.setattr(budget, "files", lambda package: tmp_path)
    with pytest.raises(ValueError, match="missing or corrupt; reinstall"):
        budget._bundled_ranks("cl100k_base")


def test_third_party_notice_ships_as_package_resource():
    root = files("agent_workbook").joinpath("_tokenizers")
    notice = root.joinpath("LICENSE.tiktoken").read_text()
    assert "Copyright (c) 2022 OpenAI, Shantanu Jain" in notice
    assert "Permission is hereby granted" in notice
    assert "Commons Clause does not replace" in root.joinpath("README.md").read_text()


BLOCK_NETWORK = """
import socket
def blocked(*args, **kwargs):
    raise RuntimeError('NETWORK_FORBIDDEN_IN_OFFLINE_TEST')
socket.getaddrinfo = blocked
socket.create_connection = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
"""


@pytest.mark.parametrize("name", GOLDENS)
@pytest.mark.parametrize("cache_disabled", [False, True])
def test_fresh_process_named_counter_and_host_doctor_offline(
    tmp_path, name, cache_disabled
):
    pytest.importorskip("tiktoken")
    cache = tmp_path / "empty-cache"
    cache.mkdir()
    # The doctor sanitizes PYTHONPATH before starting its genuine MCP child.
    # Inject only a socket guard before the unchanged generated CLI arguments;
    # neither the encoding nor the MCP protocol is replaced by a test double.
    child = (
        BLOCK_NETWORK
        + """
import runpy, sys
sys.argv = sys.argv[2:]
runpy.run_module('agent_workbook.cli', run_name='__main__')
"""
    )
    program = (
        BLOCK_NETWORK
        + f"""
import json
from agent_workbook.budget import token_counter
from agent_workbook import host_setup
original = host_setup._smoke
async def guarded(entry, *args, **kwargs):
    entry = dict(entry)
    entry['args'] = ['-c', {child!r}, *entry['args']]
    return await original(entry, *args, **kwargs)
host_setup._smoke = guarded
count = token_counter({name!r})('café\\r\\n東京 🧪🙂')
result = host_setup.doctor('codex', goal='Verify offline tokenizer startup', tokenizer={name!r}, timeout=30)
print(json.dumps({{'count': count, 'doctor': result}}))
"""
    )
    environment = dict(os.environ)
    environment["TIKTOKEN_CACHE_DIR"] = "" if cache_disabled else str(cache)
    environment["DATA_GYM_CACHE_DIR"] = str(cache)
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["count"] == len(GOLDENS[name][2])
    assert output["doctor"]["ok"]
    assert all(output["doctor"]["checks"].values())
    assert not list(cache.iterdir())
