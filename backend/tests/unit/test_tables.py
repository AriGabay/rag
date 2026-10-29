"""Table assembly: header detection, column reversal, cross-page continuation (U5, R4)."""

from __future__ import annotations

import pytest

from app.extraction.tables import (
    RawTable,
    assemble_tables,
    is_header_row,
    logical_row,
    reads_left_to_right,
    units_for,
)

HEADERS = ["כתובת", "גוש/חלקה", "תאריך עסקה", "סוג נכס", "חדרים", "שטח (מ״ר)", "סוג שטח", "מחיר (₪)",
           "מחיר למ״ר (₪)"]
# pdfplumber extract_tables() output for D1 (left -> right, i.e. reversed columns, visual cell text).
RAW_HEADER = [")₪( ר״מל ריחמ", ")₪( ריחמ", "חטש גוס", ")ר״מ( חטש", "םירדח", "סכנ גוס", "הקסע ךיראת",
              "הקלח/שוג", "תבותכ"]
RAW_ROW0 = ["26,000", "2,470,000", "וטנ", "95", "4", "הריד", "12/02/2024", "6158/42/3", "20 ןפגה"]


def _row(i: int) -> list[str]:
    return [f"רחוב {i}", f"6160/{i}/1", "01/01/2024", "דירה", "4", "90", "נטו", f"2,{i:03d},000", "25,000"]


def test_columns_are_reversed_and_cell_text_fixed():
    assert logical_row(RAW_HEADER) == HEADERS
    assert logical_row(RAW_ROW0) == ["הגפן 20", "6158/42/3", "12/02/2024", "דירה", "4", "95", "נטו", "2,470,000",
                                     "26,000"]


def test_none_cells_become_empty_strings():
    assert logical_row(["", None, "4", "הריד"]) == ["דירה", "4", "", ""]


def test_header_detection_by_vocabulary():
    assert is_header_row(HEADERS)
    assert not is_header_row(_row(1))
    assert not is_header_row(["", "", ""])
    assert not is_header_row(["סוג נכס: דירה"])


def test_units_from_headers():
    assert units_for(HEADERS) == [None, None, None, None, None, "מ״ר", None, "₪", "₪"]


def test_single_page_table():
    tables = assemble_tables([RawTable(page=1, rows=[HEADERS, _row(1), _row(2)], section="3. עסקאות השוואה")])
    assert len(tables) == 1
    t = tables[0]
    assert t.headers == HEADERS and t.index == 0
    assert [r.page for r in t.rows] == [1, 1]
    assert (t.page_start, t.page_end, t.section, t.ocr) == (1, 1, "3. עסקאות השוואה", False)


def test_a_page_of_rows_without_the_header_and_without_cell_boxes_is_its_own_part_with_its_own_row_pages():
    """Round 22 U3: rows on the next page with no repeated header are not merged on their shape (here the pieces have no cell boxes, so
    nothing says their columns align); they are a candidate continuation (undecided): separate parts, each keeping its own rows'
    pages, the later one linked to the earlier."""
    raw = [
        RawTable(page=3, rows=[HEADERS, _row(1), _row(2)]),
        RawTable(page=4, rows=[_row(3), _row(4)]),
    ]
    tables = assemble_tables(raw)
    assert len(tables) == 2
    assert tables[0].headers == HEADERS and [r.page for r in tables[0].rows] == [3, 3]
    assert tables[1].headers == [] and [r.page for r in tables[1].rows] == [4, 4] and tables[1].candidate_of == 0
    assert (tables[0].page_start, tables[0].page_end, tables[1].page_start, tables[1].page_end) == (3, 3, 4, 4)


def test_repeated_header_on_continuation_page_is_dropped():
    raw = [
        RawTable(page=1, rows=[HEADERS, _row(1)]),
        RawTable(page=2, rows=[HEADERS, _row(2), _row(3)]),
    ]
    tables = assemble_tables(raw)
    assert len(tables) == 1
    assert [r.cells[0] for r in tables[0].rows] == ["רחוב 1", "רחוב 2", "רחוב 3"]
    assert all(r.cells != HEADERS for r in tables[0].rows)
    assert [r.page for r in tables[0].rows] == [1, 2, 2]


def test_different_column_count_starts_a_new_table():
    raw = [
        RawTable(page=1, rows=[HEADERS, _row(1)]),
        RawTable(page=2, rows=[["א", "ב"], ["1", "2"]]),
    ]
    tables = assemble_tables(raw)
    assert len(tables) == 2
    assert [t.index for t in tables] == [0, 1]


def test_new_header_with_different_text_starts_a_new_table():
    other = ["כתובת", "מחיר (₪)"]
    raw = [RawTable(page=1, rows=[HEADERS, _row(1)]), RawTable(page=2, rows=[other, ["הגפן 1", "100"]])]
    assert len(assemble_tables(raw)) == 2


def test_table_not_at_top_of_next_page_does_not_continue():
    raw = [
        RawTable(page=1, rows=[HEADERS, _row(1)]),
        RawTable(page=3, rows=[_row(2)]),
    ]
    assert len(assemble_tables(raw)) == 2


def test_ocr_flag_propagates():
    tables = assemble_tables([RawTable(page=1, rows=[HEADERS, _row(1)], ocr=True)])
    assert tables[0].ocr is True


def test_snap_header_fixes_ocr_noise_but_leaves_unknown_text():
    from app.extraction.tables import snap_header

    assert snap_header("שטח (מ'\"ר)") == "שטח (מ״ר)"
    assert snap_header("מחיר למ::ר (₪)") == "מחיר למ״ר (₪)"
    assert snap_header("מחיר (₪)") == "מחיר (₪)"
    assert snap_header("הערות") == "הערות"


def _word(text: str, left: int, top: int, line: int, order: int, width: int = 60) -> dict:
    return {"text": text, "left": left, "top": top, "width": width, "height": 30, "conf": 90.0,
            "order": order, "line": (1, 1, line)}


