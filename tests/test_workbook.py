import asyncio
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex

import duckdb
import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.bench import benchmark
from agent_workbook.query import query, refresh
from agent_workbook.server import server_for
from agent_workbook.settings import Settings
from agent_workbook.store import Workbook, default_root


def note(wb, name, content, **kwargs):
    return wb.save_text(
        content, name=name, kind="note", media_type="text/plain", **kwargs
    )


def table(wb, name, content, **kwargs):
    return wb.save_text(
        content, name=name, kind="table", media_type="text/csv", **kwargs
    )


def checkpoint(wb, name, **kwargs):
    return wb.save_text(
        "",
        name=name,
        kind="checkpoint",
        media_type="text/plain",
        checkpoint={
            "objective": "Finish the lookup fix",
            "verified": "Offline regression tests passed",
            "next_step": "Get the fix reviewed",
        },
        **kwargs,
    )


def test_resume_only_selects_explicit_checkpoints_or_namespaces(tmp_path):
    wb = Workbook(tmp_path)
    patch = note(wb, "lean/patch", "Tested changes")
    old = checkpoint(wb, "lean/handoff", dependencies=[patch["artifact_id"]])
    note(wb, "openbb/worktree", "Waiting for CI")
    assert wb.resume("OpenBB regression tests") == {"checkpoint": None, "artifacts": []}
    assert wb.resume("openbb")["checkpoint"] is None
    assert wb.resume("LEAN")["checkpoint"]["artifact_id"] == old["artifact_id"]
    updated = checkpoint(wb, "lean/handoff", expected_version=1)
    other = checkpoint(wb, "lean/followup")
    checkpoint(wb, "leaner/handoff")
    assert (
        wb.resume("lean/handoff")["checkpoint"]["artifact_id"] == updated["artifact_id"]
    )
    history = wb.resume(old["artifact_id"])
    assert history["checkpoint"]["is_latest"] is False
    assert history["artifacts"][0]["artifact_id"] == patch["artifact_id"]
    ambiguous = wb.resume("lean/")
    assert ambiguous["checkpoint"] is None and ambiguous["artifacts"] == []
    assert {row["artifact_id"] for row in ambiguous["candidates"]} == {
        updated["artifact_id"],
        other["artifact_id"],
    }
    assert wb.resume()["checkpoint"] is None


def test_resume_namespaces_support_unicode(tmp_path):
    wb = Workbook(tmp_path)
    saved = checkpoint(wb, "Straße/Notizen")
    assert wb.resume("STRASSE")["checkpoint"]["artifact_id"] == saved["artifact_id"]


def test_versions_and_recursive_invalidation(tmp_path):
    wb = Workbook(tmp_path)
    first = note(wb, "source", "original")
    report = note(wb, "report", "summary", dependencies=[first["artifact_id"]])
    deliverable = note(wb, "deliverable", "conclusion", dependencies=["report"])
    with pytest.raises(ValueError, match="expected_version"):
        note(wb, "source", "overwrite")
    second = note(wb, "source", "revision", expected_version=1)
    reopened = Workbook(tmp_path)
    assert reopened.inspect("source")["previous_id"] == first["artifact_id"]
    assert reopened.inspect(first["artifact_id"], 40)["excerpt"] == "original"
    assert reopened.inspect(second["artifact_id"])["version"] == 2
    assert reopened.inspect(report["artifact_id"])["stale_inputs"]
    assert reopened.inspect(deliverable["artifact_id"])["stale_inputs"]
    assert len(reopened.find("source")) == 1


def test_concurrent_updates_have_one_winner(tmp_path):
    wb = Workbook(tmp_path)
    note(wb, "note", "first")

    def write(value):
        try:
            return note(Workbook(tmp_path), "note", str(value), expected_version=1)
        except ValueError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    assert sum(item is not None for item in results) == 1
    assert wb.inspect("note")["version"] == 2


def test_excerpt_pages_preserve_unicode(tmp_path):
    wb = Workbook(tmp_path)
    value = "A😀BéC"
    item = note(wb, "unicode", value)
    offset = 0
    pieces = []
    while offset is not None:
        part = wb.inspect(item["artifact_id"], 4, offset)
        pieces.append(part["excerpt"])
        offset = part["next_offset"]
    assert "".join(pieces) == value


