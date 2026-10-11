"""Where a citation points: anchors born in the tools and snapshotted into the stored answer (KTD1, KTD4, R3, R6,
R8, R10, R11).

A tool that returns a passage (``S#``), takes a value (``V#``) or lists a stored measurement (``M#``) records a
*stub* in ``Workspace.anchors``: the document, version and reading it read, the block range, and — when known — the
word span inside a block (``segments``: ``[block, start, end]`` character ranges of the block's text) or the table
cell (``table_index``, ``row``, ``column``, 0-based into ``extracted_tables.structure.rows``). A cell of a table read
by ``inspect`` has no ``extracted_tables`` row: its stub carries the inspected region instead (``vision``: the region
key, table, row and column of the stored reading, the table's context as transcribed, and ``cell_box``, the box of
the OCR word that confirmed the number, mapped into the rendered page by ``page_box``; None when OCR did not confirm
it in its place, KTD6). A stub holds only what
the server read from stored data: never a position the model supplied, and never a place found by searching the
document for the first occurrence of a number (a quote is located inside the cited block range only, ``locate_quote``).

When the answer is stored, ``attach`` resolves every cited stub against the turn's pinned reading (``load`` reads the
stored pages, blocks and tables in one transaction) and stores a self-contained *snapshot* inside the answer payload
(``snapshot``). A snapshot keeps what a viewer needs without the reading it came from, so an old conversation keeps its
original place after a reprocess replaced its blocks (R11):

    {"v": 1,
     "precision": "span" | "cell" | "block" | "region" | "page" | "structured",
     "precision_label": Hebrew text for the viewer's header,
     "region": "table" | "row" | None,           # what a "region" covers
     "degraded": None | "reading_changed" | "no_geometry" | "no_cell_box" | "not_located" | "unavailable"
                 | "anchor_lost",                # why the precision is lower than the stub asked for
     "document_id", "version_id", "reading_id",  # reading_id: the turn's pinned reading
     "block_start", "block_end",
     "pages": [{"page": file page (1-based), "printed_label": str | None, "width", "height" (points, rendered frame),
                "rects": [[x0, y0, x1, y1], ...],  # fractions of width/height, origin top-left
                "focus": [[...]]}],                 # the cited number inside the rects (span precision)
     "location": {"title": readable title | None, "label": readable location (never ids or file names),
                  "page", "page_end", "printed_page", "section"},
     "table": None | {"table_index", "title", "row_label", "row_number", "column_header", "column_number",
                      "unit_note", "source", "header": None | {"page", "rects"}, "notes": [{"text", "page"?}]},
     "structured": None | {"section_path", "paragraph_no", "paragraph_end", "label", "text",
                           "highlight": [start, end] | None,  # in "text"
                           "cell": None | {"table_index", "row_number", "column_number", "row_label",
                                           "column_header", "text"}},
     "truncated": bool}

Precision rules: a cell is highlighted only with its stored cell box; otherwise the table (or picture) region is
highlighted and labelled as table level (R6). A vision cell is highlighted at cell precision only with its
``cell_box``; otherwise its inspected region, labelled table level, with the table's context (R19). A page whose positions cannot be converted into the rendered frame
(``pages.geometry_issue``) or that stores none gives page precision. DOCX has no pages: structured precision with the
section path, paragraph number or normalized cell, and the cited text (R9). A stub whose reading is not the turn's
pinned one, or a version read again before the answer was stored, gives page precision with the pinned reading id and
none of the new reading's boxes (structured with the cited text for DOCX). A computed result (``C#``) anchors to its
inputs (``computed_anchor``), never to a place in a document. Every snapshot is bounded (``bound``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.verify import _NUMBER
from app.chat import reader
from app.chat.meaning import _norm, units_attested
from app.extraction.base import PageGeometry
from app.extraction.geometry import Span, geometry_box

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
COMPUTED = "computed"

READING_CHANGED = "reading_changed"
NO_GEOMETRY = "no_geometry"
NO_CELL_BOX = "no_cell_box"
NOT_LOCATED = "not_located"
UNAVAILABLE = "unavailable"
ANCHOR_LOST = "anchor_lost"

PRECISION_LABELS = {
    "span": "המקום המצוטט מסומן בעמוד",
    "cell": "התא מסומן בטבלה",
    "block": "הקטע מסומן בעמוד",
    "region_table": "הסימון ברמת הטבלה: לא נשמר מיקום לתא עצמו",
    "region_row": "השורה מסומנת בטבלה",
    "page": "הסימון ברמת העמוד: לא נשמר מיקום מדויק יותר",
    "structured": "מבנה המסמך (ללא עמודים)",
    COMPUTED: "תוצאת חישוב: מקורותיה הם הקלטים שלה",
}
LOCATION_DOCUMENT = "המסמך"

MAX_BLOCKS = 80  # blocks of one anchor read and drawn
MAX_PAGES = 6  # pages kept in one snapshot
MAX_RECTS = 24  # rectangles on one page (more are drawn as their union)
MAX_TEXT = 600  # characters of cited text in a structured anchor
MAX_NOTES = 3
MAX_CHARS = 6000  # a snapshot's JSON size

_EXT = re.compile(r"\.(pdf|docx?|rtf|txt|odt)$", re.I)
_MOJIBAKE = re.compile(r"[ÃÂ×Ø][\u0080-ÿ]|â€")
_HEXID = re.compile(r"[0-9a-fA-F-]{12,}")


# --- stubs ------------------------------------------------------------------------------------------------------

def source_stub(s) -> dict | None:
    """A source's stub (``tools.Source``): its version, reading, block range, pages, and the table (and row, for a
    table-row search hit) it is of. None for a listing: it names documents, it is no place in one."""
    if getattr(s, "is_listing", False) or s.version_id is None or s.document_id is None:
        return None
    out = {"kind": "source", "document_id": str(s.document_id), "version_id": str(s.version_id),
           "reading_id": s.reading_id, "block_start": s.block_start, "block_end": s.block_end,
           "pages": list(s.page_list or []), "section": s.section}
    if s.table_index is not None:
        out["table_index"] = s.table_index
        if getattr(s, "row_index", None) is not None:
            out["row"] = s.row_index
    return out


def measurement_stub(m) -> dict:
    """A stored measurement's stub (``tools.Measurement``): its block, table row and quote as stored. Its word span
    or cell is found at snapshot time inside that block or row only."""
    r = m.row
    block = getattr(r, "block_index", None)
    value = getattr(r, "value", None)
    return {"kind": "measurement", "document_id": str(m.document_id), "version_id": str(m.version_id),
            "reading_id": getattr(r, "reading_id", None), "block_start": block, "block_end": block,
            "table_index": getattr(r, "table_index", None), "row": getattr(r, "row_index", None),
            "quote": getattr(r, "quote", None) or "", "value": str(value) if value is not None else None,
            "anchor_lost": getattr(r, "anchor_lost", None) is not None, "pages": [],
            "section": getattr(r, "section", None)}


# --- KTD4: a quote inside the cited range ------------------------------------------------------------------------

class AmbiguousQuote(Exception):
    """The quote occurs more than once in the cited range, and the occurrences write different numbers."""


@dataclass
class Located:
    segments: list[list[int]]  # [block, start, end] per block the quote covers; empty: block precision
    number: list[int] | None  # [block, start, end] of the cited number
    blocks: list[int]  # the blocks holding the quote


def _number_value(written: str) -> Decimal | None:
    try:
        return Decimal(written.rstrip(".,").replace(",", ""))
    except InvalidOperation:
        return None


def _number_at(s: str, pos: int) -> Decimal | None:
    for m in _NUMBER.finditer(s):
        if m.start() <= pos < m.end():
            return _number_value(m.group(0))
    return None


def _values_in(s: str) -> list[Decimal]:
    return [v for m in _NUMBER.finditer(_norm(s or "")) if (v := _number_value(m.group(0))) is not None]


def locate_quote(blocks, quote: str, value: Decimal) -> Located | None:
    """Where a quote of a value is in the cited blocks (``(block_index, text, ...)`` each, in reading order), compared
    with one spelling of the abbreviation marks and one space, as ``take_value`` compares it. None when it is not in
    them. One occurrence: its words in each block and the cited number. Several: block precision over the blocks
    holding them when every occurrence writes the same number at the cited place; ``AmbiguousQuote`` when they write
    different numbers (a quote cut inside a number) — never the first occurrence."""
    tokens: list[tuple[int, int, int, int]] = []  # (block, start, end, flat start)
    texts: dict[int, str] = {}
    flat_parts: list[str] = []
    at = 0
    for b in blocks:
        index, raw = b[0], _norm(b[1] or "")
        texts[index] = raw
        for m in re.finditer(r"\S+", raw):
            if flat_parts:
                at += 1
            tokens.append((index, m.start(), m.end(), at))
            flat_parts.append(m.group(0))
            at += len(m.group(0))
    flat = " ".join(flat_parts)
    q = " ".join(_norm(quote or "").split())
    if not q or not tokens:
        return None
    hits: list[int] = []
    i = flat.find(q)
    while i >= 0:
        hits.append(i)
        i = flat.find(q, i + 1)
    if not hits:
        return None
    in_quote = next((m for m in _NUMBER.finditer(q) if _number_value(m.group(0)) == abs(value)), None)

    def raw_at(f: int) -> tuple[int, int]:
        tok = max((t for t in tokens if t[3] <= f), key=lambda t: t[3])
        return tok[0], tok[1] + (f - tok[3])

    def segments_of(start: int) -> list[list[int]]:
        out: list[list[int]] = []
        first, last = raw_at(start), raw_at(start + len(q) - 1)
        for t in tokens:
            if t[3] + (t[2] - t[1]) <= start or t[3] >= start + len(q):
                continue
            if out and out[-1][0] == t[0]:
                out[-1][2] = t[2]
            else:
                out.append([t[0], t[1], t[2]])
        out[0][1] = first[1]
        out[-1][2] = last[1] + 1
        return out

    numbers = []
    for h in hits:
        if in_quote is None:
            numbers.append(None)
            continue
        block, pos = raw_at(h + in_quote.start())
        numbers.append(_number_at(texts[block], pos))
    if len(hits) > 1:
        if len(set(numbers)) > 1:
            raise AmbiguousQuote
        held = sorted({s[0] for h in hits for s in segments_of(h)})
        return Located([], None, held)
    segments = segments_of(hits[0])
    number = None
    if in_quote is not None:
        block, pos = raw_at(hits[0] + in_quote.start())
        number = [block, pos, pos + len(in_quote.group(0))]
    return Located(segments, number, [s[0] for s in segments])


# --- the stored reading an answer's anchors are resolved against -------------------------------------------------

@dataclass
class Page:
    page_no: int
    width: float | None = None
    height: float | None = None
    mediabox: list | None = None
    cropbox: list | None = None
    rotation: int | None = None
    issue: str | None = None
    printed_label: str | None = None

    @property
    def usable(self) -> bool:
        """Positions in the rendered frame can be drawn on it (stored spans and cell boxes)."""
        return self.issue is None and bool(self.width) and bool(self.height)

    def geometry(self) -> PageGeometry:
        return PageGeometry(self.mediabox, self.cropbox, self.rotation, self.width, self.height, self.issue)


@dataclass
class Block:
    index: int
    kind: str
    page: int | None
    text: str = ""
    bbox: list | None = None  # pdfplumber frame, as stored
    spans: list | None = None  # rendered frame, as stored
    section_path: tuple = ()
    paragraph_no: int | None = None
    label: str | None = None
    table_index: int | None = None
    media: str | None = None


@dataclass
class Reading:
    """What one version's stored reading holds for an answer's anchors, read in one transaction."""

    document_id: str
    version_id: str
    title: str
    filename: str | None
    is_pdf: bool
    reading_id: str | None  # the version's reading as stored now
    pages: dict[int, Page] = field(default_factory=dict)
    blocks: dict[int, Block] = field(default_factory=dict)
    tables: dict[int, dict] = field(default_factory=dict)  # table index -> extracted_tables.structure
    table_blocks: dict[int, int] = field(default_factory=dict)  # table index -> the block it is read at


def _stub_blocks(s: dict) -> set[int]:
    out: set[int] = set()
    a, b = s.get("block_start"), s.get("block_end")
    if a is not None:
        b = a if b is None else b
        out |= set(range(a, min(b, a + MAX_BLOCKS - 1) + 1))
    out |= {seg[0] for seg in s.get("segments") or []}
    out |= set(s.get("blocks") or [])
    return out


def load(conn: Connection, stubs: list[dict]) -> dict[str, Reading]:
    """The stored reading of every version the stubs name and the user still sees (RLS, deleted documents
    excluded), with the blocks, tables and pages they point at."""
    by_version: dict[str, list[dict]] = {}
    for s in stubs:
        by_version.setdefault(s["version_id"], []).append(s)
    out: dict[str, Reading] = {}
    for vid, ss in by_version.items():
        try:
            v = reader.version(conn, UUID(vid))
        except ValueError:
            continue
        if v is None:
            continue
        indexes = set().union(*(_stub_blocks(s) for s in ss))
        tables = {s["table_index"] for s in ss if s.get("table_index") is not None}
        pages = {p for s in ss for p in s.get("pages") or []}
        structures = reader.tables_of(conn, v.version_id, tables) if tables else {}
        table_blocks = {r.table_index: r.block_index for r in conn.execute(text(
            "SELECT DISTINCT ON (table_index) table_index, block_index FROM document_blocks WHERE version_id = :v"
            " AND table_index = ANY(:t) ORDER BY table_index, block_index"),
            {"v": v.version_id, "t": list(tables)})} if tables else {}
        for t, st in structures.items():
            if t not in table_blocks and st.get("block_index") is not None:
                table_blocks[t] = st["block_index"]
            pages |= {r.get("page") for r in st.get("rows") or [] if r.get("page")}
        indexes |= set(table_blocks.values())
        blocks = {}
        if indexes:
            for r in conn.execute(text(
                    "SELECT block_index, kind, page, bbox, spans, text, section_path, paragraph_no, label, table_index,"
                    " media FROM document_blocks WHERE version_id = :v AND block_index = ANY(:i)"),
                    {"v": v.version_id, "i": sorted(indexes)}):
                blocks[r.block_index] = Block(r.block_index, r.kind, r.page, r.text or "", r.bbox, r.spans,
                                              tuple(r.section_path or ()), r.paragraph_no, r.label, r.table_index,
                                              r.media)
        pages |= {b.page for b in blocks.values() if b.page}
        page_rows = {}
        if pages and v.is_pdf:
            for r in conn.execute(text(
                    "SELECT page_no, display_width, display_height, mediabox, cropbox, rotation, geometry_issue,"
                    " printed_label FROM pages WHERE version_id = :v AND page_no = ANY(:p)"),
                    {"v": v.version_id, "p": sorted(pages)}):
                page_rows[r.page_no] = Page(r.page_no, r.display_width, r.display_height, r.mediabox, r.cropbox,
                                            r.rotation, r.geometry_issue, r.printed_label)
        out[vid] = Reading(str(v.document_id), vid, v.title, v.filename, v.is_pdf, v.reading_id, page_rows, blocks,
                           structures, table_blocks)
    return out


# --- readable location -------------------------------------------------------------------------------------------

def _garbled(t: str) -> bool:
    if "�" in t or _MOJIBAKE.search(t):
        return True
    compact = t.replace(" ", "")
    if _HEXID.fullmatch(compact) and any(c.isdigit() for c in compact):
        return True
    letters = sum(c.isalpha() for c in compact)
    return letters < 2 or letters < 0.3 * len(compact)


def readable_title(title: str | None, filename: str | None = None) -> str | None:
    """The document's name as a person reads it: the title (else the file name) without its extension and
    underscores; None when both are garbled (wrongly decoded) or technical ids, so the location leads instead."""
    for cand in (title, filename):
        t = " ".join(_EXT.sub("", (cand or "").strip()).replace("_", " ").split())
        if t and not _garbled(t):
            return t
    return None


def _pages_text(pages: list[dict]) -> str:
    if not pages:
        return ""
    first, last = pages[0], pages[-1]
    if first["page"] == last["page"]:
        printed = first.get("printed_label")
        shown = f" (בדפוס {printed})" if printed and printed != str(first["page"]) else ""
        return f"עמוד {first['page']}{shown}"
    return f"עמודים {first['page']}–{last['page']}"


def _location(out: dict, reading: Reading | None, section: str | None) -> dict:
    pages = out["pages"]
    t = out.get("table") or {}
    s = out.get("structured") or {}
    parts = [_pages_text(pages)]
    if section:
        parts.append(f"סעיף «{section}»")
    if t:
        parts.append(f"טבלה «{t['title']}»" if t.get("title") else "טבלה")
        if t.get("row_label"):
            parts.append(f"שורה «{t['row_label']}»")
        if t.get("column_header"):
            parts.append(f"עמודה «{t['column_header']}»")
    elif s.get("paragraph_no"):
        a, b = s["paragraph_no"], s.get("paragraph_end") or s["paragraph_no"]
        parts.append(f"פסקה {a}" if a == b else f"פסקאות {a}–{b}")
    printed = pages[0].get("printed_label") if pages else None
    return {"title": readable_title(reading.title, reading.filename) if reading is not None else None,
            "label": ", ".join(p for p in parts if p) or LOCATION_DOCUMENT,
            "page": pages[0]["page"] if pages else None,
            "page_end": pages[-1]["page"] if len(pages) > 1 else None,
            "printed_page": printed if printed and printed != str(pages[0]["page"]) else None,
            "section": section}


# --- snapshot ----------------------------------------------------------------------------------------------------

def _frac(box, page: Page) -> list[float] | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    w, h = float(page.width), float(page.height)

    def clip(v: float) -> float:
        return round(min(1.0, max(0.0, v)), 4)

    return [clip(x0 / w), clip(y0 / h), clip(x1 / w), clip(y1 / h)]


def _union(rects: list[list[float]]) -> list[float]:
    return [min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)]


def _block_rect(block: Block | None, reading: Reading) -> tuple[int, list[float]] | None:
    """A stored block box (pdfplumber frame) in the rendered frame, as fractions of its page."""
    if block is None or block.page is None or not block.bbox:
        return None
    page = reading.pages.get(block.page)
    if page is None or not page.usable:
        return None
    box = geometry_box(block.bbox, page.geometry())
    rect = _frac(box, page) if box is not None else None
    return (block.page, rect) if rect is not None else None


def _page_entry(reading: Reading | None, n: int, rects=(), focus=()) -> dict:
    page = reading.pages.get(n) if reading is not None else None
    return {"page": n, "printed_label": page.printed_label if page else None,
            "width": page.width if page else None, "height": page.height if page else None,
            "rects": list(rects), "focus": list(focus)}


def _base(stub: dict, pinned: str | None) -> dict:
    return {"v": SNAPSHOT_VERSION, "precision": None, "precision_label": None, "region": None, "degraded": None,
            "document_id": stub["document_id"], "version_id": stub["version_id"],
            "reading_id": pinned if pinned is not None else stub.get("reading_id"),
            "block_start": stub.get("block_start"), "block_end": stub.get("block_end"), "pages": [],
            "location": None, "table": None, "structured": None, "truncated": False}


def _finish(out: dict, reading: Reading | None, precision: str, pages: dict[int, dict] | None = None, *,
            region: str | None = None, degraded: str | None = None, table: dict | None = None,
            section: str | None = None) -> dict:
    out["precision"] = precision
    out["region"] = region
    out["degraded"] = degraded
    out["precision_label"] = PRECISION_LABELS[f"region_{region}" if precision == "region" else precision]
    out["pages"] = [_page_entry(reading, n, p.get("rects", ()), p.get("focus", ())) for n, p in
                    sorted((pages or {}).items())]
    out["table"] = table
    out["location"] = _location(out, reading, section)
    return out


def _page_only(out: dict, stub: dict, reading: Reading | None, reason: str, pages=None, *,
               table: dict | None = None, section: str | None = None) -> dict:
    numbers = sorted({p for p in (pages if pages is not None else stub.get("pages") or []) if p})
    if reading is not None and not reading.is_pdf:
        numbers = []
    return _finish(out, reading, "page", {n: {} for n in numbers}, degraded=reason, table=table,
                   section=section if section is not None else stub.get("section"))


def _table_page(reading: Reading, ti: int) -> int | None:
    block = reading.blocks.get(reading.table_blocks.get(ti, -1))
    if block is not None and block.page:
        return block.page
    rows = (reading.tables.get(ti) or {}).get("rows") or []
    return rows[0].get("page") if rows else None


def _table_ctx(reading: Reading, ti: int, row: int | None, column: int | None) -> dict | None:
    """The table context a cell or row is read with (R6, R10): title, row label, column header, the unit, notes, and
    the header cell (or row) anchored where a box is stored."""
    st = reading.tables.get(ti)
    if st is None:
        return None
    rows = st.get("rows") or []
    headers = st.get("headers") or []
    cells = (rows[row].get("cells") or []) if row is not None and 0 <= row < len(rows) else []
    col = column if column is not None and (0 <= column < max(len(headers), len(cells))) else None
    units = st.get("units") or []
    notes = [n for n in st.get("notes") or [] if n]
    unit = units[col] if col is not None and col < len(units) and units[col] else None
    if unit is None:
        unit = next((n for n in notes if units_attested(n)), None)
    header = None
    page_no = _table_page(reading, ti)
    page = reading.pages.get(page_no) if page_no is not None else None
    boxes = st.get("header_boxes") or []
    if reading.is_pdf and page is not None and page.usable and boxes:
        chosen = [boxes[col]] if col is not None and col < len(boxes) else [] if col is not None else boxes
        rects = [r for r in (_frac(b, page) for b in chosen if b) if r]
        if rects:
            header = {"page": page_no, "rects": [_union(rects)]}
    title = st.get("caption") or next((t for t in st.get("title") or [] if t), None)
    return {"table_index": ti, "title": title, "row_label": (cells[0] or None) if cells else None,
            "row_number": row + 1 if cells else None,
            "column_header": (headers[col] or None) if col is not None and col < len(headers) else None,
            "column_number": col + 1 if col is not None else None, "unit_note": unit, "source": st.get("source"),
            "header": header,
            "notes": [{"text": n} | ({"page": page_no} if reading.is_pdf and page_no else {})
                      for n in notes[:MAX_NOTES]]}


def _section_of(blocks: list[Block], stub: dict, reading: Reading) -> str | None:
    for b in blocks:
        if b.section_path:
            return b.section_path[-1]
    ti = stub.get("table_index")
    if ti is not None and reading.tables.get(ti, {}).get("section"):
        return reading.tables[ti]["section"]
    return stub.get("section")


def _found(reading: Reading, indexes) -> list[Block]:
    return [reading.blocks[i] for i in indexes if i in reading.blocks]


def _range(stub: dict) -> tuple[list[int], bool]:
    a, b = stub.get("block_start"), stub.get("block_end")
    if a is None:
        return [], False
    b = a if b is None else b
    return list(range(a, b + 1))[:MAX_BLOCKS], b - a + 1 > MAX_BLOCKS


def _blocks(out: dict, stub: dict, reading: Reading, indexes: list[int], table: dict | None) -> dict:
    found = _found(reading, indexes)
    section = _section_of(found, stub, reading)
    if not found:
        return _page_only(out, stub, reading, NOT_LOCATED, table=table, section=section)
    pages: dict[int, dict] = {}
    drawn = False
    for b in found:
        if not b.page:
            continue
        entry = pages.setdefault(b.page, {"rects": []})
        rect = _block_rect(b, reading)
        if rect is not None:
            entry["rects"].append(rect[1])
            drawn = True
    if not drawn:
        return _finish(out, reading, "page", pages, degraded=NO_GEOMETRY, table=table, section=section)
    return _finish(out, reading, "block", pages, table=table, section=section)


def _spans(block: Block, start: int, end: int, page: Page) -> list[list[float]]:
    rects = []
    for raw in block.spans or []:
        try:
            s = Span.from_json(raw)
        except (TypeError, ValueError, IndexError):
            continue
        if s.start < end and s.end > start:
            r = _frac(s.box, page)
            if r is not None:
                rects.append(r)
    return rects


def _span(out: dict, stub: dict, reading: Reading, table: dict | None) -> dict | None:
    pages: dict[int, dict] = {}
    found = []
    for b, start, end in stub["segments"]:
        block = reading.blocks.get(b)
        page = reading.pages.get(block.page) if block is not None and block.page else None
        if page is None or not page.usable:
            return None
        rects = _spans(block, start, end, page)
        if not rects:
            return None
        found.append(block)
        pages.setdefault(block.page, {"rects": [], "focus": []})["rects"] += rects
    number = stub.get("number")
    if number:
        block = reading.blocks.get(number[0])
        page = reading.pages.get(block.page) if block is not None and block.page else None
        if page is not None and block.page in pages:
            pages[block.page]["focus"] = _spans(block, number[1], number[2], page)
    return _finish(out, reading, "span", pages, table=table, section=_section_of(found, stub, reading))


def _table_region(out: dict, stub: dict, reading: Reading, ti: int, page_no: int | None, table: dict | None,
                  reason: str | None) -> dict:
    """The table (or picture) region a cell or row is in, labelled table level; page precision when the region has
    no position on the cited row's page."""
    block = reading.blocks.get(reading.table_blocks.get(ti, -1))
    rect = _block_rect(block, reading)
    section = _section_of([block] if block else [], stub, reading)
    if rect is not None and (page_no is None or rect[0] == page_no):
        return _finish(out, reading, "region", {rect[0]: {"rects": [rect[1]]}}, region="table", degraded=reason,
                       table=table, section=section)
    pages = [page_no] if page_no else [block.page] if block is not None and block.page else stub.get("pages") or []
    return _page_only(out, stub, reading, NO_GEOMETRY if reason is None else reason, pages, table=table,
                      section=section)