def test_borderless_ocr_table_from_word_boxes():
    """No ruling lines: header vocabulary gives the columns, following lines with digits are rows."""
    from app.extraction.ocr import _borderless_tables

    # Reading order (``order``) is right to left, as Tesseract reports it for Hebrew.
    words = [
        _word("עסקאות", 900, 100, 1, 0),
        _word("כתובת", 900, 200, 2, 1), _word("תאריך", 600, 200, 2, 2), _word("מחיר", 300, 200, 2, 3),
        _word("הגפן", 930, 250, 3, 4, 50), _word("20", 880, 250, 3, 5, 30),
        _word("12/02/2024", 580, 250, 3, 6, 120), _word("2,470,000", 290, 250, 3, 7, 110),
        _word("השקד", 930, 300, 4, 8, 50), _word("7", 890, 300, 4, 9, 20),
        _word("05/11/2023", 580, 300, 4, 10, 120), _word("2,050,000", 290, 300, 4, 11, 110),
        _word("4.", 950, 400, 5, 12), _word("שיקולי", 850, 400, 5, 13),
    ]
    tables, used = _borderless_tables(words)
    assert len(tables) == 1
    assert tables[0].rows == [
        ["כתובת", "תאריך", "מחיר"],
        ["הגפן 20", "12/02/2024", "2,470,000"],
        ["השקד 7", "05/11/2023", "2,050,000"],
    ]
    assert (1, 1, 1) not in used and (1, 1, 5) not in used


# --- round 17 U4: a table read's text carries the parts around the table (KTD4) -------------------------------------------

def test_table_text_puts_the_context_parts_around_the_table_and_the_notes_before_the_trailing_part():
    from app.chat import tools as T

    st = {"headers": ["תרחיש", "שווי"], "caption": "טבלת רגישות", "title": [], "notes": ["* באלפי ₪"],
          "rows": [{"cells": ["תרחיש 1", "4,010"]}]}
    out = T._table_text(st, st["rows"], before=["כותרות מעל הטבלה: 4. שיטות"],
                        after=["[אחרי הטבלה · פסקה · בלוק 9] הנתונים לגישת ההיוון בלבד."])
    lines = out.split("\n")
    assert lines[0] == "כותרות מעל הטבלה: 4. שיטות" and lines[1] == "טבלת רגישות"
    assert lines[-2:] == ["* באלפי ₪", "[אחרי הטבלה · פסקה · בלוק 9] הנתונים לגישת ההיוון בלבד."]
    assert T._table_text(st, st["rows"]) == "\n".join(lines[1:-1])  # no parts: the text as it always was


def test_a_part_is_one_line_with_its_kind_and_block_and_a_pointer_when_the_block_was_already_returned():
    from app.chat import tools as T

    part = T.TablePart("after", "paragraph", 9, "שורה ראשונה\nשורה שנייה", True)
    assert T._part_line(part) == "[אחרי הטבלה · בין שתי טבלאות · פסקה · בלוק 9] שורה ראשונה שורה שנייה"
    assert T._part_line(part, {9: "S4"}) == "[אחרי הטבלה · בין שתי טבלאות · פסקה · בלוק 9: כבר הוחזר ב-S4]"
    assert T._part_line(part, {3: "S1"}).endswith("שורה שנייה")


# --- a table with no Hebrew on a page with no Hebrew keeps the order it is stored in, when a cue shows it (round 20, U4) -----

ENGLISH_GRID = [["GLA (sq m)", "Tenant", "Cad. Municipality"], ["1,200", "Alpha", "North"]]


def test_an_english_table_on_a_page_with_no_hebrew_reads_left_to_right():
    assert reads_left_to_right(ENGLISH_GRID, page_has_hebrew=False)
    assert logical_row(ENGLISH_GRID[0], left_to_right=True) == ["GLA (sq m)", "Tenant", "Cad. Municipality"]
    assert logical_row(ENGLISH_GRID[1], left_to_right=True) == ["1,200", "Alpha", "North"]


def test_the_same_english_table_on_a_page_with_hebrew_is_not_decided_by_the_cue():
    assert not reads_left_to_right(ENGLISH_GRID, page_has_hebrew=True)
    assert logical_row(ENGLISH_GRID[0]) == ["Cad. Municipality", "Tenant", "GLA (sq m)"]


def test_a_hebrew_table_keeps_the_right_to_left_handling():
    assert not reads_left_to_right([RAW_HEADER, RAW_ROW0], page_has_hebrew=False)
    assert not reads_left_to_right([["Address", "תבותכ"], ["1", "2"]], page_has_hebrew=False)
    assert logical_row(["Address", "תבותכ"], left_to_right=True) == ["כתובת", "Address"]  # a Hebrew cell: never kept


def test_a_numbers_only_table_has_no_cue_unless_its_periods_rise_from_left_to_right():
    assert not reads_left_to_right([["1,200", "1,450"], ["3", "4"]], page_has_hebrew=False)
    assert not reads_left_to_right([["31/12/2022", "30/09/2022"], ["3", "4"]], page_has_hebrew=False)  # dates are no period cue
    assert reads_left_to_right([["2021", "2022", "2023"], ["3", "4", "5"]], page_has_hebrew=False)
    assert not reads_left_to_right([["2023", "2022", "2021"], ["3", "4", "5"]], page_has_hebrew=False)
    assert reads_left_to_right([["", "1Q23", "2Q23"], ["3", "4", "5"]], page_has_hebrew=False)
    assert reads_left_to_right([["3", "4"], ["2022", "2023"]], page_has_hebrew=False)
    assert logical_row(["1,200", "1,450"]) == ["1,450", "1,200"]  # no cue: today's handling


def test_a_trailing_minus_cell_keeps_its_sign_with_its_number_in_a_left_to_right_row():
    assert logical_row(["Net", "-50,000", "4,368,752"], left_to_right=True) == ["Net", "-50,000", "4,368,752"]


def test_wrapped_lines_of_a_left_to_right_cell_are_joined_in_reading_order():
    assert logical_row(["Gross\nrent", "1,200"], left_to_right=True) == ["Gross rent", "1,200"]


