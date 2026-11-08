# Model-selected task and approval loops

**The corrected follow-up passed 17/20 cases with the eager control and 1/20
with the lazy candidate under the six-round limit. This evaluation does not
show an efficiency win.** Those counts use the separately disclosed exact-numeric
audit below. The original strict scores, every failed attempt, and both measured
source snapshots remain published.

This is a same-capability, single-copy interface comparison using `gpt-6-luna`
on November 8, 2026. Both arms had the same ten actual Workspool tools, writer
permissions, synthetic inputs, required task graph, and approval rules. The
control received eager schemas and full JSON text replies; the candidate used
lazy discovery and adaptive text replies. Neither received duplicate structured
and text copies. This is **not Workspool versus SQL/files**, nor a test of whether
an agent independently chooses to adopt tasks: the workload explicitly requires
a reusable gated task.

## Results, including the grading correction

Each run contained 20 predeclared paired cases, with four independent workbooks
per scenario: approval allowed, approval refused, approval revoked before
completion, source changes invalidating an approval, and a fresh-agent handoff.
Each arm had a preparation phase and a follow-up phase. A case passes only if
both phases satisfy their answer and stored-task checks; a correct required
waiting outcome counts as a pass. These are 20 cases per arm, not 40 independent
phase samples.

| Run | Original strict control | Original strict candidate | Supplementary control | Supplementary candidate |
| --- | ---: | ---: | ---: | ---: |
| Initial | 12/20 | 1/20 | **19/20** | **2/20** |
| Guidance-corrected follow-up | 9/20 | 1/20 | **17/20** | **1/20** |

The initial scoring rule incorrectly rejected integer-string previews of exact
`DECIMAL(38,0)` results. The supplementary audit examines every arm, accepts
only canonical integer encodings backed by hash-verified integral numeric
Parquet columns, and compares exact stored values with the original integer
oracle. It requires an already-correct model answer, valid gated graph, current
sources, and actual current publication. It never accepts floats, rounding,
booleans, text columns, or unrelated failures. It corrects nine initial and
eight follow-up phase scores, without changing model answers or calling the
model again. Both original and supplementary scores are retained.

| Scenario, four cases each | Initial control / candidate | Follow-up control / candidate |
| --- | ---: | ---: |
| Approved | 4 / 1 | 4 / 0 |
| Refused | 4 / 0 | 3 / 0 |
| Revoked before completion | 3 / 1 | 4 / 0 |
| Sources changed | 4 / 0 | 2 / 1 |
| Fresh-agent handoff | 4 / 0 | 4 / 0 |

Descriptive 95% Wilson intervals for follow-up case passes are 64.0–94.8% for
the control and 0.9–23.6% for the candidate. The fixed scenarios and one model
alias do not represent a random sample of all agent workloads. The follow-up
reuses the same cases; the runs must not be pooled as 40 independent cases per arm.

## What failed

The initial server instructions recommended `recall`, although the identical
restricted allowlist in both arms omitted it. The candidate made 19 rejected
recall calls across 16 cases. That configuration defect was fixed before the
follow-up. Only `server.py` changed between measured source snapshots;
protocol files are byte-identical, and model prompts, cases, tools, permissions,
limits, and grading rules remained unchanged. The repeat includes another model
draw as well as corrected guidance, so changes cannot be attributed to guidance
alone.

The follow-up made no recall calls, but the candidate still hit the six-round
limit in **19/20 preparation phases** and nine follow-up phases. It made
235 tool calls with 94 tool errors, versus 141 calls with two errors for the
control. The remaining failures centered on discovery and invalid tool
arguments. The control had four phase limits affecting three cases. These
failures remain in every denominator; no extra rounds or replacement trials
were granted. This design changes discovery and reply formatting together,
so it does not isolate their individual effects.

## Provider usage and latency

All 744 attempted provider requests across the two runs returned accounted usage.
Input includes actual tool schemas, model-authored setup, instructions, and
retained history. Handoff clears prior history equally. Cached input is already
within input; reasoning tokens are already within output.

| Run / arm | Input | Output | Input + output | Cached input | Cache writes | Reasoning | Provider requests | Mean arm wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Initial control | 500,763 | 9,419 | 510,182 | 405,092 | 95,176 | 630 | 165 | 11.14 s |
| Initial candidate | 396,992 | 15,568 | 412,560 | 261,622 | 90,123 | 1,192 | 200 | 14.34 s |
| Follow-up control | 500,205 | 8,967 | 509,172 | 410,290 | 89,399 | 474 | 172 | 11.77 s |
| Follow-up candidate | 393,027 | 15,861 | 408,888 | 262,730 | 81,993 | 683 | 207 | 17.99 s |

