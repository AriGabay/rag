"""Table assembly (R4): logical column order, header detection, cross-page continuation (three-valued: ``piece_boundary``).

Input tables come from pdfplumber (cell grid, columns left -> right) or from OCR word boxes; both are
converted to logical (right-to-left) column order before assembly, so index 0 is the rightmost column
(``כתובת`` in the appraisal comparables table). Every row keeps its own physical page.

A text-layer table also keeps each cell's box (``RawTable.boxes``, parallel to its rows and cells): the boxes take the
same column reversal (``logical_boxes``) and the same row filtering and continuation as the cells, so the box at body
row ``k``, column ``j`` of an assembled table belongs to the value at ``rows[k].cells[j]`` (KTD2).
"""

from __future__ import annotations

import difflib
import functools
import math
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from app.extraction.base import TableResult, TableRow
from app.extraction.chunking import render_row
from app.extraction.hebrew import fix_line, has_hebrew, has_latin, strip_bidi_controls, visual_to_logical

HEADER_VOCAB = (
    "כתובת", "גוש", "חלקה", "תאריך", "שטח", "מחיר", "סוג", "חדרים", "קומה", "רחוב", "עסקה", "שווי", "יחידה",
)
# Canonical header texts of appraisal comparables tables. Used only to snap OCR'd header cells
# ("שטח (מ'"ר)" -> "שטח (מ״ר)"); text-layer headers are kept exactly as extracted.
CANONICAL_HEADERS = (
    "כתובת", "גוש/חלקה", "תאריך עסקה", "סוג נכס", "חדרים", "שטח (מ״ר)", "סוג שטח", "מחיר (₪)",
    "מחיר למ״ר (₪)", "קומה", "שווי (₪)",
)
_LETTERS = re.compile(r"[^\u05D0-\u05EA₪]")
_UNIT = re.compile(r"\(([^()]+)\)")
_SPACE = re.compile(r"\s+")
_DIGIT = re.compile(r"\d")
# A header cell that is only a year or a quarter ("2022", "Q3 2022", "3Q22"): its period, as (year, quarter).
_YEAR_CELL = re.compile(r"((?:19|20)\d{2})")
_QUARTER_CELL = re.compile(r"(?:Q([1-4])[ /\-]*((?:19|20)?\d{2})|((?:19|20)?\d{2})[ /\-]*Q([1-4])|([1-4])Q[ /\-]*((?:19|20)?\d{2}))", re.I)


def clean_cell(text: str | None) -> str:
    return _SPACE.sub(" ", text or "").strip()


def is_header_row(cells: list[str]) -> bool:
    """A header row has at least two cells, and most of its non-empty cells are digit-free and
    contain known Hebrew header vocabulary."""
    nonempty = [clean_cell(c) for c in cells if clean_cell(c)]
    if len(nonempty) < 2:
        return False
    hits = sum(1 for c in nonempty if not _DIGIT.search(c) and any(w in c for w in HEADER_VOCAB))
    return hits >= max(2, math.ceil(len(nonempty) / 2))


def snap_header(text: str, min_ratio: float = 0.8) -> str:
    """Snap an OCR'd header cell to the closest canonical header (letters and ₪ only compared)."""
    key = _LETTERS.sub("", text or "")
    if not key:
        return clean_cell(text)
    best, best_ratio = None, 0.0
    for cand in CANONICAL_HEADERS:
        cand_key = _LETTERS.sub("", cand)
        if ("₪" in key) != ("₪" in cand_key):
            continue  # never add or drop a unit that was (not) read
        ratio = difflib.SequenceMatcher(None, key, cand_key).ratio()
        if ratio > best_ratio:
            best, best_ratio = cand, ratio
    return best if best is not None and best_ratio >= min_ratio else clean_cell(text)


def units_for(headers: list[str]) -> list[str | None]:
    out: list[str | None] = []
    for h in headers:
        m = _UNIT.search(h or "")
        out.append(m.group(1).strip() if m else None)
    return out


def _period(cell: str | None) -> tuple[int, int] | None:
    """The (year, quarter) a header cell names when it is only a year or a quarter of a year; quarter 0: the whole year."""
    text = clean_cell(cell)
    m = _YEAR_CELL.fullmatch(text)
    if m:
        return int(m.group(1)), 0
    m = _QUARTER_CELL.fullmatch(text)
    if not m:
        return None
    quarter, year = next((int(q), int(y)) for q, y in ((m.group(1), m.group(2)), (m.group(4), m.group(3)), (m.group(5), m.group(6))) if q)
    return (year + 2000 if year < 100 else year), quarter


def _periods_rise(cells: list[str]) -> bool:
    """Whether the years or quarters a row writes (at least two) rise from left to right."""
    periods = [p for p in map(_period, cells) if p is not None]
    return len(periods) >= 2 and all(a < b for a, b in zip(periods, periods[1:], strict=False))