# --- round 21 U8: a stored table is served in the order it is drawn, each cell with its column label (KTD6, R11, AE6) ---------

def _box(x0: float) -> list[float]:
    return [x0, 100.0, x0 + 40.0, 112.0]


def _stored(rows: list[list[str]], xs: list[float] | None, headers: list[str] | None = None) -> dict:
    """A stored table structure: ``rows`` are the cells as stored, ``xs`` the x of each stored cell (None: no boxes)."""
    return {"headers": headers or [], "units": [], "caption": None, "title": [], "notes": [],
            "rows": [{"page": 1, "cells": r, **({"cell_boxes": [_box(x) for x in xs]} if xs else {})} for r in rows]}


# the round-19 generation: a left-to-right table stored from its right edge to its left one, so its boxes run from right to left
REVERSED = _stored([["Annual", "Q2 2024", "Q1 2024", "Item"], ["300", "200", "100", "Revenue"], ["15-", "20", "25", "Net"]],
                   [300, 220, 140, 60])
# the round-20 generation: the same table stored in the order it is drawn, boxes from left to right
DRAWN = _stored([["Item", "Q1 2024", "Q2 2024", "Annual"], ["Revenue", "100", "200", "300"], ["Net", "-25", "20", "-15"]],
                [60, 140, 220, 300])
NO_BOXES = _stored([["Annual", "Q2 2024", "Q1 2024", "Item"], ["300", "200", "100", "Revenue"]], None)
# a Hebrew table in logical (right-to-left) order: column 0 is the rightmost, so its boxes run from right to left too
HEBREW = _stored([["שווי", "שטח", "כתובת"], ["900", "80", "הגפן 4"]], [300, 180, 60], headers=["שווי", "שטח", "כתובת"])
NUMBERS_ONLY = _stored([["30", "20", "10"], ["3", "2", "1"]], [300, 180, 60])  # no cue: today's handling


def test_a_reversed_stored_table_with_a_left_to_right_cue_is_served_in_drawn_order_with_each_cell_paired():
    from app.extraction import tables

    assert tables.column_order(REVERSED) == [3, 2, 1, 0]
    served = tables.serve_structure(REVERSED)
    assert [r["cells"] for r in served["rows"]][:2] == [["Item", "Q1 2024", "Q2 2024", "Annual"], ["Revenue", "100", "200", "300"]]
    assert tables.render_served_row(served, 1) == "Item: Revenue | Q1 2024: 100 | Q2 2024: 200 | Annual: 300"
    assert tables.render_served_row(served, 0) == "Item | Q1 2024 | Q2 2024 | Annual"  # the row that holds the labels
    assert REVERSED["rows"][1]["cells"] == ["300", "200", "100", "Revenue"]  # nothing stored is changed


def test_the_boxes_follow_the_cells_and_each_served_column_names_its_stored_column():
    from app.extraction import tables

    served = tables.serve_structure(REVERSED)
    assert served["columns"] == [3, 2, 1, 0]
    assert [b[0] for b in served["rows"][1]["cell_boxes"]] == [60, 140, 220, 300]
    assert tables.stored_column(served, 1) == 2 and tables.served_position(served, 2) == 1  # Q1 2024: served 1, stored 2
    assert tables.stored_column(DRAWN, 1) == 1 and tables.served_position(DRAWN, 1) == 1


def test_a_negative_number_a_reversed_row_stored_with_its_minus_at_the_end_is_served_with_its_sign():
    from app.extraction import tables

    served = tables.serve_structure(REVERSED)
    assert served["rows"][2]["cells"] == ["Net", "25", "20", "-15"]
    assert tables.render_served_row(served, 2) == "Item: Net | Q1 2024: 25 | Q2 2024: 20 | Annual: -15"
    assert tables.fix_sign("5%-") == "-5%" and tables.fix_sign("₪ 3-") == "-3 ₪" and tables.fix_sign("1,200") == "1,200"
    assert tables.fix_sign("Q1-") == "Q1-" and tables.fix_sign("-") == "-"


@pytest.mark.parametrize("st", [DRAWN, NO_BOXES, HEBREW, NUMBERS_ONLY], ids=["drawn", "no_boxes", "hebrew", "numbers_only"])
def test_a_table_without_the_three_signs_is_served_as_stored(st):
    from app.extraction import tables

    assert tables.column_order(st) is None
    assert tables.serve_structure(st) is st
    block = "\n".join(" | ".join(r["cells"]) for r in st["rows"])
    assert tables.serve_text(st, block) == block


def test_a_drawn_stored_latin_table_with_ascending_boxes_is_not_flipped_even_though_its_cue_holds():
    from app.extraction import tables

    assert tables.reads_left_to_right([r["cells"] for r in DRAWN["rows"]], False)
    assert tables.column_order(DRAWN) is None


def test_a_hebrew_right_to_left_table_with_descending_boxes_is_not_flipped():
    from app.extraction import tables

    assert tables.column_order(HEBREW) is None
    one_hebrew_cell = _stored([["Annual", "Q2 2024", "Q1 2024", "פריט"], ["300", "200", "100", "הכנסה"]], [300, 220, 140, 60])
    assert tables.column_order(one_hebrew_cell) is None  # a Hebrew letter in any cell: the right-to-left handling stands


def test_years_that_rise_from_left_to_right_are_the_cue_of_a_reversed_numbers_table():
    from app.extraction import tables

    rising = _stored([["2024", "2023", "2022", ""], ["30", "20", "10", ""]], [300, 220, 140, 60])  # drawn 2022 2023 2024
    assert tables.column_order(rising) == [3, 2, 1, 0]
    falling = _stored([["2022", "2023", "2024", ""], ["30", "20", "10", ""]], [300, 220, 140, 60])  # drawn 2024 2023 2022
    assert tables.column_order(falling) is None


