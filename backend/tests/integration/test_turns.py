"""The turn orchestrator over HTTP (U9; KTD1, KTD2, KTD12-KTD14; AE1-AE8, R17-R21, R25, R26).

Every model path is scripted (``ScriptedProvider``); no real provider is called."""

from __future__ import annotations

import json
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.answering import turn
from app.answering.attributes import bump_facts_version, resolve_attribute
from app.answering.interpret import LIMITED_MODE_NOTE
from app.answering.plan import TurnPlan
from app.config import get_settings
from app.db import get_engine, tenant_tx
from app.providers.llm import CallStatus, MockLLM, Purpose, StructuredResult
from app.providers.status import FAILURE_REASONS, selected_provider_and_model
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_dedup import add_version, publish
from tests.integration.test_fact_extraction import add_doc, mention, script, subject_at
from tests.integration.test_numeric_answers import AE3, approve_all, row
from tests.integration.test_search import _add_version, add_chunks
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

SAFE_ROOM_Q = "מה גודל ממ״ד ממוצע ברמת גן?"
SAFE_ROOM = "שטח ממ״ד"
BALCONY = "שטח מרפסת"
NUMERIC_Q = "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים"
CONTENT_TEXTS = ["4. שיקולי השמאי: השמאי המכריע קבע הפחתה של 10% בשל היטל השבחה.",
                 "2. תיאור הנכס: הדירה בקרבה לפארק הלאומי ובחזית לרחוב שקט."]
CONTENT_Q = "מה נקבע לגבי היטל השבחה?"
SUPPORTED = "השמאי המכריע קבע הפחתה של 10% בשל היטל השבחה"
SEARCH = {"tool": "search", "attribute_handle": None, "source_handles": []}


# --- helpers ------------------------------------------------------------------------------------------

def post(client, question=None, conversation_id=None, *, turn_id=None, clarification=None, filters=None,
         remove=None, status=200):
    body = {"question": question, "conversation_id": conversation_id, "clarification": clarification,
            "filters": filters, "remove": remove, "turn_id": turn_id}
    r = client.post("/api/ask", json={k: v for k, v in body.items() if v is not None})
    assert r.status_code == status, r.text
    return r.json()


def scalar(office, sql, **params):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), params).scalar()


def cache_rows(office) -> int:
    return scalar(office, "SELECT count(*) FROM answer_cache")


def usage(office):
    with tenant_tx(office.system) as conn:
        return [tuple(r) for r in conn.execute(text("SELECT purpose, ok, status FROM provider_usage ORDER BY id"))]


def conversation(client, cid):
    return client.get(f"/api/conversations/{cid}").json()


def enable_cloud(office, monkeypatch, provider):
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    monkeypatch.setattr("app.answering.content.selected_provider_configured", lambda: True)
    monkeypatch.setattr("app.answering.content.get_selected_provider", lambda: provider)
    monkeypatch.setattr("app.providers.llm.selected_provider_configured", lambda: True)


def plan(**fields) -> dict:
    return TurnPlan.build(**fields).model_dump()


def compute_plan(description, *, city=None, metric="mean", unit_dimension="area") -> dict:
    return plan(task_type="compute", metric=metric, conditions={"city": city} if city else {},
                attribute={"handle": None, "description": description, "unit_dimension": unit_dimension},
                steps=[{"tool": "extract_and_compute", "attribute_handle": None, "source_handles": []}])


def search_plan(instructions, input):
    return plan(task_type="answer", search_queries=[json.loads(input)["question"]], steps=[SEARCH])


def claims(*items):
    return {"claims": [{"text": t, "evidence_ids": ids, "kind": "explicit", "numbers": []} for t, ids in items],
            "insufficient": False, "missing_info": None}


def verdicts(n):
    return {"verdicts": [{"claim": i, "verdict": "supported"} for i in range(n)]}


# --- fixtures -----------------------------------------------------------------------------------------

