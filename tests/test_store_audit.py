"""Artifact mutation and trusted provenance must commit or roll back together."""

import asyncio
import hashlib
import json
import sqlite3

import pytest

from agent_workbook.coordination import Principal, acting_as, current_principal
from agent_workbook.store import Workbook


def events(book):
    with book.connect() as db:
        return [
            dict(row) | {"details": json.loads(row["details"])}
            for row in db.execute(
                "SELECT * FROM coordination_events WHERE kind='artifact.created' ORDER BY event_id"
            )
        ]


def test_creation_audit_has_exact_metadata_and_trusted_actor_without_body(tmp_path):
    book = Workbook(tmp_path)
    with acting_as(Principal("alice")):
        source = book.save_text(
            "private source body",
            name="source",
            kind="document",
            media_type="text/plain",
        )
    with acting_as(Principal("bob")):
        result = book.save_text(
            "private result body",
            name="result",
            kind="note",
            media_type="text/plain",
            dependencies=[source["artifact_id"]],
        )
    captured = events(book)
    assert [row["actor"] for row in captured] == ["alice", "bob"]
    assert [row["role"] for row in captured] == ["writer", "writer"]
    assert captured[1]["resource"] == result["artifact_id"]
    assert captured[1]["details"] == {
        "artifact_id": result["artifact_id"],
        "name": "result",
        "version": 1,
        "sha256": hashlib.sha256(b"private result body").hexdigest(),
        "size_bytes": len(b"private result body"),
        "dependencies": [source["artifact_id"]],
    }
    assert "private" not in json.dumps(captured)


def test_audit_trigger_failure_rolls_back_artifact_search_dependencies_and_version(
    tmp_path,
):
    book = Workbook(tmp_path)
    first = book.save_text("one", name="entry", kind="note", media_type="text/plain")
    with book.connect() as db:
        db.execute(
            "CREATE TRIGGER reject_creation_audit BEFORE INSERT ON coordination_events WHEN NEW.kind='artifact.created' BEGIN SELECT RAISE(ABORT,'injected audit failure'); END"
        )
    with (
        acting_as(Principal("alice")),
        pytest.raises(sqlite3.IntegrityError, match="audit failure"),
    ):
        book.save_text(
            "two",
            name="entry",
            kind="note",
            media_type="text/plain",
            expected_version=1,
        )
    assert book.inspect("entry")["artifact_id"] == first["artifact_id"]
    assert len(events(book)) == 1
    with book.connect() as db:
        assert db.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM artifact_search").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM dependencies").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT version FROM name_versions WHERE name='entry'"
            ).fetchone()[0]
            == 1
        )
        db.execute("DROP TRIGGER reject_creation_audit")
    with acting_as(Principal("alice")):
        retried = book.save_text(
            "two",
            name="entry",
            kind="note",
            media_type="text/plain",
            expected_version=1,
        )
    assert retried["version"] == 2
    assert events(book)[-1]["actor"] == "alice"


@pytest.mark.parametrize("role", ["reader", "approver"])
def test_nonwriter_denied_before_stream_read_or_staging(tmp_path, role):
    book = Workbook(tmp_path)

    class Unreadable:
        def read(self, size):
            pytest.fail("Unauthorized save reached source I/O")

    with (
        acting_as(Principal("restricted", role)),
        pytest.raises(PermissionError, match="cannot write"),
    ):
        book._save(Unreadable(), name="forbidden", kind="note", media_type="text/plain")
    assert not events(book)
    assert list(book.blobs.iterdir()) == []
    assert not list(tmp_path.glob(".workspool-stage-*"))


def test_async_and_thread_contexts_isolate_identities_on_shared_workbook(tmp_path):
    book = Workbook(tmp_path)

    async def run():
        ready = asyncio.Event()

        async def writer(number):
            actor = "writer" + str(number)
            with acting_as(Principal(actor)):
                await ready.wait()
                await asyncio.sleep(0)
                assert current_principal().identity == actor
                result = await asyncio.to_thread(
                    book.save_text,
                    actor,
                    name=actor,
                    kind="note",
                    media_type="text/plain",
                )
                assert current_principal().identity == actor
                return result

        tasks = [asyncio.create_task(writer(number)) for number in range(8)]
        ready.set()
        return await asyncio.gather(*tasks)

    saved = asyncio.run(run())
    observed = {event["resource"]: event["actor"] for event in events(book)}
    assert observed == {item["artifact_id"]: item["name"] for item in saved}
    assert current_principal() == Principal("local")


def test_nested_principal_restores_after_exception_and_local_default_is_compatible(
    tmp_path,
):
    book = Workbook(tmp_path)
    with acting_as(Principal("outer")):
        with (
            pytest.raises(RuntimeError, match="fixture"),
            acting_as(Principal("inner")),
        ):
            assert current_principal().identity == "inner"
            raise RuntimeError("fixture")
        assert current_principal().identity == "outer"
    book.save_text("local", name="default", kind="note", media_type="text/plain")
    assert events(book)[0]["actor"] == "local"
    with (
        pytest.raises(PermissionError, match="configured principal"),
        acting_as({"identity": "forged", "role": "writer"}),
    ):
        pytest.fail("An untrusted dict became a principal")
