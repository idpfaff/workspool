"""Conditional execution and approvals bind to exact immutable task snapshots."""

import json

import pytest

from agent_workbook import skills, tasks
from agent_workbook.coordination import Principal, append_event
from agent_workbook.store import Workbook


@pytest.fixture
def book(tmp_path):
    result = Workbook(tmp_path)
    result.save_text("n\n2\n3\n", name="data", kind="table", media_type="text/csv")
    return result


def define(
    book, *, conditional=False, approval=False, first_sql=None, expected_version=None
):
    return tasks.define_task(
        book,
        "flow",
        "Compute a checked total",
        {"data": "data"},
        [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": first_sql or "SELECT sum(n)::BIGINT AS n FROM input1",
            },
            {
                "name": "double",
                "inputs": ["total"],
                "sql": "SELECT n * 2 AS n FROM input1 ORDER BY n",
                **(
                    {"when": {"step": "total", "column": "n", "op": "gt", "value": 10}}
                    if conditional
                    else {}
                ),
                **({"approval": True} if approval else {}),
            },
            {
                "name": "tail",
                "inputs": ["double"],
                "sql": "SELECT n + 1 AS n FROM input1 ORDER BY n",
            },
        ],
        expected_version=expected_version,
    )


def approve(book, ref, accepted=True):
    item = book.inspect(ref)
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return append_event(
            db,
            Principal("reviewer", "approver"),
            "approval.accepted" if accepted else "approval.revoked",
            item["artifact_id"],
            {"sha256": item["sha256"], "note": "fixture attestation"},
        )


def test_plain_sql_stays_format1_and_records_real_step_timings(book):
    saved = define(book)
    with book.connect() as db:
        assert (
            tasks._read_plan(book, book._get(db, saved["artifact_id"]))["format"] == 1
        )
    first = tasks.run_task(book, "flow")
    assert first["steps"][-1]["result"]["rows"] == [[11]]
    assert all(step["elapsed_seconds"] >= 0 for step in first["steps"])
    repeated = tasks.run_task(book, "flow", base_run=first["run_id"])
    assert repeated["delivery"] == "unchanged"


def test_approved_resume_preserves_volatile_upstream_observation(book, monkeypatch):
    define(book, approval=True, first_sql="SELECT random() AS n FROM input1 LIMIT 1")
    first = tasks.run_task(book, "flow")
    assert first["state"] == "awaiting_approval", first
    prior = first["steps"][0]
    approve(book, first["steps"][1]["approval_ref"])
    execute = tasks._execute_step

    def no_new_observation(workbook, step, inputs, force):
        assert step["name"] != "total", "Approval must retain the observed random value"
        return execute(workbook, step, inputs, force)

    monkeypatch.setattr(tasks, "_execute_step", no_new_observation)
    resumed = tasks.run_task(book, "flow", resume_from=first["run_id"])
    assert resumed["state"] == "complete", resumed
    assert resumed["steps"][0]["artifact_id"] == prior["artifact_id"]
    assert resumed["steps"][0]["cache"]["reason"] == "resumed_materialization"
    assert resumed["steps"][-1]["result"]["rows"][0][0] == pytest.approx(
        prior["result"]["rows"][0][0] * 2 + 1
    )


def test_corrupt_resumed_materialization_cannot_pass_an_approved_gate(book):
    define(book, approval=True)
    first = tasks.run_task(book, "flow")
    approve(book, first["steps"][1]["approval_ref"])
    saved = book.inspect(first["steps"][0]["artifact_id"])
    path = book.blobs / saved["sha256"]
    original = path.read_bytes()
    path.write_bytes(b"!" + original[1:])
    resumed = tasks.run_task(book, "flow", resume_from=first["run_id"])
    assert resumed["state"] == "partial"
    assert resumed["steps"][0]["state"] == "failed"
    assert all(step["state"] != "complete" for step in resumed["steps"])


