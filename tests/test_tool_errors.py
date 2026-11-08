"""Argument errors teach valid shapes without exposing submitted values."""

import asyncio
import copy
import json
import sys
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.tools.base import Tool
from pydantic import BaseModel, ConfigDict

from agent_workbook.server import TOOL_NAMES, server_for
from agent_workbook.store import Workbook
from agent_workbook.tool_errors import argument_error


@pytest.fixture
def registered(tmp_path):
    book = Workbook(tmp_path)
    app = server_for(book, tools=list(TOOL_NAMES))
    approver = server_for(book, tools=["approve"], role="approver")
    return {
        tool.name: tool
        for server in (app, approver)
        for tool in server._tool_manager.list_tools()
    }


@pytest.mark.parametrize("wrong", ["artifact_id", "refs", "artifact_ids"])
def test_inspect_unknown_field_has_exact_actionable_shape(registered, wrong):
    error = argument_error(registered["inspect"], {wrong: "private-artifact-value"})
    assert error == {
        "error": f"inspect has no field {wrong}",
        "accepts": {
            "ref": "string | string[]",
            "excerpt_bytes": "integer",
            "offset": "integer",
            "include_recipe": "boolean",
            "full": "boolean",
        },
    }
    assert "private-artifact-value" not in json.dumps(error)
    assert len(json.dumps(error)) < 250


@pytest.mark.parametrize(
    ("name", "args", "message"),
    [
        ("query", {"sql": "secret SQL"}, "query requires inputs"),
        (
            "inspect",
            {"ref": {"password": "secret"}},
            "inspect.ref must be string | string[]",
        ),
        ("inspect", {"ref": "note", "offset": {}}, "inspect.offset must be integer"),
        ("claim", {"resource": "task:x", "ttl": True}, "claim.ttl must be integer"),
        ("inspect", [], "inspect arguments must be object"),
        ("session_status", {"extra": "secret"}, "session_status has no field extra"),
    ],
)
def test_missing_type_and_empty_parameter_models(registered, name, args, message):
    error = argument_error(registered[name], args)
    assert error["error"] == message
    assert not any(
        value in json.dumps(error) for value in ("secret", "https://", "input_value")
    )


def task_arguments():
    return {
        "name": "review",
        "objective": "Check totals",
        "sources": {"spend": "source"},
        "steps": [{"name": "total", "inputs": ["spend"], "sql": "SELECT 1"}],
    }


@pytest.mark.parametrize("failure", ["unknown", "missing", "type", "bound", "enum"])
def test_nested_task_steps(registered, failure):
    args = task_arguments()
    step = args["steps"][0]
    if failure == "unknown":
        step["input"] = step.pop("inputs")
        expected = "task_define.steps[0] has no field input"
    elif failure == "missing":
        del step["inputs"]
        expected = "task_define.steps[0] requires inputs"
    elif failure == "type":
        step["inputs"] = "secret-source"
        expected = (
            "task_define.steps[0].inputs must be string[] (items >= 1, items <= 8)"
        )
    elif failure == "bound":
        step["name"] = "secret" * 10
        expected = (
            "task_define.steps[0].name must be string (length >= 1, length <= 40)"
        )
    else:
        step["kind"] = "private-value"
        expected = 'task_define.steps[0].kind must be "sql" | "worker"'
    original = copy.deepcopy(args)
    error = argument_error(registered["task_define"], args)
    assert args == original
    assert error["error"] == expected
    assert error["accepts"]["steps[].inputs"] == "string[]"
    assert error["accepts"]["steps[].approval"] == "boolean"
    assert error["accepts"]["sources"] == "object<string, string>"
    assert "secret" not in json.dumps(error)
    assert "private-value" not in json.dumps(error)


def test_ignored_nested_checkpoint_extras_are_rejected(registered):
    args = {
        "name": "checkpoint",
        "checkpoint": {
            "objective": "O",
            "verified": "V",
            "next_step": "N",
            "extra": "secret",
        },
    }
    # The SDK's default extra=ignore currently accepts this: the helper must not.
    registered["save"].fn_metadata.arg_model.model_validate(args)
    error = argument_error(registered["save"], args)
    assert error["error"] == "save.checkpoint has no field extra"
    assert error["accepts"]["checkpoint.next_step"] == "string"


def test_arbitrary_dictionaries_remain_open_and_private_keys_are_not_echoed(registered):
    args = task_arguments()
    args["steps"][0]["when"] = {"free key": {"unknown": "allowed"}}
    args["sources"] = {"arbitrary source": "source"}
    args["csv_types"] = {"free alias": {"free column": "INTEGER"}}
    assert argument_error(registered["task_define"], args) is None
    assert (
        argument_error(
            registered["remember"],
            {"kind": "decision", "data": {"arbitrary": [1, {"password": "secret"}]}},
        )
        is None
    )
    args["sources"] = {"private-secret-key": []}
    error = argument_error(registered["task_define"], args)
    assert error["error"] == "task_define.sources[*] must be string"
    assert "private-secret-key" not in json.dumps(error)


