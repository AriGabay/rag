"""Positions stored by the PDF reader (U2, KTD2, AE2, R3, R4, R6, R8): page geometry and printed page numbers, word
spans of text-layer blocks and cell boxes of text-layer tables, all in the frame of the rendered page.

The fixtures under ``tests/fixtures/positions/`` are synthetic (``scripts/generate_fixtures.py --only positions``):
one page with a Hebrew sentence around numbers and a ruled table with an empty row on each page that continues on the
next page without its header, the footer printing a page number four ahead of the file page. ``P1`` is upright;
``P2``-``P4`` show the same pages under ``/Rotate`` 90, 180 and 270 with a shifted MediaBox and an inset CropBox;
``P6`` has the shifted, inset boxes without rotation; ``P5`` sets ``/Rotate 45`` on page 1.

Every position is checked against pypdfium2, independently of pdfplumber: its character boxes (PDF user space) are
put on the rendered page with the textbook transform of the CropBox and ``/Rotate`` read by pypdf, and the page is
rendered to check a highlight lies on ink. OCR is switched off; OCR and vision are scripted where a test needs them.
"""

from __future__ import annotations

import io
import time
from functools import cache
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pytest
from pypdf import PdfReader, PdfWriter

from app.config import get_settings
from app.extraction import fontmap, geometry
from app.extraction import pdf as pdf_reader
from app.extraction.base import ExtractionResult, PageGeometry, PageResult
from app.extraction.geometry import Span
from app.extraction.pdf import _Line, _PageOut, extract_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
POS = FIXTURES / "positions"
P1 = POS / "P1_synthetic_upright.pdf"
P2 = POS / "P2_synthetic_rotated_90_cropped.pdf"
P3 = POS / "P3_synthetic_rotated_180_cropped.pdf"
P4 = POS / "P4_synthetic_rotated_270_cropped.pdf"
P5 = POS / "P5_synthetic_rotate_45.pdf"
P6 = POS / "P6_synthetic_cropped.pdf"
SENTENCE = "שטח הדירה הוא 88 מ״ר ושוויה 1,760,000 ₪ לפי ההערכה."
TOLERANCE = 2.0  # points


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)


@cache
def read(path: Path) -> ExtractionResult:
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings())


def sentence_block(result: ExtractionResult):
    return next(b for b in result.blocks if b.text == SENTENCE)


def _shown(box, crop, rotation):
    cx0, cy0, cx1, cy1 = crop
    f = {0: lambda x, y: (x - cx0, cy1 - y), 90: lambda x, y: (y - cy0, x - cx0),
         180: lambda x, y: (cx1 - x, y - cy0), 270: lambda x, y: (cy1 - y, cx1 - x)}[rotation]
    pts = [f(x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)]


@cache
def shown_chars(path: Path, page_no: int) -> list[tuple[str, list[float]]]:
    """Every visible character of the page with its box on the rendered page, from pypdfium2 and pypdf only."""
    page = PdfReader(path).pages[page_no - 1]
    mb, cb = [float(v) for v in page.mediabox], [float(v) for v in page.cropbox]
    crop = (max(mb[0], cb[0]), max(mb[1], cb[1]), min(mb[2], cb[2]), min(mb[3], cb[3]))
    text = pdfium.PdfDocument(str(path))[page_no - 1].get_textpage()
    out = []
    for i in range(text.count_chars()):
        ch = text.get_text_range(i, 1)
        if ch.strip():
            out.append((ch, _shown(text.get_charbox(i, loose=True), crop, page.rotation % 360)))
    return out


def chars_in(path: Path, page_no: int, box: list[float], pad: float = TOLERANCE) -> list[tuple[str, list[float]]]:
    return [(ch, b) for ch, b in shown_chars(path, page_no)
            if box[0] - pad <= (b[0] + b[2]) / 2 <= box[2] + pad and box[1] - pad <= (b[1] + b[3]) / 2 <= box[3] + pad]


def union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


# --- page geometry and the sentence on every rotation (AE2) ------------------------------------------------------