def test_native_file_round_trip(tmp_path):
    wb = Workbook(tmp_path / "workbook")
    sample = tmp_path / "chart.png"
    sample.write_bytes(b"\x89PNG\x00\xff")
    item = wb.save_file(sample, name="chart", kind="file", media_type="image/png")
    target = tmp_path / "export.png"
    wb.export(item["artifact_id"], target)
    assert target.read_bytes() == sample.read_bytes()
    assert "excerpt" not in wb.inspect("chart", 40)
    with pytest.raises(FileExistsError):
        wb.export("chart", target)


def test_tabular_roundtrip_and_refresh(tmp_path):
    wb = Workbook(tmp_path)
    first = table(wb, "bars", "n,price\n1,5\n2,15\n")
    result = query(
        wb,
        ["bars"],
        "SELECT sum(price)::BIGINT AS total FROM input1 WHERE n >= ?",
        [1],
        save_as="sum",
    )
    assert result["rows"] == [[20]]
    assert query(wb, ["sum"], "SELECT * FROM input1")["rows"] == [[20]]
    table(wb, "bars", "n,price\n1,25\n", expected_version=1)
    with pytest.raises(ValueError, match="stale"):
        query(wb, ["sum"], "SELECT * FROM input1")
    assert query(
        wb, [first["artifact_id"]], "SELECT count(*) FROM input1", allow_historical=True
    )["rows"] == [[2]]
    new = refresh(wb, "sum")
    assert new["rows"] == [[25]]
    assert new["saved"]["version"] == 2
    assert not wb.inspect("sum")["stale_inputs"]


def test_query_refuses_transitive_cycle_without_saving(tmp_path):
    wb = Workbook(tmp_path)
    table(wb, "features", "x\n2\n")
    query(wb, ["features"], "SELECT x + 1 AS x FROM input1", save_as="review")
    with pytest.raises(ValueError, match="dependency cycle"):
        query(
            wb,
            ["review"],
            "SELECT x + 1 AS x FROM input1",
            save_as="features",
            expected_version=1,
        )
    assert wb.inspect("features")["version"] == 1
    assert refresh(wb, "review")["rows"] == [[3]]


