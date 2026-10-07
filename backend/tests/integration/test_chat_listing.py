"""A count or a list over a set of documents rests on a cited listing (find_documents / list_documents): the
server checks the count against it, never accepts a count from nowhere, marks a count from a partly read listing
partial, and keeps a listing out of the documents an answer is about while its documents stay under the
permission check (KTD6 of plan 2026-10-07-1643). The model is scripted; synthetic documents only."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_coverage import PLACE
from tests.integration.test_chat_coverage import setup as setup  # noqa: F401 - the shared fixture
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

QUESTION = f"כמה שומות יש לנו ב{PLACE}, ועל אילו נכסים?"


def _ask(client, setup, monkeypatch, steps):
    agent = ScriptedAgent(steps)
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    m = send(client, cid, QUESTION)
    assert m["status"] == "done", m
    return m["answer"], cid


def test_a_count_that_cites_the_listing_is_verified_and_covers_the_set(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        final(f"יש לנו 2 שומות ב{PLACE} [S1]: הגפן 12 והזית 7 [S1].", scope="set", scope_query=PLACE)])
    assert a["status"] == "answered" and "2 שומות" in a["markdown"]
    assert a["ledger"]["membership"] and a["ledger"]["complete"]
    assert "תוכן המסמכים לא נקרא" in a["markdown"] and "מבוססת על 0" not in a["markdown"]
    (listing,) = [s for s in a["sources"] if s["kind"] == "listing"]
    assert set(listing["listed_document_ids"]) == {setup.d1, setup.d2} and listing["document_id"] is None
    assert "document_id=" not in listing["text"]
    assert a["documents"] == []  # a listing is not a document the answer is about


def test_a_count_without_the_listing_or_with_another_number_is_removed(client, setup, monkeypatch):
    for count, markdown in (("2", f"יש לנו 2 שומות ב{PLACE}."), ("3", f"יש לנו 3 שומות ב{PLACE} [S1].")):
        rest = final(markdown, scope="set", scope_query=PLACE)
        a, _ = _ask(client, setup, monkeypatch, [[call("find_documents", query=PLACE, page=None)], rest, rest, rest])
        assert f"{count} שומות" not in a["markdown"] and a["verification"]["removed"] >= 1, markdown


def test_a_count_from_a_partly_read_listing_is_partial(client, setup, monkeypatch):
    for n in range(30):  # one page holds 30 documents
        make_document(setup, setup.default_group_id, f"שומה רחוב {n} {PLACE}", sha=f"{n:064d}")
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        final(f"יש לנו 32 שומות ב{PLACE} [S1].", scope="set", scope_query=PLACE)])
    assert a["status"] == "partial" and "הספירה חלקית" in a["markdown"] and not a["ledger"]["complete"]


def test_revoking_a_listed_document_hides_the_answer(client, setup, monkeypatch):
    _, cid = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        final(f"יש לנו 2 שומות ב{PLACE} [S1].", scope="set", scope_query=PLACE)])
    with tenant_tx(setup.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": setup.d2})
    msgs = client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]
    answer = [m for m in msgs if m["role"] == "assistant"][-1]
    assert "2 שומות" not in (answer.get("content") or "")


def test_a_count_from_a_title_query_listing_describes_that_listing(client, setup, monkeypatch):
    # find_documents set one scope; the count cites a list_documents page with another criterion
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="שווי", page=None)],
        [call("list_documents", query="הגפן", page=None)],
        final("יש לנו מסמך אחד שכותרתו כוללת את המילה הגפן [S2].", scope="set", scope_query="הגפן")])
    assert "רשימת 1 מסמכים שכותרתם כוללת" in a["markdown"] and a["ledger"]["complete"]
    assert [d["title"] for d in a["ledger"]["matching"]] == [f"שומה הגפן 12 {PLACE}"]


def test_a_follow_up_after_a_count_does_not_offer_the_listing_as_a_reference(client, setup, monkeypatch):
    _, cid = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        final(f"יש לנו 2 שומות ב{PLACE} [S1].", scope="set", scope_query=PLACE)])
    agent = ScriptedAgent([final("לא נמצא.", "not_found")])
    agent.on("agent", {"relation": "same_datum", "scope": "entity", "standalone_question": "?", "changed_fields": [],
                       "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown",
                       "area_basis": "", "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""})
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    send(client, cid, "ומה עוד?")
    assert "הפניות למקורות שצוטטו" not in agent.seen[0][0]["content"]
