# Historical report — content-token claim superseded

**The formatting comparison below is invalid and withdrawn.** It counted a
duplicated representation as the control. This report is preserved verbatim
below for audit history; see [the correction](SUPERSEDED.md) and the
[current report](../../RECURRING_WORKFLOW.md).

---

# Recurring workflow integration — v0.9.0

On November 8, 2026, the local v0.9.0 probe passed **25/25 integration
assertions** in **4.07 seconds**. It used actual MCP subprocesses and explicit
CLI schedule ticks, with synthetic CSVs and a configured local Python worker.
No model, provider API, paid service, or external action was invoked.

- [Results and all assertions](results/2026-11-08-recurring/results.json)
- [Actual requests and replies, compressed JSON](results/2026-11-08-recurring/traces.json.gz)
- [Independent recount and artifact audit](results/2026-11-08-recurring/audit.json)
- [Runnable harness](recurring_workflow.py)

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
| Tool requests | 1,467 |
| Tool replies | 21,777 |
| **Requests + replies** | **23,244** |
| Initialization/list-tools response objects, separately | 1,861 |

These are each payload counted once, excluding JSON-RPC framing, CLI output,
and model conversation replay. They are **not provider usage or billed
savings**. No comparison against a competent SQL script or another workflow
system was measured.

The original writer's `--metrics` snapshot covered **18 calls**, zero errors,
and **1.63 seconds** of measured tool latency. For those same delivered
values, full JSON in both MCP response fields counted **21,680 tokens**;
the actual adaptive text responses counted **11,148**. The signed formatting
difference was **10,532 tokens (48.6%)**. This comparison includes the removed
duplicate representation; it does not estimate savings from omitted source
data or claim that every host would forward both fields.

Execution replies recorded 12 step-result reuses, five SQL non-reuses, and
four configured worker executions. Reuse includes eligible SQL cache hits
and explicitly resumed pinned materializations. Reopening a stored report
does not add execution samples. Separately, 11 retained runs referenced nine
unique retained result artifacts; those history observations are not added
to instrumented call totals. Provider usage and monetary cost remained
unknown. This probe did not exercise embedding coverage.

All 32 runtime/harness source fingerprints stayed unchanged during the final
run. The public trace normalizes machine-specific paths after counting;
an independent recount of its MCP objects and same-content comparison also
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

Earlier local harness diagnostics are retained under `.local-benchmarks`:
one used a subquery that correctly bypassed conservative SQL caching and
therefore needed a fresh exact-input gate; another requested `verify` above
its 50-item limit. The final fixture uses two cache-eligible scalar SQL steps
and valid pagination limits. A subsequent successful run was repeated after
the editable package metadata was synchronized to v0.9.0.
