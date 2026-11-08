# Workspool: retrieve less source, preserve exact work

This reevaluation compares the previous implementation at `bdc6561ecf759d32868dbe982e79195cf8650dd5` with source search, table descriptions, exact version deltas, and adaptive metadata packing. The earlier [known-input benchmark](EFFICIENCY.md) measured protocol overhead but supplied known table schemas, known document references, and evidence near the start of each document. It did not test the cost of discovering evidence or keeping complete work current.

The new suite executes both implementations against cloned local fixtures with identical initial artifact IDs. Each workflow uses a small explicit eager tool allowlist, text-only responses, and fully counted cold schemas. The previous implementation already batches reads, uses raw text where useful, queries table schemas without sampling data rows, and reuses unchanged bodies after metadata checks. The comparison does not make the old implementation resend everything unnecessarily.

At measurement time, `--profile efficient` enabled nine operations, lazy discovery, and adaptive replies. **That profile is not the measured configuration below.** It retains both MCP representations by default and does not select an optimal workflow automatically.

## Results, including the losses

“New payload” counts instructions, tool definitions, prompts, scripted tool calls, decoded replies, and scripted exact answers once. These are `cl100k_base` counts:

| Workflow | Previous implementation | Updated | Reduction |
| --- | ---: | ---: | ---: |
| Evidence deep in three unknown documents | 20,303 | 920 | 95.47% |
| Complete runbook across eight checks and two small edits | 4,158 | 2,593 | 37.64% |
| Complete 35-column schema, then eight table questions | 1,852 | 1,848 | 0.22% |
| Every byte of 120 documents | 105,359 | 101,792 | 3.39% |
| Complete runbook across two extensive rewrites | 4,178 | 4,819 | **−15.34%** |

For `o200k_base`, the corresponding before/after counts are 19,133/904, 3,979/2,553, 1,902/1,870, 105,386/101,931, and 3,991/4,629. Reductions are 95.28%, 35.84%, 1.68%, 3.28%, and **−15.99%**.

An additional fixture puts evidence after byte 82,000 in an artifact absent from the baseline's newest-ten fallback. The baseline plan is inconclusive; the new search finds the exact evidence. Its 397-token payload is reported as a successful retrieval, with **no savings percentage against the unsuccessful baseline**. The result does not turn an incomplete catalog search into proof that a document contains no match.

The measurements show where the new capabilities matter:

- Source search avoids reading unrelated pages. The three-document task drops from 14 tool calls to three. The source IDs and exact review codes are verified from returned evidence.
- Deltas reduce transfer for small changes when a complete base is already retained. The runbook task explicitly requires complete text for later full-document edits; all eight reconstructed versions are checked, including two applied patches. If the task needed only one instruction line, a targeted search would be a different, cheaper workload.
- Schema-only discovery offers a small improvement over an already competent `information_schema` query. The task explicitly requires every column name and type on the first turn. Both sides verify all 35 columns and answer all eight questions without schema row samples.
- Complete reads preserve all 438,365 source bytes. Packing saves repeated metadata, but cannot remove the source itself. The updated payload is still above the raw-source counterfactual: 101,792 versus 96,432 tokens.
- Extensive rewrites force two delta fallback reads. The attempted delta adds overhead before the necessary full read, producing the measured loss. Use direct reads when a rewrite is expected.

A conditional-read receipt prototype was rejected. Ordinary metadata checks already let a caller reuse unchanged text, and the prototype did not improve that baseline. It is not part of the shipped API.

## Retained context is a separate accounting view

The harness also records a hypothetical input prefix before every planned tool decision and answer. It sums previously retained message payloads plus static definitions at each step. This **cumulative input exposure** follows the scripted sequential RPC schedule; it is not provider billing or observed model behavior. Independent calls could be batched, so these counts are not a minimum exposure or a globally optimal baseline.

| Workflow | Previous cl100k exposure | Updated exposure | Reduction |
| --- | ---: | ---: | ---: |
| Deep document evidence | 142,956 | 1,993 | 98.61% |
| Small-edit runbook synchronization | 39,757 | 29,691 | 25.32% |
| Complete schema and table questions | 23,371 | 22,924 | 1.91% |
| Complete document reads | 509,985 | 493,086 | 3.31% |
| Extensive runbook rewrites | 39,867 | 46,220 | **−15.94%** |

