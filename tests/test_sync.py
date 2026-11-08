"""Smart synchronization never needs a fallback-only read or a partial patch."""

import asyncio
import json
import subprocess
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook import sync as syncing
from agent_workbook.render import decode
from agent_workbook.store import Workbook


def note(workbook, name, text, **kwargs):
    return workbook.save_text(
        text, name=name, kind="note", media_type="text/plain", **kwargs
    )


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def apply(body, changes):
    lines = body.splitlines(keepends=True)
    previous_end = 0
    for change in changes:
        assert previous_end <= change["start"] <= len(lines)
        previous_end = change["start"] + change["delete"]
        assert previous_end <= len(lines)
    for change in reversed(changes):
        lines[change["start"] : change["start"] + change["delete"]] = change["lines"]
    return "".join(lines)


def source():
    return "".join(f"Exact evidence in line {i:04}.\r\n" for i in range(200))


def test_initial_and_empty_reads_are_exact_and_have_no_seen_state(tmp_path):
    workbook = Workbook(tmp_path)
    body = "α\r\nβ\r😀\nwithout final newline"
    saved = note(workbook, "draft", body)
    for instance in (workbook, Workbook(tmp_path)):
        result = syncing.sync(instance, "draft")
        assert result == {
            "mode": "full",
            "artifact": workbook.inspect(saved["artifact_id"]),
            "excerpt": body,
            "offset": 0,
            "next_offset": None,
            "complete": True,
        }
    note(workbook, "empty", "")
    result = syncing.sync(workbook, "empty")
    assert result["excerpt"] == "" and result["complete"] is True


def test_unchanged_content_recomputes_freshness_and_version(tmp_path):
    workbook = Workbook(tmp_path)
    parent = note(workbook, "input", "original input")
    base = note(workbook, "draft", source(), dependencies=[parent["artifact_id"]])
    result = syncing.sync(workbook, "draft", base["artifact_id"])
    assert result["mode"] == "unchanged" and result["complete"]
    assert "base" not in result
    assert result["artifact"]["stale_inputs"] is False
    note(workbook, "input", "changed input", expected_version=1)
    result = syncing.sync(workbook, "draft", base["artifact_id"])
    assert result["mode"] == "unchanged"
    assert result["artifact"]["stale_inputs"] is True
    assert result["artifact"]["dependencies"] == [parent["artifact_id"]]
    newer = note(workbook, "draft", source(), expected_version=1)
    result = syncing.sync(workbook, "draft", base["artifact_id"])
    assert result["artifact"]["artifact_id"] == newer["artifact_id"]
    old = syncing.sync(workbook, base["artifact_id"], newer["artifact_id"])
    assert old["mode"] == "unchanged" and old["artifact"]["is_latest"]
    assert old["artifact"]["artifact_id"] == newer["artifact_id"]


def test_sparse_edits_reconstruct_exact_unicode_and_line_endings(tmp_path):
    workbook = Workbook(tmp_path)
    original = source()
    before = note(workbook, "draft", original)
    lines = original.splitlines(keepends=True)
    lines[180:] = ["unterminated 😀"]
    lines[100:102] = []
    lines[60:60] = ["inserted\u2028", "β\r"]
    current = "".join(lines)
    note(workbook, "draft", current, expected_version=1)
    result = syncing.sync(workbook, "draft", before["artifact_id"])
    assert result["mode"] == "delta" and result["complete"]
    assert apply(original, result["changes"]).encode() == current.encode()
    assert size(result) <= 8192


@pytest.mark.parametrize("new", ["", "rewritten entirely\r\n" * 30])
def test_dense_rewrites_return_complete_text_immediately(tmp_path, new):
    workbook = Workbook(tmp_path)
    before = note(workbook, "draft", source())
    note(workbook, "draft", new, expected_version=1)
    result = syncing.sync(workbook, "draft", before["artifact_id"])
    assert result["mode"] == "full" and result["complete"]
    assert result["excerpt"] == new
    assert "requires_read" not in result and "changes" not in result