@pytest.fixture
def records(db):
    """Verified transactions in חרוזים: two in 2024, one each in 2023 and 2022."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    rows = AE3 + [row("הגפן 3", "6158/3", "05/05/2023", "80", "2,000,000"),
                  row("הגפן 4", "6158/4", "06/06/2022", "100", "1,800,000")]
    a.doc, ver = add_version(a, a.default_group_id, rows, "1" * 64)
    publish(a, a.doc, ver)
    approve_all(a)
    return a


@pytest.fixture
def safe_rooms(db):
    """Three appraisals in רמת גן: two state the attribute, one does not."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    texts = {"שומה אחת": "הנכס כולל ממ״ד בשטח 12 מ״ר בקומה השנייה.",
             "שומה שתיים": "בדירה ממ״ד בשטח 14 מ״ר ומרפסת שמש.",
             "שומה שלוש": "דירת שלושה חדרים עם מטבח משופץ."}
    for n, (title, body) in enumerate(texts.items(), start=1):
        doc, ver = add_doc(a, a.default_group_id, title, [body])
        subject_at(a, doc, ver, "6200", str(n))
    return a


def safe_room_provider(*silent: str) -> ScriptedProvider:
    """Interprets the question as the attribute's mean in רמת גן; documents in ``silent`` do not state it."""
    p = ScriptedProvider().on(Purpose.INTERPRET, compute_plan(SAFE_ROOM, city="רמת גן"), repeat=True)
    script(p, "שומה אחת", mention("ממ״ד בשטח 12 מ״ר", "12"))
    script(p, "שומה שתיים", mention("ממ״ד בשטח 14 מ״ר", "14"))
    for title in ("שומה שלוש", *silent):
        script(p, title)
    return p


