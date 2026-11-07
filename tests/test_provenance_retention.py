"""Historical attribution retains artifacts without becoming computational input."""

import json
import sqlite3

import pytest

from agent_workbook.store import Workbook


def note(book, name, content="record", **options):
    return book.save_text(
        content, name=name, kind="note", media_type="text/plain", **options
    )


def counts(book):
    with book.connect() as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "artifacts",
                "artifact_search",
                "dependencies",
                "provenance_links",
                "coordination_events",
                "name_versions",
            )
        }


def test_provenance_resolves_deduplicates_and_audits_without_changing_empty_metadata(
    tmp_path,
):
    book = Workbook(tmp_path)
    first = note(book, "first")
    second = note(book, "second")
    owner = note(book, "owner", provenance=["second", first["artifact_id"], "second"])
    refs = [second["artifact_id"], first["artifact_id"]]
    inspected = book.inspect_many(["owner", "first"])
    assert inspected[0]["provenance"] == refs
    assert inspected[0]["dependencies"] == []
    assert "provenance" not in inspected[1]
    with book.connect() as db:
        assert Workbook._provenance(db, owner["artifact_id"]) == refs
        events = {
            row["resource"]: json.loads(row["details"])
            for row in db.execute(
                "SELECT * FROM coordination_events WHERE kind='artifact.created'"
            )
        }
    assert events[owner["artifact_id"]]["provenance"] == refs
    assert "provenance" not in events[first["artifact_id"]]


def test_retained_origin_survives_prune_and_delete_until_owner_removed(tmp_path):
    book = Workbook(tmp_path)
    origin = note(book, "origin", "old source")
    note(book, "origin", "new source", expected_version=1)
    owner = note(book, "owner", provenance=[origin["artifact_id"]])
    assert book.prune()["removed"] == []
    for ref, options in (
        (origin["artifact_id"], {}),
        ("origin", {"expected_version": 2}),
    ):
        with pytest.raises(ValueError, match="Still pinned by owner v1"):
            book.delete(ref, **options)
    assert (
        book.inspect(origin["artifact_id"], excerpt_bytes=100)["excerpt"]
        == "old source"
    )
    book.delete("owner", expected_version=1)
    assert [row["artifact_id"] for row in book.prune()["removed"]] == [
        origin["artifact_id"]
    ]
    with book.connect() as db:
        assert Workbook._provenance(db, owner["artifact_id"]) == []
    assert book.inspect("origin")["version"] == 2


def test_historical_skill_does_not_contaminate_new_result_freshness(tmp_path):
    book = Workbook(tmp_path)
    old_source = note(book, "training-source")
    skill = note(book, "skill", dependencies=[old_source["artifact_id"]])
    new_source = note(book, "new-source")
    task = note(
        book,
        "task",
        dependencies=[new_source["artifact_id"]],
        provenance=[skill["artifact_id"]],
    )
    result = note(
        book, "result", dependencies=[task["artifact_id"], new_source["artifact_id"]]
    )
    note(book, "training-source", "unrelated update", expected_version=1)
    assert book.inspect("skill")["stale_inputs"] is True
    assert book.inspect("task")["stale_inputs"] is False
    assert book.inspect(result["artifact_id"])["stale_inputs"] is False
    with book.connect() as db:
        assert Workbook._dependencies(db, task["artifact_id"]) == [
            new_source["artifact_id"]
        ]
    note(book, "new-source", "actual input changed", expected_version=1)
    assert book.inspect(result["artifact_id"])["stale_inputs"] is True


def test_provenance_does_not_create_computational_lineage_cycle(tmp_path):
    book = Workbook(tmp_path)
    old = note(book, "source")
    skill = note(book, "skill", dependencies=[old["artifact_id"]])
    updated = note(
        book,
        "source",
        "new source",
        expected_version=1,
        provenance=[skill["artifact_id"]],
    )
    assert book.inspect(updated["artifact_id"])["stale_inputs"] is False
    assert book.inspect("skill")["stale_inputs"] is True
    with pytest.raises(ValueError, match="dependency cycle"):
        note(
            book,
            "source",
            "invalid",
            expected_version=2,
            dependencies=[skill["artifact_id"]],
        )


def test_prune_fixed_point_releases_retention_chain_and_dry_run_rolls_back(tmp_path):
    book = Workbook(tmp_path)
    origin = note(book, "origin", "old")
    skill = note(book, "skill", dependencies=[origin["artifact_id"]])
    owner = note(book, "owner", provenance=[skill["artifact_id"]])
    note(book, "origin", "new", expected_version=1)
    note(book, "skill", "new plan", expected_version=1)
    assert book.prune()["removed"] == []
    note(book, "owner", "replacement without historical origin", expected_version=1)
    expected = {origin["artifact_id"], skill["artifact_id"], owner["artifact_id"]}
    before = counts(book)
    preview = book.prune(dry_run=True)
    assert {row["artifact_id"] for row in preview["removed"]} == expected
    assert counts(book) == before
    assert book.inspect(owner["artifact_id"])["provenance"] == [skill["artifact_id"]]
    assert {row["artifact_id"] for row in book.prune()["removed"]} == expected


@pytest.mark.parametrize("failed_table", ["provenance_links", "coordination_events"])
def test_failed_publication_rolls_back_artifact_links_event_search_and_version(
    tmp_path, failed_table
):
    book = Workbook(tmp_path)
    origin = note(book, "origin")
    before = counts(book)
    with book.connect() as db:
        db.execute(
            f"CREATE TRIGGER fail_provenance BEFORE INSERT ON {failed_table} BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        note(
            book,
            "owner",
            provenance=[origin["artifact_id"]],
            dependencies=[origin["artifact_id"]],
        )
    assert counts(book) == before
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect("owner")
    with book.connect() as db:
        db.execute("DROP TRIGGER fail_provenance")
    assert note(book, "owner", provenance=[origin["artifact_id"]])["version"] == 1


def test_legacy_delete_with_foreign_keys_cannot_remove_retained_origin(tmp_path):
    book = Workbook(tmp_path)
    origin = note(book, "origin")
    note(book, "owner", provenance=[origin["artifact_id"]])
    with (
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
        book.connect() as db,
    ):
        for table in ("dependencies", "artifact_search", "artifacts"):
            db.execute(
                f"DELETE FROM {table} WHERE artifact_id=?", (origin["artifact_id"],)
            )
    assert book.inspect("origin")["artifact_id"] == origin["artifact_id"]
    assert any(
        row["artifact_id"] == origin["artifact_id"] for row in book.find("origin")
    )


def test_owner_foreign_key_cascade_and_whole_name_delete_release_internal_links(
    tmp_path,
):
    book = Workbook(tmp_path)
    first = note(book, "entry")
    second = note(book, "entry", expected_version=1, provenance=[first["artifact_id"]])
    book.delete("entry", expected_version=2)
    with book.connect() as db:
        assert Workbook._provenance(db, second["artifact_id"]) == []
    origin = note(book, "origin")
    owner = note(book, "owner", provenance=[origin["artifact_id"]])
    with book.connect() as db:
        db.execute("DELETE FROM artifacts WHERE artifact_id=?", (owner["artifact_id"],))
        assert Workbook._provenance(db, owner["artifact_id"]) == []


@pytest.mark.parametrize("refs", [["missing"], [""], [None], "origin", ["origin"] * 17])
def test_invalid_provenance_leaves_no_catalog_or_audit_mutations(tmp_path, refs):
    book = Workbook(tmp_path)
    note(book, "origin")
    before = counts(book)
    with pytest.raises(ValueError):
        note(book, "owner", provenance=refs)
    assert counts(book) == before
