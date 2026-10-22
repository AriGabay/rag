"""Round 15 U1 (R12; KTD9, KTD11): ``take_value`` registers what the model can see in the source.

A table row quoted as written, a row or column label that differs from the table's only in spaces, punctuation or a group
column, and a cell whose unit its own header states are registered. A real mismatch, or a label found in several rows, is
refused with where it is found. A cell whose unit the source leaves ambiguous registers with a flag that blocks
calculation with it. A date is cited, never registered. Database-free; the tables are synthetic."""

from __future__ import annotations

import contextlib
import re
import uuid
from decimal import Decimal
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


# --- round 17 U6 (KTD6 item 6): cell fields and a quote together; refusals that name the corrected locator ---------------

def test_cell_fields_with_a_matching_quote_register_the_cell(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    _take(d, _loc(row="מקדם התאמה לשטח", column="מקדם", quote="מקדם התאמה לשטח | 0.9", number="0.9"))
    v = d.ws.values["V1"]
    assert str(v.value) == "0.9"
    assert (v.locator["row_number"], v.locator["column_number"]) == (1, 2)


def test_cell_fields_with_a_quote_of_part_of_the_row_register_the_cell(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    _take(d, _loc(row_number=2, column_number=2, quote="התאמה למיקום | 1.1"))
    assert str(d.ws.values["V1"].value) == "1.1"


def test_cell_fields_with_a_quote_of_another_row_are_refused_naming_the_cell_found(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(row="מקדם התאמה לשטח", column="מקדם", quote="מקדם התאמה למיקום | 1.1", number="1.1"))
    msg = str(e.value)
    assert "מקדם התאמה לשטח" in msg and "0.9" in msg and "מקדם התאמה למיקום" in msg  # the cell found, and the quote's row
    assert not d.ws.values


def test_a_locator_with_neither_cell_fields_nor_a_quote_names_the_corrected_locator(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(number="0.9"))
    msg = str(e.value)
    assert '"row_number": 1, "column_number": 2' in msg and '"quote"' in msg  # an example locator of the right shape
    assert not d.ws.values


def test_a_missing_column_names_the_tables_columns(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(row="מקדם התאמה לשטח"))
    msg = str(e.value)
    assert "רכיב" in msg and "מקדם" in msg and "column_number" in msg and not d.ws.values


def test_a_missing_row_names_the_tables_rows(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(column="מקדם"))
    msg = str(e.value)
    assert "מקדם התאמה לשטח" in msg and "מקדם התאמה למיקום" in msg and "row_number" in msg and not d.ws.values


def test_a_number_in_another_cell_is_refused_with_the_locator_of_that_cell(monkeypatch):
    d = _workspace(monkeypatch, COEFFS)
    with pytest.raises(T.ToolError) as e:
        _take(d, _loc(row="מקדם התאמה לשטח", column="מקדם", number="1.1"))
    msg = str(e.value)
    assert "row_number=2" in msg and "column_number=2" in msg and not d.ws.values


# --- a decimal comma is read by context where a value is taken from a source (round 19 U1, KTD3, R12, AE6) ------------------------------

def test_a_decimal_comma_is_a_decimal_and_a_thousands_comma_a_thousands_where_a_value_is_taken():
    from app.chat import tools as tools_

    assert tools_._parse_number("13,2") == ("13.2", Decimal("13.2"))
    assert tools_._parse_number("₪ 13,2") == ("13.2", Decimal("13.2"))
    assert tools_._parse_number("(3.5)")[1] == Decimal("-3.5")
    assert tools_._parse_number("3,596,696")[1] == Decimal("3596696")
    assert tools_._parse_number("1,250")[1] == Decimal("1250")
    found = tools_._numbers_of("שטח 13,2 מ\"ר ושווי 3,596,696 ₪")
    assert [(w, v) for w, _, _, v in found] == [("13,2", Decimal("13.2")), ("3,596,696", Decimal("3596696"))]
    assert all("שטח 13,2 מ\"ר ושווי 3,596,696 ₪"[s:e] == w for w, s, e, _ in found)  # found again where it is written


# --- round 21 U8: a table stored in reversed cell order is resolved in the order it is drawn, by the stored column (KTD6) -----

def _reversed_store():
    from tests.unit.test_tables import DRAWN, REVERSED

    return REVERSED, DRAWN


def test_a_cell_of_a_reversed_stored_table_is_taken_by_its_drawn_label_and_looked_up_by_its_stored_column():
    reversed_, _ = _reversed_store()
    taken = _row(reversed_, "Revenue", "Q1 2024")
    assert taken["written"] == "100" and taken["value"] == Decimal("100")
    # Q1 2024 is the second column as drawn and the third as stored: the datum and the highlighted box are the stored cell's
    assert taken["locator"] == {"table_index": 0, "row": "Revenue", "row_number": 2,
                                "column": "Q1 2024", "column_number": 3, "column_shown": 2}
    assert taken["anchor"]["column"] == 2 and taken["anchor"]["row"] == 1
    assert reversed_["rows"][1]["cell_boxes"][taken["anchor"]["column"]][0] == 140  # the box of «100»
    assert reversed_["rows"][1]["cells"][taken["anchor"]["column"]] == "100"


def test_the_column_number_the_agent_gives_counts_the_columns_as_drawn():
    reversed_, _ = _reversed_store()
    by_number = _row(reversed_, "Revenue", None, column_number=4)
    assert by_number["written"] == "300" and by_number["locator"]["column"] == "Annual"
    assert by_number["anchor"]["column"] == 0 and reversed_["rows"][1]["cells"][0] == "300"
    assert _row(reversed_, "Revenue", "Annual")["locator"] == by_number["locator"]


def test_a_negative_number_a_reversed_row_stored_with_its_minus_at_the_end_is_taken_with_its_sign():
    reversed_, _ = _reversed_store()
    taken = _row(reversed_, "Net", "Annual")
    assert taken["value"] == Decimal("-15")  # «15-» as stored, «-15» as the row is drawn
    assert reversed_["rows"][2]["cells"][taken["anchor"]["column"]] == "15-"  # as stored


def test_a_quoted_row_of_a_reversed_stored_table_is_resolved_in_the_order_it_is_drawn():
    reversed_, _ = _reversed_store()
    loc = T._row_quote_locator("S1", reversed_, {"quote": "Revenue | 100 | 200 | 300", "number": "200"})
    assert loc == {"row_number": 2, "column_number": 3, "number": "200"}  # drawn: Item, Q1, Q2, Annual
    full = T._table_text(reversed_, reversed_["rows"])
    src = T.Source(sid="S1", document_id=VERSION.document_id, version_id=VERSION.version_id, title="t", section=None,
                   location="", kind="table", text=full, block_start=0, block_end=0, reading_id="r", status="complete")
    taken = T._cell_of(src, full, loc, reversed_, 0)
    assert taken["written"] == "200" and taken["anchor"]["column"] == 1


def test_a_cached_value_of_a_reversed_stored_table_agrees_with_the_column_number_shown_to_the_agent():
    reversed_, drawn = _reversed_store()
    where = _row(reversed_, "Revenue", "Q1 2024")["locator"]
    assert T._locator_agrees({"locator": where}, {"column_number": 2, "column": "Q1 2024"})
    assert not T._locator_agrees({"locator": where}, {"column_number": 3})
    plain = _row(drawn, "Revenue", None, column_number=2)["locator"]
    assert "column_shown" not in plain and plain["column_number"] == 2
    assert T._locator_agrees({"locator": plain}, {"column_number": 2})


def test_the_anchor_of_a_cell_of_a_reversed_stored_table_highlights_the_stored_box_of_its_value():
    from app.chat import anchors as A
    from tests.unit.test_anchors import frac, page, reading, stub

    reversed_, _ = _reversed_store()
    anchor = _row(reversed_, "Revenue", "Q1 2024")["anchor"]
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, **anchor),
                      reading(tables={0: reversed_}, pages=[page(1)]), "r1")
    assert snap["precision"] == "cell"
    assert snap["pages"][0]["rects"] == [frac(reversed_["rows"][1]["cell_boxes"][2])]
    assert snap["pages"][0]["rects"] == [frac([140, 100, 180, 112])]


