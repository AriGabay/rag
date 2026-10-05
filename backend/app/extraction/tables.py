"""Table assembly (R4): logical column order, header detection, cross-page continuation.

Input tables come from pdfplumber (cell grid, columns left -> right) or from OCR word boxes; both are
converted to logical (right-to-left) column order before assembly, so index 0 is the rightmost column
(``כתובת`` in the appraisal comparables table). Every row keeps its own physical page.
"""

from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass

from app.extraction.base import TableResult, TableRow
from app.extraction.hebrew import fix_line

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


def logical_row(cells_ltr: list[str | None], visual_default: bool = True) -> list[str]:
    """pdfplumber cells (left -> right, visual text) -> logical column order with logical cell text.
    Wrapped cell lines are fixed one by one and joined with a space."""
    out = []
    for cell in reversed(cells_ltr):
        lines = [ln for ln in (cell or "").splitlines() if ln.strip()]
        out.append(clean_cell(" ".join(fix_line(ln, visual_default) for ln in lines)))
    return out


@dataclass
class RawTable:
    """One table found on one page, rows already in logical column order."""

    page: int | None
    rows: list[list[str]]
    ocr: bool = False
    section: str | None = None
    first_on_page: bool = True
    top: float = 0.0


def _width(t: TableResult) -> int:
    if t.headers:
        return len(t.headers)
    return len(t.rows[0].cells) if t.rows else 0


def assemble_tables(raws: list[RawTable]) -> list[TableResult]:
    tables: list[TableResult] = []
    for raw in raws:
        rows = [[clean_cell(c) for c in r] for r in raw.rows if any(clean_cell(c) for c in r)]
        if not rows:
            continue
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]
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
                _append(prev, body, raw)
                continue
            _new(tables, headers, body, raw)
        elif can_continue and prev.headers:
            # Continuation without a repeated header (D3): same columns, keeps each row's page.
            _append(prev, rows, raw)
        else:
            _new(tables, [], rows, raw)
    return tables


def _new(tables: list[TableResult], headers: list[str], body: list[list[str]], raw: RawTable) -> None:
    tables.append(
        TableResult(
            index=len(tables),
            headers=headers,
            units=units_for(headers),
            rows=[TableRow(page=raw.page, cells=r) for r in body],
            page_start=raw.page,
            page_end=raw.page,
            ocr=raw.ocr,
            section=raw.section,
        )
    )


def _append(table: TableResult, body: list[list[str]], raw: RawTable) -> None:
    table.rows.extend(TableRow(page=raw.page, cells=r) for r in body)
    table.page_end = raw.page
    table.ocr = table.ocr or raw.ocr
