"""Migration 0008: removed-claim detail stored in answers moves to ``message_diagnostics``; the answer keeps
counts only, and row security stays forced on both tables. Synthetic text only."""

import json

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_office

pytestmark = pytest.mark.db

OLD = {"kind": "rag", "status": "partial", "markdown": "השווי 9,500 ₪ [S1].",
       "sources": [{"id": "S1", "document_id": "11111111-1111-1111-1111-111111111111"}],
       "touched_documents": ["22222222-2222-2222-2222-222222222222"],
       "verification": {"judged": True, "judge_status": "ok",
                        "problems": [{"text": "דמי הניהול 41", "reason": "סוד", "severity": "error"},
                                     {"text": "השווי", "reason": "חלקי", "severity": "partial"}],
                        "rounds": [[{"text": "דמי הניהול 41", "reason": "סוד", "severity": "error"}]]}}


def test_old_answers_move_their_detail_to_diagnostics(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0007")
        reset_engine()
        with tenant_tx(a.ctx()) as conn:
            cid = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                    " (app_office(), :u, 'ש', 'rag') RETURNING id"), {"u": a.admin_id}).scalar_one()
            mid = conn.execute(text(
                "INSERT INTO messages (office_id, conversation_id, user_id, role, status, answer) VALUES"
                " (app_office(), :c, :u, 'assistant', 'done', CAST(:a AS jsonb)) RETURNING id"),
                {"c": cid, "u": a.admin_id, "a": json.dumps(OLD, ensure_ascii=False)}).scalar_one()
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    with tenant_tx(a.ctx()) as conn:
        v = conn.execute(text("SELECT answer->'verification' FROM messages WHERE id = :m"), {"m": mid}).scalar_one()
        d = conn.execute(text("SELECT * FROM message_diagnostics WHERE message_id = :m"), {"m": mid}).one()
    assert v == {"judged": True, "judge_status": "ok", "removed": 1, "partial": 1, "annotated": 0}
    assert d.removed[0]["text"] == "דמי הניהול 41" and d.rounds[0][0]["reason"] == "סוד"
    assert set(d.document_ids) == {"11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"}
    with db.connect() as conn:
        forced = conn.execute(text("SELECT relname, relforcerowsecurity FROM pg_class WHERE relname IN"
                                   " ('messages', 'message_diagnostics')")).all()
    assert dict(forced) == {"messages": True, "message_diagnostics": True}


def test_a_downgrade_puts_the_detail_back(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0007")
        reset_engine()
        with tenant_tx(a.ctx()) as conn:
            cid = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                    " (app_office(), :u, 'ש', 'rag') RETURNING id"), {"u": a.admin_id}).scalar_one()
            mid = conn.execute(text(
                "INSERT INTO messages (office_id, conversation_id, user_id, role, status, answer) VALUES"
                " (app_office(), :c, :u, 'assistant', 'done', CAST(:a AS jsonb)) RETURNING id"),
                {"c": cid, "u": a.admin_id, "a": json.dumps(OLD, ensure_ascii=False)}).scalar_one()
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0007")
        reset_engine()
        with tenant_tx(a.ctx()) as conn:
            v = conn.execute(text("SELECT answer->'verification' FROM messages WHERE id = :m"),
                             {"m": mid}).scalar_one()
        assert v["problems"] == OLD["verification"]["problems"] and v["rounds"] == OLD["verification"]["rounds"]
        assert "removed" not in v
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
