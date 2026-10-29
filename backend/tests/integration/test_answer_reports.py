"""Report a problematic answer (round 25 U5: R11, AE6, KTD9). Synthetic data only; the strings below are invented.

A user reports an answer of their own; the report keeps the ids and a STRUCTURAL snapshot of the run's diagnostics (codes,
statuses, timings, locators, counts: never a draft, a quote, a label, a cell or a failure's free text), so a later retry or
deletion does not lose it. The office admin lists, reads and exports the reports of their office; the export carries no note."""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import ANSWER, cloud, new_conversation, send
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call

pytestmark = pytest.mark.db

DOC_TEXTS = ["סיכום: השווי למ\"ר בנוי למסחר הוא 9,500 ₪ ודמי השכירות 55 ₪ למ\"ר/חודש.", "תיאור: מבנה מסחרי ברחוב הגפן."]


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    g2 = make_group(a, "קבוצה 2")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id])
    a.emp2 = make_user(a, "emp2@example.test", [a.default_group_id])
    a.g2, a.other = g2, b
    return a


def _ids(office) -> dict:
    """Two real documents of the office (the admin's views show only documents that exist and that the admin sees)."""
    one, v1 = make_document(office, office.default_group_id, title="מסמך ראשון", sha=uuid.uuid4().hex * 2)
    two, v2 = make_document(office, office.default_group_id, title="מסמך שני", sha=uuid.uuid4().hex * 2)
    return {"doc": str(one), "ver": str(v1), "doc2": str(two), "ver2": str(v2)}


def seed(office, user_id, *, status: str = "done", answer: dict | None = None, error: str | None = None,
         diagnostics: dict | None = None, model: str | None = "gpt-test", role: str = "assistant", age: int = 5) -> dict:
    """A conversation with one asked message and one assistant message (``role='user'`` makes the second a user message)."""
    with tenant_tx(office.system) as conn:
        cid = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                " (app_office(), :u, 'שיחה', 'rag') RETURNING id"), {"u": user_id}).scalar_one()
        asked = conn.execute(text(
            "INSERT INTO messages (office_id, conversation_id, user_id, role, status, content, created_at, updated_at)"
            " VALUES (app_office(), :c, :u, 'user', 'done', 'מה השווי?', now() - interval '20 seconds', now())"
            " RETURNING id"), {"c": cid, "u": user_id}).scalar_one()
        reply = conn.execute(text(
            "INSERT INTO messages (office_id, conversation_id, user_id, role, status, content, reply_to, answer, error, model,"
            " created_at, updated_at) VALUES (app_office(), :c, :u, :r, :s, 'תשובה', :a, CAST(:an AS jsonb), :e, :mo,"
            " now() - interval '10 seconds', now() - make_interval(secs => :age)) RETURNING id"),
            {"c": cid, "u": user_id, "r": role, "s": status, "a": asked if role == "assistant" else None,
             "an": json.dumps(answer) if answer is not None else None, "e": error, "mo": model, "age": age}).scalar_one()
        if diagnostics is not None:
            conn.execute(text(
                "INSERT INTO message_diagnostics (message_id, office_id, user_id, rounds, removed, resolution, document_ids,"
                " limits_hit) VALUES (:m, app_office(), :u, CAST(:r AS jsonb), CAST(:x AS jsonb), CAST(:res AS jsonb), :d,"
                " CAST(:l AS jsonb))"),
                {"m": reply, "u": user_id, "r": json.dumps(diagnostics.get("rounds", [])),
                 "x": json.dumps(diagnostics.get("removed", [])), "res": json.dumps(diagnostics.get("resolution")),
                 "d": diagnostics.get("document_ids", []), "l": json.dumps(diagnostics.get("limits_hit", []))})
    return {"cid": str(cid), "asked": str(asked), "mid": str(reply)}


def post_report(client, mid: str, category: str = "wrong", note: str | None = "התשובה אינה נכונה"):
    body = {"category": category} | ({"note": note} if note is not None else {})
    return client.post(f"/api/chat/messages/{mid}/report", json=body)


def rows(office) -> list:
    with tenant_tx(office.system) as conn:
        return list(conn.execute(text("SELECT * FROM answer_reports ORDER BY created_at")))