def test_historical_input_cannot_create_a_cycle_through_its_current_alias(tmp_path):
    wb = Workbook(tmp_path)
    table(wb, "features", "x\n2\n")
    old_review = table(wb, "review", "x\n1\n")
    query(
        wb,
        ["features"],
        "SELECT x FROM input1",
        save_as="review",
        expected_version=1,
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        query(
            wb,
            [old_review["artifact_id"]],
            "SELECT x FROM input1",
            save_as="features",
            expected_version=1,
            allow_historical=True,
        )
    assert wb.inspect("features")["version"] == 1
    assert not wb.inspect("review")["stale_inputs"]


@pytest.mark.parametrize("save_as", [None, "exact"])
def test_query_preview_preserves_large_integers_and_marks_encoding(tmp_path, save_as):
    wb = Workbook(tmp_path)
    table(wb, "data", "x\n1\n")
    result = query(
        wb,
        ["data"],
        """
        SELECT 1794000000000000123::BIGINT AS timestamp_ns,
               -9007199254740992::BIGINT AS negative,
               9007199254740991::BIGINT AS safe, true AS flag
        FROM input1
        """,
        save_as=save_as,
    )
    assert result["rows"] == [
        ["1794000000000000123", "-9007199254740992", 9007199254740991, True],
    ]
    assert result["integer_string_columns"] == [
        {"index": 0, "sql_type": "BIGINT"},
        {"index": 1, "sql_type": "BIGINT"},
    ]
    if save_as:
        reread = query(wb, [save_as], "SELECT timestamp_ns + 1 AS value FROM input1")
        assert reread["rows"] == [["1794000000000000124"]]


def test_query_bounds_output_and_blocks_unregistered_file(tmp_path):
    wb = Workbook(tmp_path / "workbook")
    table(wb, "data", "x\n1\n")
    file = tmp_path / "outside.csv"
    file.write_text("secret\n42\n")
    with pytest.raises(duckdb.Error):
        query(wb, ["data"], "SELECT * FROM read_csv_auto(?)", [str(file)])
    with pytest.raises(ValueError, match="one SELECT"):
        query(wb, ["data"], "SELECT 1; SET threads=1")
    with pytest.raises(ValueError, match="one SELECT"):
        query(wb, ["data"], "COPY (SELECT 1) TO '/tmp/should-not-write'")
    result = query(wb, ["data"], "SELECT range, repeat('a', 20000) FROM range(1000)")
    assert len(result["rows"]) <= 5
    assert len(result["rows"][0][1]) == 240
    assert result["preview_truncated"]


def test_two_independent_mcp_sessions_share_work(tmp_path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_workbook.cli", "--workspace", str(tmp_path), "serve"],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "save",
                "find",
                "inspect",
                "resume",
                "query",
                "refresh",
            }
            saved = await session.call_tool(
                "save",
                {
                    "name": "lean/fix",
                    "content": "Patch is on a fork",
                    "kind": "code",
                    "summary": "History lookup patch",
                },
            )
            assert not saved.isError
            ref = saved.structuredContent["artifact_id"]
            saved = await session.call_tool(
                "save",
                {
                    "name": "lean/handoff",
                    "kind": "checkpoint",
                    "dependencies": [ref],
                    "checkpoint": {
                        "objective": "Publish the LEAN fix",
                        "verified": "Offline regression passed",
                        "next_step": "Submit for maintainer review",
                    },
                },
            )
            assert not saved.isError

        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            resumed = await session.call_tool("resume", {"text": "LEAN"})
            assert not resumed.isError
            assert resumed.structuredContent["checkpoint"]["dependencies"] == [ref]
            inspected = await session.call_tool(
                "inspect", {"ref": ref, "excerpt_bytes": 100}
            )
            assert inspected.structuredContent["excerpt"] == "Patch is on a fork"
            failed = await session.call_tool("inspect", {"ref": "missing"})
            assert failed.isError

    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "too-short"])
def test_http_refuses_to_start_without_a_valid_token(tmp_path, monkeypatch, token):
    monkeypatch.delenv("WORKSPOOL_TOKEN", raising=False)
    monkeypatch.delenv("AGENT_WORKBOOK_TOKEN", raising=False)
    if token is not None:
        # The legacy variable is still read.
        monkeypatch.setenv("AGENT_WORKBOOK_TOKEN", token)
    with pytest.raises(ValueError, match="WORKSPOOL_TOKEN"):
        server_for(Workbook(tmp_path)).streamable_http_app()


def test_http_requires_the_same_bearer_token_for_every_request(tmp_path, monkeypatch):
    wb = Workbook(tmp_path)
    saved = checkpoint(wb, "private/handoff")
    token = token_hex(32)
    monkeypatch.delenv("AGENT_WORKBOOK_TOKEN", raising=False)
    monkeypatch.setenv("WORKSPOOL_TOKEN", token)
    app = server_for(wb).streamable_http_app()

    async def run():
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "resume", "arguments": {}},
        }
        accept = ("Accept", "application/json, text/event-stream")
        auth = ("Authorization", f"Bearer {token}")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8731",
            ) as client,
        ):
            for headers in [
                [accept],
                [accept, ("Authorization", "Bearer wrong")],
                [accept, auth, auth],
            ]:
                rejected = await client.post("/mcp", json=request, headers=headers)
                assert rejected.status_code == 401
                assert rejected.headers["WWW-Authenticate"] == "Bearer"
                assert "checkpoint" not in rejected.json()

            allowed = await client.post("/mcp", json=request, headers=[accept, auth])
            assert allowed.status_code == 200
            checkpoint_id = allowed.json()["result"]["structuredContent"]["checkpoint"][
                "artifact_id"
            ]
            assert checkpoint_id == saved["artifact_id"]
            rejected = await client.post("/mcp", json=request, headers=[accept])
            assert rejected.status_code == 401

    asyncio.run(run())


def test_benchmark_produces_same_exact_result():
    report = benchmark()
    assert report["expected_result"] == {"positive_bars": 950, "total_bp": -40}
    assert report["workbook_handoff_bytes"] < report["raw_handoff_bytes"] / 5


