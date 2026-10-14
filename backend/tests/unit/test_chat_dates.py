"""The pure side of the datum-date resolution (round 11 U6b: R5, R6, R7; KTD3, KTD4): which dates the label vocabulary
finds in stored blocks, how a unit's valuation date is settled, the `as_of` order (own date, table range, unit
valuation), the forecast rule and the measurement's stored date. No database. All texts are synthetic."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.chat import dates as D

ANCHOR = date(2026, 10, 14)


def block(i: int, text: str, kind: str = "paragraph", section_path: tuple = (), table_index: int | None = None):
    return SimpleNamespace(block_index=i, kind=kind, text=text, section_path=list(section_path), table_index=table_index)


def found(blocks, tables=None):
    return [(f.block_index, f.iso, f.precision) for f in D.derive(blocks, tables)]


def table(headers: list[str], *rows: list[str]) -> dict:
    """A table as `extracted_tables.structure` stores it (the parts `derive` reads)."""
    return {"headers": headers, "rows": [{"cells": r} for r in rows]}


def stated(i: int, iso: str, precision: str = "day", unit_no: int | None = 1) -> D.StatedDate:
    return D.StatedDate(i, iso, precision, iso, "stated", unit_no)


# --- derivation: the labelled valuation date, never the report-writing or signature date -------------------------

def test_the_valuation_date_is_taken_and_the_report_date_never_is():
    blocks = [block(0, "המועד הקובע: 15/04/2024"), block(1, "תאריך עריכת השומה: 22/04/2025")]
    assert found(blocks) == [(0, "2024-04-15", "day")]


@pytest.mark.parametrize("text, iso, precision", [
    ("המועד הקובע: 15/04/2024", "2024-04-15", "day"),
    ("המועד הקובע לשומה הוא 15.4.2024.", "2024-04-15", "day"),
    ("שווי הנכס למועד הקובע ליום 15 באפריל 2024 הינו כמפורט להלן.", "2024-04-15", "day"),
    ("מועד קובע – אפריל 2024", "2024-04", "month"),
    ("המועד הקובע לשומה ליום 2024-04-15", "2024-04-15", "day"),
    ("המועד הקובע | 15/04/2024", "2024-04-15", "day"),
    ("המועד הקובע\n15/04/2024", "2024-04-15", "day"),
])
def test_a_valuation_label_takes_the_date_that_follows_it_with_its_precision(text, iso, precision):
    assert found([block(3, text)]) == [(3, iso, precision)]


def test_a_label_alone_in_a_cell_takes_the_date_of_the_next_cell():
    assert found([block(4, "המועד הקובע", "table"), block(5, "15/04/2024", "table")]) == [(5, "2024-04-15", "day")]


@pytest.mark.parametrize("text", [
    "תאריך עריכת השומה: 22/04/2025",
    "תאריך הדוח: 22/04/2025",
    "נחתם ביום 22/04/2025",
    "חתימה: ____ תאריך חתימה 22/04/2025",
    "בתאריך 15/04/2024 נערכה בדיקה במקום",               # a date with no valuation label
    "המועד הקובע הוא תשפ״ד",                                # a Hebrew-calendar year is unknown
    "המועד הקובע הרבעון הראשון Q1 2024",
    "המועד הקובע יקבע בהמשך. עסקה מיום 15/04/2024.",        # the date is not the label's
    "המועד הקובע בין 2020 ל-2022",                           # a range is no valuation date
])
def test_what_is_not_a_labelled_valuation_date_is_not_stored(text):
    assert found([block(0, text)]) == []


def test_a_report_date_beside_the_valuation_date_in_one_block_is_not_taken_as_it():
    blocks = [block(0, "תאריך עריכת השומה: 22/04/2025, המועד הקובע: 15/04/2024")]
    assert found(blocks) == [(0, "2024-04-15", "day")]
    assert [p.iso for p in D.report_dates(blocks[0].text)] == ["2025-04-22"]


def test_the_report_date_is_recognised_for_both_report_and_signature_labels():
    assert [p.iso for p in D.report_dates("נחתם ביום 03/05/2025")] == ["2025-05-03"]
    assert [p.iso for p in D.report_dates("תאריך הדוח: 03.05.2025")] == ["2025-05-03"]
    assert D.report_dates("המועד הקובע: 15/04/2024") == []


def test_a_picture_block_and_bidi_marks_are_handled():
    assert found([block(0, "המועד הקובע: 15/04/2024", "image")]) == []
    assert found([block(1, "‏המועד הקובע‏: ‎15/04/2024")]) == [(1, "2024-04-15", "day")]


def test_the_same_date_twice_in_one_block_is_one_find():
    assert found([block(0, "המועד הקובע: 15/04/2024. שוב, המועד הקובע: 15/04/2024")]) == [(0, "2024-04-15", "day")]


# --- dates-v2 (round 12 U3: R15, R16, R21; KTD11, KTD12; AE6) --------------------------------------------------------

@pytest.mark.parametrize("text, iso, precision", [
    # extra words between the label and the date (KTD11: "זו", "קרי", the visit phrase as a gap only)
    ("המועד הקובע להערכה זו הינו מועד הביקור בנכס – קרי 16.08.2026", "2026-08-16", "day"),
    ("המועד הקובע לשומה זו הינו מועד הביקור בנכס – קרי 15/03/2024.", "2024-03-15", "day"),
    ("המועד הקובע לשומה זה, כלומר ליום 15/03/2024", "2024-03-15", "day"),
    ("המועד הקובע דהיינו 15/03/2024", "2024-03-15", "day"),
    # the label "תאריך הקובע"
    ("התאריך הקובע לשומה: 28/02/2024", "2024-02-28", "day"),
    ("לתאריך הקובע 31/12/2023 נקבע השווי", "2023-12-31", "day"),
    # a label-anchored two-digit year: day-first, 00-69 -> 20YY
    ("המועד הקובע: 15/04/24", "2024-04-15", "day"),
    ("המועד הקובע לשומה - 31.12.23", "2023-12-31", "day"),
    ("המועד הקובע 31.12.23, ועד 15/3/2024", "2023-12-31", "day"),
    ("המועד הקובע: 1.2.00", "2000-02-01", "day"),
    ("המועד הקובע: 1.2.69", "2069-02-01", "day"),
    # a date right before its label, with a direct connector
    ("17.08.2025, שהינו גם התאריך הקובע להערכה.", "2025-08-17", "day"),
    ("31/12/2023 שהוא המועד הקובע", "2023-12-31", "day"),
    ("הערכה זו נערכת ליום 31.3.2024 והוא המועד הקובע לשומה.", "2024-03-31", "day"),
])
def test_dates_v2_reads_the_further_evidenced_structures(text, iso, precision):
    assert found([block(1, text)]) == [(1, iso, precision)]


@pytest.mark.parametrize("text", [
    "המועד הקובע 31.12.75",                        # 70-99 is refused
    "המועד הקובע: 1.2.70",
    "המועד הקובע: 12.31.23",                       # day-first only: month 31 is no date
    "המועד הקובע: 31.02.23",                       # an impossible date
    "המועד הקובע: 1.1.23-31.12.23",                # never a range
    "המועד הקובע: 1.1.23 עד 31.12.23",
    "מועד הביקור בנכס: 15/03/2023",                # the visit phrase is no label alone
    "ביקור בנכס נערך ב-05.07.24",                  # a two-digit year with no label
    "תאריך הפקה: 22/03/23",                        # a report date
    "תאריך עריכת השומה: 22/04/23",
    "נחתם ביום 05.01.2024, שהינו גם המועד הקובע",  # the date-before reading takes no report date
    "נחתם ביום 05.01.2024. המועד הקובע ייקבע בהמשך.",
    "05.01.2024 המועד הקובע",                      # no connector between the date and its label
    "31/12/2023, וגם נערך סיור. המועד הקובע ייקבע",  # a connector must be direct
])
def test_dates_v2_refuses_what_it_does_not_evidence(text):
    assert found([block(1, text)]) == []


def test_a_signature_date_before_the_label_is_not_taken_but_the_valuation_date_after_it_is():
    assert found([block(0, "נחתם ביום 05.01.2024. המועד הקובע: 31/03/2024")]) == [(0, "2024-03-31", "day")]


def test_a_two_digit_report_date_is_recognised_as_one_and_never_taken_as_the_valuation_date():
    assert [p.iso for p in D.report_dates("תאריך הפקה: 22/03/23")] == ["2023-03-22"]
    assert [p.iso for p in D.report_dates("תאריך עריכת השומה: 22/04/23")] == ["2023-04-22"]
    assert found([block(0, "תאריך הפקה: 22/03/23, המועד הקובע: 15/04/2024")]) == [(0, "2024-04-15", "day")]


def test_the_shared_parser_still_refuses_two_digit_years_everywhere_else():
    from app.platform import dates as shared
    assert shared.find_all("המועד הקובע: 15/04/24") == []
    assert D.resolve_as_of("שווי הנכס ב-15/04/24 הוא 2,000,000 ₪", anchor=ANCHOR) is None


def test_a_heading_with_the_label_alone_takes_the_date_the_next_block_starts_with():
    assert found([block(3, "המועד הקובע", "heading"), block(4, "31.5.2021.")]) == [(4, "2021-05-31", "day")]
    assert found([block(3, "5. המועד הקובע", "heading"),
                  block(4, "30/06/2024 – תאריך הביקור בנכס והמועד שאליו מתייחסת השומה.")]) == [(4, "2024-06-30", "day")]
    assert found([block(3, "המועד הקובע", "heading"), block(4, "31.5.21")]) == [(4, "2021-05-31", "day")]


def test_a_heading_with_the_label_alone_does_not_take_a_report_date_or_a_block_that_does_not_start_with_a_date():
    assert found([block(3, "המועד הקובע", "heading"), block(4, "נחתם ביום 31.5.2021")]) == []
    assert found([block(3, "המועד הקובע", "heading"), block(4, "ייקבע בהמשך. עסקה מיום 31.5.2021.")]) == []


def test_a_header_label_of_a_one_row_table_gives_the_date_of_its_column():
    tables = {0: table(["מועד קובע", "כתובת", "סוג נכס"], ["7.11.2020", "הרימון 4", "דירה"])}
    blocks = [block(5, "מועד קובע | כתובת | סוג נכס\n7.11.2020 | הרימון 4 | דירה", "table", table_index=0)]
    assert found(blocks, tables) == [(5, "2020-11-07", "day")]


def test_the_label_may_be_in_any_column_and_the_date_comes_from_that_column_only():
    tables = {0: table(["תאריך הפקה", "מועד קובע"], ["01/02/2026", "7.11.2020"])}
    blocks = [block(5, "תאריך הפקה | מועד קובע\n01/02/2026 | 7.11.2020", "table", table_index=0)]
    assert found(blocks, tables) == [(5, "2020-11-07", "day")]
    # the label is the last cell of the header line and the first data cell is another column's date: not the label's
    tables = {0: table(["תאריך הפקה", "מועד קובע"], ["01/02/2026", "ייקבע בהמשך"])}
    blocks = [block(5, "תאריך הפקה | מועד קובע\n01/02/2026 | ייקבע בהמשך", "table", table_index=0)]
    assert found(blocks, tables) == []


def test_a_header_label_of_a_table_with_several_data_rows_is_history_and_gives_no_date():
    tables = {0: table(["מועד קובע", "שווי (₪)"], ["31/12/2021", "2,050,000"], ["31/12/2023", "2,300,000"])}
    text = "מועד קובע | שווי (₪)\n31/12/2021 | 2,050,000\n31/12/2023 | 2,300,000"
    assert found([block(5, text, "table", table_index=0)], tables) == []
    three = {0: table(["שווי (₪)", "מועד קובע"], ["2,050,000", "31/12/2021"], ["2,100,000", "31/12/2022"],
                      ["2,300,000", "31/12/2023"])}
    text3 = "שווי (₪) | מועד קובע\n2,050,000 | 31/12/2021\n2,100,000 | 31/12/2022\n2,300,000 | 31/12/2023"
    assert found([block(5, text3, "table", table_index=0)], three) == []


def test_a_one_row_table_keeps_the_other_dates_of_the_unit_and_a_multi_row_one_does_not_add_a_conflict():
    tables = {0: table(["מועד קובע", "שווי (₪)"], ["31/12/2021", "2,050,000"], ["31/12/2023", "2,300,000"])}
    blocks = [block(1, "המועד הקובע: 30/06/2025"),
              block(5, "מועד קובע | שווי (₪)\n31/12/2021 | 2,050,000\n31/12/2023 | 2,300,000", "table", table_index=0)]
    assert found(blocks, tables) == [(1, "2025-06-30", "day")]


def test_a_table_title_line_before_the_header_line_does_not_hide_the_header_from_the_table_rule():
    tables = {0: table(["כתובת", "מועד קובע"], ["הרימון 4", "ייקבע בהמשך"], ["הרימון 5", "31/12/2021"])}
    text = "טבלת נכסים\nכתובת | מועד קובע\nהרימון 4 | ייקבע בהמשך\nהרימון 5 | 31/12/2021"
    assert found([block(5, text, "table", table_index=0)], tables) == []
    one = {0: table(["שם הטבלה", "מועד קובע"], ["הרימון 4", "ייקבע בהמשך"])}
    # the header line is the last cell's label and the next line starts with a date of another column: not the label's
    assert found([block(5, "פרטים\nשם הטבלה | מועד קובע\nהרימון 4 | ייקבע בהמשך", "table", table_index=0)], one) == []


def test_a_table_block_without_a_structure_is_read_as_text_as_before():
    assert found([block(5, "מועד קובע | 15/04/2024", "table", table_index=7)], {}) == [(5, "2024-04-15", "day")]
    assert found([block(5, "מועד קובע | 15/04/2024", "table")]) == [(5, "2024-04-15", "day")]


# --- the history guard in derive (KTD11, KTD12; AE6) ----------------------------------------------------------------

PRIOR = ("3. שומות קודמות",)


def test_ae6_a_prior_appraisals_date_never_becomes_the_units_date_or_a_conflict():
    blocks = [
        block(0, "המועד הקובע לשומה - 31.12.23"),
        block(1, "3. שומות קודמות", "heading", section_path=PRIOR),
        block(2, "לתאריך הקובע 31.12.19 הוערך הנכס ב-1,800,000 ₪.", section_path=PRIOR),
        block(3, "בשומה קודמת, לתאריך הקובע 31.12.21, הוערך הנכס ב-2,000,000 ₪.", section_path=PRIOR),
    ]
    assert found(blocks) == [(0, "2023-12-31", "day")]
    stated = D.assign(D.derive(blocks), ())
    assert [(d.iso, d.status) for d in stated] == [("2023-12-31", "stated")]


def test_a_round_11_label_under_a_history_heading_is_not_a_unit_date():
    blocks = [block(1, "שומות מקרקעין קודמות", "heading", section_path=("שומות מקרקעין קודמות",)),
              block(2, "המועד הקובע 30.6.22", section_path=("שומות מקרקעין קודמות",))]
    assert found(blocks) == []
    assert found([block(2, "המועד הקובע 30.6.2022", section_path=("3. היסטוריה",))]) == []


def test_every_reading_is_guarded_the_new_ones_and_the_old_ones_alike():
    path = ("2. רכישות קודמות", "2.1 פירוט")
    for text in ("המועד הקובע: 15/04/2024", "המועד הקובע: 15/04/24", "17.08.2025, שהינו גם התאריך הקובע",
                 "לתאריך הקובע 31/12/2019 הוערך הנכס"):
        assert found([block(2, text, section_path=path)]) == [], text
    assert found([block(1, "המועד הקובע", "heading", section_path=path), block(2, "31.5.2021.", section_path=path)]) == []
    tables = {0: table(["מועד קובע", "שווי (₪)"], ["31/12/2021", "2,050,000"])}
    assert found([block(5, "מועד קובע | שווי (₪)\n31/12/2021 | 2,050,000", "table", section_path=path, table_index=0)],
                 tables) == []


def test_a_history_cue_in_the_sentence_marks_it_whatever_the_section():
    assert found([block(2, "הנכס נרכש ביום 15/05/2017, המועד הקובע ליום 15/05/2017.")]) == []
    assert found([block(2, "בעבר הוערך הנכס, המועד הקובע 31.12.19.")]) == []


def test_a_current_conclusion_sentence_keeps_the_units_date():
    blocks = [block(1, "המועד הקובע: 31/12/2025"),
              block(2, "שווי הזכויות בנכס הוערך ב-2,640,000 ₪ נכון למועד הקובע.")]
    assert found(blocks) == [(1, "2025-12-31", "day")]
    # the cue is in another sentence of the block: the label's sentence is the one that counts
    assert found([block(1, "הנכס נרכש בשנת 2017. המועד הקובע: 31/12/2025")]) == [(1, "2025-12-31", "day")]


# --- the date-pattern fixtures of round 12, read through the DOCX reader ------------------------------------------------

def _fixture_blocks(name: str):
    import time
    from pathlib import Path

    from app.config import get_settings
    from app.extraction.docx import extract_docx
    path = Path(__file__).resolve().parents[1] / "fixtures" / "round12" / name
    result = extract_docx(path.read_bytes(), time.monotonic() + 60, get_settings())
    tables = {t.index: {"headers": t.headers, "rows": [{"cells": r.cells} for r in t.rows]} for t in result.tables}
    blocks = [SimpleNamespace(block_index=b.index, kind=b.kind, text=b.text, section_path=list(b.section_path),
                              table_index=b.table_index) for b in result.blocks]
    return blocks, tables


def _date_pattern_documents():
    import json
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round12" / "manifest.json").read_text())
    return [(d["key"], d["file"].split("/")[-1], d["units"][0]["date_pattern"]["expected_date"])
            for d in manifest["documents"].values() if d["units"] and "date_pattern" in d["units"][0]]


@pytest.mark.parametrize("key, name, expected", _date_pattern_documents(), ids=lambda v: v if isinstance(v, str) else None)
def test_each_date_pattern_fixture_derives_its_manifest_date(key, name, expected):
    blocks, tables = _fixture_blocks(name)
    got = {f.iso for f in D.derive(blocks, tables)}
    assert got == ({expected} if expected else set()), key


# --- the unit's valuation date ------------------------------------------------------------------------------------

def test_one_valuation_date_in_a_unit_is_stated():
    uv = D.resolve_unit([stated(2, "2024-04-15")], 1)
    assert (uv.status, uv.date.iso, uv.unit_no) == ("stated", "2024-04-15", 1)


def test_two_differing_dates_in_a_unit_are_a_conflict_with_no_date():
    uv = D.resolve_unit([stated(2, "2024-04-15"), stated(9, "2025-01-01")], 1)
    assert (uv.status, uv.date) == ("conflict", None) and len(uv.dates) == 2


def test_the_same_date_repeated_is_not_a_conflict_and_a_month_inside_a_day_agrees_with_it():
    assert D.resolve_unit([stated(2, "2024-04-15"), stated(9, "2024-04-15")], 1).status == "stated"
    uv = D.resolve_unit([stated(2, "2024-04", "month"), stated(9, "2024-04-15")], 1)
    assert (uv.status, uv.date.iso) == ("stated", "2024-04-15")
    assert D.resolve_unit([stated(2, "2024-04", "month"), stated(9, "2024-05-15")], 1).status == "conflict"


def test_no_dates_is_none():
    uv = D.resolve_unit([], 1)
    assert (uv.status, uv.date, uv.dates) == ("none", None, ())


def test_assignment_follows_the_units_as_they_are_now():
    from app.chat.units import Unit

    def unit(no, first, last):
        return Unit(no, "appraisal", None, (), {}, None, None, first, last, "certain", {})

    found_ = [D.Found(3, "2024-04-15", "day", "15/04/2024"), D.Found(40, "2025-02-01", "day", "01/02/2025")]
    two = D.assign(found_, (unit(1, 0, 30), unit(2, 31, 80)))
    assert [(d.block_index, d.unit_no, d.status) for d in two] == [(3, 1, "stated"), (40, 2, "stated")]
    one = D.assign(found_, (unit(1, 0, 80),))
    assert [(d.unit_no, d.status) for d in one] == [(1, "conflict"), (1, "conflict")]
    outside = D.assign(found_, (unit(1, 0, 10),))
    assert [d.unit_no for d in outside] == [1, None]
    whole = D.assign(found_, ())  # a version with no units: the document is the scope
    assert [(d.unit_no, d.status) for d in whole] == [(0, "conflict"), (0, "conflict")]


# --- as_of: the three sources, in order ---------------------------------------------------------------------------

def resolve(text, **kw):
    return D.resolve_as_of(text, anchor=kw.pop("anchor", ANCHOR), **kw)


UV = D.UnitValuation("stated", stated(2, "2024-04-15"), (stated(2, "2024-04-15"),), 1)
UV_CONFLICT = D.UnitValuation("conflict", None, (stated(2, "2024-04-15"), stated(9, "2025-01-01")), 1)


def test_a_single_date_in_the_datum_own_sentence_dates_it_with_source_provenance():
    got = resolve("מחיר העסקה ב-12/2023 היה 1,900,000 ₪")
    assert got == {"date": "2023-12", "precision": "month", "end": None, "provenance": "source", "forecast": False,
                   "locator": "text"}


def test_a_table_row_with_a_month_date_cell():
    got = resolve("12/2023 | עסקה | 1,900,000")
    assert (got["date"], got["precision"], got["provenance"]) == ("2023-12", "month", "source")


def test_a_row_with_no_date_is_unknown():
    assert resolve("תל אביב | עסקה | 1,900,000") is None


def test_the_table_caption_range_dates_undated_rows_with_table_provenance():
    got = resolve("תל אביב | עסקה | 1,900,000", table_caption="עסקאות 2021–2022")
    assert (got["date"], got["end"], got["precision"], got["provenance"]) == ("2021", "2022", "year", "source_table")


def test_a_caption_with_no_range_dates_nothing():
    assert resolve("תל אביב | 1,900,000", table_caption="עסקאות בנכסים דומים") is None
    assert resolve("תל אביב | 1,900,000", table_caption="עסקאות בשנת 2022") is None  # KTD4: a range only


def test_the_rows_own_date_wins_over_the_caption_range():
    got = resolve("03/2022 | 1,900,000", table_caption="עסקאות 2021–2022")
    assert (got["date"], got["provenance"]) == ("2022-03", "source")


def test_two_different_dates_in_a_sentence_are_ambiguous_and_never_fall_through():
    assert resolve("בין 03/2022 ל-05/2023 ו-12/2024 נמכרו שלוש דירות", table_caption="עסקאות 2021–2022",
                   stance="adopted", unit_valuation=UV) is None
    assert resolve("ב-15/04/2022 וב-20/05/2023: 1,900,000 ₪", unit_valuation=UV) is None


def test_one_range_in_the_sentence_is_one_date_with_an_end():
    got = resolve("בין 2021 ל-2022 נמכרו הנכסים")
    assert (got["date"], got["end"], got["precision"], got["provenance"]) == ("2021", "2022", "year", "source")


def test_an_adopted_conclusion_with_no_date_takes_the_units_valuation_date():
    got = resolve("שווי הנכס 2,450,000 ₪", stance="adopted", unit_valuation=UV)
    assert got == {"date": "2024-04-15", "precision": "day", "end": None, "provenance": "unit_valuation",
                   "forecast": False, "locator": "block 2"}


def test_a_header_line_value_whose_stance_is_unknown_is_dated_by_its_unit():
    """Round 11 U15 A: the value of a header line ("שווי הנכס: 2,410,000 ₪") is registered with the stance unknown
    (the source does not say who stated it); the unit's valuation date still dates it."""
    for stance in ("unknown", None, "other"):
        got = resolve("שווי הנכס: 2,410,000 ₪", stance=stance, role="other", unit_valuation=UV)
        assert (got["date"], got["provenance"]) == ("2024-04-15", "unit_valuation"), stance


