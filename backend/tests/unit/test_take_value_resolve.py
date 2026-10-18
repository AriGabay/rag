"""Round 15 U1 (R12; KTD9, KTD11): ``take_value`` registers what the model can see in the source.

A table row quoted as written, a row or column label that differs from the table's only in spaces, punctuation or a group
column, and a cell whose unit its own header states are registered. A real mismatch, or a label found in several rows, is
refused with where it is found. A cell whose unit the source leaves ambiguous registers with a flag that blocks
calculation with it. A date is cited, never registered. Database-free; the tables are synthetic."""

from __future__ import annotations

import contextlib
import re
import uuid
from types import SimpleNamespace

import pytest

from app.chat import reader
from app.chat import tools as T

VERSION = reader.Version(uuid.uuid4(), uuid.uuid4(), "שומה סינתטית לבדיקה", "reading-1", "application/pdf", True, 3,
                         False, "k", "f.pdf")


def _table(headers, rows, caption="", notes=()) -> dict:
    return {"headers": list(headers), "caption": caption, "title": [], "notes": list(notes),
            "rows": [{"cells": list(r), "page": 1} for r in rows]}


def _workspace(monkeypatch, table: dict) -> SimpleNamespace:
    """One synthetic table read as one source (S1), the way ``read`` returns it."""
    block = SimpleNamespace(block_index=0, text=table["caption"] or "טבלה", page=1, status="read", section="נתונים",
                            section_path=["נתונים"], bbox=None, media=None, table_index=0, method=None, note="",
                            kind="table")
    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext(None))
    monkeypatch.setattr(T.reader, "version", lambda conn, vid: VERSION)
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T.reader, "blocks_between", lambda conn, vid, a, b, limit=None: [block])
    monkeypatch.setattr(T.reader, "table_structure", lambda conn, vid, ti: table)
    monkeypatch.setattr(T.reader, "table_block", lambda conn, vid, ti: block)
    ws = T.Workspace(ctx=None)
    src = ws.add_source(document_id=VERSION.document_id, version_id=VERSION.version_id, title=VERSION.title,
                        section="נתונים", location="עמוד 1", kind="table", text=T._table_text(table, table["rows"]),
                        block_start=0, block_end=0, reading_id="reading-1", status="complete", table_index=0)
    return SimpleNamespace(ws=ws, sid=src.sid)


def _meaning(**kw) -> dict:
    base = {"kind": "coefficient", "unit": "ratio", "period": "unknown", "vat": "unknown", "area_basis": "",
            "subject": "הנכס", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""}
    return base | kw


def _loc(**kw) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": None,
            "number": None} | kw


def _take(d, locator: dict, **meaning_) -> str:
    return T.tool_take_value(d.ws, d.sid, locator, _meaning(**meaning_), "ערך")


# --- a quoted table row ----------------------------------------------------------------------------------------------

COEFFS = _table(["רכיב", "מקדם"], [["מקדם התאמה לשטח", "0.9"], ["מקדם התאמה למיקום", "1.1"]])


