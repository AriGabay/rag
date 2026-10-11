"""A follow-up that changes the property, the unit or the document is answered from the document the user's words
name, looked up under the user's permissions — never from the previous turn's documents (KTD1, KTD2 of plan
2026-10-07-1643). The resolving and answering models are scripted; these tests prove what the server decides.
Synthetic documents and invented places only."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.chat import contexts, entities
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_coverage import add_doc
from tests.integration.test_chat_unread_documents import TOO_LONG, add_unread
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.unit_files import reread, unit_file

pytestmark = pytest.mark.db


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = a.default_group_id
    a.dror = add_doc(a, g, "שומה - הדרור 5 גבעת השקד", [
        "דירה A3 בבניין ברחוב הדרור 5. השווי למ\"ר אקוו' נקבע ל-13,700 ₪.",
        "עסקת השוואה: דירה ברחוב הסנונית 12 נמכרה ב-2,050,000 ₪."], "1" * 64)
    a.snunit = add_doc(a, g, "שומה - הסנונית 12 עין ורד", [
        "דירה B7 בקומה 4 ברחוב הסנונית 12. שווי הדירה נקבע ל-2,410,000 ₪, כולל מע\"מ."], "2" * 64)
    a.narkis_a = add_doc(a, g, "שומה - הנרקיס 4 עין ורד", ["בניין מגורים ברחוב הנרקיס 4. השווי 8,100,000 ₪."], "3" * 64)
    a.narkis_b = add_doc(a, g, "שומה - הנרקיס 4 כפר גפן", ["מבנה מסחרי ברחוב הנרקיס 4. השווי 5,300,000 ₪."], "4" * 64)
    b = make_office(db, "משרד ב", "admin-b@example.test")
    a.foreign = add_doc(b, b.default_group_id, "שומה - הסנונית 12 עין ורד", ["מסמך של משרד אחר."], "5" * 64)
    return a


def _resolution(**kw) -> dict:
    return {"relation": "correction", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _focus(doc: str, subject: str) -> dict:
    return {"metric_as_written": "השווי למ\"ר", "metric_kind": "value_per_area", "unit": "ILS_per_sqm",
            "period": "none", "area_basis": "מ\"ר אקוו'", "vat": "unknown", "subject": subject,
            "value_role": "appraiser_determination", "document_ids": [doc]}


def _first_turn(client, setup, monkeypatch) -> str:
    first = ScriptedAgent([[call("search", query="שווי למ\"ר", document_ids=[setup.dror], limit=None)],
                           final("השווי למ\"ר אקוו' נקבע ל-13,700 ₪ [S1].", focus=_focus(setup.dror, "הדרור 5"))])
    cloud(monkeypatch, setup, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "מה השווי למ״ר בהדרור 5?")["status"] == "done"
    return cid


def _turn(monkeypatch, steps, resolution) -> ScriptedAgent:
    agent = ScriptedAgent(steps)
    agent.on("resolve", resolution)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    return agent


def test_another_property_is_answered_from_its_own_document(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    agent = _turn(monkeypatch, [
        [call("search", query="שווי דירה B7", document_ids=[setup.snunit], limit=None)],
        final("שווי הדירה נקבע ל-2,410,000 ₪, כולל מע\"מ [S1].")],
        _resolution(changed_fields=[{"field": "subject", "user_words": "לדירה B7 ברחוב הסנונית 12"}],
                    subject="הסנונית 12, דירה B7", document_ids=[setup.dror]))
    m = send(client, cid, "טעיתי, התכוונתי לדירה B7 ברחוב הסנונית 12")
    assert m["status"] == "done", m
    assert m["answer"]["request"]["document_ids"] == [setup.snunit]
    task = agent.seen[0][0]["content"]
    assert setup.snunit in task and setup.dror not in task and "הנתון שבמרכז השיחה" not in task
    assert "החליף נכס" in task


def test_a_similar_address_in_two_towns_asks_then_resolves_a_bare_reply(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    agent = _turn(monkeypatch, [], _resolution(relation="new_question",
                                               changed_fields=[{"field": "subject", "user_words": "בהנרקיס 4"}]))
    m = send(client, cid, "ובהנרקיס 4?")
    a = m["answer"]
    assert a["status"] == "clarification" and agent.seen == []  # no answering step
    assert "עין ורד" in a["markdown"] and "כפר גפן" in a["markdown"]
    assert {c["document_id"] for c in a["request"]["candidates"]} == {setup.narkis_a, setup.narkis_b}
    reply = _turn(monkeypatch, [[call("search", query="שווי", document_ids=[setup.narkis_b], limit=None)],
                                final("השווי 5,300,000 ₪ [S1].")],
                  _resolution(relation="clarification_answer"))
    m2 = send(client, cid, "בכפר גפן")
    assert m2["answer"]["request"]["document_ids"] == [setup.narkis_b]
    assert setup.dror not in reply.seen[0][0]["content"]


def test_a_property_that_is_not_there_asks_and_never_answers_from_the_previous_one(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    agent = _turn(monkeypatch, [], _resolution(relation="new_question",
                                               changed_fields=[{"field": "subject", "user_words": "בהיסמין 3"}]))
    a = send(client, cid, "ובהיסמין 3?")["answer"]
    assert a["status"] == "clarification" and "לא מצאתי" in a["markdown"] and agent.seen == []
    assert "13,700" not in a["markdown"]


def test_a_clarification_is_hidden_once_a_document_it_named_is_revoked(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    _turn(monkeypatch, [], _resolution(relation="new_question",
                                       changed_fields=[{"field": "subject", "user_words": "בהנרקיס 4"}]))
    send(client, cid, "ובהנרקיס 4?")
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": setup.narkis_a})
    msgs = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]
    clarification = [m for m in msgs if m["role"] == "assistant"][-1]
    assert "עין ורד" not in (clarification.get("content") or "")
    assert "כפר גפן" not in (clarification.get("content") or "")


def test_the_lookup_never_sees_another_offices_documents(setup):
    # the other office holds a document with the same title; the focus report mentions the address as a comparable
    assert entities.authorized(setup.ctx(), [setup.foreign, setup.snunit]) == {setup.snunit}
    outcome = entities.lookup(setup.ctx(), "הסנונית 12", {setup.dror})
    assert [d.document_id for d in outcome.documents] == [setup.snunit]


def test_the_resolution_is_diagnostics_only_and_follows_the_documents_it_names(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    _turn(monkeypatch, [], _resolution(relation="new_question", document_ids=[setup.foreign],
                                       changed_fields=[{"field": "subject", "user_words": "בהנרקיס 4"},
                                                       {"field": "scale", "user_words": "הכולל"}]))
    m = send(client, cid, "ובהנרקיס 4?")
    assert "parse" not in m["answer"]["request"] and "decisions" not in str(m["answer"])
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]
    assert d["parse"]["relation"] == "new_question" and d["parse"]["document_ids"] == []  # not another office's
    assert d["decisions"]["subject"].startswith("accepted") and d["decisions"]["scale"] == "rejected: not_in_message"
    assert d["lookup"]["kind"] == "ambiguous"
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": setup.narkis_b})
    assert client.get(f"/api/chat/messages/{m['id']}/diagnostics").status_code == 404


# --- documents that were not read (round 9 U6, KTD11; AE1) -------------------------------------------------------------


def test_a_follow_up_naming_a_failed_document_says_it_was_not_read_and_why(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    add_unread(setup, setup.default_group_id, "שומה - הזית 9 נווה ים")
    agent = _turn(monkeypatch, [], _resolution(relation="new_question",
                                               changed_fields=[{"field": "subject", "user_words": "בהזית 9"}]))
    a = send(client, cid, "ובהזית 9?")["answer"]
    assert a["status"] == "clarification" and agent.seen == []  # no answering step, no search over the file
    assert "שומה - הזית 9 נווה ים" in a["markdown"] and TOO_LONG in a["markdown"]
    assert "לא נקרא" in a["markdown"] or "העיבוד נכשל" in a["markdown"]
    assert "לא מצאתי" not in a["markdown"] and "13,700" not in a["markdown"]
    assert a["request"]["candidates"] == []  # nothing to choose between: a reply is a new question


def test_a_follow_up_that_names_nothing_lists_the_files_that_were_not_read(client, setup, monkeypatch):
    cid = _first_turn(client, setup, monkeypatch)
    add_unread(setup, setup.default_group_id, "קובץ מרוכז - מתחם הרצל")
    _turn(monkeypatch, [], _resolution(relation="new_question",
                                       changed_fields=[{"field": "subject", "user_words": "בהיסמין 3"}]))
    a = send(client, cid, "ובהיסמין 3?")["answer"]
    assert a["status"] == "clarification" and "לא מצאתי" in a["markdown"]
    assert "קבצים שאתה רשאי לראות לא נקראו, ולכן לא נבדקו" in a["markdown"]
    assert "קובץ מרוכז - מתחם הרצל" in a["markdown"] and TOO_LONG in a["markdown"]


# --- the unit in the conversation's focus (round 9 U10, KTD8, R14) --------------------------------------------------------


def test_a_follow_up_in_a_file_of_one_unit_carries_no_unit(client, setup, monkeypatch):
    """A report that is one appraisal behaves as before: no unit in the answer, the focus or the request."""
    cid = _first_turn(client, setup, monkeypatch)
    _turn(monkeypatch, [[call("search", query="דמי שכירות", document_ids=[setup.dror], limit=None)],
                        final("לא נמצאו דמי שכירות.", "not_found")],
          _resolution(relation="metric_change", changed_fields=[{"field": "metric_kind", "user_words": "דמי שכירות"}],
                      metric_kind="rent"))
    m = send(client, cid, "ומה דמי שכירות?")
    a = m["answer"]
    assert a["unit"] is None and "unit_scope" not in a
    assert a["request"]["located_unit"] is None and a["request"]["unit_kept"] is False
    assert "located_unit" not in (a["focus"] or {})


@pytest.fixture
def units_file(setup):
    """A file of three appraisals (synthetic names), read once."""
    setup.units_doc, setup.units_ver = unit_file(setup, setup.default_group_id, "קובץ שומות סינתטי", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": ["פרויקט הארז", "ב"]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "identifiers": ["רחוב האשל 9"], "blocks": ["מתחם האשל", "ב"]},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": ["פרויקט הברוש", "ב"]}])
    contexts._CACHE.clear()
    return setup


def _anchor_of(setup, name: str) -> dict:
    (c,) = entities.lookup(setup.ctx(), name, set()).documents
    return c.unit


def _live(setup, anchor: dict) -> entities.LiveUnit:
    return entities.live_unit(setup.ctx(), anchor)


def test_a_unit_is_current_while_its_reading_is_and_is_found_by_its_anchor_after_a_reindex(units_file):
    anchor = _anchor_of(units_file, "האשל")
    live = _live(units_file, anchor)
    assert live.status == "current" and live.unit["unit_no"] == 2 and live.version_id == units_file.units_ver
    # the file is read again with a unit in front: the number changes, the unit does not
    reread(units_file, units_file.units_ver, "reading-units-2", [
        {"kind": "front_matter", "blocks": (0, 0), "pages": (1, 1)},
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (1, 1), "pages": (1, 1)},
        {"title": "מתחם האשל", "aliases": ["האשל"], "identifiers": ["רחוב האשל 9"], "blocks": (2, 3), "pages": (2, 2)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (4, 5), "pages": (3, 3)}])
    contexts._CACHE.clear()
    moved = _live(units_file, anchor)
    assert moved.status == "moved" and (moved.unit["unit_no"], moved.unit["reading_id"]) == (3, "reading-units-2")
    assert moved.unit["display"] == "מתחם האשל" and entities.same_unit(anchor, moved.unit)
    assert _live(units_file, moved.unit).status == "current"


def test_a_unit_renamed_by_a_reindex_is_found_by_its_first_page_label_and_identifier(units_file):
    with tenant_tx(units_file.system) as conn:  # the second appraisal starts on a page that prints the label 7
        conn.execute(text("UPDATE pages SET printed_label = '7' WHERE version_id = :v AND page_no = 2"),
                     {"v": units_file.units_ver})
    anchor = _anchor_of(units_file, "האשל")
    assert anchor["first_label"] == "7"
    reread(units_file, units_file.units_ver, "reading-units-2", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (0, 1), "pages": (1, 1)},
        # renamed in the new reading; its identifier and the printed label of its first page are the same
        {"title": "מתחם הנחלים", "aliases": [], "identifiers": ["רחוב האשל 9"], "blocks": (2, 3), "pages": (2, 2)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (4, 5), "pages": (3, 3)}])
    contexts._CACHE.clear()
    moved = _live(units_file, anchor)
    assert moved.status == "moved" and moved.unit["display"] == "מתחם הנחלים" and moved.unit["unit_no"] == 2
    # without the label the identifier alone is not enough
    assert _live(units_file, anchor | {"first_label": None}).status == "gone"


def test_a_unit_that_vanished_is_gone_with_the_files_appraisals_to_ask_about(units_file):
    anchor = _anchor_of(units_file, "האשל")
    reread(units_file, units_file.units_ver, "reading-units-3", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (0, 1), "pages": (1, 1)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (2, 5), "pages": (2, 3)}])
    contexts._CACHE.clear()
    live = _live(units_file, anchor)
    assert live.status == "gone" and live.unit is None
    assert [c.unit["display"] for c in live.candidates] == ["פרויקט הארז", "פרויקט הברוש"]
    assert all(c.document_id == units_file.units_doc for c in live.candidates)  # the old number 2 selects no one


def test_a_file_that_is_one_unit_now_or_not_visible_drops_the_unit_without_a_question(units_file):
    anchor = _anchor_of(units_file, "האשל")
    reread(units_file, units_file.units_ver, "reading-units-4", [
        {"title": "קובץ אחד", "aliases": [], "identifiers": [], "blocks": (0, 5), "pages": (1, 3)}])
    contexts._CACHE.clear()
    assert _live(units_file, anchor).status == "single"
    with tenant_tx(units_file.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": units_file.units_doc})
    assert _live(units_file, anchor).status == "unreadable"
    assert _live(units_file, anchor | {"document_id": "not-an-id"}).status == "unreadable"


def test_the_anchor_of_a_turns_unit_is_none_for_an_appendix_a_single_unit_or_a_newer_reading(units_file):
    ok = entities.anchor_of(units_file.ctx(), units_file.units_doc, "reading-units-1", 2)
    assert ok["display"] == "מתחם האשל" and ok["reading_id"] == "reading-units-1"
    assert entities.anchor_of(units_file.ctx(), units_file.units_doc, "reading-units-0", 2) is None  # read again since
    assert entities.anchor_of(units_file.ctx(), units_file.units_doc, "reading-units-1", 9) is None
    assert entities.anchor_of(units_file.ctx(), units_file.dror, None, 1) is None  # a file of one unit
