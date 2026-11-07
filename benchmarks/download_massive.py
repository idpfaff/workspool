"""Download real adjusted minute bars into resumable, validated per-symbol CSVs.

Credentials are read only at explicit execution. No live requests occur on import.
Output belongs in an ignored local directory; market data is not committed.
"""

import argparse
import asyncio
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx

ORIGIN = "https://api.massive.com"
FIELDS = ("ticker", "t", "o", "h", "l", "c", "v", "n")
MAX_BYTES = 10 * 1024**3
MIN_FREE = 40 * 1024**3
MAX_PAGE_BYTES = 64 * 1024**2
MAX_PAGES = 10000


class DownloadError(RuntimeError):
    def __init__(self, code, *, fatal=False):
        super().__init__(code)
        self.fatal = fatal


def load_key(env_file=None, environ=None):
    environment = os.environ if environ is None else environ
    if env_file is None:
        key = environment.get("MASSIVE_API_KEY", "")
    else:
        path = Path(env_file)
        if path.is_symlink() or not path.is_file():
            raise DownloadError("invalid_key_file")
        values = []
        for line in path.read_text().splitlines():
            if re.match(r"^\s*(?:export\s+)?MASSIVE_API_KEY\s*=", line):
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                values.append(value)
        if len(values) != 1:
            raise DownloadError("one_literal_massive_key_required")
        key = values[0]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", key):
        raise DownloadError("invalid_massive_key")
    return key


def dates(start, end):
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in (start, end)):
        raise DownloadError("invalid_date")
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last:
            raise ValueError
        eastern = ZoneInfo("America/New_York")
        low = int(
            datetime.combine(first, datetime.min.time(), eastern).timestamp() * 1000
        )
        high = int(
            datetime.combine(
                last + timedelta(days=1), datetime.min.time(), eastern
            ).timestamp()
            * 1000
        )
        return low, high
    except (ValueError, OverflowError):
        raise DownloadError("invalid_date_range") from None


def symbols(tickers, tickers_file=None):
    values = list(tickers or [])
    if tickers_file is not None:
        values += Path(tickers_file).read_text().split()
    values = [value.strip().upper() for value in values]
    if not values or len(values) > 1000 or len(set(values)) != len(values):
        raise DownloadError("provide_1_to_1000_distinct_tickers")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,19}", value) for value in values):
        raise DownloadError("invalid_stock_ticker")
    return values


def initial_url(ticker, start, end):
    return f"{ORIGIN}/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}?adjusted=true&sort=asc&limit=50000"


def next_url(value, ticker, start, end):
    """Never forward credentials or pagination to another host/endpoint."""
    if not isinstance(value, str) or len(value) > 8192:
        raise DownloadError("invalid_pagination_url")
    try:
        parsed = urlsplit(value)
        valid_origin = (
            parsed.scheme == "https"
            and parsed.hostname == "api.massive.com"
            and parsed.port in (None, 443)
        )
    except ValueError:
        raise DownloadError("invalid_pagination_url") from None
    prefix = f"/v2/aggs/ticker/{ticker}/range/1/minute/"
    if (
        not valid_origin
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not parsed.path.startswith(prefix)
    ):
        raise DownloadError("unsafe_pagination_url")
    parts = parsed.path[len(prefix) :].split("/")
    if len(parts) != 2 or parts[1] != end:
        raise DownloadError("pagination_changed_range")
    low, high = dates(start, end)
    if parts[0].isdigit():
        if not low <= int(parts[0]) < high:
            raise DownloadError("pagination_changed_range")
    else:
        page_low, _ = dates(parts[0], end)
        if not low <= page_low < high:
            raise DownloadError("pagination_changed_range")
    query = []
    seen = set()
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in {"apikey", "api_key"}:
            continue
        if key not in {"cursor", "adjusted", "sort", "limit"} or key in seen:
            raise DownloadError("invalid_pagination_parameters")
        seen.add(key)
        if key == "cursor":
            query.append((key, item))
        elif (
            (key == "adjusted" and item != "true")
            or (key == "sort" and item != "asc")
            or (key == "limit" and item != "50000")
        ):
            raise DownloadError("pagination_changed_parameters")
    return (
        ORIGIN
        + parsed.path
        + "?"
        + urlencode(query + [("adjusted", "true"), ("sort", "asc"), ("limit", "50000")])
    )


def retry_delay(header, attempt):
    delay = min(2**attempt, 8)
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                delay = (
                    parsedate_to_datetime(header) - datetime.now(UTC)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    return max(0.0, min(delay, 30.0))


class RateLimit:
    def __init__(self):
        self.lock, self.last = asyncio.Lock(), 0.0

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.last + 0.125 - time.monotonic()))
            self.last = time.monotonic()


