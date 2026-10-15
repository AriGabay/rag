"""Round 13 U3 (R1, R2; KTD2) against stored blocks and tables: the bounded context read of a quote value, and the
facet of a listed measurement read through its table's structure. Synthetic documents only; the rest of the facet
reading is database-free (`tests/unit/test_datum_facet.py`)."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from tests.factories import make_document, make_office
from tests.integration.test_chat_calculation import quote, run, workspace

pytestmark = pytest.mark.db

SECTION_A, SECTION_B = "3. היוון התזרימים", "4. סיכום והערות"
LEAD_IN = "תזרים הכנסות משטחי פרסום על הגג:"
BLOCKS = [  # (kind, section, text, table_index)
    ("heading", SECTION_A, SECTION_A, None),
    ("paragraph", SECTION_A, "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", None),
    ("paragraph", SECTION_A, LEAD_IN, None),
    ("paragraph", SECTION_A, "שיעור היוון 9%.", None),
    ("heading", SECTION_B, SECTION_B, None),
    ("paragraph", SECTION_B, "השמאי הביא בחשבון את מכלול הנתונים שפורטו.", None),
    ("paragraph", SECTION_B, "שיעור היוון 10%.", None),
    ("table", SECTION_B, "טבלת חישוב שווי הקרקע", 0),
]
TABLE = {"headers": ["רכיב", "ערך"], "caption": "טבלת חישוב שווי הקרקע", "title": [], "notes": [],
         "section": SECTION_B, "block_index": 7,
         "rows": [{"cells": ["שטח קרקע", "620"]}, {"cells": ["מקדם הפחתה בשל שיתוף במקרקעין", "0.9"]}]}


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, "שומה סינתטית — רחוב הכלנית 8", sha="d" * 64)
    with tenant_tx(a.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, section, body, table) in enumerate(BLOCKS):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": section, "sp": [section], "t": body, "ti": table})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(TABLE, ensure_ascii=False)})
        for statement_key, metric, kind, value, value_text, quote_text, table, row in (
                ("t0:r1:מקדם", "מקדם הפחתה", "coefficient", 0.9, "0.9", "מקדם הפחתה בשל שיתוף במקרקעין | 0.9", 0, 1),
                ("b1:x", "שיעור היוון", "rate", 6.5, "6.5%", BLOCKS[1][2], None, None)):
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
                " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
                " value_role, quote, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :bi,"
                " :ti, :ri, :sk, :m, :mk, :val, 'exact', :vt, 'ratio', 'none', 'unknown', 'general', 'other', :q, :e,"
                " 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": doc, "v": ver, "bi": 7 if table is not None else 1, "ti": table, "ri": row, "sk": statement_key,
                 "m": metric, "mk": kind, "val": value, "vt": value_text, "q": quote_text, "e": EXTRACTION_VERSION})
    a.doc = str(doc)
    return a


def _source_of(ws, section: str) -> str:
    handle = re.search(r"(§\d+) «" + re.escape(section) + "»", T.tool_outline(ws, ws_doc(ws))).group(1)
    return re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": handle})).group(1)


def ws_doc(ws) -> str:
    return ws.doc


def _meaning() -> dict:
    return {"kind": "rate", "unit": "percent", "period": "none", "vat": "unknown", "area_basis": "",
            "subject": "הנכס", "role": "other"}


def _take(office, section: str, sentence: str, number: str):
    ws = workspace(office)
    ws.doc = office.doc
    s = _source_of(ws, section)
    out = run(ws, "take_value", source=s, locator=quote(sentence.rstrip("."), number), meaning=_meaning(),
              label="שיעור")
    assert out.startswith("V1 נרשם"), out
    return ws, out


def test_a_quote_value_with_no_component_in_its_sentence_reads_the_lead_in_above_it(office):
    ws, out = _take(office, SECTION_A, BLOCKS[3][2], "9")
    assert ws.values["V1"].facet == {"status": "stated", "written": "תזרים הכנסות משטחי פרסום על הגג",
                                     "part": "lead_in", "tier": "context"}
    assert "תזרים הכנסות משטחי פרסום על הגג" in out  # the model is told


def test_a_quote_value_whose_sentence_names_the_cash_flow_has_an_own_facet(office):
    ws, _ = _take(office, SECTION_A, BLOCKS[1][2], "6.5")
    assert ws.values["V1"].facet == {"status": "stated", "written": "תזרים דמי שכירות ממשרדים", "part": "sentence",
                                     "tier": "own"}


def test_a_heading_that_names_no_cash_flow_leaves_the_value_with_status_none(office):
    ws, out = _take(office, SECTION_B, BLOCKS[6][2], "10")
    assert ws.values["V1"].facet["status"] == "none"
    assert "רכיב: לא צוין במקור" in out


def test_listed_measurements_carry_their_facets_in_the_listing_and_in_public(office):
    ws = workspace(office)
    out = T.tool_find_measurements(ws, "מקדם הפחתה")
    table_row = next(m for m in ws.measurements.values() if m.row.metric_kind == "coefficient")
    assert table_row.public()["facet"] == {"status": "stated", "written": "מקדם הפחתה בשל שיתוף במקרקעין",
                                           "part": "row", "tier": "own"}
    assert "מקדם הפחתה בשל שיתוף במקרקעין" in out.split("ציטוט")[0]
    text_ws = workspace(office)
    T.tool_find_measurements(text_ws, "שיעור היוון")
    text_row = next(m for m in text_ws.measurements.values() if m.row.metric_kind == "rate")
    assert text_row.public()["facet"]["written"] == "תזרים דמי שכירות ממשרדים"