@pytest.mark.parametrize("stance", ["claim", "proposal", "estimate"])
def test_a_partys_claim_proposal_or_estimate_stays_unknown_whatever_its_provenance(stance):
    assert resolve("שווי הנכס 2,450,000 ₪", stance=stance, unit_valuation=UV) is None


def test_a_value_attributed_to_another_party_stays_unknown_but_the_appraiser_is_not_another_party():
    kw = dict(stance="unknown", unit_valuation=UV, stated_by_provenance="source")
    for who in ("המשיבה", "שמאי המשיבה", "העוררת", "היזם"):
        assert resolve("שווי הנכס 2,450,000 ₪", stated_by=who, **kw) is None, who
    for who in ("השמאי", "שמאי", "השמאי המכריע", ""):
        assert resolve("שווי הנכס 2,450,000 ₪", stated_by=who, **kw)["provenance"] == "unit_valuation", who
    # a speaker the model asserted and the source does not attest is not an attribution to another party
    assert resolve("שווי הנכס 2,450,000 ₪", stance="unknown", stated_by="המשיבה", stated_by_provenance="model_asserted",
                   unit_valuation=UV)["provenance"] == "unit_valuation"


@pytest.mark.parametrize("role", ["comparison", "comparable", "transaction"])
def test_a_value_whose_role_marks_a_comparable_or_a_transaction_stays_unknown(role):
    assert resolve("נמכרה דירה סמוכה ב-1,900,000 ₪", stance="unknown", role=role, unit_valuation=UV) is None


