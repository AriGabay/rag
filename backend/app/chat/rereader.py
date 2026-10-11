"""The database fetcher of the bounded re-read (round 8 U5, KTD5; R13-R15, R24): the context a claim lacked — the heading
chain, the paragraphs around the cited blocks, the rest of the section, a table's headers, units and notes, the notes of
the page, a table continued on the next page, the stored reading of a picture's region — read from the stored document
through the reader, under the user's own permissions, for the verification only. ``ContextReader`` is the
``verify.ContextFetcher`` of a turn; the per-turn state of the re-read (``reread_done``, ``reread_log``) stays on the
``Workspace``. It reads stored text only: no model call is made."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import Connection, text

from app.chat import reader
from app.chat.tools import (
    _TABLE_SOURCES,
    CONTEXT_CHARS,
    NEIGHBORS,
    Source,
    ToolError,
    Workspace,
    _clip,
    _table_text,
    contexts_of,
)
from app.chat.verify import Expansion
from app.db import tenant_tx

# --- the bounded re-read of a claim's source (round 8 U5, KTD5) -----------------------------------------------------

REREAD_CHARS = 6000  # the text one expansion sends the judge
_NOTE_LINE = re.compile(r"^\s*(?:\(\s*\d{1,2}\s*\)|\d{1,2}\s*\)|\(?\*+\)?|\*+)")  # "(1)", "1)", "(*)", "*": a footnote's start


def _chain_of(ws: Workspace, conn: Connection, head: reader.Version, src: Source) -> tuple[str, ...]:
    """The heading chain of the block range a source cites, within its appraisal context; for a source with no place in
    the document (a search hit's chunk), its section."""
    if src.block_start is None:
        return (src.section,) if src.section else ()
    cx = contexts_of(ws, conn, src.version_id, head.reading_id)
    return reader.effective_path(cx, src.block_start, reader.section_path_of(conn, src.version_id, src.block_start))


def _stored_table(conn: Connection, src: Source) -> tuple[int | None, dict | None]:
    """The stored table a source is of, or is in the block range of: its index and structure."""
    index = src.table_index
    if index is None and src.block_start is not None:
        index = next((r.table_index for r in reader.blocks_between(
            conn, src.version_id, src.block_start, src.block_end if src.block_end is not None else src.block_start)
            if r.table_index is not None), None)
    return index, (reader.table_structure(conn, src.version_id, index) if index is not None else None)


def _table_head(st: dict) -> list[str]:
    """What a table says about itself outside its rows: its caption and titles, the source of its reading, its headers,
    their units and its notes."""
    lines = [x for x in [st.get("caption"), *(st.get("title") or [])] if x]
    note = _TABLE_SOURCES.get(st.get("source") or "", "")
    if note:
        lines.append(note)
    if any(st.get("headers") or []):
        lines.append("כותרות העמודות: " + " | ".join(st["headers"]))
    units = [f"{h}: {u}" for h, u in zip(st.get("headers") or [], st.get("units") or [], strict=False) if u]
    if units:
        lines.append("יחידות העמודות: " + "; ".join(units))
    lines += [f"הערה: {n}" for n in st.get("notes") or []]
    return lines


def _expand_heading(ws, conn, head, src):
    chain = _chain_of(ws, conn, head, src)
    if not chain:
        return None
    return "כותרות מעל המקור, מהחיצונית לפנימית: " + " › ".join(chain), "כותרות הסעיף", {}


def _expand_neighbours(ws, conn, head, src):
    if src.block_start is None:
        return None
    end = src.block_end if src.block_end is not None else src.block_start
    rows = [r for r in reader.blocks_between(conn, src.version_id, max(0, src.block_start - NEIGHBORS), end + NEIGHBORS)
            if r.text]
    if not rows:
        return None
    return ("\n".join(r.text for r in rows), "הפסקאות הסמוכות",
            {"block_start": rows[0].block_index, "block_end": rows[-1].block_index})


def _expand_continuation(ws, conn, head, src):
    if src.block_start is None:
        return None
    end = src.block_end if src.block_end is not None else src.block_start
    cx = contexts_of(ws, conn, src.version_id, head.reading_id)
    top = _chain_of(ws, conn, head, src)[:1]
    rows = [r for r in reader.blocks_in_section(conn, src.version_id, top, from_block=end + 1,
                                                window=reader.section_window(cx, src.block_start, top)) if r.text]
    if not rows:
        return None
    return ("\n".join(r.text for r in rows), "המשך הסעיף",
            {"block_start": src.block_start, "block_end": rows[-1].block_index})


def _expand_table_headers(ws, conn, head, src):
    _, st = _stored_table(conn, src)
    lines = _table_head(st) if st else []
    if not lines:
        return None
    chain = _chain_of(ws, conn, head, src)
    if chain:
        lines.insert(0, "כותרות מעל הטבלה: " + " › ".join(chain))
    return "\n".join(lines), "כותרות, יחידות והערות הטבלה", {}


def _expand_units_note(ws, conn, head, src):
    got = _expand_table_headers(ws, conn, head, src)
    if got is not None:
        return got[0], "יחידות והערות", {}
    # a passage: the headings above it and the paragraphs around it say the scale it is written in
    chain = _expand_heading(ws, conn, head, src)
    near = _expand_neighbours(ws, conn, head, src)
    parts = [x[0] for x in (chain, near) if x]
    return ("\n".join(parts), "יחידות והקשר", near[2] if near else {}) if parts else None


def _expand_footnotes(ws, conn, head, src):
    lines: list[str] = []
    _, st = _stored_table(conn, src)
    if st:
        lines += [f"הערת טבלה: {n}" for n in st.get("notes") or []]
    pages = src.page_list or []
    if not pages and src.block_start is not None:
        b = reader.blocks_at(conn, src.version_id, [src.block_start]).get(src.block_start)
        pages = [b.page] if b is not None and b.page else []
    if pages:
        for r in reader.blocks_on_pages(conn, src.version_id, min(pages), max(pages)):
            lines += [x.strip() for x in (r.text or "").splitlines() if _NOTE_LINE.match(x)]
    lines = list(dict.fromkeys(lines))
    if not lines:
        return None
    return "הערות שוליים והערות של העמוד והטבלה:\n" + "\n".join(lines), "הערות שוליים", {}


def _expand_next_page(ws, conn, head, src):
    index, st = _stored_table(conn, src)
    if st:
        # the extraction stored a table that continues on the next page as one table (its repeated header dropped)
        rows = st.get("rows") or []
        pages = sorted({r.get("page") for r in rows if r.get("page")})
        if len(pages) < 2:  # a table on one page (or of a Word file) has no continuation to read
            return None
        marks: dict[int, str] = {}
        seen = None
        for n, r in enumerate(rows):
            if r.get("page") and r.get("page") != seen:
                seen = r["page"]
                marks[n] = f"[עמוד {seen}]"
        body = _clip(_table_text(st, rows, marks), CONTEXT_CHARS["table"])
        return (body, "הטבלה כולה, כולל המשכה בעמוד הבא",
                {"block_start": st.get("block_index"), "block_end": st.get("block_index"), "table_index": index})
    pages = src.page_list or []
    if not pages:
        return None
    nxt = max(pages) + 1
    rows = [r for r in reader.blocks_on_pages(conn, src.version_id, nxt, nxt) if r.text]
    if not rows:
        return None
    return ("\n".join(r.text for r in rows), f"תחילת עמוד {nxt}",
            {"block_start": rows[0].block_index, "block_end": rows[-1].block_index, "page_list": [nxt]})


def _expand_region(ws, conn, head, src):
    from app.extraction.images import PictureReading
    from app.extraction.vision import reading_text

    if src.vision:
        row = conn.execute(text(
            "SELECT reading FROM region_readings WHERE version_id = :v AND reading_id = :r AND region = :g"
            " ORDER BY created_at DESC LIMIT 1"),
            {"v": src.version_id, "r": head.reading_id or "", "g": src.vision["region"]}).first()
        stored = PictureReading.from_json(row.reading) if row is not None else None
        # a stored reading, else the turn's own: both are a model's transcription kept by the server, never a new call
        return reading_text(stored or src.vision["reading"]), "הקריאה החזותית השמורה של האזור", {}
    _, st = _stored_table(conn, src)
    if not st or (st.get("source") or "text") == "text":
        return None
    return "\n".join([*_table_head(st), *(" | ".join(r.get("cells") or []) for r in st.get("rows") or [])]), \
        "הקריאה השמורה של טבלת התמונה", {}


_EXPANDERS = {"heading": _expand_heading, "neighbours": _expand_neighbours, "continuation": _expand_continuation,
              "table_headers": _expand_table_headers, "units_note": _expand_units_note,
              "footnotes": _expand_footnotes, "next_page": _expand_next_page, "region": _expand_region}


@dataclass
class ContextReader:
    """The database ``verify.ContextFetcher``: the context a claim lacked, read from the stored document for the
    verification only. It reads through ``reader`` under the user's own ``tenant_tx`` (row-level security and a
    deleted document hide what the user may not see; a source whose document was revoked, deleted or read again
    meanwhile expands to nothing), and only stored text: blocks, tables and the readings of pictures — never a new
    model reading. ``expand`` keeps the expansion unregistered; ``adopt`` registers it as a turn source once the
    verification found it makes a claim supported (cited, with its anchor: the cited block range), like the meaning
    check's evidence."""

    ws: Workspace
    pending: dict = field(default_factory=dict)  # (source id, kind) -> Source, not registered
    adopted: dict = field(default_factory=dict)  # (source id, kind) -> the id it was registered under

    def expand(self, sid: str, kind: str):
        key = (sid, kind)
        if key not in self.pending:
            src = self.ws.sources.get(sid)
            expander = _EXPANDERS.get(kind)
            if src is None or expander is None or src.document_id is None or src.version_id is None:
                self.pending[key] = None
            else:
                self.pending[key] = self._read(src, expander, kind)
        found = self.pending[key]
        return Expansion(found.text, found.location) if found is not None else None

    def _read(self, src: Source, expander, kind: str) -> Source | None:
        try:
            with tenant_tx(self.ws.ctx) as conn:
                head = reader.version(conn, src.version_id)
                if head is None:  # missing, deleted or not permitted: the same answer
                    return None
                if src.reading_id and head.reading_id != src.reading_id:
                    return None  # read again since: its block numbers mean other text now
                self.ws.readings.setdefault(str(src.version_id), head.reading_id)
                got = expander(self.ws, conn, head, src)
        except ToolError:
            return None
        if got is None:
            return None
        body, scope, where = got
        body = _clip(body.strip(), REREAD_CHARS if kind != "next_page" else CONTEXT_CHARS["table"])
        if not body or " ".join(body.split()) in " ".join(src.text.split()):
            return None  # nothing the judge has not been shown
        return Source(sid="", document_id=src.document_id, version_id=src.version_id, title=src.title,
                      section=src.section, location=f"{src.location} — {scope}",
                      kind="table" if src.table_index is not None else "context", text=body,
                      block_start=where.get("block_start", src.block_start),
                      block_end=where.get("block_end", src.block_end),
                      table_index=where.get("table_index", src.table_index), reading_id=head.reading_id,
                      page_list=where.get("page_list", src.page_list), partial_document=head.partial,
                      contexts=list(src.contexts), context=src.context)

    def adopt(self, sid: str, kind: str) -> str | None:
        """Register the expansion ``expand`` returned as a turn source, once; its id."""
        key = (sid, kind)
        found = self.pending.get(key)
        if found is None:
            return None
        if key not in self.adopted:
            self.adopted[key] = self.ws.adopt(found).sid
        return self.adopted[key]