def test_a_table_whose_boxes_disagree_row_by_row_is_not_flipped():
    from app.extraction import tables

    mixed = {**REVERSED, "rows": [REVERSED["rows"][0],
                                 {**REVERSED["rows"][1], "cell_boxes": [_box(x) for x in (60, 140, 220, 300)]}]}
    assert tables.column_order(mixed) is None


def test_the_text_of_a_reversed_table_block_and_of_its_row_chunk_is_served_by_line_and_other_lines_stay():
    from app.extraction import tables
    from app.extraction.chunking import render_row

    block = "\n".join([*(" | ".join(r["cells"]) for r in REVERSED["rows"]), "* amounts in thousands"])
    served = tables.serve_text(REVERSED, block).split("\n")
    assert served[0] == "Item | Q1 2024 | Q2 2024 | Annual"
    assert served[1] == "Item: Revenue | Q1 2024: 100 | Q2 2024: 200 | Annual: 300"
    assert served[-1] == "* amounts in thousands"
    stored_chunk = "Quarterly revenue: " + render_row([], REVERSED["rows"][1]["cells"])
    assert stored_chunk == "Quarterly revenue: 300 | 200 | 100 | Revenue"
    assert tables.serve_row_text(REVERSED, 1, stored_chunk) == \
        "Quarterly revenue: Item: Revenue | Q1 2024: 100 | Q2 2024: 200 | Annual: 300"
    assert tables.serve_row_text(REVERSED, 1, "a chunk that is not that row") == "a chunk that is not that row"
    assert tables.serve_row_text(DRAWN, 1, "Revenue | 100 | 200 | 300") == "Revenue | 100 | 200 | 300"


def test_the_count_by_class_separates_reversed_drawn_boxless_and_hebrew_tables():
    from app.extraction import tables

    assert tables.classify_stored(REVERSED) == "reversed_stored"
    assert tables.classify_stored(DRAWN) == "drawn_stored"
    assert tables.classify_stored(NO_BOXES) == "no_boxes"
    assert tables.classify_stored(HEBREW) == "hebrew"
    assert tables.classify_stored(NUMBERS_ONLY) == "other"
    counts = tables.count_stored([REVERSED, REVERSED, DRAWN, NO_BOXES, HEBREW, NUMBERS_ONLY, {}])
    assert counts == {"reversed_stored": 2, "drawn_stored": 1, "no_boxes": 2, "hebrew": 1, "other": 1}


# --- round 22 U4: one serve decision from the structure and the Hebrew context of its pages (KTD4, R7) --------------------------------
#
# Ingestion stored a Latin table on a Hebrew page for right-to-left reading; serving it as "reversed" read it the other way. The
# decision now takes the page context the loader attaches (``PAGE_HEBREW``), and a cell-box backfill cannot change it when the
# context and the cell cue already decide. Every table here is invented.

def _with_page(st: dict, hebrew: bool | None) -> dict:
    return st if hebrew is None else {**st, "page_hebrew": hebrew}  # what the loader attaches (``reader.with_page_context``)


def _without_boxes(st: dict) -> dict:
    return {**{k: v for k, v in st.items() if k != "header_boxes"},
            "rows": [{k: v for k, v in r.items() if k != "cell_boxes"} for r in st["rows"]]}


def _with_boxes(st: dict, xs: list[float]) -> dict:
    return {**st, "rows": [{**r, "cell_boxes": [_box(x) for x in xs]} for r in st["rows"]]}


def test_a_latin_table_on_a_hebrew_page_follows_the_ingestion_direction():
    from app.extraction import tables

    # the page has Hebrew: ingestion read the table for right-to-left reading and stored it that way, so it is served as stored
    on_hebrew_page = _with_page(REVERSED, True)
    assert tables.column_order(on_hebrew_page) is None and tables.serve_structure(on_hebrew_page) is on_hebrew_page
    # a page with no Hebrew, or a page not known: the cue holds and the boxes descend, as before
    assert tables.column_order(_with_page(REVERSED, False)) == [3, 2, 1, 0]
    assert tables.column_order(REVERSED) == [3, 2, 1, 0]


def test_a_hebrew_table_with_descending_boxes_stays_as_stored_whatever_the_page_says():
    from app.extraction import tables

    for hebrew in (None, False, True):
        assert tables.column_order(_with_page(HEBREW, hebrew)) is None
    one_hebrew_cell = _stored([["Annual", "Q2 2024", "Q1 2024", "פריט"], ["300", "200", "100", "הכנסה"]], [300, 220, 140, 60])
    assert tables.column_order(_with_page(one_hebrew_cell, False)) is None


@pytest.mark.parametrize("st,hebrew", [(REVERSED, True), (DRAWN, True), (HEBREW, False), (NUMBERS_ONLY, False), (NUMBERS_ONLY, True)],
                         ids=["latin_on_hebrew_page", "drawn_on_hebrew_page", "hebrew_cells", "no_cue", "no_cue_hebrew_page"])
def test_backfilling_cell_boxes_does_not_change_the_decision_when_the_context_and_the_cue_decide(st, hebrew):
    from app.extraction import tables

    base = _with_page(st, hebrew)
    decided = tables.column_order(base)
    for variant in (_without_boxes(base), _with_boxes(base, [300, 220, 140, 60][:len(base["rows"][0]["cells"])]),
                    _with_boxes(base, [60, 140, 220, 300][:len(base["rows"][0]["cells"])])):
        assert tables.column_order(variant) == decided
        assert tables.serve_direction(variant).inferred is False


def test_a_table_the_boxes_alone_decide_is_marked_inferred_and_keeps_todays_stored_boxes_rule():
    from app.extraction import tables

    assert tables.serve_direction(REVERSED) == tables.Direction(True, "boxes", True)
    assert tables.serve_direction(DRAWN) == tables.Direction(False, "boxes", True)
    assert tables.serve_direction(NO_BOXES) == tables.Direction(False, "unknown", True)
    assert tables.serve_direction(_with_page(REVERSED, True)) == tables.Direction(False, "page", False)
    assert tables.serve_direction(HEBREW) == tables.Direction(False, "cells", False)
    assert tables.serve_direction(NUMBERS_ONLY) == tables.Direction(False, "cue", False)
    served = tables.serve_structure(REVERSED)
    assert served["direction"] == {"reversed": True, "basis": "boxes", "inferred": True}


