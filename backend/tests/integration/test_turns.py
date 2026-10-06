"""The turn orchestrator over HTTP (U9; KTD1, KTD2, KTD12-KTD14; AE1-AE8, R17-R21, R25, R26).

Every model path is scripted (``ScriptedProvider``); no real provider is called."""

from __future__ import annotations

import json
import re
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.answering import turn
from app.answering.attributes import bump_facts_version, resolve_attribute
from app.answering.compose import TWO_SIDED_POLICY
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


def test_removing_a_chip_before_any_answer_changes_the_open_clarification(client, records):
    """Code review #29: with only a clarification asked, removing a chip answered 422 "write a question". It
    now changes the clarification's context and asks it again."""
    login(client, "admin-a@example.test")
    cid = post(client, "מה מחיר למ״ר בחרוזים ב-2024?")["conversation_id"]
    assert conversation(client, cid)["pending_clarification"]["key"] == "data_kind"
    again = post(client, conversation_id=cid, remove=["years"])["answer"]
    assert again["kind"] == "clarification" and again["clarification"]["key"] == "data_kind"
    conv = conversation(client, cid)
    assert conv["pending_clarification"]["key"] == "data_kind" and conv["context"]["years"] is None
    done = post(client, conversation_id=cid, clarification={"key": "data_kind", "value": "transaction_price"})
    assert conversation(client, done["conversation_id"])["context"]["years"] is None


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


def test_follow_up_without_any_context_asks_what_it_refers_to(client, records, monkeypatch):
    """R18: "ומה לגבי 2023?" opening a conversation has nothing to follow. Whatever the interpreter planned,
    the server asks a short referent clarification instead of guessing a topic or a price."""
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(task_type="answer", turn_relation="follow_up",
                                                      conditions={"year_from": 2023, "year_to": 2023},
                                                      steps=[SEARCH]), repeat=True)
    enable_cloud(records, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "ומה לגבי 2023?")["answer"]
    assert a["kind"] == "clarification"
    assert a["clarification"]["key"] == "referent" and "שאלת ההמשך" in a["clarification"]["question"]
    assert [c.purpose for c in p.calls] == [Purpose.INTERPRET]  # nothing was searched or computed


# --- round 2: entity scope, compare by name, routing by plan structure, abstention kinds -------------------

ADDRESSES = {"שומה 1": ("רחוב הדקל 4", "12"), "שומה 2": ("רחוב הגפן 9", "10"), "שומה 3": ("רחוב התאנה 2", "14")}


def _first_evidence(text_):
    return lambda instructions, input: claims((text_, re.findall(r'<evidence id="(E\d+)"', input)[:1]))


def _evidence_ids(input) -> list[str]:
    return re.findall(r'<evidence id="(E\d+)"', input)


