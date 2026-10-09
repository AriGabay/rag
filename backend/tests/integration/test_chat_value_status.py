"""Statuses without approval and bounded re-reads in the chat tools (U12, R25, R28).

A stored measurement is labelled as a person's decision only when a review recorded one, and a calculation states
its inputs' statuses without implying a review is needed; an uncertain input makes the result conditional. A value
taken from a region ingestion read uncertainly gets a focused visual re-read through the inspect path, at most twice
per value in a turn, and stays uncertain when the re-read is unclear. A search hit in a partly read document is
marked partial only when its own page has an unread region. The vision model is scripted; synthetic documents only."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat_inspect import ScriptedVision, emp, ingest_r1, region_of, tag

pytestmark = pytest.mark.db

TITLE = "שומה סינתטית לבדיקת סטטוסים"
RENT = "דמי שכירות (₪)"


# --- measurement statuses ----------------------------------------------------------------------------------------------

def _measure(conn, doc, ver, value: int, row: int, status: str, reviewer=None, lost: bool = False) -> None:
    conn.execute(text(
        "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
        " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
        " value_role, quote, extraction_version, model, status, issues, reviewed_by, anchor_lost) VALUES"
        " (app_office(), :d, :v, :b, 0, :r, :k, 'דמי שכירות למ\"ר', 'rent_per_area', :val, 'exact', :vt,"
        " 'ILS_per_sqm', 'month', 'unknown', 'asking', 'asking_price', :q, :e, 'test', :st, '[]'::jsonb, :u,"
        " CAST(:lost AS jsonb))"),
        {"d": doc, "v": ver, "b": row, "r": row, "k": f"t0:r{row}:{value}", "val": value, "vt": f"{value} ₪",
         "q": f"שורה {row}: {value} ₪", "e": EXTRACTION_VERSION, "st": status, "u": reviewer,
         "lost": json.dumps({"reason": "not found"}) if lost else None})


@pytest.fixture
def measured(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, TITLE)
    with tenant_tx(a.system) as conn:
        _measure(conn, doc, ver, 95, 1, "verified", reviewer=a.admin_id)
        _measure(conn, doc, ver, 97, 2, "auto_validated")
        _measure(conn, doc, ver, 99, 3, "needs_review")
        _measure(conn, doc, ver, 101, 4, "corrected", reviewer=a.admin_id, lost=True)
    return a


def _mid(out: str, value: int) -> str:
    m = re.search(rf"(M\d+): [^\n]*= {value} ₪[^\n]*", out)
    assert m, out
    return m.group(1)


def _line(out: str, value: int) -> str:
    return re.search(rf"M\d+: [^\n]*= {value} ₪[^\n]*", out).group(0)


def test_find_measurements_labels_each_status_and_a_person_only_with_a_review(measured):
    ws = T.Workspace(ctx=measured.ctx())
    out = T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"])
    assert T.STATUS_HUMAN in _line(out, 95)
    assert T.STATUS_AUTO in _line(out, 97)
    assert T.STATUS_UNCERTAIN in _line(out, 99)
    assert T.STATUS_UNCERTAIN in _line(out, 101)  # a person's value whose place was lost on reprocessing
    assert "ממתין לבדיקה" not in out and "ראשוני" not in out


def test_a_calculation_states_statuses_and_an_uncertain_input_makes_it_conditional(measured):
    ws = T.Workspace(ctx=measured.ctx())
    out = T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"])
    a, b, c = _mid(out, 95), _mid(out, 97), _mid(out, 99)
    certain = json.loads(T.tool_calculate(ws, f"{a} + {b}", "סכום"))
    assert certain["conditional"] is False and "טרם אומתו" not in certain["note"]
    assert f"{a} {T.STATUS_HUMAN}" in certain["note"] and f"{b} {T.STATUS_AUTO}" in certain["note"]
    shaky = json.loads(T.tool_calculate(ws, f"{b} + {c}", "סכום"))
    assert shaky["conditional"] is True and c in shaky["note"] and "מותנה" in shaky["note"]
    assert ws.computations[shaky["id"]].public()["conditions"]


# --- a focused re-read of an unclear value -------------------------------------------------------------------------------

@pytest.fixture
def vision_office(db, monkeypatch):
    """An office whose inspect reader is scripted (as in ``test_chat_inspect``): an employee in the closed group."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.vision = ScriptedVision()
    monkeypatch.setattr(T, "inspect_reader", lambda ctx: a.vision)
    return a


