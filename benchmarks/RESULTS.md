# Workspool: larger token reduction test

Run on November 7, 2026. Workspool v0.1.1, Linux x86_64, Python 3.12.14.

For the current server, see the [v0.2.0 overhead fixes and comparison](OPTIMIZATION.md). This report preserves the v0.1.1 baseline.

## What the test supports

Workspool substantially reduces the tokens needed to revisit larger saved inputs when the agent retrieves a checkpoint, selected evidence, or a local calculation. It does not compress everything an agent reads, and it is not always cheaper than existing local tools.

This run used **47 independent MCP sessions**, five sessions per primary fixture, two tokenizers, and exact answer checks. There were **219 MCP tool calls and 127 local import commands**. The original 18 product tests also passed.

These are measured payload token counts from scripted tool calls. No LLM inference or provider billing was measured. The results do not establish how reliably a model chooses the tools, or its total cost to complete an unscripted task.

## Primary result: one session with previously saved work

These counts include the question, server instructions, all six MCP tool definitions and output schemas, request arguments, and the actual text returned by `resume` plus `query` or `inspect`. They do not include the initial import and checkpoint creation; the next table does.

Qwen2.5 tokenizer:

| Workload | Raw context tokens | Workspool tokens | Change |
| --- | ---: | ---: | ---: |
| 20 rows | 333 | 1,620 | 386.49% more |
| 2,000 rows | 33,811 | 1,630 | 95.18% fewer |
| 20,000 rows | 357,980 | 1,636 | 99.54% fewer |
| 200,000 rows | 3,779,537 | 1,645 | 99.96% fewer |
| 1,000,000 rows | 19,343,156 | 1,621 | 99.99% fewer |
| 120 documents, retrieve 3 excerpts | 110,800 | 2,946 | 97.34% fewer |

The document task asks for the decision, owner, and review code in three named records. Workspool retrieves three 200-byte excerpts using pinned IDs from a checkpoint. It does not summarize all 120 documents. Each selected value was checked against the generated source.

The million-row raw baseline is a counterfactual token count. Those rows were tokenized, but were not sent to a model and may not fit its context window. Workspool calculated the result locally and returned the same four exact aggregates in all five sessions.

## Charge the import and checkpoint too

The recommended local-file workflow does not route the raw source through the model. This table charges every executed import command and its stdout, checkpoint creation, its MCP setup, and all five independent retrieval sessions. Each session is charged the MCP definitions again. The raw baseline repeats the question and complete source in each session.

| Workload | Raw context tokens | Workspool tokens | Change |
| --- | ---: | ---: | ---: |
| 20 rows, 5 sessions | 1,665 | 9,534 | 472.61% more |
| 2,000 rows, 5 sessions | 169,055 | 9,595 | 94.32% fewer |
| 20,000 rows, 5 sessions | 1,789,900 | 9,629 | 99.46% fewer |
| 200,000 rows, 5 sessions | 18,897,685 | 9,679 | 99.95% fewer |
| 1,000,000 rows, 5 sessions | 96,715,780 | 9,550 | 99.99% fewer |
| 120 documents, 5 sessions | 554,000 | 45,902 | 91.71% fewer |

This measures repeated independent sessions. It does not model a provider's prompt cache or assume that a continuing session would otherwise resend uncached source five times.

## Cold start through MCP

If an agent generates the entire source inside a `save` argument, the source still costs tokens.

For the 2,000-row fixture, the first session's inline save, query, and checkpoint used **35,405 tokens**, versus **33,811** for the raw handoff. That is **4.71% more**. Counting that cold start and four actual reopened sessions, Workspool used **41,833**, versus **169,055**, a **75.25% reduction** over five sessions.

Those inline source tokens are model-output-like request tokens, while response and schema tokens are model-input-like. The totals are an unweighted count of both directions, not a dollar-cost estimate.

## Negative control: reading everything back

When the task required every word of all 120 documents, Workspool used **153,253 tokens**, versus **110,777** for the raw corpus. That is **38.34% more**. All documents were retrieved and checked byte for byte. Per-call arguments and metadata add overhead when nothing can be left out.

The 20-row fixture also loses. The complete raw input is cheaper than establishing and reading a workbook. A short existing handoff note may be enough for a small task.

## Stronger controls: use ordinary local tools

