# Workspool v0.2.0: reducing MCP overhead

> Measured at the v0.2.0 revision (`d643436`). The audit hashes identify that revision. v0.3.0 preserves the default definitions and instructions, while new features and different artifacts can change response sizes. Hosts that opt into `import` or `delete` add those definitions. These historical counts do not measure model reasoning, final answers, provider billing, or inference speed.

The larger test exposed avoidable overhead. This update reduces it in the server, and records the remaining limits.

## Same benchmark, same questions

The original benchmark script was rerun without modification against v0.2.0. The source fixtures and exact answers matched v0.1.1. Both tokenizers were recounted from the locally captured traces, including full MCP-result JSON as an alternate host representation.

Qwen2.5 counts below include the MCP definitions, server instructions, questions, request arguments, and actual text replies. These are warm retrieval sessions; cold-start and lifecycle totals are in the JSON results.

| Workload | v0.1.1 tokens | v0.2.0 tokens | Reduction from v0.1.1 |
| --- | ---: | ---: | ---: |
| 20-row handoff | 1,620 | 1,090 | 32.72% |
| 2,000-row handoff | 1,630 | 1,114 | 31.66% |
| 20,000-row handoff | 1,636 | 1,110 | 32.15% |
| 200,000-row handoff | 1,645 | 1,111 | 32.46% |
| 1,000,000-row handoff | 1,621 | 1,119 | 30.97% |
| 120-document lookup, 3 excerpts | 2,946 | 1,715 | 41.79% |
| Read every word of 120 documents, separate calls | 153,253 | 131,068 | 14.48% |

The default session setup decreased from **934 to 661 Qwen tokens**. The 20,000-row handoff decreased from **1,636 to 1,110**, a **32.15% reduction**. The document lookup decreased from **2,946 to 1,715**, a **41.79% reduction**.

## What changed

- **Compact replies by default.** MCP text uses compact JSON. Artifact receipts omit repeated descriptive metadata, while keeping IDs, names, versions, freshness flags, dependencies, requested excerpts, and checkpoint/query data. Request `inspect` with `full: true` for hashes, source labels, timestamps, sizes, and other metadata.
- **Smaller tool definitions.** Generated schema titles and null-default annotations are removed. Equivalent simple nullable/primitive unions use shorter JSON Schema forms. An unconstrained object output schema is omitted. Runtime argument validation and the returned structured objects remain in place.
- **Batch inspection.** `inspect.ref` accepts one reference or a list of up to 16. A batch resolves versions and freshness within one catalog snapshot, with a separate 8 KiB excerpt cap per artifact. Invalid, duplicate, or aliased duplicate references are rejected. Pagination remains available.
- **Explicit tool allowlists.** `serve --tools resume inspect` exposes only those two tools and removes the others from registration. Smaller tool sets also get shorter relevant instructions.

The default six-tool setup benefits immediately. Batch calls and restricted tool sets are optional client/configuration changes and are measured separately below.

## Explicitly optimized workflows

The additional script uses the same synthetic sources and verifies the same requested values or complete document bytes. These paths are not presented as unchanged-client comparisons.

| Path | Qwen tokens | cl100k_base tokens |
| --- | ---: | ---: |
| `targeted_batch_all-tools` | 1,669 | 1,484 |
| `targeted_batch_two-tools` | 1,194 | 1,015 |
| `complete_reread_batch_all-tools` | 128,088 | 107,349 |
| `complete_reread_batch_one-tool` | 127,570 | 106,837 |
| `known_input_20_all-tools` | 851 | 817 |
| `known_input_20_one-tool` | 336 | 311 |
| `known_input_2000_all-tools` | 861 | 822 |
| `known_input_2000_one-tool` | 346 | 316 |

`targeted_batch_two-tools` uses `resume` and one three-reference `inspect`, with only those tools exposed. The original lookup used four calls and 2,946 Qwen tokens; the configured batch path uses two calls and **1,194 tokens**, about **59.47% less**. The same path with all six tools exposed uses **1,669**.

