# Recurring workflow integration — corrected content metrics

The recorded MCP payloads use the historical lazy interface with scripted, known
arguments. They do not establish model reliability. Current profiles and the current
harness use eager named tools, so a rerun has different setup/request counts; the
published evidence below is unchanged.

On November 8, 2026, the corrected local probe passed **25/25 integration
assertions** in **4.06 seconds**. It used actual MCP subprocesses and explicit
CLI schedule ticks, with synthetic CSVs and a configured local Python worker.
No model, provider API, paid service, or external action was invoked.

- [Results and all assertions](results/2026-11-08-recurring-content-v2/results.json)
- [Actual requests and replies, compressed JSON](results/2026-11-08-recurring-content-v2/traces.json.gz)
- [Independent content/wire recount and source audit](results/2026-11-08-recurring-content-v2/audit.json)
- [Runnable harness](recurring_workflow.py)

## Correction to the earlier report

The previously reported **48.6% formatting reduction is withdrawn**. Its control
duplicated the same data in both MCP response fields. That comparison did not
measure content formatting against a single-representation alternative.

Recounting the original trace gives **20,768 versus 20,768 content tokens** across
30 tool replies, and **10,495 versus 10,495** for its 18 measured writer calls:
zero content reduction. The [old evidence and report remain preserved and marked
superseded](results/2026-11-08-recurring/SUPERSEDED.md). The new run below uses
measurement version 2; it also measures zero content reduction.

## What passed

Two independent SQL steps aggregate spend and its policy limit. A hash-pinned
worker consumes both materialized results. Spend above 100,000 cents selects
the worker branch, which requires an exact-input approval; lower spend selects
the SQL branch. The fixture policy limit is also 100,000 cents. A Python CSV
oracle independently checks exact columns and JSON scalar types, complete
rows, and the skipped branch.

| Source version | Spend, cents | Over budget | Overage, cents | Verified behavior |
| --- | ---: | --- | ---: | --- |
| 1 | 120,000 | true | 20,000 | Wait for approval, resume pinned inputs, then reuse eligible SQL results on a fresh run |
| 2 | 150,000 | true | 50,000 | Source-change tick requires a new approval; approval triggers resume; revocation stops execution until reapproved |
| 3 | 60,000 | false | 0 | Worker branch skipped; SQL branch completes |
| 4 | 180,000 | true | 80,000 | New gate and approved result before export |

Approvals came from a separate configured approver MCP session. This tests
authorization and exact-version enforcement, not the quality of human review.
The unchanged schedule produced no attempt. `verify` identified the old and
new source IDs, the affected published task, and a `task_run` suggestion.

Export/import preserved **61 artifact IDs, versions, and digests**, plus the
measured aggregates. Restore disabled schedules, expired a live claim, and
closed capture sessions. Historical approval remained visible but was no
longer current: the restored task waited for reconfirmation, and a schedule
tick stayed idle. The worker registry was supplied explicitly again; it is
host configuration, not restored execution authority.

## Measurements and limits

The complete trace contains **30 MCP tool calls and 7 CLI commands**. With
the bundled `o200k_base` encoding, emitted tool request/reply objects totaled:

| Serialized payload | Tokens |
| --- | ---: |
| Tool requests | 1,468 |
| Tool replies | 22,089 |
| **Requests + replies** | **23,557** |
| Initialization/list-tools response objects, separately | 1,826 |

These are each payload counted once, excluding JSON-RPC framing, CLI output,
and model conversation replay. They are **not provider usage or billed
savings**. No comparison against a competent SQL script or another workflow
system was measured.

The original writer's `--metrics` snapshot covered **18 calls**, zero errors,
and **1.65 seconds** of measured tool latency. Those replies contained
**10,513 rendered-content tokens versus 10,513 tokens** for the same delivered
data encoded once as compact JSON: **zero content reduction**. Their actual MCP
wire result objects contained **11,166 tokens**, reported separately.

Across all 30 tool replies, rendered content and the single-JSON control both
contained **21,080 tokens**. Every reply in this fixture was plain JSON. The
control counts one encoding of the decoded delivered data; the candidate counts
each delivered text block once. Transport escaping, MCP envelopes, and duplicate
`structuredContent` are excluded from content-formatting comparisons. Source
omissions and provider context/billing are not measured. There is no demonstrated
content-packing gain in this workflow.

Persisted metrics now label `measurement_version: 2`. Legacy v1 token totals are
excluded from corrected sums; excluded calls and comparison samples are explicit.
Their valid historical duration, call, and reuse counts remain. The fresh workbook
in this run contained no legacy samples.

Execution replies recorded 12 step-result reuses, five SQL non-reuses, and
four configured worker executions. Reuse includes eligible SQL cache hits
and explicitly resumed pinned materializations. Reopening a stored report
does not add execution samples. Separately, 11 retained runs referenced nine
unique retained result artifacts; those history observations are not added
to instrumented call totals. Provider usage and monetary cost remained
unknown. This probe did not exercise embedding coverage.

All 32 runtime/harness source fingerprints stayed unchanged during the final
run. The public trace normalizes machine-specific paths after counting;
an independent recount of its MCP objects and single-representation comparison
matched exactly in this run. Normalized CLI paths are outside the token
totals. Python was 3.13.15, MCP 1.30.0, DuckDB 1.5.6, and tiktoken 0.14.0.

## Reproduction

From the repository with the development and tokens dependencies installed:

```bash
.venv/bin/python benchmarks/recurring_workflow.py \
  --output .local-benchmarks/recurring-new
```

The output directory must be absent. The harness creates only disposable
workbooks, a fixed fixture worker/manifest, and an archive there. It applies
a five-minute overall limit and bounded CLI/worker timeouts. This is one
scripted integration run, not a concurrency, large-data, security-boundary,
or autonomous-agent reliability evaluation.

The final fixture uses two cache-eligible scalar SQL steps and valid pagination
limits. Earlier local diagnostics and the first corrected run are retained under
`.local-benchmarks`. The final corrected run followed the instruction-allowlist
fix: tool advice now names only enabled operations after role filtering. Its
recorded source hashes match the independently audited source. The earlier
corrected run and report are preserved in the local evidence directory.
