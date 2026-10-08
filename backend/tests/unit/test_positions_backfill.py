"""The geometry-only backfill's reading and alignment (U3, KTD3, R13), without a database.

A version read before positions existed keeps its blocks, tables and ``reading_id``; the backfill reads the stored
PDF's text layer again (no OCR, no vision, no model) and gives each existing block and table the positions a fresh
reading would have stored, matched by text, never by index alone. Every fixture is synthetic: the "stored" reading is
a fresh ``extract_pdf`` result with its positions stripped, so the expected positions are exactly the fresh ones.
"""

from __future__ import annotations

import time
from functools import cache
from pathlib import Path

import pytest

from app.config import get_settings
from app.extraction import fontmap
from app.extraction.base import ExtractionResult
from app.extraction.geometry import Span
from app.extraction.pdf import _Line, extract_pdf
from app.platform import positions
from app.platform.positions import StoredBlock, StoredPage, StoredTable, align_blocks, remap_spans

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
POS = FIXTURES / "positions"
P1 = POS / "P1_synthetic_upright.pdf"
P2 = POS / "P2_synthetic_rotated_90_cropped.pdf"
P5 = POS / "P5_synthetic_rotate_45.pdf"
P6 = POS / "P6_synthetic_cropped.pdf"
BROKEN = FIXTURES / "fontmap" / "F1_synthetic_fontmap_broken.pdf"
CLEAN = FIXTURES / "fontmap" / "F1_synthetic_fontmap_clean.pdf"
SCANNED = FIXTURES / "D2_synthetic_harozim_scanned.pdf"


def _no_model_calls(*_a, **_k):
    raise AssertionError("the geometry backfill must not run OCR, vision or a model")


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)