def test_missing_and_evicted_bases_return_full_current_source(tmp_path):
    workbook = Workbook(tmp_path)
    old = note(workbook, "draft", "old text")
    old_blob = workbook.blobs / workbook.inspect("draft")["sha256"]
    current = note(workbook, "draft", "new text", expected_version=1)
    old_blob.unlink()
    for base in (old["artifact_id"], "wb_" + "0" * 32):
        result = syncing.sync(workbook, "draft", base)
        assert result["mode"] == "full" and result["excerpt"] == "new text"
        assert result["artifact"]["artifact_id"] == current["artifact_id"]
    workbook.delete(old["artifact_id"])
    assert syncing.sync(workbook, "draft", old["artifact_id"])["mode"] == "full"


def test_bounded_pages_pin_identity_and_preserve_every_source_byte(tmp_path):
    workbook = Workbook(tmp_path)
    original = ("😀éA\r\nβ\r" * 300) + "no final newline"
    saved = note(workbook, "draft", original)
    ref, offset, parts = "draft", 0, []
    while True:
        result = syncing.sync(workbook, ref, offset=offset, max_bytes=700)
        assert result["mode"] == "full" and size(result) <= 700
        assert result["offset"] == offset
        parts.append(result["excerpt"])
        if result["complete"]:
            assert result["next_offset"] is None
            break
        assert result["next_offset"] == offset + len(result["excerpt"].encode())
        assert result["next_offset"] > offset
        ref, offset = saved["artifact_id"], result["next_offset"]
        if len(parts) == 1:
            note(workbook, "draft", "concurrent update", expected_version=1)
            with pytest.raises(ValueError, match="immutable"):
                syncing.sync(workbook, "draft", offset=offset, max_bytes=700)
    assert "".join(parts).encode() == original.encode()
    assert result["artifact"]["is_latest"] is False
    end = syncing.sync(workbook, ref, offset=len(original.encode()))
    assert end["excerpt"] == "" and end["complete"]


def test_custom_fits_and_cost_apply_to_actual_complete_candidates(tmp_path):
    workbook = Workbook(tmp_path)
    original = source()
    before = note(workbook, "draft", original)
    current = original.replace("line 0100", "line ONE HUNDRED")
    note(workbook, "draft", current, expected_version=1)
    result = syncing.sync(
        workbook,
        "draft",
        before["artifact_id"],
        cost=lambda reply: 0 if reply["mode"] == "full" else 100000,
    )
    assert result["mode"] == "full" and result["excerpt"] == current
    fits = lambda reply: reply["mode"] == "full" and len(reply["excerpt"]) <= 13
    page = syncing.sync(workbook, "draft", before["artifact_id"], fits=fits)
    assert page["mode"] == "full" and not page["complete"]
    assert fits(page) and 1 <= len(page["excerpt"]) <= 13
    with pytest.raises(ValueError, match="budget"):
        syncing.sync(workbook, "draft", fits=lambda _: False)


@pytest.mark.parametrize(
    "base", ["draft", "wb_missing", "wb_", True, False, 0, -1, 1.0, 2**63]
)
def test_malformed_bases_are_not_silently_ignored(tmp_path, base):
    workbook = Workbook(tmp_path)
    note(workbook, "draft", "body")
    with pytest.raises(ValueError, match="immutable"):
        syncing.sync(workbook, "draft", base)


def test_integer_base_resolves_exact_same_name_and_echoes_only_for_delta(tmp_path):
    workbook = Workbook(tmp_path)
    original = source()
    note(workbook, "other", "unrelated version one")
    old = note(workbook, "draft", original)
    unchanged = syncing.sync(workbook, "draft", 1)
    assert unchanged["mode"] == "unchanged"
    assert "base" not in unchanged and unchanged["complete"]
    current = original.replace("line 0100", "changed line")
    latest = note(workbook, "draft", current, expected_version=1)
    result = syncing.sync(workbook, "draft", 1)
    assert result["mode"] == "delta" and type(result["base"]) is int
    assert result["base"] == 1
    assert apply(original, result["changes"]) == current
    assert result["artifact"]["artifact_id"] == latest["artifact_id"]
    by_id = syncing.sync(workbook, "draft", old["artifact_id"])
    assert by_id["base"] == old["artifact_id"]
    assert by_id["changes"] == result["changes"]
    already_current = syncing.sync(workbook, old["artifact_id"], 2)
    assert already_current["mode"] == "unchanged"
    assert already_current["artifact"]["artifact_id"] == latest["artifact_id"]
    assert already_current["artifact"]["is_latest"] is True


