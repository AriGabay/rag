"""The tools the answering model may call, and the evidence they produce.

The model never runs SQL and never sees anything the user may not: every tool runs in its own short
transaction under the user's tenant context (RLS decides what is visible), validates its arguments, and returns
plain text the model reads. Every passage a tool returns is registered as a source with an id (``S1``...); every
stored measurement as ``M1``...; every computation as ``C1``.... An answer may cite only ids issued in the same
turn, so a previous answer is never a source: an earlier turn's passages are offered as ``P`` references the
model must re-open (``open_source``), which reads them again under the current permissions.

Tools:

- ``search``: hybrid retrieval (lexical, trigram, semantic) over text, tables, table rows and picture text,
  optionally within named documents;
- ``open_source``: the context around a source — neighbouring paragraphs, the whole section, or the whole
  table with its caption, header and notes;
- ``list_documents``: visible documents with their reading status (fully read, partial: what was not read);
- ``outline``: a document's section headings;
- ``find_measurements``: stored measurements with their meaning (kind, unit, period, area basis, VAT, role,
  subject), grouped by what can be compared, with the coverage of the documents in scope;
- ``compute``: mean / median / sum / min / max / count / difference / ratio over measurements, in exact
  decimal code, refusing to mix kinds, units, periods, VAT status, area bases or roles.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.db import TenantContext, tenant_tx
from app.measurements.extract import EXTRACTION_VERSION, PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
from app.platform.search import SearchScope, search_passages

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 8
SEARCH_MAX = 12
PASSAGE_CHARS = 1600
CONTEXT_CHARS = {"neighbors": 3500, "section": 9000, "table": 14000}
MEASUREMENTS_MAX = 120
LIST_MAX = 60

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


class ToolError(Exception):
    """A tool call the server refuses (bad arguments, unknown ids); its message goes back to the model."""


@dataclass
class Source:
    sid: str
    document_id: UUID
    version_id: UUID
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

    def public(self) -> dict:
        return {"id": self.sid, "document_id": str(self.document_id), "version_id": str(self.version_id),
                "title": self.title, "section": self.section, "location": self.location, "kind": self.kind,
                "text": self.text, "block_start": self.block_start, "block_end": self.block_end,
                "table_index": self.table_index, "page_list": self.page_list}


@dataclass
class Measurement:
    mid: str
    id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    row: Any

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

    def _sid(self) -> str:
        return f"S{len(self.sources) + 1}"

    def add_source(self, **kw) -> Source:
        s = Source(sid=self._sid(), **kw)
        self.sources[s.sid] = s
        return s


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


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n].rstrip() + " …[קוצר]"


def _render_source(s: Source) -> str:
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
            out.append(ws.add_source(
                document_id=h["document_id"], version_id=h["version_id"], title=h["title"], section=h["section"],
                location=_location(h["section"], h["kind"], h["page_list"], bs, be, paragraphs, media),
                kind=h["kind"], text=_clip(h["text"], PASSAGE_CHARS), block_start=bs, block_end=be,
                table_index=h.get("table_index"), chunk_id=h["chunk_id"], page_list=h["page_list"] or None,
                partial_document=h["version_id"] in partial))
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
    if scope not in CONTEXT_CHARS:
        raise ToolError("scope חייב להיות neighbors, section או table")
    ref = _ref(ws, source_id)
    vid = UUID(str(ref["version_id"]))
    with tenant_tx(ws.ctx) as conn:
        head = conn.execute(text(
            "SELECT v.id, v.document_id, d.title FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " AND d.deleted_at IS NULL WHERE v.id = :v"), {"v": vid}).first()
        if head is None:
            raise ToolError("המקור אינו זמין עוד (נמחק או שאין הרשאה)")
        partial = bool(_partial_versions(conn, [vid]))
        start, end = ref.get("block_start"), ref.get("block_end")
        table_index = ref.get("table_index")
        if start is None and ref.get("chunk_id"):
            row = conn.execute(text("SELECT text, section, page_list, kind FROM chunks WHERE id = :c"),
                               {"c": ref["chunk_id"]}).first()
            if row is None:
                raise ToolError("המקור אינו זמין עוד")
            s = ws.add_source(document_id=head.document_id, version_id=vid, title=head.title, section=row.section,
                              location=_location(row.section, row.kind, row.page_list, None, None, (None, None), None),
                              kind=row.kind, text=_clip(row.text, CONTEXT_CHARS[scope]), page_list=row.page_list,
                              partial_document=partial)
            return _render_source(s)
        if scope == "table":
            if table_index is None:
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
            lines = [x for x in [st.get("caption"), *(st.get("title") or [])] if x]
            if any(st.get("headers") or []):
                lines.append(" | ".join(st["headers"]))
            lines += [" | ".join(r.get("cells") or []) for r in st.get("rows") or []]
            lines += st.get("notes") or []
            media = st.get("media")
            source_note = {"emf": "טבלה מתוך תמונה וקטורית (נקראה במדויק)", "vision": "טבלה מתוך תמונה (קריאה חזותית)",
                           "ocr": "טבלה מתוך תמונה (OCR)"}.get(st.get("source") or "", "")
            body = (source_note + "\n" if source_note else "") + "\n".join(lines)
            s = ws.add_source(document_id=head.document_id, version_id=vid, title=head.title, section=st.get("section"),
                              location=_location(st.get("section"), "table", None, None, None, (None, None), media),
                              kind="table", text=_clip(body, CONTEXT_CHARS["table"]), block_start=st.get("block_index"),
                              block_end=st.get("block_index"), table_index=table_index, partial_document=partial)
            return _render_source(s)
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
        s = ws.add_source(document_id=head.document_id, version_id=vid, title=head.title, section=section,
                          location=_location(section, "text", None, rows[0].block_index, rows[-1].block_index,
                                             (min(nums), max(nums)) if nums else (None, None), None),
                          kind="context", text=_clip(body, CONTEXT_CHARS[scope]), block_start=rows[0].block_index,
                          block_end=rows[-1].block_index, partial_document=partial)
        return _render_source(s)


def tool_list_documents(ws: Workspace, query: str | None = None) -> str:
    words = [w for w in re.findall(r"[\w\"״׳'-]+", query or "") if len(w) >= 2][:6]
    with tenant_tx(ws.ctx) as conn:
        params: dict = {"e": EXTRACTION_VERSION}
        cond = ""
        if words:
            params["p"] = [f"%{w.lower()}%" for w in words]
            cond = " AND lower(d.title) LIKE ANY(:p)"
        rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, v.status, v.page_count, v.pages_incomplete, v.ingestion,"
            " v.created_at, r.state AS mstate FROM documents d JOIN document_versions v ON v.document_id = d.id"
            " AND v.is_current LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            f" WHERE d.deleted_at IS NULL{cond} ORDER BY d.title LIMIT {LIST_MAX}"), params).all()
    if not rows:
        return "לא נמצאו מסמכים" + (f' שכותרתם כוללת "{query}"' if query else "") + "."
    lines = []
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
    return "\n".join(lines)


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
                           document_ids: list[str] | None = None, value_roles: list[str] | None = None) -> str:
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
        rows = conn.execute(text(
            "SELECT m.*, d.title FROM measurements m JOIN document_versions v ON v.id = m.version_id"
            " JOIN documents d ON d.id = m.document_id WHERE " + " AND ".join(conds)
            + f" ORDER BY d.title, m.block_index, m.row_index LIMIT {MEASUREMENTS_MAX + 1}"), params).all()
        scope_rows = conn.execute(text(
            "SELECT d.id, d.title, v.id AS vid, r.state, COALESCE((v.ingestion->>'partial')::boolean, false) AS partial"
            " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
            " LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            " WHERE d.deleted_at IS NULL" + (" AND d.id = ANY(:d)" if docs else "")), params).all()
    truncated = len(rows) > MEASUREMENTS_MAX
    rows = rows[:MEASUREMENTS_MAX]
    groups: dict[tuple, list] = {}
    known = {m.id: m for m in ws.measurements.values()}
    for r in rows:
        m = known.get(r.id)  # a measurement found again keeps its id, so it is never counted twice
        if m is None:
            m = Measurement(f"M{len(ws.measurements) + 1}", r.id, r.document_id, r.version_id, r.title, r)
            ws.measurements[m.mid] = m
            known[r.id] = m
        groups.setdefault(_compat_key(r), []).append(m)
    cov = {"documents": len(scope_rows), "extracted": sum(1 for s in scope_rows if s.state == "done"),
           "partial_extraction": [s.title for s in scope_rows if s.state == "partial"],
           "not_extracted": [s.title for s in scope_rows if s.state not in ("done", "partial")],
           "partially_read": [s.title for s in scope_rows if s.partial]}
    ws.coverage.append(cov)
    out = [f"כיסוי: {cov['extracted']} מתוך {cov['documents']} מסמכים בתחום חולצו לנתונים כמותיים."]
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
    if truncated:
        out.append(f"(מוצגים {MEASUREMENTS_MAX} הראשונים; צמצם את החיפוש לסוג מדד או למסמכים)")
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
    _fn("list_documents", "רשימת המסמכים הזמינים למשתמש, עם מצב הקריאה שלהם. אפשר לסנן לפי מילים בכותרת.",
        {"query": {"type": ["string", "null"], "description": "מילים בכותרת, או null לכל המסמכים"}}, ["query"]),
    _fn("outline", "כותרות הסעיפים של מסמך.", {"document_id": {"type": "string"}}, ["document_id"]),
    _fn("find_measurements",
        "נתונים כמותיים שחולצו מהמסמכים עם משמעותם (סוג מדד, יחידה, תקופה, בסיס שטח, מע\"מ, תפקיד, נושא), מקובצים "
        "לפי מה שניתן להשוות, עם כיסוי המסמכים. מחזיר מזהי M# לחישוב.",
        {"query": {"type": "string", "description": "תיאור הנתון המבוקש"},
         "metric_kinds": {**_NULLABLE_IDS, "description": "סוגי מדד מתוך: " + ", ".join(KIND_LABELS)},
         "document_ids": _NULLABLE_IDS,
         "value_roles": {**_NULLABLE_IDS, "description": "תפקידים מתוך: " + ", ".join(ROLE_LABELS)}},
        ["query", "metric_kinds", "document_ids", "value_roles"]),
    _fn("compute", "חישוב מדויק בקוד על נתונים M# מאותה קבוצה בלבד. מסרב לערבב סוגי מדד, יחידות, תקופות, מע\"מ, "
                   "בסיסי שטח או תפקידים.",
        {"operation": {"type": "string", "enum": list(OPERATIONS)}, "measurement_ids": _IDS},
        ["operation", "measurement_ids"]),
]

HANDLERS = {
    "search": lambda ws, a: tool_search(ws, a["query"], a.get("document_ids"), a.get("limit")),
    "open_source": lambda ws, a: tool_open_source(ws, a["source_id"], a["scope"]),
    "list_documents": lambda ws, a: tool_list_documents(ws, a.get("query")),
    "outline": lambda ws, a: tool_outline(ws, a["document_id"]),
    "find_measurements": lambda ws, a: tool_find_measurements(ws, a["query"], a.get("metric_kinds"),
                                                              a.get("document_ids"), a.get("value_roles")),
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
