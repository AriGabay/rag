"""The tools the answering model may call, and the evidence they produce.

The model never runs SQL and never sees anything the user may not: every tool runs in its own short
transaction under the user's tenant context (RLS decides what is visible), validates its arguments, and returns
plain text the model reads. Every passage a tool returns is registered as a source with an id (``S1``...); every
stored measurement as ``M1``...; every value taken from a source as ``V1``..., every user assumption as ``A1``...
and every calculation as ``C1``.... Every tool also records how deep it reached
into each document — located (listed in a set or outlined), a passage retrieved, a section or table read (or its
measurements listed) — and which sections and tables it opened, so the server can state an answer's coverage
itself; a document whose datum the verified answer cites is ``verified`` (``coverage.build``). An
answer may cite only ids issued in the same turn, so a previous answer is never a source: an earlier turn's
passages are offered as ``P`` references the model must re-open (``read``), which reads them again under the
current permissions and only while the document's reading is the one they were read from.

Documents, sections, tables, continuations and unread regions get turn-local short handles (``D#``, ``§#``,
``T#``, ``K#``, ``R#``) the server maps to what they name; every handle but ``D#`` is bound to the version and
the reading it was issued for, and every use resolves the document again under the current permissions (KTD9).

Tools:

- ``search``: hybrid retrieval (lexical, trigram, semantic) over text, tables, table rows and picture text,
  optionally within named documents;
- ``read``: one locator — a source (``S#``/``P#``: the paragraphs around it, or its whole table), a page range of
  a document, a section (``§#``, its sub-sections included), a table (``T#``) or the continuation of an earlier
  read (``K#``). A result is a part of bounded size whose header states whether it is complete, clipped (and the
  ``K#`` that continues it), has unread regions (marked in place with their ``R#``) or uncertain reading; blocks
  the turn already returned are sent as a pointer to the source that returned them (``app.chat.reader``);
- ``find_documents``: every document that contains all the terms that define a set (a place, a document type),
  in its title or text — the scope of a question about a set of documents, paged, with the total;
- ``list_documents``: visible documents with their reading status (fully read, partial: what was not read), paged;
- ``outline``: a document's sections and tables as openable handles, with their sizes and unread regions;
- ``find_measurements``: stored measurements with their meaning (kind, unit, period, area basis, VAT, role,
  subject), grouped by what can be compared, with the coverage of the documents in scope, paged;
- ``take_value``: a value of a source the turn read, verified by the server — the cell at a named row and column
  of the table the source is (``extracted_tables.structure``), or a number inside an exact quote of the source —
  with its meaning; what the source attests about it is recorded as the source's, the rest as the model's (``V#``);
- ``assume``: a number the user gave for a scenario, quoted from the user's own message (``A#``);
- ``calculate``: an expression over ``M#``/``V#``/``A#``/``C#`` (``app.chat.calc``): exact decimals, compatibility
  by operation, every result a ``C#`` with its formula, inputs, assumptions and sources that later calculations
  may use.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.verify import numbers_in
from app.chat import calc, meaning, reader
from app.chat.evidence import TABLE_SIZE_PREFIX
from app.db import TenantContext, tenant_tx
from app.measurements.extract import EXTRACTION_VERSION, PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
from app.platform.documents import reading_notes
from app.platform.search import SearchScope, search_passages

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 6
SEARCH_MAX = 12
PASSAGE_CHARS = 1600
CONTEXT_CHARS = {"neighbors": 3500, "section": 9000, "table": 14000}
MEASUREMENTS_MAX = 120  # per page
LIST_MAX = 60  # per page
SCOPE_PAGE = 30

ROLE_LABELS = {
    "appraiser_determination": "קביעת השמאי", "actual_contract": "חוזה בפועל",
    "comparable_transaction": "עסקת השוואה", "asking_price": "מחיר מבוקש", "survey_statistic": "נתון סקר",
    "calculation": "תוצאת תחשיב", "planning_legal": "תכנוני/משפטי", "other": "אחר",
}
SUBJECT_ROLE_LABELS = {
    "appraised_property": "הנכס הנישום", "comparable": "נכס השוואה", "survey": "סקר", "asking": "היצע",
    "contract": "חוזה", "general": "כללי", "other": "אחר",
}
KIND_LABELS = {
    "value_per_area": "שווי ליחידת שטח", "price_per_area": "מחיר ליחידת שטח", "rent_per_area": "דמי שכירות ליחידת שטח",
    "management_fee_per_area": "דמי ניהול ליחידת שטח", "cost_per_area": "עלות ליחידת שטח", "value": "שווי",
    "price": "מחיר", "rent": "דמי שכירות", "management_fee": "דמי ניהול", "cost": "עלות", "levy": "היטל/מס",
    "area": "שטח", "rights_area": "שטח זכויות", "rate": "שיעור", "coefficient": "מקדם", "count": "כמות",
    "duration": "משך", "other": "אחר",
}


# how deep the turn reached into a document, in order: listed in a set or outlined; a passage retrieved (a search
# hit, or the paragraphs around it); a section or table read (or its stored measurements listed); its datum cited
# by the verified answer
LEVELS = ("located", "retrieved", "read", "verified")
NOT_REACHED = "not_reached"  # a document of the set the turn never touched
READ_TO_END = "end"  # a read target read from its beginning to its end (``Workspace.read_progress``)


class ToolError(Exception):
    """A tool call the server refuses (bad arguments, unknown ids); its message goes back to the model."""


@dataclass
class Source:
    sid: str
    document_id: UUID | None  # None for a listing: it names documents, it is not one
    version_id: UUID | None
    title: str
    section: str | None
    location: str
    kind: str
    text: str
    block_start: int | None = None
    block_end: int | None = None
    table_index: int | None = None
    chunk_id: UUID | None = None
    page_list: list[int] | None = None
    partial_document: bool = False
    same_as: str | None = None  # an earlier id of this turn that returned the same text (not sent again)
    listed: list[str] = field(default_factory=list)  # a listing's documents (its page), for permission checks
    # a listing's set: {"key", "criterion", "total", "page", "pages", "documents": [{document_id, title}]}
    listing: dict | None = None
    # the reading its blocks were read from (``document_versions.ingestion.reading_id``, KTD7): a reference to it
    # after the version was read again is stale
    reading_id: str | None = None
    # what a ``read`` returned (KTD9): complete | clipped | has_unread_regions | uncertain_reading; whether it was
    # clipped, the R# handles of the regions in it that were not read, and the handle that reads on (K#, or the
    # section around paragraphs)
    status: str | None = None
    clipped: bool = False
    unread_regions: list[str] = field(default_factory=list)
    more: str | None = None
    resume: dict | None = None  # where it continues, as a position that outlives the turn (a later P#)
    # what was sent to the model when it is not ``text``: blocks the turn already returned are sent as a pointer,
    # while ``text`` stays the full text the verifier checks against
    body: str | None = None
    tags: dict = field(default_factory=dict)  # handles shown on the source's tag: {"document": "D1", "table": "T2"}
    table_part: bool = False  # some rows of a table read in parts, not all of them

    @property
    def is_listing(self) -> bool:
        return self.kind == "listing"

    def public(self) -> dict:
        out = {"id": self.sid, "document_id": str(self.document_id) if self.document_id else None,
               "version_id": str(self.version_id) if self.version_id else None,
               "title": self.title, "section": self.section, "location": self.location, "kind": self.kind,
               "text": self.text, "block_start": self.block_start, "block_end": self.block_end,
               "table_index": self.table_index, "page_list": self.page_list, "reading_id": self.reading_id,
               "status": self.status, "clipped": self.clipped, "unread_regions": len(self.unread_regions),
               "more": self.resume}
        if self.is_listing:
            out["listed_document_ids"] = list(self.listed)
        return out


@dataclass
class Measurement:
    mid: str
    id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    row: Any
    listings: set = field(default_factory=set)  # the find_measurements listings that returned it

    def public(self) -> dict:
        r = self.row
        return {"id": self.mid, "measurement_id": str(self.id), "document_id": str(self.document_id),
                "version_id": str(self.version_id), "title": self.title, "metric": r.metric,
                "metric_kind": r.metric_kind, "value_text": r.value_text, "unit": r.unit, "period": r.period,
                "vat": r.vat, "area_basis": r.area_basis, "subject": r.subject, "value_role": r.value_role,
                "status": r.status, "quote": r.quote, "section": r.section, "block_index": r.block_index,
                "table_index": r.table_index, "anchor_lost": anchor_lost(r),
                "reading_id": getattr(r, "reading_id", None)}


def anchor_lost(row) -> bool:
    """A reviewed measurement whose place was not found again when its document was reprocessed (KTD7): its
    value keeps the reviewer's decision, but it is not verified against a cell or passage of the current reading."""
    return getattr(row, "anchor_lost", None) is not None


@dataclass
class Workspace:
    """Everything one turn gathered: sources, measurements, computations, and earlier-turn references."""

    ctx: TenantContext
    sources: dict[str, Source] = field(default_factory=dict)
    measurements: dict[str, Measurement] = field(default_factory=dict)
    computations: dict[str, calc.Computation] = field(default_factory=dict)
    values: dict[str, calc.Value] = field(default_factory=dict)  # V#: values verified in a source of the turn
    assumptions: dict[str, calc.Assumption] = field(default_factory=dict)  # A#: numbers the user gave
    # the user's messages visible to the turn, oldest first, the current one last: [{"turn", "text", "current"}]
    user_messages: list[dict] = field(default_factory=list)
    prior: dict[str, dict] = field(default_factory=dict)  # P# -> {version_id, block_start, block_end, chunk_id}
    searches: list[str] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    # document id -> {"title", "level", "read", "partial", "openings": [{"sid", "scope", "name"}]}: how deep the
    # turn's tools reached into each document (``LEVELS``), whether a section or table of it was read, and the
    # sections and tables they opened
    activity: dict[str, dict] = field(default_factory=dict)
    # the set a question is about: {"query", "matching": [{document_id, title}], "pages", "pages_read"}
    scope: dict | None = None
    # find_measurements listings: filter key -> {"pages", "pages_read", "total"}
    listings: dict[tuple, dict] = field(default_factory=dict)
    returned: dict[tuple, str] = field(default_factory=dict)  # what was already returned -> its first id
    fetched: dict = field(default_factory=dict)  # what the meaning check read for the turn (``meaning.Fetcher``)
    # version id -> its reading id as the turn first read it (KTD7). Pinned for the turn: a version read again
    # mid-turn makes the turn's earlier sources of it stale, never silently re-bound.
    readings: dict = field(default_factory=dict)
    # turn-local handles (D#, §#, T#, K#, R#) -> what they name ({"kind", "version_id", "reading_id", ...})
    handles: dict[str, dict] = field(default_factory=dict)
    handle_ids: dict[tuple, str] = field(default_factory=dict)
    # version id -> block index -> the S# that first returned the block's whole text (block-level dedupe)
    sent: dict[str, dict[int, str]] = field(default_factory=dict)
    # what was read of each section, page range or table (a ``read`` target): {"to": how far it was read
    # contiguously from its beginning (``READ_TO_END`` at its end), "unread": a region of it was not read,
    # "document_id"}
    reads: dict[tuple, dict] = field(default_factory=dict)

    def handle(self, prefix: str, key: tuple, **data) -> str:
        """The turn's short handle for what ``key`` names; the same thing keeps its handle."""
        h = self.handle_ids.get((prefix, key))
        if h is None:
            h = f"{prefix}{sum(1 for x in self.handles.values() if x['kind'] == prefix) + 1}"
            self.handles[h] = {"kind": prefix} | data
            self.handle_ids[(prefix, key)] = h
        return h

    def doc_handle(self, document_id) -> str:
        return self.handle("D", (str(document_id),), document_id=str(document_id))

    def read_progress(self, target: tuple, start, nxt, document_id: str, unread: bool) -> None:
        """Record a part of ``target`` read from ``start`` (None: its beginning) to ``nxt`` (None: its end). A target
        is read completely only when its parts followed each other from the beginning to the end; the document's
        activity then says whether each target it read was read completely (``read_complete``) or not
        (``read_partial``)."""
        st = self.reads.setdefault(target, {"to": None, "unread": False, "document_id": document_id})
        if start is None or st["to"] == start:
            to = READ_TO_END if nxt is None else nxt
            if st["to"] is None or to == READ_TO_END or (st["to"] != READ_TO_END and to > st["to"]):
                st["to"] = to
        st["unread"] = st["unread"] or unread
        done = [s["to"] == READ_TO_END and not s["unread"] for s in self.reads.values()
                if s["document_id"] == document_id]
        a = self.activity.get(document_id)
        if a is not None:
            a["read_complete"], a["read_partial"] = any(done), not all(done)

    def read_complete(self, target) -> bool | None:
        """Whether a target was read to its end with no unread region (None: never read as a target)."""
        st = self.reads.get(target) if target is not None else None
        return None if st is None else st["to"] == READ_TO_END and not st["unread"]

    def note_readings(self, conn: Connection, version_ids) -> None:
        """Pin the reading ids of versions the turn is reading, in the transaction that reads them."""
        todo = list({str(v) for v in version_ids if v is not None} - set(self.readings))
        if todo:
            for r in conn.execute(text("SELECT id, ingestion->>'reading_id' AS reading_id FROM document_versions"
                                       " WHERE id = ANY(:v)"), {"v": [UUID(v) for v in todo]}):
                self.readings[str(r.id)] = r.reading_id

    def once(self, source: Source, key: tuple) -> Source:
        """Mark a source whose text this turn already returned: it keeps its own id (citable, verified against
        the same text) but is sent to the model as a reference to the first one."""
        first = self.returned.get(key)
        if first is not None and first != source.sid:
            source.same_as = first
        else:
            self.returned[key] = source.sid
        return source

    def touch(self, document_id, title: str, level: str, partial: bool = False, opening: dict | None = None) -> None:
        a = self.activity.setdefault(str(document_id), {"title": title, "level": "located", "read": False,
                                                        "partial": False, "openings": [], "read_complete": False,
                                                        "read_partial": False})
        if LEVELS.index(level) > LEVELS.index(a["level"]):
            a["level"] = level
        a["read"] = a["read"] or level == "read"  # kept apart: a verified datum may come from a passage only
        a["partial"] = a["partial"] or partial
        if opening is not None:
            a["openings"].append(opening)

    def _sid(self) -> str:
        return f"S{len(self.sources) + 1}"

    def add_source(self, **kw) -> Source:
        return self.adopt(Source(sid="", **kw))

    def add_measurement(self, row) -> Measurement:
        """A stored measurement as the turn's M#; one found again keeps its id, so it is never counted twice."""
        known = next((m for m in self.measurements.values() if m.id == row.id), None)
        if known is None:
            known = Measurement(f"M{len(self.measurements) + 1}", row.id, row.document_id, row.version_id, row.title,
                                row)
            self.measurements[known.mid] = known
        return known

    def adopt(self, source: Source) -> Source:
        """Register a source the server read for its own checks, once it is cited (a new id)."""
        if source.version_id is not None and source.reading_id is None:
            source.reading_id = self.readings.get(str(source.version_id))
        source.sid = self._sid()
        self.sources[source.sid] = source
        return source