def test_sdk_preparse_and_coercion_are_preserved_without_handler_execution():
    calls = []

    def sample(items: list[str], count: int = 1):
        calls.append((items, count))
        return count

    tool = Tool.from_function(sample)
    args = {"items": '["a","b"]', "count": "2"}
    before = copy.deepcopy(args)
    assert argument_error(tool, args) is None
    assert calls == [] and args == before
    assert asyncio.run(tool.run(args)) == 2
    assert calls == [(["a", "b"], 2)]


def test_preparsed_nested_model_unknowns(registered):
    args = task_arguments()
    args["steps"][0]["unexpected"] = "secret"
    args["steps"] = json.dumps(args["steps"])
    assert (
        argument_error(registered["task_define"], args)["error"]
        == "task_define.steps[0] has no field unexpected"
    )


class OpenModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    known: str


class TypedOpenModel(BaseModel):
    model_config = ConfigDict(extra="allow")
    __pydantic_extra__: dict[str, int]
    known: str


class NestedA(BaseModel):
    a: int


class NestedB(BaseModel):
    b: int


class ChoiceA(BaseModel):
    value: NestedA


class ChoiceB(BaseModel):
    value: NestedB


def test_explicit_extra_allow_model_is_not_closed():
    def sample(value: OpenModel, data: dict[str, Any]):
        return value

    tool = Tool.from_function(sample)
    assert (
        argument_error(
            tool,
            {
                "value": {"known": "ok", "extra": 2},
                "data": {"anything": {"else": True}},
            },
        )
        is None
    )


def test_typed_extra_allow_fields_remain_validated_by_sdk():
    def sample(value: TypedOpenModel):
        return value

    tool = Tool.from_function(sample)
    assert argument_error(tool, {"value": {"known": "ok", "extra": 2}}) is None
    assert (
        argument_error(tool, {"value": {"known": "ok", "extra": []}})["error"]
        == "sample.value[*] must be integer"
    )


@pytest.mark.parametrize("key", ["a", "b"])
def test_union_model_alternatives_keep_their_nested_fields(key):
    def sample(value: ChoiceA | ChoiceB):
        return value

    tool = Tool.from_function(sample)
    args = {"value": {"value": {key: 1}}}
    tool.fn_metadata.arg_model.model_validate(args)
    assert argument_error(tool, args) is None
    assert argument_error(tool, {"value": {"value": {"unknown": 1}}}) is not None


def _example(schema, definitions):
    if "$ref" in schema:
        return _example(definitions[schema["$ref"].split("/")[-1]], definitions)
    if "anyOf" in schema:
        return _example(schema["anyOf"][0], definitions)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "object":
        return {
            key: _example(value, definitions)
            for key, value in schema.get("properties", {}).items()
        }
    if kind == "array":
        return [_example(schema.get("items", {}), definitions)] * max(
            1, schema.get("minItems", 0)
        )
    if kind == "string":
        return "x" * max(1, schema.get("minLength", 0))
    if kind in {"integer", "number"}:
        return max(1, schema.get("minimum", 0))
    if kind == "boolean":
        return False
    return None


def test_every_registered_tool_preserves_all_schema_fields(registered):
    # Include optional fields and nested properties, without executing handlers.
    assert set(registered) == set(TOOL_NAMES)
    for tool in registered.values():
        schema = tool.parameters
        args = _example(schema, schema.get("$defs", {}))
        assert argument_error(tool, args) is None, tool.name
        args["unknown_field"] = "secret"
        error = argument_error(tool, args)
        assert error["error"] == f"{tool.name} has no field unknown_field"
        assert schema["properties"].keys() <= error["accepts"].keys()


def test_large_secret_input_is_not_rendered(registered):
    secret = "sk-proj-private" * 10000
    error = argument_error(registered["inspect"], {"ref": {"secret": secret}})
    encoded = json.dumps(error)
    assert len(encoded) < 300
    assert "sk-proj" not in encoded


def error_payload(result):
    assert result.isError
    assert len(result.content) == 1 and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert set(payload) == {"error", "accepts"}
    assert "https://" not in result.content[0].text
    assert "input_value" not in result.content[0].text
    return payload


