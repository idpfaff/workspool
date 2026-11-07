"""Public task workflows, exact CSV ingestion, and budgets over real MCP."""

import asyncio
import json
import subprocess
import sys
from decimal import Decimal

import duckdb
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.query import query
from agent_workbook.render import decode
from agent_workbook.server import server_for
from agent_workbook.store import Workbook
from agent_workbook.tasks import define_task, run_task, status


def fixture(path):
    book = Workbook(path)
    book.save_text(
        "v\n9007199254740993.000000000001\n2.000000000002\n",
        name="numbers",
        kind="table",
        media_type="text/csv",
    )
    book.save_text(
        "The watchlist requires exact volume totals. Source text: 😀.\n",
        name="policy",
        kind="note",
        media_type="text/plain",
    )
    return book


def plan():
    return {
        "objective": "Return exact volume totals and the watchlist policy.",
        "sources": {"bars": "numbers", "policy": "policy"},
        "steps": [
            {
                "name": "total",
                "inputs": ["bars"],
                "sql": "SELECT sum(v) AS volume FROM input1",
            }
        ],
        "csv_types": {"bars": {"v": "DECIMAL(38,12)"}},
        "searches": ["watchlist"],
    }


def test_typed_csv_preserves_fraction_and_resumes_with_same_types(tmp_path):
    book = fixture(tmp_path)
    define_task(book, "volume", **plan())
    first = run_task(book, "volume")
    assert first["state"] == "complete", first
    assert first["steps"][0]["result"]["rows"] == [["9007199254740995.000000000003"]]
    recovered = run_task(Workbook(tmp_path), "volume", resume_from=first["run_id"])
    assert recovered["steps"][0]["cache"]["hit"]
    assert (
        recovered["steps"][0]["result"]["rows"] == first["steps"][0]["result"]["rows"]
    )


def test_retained_base_receipt_requires_identical_current_evidence(tmp_path):
    book = fixture(tmp_path)
    define_task(book, "volume", **plan())
    first = run_task(book, "volume")
    receipt = run_task(book, "volume", base_run=first["run_id"], max_bytes=1200)
    assert receipt["delivery"] == "unchanged" and receipt["requires_retained_base"]
    assert receipt["current"] and not receipt["brief_complete"]
    assert "steps" not in receipt and "citations" not in receipt
    full = status(Workbook(tmp_path), "volume")
    assert full["citations"] == first["citations"]
    assert full["steps"][0]["result"] == first["steps"][0]["result"]
    assert "steps" in status(book, "volume", base_run="unknown")
    book.save_text(
        "Updated watchlist policy.",
        name="policy",
        kind="note",
        media_type="text/plain",
        expected_version=1,
    )
    stale = status(book, "volume", base_run=first["run_id"])
    assert stale["state"] == "superseded" and "steps" in stale
    changed = run_task(book, "volume", base_run=first["run_id"])
    assert "steps" in changed and changed["citations"] != first["citations"]
    assert changed["steps"][0]["cache"]["hit"]
    with pytest.raises(ValueError, match="max_bytes"):
        status(book, "volume", base_run=changed["run_id"], max_bytes=255)


def test_recurring_task_ids_follow_updates_unless_explicitly_pinned(tmp_path):
    book = fixture(tmp_path)
    definition = plan()
    definition["sources"] = {
        alias: book.inspect(ref)["artifact_id"]
        for alias, ref in definition["sources"].items()
    }
    live = define_task(book, "live", **definition)
    pinned = define_task(book, "pinned", **definition, pin_sources=True)
    assert live["source_mode"] == "latest" and live["sources"]["bars"] == "numbers"
    assert pinned["sources"]["bars"] == definition["sources"]["bars"]
    old = run_task(book, "live")
    book.save_text(
        "v\n42.000000000001\n",
        name="numbers",
        kind="table",
        media_type="text/csv",
        expected_version=1,
    )
    current = run_task(book, "live")
    assert current["current"] and current["steps"][0]["result"]["rows"] == [
        ["42.000000000001"]
    ]
    historical = run_task(book, "pinned")
    assert historical["state"] == "superseded" and not historical["current"]
    assert historical["steps"][0]["result"]["rows"] == old["steps"][0]["result"]["rows"]
    resumed = run_task(book, "live", resume_from=old["run_id"])
    assert resumed["steps"][0]["result"]["rows"] == old["steps"][0]["result"]["rows"]