def _cell(out: dict, stub: dict, reading: Reading) -> dict:
    ti, row, col = stub["table_index"], stub["row"], stub.get("column")
    st = reading.tables.get(ti)
    rows = (st or {}).get("rows") or []
    if st is None or not 0 <= row < len(rows):
        return _blocks(out, stub, reading, _range(stub)[0], None)
    table = _table_ctx(reading, ti, row, col)
    r = rows[row]
    page_no = r.get("page") or _table_page(reading, ti)
    page = reading.pages.get(page_no) if page_no else None
    boxes = r.get("cell_boxes") or []
    if col is not None:
        box = boxes[col] if col < len(boxes) else None
        rect = _frac(box, page) if box and page is not None and page.usable else None
        if rect is not None:
            return _finish(out, reading, "cell", {page_no: {"rects": [rect]}}, table=table,
                           section=_section_of([], stub, reading))
    else:
        rects = [x for x in (_frac(b, page) for b in boxes if b) if x] if page is not None and page.usable else []
        if rects:
            return _finish(out, reading, "region", {page_no: {"rects": [_union(rects)]}}, region="row", table=table,
                           section=_section_of([], stub, reading))
    reason = NO_CELL_BOX if page is not None and page.usable else NO_GEOMETRY
    return _table_region(out, stub, reading, ti, page_no, table, reason)


