"""Configured public-data task stress with an independent streaming Arrow oracle.

No downloads or model calls. Run against local Parquet files in a fresh output
directory. A configuration declares SQL and a separate aggregation specification;
the latter, never DuckDB or a SQL preview, supplies the expected full results.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import platform
import re
import sys
import time
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Timer

BATCH_ROWS = 65536
MAX_RESULT_ROWS = 10000
MAX_GROUPS = 100000
ROOT = Path(__file__).resolve().parents[1]


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
            context.prec = 80
            if value == value.to_integral_value():
                return int(value)
            return format(value.normalize(), "f")
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    return value


def arrow_type(value):
    import pyarrow as pa

    decimal = re.fullmatch(r"decimal128\((\d+),(\d+)\)", value.replace(" ", ""))
    if decimal:
        return pa.decimal128(*map(int, decimal.groups()))
    timestamp = re.fullmatch(r"timestamp\[(s|ms|us|ns)\]", value)
    if timestamp:
        return pa.timestamp(timestamp[1])
    return pa.type_for_alias(value)


def merge_parquet(
    files, destination, *, columns=None, types=None, batch_rows=BATCH_ROWS
):
    """Project/cast bounded batches; preserve authentic row order and duplicates.

    Explicit Arrow casts are recorded in the config. In particular, choosing a
    decimal scale for a floating-point source is a declared normalization, not a
    claim to recover decimal digits lost by that source's original encoding.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    if type(batch_rows) is not int or not 1 <= batch_rows <= 1048576:
        raise ValueError("batch_rows must be 1..1048576")
    if not files:
        raise ValueError("At least one source Parquet file is required")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Prepared destination must be new")
    temporary = destination.with_suffix(".partial.parquet")
    writer, schema, count, largest = None, None, 0, 0
    try:
        for filename in files:
            source = pq.ParquetFile(filename)
            for batch in source.iter_batches(batch_size=batch_rows, columns=columns):
                table = pa.Table.from_batches([batch])
                for name, kind in (types or {}).items():
                    index = table.schema.get_field_index(name)
                    if index < 0:
                        raise ValueError(f"Cast column is missing: {name}")
                    table = table.set_column(
                        index, name, pc.cast(table[name], arrow_type(kind))
                    )
                table = table.replace_schema_metadata(None)
                if writer is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(temporary, schema, compression="zstd")
                elif table.schema != schema:
                    raise ValueError(
                        "Projected source schemas differ; declare common types"
                    )
                writer.write_table(table, row_group_size=batch_rows)
                count += table.num_rows
                largest = max(largest, table.num_rows)
        if writer is None:
            raise ValueError("Source files contain no rows")
        writer.close()
        writer = None
        temporary.replace(destination)
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "sha256": digest_file(destination),
        "bytes": destination.stat().st_size,
        "rows": count,
        "largest_batch_rows": largest,
        "schema": str(schema),
    }


def _sorted(rows, columns, order):
    result = list(rows)
    for spec in reversed(order):
        position = columns.index(spec["column"])
        direction = spec.get("direction", "ascending")
        if direction not in {"ascending", "descending"}:
            raise ValueError("Oracle sort direction must be ascending or descending")
        present = [row for row in result if row[position] is not None]
        missing = [row for row in result if row[position] is None]
        result = (
            sorted(
                present,
                key=lambda row: row[position],
                reverse=direction == "descending",
            )
            + missing
        )  # DuckDB's default is NULLS LAST in both directions.
    return result


