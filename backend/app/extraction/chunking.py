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


# --- block-based chunking (DOCX) ---------------------------------------------------------------------------

def _row_context(table: TableResult) -> str:
    """What a row belongs to, prefixed to each row chunk: the table's title or the sentence introducing it."""
    for candidate in [*table.title, table.caption or ""]:
        candidate = " ".join(candidate.split()).rstrip(":：- –")
        if candidate:
            return candidate[:160]
    return ""


def _table_chunks(table: TableResult, section: str | None, max_chars: int) -> list[tuple[str, str, int | None]]:
    """(kind, text, row index) chunks of one table: the whole table (split into row groups that repeat the
    caption, title and header when it is long), then one chunk per row with its context and header pairs."""
    intro = [x for x in [table.caption, *table.title] if x]
    header = " | ".join(table.headers) if any(table.headers) else ""
    head = "\n".join([*intro, header] if header else intro)
    out: list[tuple[str, str, int | None]] = []
    body: list[str] = []
    size = len(head)
    for r in table.rows:
        line = " | ".join(r.cells)
        if body and size + len(line) + 1 > max_chars:
            out.append(("table", "\n".join([head, *body]).strip(), None))
            body, size = [], len(head)
        body.append(line)
        size += len(line) + 1
    tail = "\n".join(table.notes)
    out.append(("table", "\n".join(x for x in [head, *body, tail] if x).strip(), None))
    context = _row_context(table)
    for row_index, row in table_rows(table.rows, lambda r: r.cells):
        rendered = render_row(table.headers, row.cells, table.units)
        out.append(("table_row", f"{context}: {rendered}" if context else rendered, row_index))
    return out


def chunk_blocks(blocks: list, tables: list[TableResult], max_chars: int = MAX_CHARS) -> list[ChunkResult]:
    """Chunks over DOCX blocks: paragraphs packed per section (a continuation repeats the section heading),
    every table (Word's own or read from a picture) as table chunks plus one chunk per row, and a picture's
    own text as an ``image`` chunk with the sentence that introduces it. Each chunk records its block range."""
    chunks: list[ChunkResult] = []
    tables_of: dict[int, list[TableResult]] = {}
    for t in tables:
        if t.block_index is not None:
            tables_of.setdefault(t.block_index, []).append(t)

    def emit(kind: str, section: str | None, text: str, start: int, end: int, table_index: int | None = None,
             row_index: int | None = None) -> None:
        if text.strip():
            chunks.append(ChunkResult(index=len(chunks), kind=kind, page_list=None, section=section, text=text,
                                      table_index=table_index, row_index=row_index, block_start=start,
                                      block_end=end))

    current: list = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        # a heading alone is no passage: the table or picture after it carries it as its section and caption
        if current and any(b.kind != "heading" for b in current):
            emit("text", current[0].section, "\n".join(b.text for b in current), current[0].index, current[-1].index)
        current, size = [], 0

    for b in blocks:
        if b.kind in ("heading", "paragraph", "textbox"):
            if b.kind == "heading" or (current and current[0].section != b.section):
                flush()
            if current and size + len(b.text) + 1 > max_chars:
                heading = b.section
                flush()
                if heading:
                    current = [type(b)(index=b.index, kind="heading", text=heading, section=b.section)]
                    size = len(heading) + 1
            current.append(b)
            size += len(b.text) + 1
            continue
        flush()
        for t in tables_of.get(b.index, []):
            for kind, text, row_index in _table_chunks(t, b.section, max_chars):
                emit(kind, b.section, text, b.index, b.index, t.index, row_index)
        if b.kind == "image" and b.picture_text.strip():
            intro = next((p.text for p in reversed(blocks[:b.index]) if p.kind in ("paragraph", "heading")
                          and p.section == b.section), "")[:300]
            emit("image", b.section, "\n".join(x for x in [intro, b.picture_text] if x), b.index, b.index)
    flush()
    return chunks
