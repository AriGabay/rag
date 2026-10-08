"""One reader over a version's stored reading, for the source panel and the model alike (KTD8).

Every function takes a connection already inside the caller's ``tenant_tx``: row-level security decides what is
visible, and a version is resolved only through a document that is not deleted (``version``,
``current_version``), so nothing below it is read for a document the user may not see. The source panel's
``/blocks`` endpoint and the agent's ``read`` and ``outline`` tools read the same blocks through these functions,
so a citation opens exactly what the model read.

A version's reading is its ``document_blocks`` in reading order (each with page, region, section path, status),
its ``extracted_tables`` (headers, rows, caption, notes) and its ``pages``. Windows are cut by block index, page
range, section path (a section includes its ``N.M`` sub-sections) or table; ``take`` cuts a window into parts
of a bounded size with the exact position where the next part starts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

SECTION_CHARS = 3500  # a part of a section or a page range (KTD12)
TABLE_ROWS = 40  # rows in a part of a table
WINDOW_MAX = 400  # blocks in one window of the source panel
MARKER_CHARS = 80  # what a region marker costs in a part, whatever its text
UNREAD = "unread"
UNCERTAIN = "read_uncertain"

BLOCK_COLS = ("block_index, kind, section, section_path, label, paragraph_no, page, media, source, status, note,"
              " table_index, text, bbox, method, reader_version, content_hash, original_text")


@dataclass(frozen=True)
class Version:
    document_id: UUID
    version_id: UUID
    title: str
    reading_id: str | None
    mime_type: str
    is_current: bool
    page_count: int | None
    partial: bool  # some region or page of it was not read
    storage_key: str
    filename: str

    @property
    def is_pdf(self) -> bool:
        return self.mime_type == "application/pdf"


_VERSION_SQL = (
    "SELECT v.id, v.document_id, d.title, v.ingestion->>'reading_id' AS reading_id, v.mime_type, v.is_current,"
    " v.page_count, v.storage_key, v.filename, (COALESCE(v.pages_incomplete, 0) > 0"
    " OR COALESCE((v.ingestion->>'partial')::boolean, false)) AS partial"
    " FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL")


def _version(row) -> Version | None:
    if row is None:
        return None
    return Version(row.document_id, row.id, row.title, row.reading_id, row.mime_type, row.is_current, row.page_count,
                   bool(row.partial), row.storage_key, row.filename)


def version(conn: Connection, version_id: UUID, document_id: UUID | None = None) -> Version | None:
    """A version the user may see (of ``document_id`` when given), or None: missing, deleted or not permitted are
    the same answer."""
    sql = _VERSION_SQL + " WHERE v.id = :v" + (" AND v.document_id = :d" if document_id is not None else "")
    return _version(conn.execute(text(sql), {"v": version_id, "d": document_id}).first())


def current_version(conn: Connection, document_id: UUID) -> Version | None:
    """The current version of a document the user may see, or None."""
    return _version(conn.execute(text(_VERSION_SQL + " WHERE v.document_id = :d AND v.is_current"),
                                 {"d": document_id}).first())


# --- windows ---------------------------------------------------------------------------------------------------

def blocks_between(conn: Connection, version_id: UUID, start: int | None = None, end: int | None = None,
                   limit: int | None = WINDOW_MAX) -> list:
    """Blocks by index, in reading order (``start``/``end`` inclusive, open when None)."""
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :b"
        " ORDER BY block_index" + (f" LIMIT {int(limit)}" if limit else "")),
        {"v": version_id, "a": start if start is not None else 0,
         "b": end if end is not None else 2_000_000_000}).all()


def block_count(conn: Connection, version_id: UUID) -> int:
    return conn.execute(text("SELECT count(*) FROM document_blocks WHERE version_id = :v"),
                        {"v": version_id}).scalar_one()


def blocks_on_pages(conn: Connection, version_id: UUID, first: int, last: int) -> list:
    """The blocks of a page range, in reading order."""
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND page BETWEEN :a AND :b"
        " ORDER BY block_index"), {"v": version_id, "a": first, "b": last}).all()


def blocks_in_section(conn: Connection, version_id: UUID, path: tuple[str, ...]) -> list:
    """The blocks of a section, its sub-sections included: every block whose section path starts with ``path``.
    The empty path is the part before the first heading."""
    if not path:
        return conn.execute(text(
            f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND cardinality(section_path) = 0"
            " ORDER BY block_index"), {"v": version_id}).all()
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v"
        f" AND section_path[1:{len(path)}] = CAST(:p AS text[]) ORDER BY block_index"),
        {"v": version_id, "p": list(path)}).all()


def section_path_of(conn: Connection, version_id: UUID, block_index: int) -> tuple[str, ...]:
    row = conn.execute(text("SELECT section_path FROM document_blocks WHERE version_id = :v AND block_index = :b"),
                       {"v": version_id, "b": block_index}).first()
    return tuple(row.section_path or ()) if row else ()


def table_structure(conn: Connection, version_id: UUID, table_index: int) -> dict | None:
    row = conn.execute(text("SELECT structure FROM extracted_tables WHERE version_id = :v AND table_index = :t"),
                       {"v": version_id, "t": table_index}).first()
    return (row.structure or {}) if row else None


def tables_of(conn: Connection, version_id: UUID) -> dict[int, dict]:
    return {r.table_index: (r.structure or {}) for r in conn.execute(text(
        "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v ORDER BY table_index"),
        {"v": version_id})}


def table_block(conn: Connection, version_id: UUID, table_index: int):
    """The block a table is read at (its page, region and status), or None for a table stored without one."""
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND table_index = :t"
        " ORDER BY block_index LIMIT 1"), {"v": version_id, "t": table_index}).first()


def unread_pages(conn: Connection, version_id: UUID, first: int, last: int) -> list[int]:
    """Pages of a range whose reading failed (``pages.ok`` false)."""
    return [r.page_no for r in conn.execute(text(
        "SELECT page_no FROM pages WHERE version_id = :v AND page_no BETWEEN :a AND :b AND NOT ok ORDER BY page_no"),
        {"v": version_id, "a": first, "b": last})]


# --- outline ---------------------------------------------------------------------------------------------------

@dataclass
class Section:
    path: tuple[str, ...]
    first: int
    last: int
    chars: int = 0
    blocks: int = 0
    unread: int = 0
    uncertain: int = 0
    pages: set = field(default_factory=set)


def outline(conn: Connection, version_id: UUID) -> tuple[list[Section], list[dict]]:
    """Every section (each prefix of a block's section path, sub-sections included, in reading order) with its
    size and its unread and uncertain regions, and every table with its caption, rows, page and section."""
    sections: dict[tuple, Section] = {}
    table_at: dict[int, tuple] = {}
    for r in conn.execute(text(
            "SELECT block_index, section_path, page, status, table_index, length(text) AS chars FROM document_blocks"
            " WHERE version_id = :v ORDER BY block_index"), {"v": version_id}):
        path = tuple(r.section_path or ())
        for p in [path[:i] for i in range(1, len(path) + 1)] or [()]:
            s = sections.setdefault(p, Section(p, r.block_index, r.block_index))
            s.last = r.block_index
            s.chars += r.chars or 0
            s.blocks += 1
            s.unread += r.status == UNREAD
            s.uncertain += r.status == UNCERTAIN
            if r.page:
                s.pages.add(r.page)
        if r.table_index is not None and r.table_index not in table_at:
            table_at[r.table_index] = (r.block_index, r.page, path)
    tables = []
    for index, st in tables_of(conn, version_id).items():
        block, page, path = table_at.get(index, (st.get("block_index"), st.get("page"), ()))
        tables.append({"table_index": index, "caption": st.get("caption") or next(iter(st.get("title") or []), None),
                       "rows": len(st.get("rows") or []), "block": block, "page": page,
                       "section": path[-1] if path else st.get("section"), "path": path})
    return list(sections.values()), tables


# --- parts -----------------------------------------------------------------------------------------------------

Position = tuple[int, int]  # (block index, character offset in that block)


@dataclass
class Part:
    """A part of a window: ``items`` are (block, the text of it in this part, whether that is its whole text);
    ``next`` the position the following part starts at, None when the window ends here."""

    items: list
    next: Position | None


def take(rows: list, pos: Position | None = None, chars: int = SECTION_CHARS, sent: set[int] | None = None) -> Part:
    """From ``pos`` (None: the window's beginning) as many whole blocks as fit in ``chars``; a single block longer
    than that is cut at a word boundary and continued from there. Never an empty part while blocks remain. A block
    in ``sent`` (already returned whole to the reader) costs nothing: it is sent as a pointer."""
    sent = sent or set()
    items: list = []
    used = 0
    for r in rows:
        if pos is not None and r.block_index < pos[0]:
            continue
        offset = pos[1] if pos is not None and r.block_index == pos[0] else 0
        piece = (r.text or "")[offset:]
        size = (0 if offset == 0 and r.block_index in sent else len(piece)) + (
            MARKER_CHARS if r.status in (UNREAD, UNCERTAIN) else 0)
        if items and used + size > chars:
            return Part(items, (r.block_index, offset))
        if not items and size > chars and len(piece) > chars:
            cut = piece.rfind(" ", 0, chars)
            cut = cut if cut > chars // 2 else chars
            items.append((r, piece[:cut], False))
            return Part(items, (r.block_index, offset + cut))
        items.append((r, piece, offset == 0))
        used += size
    return Part(items, None)