# --- helpers ---------------------------------------------------------------------------------------------

def _location(section: str | None, kind: str, page_list, block_start, block_end, paragraphs, media) -> str:
    parts = []
    if page_list:
        parts.append(f"עמוד {page_list[0]}" if len(page_list) == 1 else f"עמודים {page_list[0]}–{page_list[-1]}")
    if section:
        parts.append(f"סעיף \"{section}\"")
    if kind in ("table", "table_row"):
        parts.append("טבלה" + (" (מתוך תמונה)" if media else ""))
    elif kind == "image":
        parts.append("תמונה")
    elif paragraphs:
        a, b = paragraphs
        if a and b:
            parts.append(f"פסקה {a}" if a == b else f"פסקאות {a}–{b}")
    return ", ".join(parts) or "המסמך"


def _block_info(conn: Connection, version_id: UUID, start: int | None, end: int | None) -> tuple:
    if start is None:
        return (None, None), None
    rows = conn.execute(text(
        "SELECT paragraph_no, media FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :b"
        " ORDER BY block_index"), {"v": version_id, "a": start, "b": end if end is not None else start}).all()
    nums = [r.paragraph_no for r in rows if r.paragraph_no]
    media = next((r.media for r in rows if r.media), None)
    return ((min(nums), max(nums)) if nums else (None, None)), media


def _partial_versions(conn: Connection, version_ids: list[UUID]) -> set[UUID]:
    if not version_ids:
        return set()
    return {r.id for r in conn.execute(text(
        "SELECT id FROM document_versions WHERE id = ANY(:v) AND (pages_incomplete > 0 OR"
        " COALESCE((ingestion->>'partial')::boolean, false))"), {"v": version_ids})}


def _visible_documents(conn: Connection, ids: list[str]) -> list[UUID]:
    out = []
    for raw in ids or []:
        try:
            out.append(UUID(str(raw)))
        except ValueError:
            raise ToolError(f"מזהה מסמך לא תקין: {raw}") from None
    if not out:
        return []
    found = {r.id for r in conn.execute(text(
        "SELECT id FROM documents WHERE id = ANY(:d) AND deleted_at IS NULL"), {"d": out})}
    missing = [str(d) for d in out if d not in found]
    if missing:
        raise ToolError("מסמכים לא נמצאו או שאין הרשאה אליהם: " + ", ".join(missing))
    return out


_TABLE_ROWS = re.compile(re.escape(TABLE_SIZE_PREFIX) + r"\s*(\d+)\s*שורות")


def table_row_count(text: str) -> int | None:
    """The row count a source's table size line states (``table_size_line``), or None."""
    m = _TABLE_ROWS.search(text or "")
    return int(m.group(1)) if m else None


def table_size_line(structure: dict | None) -> str:
    """"הטבלה: 9 שורות; ערכים לפי עמודה: ..." — so a single row is never read as the whole table."""
    st = structure or {}
    rows = [r.get("cells") or [] for r in st.get("rows") or []]
    if not rows:
        return ""
    headers = st.get("headers") or []
    counts = []
    for i, h in enumerate(headers):
        n = sum(1 for r in rows if i < len(r) and re.search(r"\d", r[i] or ""))
        if h and n:
            counts.append(f"{h} {n}")
    return f"{TABLE_SIZE_PREFIX} {len(rows)} שורות" + (f"; ערכים מספריים לפי עמודה: {', '.join(counts[:6])}" if counts else "")


def _table_sizes(conn: Connection, keys: set[tuple]) -> dict[tuple, str]:
    if not keys:
        return {}
    vids = list({k[0] for k in keys})
    out = {}
    for r in conn.execute(text("SELECT version_id, table_index, structure FROM extracted_tables"
                               " WHERE version_id = ANY(:v) AND table_index = ANY(:t)"),
                          {"v": vids, "t": list({k[1] for k in keys})}):
        if (r.version_id, r.table_index) in keys:
            out[(r.version_id, r.table_index)] = table_size_line(r.structure)
    return out


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + " …[קוצר]"


def _with_size(size: str | None, body: str) -> str:
    return f"{size}\n{body}" if size else body


def _render_source(s: Source) -> str:
    if s.is_listing:
        return f'<source id="{s.sid}" kind="listing" title="{_attr(s.title)}">\n{_txt(s.text)}\n</source>'
    if s.same_as:
        return (f'<source id="{s.sid}" same_as="{s.same_as}" document_id="{s.document_id}" title="{_attr(s.title)}"'
                f' location="{_attr(s.location)}"/> (אותו טקסט כמו {s.same_as}, שכבר הוחזר בתור הזה)')
    flag = " (המסמך נקרא חלקית: חלק מהתמונות לא נקראו)" if s.partial_document else ""
    tags = "".join(f' {k}="{v}"' for k, v in s.tags.items())
    if s.status:  # a reading: how complete it is, the reading it is of, and how to read on
        tags += (f' status="{s.status}" version_id="{s.version_id}" reading_id="{_attr(s.reading_id)}"'
                 + (f' more="{s.more}"' if s.more else ""))
    return (f'<source id="{s.sid}" document_id="{s.document_id}"{tags} title="{_attr(s.title)}"'
            f' location="{_attr(s.location)}" kind="{s.kind}">{flag}\n{_txt(s.body if s.body is not None else s.text)}'
            "\n</source>")


def _attr(v: str | None) -> str:
    from app.providers.llm import prompt_attr

    return prompt_attr(v or "")


def _txt(v: str | None) -> str:
    from app.providers.llm import prompt_text

    return prompt_text(v or "")


# --- tools -----------------------------------------------------------------------------------------------

def tool_search(ws: Workspace, query: str, document_ids: list[str] | None = None, limit: int | None = None) -> str:
    query = (query or "").strip()
    if len(query) < 2:
        raise ToolError("שאילתת חיפוש ריקה")
    limit = max(3, min(int(limit or SEARCH_LIMIT), SEARCH_MAX))
    ws.searches.append(query)
    with tenant_tx(ws.ctx) as conn:
        docs = _visible_documents(conn, document_ids or [])
        scope = SearchScope(document_ids=tuple(docs)) if docs else None
        hits = search_passages(conn, query, limit, scope=scope)
        ids = [h["chunk_id"] for h in hits]
        extra = {r.id: r for r in conn.execute(text(
            "SELECT id, block_start, block_end FROM chunks WHERE id = ANY(:i)"), {"i": ids})} if ids else {}
        partial = _partial_versions(conn, list({h["version_id"] for h in hits}))
        ws.note_readings(conn, {h["version_id"] for h in hits})
        sizes = _table_sizes(conn, {(h["version_id"], h.get("table_index")) for h in hits
                                    if h["kind"] in ("table", "table_row") and h.get("table_index") is not None})
        # a table chunk already holds its rows: a row hit of a returned table part adds nothing
        tables = [h for h in hits if h["kind"] == "table"]
        out = []
        for h in hits:
            if h["kind"] == "table_row" and any(
                    t["version_id"] == h["version_id"] and t.get("table_index") == h.get("table_index")
                    and h["text"].split(": ", 1)[-1][:40] in t["text"] for t in tables):
                continue
            e = extra.get(h["chunk_id"])
            bs, be = (e.block_start, e.block_end) if e else (None, None)
            paragraphs, media = _block_info(conn, h["version_id"], bs, be)
            tags = {"document": ws.doc_handle(h["document_id"])}
            if h.get("table_index") is not None:  # a matched row opens its whole table
                tags["table"] = _table_handle(ws, h["document_id"], h["version_id"],
                                              ws.readings.get(str(h["version_id"])), h["table_index"])
            out.append(ws.once(ws.add_source(
                document_id=h["document_id"], version_id=h["version_id"], title=h["title"], section=h["section"],
                location=_location(h["section"], h["kind"], h["page_list"], bs, be, paragraphs, media),
                kind=h["kind"], text=_with_size(sizes.get((h["version_id"], h.get("table_index"))),
                                                _clip(h["text"], PASSAGE_CHARS)), block_start=bs, block_end=be,
                table_index=h.get("table_index"), chunk_id=h["chunk_id"], page_list=h["page_list"] or None,
                partial_document=h["version_id"] in partial, tags=tags), ("chunk", h["chunk_id"])))
            ws.touch(h["document_id"], h["title"], "retrieved", h["version_id"] in partial)
    if not out:
        return f'לא נמצאו קטעים עבור "{query}". אפשר לנסות ניסוח אחר, מונחים נרדפים או חיפוש בתוך מסמך מסוים.'
    return "\n\n".join(_render_source(s) for s in out)


MSG_STALE_REF = ("המקור {source_id} נקרא מקריאה קודמת של המסמך: המסמך עובד מחדש מאז, והמיקום שלו אינו תקף עוד. "
                 "לא נפתח דבר. כדי להסתמך על התוכן יש לחפש אותו מחדש במסמך (search או outline).")


def _ref(ws: Workspace, source_id: str) -> dict:
    sid = (source_id or "").strip()
    if sid in ws.sources:
        s = ws.sources[sid]
        return {"version_id": s.version_id, "block_start": s.block_start, "block_end": s.block_end,
                "table_index": s.table_index, "chunk_id": s.chunk_id, "reading_id": s.reading_id}
    if sid in ws.prior:
        return ws.prior[sid]
    raise ToolError(f"מזהה מקור לא מוכר: {source_id}. אפשר לפתוח רק מקורות שהוחזרו בתור הזה (S#) או הפניות מתורות קודמות (P#).")


MSG_UNAVAILABLE = "המקור אינו זמין (נמחק, או שאין הרשאה אליו)"
MSG_DOCUMENT_UNAVAILABLE = "המסמך אינו זמין (לא נמצא, נמחק, או שאין הרשאה אליו)"
MSG_STALE_HANDLE = ("{handle} נפתח מקריאה קודמת של המסמך: המסמך עובד מחדש מאז, והמיקום אינו תקף עוד. לא נפתח דבר. "
                    "אפשר לפתוח את מבנה המסמך מחדש (outline) או לחפש שוב.")
LOCATORS = ("source", "pages", "section", "table", "cursor")
NEIGHBORS = 3  # blocks on each side of a source
PAGES_MAX = 30  # pages in one page range
SECTIONS_LISTED = 8  # section handles listed with a page range
STATUS_LABELS = {"complete": "נקרא במלואו", "clipped": "נקרא חלק ממנו; יש המשך",
                 "has_unread_regions": "יש בו אזורים שלא נקראו", "uncertain_reading": "חלק ממנו נקרא בקריאה לא ודאית"}
_TABLE_SOURCES = {"emf": "טבלה מתוך תמונה וקטורית (נקראה במדויק)", "vision": "טבלה מתוך תמונה (קריאה חזותית)",
                  "ocr": "טבלה מתוך תמונה (OCR)"}


def _table_text(st: dict, rows: list) -> str:
    """A table as the model and the verifier read it: its caption and titles, its size, its headers, the given
    rows and its notes."""
    lines = [x for x in [st.get("caption"), *(st.get("title") or []), table_size_line(st)] if x]
    if any(st.get("headers") or []):
        lines.append(" | ".join(st["headers"]))
    lines += [" | ".join(r.get("cells") or []) for r in rows]
    lines += st.get("notes") or []
    note = _TABLE_SOURCES.get(st.get("source") or "", "")
    return (note + "\n" if note else "") + "\n".join(lines)


