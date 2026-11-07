"""Search returns complete, source-grounded evidence under bounded pagination."""

import base64
import struct

import pytest

from agent_workbook import retrieval
from agent_workbook.retrieval import search
from agent_workbook.store import Workbook


def note(workbook, name, text, **kwargs):
    return workbook.save_text(
        text, name=name, kind="document", media_type="text/plain", **kwargs
    )


def all_hits(workbook, text, **kwargs):
    hits, cursor = [], None
    for _ in range(200):
        result = search(workbook, text, cursor=cursor, **kwargs)
        assert result["scanned_bytes"] <= retrieval.MAX_SCAN_BYTES
        hits.extend(result["hits"])
        cursor = result["cursor"]
        assert result["complete"] is (cursor is None)
        if cursor is None:
            return hits
    raise AssertionError("Search pagination made no progress")


def test_search_finds_late_source_evidence_without_prefix_index(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "distractor", "ordinary filler")
    source = "ordinary filler\n" * 6000 + "Unique (exact) [finding].\n"
    saved = note(workbook, "evidence", source)
    assert not workbook.find("Unique")
    result = search(workbook, "unique (exact) [finding].", context_bytes=20)
    assert result["complete"] and len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["artifact_id"] == saved["artifact_id"]
    assert hit["match_start_byte"] > 65536
    data = source.encode()
    assert data[hit["start_byte"] : hit["end_byte"]].decode() == hit["excerpt"]
    assert (
        data[hit["match_start_byte"] : hit["match_end_byte"]].decode()
        == "Unique (exact) [finding]."
    )


def test_overlapping_matches_and_more_than_ten_hits_are_not_lost(tmp_path):
    workbook = Workbook(tmp_path)
    source = "a" * 31
    note(workbook, "overlap", source)
    hits = all_hits(workbook, "aa", limit=3, context_bytes=0)
    assert [hit["match_start_byte"] for hit in hits] == list(range(30))
    assert all(hit["excerpt"] == "aa" for hit in hits)


