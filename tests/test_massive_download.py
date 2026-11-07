"""Massive downloader tests use local HTTP mocks, never production credentials."""

import asyncio
import csv
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "massive_download", Path(__file__).parents[1] / "benchmarks/download_massive.py"
)
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)
KEY = "test-key-" + "x" * 24
START, END = "2024-01-01", "2025-12-31"
LOW, HIGH = download.dates(START, END)


class NoWait:
    async def wait(self):
        pass


def bar(stamp=LOW, **updates):
    return {"t": stamp, "o": 10, "h": 12, "l": 9, "c": 11, "v": 15, "n": 2} | updates


def payload(bars, next_url=None):
    value = {"status": "OK", "ticker": "AAPL", "adjusted": True, "results": bars}
    if next_url:
        value["next_url"] = next_url
    return value


def execute(tmp_path, handler, *, limit=download.MAX_BYTES):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await download.download(
                client,
                tmp_path,
                "AAPL",
                START,
                END,
                KEY,
                NoWait(),
                download.Budget(limit),
                progress=lambda _: None,
            )

    return asyncio.run(run())


def test_real_decimal_text_pagination_atomic_csv_and_verified_resume(tmp_path):
    requests = []
    continuation = f"{download.ORIGIN}/v2/aggs/ticker/AAPL/range/1/minute/{LOW + 60000}/{END}?adjusted=true&sort=asc&limit=50000&apiKey={KEY}"

    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert KEY not in str(request.url)
        if len(requests) == 1:
            content = json.dumps(payload([bar()], continuation)).replace(
                '"o": 10', '"o": 10.123456789012345678901'
            )
            return httpx.Response(200, content=content)
        return httpx.Response(200, json=payload([bar(LOW + 60000, n=None)]))

    result = execute(tmp_path, handler)
    assert result["rows"] == result["pages"] == result["requests"] == 2
    assert result["missing_transaction_count"] == 1
    assert result["sha256"] == download.file_sha(tmp_path / "AAPL.csv")
    with (tmp_path / "AAPL.csv").open() as stream:
        reader = csv.DictReader(stream)
        rows = list(reader)
        assert reader.fieldnames == list(download.FIELDS)
    assert rows[0]["o"] == "10.123456789012345678901"
    assert rows[1]["n"] == ""
    assert not list(tmp_path.glob("*.partial")) and not list(
        tmp_path.glob(".*.partial")
    )
    assert KEY not in (tmp_path / "AAPL.manifest.json").read_text()

    def forbidden(_):
        pytest.fail("A verified finished file must resume without HTTP")

    resumed = execute(tmp_path, forbidden)
    assert resumed["resumed"] is True and resumed["sha256"] == result["sha256"]


def test_corrupted_finished_file_is_redownloaded(tmp_path):
    execute(tmp_path, lambda _: httpx.Response(200, json=payload([bar()])))
    (tmp_path / "AAPL.csv").write_text("corrupt")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload([bar()]))

    result = execute(tmp_path, handler)
    assert len(calls) == 1 and not result.get("resumed")
    assert result["sha256"] == download.file_sha(tmp_path / "AAPL.csv")


@pytest.mark.parametrize(
    "changes",
    [
        {"h": 9},
        {"l": 12},
        {"o": None},
        {"c": Decimal("NaN")},
        {"v": -1},
        {"n": True},
        {"n": Decimal("2.1")},
        {"t": True},
        {"t": LOW - 1},
        {"t": HIGH},
    ],
)
def test_invalid_bar_is_never_silently_skipped(changes):
    with pytest.raises(download.DownloadError):
        download.bar_row("AAPL", bar(**changes), None, LOW, HIGH)


def test_duplicate_or_out_of_order_timestamps_abort_without_final_file(tmp_path):
    with pytest.raises(download.DownloadError, match="nonincreasing"):
        execute(tmp_path, lambda _: httpx.Response(200, json=payload([bar(), bar()])))
    assert not (tmp_path / "AAPL.csv").exists()
    assert not (tmp_path / "AAPL.manifest.json").exists()
    assert not list(tmp_path.glob(".*.partial"))


