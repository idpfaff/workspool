# Reusable task skills

A Workspool skill stores an exact task plan extracted from a successful local run. It lets an agent find that plan and apply it to explicitly selected, compatible data without reconstructing the SQL. Applying a skill creates a task definition. `task_run` remains a separate call.

This is deterministic extraction, without an LLM. `proof: "successful_execution"` means the originating task finished and published successfully with recorded provenance. It does not mean the SQL is analytically correct, the data is trustworthy, or the plan generalizes to every compatible table. Review the plan with `skill_inspect` when those judgments matter.

Distillation currently accepts SQL-only plans. Tasks with conditions, approval gates, or configured workers are rejected explicitly until their additional replay proof is supported. A [workbook export](RECURRING_WORK.md#share-a-workbook-and-restrict-agent-actions) transfers existing skill packages together with their authoritative task and audit history; copying a package's JSON alone does not.

## Example

After an existing task called `spending-review` completes successfully:

```json
{"name":"spending-review","task":"spending-review","description":"Summarize spending by category with the recorded SQL."}
```

Send that object to `skill_distill`. The resulting artifact is named `_skills/spending-review`. Its compact receipt includes its immutable `artifact_id`, version, description, source aliases, step names, and origin run ID. The SQL is available through `skill_inspect`, rather than repeated in the receipt.

Discover and inspect:

```json
{"query":"spending","limit":8}
```

```json
{"ref":"spending-review"}
```

These are arguments for `skill_find` and `skill_inspect`, respectively. If inspection shows a source alias `transactions`, apply the plan to a new imported table:

```json
{"ref":"spending-review","task_name":"november-spending","bindings":{"transactions":"transactions/november"}}
```

Send that object to `skill_use`, then run the new task separately:

```json
{"name":"november-spending"}
```

The final object is for `task_run`. Lazy discovery profiles wrap these in `{"tool":"skill_use","arguments":{...}}` or the corresponding operation name. Names, `_skills/` names, and immutable skill artifact IDs all work as skill references. Use the immutable ID when a particular skill version is required.

## Eligibility and provenance

Distillation reads the authoritative task ledger and its immutable definition, not a truncated response, unchanged receipt, or computation snapshot alone. The selected run must be finalized, complete, published as current, and still the task's latest successful run with current source versions when the skill is published. Failed, partial, superseded, claim-lost, or incomplete-evidence runs are rejected.

The sole allowed omission is `rows_retained_in_artifact`: the complete step result must be retained in its recorded Parquet artifact, with matching lineage and verified content hash. The package preserves that notice. Missing evidence, missing steps, and response-budget omissions recorded in the ledger are not treated as successful extraction. A delivery-only shortened response does not replace the full ledger used for eligibility.

Packages preserve:

- The exact objective, SQL, ordered steps and input aliases, scalar parameter values, evidence searches, and CSV type overrides.
- Each source's media type and, for tables, exact ordered column names and DuckDB types.
- The origin task/run, definition, source, result, anchor, and computation artifact IDs and hashes; publication event ID; execution producer; and distilling identity.

Packages contain no source rows, query previews, or citation bodies. SQL literals and original parameter values are preserved intentionally: inspect the plan before sharing it. This release stores skills inside the local workbook; copying the JSON into another workbook does not transfer its authoritative execution proof.

An internal proof registry and the append-only publication event bind the package to that execution. Arbitrary artifacts labeled `kind: "skill"` do not qualify. Loading a skill verifies its package hash, full definition, retained computation snapshot and anchor, ledger/publication agreement, and artifact lineage. Source schema checks assume Workspool's immutable, content-addressed blobs: they check regular-file identity/size and do not rehash every source on each inspection or application. A subsequent task execution performs the existing cache/input integrity checks. This is local integrity checking, not a security boundary against the owner of the workbook files or database.

The skill retains its origin computation artifact, which transitively retains the definition, sources, and results. A task created from a skill retains the exact skill version through an explicit `provenance` link and its pinned bindings through computational dependencies. Provenance retention protects history from deletion without making old origin data a computational input to the new task. Old skill versions remain addressable by ID. Changing the original sources or publishing a newer origin run sets `origin_stale: true`; an already proven historical skill remains reusable. Its provenance is historical, while a new run determines the freshness and correctness of its newly bound computation.

## Applying safely

`bindings` must explicitly contain exactly every original source alias. A binding can be an artifact name or immutable ID. Workspool resolves all bindings once, validates those versions, then defines the new task with those same immutable IDs and `source_mode: "pinned"`. A concurrent source-name update cannot silently substitute a different version after validation. Reapply the skill explicitly to adopt newer inputs; `resume_from` continues to mean the existing task run's pinned snapshot.

Table compatibility is deliberately strict: media type, all column names, order, and types must match. Extra columns are rejected, since they can change `SELECT *` and positional SQL semantics. No new coercion is introduced. Original `csv_types` overrides remain part of the plan and are used for schema validation and execution. Non-table sources require matching media type; a text document's substantive relevance still needs review.

Optional `parameters` maps existing step names to replacement scalar arrays. Each override must preserve that step's parameter count and satisfy normal task validation. SQL, step order, source aliases, and evidence searches remain unchanged. For example, a step originally containing two parameters accepts `{"parameters":{"filtered_total":[2026,100]}}`, but not a different arity or unknown step name.

`skill_distill` and `skill_use` require writer permission. `skill_find` and `skill_inspect` permit readers. Producer and author identities come from the trusted server/session principal, not caller-supplied tool arguments. Existing task execution permissions, claims, budgets, cache validation, and publication checks apply when the new task is run.

## Versioning, limits, and failure recovery

`expected_version` on `skill_distill` is compare-and-swap for the skill name; on `skill_use` it applies to the target task definition. Omit it to create a new name, and pass the current version to update. Competing updates cannot silently overwrite each other.

A package is limited to 64 KiB, a source table to 256 columns with bounded names, and discovery to 20 summaries per call. Discovery performs a literal, case-insensitive name/description search over current registered skill versions. Its `complete` flag indicates whether the result limit truncated matching candidates; `invalid` counts candidates rejected during verification. Full SQL and package contents require explicit inspection.

Result integrity checks stream at most 64 MiB per materialized output, with a 15-second verification deadline. Schema discovery uses restricted local DuckDB access, configured memory/threads, and a query interrupt deadline capped at 30 seconds. It does not return sample rows. These are bounded schema checks, not a separate process RSS limit.

Saving the immutable artifact and publishing its proof are separate transactions. A crash or failed proof event can leave an unregistered artifact; it is excluded from discovery and rejected by inspection/application. Repeating the same distillation with the same actor, description, origin, and expected version repairs an otherwise matching pending artifact. If the origin has changed or a different version has won, the operation fails clearly and requires an explicit current-version decision. Artifact creation, proof publication, and task definition retention are separately audited; no unregistered package is accepted as proven.