@pytest.fixture
def addresses(db):
    """Three appraisals in רמת גן, each naming its subject address in its header and stating the attribute."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.docs = {}
    for title, (address, value) in ADDRESSES.items():
        a.docs[title], _ = add_doc(a, a.default_group_id, title,
                                   [f"שומת דירה ב{address}, רמת גן.", f"בדירה ממ״ד בשטח {value} מ״ר."])
    return a


def _addresses_provider(interpret) -> ScriptedProvider:
    p = ScriptedProvider().on(Purpose.INTERPRET, interpret, repeat=True)
    for title, (_address, value) in ADDRESSES.items():
        script(p, title, mention(f"ממ״ד בשטח {value} מ״ר", value, source="C2"))
    return p


def test_value_question_about_one_address_reads_and_computes_only_its_document(client, addresses, monkeypatch):
    """GQ55: a single property's value is never a mean over several documents; the address scopes extraction
    and search to its document (no city-wide computation)."""
    p = _addresses_provider(plan(
        task_type="answer", entities=["רחוב הדקל 4"],
        attribute={"handle": None, "description": SAFE_ROOM, "unit_dimension": "area", "value_type": "numeric"},
        search_queries=["שטח הממ״ד ברחוב הדקל 4"],
        steps=[SEARCH, {"tool": "extract_and_compute", "attribute_handle": None, "source_handles": []}]))
    p.on(Purpose.ANSWER, _first_evidence("בדירה ממ״ד בשטח 12 מ״ר")).on(Purpose.VERIFY, verdicts(1))
    enable_cloud(addresses, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "מה שטח הממ״ד בדירה ברחוב הדקל 4?")["answer"]
    assert [c.purpose for c in p.calls].count(Purpose.EXTRACT) == 1 and '"שומה 1"' in next(
        c.input for c in p.calls if c.purpose == Purpose.EXTRACT)
    assert a["preliminary"]["values"] == ["12"] and a["preliminary"]["record_count"] == 1
    assert a["numeric"]["operation"] == "values"  # one property: its value, not a mean
    assert {s["document_id"] for s in a["sources"]} == {str(addresses.docs["שומה 1"])}
    steps = scalar(addresses, "SELECT steps FROM questions")
    assert steps[0]["args"]["documents"] == [str(addresses.docs["שומה 1"])]
    assert steps[1]["args"]["documents"] == [str(addresses.docs["שומה 1"])] and steps[1]["args"]["filters"] is None


def test_compare_by_address_names_its_sides_without_source_handles(client, addresses, monkeypatch):
    """GQ19/GQ21: a new conversation that names two addresses compares their documents, no clarification."""
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="compare", entities=["רחוב הדקל 4", "רחוב הגפן 9"], search_queries=["שטח ממ״ד"],
        steps=[{"tool": "compare", "attribute_handle": None, "source_handles": []}]))
    p.on(Purpose.ANSWER, lambda i, inp: {**claims(("ממ״ד בשטח 12 מ״ר", _evidence_ids(inp)[:1]),
                                                  ("ממ״ד בשטח 10 מ״ר", _evidence_ids(inp)[-1:])), "conflicts": []})
    p.on(Purpose.VERIFY, verdicts(2))
    enable_cloud(addresses, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "השווה את שטח הממ״ד בין הדירה ברחוב הדקל 4 לדירה ברחוב הגפן 9")["answer"]
    assert a["kind"] == "content" and a.get("clarification") is None
    assert [s["label"] for s in a["compare"]["sides"]] == ["שומה 1", "שומה 2"]
    assert a["compare"]["incomplete"] is False
    assert {s["document_id"] for s in a["sources"]} == {str(addresses.docs["שומה 1"]), str(addresses.docs["שומה 2"])}


def test_compare_of_one_named_document_reads_its_two_latest_versions(client, db, monkeypatch):
    """GQ24/GQ25: "between the versions of the appraisal at X" compares the old and the new version."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, old_v = add_chunks(a, a.default_group_id, ["שומת דירה ברחוב הדקל 4.", "שיעור ההתאמה לגודל הוא 5%."],
                            "e" * 64)
    new_v = _add_version(a, doc, ["שומת דירה ברחוב הדקל 4.", "שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="compare", entities=["הדקל 4"], search_queries=["שיעור ההתאמה לגודל"],
        steps=[{"tool": "compare", "attribute_handle": None, "source_handles": []}]))
    p.on(Purpose.ANSWER, lambda i, inp: {**claims(("שיעור ההתאמה לגודל הוא 5%", _evidence_ids(inp)[:1]),
                                                  ("שיעור ההתאמה לגודל הוא 7%", _evidence_ids(inp)[-1:])),
                                         "conflicts": [{"datum": "שיעור ההתאמה", "claims": [0, 1]}]})
    p.on(Purpose.VERIFY, verdicts(2))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "האם שיעור ההתאמה השתנה בין גרסאות השומה של הדקל 4?")["answer"]
    assert ans.get("clarification") is None and ans["compare"]["incomplete"] is False
    labels = [s["label"] for s in ans["compare"]["sides"]]
    assert "גרסה 1" in labels[0] and "גרסה 2" in labels[1]
    assert {s["version_id"] for s in ans["sources"]} == {str(old_v), str(new_v)}


