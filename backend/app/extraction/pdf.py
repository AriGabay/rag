"""PDF extraction (KTD11): pdfplumber text layer + Hebrew order fix-up, quality gate, OCR fallback per
page, tables with row page provenance, section-aware chunks.

Validation errors (encrypted, corrupt, too many pages, deadline) raise ``ExtractionError`` with a
Hebrew reason. Document text is content only: nothing in it changes processing (R29).
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass

import pdfplumber
import pypdfium2 as pdfium

from app.config import Settings
from app.extraction import ocr
from app.extraction.base import ExtractionError, ExtractionResult, PageResult
from app.extraction.chunking import chunk_document, is_heading
from app.extraction.hebrew import QUALITY_THRESHOLD, fix_text_lines, page_is_visual, quality_score
from app.extraction.tables import RawTable, assemble_tables, logical_row

log = logging.getLogger(__name__)

MSG_ENCRYPTED = "הקובץ מוגן בסיסמה ולא ניתן לעבד אותו"
MSG_CORRUPT = "הקובץ פגום או שאינו PDF תקין"
MSG_TOO_MANY_PAGES = "המסמך ארוך מהמותר: {n} עמודים (המקסימום הוא {limit} עמודים)"
MSG_DEADLINE = "חריגה מזמן העיבוד המותר למסמך"
WARN_NO_OCR = "זיהוי טקסט (OCR) אינו זמין בשרת; עמודים ללא שכבת טקסט תקינה סומנו כלא מעובדים"
WARN_PLUMBER = "שכבת הטקסט של הקובץ לא נקראה; כל העמודים עברו זיהוי טקסט (OCR)"
WARN_PAGE_FAILED = "עמוד {page}: לא ניתן היה לחלץ טקסט באיכות מספקת"


@dataclass
class _Line:
    top: float
    text: str


@dataclass
class _PageOut:
    page: PageResult
    lines: list[_Line]
    tables: list[RawTable]


def check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise ExtractionError(MSG_DEADLINE, True)


def open_pdf(data: bytes, max_pages: int) -> pdfium.PdfDocument:
    try:
        doc = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ExtractionError(MSG_ENCRYPTED, True) from None
        raise ExtractionError(MSG_CORRUPT, True) from None
    except Exception:  # noqa: BLE001
        raise ExtractionError(MSG_CORRUPT, True) from None
    n = len(doc)
    if n == 0:
        doc.close()
        raise ExtractionError(MSG_CORRUPT, True)
    if n > max_pages:
        doc.close()
        raise ExtractionError(MSG_TOO_MANY_PAGES.format(n=n, limit=max_pages), True)
    return doc


def _text_layer(page) -> tuple[list[_Line], list[tuple[float, list[list[str]]]], bool]:
    raw_lines = page.extract_text_lines(return_chars=False)
    raw_texts = [ln["text"] for ln in raw_lines]
    visual = page_is_visual(raw_texts)
    fixed = fix_text_lines(raw_texts, default_visual=visual)
    lines = [_Line(float(ln["top"]), t) for ln, t in zip(raw_lines, fixed, strict=True)]
    tables = []
    for t in page.find_tables():
        grid = t.extract()
        if not grid or max(len(r) for r in grid) < 2:
            continue
        # pdfplumber returns columns left -> right: reverse to logical order, fix each cell's text.
        rows = [logical_row(r, visual_default=True if visual is None else visual) for r in grid]
        tables.append((float(t.bbox[1]), rows))
    return lines, tables, bool(visual)


def _ocr_page(doc, index: int, settings: Settings) -> tuple[list[_Line], list[tuple[float, list[list[str]]]]]:
    img = ocr.render_page(doc, index, settings.ocr_dpi, settings.max_render_pixels)
    result = ocr.ocr_page_image(img, settings.ocr_languages)
    texts = fix_text_lines([ln.text for ln in result.lines])
    lines = [_Line(ln.top, t) for ln, t in zip(result.lines, texts, strict=True)]
    return lines, [(t.top, t.rows) for t in result.tables]


def _process_page(doc, plumber, index: int, settings: Settings, warnings: list[str]) -> _PageOut:
    page_no = index + 1
    lines: list[_Line] = []
    tables: list[tuple[float, list[list[str]]]] = []
    if plumber is not None:
        page = plumber.pages[index]
        try:
            lines, tables, _ = _text_layer(page)
        except Exception:  # noqa: BLE001 - a broken page falls through to OCR
            log.warning("text layer failed on page %s", page_no, exc_info=True)
            lines, tables = [], []
        finally:
            page.close()
    text = "\n".join(ln.text for ln in lines)
    quality = quality_score(text)
    if quality >= QUALITY_THRESHOLD:
        return _PageOut(PageResult(page_no, text, "text_layer", quality, True), lines,
                        [RawTable(page_no, rows, ocr=False, top=top) for top, rows in tables])

    if not ocr.ocr_available(settings.ocr_languages):
        if WARN_NO_OCR not in warnings:
            warnings.append(WARN_NO_OCR)
        return _PageOut(PageResult(page_no, text, "failed", quality, False), [], [])

    ocr_lines, ocr_tables = _ocr_page(doc, index, settings)
    ocr_text = "\n".join(ln.text for ln in ocr_lines)
    ocr_quality = quality_score(ocr_text)
    if ocr_quality >= QUALITY_THRESHOLD:
        return _PageOut(PageResult(page_no, ocr_text, "ocr", ocr_quality, True), ocr_lines,
                        [RawTable(page_no, rows, ocr=True, top=top) for top, rows in ocr_tables])
    # Still bad: the page is incomplete; garbled text never counts as decoded and is not indexed.
    warnings.append(WARN_PAGE_FAILED.format(page=page_no))
    best_text, best_q = (ocr_text, ocr_quality) if ocr_quality >= quality else (text, quality)
    return _PageOut(PageResult(page_no, best_text, "failed", best_q, False), [], [])


def extract_pdf(data: bytes, deadline: float, settings: Settings) -> ExtractionResult:
    doc = open_pdf(data, settings.max_pages)
    warnings: list[str] = []
    try:
        try:
            plumber = pdfplumber.open(io.BytesIO(data))
        except Exception:  # noqa: BLE001 - pdfium opened it; pdfminer did not
            plumber = None
            warnings.append(WARN_PLUMBER)
        try:
            outs: list[_PageOut] = []
            for index in range(len(doc)):
                check_deadline(deadline)
                outs.append(_process_page(doc, plumber, index, settings, warnings))
        finally:
            if plumber is not None:
                plumber.close()
        page_count = len(doc)
    finally:
        doc.close()

    raws: list[RawTable] = []
    section: str | None = None
    for out in outs:
        headings = [(ln.top, ln.text.strip()) for ln in out.lines if is_heading(ln.text)]
        for k, raw in enumerate(sorted(out.tables, key=lambda t: t.top)):
            above = [h for top, h in headings if top < raw.top]
            raw.section = above[-1] if above else section
            raw.first_on_page = k == 0
            raws.append(raw)
        if headings:
            section = headings[-1][1]

    tables = assemble_tables(raws)
    pages = [o.page for o in outs]
    chunks = chunk_document([(p.page_no, p.text) for p in pages if p.ok], tables)
    return ExtractionResult(page_count=page_count, pages=pages, tables=tables, chunks=chunks, warnings=warnings)
