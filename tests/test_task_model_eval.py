"""Exercise live-eval accounting and real tool behavior without API access."""

import asyncio
import copy
import importlib.util
import json
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from agent_workbook.server import server_for
from agent_workbook.store import Workbook

SPEC = importlib.util.spec_from_file_location(
    "task_model_eval", Path(__file__).parents[1] / "benchmarks/task_model_eval.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)
SOURCE = Path(__file__).parents[1]


def response(output):
    return {
        "id": "resp_private_identifier",
        "model": "simulated",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    }


def call(name, arguments, identifier):
    return {
        "type": "function_call",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": identifier,
    }


def answer(value):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(value)}],
    }


PLAN = {
    "name": "desk_review",
    "objective": "Compute desk review totals and cite current policy.",
    "sources": {"trades": "trades", "limits": "limits", "policy": "policy"},
    "steps": [
        {
            "name": "trade_total",
            "inputs": ["trades"],
            "sql": "SELECT sum(price_cents * quantity)::BIGINT AS trade_notional_cents FROM input1",
        },
        {
            "name": "limit_total",
            "inputs": ["limits"],
            "sql": "SELECT sum(allocation_cents)::BIGINT AS desk_limit_cents FROM input1",
        },
        {
            "name": "combined",
            "inputs": ["trade_total", "limit_total"],
            "sql": "SELECT input1.trade_notional_cents, input2.desk_limit_cents, input2.desk_limit_cents - input1.trade_notional_cents AS headroom_cents FROM input1 CROSS JOIN input2",
        },
    ],
    "searches": ["review_code"],
}
BASE_SQL = (
    "WITH a AS (SELECT sum(price_cents * quantity)::BIGINT AS trade_notional_cents FROM input1), "
    "b AS (SELECT sum(allocation_cents)::BIGINT AS desk_limit_cents FROM input2) "
    "SELECT a.trade_notional_cents, b.desk_limit_cents, b.desk_limit_cents-a.trade_notional_cents AS headroom_cents "
    "FROM a CROSS JOIN b"
)


class ScriptedProvider:
    """Oracle-backed responses test the harness, not model reasoning quality."""

    def __init__(self, wrong=None, request_budget=36):
        self.requests = 0
        self.request_budget = request_budget
        self.wrong = wrong
        self.turns = defaultdict(int)
        self.payloads = []

    async def create(self, payload):
        if self.requests >= self.request_budget:
            raise bench.shared.RequestLimit("request_budget_exhausted")
        self.requests += 1
        self.payloads.append(copy.deepcopy(payload))
        arm = (
            "candidate"
            if any(tool["name"] == "task_define" for tool in payload["tools"])
            else "baseline"
        )
        user = next(
            item for item in reversed(payload["input"]) if item.get("role") == "user"
        )
        phase = re.match(r"Phase: (\w+)\.", user["content"])[1]
        turn = self.turns[arm, phase]
        self.turns[arm, phase] += 1
        identifier = f"{arm}_{phase}_{turn}"
        if arm == "candidate" and phase == "cold" and turn == 0:
            return response([call("task_define", PLAN, identifier)])
        if arm == "candidate" and phase == "cold" and turn == 1:
            return response([call("task_run", {"name": "desk_review"}, identifier)])
        if turn == 0:
            if arm == "candidate":
                arguments = {"name": "desk_review"}
                if phase == "warm":
                    outputs = "\n".join(
                        item.get("output", "")
                        for item in payload["input"]
                        if item.get("type") == "function_call_output"
                    )
                    arguments["base_run"] = re.findall(
                        r'"run_id"\s*:\s*"(run_[a-f0-9]+)"', outputs
                    )[-1]
                tool = "task_run" if phase == "changed" else "task_status"
                return response([call(tool, arguments, identifier)])
            if phase == "warm":
                return response(
                    [call("inspect", {"ref": ["desk_review", "policy"]}, identifier)]
                )
            query = (
                call(
                    "query",
                    {
                        "inputs": ["trades", "limits"],
                        "sql": BASE_SQL,
                        "save_as": "desk_review",
                    },
                    identifier,
                )
                if phase == "cold"
                else call("refresh", {"ref": "desk_review"}, identifier)
                if phase == "changed"
                else call(
                    "query",
                    {"inputs": ["desk_review"], "sql": "SELECT * FROM input1"},
                    identifier,
                )
            )
            return response(
                [
                    query,
                    call(
                        "search",
                        {"text": "review_code", "refs": ["policy"]},
                        identifier + "_policy",
                    ),
                ]
            )
        value = bench.expected(phase == "changed")
        if self.wrong == (arm, phase):
            value = dict(value, headroom_cents=-999)
        return response([answer(value)])