def test_compare_with_nothing_to_compare_asks_which_documents(client, addresses, monkeypatch):
    """GQ54: the model invented source handles in a fresh conversation; the plan is repaired, not rejected,
    and the turn asks which documents to compare (never a limited-mode search)."""
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="compare", search_queries=["השוואה בין שתי השומות"],
        steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1", "S2"]}]))
    enable_cloud(addresses, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "השווה בין שתי השומות")["answer"]
    assert a["kind"] == "clarification" and a["clarification"]["key"] == "referent"
    stored = scalar(addresses, "SELECT plan FROM questions")
    assert stored["mode"] == "model" and stored["status"] == "ok" and stored["turn_plan"]["task_type"] == "clarify"
    assert stored["model_plan"]["task_type"] == "compare"
    assert [c.purpose for c in p.calls] == [Purpose.INTERPRET]


def test_one_address_named_by_two_documents_shows_each_documents_statement(client, db, monkeypatch):
    """GQ26/GQ03: the same property appraised twice: both documents are searched, the answer is asked to
    state each document's value, and the limitation names both."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    first, _ = add_doc(a, a.default_group_id, "שומה 2022", ["שומת דירה ברחוב הדקל 4, רמת גן.", "הבניין נבנה ב-1958."])
    second, _ = add_doc(a, a.default_group_id, "שומה 2023", ["שומת דירה ברחוב הדקל 4, רמת גן.", "הבניין נבנה ב-1962."])
    add_doc(a, a.default_group_id, "שומה אחרת", ["שומת דירה ברחוב הגפן 9, רמת גן.", "הבניין נבנה ב-1990."])
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="answer", entities=["רחוב הדקל 4"], search_queries=["שנת הבנייה של הבניין"], steps=[SEARCH]))
    p.on(Purpose.ANSWER, lambda i, inp: claims(("הבניין נבנה ב-1958", _evidence_ids(inp)[:1])))
    p.on(Purpose.VERIFY, verdicts(1))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "באיזו שנה נבנה הבניין ברחוב הדקל 4?")["answer"]
    answer_call = next(c for c in p.calls if c.purpose == Purpose.ANSWER)
    assert TWO_SIDED_POLICY in answer_call.instructions
    assert {s["document_id"] for s in ans["sources"]} <= {str(first), str(second)}
    assert any(lim.startswith("השאלה מתאימה ל-2 מסמכים") for lim in ans["limitations"])


def test_condition_on_the_value_counts_the_matching_cases_of_all_observed(client, addresses, monkeypatch):
    """GQ35/GQ60: "which ... smaller than 13" planned as a document list is a filtered computation: how many of
    the observed values meet the condition, and which documents."""
    p = _addresses_provider(plan(
        task_type="locate", search_queries=["ממ״ד קטן מ-13 מ״ר"],
        attribute={"handle": None, "description": SAFE_ROOM, "unit_dimension": "area", "value_type": "numeric"},
        value_filter={"op": "<", "value": "13"},
        steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    enable_cloud(addresses, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "באילו דירות יש ממ״ד קטן מ-13 מ״ר?")["answer"]
    assert a["kind"] == "numeric" and a["numeric"]["operation"] == "count"
    assert a["preliminary"]["value"] == "2" and a["preliminary"]["record_count"] == 3
    assert a["numeric"]["value_filter"] == {"op": "<", "value": "13"}
    assert {s["title"] for s in a["sources"]} == {"שומה 1", "שומה 2"}
    assert "• שומה 1" in a["text"] and "• שומה 2" in a["text"] and "שומה 3" not in a["text"]
    assert f"{turn.FILTER_LABEL}: קטן מ-13 מ״ר" in a["method"]
    assert {"label": turn.FILTER_LABEL, "value": "קטן מ-13 מ״ר"} in a["conditions"]
    stored = scalar(addresses, "SELECT plan FROM questions")
    assert stored["turn_plan"]["task_type"] == "compute" and stored["turn_plan"]["metric"] == "count"
    assert stored["model_plan"]["task_type"] == "locate"


def test_text_attribute_lists_its_distinct_values(client, db, monkeypatch):
    """GQ56: a designation is a text attribute: its values are listed with how many documents state each."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="compute", metric="values",
        attribute={"handle": None, "description": "סיווג המגרש", "unit_dimension": None, "value_type": "text"},
        steps=[{"tool": "extract_and_compute", "attribute_handle": None, "source_handles": []}]), repeat=True)
    for title, value in (("א", "מגורים ב׳"), ("ב", "מגורים ב׳"), ("ג", "מגורים ג׳")):
        add_doc(a, a.default_group_id, title, [f"סיווג המגרש הוא {value}."])
        script(p, title, mention(f"סיווג המגרש הוא {value}", value, source="C1", unit=None, term="סיווג המגרש"))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "אילו סיווגי מגרש מופיעים בשומות?")["answer"]
    assert ans["kind"] == "numeric" and ans["numeric"]["value_type"] == "text"
    assert ans["preliminary"]["values"] == ["מגורים ב׳", "מגורים ג׳"] and ans["preliminary"]["record_count"] == 3
    assert "מגורים ב׳ (2)" in ans["text"]


