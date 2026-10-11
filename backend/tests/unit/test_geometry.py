"""Positions in the frame of the rendered page (U2, KTD2, R4): the pdfplumber-to-display conversion for every
rotation and an offset CropBox, the pages it refuses, the recovery of a line's character order, word spans, and table
cell boxes through the column reversal, row filtering and continuation. Pure functions; synthetic numbers only."""

from __future__ import annotations

import pytest

from app.extraction import geometry
from app.extraction.geometry import (
    Span,
    display_size,
    logical_order,
    page_geometry,
    to_display,
    word_spans,
)
from app.extraction.hebrew import visual_to_logical
from app.extraction.tables import RawTable, assemble_tables, logical_boxes, logical_row

MEDIA = (36.0, 24.0, 631.0, 866.0)  # an A4-like page whose MediaBox does not start at 0, 0
CROP = (46.0, 32.0, 619.0, 860.0)  # inset 10 / 8 / 12 / 6 from it
GLYPH = (100.0, 700.0, 110.0, 712.0)  # a glyph in user space: x0, y0, x1, y1


def plumber_box(user, mediabox, rotation):
    """Where pdfplumber 0.11 reports a user-space box: pdfminer turns the page by ``/Rotate`` from the MediaBox
    corner (``x'``, ``y'`` up), and pdfplumber adds the rotated MediaBox's left edge to ``x'`` and puts ``top`` at the
    rotated height minus ``y'`` minus the rotated MediaBox's bottom edge."""
    mx0, my0, mx1, my1 = mediabox
    turned = rotation in (90, 270)
    origin_x, origin_y = (my0, mx0) if turned else (mx0, my0)
    height = (mx1 - mx0) if turned else (my1 - my0)

    def turn(x, y):
        return {0: (x - mx0, y - my0), 90: (y - my0, mx1 - x), 180: (mx1 - x, my1 - y), 270: (my1 - y, x - mx0)}[
            rotation]

    pts = [turn(x, y) for x in (user[0], user[2]) for y in (user[1], user[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [min(xs) + origin_x, height - max(ys) - origin_y, max(xs) + origin_x, height - min(ys) - origin_y]


def shown_box(user, crop, rotation):
    """Where a user-space box appears on the rendered page: the CropBox turned clockwise by ``rotation``, origin at
    its top-left corner (the textbook transform, written independently of ``geometry``)."""
    cx0, cy0, cx1, cy1 = crop
    f = {0: lambda x, y: (x - cx0, cy1 - y), 90: lambda x, y: (y - cy0, x - cx0),
         180: lambda x, y: (cx1 - x, y - cy0), 270: lambda x, y: (cy1 - y, cx1 - x)}[rotation]
    pts = [f(x, y) for x in (user[0], user[2]) for y in (user[1], user[3])]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


# --- conversion ----------------------------------------------------------------------------------------------------

def test_rotation_zero_with_the_crop_box_equal_to_the_media_box_keeps_pdfplumber_boxes():
    media = (0.0, 0.0, 595.28, 841.89)
    box = [326.04, 110.57, 552.76, 130.4]
    assert to_display(box, media, media, 0) == [326.0, 110.6, 552.8, 130.4]
    assert display_size(media, None, 0) == (595.28, 841.89)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_every_rotation_with_an_offset_crop_box_lands_where_the_page_is_shown(rotation):
    got = to_display(plumber_box(GLYPH, MEDIA, rotation), MEDIA, CROP, rotation)
    want = shown_box(GLYPH, CROP, rotation)
    assert got == pytest.approx(want, abs=0.06)
    w, h = display_size(MEDIA, CROP, rotation)
    assert (w, h) == ((828.0, 573.0) if rotation in (90, 270) else (573.0, 828.0))


def test_worked_example_for_a_page_turned_ninety_degrees():
    # pdfplumber: x = y, top = x - 2 * mx0 for this page; shown: x = y - cy0, y = x - cx0
    assert plumber_box(GLYPH, MEDIA, 90) == [700.0, 28.0, 712.0, 38.0]
    assert to_display([700.0, 28.0, 712.0, 38.0], MEDIA, CROP, 90) == [668.0, 54.0, 680.0, 64.0]


def test_a_box_outside_the_rendered_page_is_dropped_and_an_overhang_clipped():
    media = (0.0, 0.0, 600.0, 800.0)
    crop = (50.0, 50.0, 550.0, 750.0)
    assert to_display([10.0, 10.0, 40.0, 20.0], media, crop, 0) is None  # in the margin the crop cuts away
    assert to_display([45.0, 100.0, 80.0, 110.0], media, crop, 0) == [0.0, 50.0, 30.0, 60.0]


@pytest.mark.parametrize(
    ("kwargs", "issue"),
    [
        ({"mediabox": MEDIA, "cropbox": CROP, "rotation": 45}, geometry.ISSUE_ROTATION),
        ({"mediabox": MEDIA, "cropbox": (700.0, 900.0, 800.0, 1000.0), "rotation": 0}, geometry.ISSUE_CROP),
        ({"mediabox": None, "cropbox": CROP, "rotation": 0}, geometry.ISSUE_BOX),
        ({"mediabox": MEDIA, "cropbox": CROP, "rotation": 0, "rendered_size": (612.0, 792.0)}, geometry.ISSUE_SIZE),
        ({"mediabox": MEDIA, "cropbox": CROP, "rotation": 0, "rendered_size": (573.0, 828.0),
          "rendered_crop": (0.0, 0.0, 573.0, 828.0)}, geometry.ISSUE_FRAME),
        ({"mediabox": MEDIA, "cropbox": CROP, "rotation": 90, "rendered_rotation": 0}, geometry.ISSUE_FRAME),
    ],
)
def test_a_page_whose_positions_cannot_be_converted_records_why(kwargs, issue):
    geom = page_geometry(**kwargs)
    assert geom.issue == issue
    assert geometry.geometry_box([100.0, 100.0, 110.0, 110.0], geom) is None


def test_a_convertible_page_records_its_boxes_rotation_and_shown_size():
    geom = page_geometry(MEDIA, CROP, 90, rendered_size=(828.0, 573.0), rendered_crop=CROP, rendered_rotation=90)
    assert (geom.mediabox, geom.cropbox, geom.rotation, geom.width, geom.height, geom.issue) == (
        list(MEDIA), list(CROP), 90, 828.0, 573.0, None)
    assert geometry.geometry_box([700.0, 28.0, 712.0, 38.0], geom) == [668.0, 54.0, 680.0, 64.0]


# --- the order of a line's characters --------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "שטח הדירה הוא 88 מ״ר ושוויה 1,760,000 ₪ לפי ההערכה.",  # numbers stay left to right
        "שטח (מ״ר) 12.5%",  # brackets mirrored back
        "תל אביב Tel Aviv",  # a Latin run kept whole
    ],
)
def test_logical_order_maps_every_character_to_the_one_it_came_from(text):
    raw = visual_to_logical(text)  # the order a visual producer draws it in (the fix is its own inverse)
    assert raw != text and visual_to_logical(raw) == text
    order = logical_order(raw, text)
    assert order is not None and sorted(order) == list(range(len(raw)))
    got = "".join(raw[k] for k in order)
    assert got == text or got.translate(str.maketrans("()", ")(")) == text
    assert logical_order(text, text) == list(range(len(text)))  # a logical producer: nothing moves


def test_a_line_whose_text_is_not_a_reordering_of_its_layer_text_gets_no_order():
    assert logical_order("abc", "abd") is None
    assert logical_order("abc", "ab") is None


def _glyphs(raw: str, top: float = 10.0) -> list:
    """One 5pt-wide box per character, left to right as the text layer drew them; spaces have none."""
    return [None if ch == " " else [100.0 + 5 * k, top, 105.0 + 5 * k, top + 10] for k, ch in enumerate(raw)]


def test_word_spans_of_a_right_to_left_line_with_an_embedded_number_select_each_word_and_its_glyphs():
    text = "ושוויה 1,760,000 ₪ לפי ההערכה"
    raw = visual_to_logical(text)
    assert raw.startswith("הכרעהה")  # drawn from the left: the last word first
    spans = [Span.from_json(s) for s in word_spans([(text, raw, _glyphs(raw))])]
    assert [text[s.start:s.end] for s in spans] == text.split()
    number = spans[1]
    first = raw.index("1,760,000")
    assert number.box == [100.0 + 5 * first, 10.0, 105.0 + 5 * (first + 8), 20.0]  # its own glyphs, left to right
    assert spans[0].box[0] > number.box[2]  # the first Hebrew word is drawn right of the number
    assert [(s.line, s.word) for s in spans] == [(0, k) for k in range(5)]


def test_word_spans_index_the_block_text_across_lines_and_skip_lines_without_glyphs():
    first, second, third = "שטח הדירה", "88 מ״ר", "בקומה השנייה"
    padded = f"  {first} "  # the text layer kept spaces around it; the block text is stripped
    lines = [(padded, padded, _glyphs(padded)), (second, second, None), (third, third, _glyphs(third, 30.0))]
    text = "\n".join(t.strip() for t, _, _ in lines)
    spans = [Span.from_json(s) for s in word_spans(lines)]
    assert [(s.line, s.word, text[s.start:s.end]) for s in spans] == [
        (0, 0, "שטח"), (0, 1, "הדירה"), (2, 0, "בקומה"), (2, 1, "השנייה")]
    assert word_spans([(second, second, None)]) is None


# --- table cell boxes ------------------------------------------------------------------------------------------------

HEADERS = ["כתובת", "שטח (מ״ר)", "מחיר (₪)"]


def _box(row: int, col: int, page: int = 1) -> list[float]:
    return [float(col), float(row), float(col) + 0.5, float(row) + 0.5 + page / 10]


def test_cell_boxes_follow_the_column_reversal_of_their_cells():
    cells_ltr = ["1,640,000", "82", "3 הנאתה"]  # visual text, columns left to right
    boxes_ltr = [[300.0, 0, 400.0, 10], [200.0, 0, 300.0, 10], [100.0, 0, 200.0, 10]]
    assert logical_row(cells_ltr) == ["התאנה 3", "82", "1,640,000"]
    assert logical_boxes(boxes_ltr) == [boxes_ltr[2], boxes_ltr[1], boxes_ltr[0]]
    assert logical_boxes(None) == []


def test_cell_boxes_survive_empty_rows_padding_and_a_continuation_page():
    page1 = [HEADERS, ["התאנה 3", "82", "1,640,000"], ["", " ", ""], ["התאנה 9", "95"]]
    page2 = [["", "", ""], ["הרימון 4", "77", "1,520,000"]]
    raws = [
        RawTable(page=1, rows=page1, boxes=[[_box(r, c) for c in range(len(row))] for r, row in enumerate(page1)]),
        RawTable(page=2, rows=page2, boxes=[[_box(r, c, 2) for c in range(3)] for r in range(2)]),
    ]
    raws[1].boxes[1][1] = None  # a merged cell has no box of its own
    [table] = assemble_tables(raws)
    assert [r.cells for r in table.rows] == [["התאנה 3", "82", "1,640,000"], ["התאנה 9", "95", ""],
                                              ["הרימון 4", "77", "1,520,000"]]
    assert table.header_boxes == [_box(0, c) for c in range(3)]
    assert [r.cell_boxes for r in table.rows] == [
        [_box(1, 0), _box(1, 1), _box(1, 2)],
        [_box(3, 0), _box(3, 1), None],  # padded like its cells
        [_box(1, 0, 2), None, _box(1, 2, 2)],
    ]
    assert [r.page for r in table.rows] == [1, 1, 2]


def test_a_repeated_header_on_the_continuation_page_drops_its_boxes_with_it():
    page2 = [HEADERS, ["הרימון 4", "77", "1,520,000"]]
    raws = [RawTable(page=1, rows=[HEADERS, ["התאנה 3", "82", "1,640,000"]],
                     boxes=[[_box(r, c) for c in range(3)] for r in range(2)]),
            RawTable(page=2, rows=page2, boxes=[[_box(r, c, 2) for c in range(3)] for r in range(2)])]
    [table] = assemble_tables(raws)
    assert [r.cell_boxes for r in table.rows] == [[_box(1, c) for c in range(3)], [_box(1, c, 2) for c in range(3)]]


def test_tables_without_positions_or_with_misaligned_boxes_keep_none():
    ocr = assemble_tables([RawTable(page=1, rows=[HEADERS, ["התאנה 3", "82", "1,640,000"]], ocr=True)])[0]
    assert ocr.header_boxes is None and ocr.rows[0].cell_boxes is None
    bad = assemble_tables([RawTable(page=1, rows=[HEADERS, ["התאנה 3", "82", "1,640,000"]], boxes=[[_box(0, 0)]])])[0]
    assert bad.header_boxes is None and bad.rows[0].cell_boxes is None
