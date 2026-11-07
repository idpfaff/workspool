"""Bounded actual-agent task evaluation; no API calls or key reads on import.

Two equally informed arms use the current checkout and identical synthetic data.
The baseline can persist an efficient SQL query/result; the candidate additionally
receives task tools. The model chooses its calls and pays its own cold setup.
Four phases test cold setup, retained-context freshness, a context reset, and
changed sources. Failures and non-adoption of task tools remain in the report.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from agent_workbook.render import decode
from agent_workbook.store import Workbook

SPEC = importlib.util.spec_from_file_location(
    "task_model_shared", Path(__file__).with_name("model_efficiency.py")
)
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)

PHASES = ("cold", "warm", "reset", "changed")
ARMS = ("baseline", "candidate")
BASE_TOOLS = ("query", "refresh", "save", "find", "inspect", "search")
TASK_TOOLS = ("task_define", "task_run", "task_status", "context")
TRADES = (("AAA", 100, 3), ("BBB", 250, 2), ("AAA", 110, 1))
LIMITS = (("primary", 1000), ("reserve", 500))
CHANGED_TRADES = (*TRADES, ("CCC", 200, 4))
CHANGED_LIMITS = (("primary", 1500), ("reserve", 900))
THRESHOLD = 600


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def expected(changed=False):
    trades = CHANGED_TRADES if changed else TRADES
    limits = CHANGED_LIMITS if changed else LIMITS
    notional = sum(price * quantity for _, price, quantity in trades)
    allocation = sum(amount for _, amount in limits)
    headroom = allocation - notional
    return {
        "trade_notional_cents": notional,
        "desk_limit_cents": allocation,
        "headroom_cents": headroom,
        "review_code": "MAPLE" if changed else "CEDAR",
        "requires_review": headroom < THRESHOLD,
    }


def fixture(workspace, *, changed=False):
    book = Workbook(workspace)
    trades = CHANGED_TRADES if changed else TRADES
    limits = CHANGED_LIMITS if changed else LIMITS
    bodies = {
        "trades": (
            "ticker,price_cents,quantity\n"
            + "".join(
                f"{ticker},{price},{quantity}\n" for ticker, price, quantity in trades
            )
        ),
        "limits": "bucket,allocation_cents\n"
        + "".join(f"{bucket},{amount}\n" for bucket, amount in limits),
        "policy": (
            "Current desk review policy.\n"
            f"review_code={'MAPLE' if changed else 'CEDAR'}\n"
            f"review_threshold_cents={THRESHOLD}\n"
            "Review is required exactly when headroom_cents is less than review_threshold_cents.\n"
        ),
    }
    for name, body in bodies.items():
        book.save_text(
            body,
            name=name,
            kind="note" if name == "policy" else "table",
            media_type="text/plain" if name == "policy" else "text/csv",
            summary=f"Synthetic desk review {name}.",
            expected_version=1 if changed else None,
        )
    return {
        name: hashlib.sha256(body.encode()).hexdigest() for name, body in bodies.items()
    }


def prompt(phase):
    phase_text = {
        "cold": (
            "Prepare and save reusable computation named desk_review for later turns. "
            "Choose the most efficient available tools; a saved SQL result/recipe or a task is acceptable."
        ),
        "warm": (
            "You retain the previous conversation. Reuse verified results where valid; verify freshness "
            "with tools before answering. Do not repeat work unnecessarily."
        ),
        "reset": (
            "This is a fresh conversation: you do not retain earlier messages or tool replies. "
            "The local workbook and any reusable desk_review work remain. Reopen the evidence "
            "and verify freshness; do not claim to retain a previous brief."
        ),
        "changed": (
            "The trades, limits, and policy sources have now been updated. You retain this conversation, "
            "but old answers are stale. Use current source versions and recompute dependent results."
        ),
    }[phase]
    return (
        f"Phase: {phase}. {phase_text}\n"
        "The workbook has two independent CSV sources: trades(ticker,price_cents,quantity) "
        "and limits(bucket,allocation_cents), plus a text source named policy. "
        "Compute trade_notional_cents = SUM(price_cents * quantity), desk_limit_cents = SUM(allocation_cents), "
        "and the dependent headroom_cents = desk_limit_cents - trade_notional_cents. "
        "Read the current review_code and review_threshold_cents from policy. "
        "requires_review is true exactly when headroom_cents < review_threshold_cents. "
        "Keep arithmetic exact. A single SQL query with independent aggregates is allowed; "
        "you need not materialize every intermediate. Persist reusable work without changing input sources. "
        "Return only JSON with trade_notional_cents, desk_limit_cents, headroom_cents, review_code, requires_review."
    )


class RetainedConversation:
    """Prepend actual prior Responses items; never flatten reasoning/tool history."""

    def __init__(self, provider):
        self.provider = provider
        self.history = []
        self.full_runs = set()

    def begin(self, *, reset):
        if reset:
            self.history = []
            self.full_runs.clear()
        self.prefix = list(self.history)
        self.last_history = list(self.prefix)
        self.cache_write_reported = []

    async def create(self, payload):
        request = dict(payload, input=[*self.prefix, *payload["input"]])
        response = await self.provider.create(request)
        output = response.get("output")
        if isinstance(output, list) and all(isinstance(item, dict) for item in output):
            self.last_history = [*request["input"], *output]
        details = (response.get("usage") or {}).get("input_tokens_details") or {}
        self.cache_write_reported.append("cache_write_tokens" in details)
        return response

    def finish(self, result):
        # At a step limit, solve has delivered the final tool outputs but has not
        # made another provider request. Preserve those outputs before resuming.
        outputs = {event["call_id"]: event["output"] for event in result["tool_calls"]}
        present = {
            item["call_id"]
            for item in self.last_history
            if item.get("type") == "function_call_output"
        }
        for item in list(self.last_history):
            identifier = item.get("call_id")
            if (
                item.get("type") == "function_call"
                and identifier in outputs
                and identifier not in present
            ):
                self.last_history.append(
                    {
                        "type": "function_call_output",
                        "call_id": identifier,
                        "output": outputs[identifier],
                    }
                )
                present.add(identifier)
        self.history = self.last_history
        for record, reported in zip(
            result["provider_requests"], self.cache_write_reported, strict=False
        ):
            if not reported:
                record["usage"]["cache_write_tokens"] = None
        if not all(self.cache_write_reported):
            result["usage"]["cache_write_tokens"] = None


class EvidenceClient:
    """An unchanged receipt is evidence only when its complete base is retained."""

    def __init__(self, client, conversation):
        self.client, self.conversation = client, conversation
        self.unretained_receipts = 0

    async def call_tool(self, name, arguments):
        result = await self.client.call_tool(name, arguments)
        if not result.isError:
            data = result.structuredContent
            if data is None:
                try:
                    data = decode(result.content)
                except (ValueError, TypeError, KeyError):
                    data = None
            if isinstance(data, dict):
                if (
                    data.get("delivery") == "unchanged"
                    and data.get("base_run") not in self.conversation.full_runs
                ):
                    self.unretained_receipts += 1
                if (
                    data.get("run_id")
                    and data.get("brief_complete") is True
                    and all(key in data for key in ("sources", "steps", "citations"))
                    and not data.get("omitted")
                ):
                    self.conversation.full_runs.add(data["run_id"])
        return result


def public_result(result):
    """Provider IDs and encrypted reasoning are not publication artifacts."""
    result = json.loads(json.dumps(result))
    for request in result["provider_requests"]:
        request.pop("response_id", None)
    for index, call in enumerate(result["tool_calls"], 1):
        call["call_id"] = f"call_{index}"
    return result


def totals(cases):
    result = {}
    for metric in shared.METRICS:
        values = [case["usage"][metric] for case in cases if case["attempted"]]
        result[metric] = None if any(value is None for value in values) else sum(values)
    return result


def skipped(phase, arm, reason):
    return {
        "phase": phase,
        "arm": arm,
        "attempted": False,
        "success": False,
        "failure": reason,
        "usage": dict.fromkeys(shared.METRICS, 0),
        "provider_requests": [],
        "tool_calls": [],
        "expected": expected(phase == "changed"),
    }


def source_hashes(source):
    files = [
        Path(__file__),
        Path(__file__).with_name("model_efficiency.py"),
        *sorted((source / "src/agent_workbook").glob("*.py")),
    ]
    return {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }


async def evaluate(
    provider,
    root,
    source,
    *,
    model="gpt-6-luna",
    max_steps=6,
    max_output_tokens=1600,
    connector=None,
):
    """Dependency-injected provider/connector keep all tests offline."""
    if type(max_steps) is not int or not 1 <= max_steps <= 6:
        raise ValueError("max_steps must be between 1 and 6")
    if type(max_output_tokens) is not int or not 128 <= max_output_tokens <= 1600:
        raise ValueError("max_output_tokens must be between 128 and 1600")
    connector = shared.connection if connector is None else connector
    prepared = root / "fixture"
    initial_hashes = fixture(prepared)
    workspaces = {arm: root / arm for arm in ARMS}
    for workspace in workspaces.values():
        shutil.copytree(prepared, workspace)
    conversations = {arm: RetainedConversation(provider) for arm in ARMS}
    before = source_hashes(source)
    report = {
        "evaluation_revision": 2,
        "requested_model": model,
        "max_steps_per_trial": max_steps,
        "max_output_tokens_per_request": max_output_tokens,
        "source_sha256": before,
        "fixture_sha256": {"initial": initial_hashes},
        "cases": [],
        "scope": "Actual provider usage on one synthetic workflow, two arms and four phases. The model authors its cold reusable computation; no task definitions/results are supplied for free. Identical fixture creation/import and MCP process startup are excluded for both. Provider input includes tool schemas, instructions, model-authored setup and retained conversation on every request. All failures remain. This is a small diagnostic, not a general savings, billing, statistical reliability, or arbitrary-shell comparison.",
        "baseline": "Current adaptive text-only SQL tools with saved query recipes, results, notes and batched freshness inspection; a single combined SQL query is explicitly allowed.",
        "candidate": "The same baseline tools plus task_define, task_run, task_status and context; tool use is model-selected, including non-adoption.",
        "history": "Actual Responses items, including encrypted reasoning, are retained in memory for warm/changed phases. Reset removes all earlier messages and receipt bases; each phase reconnects MCP to the persistent workbook. Encrypted reasoning and provider response IDs are excluded from results.",
        "cache_usage": "cached_input_tokens is a subset of input_tokens, not an additional charge. cache_write_tokens is null when the provider does not report it.",
        "request_limit_note": "The global request budget is authoritative. Six rounds across eight trials can exceed 36 requests; later unattempted trials remain failures. A budget-exhausted run is not a complete paired comparison. The recorded case order alternates arms between phases.",
    }
    abort = False
    expected_hashes = initial_hashes
    for index, phase in enumerate(PHASES):
        if phase == "changed":
            for workspace in workspaces.values():
                expected_hashes = fixture(workspace, changed=True)
            report["fixture_sha256"]["changed"] = expected_hashes
        # Alternate which arm runs first, without discarding failed trials.
        order = ARMS if index % 2 == 0 else tuple(reversed(ARMS))
        for arm in order:
            if abort:
                report["cases"].append(
                    skipped(phase, arm, "not_attempted_after_provider_failure")
                )
                continue
            conversation = conversations[arm]
            conversation.begin(reset=phase in {"cold", "reset"})
            names = [*BASE_TOOLS, *(TASK_TOOLS if arm == "candidate" else ())]
            result = None
            try:
                async with connector(source, workspaces[arm], names) as (
                    client,
                    definitions,
                    instructions,
                ):
                    evidence = EvidenceClient(client, conversation)
                    result = await shared.solve(
                        conversation,
                        evidence,
                        definitions,
                        instructions,
                        prompt(phase),
                        expected(phase == "changed"),
                        model=model,
                        max_steps=max_steps,
                        max_output_tokens=max_output_tokens,
                    )
                    conversation.finish(result)
                    result["unretained_receipts"] = evidence.unretained_receipts
                    if evidence.unretained_receipts:
                        result.update(
                            success=False, failure="receipt_without_retained_base"
                        )
            except (OSError, RuntimeError, TimeoutError, ValueError):
                if result is None:
                    report["cases"].append(skipped(phase, arm, "tool_session_failure"))
                    continue
                result.update(success=False, failure="tool_session_failure")
            book = Workbook(workspaces[arm])
            actual_hashes = {
                name: book.inspect(name)["sha256"] for name in expected_hashes
            }
            result["source_integrity"] = actual_hashes == expected_hashes
            if not result["source_integrity"]:
                result.update(success=False, failure="fixture_source_modified")
            abort = result["abort_run"]
            record = {
                "phase": phase,
                "arm": arm,
                "attempted": True,
                **public_result(result),
            }
            record["tool_selection"] = dict(
                Counter(call["name"] for call in result["tool_calls"])
            )
            record["task_tool_calls"] = sum(
                call["name"] in TASK_TOOLS for call in result["tool_calls"]
            )
            report["cases"].append(record)
            print(
                f"{phase}/{arm}: {'pass' if result['success'] else result['failure']}",
                flush=True,
            )
    report["sources_unchanged"] = source_hashes(source) == before
    report["provider_requests_attempted"] = provider.requests
    report["provider_responses_with_usage"] = sum(
        len(case["provider_requests"]) for case in report["cases"]
    )
    report["usage_complete"] = (
        report["provider_requests_attempted"] == report["provider_responses_with_usage"]
    )
    report["complete"] = all(case["attempted"] for case in report["cases"])
    report["all_correct"] = report["complete"] and all(
        case["success"] for case in report["cases"]
    )
    report["usage"] = totals(report["cases"])
    report["arms"] = {
        arm: {
            "correct": sum(
                case["success"] for case in report["cases"] if case["arm"] == arm
            ),
            "trials": len(PHASES),
            "usage": totals([case for case in report["cases"] if case["arm"] == arm]),
            "task_tool_calls": sum(
                case.get("task_tool_calls", 0)
                for case in report["cases"]
                if case["arm"] == arm
            ),
        }
        for arm in ARMS
    }
    return report


def prior_diagnostic(path):
    """Link an earlier attempt without overwriting it or copying raw traces."""
    with path.open("rb") as source:
        raw = source.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Prior diagnostic exceeds 16 MiB")
    previous = json.loads(raw)
    return {
        "report_sha256": hashlib.sha256(raw).hexdigest(),
        "requested_model": previous["requested_model"],
        "max_steps_per_trial": previous["max_steps_per_trial"],
        "usage": previous["usage"],
        "cases": [
            {key: case[key] for key in ("phase", "arm", "success", "failure")}
            for case in previous["cases"]
        ],
        "note": "Retained diagnostic, not replaced or excluded. This follow-up uses its own recorded round limit, source hashes and tool instructions; changed methodology prevents treating the attempts as repeated identical trials.",
    }


async def run(args):
    if args.output.exists():
        raise ValueError("Choose a new output directory")
    prior = (
        prior_diagnostic(args.prior_report) if args.prior_report is not None else None
    )
    key = shared.load_key(args.env_file)
    source = Path(__file__).resolve().parents[1]
    async with httpx.AsyncClient(
        timeout=90, follow_redirects=False, trust_env=False
    ) as http:
        provider = shared.ResponsesProvider(http, key, args.request_budget)
        with TemporaryDirectory(prefix="workspool-task-eval-") as temporary:
            report = await evaluate(
                provider,
                Path(temporary),
                source,
                model=args.model,
                max_steps=args.max_steps,
                max_output_tokens=args.max_output_tokens,
            )
    report["request_budget"] = args.request_budget
    if prior is not None:
        report["prior_diagnostic"] = prior
    report["runtime"] = {
        name: importlib.metadata.version(name) for name in ("duckdb", "mcp", "httpx")
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--request-budget", type=int, choices=range(1, 37), default=36)
    parser.add_argument("--max-steps", type=int, choices=range(1, 7), default=6)
    parser.add_argument("--prior-report", type=Path)
    parser.add_argument(
        "--max-output-tokens", type=int, choices=range(128, 1601), default=1600
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except (OSError, ValueError):
        print(
            "Task evaluation setup failed; check local arguments and private key configuration.",
            file=sys.stderr,
        )
        return 1
    return (
        0
        if report["all_correct"]
        and report["sources_unchanged"]
        and report["usage_complete"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
