"""Full real-data CSV stress: Arrow oracle, actual MCP, and direct DuckDB.

No credentials, download, or model calls. Requires a complete download_massive
manifest. Each isolated worker has an external timeout/RSS/disk watchdog.
"""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from decimal import Decimal, localcontext
from pathlib import Path
from threading import Timer

COLUMNS = ("ticker", "t", "o", "h", "l", "c", "v", "n")
AGG_COLUMNS = (
    "row_count",
    "ticker_count",
    "first_t",
    "last_t",
    "total_volume",
    "total_trades",
    "min_low",
    "max_high",
    "up_rows",
)
SQL = """SELECT count(*) AS row_count, count(DISTINCT ticker) AS ticker_count,
min(t) AS first_t, max(t) AS last_t,
sum(CAST(v AS DECIMAL(38,12))) AS total_volume,
sum(CAST(n AS DECIMAL(38,12))) AS total_trades,
min(CAST(l AS DECIMAL(38,12))) AS min_low,
max(CAST(h AS DECIMAL(38,12))) AS max_high,
count(*) FILTER (WHERE CAST(c AS DECIMAL(38,12)) > CAST(o AS DECIMAL(38,12))) AS up_rows
FROM input1"""
GIB = 1024**3


def helpers():
    spec = importlib.util.spec_from_file_location(
        "csv_stress_helpers", Path(__file__).with_name("massive_stress.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def concatenate(data, target):
    """Copy every verified CSV once, sorted by ticker, with one header."""
    helper = helpers()
    manifest = json.loads((data / "manifest.json").read_text())
    symbols = manifest.get("symbols", {})
    if (
        not manifest.get("complete")
        or set(symbols) != set(manifest.get("tickers", []))
        or not symbols
    ):
        raise ValueError(
            "Download manifest must be complete for every requested ticker"
        )
    sources = []
    for ticker, item in sorted(symbols.items()):
        if (
            item.get("status") != "complete"
            or item.get("ticker") != ticker
            or item.get("file") != ticker + ".csv"
            or item.get("columns") != list(COLUMNS)
        ):
            raise ValueError("Invalid completed symbol manifest")
        if Path(ticker).name != ticker or "/" in ticker or "\\" in ticker:
            raise ValueError("Invalid ticker path")
        source = data / item["file"]
        if source.is_symlink() or source.stat().st_size != item["bytes"]:
            raise ValueError("Source size or path changed")
        sources.append(
            {
                "ticker": ticker,
                "file": source.name,
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "rows": item["rows"],
            }
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = target.with_suffix(".manifest.json")
    if target.exists() and metadata_path.exists():
        saved = json.loads(metadata_path.read_text())
        if (
            saved.get("sources") == sources
            and saved.get("sha256") == helper.digest_file(target)
            and all(
                helper.digest_file(data / source["file"]) == source["sha256"]
                for source in sources
            )
        ):
            return saved | {"reused": True}
    descriptor, temporary = tempfile.mkstemp(prefix=".full-csv-", dir=target.parent)
    digest, size = hashlib.sha256(), 0
    header = (",".join(COLUMNS) + "\n").encode()
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(header)
            digest.update(header)
            size += len(header)
            for source in sources:
                observed = hashlib.sha256()
                with (data / source["file"]).open("rb") as stream:
                    actual_header = stream.readline()
                    observed.update(actual_header)
                    if actual_header != header:
                        raise ValueError(
                            "CSV header differs from canonical downloader schema"
                        )
                    last = b"\n"
                    while chunk := stream.read(1024 * 1024):
                        observed.update(chunk)
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        last = chunk[-1:]
                    if last != b"\n" or observed.hexdigest() != source["sha256"]:
                        raise ValueError(
                            "CSV source digest or final newline is invalid"
                        )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    saved = {
        "path": str(target),
        "bytes": size,
        "sha256": digest.hexdigest(),
        "rows": sum(source["rows"] for source in sources),
        "sources": sources,
        "selection": "All downloaded rows, sorted by ticker file; no replication or truncation",
    }
    helper.write_json(metadata_path, saved)
    return saved


def arrow_oracle(path, expected_rows=None, block_size=16 * 1024**2):
    """Validate every row and combine vectorized Arrow partials with Decimal60."""
    import pyarrow as pa
    import pyarrow.compute as pc
    from pyarrow import csv

    started = time.perf_counter()
    initial = path.stat()
    schema = {
        "ticker": pa.string(),
        "t": pa.int64(),
        **{name: pa.decimal128(38, 12) for name in COLUMNS[2:]},
    }
    reader = csv.open_csv(
        path,
        read_options=csv.ReadOptions(block_size=block_size, use_threads=False),
        convert_options=csv.ConvertOptions(
            column_types=schema, strings_can_be_null=True, null_values=[""]
        ),
    )
    rows = up = batches = maximum = volume_count = trade_count = 0
    volume = trades = Decimal(0)
    tickers, minimum_t, maximum_t, minimum_l, maximum_h, last = (
        set(),
        None,
        None,
        None,
        None,
        None,
    )
    for batch in reader:
        if batch.schema.names != list(COLUMNS):
            raise ValueError("Unexpected CSV columns")
        arrays = {name: batch.column(name) for name in COLUMNS}
        if any(arrays[name].null_count for name in COLUMNS if name != "n"):
            raise ValueError("Null identity/OHLC/volume value")
        if (
            pc.any(pc.equal(arrays["ticker"], "")).as_py()
            or pc.any(pc.less(arrays["t"], 0)).as_py()
        ):
            raise ValueError("Invalid ticker or timestamp")
        valid = pc.and_(
            pc.and_(
                pc.less_equal(arrays["l"], arrays["o"]),
                pc.less_equal(arrays["o"], arrays["h"]),
            ),
            pc.and_(
                pc.less_equal(arrays["l"], arrays["c"]),
                pc.less_equal(arrays["c"], arrays["h"]),
            ),
        )
        if (
            pc.any(pc.invert(valid)).as_py()
            or pc.any(pc.less(arrays["v"], 0)).as_py()
            or pc.any(pc.less(arrays["n"], 0)).as_py()
        ):
            raise ValueError("Invalid OHLC, volume, or transaction count")
        pc.cast(arrays["n"], pa.int64(), safe=True)
        if len(batch):
            first = (arrays["ticker"][0].as_py(), arrays["t"][0].as_py())
            if last is not None and first <= last:
                raise ValueError("Nonincreasing ticker/timestamp sequence")
            if len(batch) > 1:
                same = pc.equal(
                    arrays["ticker"].slice(1), arrays["ticker"].slice(0, len(batch) - 1)
                )
                bad = pc.and_(
                    same,
                    pc.less_equal(
                        arrays["t"].slice(1), arrays["t"].slice(0, len(batch) - 1)
                    ),
                )
                reversed_tickers = pc.less(
                    arrays["ticker"].slice(1), arrays["ticker"].slice(0, len(batch) - 1)
                )
                if pc.any(pc.or_(bad, reversed_tickers)).as_py():
                    raise ValueError("Nonincreasing ticker/timestamp sequence")
            last = (arrays["ticker"][-1].as_py(), arrays["t"][-1].as_py())
        rows += len(batch)
        batches += 1
        maximum = max(maximum, len(batch))
        tickers.update(pc.unique(arrays["ticker"]).to_pylist())
        low_t, high_t = pc.min(arrays["t"]).as_py(), pc.max(arrays["t"]).as_py()
        low_l, high_h = pc.min(arrays["l"]).as_py(), pc.max(arrays["h"]).as_py()
        minimum_t = low_t if minimum_t is None else min(minimum_t, low_t)
        maximum_t = high_t if maximum_t is None else max(maximum_t, high_t)
        minimum_l = low_l if minimum_l is None else min(minimum_l, low_l)
        maximum_h = high_h if maximum_h is None else max(maximum_h, high_h)
        with localcontext() as context:
            context.prec = 60
            v_sum, n_sum = pc.sum(arrays["v"]).as_py(), pc.sum(arrays["n"]).as_py()
            if v_sum is not None:
                volume += v_sum
                volume_count += len(batch) - arrays["v"].null_count
            if n_sum is not None:
                trades += n_sum
                trade_count += len(batch) - arrays["n"].null_count
        up += (
            pc.sum(pc.cast(pc.greater(arrays["c"], arrays["o"]), pa.int64())).as_py()
            or 0
        )
    final = path.stat()
    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        raise ValueError("CSV changed during oracle scan")
    if rows == 0 or (expected_rows is not None and rows != expected_rows):
        raise ValueError("Full dataset row count differs from manifest or is empty")
    values = [
        rows,
        len(tickers),
        minimum_t,
        maximum_t,
        volume if volume_count else None,
        trades if trade_count else None,
        minimum_l,
        maximum_h,
        up,
    ]
    return {
        "aggregate": [
            str(value) if isinstance(value, Decimal) else value for value in values
        ],
        "columns": list(AGG_COLUMNS),
        "rows": rows,
        "tickers": sorted(tickers),
        "batches": batches,
        "maximum_batch_rows": maximum,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_process_rss_bytes": helpers().peak_rss(),
        "validation": "All rows parsed as explicit Arrow types; nonnull identities/OHLC/volume, OHLC bounds, nonnegative volume/integer transaction counts, strict ordered ticker/timestamp pairs",
    }


def validation(result, oracle):
    if (
        result.get("preview_truncated")
        or result.get("columns") != list(AGG_COLUMNS)
        or len(result.get("rows", [])) != 1
        or len(result["rows"][0]) != len(AGG_COLUMNS)
    ):
        return {
            "success": False,
            "error": "Incomplete aggregate preview",
            "mismatches": [],
        }
    mismatches = []
    with localcontext() as context:
        context.prec = 60
        for column, actual, expected in zip(
            AGG_COLUMNS, result["rows"][0], oracle["aggregate"], strict=True
        ):
            observed = None if actual is None else Decimal(str(actual))
            wanted = None if expected is None else Decimal(str(expected))
            if observed != wanted:
                mismatches.append(
                    {
                        "column": column,
                        "actual": actual,
                        "expected": expected,
                        "difference": None
                        if observed is None or wanted is None
                        else str(observed - wanted),
                    }
                )
    return {"success": not mismatches, "mismatches": mismatches}


def check(result, oracle):
    checked = validation(result, oracle)
    if not checked["success"]:
        raise ValueError(
            checked.get(
                "error", "Aggregate differs from independent Arrow/Decimal oracle"
            )
        )


def failure_details(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for child in error.exceptions for item in failure_details(child)]
    return [{"error_type": type(error).__name__, "error": str(error)[:1000]}]


def direct(config):
    import duckdb

    from agent_workbook.query import _preview

    started = time.perf_counter()
    with duckdb.connect(
        config={
            "threads": "2",
            "memory_limit": config["memory"],
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        }
    ) as db:
        path = str(Path(config["csv"]).resolve()).replace("'", "''")
        db.execute(f"SET allowed_paths = ['{path}']")
        db.execute("SET enable_external_access=false")
        db.execute("SET lock_configuration=true")
        db.execute(f"CREATE VIEW input1 AS SELECT * FROM read_csv_auto('{path}')")
        timer = Timer(config["query_timeout"], db.interrupt)
        timer.start()
        try:
            result = _preview(db.sql(SQL).limit(6), 5)
        finally:
            timer.cancel()
            timer.join()
    checked = validation(result, config["oracle"])
    return {
        "status": "ok" if checked["success"] else "validation_failed",
        "validation": checked,
        "result": result,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_process_rss_bytes": helpers().peak_rss(),
    }


def offline_encoders():
    """Load cached rank files without leaving tiktoken's loader patched."""
    import tiktoken
    import tiktoken.load

    def offline_tokenizer_file(path):
        if path.startswith(("https://", "http://")):
            raise ValueError("Tokenizer ranks must already be in the local cache")
        return Path(path).read_bytes()

    original_read_file = tiktoken.load.read_file
    try:
        tiktoken.load.read_file = offline_tokenizer_file
        return {
            name: tiktoken.get_encoding(name) for name in ("cl100k_base", "o200k_base")
        }
    finally:
        tiktoken.load.read_file = original_read_file


async def mcp(config):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from agent_workbook.render import decode

    encoders = offline_encoders()

    def counts(text):
        return {
            name: len(encoder.encode(text, disallowed_special=()))
            for name, encoder in encoders.items()
        }

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agent_workbook.cli",
            "--workspace",
            config["workspace"],
            "--query-timeout",
            str(config["query_timeout"]),
            "--query-memory",
            config["memory"],
            "--query-threads",
            "2",
            "serve",
            "--tools",
            "import",
            "query",
            "--response-content",
            "text",
        ],
        env={
            key: value
            for key, value in os.environ.items()
            if key not in {"OPENAI_API_KEY", "MASSIVE_API_KEY"}
        },
    )
    calls = []
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        initialized = await client.initialize()
        listed = await client.list_tools()
        setup = (initialized.instructions or "") + compact(
            [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools]
        )

        async def invoke(name, arguments):
            started = time.perf_counter()
            reply = await client.call_tool(name, arguments)
            elapsed = time.perf_counter() - started
            text = "\n".join(
                block.text for block in reply.content if block.type == "text"
            )
            calls.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "reply": reply.model_dump(by_alias=True, exclude_none=True),
                    "elapsed_seconds": elapsed,
                    "request_tokens": counts(
                        compact({"name": name, "arguments": arguments})
                    ),
                    "reply_tokens": counts(text),
                }
            )
            if config.get("trace_path"):
                helpers().write_json(
                    Path(config["trace_path"]),
                    {"calls": calls, "setup_tokens": counts(setup)},
                )
            return reply, decode(reply.content) if not reply.isError else None

        imported, artifact = await invoke(
            "import",
            {"path": str(Path(config["csv"]).resolve()), "name": "market/minute-bars"},
        )
        if imported.isError:
            return {
                "status": "import_failed",
                "calls": calls,
                "setup_tokens": counts(setup),
            }
        reply, result = await invoke(
            "query", {"inputs": [artifact["artifact_id"]], "sql": SQL, "limit": 5}
        )
        output = {
            "status": "query_failed" if reply.isError else "ok",
            "calls": calls,
            "setup_tokens": counts(setup),
            "result": result,
            "peak_host_process_rss_bytes": helpers().peak_rss(),
        }
        if not reply.isError:
            output["validation"] = validation(result, config["oracle"])
            if not output["validation"]["success"]:
                output["status"] = "validation_failed"
        output["payload_tokens"] = {
            name: output["setup_tokens"][name]
            + sum(
                call["request_tokens"][name] + call["reply_tokens"][name]
                for call in calls
            )
            for name in encoders
        }
        return output