def test_a_quoted_row_registers_the_cell_the_number_is_in(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    _take(d, _loc(quote="מקדם התאמה לשטח | 0.9", number="0.9"))
    v = d.ws.values["V1"]
    assert str(v.value) == "0.9"
    assert (v.locator["row_number"], v.locator["column_number"]) == (1, 2)
    assert v.locator["column"] == "מקדם"


def test_a_quoted_part_of_a_row_registers_the_same_cell_as_the_cell_locator(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    _take(d, _loc(quote="התאמה למיקום | 1.1", number="1.1"))
    out = _take(d, _loc(row="מקדם התאמה למיקום", column="מקדם"))
    assert list(d.ws.values) == ["V1"]  # the same span is the same datum (round 14)
    assert out.startswith("V1")


def test_a_quoted_row_that_is_in_no_row_is_refused_with_the_rows(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(quote="מקדם אחר | 0.9", number="0.9"))
    assert "מקדם התאמה לשטח" in str(e.value) and not d.ws.values


def test_a_quoted_row_found_in_two_rows_names_both(monkeypatch):
    table = _table(["נכס", "קומה", "שטח (מ\"ר)"], [["דירה", "ראשונה", "90"], ["דירה", "ראשונה", "90"]])
    d = _workspace(monkeypatch, table)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(quote="דירה | ראשונה | 90", number="90"), kind="area", unit="sqm")
    msg = str(e.value)
    assert re.search(r"\b1\b", msg) and re.search(r"\b2\b", msg) and not d.ws.values


# --- labels that differ only in spaces, punctuation or a group column -------------------------------------------------

def _row(table: dict, row: str, column: str, **loc) -> dict:
    full = T._table_text(table, table["rows"])
    src = T.Source(sid="S1", document_id=VERSION.document_id, version_id=VERSION.version_id, title="t", section=None,
                   location="", kind="table", text=full, block_start=0, block_end=0, reading_id="r", status="complete")
    return T._cell_of(src, full, {"row": row, "column": column} | loc, table, 0)


def test_a_row_label_with_a_different_mark_and_spacing_is_the_same_row():
    table = _table(["סעיף", "ערך"], [["ממוצע (*)", "12"], ["חציון", "10"]])
    assert _row(table, "ממוצע ( *)", "ערך")["locator"]["row_number"] == 1
    assert _row(table, "ממוצע(*)", "ערך")["locator"]["row_number"] == 1
    assert _row(table, "ממוצע", "ערך")["locator"]["row_number"] == 1


def test_a_column_header_with_a_different_spacing_is_the_same_column():
    table = _table(["נכס", "שכר דירה  חודשי (₪)"], [["דירה", "5,000"]])
    assert _row(table, "דירה", "שכר דירה חודשי(₪)")["locator"]["column_number"] == 2


def test_a_group_first_column_does_not_hide_the_row_label():
    table = _table(["בניין", "קומה", "שכירות (₪)"], [["בניין א", "קומת קרקע", "1,200"], ["", "קומה ראשונה", "1,100"],
                                                     ["בניין ב", "קומת קרקע", "900"]])
    assert _row(table, "קומה ראשונה", "שכירות (₪)")["written"] == "1,100"
    with pytest.raises(T.ToolError) as e:  # the label is in two rows: refused, and both are named
        _row(table, "קומת קרקע", "שכירות (₪)")
    assert re.search(r"\b1\b", str(e.value)) and re.search(r"\b3\b", str(e.value))


def test_a_row_number_that_disagrees_with_the_label_resolves_by_the_unique_label():
    table = _table(["סעיף", "ערך"], [["א", "1"], ["ב", "2"], ["ג", "3"], ["ד", "4"], ["ה", "5"]])
    taken = _row(table, "ה", "ערך", row_number=4)
    assert taken["locator"]["row_number"] == 5 and taken["written"] == "5"


def test_a_row_number_that_disagrees_with_a_label_found_in_two_rows_is_refused_naming_both():
    table = _table(["סעיף", "ערך"], [["א", "1"], ["ב", "2"], ["ב", "3"], ["ד", "4"]])
    with pytest.raises(T.ToolError) as e:
        _row(table, "ב", "ערך", row_number=4)
    assert re.search(r"\b2\b", str(e.value)) and re.search(r"\b3\b", str(e.value))


def test_a_row_number_that_agrees_with_the_label_is_kept():
    table = _table(["סעיף", "ערך"], [["ב", "2"], ["ב", "3"]])
    assert _row(table, "ב", "ערך", row_number=2)["locator"]["row_number"] == 2


# --- the unit a cell's own header states ------------------------------------------------------------------------------

def test_a_header_that_states_a_unit_per_area_gives_the_cell_that_unit(monkeypatch):
    table = _table(["נכס", "שווי למ\"ר בנוי"], [["דירה 3 חדרים", "21,000"]], notes=["נתונים סינתטיים"])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="דירה 3 חדרים", column="שווי למ\"ר בנוי"), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.unit == "ILS_per_sqm" and v.provenance["unit"] == "source" and v.meaning_from["unit"] == "header"
    assert "V1" not in d.ws.ambiguous_units


