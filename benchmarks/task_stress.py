"""Exact task-graph stress against a capable persistent DuckDB cache.

No downloads or model calls. Inputs remain local; reports may contain aggregate
market results and local paths and require review before publication. Run once
per fresh output directory. Full-scale runs are deliberately caller initiated.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sys
import time
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Timer

AGGREGATE = """SELECT ticker, count(*) AS minute_rows, sum(v) AS volume,
min(t) AS first_t, max(t) AS last_t, min(l) AS low, max(h) AS high
FROM input1 GROUP BY ticker ORDER BY ALL"""
WATCHLIST = """SELECT ticker, minute_rows, volume, first_t, last_t, low, high
FROM input1 ORDER BY volume DESC, ticker, minute_rows, first_t, last_t, low, high LIMIT 5"""
CALENDAR = """SELECT count(*) AS days, CAST(sum(minute_rows) AS BIGINT) AS minute_rows,
min(day) AS first_day, max(day) AS last_day FROM input1"""
STEPS = [
    {"name": "symbols", "inputs": ["bars"], "sql": AGGREGATE},
    {"name": "watchlist", "inputs": ["symbols"], "sql": WATCHLIST},
    {"name": "calendar_stats", "inputs": ["calendar"], "sql": CALENDAR},
]
POLICY = (
    "Review volume rankings against the pinned historical data snapshot.\n"
    "Volume measures historical activity, not future returns or suitability.\n"
    "Calendar coverage is an independent fixed source; a bars update does not update it.\n"
    "Every numerical result must identify its exact inputs. A superseded run is not current.\n"
)
TASK_NAME = "market-review"


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scalar(value):
    if isinstance(value, Decimal):
        with localcontext() as context:
            context.prec = 60
            return format(value.normalize(), "f")
    return value


def table_result(path):
    import pyarrow.parquet as pq

    source = pq.ParquetFile(path)
    if source.metadata.num_rows > 10000:
        raise ValueError("Benchmark result exceeds 10000 rows")
    table = source.read()
    return {
        "columns": table.column_names,
        "rows": [
            [scalar(value) for value in row.values()] for row in table.to_pylist()
        ],
    }


def oracle(path):
    """Arrow grouping plus Python Decimal accumulation, independent of DuckDB."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    symbols, days, count = {}, {}, 0
    for batch in pq.ParquetFile(path).iter_batches(
        batch_size=262144, columns=["ticker", "t", "v", "l", "h"]
    ):
        table = pa.Table.from_batches([batch])
        if any(column.null_count for column in table.columns):
            raise ValueError("Task oracle requires nonnull canonical market columns")
        grouped = table.group_by("ticker").aggregate(
            [
                ("t", "count"),
                ("v", "sum"),
                ("t", "min"),
                ("t", "max"),
                ("l", "min"),
                ("h", "max"),
            ]
        )
        for row in grouped.to_pylist():
            key = row["ticker"]
            previous = symbols.setdefault(
                key,
                [
                    key,
                    0,
                    Decimal(0),
                    row["t_min"],
                    row["t_max"],
                    row["l_min"],
                    row["h_max"],
                ],
            )
            previous[1] += row["t_count"]
            with localcontext() as context:
                context.prec = 60
                previous[2] += row["v_sum"]
            previous[3] = min(previous[3], row["t_min"])
            previous[4] = max(previous[4], row["t_max"])
            previous[5] = min(previous[5], row["l_min"])
            previous[6] = max(previous[6], row["h_max"])
        by_day = pa.table(
            {"day": pc.divide(table["t"], pa.scalar(86400000, type=pa.int64()))}
        )
        for row in by_day.group_by("day").aggregate([("day", "count")]).to_pylist():
            days[row["day"]] = days.get(row["day"], 0) + row["day_count"]
        count += batch.num_rows
    if not count:
        raise ValueError("Task stress requires nonempty market data")
    columns = ["ticker", "minute_rows", "volume", "first_t", "last_t", "low", "high"]
    ordered = sorted(symbols.values())
    watchlist = sorted(symbols.values(), key=lambda row: (-row[2], row[0], *row[1:]))[
        :5
    ]
    return {
        "row_count": count,
        "symbols": {
            "columns": columns,
            "rows": [[scalar(v) for v in row] for row in ordered],
        },
        "watchlist": {
            "columns": columns,
            "rows": [[scalar(v) for v in row] for row in watchlist],
        },
        "calendar_stats": {
            "columns": ["days", "minute_rows", "first_day", "last_day"],
            "rows": [[len(days), count, min(days), max(days)]],
        },
        "calendar_rows": [
            {"day": day, "minute_rows": rows} for day, rows in sorted(days.items())
        ],
    }


