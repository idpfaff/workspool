"""Request authorization and honest telemetry through both MCP dispatch paths."""

import asyncio
import json

import pytest

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.render import decode
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


async def invoke(app, mode, tool, arguments):
    result = await app.call_tool(
        "workspool" if mode == "lazy" else tool,
        {"tool": tool, "arguments": arguments} if mode == "lazy" else arguments,
    )
    assert not result.isError
    return result, result.structuredContent or decode(result.content)


@pytest.mark.parametrize("mode", ["eager", "lazy"])
def test_restricted_writer_cannot_write_through_query_or_hidden_tool(tmp_path, mode):
    book = Workbook(tmp_path / "book")
    book.save_text("n\n2\n3\n", name="numbers", kind="table", media_type="text/csv")
    app = server_for(
        book, profile="runtime", capabilities=["read"], tool_discovery=mode
    )

    async def check():
        _, result = await invoke(
            app,
            mode,
            "query",
            {
                "inputs": ["numbers"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            },
        )
        assert result["rows"] == [[5]]
        for tool, arguments in [
            (
                "query",
                {
                    "inputs": ["numbers"],
                    "sql": "SELECT * FROM input1",
                    "save_as": "forbidden",
                },
            ),
            ("save", {"name": "forbidden", "content": "not allowed"}),
            (
                "task_schedule",
                {"action": "set", "name": "bad", "task": "absent", "on_change": True},
            ),
        ]:
            with pytest.raises(PermissionError):
                await invoke(app, mode, tool, arguments)
        _, listing = await invoke(app, mode, "task_schedule", {})
        assert listing["schedules"] == []
        assert not any(item["name"] == "forbidden" for item in book.find())

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["eager", "lazy"])
@pytest.mark.parametrize("response_format", ["compact", "text", "adaptive"])
@pytest.mark.parametrize("response_content", ["text", "both", "structured"])
def test_plain_json_has_zero_content_savings_in_every_response_mode(
    tmp_path, mode, response_format, response_content
):
    book = Workbook(tmp_path / "book")
    book.save_text("n\n2\n3\n", name="numbers", kind="table", media_type="text/csv")
    app = server_for(
        book,
        profile="runtime",
        tokenizer="o200k_base",
        collect_metrics=True,
        tool_discovery=mode,
        response_format=response_format,
        response_content=response_content,
    )

    async def check():
        wire, delivered = await invoke(
            app,
            mode,
            "query",
            {
                "inputs": ["numbers"],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            },
        )
        counter = token_counter("o200k_base")
        baseline = counter(
            json.dumps(delivered, ensure_ascii=False, separators=(",", ":"))
        )
        assert not wire.content or wire.content[0].text == json.dumps(
            delivered, ensure_ascii=False, separators=(",", ":")
        )
        _, report = await invoke(app, mode, "metrics", {})
        assert report["recording_errors"] == 0
        assert report["measurement_version"] == 2
        group = next(row for row in report["groups"] if row["tool"] == "query")
        assert group["content_samples"] == group["wire_samples"] == 1
        assert group["content_tokens"] == group["json_content_tokens"] == baseline
        assert group["content_tokens_reduced"] == 0
        assert group["wire_tokens"] == counter(serialized_result(wire)) > baseline
        _, second = await invoke(app, mode, "metrics", {})
        assert second == report

    asyncio.run(check())


@pytest.mark.parametrize("mode", ["eager", "lazy"])
@pytest.mark.parametrize("response_content", ["text", "both"])
def test_raw_excerpt_counts_one_exact_content_representation(
    tmp_path, mode, response_content
):
    book = Workbook(tmp_path / "book")
    source = book.save_text(
        ('{"quote":"café 東京\\nested"}\n' * 120),
        name="note",
        kind="note",
        media_type="text/plain",
    )
    app = server_for(
        book,
        profile="runtime",
        tokenizer="o200k_base",
        collect_metrics=True,
        tool_discovery=mode,
        response_format="text",
        response_content=response_content,
    )

    async def check():
        arguments = {"ref": source["artifact_id"], "excerpt_bytes": 8192}
        wire = await app.call_tool(
            "workspool" if mode == "lazy" else "inspect",
            {"tool": "inspect", "arguments": arguments}
            if mode == "lazy"
            else arguments,
        )
        assert not wire.isError
        assert len(wire.content) == 2
        delivered = json.loads(wire.content[0].text)
        assert delivered.pop("excerpt_block") == 1
        delivered["excerpt"] = wire.content[1].text
        counter = token_counter("o200k_base")
        actual = sum(counter(block.text) for block in wire.content)
        baseline = counter(
            json.dumps(delivered, ensure_ascii=False, separators=(",", ":"))
        )
        _, report = await invoke(app, mode, "metrics", {})
        group = next(row for row in report["groups"] if row["tool"] == "inspect")
        assert group["content_tokens"] == actual
        assert group["json_content_tokens"] == baseline
        assert group["content_tokens_reduced"] == baseline - actual > 0
        assert group["wire_tokens"] == counter(serialized_result(wire))
        assert group["wire_tokens"] > group["content_tokens"]
        assert report["recording_errors"] == 0

    asyncio.run(check())


@pytest.mark.parametrize(
    "response_format,blocks,structured",
    [
        ("text", ['{"artifact_id":"wb_fixture","excerpt_block":99}', "unmapped"], None),
        ("adaptive", ["not json"], None),
        ("adaptive", ['{"value":true}'], {"value": 1}),
    ],
)
def test_ambiguous_content_records_missing_comparison_without_guessing(
    tmp_path, response_format, blocks, structured
):
    from mcp.types import CallToolResult, TextContent

    from agent_workbook.observability import metrics

    book = Workbook(tmp_path)
    app = server_for(
        book,
        tokenizer="o200k_base",
        collect_metrics=True,
        response_format=response_format,
        response_content="text",
    )
    result = CallToolResult(
        content=[TextContent(type="text", text=value) for value in blocks],
        structuredContent=structured,
    )
    app._measure("inspect", result=result, elapsed=0.1)
    group = metrics(book)["groups"][0]
    assert (
        group["calls"] == group["content_missing_calls"] == group["wire_samples"] == 1
    )
    assert group["content_samples"] == group["content_tokens_reduced"] == 0
    assert group["wire_tokens"] > 0 and app.metrics_errors == 0
