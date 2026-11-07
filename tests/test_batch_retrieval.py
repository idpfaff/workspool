"""Combined retrieval preserves exact requests while sharing source evidence."""

import json

import pytest

from agent_workbook import batch_retrieval, retrieval
from agent_workbook.batch_retrieval import retrieve
from agent_workbook.store import Workbook


def note(workbook, name, text, **kwargs):
    return workbook.save_text(
        text, name=name, kind="document", media_type="text/plain", **kwargs
    )


def excerpt(result, entry):
    span = result["spans"][entry["span"]]
    data = span["excerpt"].encode()
    return data[
        entry["start_byte"] - span["start_byte"] : entry["end_byte"]
        - span["start_byte"]
    ].decode()


def test_overlapping_queries_and_reads_share_metadata_and_exact_source(tmp_path):
    workbook = Workbook(tmp_path)
    source = "start " + "filler " * 20 + "ALPHA evidence BETA" + " tail" * 40
    saved = note(workbook, "source", source)
    options = [
        {"text": text, "refs": ["source"], "context_bytes": 200}
        for text in ("alpha", "beta", "ALPHA")
    ]
    result = retrieve(
        workbook, queries=options, reads=[{"ref": "source", "offset": 10, "bytes": 100}]
    )
    assert len(result["artifacts"]) == len(result["spans"]) == 1
    assert result["artifacts"][0]["artifact_id"] == saved["artifact_id"]
    span = result["spans"][0]
    assert (
        span["excerpt"].encode()
        == source.encode()[span["start_byte"] : span["end_byte"]]
    )
    assert [excerpt(result, q["matches"][0]) for q in result["queries"]] == [
        "ALPHA",
        "BETA",
        "ALPHA",
    ]
    assert excerpt(result, result["reads"][0]) == source[10:110]
    assert all(q["complete"] and q["cursor"] is None for q in result["queries"])
    # Compare complete outputs against already batched reads plus search calls,
    # rather than claiming a win against an unnecessarily unbatched read loop.
    baseline = [retrieval.search(workbook, **q) for q in options]
    baseline.append(workbook.inspect_many(["source"], excerpt_bytes=100, offset=10))
    assert len(json.dumps(result)) < len(json.dumps(baseline))


def test_distinct_offsets_nested_and_adjacent_reads_remain_separately_addressable(
    tmp_path,
):
    workbook = Workbook(tmp_path)
    source = "0123456789" * 4
    note(workbook, "source", source)
    reads = [
        {"ref": "source", "offset": start, "bytes": count}
        for start, count in [(10, 5), (0, 10), (2, 4), (15, 5), (30, 10)]
    ]
    result = retrieve(workbook, reads=reads)
    assert len(result["spans"]) == 2
    assert [(s["start_byte"], s["end_byte"]) for s in result["spans"]] == [
        (0, 20),
        (30, 40),
    ]
    assert [excerpt(result, r) for r in result["reads"]] == [
        source[r["offset"] : r["offset"] + r["bytes"]] for r in reads
    ]
    assert [r["next_offset"] for r in result["reads"]] == [15, 10, 6, 20, None]


def test_unicode_boundaries_never_replace_or_split_source_characters(tmp_path):
    workbook = Workbook(tmp_path)
    source = "α😀needleβz"
    note(workbook, "source", source)
    result = retrieve(
        workbook,
        queries=[{"text": "NEEDLE", "context_bytes": 0}],
        reads=[
            {"ref": "source", "bytes": 3},
            {"ref": "source", "offset": 2, "bytes": 4},
        ],
    )
    assert result["spans"][0]["excerpt"] == "α😀needle"
    assert result["queries"][0]["matches"][0]["start_byte"] == 6
    assert [excerpt(result, read) for read in result["reads"]] == ["α", "😀"]
    assert [read["next_offset"] for read in result["reads"]] == [2, 6]
    with pytest.raises(ValueError, match="UTF-8 boundary"):
        retrieve(workbook, reads=[{"ref": "source", "offset": 1}])
    with pytest.raises(ValueError, match="too small for the next UTF-8"):
        retrieve(workbook, reads=[{"ref": "source", "bytes": 1}])