def read_scope(ws: Workspace, source_id: str, scope: str = "neighbors", ref: dict | None = None) -> Source:
    """The context around a source (``neighbors``), its whole top-level section, or its whole table, read for the
    server's own checks (``app.chat.meaning``) through the same reader as ``read``: the source is not registered
    and the turn's coverage does not change — the caller registers it (``Workspace.adopt``) only if it cites it."""
    if scope not in CONTEXT_CHARS:
        raise ToolError("scope חייב להיות neighbors, section או table")
    ref = ref or _ref(ws, source_id)
    vid = UUID(str(ref["version_id"]))
    with tenant_tx(ws.ctx) as conn:
        head = reader.version(conn, vid)
        if head is None:
            raise ToolError(MSG_UNAVAILABLE)
        if "reading_id" in ref and ref["reading_id"] != head.reading_id:
            # block numbers of an earlier reading mean other text in the current one: nothing is opened
            raise ToolError(MSG_STALE_REF.format(source_id=source_id))
        ws.readings.setdefault(str(vid), head.reading_id)

        def make(**kw) -> Source:
            return Source(sid="", document_id=head.document_id, version_id=vid, title=head.title,
                          partial_document=head.partial, reading_id=head.reading_id, **kw)

        start, end = ref.get("block_start"), ref.get("block_end")
        table_index = ref.get("table_index")
        if start is None and ref.get("chunk_id") and not (scope == "table" and table_index is not None):
            row = conn.execute(text("SELECT text, section, page_list, kind FROM chunks WHERE id = :c AND version_id = :v"),
                               {"c": ref["chunk_id"], "v": vid}).first()
            if row is None:
                raise ToolError(MSG_UNAVAILABLE)
            return make(section=row.section, kind=row.kind, text=_clip(row.text, CONTEXT_CHARS[scope]),
                        location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                        page_list=row.page_list)
        if scope == "table":
            if table_index is None and start is not None:
                table_index = next((r.table_index for r in reader.blocks_between(
                    conn, vid, start, end if end is not None else start) if r.table_index is not None), None)
            st = reader.table_structure(conn, vid, table_index) if table_index is not None else None
            if st is None:
                raise ToolError("למקור הזה אין טבלה")
            return make(section=st.get("section"), kind="table",
                        location=_location(st.get("section"), "table", None, None, None, (None, None), st.get("media")),
                        text=_clip(_table_text(st, st.get("rows") or []), CONTEXT_CHARS["table"]),
                        block_start=st.get("block_index"), block_end=st.get("block_index"), table_index=table_index)
        if start is None:
            raise ToolError("למקור הזה אין מיקום במסמך להרחבה")
        top = None
        if scope == "section":
            top = reader.section_path_of(conn, vid, start)[:1]
            rows = reader.blocks_in_section(conn, vid, top)
        else:
            rows = reader.blocks_between(conn, vid, max(0, start - NEIGHBORS),
                                         (end if end is not None else start) + NEIGHBORS)
        rows = [r for r in rows if r.text]
        if not rows:
            raise ToolError("אין טקסט בהקשר הזה")
        nums = [r.paragraph_no for r in rows if r.paragraph_no]
        section = rows[0].section if scope == "neighbors" else ((top[0] if top else None) or rows[0].section)
        return make(section=section, kind="context", text=_clip("\n".join(r.text for r in rows), CONTEXT_CHARS[scope]),
                    location=_location(section, "text", None, rows[0].block_index, rows[-1].block_index,
                                       (min(nums), max(nums)) if nums else (None, None), None),
                    block_start=rows[0].block_index, block_end=rows[-1].block_index)


# --- read: one locator, a bounded part, a stated completeness ----------------------------------------------------

def _table_handle(ws: Workspace, document_id, version_id, reading_id, table_index: int) -> str:
    return ws.handle("T", (str(version_id), table_index), document_id=str(document_id), version_id=str(version_id),
                     reading_id=reading_id, table_index=table_index)


def _section_handle(ws: Workspace, v: reader.Version, path: tuple) -> str:
    return ws.handle("§", (str(v.version_id), tuple(path)), document_id=str(v.document_id),
                     version_id=str(v.version_id), reading_id=v.reading_id, path=tuple(path))


def _section_name(path: tuple) -> str:
    return path[-1] if path else "תחילת המסמך (לפני הכותרת הראשונה)"


def _pages_label(pages: list[int]) -> str:
    if not pages:
        return "המסמך"
    return f"עמוד {pages[0]}" if pages[0] == pages[-1] else f"עמודים {pages[0]}–{pages[-1]}"


def _handle(ws: Workspace, value: str, kind: str) -> dict:
    data = ws.handles.get(value)
    if data is None or data["kind"] != kind:
        what = {"§": "סעיף (§# מ-outline)", "T": "טבלה (T# מ-outline או מתוצאת חיפוש)", "K": "המשך (K# מ-more)"}[kind]
        raise ToolError(f"מזהה לא מוכר: {value}. נדרש מזהה {what} שהוחזר בתור הזה.")
    return data


def _bound(conn: Connection, ws: Workspace, handle: str, data: dict) -> reader.Version:
    """The version a handle was issued for, under the current permissions and only while its reading is current."""
    v = reader.version(conn, UUID(data["version_id"]))
    if v is None:
        raise ToolError(MSG_UNAVAILABLE)
    if v.reading_id != data["reading_id"]:
        raise ToolError(MSG_STALE_HANDLE.format(handle=handle))
    return v


def _document(conn: Connection, ws: Workspace, ref) -> reader.Version:
    """The current version of a document named by its D# or its id, under the current permissions. A document
    the user may not see is not available, with nothing about it (not its title, not its size)."""
    ref = str(ref or "").strip()
    data = ws.handles.get(ref)
    if data is not None and data["kind"] != "D":
        raise ToolError(f"{ref} אינו מסמך: מסמך נבחר ב-D# (מ-list_documents, find_documents או search) או ב-document_id")
    try:
        document_id = UUID(data["document_id"] if data is not None else ref)
    except ValueError:
        raise ToolError(f"מזהה מסמך לא מוכר: {ref}") from None
    v = reader.current_version(conn, document_id)
    if v is None:
        raise ToolError(MSG_DOCUMENT_UNAVAILABLE)
    ws.readings.setdefault(str(v.version_id), v.reading_id)
    return v


def _target_json(target: tuple, pos) -> dict | None:
    """A read target and a position in it as JSON: where a clipped source continues, kept with an answer's
    sources so a later turn's P# can continue from there."""
    if pos is None or target[0] not in ("section", "pages", "table"):
        return None
    return {"target": [target[0], target[1], *[list(x) if isinstance(x, tuple) else x for x in target[2:]]],
            "pos": list(pos) if isinstance(pos, tuple) else pos}


def _target_from_json(resume, version_id: str) -> tuple[tuple, object] | None:
    """The target and position of ``_target_json`` back, when they are well formed and of ``version_id``."""
    try:
        t, pos = resume["target"], resume["pos"]
        if str(t[1]) != version_id:
            return None
        if t[0] == "section":
            return ("section", version_id, tuple(str(x) for x in t[2])), (int(pos[0]), int(pos[1]))
        if t[0] == "pages":
            return ("pages", version_id, int(t[2]), int(t[3])), (int(pos[0]), int(pos[1]))
        if t[0] == "table":
            return ("table", version_id, int(t[2])), int(pos)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def _cursor(ws: Workspace, v: reader.Version, target: tuple, pos) -> str:
    return ws.handle("K", (target, pos), document_id=str(v.document_id), version_id=str(v.version_id),
                     reading_id=v.reading_id, target=target, pos=pos)


def _emit(ws: Workspace, v: reader.Version, target: tuple, pos, part: reader.Part, *, name: str, scope: str,
          location: str, section: str | None, lines: list[str], read: bool, more: str | None = None,
          cut: bool = False, unread_pages: list[int] | None = None) -> Source:
    """A part of a window as a source of the turn. Its ``text`` is the part's full text (what the verifier
    checks); what is sent leaves out blocks the turn already returned whole, as a pointer to the source that
    returned them, and marks unread and uncertain regions where they are in reading order with their R#."""
    vid = str(v.version_id)
    sent = ws.sent.setdefault(vid, {})
    content, body, regions, run = [], [], [], []
    unread, uncertain = bool(unread_pages), False

    def flush() -> None:
        if run:
            body.append(f"[{len(run)} קטעים שכבר הוחזרו בתור הזה ב-{', '.join(dict.fromkeys(run))}: לא נשלחים שוב]")
            run.clear()

    for r, piece, whole in part.items:
        if r.status == "decorative":
            continue
        region = None
        if r.status in (reader.UNREAD, reader.UNCERTAIN):
            region = ws.handle("R", (vid, r.block_index), document_id=str(v.document_id), version_id=vid,
                               reading_id=v.reading_id, block_index=r.block_index, page=r.page, bbox=r.bbox,
                               status=r.status, note=r.note)
            regions.append(region)
        if r.status == reader.UNREAD:
            unread = True
            flush()
            where = f"עמוד {r.page}, " if r.page else ""
            body.append(f"[אזור שלא נקרא {region}: {where}{'תמונה' if r.kind == 'image' else 'קטע'}"
                        + (f" — {r.note}" if r.note else "") + "]")
            continue
        if r.status == "no_text":
            flush()
            body.append(f"[תמונה ללא טקסט{': ' + r.note if r.note else ''}]")
            continue
        uncertain = uncertain or r.status == reader.UNCERTAIN
        if piece:
            content.append(piece)
        if whole and r.block_index in sent:
            run.append(sent[r.block_index])
            continue
        flush()
        if r.status == reader.UNCERTAIN:
            body.append(f"[קריאה לא ודאית {region}" + (f" — {r.note}" if r.note else "") + "]")
        if piece:
            body.append(piece)
        if r.kind == "table" and r.table_index is not None:
            t = _table_handle(ws, v.document_id, v.version_id, v.reading_id, r.table_index)
            body.append(f"[טבלה {t}: כל השורות, הכותרות וההערות — read(table={t})]")
    flush()
    if part.next is not None:
        more = _cursor(ws, v, target, part.next)
    clipped = part.next is not None or cut
    status = ("clipped" if clipped else "has_unread_regions" if unread
              else "uncertain_reading" if uncertain else "complete")
    head = [f"מצב: {STATUS_LABELS[status]}" + (f"; להמשך: read(cursor={more})" if part.next is not None
                                               else f"; להמשך: read(section={more})" if more else "")]
    if unread_pages:
        head.append("עמודים שלא נקראו בעיבוד המסמך: " + ", ".join(str(p) for p in unread_pages))
    indexes = [r.block_index for r, _, _ in part.items]
    pages = sorted({r.page for r, _, _ in part.items if r.page})
    s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title, section=section,
                      location=location or _pages_label(pages), kind="context", text="\n".join(content),
                      block_start=min(indexes), block_end=max(indexes), page_list=pages or None,
                      partial_document=v.partial, reading_id=v.reading_id, status=status,
                      clipped=clipped, unread_regions=regions, more=more,
                      resume=_target_json(target, part.next), body="\n".join(head + lines + body),
                      tags={"document": ws.doc_handle(v.document_id)})
    first = ws.returned.get(("read", target, pos))
    if first is not None:
        s.same_as = first
    else:
        ws.returned[("read", target, pos)] = s.sid
    for r, _, whole in part.items:
        if whole:
            sent.setdefault(r.block_index, s.sid)
    if read:
        ws.touch(v.document_id, v.title, "read", v.partial, {"sid": s.sid, "scope": scope, "name": name,
                                                             "target": target})
        ws.read_progress(target, pos, part.next, str(v.document_id), unread)
    else:
        ws.touch(v.document_id, v.title, "retrieved", v.partial)
    return s


def _read_window(ws: Workspace, conn: Connection, v: reader.Version, target: tuple, pos) -> Source:
    """A part of a section (its sub-sections included) or of a page range, from ``pos`` (None: its beginning)."""
    vid = v.version_id
    unread_pages = None
    if target[0] == "section":
        path = target[2]
        rows = reader.blocks_in_section(conn, vid, path)
        name = _section_name(path)
        lines = []
    else:
        first, last = target[2], target[3]
        rows = reader.blocks_on_pages(conn, vid, first, last)
        unread_pages = reader.unread_pages(conn, vid, first, last)
        name = _pages_label([first, last])
        lines = []
    if not rows:
        if unread_pages:
            raise ToolError(f"{name}: לא נקראו בעיבוד המסמך (אין בהם טקסט שנקרא)")
        raise ToolError(f"אין קטעים שנשמרו ב{name}" if target[0] == "pages" else "אין קטעים בסעיף הזה")
    part = reader.take(rows, pos, reader.SECTION_CHARS, set(ws.sent.get(str(vid), {})))
    pages = sorted({r.page for r, _, _ in part.items if r.page})
    if target[0] == "section":
        location = _location(name, "text", pages, None, None, (None, None), None)
        section = name
    else:
        location, section = _pages_label(pages), None
        # the sections these pages are in, as handles that open each of them whole
        paths = dict.fromkeys(tuple(r.section_path or ())[:i] for r, _, _ in part.items
                              for i in range(1, len(r.section_path or ()) + 1))
        if paths:
            lines.append("סעיפים בעמודים האלה: " + "; ".join(
                f"{_section_handle(ws, v, p)} «{_section_name(p)}»" for p in list(paths)[:SECTIONS_LISTED]))
    return _emit(ws, v, target, pos, part, name=name, scope=target[0], location=location, section=section,
                 lines=lines, read=True, unread_pages=unread_pages if pos is None else None)