def test_a_row_of_a_comparables_table_without_a_date_of_its_own_stays_unknown():
    assert resolve("רחוב הברוש 5 | | 2,000,000", stance="unknown", role="other", comparables=True,
                   unit_valuation=UV) is None
    # its own date still dates it, and a caption range
    assert resolve("רחוב הדקל 3 | 01/03/2024 | 1,900,000", comparables=True, unit_valuation=UV)["provenance"] == "source"
    assert resolve("x | 1,900,000", table_caption="עסקאות 2021–2022", comparables=True,
                   unit_valuation=UV)["provenance"] == "source_table"


@pytest.mark.parametrize("headers, caption, expected", [
    (["נכס", "מועד", "מחיר"], None, True),                       # a date column
    (["נכס", "תאריך עסקה", "מחיר"], "", True),
    (["נכס", "מחיר"], "עסקאות בנכסים דומים", True),               # the caption marks transactions
    (["נכס", "מחיר"], "נכסי השוואה", True),
    (["נכס", "מחיר להשוואה"], None, True),                        # a header marks comparison
    (["פרט", "ערך"], "סיכום השומה", False),                       # a summary table
    (["שנת בנייה", "שטח"], None, False),                           # year built is not a date of the datum
])
def test_a_table_is_a_comparables_table_by_a_date_column_or_a_comparables_caption(headers, caption, expected):
    assert D.is_comparables_table(headers, caption) is expected