def forbid_reading(monkeypatch) -> None:
    """From here on nothing may OCR a page, verify a font map or call a vision model (the backfill runs next)."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", _no_model_calls)
    monkeypatch.setattr("app.extraction.ocr.ocr_page_image", _no_model_calls)
    monkeypatch.setattr("app.extraction.ocr.render_page", _no_model_calls)
    monkeypatch.setattr(fontmap, "detect_and_repair", _no_model_calls)
    monkeypatch.setattr("app.extraction.regions.read_regions", _no_model_calls)


@cache
def fresh(path: Path) -> ExtractionResult:
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings())


@cache
def fresh_repaired() -> ExtractionResult:
    from tests.unit.test_fontmap import CleanTwinReader

    real = fontmap.detect_and_repair
    reader = CleanTwinReader(CLEAN)
    original = fontmap.detect_and_repair
    fontmap.detect_and_repair = lambda words, pdf_doc=None, config=None, reader_=None: real(words, pdf_doc, config,
                                                                                           reader)
    try:
        return extract_pdf(BROKEN.read_bytes(), time.monotonic() + 600, get_settings())
    finally:
        fontmap.detect_and_repair = original


def stored(result: ExtractionResult) -> tuple[list[StoredPage], list[StoredBlock], list[StoredTable]]:
    """The reading as a version read before positions has it: the same pages, blocks and tables, no positions."""
    pages = [StoredPage(p.page_no, p.method, p.ok) for p in result.pages]
    blocks = [StoredBlock(b.index, b.kind, b.page, b.method, b.text) for b in result.blocks]
    tables = [StoredTable(t.index, {"headers": t.headers, "ocr": t.ocr, "source": t.source,
                                    "rows": [{"page": r.page, "cells": r.cells} for r in t.rows],
                                    "block_index": t.block_index}) for t in result.tables]
    return pages, blocks, tables


def backfill(data: bytes, pages, blocks, tables, report=None) -> positions.Positions:
    return positions.read_positions(data, pages, blocks, tables, report, get_settings(), time.monotonic() + 600)


def text_blocks(result: ExtractionResult):
    return [b for b in result.blocks if b.kind in ("heading", "paragraph") and b.method in ("text_layer", "mixed")]


# --- the same positions a fresh reading stores -------------------------------------------------------------------

@pytest.mark.parametrize("path", [P1, P2, P6, P5])
def test_the_backfill_stores_exactly_the_positions_a_fresh_reading_stores(path, monkeypatch):
    result = fresh(path)
    forbid_reading(monkeypatch)
    got = backfill(path.read_bytes(), *stored(result))
    assert {b.index: b.spans for b in result.blocks if b.spans} == got.spans
    for t in result.tables:
        structure = got.tables.get(t.index)
        if any(r.cell_boxes for r in t.rows):
            assert [r.get("cell_boxes") for r in structure["rows"]] == [r.cell_boxes or None for r in t.rows]
            assert structure.get("header_boxes") == (t.header_boxes or None)
    assert [(p.page_no, p.geometry, p.printed_label) for p in got.pages] == \
        [(p.page_no, p.geometry, p.printed_label) for p in result.pages]


def test_counts_of_aligned_blocks_and_tables_are_reported():
    result = fresh(P1)
    got = backfill(P1.read_bytes(), *stored(result))
    assert got.counts["blocks"] == {"aligned": len(text_blocks(result)), "unaligned": 0, "no_positions": 0}
    assert got.counts["tables"] == {"aligned": len(result.tables), "unaligned": 0, "no_positions": 0}


def test_a_page_whose_geometry_cannot_be_converted_aligns_but_stores_no_spans():
    result = fresh(P5)
    got = backfill(P5.read_bytes(), *stored(result))
    on_first = [b for b in text_blocks(result) if b.page == 1]
    assert on_first and not any(b.index in got.spans for b in on_first)
    assert got.counts["blocks"]["no_positions"] == len(on_first)
    assert got.pages[0].geometry.issue is not None and got.pages[0].geometry.rotation == 45


# --- aligned by text, never by index alone -----------------------------------------------------------------------

def test_blocks_align_by_their_text_when_the_stored_indexes_differ_from_a_fresh_reading():
    result = fresh(P1)
    pages, blocks, tables = stored(result)
    # an older reader stored a picture block first: every index is shifted by one
    shifted = [StoredBlock(0, "image", 1, "none", "")] + [
        StoredBlock(b.index + 1, b.kind, b.page, b.method, b.text) for b in blocks]
    got = backfill(P1.read_bytes(), pages, shifted, tables)
    assert {i - 1: s for i, s in got.spans.items()} == {b.index: b.spans for b in result.blocks if b.spans}
    for b in shifted:
        for s in map(Span.from_json, got.spans.get(b.index) or []):
            assert b.text[s.start:s.end] == b.text.split("\n")[s.line].split()[s.word]


def test_a_block_whose_text_differs_from_the_reread_gets_nothing_and_the_others_still_align():
    result = fresh(P1)
    pages, blocks, tables = stored(result)
    changed = text_blocks(result)[1].index
    blocks = [StoredBlock(b.index, b.kind, b.page, b.method, "טקסט אחר לגמרי" if b.index == changed else b.text)
              for b in blocks]
    got = backfill(P1.read_bytes(), pages, blocks, tables)
    assert changed not in got.spans
    assert set(got.spans) == {b.index for b in text_blocks(result)} - {changed}
    assert got.counts["blocks"]["unaligned"] == 1


def test_blocks_of_a_reader_without_block_provenance_align_by_their_page_method():
    result = fresh(P1)
    pages, blocks, tables = stored(result)
    older = [StoredBlock(b.index, b.kind, b.page, None, b.text) for b in blocks]
    got = backfill(P1.read_bytes(), pages, older, tables)
    assert got.spans == {b.index: b.spans for b in result.blocks if b.spans}


def test_a_table_aligns_by_its_content_and_a_changed_table_gets_no_cell_boxes():
    result = fresh(P1)
    pages, blocks, tables = stored(result)
    [table] = tables
    extra = StoredTable(0, {"headers": ["א", "ב"], "ocr": True, "source": "ocr", "rows": [{"page": 1, "cells": ["1", "2"]}]})
    moved = StoredTable(1, table.structure)
    got = backfill(P1.read_bytes(), pages, blocks, [extra, moved])
    assert 0 not in got.tables and got.tables[1]["rows"][0]["cell_boxes"] == result.tables[0].rows[0].cell_boxes
    edited = StoredTable(0, table.structure | {"rows": [{"page": 1, "cells": ["x", "y", "z"]}, *table.structure["rows"][1:]]})
    got = backfill(P1.read_bytes(), pages, blocks, [edited])
    assert got.tables == {} and got.counts["tables"]["unaligned"] == 1


def test_whitespace_differences_remap_spans_onto_the_stored_text():
    src = "שטח 88 מ״ר\nשווי 1,760,000 ₪"
    dst = "שטח  88 מ״ר\n שווי 1,760,000 ₪"
    spans = [[0, k, m.start(), m.end(), 0, 0, 1, 1] for k, m in enumerate(__import__("re").finditer(r"\S+", src))]
    out = remap_spans(spans, src, dst)
    assert [dst[s[2]:s[3]] for s in out] == src.split()
    assert remap_spans(spans, src, "טקסט אחר") is None


def test_align_blocks_walks_the_lines_in_order_and_skips_table_and_footer_lines():
    lines = [_Line(10.0, "כותרת", 20.0, 0.0, 50.0), _Line(30.0, "שורה א", 40.0, 0.0, 50.0),
             _Line(45.0, "תא בטבלה", 55.0, 0.0, 50.0, in_table=True), _Line(60.0, "שורה א", 70.0, 0.0, 50.0),
             _Line(800.0, "עמוד 5", 810.0, 0.0, 50.0)]
    blocks = [StoredBlock(3, "paragraph", 1, "text_layer", "שורה א"), StoredBlock(4, "paragraph", 1, "text_layer",
                                                                                   "שורה א")]
    found = align_blocks(blocks, lines)
    assert [run[0].top for run in found.values()] == [30.0, 60.0]
    assert align_blocks([StoredBlock(1, "paragraph", 1, "text_layer", "עמוד 5")], lines) == {}


# --- font-map-repaired versions ----------------------------------------------------------------------------------

def test_repaired_pages_are_read_again_through_the_recorded_corrections(monkeypatch):
    result = fresh_repaired()
    forbid_reading(monkeypatch)
    assert result.fontmap and result.fontmap["corrections"]
    corrected = [b for b in result.blocks if b.original_text is not None]
    got = backfill(BROKEN.read_bytes(), *stored(result), result.fontmap)
    assert corrected and all(b.index in got.spans for b in corrected)
    assert {b.index: b.spans for b in result.blocks if b.spans} == got.spans
    for b in corrected:
        words = [b.text[s.start:s.end] for s in map(Span.from_json, got.spans[b.index])]
        assert words == b.text.split() and "ð" not in "".join(words)


def test_without_the_recorded_corrections_repaired_blocks_do_not_align():
    result = fresh_repaired()
    got = backfill(BROKEN.read_bytes(), *stored(result), None)
    corrected = {b.index for b in result.blocks if b.original_text is not None}
    assert corrected and not corrected & set(got.spans)
    assert got.counts["blocks"]["unaligned"] >= len(corrected)


def test_a_repaired_block_whose_reread_still_differs_gets_no_spans_and_the_others_do():
    result = fresh_repaired()
    pages, blocks, tables = stored(result)
    corrected = [b.index for b in result.blocks if b.original_text is not None]
    odd = corrected[0]
    blocks = [StoredBlock(b.index, b.kind, b.page, b.method, b.text.replace("נ", "ן", 1) if b.index == odd else b.text)
              for b in blocks]
    got = backfill(BROKEN.read_bytes(), pages, blocks, tables, result.fontmap)
    assert odd not in got.spans and all(i in got.spans for i in corrected[1:])


def test_a_positional_correction_without_its_recorded_order_still_aligns_through_the_other_order():
    fixes = positions.fixes_from_report({"corrections": [
        {"font": "F", "from": "ð", "to": "נ", "to_final": "ן", "samples": 5, "agreement": 1.0, "occurrences": 9}]})
    assert [next(iter(f.corrections.values())).visual for f in fixes] == [False, True]
    assert positions.fixes_from_report({"corrections": [
        {"font": "F", "from": "ð", "to": "נ", "to_final": None}]})[0].corrections[("F", "ð")].letter == "נ"
    assert positions.fixes_from_report(None) == [None]


# --- pages read by OCR and vision content ------------------------------------------------------------------------

def test_ocr_pages_get_page_geometry_but_no_spans(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    from app.extraction import pdf as pdf_reader

    lines = [_Line(40.0, "שומת מקרקעין לדוגמה"), _Line(60.0, "שטח הדירה 88 מ״ר ושוויה 1,760,000 ₪.")]
    monkeypatch.setattr(pdf_reader, "_ocr_page", lambda doc, index, settings: (list(lines), []))
    result = extract_pdf(SCANNED.read_bytes(), time.monotonic() + 600, get_settings())
    assert result.pages[0].method == "ocr"
    monkeypatch.setattr(pdf_reader, "_ocr_page", _no_model_calls)
    monkeypatch.setattr("app.extraction.ocr.ocr_available", _no_model_calls)
    got = backfill(SCANNED.read_bytes(), *stored(result))
    assert got.spans == {} and got.counts["blocks"] == {"aligned": 0, "unaligned": 0, "no_positions": 0}
    assert got.pages[0].geometry == result.pages[0].geometry


def test_a_mixed_page_keeps_its_text_spans_and_its_vision_table_gets_no_cell_boxes(monkeypatch):
    from tests.unit.test_regions import R1, TABLE, TABLE_WORDS, ScriptedVision, scripted_ocr

    scripted_ocr(monkeypatch, {TABLE: TABLE_WORDS})
    result = extract_pdf(R1.read_bytes(), time.monotonic() + 600, get_settings(), ScriptedVision())
    assert result.pages[0].method == "mixed"
    monkeypatch.setattr("app.extraction.regions.read_regions", _no_model_calls)
    monkeypatch.setattr("app.extraction.ocr.ocr_page_image", _no_model_calls)
    got = backfill(R1.read_bytes(), *stored(result))
    assert {b.index: b.spans for b in result.blocks if b.spans} == got.spans and got.spans
    assert not any(t.index in got.tables for t in result.tables if t.source == "vision")


def test_a_file_that_is_not_a_pdf_is_a_permanent_error():
    from app.extraction.base import ExtractionError

    with pytest.raises(ExtractionError) as err:
        backfill(b"not a pdf", [], [], [])
    assert err.value.permanent
