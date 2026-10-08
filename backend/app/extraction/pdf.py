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

Content the text layer does not cover is found and read by ``app.extraction.regions`` (KTD4, KTD5): image
objects and vector ink outside the text layer, each read once per content (OCR, or the vision model on a crop at
legible scale) or reported ``unread`` with its reason, so a document with unread regions is partly read. A region
becomes an ``image`` block at its place with its region and content hash; a table read from it becomes a table
(source ``ocr`` or ``vision``) whose rows keep the page, with the text above it as caption and the note lines
below it as notes. A region the text layer already holds (a searchable scan) becomes no block, and a page whose
regions added text is ``mixed``. A picture repeated through the document (a logo on every page, watermark tiles)
is page furniture: one block at its first occurrence, kept out of the page text, not making any page ``mixed``,
and counted in the ingestion report (``repeated_images``).

A font whose character map is broken (the page shows the right Hebrew letter, the text layer another character)
is found and repaired per document by ``app.extraction.fontmap`` (KTD6): the words of every page are collected
while the pages are read, the suspect ``font × character`` pairs are verified against OCR of their rendered glyphs,
and only the pages holding a suspect character are read again from a corrected text layer. Corrected text is the
text of record (blocks, tables, pages, chunks); a block whose text changed keeps the text as extracted in
``original_text``, and the corrections go to the ingestion report. A corruption no verified repair fixed leaves
the text as extracted: its blocks are ``read_uncertain`` with the reason, the page quality is scored down, and the
document is partly read. Such a page keeps its text layer rather than falling back to whole-page OCR: its letters
are otherwise readable, and the OCR of its glyphs is what did not agree.

Validation errors (encrypted, corrupt, too many pages, deadline) raise ``ExtractionError`` with a Hebrew reason.
Document text is content only: nothing in it changes processing (R29).
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
from app.extraction import fontmap, ocr
from app.extraction.base import (
    Block,
    ExtractionError,
    ExtractionResult,
    PageResult,
    TableResult,
    TableRow,
    check_deadline,
)
from app.extraction.chunking import chunk_blocks, heading_label, is_footer
from app.extraction.fontmap import FontMapConfig, FontMapFix, WordSample
from app.extraction.hebrew import (
    QUALITY_THRESHOLD,
    document_is_visual,
    fix_text_lines,
    page_is_visual,
    quality_score,
)
from app.extraction.images import PictureReading, VisionReader
from app.extraction.regions import PageLayer, ReadingCache, Region, mark_repeated, read_regions
from app.extraction.tables import RawTable, assemble_tables, logical_row, units_for

log = logging.getLogger(__name__)

# The reader that produced a PDF's blocks; a version read by an older one is reprocessed.
READER_VERSION = "pdf-blocks-v5"

MSG_ENCRYPTED = "הקובץ מוגן בסיסמה ולא ניתן לעבד אותו"
MSG_CORRUPT = "הקובץ פגום או שאינו PDF תקין"
MSG_TOO_MANY_PAGES = "המסמך ארוך מהמותר: {n} עמודים (המקסימום הוא {limit} עמודים)"
WARN_NO_OCR = "זיהוי טקסט (OCR) אינו זמין בשרת; עמודים ללא שכבת טקסט תקינה סומנו כלא מעובדים"
WARN_PLUMBER = "שכבת הטקסט של הקובץ לא נקראה; כל העמודים עברו זיהוי טקסט (OCR)"
WARN_PAGE_FAILED = "עמוד {page}: לא ניתן היה לחלץ טקסט באיכות מספקת"
WARN_PICTURES = "{n} תמונות לא נקראו"
WARN_FONTMAP = "{n} קטעי טקסט נקראו עם תווים שגויים שלא ניתן היה לתקן (מיפוי גופן פגום)"
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
    original: str | None = None  # the text as extracted, when a font-map correction changed it
    uncertain: str | None = None  # why its text is uncertain (a font-map corruption left unrepaired)
    raw: str = ""  # a corrected line's raw text and chars, to restore its original after a reorientation
    fm_chars: list[dict] | None = None

    @property
    def bbox(self) -> list[float] | None:
        if self.bottom is None or self.x0 is None or self.x1 is None:
            return None
        return [round(self.x0, 1), round(self.top, 1), round(self.x1, 1), round(self.bottom, 1)]


