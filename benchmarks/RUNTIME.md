# Runtime continuity, recall, and coordination

Workspool v0.7.0 adds an opt-in `runtime` profile: capture useful observations
from its own MCP calls, recover local context for a startup goal, and coordinate
task publication across agents sharing a workbook. It combines existing SQL
and versioned sources with optional learned local retrieval. It does not watch
other applications or automatically receive another host's conversation.

The earlier [live task evaluation](TASK_MODEL.md) measures v0.6.0 task tools;
its token savings do not measure this runtime. The [public-data stress report](PUBLIC_DATA.md)
retains capable SQL/index controls and losing cases. The separate
[semantic diagnostic](SEMANTIC.md) describes the learned retrieval evaluation;
these checks are not a guarantee of model accuracy or lower provider bills.

## Configure one agent

From the checked-out project, install the optional tokenizer and embedding
dependencies, then explicitly download the supported model:

```sh
uv sync --locked --python 3.12 --extra tokens --extra semantic
.venv/bin/workspool model-download "$HOME/.cache/workspool/bge-small-en-v1.5"
```

Provisioning uses `BAAI/bge-small-en-v1.5` through FastEmbed and writes a manifest
of the downloaded assets' SHA-256 hashes. The destination must not already
exist. Retrieval uses that local copy and a network-disabled embedding worker;
it does not fetch weights or call a paid model API. Named tiktoken encodings
may download their ranks on first use. To prepare that tokenizer before an
offline session:

```sh
.venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'
```

Alternatively, `--tokenizer /absolute/path/tokenizer.json` uses an existing
local tokenizer file. Start the stdio MCP server with a specific goal:

```sh
.venv/bin/workspool --workspace "$HOME/MyWorkbook" serve \
  --profile runtime --agent-id research-agent --role writer \
  --goal "Continue the spending review and recover prior decisions" \
  --tokenizer o200k_base --recall-tokens 1500 \
  --model-dir "$HOME/.cache/workspool/bge-small-en-v1.5"
```