@asynccontextmanager
async def local_connection(source, workspace, names):
    app = server_for(
        Workbook(workspace),
        tools=names,
        response_format="adaptive",
        response_content="text",
    )
    definitions = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in await app.list_tools()
    ]
    yield app, definitions, app.instructions


def evaluate(tmp_path, provider, monkeypatch):
    def no_key(*args, **kwargs):
        raise AssertionError("offline evaluation must not read a key")

    monkeypatch.setattr(bench.shared, "load_key", no_key)
    return asyncio.run(
        bench.evaluate(provider, tmp_path, SOURCE, connector=local_connection)
    )


def test_real_tools_all_phases_usage_and_context_reset_are_auditable(
    tmp_path, monkeypatch
):
    provider = ScriptedProvider()
    report = evaluate(tmp_path, provider, monkeypatch)
    assert report["all_correct"], [
        (row["phase"], row["arm"], row["failure"]) for row in report["cases"]
    ]
    assert len(report["cases"]) == 8 and report["usage_complete"]
    assert report["provider_requests_attempted"] == provider.requests == 17
    assert report["usage"]["input_tokens"] == 1700
    assert report["usage"]["output_tokens"] == 340
    assert report["usage"]["cached_input_tokens"] == 170
    assert report["usage"]["cache_write_tokens"] is None
    assert report["arms"]["candidate"]["task_tool_calls"] == 5
    for phase in bench.PHASES:
        cases = [case for case in report["cases"] if case["phase"] == phase]
        assert cases[0]["prompt"] == cases[1]["prompt"]
        assert cases[0]["expected"] == cases[1]["expected"]
        assert all(
            case["source_integrity"] and not case["unretained_receipts"]
            for case in cases
        )
    for arm in bench.ARMS:
        payloads = [
            payload
            for payload in provider.payloads
            if (any(tool["name"] == "task_define" for tool in payload["tools"]))
            == (arm == "candidate")
        ]
        warm = next(
            payload
            for payload in payloads
            if any(
                item.get("role") == "user"
                and item.get("content", "").startswith("Phase: warm.")
                for item in payload["input"]
            )
        )
        reset = next(
            payload
            for payload in payloads
            if any(
                item.get("role") == "user"
                and item.get("content", "").startswith("Phase: reset.")
                for item in payload["input"]
            )
        )
        assert any(item.get("type") == "function_call_output" for item in warm["input"])
        assert len(reset["input"]) == 1
    assert "resp_private_identifier" not in json.dumps(report)
    assert report["fixture_sha256"]["initial"] != report["fixture_sha256"]["changed"]


def test_incorrect_answer_cost_is_retained(tmp_path, monkeypatch):
    report = evaluate(
        tmp_path, ScriptedProvider(wrong=("candidate", "cold")), monkeypatch
    )
    failed = next(
        case
        for case in report["cases"]
        if case["arm"] == "candidate" and case["phase"] == "cold"
    )
    assert not report["all_correct"] and report["complete"]
    assert (
        failed["failure"] == "incorrect_answer"
        and failed["usage"]["input_tokens"] == 300
    )
    assert report["usage"]["input_tokens"] == 1700


