# Public dataset stress: tables and research context

The published MCP counts below are historical scripted measurements with known
operation arguments. Their lazy interface was not model-validated. Current profiles
use eager typed tools; rerunning the current harness changes setup/request counts.
The original result files remain unchanged.

Recorded November 7, 2026 against the v0.7 release candidate. Workspool passed exact result, incremental reuse, and
context recovery checks on **48,722,602 NYC taxi trips**, **38,470,483 NOAA
observations**, and **8,192 Wikipedia articles containing 463,555,655 UTF-8
bytes**. The controls passed too. A persistent DuckDB script and a persistent
SQLite FTS5 index were faster and often returned less metadata. These results
demonstrate working capabilities at this scale, not a universal speed or token
advantage.

The [sanitized evidence](results/2026-11-07-public/) records source and code
hashes, exact row counts, every lifecycle outcome, retrieval checks, timings,
and token counts. It contains no source rows, article passages, credentials,
local absolute paths, or generated workspace IDs. Original data and detailed
traces remain local.

## Sources and preparation

| Public source | Exact selection | Data exercised |
| --- | --- | --- |
| [NYC TLC yellow taxi records](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page) | All twelve 2025 monthly files; 829,973,299 downloaded bytes | All 48,722,602 rows, projected to pickup location, payment type, and passenger count |
| [NOAA GHCN-Daily 2024](https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/2024.csv.gz) | Entire downloaded yearly file; 176,612,851 compressed bytes | All 38,470,483 rows, including quality-flagged observations |
| [English Wikipedia dump shard](https://dumps.wikimedia.org/enwiki/20261001/enwiki-20261001-pages-articles-multistream1.xml-p1p41242.bz2) | First 8,192 nonempty, nonredirect namespace-0 pages in archive order | Exact revision wikitext, 463,555,655 bytes (442.08 MiB), not rendered prose |

The acquisition script requires no paid API or credentials. Public availability
does not replace the source's terms: Wikipedia text generally uses CC BY-SA 4.0
and GFDL, with exceptions described in the
[Wikimedia dump licensing guide](https://dumps.wikimedia.org/legal.html).
TLC cautions that its records are vendor supplied and makes no accuracy
guarantee. This benchmark checks faithful computation over the downloaded
records, not whether each underlying observation is true.

NOAA's [format documentation](https://www.ncei.noaa.gov/pub/data/ghcn/daily/by_year/readme-by_year.txt)
says yearly files are updated daily. The captured SHA-256, rather than the year
in the URL or a previously observed file size, identifies this run. The
Wikipedia archive also passed its official published SHA-1 check.

Table preparation streams batches of at most 65,536 rows into typed Parquet,
retaining row order, duplicates, nulls, and all rows. Taxi's selected integer
columns are widened losslessly to int64; this does not test floating monetary
fields. NOAA's integer values remain grouped by element and native unit; sums
across different elements would be meaningless. Blank quality flags are
preserved. The projected initial inputs are 49,159,958 bytes for taxi and
45,384,530 bytes for NOAA. These are the files queried, not all columns of the
larger downloads.

## Exact table results and incremental work

Each task has a three-step graph: an aggregate, a dependent top-five result,
and an independent fixed-source aggregate. Taxi groups trips and passengers by
pickup zone and keeps a January payment summary. NOAA groups counts, sums,
minima and maxima by element and keeps quality-flag counts from the first
authentic prepared part.

An independent streaming Arrow oracle checks **every materialized result row**,
with exact integer/decimal accumulation, declared ordering, and pinned input
provenance. It does not use DuckDB or model-visible previews to derive expected
answers. Initial and updated outputs contain 262 zone groups, five top zones,
and six payment groups for taxi; NOAA has 74 element groups, five leading
groups, and 13 quality-flag groups.

Both Workspool and the persistent direct DuckDB control executed the same
number of computations:

| Lifecycle | Computations in either arm | Exact checks |
| --- | ---: | --- |
| Cold | 3 | Passed |
| Warm | 0 | Passed |
| Reopened workbook/cache | 0 | Passed |
| Changed upstream source | 2 | Passed; independent branch reused |
| Forced recomputation | 3 | Passed |

The changed taxi source excludes December, leaving 44,417,596 authentic rows.
The changed NOAA source excludes the last prepared part, leaving 37,791,628.
These are controlled subset changes, not newly downloaded revisions. The old
task result reported `superseded` before refresh in both cases.

| Execution time | Taxi Workspool / direct | NOAA Workspool / direct |
| --- | ---: | ---: |
| Cold | 628.13 / 258.12 ms | 489.45 / 237.05 ms |
| Warm | 23.89 / 0.38 ms | 23.57 / 0.38 ms |
| Reopened | 22.01 / 0.36 ms | 24.11 / 0.35 ms |
| Source change | 415.89 / 195.29 ms | 402.17 / 198.11 ms |
| Forced | 535.37 / 198.68 ms | 537.43 / 193.16 ms |

Preparation, imports, independent oracles and validation are separately timed
in the evidence. The direct control is a capable persistent script cache using
the same SQL and typed sources. It does not reread raw downloads on every turn.

## Actual MCP delivery costs

These table measurements use the actual MCP server with the `agent` profile,
lazy discovery, adaptive text output, and known operation schemas. Counts below
are `o200k_base` request plus response tokens. Initialization instructions and
the listed facade add 372 tokens separately for either dataset. Operation-help
discovery and provider framing are outside this known-schema measurement;
there are no model decisions or provider billing measurements.

The `agent` profile deliberately does not enable automatic continuity capture.
The separate two-session `runtime` demonstration below exercises that feature;
its extra writes and context are not silently included in the table timings.

| Delivery | Calls | Taxi tokens | NOAA tokens |
| --- | ---: | ---: | ---: |
| Full task status brief | 1 | 1,258 | 1,271 |
| Retained task receipt (`task_run` or `task_status`) | 1 | 324 | 325 |
| Retained terminal-result freshness check (`inspect`) | 1 | 296 | 283 |
| Terminal previews from existing tools (`inspect` + two queries) | 3 | 719 | 715 |

The retained controls already hold their result content and know the unchanged
plan; inspecting terminal outputs recursively detects stale upstream inputs.
Workspool's task receipts cost 28 and 42 more tokens here. They carry task-level
status that the smaller terminal-only control does not request.

The fresh-context rows also have different output scopes: the task brief
includes graph/provenance and intermediate previews; the existing-tool control
requests only five-row terminal previews. Fewer calls did **not** imply fewer
tokens in these tasks. Full materialized correctness was verified separately
from these bounded wire previews.

## Research context over 442 MiB

The text test imports each authentic article as its own immutable source.
Both Workspool and the independent persistent FTS5 control use deterministic
lexical ranking over roughly 2 KiB chunks with 256-byte overlap. They index all
8192 sources in 64 resumable passes of at most 128 versions. Every pass reports
whether coverage is complete; intermediate results are never represented as
exhaustive coverage.

| Measurement | Workspool | Persistent FTS5 control |
| --- | ---: | ---: |
| Cold indexing, sum of 64 calls | 52.73 s | 20.11 s |
| Warm lookup median | 125.59 ms | 83.59 ms |
| Reopened lookup median | 127.83 ms | 83.10 ms |
| Selected target found, warm and reopened | 10 / 10 | 10 / 10 |
| Changed versions indexed after one replacement | 1 | 1 |

Six large documents were considered; five supplied eligible complete passages.
Queries use two actual words selected by measured rarity within those
passages. All five target passages begin more than 64 KiB into their source.
This is a small, deliberately targeted lexical test, not a general semantic
recall or research-quality evaluation. Each returned citation was independently
reread at its exact UTF-8 byte offsets and checked against the acquired source
and digest, including after reopen and when explicitly requesting a historical
version.

The existing literal search was tested separately because it answers a
different query: an exact anchor passage. Searching the whole corpus exhausted
eight 16 MiB scan pages per query without reaching these targets; each response
correctly remained incomplete. When supplied the correct immutable source,
literal search found every passage in one call. This demonstrates the value of
scope information and indexing, not that literal search is incorrect.

A text-only durable task recovered the same citations after reopening its
workbook. Its full reset response was 9,004 native JSON tokens; an explicit
retained-run receipt was 178. That receipt assumes the caller already holds the
previous unabridged delivered brief. It is not a replacement for lost context,
nor a comparison against an optimized independent context store. These text
counts exclude MCP envelopes and model/provider usage.

Two limits remain visible:

- A 1,000-token budget could not hold this task's required status/provenance and
  returned an explicit budget error. A 2,000-token budget produced 1,983 tokens,
  two citations, and explicit omissions; it was not a complete brief.
- Concatenating the authentic corpus once into a derived 463,563,847-byte
  source did **not** complete indexing. The default 128 MiB per-call allowance
  left the version pending. Raising the internal byte allowance still hit the
  30-second build limit, rolled back the unfinished version, and returned
  incomplete status. This probe is not an original Wikipedia article and is
  not counted as successful whole-source coverage. Splitting source documents
  is currently necessary for this case; whole-version rollback cannot resume
  partway through one oversized source.

## Automatic capture and a fresh MCP session

[runtime_demo.py](runtime_demo.py) starts two separate real MCP server
processes with the `runtime` profile. Its disclosed synthetic four-row issue
table has two open critical issues. The first session saves the table and
queries the count; the server automatically captures the observed result and
records session close. No `remember` call is made.

**Historical initialization transport, removed in v0.9.2.** The recorded second process starts with the goal `launch_blockers`. Its startup
instructions recover the prior query episode containing the exact answer
`2`, with byte-verified citations, in **607 tokens including the full restored
context prefix and JSON**, below the requested 2,000-token budget. It reports
lexical mode, `model_not_configured`, and incomplete dense coverage. No external
model/API call or semantic-quality claim is involved. This is an end-to-end
capability demonstration, not an additional large-data benchmark or a measured
savings comparison. The [sanitized demo result](results/2026-11-07-public/runtime-demo.json)
records the actual call counts, token counts and implementation hashes.

The current `runtime_demo.py` instead makes an explicit named `recall` call in
the new session and checks the exact projected observation and citations. It
asserts that source text is absent from initialization instructions. Running it
writes a new result; it does not reproduce the historical 607-token transport.

## Limits of the measurement

This is one fixed-order local trial on arm64 macOS 26.6.2 / Python 3.13.15. The
three suites were launched together while a separate local semantic-model
probe was also running, so timings include ordinary concurrent load and warm
filesystem effects. They are indicative, not isolated latency
estimates. Table raw inputs and code fingerprints were unchanged across their
runs. The text index/retrieval/task fingerprints match the earlier table
snapshots; its harness records fingerprints at completion rather than making
a formal start/end stability assertion.

After these final dataset runs, one failure-path-only server fix made automatic
capture recognize failed MCP results containing non-JSON error text. The dataset
numbers retain the fingerprints of the code actually measured; they are not
relabeled as measurements of that later change. The two-session runtime demo
was rerun after the fix and records its own implementation fingerprints.

External guards allowed 1,200 seconds per suite, limited each output file to
4 GiB and each run directory to 10 GiB, and stopped on low free disk. None
triggered. Reported child/process high-water RSS was about 306 MiB for taxi,
309 MiB for NOAA and 473 MiB for Wikipedia. These are cumulative OS high-water
statistics, not simultaneous process-tree RSS, per-operation measurements, or
enforced memory ceilings. Full wall times including preparation and checks
were 9.42 s, 9.20 s and 161.25 s respectively.

## Reproduce

Install the locked development environment and the `benchmark`/`tokens` extras. Populate
the tokenizer cache once before an offline run:

```sh
uv sync --extra benchmark --extra tokens
uv run python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base"); tiktoken.get_encoding("o200k_base")'
uv run python benchmarks/public_data.py --root .local-benchmarks/public-replay
```

The downloader fetches official public files, records their hashes and
prepares NOAA/Wikipedia inputs. Compare its hashes with
[acquisition.json](results/2026-11-07-public/acquisition.json); mutable sources
may have changed. To recreate the recorded table configurations beside the
downloaded files:

```sh
uv run python - <<'PY'
import json
from pathlib import Path
root = Path('.local-benchmarks/public-replay')
for dataset in ('taxi', 'noaa'):
    evidence = Path('benchmarks/results/2026-11-07-public') / f'{dataset}.json'
    config = json.loads(evidence.read_text())['config']
    (root / f'{dataset}-config.json').write_text(json.dumps(config, indent=2))
PY
uv run python benchmarks/public_table_stress.py --config .local-benchmarks/public-replay/taxi-config.json --output .local-benchmarks/public-replay/taxi-run
uv run python benchmarks/public_table_stress.py --config .local-benchmarks/public-replay/noaa-config.json --output .local-benchmarks/public-replay/noaa-run
uv run python benchmarks/public_text_stress.py --documents .local-benchmarks/public-replay/wikipedia/documents --manifest .local-benchmarks/public-replay/wikipedia/documents/manifest.json --output .local-benchmarks/public-replay/text-run --bundle-probe
```

Use fresh output directories. The published NOAA configuration lists the 19
parts from this captured source; if a later daily snapshot has a different
part count, update the explicit selection and label it as a new dataset run.
The separate automatic-capture demonstration uses a fresh workspace:

```sh
uv run python benchmarks/runtime_demo.py --workspace .local-benchmarks/runtime-demo-replay --output .local-benchmarks/runtime-demo-replay.json
```

Run under a suitable external timeout/disk guard when using other corpora.
The source hashes in each evidence file identify the measured implementation;
later feature changes require new traces before reusing these numbers.
