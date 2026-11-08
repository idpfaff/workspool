"""Freshness reports distinguish exact historical approval from current evidence."""

import pytest

from agent_workbook import tasks
from agent_workbook import verification as v
from agent_workbook.coordination import (
    Principal,
    acting_as,
    append_event,
    changes_since,
)
from agent_workbook.store import Workbook


def save(book, name="data", content="n\n2\n3\n", expected_version=None, **options):
    return book.save_text(
        content,
        name=name,
        kind="table",
        media_type="text/csv",
        expected_version=expected_version,
        **options,
    )


def define(book, name="analysis", expected_version=None, alias="input"):
    return tasks.define_task(
        book,
        name,
        "Sum current data",
        {alias: "data"},
        [
            {
                "name": "total",
                "inputs": [alias],
                "sql": "SELECT sum(n)::BIGINT AS total FROM input1",
            }
        ],
        expected_version=expected_version,
    )


def approve(book, artifact_id):
    digest = book.inspect(artifact_id)["sha256"]
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        append_event(
            db,
            Principal("reviewer", "approver"),
            "approval.accepted",
            artifact_id,
            {"sha256": digest},
        )


def test_changed_source_and_affected_published_task_have_actionable_rerun(tmp_path):
    book = Workbook(tmp_path)
    old = save(book)
    define(book)
    result = tasks.run_task(book, "analysis")
    checkpoint = changes_since(book)["through"]
    new = save(book, content="n\n9\n", expected_version=1)
    status = v.verify(book, refs=[old["artifact_id"]], after=checkpoint)
    assert status["complete"]
    changed = status["sources"][0]["changed_sources"][0]
    assert changed["artifact_id"] == old["artifact_id"]
    assert changed["current_artifact_id"] == new["artifact_id"]
    task = status["published_tasks"][0]
    assert task["run_id"] == result["run_id"] and task["current"] is False
    assert task["affected_since_checkpoint"]
    assert task["suggestion"]["tool"] == "task_run"
    assert status["next_after"] == status["through"] > checkpoint


def test_stale_approval_is_historical_and_never_transferred_to_new_run(tmp_path):
    book = Workbook(tmp_path)
    save(book)
    define(book)
    first = tasks.run_task(book, "analysis")
    approve(book, first["artifact_id"])
    assert v.verify(book)["published_tasks"][0]["current_approved"]
    save(book, content="n\n7\n", expected_version=1)
    stale = v.verify(book, refs=[first["artifact_id"]])
    assert not stale["sources"][0]["current_approved"]
    assert stale["sources"][0]["exact_version_approval"]["actor"] == "reviewer"
    assert not stale["published_tasks"][0]["current_approved"]
    second = tasks.run_task(book, "analysis")
    current = v.verify(book)["published_tasks"][0]
    assert current["current"] and current["run_id"] == second["run_id"]
    assert not current["current_approved"] and current["exact_version_approval"] is None
    assert current["suggestion"]["tool"] == "approve"


def test_restore_barrier_preserves_history_but_requires_reapproval(tmp_path):
    book = Workbook(tmp_path)
    item = save(book)
    save(book, name="other")
    approve(book, item["artifact_id"])
    before = v.verify(book, refs=["data"], limit=1)
    assert before["sources"][0]["current_approved"]
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        append_event(
            db,
            Principal("restorer"),
            "workbook.restored",
            "workbook",
            {"archive_sha256": "a" * 64},
        )
    after = v.verify(book, refs=["data"])["sources"][0]
    assert after["current"] and not after["current_approved"]
    assert after["exact_version_approval"] and after["approval_requires_reconfirmation"]
    with pytest.raises(ValueError, match="snapshot"):
        v.verify(book, refs=["data"], limit=1, cursor=before["cursor"])
    approve(book, item["artifact_id"])
    renewed = v.verify(book, refs=["data"])["sources"][0]
    assert (
        renewed["current_approved"] and not renewed["approval_requires_reconfirmation"]
    )


def test_superseded_saved_report_suggests_rerun_not_stale_approval(tmp_path):
    book = Workbook(tmp_path)
    save(book)
    define(book)
    result = tasks.run_task(book, "analysis")
    metadata = book.inspect(result["artifact_id"])
    book.save_text(
        "replacement",
        name=metadata["name"],
        kind="note",
        media_type="text/plain",
        expected_version=1,
    )
    observed = v.verify(book)["published_tasks"][0]
    assert observed["current"] and not observed["result_current"]
    assert observed["suggestion"]["tool"] == "task_run"