A direct local DuckDB aggregate over the 20,000-row file used a canonical shell-tool payload of **264 Qwen tokens**, versus **1,636** for Workspool. Direct access to the same three document excerpts used **392**, versus **2,946** for Workspool.

The SQL command and file-reading command were executed, and their outputs were checked. The generic shell schema is a simple reference schema, not the schema of a particular agent product. These controls do not include checkpoint creation, stored provenance, freshness tracking, or artifact version references.

Ordinary SQL and targeted file reads can already keep raw data out of a model's context. Workspool adds persistent checkpoints, immutable artifact references, versions, and freshness checks. The benchmark does not show that it beats a well-designed agent that already manages those things with other tools.

## Correctness and coverage

- Deterministic synthetic CSV fixtures of 20, 2,000, 20,000, 200,000, and 1,000,000 rows. Four aggregates in each session were checked against a separate Python calculation.
- 120 synthetic engineering review documents, each containing a decision, owner, and review code. Targeted retrieval, indexed search, and complete rereading were checked against the source text.
- Grouped aggregates, a filtered top-five ranking, and a two-input threshold join on 200,000 rows. Every returned row matched an independently calculated Python result. Those three tasks used 1,657, 1,711, and 1,756 Qwen tokens respectively, including MCP setup.
- A 20,000-row source update marked a saved result stale. A query against the old input was rejected, and `refresh` produced the independently calculated result for the new version.
- The counts for all 48 recorded traces were independently recounted with both tokenizers and matched the report. The 48 traces include one import-only trace; 47 opened actual MCP sessions.

All scripted checks passed. No Workspool runtime changes were needed for these tests.

## Second tokenizer

`cl100k_base`, via tiktoken 0.14.0, gave the same overall conclusion. This is a tokenizer cross-check, not a claim about the current tokenizer or billing of any named hosted model.

| Workload | Raw context tokens | Workspool tokens | Change |
| --- | ---: | ---: | ---: |
| 20 rows | 213 | 1,515 | 611.27% more |
| 2,000 rows | 19,029 | 1,514 | 92.04% fewer |
| 20,000 rows | 199,014 | 1,526 | 99.23% fewer |
| 200,000 rows | 1,998,856 | 1,531 | 99.92% fewer |
| 1,000,000 rows | 9,998,107 | 1,522 | 99.98% fewer |
| 120 documents, retrieve 3 excerpts | 92,674 | 2,562 | 97.24% fewer |

## Accounting and limits

Tokens are counted separately for server instructions, serialized tool definitions, prompts, `tools/call` request objects, and actual MCP text responses, then summed. Tool definitions include output schemas. The totals include request arguments as well as tool responses.

`results.json` also records `full_result_json_tokens`: it replaces each text response with the complete MCP `CallToolResult`, including the SDK's duplicate `structuredContent`. Hosts differ in whether they forward one representation or both. That alternate count is a sensitivity check, not a measured host integration.

The test excludes model reasoning, final answers, role framing, host-specific tool wrappers, prompt caching, and initialize/list-tools transport envelopes. Metadata such as UUIDs, timestamps, and temporary paths can slightly change token counts between runs. No MacBook or MLX inference latency, memory reduction, or end-to-end agent quality was measured.

The raw baselines intentionally represent replaying the entire source. They are useful for measuring avoided context, but are not the strongest available alternative. The direct SQL and file-access controls are included for that reason.

## Reproduce and inspect

Run from the repository root. The tokenizer file must match SHA-256 `c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539` for the reported Qwen counts.

```sh
curl -L https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct/resolve/main/tokenizer.json -o /tmp/workspool-tokenizer.json
uv sync --locked --extra benchmark
uv run --locked --extra benchmark --with tiktoken==0.14.0 python benchmarks/token_reduction.py \
  --tokenizer /tmp/workspool-tokenizer.json \
  --output /tmp/workspool-results
```

- [Benchmark source](token_reduction.py)
- [Machine-readable results](results/2026-11-07/results.json)
- [Complete captured traces, gzip](results/2026-11-07/mcp-traces.jsonl.gz)
- [Independent recount and test audit](results/2026-11-07/audit.json)

The harness SHA-256 for this run is `4ff22d946966f04c846ebaba7d66cefbe4859ec263b9cbec53561f633bb51905`. Runtime dependency versions, fixture hashes, expected answers, and per-call accounting are recorded in the results and traces. Source imports stay local and require no model-provider credentials.
