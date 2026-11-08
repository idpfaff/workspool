"""Local recurring-workflow integration probe; no provider/model/network calls.

All inputs are disclosed synthetic integer-cent CSVs. Actual MCP requests/replies
and bounded CLI ticks are retained. Token counts describe serialized protocol
payloads, not provider billing or source-exclusion savings.
"""

import argparse
import asyncio
import csv
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, TextContent

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.render import decode
from agent_workbook.store import Workbook

ROOT = Path(__file__).resolve().parents[1]
TASK = "recurring_spend"
IDENTITY = "recurring-agent"
LIMIT = 100000
FIXTURES = [
    "category,amount_cents\nhosting,70000\nanalytics,30000\nstorage,20000\n",
    "category,amount_cents\nhosting,90000\nanalytics,40000\nstorage,20000\n",
    "category,amount_cents\nhosting,30000\nanalytics,20000\nstorage,10000\n",
    "category,amount_cents\nhosting,100000\nanalytics,50000\nstorage,30000\n",
]
WORKER = """import json
import sys
import duckdb

request = json.load(sys.stdin)
assert request["format"] == 1
connection = duckdb.connect()
total = connection.execute(
    "SELECT total_cents FROM read_parquet(?)",
    [request["inputs"][0]["path"]],
).fetchone()[0]
limit = connection.execute(
    "SELECT limit_cents FROM read_parquet(?)",
    [request["inputs"][1]["path"]],
).fetchone()[0]
json.dump({"format": 1,
    "columns": ["total_cents", "limit_cents", "over_budget", "overage_cents"],
    "types": ["BIGINT", "BIGINT", "BOOLEAN", "BIGINT"],
    "rows": [[total, limit, total > limit, max(0, total - limit)]]}, sys.stdout)
"""


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprints():
    paths = sorted((ROOT / "src/agent_workbook").glob("*.py")) + [Path(__file__)]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def environment():
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"}
    }


def oracle(fixture):
    total = sum(
        int(row["amount_cents"]) for row in csv.DictReader(io.StringIO(fixture))
    )
    return [[total, LIMIT, total > LIMIT, max(0, total - LIMIT)]]


def steps(run):
    return {step["name"]: step for step in run["steps"]}


def snapshot(book):
    with book.connect() as db:
        return [
            list(row)
            for row in db.execute(
                "SELECT artifact_id,name,version,digest FROM artifacts ORDER BY artifact_id"
            )
        ]


