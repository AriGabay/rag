"""PDF reading in blocks (U3, KTD3): headings with sub-sections, paragraphs, tables with their caption and notes, and
picture placeholders, in reading order with pages and page regions, chunked by ``chunk_blocks``.

The fixtures under ``tests/fixtures/blocks/`` are synthetic reproductions of producer quirks, written by
``scripts/generate_fixtures.py``: bold drawn twice, a title page without orientation evidence, a heading whose last
letter sits on its own baseline, a table between two paragraphs and a table continuing on the next page. The DOCX
characterization at the end pins DOCX output to what it was before PDFs moved to the block model."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from functools import cache
from pathlib import Path

import pytest

from app.config import get_settings
from app.extraction import docx
from app.extraction.base import ExtractionResult
from app.extraction.images import PictureReading
from app.extraction.pdf import extract_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
B1 = FIXTURES / "blocks" / "B1_synthetic_reading_order.pdf"
B2 = FIXTURES / "blocks" / "B2_synthetic_pictures.pdf"


@cache
def read(path: Path) -> ExtractionResult:
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings())


def blocks(path: Path = B1):
    return read(path).blocks


def find(text: str, path: Path = B1):
    return next(b for b in blocks(path) if b.text.splitlines() and b.text.splitlines()[0] == text)


# --- reading order ---------------------------------------------------------------------------------------------

def test_blocks_follow_the_page_heading_paragraph_table_paragraph():
    got = [(b.kind, b.page, b.text.splitlines()[0] if b.text else "") for b in blocks()]
    assert got == [
        ("paragraph", 1, "חוות דעת שמאית"),
        ("paragraph", 1, "גבעת התאנה"),  # another type size: another block
        ("heading", 2, "1. מטרת חוות הדעת"),
        ("paragraph", 2, "חוות דעת זו נערכה לצורך הדגמה בלבד ואינה מתייחסת לנכס אמיתי. כל השמות והמספרים בה בדויים."),
        ("heading", 2, "2. נתוני השוואה"),
        ("paragraph", 2, "להלן עסקאות שנמצאו בסביבת הנכס:"),
        ("table", 2, "כתובת | שטח (מ״ר) | מחיר (₪)"),
        ("paragraph", 2, "העסקאות מלמדות על מחיר ממוצע של כ-20,000 ₪ למ״ר בנוי."),
        ("heading", 2, "3. תיאור הנכס"),
        ("heading", 2, "3.1 הבניין"),
        ("paragraph", 2, "הבניין בן ארבע קומות ונבנה בשנת 1995."),
        ("heading", 2, "3.2 הדירה"),
        ("paragraph", 2, "הדירה בקומה השנייה ושטחה 88 מ״ר."),
        ("heading", 2, "4. התחשיב"),
        ("paragraph", 2, "טבלת התחשיב לפי רכיבים:"),
        ("table", 2, "רכיב | שטח (מ״ר) | שווי (₪)"),
        ("paragraph", 3, "סך שווי הרכיבים מופיע בשורה האחרונה של הטבלה."),
        ("heading", 3, "5. סיכום"),
        ("paragraph", 3, "שווי הנכס המוערך הוא 1,760,000 ₪."),
    ]
    assert [b.index for b in blocks()] == list(range(len(blocks())))


def test_every_block_keeps_its_page_region_and_method():
    for b in blocks():
        assert b.page is not None and b.method == "text_layer" and b.reader_version, b
        x0, top, x1, bottom = b.bbox
        assert 0 <= x0 < x1 <= 600 and 0 <= top < bottom <= 850, b


def test_chunks_follow_the_blocks():
    result = read(B1)
    starts = [c.block_start for c in result.chunks]
    assert None not in starts and starts == sorted(starts)
    for c in result.chunks:
        assert c.page_list and c.page_list == sorted(set(c.page_list)), c
    first = next(c for c in result.chunks if "העסקאות מלמדות" in c.text)
    table = next(c for c in result.chunks if c.kind == "table" and "התאנה 3" in c.text)
    assert table.block_start < first.block_start and first.page_list == [2]


# --- sections --------------------------------------------------------------------------------------------------

def test_a_sub_section_is_its_own_section_under_its_parent():
    heading = find("3.1 הבניין")
    assert heading.label == "3.1" and heading.section_path == ["3. תיאור הנכס", "3.1 הבניין"]
    para = find("הבניין בן ארבע קומות ונבנה בשנת 1995.")
    assert para.section == "3.1 הבניין" and para.section_path == ["3. תיאור הנכס", "3.1 הבניין"]
    assert find("הדירה בקומה השנייה ושטחה 88 מ״ר.").section_path == ["3. תיאור הנכס", "3.2 הדירה"]
    assert find("4. התחשיב").section_path == ["4. התחשיב"]  # a new top-level section closes 3.2
    chunk = next(c for c in read(B1).chunks if "ארבע קומות" in c.text)
    assert chunk.section == "3.1 הבניין"


def test_a_decimal_number_starting_a_line_is_not_a_sub_heading():
    from app.extraction.chunking import heading_level

    assert heading_level("3.1 הבניין") == 2 and heading_level("4. התחשיב") == 1
    assert heading_level("2.1.3 מצב תחזוקה") == 3
    assert heading_level("2.5 חדרים בקומה השנייה.") is None  # a sentence, not a heading
    assert heading_level("3,500 ₪ לחודש") is None


def test_a_dotted_number_outside_its_parent_section_is_paragraph_text():
    from app.extraction.base import PageResult
    from app.extraction.pdf import _Line, _PageOut, _Walker

    lines = [_Line(10, "3. תיאור הנכס", 20, 300, 550, 12, True), _Line(30, "3.1 הבניין", 40, 400, 550, 11, True),
             _Line(50, "2.5 חדרים בקומה השנייה", 60, 300, 550, 11, False)]
    w = _Walker()
    w.page(_PageOut(PageResult(1, "", "text_layer", 1.0, True), lines, []))
    assert [(b.kind, b.text) for b in w.blocks] == [("heading", "3. תיאור הנכס"), ("heading", "3.1 הבניין"),
                                                    ("paragraph", "2.5 חדרים בקומה השנייה")]
    assert w.blocks[-1].section_path == ["3. תיאור הנכס", "3.1 הבניין"]


# --- producer quirks -------------------------------------------------------------------------------------------

def test_bold_drawn_twice_reads_as_single_letters():
    assert find("1. מטרת חוות הדעת").kind == "heading"
    assert "ממ" not in "\n".join(b.text for b in blocks() if b.kind == "heading").replace("מטרת", "")
    assert all("תתעעדד" not in p.text for p in read(B1).pages)


def test_a_title_page_without_orientation_evidence_follows_the_document():
    assert read(B1).pages[0].text.splitlines() == ["חוות דעת שמאית", "גבעת התאנה"]


def test_a_heading_split_inside_a_word_is_one_heading():
    heading = find("4. התחשיב")
    assert heading.kind == "heading" and heading.text == "4. התחשיב"
    assert not any(b.text.strip() == "ב" for b in blocks())


# --- tables ----------------------------------------------------------------------------------------------------

def test_a_table_between_paragraphs_keeps_its_caption_and_notes_and_leaves_the_paragraphs():
    t = read(B1).tables[0]
    assert t.headers == ["כתובת", "שטח (מ״ר)", "מחיר (₪)"]
    assert [r.cells for r in t.rows][0] == ["התאנה 3", "82", "1,640,000"]
    assert t.caption == "להלן עסקאות שנמצאו בסביבת הנכס:" and t.notes == ["(*) המחירים כוללים מע״מ."]
    assert t.section == "2. נתוני השוואה" and t.block_index == find("כתובת | שטח (מ״ר) | מחיר (₪)").index
    paragraphs = "\n".join(b.text for b in blocks() if b.kind == "paragraph")
    assert "1,640,000" not in paragraphs and "(*) המחירים" not in paragraphs
    assert "(*) המחירים כוללים מע״מ." in find("כתובת | שטח (מ״ר) | מחיר (₪)").text


def test_a_table_continuing_on_the_next_page_is_one_table_with_row_pages():
    result = read(B1)
    assert len(result.tables) == 2
    t = result.tables[1]
    assert (t.page_start, t.page_end) == (2, 3) and len(t.rows) == 24
    assert {r.page for r in t.rows} == {2, 3} and t.rows[-1].page == 3
    assert t.caption == "טבלת התחשיב לפי רכיבים:" and t.section == "4. התחשיב"
    assert sum(1 for b in blocks() if b.kind == "table") == 2
    rows = [c for c in result.chunks if c.kind == "table_row" and c.table_index == 1]
    assert len(rows) == 24 and rows[-1].page_list == [3] and rows[0].page_list == [2]
    assert rows[0].text.startswith("טבלת התחשיב לפי רכיבים: ")
    whole = [c for c in result.chunks if c.kind == "table" and c.table_index == 1]
    assert sorted({p for c in whole for p in c.page_list}) == [2, 3]


# --- pictures --------------------------------------------------------------------------------------------------

def _pictures_unread(monkeypatch) -> ExtractionResult:
    """B2 without OCR and without the vision model: its pictures cannot be read (U4 reads them when it can)."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    return extract_pdf(B2.read_bytes(), time.monotonic() + 600, get_settings())