def test_the_header_unit_wins_over_a_unit_in_the_row_label(monkeypatch):
    table = _table(["נכס", "שכירות (₪)"], [["משרד 120 מ\"ר", "9,000"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="משרד 120 מ\"ר", column="שכירות (₪)"), kind="rent", unit="ILS")
    v = d.ws.values["V1"]
    assert v.unit == "ILS" and v.provenance["unit"] == "source" and "V1" not in d.ws.ambiguous_units


def test_a_unit_the_cell_states_itself_still_joins_the_header_unit(monkeypatch):
    table = _table(["נכס", "שווי (₪)"], [["דירה", "9,500 ₪ למ\"ר"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="דירה", column="שווי (₪)", number="9,500"), kind="value", unit="ILS_per_sqm")
    assert d.ws.values["V1"].unit == "ILS_per_sqm" and d.ws.values["V1"].provenance["unit"] == "source"


# --- an ambiguous unit registers with a flag that blocks calculation -----------------------------------------------------

AMBIGUOUS = _table(["נכס", "ערך"], [["דירה 90 מ\"ר", "21,000"], ["דירה 80 מ\"ר", "19,000"]], caption="נתוני עסקאות (₪)")


def test_a_unit_the_source_leaves_ambiguous_registers_with_the_flag(monkeypatch):
    d = _workspace(monkeypatch, AMBIGUOUS)
    out = _take(d, _loc(row="דירה 90 מ\"ר", column="ערך"), kind="value", unit="ILS")
    assert d.ws.values["V1"].unit == "ILS" and "V1" in d.ws.ambiguous_units
    assert T.MSG_UNIT_AMBIGUOUS_TAKEN.split("{")[0] in out  # the model is told the value cannot be calculated with


def test_calculate_refuses_an_input_whose_unit_is_ambiguous_and_names_it(monkeypatch):
    d = _workspace(monkeypatch, AMBIGUOUS)
    _take(d, _loc(row="דירה 90 מ\"ר", column="ערך"), kind="value", unit="ILS")
    _take(d, _loc(row="דירה 80 מ\"ר", column="ערך"), kind="value", unit="ILS")
    with pytest.raises(T.ToolError) as e:
        T.tool_calculate(d.ws, "V1 - V2", "הפרש")
    assert "V1" in str(e.value) and "V2" in str(e.value)
    assert not d.ws.computations


def test_a_unit_the_row_label_states_for_an_area_is_not_ambiguous(monkeypatch):
    table = _table(["סעיף", "ערך"], [["שטח בנוי (מ\"ר)", "96"]], caption="נתוני נכס (₪)")
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="שטח בנוי (מ\"ר)", column="ערך"), kind="area", unit="sqm")
    v = d.ws.values["V1"]
    assert v.unit == "sqm" and v.provenance["unit"] == "source" and "V1" not in d.ws.ambiguous_units


def test_a_clear_value_calculates(monkeypatch):
    table = _table(["נכס", "שכירות (₪)"], [["א", "5,000"], ["ב", "4,000"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="א", column="שכירות (₪)"), kind="rent", unit="ILS", period="month")
    _take(d, _loc(row="ב", column="שכירות (₪)"), kind="rent", unit="ILS", period="month")
    out = T.tool_calculate(d.ws, "V1 - V2", "הפרש", None)
    assert '"id": "C1"' in out


# --- a date is cited, a year is a number ---------------------------------------------------------------------------------

def test_a_date_quote_gets_the_cite_it_message_and_registers_nothing(monkeypatch):
    block = SimpleNamespace(block_index=0, text="המועד הקובע הוא 15.03.2024.", page=1, status="read", section="א",
                            section_path=["א"], bbox=None, media=None, table_index=None, method=None, note="")
    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext(None))
    monkeypatch.setattr(T.reader, "version", lambda conn, vid: VERSION)
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T.reader, "blocks_between", lambda conn, vid, a, b, limit=None: [block])
    ws = T.Workspace(ctx=None)
    src = ws.add_source(document_id=VERSION.document_id, version_id=VERSION.version_id, title=VERSION.title, section="א",
                        location="עמוד 1", kind="context", text=block.text, block_start=0, block_end=0,
                        reading_id="reading-1", status="complete")
    with pytest.raises(T.ToolError) as e:
        T.tool_take_value(ws, src.sid, _loc(quote=block.text, number="15.03.2024"), _meaning(kind="count", unit="units"), "")
    assert T.MSG_DATE_CITE in str(e.value) and "אינו מספר אחד" not in str(e.value)
    assert not ws.values


def test_a_date_in_a_cell_gets_the_cite_it_message():
    table = _table(["נכס", "תאריך עסקה"], [["דירה", "15.03.2024"]])
    with pytest.raises(T.ToolError) as e:
        _row(table, "דירה", "תאריך עסקה", number="15.03.2024")
    assert T.MSG_DATE_CITE in str(e.value)
    with pytest.raises(T.ToolError) as e:
        _row(table, "דירה", "תאריך עסקה")
    assert T.MSG_DATE_CITE in str(e.value)


def test_a_year_alone_in_a_year_built_cell_is_a_number():
    table = _table(["נכס", "שנת בנייה"], [["דירה", "1998"]])
    taken = _row(table, "דירה", "שנת בנייה", number="1998")
    assert str(taken["value"]) == "1998" and taken["written"] == "1998"
    assert str(_row(table, "דירה", "שנת בנייה")["value"]) == "1998"



# --- a whole rendered row as `row`, and a multi-level column header (round 15 U1 follow-up, KTD9) -------------------------

ROWS = _table(["נושא", "תאריך", "תיאור", "שווי (₪)"],
              [["פרויקט אלפא", "10/01/2020", "תכנון ובנייה בע\"מ", "12,000,000"],
               ["פרויקט בטא", "20/05/2021", "ייזום", "7,500,000"],
               ["פרויקט גמא", "11/11/2021", "שיווק", "3,250,000"],
               ["פרויקט דלתא", "30/09/2021", "כלכלה והנדסה בע\"מ *", "53,000,000", ""]])
RENDERED = "פרויקט דלתא | 30/09/2021 | כלכלה ו הנדסה בע\"מ | 53,000,000 ₪"


