"""Source anchors (U4, KTD1, KTD4): stubs born in the tools, snapshots built from stored data only.

Database-free: the stored reading a snapshot is built from is given as ``anchors.Reading`` (what ``anchors.load``
reads in the answer's transaction). Synthetic texts and numbers only."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from decimal import Decimal

import pytest

from app.chat import anchors as A

DOC, VER = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
W, H = 600.0, 800.0
PARAGRAPH = "השווי הכולל נקבע ל-12,450,000 ₪ לפי הטבלה."
TABLE = {"headers": ["רכיב", "הכנסות (₪)", "עלויות (₪)"], "units": [None, "₪", "₪"], "caption": "סיכום הכנסות",
         "title": [], "notes": ["הסכומים ללא מע\"מ."], "section": "5. תחשיב", "source": None, "block_index": 1,
         "header_boxes": [[400, 100, 500, 120], [250, 100, 400, 120], [100, 100, 250, 120]],
         "rows": [{"page": 3, "cells": ["שלב א", "5,200,000", "4,300,000"],
                   "cell_boxes": [[400, 120, 500, 140], [250, 120, 400, 140], [100, 120, 250, 140]]},
                  {"page": 3, "cells": ["סה\"כ", "12,450,000", "10,400,000"],
                   "cell_boxes": [[400, 140, 500, 160], [250, 140, 400, 160], [100, 140, 250, 160]]}]}


def page(n: int, **kw) -> A.Page:
    base = {"width": W, "height": H, "mediabox": [0, 0, W, H], "cropbox": [0, 0, W, H], "rotation": 0}
    return A.Page(n, **(base | kw))


def spans_of(text_: str, top: float) -> list[list]:
    """One word span per word of a one-line block, laid out right to left at ``top``."""
    out, x = [], 560.0
    pos = 0
    for wi, word in enumerate(text_.split(" ")):
        start = text_.index(word, pos)
        out.append([0, wi, start, start + len(word), x - 10 * len(word), top, x, top + 12])
        x -= 10 * len(word) + 5
        pos = start + len(word)
    return out


def reading(*, reading_id: str = "r1", is_pdf: bool = True, blocks=None, tables=None, pages=None,
            title: str = "שומה שדרות האלון 3", filename: str = "f.pdf") -> A.Reading:
    blocks = blocks if blocks is not None else [
        A.Block(0, "paragraph", 3, PARAGRAPH, bbox=[50, 40, 560, 60], spans=spans_of(PARAGRAPH, 40),
                section_path=("5. תחשיב",), paragraph_no=12),
        A.Block(1, "table", 3, "", bbox=[100, 100, 500, 160], table_index=0, section_path=("5. תחשיב",))]
    return A.Reading(DOC, VER, title, filename, is_pdf, reading_id,
                     pages={p.page_no: p for p in (pages or [page(3, printed_label="12")])},
                     blocks={b.index: b for b in blocks},
                     tables=tables if tables is not None else {0: TABLE}, table_blocks={0: 1})


def stub(**kw) -> dict:
    return {"document_id": DOC, "version_id": VER, "reading_id": "r1", "block_start": None, "block_end": None,
            "pages": [3]} | kw


def frac(box) -> list[float]:
    return [round(box[0] / W, 4), round(box[1] / H, 4), round(box[2] / W, 4), round(box[3] / H, 4)]


# --- KTD4: a quote is located within the cited range, never by first occurrence --------------------------------

def test_a_quote_is_located_once_with_its_words_and_its_number():
    blocks = [(0, "פתיחה"), (1, PARAGRAPH)]
    found = A.locate_quote(blocks, "נקבע ל-12,450,000 ₪", Decimal("12450000"))
    (seg,) = found.segments
    assert seg[0] == 1 and PARAGRAPH[seg[1]:seg[2]] == "נקבע ל-12,450,000 ₪"
    assert PARAGRAPH[found.number[1]:found.number[2]] == "12,450,000" and found.blocks == [1]


def test_a_quote_twice_in_the_range_with_different_numbers_is_ambiguous():
    blocks = [(0, "השווי הוא 12,000 ₪ לשלב א."), (1, "השווי הוא 15,000 ₪ לשלב ב.")]
    with pytest.raises(A.AmbiguousQuote):
        A.locate_quote(blocks, "השווי הוא 1", Decimal("1"))


def test_a_quote_twice_with_the_same_number_is_located_at_block_precision():
    blocks = [(0, "השווי הוא 9,500 ₪ למ\"ר."), (1, "ביניים"), (2, "כאמור, השווי הוא 9,500 ₪ למ\"ר.")]
    found = A.locate_quote(blocks, "השווי הוא 9,500 ₪", Decimal("9500"))
    assert found.segments == [] and found.number is None and found.blocks == [0, 2]


def test_a_quote_across_two_blocks_keeps_a_segment_in_each_and_spelling_marks_do_not_matter():
    blocks = [(0, "השטח ברוטו"), (1, "135 מ״ר בקומה")]
    found = A.locate_quote(blocks, 'השטח ברוטו 135 מ"ר', Decimal("135"))
    assert [s[0] for s in found.segments] == [0, 1] and found.number[0] == 1
    assert "135 מ״ר" == blocks[1][1][found.segments[1][1]:found.segments[1][2]]


def test_a_quote_not_in_the_blocks_is_not_located():
    assert A.locate_quote([(0, "טקסט אחר")], "השווי הוא 9,500", Decimal("9500")) is None


# --- AE1: a number in a paragraph and in a table cell; the value taken from the cell anchors to the cell ---------

def test_ae1_a_value_taken_from_a_cell_anchors_to_that_cells_box_not_the_paragraph():
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, table_index=0, row=1, column=1), reading(), "r1")
    assert snap["precision"] == "cell" and snap["degraded"] is None
    (p,) = snap["pages"]
    assert p["page"] == 3 and p["rects"] == [frac([250, 140, 400, 160])]
    assert frac([50, 40, 560, 60]) not in p["rects"]
    t = snap["table"]
    assert (t["title"], t["row_label"], t["column_header"], t["unit_note"]) == ("סיכום הכנסות", "סה\"כ", "הכנסות (₪)",
                                                                                 "₪")
    assert (t["row_number"], t["column_number"]) == (2, 2)
    assert t["header"] == {"page": 3, "rects": [frac([250, 100, 400, 120])]}
    assert t["notes"] == [{"text": "הסכומים ללא מע\"מ.", "page": 3}]
    assert snap["reading_id"] == "r1" and snap["version_id"] == VER and snap["document_id"] == DOC


def test_a_merged_cell_without_a_box_is_highlighted_at_table_level_and_labelled():
    st = json.loads(json.dumps(TABLE))
    st["rows"][1]["cell_boxes"][1] = None
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, table_index=0, row=1, column=1),
                      reading(tables={0: st}), "r1")
    assert snap["precision"] == "region" and snap["region"] == "table" and snap["degraded"] == A.NO_CELL_BOX
    assert snap["pages"][0]["rects"] == [frac([100, 100, 500, 160])]
    assert snap["precision_label"] == A.PRECISION_LABELS["region_table"]


def test_an_image_table_value_is_a_region_with_the_table_level_label():
    st = {k: v for k, v in TABLE.items() if k != "header_boxes"} | {"source": "vision", "rows": [
        {"page": 3, "cells": r["cells"]} for r in TABLE["rows"]]}
    blocks = [A.Block(1, "image", 3, "", bbox=[80, 90, 520, 300], table_index=0)]
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, table_index=0, row=0, column=2),
                      reading(blocks=blocks, tables={0: st}), "r1")
    assert snap["precision"] == "region" and snap["region"] == "table"
    assert snap["pages"][0]["rects"] == [frac([80, 90, 520, 300])]
    assert snap["table"]["source"] == "vision" and snap["table"]["header"] is None
    assert "טבלה" in snap["precision_label"]


def test_a_table_row_search_hit_highlights_its_row():
    snap = A.snapshot(stub(kind="source", block_start=1, block_end=1, table_index=0, row=0), reading(), "r1")
    assert snap["precision"] == "region" and snap["region"] == "row"
    assert snap["pages"][0]["rects"] == [frac([100, 120, 500, 140])]
    assert snap["table"]["row_label"] == "שלב א" and snap["table"]["column_header"] is None


# --- spans, blocks and pages ------------------------------------------------------------------------------------

def test_a_quote_value_highlights_its_words_and_focuses_its_number():
    found = A.locate_quote([(0, PARAGRAPH)], "נקבע ל-12,450,000 ₪", Decimal("12450000"))
    snap = A.snapshot(stub(kind="quote", block_start=0, block_end=0, segments=found.segments, number=found.number),
                      reading(), "r1")
    assert snap["precision"] == "span"
    (p,) = snap["pages"]
    assert len(p["rects"]) == 3 and len(p["focus"]) == 1
    assert all(0 <= v <= 1 for r in p["rects"] for v in r)
    assert snap["location"]["printed_page"] == "12" and "בדפוס 12" in snap["location"]["label"]
    assert snap["location"]["label"].startswith("עמוד 3")


def test_a_source_spanning_two_pages_stacks_both_with_their_block_boxes():
    blocks = [A.Block(4, "paragraph", 3, "סוף עמוד", bbox=[50, 700, 560, 760]),
              A.Block(5, "paragraph", 4, "תחילת עמוד", bbox=[50, 40, 560, 80])]
    snap = A.snapshot(stub(kind="source", block_start=4, block_end=5, pages=[3, 4]),
                      reading(blocks=blocks, pages=[page(3), page(4)]), "r1")
    assert snap["precision"] == "block" and [p["page"] for p in snap["pages"]] == [3, 4]
    assert snap["pages"][1]["rects"] == [frac([50, 40, 560, 80])]
    assert snap["location"]["page"] == 3 and snap["location"]["page_end"] == 4
    assert snap["location"]["label"].startswith("עמודים 3–4")


def test_a_page_whose_positions_cannot_be_converted_falls_back_to_page_precision():
    snap = A.snapshot(stub(kind="source", block_start=0, block_end=0),
                      reading(pages=[page(3, issue="rotation_unsupported")]), "r1")
    assert snap["precision"] == "page" and snap["degraded"] == A.NO_GEOMETRY
    assert snap["pages"] == [{"page": 3, "printed_label": None, "width": W, "height": H, "rects": [], "focus": []}]


def test_a_rotated_cropped_page_converts_the_block_box_into_the_rendered_frame():
    # a 90-degree page whose CropBox is offset: the stored pdfplumber box is moved by the CropBox corner
    rotated = page(3, width=700.0, height=500.0, mediabox=[0, 0, 600, 800], cropbox=[50, 50, 550, 750],
                   rotation=90)
    blocks = [A.Block(0, "paragraph", 3, "שורה", bbox=[100, 100, 200, 120])]
    snap = A.snapshot(stub(kind="source", block_start=0, block_end=0), reading(blocks=blocks, pages=[rotated]), "r1")
    (rect,) = snap["pages"][0]["rects"]
    assert snap["precision"] == "block" and rect == [round(50 / 700, 4), round(50 / 500, 4), round(150 / 700, 4),
                                                       round(70 / 500, 4)]


# --- KTD1: a reprocess between the tool call and storing the answer ----------------------------------------------

def test_a_reading_changed_before_storing_keeps_page_precision_and_the_pinned_reading():
    new = reading(reading_id="r2")
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, table_index=0, row=1, column=1), new, "r1")
    assert snap["precision"] == "page" and snap["degraded"] == A.READING_CHANGED and snap["reading_id"] == "r1"
    assert all(p["rects"] == [] and p["focus"] == [] for p in snap["pages"]) and snap["pages"][0]["page"] == 3
    assert snap["table"] is None  # the new reading's table is other content: nothing of it is used


def test_a_stub_of_an_earlier_reading_than_the_pinned_one_is_page_precision():
    snap = A.snapshot(stub(kind="source", block_start=0, block_end=0, reading_id="r0"), reading(), "r1")
    assert snap["precision"] == "page" and snap["degraded"] == A.READING_CHANGED and snap["reading_id"] == "r1"


def test_a_version_no_longer_visible_is_page_precision_and_unavailable():
    snap = A.snapshot(stub(kind="source", block_start=0, block_end=0), None, "r1")
    assert snap["precision"] == "page" and snap["degraded"] == A.UNAVAILABLE


# --- DOCX: structured precision, no invented page ---------------------------------------------------------------

def _docx() -> A.Reading:
    blocks = [A.Block(7, "paragraph", None, "שטח הדירה 120 מ\"ר אקוו'.", section_path=("3. הנכס", "3.2 שטחים"),
                      paragraph_no=14),
              A.Block(8, "table", None, "", table_index=0, section_path=("3. הנכס",))]
    st = {k: v for k, v in TABLE.items() if k != "header_boxes"} | {
        "rows": [{"page": None, "cells": r["cells"]} for r in TABLE["rows"]]}
    return reading(is_pdf=False, blocks=blocks, tables={0: st}, pages=[], filename="f.docx")


def test_a_docx_paragraph_is_structured_with_section_and_paragraph_number_and_no_page():
    found = A.locate_quote([(7, _docx().blocks[7].text)], "120 מ\"ר", Decimal("120"))
    snap = A.snapshot(stub(kind="quote", block_start=7, block_end=7, pages=[], segments=found.segments,
                           number=found.number), _docx(), "r1")
    assert snap["precision"] == "structured" and snap["pages"] == []
    s = snap["structured"]
    assert s["section_path"] == ["3. הנכס", "3.2 שטחים"] and s["paragraph_no"] == 14
    assert s["text"][s["highlight"][0]:s["highlight"][1]] == "120 מ\"ר"
    assert not re.search(r"(?<![א-ת])עמוד(?![א-ת])", snap["location"]["label"]) and "פסקה 14" in snap["location"]["label"]
    assert snap["precision_label"] == "מבנה המסמך (ללא עמודים)"


def test_a_docx_cell_is_structured_with_the_normalized_cell():
    snap = A.snapshot(stub(kind="cell", block_start=8, block_end=8, pages=[], table_index=0, row=1, column=2),
                      _docx(), "r1")
    assert snap["precision"] == "structured" and snap["pages"] == []
    assert snap["structured"]["cell"] == {"table_index": 0, "row_number": 2, "column_number": 3, "row_label": "סה\"כ",
                                          "column_header": "עלויות (₪)", "text": "10,400,000"}
    assert snap["table"]["header"] is None


def test_a_stale_docx_anchor_keeps_the_cited_text_and_section():
    snap = A.snapshot(stub(kind="source", block_start=7, block_end=7, pages=[], section="3.2 שטחים"),
                      replace(_docx(), reading_id="r2"), "r1", "שטח הדירה 120 מ\"ר")
    assert snap["precision"] == "structured" and snap["degraded"] == A.READING_CHANGED
    assert snap["structured"]["text"] == "שטח הדירה 120 מ\"ר" and snap["location"]["section"] == "3.2 שטחים"


# --- measurements --------------------------------------------------------------------------------------------

def test_a_measurement_in_a_table_row_anchors_to_the_one_cell_holding_its_value():
    snap = A.snapshot(stub(kind="measurement", block_start=1, block_end=1, table_index=0, row=1, value="10400000",
                           pages=[]), reading(), "r1")
    assert snap["precision"] == "cell" and snap["pages"][0]["rects"] == [frac([100, 140, 250, 160])]


def test_a_measurement_whose_place_was_lost_is_page_precision():
    snap = A.snapshot(stub(kind="measurement", block_start=0, block_end=0, anchor_lost=True, pages=[]), reading(),
                      "r1")
    assert snap["precision"] == "page" and snap["degraded"] == A.ANCHOR_LOST and snap["location"]["label"] == "המסמך"


# --- readable location, bounded size, computed results -----------------------------------------------------------

@pytest.mark.parametrize("title, filename, shown", [
    ("שומה_שדרות_האלון.pdf", "x.pdf", "שומה שדרות האלון"),
    ("×©×•×ž×”.pdf", "שומה מקרקעין.pdf", "שומה מקרקעין"),
    ("3f2a9c1e-77aa-4b1c-9d2e-0a1b2c3d4e5f", "a1b2c3d4e5f60718.pdf", None),
    ("דו\"ח שמאי", None, "דו\"ח שמאי"),
])
def test_the_title_shown_is_readable_and_never_a_garbled_name_or_an_id(title, filename, shown):
    assert A.readable_title(title, filename) == shown


def test_the_location_label_never_carries_technical_ids():
    snap = A.snapshot(stub(kind="cell", block_start=1, block_end=1, table_index=0, row=1, column=1), reading(), "r1")
    label = snap["location"]["label"]
    assert DOC not in label and VER not in label and "r1" not in label
    assert "טבלה «סיכום הכנסות»" in label and "שורה «סה\"כ»" in label and "עמודה «הכנסות (₪)»" in label


def test_a_snapshot_is_bounded_in_pages_rects_and_size():
    blocks = [A.Block(i, "paragraph", 1 + i // 30, "מילה " * 200, bbox=[50, 10 + (i % 30) * 25, 560, 30 + (i % 30) * 25])
              for i in range(300)]
    pages = [page(n) for n in range(1, 12)]
    snap = A.bound(A.snapshot(stub(kind="source", block_start=0, block_end=299, pages=list(range(1, 11))),
                              reading(blocks=blocks, pages=pages), "r1"))
    assert len(snap["pages"]) <= A.MAX_PAGES and snap["truncated"] is True
    assert all(len(p["rects"]) <= A.MAX_RECTS for p in snap["pages"])
    assert len(json.dumps(snap, ensure_ascii=False)) <= A.MAX_CHARS


def test_a_computed_result_anchors_to_its_inputs_never_to_a_place():
    c = {"id": "C2", "inputs": [{"id": "C1"}, {"id": "V1"}, "A1"]}
    anchor = A.computed_anchor(c)
    assert anchor["precision"] == "computed" and anchor["inputs"] == ["C1", "V1", "A1"]
    assert "pages" not in anchor and "document_id" not in anchor


def test_anchored_documents_name_every_document_an_anchor_points_at():
    answer = {"sources": [{"id": "S1", "anchor": {"document_id": "d1"}}],
              "values": [{"id": "V1", "anchor": {"document_id": "d2"}}],
              "measurements": [{"id": "M1", "anchor": None}], "computations": [{"id": "C1", "anchor": {}}]}
    assert A.anchored_documents(answer) == {"d1", "d2"}
    assert A.anchored_documents({"sources": [{"id": "S1"}]}) == set()  # an old answer has none


def test_the_answer_documents_include_anchored_documents_and_old_answers_still_work():
    from app.chat.api import _answer_documents

    old = {"sources": [{"id": "S1", "document_id": "d1"}], "values": [], "measurements": []}
    assert _answer_documents(old) == {"d1"}
    new = old | {"values": [{"id": "V1", "document_id": "d1", "anchor": {"document_id": "d3"}}]}
    assert _answer_documents(new) == {"d1", "d3"}


# --- the tools: stubs born where the value is taken ------------------------------------------------------------

def _source(**kw):
    from uuid import UUID

    from app.chat.tools import Source

    return Source(sid="S1", document_id=UUID(DOC), version_id=UUID(VER), title="t", section=None, location="",
                  kind="context", **kw)


def test_take_quote_asks_for_a_longer_quote_when_it_occurs_twice_with_different_numbers():
    from app.chat.tools import ToolError, _take_quote

    full = "השווי הוא 12,000 ₪ לשלב א.\nהשווי הוא 15,000 ₪ לשלב ב."
    src = _source(text=full, block_start=0, block_end=1, reading_id="r1")
    blocks = [(0, "השווי הוא 12,000 ₪ לשלב א.", 3), (1, "השווי הוא 15,000 ₪ לשלב ב.", 3)]
    with pytest.raises(ToolError, match="ציטוט ארוך יותר"):
        _take_quote(src, full, {"quote": "השווי הוא 1", "number": "1"}, blocks)


def test_take_quote_with_the_same_number_twice_is_accepted_at_block_precision():
    from app.chat.tools import _take_quote

    full = "השווי הוא 9,500 ₪.\nכאמור, השווי הוא 9,500 ₪."
    src = _source(text=full, block_start=4, block_end=5, reading_id="r1")
    blocks = [(4, "השווי הוא 9,500 ₪.", 3), (5, "כאמור, השווי הוא 9,500 ₪.", 4)]
    taken = _take_quote(src, full, {"quote": "השווי הוא 9,500", "number": "9,500"}, blocks)
    assert taken["anchor"]["blocks"] == [4, 5] and "segments" not in taken["anchor"]
    assert taken["anchor"]["pages"] == [3, 4]


def test_take_quote_records_the_block_and_word_span():
    from app.chat.tools import _take_quote

    src = _source(text=PARAGRAPH, block_start=0, block_end=0, reading_id="r1")
    taken = _take_quote(src, PARAGRAPH, {"quote": "נקבע ל-12,450,000 ₪", "number": "12,450,000"},
                        [(0, PARAGRAPH, 3)])
    (seg,) = taken["anchor"]["segments"]
    assert seg[0] == 0 and PARAGRAPH[seg[1]:seg[2]] == "נקבע ל-12,450,000 ₪" and taken["anchor"]["pages"] == [3]


def test_a_source_registered_in_a_workspace_gets_a_stub_with_its_reading_and_row():
    from uuid import UUID

    from app.chat.tools import Workspace

    ws = Workspace(ctx=None)
    ws.readings[VER] = "r1"
    s = ws.add_source(document_id=UUID(DOC), version_id=UUID(VER), title="t", section="5. תחשיב", location="",
                      kind="table_row", text="x", block_start=1, block_end=1, table_index=0, row_index=1,
                      page_list=[3])
    a = ws.anchors[s.sid]
    assert (a["reading_id"], a["table_index"], a["row"], a["pages"], a["block_start"]) == ("r1", 0, 1, [3], 1)
    assert s.public()["row_index"] == 1
    listing = ws.add_source(document_id=None, version_id=None, title="רשימה", section=None, location="",
                            kind="listing", text="")
    assert listing.sid not in ws.anchors
