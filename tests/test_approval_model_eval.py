"""Offline model-loop tests use actual tools and simulated provider responses."""

import asyncio
import copy
import importlib.util
import json
import re
import zipfile
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from agent_workbook.server import server_for
from agent_workbook.store import Workbook

SPEC = importlib.util.spec_from_file_location(
    "approval_bench", Path(__file__).parents[1] / "benchmarks/approval_model_eval.py"
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)
PLAN = {
    "name": "review_job",
    "objective": "Compute exact independent totals and require approval for publication",
    "sources": {"spend": "spend", "limits": "limits"},
    "steps": [
        {
            "name": "total",
            "inputs": ["spend"],
            "sql": "SELECT sum(unit_cents * quantity)::BIGINT AS total_cents FROM input1",
        },
        {
            "name": "budget",
            "inputs": ["limits"],
            "sql": "SELECT sum(approved_cents)::BIGINT AS budget_cents FROM input1",
        },
        {
            "name": "final",
            "inputs": ["total", "budget"],
            "sql": "SELECT total_cents,budget_cents,budget_cents-total_cents AS remaining_cents FROM input1 CROSS JOIN input2",
            "approval": True,
        },
    ],
}


def response(output):
    return {
        "id": "private_provider_id",
        "model": "simulated",
        "status": "completed",
        "output": output,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 10, "cache_write_tokens": 5},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }


def message(value):
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": json.dumps(value)}],
    }


class Scripted:
    """Oracle-backed responses test accounting/enforcement, not model skill."""

    def __init__(self, oracles, *, wrong_control=False):
        self.oracles, self.wrong_control = oracles, wrong_control
        self.requests, self.turns, self.payloads = 0, defaultdict(int), []

    async def create(self, payload):
        self.requests += 1
        self.payloads.append(copy.deepcopy(payload))
        user = next(
            item["content"]
            for item in reversed(payload["input"])
            if item.get("role") == "user"
        )
        identifier = re.match(r"Case ([a-z_]+-\d+)\.", user)[1]
        phase = (
            "followup"
            if "Actual external reviewer/source update receipt:" in user
            else "prepare"
        )
        candidate = payload["tools"][0]["name"] == "workspool"
        turn = self.turns[phase]
        self.turns[phase] += 1
        if phase == "prepare" and turn == 0:
            name, arguments = "task_define", PLAN
        elif phase == "prepare" and turn == 1 or phase == "followup" and turn == 0:
            name, arguments = "task_run", {"name": "review_job"}
        else:
            if phase == "prepare":
                value = {"state": "awaiting_approval"}
            else:
                allowed = identifier.startswith(("approved-", "handoff-"))
                value = {
                    "state": "complete" if allowed else "awaiting_approval",
                    **self.oracles[identifier],
                    "published_current": allowed,
                }
                if self.wrong_control and not candidate:
                    value["remaining_cents"] += 1
            return response([message(value)])
        if candidate:
            name, arguments = "workspool", {"tool": name, "arguments": arguments}
        return response(
            [
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": json.dumps(arguments),
                    "call_id": f"{phase}_{turn}",
                }
            ]
        )


@asynccontextmanager
async def local_connection(workspace, arm, identity, log):
    app = server_for(
        Workbook(workspace),
        profile="runtime",
        tools=list(bench.TOOLS),
        agent_id=identity,
        response_format="full",
        response_content="text",
        experimental_lazy=arm == "lazy_typed_errors",
    )
    yield (
        app,
        [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in await app.list_tools()
        ],
        app.instructions,
    )


def offline_baseline(tmp_path):
    """Simulated interface fixture; never claims to be an archived release."""
    app = server_for(
        Workbook(tmp_path / "interface-fixture"),
        tools=list(bench.TOOLS),
        profile="runtime",
        response_format="full",
        response_content="text",
    )
    definitions = asyncio.run(app.list_tools())
    return {
        "definitions": [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in definitions
        ],
        "instructions": "Offline baseline sentinel. " + app.instructions,
        "provenance": "simulated offline test only",
    }


