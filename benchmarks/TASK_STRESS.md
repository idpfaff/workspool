# Real-data task stress: exact reuse, measured overhead

On 2026-11-07, Workspool passed all six lifecycle scenarios and a controlled
source-update case over **34,015,435 real minute bars across 131 symbols**. Full
materialized results matched an independent Arrow oracle exactly. Warm and
reopened runs reused all three analytical results; changing one source recomputed its two dependent
steps and retained the independent branch.

The persistent direct DuckDB control achieved the same reuse and was faster in
every scenario. Retained-context receipts saved **51 protocol tokens (15.94%)**
against a competent three-reference freshness check. Reconstructing fresh
context used **28 more tokens (1.73%)**, despite requiring one tool call instead
of five. These are single-run local measurements, not general speedup or
model-efficiency claims. The [sanitized evidence](results/2026-11-07-task-stress.json)
retains exact timings, checks, input hashes and measured source hashes.

## Scope and correctness

The graph computes per-symbol minute counts, exact volume sums, first/last
timestamps and low/high prices, then a five-symbol volume ranking. A separate
calendar table produces coverage statistics. A short synthetic policy document
supplies exact source citations. The market records are real; the policy text is
a controlled fixture. Data preparation and provenance are described in
[the Massive stress report](MASSIVE_STRESS.md).

| Typed Parquet snapshot | Rows | Bytes | SHA-256 prefix |
|---|---:|---:|---|
| Initial | 34,015,435 | 862,965,562 | `68ff4a547f09a586` |
| Updated, one fewer row | 34,015,434 | 862,965,535 | `2b161fef58b9ecdc` |

Full hashes are in the JSON evidence. Timestamps are int64; prices, volumes and
trade counts use `DECIMAL(38,12)`. Arrow streamed both complete inputs, grouped
the required columns, and combined decimal sums in Python with 60-digit
precision. All 131 symbol rows, five ranking rows and one calendar row were
compared exactly with each arm's retained materializations. Checks also verified
ordered artifact dependencies and citation byte ranges. No preview was treated
as the complete table, and no raw market rows are published.

The calendar source intentionally remains a separate fixed snapshot when bars
change. Its reuse demonstrates selective invalidation; it does not claim that
calendar statistics automatically follow a source they do not depend on.

## Execution lifecycle

Both arms use the same SQL, a 1 GB DuckDB memory setting, two threads and a
15-second query timeout. The direct script retains Parquet results and a cache
keyed by SQL, settings and ordered input identities/digests. It checks input
fingerprints, hashes changed inputs and verifies cached output hashes. It is a
capable control, not a repeated full-file baseline.

| Scenario | SQL executions, direct / Workspool | Direct ms | Workspool ms | Exact checks |
|---|---:|---:|---:|---|
| Cold | 3 / 3 | 1,031.670 | 1,380.598 | Pass |
| Warm | 0 / 0 | 1.255 | 21.814 | Pass |
| Reopened objects | 0 / 0 | 1.198 | 19.023 | Pass |
| One source changed | 2 / 2 | 1,013.684 | 1,280.206 | Pass |
| Forced recomputation | 3 / 3 | 741.327 | 1,031.786 | Pass |
| Recover current source | 2 / 2 | 732.302 | 987.935 | Pass |

The changed-source run invalidated the symbol aggregate and ranking, while
calendar statistics stayed cached. Before refresh, the previous task correctly
reported `current=false` and `state="superseded"`.

In the additional Workspool-only interleaving, the source changed after run
pinning, inside the first step invocation before its query began. All three
forced computations still matched the pinned historical snapshot, and the run
was correctly marked superseded. It took 1,359.591 ms, including that source
import. The following recovery used the current source. This is one controlled
interleaving, not a concurrent-load or crash-recovery benchmark.

Preparation, both oracle scans and initial imports took 4.508 seconds and are
outside the scenario timings. Imports between scenarios are also excluded.
Workspool timings exclude the independent full-result validator; direct timings
include reading its small result tables. Reopened means new workbook/cache
objects in the same process, not a reboot or cold filesystem cache. The direct
arm runs first in each scenario; its executor stays in process while Workspool
uses isolated workers and durable task publication.

