"""Initialization never restores facts that startup cleanup just retired."""

import asyncio
import time

import pytest

from agent_workbook import continuity
from agent_workbook.coordination import Principal
from agent_workbook.render import decode
from agent_workbook.server import server_for
from agent_workbook.store import Workbook


def test_runtime_expires_facts_before_explicit_recall(tmp_path, monkeypatch):
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
    assert "retentioncanary" not in app.instructions
    result = asyncio.run(app.call_tool("recall", {}))
    assert not result.isError
    restored = decode(result.content)
    assert restored["hits"] == []
    with pytest.raises(ValueError, match="Unknown artifact"):
        book.inspect(fact)
    assert app.capture_session is not None
