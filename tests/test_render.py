"""Adaptive text rendering retains exact result semantics without configuration."""

import copy
import json
import random

import pytest
from mcp.types import TextContent

from agent_workbook.render import TAG, content_bytes, decode, render


def block(value):
    return TextContent(
        type="text", text=json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )


def artifact(index, excerpt=""):
    return {
        "artifact_id": f"wb_{index:032x}",
        "name": f"reviews/case{index:04}",
        "version": 1,
        "is_latest": True,
        "stale_inputs": False,
        "dependencies": [],
        "next_offset": None,
        "excerpt": excerpt,
    }


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"columns": ["count"], "rows": [[20]], "preview_truncated": False},
        {"artifacts": []},
        {TAG: "unknown-v1", "data": "ordinary metadata"},
        {TAG: ["unhashable", "user", "data"]},
        {TAG: {"nested": "user data"}},
    ],
)
def test_small_nonartifact_results_remain_plain_json(data):
    content = render(data)
    assert len(content) == 1
    assert json.loads(content[0].text) == data
    assert decode(content) == data
    assert content_bytes(content) == content_bytes([block(data)])


@pytest.mark.parametrize("encoding", ["json-v1", "text-v1", "artifacts-v1"])
def test_reserved_envelope_collisions_are_escaped_once(encoding):
    data = {TAG: encoding, "data": {TAG: "text-v1", "excerpt_rows": "all"}}
    content = render(data)
    assert json.loads(content[0].text)[TAG] == "json-v1"
    assert decode(content) == data


def test_complete_document_batch_packs_metadata_and_keeps_exact_blocks():
    text = 'A😀BéC\n"quotes", backslash \\ and a tab\t\n' * 50
    data = {"artifacts": [artifact(index, text + str(index)) for index in range(120)]}
    before = copy.deepcopy(data)
    content = render(data)
    envelope = json.loads(content[0].text)
    assert envelope[TAG] == "artifacts-v1"
    assert envelope["excerpt_rows"] == "all"
    assert "artifact_id" in envelope["fields"]
    assert envelope["defaults"]["is_latest"] is True
    assert envelope["defaults"]["dependencies"] == []
    assert len(content) == 121
    assert [item.text for item in content[1:]] == [
        row["excerpt"] for row in data["artifacts"]
    ]
    assert decode(content) == data
    assert data == before
    assert content_bytes(content) < content_bytes([block(data)])
    # Expanding a common mutable value must not make rows share Python objects.
    restored = decode(content)
    restored["artifacts"][0]["dependencies"].append("changed")
    assert restored["artifacts"][1]["dependencies"] == []


def test_heterogeneous_batch_preserves_missing_null_false_and_nested_data():
    rows = [artifact(index, 'Exact\n"text"😀' * 30) for index in range(60)]
    for index, row in enumerate(rows):
        row["checkpoint"] = {
            "objective": "Preserve nested metadata",
            "user": {
                "fields": ["x"],
                "rows": [[None]],
                "missing": [[0, 0]],
                TAG: "artifacts-v1",
            },
        }
        if index % 3 == 0:
            row["optional"] = None
            row["is_latest"] = False
            del row["excerpt"]
        elif index % 3 == 1:
            row["optional"] = False
            row["dependencies"] = [rows[index - 1]["artifact_id"]]
        else:
            row["optional"] = 0
            row["excerpt"] = ""
        if index % 7 == 0:
            del row["optional"]
            row["next_offset"] = 8190
        row["source"] = "文書/é"
    data = {
        "artifacts": rows,
        "other": {"artifact_id": "nested", "excerpt": "Leave nested data alone"},
    }
    content = render(data)
    envelope = json.loads(content[0].text)
    assert envelope[TAG] == "artifacts-v1"
    assert envelope["missing"]
    restored = decode(content)
    assert restored == data
    assert type(restored["artifacts"][1]["optional"]) is bool
    assert type(restored["artifacts"][2]["optional"]) is int
    assert "excerpt" not in restored["artifacts"][0]
    assert restored["artifacts"][2]["excerpt"] == ""
    assert "optional" not in restored["artifacts"][0]
    assert restored["other"] == data["other"]


def test_large_single_excerpt_and_full_metadata_round_trip():
    data = artifact(1, '\n"quoted" \\ 😀' * 500)
    data.update(
        {
            "sha256": "a" * 64,
            "size_bytes": 99999,
            "previous_id": "wb_old",
            "recipe": {"sql": "SELECT 'exact'", "parameters": [None, False, 0]},
        }
    )
    content = render(data)
    assert len(content) == 2
    assert decode(content) == data
    assert content[1].text == data["excerpt"]


def test_empty_excerpts_and_nonstring_excerpt_values_survive():
    rows = [artifact(index) for index in range(40)]
    for index, row in enumerate(rows):
        if index % 3 == 0:
            row["excerpt"] = None
        elif index % 3 == 1:
            del row["excerpt"]
    data = {"artifacts": rows}
    assert decode(render(data)) == data


def test_deterministic_roundtrip_for_varied_metadata_shapes():
    rng = random.Random(8192)
    values = [None, False, True, 0, 1, 1.0, "", '😀\n\\"', [], {}, {TAG: "text-v1"}]
    for _ in range(30):
        rows = []
        for index in range(rng.randrange(1, 40)):
            row = artifact(index, rng.choice(["", "line\n" * 50]))
            for field in ("source", "summary", "checkpoint", "recipe", "optional"):
                if rng.choice([True, False]):
                    row[field] = copy.deepcopy(rng.choice(values))
            rows.append(row)
        data = {"artifacts": rows}
        first = render(data)
        assert first == render(data)
        assert decode(first) == data
        assert content_bytes(first) <= content_bytes([block(data)])


@pytest.mark.parametrize(
    "metadata, texts",
    [
        (
            {TAG: "text-v1", "data": {"artifacts": [{}]}, "excerpt_rows": [0, 0]},
            ["a", "b"],
        ),
        ({TAG: "text-v1", "data": {"artifacts": [{}]}, "excerpt_rows": [1]}, ["a"]),
        ({TAG: "text-v1", "data": {"artifacts": [{}]}, "excerpt_rows": [True]}, ["a"]),
        ({TAG: "artifacts-v1", "fields": ["x"], "rows": [[1, 2]]}, []),
        ({TAG: "artifacts-v1", "fields": ["x", "x"], "rows": [[1, 2]]}, []),
        (
            {
                TAG: "artifacts-v1",
                "fields": ["x"],
                "rows": [[None]],
                "missing": [[0, 1]],
            },
            [],
        ),
        ({TAG: "artifacts-v1", "fields": [], "rows": [[], []], "single": True}, []),
    ],
)
def test_decoder_rejects_inconsistent_representation(metadata, texts):
    content = [
        block(metadata),
        *(TextContent(type="text", text=text) for text in texts),
    ]
    with pytest.raises(ValueError):
        decode(content)
