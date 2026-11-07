# Workspool with 34 million real market records

This test uses **34,015,435 distinct one-minute bars across 131 stocks and ETFs**, downloaded from Massive for 2024–2025. The normalized CSV files total **1,855,955,557 bytes (1.86 GB)**. The download completed all 131 symbols in 755 API requests. No rows were duplicated to increase the test size.

The workload represents recurring data-analysis jobs: summarize a large dataset, screen records, calculate ordered results, save the result, hand it to another session, and refresh it when the source changes. Market data supplies a substantial real dataset; this is a test of the workspace, not a trading strategy or a claim that the selected symbols represent the entire market.

## Data and independent checks

The downloader follows Massive's [historical aggregate endpoint](https://massive.com/docs/rest/stocks/aggregates/custom-bars). Massive also provides [bulk flat files](https://massive.com/docs/flat-files/quickstart); this run used the existing authorized REST connection and followed every returned page.

Each source CSV contains `ticker,t,o,h,l,c,v,n`: symbol, epoch-millisecond timestamp, open, high, low, close, volume, and trade count. JSON decimal values are preserved as decimal text. Optional provider fields such as VWAP are excluded. Every downloaded symbol has strictly increasing timestamps, finite numeric values, nonnegative volume, and consistent OHLC bounds. All completed file sizes and SHA-256 hashes were rechecked independently after download. There were no missing trade counts.

A separate query through the Massive connector verified an overlapping AAPL interval: **4,340 rows**, total volume **219,275,017**, **3,206,133 trades**, price bounds **233.0–251.9**, and **1,690 up-bars**, with matching first and last timestamps. The large-data harnesses additionally calculate expected answers independently of DuckDB.

Raw market data, credentials, and workbook blobs stay local. Published evidence contains aggregate measurements, source fingerprints, and correctness results.

## Results

**All five typed-Parquet cases passed every exact-answer and lifecycle check.** The largest input contains all 34,015,435 downloaded rows in an 862,965,562-byte Parquet file. The summary returns one row with nine fields: row and symbol counts, first and last timestamps, volume and trade totals, price bounds, and the number of up-bars.

| Input rows | Summary through MCP | Direct DuckDB summary | Save filtered result | Refresh result | Peak server RSS | Summary reply tokens |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000,000 | 0.040 s | 0.038 s | 0.038 s | 0.038 s | 112 MiB | 115 |
| 5,000,000 | 0.175 s | 0.173 s | 0.156 s | 0.155 s | 172 MiB | 122 |
| 10,000,000 | 0.334 s | 0.333 s | 0.306 s | 0.304 s | 237 MiB | 128 |
| 25,000,000 | 0.819 s | 0.821 s | 0.731 s | 0.744 s | 390 MiB | 120 |
| 34,015,435 | 1.119 s | 1.114 s | 0.996 s | 0.994 s | 484 MiB | 121 |

At full size, importing the already-prepared Parquet file took **0.330 seconds**. Saving the filtered result retained **528,697 rows**, checked row by row for values, order, and window sequence. Direct DuckDB materialization took **0.997 seconds**. The complete case, including three concurrent reads, independent output checks, two source versions, handoffs, refresh, and timeout recovery, finished in **21.98 seconds**. Peak sampled memory for the entire test process tree was **1.11 GiB**, including the driver and controls; the server alone peaked at **484 MiB**. The workbook occupied **1.75 GB** before cleanup, including both source versions and saved results.

The full-size summary reply was **349 UTF-8 bytes / 121 `cl100k_base` tokens / 123 `o200k_base` tokens**. Its request used 110 / 111 tokens, and the serialized MCP result including its envelope used 145 / 146 tokens. Across the entire 23-call lifecycle, answer text used 2,982 / 3,004 tokens, requests used 1,477 / 1,494, and instructions plus advertised schemas across both server sessions used 1,386 / 1,454. No reply exceeded 978 bytes. These are counted payloads; no model was asked to ingest the full dataset, and no billed-cost reduction is inferred from an infeasible raw-data prompt.