@pytest.mark.parametrize(
    "url",
    [
        "https://[invalid",
        "https://evil.example/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31",
        "http://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31",
        "https://api.massive.com:444/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31",
        "https://user:pass@api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31",
        "https://api.massive.com/v2/aggs/ticker/MSFT/range/1/minute/2024-01-01/2025-12-31",
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/day/2024-01-01/2025-12-31",
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2023-01-01/2025-12-31",
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31?sort=desc",
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31?adjusted=false",
        "https://api.massive.com/v2/aggs/ticker/AAPL/range/1/minute/2024-01-01/2025-12-31?redirect=https://evil.example",
    ],
)
def test_pagination_cannot_change_host_endpoint_or_request_meaning(url):
    with pytest.raises(download.DownloadError):
        download.next_url(url, "AAPL", START, END)


def test_rate_limit_retry_after_and_retry_count_are_bounded(tmp_path, monkeypatch):
    pauses = []

    async def pause(delay):
        pauses.append(delay)

    monkeypatch.setattr(download.asyncio, "sleep", pause)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 4:
            return httpx.Response(429, headers={"Retry-After": "999999"})
        return httpx.Response(200, json=payload([bar()]))

    assert execute(tmp_path, handler)["requests"] == 4
    assert pauses == [30, 30, 30]
    assert download.retry_delay("-3", 0) == 0


def test_http_errors_never_expose_provider_body_or_key(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="Credential was " + KEY)

    with pytest.raises(download.DownloadError) as caught:
        execute(tmp_path, handler)
    assert str(caught.value) == "http_401" and caught.value.fatal
    assert KEY not in str(caught.value) and len(calls) == 1


def test_network_errors_retry_only_three_times_and_sanitize(tmp_path, monkeypatch):
    async def pause(_):
        pass

    monkeypatch.setattr(download.asyncio, "sleep", pause)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private key " + KEY)

    with pytest.raises(download.DownloadError, match="^network_error$"):
        execute(tmp_path, handler)
    assert len(calls) == 4


def test_global_byte_cap_stops_before_publishing_partial_data(tmp_path):
    with pytest.raises(download.DownloadError, match="byte_cap"):
        execute(
            tmp_path, lambda _: httpx.Response(200, json=payload([bar()])), limit=100
        )
    assert not (tmp_path / "AAPL.csv").exists()


def test_nonfinite_json_is_rejected_even_in_optional_fields(tmp_path):
    content = json.dumps(payload([bar(n=None)])).replace('"n": null', '"n": NaN')
    with pytest.raises(download.DownloadError, match="invalid_json"):
        execute(tmp_path, lambda _: httpx.Response(200, content=content))
    assert not (tmp_path / "AAPL.csv").exists()


def test_literal_key_file_reads_only_named_assignment(tmp_path):
    path = tmp_path / "keys.env"
    path.write_text("UNRELATED=do-not-use\nMASSIVE_API_KEY='" + KEY + "'\n")
    assert download.load_key(path, environ={}) == KEY
    assert download.load_key(environ={"MASSIVE_API_KEY": KEY}) == KEY
    path.write_text("MASSIVE_API_KEY=$(touch injected)")
    with pytest.raises(download.DownloadError, match="invalid_massive_key"):
        download.load_key(path)
    assert not (tmp_path / "injected").exists()


def test_tickers_file_dates_and_free_space_gate(tmp_path, monkeypatch):
    path = tmp_path / "tickers.txt"
    path.write_text("aapl\nBRK.B\n")
    assert download.symbols([], path) == ["AAPL", "BRK.B"]
    with pytest.raises(download.DownloadError, match="distinct"):
        download.symbols(["AAPL", "aapl"])
    with pytest.raises(download.DownloadError, match="invalid"):
        download.dates("2025-01-01", "2024-01-01")
    monkeypatch.setattr(download, "load_key", lambda _: KEY)
    monkeypatch.setattr(
        download.shutil, "disk_usage", lambda _: SimpleNamespace(free=1)
    )
    args = SimpleNamespace(
        env_file=None,
        tickers=["AAPL"],
        tickers_file=None,
        from_date=START,
        to_date=END,
        output=tmp_path,
        workers=4,
    )
    with pytest.raises(download.DownloadError, match="40GiB"):
        asyncio.run(download.run(args))
