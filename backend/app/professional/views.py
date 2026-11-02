"""What the document screens show of a standard or a professional document (round 10 U8; U10 renders it): the metadata it
states about itself with the page and quote of each value, the state of its derivations, the editions of its number with
whether their order is known, and its copies.

Read-only and permission-safe: every query runs in the caller's transaction, so the editions, copies and links a user is
shown are those of documents they may see, and a document they may not see is neither listed nor linked. Nothing is
derived here — a document whose structure or profile is not derived yet is ``pending`` until the answer tools, or the
worker, derive it (``sections.ensure``, ``profile.ensure_profile``)."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import Connection, text

from app.platform.source_kinds import PROFESSIONALS_SQL
from app.professional import catalog, profile, sections

State = Literal["pending", "ready", "failed"]
SUMMARY_FIELDS = (*profile.FIELDS, "amends")

_SQL = (
    "SELECT d.id AS document_id, v.id AS version_id, v.ingestion, p.fields FROM documents d"
    " JOIN document_versions v ON v.document_id = d.id AND v.is_current"
    " LEFT JOIN standard_profiles p ON p.version_id = v.id AND p.reading_id = COALESCE(v.ingestion->>'reading_id', '')"
    f" AND p.profile_version = '{profile.PROFILE_VERSION}'"
    f" WHERE d.id = ANY(:ids) AND d.deleted_at IS NULL AND {PROFESSIONALS_SQL}")


def field_summary(field: dict | None) -> dict:
    """One profile field as the screen shows it: the value (None: the document does not state it) with its status, how it
    was found, and where — the page, the block and the verbatim quote."""
    f = field or profile.UNKNOWN
    source = f.get("source") or {}
    out = {"value": f.get("value"), "status": f.get("status", "unknown"), "method": f.get("method"),
           "page": source.get("page"), "block": source.get("block"), "quote": source.get("quote")}
    for extra in ("normalized", "relation", "standard"):
        if extra in f:
            out[extra] = f[extra]
    return out


def derivation_state(ingestion: dict, has_profile: bool) -> State:
    """``failed`` when a derivation failed (``professional_error``), ``ready`` when the sections of the current reading and
    the profile are both stored, else ``pending``."""
    if ingestion.get("professional_error"):
        return "failed"
    done = ingestion.get("sections_derived") or {}
    sections_ok = (done.get("reading_id"), done.get("version")) == (str(ingestion.get("reading_id") or ""),
                                                                    sections.SECTIONS_VERSION)
    return "ready" if sections_ok and has_profile else "pending"


def summaries(conn: Connection, document_ids: list) -> dict[str, dict]:
    """document id -> ``{state, profile, editions, duplicates}`` for the visible standards and professional documents
    among ``document_ids`` (appraisals and documents the caller may not see have no entry)."""
    if not document_ids:
        return {}
    rows = conn.execute(text(_SQL), {"ids": list(document_ids)}).all()
    if not rows:
        return {}
    found = catalog.entries(conn, derive=False)
    heads, later = catalog.canonical(found), catalog.copies(found)
    loaded = profile.visible_editions(conn)
    head_loaded = ([e for e in loaded[0] if e.version_id not in heads or heads[e.version_id].version_id == e.version_id],
                   loaded[1])
    by_version = {e.version_id: e for e in found}
    keyed = profile.by_key(head_loaded[0])  # every row's group is read from the same editions
    out: dict[str, dict] = {}
    for r in rows:
        ing = r.ingestion or {}
        fields = r.fields or {}
        own = by_version.get(r.version_id)
        head = heads.get(r.version_id)
        key = profile.normalize_number((fields.get("number") or {}).get("value")) if fields else None
        group = profile.group_of(head_loaded, key, keyed) if key else None
        out[str(r.document_id)] = {
            "state": derivation_state(ing, r.fields is not None),
            "profile": {name: field_summary(fields.get(name)) for name in SUMMARY_FIELDS} if r.fields is not None else None,
            "editions": None if group is None else {
                "number": group.number, "order_known": group.order_known,
                "editions": [{"document_id": str(e.document_id), "title": e.title, "edition": e.edition,
                              "published": e.published_text, "effective": e.effective_text,
                              "current": e.document_id == r.document_id} for e in group.editions],
                "related": [{"document_id": str(e.document_id), "title": e.title,
                             "relation": (e.amends or {}).get("relation")} for e in group.related]},
            "duplicates": {
                "same_text_as": None if head is None or own is None or head.version_id == own.version_id else
                {"document_id": str(head.document_id), "title": head.title},
                "copies": [{"document_id": str(c.document_id), "title": c.title} for c in later.get(r.version_id, [])]},
        }
    return out