def page_box(box, frame: dict) -> list[float] | None:
    """An OCR word box of an inspected crop (pixels of the picture OCR read) in the rendered page's frame, in points:
    divided by the OCR upscale and the render scale actually applied to the crop, then offset by the crop's origin
    in the rendered frame (``display_box_of`` gave the crop, so the box is already in that frame and is never
    converted again). None without a box (KTD6)."""
    if not box:
        return None
    try:
        per_point = float(frame["scale"]) * float(frame.get("upscale") or 1.0)
        ox, oy = (float(v) for v in frame["origin"])
        x0, y0, x1, y1 = (float(v) for v in box)
    except (KeyError, TypeError, ValueError):
        return None
    if per_point <= 0:
        return None
    return [round(ox + x0 / per_point, 2), round(oy + y0 / per_point, 2), round(ox + x1 / per_point, 2),
            round(oy + y1 / per_point, 2)]


def _vision_cell(out: dict, stub: dict, reading: Reading) -> dict:
    """A cell of a table read by ``inspect`` (no ``extracted_tables`` row): cell precision with the box its confirming
    OCR word gave (``vision.cell_box``, rendered-frame points); otherwise the inspected region, labelled table level,
    or its page; always with the table's context as the transcription gave it (R17, R19)."""
    v = stub["vision"]
    page_no = v.get("page") or next(iter(stub.get("pages") or []), None)
    notes = [n for n in v.get("notes") or [] if n]
    table = {"table_index": None, "title": v.get("title"), "row_label": v.get("row_label"),
             "row_number": v.get("row_number"), "column_header": v.get("column_header"),
             "column_number": v.get("column_number"), "unit_note": v.get("unit_note"), "source": "vision",
             "header": None,
             "notes": [{"text": n} | ({"page": page_no} if page_no else {}) for n in notes[:MAX_NOTES]]}
    block = reading.blocks.get(stub.get("block_start")) if stub.get("block_start") is not None else None
    section = _section_of([block] if block else [], stub, reading)
    page = reading.pages.get(page_no) if page_no else None
    usable = page is not None and page.usable
    rect = _frac(v["cell_box"], page) if v.get("cell_box") and usable else None
    if rect is not None:
        return _finish(out, reading, "cell", {page_no: {"rects": [rect]}}, table=table, section=section)
    reason = NO_CELL_BOX if usable else NO_GEOMETRY
    region = _block_rect(block, reading)
    if region is not None and region[0] == page_no:
        return _finish(out, reading, "region", {page_no: {"rects": [region[1]]}}, region="table", degraded=reason,
                       table=table, section=section)
    return _page_only(out, stub, reading, reason, [page_no] if page_no else None, table=table, section=section)