def test_benchmark_accepts_local_tokenizer(tmp_path: Path):
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.WordLevel({"[UNK]": 0}, unk_token="[UNK]")
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    report = benchmark(path)
    assert report["raw_handoff_tokens"] > report["workbook_handoff_tokens"] > 0


def test_compact_mcp_batch_reads_preserve_content_versions_and_freshness(tmp_path):
    wb = Workbook(tmp_path)
    original = note(wb, "source", "A😀BéC" * 2000)
    derived = note(
        wb, "review", "Pinned review", dependencies=[original["artifact_id"]]
    )
    note(wb, "source", "Updated source", expected_version=1)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_workbook.cli", "--workspace", str(tmp_path), "serve"],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "inspect",
                {
                    "ref": [original["artifact_id"], derived["artifact_id"]],
                    "excerpt_bytes": 8192,
                },
            )
            assert not result.isError
            text = result.content[0].text
            assert json.loads(text) == result.structuredContent
            first, second = result.structuredContent["artifacts"]
            assert first["artifact_id"] == original["artifact_id"]
            assert first["is_latest"] is False
            assert second["stale_inputs"] is True
            assert second["dependencies"] == [original["artifact_id"]]
            assert first["next_offset"] is not None
            assert "sha256" not in first
            pieces = [first["excerpt"]]
            offset = first["next_offset"]
            while offset is not None:
                rest = await session.call_tool(
                    "inspect",
                    {
                        "ref": first["artifact_id"],
                        "excerpt_bytes": 8192,
                        "offset": offset,
                    },
                )
                pieces.append(rest.structuredContent["excerpt"])
                offset = rest.structuredContent["next_offset"]
            assert "".join(pieces) == "A😀BéC" * 2000
            full = await session.call_tool(
                "inspect",
                {
                    "ref": original["artifact_id"],
                    "full": True,
                },
            )
            assert full.structuredContent == wb.inspect(original["artifact_id"])
            for refs in [
                [],
                ["source"] * 2,
                ["review", derived["artifact_id"]],
                [str(i) for i in range(17)],
            ]:
                invalid = await session.call_tool("inspect", {"ref": refs})
                assert invalid.isError

    asyncio.run(run())


def test_full_response_mode_restores_metadata_over_mcp(tmp_path):
    wb = Workbook(tmp_path)
    saved = note(
        wb, "source", "Evidence", source="original.txt", summary="Keep the source"
    )

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(tmp_path),
                "serve",
                "--response-format",
                "full",
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "inspect", {"ref": "source", "excerpt_bytes": 100}
            )
            assert not result.isError
            assert result.structuredContent == wb.inspect(saved["artifact_id"], 100)
            assert json.loads(result.content[0].text) == result.structuredContent

    asyncio.run(run())


def test_tool_allowlist_rejects_hidden_writes_and_preserves_exact_query_output(
    tmp_path,
):
    wb = Workbook(tmp_path)
    table(wb, "numbers", "n\n9007199254740993\n")

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(tmp_path),
                "serve",
                "--tools",
                "query",
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["query"]
            schema = tools.tools[0].inputSchema
            assert set(schema["required"]) == {"inputs", "sql"}
            assert schema["properties"]["allow_historical"] == {"type": "boolean"}
            invalid = await session.call_tool("query", {"sql": "SELECT 1"})
            assert invalid.isError
            failed = await session.call_tool(
                "save", {"name": "hidden-write", "content": "No"}
            )
            assert failed.isError
            assert wb.find("hidden-write") == []
            result = await session.call_tool(
                "query",
                {
                    "inputs": ["numbers"],
                    "sql": "SELECT * FROM input1",
                },
            )
            assert not result.isError
            assert result.structuredContent["rows"] == [["9007199254740993"]]
            assert result.structuredContent["integer_string_columns"] == [
                {"index": 0, "sql_type": "BIGINT"}
            ]
            assert json.loads(result.content[0].text) == result.structuredContent
            nullable = await session.call_tool(
                "query",
                {
                    "inputs": ["numbers"],
                    "sql": "SELECT ? AS missing, ? AS count, ? AS label, ? AS enabled FROM input1",
                    "parameters": [None, 3, "exact", True],
                },
            )
            assert not nullable.isError
            assert nullable.structuredContent["rows"] == [[None, 3, "exact", True]]

    asyncio.run(run())