def test_a_table_served_as_stored_keeps_the_stored_column_and_no_shown_column():
    _, drawn = _reversed_store()
    taken = _row(drawn, "Revenue", None, column_number=4)
    assert taken["anchor"]["column"] == 3 and taken["locator"]["column_number"] == 4 and "column_shown" not in taken["locator"]
    assert taken["written"] == "300"


# --- round 22 U4: a quote of a table cell is that cell, with its table, row and stored column (KTD4, R7) -------------------------

def test_a_quote_of_a_served_cell_of_a_reversed_table_anchors_to_that_cell_with_its_column_identity(monkeypatch):
    reversed_, _ = _reversed_store()
    d = _workspace(monkeypatch, reversed_)
    _take(d, _loc(quote="Q1 2024: 100", number="100"))  # the quote as the served table shows the cell (no pipe)
    v = d.ws.values["V1"]
    assert v.locator["row_number"] == 2 and v.locator["column"] == "Q1 2024"
    assert v.locator["column_number"] == 3 and v.locator["column_shown"] == 2  # the stored column and the one shown
    anchor = d.ws.anchors["V1"]
    assert (anchor["table_index"], anchor["row"], anchor["column"]) == (0, 1, 2)  # the stored cell: its box is looked up by it
    assert reversed_["rows"][1]["cells"][anchor["column"]] == "100"