def _refine_measurement(stub: dict, reading: Reading) -> dict:
    """A stored measurement's cell (the one cell of its row that holds its value) or word span (its quote, inside its
    own block), found in the stored reading; none when that is not unambiguous."""
    value = _number_value(stub["value"]) if stub.get("value") else None
    ti, row = stub.get("table_index"), stub.get("row")
    if value is None:
        return stub
    if ti is not None and row is not None:
        rows = (reading.tables.get(ti) or {}).get("rows") or []
        if 0 <= row < len(rows):
            hits = [j for j, c in enumerate(rows[row].get("cells") or []) if abs(value) in _values_in(c)]
            if len(hits) == 1:
                return stub | {"column": hits[0]}
        return stub
    a = stub.get("block_start")
    block = reading.blocks.get(a) if a is not None else None
    if block is not None and stub.get("quote"):
        try:
            found = locate_quote([(a, block.text)], stub["quote"], abs(value))
        except AmbiguousQuote:
            found = None
        if found is not None and found.segments:
            return stub | {"segments": found.segments, "number": found.number}
    return stub


def _structured(out: dict, stub: dict, reading: Reading) -> dict:
    """A DOCX anchor: the section path, paragraph number or normalized cell, and the cited text (R9)."""
    ti, row, col = stub.get("table_index"), stub.get("row"), stub.get("column")
    segments = stub.get("segments") or []
    indexes = [s[0] for s in segments] or stub.get("blocks") or _range(stub)[0]
    if not indexes and ti is not None and ti in reading.table_blocks:
        indexes = [reading.table_blocks[ti]]
    found = _found(reading, indexes)
    table = _table_ctx(reading, ti, row, col) if ti is not None else None
    cell = None
    st = reading.tables.get(ti) if ti is not None else None
    rows = (st or {}).get("rows") or []
    if st is not None and row is not None and col is not None and 0 <= row < len(rows):
        cells = rows[row].get("cells") or []
        headers = st.get("headers") or []
        if 0 <= col < len(cells):
            cell = {"table_index": ti, "row_number": row + 1, "column_number": col + 1,
                    "row_label": cells[0] or None, "column_header": headers[col] if col < len(headers) else None,
                    "text": cells[col]}
    nums = [b.paragraph_no for b in found if b.paragraph_no]
    body = cell["text"] if cell else "\n".join(b.text for b in found if b.text)
    highlight = None
    if cell is None and len(segments) == 1 and len(found) == 1 and segments[0][2] <= MAX_TEXT:
        highlight = [segments[0][1], segments[0][2]]
    section_path = list(found[0].section_path) if found and found[0].section_path else (
        [stub["section"]] if stub.get("section") else [])
    out["structured"] = {"section_path": section_path, "paragraph_no": min(nums) if nums else None,
                         "paragraph_end": max(nums) if len(set(nums)) > 1 else None,
                         "label": found[0].label if found else None, "text": body[:MAX_TEXT],
                         "highlight": highlight, "cell": cell}
    return _finish(out, reading, "structured", None, table=table,
                   section=section_path[-1] if section_path else None,
                   degraded=None if found or cell else NOT_LOCATED)


