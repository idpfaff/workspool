# Workspool

**Carry useful context across sessions, retrieve evidence within a budget, and coordinate shared work.**

Workspool gives AI agents a shared local workspace for work that continues across sessions. Its opt-in `runtime` profile captures compact facts from executed tools and restores relevant local context for a configured startup goal. Optional local embeddings combine semantic and keyword retrieval. Durable tasks keep source versions, calculated results, and evidence together; fenced claims coordinate agents sharing the same workspace.

- **Capture useful work automatically.** Configured MCP hooks retain bounded result previews, artifact references, task status, and failures. Bulk source bodies and SQL literals stay out of session memory.
- **Restore relevant context.** Supply a startup goal and tokenizer to receive a bounded recall package. Add a provisioned local embedding model for paraphrase retrieval alongside keyword matches.
- **Run recurring work.** Combine SQL with registered local Python programs or tool adapters, branch on results, and pause for exact-input approval. An explicit runner handles intervals and input-change triggers.
- **Coordinate agents.** Give each server a configured identity and role, claim a task before execution, and inspect producer events and stale dependency versions.
- **Keep evidence inspectable.** Reopen complete task briefs after context loss, with immutable source references, exact byte citations, and explicit omissions.
- **Reuse successful analytical plans.** Distill a published task into a discoverable skill, validate new inputs against its saved schema, and run its SQL again without reconstructing the plan.
- **Review changes and delivered value.** See which source versions made a result stale, check current approvals, and inspect measured latency, result reuse, and formatting token reductions.

Works with local or hosted models through an MCP-compatible agent. Workspace files and embedding inference stay local; the host receives selected context and tool replies. Automatic capture covers calls through the configured Workspool server. Other tools and conversations need explicit host hooks, and the MCP host must forward initialization instructions for startup recall to reach the model. Workspool does not monitor your desktop or watch external files. Scheduled execution requires an explicitly started local runner; configured workers run with that account's permissions.

[Connect Claude Desktop, Cursor, or Codex](docs/HOSTS.md) · [Reusable analytical skills](docs/SKILLS.md) · [Runtime guide](benchmarks/RUNTIME.md) · [Public dataset stress](benchmarks/PUBLIC_DATA.md)

[Recurring work, approvals, shared workbooks, and metrics](docs/RECURRING_WORK.md) covers the complete workflow, trusted-worker contract, capability grants, and portable snapshots.

## What you can save

Workspool reduces the material your agent needs to read again. Use it for everyday work that grows across files, questions, and sessions:

| Common task | Keep in Workspool | Where you save tokens |
| --- | --- | --- |
| **Answer questions about documents** | Reference material, research notes, and supporting passages | Retrieve the passages for each question instead of repeatedly sending entire documents. |
| **Analyze spreadsheets and reports** | CSV or Parquet exports of sales, spending, customer activity, or operations | Calculate totals, trends, and comparisons locally; send the answer or selected rows to your agent. |
| **Analyze stock market data** | Imported price and volume history, fundamentals, earnings notes, and saved screens | Use local SQL to calculate returns, volume trends, and volatility; send selected symbols, summary tables, and supporting notes to your agent instead of every market-data row. |
| **Write and revise long documents** | Drafts, review comments, and editing decisions | Read the section being edited; receive only the changes when your agent still retains the previous draft. |
| **Develop and debug software** | Relevant code, error logs, test results, and attempted fixes | Carry the useful evidence into the next session without replaying the full debugging conversation. |
| **Manage projects and follow-ups** | Meeting notes, decisions, action items, and progress checkpoints | Start from the current status and next action, then open the supporting details when needed. |
| **Switch agents or resume later** | The objective, verified results, supporting files, and next step | Give the next agent a focused handoff instead of rebuilding the task history. |

Saved items have stable references and versions. Results track their input versions, so your agent can see when they need refreshing. Saving an item does not replay it into the conversation; configured startup recall or an explicit retrieval selects what the host receives.

For recurring spending reviews, v0.9.0 can calculate totals, run a registered Python check, pause for approval, rerun when inputs change, and transfer the workbook with its evidence. The complete workflow passed 25 checks. Its 18 measured writer replies used **11,148 tokens versus 21,680 for full JSON replies containing the same information: 10,532 fewer tokens (48.6%)**. This measures response formatting with `o200k_base`, excluding request/setup tokens and model billing; it is not a comparison against a SQL script. [Workflow results and exact counts](benchmarks/RECURRING_WORKFLOW.md).

