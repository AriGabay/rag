"""PDF extraction (KTD11, KTD3): pdfplumber text layer + Hebrew order fix-up, quality gate, OCR fallback per
page, and the document as blocks in reading order, chunked like a DOCX.

Each page yields its text lines, its tables and its pictures with their positions. Walking the pages top to
bottom gives the blocks: numbered headings (``9.``, and ``9.1`` / ``9.1.2`` sub-sections under their parent),
paragraphs, tables at their place with the text above them as caption and the note lines below them as notes
(their cells are not repeated as paragraph text), and pictures. A table continuing on the next page is one table
whose rows keep their own pages. Every block keeps its page and its region on the page.

The text layer is cleaned before anything reads it:

- glyphs drawn twice at (almost) the same place, the way some producers fake bold, are kept once;
- a line whose last letters were drawn on a slightly different baseline is joined back into one line;
- a page whose words carry no orientation evidence (a title page) takes the orientation of the document.

Pictures on a text-layer page are not read here: each becomes an ``image`` block marked ``unread`` with its
region and a hash of its image bytes (the same picture on many pages has one hash), so a document with pictures
is partly read until they are. Validation errors (encrypted, corrupt, too many pages, deadline) raise
``ExtractionError`` with a Hebrew reason. Document text is content only: nothing in it changes processing (R29).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import statistics
from dataclasses import dataclass, field

import pdfplumber
import pypdfium2 as pdfium

from app.config import Settings
from app.extraction import ocr
from app.extraction.base import Block, ExtractionError, ExtractionResult, PageResult, check_deadline
from app.extraction.chunking import chunk_blocks, heading_label, is_footer
from app.extraction.hebrew import (
    QUALITY_THRESHOLD,
    document_is_visual,
    fix_text_lines,
    page_is_visual,
    quality_score,
)
from app.extraction.tables import RawTable, assemble_tables, logical_row

log = logging.getLogger(__name__)

# The reader that produced a PDF's blocks; a version read by an older one is reprocessed.
READER_VERSION = "pdf-blocks-v1"

MSG_ENCRYPTED = "הקובץ מוגן בסיסמה ולא ניתן לעבד אותו"
MSG_CORRUPT = "הקובץ פגום או שאינו PDF תקין"
MSG_TOO_MANY_PAGES = "המסמך ארוך מהמותר: {n} עמודים (המקסימום הוא {limit} עמודים)"
WARN_NO_OCR = "זיהוי טקסט (OCR) אינו זמין בשרת; עמודים ללא שכבת טקסט תקינה סומנו כלא מעובדים"
WARN_PLUMBER = "שכבת הטקסט של הקובץ לא נקראה; כל העמודים עברו זיהוי טקסט (OCR)"
WARN_PAGE_FAILED = "עמוד {page}: לא ניתן היה לחלץ טקסט באיכות מספקת"
WARN_PICTURES = "{n} תמונות לא נקראו"
NOTE_NOT_READ = "התמונה עדיין לא נקראה"

DEDUPE_TOLERANCE = 1.0  # points: a glyph repeated closer than this (same font and size) is one glyph
_HEB_FRAGMENT = re.compile(r"[א-ת]{1,3}")
_HEB_LETTER = re.compile(r"[א-ת]")
_BOLD = re.compile(r"bold|black|heavy", re.IGNORECASE)
# a note under a table: "(*) ...", "* ...", "הערה: ...", "מקור: ..."
_NOTE = re.compile(r"^\s*(?:\(\s*\*+\s*\)|\*+\s|הערה\s*:|הערות\s*:|מקור\s*:)")


@dataclass
class _Line:
    top: float
    text: str
    bottom: float | None = None  # None for OCR lines (no box)
    x0: float | None = None
    x1: float | None = None
    size: float = 0.0
    bold: bool = False
    in_table: bool = False

    @property
    def bbox(self) -> list[float] | None:
        if self.bottom is None or self.x0 is None or self.x1 is None:
            return None
        return [round(self.x0, 1), round(self.top, 1), round(self.x1, 1), round(self.bottom, 1)]


@dataclass
class _Picture:
    bbox: list[float]
    content_hash: str


@dataclass
class _PageOut:
    page: PageResult
    lines: list[_Line]
    tables: list[RawTable]
    pictures: list[_Picture] = field(default_factory=list)
    raw_undecided: list[str] | None = None  # text-layer lines whose orientation the page alone did not decide
    raw_texts: list[str] = field(default_factory=list)


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


# --- text layer ------------------------------------------------------------------------------------------------

def _line_size(line: dict) -> float:
    sizes = [float(c.get("size") or 0) for c in line.get("chars") or []]
    return statistics.median(sizes) if sizes else float(line["bottom"] - line["top"])


def _is_fragment_of(frag: dict, line: dict) -> bool:
    """A 1-3 letter Hebrew piece drawn inside ``line``'s vertical band and touching its left or right end: the
    end of a word whose last glyphs sit on another baseline ("8. התחשי" + "ב")."""
    text = frag["text"].strip()
    if not _HEB_FRAGMENT.fullmatch(text) or not line["text"].strip():
        return False
    size = _line_size(line)
    if not line["top"] < frag["top"] < line["bottom"] - 0.1 * size:
        return False
    left_gap, right_gap = line["x0"] - frag["x1"], frag["x0"] - line["x1"]
    if -1 <= left_gap <= 0.5 * size:
        return bool(_HEB_LETTER.match(line["text"]))
    if -1 <= right_gap <= 0.5 * size:
        return bool(_HEB_LETTER.match(line["text"][-1]))
    return False


def _rejoin(lines: list[dict]) -> list[dict]:
    """Join split-off word ends back into their line, in visual (left to right) order like the rest of the raw
    line, so the orientation fix treats the joined line as one."""
    out: list[dict] = []
    for ln in lines:
        prev = out[-1] if out else None
        if prev is not None and _is_fragment_of(ln, prev):
            left, right = (ln, prev) if ln["x1"] <= prev["x0"] + 1 else (prev, ln)
            out[-1] = prev | {"text": left["text"] + right["text"], "x0": min(ln["x0"], prev["x0"]),
                              "x1": max(ln["x1"], prev["x1"]), "bottom": max(ln["bottom"], prev["bottom"]),
                              "chars": (left.get("chars") or []) + (right.get("chars") or [])}
            continue
        out.append(ln)
    return out


def _picture_hash(img: dict) -> str | None:
    try:
        return hashlib.sha256(img["stream"].get_rawdata()).hexdigest()
    except Exception:  # noqa: BLE001 - an unreadable image stream still is a picture on the page
        return None


def _text_layer(page) -> tuple[list[_Line], list[tuple[list[float], list[list[str]]]], bool | None, list[str],
                                list[_Picture]]:
    page = page.dedupe_chars(tolerance=DEDUPE_TOLERANCE)
    raw_lines = _rejoin(page.extract_text_lines(return_chars=True))
    raw_texts = [ln["text"] for ln in raw_lines]
    visual = page_is_visual(raw_texts)
    fixed = fix_text_lines(raw_texts, default_visual=visual)
    lines = []
    for ln, t in zip(raw_lines, fixed, strict=True):
        chars = ln.get("chars") or []
        bold = sum(1 for c in chars if _BOLD.search(c.get("fontname") or "")) * 2 > len(chars) if chars else False
        lines.append(_Line(float(ln["top"]), t, float(ln["bottom"]), float(ln["x0"]), float(ln["x1"]),
                           _line_size(ln), bold))
    tables = []
    for t in page.find_tables():
        grid = t.extract()
        if not grid or max(len(r) for r in grid) < 2:
            continue
        # pdfplumber returns columns left -> right: reverse to logical order, fix each cell's text.
        rows = [logical_row(r, visual_default=True if visual is None else visual) for r in grid]
        tables.append(([round(float(x), 1) for x in t.bbox], rows))
    pictures = []
    for img in page.images:
        bbox = [round(float(img[k]), 1) for k in ("x0", "top", "x1", "bottom")]
        digest = _picture_hash(img)
        if digest and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            pictures.append(_Picture(bbox, digest))
    return lines, tables, visual, raw_texts, pictures


def _mark_table_lines(lines: list[_Line], tables: list[RawTable]) -> None:
    """Lines inside a table's region are its cells: they stay in the page text, not in paragraph blocks."""
    for ln in lines:
        if ln.bottom is None:
            continue
        mid = (ln.top + ln.bottom) / 2
        for t in tables:
            if t.bbox and t.bbox[1] <= mid <= t.bbox[3] and ln.x0 < t.bbox[2] and ln.x1 > t.bbox[0]:
                ln.in_table = True
                break


