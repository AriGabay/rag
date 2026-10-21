"""Table assembly (R4): logical column order, header detection, cross-page continuation.

Input tables come from pdfplumber (cell grid, columns left -> right) or from OCR word boxes; both are
converted to logical (right-to-left) column order before assembly, so index 0 is the rightmost column
(``כתובת`` in the appraisal comparables table). Every row keeps its own physical page.

A text-layer table also keeps each cell's box (``RawTable.boxes``, parallel to its rows and cells): the boxes take the
same column reversal (``logical_boxes``) and the same row filtering and continuation as the cells, so the box at body
row ``k``, column ``j`` of an assembled table belongs to the value at ``rows[k].cells[j]`` (KTD2).
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass

from app.extraction.base import TableResult, TableRow
from app.extraction.hebrew import fix_line, has_hebrew, has_latin, visual_to_logical

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


def reads_left_to_right(grid: list[list[str | None]], page_has_hebrew: bool) -> bool:
    """Whether a text-layer table is stored in the order it reads: neither the table nor its page holds a Hebrew letter, and the
    table gives a left-to-right cue: a cell with Latin letters in its first row, or years or quarters that rise from left to
    right along a row. A table of numbers only has no cue, and keeps the right-to-left handling (KTD4)."""
    if page_has_hebrew or any(has_hebrew(c or "") for row in grid for c in row):
        return False
    first = next((row for row in grid if any(clean_cell(c) for c in row)), [])
    if any(has_latin(c or "") for c in first):
        return True
    for row in grid:
        periods = [p for p in map(_period, row) if p is not None]
        if len(periods) >= 2 and all(a < b for a, b in zip(periods, periods[1:], strict=False)):
            return True
    return False


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
    table the piece went into (``table_index``) and whether it continued an earlier one (``continues``)."""

    page: int | None
    rows: list[list[str]]
    ocr: bool = False
    section: str | None = None
    first_on_page: bool = True
    top: float = 0.0
    bbox: list[float] | None = None
    table_index: int | None = None
    continues: bool = False
    uncertain: str | None = None  # why some of its text is uncertain (an unrepaired font map)
    boxes: list[list[list[float] | None]] | None = None  # each cell's box, parallel to ``rows``; None: no positions


def _width(t: TableResult) -> int:
    if t.headers:
        return len(t.headers)
    return len(t.rows[0].cells) if t.rows else 0


def assemble_tables(raws: list[RawTable]) -> list[TableResult]:
    tables: list[TableResult] = []
    for raw in raws:
        rows, boxes = _rows_and_boxes(raw)
        if not rows:
            continue
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
        boxes = [b + [None] * (ncols - len(b)) if b is not None else None for b in boxes]
        has_header = is_header_row(rows[0])
        prev = tables[-1] if tables else None
        can_continue = bool(
            prev
            and raw.page is not None
            and prev.page_end is not None
            and raw.first_on_page
            and raw.page == prev.page_end + 1
            and _width(prev) == ncols
        )
        if has_header:
            headers, body = rows[0], rows[1:]
            if can_continue and prev.headers == headers:
                # Repeated header on the continuation page (D9): drop it, keep appending rows.
                _append(prev, body, boxes[1:], raw)
                continue
            _new(tables, headers, body, boxes[1:], raw, header_boxes=boxes[0])
        elif can_continue and prev.headers:
            # Continuation without a repeated header (D3): same columns, keeps each row's page.
            _append(prev, rows, boxes, raw)
        else:
            _new(tables, [], rows, boxes, raw)
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
         raw: RawTable, header_boxes: list | None = None) -> None:
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
# as stored. A served table keeps, for each column, the stored column it came from (``columns``): the stored index is what a cell's
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


def _texts(st: dict) -> list[str]:
    return [c or "" for c in [*(st.get("headers") or []), *(c for r in st.get("rows") or [] for c in r.get("cells") or [])]]


