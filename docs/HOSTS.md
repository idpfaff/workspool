# Connect Claude Desktop, Cursor, or Codex

Workspool can generate and install a local MCP configuration for these hosts on macOS and Linux. The configuration starts the `runtime` profile with eager tool schemas, automatic capture of Workspool tool activity, and a distinct host identity. Hosts see and call tools such as `recall` and `task_run` directly. Each conversation calls `recall` with its current objective. An optional `--goal` provides a default query for that explicit tool call. Initialization instructions contain static guidance, never retrieved artifact or observation text. You can use a standard Python virtual environment; `uv` is optional.

## Install Workspool once

The v0.10.0 wheel links below target the unreleased candidate and become available after its release gates pass.

Use Python 3.11 or later. Install the versioned release wheel into an environment you intend to keep:

```bash
python3 -m venv "$HOME/.local/share/workspool/venv"
"$HOME/.local/share/workspool/venv/bin/python" -m pip install \
  'agent-workbook[tokens] @ https://github.com/idpfaff/workspool/releases/download/v0.10.0/agent_workbook-0.10.0-py3-none-any.whl'

workspool_python="$HOME/.local/share/workspool/venv/bin/python"
workspool_workspace="$HOME/.local/share/workspool/workspace"
```

The generated config records the absolute path to this Python interpreter and runs `-m agent_workbook.cli`. It does not depend on shell activation, your working directory, or a globally installed `workspool` command. Keep the environment at that path; regenerate the config if you move it. Updating the package inside the same environment preserves the configured command.

All profiles now use eager schemas by default. If an existing command contains `--tool-discovery eager`, `--tool-discovery lazy`, or either `--tool-discovery=...` form, remove that option and its value; the old flag produces a migration error. The separate `serve --experimental-lazy` switch is reserved for explicit experiments and is never emitted by `host-config` or used by `host-doctor`. The [paired model evaluation](../benchmarks/APPROVAL_MODEL.md) found substantially lower completion with lazy discovery under its six-round limit.

## Preview, then connect

Preview prints configuration to stdout and does not create the workspace or change host settings:

```bash
"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config claude-desktop
```

Use `cursor` or `codex` in place of `claude-desktop`. The default tokenizer is `o200k_base`. Both named vocabularies ship in the package and load offline, including on first use after dependencies are installed. Choose `--tokenizer cl100k_base` or an absolute local `tokenizer.json` path when appropriate. If you supply a `--goal`, an explicit `recall` call that omits its query uses that goal and a default 1,500-token reply budget in that encoding; this is not a provider billing measurement.

To install, run the command for your host:

```bash
"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config claude-desktop --install

"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config cursor --install

"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config codex --install
```

`--install` first runs a disposable MCP protocol check, then merges the configuration. A failed check leaves host settings untouched. The check starts local Workspool and does not call a model API or download tokenizer data. Restart the host after installation and check its MCP server list for `workspool`.

For a dedicated research workspace, you can add `--goal 'Continue my customer research and recover relevant evidence'` to preview and install. This sets a default for explicit recall, not a setting that changes with each chat or a request to inject source text at startup. For general-purpose installations, leave it out and ask the agent to call `recall` with the current conversation's question. Different conversations can supply different queries without changing shared server state.

The installed identities are `host-claude-desktop`, `host-cursor`, and `host-codex`, with the `writer` role. Override the identity with `--agent-id NAME` when separate instances need distinct audit attribution. Host approval settings are left unchanged. Sharing a workspace shares its stored artifacts; use separate workspace directories across trust boundaries.

## Open the tools in your host

After installing the configuration, use the short walkthrough for your host:

- **Claude Desktop:** fully quit and reopen the app. Open Settings → Developer to inspect the local server connection. In a conversation, use the connectors menu to locate Workspool and its available tools. The [official local-server walkthrough](https://modelcontextprotocol.io/docs/develop/connect-local-servers) shows these screens.
- **Cursor:** restart Cursor, open Customize → MCPs, and check that Workspool is enabled. Open an Agent conversation and ask it to use Workspool. Connection errors appear in the Output panel's MCP Logs channel. See [Cursor's MCP help](https://prod.cursor.com/help/customization/mcp).
- **Codex:** run `codex mcp list`, then start a new `codex` session and use `/mcp` to inspect active servers. Ask it to use the Workspool tools. The [official MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) covers CLI and IDE connections.

Try this two-session check:

1. Ask the agent: “Use Workspool to remember this customer research decision: the fictional onboarding pilot starts with 50 accounts. Show me the saved reference.” Confirm that it actually calls `remember`, rather than only acknowledging the request.
2. Open a fresh conversation using the same workspace. Ask: “Use Workspool recall to find the customer onboarding pilot decision. Include its source reference.” This uses the current question rather than a fixed installation goal.
3. To check a configured default, set the customer-research `--goal`, restart the Workspool server, and ask: “Call Workspool recall without a query, then answer from its returned evidence: what was the onboarding pilot decision? Include its source reference.” Source text is returned through the tool response, not server instructions.

Opening another chat does not necessarily restart the MCP server. Retrieve the saved observation with an explicit `recall` call in each conversation. This exercise checks your host's actual behavior; it does not turn the fictional decision into a verified fact.

## Configuration locations and preservation

| Host | Default destination | Format |
| --- | --- | --- |
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | `mcpServers.workspool` JSON; [official setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | Same JSON format; [official setup](https://modelcontextprotocol.io/docs/develop/connect-local-servers) |
| Cursor | `~/.cursor/mcp.json` | `mcpServers.workspool` JSON; [official locations](https://prod.cursor.com/help/customization/mcp) |
| Codex | `$CODEX_HOME/config.toml`, default `~/.codex/config.toml` | `[mcp_servers.workspool]` TOML; [MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [state directory](https://learn.chatgpt.com/docs/config-file/config-advanced) |

For Cursor project scope, add `--project /absolute/project/path`; this selects `.cursor/mcp.json` in that project. Cursor gives project entries precedence over global entries with the same name. Use `--config-path /absolute/config/file` for another location. `--project` and `--config-path` are mutually exclusive.

JSON installation preserves unrelated values and server entries, with normalized indentation. It refuses comments, duplicate keys, malformed data, and conflicting Workspool entries. After reviewing a conflict, `--replace --install` replaces only that JSON entry. Identical entries are a no-op.

Codex installation appends the Workspool table while preserving existing text and comments. It sets `startup_timeout_sec = 60` to allow cold startup. A conflicting existing table must be edited manually: `--replace` does **not** rewrite TOML. Unextendable inline TOML tables are also refused. Preview the new table and transfer its values into the existing table, keeping unrelated settings.

Before changing an existing file, the installer creates an exact backup beside it, named `<config>.workspool-<unique-id>.bak`, with private permissions. New files are private. Publication is atomic, symlink paths and hard-linked config files are refused, cooperating installers use a private lock file, and changes observed during the merge cause an abort. Close settings editors while installing; this does not lock out unrelated programs that ignore the lock. Backups can contain your other server settings, so keep them private. To undo an installation, close the host and restore the reported backup; for a newly created config, remove the Workspool entry manually.

## Check a launcher without installing it

```bash
"$workspool_python" -m agent_workbook.cli host-doctor codex \
  --goal 'Continue my customer research and recover relevant evidence' \
  --tokenizer o200k_base --recall-tokens 1500 --timeout 60
```

The doctor parses the generated host fragment and launches its command in a disposable workspace. It performs real MCP initialization, checks that runtime tools are individually listed, calls `session_status` directly, and checks the capture session and configured identity. It checks that the old injected-context section is absent. With `--goal`, it also calls `recall` directly without a query and verifies that the disposable fixture is returned. Success returns JSON; failure exits nonzero with troubleshooting guidance. It does not read or write personal host configurations, use your real workspace, or forward API keys to its child process.

These are **protocol smoke tests**, including transport round trips for all three generated formats. They do not prove a particular Claude Desktop, Cursor, or Codex application session connected or selected Workspool tools correctly. Confirm those behaviors in your actual host by asking its agent to call `recall` explicitly.

Both named tokenizers also passed actual MCP doctor checks in fresh processes with an empty cache and network connections blocked. The real-host conversation check below is separate from those protocol checks.

If startup fails, check that the recorded interpreter still exists and that its environment includes the `tokens` extra. Verify a custom local tokenizer path if supplied; bundled named encodings need no online provisioning. A very small recall budget can fail the doctor's explicit recall check; increase `--recall-tokens`. Use a larger doctor timeout for cold startup. On Codex, the generated startup allowance is 60 seconds; adjust it manually if your machine needs longer.

## Verified model delivery: Codex CLI

**Historical experiment; this transport has been removed.** Current Workspool returns recalled source material only through explicit tool calls, never through server instructions. The evidence below describes v0.8.1 behavior and does not validate the current recall path.

On 2026-11-07, using **Workspool v0.8.1**, **Codex CLI 0.167.0 returned a fresh random phrase and its exact immutable artifact ID from Workspool's MCP initialization instructions**. The phrase and ID were absent from the prompt. The disconnected fresh-conversation control returned `NOT RECEIVED`. Both traces contain zero non-message items, and the connected Workspool writer recorded zero tool-execution episodes.

The test used `host-config`'s generated runtime configuration, default lazy tool discovery, bundled `o200k_base`, and the CLI's existing authenticated account with no model override. It supplied the configuration for that invocation only; saved settings were unchanged. The prompt explicitly permitted metadata discovery while forbidding Workspool tool execution and file reads:

> Discover Workspool's tool schema if needed, then return the confirmation phrase and source artifact ID from its MCP startup instructions. Do not execute Workspool tools, read files, or use other services. If the instructions do not contain it output NOT RECEIVED.

**Delivery was deferred in this historical test.** Earlier probes that prohibited all tool discovery returned `NOT RECEIVED`, even though Workspool initialized successfully. Allowing Codex to discover the server metadata exposed the full context. This proved instruction delivery for that version and CLI path. The [official Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) describes initialization instructions as server guidance.

Inspect the [exact connected trace](evidence/codex-startup-connected.jsonl), [disconnected trace](evidence/codex-startup-disconnected.jsonl), and [result with runtime fingerprints](evidence/codex-startup-result.json). A [readable transcript](evidence/codex-startup-transcript.html) renders those results. Screenshot capture was blocked by the computer-use policy for Terminal and local files; no screenshot or simulated application UI is presented as proof.

To reproduce the historical experiment, use a separate checkout of **v0.8.1** and its dependencies. The following harness uses your authenticated Codex CLI's model allowance; it is not a current-version readiness check:

```sh
.venv/bin/python benchmarks/host_delivery.py --output /absolute/new/proof-directory
```

Claude Desktop and Cursor model delivery remain unverified. Their generated configurations have protocol coverage only. For ordinary conversations in any host, ask the agent to use `recall` with the current question; optional `--goal` supplies a default query only when that explicit call omits its query.

## Optional local semantic recall

Without a configured embedding model, recall uses lexical search and reports dense retrieval as unavailable. To enable local semantic retrieval, install the `semantic` extra in the same environment and provision the model explicitly:

```bash
"$workspool_python" -m pip install \
  'agent-workbook[tokens,semantic] @ https://github.com/idpfaff/workspool/releases/download/v0.10.0/agent_workbook-0.10.0-py3-none-any.whl'
"$workspool_python" -m agent_workbook.cli model-download \
  "$HOME/.local/share/workspool/model"
```

Add `--model-dir "$HOME/.local/share/workspool/model"` to both preview/install and doctor commands. Provisioning downloads a local model; startup uses that directory. Generated configs contain no API keys. Review the [runtime guide](../benchmarks/RUNTIME.md) for coverage limits, budgets, roles, and claims.

Automatic capture covers Workspool's own MCP calls. It does not observe the host's other tools or entire conversation. External activity needs explicit [host hooks](../benchmarks/RUNTIME.md); recall needs relevant material already stored in the selected workspace. Retrieved source content remains untrusted evidence, and freshness or missing coverage must still be checked. Captured observations and their episodes are eligible for cleanup after 30 inactive days by default; save durable knowledge as an ordinary document or checkpoint.