def test_delete_name_removes_every_version_and_frees_blobs(tmp_path):
    wb = Workbook(tmp_path)
    first = note(wb, "draft", "one")
    note(wb, "draft", "two", expected_version=1)
    other = note(wb, "other", "one")  # shares the blob of draft v1
    with pytest.raises(ValueError, match="expected_version"):
        wb.delete("draft")
    result = wb.delete("draft", expected_version=2)
    assert [row["version"] for row in result["removed"]] == [1, 2]
    assert result["blobs_removed"] == 1 and result["bytes_freed"] == len(b"two")
    assert not (tmp_path / "blobs" / hashlib.sha256(b"two").hexdigest()).exists()
    assert (tmp_path / "blobs" / hashlib.sha256(b"one").hexdigest()).exists()
    assert wb.find("draft") == []
    with pytest.raises(ValueError, match="Unknown artifact"):
        wb.inspect(first["artifact_id"])
    assert wb.inspect(other["artifact_id"], 10)["excerpt"] == "one"
    assert note(wb, "draft", "three")["version"] == 3
    with pytest.raises(ValueError, match="Unknown artifact"):
        wb.delete("missing", expected_version=1)


def test_delete_refuses_pinned_and_current_versions(tmp_path):
    wb = Workbook(tmp_path)
    source = note(wb, "source", "v1")
    note(wb, "report", "pins v1", dependencies=[source["artifact_id"]])
    current = note(wb, "source", "v2", expected_version=1)
    with pytest.raises(ValueError, match="Still pinned by report v1"):
        wb.delete(source["artifact_id"])
    with pytest.raises(ValueError, match="current version"):
        wb.delete(current["artifact_id"])
    with pytest.raises(ValueError, match="Still pinned"):
        wb.delete("source", expected_version=2)
    assert wb.inspect("source")["version"] == 2
    wb.delete("report", expected_version=1)
    result = wb.delete(source["artifact_id"])
    assert result["removed"] == [
        {"artifact_id": source["artifact_id"], "name": "source", "version": 1}
    ]
    spliced = wb.inspect("source")
    assert spliced["version"] == 2 and spliced["previous_id"] is None
    assert spliced["is_latest"] and not spliced["stale_inputs"]


def test_prune_runs_to_a_fixed_point_and_respects_pins(tmp_path):
    wb = Workbook(tmp_path)
    table(wb, "bars", "n\n1\n")
    query(wb, ["bars"], "SELECT count(*) AS c FROM input1", save_as="count")
    table(wb, "bars", "n\n1\n2\n", expected_version=1)
    refresh(wb, "count")
    oldest = note(wb, "scratch", "a")
    note(wb, "scratch", "b", expected_version=1)
    note(wb, "scratch", "c", expected_version=2)

    def removed(result):
        return {(row["name"], row["version"]) for row in result["removed"]}

    preview = wb.prune(dry_run=True)
    assert preview["dry_run"] and preview["blobs_removed"] == 4
    assert removed(preview) == {
        ("count", 1),
        ("bars", 1),
        ("scratch", 1),
        ("scratch", 2),
    }
    assert wb.inspect(oldest["artifact_id"])["version"] == 1
    assert len(list((tmp_path / "blobs").iterdir())) == 7

    assert removed(wb.prune(keep=2)) == {("scratch", 1)}
    final = wb.prune()
    assert removed(final) == {("count", 1), ("bars", 1), ("scratch", 2)}
    assert final["blobs_removed"] == 3 and not final["dry_run"]
    assert len(list((tmp_path / "blobs").iterdir())) == 3
    scratch = wb.inspect("scratch")
    assert scratch["version"] == 3 and scratch["previous_id"] is None
    count = wb.inspect("count")
    assert count["version"] == 2 and not count["stale_inputs"]
    assert wb.prune()["removed"] == []
    assert refresh(wb, "count")["rows"] == [[2]]
    assert removed(wb.prune()) == {("count", 2)}
    with pytest.raises(ValueError, match="at least one"):
        wb.prune(keep=0)