def test_the_pages_whose_text_gives_a_tables_context_are_the_pages_of_its_rows_and_only_for_a_table_it_can_change():
    from app.extraction import tables

    two_pages = {**REVERSED, "rows": [{**REVERSED["rows"][0], "page": 4}, {**REVERSED["rows"][1], "page": 5}]}
    assert tables.context_pages(two_pages) == [4, 5]
    assert tables.context_pages(HEBREW) == [] and tables.context_pages(_with_page(REVERSED, False)) == []
    assert tables.context_pages(tables.serve_structure(REVERSED)) == []


def test_measurements_and_dates_read_a_cell_by_the_stored_column_the_serve_decision_names():
    """The readers of the stored structure that do not serve it (a date of a header's column, a measurement's re-anchoring row) index a
    cell by its stored column; the serve decision keeps that column as the identity of every served position (``columns``)."""
    from app.chat import dates
    from app.extraction import tables
    from app.measurements.store import _find_row

    # a Hebrew valuation table in logical order: stored column 0 is the rightmost; it is served as stored whatever its page says
    hebrew = {"headers": ["מועד קובע", "שווי"], "units": [], "caption": "ריכוז", "title": [], "notes": [],
              "rows": [{"page": 1, "cells": ["15/04/2025", "9,000"], "cell_boxes": [_box(300), _box(60)]}]}
    for page in (None, False, True):
        st = _with_page(hebrew, page)
        assert tables.serve_structure(st) is st
        assert [d.iso for d, _ in dates._table_dates(st["headers"], st["rows"])] == ["2025-04-15"]
    # a Latin table served reversed: the served position names the stored column the measurement row is found in
    served = tables.serve_structure(REVERSED)
    for position, label in enumerate(["Item", "Q1 2024", "Q2 2024", "Annual"]):
        stored = tables.stored_column(served, position)
        assert served["rows"][0]["cells"][position] == label == REVERSED["rows"][0]["cells"][stored]
        assert served["rows"][1]["cells"][position] == REVERSED["rows"][1]["cells"][stored]
    old = {"caption": "", "headers": [], "row": REVERSED["rows"][1]["cells"]}
    found = _find_row([(0, REVERSED)], old, ["200"], {})
    assert found is not None and found[1] == 1 and found[3][tables.stored_column(served, 2)] == "200"  # the cell «200» is Q2 2024


# --- round 22 U3: a table continues across a page on structural evidence, never on shape alone (KTD3, R6, R9) -----------------------
#
# Three values: "continues" (a repeated header and nothing between the parts), "separate" (a heading or paragraph that is no page
# furniture between them, a note closing the first part, another caption, another section or other column x-geometry) and
# "undecided" (the same shape and nothing between, but no repeated header): undecided keeps the parts apart with a candidate link
# (``TableResult.candidate_of``) that no consumer crosses. Every text here is invented.

def _piece(page, rows, **kw) -> RawTable:
    return RawTable(page=page, rows=rows, **kw)


def test_ae1_an_income_table_and_a_costs_table_with_a_heading_between_are_two_tables():
    income = _piece(1, [HEADERS, _row(1), _row(2)], section="3. הכנסות", caption="להלן טבלת הכנסות (באלפי ₪)")
    costs_no_header = _piece(2, [_row(3), _row(4)], section="4. עלויות", caption="להלן טבלת עלויות", gap=["4. עלויות", "להלן טבלת עלויות"])
    tables = assemble_tables([income, costs_no_header])
    assert len(tables) == 2 and tables[1].section == "4. עלויות" and tables[1].headers == []
    assert tables[1].candidate_of is None  # a heading between is evidence of "separate", not a candidate continuation
    assert costs_no_header.decision == "separate" and not costs_no_header.continues
    costs_same_header = _piece(2, [HEADERS, _row(3)], gap=["4. עלויות"])
    assert len(assemble_tables([_piece(1, [HEADERS, _row(1)]), costs_same_header])) == 2


def test_equal_headers_with_a_caption_change_are_two_tables():
    first = _piece(1, [HEADERS, _row(1)], caption="טבלה 1 - עסקאות במגרש א")
    second = _piece(2, [HEADERS, _row(2)], caption="טבלה 2 - עסקאות במגרש ב")
    tables = assemble_tables([first, second])
    assert len(tables) == 2 and second.break_before == "caption"


def test_a_note_closing_the_first_table_makes_the_next_piece_separate():
    first = _piece(1, [HEADERS, _row(1)], notes=["(*) המחירים כוללים מע״מ."])
    second = _piece(2, [HEADERS, _row(2)])
    assert len(assemble_tables([first, second])) == 2 and second.decision == "separate"
    assert len(assemble_tables([_piece(1, [HEADERS, _row(1)]), _piece(2, [HEADERS, _row(2)])])) == 1


def test_a_section_change_or_other_column_geometry_separates_equal_headers():
    assert len(assemble_tables([_piece(1, [HEADERS, _row(1)], section="3. א"), _piece(2, [HEADERS, _row(2)], section="4. ב")])) == 2
    n = len(HEADERS)
    left = [[10.0 + 50 * c, 100.0, 55.0 + 50 * c, 120.0] for c in range(n)]
    shifted = [[x0 + 90, top, x1 + 90, bottom] for x0, top, x1, bottom in left]  # the same columns drawn 90 points to the right
    a = _piece(1, [HEADERS, _row(1)], boxes=[left, left])
    b = _piece(2, [HEADERS, _row(2)], boxes=[shifted, shifted])
    assert len(assemble_tables([a, b])) == 2 and b.break_before == "geometry"
    c = _piece(2, [HEADERS, _row(2)], boxes=[[[x0 + 2, top, x1 + 2, bottom] for x0, top, x1, bottom in left]] * 2)
    assert len(assemble_tables([_piece(1, [HEADERS, _row(1)], boxes=[left, left]), c])) == 1  # a small drift is one table


