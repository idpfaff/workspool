"""Reusable analytical plans through the same interfaces agents actually use."""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.render import decode
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


@asynccontextmanager
async def client_for(root, discovery, role="writer"):
    # This tiny test log is opened once before the asynchronous protocol exchange.
    with open(root.parent / "server-errors.log", "w") as errors:  # noqa: ASYNC230
        async with (
            stdio_client(
                StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "agent_workbook.cli",
                        "--workspace",
                        str(root),
                        "serve",
                        "--profile",
                        "runtime",
                        "--tool-discovery",
                        discovery,
                        "--agent-id",
                        "integration-agent",
                        "--role",
                        role,
                    ],
                ),
                errlog=errors,
            ) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            yield client


async def invoke(client, discovery, operation, arguments, *, error=False):
    result = await client.call_tool(
        "workspool" if discovery == "lazy" else operation,
        {"tool": operation, "arguments": arguments}
        if discovery == "lazy"
        else arguments,
    )
    assert bool(result.isError) == error, result.content
    return result if error else decode(result.content)


@pytest.mark.parametrize("discovery", ["eager", "lazy"])
def test_distill_discover_rebind_and_run_across_real_sessions(tmp_path, discovery):
    book = Workbook(tmp_path / "book")
    book.save_text("n\n2\n3\n", name="old", kind="table", media_type="text/csv")
    newer = book.save_text(
        "n\n7\n11\n", name="new", kind="table", media_type="text/csv"
    )

    async def run():
        async with client_for(book.root, discovery) as client:
            await invoke(
                client,
                discovery,
                "task_define",
                {
                    "name": "original",
                    "objective": "Sum recorded values",
                    "sources": {"data": "old"},
                    "csv_types": {"data": {"n": "BIGINT"}},
                    "steps": [
                        {
                            "name": "total",
                            "inputs": ["data"],
                            "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
                        }
                    ],
                },
            )
            completed = await invoke(
                client, discovery, "task_run", {"name": "original"}
            )
            assert completed["steps"][0]["result"]["rows"] == [[5]]
            saved = await invoke(
                client,
                discovery,
                "skill_distill",
                {
                    "name": "sum-values",
                    "task": "original",
                },
            )
            assert "package" not in saved
            skill_id = saved["artifact_id"]

        async with client_for(book.root, discovery) as client:
            found = await invoke(client, discovery, "skill_find", {"query": "sum"})
            assert [item["artifact_id"] for item in found["skills"]] == [skill_id]
            inspected = await invoke(
                client, discovery, "skill_inspect", {"ref": skill_id}
            )
            assert inspected["proof"] == "successful_execution"
            assert inspected["package"]["plan"]["steps"][0]["sql"].startswith(
                "SELECT sum"
            )
            defined = await invoke(
                client,
                discovery,
                "skill_use",
                {
                    "ref": skill_id,
                    "task_name": "reused",
                    "bindings": {"data": "new"},
                },
            )
            assert defined["source_mode"] == "pinned"
            assert defined["sources"] == {"data": newer["artifact_id"]}
            with book.connect() as db:
                assert not db.execute(
                    "SELECT 1 FROM task_runs WHERE name='reused'"
                ).fetchone()
            ticket = await invoke(
                client, discovery, "claim", {"resource": "task:reused"}
            )
            await invoke(client, discovery, "task_run", {"name": "reused"}, error=True)
            result = await invoke(
                client,
                discovery,
                "task_run",
                {
                    "name": "reused",
                    "claim_ticket": ticket,
                },
            )
            assert result["state"] == "complete"
            assert result["producer"]["identity"] == "integration-agent"
            assert result["steps"][0]["result"]["rows"] == [[18]]
            await invoke(
                client,
                discovery,
                "skill_use",
                {
                    "ref": skill_id,
                    "task_name": "missing",
                    "bindings": {},
                },
                error=True,
            )
        return skill_id

    skill_id = asyncio.run(run())
    with book.connect() as db:
        event = db.execute(
            "SELECT actor FROM coordination_events WHERE resource=? AND kind='artifact.created'",
            (skill_id,),
        ).fetchone()
        assert event[0] == "integration-agent"


@pytest.mark.parametrize("role", ["reader", "approver"])
def test_skill_mutations_are_not_available_to_readers_or_approvers(tmp_path, role):
    book = Workbook(tmp_path / role)

    async def run():
        async with client_for(book.root, "lazy", role) as client:
            listed = await client.list_tools()
            operations = listed.tools[0].inputSchema["properties"]["tool"]["enum"]
            assert {"skill_find", "skill_inspect"} <= set(operations)
            assert not {"skill_distill", "skill_use"} & set(operations)
            found = await invoke(client, "lazy", "skill_find", {})
            assert found["skills"] == []
            await invoke(
                client, "lazy", "skill_distill", {"name": "x", "task": "x"}, error=True
            )

    asyncio.run(run())


def test_skill_wire_budget_is_enforced_before_returning_full_plan(
    tmp_path, monkeypatch
):
    from agent_workbook import server, skills, tasks

    book = Workbook(tmp_path / "book")
    book.save_text("n\n2\n", name="numbers", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "sum",
        "Sum values",
        {"data": "numbers"},
        [
            {"name": "total", "inputs": ["data"], "sql": "SELECT sum(n) FROM input1"},
        ],
    )
    tasks.run_task(book, "sum")
    saved = skills.distill(book, "sum", "sum")
    monkeypatch.setattr(server, "token_counter", lambda _: len)
    app = server_for(book, profile="runtime", tokenizer="test-counter")

    async def run():
        from mcp.server.fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="exceeds token budget"):
            await app.call_tool(
                "workspool",
                {
                    "tool": "skill_inspect",
                    "arguments": {
                        "ref": saved["artifact_id"],
                        "max_tokens": 1,
                    },
                },
            )
        result = await app.call_tool(
            "workspool", {"tool": "skill_find", "arguments": {"max_tokens": 5000}}
        )
        assert (
            decode(result.content)["skills"][0]["artifact_id"] == saved["artifact_id"]
        )

    asyncio.run(run())


def test_cli_skill_reuse_requires_explicit_bindings(tmp_path):
    from agent_workbook import tasks

    book = Workbook(tmp_path / "book")
    book.save_text("n\n4\n", name="values", kind="table", media_type="text/csv")
    tasks.define_task(
        book,
        "sum",
        "Sum values",
        {"data": "values"},
        [
            {"name": "total", "inputs": ["data"], "sql": "SELECT sum(n) FROM input1"},
        ],
    )
    tasks.run_task(book, "sum")

    def cli(*arguments):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(book.root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    saved = cli("skill-distill", "sum", "--task", "sum")
    assert saved.returncode == 0, saved.stderr
    skill_id = json.loads(saved.stdout)["artifact_id"]
    bad = cli("skill-use", skill_id, "--task-name", "next", "--bindings", "{}")
    assert bad.returncode != 0
    good = cli(
        "skill-use", skill_id, "--task-name", "next", "--bindings", '{"data":"values"}'
    )
    assert good.returncode == 0, good.stderr
    assert json.loads(good.stdout)["source_mode"] == "pinned"