def test_a_date_in_the_sentence_is_its_own_date_even_for_an_adopted_conclusion():
    got = resolve("שווי הנכס ביום 01/02/2025 הוא 2,450,000 ₪", stance="adopted",
                  unit_valuation=UV)
    assert (got["date"], got["provenance"]) == ("2025-02-01", "source")


def test_a_price_a_parcel_and_an_area_in_the_sentence_are_no_date():
    assert resolve("חלקה 2024 בגוש 6001, שטח 120 מ״ר, 2,024,000 ₪", stance="adopted", unit_valuation=UV)["provenance"] == "unit_valuation"


# --- forecast -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "דמי השכירות צפויים לעמוד ב-2027 על 6,000 ₪",
    "הערך צפוי לעלות", "צפויה עלייה בשכר הדירה", "תחזית לשנת 2027", "המחיר יהיה גבוה יותר", "השכירות תהיה גבוהה",
    "ייתכן שינוי בעתיד", "הערכה ל-2028 על 3,000,000 ₪", "הערכה לשנת 2028", "אפשרויות עתיד", "לעתיד הקרוב",
])
def test_a_forecast_marker_makes_a_forecast(sentence):
    assert D.is_forecast(sentence, None, ANCHOR) is True


@pytest.mark.parametrize("sentence", [
    "דמי השכירות עמדו ב-2022 על 5,000 ₪", "המכר בוצע ביום 15/04/2024", "הערכה לשווי הנכס בוצעה על ידי השמאי",
    "הוצאה בפועל", "",
])
def test_a_past_sentence_without_a_marker_is_not_a_forecast(sentence):
    assert D.is_forecast(sentence, None, ANCHOR) is False