@pytest.fixture
def content(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.doc, _ = add_chunks(a, a.default_group_id, CONTENT_TEXTS, "c" * 64)
    return a


# --- AE1 / AE2: an unanticipated attribute ----------------------------------------------------------------

def test_safe_room_average_is_computed_from_extracted_facts_in_cloud_mode(client, safe_rooms, monkeypatch):
    """AE1: no price clarification; computed over the documents in scope with n, method, coverage, pages."""
    p = safe_room_provider()
    enable_cloud(safe_rooms, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, SAFE_ROOM_Q)["answer"]
    assert a["kind"] == "numeric" and a["mode"] == "cloud" and a.get("clarification") is None
    assert a["numeric"]["record_count"] == 0  # nothing reviewed yet: only a labeled preliminary figure
    assert a["preliminary"] == {"value": "13.00", "record_count": 2, "values": ["12", "14"]}
    assert "נתון ראשוני" in a["text"] and "13" in a["text"]
    facts_cov = a["coverage"]["facts"]
    assert (facts_cov["in_scope"], facts_cov["found"], facts_cov["not_stated"]) == (3, 2, 1)
    assert "כיסוי: 3 מסמכים בתחום" in a["text"]
    assert {s["page_list"][0] for s in a["sources"]} == {1} and len(a["sources"]) == 2
    assert all(s["tier"] == "preliminary" and s["snippet"] for s in a["sources"])
    assert "חילוץ הנתון" in a["method"] and turn.FACT_SCOPE_NOTE in a["limitations"]
    assert [c.purpose for c in p.calls].count(Purpose.EXTRACT) == 3
    assert [c.purpose for c in p.calls][0] == Purpose.INTERPRET
    steps = scalar(safe_rooms, "SELECT steps FROM questions")
    assert steps[0]["tool"] == "extract_and_compute" and steps[0]["provider_statuses"] == ["ok"] * 3
    assert scalar(safe_rooms, "SELECT facts_versions FROM questions")  # the attribute's facts version is kept


@pytest.mark.parametrize("demo", [True, False])
def test_safe_room_question_without_cloud_lists_passages_and_never_asks_about_prices(client, safe_rooms,
                                                                                     monkeypatch, demo):
    """AE2: limited (or demo) mode: relevant passages and the limitation, no number, no clarification."""
    monkeypatch.setattr(get_settings(), "demo_mode", demo)
    p = safe_room_provider()
    monkeypatch.setattr("app.answering.content.get_selected_provider", lambda: p)
    login(client, "admin-a@example.test")
    a = post(client, SAFE_ROOM_Q)["answer"]
    assert a["kind"] in ("content", "abstain") and a["kind"] != "clarification"
    assert a["numeric"] is None and a["preliminary"] is None
    assert a["mode"] == ("demo" if demo else "limited")
    assert LIMITED_MODE_NOTE in a["limitations"]
    assert a["sources"] and all("ממ״ד" in s["snippet"] for s in a["sources"])
    assert p.calls == []


def test_fully_explained_monetary_question_makes_no_provider_call_even_in_cloud_mode(client, records,
                                                                                    monkeypatch):
    p = ScriptedProvider()  # anything called would be unscripted and logged
    enable_cloud(records, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, NUMERIC_Q)["answer"]
    assert a["kind"] == "numeric" and a["numeric"]["record_count"] == 2 and a["provider"] == "template"
    assert p.calls == [] and usage(records) == []


# --- AE3: idempotent turns ----------------------------------------------------------------------------

def _year_2023(client):
    r = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2023 בחרוזים")
    assert r["answer"]["numeric"]["record_count"] == 1
    return r["conversation_id"]


def _year_of(answer) -> str:
    return " ".join(c["value"] for c in answer["conditions"])


def test_same_turn_posted_twice_applies_once(client, records):
    login(client, "admin-a@example.test")
    cid = _year_2023(client)
    tid = str(uuid.uuid4())
    first = post(client, "ומה לגבי השנה הקודמת?", cid, turn_id=tid)
    again = post(client, "ומה לגבי השנה הקודמת?", cid, turn_id=tid)
    assert again == first and "2022" in _year_of(first["answer"]) and "2021" not in _year_of(again["answer"])
    assert first["answer"]["numeric"]["record_count"] == 1
    assert scalar(records, "SELECT count(*) FROM questions WHERE turn_id = :t", t=tid) == 1
    assert conversation(client, cid)["context"]["years"] == {"from": 2022, "to": None}


def test_same_turn_posted_concurrently_answers_409_then_the_stored_result(client, records, monkeypatch):
    login(client, "admin-a@example.test")
    cid = _year_2023(client)
    original, entered, release = turn.execute_turn, threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(turn, "execute_turn", slow)
    tid, out = str(uuid.uuid4()), {}
    worker = threading.Thread(target=lambda: out.update(first=post(client, "ומה לגבי השנה הקודמת?", cid,
                                                                     turn_id=tid)))
    worker.start()
    assert entered.wait(20)
    with TestClient(client.app) as other:
        login(other, "admin-a@example.test")
        busy = post(other, "ומה לגבי השנה הקודמת?", cid, turn_id=tid, status=409)
        assert busy["detail"] == "השאלה עדיין בעיבוד"
        release.set()
        worker.join(20)
        stored = post(other, "ומה לגבי השנה הקודמת?", cid, turn_id=tid)
    assert stored == out["first"] and "2022" in _year_of(stored["answer"])
    assert scalar(records, "SELECT count(*) FROM questions WHERE turn_id = :t", t=tid) == 1


def test_state_version_conflict_reapplies_the_plan_once_to_fresh_state(client, records, monkeypatch):
    login(client, "admin-a@example.test")
    cid = _year_2023(client)
    original, seen = turn.execute_turn, []

    def concurrent_turn(ctx, it, L, qid, started):
        seen.append(L.state_version)
        if len(seen) == 1:  # another turn on this conversation moves the year to 2024 while this one runs
            with tenant_tx(records.system) as conn:
                conn.execute(text("UPDATE conversations SET state_version = state_version + 1, state ="
                                  " jsonb_set(state, '{conditions,year_from}', '2024')"))
        return original(ctx, it, L, qid, started)

    monkeypatch.setattr(turn, "execute_turn", concurrent_turn)
    a = post(client, "ומה לגבי השנה הקודמת?", cid)["answer"]
    assert seen[1] == seen[0] + 1  # retried once, on the fresh state version
    # "the previous year" was resolved against the state the turn started from (2023), once
    assert a["numeric"]["record_count"] == 1 and "2022 (" in _year_of(a)
    assert conversation(client, cid)["context"]["years"] == {"from": 2022, "to": None}
    assert scalar(records, "SELECT state_version FROM conversations") == seen[0] + 2


def test_repeated_state_conflict_answers_409_and_the_turn_can_run_again(client, records, monkeypatch):
    login(client, "admin-a@example.test")
    cid = _year_2023(client)
    original = turn.execute_turn

    def always_conflicting(ctx, it, L, qid, started):
        with tenant_tx(records.system) as conn:
            conn.execute(text("UPDATE conversations SET state_version = state_version + 1"))
        return original(ctx, it, L, qid, started)

    monkeypatch.setattr(turn, "execute_turn", always_conflicting)
    tid = str(uuid.uuid4())
    r = post(client, "ומה לגבי השנה הקודמת?", cid, turn_id=tid, status=409)
    assert r["detail"] == turn.STATE_CONFLICT
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "failed"
    monkeypatch.setattr(turn, "execute_turn", original)
    again = post(client, "ומה לגבי השנה הקודמת?", cid, turn_id=tid)
    assert "2022" in _year_of(again["answer"])
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "done"


# --- AE5: clarifications in free text ---------------------------------------------------------------------

def test_free_text_reply_resolves_the_clarification(client, records):
    login(client, "admin-a@example.test")
    r = post(client, "מה מחיר למ״ר בחרוזים?")
    assert r["answer"]["clarification"]["key"] == "data_kind"
    a = post(client, "התכוונתי לעסקאות", r["conversation_id"])["answer"]
    assert a["kind"] == "numeric" and a["numeric"]["record_count"] == 4
    assert a["interpretation_note"] and "מחירי עסקאות" in a["interpretation_note"]
    assert conversation(client, r["conversation_id"])["pending_clarification"] is None


def test_unrelated_question_keeps_the_pending_clarification(client, records):
    add_chunks(records, records.default_group_id, CONTENT_TEXTS, "c" * 64)
    login(client, "admin-a@example.test")
    r = post(client, "מה מחיר למ״ר בחרוזים?")
    cid = r["conversation_id"]
    other = post(client, CONTENT_Q, cid)["answer"]
    assert other["kind"] == "content" and "עדיין פתוחה" in other["interpretation_note"]
    assert conversation(client, cid)["pending_clarification"]["key"] == "data_kind"
    a = post(client, conversation_id=cid, clarification={"key": "data_kind", "value": "transaction_price"})
    assert a["answer"]["kind"] == "numeric" and a["answer"]["numeric"]["record_count"] == 4


# --- meta-turns -------------------------------------------------------------------------------------------

def test_why_explains_method_conditions_and_sources_without_a_provider_call(client, records, monkeypatch):
    calls = []
    monkeypatch.setattr(MockLLM, "answer", lambda *a, **k: calls.append(1))
    login(client, "admin-a@example.test")
    r = post(client, NUMERIC_Q)
    before = cache_rows(records)
    a = post(client, "למה?", r["conversation_id"])["answer"]
    assert a["kind"] == "content" and a["provider"] == "template" and a["meta"] == "explain_previous"
    assert "חישוב SQL על רשומות מאומתות" in a["text"] and "חרוזים" in a["text"] and "2024" in a["text"]
    assert {s["document_id"] for s in a["sources"]} == {str(records.doc)}
    assert calls == [] and usage(records) == [] and cache_rows(records) == before
    again = post(client, "למה?", r["conversation_id"])["answer"]
    assert again.get("cached") is None


def test_show_sources_after_deletion_says_the_source_is_gone(client, records):
    login(client, "admin-a@example.test")
    r = post(client, NUMERIC_Q)
    shown = post(client, "תראה לי את המקור", r["conversation_id"])["answer"]
    assert shown["kind"] == "content" and shown["sources"]
    client.delete(f"/api/documents/{records.doc}")
    gone = post(client, "תראה לי את המקור", r["conversation_id"])["answer"]
    assert gone["kind"] == "abstain" and gone["sources"] == [] and "כבר אינו זמין" in gone["text"]


# --- AE8 and KTD14: cache and stale marking --------------------------------------------------------------

def test_cache_invalidated_on_delete(client, records):
    login(client, "admin-a@example.test")
    first = post(client, NUMERIC_Q)
    assert post(client, NUMERIC_Q)["answer"]["cached"] is True
    client.delete(f"/api/documents/{records.doc}")
    after = post(client, NUMERIC_Q)["answer"]
    assert after.get("cached") is None and after["sources"] == []
    old = conversation(client, first["conversation_id"])["messages"][0]
    assert old["hidden"] is True and old["answer"] is None


def test_cache_invalidated_on_group_removal(client, db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = make_group(a, "G1")
    emp = make_user(a, "emp@example.test", [g])
    doc, ver = add_version(a, g, AE3, "1" * 64)
    publish(a, doc, ver)
    approve_all(a)
    login(client, "emp@example.test")
    first = post(client, NUMERIC_Q)
    assert post(client, NUMERIC_Q)["answer"]["cached"] is True
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u"), {"u": emp})
    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    after = post(client, NUMERIC_Q)["answer"]
    assert after.get("cached") is None and after["sources"] == [] and after["numeric"] is None
    old = conversation(client, first["conversation_id"])["messages"][0]
    assert old["stale"] is True or old["hidden"] is True


def test_fact_review_invalidates_only_answers_of_that_attribute(client, safe_rooms, monkeypatch):
    """AE8 via facts_version: reviewing a fact recomputes the attribute's answer and marks its history stale;
    another attribute's job and an unrelated conversation are untouched (KTD9)."""
    enable_cloud(safe_rooms, monkeypatch, safe_room_provider("דוח 1111"))
    records_doc, ver = add_version(safe_rooms, safe_rooms.default_group_id, AE3, "1" * 64)
    publish(safe_rooms, records_doc, ver)
    approve_all(safe_rooms)
    login(client, "admin-a@example.test")
    priced = post(client, NUMERIC_Q)
    first = post(client, SAFE_ROOM_Q)
    assert first["answer"]["kind"] == "numeric"
    assert post(client, SAFE_ROOM_Q)["answer"]["cached"] is True
    with tenant_tx(safe_rooms.ctx()) as conn:
        balcony = resolve_attribute(conn, handle=None, description=BALCONY, unit_dimension="area")
        bump_facts_version(conn, balcony.id)  # a balcony extraction job finished
    assert post(client, SAFE_ROOM_Q)["answer"]["cached"] is True
    assert conversation(client, first["conversation_id"])["messages"][0]["stale"] is False
    with tenant_tx(safe_rooms.ctx()) as conn:  # a reviewer approves one value
        attr = resolve_attribute(conn, handle=None, description=SAFE_ROOM, unit_dimension="area")
        conn.execute(text("UPDATE facts SET status = 'verified' WHERE attribute_id = :a AND canonical_value = 12"),
                     {"a": attr.id})
        bump_facts_version(conn, attr.id)
    after = post(client, SAFE_ROOM_Q)["answer"]
    assert after.get("cached") is None and after["numeric"]["record_count"] == 1
    assert after["numeric"]["value"] == "12.00"
    assert conversation(client, first["conversation_id"])["messages"][0]["stale"] is True
    assert conversation(client, priced["conversation_id"])["messages"][0]["stale"] is False


def test_partial_clarification_and_meta_answers_are_never_cached(client, safe_rooms, monkeypatch):
    enable_cloud(safe_rooms, monkeypatch, safe_room_provider())
    monkeypatch.setattr(get_settings(), "extract_sync_max_versions", 0)  # everything goes to async jobs
    login(client, "admin-a@example.test")
    a = post(client, SAFE_ROOM_Q)["answer"]
    assert a["partial"] is True and a["pending_extraction"] == 3 and a["kind"] == "abstain"
    assert a["abstention_kind"] == "not_extracted_or_verified"
    assert cache_rows(safe_rooms) == 0
    monkeypatch.setattr(get_settings(), "extract_sync_max_versions", 6)
    monkeypatch.setattr(get_settings(), "turn_deadline_seconds", 0)  # the deadline has already passed
    late = post(client, SAFE_ROOM_Q)["answer"]
    assert late["partial"] is True and turn.PARTIAL_NOTE in late["limitations"]
    assert cache_rows(safe_rooms) == 0
    meta = post(client, "תראה לי את המקור")["answer"]
    assert meta["meta"] == "show_sources" and cache_rows(safe_rooms) == 0


def test_clarifications_and_fallbacks_are_never_cached(client, records, monkeypatch):
    add_chunks(records, records.default_group_id, CONTENT_TEXTS, "c" * 64)
    login(client, "admin-a@example.test")
    assert post(client, "מה מחיר למ״ר בחרוזים?")["answer"]["kind"] == "clarification"
    assert cache_rows(records) == 0
    p = ScriptedProvider().on(Purpose.INTERPRET, search_plan, repeat=True)
    p.on(Purpose.ANSWER, claims((SUPPORTED, ["E1"])), repeat=True).on(Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    enable_cloud(records, monkeypatch, p)
    a = post(client, CONTENT_Q)["answer"]
    assert a["provider"] == "extractive" and cache_rows(records) == 0


def test_two_metrics_with_the_same_conditions_have_different_cache_keys(client, records):
    login(client, "admin-a@example.test")
    mean = post(client, "מה מחיר העסקאות הממוצע למ״ר בחרוזים לפי תאריך עסקה ב-2024?")["answer"]
    median = post(client, "מה חציון מחיר העסקאות למ״ר בחרוזים לפי תאריך עסקה ב-2024?")["answer"]
    assert mean["kind"] == median["kind"] == "numeric" and median.get("cached") is None
    assert cache_rows(records) == 2


# --- conversation state and isolation ---------------------------------------------------------------------

def test_reopening_restores_state_pending_clarification_and_conditions(client, records):
    login(client, "admin-a@example.test")
    r = post(client, "מה מחיר למ״ר בחרוזים ב-2024?")
    cid = r["conversation_id"]
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    conv = conversation(client, cid)
    assert conv["pending_clarification"]["key"] == "data_kind"
    assert conv["context"]["neighborhood"] == "חרוזים" and conv["context"]["years"] == {"from": 2024, "to": None}
    post(client, conversation_id=cid, clarification={"key": "data_kind", "value": "transaction_price"})
    post(client, conversation_id=cid, clarification={"key": "date_field", "value": "transaction_date"})
    conv = conversation(client, cid)
    assert conv["pending_clarification"] is None
    ctx = conv["context"]
    assert (ctx["data_kind"], ctx["date_field"], ctx["attribute"]) == ("transaction_price", "transaction_date",
                                                                       "מחיר למ״ר")
    assert {c["key"] for c in ctx["chips"]} >= {"attribute", "data_kind", "neighborhood", "years", "date_field"}
    assert {"label": "שכונה", "value": "חרוזים"} in conv["confirmed_conditions"]
    assert [m["answer"]["kind"] for m in conv["messages"]] == ["clarification", "clarification", "numeric"]


def test_chip_edit_and_removal_rerun_the_last_task(client, records):
    login(client, "admin-a@example.test")
    cid = post(client, NUMERIC_Q)["conversation_id"]
    edited = post(client, conversation_id=cid, filters={"year_from": 2023})["answer"]
    assert edited["numeric"]["record_count"] == 1 and "2023" in _year_of(edited)
    removed = post(client, conversation_id=cid, remove=["years"])["answer"]
    assert removed["numeric"]["record_count"] == 4
    assert [c["key"] for c in removed["cleared"]] == ["years"]
    assert conversation(client, cid)["context"]["years"] is None


def test_other_user_cannot_open_or_continue_a_conversation(client, records):
    make_user(records, "y@example.test", [records.default_group_id])
    login(client, "admin-a@example.test")
    cid = post(client, NUMERIC_Q)["conversation_id"]
    client.post("/api/auth/logout")
    login(client, "y@example.test")
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    post(client, "ומה לגבי 2023?", cid, status=404)


# --- provider failures and modes ---------------------------------------------------------------------------

def test_provider_auth_failure_mid_turn_is_visible_and_not_cached(client, content, monkeypatch):
    p = ScriptedProvider().on(Purpose.INTERPRET, search_plan, repeat=True).on(Purpose.ANSWER, CallStatus.AUTH,
                                                                              repeat=True)
    enable_cloud(content, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, CONTENT_Q)["answer"]
    assert a["provider"] == "extractive" and a["demo"] is False and a["mode"] == "cloud"
    assert any(FAILURE_REASONS[CallStatus.AUTH] in lim for lim in a["limitations"])
    assert ("answer", False, "auth") in usage(content) and cache_rows(content) == 0


def test_error_mode_shows_the_failure_and_calls_no_model(client, content, monkeypatch):
    p = ScriptedProvider()
    enable_cloud(content, monkeypatch, p)
    provider, model = selected_provider_and_model()
    with tenant_tx(content.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET provider_test_provider = :p, provider_test_model = :m,"
                          " provider_test_ok = false, provider_test_status = 'auth', provider_tested_at = now()"),
                     {"p": provider, "m": model})
    login(client, "admin-a@example.test")
    a = post(client, CONTENT_Q)["answer"]
    assert a["mode"] == "error" and a["provider"] == "extractive" and p.calls == []
    assert any(FAILURE_REASONS[CallStatus.AUTH] in lim for lim in a["limitations"])
    assert cache_rows(content) == 0


# --- AE4 / AE6: topic change and comparison by conversation referents -------------------------------------

def test_topic_change_clears_place_and_attribute_and_reports_them(client, db, monkeypatch):
    """AE4: after a balcony question filtered to גבעתיים, a new topic clears both."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = add_doc(a, a.default_group_id, "שומה בגבעתיים", ["בדירה מרפסת בשטח 9 מ״ר והיתר בנייה לתוספת."])
    with tenant_tx(a.system) as conn:
        txn = conn.execute(text("INSERT INTO transactions (office_id, data_kind) VALUES (app_office(),"
                                " 'appraised_value') RETURNING id")).scalar_one()
        conn.execute(text("INSERT INTO occurrences (office_id, transaction_id, document_id, version_id, record_index,"
                          " extraction_version, data_kind, city) VALUES (app_office(), :t, :d, :v, 0, 'rules-v1',"
                          " 'appraised_value', 'גבעתיים')"), {"t": txn, "d": doc, "v": ver})
    p = ScriptedProvider()
    p.on(Purpose.INTERPRET, plan(task_type="answer", topic="מרפסות", conditions={"city": "גבעתיים"},
                                 attribute={"handle": None, "description": BALCONY, "unit_dimension": "area"},
                                 search_queries=["שטח מרפסת"], steps=[SEARCH]))
    p.on(Purpose.INTERPRET, plan(task_type="locate", turn_relation="topic_change", topic="היתרי בנייה",
                                 search_queries=["היתר בנייה"],
                                 steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    p.on(Purpose.ANSWER, claims(("בדירה מרפסת בשטח 9 מ״ר", ["E1"]))).on(Purpose.VERIFY, verdicts(1))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    first = post(client, "מה שטח המרפסות בגבעתיים?")
    cid = first["conversation_id"]
    assert conversation(client, cid)["context"]["city"] == "גבעתיים"
    assert conversation(client, cid)["context"]["attribute"] == BALCONY
    second = post(client, "עכשיו בנושא אחר: אילו שומות מזכירות היתר בנייה?", cid)["answer"]
    assert {c["key"] for c in second["cleared"]} >= {"city", "attribute"}
    assert second["kind"] == "content" and "שומה בגבעתיים" in second["text"]
    ctx = conversation(client, cid)["context"]
    assert ctx["city"] is None and ctx["attribute"] is None


def test_compare_uses_the_conversation_referent_and_reads_both_versions(client, db, monkeypatch):
    """AE6: "between the versions" of the appraisal the previous answer cited."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, old_v = add_chunks(a, a.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(a, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider()
    p.on(Purpose.INTERPRET, plan(task_type="answer", search_queries=["שיעור ההתאמה לגודל"], steps=[SEARCH]))
    p.on(Purpose.ANSWER, claims(("שיעור ההתאמה לגודל הוא 7%", ["E1"]))).on(Purpose.VERIFY, verdicts(1))
    p.on(Purpose.INTERPRET, plan(task_type="compare", turn_relation="follow_up",
                                 search_queries=["שיעור ההתאמה לגודל"],
                                 steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1"]}]))
    p.on(Purpose.ANSWER, {**claims(("שיעור ההתאמה לגודל הוא 5%", ["E1"]), ("שיעור ההתאמה לגודל הוא 7%", ["E2"])),
                          "conflicts": [{"datum": "שיעור ההתאמה לגודל", "claims": [0, 1]}]})
    p.on(Purpose.VERIFY, verdicts(2))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    first = post(client, "מה שיעור ההתאמה לגודל?")
    assert {s["version_id"] for s in first["answer"]["sources"]} == {str(new_v)}
    a2 = post(client, "אילו הנחות השתנו בין הגרסאות?", first["conversation_id"])["answer"]
    assert a2["compare"]["incomplete"] is False
    assert {s["version_id"] for s in a2["sources"]} == {str(old_v), str(new_v)}
    labels = {s["label"] for s in a2["compare"]["sides"]}
    assert any("גרסה 1" in x for x in labels) and any("גרסה 2" in x for x in labels)
    assert a2.get("cached") is None


# --- R26 / KTD13: no connection held across model calls; interpreter usage -----------------------------

def probed(response, seen: list[int]):
    """A scripted response that records how many pooled connections are checked out when it is called."""
    def call(instructions, input):
        seen.append(get_engine().pool.checkedout())
        return response(instructions, input) if callable(response) else response
    return call


def test_compare_turn_holds_no_connection_while_the_provider_is_called(client, db, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, old_v = add_chunks(a, a.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    _add_version(a, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    seen: list[int] = []
    p = ScriptedProvider()
    p.on(Purpose.INTERPRET, probed(plan(task_type="answer", search_queries=["שיעור ההתאמה לגודל"],
                                        steps=[SEARCH]), seen))
    p.on(Purpose.ANSWER, probed(claims(("שיעור ההתאמה לגודל הוא 7%", ["E1"])), seen))
    p.on(Purpose.VERIFY, probed(verdicts(1), seen))
    p.on(Purpose.INTERPRET, probed(plan(task_type="compare", turn_relation="follow_up",
                                        search_queries=["שיעור ההתאמה לגודל"],
                                        steps=[{"tool": "compare", "attribute_handle": None,
                                                "source_handles": ["S1"]}]), seen))
    p.on(Purpose.ANSWER, probed({**claims(("שיעור ההתאמה לגודל הוא 5%", ["E1"]),
                                          ("שיעור ההתאמה לגודל הוא 7%", ["E2"])), "conflicts": []}, seen))
    p.on(Purpose.VERIFY, probed(verdicts(2), seen))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    first = post(client, "מה שיעור ההתאמה לגודל?")
    a2 = post(client, "אילו הנחות השתנו בין הגרסאות?", first["conversation_id"])["answer"]
    assert a2["compare"]["incomplete"] is False and a2["provider"] == "cloud"
    assert len(seen) == 6 and seen == [0] * 6  # interpret, answer and judge of both turns: no open connection
    assert [u[0] for u in usage(a)].count("answer") == 2 and [u[0] for u in usage(a)].count("verify") == 2


def test_interpreter_usage_row_has_tokens_and_latency(client, content, monkeypatch):
    def interpreted(instructions, input):
        parsed = TurnPlan.model_validate(search_plan(instructions, input))
        return StructuredResult(CallStatus.OK, parsed, input_tokens=321, output_tokens=45, latency_ms=67)

    p = ScriptedProvider().on(Purpose.INTERPRET, interpreted).on(Purpose.ANSWER, claims((SUPPORTED, ["E1"])))
    p.on(Purpose.VERIFY, verdicts(1))
    enable_cloud(content, monkeypatch, p)
    login(client, "admin-a@example.test")
    post(client, CONTENT_Q)
    with tenant_tx(content.system) as conn:
        row = conn.execute(text("SELECT input_tokens, output_tokens, latency_ms, ok, status FROM provider_usage"
                                " WHERE purpose = 'interpret'")).one()
    assert tuple(row) == (321, 45, 67, True, "ok")