def test_each_served_cell_of_a_reversed_table_anchors_to_its_own_stored_column(monkeypatch):
    reversed_, _ = _reversed_store()
    for quote, number, column in (("Q2 2024: 200", "200", 1), ("Annual: 300", "300", 0), ("Q1 2024: 100", "100", 2)):
        d = _workspace(monkeypatch, reversed_)
        _take(d, _loc(quote=quote, number=number))
        assert (d.ws.anchors["V1"]["row"], d.ws.anchors["V1"]["column"]) == (1, column)
        assert reversed_["rows"][1]["cells"][column] == number


def test_a_quote_of_a_cell_of_a_table_served_as_stored_is_that_cell_too(monkeypatch):
    _, drawn = _reversed_store()
    d = _workspace(monkeypatch, drawn)
    _take(d, _loc(quote="200", number="200"))
    v = d.ws.values["V1"]
    assert (d.ws.anchors["V1"]["table_index"], d.ws.anchors["V1"]["row"], d.ws.anchors["V1"]["column"]) == (0, 1, 2)
    assert v.locator["row_number"] == 2 and v.locator["column_number"] == 3 and "column_shown" not in v.locator


def _source_of(table: dict, row_index: int | None = None) -> T.Source:
    full = T._table_text(table, table["rows"])
    return T.Source(sid="S1", document_id=VERSION.document_id, version_id=VERSION.version_id, title="t", section=None, location="",
                    kind="table", text=full, block_start=0, block_end=0, reading_id="r", status="complete", table_index=0,
                    row_index=row_index)


def test_a_quote_that_fits_several_cells_or_is_written_in_a_note_is_not_forced_into_a_cell():
    twice = _table(["Item", "Q1", "Q2"], [["Rent", "100", "100"], ["Fees", "20", "30"]], notes=["* amounts as of 2024"])
    src = _source_of(twice)
    assert T._quote_cell_locator(src, src.text, twice, {"quote": "100", "number": "100"}) is None  # two cells hold it
    src.text = "Item | Q1 | Q2\nItem: Rent | Q1: 100 | Q2: 100\nItem: Fees | Q1: 20 | Q2: 30"  # a read that shows the cells with their labels
    assert T._quote_cell_locator(src, src.text, twice, {"quote": "Q1: 100", "number": "100"}) == \
        {"row_number": 1, "column_number": 2, "number": "100"}
    assert T._quote_cell_locator(src, src.text, twice, {"quote": "Q9: 100", "number": "100"}) is None  # not in the source
    noted = _table(["Item", "Q1"], [["Rent", "2024"]], notes=["* amounts as of 2024"])
    src = _source_of(noted)
    src.text += "\nItem: Rent | Q1: 2024"
    assert T._quote_cell_locator(src, src.text, noted, {"quote": "2024", "number": "2024"}) is None  # the note writes it too
    assert T._quote_cell_locator(src, src.text, noted, {"quote": "amounts as of 2024", "number": "2024"}) is None
    assert T._quote_cell_locator(src, src.text, noted, {"quote": "Q1: 2024", "number": "2024"})["column_number"] == 2


def test_a_row_chunk_hit_limits_the_cells_a_quote_can_be_to_its_own_row():
    table = _table(["Item", "Q1"], [["Rent", "100"], ["Fees", "100"]])
    src = _source_of(table, row_index=1)
    src.text = "Item: Fees | Q1: 100"
    assert T._quote_cell_locator(src, src.text, table, {"quote": "Q1: 100", "number": "100"}) == \
        {"row_number": 2, "column_number": 2, "number": "100"}


# --- round 22 U3: a stored table whose rows span pages applies its caption scale to a later page only on evidence (KTD3, R9) -----