def test_locate_lists_only_documents_that_carry_the_whole_topic(client, db, monkeypatch):
    """GQ12-GQ16: a document that shares one word of the topic is not listed when others carry all of it."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    hit, _ = add_doc(a, a.default_group_id, "שומה עם אישור", ["התקבל אישור עירוני לתוספת קומה בשנת 2021."])
    add_doc(a, a.default_group_id, "שומה אחרת", ["הבניין מקבל אישור אכלוס חלקי בלבד."])
    add_doc(a, a.default_group_id, "שומה שלישית", ["תיאור הדירה והסביבה."])
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="locate", search_queries=["אישור לתוספת קומה"],
        steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "באילו שומות מוזכר אישור לתוספת קומה?")["answer"]
    assert ans["kind"] == "content" and {s["document_id"] for s in ans["sources"]} == {str(hit)}
    assert "שומה עם אישור" in ans["text"] and "שומה אחרת" not in ans["text"]


def test_new_question_read_as_a_change_of_the_pending_clarification_keeps_it_open(client, records, monkeypatch):
    """GQ52 (AE5): a self-contained question about another attribute while a clarification is pending is a
    new question; the pending clarification stays open."""
    add_chunks(records, records.default_group_id, CONTENT_TEXTS, "c" * 64)
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="answer", turn_relation="change_clarification", search_queries=[CONTENT_Q],
        attribute={"handle": None, "description": "היטל השבחה", "unit_dimension": None, "value_type": "text"},
        steps=[SEARCH]))
    p.on(Purpose.ANSWER, claims((SUPPORTED, ["E1"]))).on(Purpose.VERIFY, verdicts(1))
    enable_cloud(records, monkeypatch, p)
    login(client, "admin-a@example.test")
    r = post(client, "מה מחיר למ״ר בחרוזים?")
    assert r["answer"]["clarification"]["key"] == "data_kind"
    a = post(client, CONTENT_Q, r["conversation_id"])["answer"]
    assert a["kind"] == "content" and "עדיין פתוחה" in a["interpretation_note"]
    assert conversation(client, r["conversation_id"])["pending_clarification"]["key"] == "data_kind"
    stored = scalar(records, "SELECT plan FROM questions WHERE question_text = :q", q=CONTENT_Q)
    assert stored["turn_plan"]["turn_relation"] == "new_question"


def test_no_matching_records_abstention_says_not_found(client, records):
    """GQ47: the price path's no-records abstention carries its kind."""
    login(client, "admin-a@example.test")
    a = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2021 בחרוזים")["answer"]
    assert a["kind"] == "abstain" and a["abstention_kind"] == "not_found" and a["numeric"] is None


def test_combined_answer_keeps_the_computations_abstention_kind(client, safe_rooms, monkeypatch):
    """GQ45: nothing extracted and the composed part fell back to passages: the answer is still an abstention
    of the computation's kind, not an answer without one."""
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="compute_explain", metric="mean", conditions={"city": "רמת גן"}, search_queries=["שטח ממ״ד"],
        attribute={"handle": None, "description": SAFE_ROOM, "unit_dimension": "area", "value_type": "numeric"},
        steps=[{"tool": "extract_and_compute", "attribute_handle": None, "source_handles": []}, SEARCH]))
    for title in ("שומה אחת", "שומה שתיים", "שומה שלוש"):
        script(p, title)
    p.on(Purpose.ANSWER, _first_evidence("טענה שאינה נתמכת")).on(
        Purpose.VERIFY, {"verdicts": [{"claim": 0, "verdict": "unsupported"}]})
    enable_cloud(safe_rooms, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, SAFE_ROOM_Q)["answer"]
    assert a["kind"] == "combined" and a["numeric"] is None and a["preliminary"] is None
    assert a["abstention_kind"] == "not_stated"