The complete-document batch reads every source byte in **eight calls**, compared with 120 separate read calls. Its all-tools cost is **128,088 Qwen tokens**. The inspect-only configuration costs **127,570**, versus **110,777** for the raw corpus. Extra overhead falls from **38.34% to 15.16%**, but reading every word still costs more than supplying raw text.

For a known 2,000-row input, a query-only host uses **346 Qwen tokens**, versus **861** with all six tools exposed. This path queries the known artifact name directly, so it does not perform a checkpoint lookup first. Its local import cost is recorded separately.

## Limits that remain

A 20-row aggregate with the default handoff workflow still costs **1,090 Qwen tokens**, versus **333** for the raw input. The known-input query-only path is near break-even with Qwen (**336 vs 342**), but loses with cl100k_base (**311 vs 221**). Small inputs do not provide consistent savings across tokenizers.

An initial inline save still sends the full source through the model-facing request. The 2,000-row cold start costs **34,914 Qwen tokens**, versus **33,811** raw. Counting that cold start and four reopened sessions, the total is **39,270 vs 169,055**, a **76.77% reduction** across five sessions.

Reading all source content cannot eliminate the tokens in that content. Batch reads and smaller metadata reduce protocol overhead. Targeted retrieval or local processing is still how this workbook avoids the largest context costs. Ordinary local SQL and targeted file access remain useful, often cheaper alternatives for isolated tasks.

These are scripted payload measurements. No provider billing, model reasoning, automatic tool-choice reliability, or MLX inference speed is measured. SDK `structuredContent` is still provided along with the JSON text for agent compatibility. A host that forwards both can pay for both; the alternate full-result counts are included in the results.

## Compatibility and validation

v0.2.0 changes the default MCP metadata projection. To retain the earlier complete field set for all MCP replies, start with `serve --response-format full`. One `inspect` request can also use `full: true`. CLI and direct Python inspection continue to return full metadata.

**21 regression tests passed** with the locked MCP 1.30.0 SDK and with the minimum supported MCP 1.26.0 SDK. Coverage includes HTTP authentication, restricted tools, exact large integers, nullable query parameters, paged Unicode, full metadata access, version freshness, and batch validation.

The unchanged suite opened **47 MCP sessions**. The optimized-path suite opened **nine** more. All **59 traces** across the two suites were independently recounted with both tokenizers, and every recount matched. All original fixture hashes and independent expected answers matched the baseline.

## Reproduce and inspect

Use the same tokenizer SHA-256 as the [original larger test](RESULTS.md). Run these from the repository root:

```sh
uv sync --locked --extra benchmark
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/token_reduction.py \
  --tokenizer /path/to/tokenizer.json --output /tmp/workspool-default
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/optimized_paths.py \
  --tokenizer /path/to/tokenizer.json --output /tmp/workspool-optimized
```

| Evidence | Default workflow | Optimized paths |
| --- | --- | --- |
| Script | [Unchanged original script](token_reduction.py) | [Optimized-path script](optimized_paths.py) |
| Results | [JSON](results/2026-11-07-v0.2.0/results.json) | [JSON](results/2026-11-07-optimized/results.json) |
| Traces | Generated locally by the script | Generated locally by the script |
| Independent audit | [JSON](results/2026-11-07-v0.2.0/audit.json) | [JSON](results/2026-11-07-optimized/audit.json) |

Fixture hashes, expected answers, runtime versions, source commits, and per-category counts are included in the original and unchanged-suite results. Metadata IDs, timestamps, and temporary paths cause small variations on reruns.

The two compressed v0.2.0 trace captures are retained locally. Automatic approval review rejected their public upload because the complete opaque contents were not cleared for disclosure. The readable results, trace digests, independent audit summaries, and reproduction scripts are published.

Recorded `code_commit` values identify the local checkout at execution. Publication can create a different GitHub commit. The audit files include SHA-256 hashes of the runtime and benchmark source files so the published implementation can be checked directly.