def _structured_stale(out: dict, stub: dict, reading: Reading, cited: str) -> dict:
    """A DOCX anchor whose reading changed before the answer was stored: the cited text and section as the tool
    returned them, none of the new reading's blocks."""
    section = stub.get("section")
    out["structured"] = {"section_path": [section] if section else [], "paragraph_no": None, "paragraph_end": None,
                         "label": None, "text": (cited or "")[:MAX_TEXT], "highlight": None, "cell": None}
    return _finish(out, reading, "structured", None, degraded=READING_CHANGED, section=section)


def snapshot(stub: dict, reading: Reading | None, pinned: str | None, cited: str = "") -> dict:
    """The self-contained anchor of one citation, from its stub and the stored reading of its version. ``pinned``:
    the reading the turn pinned for the version; ``cited``: the cited text as the answer keeps it (a stale DOCX
    anchor shows it)."""
    out = _base(stub, pinned)
    pinned = out["reading_id"]
    if reading is None:
        return _page_only(out, stub, None, UNAVAILABLE)
    if stub.get("anchor_lost"):
        return _page_only(out, stub, reading, ANCHOR_LOST, pages=[])
    if stub.get("reading_id") != pinned or reading.reading_id != pinned:
        if not reading.is_pdf:
            return _structured_stale(out, stub, reading, cited)
        return _page_only(out, stub, reading, READING_CHANGED)
    if stub.get("kind") == "measurement":
        stub = _refine_measurement(stub, reading)
    if not reading.is_pdf:
        return _structured(out, stub, reading)
    if stub.get("vision"):
        return _vision_cell(out, stub, reading)
    ti = stub.get("table_index")
    if ti is not None and stub.get("row") is not None:
        return _cell(out, stub, reading)
    table = _table_ctx(reading, ti, None, None) if ti is not None else None
    if stub.get("segments"):
        done = _span(out, stub, reading, table)
        if done is not None:
            return done
        return _blocks(out, stub, reading, [s[0] for s in stub["segments"]], table)
    if stub.get("blocks"):
        return _blocks(out, stub, reading, list(stub["blocks"]), table)
    indexes, cut = _range(stub)
    if not indexes and ti is not None and ti in reading.table_blocks:
        indexes = [reading.table_blocks[ti]]
    done = _blocks(out, stub, reading, indexes, table)
    done["truncated"] = done["truncated"] or cut
    return done