def _read_table(ws: Workspace, conn: Connection, v: reader.Version, table_index: int, row0: int = 0) -> Source:
    """``reader.TABLE_ROWS`` rows of a table from ``row0``, each part with the table's caption, size, headers and
    notes."""
    st = reader.table_structure(conn, v.version_id, table_index)
    if st is None:
        raise ToolError("הטבלה לא נמצאה")
    rows = st.get("rows") or []
    window = rows[row0:row0 + reader.TABLE_ROWS]
    nxt = row0 + len(window) if row0 + len(window) < len(rows) else None
    block = reader.table_block(conn, v.version_id, table_index)
    target = ("table", str(v.version_id), table_index)
    vid = str(v.version_id)
    more = _cursor(ws, v, target, nxt) if nxt is not None else None
    uncertain = block is not None and block.status == reader.UNCERTAIN
    status = "clipped" if nxt is not None else "uncertain_reading" if uncertain else "complete"
    head = [f"מצב: {STATUS_LABELS[status]}" + (f"; להמשך: read(cursor={more})" if more else "")]
    if len(rows) > reader.TABLE_ROWS:
        head.append(f"שורות {row0 + 1}–{row0 + len(window)} מתוך {len(rows)}")
    body = _table_text(st, window)
    page = block.page if block is not None else st.get("page")
    index = block.block_index if block is not None else st.get("block_index")
    name = st.get("caption") or (st.get("title") or [""])[0] or st.get("section") or "טבלה"
    s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title, section=st.get("section"),
                      location=_location(st.get("section"), "table", [page] if page else None, None, None,
                                         (None, None), st.get("media")),
                      kind="table", text=body, block_start=index, block_end=index, table_index=table_index,
                      page_list=[page] if page else None, partial_document=v.partial, reading_id=v.reading_id,
                      status=status, clipped=nxt is not None, more=more, resume=_target_json(target, nxt),
                      table_part=len(window) < len(rows),
                      body="\n".join(head) + "\n" + body,
                      tags={"document": ws.doc_handle(v.document_id),
                            "table": _table_handle(ws, v.document_id, vid, v.reading_id, table_index)})
    first = ws.returned.get(("read", target, row0))
    if first is not None:
        s.same_as = first
    else:
        ws.returned[("read", target, row0)] = s.sid
    ws.touch(v.document_id, v.title, "read", v.partial, {"sid": s.sid, "scope": "table", "name": name,
                                                         "target": target})
    ws.read_progress(target, row0 or None, nxt, str(v.document_id), False)
    return s


def _neighbors(rows: list, start: int, end: int, sent: set) -> tuple[list, bool]:
    """The source's blocks and as many around them as fit, nearest first, kept contiguous; whether some were
    left out. Blocks the turn already returned cost nothing: they are sent as a pointer."""
    def cost(r) -> int:
        return 0 if r.block_index in sent else len(r.text or "")

    core = [r for r in rows if start <= r.block_index <= end]
    if not core:
        return [], False
    budget = reader.SECTION_CHARS - sum(cost(r) for r in core)
    lo, hi = rows.index(core[0]), rows.index(core[-1])
    grow = True
    while grow:
        grow = False
        for i in (lo - 1, hi + 1):
            if 0 <= i < len(rows) and cost(rows[i]) <= budget:
                budget -= cost(rows[i])
                lo, hi = min(lo, i), max(hi, i)
                grow = True
    return rows[lo:hi + 1], lo > 0 or hi < len(rows) - 1


def _read_source(ws: Workspace, conn: Connection, sid: str) -> Source:
    """The paragraphs around a source (S#/P#), or the whole table a table passage is from."""
    if sid in ws.sources and ws.sources[sid].is_listing:
        raise ToolError("רשימת מסמכים אינה מקום במסמך: מסמך נפתח ב-outline או ב-pages עם ה-D# שלו")
    ref = _ref(ws, sid)
    v = reader.version(conn, UUID(str(ref["version_id"])))
    if v is None:
        raise ToolError(MSG_UNAVAILABLE)
    if "reading_id" in ref and ref["reading_id"] != v.reading_id:
        # block numbers of an earlier reading mean other text in the current one: nothing is opened
        raise ToolError(MSG_STALE_REF.format(source_id=sid))
    vid = str(v.version_id)
    ws.readings.setdefault(vid, v.reading_id)
    lines = []
    resumed = _target_from_json(ref["resume"], vid) if ref.get("resume") else None
    if resumed is not None:  # an earlier turn read it only in part: where it stopped
        lines.append(f"בתור הקודם המקור נקרא רק בחלקו; להמשך מהמקום שבו נעצרה הקריאה: read(cursor="
                     f"{_cursor(ws, v, *resumed)})")
    if ref.get("table_index") is not None:
        s = _read_table(ws, conn, v, ref["table_index"])
        if lines:
            s.body = "\n".join(lines) + "\n" + s.body
        return s
    start, end = ref.get("block_start"), ref.get("block_end")
    if start is None:
        if not ref.get("chunk_id"):
            raise ToolError("למקור הזה אין מיקום במסמך להרחבה")
        row = conn.execute(text("SELECT text, section, page_list, kind FROM chunks WHERE id = :c AND version_id = :v"),
                           {"c": ref["chunk_id"], "v": v.version_id}).first()
        if row is None:
            raise ToolError(MSG_UNAVAILABLE)
        clipped = len(row.text) > reader.SECTION_CHARS
        status = "clipped" if clipped else "complete"
        s = ws.add_source(document_id=v.document_id, version_id=v.version_id, title=v.title, section=row.section,
                          location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                          kind=row.kind, text=row.text[:reader.SECTION_CHARS], page_list=row.page_list,
                          partial_document=v.partial, reading_id=v.reading_id, status=status, clipped=clipped,
                          tags={"document": ws.doc_handle(v.document_id)})
        s.body = "\n".join([f"מצב: {STATUS_LABELS[status]}"] + lines + [s.text])
        ws.touch(v.document_id, v.title, "retrieved", v.partial)
        return s
    end = end if end is not None else start
    rows = reader.blocks_between(conn, v.version_id, max(0, start - NEIGHBORS), end + NEIGHBORS)
    chosen, cut = _neighbors(rows, start, end, set(ws.sent.get(vid, {})))
    if not chosen:
        raise ToolError("למקור הזה אין קטעים בקריאה הנוכחית של המסמך")
    path = tuple(next((r.section_path for r in chosen if start <= r.block_index <= end), None) or ())
    handles = [f"{_section_handle(ws, v, path[:i])} «{_section_name(path[:i])}»" for i in range(1, len(path) + 1)]
    if handles:
        lines.append("בסעיף: " + " / ".join(handles) + "; לקריאת הסעיף כולו: read(section=§#)")
    part = reader.Part([(r, r.text or "", True) for r in chosen], None)
    pages = sorted({r.page for r in chosen if r.page})
    section = path[-1] if path else None
    s = _emit(ws, v, ("neighbors", vid, start, end), None, part, name=section or "", scope="neighbors",
              location=_location(section, "text", pages, None, None, (None, None), None), section=section,
              lines=lines, read=False, more=_section_handle(ws, v, path) if cut and path else None, cut=cut)
    return s


def tool_read(ws: Workspace, target: dict) -> str:
    """Open one place of a document (``LOCATORS``). Every access resolves the document again under the current
    permissions, and a handle only while the reading it was issued for is the document's current one."""
    if not isinstance(target, dict):
        raise ToolError("target חייב להיות אובייקט עם מיקום אחד")
    given = {k: v for k, v in target.items() if k in LOCATORS and v not in (None, "", {})}
    if len(given) != 1:
        raise ToolError("יש לבחור מיקום אחד בדיוק: source (S#/P#), pages, section (§#), table (T#) או cursor (K#)")
    ((kind, value),) = given.items()
    if kind != "pages":
        value = str(value).strip()
        data = ws.handles.get(value)
        if data is not None and data["kind"] in ("§", "T", "K"):  # a handle given in another field opens as itself
            kind = {"§": "section", "T": "table", "K": "cursor"}[data["kind"]]
        elif data is not None and data["kind"] == "D":
            raise ToolError(f"{value} הוא מסמך: את המבנה שלו פותחים ב-outline, ועמודים ב-pages")
    elif not isinstance(value, dict):
        raise ToolError("pages חייב לכלול document, from_page ו-to_page")
    with tenant_tx(ws.ctx) as conn:
        if kind == "source":
            s = _read_source(ws, conn, value)
        elif kind == "pages":
            v = _document(conn, ws, value.get("document"))
            try:
                first, last = int(value["from_page"]), int(value["to_page"])
            except (KeyError, TypeError, ValueError):
                raise ToolError("טווח עמודים לא תקין: נדרשים from_page ו-to_page") from None
            if first < 1 or last < first:
                raise ToolError("טווח עמודים לא תקין: from_page מ-1 ומעלה, ו-to_page לא קטן ממנו")
            if not v.is_pdf:
                raise ToolError("למסמך הזה אין עמודים (DOCX): פותחים בו סעיף מתוך outline")
            if v.page_count and first > v.page_count:
                raise ToolError(f"למסמך יש {v.page_count} עמודים")
            last = min(last, v.page_count or last, first + PAGES_MAX - 1)
            s = _read_window(ws, conn, v, ("pages", str(v.version_id), first, last), None)
        elif kind == "section":
            data = _handle(ws, value, "§")
            v = _bound(conn, ws, value, data)
            s = _read_window(ws, conn, v, ("section", str(v.version_id), tuple(data["path"])), None)
        elif kind == "table":
            data = _handle(ws, value, "T")
            s = _read_table(ws, conn, _bound(conn, ws, value, data), data["table_index"])
        else:
            data = _handle(ws, value, "K")
            v = _bound(conn, ws, value, data)
            if data["target"][0] == "table":
                s = _read_table(ws, conn, v, data["target"][2], data["pos"])
            else:
                s = _read_window(ws, conn, v, data["target"], data["pos"])
    return _render_source(s)


def _paginate(page, total: int, size: int) -> tuple[int, int]:
    """(the requested page, the number of pages) for ``total`` items, ``size`` per page; refuses a page out of
    range."""
    pages = math.ceil(total / size)
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        raise ToolError("מספר עמוד לא תקין") from None
    if page < 1 or (pages and page > pages):
        raise ToolError(f"אין עמוד {page}; יש {pages} עמודים")
    return page, pages


_LISTED_ID = re.compile(r"(?:D\d+\s+)?document_id=[0-9a-fA-F-]+\s*\|\s*")


def _listing(ws: Workspace, title: str, lines: list[str], documents: list[tuple], *, key: tuple, criterion: str,
             total: int, page: int, pages: int) -> str:
    """A page of documents as a source of the turn: a count or a list over the set cites it (S#), and is
    checked against it like any passage. It has no document of its own; the documents it names are kept for the
    permission checks of the answer that cites it, and the set it is a page of (``key``: the tool and its query)
    for the coverage of that answer."""
    # the source keeps the text without the document ids: their hex digits are no numbers of the set
    docs = [{"document_id": str(d), "title": t} for d, t in documents]
    s = ws.add_source(document_id=None, version_id=None, title=title, section=None, location="רשימת מסמכים",
                      kind="listing", text="\n".join(_LISTED_ID.sub("", ln) for ln in lines),
                      listed=[d["document_id"] for d in docs],
                      listing={"key": key, "criterion": criterion, "total": total, "page": page, "pages": pages,
                               "documents": docs})
    return (f'<source id="{s.sid}" kind="listing" title="{_attr(title)}">\n' + "\n".join(_txt(ln) for ln in lines)
            + f"\n</source>\nאפשר לצטט את הרשימה הזו ({s.sid}) לספירה או לרשימה של המסמכים בתחום.")


def new_scope(query: str, documents, pages: int) -> dict:
    """The turn's scope: the set a question is about (``documents`` as (id, title) pairs), read page by page."""
    return {"query": query, "matching": [{"document_id": str(i), "title": t} for i, t in documents],
            "pages": pages, "pages_read": set()}


def _page_line(page: int, pages: int, total: int, what: str) -> str:
    more = f" — יש עוד; לקבלת הבאים: page={page + 1}" if page < pages else ""
    return f"עמוד {page} מתוך {max(pages, 1)}; סה\"כ {total} {what}{more}"


