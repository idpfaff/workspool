# Model-driven evaluation: correctness improved, token savings varied

On 2026-11-07, `gpt-6-luna` completed **all six executions** in one corrected run: three synthetic tasks, each run once against v0.4.1 and once against the updated implementation. The updated interface used **8.96% more provider input tokens overall**. It reduced input tokens for the dense rewrite task, but increased them for sparse edits and document lookup. This is a small diagnostic sample, not a statistical performance estimate or a universal savings claim.

An earlier diagnostic failed on two updated-interface executions. That failure exposed an ambiguity in `sync` and led to a runtime correction. Both runs are retained below; the failed answers are not counted as savings.

## Corrected run

These are actual Responses API usage counts, summed across every model request in each execution. **Cache read, cache write, and reasoning are provider detail fields, not additional tokens to add to input/output totals.** Cached input and cache write are reported separately; no provider-price or billing calculation is made.

| Task | Interface | Correct | Input | Output | Cache read | Cache write | Reasoning |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Unknown document passages | v0.4.1 | Yes | 2,310 | 166 | 0 | 1,144 | 16 |
| Unknown document passages | Updated | Yes | 3,475 | 194 | 0 | 1,498 | 38 |
| Sparse runbook edit | v0.4.1 | Yes | 2,767 | 96 | 2,598 | 163 | 0 |
| Sparse runbook edit | Updated | Yes | 3,023 | 96 | 1,423 | 1,594 | 0 |
| Dense runbook rewrite | v0.4.1 | Yes | 4,452 | 132 | 4,029 | 414 | 0 |
| Dense runbook rewrite | Updated | Yes | 3,885 | 95 | 1,420 | 2,459 | 0 |
| **Three-task total** | **v0.4.1** | **3/3** | **9,529** | **394** | **6,627** | **1,721** | **16** |
| **Three-task total** | **Updated** | **3/3** | **10,383** | **385** | **2,843** | **5,551** | **38** |

Updated input changed by **+50.43%** for passages, **+9.25%** for sparse edits, and **−12.74%** for dense rewrites. Input plus output changed by +48.18%, +8.94%, and −13.18%, respectively. Input includes repeatedly supplied context, instructions, tool schemas, previous calls, and tool results; it is not the once-only payload accounting used in [the deterministic roundtrip benchmark](ROUNDTRIPS.md).

| Task | Interface | Provider requests | Tool calls | Tool errors recovered | End-to-end seconds | Sum of tool milliseconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Passages | v0.4.1 | 4 | 3 | 1 | 3.846 | 15.2 |
| Passages | Updated | 4 | 3 | 1 | 5.611 | 20.1 |
| Sparse edit | v0.4.1 | 2 | 1 | 0 | 2.107 | 10.1 |
| Sparse edit | Updated | 2 | 1 | 0 | 1.809 | 9.0 |
| Dense rewrite | v0.4.1 | 3 | 2 | 0 | 3.088 | 14.6 |
| Dense rewrite | Updated | 2 | 1 | 0 | 1.927 | 8.6 |

Timing starts after MCP startup and includes model requests, tool execution, and loop overhead. Tool milliseconds sum individual call durations; parallel calls can overlap. Single observations with uncontrolled service/cache conditions do not establish a latency advantage.

### What the model actually did

- **Passages:** Both interfaces searched for `atlas`, guessed an invalid workbook/root reference with `inspect`, then searched for `renewal_code`. The returned surrounding text contained the required renewal codes, owners, and statuses for three documents. The updated interface exposed `retrieve`, but the model **never selected it**. It paid for the extra schema and chose a larger final search context. This run demonstrates no realized model-driven benefit from fused retrieval.
- **Sparse edit:** v0.4.1 selected `diff`; the updated interface selected `sync`. Both answered correctly after one tool call. The additional advertised operation and instructions increased setup/context cost. The model used an immutable base ID here even though the updated operation also accepts a shorter retained version number.
- **Dense rewrite:** v0.4.1 selected `diff`, received a fallback, then inspected a short current excerpt. The updated interface selected `sync` with the old artifact ID and integer base `1`. Corrected sync resolved the current version and returned its text immediately. The updated execution avoided one model/tool round trip, though it received more source than this question needed. A targeted search was available to both interfaces and could have been a cheaper choice; the harness did not force either plan.

Before receiving any tool result, the first provider request used 339 versus 608 input tokens for passages, 1,302 versus 1,426 for the sparse task, and 1,299 versus 1,423 for the dense task. With identical prompts within each pair, those increases show the cost of the extra advertised interface and instructions: **269 tokens for passages and 124 for each runbook task**. More available operations do not ensure fewer tokens.

Cache warmth was not controlled. The corrected-run baseline already had cache hits on the first requests for both runbook tasks; the updated interface did not. Earlier diagnostic requests may have influenced those cache states. Do not infer relative billed cost from either total input alone or these cache details.

## The failed diagnostic and the correction