def evaluate(
    tmp_path,
    monkeypatch,
    *,
    scenarios=bench.SCENARIOS,
    wrong=False,
    connector=local_connection,
):
    monkeypatch.setattr(
        bench.shared, "load_key", lambda *_: pytest.fail("offline test read a key")
    )
    cases = [
        {"id": f"{scenario}-8201", "scenario": scenario, "seed": 8201}
        for scenario in scenarios
    ]
    oracles = {
        case["id"]: bench.fixture(tmp_path / ("oracle-" + case["id"]), case)["oracle"]
        for case in cases
    }
    for case in cases:
        if case["scenario"] == "source_changed":
            oracles[case["id"]] = bench.fixture(
                tmp_path / ("oracle-" + case["id"]), case, changed=True
            )["oracle"]
    providers = []

    def factory():
        result = Scripted(oracles, wrong_control=wrong)
        providers.append(result)
        return result

    result = asyncio.run(
        bench.evaluate(
            tmp_path,
            factory,
            cases=cases,
            connector=connector,
            baseline_interface=offline_baseline(tmp_path),
        )
    )
    return result, providers


def test_all_scenarios_real_tools_and_genuine_reviewer_events(tmp_path, monkeypatch):
    report, providers = evaluate(tmp_path, monkeypatch)
    assert len(report["records"]) == 15
    assert all(row["success"] for row in report["records"]), [
        (
            row["case"],
            row.get("failure"),
            [(phase["failure"], phase["task_audit"]) for phase in row["phases"]],
        )
        for row in report["records"]
        if not row["success"]
    ]
    assert (
        report["summary"]["comparisons"]["eager_compact"]["both_correct_pairs"]["n"]
        == 5
    )
    assert all(row["usage_complete"] for row in report["records"])
    assert sum(provider.requests for provider in providers) == 75
    assert report["summary"]["arms"]["eager_baseline"]["usage"]["input_tokens"] == 2500
    for row in report["records"]:
        events = row["intervention"]["approval_events"]
        scenario = row["case"]["scenario"]
        assert len(events) == (
            0 if scenario == "refused" else 2 if scenario == "revoked" else 1
        )
        assert all(event["approver"] == "fixture-reviewer" for event in events)
        assert all(
            request.get("response_id") is None
            for phase in row["phases"]
            for request in phase["provider_requests"]
        )
    for provider in providers:
        follows = [
            payload
            for payload in provider.payloads
            if "Actual external reviewer"
            in next(
                item["content"]
                for item in reversed(payload["input"])
                if item.get("role") == "user"
            )
        ]
        user = follows[0]["input"]
        handoff = "handoff-" in next(
            item["content"] for item in reversed(user) if item.get("role") == "user"
        )
        assert sum(item.get("role") == "user" for item in user) == (1 if handoff else 2)


def test_wrong_model_answer_remains_failure_despite_real_approval(
    tmp_path, monkeypatch
):
    report, _ = evaluate(tmp_path, monkeypatch, scenarios=("approved",), wrong=True)
    control = next(row for row in report["records"] if row["arm"] == "eager_baseline")
    assert not control["success"] and control["enforcement_pass"]
    assert control["phases"][-1]["failure"] == "incorrect_answer"
    assert report["summary"]["comparisons"]["eager_compact"]["paired"]["n"] == 1
    assert (
        report["summary"]["comparisons"]["eager_compact"]["both_correct_pairs"]["n"]
        == 0
    )


def test_actual_stdio_mcp_with_simulated_provider(tmp_path, monkeypatch):
    report, _ = evaluate(
        tmp_path, monkeypatch, scenarios=("approved",), connector=bench.connection
    )
    assert all(row["success"] for row in report["records"])


def test_invalid_dag_gets_no_fabricated_approval(tmp_path):
    case = bench.declared_cases()[0]
    setup = bench.fixture(tmp_path, case)
    book = Workbook(tmp_path)
    malformed = copy.deepcopy(PLAN)
    malformed["steps"][1]["inputs"] = ["total"]
    malformed["steps"][1]["sql"] = "SELECT total_cents AS budget_cents FROM input1"
    bench.tasks.define_task(book, **malformed)
    bench.tasks.run_task(book, "review_job")
    audit = bench.audit_task(tmp_path, setup["sources"])
    assert not audit["valid_gated_plan"]
    _, receipt, allowed = bench.intervene(tmp_path, case, setup, audit)
    assert not allowed and not receipt["approval_events"]


def payload():
    return {
        "model": bench.MODEL,
        "max_output_tokens": bench.MAX_REPLY,
        "input": "fixture",
    }


