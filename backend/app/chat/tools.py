"""The tools the answering model may call, and the evidence they produce.

The model never runs SQL and never sees anything the user may not: every tool runs in its own short
transaction under the user's tenant context (RLS decides what is visible), validates its arguments, and returns
plain text the model reads. Every passage a tool returns is registered as a source with an id (``S1``...); every
stored measurement as ``M1``...; every computation as ``C1``.... Every tool also records how deep it reached
into each document — located (listed in a set or outlined), a passage retrieved, a section or table read (or its
measurements listed) — and which sections and tables it opened, so the server can state an answer's coverage
itself; a document whose datum the verified answer cites is ``verified`` (``coverage.build``). An
answer may cite only ids issued in the same turn, so a previous answer is never a source: an earlier turn's
passages are offered as ``P`` references the model must re-open (``open_source``), which reads them again under
the current permissions.

Tools:

- ``search``: hybrid retrieval (lexical, trigram, semantic) over text, tables, table rows and picture text,
  optionally within named documents;
- ``open_source``: the context around a source — neighbouring paragraphs, the whole section, or the whole
  table with its caption, header and notes;
- ``find_documents``: every document that contains all the terms that define a set (a place, a document type),
  in its title or text — the scope of a question about a set of documents, paged, with the total;
- ``list_documents``: visible documents with their reading status (fully read, partial: what was not read), paged;
- ``outline``: a document's section headings;
- ``find_measurements``: stored measurements with their meaning (kind, unit, period, area basis, VAT, role,
  subject), grouped by what can be compared, with the coverage of the documents in scope, paged;
- ``compute``: mean / median / sum / min / max / count / difference / ratio over measurements, in exact
  decimal code, refusing to mix kinds, units, periods, VAT status, area bases or roles.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat.evidence import TABLE_SIZE_PREFIX
from app.db import TenantContext, tenant_tx
from app.measurements.extract import EXTRACTION_VERSION, PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
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

    @property
    def is_listing(self) -> bool:
        return self.kind == "listing"

    def public(self) -> dict:
        out = {"id": self.sid, "document_id": str(self.document_id) if self.document_id else None,
               "version_id": str(self.version_id) if self.version_id else None,
               "title": self.title, "section": self.section, "location": self.location, "kind": self.kind,
               "text": self.text, "block_start": self.block_start, "block_end": self.block_end,
               "table_index": self.table_index, "page_list": self.page_list}
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
                "table_index": r.table_index}


@dataclass
class Computation:
    cid: str
    operation: str
    result: Decimal | None
    unit_label: str
    measurement_ids: list[str]
    documents: int
    note: str

    def public(self) -> dict:
        return {"id": self.cid, "operation": self.operation, "result": None if self.result is None else str(self.result),
                "unit": self.unit_label, "inputs": self.measurement_ids, "documents": self.documents, "note": self.note}


@dataclass
class Workspace:
    """Everything one turn gathered: sources, measurements, computations, and earlier-turn references."""

    ctx: TenantContext
    sources: dict[str, Source] = field(default_factory=dict)
    measurements: dict[str, Measurement] = field(default_factory=dict)
    computations: dict[str, Computation] = field(default_factory=dict)
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
                                                        "partial": False, "openings": []})
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
    return (f'<source id="{s.sid}" document_id="{s.document_id}" title="{_attr(s.title)}" location="{_attr(s.location)}"'
            f' kind="{s.kind}">{flag}\n{_txt(s.text)}\n</source>')


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
            out.append(ws.once(ws.add_source(
                document_id=h["document_id"], version_id=h["version_id"], title=h["title"], section=h["section"],
                location=_location(h["section"], h["kind"], h["page_list"], bs, be, paragraphs, media),
                kind=h["kind"], text=_with_size(sizes.get((h["version_id"], h.get("table_index"))),
                                                _clip(h["text"], PASSAGE_CHARS)), block_start=bs, block_end=be,
                table_index=h.get("table_index"), chunk_id=h["chunk_id"], page_list=h["page_list"] or None,
                partial_document=h["version_id"] in partial), ("chunk", h["chunk_id"])))
            ws.touch(h["document_id"], h["title"], "retrieved", h["version_id"] in partial)
    if not out:
        return f'לא נמצאו קטעים עבור "{query}". אפשר לנסות ניסוח אחר, מונחים נרדפים או חיפוש בתוך מסמך מסוים.'
    return "\n\n".join(_render_source(s) for s in out)


def _ref(ws: Workspace, source_id: str) -> dict:
    sid = (source_id or "").strip()
    if sid in ws.sources:
        s = ws.sources[sid]
        return {"version_id": s.version_id, "block_start": s.block_start, "block_end": s.block_end,
                "table_index": s.table_index, "chunk_id": s.chunk_id}
    if sid in ws.prior:
        return ws.prior[sid]
    raise ToolError(f"מזהה מקור לא מוכר: {source_id}. אפשר לפתוח רק מקורות שהוחזרו בתור הזה (S#) או הפניות מתורות קודמות (P#).")


def tool_open_source(ws: Workspace, source_id: str, scope: str = "neighbors") -> str:
    s = read_scope(ws, source_id, scope)
    # keyed by the text sent: a section opened after the paragraphs around the same place is longer, and is sent
    return _render_source(ws.once(s, ("text", s.version_id, s.text)))


def read_scope(ws: Workspace, source_id: str, scope: str = "neighbors", quiet: bool = False,
               ref: dict | None = None) -> Source:
    """The context around a source (``neighbors``), its whole section, or its whole table, as a new source of the
    turn. ``quiet``: read for the server's own checks (``app.chat.meaning``): the source is not registered and the
    turn's coverage does not change — the caller registers it (``Workspace.adopt``) only if it cites it."""
    if scope not in CONTEXT_CHARS:
        raise ToolError("scope חייב להיות neighbors, section או table")
    ref = ref or _ref(ws, source_id)

    def make(**kw) -> Source:
        return Source(sid="", **kw) if quiet else ws.add_source(**kw)

    def touch(*args, **kw) -> None:
        if not quiet:
            ws.touch(*args, **kw)

    vid = UUID(str(ref["version_id"]))
    with tenant_tx(ws.ctx) as conn:
        head = conn.execute(text(
            "SELECT v.id, v.document_id, d.title FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " AND d.deleted_at IS NULL WHERE v.id = :v"), {"v": vid}).first()
        if head is None:
            raise ToolError("המקור אינו זמין עוד (נמחק או שאין הרשאה)")
        partial = bool(_partial_versions(conn, [vid]))
        touch(head.document_id, head.title, "retrieved", partial)
        start, end = ref.get("block_start"), ref.get("block_end")
        table_index = ref.get("table_index")
        if start is None and ref.get("chunk_id") and not (scope == "table" and table_index is not None):
            row = conn.execute(text("SELECT text, section, page_list, kind FROM chunks WHERE id = :c"),
                               {"c": ref["chunk_id"]}).first()
            if row is None:
                raise ToolError("המקור אינו זמין עוד")
            s = make(document_id=head.document_id, version_id=vid, title=head.title, section=row.section,
                              location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                              kind=row.kind, text=_clip(row.text, CONTEXT_CHARS[scope]), page_list=row.page_list,
                              partial_document=partial)
            return s
        if scope == "table":
            if table_index is None and start is not None:
                hit = conn.execute(text(
                    "SELECT table_index FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a AND :b"
                    " AND table_index IS NOT NULL ORDER BY block_index LIMIT 1"),
                    {"v": vid, "a": start, "b": end if end is not None else start}).first()
                table_index = hit.table_index if hit else None
            if table_index is None:
                raise ToolError("למקור הזה אין טבלה; אפשר לפתוח neighbors או section")
            t = conn.execute(text("SELECT structure FROM extracted_tables WHERE version_id = :v AND table_index = :t"),
                             {"v": vid, "t": table_index}).first()
            if t is None:
                raise ToolError("הטבלה לא נמצאה")
            st = t.structure or {}
            lines = [x for x in [st.get("caption"), *(st.get("title") or []), table_size_line(st)] if x]
            if any(st.get("headers") or []):
                lines.append(" | ".join(st["headers"]))
            lines += [" | ".join(r.get("cells") or []) for r in st.get("rows") or []]
            lines += st.get("notes") or []
            media = st.get("media")
            source_note = {"emf": "טבלה מתוך תמונה וקטורית (נקראה במדויק)", "vision": "טבלה מתוך תמונה (קריאה חזותית)",
                           "ocr": "טבלה מתוך תמונה (OCR)"}.get(st.get("source") or "", "")
            body = (source_note + "\n" if source_note else "") + "\n".join(lines)
            s = make(document_id=head.document_id, version_id=vid, title=head.title, section=st.get("section"),
                              location=_location(st.get("section"), "table", None, None, None, (None, None), media),
                              kind="table", text=_clip(body, CONTEXT_CHARS["table"]), block_start=st.get("block_index"),
                              block_end=st.get("block_index"), table_index=table_index, partial_document=partial)
            touch(head.document_id, head.title, "read", partial,
                     {"sid": s.sid, "scope": "table", "name": st.get("caption") or (st.get("title") or [""])[0]
                      or st.get("section") or "טבלה"})
            return s
        if start is None:
            raise ToolError("למקור הזה אין מיקום במסמך להרחבה")
        if scope == "section":
            sec = conn.execute(text("SELECT section, section_path FROM document_blocks WHERE version_id = :v"
                                    " AND block_index = :b"), {"v": vid, "b": start}).first()
            top = (sec.section_path[0] if sec and sec.section_path else None)
            rows = conn.execute(text(
                "SELECT block_index, kind, text, section, paragraph_no, media FROM document_blocks WHERE version_id = :v"
                " AND (section_path[1] IS NOT DISTINCT FROM :top) ORDER BY block_index"), {"v": vid, "top": top}).all()
        else:
            rows = conn.execute(text(
                "SELECT block_index, kind, text, section, paragraph_no, media FROM document_blocks WHERE version_id = :v"
                " AND block_index BETWEEN :a AND :b ORDER BY block_index"),
                {"v": vid, "a": max(0, start - 3), "b": (end if end is not None else start) + 3}).all()
        rows = [r for r in rows if r.text]
        if not rows:
            raise ToolError("אין טקסט בהקשר הזה")
        body = "\n".join(r.text for r in rows)
        nums = [r.paragraph_no for r in rows if r.paragraph_no]
        section = rows[0].section if scope == "neighbors" else (top or rows[0].section)
        s = make(document_id=head.document_id, version_id=vid, title=head.title, section=section,
                          location=_location(section, "text", None, rows[0].block_index, rows[-1].block_index,
                                             (min(nums), max(nums)) if nums else (None, None), None),
                          kind="context", text=_clip(body, CONTEXT_CHARS[scope]), block_start=rows[0].block_index,
                          block_end=rows[-1].block_index, partial_document=partial)
        if scope == "section":
            touch(head.document_id, head.title, "read", partial,
                     {"sid": s.sid, "scope": "section", "name": section or "הסעיף"})
        return s


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


_LISTED_ID = re.compile(r"document_id=[0-9a-fA-F-]+\s*\|\s*")


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
        ing = r.ingestion or {}
        images = ing.get("images") or {}
        unread = images.get("unread", 0)
        uncertain = images.get("read_uncertain", 0)
        status = "נקרא במלואו" if not (unread or r.pages_incomplete) else "נקרא חלקית"
        details = []
        if unread:
            details.append(f"{unread} תמונות לא נקראו")
        if uncertain:
            details.append(f"{uncertain} תמונות נקראו בקריאה לא ודאית")
        if r.pages_incomplete:
            details.append(f"{r.pages_incomplete} עמודים לא נקראו")
        mstate = {"done": "חולצו", "partial": "חולצו חלקית", "failed": "החילוץ נכשל", "pending": "בתהליך"}.get(
            r.mstate or "", "טרם חולצו")
        lines.append(f'- document_id={r.id} | "{_txt(r.title)}" | עיבוד: {r.status} | קריאה: {status}'
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
        lines.append(f'- document_id={d["document_id"]} | "{_txt(d["title"])}" | ' + "; ".join(why))
    if page < pages:
        lines.append(f"הרשימה חלקית: זה עמוד {page} מתוך {pages}.")
    return _listing(ws, f'מסמכים בתחום "{query}"', lines, [(d["document_id"], d["title"]) for d in shown],
                    key=("find_documents", query), criterion=f'מסמכים שמכילים את כל המונחים "{query}"', total=total,
                    page=page, pages=pages)


def tool_outline(ws: Workspace, document_id: str) -> str:
    with tenant_tx(ws.ctx) as conn:
        (doc,) = _visible_documents(conn, [document_id])
        v = conn.execute(text("SELECT v.id, d.title FROM document_versions v JOIN documents d ON d.id = v.document_id"
                              " WHERE v.document_id = :d AND v.is_current"), {"d": doc}).first()
        if v is None:
            raise ToolError("למסמך אין גרסה זמינה")
        rows = conn.execute(text("SELECT block_index, text, section_path FROM document_blocks WHERE version_id = :v"
                                 " AND kind = 'heading' ORDER BY block_index"), {"v": v.id}).all()
    if not rows:
        return f'למסמך "{_txt(v.title)}" לא זוהו כותרות סעיפים.'
    return f'מבנה המסמך "{_txt(v.title)}":\n' + "\n".join(
        "  " * (len(r.section_path) - 1) + f"- {_txt(r.text)}" for r in rows)


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
            "SELECT m.*, d.title" + where + " ORDER BY d.title, d.id, m.block_index NULLS FIRST,"
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
            out.append(f'  {m.mid}: {_txt(r.metric)} = {_txt(r.value_text)} | מסמך: "{_txt(m.title)}"{extra}'
                       f" | סטטוס: {status}{issues}\n    ציטוט: {_txt(_clip(r.quote, 300))}")
    return "\n".join(out)


OPERATIONS = ("mean", "median", "sum", "min", "max", "count", "difference", "ratio")


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if value != value.to_integral() else value


def tool_compute(ws: Workspace, operation: str, measurement_ids: list[str]) -> str:
    if operation not in OPERATIONS:
        raise ToolError("פעולה לא נתמכת. אפשרויות: " + ", ".join(OPERATIONS))
    ids = list(dict.fromkeys(measurement_ids or []))
    unknown = [i for i in ids if i not in ws.measurements]
    if unknown:
        raise ToolError("מזהי נתונים לא מוכרים: " + ", ".join(unknown) + ". יש לאתר אותם קודם ב-find_measurements.")
    if not ids:
        raise ToolError("לא נבחרו נתונים לחישוב")
    ms = list({ws.measurements[i].id: ws.measurements[i] for i in ids}.values())  # each stored value once
    ids = [m.mid for m in ms]
    keys = {_compat_key(m.row) for m in ms}
    if len(keys) > 1:
        lines = ["אי אפשר לחשב: הנתונים שנבחרו אינם מאותו סוג, ולכן חישוב משותף יטעה. הקבוצות:"]
        for k in keys:
            lines.append(f"- [{_describe_key(k)}]: " + ", ".join(m.mid for m in ms if _compat_key(m.row) == k))
        lines.append("אפשר לחשב בנפרד לכל קבוצה, או להסביר למשתמש מדוע הנתונים אינם ברי השוואה.")
        return "\n".join(lines)
    if any(m.row.value is None for m in ms) and operation != "count":
        ranges = [m.mid for m in ms if m.row.value is None]
        return ("אי אפשר לחשב: חלק מהנתונים הם טווחים ולא ערך יחיד (" + ", ".join(ranges)
                + "). אפשר לחשב על הערכים היחידים בלבד או להציג את הטווחים.")
    values = [Decimal(str(m.row.value)) for m in ms if m.row.value is not None]
    key = next(iter(keys))
    unit_label = UNIT_LABELS.get(key[1], "")
    note = ""
    if operation == "mean":
        result = sum(values) / len(values)
    elif operation == "median":
        result = Decimal(str(statistics.median(values)))
    elif operation == "sum":
        if key[0] in ("value_per_area", "price_per_area", "rent_per_area", "management_fee_per_area",
                      "cost_per_area", "rate", "coefficient"):
            return "אי אפשר לסכם ערכים ליחידת שטח, שיעורים או מקדמים: סכום כזה אינו בעל משמעות."
        result = sum(values)
    elif operation == "min":
        result = min(values)
    elif operation == "max":
        result = max(values)
    elif operation == "count":
        result = Decimal(len(ms))
        unit_label = "ערכים"
    elif operation in ("difference", "ratio"):
        if len(values) != 2:
            raise ToolError("הפרש ויחס דורשים בדיוק שני נתונים (הראשון פחות/חלקי השני)")
        if operation == "difference":
            result = values[0] - values[1]
        else:
            if values[1] == 0:
                raise ToolError("חלוקה באפס")
            result = values[0] / values[1]
            unit_label = "יחס"
    approx = [m.mid for m in ms if m.row.value_form != "exact"]
    if approx:
        note = "חלק מהערכים מקורבים או גבולות (" + ", ".join(approx) + "); התוצאה מקורבת בהתאם."
    # the values were chosen from a listing whose later pages were not read: more matching values may exist
    unread = [ws.listings[k] for k in {k for m in ms for k in m.listings}
              if len(ws.listings[k]["pages_read"]) < ws.listings[k]["pages"]]
    if unread:
        note += (" " if note else "") + ("חישוב חלקי: לא נקראו כל העמודים של הנתונים המתאימים (נקראו "
                                          f"{len(unread[0]['pages_read'])} מתוך {unread[0]['pages']}).")
    pending = [m.mid for m in ms if m.row.status in ("auto_validated", "needs_review")]
    if pending:
        note += (" " if note else "") + f"{len(pending)} מהערכים טרם אומתו על ידי אדם (נתון ראשוני)."
    docs = len({m.document_id for m in ms})
    c = Computation(f"C{len(ws.computations) + 1}", operation, _q(result), unit_label, ids, docs, note)
    ws.computations[c.cid] = c
    return json.dumps({"id": c.cid, "operation": operation, "result": str(c.result), "unit": unit_label,
                       "kind": _describe_key(key), "n": len(ms), "documents": docs, "note": note}, ensure_ascii=False)


# --- registry ----------------------------------------------------------------------------------------------

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "name": name, "description": description, "strict": True,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}


_IDS = {"type": "array", "items": {"type": "string"}}
_NULLABLE_IDS = {"type": ["array", "null"], "items": {"type": "string"}}

TOOLS = [
    _fn("search", "חיפוש קטעים במסמכי המשרד (טקסט, טבלאות, שורות טבלה וטקסט מתוך תמונות). מחזיר מקורות S#.",
        {"query": {"type": "string", "description": "שאילתה בעברית, במילים שעשויות להופיע במסמך"},
         "document_ids": {**_NULLABLE_IDS, "description": "הגבלה למסמכים מסוימים, או null לכל המאגר"},
         "limit": {"type": ["integer", "null"], "description": "מספר תוצאות (3-12), null לברירת מחדל"}},
        ["query", "document_ids", "limit"]),
    _fn("open_source", "הרחבת ההקשר של מקור: פסקאות סמוכות, כל הסעיף, או הטבלה המלאה עם כותרות והערות.",
        {"source_id": {"type": "string", "description": "S# מהתור הזה או P# מתור קודם"},
         "scope": {"type": "string", "enum": ["neighbors", "section", "table"]}},
        ["source_id", "scope"]),
    _fn("find_documents",
        "התחום של שאלה על קבוצת מסמכים (סקירה, השוואה, רשימה או חישוב על כמה מסמכים; שאלה \"בעיר/באזור X\"): כל "
        "המסמכים שמכילים את כל המונחים שמגדירים את הקבוצה, בכותרת או בתוכן. מחזיר את כולם בעמודים, עם הסך הכול.",
        {"query": {"type": "string", "description": "רק המונחים שמגדירים את הקבוצה (מקום, סוג מסמך) — לא המדד"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "page"]),
    _fn("list_documents", "רשימת המסמכים הזמינים למשתמש, עם מצב הקריאה שלהם, בעמודים. אפשר לסנן לפי מילים בכותרת.",
        {"query": {"type": ["string", "null"], "description": "מילים בכותרת, או null לכל המסמכים"},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}}, ["query", "page"]),
    _fn("outline", "כותרות הסעיפים של מסמך.", {"document_id": {"type": "string"}}, ["document_id"]),
    _fn("find_measurements",
        "נתונים כמותיים שחולצו מהמסמכים עם משמעותם (סוג מדד, יחידה, תקופה, בסיס שטח, מע\"מ, תפקיד, נושא), מקובצים "
        "לפי מה שניתן להשוות, עם כיסוי המסמכים. מחזיר מזהי M# לחישוב.",
        {"query": {"type": "string", "description": "תיאור הנתון המבוקש"},
         "metric_kinds": {**_NULLABLE_IDS, "description": "סוגי מדד מתוך: " + ", ".join(KIND_LABELS)},
         "document_ids": _NULLABLE_IDS,
         "value_roles": {**_NULLABLE_IDS, "description": "תפקידים מתוך: " + ", ".join(ROLE_LABELS)},
         "page": {"type": ["integer", "null"], "description": "מספר עמוד, null לראשון"}},
        ["query", "metric_kinds", "document_ids", "value_roles", "page"]),
    _fn("compute", "חישוב מדויק בקוד על נתונים M# מאותה קבוצה בלבד. מסרב לערבב סוגי מדד, יחידות, תקופות, מע\"מ, "
                   "בסיסי שטח או תפקידים.",
        {"operation": {"type": "string", "enum": list(OPERATIONS)}, "measurement_ids": _IDS},
        ["operation", "measurement_ids"]),
]

HANDLERS = {
    "search": lambda ws, a: tool_search(ws, a["query"], a.get("document_ids"), a.get("limit")),
    "open_source": lambda ws, a: tool_open_source(ws, a["source_id"], a["scope"]),
    "find_documents": lambda ws, a: tool_find_documents(ws, a["query"], a.get("page")),
    "list_documents": lambda ws, a: tool_list_documents(ws, a.get("query"), a.get("page")),
    "outline": lambda ws, a: tool_outline(ws, a["document_id"]),
    "find_measurements": lambda ws, a: tool_find_measurements(ws, a["query"], a.get("metric_kinds"),
                                                              a.get("document_ids"), a.get("value_roles"),
                                                              a.get("page")),
    "compute": lambda ws, a: tool_compute(ws, a["operation"], a["measurement_ids"]),
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
