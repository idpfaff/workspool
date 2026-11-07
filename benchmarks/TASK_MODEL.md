# Actual-agent task evaluation

On November 7, 2026, `gpt-6-luna` completed all four workflow phases with both
the SQL baseline and the added task tools. Task tools reduced provider input
plus output after a context reset by **32.6%** and after source changes by
**38.5%**, but their larger setup cost left the complete workflow **2.4% more
expensive in token count**: 33,814 versus 33,012. The candidate made 12 provider
requests versus 16, a 25% reduction. These are observed requests and token
counts, not dollar costs or a general savings guarantee.

An earlier four-round diagnostic completed only three of eight answers. Both
reports are published; the successful follow-up changed the product and the
round limit, so it is not an identical repeat or an independent reliability
sample.

- [Follow-up report, including every tool call and provider usage record](results/2026-11-07-task-model-followup.json)
- [Initial diagnostic, including every failure](results/2026-11-07-task-model-initial.json)
- [Evaluation harness](task_model_eval.py)

## Follow-up results

The following totals come from actual provider usage, including tool schemas,
instructions, model-authored setup, and retained conversation on every request.
Input plus output is an unweighted token sum; cached input is already part of
input and is not added again. Each phase depends on the preceding workbook
state. These are eight answers in two four-phase workflows, not eight
independent tasks.

| Phase | Baseline input | Task input | Baseline output | Task output | Baseline input + output | Task input + output | Task change | Requests, baseline / task |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Cold setup | 3,401 | 11,077 | 318 | 633 | 3,719 | 11,710 | +214.9% | 3 / 5 |
| Warm, history retained | 6,651 | 7,615 | 223 | 130 | 6,874 | 7,745 | +12.7% | 3 / 2 |
| Fresh context, workbook retained | 9,439 | 6,443 | 401 | 186 | 9,840 | 6,629 | −32.6% | 6 / 3 |
| All sources changed | 12,239 | 7,661 | 340 | 69 | 12,579 | 7,730 | −38.5% | 4 / 2 |
| **Total** | **31,730** | **32,796** | **1,282** | **1,018** | **33,012** | **33,814** | **+2.4%** | **16 / 12** |

Both arms answered **4/4 correctly**. On input alone, the candidate used 3.36%
more overall; reset and changed phases used 31.74% and 37.41% less. The reset
saved 3,211 input-plus-output tokens and the changed phase saved 4,849. A
one-call warm receipt still cost more input than the baseline's three tool
calls because the candidate retained a larger preceding conversation.

| Provider usage detail | Baseline | Task candidate |
| --- | ---: | ---: |
| Cached input tokens, included in input | 23,696 | 25,775 |
| Reported cache-write tokens | 6,287 | 6,985 |
| Reasoning tokens, included in output | 18 | 186 |
| Tool calls | 14 | 10 |

Caching was observed, not controlled. Cache-write fields remain `null` when a
provider does not report them; this run reported them. No price conversion or
cache-adjusted cost claim is made.

## What the model actually used

The candidate called `task_define` once, `task_run` twice, and `task_status`
twice. It never called `context`; adoption here was durable tasks, not merely
indexed retrieval. The report's aggregate `task_tool_calls` field covers all
four added tools, so the per-tool `tool_selection` records establish this
distinction.

During cold setup, the model created one task with the two tables and the
policy document as sources. It chose a **single combined SQL step** containing
independent aggregates and dependent subtraction. This run therefore does not
demonstrate autonomous planning of a multi-node task graph.

During the warm phase, `task_status(base_run=...)` returned an unchanged
receipt. The harness independently checked that the complete corresponding
brief was still retained. After resetting context, the model requested a full
status brief without a retained base and also reread source excerpts. After
source changes, it called `task_run(force=true)` and returned the correct new
answer. That forced execution demonstrates recomputation against current
sources; it does **not** demonstrate a query-cache hit or a SQL execution-speed
advantage.

The baseline could save SQL recipes/results, inspect freshness in batches,
refresh saved work, and use one combined SQL query. It did save a combined
query. Its reset path nevertheless reran computation instead of making the
best possible use of that saved result. The comparison measures the model's
chosen paths with useful tools available, not an optimal hand-written SQL
script. Neither arm had arbitrary shell or code-execution access.

## Initial diagnostic and subsequent changes

The initial run allowed four provider rounds per phase. All five failures
were `step_limit`; they are retained with their consumed tokens and completed
tool work. `complete: true` in the JSON means all trials were attempted, not
that all answers were correct.