def test_all_queries_and_reads_are_pinned_before_first_blob_read(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    saved = note(workbook, "source", "old needle")
    original_read = batch_retrieval._read

    def mutate_then_read(*args):
        note(workbook, "source", "new needle", expected_version=1)
        return original_read(*args)

    monkeypatch.setattr(batch_retrieval, "_read", mutate_then_read)
    result = retrieve(
        workbook,
        queries=[{"text": "needle", "refs": ["source"]}],
        reads=[{"ref": "source"}],
    )
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["artifact_id"] == saved["artifact_id"]
    assert result["artifacts"][0]["is_latest"] is True
    assert result["spans"][0]["excerpt"] == "old needle"


def test_historical_versions_and_stale_inputs_are_not_collapsed(tmp_path):
    workbook = Workbook(tmp_path)
    old = note(workbook, "source", "same needle")
    dependent = note(
        workbook, "derived", "same needle", dependencies=[old["artifact_id"]]
    )
    new = note(workbook, "source", "same needle", expected_version=1)
    result = retrieve(
        workbook,
        reads=[
            {"ref": ref}
            for ref in (
                old["artifact_id"],
                new["artifact_id"],
                dependent["artifact_id"],
            )
        ],
    )
    assert len(result["artifacts"]) == len(result["spans"]) == 3
    assert [a["is_latest"] for a in result["artifacts"]] == [False, True, True]
    assert result["artifacts"][2]["stale_inputs"] is True


def test_query_cursors_preserve_every_overlapping_match_and_work_with_search(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "a" * 31)
    cursor, offsets = None, []
    for _ in range(20):
        result = retrieve(
            workbook,
            queries=[
                {
                    "text": "aa",
                    "refs": ["source"],
                    "limit": 3,
                    "context_bytes": 0,
                    "cursor": cursor,
                }
            ],
        )
        query = result["queries"][0]
        offsets.extend(hit["start_byte"] for hit in query["matches"])
        cursor = query["cursor"]
        assert query["complete"] is (cursor is None)
        if cursor is None:
            break
    assert offsets == list(range(30))
    first = retrieval.search(workbook, "aa", refs=["source"], limit=1)
    result = retrieve(
        workbook,
        queries=[
            {"text": "aa", "refs": ["source"], "cursor": first["cursor"], "limit": 1}
        ],
    )
    assert result["queries"][0]["matches"][0]["start_byte"] == 1
    resumed = retrieval.search(
        workbook, "aa", refs=["source"], cursor=result["queries"][0]["cursor"]
    )
    assert resumed["hits"][0]["match_start_byte"] == 2
    note(workbook, "source", "replacement", expected_version=1)
    with pytest.raises(ValueError, match="snapshot or query changed"):
        retrieve(
            workbook,
            queries=[{"text": "aa", "refs": ["source"], "cursor": first["cursor"]}],
        )


def test_scan_budget_is_shared_and_unstarted_queries_have_real_continuations(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(batch_retrieval, "MAX_SCAN_BYTES", 200)
    monkeypatch.setattr(retrieval, "WINDOW_BYTES", 64)
    workbook = Workbook(tmp_path)
    note(workbook, "source", "x" * 500 + "needle")
    result = retrieve(
        workbook,
        queries=[
            {"text": "needle", "context_bytes": 0},
            {"text": "x", "context_bytes": 0},
        ],
        reads=[{"ref": "source", "bytes": 10}],
    )
    assert result["scanned_bytes"] <= 200
    assert all(not q["complete"] and q["cursor"] for q in result["queries"])
    cursors, positions = [q["cursor"] for q in result["queries"]], []
    for _ in range(10):
        page = retrieve(
            workbook,
            queries=[{"text": "needle", "context_bytes": 0, "cursor": cursors[0]}],
        )
        query = page["queries"][0]
        positions.extend(hit["start_byte"] for hit in query["matches"])
        cursors[0] = query["cursor"]
        if query["complete"]:
            break
    assert positions == [500]
    assert retrieval.search(workbook, "x", cursor=cursors[1], context_bytes=0)["hits"]


def test_empty_sources_no_match_and_end_of_file_are_explicit(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "empty", "")
    result = retrieve(workbook, queries=[{"text": "absent"}], reads=[{"ref": "empty"}])
    assert result["queries"] == [{"matches": [], "complete": True, "cursor": None}]
    assert result["reads"] == [
        {"span": 0, "start_byte": 0, "end_byte": 0, "next_offset": None}
    ]
    assert result["spans"][0]["excerpt"] == ""


def test_output_budget_preserves_request_slots_and_checks_final_representation(
    tmp_path,
):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle and source")
    options = {"queries": [{"text": "needle"}], "reads": [{"ref": "source"}]}
    result = retrieve(workbook, **options)
    size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
    assert retrieve(workbook, **options, max_bytes=size) == result
    reduced = retrieve(workbook, **options, max_bytes=size - 1)
    assert (
        len(json.dumps(reduced, ensure_ascii=False, separators=(",", ":")).encode())
        < size
    )
    assert len(reduced["queries"]) == len(reduced["reads"]) == 1
    assert (
        reduced["queries"][0]["cursor"]
        or reduced["reads"][0]["next_offset"] is not None
    )
    with pytest.raises(ValueError, match="required metadata"):
        retrieve(workbook, **options, max_bytes=1)
    seen = []
    with pytest.raises(ValueError, match="required metadata"):
        retrieve(workbook, **options, fits=lambda value: seen.append(value) or False)
    assert seen[0] == result and len(seen) > 1
    assert retrieve(workbook, **options, fits=lambda value: value == result) == result


def test_deleted_and_malformed_sources_fail_explicitly(tmp_path, monkeypatch):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle")
    original_read = batch_retrieval._read

    def delete_then_read(workbook, record, *args):
        (workbook.blobs / record[1]).unlink()
        return original_read(workbook, record, *args)

    monkeypatch.setattr(batch_retrieval, "_read", delete_then_read)
    with pytest.raises(ValueError, match="deleted"):
        retrieve(workbook, reads=[{"ref": "source"}])
    monkeypatch.setattr(batch_retrieval, "_read", original_read)
    invalid = tmp_path / "bad.txt"
    invalid.write_bytes(b"invalid\xff")
    workbook.import_path(invalid, name="bad")
    with pytest.raises(ValueError, match="not valid UTF-8"):
        retrieve(workbook, reads=[{"ref": "bad"}])


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"queries": []},
        {"queries": "needle"},
        {"queries": [{}]},
        {"queries": [{"text": "needle"}] * 9},
        {"queries": [{"text": "needle", "cursor": "invalid"}]},
        {"queries": [{"text": "needle", "unexpected": 1}]},
        {"reads": [{"ref": "source"}] * 17},
        {"reads": [{}]},
        {"reads": [{"ref": "source", "offset": True}]},
        {"reads": [{"ref": "source", "offset": 100}]},
        {"reads": [{"ref": "source", "bytes": 0}]},
        {"reads": [{"ref": "source", "bytes": True}]},
        {"reads": [{"ref": "source", "typo": 1}]},
        {"reads": [{"ref": "source"}], "max_bytes": 0},
        {"reads": [{"ref": "source"}], "max_bytes": 32769},
        {"reads": [{"ref": "source"}], "fits": False},
    ],
)
def test_invalid_batch_arguments_are_explicit(tmp_path, options):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle")
    with pytest.raises(ValueError):
        retrieve(workbook, **options)