@pytest.mark.parametrize("kind", ["sparse", "dense"])
def test_initial_old_id_sync_always_targets_current_version(tmp_path, kind):
    workbook = Workbook(tmp_path)
    before = source()
    old = note(workbook, "draft", before)
    current = (
        before.replace("line 0100", "current line")
        if kind == "sparse"
        else "dense current replacement"
    )
    latest = note(workbook, "draft", current, expected_version=1)
    for base in (old["artifact_id"], 1):
        result = syncing.sync(workbook, old["artifact_id"], base)
        assert result["artifact"]["artifact_id"] == latest["artifact_id"]
        assert result["artifact"]["is_latest"] is True
        assert result["artifact"]["version"] == 2
        if kind == "sparse":
            assert result["mode"] == "delta"
            assert apply(before, result["changes"]) == current
        else:
            assert result["mode"] == "full" and result["excerpt"] == current
    fresh = syncing.sync(workbook, old["artifact_id"])
    assert fresh["mode"] == "full" and fresh["excerpt"] == current
    assert workbook.inspect(old["artifact_id"], excerpt_bytes=8192)["excerpt"] == before


def test_integer_base_dense_and_missing_versions_fall_back_to_full(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "draft", source())
    note(workbook, "draft", "dense replacement", expected_version=1)
    for base in (1, 3, 2**63 - 1):
        result = syncing.sync(workbook, "draft", base)
        assert result["mode"] == "full" and result["complete"]
        assert result["excerpt"] == "dense replacement"


def test_deleted_integer_base_cannot_retarget_a_recreated_name(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "draft", source())
    workbook.delete("draft", expected_version=1)
    recreated = note(workbook, "draft", "replacement after deletion")
    assert recreated["version"] == 2
    result = syncing.sync(workbook, "draft", 1)
    assert result["mode"] == "full" and result["complete"]
    assert result["excerpt"] == "replacement after deletion"
    assert "changes" not in result
    unchanged = syncing.sync(workbook, "draft", 2)
    assert unchanged["mode"] == "unchanged" and "base" not in unchanged


def test_wrong_names_binary_invalid_utf8_and_corruption_fail(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "draft", "body")
    other = note(workbook, "other", "body")
    with pytest.raises(ValueError, match="same artifact name"):
        syncing.sync(workbook, "draft", other["artifact_id"])
    path = tmp_path / "invalid.txt"
    path.write_bytes(b"valid prefix" * 200 + b"\xff")
    invalid = workbook.import_path(path, name="invalid")
    with pytest.raises(ValueError, match="UTF-8"):
        syncing.sync(workbook, "invalid", max_bytes=700)
    binary = workbook.save_file(
        path, name="binary", kind="file", media_type="application/octet-stream"
    )
    with pytest.raises(ValueError, match="text artifacts"):
        syncing.sync(workbook, binary["artifact_id"])
    (workbook.blobs / workbook.inspect(invalid["artifact_id"])["sha256"]).write_bytes(
        b"mutated"
    )
    with pytest.raises(ValueError, match="outside the workbook"):
        syncing.sync(workbook, "invalid")


def test_corrupt_existing_base_fails_instead_of_applying_unverified_patch(tmp_path):
    workbook = Workbook(tmp_path)
    old = note(workbook, "draft", source())
    digest = workbook.inspect("draft")["sha256"]
    note(
        workbook, "draft", source().replace("line 0100", "changed"), expected_version=1
    )
    (workbook.blobs / digest).write_text("bad source")
    with pytest.raises(ValueError, match="outside the workbook"):
        syncing.sync(workbook, "draft", old["artifact_id"])


