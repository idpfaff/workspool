"""Actual-model harness tests use a simulated provider, never an API key/network."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

SPEC = importlib.util.spec_from_file_location(
    "model_benchmark", Path(__file__).parents[1] / "benchmarks/model_efficiency.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)
DEFINITIONS = [
    {
        "name": "inspect",
        "description": "Actual description",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    }
]


def response(output, *, status="completed", input_tokens=100, output_tokens=20):
    return {
        "id": "resp_simulated",
        "model": "simulated-model",
        "status": status,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }


def call(identifier, arguments='{"ref":"source"}', name="inspect"):
    return {
        "type": "function_call",
        "call_id": identifier,
        "name": name,
        "arguments": arguments,
    }


def message(value):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(value)}],
    }


class FakeProvider:
    def __init__(self, replies):
        self.replies, self.payloads = list(replies), []

    async def create(self, payload):
        self.payloads.append(copy.deepcopy(payload))
        return self.replies.pop(0)


class FakeTools:
    def __init__(self, error=False):
        self.calls, self.active, self.maximum, self.error = [], 0, 0, error

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        await asyncio.sleep(0)
        self.active -= 1
        return CallToolResult(
            content=[TextContent(type="text", text="exact evidence")],
            isError=self.error,
        )


def solve(provider, client=None, **kwargs):
    return asyncio.run(
        bench.solve(
            provider,
            client or FakeTools(),
            DEFINITIONS,
            "server instructions",
            "question",
            {"value": 7},
            **kwargs,
        )
    )


def test_parallel_tool_calls_replay_reasoning_and_preserve_provider_usage():
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-state",
    }
    first = [reasoning, call("a"), call("b", '{"ref":"second"}')]
    provider = FakeProvider(
        [response(first), response([message({"value": 7})], input_tokens=250)]
    )
    tools = FakeTools()
    result = solve(provider, tools)
    assert result["success"] and tools.maximum == 2
    assert result["usage"] == {
        "input_tokens": 350,
        "output_tokens": 40,
        "cached_input_tokens": 20,
        "cache_write_tokens": 0,
        "reasoning_tokens": 10,
    }
    assert len(result["tool_calls"]) == 2
    next_payload = provider.payloads[1]
    assert next_payload["input"][1:4] == first
    assert [item["call_id"] for item in next_payload["input"][4:]] == ["a", "b"]
    assert all(
        item["type"] == "function_call_output" for item in next_payload["input"][4:]
    )
    assert next_payload["store"] is False
    assert next_payload["include"] == ["reasoning.encrypted_content"]
    assert next_payload["model"] == "gpt-6-luna"
    assert next_payload["tools"][0]["strict"] is False
    assert next_payload["tools"][0]["parameters"] == DEFINITIONS[0]["inputSchema"]
    assert "server instructions" in next_payload["instructions"]
    assert "opaque-state" not in json.dumps(result)


def test_invalid_tool_calls_return_errors_and_cannot_invoke_hidden_tools():
    provider = FakeProvider(
        [
            response(
                [
                    call("bad", name="delete"),
                    call("json", arguments="not-json"),
                    call("ok"),
                ]
            ),
            response([message({"value": 7})]),
        ]
    )
    tools = FakeTools()
    result = solve(provider, tools)
    assert result["success"]
    assert tools.calls == [("inspect", {"ref": "source"})]
    assert sum(item["error"] is not None for item in result["tool_calls"]) == 2
    assert len(provider.payloads[1]["input"]) == 7


def test_tool_errors_and_answers_without_evidence_are_not_wins():
    provider = FakeProvider([response([call("a")]), response([message({"value": 7})])])
    result = solve(provider, FakeTools(error=True))
    assert not result["success"] and result["failure"] == "no_tool_evidence"
    assert result["tool_calls"][0]["error"] == "tool_error"
    provider = FakeProvider([response([message({"value": 7})])])
    assert solve(provider)["failure"] == "no_tool_evidence"


def test_step_limit_and_incomplete_outputs_preserve_cost_as_failures():
    provider = FakeProvider([response([call("a")]), response([call("b")])])
    result = solve(provider, max_steps=2)
    assert not result["success"] and result["failure"] == "step_limit"
    assert result["usage"]["input_tokens"] == 200 and len(result["tool_calls"]) == 2
    result = solve(
        FakeProvider([response([], status="incomplete", output_tokens=2000)])
    )
    assert result["failure"] == "provider_response_incomplete"
    assert result["usage"]["output_tokens"] == 2000


def test_cache_write_tokens_are_separate_optional_provider_usage():
    first = response([call("a")])
    first["usage"]["input_tokens_details"]["cache_write_tokens"] = 42
    result = solve(FakeProvider([first, response([message({"value": 7})])]))
    assert result["success"]
    assert result["usage"]["cache_write_tokens"] == 42
    assert result["usage"]["cached_input_tokens"] == 20
    assert result["usage"]["input_tokens"] == 200
    assert result["provider_requests"][0]["usage"]["cache_write_tokens"] == 42
    assert result["provider_requests"][1]["usage"]["cache_write_tokens"] == 0


@pytest.mark.parametrize(
    "items", [[call("same"), call("same")], [call(str(i)) for i in range(9)]]
)
def test_invalid_or_excessive_call_batches_execute_nothing(items):
    tools = FakeTools()
    result = solve(FakeProvider([response(items)]), tools)
    assert result["failure"] == "tool_call_limit_or_invalid_ids"
    assert tools.calls == []


def test_missing_usage_aborts_instead_of_reporting_free_tokens():
    result = solve(FakeProvider([{"status": "completed", "output": [], "usage": {}}]))
    assert (
        result["abort_run"] and result["failure"] == "provider_missing_or_invalid_usage"
    )


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_are_sanitized_and_never_retried(status):
    secret = "sk-test-" + "x" * 24
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "secret " + secret}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = bench.ResponsesProvider(client, secret)
            result = await bench.solve(provider, FakeTools(), DEFINITIONS, "", "q", {})
            assert (
                result["abort_run"] and result["failure"] == f"provider_http_{status}"
            )
            assert secret not in json.dumps(result)
            assert provider.requests == 1

    asyncio.run(run())
    assert len(requests) == 1 and str(requests[0].url) == bench.API_URL


def test_shared_provider_request_budget_stops_before_extra_http_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response([call("a")]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = bench.ResponsesProvider(
                client, "sk-" + "x" * 30, request_budget=1
            )
            result = await bench.solve(provider, FakeTools(), DEFINITIONS, "", "q", {})
            assert (
                result["failure"] == "request_budget_exhausted" and result["abort_run"]
            )
            assert result["usage"]["input_tokens"] == 100
            assert provider.requests == 1

    asyncio.run(run())
    assert len(requests) == 1


def test_key_loading_never_evaluates_shell_or_accepts_ambiguous_files(tmp_path):
    key = "sk-test-" + "a" * 32
    assert bench.load_key(environ={"OPENAI_API_KEY": key}) == key
    path = tmp_path / ".env.local"
    path.write_text("OTHER=value\nexport OPENAI_API_KEY='" + key + "'\n")
    path.chmod(0o600)
    assert bench.load_key(path, environ={"OPENAI_API_KEY": "not-used"}) == key
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular"):
        bench.load_key(path)
    path.chmod(0o600)
    for content in (
        "OPENAI_API_KEY=$(touch injected)\n",
        "OPENAI_API_KEY=" + key + "\nOPENAI_API_KEY=" + key,
    ):
        path.write_text(content)
        with pytest.raises(ValueError) as error:
            bench.load_key(path)
        assert key not in str(error.value)
    assert not (tmp_path / "injected").exists()
    linked = tmp_path / "linked"
    linked.symlink_to(path)
    with pytest.raises(ValueError, match="private regular"):
        bench.load_key(linked)


def test_independent_answer_check_rejects_wrong_types_and_extra_facts():
    expected = {"version": 1, "stale_inputs": False}
    assert bench.correct_answer(json.dumps(expected), expected)
    assert bench.correct_answer("```json\n" + json.dumps(expected) + "\n```", expected)
    assert not bench.correct_answer('{"version":true,"stale_inputs":0}', expected)
    assert not bench.correct_answer(
        '{"version":1,"stale_inputs":false,"extra":1}', expected
    )


def test_synthetic_fixtures_have_repeatable_ids_prompts_and_independent_answers(
    tmp_path,
):
    for case in bench.CASES:
        left = bench.fixture(tmp_path / (case + "-left"), case)
        right = bench.fixture(tmp_path / (case + "-right"), case)
        assert left == right
        prompt, expected, hashes = left
        assert prompt and expected and hashes
        if case != "passages":
            assert expected["version"] == 2
            assert expected["artifact_id"] not in prompt
            assert "route-old" in prompt and "route-current" not in prompt


@pytest.mark.parametrize("arm", ["baseline", "candidate"])
def test_real_mcp_schema_and_text_replies_with_simulated_provider(tmp_path, arm):
    source = Path(__file__).parents[1]
    if arm == "baseline":
        source = tmp_path / "baseline"
        source.mkdir()
        bench.helpers().archive_baseline("v0.4.1", source)
    workspace = tmp_path / "workspace"
    prompt, expected, _ = bench.fixture(workspace, "sparse")
    names = ["search", "inspect", "diff"]
    if arm == "candidate":
        names.append("sync")
        base = bench.Workbook(workspace).inspect("operations/runbook")["previous_id"]
        request = call(
            "read", json.dumps({"ref": "operations/runbook", "base": base}), "sync"
        )
    else:
        request = call(
            "read",
            '{"text":"INSTRUCTION=","refs":["operations/runbook"],"context_bytes":64}',
            "search",
        )
    provider = FakeProvider([response([request]), response([message(expected)])])

    async def run():
        async with bench.connection(source, workspace, names) as (
            client,
            definitions,
            instructions,
        ):
            return await bench.solve(
                provider, client, definitions, instructions, prompt, expected
            )

    result = asyncio.run(run())
    assert result["success"]
    assert expected["artifact_id"] in result["tool_calls"][0]["output"]
    assert "route-current" in result["tool_calls"][0]["output"]
    assert {tool["name"] for tool in provider.payloads[0]["tools"]} == set(names)


@pytest.mark.parametrize(
    "kwargs", [{"max_steps": 0}, {"max_steps": 7}, {"max_output_tokens": 2001}]
)
def test_loop_limits_are_validated_before_provider_calls(kwargs):
    provider = FakeProvider([])
    with pytest.raises(ValueError):
        solve(provider, **kwargs)
    assert provider.payloads == []
