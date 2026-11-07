"""Exercise response representations through the MCP wire protocol."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.server import server_for
from agent_workbook.store import Workbook


@asynccontextmanager
async def session(workspace, response_format="text", response_content="both"):
    script = (
        "import sys; from pathlib import Path; "
        "from agent_workbook.server import server_for; "
        "from agent_workbook.store import Workbook; "
        "server_for(Workbook(Path(sys.argv[1])), "
        "response_format=sys.argv[2], response_content=sys.argv[3]).run()"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", script, str(workspace), response_format, response_content],
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


def reconstruct(result):
    """Follow the public text-format block references without using fixtures."""

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        value = {key: visit(item) for key, item in value.items()}
        if "excerpt_block" in value:
            index = value.pop("excerpt_block")
            assert isinstance(index, int) and 0 < index < len(result.content)
            value["excerpt"] = result.content[index].text
        return value

    return visit(json.loads(result.content[0].text))


def note(workbook, name, content, **kwargs):
    return workbook.save_text(
        content, name=name, kind="note", media_type="text/plain", **kwargs
    )


@pytest.mark.parametrize("response_content", ["both", "text"])
def test_raw_excerpt_batches_preserve_exact_text_and_versions(
    tmp_path, response_content
):
    workbook = Workbook(tmp_path)
    text = 'A😀BéC\n"quotes", backslash \\ and a tab\t\n' * 400
    original = note(workbook, "source", text, summary="Exact original")
    derived = note(
        workbook,
        "review",
        '{"excerpt_block": 99}\n',
        dependencies=[original["artifact_id"]],
    )
    empty = note(workbook, "empty", "")
    note(workbook, "source", "Revised", expected_version=1)
    refs = [original["artifact_id"], derived["artifact_id"], empty["artifact_id"]]

    async def run():
        async with session(tmp_path, response_content=response_content) as client:
            result = await client.call_tool(
                "inspect", {"ref": refs, "excerpt_bytes": 8192}
            )
            assert not result.isError
            assert len(result.content) == 4
            data = reconstruct(result)
            if response_content == "both":
                assert result.structuredContent == data
            else:
                assert result.structuredContent is None
            first, second, third = data["artifacts"]
            assert [item["artifact_id"] for item in data["artifacts"]] == refs
            assert first["is_latest"] is False
            assert second["stale_inputs"] is True
            assert second["dependencies"] == [original["artifact_id"]]
            assert second["excerpt"] == '{"excerpt_block": 99}\n'
            assert third["excerpt"] == "" and third["next_offset"] is None
            assert "sha256" not in first
            pieces = [first["excerpt"]]
            offset = first["next_offset"]
            while offset is not None:
                page = await client.call_tool(
                    "inspect",
                    {"ref": refs[0], "excerpt_bytes": 8192, "offset": offset},
                )
                data = reconstruct(page)
                pieces.append(data["excerpt"])
                offset = data["next_offset"]
            assert "".join(pieces).encode() == text.encode()

            full = await client.call_tool(
                "inspect", {"ref": refs, "excerpt_bytes": 8192, "full": True}
            )
            assert reconstruct(full) == {"artifacts": workbook.inspect_many(refs, 8192)}

    asyncio.run(run())


@pytest.mark.parametrize("response_content", ["text", "structured"])
def test_single_copy_modes_preserve_query_values_and_errors(tmp_path, response_content):
    workbook = Workbook(tmp_path)
    workbook.save_text(
        "n\n9007199254740993\n", name="numbers", kind="table", media_type="text/csv"
    )

    async def run():
        async with session(tmp_path, response_content=response_content) as client:
            definitions = await client.list_tools()
            if response_content == "text":
                assert all(tool.outputSchema is None for tool in definitions.tools)
            result = await client.call_tool(
                "query", {"inputs": ["numbers"], "sql": "SELECT * FROM input1"}
            )
            assert not result.isError
            if response_content == "structured":
                assert result.content == []
                data = result.structuredContent
            else:
                assert len(result.content) == 1
                assert result.structuredContent is None
                data = reconstruct(result)
            assert data["rows"] == [["9007199254740993"]]
            assert data["integer_string_columns"] == [
                {"index": 0, "sql_type": "BIGINT"}
            ]
            assert data["preview_truncated"] is False
            assert len(data["inputs"]) == 1

            # find's generated output schema wraps its list in a result object.
            # This also exercises output-schema handling in the SDK/client.
            found = await client.call_tool("find", {"text": "numbers"})
            assert not found.isError
            data = found.structuredContent or reconstruct(found)
            assert data["result"][0]["name"] == "numbers"
            failed = await client.call_tool("inspect", {"ref": "missing"})
            assert failed.isError
            assert failed.content and "missing" in failed.content[0].text

    asyncio.run(run())


def test_structured_only_inspection_retains_full_excerpt(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", 'Raw\n"text" 😀')

    async def run():
        async with session(tmp_path, response_content="structured") as client:
            result = await client.call_tool(
                "inspect", {"ref": "source", "excerpt_bytes": 8192, "full": True}
            )
            assert not result.isError
            assert result.content == []
            assert result.structuredContent == workbook.inspect("source", 8192)
            assert "excerpt_block" not in result.structuredContent

    asyncio.run(run())


@pytest.mark.parametrize(
    "options, message",
    [
        ({"response_format": "unknown"}, "Response format"),
        ({"response_content": "unknown"}, "Response content"),
    ],
)
def test_invalid_response_configuration_fails(tmp_path, options, message):
    with pytest.raises(ValueError, match=message):
        server_for(Workbook(tmp_path), **options)