def test_overlap_merge_detects_inconsistent_source_bytes():
    spans = [
        {"artifact": 0, "start_byte": 0, "end_byte": 3, "excerpt": "abc"},
        {"artifact": 0, "start_byte": 1, "end_byte": 4, "excerpt": "XYZ"},
    ]
    with pytest.raises(ValueError, match="changed outside"):
        batch_retrieval._merge(spans)


def test_small_budget_read_pages_reconstruct_every_unicode_byte(tmp_path):
    workbook = Workbook(tmp_path)
    source = "α😀line\r\n" * 150 + "lastβ"
    saved = note(workbook, "source", source)
    offset, parts = 0, []
    for _ in range(100):
        result = retrieve(
            workbook,
            reads=[{"ref": saved["artifact_id"], "offset": offset, "bytes": 32768}],
            max_bytes=600,
        )
        assert (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            <= 600
        )
        read = result["reads"][0]
        assert read["start_byte"] == offset
        parts.append(excerpt(result, read))
        if read["next_offset"] is None:
            break
        assert read["next_offset"] > offset
        offset = read["next_offset"]
    else:
        pytest.fail("Read pagination made no progress")
    assert "".join(parts).encode() == source.encode()


def test_budget_omissions_resume_first_overlapping_unicode_match(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "é" * 20)
    cursor, offsets = None, []
    fits = lambda value: sum(len(s["excerpt"].encode()) for s in value["spans"]) <= 6
    for _ in range(30):
        result = retrieve(
            workbook,
            queries=[{"text": "éé", "context_bytes": 0, "limit": 20, "cursor": cursor}],
            fits=fits,
        )
        assert fits(result)
        query = result["queries"][0]
        offsets.extend(hit["start_byte"] for hit in query["matches"])
        assert all(excerpt(result, hit) == "éé" for hit in query["matches"])
        cursor = query["cursor"]
        assert query["complete"] is (cursor is None)
        if cursor is None:
            break
    assert offsets == list(range(0, 38, 2))