def tool_list_documents(ws: Workspace, query: str | None = None, page: int | None = None) -> str:
    """Visible documents with their reading state, a page at a time. Without a title query the listing is the
    whole repository the user sees, and it becomes the turn's scope (all of it), read page by page."""
    words = [w for w in re.findall(r"[\w\"״׳'-]+", query or "") if len(w) >= 2][:6]
    with tenant_tx(ws.ctx) as conn:
        params: dict = {"e": EXTRACTION_VERSION}
        cond = ""
        if words:
            params["p"] = [f"%{w.lower()}%" for w in words]
            cond = " AND lower(d.title) LIKE ANY(:p)"
        total = conn.execute(text("SELECT count(*) FROM documents d JOIN document_versions v ON v.document_id = d.id"
                                  f" AND v.is_current WHERE d.deleted_at IS NULL{cond}"), params).scalar_one()
        page, pages = _paginate(page, total, LIST_MAX)
        rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, v.status, v.page_count, v.pages_incomplete, v.ingestion,"
            " v.created_at, r.state AS mstate FROM documents d JOIN document_versions v ON v.document_id = d.id"
            " AND v.is_current LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            f" WHERE d.deleted_at IS NULL{cond} ORDER BY d.title, d.id LIMIT {LIST_MAX} OFFSET :o"),
            params | {"o": (page - 1) * LIST_MAX}).all()
        if not words:
            # the whole repository is the scope only when no set was looked up (find_documents keeps precedence)
            if ws.scope is None:
                everything = conn.execute(text(
                    "SELECT d.id, d.title FROM documents d JOIN document_versions v ON v.document_id = d.id"
                    " AND v.is_current WHERE d.deleted_at IS NULL ORDER BY d.title, d.id")).all()
                ws.scope = new_scope("", [(r.id, r.title) for r in everything], pages)
            if ws.scope["query"] == "":
                ws.scope["pages_read"].add(page)
    if not rows:
        return "לא נמצאו מסמכים" + (f' שכותרתם כוללת "{query}"' if query else "") + "."
    criterion = (f'מסמכים שכותרתם כוללת את אחת המילים "{_txt(query)}"' if words
                 else "המסמכים שהמשתמש מורשה לראות")
    lines = [f"תחום: {criterion}", _page_line(page, pages, total, "מסמכים")]
    for r in rows:
        partial, details = reading_notes(r.ingestion or {}, r.pages_incomplete)
        status = "נקרא חלקית" if partial else "נקרא במלואו"
        mstate = {"done": "חולצו", "partial": "חולצו חלקית", "failed": "החילוץ נכשל", "pending": "בתהליך"}.get(
            r.mstate or "", "טרם חולצו")
        lines.append(f'- {ws.doc_handle(r.id)} document_id={r.id} | "{_txt(r.title)}" | עיבוד: {r.status} | קריאה: {status}'
                     + (f" ({'; '.join(details)})" if details else "") + f" | נתונים כמותיים: {mstate}")
    return _listing(ws, "רשימת מסמכים", lines, [(r.id, r.title) for r in rows], key=("list_documents", " ".join(words)),
                    criterion=criterion, total=total, page=page, pages=pages)


def tool_find_documents(ws: Workspace, query: str, page: int | None = None) -> str:
    """The set a question is about: every visible document containing all the terms that define it (in its title
    or text). The server keeps the whole set as the turn's scope; the model reads it a page at a time."""
    from app.platform.search import _scope_terms, documents_matching

    query = (query or "").strip()
    if len(query) < 2:
        raise ToolError("שאילתת תחום ריקה")
    with tenant_tx(ws.ctx) as conn:
        found = documents_matching(conn, query)
    total = len(found)
    page, pages = _paginate(page, total, SCOPE_PAGE)
    if ws.scope is None or ws.scope.get("query") != query:
        ws.scope = new_scope(query, [(d["document_id"], d["title"]) for d in found], pages)
    ws.scope["pages_read"].add(page)
    for d in found[(page - 1) * SCOPE_PAGE:page * SCOPE_PAGE]:
        ws.touch(d["document_id"], d["title"], "located")
    if not found:
        return f'לא נמצאו מסמכים שמכילים את כל המונחים של "{query}". אפשר לנסות מונחים אחרים או פחות מונחים.'
    shown = found[(page - 1) * SCOPE_PAGE:page * SCOPE_PAGE]
    n_terms = len(_scope_terms(query))
    in_title = sum(1 for d in found if len(d["in_title"]) == n_terms)
    lines = [f'תחום: מסמכים שמכילים את כל המונחים "{_txt(query)}" (בכותרת או בתוכן)',
             _page_line(page, pages, total, "מסמכים מתאימים"),
             f"מתוכם {in_title} שכל המונחים בכותרתם; השאר מזכירים אותם בתוכן בלבד.",
             "אלה כל המסמכים בתחום; תשובה על התחום צריכה לבדוק את כולם או לומר אילו לא נבדקו."]
    for d in shown:
        why = []
        if d["in_title"]:
            why.append("בכותרת: " + ", ".join(d["in_title"]))
        if d["in_text"]:
            why.append(f"בתוכן: {d['hits']} קטעים")
        lines.append(f'- {ws.doc_handle(d["document_id"])} document_id={d["document_id"]} | "{_txt(d["title"])}" | '
                     + "; ".join(why))
    if page < pages:
        lines.append(f"הרשימה חלקית: זה עמוד {page} מתוך {pages}.")
    return _listing(ws, f'מסמכים בתחום "{query}"', lines, [(d["document_id"], d["title"]) for d in shown],
                    key=("find_documents", query), criterion=f'מסמכים שמכילים את כל המונחים "{query}"', total=total,
                    page=page, pages=pages)


OUTLINE_MAX = 150  # sections listed in one outline


def tool_outline(ws: Workspace, document: str) -> str:
    """A document's sections (sub-sections included) and tables as handles ``read`` opens, each with its pages,
    its size and its unread regions."""
    with tenant_tx(ws.ctx) as conn:
        v = _document(conn, ws, document)
        sections, tables = reader.outline(conn, v.version_id)
    ws.touch(v.document_id, v.title, "located", v.partial)
    d = ws.doc_handle(v.document_id)
    head = (f'מבנה המסמך "{_txt(v.title)}" ({d}; ' + (f"{v.page_count} עמודים; " if v.page_count else "")
            + f"גרסה {v.version_id}; קריאה {_txt(v.reading_id or '')}):")
    if not sections:
        return head + "\nלמסמך לא נשמרו קטעים לפי מבנה; אפשר לחפש בו (search עם document_ids)."
    out = [head]
    if v.partial:
        out.append("המסמך נקרא חלקית: בסעיפים שמסומנים בהם אזורים שלא נקראו ייתכן מידע שלא נקרא.")
    out.append("סעיפים:")
    for e in sections[:OUTLINE_MAX]:
        h = _section_handle(ws, v, e.path)
        extra = (f", {e.unread} אזורים שלא נקראו" if e.unread else "") + (
            f", {e.uncertain} קטעים בקריאה לא ודאית" if e.uncertain else "")
        pages = _pages_label(sorted(e.pages)) + ", " if e.pages else ""
        out.append("  " * max(len(e.path) - 1, 0) + f"- {h} «{_txt(_section_name(e.path))}» — {pages}"
                   f"{e.chars:,} תווים{extra}")
    if len(sections) > OUTLINE_MAX:
        out.append(f"(ועוד {len(sections) - OUTLINE_MAX} סעיפים; אפשר לפתוח עמודים ב-pages)")
    if tables:
        out.append("טבלאות:")
        for t in tables:
            h = _table_handle(ws, v.document_id, v.version_id, v.reading_id, t["table_index"])
            where = (f"עמוד {t['page']}, " if t["page"] else "")
            out.append(f"- {h} «{_txt(t['caption'] or 'טבלה')}» — {where}{t['rows']} שורות"
                       + (f", בסעיף «{_txt(t['section'])}»" if t["section"] else ""))
    out.append("פתיחה: read עם section=§# או table=T#; עמודים: read עם pages (document=" + d + ").")
    return "\n".join(out)


# --- measurements and computation ----------------------------------------------------------------------------

def _compat_key(r) -> tuple:
    return (r.metric_kind, r.unit, r.period, r.vat, (r.area_basis or "").strip(), r.value_role)


def _describe_key(key: tuple) -> str:
    kind, unit, period, vat, basis, role = key
    parts = [KIND_LABELS.get(kind, kind), UNIT_LABELS.get(unit, unit or ""), PERIOD_LABELS.get(period, ""),
             VAT_LABELS.get(vat, ""), f"בסיס שטח: {basis}" if basis else "בסיס שטח לא צוין",
             ROLE_LABELS.get(role, role)]
    return ", ".join(p for p in parts if p)


def tool_find_measurements(ws: Workspace, query: str, metric_kinds: list[str] | None = None,
                           document_ids: list[str] | None = None, value_roles: list[str] | None = None,
                           page: int | None = None) -> str:
    words = [w for w in re.findall(r"[֐-׿\w\"״׳']+", query or "") if len(w) >= 2]
    with tenant_tx(ws.ctx) as conn:
        docs = _visible_documents(conn, document_ids or [])
        params: dict = {"e": EXTRACTION_VERSION}
        conds = ["v.is_current", "d.deleted_at IS NULL", "m.status <> 'rejected'"]
        if docs:
            params["d"] = docs
            conds.append("m.document_id = ANY(:d)")
        if metric_kinds:
            params["k"] = list(metric_kinds)
            conds.append("m.metric_kind = ANY(:k)")
        if value_roles:
            params["r"] = list(value_roles)
            conds.append("m.value_role = ANY(:r)")
        if words and not metric_kinds:
            params["w"] = [f"%{w}%" for w in words]
            conds.append("(m.metric ILIKE ANY(:w) OR m.quote ILIKE ANY(:w) OR m.subject ILIKE ANY(:w))")
        where = (" FROM measurements m JOIN document_versions v ON v.id = m.version_id"
                 " JOIN documents d ON d.id = m.document_id WHERE " + " AND ".join(conds))
        total = conn.execute(text("SELECT count(*)" + where), params).scalar_one()
        page, pages = _paginate(page, total, MEASUREMENTS_MAX)
        # a total order (the id breaks ties), so no row falls between two pages
        rows = conn.execute(text(
            "SELECT m.*, d.title, v.ingestion->>'reading_id' AS reading_id" + where + " ORDER BY d.title, d.id, m.block_index NULLS FIRST,"
            f" m.row_index NULLS FIRST, m.id LIMIT {MEASUREMENTS_MAX} OFFSET :o"),
            params | {"o": (page - 1) * MEASUREMENTS_MAX}).all()
        scope_rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, r.state, COALESCE((v.ingestion->>'partial')::boolean, false) AS partial"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            " LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            " WHERE d.deleted_at IS NULL" + (" AND d.id = ANY(:d)" if docs else "")), params).all()
    key = (" ".join(words) if not metric_kinds else "", tuple(sorted(metric_kinds or [])),
           tuple(sorted(str(d) for d in docs)), tuple(sorted(value_roles or [])))
    listing = ws.listings.setdefault(key, {"pages": pages, "pages_read": set(), "total": total})
    listing["pages_read"].add(page)
    groups: dict[tuple, list] = {}
    for r in rows:
        m = ws.add_measurement(r)
        m.listings.add(key)
        ws.touch(r.document_id, r.title, "read")
        groups.setdefault(_compat_key(r), []).append(m)
    cov = {"documents": len(scope_rows), "extracted": sum(1 for s in scope_rows if s.state == "done"),
           "total": total, "page": page, "pages": pages,
           "partial_extraction": [s.title for s in scope_rows if s.state == "partial"],
           "not_extracted": [s.title for s in scope_rows if s.state not in ("done", "partial")],
           "partially_read": [s.title for s in scope_rows if s.partial]}
    ws.coverage.append(cov)
    out = [f"כיסוי: {cov['extracted']} מתוך {cov['documents']} מסמכים בתחום חולצו לנתונים כמותיים.",
           _page_line(page, pages, total, "נתונים מתאימים")]
    if cov["not_extracted"]:
        out.append("טרם חולצו (נתוניהם לא נכללים כאן; אפשר לחפש בהם בטקסט): "
                   + "; ".join(_txt(t) for t in cov["not_extracted"]))
    if cov["partial_extraction"]:
        out.append("חולצו חלקית: " + "; ".join(_txt(t) for t in cov["partial_extraction"]))
    if cov["partially_read"]:
        out.append("מסמכים שנקראו חלקית (תמונות שלא נקראו): " + "; ".join(_txt(t) for t in cov["partially_read"]))
    if not rows:
        out.append("לא נמצאו נתונים כמותיים מתאימים.")
        return "\n".join(out)
    if pages > 1:
        out.append("חישוב על כל הנתונים המתאימים דורש לקרוא את כל העמודים; חישוב על חלקם יסומן כחלקי.")
    for key, ms in groups.items():
        out.append(f"\nקבוצה [{_describe_key(key)}] — {len(ms)} ערכים:")
        for m in ms:
            r = m.row
            status = {"verified": "מאומת", "corrected": "תוקן ידנית", "auto_validated": "ראשוני",
                      "needs_review": "ממתין לבדיקה"}.get(r.status, r.status)
            extra = f" | נושא: {_txt(r.subject)}" if r.subject else ""
            issues = f" | הערות: {_txt('; '.join(r.issues))}" if r.issues else ""
            if anchor_lost(r):
                issues += " | המיקום במסמך אבד בעיבוד מחדש: הערך אינו מאומת מול הקריאה הנוכחית של המסמך"
            out.append(f'  {m.mid}: {_txt(r.metric)} = {_txt(r.value_text)} | מסמך: "{_txt(m.title)}"{extra}'
                       f" | סטטוס: {status}{issues}\n    ציטוט: {_txt(_clip(r.quote, 300))}")
    return "\n".join(out)


# --- values, assumptions and calculation (KTD10) ---------------------------------------------------------------

VALUE_KINDS = {**KIND_LABELS, "income": "הכנסות", "profit": "רווח"}
_SIGNED = re.compile(r"([-−]\s*)?(\()?\s*(\d[\d,]*(?:\.\d+)?)\s*(\))?")
MSG_VALUE_UNAVAILABLE = ("{ids}: המסמך שממנו נלקח הערך אינו זמין עוד (נמחק, או שאין הרשאה אליו); אי אפשר להשתמש בו "
                         "בחישוב")