def _two_page_table(caption="להלן טבלת הוצאות (באלפי ₪)") -> dict:
    return {"headers": ["סעיף", "סכום"], "units": [None, None], "caption": caption, "title": [], "notes": [],
            "rows": [{"cells": ["שכירות", "100"], "page": 1}, {"cells": ["אחזקה", "500"], "page": 2}]}


def _cell_at(table: dict, row: str, **kw) -> dict:
    full = T._table_text(table, table["rows"])
    src = T.Source(sid="S1", document_id=VERSION.document_id, version_id=VERSION.version_id, title="t", section=None,
                   location="", kind="table", text=full, block_start=0, block_end=0, reading_id="r", status="complete")
    return T._cell_of(src, full, {"row": row, "column": "סכום"}, table, 0, **kw)


def test_without_a_block_above_the_later_rows_the_caption_scale_applies_to_both_pages():
    table = _two_page_table()
    for row in ("שכירות", "אחזקה"):
        taken = _cell_at(table, row, unconfirmed={})
        assert taken["scale"] == 1000 and not taken.get("unconfirmed") and taken["meaning_from"] == {"unit": "table"}


def test_with_a_block_above_the_later_rows_the_later_cell_carries_the_scale_unconfirmed_and_no_table_unit():
    table = _two_page_table()
    first = _cell_at(table, "שכירות", unconfirmed={2: "4. עלויות"})
    later = _cell_at(table, "אחזקה", unconfirmed={2: "4. עלויות"})
    assert first["scale"] == 1000 and not first.get("unconfirmed")  # the first page is the caption's own
    assert later["scale"] == 1000 and "4. עלויות" in later["unconfirmed"]  # never silently scale 1
    assert later["units"] == set() and "unit" not in later["meaning_from"]  # the unit is not the source's for this cell


def test_a_later_cell_with_an_unconfirmed_scale_registers_uncertain_so_a_calculation_with_it_is_conditional(monkeypatch):
    table = _two_page_table()
    d = _workspace(monkeypatch, table)
    monkeypatch.setattr(T, "_later_page_blocks", lambda conn, version_id, st, block: ([T.stored_tables.BlockAt(2, 60.0, "4. עלויות")], None, None))
    _take(d, _loc(row="שכירות", column="סכום"), kind="rent", unit="ILS")
    _take(d, _loc(row="אחזקה", column="סכום"), kind="rent", unit="ILS")
    assert "V1" not in d.ws.uncertain_values and "V2" in d.ws.uncertain_values
    assert "4. עלויות" in d.ws.uncertain_values["V2"]
    assert d.ws.values["V2"].scale == 1000


def test_a_table_with_a_candidate_continuation_link_inherits_no_scale_from_above_its_other_part(monkeypatch):
    heading = SimpleNamespace(block_index=0, text="ממצאי הבדיקה באלפי ₪", page=1, status="read", section="נתונים",
                              section_path=["נתונים"], bbox=None, media=None, table_index=None, method=None, note="", kind="heading")
    first = SimpleNamespace(block_index=1, text="טבלה", page=1, status="read", section="נתונים", section_path=["נתונים"], bbox=None,
                            media=None, table_index=0, method=None, note="", kind="table")
    second = SimpleNamespace(block_index=2, text="טבלה", page=2, status="read", section="נתונים", section_path=["נתונים"],
                             bbox=None, media=None, table_index=1, method=None, note="", kind="table")
    structures = {0: {"headers": ["א", "ב"], "rows": [{"cells": ["x", "1"], "page": 1}]},
                  1: {"headers": [], "rows": [{"cells": ["y", "2"], "page": 2}], "candidate_of": 0}}
    monkeypatch.setattr(T.reader, "blocks_between", lambda conn, vid, a, b, limit=None: [heading, first, second])
    monkeypatch.setattr(T.reader, "table_structure", lambda conn, vid, ti: structures[ti])
    monkeypatch.setattr(T.reader, "table_block", lambda conn, vid, ti: (first, second)[ti])
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T, "contexts_of", lambda ws, conn, vid: T.contexts.derive([], str(vid)))
    ws = T.Workspace(ctx=None)
    assert T._governing_note(ws, None, VERSION.version_id, 1) != ""  # the heading governs the first part
    assert T._governing_note(ws, None, VERSION.version_id, 2, structures[1].get("candidate_of")) == ""  # not across the link
