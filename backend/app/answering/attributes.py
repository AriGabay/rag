"""Attribute registry (KTD7): attributes are data, not columns, parser rules or keywords.

The interpreter sees the office's definitions as short handles (A1, A2, ...) and makes the semantic
match itself. The server merges only on an interpreter-chosen handle or on an exact label/alias match
after Hebrew normalization (quotes, niqqud, one- and two-letter prefixes such as the article ה).
Trigram similarity only ranks the candidates shown to the interpreter and never merges: "שטח מגרש"
and "שטח בנוי" stay distinct. Without a match, a ``proposed`` extracted definition is created.

Everything runs inside the caller's tenant transaction, so RLS limits it to the current office.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.extraction.normalize_text import base_normalize, prefix_variants

# Structured attributes map to whitelisted record columns (the CHECK in migration 0004 and
# STRUCTURED_COLUMNS in app.appraisal.query). Single source of truth; scripts/seed_demo.py imports it.
STRUCTURED_ATTRIBUTES = [
    {"key": "price_per_sqm", "label_he": "מחיר למ״ר",
     "aliases": ["מחיר למטר", "מחיר למ\"ר", "מחיר למטר רבוע", "מחיר למטר מרובע", "שווי למ״ר"],
     "unit_dimension": "currency_per_area", "canonical_unit": "ILS/sqm", "column": "transactions.price_per_sqm"},
    {"key": "price", "label_he": "מחיר",
     "aliases": ["מחיר עסקה", "מחיר מכירה", "סכום העסקה", "תמורה"],
     "unit_dimension": "currency", "canonical_unit": "ILS", "column": "transactions.price"},
    {"key": "area", "label_he": "שטח",
     "aliases": ["שטח דירה", "שטח הנכס", "שטח במ״ר", "גודל הדירה"],
     "unit_dimension": "area", "canonical_unit": "sqm", "column": "transactions.area"},
    {"key": "rooms", "label_he": "מספר חדרים",
     "aliases": ["חדרים", "מס׳ חדרים", "כמות חדרים"],
     "unit_dimension": "count", "canonical_unit": "room", "column": "occurrences.rooms"},
]
# Unit dimension -> canonical unit that extracted values are converted to.
CANONICAL_UNITS = {
    "area": "sqm",
    "length": "m",
    "volume": "m3",
    "count": "unit",
    "currency": "ILS",
    "currency_per_area": "ILS/sqm",
    "percent": "percent",
    "ratio": "ratio",
    "year": "year",
    "duration": "month",
}
VALUE_TYPES = ("numeric", "text", "boolean", "date")
EXTRACTION_PROMPT_VERSION = "x1"
_WORD = re.compile(r"[\w״׳]+")
_FILLER = frozenset({"של"})  # "שטח של הדירה" names the same attribute as "שטח הדירה"
_COLUMNS = ("id, key, label_he, aliases, value_type, unit_dimension, canonical_unit, source, structured_column,"
            " extraction_prompt_version, status, facts_version")


@dataclass
class AttributeDef:
    id: UUID
    key: str
    label: str
    unit_dimension: str | None
    canonical_unit: str | None
    source: str
    structured_column: str | None
    facts_version: int
    extraction_prompt_version: str | None
    created: bool = False
    value_type: str = "numeric"
    status: str = "active"
    aliases: list[str] = field(default_factory=list)


def canonical_unit_for(unit_dimension: str | None) -> str | None:
    return CANONICAL_UNITS.get(unit_dimension) if unit_dimension else None


def _tokens(label: str) -> list[str]:
    return [w for w in _WORD.findall(base_normalize(label or "")) if w not in _FILLER]


def _same_word(a: str, b: str) -> bool:
    return a == b or a in prefix_variants(b) or b in prefix_variants(a)


def labels_match(a: str, b: str) -> bool:
    """Exact match after Hebrew normalization, word by word, allowing a stripped prefix or article."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta) and len(ta) == len(tb) and all(_same_word(x, y) for x, y in zip(ta, tb, strict=True))


def proposed_key(description: str) -> str:
    """Deterministic key for a proposed definition, so concurrent creators converge on one row."""
    return "x_" + hashlib.sha256(" ".join(_tokens(description)).encode()).hexdigest()[:12]


def ensure_structured_attributes(conn: Connection) -> int:
    """Insert the structured entries missing for the current office. Idempotent; returns rows added."""
    present = set(conn.execute(text("SELECT key FROM attribute_definitions WHERE source = 'structured'")).scalars())
    added = 0
    for a in STRUCTURED_ATTRIBUTES:
        if a["key"] in present:
            continue
        added += conn.execute(
            text("INSERT INTO attribute_definitions (office_id, key, label_he, aliases, value_type, unit_dimension,"
                 " canonical_unit, source, structured_column, status) VALUES (app_office(), :k, :l, :a, 'numeric',"
                 " :d, :u, 'structured', :c, 'active') ON CONFLICT (office_id, key) DO NOTHING"),
            {"k": a["key"], "l": a["label_he"], "a": a["aliases"], "d": a["unit_dimension"],
             "u": a["canonical_unit"], "c": a["column"]},
        ).rowcount
    return added