class Budget:
    def __init__(self, limit=MAX_BYTES):
        self.limit, self.downloaded, self.written = limit, 0, 0

    def consume(self, count, kind):
        total = getattr(self, kind) + count
        if total > self.limit:
            raise DownloadError(f"{kind}_byte_cap", fatal=True)
        setattr(self, kind, total)


def invalid_constant(_):
    raise ValueError("nonfinite_json_number")


async def page(client, url, key, limiter, budget, stats):
    for attempt in range(4):
        await limiter.wait()
        stats["requests"] += 1
        try:
            async with client.stream(
                "GET", url, headers={"Authorization": "Bearer " + key}
            ) as response:
                status = response.status_code
                if status == 429 or 500 <= status <= 599:
                    if attempt == 3:
                        raise DownloadError(f"http_{status}")
                    delay = retry_delay(response.headers.get("Retry-After"), attempt)
                elif status != 200:
                    raise DownloadError(f"http_{status}", fatal=status in (401, 403))
                else:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        budget.consume(len(chunk), "downloaded")
                        if len(data) + len(chunk) > MAX_PAGE_BYTES:
                            raise DownloadError("page_byte_cap", fatal=True)
                        data.extend(chunk)
                    try:
                        result = json.loads(
                            data, parse_float=Decimal, parse_constant=invalid_constant
                        )
                    except (ValueError, UnicodeError):
                        raise DownloadError("invalid_json") from None
                    if not isinstance(result, dict) or result.get("status") not in {
                        "OK",
                        "DELAYED",
                    }:
                        raise DownloadError("invalid_api_status")
                    return result
        except httpx.HTTPError:
            if attempt == 3:
                raise DownloadError("network_error") from None
            delay = min(2**attempt, 8)
        await asyncio.sleep(delay)
    raise DownloadError("retry_limit")


def bar_row(ticker, bar, previous, low, high):
    if not isinstance(bar, dict):
        raise DownloadError("invalid_bar")
    stamp = bar.get("t")
    if (
        type(stamp) is not int
        or not low <= stamp < high
        or (previous is not None and stamp <= previous)
    ):
        raise DownloadError("invalid_or_nonincreasing_timestamp")
    values = {}
    for field in ("o", "h", "l", "c", "v"):
        value = bar.get(field)
        if type(value) not in (int, Decimal) or not Decimal(value).is_finite():
            raise DownloadError("invalid_numeric_bar")
        values[field] = value
    if (
        not values["l"]
        <= min(values["o"], values["c"])
        <= max(values["o"], values["c"])
        <= values["h"]
        or values["v"] < 0
    ):
        raise DownloadError("invalid_ohlc_or_volume")
    count = bar.get("n")
    if count is not None and (type(count) is not int or count < 0):
        raise DownloadError("invalid_transaction_count")
    return [
        ticker,
        stamp,
        *(str(values[field]) for field in ("o", "h", "l", "c", "v")),
        "" if count is None else count,
    ]


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def completed(output, ticker, start, end):
    path, manifest = output / (ticker + ".csv"), output / (ticker + ".manifest.json")
    if (
        not path.is_file()
        or not manifest.is_file()
        or path.is_symlink()
        or manifest.is_symlink()
    ):
        return None
    try:
        saved = json.loads(manifest.read_text())
        expected = {
            "ticker": ticker,
            "from_date": start,
            "to_date": end,
            "adjusted": True,
            "columns": list(FIELDS),
            "status": "complete",
        }
        if any(saved.get(key) != value for key, value in expected.items()):
            return None
        if path.stat().st_size != saved["bytes"] or file_sha(path) != saved["sha256"]:
            return None
        return saved
    except (OSError, ValueError, KeyError, TypeError):
        return None


class CsvOutput:
    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget
        self.digest, self.size = hashlib.sha256(), 0

    def write(self, value):
        raw = value.encode("utf-8")
        self.budget.consume(len(raw), "written")
        self.stream.write(raw)
        self.digest.update(raw)
        self.size += len(raw)
        return len(value)


