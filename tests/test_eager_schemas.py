"""Offline schema-size guard and preservation of typed call contracts."""

import asyncio
import json

import pytest

from agent_workbook.budget import token_counter
from agent_workbook.server import _slim_schema, server_for
from agent_workbook.store import Workbook

EVAL_TOOLS = (
    "query",
    "inspect",
    "find",
    "save",
    "refresh",
    "task_define",
    "task_run",
    "task_status",
    "verify",
    "provenance",
)


def test_ten_tool_schema_token_snapshot(tmp_path):
    pytest.importorskip("tiktoken")
    app = server_for(
        Workbook(tmp_path), tools=list(EVAL_TOOLS), response_content="text"
    )
    listed = asyncio.run(app.list_tools())
    definitions = [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema,
            "strict": False,
        }
        for tool in listed
    ]
    encoded = json.dumps(definitions, ensure_ascii=False, separators=(",", ":"))
    count = token_counter("o200k_base")(encoded)
    assert count < 1000, f"Ten typed tools grew to {count} tokens (limit: 999)"
    assert count == 998, "Update the reviewed token snapshot when schemas change"
    assert {tool.name for tool in listed} == set(EVAL_TOOLS)


def test_slim_schema_preserves_named_fields_types_bounds_and_scalar_nulls():
    schema = {
        "title": "Arguments",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string", "default": "untitled"},
            "default": {"type": "integer", "default": 7, "minimum": 1},
            "optional": {
                "anyOf": [{"type": "string", "maxLength": 10}, {"type": "null"}],
                "default": None,
            },
            "ref": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}, "maxItems": 16},
                ]
            },
            "values": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "integer"},
                        {"type": "number"},
                        {"type": "boolean"},
                        {"type": "null"},
                    ]
                },
            },
        },
        "required": ["title", "default"],
        "additionalProperties": False,
    }
    original = json.loads(json.dumps(schema))
    slim = _slim_schema(schema)
    assert schema == original
    assert slim["required"] == ["title", "default"]
    assert slim["additionalProperties"] is False
    assert slim["properties"] == {
        "title": {"type": "string"},
        "default": {"type": "integer", "minimum": 1},
        "optional": {"type": "string", "maxLength": 10},
        "ref": {
            "type": ["string", "array"],
            "items": {"type": "string"},
            "maxItems": 16,
        },
        "values": {
            "type": "array",
            "items": {"type": ["string", "number", "boolean", "null"]},
        },
    }
    assert (
        "null"
        in _slim_schema(schema, nullable=True)["properties"]["optional"]["anyOf"][1][
            "type"
        ]
    )


def test_nested_task_and_checkpoint_fields_remain_typed(tmp_path):
    app = server_for(
        Workbook(tmp_path), tools=["task_define", "save"], response_content="text"
    )
    tools = {tool.name: tool.inputSchema for tool in asyncio.run(app.list_tools())}
    steps = tools["task_define"]["properties"]["steps"]["items"]
    checkpoint = tools["save"]["properties"]["checkpoint"]
    assert steps["required"] == ["name", "inputs"]
    assert steps["additionalProperties"] is False
    assert set(steps["properties"]) == {
        "name",
        "inputs",
        "sql",
        "parameters",
        "kind",
        "worker",
        "when",
        "approval",
    }
    assert steps["properties"]["sql"] == {
        "maxLength": 6000,
        "minLength": 1,
        "type": "string",
    }
    assert steps["properties"]["kind"] == {"enum": ["sql", "worker"], "type": "string"}
    assert steps["properties"]["parameters"]["items"]["type"] == [
        "string",
        "number",
        "boolean",
        "null",
    ]
    assert checkpoint["required"] == ["objective", "verified", "next_step"]
    assert checkpoint["properties"]["verified"] == {"maxLength": 600, "type": "string"}
    # Compact advertisements never replace the SDK's defaults or validation.
    raw = app._tool_manager.get_tool("save")
    parsed = raw.fn_metadata.arg_model.model_validate(
        {"name": "note", "checkpoint": None}
    )
    assert parsed.kind == "note" and parsed.checkpoint is None


def test_shared_and_recursive_definitions_remain_references():
    schema = {
        "$defs": {
            "Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}
        },
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/Node"}, "b": {"$ref": "#/$defs/Node"}},
    }
    assert _slim_schema(schema) == schema


@pytest.mark.parametrize("profile", ["compatible", "efficient", "agent", "runtime"])
def test_every_profile_advertises_typed_tools_by_default(tmp_path, profile):
    app = server_for(Workbook(tmp_path), profile=profile)
    tools = asyncio.run(app.list_tools())
    assert "workspool" not in {tool.name for tool in tools}
    for tool in tools:
        assert tool.inputSchema["type"] == "object"
        assert tool.description.endswith(".") and tool.description.count(".") == 1
    app.finish_session()


def test_experimental_lazy_warns_with_measured_failure_and_report(tmp_path, caplog):
    app = server_for(Workbook(tmp_path), experimental_lazy=True)
    assert [tool.name for tool in asyncio.run(app.list_tools())] == ["workspool"]
    assert "1/20" in caplog.text and "17/20" in caplog.text
    assert "benchmarks/APPROVAL_MODEL.md" in caplog.text
