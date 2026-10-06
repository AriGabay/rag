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
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering import units
from app.extraction.normalize_text import base_normalize, inflection_variants, prefix_variants

# Structured attributes map to whitelisted record columns (the CHECK in migration 0004 and
# STRUCTURED_COLUMNS in app.appraisal.query). Single source of truth; scripts/seed_demo.py imports it.
STRUCTURED_ATTRIBUTES = [
    {"key": "price_per_sqm", "label_he": "מחיר למ״ר",
     "aliases": ["מחיר למטר", "מחיר למ\"ר", "מחיר למטר רבוע", "מחיר למטר מרובע", "שווי למ״ר"],
     "unit_dimension": "currency_per_area", "canonical_unit": "ILS/sqm", "column": "transactions.price_per_sqm",
     "description": "מחיר או שווי למ״ר של נכס שלם, מרשומות העסקאות והשומות המאומתות"},
    {"key": "price", "label_he": "מחיר",
     "aliases": ["מחיר עסקה", "מחיר מכירה", "סכום העסקה", "תמורה"],
     "unit_dimension": "currency", "canonical_unit": "ILS", "column": "transactions.price",
     "description": "מחיר העסקה או השווי של נכס שלם, מרשומות העסקאות והשומות המאומתות"},
    {"key": "area", "label_he": "שטח",
     "aliases": ["שטח דירה", "שטח הנכס", "שטח במ״ר", "גודל הדירה"],
     "unit_dimension": "area", "canonical_unit": "sqm", "column": "transactions.area",
     "description": "שטח הנכס כולו ברשומות העסקאות והשומות; לא שטח של רכיב או חלל בתוך הנכס"},
    {"key": "rooms", "label_he": "מספר חדרים",
     "aliases": ["חדרים", "מס׳ חדרים", "כמות חדרים"],
     "unit_dimension": "count", "canonical_unit": "room", "column": "occurrences.rooms",
     "description": "מספר החדרים של נכס שלם ברשומות העסקאות והשומות"},
]
# What a structured entry covers, shown to the interpreter so it does not stretch a whole-property column
# over a part of the property.
_STRUCTURED_DESCRIPTIONS = {a["key"]: a["description"] for a in STRUCTURED_ATTRIBUTES}
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
# x2: a value must be named as the attribute (attribute_term). x3: table cells carry their row label, counts
# as words, W×H dimensions, text values. x4: a value bound to its own label among several, terms of the same
# root, stated absence as zero; earlier ledger states are read again. x5/x6: quotes compared without separator
# punctuation, the fixed dimension vocabulary. x7: validation binds a value to the neighbourhood of its cited
# source, a count to its own noun and a zero word to the attribute, and rejects a value named by another
# attribute; documents read under x6 are read again. Reviewed facts (verified, corrected, rejected) keep their
# decision across versions (``facts.compute_facts``).
EXTRACTION_PROMPT_VERSION = "x7"
INTERPRETER_ATTRIBUTES_MAX = 60  # definitions shown to the interpreter on each turn (prompt size)
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


# --- naming words: what a description, a naming term or a label says is measured ----------------------------

def match_norm(value: str) -> str:
    """Normalized text for verbatim comparisons (prompt-safe angle quotes read back as plain ones)."""
    return " ".join(base_normalize((value or "").replace("‹", "<").replace("›", ">")).split())


# Words that say what is measured, not what the measured thing is ("שטח", "גודל" ...). A naming term made only
# of these does not tie a number to the attribute: "שטח 94 מ״ר" is not the area of every part of a property.
GENERIC_MEASURE_WORDS = frozenset(match_norm(w) for w in (
    "גודל", "שטח", "מספר", "כמות", "גובה", "אורך", "רוחב", "עומק", "נפח", "שנת", "שנה", "ממוצע", "סך", "סה״כ",
    "ערך", "מידה", "מידות", "יחידה", "יחידות", "מ״ר", "במ״ר", "מטר", "של", "כולל", "נטו", "ברוטו", "רשום"))
_LETTER_WORD = re.compile(r"[^\W\d_][\w״׳]*")  # punctuation ("(מ״ר):") and bare numbers are not words
_ARTICLES = ("וה", "ה")