def test_omitted_query_cursor_identifies_original_scope_artifact(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "first", "needle")
    second = note(workbook, "second", "needle needle")
    options = {"text": "needle", "refs": ["first", "second"], "context_bytes": 0}
    first = retrieve(
        workbook, queries=[options], fits=lambda r: len(r["artifacts"]) <= 1
    )
    query = first["queries"][0]
    assert len(query["matches"]) == 1 and not query["complete"]
    second_page = retrieve(workbook, queries=[options | {"cursor": query["cursor"]}])
    assert second_page["artifacts"][0]["artifact_id"] == second["artifact_id"]
    assert [m["start_byte"] for m in second_page["queries"][0]["matches"]] == [0, 7]


def test_pending_reads_keep_slots_and_resume_when_completed_requests_are_removed(
    tmp_path,
):
    workbook = Workbook(tmp_path)
    note(workbook, "evidence", "needle")
    source = note(workbook, "source", "body" * 200)
    result = retrieve(
        workbook,
        queries=[{"text": "needle", "refs": ["evidence"]}],
        reads=[{"ref": "source", "offset": 2, "bytes": 300}],
        fits=lambda r: all(
            entry["start_byte"] == entry["end_byte"] for entry in r["reads"]
        ),
    )
    assert result["queries"][0]["complete"] and result["queries"][0]["matches"]
    read = result["reads"][0]
    assert read["start_byte"] == read["end_byte"] == read["next_offset"] == 2
    assert excerpt(result, read) == ""
    resumed = retrieve(
        workbook,
        reads=[
            {"ref": source["artifact_id"], "offset": read["next_offset"], "bytes": 300}
        ],
        max_bytes=600,
    )
    assert resumed["reads"][0]["end_byte"] > 2


def test_trimming_reuses_loaded_sources_and_preserves_intact_passages(
    tmp_path, monkeypatch
):
    workbook = Workbook(tmp_path)
    note(workbook, "evidence", "needle with exact context")
    note(workbook, "source", "body" * 200)
    calls = {"read": 0, "search": 0}
    original_read, original_search = batch_retrieval._read, retrieval._search_selected

    def read(*args, **kwargs):
        calls["read"] += 1
        return original_read(*args, **kwargs)

    def search(*args, **kwargs):
        calls["search"] += 1
        return original_search(*args, **kwargs)

    monkeypatch.setattr(batch_retrieval, "_read", read)
    monkeypatch.setattr(retrieval, "_search_selected", search)
    fits = lambda result: sum(len(s["excerpt"]) for s in result["spans"]) <= 50
    result = retrieve(
        workbook,
        queries=[{"text": "needle", "refs": ["evidence"]}],
        reads=[{"ref": "source", "bytes": 800}],
        fits=fits,
    )
    assert fits(result) and calls == {"read": 1, "search": 1}
    query = result["queries"][0]
    assert query["complete"] and len(query["matches"]) == 1
    assert (
        result["spans"][query["matches"][0]["span"]]["excerpt"]
        == "needle with exact context"
    )
    assert 0 < result["reads"][0]["next_offset"] < 800


def test_nonmonotone_budget_only_returns_an_actually_fitting_candidate(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "😀" * 40)
    fits = lambda result: len(result["spans"][0]["excerpt"]) in {0, 1, 7, 13, 19}
    result = retrieve(workbook, reads=[{"ref": "source", "bytes": 160}], fits=fits)
    assert fits(result)
    assert 0 < result["reads"][0]["end_byte"] < 160


def test_pending_only_results_fail_instead_of_returning_nonprogressing_cycles(tmp_path):
    workbook = Workbook(tmp_path)
    note(workbook, "source", "needle")
    with pytest.raises(ValueError, match="cannot fit progress"):
        retrieve(
            workbook,
            queries=[{"text": "needle"}],
            fits=lambda result: not result["spans"],
        )
    with pytest.raises(ValueError, match="cannot fit progress"):
        retrieve(
            workbook,
            reads=[{"ref": "source"}],
            fits=lambda result: not result["spans"][0]["excerpt"],
        )
    note(workbook, "gap", "x" * 100 + "needle")
    first = retrieve(
        workbook,
        queries=[{"text": "needle", "refs": ["gap"]}],
        fits=lambda result: not result["spans"],
    )
    assert not first["queries"][0]["matches"] and first["queries"][0]["cursor"]
    with pytest.raises(ValueError, match="cannot fit progress"):
        retrieve(
            workbook,
            queries=[
                {
                    "text": "needle",
                    "refs": ["gap"],
                    "cursor": first["queries"][0]["cursor"],
                }
            ],
            fits=lambda result: not result["spans"],
        )