def bound(snap: dict) -> dict:
    """A snapshot within ``MAX_PAGES`` pages, ``MAX_RECTS`` rectangles a page and ``MAX_CHARS`` characters of JSON:
    what does not fit is drawn as a union or dropped, and the snapshot says it was cut."""
    if len(snap.get("pages") or []) > MAX_PAGES:
        snap["pages"] = snap["pages"][:MAX_PAGES]
        snap["truncated"] = True
    for p in snap.get("pages") or []:
        for key in ("rects", "focus"):
            if len(p.get(key) or []) > MAX_RECTS:
                p[key] = [_union(p[key])]
    if snap.get("structured") and len(snap["structured"].get("text") or "") > MAX_TEXT:
        snap["structured"]["text"] = snap["structured"]["text"][:MAX_TEXT]
    steps = [_drop_notes, _drop_focus, _union_rects, _fewer_pages, _shorter_text]
    while len(json.dumps(snap, ensure_ascii=False, default=str)) > MAX_CHARS and steps:
        steps.pop(0)(snap)
        snap["truncated"] = True
    return snap


def _drop_notes(snap: dict) -> None:
    if snap.get("table"):
        snap["table"]["notes"] = []


def _drop_focus(snap: dict) -> None:
    for p in snap.get("pages") or []:
        p["focus"] = []