The useful saving is that **34 million source rows remain outside the conversation while the agent receives the requested answer**. DuckDB performs the calculation; Workspool adds local retention, bounded previews, version references, saved recipes, freshness checks, and handoffs. This test does not establish a speed advantage over DuckDB or guarantee the same savings for tasks that require reading every record.

Measured on an **Apple M5 Max with 18 physical CPU cores and 128 GiB RAM**, running macOS 26.6.2 / ARM64, Python 3.13.15, DuckDB 1.5.6, MCP 1.30.0, Arrow 23.0.1, and tiktoken 0.14.0. Queries were limited to two threads. Preparation of all ten N/N−1 snapshots took 47.62 seconds and is separate from query/import timings. This release changes documentation and test tooling; the measured Workspool runtime is unchanged from v0.5.0.

Evidence: [dataset manifest](results/2026-11-07-massive/dataset.json), [independent connector check](results/2026-11-07-massive/connector-check.json), and [Parquet measurements and exact checks](results/2026-11-07-massive/parquet.json).

## CSV precision finding

The full CSV scan completed within the resource limits, but **failed the strict decimal-answer check** in both Workspool and direct DuckDB. Automatic CSV inference reads volume as `DOUBLE`; subsequently casting those values to `DECIMAL(38,12)` preserves small binary-floating-point artifacts. The observed total was `1160177301319.835000003911`, versus the source-exact `1160177301319.835000000000`, a difference of `0.000000003911`. The other eight summary fields matched exactly.

| CSV configuration | MCP import | MCP query | Direct DuckDB query | Exact-answer check |
| --- | ---: | ---: | ---: | --- |
| Default: 15 s / 1 GB / 2 threads | 0.724 s | 3.712 s | 3.695 s | Volume differs by 3.911e−9 |
| Diagnostic retry: 120 s / 4 GB / 2 threads | 0.737 s | 3.794 s | 3.722 s | Same difference |

The default MCP run peaked at 327 MiB of sampled process-tree RSS, including its client; the direct control peaked at 127 MiB. A separate direct DuckDB diagnostic with an explicit `DECIMAL(38,12)` type for `v` matched the independent exact volume total. That diagnostic confirms the inference cause; it is not counted as a passing Workspool CSV run. [CSV measurements and field differences](results/2026-11-07-massive/csv.json).

This can be reproduced without market data: casting the text `10000000.001` to `DOUBLE` and then to `DECIMAL(38,12)` produces `10000000.000999999488`. Increasing memory or the timeout does not repair a type-conversion difference. The checks retain this failure; they do not round it away or classify it as an exact pass.

**Use explicitly typed Parquet when source decimal precision must be preserved.** Workspool currently uses automatic type inference for CSV and does not expose per-column CSV type overrides. A later decimal cast cannot recover precision already lost during parsing. This is the practical distinction between the CSV diagnostic and the passing Parquet cases.

## What the scale test exercises

The Parquet cases use nested prefixes of **1 million, 5 million, 10 million, 25 million, and all 34,015,435 rows**. Files are selected in symbol order, not sampled to represent the whole market at every size. Arrow converts the source decimal text to explicit `DECIMAL(38,12)` fields and fails on a lossy conversion. Integer timestamps remain integers. Streaming Arrow kernels and Python decimal arithmetic calculate the expected full-data answers independently of DuckDB.

Each case uses actual stdio MCP calls to import the file, compute a nine-field summary, issue three simultaneous summary requests, save a filtered and ordered result with a per-symbol window sequence, create and resume a checkpoint, replace the source with an N−1-row version, detect stale results, refresh them, and advance the checkpoint. Every saved row and sequence is checked independently. The filter selects minute indices divisible by 64, yielding roughly 1/64 of the source; the saved output is capped at one million rows. This exercises filtering, sorting, and a window function without pretending to materialize an unlimited result.