The raw-source control supplies a source once and retains it, adding a new complete snapshot only when it changes. It does not replay the entire source on every turn. For the runbook, that counterfactual has 15,288 cumulative tokens versus 29,691 for the updated tool workflow, even though the tool workflow introduces fewer new tokens. The raw control assumes upstream knowledge of when a snapshot changes; it is an answering lower bound, not an implemented freshness protocol.

Execution plans are deterministic and use observed tool responses. No model chooses tools, evaluates relevance, or generates the recorded answers. Prompt caching, provider framing, hidden reasoning, inference timing, and billing are excluded. Tool quality, model reliability, and the best possible shell or application-specific workflow remain separate questions. The earlier Massive comparison likewise does not establish an advantage over Massive's existing `store_as`/`query_data` path.

## Enable the operations

```sh
uv run workspool --workspace ~/MyWorkbook serve --profile efficient
```

The profile adds the read-only `search`, `describe`, and `diff` operations to `save`, `find`, `inspect`, `resume`, `query`, and `refresh`. The measured version used the lazy `workspool` dispatcher and adaptive text rendering. Current profiles expose eager typed tools; that historical dispatcher configuration is an explicit `--experimental-lazy` experiment. Compatibility defaults remain the original six eager tools and compact JSON. `import` and `delete` still require an explicit allowlist. Explicit `--tools` and `--response-format` flags override profile choices.

Retrieve an operation's schema with `{"tool":"search"}`. Execute with `{"tool":"search","arguments":{"text":"release code"}}`. An omitted or null `arguments` requests a schema; an empty object executes defaults. A compatible text-only host can add `--response-content text`; structured consumers can select `structured`. Both representations remain the default.

For a fixed task, a smaller eager interface can be appropriate:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools search --response-content text
uv run workspool --workspace ~/MyWorkbook serve --tools inspect diff --response-format adaptive --response-content text
```

## Search source passages

`search(text, refs=None, limit=5, context_bytes=256, cursor=None)` scans current textual artifacts, or up to 16 distinct supplied references. Names resolve and freshness is evaluated in one catalog snapshot; returned IDs pin the searched versions. Search uses escaped literal matching with Python's Unicode case-insensitive semantics, including overlapping occurrences. It does not accept arbitrary regular expressions or perform semantic search.

Each hit contains `artifact_id`, `name`, `version`, `is_latest`, `stale_inputs`, an exact `excerpt`, its `start_byte`/`end_byte`, and the narrower `match_start_byte`/`match_end_byte`. Offsets are UTF-8 byte positions in the pinned source. Context rounds inward to character boundaries and is at most `context_bytes` on each side. Invalid UTF-8 produces an explicit error; binary references are unsupported.

Bounds are 1–500 nonblank query characters, 1–20 hits per page, 0–2,048 context bytes per side, 16 MiB of source reads per call, and a 32 KiB hit-output budget plus bounded control metadata. Window overlap preserves matches that cross read boundaries. The 64 KiB catalog index does not limit this search.

Follow `cursor` with the same query and scope until `complete` is true. An empty page with a cursor means scanning must continue. The 60-character opaque cursor binds the query, selected versions and freshness, file position, and byte offset. A changed relevant snapshot or query requires restarting without the cursor. It is a continuation, not an authorization token, and no global “already seen” state is shared across callers.

```sh
uv run workspool --workspace ~/MyWorkbook search "release code" --context-bytes 128
```

Search is bounded local scanning rather than a full inverted index. Repeated searches over very large collections can cost disk I/O even when the model receives few tokens. Narrow `refs` when the relevant sources are known.

## Table schema discovery

`describe(inputs, offset=0, limit=50, allow_historical=False)` returns a `tables` list. Each entry includes its pinned artifact identity and freshness, `input` alias, exact `columns`, matching SQL `types`, `column_count`, and `next_offset`. It returns no sample rows and does not request a row count. CSV type inference can still read local data; “schema only” describes the response, not zero disk work.

The same DuckDB file restrictions and query settings apply. At most eight distinct CSV or Parquet inputs are allowed. Each page contains 1–100 columns per table, subject to an approximately 7.5 KB budget. Column names and types are never silently clipped; an individual oversized field raises an error. Continue using each table's `next_offset` and pinned ID. A newly updated current name should be described again, or accessed explicitly as historical data.

```sh
uv run workspool --workspace ~/MyWorkbook describe --input replay/bars
```

## Exact version deltas

`diff(ref, base, max_bytes=8192)` compares two text versions of the same artifact name. `base` must be an immutable artifact ID whose **complete decoded text the caller retains**. The reply returns current artifact metadata and either `changes` or `requires_read: true` with a reason. There is no server-side assumption that a particular model has seen the base.

Successful changes are exact line splices. Apply them in reverse order:

```python
lines = base_text.splitlines(keepends=True)
for change in reversed(reply["changes"]):
    start = change["start"]
    lines[start:start + change["delete"]] = change["lines"]
