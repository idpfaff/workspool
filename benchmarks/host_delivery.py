"""Opt-in real Codex host proof; uses the CLI's existing authenticated account.

Creates only synthetic fixtures, uses generated host configuration without
installing it, and compares connected and disconnected fresh conversations.
This is not an offline test and does not select or override the host's model.

Historical-source probe: its initialization-context contract was removed in v0.9.2.
Use the archived measured source to reproduce it; this is not a current host demo.
"""

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from agent_workbook.host_setup import generate
from agent_workbook.store import Workbook

PROMPT = (
    "Discover Workspool's tool schema if needed, then return the confirmation "
    "phrase and source artifact ID from its MCP startup instructions. "
    "Do not execute Workspool tools, read files, or use other services. "
    "If the instructions do not contain it output NOT RECEIVED."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cwd = output / "empty"
    cwd.mkdir()
    book = Workbook(output / "workbook")
    nonce = "hosting-proof-" + secrets.token_hex(12)
    source = book.save_text(
        "Hosting spend review confirmation phrase: " + nonce,
        name="hosting-spend-proof",
        kind="note",
        media_type="text/plain",
        summary="Hosting spend review confirmation phrase",
    )
    config = tomllib.loads(
        generate("codex", book.root, goal="hosting spend review confirmation phrase")
    )["mcp_servers"]["workspool"]
    override = (
        "mcp_servers.workspool={"
        + ",".join(f"{key}={json.dumps(value)}" for key, value in config.items())
        + "}"
    )
    base = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "-C",
        str(cwd),
        "--disable",
        "shell_tool",
        "--disable",
        "apps",
        "--disable",
        "multi_agent",
        "--json",
    ]
    result = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "host": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "prompt": PROMPT,
        "expected_phrase": nonce,
        "expected_artifact_id": source["artifact_id"],
        "configuration": {
            "source": "host_setup.generate('codex', workspace, goal=...)",
            "profile": "runtime",
            "role": "writer",
            "tool_discovery": "lazy",
            "tokenizer": "o200k_base",
            "recall_tokens": 1500,
            "saved_host_settings_changed": False,
            "model_override": None,
        },
        "cases": {},
    }
    print("Workspool: real Codex host delivery check", flush=True)
    print(result["host"], flush=True)
    print("Generated runtime configuration; fresh isolated conversations.", flush=True)
    print(
        "Prompt allows metadata discovery; forbids tool execution/file reads.",
        flush=True,
    )
    for name, extra in [("connected", ["-c", override]), ("disconnected", [])]:
        response_path = output / f"{name}.response.txt"
        command = base + extra + ["--output-last-message", str(response_path), PROMPT]
        with (
            (output / f"{name}.jsonl").open("w") as out,
            (output / f"{name}.stderr").open("w") as err,
        ):
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=180,
            )
        if completed.returncode:
            raise RuntimeError(
                f"Codex {name} failed; inspect private stderr in {output}"
            )
        raw = (output / f"{name}.jsonl").read_bytes()
        events = [json.loads(line) for line in raw.splitlines() if line]
        items = [event["item"] for event in events if "item" in event]
        other_items = [item for item in items if item.get("type") != "agent_message"]
        response = response_path.read_text().strip()
        result["cases"][name] = {
            "response": response,
            "non_message_items": other_items,
            "event_trace_sha256": hashlib.sha256(raw).hexdigest(),
            "usage": [e["usage"] for e in events if "usage" in e],
        }
        print(f"\n{name.upper()}: {response}", flush=True)
        print(f"Recorded non-message items: {len(other_items)}", flush=True)
    with book.connect() as db:
        tool_episodes = db.execute(
            "SELECT count(*) FROM continuity_episodes WHERE kind='tool_result'"
        ).fetchone()[0]
    result["workspool_tool_execution_episodes"] = tool_episodes
    connected = result["cases"]["connected"]
    disconnected = result["cases"]["disconnected"]
    result["passed"] = (
        nonce in connected["response"]
        and source["artifact_id"] in connected["response"]
        and disconnected["response"] == "NOT RECEIVED"
        and not connected["non_message_items"]
        and not disconnected["non_message_items"]
        and tool_episodes == 0
    )
    project = Path(__file__).resolve().parents[1]
    result["runtime_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((project / "src/agent_workbook").glob("*.py"))
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"\nWorkspool tool execution episodes: {tool_episodes}", flush=True)
    print("PASS" if result["passed"] else "FAIL", flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