def word_forms(word: str) -> set[str]:
    """A word, its prefix-stripped variants, and the singular/plural forms of the word and of the word without
    its article ("הכיתות" also names "כיתה"). Other stripped prefixes are not inflected: "מקומות" is not a
    form of "קומה"."""
    out = {word, *prefix_variants(word)}
    for base in [word] + [word[len(a):] for a in _ARTICLES if word.startswith(a) and len(word) - len(a) >= 3]:
        out.add(base)
        out.update(inflection_variants(base))
    # a stem cut from a plural ends in a medial letter ("X-ים" -> stem): compare with final forms unified
    return out | {w.translate(_FINALS) for w in out}


def text_words(value: str) -> list[str]:
    return _LETTER_WORD.findall(match_norm(value))


def is_measure_word(word: str) -> bool:
    return bool(word_forms(word) & GENERIC_MEASURE_WORDS)


def distinctive_words(value: str) -> list[str]:
    """The words of ``value`` that name what is measured: generic measure words excluded."""
    return [w for w in text_words(value) if not is_measure_word(w)]


# Root skeleton (a light Hebrew morphology rule, not vocabulary): up to two prefix letters stripped, the
# vowel letters ו and י dropped, final letter forms unified; the first three consonants approximate the root.
_ROOT_PREFIXES = "והבלמשכנ"
_FINALS = str.maketrans("ךםןףץ", "כמנפצ")
_NOT_LETTER = re.compile(r"[^א-ת]")


def _root_variants(word: str) -> list[str | None]:
    """Skeletons of the word with 0, 1 and 2 prefix letters stripped (None when shorter than three)."""
    w = _NOT_LETTER.sub("", word).translate(_FINALS)
    out: list[str | None] = []
    for k in range(3):
        if k and not (len(w) > k and w[k - 1] in _ROOT_PREFIXES):
            break
        skeleton = w[k:].replace("ו", "").replace("י", "")
        out.append(skeleton[:3] if len(skeleton) >= 3 else None)
    return out


def share_root(a: str, b: str) -> bool:
    """Two words of one root in other forms (a verb and its noun, a participle). Two unstripped skeletons that
    begin with the same prefix letter match only when there is nothing left to compare after it: "המחיר" and
    "המחלקה" share only their article. Limits: a word whose own first letters look like prefixes (or a root
    with a weak letter) can meet an unrelated word of three equal consonants; used only to tell a naming
    term of the attribute's own words from another phrasing, never to merge definitions."""
    va, vb = _root_variants(a), _root_variants(b)
    for i, x in enumerate(va):
        for j, y in enumerate(vb):
            if x is None or y is None or x != y:
                continue
            if i == j == 0 and len(va) > 1 and va[1] and len(vb) > 1 and vb[1]:
                continue
            return True
    return False


def words_share(a: list[str], b: list[str], *, roots: bool = False) -> bool:
    """Whether a word of ``a`` and a word of ``b`` are forms of one word (or, with ``roots``, of one root)."""
    fb = [word_forms(y) for y in b]
    if any(word_forms(x) & f for x in a for f in fb):
        return True
    return roots and any(share_root(x, y) for x in a for y in b)


# --- identity: one definition per meaning ------------------------------------------------------------------
# Measure words say what is measured; two names with different measure words ("גובה X", "רוחב X") are different
# attributes even with the same distinctive words, while "גודל X" and "שטח X" measure the same thing.
_MEASURE_CLASS = {match_norm(w): c for w, c in (
    ("גודל", "size"), ("שטח", "size"), ("מידה", "size"), ("מידות", "size"), ("גובה", "height"), ("אורך", "length"),
    ("רוחב", "width"), ("עומק", "depth"), ("נפח", "volume"), ("מספר", "count"), ("כמות", "count"),
    ("שנת", "year"), ("שנה", "year"), ("ערך", "value"))}
# Measure words that qualify what is measured: "שטח X נטו" and "שטח X ברוטו" are different attributes.
_QUALIFIERS = frozenset(match_norm(w) for w in ("נטו", "ברוטו", "כולל", "רשום"))
_MONETARY_DIMENSIONS = frozenset({"currency", "currency_per_area"})