The simultaneous requests share one server, whose query slot serializes execution. A second server with a 50 ms query timeout runs an intentionally pathological query, then successfully reads the dataset again. The separate failure suite also checks competing writers in two processes.

Direct DuckDB controls use the same SQL, source, memory setting, thread count, and preview limit. Saving directly omits Workspool's catalog, version tracking, and blob copy. Source preparation and imports warm filesystem caches; timings are observations from this run, not cold-storage measurements or statistical speedup estimates. Three simultaneous reads provide repeated correctness checks, not independent latency trials.

The normal query settings are **15 seconds, 1 GB DuckDB memory, and two threads**. External guards allow **180 seconds per complete case, 4 GiB sampled process-tree RSS, 40 GiB output, and at least 10 GiB free disk**. RSS sampling can miss brief peaks; the report also records native process high-water marks. Test-driver memory is reported separately from server memory.

## Reproduce locally

Install the optional measurement dependencies:

```sh
uv sync --locked --extra benchmark --extra tokens
```

Download the two tokenizer rank files into the local cache before measuring. This makes no model request:

```sh
uv run python -c 'import tiktoken; [tiktoken.get_encoding(n) for n in ("cl100k_base", "o200k_base")]'
```

Supply a local environment file containing your authorized `MASSIVE_API_KEY`, and a file with the symbols you want to download. The published dataset manifest lists the 131 symbols used here. The downloader defaults to bounded concurrency and writes resumable per-symbol manifests; it never publishes the data.

```sh
uv run python benchmarks/download_massive.py \
  --tickers-file symbols.txt --from-date 2024-01-01 --to-date 2025-12-31 \
  --env-file /path/to/private.env --output .local-benchmarks/massive/data --workers 4

uv run python benchmarks/massive_stress.py \
  --sources .local-benchmarks/massive/data/*.csv \
  --rows 1000000 5000000 10000000 25000000 34015435 \
  --output .local-benchmarks/massive/parquet-results

uv run python benchmarks/massive_csv_stress.py \
  --data .local-benchmarks/massive/data \
  --csv .local-benchmarks/massive/full.csv \
  --output .local-benchmarks/massive/csv-results

uv run python benchmarks/stress_failures.py \
  --source .local-benchmarks/massive/data/AAPL.csv \
  --output .local-benchmarks/massive/failure-results --rows 100000
```

The scale test refuses to duplicate rows when the requested size exceeds the source. Local traces may contain source previews and filesystem paths; publish only reviewed aggregate evidence.

## Failure and recovery checks

Six isolated probes use 100,000 real source rows plus explicitly identified malformed records or synthetic edge values. They are separate from the large-data throughput measurements.

- A malformed final CSV row and a late nonnumeric value fail explicitly; a null remains a null and does not silently remove its row.
- Truncated Parquet is rejected. Integers beyond JavaScript's exact integer range retain their values in previews and saved results.
- A generated 16 MiB cell produces a bounded 361-byte reply. This bounds the reply, not the engine's allocation for the original cell.
- A deliberately expensive query is interrupted, and the next query succeeds.
- Two processes updating the same expected version produce exactly one winner. A source update during a query preserves the pinned input; the saved result becomes stale and can be refreshed.
- Killing an import leaves the catalog atomic and permits a subsequent successful import. **The test also found that an unreferenced 64 KiB staging file survives `prune` after a hard kill.** Successful recovery does not imply automatic cleanup of those temporary files.

The query timeout applies to the running SQL statement. Connection setup, waiting for the process-local query slot, and importing a saved result are additional work. DuckDB's memory setting is not a cap on whole-process memory. The harnesses therefore use external wall-time, process-memory, and disk watchdogs and record their limits separately.

The [failure and recovery measurements](results/2026-11-07-massive/failures.json) retain the staging-file limitation separately from the passing catalog and recovery assertions.
