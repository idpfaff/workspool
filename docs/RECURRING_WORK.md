# Recurring work, review, and shared workbooks

A recurring analysis can now combine SQL with an existing local Python program
or tool adapter, branch on a calculated value, and pause for approval. Its run
keeps the exact input versions, worker identity, outputs, and review references.
Input changes can trigger another run through an explicit local runner.

All profiles expose named, typed MCP tools by default. Call `task_define`,
`task_run`, and `task_status` directly with their advertised argument objects;
there is no discovery dispatcher in the default path. The old `--tool-discovery`
flag is removed. `--experimental-lazy` is a warned, explicitly opted-in experiment,
not a profile default. Historical scripted dispatcher measurements do not establish
model reliability. The [planned three-arm follow-up](../benchmarks/APPROVAL_MODEL.md#planned-v0100-follow-up-not-yet-run)
requires the eager default to complete at least the control's cases minus one.

## Conditional steps and approval

Existing SQL definitions continue to work. Add `when` to select a branch and
`approval: true` to pause before executing that step:

```json
{
  "objective": "Review spending that exceeds the approval threshold",
  "sources": {"expenses": "expenses"},
  "steps": [
    {
      "name": "total",
      "inputs": ["expenses"],
      "sql": "SELECT sum(amount)::DECIMAL(18,2) AS amount FROM input1"
    },
    {
      "name": "reviewed",
      "inputs": ["total"],
      "sql": "SELECT * FROM input1",
      "when": {"step": "total", "column": "amount", "op": "gt", "value": 200},
      "approval": true
    }
  ]
}
```

Save this as `review.json`, then define and run it:

```sh
workspool --workspace "$HOME/Workspool" task-define spending review.json
workspool --workspace "$HOME/Workspool" task-run spending
```

Conditions read exactly one row and the named column from an immutable input.
Operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `is_null`, and `not_null`.
Null predicates omit `value`. Types must match; arbitrary expressions and code
are not evaluated. A false branch and its dependent steps are explicitly skipped.
Failed or ambiguous conditions remain failures, not silent skips.

A gated run returns `state: "awaiting_approval"` and a step `approval_ref`.
Inspect that artifact to review the exact plan, sources, upstream results, and
worker identity. A separately configured approver can then attest to it:

```sh
workspool --workspace "$HOME/Workspool" inspect wb_GATE_REFERENCE
workspool --workspace "$HOME/Workspool" approve wb_GATE_REFERENCE \
  --agent-id reviewer --note 'Reviewed the inputs and proposed step'
workspool --workspace "$HOME/Workspool" task-run spending --resume-from run_RUN_ID
```

MCP hosts use `approve` with the `approver` role. Writer agents cannot grant
their own approval. Identity and capability grants come from the server's launch
configuration. An approver attestation records who approved; it does not prove
that a human reviewed the evidence or that the underlying claim is true.

Approval binds exact immutable inputs and the configured worker version.
Changed inputs, plans, or workers require a fresh run and approval. Explicit
resume reuses validated completed upstream materializations so a volatile
worker cannot change the snapshot being reviewed; `force` deliberately
recomputes. To revoke an approval:

```sh
workspool --workspace "$HOME/Workspool" approve wb_GATE_REFERENCE \
  --agent-id reviewer --decision revoke --note 'Requires another review'
```

Approval is rechecked before gated steps and at final publication. Revocation
does not undo work already executed or stop a program already running. Status
also rechecks completed runs: a revoked gate reports `awaiting_approval` with
`current: false`, while the earlier execution remains in the audit history.

## Register an existing Python program or tool adapter

A task selects a **worker name**, never a command or inline program:

```json
{
  "name": "validated",
  "kind": "worker",
  "worker": "invoice_check",
  "inputs": ["expenses"],
  "parameters": [200]
}
```

The operator supplies a local JSON manifest through `--worker-registry` on
`serve`, `task-run`, or the schedule runner. A manifest pins the executable,
script, and declared dependency files by SHA-256:

```json
{
  "format": 1,
  "workers": {
    "invoice_check": {
      "command": ["/absolute/venv/bin/python", "/absolute/invoice_check.py"],
      "files": {
        "/absolute/python-executable-target": "EXECUTABLE_SHA256",
        "/absolute/invoice_check.py": "SCRIPT_SHA256"
      },
      "timeout_seconds": 30,
      "max_input_bytes": 268435456,
      "max_output_bytes": 16777216,
      "max_rows": 10000
    }
  }
}
```

Use the real SHA-256 values of the files, resolving the interpreter's symlink
for its hash entry while retaining the virtual-environment launcher in
`command`. Fixed file arguments must also be pinned. Changes fail closed until
the operator updates and reloads the manifest.

The program receives one JSON object on stdin: `format: 1`, scalar `parameters`,
`inputs` with immutable IDs, SHA-256 values, media types, and paths to read-only
scratch copies, an `output_directory`, and a stable `idempotency_key`. Emit one
JSON object on stdout:

```json
{"format":1,"columns":["total","needs_review"],"types":["DECIMAL(18,2)","BOOLEAN"],"rows":[["300.00",true]]}
```

Alternatively emit `{"format":1,"parquet":"result.parquet"}` for a bounded
regular file inside the supplied scratch directory. Logs belong on stderr.
Typed results are checked and retained as Parquet with exact input lineage.
New runs execute workers again; SQL cache reuse and pinned resume reuse are
reported separately from a new worker execution.

Workers run with the local account's permissions. This is a registry for
trusted programs, not a sandbox for agent-generated code. Use adapters whose
permissions fit the job and whose external effects tolerate the supplied
idempotency key. A crash can require retrying execution; publication fencing
does not make external services transactional.

Execution and result materialization have separate wall-clock deadlines. On
macOS and Linux, output limits apply per file during writes; they are not an
aggregate disk or memory quota. The watchdog stops ordinary worker process groups
when their server exits, but deliberately detached programs are outside that
guarantee.

## Run on a schedule or when inputs change

```sh
workspool --workspace "$HOME/Workspool" schedule-set spending-daily \
  --task spending --interval-seconds 86400 --on-change --agent-id analyst
workspool --workspace "$HOME/Workspool" schedule-run \
  --agent-id analyst --worker-registry /absolute/workers.json
```

The explicit local runner polls for due intervals, changed task/source versions,
and decisions on pending approval gates. MCP `task_schedule` configures or lists
triggers; it does not start a background process. `schedule-tick` performs one
bounded page and can be invoked by an existing service manager. Follow its
`next_after` cursor until `complete` to check every configured schedule.

Missed intervals coalesce into one attempt. Unchanged input-only triggers do
not rerun. Pending approvals wait for a decision or changed inputs. Claim fences
and atomic trigger consumption prevent competing live runners from publishing
the same scheduled attempt; the runner renews its claim during longer work.
After a crash, execution may retry. The configured runner identity must match
the schedule owner. `schedule-list` shows each schedule's latest attempt and state.

Updates require the returned version, including disabling future execution:

```sh
workspool --workspace "$HOME/Workspool" schedule-set spending-daily \
  --task spending --interval-seconds 86400 --on-change --disabled \
  --expected-version 1 --agent-id analyst
```

An active attempt must finish before its schedule can be edited. Stopping the
runner stops future polling; the schedule stays durable for an explicit restart.

## Share a workbook and restrict agent actions

Each server has one configured principal. `--capability` narrows its role to
specific actions. For example, a worker can read and run existing tasks without
defining tasks or directly editing artifacts:

```sh
workspool --workspace "$HOME/SharedWorkbook" serve --profile runtime \
  --agent-id analyst --role writer --worker-registry /absolute/workers.json \
  --capability read --capability task:run --capability workers --capability claims
```

Available grants are `read`, `artifact:write`, `artifact:delete`, `task:define`,
`task:run`, `skill:write`, `claims`, `remember`, `approve`, `workers`, `schedule`,
`export`, and `import`. Omission preserves the role's existing grants. Grants
cannot expand a role; an approver still cannot run a writer task. Both direct
and experimental lazy MCP dispatch enforce the restrictions, including materializing queries.

These are server request permissions. They do not isolate principals with local
filesystem access, authenticate multiple users behind one bearer token, or
provide separate private namespaces. Use separate workbook directories across
trust boundaries. Claims, approvals, task publication, and schedule attempts
retain configured actor attribution.

Export a coherent snapshot and restore it into a **new** destination:

```sh
workspool --workspace "$HOME/SharedWorkbook" workbook-export review.workspool.zip
workspool workbook-import review.workspool.zip "$HOME/RestoredReview"
```

The archive preserves artifact IDs, versions, referenced bytes, task/skill proof,
and audit history. Hashes, schema, foreign keys, paths, and declared size limits
are checked. Existing destinations are never overwritten. Derived caches,
worker launch manifests, environment files, and unused blobs are excluded;
content deliberately saved as an artifact remains part of the export.

Restore expires live claims, closes old sessions, marks interrupted task attempts,
and disables schedules. Historical approvals stay inspectable; fresh approval
is required to authorize restored gates. Configure workers and re-enable
schedules explicitly on the destination machine.

## Check freshness and delivered value

Use MCP `verify` or `workspool verify` to see exact changed source versions,
affected published tasks, current approval status, and suggested reruns. Follow
its cursor for bounded pages. A relevant mutation invalidates the cursor so
pages cannot silently combine different snapshots. `changes` remains the audit
event feed.

Enable `serve --metrics` to record bounded daily aggregates. `metrics` reports
observed duration, failures, step-result reuse, and recall/embedding coverage,
plus available task and session history. Histories and sampled coverage are
labeled; missing data is not treated as zero usage.

With `--tokenizer`, `measurement_version: 2` compares one compact JSON encoding
of the delivered data (`json_content_tokens`) with one rendered representation
(`content_tokens`, summed across text blocks). Plain JSON and structured-only
replies have zero `content_tokens_reduced`; reversible text/metadata formatting
may produce a positive or negative difference. MCP envelopes, transport escaping,
duplicate `structuredContent`, omitted source text, host prompts, and provider
framing are excluded from this comparison. Actual serialized result counts remain
separate as `wire_tokens`; they are not content-formatting savings.

Legacy unversioned/v1 token comparisons used a duplicated representation as their
control. Their token totals are excluded from corrected sums, with
`legacy_token_calls_excluded` and `legacy_formatting_samples_excluded` reporting
the missing comparable history. Historical call, duration, and reuse counters
remain. Host-reported usage stays separate; these counts do not claim lower
provider bills. Aggregates retain 90 UTC days with bounded groups, not an
unbounded per-call ledger.

Existing skill distillation continues to support completed SQL-only plans.
Conditional, gated, and worker plans require additional replay proof and are
explicitly rejected by distillation in this increment.