def _identity(name: str) -> tuple[frozenset[str], frozenset[str], list[str]]:
    """(measure classes, qualifiers, distinctive words) of an attribute name."""
    measures: set[str] = set()
    qualifiers: set[str] = set()
    for w in text_words(name):
        forms = word_forms(w)
        measures.update(_MEASURE_CLASS[f] for f in forms if f in _MEASURE_CLASS)
        qualifiers.update(f for f in forms if f in _QUALIFIERS)
        if w.startswith("ל") and len(w) > 2 and (per := units.parse_unit(w[1:])) is not None:
            qualifiers.add(f"per:{per.dimension}")  # "למ״ר", "לחודש": a rate is not its total
    return frozenset(measures), frozenset(qualifiers), [w for w in distinctive_words(name) if not (
        w.startswith("ל") and len(w) > 2 and units.parse_unit(w[1:]) is not None)]


def same_attribute(a: str, dim_a: str | None, b: str, dim_b: str | None) -> bool:
    """Whether two names with their dimensions denote one attribute: the same distinctive words up to form
    (singular or plural, with or without a prefix or article: "שטח X" ~ "שטח ה-Xים"), the same measure
    and qualifier words, and no conflicting dimension. Word order and filler do not matter. Another word, another
    measure word, another qualifier or another unit dimension is another attribute: names are never merged by
    root, by similarity, or by one name containing the other ("X" vs "X בבניין")."""
    if dim_a and dim_b and dim_a != dim_b:
        return False
    ma, qa, da = _identity(a)
    mb, qb, db = _identity(b)
    if ma != mb or qa != qb or not da or len(da) != len(db):
        return False
    left = list(db)
    for w in da:
        match = next((x for x in left if word_forms(w) & word_forms(x)), None)
        if match is None:
            return False
        left.remove(match)
    return True


def other_attribute_names(conn: Connection, attribute: AttributeDef) -> list[list[str]]:
    """The distinctive name words of the office's other attributes whose names share none with this one's: a
    value named by them belongs to another attribute (``facts.validate_mention``). Definitions that share a
    word with this one (another phrasing, a duplicate) are left out, so they never reject its values."""
    own = [w for n in [attribute.label, *attribute.aliases] for w in distinctive_words(n)]
    out: list[list[str]] = []
    for r in conn.execute(text("SELECT id, label_he, aliases, value_type, unit_dimension FROM attribute_definitions"
                               " WHERE id <> :a"), {"a": attribute.id}).all():
        # only an attribute that could hold this value: the same value type, and a unit dimension that is the
        # same or unknown (a count never claims an area)
        if r.value_type != attribute.value_type or (
                r.unit_dimension and attribute.unit_dimension and r.unit_dimension != attribute.unit_dimension):
            continue
        words = [w for n in [r.label_he, *(r.aliases or [])] for w in distinctive_words(n)]
        if words and not words_share(words, own, roots=True):
            out.append(words)
    return out


def proposed_key(description: str, value_type: str = "numeric", unit_dimension: str | None = None) -> str:
    """Deterministic key for a proposed definition, so concurrent creators converge on one row. A non-numeric
    definition of the same label has its own key (it never reuses a numeric one's facts), and so does a numeric
    one of another unit dimension."""
    base = " ".join(_tokens(description)) + ("" if value_type == "numeric" else f"|{value_type}") + (
        f"|{unit_dimension}" if unit_dimension and value_type == "numeric" else "")
    return "x_" + hashlib.sha256(base.encode()).hexdigest()[:12]


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
            "value_type": r.value_type, "status": r.status,
            "description": _STRUCTURED_DESCRIPTIONS.get(r.key) if r.source == "structured" else None}


def upgrade_extraction_version(conn: Connection) -> int:
    """Move extracted definitions to the current extraction prompt: facts of an older prompt are no longer
    used, the ledger re-reads those documents, and answers keyed on the old facts version go stale."""
    return conn.execute(
        text("UPDATE attribute_definitions SET extraction_prompt_version = :pv, facts_version = facts_version + 1"
             " WHERE source = 'extracted'"
             " AND split_part(coalesce(extraction_prompt_version, ''), '+', 1) IS DISTINCT FROM :pv"),
        {"pv": EXTRACTION_PROMPT_VERSION}).rowcount


