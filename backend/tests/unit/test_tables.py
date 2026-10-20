"""Table assembly: header detection, column reversal, cross-page continuation (U5, R4)."""

from __future__ import annotations

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
