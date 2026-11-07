"""Reusable plans are authorized executions, not trusted arbitrary JSON."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_workbook import skills, tasks
from agent_workbook.coordination import Principal, acting_as
from agent_workbook.store import Workbook


def table(book, name="data", body="n\n2\n3\n", expected_version=None):
    return book.save_text(
        body,
        name=name,
        kind="table",
        media_type="text/csv",
        expected_version=expected_version,
    )


def define(book, name="origin", *, steps=None, csv_types=None):
    return tasks.define_task(
        book,
        name,
        "Calculate the exact total",
        {"data": "data"},
        steps
        or [
            {
                "name": "total",
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT + ? AS total FROM input1",
                "parameters": [2],
            }
        ],
        csv_types=csv_types,
    )


@pytest.fixture
def origin(tmp_path):
    book = Workbook(tmp_path)
    table(book)
    define(book)
    run = tasks.run_task(book, "origin")
    assert run["state"] == "complete" and not run["omitted"]
    return book, run


def test_exact_plan_compact_receipt_and_new_data_execution(origin):
    book, run = origin
    distilled = skills.distill(book, "total", "origin")
    assert "package" not in distilled and distilled["origin_run"] == run["run_id"]
    inspected = skills.inspect_skill(book, "total")
    package = inspected["package"]
    assert package["plan"]["steps"][0]["parameters"] == [2]
    assert package["contracts"]["data"] == {
        "media_type": "text/csv",
        "columns": ["n"],
        "types": ["BIGINT"],
    }
    assert "rows" not in package["origin"]["results"]["total"]
    table(book, "new", "n\n20\n30\n")
    result = skills.use_skill(book, "total", "replay", {"data": "new"}, {"total": [3]})
    assert result["source_mode"] == "pinned"
    with book.connect() as db:
        assert (
            db.execute("SELECT count(*) FROM task_runs WHERE name='replay'").fetchone()[
                0
            ]
            == 0
        )
    replay = tasks.run_task(book, "replay")
    assert replay["steps"][0]["result"]["rows"] == [[53]]
    assert replay["producer"]["identity"] == "local"
    assert (
        skills.find_skills(book, "exact")["skills"][0]["artifact_id"]
        == distilled["artifact_id"]
    )


def test_full_ledger_is_used_after_budgeted_receipt(origin):
    book, run = origin
    receipt = tasks.run_task(book, "origin", base_run=run["run_id"])
    assert receipt["delivery"] == "unchanged" and "steps" not in receipt
    distilled = skills.distill(book, "total", "origin", run_id=receipt["run_id"])
    assert skills.inspect_skill(book, distilled["artifact_id"])["package"]["plan"][
        "steps"
    ]


def test_explicit_bindings_and_parameter_arity(origin):
    book, _ = origin
    skills.distill(book, "total", "origin")
    for bindings in ({}, {"data": "data", "extra": "data"}, {"wrong": "data"}):
        with pytest.raises(ValueError, match="exactly every"):
            skills.use_skill(book, "total", "bad", bindings)
    for parameters in (
        {"total": []},
        {"total": [1, 2]},
        {"wrong": [2]},
        {"total": [[2]]},
    ):
        with pytest.raises(ValueError):
            skills.use_skill(book, "total", "bad", {"data": "data"}, parameters)


@pytest.mark.parametrize("body", ["n,extra\n2,4\n", "wrong\n2\n", "n\nhello\n"])
def test_exact_schema_rejects_extra_missing_and_changed_types(origin, body):
    book, _ = origin
    skills.distill(book, "total", "origin")
    table(book, "other", body)
    with pytest.raises(ValueError, match="ordered schema"):
        skills.use_skill(book, "total", "bad", {"data": "other"})


def test_media_type_mismatch(origin):
    book, _ = origin
    skills.distill(book, "total", "origin")
    book.save_text("n\n2\n", name="text", kind="document", media_type="text/plain")
    with pytest.raises(ValueError, match="media type"):
        skills.use_skill(book, "total", "bad", {"data": "text"})


def test_typed_csv_contract_and_exact_decimal_replay(tmp_path):
    book = Workbook(tmp_path)
    table(book, body="n\n9007199254740995.000000000003\n")
    define(
        book,
        steps=[{"name": "value", "inputs": ["data"], "sql": "SELECT n FROM input1"}],
        csv_types={"data": {"n": "DECIMAL(38,12)"}},
    )
    tasks.run_task(book, "origin")
    skills.distill(book, "decimal", "origin")
    assert skills.inspect_skill(book, "decimal")["package"]["contracts"]["data"][
        "types"
    ] == ["DECIMAL(38,12)"]
    table(book, "new", "n\n9007199254740996.000000000004\n")
    skills.use_skill(book, "decimal", "replay", {"data": "new"})
    replay = tasks.run_task(book, "replay")
    assert replay["steps"][0]["result"]["rows"] == [["9007199254740996.000000000004"]]


def test_original_updates_make_skill_stale_but_reusable(origin):
    book, run = origin
    skill = skills.distill(book, "total", "origin")
    table(book, body="n\n10\n20\n", expected_version=1)
    with pytest.raises(ValueError, match="current inputs"):
        skills.distill(book, "invalid", "origin", run_id=run["run_id"])
    inspected = skills.inspect_skill(book, skill["artifact_id"])
    assert inspected["origin_stale"] is True
    skills.use_skill(book, "total", "replay", {"data": "data"})
    assert tasks.run_task(book, "replay")["steps"][0]["result"]["rows"] == [[32]]


def test_old_skill_versions_replay_and_retain_exact_plan(origin):
    book, _ = origin
    first = skills.distill(book, "total", "origin", description="First exact plan")
    second = skills.distill(
        book, "total", "origin", description="Second description", expected_version=1
    )
    assert second["version"] == 2
    assert not skills.inspect_skill(book, first["artifact_id"])["is_latest"]
    skills.use_skill(book, first["artifact_id"], "old_replay", {"data": "data"})
    assert tasks.run_task(book, "old_replay")["steps"][0]["result"]["rows"] == [[7]]
    assert skills.find_skills(book)["skills"][0]["artifact_id"] == second["artifact_id"]


def test_resolved_binding_cannot_race_schema_validation(origin, monkeypatch):
    book, _ = origin
    skills.distill(book, "total", "origin")
    old_id = book.inspect("data")["artifact_id"]
    contract = skills._contract

    def changed(workbook, row, types=None):
        result = contract(workbook, row, types)
        table(workbook, body="n\n999\n", expected_version=1)
        return result

    monkeypatch.setattr(skills, "_contract", changed)
    replay = skills.use_skill(book, "total", "replay", {"data": "data"})
    assert replay["sources"] == {"data": old_id}
    result = tasks.run_task(book, "replay")
    assert result["steps"][0]["result"]["rows"] == [[7]]
    assert result["current"] is False


@pytest.mark.parametrize(
    "state,omission",
    [
        ("partial", "claim_lost"),
        ("superseded", None),
        ("running", None),
        ("complete", "no_matching_evidence"),
        ("complete", "brief_budget"),
    ],
)
def test_failed_or_omitted_ledger_cannot_be_distilled(origin, state, omission):
    book, run = origin
    with book.connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM task_runs WHERE run_id=?", (run["run_id"],)
            ).fetchone()[0]
        )
        payload["state"] = state
        payload["omitted"] = [{"reason": omission}] if omission else []
        db.execute(
            "UPDATE task_runs SET state=?,payload=? WHERE run_id=?",
            (state, json.dumps(payload), run["run_id"]),
        )
    with pytest.raises(ValueError):
        skills.distill(book, "bad", "origin")
    assert skills.find_skills(book)["skills"] == []


def test_retained_complete_rows_are_allowed_and_labeled(tmp_path):
    book = Workbook(tmp_path)
    table(book, body="n\n" + "\n".join(map(str, range(10))) + "\n")
    define(
        book,
        steps=[
            {
                "name": "all_rows",
                "inputs": ["data"],
                "sql": "SELECT * FROM input1 ORDER BY n",
            }
        ],
    )
    run = tasks.run_task(book, "origin")
    assert run["omitted"][0]["reason"] == "rows_retained_in_artifact"
    skills.distill(book, "all", "origin")
    package = skills.inspect_skill(book, "all")["package"]
    assert package["origin"]["retained_output_notices"] == run["omitted"]
    assert "rows" not in package["origin"]["results"]["all_rows"]


def test_unregistered_copy_and_corrupted_package_are_rejected(origin):
    book, _ = origin
    skill = skills.distill(book, "total", "origin")
    package = skills.inspect_skill(book, "total")["package"]
    fake = book.save_text(
        json.dumps(package),
        name="_skills/fake",
        kind="skill",
        media_type="application/json",
    )
    with pytest.raises(ValueError, match="registered"):
        skills.inspect_skill(book, fake["artifact_id"])
    with book.connect() as db:
        row = book._get(db, skill["artifact_id"])
    (book.blobs / row["digest"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="invalid|integrity"):
        skills.inspect_skill(book, "total")
    assert skills.find_skills(book)["skills"] == []


def test_actor_identity_and_roles(origin):
    book, _ = origin
    principal = Principal("distiller")
    skill = skills.distill(book, "total", "origin", principal=principal)
    package = skills.inspect_skill(book, "total")["package"]
    assert package["distilled_by"] == {"identity": "distiller", "role": "writer"}
    assert package["origin"]["producer"]["identity"] == "local"
    with acting_as(Principal("viewer", "reader")):
        assert (
            skills.inspect_skill(book, "total")["artifact_id"] == skill["artifact_id"]
        )
        with pytest.raises(PermissionError):
            skills.distill(book, "no", "origin")
        with pytest.raises(PermissionError):
            skills.use_skill(book, "total", "no", {"data": "data"})
    skills.use_skill(
        book, "total", "replay", {"data": "data"}, principal=Principal("reuser")
    )
    with book.connect() as db:
        row = book._get(db, "_tasks/replay/definition")
        assert (
            db.execute(
                "SELECT actor FROM coordination_events WHERE kind='artifact.created' AND resource=?",
                (row["artifact_id"],),
            ).fetchone()[0]
            == "reuser"
        )


def test_proof_publication_failure_is_fail_closed_and_retry_repairs(origin):
    book, _ = origin
    with book.connect() as db:
        db.execute(
            "CREATE TRIGGER fail_skill BEFORE INSERT ON coordination_events WHEN NEW.kind='skill.distilled' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        skills.distill(book, "total", "origin")
    with pytest.raises(ValueError, match="registered"):
        skills.inspect_skill(book, "total")
    assert skills.find_skills(book)["skills"] == []
    with book.connect() as db:
        db.execute("DROP TRIGGER fail_skill")
    repaired = skills.distill(book, "total", "origin")
    assert repaired["version"] == 1


def test_concurrent_cas_has_one_winner(origin):
    book, _ = origin
    skills.distill(book, "total", "origin")
    barrier = Barrier(2)

    def attempt(number):
        barrier.wait(timeout=5)
        try:
            return skills.distill(
                book,
                "total",
                "origin",
                description=f"Version {number}",
                expected_version=1,
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert len([value for value in results if value]) == 1
    assert skills.inspect_skill(book, "total")["version"] == 2


def test_definition_tamper_is_rejected(origin):
    book, run = origin
    with book.connect() as db:
        definition = book._get(db, run["definition_id"])
    (book.blobs / definition["digest"]).write_text("{}")
    with pytest.raises(ValueError, match="corrupted"):
        skills.distill(book, "bad", "origin")


def test_old_skill_and_binding_are_retained_after_apply(origin):
    book, _ = origin
    first = skills.distill(book, "total", "origin")
    table(book, "new", "n\n40\n")
    bound = book.inspect("new")["artifact_id"]
    applied = skills.use_skill(book, "total", "replay", {"data": "new"})
    definition = book.inspect(applied["artifact_id"])
    assert first["artifact_id"] in definition["provenance"]
    assert bound in definition["dependencies"]
    skills.distill(
        book, "total", "origin", description="Updated description", expected_version=1
    )
    table(book, "new", "n\n999\n", expected_version=1)
    book.prune(keep=1)
    assert book.inspect(first["artifact_id"])["artifact_id"] == first["artifact_id"]
    assert book.inspect(bound)["artifact_id"] == bound
    assert tasks.run_task(book, "replay")["steps"][0]["result"]["rows"] == [[42]]


def test_origin_change_during_distillation_cannot_publish_proof(origin, monkeypatch):
    book, _ = origin
    contract = skills._contract

    def changed(workbook, row, types=None):
        value = contract(workbook, row, types)
        table(workbook, body="n\n9\n", expected_version=1)
        return value

    monkeypatch.setattr(skills, "_contract", changed)
    with pytest.raises(ValueError, match="current inputs"):
        skills.distill(book, "bad", "origin")
    assert skills.find_skills(book)["skills"] == []
    with pytest.raises(ValueError, match="registered"):
        skills.inspect_skill(book, "bad")


def test_duplicate_materialized_results_preserve_step_order(tmp_path):
    book = Workbook(tmp_path)
    table(book)
    define(
        book,
        steps=[
            {
                "name": name,
                "inputs": ["data"],
                "sql": "SELECT sum(n)::BIGINT AS n FROM input1",
            }
            for name in ("first", "second")
        ],
    )
    run = tasks.run_task(book, "origin")
    assert run["steps"][0]["artifact_id"] == run["steps"][1]["artifact_id"]
    skills.distill(book, "twice", "origin")
    assert skills.inspect_skill(book, "twice")["steps"] == ["first", "second"]


def test_retained_result_corruption_cannot_be_distilled(origin):
    book, run = origin
    with book.connect() as db:
        row = book._get(db, run["steps"][0]["artifact_id"])
    path = book.blobs / row["digest"]
    data = path.read_bytes()
    path.write_bytes(b"BAD!" + data[4:])
    with pytest.raises(ValueError, match="integrity"):
        skills.distill(book, "bad", "origin")


def test_retained_snapshot_tamper_invalidates_loaded_proof(origin):
    book, run = origin
    skills.distill(book, "total", "origin")
    with book.connect() as db:
        row = book._get(db, run["artifact_id"])
    path = book.blobs / row["digest"]
    data = path.read_bytes()
    path.write_bytes(b" " + data[1:])
    with pytest.raises(ValueError, match="integrity"):
        skills.inspect_skill(book, "total")


def test_stale_origin_is_not_a_computational_dependency_of_new_task(origin):
    book, _ = origin
    skills.distill(book, "total", "origin")
    table(book, body="n\n99\n", expected_version=1)
    table(book, "fresh", "n\n40\n")
    applied = skills.use_skill(book, "total", "replay", {"data": "fresh"})
    assert skills.inspect_skill(book, "total")["origin_stale"] is True
    assert book.inspect(applied["artifact_id"])["stale_inputs"] is False
    run = tasks.run_task(book, "replay")
    assert run["state"] == "complete" and run["current"] is True
    assert book.inspect(run["artifact_id"])["stale_inputs"] is False
    assert run["steps"][0]["result"]["rows"] == [[42]]