def list_attribute_handles(conn: Connection, *, useful_only: bool = False) -> list[dict]:
    """The office's definitions as interpreter handles A1..An (structured entries ensured first).

    ``useful_only`` (what the interpreter sees) keeps structured and active definitions and the proposed
    ones that were read at least once (they hold facts or ledger states) or hold a reviewed fact, the most
    used first, up to ``INTERPRETER_ATTRIBUTES_MAX``: a definition nobody ever extracted is a phrasing nobody
    confirmed. A definition the interpreter does not see is still found by name (``resolve_attribute``)."""
    ensure_structured_attributes(conn)
    upgrade_extraction_version(conn)
    rows = _rows(conn)
    numbered = [(f"A{i}", r) for i, r in enumerate(rows, start=1)]
    if useful_only:
        use: Counter = Counter()
        for r in conn.execute(text(
            "SELECT attribute_id, count(*) AS n FROM fact_extraction_ledger GROUP BY attribute_id"
            " UNION ALL SELECT attribute_id, count(*) FROM facts WHERE status IN ('verified', 'corrected')"
            " GROUP BY attribute_id")).all():
            use[r.attribute_id] += r.n
        fixed = [(h, r) for h, r in numbered if r.source == "structured" or r.status == "active"]
        read = sorted(((h, r) for h, r in numbered if (h, r) not in fixed and r.id in use),
                      key=lambda hr: -use[hr[1].id])
        keep = {r.id for _, r in fixed + read[:max(0, INTERPRETER_ATTRIBUTES_MAX - len(fixed))]}
        numbered = [(h, r) for h, r in numbered if r.id in keep]
    return [_handle_dict(h, r) for h, r in numbered]


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


# Words the interpreter may use for a measurement dimension, mapped to ``CANONICAL_UNITS`` keys.
_DIMENSION_SYNONYMS = {
    "area": "area", "size": "area", "surface": "area",
    "length": "length", "height": "length", "width": "length", "depth": "length", "distance": "length",
    "volume": "volume",
    "count": "count", "quantity": "count", "number": "count", "amount": "count",
    "currency": "currency", "money": "currency", "price": "currency", "value": "currency",
    "currency_per_area": "currency_per_area", "price_per_area": "currency_per_area",
    "percent": "percent", "percentage": "percent", "ratio": "ratio",
    "year": "year", "date": "year", "duration": "duration", "time": "duration",
}


def normalize_dimension(dimension: str | None) -> str | None:
    """A known measurement dimension, or None (unknown words never become a dimension)."""
    if not dimension:
        return None
    return _DIMENSION_SYNONYMS.get(dimension.strip().lower())


def _with_dimension(conn: Connection, r, dimension: str | None):
    """An extracted numeric definition created without a dimension takes the one a later request
    names. It moves to its own extraction version ("x7+area"), for the whole office at once: every document is
    read again under the new version, facts of the dimensionless reads stop counting (they are kept, with their
    history, never deleted), and cached answers keyed on the old facts version go stale. A reviewer's earlier
    decision still counts (``facts.compute_facts``)."""
    if (dimension is None or r.source != "extracted" or r.value_type != "numeric" or r.unit_dimension
            is not None):
        return r
    conn.execute(text("UPDATE attribute_definitions SET unit_dimension = :d, canonical_unit = :u,"
                      " extraction_prompt_version = :pv, facts_version = facts_version + 1 WHERE id = :a"),
                 {"d": dimension, "u": canonical_unit_for(dimension), "a": r.id,
                  "pv": f"{EXTRACTION_PROMPT_VERSION}+{dimension}"})
    return conn.execute(text(f"SELECT {_COLUMNS} FROM attribute_definitions WHERE id = :a"), {"a": r.id}).one()


def _names(r) -> list[str]:
    return [r.label_he, *(r.aliases or [])]


def _handle_fits_description(r, description: str | None, dimension: str | None, rows: list) -> bool:
    """Whether an interpreter-chosen handle agrees with the plan's own description of the attribute.

    It does not when the plan names a known dimension the definition does not measure ("the area of X" with
    the handle of "the number of X"), or when the description shares no distinctive word (form or root) with
    the definition's names and itself names another definition exactly. A handle whose names merely use other
    words is the interpreter's semantic match (a synonym) and is kept (KTD7)."""
    if dimension is not None and r.unit_dimension is not None and r.unit_dimension != dimension and not (
            {dimension, r.unit_dimension} <= _MONETARY_DIMENSIONS):  # "price" for a price per m² is a loose word
        return False
    asked = distinctive_words(description or "")
    if not asked:
        return True
    if words_share(asked, [w for n in _names(r) for w in distinctive_words(n)], roots=True):
        return True
    return not any(o.id != r.id and any(labels_match(description or "", n) for n in _names(o)) for o in rows)