def _rows(conn: Connection) -> list:
    # Creation order keeps handles stable: a new definition appends, it never renumbers older ones.
    return conn.execute(text(f"SELECT {_COLUMNS} FROM attribute_definitions ORDER BY created_at, key, id")).all()


def _handle_dict(handle: str, r) -> dict:
    return {"handle": handle, "id": r.id, "key": r.key, "label": r.label_he, "aliases": list(r.aliases or []),
            "unit_dimension": r.unit_dimension, "canonical_unit": r.canonical_unit, "source": r.source,
            "value_type": r.value_type, "status": r.status}


def list_attribute_handles(conn: Connection) -> list[dict]:
    """The office's definitions as interpreter handles A1..An (structured entries ensured first)."""
    ensure_structured_attributes(conn)
    return [_handle_dict(f"A{i}", r) for i, r in enumerate(_rows(conn), start=1)]


def handle_map(handles: list[dict]) -> dict[str, UUID]:
    """Handle -> definition id, for resolving the handles exactly as they were shown."""
    return {h["handle"]: h["id"] for h in handles}


def rank_candidates(conn: Connection, description: str, limit: int = 8) -> list[dict]:
    """Handles ranked by trigram similarity to ``description`` (best label or alias). Ranking only."""
    handles = {h["id"]: h for h in list_attribute_handles(conn)}
    rows = conn.execute(
        text("SELECT id, greatest(similarity(label_he, :d),"
             " coalesce((SELECT max(similarity(x, :d)) FROM unnest(aliases) AS x), 0)) AS score"
             " FROM attribute_definitions ORDER BY score DESC, created_at, key LIMIT :n"),
        {"d": base_normalize(description or ""), "n": limit},
    ).all()
    return [handles[r.id] | {"score": float(r.score)} for r in rows if r.id in handles]


def _to_def(r, created: bool = False) -> AttributeDef:
    return AttributeDef(
        id=r.id, key=r.key, label=r.label_he, unit_dimension=r.unit_dimension, canonical_unit=r.canonical_unit,
        source=r.source, structured_column=r.structured_column, facts_version=r.facts_version,
        extraction_prompt_version=r.extraction_prompt_version, created=created, value_type=r.value_type,
        status=r.status, aliases=list(r.aliases or []),
    )


def resolve_attribute(
    conn: Connection,
    *,
    handle: str | None,
    description: str | None,
    unit_dimension: str | None,
    value_type: str = "numeric",
    handles: dict[str, UUID] | None = None,
) -> AttributeDef:
    """Interpreter-chosen handle, else exact normalized label/alias, else a new ``proposed`` definition.

    ``handles`` pins resolution to the mapping shown to the interpreter (see ``handle_map``); without
    it the current numbering is used. An unknown handle falls back to the description."""
    ensure_structured_attributes(conn)
    rows = _rows(conn)
    if handle:
        mapping = handles if handles is not None else {f"A{i}": r.id for i, r in enumerate(rows, start=1)}
        target = mapping.get(handle.strip())
        for r in rows:
            if r.id == target:
                return _to_def(r)
    description = (description or "").strip()
    if not _tokens(description):
        raise ValueError("an attribute needs a known handle or a description")
    # structured entries first, then the oldest definition
    for r in sorted(rows, key=lambda r: r.source != "structured"):
        if any(labels_match(description, name) for name in [r.label_he, *(r.aliases or [])]):
            return _to_def(r)
    if value_type not in VALUE_TYPES:
        raise ValueError(f"unknown value type {value_type!r}")
    key = proposed_key(description)
    row = conn.execute(
        text("INSERT INTO attribute_definitions (office_id, key, label_he, aliases, value_type, unit_dimension,"
             " canonical_unit, source, extraction_prompt_version, status) VALUES (app_office(), :k, :l, :a, :vt,"
             f" :d, :u, 'extracted', :pv, 'proposed') ON CONFLICT (office_id, key) DO NOTHING RETURNING {_COLUMNS}"),
        {"k": key, "l": description, "a": [description], "vt": value_type, "d": unit_dimension,
         "u": canonical_unit_for(unit_dimension), "pv": EXTRACTION_PROMPT_VERSION},
    ).one_or_none()
    if row is not None:
        return _to_def(row, created=True)
    # a concurrent turn created it first
    return _to_def(conn.execute(text(f"SELECT {_COLUMNS} FROM attribute_definitions WHERE key = :k"),
                                {"k": key}).one())


def bump_facts_version(conn: Connection, attribute_id: UUID) -> int | None:
    """Invalidate cached answers built on this attribute's facts; None when not visible in this office."""
    return conn.execute(
        text("UPDATE attribute_definitions SET facts_version = facts_version + 1 WHERE id = :i RETURNING facts_version"),
        {"i": attribute_id},
    ).scalar()