def test_scan_budget_and_window_edges_never_claim_false_completion(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(retrieval, "WINDOW_BYTES", 128)
    monkeypatch.setattr(retrieval, "MAX_SCAN_BYTES", 512)
    workbook = Workbook(tmp_path)
    source = "x" * 126 + "NEEDLE" + "x" * 3000 + "needle"
    note(workbook, "source", source)
    first = search(workbook, "needle", context_bytes=0)
    assert not first["complete"] and first["cursor"]
    hits = all_hits(workbook, "needle", context_bytes=0)
    assert [hit["match_start_byte"] for hit in hits] == [126, 3132]


def test_utf8_offsets_and_context_round_inward_at_every_page(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "WINDOW_BYTES", 23)
    monkeypatch.setattr(retrieval, "MAX_SCAN_BYTES", 512)
    workbook = Workbook(tmp_path)
    source = "😀α" * 20 + "CAFÉ" + "😀β" * 20 + "café" + "😀"
    note(workbook, "unicode", source)
    data = source.encode()
    hits = all_hits(workbook, "café", limit=1, context_bytes=7)
    assert len(hits) == 2
    for hit in hits:
        assert hit["excerpt"].encode() == data[hit["start_byte"] : hit["end_byte"]]
        match = data[hit["match_start_byte"] : hit["match_end_byte"]].decode()
        assert match.lower() == "café"
        assert hit["match_start_byte"] - hit["start_byte"] <= 7
        assert hit["end_byte"] - hit["match_end_byte"] <= 7


def test_output_budget_continues_at_unreturned_match(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "MAX_OUTPUT_BYTES", 1200)
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle----" * 25)
    first = search(workbook, "needle", limit=20, context_bytes=20)
    assert 0 < len(first["hits"]) < 20 and not first["complete"]
    hits = all_hits(workbook, "needle", limit=20, context_bytes=20)
    assert [hit["match_start_byte"] for hit in hits] == list(range(0, 250, 10))


def test_cursor_rejects_changed_query_scope_or_relevant_snapshot(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "hit hit hit")
    first = search(workbook, "hit", refs=["source"], limit=1)
    cursor = first["cursor"]
    with pytest.raises(ValueError, match="snapshot or query changed"):
        search(workbook, "other", refs=["source"], cursor=cursor)
    with pytest.raises(ValueError, match="snapshot or query changed"):
        search(workbook, "hit", cursor=cursor)
    # An unrelated artifact does not invalidate an explicitly pinned scope.
    note(workbook, "unrelated", "unrelated")
    assert search(workbook, "hit", refs=["source"], cursor=cursor)["hits"]
    note(workbook, "source", "updated hit", expected_version=1)
    with pytest.raises(ValueError, match="snapshot or query changed"):
        search(workbook, "hit", refs=["source"], cursor=cursor)


def test_historical_refs_and_stale_dependencies_keep_correct_identity(tmp_path):
    workbook = Workbook(tmp_path)
    original = note(workbook, "source", "old evidence")
    derived = note(
        workbook,
        "derived",
        "dependent evidence",
        dependencies=[original["artifact_id"]],
    )
    note(workbook, "source", "new content", expected_version=1)
    result = search(
        workbook, "evidence", refs=[original["artifact_id"], derived["artifact_id"]]
    )
    assert [hit["artifact_id"] for hit in result["hits"]] == [
        original["artifact_id"],
        derived["artifact_id"],
    ]
    assert result["hits"][0]["is_latest"] is False
    assert result["hits"][1]["stale_inputs"] is True


def test_cursor_is_caller_supplied_and_independent_between_workspaces(tmp_path):
    first, second = Workbook(tmp_path / "one"), Workbook(tmp_path / "two")
    note(first, "source", "hit hit")
    note(second, "source", "hit hit")
    cursor = search(first, "hit", limit=1)["cursor"]
    assert search(first, "hit", limit=1)["hits"][0]["match_start_byte"] == 0
    assert search(first, "hit", cursor=cursor)["hits"][0]["match_start_byte"] == 4
    with pytest.raises(ValueError, match="snapshot or query changed"):
        search(second, "hit", cursor=cursor)


def test_empty_scope_no_match_binary_refs_and_invalid_utf8(tmp_path):
    workbook = Workbook(tmp_path)
    assert search(workbook, "needle") == {
        "hits": [],
        "cursor": None,
        "complete": True,
        "scanned_bytes": 0,
    }
    note(workbook, "empty", "")
    note(workbook, "other", "something else")
    assert search(workbook, "needle")["complete"]
    binary = tmp_path / "binary.bin"
    binary.write_bytes(b"\x00needle")
    workbook.import_path(binary, name="binary")
    assert search(workbook, "needle")["hits"] == []
    with pytest.raises(ValueError, match="not searchable text"):
        search(workbook, "needle", refs=["binary"])
    invalid = tmp_path / "bad.txt"
    invalid.write_bytes(b"needle\xff")
    workbook.import_path(invalid, name="bad")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        search(workbook, "needle", refs=["bad"])


@pytest.mark.parametrize(
    "options",
    [
        {"text": ""},
        {"text": " "},
        {"text": "a" * 501},
        {"limit": 0},
        {"limit": 21},
        {"limit": True},
        {"context_bytes": -1},
        {"context_bytes": 2049},
        {"context_bytes": True},
        {"refs": []},
        {"refs": "source"},
        {"refs": ["source", "source"]},
        {"cursor": "invalid%%"},
        {"cursor": "x" * 1025},
    ],
)
def test_invalid_arguments_are_explicit(tmp_path, options):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle")
    with pytest.raises(ValueError):
        search(workbook, **({"text": "needle"} | options))


def test_cursor_requires_supported_version_valid_range_and_utf8_boundary(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "😀hit hit")
    cursor = search(workbook, "hit", limit=1)["cursor"]
    assert len(cursor) == 60
    decoded = list(struct.unpack(">B16s16sIQ", base64.urlsafe_b64decode(cursor)))
    for field, value in [(0, 2), (3, 50), (4, 2**64 - 1), (4, 1)]:
        changed = decoded.copy()
        changed[field] = value
        malformed = base64.urlsafe_b64encode(
            struct.pack(">B16s16sIQ", *changed)
        ).decode()
        with pytest.raises(ValueError, match="Invalid search cursor"):
            search(workbook, "hit", cursor=malformed)
    with pytest.raises(ValueError, match="Invalid search cursor"):
        search(workbook, "hit", cursor=cursor[:-1])


def test_insufficient_output_budget_fails_instead_of_repeating_cursor(
    tmp_path, monkeypatch
):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle")
    monkeypatch.setattr(retrieval, "MAX_OUTPUT_BYTES", 1)
    with pytest.raises(ValueError, match="output budget is too small"):
        search(workbook, "needle")


def test_aliases_cannot_search_the_same_version_twice(tmp_path):
    workbook = Workbook(tmp_path)
    saved = note(workbook, "source", "needle")
    with pytest.raises(ValueError, match="distinct versions"):
        search(workbook, "needle", refs=["source", saved["artifact_id"]])