@dataclass
class _PageOut:
    page: PageResult
    lines: list[_Line]
    tables: list[RawTable]
    pictures: list[Region] = field(default_factory=list)
    chars: list[tuple[float, float, float, float]] = field(default_factory=list)  # text-layer character boxes
    raw_undecided: list[str] | None = None  # text-layer lines whose orientation the page alone did not decide
    raw_texts: list[str] = field(default_factory=list)
    width: float = 0.0  # page size in points
    height: float = 0.0
    words: list[WordSample] = field(default_factory=list)  # the text layer's words with their fonts (font maps)
    corruption: float = 0.0  # share of the page's Hebrew words with an unrepaired font-map corruption
    warnings: list[str] = field(default_factory=list)


@dataclass
class _TextLayer:
    lines: list[_Line] = field(default_factory=list)
    tables: list[tuple[list[float], list[list[str]], str | None]] = field(default_factory=list)  # + uncertain
    visual: bool | None = None
    raw_texts: list[str] = field(default_factory=list)
    pictures: list[Region] = field(default_factory=list)
    chars: list[tuple[float, float, float, float]] = field(default_factory=list)
    words: list[WordSample] = field(default_factory=list)
    corruption: float = 0.0


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


def _uncertain_in(chars: list[dict], bbox) -> str | None:
    """Why text inside ``bbox`` is uncertain: the reason of an unrepaired font-map char there, or None."""
    x0, top, x1, bottom = bbox
    return next((c["fontmap_uncertain"] for c in chars if c.get("fontmap_uncertain")
                 and x0 <= (c["x0"] + c["x1"]) / 2 <= x1 and top <= (c["top"] + c["bottom"]) / 2 <= bottom), None)


def _text_layer(page, index: int, fix: FontMapFix | None = None) -> _TextLayer:
    """The page's text layer. Without ``fix`` it also collects the page's words for font-map detection; with it
    the page is read from its corrected text layer (``FontMapFix.correct_page``)."""
    page = page.dedupe_chars(tolerance=DEDUPE_TOLERANCE)
    out = _TextLayer()
    if fix is None:
        out.words = fontmap.words_of_page(page, index)
    else:
        corrected = fix.correct_page(page)
        page, out.corruption = corrected.page, corrected.corruption
    raw_lines = _rejoin(page.extract_text_lines(return_chars=True))
    out.raw_texts = raw_texts = [ln["text"] for ln in raw_lines]
    out.visual = visual = page_is_visual(raw_texts)
    fixed = fix_text_lines(raw_texts, default_visual=visual)
    for ln, t in zip(raw_lines, fixed, strict=True):
        chars = ln.get("chars") or []
        bold = sum(1 for c in chars if _BOLD.search(c.get("fontname") or "")) * 2 > len(chars) if chars else False
        line = _Line(float(ln["top"]), t, float(ln["bottom"]), float(ln["x0"]), float(ln["x1"]), _line_size(ln),
                     bold)
        if fix is not None:
            line.uncertain = fix.uncertain_reason(chars)
            line.original = fix.original_line(ln["text"], t, chars)
            if line.original is not None:
                line.raw, line.fm_chars = ln["text"], chars
        out.lines.append(line)
    for t in page.find_tables():
        grid = t.extract()
        if not grid or max(len(r) for r in grid) < 2:
            continue
        # pdfplumber returns columns left -> right: reverse to logical order, fix each cell's text.
        rows = [logical_row(r, visual_default=True if visual is None else visual) for r in grid]
        reason = _uncertain_in(page.chars, t.bbox) if fix is not None else None
        out.tables.append(([round(float(x), 1) for x in t.bbox], rows, reason))
    for img in page.images:
        bbox = [round(float(img[k]), 1) for k in ("x0", "top", "x1", "bottom")]
        digest = _picture_hash(img)
        if digest and bbox[2] > bbox[0] and bbox[3] > bbox[1]:
            size = img.get("srcsize")
            srcsize = (int(size[0]), int(size[1])) if size and size[0] and size[1] else None
            out.pictures.append(Region(page.page_number, bbox, "image", digest, srcsize))
    out.chars = [(float(c["x0"]), float(c["top"]), float(c["x1"]), float(c["bottom"])) for c in page.chars]
    return out


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