def _ocr_page(doc, index: int, settings: Settings) -> tuple[list[_Line], list[tuple[float, list[list[str]]]]]:
    img = ocr.render_page(doc, index, settings.ocr_dpi, settings.max_render_pixels)
    result = ocr.ocr_page_image(img, settings.ocr_languages)
    texts = fix_text_lines([ln.text for ln in result.lines])
    # the OCR page text repeats each table row as a line at the table's place (top + k * 0.01)
    row_lines = {(round(t.top + k * 0.01, 4), " ".join(c for c in row if c))
                 for t in result.tables for k, row in enumerate(t.rows)}
    lines = [_Line(ln.top, t, in_table=(round(ln.top, 4), ln.text) in row_lines)
             for ln, t in zip(result.lines, texts, strict=True)]
    return lines, [(t.top, t.rows) for t in result.tables]


def _process_page(doc, plumber, index: int, settings: Settings, warnings: list[str]) -> _PageOut:
    page_no = index + 1
    lines: list[_Line] = []
    tables: list[tuple[list[float], list[list[str]]]] = []
    pictures: list[_Picture] = []
    visual: bool | None = None
    raw_texts: list[str] = []
    if plumber is not None:
        page = plumber.pages[index]
        try:
            lines, tables, visual, raw_texts, pictures = _text_layer(page)
        except Exception:  # noqa: BLE001 - a broken page falls through to OCR
            log.warning("text layer failed on page %s", page_no, exc_info=True)
            lines, tables, raw_texts, pictures = [], [], [], []
        finally:
            page.close()
    text = "\n".join(ln.text for ln in lines)
    quality = quality_score(text)
    if quality >= QUALITY_THRESHOLD:
        raws = [RawTable(page_no, rows, ocr=False, top=bbox[1], bbox=bbox) for bbox, rows in tables]
        _mark_table_lines(lines, raws)
        return _PageOut(PageResult(page_no, text, "text_layer", quality, True), lines, raws, pictures,
                        raw_texts if visual is None else None, raw_texts)

    if not ocr.ocr_available(settings.ocr_languages):
        if WARN_NO_OCR not in warnings:
            warnings.append(WARN_NO_OCR)
        return _PageOut(PageResult(page_no, text, "failed", quality, False), [], [])

    try:
        ocr_lines, ocr_tables = _ocr_page(doc, index, settings)
    except RuntimeError:  # pytesseract raises RuntimeError on its per-call timeout
        log.warning("OCR timed out on page %s", page_no)
        warnings.append(WARN_PAGE_FAILED.format(page=page_no))
        return _PageOut(PageResult(page_no, text, "failed", quality, False), [], [])
    ocr_text = "\n".join(ln.text for ln in ocr_lines)
    ocr_quality = quality_score(ocr_text)
    if ocr_quality >= QUALITY_THRESHOLD:
        return _PageOut(PageResult(page_no, ocr_text, "ocr", ocr_quality, True), ocr_lines,
                        [RawTable(page_no, rows, ocr=True, top=top) for top, rows in ocr_tables])
    # Still bad: the page is incomplete; garbled text never counts as decoded and is not indexed.
    warnings.append(WARN_PAGE_FAILED.format(page=page_no))
    best_text, best_q = (ocr_text, ocr_quality) if ocr_quality >= quality else (text, quality)
    return _PageOut(PageResult(page_no, best_text, "failed", best_q, False), [], [])