def _type_fits(r, value_type: str | None) -> bool:
    """Whether a definition can answer a request for ``value_type``. Structured definitions are numeric columns
    and answer anything asked of them. A numeric request needs numbers, and a numeric definition extracts only
    numbers, so neither crosses to the other; a text, boolean or date request reuses a definition of any of
    these three (they all keep the value as written)."""
    if value_type is None or r.source == "structured" or r.value_type == value_type:
        return True
    return value_type != "numeric" and r.value_type != "numeric"


def _dimension_fits(r, dimension: str | None) -> bool:
    return dimension is None or r.unit_dimension is None or r.unit_dimension == dimension or (
        {dimension, r.unit_dimension} <= _MONETARY_DIMENSIONS)


def _add_alias(conn: Connection, r, name: str) -> None:
    """Record another phrasing of a definition, so it is matched exactly and shown to the interpreter."""
    if name and not any(labels_match(name, n) for n in _names(r)):
        conn.execute(text("UPDATE attribute_definitions SET aliases = array_append(aliases, :n) WHERE id = :a"),
                     {"n": name, "a": r.id})


def resolve_attribute(
    conn: Connection,
    *,
    handle: str | None,
    description: str | None,
    unit_dimension: str | None,
    value_type: str | None = None,
    handles: dict[str, UUID] | None = None,
) -> AttributeDef:
    """The definition an attribute request denotes: the interpreter-chosen handle, else one whose name denotes
    the same attribute (``labels_match``, then ``same_attribute``: another form of the same words), else a new
    ``proposed`` definition. One meaning keeps one definition, so a rephrased question reuses the facts already
    extracted and verified instead of reading the documents again; names that differ in a word, a measure
    word, a qualifier or the unit dimension stay separate definitions.

    ``handles`` pins resolution to the mapping shown to the interpreter (see ``handle_map``); without
    it the current numbering is used. An unknown handle falls back to the description.

    ``value_type`` (one of ``VALUE_TYPES``) is what the caller needs; None accepts any existing type and
    creates numeric. A numeric request is never answered by a non-numeric definition, nor the reverse: the
    match goes to a definition of the same name of a fitting type, created when missing. A text, boolean or
    date request reuses one of the same name of any of those types, its own type first (``_type_fits``)."""
    if value_type is not None and value_type not in VALUE_TYPES:
        raise ValueError(f"unknown value type {value_type!r}")

    unit_dimension = normalize_dimension(unit_dimension)
    ensure_structured_attributes(conn)
    rows = _rows(conn)
    if handle:
        mapping = handles if handles is not None else {f"A{i}": r.id for i, r in enumerate(rows, start=1)}
        target = mapping.get(handle.strip())
        for r in rows:
            if r.id == target and _handle_fits_description(r, description, unit_dimension, rows):
                if _type_fits(r, value_type):
                    return _to_def(_with_dimension(conn, r, unit_dimension))
                description = r.label_he  # the same attribute, asked as another type
    description = (description or "").strip()
    if not _tokens(description):
        raise ValueError("an attribute needs a known handle or a description")
    # structured entries first, then the own type, then the oldest definition
    ranked = sorted(rows, key=lambda r: (r.source != "structured", value_type is not None and r.value_type != value_type))
    candidates = [r for r in ranked if _type_fits(r, value_type) and _dimension_fits(r, unit_dimension)]
    for r in candidates:
        if any(labels_match(description, name) for name in _names(r)):
            return _to_def(_with_dimension(conn, r, unit_dimension))
    for r in candidates:
        if r.source != "structured" and any(same_attribute(description, unit_dimension, name, r.unit_dimension)
                                            for name in _names(r)):
            _add_alias(conn, r, description)
            return _to_def(_with_dimension(conn, r, unit_dimension))
    value_type = value_type or "numeric"
    if value_type != "numeric":
        unit_dimension = None
    key = proposed_key(description, value_type, unit_dimension)
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
