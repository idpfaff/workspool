# Workspool: smaller setup and lossless response formats

The 2026-11-07 run measures three optional improvements: schemas loaded through a small dispatcher, exact excerpts outside JSON strings, and one-copy MCP responses. Defaults remain six eager tools, compact JSON, and both text and structured output. The source version remains 0.3.0; this report describes the measured working tree, identified by runtime source hashes in the [results](results/2026-11-07-efficiency/results.json).

## What was measured

Each primary count includes one copy of server instructions, advertised tool definitions, the user question, tool-call requests, and decoded text response blocks joined with newlines. All cases use real stdio MCP sessions and already-imported artifacts. Queries use known input names; document reads use known version IDs and batches of at most 16. There is no checkpoint lookup in these paths.

Raw replay supplies the same question plus every CSV row or the complete document corpus directly as text. Imports are counted separately as executed CLI command payloads and their full receipts. API acquisition, shell-tool definitions, chat framing, repeated retained context on later model turns, prompt caching, reasoning, final answers, model tool-choice reliability, and provider billing are not measured. These are payload comparisons, not end-to-end cost or speed claims.

The run uses Python 3.13.15, MCP 1.30.0, DuckDB 1.5.6, and tiktoken 0.14.0 on macOS. Both `cl100k_base` and `o200k_base` were counted. All 72 cases and 198 traces were independently recounted; the [audit](results/2026-11-07-efficiency/audit.json) reports matching counts and source hashes.

## Lazy discovery: pay for the schemas you use

Each cell below is **cl100k_base / o200k_base** tokens. “Known” assumes the caller already knows the operation's arguments. “Discover” explicitly fetches the used operation's schema once in that session and includes the extra request and reply.

| Workload | Raw replay | Default | Lazy, known | Lazy, discover |
| --- | ---: | ---: | ---: | ---: |
| 20 synthetic rows | 222 / 221 | 820 / 854 | 286 / 291 | 441 / 451 |
| 2,000 synthetic rows | 19,039 / 19,038 | 823 / 857 | 289 / 294 | 444 / 454 |
| 250 Massive daily bars | 9,827 / 9,827 | 814 / 850 | 280 / 287 | 435 / 447 |
| Three excerpts from 120 documents | 92,674 / 92,674 | 1,101 / 1,137 | 568 / 574 | 703 / 714 |
| Every word of 120 documents | 92,657 / 92,657 | 107,393 / 107,458 | 106,923 / 106,951 | 107,058 / 107,091 |

Default setup costs 652 / 685 tokens; the lazy dispatcher costs 110 / 114. For the Massive query, known lazy arguments reduce the cl100k payload by 65.60% relative to default and 97.15% relative to raw replay. Paying for discovery still reduces it by 46.56% relative to default and 95.57% relative to raw replay.

Lazy discovery does not guarantee a win on tiny inputs. One market bar costs 81 tokens directly versus 278 with known lazy arguments; five bars cost 238 versus 277. The 20-row synthetic case also loses with both tokenizers. Discovering all schemas or making many calls can erase the setup advantage. A fixed query-only host is another option: `--tools query` costs 308 / 316 tokens for the Massive case without a discovery round trip.

```sh
uv run workspool --workspace ~/MyWorkbook serve --tool-discovery lazy
```

Lazy mode advertises one tool named `workspool`. Retrieve a schema with:

```json
{"tool":"query"}
```

Execute with known or newly discovered arguments:

```json
{"tool":"query","arguments":{"inputs":["replay/bars"],"sql":"SELECT count(*) AS bar_count FROM input1"}}
```

Omitting `arguments`, or passing null, requests the schema. An empty object executes defaults, for example `{"tool":"find","arguments":{}}`. `--tools` still limits the allowed operations. `import` and `delete` remain opt-in; disabled operations cannot be reached through the dispatcher or direct named calls. Original validation, version checks, full metadata, and result semantics remain in place.

## Response representations

`--response-format text` changes how excerpt strings appear in decoded text replies. It retains compact metadata and places exact raw excerpts in separate content blocks. It does not summarize, truncate beyond the requested existing page limits, or change `structuredContent`.

The block contract is:

1. `content[0].text` is compact JSON with the original result shape. For each artifact containing an excerpt, the string `excerpt` field is replaced by an integer `excerpt_block`.
2. `content[excerpt_block].text` is that exact excerpt. Indexes are zero-based positions in the complete `content` array, so the first excerpt is at index 1. Empty excerpts still receive a block.
3. Batches retain `{"artifacts":[...]}` in the metadata, with one block pointer per excerpt. IDs, versions, freshness, dependencies, and `next_offset` retain their meanings. `inspect full=true` includes full metadata.
4. With `--response-content both`, `structuredContent` retains the original object, including string `excerpt` fields, with no block pointers.

For example, a single artifact's metadata block can be:

```json
{"artifact_id":"wb_example","name":"draft","version":1,"is_latest":true,"stale_inputs":false,"dependencies":[],"next_offset":null,"excerpt_block":1}
```

The following block contains the source text itself, including its original quotes, newlines, and Unicode. Clients reconstruct an excerpt by following the pointer, replacing `excerpt_block` with `excerpt`, and reading that block's text. They should preserve content-block boundaries instead of trying to parse a flattened transcript.