def _orient_undecided_pages(outs: list[_PageOut]) -> None:
    """Pages whose own words did not decide their orientation take the document's."""
    decided = [o.raw_texts for o in outs if o.raw_undecided is None and o.raw_texts]
    visual = document_is_visual(decided)
    if visual is None:
        return
    for o in outs:
        if o.raw_undecided is None:
            continue
        for ln, t in zip(o.lines, fix_text_lines(o.raw_undecided, default_visual=visual), strict=True):
            ln.text = t
        o.page.text = "\n".join(ln.text for ln in o.lines)
        o.page.quality = quality_score(o.page.text)


# --- blocks ----------------------------------------------------------------------------------------------------

@dataclass
class _Walker:
    blocks: list[Block] = field(default_factory=list)
    raws: list[RawTable] = field(default_factory=list)
    table_raw: dict[int, RawTable] = field(default_factory=dict)  # table block index -> its piece
    captions: dict[int, str] = field(default_factory=dict)  # id(raw) -> caption
    notes: dict[int, list[str]] = field(default_factory=dict)  # id(raw) -> note lines under it
    path: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    last_text: str = ""

    def add(self, kind: str, text: str, page: int, bbox: list[float] | None, method: str, **kw) -> Block:
        b = Block(index=len(self.blocks), kind=kind, text=text, section=self.path[-1] if self.path else None,
                  section_path=list(self.path), page=page, bbox=bbox, method=method, reader_version=READER_VERSION,
                  **kw)
        self.blocks.append(b)
        return b

    def heading_depth(self, text: str) -> tuple[int, str] | None:
        """(depth, label) when the line opens a section: any top-level numbered heading, a sub-section only
        directly under its parent ("9.1" under "9."), so a line starting with "2.75" is no heading."""
        label = heading_label(text)
        if label is None:
            return None
        depth = label.count(".") + 1
        if depth > 1 and (len(self.labels) < depth - 1 or self.labels[depth - 2] != label.rsplit(".", 1)[0]):
            return None
        return depth, label

    def page(self, out: _PageOut) -> None:
        page_no = out.page.page_no
        method = out.page.method
        items: list[tuple[float, int, object]] = []
        for ln in out.lines:
            if ln.text.strip() and not ln.in_table and not is_footer(ln.text):
                items.append((ln.top, 1, ln))
        for raw in out.tables:
            items.append((raw.top, 0, raw))
        for pic in out.pictures:
            items.append((pic.bbox[1], 0, pic))
        items.sort(key=lambda x: (x[0], x[1]))
        para: list[_Line] = []
        notes_of: RawTable | None = None
        notes_bottom = 0.0

        def flush() -> None:
            nonlocal para
            if para:
                text = "\n".join(ln.text.strip() for ln in para)
                boxes = [ln.bbox for ln in para if ln.bbox]
                bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                        max(b[3] for b in boxes)] if boxes else None
                self.add("paragraph", text, page_no, bbox, method, source="ocr" if method == "ocr" else "text")
                self.last_text = text
            para = []

        for _, _, item in items:
            if isinstance(item, RawTable):
                flush()
                item.section = self.path[-1] if self.path else None
                b = self.add("table", "", page_no, item.bbox, method, source="ocr" if item.ocr else "text",
                             status="read_uncertain" if item.ocr else "read")
                self.table_raw[b.index] = item
                self.captions[id(item)] = _caption(self.last_text)
                self.raws.append(item)
                notes_of, notes_bottom = (item, item.bbox[3]) if item.bbox else (None, 0.0)
                continue
            if isinstance(item, _Picture):
                flush()
                self.add("image", "", page_no, item.bbox, "none", source="none", status="unread",
                         note=NOTE_NOT_READ, content_hash=item.content_hash)
                notes_of = None
                continue
            ln: _Line = item
            text = ln.text.strip()
            if notes_of is not None:
                if _NOTE.match(text) and ln.bottom is not None and ln.top - notes_bottom <= 2 * max(ln.size, 1):
                    self.notes.setdefault(id(notes_of), []).append(text)
                    notes_bottom = ln.bottom
                    continue
                notes_of = None
            found = self.heading_depth(text)
            if found is not None:
                flush()
                depth, label = found
                self.path, self.labels = self.path[: depth - 1] + [text], self.labels[: depth - 1] + [label]
                self.add("heading", text, page_no, ln.bbox, method, label=label,
                         source="ocr" if method == "ocr" else "text")
                self.last_text = text
                continue
            if para and _breaks(para[-1], ln, para):
                flush()
            para.append(ln)
        flush()

    def finish(self) -> tuple[list[Block], list]:
        """Assemble the table pieces (a continuation joins the table it continues and leaves no block of its
        own), renumber the blocks and give each table its block, caption, notes and text."""
        from app.extraction.docx import render_table

        tables = assemble_tables(self.raws)
        kept: list[Block] = []
        renumber: dict[int, int] = {}
        for b in self.blocks:
            raw = self.table_raw.get(b.index)
            if raw is not None and (raw.table_index is None or raw.continues):
                continue
            renumber[b.index] = len(kept)
            b.index = len(kept)
            kept.append(b)
        for old, raw in self.table_raw.items():
            if raw.table_index is None:
                continue
            t = tables[raw.table_index]
            t.notes.extend(self.notes.get(id(raw), []))
            if not raw.continues:
                b = kept[renumber[old]]
                b.table_index, t.block_index = t.index, b.index
                t.caption = self.captions.get(id(raw)) or None
        for t in tables:
            if t.block_index is not None:
                kept[t.block_index].text = render_table(t)
        return kept, tables