def test_concurrent_request_reservation_cannot_oversubscribe():
    async def exercise():
        budget = bench.Budget(requests=1)
        results = await asyncio.gather(
            budget.reserve(payload()), budget.reserve(payload()), return_exceptions=True
        )
        assert sum(isinstance(item, bench.shared.RequestLimit) for item in results) == 1
        reservation = next(item for item in results if isinstance(item, tuple))
        await budget.finish(reservation, response([]))
        assert (
            budget.requests == 1 and budget.pending_input == budget.pending_output == 0
        )
        assert budget.input == 100 and budget.output == 20

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "bad",
    [
        {"usage": "invalid"},
        {"usage": {"input_tokens": 1, "output_tokens": 1, "input_tokens_details": [1]}},
        None,
    ],
)
def test_unknown_usage_consumes_reservation_and_stops(bad):
    async def exercise():
        budget = bench.Budget()
        reservation = await budget.reserve(payload())
        await budget.finish(reservation, bad)
        assert budget.stopped and budget.unknown_usage_requests == 1
        assert (budget.input, budget.output) == reservation
        with pytest.raises(bench.shared.RequestLimit):
            await budget.reserve(payload())

    asyncio.run(exercise())


def test_context_and_dollar_guards_precede_dispatch():
    async def exercise():
        with pytest.raises(bench.shared.RequestLimit):
            await bench.Budget().reserve(dict(payload(), input="x" * 100001))
        with pytest.raises(bench.shared.RequestLimit):
            await bench.Budget(usd=0.00001).reserve(payload())

    asyncio.run(exercise())


def test_provider_error_never_exposes_credential():
    secret = "sk-fixture-" + "z" * 30

    async def exercise():
        def handler(request):
            assert str(request.url) == bench.shared.API_URL
            assert request.headers["authorization"] == "Bearer " + secret
            return httpx.Response(403, text="credential " + secret)

        budget = bench.Budget()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = bench.GuardedProvider(
                bench.shared.ResponsesProvider(client, secret, 12), budget
            )
            with pytest.raises(bench.shared.ProviderFailure) as error:
                await provider.create(payload())
        assert secret not in str(error.value) and budget.stopped

    asyncio.run(exercise())


def test_unknown_usage_is_never_a_zero_cost_win():
    rows = [
        {
            "case": {"id": "case"},
            "arm": arm,
            "attempted": True,
            "success": False,
            "enforcement_pass": False,
            "phases": [],
            "usage": dict.fromkeys(
                bench.shared.METRICS, None if arm == "candidate" else 100
            ),
        }
        for arm in ("control", "candidate")
    ]
    summary = bench.summarize(rows)
    assert summary["paired"] == {"n": 0, "unmeasurable_pairs": 1}
    assert summary["arms"]["candidate"]["usage"]["input_tokens"] is None


def test_single_pair_does_not_report_a_zero_width_interval():
    rows = [
        {
            "case": {"id": "case"},
            "arm": arm,
            "attempted": True,
            "success": True,
            "enforcement_pass": True,
            "phases": [],
            "usage": dict.fromkeys(bench.shared.METRICS, 100),
        }
        for arm in ("control", "candidate")
    ]
    summary = bench.summarize(rows)
    for key in ("paired", "both_correct_pairs"):
        assert summary[key]["n"] == 1
        assert summary[key]["bootstrap_95_mean_difference"] is None
        assert summary[key]["uncertainty"] == "insufficient_samples"


def test_failed_context_teardown_retains_paid_phase_usage(tmp_path, monkeypatch):
    @asynccontextmanager
    async def broken(workspace, arm, identity, log):
        async with local_connection(workspace, arm, identity, log) as connected:
            yield connected
        raise RuntimeError("fixture teardown failure")

    report, _ = evaluate(
        tmp_path, monkeypatch, scenarios=("approved",), connector=broken
    )
    assert all(
        not row["success"] and row["usage"]["input_tokens"] == 300
        for row in report["records"]
    )
    assert all(
        len(row["phases"]) == 1 and row["usage_complete"] for row in report["records"]
    )


