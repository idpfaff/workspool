"""Verify limits against actual MCP results, then reconstruct their exact sources."""

import asyncio
import json
import subprocess
import sys

import pytest

from agent_workbook.budget import serialized_result, token_counter, validate_limit
from agent_workbook.render import decode, render
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


def note(workbook, text):
    return workbook.save_text(text, name="source", kind="note", media_type="text/plain")


def counter():
    pytest.importorskip("tiktoken")
    return token_counter("cl100k_base")


def data(result):
    return (
        result.structuredContent
        if result.structuredContent is not None
        else decode(result.content)
    )


@pytest.mark.parametrize("discovery", ["eager", "lazy"])
@pytest.mark.parametrize("representation", ["both", "text", "structured"])
@pytest.mark.parametrize("format", ["compact", "adaptive", "text", "full"])
def test_sync_token_limit_counts_actual_result_and_exact_pages(
    tmp_path, discovery, representation, format
):
    count = counter()
    workbook = Workbook(tmp_path)
    source = "".join(f'line {i}: "α😀" \\ exact\r\n' for i in range(150))
    saved = note(workbook, source)
    app = server_for(
        workbook,
        tools=["sync"],
        tokenizer="cl100k_base",
        tool_discovery=discovery,
        response_content=representation,
        response_format=format,
    )

    async def run():
        pages, offset = [], 0
        for _ in range(100):
            args = {"ref": saved["artifact_id"], "offset": offset, "max_tokens": 500}
            result = await app.call_tool(
                "workspool" if discovery == "lazy" else "sync",
                {"tool": "sync", "arguments": args} if discovery == "lazy" else args,
            )
            assert not result.isError
            assert count(serialized_result(result)) <= 500
            item = data(result)
            assert item["mode"] == "full"
            pages.append(item["excerpt"])
            if item["complete"]:
                assert item["next_offset"] is None
                break
            assert item["next_offset"] > offset
            offset = item["next_offset"]
        else:
            pytest.fail("Pages did not progress")
        assert "".join(pages) == source

    asyncio.run(run())


def test_retrieve_tokens_preserve_exact_reads_and_continuation(tmp_path):
    count = counter()
    workbook = Workbook(tmp_path)
    source = 'a "quoted" 😀 line\n' * 500
    saved = note(workbook, source)
    app = server_for(
        workbook,
        tools=["retrieve"],
        tokenizer="cl100k_base",
        response_format="adaptive",
        response_content="both",
    )

    async def run():
        offset, parts = 0, []
        for _ in range(150):
            result = await app.call_tool(
                "retrieve",
                {
                    "reads": [
                        {"ref": saved["artifact_id"], "offset": offset, "bytes": 32768}
                    ],
                    "max_tokens": 450,
                },
            )
            assert not result.isError
            assert count(serialized_result(result)) <= 450
            item = data(result)
            read = item["reads"][0]
            span = item["spans"][read["span"]]
            assert (
                span["excerpt"].encode()
                == source.encode()[read["start_byte"] : read["end_byte"]]
            )
            parts.append(span["excerpt"])
            if read["next_offset"] is None:
                break
            assert read["next_offset"] > offset
            offset = read["next_offset"]
        else:
            pytest.fail("Read did not progress")
        assert "".join(parts) == source

    asyncio.run(run())


def test_missing_tokenizer_and_impossible_budgets_fail_explicitly(tmp_path):
    for invalid in (0, -1, True, 131073, "20"):
        with pytest.raises(ValueError, match="between"):
            validate_limit(invalid, len)
    workbook = Workbook(tmp_path)
    note(workbook, "small")
    app = server_for(workbook, tools=["sync"])
    with pytest.raises(Exception, match="requires.*tokenizer"):
        asyncio.run(app.call_tool("sync", {"ref": "source", "max_tokens": 100}))
    counter()
    app = server_for(workbook, tools=["sync"], tokenizer="cl100k_base")
    with pytest.raises(Exception, match="budget"):
        asyncio.run(app.call_tool("sync", {"ref": "source", "max_tokens": 1}))


def test_cli_budget_counts_the_printed_output(tmp_path):
    count = counter()
    workbook = Workbook(tmp_path)
    note(workbook, '"日本語😀"\n' * 200)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(tmp_path),
            "sync",
            "source",
            "--tokenizer",
            "cl100k_base",
            "--max-tokens",
            "300",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert count(result.stdout) <= 300
    reply = json.loads(result.stdout)
    assert not reply["complete"] and reply["next_offset"] > 0


def test_token_aware_renderer_uses_provided_cost_and_preserves_nested_sources():
    value = {
        "mode": "full",
        "artifact": {"artifact_id": "wb_example"},
        "excerpt": '"x"\n' * 100,
    }
    chosen = render(value, measure=lambda blocks: -len(blocks))
    assert json.loads(chosen[0].text)["workspool_encoding"] == "sources-v1"
    assert decode(chosen) == value
    collision = {
        "workspool_encoding": "sources-v1",
        "data": "user content",
        "excerpt_paths": [],
    }
    assert decode(render(collision)) == collision


def test_local_tokenizer_needs_no_network(tmp_path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel({"[UNK]": 0, "hello": 1}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    assert token_counter(str(path))("hello unknown") == 2
