# Capture-on recall regression

The v0.8.0 runtime indexed its own session episode JSON as ordinary evidence. In this reproduction, repeated goals and accounting fields displaced the invoice source from an explicit lexical recall response. The corrected runtime excludes episode ledgers before lexical and dense retrieval, and indexes a separate, published projection of substantive observations.

This is a controlled regression probe with synthetic hosting invoices, not a broad retrieval-quality or token-savings benchmark. It uses real MCP stdio servers, automatic capture enabled, and a genuinely fresh second process. No provider/model API was called.

## Reproduction

The configured goal is `review hosting spend`. A producer performs three calls: save an action note, query two invoice rows, and inspect the invoice note. The independently known query answer is **1500 USD** (1200 + 300). A second process receives startup context before any agent tool call, then requests recall with `limit: 6`.

Both startup context and the complete formatted MCP recall result have an **o200k_base budget of 2000 tokens**. Startup counts include the restoration prefix; recall counts include formatted content/structured content. Ordinary server instructions and tool schemas are outside this per-reply budget. UUIDs affect exact counts across repetitions.

The baseline is archived tag `v0.8.0`, commit `6ee0f3ba0805b46f39b9e56257c97f0f09ce7fe6`, using the same installed dependencies. The hybrid arm uses the already provisioned local BGE-small-en-v1.5 model. Its runtime embedding worker blocks network sockets; local model file hashes were unchanged.

## Observed results

| Retrieval / response | Episode ledger hits before → after | Invoice source rank before → after | Payload tokens before → after |
|---|---:|---:|---:|
| Lexical startup | 4 → 0 | 5 → 2 | 1940 → 535 |
| Lexical explicit recall | 3 → 0 | Absent → 2 | 1565 → 562 |
| Hybrid startup | 3 → 0 | 2 → 2 | 1707 → 1053 |
| Hybrid explicit recall | 3 → 0 | 2 → 2 | 1994 → 1089 |

All corrected responses retained the observed `hosting_spend_usd = 1500` result and the original invoice note. The projection ranked first; the original note ranked second. Hybrid retrieval also returned the small invoice table and action note. No corrected hit contained an episode ledger, repeated session goal, or `accounting_note`. Every returned byte citation was checked against its immutable artifact's bytes. The observation is labeled as executed tool output, **not independently validated factual truth**.

The probe also copies each genuinely pre-indexed baseline workbook and opens it with the corrected runtime. The invoice source becomes rank 1 in both modes, with zero ledger hits and zero legacy episode chunks remaining; the hybrid cache also has zero legacy episode vectors remaining. These upgrade startup payloads used 275 lexical / 770 hybrid tokens. Old episodes are not retroactively converted into observations: they remain explicit audit records, while new captures create the compact projections.

## Scope of the fix

- `memory_episode` is excluded from default and explicit-reference recall scopes, before either ranker runs.
- Existing excluded chunks and orphaned vectors are reclaimed incrementally. Cleanup visits at most 256 cached versions and removes at most 4096 unreferenced vectors per call. Vectors whose exact content hash remains backed by a real document are retained.
- Only bounded query/task result cells and explicit decisions produce `memory_fact` projections. Goals, accounting, timing, requests, metadata-only saves/reads, recall, help, and status replies are not projected. Repeated observations with the same source/result versions deduplicate across sessions.
- A projection is recallable only after an episode owner is published atomically in the continuity ledger. Failed publication leaves an unsearchable orphan; exact retry can repair it. Concurrent cleanup is handled by bounded creation/publication retries, including deletion between a duplicate-name conflict and the matching fact lookup. Conflicting content and other validation errors still fail. Retention preserves owner proof for externally referenced facts.
- Source IDs are computational dependencies of observations, so later source changes set `stale_inputs`. External decisions remain explicitly unverified. No confidence score is inferred from capture.

The tests include default/explicit scopes, seeded legacy lexical/vector caches, shared vector hashes, source freshness, read/status suppression, exact retry after audit failure, and unchanged task-result deduplication. The probe does not establish general semantic superiority, model billing savings, or automatic factual correctness.

## Run and evidence

```sh
uv run --extra tokens --extra semantic python benchmarks/capture_on_recall.py \
  --output .local-benchmarks/capture-on-recall-new \
  --model-dir .local-benchmarks/models/bge-small-en-v1.5
```

Use a fresh output directory. The model must already be provisioned locally; omit `--model-dir` for the lexical-only comparison. The script archives the baseline locally and writes full request/result traces, exact-citation checks, source fingerprints, model hashes, and aggregate results. It does not acquire datasets or provision a model.

Published aggregate evidence: [2026-11-07-capture-on-recall.json](results/2026-11-07-capture-on-recall.json). It contains no private paths, raw source passages, or credentials. The measured local trace set is `.local-benchmarks/capture-on-recall-startup-order-final/`, rerun after the retention race and startup cleanup ordering fixes with matching final runtime fingerprints. This is a local MCP runtime probe; the separate real-host check establishes delivery through the generated client configuration.