def test_complex_and_oversized_delta_sources_still_return_text(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    old = note(workbook, "draft", "a\nb\n" * 500)
    current = "b\na\n" * 500
    note(workbook, "draft", current, expected_version=1)
    assert syncing.sync(workbook, "draft", old["artifact_id"])["mode"] == "full"
    monkeypatch.setattr(syncing, "MAX_SOURCE_BYTES", 8)
    result = syncing.sync(workbook, "draft", old["artifact_id"])
    assert result["mode"] == "full" and result["excerpt"] == current
    monkeypatch.setattr(syncing, "MAX_SYNC_SOURCE_BYTES", 16)
    with pytest.raises(ValueError, match="validation limit"):
        syncing.sync(workbook, "draft")


@pytest.mark.parametrize("max_bytes", [0, -1, 32769, True, 1.5])
def test_invalid_output_limits_fail(tmp_path, max_bytes):
    workbook = Workbook(tmp_path)
    note(workbook, "draft", "text")
    with pytest.raises(ValueError, match="max_bytes"):
        syncing.sync(workbook, "draft", max_bytes=max_bytes)


def test_invalid_offsets_and_impossibly_small_budget_fail(tmp_path):
    workbook = Workbook(tmp_path)
    saved = note(workbook, "draft", "😀\n")
    for offset in (-1, True, 1.5, 6):
        with pytest.raises(ValueError, match="offset"):
            syncing.sync(workbook, saved["artifact_id"], offset=offset)
    with pytest.raises(ValueError, match="UTF-8 character boundary"):
        syncing.sync(workbook, saved["artifact_id"], offset=1)
    with pytest.raises(ValueError, match="budget"):
        syncing.sync(workbook, "draft", max_bytes=1)


def test_cli_numeric_base_and_initial_old_id_sync_target_current(tmp_path):
    workbook = Workbook(tmp_path)
    original = source()
    old = note(workbook, "draft", original)
    current = original.replace("line 0100", "current exact line")
    latest = note(workbook, "draft", current, expected_version=1)
    command = [
        sys.executable,
        "-m",
        "agent_workbook.cli",
        "--workspace",
        str(tmp_path),
        "sync",
        old["artifact_id"],
    ]
    result = json.loads(subprocess.check_output([*command, "--base", "1"], text=True))
    assert result["mode"] == "delta"
    assert type(result["base"]) is int and result["base"] == 1
    assert result["artifact"]["artifact_id"] == latest["artifact_id"]
    assert result["artifact"]["is_latest"] is True
    assert apply(original, result["changes"]) == current
    unchanged = json.loads(
        subprocess.check_output([*command, "--base", "2"], text=True)
    )
    assert unchanged["mode"] == "unchanged" and "base" not in unchanged
    assert unchanged["artifact"]["version"] == 2
    full = json.loads(subprocess.check_output(command, text=True))
    assert full["mode"] == "full" and full["excerpt"] == current


@pytest.mark.parametrize("discovery", ["eager", "lazy"])
def test_actual_mcp_preserves_integer_base_and_rejects_boolean_numbers(
    tmp_path, discovery
):
    workbook = Workbook(tmp_path)
    original = source()
    old = note(workbook, "draft", original)
    current = original.replace("line 0100", "current exact line")
    latest = note(workbook, "draft", current, expected_version=1)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(tmp_path),
                "serve",
                "--tools",
                "sync",
                "retrieve",
                *(["--experimental-lazy"] if discovery == "lazy" else []),
                "--response-content",
                "text",
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()

            async def call(name, arguments):
                return await client.call_tool(
                    "workspool" if discovery == "lazy" else name,
                    {"tool": name, "arguments": arguments}
                    if discovery == "lazy"
                    else arguments,
                )

            result = await call("sync", {"ref": old["artifact_id"], "base": 1})
            assert not result.isError
            data = decode(result.content)
            assert data["mode"] == "delta" and type(data["base"]) is int
            assert data["artifact"]["artifact_id"] == latest["artifact_id"]
            assert apply(original, data["changes"]) == current
            for boolean in (True, False):
                for field in ("base", "offset", "max_bytes", "max_tokens"):
                    invalid = await call("sync", {"ref": "draft", field: boolean})
                    assert invalid.isError, (field, boolean, invalid.content)
                for field in ("max_bytes", "max_tokens"):
                    invalid = await call(
                        "retrieve", {"reads": [{"ref": "draft"}], field: boolean}
                    )
                    assert invalid.isError, (field, boolean, invalid.content)
                for field in ("offset", "bytes"):
                    invalid = await call(
                        "retrieve", {"reads": [{"ref": "draft", field: boolean}]}
                    )
                    assert invalid.isError, (field, boolean, invalid.content)
                for field in ("limit", "context_bytes"):
                    invalid = await call(
                        "retrieve", {"queries": [{"text": "line", field: boolean}]}
                    )
                    assert invalid.isError, (field, boolean, invalid.content)

    asyncio.run(run())