def arrow_oracle(source, specification, *, batch_rows=BATCH_ROWS):
    """Independent count/sum/min/max grouping or a downstream ordered projection.

    Integer/decimal sums are exact. Floating sums must be explicitly normalized
    during preparation, since execution order can change their least bits.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    keys = specification.get("group_by", [])
    aggregates = specification.get("aggregates")
    previous = isinstance(source, dict)
    if aggregates is None:
        if not previous:
            raise ValueError("Oracle projections must consume a bounded prior result")
        columns = specification.get("columns", source["columns"])
        indexes = [source["columns"].index(column) for column in columns]
        rows = [[row[index] for index in indexes] for row in source["native_rows"]]
    else:
        if not aggregates:
            raise ValueError("Oracle needs at least one aggregate")
        columns = keys + [item["name"] for item in aggregates]
        if len(columns) != len(set(columns)):
            raise ValueError("Oracle output names must be unique")
        needed = list(
            dict.fromkeys(keys + [a["column"] for a in aggregates if "column" in a])
        )
        if previous:
            table = pa.Table.from_pylist(
                [dict(zip(source["columns"], row)) for row in source["native_rows"]]
            )
            batches = table.to_batches(max_chunksize=batch_rows)
        else:
            parquet = pq.ParquetFile(source)
            # A physical column supplies rows for count_rows-only expressions.
            batches = parquet.iter_batches(
                batch_size=batch_rows, columns=needed or [parquet.schema_arrow.names[0]]
            )
        groups = {}
        for batch in batches:
            table = pa.Table.from_batches([batch])
            internal = "__oracle_count__"
            while internal in table.column_names:
                internal += "_"
            table = table.append_column(
                internal, pa.array([1] * table.num_rows, type=pa.int8())
            )
            operations = []
            for aggregate_index, item in enumerate(aggregates):
                operation = item["op"]
                column = internal if operation == "count_rows" else item["column"]
                if operation not in {"count_rows", "count", "sum", "min", "max"}:
                    raise ValueError(f"Unsupported oracle aggregate: {operation}")
                if operation == "sum" and pa.types.is_floating(table[column].type):
                    raise ValueError(
                        "Normalize floating sum inputs to an explicit decimal type"
                    )
                if operation == "sum" and pa.types.is_integer(table[column].type):
                    # Arrow integer sum wraps at 64 bits, unlike DuckDB HUGEINT.
                    # Cast before partial aggregation, not after an overflow.
                    exact = f"{internal}_sum_{aggregate_index}"
                    table = table.append_column(
                        exact, pc.cast(table[column], pa.decimal128(38, 0))
                    )
                    column = exact
                operations.append(
                    (column, "count" if operation == "count_rows" else operation)
                )
            partial = table.group_by(keys).aggregate(operations)
            for values in zip(*[column.to_pylist() for column in partial.columns]):
                key, additions = tuple(values[: len(keys)]), values[len(keys) :]
                if key not in groups:
                    if len(groups) >= MAX_GROUPS:
                        raise ValueError("Independent oracle group cap exceeded")
                    groups[key] = [None] * len(aggregates)
                accumulator = groups[key]
                for index, (item, addition) in enumerate(zip(aggregates, additions)):
                    if addition is None:
                        continue
                    current = accumulator[index]
                    if current is None:
                        accumulator[index] = addition
                    elif item["op"] in {"sum", "count", "count_rows"}:
                        with localcontext() as context:
                            context.prec = 80
                            accumulator[index] = current + addition
                    elif item["op"] == "min":
                        accumulator[index] = min(current, addition)
                    else:
                        accumulator[index] = max(current, addition)
        if not groups and not keys:
            groups[()] = [
                0 if a["op"] in {"count", "count_rows"} else None for a in aggregates
            ]
        rows = [list(key) + values for key, values in groups.items()]
    rows = _sorted(rows, columns, specification.get("sort", []))
    if "limit" in specification:
        limit = specification["limit"]
        if type(limit) is not int or not 0 <= limit <= MAX_RESULT_ROWS:
            raise ValueError("Oracle limit must be bounded")
        rows = rows[:limit]
    if len(rows) > MAX_RESULT_ROWS:
        raise ValueError(
            "Full oracle output exceeds result cap; aggregate or limit explicitly"
        )
    return {
        "columns": columns,
        "native_rows": rows,
        "rows": [[scalar(v) for v in row] for row in rows],
    }


def expected_results(sources, steps):
    aliases, results = dict(sources), {}
    for step in steps:
        specification = step["oracle"]
        origin = specification["source"]
        if origin not in step["inputs"]:
            raise ValueError("Oracle source must be a declared step input")
        value = arrow_oracle(aliases[origin], specification)
        aliases[step["name"]] = value
        results[step["name"]] = {"columns": value["columns"], "rows": value["rows"]}
    return results


def read_result(path):
    import pyarrow.parquet as pq

    source = pq.ParquetFile(path)
    if source.metadata.num_rows > MAX_RESULT_ROWS:
        raise ValueError("Full result exceeds validation cap")
    table = source.read()
    return {
        "columns": table.column_names,
        "rows": [
            [scalar(value) for value in row.values()] for row in table.to_pylist()
        ],
    }


def support():
    specification = importlib.util.spec_from_file_location(
        "_public_table_task_support", Path(__file__).with_name("task_stress.py")
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class DirectCache:
    """Capable persisted script cache for the trusted, declared benchmark SQL.

    It validates source fingerprints/hashes and cached bytes, but is not a general
    concurrent or untrusted-query service. SQL, typed JSON parameters, ordered
    inputs, engine version, and execution settings form its exact cache key.
    """

    def __init__(self, root, settings, steps):
        self.root, self.settings, self.steps = Path(root), settings, steps
        self.root.mkdir(parents=True, exist_ok=True)
        self.verifier = support().DirectCache(root, settings)

    def run(self, sources, *, force=False):
        import duckdb

        from agent_workbook.materialize import lossless_parquet_projection

        aliases, results = dict(sources), []
        for step in self.steps:
            started = time.perf_counter()
            inputs = [aliases[alias] for alias in step["inputs"]]
            self.verifier._verify_inputs(inputs)
            key_data = {
                "sql": step["sql"],
                "parameters": step.get("parameters", []),
                "inputs": [
                    {key: item[key] for key in ("id", "digest")} for item in inputs
                ],
                "memory": self.settings.query_memory,
                "threads": self.settings.query_threads,
                "duckdb": duckdb.__version__,
            }
            key = hashlib.sha256(compact(key_data).encode()).hexdigest()
            path, metadata = self.root / (key + ".parquet"), self.root / (key + ".json")
            hit, saved = False, None
            if not force and path.exists() and metadata.exists():
                saved = json.loads(metadata.read_text())
                hit = (
                    saved["key_data"] == key_data
                    and digest_file(path) == saved["digest"]
                )
            if not hit:
                temporary = path.with_suffix(".partial.parquet")
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
                    timer = Timer(self.settings.query_timeout, db.interrupt)
                    timer.start()
                    try:
                        relation = db.sql(
                            step["sql"], params=step.get("parameters", [])
                        )
                        relation.limit(MAX_RESULT_ROWS + 1).create("bounded_result")
                        native = db.table("bounded_result")
                        if native.count("*").fetchone()[0] > MAX_RESULT_ROWS:
                            raise ValueError(
                                "Direct materialized result exceeds validation cap"
                            )
                        projected, _ = lossless_parquet_projection(native)
                        projected.write_parquet(str(temporary))
                        temporary.replace(path)
                    finally:
                        timer.cancel()
                        timer.join()
                        temporary.unlink(missing_ok=True)
                saved = {"key_data": key_data, "digest": digest_file(path)}
                write_json(metadata, saved)
            descriptor = {"id": key, "digest": saved["digest"], "path": str(path)}
            aliases[step["name"]] = descriptor
            results.append(
                {
                    "name": step["name"],
                    "cache_hit": hit,
                    "elapsed_seconds": time.perf_counter() - started,
                    "artifact": descriptor,
                }
            )
        return results


def source_hashes():
    return {
        str(path.relative_to(ROOT)): digest_file(path)
        for path in [
            Path(__file__),
            Path(__file__).with_name("task_stress.py"),
            *sorted((ROOT / "src" / "agent_workbook").glob("*.py")),
        ]
    }


def validate_task(book, result, steps, expected):
    failures, observed = [], {}
    aliases = {alias: item["artifact_id"] for alias, item in result["sources"].items()}
    actual = {step["name"]: step for step in result["steps"]}
    if len(actual) != len(steps) or set(actual) != {step["name"] for step in steps}:
        failures.append("unexpected_step_set")
    for definition in steps:
        name = definition["name"]
        step = actual.get(name, {})
        if step.get("state") != "complete":
            failures.append(name + ":not_complete")
            continue
        artifact = book.inspect(step["artifact_id"])
        if artifact["dependencies"] != [aliases[item] for item in definition["inputs"]]:
            failures.append(name + ":wrong_dependencies")
        observed[name] = read_result(book.blobs / artifact["sha256"])
        if observed[name] != expected[name]:
            failures.append(name + ":oracle_mismatch")
        aliases[name] = step["artifact_id"]
    return {"success": not failures, "failures": failures, "full_results": observed}


def _public_steps(steps):
    return [
        {
            key: step[key]
            for key in ("name", "inputs", "sql", "parameters")
            if key in step
        }
        for step in steps
    ]


def prepare(config, output):
    prepared, hashes = {}, {}
    for alias, source in config["sources"].items():
        prepared[alias] = {}
        for version, field in (("initial", "files"), ("updated", "updated_files")):
            if field not in source:
                continue
            files = [Path(path).resolve() for path in source[field]]
            for path in files:
                hashes[str(path)] = {
                    "sha256": digest_file(path),
                    "bytes": path.stat().st_size,
                }
            started = time.perf_counter()
            item = merge_parquet(
                files,
                output / f"{alias}-{version}.parquet",
                columns=source.get("columns"),
                types=source.get("types"),
            )
            item["preparation_seconds"] = time.perf_counter() - started
            item["source_files"] = [str(path) for path in files]
            prepared[alias][version] = item
        if (
            "updated" in prepared[alias]
            and prepared[alias]["updated"]["sha256"]
            == prepared[alias]["initial"]["sha256"]
        ):
            raise ValueError("An updated source must actually change")
    return prepared, hashes


def run_suite(config, output, *, counters=None, measure_mcp=True):
    from agent_workbook import tasks
    from agent_workbook.settings import Settings
    from agent_workbook.store import Workbook

    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a fresh output directory")
    output.mkdir(parents=True, exist_ok=True)
    if not config.get("name") or not 1 <= len(config.get("sources", {})) <= 8:
        raise ValueError("Config needs a task name and 1..8 sources")
    if not 1 <= len(config.get("steps", [])) <= 8:
        raise ValueError("Config needs 1..8 SQL/oracle steps")
    if any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", alias)
        for alias in config["sources"]
    ):
        raise ValueError("Source aliases must be simple names")
    counters = counters or {
        name: support().load_counter(name) for name in ("cl100k_base", "o200k_base")
    }
    before = source_hashes()
    prepared, raw_hashes = prepare(config, output)
    initial = {alias: item["initial"]["path"] for alias, item in prepared.items()}
    updated = {
        alias: item.get("updated", item["initial"])["path"]
        for alias, item in prepared.items()
    }
    started = time.perf_counter()
    expected_old = expected_results(initial, config["steps"])
    expected_new = expected_results(updated, config["steps"])
    oracle_seconds = time.perf_counter() - started
    settings = Settings(**config.get("settings", {}))
    book = Workbook(output / "workbook", settings)
    imports, direct_sources = [], {}
    for alias, item in prepared.items():
        source = item["initial"]
        started = time.perf_counter()
        saved = book.import_path(Path(source["path"]), name="public/" + alias)
        imports.append(
            {
                "source": alias,
                "phase": "initial",
                "elapsed_seconds": time.perf_counter() - started,
                "artifact": saved,
            }
        )
        direct_sources[alias] = {
            "id": alias + ":1",
            "digest": source["sha256"],
            "path": source["path"],
        }
    plan = {
        "name": config["name"],
        "objective": config.get(
            "objective",
            "Calculate the declared public-data summaries with exact source provenance.",
        ),
        "sources": {alias: "public/" + alias for alias in prepared},
        "steps": _public_steps(config["steps"]),
    }
    started = time.perf_counter()
    definition = tasks.define_task(book, **plan)
    report = {
        "format": 1,
        "config": config,
        "prepared": prepared,
        "raw_source_hashes": raw_hashes,
        "source_hashes": before,
        "imports": imports,
        "plan": plan,
        "definition": definition,
        "definition_seconds": time.perf_counter() - started,
        "oracle_seconds": oracle_seconds,
        "expected": {"initial": expected_old, "updated": expected_new},
        "scenarios": [],
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "method": "Fixed-order local lifecycle. Preparation, independent oracle, imports and validation are timed separately from execution. Full materialized outputs are compared in declared order, never inferred from previews. Direct control is a persistent deterministic-plan DuckDB script cache. Warm filesystem effects and orchestration differences are included; this is not a statistical latency or provider-billing claim.",
    }
    direct = DirectCache(output / "direct-cache", settings, config["steps"])

    def scenario(name, expected, *, force=False, reopen=False):
        nonlocal book, direct
        if reopen:
            book = Workbook(output / "workbook", settings)
            direct = DirectCache(output / "direct-cache", settings, config["steps"])
        started = time.perf_counter()
        controlled = direct.run(direct_sources, force=force)
        direct_seconds = time.perf_counter() - started
        started = time.perf_counter()
        result = tasks.run_task(book, config["name"], force=force, max_bytes=131072)
        task_seconds = time.perf_counter() - started
        started = time.perf_counter()
        validation = validate_task(book, result, config["steps"], expected)
        direct_results = {
            item["name"]: read_result(item["artifact"]["path"]) for item in controlled
        }
        direct_ok = direct_results == expected
        record = {
            "name": name,
            "direct": {
                "elapsed_seconds": direct_seconds,
                "steps": controlled,
                "success": direct_ok,
                "compute_executions": sum(not step["cache_hit"] for step in controlled),
            },
            "workspool": {
                "elapsed_seconds": task_seconds,
                "result": result,
                "validation": validation,
                "compute_executions": sum(
                    not step.get("cache", {}).get("hit", False)
                    for step in result["steps"]
                ),
            },
            "validation_seconds": time.perf_counter() - started,
            "context_json_tokens": {
                name: count(compact(result)) for name, count in counters.items()
            },
        }
        report["scenarios"].append(record)
        write_json(output / "results.json", report)
        print(
            compact(
                {
                    "scenario": name,
                    "success": direct_ok and validation["success"],
                    "direct_seconds": direct_seconds,
                    "workspool_seconds": task_seconds,
                }
            ),
            flush=True,
        )

    scenario("cold", expected_old)
    scenario("warm", expected_old)
    scenario("reopened", expected_old, reopen=True)
    for alias, item in prepared.items():
        if "updated" not in item:
            continue
        source = item["updated"]
        started = time.perf_counter()
        saved = book.import_path(
            Path(source["path"]), name="public/" + alias, expected_version=1
        )
        imports.append(
            {
                "source": alias,
                "phase": "updated",
                "elapsed_seconds": time.perf_counter() - started,
                "artifact": saved,
            }
        )
        direct_sources[alias] = {
            "id": alias + ":2",
            "digest": source["sha256"],
            "path": source["path"],
        }
    if any("updated" in item for item in prepared.values()):
        stale = tasks.status(book, config["name"], max_bytes=131072)
        report["stale_after_update"] = {key: stale[key] for key in ("current", "state")}
        scenario("source_update", expected_new)
    scenario("forced", expected_new, force=True)
    if measure_mcp:
        report["mcp"] = asyncio.run(
            mcp_trace(book.root, config["name"], config["steps"], counters, output)
        )
    report["source_files_unchanged"] = source_hashes() == before
    report["raw_sources_unchanged"] = all(
        digest_file(path) == item["sha256"] for path, item in raw_hashes.items()
    )
    report["success"] = (
        all(
            item["direct"]["success"] and item["workspool"]["validation"]["success"]
            for item in report["scenarios"]
        )
        and report["source_files_unchanged"]
        and report["raw_sources_unchanged"]
    )
    if "stale_after_update" in report:
        report["success"] &= report["stale_after_update"] == {
            "current": False,
            "state": "superseded",
        }
    if measure_mcp:
        report["success"] &= report["mcp"]["success"]
    write_json(output / "results.json", report)
    return report


async def mcp_trace(workspace, name, steps, counters, output):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from agent_workbook.render import decode

    trace = {
        "calls": [],
        "measurement": "Actual MCP formatted requests and results; known operation schemas. Retained arms assume a prior full unabridged brief/results. Setup is counted separately. No model decisions, provider framing or billing. Fresh-context control reads terminal materializations; metadata-only retained control already holds their results and knows the unchanged plan.",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_workbook.cli",
            "--workspace",
            str(workspace),
            "serve",
            "--profile",
            "agent",
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
        initialized, listed = await client.initialize(), await client.list_tools()
        setup = {
            "instructions": initialized.instructions,
            "tools": [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in listed.tools
            ],
        }
        trace["setup"] = setup
        trace["setup_tokens"] = {
            name: count(compact(setup)) for name, count in counters.items()
        }

        async def invoke(tool, arguments, arm):
            request = {
                "name": "workspool",
                "arguments": {"tool": tool, "arguments": arguments},
            }
            started = time.perf_counter()
            result = await client.call_tool(request["name"], request["arguments"])
            wire = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            decoded = None if result.isError else decode(result.content)
            record = {
                "arm": arm,
                "tool": tool,
                "request": request,
                "response": wire,
                "decoded": decoded,
                "is_error": bool(result.isError),
                "elapsed_seconds": time.perf_counter() - started,
                "tokens": {
                    name: {
                        "request": count(compact(request)),
                        "response": count(compact(wire)),
                    }
                    for name, count in counters.items()
                },
            }
            trace["calls"].append(record)
            write_json(Path(output) / "mcp-trace.json", trace)
            return record

        full = await invoke(
            "task_status", {"name": name, "max_bytes": 131072}, "full_brief"
        )
        if full["is_error"]:
            trace["success"] = False
            return trace
        retained = full["decoded"]
        for tool in ("task_run", "task_status"):
            await invoke(
                tool,
                {"name": name, "base_run": retained["run_id"], "max_bytes": 131072},
                tool + "_retained",
            )
        consumed = {item for step in steps for item in step["inputs"]}
        terminal = [step for step in retained["steps"] if step["name"] not in consumed]
        refs = [step["artifact_id"] for step in terminal]
        await invoke("inspect", {"ref": refs}, "terminal_retained_freshness")
        await invoke("inspect", {"ref": refs}, "terminal_fresh_context")
        for step in terminal:
            await invoke(
                "query",
                {
                    "inputs": [step["artifact_id"]],
                    "sql": "SELECT * FROM input1 ORDER BY ALL",
                    "limit": 5,
                },
                "terminal_fresh_context",
            )
        # Definition/SQL discovery and all intermediate previews are intentionally
        # omitted from the competent control's known-plan terminal-output task.
    trace["totals"] = {}
    for record in trace["calls"]:
        total = trace["totals"].setdefault(
            record["arm"],
            {
                "calls": 0,
                "errors": 0,
                "tokens": {name: {"request": 0, "response": 0} for name in counters},
            },
        )
        total["calls"] += 1
        total["errors"] += record["is_error"]
        for encoding, values in record["tokens"].items():
            for kind, value in values.items():
                total["tokens"][encoding][kind] += value
    receipts = [
        record
        for record in trace["calls"]
        if record["arm"] in {"task_run_retained", "task_status_retained"}
    ]
    trace["success"] = not any(record["is_error"] for record in trace["calls"]) and all(
        record["decoded"].get("delivery") == "unchanged"
        and record["decoded"].get("base_run") == retained["run_id"]
        for record in receipts
    )
    write_json(Path(output) / "mcp-trace.json", trace)
    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-mcp", action="store_true")
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text())
    # Relative file paths are resolved beside the config, never an implicit cwd.
    for source in config["sources"].values():
        for field in ("files", "updated_files"):
            if field in source:
                source[field] = [
                    str((arguments.config.parent / path).resolve())
                    for path in source[field]
                ]
    result = run_suite(config, arguments.output, measure_mcp=not arguments.skip_mcp)
    raise SystemExit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
