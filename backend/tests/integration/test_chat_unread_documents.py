"""Documents the user may see that were not read, in the chat (round 9 U6, KTD11; R5, R18, R25; AE1).

A first version that failed, is waiting or is being processed is never current, so every lookup over current versions
was blind to it: the chat answered "not found in the documents checked" about a file the documents screen shows. These
tests prove what the server now does: a title match on such a document is a ``document_not_read`` outcome with its state
and reason (never a search over it), a component whose named document was not read gets that reason in its gap line, and
a turn that located nothing gets one "not checked" notice listing the unread files. The model is scripted; the files
are synthetic rows (a failed or processing version inserted next to the readable ones)."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import entities
from app.chat.coverage import REASONS
from app.db import TenantContext, tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_coverage import add_doc
from tests.support.scripted_agent import ScriptedAgent, call, component, final, requirement

pytestmark = pytest.mark.db

TOO_LONG = "המסמך ארוך מהמותר: 1,000 עמודים (המותר 400)"
FAILED_TITLE = "שומה - הזית 9 נווה ים"
PROCESSING_TITLE = "שומה - התמר 5 אור עקיבא"
SECRET_TITLE = "שומה - האורן 3 סודי"
READY_TITLE = "שומה - הגפן 12 רמת השרון"
PROJECT_TITLE = "שומה מרוכזת - מתחם הרצל"


def add_unread(office, group, title: str, status: str = "failed", reason: str | None = TOO_LONG,
               ingestion: dict | None = None, minutes_ago: int = 0, sha: str | None = None) -> str:
    """A document whose only version is not read: failed (with its reason), pending or processing. Never current."""
    doc, ver = make_document(office, group, title, sha=sha or f"{abs(hash(title)) % 10**12:064d}")
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "UPDATE document_versions SET status = :s, status_reason = :r, is_current = false,"
            " ingestion = CAST(:i AS jsonb), created_at = now() - make_interval(mins => :m) WHERE id = :v"),
            {"s": status, "r": reason if status == "failed" else None, "i": json.dumps(ingestion or {}),
             "m": minutes_ago, "v": ver})
    return str(doc)


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g1, g2 = a.default_group_id, make_group(a, "קבוצה 2")
    a.emp = make_user(a, "emp@example.test", [g1])
    a.ready = add_doc(a, g1, READY_TITLE, ["שומה לנכס ברמת השרון: השווי למ\"ר בנוי הוא 9,500 ₪."], "1" * 64)
    a.failed = add_unread(a, g1, FAILED_TITLE)
    a.processing = add_unread(a, g1, PROCESSING_TITLE, "processing",
                              ingestion={"stage": "reading", "pages_done": 12, "pages_total": 80})
    a.secret = add_unread(a, g2, SECRET_TITLE)
    return a


def _emp(office) -> TenantContext:
    return office.ctx("employee", office.emp)


# --- the lookup (entities) -------------------------------------------------------------------------------------------


def test_a_failed_document_named_by_title_words_is_not_read_with_its_reason(setup):
    out = entities.lookup(setup.ctx(), "הזית 9", set())
    assert out.kind == "not_read" and [d.title for d in out.documents] == [FAILED_TITLE]
    state = out.documents[0].unread
    assert (state["status"], state["reason"]) == ("failed", TOO_LONG)


def test_a_processing_document_carries_its_progress(setup):
    out = entities.lookup(setup.ctx(), "התמר 5", set())
    assert out.kind == "not_read" and out.documents[0].unread["status"] == "processing"
    assert out.documents[0].unread["progress"]["pages_done"] == 12
    assert out.documents[0].unread["progress"]["pages_total"] == 80


def test_a_ready_document_is_unaffected(setup):
    out = entities.lookup(setup.ctx(), "הגפן 12", set())
    assert out.kind == "resolved" and [d.title for d in out.documents] == [READY_TITLE]


def test_words_that_name_nothing_carry_the_files_that_were_not_read(setup):
    out = entities.lookup(setup.ctx(), "פרויקט כרמל-ים", set())
    assert out.kind == "not_found"
    assert {d["title"] for d in out.unread} == {FAILED_TITLE, PROCESSING_TITLE, SECRET_TITLE}


def test_a_failed_document_of_a_group_the_user_cannot_see_is_never_named(setup):
    out = entities.lookup(_emp(setup), "האורן 3", set())
    assert out.kind == "not_found"
    assert SECRET_TITLE not in json.dumps([d["title"] for d in out.unread], ensure_ascii=False)
    assert {d["title"] for d in out.unread} == {FAILED_TITLE, PROCESSING_TITLE}
    assert SECRET_TITLE not in entities.titles_of(_emp(setup))
    assert SECRET_TITLE in entities.titles_of(setup.ctx())


def test_a_deleted_unread_document_is_not_named(setup):
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": setup.failed})
    out = entities.lookup(setup.ctx(), "הזית 9", set())
    assert out.kind == "not_found" and FAILED_TITLE not in {d["title"] for d in out.unread}


def test_a_document_whose_older_version_is_current_is_still_read(setup):
    """A re-upload that failed leaves the earlier version current: the chat answers from it, as before."""
    doc = add_doc(setup, setup.default_group_id, "שומה - הדס 2 גבעת זאב", ["שווי הנכס 3,100,000 ₪."], "9" * 64)
    with tenant_tx(setup.system) as conn:
        conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, status, status_reason, is_current) VALUES (app_office(), :d, 2, :s, 'f.pdf',"
            " 'application/pdf', 10, 'k', 'failed', :r, false)"), {"d": doc, "s": "8" * 64, "r": TOO_LONG})
    out = entities.lookup(setup.ctx(), "הדס 2", set())
    assert out.kind == "resolved" and out.documents[0].document_id == doc


# --- the turn: tools, gap lines, notice -------------------------------------------------------------------------------

NOT_FOUND = {"label": "שווי השומה", "document_ids": [], "status": "not_found_search", "checked_where": ""}


def _ask(client, setup, monkeypatch, steps, question, email="admin-a@example.test", **kw):
    agent = ScriptedAgent(steps, **kw)
    cloud(monkeypatch, setup, agent)
    login(client, email)
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return m, agent


def test_a_failed_document_named_by_title_words_gets_its_reason_not_not_found(client, setup, monkeypatch):
    """AE1."""
    m, agent = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="הזית 9 נווה ים")],
        final("לא ניתן היה לקרוא את המסמך.", requested=[NOT_FOUND])], "מה שווי השומה בהזית 9?")
    seen = agent.tool_outputs(1)[0]
    assert FAILED_TITLE in seen and TOO_LONG in seen and "לא נקראו" in seen  # the model is told, with the reason
    a = m["answer"]
    (gap,) = [g for g in a["gaps"] if g["reason"] == "document_not_read"]
    assert FAILED_TITLE in gap["text"] and TOO_LONG in gap["text"] and "לא נקרא" in gap["text"]
    assert "לא נמצא בחיפוש" not in a["markdown"] and "לא אותר" not in a["markdown"]
    assert a["markdown"].count(FAILED_TITLE) == 1  # the gap names it; the notice does not repeat it
    assert "gap_paragraph" in {s["kind"] for s in a["server_spans"]}
    assert a["status"] == "partial"


def test_a_component_whose_document_was_not_read_has_the_new_reason(client, setup, monkeypatch):
    def judge(input: str) -> dict:
        units = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": [requirement(id=i, status="missing") for i in ids]}

    m, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="הזית 9 נווה ים")],
        final("לא ניתן היה לקרוא את המסמך.")], "מה שווי השומה בהזית 9?", judge=judge,
        request=[component("שווי השומה בהזית 9")])
    a = m["answer"]
    (c,) = [c for c in a["ledger"]["requirements"] if c["limitation"]]
    assert c["limitation"] == "document_not_read" and c["document_id"] == setup.failed
    assert c["limitation_text"] == REASONS["document_not_read"]
    assert a["verification"]["completeness"]["missing"][0]["reason"] == "document_not_read"
    assert FAILED_TITLE in a["markdown"] and "לא נמצא בחיפוש" not in a["markdown"]


def test_a_processing_document_is_still_being_read_with_progress(client, setup, monkeypatch):
    m, agent = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="התמר 5 אור עקיבא")],
        final("המסמך עדיין לא זמין לבדיקה.", requested=[NOT_FOUND])], "מה שווי השומה בהתמר 5?")
    assert "12 מתוך 80" in agent.tool_outputs(1)[0]
    gap = next(g for g in m["answer"]["gaps"] if g["reason"] == "document_not_read")
    assert PROCESSING_TITLE in gap["text"] and "עדיין" in gap["text"] and "12 מתוך 80 עמודים" in gap["text"]
    assert "לא נמצא" not in m["answer"]["markdown"]


def test_a_project_held_only_in_a_failed_file_gets_the_not_checked_notice(client, setup, monkeypatch):
    add_unread(setup, setup.default_group_id, PROJECT_TITLE, reason=TOO_LONG, minutes_ago=5)
    m, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="פרויקט כרמל-ים", document_ids=None, limit=None)],
        final("לא נמצאו קטעים רלוונטיים.", requested=[NOT_FOUND])], "מה שווי פרויקט כרמל-ים?")
    a = m["answer"]
    md = a["markdown"]
    assert "לא נקראו, ולכן לא נבדקו" in md
    for title in (PROJECT_TITLE, FAILED_TITLE, PROCESSING_TITLE):
        assert f"«{title}»" in md
    assert TOO_LONG in md and "12 מתוך 80" in md
    assert SECRET_TITLE.split()[-1] in md  # the admin sees every group
    kinds = [s["kind"] for s in a["server_spans"]]
    assert kinds.index("gap_paragraph") < kinds.index("unread_notice")
    notice = next(s for s in a["server_spans"] if s["kind"] == "unread_notice")
    assert md[notice["start"]:notice["end"]].lstrip().startswith("קבצים שאתה רשאי לראות לא נקראו")
    assert a["status"] == "partial"
    assert {d["title"] for d in a["ledger"]["unread_documents"]} >= {PROJECT_TITLE, FAILED_TITLE}


def test_more_than_five_unread_files_list_five_ordered_by_the_question_and_count_the_rest(client, setup, monkeypatch):
    with tenant_tx(setup.system) as conn:  # only the files below
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = ANY(:i)"),
                     {"i": [setup.failed, setup.processing, setup.secret]})
    for i in range(1, 7):  # newest first by number: ארכיון 1 is the most recent upload
        add_unread(setup, setup.default_group_id, f"תיק ארכיון {i}", minutes_ago=i)
    add_unread(setup, setup.default_group_id, "תיק אלמוג 1", minutes_ago=500)
    add_unread(setup, setup.default_group_id, "תיק אלמוג 2", minutes_ago=400)
    m, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="אלמוג", document_ids=None, limit=None)],
        final("לא נמצאו קטעים רלוונטיים.", requested=[NOT_FOUND])], "מה השווי בתיק אלמוג?")
    md = m["answer"]["markdown"]
    listed = re.findall(r"^- «(.+?)» —", md, re.M)
    assert len(listed) == 5
    assert listed[:2] == ["תיק אלמוג 2", "תיק אלמוג 1"]  # the title shares a word with the question, newest first
    assert listed[2:] == ["תיק ארכיון 1", "תיק ארכיון 2", "תיק ארכיון 3"]  # then the most recent uploads
    assert "ועוד 3 קבצים שלא נקראו." in md


def test_a_ready_document_is_answered_without_a_notice(client, setup, monkeypatch):
    m, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי למ\"ר בנוי", document_ids=[setup.ready], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", documents=[setup.ready])], "מה השווי למ\"ר בהגפן 12?")
    a = m["answer"]
    assert a["status"] == "answered" and "9,500" in a["markdown"]
    assert "לא נקראו" not in a["markdown"] and "unread_notice" not in {s["kind"] for s in a["server_spans"]}


def test_a_failed_document_of_an_unseen_group_is_never_named_anywhere(client, setup, monkeypatch):
    m, agent = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="האורן 3"), call("search", query="האורן 3", document_ids=None, limit=None)],
        final("לא נמצאו קטעים רלוונטיים.", requested=[NOT_FOUND])], "מה שווי השומה באורן 3?",
        email="emp@example.test")
    everything = json.dumps(m, ensure_ascii=False) + json.dumps(agent.seen, ensure_ascii=False, default=str)
    assert "סודי" not in everything and setup.secret not in everything
    assert FAILED_TITLE in m["answer"]["markdown"]  # the files the user may see are still listed as not checked
    assert "document_not_read" not in {g["reason"] for g in m["answer"]["gaps"]}
