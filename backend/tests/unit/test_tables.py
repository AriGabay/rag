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


def test_cross_page_continuation_without_repeated_header_keeps_row_pages():
    raw = [
        RawTable(page=3, rows=[HEADERS, _row(1), _row(2)]),
        RawTable(page=4, rows=[_row(3), _row(4)]),
    ]
    tables = assemble_tables(raw)
    assert len(tables) == 1
    t = tables[0]
    assert t.headers == HEADERS
    assert [r.page for r in t.rows] == [3, 3, 4, 4]
    assert [r.cells[0] for r in t.rows] == ["רחוב 1", "רחוב 2", "רחוב 3", "רחוב 4"]
    assert (t.page_start, t.page_end) == (3, 4)


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