class Probe:
    def __init__(self, output, tokenizer):
        self.output, self.tokenizer = output, tokenizer
        self.count = token_counter(tokenizer)
        self.trace, self.checks = [], []
        self.clients = {}
        self.workspace = output / "original"
        self.registry = output / "workers.json"
        self.result = {
            "format": 1,
            "scope": "Synthetic local integration; no model or paid API calls",
            "tokenizer": tokenizer,
            "fixtures": FIXTURES,
            "expected_rows": [oracle(fixture) for fixture in FIXTURES],
            "source_sha256": fingerprints(),
            "dependencies": {
                "python": sys.version.split()[0],
                **{
                    name: importlib.metadata.version(name)
                    for name in ("mcp", "duckdb", "tiktoken", "agent-workbook")
                },
            },
        }

    def check(self, name, condition, **evidence):
        self.checks.append({"name": name, "passed": bool(condition), **evidence})
        if not condition:
            raise AssertionError(name)

    def configure(self):
        script = self.output / "review_worker.py"
        script.write_text(WORKER)
        hashes = {
            str(Path(sys.executable).resolve()): digest(Path(sys.executable).resolve()),
            str(script): digest(script),
        }
        self.registry.write_text(
            json.dumps(
                {
                    "format": 1,
                    "workers": {
                        "local_review": {
                            "command": [sys.executable, str(script)],
                            "files": hashes,
                            "timeout_seconds": 30,
                            "max_output_bytes": 1024 * 1024,
                            "max_input_bytes": 1024 * 1024,
                            "max_rows": 100,
                        }
                    },
                },
                indent=2,
            )
        )
        self.result["worker_sha256"] = {
            "script": digest(script),
            "interpreter": digest(Path(sys.executable).resolve()),
        }

    @asynccontextmanager
    async def session(self, book, role="writer"):
        args = [
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(book),
            "serve",
            "--profile",
            "runtime",
            "--agent-id",
            IDENTITY if role == "writer" else "recurring-reviewer",
            "--role",
            role,
            "--tokenizer",
            self.tokenizer,
            "--worker-registry",
            str(self.registry),
        ]
        if role == "writer":
            args.append("--metrics")
        with (self.output / f"{book.name}-{role}.stderr.log").open("a") as errors:
            async with (
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable, args=args, env=environment()
                    ),
                    errlog=errors,
                ) as (reader, writer),
                ClientSession(reader, writer) as client,
            ):
                started = time.perf_counter()
                initialized = await client.initialize()
                listed = await client.list_tools()
                setup = {
                    "initialize": initialized.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                    "tools": listed.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    ),
                }
                self.trace.append(
                    {
                        "transport": "mcp_setup",
                        "workspace": book.name,
                        "role": role,
                        "response": setup,
                        "response_tokens": self.count(compact(setup)),
                        "elapsed_seconds": time.perf_counter() - started,
                    }
                )
                self.clients[id(client)] = {"workspace": book.name, "role": role}
                try:
                    yield client
                finally:
                    self.clients.pop(id(client), None)

    async def call(self, client, tool, arguments=None):
        request = {
            "name": "workspool",
            "arguments": {"tool": tool, "arguments": arguments or {}},
        }
        started = time.perf_counter()
        response = await client.call_tool(request["name"], request["arguments"])
        raw = serialized_result(response)
        self.trace.append(
            {
                "transport": "mcp",
                **self.clients[id(client)],
                "request": request,
                "response": json.loads(raw),
                "request_tokens": self.count(compact(request)),
                "response_tokens": self.count(raw),
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
        if response.isError:
            raise RuntimeError(f"MCP {tool} failed: {raw[:2000]}")
        return decode(response.content)

    async def cli(self, workspace, *arguments):
        command = [
            sys.executable,
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(workspace),
            *map(str, arguments),
        ]
        started = time.perf_counter()
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            env=environment(),
            capture_output=True,
            text=True,
            timeout=90,
        )
        record = {
            "transport": "cli",
            "command": command,
            "returncode": completed.returncode,
            "elapsed_seconds": time.perf_counter() - started,
        }
        self.trace.append(record)
        if completed.returncode:
            record["error"] = completed.stderr[:2000]
            raise RuntimeError(f"CLI {arguments[0]} failed: {completed.stderr[:2000]}")
        record["response"] = json.loads(completed.stdout)
        return record["response"]

    async def tick(self, workspace=None):
        return await self.cli(
            workspace or self.workspace,
            "schedule-tick",
            "--worker-registry",
            self.registry,
            "--agent-id",
            IDENTITY,
        )

    async def run(self, client, resume=None):
        args = {"name": TASK, "max_bytes": 65536}
        if resume:
            args["resume_from"] = resume
        return await self.call(client, "task_run", args)

    async def save(self, client, index):
        args = {
            "name": "spend",
            "kind": "table",
            "media_type": "text/csv",
            "content": FIXTURES[index],
        }
        if index:
            args["expected_version"] = index
        return await self.call(client, "save", args)

    def exact_run(self, label, run, index):
        items = steps(run)
        branch = "review" if oracle(FIXTURES[index])[0][2] else "no_alert"
        other = "no_alert" if branch == "review" else "review"
        self.check(
            label,
            run["state"] == "complete"
            and run["current"]
            and items[branch]["result"]["columns"]
            == ["total_cents", "limit_cents", "over_budget", "overage_cents"]
            and compact(items[branch]["result"]["rows"])
            == compact(oracle(FIXTURES[index]))
            and items[branch]["result"]["preview_truncated"] is False
            and items[other]["state"] == "skipped",
            state=run["state"],
            rows=items.get(branch, {}).get("result", {}).get("rows"),
            expected=oracle(FIXTURES[index]),
            skipped=items[other]["state"],
        )

    async def exercise(self):
        self.configure()
        async with (
            self.session(self.workspace) as writer,
            self.session(self.workspace, "approver") as reviewer,
        ):
            first_source = await self.save(writer, 0)
            await self.call(
                writer,
                "save",
                {
                    "name": "policy",
                    "kind": "table",
                    "media_type": "text/csv",
                    "content": f"limit_cents\n{LIMIT}\n",
                },
            )
            await self.call(
                writer,
                "task_define",
                {
                    "name": TASK,
                    "objective": "Review recurring spend in exact integer cents; require approval before the local review worker",
                    "sources": {"spend": "spend", "policy": "policy"},
                    "steps": [
                        {
                            "name": "summary",
                            "inputs": ["spend"],
                            "sql": "SELECT sum(amount_cents)::BIGINT AS total_cents FROM input1",
                        },
                        {
                            "name": "policy_limit",
                            "inputs": ["policy"],
                            "sql": "SELECT max(limit_cents)::BIGINT AS limit_cents FROM input1",
                        },
                        {
                            "name": "review",
                            "kind": "worker",
                            "worker": "local_review",
                            "inputs": ["summary", "policy_limit"],
                            "when": {
                                "step": "summary",
                                "column": "total_cents",
                                "op": "gt",
                                "value": LIMIT,
                            },
                            "approval": True,
                        },
                        {
                            "name": "no_alert",
                            "inputs": ["summary", "policy_limit"],
                            "sql": "SELECT total_cents, limit_cents, false AS over_budget, 0::BIGINT AS overage_cents FROM input1 CROSS JOIN input2",
                            "when": {
                                "step": "summary",
                                "column": "total_cents",
                                "op": "le",
                                "value": LIMIT,
                            },
                        },
                    ],
                },
            )
            waiting = await self.run(writer)
            gate = steps(waiting)["review"]["approval_ref"]
            self.check(
                "initial exact-input approval wait",
                waiting["state"] == "awaiting_approval"
                and "result" not in steps(waiting)["review"],
            )
            await self.call(
                reviewer, "approve", {"ref": gate, "note": "Synthetic fixture approval"}
            )
            first = await self.run(writer, waiting["run_id"])
            self.exact_run("approved worker result", first, 0)
            self.check(
                "resume retains exact upstream materialization",
                steps(first)["summary"]["artifact_id"]
                == steps(waiting)["summary"]["artifact_id"]
                and steps(first)["summary"]["cache"]["hit"],
            )
            warm = await self.run(writer)
            self.exact_run("warm result", warm, 0)
            self.check(
                "warm SQL reuse and fresh worker execution",
                steps(warm)["summary"]["cache"]["hit"]
                and steps(warm)["review"]["cache"]["hit"] is False
                and steps(warm)["review"]["cache"]["reason"] == "configured_worker",
                cache={name: item.get("cache") for name, item in steps(warm).items()},
            )
            await self.call(reviewer, "approve", {"ref": warm["artifact_id"]})
            before = await self.call(
                writer,
                "verify",
                {
                    "refs": [first_source["artifact_id"], warm["artifact_id"]],
                    "limit": 50,
                },
            )
            self.check(
                "fresh published report approval",
                next(
                    item
                    for item in before["sources"]
                    if item["artifact_id"] == warm["artifact_id"]
                )["current_approved"],
            )
            checkpoint = before["through"]
            await self.call(
                writer,
                "task_schedule",
                {
                    "action": "set",
                    "name": "spend_on_change",
                    "task": TASK,
                    "on_change": True,
                },
            )
            initial_tick = await self.tick()
            self.check(
                "initial explicit schedule tick",
                [item["state"] for item in initial_tick["attempts"]] == ["complete"],
            )
            idle = await self.tick()
            self.check("unchanged schedule is idle", idle["attempts"] == [])
            changed_source = await self.save(writer, 1)
            changed = await self.call(
                writer,
                "verify",
                {
                    "refs": [first_source["artifact_id"]],
                    "after": checkpoint,
                    "limit": 50,
                },
            )
            source = changed["sources"][0]
            self.check(
                "verification identifies exact changed source and affected task",
                source["current"] is False
                and any(
                    item["current_artifact_id"] == changed_source["artifact_id"]
                    for item in source["changed_sources"]
                )
                and any(
                    item["task"] == TASK
                    and not item["current"]
                    and item["suggestion"]["tool"] == "task_run"
                    for item in changed["published_tasks"]
                ),
                verification=changed,
            )
            changed_tick = await self.tick()
            self.check(
                "source-change schedule waits for new approval",
                [item["state"] for item in changed_tick["attempts"]]
                == ["awaiting_approval"],
            )
            waiting = await self.call(
                writer,
                "task_status",
                {
                    "name": TASK,
                    "run_id": changed_tick["attempts"][0]["run_id"],
                    "max_bytes": 65536,
                },
            )
            gate2 = steps(waiting)["review"]["approval_ref"]
            self.check(
                "old approval does not authorize new source snapshot", gate2 != gate
            )
            await self.call(reviewer, "approve", {"ref": gate2})
            resumed_tick = await self.tick()
            self.check(
                "approval triggers scheduled resume",
                [item["state"] for item in resumed_tick["attempts"]] == ["complete"],
            )
            resumed = await self.call(
                writer,
                "task_status",
                {
                    "name": TASK,
                    "run_id": resumed_tick["attempts"][0]["run_id"],
                    "max_bytes": 65536,
                },
            )
            self.exact_run("changed-source exact worker result", resumed, 1)
            await self.call(reviewer, "approve", {"ref": gate2, "decision": "revoke"})
            revoked = await self.run(writer)
            self.check(
                "revocation stops worker execution",
                revoked["state"] == "awaiting_approval"
                and "result" not in steps(revoked)["review"],
            )
            await self.call(reviewer, "approve", {"ref": gate2})
            self.exact_run(
                "explicit reapproval resumes result",
                await self.run(writer, revoked["run_id"]),
                1,
            )
            await self.save(writer, 2)
            self.exact_run("low-spend conditional branch", await self.run(writer), 2)
            await self.save(writer, 3)
            waiting = await self.run(writer)
            gate3 = steps(waiting)["review"]["approval_ref"]
            await self.call(reviewer, "approve", {"ref": gate3})
            final = await self.run(writer, waiting["run_id"])
            self.exact_run("final high-spend worker result", final, 3)
            await self.call(
                writer, "claim", {"resource": "task:archive-proof", "ttl": 3600}
            )
            measured = await self.call(writer, "metrics", {"limit": 100})
            totals = {
                field: sum(group[field] for group in measured["groups"])
                for field in (
                    "calls",
                    "errors",
                    "elapsed_seconds",
                    "formatting_samples",
                    "actual_tokens",
                    "full_tokens",
                    "formatting_tokens_reduced",
                    "cache_hits",
                    "cache_misses",
                    "cache_bypassed",
                )
            }
            self.check(
                "actual MCP metrics are measured",
                measured["recording_enabled"]
                and measured["recording_errors"] == 0
                and measured["complete"]
                and totals["calls"] > 0
                and totals["formatting_samples"] > 0,
                totals=totals,
            )
            self.result["metrics"] = measured
            self.result["metric_totals"] = totals
            book = Workbook(self.workspace)
            exported_snapshot = snapshot(book)
            archive = self.output / "workbook.zip"
            exported = await self.cli(self.workspace, "workbook-export", archive)
            restored_path = self.output / "restored"
            restored = await self.cli(
                self.workspace, "workbook-import", archive, restored_path
            )
            restored_book = Workbook(restored_path)
            self.check(
                "bundle preserves exact artifact IDs versions and digests",
                snapshot(restored_book) == exported_snapshot,
                artifacts=len(exported_snapshot),
                archive_sha256=exported["sha256"],
            )
            with restored_book.connect() as db:
                active_schedules = db.execute(
                    "SELECT count(*) FROM task_schedules WHERE enabled OR lease_owner IS NOT NULL OR lease_until>0"
                ).fetchone()[0]
                open_sessions = db.execute(
                    "SELECT count(*) FROM continuity_sessions WHERE ended_at IS NULL"
                ).fetchone()[0]
                original_metrics = [
                    tuple(row)
                    for row in db.execute(
                        "SELECT * FROM observation_daily ORDER BY day,tool,tokenizer"
                    )
                ]
            with book.connect() as db:
                matching_metrics = [
                    tuple(row)
                    for row in db.execute(
                        "SELECT * FROM observation_daily ORDER BY day,tool,tokenizer"
                    )
                ]
            self.check(
                "restore disarms schedules and closes sessions",
                active_schedules == 0
                and open_sessions == 0
                and restored["live_execution_restored"] is False,
            )
            self.check(
                "restore preserves measured aggregates",
                original_metrics == matching_metrics,
            )
            self.result["bundle"] = {
                "artifacts": exported["artifacts"],
                "blobs": exported["blobs"],
                "bytes": exported["bytes"],
                "sha256": exported["sha256"],
            }
        async with self.session(restored_path) as restored_client:
            approval = await self.call(
                restored_client, "verify", {"refs": [gate3], "limit": 50}
            )
            item = approval["sources"][0]
            self.check(
                "import retains historical approval but requires reconfirmation",
                item["current"] is True
                and not item["current_approved"]
                and item["approval_requires_reconfirmation"],
                approval=item,
            )
            owner = await self.call(
                restored_client, "claim_owner", {"resource": "task:archive-proof"}
            )
            self.check("import expires live ownership", owner["active"] is False)
            self.check(
                "imported schedule remains disabled",
                (await self.tick(restored_path))["attempts"] == [],
            )
            waiting = await self.run(restored_client)
            self.check(
                "imported approval cannot execute worker",
                waiting["state"] == "awaiting_approval"
                and "result" not in steps(waiting)["review"],
            )

    def finish(self, error=None):
        calls = [item for item in self.trace if item["transport"] == "mcp"]
        measured = [
            item
            for item in calls
            if item["workspace"] == "original"
            and item["role"] == "writer"
            and item["request"]["arguments"]["tool"] not in {"metrics", "verify"}
        ]
        audit = {
            "requests_recount_match": True,
            "responses_recount_match": True,
            "measured_calls": len(measured),
            "actual_tokens": 0,
            "full_tokens": 0,
        }
        for item in calls:
            audit["requests_recount_match"] &= (
                self.count(compact(item["request"])) == item["request_tokens"]
            )
            audit["responses_recount_match"] &= (
                self.count(compact(item["response"])) == item["response_tokens"]
            )
        for item in measured:
            response = CallToolResult.model_validate(item["response"])
            if response.isError:
                continue
            data = response.structuredContent or decode(response.content)
            full = CallToolResult(
                content=[TextContent(type="text", text=compact(data))],
                structuredContent=data,
            )
            audit["actual_tokens"] += self.count(serialized_result(response))
            audit["full_tokens"] += self.count(serialized_result(full))
        audit["formatting_tokens_reduced"] = (
            audit["full_tokens"] - audit["actual_tokens"]
        )
        totals = self.result.get("metric_totals", {})
        audit["metrics_recount_match"] = audit["measured_calls"] == totals.get(
            "calls"
        ) and all(
            audit[field] == totals.get(field)
            for field in ("actual_tokens", "full_tokens", "formatting_tokens_reduced")
        )
        self.result["trace_audit"] = audit
        self.result.update(
            {
                "passed": error is None and all(item["passed"] for item in self.checks),
                "checks": self.checks,
                "source_unchanged": fingerprints() == self.result["source_sha256"],
                "source_sha256_after": fingerprints(),
                "error": error,
                "protocol": {
                    "mcp_calls": len(calls),
                    "cli_commands": sum(
                        item["transport"] == "cli" for item in self.trace
                    ),
                    "request_tokens": sum(item["request_tokens"] for item in calls),
                    "response_tokens": sum(item["response_tokens"] for item in calls),
                    "setup_response_tokens": sum(
                        item.get("response_tokens", 0)
                        for item in self.trace
                        if item["transport"] == "mcp_setup"
                    ),
                    "scope": "Actual serialized MCP payloads; initialization/list-tools separate; no model framing or billed usage",
                },
            }
        )
        self.result["passed"] &= self.result["source_unchanged"]
        self.result["passed"] &= all(
            audit[field]
            for field in (
                "requests_recount_match",
                "responses_recount_match",
                "metrics_recount_match",
            )
        )
        # Only synthetic content is published; normalize machine-specific paths.
        replacements = [
            (str(self.output), "<probe>"),
            (sys.executable, "<python>"),
            (str(ROOT), "<repository>"),
            (str(Path.home()), "<home>"),
        ]

        def sanitized(value):
            raw = json.dumps(value, ensure_ascii=False, indent=2)
            for original, replacement in replacements:
                raw = raw.replace(original, replacement)
            return raw

        (self.output / "results.json").write_text(sanitized(self.result) + "\n")
        with gzip.open(self.output / "traces.json.gz", "wt") as stream:
            stream.write(sanitized(self.trace))
        return self.result["passed"]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tokenizer", default="o200k_base", choices=["cl100k_base", "o200k_base"]
    )
    arguments = parser.parse_args()
    output = arguments.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    probe = Probe(output, arguments.tokenizer)
    started = time.perf_counter()
    error = None
    try:
        await asyncio.wait_for(probe.exercise(), timeout=300)
    except Exception as exc:  # noqa: BLE001 - retain unsuccessful scenarios as evidence
        error = f"{type(exc).__name__}: {exc}"
    probe.result["elapsed_seconds"] = time.perf_counter() - started
    passed = probe.finish(error)
    print(
        json.dumps(
            {
                "passed": passed,
                "checks": len(probe.checks),
                "error": error,
                "results": str(output / "results.json"),
            }
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