Configure the same command and arguments in the MCP host, as shown in the
[README](../README.md#connect-your-agent). This command waits for the host to
speak MCP; it is not an interactive chat client. Use absolute paths in host
configuration. Each distinct agent should have its own trusted `--agent-id`.

`runtime` uses lazy discovery through the `workspool` tool, adaptive formatting,
and text-only results. Without a profile, `compatible` remains the default.
The earlier `agent` profile keeps tasks and keyword context without automatic
session capture. Profile selection does not bypass an explicit tool allowlist
or role restriction; `import` and `delete` still require `--tools` inclusion.

## Startup context and later recall

`--goal` searches current local sources and appends the selected context to MCP
initialization instructions. `--recall-tokens` defaults to 1,500 and counts the
**entire added section**, including its label and JSON metadata, using the
configured tokenizer. Existing server instructions, advertised tools, other
conversation history, and provider framing are outside this section's budget.
A goal requires `--tokenizer`. An impossible budget returns an error rather
than silently discarding required metadata.

The host must forward MCP initialization instructions to its model. Workspool
cannot make a host inject ignored instructions or recover uncaptured external
history. Startup recall runs when the server starts; use `recall` again when
the goal changes or new work arrives:

```json
{"tool":"recall","arguments":{"query":"Which spending decisions still need supporting evidence?","limit":6,"max_tokens":1500}}
```

Here `max_tokens` counts the complete serialized MCP result, including both
representations if the configuration enables both. It is separate from the
startup-section budget and is not a provider billing measurement. Supply
`refs` to narrow the scope to known artifact names or immutable IDs.

With a provisioned model, recall combines FTS5 keyword ranks with cosine ranks
from learned embeddings using reciprocal rank fusion. Dense candidates come
from the chunk store, including chunks without lexical overlap. Without a
model, or when model validation/execution is unavailable, the response
explicitly reports lexical fallback and its reason. Omitting `--model-dir`
does not silently enable a semantic model.

Inspect `mode`, `index_complete`, `dense_complete`, `complete`, `truncated`,
`pending`, and `dense_coverage`. Defaults bound embedding work to 128 chunks
per call, 20 seconds of embedding work, and a 20,000-vector/five-second scan;
the source index has its own 128 MiB/30-second build limits. Repeated calls can
build more vectors. Scope, output limits, missing vectors, and unfinished
indexing remain visible. Oversized individual sources can remain pending;
the [Wikipedia stress test](PUBLIC_DATA.md#research-context-over-442-mib)
documents that limitation.

Ranking returns exact UTF-8 source ranges and immutable artifact references.
It does not prove that a passage answers the question or that the source is
true. The embedding model accepts at most 512 input tokens, and chunk inputs
can truncate; this is disclosed in model metadata. Coverage flags describe
indexing/scoring and selected results, not exhaustive semantic understanding.
`context` remains keyword-only ranking; `search` remains literal matching with
its own continuation cursor.

## What capture retains

A writer using `runtime` gets a durable session. The configured MCP hook saves
compact observations after executed tools: task state, schema/result previews,
saved artifact references, and failures. It excludes bulk source bodies,
excerpts, SQL literals, parameters, and arbitrary request fields. Captured
previews are bounded and marked; existing immutable references become
dependencies, up to the supported limit. Names alone are not rebound to a
possibly newer source and presented as execution provenance.

Repeated successful reads with the same compact facts are deduplicated when
they carry no supplied token accounting. Failures remain unverified. Successful
internal observations mean the hook observed a tool execution, **not** that
Workspool independently established every claim in its result. External
decisions and tool reports always remain unverified.

Common credential keys and recognizable secret strings are redacted, including
sensitive columns in captured row previews. This is defensive filtering, not a
promise to recognize every secret. Hosts should submit deliberately selected
facts through these hooks. Source content remains untrusted data when recalled.

Inspect the active session and capture failures:

```json
{"tool":"session_status","arguments":{}}
```

The status separates `host_usage_totals`, their reporting-event counts, and
`estimated_rendered_tokens`. With a configured tokenizer, MCP capture supplies
rendered-result token estimates when a result is available. These exclude host
requests, other conversation history, and provider framing. MCP capture does
not observe the host's provider usage automatically; its host-usage totals stay
zero unless an integration supplies them. Zero reporting events is not evidence
of zero model cost. Python hook adapters can provide `usage` with
input/output/cached-input, cache-write, and reasoning token fields, or
`estimated_tokens` separately.
Cached input is part of input, and reasoning is part of output; do not add
those detail fields again. Accounting-bearing calls are not noise-deduplicated.

Record a deliberate observation through the MCP dispatcher:

```json
{"tool":"remember","arguments":{"kind":"decision","data":{"summary":"Inspect the renewal policy before approving hosting spend."},"event_id":"renewal-decision-1"}}
```

The configured identity supplies the actor and role. A caller's `verified`,
`actor`, or `role` fields cannot make an external event verified or privileged.
Reusing an event ID with the same captured content is idempotent; different
facts or accounting under that ID are rejected. Tool retries may differ in
elapsed time while retaining the original recorded duration.

Episode artifacts are saved before their immutable session ledger and audit
entry commit together. A crash in between can leave an unlisted artifact;
retrying the same event repairs publication without creating a second version.
Capture errors are counted and logged without misreporting an already completed
tool operation. No automatic consolidation or claim of lossless conversation
storage is made.

## Wire another host's lifecycle

The CLI hook accepts `session_start`, `decision`, `tool_result`, and
`session_end`. Events other than startup take a JSON object on stdin, limited
to 16 KiB, and require the returned session ID. They are explicit external
reports and remain unverified. The host must invoke them when those events
actually occur.

This runnable local example starts a session, records a decision, and ends it:

```sh
.venv/bin/python - <<'PY'
import json
import subprocess
from pathlib import Path

command = [str(Path('.venv/bin/workspool').resolve()),
           '--workspace', str(Path.home() / 'MyWorkbook'), 'hook']

def hook(kind, data=None, *options):
    result = subprocess.run(
        [*command, kind, '--agent-id', 'editor-host', *options],
        input=json.dumps(data or {}), text=True,
        capture_output=True, check=True,
    )
    return json.loads(result.stdout)

session = hook('session_start', None, '--goal', 'Review evidence before drafting')
identifier = session['session_id']
hook('decision', {'summary': 'Read the source policy before drafting conclusions.'},
     '--session-id', identifier, '--event-id', 'decision-1')
hook('session_end', {'summary': 'Host session ended.'},
     '--session-id', identifier, '--event-id', 'end-1')
print(json.dumps({'session_id': identifier}))
PY
```

For an external tool, pass a deliberately selected object such as
`{"tool":"host_query","arguments":{"ref":"report"},"result":{"columns":["count"],"rows":[[12]]}}`
to `hook tool_result`; replace the illustrative result with the actual host
output. This reports what the host says happened. The hook never executes that
named tool, shell command, or code. The CLI has no event-supplied role switch;
its identity is trusted local launch configuration. Another host's private
conversation, MCP servers, and provider usage remain invisible unless the
integration explicitly passes supported observations.

## Roles and shared-work claims

| Configured role | Permissions |
| --- | --- |
| `reader` | Read and query existing sources; no saved outputs, task execution, capture, or approvals |
| `writer` | Read and write, including task execution, capture, and claims; no approvals |
| `approver` | Read and record approvals; no ordinary writes, task execution, or capture |

Roles filter tool exposure and are enforced at dispatch, including the lazy
dispatcher. A query that saves a result needs write permission. An approval
attests to a particular current artifact and digest; it is refused for stale
inputs or a superseded artifact. It does not independently verify content or
gate all other writes. An approver can call:

```json
{"tool":"approve","arguments":{"ref":"review/final","note":"Reviewed the cited source versions."}}
```

To require leases for task execution and publication, start a trusted writer
once with the additional `--require-claims` flag. This is a **persistent
workspace policy**: later launches without the flag do not turn it off, and
direct Python task runs check it too. There is no MCP operation to disable it.

Acquire a task-specific lease:

```json
{"tool":"claim","arguments":{"resource":"task:spending-review","ttl":300}}
```

Pass the returned ticket object unchanged as `claim_ticket` in `task_run` with
`name: "spending-review"`. The ticket contains `resource`, configured `owner`,
monotonically increasing `fence`, and `expires_at`. Use `claim_renew` with that
ticket before expiry, and `claim_release` when finished. TTL is 1–3,600 seconds.
Even the same owner must renew an existing lease rather than acquire it again.
Without mandatory policy, an active optional claim still prevents ticketless
publication. Tickets are fencing information, not authentication credentials.

Lease validation runs inside the same catalog write transaction as the task's
publication check. An expired, released, wrong-owner, or replaced ticket cannot
advance `latest_success`. Losing a lease during work leaves computed results
available but marks the attempt partial with a `claim_lost` omission. A valid
ticket alone does not make a stale snapshot current.

Claims do not freeze source or definition edits. Version checks and task
supersession handle those changes. Generic resource leases coordinate
cooperating callers; they do not lock arbitrary files or automatically protect
every write operation. Leases use the local host's wall clock. This is
coordination over one shared SQLite workbook, not distributed federation.

## Producer history and stale evidence

`claim_owner` reports ownership and whether a lease remains active. `provenance`
returns an artifact, changed ancestor IDs/versions, and recorded creation and
approval events:

```json
{"tool":"provenance","arguments":{"ref":"review/final"}}
```

`changes` pages the immutable actor-event ledger:

```json
{"tool":"changes","arguments":{"after":0,"limit":50}}
```

Keep the returned `through` fixed while following `next_after` until `complete`.
For the next polling cycle, retain that `next_after` and omit `through` to see
newer events. An optional `resource` filter selects one artifact ID, task
resource, or session resource. Artifact creation is audited atomically with
its catalog insertion; task publication and approval events are also committed
with their corresponding state changes. Older artifacts predating this audit
feature do not acquire invented producer history.

Identity comes from trusted server launch configuration, not a tool argument.
With HTTP, the existing bearer token authorizes one configured server identity;
it does not identify multiple independent users. Use separately configured
servers for distinct identities. Local callers who can edit the database,
launch configuration, or Python process remain trusted. Append-only triggers
are not tamper-proof storage, and role checks are not filesystem isolation.
Provenance and citations help an agent inspect evidence; they do not make
hallucinations impossible.
