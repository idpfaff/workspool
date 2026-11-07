"""The benchmark must not manufacture savings through accounting or partial reads."""

import asyncio
import importlib.util
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from agent_workbook.store import Workbook

SPEC = importlib.util.spec_from_file_location(
    "roundtrip_benchmark",
    Path(__file__).parents[1] / "benchmarks/roundtrip_efficiency.py",
)
bench = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bench)


class CharacterCounts:
    def parts(self, texts):
        return {"characters": sum(map(len, texts))}


class EchoClient:
    async def call_tool(self, name, arguments):
        await asyncio.sleep(0)
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=bench.compact({"name": name, "arguments": arguments}),
                )
            ]
        )


def test_independent_calls_share_one_decision_and_keep_every_payload():
    trace = bench.Trace("group", CharacterCounts())
    trace.setup = ["S"]
    trace.add("user", "Q")
    requests = [("inspect", {"ref": "a"}), ("inspect", {"ref": "b"})]
    result = asyncio.run(bench.calls(EchoClient(), trace, requests))
    assert result == [
        {"name": name, "arguments": arguments} for name, arguments in requests
    ]
    trace.answer("A")
    assert trace.decisions == [1, 5]
    assert [event["role"] for event in trace.events] == [
        "user",
        "assistant_tool_call",
        "assistant_tool_call",
        "tool",
        "tool",
        "assistant_answer",
    ]
    measured = trace.measure()
    # Two decisions see setup/question twice; each request/reply appears once.
    exchanges = (
        sum(
            len(bench.compact({"name": name, "arguments": arguments}))
            for name, arguments in requests
        )
        * 2
    )
    assert measured["retained_input_exposure"] == {"characters": 4 + exchanges}
    assert measured["new_payload_tokens"] == {"characters": 3 + exchanges}
    assert measured["tool_calls"] == 2 and measured["scripted_decisions"] == 2
    assert measured["instruction_tokens"] == {"characters": 1}
    assert measured["schema_tokens"] == {"characters": 0}
    assert measured["tool_request_tokens"] == {"characters": exchanges // 2}
    assert measured["tool_reply_tokens"] == {"characters": exchanges // 2}
    assert measured["user_tokens"] == measured["answer_tokens"] == {"characters": 1}


def test_line_splices_reconstruct_exact_unicode_and_line_endings():
    before = "first\r\n😀 old\nlast without newline"
    changes = [
        {"start": 1, "delete": 1, "lines": ["é new\r\n"]},
        {"start": 3, "delete": 0, "lines": [" appended"]},
    ]
    assert (
        bench.apply_changes(before, changes)
        == "first\r\né new\r\nlast without newline appended"
    )
    assert bench.apply_changes(before, []) == before


@pytest.mark.parametrize(
    "changes",
    [
        [{"start": -1, "delete": 0, "lines": []}],
        [{"start": 0, "delete": 4, "lines": []}],
        [
            {"start": 1, "delete": 1, "lines": []},
            {"start": 0, "delete": 0, "lines": []},
        ],
        [{"start": True, "delete": 0, "lines": []}],
    ],
)
def test_invalid_delta_cannot_count_as_success(changes):
    with pytest.raises(AssertionError):
        bench.apply_changes("one\ntwo\n", changes)


def test_page_oracle_preserves_utf8_without_replacement_characters():
    assert bench.expected_page("A😀BéC", 0, 3) == ("A", 1)
    assert bench.expected_page("A😀BéC", 1, 4) == ("😀", 5)
    assert bench.expected_page("A😀BéC", 5, 100) == ("BéC", None)
    assert bench.expected_page("", 0, 8192) == ("", None)
    with pytest.raises(UnicodeDecodeError):
        bench.expected_page("A😀B", 2, 4)


def test_range_extraction_uses_byte_offsets_within_merged_span():
    text = "A😀BéC"
    reply = {
        "artifacts": [{"name": "doc", "artifact_id": "wb_example"}],
        "spans": [
            {
                "artifact": 0,
                "start_byte": 100,
                "end_byte": 100 + len(text.encode()),
                "excerpt": text,
            }
        ],
    }
    result = bench.select_span(
        reply, {"span": 0, "start_byte": 101, "end_byte": 105, "next_offset": 105}
    )
    assert result["excerpt"] == "😀" and result["next_offset"] == 105
    with pytest.raises(AssertionError):
        bench.select_span(reply, {"span": 0, "start_byte": 99, "end_byte": 105})
    with pytest.raises(UnicodeDecodeError):
        bench.select_span(reply, {"span": 0, "start_byte": 102, "end_byte": 105})


def test_inspect_control_batches_matching_ranges_and_deduplicates_refs():
    reads = [
        {"ref": "a", "bytes": 512},
        {"ref": "b", "bytes": 512},
        {"ref": "a", "bytes": 512},
        {"ref": "a", "offset": 384, "bytes": 512},
    ]
    assert bench.inspect_groups(reads) == [(0, 512, ["a", "b"]), (384, 512, ["a"])]
    assert [
        len(refs)
        for _, _, refs in bench.inspect_groups(
            [{"ref": str(index)} for index in range(33)]
        )
    ] == [16, 16, 1]


def test_fixture_ids_are_identical_across_implementations_and_advance(tmp_path):
    first, second = Workbook(tmp_path / "first"), Workbook(tmp_path / "second")
    one = bench.save_fixture(first, "test", "source", "old")
    same = bench.save_fixture(second, "test", "source", "old")
    assert one["artifact_id"] == same["artifact_id"]
    new = bench.save_fixture(first, "test", "source", "new", version=2)
    other = bench.save_fixture(second, "test", "source", "new", version=2)
    assert new["artifact_id"] == other["artifact_id"] != one["artifact_id"]
    assert first.inspect(one["artifact_id"], 100)["excerpt"] == "old"


def test_passage_oracle_includes_overlaps_and_unicode_byte_positions():
    assert bench.match_truth({"doc": "é ababa"}, "aba") == {
        ("doc", 3, 6),
        ("doc", 5, 8),
    }


def test_dispersed_fact_fixture_cannot_answer_other_fields_from_first_context(tmp_path):
    texts, truth = bench.passage_fixture(Workbook(tmp_path), dispersed=True)
    for name, text in texts.items():
        raw = text.encode()
        first = raw.index(b"renewal_code=")
        context = raw[max(0, first - 256) : first + len(b"renewal_code=") + 256]
        assert truth[name]["renewal_code"].encode() in context
        assert all(
            field.encode() + b"=" not in context
            for field in ("owner", "status", "risk_limit")
        )
        assert all(
            len(bench.match_truth({name: text}, field + "=")) == 1
            for field in truth[name]
        )


def test_shortened_read_slots_continue_until_every_requested_range_is_exact():
    texts = {"a": "0123456789", "b": "ABCDEF"}
    requests = [{"ref": "a", "offset": 1, "bytes": 5}, {"ref": "b", "bytes": 4}]
    seen = []

    async def limited(_client, _trace, reads):
        seen.append(reads)
        output = []
        for index, request in enumerate(reads):
            name = request["ref"].removeprefix("wb_")
            start = request.get("offset", 0)
            excerpt, following = bench.expected_page(
                texts[name], start, min(request["bytes"], 2) if index == 0 else 0
            )
            output.append(
                {
                    "artifact": {
                        "name": name,
                        "artifact_id": "wb_" + name,
                        "is_latest": True,
                        "stale_inputs": False,
                    },
                    "start_byte": start,
                    "end_byte": start + len(excerpt.encode()),
                    "excerpt": excerpt,
                    "next_offset": following,
                }
            )
        return output

    retained, _ = asyncio.run(bench.collect_reads(None, None, requests, texts, limited))
    assert retained == ["12345", "ABCD"]
    assert len(seen) == 5
    assert seen[1] == [
        {"ref": "wb_a", "offset": 3, "bytes": 3},
        {"ref": "wb_b", "offset": 0, "bytes": 4},
    ]
    assert seen[3] == [{"ref": "wb_b", "offset": 0, "bytes": 4}]
    assert seen[4] == [{"ref": "wb_b", "offset": 2, "bytes": 2}]


def test_read_continuations_reject_a_completely_stalled_reply():
    async def stalled(_client, _trace, _reads):
        return [
            {
                "artifact": {
                    "name": "a",
                    "artifact_id": "wb_a",
                    "is_latest": True,
                    "stale_inputs": False,
                },
                "start_byte": 0,
                "end_byte": 0,
                "excerpt": "",
                "next_offset": 0,
            }
        ]

    with pytest.raises(AssertionError, match="no progress"):
        asyncio.run(
            bench.collect_reads(
                None, None, [{"ref": "a", "bytes": 4}], {"a": "test"}, stalled
            )
        )


def test_sync_unchanged_retains_body_without_requiring_same_current_version():
    class SyncClient:
        async def call_tool(self, name, arguments):
            assert name == "sync" and arguments == {"ref": "a", "base": 3}
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=bench.compact(
                            {
                                "mode": "unchanged",
                                "artifact": {"artifact_id": "wb_a", "version": 4},
                                "complete": True,
                            }
                        ),
                    )
                ]
            )

    metadata, body, mode = asyncio.run(
        bench.sync_read(
            SyncClient(), bench.Trace("sync", CharacterCounts()), "a", 3, "é\n"
        )
    )
    assert body == "é\n" and mode == "unchanged" and metadata["version"] == 4
