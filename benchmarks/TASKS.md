# Task execution and context reference

For configured Python/tool workers, conditional steps, exact-input approvals,
scheduled/input-change execution, and shared-workbook operations, see
[Recurring work](../docs/RECURRING_WORK.md). The SQL/cache mechanics below still
apply to SQL steps; existing format-1 definitions remain compatible.

Workspool tasks save an explicit SQL dependency graph and evidence searches. A
run pins its definition and sources, computes or reuses each eligible result,
and returns a versioned brief. The caller supplies the plan; the runtime does
not invent SQL, infer investment decisions, or perform semantic search.

Start an MCP server with `workspool serve --profile agent --tokenizer o200k_base`.
The agent profile exposes named eager tools with typed schemas and adaptive text responses. An explicit
tokenizer is necessary for `max_tokens`; a local `tokenizer.json` is also accepted.

## API and delivery contract

| Tool | Main arguments | Behavior |
|---|---|---|
| `task_define` | `name`, `objective`, `sources`, `steps`, optional `searches`, `csv_types`, `pin_sources`, `expected_version` | Saves a versioned plan without executing it. Sources follow logical artifact names by default; `pin_sources=true` freezes exact versions. Step inputs name aliases. |
| `task_run` | `name`, optional `resume_from`, `force`, `max_bytes`, `max_tokens`, `base_run` | Pins a coherent snapshot and executes the graph. `force=true` recomputes; `resume_from` retries the earlier run's pinned definition and sources. |
| `task_status` | `name`, optional `run_id`, `max_bytes`, `max_tokens`, `base_run` | Reopens persisted context without executing SQL. The default is the newest attempt, which may have failed. `latest_success` identifies the last successful publication. |
| `context` | `query`, optional `refs`, `limit`, `max_bytes`, `max_tokens` | Returns ranked lexical chunks with immutable artifact identity and exact UTF-8 byte citations. |

A step is `{name, inputs, sql, parameters?}`. Its ordered inputs become `input1`,
`input2`, and so on. Dependent steps consume retained Parquet results. Definitions
allow 1–8 source aliases, at most eight steps and eight evidence searches, and
at most 32 KiB of definition JSON. Each SQL string is at most 6,000 characters;
each step accepts at most 30 scalar parameters. Cycles and unknown aliases fail.

By default, both supplied names and supplied immutable IDs resolve to logical
artifact names when the task is defined. Each new run then pins the current
versions of those names. Passing an ID therefore does not accidentally freeze a
task's future inputs. Set `pin_sources=true` deliberately for historical work:
names and IDs then resolve to exact immutable versions at definition time. A
historical run can be correct while reporting `current=false`. `resume_from`
also preserves the earlier run's pinned snapshot, regardless of default binding.
Evidence-search plans must declare a searchable, non-table text source; a plan
that asks for searches but supplies only tables or binary sources is rejected.

Default delivery is self-contained within the requested budget. `state` describes
execution; `current` describes catalog freshness. `brief_complete` and `omitted`
describe missing presentation content. A successful computation can still have
an incomplete brief: previews contain at most five rows per step, while complete
results remain in the referenced artifacts. Budget reduction removes whole
citations or step previews and records the omissions. If required status and
provenance cannot fit, the operation returns an explicit budget error.

Supply `base_run` **only when the caller still retains that run's unabridged
delivered brief**, including its explicit preview omissions. Do not use it after
context loss or after receiving a budget-trimmed brief. When the same task's base
and current run are complete, the current run is fresh, and their definition,
sources, step results, citations, and omissions match, the response may be an
`unchanged` receipt. Execution/cache bookkeeping and lexical ranking scores are
excluded from that content comparison. The receipt includes `base_run` and
`requires_retained_base=true`; it is not a standalone answer and reports
`brief_complete=false`. Changed or unavailable bases fall back to a normal brief,
which can still require a larger budget. No hidden server-side “already seen”
flag determines delivery.

`task_status` and a batched `inspect` are catalog freshness checks, not full file
integrity scans. `task_run` additionally exercises input/cache verification.

## Coherence, caching, and recovery

Each run retains dependency anchors for its pinned inputs. Updating an input
during execution cannot mix old and new source versions within that run. Such a
run can finish with correct historical results while being marked `superseded`.
A late or failed attempt cannot replace a newer successful publication. Completed
steps survive a partial run; a retry can reuse eligible materializations.

Cache keys include SQL, typed parameters, ordered input identities/digests and
file fingerprints, CSV type overrides, execution settings, result limits,
DuckDB/Python versions, and relevant locale/timezone state. A changed source
invalidates its descendants; independent branches can reuse their results.
First-use or changed-file verification hashes the immutable input. Warm lookups
check device, inode, size, mtime and ctime; cached results are hash-checked. This
assumes the local filesystem reports mutations honestly.

