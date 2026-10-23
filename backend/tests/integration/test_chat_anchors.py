"""Anchors born in the tools and snapshotted into stored answers (U4, KTD1, KTD4, AE1).

The model is scripted; the stored reading is synthetic and positioned: a page with its geometry, a paragraph with word
spans that writes the same number as a table cell, and a table with cell boxes. These tests prove that the answer
stores, for every cited source and value, a snapshot built from stored data only: the cell's own box (never the
paragraph that writes the same number), page precision with the pinned reading after a reprocess, structured
precision for DOCX, table level for a picture table; that a quote written twice with different numbers is refused;
that an old answer without anchors keeps opening; and that revoking a document hides the answer whose anchor names
it. Synthetic documents only; the project and streets are invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import anchors
from app.chat import api as chat_api
from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import cell, handle_of, meaning, quote, run, take
from tests.support.scripted_agent import ScriptedAgent, call, final, read

pytestmark = pytest.mark.db

TITLE = "שומת פרויקט רחוב הדקל 5"
CAPTION = "סיכום הכנסות"
SECTION = "5. תחשיב"
INCOME = "הכנסות (₪)"
PARAGRAPH = "השווי הכולל נקבע ל-12,450,000 ₪ לפי הטבלה."
TWICE = "השווי הוא 12,000 ₪ לשלב א. השווי הוא 15,000 ₪ לשלב ב."
W, H = 600.0, 800.0
CELL_BOX = [250, 140, 400, 160]
QUESTION = "מה ההכנסות הכוללות?"


def _spans(t: str, top: float) -> list[list]:
    out, x, pos = [], 560.0, 0
    for wi, word in enumerate(t.split(" ")):
        start = t.index(word, pos)
        out.append([0, wi, start, start + len(word), x - 10 * len(word), top, x, top + 12])
        x -= 10 * len(word) + 5
        pos = start + len(word)
    return out


def add_document(office, group=None, *, sha: str = "e" * 64, mime: str = "application/pdf",
                 table_source: str | None = None, cell_boxes: bool = True) -> tuple[str, object]:
    doc, ver = make_document(office, group or office.default_group_id, TITLE, sha=sha)
    pdf = mime == "application/pdf"
    rows = [{"page": 3 if pdf else None, "cells": ["שלב א", "5,200,000"]},
            {"page": 3 if pdf else None, "cells": ["סה\"כ", "12,450,000"]}]
    if pdf and cell_boxes:
        rows[0]["cell_boxes"] = [[400, 120, 500, 140], [250, 120, 400, 140]]
        rows[1]["cell_boxes"] = [[400, 140, 500, 160], CELL_BOX]
    structure = {"headers": ["רכיב", INCOME], "units": [None, "₪"], "caption": CAPTION, "title": [],
                 "notes": ["הסכומים ללא מע\"מ."], "section": SECTION, "block_index": 3, "source": table_source,
                 "rows": rows}
    if pdf and cell_boxes:
        structure["header_boxes"] = [[400, 100, 500, 120], [250, 100, 400, 120]]
    table_kind = "image" if table_source == "vision" else "table"
    blocks = [("heading", SECTION, None, [50, 10, 560, 30], None),
              ("paragraph", PARAGRAPH, None, [50, 40, 560, 60], _spans(PARAGRAPH, 40) if pdf else None),
              ("paragraph", TWICE, None, [50, 70, 560, 90], None),
              (table_kind, CAPTION, 0, [100, 100, 500, 160], None)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 3, mime_type = :m, filename = :f,"
                          " ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "m": mime, "f": "f.pdf" if pdf else "f.docx",
                      "i": json.dumps({"reading_id": "reading-1"})})
        if pdf:
            conn.execute(text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox,"
                " cropbox, rotation, display_width, display_height, printed_label) VALUES (app_office(), :d, :v, 3,"
                " '', 'text_layer', 1, true, CAST(:mb AS jsonb), CAST(:mb AS jsonb), 0, :w, :h, '12')"),
                {"d": doc, "v": ver, "mb": json.dumps([0, 0, W, H]), "w": W, "h": H})
        for i, (kind, t, table, bbox, spans) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, paragraph_no, text, status, table_index, bbox, spans) VALUES (app_office(), :d,"
                " :v, :b, :k, :s, :sp, :pg, :pn, :t, 'read', :ti, CAST(:bb AS jsonb), CAST(:spans AS jsonb))"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": SECTION, "sp": [SECTION], "pg": 3 if pdf else None,
                 "pn": i + 10 if not pdf else None, "t": t, "ti": table,
                 "bb": json.dumps(bbox) if pdf else None, "spans": json.dumps(spans) if spans else None})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, :p, :p, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "p": 3 if pdf else None, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.doc, a.ver = add_document(a)
    return a


def workspace(office, user=None) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx() if user is None else office.ctx("employee", user))
    ws.user_messages = [{"turn": 1, "text": QUESTION, "current": True}]
    return ws


def _source_id(output: str) -> str:
    import re

    return re.search(r'<source id="(S\d+)"', output).group(1)


def _steps(doc: str) -> list:
    return [[call("outline", document=doc)],
            lambda items: [read(table=handle_of([i["output"] for i in items if isinstance(i, dict)
                                                 and i.get("type") == "function_call_output"][-1], CAPTION))],
            [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income"), "סה״כ הכנסות")]]


def _frac(box) -> list[float]:
    return [round(box[0] / W, 4), round(box[1] / H, 4), round(box[2] / W, 4), round(box[3] / H, 4)]


# --- AE1 ------------------------------------------------------------------------------------------------------

def test_ae1_the_value_taken_from_the_cell_is_stored_with_the_cells_box_not_the_paragraph(client, office, monkeypatch):
    agent = ScriptedAgent([*_steps(office.doc), final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].",
                                                      documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    (value,) = m["answer"]["values"]
    a = value["anchor"]
    assert a["precision"] == "cell" and a["reading_id"] == "reading-1" and a["version_id"] == str(office.ver)
    (page,) = a["pages"]
    assert page["page"] == 3 and page["rects"] == [_frac(CELL_BOX)] and page["printed_label"] == "12"
    assert a["table"]["row_label"] == "סה\"כ" and a["table"]["column_header"] == INCOME
    assert a["table"]["unit_note"] == "₪" and a["table"]["header"]["rects"] == [_frac([250, 100, 400, 120])]
    assert "בדפוס 12" in a["location"]["label"] and str(office.ver) not in a["location"]["label"]
    source = next(s for s in m["answer"]["sources"] if s["id"] == value["source_id"])
    assert source["anchor"]["precision"] == "block" and source["anchor"]["table"]["title"] == CAPTION


def test_a_quote_value_is_anchored_to_its_words_in_the_paragraph(office):
    ws = workspace(office)
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = _source_id(T.tool_read(ws, {"section": sec}))
    out = run(ws, "take_value", source=s, locator=quote("נקבע ל-12,450,000 ₪", "12,450,000"),
              meaning=meaning("value"), label="שווי")
    assert out.startswith("V1 נרשם"), out
    payload = {"values": [ws.values["V1"].public()]}
    anchors.attach(ws, payload)
    a = payload["values"][0]["anchor"]
    assert a["precision"] == "span" and len(a["pages"][0]["rects"]) == 3 and len(a["pages"][0]["focus"]) == 1


# --- KTD4 -----------------------------------------------------------------------------------------------------

def test_a_quote_written_twice_with_different_numbers_asks_for_a_longer_quote(office):
    ws = workspace(office)
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = _source_id(T.tool_read(ws, {"section": sec}))
    out = run(ws, "take_value", source=s, locator=quote("השווי הוא 1", "1"), meaning=meaning("value"), label="שווי")
    assert out.startswith("שגיאה") and "ציטוט ארוך יותר" in out and not ws.values


# --- KTD1: a reprocess between the tool call and storing the answer -----------------------------------------------

def test_a_reprocess_before_storing_keeps_page_precision_and_the_pinned_reading(office):
    ws = workspace(office)
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = _source_id(T.tool_read(ws, {"section": sec}))
    assert run(ws, "take_value", source=s, locator=quote("נקבע ל-12,450,000 ₪", "12,450,000"),
               meaning=meaning("value"), label="שווי").startswith("V1")
    with tenant_tx(office.system) as conn:  # read again: new reading id, other boxes at the same block numbers
        conn.execute(text("UPDATE document_versions SET ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": office.ver, "i": json.dumps({"reading_id": "reading-2"})})
        conn.execute(text("UPDATE document_blocks SET bbox = CAST(:b AS jsonb), spans = NULL WHERE version_id = :v"),
                     {"v": office.ver, "b": json.dumps([10, 700, 590, 790])})
    payload = {"values": [ws.values["V1"].public()], "sources": [chat_api._public_source(ws.sources[s])]}
    anchors.attach(ws, payload)
    for item in payload["values"] + payload["sources"]:
        a = item["anchor"]
        assert a["precision"] == "page" and a["degraded"] == anchors.READING_CHANGED
        assert a["reading_id"] == "reading-1" and all(p["rects"] == [] for p in a["pages"])
        assert [p["page"] for p in a["pages"]] == [3]


# --- DOCX and picture tables ------------------------------------------------------------------------------------

def test_a_docx_value_has_structured_precision_and_no_page(office):
    doc, _ = add_document(office, sha="f" * 64, mime="application/vnd.openxmlformats-officedocument."
                                                      "wordprocessingml.document")
    ws = workspace(office)
    t = handle_of(T.tool_outline(ws, doc), CAPTION)
    s = _source_id(T.tool_read(ws, {"table": t}))
    assert run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME), meaning=meaning("income", role="income"),
               label="הכנסות").startswith("V1")
    payload = {"values": [ws.values["V1"].public()]}
    anchors.attach(ws, payload)
    a = payload["values"][0]["anchor"]
    assert a["precision"] == "structured" and a["pages"] == [] and not re.search(r"(?<![א-ת])עמוד(?![א-ת])", a["location"]["label"])
    assert a["structured"]["cell"]["text"] == "12,450,000" and a["structured"]["section_path"] == [SECTION]


def test_a_picture_table_value_is_highlighted_at_table_level(office):
    doc, _ = add_document(office, sha="a" * 64, table_source="vision", cell_boxes=False)
    ws = workspace(office)
    t = handle_of(T.tool_outline(ws, doc), CAPTION)
    s = _source_id(T.tool_read(ws, {"table": t}))
    assert run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME), meaning=meaning("income", role="income"),
               label="הכנסות").startswith("V1")
    payload = {"values": [ws.values["V1"].public()]}
    anchors.attach(ws, payload)
    a = payload["values"][0]["anchor"]
    assert a["precision"] == "region" and a["region"] == "table" and a["degraded"] == anchors.NO_CELL_BOX
    assert a["pages"][0]["rects"] == [_frac([100, 100, 500, 160])] and a["table"]["source"] == "vision"


# --- search keeps a table row's index ----------------------------------------------------------------------------

def test_a_table_row_search_hit_keeps_its_row_index_in_its_anchor(office):
    from app.extraction.normalize_text import normalize_for_search
    from app.platform import pipeline

    t = f"{CAPTION}: רכיב: סה\"כ | {INCOME}: 12,450,000"
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section, text,"
            " normalized_text, table_index, row_index, block_start, block_end) VALUES (app_office(), :d, :v, 0,"
            " 'table_row', :p, :s, :t, :n, 0, 1, 3, 3)"),
            {"d": office.doc, "v": office.ver, "p": [3], "s": SECTION, "t": t, "n": normalize_for_search(t)})
    pipeline.embed_stage(office.system, pipeline.VersionInfo(office.ver, office.doc, "k", "application/pdf", None),
                         1e18)
    ws = workspace(office)
    T.tool_search(ws, "סה\"כ הכנסות", [office.doc], None)
    row = next(s for s in ws.sources.values() if s.kind == "table_row")
    assert row.row_index == 1 and ws.anchors[row.sid]["row"] == 1
    payload = {"sources": [row.public()]}
    anchors.attach(ws, payload)
    a = payload["sources"][0]["anchor"]
    assert a["precision"] == "region" and a["region"] == "row" and a["table"]["row_label"] == "סה\"כ"


# --- old answers and permissions -------------------------------------------------------------------------------

def test_an_old_answer_without_anchors_still_renders_and_its_source_opens(client, office, monkeypatch):
    agent = ScriptedAgent([*_steps(office.doc), final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].",
                                                      documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    with tenant_tx(office.system) as conn:
        answer = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        for key in ("sources", "values", "measurements", "computations"):
            for item in answer.get(key) or []:
                item.pop("anchor", None)
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"),
                     {"a": json.dumps(answer, ensure_ascii=False), "m": m["id"]})
    shown = client.get(f"/api/chat/messages/{m['id']}").json()
    assert shown["status"] == "done" and not shown["answer"].get("hidden")
    assert all("anchor" not in s for s in shown["answer"]["sources"])
    s = shown["answer"]["sources"][0]
    r = client.get(f"/api/documents/{s['document_id']}/versions/{s['version_id']}/blocks",
                   params={"start": s["block_start"], "end": s["block_end"], "reading_id": s["reading_id"]})
    assert r.status_code == 200 and r.json()["blocks"] and not r.json().get("stale")


def test_revoking_a_document_an_anchor_names_hides_the_answer(client, office, monkeypatch):
    secret, _ = add_document(office, office.private, sha="b" * 64)
    agent = ScriptedAgent([*_steps(office.doc), final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].",
                                                      documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    with tenant_tx(office.system) as conn:  # an anchor that names the private document (as its header anchor would)
        answer = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        answer["values"][0]["anchor"]["document_id"] = secret
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"),
                     {"a": json.dumps(answer, ensure_ascii=False), "m": m["id"]})
    assert not client.get(f"/api/chat/messages/{m['id']}").json()["answer"].get("hidden")
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u AND group_id = :g"),
                     {"u": office.emp, "g": office.private})
    shown = client.get(f"/api/chat/messages/{m['id']}").json()
    assert shown["answer"].get("hidden") is True and shown["content"] == ""
