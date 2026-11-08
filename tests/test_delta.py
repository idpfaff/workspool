"""Exact bounded deltas require an explicitly held immutable base."""

import asyncio
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workbook.delta import MAX_SOURCE_BYTES, MAX_SOURCE_LINES, diff
from agent_workbook.render import decode
from agent_workbook.store import Workbook


def note(workbook, name, content, **kwargs):
    return workbook.save_text(
        content, name=name, kind="note", media_type="text/plain", **kwargs
    )


def apply(base, changes):
    lines = base.splitlines(keepends=True)
    previous_end = 0
    for change in changes:
        assert change["start"] >= previous_end
        assert change["delete"] >= 0
        previous_end = change["start"] + change["delete"]
        assert previous_end <= len(lines)
    for change in reversed(changes):
        lines[change["start"] : change["start"] + change["delete"]] = change["lines"]
    return "".join(lines)


def long_source():
    return "".join(
        f"Line {index:04}: exact evidence retained for the current review.\n"
        for index in range(300)
    )


def test_sparse_insert_delete_replace_reconstruct_exact_source(tmp_path):
    workbook = Workbook(tmp_path)
    original = long_source()
    before = note(workbook, "draft", original)
    lines = original.splitlines(keepends=True)
    lines[240:242] = ["replacement with no normalization\r\n"]
    lines[160:162] = []
    lines[80:80] = ["inserted 😀\n", "inserted é\r"]
    current = "".join(lines)
    latest = note(workbook, "draft", current, expected_version=1)
    result = diff(workbook, "draft", before["artifact_id"])
    assert not result.get("requires_read")
    assert len(result["changes"]) == 3
    assert apply(original, result["changes"]).encode() == current.encode()
    assert result["base"] == before["artifact_id"]
    assert result["artifact"] == workbook.inspect(latest["artifact_id"])


def test_mixed_line_endings_unicode_and_unterminated_last_line(tmp_path):
    workbook = Workbook(tmp_path)
    original = ("α\r\nβ\rγ\nδ\u2028" * 100) + "last line"
    previous = note(workbook, "unicode", original)
    current = original.replace("β\r", "😀 changed\r", 1) + " without a newline"
    note(workbook, "unicode", current, expected_version=1)
    result = diff(workbook, "unicode", previous["artifact_id"])
    assert not result.get("requires_read")
    assert apply(original, result["changes"]).encode() == current.encode()


def test_unchanged_deltas_recompute_identity_and_ancestor_freshness(tmp_path):
    workbook = Workbook(tmp_path)
    parent = note(workbook, "source", "source data")
    base = note(
        workbook, "review", "reviewed text", dependencies=[parent["artifact_id"]]
    )
    first = diff(workbook, "review", base["artifact_id"])
    assert first["changes"] == []
    assert first["artifact"]["stale_inputs"] is False
    note(workbook, "source", "new source", expected_version=1)
    stale = diff(workbook, "review", base["artifact_id"])
    assert stale["changes"] == []
    assert stale["artifact"]["stale_inputs"] is True
    assert stale["artifact"]["dependencies"] == [parent["artifact_id"]]
    revised = note(workbook, "review", "reviewed text", expected_version=1)
    same_body = diff(workbook, "review", base["artifact_id"])
    assert same_body["changes"] == []
    assert same_body["artifact"]["artifact_id"] == revised["artifact_id"]
    assert same_body["artifact"]["version"] == 2
    historical = diff(workbook, base["artifact_id"], revised["artifact_id"])
    assert historical["changes"] == [] and historical["artifact"]["is_latest"] is False


def test_empty_text_and_explicit_base_have_no_implicit_cache(tmp_path):
    workbook = Workbook(tmp_path)
    saved = note(workbook, "empty", "")
    assert diff(workbook, "empty", saved["artifact_id"])["changes"] == []
    reopened = Workbook(tmp_path)
    assert diff(reopened, "empty", saved["artifact_id"])["changes"] == []
    with pytest.raises(ValueError, match="immutable"):
        diff(reopened, "empty", "empty")


def test_reverse_historical_delta_is_also_exact(tmp_path):
    workbook = Workbook(tmp_path)
    original = long_source()
    base = note(workbook, "draft", original)
    current = original.replace("Line 0150:", "Revised 0150:")
    new = note(workbook, "draft", current, expected_version=1)
    result = diff(workbook, base["artifact_id"], new["artifact_id"])
    assert apply(current, result["changes"]) == original
    assert result["artifact"]["is_latest"] is False


