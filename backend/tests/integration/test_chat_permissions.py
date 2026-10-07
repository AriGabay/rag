"""Permissions inside the conversation's own memory: the summary, the history, focus documents and earlier
references never bring back a document the user can no longer see.

A long conversation gets a summary; then a document it relied on is moved to a group the employee is not in
(or deleted, or the employee's groups change). The next turn is inspected end to end: everything the model
was given (context, tool outputs, judge input), the answer, and the summary stored after the turn. The marker
text below is unique to the revoked document, so its absence is checked literally.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.db import tenant_tx
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

MARKER = "קרן-השקמה-4471"
SECRET = f"נספח: דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש."
OPEN = "סיכום: השווי למ\"ר בנוי במבנה הגפן הוא 9,500 ₪."


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    # a summary after a few messages, so a short test conversation has one
    monkeypatch.setattr(chat_api, "HISTORY_MESSAGES", 2)
    monkeypatch.setattr(chat_api, "SUMMARY_EVERY", 2)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g2, g3 = make_group(a, "קבוצה 2"), make_group(a, "קבוצה 3")
    make_user(a, "emp@example.test", [g2])
    secret, _ = add_chunks(a, g2, [SECRET], "1" * 64)
    public, _ = add_chunks(a, g2, [OPEN], "2" * 64)
    a.secret, a.public, a.g2, a.g3 = str(secret), str(public), g2, g3
    return a


def _agent(steps: list) -> ScriptedAgent:
    agent = ScriptedAgent(steps)
    # the summarizer echoes what it was given, so whatever reaches it shows up in the stored summary
    agent.on(Purpose.AGENT, lambda instructions, input: {"summary": input[-3000:]}, repeat=True)
    return agent


def _secret_turn(doc: str) -> list:
    return [[call("search", query="דמי ניהול", document_ids=[doc], limit=None)],
            final(f"דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש [S1].", documents=[doc])]


def _public_turn(doc: str) -> list:
    return [[call("search", query="שווי למ\"ר", document_ids=[doc], limit=None)],
            final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", documents=[doc])]


def _conversation(client, office, monkeypatch, turns: int) -> tuple[str, ScriptedAgent]:
    steps = []
    for i in range(turns):
        steps += _secret_turn(office.secret) if i % 2 == 0 else _public_turn(office.public)
    agent = _agent(steps)
    cloud(monkeypatch, office, agent)
    login(client, "emp@example.test")
    cid = new_conversation(client)
    for i in range(turns):
        m = send(client, cid, f"שאלה מספר {i + 1}")
        assert m["status"] == "done", m
    return cid, agent


def _summary(office, cid: str):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT summary, summary_meta FROM conversations WHERE id = :c"), {"c": cid}).one()


def _everything_the_model_saw(agent: ScriptedAgent, since_step: int, since_call: int) -> str:
    seen = [str(items) for items in agent.seen[since_step:]]
    seen += [c.input for c in agent.calls[since_call:]]
    return "\n".join(seen)


def _follow_up(client, office, agent: ScriptedAgent, cid: str) -> tuple[dict, str]:
    step0, call0 = len(agent.seen), len(agent.calls)
    agent.steps += _public_turn(office.public)
    m = send(client, cid, "ומה עוד ידוע?")
    assert m["status"] == "done", m
    return m, _everything_the_model_saw(agent, step0, call0)


def _revoke(office, doc: str) -> None:
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET group_id = :g WHERE id = :d"), {"g": office.g3, "d": doc})


def test_revoked_document_never_returns_through_the_summary(client, setup, monkeypatch):
    cid, agent = _conversation(client, setup, monkeypatch, 4)
    before = _summary(setup, cid)
    assert before.summary and MARKER in before.summary  # the summary did fold the secret answer
    assert setup.secret in before.summary_meta["document_ids"]

    _revoke(setup, setup.secret)
    m, seen = _follow_up(client, setup, agent, cid)

    assert MARKER not in seen  # context, history, summary, tool outputs, judge input
    assert MARKER not in (m["answer"]["markdown"] or "")
    after = _summary(setup, cid)
    assert MARKER not in (after.summary or "")
    assert setup.secret not in after.summary_meta["document_ids"]
    assert not after.summary_meta.get("invalid")


def test_deleted_document_never_returns_through_the_summary(client, setup, monkeypatch):
    cid, agent = _conversation(client, setup, monkeypatch, 4)
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": setup.secret})
    m, seen = _follow_up(client, setup, agent, cid)
    assert MARKER not in seen and MARKER not in (m["answer"]["markdown"] or "")
    assert MARKER not in (_summary(setup, cid).summary or "")


def test_a_summary_without_its_record_is_not_shown_and_is_rebuilt(client, setup, monkeypatch):
    cid, agent = _conversation(client, setup, monkeypatch, 4)
    # a summary written before summaries recorded their sources
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE conversations SET summary = :s, summary_meta = NULL WHERE id = :c"),
                     {"s": f"סיכום ישן: {MARKER}", "c": cid})
    _revoke(setup, setup.secret)
    m, seen = _follow_up(client, setup, agent, cid)
    assert "סיכום ישן" not in seen and MARKER not in seen
    after = _summary(setup, cid)
    assert "סיכום ישן" not in (after.summary or "") and after.summary_meta is not None


def test_a_summary_built_under_other_groups_is_not_used(client, setup, monkeypatch):
    cid, agent = _conversation(client, setup, monkeypatch, 4)
    # the employee gains a group: every document is still visible, but the scope the summary was built under
    # is not the scope of this turn
    with tenant_tx(setup.system) as conn:
        uid = conn.execute(text("SELECT id FROM users WHERE email = 'emp@example.test'")).scalar_one()
        conn.execute(text("INSERT INTO user_groups (user_id, group_id, office_id) VALUES (:u, :g, app_office())"),
                     {"u": uid, "g": setup.g3})
    login(client, "emp@example.test")
    m, seen = _follow_up(client, setup, agent, cid)
    assert "סיכום השיחה עד כה" not in seen


def test_a_valid_summary_is_used_and_extended(client, setup, monkeypatch):
    cid, agent = _conversation(client, setup, monkeypatch, 4)
    m, seen = _follow_up(client, setup, agent, cid)
    assert "סיכום השיחה עד כה" in seen  # nothing changed: the summary is given to the model
    after = _summary(setup, cid)
    assert {setup.secret, setup.public} <= set(after.summary_meta["document_ids"])


def test_a_document_only_in_scope_hides_its_message_once_revoked(client, setup, monkeypatch):
    # an answer whose coverage note named a document (never cited) keeps nothing of it once it is revoked
    agent = _agent([[call("search", query="שווי", document_ids=[setup.public], limit=None)],
                    final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", documents=[setup.public])])
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    cid = new_conversation(client)
    m = send(client, cid, "מה השווי?")
    with tenant_tx(setup.system) as conn:
        answer = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        answer["ledger"] = {"matching": [{"document_id": setup.secret, "title": MARKER}], "complete": True}
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb), content = content || :t WHERE id = :m"),
                     {"a": __import__("json").dumps(answer, ensure_ascii=False), "t": f" (גם {MARKER})",
                      "m": m["id"]})
    _revoke(setup, setup.secret)
    shown = client.get(f"/api/chat/messages/{m['id']}").json()
    assert shown["answer"].get("hidden") is True and MARKER not in str(shown)
    m2, seen = _follow_up(client, setup, agent, cid)
    assert MARKER not in seen


def test_users_own_words_stay_in_the_summary(client, setup, monkeypatch):
    # what the user typed is theirs: it stays in the rebuilt summary even when it names a revoked document
    cid, agent = _conversation(client, setup, monkeypatch, 1)
    agent.steps += _public_turn(setup.public) * 3
    for q in ("מה ידוע על מגדל השקמה?", "ומה עוד?", "תודה"):
        send(client, cid, q)
    _revoke(setup, setup.secret)
    _follow_up(client, setup, agent, cid)
    assert "מגדל השקמה" in (_summary(setup, cid).summary or "")


# --- the conversation's focus ---------------------------------------------------------------------------------

def _focus(doc: str, subject: str = "מבנה הגפן") -> dict:
    return {"metric_as_written": "דמ\"ש ראויים למ\"ר", "metric_kind": "rent_per_area", "unit": "ILS_per_sqm",
            "period": "month", "area_basis": "בנוי", "vat": "unknown", "subject": subject,
            "value_role": "appraiser_determination", "document_ids": [doc]}


def test_the_next_turn_gets_the_previous_answers_focus(client, setup, monkeypatch):
    agent = _agent([[call("search", query="שווי", document_ids=[setup.public], limit=None)],
                    final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", focus=_focus(setup.public))]
                   + _public_turn(setup.public))
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    cid = new_conversation(client)
    send(client, cid, "מה דמי השכירות?")
    send(client, cid, "התכוונתי לשווי")
    context = str(agent.seen[2])
    assert "הנתון שבמרכז השיחה" in context
    for value in ("דמי שכירות ליחידת שטח", "₪ למ״ר", "לחודש", "בנוי", "מבנה הגפן", setup.public,
                  "שנה רק את מה שתוקן"):
        assert value in context, value


def test_a_focus_on_a_revoked_document_is_dropped_whole(client, setup, monkeypatch):
    agent = _agent([[call("search", query="ניהול", document_ids=[setup.secret], limit=None)],
                    final("דמי הניהול הם 33 ₪ למ\"ר לחודש [S1].", focus=_focus(setup.secret, f"מגדל {MARKER}"))]
                   + _public_turn(setup.public))
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    cid = new_conversation(client)
    send(client, cid, "מה דמי הניהול?")
    _revoke(setup, setup.secret)
    send(client, cid, "ומה לגבי זה?")
    context = str(agent.seen[2])
    assert "הנתון שבמרכז השיחה" not in context and MARKER not in context


def test_a_focus_document_the_turn_never_touched_is_not_stored(client, setup, monkeypatch):
    agent = _agent([[call("search", query="שווי", document_ids=[setup.public], limit=None)],
                    final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].",
                          focus={**_focus(setup.public), "document_ids": [setup.public, setup.secret]})])
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["answer"]["focus"]["document_ids"] == [setup.public]


def test_a_document_only_touched_by_the_turn_hides_its_message_once_revoked(client, setup, monkeypatch):
    # the turn read the secret document (its claim was removed in verification), then answered from another one
    agent = _agent([[call("search", query="ניהול", document_ids=[setup.secret], limit=None)],
                    [call("search", query="שווי", document_ids=[setup.public], limit=None)],
                    final("השווי למ\"ר בנוי הוא 9,500 ₪ [S2].", documents=[setup.public])])
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה ידוע?")
    assert setup.secret in m["answer"]["touched_documents"]
    _revoke(setup, setup.secret)
    shown = client.get(f"/api/chat/messages/{m['id']}").json()
    assert shown["answer"].get("hidden") is True


def test_a_failing_summary_never_turns_a_finished_answer_into_a_failure(client, setup, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("summary store down")

    monkeypatch.setattr(chat_api, "_maybe_summarize", boom)
    cid, agent = _conversation(client, setup, monkeypatch, 2)
    rows = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]
    assert [r["status"] for r in rows if r["role"] == "assistant"] == ["done", "done"]