def test_a_repeated_header_with_nothing_between_continues():
    second = _piece(2, [HEADERS, _row(2)])
    [t] = assemble_tables([_piece(1, [HEADERS, _row(1)]), second])
    assert second.decision == "continues" and second.continues and t.candidate_of is None
    assert [r.page for r in t.rows] == [1, 2]


def test_a_three_page_table_is_one_table():
    raws = [_piece(1, [HEADERS, _row(1)]), _piece(2, [HEADERS, _row(2)]), _piece(3, [HEADERS, _row(3)])]
    [t] = assemble_tables(raws)
    assert (t.page_start, t.page_end) == (1, 3) and [r.page for r in t.rows] == [1, 2, 3]


def test_a_noisy_header_spelling_with_nothing_between_is_undecided_separate_parts_and_a_candidate_link():
    noisy = [*HEADERS[:-1], "מחיר למ״ר ₪"]
    second = _piece(2, [noisy, _row(2)])
    tables = assemble_tables([_piece(1, [HEADERS, _row(1)]), second])
    assert len(tables) == 2 and second.decision == "undecided" and not second.continues
    assert tables[1].candidate_of == 0 and tables[0].candidate_of is None
    assert tables[0].headers == HEADERS and tables[1].headers == noisy  # the second part keeps its own header


def test_headerless_rows_after_a_hebrew_headed_table_with_no_cell_boxes_are_undecided_not_merged_on_shape():
    second = _piece(2, [_row(2), _row(3)])
    tables = assemble_tables([_piece(1, [HEADERS, _row(1)]), second])
    assert len(tables) == 2 and second.decision == "undecided" and tables[1].candidate_of == 0
    assert tables[1].headers == [] and [r.page for r in tables[0].rows] == [1]  # nothing was taken from, or given to, the other part


# a headerless piece continues on structure: aligned columns (cell boxes on both sides) and the page edges (REGION_SHARE = a third)
PAGE = (50.0, 800.0)  # the content area of a page, top to bottom


def _headerless_pair(first_bottom=780.0, second_top=60.0, shift=0.0, boxes=True, extent=True):
    n = len(HEADERS)
    band = [[10.0 + 50 * c, 100.0, 55.0 + 50 * c, 120.0] for c in range(n)]
    moved = [[x0 + shift, t, x1 + shift, b] for x0, t, x1, b in band]
    a = _piece(1, [HEADERS, _row(1)], bbox=[10.0, first_bottom - 80, 460.0, first_bottom], boxes=[band, band] if boxes else None,
               extent=PAGE if extent else None)
    b = _piece(2, [_row(2), _row(3)], bbox=[10.0 + shift, second_top, 460.0 + shift, second_top + 60],
               boxes=[moved, moved] if boxes else None, extent=PAGE if extent else None)
    return a, b


def test_a_headerless_piece_with_aligned_columns_at_the_page_edges_continues_the_table():
    a, b = _headerless_pair()
    [t] = assemble_tables([a, b])
    assert b.decision == "continues" and b.continues and t.headers == HEADERS and [r.page for r in t.rows] == [1, 2, 2]
    a, b = _headerless_pair(shift=5.0)  # a small drift in the columns is the same band
    assert len(assemble_tables([a, b])) == 1


def test_a_headerless_piece_with_aligned_columns_that_starts_mid_page_after_a_long_gap_stays_undecided():
    a, b = _headerless_pair(second_top=400.0)
    tables = assemble_tables([a, b])
    assert len(tables) == 2 and b.decision == "undecided" and tables[1].candidate_of == 0
    a, b = _headerless_pair(first_bottom=300.0)  # the earlier part ends mid-page
    assert len(assemble_tables([a, b])) == 2 and b.decision == "undecided"


def test_a_headerless_piece_whose_columns_are_not_aligned_is_separate_not_a_continuation():
    a, b = _headerless_pair(shift=90.0)
    tables = assemble_tables([a, b])
    assert len(tables) == 2 and b.decision == "separate" and b.break_before == "geometry" and tables[1].candidate_of is None


def test_a_headerless_piece_without_cell_boxes_or_without_a_page_extent_stays_undecided():
    for kw in ({"boxes": False}, {"extent": False}):
        a, b = _headerless_pair(**kw)
        assert len(assemble_tables([a, b])) == 2 and b.decision == "undecided", kw
    a, b = _headerless_pair()
    a.boxes = None  # one side only
    assert len(assemble_tables([a, b])) == 2 and b.decision == "undecided"


def test_aligned_headerless_columns_do_not_continue_over_a_heading_a_note_or_a_section_change():
    for change in ({"gap": ["4. עלויות"]}, {"section": "4. עלויות"}):
        a, b = _headerless_pair()
        for k, v in change.items():
            setattr(b, k, v)
        assert len(assemble_tables([a, b])) == 2 and b.decision == "separate", change
    a, b = _headerless_pair()
    a.notes = ["(*) הערה"]
    assert len(assemble_tables([a, b])) == 2 and b.decision == "separate"


def test_through_the_walker_a_headerless_table_that_runs_from_the_bottom_of_a_page_to_the_top_of_the_next_is_one_table():
    n = len(HEADERS)

    def boxes(top, rows):
        return [[[10.0 + 50 * c, top + 20 * r, 55.0 + 50 * c, top + 20 * r + 18] for c in range(n)] for r in range(rows)]

    first = RawTable(1, [HEADERS, _row(1), _row(2)], top=700.0, bbox=[10, 700, 460, 760], boxes=boxes(700.0, 3))
    second = RawTable(2, [_row(3), _row(4)], top=40.0, bbox=[10, 40, 460, 80], boxes=boxes(40.0, 2))
    p1, p2 = _out(1, [], [first]), _out(2, [], [second])
    p1.height = p2.height = 800.0
    [table] = _document(p1, p2).tables
    assert table.headers == HEADERS and [r.page for r in table.rows] == [1, 1, 2, 2]
    mid = RawTable(2, [_row(3), _row(4)], top=400.0, bbox=[10, 400, 460, 440], boxes=boxes(400.0, 2))
    p1, p2 = _out(1, [], [RawTable(1, [HEADERS, _row(1), _row(2)], top=700.0, bbox=[10, 700, 460, 760], boxes=boxes(700.0, 3))]), _out(2, [], [mid])
    p1.height = p2.height = 800.0
    assert len(_document(p1, p2).tables) == 2