def test_address_that_names_no_document_is_a_limitation_not_an_unknown_place(client, addresses, monkeypatch):
    """An address no visible document names: the search runs over every authorized document and says so."""
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="answer", entities=["רחוב הזית 7"], search_queries=["שטח ממ״ד"], steps=[SEARCH]))
    p.on(Purpose.ANSWER, _first_evidence("בדירה ממ״ד בשטח 12 מ״ר")).on(Purpose.VERIFY, verdicts(1))
    enable_cloud(addresses, monkeypatch, p)
    login(client, "admin-a@example.test")
    a = post(client, "מה שטח הממ״ד בדירה ברחוב הזית 7?")["answer"]
    assert a["kind"] == "content" and a["abstention_kind"] is None
    assert turn.UNMATCHED_NOTE.format(entity="רחוב הזית 7") in a["limitations"]
    assert turn.UNSCOPED_NOTE in a["limitations"]


def test_choosing_the_second_source_resumes_the_comparison(client, db, monkeypatch):
    """Code review #6: a comparison with one known side asks for the second; choosing it (a button) must run the
    comparison of both sides, not ask again."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    one, _ = add_chunks(a, a.default_group_id, ["שיעור ההתאמה לגודל בשומה הראשונה הוא 5%."], "1" * 64)
    two, _ = add_chunks(a, a.default_group_id, ["שיעור ההתאמה לגודל בשומה השנייה הוא 7%."], "2" * 64)
    p = ScriptedProvider()
    p.on(Purpose.INTERPRET, plan(task_type="answer", search_queries=["שיעור ההתאמה לגודל"], steps=[SEARCH]))
    p.on(Purpose.ANSWER, claims(("שיעור ההתאמה לגודל בשומה הראשונה הוא 5%", ["E1"]),
                                ("שיעור ההתאמה לגודל בשומה השנייה הוא 7%", ["E2"])))
    p.on(Purpose.VERIFY, verdicts(2))
    p.on(Purpose.INTERPRET, plan(task_type="compare", turn_relation="follow_up",
                                 search_queries=["שיעור ההתאמה לגודל"],
                                 steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1"]}]))
    p.on(Purpose.ANSWER, {**claims(("שיעור ההתאמה לגודל בשומה הראשונה הוא 5%", ["E1"]),
                                   ("שיעור ההתאמה לגודל בשומה השנייה הוא 7%", ["E2"])),
                          "conflicts": [{"datum": "שיעור ההתאמה לגודל", "claims": [0, 1]}]})
    p.on(Purpose.VERIFY, verdicts(2))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    first = post(client, "מה שיעור ההתאמה לגודל?")
    cid = first["conversation_id"]
    asked = post(client, "השווה את זה למקור אחר", cid)["answer"]
    assert asked["kind"] == "clarification" and asked["clarification"]["key"] == "referent"
    option = asked["clarification"]["options"][0]["value"]
    done = post(client, None, cid, clarification={"key": "referent", "value": option})["answer"]
    assert done["kind"] != "clarification", done.get("text")
    # both documents are the comparison's sides, each with its own evidence (which side the scripted claims
    # land on depends on the order the first answer cited them)
    assert {x["document_id"] for x in done["compare"]["sides"]} == {str(one), str(two)}
    assert all(x["evidence_count"] >= 1 for x in done["compare"]["sides"])


def test_a_document_lacking_only_a_word_found_nowhere_is_listed_with_that_caveat(client, db, monkeypatch):
    """Round 8 (existing set T04): a word misread in a scanned report occurs in no document; the document that
    holds every other word of the question is listed, and the answer names the word it could not find. A
    document matching only some of the other words stays a near source (see the GQ43 case)."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    add_chunks(a, a.default_group_id, ["תוכנן חיזוק המבנה במסגרת תכנית 38 לפני מספר שנים."], "9" * 64)
    add_chunks(a, a.default_group_id, ["בדירה מטבח משופץ ומרפסת שמש."], "a" * 64)
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="locate", search_queries=["חיזוק במסגרת תמ״א 38"],
        steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "באיזו שומה מוזכר חיזוק במסגרת תמ״א 38?")["answer"]
    assert ans["kind"] == "content" and "דוח 999" in ans["text"]
    assert any("«תמ״א»" in x for x in ans["limitations"])