def test_frozen_interface_is_used_without_reconstructing_annotations(
    tmp_path, monkeypatch
):
    report, providers = evaluate(tmp_path, monkeypatch, scenarios=("approved",))
    baseline = report["baseline_interface"]
    payloads = [payload for provider in providers for payload in provider.payloads]
    historical = [
        p for p in payloads if "Offline baseline sentinel." in p["instructions"]
    ]
    assert len(historical) == 5
    assert all(
        p["tools"] == bench.shared.function_tools(baseline["definitions"])
        for p in historical
    )
    assert len(report["summary"]["comparisons"]) == 2
    assert not report["default_release_gate"]["passed"]  # Only one case, not 20.


def test_baseline_wheel_requires_exact_published_bytes(tmp_path):
    wheel = tmp_path / "fake.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("agent_workbook/server.py", "# plausible but unproven\n")
    with pytest.raises(ValueError, match="hash-pinned"):
        bench.extract_baseline(wheel, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()


def test_schema_count_scope_is_one_explicit_list(tmp_path):
    interface = offline_baseline(tmp_path)
    counts = bench.interface_counts(interface["definitions"], interface["instructions"])
    for encoding, values in counts.items():
        counter = bench.token_counter(encoding)
        assert values["instructions"] == counter(interface["instructions"])
        assert values["openai_function_list"] == counter(
            bench.compact(bench.shared.function_tools(interface["definitions"]))
        )
        assert set(values) == {
            "mcp_definition_list",
            "openai_function_list",
            "instructions",
        }


def test_default_gate_requires_all_cases_and_no_more_than_one_regression():
    records = [
        {
            "case": case,
            "arm": arm,
            "attempted": True,
            "usage_complete": True,
            "success": not (arm == "eager_compact" and index == 0),
            "enforcement_pass": True,
            "usage": dict.fromkeys(bench.shared.METRICS, 100),
        }
        for index, case in enumerate(bench.declared_cases())
        for arm in bench.ARMS
    ]
    report = {
        "records": records,
        "sources_unchanged": True,
        "summary": bench.summarize(records),
    }
    assert bench.release_gate(report)["passed"]
    next(row for row in records if row["arm"] == "eager_compact" and row["success"])[
        "success"
    ] = False
    report["summary"] = bench.summarize(records)
    assert not bench.release_gate(report)["passed"]
    report["sources_unchanged"] = False
    assert not bench.release_gate(report)["complete_evidence"]
    assert bench.protocol()["limits"]["requests"] == 20 * 3 * 2 * 6


def test_offline_preflight_never_reads_key(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(bench.shared, "load_key", lambda *_: pytest.fail("Key read"))
    monkeypatch.setattr(
        bench.httpx, "AsyncClient", lambda *_: pytest.fail("Network client")
    )

    async def capture(*_):
        return {"fixture": True}

    async def plan(*_):
        return {"state": "planned_not_run"}

    monkeypatch.setattr(bench, "capture_baseline", capture)
    monkeypatch.setattr(bench, "preflight", plan)
    args = SimpleNamespace(
        output=tmp_path / "planned", baseline_wheel=tmp_path / "wheel", execute=False
    )
    assert asyncio.run(bench.run(args))["state"] == "planned_not_run"


def test_exact_numeric_grading_requires_verified_integral_materialization(tmp_path):
    case = bench.declared_cases()[0]
    setup = bench.fixture(tmp_path, case)
    book = Workbook(tmp_path)
    plan = copy.deepcopy(PLAN)
    plan["steps"][0]["sql"] = (
        "SELECT sum(unit_cents * quantity)::DECIMAL(38,0) AS total_cents FROM input1"
    )
    plan["steps"][1]["sql"] = (
        "SELECT sum(approved_cents)::DECIMAL(38,0) AS budget_cents FROM input1"
    )
    bench.tasks.define_task(book, **plan)
    bench.tasks.run_task(book, bench.TASK)
    audit = bench.audit_task(tmp_path, setup["sources"])
    bench.intervene(tmp_path, case, setup, audit)
    bench.tasks.run_task(book, bench.TASK)
    audit = bench.audit_task(tmp_path, setup["sources"])
    assert all(type(value) is str for value in audit["final_result"]["rows"][0])
    assert bench.phase_audit_pass(audit, "followup", True, setup["oracle"], tmp_path)
    audit["final_result"]["rows"][0][0] = float(setup["oracle"]["total_cents"])
    assert not bench.phase_audit_pass(
        audit, "followup", True, setup["oracle"], tmp_path
    )
