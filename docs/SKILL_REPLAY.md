# Reusing a proven monthly analytical workflow

The recorded MCP payloads use the historical lazy interface with scripted, known
arguments. They do not establish model reliability. Current profiles and the current
harness use eager named tools, so a rerun has different setup/request counts; the
published evidence below is unchanged.

Workspool skills retain a successful task's plan, exact input contracts and
execution provenance. They can define a new task with different source
bindings without resending the SQL. `skill_use` defines that task; `task_run`
executes it. A successful example is evidence that the plan ran on those
inputs, not independent proof that its business interpretation is correct.

The reproducible demonstration in
[skill_replay.py](../benchmarks/skill_replay.py) uses January and February 2025
[NYC yellow-taxi records](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page).
It computes monthly trip/passenger totals, missing-passenger counts, and the
five busiest pickup zones. Every selected result is checked against an
independent streaming Arrow oracle, including nulls and deterministic ordering.
No model or paid API is called.

## Recorded result

The November 7, 2026 run used all **3,475,226 January rows** and **3,577,543
February rows**. Both months retain their exact pickup-zone and passenger-count
integers, including nulls; the benchmark does not exercise monetary columns.
All selected materialized results and exact input lineage passed the
independent oracle in all three arms. The [sanitized evidence](../benchmarks/results/2026-11-07-skill-replay.json)
includes official acquisition URLs/hashes, projection hashes, aggregate answers,
every call's accounting and the exact measured implementation fingerprints.

| February workflow | MCP calls | Request + result tokens |
| --- | ---: | ---: |
| Known working plan: `task_define` + `task_run` | 2 | 1,161 |
| Known skill: `skill_use` + `task_run` | 2 | 1,081 |
| Existing recurring task after logical-source update: `task_run` | 1 | 857 |
| Unchanged task with explicitly retained result | 1 | 237–242 |

Skill transfer saved **80 tokens (6.89%)** against resending this known plan.
It did not reduce calls. The simpler recurring-task control was cheaper than
either transfer workflow. Skills add discoverable, typed reusable plans and
retained proof of their origin; they do not replace ordinary task reuse.

Distillation added 210 request/result tokens. The skill server's facade and
instructions added 68 tokens over the baseline's 197-token initialization.
At the observed 80-token transfer difference, those 278 extra tokens amortize
after four transfers. Finding and inspecting the skill cost another 1,398
tokens: including that one-time discovery raises the projected break-even to
21 transfers. A one-off cold discovery therefore cost more than simply using
an already known plan. Discovery was tested over one saved skill, not a large
library.

February execution times were 307 ms for the known plan, 312 ms for skill
transfer, and 305 ms for the recurring task. These are one local scripted trial
during other verification activity, not statistical speed estimates. Random
artifact IDs also change tokenization slightly between runs. Counts are local
MCP serialization, not provider usage or billing.

## What the comparison measures

Both arms receive identical Parquet bytes containing all rows and a lossless
integer projection of pickup zone and passenger count. They use separate
workbooks, execute the same January plan, and pin the same February source
semantics. Raw source imports and projection are common preparation, outside
the measured plan-reuse calls.

The skill arm distills the complete January run, discovers and inspects the
skill, then calls `skill_use` and `task_run` for February. Its strong control
already knows the working plan and calls `task_define(pin_sources=True)` and
`task_run`. This is not a comparison against putting millions of source rows
into a prompt.

Distillation, discovery, server initialization and transfer are counted
separately. A stronger recurring-work control updates a rolling logical source
from January to February and calls its existing task once, without redefining
the plan. Each arm also measures an unchanged rerun with an explicitly retained
base. These already avoid resending SQL; skills should not be credited with a
benefit that ordinary durable tasks already provide. Counts use actual MCP
request/result serialization with `o200k_base`, the `agent` profile and known
operation schemas. They exclude provider framing, model decisions, and
automatic continuity capture. A cold host that needs operation schema help
would incur additional discovery calls.

## Correctness boundaries

The three result sets have one, one and five rows respectively. The top-five
limit is part of the SQL, so every selected row fits the delivered result.
Initial distillation requires a current published complete run with no missing
work or evidence. Notices that rows are retained in a complete materialization
are allowed after bounded integrity checks; a missing result or missing
evidence is not. This particular workload has no omitted-content notices.

Replay checks exact ordered source column names/types and media type before
defining the task, then executes those same immutable validated inputs. A
changed source binding produces a new run. It does not reuse the old answer or
use `resume_from`, which deliberately pins an earlier attempt's inputs.
The benchmark reads every small materialized output and verifies its exact
dependencies, rather than trusting the model-visible result alone.

The January oracle counted 540,149 trips with missing passenger counts; February
had 806,937. The saved skill transferred the computation, not January's answer.
An early pilot caught a missing `ORDER BY` on the derived coverage step: the
conservative cache recomputed it and correctly withheld a retained-result
receipt. The final plan declares its order explicitly, with a regression that
checks unchanged reuse. That pilot was preserved rather than counted as a pass.

Historical skill provenance is retained separately from computational input
dependencies. A regression changes the original January input after
distillation, then replays on fresh February data: the skill reports
`origin_stale=true`, while the new definition, run and result remain current.
That distinction prevents an old example from incorrectly invalidating a new
computation.

## Reproduce

First acquire the public files using the
[public-data instructions](../benchmarks/PUBLIC_DATA.md#reproduce). No new
download is needed when those local inputs already exist. Use the locked
`benchmark` and `tokens` extras and populate the tokenizer cache before an
offline run:

```sh
uv sync --extra benchmark --extra tokens
uv run python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
uv run python benchmarks/skill_replay.py --source-root .local-benchmarks/public-replay --output .local-benchmarks/skill-replay
```

Use a fresh output directory. The harness checks the downloaded files against
their acquisition manifest, saves actual MCP traces locally, checks source
fingerprints before/after execution, and refuses to count an errored tool call
as a successful comparison. The yearly/monthly public files may be updated;
the recorded hashes identify the exact inputs used.
