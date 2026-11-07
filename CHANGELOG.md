# Changelog

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