def stored_reversed(st: dict | None) -> bool:
    """Whether a stored table is a left-to-right table stored in reversed cell order (KTD6): its stored cell boxes descend, no
    cell or header holds a Hebrew letter, and the left-to-right cue holds on the table read in the order it is drawn (the stored
    order reversed). Read from the stored structure alone; the page's own text is not at hand, so a table of a page with Hebrew
    and none in its cells is judged by its boxes and its cue alone."""
    if not st or st.get("columns") is not None:  # a structure already served
        return False
    texts = _texts(st)
    if any(has_hebrew(c) for c in texts):
        return False
    rows = st.get("rows") or []
    if _boxes_descend([st.get("header_boxes"), *(r.get("cell_boxes") for r in rows)]) is not True:
        return False
    headers = st.get("headers") or []
    grid = ([headers[::-1]] if any(headers) else []) + [list(r.get("cells") or [])[::-1] for r in rows]
    return reads_left_to_right(grid, False)


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


def serve_cells(order: list[int], cells: list[str], fixed: bool = True) -> list[str]:
    """A stored row's cells at the served positions (``order``); a stored cell the row lacks is empty."""
    return [fix_sign(c) if fixed else c for c in ((cells[i] if i < len(cells) else "") for i in order)]


def serve_structure(st: dict | None) -> dict | None:
    """A stored table structure in the order it is drawn, when the stored data show it was stored reversed (``stored_reversed``);
    ``st`` itself otherwise. The served structure holds the headers, units, cells and cell boxes at the served positions and
    ``columns``: the stored column index at each position (a value's box and its datum are looked up by it). Nothing stored is
    changed: a new structure is built."""
    order = column_order(st)
    if order is None:
        return st
    headers = st.get("headers") or []
    units = st.get("units") or []
    out = dict(st)
    out["columns"] = order
    out["headers"] = serve_cells(order, headers, fixed=False) if any(headers) else []
    out["units"] = [units[i] if i < len(units) else None for i in order] if units else []
    if st.get("header_boxes"):
        out["header_boxes"] = [st["header_boxes"][i] if i < len(st["header_boxes"]) else None for i in order]
    out["rows"] = [
        {**r, "cells": serve_cells(order, list(r.get("cells") or [])),
         **({"cell_boxes": [r["cell_boxes"][i] if i < len(r["cell_boxes"]) else None for i in order]} if r.get("cell_boxes") else {})}
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


def _periods_rise(cells: list[str]) -> bool:
    periods = [p for p in map(_period, cells) if p is not None]
    return len(periods) >= 2 and all(a < b for a, b in zip(periods, periods[1:], strict=False))


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


def _render_row(headers: list[str], cells: list[str], units: list[str | None] | None = None) -> str:
    from app.extraction.chunking import render_row

    return render_row(headers, cells, units)


def render_served_row(sst: dict, index: int) -> str:
    """One row of a served table as a row chunk renders it (``render_row``): each cell paired with its column label, in the order
    the table is drawn. The row that holds the labels is its cells in order; a table with no label gives its cells in order."""
    row = sst["rows"][index]
    cells = list(row.get("cells") or [])
    labels, label_row = served_labels(sst)
    if not labels or index == label_row:
        return " | ".join(cells)
    return _render_row(labels, cells, sst.get("units") or None if any(sst.get("headers") or []) else None)


def render_served_cells(sst: dict, cells: list[str]) -> str:
    """The same for a row that is not one of the structure's own (a window the caller cut): its stored cells are put in served
    order first."""
    order = sst.get("columns") or []
    served = serve_cells(order, cells)
    labels, _ = served_labels(sst)
    if not labels or served == labels:
        return " | ".join(served)
    return _render_row(labels, served, sst.get("units") or None if any(sst.get("headers") or []) else None)


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
    for i, r in enumerate(st.get("rows") or []):
        line = " | ".join(r.get("cells") or [])
        if line.strip(" |"):
            mapping.setdefault(line, render_served_row(sst, i))
    return "\n".join(mapping.get(ln, ln) for ln in text.split("\n"))


def serve_row_text(st: dict | None, row_index: int | None, text: str) -> str:
    """The text of a ``table_row`` chunk (its table's title, then the row as ``render_row`` wrote it) with the row paired with its
    column labels in the order the table is drawn, when the table is served reversed; ``text`` as it is otherwise, and when the
    chunk is not that row as stored. The stored chunk is not touched: this is what a hit shows."""
    sst = serve_structure(st)
    rows = (st or {}).get("rows") or []
    if sst is st or row_index is None or not 0 <= row_index < len(rows):
        return text
    stored = _render_row(st.get("headers") or [], list(rows[row_index].get("cells") or []), st.get("units") or None)
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