current_text = "".join(lines)
```

Line endings, Unicode, and a missing final newline are preserved. An empty `changes` list leaves the body unchanged but still returns current identity and freshness. Do not treat unchanged content as unchanged dependencies.

`max_bytes` is 1–32,768 bytes and bounds a complete successful JSON delta before MCP metadata projection. It is not permission to truncate a patch. Sources are limited to 1 MiB and 20,000 lines each. Conservative changed-region and matching limits prevent unbounded quadratic work. Oversized or complex changes, or a delta no smaller than a full read, return `requires_read: true`. Fixed fallback metadata is outside the requested delta byte budget. Invalid text, unavailable versions, and corrupted blobs raise explicit errors.

On fallback, read the returned `artifact.artifact_id` through `inspect`, following every `next_offset` before replacing the retained base. If the base is unavailable or has been lost during compaction or a new session, perform a complete read first. A model context or a host-side cache must actually retain the full base; remembering its ID alone is insufficient.

```sh
uv run workspool --workspace ~/MyWorkbook diff operations/runbook --base wb_saved_version_id --max-bytes 8192
```

## Adaptive replies

`--response-format adaptive` selects the smallest supported serialized UTF-8 content representation. It can keep ordinary compact JSON, move excerpts to exact text blocks, or pack repeated artifact fields and common values once. Selection is a deterministic byte-size heuristic, not an exact tokenizer minimum. Short replies can remain ordinary JSON.

Versioned envelopes use `workspool_encoding`:

- `artifacts-v1` uses `fields` and `rows` for varying metadata, `defaults` for shared values, and explicit missing-field positions to preserve absence versus null. `single` and `extra` preserve the original outer shape.
- `text-v1` retains ordinary metadata plus raw excerpt blocks. `excerpt_rows` maps subsequent blocks to artifacts, or is `"all"` when every artifact has an excerpt.
- `json-v1` escapes an ordinary user value that happens to resemble a reserved envelope.

The public [`agent_workbook.render.decode`](../src/agent_workbook/render.py) reconstructs the original object from text blocks alone. `structuredContent`, when enabled, retains the original object. Roundtrip tests verify exact values and excerpts; they do not establish that every model interprets the packed form reliably. The separate historical `--response-format text` block contract remains available.

## Validation and reproduction

The final run uses Python 3.13.15, MCP 1.30.0, DuckDB 1.5.6, tiktoken 0.14.0, and Pydantic 2.14.3. All 18 traces and 177 decision prefixes were independently recounted with `cl100k_base` and `o200k_base`; the audit verifies comparison arithmetic, outcomes, and candidate source hashes. The full suite passes **126 tests** with both locked MCP 1.30.0 and minimum supported MCP 1.26.0. Ruff lint and formatting pass.

The fixtures are synthetic and imported before retrieval measurement. Acquisition and import commands are excluded from this workflow comparison. No API service is needed to reproduce it.

Historical comparison requires a separately retained Git checkout of the original history at `bdc6561ecf759d32868dbe982e79195cf8650dd5`, or a checkout restored from a Git bundle/archive preserving that history. This baseline is not included in the standalone snapshot's history. A source-only archive is insufficient: the harness verifies the baseline's Git commit. Set `--baseline` to that existing checkout and run from this repository's root:

```sh
uv sync --locked --extra benchmark
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/workflow_efficiency.py \
  --baseline /path/to/original-baseline --output .local-benchmarks/workflows
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/audit_workflow_efficiency.py \
  .local-benchmarks/workflows
```

`--scenarios documents table repeat full-read` selects workflow groups. Tokenizer files may download on first use; subsequent runs use the local cache. The current standalone `workspool bench` and known-input benchmark can run without this historical checkout.

Published evidence consists of [results](results/2026-11-07-workflows/results.json) and the [independent audit](results/2026-11-07-workflows/audit.json). Full traces remain local and are regenerated by the script. Runtime source hashes identify the measured changes; the recorded candidate Git commit is the checkout base because measurement preceded committing the changes. Random version IDs can cause small count differences on reruns. Historical reports retain their original assumptions and should not be mixed with this suite as direct before-and-after counts.