def test_global_request_limit_keeps_unattempted_trials_as_failures(
    tmp_path, monkeypatch
):
    report = evaluate(tmp_path, ScriptedProvider(request_budget=1), monkeypatch)
    assert not report["all_correct"] and not report["complete"]
    assert len(report["cases"]) == 8 and report["provider_requests_attempted"] == 1
    assert any(
        case["failure"] == "request_budget_exhausted" for case in report["cases"]
    )
    assert any(not case["attempted"] for case in report["cases"])
    assert report["usage"]["input_tokens"] == 100


def test_receipt_requires_an_actually_retained_complete_brief():
    conversation = bench.RetainedConversation(None)
    conversation.begin(reset=True)

    class Client:
        def __init__(self, value):
            self.value = value

        async def call_tool(self, name, arguments):
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(self.value))]
            )

    receipt = {
        "delivery": "unchanged",
        "base_run": "run_a",
        "requires_retained_base": True,
    }
    bad = bench.EvidenceClient(Client(receipt), conversation)
    asyncio.run(bad.call_tool("task_status", {}))
    assert bad.unretained_receipts == 1
    full = bench.EvidenceClient(
        Client(
            {
                "run_id": "run_a",
                "sources": {},
                "steps": [],
                "citations": [],
                "omitted": [],
                "brief_complete": True,
            }
        ),
        conversation,
    )
    asyncio.run(full.call_tool("task_status", {}))
    good = bench.EvidenceClient(Client(receipt), conversation)
    asyncio.run(good.call_tool("task_status", {}))
    assert good.unretained_receipts == 0
    conversation.begin(reset=True)
    after_reset = bench.EvidenceClient(Client(receipt), conversation)
    asyncio.run(after_reset.call_tool("task_status", {}))
    assert after_reset.unretained_receipts == 1


def test_step_limit_retains_completed_tool_outputs_without_publishing_reasoning():
    conversation = bench.RetainedConversation(None)
    conversation.begin(reset=True)
    conversation.last_history = [
        {"type": "reasoning", "encrypted_content": "private-state"},
        call("query", {}, "a"),
    ]
    conversation.cache_write_reported = [False]
    result = {
        "tool_calls": [{"call_id": "a", "output": "exact evidence"}],
        "provider_requests": [
            {"response_id": "private-id", "usage": {"cache_write_tokens": 0}}
        ],
        "usage": {"cache_write_tokens": 0},
    }
    conversation.finish(result)
    assert conversation.history[-1] == {
        "type": "function_call_output",
        "call_id": "a",
        "output": "exact evidence",
    }
    published = bench.public_result(result)
    assert "private-state" not in json.dumps(
        published
    ) and "private-id" not in json.dumps(published)
    assert result["usage"]["cache_write_tokens"] is None


@pytest.mark.parametrize("arguments", [{"max_steps": 7}, {"max_output_tokens": 1601}])
def test_limits_are_checked_before_fixture_or_provider_work(tmp_path, arguments):
    with pytest.raises(ValueError):
        asyncio.run(bench.evaluate(None, tmp_path, SOURCE, **arguments))
    assert not list(tmp_path.iterdir())


def test_prior_diagnostic_is_retained_and_method_change_explicit(tmp_path):
    path = tmp_path / "initial.json"
    previous = {
        "requested_model": "gpt-6-luna",
        "max_steps_per_trial": 4,
        "usage": {"input_tokens": 100},
        "cases": [
            {
                "phase": "cold",
                "arm": "baseline",
                "success": False,
                "failure": "step_limit",
                "tool_calls": ["private trace"],
            }
        ],
    }
    path.write_text(json.dumps(previous))
    before = path.read_bytes()
    summary = bench.prior_diagnostic(path)
    assert path.read_bytes() == before
    assert summary["max_steps_per_trial"] == 4
    assert summary["cases"][0]["failure"] == "step_limit"
    assert "private trace" not in json.dumps(summary)
    assert "changed methodology" in summary["note"]