def test_pieces_of_pages_that_do_not_follow_or_of_another_width_are_separate_without_a_link():
    far = _piece(3, [HEADERS, _row(2)])
    narrow = _piece(2, [["א", "ב"], ["1", "2"]])
    for second in (far, narrow):
        tables = assemble_tables([_piece(1, [HEADERS, _row(1)]), second])
        assert len(tables) == 2 and second.decision == "separate" and tables[1].candidate_of is None


def test_an_empty_piece_between_does_not_hide_what_stood_between_the_parts():
    empty = _piece(2, [["", ""]], gap=["4. עלויות"])
    after = _piece(2, [HEADERS, _row(2)])
    tables = assemble_tables([_piece(1, [HEADERS, _row(1)]), empty, after])
    assert len(tables) == 2 and after.decision == "separate"


# --- page furniture: a text on a large share of the pages at one place is no evidence ---------------------------------------------

def test_furniture_is_a_normalized_text_on_a_large_share_of_the_pages_at_the_same_place():
    from app.extraction import tables

    entries = [(p, 20.0, f"דוח שמאות לדוגמה - עמוד {p}") for p in range(1, 7)] + [(2, 300.0, "4. עלויות"), (3, 20.0, "4. עלויות")]
    fur = tables.furniture_of(entries, pages_total=6)
    assert fur.covers(20.0, "דוח שמאות לדוגמה - עמוד 9") and fur.covers(22.0, "דוח שמאות לדוגמה - עמוד 3")
    assert not fur.covers(300.0, "דוח שמאות לדוגמה - עמוד 3")  # another place on the page
    assert not fur.covers(300.0, "4. עלויות") and not fur.covers(20.0, "4. עלויות")  # two pages are no share
    assert not tables.furniture_of([(1, 20.0, "דוח"), (2, 20.0, "דוח")], pages_total=2).covers(20.0, "דוח")


# --- the real PDF path (pdf.assemble_result) and the re-read of positions share the decision ----------------------------------------

def _line(top, text):
    from app.extraction import pdf

    return pdf._Line(top, text, bottom=top + 10, x0=50, x1=300, size=10)


def _out(no, lines, raws):
    from app.extraction import pdf
    from app.extraction.base import PageResult

    return pdf._PageOut(PageResult(no, "\n".join(ln.text for ln in lines), "text_layer", 1.0, True), lines, raws)


class _NoFix:
    def report(self):
        return {}


def _document(*pages):
    from app.extraction import pdf

    return pdf.assemble_result([list(pages)], len(pages), [], _NoFix(), [])


def _income_costs():
    t1 = RawTable(1, [["תאריך", "מחיר"], ["01/2020", "1,000"], ["02/2020", "1,100"]], top=300.0, bbox=[50, 300, 500, 360])
    t2 = RawTable(2, [["אחזקה", "500"], ["ביטוח", "300"]], top=160.0, bbox=[50, 160, 500, 200])
    p1 = _out(1, [_line(100, "3. הכנסות"), _line(130, "להלן טבלת הכנסות (באלפי ₪)"), _line(370, "(*) הערה להכנסות")], [t1])
    p2 = _out(2, [_line(100, "4. עלויות"), _line(130, "להלן טבלת עלויות בשקלים חדשים בלבד"), _line(210, "(*) הערה לעלויות")], [t2])
    return p1, p2


def test_ae1_through_the_pdf_walker_an_income_and_a_costs_table_stay_two_tables_each_with_its_own_caption_section_and_notes():
    res = _document(*_income_costs())
    assert len(res.tables) == 2
    income, costs = res.tables
    assert (income.section, costs.section) == ("3. הכנסות", "4. עלויות")
    assert "הכנסות" in income.caption and "עלויות" in costs.caption
    assert income.notes == ["(*) הערה להכנסות"] and costs.notes == ["(*) הערה לעלויות"]
    assert [r.page for r in costs.rows] == [2, 2] and costs.headers == [] and income.page_end == 1
    assert sum(1 for b in res.blocks if b.kind == "table") == 2


def test_the_costs_cell_does_not_inherit_the_income_captions_scale_through_take_value():
    from types import SimpleNamespace as NS

    from app.chat import tools

    res = _document(*_income_costs())
    costs = res.tables[1]
    st = {"headers": costs.headers, "units": costs.units, "caption": costs.caption, "title": costs.title, "notes": costs.notes,
          "source": "text", "rows": [{"page": r.page, "cells": r.cells} for r in costs.rows]}
    cell = tools._cell_of(NS(sid="S1", version_id="v", table_index=1), "אחזקה | 500",
                          {"row": "אחזקה", "column_number": 2, "number": "500"}, st, 1)
    assert cell["scale"] == 1 and "באלפי" not in cell["facet_in"]["caption"]


def _running_header_document(second_gets_heading: bool):
    """Four pages, each with the same running header line at the top; a Hebrew-headed table on pages 2 and 3 with the header repeated."""
    pages = []
    for no in range(1, 5):
        lines = [_line(20, "דוח שמאות סינתטי לדוגמה"), _line(60, f"פסקה רגילה בעמוד {no}") if no in (1, 4) else None]
        raws = []
        if no in (2, 3):
            raws = [RawTable(no, [HEADERS, _row(no)], top=120.0, bbox=[50, 120, 500, 180])]
        if no == 3 and second_gets_heading:
            lines.append(_line(90, "4. עלויות"))
        pages.append(_out(no, [ln for ln in lines if ln is not None], raws))
    return _document(*pages)