def _process_page(doc, plumber, index: int, settings: Settings, warnings: list[str],
                  fix: FontMapFix | None = None) -> _PageOut:
    page_no = index + 1
    layer = _TextLayer()
    if plumber is not None:
        page = plumber.pages[index]
        try:
            layer = _text_layer(page, index, fix)
        except Exception:  # noqa: BLE001 - a broken page falls through to OCR
            log.warning("text layer failed on page %s", page_no, exc_info=True)
            layer = _TextLayer()
        finally:
            page.close()
    lines = layer.lines
    text = "\n".join(ln.text for ln in lines)
    quality = quality_score(text, corruption=layer.corruption)
    # an unrepaired font-map corruption scores the page down but keeps its text layer (its blocks are uncertain)
    if (quality_score(text) if layer.corruption else quality) >= QUALITY_THRESHOLD:
        raws = [RawTable(page_no, rows, ocr=False, top=bbox[1], bbox=bbox, uncertain=reason)
                for bbox, rows, reason in layer.tables]
        _mark_table_lines(lines, raws)
        return _PageOut(PageResult(page_no, text, "text_layer", quality, True), lines, raws, layer.pictures,
                        layer.chars, layer.raw_texts if layer.visual is None else None, layer.raw_texts,
                        words=layer.words, corruption=layer.corruption)

    if not ocr.ocr_available(settings.ocr_languages):
        if WARN_NO_OCR not in warnings:
            warnings.append(WARN_NO_OCR)
        return _PageOut(PageResult(page_no, text, "failed", quality, False), [], [], words=layer.words)

    try:
        ocr_lines, ocr_tables = _ocr_page(doc, index, settings)
    except RuntimeError:  # pytesseract raises RuntimeError on its per-call timeout
        log.warning("OCR timed out on page %s", page_no)
        warnings.append(WARN_PAGE_FAILED.format(page=page_no))
        return _PageOut(PageResult(page_no, text, "failed", quality, False), [], [], words=layer.words)
    ocr_text = "\n".join(ln.text for ln in ocr_lines)
    ocr_quality = quality_score(ocr_text)
    if ocr_quality >= QUALITY_THRESHOLD:
        return _PageOut(PageResult(page_no, ocr_text, "ocr", ocr_quality, True), ocr_lines,
                        [RawTable(page_no, rows, ocr=True, top=top) for top, rows in ocr_tables], words=layer.words)
    # Still bad: the page is incomplete; garbled text never counts as decoded and is not indexed.
    warnings.append(WARN_PAGE_FAILED.format(page=page_no))
    best_text, best_q = (ocr_text, ocr_quality) if ocr_quality >= quality else (text, quality)
    return _PageOut(PageResult(page_no, best_text, "failed", best_q, False), [], [], words=layer.words)


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
            if ln.fm_chars is not None:
                ln.original = FontMapFix.original_line(ln.raw, t, ln.fm_chars)
        o.page.text = "\n".join(ln.text for ln in o.lines)
        o.page.quality = quality_score(o.page.text, corruption=o.corruption)


# --- blocks ----------------------------------------------------------------------------------------------------

