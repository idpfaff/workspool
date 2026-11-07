# Connect Claude Desktop, Cursor, or Codex

Workspool can generate and install a local MCP configuration for these hosts on macOS and Linux. The configuration starts the `runtime` profile with a fixed goal, bounded startup recall, automatic capture of Workspool tool activity, and a distinct host identity. You can use a standard Python virtual environment; `uv` is optional.

## Install Workspool once

Use Python 3.11 or later. Install the versioned release wheel into an environment you intend to keep:

```bash
python3 -m venv "$HOME/.local/share/workspool/venv"
"$HOME/.local/share/workspool/venv/bin/python" -m pip install \
  'agent-workbook[tokens] @ https://github.com/idpfaff/workspool/releases/download/v0.8.0/agent_workbook-0.8.0-py3-none-any.whl'

workspool_python="$HOME/.local/share/workspool/venv/bin/python"
workspool_workspace="$HOME/.local/share/workspool/workspace"
```

The generated config records the absolute path to this Python interpreter and runs `-m agent_workbook.cli`. It does not depend on shell activation, your working directory, or a globally installed `workspool` command. Keep the environment at that path; regenerate the config if you move it. Updating the package inside the same environment preserves the configured command.

## Preview, then connect

Preview prints configuration to stdout and does not create the workspace or change host settings:

```bash
"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config claude-desktop --goal 'Continue my customer research and recover relevant evidence'
```

Use `cursor` or `codex` in place of `claude-desktop`. The default tokenizer is `o200k_base`, and the entire added startup-context section has a 1,500-token budget in that encoding. Choose `--tokenizer cl100k_base` or an absolute local `tokenizer.json` path when appropriate; this count is not a provider billing measurement.

To install, run the command for your host:

```bash
"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config claude-desktop --goal 'Continue my customer research and recover relevant evidence' --install

"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config cursor --goal 'Continue my customer research and recover relevant evidence' --install

"$workspool_python" -m agent_workbook.cli --workspace "$workspool_workspace" \
  host-config codex --goal 'Continue my customer research and recover relevant evidence' --install
```

`--install` first runs a disposable MCP protocol check, then merges the configuration. A failed check leaves host settings untouched. The check starts local Workspool and does not call a model API. Named tokenizer data may download on first use. Restart the host after installation and check its MCP server list for `workspool`.

The installed identities are `host-claude-desktop`, `host-cursor`, and `host-codex`, with the `writer` role. Override the identity with `--agent-id NAME` when separate instances need distinct audit attribution. Host approval settings are left unchanged. Sharing a workspace shares its stored artifacts; use separate workspace directories across trust boundaries.

## Open the tools in your host

After installing the configuration, use the short walkthrough for your host:

- **Claude Desktop:** fully quit and reopen the app. Open Settings → Developer to inspect the local server connection. In a conversation, use the connectors menu to locate Workspool and its available tools. The [official local-server walkthrough](https://modelcontextprotocol.io/docs/develop/connect-local-servers) shows these screens.
- **Cursor:** restart Cursor, open Customize → MCPs, and check that Workspool is enabled. Open an Agent conversation and ask it to use Workspool. Connection errors appear in the Output panel's MCP Logs channel. See [Cursor's MCP help](https://prod.cursor.com/help/customization/mcp).
- **Codex:** run `codex mcp list`, then start a new `codex` session and use `/mcp` to inspect active servers. Ask it to use the Workspool tools. The [official MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) covers CLI and IDE connections.

Try this two-session check with the customer-research goal used above:

1. Ask the agent: “Use Workspool to remember this customer research decision: the fictional onboarding pilot starts with 50 accounts. Show me the saved reference.” Confirm that it actually calls `remember`, rather than only acknowledging the request.
2. Fully restart the host or its Workspool MCP server, then open a fresh conversation using the same workspace. Ask: “From your restored Workspool context, what was the onboarding pilot decision? Include its source reference.”
3. If the host did not forward startup context, ask: “Use Workspool recall to find the customer onboarding pilot decision.” That checks explicit retrieval separately.

Startup recall runs when the MCP server starts. Opening another chat does not necessarily restart that server. The saved observation remains available through `recall` either way. This exercise checks your host's actual behavior; it does not turn the fictional decision into a verified fact.

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

The doctor parses the generated host fragment and launches its command in a disposable workspace. It performs real MCP initialization, lists runtime tools, calls `session_status`, and checks the startup-context prefix, capture session, and configured identity. Success returns JSON; failure exits nonzero with troubleshooting guidance. It does not read or write personal host configurations, use your real workspace, or forward API keys to its child process.

These are **protocol smoke tests**, including transport round trips for all three generated formats. They do not prove a particular Claude Desktop, Cursor, or Codex application session connected, forwarded initialization instructions to its model, or selected Workspool tools correctly. Confirm those behaviors in your actual host. If a host does not forward MCP initialization instructions, ask its agent to use `recall` explicitly.

For this release, all three doctor commands also passed with `o200k_base`. The installed Codex CLI accepted the generated command and arguments through a temporary command-line configuration override and `mcp get --json`; its saved settings were unchanged. That verifies Codex configuration parsing, not a model conversation or GUI session.

If startup fails, check that the recorded interpreter still exists and that its environment includes the `tokens` extra. Verify a local tokenizer path or provision named tokenizer data while online. A very small recall budget can fail to fit the required metadata; increase `--recall-tokens`. Use a larger doctor timeout for cold startup. On Codex, the generated startup allowance is 60 seconds; adjust it manually if your machine needs longer.

## Optional local semantic recall

Without a configured embedding model, recall uses lexical search and reports dense retrieval as unavailable. To enable local semantic retrieval, install the `semantic` extra in the same environment and provision the model explicitly:

```bash
"$workspool_python" -m pip install \
  'agent-workbook[tokens,semantic] @ https://github.com/idpfaff/workspool/releases/download/v0.8.0/agent_workbook-0.8.0-py3-none-any.whl'
"$workspool_python" -m agent_workbook.cli model-download \
  "$HOME/.local/share/workspool/model"
```

Add `--model-dir "$HOME/.local/share/workspool/model"` to both preview/install and doctor commands. Provisioning downloads a local model; startup uses that directory. Generated configs contain no API keys. Review the [runtime guide](../benchmarks/RUNTIME.md) for coverage limits, budgets, roles, and claims.

Automatic capture covers Workspool's own MCP calls. It does not observe the host's other tools or entire conversation. External activity needs explicit [host hooks](../benchmarks/RUNTIME.md); startup recall needs relevant material already stored in the selected workspace. Restored source content remains untrusted evidence, and freshness or missing coverage must still be checked.
