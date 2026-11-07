"""Real monthly analytical replay through MCP, without model/provider calls.

Uses locally downloaded January/February 2025 NYC yellow-taxi files. Both arms
receive identical typed source bytes and perform the same pinned computations.
The control already knows the proven SQL plan; no raw-input prompt baseline.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.budget import serialized_result, token_counter
from agent_workbook.render import decode
from agent_workbook.store import Workbook

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ["PULocationID", "passenger_count"]
OBJECTIVE = "Summarize monthly trips and passenger-data coverage, then rank the five busiest pickup zones."
STEPS = [
    {
        "name": "totals",
        "inputs": ["trips"],
        "sql": "SELECT count(*) AS trips, count(passenger_count) AS recorded_passenger_trips, sum(passenger_count) AS passengers FROM input1",
    },
    {
        "name": "coverage",
        "inputs": ["totals"],
        "sql": "SELECT trips - recorded_passenger_trips AS missing_passenger_trips, recorded_passenger_trips, passengers FROM input1 ORDER BY ALL",
    },
    {
        "name": "top_zones",
        "inputs": ["trips"],
        "sql": "SELECT PULocationID, count(*) AS trips FROM input1 GROUP BY PULocationID ORDER BY trips DESC, PULocationID LIMIT 5",
    },
]


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            value.update(block)
    return value.hexdigest()


def support():
    spec = importlib.util.spec_from_file_location(
        "skill_table_helpers", ROOT / "benchmarks/public_table_stress.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(name, ref):
    return {
        "name": name,
        "objective": OBJECTIVE,
        "sources": {"trips": ref},
        "steps": STEPS,
        "pin_sources": True,
    }


def expected(path):
    """Independent streaming Arrow aggregation, never SQL or a preview."""
    helper = support()
    totals = helper.arrow_oracle(
        path,
        {
            "aggregates": [
                {"name": "trips", "op": "count_rows"},
                {
                    "name": "recorded_passenger_trips",
                    "op": "count",
                    "column": "passenger_count",
                },
                {"name": "passengers", "op": "sum", "column": "passenger_count"},
            ]
        },
    )
    trips, recorded, passengers = totals["rows"][0]
    zones = helper.arrow_oracle(
        path,
        {
            "group_by": ["PULocationID"],
            "aggregates": [{"name": "trips", "op": "count_rows"}],
            "sort": [
                {"column": "trips", "direction": "descending"},
                {"column": "PULocationID"},
            ],
            "limit": 5,
        },
    )
    return {
        "totals": {"columns": totals["columns"], "rows": totals["rows"]},
        "coverage": {
            "columns": [
                "missing_passenger_trips",
                "recorded_passenger_trips",
                "passengers",
            ],
            "rows": [[trips - recorded, recorded, passengers]],
        },
        "top_zones": {"columns": zones["columns"], "rows": zones["rows"]},
    }


def verify_run(book, result, oracle, pinned):
    if (
        result.get("state") != "complete"
        or not result.get("current")
        or result.get("omitted")
    ):
        raise AssertionError("Replay did not yield a complete current unabridged task")
    if result["sources"]["trips"]["artifact_id"] != pinned:
        raise AssertionError("Replay used a different source binding")
    if {item["name"] for item in result["steps"]} != set(oracle):
        raise AssertionError("Replay step set differs from the proven plan")
    helper = support()
    aliases = {"trips": pinned}
    checks = []
    for item in result["steps"]:
        spec = next(step for step in STEPS if step["name"] == item["name"])
        metadata = book.inspect(item["artifact_id"])
        wanted = [aliases[alias] for alias in spec["inputs"]]
        if metadata["dependencies"] != wanted:
            raise AssertionError(
                "Replay materialization has incorrect exact provenance"
            )
        observed = helper.read_result(book.blobs / metadata["sha256"])
        if observed != oracle[item["name"]]:
            raise AssertionError(
                "Full materialized output differs from the independent oracle: "
                + item["name"]
            )
        aliases[item["name"]] = item["artifact_id"]
        checks.append(
            {
                "step": item["name"],
                "rows": len(observed["rows"]),
                "exact_result": True,
                "exact_dependencies": True,
                "cache_hit": item["cache"]["hit"],
            }
        )
    return checks


def totals(calls, arm):
    selected = [item for item in calls if item["arm"] == arm]
    return {
        "calls": len(selected),
        "errors": sum(item["is_error"] for item in selected),
        "request_tokens": sum(item["request_tokens"] for item in selected),
        "response_tokens": sum(item["response_tokens"] for item in selected),
        "elapsed_seconds": sum(item["elapsed_seconds"] for item in selected),
    }


def break_even(extra_setup, baseline_per_replay, candidate_per_replay):
    saving = baseline_per_replay - candidate_per_replay
    return math.ceil(extra_setup / saving) if saving > 0 else None


async def measure_arm(
    directory, prepared, oracles, *, candidate, count, tokenizer, recurring=False
):
    book = Workbook(directory / "workbook")
    refs = (
        {
            "january": book.import_path(
                Path(prepared["january"]["path"]), name="current-month"
            )["artifact_id"]
        }
        if recurring
        else {
            month: book.import_path(Path(value["path"]), name=month)["artifact_id"]
            for month, value in prepared.items()
        }
    )
    operations = ["task_define", "task_run"]
    if candidate:
        operations += ["skill_distill", "skill_find", "skill_inspect", "skill_use"]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(book.root),
            "serve",
            "--profile",
            "agent",
            "--tools",
            *operations,
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "MASSIVE_API_KEY"}
        },
    )
    calls, validations = [], []
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as client,
    ):
        initialized, listed = await client.initialize(), await client.list_tools()
        setup = {
            "instructions": initialized.instructions,
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in listed.tools
            ],
        }

        async def invoke(operation, arguments, arm):
            request = {
                "name": "workspool",
                "arguments": {"tool": operation, "arguments": arguments},
            }
            started = time.perf_counter()
            response = await client.call_tool(request["name"], request["arguments"])
            wire = response.model_dump(mode="json", by_alias=True, exclude_none=True)
            calls.append(
                {
                    "arm": arm,
                    "tool": operation,
                    "request": request,
                    "response": wire,
                    "is_error": bool(response.isError),
                    "elapsed_seconds": time.perf_counter() - started,
                    "request_tokens": count(compact(request)),
                    "response_tokens": count(serialized_result(response)),
                }
            )
            (directory / "trace.json").write_text(
                json.dumps({"setup": setup, "calls": calls}, indent=2) + "\n"
            )
            if response.isError:
                raise AssertionError("Actual MCP operation failed: " + operation)
            return decode(response.content)

        origin_name = "monthly-review" if recurring else "january-review"
        definition = plan(
            origin_name, "current-month" if recurring else refs["january"]
        )
        definition["pin_sources"] = not recurring
        await invoke("task_define", definition, "common_origin")
        origin = await invoke("task_run", {"name": origin_name}, "common_origin")
        validations += verify_run(book, origin, oracles["january"], refs["january"])
        if recurring:
            refs["february"] = book.import_path(
                Path(prepared["february"]["path"]),
                name="current-month",
                expected_version=1,
            )["artifact_id"]
        elif candidate:
            skill = await invoke(
                "skill_distill",
                {
                    "name": "monthly-taxi-summary",
                    "task": "january-review",
                    "run_id": origin["run_id"],
                    "description": "Monthly taxi demand and passenger-data coverage with a top-five pickup-zone ranking.",
                },
                "distillation",
            )
            skill_ref = skill["artifact_id"]
            found = await invoke("skill_find", {"query": "monthly taxi"}, "discovery")
            if not any(item["artifact_id"] == skill_ref for item in found["skills"]):
                raise AssertionError("The saved skill could not be discovered")
            inspected = await invoke("skill_inspect", {"ref": skill_ref}, "discovery")
            if inspected["proof"] != "successful_execution":
                raise AssertionError("Skill lacks its successful execution provenance")
            definition = await invoke(
                "skill_use",
                {
                    "ref": skill_ref,
                    "task_name": "february-review",
                    "bindings": {"trips": refs["february"]},
                },
                "transfer",
            )
            if definition["sources"] != {"trips": refs["february"]}:
                raise AssertionError("Skill validated a different immutable binding")
        else:
            definition = await invoke(
                "task_define", plan("february-review", refs["february"]), "transfer"
            )
        if not recurring and definition["source_mode"] != "pinned":
            raise AssertionError("Both arms must pin the same source version semantics")
        replay_name = origin_name if recurring else "february-review"
        replay = await invoke("task_run", {"name": replay_name}, "transfer")
        validations += verify_run(book, replay, oracles["february"], refs["february"])
        retained = await invoke(
            "task_run",
            {"name": replay_name, "base_run": replay["run_id"]},
            "retained_task",
        )
        if (
            retained.get("delivery") != "unchanged"
            or retained.get("base_run") != replay["run_id"]
        ):
            raise AssertionError(
                "Competent retained-task control did not reuse the prior result"
            )
    return {
        "arm": "rolling_task" if recurring else "skill" if candidate else "known_plan",
        "tokenizer": tokenizer,
        "setup_tokens": count(compact(setup)),
        "calls": calls,
        "validations": validations,
        "totals": {
            phase: totals(calls, phase)
            for phase in (
                "common_origin",
                "distillation",
                "discovery",
                "transfer",
                "retained_task",
            )
        },
        "success": True,
    }


def run_suite(source_root, output, tokenizer="o200k_base"):
    source_root, output = Path(source_root).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("Use a fresh benchmark output directory")
    output.mkdir(parents=True)
    helper, count = support(), token_counter(tokenizer)
    downloads = json.loads((source_root / "downloads.json").read_text())
    prepared, acquisition, oracles = {}, {}, {}
    for number, month in ((1, "january"), (2, "february")):
        filename = f"yellow_tripdata_2025-{number:02d}.parquet"
        source = source_root / "taxi" / filename
        evidence = next(item for item in downloads["files"] if item["file"] == filename)
        if digest(source) != evidence["sha256"]:
            raise ValueError("Official downloaded source hash mismatch")
        acquisition[month] = evidence
        prepared[month] = helper.merge_parquet(
            [source],
            output / (month + ".parquet"),
            columns=COLUMNS,
            types={name: "int64" for name in COLUMNS},
        )
        oracles[month] = expected(prepared[month]["path"])
    fingerprints = {
        str(path.relative_to(ROOT)): digest(path)
        for path in [
            Path(__file__),
            ROOT / "benchmarks/public_table_stress.py",
            *sorted((ROOT / "src/agent_workbook").glob("*.py")),
        ]
    }
    arms = {}
    for name, candidate, recurring in (
        ("known_plan", False, False),
        ("skill", True, False),
        ("rolling_task", False, True),
    ):
        directory = output / name
        directory.mkdir()
        arms[name] = asyncio.run(
            measure_arm(
                directory,
                prepared,
                oracles,
                candidate=candidate,
                count=count,
                tokenizer=tokenizer,
                recurring=recurring,
            )
        )
    cost = lambda value: value["request_tokens"] + value["response_tokens"]
    baseline = cost(arms["known_plan"]["totals"]["transfer"])
    candidate = cost(arms["skill"]["totals"]["transfer"])
    extra = cost(arms["skill"]["totals"]["distillation"]) + max(
        0, arms["skill"]["setup_tokens"] - arms["known_plan"]["setup_tokens"]
    )
    result = {
        "format": 1,
        "method": "Actual MCP agent-profile calls, separate workbooks and identical typed bytes. Known-plan task_define+task_run transfer control; existing rolling task after source update and unchanged retained task controls also measured. No model calls or automatic continuity capture.",
        "tokenizer": tokenizer,
        "token_accounting": "Exact serialized MCP request/result counts. Initialization listed separately; operation schemas assumed known. Not provider billing or agent decision quality.",
        "acquisition": acquisition,
        "prepared": prepared,
        "oracle": oracles,
        "plan_steps": STEPS,
        "arms": arms,
        "comparison": {
            "known_plan_transfer_tokens": baseline,
            "skill_transfer_tokens": candidate,
            "skill_transfer_token_saving": baseline - candidate,
            "extra_distillation_and_setup_tokens": extra,
            "replays_to_amortize_distillation_and_setup": break_even(
                extra, baseline, candidate
            ),
            "discovery_tokens": cost(arms["skill"]["totals"]["discovery"]),
            "rolling_task_updated_source_tokens": cost(
                arms["rolling_task"]["totals"]["transfer"]
            ),
        },
        "source_hashes": fingerprints,
        "source_files_unchanged": all(
            digest(ROOT / path) == value for path, value in fingerprints.items()
        ),
        "raw_sources_unchanged": all(
            digest(source_root / "taxi" / item["file"]) == item["sha256"]
            for item in acquisition.values()
        ),
    }
    result["success"] = (
        result["source_files_unchanged"]
        and result["raw_sources_unchanged"]
        and all(arm["success"] for arm in arms.values())
    )
    (output / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer", default="o200k_base")
    args = parser.parse_args()
    report = run_suite(args.source_root, args.output, args.tokenizer)
    print(json.dumps({"success": report["success"], **report["comparison"]}))
    if not report["success"]:
        raise SystemExit("Recorded source change or failed comparison; inspect results")


if __name__ == "__main__":
    main()