def test_resume_previews_every_pinned_artifact(tmp_path):
    wb = Workbook(tmp_path)
    pinned = [note(wb, f"part/{i}", f"piece {i}")["artifact_id"] for i in range(7)]
    checkpoint(wb, "part/handoff", dependencies=pinned)
    resumed = wb.resume("part/handoff")
    assert [item["artifact_id"] for item in resumed["artifacts"]] == pinned
    assert resumed["checkpoint"]["dependencies"] == pinned


def test_settings_bound_queries_and_indexing(tmp_path):
    with pytest.raises(ValueError, match="query_memory"):
        Settings(query_memory="lots")
    with pytest.raises(ValueError, match="index_bytes"):
        Settings(index_bytes=10)
    small = Workbook(
        tmp_path / "small",
        Settings(index_bytes=1024, max_preview_rows=2, query_threads=1),
    )
    body = "x" * 2000 + " needle"
    note(small, "haystack", body)
    assert small.find("needle") == []
    assert small.find("haystack")[0]["name"] == "haystack"
    wide = Workbook(tmp_path / "wide")
    note(wide, "haystack", body)
    assert wide.find("needle")[0]["name"] == "haystack"

    table(small, "rows", "n\n1\n2\n3\n")
    clipped = query(small, ["rows"], "SELECT n FROM input1 ORDER BY n", limit=10)
    assert clipped["rows"] == [[1], [2]] and clipped["preview_truncated"]
    threads = query(
        small, ["rows"], "SELECT current_setting('threads') AS t FROM input1 LIMIT 1"
    )
    assert threads["rows"] == [[1]]


def test_workspool_env_vars_take_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSPOOL_HOME", raising=False)
    monkeypatch.setenv("AGENT_WORKBOOK_HOME", str(tmp_path / "old"))
    assert default_root() == tmp_path / "old"
    monkeypatch.setenv("WORKSPOOL_HOME", str(tmp_path / "new"))
    assert default_root() == tmp_path / "new"


