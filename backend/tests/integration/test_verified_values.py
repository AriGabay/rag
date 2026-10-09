"""Verified values cached per reading (U13, KTD12, R29) and OCR languages in reading-cache keys (KTD11, R30).

A value ``take_value`` verified with its own region read clearly is cached per version, reading and locator with
what its source attests only; a later turn finds it in ``find_measurements`` as ``Q#`` and takes it without reading
again, with its own meaning checked against the source; a new reading, or a document the user can no longer see,
never serves it. The reading caches hit for the OCR languages their rows were made with — the rows rewritten by
migration 0016 included — and miss for others. Synthetic documents only; the project and streets are invented."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction import regions
from app.extraction.images import PictureReading
from app.extraction.vision import INSPECT_READER_VERSION
from app.platform.pipeline import ImageReadingStore
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat_calculation import (
    INCOME,
    SECTION,
    add_document,
    cell,
    handle_of,
    meaning,
    quote,
    read_table,
    run,
    workspace,
)

pytestmark = pytest.mark.db

PROSE_QUOTE = "השטח ברוטו 135 מ\"ר"


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.other = make_user(a, "other@example.test", [a.default_group_id, a.private])
    a.doc, a.ver = add_document(a, a.private)
    return a


def _rows(office) -> list:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT reading_id, locator, value FROM verified_values ORDER BY created_at")).all()


def _take_total_income(ws, office) -> str:
    s = read_table(ws, office.doc)
    return run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME), meaning=meaning("income", role="total"),
               label="הכנסות")


def _q(out: str) -> str:
    m = re.search(r"(Q\d+)", out)
    assert m, out
    return m.group(1)


def _no_fresh_reading(monkeypatch) -> None:
    def refuse(*a, **kw):
        raise AssertionError("a cached value is taken without locating it again")

    monkeypatch.setattr(T, "_take_cell", refuse)
    monkeypatch.setattr(T, "_take_quote", refuse)


def test_a_value_taken_on_demand_is_cached_and_reused_in_a_later_turn_without_a_new_read(office, monkeypatch):
    first = workspace(office, office.emp)
    assert _take_total_income(first, office).startswith("V1 נרשם")
    assert json.loads(T.tool_calculate(first, "V1 * 12", "הכנסות שנתיות"))["value"] == "149400000"
    (row,) = _rows(office)
    assert row.reading_id == "reading-1" and row.locator["row"] == "סה\"כ" and row.locator["column"] == INCOME
    record = row.value["record"]
    assert record["value"] == "12450000" and record["vat"] == "excluded"  # the table's note: the source's
    assert not {"label", "subject", "role"} & set(record)

    later = workspace(office, office.emp)
    _no_fresh_reading(monkeypatch)
    listed = T.tool_find_measurements(later, "הכנסות")
    q = _q(listed)
    assert "12,450,000" in listed and "Q1" in listed
    out = run(later, "take_value", source=q, locator=cell(None, None),
              meaning=meaning("income", role="total"), label="הכנסות")
    assert out.startswith("V1 נרשם") and "ערך מאומת שמור" in out
    v = later.values["V1"]
    assert v.value == Decimal("12450000") and v.vat == "excluded" and v.reading_id == "reading-1"
    assert later.reads == {} and len(later.sources) == 1  # nothing read: the one source is the cached value's
    assert later.anchors["V1"]["kind"] == "cell" and later.anchors["V1"]["reading_id"] == "reading-1"
    assert json.loads(T.tool_calculate(later, "V1 * 12", "הכנסות שנתיות"))["value"] == "149400000"


def test_a_value_of_an_earlier_reading_is_not_listed_or_reused(office):
    first = workspace(office, office.emp)
    _take_total_income(first, office)
    stale = workspace(office, office.emp)
    q = _q(T.tool_find_measurements(stale, "הכנסות"))
    with tenant_tx(office.system) as conn:  # the document is read again: a new reading id
        conn.execute(text("UPDATE document_versions SET ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": office.ver, "i": json.dumps({"reading_id": "reading-2"})})
    out = run(stale, "take_value", source=q, locator=cell(None, None),
              meaning=meaning("income", role="total"), label="הכנסות")
    assert out.startswith("שגיאה") and "עובד מחדש" in out and not stale.values
    fresh = workspace(office, office.emp)
    assert "Q1" not in T.tool_find_measurements(fresh, "הכנסות")
    assert _take_total_income(fresh, office).startswith("V1 נרשם")  # verified again from the new reading
    assert sorted(r.reading_id for r in _rows(office)) == ["reading-1", "reading-2"]


def test_a_cached_value_of_a_document_no_longer_visible_is_not_listed_or_used(office):
    _take_total_income(workspace(office, office.emp), office)
    ws = workspace(office, office.other)
    q = _q(T.tool_find_measurements(ws, "הכנסות"))
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u AND group_id = :g"),
                     {"u": office.other, "g": office.private})
    out = run(ws, "take_value", source=q, locator=cell(None, None),
              meaning=meaning("income", role="total"), label="הכנסות")
    assert out.startswith("שגיאה") and "אינו זמין" in out and not ws.values
    listed = T.tool_find_measurements(workspace(office, office.other), "הכנסות")
    assert "Q1" not in listed and "12,450,000" not in listed


def test_asserted_meaning_of_one_turn_is_not_reused_by_another_users_turn(office):
    ws = workspace(office, office.emp)
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": sec})).group(1)
    asserted = meaning("area", "sqm", period="month") | {"stance": "claim", "stated_by": "המשיבה", "scenario": ""}
    out = run(ws, "take_value", source=s, locator=quote(PROSE_QUOTE, "135"), meaning=asserted, label="שטח ברוטו")
    assert out.startswith("V1 נרשם")
    first = ws.values["V1"]
    assert first.provenance["period"] == "model_asserted" and first.provenance["stance"] == "model_asserted"
    (row,) = [r for r in _rows(office) if "quote" in r.locator]
    assert not {"period", "stance", "stated_by", "subject", "role"} & set(row.value["record"])

    other = workspace(office, office.other)
    q = _q(T.tool_find_measurements(other, "ברוטו"))
    out = run(other, "take_value", source=q, locator=quote(None, None),
              meaning=meaning("area", "sqm") | {"stance": "unknown", "stated_by": "", "scenario": ""}, label="שטח")
    v = other.values["V1"]
    assert out.startswith("V1 נרשם") and v.period == "none" and v.stance == "unknown" and not v.stated_by
    assert "model_asserted" not in v.provenance.values() and v.certainty == "verified"


def test_a_cached_value_is_not_reused_for_another_locator(office):
    _take_total_income(workspace(office, office.emp), office)
    ws = workspace(office, office.emp)
    q = _q(T.tool_find_measurements(ws, "הכנסות"))
    out = run(ws, "take_value", source=q, locator=cell("שלב א", INCOME), meaning=meaning("income", role="component"),
              label="הכנסות שלב א")
    assert out.startswith("שגיאה") and q in out and not ws.values


# --- OCR languages in reading-cache keys (KTD11) ------------------------------------------------------------------

def test_picture_readings_hit_for_their_ocr_languages_including_rows_rewritten_by_the_migration(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    with tenant_tx(a.system) as conn:  # a row as migration 0016 left it: the old key with the default languages
        conn.execute(text(
            "INSERT INTO image_readings (office_id, content_hash, reader_version, model_config, crop_scale, status,"
            " reading) VALUES (app_office(), 'logo-hash', :r, 'none|ocr=heb+eng', '1.0', 'read', CAST(:g AS jsonb))"),
            {"r": regions.REGION_READER_VERSION, "g": PictureReading("read", "ocr", text="לוגו לדוגמה").to_json()})
    store = ImageReadingStore(a.system)

    def key(languages: str) -> regions.ReadingKey:
        return regions.ReadingKey("logo-hash", regions.REGION_READER_VERSION, regions.model_config(None, languages),
                                  "1.0")

    assert store.get(key("heb+eng")).text == "לוגו לדוגמה"
    assert store.get(key("eng")) is None


def test_inspections_hit_for_their_ocr_languages_including_rows_rewritten_by_the_migration(db, monkeypatch):
    from app.config import get_settings

    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, "שומה סינתטית")
    vision = SimpleNamespace(config="scripted:low")
    with tenant_tx(a.system) as conn:
        conn.execute(text(
            "INSERT INTO region_readings (office_id, document_id, version_id, reading_id, region, reader_version,"
            " model_config, page, status, reading) VALUES (app_office(), :d, :v, 'reading-1', 'block:3', :rv,"
            " 'scripted:low|ocr=heb+eng', 2, 'read', CAST(:g AS jsonb))"),
            {"d": doc, "v": ver, "rv": INSPECT_READER_VERSION,
             "g": PictureReading("read", "vision", text="דמי שכירות 41,300").to_json()})
    spot = T._Spot(SimpleNamespace(version_id=ver, reading_id="reading-1"), "block:3", 2, None, "האזור")
    with tenant_tx(a.ctx()) as conn:
        assert T._cached(conn, spot, T.inspect_config(vision)).text == "דמי שכירות 41,300"
    monkeypatch.setattr(get_settings(), "ocr_languages", "eng")
    with tenant_tx(a.ctx()) as conn:
        assert T._cached(conn, spot, T.inspect_config(vision)) is None