def _uncertain_table(vision_office, monkeypatch, ws) -> str:
    """The synthetic raster table, read uncertainly at ingestion; the source its stored reading was returned as."""
    doc = ingest_r1(vision_office, monkeypatch, at_ingestion=ScriptedVision(uncertain=["תפוסה"]))
    region = region_of(ws, doc, marker="קריאה לא ודאית")
    out = T.tool_inspect(ws, {"region": region})
    assert vision_office.vision.calls == []  # the stored reading: no model call yet
    return tag(out, "id")


def _ocr_sees_the_table(monkeypatch) -> None:
    """OCR of the re-read crop is available and confidently sees the table's words: the independent check a
    focused visual re-read is confirmed by (the host has no Hebrew tessdata, so the words are given)."""
    from tests.unit.test_regions import HEADERS, ROWS

    words = [{"text": w, "conf": 95} for cell in [*HEADERS, *[c for r in ROWS for c in r]] for w in cell.split()]
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr("app.extraction.images._ocr_words", lambda gray, languages: words)


def _take(ws, sid: str) -> str:
    return T.tool_take_value(ws, sid, {"row": "צפון", "column": RENT},
                             {"kind": "rent", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "",
                              "subject": "אזור צפון", "role": "other"}, "דמי שכירות צפון")


def test_an_unclear_value_is_re_read_at_most_twice_and_then_reported_uncertain(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)
    _ocr_sees_the_table(monkeypatch)
    vision_office.vision.uncertain = ["דמי שכירות"]  # the re-read is unclear too
    outs = [_take(ws, sid) for _ in range(3)]
    # the first re-read calls the model; the second is served from the stored reading; the third is not made
    assert len(vision_office.vision.calls) == 1 and list(ws.rereads.values()) == [2]
    assert all(T.STATUS_UNCERTAIN in o for o in outs) and "כבר נקרא שוב 2 פעמים" in outs[2]
    assert set(ws.uncertain_values) == {"V1", "V2", "V3"}
    other = T.tool_take_value(ws, sid, {"row": "מרכז", "column": RENT},
                              {"kind": "rent", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "",
                               "subject": "אזור מרכז", "role": "other"}, "דמי שכירות מרכז")
    assert T.STATUS_UNCERTAIN in other
    result = json.loads(T.tool_calculate(ws, "V1 + V4", "סכום", "סכום דמי השכירות של שני האזורים, כפי שהתבקש"))
    assert result["conditional"] is True and "V1" in result["note"]


def test_a_clear_re_read_settles_the_value(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)
    _ocr_sees_the_table(monkeypatch)
    out = _take(ws, sid)
    assert len(vision_office.vision.calls) == 1 and T.STATUS_AUTO in out
    assert "V1" in ws.settled_values and "V1" not in ws.uncertain_values
    assert T.uncertain_inputs(ws, ["V1"]) == []


# --- partial labels -------------------------------------------------------------------------------------------------------

def test_a_hit_on_a_page_without_unread_regions_in_a_partly_read_document_is_not_partial(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, TITLE)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    texts = ["תיאור הנכס: דירת ארבעה חדרים בקומה שלישית ברחוב הדוגמה 5.",
             "נספח תשריט: מפת הסביבה של חלקת הדוגמה."]
    result = ExtractionResult(2, [PageResult(1, texts[0], "text_layer", 1.0, True),
                                  PageResult(2, texts[1], "text_layer", 1.0, True)], [],
                              [ChunkResult(0, "text", [1], None, texts[0]), ChunkResult(1, "text", [2], None, texts[1])])
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(a.system, info, 1e18)
    coverage = [{"page": 2, "ok": True, "method": None, "corrected": 0,
                 "regions": [{"block": 1, "kind": "image", "status": "unread", "reason": "תמונה ללא טקסט קריא",
                              "bbox": None, "section": None, "media": None}]}]
    with tenant_tx(a.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb)"
                          " || CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"partial": True, "coverage": coverage}, ensure_ascii=False)})
    ws = T.Workspace(ctx=a.ctx())
    T.tool_search(ws, "דירת ארבעה חדרים", [str(doc)])
    hit = next(s for s in ws.sources.values() if s.page_list == [1])
    assert hit.partial_document is False and "נקרא חלקית" not in T._render_source(hit)
    assert ws.activity[str(doc)]["partial"] is True  # a datum not found may still be in the unread part
    T.tool_search(ws, "נספח תשריט מפת הסביבה", [str(doc)])
    assert next(s for s in ws.sources.values() if s.page_list == [2]).partial_document is True


def test_without_ocr_an_unclear_value_is_not_re_read_and_stays_uncertain(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)  # ingested with OCR off, which stays off
    out = _take(ws, sid)
    assert vision_office.vision.calls == [] and T.STATUS_UNCERTAIN in out and "אין OCR" in out
    assert "V1" in ws.uncertain_values