@pytest.mark.parametrize(("path", "rotation"), [(P1, 0), (P2, 90), (P3, 180), (P4, 270), (P6, 0)])
def test_each_word_of_the_sentence_lies_on_its_glyphs_on_the_rendered_page(path, rotation):
    result = read(path)
    page = result.pages[0]
    geom = page.geometry
    rendered = pdfium.PdfDocument(str(path))[0]
    assert geom.issue is None and geom.rotation == rotation
    assert (geom.width, geom.height) == pytest.approx(rendered.get_size(), abs=0.01)
    block = sentence_block(result)
    spans = [Span.from_json(s) for s in block.spans]
    assert [block.text[s.start:s.end] for s in spans] == SENTENCE.split()
    for s in spans:
        found = chars_in(path, 1, s.box)
        assert sorted(ch for ch, _ in found) == sorted(block.text[s.start:s.end])  # its own glyphs, no others
        assert union([b for _, b in found]) == pytest.approx(s.box, abs=TOLERANCE)
    # the sentence as a whole: its stored region, converted with the page geometry, is where its glyphs are
    shown = union([b for s in spans for _, b in chars_in(path, 1, s.box)])
    assert geometry.geometry_box(block.bbox, geom) == pytest.approx(shown, abs=TOLERANCE)


def test_on_a_page_turned_ninety_degrees_with_an_offset_crop_box_the_highlight_lies_on_the_sentence_ink():
    block = sentence_block(read(P2))
    scale = 2
    image = pdfium.PdfDocument(str(P2))[0].render(scale=scale).to_pil().convert("L")

    def ink(box) -> int:
        x0, top, x1, bottom = (round(v * scale) for v in box)
        return sum(1 for v in image.crop((x0, top, x1, bottom)).getdata() if v < 128)

    for row in block.spans:
        assert ink(Span.from_json(row).box) > 10
    line = union([Span.from_json(s).box for s in block.spans])
    assert ink([line[0], line[3] + 6, line[2], line[3] + 12]) == 0  # the band under the sentence is blank


def test_rotation_zero_with_the_crop_box_equal_to_the_media_box_stores_pdfplumber_boxes():
    result = read(P1)
    block = sentence_block(result)
    assert union([Span.from_json(s).box for s in block.spans]) == pytest.approx(block.bbox, abs=0.11)
    with pdfplumber.open(P1) as doc:
        cells = doc.pages[0].find_tables()[0].rows[1].cells
    stored = result.tables[0].rows[0].cell_boxes
    assert stored == [[round(v, 1) for v in c] for c in reversed(cells)]


def test_a_hebrew_line_with_embedded_numbers_selects_each_word_and_covers_its_glyphs():
    block = sentence_block(read(P1))
    spans = {block.text[s.start:s.end]: s for s in map(Span.from_json, block.spans)}
    for word in ("88", "1,760,000", "₪", "מ״ר", "ההערכה."):
        found = chars_in(P1, 1, spans[word].box, pad=0.5)
        assert "".join(sorted(ch for ch, _ in found)) == "".join(sorted(word))
    # right to left: the first word is the rightmost, the number sits between its neighbours
    assert spans["שטח"].box[0] > spans["הדירה"].box[2]
    assert spans["ושוויה"].box[0] > spans["1,760,000"].box[2] > spans["1,760,000"].box[0] > spans["₪"].box[2]


def test_rotated_pages_read_the_same_text_as_the_upright_one():
    upright = [(b.kind, b.text, b.page) for b in read(P1).blocks]
    assert [(b.kind, b.text, b.page) for b in read(P6).blocks] == upright
    # pdfplumber reads the ruled table of the turned variants only in pieces (its rules are drawn turned, so some
    # cells merge); the text above the table reads the same
    for path in (P2, P3, P4):
        assert [(b.kind, b.text, b.page) for b in read(path).blocks][:3] == upright[:3]


