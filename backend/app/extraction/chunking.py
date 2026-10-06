"""Section-aware chunking (R5).

Text is split at numbered section headings (``3. עסקאות השוואה``); inside a section, lines are packed
into chunks up to ``MAX_CHARS`` (short pieces merge). A chunk that crosses a page break stores the exact
list of physical pages it covers. Page footers (``... | עמוד N``) are left out of chunk text. Each table
row also becomes a ``table_row`` chunk rendered as ``header (unit): value`` pairs, with the row's own page
and its table and row index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.extraction.base import ChunkResult, TableResult

MAX_CHARS = 1200
_HEADING = re.compile(r"^\s*(\d{1,2})\.\s+\S")
_HEB = re.compile(r"[א-ת]")
_FOOTER = re.compile(r"(?:^|\|)\s*עמוד\s+\d+(?:\s+מתוך\s+\d+)?\s*$")
_PAGE_NO_ONLY = re.compile(r"^[\s\-–—]*\d{1,4}[\s\-–—]*$")


def is_heading(line: str) -> bool:
    s = line.strip()
    if not _HEADING.match(s) or not _HEB.search(s) or len(s) > 80:
        return False
    if s.endswith((".", ":", ",")) or len(s.split()) > 9:
        return False
    return True


def is_footer(line: str) -> bool:
    s = line.strip()
    return bool(s) and ((len(s) <= 80 and bool(_FOOTER.search(s))) or bool(_PAGE_NO_ONLY.match(s)))


@dataclass
class _Section:
    title: str | None
    lines: list[tuple[int | None, str]]


def _sections(pages: list[tuple[int | None, str]]) -> list[_Section]:
    sections = [_Section(None, [])]
    for page_no, text in pages:
        for line in text.splitlines():
            if not line.strip() or is_footer(line):
                continue
            if is_heading(line):
                sections.append(_Section(line.strip(), []))
            sections[-1].lines.append((page_no, line.strip()))
    return [s for s in sections if s.lines]


def _page_list(items: list[tuple[int | None, str]]) -> list[int] | None:
    pages = sorted({p for p, _ in items if p is not None})
    return pages or None


def chunk_document(pages: list[tuple[int | None, str]], tables: list[TableResult],
                   max_chars: int = MAX_CHARS) -> list[ChunkResult]:
    chunks: list[ChunkResult] = []

    def emit(kind: str, page_list: list[int] | None, section: str | None, text: str,
             table_index: int | None = None, row_index: int | None = None) -> None:
        chunks.append(ChunkResult(index=len(chunks), kind=kind, page_list=page_list, section=section, text=text,
                                  table_index=table_index, row_index=row_index))

    for sec in _sections(pages):
        current: list[tuple[int | None, str]] = []
        size = 0
        for item in sec.lines:
            line_len = len(item[1]) + 1
            if current and size + line_len > max_chars:
                emit("text", _page_list(current), sec.title, "\n".join(t for _, t in current))
                # Continuation chunks repeat the heading so they stay findable by section.
                current = [(item[0], sec.title)] if sec.title else []
                size = len(sec.title) + 1 if sec.title else 0
            current.append(item)
            size += line_len
        if current:
            emit("text", _page_list(current), sec.title, "\n".join(t for _, t in current))

    for table in tables:
        for row_index, row in table_rows(table.rows, lambda r: r.cells):
            emit("table_row", [row.page] if row.page is not None else None, table.section,
                 render_row(table.headers, row.cells, table.units), table.index, row_index)
    return chunks


def table_rows(rows: list, cells_of) -> list[tuple[int, object]]:
    """Rows that become chunks, with their index in the table (all-empty rows are skipped)."""
    return [(i, r) for i, r in enumerate(rows) if any((c or "").strip() for c in cells_of(r))]


def render_row(headers: list[str], cells: list[str], units: list[str | None] | None = None) -> str:
    """Searchable rendering of one table row: ``header (unit): value`` pairs (empty cells skipped).
    A unit already written in the header is not repeated."""
    if not headers:
        return " | ".join(c for c in cells if c)
    parts = []
    for i, cell in enumerate(cells):
        if not cell:
            continue
        label = headers[i] if i < len(headers) and headers[i] else f"עמודה {i + 1}"
        unit = units[i] if units and i < len(units) else None
        if unit and f"({unit})" not in label:
            label = f"{label} ({unit})"
        parts.append(f"{label}: {cell}")
    return " | ".join(parts)