def worker(config):
    stage = config["stage"]
    if stage == "prepare":
        source = concatenate(Path(config["data"]), Path(config["csv"]))
        return {
            "status": "ok",
            "source": source,
            "oracle": arrow_oracle(Path(config["csv"]), source["rows"]),
        }
    return direct(config) if stage == "direct" else asyncio.run(mcp(config))


def run(args):
    helper = helpers()
    args.output.mkdir(parents=True, exist_ok=True)
    base = {"data": str(args.data.resolve()), "csv": str(args.csv.resolve())}
    report = {
        "sql": SQL,
        "source_format": "complete canonical CSV",
        "cases": [],
        "limits": {
            "external_seconds": args.timeout,
            "tree_rss_bytes": 4 * GIB,
            "query_threads": 2,
        },
        "scope": "All real downloaded rows; no replication or truncation. Arrow streaming DECIMAL(38,12) with Python Decimal60 combines independent partials. Same SQL, CSV bytes, threads/memory, read_csv_auto parser and preview in direct DuckDB control. Separate workers; cache state/order uncontrolled. MCP payload counts exact instructions/schemas/requests/text replies once, not provider billing or full-source token estimates.",
    }

    def launch(stage, label, extra):
        config = (
            base
            | extra
            | {
                "stage": stage,
                "workspace": str((args.output / (label + "-workbook")).resolve()),
                "trace_path": str((args.output / (label + "-trace.json")).resolve()),
            }
        )
        path = args.output / (label + "-config.json")
        result_path = args.output / (label + "-result.json")
        helper.write_json(path, config)
        measured = helper.guarded(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker-config",
                str(path.resolve()),
                "--worker-result",
                str(result_path.resolve()),
            ],
            output=args.output,
            timeout=args.timeout,
            rss_limit=4 * GIB,
            disk_limit=12 * GIB,
            free_minimum=2 * GIB,
            log=args.output / (label + ".log"),
        )
        result = (
            json.loads(result_path.read_text())
            if result_path.exists()
            else {"status": "no_worker_result"}
        )
        success = measured["status"] == "ok" and result["status"] == "ok"
        return {
            "stage": stage,
            "label": label,
            "success": success,
            "watchdog": measured,
            "worker": result,
        }

    prepared = launch("prepare", "prepare", {})
    report["preparation"] = prepared
    helper.write_json(args.output / "results.json", report)
    if not prepared["success"]:
        return report
    report["source"] = prepared["worker"]["source"]
    report["oracle"] = prepared["worker"]["oracle"]
    extra = {"oracle": report["oracle"], "memory": "1GB", "query_timeout": 15}
    for stage in ("mcp", "direct"):
        result = launch(stage, stage + "-default", extra)
        report["cases"].append(result)
        helper.write_json(args.output / "results.json", report)
        print(stage + " default: " + result["worker"]["status"], flush=True)
    if any(not case["success"] for case in report["cases"]):
        extra |= {"memory": "4GB", "query_timeout": 120}
        for stage in ("mcp", "direct"):
            result = launch(stage, stage + "-tuned", extra)
            report["cases"].append(result)
            helper.write_json(args.output / "results.json", report)
            print(stage + " tuned: " + result["worker"]["status"], flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--worker-result", type=Path)
    args = parser.parse_args()
    if args.worker_config:
        config = {}
        try:
            config = json.loads(args.worker_config.read_text())
            result = worker(config)
        except Exception as error:  # noqa: BLE001 -- persist every isolated worker failure.
            result = {
                "status": "failed",
                "error_type": type(error).__name__,
                "errors": failure_details(error),
            }
            if config.get("trace_path") and Path(config["trace_path"]).is_file():
                result["partial_trace"] = json.loads(
                    Path(config["trace_path"]).read_text()
                )
        helpers().write_json(args.worker_result, result)
        return 0 if result["status"] == "ok" else 1
    if not all((args.data, args.csv, args.output)) or not 0 < args.timeout <= 180:
        parser.error("--data, --csv, --output and timeout in (0,180] are required")
    report = run(args)
    return (
        0
        if report.get("cases") and all(item["success"] for item in report["cases"])
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
