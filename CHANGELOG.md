# Changelog

## 0.7.0 — 2026-11-07

- Add an opt-in runtime profile that automatically captures bounded tool observations, task results, failures, and source references. Restore relevant context in MCP initialization instructions for a configured goal and exact tokenizer budget; expose stdin hooks for other host integrations.
- Add optional local BGE embeddings with hybrid lexical/vector retrieval, exact citations, persistent content/model caches, explicit coverage, and truthful lexical fallback. Model provisioning is explicit; inference workers use local assets and disable socket connections.
- Add configured reader, writer, and approver roles; atomic expiring claims with monotonic fences; persisted claim policy; and ownership validation in the same transaction as task publication. Superseded owners cannot advance the successful-run pointer.
- Commit artifact producer events atomically with saved artifacts, and expose immutable event pagination, approval attestations, and transitive stale-input explanations. Local filesystem owners remain trusted; this is not federated authentication.
- Preserve execution observations separately from unverified host reports. Add redaction, bounded previews, retry-safe capture, repeated-read deduplication, and incremental session accounting with reported usage distinct from rendered-token estimates.
- Stress public NYC taxi, NOAA weather, and Wikipedia datasets in addition to Massive. Publish exact checks, comparative overhead, failed limits, and small real-model semantic retrieval diagnostics without raw data or model weights.
- Preserve the compatible, efficient, and agent profiles; semantic dependencies remain optional. Keep the MIT License with Commons Clause.

## 0.6.0 — 2026-11-07

- Add durable tasks: versioned definitions, dependent SQL steps, a coherent snapshot of all inputs, reusable materialized results, explicit partial recovery, and generation-checked publication across concurrent agents.
- Add `task_define`, `task_run`, `task_status`, and ranked `context` retrieval through a new `agent` profile. Preserve the compatible and efficient profiles. Fresh sessions receive self-contained briefs; callers retaining an unabridged brief can request a small unchanged receipt with `base_run`.
- Add a persistent query cache with conservative deterministic-query eligibility, cross-process single flight, owner leases and fencing, integrity validation, isolated workers, and bounded cache entries, rows, files, memory, and query time.
- Add a persistent full-source FTS5 passage index with exact UTF-8 byte citations, immutable source versions, corruption detection, and explicit indexing and output coverage.
- Preserve typed CSV decimals and large integer Parquet results. Reject unsupported or out-of-range HUGEINT materialization instead of silently rounding through floating point.
- Safely reclaim abandoned import stages on macOS and Linux while file locks protect live writers.
- Exercise the task lifecycle against 34,015,435 real Massive records and a persistent direct DuckDB control. Include independently checked full results, source-change invalidation, concurrent publication, context resets, exact MCP token counts, and a bounded live-model evaluation.

## 0.5.1 — 2026-11-07

- Expand the README with everyday uses: document questions, spreadsheet analysis, writing, debugging, project follow-ups, and handoffs between agents.
- Add a resumable Massive downloader and bounded stress tests for CSV and typed Parquet, with independent exact-answer checks, actual MCP calls, resource measurements, and direct DuckDB controls.
- Publish a run over 34,015,435 real records: all five Parquet scales pass, including saved results and refresh; preserve the CSV decimal-inference difference as a failed exactness check.
- Exercise malformed inputs, nulls, large integers, oversized cells, query interruption, concurrent updates, freshness, and interrupted imports. Publish the observed limits alongside the results.
- Add optional benchmark dependencies for streaming data preparation and process-memory measurement. The Workspool runtime and core dependencies are unchanged.

## 0.5.0 — 2026-11-07

- Add `sync`: return unchanged status, complete line edits, or full source pages in one call. Accept retained version numbers or IDs; resolve initial reads to the current version and pin continuation pages to their original snapshot.
- Add `retrieve`: combine up to 8 literal queries and 16 independent reads, merge overlapping source spans, share metadata, and preserve per-request continuations under bounded output.
- Add optional explicit tokenizer support and `max_tokens` for `sync`/`retrieve`. Count the complete serialized MCP result, including duplicate representations when configured; CLI budgets count printed output. Add lossless `sources-v1` rendering and token-aware representation selection.
- Include the new operations in the eleven-operation efficient profile; preserve the compatible profile's six tools and existing defaults.
- Publish independently audited comparisons against v0.4.1: 12.83% fewer new payload tokens for sparse synchronization and 3.75% fewer for sixteen scattered ranges. Record the cases that increase tokens and distinguish fewer RPCs from fewer model decisions.
- Add a bounded live OpenAI evaluation with model-selected calls and separate input, output, cache, correctness, and timing records. Fix the stale-ID synchronization ambiguity it exposed; all six corrected cases pass. The small sample shows mixed token results and is not a universal savings claim.
- Keep the MIT License with Commons Clause and ship the full combined license in both package archives.