# --- tables ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [P1, P6])
def test_each_cell_box_belongs_to_the_value_at_its_row_and_column(path):
    result = read(path)
    [table] = result.tables
    assert table.headers == ["רכיב", "שטח (מ״ר)", "שווי (₪)"]
    assert len(table.rows) == 38 and all(any(r.cells) for r in table.rows)  # the two empty rows are dropped
    assert {r.page for r in table.rows} == {1, 2} and table.page_start == 1 and table.page_end == 2
    pairs = [(1, h, b) for h, b in zip(table.headers, table.header_boxes, strict=True)]
    pairs += [(r.page, c, b) for r in table.rows for c, b in zip(r.cells, r.cell_boxes, strict=True)]
    for page, value, box in pairs:
        found = chars_in(path, page, box, pad=0.0)
        assert sorted(ch for ch, _ in found) == sorted(value.replace(" ", "")), (page, value)
    # the row after each empty row: row index k still names the value the table shows at k
    assert table.rows[2].cells[0] == "רכיב 4" and table.rows[2].cells[2] == f"{14 * 21_000:,}"
    assert table.rows[-1].cells[0] == "רכיב 40" and table.rows[-1].page == 2


def test_the_table_block_keeps_its_region_and_no_spans():
    result = read(P1)
    block = next(b for b in result.blocks if b.kind == "table")
    assert block.bbox is not None and block.spans is None


# --- pages whose positions cannot be converted -------------------------------------------------------------------

def test_a_page_whose_geometry_cannot_be_converted_keeps_block_regions_and_records_why():
    result = read(P5)
    first, second = result.pages
    assert first.geometry.issue == geometry.ISSUE_ROTATION and first.geometry.rotation == 45
    assert second.geometry.issue is None
    on_first = [b for b in result.blocks if b.page == 1 and b.kind in ("heading", "paragraph")]
    assert on_first and all(b.spans is None and b.bbox is not None for b in on_first)
    [table] = result.tables
    assert table.header_boxes is None
    assert all(r.cell_boxes is None for r in table.rows if r.page == 1)
    assert all(r.cell_boxes is not None for r in table.rows if r.page == 2)
    assert [b.text for b in result.blocks] == [b.text for b in read(P1).blocks]


# --- OCR and vision content get no word or cell positions --------------------------------------------------------