MSG_VALUE_STALE = ("{ids}: המסמך שממנו נלקח הערך עובד מחדש מאז שנלקח; הערך אינו תקף עוד. יש לקרוא את המקור מחדש "
                   "ולקחת את הערך שוב")


def _parse_number(raw: str) -> tuple[str, Decimal] | None:
    """A number as written, tolerant of ₪, %, NBSP, spaces, thousands commas and a sign: ("12,450,000", 12450000).
    None when it holds no number or more than one."""
    s = meaning._norm(str(raw or "")).replace(" ", " ").replace(" ", " ")
    found = [m for m in _SIGNED.finditer(s) if m.group(3)]
    if len(found) != 1:
        return None
    m = found[0]
    try:
        value = Decimal(m.group(3).replace(",", ""))
    except ArithmeticError:
        return None
    if m.group(1) or (m.group(2) and m.group(4)):  # "-5" / "(5)": a negative amount
        value = -value
    return m.group(3), value


def _numbers_of(text_: str) -> list[tuple[str, int, int, Decimal]]:
    """Every number written in a text: (as written, start, end, value)."""
    from app.answering.verify import _NUMBER

    out = []
    for m in _NUMBER.finditer(text_ or ""):
        try:
            out.append((m.group(0), m.start(), m.end(), Decimal(m.group(0).rstrip(".,").replace(",", ""))))
        except ArithmeticError:
            continue
    return out


def _key(s) -> str:
    """A row label or a column header for matching: one spelling of the abbreviation marks, one space."""
    return " ".join(meaning._norm(str(s or "")).replace(" ", " ").split()).strip(" :")


def _match_one(wanted: str, options: list[str], what: str) -> int:
    """The index of the option a label names: an exact match, else the only option that contains it."""
    k = _key(wanted)
    exact = [i for i, o in enumerate(options) if _key(o) == k]
    if len(exact) == 1:
        return exact[0]
    near = exact or [i for i, o in enumerate(options) if k and k in _key(o)]
    if len(near) == 1:
        return near[0]
    if not near:
        raise ToolError(f"{what} «{wanted}» לא נמצא בטבלה. האפשרויות: " + "; ".join(f"«{o}»" for o in options[:20] if o))
    raise ToolError(f"{what} «{wanted}» מתאים לכמה ({', '.join(str(i + 1) for i in near[:8])}); בחר לפי מספר")


def _table_of(ws: Workspace, src: Source, given) -> int:
    """The table a cell locator names: the source's own table, or the T# / table S# given (of the same table)."""
    index = src.table_index
    if given:
        g = str(given).strip()
        h = ws.handles.get(g)
        if h is not None and h["kind"] == "T":
            if h["version_id"] != str(src.version_id):
                raise ToolError(f"{g} היא טבלה של מסמך אחר מזה של {src.sid}")
            named = h["table_index"]
        elif g in ws.sources and ws.sources[g].table_index is not None:
            if ws.sources[g].version_id != src.version_id:
                raise ToolError(f"{g} הוא מקור של מסמך אחר מזה של {src.sid}")
            named = ws.sources[g].table_index
        else:
            raise ToolError(f"טבלה לא מוכרת: {g}. נדרש T# או S# של טבלה שנקראה בתור הזה")
        if index is not None and named != index:
            raise ToolError(f"{src.sid} אינו מהטבלה {g}: קח את התא מהמקור שבו הטבלה נקראה")
        index = named
    if index is None:
        raise ToolError(f"{src.sid} אינו טבלה: פתח את הטבלה (read עם table=T#) וקח את התא מהמקור שהוחזר, או צטט "
                        "משפט (quote) שבו המספר כתוב")
    return index


def _take_cell(ws: Workspace, conn: Connection, src: Source, full: str, loc: dict) -> dict:
    """The number in the cell at a named row and column of the source's table, verified against the table's stored
    structure; a number the model names that is in another row or column is refused, with where it is."""
    index = _table_of(ws, src, loc.get("table"))
    st = reader.table_structure(conn, src.version_id, index)
    if st is None:
        raise ToolError("הטבלה לא נמצאה בקריאה הנוכחית של המסמך")
    rows = [list(r.get("cells") or []) for r in st.get("rows") or []]
    headers = list(st.get("headers") or [])
    if not rows:
        raise ToolError("לטבלה אין שורות")
    if loc.get("row_number") is not None:
        ri = int(loc["row_number"]) - 1
        if not 0 <= ri < len(rows):
            raise ToolError(f"לטבלה {len(rows)} שורות; row_number מ-1 עד {len(rows)}")
        if loc.get("row") and _key(loc["row"]) not in (_key(c) for c in rows[ri]):
            raise ToolError(f"שורה {ri + 1} אינה «{loc['row']}»: היא «{' | '.join(rows[ri])}»")
    elif loc.get("row"):
        labels = [r[0] if r else "" for r in rows]
        try:
            ri = _match_one(loc["row"], labels, "השורה")
        except ToolError:
            # a label in another cell of the row (a table whose label column is not the first)
            hits = [i for i, r in enumerate(rows) if any(_key(c) == _key(loc["row"]) and _parse_number(c) is None
                                                         for c in r)]
            if len(hits) != 1:
                raise
            ri = hits[0]
    else:
        raise ToolError("תא בטבלה נבחר לפי שורה (row: התווית, או row_number) ועמודה (column: הכותרת, או "
                        "column_number)")
    cells = rows[ri]
    if loc.get("column_number") is not None:
        ci = int(loc["column_number"]) - 1
        if not 0 <= ci < len(cells):
            raise ToolError(f"בשורה {len(cells)} עמודות; column_number מ-1 עד {len(cells)}")
    elif loc.get("column"):
        if not any(headers):
            raise ToolError("לטבלה אין כותרות עמודה: בחר עמודה לפי column_number")
        ci = _match_one(loc["column"], headers, "העמודה")
        if ci >= len(cells):
            raise ToolError(f"בשורה «{cells[0] if cells else ''}» אין תא בעמודה «{headers[ci]}»")
    else:
        raise ToolError("חסרה עמודה: column (הכותרת) או column_number")
    cell = cells[ci]
    header = headers[ci] if ci < len(headers) else ""
    numbers = _numbers_of(meaning._norm(cell))
    where = f"שורה «{cells[0] if cells else ri + 1}», עמודה «{header or ci + 1}»"
    if loc.get("number"):
        wanted = _parse_number(loc["number"])
        if wanted is None:
            raise ToolError(f"number «{loc['number']}» אינו מספר אחד")
        hit = [n for n in numbers if n[3] == wanted[1]]
        if not hit:
            elsewhere = [(r, c) for r, row in enumerate(rows) for c, x in enumerate(row)
                         if any(n[3] == wanted[1] for n in _numbers_of(meaning._norm(x)))]
            if elsewhere:
                r, c = elsewhere[0]
                at = f"שורה «{rows[r][0] if rows[r] else r + 1}», עמודה «{headers[c] if c < len(headers) else c + 1}»"
                raise ToolError(f"המספר {loc['number']} נמצא בטבלה ב{at}, לא ב{where} שנבחרו (שם כתוב «{cell}»). ערך "
                                "נלקח רק מהשורה והעמודה שהוא שייך להן")
            raise ToolError(f"המספר {loc['number']} אינו בתא שנבחר ({where}: «{cell}») ואינו בטבלה")
        written, value = hit[0][0], hit[0][3]
    else:
        if len(numbers) != 1:
            raise ToolError(f"בתא שנבחר ({where}) " + ("אין מספר" if not numbers else f"יש כמה מספרים («{cell}»); "
                                                       "ציין number"))
        written, value = numbers[0][0], numbers[0][3]
        sign = _parse_number(cell)
        if sign is not None and sign[1] == -value:
            value = -value
    forms = frozenset(numbers_in(written))
    if not forms & numbers_in(full):
        raise ToolError(f"הערך {written} ({where}) אינו בטקסט של {src.sid}: קרא את חלק הטבלה שבו הוא נמצא "
                        "(read עם table=T# או cursor) וקח אותו מהמקור הזה")
    line = " | ".join(cells)
    label = cells[0] if cells else ""
    column_units = st.get("units") or []
    near = [cell, label, header, str(column_units[ci]) if ci < len(column_units) and column_units[ci] else ""]
    table_text = [st.get("caption") or "", *(st.get("title") or []), *(st.get("notes") or [])]
    # the unit of the cell, its row or its column; the table's caption or notes only when those state none
    units = meaning.units_attested(" ".join(near)) or meaning.units_attested(" ".join(table_text))
    return {"written": written, "value": value, "forms": forms, "quote": line,
            "qualifiers": meaning.number_qualifiers(_table_text(st, st.get("rows") or []), forms, line),
            "units": units, "vat": meaning.vat_attested("\n".join(near + table_text), forms),
            "kind_context": " ".join([label, header, st.get("caption") or ""]),
            "locator": {"table_index": index, "row": cells[0] if cells else "", "row_number": ri + 1,
                        "column": header, "column_number": ci + 1},
            "total": bool(calc.TOTAL_WORDS.search(meaning._norm(cells[0] if cells else ""))),
            "table": (str(src.version_id), index)}


def _take_quote(src: Source, full: str, loc: dict) -> dict:
    """The number inside an exact quote of the source: the quote must occur in the source's full text, and the
    number inside the quote."""
    if not loc.get("number"):
        raise ToolError("ציטוט (quote) דורש גם את המספר כפי שנכתב בו (number)")
    wanted = _parse_number(loc["number"])
    if wanted is None:
        raise ToolError(f"number «{loc['number']}» אינו מספר אחד")
    quote = " ".join(meaning._norm(loc["quote"]).split())
    text_ = " ".join(meaning._norm(full).split())
    if len(quote) < 3 or quote not in text_:
        raise ToolError(f"הציטוט אינו מופיע כלשונו ב-{src.sid}: העתק את המשפט מהמקור בדיוק (או בחר תא בטבלה)")
    if " | " in quote:
        raise ToolError("הציטוט הוא שורת טבלה: קח את הערך כתא (row ו-column), כדי שהשרת יבדוק לאיזו עמודה הוא שייך")
    hit = [n for n in _numbers_of(quote) if n[3] == abs(wanted[1])]
    if not hit:
        if any(n[3] == abs(wanted[1]) for n in _numbers_of(text_)):
            raise ToolError(f"המספר {loc['number']} נמצא ב-{src.sid} אבל לא בתוך הציטוט: צטט את המשפט שבו הוא כתוב")
        raise ToolError(f"המספר {loc['number']} אינו בציטוט ואינו ב-{src.sid}")
    written, start, end, value = hit[0]
    at = text_.index(quote)
    local = text_[max(0, at + start - 8):at + end + 16]
    forms = frozenset(numbers_in(written))
    line = next((ln for ln in meaning._norm(full).split("\n") if quote[:40] in " ".join(ln.split())), quote)
    sign = -1 if wanted[1] < 0 else 1
    units = meaning.units_attested(local)
    if units == {"ILS"} and meaning._PER_SQM.search(quote):
        # "השווי למ״ר ... 9,500 ₪": a per-area amount whose "למ״ר" is not next to it; either reading is the source's
        units = {"ILS", "ILS_per_sqm"}
    return {"written": written, "value": sign * value, "forms": forms, "quote": loc["quote"].strip(),
            "qualifiers": meaning.number_qualifiers(full, forms, quote),
            "units": units, "vat": meaning.vat_attested(line, forms, full),
            "kind_context": quote, "locator": {"quote": loc["quote"].strip()}, "total": False, "table": None}


def _settle_meaning(taken: dict, given: dict) -> tuple[dict, dict]:
    """The value's meaning: what the source attests about the number is the source's (and fills what the model left
    unknown); what it does not is the model's (``model_asserted``); a contradiction is refused."""
    written = taken["written"]
    q: meaning.Qualifiers = taken["qualifiers"]
    out = dict(given)
    prov: dict[str, str] = {}
    units = taken["units"]
    if units and given["unit"] not in units:
        raise ToolError(f"המקור נותן ל-{written} יחידה {', '.join(UNIT_LABELS.get(u, u) for u in sorted(units))}, "
                        f"לא {UNIT_LABELS.get(given['unit'], given['unit']) or given['unit']}")
    prov["unit"] = "source" if units else "model_asserted"
    periods = q.keys("period")
    if periods:
        if given["period"] == "unknown" and len(periods) == 1:
            out["period"] = next(iter(periods))
        elif given["period"] not in periods:
            raise ToolError(f"המקור נותן ל-{written} תקופה "
                            + " / ".join(PERIOD_LABELS.get(p, p) for p in sorted(periods))
                            + f", לא {PERIOD_LABELS.get(given['period'], given['period']) or given['period']}")
        prov["period"] = "source"
    else:
        prov["period"] = "model_asserted" if given["period"] in ("month", "year") else "not_stated"
    vats = taken["vat"]
    if vats:
        if given["vat"] in ("unknown", "not_applicable") and len(vats) == 1:
            out["vat"] = next(iter(vats))
        elif given["vat"] not in vats:
            raise ToolError(f"המקור נותן ל-{written} " + " / ".join(VAT_LABELS[v] for v in sorted(vats))
                            + f", לא {VAT_LABELS.get(given['vat']) or given['vat']}")
        prov["vat"] = "source"
    else:
        prov["vat"] = "model_asserted" if given["vat"] in ("included", "excluded") else "not_stated"
    bases = q.found.get("basis", {})
    said = (given.get("area_basis") or "").strip()
    if bases:
        keys = meaning.Qualifiers()
        for _, kind, key, w in meaning._scan(meaning._norm(said)):
            keys.add(kind, key, w)
        if not said:
            out["area_basis"] = meaning.display(" ".join(bases.values()))
        elif not keys.keys("basis") & set(bases):
            raise ToolError(f"המקור נותן ל-{written} בסיס שטח «{meaning.display(' '.join(bases.values()))}», לא «{said}»")
        else:
            out["area_basis"] = meaning.display(" ".join(bases.values()))
        prov["area_basis"] = "source"
    else:
        prov["area_basis"] = "model_asserted" if said else "not_stated"
    prov["kind"] = "source" if meaning.kind_attested(given["kind"], taken["kind_context"]) else "model_asserted"
    return out, prov