def test_a_running_page_header_above_a_continuation_with_a_repeated_header_stays_one_table():
    res = _running_header_document(second_gets_heading=False)
    assert len(res.tables) == 1 and (res.tables[0].page_start, res.tables[0].page_end) == (2, 3)


def test_a_real_heading_above_the_continuation_splits_it_even_beside_a_running_header():
    res = _running_header_document(second_gets_heading=True)
    assert len(res.tables) == 2 and res.tables[1].section == "4. עלויות"


def test_the_positions_reread_decides_like_the_walker():
    from app.platform import positions

    p1, p2 = _income_costs()
    rebuilt = positions._text_tables([p1, p2])
    assert [t.section for t in rebuilt] == ["3. הכנסות", "4. עלויות"]
    assert len(positions._text_tables(list(_pages_of_a_continuation()))) == 1


def _pages_of_a_continuation():
    for no in (1, 2):
        yield _out(no, [_line(100, "3. הכנסות")] if no == 1 else [], [RawTable(no, [HEADERS, _row(no)], top=200.0, bbox=[50, 200, 500, 260])])


# --- a stored table whose rows span pages and records no parts (R9): evidence is the stored blocks above the later rows ------------------

def _stored_two_pages(with_boxes: bool = True) -> dict:
    def box(top):
        return [[300.0, top, 400.0, top + 20], [100.0, top, 300.0, top + 20]]

    rows = [{"page": 1, "cells": ["שכירות", "100"]}, {"page": 1, "cells": ["ביטוח", "200"]},
            {"page": 2, "cells": ["אחזקה", "500"]}, {"page": 2, "cells": ["ארנונה", "300"]}]
    if with_boxes:
        for r, top in zip(rows, (500.0, 530.0, 120.0, 150.0), strict=True):
            r["cell_boxes"] = box(top)
    return {"headers": ["סעיף", "סכום"], "units": [None, None], "caption": "להלן טבלת הוצאות (באלפי ₪)", "title": [], "notes": [],
            "rows": rows}


def _block(page, top, text):
    from app.extraction.tables import BlockAt

    return BlockAt(page, top, text)


def test_a_stored_two_page_continuation_with_no_block_above_the_later_rows_applies_to_both_pages():
    from app.extraction import tables

    assert tables.unconfirmed_pages(_stored_two_pages(), []) == {}
    below = [_block(2, 400.0, "פסקה שבאה אחרי השורות")]  # on the later page but under its rows
    assert tables.unconfirmed_pages(_stored_two_pages(), below) == {}
    assert tables.unconfirmed_pages(_stored_two_pages(), [_block(1, 100.0, "פסקה מעל שורות עמוד 1")]) == {}  # above the first page's rows


def test_a_stored_two_page_table_with_a_heading_above_the_later_rows_marks_the_later_page_unconfirmed():
    from app.extraction import tables

    got = tables.unconfirmed_pages(_stored_two_pages(), [_block(2, 60.0, "4. עלויות")])
    assert list(got) == [2] and "4. עלויות" in got[2]
    below_on_the_first_page = tables.unconfirmed_pages(_stored_two_pages(), [_block(1, 700.0, "פסקה מתחת לחלק הראשון")])
    assert list(below_on_the_first_page) == [2]


def test_stored_page_furniture_above_the_later_rows_is_no_evidence_and_a_page_between_is():
    from app.extraction import tables

    furniture = tables.furniture_of([(p, 20.0, "דוח שמאות סינתטי") for p in range(1, 9)], pages_total=8)
    assert tables.unconfirmed_pages(_stored_two_pages(), [_block(2, 20.0, "דוח שמאות סינתטי")], furniture) == {}
    skipped = _stored_two_pages()
    skipped["rows"][2]["page"] = skipped["rows"][3]["page"] = 3  # the rows run from page 1 to page 3
    got = tables.unconfirmed_pages(skipped, [_block(2, 20.0, "פסקה בעמוד שבין השניים")])
    assert list(got) == [3]
    assert tables.unconfirmed_pages(skipped, [_block(2, 20.0, "דוח שמאות סינתטי")], furniture) == {}
    three = _stored_two_pages()
    three["rows"][3]["page"] = 3  # pages 1, 1, 2, 3
    got = tables.unconfirmed_pages(three, [_block(2, 90.0, "פסקה מעל העמוד השני")])
    assert sorted(got) == [2, 3]  # once separated, every later page is unconfirmed (the later part may take its scale from the earlier)


def test_a_stored_table_without_cell_boxes_is_judged_by_the_page_text_then_conservatively():
    from app.extraction import tables

    st = _stored_two_pages(with_boxes=False)
    text = {2: "דוח\n4. עלויות\nאחזקה | 500\nארנונה | 300\nפסקה אחרי הטבלה"}
    above = _block(2, None, "4. עלויות")
    below = _block(2, None, "פסקה אחרי הטבלה")
    assert list(tables.unconfirmed_pages(st, [above], page_text=text.get)) == [2]
    assert tables.unconfirmed_pages(st, [below], page_text=text.get) == {}
    assert list(tables.unconfirmed_pages(st, [below])) == [2]  # nothing says where it stands: conservative
    assert tables.unconfirmed_pages({**st, "rows": st["rows"][:2]}, [above]) == {}  # a one-page table has no later page


def test_a_window_staged_before_round_22_decodes_its_first_on_page_flag_as_no_break_and_a_new_one_round_trips():
    from app.extraction import pdf_windows

    legacy = [1, [["א"]], False, "3. א", True, 12.0, [0, 12, 50, 30], None, False, None, None]  # ``first_on_page`` was a bool here
    assert pdf_windows._dec_table(legacy).break_before is None
    piece = RawTable(page=2, rows=[["א", "ב"]], section="4. ב", top=10.0, bbox=[0, 10, 50, 20])
    assert pdf_windows._dec_table(pdf_windows._enc_table(piece)) == piece