def reads_left_to_right(grid: list[list[str | None]], page_has_hebrew: bool) -> bool:
    """Whether a text-layer table is stored in the order it reads: neither the table nor its page holds a Hebrew letter, and the
    table gives a left-to-right cue: a cell with Latin letters in its first row, or years or quarters that rise from left to
    right along a row. A table of numbers only has no cue, and keeps the right-to-left handling (KTD4)."""
    if page_has_hebrew or any(has_hebrew(c or "") for row in grid for c in row):
        return False
    first = next((row for row in grid if any(clean_cell(c) for c in row)), [])
    if any(has_latin(c or "") for c in first):
        return True
    return any(_periods_rise(row) for row in grid)


def logical_row(cells_ltr: list[str | None], visual_default: bool = True, left_to_right: bool = False) -> list[str]:
    """pdfplumber cells (left -> right, visual text) -> logical column order with logical cell text.
    Wrapped cell lines are fixed one by one and joined with a space. ``left_to_right`` (``reads_left_to_right``): the columns
    keep the order they are stored in and the cell text, which holds no Hebrew letter, is kept as it is."""
    if left_to_right and not any(has_hebrew(c or "") for c in cells_ltr):
        return [clean_cell(" ".join(ln for ln in (cell or "").splitlines() if ln.strip())) for cell in cells_ltr]
    out = []
    for cell in reversed(cells_ltr):
        lines = [ln for ln in (cell or "").splitlines() if ln.strip()]
        out.append(clean_cell(" ".join(fix_line(ln, visual_default) for ln in lines)))
    return out


def logical_boxes(boxes_ltr: list | None, left_to_right: bool = False) -> list:
    """pdfplumber cell boxes (left -> right) in the column order ``logical_row`` gives their cells."""
    return list(boxes_ltr or []) if left_to_right else list(reversed(boxes_ltr or []))


@dataclass
class RawTable:
    """One table found on one page, rows already in logical column order. ``assemble_tables`` records which
    table the piece went into (``table_index``) and what it decided about the piece before it (``decision``, ``continues``,
    ``break_before``).

    The evidence the decision reads is the producer's (round 22 U3, KTD3): ``gap`` the texts of the headings and paragraphs
    that are no page furniture between the piece before and this one (an empty string: a picture), ``caption`` the text that
    introduces it, ``notes`` the note lines right under it. ``decision`` is ``CONTINUES``, ``SEPARATE`` or ``UNDECIDED`` (None:
    the first piece); ``break_before`` the evidence that keeps it apart from the piece before (or, for ``UNDECIDED``, why it
    could not join it)."""

    page: int | None
    rows: list[list[str]]
    ocr: bool = False
    section: str | None = None
    break_before: str | None = None
    top: float = 0.0
    bbox: list[float] | None = None
    table_index: int | None = None
    continues: bool = False
    uncertain: str | None = None  # why some of its text is uncertain (an unrepaired font map)
    boxes: list[list[list[float] | None]] | None = None  # each cell's box, parallel to ``rows``; None: no positions
    gap: list[str] = field(default_factory=list)
    caption: str | None = None
    notes: list[str] = field(default_factory=list)
    decision: str | None = None
    extent: tuple[float, float] | None = None  # the content area of its page (top, bottom), in the frame of ``bbox`` (the walker's)


def _width(t: TableResult) -> int:
    if t.headers:
        return len(t.headers)
    return len(t.rows[0].cells) if t.rows else 0


# --- the boundary between two table pieces (round 22 U3, KTD3, R6) ---------------------------------------------------------------
#
# One decision for the PDF walker (``pdf._Walker.finish``, which knows the blocks between two pieces) and the positions re-read
# (it runs the same walker): ``piece_boundary``. A piece continues the table before it on a repeated header with nothing
# between the two parts; a heading or paragraph that is no page furniture, a note closing the first part, another caption, another
# section or other column x-geometry keeps them apart; the same shape with nothing between but no repeated header continues only
# when the cell boxes of both sides align column by column and the earlier part ends at the bottom of its page and the later one starts
# at the top of the next (``REGION_SHARE``); otherwise it is undecided:
# separate parts, the later one linked to the earlier (``TableResult.candidate_of``), and no consumer crosses that link.

CONTINUES, SEPARATE, UNDECIDED = "continues", "separate", "undecided"
REGION_SHARE = 1 / 3  # a headerless piece continues only if the part before ends in the last third of its page's content area and it starts in the first third
GEOMETRY_TOLERANCE = 12.0  # points; a column edge that moves less, or less than a tenth of the table's width, is the same column
GEOMETRY_SHARE = 0.1


def _norm_caption(text: str | None) -> str:
    return " ".join(strip_bidi_controls(text or "").split())


