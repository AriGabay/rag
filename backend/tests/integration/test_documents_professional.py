"""What the document list and detail show of a standard (round 10 U8: R18, R19, R24, R30; U10 renders it).

A professional document comes with a ``professional`` block: the metadata it states with the page and quote of each
value (or unknown), the state of its derivations (``pending``, ``ready``, ``failed``), the editions of its number with
whether their order is known, and its copies. An appraisal has none. Everything is read under the caller's own
permissions: a user outside the document's group is shown neither the document nor a link to it. Synthetic data only."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import professional
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.support.professional_files import MARK, professional_file, standard_blocks

pytestmark = pytest.mark.db


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    make_user(a, "outsider@example.test", [a.default_group_id])
    return a


def listed(client) -> dict:
    return {d["title"].rsplit(" — ", 1)[-1]: d for d in client.get("/api/documents").json()["documents"]}


def derive(office, version) -> None:
    assert professional.after_publish(office.system, version)


def test_a_standard_lists_what_it_states_with_page_and_quote_and_its_state(client, office):
    doc, ver = professional_file(office, office.default_group_id, standard_blocks("98.1"), title="התקן")
    login(client, "admin-a@example.test")
    assert listed(client)["התקן"]["professional"]["state"] == "pending"  # nothing derived yet
    derive(office, ver)
    shown = listed(client)["התקן"]["professional"]
    assert shown["state"] == "ready"
    number = shown["profile"]["number"]
    assert number["value"] == "98.1" and number["status"] == "stated" and number["page"] == 1
    assert "98.1" in number["quote"] and number["method"] == "pattern"
    assert shown["profile"]["edition"]["value"] == "גרסה 2.0"
    assert shown["profile"]["effective"]["value"] == "05.10.2025" and shown["profile"]["effective"]["normalized"] == "2025-10-05"
    # what the document does not state is unknown, never filled in
    assert shown["profile"]["issuer"] == {"value": None, "status": "unknown", "method": None, "page": None,
                                          "block": None, "quote": None}
    detail = client.get(f"/api/documents/{doc}").json()
    assert detail["professional"] == shown and detail["source_kind"] == "standard"


def test_an_appraisal_has_no_professional_block(client, office):
    professional_file(office, office.default_group_id, [("paragraph", None, [], MARK, 1)], title="שומה",
                      source_kind="appraisal")
    login(client, "admin-a@example.test")
    assert "professional" not in listed(client)["שומה"]


def test_a_failed_derivation_is_reported_as_failed(client, office):
    _, ver = professional_file(office, office.default_group_id, standard_blocks("98.1"), title="התקן")
    derive(office, ver)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = ingestion || CAST(:e AS jsonb) WHERE id = :v"),
                     {"v": ver, "e": json.dumps({"professional_error": {"stage": "profile", "error": "RuntimeError"}})})
    login(client, "admin-a@example.test")
    assert listed(client)["התקן"]["professional"]["state"] == "failed"


def test_editions_are_listed_with_their_order_and_copies_with_the_text_they_repeat(client, office):
    old_doc, old = professional_file(office, office.default_group_id,
                                     standard_blocks("98.1", edition="גרסה 1.0", effective="01.01.2022"), title="ישנה",
                                     sha="1" * 64)
    new_doc, new = professional_file(office, office.default_group_id, standard_blocks("98.1"), title="חדשה", sha="2" * 64)
    copy_doc, copy = professional_file(office, office.default_group_id, standard_blocks("98.1"), title="עותק", sha="3" * 64)
    for v in (old, new, copy):
        derive(office, v)
    login(client, "admin-a@example.test")
    docs = listed(client)
    editions = docs["חדשה"]["professional"]["editions"]
    assert editions["number"] == "98.1" and editions["order_known"] is True
    assert [(e["document_id"], e["edition"], e["effective"]) for e in editions["editions"]] == [
        (old_doc, "גרסה 1.0", "01.01.2022"), (new_doc, "גרסה 2.0", "05.10.2025")]  # the copy is not an edition
    assert [e["current"] for e in editions["editions"]] == [False, True]
    assert docs["חדשה"]["professional"]["duplicates"] == {
        "same_text_as": None, "copies": [{"document_id": copy_doc, "title": docs["עותק"]["title"]}]}
    assert docs["עותק"]["professional"]["duplicates"]["same_text_as"] == {"document_id": new_doc,
                                                                          "title": docs["חדשה"]["title"]}
    assert docs["ישנה"]["professional"]["duplicates"] == {"same_text_as": None, "copies": []}


def test_editions_with_no_stated_dates_have_no_known_order(client, office):
    docs = []
    for i, edition in enumerate(("גרסה 1.0", "גרסה 2.0")):
        docs.append(professional_file(office, office.default_group_id, standard_blocks("98.1", edition=edition,
                                      effective=None), title=f"נוסח {i}", sha=str(i) * 64))
    for _, v in docs:
        derive(office, v)
    login(client, "admin-a@example.test")
    assert listed(client)["נוסח 0"]["professional"]["editions"]["order_known"] is False


def test_a_user_outside_the_group_is_shown_neither_the_document_nor_a_link_to_it(client, office):
    secret, sv = professional_file(office, office.private, standard_blocks("98.1"), title="סודי", sha="a" * 64)
    open_doc, ov = professional_file(office, office.default_group_id, standard_blocks("98.1", edition="גרסה 3.0"),
                                     title="גלוי", sha="b" * 64)
    derive(office, sv)
    derive(office, ov)
    login(client, "outsider@example.test")
    docs = listed(client)
    assert set(docs) == {"גלוי"}
    editions = docs["גלוי"]["professional"]["editions"]
    assert [e["document_id"] for e in editions["editions"]] == [open_doc]
    assert secret not in json.dumps(docs, ensure_ascii=False)
    assert client.get(f"/api/documents/{secret}").status_code == 404


def test_a_document_changed_to_a_standard_is_derived_at_once_from_its_stored_blocks(client, office):
    # round 10 U10 review: the documents screen shows the standard's metadata right after an admin changes the kind,
    # not a derivation left pending until a chat tool happens to touch the document
    doc, ver = professional_file(office, office.default_group_id, standard_blocks("97.0"), title="הועלה כשומה",
                                 source_kind="appraisal")
    login(client, "admin-a@example.test")
    assert "professional" not in listed(client)["הועלה כשומה"] or not listed(client)["הועלה כשומה"]["professional"]
    r = client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"})
    assert r.status_code == 200
    shown = listed(client)["הועלה כשומה"]["professional"]
    assert shown["state"] == "ready" and shown["profile"]["number"]["value"] == "97.0"