| Phase | Arm | Outcome | Input tokens | Output tokens | Requests |
| --- | --- | --- | ---: | ---: | ---: |
| Cold | Baseline | Step limit | 5,407 | 512 | 4 |
| Cold | Task candidate | Step limit | 7,638 | 545 | 4 |
| Warm | Baseline | Correct | 8,995 | 240 | 3 |
| Warm | Task candidate | Correct | 10,902 | 235 | 3 |
| Reset | Baseline | Step limit | 6,341 | 598 | 4 |
| Reset | Task candidate | Step limit | 8,269 | 124 | 4 |
| Changed | Baseline | Correct | 14,621 | 420 | 4 |
| Changed | Task candidate | Step limit | 14,463 | 75 | 4 |

The baseline scored 2/4 and the candidate 1/4. Baseline usage totaled 35,364
input and 1,770 output tokens; candidate usage totaled 41,272 input and 979
output. Across both arms, reported cached input was 58,516, cache writes
15,359, and reasoning 306 tokens. All 30 requests had usage records.

Both cold arms spent extra rounds fetching metadata and then content. The
candidate defined a task with immutable table IDs and omitted the text policy
from its sources. Its later task evidence was incomplete, and its immutable
bindings continued to select old inputs after updates. The baseline also
encountered an existing-result version conflict during reset. These were
useful failures, not discarded outliers.

The follow-up made four changes:

1. Definitions resolve supplied source names or IDs to logical names by
   default. Fresh runs pin the current versions together. Explicit
   `pin_sources=true` retains immutable historical bindings.
2. A definition with evidence searches must list at least one text source;
   otherwise it receives an actionable error before saving.
3. Inspection guidance states that `full=true` adds metadata only, explains
   `excerpt_bytes`, and encourages batching related refs. Both arms receive
   this guidance.
4. The per-phase limit increased from four to six provider rounds. The total
   request ceiling stayed at 36; output remained capped at 1,600 tokens per
   request. The follow-up used 28 requests.

The improvement from 3/8 to 8/8 cannot be attributed to one change. Model
variation, changed guidance, product fixes, and the larger round allowance
all differ. A global cap exhausted before later phases would leave those
phases as failures, not silently exclude them.

## Method and limits

The workload uses small synthetic stock-like tables, with integer cents, and
a text policy. It asks for exact trade notional, allocation, dependent
headroom, the current policy code, and a threshold decision. Initial values
are 910, 1,500, and 590 cents with code `CEDAR` and review required; changed
values are 1,710, 2,400, and 690 cents with code `MAPLE` and no review. Python
integer arithmetic supplies the oracle, and successful answers must match
both JSON values and types.

Arms start from equivalent workbook clones and receive identical phase
prompts. The baseline exposes `query`, `refresh`, `save`, `find`, `inspect`,
and `search`; the candidate adds the four tools above. Both use eager tool
schemas and adaptive text-only MCP replies. The model authors its reusable
work: task definitions and their replies are counted, not preinstalled for
free. Identical fixture creation/import and MCP startup are excluded equally.

Warm and changed phases retain actual Responses history. Reset removes all
previous messages and retained receipt bases while preserving workbook files.
Each phase reconnects MCP. Source hashes verify unchanged fixtures within a
phase and frozen measured code within a run. Publication checks reconciled
request, phase, arm, and total usage; paired prompts; answer oracles; and
receipt-base requirements. Both reports contain the exact runtime source
hashes and dependency versions. Their publication metadata records the
original report hash and sanitization checks. No credential or local-path
redactions were needed; provider response IDs and encrypted reasoning were
already excluded by the harness.

This is one small workflow with one model alias, `gpt-6-luna`, not a pinned
model snapshot or statistical reliability study. It does not establish gains
on large data, arbitrary tasks, all hosts, or a capable agent with a bespoke
script. Provider input includes repeated retained context, so fewer tool
calls need not mean fewer input tokens. The result supports narrower claims:
the model adopted durable tasks, recovered a complete brief after reset,
and recomputed current results with fewer requests; total workflow tokens
still increased.

## Reproduction

With the project's benchmark dependencies and an authorized local API key,
run from the repository root:

```bash
.venv/bin/python benchmarks/task_model_eval.py \
  --env-file .env.local \
  --max-steps 6 \
  --request-budget 36 \
  --output .local-benchmarks/task-model-reproduction
```

This makes real provider requests. The harness uses the fixed Responses API
endpoint and never publishes the key. `--prior-report` can link a previous
local report by hash; the published follow-up links the preserved initial
diagnostic. Repeating the command on current code will not reproduce the
initial product behavior. Each report records its own source hashes, request
limits, prompts, tool schemas, calls, answers, and usage for inspection.
