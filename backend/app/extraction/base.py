"""Extractor interface (KTD11). The worker depends only on these types, so Docling or a cloud
OCR adapter can replace the default extractor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Protocol

MSG_DEADLINE = "חריגה מזמן העיבוד המותר למסמך"
COVERAGE_REGIONS_MAX = 40  # regions listed per page in the reading report (the rest are counted)


class ExtractionError(Exception):
    """Processing failed. ``reason`` is a Hebrew message shown to users. Permanent errors
    (encrypted, corrupt, over limits) are not retried."""

    def __init__(self, reason: str, permanent: bool = True):
        super().__init__(reason)
        self.reason = reason
        self.permanent = permanent


def check_deadline(deadline: float) -> None:
    """Fail the job permanently once its wall-clock budget is spent."""
    if time.monotonic() > deadline:
        raise ExtractionError(MSG_DEADLINE, True)


@dataclass
class PageGeometry:
    """Where a PDF page's positions live (KTD2): the raw ``/MediaBox`` and ``/CropBox`` (``[x0, y0, x1, y1]`` in PDF
    user space, corners sorted), ``/Rotate`` in degrees, and the size of the rendered page in points (the CropBox
    within the MediaBox, turned by the rotation). ``issue`` says why positions on the page cannot be converted into
    the frame of the rendered page (``app.extraction.geometry``); such a page keeps block boxes only."""

    mediabox: list[float] | None
    cropbox: list[float] | None
    rotation: int | None
    width: float | None
    height: float | None
    issue: str | None = None


@dataclass
class PageResult:
    page_no: int  # physical page in the file, 1-based
    text: str  # logical-order text
    method: Literal["text_layer", "ocr", "mixed", "docx", "failed"]
    quality: float
    ok: bool
    geometry: PageGeometry | None = None  # PDF pages only
    printed_label: str | None = None  # the page number printed on the page (``/PageLabels`` or its page-number line)


@dataclass
class TableRow:
    page: int | None  # physical page of this row (None for DOCX)
    cells: list[str]
    # each cell's box on the rendered page ([x0, top, x1, bottom], display frame), None for a merged cell; None for
    # the whole row when the table was not read from a PDF text layer or its page cannot be converted (KTD2)
    cell_boxes: list[list[float] | None] | None = None


@dataclass
class TableResult:
    index: int
    headers: list[str]
    units: list[str | None]
    rows: list[TableRow]
    page_start: int | None
    page_end: int | None
    ocr: bool = False
    section: str | None = None
    # Where the table came from: a Word table, a picture's text records (``emf``), OCR or a model's reading of a
    # picture (``vision``). Pictures keep their media name; every table keeps the text that introduces it
    # (``caption``: "להלן נתוני היצע..."), its own title lines and its notes ("(*) בהתאם ל...").
    source: str = "text"
    media: str | None = None
    caption: str | None = None
    title: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    block_index: int | None = None
    header_boxes: list[list[float] | None] | None = None  # the header cells' boxes, like ``TableRow.cell_boxes``


@dataclass
class Block:
    """One unit of a document in reading order: a heading, a paragraph, a text box, a table, or a picture.

    DOCX has no pages, so a block is located by its section (the heading path), its numbering label ("6.2")
    and its running paragraph number; a picture also by its media name. A PDF block also has its page and its
    region on the page (``bbox``: x0, top, x1, bottom in points from the top-left corner). ``status`` says whether
    its content was read: ``read``, ``read_uncertain`` (OCR, or a model reading that OCR could not confirm),
    ``no_text`` (a photo or drawing without legible text), ``decorative`` (too small to carry content) or
    ``unread``.

    Provenance (PDF): ``method`` is how the text was obtained (``text_layer``, ``ocr``, ``vision``, ``none`` for a
    picture not read yet), ``reader_version`` the reader that produced the block, ``content_hash`` a picture's
    hash over its raw image bytes (the key a reading is reused by), and ``original_text`` the text as extracted
    when ``text`` is a corrected form of it. DOCX blocks leave them empty.

    ``spans`` (PDF text-layer headings and paragraphs): each word's character range in ``text`` and its box on the
    rendered page, ``[line, word, start, end, x0, top, x1, bottom]`` (``app.extraction.geometry``); None for OCR,
    pictures, tables, DOCX and pages whose positions cannot be converted."""

    index: int
    kind: Literal["heading", "paragraph", "textbox", "table", "image"]
    text: str
    section: str | None = None
    section_path: list[str] = field(default_factory=list)
    label: str | None = None
    paragraph_no: int | None = None
    page: int | None = None
    media: str | None = None
    source: str = "text"  # text | word_table | emf | ocr | vision
    status: str = "read"
    note: str | None = None  # why a picture is unread or uncertain; what a non-text picture shows
    table_index: int | None = None
    picture_text: str = ""  # a picture's text outside its tables (labels, a screenshot's lines)
    bbox: list[float] | None = None
    method: str | None = None
    reader_version: str | None = None
    content_hash: str | None = None
    original_text: str | None = None
    spans: list[list] | None = None


@dataclass
class ChunkResult:
    index: int
    kind: Literal["text", "table_row", "table", "image"]
    page_list: list[int] | None
    section: str | None
    text: str
    table_index: int | None = None  # table_row chunks: the source table and row (KTD10)
    row_index: int | None = None
    block_start: int | None = None  # the blocks this chunk covers (inclusive)
    block_end: int | None = None


@dataclass
class ExtractionResult:
    page_count: int | None
    pages: list[PageResult]
    tables: list[TableResult]
    chunks: list[ChunkResult]
    is_docx: bool = False
    warnings: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    # PDF font maps (``fontmap.FontMapFix.report``): suspect fonts, accepted corrections, unresolved pairs; None
    # when no font was suspect. ``uncertain``: text kept with a corruption no verified repair fixed, per page and
    # reason ({"page", "blocks", "reason"}); the document is then only partly read.
    fontmap: dict | None = None
    uncertain: list[dict] = field(default_factory=list)
    # PDF page furniture: each picture repeated through the document and shown as one block
    # ({"hash", "occurrences", "pages", "first_page", "status"}; ``regions.mark_repeated``)
    repeated: list[dict] = field(default_factory=list)

    @property
    def components(self) -> dict:
        """What the document holds and what was read: counts per block kind, pictures per status, the pictures
        that were not read and the text whose reading stays uncertain (the document is then only partly read),
        the font-map record when a font was suspect, the pictures repeated through the document and read
        once (``repeated_images``), what each page left unread, uncertain or corrected (``coverage``), and the
        number of blocks whose text is a verified correction of the extracted text (``corrected_blocks``)."""
        kinds: dict[str, int] = {}
        images: dict[str, int] = {}
        methods: dict[str, int] = {}
        unread = []
        for b in self.blocks:
            kinds[b.kind] = kinds.get(b.kind, 0) + 1
            if b.kind == "image":
                images[b.status] = images.get(b.status, 0) + 1
                methods[b.source] = methods.get(b.source, 0) + 1
                if b.status == "unread":
                    entry = {"media": b.media, "section": b.section, "reason": b.note}
                    unread.append(entry if b.page is None else entry | {"page": b.page})
        out = {"blocks": kinds, "images": images, "image_methods": methods, "unread": unread,
               "tables": len(self.tables),
               "partial": bool(unread) or bool(self.uncertain) or self.pages_incomplete > 0}
        if self.uncertain:
            out["uncertain"] = self.uncertain
        if self.fontmap:
            out["fontmap"] = self.fontmap
        if self.repeated:
            out["repeated_images"] = self.repeated
        coverage = self.coverage
        if coverage:
            out["coverage"] = coverage
        corrected = sum(1 for b in self.blocks if b.original_text is not None)
        if corrected:
            out["corrected_blocks"] = corrected
        return out

    @property
    def coverage(self) -> list[dict]:
        """Per page, what was not fully read or was corrected: ``{"page", "ok", "method", "corrected", "regions"}``,
        only for pages with something to report (in page order; DOCX blocks, which have no page, under ``page``
        None). A region is a block left ``unread``, or ``read_uncertain`` with a reason, located by its block index
        and box: ``{"block", "kind", "status", "reason", "bbox", "section", "media"}``. A page holds at most
        ``COVERAGE_REGIONS_MAX`` regions; ``more`` counts the rest. A picture repeated through the document is one
        block, so it is reported once (``repeated_images``), not on every page."""
        pages: dict[int | None, dict] = {}

        def entry(page: int | None) -> dict:
            if page not in pages:
                p = next((p for p in self.pages if p.page_no == page), None)
                pages[page] = {"page": page, "ok": p.ok if p else True, "method": p.method if p else None,
                               "corrected": 0, "regions": []}
            return pages[page]

        for p in self.pages:
            if not p.ok:
                entry(p.page_no)
        for b in self.blocks:
            if b.original_text is not None:
                entry(b.page)["corrected"] += 1
            if b.status == "unread" or (b.status == "read_uncertain" and b.note):
                e = entry(b.page)
                if len(e["regions"]) >= COVERAGE_REGIONS_MAX:
                    e["more"] = e.get("more", 0) + 1
                    continue
                e["regions"].append({"block": b.index, "kind": b.kind, "status": b.status, "reason": b.note,
                                     "bbox": b.bbox, "section": b.section, "media": b.media})
        return sorted(pages.values(), key=lambda e: (e["page"] is not None, e["page"] or 0))

    @property
    def pages_incomplete(self) -> int:
        return sum(1 for p in self.pages if not p.ok)


class Extractor(Protocol):
    def extract(self, data: bytes, mime_type: str, deadline: float, vision=None) -> ExtractionResult:
        """``vision``: an optional ``app.extraction.images.VisionReader`` for pictures with text."""
