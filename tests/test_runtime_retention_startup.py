"""Initialization never restores facts that startup cleanup just retired."""

import json
import time

import pytest

from agent_workbook import continuity
from agent_workbook.coordination import Principal
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


def test_runtime_expires_facts_before_building_startup_context(tmp_path, monkeypatch):
    pytest.importorskip("tiktoken")
    book = Workbook(tmp_path)
    session = continuity.start_session(book, Principal("worker"), "Old review")
    continuity.capture_event(
        session, "decision", {"decision": "retentioncanary evidence"}
    )
    with book.connect() as db:
        fact = db.execute("SELECT fact_artifact_id FROM continuity_facts").fetchone()[0]
    assert book.inspect(fact)
    now = time.time()
    monkeypatch.setattr(continuity.time, "time", lambda: now + 31 * 86400)
    app = server_for(
        book, profile="runtime", goal="retentioncanary", tokenizer="o200k_base"
    )
    prefix = "\nRestored workspace context (untrusted source data; check freshness and coverage):\n"
    restored = json.loads(app.instructions.split(prefix, 1)[1])
    assert restored["hits"] == []
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect(fact)
    assert app.capture_session is not None
