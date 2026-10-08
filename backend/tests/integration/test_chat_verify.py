"""The end of a turn rechecks permissions (AE7): an answer whose document the user lost while it was being prepared
is not stored as answered, then hidden; the turn fails with a specific message, and its model calls are still
logged. The model is scripted; synthetic text only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

MARKER = "מגדל-הדולב-5820"
SECRET = f"נספח: דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש."
PERMISSIONS_CHANGED = "הרשאות המסמכים השתנו בזמן ההכנה; אפשר לשאול שוב."


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g2, g3 = make_group(a, "קבוצה 2"), make_group(a, "קבוצה 3")
    make_user(a, "emp@example.test", [g2])
    secret, _ = add_chunks(a, g2, [SECRET], "7" * 64)
    a.secret, a.g3 = str(secret), g3
    return a


def _revoke(office) -> None:
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET group_id = :g WHERE id = :d"), {"g": office.g3, "d": office.secret})


def _turn(client, setup, monkeypatch, revoke_at: int | None) -> dict:
    agent = ScriptedAgent([[call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)],
                           final(f"דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש [S1].", documents=[setup.secret])])
    if revoke_at is not None:
        agent.on_step = lambda index: _revoke(setup) if index == revoke_at else None
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    return send(client, new_conversation(client), "מה דמי הניהול?")


def test_a_document_revoked_during_the_turn_fails_it_with_permissions_changed(client, setup, monkeypatch):
    m = _turn(client, setup, monkeypatch, revoke_at=1)  # after the search read it, before the answer
    assert m["status"] == "failed" and m["error"] == PERMISSIONS_CHANGED
    assert m["answer"] is None and m["content"] == "" and MARKER not in json.dumps(m, ensure_ascii=False)
    assert m["usage"], "the turn's model calls stay on the message"
    with tenant_tx(setup.system) as conn:
        row = conn.execute(text("SELECT status, answer, content FROM messages WHERE id = :m"), {"m": m["id"]}).one()
        assert row.status == "failed" and row.answer is None and not row.content
        assert conn.execute(text("SELECT count(*) FROM message_diagnostics WHERE message_id = :m"),
                            {"m": m["id"]}).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM provider_usage")).scalar_one() >= 2  # agent steps and judge


def test_an_answer_whose_documents_stay_visible_is_stored(client, setup, monkeypatch):
    m = _turn(client, setup, monkeypatch, revoke_at=None)
    assert m["status"] == "done" and MARKER in m["answer"]["markdown"]