def test_import_and_delete_tools_are_opt_in(tmp_path):
    workspace = tmp_path / "workbook"
    wb = Workbook(workspace)
    parquet = tmp_path / "bars.parquet"
    duckdb.sql("SELECT 1 AS n UNION ALL SELECT 2").write_parquet(str(parquet))
    image = tmp_path / "chart.png"
    image.write_bytes(b"\x89PNG\x00\xff")

    def params(*extra):
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "agent_workbook.cli", "--workspace", str(workspace), "serve"]
            + list(extra),
        )

    async def run():
        async with (
            stdio_client(params()) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == {"save", "find", "inspect", "resume", "query", "refresh"}
            hidden = await session.call_tool("import", {"path": str(parquet)})
            assert hidden.isError
            hidden = await session.call_tool("delete", {"ref": "anything"})
            assert hidden.isError

        async with (
            stdio_client(params("--tools", "import", "query", "delete")) as (r, w),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["query", "import", "delete"]
            relative = await session.call_tool("import", {"path": "bars.parquet"})
            assert relative.isError
            missing = await session.call_tool(
                "import", {"path": str(tmp_path / "absent.csv")}
            )
            assert missing.isError

            imported = await session.call_tool(
                "import", {"path": str(parquet), "name": "replay/bars"}
            )
            assert not imported.isError
            assert imported.structuredContent["name"] == "replay/bars"
            assert imported.structuredContent["version"] == 1
            bars = wb.inspect("replay/bars")
            assert bars["kind"] == "table"
            assert bars["media_type"] == "application/vnd.apache.parquet"
            assert bars["source"] == "bars.parquet"
            total = await session.call_tool(
                "query",
                {
                    "inputs": ["replay/bars"],
                    "sql": "SELECT sum(n) AS total FROM input1",
                },
            )
            assert total.structuredContent["rows"] == [[3]]

            native = await session.call_tool("import", {"path": str(image)})
            assert not native.isError
            chart = wb.inspect("chart", 40)
            assert chart["kind"] == "file" and chart["media_type"] == "image/png"
            assert "excerpt" not in chart
            again = await session.call_tool("import", {"path": str(image)})
            assert again.isError  # version 1 exists; expected_version is required
            again = await session.call_tool(
                "import", {"path": str(image), "expected_version": 1}
            )
            assert again.structuredContent["version"] == 2

            refused = await session.call_tool("delete", {"ref": "replay/bars"})
            assert refused.isError
            removed = await session.call_tool(
                "delete", {"ref": "replay/bars", "expected_version": 1}
            )
            assert not removed.isError
            assert removed.structuredContent["removed"][0]["name"] == "replay/bars"
            assert removed.structuredContent["blobs_removed"] == 1
            assert wb.find("replay") == []

    asyncio.run(run())


def test_cli_query_delete_and_prune(tmp_path):
    workspace = tmp_path / "workbook"
    source = tmp_path / "bars.csv"
    source.write_text("n,price\n1,5\n2,15\n")

    def cli(*args):
        completed = subprocess.run(
            [sys.executable, "-m", "agent_workbook.cli", "--workspace", str(workspace)]
            + list(args),
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    assert cli("import", str(source), "--name", "replay/bars")["version"] == 1
    result = cli(
        "query",
        "SELECT sum(price)::BIGINT AS total FROM input1 WHERE n >= ?",
        "--input",
        "replay/bars",
        "--params",
        "[1]",
        "--save-as",
        "replay/total",
    )
    assert result["rows"] == [[20]] and result["saved"]["version"] == 1
    source.write_text("n,price\n1,25\n")
    cli("import", str(source), "--name", "replay/bars", "--expected-version", "1")
    assert cli("inspect", "replay/total")["stale_inputs"] is True
    assert cli("prune", "--dry-run")["removed"] == []  # bars v1 is still pinned
    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(workspace),
            "delete",
            "replay/total",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1 and "expected_version" in failed.stderr
    assert (
        cli("delete", "replay/total", "--expected-version", "1")["blobs_removed"] == 1
    )
    assert [row["version"] for row in cli("prune")["removed"]] == [1]
    assert cli("find", "replay")[0]["version"] == 2


@pytest.mark.parametrize("operation", ["delete", "prune"])
def test_failed_catalog_commit_preserves_every_blob(tmp_path, monkeypatch, operation):
    wb = Workbook(tmp_path)
    first = note(wb, "draft", "one")
    current = note(wb, "draft", "two", expected_version=1)
    real_connect = wb.connect

    @contextmanager
    def failed_commit():
        with real_connect() as db:
            yield db
            raise sqlite3.OperationalError("simulated catalog commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(wb, "connect", failed_commit)
        with pytest.raises(sqlite3.OperationalError, match="catalog commit failure"):
            if operation == "delete":
                wb.delete("draft", expected_version=2)
            else:
                wb.prune()

    assert wb.inspect(first["artifact_id"], 100)["excerpt"] == "one"
    latest = wb.inspect(current["artifact_id"], 100)
    assert latest["excerpt"] == "two"
    assert latest["previous_id"] == first["artifact_id"]


@pytest.mark.parametrize("operation", ["delete", "prune"])
def test_partial_file_cleanup_is_reported_and_retryable(
    tmp_path, monkeypatch, operation
):
    wb = Workbook(tmp_path)
    note(wb, "draft", "one")
    note(wb, "draft", "two", expected_version=1)
    note(wb, "draft", "three", expected_version=2)
    survivor = note(wb, "retained", "keep this evidence")
    real_unlink = os.unlink
    attempts = []

    def fail_second(path, *args, **kwargs):
        if Path(path).parent == wb.blobs:
            attempts.append(Path(path))
            if len(attempts) == 2:
                raise PermissionError("simulated second file cleanup failure")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("agent_workbook.store.os.unlink", fail_second)
        result = (
            wb.delete("draft", expected_version=3)
            if operation == "delete"
            else wb.prune()
        )

    assert result["cleanup_pending"] is True
    assert len(result["gc_errors"]) == 1
    assert not result["gc_errors_truncated"]
    assert result["blobs_removed"] == len(result["removed"]) - 1
    assert attempts[1].exists()
    for removed in result["removed"]:
        with pytest.raises(ValueError, match="Unknown artifact"):
            wb.inspect(removed["artifact_id"])
    if operation == "prune":
        assert wb.inspect("draft", 100)["excerpt"] == "three"
    assert wb.inspect(survivor["artifact_id"], 100)["excerpt"] == "keep this evidence"
    retry = wb.prune()
    assert retry["blobs_removed"] == 1
    assert not retry.get("cleanup_pending", False)
    assert not attempts[1].exists()


def test_cleanup_rechecks_blob_reused_after_catalog_commit(tmp_path, monkeypatch):
    wb = Workbook(tmp_path)
    note(wb, "draft", "shared content")
    real_collect = wb._collect_garbage
    replacement = None

    def interleave_new_save():
        nonlocal replacement
        # This connection can write only after the deletion transaction commits.
        replacement = note(Workbook(tmp_path), "draft", "shared content")
        return real_collect()

    monkeypatch.setattr(wb, "_collect_garbage", interleave_new_save)
    result = wb.delete("draft", expected_version=1)
    assert replacement["version"] == 2
    assert result["blobs_removed"] == 0
    assert wb.inspect(replacement["artifact_id"], 100)["excerpt"] == "shared content"


def test_recreated_name_rejects_stale_writes_and_deletes_after_reopen(tmp_path):
    wb = Workbook(tmp_path)
    original = note(wb, "draft", "session A original")
    wb.delete("draft", expected_version=1)
    replacement = note(Workbook(tmp_path), "draft", "session B replacement")
    assert replacement["version"] == 2
    with pytest.raises(ValueError, match="version 2"):
        note(wb, "draft", "session A stale write", expected_version=original["version"])
    with pytest.raises(ValueError, match="version 2"):
        wb.delete("draft", expected_version=original["version"])
    assert wb.inspect("draft", 100)["excerpt"] == "session B replacement"
    wb.delete("draft", expected_version=2)
    third = note(Workbook(tmp_path), "draft", "third generation")
    assert third["version"] == 3
    assert wb.inspect(third["artifact_id"])["previous_id"] is None


def test_existing_catalog_seeds_persistent_version_counters(tmp_path):
    wb = Workbook(tmp_path)
    first = note(wb, "draft", "one")
    note(wb, "draft", "two", expected_version=1)
    # Reproduce the earlier catalog schema without the counter table/triggers.
    with wb.connect() as db:
        db.execute("DROP TRIGGER artifacts_version_guard")
        db.execute("DROP TRIGGER artifacts_version_record")
        db.execute("DROP TABLE name_versions")
    upgraded = Workbook(tmp_path)
    assert upgraded.inspect(first["artifact_id"], 100)["excerpt"] == "one"
    upgraded.delete("draft", expected_version=2)
    assert note(Workbook(tmp_path), "draft", "new draft")["version"] == 3


def test_database_rejects_version_reuse_from_an_older_writer(tmp_path):
    wb = Workbook(tmp_path)
    note(wb, "draft", "one")
    template = note(wb, "template", "other content")
    wb.delete("draft", expected_version=1)
    with wb.connect() as db:
        row = list(
            db.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?",
                (template["artifact_id"],),
            ).fetchone()
        )
        row[0], row[1], row[2] = "wb_legacy_writer", "draft", 1
        with pytest.raises(sqlite3.IntegrityError, match="versions must advance"):
            db.execute(
                "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    assert wb.find("draft") == []
    assert wb.inspect(template["artifact_id"], 100)["excerpt"] == "other content"


def test_cleanup_scan_failure_keeps_committed_deletion_retryable(tmp_path, monkeypatch):
    wb = Workbook(tmp_path)
    saved = note(wb, "draft", "retained on disk until retry")
    metadata = wb.inspect(saved["artifact_id"])

    def failed_scan(db):
        raise PermissionError("simulated directory scan failure")

    with monkeypatch.context() as patch:
        patch.setattr(wb, "_garbage", failed_scan)
        result = wb.delete("draft", expected_version=1)
    assert result["cleanup_pending"]
    assert result["blobs_removed"] == 0
    assert (wb.blobs / metadata["sha256"]).exists()
    with pytest.raises(ValueError, match="Unknown artifact"):
        wb.inspect(saved["artifact_id"])
    assert wb.prune()["blobs_removed"] == 1