def test_false_condition_skips_branch_and_downstream_without_false_failure(
    book, monkeypatch
):
    saved = define(book, conditional=True, approval=True)
    with book.connect() as db:
        assert (
            tasks._read_plan(book, book._get(db, saved["artifact_id"]))["format"] == 2
        )
    executed = []
    execute = tasks._execute_step

    def record(workbook, step, inputs, force):
        executed.append(step["name"])
        return execute(workbook, step, inputs, force)

    monkeypatch.setattr(tasks, "_execute_step", record)
    result = tasks.run_task(book, "flow")
    assert executed == ["total"]
    assert result["state"] == "complete" and result["current"]
    assert [step["state"] for step in result["steps"]] == [
        "complete",
        "skipped",
        "skipped",
    ]
    assert not result["omitted"] and "approval_ref" not in result["steps"][1]
    with pytest.raises(ValueError, match="unconditional SQL-only"):
        skills.distill(book, "not-yet-supported", "flow")


def test_true_condition_runs_exact_branch(book):
    define(
        book, conditional=True, first_sql="SELECT sum(n)::BIGINT + 10 AS n FROM input1"
    )
    result = tasks.run_task(book, "flow")
    assert result["state"] == "complete"
    assert result["steps"][1]["condition_matched"] is True
    assert result["steps"][-1]["result"]["rows"] == [[31]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT n FROM input1",
        "SELECT n FROM input1 WHERE false",
        "SELECT '5' AS n FROM input1 LIMIT 1",
    ],
)
def test_conditions_require_exactly_one_correctly_typed_scalar(book, sql):
    define(book, conditional=True, first_sql=sql)
    result = tasks.run_task(book, "flow")
    assert result["state"] == "partial"
    assert result["steps"][1]["state"] == "failed"
    assert result["steps"][2]["state"] == "blocked"


def test_gate_waits_then_resume_reuses_same_exact_snapshot(book):
    define(book, approval=True)
    waiting = tasks.run_task(book, "flow")
    assert waiting["state"] == "awaiting_approval"
    assert [step["state"] for step in waiting["steps"]] == [
        "complete",
        "awaiting_approval",
        "blocked",
    ]
    assert waiting["latest_success"] is None
    ref = waiting["steps"][1]["approval_ref"]
    gate = json.loads(book.inspect(ref, excerpt_bytes=8192)["excerpt"])
    assert gate["definition_id"] == waiting["definition_id"]
    assert {item["artifact_id"] for item in gate["inputs"]} >= {
        waiting["definition_id"],
        waiting["sources"]["data"]["artifact_id"],
        waiting["steps"][0]["artifact_id"],
    }
    approve(book, ref)
    resumed = tasks.run_task(book, "flow", resume_from=waiting["run_id"])
    assert resumed["state"] == "complete" and resumed["current"]
    assert resumed["steps"][1]["approval_ref"] == ref
    assert resumed["steps"][0]["cache"]["hit"]
    assert resumed["steps"][-1]["result"]["rows"] == [[11]]
    assert ref in book.inspect(resumed["artifact_id"])["provenance"]


def test_source_or_definition_changes_cannot_reuse_approval(book):
    define(book, approval=True)
    first = tasks.run_task(book, "flow")
    old_ref = first["steps"][1]["approval_ref"]
    approve(book, old_ref)
    book.save_text(
        "n\n20\n", name="data", kind="table", media_type="text/csv", expected_version=1
    )
    historical = tasks.run_task(book, "flow", resume_from=first["run_id"])
    assert historical["state"] == "awaiting_approval"
    assert not historical["steps"][1]["approval"]["approved"]
    current = tasks.run_task(book, "flow")
    assert current["steps"][1]["approval_ref"] != old_ref
    approve(book, current["steps"][1]["approval_ref"])
    define(
        book,
        approval=True,
        first_sql="SELECT sum(n)::BIGINT + 2 AS n FROM input1",
        expected_version=1,
    )
    changed = tasks.run_task(book, "flow")
    assert changed["state"] == "awaiting_approval"
    assert changed["steps"][1]["approval_ref"] != current["steps"][1]["approval_ref"]