## 0.4.1 — 2026-11-07

- Prepare a standalone source snapshot intended for a new repository history, preserving the v0.4.0 implementation, tests, benchmark records, and provenance.
- Make no runtime or dependency changes; update release metadata and the quick-start tag to v0.4.1.
- Retain the MIT License with Commons Clause License Condition v1.0. Existing MIT grants for previously published copies remain unaffected; this snapshot does not revoke or retroactively change them.

## 0.4.0 — 2026-11-07

- **License change:** distribute this release under the MIT License with Commons Clause License Condition v1.0. Personal and internal business use, including saving token costs, remains permitted. Selling the software as defined by the Commons Clause is restricted. This is a source-available license, not an OSI-approved open-source license; see `LICENSE` and the README's Licensing section for the scope.
- Preserve the existing MIT grants for versions 0.3.0 and earlier and repository revisions published before this change. The new condition is not retroactive, and third-party dependencies retain their own licenses.
- Include the full combined license in source distributions and wheels, and identify it in package metadata as `LicenseRef-MIT-Commons-Clause`.
- Add a GitHub release workflow that tests and verifies both package archives, publishes their checksums, and refuses to replace existing version tags or releases.
- Add the `efficient` server profile, lazy tool discovery, raw text replies, optional single-copy MCP output, and adaptive lossless rendering of repeated metadata.
- Add full-source `search`, schema-only `describe`, and exact version `diff` operations. Keep the default `compatible` profile and its six eager tools unchanged.
- Publish reproducible workflow measurements and independent token recounts, including the cases that increase token usage. See `benchmarks/WORKFLOWS.md` for methodology and limits.

## 0.3.0

- Add `delete` and `prune`. Deletion refuses pinned versions; pruning preserves them and supports `--keep` and `--dry-run`.
- Commit catalog removal before collecting unused blobs. Recheck references under a separate write lock, report partial cleanup with `cleanup_pending` and `gc_errors`, and retry unused files on later pruning.
- Retain per-name version counters after deletion. Recreated names advance their versions, and database guards reject version reuse by older writers. Existing catalogs seed counters from saved history.
- Add an opt-in `import` MCP tool for native local files and an opt-in `delete` tool. The default six definitions and server instructions stay unchanged; published v0.2.0 results remain historical fixture measurements.
- Make the limits flags: `--query-timeout`, `--query-memory`, `--query-threads`, `--max-preview-rows`, and `--index-bytes`. The indexing default rises from 16 KiB to 64 KiB for newly saved versions.
- `resume` previews every pinned artifact instead of the first five.
- Add a `workspool` command alongside `agent-workbook`, and read `WORKSPOOL_HOME` and `WORKSPOOL_TOKEN` before the `AGENT_WORKBOOK_*` variables.
- Add a CLI `query` command, so the handoff can be run and checked without a model.
- Report "deleted by another session" when an excerpt's content was removed between resolving the catalog and reading the blob.
- Add CI (Linux and macOS, Python 3.11 to 3.13, plus the minimum supported MCP SDK) and regression tests for failed commits, partial cleanup, concurrent blob reuse, version counters, opt-in tools, limits, and the CLI.
- Rewrite the README around the measured results, including the cases Workspool loses.

## 0.2.0

- Use compact JSON and smaller artifact metadata in MCP replies by default.
- Preserve full provenance on demand with `inspect` `full: true`, or for every reply with `serve --response-format full`.
- Reduce generated tool-schema annotations and encode equivalent primitive unions more compactly.
- Read up to 16 distinct artifact references with one `inspect` call. Resolve the batch within one catalog snapshot and preserve per-artifact pagination and freshness.
- Add `serve --tools` to register only the selected MCP tools for a host.
- Publish the unchanged-client benchmark comparison, explicit optimized workflows, captured traces, and independent token recounts.
- Pass 21 regression tests with MCP 1.30.0 and the minimum supported MCP 1.26.0.

The MCP metadata defaults change in this release. Clients that require the previous complete field set can use `serve --response-format full`. CLI and direct Python inspection continue to return full metadata.

## 0.1.1

- Resolve checkpoint names, IDs, and project namespaces explicitly.
- Reject dependency cycles, including cycles introduced by refreshing newer versions.
- Require bearer authentication for local Streamable HTTP.
- Preserve query integers outside JavaScript's safe range as decimal strings with type metadata.
