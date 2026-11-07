# Workspool

**Stop re-sending work your agent already did.**

Workspool is a local, agent agnostic MCP workspace. Save a patch, a query result, a document, or a checkpoint once. A later session, on any MCP host and model with tool support, asks for the piece it needs: a short checkpoint, an excerpt from a specific version, or an aggregate computed on your machine with DuckDB. Imports keep source files outside the model's context; inline saves still send their contents once.

**License change in v0.4.0:** Workspool is source-available under the [MIT License with Commons Clause](LICENSE). Personal and internal business use, including reducing token costs, is permitted. Selling the software as defined by the Commons Clause is restricted. See [Licensing](#licensing) for the scope and the status of earlier MIT versions.

It saves the most where replaying the source costs the most, and it is not free. The numbers below are the measured ones, including the cases it loses.

## Where it saves tokens, and where it doesn't

The largest gains come from keeping irrelevant source text out of the conversation: import files once, query local tables, and retrieve only the evidence needed. Version 0.5.0 adds `sync` for a retained document, `retrieve` for combined searches and ranges, and explicit tokenizer budgets. Each has overhead.

The new benchmark compares real MCP calls with competent **v0.4.1** workflows. It counts cold instructions and schemas, scripted requests, exact replies, and answers with `cl100k_base`. Both sides use tailored tool allowlists and text-only replies; these are payload counts, not billed-token savings.

| Workflow | v0.4.1 control | v0.5.0 | Change |
| --- | ---: | ---: | --- |
| Maintain a complete runbook over eight checks and two small edits | 2,651 | 2,311 | **12.83% fewer** |
| Maintain it through two extensive rewrites | 4,739 | 4,697 | 0.89% fewer; 10 tool calls → 8 |
| Read 16 scattered ranges from one source | 3,437 | 3,308 | **3.75% fewer**; 16 tool calls → 1 |
| Keep a tiny note current | 1,088 | 1,095 | 0.64% more |
| Find nearby facts, reusing each search result | 1,696 | 2,151 | **26.83% more** |
| Read complete documents | 11,014 | 12,086 | **9.73% more** |

Use `sync` when you retain the complete source and need to keep it current. Use `retrieve` selectively for many independent ranges or combined requests; ordinary `inspect` and `search` remain cheaper for simple reads and neighboring facts. Combining calls does not necessarily save model turns: a capable host can already issue independent calls together. Even the 16-range case slightly increases the measured retained-input exposure despite its smaller new payload.

The [roundtrip report](benchmarks/ROUNDTRIPS.md) publishes all 29 arms, both tokenizers, independent recounts, exact reconstruction checks, and losing cases. A separate [live model evaluation](benchmarks/MODEL_EVAL.md) let `gpt-6-luna` choose tools: all six final cases passed, but input-token usage increased for passage discovery and sparse edits and decreased for dense rewrites. This small diagnostic run establishes neither universal savings nor reliable latency gains. It also exposed and helped fix an old-ID synchronization ambiguity before release.

The [historical workflow report](benchmarks/WORKFLOWS.md) records the earlier improvements from full-source search, table descriptions, and exact deltas, including a 95.47% payload reduction for finding evidence deep in initially unknown documents. That comparison uses an older implementation and a different task; it is not an additional v0.5.0 gain.

`serve --profile efficient` enables all eleven operations through lazy discovery and adaptive replies. It preserves both MCP response representations by default. Select the tools and response content your host needs; the profile does not choose an optimal workflow automatically.

<details>
<summary>Historical known-input measurements: the earlier overhead optimization</summary>

Measured in one run with `cl100k_base`. Each Workspool count includes server instructions, advertised tools, the question, request arguments, and decoded text replies. Artifacts are already imported; import costs are recorded separately. Document references are known, and reads are batched.

| Workload | Raw replay | Default tools | Lazy, arguments known | Lazy, including schema discovery |
| --- | ---: | ---: | ---: | ---: |
| 20 synthetic rows, one aggregate | 222 | 820 | 286 | 441 |
| 2,000 synthetic rows, one aggregate | 19,039 | 823 | 289 | 444 |
| 250 real Massive daily bars, one aggregate | 9,827 | 814 | 280 | 435 |
| 120 documents, read 3 excerpts | 92,674 | 1,101 | 568 | 703 |
| 120 documents, read every word | 92,657 | 107,393 | 106,923 | 107,058 |

`--tool-discovery lazy` advertises one small dispatcher instead of all six schemas. The known-arguments column assumes the caller already knows the operation's inputs; the last column pays for fetching its schema first. Setup falls from **652 to 110 tokens**. On the Massive fixture, the complete query payload falls from **814 to 280 tokens**, or **435** with discovery included. The default configuration remains unchanged.

The remaining losses are useful boundaries:

- **Tiny inputs still lose.** The 20-row synthetic source costs 222 tokens directly and 286 even with known lazy arguments. A short note or direct calculation can be cheaper. Row size matters: 20 market bars contain more information than 20 synthetic rows.
- **Reading everything still costs more.** Raw excerpt blocks with `--response-format text --tools inspect` reduce the complete-document run to **102,810 tokens**, from 15.90% overhead to **10.96% overhead**. Every source word, version reference, and freshness record still has a cost. Short excerpts can be cheaper in the default JSON format.
- **Host behavior matters.** By default MCP carries both text and structured data. A host forwarding the whole result can include both copies. `--response-content text` removes the structured copy for compatible hosts; on the complete-document run, the full-result count falls from **216,708 to 108,466** with text format and inspect-only tools. This is a different accounting view from the table.
- **Local computation and selective retrieval do most of the work.** Import files once, query only the answer, and request only relevant excerpts. Inline saves send their source once. Plain shell commands can be cheaper for isolated work; Workspool adds versions, pinned inputs, freshness checks, and reusable handoffs.

The [earlier known-input report](benchmarks/EFFICIENCY.md) includes both tokenizers, exact validation, import costs, host representations, and reproduction commands. These are historical scripted payload counts, not billed-token savings or model tool-choice tests. Massive's existing `store_as`/`query_data` workflow already avoids raw replay; this comparison does not claim to beat that workflow. The earlier [optimization report](benchmarks/OPTIMIZATION.md) and [baseline](benchmarks/RESULTS.md) retain the historical Qwen measurements and multi-session comparisons.

</details>

## What goes in a workbook

| Work | Save | Pick it up later |
| --- | --- | --- |
| Fix a bug | The patch or commit reference, test output, and the next step | Verify exactly what changed and where review stands |
| Research a question | Source excerpts, findings, and open questions | Read the evidence behind a finding and continue the research |
| Revise a document | Draft versions and the decisions behind them | Open the right draft and see why earlier choices were made |
| Analyze data | CSV or Parquet snapshots, SQL, and saved results | Run a follow-up query against the same inputs or refresh the result with new data |

A short note is often enough for a one-off handoff. A workbook earns its overhead when the files matter, the numbers need to be reproduced, or several sessions will build on the same work.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and check out the release:

```sh
git clone --branch v0.5.0 --depth 1 https://github.com/idpfaff/workspool.git
cd workspool
```

Run these from the project directory. The command is `workspool` (`agent-workbook` still works); the Python package is `agent-workbook`.

```sh
uv sync --locked --python 3.12
uv run workspool --help
uv run workspool bench
```

Python 3.11 or newer, SQLite with FTS5, and a supported DuckDB wheel. CI runs on macOS and Linux. The first install downloads dependencies; after that it runs offline. MacBook and MLX inference timing still needs to be measured on the target machine.

## Connect your agent

Use stdio with a local MCP host. Give each host the same absolute workspace path when they should share the work.

```json
{
  "mcpServers": {
    "workspool": {
      "command": "/absolute/path/to/workspool/.venv/bin/workspool",
      "args": ["--workspace", "/absolute/path/to/MyWorkbook", "serve"]
    }
  }
}
```

Point the command at the environment `uv sync` created; from the project directory, `printf '%s\n' "$PWD/.venv/bin/workspool"` prints it.

The workbook defaults to `~/.agent-workbook/default`. Set `WORKSPOOL_HOME` or pass `--workspace` to choose another directory (`AGENT_WORKBOOK_HOME` is still read). Separate workspaces keep unrelated projects apart; checkpoints inside a workspace can be grouped with names such as `pricing/handoff`.

Workspool speaks MCP and runs independently of the model. Any host that supports MCP tool calls works, with a local MLX model, another local runtime, or a hosted model. The host decides which tool responses it forwards to its model.

For the retrieval and revision features, start with:

```sh
uv run workspool --workspace ~/MyWorkbook serve --profile efficient
```

This exposes the six existing operations plus `search`, `describe`, `diff`, `sync`, and `retrieve` through the lazy `workspool` dispatcher, with adaptive text replies. The default `compatible` profile retains the six eager tools and compact JSON. `--tools`, `--tool-discovery`, and `--response-format` override the selected profile; `import` and `delete` always require an explicit allowlist.

### Choose the tools each host gets

The compatibility default exposes six tools. `search`, `describe`, `diff`, `sync`, and `retrieve` can be added individually or enabled by the efficient profile. Two other operations require an explicit allowlist because they read or remove local files:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools save find inspect resume query refresh import delete
```

For a host that only retrieves work, expose only what it uses, which also shrinks the per-session overhead:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools resume inspect
```

A host that queries known tables can run with `--tools query`; that tool keeps its `save_as` capability.

### Load tool schemas when needed

Lazy discovery keeps the configured operations available through one `workspool` tool:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tool-discovery lazy
```

Call `workspool` with `{"tool":"query"}` to retrieve the query schema, then execute it with:

```json
{"tool":"query","arguments":{"inputs":["replay/bars"],"sql":"SELECT count(*) AS bar_count FROM input1"}}
```

Callers that already know the arguments can execute directly. Omitted or null `arguments` requests a schema; `arguments: {}` executes the operation with its defaults. `--tools` still controls the allowlist, including opt-in `import` and `delete`. Original argument validation and version checks apply. Lazy mode changes the calling interface, so use it with clients that can call the dispatcher; eager tools remain the default. Discovering many schemas can cost more than advertising them up front.

### Keep MCP overhead small

Replies are compact JSON by default. IDs, names, versions, freshness flags, dependencies, checkpoint fields, query results, and requested excerpts are always present. The compact form of `inspect` omits the summary, source label, hash, timestamp, size, kind, media type, and previous version ID; request `inspect` with `full: true` for all of them, or start with `serve --response-format full` to get full metadata in every reply. `find` and `resume` keep summaries, since that is where you decide what to open. CLI output is always full.

Read related artifacts in one call:

```json
{"ref": ["project/decision", "project/evidence"], "excerpt_bytes": 1000}
```

`inspect` accepts up to 16 distinct references in one catalog snapshot. Each excerpt is capped at 8 KiB; follow each returned `next_offset` to read the next page.

For long document reads, text format avoids quoting and escaping the excerpt inside model-facing JSON. A host that reads text responses can also request a single copy:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools inspect --response-format text --response-content text
```

In text format, `content[0].text` contains compact JSON metadata. Each artifact's `excerpt_block` is the zero-based index of the content block containing its exact raw excerpt; batches preserve the `artifacts` array. Full metadata and pagination work as before. With the default `--response-content both`, `structuredContent` retains the original object with string `excerpt` fields. Choose `text` to omit that structured copy, or `structured` to omit successful text replies for hosts that consume structured results. Errors remain readable text. These modes require a compatible host; [the report](benchmarks/EFFICIENCY.md#response-representations) specifies the block layout and measured tradeoffs.

`--response-format adaptive` chooses among compact JSON, raw excerpt blocks, and packed artifact metadata using serialized byte size. It preserves missing fields, nulls, values, and exact excerpts. With `--tokenizer`, it instead chooses the lowest counted serialized result among its lossless representations; without a tokenizer, byte size remains a heuristic. Hosts can reconstruct the original object with [`agent_workbook.render.decode`](src/agent_workbook/render.py); `structuredContent` stays unchanged when present. The [workflow report](benchmarks/WORKFLOWS.md#adaptive-replies) describes the versioned envelopes.

### Retrieve the evidence you need

Under the efficient profile, call `workspool` with a literal source search:

```json
{"tool":"search","arguments":{"text":"release code","context_bytes":128}}
```

Search covers current text artifacts, including content beyond the 64 KiB catalog index. Supply `refs` to search up to 16 selected versions. Hits include exact snippets, pinned IDs, freshness, and UTF-8 byte ranges. Follow `cursor` until `complete` is true; an empty page with a cursor does not mean no match. Search is case-insensitive literal matching, not arbitrary regular expressions, and rejects malformed UTF-8. Each call reads at most 16 MiB and bounds hit objects to 32 KiB, excluding small cursor/envelope metadata; `limit` is 1–20 and `context_bytes` is 0–2,048 per side. Continuations reject a changed query or relevant snapshot.

For an unfamiliar table, `describe` returns exact column names and SQL types without sample rows:

```json
{"tool":"describe","arguments":{"inputs":["replay/bars"]}}
```

Follow each table's `next_offset` to verify its complete schema. Use its returned artifact ID in subsequent queries to keep the same version. The [workflow report](benchmarks/WORKFLOWS.md#table-schema-discovery) explains limits and the small measured gain over an already efficient SQL schema query.

### Read edits against a retained version

If the caller holds the complete text of an immutable version, `diff` returns exact line edits and current metadata:

```json
{"tool":"diff","arguments":{"ref":"operations/runbook","base":"wb_saved_version_id","max_bytes":8192}}
```

Apply edits in reverse order to the retained base's lines, preserving their line endings. Empty changes mean the content is unchanged; freshness metadata still matters. `requires_read: true` requests a full `inspect` of the returned pinned artifact ID, with an explicit reason. Deltas are never silently truncated. If a new session or compaction has lost the complete base, read the source again. The [delta contract and limits](benchmarks/WORKFLOWS.md#exact-version-deltas) include the reconstruction code and failure behavior.

### Synchronize without a separate fallback read

`sync` chooses unchanged status, complete line edits, or a full source page within one call:

```json
{"tool":"sync","arguments":{"ref":"operations/runbook","base":3,"max_bytes":8192}}
```

Supply `base` only when you retain that version's **complete text**. It accepts a version number scoped to the source name or an immutable artifact ID. Omit it after losing context. At offset zero, `ref` resolves the current version of that name, even if you supply an old ID. To read a historical version deliberately, use `inspect`.

- `mode: "unchanged"`: keep the retained text and update its metadata.
- `mode: "delta"`: apply `changes` in reverse order to the base's lines, preserving line endings. The entire delta is present.
- `mode: "full"`: replace the old text by collecting `excerpt` pages until `complete`. Continue with the returned artifact ID and `next_offset`; subsequent pages stay pinned even if a newer version appears.

Missing or deleted bases fall back to a full read. Deltas use the existing bounded comparison algorithm; an unsuitable or larger delta also falls back directly. Full synchronization supports valid UTF-8 sources up to 16 MiB and verifies their content hash. `max_bytes` caps the full metadata-and-content JSON object at 1–32,768 bytes, before MCP rendering. An impossible budget returns an explicit error instead of silently dropping source content.

### Combine searches and independent ranges

```json
{"tool":"retrieve","arguments":{"queries":[{"text":"renewal_code","context_bytes":128},{"text":"owner","context_bytes":128}],"reads":[{"ref":"operations/runbook","offset":0,"bytes":512}],"max_bytes":8192}}
```

`retrieve` resolves names and search scopes in one catalog snapshot. It returns shared `artifacts` metadata, exact `spans`, and ordered `queries`/`reads` results that refer to those spans by index. A match or read's byte offsets select its exact content within the span; overlapping and adjacent ranges merge only within the same immutable artifact.

Follow each unfinished query's `cursor` and each read's `next_offset`, using the returned artifact ID for continued reads. Under a tight budget, a read may be deferred with an empty span and its original offset; process the other returned progress and retry it. A response must make progress somewhere or fail explicitly. Queries use the same literal matching and cursor validation as `search`.

The limits are 8 queries, 16 reads, 32,768 bytes per requested read, and 16 MiB of total scan/read work per call, including repeated search windows. The default `max_bytes` is 32,768 and caps the complete result JSON. Combining queries does not create a persistent search index or guarantee a single scan. Existing `inspect` remains the lower-overhead choice for ordinary reads in the measured cases.

### Set a measured output-token budget

Install the optional tokenizer dependencies and configure the encoding explicitly:

```sh
uv sync --locked --extra tokens
uv run --locked --extra tokens workspool --workspace ~/MyWorkbook serve \
  --profile efficient --tokenizer cl100k_base
```

Then include `"max_tokens": 800` in a `sync` or `retrieve` call. Supported encodings are `cl100k_base`, `o200k_base`, or a path to a local `tokenizer.json`; Workspool does not guess an encoding from a model name. Named encodings may download their rank files on first use. A local tokenizer file works offline.

The limit counts the exact compact JSON serialization of the **complete successful MCP result**, including both text and structured content when enabled. It excludes JSON-RPC framing, provider message formatting, and error replies, so it is not a billing cap. Limits are 1–131,072 tokens. Requests without a configured tokenizer, or budgets too small for metadata and progress, fail explicitly. CLI `sync`/`retrieve` also support `--tokenizer` and `--max-tokens`, counting their actual printed JSON and final newline instead.

Adaptive replies may use a `sources-v1` envelope: `data` holds metadata and `excerpt_paths` identifies where the following exact text blocks belong. Use `agent_workbook.render.decode` to reconstruct it. Structured content always retains the original object. Token budgets limit output size; they do not summarize or discard evidence silently.

### Connect over local Streamable HTTP

Generate a token and start the server:

```sh
export WORKSPOOL_TOKEN="$(openssl rand -hex 32)"
uv run workspool --workspace ~/MyWorkbook serve --transport streamable-http
```

Use `http://127.0.0.1:8731/mcp` and send `Authorization: Bearer <your token>` with every request. The listener binds to loopback. Stdio needs no token. `AGENT_WORKBOOK_TOKEN` is still read.

## Save a handoff and resume it

1. In the first session, `save` the patch, test output, or other work to keep. Save a checkpoint with the objective, what was verified, the next step, and the IDs of the supporting artifacts.
2. Open a fresh session connected to the same workspace. Ask it to `resume` with the checkpoint name, ID, or project namespace.
3. Use `inspect` to read a specific piece of evidence. Use `find` when you remember a keyword and need the checkpoint name.

`resume` resolves an exact checkpoint name or ID first. A namespace such as `pricing` selects a checkpoint under `pricing/` when there is a single match; with several matches, the reply lists candidate IDs. The reply previews every artifact the checkpoint pins.

<details>
<summary>Run the handoff yourself without a model</summary>

This demo saves a sample patch and a sample test record in one MCP session, then opens a second session on the same workbook and retrieves the exact patch from the checkpoint. Run it from the project directory after installation.

```sh
uv run --locked python - <<'PY'
import asyncio
import sys
from contextlib import asynccontextmanager
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@asynccontextmanager
async def session(workspace):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_workbook.cli", "--workspace", workspace, "serve"],
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


async def call(client, tool, arguments):
    reply = await client.call_tool(tool, arguments)
    if reply.isError:
        raise RuntimeError(reply.content)
    return reply.structuredContent


async def main():
    with TemporaryDirectory() as workspace:
        async with session(workspace) as client:
            patch = await call(client, "save", {
                "name": "lookup/patch",
                "kind": "code",
                "content": "- return None\n+ return []\n",
                "summary": "Return an empty list when the lookup has no matches",
            })
            tests = await call(client, "save", {
                "name": "lookup/test-output",
                "content": "Sample test record\n2 passed\n",
            })
            await call(client, "save", {
                "name": "lookup/handoff",
                "kind": "checkpoint",
                "checkpoint": {
                    "objective": "Fix the empty lookup response",
                    "verified": "The sample test record shows two passing checks",
                    "next_step": "Send the patch for review",
                },
                "dependencies": [patch["artifact_id"], tests["artifact_id"]],
            })

        async with session(workspace) as client:
            handoff = await call(client, "resume", {"text": "lookup/handoff"})
            checkpoint = handoff["checkpoint"]
            evidence = await call(client, "inspect", {
                "ref": checkpoint["dependencies"][0],
                "excerpt_bytes": 256,
            })
            assert evidence["excerpt"] == "- return None\n+ return []\n"
            for field in ("objective", "verified", "next_step"):
                print(checkpoint[field])
            print(evidence["excerpt"], end="")


asyncio.run(main())
PY
```

</details>

## Use a saved result as input

Import a file from the terminal, or let a host that has the `import` tool do it by absolute path. The workbook copies the file in its native format and assigns a version. Importing keeps the source out of the model's context; saving it inline through `save` costs the tokens of the content once.

```sh
uv run workspool --workspace ~/MyWorkbook import ./bars.parquet --name replay/bars
uv run workspool --workspace ~/MyWorkbook find replay
uv run workspool --workspace ~/MyWorkbook inspect replay/bars
```

A `query` call that counts the rows and saves the result as a new artifact. DuckDB runs it on your machine; the tool returns a preview and a reference to the full result.

```json
{
  "inputs": ["replay/bars"],
  "sql": "SELECT count(*) AS bar_count FROM input1",
  "save_as": "replay/count"
}
```

The same query from the terminal, to check a handoff without a model:

```sh
uv run workspool --workspace ~/MyWorkbook query "SELECT count(*) AS bar_count FROM input1" --input replay/bars --save-as replay/count
```

When a new snapshot arrives, import it under `replay/bars` with `--expected-version` set to its current version. Workspool marks `replay/count` stale; `refresh` reruns the saved SQL on the current input. For longer dependency chains, refresh upstream results first.

## Delete and prune

Nothing is removed on its own. Deletion refuses versions another artifact still pins and names the blockers. Pruning skips pinned versions.

- `delete <wb_id>` removes one superseded version. `delete <name> --expected-version N` removes every version of a name, with the same version check an update makes.
- `prune` drops superseded versions nothing pins, keeping the newest `--keep` versions of each name (default 1). It runs to a fixed point, so an old result and the old input it pinned go together. `prune --dry-run` reports what would go.

```sh
uv run workspool --workspace ~/MyWorkbook prune --dry-run
uv run workspool --workspace ~/MyWorkbook prune --keep 2
uv run workspool --workspace ~/MyWorkbook delete replay/scratch --expected-version 3
```

Catalog removal commits before blob cleanup. Cleanup takes a new write lock and checks references again, so a file another session has reused stays available. The reply reports files actually removed and bytes freed. If a file cannot be removed, `cleanup_pending: true` and `gc_errors` report the problem. The catalog deletion remains committed, and a later `prune` retries unused files.

Names retain their version counters after deletion. If `draft` reached version 3, deleting it and recreating it produces version 4. An old session holding `expected_version: 3` cannot overwrite or delete that replacement. Existing catalogs seed their counters from saved history when opened. The database also rejects version reuse by older writers.

Over MCP, `delete` is available only to hosts whose `--tools` list includes it.

## Versions and source files

- **A name points to the current version.** An artifact ID points to one specific version. To update a name, pass its current `expected_version`; this catches competing edits from separate sessions.
- **Results pin their input IDs.** If any recorded input or ancestor has a newer version, the derived result is marked stale. Queries require current inputs by default; set `allow_historical` to replay a specific older snapshot.
- **Imports are snapshots.** When the original file changes, import a new version. The previous bytes stay available by artifact ID until you delete or prune them.
- **Native files stay in their original format.** The MCP tools return paged excerpts for supported text and query CSV or Parquet. Use `export` to copy any version back out, such as a PDF or an image: `uv run workspool --workspace ~/MyWorkbook export replay/bars ./bars-copy.parquet`.
- **Large integers stay exact.** Query previews return integers outside JavaScript's safe range as decimal strings and list the affected columns in `integer_string_columns`. Parquet results keep their original types. Pass a large integer back through a JSON client as a string with an explicit SQL cast.

## MCP tools

| Tool | What it does |
| --- | --- |
| `save` | Save a note, code, document text, inline CSV, or checkpoint. Inline content is capped at 1 MiB; use `import` for larger or native files. |
| `find` | Search current names, summaries, source labels, and the indexed start of each text artifact (64 KiB by default). |
| `inspect` | Read versions, freshness, dependencies, and optional excerpts for one or up to 16 references. `full: true` returns complete metadata. |
| `resume` | Open a checkpoint and preview every artifact it pins. |
| `query` | Run one SELECT over selected CSV or Parquet artifacts as `input1`, `input2`, and so on. Optionally save the full result as Parquet. |
| `refresh` | Rerun a saved query using the current versions of its direct inputs. |
| `search` | Efficient profile or explicit allowlist. Find exact source passages beyond the catalog index, with bounded scanning and continuations. |
| `describe` | Efficient profile or explicit allowlist. Page through exact table column names and SQL types without returning rows. |
| `diff` | Efficient profile or explicit allowlist. Return exact edits against a complete immutable base the caller retains, or request a full read. |
| `sync` | Efficient profile or explicit allowlist. Return unchanged status, exact edits, or a full page of the current source in one call. |
| `retrieve` | Efficient profile or explicit allowlist. Combine up to 8 literal searches and 16 byte-range reads, merging overlapping spans. |
| `import` | Opt-in. Copy a local file by absolute path in its native format: CSV, Parquet, PDF, images, anything. |
| `delete` | Opt-in. Delete a superseded version by ID, or every version of a name with its `expected_version`. |

## Limits

The defaults keep a query predictable on a laptop; raise them when the machine and the task call for it. All are flags before the subcommand and apply to the CLI and the server alike.

| Flag | Default | Bounds |
| --- | ---: | --- |
| `--query-timeout` | 15 s | a running SELECT is interrupted after this |
| `--query-memory` | 1GB | DuckDB memory limit |
| `--query-threads` | 2 | DuckDB threads per query |
| `--max-preview-rows` | 25 | most rows one query reply previews; the preview is also capped at about 7.5 KB |
| `--index-bytes` | 65536 | bytes of each text artifact indexed for `find` |

The indexing limit applies when saving a new version. Existing versions keep their earlier search index; import a new version to index more of an existing source.

Fixed bounds: names up to 120 characters, summaries and source labels up to 360, at most 16 dependencies per artifact, at most 8 inputs and 6,000 characters of SQL per query, one query at a time per server process, and 8 KiB per `inspect` excerpt page. The `sync` and `retrieve` limits are documented above.

## Benchmark

The included benchmark uses 2,000 synthetic rows and asks for the number of positive rows and their total. The workbook runs the calculation locally; the benchmark checks the answer against a separate calculation and compares sending the raw rows in the prompt with sending a checkpoint plus the query result.

| Input to the benchmark | Payload size |
| --- | ---: |
| Question and raw data handoff | 14,200 bytes |
| Question, workbook checkpoint, and exact query result | 1,091 bytes |
| Reduction on this fixture | 92.3% |

This compact benchmark counts payload bytes only. The larger per-session tables include tool definitions, arguments, and replies. Both measurements exclude model reasoning, final-answer tokens, provider billing, and inference time. To count tokens with your model's tokenizer, pass its local `tokenizer.json`:

```sh
uv sync --locked --extra benchmark
uv run --locked --extra benchmark workspool bench --tokenizer /path/to/model/tokenizer.json
```

The historical larger measurements, with up to one million rows, 120 documents, two tokenizers, and 56 scripted MCP sessions, are documented in [`benchmarks/`](benchmarks/). The newer [workflow suite](benchmarks/WORKFLOWS.md) covers discovery and updates with 18 traces and 177 scripted decision prefixes. Scripts, result JSON, and independent recounts are published there. Historical comparisons require a separately retained original-history baseline checkout, which is not included in this standalone snapshot; current standalone benchmarks still run without it.

## How it runs

SQLite stores the artifact catalog, search index, checkpoints, and dependency links. File contents live under their SHA-256 hashes in the workspace directory. DuckDB runs analytical queries in a separate in-memory connection with file access limited to the selected artifacts and the temporary output used when saving.

The server is designed for trusted local workspaces and files. Give each host access to the workspace it needs, and the tools it needs. Checkpoints are saved explicitly, and freshness follows the versions recorded in the workbook.

## Development

```sh
uv sync --locked --extra benchmark --extra tokens
uv run --locked --extra benchmark --extra tokens pytest -q
uv run --no-sync ruff check src tests benchmarks
uv run --no-sync ruff format --check src tests
```

CI runs the suite on Linux and macOS with Python 3.11 to 3.13, and once more with the minimum supported MCP SDK. Coverage includes independent-session handoffs, stale results and refresh, deletion and pruning with pinned versions, the opt-in tools, HTTP authentication, exact large integers, batched Unicode reads, complete metadata access, restricted tool registration, current-version synchronization, merged retrieval and pagination, and exact serialized token limits.

To publish a new GitHub release, update the version in `pyproject.toml` and `uv.lock`, add its changelog entry, and push to `main`. The release workflow runs when the package metadata or release workflow changes, and also supports manual runs on `main`. It tests the package, verifies the license in both archives, and publishes a new version tag with a wheel, source distribution, and checksums. It refuses to replace an existing tag or release. It does not publish to PyPI.

If you find a bug, include the command or tool arguments, the result you expected, and a small input that reproduces it.

## Licensing

Starting with **v0.4.0**, Workspool is distributed under the [MIT License with Commons Clause License Condition v1.0](LICENSE). These are combined terms, not a choice between two licenses. The package metadata identifies them as `LicenseRef-MIT-Commons-Clause`. Workspool is source-available; this combined license is not an OSI-approved open-source license.

You may use Workspool personally or internally at a business, including to save money on tokens, and modify or redistribute it subject to the combined license. The license does not grant permission to **Sell** the software. In the Commons Clause, this includes providing a product or service to others for a fee or other consideration when its value derives entirely or substantially from Workspool's functionality. Paid hosting, consulting, or support can fall within that restriction. It is not a blanket prohibition on every commercial product that incorporates Workspool. The [full license](LICENSE) controls; the [Commons Clause FAQ](https://commonsclause.com/) provides further explanation.

Versions 0.3.0 and earlier, and repository revisions published before this license change, were offered under MIT. Their existing license grants are not revoked by this change. The restriction starts with this revision and the v0.4.0 release; it does not retroactively change earlier copies. Third-party dependencies retain their own licenses.