@pytest.mark.parametrize(
    "original,current,max_bytes,reason",
    [
        ("x" * (MAX_SOURCE_BYTES + 1), "small", 8192, "source_too_large"),
        ("a\n" * (MAX_SOURCE_LINES + 1), "small", 8192, "too_many_lines"),
        ("a\nb\n" * 500, "b\na\n" * 500, 8192, "diff_too_complex"),
        (
            "".join(f"before {i}\n" for i in range(3000)),
            "new\n" * 3000,
            8192,
            "diff_too_complex",
        ),
        (
            long_source(),
            long_source().replace("Line 0150:", "Changed 0150:"),
            1,
            "delta_too_large",
        ),
        ("old short text", "new short text", 8192, "full_read_smaller"),
        ("old\n" * 20, "new\n" * 20, 8192, "full_read_smaller"),
        ("some text", "", 8192, "full_read_smaller"),
    ],
)
def test_limits_return_explicit_whole_read_fallback(
    tmp_path, original, current, max_bytes, reason
):
    workbook = Workbook(tmp_path)
    # Native import permits the source-size-bound fixture without inline limits.
    path = tmp_path / "source.txt"
    path.write_text(original, newline="")
    base = workbook.import_path(path, name="draft")
    path.write_text(current, newline="")
    latest = workbook.import_path(path, name="draft", expected_version=1)
    result = diff(workbook, "draft", base["artifact_id"], max_bytes)
    assert result["requires_read"] is True
    assert result["reason"] == reason
    assert "changes" not in result
    assert result["artifact"]["artifact_id"] == latest["artifact_id"]
    assert result["base"] == base["artifact_id"]


def test_wrong_name_binary_invalid_utf8_and_unknown_versions_fail(tmp_path):
    workbook = Workbook(tmp_path)
    source = note(workbook, "source", "text")
    other = note(workbook, "other", "same content is insufficient")
    with pytest.raises(ValueError, match="same artifact name"):
        diff(workbook, "source", other["artifact_id"])
    with pytest.raises(ValueError, match="Unknown artifact"):
        diff(workbook, "source", "wb_missing")
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\xff\x00")
    binary = workbook.save_file(
        path, name="binary", kind="file", media_type="application/octet-stream"
    )
    with pytest.raises(ValueError, match="text artifacts"):
        diff(workbook, "binary", binary["artifact_id"])
    invalid = workbook.save_file(
        path, name="invalid", kind="document", media_type="text/plain"
    )
    with pytest.raises(ValueError, match="UTF-8"):
        diff(workbook, "invalid", invalid["artifact_id"])
    with pytest.raises(ValueError, match="Unknown artifact"):
        diff(workbook, "missing", source["artifact_id"])


def test_deleted_base_cannot_retarget_recreated_name(tmp_path):
    workbook = Workbook(tmp_path)
    base = note(workbook, "draft", "original")
    workbook.delete("draft", expected_version=1)
    replacement = note(workbook, "draft", "replacement")
    assert replacement["version"] == 2
    with pytest.raises(ValueError, match="Unknown artifact"):
        diff(workbook, "draft", base["artifact_id"])


def test_diff_detects_external_blob_mutation(tmp_path):
    workbook = Workbook(tmp_path)
    base = note(workbook, "draft", "original")
    item = workbook.inspect("draft")
    (workbook.blobs / item["sha256"]).write_text("modified")
    with pytest.raises(ValueError, match="outside the workbook"):
        diff(workbook, "draft", base["artifact_id"])


@pytest.mark.parametrize("max_bytes", [0, -1, 32769, True, 1.5])
def test_invalid_output_bounds_fail(tmp_path, max_bytes):
    workbook = Workbook(tmp_path)
    saved = note(workbook, "draft", "text")
    with pytest.raises(ValueError, match="max_bytes"):
        diff(workbook, "draft", saved["artifact_id"], max_bytes)


def test_diff_roundtrip_over_efficient_cli(tmp_path):
    workbook = Workbook(tmp_path)
    original = long_source()
    base = note(workbook, "draft", original)
    current = original.replace("Line 0150:", "Changed 0150:")
    note(workbook, "draft", current, expected_version=1)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "agent_workbook.cli",
                "--workspace",
                str(tmp_path),
                "serve",
                "--profile",
                "efficient",
                "--response-content",
                "text",
            ],
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            result = await client.call_tool(
                "diff",
                {"ref": "draft", "base": base["artifact_id"]},
            )
            assert not result.isError, result.content
            data = decode(result.content)
            assert apply(original, data["changes"]) == current
            assert data["artifact"]["version"] == 2
            assert result.structuredContent is None
            invalid = await client.call_tool(
                "diff",
                {"ref": "draft", "base": "draft"},
            )
            assert invalid.isError

    asyncio.run(run())
