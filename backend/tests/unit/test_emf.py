"""EMF pictures: text runs, grid and table reconstruction (synthetic metafiles, no real documents)."""

from __future__ import annotations

from app.extraction.emf import cell_text, parse, read_emf
from app.extraction.images import read_picture
from tests.support.emf_builder import emf as _emf
from tests.support.emf_builder import grid
from tests.support.emf_builder import line as _line
from tests.support.emf_builder import text as _text


def test_hebrew_runs_are_read_right_to_left_and_numbers_left_to_right():
    # visual order, left to right: "עירייה" ", " "21" "השקמים " — logical: "השקמים 21, עירייה"
    mf = parse(_emf(_text(390, 50, "עירייה", 6), _text(427, 50, ", ", 4), _text(435, 50, "21", 7),
                    _text(450, 50, "השקמים ", 6)))
    assert cell_text(mf.runs) == "השקמים 21, עירייה"


def test_currency_and_number_runs_keep_their_order():
    mf = parse(_emf(_text(100, 50, "₪ 17,500")))
    assert cell_text(mf.runs) == "₪ 17,500"


def test_parenthesised_note_marks_and_gershayim_join_without_spaces():
    # "(במ"ר)" drawn as five runs: ")" "ר" '"' "במ" "("  (visual, left to right)
    mf = parse(_emf(_text(100, 50, ")", 4), _text(104, 50, "ר", 7), _text(111, 50, '"', 3), _text(114, 50, "במ", 7),
                    _text(128, 50, "(", 4), _text(140, 50, "שטח ", 7)))
    assert cell_text(mf.runs) == 'שטח (במ"ר)'


def _table() -> bytes:
    xs = [0, 100, 200, 300]
    ys = [0, 30, 60, 90, 120]
    recs = grid(xs, ys)
    # header (columns right to left: כתובת | שטח | שכ"ד למ"ר), then three rows
    recs += [_text(230, 8, "כתובת"), _text(130, 8, "שטח"), _text(10, 8, 'שכ"ד למ"ר', 7)]
    rows = [("הגפן 3", "120", "₪ 55"), ("הזית 7", "80", "₪ 61"), ("התאנה 1", "95", "₪ 58")]
    for i, (addr, area, rent) in enumerate(rows):
        y = 38 + 30 * i
        recs += [_text(230, y, addr), _text(150, y, area), _text(20, y, rent)]
    recs += [_text(20, 130, "(*) הנתונים לחודש")]  # a note under the grid
    return _emf(*recs)


def test_bordered_table_keeps_header_rows_and_notes():
    content = read_emf(_table())
    assert content.has_text and len(content.tables) == 1
    t = content.tables[0]
    assert t.headers == ["כתובת", "שטח", 'שכ"ד למ"ר']
    assert t.rows == [["הגפן 3", "120", "₪ 55"], ["הזית 7", "80", "₪ 61"], ["התאנה 1", "95", "₪ 58"]]
    assert t.notes == ["(*) הנתונים לחודש"]


def test_vertically_merged_cell_value_goes_to_every_row_of_its_region():
    recs = [_line(x, 0, x, 90) for x in (0, 100, 200)]
    recs += [_line(0, 0, 200, 0), _line(0, 30, 200, 30), _line(0, 90, 200, 90)]
    recs += [_line(0, 60, 100, 60)]  # the row border at 60 crosses only the left column
    recs += [_text(130, 8, "גוש"), _text(30, 8, "מחיר")]
    recs += [_text(130, 52, "6801")]  # one block value, centered over two rows
    recs += [_text(20, 38, "₪ 900"), _text(20, 68, "₪ 950")]
    t = read_emf(_emf(*recs)).tables[0]
    assert t.headers == ["גוש", "מחיר"]
    assert t.rows == [["6801", "₪ 900"], ["6801", "₪ 950"]]


def test_metafile_without_text_or_bitmap_is_a_picture_without_text():
    reading = read_picture(_emf(*grid([0, 50], [0, 50])), "emf", "", None, "heb+eng")
    assert reading.status == "no_text"


def test_malformed_metafile_reads_as_nothing_and_never_raises():
    assert not read_emf(b"\x01\x00\x00\x00garbage").has_text
    assert not read_emf(b"").has_text


def test_a_label_spanning_columns_without_a_border_is_one_cell():
    ys = [0, 30, 60]
    recs = [_line(x, 0, x, 60) for x in (0, 300)]
    recs += [_line(100, 0, 100, 60), _line(200, 0, 200, 30)]  # no border between the two right columns in row 2
    recs += [_line(0, y, 300, y) for y in ys]
    recs += [_text(230, 8, "גישה"), _text(130, 8, "שווי"), _text(30, 8, "משקל")]
    # "סה"כ שווי משוקלל" drawn across the two right columns, as two runs on either side of x=200
    recs += [_text(205, 38, 'סה"', 6), _text(150, 38, "כ שווי משוקלל", 4), _text(20, 38, "₪ 900")]
    t = read_emf(_emf(*recs)).tables[0]
    assert t.rows == [['סה"כ שווי משוקלל', "", "₪ 900"]]


def test_aligned_lines_without_row_borders_are_separate_rows():
    xs, ys = [0, 100, 200], [0, 30, 120]
    recs = grid(xs, ys)
    recs += [_text(130, 8, "רכיב"), _text(30, 8, "סכום")]
    for i, (label, amount) in enumerate([("שווי", "₪ 900"), ("הפחתה", "(₪ 90)"), ("יתרה", "₪ 810")]):
        recs += [_text(130, 38 + 25 * i, label), _text(30, 38 + 25 * i, amount)]
    t = read_emf(_emf(*recs)).tables[0]
    assert t.rows == [["שווי", "₪ 900"], ["הפחתה", "(₪ 90)"], ["יתרה", "₪ 810"]]