The initial run passed all three v0.4.1 executions but only the updated passages execution: **4/6 correct overall, 1/3 for the updated interface**. In both updated runbook tasks, the model supplied an old artifact ID as `sync.ref`. It used integer base `1` for the sparse task and the old immutable base ID for the dense task. The original implementation treated that reference as a historical target, returned unchanged old content with `is_latest: false`, and the model nevertheless answered with version 1 and `route-old`.

The API now makes initial sync a current-state operation. At `offset=0`, a name or artifact ID resolves to the latest version of that name in one catalog snapshot. At `offset>0`, full-page continuation stays pinned to the returned immutable ID so concurrent updates cannot mix versions. `inspect` remains the explicit historical reader. Regression tests cover old-ID initial requests, exact sparse/dense results, integer CLI bases, and pagination across concurrent updates.

The corrected run again used an old ID for the dense task and returned version 2 with `route-current`. All six answers then passed. This verifies the observed failure path in this sample; it does not establish a general model reliability rate.

| Initial diagnostic | Correct | Input | Output | Cache read | Cache write | Reasoning |
| --- | --- | ---: | ---: | ---: | --- | ---: |
| v0.4.1, three tasks | 3/3 | 9,434 | 396 | 4,038 | Unavailable | 16 |
| Updated, three tasks | 1/3 | 9,261 | 379 | 2,847 | Unavailable | 14 |

The initial harness did not record cache-write usage; it is **unavailable**, not zero. Its lower token counts on incorrect answers are not an efficiency win. The runtime and instructions changed between runs, so pooling them as repeated trials would also be misleading.

## Method and limits

The [harness](model_efficiency.py) uses the fixed OpenAI Responses endpoint with `gpt-6-luna`, low reasoning effort, `store=false`, at most six model requests per execution, at most 2,000 output tokens per request, and a shared limit of 36 requests. Both completed runs used 17 requests each. Authentication/quota errors abort immediately; exhausted limits, missing usage, incorrect answers, and answers without successful tool evidence cannot count as wins.

Each task uses identical prompts, expected answers, and cloned synthetic workbooks across its pair. The document task asks for nine exact facts from initially unknown references. Runbook tasks supply the complete retained base and ask only for the current instruction line, artifact ID, version, and freshness—not a full-document hash or reconstruction. Answers are checked against fixture truth with exact JSON values and types.

The model sees actual eager MCP operation schemas and server instructions. Tool responses use adaptive rendering with text-only content. The passages baseline exposes `search` and `inspect`; the updated arm adds `retrieve`. Runbook baselines expose `search`, `inspect`, and `diff`; the updated arm adds `sync`. `strict=false` preserves the tools' optional arguments. The model chooses the tools, their arguments, and whether to issue independent calls together. Returned reasoning and function-call items are replayed in memory along with matching function-call outputs; reasoning is not replaced with a scripted answer.

This is one model alias, one run of three paired tasks, one machine, synthetic data, and no randomized arm order or repeated-trial analysis. It excludes acquisition/import and MCP startup, and has no direct shell/local-file comparison. The fixtures are deliberately small enough to finish within the request cap. Results do not establish performance on real repositories, large collections, other models, complete-document editing, or an optimal tool-selection policy. The reported model is the provider's `gpt-6-luna` alias, not a pinned model snapshot.

## Artifacts and provenance

- Corrected [aggregate results](results/2026-11-07-model/results.json) and [synthetic traces](results/2026-11-07-model/traces.json.gz).
- Initial [diagnostic aggregates](results/2026-11-07-model/initial-results.json) and [synthetic traces](results/2026-11-07-model/initial-traces.json.gz), retaining the two incorrect answers.
- [Artifact and arithmetic audit](results/2026-11-07-model/audit.json): request/execution usage sums, exact answer checks, paired fixture/prompt equality, compressed artifact hashes, and corrected source-hash verification.

Traces contain synthetic prompts, server instructions, advertised schemas, tool arguments/replies, answers, per-request usage, and timing. Provider response IDs are removed and tool call IDs normalized. They contain no credentials, local absolute paths, or encrypted reasoning. The latter was held only in memory, so these are tool/usage traces rather than complete replayable provider response bodies.

The baseline is the isolated source archive at v0.4.1, commit `094be81de2a77686726a77b17aeed0b9ee4dc688`, archive SHA-256 `510adcaf9d98dd9afe1c4bf71df7c2070a3662b7ca1ac63b556d1ddcf7e08f14`. Each report records every measured runtime and harness source hash. Both runs reported unchanged sources during measurement; corrected hashes also matched the publication checkout. The initial hashes identify the superseded diagnostic implementation, which cannot be reproduced from the corrected tree without restoring that earlier source.

## Reproduction

After configuring an authorized API key in the environment, or in a private ignored env file, run from the repository root:

```sh
uv sync --locked
uv run --locked python benchmarks/model_efficiency.py \
  --env-file .env.local --model gpt-6-luna \
  --output .local-benchmarks/model-eval
```

Omit `--env-file` to use `OPENAI_API_KEY` from the environment. The optional env file must be a private regular file with one literal key assignment; it is never sourced as shell code. Live reproduction makes paid provider requests within the configured bounds. Provider behavior, caching, and timings may differ on rerun. Local harness tests use a simulated provider and actual MCP subprocesses without making live API calls.