def test_latest_revocation_clears_exact_current_approval(tmp_path):
    book = Workbook(tmp_path)
    item = save(book)
    approve(book, item["artifact_id"])
    digest = book.inspect(item["artifact_id"])["sha256"]
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        append_event(
            db,
            Principal("reviewer", "approver"),
            "approval.revoked",
            item["artifact_id"],
            {"sha256": digest},
        )
    revoked = v.verify(book, refs=["data"])["sources"][0]
    assert revoked["current"] and not revoked["current_approved"]
    assert revoked["exact_version_approval"]["kind"] == "approval.revoked"
    assert "revoked" in revoked["action"]
    approve(book, item["artifact_id"])
    assert v.verify(book, refs=["data"])["sources"][0]["current_approved"]


def test_alias_named_definition_does_not_hide_changed_task_definition(tmp_path):
    book = Workbook(tmp_path)
    save(book)
    define(book, alias="definition")
    tasks.run_task(book, "analysis")
    define(book, alias="definition", expected_version=1)
    task = v.verify(book)["published_tasks"][0]
    assert task["current"] is False
    assert any(
        change["source_alias"] == "@definition" for change in task["changed_sources"]
    )


def test_events_and_tasks_page_once_and_cursor_rejects_mutation(tmp_path):
    book = Workbook(tmp_path)
    save(book)
    for name in ("alpha", "beta", "gamma"):
        define(book, name)
        tasks.run_task(book, name)
    events, names, cursor = [], [], None
    for _ in range(50):
        page = v.verify(book, limit=1, cursor=cursor)
        events += [event["event_id"] for event in page["changed_artifacts"]]
        names += [task["task"] for task in page["published_tasks"]]
        if page["complete"]:
            break
        cursor = page["cursor"]
    else:
        pytest.fail("Verification pagination did not finish")
    assert names == ["alpha", "beta", "gamma"]
    assert len(events) == len(set(events))
    with book.connect() as db:
        assert (
            len(events)
            == db.execute(
                "SELECT count(*) FROM coordination_events WHERE kind='artifact.created'"
            ).fetchone()[0]
        )
    first = v.verify(book, limit=1)
    save(book, name="other")
    with pytest.raises(ValueError, match="snapshot changed"):
        v.verify(book, limit=1, cursor=first["cursor"])


def test_cursor_is_bound_to_workbook_scope_and_approval_state(tmp_path):
    book = Workbook(tmp_path / "a")
    first = save(book)
    save(book, name="other")
    page = v.verify(book, refs=["data"], limit=1)
    with pytest.raises(ValueError, match="scope"):
        v.verify(book, refs=["other"], limit=1, cursor=page["cursor"])
    approve(book, first["artifact_id"])
    with pytest.raises(ValueError, match="snapshot"):
        v.verify(book, refs=["data"], limit=1, cursor=page["cursor"])
    other = Workbook(tmp_path / "b")
    save(other)
    save(other, name="other")
    with pytest.raises(ValueError):
        v.verify(other, refs=["data"], limit=1, cursor=page["cursor"])


def test_missing_artifact_and_bounded_lineage_do_not_claim_current_approval(
    tmp_path, monkeypatch
):
    book = Workbook(tmp_path)
    root = save(book)
    for index in range(4):
        root = save(
            book, name="derived" + str(index), dependencies=[root["artifact_id"]]
        )
    approve(book, root["artifact_id"])
    monkeypatch.setattr(v, "LINEAGE_LIMIT", 2)
    result = v.verify(book, refs=[root["artifact_id"], "missing"])
    assert result["sources"][0]["current"] is None
    assert not result["sources"][0]["current_approved"]
    assert not result["sources"][0]["lineage_complete"]
    assert result["sources"][1]["current"] is False


def test_observer_read_capability_and_no_mutation(tmp_path):
    book = Workbook(tmp_path)
    save(book)
    before = changes_since(book)["through"]
    with (
        acting_as(Principal("restricted", "writer", {"task:run"})),
        pytest.raises(PermissionError),
    ):
        v.verify(book)
    with acting_as(Principal("reader", "reader")):
        assert v.verify(book, refs=["data"])["sources"][0]["current"]
    assert changes_since(book)["through"] == before


@pytest.mark.parametrize(
    "options",
    [
        {"after": -1},
        {"limit": 0},
        {"refs": []},
        {"refs": ["data", "data"]},
        {"cursor": "junk"},
        {"through": 1000000},
    ],
)
def test_invalid_inputs_are_explicit(tmp_path, options):
    with pytest.raises(ValueError):
        v.verify(Workbook(tmp_path), **options)
