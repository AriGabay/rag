"""Entity scoping (KTD10, R9-R11): the documents a question names by address, block/parcel or title.

The interpreter extracts entity strings ("רחוב הרצל 5", "הרצל 5, חולון", "גוש 6158 חלקה 42", a title
fragment). ``resolve_entities`` finds the visible current documents that name each one, with the passage
or title that names it, lists every visible version of those documents, and says whether the match is
unique, ambiguous or none.

Matching is normalized Hebrew: street-type labels (רחוב, רח׳, שדרות ...), one- and two-letter prefixes
and the definite article are optional on both sides, gershayim variants unify, and a house number must
follow the street name (12 never matches 120 or 12א). A mention in the document title or the report
header (its first passages, or a "כתובת" line on page 1) names the document's subject; a mention elsewhere
(a comparables table) counts only when no document names the entity as its subject. A city named in the
entity or the question narrows several candidates, and a document whose known city contradicts it is
dropped. Everything runs in the caller's RLS-scoped transaction: nothing here widens what the user sees.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.metadata import place_matches, version_metadata
from app.answering.places import KNOWN_CITIES
from app.extraction.normalize_text import STOPWORDS, base_normalize, query_tokens
from app.platform.search import SearchScope

HEADER_CHUNKS = 2  # the first passages of a report name its subject
STREET_LABELS = frozenset({"רחוב", "רח׳", "שדרות", "שד׳", "שדרת", "דרך", "סמטת", "כיכר"})
_NUMBER_WORDS = frozenset({"מס׳", "מספר"})
_PREFIXES = "והבלמשכ"
_HEB = re.compile(r"[א-ת][א-ת״׳]*")
_HOUSE = re.compile(r"(\d{1,3})([א-ת])?")
_WORD = re.compile(r"[\w״׳/]+")
_BP_LABELED = re.compile(r"גוש\D{0,6}?(\d{3,6})\D{0,20}?חלק(?:ה|ות)?\D{0,6}?(\d{1,5})(?!\d)")
_BP_SLASH = re.compile(r"(?<![\d/])(\d{3,6})\s*/\s*(\d{1,5})(?!\d)")
# A number followed by a unit is a measure, not a house number (question fallback only).
_UNIT_AFTER = re.compile(r"\s*(?:מ״ר|מטר|מ׳|ס״מ|%|₪|קומות|קומה|חדרים|שנים|שנה|דונם|יח׳)")
_ADDRESS_LABEL = re.compile(r"כתובת")


@dataclass(frozen=True)
class VersionRef:
    version_id: UUID
    version_no: int
    is_current: bool


@dataclass(frozen=True)
class EntityEvidence:
    entity: str
    kind: str  # address | street | block_parcel | title
    where: str  # title | header | body
    document_id: UUID
    version_id: UUID
    chunk_id: UUID | None
    page_list: list[int]
    text: str  # the matched text as it appears (normalized)


@dataclass
class MatchedDocument:
    document_id: UUID
    title: str
    current_version_id: UUID
    versions: list[VersionRef] = field(default_factory=list)  # every visible version, newest first
    evidence: list[EntityEvidence] = field(default_factory=list)


@dataclass
class EntityResolution:
    entity: str
    kind: str  # address | street | block_parcel | title
    status: str  # unique | ambiguous | none
    documents: list[MatchedDocument]
    subject: bool  # the documents name the entity as their subject (title/header), not only in passing
    city: str | None = None


@dataclass
class EntityMatch:
    """``status``: unique (every entity names exactly one document), ambiguous (some entity names several),
    partial (some entity names none, others resolve), none (nothing resolves). ``terms`` are the surface
    forms of the resolved entities, to pass as ``place_terms`` so they are not topic terms."""

    status: str
    resolutions: list[EntityResolution]
    terms: list[str]

    @property
    def document_ids(self) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys(d.document_id for r in self.resolutions for d in r.documents))

    @property
    def version_ids(self) -> tuple[UUID, ...]:
        """Current version ids of the matched documents."""
        return tuple(dict.fromkeys(d.current_version_id for r in self.resolutions for d in r.documents))

    def scope(self) -> SearchScope:
        return SearchScope(document_ids=self.document_ids)


@dataclass(frozen=True)
class _Pattern:
    entity: str
    kind: str
    regex: re.Pattern | None
    probe: str  # a substring every matching passage's normalized text contains
    terms: tuple[str, ...]
    fragment: str = ""


def _canon(value: str) -> str:
    """Normalized text with ''-gershayim unified (רמב''ם = רמב"ם = רמב״ם)."""
    return base_normalize(re.sub(r"(?<=[א-ת])''(?=[א-ת])", "״", value))


def _city_regex(city: str) -> re.Pattern:
    words = r"[\s\-]+".join(re.escape(w) for w in _canon(city).replace("-", " ").split())
    return re.compile(rf"(?<![\w״׳])[{_PREFIXES}]{{0,2}}-?{words}(?![\w״׳])")


_CITIES = sorted(KNOWN_CITIES, key=len, reverse=True)
_CITY_RE = {c: _city_regex(c) for c in _CITIES}


def _find_city(norm: str) -> tuple[str | None, str]:
    """The first (longest) known city named in ``norm``, and ``norm`` without it."""
    for city in _CITIES:
        if _CITY_RE[city].search(norm):
            return city, _CITY_RE[city].sub(" ", norm)
    return None, norm


def _strip_prefix(word: str, keep: int) -> str:
    """Up to two leading prefix letters (and the article) removed, keeping at least ``keep`` letters."""
    for _ in range(2):
        if len(word) - 1 >= keep and word[0] in _PREFIXES:
            word = word[1:]
    return word


def _is_label(word: str) -> bool:
    return word in STREET_LABELS or any(word[k:] in STREET_LABELS and all(c in _PREFIXES for c in word[:k])
                                        for k in (1, 2))


def _street_regex(street: list[str], number: str | None, letter: str) -> tuple[re.Pattern, str, str]:
    """Regex over normalized text, the probe substring, and the street's core phrase."""
    keep = 2 if len(street) > 1 else 3
    core = [_strip_prefix(street[0], keep), *street[1:]]
    head = rf"(?<![\w״׳])[{_PREFIXES}]{{0,3}}ה?{re.escape(core[0])}"
    body = r"[\s\-]+".join([head, *(re.escape(w) for w in core[1:])])
    if number is not None:
        tail = rf"\s*,?\s*(?:מס׳\s*|מספר\s*)?0*{number}{letter}(?!\d)" + ("" if letter else "(?![א-ת])")
    else:
        tail = r"(?![\w״׳])"
    return re.compile(body + tail), max(core, key=len), " ".join(core)


def _address_patterns(entity: str, norm: str, *, fallback: bool = False) -> list[list[_Pattern]]:
    """For each house number in ``norm``: candidate patterns, the longest street phrase first."""
    matches = list(_WORD.finditer(norm))
    words = [m.group(0) for m in matches]
    found: list[list[_Pattern]] = []
    for i, w in enumerate(words):
        m = _HOUSE.fullmatch(w)
        if not m or (fallback and _UNIT_AFTER.match(norm, matches[i].end())):
            continue
        j = i - 1
        if j >= 0 and words[j] in _NUMBER_WORDS:
            j -= 1
        street: list[str] = []
        while j >= 0 and len(street) < 3 and _HEB.fullmatch(words[j]) and not _is_label(words[j]):
            if words[j] in STOPWORDS:
                break
            street.insert(0, words[j])
            j -= 1
        options = []
        for n in range(len(street), 0, -1):
            sw = street[-n:]
            if len(_strip_prefix(sw[0], 2 if n > 1 else 3)) < 2:
                continue
            regex, probe, core = _street_regex(sw, m.group(1), m.group(2) or "")
            num = m.group(1) + (m.group(2) or "")
            phrase = f"{' '.join(sw)} {num}"
            terms = (f"רחוב {phrase}", f"רח׳ {phrase}", phrase, f"{core} {num}")
            options.append(_Pattern(phrase if fallback else entity, "address", regex, probe, terms))
        if options:
            found.append(options)
    return found


def _parse(entity: str) -> tuple[list[list[_Pattern]], str | None]:
    """Candidate patterns of one entity string (alternatives, most specific first), and its city."""
    city, norm = _find_city(_canon(entity))
    bp = _BP_LABELED.search(norm) or (_BP_SLASH.search(norm) if "גוש" in norm or re.fullmatch(
        r"[\d\s/]+", norm.strip()) else None)
    if bp:
        b, p = bp.group(1), bp.group(2)
        regex = re.compile(rf"גוש\D{{0,6}}?0*{b}\D{{0,20}}?חלק(?:ה|ות)?\D{{0,6}}?0*{p}(?!\d)"
                           rf"|(?<![\d/])0*{b}\s*/\s*0*{p}(?!\d)")
        return [[_Pattern(entity, "block_parcel", regex, b, (f"גוש {b} חלקה {p}", f"{b}/{p}", norm.strip()))]], city
    addresses = _address_patterns(entity, norm)
    if addresses:
        return addresses[-1:], city
    words = [w for w in _WORD.findall(norm) if _HEB.fullmatch(w)]
    labeled = [i for i, w in enumerate(words) if _is_label(w)]
    if labeled and labeled[0] + 1 < len(words):
        street = words[labeled[0] + 1:labeled[0] + 4]
        regex, probe, core = _street_regex(street, None, "")
        if len(core.replace(" ", "")) >= 3:
            return [[_Pattern(entity, "street", regex, probe, (" ".join(words[labeled[0]:]), core))]], city
    fragment = " ".join(_WORD.findall(norm))
    return ([[_Pattern(entity, "title", None, "", (fragment,), fragment=fragment)]] if fragment else []), city


def _question_patterns(question: str) -> list[list[_Pattern]]:
    norm = _canon(question)
    found: list[list[_Pattern]] = []
    for m in [*_BP_LABELED.finditer(norm), *_BP_SLASH.finditer(norm)]:
        found += _parse(m.group(0))[0]
    return found + _address_patterns(question, norm, fallback=True)


def _where(row, start: int, norm: str) -> str:
    if row.kind != "text":
        return "body"
    first_page = not row.page_list or row.page_list[0] == 1
    if row.chunk_index < HEADER_CHUNKS or (first_page and _ADDRESS_LABEL.search(norm[max(0, start - 30):start])):
        return "header"
    return "body"


def _title_norm(title: str) -> str:
    return _canon(title.replace("_", " "))


def _match(conn: Connection, pattern: _Pattern, titles: list) -> list[EntityEvidence]:
    out: list[EntityEvidence] = []
    fragment_tokens = set(query_tokens(pattern.fragment)) if pattern.fragment else set()
    for t in titles:
        tn = _title_norm(t.title)
        if pattern.regex is not None:
            m = pattern.regex.search(tn)
            surface = m.group(0).strip() if m else None
        elif pattern.fragment in tn or (fragment_tokens and fragment_tokens <= set(query_tokens(tn))):
            surface = pattern.fragment
        else:
            surface = None
        if surface:
            out.append(EntityEvidence(pattern.entity, pattern.kind, "title", t.document_id, t.version_id, None, [],
                                      surface))
    if pattern.regex is None:
        return out
    rows = conn.execute(
        text("SELECT c.id, c.document_id, c.version_id, c.chunk_index, c.kind, c.page_list, c.text FROM chunks c"
             " JOIN document_versions v ON v.id = c.version_id AND v.is_current"
             " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL"
             " WHERE c.normalized_text LIKE '%' || :probe || '%' ORDER BY c.document_id, c.chunk_index"),
        {"probe": pattern.probe},
    ).all()
    for r in rows:
        norm = _canon(r.text)
        m = pattern.regex.search(norm)
        if m:
            out.append(EntityEvidence(pattern.entity, pattern.kind, _where(r, m.start(), norm), r.document_id,
                                      r.version_id, r.id, list(r.page_list or []), m.group(0).strip()))
    return out


def _narrow_by_city(conn: Connection, docs: dict[UUID, list[EntityEvidence]], city: str, titles: dict
                    ) -> dict[UUID, list[EntityEvidence]]:
    """Keep documents in ``city``; when none states a city, keep the unknown ones; drop contradictions."""
    versions = {evs[0].version_id: d for d, evs in docs.items()}
    meta = version_metadata(conn, list(versions))
    first = {r.document_id: _canon(r.text) for r in conn.execute(
        text("SELECT c.document_id, c.text FROM chunks c WHERE c.version_id = ANY(:v) AND c.chunk_index = 0"),
        {"v": list(versions)},
    ).all()}
    pattern = _CITY_RE.get(city) or _city_regex(city)
    yes, unknown = {}, {}
    for d, evs in docs.items():
        named = any(pattern.search(t) for t in [*(e.text for e in evs), first.get(d, ""), _title_norm(titles[d])])
        known = meta[evs[0].version_id].cities if evs[0].version_id in meta else frozenset()
        if (known and place_matches(city, known)) or named:
            yes[d] = evs
        elif not known:
            unknown[d] = evs
    return yes or unknown


def _resolve_one(conn: Connection, options: list[_Pattern], city: str | None, titles: list,
                 versions: dict) -> tuple[EntityResolution, _Pattern]:
    chosen, docs = options[-1], {}
    for pattern in options:  # the longest street phrase that names something wins
        evidence = _match(conn, pattern, titles)
        if evidence:
            chosen = pattern
            for e in evidence:
                docs.setdefault(e.document_id, []).append(e)
            break
    subject_docs = {d: evs for d, evs in docs.items() if any(e.where in ("title", "header") for e in evs)}
    subject = bool(subject_docs)
    docs = subject_docs or docs
    title_of = {t.document_id: t.title for t in titles}
    if city and docs:
        docs = _narrow_by_city(conn, docs, city, title_of)
    matched = []
    for d, evs in docs.items():
        evs = sorted(evs, key=lambda e: ({"title": 0, "header": 1, "body": 2}[e.where], e.page_list or [0]))
        matched.append(MatchedDocument(d, title_of.get(d, ""), evs[0].version_id, versions.get(d, []), evs))
    status = "none" if not matched else "unique" if len(matched) == 1 else "ambiguous"
    return EntityResolution(chosen.entity, chosen.kind, status, matched, subject and bool(matched), city), chosen


def resolve_entities(conn: Connection, entities: Sequence[str], *, question: str = "") -> EntityMatch:
    """The visible current documents each entity names. When ``entities`` is empty, addresses and
    block/parcel numbers written in ``question`` are tried instead, and only those that name a document's
    subject are kept. A city in the entity, else in the question, narrows the candidates."""
    question_city, _ = _find_city(_canon(question)) if question else (None, "")
    parsed: list[tuple[list[_Pattern], str | None]] = []
    for entity in entities:
        if not entity or not entity.strip():
            continue
        alternatives, city = _parse(entity)
        parsed += [(opts, city or question_city) for opts in alternatives]
    fallback = not parsed
    if fallback and question:
        parsed = [(opts, question_city) for opts in _question_patterns(question)]
    titles = conn.execute(text(
        "SELECT d.id AS document_id, d.title, v.id AS version_id FROM documents d"
        " JOIN document_versions v ON v.document_id = d.id AND v.is_current WHERE d.deleted_at IS NULL"
        " ORDER BY d.created_at, d.id")).all()
    versions: dict[UUID, list[VersionRef]] = {}
    for r in conn.execute(text(
            "SELECT v.id, v.document_id, v.version_no, v.is_current FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL ORDER BY v.version_no DESC")).all():
        versions.setdefault(r.document_id, []).append(VersionRef(r.id, r.version_no, r.is_current))
    resolutions: list[EntityResolution] = []
    terms: list[str] = []
    for options, city in parsed:
        resolution, pattern = _resolve_one(conn, options, city, titles, versions)
        if fallback and not resolution.subject:
            continue
        resolutions.append(resolution)
        if resolution.documents:
            # the document's own spelling ("שינקין 18" for an asked "בשינקין 18") strips the question best
            surfaces = [e.text for d in resolution.documents for e in d.evidence if e.where != "body"]
            labeled = [f"{label} {x}" for x in surfaces for label in ("רחוב", "רח׳") if pattern.kind == "address"]
            terms += [t for t in (*pattern.terms, *surfaces, *labeled) if t and t not in terms]
    statuses = [r.status for r in resolutions]
    if not statuses or all(s == "none" for s in statuses):
        status = "none"
    elif "none" in statuses:
        status = "partial"
    elif "ambiguous" in statuses:
        status = "ambiguous"
    else:
        status = "unique"
    return EntityMatch(status, resolutions, sorted(terms, key=len, reverse=True))
