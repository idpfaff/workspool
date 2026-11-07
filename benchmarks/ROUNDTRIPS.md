# Workspool: synchronization and combined retrieval

The 2026-11-07 deterministic run compares `sync` and `retrieve` with competent v0.4.1 workflows. `sync` reduces new payload tokens by 12.83% for sparse edits and 0.89% for dense rewrites against the strongest measured controls. Tiny synchronization costs 0.64% more. `retrieve` has a narrower benefit: 16 scattered reads from one source save 3.75% of new payload tokens, but do not reduce the measured retained-input exposure. Simple reads and nearby facts are cheaper with existing tools.

These are measurements of real MCP payloads under fixed plans, not provider billing, model reliability, or proven latency improvements. The [aggregate results](results/2026-11-07-roundtrips/results.json) contain every control, token component, source hash, and correctness check. The [independent audit](results/2026-11-07-roundtrips/audit.json) recounts 29 arms, 150 tool calls, and 237 scripted decision prefixes with both tokenizers.

Each token cell below is **cl100k_base / o200k_base**. Counts include cold server instructions and tool schemas, user requests, tool-call arguments, actual formatted text replies, and fixed final answers. Savings are relative to the named control; negative values mean the candidate costs more.

| Complete-source synchronization | v0.4.1 control | Control tokens | `sync` tokens | Payload savings |
| --- | --- | ---: | ---: | ---: |
| Sparse edits | Inspect once, then exact `diff` | 2,651 / 2,663 | 2,311 / 2,319 | 12.83% / 12.92% |
| Dense rewrites | Check metadata; read only changed versions | 4,739 / 4,744 | 4,697 / 4,701 | 0.89% / 0.91% |
| Tiny source | Read the complete text every turn | 1,088 / 1,095 | 1,095 / 1,101 | −0.64% / −0.55% |

Each synchronization session has eight requests, two source changes, and an unrelated artifact change. The task explicitly requires retaining every source byte for later editing. All arms verify the complete current text, version, and freshness after each turn. `sync` uses the version number returned by the prior completed read as its base; existing `diff` requires the retained immutable ID. These are separate fixed policies, with no per-turn selection based on the known change. The results retain metadata-first and diff-first controls for every source size, plus direct reading for the tiny source.

Sparse synchronization uses eight calls with either `diff` or `sync`. Dense synchronization uses ten metadata/read calls versus eight `sync` calls because `sync` returns full content directly when a delta is unsuitable. Relative to the controls in the table, cl100k retained-input exposure falls by 10.51% for sparse edits and 11.30% for dense rewrites. Tiny synchronization differs by less than 1%.

The sparse-range sweep requests exact, disjoint 256-byte ranges from one already supplied immutable artifact ID. The file is 104,400 bytes; the largest request reads 4,096 bytes, or 3.92% of it. Both arms use the same ID. The control sends all independent `inspect` calls in one scripted decision, so sixteen tool calls are not treated as sixteen model turns.

| Ranges from one source | `inspect` tokens | `retrieve` tokens | Payload savings | Tool calls, control → candidate |
| --- | ---: | ---: | ---: | ---: |
| 1 | 390 / 398 | 664 / 685 | −70.26% / −72.11% | 1 → 1 |
| 4 | 992 / 1,009 | 1,186 / 1,213 | −19.56% / −20.22% | 4 → 1 |
| 16 | 3,437 / 3,490 | 3,308 / 3,359 | 3.75% / 3.75% | 16 → 1 |

Only sixteen ranges cross the new-payload break-even point among the tested sizes. Larger `retrieve` schemas still matter: cold setup is 389 / 403 tokens versus 166 / 171 for `inspect`. At sixteen ranges, cl100k retained-input exposure is 4,252 versus 4,158 for the parallel control, a 2.26% increase. Fewer RPCs therefore do not establish fewer billable model tokens or model decisions. The whole source was not a competing strategy in this range-specific case; its content is over 25 times the requested bytes.

The question workflows use three documents and independently generated expected values. The caller knows the requested field names, but not their values, document references, or byte offsets. A second question repeats an already observed fact, and every arm answers it without another tool call. Nearby fields share one context window; dispersed fields are separated by more than 1 KiB.