This was one ordered trial on an ordinary, unisolated macOS arm64 machine with
Python 3.13.15. No pytest run overlapped this final measurement. Filesystem cache,
background activity and execution order were not randomized or controlled;
there are no confidence intervals or whole-process RSS measurements here.

## Actual MCP traffic

The harness made real MCP calls with known arguments through the current
`agent` profile: lazy discovery, adaptive text responses and `o200k_base` token
counting. Counts cover exact serialized requests and `CallToolResult` responses.
They are local protocol counts, not provider usage, billing or model reasoning
cost. The shared schema/instruction setup cost **383 tokens**, reported
separately from the incremental messages below. The controls use established
tools on the same current server, not a separately installed older release.

| Workflow | Calls | Request tokens | Response tokens | Total | Tool ms |
|---|---:|---:|---:|---:|---:|
| Retained `task_status` receipt | 1 | 55 | 214 | 269 | 1.321 |
| Retained `task_run` receipt | 1 | 55 | 214 | 269 | 21.282 |
| Retained results: metadata freshness check | 1 | 78 | 242 | 320 | 1.081 |
| Fresh task brief: warm `task_run` | 1 | 28 | 1,618 | 1,646 | 26.917 |
| Fresh-context reconstruction with existing tools | 5 | 358 | 1,260 | 1,618 | 16.638 |

For retained context, the caller already holds the full delivered brief or its
equivalent results. The control inspects only the ranking, calendar result and
policy: recursive `stale_inputs` covers upstream tables. It does not reread
results, intermediate tables or a candidate-only task definition. Both receipts
were unchanged, current, tied to the supplied retained base, and below the
1,000-token response cap. The status receipt and metadata check are both
catalog-freshness operations; `task_run` additionally validates/reuses its task
materializations and publishes a run.

The 51-token retained-context difference is modest, and the metadata check was
faster. The fresh-context control uses one batched inspect, three small queries
against retained outputs and one policy search. The task collapses these calls
but sends slightly more payload. Fewer calls here do not establish fewer model
tokens or lower end-to-end agent latency.

Budget checks expose the limits rather than count missing answers as savings:

| `task_status` response cap | Actual MCP response tokens | Delivered content |
|---|---:|---|
| 1,000 | 908 | Status/provenance; all step previews and the policy citation omitted with explicit budget reasons |
| 2,000 | 1,629 | All three step previews and the policy citation; the full symbol table remains in its artifact |

Both report `brief_complete=false`. The 1,000-token response cannot independently
answer the numerical/policy task. The 2,000-token response includes the complete
five-row ranking and calendar result, but only five intermediate symbol rows.
An unchanged receipt is also not a standalone answer: `base_run` is valid only
when the caller retains the unabridged delivered base. See [the API contract](TASKS.md).

## Reproduction and provenance

The [harness](task_stress.py) makes no model or market-data API calls. Supply the
same licensed data locally; the two input hashes identify the exact snapshots.
Pre-cache the tokenizer before the offline run, then use a fresh output directory:

```sh
uv run --locked --extra tokens --extra benchmark python -c \
  'import tiktoken; tiktoken.get_encoding("o200k_base")'
uv run --locked --extra tokens --extra benchmark python benchmarks/task_stress.py \
  --source data/rows-34015435.parquet \
  --updated-source data/rows-34015434.parquet \
  --output .local-benchmarks/task-stress-reproduction \
  --tokenizer o200k_base --mcp
```

The JSON records harness/runtime SHA-256 values, input sizes/digests, canonical
result fingerprints, every scenario's checks, budget omissions, and wire totals.
Lifecycle and MCP phases used matching final sources; the captured source hashes
also matched the publication checkout. Private complete-report hashes link the
sanitized projection to the local evidence. Raw reports, source rows, result
rows, local absolute paths, artifact/run IDs and credentials are excluded from
the public artifact. Reruns can produce different identifiers, token counts and
timings even when exact results match.