@pytest.mark.parametrize("lazy", [False, True])
def test_dispatch_validation_records_failure_and_prevents_save(tmp_path, lazy):
    from agent_workbook.continuity import recent
    from agent_workbook.observability import metrics

    book = Workbook(tmp_path)
    app = server_for(
        book,
        profile="runtime",
        tools=["save", "inspect"],
        experimental_lazy=lazy,
        collect_metrics=True,
    )

    async def call(name, args):
        return await app.call_tool(
            "workspool" if lazy else name,
            {"tool": name, "arguments": args} if lazy else args,
        )

    invalid = asyncio.run(
        call("save", {"name": "must-not-exist", "content": "private-body", "typo": 1})
    )
    assert error_payload(invalid)["error"] == "save has no field typo"
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect("must-not-exist")
    invalid = asyncio.run(call("inspect", {"artifact_id": "private-id"}))
    assert error_payload(invalid)["error"] == "inspect has no field artifact_id"
    records = recent(book, limit=10)
    assert len(records) == 2
    for record in records:
        assert record["episode"]["verified"] is False
        assert record["episode"]["error"]
        assert "private-body" not in json.dumps(record)
    groups = {group["tool"]: group for group in metrics(book)["groups"]}
    assert groups["save"]["calls"] == groups["save"]["errors"] == 1
    assert groups["inspect"]["calls"] == groups["inspect"]["errors"] == 1
    assert app.capture_errors == app.metrics_errors == 0


def test_facade_outer_shape_errors_are_compact(tmp_path):
    app = server_for(Workbook(tmp_path), tools=["inspect"], experimental_lazy=True)
    extra = asyncio.run(app.call_tool("workspool", {"tool": "inspect", "typo": True}))
    assert error_payload(extra)["error"] == "workspool has no field typo"
    missing = asyncio.run(app.call_tool("workspool", {"arguments": {}}))
    assert error_payload(missing)["error"] == "workspool requires tool"
    wrong = asyncio.run(
        app.call_tool("workspool", {"tool": "inspect", "arguments": []})
    )
    assert error_payload(wrong)["error"] == "workspool.arguments must be object | null"


@pytest.mark.parametrize("encoded", [False, True])
def test_lazy_json_preparse_cannot_bypass_argument_dependent_capability(
    tmp_path, encoded
):
    book = Workbook(tmp_path)
    book.save_text("n\n1\n", name="input", kind="table", media_type="text/csv")
    app = server_for(
        book, tools=["query"], experimental_lazy=True, capabilities=["read"]
    )
    arguments = {
        "inputs": ["input"],
        "sql": "SELECT * FROM input1",
        "save_as": "forbidden",
    }
    with pytest.raises((PermissionError, ToolError), match="artifact:write"):
        asyncio.run(
            app.call_tool(
                "workspool",
                {
                    "tool": "query",
                    "arguments": json.dumps(arguments) if encoded else arguments,
                },
            )
        )
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect("forbidden")


@pytest.mark.parametrize(
    "field,value", [("role", "writer"), ("capabilities", ["artifact:write"])]
)
def test_lazy_json_preparse_preserves_identity_field_rejection(tmp_path, field, value):
    book = Workbook(tmp_path)
    app = server_for(
        book, tools=["query"], experimental_lazy=True, capabilities=["read"]
    )
    args = {
        "inputs": ["missing"],
        "sql": "SELECT 1",
        "save_as": "forbidden",
        field: value,
    }
    with pytest.raises((ValueError, ToolError), match="Identity and role fields"):
        asyncio.run(
            app.call_tool("workspool", {"tool": "query", "arguments": json.dumps(args)})
        )
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect("forbidden")


@pytest.mark.parametrize("lazy", [False, True])
def test_real_stdio_validation_and_successful_correction(tmp_path, lazy):
    args = [
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(tmp_path),
        "serve",
        "--tools",
        "inspect",
        "save",
        "task_define",
        "--response-content",
        "text",
    ]
    if lazy:
        args += ["--experimental-lazy"]

    async def run():
        params = StdioServerParameters(command=sys.executable, args=args)
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()

            async def call(name, arguments):
                return await client.call_tool(
                    "workspool" if lazy else name,
                    {"tool": name, "arguments": arguments} if lazy else arguments,
                )

            for wrong in ("artifact_id", "refs", "artifact_ids"):
                invalid = await call("inspect", {wrong: "private-id"})
                assert (
                    error_payload(invalid)["error"] == f"inspect has no field {wrong}"
                )
                assert "private-id" not in invalid.content[0].text
            invalid = await call("inspect", {"ref": {"password": "private-value"}})
            assert (
                error_payload(invalid)["error"]
                == "inspect.ref must be string | string[]"
            )
            assert "private-value" not in invalid.content[0].text
            task = task_arguments()
            task["steps"][0]["inputs"] = "private-source"
            invalid = await call("task_define", task)
            assert error_payload(invalid)["error"].startswith(
                "task_define.steps[0].inputs must be"
            )
            saved = await call("save", {"name": "corrected", "content": "exact"})
            assert not saved.isError
            valid = await call("inspect", {"ref": "corrected", "excerpt_bytes": 100})
            assert not valid.isError
            assert "exact" in valid.content[0].text

    asyncio.run(run())