@dataclass
class _Walker:
    blocks: list[Block] = field(default_factory=list)
    raws: list[RawTable] = field(default_factory=list)
    table_raw: dict[int, RawTable] = field(default_factory=dict)  # table block index -> its piece
    captions: dict[int, str] = field(default_factory=dict)  # id(raw) -> caption
    notes: dict[int, list[str]] = field(default_factory=dict)  # id(raw or region) -> note lines under it
    pictures: dict[int, tuple[Region, PictureReading, str]] = field(default_factory=dict)  # image block -> reading
    path: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    last_text: str = ""
    uncertain: list[Block] = field(default_factory=list)  # blocks left uncertain by an unrepaired font map

    def add(self, kind: str, text: str, page: int, bbox: list[float] | None, method: str, **kw) -> Block:
        b = Block(index=len(self.blocks), kind=kind, text=text, section=self.path[-1] if self.path else None,
                  section_path=list(self.path), page=page, bbox=bbox, method=method, reader_version=READER_VERSION,
                  **kw)
        self.blocks.append(b)
        return b

    def add_text(self, kind: str, lines: list[_Line], page: int, bbox: list[float] | None, method: str,
                 **kw) -> Block:
        """A heading or paragraph from its lines: the corrected text, the text as extracted when a font-map
        correction changed it, and ``read_uncertain`` with the reason when a corruption stays unrepaired."""
        text = "\n".join(ln.text.strip() for ln in lines)
        original = "\n".join((ln.original if ln.original is not None else ln.text).strip() for ln in lines)
        reason = next((ln.uncertain for ln in lines if ln.uncertain), None)
        if original != text:
            kw["original_text"] = original
        if reason:
            kw |= {"status": "read_uncertain", "note": reason}
        b = self.add(kind, text, page, bbox, method, source="ocr" if method == "ocr" else "text", **kw)
        if reason:
            self.uncertain.append(b)
        return b

    def mark_uncertain(self, b: Block, reason: str) -> None:
        b.status, b.note = "read_uncertain", b.note or reason
        if all(b is not u for u in self.uncertain):
            self.uncertain.append(b)

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
        notes_of: RawTable | Region | None = None
        notes_bottom = 0.0

        def flush() -> None:
            nonlocal para
            if para:
                boxes = [ln.bbox for ln in para if ln.bbox]
                bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                        max(b[3] for b in boxes)] if boxes else None
                self.last_text = self.add_text("paragraph", para, page_no, bbox, method).text
            para = []

        for _, _, item in items:
            if isinstance(item, RawTable):
                flush()
                item.section = self.path[-1] if self.path else None
                b = self.add("table", "", page_no, item.bbox, method, source="ocr" if item.ocr else "text",
                             status="read_uncertain" if item.ocr else "read")
                if item.uncertain:
                    self.mark_uncertain(b, item.uncertain)
                self.table_raw[b.index] = item
                self.captions[id(item)] = _caption(self.last_text)
                self.raws.append(item)
                notes_of, notes_bottom = (item, item.bbox[3]) if item.bbox else (None, 0.0)
                continue
            if isinstance(item, Region):
                if item.covered or item.duplicate:
                    continue  # the text layer holds it, or another occurrence of the same picture stands for it
                r = item.reading or PictureReading("unread", "none", note=NOTE_NOT_READ)
                flush()
                b = self.add("image", "", page_no, item.bbox, r.method, source=r.method, status=r.status,
                             note=r.note, content_hash=item.content_hash)
                b.picture_text = r.text
                self.pictures[b.index] = (item, r, _caption(self.last_text))
                notes_of, notes_bottom = (item, item.bbox[3]) if r.tables else (None, 0.0)
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
                self.add_text("heading", [ln], page_no, ln.bbox, method, label=label)
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
            elif raw.uncertain and t.block_index is not None:  # a continuation's uncertainty is its table's
                self.mark_uncertain(kept[t.block_index], raw.uncertain)
        for t in tables:
            if t.block_index is not None:
                kept[t.block_index].text = render_table(t)
        for old, (region, reading, caption) in sorted(self.pictures.items()):
            b = kept[renumber[old]]
            texts = [reading.text] if reading.text else []
            extra = self.notes.get(id(region), [])
            for k, pt in enumerate(reading.tables):
                t = TableResult(index=len(tables), headers=pt.headers, units=units_for(pt.headers),
                                rows=[TableRow(page=b.page, cells=cells) for cells in pt.rows], page_start=b.page,
                                page_end=b.page, ocr=True, section=b.section, source=reading.method,
                                caption=caption or None, title=list(pt.title),
                                notes=list(pt.notes) + (extra if k == len(reading.tables) - 1 else []),
                                block_index=b.index)
                tables.append(t)
                if b.table_index is None:
                    b.table_index = t.index
                texts.append("\n".join(x for x in [*t.title, render_table(t)] if x))
            b.text = "\n".join(x for x in texts if x)
        kept_ids = {id(b) for b in kept}
        self.uncertain = [b for b in self.uncertain if id(b) in kept_ids]
        return kept, tables


def _uncertain_report(blocks: list[Block]) -> list[dict]:
    """The text left uncertain by unrepaired font maps, per page and reason: {"page", "blocks", "reason"}."""
    out: dict[tuple, dict] = {}
    for b in blocks:
        entry = out.setdefault((b.page, b.note), {"page": b.page, "blocks": 0, "reason": b.note})
        entry["blocks"] += 1
    return list(out.values())


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


