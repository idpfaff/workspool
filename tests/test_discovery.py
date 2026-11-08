"""Explicit experimental discovery preserves validation over the MCP wire."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.store import Workbook


@asynccontextmanager
async def session(
    workspace, *, tools=None, response_format="compact", response_content="both"
):
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(workspace),
        "serve",
        "--experimental-lazy",
        "--response-format",
        response_format,
        "--response-content",
        response_content,
    ]
    if tools:
        args += ["--tools", *tools]
    params = StdioServerParameters(
        command=sys.executable,
        args=args,
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


def reconstruct(result):
    if result.structuredContent is not None:
        return result.structuredContent

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: visit(item) for key, item in value.items()}
        if "excerpt_block" in value:
            value["excerpt"] = result.content[value.pop("excerpt_block")].text
        return value

    return visit(json.loads(result.content[0].text))


def note(workbook, name, content, **kwargs):
    return workbook.save_text(
        content, name=name, kind="note", media_type="text/plain", **kwargs
    )


def test_discovery_advertises_only_allowed_tools_and_validates_calls(tmp_path):
    workbook = Workbook(tmp_path)
    workbook.save_text(
        "n\n9007199254740993\n", name="numbers", kind="table", media_type="text/csv"
    )

    async def run():
        async with session(tmp_path, tools=["query"]) as client:
            definitions = await client.list_tools()
            assert [tool.name for tool in definitions.tools] == ["workspool"]
            schema = definitions.tools[0].inputSchema
            assert schema["properties"]["tool"] == {"const": "query", "type": "string"}
            help_result = await client.call_tool("workspool", {"tool": "query"})
            assert not help_result.isError
            help_data = reconstruct(help_result)
            assert help_data["name"] == "query"
            assert set(help_data["inputSchema"]["required"]) == {"inputs", "sql"}
            invalid = await client.call_tool(
                "workspool", {"tool": "query", "arguments": {"sql": "SELECT 1"}}
            )
            assert invalid.isError
            invalid_type = await client.call_tool(
                "workspool",
                {
                    "tool": "query",
                    "arguments": {"inputs": "numbers", "sql": "SELECT * FROM input1"},
                },
            )
            assert invalid_type.isError
            for name in ["save", "import", "delete", "missing"]:
                denied = await client.call_tool(
                    "workspool", {"tool": name, "arguments": {}}
                )
                assert denied.isError
            direct = await client.call_tool(
                "query", {"inputs": ["numbers"], "sql": "SELECT * FROM input1"}
            )
            assert direct.isError
            for name in ["save", "import", "delete"]:
                assert (await client.call_tool(name, {})).isError
            result = await client.call_tool(
                "workspool",
                {
                    "tool": "query",
                    "arguments": {
                        "inputs": ["numbers"],
                        "sql": "SELECT * FROM input1",
                    },
                },
            )
            assert not result.isError
            data = reconstruct(result)
            assert data["rows"] == [["9007199254740993"]]
            assert data["integer_string_columns"] == [
                {"index": 0, "sql_type": "BIGINT"}
            ]
            assert len(data["inputs"]) == 1

    asyncio.run(run())


@pytest.mark.parametrize("response_content", ["both", "text", "structured"])
def test_lazy_text_batches_and_full_metadata_keep_original_tool_semantics(
    tmp_path, response_content
):
    workbook = Workbook(tmp_path)
    original = note(
        workbook, "source", 'A😀\n"quoted"\\text', summary="Original summary"
    )
    dependent = note(
        workbook, "review", "Evidence", dependencies=[original["artifact_id"]]
    )
    note(workbook, "source", "Changed", expected_version=1)

    async def run():
        async with session(
            tmp_path, response_format="text", response_content=response_content
        ) as client:
            result = await client.call_tool(
                "workspool",
                {
                    "tool": "inspect",
                    "arguments": {
                        "ref": [original["artifact_id"], dependent["artifact_id"]],
                        "excerpt_bytes": 8192,
                        "full": True,
                    },
                },
            )
            assert not result.isError
            data = reconstruct(result)
            assert data == {
                "artifacts": workbook.inspect_many(
                    [original["artifact_id"], dependent["artifact_id"]], 8192
                )
            }
            assert data["artifacts"][0]["is_latest"] is False
            assert data["artifacts"][1]["stale_inputs"] is True
            if response_content == "text":
                assert result.structuredContent is None
                assert len(result.content) == 3
            if response_content == "structured":
                assert result.content == []

            # Explicit empty arguments execute defaults rather than discovery.
            found = await client.call_tool(
                "workspool", {"tool": "find", "arguments": {}}
            )
            assert not found.isError
            assert len(reconstruct(found)["result"]) == 2
            discovered = await client.call_tool(
                "workspool", {"tool": "find", "arguments": None}
            )
            assert reconstruct(discovered)["name"] == "find"

    asyncio.run(run())


def test_lazy_save_keeps_version_checks_and_find_summaries(tmp_path):
    workbook = Workbook(tmp_path)

    async def run():
        async with session(tmp_path) as client:
            create = await client.call_tool(
                "workspool",
                {
                    "tool": "save",
                    "arguments": {
                        "name": "draft",
                        "content": "first",
                        "summary": "Retain this summary",
                    },
                },
            )
            assert not create.isError
            assert reconstruct(create)["version"] == 1
            conflict = await client.call_tool(
                "workspool",
                {
                    "tool": "save",
                    "arguments": {
                        "name": "draft",
                        "content": "wrong",
                    },
                },
            )
            assert conflict.isError
            found = await client.call_tool(
                "workspool",
                {
                    "tool": "find",
                    "arguments": {
                        "text": "draft",
                    },
                },
            )
            assert reconstruct(found)["result"][0]["summary"] == "Retain this summary"
            updated = await client.call_tool(
                "workspool",
                {
                    "tool": "save",
                    "arguments": {
                        "name": "draft",
                        "content": "second",
                        "expected_version": 1,
                    },
                },
            )
            assert not updated.isError
            assert reconstruct(updated)["version"] == 2

    asyncio.run(run())
    assert workbook.inspect("draft", 100)["excerpt"] == "second"