def tool_take_value(ws: Workspace, source: str, locator: dict | None, meaning_: dict | None, label: str = "") -> str:
    """Register a value of a source the turn read (V#) once the server verified that the number is the one the
    locator names: the cell at that row and column of the table, or a number inside an exact quote."""
    sid = (source or "").strip()
    src = ws.sources.get(sid)
    if src is None:
        raise ToolError(f"מקור לא מוכר: {source}. ערך נלקח רק ממקור S# שהוחזר בתור הזה (search או read)")
    if src.is_listing or src.version_id is None:
        raise ToolError("רשימת מסמכים אינה מקור לערך: קח את הערך ממקור במסמך")
    full = src.text or (ws.sources[src.same_as].text if src.same_as in ws.sources else "")
    loc = {k: v for k, v in (locator or {}).items() if v not in (None, "")}
    given = dict(meaning_ or {})
    for name, allowed in (("kind", VALUE_KINDS), ("unit", UNIT_LABELS), ("period", PERIOD_LABELS),
                          ("vat", VAT_LABELS), ("role", calc.ROLE_LABELS)):
        if given.get(name) not in allowed:
            raise ToolError(f"meaning.{name} חייב להיות אחד מ: " + ", ".join(allowed))
    cell = any(k in loc for k in ("table", "row", "row_number", "column", "column_number"))
    if cell == ("quote" in loc):
        raise ToolError("locator: תא בטבלה (row או row_number, ו-column או column_number; אפשר גם table) או ציטוט "
                        "(quote ו-number) — אחד מהם בלבד")
    with tenant_tx(ws.ctx) as conn:
        v = reader.version(conn, src.version_id)
        if v is None:
            raise ToolError(MSG_UNAVAILABLE)
        if src.reading_id is not None and v.reading_id != src.reading_id:
            raise ToolError(MSG_STALE_REF.format(source_id=sid))
        taken = _take_cell(ws, conn, src, full, loc) if cell else _take_quote(src, full, loc)
    fields, prov = _settle_meaning(taken, given)
    approx = "approx" in taken["qualifiers"].keys("approx")
    total = taken["total"] or given["role"] == "total"
    where = taken["locator"]
    default = (f"{where['row']} — {where['column']}" if "row" in where
               else f"{VALUE_KINDS[fields['kind']]} {fields.get('subject') or ''}".strip())
    value = calc.Value(f"V{len(ws.values) + 1}", taken["value"], taken["written"], sid, src.document_id,
                       src.version_id, src.reading_id or v.reading_id, src.title, src.location,
                       (label or "").strip() or default, fields["kind"], fields["unit"], fields["period"],
                       fields["vat"], fields.get("area_basis") or "", (fields.get("subject") or "").strip(),
                       fields["role"], prov, where, taken["quote"], total, taken["table"], approx)
    ws.values[value.vid] = value
    shown = [UNIT_LABELS.get(value.unit, ""), PERIOD_LABELS.get(value.period, ""), VAT_LABELS.get(value.vat, ""),
             f"בסיס שטח: {value.area_basis}" if value.area_basis else ""]
    place = (f"שורה «{where['row']}» (מס' {where['row_number']}), עמודה «{where['column']}»" if "row" in where
             else f"ציטוט «{_clip(value.quote, 200)}»")
    asserted = [{"unit": "יחידה", "period": "תקופה", "vat": "מע\"מ", "area_basis": "בסיס שטח", "kind": "סוג"}[k]
                for k, p in prov.items() if p == "model_asserted"]
    lines = [f"{value.vid} נרשם: «{_txt(value.label)}» = {value.written} ({'; '.join(x for x in shown if x) or 'ללא יחידה'})"
             f" | סוג: {VALUE_KINDS[value.kind]} | תפקיד: {calc.ROLE_LABELS[value.role]}"
             + (f" | נושא: {_txt(value.subject)}" if value.subject else "") + (" | שורת סה\"כ" if total else ""),
             f"אומת ב-{sid}: {_txt(place)}"]
    if approx:
        lines.append("המקור כותב את הערך כמקורב.")
    lines.append("ודאות: " + ("כל התכונות שצוינו נמצאו במקור" if not asserted else
                              "נמוכה יותר — תכונות שהמודל קבע ולא נמצאו במקור: " + ", ".join(asserted)))
    return "\n".join(lines)


def tool_assume(ws: Workspace, value: str, quote: str, label: str = "") -> str:
    """Register a number the user gave for a scenario (A#): quoted from a user message visible to the turn — the
    current one or an earlier one, never an answer's or a document's text."""
    wanted = _parse_number(value)
    if wanted is None:
        raise ToolError("value חייב להיות מספר אחד כפי שהמשתמש כתב אותו (למשל 5%)")
    q = " ".join(meaning._norm(quote or "").split())
    if len(q) < 2:
        raise ToolError("quote חייב להיות ציטוט מדויק מהודעת המשתמש")
    found = None
    for msg in [m for m in ws.user_messages if m["current"]] + [m for m in reversed(ws.user_messages)
                                                                if not m["current"]]:
        if q in " ".join(meaning._norm(msg["text"]).split()):
            found = msg
            break
    if found is None:
        raise ToolError("הציטוט אינו מופיע בהודעות המשתמש בשיחה הזו. הנחה היא רק מספר שהמשתמש עצמו כתב (לא מתשובה "
                        "קודמת ולא ממסמך). אם התרחיש דורש מספר שהמשתמש לא נתן — שאל את המשתמש")
    hit = [n for n in _numbers_of(q) if n[3] == abs(wanted[1])]
    if not hit:
        raise ToolError(f"הערך {value} אינו בתוך הציטוט «{_txt(quote)}». הנחה נרשמת רק כפי שהמשתמש כתב אותה; אם "
                        "הערך לא נכתב — שאל את המשתמש")
    written, _, end, number = hit[0]
    after = q[end:end + 8]
    raw = meaning._norm(value)
    unit = ("percent" if "%" in raw or after.lstrip().startswith(("%", "אחוז")) else
            "ILS" if re.match(r"\s*(?:₪|ש\"ח|שקל)", after) else
            "sqm" if re.match(r"\s*מ\"ר", after) else "ratio")
    a = calc.Assumption(f"A{len(ws.assumptions) + 1}", number if wanted[1] >= 0 else -number, written, unit,
                        (label or "").strip() or "הנחת המשתמש", (quote or "").strip(), found["turn"], found["current"])
    ws.assumptions[a.aid] = a
    turn = "מההודעה הנוכחית" if a.current else "מהודעה קודמת"
    return (f"{a.aid} נרשם: «{_txt(a.label)}» = {written}{'%' if unit == 'percent' else ''} — הנחת המשתמש, מצוטטת "
            f"{turn}: «{_txt(a.quote)}». בחישוב: {a.aid}% הוא {a.aid}/100. בתשובה הצג אותה כהנחת המשתמש, בנפרד "
            "מנתוני המסמך, וצטט [" + a.aid + "].")


def _operand(ws: Workspace, i: str) -> calc.Operand:
    if i in ws.values:
        return ws.values[i].operand()
    if i in ws.assumptions:
        return ws.assumptions[i].operand()
    if i in ws.computations:
        return calc.result_operand(i, ws.computations[i].outcome)
    if i in ws.measurements:
        m = ws.measurements[i]
        r = m.row
        return calc.operand(i, r.value, r.unit, period=r.period, vat=r.vat, basis=meaning.basis_key(r.area_basis),
                            kind=r.metric_kind, subject=r.subject or "",
                            total=bool(calc.TOTAL_WORDS.search(meaning._norm(r.metric or ""))),
                            table=(str(m.version_id), r.table_index) if r.table_index is not None else None,
                            group=r.value_role, same=str(m.id), approx=r.value_form != "exact")
    raise ToolError(f"מזהה לא מוכר: {i}. אפשר להשתמש רק במזהים שנרשמו בתור הזה: V# (take_value), A# (assume), "
                    "M# (find_measurements), C# (calculate)")


def _leaves(ws: Workspace, ids) -> list[str]:
    """The registered values and measurements an expression rests on, through earlier results."""
    out: list[str] = []
    for i in ids:
        if i in ws.computations:
            out += ws.computations[i].leaves
        elif i not in out:
            out.append(i)
    return list(dict.fromkeys(out))


def _check_available(ws: Workspace, leaves: list[str]) -> None:
    """Every document a value rests on is still visible, in the reading the value was taken from (R13)."""
    versions: dict[str, list[str]] = {}
    for i in leaves:
        vid = (ws.values[i].version_id if i in ws.values else ws.measurements[i].version_id if i in ws.measurements
               else None)
        if vid is not None:
            versions.setdefault(str(vid), []).append(i)
    if not versions:
        return
    with tenant_tx(ws.ctx) as conn:
        for vid, ids in versions.items():
            v = reader.version(conn, UUID(vid))
            if v is None:
                raise ToolError(MSG_VALUE_UNAVAILABLE.format(ids=", ".join(ids)))
            stale = [i for i in ids if i in ws.values and ws.values[i].reading_id is not None
                     and ws.values[i].reading_id != v.reading_id]
            if stale:
                raise ToolError(MSG_VALUE_STALE.format(ids=", ".join(stale)))


def _reproduces(ws: Workspace, out: calc.Outcome, leaves: list[str]) -> dict | None:
    """A number the report writes that the result equals at the precision it is written in, in the turn's sources of
    the documents the inputs come from (not one of the inputs themselves)."""
    versions = {ws.values[i].version_id for i in leaves if i in ws.values}
    versions |= {ws.measurements[i].version_id for i in leaves if i in ws.measurements}
    inputs = set()
    for i in leaves:
        if i in ws.values:
            inputs |= numbers_in(ws.values[i].written)
        elif i in ws.measurements:
            inputs |= numbers_in(ws.measurements[i].row.value_text or "")
    texts = [(s.sid, s.text) for s in ws.sources.values() if s.version_id in versions and not s.is_listing]
    texts += [(i, ws.measurements[i].row.quote or "") for i in leaves if i in ws.measurements]
    for sid, t in texts:
        for written, _, end, _ in _numbers_of(t):
            if numbers_in(written) & inputs or len(re.sub(r"\D", "", written).lstrip("0")) < 3:
                continue
            percent = t[end:end + 2].lstrip().startswith("%")
            if calc.display_matches(written, percent, out.value, out.dims):
                return {"source": sid, "as_written": written + ("%" if percent else "")}
    return None