def _union_rects(snap: dict) -> None:
    for p in snap.get("pages") or []:
        if len(p.get("rects") or []) > 1:
            p["rects"] = [_union(p["rects"])]


def _fewer_pages(snap: dict) -> None:
    snap["pages"] = (snap.get("pages") or [])[:2]


def _shorter_text(snap: dict) -> None:
    if snap.get("structured"):
        snap["structured"]["text"] = (snap["structured"].get("text") or "")[:200]


def computed_anchor(computation: dict) -> dict:
    """A computed result's anchor: its inputs (V#, A#, M#, C#), each opened through its own anchor — never a place in
    a document (KTD6)."""
    inputs = [x.get("id") if isinstance(x, dict) else str(x) for x in computation.get("inputs") or []]
    return {"v": SNAPSHOT_VERSION, "precision": COMPUTED, "precision_label": PRECISION_LABELS[COMPUTED],
            "inputs": [i for i in inputs if i]}


# --- the answer ---------------------------------------------------------------------------------------------------

CITED_KEYS = ("sources", "values", "measurements")


def anchored_documents(answer: dict | None) -> set[str]:
    """Every document an answer's anchors point at (the answer is shown only while all of them are visible)."""
    out = set()
    for key in CITED_KEYS:
        for d in (answer or {}).get(key) or []:
            a = d.get("anchor") if isinstance(d, dict) else None
            if isinstance(a, dict) and a.get("document_id"):
                out.add(str(a["document_id"]))
    return out


def attach(ws, answer: dict) -> None:
    """Snapshot the anchor of every source, value and measurement the answer keeps (``anchor`` on each), and the
    inputs of every computation, resolved in one transaction against the turn's pinned readings."""
    from app.db import tenant_tx

    for c in answer.get("computations") or []:
        c["anchor"] = computed_anchor(c)
    items = []
    for key in CITED_KEYS:
        for d in answer.get(key) or []:
            stub = ws.anchors.get(d.get("id")) if isinstance(d, dict) else None
            if stub is not None:
                items.append((d, stub, d.get("text") or d.get("quote") or ""))
    if not items:
        return
    readings: dict[str, Reading] = {}
    if ws.ctx is not None:
        try:
            with tenant_tx(ws.ctx) as conn:
                readings = load(conn, [s for _, s, _ in items])
        except Exception:  # noqa: BLE001 - an answer is stored even when its anchors cannot be resolved
            logger.exception("anchor resolution failed")
            readings = {}
    for d, stub, cited in items:
        vid = stub["version_id"]
        d["anchor"] = bound(snapshot(stub, readings.get(vid), ws.readings.get(vid, stub.get("reading_id")), cited))
