"""Bounded local-computation stress test over real, immutable market-data files.

No data download or model call is performed. Example:
    python benchmarks/massive_stress.py --sources data/*.csv \
        --rows 1000000 5000000 10000000 25000000 --output .local-benchmarks/stress

Preparation and correctness use Python/Arrow streaming, not DuckDB aggregates.
Only local reports/traces contain source previews; do not publish raw market data.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from threading import Timer

COLUMNS = ("ticker", "t", "o", "h", "l", "c", "v", "n")
DECIMALS = COLUMNS[2:]
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
AGG_SQL = """SELECT count(*) AS row_count, count(DISTINCT ticker) AS ticker_count,
min(t) AS first_t, max(t) AS last_t, sum(v) AS total_volume,
sum(n) AS total_trades, min(l) AS min_low, max(h) AS max_high,
count(*) FILTER (WHERE c > o) AS up_rows FROM input1"""
SAMPLE_PREDICATE = "(t // 60000) % 64 = 0"
TOOLS = ["import", "inspect", "query", "save", "resume", "refresh"]
GIB = 1024**3


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scalar(value):
    if isinstance(value, Decimal):
        with localcontext() as context:
            context.prec = 60
            return format(value.normalize(), "f")
    return value


def canonical_row(record, decimal_scale=12):
    """Reject lossy conversion, nonfinite values and missing identity fields."""
    missing = set(COLUMNS) - record.keys()
    if missing:
        raise ValueError(f"Missing canonical columns: {sorted(missing)}")
    ticker = str(record["ticker"] or "")
    if not ticker or len(ticker) > 120:
        raise ValueError("ticker must be a nonempty bounded string")
    stamp = Decimal(str(record["t"]))
    if (
        not stamp.is_finite()
        or stamp != stamp.to_integral_value()
        or not 0 <= stamp < 2**63
    ):
        raise ValueError("t must be a nonnegative int64 epoch-millisecond value")
    row = [ticker, int(stamp)]
    with localcontext() as context:
        context.prec = 60
        quantum = Decimal(1).scaleb(-decimal_scale)
        for name in DECIMALS:
            value = record[name]
            if value is None or value == "":
                row.append(None)
                continue
            decimal = Decimal(str(value))
            if not decimal.is_finite():
                raise ValueError(f"Nonfinite {name}")
            try:
                exact = decimal.quantize(quantum)
            except InvalidOperation as error:
                raise ValueError(f"Unrepresentable {name}") from error
            if exact != decimal or abs(exact) >= Decimal(10) ** (38 - decimal_scale):
                raise ValueError(
                    f"{name} does not fit exact DECIMAL(38,{decimal_scale})"
                )
            row.append(exact)
    return tuple(row)


class Oracle:
    """Constant memory per ticker, with exact decimal sums and a sampled multiset hash."""

    def __init__(self):
        self.rows = self.up = self.volume_count = self.trade_count = 0
        self.volume = self.trades = Decimal(0)
        self.first = self.last = self.low = self.high = None
        self.tickers = set()
        self.ticker_rows = defaultdict(int)
        self.last_by_ticker = {}
        self.nonincreasing = 0
        self.multiset = 0

    def add(self, row, *, hash_row=False):
        ticker, stamp, opening, high, low, closing, volume, trades = row
        self.rows += 1
        self.tickers.add(ticker)
        self.ticker_rows[ticker] += 1
        previous = self.last_by_ticker.get(ticker)
        self.nonincreasing += previous is not None and stamp <= previous
        self.last_by_ticker[ticker] = stamp
        self.first = stamp if self.first is None else min(self.first, stamp)
        self.last = stamp if self.last is None else max(self.last, stamp)
        if low is not None:
            self.low = low if self.low is None else min(self.low, low)
        if high is not None:
            self.high = high if self.high is None else max(self.high, high)
        with localcontext() as context:
            context.prec = 60
            if volume is not None:
                self.volume += volume
                self.volume_count += 1
            if trades is not None:
                self.trades += trades
                self.trade_count += 1
        self.up += opening is not None and closing is not None and closing > opening
        if hash_row:
            digest = hashlib.sha256(compact(list(map(scalar, row))).encode()).digest()
            self.multiset = (self.multiset + int.from_bytes(digest, "big")) % 2**256

    def add_table(self, table):
        """Arrow kernels are independent from DuckDB; Decimal combines batch sums."""
        import pyarrow as pa
        import pyarrow.compute as pc

        if not len(table):
            return
        self.rows += len(table)
        for name, count_name, total_name in (
            ("v", "volume_count", "volume"),
            ("n", "trade_count", "trades"),
        ):
            count = len(table) - table[name].null_count
            setattr(self, count_name, getattr(self, count_name) + count)
            if count:
                with localcontext() as context:
                    context.prec = 60
                    setattr(
                        self,
                        total_name,
                        getattr(self, total_name) + pc.sum(table[name]).as_py(),
                    )
        for name, attribute, operation in (
            ("t", "first", min),
            ("t", "last", max),
            ("l", "low", min),
            ("h", "high", max),
        ):
            extrema = pc.min_max(table[name]).as_py()
            value = extrema["min" if operation is min else "max"]
            previous = getattr(self, attribute)
            if value is not None:
                setattr(
                    self,
                    attribute,
                    value if previous is None else operation(previous, value),
                )
        self.up += (
            pc.sum(pc.cast(pc.greater(table["c"], table["o"]), pa.int64())).as_py() or 0
        )
        for ticker in pc.unique(table["ticker"]).to_pylist():
            times = pc.filter(table["t"], pc.equal(table["ticker"], ticker))
            self.tickers.add(ticker)
            self.ticker_rows[ticker] += len(times)
            previous = self.last_by_ticker.get(ticker)
            self.nonincreasing += previous is not None and times[0].as_py() <= previous
            if len(times) > 1:
                self.nonincreasing += pc.sum(
                    pc.cast(
                        pc.less_equal(times.slice(1), times.slice(0, len(times) - 1)),
                        pa.int64(),
                    )
                ).as_py()
            self.last_by_ticker[ticker] = times[-1].as_py()

    def snapshot(self):
        return {
            "aggregate": list(
                map(
                    scalar,
                    [
                        self.rows,
                        len(self.tickers),
                        self.first,
                        self.last,
                        self.volume if self.volume_count else None,
                        self.trades if self.trade_count else None,
                        self.low,
                        self.high,
                        self.up,
                    ],
                )
            ),
            "per_ticker_rows": dict(sorted(self.ticker_rows.items())),
            "strictly_increasing_timestamps_per_ticker": self.nonincreasing == 0,
            "nonincreasing_timestamp_count": self.nonincreasing,
            "multiset_sha256_sum": f"{self.multiset:064x}",
        }


def source_batches(path, batch_rows):
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            batch = []
            for record in reader:
                batch.append(record)
                if len(batch) == batch_rows:
                    yield batch
                    batch = []
            if batch:
                yield batch
    elif path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        for batch in pq.ParquetFile(path).iter_batches(
            batch_size=batch_rows, columns=list(COLUMNS)
        ):
            yield batch.to_pylist()
    else:
        raise ValueError(f"Unsupported source type: {path.suffix}")


def arrow_batches(path, schema, batch_rows):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.csv as arrow_csv
    import pyarrow.parquet as pq

    if path.suffix.lower() == ".csv":
        reader = arrow_csv.open_csv(
            path,
            read_options=arrow_csv.ReadOptions(block_size=max(65536, batch_rows * 128)),
            convert_options=arrow_csv.ConvertOptions(
                column_types=schema, include_columns=list(COLUMNS), null_values=[""]
            ),
        )
    elif path.suffix.lower() == ".parquet":
        reader = pq.ParquetFile(path).iter_batches(
            batch_size=batch_rows, columns=list(COLUMNS)
        )
    else:
        raise ValueError(f"Unsupported source type: {path.suffix}")
    for raw in reader:
        for offset in range(0, len(raw), batch_rows):
            batch = raw.slice(offset, batch_rows)
            if any(pa.types.is_floating(field.type) for field in batch.schema):
                rows = [
                    canonical_row(row, schema.field("v").type.scale)
                    for row in batch.to_pylist()
                ]
                table = pa.Table.from_arrays(
                    [
                        pa.array([row[i] for row in rows], type=field.type)
                        for i, field in enumerate(schema)
                    ],
                    schema=schema,
                )
            else:
                table = pa.Table.from_batches([batch]).cast(schema, safe=True)
            if (
                table["t"].null_count
                or table["ticker"].null_count
                or pc.any(pc.less(table["t"], 0)).as_py()
            ):
                raise ValueError("Missing ticker/t or negative timestamp")
            lengths = pc.utf8_length(table["ticker"])
            if pc.any(pc.or_(pc.equal(lengths, 0), pc.greater(lengths, 120))).as_py():
                raise ValueError("Invalid ticker length")
            yield table


def prepare(config):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    destination = Path(config["prepared"])
    destination.mkdir(parents=True, exist_ok=True)
    targets = sorted({size for n in config["rows"] for size in (n - 1, n)})
    schema = pa.schema(
        [
            ("ticker", pa.string()),
            ("t", pa.int64()),
            *[(name, pa.decimal128(38, config["decimal_scale"])) for name in DECIMALS],
        ]
    )
    writers, completed, sources = {}, {}, []
    all_rows, sampled = Oracle(), Oracle()
    maximum_batch = 0
    started = time.perf_counter()
    try:
        for target in targets:
            writers[target] = pq.ParquetWriter(
                destination / f"rows-{target}.parquet", schema, compression="zstd"
            )
        for filename in config["sources"]:
            path = Path(filename)
            initial = path.stat()
            provenance = {
                "path": str(path),
                "bytes": initial.st_size,
                "sha256": digest_file(path),
                "rows_used": 0,
            }
            sources.append(provenance)
            for table in arrow_batches(path, schema, config["batch_rows"]):
                table = table.slice(0, targets[-1] - all_rows.rows)
                if not len(table):
                    break
                maximum_batch = max(maximum_batch, len(table))
                before = all_rows.rows
                for target, writer in list(writers.items()):
                    length = min(len(table), target - before)
                    if length > 0:
                        writer.write_table(
                            table.slice(0, length), row_group_size=config["batch_rows"]
                        )
                    if target <= before + len(table):
                        writer.close()
                        del writers[target]
                boundaries = [
                    target - before
                    for target in targets
                    if before < target <= before + len(table)
                ]
                boundaries.append(len(table))
                start = 0
                for end in sorted(set(boundaries)):
                    segment = table.slice(start, end - start)
                    start = end
                    all_rows.add_table(segment)
                    minutes = pc.divide(segment["t"], 60000)
                    sample = pc.filter(
                        segment, pc.equal(pc.bit_wise_and(minutes, 63), 0)
                    )
                    for record in sample.to_pylist():
                        sampled.add(
                            tuple(record[name] for name in COLUMNS), hash_row=True
                        )
                    if all_rows.rows in targets:
                        target = all_rows.rows
                        output = destination / f"rows-{target}.parquet"
                        completed[str(target)] = {
                            "path": str(output),
                            "rows": target,
                            "bytes": output.stat().st_size,
                            "sha256": digest_file(output),
                            "oracle": all_rows.snapshot(),
                            "sample_oracle": sampled.snapshot(),
                        }
                provenance["rows_used"] += len(table)
                if all_rows.rows == targets[-1]:
                    break
            final = path.stat()
            if (initial.st_size, initial.st_mtime_ns) != (
                final.st_size,
                final.st_mtime_ns,
            ):
                raise ValueError(f"Source changed during preparation: {path.name}")
            if all_rows.rows == targets[-1]:
                break
        if all_rows.rows < targets[-1]:
            raise ValueError(
                f"Insufficient real rows: {all_rows.rows}; requested {targets[-1]}. No replication performed."
            )
    finally:
        for writer in writers.values():
            writer.close()
    return {
        "snapshots": completed,
        "sources": sources,
        "source_rows_used": all_rows.rows,
        "source_bytes": sum(item["bytes"] for item in sources),
        "maximum_batch_rows": maximum_batch,
        "elapsed_seconds": time.perf_counter() - started,
        "selection": "Prefix of sorted source paths and original row order; no replication, resampling, deduplication, or shuffled population claim.",
    }


def disk_bytes(path):
    total = 0
    for item in Path(path).rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except FileNotFoundError:
            pass
    return total


def resource_problem(output, disk_limit, free_minimum):
    if shutil.disk_usage(output).free < free_minimum:
        return "minimum_free_disk"
    if disk_bytes(output) > disk_limit:
        return "output_disk_limit"
    return None


def terminate_tree(process):
    import psutil

    try:
        processes = psutil.Process(process.pid).children(recursive=True)
        processes.append(psutil.Process(process.pid))
    except psutil.NoSuchProcess:
        return
    for child in reversed(processes):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=2)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=2)


def guarded(command, *, output, timeout, rss_limit, disk_limit, free_minimum, log):
    """Hard outer watchdog includes startup, queue waits, copies and descendants."""
    import psutil

    # Fail before spawning when the host sandbox prohibits process accounting.
    psutil.Process(os.getpid()).children(recursive=True)
    psutil.Process(os.getpid()).memory_info()

    problem = resource_problem(output, disk_limit, free_minimum)
    if problem:
        return {
            "status": problem,
            "elapsed_seconds": 0,
            "peak_sampled_tree_rss_bytes": 0,
        }
    started, peak, samples, next_disk = time.perf_counter(), 0, 0, 0
    with Path(log).open("w") as stream:
        process = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
        )
        while process.poll() is None:
            elapsed = time.perf_counter() - started
            try:
                parent = psutil.Process(process.pid)
                rss = 0
                for item in [parent, *parent.children(recursive=True)]:
                    try:
                        rss += item.memory_info().rss
                    except psutil.NoSuchProcess:
                        pass
                peak, samples = max(peak, rss), samples + 1
            except psutil.NoSuchProcess:
                pass
            problem = (
                "wall_timeout"
                if elapsed > timeout
                else "rss_limit"
                if peak > rss_limit
                else None
            )
            if elapsed >= next_disk:
                problem = problem or resource_problem(output, disk_limit, free_minimum)
                next_disk = elapsed + 1
            if problem:
                terminate_tree(process)
                break
            time.sleep(0.05)
        returncode = process.wait()
    return {
        "status": problem or ("ok" if returncode == 0 else "worker_failed"),
        "exit_code": returncode,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_sampled_tree_rss_bytes": peak,
        "rss_samples": samples,
        "rss_sampling_seconds": 0.05,
        "output_bytes_after_exit": disk_bytes(output),
        "free_disk_bytes_after_exit": shutil.disk_usage(output).free,
    }


def peak_rss():
    native = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return native if sys.platform == "darwin" else native * 1024


def check_aggregate(result, expected):
    if (
        result.get("preview_truncated")
        or result["columns"] != list(AGG_COLUMNS)
        or len(result["rows"]) != 1
    ):
        raise AssertionError(
            "Aggregate response is incomplete or has unexpected columns"
        )
    actual = result["rows"][0]
    for observed, wanted in zip(actual, expected["aggregate"], strict=True):
        if wanted is None:
            assert observed is None
        else:
            assert Decimal(str(observed)) == Decimal(str(wanted)), (observed, wanted)


def saved_sql(limit):
    order = ",".join(COLUMNS)
    return f"SELECT *, row_number() OVER (PARTITION BY ticker ORDER BY {order}) AS sequence FROM input1 WHERE {SAMPLE_PREDICATE} ORDER BY {order} LIMIT {limit + 1}"


def verify_saved(path, expected, batch_rows):
    import pyarrow.parquet as pq

    observed, sequences = Oracle(), defaultdict(int)
    last_key = None
    for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_rows):
        for item in batch.to_pylist():
            row = tuple(item[name] for name in COLUMNS)
            observed.add(row, hash_row=True)
            sequences[row[0]] += 1
            assert item["sequence"] == sequences[row[0]]
            key = (row[0], row[1])
            assert last_key is None or key >= last_key
            last_key = key
    actual = observed.snapshot()
    assert actual["aggregate"] == expected["aggregate"]
    assert actual["multiset_sha256_sum"] == expected["multiset_sha256_sum"]
    return {
        "rows": observed.rows,
        "multiset_sha256_sum": actual["multiset_sha256_sum"],
        "sequence_verified": True,
    }


def direct_query(config):
    import duckdb

    from agent_workbook.query import _preview

    started = time.perf_counter()
    with duckdb.connect(
        config={
            "threads": str(config["query_threads"]),
            "memory_limit": config["query_memory"],
            "autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false",
        }
    ) as db:
        source = config["source"].replace("'", "''")
        db.execute(f"CREATE VIEW input1 AS SELECT * FROM read_parquet('{source}')")
        timer = Timer(config["query_timeout"], db.interrupt)
        timer.start()
        try:
            if config.get("save_path"):
                db.sql(config["sql"]).write_parquet(config["save_path"])
                result = _preview(
                    db.execute(
                        "SELECT * FROM read_parquet(?) LIMIT 6", [config["save_path"]]
                    ),
                    5,
                )
            else:
                result = _preview(db.sql(config["sql"]).limit(6), 5)
        finally:
            timer.cancel()
            timer.join()
    measured = {
        "result": result,
        "elapsed_seconds": time.perf_counter() - started,
        "peak_process_rss_bytes": peak_rss(),
    }
    if config.get("save_path"):
        measured["saved_bytes"] = Path(config["save_path"]).stat().st_size
        measured["saved_sha256"] = digest_file(config["save_path"])
    return measured


class Recorder:
    def __init__(self, path):
        import tiktoken

        self.path = Path(path)
        self.encoders = {
            name: tiktoken.get_encoding(name) for name in ("cl100k_base", "o200k_base")
        }
        self.setup, self.calls = [], []

    def tokens(self, text):
        return {
            name: len(codec.encode(text, disallowed_special=()))
            for name, codec in self.encoders.items()
        }

    async def call(self, client, phase, name, arguments, *, expect_error=False):
        from agent_workbook.render import decode

        started = time.perf_counter()
        result = await client.call_tool(name, arguments)
        text = "\n".join(block.text for block in result.content if block.type == "text")
        record = {
            "phase": phase,
            "tool": name,
            "arguments": arguments,
            "elapsed_seconds": time.perf_counter() - started,
            "request_tokens": self.tokens(
                compact({"name": name, "arguments": arguments})
            ),
            "reply_tokens": self.tokens(text),
            "reply_bytes": len(text.encode()),
            "wire_tokens": self.tokens(
                result.model_dump_json(by_alias=True, exclude_none=True)
            ),
            "is_error": bool(result.isError),
            "result": result.model_dump(by_alias=True, exclude_none=True),
        }
        self.calls.append(record)
        self.flush()
        if bool(result.isError) != expect_error:
            raise AssertionError(f"Unexpected MCP result in {phase}: {text[:1000]}")
        return {"error": text} if result.isError else decode(result.content)

    def flush(self):
        write_json(self.path, {"setup": self.setup, "calls": self.calls})

    def summary(self):
        return {
            "tool_calls": len(self.calls),
            "cold_setup_tokens": {
                name: sum(item["tokens"][name] for item in self.setup)
                for name in self.encoders
            },
            "request_tokens": {
                name: sum(item["request_tokens"][name] for item in self.calls)
                for name in self.encoders
            },
            "reply_tokens": {
                name: sum(item["reply_tokens"][name] for item in self.calls)
                for name in self.encoders
            },
            "maximum_reply_bytes": max(
                (item["reply_bytes"] for item in self.calls), default=0
            ),
        }


def serve_child(config):
    from agent_workbook.server import server_for
    from agent_workbook.settings import Settings
    from agent_workbook.store import Workbook

    state = Path(config["server_state"])
    write_json(state, {"pid": os.getpid(), "status": "starting"})
    settings = Settings(
        query_timeout=config["query_timeout"],
        query_memory=config["query_memory"],
        query_threads=config["query_threads"],
    )
    app = server_for(
        Workbook(Path(config["workspace"]), settings),
        tools=TOOLS,
        response_format="adaptive",
        response_content="text",
    )
    try:
        app.run(transport="stdio")
    finally:
        write_json(
            state,
            {
                "pid": os.getpid(),
                "status": "exited",
                "peak_process_rss_bytes": peak_rss(),
            },
        )


@asynccontextmanager
async def mcp_session(config, recorder, label):
    import psutil
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    directory = Path(config["case_directory"])
    server_config = dict(config, server_state=str(directory / f"{label}-server.json"))
    path = directory / f"{label}-server-config.json"
    write_json(path, server_config)
    peak, samples, stop = 0, 0, asyncio.Event()

    async def monitor(pid):
        nonlocal peak, samples
        process = psutil.Process(pid)
        while not stop.is_set():
            try:
                peak = max(peak, process.memory_info().rss)
                samples += 1
            except psutil.NoSuchProcess:
                return
            await asyncio.sleep(0.025)

    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), "--server-config", str(path)],
        env=dict(os.environ),
    )
    started = time.perf_counter()
    with (directory / f"{label}-server.log").open("w") as errors:
        async with (
            stdio_client(parameters, errlog=errors) as (read, write),
            ClientSession(read, write) as client,
        ):
            initialized = await client.initialize()
            definitions = await client.list_tools()
            assert {tool.name for tool in definitions.tools} == set(TOOLS)
            setup = (
                (initialized.instructions or "")
                + "\n"
                + compact(
                    [
                        tool.model_dump(by_alias=True, exclude_none=True)
                        for tool in definitions.tools
                    ]
                )
            )
            recorder.setup.append(
                {
                    "session": label,
                    "text": setup,
                    "tokens": recorder.tokens(setup),
                    "startup_seconds": time.perf_counter() - started,
                }
            )
            recorder.flush()
            pid = json.loads(Path(server_config["server_state"]).read_text())["pid"]
            task = asyncio.create_task(monitor(pid))
            try:
                yield client
            finally:
                stop.set()
                await task
    state = json.loads(Path(server_config["server_state"]).read_text())
    write_json(
        directory / f"{label}-rss.json",
        {
            **state,
            "peak_sampled_server_rss_bytes": peak,
            "samples": samples,
            "sampling_seconds": 0.025,
        },
    )


async def case_workflow(config):
    directory = Path(config["case_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(directory / "mcp-traces.json")
    original, updated = config["original"], config["updated"]
    verified, direct = {}, {}
    source_name, saved_name, checkpoint_name = (
        "stress/bars",
        "stress/window",
        "stress/checkpoint",
    )
    sample_count = original["sample_oracle"]["aggregate"][0]
    if sample_count > config["saved_rows_cap"]:
        raise ValueError(f"Saved-output cap exceeded by oracle: {sample_count}")
    query_sql = saved_sql(config["saved_rows_cap"])

    async with mcp_session(config, recorder, "primary") as client:
        imported = await recorder.call(
            client, "import", "import", {"path": original["path"], "name": source_name}
        )
        source_id = imported["artifact_id"]
        result = await recorder.call(
            client, "aggregate", "query", {"inputs": [source_id], "sql": AGG_SQL}
        )
        check_aggregate(result, original["oracle"])
        verified["aggregate"] = True

        direct["aggregate"] = await asyncio.to_thread(
            direct_query, dict(config, source=original["path"], sql=AGG_SQL)
        )
        check_aggregate(direct["aggregate"]["result"], original["oracle"])

        started = time.perf_counter()
        simultaneous = await asyncio.gather(
            *[
                recorder.call(
                    client,
                    f"concurrent_{index}",
                    "query",
                    {"inputs": [source_id], "sql": AGG_SQL},
                )
                for index in range(config["concurrent_reads"])
            ]
        )
        concurrency = {
            "requests": len(simultaneous),
            "elapsed_seconds": time.perf_counter() - started,
            "scope": "Concurrent requests to one server; current runtime serializes SQL using QUERY_SLOT.",
        }
        for result in simultaneous:
            check_aggregate(result, original["oracle"])
        verified["concurrent_reads"] = True

        saved = await recorder.call(
            client,
            "save_window",
            "query",
            {"inputs": [source_id], "sql": query_sql, "save_as": saved_name},
        )
        assert saved["saved"]["version"] == 1
        saved_id = saved["saved"]["artifact_id"]
        sampled = await recorder.call(
            client, "check_saved", "query", {"inputs": [saved_id], "sql": AGG_SQL}
        )
        check_aggregate(sampled, original["sample_oracle"])
        metadata = await recorder.call(
            client, "saved_metadata", "inspect", {"ref": saved_id, "full": True}
        )
        saved_path = Path(config["workspace"]) / "blobs" / metadata["sha256"]
        verified["saved_rows"] = await asyncio.to_thread(
            verify_saved, saved_path, original["sample_oracle"], config["batch_rows"]
        )

        direct_path = directory / "direct-window.parquet"
        direct["save_window"] = await asyncio.to_thread(
            direct_query,
            dict(
                config,
                source=original["path"],
                sql=query_sql,
                save_path=str(direct_path),
            ),
        )
        verified["direct_saved_rows"] = await asyncio.to_thread(
            verify_saved, direct_path, original["sample_oracle"], config["batch_rows"]
        )
        checkpoint = {
            "objective": "Retain verified sampled minute-bar results",
            "verified": f"Exact aggregate and {sample_count} saved rows checked",
            "next_step": "Check freshness before reusing the saved result",
        }
        saved_checkpoint = await recorder.call(
            client,
            "save_checkpoint",
            "save",
            {
                "name": checkpoint_name,
                "kind": "checkpoint",
                "checkpoint": checkpoint,
                "dependencies": [saved_id],
            },
        )
        resumed = await recorder.call(
            client, "resume", "resume", {"text": checkpoint_name}
        )
        assert resumed["checkpoint"]["artifact_id"] == saved_checkpoint["artifact_id"]
        assert not resumed["checkpoint"]["stale_inputs"]
        assert resumed["checkpoint"]["dependencies"] == [saved_id]

        update = await recorder.call(
            client,
            "update_source",
            "import",
            {"path": updated["path"], "name": source_name, "expected_version": 1},
        )
        assert update["version"] == 2
        stale = await recorder.call(client, "stale_saved", "inspect", {"ref": saved_id})
        assert stale["stale_inputs"] and stale["is_latest"]
        resumed = await recorder.call(
            client, "resume_stale", "resume", {"text": checkpoint_name}
        )
        assert resumed["checkpoint"]["stale_inputs"]
        await recorder.call(
            client,
            "reject_stale_query",
            "query",
            {"inputs": [saved_id], "sql": AGG_SQL},
            expect_error=True,
        )
        historical = await recorder.call(
            client,
            "explicit_historical_query",
            "query",
            {"inputs": [saved_id], "sql": AGG_SQL, "allow_historical": True},
        )
        check_aggregate(historical, original["sample_oracle"])

        refreshed = await recorder.call(
            client, "refresh", "refresh", {"ref": saved_name}
        )
        current_id = refreshed["saved"]["artifact_id"]
        assert refreshed["saved"]["version"] == 2 and current_id != saved_id
        result = await recorder.call(
            client, "check_refreshed", "query", {"inputs": [current_id], "sql": AGG_SQL}
        )
        check_aggregate(result, updated["sample_oracle"])
        metadata = await recorder.call(
            client, "refreshed_metadata", "inspect", {"ref": current_id, "full": True}
        )
        assert not metadata["stale_inputs"]
        verified["refreshed_rows"] = await asyncio.to_thread(
            verify_saved,
            Path(config["workspace"]) / "blobs" / metadata["sha256"],
            updated["sample_oracle"],
            config["batch_rows"],
        )
        resumed = await recorder.call(
            client, "old_checkpoint_stays_pinned", "resume", {"text": checkpoint_name}
        )
        assert resumed["checkpoint"]["stale_inputs"] and resumed["checkpoint"][
            "dependencies"
        ] == [saved_id]
        await recorder.call(
            client,
            "advance_checkpoint",
            "save",
            {
                "name": checkpoint_name,
                "kind": "checkpoint",
                "checkpoint": checkpoint,
                "dependencies": [current_id],
                "expected_version": 1,
            },
        )
        resumed = await recorder.call(
            client, "resume_current", "resume", {"text": checkpoint_name}
        )
        assert not resumed["checkpoint"]["stale_inputs"] and resumed["checkpoint"][
            "dependencies"
        ] == [current_id]
        verified["staleness_refresh_and_manual_checkpoint_advance"] = True

    timeout_config = dict(config, query_timeout=0.05)
    async with mcp_session(timeout_config, recorder, "timeout_probe") as client:
        error = await recorder.call(
            client,
            "bounded_pathological_query",
            "query",
            {
                "inputs": [source_name],
                "sql": "SELECT sum(sin(a.i+b.i)) FROM range(1000000) a(i) CROSS JOIN range(1000000) b(i)",
            },
            expect_error=True,
        )
        assert "interrupt" in error["error"].lower(), error
        recovery = await recorder.call(
            client,
            "timeout_recovery",
            "query",
            {"inputs": [source_name], "sql": "SELECT count(*) AS n FROM input1"},
        )
        assert recovery["rows"] == [[updated["rows"]]]
        verified["timeout_and_same_server_recovery"] = True
    return {
        "status": "passed",
        "rows": original["rows"],
        "prepared_bytes": original["bytes"],
        "updated_rows": updated["rows"],
        "verified": verified,
        "direct_duckdb": direct,
        "concurrent_reads": concurrency,
        "tokens": recorder.summary(),
        "phases": [
            {
                key: value
                for key, value in item.items()
                if key not in {"arguments", "result"}
            }
            for item in recorder.calls
        ],
        "server_memory": {
            label: json.loads((directory / f"{label}-rss.json").read_text())
            for label in ("primary", "timeout_probe")
        },
        "worker_peak_rss_bytes": peak_rss(),
        "workspace_bytes": disk_bytes(config["workspace"]),
    }


def run_worker(config, role, report_path):
    import traceback

    try:
        if role == "prepare":
            result = prepare(config)
        elif role == "case":
            result = asyncio.run(case_workflow(config))
        else:
            raise ValueError("Unknown worker role")
        write_json(report_path, result)
    except Exception as error:
        write_json(
            report_path,
            {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            },
        )
        raise


def main(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sources = sorted({str(path.resolve()) for path in args.sources})
    if not sources or any(not Path(path).is_file() for path in sources):
        raise ValueError("Provide existing immutable CSV/Parquet sources")
    if any(n < 2 for n in args.rows) or not 0 <= args.decimal_scale <= 18:
        raise ValueError(
            "Scales require at least two rows; decimal scale must be 0..18"
        )
    if not 1 <= args.concurrent_reads <= 8 or not 1 <= args.batch_rows <= 65536:
        raise ValueError("Concurrency must be1..8 and batch rows1..65536")
    source_root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), *sorted((source_root / "src/agent_workbook").glob("*.py"))]
    source_hashes = {
        str(path.relative_to(source_root)): digest_file(path) for path in paths
    }
    config = {
        "sources": sources,
        "rows": sorted(set(args.rows)),
        "prepared": str(output / "prepared"),
        "decimal_scale": args.decimal_scale,
        "batch_rows": args.batch_rows,
        "query_timeout": args.query_timeout,
        "query_memory": args.query_memory,
        "query_threads": args.query_threads,
        "concurrent_reads": args.concurrent_reads,
        "saved_rows_cap": args.saved_rows_cap,
    }
    report = {
        "settings": config,
        "source_sha256": source_hashes,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ("mcp", "duckdb", "pyarrow", "psutil", "tiktoken")
            },
        },
        "scope": "Real local rows, no replication or model calls. Local computation/IO versus direct DuckDB, not a raw-corpus model-latency claim. MCP payload counts are not provider billing. Canonical decimal conversion is exact or fails.",
        "limits": {
            "case_seconds": args.case_seconds,
            "preparation_seconds": args.preparation_seconds,
            "tree_rss_bytes": int(args.rss_gib * GIB),
            "output_bytes": int(args.disk_gib * GIB),
            "minimum_free_bytes": int(args.free_gib * GIB),
        },
        "timing_limits": "Import/staging touch files: timings use uncontrolled/warm OS caches, never claimed as cold-disk or statistical speedup. Direct SELECT uses identical SQL, fresh connections, memory/threads and preview limit; direct materialization omits Workspool catalog/version copy. Worker/direct RSS and server RSS are distinct processes. Sampled tree RSS includes descendants, may miss peaks, and allows polling-interval overshoot. Native ru_maxrss is reported separately.",
        "cases": [],
    }

    def launch(role, configuration, directory, seconds):
        directory.mkdir(parents=True, exist_ok=True)
        path, result_path = directory / "config.json", directory / "worker-result.json"
        write_json(path, configuration)
        guard = guarded(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                role,
                "--config",
                str(path),
                "--worker-report",
                str(result_path),
            ],
            output=output,
            timeout=seconds,
            rss_limit=report["limits"]["tree_rss_bytes"],
            disk_limit=report["limits"]["output_bytes"],
            free_minimum=report["limits"]["minimum_free_bytes"],
            log=directory / "worker.log",
        )
        result = (
            json.loads(result_path.read_text())
            if result_path.exists()
            else {"status": "incomplete"}
        )
        return {"guard": guard, "result": result}

    report["preparation"] = launch(
        "prepare", config, output / "preparation", args.preparation_seconds
    )
    write_json(output / "results.json", report)
    prepared = report["preparation"]["result"]
    if report["preparation"]["guard"]["status"] != "ok":
        print(
            compact({"status": "preparation_failed", "output": str(output)}), flush=True
        )
        return 1
    for rows in config["rows"]:
        directory = output / f"rows-{rows}"
        case_config = dict(
            config,
            case_directory=str(directory),
            workspace=str(directory / "workbook"),
            original=prepared["snapshots"][str(rows)],
            updated=prepared["snapshots"][str(rows - 1)],
        )
        case = launch("case", case_config, directory, args.case_seconds)
        case["rows"] = rows
        report["cases"].append(case)
        report["source_files_unchanged"] = all(
            digest_file(source_root / name) == value
            for name, value in source_hashes.items()
        )
        write_json(output / "results.json", report)
        print(
            compact(
                {
                    "rows": rows,
                    "guard": case["guard"]["status"],
                    "status": case["result"].get("status"),
                    "elapsed": case["guard"]["elapsed_seconds"],
                }
            ),
            flush=True,
        )
        if not args.keep_workspaces and Path(case_config["workspace"]).exists():
            shutil.rmtree(case_config["workspace"])
        if case["guard"]["status"] in {
            "minimum_free_disk",
            "output_disk_limit",
            "rss_limit",
        }:
            break
    report["output_bytes_after_cleanup"] = disk_bytes(output)
    write_json(output / "results.json", report)
    return (
        0
        if all(
            case["guard"]["status"] == "ok" and case["result"].get("status") == "passed"
            for case in report["cases"]
        )
        and report["source_files_unchanged"]
        else 1
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", nargs="+", type=Path)
    parser.add_argument(
        "--rows",
        nargs="+",
        type=int,
        default=[1_000_000, 5_000_000, 10_000_000, 25_000_000],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--batch-rows", type=int, default=16384)
    parser.add_argument("--decimal-scale", type=int, default=12)
    parser.add_argument("--query-timeout", type=float, default=15)
    parser.add_argument("--query-memory", default="1GB")
    parser.add_argument("--query-threads", type=int, default=2)
    parser.add_argument("--case-seconds", type=float, default=180)
    parser.add_argument("--preparation-seconds", type=float, default=1800)
    parser.add_argument("--rss-gib", type=float, default=4)
    parser.add_argument("--disk-gib", type=float, default=40)
    parser.add_argument("--free-gib", type=float, default=10)
    parser.add_argument("--concurrent-reads", type=int, default=3)
    parser.add_argument("--saved-rows-cap", type=int, default=1_000_000)
    parser.add_argument("--keep-workspaces", action="store_true")
    parser.add_argument("--worker", choices=["prepare", "case"], help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--server-config", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.server_config:
        serve_child(json.loads(options.server_config.read_text()))
    elif options.worker:
        run_worker(
            json.loads(options.config.read_text()),
            options.worker,
            options.worker_report,
        )
    elif not options.sources or options.output is None:
        parser.error("--sources and --output are required")
    else:
        sys.exit(main(options))