def _caption(text: str) -> str:
    """The text introducing a table: the paragraph above it, or its last line when that paragraph is long."""
    if len(text) <= 300:
        return text
    return text.splitlines()[-1][:300]


def _breaks(prev: _Line, ln: _Line, para: list[_Line]) -> bool:
    """Whether ``ln`` starts a new paragraph after ``prev``: a larger gap than the line spacing, or a change of
    type size or weight. OCR lines have no box, so only their spacing is compared."""
    if prev.bottom is None or ln.bottom is None:
        if len(para) < 2:
            return False
        pitches = [b.top - a.top for a, b in zip(para, para[1:], strict=False)]
        return ln.top - prev.top > 1.5 * statistics.median(pitches)
    height = max(prev.bottom - prev.top, ln.bottom - ln.top, 1.0)
    if ln.top - prev.bottom > 0.8 * height:
        return True
    if prev.bold != ln.bold:
        return True
    return abs(prev.size - ln.size) > 0.2 * max(prev.size, ln.size, 1.0)


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

    _orient_undecided_pages(outs)
    walker = _Walker()
    for out in outs:
        for k, raw in enumerate(sorted(out.tables, key=lambda t: t.top)):
            raw.first_on_page = k == 0
        if out.page.ok:
            walker.page(out)
    blocks, tables = walker.finish()
    pages = [o.page for o in outs]
    chunks = chunk_blocks(blocks, tables)
    result = ExtractionResult(page_count=page_count, pages=pages, tables=tables, chunks=chunks, warnings=warnings,
                              blocks=blocks)
    unread = result.components["unread"]
    if unread:
        warnings.append(WARN_PICTURES.format(n=len(unread)))
    return result