def tool_calculate(ws: Workspace, expression: str, label: str = "", justification: str | None = None) -> str:
    """Evaluate an expression over the turn's ids (``app.chat.calc``) and register the result as a C#."""
    try:
        node = calc.parse(expression)
    except calc.CalcError as e:
        raise ToolError(f"ביטוי לא תקין: {e}") from None
    ids = list(dict.fromkeys(calc.ids_of(node)))
    operands = {i: _operand(ws, i) for i in ids}
    leaves = _leaves(ws, ids)
    _check_available(ws, leaves)
    justification = (justification or "").strip() or None
    try:
        out = calc.evaluate(node, operands, justification)
    except calc.CalcError as e:
        raise ToolError(f"אי אפשר לחשב: {e}") from None

    def name(i: str) -> str:
        if i in ws.values:
            return ws.values[i].label
        if i in ws.assumptions:
            return ws.assumptions[i].label
        if i in ws.computations:
            return ws.computations[i].label
        return ws.measurements[i].row.metric if i in ws.measurements else i

    inputs = []
    for i in out.inputs:
        kind = ("value" if i in ws.values else "assumption" if i in ws.assumptions else
                "computation" if i in ws.computations else "measurement")
        value = (ws.values[i].value if i in ws.values else ws.assumptions[i].value if i in ws.assumptions
                 else ws.computations[i].value if i in ws.computations else ws.measurements[i].row.value)
        entry = {"id": i, "label": name(i), "kind": kind, "value": None if value is None else str(value),
                 "display": None if value is None else calc.fmt(Decimal(str(value)))}
        if i in ws.values:
            entry |= {"source_id": ws.values[i].source_id, "value_text": ws.values[i].written,
                      "certainty": ws.values[i].certainty}
        elif i in ws.assumptions:
            entry |= {"quote": ws.assumptions[i].quote, "value_text": ws.assumptions[i].written}
        elif i in ws.measurements:
            entry |= {"value_text": ws.measurements[i].row.value_text}
        inputs.append(entry)
    sources = list(dict.fromkeys([ws.values[i].source_id for i in leaves if i in ws.values]
                                 + [i for i in leaves if i in ws.measurements]))
    docs = {str(ws.values[i].document_id) for i in leaves if i in ws.values}
    docs |= {str(ws.measurements[i].document_id) for i in leaves if i in ws.measurements}
    notes = []
    if out.approx:
        notes.append("חלק מהערכים מקורבים; התוצאה מקורבת בהתאם.")
    asserted = [i for i in leaves if i in ws.values and ws.values[i].certainty != "verified"]
    if asserted:
        notes.append("ודאות נמוכה יותר: תכונות של " + ", ".join(asserted) + " נקבעו ולא נמצאו במקור.")
    lost = [i for i in leaves if i in ws.measurements and anchor_lost(ws.measurements[i].row)]
    if lost:
        notes.append("מיקומם של חלק מהערכים במסמך אבד בעיבוד מחדש (" + ", ".join(lost)
                     + "); הם אינם מאומתים מול הקריאה הנוכחית.")
    # values chosen from a listing whose later pages were not read: more matching values may exist
    unread = [ws.listings[k] for k in {k for i in leaves if i in ws.measurements for k in ws.measurements[i].listings}
              if len(ws.listings[k]["pages_read"]) < ws.listings[k]["pages"]]
    if unread:
        notes.append("חישוב חלקי: לא נקראו כל העמודים של הנתונים המתאימים (נקראו "
                     f"{len(unread[0]['pages_read'])} מתוך {unread[0]['pages']}).")
    pending = [i for i in leaves if i in ws.measurements
               and ws.measurements[i].row.status in ("auto_validated", "needs_review")]
    if pending:
        notes.append(f"{len(pending)} מהערכים טרם אומתו על ידי אדם (נתון ראשוני).")
    conditional = ("מותנה: " + "; ".join(out.conditional) + f" — לפי ההצדקה: {justification}"
                   if out.conditional else "")
    reproduces = None if out.assumptions else _reproduces(ws, out, leaves)
    kind = "scenario" if out.assumptions else "reproduces_report_value" if reproduces else "computed"
    c = calc.Computation(f"C{len(ws.computations) + 1}", (label or "").strip() or calc.render(node, name, True),
                         calc.render(node, lambda i: i), calc.render(node, lambda i: f"«{name(i)}»", True), out,
                         inputs, sources, len(docs), kind, justification if out.conditional else None, " ".join(notes),
                         reproduces, [lf.id for lf in out.leaves])
    ws.computations[c.cid] = c
    return json.dumps({"id": c.cid, "label": c.label, "expression": c.expression, "formula": c.formula,
                       "value": str(c.value), "display": c.display(), "unit": c.unit_label,
                       "result_kind": calc.RESULT_KINDS[kind], "reproduces": reproduces, "conditional": c.conditional,
                       "inputs": [x["id"] for x in inputs], "assumptions": c.assumptions, "n": out.n,
                       "documents": c.documents, "note": " ".join(x for x in (c.note, conditional) if x),
                       "how_to_show": f"הצג את התוצאה מעוגלת (display) וצטט [{c.cid}]; הנחות המשתמש בנפרד עם [A#]"},
                      ensure_ascii=False)


# --- registry ----------------------------------------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_NULLABLE_IDS = {"type": ["array", "null"], "items": {"type": "string"}}

TOOLS = [
    _fn("search", "חיפוש קטעים במסמכי המשרד (טקסט, טבלאות, שורות טבלה וטקסט מתוך תמונות). מחזיר מקורות S#.",
        {"query": {"type": "string", "description": "שאילתה בעברית, במילים שעשויות להופיע במסמך"},
         "document_ids": {**_NULLABLE_IDS, "description": "הגבלה למסמכים מסוימים, או null לכל המאגר"},
         "limit": {"type": ["integer", "null"], "description": "מספר תוצאות (3-12), null לברירת מחדל"}},
        ["query", "document_ids", "limit"]),
    _fn("read",
        "פתיחת מקום במסמך בלי צורך בחיפוש קודם. בוחרים מיקום אחד בדיוק ב-target (השאר null): source — S# מהתור הזה "
        "או P# מתור קודם (הפסקאות סביבו; קטע מטבלה — הטבלה כולה); pages — עמודים של מסמך PDF; section — סעיף §# "
        "מ-outline (כולל תתי-הסעיפים); table — טבלה T# (כותרות, שורות והערות); cursor — K# להמשך קריאה (more). "
        "כל תוצאה מציינת status: complete, clipped (יש המשך ב-more), has_unread_regions (אזורים שלא נקראו, R#) או "
        "uncertain_reading.",
        {"target": {"type": "object", "additionalProperties": False,
                    "required": list(LOCATORS),
                    "properties": {
                        "source": {"type": ["string", "null"], "description": "S# או P#"},
                        "pages": {"type": ["object", "null"], "additionalProperties": False,
                                  "required": ["document", "from_page", "to_page"],
                                  "properties": {"document": {"type": "string", "description": "D# או document_id"},
                                                 "from_page": {"type": "integer"}, "to_page": {"type": "integer"}}},
                        "section": {"type": ["string", "null"], "description": "§# מ-outline"},
                        "table": {"type": ["string", "null"], "description": "T# מ-outline או מתוצאת חיפוש"},
                        "cursor": {"type": ["string", "null"], "description": "K# מ-more של קריאה קודמת"}}}},
        ["target"]),
    _fn("find_documents",
        "התחום של שאלה על קבוצת מסמכים (סקירה, השוואה, רשימה או חישוב על כמה מסמכים; שאלה \"בעיר/באזור X\"): כל "
        "המסמכים שמכילים את כל המונחים שמגדירים את הקבוצה, בכותרת או בתוכן. מחזיר את כולם בעמודים, עם הסך הכול.",
        {"query": {"type": "string", "description": "רק המונחים שמגדירים את הקבוצה (מקום, סוג מסמך) — לא המדד"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "page"]),
    _fn("list_documents", "רשימת המסמכים הזמינים למשתמש, עם מצב הקריאה שלהם, בעמודים. אפשר לסנן לפי מילים בכותרת.",
        {"query": {"type": ["string", "null"], "description": "מילים בכותרת, או null לכל המסמכים"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}}, ["query", "page"]),
    _fn("outline", "מבנה מסמך: סעיפים (§#) וטבלאות (T#) שאפשר לפתוח ב-read, עם עמודים, גודל ואזורים שלא נקראו.",
        {"document": {"type": "string", "description": "D# או document_id"}}, ["document"]),
    _fn("find_measurements",
        "נתונים כמותיים שחולצו מהמסמכים עם משמעותם (סוג מדד, יחידה, תקופה, בסיס שטח, מע\"מ, תפקיד, נושא), מקובצים "
        "לפי מה שניתן להשוות, עם כיסוי המסמכים. מחזיר מזהי M# לחישוב.",
        {"query": {"type": "string", "description": "תיאור הנתון המבוקש"},
         "metric_kinds": {**_NULLABLE_IDS, "description": "סוגי מדד מתוך: " + ", ".join(KIND_LABELS)},
         "document_ids": _NULLABLE_IDS,
         "value_roles": {**_NULLABLE_IDS, "description": "תפקידים מתוך: " + ", ".join(ROLE_LABELS)},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "metric_kinds", "document_ids", "value_roles", "page"]),
    _fn("take_value",
        "רישום ערך ממקור S# שקראת בתור הזה, אחרי שהשרת מאמת שהמספר הוא זה שבמיקום שבחרת: תא בטבלה (שורה ועמודה; "
        "השאר null) או ציטוט מדויק מהמקור שהמספר בתוכו (quote ו-number; השאר null), עם משמעות הערך. מחזיר V#. מספר "
        "שנמצא בשורה או בעמודה אחרת נדחה עם הסיבה; מה שהמקור מעיד על הערך נרשם כשל המקור, והשאר כקביעה שלך.",
        {"source": {"type": "string", "description": "S# מהתור הזה (טבלה שנקראה, שורת טבלה מחיפוש, או קטע)"},
         "locator": {"type": "object", "additionalProperties": False,
                     "required": ["table", "row", "row_number", "column", "column_number", "quote", "number"],
                     "properties": {
                         "table": {"type": ["string", "null"], "description": "T# או S# של הטבלה (לא חובה)"},
                         "row": {"type": ["string", "null"], "description": "תווית השורה כפי שכתובה בטבלה"},
                         "row_number": {"type": ["integer", "null"], "description": "מספר השורה (מ-1)"},
                         "column": {"type": ["string", "null"], "description": "כותרת העמודה כפי שכתובה"},
                         "column_number": {"type": ["integer", "null"], "description": "מספר העמודה (מ-1)"},
                         "quote": {"type": ["string", "null"], "description": "ציטוט מדויק מהמקור שהמספר בתוכו"},
                         "number": {"type": ["string", "null"], "description": "המספר כפי שנכתב"}}},
         "meaning": {"type": "object", "additionalProperties": False,
                     "required": ["kind", "unit", "period", "vat", "area_basis", "subject", "role"],
                     "properties": {
                         "kind": {"type": "string", "enum": list(VALUE_KINDS)},
                         "unit": {"type": "string", "enum": list(UNIT_LABELS)},
                         "period": {"type": "string", "enum": list(PERIOD_LABELS)},
                         "vat": {"type": "string", "enum": list(VAT_LABELS)},
                         "area_basis": {"type": "string", "description": "בסיס השטח כפי שנכתב, או ריק"},
                         "subject": {"type": "string", "description": "הנכס, השלב, התקופה או מערך הנתונים"},
                         "role": {"type": "string", "enum": list(calc.ROLE_LABELS)}}},
         "label": {"type": "string", "description": "שם קצר בעברית לערך, כפי שיוצג בנוסחה"}},
        ["source", "locator", "meaning", "label"]),
    _fn("assume",
        "רישום מספר שהמשתמש נתן לתרחיש (למשל \"העלויות יעלו ב-5%\") כהנחת משתמש A#, עם ציטוט מדויק מהודעת המשתמש "
        "(הנוכחית או קודמת). לעולם לא מספר מתשובה קודמת או ממסמך; אם המשתמש לא נתן את המספר — שאל אותו.",
        {"value": {"type": "string", "description": "המספר כפי שהמשתמש כתב (למשל 5%)"},
         "quote": {"type": "string", "description": "ציטוט מדויק מהודעת המשתמש שהמספר בתוכו"},
         "label": {"type": "string", "description": "שם קצר בעברית להנחה"}},
        ["value", "quote", "label"]),
    _fn("calculate",
        "חישוב מדויק בקוד על מזהים: V# (take_value), A# (assume), M# (find_measurements), C# (חישוב קודם). מותר: + - * / "
        "(או × ÷), סוגריים, % אחרי ערך (A1% = A1/100), sum/mean/median/min/max/count(מזהים), והקבועים 1, 100, 12 בלבד. "
        "מסרב לחבר יחידות או תקופות שונות, שכירות עם שווי, או סה\"כ עם הרכיבים שלו; ערבוב מע\"מ, בסיסי שטח או נושאים "
        "מותר רק עם justification, והתוצאה מותנית. מחזיר C# בדיוק מלא.",
        {"expression": {"type": "string", "description": "למשל V1 - V2 * (1 + A1%) או mean(M1, M2, M3)"},
         "label": {"type": "string", "description": "שם קצר בעברית לתוצאה"},
         "justification": {"type": ["string", "null"],
                           "description": "רק כשהחישוב מערבב מע\"מ, בסיס שטח או נושא: למה זה תקף; אחרת null"}},
        ["expression", "label", "justification"]),
]

HANDLERS = {
    "search": lambda ws, a: tool_search(ws, a["query"], a.get("document_ids"), a.get("limit")),
    "read": lambda ws, a: tool_read(ws, a["target"]),
    "find_documents": lambda ws, a: tool_find_documents(ws, a["query"], a.get("page")),
    "list_documents": lambda ws, a: tool_list_documents(ws, a.get("query"), a.get("page")),
    "outline": lambda ws, a: tool_outline(ws, a.get("document") or a.get("document_id")),
    "find_measurements": lambda ws, a: tool_find_measurements(ws, a["query"], a.get("metric_kinds"),
                                                              a.get("document_ids"), a.get("value_roles"),
                                                              a.get("page")),
    "take_value": lambda ws, a: tool_take_value(ws, a["source"], a.get("locator"), a.get("meaning"), a.get("label") or ""),
    "assume": lambda ws, a: tool_assume(ws, a["value"], a["quote"], a.get("label") or ""),
    "calculate": lambda ws, a: tool_calculate(ws, a["expression"], a.get("label") or "", a.get("justification")),
}


def run_tool(ws: Workspace, name: str, arguments: str) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        return f"כלי לא קיים: {name}"
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "ארגומנטים לא תקינים (JSON)"
    try:
        return handler(ws, args)
    except ToolError as e:
        return f"שגיאה: {e}"
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("tool %s rejected its arguments: %s", name, type(e).__name__)
        return "שגיאה: ארגומנטים לא תקינים לכלי"
    except Exception:  # noqa: BLE001 - a failing tool is reported to the model; the turn goes on
        logger.exception("tool %s failed", name)
        return "שגיאה: הכלי נכשל. אפשר לנסות שוב או לנסות דרך אחרת"