def test_a_whole_rendered_row_with_ocr_spacing_resolves_the_row_the_number_names():
    taken = _row(ROWS, RENDERED, "שווי (₪)", row_number=4)
    assert taken["locator"]["row_number"] == 4 and taken["written"] == "53,000,000"


def test_a_whole_rendered_row_resolves_by_its_cells_when_the_row_number_is_wrong():
    taken = _row(ROWS, RENDERED, "שווי (₪)", row_number=2)
    assert taken["locator"]["row_number"] == 4 and taken["written"] == "53,000,000"
    assert _row(ROWS, RENDERED, "שווי (₪)")["locator"]["row_number"] == 4


def test_a_whole_rendered_row_that_matches_no_row_is_refused_naming_the_rows():
    with pytest.raises(T.ToolError) as e:
        _row(ROWS, "פרויקט אחר | 01/01/2019 | משהו אחר | 99", "שווי (₪)", row_number=4)
    msg = str(e.value)
    assert "פרויקט דלתא" in msg and re.search(r"\b4\b", msg)


def test_a_whole_rendered_row_found_in_two_rows_needs_the_row_number():
    table = _table(["נושא", "שווי (₪)"], [["פרויקט", "5"], ["אחר", "6"], ["פרויקט", "5"]])
    with pytest.raises(T.ToolError) as e:
        _row(table, "פרויקט | 5", "שווי (₪)")
    assert re.search(r"\b1\b", str(e.value)) and re.search(r"\b3\b", str(e.value))
    assert _row(table, "פרויקט | 5", "שווי (₪)", row_number=3)["locator"]["row_number"] == 3


def test_a_multi_level_column_header_is_matched_loosely_against_the_headers():
    table = _table(["נושא", "קבוצה א 101", "קבוצה א 102"], [["דירה", "4", "5"]])
    assert _row(table, "דירה", "קבוצה א | 101")["written"] == "4"
    assert _row(table, "דירה", "קבוצה א | 102")["written"] == "5"


def test_a_multi_level_column_header_falls_back_to_its_leaf_header():
    table = _table(["נושא", "101", "102"], [["דירה", "4", "5"]])
    assert _row(table, "דירה", "קבוצה א | 102")["written"] == "5"


# --- a row label that states a money-per-area quantity gives the currency-only cell that unit -----------------------------

PER_AREA_ROWS = _table(["סעיף", "ערך"], [["שווי מ\"ר מבונה", "11,900 ₪"], ["מחיר למ\"ר", "12,400 ₪"],
                                         ["שווי דירה 90 מ\"ר", "1,071,000 ₪"], ["שווי למ\"ר", "13,000 ₪ למ\"ר"]])


def test_a_row_label_that_names_money_per_area_gives_a_currency_only_cell_that_unit(monkeypatch):
    d = _workspace(monkeypatch, PER_AREA_ROWS)
    _take(d, _loc(row="שווי מ\"ר מבונה", column="ערך"), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.unit == "ILS_per_sqm" and v.provenance["unit"] == "source" and v.meaning_from["unit"] == "row"
    assert "V1" not in d.ws.ambiguous_units
    _take(d, _loc(row="מחיר למ\"ר", column="ערך"), kind="price", unit="ILS_per_sqm")
    assert d.ws.values["V2"].unit == "ILS_per_sqm"


def test_a_row_label_with_a_property_area_stays_a_currency_amount(monkeypatch):
    d = _workspace(monkeypatch, PER_AREA_ROWS)
    _take(d, _loc(row="שווי דירה 90 מ\"ר", column="ערך"), kind="value", unit="ILS")
    assert d.ws.values["V1"].unit == "ILS"
    with pytest.raises(T.ToolError):
        _take(d, _loc(row="שווי דירה 90 מ\"ר", column="ערך"), kind="value", unit="ILS_per_sqm")


def test_a_cell_that_states_its_own_per_area_unit_is_unchanged(monkeypatch):
    d = _workspace(monkeypatch, PER_AREA_ROWS)
    _take(d, _loc(row="שווי למ\"ר", column="ערך"), kind="value", unit="ILS_per_sqm")
    assert d.ws.values["V1"].unit == "ILS_per_sqm" and d.ws.values["V1"].meaning_from["unit"] == "cell"


def test_a_header_that_states_another_unit_keeps_its_unit_over_the_row_label(monkeypatch):
    table = _table(["סעיף", "שטח (מ\"ר)"], [["שווי מ\"ר מבונה", "120 ₪"]])
    d = _workspace(monkeypatch, table)
    with pytest.raises(T.ToolError):
        _take(d, _loc(row="שווי מ\"ר מבונה", column="שטח (מ\"ר)"), kind="value", unit="ILS_per_sqm")