Across all 20 measurable pairs, candidate-minus-control input-plus-output
averaged −4,881 tokens initially and −5,014 in the follow-up. Case-paired
bootstrap 95% intervals were [−8,438, −1,129] and [−8,960, −1,195]. These
totals include substantially more incomplete candidate work; **lower token
consumption does not establish equivalent-work savings**.

Only one pair passed in both arms in each run: the initial candidate used
187 more tokens; the follow-up candidate used 11,197 fewer on a correctly
blocked source-change case. One pair cannot support an informative cost
interval. The frozen follow-up report contains a zero-width bootstrap interval
for its singleton sample; it is non-informative and suppressed in the
supplement. The current aggregation helper returns `null`/`insufficient_samples`
for fewer than two pairs. AST comparison verifies that only `summarize` changed
after measurement; archived measured code and raw results are unchanged.

The shared ledger enforced 480 requests, five million input tokens, 768,000
output tokens, and a $10 guard per run, with two concurrent arms. Each phase
allowed six responses of at most 1,600 output tokens. The conservative guard
accounted $0.327 initially and $0.325 afterward; these are guard calculations,
not invoices. The verified Standard prices and cache-write rates are linked in
each protocol. [Official model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna),
[official pricing](https://developers.openai.com/api/docs/pricing).

## Approval enforcement and evidence

A separate configured fixture approver made the predeclared decisions against
actual valid pending gates. The model had no approval tool. Refusal created no
approval; revocation was an actual subsequent revocation event. These external
decisions are not credited as model reasoning.

Independent audits checked all 80 catalogs and 1,586 artifact hashes/sizes.
Every one of the 18 publications had a prior current exact-digest approval:
eight control and two candidate publications initially, then eight control
and none from the candidate. There were no publications in refused, revoked,
or source-changed scenarios. This is observed enforcement in these cases, not
a universal security guarantee. Revocation was tested **before completion**;
post-publication revocation was not tested. Blocked cases verify current
totals and waiting behavior, not the semantics of final SQL that never ran.

The public bundles contain every attempted arm, tool request/reply, provider
usage record, source hash, and both scoring summaries. They exclude credentials,
provider response IDs, encrypted reasoning, and raw workbook files. The source
ZIPs include exact measured Python bytes, license notices, dependency metadata,
hash manifests, and reproduction notes.

| Evidence | Initial | Follow-up |
| --- | --- | --- |
| Original report (gzip; original bytes preserved) | [results](results/2026-11-08-approval-model-initial/results.json.gz) | [results](results/2026-11-08-approval-model-followup/results.json.gz) |
| Exact-numeric supplement | [supplement](results/2026-11-08-approval-model-initial/supplementary.json) | [supplement](results/2026-11-08-approval-model-followup/supplementary.json) |
| Full traces | [JSON.gz](results/2026-11-08-approval-model-initial/traces.json.gz) | [JSON.gz](results/2026-11-08-approval-model-followup/traces.json.gz) |
| Independent evidence audit | [audit](results/2026-11-08-approval-model-initial/audit.json) | [audit](results/2026-11-08-approval-model-followup/audit.json) |
| Measured source | [ZIP](results/2026-11-08-approval-model-initial/measured-source.zip) | [ZIP](results/2026-11-08-approval-model-followup/measured-source.zip) |

The [evaluator](approval_model_eval.py) uses the authorized private key loader;
the [supplementary auditor](approval_model_regrade.py) makes no provider calls
and opens retained catalogs read-only. Runtime metadata was Workspool 0.9.2,
MCP 1.30.0, DuckDB 1.5.6, httpx 0.28.1, and tiktoken 0.14.0. The provider
reported `gpt-6-luna`, an alias rather than a pinned model-weight snapshot.

To reproduce either historical run, use its archived measured source ZIP and
recorded protocol. The current evaluator implements the planned protocol below;
it is not a byte-for-byte reproduction of the earlier two-arm experiment.
The supplementary auditor still accepts the retained historical workbooks and
preserves the original strict scores.

## Planned v0.10.0 follow-up (not yet run)

The follow-up retains the same 20 cases, prompts, task graph, five scenarios,
independent reviewer interventions, six model rounds per phase, 1,600 output-token
limit, and fresh-history handoff rule. It adds a third arm and fixes the exact-numeric
grader before measurement. This section is a protocol, not a new performance result.

| Arm | Model-facing tool interface | Execution and successful replies |
| --- | --- | --- |
| `eager_baseline` | Exact ten eager schemas and instructions captured from the published v0.9.2 wheel | Current runtime; full JSON, text-only |
| `eager_compact` | Current named, typed schemas with shared guidance in instructions | Same current runtime; full JSON, text-only |
| `lazy_typed_errors` | Explicit experimental dispatcher with schema help and typed errors | Same current runtime; full JSON, text-only |

The baseline wheel is pinned to commit
`89528472182ee4f830d99c5cab590855df28d545` and SHA-256
`b4d124650c83d73712c63684892bddaafbfe9224dd18123ecedfc56e3a383c06`.
An offline subprocess loads that exact package and captures its actual `tools/list`
and initialization instructions. The harness records every extracted package-file
hash and the captured interface. It does not rebuild old annotations from current
code. Baseline model calls dispatch to the same current implementation as the other
arms: this freezes the old advertised interface, not the old execution engine.
Current validation and typed errors therefore apply to all three arms.

All replies use the same full JSON/text-only configuration, removing the earlier
adaptive-versus-full formatting confound. Compact eager schemas also move shared
workflow guidance into instructions, so this compares complete interfaces rather
than attributing every difference to schema length alone. The experimental facade
changes invocation and discovery behavior as well. Provider input counts will
include the actual tools, instructions, accumulated conversation and failed calls.
No failures are replaced or given extra rounds.

Every published numeric answer must pass the audited exact-integer rule: canonical
integer previews or integer-string previews, backed by a hash-verified complete
integral numeric Parquet result matching the independent oracle. Floating-point
approximations, booleans, text result columns and incorrect model answers still fail.
The historical strict and supplementary reports are not rewritten.

**Default-profile release rule:** after all 20 cases in all three arms are attempted,
with complete usage and unchanged measured sources, `eager_compact` must complete
at least `eager_baseline - 1` cases. This is a predeclared acceptance threshold,
not a statistical non-inferiority claim. Report both candidate comparisons,
all failures, and costs for pairs where both arms passed. Lower token consumption
on incomplete work cannot establish savings. Experimental lazy results do not
qualify it as a default. The gate remains **unmeasured** until the paid run is
explicitly authorized and completed.

The ceiling is 60 arms / 120 phases / 720 provider requests, 7.5 million input
tokens, 1,152,000 output tokens, and a $10 conservative guard. Concurrency stays at
two; case order rotates arm positions. These limits scale the earlier per-phase
budget without prematurely truncating a third arm. Model alias and prices must
be reverified before an authorized live run; no paid calls were made for this change.

The default invocation is offline. It verifies the wheel, starts disposable local
MCP sessions, records interface schemas and counts, and **does not read an API key**:

```bash
.venv/bin/python benchmarks/approval_model_eval.py \
  --baseline-wheel .local-benchmarks/v092-package/dist/agent_workbook-0.9.2-py3-none-any.whl \
  --output .local-benchmarks/approval-v010-plan --plan-only
```

It reports both encodings using explicit serialization scopes: compact JSON for
`[{name,description,inputSchema},...]`, the actual OpenAI function list (including
`type` and `strict:false`), and instructions counted separately. These exclude MCP
wire envelopes and provider framing; they are not billed usage. Different list or
envelope serialization can legitimately yield a different setup-token number.

The November 8 offline preflight measured these `o200k_base` counts for the
evaluation's exact ten-tool allowlist:

| Interface | MCP definition list | OpenAI function list | Server instructions | Function list + instructions |
| --- | ---: | ---: | ---: | ---: |
| Archived v0.9.2 eager | 1,415 | 1,485 | 155 | 1,640 |
| Current compact eager | 928 | 998 | 239 | 1,237 |

The last column sums the separately counted function list and server instructions:
**403 fewer tokens (24.6%)** for that subtotal. The MCP list is an alternative
serialization, not an additional component. This offline schema measurement
excludes the evaluator's common guidance, prompts, history, replies, and provider
framing. It establishes a smaller advertised interface, not completed-work savings,
model reliability, or satisfaction of the release gate. The preflight writes exact
interfaces and source hashes for independent recounting; no model was called.

After separate authorization, use a new output directory and append `--execute`
instead of `--plan-only`, plus `--env-file .env.local`. Preflight still completes
before any key is loaded. All results and source fingerprints go to that new
directory; existing published evidence is never overwritten.