def test_pictures_become_unread_placeholders_in_reading_order(monkeypatch):
    result = _pictures_unread(monkeypatch)
    got = [(b.kind, b.page) for b in result.blocks]
    assert got == [("image", 1), ("heading", 1), ("paragraph", 1), ("image", 1), ("paragraph", 1),
                   ("image", 2), ("heading", 2), ("paragraph", 2)]
    pictures = [b for b in result.blocks if b.kind == "image"]
    assert all(b.status == "unread" and b.note and b.bbox and b.content_hash for b in pictures)
    assert pictures[0].content_hash == pictures[2].content_hash != pictures[1].content_hash  # the repeated logo
    assert "repeated_images" not in result.components  # twice, on two pages: each place keeps its block
    assert pictures[1].bbox[0] > 300  # the photo is on the right half of the page, in points


def test_a_pdf_with_unread_pictures_is_partly_read(monkeypatch):
    result = _pictures_unread(monkeypatch)
    comp = result.components
    assert comp["images"] == {"unread": 3} and comp["partial"] is True
    assert [u["page"] for u in comp["unread"]] == [1, 1, 2]
    assert result.warnings
    assert read(B1).components["partial"] is False and read(B1).components["images"] == {}


def test_existing_fixtures_report_their_blocks():
    from tests.integration.test_extraction_pipeline import extract

    result = extract("D1")
    comp = result.components
    assert comp["blocks"]["heading"] == 5 and comp["blocks"]["table"] == 1 and not comp["partial"]