Reuse is deliberately conservative. Recognized single-input SELECTs with known
pure expressions and provable output ordering are eligible; scalar aggregates
are also supported. `ORDER BY ALL` is useful for a deterministic retained table.
Joins, CTEs, window expressions, volatile/unknown functions, unproven ordering,
floating-point SUMs and temporal CSV conversions can bypass reuse. Bypass means
the bounded query still executes and reports its reason; it is not a cache hit.
Arbitrary valid SELECT syntax is not a promise of reusable results.

Default execution bounds are 15 seconds of query time, 1 GB of DuckDB memory,
two threads, one million materialized rows, and 64 MiB per cached result.
The cache accounts for 256 MiB and at most 128 entries; SQLite metadata and
retained workbook artifacts are separate. Disk spilling is disabled in cached
workers. Worker timeouts, a per-file size limit, and a parent/deadline watchdog
bound failed execution as well as publication. These settings do not promise
a fixed end-to-end latency or impose a 1 GB whole-process RSS ceiling.

Cross-process leases have ownership fencing and heartbeats. Expired or killed
owners cannot publish over a replacement owner; callers can retry abandoned
work. Cache eviction does not delete retained source/result history. Pruning
still respects dependency pins.

## Full-source lexical index

The separate disposable context index uses FTS5 BM25 over overlapping source
chunks, approximately 2 KiB with 256 bytes of overlap. Queries become escaped
Unicode words joined by OR; lower BM25 scores rank first, with deterministic
tie-breaking. This is lexical ranking, not semantic similarity or confidence.
Scores depend on the indexed corpus, including retained historical versions.

A version is published to the index only after its entire UTF-8 source and
SHA-256 digest validate. Warm requests check file fingerprints and reread each
returned chunk against its exact source bytes. Chunk IDs incorporate immutable
version identity, source digest, byte range and chunk content. Historical IDs
remain valid explicit scopes; default searches select latest text versions.

Per-call indexing defaults to 128 MiB, 128 versions, and 30 seconds of build
work. Verified whole versions persist across calls; unfinished versions remain
pending and are excluded from results. A single source larger than the byte
budget needs a larger internal indexing budget; repeated default calls cannot
finish part of that source. Explicit scopes accept at most 16 distinct refs;
the default scope is capped at 10,000 versions. At most 64 ranked chunks are
returned. Cached-history cleanup examines at most 256 versions per call.

`index_complete` means every selected source was indexed; `complete` also means
no matching chunks were omitted by ranking/output limits. Pending versions and
truncation are explicit. This ranked API has no exhaustive continuation cursor.
Query evaluation has a five-second budget, but cache locking and setup can add
latency; warm readers can wait behind an index build. Output limits preserve
whole chunks and can fail when even one chunk cannot fit.

## Exact decimals and abandoned imports

Automatic CSV inference may parse decimal-looking values as DOUBLE. Casting
those values to DECIMAL later preserves the earlier binary rounding error.
For task CSV sources, pin parser types in the definition, for example
`csv_types={"bars":{"v":"DECIMAL(38,12)"}}`. Typed Parquet is another option.
Overrides participate in provenance/cache identity. They do not change the
source bytes or silently alter standalone query defaults.

On Mac/Linux, import staging files have recognizable names and exclusive file
locks. Creation and lock acquisition are coordinated under the catalog write
lock, so cleanup cannot race a new live writer. SIGKILL releases those locks;
`prune` can reclaim the abandoned stage and reports `stages_removed` separately
from `blobs_removed`. `bytes_freed` includes both. Dry runs report candidates
without deleting them. Live writers, symlinks, directories and legacy unmarked
temporary files are preserved. Platforms without `flock` retain the prior
unmarked staging behavior and do not provide this orphan cleanup guarantee.

## Measurement boundaries

[task_stress.py](task_stress.py) compares the lifecycle against a persistent
direct DuckDB cache, using identical SQL and settings plus an independent Arrow
oracle. It checks complete materialized answers and provenance, cold/warm/reopened
reuse, one-source invalidation, forced recomputation, and source updates during a
run. Actual MCP measurements separate fresh-context reconstruction from retained
context. The latter compares unchanged task receipts with one batched metadata
freshness check of the terminal results and policy; recursive `stale_inputs`
covers their table ancestors. It does not charge the baseline for rereading
retained results, intermediate/source tables, or a candidate-only task definition.
Known-argument tool traces are not model-driven evaluations, and a single
lifecycle run does not establish a general latency or token-reduction percentage.