def load_counter(name):
    """Real cached tokenizer only: this benchmark never downloads rank files."""
    import tiktoken
    import tiktoken.load

    original = tiktoken.load.read_file

    def local_only(path):
        if path.startswith(("https://", "http://")):
            raise ValueError("Pre-cache tokenizer ranks before running task stress")
        return Path(path).read_bytes()

    try:
        tiktoken.load.read_file = local_only
        encoder = tiktoken.get_encoding(name)
    finally:
        tiktoken.load.read_file = original
    return lambda text: len(encoder.encode(text, disallowed_special=()))


class DirectCache:
    """A persistent script cache with SQL, settings, ordered input IDs and hashes.

    This deliberately strong control has the same known deterministic SQL plan.
    Its runner stays in process, and makes no claim to general SQL safety or
    cross-process lease recovery. Cached result hashes are checked on every hit.
    """

    def __init__(self, root, settings):
        self.root, self.settings = Path(root), settings
        self.root.mkdir(parents=True, exist_ok=True)

    def _verify_inputs(self, inputs):
        record = self.root / "verified-inputs.json"
        verified = json.loads(record.read_text()) if record.exists() else {}
        changed = False
        for item in inputs:
            path = Path(item["path"]).resolve()
            info = path.stat()
            fingerprint = [
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
            expected = {"fingerprint": fingerprint, "digest": item["digest"]}
            if verified.get(str(path)) != expected:
                if digest_file(path) != item["digest"]:
                    raise ValueError(
                        "Direct cache source changed outside its pinned snapshot"
                    )
                after = path.stat()
                if [
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ] != fingerprint:
                    raise ValueError("Direct cache source changed during verification")
                verified[str(path)] = expected
                changed = True
        if changed:
            write_json(record, verified)

    def run(self, sources, *, force=False):
        import duckdb

        aliases, results = dict(sources), []
        for step in STEPS:
            inputs = [aliases[name] for name in step["inputs"]]
            self._verify_inputs(inputs)
            key_data = {
                "sql": step["sql"],
                "inputs": [{k: item[k] for k in ("id", "digest")} for item in inputs],
                "memory": self.settings.query_memory,
                "threads": self.settings.query_threads,
                "duckdb": duckdb.__version__,
            }
            key = hashlib.sha256(compact(key_data).encode()).hexdigest()
            path, metadata = self.root / (key + ".parquet"), self.root / (key + ".json")
            hit, started = False, time.perf_counter()
            if not force and path.exists() and metadata.exists():
                saved = json.loads(metadata.read_text())
                if (
                    saved["key_data"] == key_data
                    and digest_file(path) == saved["digest"]
                ):
                    hit = True
            if not hit:
                with duckdb.connect(
                    config={
                        "threads": str(self.settings.query_threads),
                        "memory_limit": self.settings.query_memory,
                        "autoload_known_extensions": "false",
                        "autoinstall_known_extensions": "false",
                    }
                ) as db:
                    for position, item in enumerate(inputs, 1):
                        literal = (
                            "'"
                            + str(Path(item["path"]).resolve()).replace("'", "''")
                            + "'"
                        )
                        db.execute(
                            f"CREATE VIEW input{position} AS SELECT * FROM read_parquet({literal})"
                        )
                    temporary = path.with_suffix(".partial.parquet")
                    timer = Timer(self.settings.query_timeout, db.interrupt)
                    timer.start()
                    try:
                        db.sql(step["sql"]).write_parquet(str(temporary))
                    finally:
                        timer.cancel()
                        timer.join()
                    temporary.replace(path)
                saved = {"key_data": key_data, "digest": digest_file(path)}
                write_json(metadata, saved)
            descriptor = {"id": key, "digest": saved["digest"], "path": str(path)}
            aliases[step["name"]] = descriptor
            results.append(
                {
                    "name": step["name"],
                    "cache_hit": hit,
                    "elapsed_seconds": time.perf_counter() - started,
                    "inputs": key_data["inputs"],
                    "artifact": descriptor,
                    "result": table_result(path),
                }
            )
        return results


def validate_task(workbook, result, expected):
    failures, full_results, executions = [], {}, 0
    aliases = {name: item["artifact_id"] for name, item in result["sources"].items()}
    by_name = {step["name"]: step for step in result["steps"]}
    if len(by_name) != len(result["steps"]) or set(by_name) != {
        step["name"] for step in STEPS
    }:
        failures.append({"reason": "unexpected_step_set"})
    for definition in STEPS:
        name = definition["name"]
        step = by_name.get(name, {})
        if step.get("state") != "complete":
            failures.append(
                {"step": name, "reason": "step_not_complete", "detail": step}
            )
            continue
        executions += not step["cache"]["hit"]
        artifact = workbook.inspect(step["artifact_id"])
        inputs = [aliases[alias] for alias in definition["inputs"]]
        if artifact["dependencies"] != inputs:
            failures.append({"step": name, "reason": "wrong_provenance"})
        observed = table_result(workbook.blobs / artifact["sha256"])
        full_results[name] = observed
        if observed != expected[name]:
            failures.append(
                {
                    "step": name,
                    "reason": "oracle_mismatch",
                    "observed": observed,
                    "expected": expected[name],
                }
            )
        aliases[name] = artifact["artifact_id"]
    for citation in result["citations"]:
        source = workbook.inspect(citation["artifact_id"])
        raw = (workbook.blobs / source["sha256"]).read_bytes()
        if (
            citation["excerpt"].encode()
            != raw[citation["start_byte"] : citation["end_byte"]]
        ):
            failures.append({"reason": "wrong_citation"})
    if not result["citations"]:
        failures.append({"reason": "missing_policy_citation"})
    return {
        "success": not failures,
        "failures": failures,
        "compute_executions": executions,
        "results": full_results,
    }


def budget_status(workbook, counter, budgets):
    from agent_workbook.tasks import status

    outputs = []
    for budget in budgets:
        started = time.perf_counter()
        try:
            result = status(
                workbook,
                TASK_NAME,
                max_bytes=131072,
                fits=lambda value, limit=budget: counter(compact(value)) <= limit,
            )
            actual = counter(compact(result))
            outputs.append(
                {
                    "budget": budget,
                    "status": "ok",
                    "tokens": actual,
                    "brief_complete": result["brief_complete"],
                    "omitted": result["omitted"],
                    "elapsed_seconds": time.perf_counter() - started,
                    "result": result,
                }
            )
            if actual > budget:
                raise AssertionError("Task context exceeded its accepted token budget")
        except ValueError as error:
            outputs.append(
                {
                    "budget": budget,
                    "status": "budget_error",
                    "error": str(error),
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
    return outputs


def run_suite(
    source,
    updated_source,
    output,
    *,
    counter,
    tokenizer="o200k_base",
    budgets=(1000, 2000),
    race=True,
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from agent_workbook import tasks
    from agent_workbook.settings import Settings
    from agent_workbook.store import Workbook

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "workbook" / "workbook.sqlite3").exists():
        raise ValueError(
            "Use a fresh output directory; cold measurements must start empty"
        )
    source, updated_source = Path(source).resolve(), Path(updated_source).resolve()
    started = time.perf_counter()
    old, new = oracle(source), oracle(updated_source)
    source_digest, updated_digest = digest_file(source), digest_file(updated_source)
    if source_digest == updated_digest:
        raise ValueError("Updated source must contain an actual content change")
    calendar_path = output / "calendar.parquet"
    pq.write_table(pa.Table.from_pylist(old["calendar_rows"]), calendar_path)
    calendar_digest = digest_file(calendar_path)
    # Calendar is a distinct fixed source. Updating bars invalidates only its descendants.
    new["calendar_stats"] = old["calendar_stats"]
    settings = Settings()
    workbook = Workbook(output / "workbook", settings)
    imported = workbook.import_path(source, name="bars")
    workbook.import_path(calendar_path, name="calendar")
    workbook.save_text(POLICY, name="policy", kind="document", media_type="text/plain")
    plan = {
        "name": TASK_NAME,
        "objective": "Review historical volume rankings and independent calendar coverage with exact evidence.",
        "sources": {"bars": "bars", "calendar": "calendar", "policy": "policy"},
        "steps": STEPS,
        "searches": ["review volume rankings"],
    }
    definition = tasks.define_task(workbook, **plan)
    direct_sources = {
        "bars": {"id": "bars:1", "digest": source_digest, "path": str(source)},
        "calendar": {
            "id": "calendar:1",
            "digest": calendar_digest,
            "path": str(calendar_path),
        },
    }
    report = {
        "format": 1,
        "tokenizer": tokenizer,
        "method": "One ordered lifecycle run; exact Arrow oracle; persistent direct DuckDB cache control. No model calls or statistical speedup claim.",
        "hardware": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "inputs": {
            "source": {
                "sha256": source_digest,
                "bytes": source.stat().st_size,
                "rows": old["row_count"],
            },
            "updated": {
                "sha256": updated_digest,
                "bytes": updated_source.stat().st_size,
                "rows": new["row_count"],
            },
        },
        "settings": {
            "memory": settings.query_memory,
            "threads": settings.query_threads,
            "query_timeout_seconds": settings.query_timeout,
        },
        "plan": plan,
        "definition": definition,
        "preparation_seconds": time.perf_counter() - started,
        "scenarios": [],
        "source_hashes": {},
    }
    direct = DirectCache(output / "direct-cache", settings)

    def scenario(name, expected, expected_compute, *, force=False):
        started = time.perf_counter()
        direct_result = direct.run(direct_sources, force=force)
        direct_elapsed = time.perf_counter() - started
        direct_failures = [
            item["name"]
            for item in direct_result
            if item["result"] != expected[item["name"]]
        ]
        started = time.perf_counter()
        result = tasks.run_task(workbook, TASK_NAME, force=force, max_bytes=131072)
        elapsed = time.perf_counter() - started
        validation = validate_task(workbook, result, expected)
        direct_compute = sum(not item["cache_hit"] for item in direct_result)
        observed_compute = validation["compute_executions"]
        record = {
            "name": name,
            "expected_compute_executions": expected_compute,
            "direct": {
                "elapsed_seconds": direct_elapsed,
                "compute_executions": direct_compute,
                "success": not direct_failures,
                "failures": direct_failures,
                "steps": direct_result,
            },
            "workspool": {
                "elapsed_seconds": elapsed,
                "validation": validation,
                "context_tokens": counter(compact(result)),
                "result": result,
            },
            "compute_expectation_met": direct_compute
            == observed_compute
            == expected_compute,
            "budgeted_context": budget_status(workbook, counter, budgets),
        }
        report["scenarios"].append(record)
        write_json(output / "results.json", report)
        print(
            compact(
                {
                    "scenario": name,
                    "correct": validation["success"] and not direct_failures,
                    "direct_computations": direct_compute,
                    "workspool_computations": observed_compute,
                    "direct_seconds": direct_elapsed,
                    "workspool_seconds": elapsed,
                }
            ),
            flush=True,
        )
        return result

    scenario("cold", old, 3)
    scenario("warm", old, 0)
    workbook, direct = (
        Workbook(output / "workbook", settings),
        DirectCache(output / "direct-cache", settings),
    )
    scenario("reopened", old, 0)
    workbook.import_path(
        updated_source, name="bars", expected_version=imported["version"]
    )
    stale = tasks.status(workbook, TASK_NAME, max_bytes=131072)
    report["stale_before_refresh"] = {
        "current": stale["current"],
        "state": stale["state"],
    }
    direct_sources["bars"] = {
        "id": "bars:2",
        "digest": updated_digest,
        "path": str(updated_source),
    }
    scenario("one_source_changed", new, 2)
    scenario("forced_recompute", new, 3, force=True)
    if race:
        original_execute, updated = tasks._execute_step, False

        def updating_step(*args, **kwargs):
            nonlocal updated
            if not updated:
                workbook.import_path(source, name="bars", expected_version=2)
                updated = True
            return original_execute(*args, **kwargs)

        tasks._execute_step = updating_step
        try:
            started = time.perf_counter()
            raced = tasks.run_task(workbook, TASK_NAME, force=True, max_bytes=131072)
        finally:
            tasks._execute_step = original_execute
        report["source_update_during_step"] = {
            "timing": "After run pinning, inside the first step invocation before its query starts",
            "elapsed_seconds": time.perf_counter() - started,
            "validation": validate_task(workbook, raced, new),
            "correctly_superseded": raced["state"] == "superseded"
            and not raced["current"],
            "result": raced,
        }
        direct_sources["bars"] = {
            "id": "bars:3",
            "digest": source_digest,
            "path": str(source),
        }
        scenario("recover_current", old, 2)
    for relative in (
        "benchmarks/task_stress.py",
        "src/agent_workbook/tasks.py",
        "src/agent_workbook/query_cache.py",
        "src/agent_workbook/context_index.py",
    ):
        path = Path(__file__).parents[1] / relative
        report["source_hashes"][relative] = digest_file(path)
    report["success"] = all(
        item["direct"]["success"]
        and item["workspool"]["validation"]["success"]
        and item["compute_expectation_met"]
        for item in report["scenarios"]
    ) and report["stale_before_refresh"] == {"current": False, "state": "superseded"}
    if race:
        report["success"] &= (
            report["source_update_during_step"]["correctly_superseded"]
            and report["source_update_during_step"]["validation"]["success"]
        )
    write_json(output / "results.json", report)
    return report


def terminal_retained_refs(run):
    """Check terminal outputs and policy for this known unchanged plan.

    Recursive stale_inputs covers table ancestors. Candidate-only definitions,
    intermediate results and table sources need no separate metadata request.
    """
    consumed = {alias for step in STEPS for alias in step["inputs"]}
    terminal = {step["name"] for step in STEPS} - consumed
    outputs = {step["name"]: step["artifact_id"] for step in run["steps"]}
    return [outputs[name] for name in sorted(terminal)] + [
        run["sources"]["policy"]["artifact_id"]
    ]


async def mcp_trace(output, tokenizer, counter, budgets):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from agent_workbook.render import decode
    from agent_workbook.store import Workbook
    from agent_workbook.tasks import status

    output = Path(output).resolve()
    workbook = Workbook(output / "workbook")
    current = status(workbook, TASK_NAME, max_bytes=131072)
    trace = {
        "measurement": "Actual MCP, known tool arguments; no model decisions. Fresh-context fallback reads retained materializations, never raw market rows. Retained-context comparisons assume the caller already holds the unabridged prior brief/results and separately measure a metadata-only freshness check.",
        "calls": [],
        "source_hashes": {
            relative: digest_file(Path(__file__).parents[1] / relative)
            for relative in (
                "benchmarks/task_stress.py",
                "src/agent_workbook/tasks.py",
                "src/agent_workbook/query_cache.py",
                "src/agent_workbook/context_index.py",
                "src/agent_workbook/server.py",
                "src/agent_workbook/render.py",
                "src/agent_workbook/budget.py",
                "src/agent_workbook/discovery.py",
            )
        },
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(workbook.root),
            "serve",
            "--profile",
            "agent",
            "--tokenizer",
            tokenizer,
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "MASSIVE_API_KEY"}
        },
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        listed = await client.list_tools()
        setup = {
            "instructions": initialized.instructions,
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in listed.tools
            ],
        }
        trace["setup"] = setup
        trace["setup_tokens"] = counter(compact(setup))
        lazy = any(tool.name == "workspool" for tool in listed.tools)

        async def invoke(name, arguments, arm):
            wire_name = "workspool" if lazy else name
            wire_arguments = (
                {"tool": name, "arguments": arguments} if lazy else arguments
            )
            request = {"name": wire_name, "arguments": wire_arguments}
            started = time.perf_counter()
            result = await client.call_tool(wire_name, wire_arguments)
            wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            blocks = [item for item in result.content if item.type == "text"]
            decoded = None if result.isError else decode(blocks)
            trace["calls"].append(
                {
                    "arm": arm,
                    "tool": name,
                    "request": request,
                    "response": wire,
                    "decoded": decoded,
                    "is_error": bool(result.isError),
                    "elapsed_seconds": time.perf_counter() - started,
                    "request_tokens": counter(compact(request)),
                    "response_tokens": counter(compact(wire)),
                }
            )
            write_json(output / "mcp-trace.json", trace)
            return trace["calls"][-1]

        for budget in budgets:
            await invoke(
                "task_status",
                {"name": TASK_NAME, "max_bytes": 131072, "max_tokens": budget},
                f"task_status_{budget}",
            )
        warm = await invoke(
            "task_run", {"name": TASK_NAME, "max_bytes": 131072}, "task_warm_run"
        )
        if not warm["is_error"]:
            retained = warm["decoded"]
            base_run = retained["run_id"]
            trace["retained_base"] = {
                "run_id": base_run,
                "source_call": "task_warm_run",
                "assumption": "Caller retains this full, untrimmed delivered brief, including its explicit preview omissions.",
            }
            for tool in ("task_run", "task_status"):
                await invoke(
                    tool,
                    {
                        "name": TASK_NAME,
                        "base_run": base_run,
                        "max_bytes": 131072,
                        "max_tokens": 1000,
                    },
                    tool + "_retained_receipt",
                )
            retained_refs = terminal_retained_refs(retained)
            trace["retained_freshness_scope"] = {
                "references": len(retained_refs),
                "outputs": ["calendar_stats", "watchlist"],
                "text_sources": ["policy"],
                "reason": "Known unchanged plan; terminal result stale_inputs recursively covers its table ancestors. No intermediate, table-source or candidate-only definition reads are needed.",
            }
            freshness = await invoke(
                "inspect", {"ref": retained_refs}, "prior_tools_retained_freshness"
            )
            trace["retained_freshness_current"] = (
                not freshness["is_error"]
                and len(freshness["decoded"]["artifacts"]) == len(retained_refs)
                and all(
                    item["is_latest"] and not item["stale_inputs"]
                    for item in freshness["decoded"]["artifacts"]
                )
            )
        refs = [item["artifact_id"] for item in current["sources"].values()] + [
            step["artifact_id"] for step in current["steps"]
        ]
        await invoke("inspect", {"ref": refs}, "prior_tools_retained_results")
        for step in current["steps"]:
            await invoke(
                "query",
                {
                    "inputs": [step["artifact_id"]],
                    "sql": "SELECT * FROM input1 ORDER BY ALL",
                    "limit": 5,
                },
                "prior_tools_retained_results",
            )
        await invoke(
            "search",
            {
                "text": "review volume rankings",
                "refs": [current["sources"]["policy"]["artifact_id"]],
                "limit": 1,
                "context_bytes": 512,
            },
            "prior_tools_retained_results",
        )
    trace["totals"] = {}
    for call in trace["calls"]:
        total = trace["totals"].setdefault(
            call["arm"],
            {
                "calls": 0,
                "request_tokens": 0,
                "response_tokens": 0,
                "elapsed_seconds": 0,
                "errors": 0,
            },
        )
        for key in ("request_tokens", "response_tokens", "elapsed_seconds"):
            total[key] += call[key]
        total["calls"] += 1
        total["errors"] += call["is_error"]
    receipts = [
        call for call in trace["calls"] if call["arm"].endswith("_retained_receipt")
    ]
    trace["retained_validation"] = {
        "unchanged_receipts": len(receipts) == 2
        and all(
            not call["is_error"]
            and call["decoded"].get("delivery") == "unchanged"
            and call["decoded"].get("base_run") == trace["retained_base"]["run_id"]
            and call["decoded"].get("requires_retained_base") is True
            and call["decoded"].get("current") is True
            and call["response_tokens"] <= 1000
            for call in receipts
        ),
        "baseline_freshness_current": trace.get("retained_freshness_current", False),
    }
    trace["source_files_unchanged"] = all(
        digest_file(Path(__file__).parents[1] / relative) == expected
        for relative, expected in trace["source_hashes"].items()
    )
    write_json(output / "mcp-trace.json", trace)
    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--updated-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tokenizer", choices=("cl100k_base", "o200k_base"), default="o200k_base"
    )
    parser.add_argument("--budgets", type=int, nargs="+", default=[1000, 2000])
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--mcp-only", action="store_true")
    parser.add_argument("--no-race", action="store_true")
    args = parser.parse_args()
    counter = load_counter(args.tokenizer)
    if not args.mcp_only:
        report = run_suite(
            args.source,
            args.updated_source,
            args.output,
            counter=counter,
            tokenizer=args.tokenizer,
            budgets=args.budgets,
            race=not args.no_race,
        )
        if not report["success"]:
            raise SystemExit(
                "Task stress recorded validation failures; inspect results.json"
            )
    if args.mcp or args.mcp_only:
        asyncio.run(mcp_trace(args.output, args.tokenizer, counter, args.budgets))


if __name__ == "__main__":
    main()
