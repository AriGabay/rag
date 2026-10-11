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

A file holding several appraisals (round 7 U6, KTD7: ``app.chat.contexts``) has the same numbered chapters once per
appraisal, and each block keeps its own section path — the extraction does not merge them; what merged them was
reading a section by its path across the whole file. A section is therefore read within a block window (``window``):
the outline lists each context's sections apart, with the window each covers, and a context that starts inside
another's last chapter has its own title part (the blocks before its first heading). A table the extraction merged
across two appraisals (continued on the next page under the same headers) is listed with the rows of each context,
by each row's position.
"""

from __future__ import annotations

from collections.abc import Collection, Container
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

SECTION_CHARS = 3500  # a part of a section or a page range (KTD12)
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


def _from(from_block: int | None) -> str:
    return " AND block_index >= :from" if from_block is not None else ""


def blocks_on_pages(conn: Connection, version_id: UUID, first: int, last: int, from_block: int | None = None) -> list:
    """The blocks of a page range, in reading order (from block ``from_block`` when given)."""
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND page BETWEEN :a AND :b"
        f"{_from(from_block)} ORDER BY block_index"), {"v": version_id, "a": first, "b": last, "from": from_block}).all()


def blocks_in_section(conn: Connection, version_id: UUID, path: tuple[str, ...], from_block: int | None = None,
                      window: tuple[int, int] | None = None) -> list:
    """The blocks of a section, its sub-sections included: every block whose section path starts with ``path``
    (from block ``from_block`` when given). The empty path is the part before the first heading. ``window``: only
    the blocks of that range (a section of one appraisal context); with the empty path, every block of it (the
    context's title part, whatever path the extraction gave it)."""
    if window is not None:
        lo, hi = window
        cond = "" if not path else f" AND section_path[1:{len(path)}] = CAST(:p AS text[])"
        return conn.execute(text(
            f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :lo AND :hi"
            f"{cond}{_from(from_block)} ORDER BY block_index"),
            {"v": version_id, "p": list(path), "lo": lo, "hi": hi, "from": from_block}).all()
    if not path:
        return conn.execute(text(
            f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v AND cardinality(section_path) = 0"
            f"{_from(from_block)} ORDER BY block_index"), {"v": version_id, "from": from_block}).all()
    return conn.execute(text(
        f"SELECT {BLOCK_COLS} FROM document_blocks WHERE version_id = :v"
        f" AND section_path[1:{len(path)}] = CAST(:p AS text[]){_from(from_block)} ORDER BY block_index"),
        {"v": version_id, "p": list(path), "from": from_block}).all()


def section_blocks(conn: Connection, version_id: UUID) -> list[tuple[int, tuple[str, ...] | None, str | None]]:
    """Every block's (index, section path, status), in reading order, without its text: ``in_section`` picks a
    section's blocks from them."""
    return [(r.block_index, tuple(r.section_path) if r.section_path is not None else None, r.status)
            for r in conn.execute(text("SELECT block_index, section_path, status FROM document_blocks"
                                       " WHERE version_id = :v ORDER BY block_index"), {"v": version_id})]


def in_section(blocks: list[tuple], path: tuple[str, ...], window: tuple[int, int] | None = None) -> list[tuple]:
    """The blocks of ``section_blocks`` that ``blocks_in_section`` (without ``from_block``) returns for ``path`` and
    ``window``."""
    n = len(path)
    if window is not None:
        lo, hi = window
        return [b for b in blocks if lo <= b[0] <= hi and (not path or (b[1] is not None and b[1][:n] == path))]
    if not path:
        return [b for b in blocks if b[1] is not None and not b[1]]
    return [b for b in blocks if b[1] is not None and b[1][:n] == path]


def blocks_at(conn: Connection, version_id: UUID, indexes: Collection[int]) -> dict[int, object]:
    """The blocks of ``indexes`` that exist, by index: their page, their stored box and their page's stored geometry
    (so ``contexts.top_of`` places them on the rendered page, the frame of a table row's top: ``row_top``)."""
    return {r.block_index: r for r in conn.execute(text(
        "SELECT b.block_index, b.page, b.bbox, p.mediabox, p.cropbox, p.rotation, p.display_width, p.display_height,"
        " p.geometry_issue FROM document_blocks b LEFT JOIN pages p ON p.version_id = b.version_id"
        " AND p.page_no = b.page WHERE b.version_id = :v AND b.block_index = ANY(:i)"),
        {"v": version_id, "i": list(indexes)})}


def table_first_blocks(conn: Connection, version_id: UUID) -> dict[int, int]:
    """Each table's first block (the block ``table_block`` reads it at), by table index; a table stored without a
    block is not among them."""
    return {r.table_index: r.block_index for r in conn.execute(text(
        "SELECT table_index, min(block_index) AS block_index FROM document_blocks WHERE version_id = :v"
        " AND table_index IS NOT NULL GROUP BY table_index"), {"v": version_id})}


def section_path_of(conn: Connection, version_id: UUID, block_index: int) -> tuple[str, ...]:
    row = conn.execute(text("SELECT section_path FROM document_blocks WHERE version_id = :v AND block_index = :b"),
                       {"v": version_id, "b": block_index}).first()
    return tuple(row.section_path or ()) if row else ()


def table_structure(conn: Connection, version_id: UUID, table_index: int) -> dict | None:
    row = conn.execute(text("SELECT structure FROM extracted_tables WHERE version_id = :v AND table_index = :t"),
                       {"v": version_id, "t": table_index}).first()
    return (row.structure or {}) if row else None


def tables_of(conn: Connection, version_id: UUID, indexes: Collection[int] | None = None) -> dict[int, dict]:
    """A version's tables by index (only those of ``indexes`` when given)."""
    return {r.table_index: (r.structure or {}) for r in conn.execute(text(
        "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v"
        + (" AND table_index = ANY(:t)" if indexes is not None else "") + " ORDER BY table_index"),
        {"v": version_id, "t": list(indexes) if indexes is not None else None})}


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
    context: int | None = None  # its appraisal context, in a file holding several (KTD7)
    window: tuple[int, int] | None = None  # the blocks it is read within (``blocks_in_section``)


def effective_path(cx, block_index: int, stored) -> tuple[str, ...]:
    """A block's section path within its appraisal context: a block of a later context's title part (before its
    first heading) belongs to no chapter of the context before it."""
    seg = cx.segment_at(block_index) if cx is not None and cx.multi else None
    if seg is not None and seg is not cx.segments[0] and (seg.lead is None or block_index < seg.lead):
        return ()
    return tuple(stored or ())


def section_window(cx, block_index: int, path: tuple[str, ...]) -> tuple[int, int] | None:
    """The block window a section is read within, for the context the block is in (None: a file with one context).
    The title part of a context is its blocks before its first heading; its chapters are read from that heading on."""
    seg = cx.segment_at(block_index) if cx is not None and cx.multi else None
    if seg is None:
        return None
    if not path:
        return (seg.first, seg.lead - 1 if seg.lead is not None and seg.lead > seg.first else seg.last)
    return (max(seg.first, seg.lead if seg.lead is not None else seg.first), seg.last)


def row_top(row: dict) -> float | None:
    """A table row's top on the rendered page (its first cell's box: cell boxes are stored in the display frame);
    None for a row stored without cell boxes."""
    try:
        return float((row.get("cell_boxes") or [])[0][1])
    except (TypeError, ValueError, IndexError):
        return None


def table_contexts(cx, st: dict, block_index: int | None, block_page: int | None) -> list[dict]:
    """The rows of a table by appraisal context, in order: ``[{"context", "first", "last", "pages"}]`` with rows
    counted from 1; a row is in the context of its own position (its page and top), a row with none in the table's
    block's. Empty in a file with one context."""
    if cx is None or not cx.multi:
        return []
    own = cx.at_block(block_index) if block_index is not None else None
    out: list[dict] = []
    for n, row in enumerate(st.get("rows") or [], 1):
        page = row.get("page") or block_page
        number = cx.at_position(page, row_top(row)) if row.get("page") else own
        number = number or own or 1
        if out and out[-1]["context"] == number:
            out[-1]["last"] = n
            if page:
                out[-1]["pages"].add(page)
        else:
            out.append({"context": number, "first": n, "last": n, "pages": {page} if page else set()})
    return out


def outline(conn: Connection, version_id: UUID, cx=None) -> tuple[list[Section], list[dict]]:
    """Every section (each prefix of a block's section path, sub-sections included, in reading order) with its
    size and its unread and uncertain regions, and every table with its caption, rows, page and section. ``cx``: the
    version's appraisal contexts (``app.chat.contexts``); in a file holding several, each context's sections are
    its own (the same path in two contexts is two sections), and each table lists its rows by context."""
    multi = cx is not None and cx.multi
    position = {id(s): n for n, s in enumerate(cx.segments)} if multi else {}  # a segment's place in ``segments``
    sections: dict[tuple, Section] = {}
    table_at: dict[int, tuple] = {}
    for r in conn.execute(text(
            "SELECT block_index, section_path, page, status, table_index, length(text) AS chars FROM document_blocks"
            " WHERE version_id = :v ORDER BY block_index"), {"v": version_id}):
        path = effective_path(cx, r.block_index, r.section_path) if multi else tuple(r.section_path or ())
        at = cx.segment_at(r.block_index) if multi else None
        seg = position[id(at)] if at is not None else None
        for p in [path[:i] for i in range(1, len(path) + 1)] or [()]:
            key = (seg, p) if multi else p
            s = sections.get(key)
            if s is None:
                s = sections[key] = Section(p, r.block_index, r.block_index)
                if multi and seg is not None:
                    s.context = cx.segments[seg].number
                    s.window = section_window(cx, r.block_index, p)
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
                       "section": path[-1] if path else st.get("section"), "path": path,
                       "contexts": table_contexts(cx, st, block, page)})
    return list(sections.values()), tables


# --- parts -----------------------------------------------------------------------------------------------------

Position = tuple[int, int]  # (block index, character offset in that block)


@dataclass
class Part:
    """A part of a window: ``items`` are (block, the text of it in this part, whether that is its whole text);
    ``next`` the position the following part starts at, None when the window ends here."""

    items: list
    next: Position | None


def take(rows: list, pos: Position | None = None, chars: int = SECTION_CHARS, sent: Container[int] | None = None
         ) -> Part:
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
