"""Conversational chat: the tool loop, verification, conversation state, isolation, cancellation and limits.

The model is scripted (``ScriptedAgent``): these tests prove the server's behaviour — what the model is shown,
which tools it may use, how its answer is checked, stored and shown — not the model's quality (the real-model
runs are reported separately)."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from app.providers.llm import CallStatus
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

TEXTS = [
    "סיכום: ראוי לקבוע את השווי למ\"ר בנוי ברוטו למסחר בגבולות של 9,500 ₪, ללא מע\"מ ודמ\"ש ראויים למ\"ר בגבולות של 55 ₪ למ\"ר/חודש.",
    "תיאור הנכס: מבנה מסחרי בן 3 קומות ברחוב הגפן, בקרבת פארק.",
    "הערות: העסקאות המוצגות הן לנכסים חדשים באזור.",
]


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    # turns run inside the request (no background thread), so a test sees the finished message
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    g2 = make_group(a, "קבוצה 2")
    make_user(a, "emp@example.test", [g2])
    doc, ver = add_chunks(a, a.default_group_id, TEXTS, "1" * 64)
    add_chunks(b, b.default_group_id, ["משרד ב: השווי למ\"ר בנוי נקבע 7,000 ₪"], "2" * 64)
    a.doc, a.ver, a.other = str(doc), ver, b
    return a


def cloud(monkeypatch, office, agent):
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    monkeypatch.setattr("app.providers.llm.selected_provider_configured", lambda: True)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)


def new_conversation(client) -> str:
    r = client.post("/api/chat/conversations")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def send(client, cid: str, content: str) -> dict:
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": content})
    assert r.status_code == 200, r.text
    return client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()


ANSWER = final("דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש [S1].",
               claims=[{"text": "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש", "source_ids": ["S1"], "basis": "explicit"}])


def test_answer_from_searched_passage_is_cited_and_verified(client, office, monkeypatch):
    agent = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות הראויים?")
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["kind"] == "rag" and a["status"] == "answered"
    assert "55" in a["markdown"] and "[S1]" in a["markdown"]
    assert [s["id"] for s in a["sources"]] == ["S1"] and a["sources"][0]["document_id"] == office.doc
    assert a["verification"]["judged"] and a["verification"]["removed"] == 0
    labels = [p["step"] for p in m["progress"]]
    assert labels[:2] == ["queued", "understand"] and "search" in labels and "verify" in labels
    # the cost of the turn: token counts per model call, nothing of the content
    assert [u["purpose"] for u in m["usage"]] == ["agent", "agent", "verify"]
    assert set(m["usage"][0]) == {"purpose", "model", "status", "input_tokens", "cached_input_tokens",
                                  "cache_write_tokens", "output_tokens", "latency_ms", "cost_usd"}
    # the model saw the passage as data inside a source tag, and only office A's
    out = agent.tool_outputs(1)[0]
    assert '<source id="S1"' in out and "9,500" in out and "7,000" not in out


def test_number_no_source_states_is_repaired_or_removed(client, office, monkeypatch):
    wrong = final("דמי השכירות הם 70 ₪ למ\"ר לחודש [S1].")
    # the first answer, the repair with tools, and the rewrite from verified content all keep the wrong number
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)], wrong, wrong, wrong])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות?")
    a = m["answer"]
    assert "70" not in a["markdown"]
    assert a["status"] == "partial"
    assert a["verification"]["removed"] == 1 and "problems" not in a["verification"]
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()
    assert any("70" in p["reason"] for p in d["removed"]) and len(d["rounds"]) == 3
    # the repair step told the model what failed
    assert any("70" in str(i) for i in agent.seen[2])


def test_a_failed_repair_is_rewritten_from_verified_content_instead_of_cut(client, office, monkeypatch):
    first = final("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]. הם כוללים מע\"מ [S1].")
    rewritten = final("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]. מעמד המע\"מ שלהם לא צוין במקור [S1].")

    def judge(input: str) -> dict:
        n = input.count("<unit index=")
        verdicts = []
        for i in range(n):
            unit = input.split(f'<unit index="{i}"')[1].split("</unit>")[0]
            bad = "כוללים מע" in unit.split("<source")[0]
            verdicts.append({"index": i, "verdict": "unsupported" if bad else "supported", "reason": "בדיקה"})
        return {"verdicts": verdicts}

    agent = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)], first, first,
                           rewritten], judge=judge)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), "האם דמי השכירות כוללים מע״מ?")["answer"]
    assert a["markdown"] == "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]. מעמד המע\"מ שלהם לא צוין במקור [S1]."
    assert a["status"] == "answered"
    assert "רק מתוכן שאומת" in str(agent.seen[3]) or "רק בתוכן שאומת" in str(agent.seen[3])


def test_citing_an_id_never_issued_fails_verification(client, office, monkeypatch):
    agent = ScriptedAgent([final("השווי נקבע 9,500 ₪ [S7].")] * 3)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert "[S7]" not in m["answer"]["markdown"]
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()
    assert any("S7" in p["reason"] for p in d["removed"])


def test_judge_rejects_a_meaning_the_source_does_not_give(client, office, monkeypatch):
    claim = final("דמי השכירות הם 55 ₪ למ\"ר לחודש ללא מע\"מ [S1].")
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)], claim, claim, claim],
                          judge="unsupported")
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), "האם דמי השכירות כוללים מע״מ?")["answer"]
    assert "ללא מע" not in a["markdown"]
    assert a["status"] == "partial"


def test_follow_up_sees_history_and_reopens_prior_sources_under_current_permissions(client, office, monkeypatch):
    first = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    first_answer = send(client, cid, "מה דמי השכירות הראויים?")
    assert first_answer["status"] == "done", first_answer
    second = ScriptedAgent([[call("open_source", source_id="P1", scope="neighbors")],
                            final("לפי אותו מקור, השווי למ\"ר בנוי ברוטו הוא 9,500 ₪ ללא מע\"מ [S1].")])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "ומה השווי באותה שומה?")
    context = second.seen[0][0]["content"]
    assert "מה דמי השכירות הראויים?" in context and "55" in context  # the history
    assert "P1:" in context  # the earlier answer's source as a reference, not as text to rely on
    reopened = second.tool_outputs(1)[0]
    assert '<source id="S1"' in reopened and "9,500" in reopened
    assert m["answer"]["status"] == "answered"


def test_prior_reference_of_a_deleted_document_is_not_reopened(client, office, monkeypatch):
    first = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    first_answer = send(client, cid, "מה דמי השכירות הראויים?")
    assert client.delete(f"/api/documents/{office.doc}").status_code == 200
    second = ScriptedAgent([[call("open_source", source_id="P1", scope="neighbors")], final("לא נמצא מקור זמין.", "not_found")])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    send(client, cid, "ומה עוד נכתב שם?")
    # the earlier answer rests on a document that is gone: neither its text nor its references reach the model
    context = second.seen[0][0]["content"]
    assert "P1:" not in context and "55" not in context
    assert "מזהה מקור לא מוכר" in second.tool_outputs(1)[0]
    # the earlier answer is hidden now that its source is gone
    msgs = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]
    old = next(x for x in msgs if x["id"] == first_answer["id"])
    assert old["answer"] == {"kind": "rag", "hidden": True} and old["content"] == ""


def test_search_scoped_to_an_invisible_document_is_refused(client, office, monkeypatch):
    foreign = None
    with tenant_tx(office.other.ctx()) as conn:
        foreign = str(conn.execute(text("SELECT id FROM documents")).scalar_one())
    agent = ScriptedAgent([[call("search", query="שווי", document_ids=[foreign], limit=None)],
                           final("לא נמצא.", "not_found")])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    send(client, new_conversation(client), "מה השווי?")
    assert "אין הרשאה" in agent.tool_outputs(1)[0]


def test_provider_failure_is_reported_and_retry_runs_again(client, office, monkeypatch):
    agent = ScriptedAgent([CallStatus.RATE_LIMITED])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["status"] == "failed" and m["answer"] is None and "נסות שוב" in m["error"]
    good = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)],
                          final("השווי נקבע 9,500 ₪ למ\"ר בנוי ברוטו [S1].")])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: good)
    r = client.post(f"/api/chat/messages/{m['id']}/retry")
    assert r.status_code == 200
    again = client.get(f"/api/chat/messages/{r.json()['id']}").json()
    assert again["status"] == "done" and "9,500" in again["answer"]["markdown"]


def test_cancel_during_a_model_call_discards_its_result(client, office, monkeypatch):
    agent = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)], final("השווי 9,500 ₪ [S1].")])
    cloud(monkeypatch, office, agent)

    def press_stop(step: int) -> None:
        if step == 1:  # the user presses stop while the second model call is in flight
            with tenant_tx(office.system) as conn:
                conn.execute(text("UPDATE messages SET cancel_requested = true WHERE role = 'assistant'"))

    agent.on_step = press_stop
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי?")
    assert m["status"] == "cancelled" and m["answer"] is None


def test_limited_mode_shows_search_results_labelled_as_such(client, office):
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "דמי שכירות ראויים")
    a = m["answer"]
    assert m["status"] == "done" and a["kind"] == "search_only"
    assert "לא נוסחה תשובה מנותחת" in a["markdown"] and a["sources"]


def test_conversations_are_per_user_and_per_office(client, office, monkeypatch):
    cloud(monkeypatch, office, ScriptedAgent([ANSWER]))
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    send(client, cid, "שאלה ראשונה")
    msg_id = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"][-1]["id"]
    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    assert client.get(f"/api/chat/conversations/{cid}/messages").status_code == 404
    assert client.get(f"/api/chat/messages/{msg_id}").status_code == 404
    assert client.get("/api/chat/conversations").json()["conversations"] == []
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.get(f"/api/chat/conversations/{cid}/messages").status_code == 404
    assert client.post(f"/api/chat/messages/{msg_id}/cancel").status_code == 404


def test_conversation_list_search_rename_archive_delete(client, office, monkeypatch):
    cloud(monkeypatch, office, ScriptedAgent([ANSWER, ANSWER]))
    login(client, "admin-a@example.test")
    c1, c2 = new_conversation(client), new_conversation(client)
    send(client, c1, "דמי שכירות בגפן")
    send(client, c2, "שאלה על משהו אחר")
    titles = [c["title"] for c in client.get("/api/chat/conversations").json()["conversations"]]
    assert titles[:2] == ["שאלה על משהו אחר", "דמי שכירות בגפן"]
    found = client.get("/api/chat/conversations", params={"q": "גפן"}).json()["conversations"]
    assert [c["id"] for c in found] == [c1]
    assert client.patch(f"/api/chat/conversations/{c1}", json={"title": "  שכירות  "}).json()["title"] == "שכירות"
    assert client.patch(f"/api/chat/conversations/{c1}", json={"archived": True}).json()["archived"] is True
    assert [c["id"] for c in client.get("/api/chat/conversations").json()["conversations"]] == [c2]
    assert [c["id"] for c in client.get("/api/chat/conversations", params={"archived": "true"}).json()["conversations"]] == [c1]
    assert client.delete(f"/api/chat/conversations/{c2}").status_code == 200
    assert client.get(f"/api/chat/conversations/{c2}/messages").status_code == 404


def test_messages_page_backwards_in_order(client, office, monkeypatch):
    cloud(monkeypatch, office, ScriptedAgent([ANSWER] * 4))
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    for i in range(4):
        send(client, cid, f"שאלה {i}")
    page = client.get(f"/api/chat/conversations/{cid}/messages", params={"limit": 3}).json()
    assert page["has_more"] is True
    assert [m["role"] for m in page["messages"]] == ["assistant", "user", "assistant"]
    older = client.get(f"/api/chat/conversations/{cid}/messages",
                       params={"limit": 30, "before": page["messages"][0]["id"]}).json()
    contents = [m["content"] for m in older["messages"] if m["role"] == "user"]
    assert contents == ["שאלה 0", "שאלה 1", "שאלה 2"] and older["has_more"] is False


def test_a_new_message_waits_for_the_running_one(client, office):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text(
            "INSERT INTO messages (office_id, conversation_id, user_id, role, status) VALUES (app_office(), :c, :u,"
            " 'assistant', 'running')"), {"c": cid, "u": office.admin_id})
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "עוד שאלה"})
    assert r.status_code == 409


def test_same_client_id_does_not_start_a_second_turn(client, office, monkeypatch):
    agent = ScriptedAgent([ANSWER])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    body = {"content": "מה השווי?", "client_id": "8e0f5c1e-4e7b-4d39-9a43-1b0e9c3f4a11"}
    r1 = client.post(f"/api/chat/conversations/{cid}/messages", json=body).json()
    steps = len(agent.seen)
    r2 = client.post(f"/api/chat/conversations/{cid}/messages", json=body).json()
    assert r1["user"]["id"] == r2["user"]["id"] and r1["assistant"]["id"] == r2["assistant"]["id"]
    assert len(agent.seen) == steps  # the repeated request started no model step


def test_an_answer_the_judge_cannot_check_fails_with_retry_and_shows_nothing(client, office, monkeypatch):
    # the judge times out on the call and on its retry: the answer was never checked, so it is not shown
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)],
                           final("הנכס פנוי ומושכר לטווח ארוך.")], judge=lambda input: CallStatus.TIMEOUT)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "האם הנכס פנוי?")
    assert m["status"] == "failed" and m["answer"] is None and m["content"] == ""
    assert "לא ניתן היה לאמת" in m["error"] and "לנסות שוב" in m["error"]
    assert sum(c.purpose.value == "verify" for c in agent.calls) == 2


def test_a_passage_returned_again_is_referenced_not_resent_and_still_citable(client, office, monkeypatch):
    agent = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)],
                           [call("search", query="דמ\"ש ראויים למ\"ר", document_ids=None, limit=None)],
                           lambda items: final("דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש "
                                               f"[{re.search(r'<source id=.(S[0-9]+). same_as', str(items)).group(1)}]."),
                           ])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות הראויים?")
    second = agent.tool_outputs(2)[-1]
    assert 'same_as="S1"' in second and "9,500" not in second  # the summary passage is not sent twice
    a = m["answer"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0


def test_earlier_answers_reach_the_model_shortened_and_the_users_words_whole(client, office, monkeypatch):
    long_answer = "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש [S1]. " + "הסבר נוסף על הסביבה. " * 150
    question = "שאלה ארוכה של המשתמש " * 60
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)], final(long_answer),
                           final("כן [S1].") ])
    agent.steps.insert(2, [call("search", query="דמי שכירות", document_ids=None, limit=None)])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    send(client, cid, question)
    send(client, cid, "ומה עוד?")
    context = next(items[0]["content"] for items in agent.seen if "ומה עוד?" in str(items[0].get("content")))
    assert question.strip() in context
    assert "הסבר נוסף על הסביבה. " * 40 not in context and "…" in context


# --- follow-ups resolved in context (app.chat.resolve) ---------------------------------------------------------

def _rent_focus(doc: str) -> dict:
    return {"metric_as_written": "דמ\"ש ראויים למ\"ר", "metric_kind": "rent_per_area", "unit": "ILS_per_sqm",
            "period": "month", "area_basis": "", "vat": "unknown", "subject": "הנכס ברחוב הגפן",
            "value_role": "appraiser_determination", "document_ids": [doc]}


def _value_focus(doc: str, kind: str) -> dict:
    return _rent_focus(doc) | {"metric_as_written": "השווי למ\"ר בנוי ברוטו", "metric_kind": kind,
                               "unit": "ILS_per_sqm" if kind.endswith("_per_area") else "ILS", "period": "none",
                               "vat": "excluded"}


def _resolution(**kw) -> dict:
    return {"relation": "correction", "scope": "entity", "standalone_question": "מה השווי?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _first_turn(client, office, monkeypatch) -> str:
    first = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)],
                           final("דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש [S1].", focus=_rent_focus(office.doc))])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "מה דמי השכירות הראויים?")["status"] == "done"
    return cid


def test_a_rent_to_value_correction_is_resolved_per_area_and_a_total_is_repaired(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    # the resolver errs: a total value, with the rent's period; the server keeps the per-m² class and drops it
    second = ScriptedAgent([
        [call("search", query="שווי", document_ids=[office.doc], limit=None)],
        final("השווי הוא 9,500 ₪ למ\"ר בנוי ברוטו, ללא מע\"מ [S1].", focus=_value_focus(office.doc, "value")),
        final("השווי למ\"ר בנוי ברוטו הוא 9,500 ₪, ללא מע\"מ [S1].",
              focus=_value_focus(office.doc, "value_per_area"))])
    second.on("agent", _resolution(changed_fields=[{"field": "metric_kind", "user_words": "השווי"}],
                                   metric_kind="value", unit="ILS", period="month", document_ids=[office.doc]))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "דווקא את השווי, לא השכירות")
    assert m["status"] == "done", m
    task = second.seen[0][0]["content"]
    assert "סוג המדד: שווי ליחידת שטח" in task and "יחידה: ₪ למ״ר" in task and "תקופה: לא צוין" in task
    assert "דווקא את השווי, לא השכירות" in task  # the user's own words are still shown
    req = m["answer"]["request"]
    assert req["metric_kind"] == "value_per_area" and req["period"] == "unknown" and req["document_ids"] == [office.doc]
    # the first answer named a total: a problem for the repair round, which answered the requested datum
    assert len(second.seen) == 3 and "שהתבקש" not in m["answer"]["markdown"]
    assert m["answer"]["status"] == "answered" and "שימו לב" not in m["answer"]["markdown"]


def test_a_total_that_survives_the_repairs_is_said_plainly(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    total = final("השווי הוא 9,500 ₪ למ\"ר בנוי ברוטו, ללא מע\"מ [S1].", focus=_value_focus(office.doc, "value"))
    second = ScriptedAgent([[call("search", query="שווי", document_ids=[office.doc], limit=None)], total, total, total])
    second.on("agent", _resolution(changed_fields=[{"field": "metric_kind", "user_words": "השווי"}],
                                   metric_kind="value_per_area", unit="ILS_per_sqm", document_ids=[office.doc]))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    a = send(client, cid, "רציתי את השווי ולא את השכירות")["answer"]
    assert a["status"] == "partial" and "שימו לב" in a["markdown"] and "שווי ליחידת שטח" in a["markdown"]


def test_an_ambiguous_correction_gets_one_question_and_no_tools(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([])
    second.on("agent", _resolution(changed_fields=[{"field": "metric_kind", "user_words": "השווי"}],
                                   metric_kind="value_per_area", ambiguity="התכוונת לשווי למ\"ר או לשווי הכולל?"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "רציתי את השווי")
    assert m["status"] == "done" and m["answer"]["status"] == "clarification"
    assert m["answer"]["markdown"] == "התכוונת לשווי למ\"ר או לשווי הכולל?" and second.seen == []
    assert m["answer"]["verification"]["judged"] and m["answer"]["verification"]["judge_status"] == "no_claims"


def test_a_failed_resolution_falls_back_to_the_message_and_focus(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)],
                            final("השווי למ\"ר בנוי ברוטו הוא 9,500 ₪, ללא מע\"מ [S1].")])
    second.on("agent", CallStatus.TIMEOUT)  # the resolver is down
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "רציתי את השווי")
    assert m["status"] == "done" and m["answer"]["request"] is None
    assert any(u["purpose"] == "resolve" and u["status"] == "timeout" for u in m["usage"])
    assert "הנתון שבמרכז השיחה" in second.seen[0][0]["content"]


def test_an_answer_without_a_focus_hands_its_request_to_the_next_turn(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([[call("search", query="מקור", document_ids=[office.doc], limit=None)],
                            final("המקור הוא סעיף הסיכום של השומה [S1].")])
    second.on("agent", _resolution(relation="same_datum", metric_kind="rent_per_area", unit="ILS_per_sqm",
                                   period="month", document_ids=[office.doc]))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    assert send(client, cid, "מאיפה זה לקוח?")["answer"]["focus"] is None
    third = ScriptedAgent([final("לא נמצא.", "not_found")])
    third.on("agent", _resolution(relation="same_datum"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: third)
    send(client, cid, "ומה עוד?")
    resolve_input = next(c.input for c in third.calls if c.purpose.value == "agent")
    assert "rent_per_area" in resolve_input


def test_a_new_topic_leaves_the_previous_datum_out_of_the_task(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([final("לא נמצא.", "not_found")])
    second.on("agent", _resolution(relation="new_question", standalone_question="מה תיאור הנכס?"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    send(client, cid, "שאלה אחרת: מה תיאור הנכס?")
    task = second.seen[0][0]["content"]
    assert "הנתון שבמרכז השיחה" not in task and "ההודעות האחרונות בשיחה" in task


def test_a_follow_up_keeps_the_previous_datum_in_the_task(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([final("לא נמצא.", "not_found")])
    second.on("agent", _resolution(relation="same_datum"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    send(client, cid, "ומה המקור לזה?")
    assert "הנתון שבמרכז השיחה" in second.seen[0][0]["content"]


def test_a_clarification_that_states_a_figure_is_not_used(client, office, monkeypatch):
    cid = _first_turn(client, office, monkeypatch)
    second = ScriptedAgent([[call("search", query="שווי", document_ids=[office.doc], limit=None)],
                            final("השווי למ\"ר בנוי ברוטו הוא 9,500 ₪, ללא מע\"מ [S1].")])
    second.on("agent", _resolution(changed_fields=[{"field": "metric_kind", "user_words": "השווי"}],
                                   metric_kind="value_per_area", ambiguity="התכוונת ל-9,500 ₪ למ\"ר או ל-13,700?"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "רציתי את השווי")
    assert m["answer"]["status"] != "clarification" and len(second.seen) == 2  # the turn went on with tools


def test_a_qualifier_still_missing_after_the_repair_is_written_in_without_a_rewrite(client, office, monkeypatch):
    bare = final("השווי למ\"ר הוא 9,500 ₪, ללא מע\"מ [S1].")  # the source says "למ"ר בנוי ברוטו"
    agent = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)], bare, bare, bare])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), "מה השווי למ\"ר?")["answer"]
    assert len(agent.seen) == 3  # first answer and one repair; no rewrite that would drop the datum
    assert "9,500 ₪ (מ״ר בנוי ברוטו, כפי שנכתב במקור)" in a["markdown"]
    assert a["verification"]["annotated"] == 1 and a["verification"]["removed"] == 0