def _layers(outs: list[_PageOut]) -> list[PageLayer]:
    """The pages read from their text layer, with their pictures, for region reading."""
    layers = []
    for k, o in enumerate(outs):
        if o.page.method != "text_layer" or not o.page.ok:
            continue
        for pic in o.pictures:
            pic.page = o.page.page_no
        layers.append(PageLayer(index=k, width=o.width, height=o.height, chars=o.chars,
                                lines=[(ln.bbox, ln.text) for ln in o.lines if ln.bbox], regions=o.pictures))
    return layers


def _region_text(r: PictureReading) -> str:
    rows = [" | ".join(c for c in row if c) for t in r.tables for row in [t.headers, *t.rows]]
    return "\n".join(x for x in [r.text, *rows] if x.strip())


def _merge_region_text(out: _PageOut) -> None:
    """A page whose regions added text: its text holds them at their place and its method is ``mixed``. Page
    furniture (a logo, a watermark) is no content of the page: its one block carries its reading."""
    found = [(pic.bbox[1], _region_text(pic.reading)) for pic in out.pictures
             if not (pic.covered or pic.furniture or pic.duplicate) and pic.reading is not None
             and pic.reading.status in ("read", "read_uncertain")]
    found = [(top, t) for top, t in found if t]
    if not found:
        return
    items = [(ln.top, ln.text) for ln in out.lines] + found
    items.sort(key=lambda x: x[0])
    out.page.text = "\n".join(t for _, t in items)
    out.page.method = "mixed"


def _read_page(doc, plumber, index: int, settings: Settings, fix: FontMapFix | None = None) -> _PageOut:
    warnings: list[str] = []
    out = _process_page(doc, plumber, index, settings, warnings, fix)
    out.warnings = warnings
    pdf_page = doc[index]
    out.width, out.height = pdf_page.get_size()
    pdf_page.close()
    return out


def extract_pdf(data: bytes, deadline: float, settings: Settings, vision: VisionReader | None = None,
                readings: ReadingCache | None = None) -> ExtractionResult:
    """``vision``: the office's vision reader (None: OCR only). ``readings``: the office's earlier readings of
    region content (``image_readings``), consulted and filled by region reading."""
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
                outs.append(_read_page(doc, plumber, index, settings))
            # broken font maps: detected over the whole document, verified against the glyphs; only the pages
            # holding a suspect character are read again, from their corrected text layer
            fix = fontmap.detect_and_repair([w for o in outs for w in o.words], doc,
                                            FontMapConfig.from_settings(settings))
            for index in sorted(fix.pages):
                check_deadline(deadline)
                outs[index] = _read_page(doc, plumber, index, settings, fix)
        finally:
            if plumber is not None:
                plumber.close()
        for o in outs:
            o.words = []
            for w in o.warnings:
                if w not in warnings:
                    warnings.append(w)
        page_count = len(doc)
        _orient_undecided_pages(outs)
        layers = _layers(outs)
        read_regions(doc, data, layers, settings, vision, readings, deadline)
        repeated = mark_repeated(layers)
        for o in outs:
            o.chars = []  # the character boxes served region reading only
        del layers
    finally:
        doc.close()

    walker = _Walker()
    for out in outs:
        for k, raw in enumerate(sorted(out.tables, key=lambda t: t.top)):
            raw.first_on_page = k == 0
        if out.page.ok:
            _merge_region_text(out)
            walker.page(out)
    blocks, tables = walker.finish()
    pages = [o.page for o in outs]
    chunks = chunk_blocks(blocks, tables)
    report = fix.report()
    result = ExtractionResult(page_count=page_count, pages=pages, tables=tables, chunks=chunks, warnings=warnings,
                              blocks=blocks, uncertain=_uncertain_report(walker.uncertain),
                              fontmap=report if any(report.values()) else None, repeated=repeated)
    unread = result.components["unread"]
    if unread:
        warnings.append(WARN_PICTURES.format(n=len(unread)))
    if walker.uncertain:
        warnings.append(WARN_FONTMAP.format(n=len(walker.uncertain)))
    return result