`--response-content` selects the successful result representation:

| Value | Text `content` | `structuredContent` |
| --- | --- | --- |
| `both` (default) | Present | Present |
| `text` | Present | Omitted; advertised output schemas are omitted too |
| `structured` | Empty array | Present |

Errors remain readable text. Choose a single-copy mode only for a host that consumes that representation. MCP wire JSON still escapes strings; raw-text gains apply to decoded text blocks. A host forwarding the complete serialized result has different counts, measured separately below.

### Complete reads still cost more than raw source

These paths return every source byte from all 120 documents in eight calls. Cells again show **cl100k_base / o200k_base** counts, including setup, questions, and requests. “Full result JSON” replaces decoded replies with complete serialized MCP results; it is an alternative host representation, not an additional cost to add to the primary count.

| Profile | Decoded text count | Full result JSON count |
| --- | ---: | ---: |
| Default six tools, compact, both | 107,393 / 107,458 | 216,708 / 216,665 |
| Inspect only, text format, both | 102,810 / 102,847 | 212,485 / 212,414 |
| Inspect only, text format, text only | 102,810 / 102,847 | 108,466 / 108,383 |
| Lazy, known arguments, text format, text only | 102,852 / 102,880 | 108,508 / 108,416 |

Raw replay is 92,657 tokens with either tokenizer. The inspect-only text path reduces cl100k overhead from 15.90% to 10.96%, but cannot remove the source words or the version and freshness metadata. Text-only output nearly halves the full-result representation by removing duplicated structured content; it adds no saving when a host already forwards just decoded text.

Short excerpts can lose: the three-excerpt inspect-only case costs 589 tokens with compact JSON and 603 with text format. Raw block pointers and their instructions have a cost. Use text format for substantial text reads; keep compact JSON for small replies unless measurements favor otherwise.

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools inspect --response-format text --response-content text
uv run workspool --workspace ~/MyWorkbook serve --tools query --response-content structured
```

## Real data, imports, and correctness

The optional local fixture contains 250 AAPL daily bars from Massive for 2025. The query returns row count, days closing above open, total volume, and highest high. An independent Decimal-based calculation checks the result; a separate Massive connector SQL query also agreed with `[250,128,13544474239,288.62]`. The CSV's SHA-256 is recorded in the results. Raw market rows and local workspace artifacts are excluded from the published files.

Massive's own `store_as` and `query_data` workflow already avoids sending every row to a model. This benchmark compares Workspool with raw replay, not with that workflow. It does not count upstream API fetching or imply that routing an already-small connector aggregate through Workspool saves additional tokens.

Local import costs matter on the first use. With cl100k, the Massive CLI import costs 243 tokens for its command and receipt: known lazy query plus import totals 523, and discovery plus query plus import totals 678. The synthetic 20-row import costs 241, bringing its known lazy total to 527 against 222 for raw replay. Importing all 120 documents costs 28,470; targeted known lazy retrieval plus those imports totals 29,038. Full per-case import totals are in the results. Inline saves would send source content through the model-facing request once and are not measured in this run.

Queries are checked against independently calculated values. Document paths assert exact excerpts or complete content, including artifact IDs and freshness. The repository's 50 tests pass with locked MCP 1.30.0 and minimum supported MCP 1.26.0; coverage includes single-copy transport, schema discovery, disabled operations, malformed arguments, Unicode pagination, stale dependencies, full metadata, and exact large integers. The minimum-SDK run emits one existing Pydantic forward-reference warning. Ruff lint and format checks pass.

## Reproduce

Run from the repository root. Synthetic cases require no data service. The first tokenizer use may download tiktoken encoding files; later runs can use the local cache.

```sh
uv sync --locked --extra benchmark
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/efficiency.py \
  --output .local-benchmarks/efficiency
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/audit_efficiency.py \
  .local-benchmarks/efficiency
```

To include market cases, export your own Massive aggregate CSV with the required `ticker`, `t`, `o`, `c`, `h`, and `v` columns and pass `--csv /absolute/path/to/bars.csv` to `efficiency.py`. The measured file also contains the optional `vw`, `l`, and `n` columns. The script reads it locally and does not fetch or publish data. Add `--tokenizer /path/to/Qwen2.5/tokenizer.json` to both commands for a third tokenizer; this run measured only the two tiktoken encodings. Use `--skip-documents` for table cases only or `--profiles default lazy lazy-discover` for a smaller comparison.

The script writes `results.json` and local `traces.json`; the independent audit writes `audit.json`. Published evidence includes [aggregate results](results/2026-11-07-efficiency/results.json) and the [audit](results/2026-11-07-efficiency/audit.json). Traces are regenerated locally. Fixture and runtime hashes allow exact source checks; random artifact IDs and temporary paths can slightly change counts. The recorded Git commit is the checkout base, while source hashes identify the exact implementation measured before publication.

The earlier [optimization report](OPTIMIZATION.md) and [baseline](RESULTS.md) retain historical Qwen counts, shell controls, larger sources, and cold-start lifecycle comparisons. They use different workflows and should not be treated as direct before-and-after measurements for this run.