def test_revocation_during_step_blocks_downstream_and_publication(book, monkeypatch):
    define(book, approval=True)
    first = tasks.run_task(book, "flow")
    ref = first["steps"][1]["approval_ref"]
    approve(book, ref)
    execute = tasks._execute_step
    executed = []

    def revoke(workbook, step, inputs, force):
        executed.append(step["name"])
        result = execute(workbook, step, inputs, force)
        if step["name"] == "double":
            approve(workbook, ref, accepted=False)
        return result

    monkeypatch.setattr(tasks, "_execute_step", revoke)
    result = tasks.run_task(book, "flow", resume_from=first["run_id"])
    assert executed == ["double"]  # The explicitly resumed upstream is retained.
    assert result["state"] == "awaiting_approval" and result["latest_success"] is None
    assert result["steps"][2]["reason"] == "approval_dependency"


def test_revoke_between_snapshot_save_and_publication_is_fenced(book, monkeypatch):
    define(book, approval=True)
    first = tasks.run_task(book, "flow")
    ref = first["steps"][1]["approval_ref"]
    approve(book, ref)
    save_text = book.save_text

    def before_publication(content, **fields):
        result = save_text(content, **fields)
        if fields.get("kind") == "task_run":
            approve(book, ref, accepted=False)
        return result

    monkeypatch.setattr(book, "save_text", before_publication)
    result = tasks.run_task(book, "flow", resume_from=first["run_id"])
    assert result["state"] == "awaiting_approval" and result["latest_success"] is None
    assert all(step["state"] == "complete" for step in result["steps"])
    assert not result["steps"][1]["approval"]["approved"]


def test_restored_workbook_requires_new_approval(book):
    define(book, approval=True)
    first = tasks.run_task(book, "flow")
    ref = first["steps"][1]["approval_ref"]
    approve(book, ref)
    with book.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        append_event(
            db,
            Principal("restore"),
            "workbook.restored",
            "workbook",
            {"archive_sha256": "a" * 64},
        )
    assert (
        tasks.run_task(book, "flow", resume_from=first["run_id"])["state"]
        == "awaiting_approval"
    )
    approve(book, ref)
    assert (
        tasks.run_task(book, "flow", resume_from=first["run_id"])["state"] == "complete"
    )


@pytest.mark.parametrize(
    "condition",
    [
        {"step": "missing", "column": "n", "op": "eq", "value": 1},
        {"step": "double", "column": "n", "op": "eq", "value": 1},
        {"step": "total", "column": "n", "op": "eval", "value": "x"},
        {"step": "total", "column": "n", "op": "eq", "value": float("nan")},
        {"step": "total", "column": "n", "op": "is_null", "value": None},
    ],
)
def test_invalid_conditions_are_rejected_before_definition(book, condition):
    with pytest.raises(ValueError):
        tasks.define_task(
            book,
            "bad",
            "Invalid",
            {"data": "data"},
            [
                {"name": "total", "inputs": ["data"], "sql": "SELECT 1 AS n"},
                {
                    "name": "double",
                    "inputs": ["total"],
                    "sql": "SELECT * FROM input1",
                    "when": condition,
                },
            ],
        )


@pytest.mark.parametrize("change", ["revoke", "restore"])
def test_live_status_does_not_reuse_stale_authorization_receipt(book, change):
    define(book, approval=True)
    waiting = tasks.run_task(book, "flow")
    ref = waiting["steps"][1]["approval_ref"]
    approve(book, ref)
    complete = tasks.run_task(book, "flow", resume_from=waiting["run_id"])
    assert complete["state"] == "complete"
    if change == "revoke":
        approve(book, ref, accepted=False)
    else:
        with book.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            append_event(
                db,
                Principal("restore"),
                "workbook.restored",
                "workbook",
                {"archive_sha256": "a" * 64},
            )
    live = tasks.status(
        book, "flow", run_id=complete["run_id"], base_run=complete["run_id"]
    )
    assert live["state"] == "awaiting_approval" and live["current"] is False
    assert live["approval_current"] is False
    assert live["steps"][1]["approval"]["approved"] is False
    assert live.get("delivery") != "unchanged"