def _column_edges(raw: RawTable) -> list[tuple[float, float]] | None:
    """The x extent of each column of a piece: the median left and right edge of its cell boxes. None: no positions."""
    if not raw.boxes:
        return None
    width = max((len(b) for b in raw.boxes if b), default=0)
    out = []
    for j in range(width):
        cells = [b[j] for b in raw.boxes if b and j < len(b) and b[j]]
        if not cells:
            return None
        lefts, rights = sorted(c[0] for c in cells), sorted(c[2] for c in cells)
        out.append((lefts[len(lefts) // 2], rights[len(rights) // 2]))
    return out


def _geometry_differs(prev: RawTable | None, raw: RawTable) -> bool:
    """Whether the two pieces' columns stand at other x positions: their cell boxes' column edges, else the pieces' own
    regions. False when either has no positions: no evidence either way."""
    if prev is None:
        return False
    a, b = _column_edges(prev), _column_edges(raw)
    if a is not None and b is not None and len(a) == len(b):
        span = max(r for _, r in a) - min(left for left, _ in a)
        tolerance = max(GEOMETRY_TOLERANCE, GEOMETRY_SHARE * span)
        return any(abs(x0 - y0) > tolerance or abs(x1 - y1) > tolerance for (x0, x1), (y0, y1) in zip(a, b, strict=True))
    if prev.bbox and raw.bbox:
        tolerance = max(GEOMETRY_TOLERANCE, GEOMETRY_SHARE * (prev.bbox[2] - prev.bbox[0]))
        return abs(prev.bbox[0] - raw.bbox[0]) > tolerance or abs(prev.bbox[2] - raw.bbox[2]) > tolerance
    return False


def _edge_regions(prev: RawTable, raw: RawTable) -> bool:
    """Whether the earlier piece ends in the bottom ``REGION_SHARE`` of its page's content area and the later one starts in the top
    ``REGION_SHARE`` of its own (``RawTable.extent``). False when either has no region or no extent: no evidence."""
    for piece, ends in ((prev, True), (raw, False)):
        if not piece.bbox or not piece.extent:
            return False
        top, bottom = piece.extent
        span = bottom - top
        if span <= 0 or (ends and piece.bbox[3] < bottom - REGION_SHARE * span) or (not ends and piece.bbox[1] > top + REGION_SHARE * span):
            return False
    return True


def _aligned_columns(prev: RawTable, raw: RawTable) -> bool:
    """Whether both pieces have cell boxes and every column stands in the same x-band (``_geometry_differs`` said it does not move)."""
    a, b = _column_edges(prev), _column_edges(raw)
    return a is not None and b is not None and len(a) == len(b)


def piece_boundary(prev: TableResult | None, prev_raw: RawTable | None, raw: RawTable, ncols: int, headers: list[str],
                   carried: Sequence[str] = ()) -> tuple[str | None, str | None]:
    """What ``raw`` (``ncols`` columns, ``headers`` its header row or ``[]``) is to the table before it (``prev``, last piece
    ``prev_raw``): ``(decision, evidence)``. ``carried``: what stood between, from pieces with no rows. ``(None, None)`` for the first
    piece. Pieces on pages that do not follow each other, or of another width, are separate with no link (nothing says they may
    be the same table); a piece that follows with the same width is decided by the evidence of the module comment above."""
    if prev is None or prev_raw is None:
        return None, None
    if raw.page is None or prev.page_end is None or raw.page != prev.page_end + 1:
        return SEPARATE, "pages"
    if _width(prev) != ncols:
        return SEPARATE, "width"
    if raw.gap or carried:
        return SEPARATE, "block"
    if prev_raw.notes:
        return SEPARATE, "note"
    if prev.section != raw.section:
        return SEPARATE, "section"
    if prev_raw.caption and raw.caption and _norm_caption(prev_raw.caption) != _norm_caption(raw.caption):
        return SEPARATE, "caption"
    if _geometry_differs(prev_raw, raw):
        return SEPARATE, "geometry"
    if headers and prev.headers == headers:
        return CONTINUES, None
    if not headers and _aligned_columns(prev_raw, raw) and _edge_regions(prev_raw, raw):
        # no repeated header, but the structure says the rows run on: the same columns at the same x positions (cell boxes on both
        # sides), the earlier part at the bottom of its page and this one at the top of the next, nothing between (round 22 U3)
        return CONTINUES, None
    return UNDECIDED, "no repeated header" if not headers else "header differs"


def assemble_tables(raws: list[RawTable]) -> list[TableResult]:
    tables: list[TableResult] = []
    prev_raw: RawTable | None = None
    carried: list[str] = []
    for raw in raws:
        rows, boxes = _rows_and_boxes(raw)
        if not rows:
            carried += [*raw.gap, *raw.notes]  # a piece with no row is no table, but what stood around it still stood between
            continue
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        boxes = [b + [None] * (ncols - len(b)) if b is not None else None for b in boxes]
        has_header = is_header_row(rows[0])
        headers, body, body_boxes = (rows[0], rows[1:], boxes[1:]) if has_header else ([], rows, boxes)
        prev = tables[-1] if tables else None
        raw.decision, raw.break_before = piece_boundary(prev, prev_raw, raw, ncols, headers, carried)
        carried = []
        if raw.decision == CONTINUES:
            _append(prev, body, body_boxes, raw)  # the repeated header is dropped (D9)
        else:
            _new(tables, headers, body, body_boxes, raw, header_boxes=boxes[0] if has_header else None,
                 candidate_of=prev.index if raw.decision == UNDECIDED else None)
        prev_raw = raw
    return tables


def _rows_and_boxes(raw: RawTable) -> tuple[list[list[str]], list[list | None]]:
    """The piece's non-empty rows (cleaned) with each row's cell boxes, or None per row when the piece has no
    positions or they do not line up with its cells."""
    boxes = raw.boxes
    if boxes is None or len(boxes) != len(raw.rows) or any(
            b is not None and len(b) != len(r) for r, b in zip(raw.rows, boxes, strict=True)):
        boxes = [None] * len(raw.rows)
    kept = [([clean_cell(c) for c in r], list(b) if b is not None else None)
            for r, b in zip(raw.rows, boxes, strict=True) if any(clean_cell(c) for c in r)]
    return [r for r, _ in kept], [b for _, b in kept]


def _new(tables: list[TableResult], headers: list[str], body: list[list[str]], boxes: list[list | None],
         raw: RawTable, header_boxes: list | None = None, candidate_of: int | None = None) -> None:
    raw.table_index = len(tables)
    tables.append(
        TableResult(
            index=len(tables),
            headers=headers,
            units=units_for(headers),
            rows=[TableRow(page=raw.page, cells=r, cell_boxes=b) for r, b in zip(body, boxes, strict=True)],
            page_start=raw.page,
            page_end=raw.page,
            ocr=raw.ocr,
            section=raw.section,
            header_boxes=header_boxes,
            candidate_of=candidate_of,
        )
    )


def _append(table: TableResult, body: list[list[str]], boxes: list[list | None], raw: RawTable) -> None:
    raw.table_index, raw.continues = table.index, True
    table.rows.extend(TableRow(page=raw.page, cells=r, cell_boxes=b) for r, b in zip(body, boxes, strict=True))
    table.page_end = raw.page
    table.ocr = table.ocr or raw.ocr


# --- serving a stored table (round 21 U8, KTD6; R11) -------------------------------------------------------------------------------
#
# Round 20 changed how a text-layer table with no Hebrew is stored (``reads_left_to_right``) without a ``READER_VERSION`` bump, so
# the tables already stored come in two generations: a left-to-right table stored reversed (the round-19 reading: its logical row
# runs from the right edge to the left one) and one stored in the order it is drawn. Nothing stored is rewritten (a re-read needs
# approval); the order is recovered at serve time from what the stored data itself shows. All three must hold for a table to be
# served reversed: its stored cell boxes run from right to left, it holds no Hebrew letter, and the left-to-right cue holds on the
# table read in the order it is drawn. A table with no boxes, with ascending boxes, with a Hebrew letter or with no cue is served
# as stored. Round 22 U4 (KTD4): Hebrew on the table's pages (``PAGE_HEBREW``) also means as stored, as ingestion read the table, and
# when the page context and the cue decide the boxes are not asked (``serve_direction``). A served table keeps, for each column, the stored column it came from (``columns``): the stored index is what a cell's
# value, its box and the datum's identity are looked up by.

_LETTER = re.compile(r"[^\W\d_]")
_DIGIT_RUN = re.compile(r"\d")


def _x0(box) -> float | None:
    try:
        return float(box[0])
    except (TypeError, ValueError, IndexError):
        return None


def _boxes_descend(rows_of_boxes) -> bool | None:
    """Whether the stored cell boxes run from right to left: True when every row with two positioned cells has its first cell to the
    right of its last, False when some row runs the other way, None when no row says (no boxes, or boxes at one x)."""
    verdicts = []
    for boxes in rows_of_boxes:
        xs = [x for x in (_x0(b) for b in boxes or [] if b) if x is not None]
        if len(xs) >= 2 and xs[0] != xs[-1]:
            verdicts.append(xs[0] > xs[-1])
    return None if not verdicts else all(verdicts)


def _texts(st: dict) -> Iterator[str]:
    """The header and cell texts of a stored structure, one at a time (a caller that looks for a Hebrew letter stops at the first)."""
    for c in st.get("headers") or []:
        yield c or ""
    for r in st.get("rows") or []:
        for c in r.get("cells") or []:
            yield c or ""


PAGE_HEBREW = "page_hebrew"  # the page context a loader attaches to a structure it reads (``context_pages`` names the pages it is read from)


class Direction(NamedTuple):
    """How a stored table is served (round 22 U4, KTD4): ``reversed`` (its stored columns run right to left and are served in the order
    they are drawn), ``basis`` (what decided it: ``cells`` a Hebrew letter in the table, ``page`` Hebrew on the table's pages, ``cue`` no
    left-to-right cue, ``boxes`` the stored cell boxes, ``unknown`` no box says), and ``inferred`` (the boxes or nothing decided it, not
    the page context and the cell cue)."""

    reversed: bool
    basis: str
    inferred: bool


def context_pages(st: dict | None) -> list[int]:
    """The pages a stored table's Hebrew context is taken from (round 22 U4): the pages of its rows, else its own page. Empty for a
    table the context cannot change (a structure already served, one that holds a Hebrew letter: both are decided without it) and for
    one that already carries it."""
    if not st or st.get("columns") is not None or PAGE_HEBREW in st or any(has_hebrew(c) for c in _texts(st)):
        return []
    pages = sorted({r["page"] for r in st.get("rows") or [] if isinstance(r, dict) and isinstance(r.get("page"), int)})
    return pages or ([st["page"]] if isinstance(st.get("page"), int) else [])


def serve_direction(st: dict | None) -> Direction:
    """The one decision of how a stored table is served (round 22 U4, KTD4, R7): a pure function of the stored structure and the Hebrew
    context of its pages (``PAGE_HEBREW``: whether a page of its rows holds a Hebrew letter, as the stored page text shows; absent when
    the structure was read without it). Every path that shows a cell (search, read, take_value, the citation and the source viewer)
    asks this function, so a cell has one identity whatever path reads it.

    Ingestion stored a table reversed unless ``reads_left_to_right`` held for it with the context of its page. So: a Hebrew letter in the
    table, or Hebrew on its page, means it was stored for right-to-left reading and is served as stored, whatever its boxes show (a box
    backfill cannot change it); a table with no left-to-right cue (read in the order it is drawn) was stored reversed by every reader
    and is served as stored. What is left (no Hebrew anywhere, the cue holds) is a table the round-19 reading stored reversed or a
    later one stored in the order it is drawn: only its stored cell boxes tell, and the direction is then inferred."""
    if not st or st.get("columns") is not None:  # a structure already served
        return Direction(False, "served", False)
    if any(has_hebrew(c) for c in _texts(st)):
        return Direction(False, "cells", False)
    if st.get(PAGE_HEBREW):
        return Direction(False, "page", False)
    rows = st.get("rows") or []
    headers = st.get("headers") or []
    grid = ([headers[::-1]] if any(headers) else []) + [list(r.get("cells") or [])[::-1] for r in rows]
    if not reads_left_to_right(grid, False):
        return Direction(False, "cue", False)
    boxes = _boxes_descend([st.get("header_boxes"), *(r.get("cell_boxes") for r in rows)])
    return Direction(boxes is True, "unknown" if boxes is None else "boxes", True)


def stored_reversed(st: dict | None) -> bool:
    """Whether a stored table is a left-to-right table stored in reversed cell order and served in the order it is drawn
    (``serve_direction``)."""
    return serve_direction(st).reversed


def _width_of(st: dict) -> int:
    return max([len(st.get("headers") or []), *(len(r.get("cells") or []) for r in st.get("rows") or [])] or [0])


def column_order(st: dict | None) -> list[int] | None:
    """The stored column at each served position (right to left reversed: ``[3, 2, 1, 0]``), or None when the table is served as
    stored."""
    if not stored_reversed(st):
        return None
    return list(range(_width_of(st) - 1, -1, -1))


def fix_sign(cell: str) -> str:
    """A number a reversed reading stored with its minus at the end ("1,200-", "5%-", "₪ 3-") written with its sign in front, as
    the left-to-right row it is in reads it. A cell with a letter, or with no digit, or not ending in a minus, is returned as it is."""
    text = cell or ""
    if _DIGIT_RUN.search(text) and not _LETTER.search(text) and text.rstrip().endswith("-"):
        return visual_to_logical(text)
    return text


def _pick(seq, order: list[int], default):
    """``seq`` at the stored positions ``order`` names; ``default`` where ``seq`` is shorter than a position."""
    return [seq[i] if i < len(seq) else default for i in order]


def serve_cells(order: list[int], cells: list[str], fixed: bool = True) -> list[str]:
    """A stored row's cells at the served positions (``order``); a stored cell the row lacks is empty."""
    return [fix_sign(c) if fixed else c for c in _pick(cells, order, "")]


class _ById:
    """A stored structure as a cache key: equal when it is the very same object (a stored structure is read once per turn and never
    changed), hashed by identity. The cache holds the key, so the structure it names stays alive and its identity stays its own."""

    __slots__ = ("st",)

    def __init__(self, st: dict):
        self.st = st

    def __hash__(self) -> int:
        return id(self.st)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ById) and other.st is self.st


def spans_pages(st: dict | None) -> bool:
    """Whether the rows of a stored table are on more than one page (a table continued across pages; round 22 U0, the
    measurement of how many such tables are served). Reads the stored rows' ``page`` only."""
    return len({r.get("page") for r in (st or {}).get("rows") or [] if isinstance(r, dict) and r.get("page")}) > 1


# --- page furniture, and a stored table that spans pages (round 22 U3, KTD3, R9) ---------------------------------------------------
#
# Page furniture is a normalized line (digits and punctuation dropped) that recurs at the same place on a large share of the pages: a
# running header never separates two table pieces. The same idea as ``app.chat.units`` (``FURNITURE_SHARE``: a text on half of the pages,
# and at least ``FURNITURE_MIN_PAGES``), written here because the extraction layer does not import the chat layer; it adds the place
# (the top of the line, within ``FURNITURE_TOP_TOLERANCE`` points) so a heading that repeats at other places stays a heading.

FURNITURE_MIN_PAGES = 3
FURNITURE_SHARE = 0.5
FURNITURE_TOP_TOLERANCE = 6.0
_NON_WORD = re.compile(r"[\W\d_]+")


def norm_line(text: str | None) -> str:
    """A line as page furniture is compared: its letters only (no digits, punctuation, direction marks or spacing)."""
    return " ".join(_NON_WORD.sub(" ", strip_bidi_controls(text or "")).split())


class BlockAt(NamedTuple):
    """A heading or paragraph on a page: ``top`` in the frame of the rendered page (None: not known) and its ``text``."""

    page: int
    top: float | None
    text: str


@dataclass(frozen=True)
class Furniture:
    """The normalized lines that recur, each with the tops they recur at (``None`` among them: at any place)."""

    places: dict[str, tuple[float | None, ...]]

    def covers(self, top: float | None, text: str | None) -> bool:
        tops = self.places.get(norm_line(text))
        if not tops:
            return False
        return top is None or None in tops or any(abs(top - t) <= FURNITURE_TOP_TOLERANCE for t in tops)


def furniture_of(entries: Iterable[tuple[int, float | None, str]], pages_total: int | None = None) -> Furniture:
    """The page furniture among ``entries`` (page, top, text): a normalized line at one place on at least ``FURNITURE_SHARE`` of the
    ``pages_total`` pages (by default the pages the entries are on) and at least ``FURNITURE_MIN_PAGES``."""
    groups: dict[str, list[tuple[int, float | None]]] = {}
    for page, top, text in entries:
        key = norm_line(text)
        if key:
            groups.setdefault(key, []).append((page, top))
    total = pages_total if pages_total is not None else len({p for g in groups.values() for p, _ in g})
    need = max(FURNITURE_MIN_PAGES, math.ceil(FURNITURE_SHARE * total))
    places: dict[str, tuple[float | None, ...]] = {}
    for key, group in groups.items():
        if len({p for p, _ in group}) < need:
            continue
        anchors = sorted({round(t, 1) for _, t in group if t is not None})
        if not anchors:
            places[key] = (None,)
            continue
        kept = [a for a in anchors
                if len({p for p, t in group if t is None or abs(t - a) <= FURNITURE_TOP_TOLERANCE}) >= need]
        if kept:
            places[key] = tuple(kept)
    return Furniture(places)


NO_FURNITURE = Furniture({})


def _row_extent(row: dict) -> tuple[float, float] | None:
    """A stored row's top and bottom on the rendered page, from its cell boxes; None for a row stored without them."""
    try:
        boxes = [b for b in row.get("cell_boxes") or [] if b]
        return min(float(b[1]) for b in boxes), max(float(b[3]) for b in boxes)
    except (TypeError, ValueError, IndexError):
        return None


def _page_position(page_text: Callable[[int], str | None] | None, page: int, snippet: str) -> int | None:
    """Where ``snippet`` first stands in the stored text of ``page`` (None: not known)."""
    text = page_text(page) if page_text is not None else None
    snippet = (snippet or "").strip()
    at = text.find(snippet) if text and snippet else -1
    return at if at >= 0 else None


def _first_cell(row: dict) -> str:
    return next((c.strip() for c in row.get("cells") or [] if c and c.strip()), "")


def unconfirmed_pages(st: dict | None, blocks: Iterable[BlockAt], furniture: Furniture | None = None,
                      page_text: Callable[[int], str | None] | None = None) -> dict[int, str]:
    """The pages of a stored table whose rows come after a separation: ``{page: the text that separates it}`` (R9). A table whose rows
    span pages records no parts, so what stood between them is read from the stored blocks. ``blocks``: the headings and paragraphs
    of the table's pages that come after the table's own block (the continuation pieces left no block of their own, so a block of a
    later page is either above their rows or below them). Between two row pages, a block is evidence of a separation when it is no
    page furniture (``furniture``) and it stands under the earlier page's last row, on a page between, or above the later page's first
    row: by their tops in the frame of the rendered page when both are stored, else by where they stand in the page's stored text
    (``page_text``), else conservatively (a block whose place is not known counts). The earlier page of a table is the caption's own.
    Every page after a separation is unconfirmed: it may take its header, scale and notes from nothing. ``{}``: one page, or no
    separation, and the caption's scale and notes apply to every page."""
    rows = [r for r in (st or {}).get("rows") or [] if isinstance(r, dict)]
    order: list[int] = []
    by_page: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("page"):
            if r["page"] not in by_page:
                order.append(r["page"])
            by_page.setdefault(r["page"], []).append(r)
    if len(order) < 2:
        return {}
    furn = furniture or NO_FURNITURE
    cands = [b for b in blocks if norm_line(b.text) and not furn.covers(b.top, b.text)]
    out: dict[int, str] = {}
    why = ""
    for q, p in zip(order, order[1:], strict=False):
        if not why:
            why = _separator(cands, q, p, by_page, q == order[0], page_text)
        if why:
            out[p] = why
    return out


def _separator(cands: list[BlockAt], q: int, p: int, by_page: dict[int, list[dict]], q_is_first: bool,
               page_text: Callable[[int], str | None] | None) -> str:
    """The text of the first block that separates the rows of page ``q`` from the next rows, on page ``p``; "" when none does."""
    last = _row_extent(by_page[q][-1])
    first = _row_extent(by_page[p][0])
    q_rows_end = _page_position(page_text, q, _first_cell(by_page[q][-1])) if page_text else None
    p_rows_start = _page_position(page_text, p, _first_cell(by_page[p][0])) if page_text else None
    for b in cands:
        if q < b.page < p:
            return b.text.strip()[:120]
        if b.page == q:
            if last is not None and b.top is not None:
                below = b.top >= last[1]
            elif q_is_first:
                below = True  # after the table's own block on the page its first rows are on: under them
            else:
                at = _page_position(page_text, q, b.text.strip().split("\n")[0])
                below = True if at is None or q_rows_end is None else at >= q_rows_end
            if below:
                return b.text.strip()[:120]
        elif b.page == p:
            if first is not None and b.top is not None:
                above = b.top <= first[0]
            else:
                at = _page_position(page_text, p, b.text.strip().split("\n")[0])
                above = True if at is None or p_rows_start is None else at <= p_rows_start
            if above:
                return b.text.strip()[:120]
    return ""


def serve_structure(st: dict | None) -> dict | None:
    """A stored table structure in the order it is drawn, when the stored data show it was stored reversed (``stored_reversed``);
    ``st`` itself otherwise. The served structure holds the headers, units, cells and cell boxes at the served positions and
    ``columns``: the stored column index at each position (a value's box and its datum are looked up by it). Nothing stored is
    changed: a new structure is built, once per stored structure (the callers only read it)."""
    return _served(_ById(st)) if st else st


@functools.lru_cache(maxsize=64)
def _served(key: _ById) -> dict:
    st = key.st
    order = column_order(st)
    if order is None:
        return st
    headers = st.get("headers") or []
    units = st.get("units") or []
    out = dict(st)
    out["columns"] = order
    out["direction"] = serve_direction(st)._asdict()  # why it is served reversed, and whether that is inferred (round 22 U4)
    out["headers"] = serve_cells(order, headers, fixed=False) if any(headers) else []
    out["units"] = _pick(units, order, None) if units else []
    if st.get("header_boxes"):
        out["header_boxes"] = _pick(st["header_boxes"], order, None)
    out["rows"] = [
        {**r, "cells": serve_cells(order, list(r.get("cells") or [])),
         **({"cell_boxes": _pick(r["cell_boxes"], order, None)} if r.get("cell_boxes") else {})}
        for r in st.get("rows") or []]
    return out


def stored_column(st: dict | None, position: int) -> int:
    """The stored column of a served position (the position itself in a table served as stored)."""
    order = (st or {}).get("columns")
    return order[position] if order is not None and 0 <= position < len(order) else position


def served_position(st: dict | None, column: int) -> int:
    """The served position of a stored column (the column itself in a table served as stored)."""
    order = (st or {}).get("columns")
    return order.index(column) if order is not None and column in order else column


def served_labels(sst: dict) -> tuple[list[str], int | None]:
    """The column labels of a served table, and the row they come from when it has no header of its own: the headers; else its
    first row when that row is the one that gives the left-to-right cue (a cell with Latin letters, or periods that rise) — a
    Latin header row is not a header to the extraction, which knows Hebrew header words only. No labels: ``([], None)``."""
    headers = sst.get("headers") or []
    if any(headers):
        return list(headers), None
    rows = sst.get("rows") or []
    first = next((i for i, r in enumerate(rows) if any(clean_cell(c) for c in r.get("cells") or [])), None)
    if first is None:
        return [], None
    cells = list(rows[first].get("cells") or [])
    if any(has_latin(c or "") for c in cells) or _periods_rise(cells):
        return cells, first
    return [], None


def _served_units(sst: dict) -> list[str | None] | None:
    """The units ``render_row`` pairs with the labels of a served table: its own, when it has headers; none otherwise."""
    return sst.get("units") or None if any(sst.get("headers") or []) else None


def render_served_row(sst: dict, index: int, labels: tuple[list[str], int | None] | None = None) -> str:
    """One row of a served table as a row chunk renders it (``render_row``): each cell paired with its column label, in the order
    the table is drawn. The row that holds the labels is its cells in order; a table with no label gives its cells in order.
    ``labels``: ``served_labels(sst)`` when the caller renders several rows of the table."""
    row = sst["rows"][index]
    cells = list(row.get("cells") or [])
    labels, label_row = labels if labels is not None else served_labels(sst)
    if not labels or index == label_row:
        return " | ".join(cells)
    return render_row(labels, cells, _served_units(sst))


def render_served_cells(sst: dict, cells: list[str], labels: tuple[list[str], int | None] | None = None) -> str:
    """The same for a row that is not one of the structure's own (a window the caller cut): its stored cells are put in served
    order first. ``labels``: ``served_labels(sst)`` when the caller renders several rows of the table."""
    order = sst.get("columns") or []
    served = serve_cells(order, cells)
    labels, _ = labels if labels is not None else served_labels(sst)
    if not labels or served == labels:
        return " | ".join(served)
    return render_row(labels, served, _served_units(sst))


def serve_text(st: dict | None, text: str) -> str:
    """The text of a stored table block or table chunk (the header line and one ``a | b | c`` line per row, then notes) with its
    header and row lines of a table served reversed replaced by the served ones; any other line, and the text of a table served as
    stored, is returned as it is."""
    sst = serve_structure(st)
    if sst is st or not text:
        return text
    mapping: dict[str, str] = {}
    headers = st.get("headers") or []
    if any(headers):
        mapping[" | ".join(headers)] = " | ".join(sst["headers"])
    labels = served_labels(sst)
    for i, r in enumerate(st.get("rows") or []):
        line = " | ".join(r.get("cells") or [])
        if line.strip(" |"):
            mapping.setdefault(line, render_served_row(sst, i, labels))
    return "\n".join(mapping.get(ln, ln) for ln in text.split("\n"))


def serve_row_text(st: dict | None, row_index: int | None, text: str) -> str:
    """The text of a ``table_row`` chunk (its table's title, then the row as ``render_row`` wrote it) with the row paired with its
    column labels in the order the table is drawn, when the table is served reversed; ``text`` as it is otherwise, and when the
    chunk is not that row as stored. The stored chunk is not touched: this is what a hit shows."""
    sst = serve_structure(st)
    rows = (st or {}).get("rows") or []
    if sst is st or row_index is None or not 0 <= row_index < len(rows):
        return text
    stored = render_row(st.get("headers") or [], list(rows[row_index].get("cells") or []), st.get("units") or None)
    if not stored or not text.endswith(stored):
        return text
    return text[:len(text) - len(stored)] + render_served_row(sst, row_index)


# --- the read-only count of stored tables by class (KTD6, first step) -----------------------------------------------------------

CLASSES = ("reversed_stored", "drawn_stored", "no_boxes", "hebrew", "other")


def classify_stored(st: dict | None) -> str:
    """The class of a stored table: ``reversed_stored`` (served reversed), ``drawn_stored`` (no Hebrew, boxes that ascend, the
    left-to-right cue holds as stored), ``no_boxes`` (no Hebrew and no cell box: the order cannot be told), ``hebrew`` (a Hebrew
    letter in a header or a cell), ``other`` (no Hebrew with boxes, but no cue, or boxes that do not settle the order)."""
    st = st or {}
    if any(has_hebrew(c) for c in _texts(st)):
        return "hebrew"
    if stored_reversed(st):
        return "reversed_stored"
    rows = st.get("rows") or []
    boxes = [st.get("header_boxes"), *(r.get("cell_boxes") for r in rows)]
    if not any(b for b in boxes):
        return "no_boxes"
    headers = st.get("headers") or []
    grid = ([list(headers)] if any(headers) else []) + [list(r.get("cells") or []) for r in rows]
    if _boxes_descend(boxes) is False and reads_left_to_right(grid, False):
        return "drawn_stored"
    return "other"


def count_stored(structures) -> dict[str, int]:
    """The number of stored tables in each class (``CLASSES``) among ``structures`` (each an ``extracted_tables.structure``)."""
    counts = dict.fromkeys(CLASSES, 0)
    for st in structures:
        counts[classify_stored(st)] += 1
    return counts