def test_a_word_only_the_rephrasing_added_never_blocks_the_document_the_user_asked_for(client, db, monkeypatch):
    """Existing set T07, final run: the interpreter's variants added words found in no document; the document
    holding every word the user wrote must still be listed."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    add_chunks(a, a.default_group_id, ["השווי נקבע בסכום של כ-1.25 מ׳ ₪ לפי גישת ההשוואה."], "b" * 64)
    add_chunks(a, a.default_group_id, ["הדירה ממוקמת בקומה שלישית ופונה לחזית."], "d" * 64)
    add_chunks(a, a.default_group_id, ["בבניין ארבע קומות ומעלית אחת."], "e" * 64)
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="locate", search_queries=["סכום כ-1.25 מיליון שקלים"],
        steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "באיזה מסמך מופיע סכום של כ-1.25 מ׳ ₪?")["answer"]
    assert ans["kind"] == "content" and "דוח bbb" in ans["text"]


def test_an_address_alone_does_not_list_a_document_for_a_topic_found_nowhere(client, db, monkeypatch):
    """Final run, GQ43: "what is written about X at Y 18" with X in no document: the document at that address
    is a near source at most, never the answer."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    add_chunks(a, a.default_group_id, ["הנכס ברחוב הדקלים 18 הוא דירת 4 חדרים."], "c" * 64)
    p = ScriptedProvider().on(Purpose.INTERPRET, plan(
        task_type="locate", search_queries=["סאונה הדקלים 18"],
        steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "מה נכתב על סאונה בהדקלים 18?")["answer"]
    assert ans["kind"] == "abstain"


# --- narrative reports with no structured records (real office files) ---------------------------------------------

def test_a_price_question_over_narrative_reports_is_answered_from_their_text(client, db, monkeypatch):
    """Real reports produced no records and named their city only in prose: "מה המחיר למ״ר ממוצע בעיר X" answered
    "no records for X" without looking at the documents. The city is known because a document names it, the
    documents are placed by their own titles, and the price per m² is extracted from their text with quotes."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    add_doc(a, a.default_group_id, "שומה - הדקל 5 חולון", ["הנכס ממוקם בחולון.", "שווי למ״ר: 20000 ₪."])
    add_doc(a, a.default_group_id, "שומה - הדקל 9 חולון", ["הנכס ממוקם בחולון.", "שווי למ״ר: 24000 ₪."])
    add_doc(a, a.default_group_id, "שומה - הגפן 1 בת ים", ["הנכס ממוקם בבת ים.", "שווי למ״ר: 90000 ₪."])
    p = ScriptedProvider()
    for title, v in (("שומה - הדקל 5 חולון", "20000"), ("שומה - הדקל 9 חולון", "24000"),
                     ("שומה - הגפן 1 בת ים", "90000")):
        script(p, title, mention(f"שווי למ״ר: {v} ₪", v, source="C2", unit="₪", term="שווי למ״ר"))
    enable_cloud(a, monkeypatch, p)
    login(client, "admin-a@example.test")
    ans = post(client, "מה המחיר למ״ר ממוצע בחולון?")["answer"]
    assert "אין במאגר המשרד רשומות" not in ans["text"]
    assert ans["kind"] in ("numeric", "combined"), ans["text"]
    pre = ans["preliminary"] or {}
    assert pre.get("value") == "22000.00" and pre.get("record_count") == 2  # the other city's report is left out
    assert any("חולצו מטקסט המסמכים" in x for x in ans["limitations"])
    assert any("לפי הכותרת או הפתיח" in x for x in ans["limitations"])