async def download(
    client, output, ticker, start, end, key, limiter, budget, progress=print
):
    saved = completed(output, ticker, start, end)
    if saved is not None:
        budget.consume(saved["bytes"], "written")
        return saved | {"resumed": True}
    low, high = dates(start, end)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{ticker}-", suffix=".partial", dir=output
    )
    stats = {"requests": 0}
    rows, previous, first, missing_n, pages = 0, None, None, 0, 0
    seen, url = set(), initial_url(ticker, start, end)
    last_progress = time.monotonic()
    try:
        with os.fdopen(descriptor, "wb") as stream:
            target = CsvOutput(stream, budget)
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(FIELDS)
            while url:
                if url in seen or pages >= MAX_PAGES:
                    raise DownloadError("pagination_loop_or_limit")
                seen.add(url)
                result = await page(client, url, key, limiter, budget, stats)
                if (
                    result.get("ticker", ticker) != ticker
                    or result.get("adjusted", True) is not True
                ):
                    raise DownloadError("response_identity_or_adjustment_mismatch")
                bars = result.get("results", [])
                if not isinstance(bars, list) or len(bars) > 50000:
                    raise DownloadError("invalid_result_list")
                for bar in bars:
                    row = bar_row(ticker, bar, previous, low, high)
                    writer.writerow(row)
                    previous = row[1]
                    first = previous if first is None else first
                    missing_n += row[-1] == ""
                    rows += 1
                pages += 1
                if time.monotonic() - last_progress >= 10:
                    progress(
                        json.dumps(
                            {
                                "ticker": ticker,
                                "status": "downloading",
                                "pages": pages,
                                "rows": rows,
                                "requests": stats["requests"],
                            }
                        )
                    )
                    last_progress = time.monotonic()
                continuation = result.get("next_url")
                url = (
                    next_url(continuation, ticker, start, end) if continuation else None
                )
                if shutil.disk_usage(output).free < 1024**3:
                    raise DownloadError("disk_space_low", fatal=True)
            stream.flush()
            os.fsync(stream.fileno())
        saved = {
            "status": "complete",
            "ticker": ticker,
            "from_date": start,
            "to_date": end,
            "adjusted": True,
            "columns": list(FIELDS),
            "rows": rows,
            "first_timestamp": first,
            "last_timestamp": previous,
            "missing_transaction_count": missing_n,
            "pages": pages,
            "requests": stats["requests"],
            "bytes": target.size,
            "sha256": target.digest.hexdigest(),
            "file": ticker + ".csv",
        }
        os.replace(temporary, output / (ticker + ".csv"))
        atomic_json(output / (ticker + ".manifest.json"), saved)
        return saved
    finally:
        Path(temporary).unlink(missing_ok=True)


async def run(args):
    key = load_key(args.env_file)
    tickers = symbols(args.tickers, args.tickers_file)
    dates(args.from_date, args.to_date)
    args.output.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(args.output).free < MIN_FREE:
        raise DownloadError("need_40GiB_free_space")
    if not 1 <= args.workers <= 4:
        raise DownloadError("workers_must_be_1_to_4")
    limiter, budget, stopped = RateLimit(), Budget(), asyncio.Event()
    semaphore, write_lock = asyncio.Semaphore(args.workers), asyncio.Lock()
    report = {
        "source": "Massive adjusted 1-minute stock aggregates",
        "from_date": args.from_date,
        "to_date": args.to_date,
        "tickers": tickers,
        "workers": args.workers,
        "max_requests_per_second": 8,
        "download_byte_cap": MAX_BYTES,
        "symbols": {},
    }
    async with httpx.AsyncClient(
        timeout=60,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=args.workers),
    ) as client:

        async def worker(ticker):
            async with semaphore:
                try:
                    if stopped.is_set():
                        raise DownloadError("cancelled_after_fatal_error")
                    result = await download(
                        client,
                        args.output,
                        ticker,
                        args.from_date,
                        args.to_date,
                        key,
                        limiter,
                        budget,
                        progress=lambda message: print(message, flush=True),
                    )
                except DownloadError as error:
                    result = {"status": "failed", "ticker": ticker, "error": str(error)}
                    if error.fatal:
                        stopped.set()
                except OSError:
                    result = {
                        "status": "failed",
                        "ticker": ticker,
                        "error": "filesystem_error",
                    }
                    stopped.set()
                async with write_lock:
                    report["symbols"][ticker] = result
                    report["downloaded_bytes"] = budget.downloaded
                    report["written_or_resumed_bytes"] = budget.written
                    report["complete"] = len(report["symbols"]) == len(tickers) and all(
                        item["status"] == "complete"
                        for item in report["symbols"].values()
                    )
                    atomic_json(args.output / "manifest.json", report)
                print(
                    json.dumps(
                        {
                            key: result[key]
                            for key in (
                                "ticker",
                                "status",
                                "rows",
                                "requests",
                                "error",
                                "resumed",
                            )
                            if key in result
                        }
                    ),
                    flush=True,
                )

        await asyncio.gather(*(worker(ticker) for ticker in tickers))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--tickers-file", type=Path)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / ".download.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = asyncio.run(run(args))
    except DownloadError as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(
            json.dumps({"status": "failed", "error": "local_setup_or_lock_error"}),
            file=sys.stderr,
        )
        return 1
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
