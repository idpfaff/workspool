"""Cross-feature workflows exercised through the actual MCP CLI."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager

import duckdb
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.query import describe
from agent_workbook.render import decode
from agent_workbook.store import Workbook


@asynccontextmanager
async def session(workspace, *options, efficient=True):
    args = ["-m", "agent_workbook.cli", "--workspace", str(workspace), "serve"]
    if efficient:
        args += ["--profile", "efficient"]
    args += options
    async with (
        stdio_client(StdioServerParameters(command=sys.executable, args=args)) as (
            read,
            write,
        ),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


async def call(client, name, arguments, *, error=False):
    result = await client.call_tool("workspool", {"tool": name, "arguments": arguments})
    assert bool(result.isError) == error, result.content
    if error:
        return result
    data = decode(result.content) if result.content else result.structuredContent
    if result.structuredContent is not None:
        assert data == result.structuredContent
    return data


def note(workbook, name, content, **kwargs):
    return workbook.save_text(
        content, name=name, kind="note", media_type="text/plain", **kwargs
    )


def table(workbook, name, content, **kwargs):
    return workbook.save_text(
        content, name=name, kind="table", media_type="text/csv", **kwargs
    )


def test_efficient_profile_and_explicit_cli_overrides(tmp_path):
    workbook = Workbook(tmp_path)
    table(workbook, "numbers", "n\n2\n3\n")
    for index in range(16):
        note(workbook, f"doc/{index}", 'Exact\n"source"😀\\text' * 100)

    async def run():
        async with session(tmp_path) as client:
            definitions = await client.list_tools()
            assert [tool.name for tool in definitions.tools] == ["workspool"]
            operations = definitions.tools[0].inputSchema["properties"]["tool"]["enum"]
            assert set(operations) == {
                "save",
                "find",
                "inspect",
                "resume",
                "query",
                "refresh",
                "search",
                "describe",
                "diff",
                "sync",
                "retrieve",
            }
            for name in ("import", "delete"):
                await call(client, name, {}, error=True)
                assert (await client.call_tool(name, {})).isError
            raw = await client.call_tool(
                "workspool",
                {
                    "tool": "inspect",
                    "arguments": {
                        "ref": [f"doc/{index}" for index in range(16)],
                        "excerpt_bytes": 8192,
                    },
                },
            )
            assert not raw.isError
            assert (
                json.loads(raw.content[0].text)["workspool_encoding"] == "artifacts-v1"
            )
            assert decode(raw.content) == raw.structuredContent
            assert all(
                item["excerpt"] == 'Exact\n"source"😀\\text' * 100
                for item in decode(raw.content)["artifacts"]
            )

        async with session(
            tmp_path,
            "--tools",
            "query",
            "--tool-discovery",
            "eager",
            "--response-format",
            "compact",
        ) as client:
            definitions = await client.list_tools()
            assert [tool.name for tool in definitions.tools] == ["query"]
            result = await client.call_tool(
                "query",
                {"inputs": ["numbers"], "sql": "SELECT sum(n)::BIGINT FROM input1"},
            )
            assert not result.isError
            assert result.structuredContent["rows"] == [[5]]
            assert json.loads(result.content[0].text) == result.structuredContent
            assert (
                await client.call_tool(
                    "workspool",
                    {"tool": "describe", "arguments": {"inputs": ["numbers"]}},
                )
            ).isError

    asyncio.run(run())


@pytest.mark.parametrize("empty", [True, False])
def test_describe_returns_exact_wide_schema_without_values(tmp_path, empty):
    workbook = Workbook(tmp_path / "workbook")
    names = [f"column_{index}" for index in range(34)] + [
        'very_long_"column_' + "é" * 130
    ]
    types = ["BIGINT", "BOOLEAN", "DECIMAL(12,3)", "TIMESTAMP", "VARCHAR"] * 7
    declaration = ",".join(
        '"' + name.replace('"', '""') + '" ' + sql_type
        for name, sql_type in zip(names, types, strict=True)
    )
    path = tmp_path / "wide.parquet"
    with duckdb.connect() as db:
        db.execute("CREATE TABLE fixture (" + declaration + ")")
        if not empty:
            db.execute("INSERT INTO fixture DEFAULT VALUES")
        db.execute("COPY fixture TO ? (FORMAT PARQUET)", [str(path)])
    saved = workbook.import_path(path, name="wide")

    async def run():
        async with session(workbook.root, "--response-content", "text") as client:
            offset, actual_names, actual_types = 0, [], []
            while offset is not None:
                result = await call(
                    client,
                    "describe",
                    {"inputs": ["wide"], "offset": offset, "limit": 12},
                )
                item = result["tables"][0]
                assert item["artifact_id"] == saved["artifact_id"]
                assert item["input"] == "input1" and item["column_count"] == 35
                assert "rows" not in item and "values" not in item
                actual_names.extend(item["columns"])
                actual_types.extend(item["types"])
                offset = item["next_offset"]
            assert actual_names == names
            assert actual_types == types
            exhausted = await call(
                client, "describe", {"inputs": ["wide"], "offset": 100}
            )
            assert exhausted["tables"][0]["columns"] == []
            assert exhausted["tables"][0]["next_offset"] is None

    asyncio.run(run())
    assert describe(workbook, ["wide"], limit=100)["tables"][0]["columns"] == names


def test_describe_pins_versions_for_query_and_refuses_stale_inputs(tmp_path):
    workbook = Workbook(tmp_path)
    source = table(workbook, "bars", "n,label\n1,original\n")

    async def run():
        async with session(tmp_path) as client:
            schema = await call(client, "describe", {"inputs": ["bars"]})
            pinned = schema["tables"][0]["artifact_id"]
            assert pinned == source["artifact_id"]
            new = table(
                workbook, "bars", "n,label,extra\n2,revised,7\n", expected_version=1
            )
            await call(client, "describe", {"inputs": [pinned]}, error=True)
            await call(
                client,
                "query",
                {"inputs": [pinned], "sql": "SELECT n FROM input1"},
                error=True,
            )
            historical = await call(
                client, "describe", {"inputs": [pinned], "allow_historical": True}
            )
            assert historical["tables"][0]["columns"] == ["n", "label"]
            assert historical["tables"][0]["is_latest"] is False
            old_result = await call(
                client,
                "query",
                {
                    "inputs": [pinned],
                    "sql": "SELECT n FROM input1",
                    "allow_historical": True,
                },
            )
            assert old_result["rows"] == [[1]] and old_result["inputs"] == [pinned]
            current = await call(client, "describe", {"inputs": ["bars"]})
            assert current["tables"][0]["columns"] == ["n", "label", "extra"]
            assert current["tables"][0]["artifact_id"] == new["artifact_id"]
            result = await call(
                client,
                "query",
                {"inputs": [new["artifact_id"]], "sql": "SELECT n FROM input1"},
            )
            assert result["rows"] == [[2]]
            await call(
                client, "describe", {"inputs": ["bars", new["artifact_id"]]}, error=True
            )

    asyncio.run(run())


def test_efficient_search_reads_deep_passages_and_cursor_continues(tmp_path):
    workbook = Workbook(tmp_path)
    source = (
        "Unrelated text.\n" * 6000
        + "α target_phrase ω\n"
        + "filler\n" * 30
        + "Second TARGET_PHRASE end."
    )
    saved = note(workbook, "deep", source)
    assert workbook.find("target_phrase") == []

    async def run():
        async with session(tmp_path, "--response-content", "text") as client:
            args = {
                "text": "target_phrase",
                "refs": ["deep"],
                "context_bytes": 32,
                "limit": 1,
            }
            first = await call(client, "search", args)
            assert len(first["hits"]) == 1 and first["cursor"] is not None
            assert first["complete"] is False
            all_hits = list(first["hits"])
            cursor = first["cursor"]
            while cursor is not None:
                result = await call(client, "search", args | {"cursor": cursor})
                all_hits.extend(result["hits"])
                cursor = result["cursor"]
            assert len(all_hits) == 2
            for hit in all_hits:
                assert hit["artifact_id"] == saved["artifact_id"]
                assert (
                    hit["excerpt"].encode()
                    == source.encode()[hit["start_byte"] : hit["end_byte"]]
                )
                assert (
                    source.encode()[hit["match_start_byte"] : hit["match_end_byte"]]
                    .decode()
                    .lower()
                    == "target_phrase"
                )

    asyncio.run(run())