def test_a_date_after_the_anchor_is_a_forecast_by_its_precision():
    def asof(iso, precision, end=None):
        return {"date": iso, "precision": precision, "end": end}

    assert D.is_forecast("מחיר", asof("2026-10-15", "day"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026-10-14", "day"), ANCHOR) is False
    assert D.is_forecast("מחיר", asof("2026-11", "month"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026-10", "month"), ANCHOR) is False  # this month is not later than today
    assert D.is_forecast("מחיר", asof("2027", "year"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026", "year"), ANCHOR) is False
    assert D.is_forecast("מחיר", asof("2025", "year", "2028"), ANCHOR) is True  # a range into the future
    assert D.is_forecast("מחיר", asof("2021", "year", "2022"), ANCHOR) is False


def test_resolve_marks_a_future_date_as_a_forecast_with_no_marker_and_stance_plays_no_part():
    got = resolve("שכר הדירה ב-2027 הוא 6,000 ₪")
    assert (got["date"], got["forecast"]) == ("2027", True)
    got = resolve("שכר הדירה ב-2022 הוא 5,000 ₪", stance="estimate")
    assert (got["date"], got["forecast"]) == ("2022", False)  # `estimate` is a party's estimate, not a forecast


def test_a_forecast_sentence_with_a_past_date_is_a_forecast():
    assert resolve("תחזית: ב-2022 התכוונו לשכר של 5,000 ₪")["forecast"] is True


def test_the_unit_valuation_date_after_the_anchor_is_a_forecast_too():
    future = D.UnitValuation("stated", stated(2, "2027-01-01"), (stated(2, "2027-01-01"),), 1)
    got = resolve("שווי הנכס 2,450,000 ₪", stance="adopted", unit_valuation=future)
    assert got["forecast"] is True


# --- the measurement: the stored date counts only when the quote says it ------------------------------------------

def test_a_stored_date_the_quote_does_not_hold_is_unknown():
    assert D.measurement_as_of("שכר דירה חודשי 5,000 ₪", "2023", anchor=ANCHOR) is None


def test_a_stored_year_the_quote_holds_beside_a_date_word_dates_the_measurement_with_source_provenance():
    got = D.measurement_as_of("בשנת 2023 עמד שכר הדירה על 5,000 ₪", "2023", anchor=ANCHOR)
    assert (got["date"], got["precision"], got["provenance"], got["locator"]) == ("2023", "year", "source", "quote")


def test_a_bare_number_that_equals_the_stored_year_is_no_date():
    assert D.measurement_as_of("שכר דירה 2023 ₪ לחודש", "2023", anchor=ANCHOR) is None


def test_the_stored_date_picks_among_the_quotes_dates_when_it_names_one_of_them():
    quote = "בין 15/04/2022 לבין 20/05/2023 נחתם החוזה ב-20/05/2023, ושכר הדירה 5,000 ₪"
    assert D.resolve_as_of(quote, anchor=ANCHOR) is None  # ambiguous by itself
    got = D.measurement_as_of(quote, "2023-05-20", anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2023-05-20", "source")
    assert D.measurement_as_of(quote, "2021-01-01", anchor=ANCHOR) is None


def test_a_stored_date_of_another_precision_than_the_quote_is_not_the_same_date():
    assert D.measurement_as_of("מיום 15/04/2023 שכר 5,000 ₪ ו-20/05/2023: 5,200 ₪", "2023", anchor=ANCHOR) is None


def test_a_quote_with_its_own_single_date_dates_the_measurement_with_no_stored_date():
    got = D.measurement_as_of("שכר דירה מיום 15/04/2023: 5,000 ₪", None, anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2023-04-15", "source")


def test_a_measurement_forecast_is_marked():
    got = D.measurement_as_of("בשנת 2029 צפוי שכר של 7,000 ₪", "2029", anchor=ANCHOR)
    assert got["forecast"] is True