def test_an_ocr_page_stores_its_geometry_but_no_spans_or_cell_boxes(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    lines = [_Line(40.0, "שומת מקרקעין לדוגמה"), _Line(60.0, "שטח הדירה 88 מ״ר ושוויה 1,760,000 ₪.")]
    monkeypatch.setattr(pdf_reader, "_ocr_page", lambda doc, index, settings: (
        list(lines), [(120.0, [["כתובת", "שטח (מ״ר)"], ["התאנה 3", "82"]])]))
    result = extract_pdf((FIXTURES / "D2_synthetic_harozim_scanned.pdf").read_bytes(), time.monotonic() + 600,
                         get_settings())
    page = result.pages[0]
    assert page.method == "ocr" and page.geometry is not None and page.geometry.width
    text_blocks = [b for b in result.blocks if b.kind in ("heading", "paragraph")]
    assert text_blocks and all(b.spans is None for b in text_blocks)
    assert result.tables and all(t.header_boxes is None and all(r.cell_boxes is None for r in t.rows)
                                 for t in result.tables)


def test_a_vision_table_keeps_its_picture_region_and_no_cell_boxes(monkeypatch):
    from tests.unit.test_regions import R1, TABLE, TABLE_WORDS, ScriptedVision, scripted_ocr

    scripted_ocr(monkeypatch, {TABLE: TABLE_WORDS})
    result = extract_pdf(R1.read_bytes(), time.monotonic() + 600, get_settings(), ScriptedVision())
    vision_tables = [t for t in result.tables if t.source == "vision"]
    assert vision_tables
    for t in vision_tables:
        block = result.blocks[t.block_index]
        assert block.kind == "image" and block.bbox is not None and block.spans is None
        assert t.header_boxes is None and all(r.cell_boxes is None for r in t.rows)
    # the text-layer paragraphs of the same (mixed) page still carry their spans
    assert result.pages[0].method == "mixed"
    assert all(b.spans for b in result.blocks if b.page == 1 and b.kind in ("heading", "paragraph"))


# --- a font-map-repaired page --------------------------------------------------------------------------------------

def test_spans_of_a_repaired_page_index_the_corrected_text(monkeypatch):
    from tests.unit.test_fontmap import BROKEN, CLEAN, CleanTwinReader

    reader = CleanTwinReader(CLEAN)
    real = fontmap.detect_and_repair
    monkeypatch.setattr(fontmap, "detect_and_repair",
                        lambda words, pdf_doc=None, config=None, reader_=None: real(words, pdf_doc, config, reader))
    repaired = extract_pdf(BROKEN.read_bytes(), time.monotonic() + 600, get_settings())
    monkeypatch.undo()
    clean = extract_pdf(CLEAN.read_bytes(), time.monotonic() + 600, get_settings())
    corrected = [b for b in repaired.blocks if b.original_text is not None]
    assert corrected and all(b.spans for b in corrected)
    for b in corrected:
        words = [b.text[s.start:s.end] for s in map(Span.from_json, b.spans)]
        assert words == b.text.split() and "ð" not in "".join(words)
    assert [(b.text, b.spans) for b in repaired.blocks] == [(b.text, b.spans) for b in clean.blocks]


# --- printed page numbers (R8) -----------------------------------------------------------------------------------

def test_the_printed_page_number_is_read_from_a_consistent_footer():
    assert [p.printed_label for p in read(P1).pages] == ["5", "6"]
    assert [p.printed_label for p in read(P2).pages] == ["5", "6"]  # found on the rendered page, not the raw one


def test_page_labels_of_the_document_win_over_the_footer():
    writer = PdfWriter(clone_from=PdfReader(P1))
    writer.set_page_label(0, 1, style="/r", start=3)
    buf = io.BytesIO()
    writer.write(buf)
    result = extract_pdf(buf.getvalue(), time.monotonic() + 600, get_settings())
    assert [p.printed_label for p in result.pages] == ["iii", "iv"]


class _Doc:
    def get_page_label(self, index):
        return ""


def _out(page_no: int, footer: str | None) -> _PageOut:
    geom = PageGeometry([0, 0, 600, 800], [0, 0, 600, 800], 0, 600.0, 800.0)
    lines = [_Line(100.0, "פסקה בגוף העמוד", 110.0, 100.0, 500.0)]
    if footer is not None:
        lines.append(_Line(770.0, footer, 778.0, 250.0, 350.0))
    return _PageOut(PageResult(page_no, "", "text_layer", 1.0, True, geom), lines, [], width=600.0, height=800.0)


@pytest.mark.parametrize(
    ("footers", "labels"),
    [
        (["עמוד 3", "עמוד 4", None], ["3", "4", None]),
        (["- 1 -", "- 2 -", "- 3 -"], ["1", "2", "3"]),
        (["עמוד 3", "עמוד 7", "עמוד 5"], [None, None, None]),  # no consistent numbering: file pages only
        (["עמוד 3", None, None], [None, None, None]),  # one page is no evidence
    ],
)
def test_printed_numbers_need_a_numbering_consistent_across_pages(footers, labels):
    outs = [_out(k + 1, f) for k, f in enumerate(footers)]
    pdf_reader._printed_labels(_Doc(), outs)
    assert [o.page.printed_label for o in outs] == labels


def test_a_number_in_the_middle_of_the_page_is_not_a_page_number():
    outs = [_out(1, None), _out(2, None)]
    for o in outs:
        o.lines.append(_Line(400.0, str(o.page.page_no + 2), 410.0, 280.0, 300.0))
    pdf_reader._printed_labels(_Doc(), outs)
    assert [o.page.printed_label for o in outs] == [None, None]


# --- the text lines the reader keeps glyphs for ------------------------------------------------------------------

@pytest.mark.parametrize("path", [P1, P2, FIXTURES / "blocks" / "B1_synthetic_reading_order.pdf",
                                  FIXTURES / "D10_synthetic_harozim_visual_order.pdf"])
def test_text_lines_are_pdfplumbers_lines_with_one_glyph_per_character(path):
    with pdfplumber.open(path) as doc:
        for page in doc.pages:
            page = page.dedupe_chars(tolerance=pdf_reader.DEDUPE_TOLERANCE)
            theirs = page.extract_text_lines(return_chars=True)
            ours = pdf_reader._text_lines(page)
            assert [(ln["text"], ln["x0"], ln["top"], ln["x1"], ln["bottom"], [id(c) for c in ln["chars"]])
                    for ln in ours] == [(ln["text"], ln["x0"], ln["top"], ln["x1"], ln["bottom"],
                                         [id(c) for c in ln["chars"]]) for ln in theirs]
            assert all(len(ln["slots"]) == len(ln["text"]) for ln in ours)
