"""Request authorization and honest telemetry through both MCP dispatch paths."""

import asyncio

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
def test_metrics_use_exact_same_content_wire_baseline(tmp_path, mode):
    from mcp.types import CallToolResult

    from agent_workbook.server import _text_content

    book = Workbook(tmp_path / "book")
    source = book.save_text(
        "repeatable evidence " * 50, name="note", kind="note", media_type="text/plain"
    )
    app = server_for(
        book,
        profile="runtime",
        tokenizer="o200k_base",
        collect_metrics=True,
        tool_discovery=mode,
    )

    async def check():
        wire, delivered = await invoke(
            app, mode, "inspect", {"ref": source["artifact_id"], "excerpt_bytes": 1000}
        )
        counter = token_counter("o200k_base")
        expected = counter(
            serialized_result(
                CallToolResult(
                    content=_text_content(delivered, raw_excerpts=False),
                    structuredContent=delivered,
                )
            )
        ) - counter(serialized_result(wire))
        _, report = await invoke(app, mode, "metrics", {})
        assert report["recording_errors"] == 0
        assert expected > 0

        # Assert measured values anywhere in the bounded aggregate report without
        # depending on its presentation grouping.
        def dictionaries(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from dictionaries(child)
            elif isinstance(value, list):
                for child in value:
                    yield from dictionaries(child)

        assert any(
            row.get("formatting_tokens_reduced") == expected
            for row in dictionaries(report)
        )
        _, second = await invoke(app, mode, "metrics", {})
        assert second == report

    asyncio.run(check())