def walk(value):
    """Every (key, value) pair and every string at any depth."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


# --- the secrets of the value test: invented strings that must never reach a snapshot or an export -------------------------

LABEL = "שורת-קרן-דוגמה-7781"
QUOTE = "הציטוט-הסודי-של-השורה-4471 והסכום 1,234,567 ₪"
DRAFT = "טיוטת-תשובה-פנימית-5512 דמי הניהול הם 77 ₪"
DETAIL = "תקלה-חופשית-9921 מטקסט הספק"
ASCII_LABEL = "ZX-4471-label"
ASCII_QUOTE = "Rent was 12,345 per month"


def crafted(ids: dict, label: str = LABEL, quote: str = QUOTE, draft: str = DRAFT) -> dict:
    """A diagnostics row of the shapes the engine writes, with document text in every place it can appear."""
    locator = {"type": "source", "document_id": ids["doc"], "version_id": ids["ver"], "reading_id": "reading-1",
               "block_start": 3, "block_end": 4, "table_index": 1, "row_index": 2, "chunk_id": None, "table_part": None,
               "kind": "table_row"}
    return {
        "document_ids": [ids["doc"], ids["doc2"]], "limits_hit": ["tool_budget"],
        "rounds": [[{"text": draft, "reason": f"{quote} לא נמצא במקור", "severity": "error", "kind": "claim",
                     "failure_kind": "absent_from_source", "check": "unstated_number", "component": "N1",
                     "checked_ids": ["S1"], "repair_attempted": True, "claim_id": "c1", "cause": "", "basis": "",
                     "resolution": None}]],
        "removed": [{"claim_id": "c1", "action": "removed", "text": draft, "remaining": "", "verdict": "unsupported",
                     "reason": "absent_from_source", "failure_kind": "absent_from_source", "check": "unstated_number",
                     "component": "N1", "cause": "", "sources_checked": ["S1"],
                     "sources": [{"type": "sources", "id": "S1", "document_id": ids["doc"], "version_id": ids["ver"],
                                  "title": label, "text": quote, "section": quote, "location": label, "kind": "table_row",
                                  "block_start": 3, "block_end": 4, "table_index": 1, "row_index": 2,
                                  "reading_id": "reading-1", "label": ASCII_LABEL, "quote": ASCII_QUOTE}],
                     "rereads": [{"source": "S1", "outcome": "kept", "reason": quote}], "post_check": "n/a"}],
        "resolution": {
            "intent": {"value": "professional", "question": quote},
            "timing": {"total_seconds": 41.5, "stages": {"read": {"seconds": 12.0, "calls": 3}}, "unattributed_seconds": 1.0,
                       "tools": [{"tool": "read", "stage": "read", "seconds": 1.2, "chars": 900, "pages": [2, 3]}],
                       "not_checked": {"timeout": 2}, "reads": {"D1:p2-3": {"tool": "read", "attempted": True,
                                                                           "completed": False, "failed_with": "time_limit",
                                                                           "budget_skipped": False, "calls": 1}},
                       "tables_served": 1, "multi_page_tables": 0,
                       "repair_gate": [{"attempt": 1, "decision": "refused", "steps": 4, "refused_by": "time"}],
                       "unfollowed_cursors": []},
            "requirements_trace": [{"id": "N1", "final_status": "partial", "history": [], "history_omitted": 0,
                                    "votes": [{"call": 1, "reused": False, "status": "partial", "passages": {"rendered": True, "chars": 800, "ids": ["S1"]},
                                               "missing": quote, "support_omitted": 0, "version": 1, "origin": "fresh",
                                               "call_units": [0],
                                               "support": [{"id": "S1", "quote": quote, "outcome": "accepted"}]}],
                                    "decision": {"status": "partial", "version": 1, "digest": "ab12cd34", "rests_on": [],
                                                 "caps": [], "evidence": ["S1"], "reused": False},
                                    "items": [{"id": "I1", "origin": "judge", "source": "S1", "state": "omitted", "declared": True,
                                               "why": "value_not_stated", "covered_by": None, "label": label, "quote": quote,
                                               "value": "1,234,567 ₪", "condition": quote, "reason": quote,
                                               "condition_check": None, "condition_hint": quote}]}],
            "item_ledger": {"items": [{"id": "I1", "component": "N1", "state": "omitted", "document_id": ids["doc"],
                                       "label": label, "quote": quote, "value": "1,234,567 ₪"}],
                            "excluded": [{"source": "S1", "quote": quote, "reason": "not_in_source"}]},
            "drafts": [{"round": 1, "version": 1, "markdown": draft, "sources": {"S1": locator}}],
            "failure": {"status": "timeout", "detail": f"final_text: {DETAIL}", "stage": "verify"},
        },
    }


ANSWER_JSON = {"kind": "rag", "status": "answered", "markdown": DRAFT,
               "sources": [{"id": "S1", "document_id": "DOC", "version_id": "VER", "title": LABEL, "text": QUOTE,
                            "block_start": 3, "block_end": 4, "table_index": 1, "row_index": 2, "reading_id": "reading-1",
                            "kind": "table_row", "page_list": [2], "status": "ok", "section": QUOTE, "location": LABEL}],
               "verification": {"judged": True, "removed": 1, "partial": 0, "annotated": 0}}


def with_ids(answer: dict, ids: dict) -> dict:
    out = json.loads(json.dumps(answer))
    out["sources"][0]["document_id"], out["sources"][0]["version_id"] = ids["doc"], ids["ver"]
    return out


# --- the report ---------------------------------------------------------------------------------------------------------

def test_the_owner_reports_a_done_answer_and_the_report_keeps_the_ids_the_note_and_the_snapshot(client, office, monkeypatch):
    monkeypatch.setenv("APP_BUILD_SHA", "abc1234def")
    ids = _ids(office)
    s = seed(office, office.emp, answer=with_ids(ANSWER_JSON, ids), diagnostics=crafted(ids))
    login(client, "emp@example.test")
    r = post_report(client, s["mid"], "wrong_source", "המקור שגוי")
    assert r.status_code == 200, r.text
    body = r.json()
    (row,) = rows(office)
    assert body["id"] == str(row.id) and body["category"] == "wrong_source" and body["note"] == "המקור שגוי"
    assert str(row.message_id) == s["mid"] and str(row.conversation_id) == s["cid"] and row.user_id == office.emp
    assert row.office_id == office.office_id and row.message_status == "done" and row.build_sha == "abc1234def"
    assert set(row.model_ids) >= {"agent", "resolve", "verify", "message"} and row.model_ids["message"] == "gpt-test"
    snap = row.snapshot
    assert snap["counts"]["rounds"] == 1 and snap["counts"]["removed"] == 1
    assert [x["id"] for x in snap["cited_sources"]] == ["S1"] and snap["cited_sources"][0]["document_id"] == ids["doc"]
    assert snap["resolution"]["timing"]["repair_gate"] == [{"attempt": 1, "decision": "refused", "steps": 4, "refused_by": "time"}]
    assert snap["resolution"]["drafts"][0]["sources"]["S1"]["row_index"] == 2   # the locator stays, the draft goes
    assert snap["limits_hit"] == ["tool_budget"] and set(snap["document_ids"]) == {ids["doc"], ids["doc2"]}


def test_the_build_sha_is_unknown_when_the_image_set_none(client, office, monkeypatch):
    monkeypatch.delenv("APP_BUILD_SHA", raising=False)
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    assert post_report(client, s["mid"]).status_code == 200
    assert rows(office)[0].build_sha == "unknown"


def test_a_second_report_by_the_same_user_updates_the_first(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    first = post_report(client, s["mid"], "wrong", "ראשון").json()
    second = post_report(client, s["mid"], "missing", "שני")
    assert second.status_code == 200 and second.json()["id"] == first["id"]
    (row,) = rows(office)
    assert row.category == "missing" and row.note == "שני" and str(row.id) == first["id"]
    # the user's own report for the conversation comes back, so the marker reopens prefilled
    listed = client.get(f"/api/chat/conversations/{s['cid']}/reports").json()["reports"]
    assert listed == [{"id": first["id"], "message_id": s["mid"], "category": "missing", "note": "שני"}]


def test_reporting_without_stored_diagnostics_still_works_with_a_minimal_snapshot(client, office):
    s = seed(office, office.emp)   # no diagnostics row (an answer from before diagnostics, or a cancelled turn)
    login(client, "emp@example.test")
    assert post_report(client, s["mid"], "other", None).status_code == 200
    (row,) = rows(office)
    assert row.note == "" and row.snapshot["counts"]["rounds"] == 0 and row.snapshot["document_ids"] == []


def test_a_failed_answer_can_be_reported_and_its_snapshot_holds_the_cause_as_a_code(client, office):
    diag = {"rounds": [], "removed": [], "document_ids": [],
            "resolution": {"failure": {"status": "timeout", "detail": f"final_text: {DETAIL}"}}}
    s = seed(office, office.emp, status="failed", error="אירעה תקלה. אפשר לנסות שוב.", diagnostics=diag)
    login(client, "emp@example.test")
    assert post_report(client, s["mid"], "other", "נכשל").status_code == 200
    (row,) = rows(office)
    assert row.message_status == "failed"
    assert row.snapshot["resolution"]["failure"] == {"status": "timeout", "stage": "final_text"}
    assert DETAIL not in json.dumps(row.snapshot, ensure_ascii=False) and "אירעה" not in json.dumps(row.snapshot, ensure_ascii=False)


@pytest.mark.parametrize("status_", ["cancelled", "failed"])
def test_a_cancelled_or_failed_answer_without_diagnostics_can_be_reported(client, office, status_):
    s = seed(office, office.emp, status=status_, error="העיבוד נעצר לבקשתך.")
    login(client, "emp@example.test")
    assert post_report(client, s["mid"]).status_code == 200 and rows(office)[0].message_status == status_


def test_a_clarification_can_be_reported(client, office):
    s = seed(office, office.emp, answer={"kind": "rag", "status": "needs_clarification", "markdown": "לאיזה נכס?"})
    login(client, "emp@example.test")
    assert post_report(client, s["mid"]).status_code == 200
    assert rows(office)[0].snapshot["answer"] == {"kind": "rag", "status": "needs_clarification"}


def test_a_running_answer_gets_409_and_a_stale_one_is_reported_as_failed(client, office):
    live = seed(office, office.emp, status="running")
    stale = seed(office, office.emp, status="running", age=100000)
    login(client, "emp@example.test")
    assert post_report(client, live["mid"]).status_code == 409
    assert post_report(client, seed(office, office.emp, status="cancelling")["mid"]).status_code == 409
    assert post_report(client, stale["mid"]).status_code == 200 and rows(office)[0].message_status == "failed"
    assert len(rows(office)) == 1


def test_a_user_message_an_unknown_id_and_a_bad_id_get_404(client, office):
    s = seed(office, office.emp)
    login(client, "emp@example.test")
    assert post_report(client, s["asked"]).status_code == 404   # the user's own question
    assert post_report(client, str(uuid.uuid4())).status_code == 404
    assert post_report(client, "not-a-uuid").status_code == 404
    assert rows(office) == []


def test_another_user_of_the_same_office_gets_404_even_for_an_admin(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp2@example.test")
    assert post_report(client, s["mid"]).status_code == 404
    login(client, "admin-a@example.test")   # messages are owner-only, admins included
    assert post_report(client, s["mid"]).status_code == 404
    assert rows(office) == []


def test_an_unauthenticated_request_gets_401(client, office):
    assert post_report(client, str(uuid.uuid4())).status_code == 401
    assert client.get("/api/admin/answer-reports").status_code == 401


def test_the_category_and_the_note_are_validated(client, office):
    s = seed(office, office.emp)
    login(client, "emp@example.test")
    assert post_report(client, s["mid"], "").status_code == 422
    assert post_report(client, s["mid"], "no_such").status_code == 422
    assert client.post(f"/api/chat/messages/{s['mid']}/report", json={}).status_code == 422
    assert post_report(client, s["mid"], "wrong", "א" * 1001).status_code == 422
    assert rows(office) == []
    assert post_report(client, s["mid"], "wrong", "א" * 1000).status_code == 200
    assert len(rows(office)[0].note) == 1000


def test_the_office_comes_from_the_session_only(client, office):
    s = seed(office, office.emp)
    login(client, "emp@example.test")
    other = str(office.other.office_id)
    r = client.post(f"/api/chat/messages/{s['mid']}/report", json={"category": "wrong", "office_id": other,
                                                                   "user_id": str(office.emp2)})
    assert r.status_code == 200
    (row,) = rows(office)
    assert row.office_id == office.office_id and row.user_id == office.emp


# --- AE6: a retry or a deleted conversation leaves the report --------------------------------------------------------------

def test_report_then_retry_the_report_remains_with_an_empty_message_link_and_its_snapshot(client, office, monkeypatch):
    monkeypatch.setattr(chat_api, "_start", lambda *a: None)   # the retry creates the new running row; no turn is run
    ids = _ids(office)
    s = seed(office, office.emp, diagnostics=crafted(ids))
    login(client, "emp@example.test")
    rid = post_report(client, s["mid"]).json()["id"]
    before = rows(office)[0].snapshot
    r = client.post(f"/api/chat/messages/{s['mid']}/retry")
    assert r.status_code == 200 and r.json()["id"] != s["mid"]
    (row,) = rows(office)
    assert str(row.id) == rid and row.message_id is None and str(row.conversation_id) == s["cid"]
    assert row.snapshot == before and row.snapshot["counts"]["removed"] == 1
    with tenant_tx(office.system) as conn:   # retry deleted the old message and its diagnostics, as it always did
        assert conn.execute(text("SELECT count(*) FROM message_diagnostics")).scalar_one() == 0
    login(client, "admin-a@example.test")   # the admin opens it all the same, and the list says the message is gone
    detail = client.get(f"/api/admin/answer-reports/{rid}").json()
    assert detail["message_id"] is None and detail["message_exists"] is False and detail["snapshot"] == before
    assert client.get("/api/admin/answer-reports").json()["reports"][0]["message_exists"] is False
    login(client, "emp@example.test")   # the new answer is a new message: no marker on it
    assert client.get(f"/api/chat/conversations/{s['cid']}/reports").json()["reports"] == []


def test_deleting_the_conversation_keeps_the_report_with_empty_links(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    rid = post_report(client, s["mid"]).json()["id"]
    assert client.delete(f"/api/chat/conversations/{s['cid']}").status_code == 200
    (row,) = rows(office)
    assert str(row.id) == rid and row.message_id is None and row.conversation_id is None and row.snapshot["counts"]["rounds"] == 1
    login(client, "admin-a@example.test")
    assert client.get(f"/api/admin/answer-reports/{rid}").json()["message_exists"] is False


# --- the admin ------------------------------------------------------------------------------------------------------------

def test_the_admin_lists_newest_first_reads_and_exports_the_office_reports_and_an_employee_cannot(client, office):
    ids = _ids(office)
    first = seed(office, office.emp, diagnostics=crafted(ids))
    second = seed(office, office.emp2, status="failed", error="נכשל", diagnostics=crafted(ids))
    login(client, "emp@example.test")
    r1 = post_report(client, first["mid"], "wrong", "הערה ראשונה").json()["id"]
    login(client, "emp2@example.test")
    r2 = post_report(client, second["mid"], "missing", "הערה שנייה").json()["id"]
    login(client, "emp@example.test")
    for url in ("/api/admin/answer-reports", f"/api/admin/answer-reports/{r1}", f"/api/admin/answer-reports/{r1}/export"):
        assert client.get(url).status_code == 403   # another user of the office, not an admin
    login(client, "admin-a@example.test")
    listed = client.get("/api/admin/answer-reports").json()["reports"]
    assert [x["id"] for x in listed] == [r2, r1]   # newest first
    assert {k: listed[0][k] for k in ("category", "message_status", "message_exists")} == {
        "category": "missing", "message_status": "failed", "message_exists": True}
    assert listed[0]["created_at"] and "note" not in listed[0] and "snapshot" not in listed[0]
    detail = client.get(f"/api/admin/answer-reports/{r1}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["note"] == "הערה ראשונה" and d["category"] == "wrong" and d["message_status"] == "done"
    assert d["message_id"] == first["mid"] and d["snapshot"]["counts"]["rounds"] == 1
    exp = client.get(f"/api/admin/answer-reports/{r1}/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    e = exp.json()
    assert "note" not in e and "הערה ראשונה" not in exp.text and e["report_id"] == r1 and e["snapshot"] == d["snapshot"]
    assert client.get(f"/api/admin/answer-reports/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/admin/answer-reports/nope/export").status_code == 404


def test_an_admin_of_another_office_sees_nothing_and_gets_404(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    rid = post_report(client, s["mid"]).json()["id"]
    login(client, "admin-b@example.test")
    assert client.get("/api/admin/answer-reports").json()["reports"] == []
    assert client.get(f"/api/admin/answer-reports/{rid}").status_code == 404
    assert client.get(f"/api/admin/answer-reports/{rid}/export").status_code == 404


def test_reading_and_exporting_a_report_of_another_user_is_audited(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    rid = post_report(client, s["mid"]).json()["id"]
    login(client, "admin-a@example.test")
    client.get(f"/api/admin/answer-reports/{rid}")
    client.get(f"/api/admin/answer-reports/{rid}/export")
    with tenant_tx(office.system) as conn:
        actions = [r.action for r in conn.execute(text(
            "SELECT action FROM audit_events WHERE target_id = :r ORDER BY id"), {"r": rid})]
    assert actions == ["answer_report.create", "answer_report.read", "answer_report.export"]


# --- what the snapshot holds and what it never does -----------------------------------------------------------------------

@pytest.mark.parametrize("label,quote,draft", [
    (LABEL, QUOTE, DRAFT),
    ("שורה אחרת ב-קרן-3302", "ציטוט אחר-של-המשרד 98,765 ₪", "טיוטה-שונה-0042 בגובה 3 ₪"),
])
def test_no_row_label_quote_draft_or_failure_text_reaches_the_snapshot_or_the_export(client, office, label, quote, draft):
    ids = _ids(office)
    s = seed(office, office.emp, answer=with_ids(ANSWER_JSON, ids), diagnostics=crafted(ids, label, quote, draft))
    login(client, "emp@example.test")
    post_report(client, s["mid"], "wrong", "הערה-פרטית-6613")
    (row,) = rows(office)
    stored = json.dumps(row.snapshot, ensure_ascii=False)
    login(client, "admin-a@example.test")
    detail = client.get(f"/api/admin/answer-reports/{row.id}")
    export = client.get(f"/api/admin/answer-reports/{row.id}/export")
    secrets = {label, quote, draft, DETAIL, ASCII_LABEL, ASCII_QUOTE, "1,234,567", "98,765"}
    for where, blob in (("stored", stored), ("detail snapshot", json.dumps(detail.json()["snapshot"], ensure_ascii=False)),
                        ("export", export.text), ("export (unicode-escaped)", json.dumps(export.json()))):
        for secret in secrets:
            assert secret not in blob, (where, secret)
        assert "ZX-4471" not in blob and "12,345" not in blob and "אירעה" not in blob
    assert "הערה-פרטית-6613" in detail.text and "הערה-פרטית-6613" not in export.text and "הערה-פרטית-6613" not in stored
    snap = row.snapshot
    # the structure is still there
    assert snap["resolution"]["timing"]["tools"][0] == {"tool": "read", "stage": "read", "seconds": 1.2, "chars": 900, "pages": [2, 3]}
    assert snap["removed"][0]["sources"][0] == {"type": "sources", "id": "S1", "document_id": ids["doc"], "version_id": ids["ver"],
                                                "kind": "table_row", "block_start": 3, "block_end": 4, "table_index": 1,
                                                "row_index": 2, "reading_id": "reading-1"}
    assert snap["removed"][0]["failure_kind"] == "absent_from_source" and snap["rounds"][0][0]["check"] == "unstated_number"
    vote = snap["resolution"]["requirements_trace"][0]["votes"][0]
    assert vote["support"] == [{"id": "S1", "outcome": "accepted"}] and "missing" not in vote
    item = snap["resolution"]["requirements_trace"][0]["items"][0]
    assert item["id"] == "I1" and item["state"] == "omitted" and not {"label", "quote", "value", "condition"} & set(item)
    assert snap["resolution"]["failure"] == {"status": "timeout", "stage": "final_text"}
    assert "intent" not in snap["resolution"]   # the parse of the question is not part of the snapshot


def test_every_key_of_the_snapshot_and_the_export_is_in_the_allowlist_and_every_string_is_a_code(client, office):
    from app.chat import reports

    ids = _ids(office)
    s = seed(office, office.emp, answer=with_ids(ANSWER_JSON, ids), diagnostics=crafted(ids))
    login(client, "emp@example.test")
    post_report(client, s["mid"])
    login(client, "admin-a@example.test")
    rid = str(rows(office)[0].id)
    for snap in (rows(office)[0].snapshot, client.get(f"/api/admin/answer-reports/{rid}/export").json()["snapshot"]):
        def check(value, parent=None):
            if isinstance(value, dict):
                for k, v in value.items():
                    assert (k in reports.ALLOWED_KEYS) or (parent in reports.MAP_KEYS and reports.SAFE_CODE.match(k)), (parent, k)
                    check(v, k)
            elif isinstance(value, list):
                for v in value:
                    check(v, parent)
            elif isinstance(value, str):
                assert reports.SAFE_CODE.match(value), (parent, value)
        check(snap)


def test_the_structural_filter_drops_what_is_not_a_code_and_what_is_not_allowed():
    from app.chat import reports

    out = reports.structural({
        "status": "partial", "label": "ZX-9", "quote": "x", "reason": "הסבר בעברית", "check": "unstated_number",
        "sources": {"S1": {"document_id": "d1", "title": "כותרת", "block_start": 3}, "הסבר חופשי": {"block_start": 1}},
        "checked_ids": ["S1", "S2 with spaces", "מלל"], "seconds": 1.5, "completed": True, "failed_with": None,
        "decision": "x" * 100, "deep": {"deeper": {"status": "ok"}}})
    assert out == {"status": "partial", "check": "unstated_number", "sources": {"S1": {"document_id": "d1", "block_start": 3}},
                   "checked_ids": ["S1"], "seconds": 1.5, "completed": True, "failed_with": None}


def test_the_filter_is_bounded_in_depth_and_list_length():
    from app.chat import reports

    nest: dict = {"status": "ok"}
    for _ in range(40):
        nest = {"decision": nest}
    flat = {"checked_ids": [f"S{i}" for i in range(1000)]}
    assert len(json.dumps(reports.structural(nest))) < 2000
    assert len(reports.structural(flat)["checked_ids"]) <= reports.LIST_KEPT


def test_a_real_turn_snapshot_holds_only_structure(client, db, monkeypatch):
    from app.chat import reports
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_user(a, "emp@example.test", [a.default_group_id])
    doc, _ = add_chunks(a, a.default_group_id, DOC_TEXTS, "1" * 64)
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, a, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות?")
    assert m["status"] == "done", m
    r = post_report(client, m["id"], "wrong", "בדיקה")
    assert r.status_code == 200, r.text
    (row,) = rows(a)
    blob = json.dumps(row.snapshot, ensure_ascii=False)
    assert "55 ₪" not in blob and "דמי השכירות" not in blob and "9,500" not in blob   # the answer, the question, the document text
    assert row.snapshot["counts"]["sources"] == 1 and [x["id"] for x in row.snapshot["cited_sources"]] == ["S1"]
    assert row.snapshot["cited_sources"][0]["document_id"] == str(doc)
    assert row.snapshot["resolution"]["timing"]["stages"]
    assert reports.SAFE_CODE.match(row.snapshot["answer"]["status"])


# --- visibility -----------------------------------------------------------------------------------------------------------

def test_an_admin_who_cannot_see_a_cited_document_gets_the_views_without_its_id(client, office):
    ids = _ids(office)
    visible, _ = make_document(office, office.default_group_id, title="גלוי")
    hidden, hidden_ver = make_document(office, office.default_group_id, title="נסתר", sha="y" * 64)
    ids |= {"doc": str(visible), "doc2": str(hidden), "ver2": str(hidden_ver)}
    diag = crafted(ids)
    diag["resolution"]["item_ledger"]["items"].append({"id": "I2", "component": "N1", "state": "omitted", "document_id": ids["doc2"]})
    diag["removed"][0]["sources"].append({"type": "sources", "id": "S2", "document_id": ids["doc2"], "version_id": ids["ver2"],
                                          "block_start": 1, "block_end": 1, "reading_id": "r"})
    diag["resolution"]["drafts"][0]["sources"]["S2"] = {"type": "source", "document_id": ids["doc2"], "version_id": ids["ver2"],
                                                        "reading_id": "r", "block_start": 1, "block_end": 1}
    answer = with_ids(ANSWER_JSON, ids)
    answer["sources"].append({"id": "S2", "document_id": ids["doc2"], "version_id": ids["ver2"], "block_start": 1, "block_end": 1})
    s = seed(office, office.emp, answer=answer, diagnostics=diag)
    login(client, "emp@example.test")
    rid = post_report(client, s["mid"]).json()["id"]
    login(client, "admin-a@example.test")
    both = client.get(f"/api/admin/answer-reports/{rid}")
    assert ids["doc2"] in json.dumps(rows(office)[0].snapshot) and ids["doc"] in both.text   # the stored snapshot is whole
    with tenant_tx(office.system) as conn:   # the document is deleted: nobody sees it any more
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": ids["doc2"]})
    for resp in (client.get(f"/api/admin/answer-reports/{rid}"), client.get(f"/api/admin/answer-reports/{rid}/export")):
        assert resp.status_code == 200
        assert ids["doc2"] not in resp.text and ids["ver2"] not in resp.text, resp.text
        assert ids["doc"] in resp.text and "S1" in resp.text   # the visible document and the cited ids stay
    assert ids["doc2"] in json.dumps(rows(office)[0].snapshot)   # nothing in the stored row changed


def test_a_user_who_lost_the_group_of_a_cited_document_can_still_report_and_the_export_holds_ids_only(client, office):
    ids = _ids(office)
    doc, _ = make_document(office, office.g2, title="מסמך סגור")
    ids["doc"] = str(doc)
    s = seed(office, office.emp, answer=with_ids(ANSWER_JSON, ids), diagnostics=crafted(ids))
    login(client, "emp@example.test")   # emp is not in g2: the message is hidden from them, the report is still theirs to make
    assert post_report(client, s["mid"]).status_code == 200
    login(client, "admin-a@example.test")
    rid = str(rows(office)[0].id)
    exp = client.get(f"/api/admin/answer-reports/{rid}/export")
    assert exp.status_code == 200 and str(doc) in exp.text   # the admin sees the document
    assert not any(secret in exp.text for secret in (LABEL, QUOTE, DRAFT, DETAIL, ASCII_LABEL, ASCII_QUOTE))


# --- races ----------------------------------------------------------------------------------------------------------------

def _post_in_thread(client, mid: str, out: list) -> threading.Thread:
    def run():
        try:
            out.append(post_report(client, mid).status_code)
        except Exception as exc:  # noqa: BLE001 - a server error would surface here
            out.append(repr(exc))
    t = threading.Thread(target=run)
    t.start()
    return t


def test_a_report_waiting_for_a_retry_finds_the_message_gone_and_gets_404_not_500(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    out: list = []
    with tenant_tx(office.ctx("employee", office.emp)) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"chat:{s['cid']}"})
        t = _post_in_thread(client, s["mid"], out)
        time.sleep(0.6)
        assert out == []   # the report waits for the lock the retry holds
        conn.execute(text("DELETE FROM messages WHERE id = :m"), {"m": s["mid"]})   # what the retry does
    t.join(10)
    assert out == [404] and rows(office) == []


def test_a_report_waiting_for_a_conversation_delete_gets_404_not_500(client, office):
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    out: list = []
    with tenant_tx(office.ctx("employee", office.emp)) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"chat:{s['cid']}"})
        t = _post_in_thread(client, s["mid"], out)
        time.sleep(0.6)
        conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": s["cid"]})
    t.join(10)
    assert out == [404] and rows(office) == []


def test_a_conversation_delete_that_lands_between_the_lookup_and_the_insert_is_a_404_not_a_foreign_key_error(client, office):
    """The delete does not take the chat lock: the report holds a row lock on the message, so the delete waits for it
    (the report keeps its link, then the link is set NULL) or has finished first (the report finds nothing)."""
    s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
    login(client, "emp@example.test")
    results: list = []
    def report():
        for _ in range(8):
            try:
                results.append(post_report(client, s["mid"]).status_code)
            except Exception as exc:  # noqa: BLE001
                results.append(repr(exc))
    t = threading.Thread(target=report)
    t.start()
    time.sleep(0.05)
    with tenant_tx(office.ctx("employee", office.emp)) as conn:
        conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": s["cid"]})
    t.join(20)
    assert set(results) <= {200, 404}, results
    assert all(r.message_id is None for r in rows(office))


def test_a_report_racing_a_retry_of_the_same_message_never_gives_500(client, office, monkeypatch):
    monkeypatch.setattr(chat_api, "_start", lambda *a: None)
    outcomes: list = []
    for _ in range(4):
        s = seed(office, office.emp, diagnostics=crafted(_ids(office)))
        login(client, "emp@example.test")
        out: list = []
        t = _post_in_thread(client, s["mid"], out)
        try:
            retry = client.post(f"/api/chat/messages/{s['mid']}/retry").status_code
        except Exception as exc:  # noqa: BLE001
            retry = repr(exc)
        t.join(20)
        outcomes.append((out[0], retry))
    for report_status, retry_status in outcomes:
        assert report_status in (200, 404, 409), outcomes
        assert retry_status in (200, 404, 409), outcomes
    assert all(r.message_id is None for r in rows(office))   # whoever won, the report ends with the link set NULL