def test_missing_evidence_source_rejected_before_plan_is_saved(tmp_path):
    book = fixture(tmp_path)
    definition = plan() | {"sources": {"bars": "numbers"}}
    with pytest.raises(ValueError, match="add the policy"):
        define_task(book, "missing-policy", **definition)
    with pytest.raises(ValueError, match="Unknown"):
        book.inspect("_tasks/missing-policy/definition")


@pytest.mark.parametrize(
    "types",
    [{"unknown": {"v": "DECIMAL(38,12)"}}, {"bars": []}, {"bars": {1: "BIGINT"}}],
)
def test_invalid_csv_schema_rejected_before_write(tmp_path, types):
    book = fixture(tmp_path)
    definition = plan() | {"csv_types": types}
    with pytest.raises(ValueError, match="CSV types"):
        define_task(book, "invalid", **definition)
    assert not book.find("_tasks/")


def test_agent_profile_provenance_budgets_and_new_client(tmp_path):
    book = fixture(tmp_path)
    count = token_counter("cl100k_base")

    async def run():
        app = server_for(book, profile="agent", tokenizer="cl100k_base")
        definitions = await app.list_tools()
        assert [tool.name for tool in definitions] == ["workspool"]
        operations = definitions[0].inputSchema["properties"]["tool"]["enum"]
        assert {"task_define", "task_run", "task_status", "context"} <= set(operations)
        assert "import" not in operations and "delete" not in operations

        async def call(app, name, args):
            result = await app.call_tool("workspool", {"tool": name, "arguments": args})
            assert not result.isError, result.content
            assert result.structuredContent is None
            return result, decode(result.content)

        await call(app, "task_define", {"name": "volume", **plan()})
        raw, first = await call(app, "task_run", {"name": "volume", "max_tokens": 2000})
        assert count(serialized_result(raw)) <= 2000
        assert first["state"] == "complete" and first["current"]
        assert first["sources"]["bars"]["sha256"] == book.inspect("numbers")["sha256"]
        source = book.inspect("policy", excerpt_bytes=4096)["excerpt"].encode()
        hit = first["citations"][0]
        assert hit["excerpt"].encode() == source[hit["start_byte"] : hit["end_byte"]]
        fresh = server_for(Workbook(tmp_path), profile="agent", tokenizer="cl100k_base")
        _, resumed = await call(fresh, "task_status", {"name": "volume"})
        assert resumed["sources"] == first["sources"]
        assert resumed["citations"] == first["citations"]
        raw, context = await call(
            fresh,
            "context",
            {"query": "watchlist", "refs": ["policy"], "max_tokens": 600},
        )
        assert count(serialized_result(raw)) <= 600 and context["hits"]
        with pytest.raises(ToolError, match="Brief budget"):
            await fresh.call_tool(
                "workspool",
                {
                    "tool": "task_status",
                    "arguments": {"name": "volume", "max_tokens": 10},
                },
            )

    asyncio.run(run())


def test_cli_task_definition_run_status_and_context(tmp_path):
    fixture(tmp_path / "book")
    definition = tmp_path / "plan.json"
    definition.write_text(json.dumps(plan()))

    def cli(*args):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(tmp_path / "book"),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout)

    assert cli("task-define", "volume", str(definition))["version"] == 1
    first = cli("task-run", "volume")
    assert first["state"] == "complete"
    assert cli("task-status", "volume")["run_id"] == first["run_id"]
    assert cli("context", "watchlist", "--ref", "policy")["hits"]


def test_existing_saved_queries_preserve_large_integer_materialization(tmp_path):
    book = Workbook(tmp_path)
    book.save_text(
        "n\n9007199254740993\n2\n", name="large", kind="table", media_type="text/csv"
    )
    result = query(
        book, ["large"], "SELECT sum(n) AS total FROM input1", save_as="total"
    )
    assert result["rows"] == [["9007199254740995"]]
    saved = book.blobs / book.inspect(result["saved"]["artifact_id"])["sha256"]
    with duckdb.connect() as db:
        assert db.execute("SELECT * FROM read_parquet(?)", [str(saved)]).fetchone() == (
            Decimal(9007199254740995),
        )
    assert result["materialized_type_conversions"] == [
        {"column": "total", "source_type": "HUGEINT", "stored_type": "DECIMAL(38,0)"}
    ]