# --- DOCX is unchanged -----------------------------------------------------------------------------------------

_BLOCK_FIELDS = ["index", "kind", "text", "section", "section_path", "label", "paragraph_no", "page", "media",
                 "source", "status", "note", "table_index", "picture_text"]
# sha256 of each fixture's blocks, chunks, tables, pages, components and warnings, taken before PDFs used the block
# model (pictures stubbed so the digest does not depend on the installed OCR languages).
_DOCX_DIGESTS = {
    "D11_synthetic_ramatgan_report.docx": "9cf8accf1c2c228b029b334fb22509cfc4bb170ca36ad7c113b86c477fbfa5b9",
    "general/H7_synthetic_telaviv_benyehuda_2023.docx":
        "8b63123e5cdb4b076d3747b41e31a52baa7b129f6e674731126effc102bb9138",
    "holdout_v2/K6_synthetic_holon_haplada_industrial_2024.docx":
        "97ca4b4caaba0ae5c9d150ea58a7825e7ef017b68e872db898964253daf9fbd0",
    "chat/CHAT_synthetic_mixed_report.docx": "f7ac8bef328ef18ca6ca6141db8945c85d0a1d6a854a36c044609bc3c62d64df",
}


@pytest.mark.parametrize("name", sorted(_DOCX_DIGESTS))
def test_docx_output_is_unchanged(name, monkeypatch):
    monkeypatch.setattr(docx, "read_picture", lambda *a: PictureReading("unread", "none", note="stub"))
    r = docx.extract_docx((FIXTURES / name).read_bytes(), time.monotonic() + 600, get_settings())
    assert all(b.bbox is None and b.method is None and b.original_text is None for b in r.blocks)
    # the per-page coverage report (U6) was added to the components after the digests were taken; it only
    # restates the blocks' statuses, and a DOCX has no pages, so its regions are listed under page None
    components = dict(r.components)
    coverage = components.pop("coverage", [])
    assert all(e["page"] is None for e in coverage)
    assert sum(len(e["regions"]) for e in coverage) == sum(
        1 for b in r.blocks if b.status == "unread" or (b.status == "read_uncertain" and b.note))
    out = {"blocks": [{k: getattr(b, k) for k in _BLOCK_FIELDS} for b in r.blocks],
           "chunks": [dataclasses.asdict(c) for c in r.chunks], "tables": [dataclasses.asdict(t) for t in r.tables],
           "pages": [dataclasses.asdict(x) for x in r.pages], "components": components, "warnings": r.warnings}
    digest = hashlib.sha256(json.dumps(out, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert digest == _DOCX_DIGESTS[name]