| Question workflow and control | Control tokens | `retrieve` tokens | Payload savings | Tool calls, control → candidate |
| --- | ---: | ---: | ---: | ---: |
| Nearby facts; search one missing field, then reuse its context | 1,696 / 1,699 | 2,151 / 2,174 | −26.83% / −27.96% | 2 → 2 |
| Dispersed facts; issue independent searches together | 3,035 / 3,037 | 3,062 / 3,085 | −0.89% / −1.58% | 4 → 2 |

Combined retrieval saves 29.73% of cl100k payload against a plan that searches every nearby field independently, and 12.20% against context-free searches followed by merged reads. Both controls are included in the results. They are more expensive than the incremental policy on this fixture: its first search already reveals the neighboring answers. These gains should not be presented as improvements over the strongest measured policy.

Dispersed facts remove that reuse advantage, but the candidate has a small payload increase and costs 3.46% more cl100k retained-input exposure than parallel searches. Compared with sequential incremental searches, it reduces retained exposure by 16.24%; that comparison reflects the chosen scheduling policy. The parallel baseline remains the appropriate control for a claim about avoiding model turns.

Existing read operations also remain useful:

| Read task | Batched `inspect` tokens | `retrieve` tokens | Candidate payload increase |
| --- | ---: | ---: | ---: |
| Four uneven ranges across three sources | 969 / 981 | 1,205 / 1,229 | 24.36% / 25.28% |
| Complete uneven documents, including Unicode and an empty source | 11,014 / 11,023 | 12,086 / 12,118 | 9.73% / 9.93% |
| One tiny exact note | 274 / 280 | 544 / 563 | 98.54% / 101.07% |

The evidence supports `sync` for maintaining a complete retained source, and selective use of `retrieve` for many independent ranges. For nearby answers, inspect one useful search result before requesting more evidence. For a simple read, `inspect` has lower overhead in these cases. This suite does not support replacing all reads or searches with `retrieve`.

Both implementations run as isolated stdio MCP subprocesses under Python 3.13.15, MCP 1.30.0, DuckDB 1.5.6, and tiktoken 0.14.0. The baseline is a `git archive` of v0.4.1, commit `094be81de2a77686726a77b17aeed0b9ee4dc688`. Candidate file hashes identify the measured working tree. Both arms expose only their required eager tool schemas and use adaptive text-only responses. Fixture IDs are deterministic, valid, and identical across arms.

The runner decodes the actual returned text blocks, never hidden structured content. It verifies exact UTF-8 ranges, complete source reconstruction, delta application, pinned continuations, source status, every literal match against an independent full-source oracle, and every requested answer. Shortened or deferred read slots continue until the full requested range is retained; tests force partial progress and reject a completely stalled continuation. Different page boundaries are allowed, while reconstructed bytes must agree. Source and dependency versions must remain unchanged during a run.

`new_payload_tokens` counts each canonical message once, plus setup once. `retained_input_exposure` sums setup and the entire retained canonical transcript before each scripted decision. Independent tool calls share a decision; a final answer is another decision. Both metrics exclude provider message framing, cached-input discounts, model reasoning, inferred tool choice, and billing. Source acquisition/import happens before measurement and is excluded. Single-run local request timing is diagnostic only. Output-token budgets have separate runtime tests; this default-budget comparison is not a benchmark of constrained-budget behavior.

Reproduce from the repository root with the v0.4.1 tag available:

```sh
uv sync --locked --extra tokens
uv run --locked --extra tokens python benchmarks/roundtrip_efficiency.py \
  --output .local-benchmarks/roundtrip-final
uv run --locked --extra tokens python benchmarks/audit_roundtrip_efficiency.py \
  .local-benchmarks/roundtrip-final
uv run --locked --extra tokens pytest -q tests/test_roundtrip_benchmark.py
```

The first tokenizer use may download encoding files. The runner writes complete local traces and aggregate results; the auditor writes its independent recount. Published artifacts contain aggregates and hashes, while synthetic traces can be regenerated locally. No inference calls or private market data are required.