In the local recall probe, keyword and hybrid retrieval both found **5/5 target Wikipedia articles first**. A separate four-question synthetic paraphrase diagnostic found **4/4 targets with the real local model versus no keyword hits**; it demonstrates a narrow capability, not a general accuracy gain. The MCP initialization response also included an exact source citation within its 1,200-token context budget. A separate [real Codex host test](docs/HOSTS.md#verified-model-delivery-codex-cli) confirms model delivery after metadata discovery. [Semantic recall results and limits](benchmarks/SEMANTIC.md).

### Earlier task measurements: v0.6.0

In the v0.6.0 live agent evaluation, both approaches answered correctly using local data and saved work. These measurements cover task tools, not the new v0.7.0 runtime capture or semantic recall. Task tools reduced the tokens needed to recover and update an analysis:

| Agent action | Existing SQL and saved-result tools | Task tools | Tokens saved |
| --- | ---: | ---: | ---: |
| Resume after losing conversation context | 9,840 | 6,629 | **3,211 (32.6%)** |
| Recalculate after sources change | 12,579 | 7,730 | **4,849 (38.5%)** |

These are provider-reported input plus output tokens for one small workflow, including tool schemas and model decisions. Creating the task cost more; across initial setup and all three follow-ups, task tools used **33,814 tokens versus 33,012**, or 2.4% more overall. The benefit appeared in recovery and source updates, not every turn. [Full results, including the initial failures and fixes](benchmarks/TASK_MODEL.md).

On **34,015,435 real market records**, a three-step task took **1.38 seconds cold and 22 ms with reusable results**. Changing one input recomputed its two affected steps and reused the independent branch. A direct cached DuckDB script was faster; Workspool supplies the shared task state, provenance, recovery, and agent interface. [Stress results and token counts](benchmarks/TASK_STRESS.md) · [Task API and limits](benchmarks/TASKS.md).

Public-data tests also exercised **48.7 million NYC taxi trips**, **38.5 million NOAA observations**, and **8,192 Wikipedia articles containing 442 MiB of text**. Exact-result and recovery checks passed; capable persistent SQL and text-index controls were faster and often used fewer tokens. The report retains incomplete-index and tight-budget failures. [Public dataset results](benchmarks/PUBLIC_DATA.md).

As a source-exclusion example, the recorded 1,000-row stock-data summary used **1,006 `cl100k_base` tokens**, including compatible-profile setup, import, query requests, and replies, versus **30,375 tokens** for the CSV alone. That measures text kept out of context, not a provider bill or an advantage over a local SQL script. The [Massive stress report](benchmarks/MASSIVE_STRESS.md#token-savings) also covers 34 million real rows and the limits of that comparison.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and check out the release:

```sh
git clone --branch v0.9.0 --depth 1 https://github.com/idpfaff/workspool.git
cd workspool
```

Run these from the project directory. The command is `workspool` (`agent-workbook` still works); the Python package is `agent-workbook`.

```sh
uv sync --locked --python 3.12 --extra tokens
.venv/bin/workspool --help
```

Python 3.11 or newer, SQLite with FTS5, and supported DuckDB wheels. Runs on macOS and Linux. The first install downloads dependencies. The [host walkthrough](docs/HOSTS.md) also provides a standard Python `venv` and `pip` install using the release wheel.

Keyword recall works immediately. For optional semantic recall, install the local inference extra and explicitly provision the embedding model once:

```sh
uv sync --locked --python 3.12 --extra tokens --extra semantic
.venv/bin/workspool model-download "$HOME/.cache/workspool/bge-small-en-v1.5"
```

This downloads and hashes the supported local model; the destination must be new. Retrieval never downloads a model. Omit `--model-dir` below to use the disclosed lexical fallback. Both named tokenizer vocabularies are bundled and hash-verified, so tokenizer startup works offline after installing the `tokens` dependencies. A local `tokenizer.json` also works.

## Connect your agent

Generate and install the configuration for your host without editing JSON:

```sh
.venv/bin/workspool --workspace "$HOME/Workspool" host-config claude-desktop --install
```

Use `cursor` or `codex` in place of `claude-desktop` for those hosts. Omit `--install` to preview the configuration. Installation preserves other server entries and makes a backup before changing an existing file. Conflicting Workspool entries require an explicit decision; Codex TOML conflicts are left for manual editing.

Each conversation can use `recall` with its current objective. Optionally add `--goal "Continue the spending review"` for context selected when the server starts; a shared host configuration does not need a fixed conversation goal.

Check the generated launch command through a disposable MCP session:

```sh
.venv/bin/workspool host-doctor claude-desktop \
  --goal "Continue the spending review and recover prior decisions"
```

Then fully restart the host. The [host walkthrough](docs/HOSTS.md) covers where to find the tools, a two-session continuity check, and troubleshooting. The doctor checks the generated command and MCP exchange; it does not establish that a particular host forwards startup context to its model.

### Manual configuration

Use stdio with a local MCP host. Give each host the same absolute workspace path when they should share the work.

```json
{
  "mcpServers": {
    "workspool": {
      "command": "/absolute/path/to/workspool/.venv/bin/workspool",
      "args": [
        "--workspace", "/absolute/path/to/MyWorkbook",
        "serve", "--profile", "runtime",
        "--agent-id", "research-agent", "--role", "writer",
        "--goal", "Continue the spending review and recover prior decisions",
        "--tokenizer", "o200k_base", "--recall-tokens", "1500",
        "--model-dir", "/absolute/path/to/.cache/workspool/bge-small-en-v1.5"
      ]
    }
  }
}
```

Point the command at the environment `uv sync` created; from the project directory, `printf '%s\n' "$PWD/.venv/bin/workspool"` prints it.

The workbook defaults to `~/.agent-workbook/default`. Set `WORKSPOOL_HOME` or pass `--workspace` to choose another directory (`AGENT_WORKBOOK_HOME` is still read). Separate workspaces keep unrelated projects apart; checkpoints inside a workspace can be grouped with names such as `pricing/handoff`.

Workspool speaks MCP and runs independently of the model. Any host that supports MCP tool calls works, with a local MLX model, another local runtime, or a hosted model. The host decides which tool responses it forwards to its model.

For capture and startup recall, use the `runtime` profile:

```sh
.venv/bin/workspool --workspace "$HOME/MyWorkbook" serve \
  --profile runtime --agent-id research-agent --role writer \
  --goal "Continue the spending review and recover prior decisions" \
  --tokenizer o200k_base --recall-tokens 1500 \
  --model-dir "$HOME/.cache/workspool/bge-small-en-v1.5"
```

It exposes storage, SQL, durable tasks, recall, session memory, claims, and provenance through one lazy `workspool` dispatcher, filtered by role. Adaptive text replies carry one copy of the result. The added startup context section, including its label and metadata, must fit the configured 1,500-token budget; ordinary server instructions and tool schemas are separate. The host must support the dispatcher, text response blocks, and initialization instructions.

The `agent` profile retains durable tasks and indexed keyword context without automatic session capture. Use the [runtime guide](benchmarks/RUNTIME.md) to connect external host events or configure separate reader, writer, and approver agents.

Starting `serve` without a profile still selects `compatible`: six eager tools, compact JSON, and both text and structured replies. `efficient` adds retrieval and revision operations with lazy discovery and adaptive replies, but does not add task operations and retains both response copies. `--tools`, `--tool-discovery`, `--response-format`, and `--response-content` override a profile. `import` and `delete` always require an explicit allowlist.

## Define a task, then run or reopen it

This example keeps a spending report and its review policy together. Create and import the sample inputs from the project directory:

```sh
mkdir -p demo-input
printf 'category,amount\nHosting,40.25\nHosting,9.75\nTools,15.00\n' > demo-input/spending.csv
printf 'Review categories with total spending of at least 50.00 before renewing.\n' > demo-input/policy.txt
uv run workspool --workspace ~/MyWorkbook import demo-input/spending.csv --name demo/spending
uv run workspool --workspace ~/MyWorkbook import demo-input/policy.txt --name demo/policy
```

With the `agent` profile, call the `workspool` tool with this JSON to save the plan:

```json
{
  "tool": "task_define",
  "arguments": {
    "name": "spending-review",
    "objective": "Total spending by category, flag categories requiring review, and cite the policy.",
    "sources": {"spending": "demo/spending", "policy": "demo/policy"},
    "csv_types": {"spending": {"amount": "DECIMAL(18,2)"}},
    "steps": [
      {
        "name": "totals",
        "inputs": ["spending"],
        "sql": "SELECT category, sum(amount) AS total FROM input1 GROUP BY category ORDER BY category"
      },
      {
        "name": "review",
        "inputs": ["totals"],
        "sql": "SELECT category, total FROM input1 WHERE total >= ? ORDER BY total DESC, category",
        "parameters": [50]
      }
    ],
    "searches": ["review"]
  }
}
```

Each step's ordered `inputs` become SQL tables `input1`, `input2`, and so on. Later steps can consume earlier results. Explicit CSV types preserve the intended decimal precision; imported Parquet already supplies its schema. Updating the definition requires its current `expected_version`.

By default, `task_define` resolves each source name or artifact ID to its stable name. Future runs follow that name's latest version, even when the definition originally received an immutable ID. Set `"pin_sources": true` in the definition arguments to keep exactly the versions resolved at definition time across future runs; updating those inputs then requires a revised definition. Every run still pins one coherent snapshot before executing its steps.

If you provide evidence `searches`, include at least one document-text source in `sources`, as this example does with `policy`. A definition containing searches but only table sources is rejected with an actionable error; CSV or Parquet tables alone cannot supply document citations.

Run the task:

```json
{"tool":"task_run","arguments":{"name":"spending-review"}}
```

The brief includes the source versions, both step results, materialized artifact IDs, and a policy citation. Here `Hosting` totals `50.00` and requires review. Repeating the run reuses eligible results. After importing a changed source version, running again reconciles the plan against the new snapshot and recomputes affected work. A source update during execution cannot silently mix old and new inputs; check `current`, `state`, and `omitted` before using the result.

In a fresh session, reopen the brief without executing SQL:

```json
{"tool":"task_status","arguments":{"name":"spending-review"}}
```

The default is the newest attempt; `latest_success` points to the last successful publication. Supply `run_id` to reopen a particular attempt. To retry an interrupted or partial attempt with its original definition and source versions, call `task_run` with `resume_from` set to that attempt's returned ID. This preserves the historical snapshot even when the plan normally follows latest sources. Use `force: true` to recompute results deliberately. The corresponding terminal commands are `task-run spending-review` and `task-status spending-review`.

### Turn a successful task into a reusable skill

Keep the exact calculation plan after it succeeds:

```json
{"tool":"skill_distill","arguments":{"name":"spending-review","task":"spending-review"}}
```

The `runtime` profile exposes skill tools. Later, another session can discover the skill with `skill_find`, inspect its SQL and source requirements with `skill_inspect`, and apply it to new inputs:

```json
{"tool":"skill_find","arguments":{"query":"spending"}}
```

```json
{"tool":"skill_use","arguments":{"ref":"spending-review","task_name":"next-spending-review","bindings":{"spending":"demo/next-spending","policy":"demo/next-policy"}}}
```

Import those new sources first. Applying the skill checks their media types and exact ordered table schemas, then defines a task pinned to those validated versions. It retains the skill version and inputs for later inspection. Run it with `task_run` using `"name":"next-spending-review"`; execution still obeys the workspace's claim policy. Optional parameter overrides let a caller change existing SQL placeholders without rewriting the steps.

Skills retain successful execution provenance; they do not certify the data's truth or that a calculation suits a new business question. Distillation preserves the actual plan, without an external model call. [Skill guide and limits](docs/SKILLS.md) · [Measured public-data replay](docs/SKILL_REPLAY.md).

In the January-to-February taxi replay, applying a known skill used **1,081 request-and-response tokens versus 1,161 for resending the known plan: 80 tokens saved (6.9%)**. An existing recurring task was cheaper at 857 tokens. Distillation and added setup cost 278 tokens, reaching break-even after four similar transfers; fresh discovery and inspection add more. These are scripted MCP payload counts, with exact results checked over **7,052,769 real rows**, not provider billing or a general accuracy claim. [Full comparison](docs/SKILL_REPLAY.md).

### Reuse a brief the agent still retains

Only when the caller retains the **unabridged brief** of a prior run may it supply that returned ID as `base_run`:

```json
{"tool":"task_run","arguments":{"name":"spending-review","base_run":"run_returned_by_your_previous_call"}}
```

If the definition, sources, results, citations, and omissions match a complete current run, the response may be `delivery: "unchanged"` with `requires_retained_base: true`. It identifies the base and supplies current run status; it does not repeat the evidence. A changed or unusable base falls back to a full brief. `task_status` accepts the same option. Omit `base_run` after context loss, compaction, or a handoff to an agent that lacks that brief. Omissions remain explicit, and `brief_complete: false` must never be read as complete evidence coverage.

### Rank supporting passages

```json
{"tool":"context","arguments":{"query":"review spending","refs":["demo/policy"],"limit":4}}
```

`context` uses a persistent full-source index to rank matching chunks. Citations contain immutable source IDs, exact UTF-8 byte ranges, and source text. It is keyword ranking, not a semantic model or an exhaustive answer: `index_complete` describes indexing coverage, while `complete`, `truncated`, and pending versions disclose missing results. Use the literal `search` operation below when you need its exhaustive continuation contract. Source excerpts remain untrusted data.

The [task reference](benchmarks/TASKS.md) covers cache eligibility, concurrency, interrupted runs, source reconciliation, resource bounds, and evaluation methods.

## Configure tools and replies

### Choose the tools each host gets

The compatibility default exposes six tools. The `agent` profile adds task and ranked-evidence operations as well as all five retrieval and revision operations; `runtime` adds continuity, hybrid recall, and coordination. `efficient` enables the five retrieval and revision operations alone. All can also be selected individually, subject to role permissions. Two operations require an explicit allowlist because they read or remove local files:

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

Callers that already know the arguments can execute directly. Omitted or null `arguments` requests a schema; `arguments: {}` executes the operation with its defaults. `--tools` still controls the allowlist, including opt-in `import` and `delete`. Original argument validation and version checks apply. Lazy mode changes the calling interface, so use it with clients that can call the dispatcher; the compatible profile keeps eager tools. Discovering many schemas can cost more than advertising them up front.

### Keep MCP overhead small

The compatible profile uses compact JSON. Its artifact metadata keeps IDs, names, versions, freshness flags, dependencies, and requested excerpts. The compact form of `inspect` omits the summary, source label, hash, timestamp, size, kind, media type, and previous version ID; request `inspect` with `full: true` for all of them, or start with `serve --response-format full` to get full metadata in every reply. `find` and `resume` keep summaries, since that is where you decide what to open. Task briefs retain their provenance fields and disclose any budget omissions. CLI output uses the full metadata representation.

Read related artifacts in one call:

```json
{"ref": ["project/decision", "project/evidence"], "excerpt_bytes": 1000}
```

`inspect` accepts up to 16 distinct references in one catalog snapshot. Each excerpt is capped at 8 KiB; follow each returned `next_offset` to read the next page.

For long document reads, text format avoids quoting and escaping the excerpt inside model-facing JSON. A host that reads text responses can also request a single copy:

```sh
uv run workspool --workspace ~/MyWorkbook serve --tools inspect --response-format text --response-content text
```

In text format, `content[0].text` contains compact JSON metadata. Each artifact's `excerpt_block` is the zero-based index of the content block containing its exact raw excerpt; batches preserve the `artifacts` array. Full metadata and pagination work as before. With `--response-content both` (the compatible/efficient profile default), `structuredContent` retains the original object with string `excerpt` fields. Choose `text` to omit that structured copy, or `structured` to omit successful text replies for hosts that consume structured results. Errors remain readable text. These modes require a compatible host; [the report](benchmarks/EFFICIENCY.md#response-representations) specifies the block layout and measured tradeoffs.

`--response-format adaptive` chooses among compact JSON, raw excerpt blocks, and packed artifact metadata using serialized byte size. It preserves missing fields, nulls, values, and exact excerpts. With `--tokenizer`, it instead chooses the lowest counted serialized result among its lossless representations; without a tokenizer, byte size remains a heuristic. Hosts can reconstruct the original object with [`agent_workbook.render.decode`](src/agent_workbook/render.py); `structuredContent` stays unchanged when present. The [workflow report](benchmarks/WORKFLOWS.md#adaptive-replies) describes the versioned envelopes.

### Retrieve the evidence you need

Under the agent or efficient profile, call `workspool` with a literal source search:

```json
{"tool":"search","arguments":{"text":"release code","context_bytes":128}}
```

Search covers current text artifacts, including content beyond the 64 KiB catalog index. Supply `refs` to search up to 16 selected versions. Hits include exact snippets, pinned IDs, freshness, and UTF-8 byte ranges. Follow `cursor` until `complete` is true; an empty page with a cursor does not mean no match. Search is case-insensitive literal matching, not arbitrary regular expressions, and rejects malformed UTF-8. Each call reads at most 16 MiB and bounds hit objects to 32 KiB, excluding small cursor/envelope metadata; `limit` is 1–20 and `context_bytes` is 0–2,048 per side. Continuations reject a changed query or relevant snapshot.

For an unfamiliar table, `describe` returns exact column names and SQL types without sample rows:

```json
{"tool":"describe","arguments":{"inputs":["replay/bars"]}}
```

Follow each table's `next_offset` to verify its complete schema. Use its returned artifact ID in subsequent queries to keep the same version. See the [schema discovery reference](benchmarks/WORKFLOWS.md#table-schema-discovery) for pagination and limits.

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

The limits are 8 queries, 16 reads, 32,768 bytes per requested read, and 16 MiB of total scan/read work per call, including repeated search windows. The default `max_bytes` is 32,768 and caps the complete result JSON. Use `inspect` for a straightforward read and `retrieve` when you need to combine independent ranges or searches.

### Set a measured output-token budget

Install the optional tokenizer dependencies and configure the encoding explicitly:

```sh
uv sync --locked --extra tokens
uv run --locked --extra tokens workspool --workspace ~/MyWorkbook serve \
  --profile agent --tokenizer cl100k_base
```

Then include `"max_tokens": 800` in a `task_run`, `task_status`, `context`, `sync`, or `retrieve` call. Supported encodings are `cl100k_base`, `o200k_base`, or a path to a local `tokenizer.json`; Workspool does not guess an encoding from a model name. Both named vocabularies ship with Workspool and work offline, including on first use. Their [upstream provenance and MIT notice](src/agent_workbook/_tokenizers/README.md) are included in the package.

The limit counts the exact compact JSON serialization of the **complete successful MCP result**, including both text and structured content when enabled. It excludes JSON-RPC framing, provider message formatting, and error replies, so it is not a billing cap. Limits are 1–131,072 tokens. Requests without a configured tokenizer, or budgets too small for required metadata, fail explicitly. Task briefs may omit whole result previews or citations and report those omissions; `context` may return fewer complete chunks. The matching CLI commands also support `--tokenizer` and `--max-tokens`, counting their actual printed JSON and final newline instead.

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

Saved artifacts are not removed on their own. The query reuse cache can evict entries; promoted task results and source/run records remain durable artifacts. Deletion refuses versions another artifact still pins and names the blockers. Pruning skips pinned versions.

- `delete <wb_id>` removes one superseded version. `delete <name> --expected-version N` removes every version of a name, with the same version check an update makes.
- `prune` drops superseded versions nothing pins, keeping the newest `--keep` versions of each name (default 1). It runs to a fixed point, so an old result and the old input it pinned go together. `prune --dry-run` reports what would go.

Runtime capture history has a separate **30-day retention policy**. Writer startup and `prune` collect expired inactive-session episodes, unreferenced observations, and disposable events in bounded passes. `--episode-retention DAYS` on `serve` or `prune` changes the persisted policy; `0` disables expiry. A dry run reports the proposed effect without changing policy. Active references preserve their evidence, including a skill's origin proof while that skill or a replay task still pins it. Aggregate usage and small version/claim identity records remain. Direct deletion of managed episodes gives an actionable error; use retention cleanup. See the [retention guide](benchmarks/RUNTIME.md#history-retention) for limits and reporting.

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
- **Large integers stay exact.** Query previews return integers outside JavaScript's safe range as decimal strings and list the affected columns in `integer_string_columns`. Materialization stores DuckDB `HUGEINT`/`UHUGEINT` columns as checked `DECIMAL(38,0)` and reports the conversion; values outside that range fail instead of rounding through floating point. Pass a large integer back through a JSON client as a string with an explicit SQL cast.

## MCP tools

| Tool | What it does |
| --- | --- |
| `save` | Save a note, code, document text, inline CSV, or checkpoint. Inline content is capped at 1 MiB; use `import` for larger or native files. |
| `find` | Search current names, summaries, source labels, and the indexed start of each text artifact (64 KiB by default). |
| `inspect` | Read versions, freshness, dependencies, and optional excerpts for one or up to 16 references. `full: true` returns complete metadata. |
| `resume` | Open a checkpoint and preview every artifact it pins. |
| `query` | Run one SELECT over selected CSV or Parquet artifacts as `input1`, `input2`, and so on. Optionally save the full result as Parquet. |
| `refresh` | Rerun a saved query using the current versions of its direct inputs. |
| `task_define` | Agent profile or explicit allowlist. Save sources, SQL or configured-worker steps, scalar conditions, approval gates, and evidence searches. Sources follow latest versions by default; `pin_sources: true` fixes the resolved versions. |
| `task_run` | Agent profile or explicit allowlist. Pin a source snapshot, reuse eligible materialized results, and return results with exact evidence and status. |
| `task_status` | Agent profile or explicit allowlist. Reopen a persisted attempt without recomputing; default replies are self-contained. |
| `context` | Agent profile or explicit allowlist. Rank exact document chunks through a persistent full-source index, reporting coverage and omitted matches. |
| `recall` | Runtime profile or explicit allowlist. Retrieve relevant source passages and published observations within a budget, using keyword and optional local semantic search. Episode ledgers are excluded. |
| `remember` | Runtime writer. Record an unverified decision or observation with an idempotent event ID. |
| `session_status` | Runtime profile. Inspect capture health, usage counters, and the session-retention policy. |
| `claim` | Runtime writer. Acquire an atomic, expiring claim with a fencing ticket for coordinated publication. |
| `claim_renew` | Runtime writer. Extend a claim while its owner and ticket remain valid. |
| `claim_release` | Runtime writer. Release a claim using its current ticket. |
| `claim_owner` | Runtime profile. Inspect current ownership and expiry. |
| `provenance` | Runtime profile. Explain source dependencies, producer events, and why evidence is stale. |
| `changes` | Runtime profile. Page through actor-attributed events with explicit history-expiry markers. |
| `approve` | Runtime approver. Approve an exact current artifact or revoke its attestation. Task gates bind exact inputs and worker identity. |
| `verify` | Runtime profile. Report exact changed inputs, affected published tasks, current approvals, and suggested reruns with bounded pagination. |
| `metrics` | Runtime profile. Read observed latency, failures, step-result reuse, recall coverage, and signed same-content formatting token reductions. Missing coverage is explicit. |
| `task_schedule` | Runtime profile. List or configure interval and input-change triggers. An explicit local runner executes due tasks; updates require the current version. |
| `skill_distill` | Runtime writer. Retain a reusable SQL plan and source contracts from a successful published task. |
| `skill_find` | Runtime profile. Find reusable analytical plans by name or description. |
| `skill_inspect` | Runtime profile. Inspect a skill's plan, contracts, and origin proof. |
| `skill_use` | Runtime writer. Validate new source bindings and define a task from a skill; `task_run` executes it. |
| `search` | Agent/efficient profile or explicit allowlist. Find exact source passages beyond the catalog index, with bounded scanning and continuations. |
| `describe` | Agent/efficient profile or explicit allowlist. Page through exact table column names and SQL types without returning rows. |
| `diff` | Agent/efficient profile or explicit allowlist. Return exact edits against a complete immutable base the caller retains, or request a full read. |
| `sync` | Agent/efficient profile or explicit allowlist. Return unchanged status, exact edits, or a full page of the current source in one call. |
| `retrieve` | Agent/efficient profile or explicit allowlist. Combine up to 8 literal searches and 16 byte-range reads, merging overlapping spans. |
| `import` | Opt-in. Copy a local file by absolute path in its native format: CSV, Parquet, PDF, images, anything. |
| `delete` | Opt-in. Delete a superseded version by ID, or every version of a name with its `expected_version`. |

## Limits

The defaults keep a query predictable on a laptop; raise them when the machine and the task call for it. All are flags before the subcommand and apply to the CLI and the server alike.

| Flag | Default | Bounds |
| --- | ---: | --- |
| `--query-timeout` | 15 s | a running SELECT is interrupted after this |
| `--query-memory` | 1GB | DuckDB memory limit |
| `--query-threads` | 2 | DuckDB threads per query |
| `--max-preview-rows` | 25 | most rows an ordinary query reply previews; the preview is also capped at about 7.5 KB; task steps preview 5 rows |
| `--index-bytes` | 65536 | bytes of each text artifact indexed for `find` |

The `find` indexing limit applies when saving a new version. Existing versions keep their earlier catalog index; import a new version to index more of an existing source. The persistent full-source index used by `context` and task evidence has separate bounded build/query work and reports pending versions.

Fixed bounds include names up to 120 characters, summaries and source labels up to 360, at most 16 dependencies per artifact, at most 8 inputs and 6,000 characters of SQL per query, and 8 KiB per `inspect` excerpt page. The existing `query`/`refresh` path serializes queries within its server process. Task steps use separate bounded workers and coordinate same-key cache computation across processes.

A task supports up to 8 source aliases, 8 steps, and 8 evidence searches. SQL steps execute sequentially, with at most 1 million materialized rows and 64 MiB per cached result. The reuse cache defaults to 256 MiB and 128 entries, with leased reservations and eviction. Registered workers have explicit input/output/row/deadline limits and run with the local account's permissions. These are per-step/cache limits: they are not a whole-run deadline, a process RSS cap, or a total workbook storage quota. Durable task results and source/run anchors remain pinned after cache eviction. See [task limits and recovery](benchmarks/TASKS.md) and [worker/trigger behavior](docs/RECURRING_WORK.md); `sync` and `retrieve` limits are documented above.

## Try a local example

Run a self-contained example that queries 2,000 rows and compares the size of the returned answer with the full source:

```sh
uv run workspool bench
```

To count tokens with a local model's tokenizer:

```sh
uv sync --locked --extra benchmark
uv run --locked --extra benchmark workspool bench --tokenizer /path/to/model/tokenizer.json
```

Detailed measurements, evaluation results, and reproduction instructions are available in [`benchmarks/`](benchmarks/).

## How it runs

SQLite stores the artifact catalog, checkpoints, dependency links, and task ledger. Separate local databases hold the reusable query cache and ranked text index. File contents live under their SHA-256 hashes in the workspace directory.

Ordinary `query` calls use a restricted in-memory DuckDB connection. SQL task steps execute in bounded DuckDB worker processes, materialize exact results, and promote them into versioned artifacts. Cache keys bind the exact SQL, typed parameters, ordered immutable inputs, reader types, engine version, and relevant settings. Only conservatively classified deterministic queries qualify for reuse; other accepted queries still execute but are not reused. Cross-process leases coordinate eligible computations and allow recovery after an owner dies. Configured external workers use hash-verified scratch inputs and a typed result protocol; fresh runs execute them again, while explicit resume can reuse validated pinned materializations.

Each task run pins its definition and sources before executing steps. Publication checks freshness and generation order, so an older finishing run cannot replace a newer successful brief. Interrupted attempts retain completed progress; explicitly resume their snapshot to continue. Immutable run artifacts describe computation snapshots and direct readers to `task_status` for publication and freshness status.

The server is designed for trusted local workspaces and files. Give each host access to the workspace it needs, and the tools it needs. Checkpoints are saved explicitly, and freshness follows the versions recorded in the workbook.

## Development

The baseline harness test needs the v0.4.1 tag. If you used the shallow quick-start clone, fetch it before testing:

```sh
git fetch origin tag v0.4.1
```

```sh
uv sync --locked --extra benchmark --extra tokens
uv run --locked --extra benchmark --extra tokens pytest -q
uv run --no-sync ruff check src tests benchmarks
uv run --no-sync ruff format --check src tests
```

CI runs the suite on Linux and macOS with Python 3.11 to 3.13, and once more with the minimum supported MCP SDK. Coverage includes independent-session handoffs, stale results and refresh, deletion and pruning with pinned versions, the opt-in tools, HTTP authentication, exact large integers, batched Unicode reads, complete metadata access, restricted tool registration, current-version synchronization, merged retrieval and pagination, exact serialized token limits, task snapshot races, cache process death and singleflight, typed CSV materialization, and explicit evidence omissions.

To publish a new GitHub release, update the version in `pyproject.toml` and `uv.lock`, add its changelog entry, and push to `main`. The release workflow runs when the package metadata or release workflow changes, and also supports manual runs on `main`. It tests the package, verifies the license in both archives, and publishes a new version tag with a wheel, source distribution, and checksums. It refuses to replace an existing tag or release. It does not publish to PyPI.

If you find a bug, include the command or tool arguments, the result you expected, and a small input that reproduces it.

## Licensing

Starting with **v0.4.0**, Workspool is distributed under the [MIT License with Commons Clause License Condition v1.0](LICENSE). These are combined terms, not a choice between two licenses. The package metadata identifies them as `LicenseRef-MIT-Commons-Clause`. Workspool is source-available; this combined license is not an OSI-approved open-source license.

You may use Workspool personally or internally at a business, including to save money on tokens, and modify or redistribute it subject to the combined license. The license does not grant permission to **Sell** the software. In the Commons Clause, this includes providing a product or service to others for a fee or other consideration when its value derives entirely or substantially from Workspool's functionality. Paid hosting, consulting, or support can fall within that restriction. It is not a blanket prohibition on every commercial product that incorporates Workspool. The [full license](LICENSE) controls; the [Commons Clause FAQ](https://commonsclause.com/) provides further explanation.

Versions 0.3.0 and earlier, and repository revisions published before this license change, were offered under MIT. Their existing license grants are not revoked by this change. The restriction starts with this revision and the v0.4.0 release; it does not retroactively change earlier copies. Third-party dependencies retain their own licenses.
