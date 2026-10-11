"""Appraisal contexts inside one file, derived from its stored reading at query time (round 7 U6: KTD7; R20–R22).

A file may hold several appraisals, or an appendix about a comparison property, each about its own property. A
figure belongs to the property of the part of the file it is written in, never to the file as a whole: the file's
permission, its title or the conversation's focus never prove that a number belongs to the asked property (R21).

**Derivation** (``derive``), from the version's ``document_blocks`` only — no re-extraction, no model call, no
migration:

- only *labelled* property identifiers count (``identifiers``): a block and parcel pair written with its labels
  ("גוש 30871 חלקה 15", the pattern of ``answering.entities._BP_LABELED``, as ``appraisal.normalize.
  parse_block_parcel`` reads a labelled pair), and an address with a street label and a house number ("רחוב הצפצפה
  7", the street labels of ``answering.entities.STREET_LABELS``). A bare number, a section number, a plan number
  ("דמו/5110"), a year, a slash pair ("30871/22") or a street without a label is never one;
- the first context's identity is the identifiers of the file's title block (the blocks before its first heading);
  a file whose title block names none is one context;
- a new context opens only where a heading, or a title block (the run of blocks at the top of a page that ends at a
  heading), restates a complete identifier set of the same kinds as the current one — every kind the current one
  has, one value of each — that shares no identifier with it, **and** a new report starts there: the first numbered
  top-level heading at or after it ("1. מבוא"; never "2.1") has a lower number than the last one of the current
  context — the report's numbering restarts. A file or a context without numbered top-level headings never splits on
  an identifier run alone. Identifiers inside tables (table blocks are never read here), in a run that names several
  properties (a comparison list), in a bulleted run (a list item), or in a heading or run inside a section that is
  still continuing (a sub-building, a comparable — the numbering goes on) never open one, and a page break alone
  never does;
- an identity seen before (the main property after an appendix) is the same context again when the numbering it
  had continues there (its next top-level number is above its last one): a context may be several ranges
  (``Segment``) with one number.

**Units** (round 9 U8, KTD6; R6, R10): ``of_version`` builds its segments from the reading's stored appraisal units
(``units.ensure``: stored, or built once on first use), one segment per unit, numbered by the unit's number, so every
caller keeps this one API. A unit's identity holds its labelled identifiers and, as the ``names`` kind, its title and
its document-grounded aliases, which ``named`` matches in free text like an identifier (normalized, the Hebrew prefix
letters before a name allowed). A unit of kind ``appendix`` or ``comparables`` is labelled as such and is never a
subject: ``named`` never returns it, so no check takes it for the asked property. Front matter and undetermined
document units are candidates like an appraisal. The derivation above stays as the fallback when the units cannot be
obtained (no reading).

A file with one identifier set is a single context (``Contexts.multi`` false): every tool then behaves exactly as
before and shows no context label. A row of a table that the extraction merged across two appraisals (a table
continued on the next page under the same headers) belongs to the context of its own position (page and top), so a
table is split by context at query time, never re-extracted.

**Caching** (``of_version``): the result is cached per version and reading (a version read again is a new key), in
process; the caller has already resolved the version under the user's permissions. ``Contexts.key`` carries the
reading too, so a key is never reused across readings.

**Enforcement** (``enforced``, the ``chat_appraisal_context_enforced`` setting, on by default — enabled once the
contexts were counted per report on the regression reports by ``scripts/count_appraisal_contexts.py``: every
single-appraisal report derives exactly one; set it false to show the labels without enforcing): the subject
measurements a meaning check reads are restricted to the cited context (``meaning.Fetcher``), ``calculate``
refuses to combine values of different contexts unless a frozen calculation component of the turn compares them
(``calc.evaluate``), ``coverage.conflicting`` keys on the context, and verification removes a claim about the
asked property that rests on another context (``verify``, failure kind ``wrong_subject``). The labels themselves
are always shown.
"""

from __future__ import annotations

import logging
import re
import threading
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.entities import _BP_LABELED, _PREFIXES, STREET_LABELS, _canon, _street_regex, _strip_prefix
from app.extraction.base import PageGeometry
from app.extraction.geometry import geometry_box

logger = logging.getLogger(__name__)

ADDRESS = "address"
PARCEL = "block_parcel"
NAMES = "names"
KIND_LABELS = {ADDRESS: "כתובת", PARCEL: "גוש/חלקה", NAMES: "שם"}

# the kinds of a unit (``units.KINDS``) that are never a subject: a part of a file about other properties
NON_SUBJECT_KINDS = frozenset({"appendix", "comparables"})
# how a unit that is not an appraisal is called in a label (the one place that words them)
UNIT_KIND_MARKS = {"appendix": "נספח", "comparables": "עסקאות השוואה", "front_matter": "פתיח"}
MSG_UNNAMED = "ללא שם מזהה"
# punctuation that separates the words of a name (the aliases of a unit are normalized with it)
NAME_PUNCT = re.compile(r"[\"“”‘’,.:;(){}\[\]<>!?|/\\—–_*•]")

# a street label (with up to two prefix letters: "ברחוב"), one to three street words, an optional "מס׳", and a house
# number of up to three digits (with an optional letter) that is not a measure ("120 מ״ר")
_LABELS = "|".join(sorted((re.escape(x) for x in STREET_LABELS), key=len, reverse=True))
_STREET_WORD = r"[א-ת][א-ת״׳\-]*"
_ADDRESS = re.compile(
    rf"(?<![א-ת])[{_PREFIXES}]{{0,2}}(?:{_LABELS})\s+((?:{_STREET_WORD}\s+){{0,2}}{_STREET_WORD})"
    r"\s*,?\s*(?:מס׳\s*|מספר\s*)?(\d{1,3})([א-ת](?![א-ת]))?(?![\d.,]?\d)"
    r"(?!\s*(?:מ״ר|מטר|מ׳|ס״מ|%|₪|קומות|קומה|חדרים|שנים|שנה|דונם|יח׳))")
_NOT_STREET = frozenset({"גוש", "חלקה", "חלקות", "מגרש", "תכנית", "דירה", "קומה", "בניין"})
_CANDIDATE_SQL = "גוש|רחוב|רח׳|רח'|שד׳|שד'|שדרות|שדרת|דרך|סמטת|כיכר"
_SKIP_STATUS = ("decorative", "unread", "no_text")
CACHE_MAX = 256


@dataclass(frozen=True)
class Identity:
    """A labelled identifier set: addresses as ``(street core, house number)`` and parcels as ``(block, parcel)``,
    with how each was written (``shown``) and the street words as written (for matching a subject); a unit's
    ``names`` (its title and aliases, ``name_key``) are the third kind."""

    addresses: frozenset = frozenset()
    parcels: frozenset = frozenset()
    shown: tuple[str, ...] = ()
    streets: tuple[tuple[tuple[str, ...], str, str], ...] = ()  # (street words, number, letter)
    names: tuple[str, ...] = ()  # a unit's title and aliases, normalized (``name_key``)

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(k for k, v in ((ADDRESS, self.addresses), (PARCEL, self.parcels), (NAMES, self.names)) if v)

    @property
    def empty(self) -> bool:
        return not self.addresses and not self.parcels and not self.names

    @property
    def label(self) -> str:
        return " · ".join(self.shown)

    def count(self, kind: str) -> int:
        return len({ADDRESS: self.addresses, PARCEL: self.parcels, NAMES: self.names}[kind])

    def shares(self, other: Identity) -> bool:
        """Whether the two sets name a common identifier or name."""
        return bool(self.addresses & other.addresses or self.parcels & other.parcels
                    or set(self.names) & set(other.names))

    def restates_other(self, current: Identity) -> bool:
        """Whether this set opens a new context after ``current``: it has every kind ``current`` has, one value of
        each (a run naming several properties is a list, not a title), and shares nothing with it."""
        if current.empty or not current.kinds <= self.kinds:
            return False
        if any(self.count(k) != 1 for k in self.kinds):
            return False
        return not self.shares(current)

    def identifier_in(self, words: str) -> bool:
        """Whether a free text (a subject, a request component, a claim) names one of the identifiers: a street and
        house number (the label optional), or a block and parcel (labelled, or as ``block/parcel``)."""
        norm = _canon(words or "")
        return any(p.search(norm) for p in _patterns(self))

    def name_spans(self, words: str) -> list[tuple[int, int]]:
        """Where a free text names the unit's title or an alias (the spans in ``name_key(words)``)."""
        norm = name_key(words)
        return [m.span() for p in _name_patterns(self.names) for m in p.finditer(norm)]

    def named_in(self, words: str) -> bool:
        """Whether a free text names one of the identifiers or one of the names."""
        return self.identifier_in(words) or bool(self.name_spans(words))

    def __or__(self, other: Identity) -> Identity:
        shown = tuple(dict.fromkeys(self.shown + other.shown))
        streets = tuple(dict.fromkeys(self.streets + other.streets))
        names = tuple(dict.fromkeys(self.names + other.names))
        return Identity(self.addresses | other.addresses, self.parcels | other.parcels, shown, streets, names)


@lru_cache(maxsize=1024)
def _patterns(identity: Identity) -> tuple[re.Pattern, ...]:
    out = []
    for words, number, letter in identity.streets:
        out.append(_street_regex(list(words), number, letter)[0])
    for b, p in identity.parcels:
        out.append(re.compile(rf"גוש\D{{0,6}}?0*{b}\D{{0,20}}?חלק(?:ה|ות)?\D{{0,6}}?0*{p}(?!\d)"
                              rf"|(?<![\d/])0*{b}\s*/\s*0*{p}(?!\d)"))
    return tuple(out)


def name_key(raw: str | None) -> str:
    """A name as it is compared: normalized, gershayim unified, punctuation and the spacing around it dropped."""
    return " ".join(NAME_PUNCT.sub(" ", _canon(raw or "")).split())


@lru_cache(maxsize=1024)
def _name_patterns(names: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    """One pattern per name over ``name_key`` text: the prefix letters before its first word (and the article) are
    allowed, as for a street, and the name must end where a word ends."""
    out = []
    for name in names:
        words = name.split()
        if not words:
            continue
        core = _strip_prefix(words[0], 2 if len(words) > 1 else 3)
        body = r"[\s\-]+".join([rf"(?<![\w״׳])[{_PREFIXES}]{{0,3}}ה?{re.escape(core)}", *(re.escape(w) for w in words[1:])])
        out.append(re.compile(body + r"(?![\w״׳])"))
    return tuple(out)


def identifiers(raw: str | None) -> Identity:
    """The labelled property identifiers of a text (KTD7): block and parcel pairs with their labels, and addresses
    with a street label and a house number. Anything else is not an identifier."""
    if not raw:
        return Identity()
    norm = _canon(raw)
    addresses, parcels, shown, streets = set(), set(), [], []
    for m in _ADDRESS.finditer(norm):
        words = m.group(1).split()
        while words and words[-1] in _NOT_STREET:
            words.pop()
        if not words or words[0] in _NOT_STREET:
            continue
        number, letter = m.group(2).lstrip("0") or "0", m.group(3) or ""
        keep = 2 if len(words) > 1 else 3
        core = " ".join([_strip_prefix(words[0], keep), *words[1:]])
        if (core, number + letter) not in addresses:
            addresses.add((core, number + letter))
            label = re.match(rf"[{_PREFIXES}]{{0,2}}((?:{_LABELS}))", m.group(0).strip())
            shown.append(f"{label.group(1) if label else ''} {' '.join(words)} {number}{letter}".strip())
            streets.append((tuple(words), number, letter))
    for m in _BP_LABELED.finditer(norm):
        b, p = m.group(1).lstrip("0"), m.group(2).lstrip("0")
        if (b, p) not in parcels:
            parcels.add((b, p))
            shown.append(f"גוש {b} חלקה {p}")
    return Identity(frozenset(addresses), frozenset(parcels), tuple(shown), tuple(streets))


# --- derivation ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Segment:
    """A range of blocks of one context: ``number`` is the context's (1 for the file's own property), ``first`` and
    ``last`` its blocks, ``lead`` the block of its first heading (the blocks before it are its title part; None: it
    has no heading), ``start`` the position it starts at (page, top on the rendered page: ``top_of``) for a table
    row, and its pages."""

    number: int
    identity: Identity
    first: int
    last: int
    lead: int | None
    start: tuple[int, float] | None
    first_page: int | None
    last_page: int | None
    kind: str = "appraisal"  # a unit's kind (``units.KINDS``); the derivation below only finds appraisals
    certainty: str = "certain"  # the certainty of the boundary that opens it
    title: str | None = None  # a unit's title


@dataclass(frozen=True)
class Contexts:
    """A version's contexts: its segments in reading order (one segment, number 1, for a single-context file).
    ``reading_id``: the reading they were built from (None: not known), part of ``key``."""

    version_id: str
    segments: tuple[Segment, ...]
    reading_id: str | None = None

    @property
    def multi(self) -> bool:
        return len({s.number for s in self.segments}) > 1

    @property
    def subjects(self) -> list[int]:
        """The contexts that can be the asked property: every one but an appendix and a comparables part."""
        return [n for n in self.numbers if self.is_subject(n)]

    def is_subject(self, number: int) -> bool:
        return self.kind(number) not in NON_SUBJECT_KINDS

    def kind(self, number: int) -> str:
        return next(s.kind for s in self.segments if s.number == number)

    @property
    def numbers(self) -> list[int]:
        return sorted({s.number for s in self.segments})

    def identity(self, number: int) -> Identity:
        return next(s.identity for s in self.segments if s.number == number)

    def key(self, number: int) -> str:
        """A context across the reading: version, reading and number (``split_key`` reads it back)."""
        return f"{self.version_id}#{self.reading_id}#{number}" if self.reading_id else f"{self.version_id}#{number}"

    def label(self, number: int) -> str:
        """What names a context: a unit's title, then its identifiers; a part that is not an appraisal says so."""
        seg = next(s for s in self.segments if s.number == number)
        # a title that restates one of the unit's identifiers adds nothing to them: the identifiers label it
        parts = [seg.title] if seg.title and not any(x in seg.title for x in seg.identity.shown) else []
        parts += seg.identity.shown
        mark = UNIT_KIND_MARKS.get(seg.kind)
        if mark and not any(mark in p for p in parts):
            parts.insert(0, mark)
        return " · ".join(parts) or MSG_UNNAMED

    def pages(self, number: int) -> tuple[int | None, int | None]:
        own = [s for s in self.segments if s.number == number]
        firsts = [s.first_page for s in own if s.first_page]
        lasts = [s.last_page for s in own if s.last_page]
        return (min(firsts) if firsts else None, max(lasts) if lasts else None)

    def describe(self, number: int) -> str:
        """"הקשר 2: רחוב הצפצפה 7 · גוש 30874 חלקה 9 (עמוד 2)" — as the tools show it."""
        a, b = self.pages(number)
        where = "" if a is None else (f" (עמוד {a})" if a == b else f" (עמודים {a}–{b})")
        return f"הקשר {number}: {self.label(number)}{where}"

    def segment_at(self, block_index: int | None) -> Segment | None:
        if block_index is None:
            return None
        return next((s for s in self.segments if s.first <= block_index <= s.last), None)

    def at_block(self, block_index: int | None) -> int | None:
        s = self.segment_at(block_index)
        return s.number if s is not None else None

    def at_position(self, page: int | None, top: float | None) -> int | None:
        """The context of a position (a table row's page and top on the rendered page): the last segment that
        starts at or before it."""
        if page is None:
            return None
        where = (page, top if top is not None else 0.0)
        found = self.segments[0]
        for s in self.segments[1:]:
            if s.start is not None and s.start <= where:
                found = s
        return found.number

    def spanned(self, first: int | None, last: int | None) -> list[int]:
        """The contexts a block range touches, in reading order."""
        if first is None:
            return []
        last = first if last is None else last
        return list(dict.fromkeys(s.number for s in self.segments if s.first <= last and s.last >= first))

    def named(self, words: str | None) -> list[int]:
        """The subject contexts whose identifiers or names a free text names (never an appendix or a comparables
        part). A name that lies inside a longer name of another context in the text ("מגדלי הים" inside "מגדלי הים
        הצפוני") does not name its own context there."""
        if not words or not self.multi:
            return []
        by_id = [n for n in self.subjects if self.identity(n).identifier_in(words)]
        spans = {n: self.identity(n).name_spans(words) for n in self.subjects if n not in by_id}
        spans = {n: sp for n, sp in spans.items() if sp}
        every = [x for sp in spans.values() for x in sp]
        by_name = [n for n, sp in spans.items()
                   if not all(any(o[0] <= a and b <= o[1] and (a, b) != o for o in every) for a, b in sp)]
        return [n for n in self.numbers if n in by_id or n in by_name]


def split_key(key: str) -> tuple[str, int]:
    """(version id, context number) of a ``Contexts.key``."""
    return key.partition("#")[0], int(key.rpartition("#")[2])


# a numbered top-level heading: "1. מבוא", "12 סיכום" — never a sub-section ("2.1") or a lettered one
_TOP_NUMBER = re.compile(r"^\s*(\d{1,3})(?:\.(?!\d)|\)|\s)")
# a list item: a run starting with a bullet is a list (comparables), never a title block
_BULLET = re.compile(r"^\s*(?:[•●▪◦‣∙·*\-–—]|\(?\d{1,2}\)|[א-ת]\))\s*")


def _top_number(r) -> int | None:
    if r.kind != "heading" or not r.text:
        return None
    m = _TOP_NUMBER.match(r.text)
    return int(m.group(1)) if m else None


def _first_heading(rows: Sequence) -> int | None:
    return next((i for i, r in enumerate(rows) if r.kind == "heading"), None)


def top_of(r) -> float:
    """A block's top in the frame of the rendered page — the frame a table's cell boxes are stored in
    (``reader.row_top``), so ``Contexts.at_position`` compares like with like. ``document_blocks.bbox`` keeps
    pdfplumber's frame; with its page's stored geometry on the row (``mediabox``, ``cropbox``, ``rotation``,
    ``display_width``, ``display_height``, ``geometry_issue``, as ``of_version`` reads them) the box is converted
    as ``geometry.geometry_box`` does (a translation on a turned or cropped page); a page without usable geometry
    keeps the stored top, as its rows keep no cell boxes."""
    bbox = getattr(r, "bbox", None)
    try:
        box = [float(x) for x in bbox] if bbox else None
    except (TypeError, ValueError):
        return 0.0
    if not box or len(box) < 4:
        return 0.0
    if getattr(r, "mediabox", None) is not None:
        geom = PageGeometry(r.mediabox, getattr(r, "cropbox", None), getattr(r, "rotation", None),
                            getattr(r, "display_width", None), getattr(r, "display_height", None),
                            getattr(r, "geometry_issue", None))
        shown = geometry_box(box, geom)
        if shown is not None:
            return float(shown[1])
    return box[1]


def _candidates(rows: Sequence) -> list[tuple[int, Identity]]:
    """Where an identifier set is restated (row position, set): every heading, and every title block — the blocks at
    the top of a page, before its first heading, that end at a heading (on that page, or the next block)."""
    out: list[tuple[int, Identity]] = []
    first_page = next((r.page for r in rows if r.page), None)
    i = 0
    while i < len(rows):
        r = rows[i]
        if r.kind == "heading":
            out.append((i, identifiers(r.text)))
            i += 1
            continue
        page_start = r.page and r.page != first_page and (i == 0 or rows[i - 1].page != r.page)
        if not page_start:
            i += 1
            continue
        j = i
        while j < len(rows) and rows[j].page == r.page and rows[j].kind != "heading":
            j += 1
        if j < len(rows) and rows[j].kind == "heading" and not any(
                _BULLET.match(x.text or "") for x in rows[i:j]):
            ident = Identity()
            for x in rows[i:j]:
                ident = ident | identifiers(x.text)
            out.append((i, ident))
        i = max(j, i + 1)
    return out


def derive(blocks: Iterable, version_id: str = "") -> Contexts:
    """The contexts of a version's blocks (``block_index``, ``kind``, ``page``, ``bbox``, ``text``, ``status``,
    ``table_index``, and optionally their page's geometry for ``top_of``), in reading order (KTD7). Tables, pictures
    and blocks not read are never read for identifiers."""
    every = sorted(blocks, key=lambda r: r.block_index)
    if not every:
        return Contexts(version_id, (Segment(1, Identity(), 0, 0, None, None, None, None),))
    rows = [r for r in every if r.kind not in ("table", "image") and r.table_index is None
            and (r.status or "read") not in _SKIP_STATUS]
    lead = _first_heading(rows)
    title = Identity()
    # the title block: the blocks before the first heading, and that heading (a title set as a heading)
    for r in rows[:lead + 1 if lead is not None else len(rows)]:
        if lead is None and r.page and r.page != rows[0].page:
            break  # a file without headings: its first page is its title block
        title = title | identifiers(r.text)
    last_block = every[-1].block_index
    starts: list[tuple[int, Identity, int]] = [(every[0].block_index, title, 1)]  # (first block, identity, number)
    if not title.empty:
        tops = [(i, n) for i, r in enumerate(rows) if (n := _top_number(r)) is not None]
        known: list[tuple[Identity, int]] = [(title, 1)]
        last_top: dict[int, int] = {}  # context number -> its last numbered top-level heading so far
        current, number_now, seen = title, 1, 0  # ``seen``: the tops already counted
        for i, ident in _candidates(rows):
            while seen < len(tops) and tops[seen][0] < i:
                last_top[number_now] = tops[seen][1]
                seen += 1
            if not ident.restates_other(current):
                continue
            nxt = tops[seen][1] if seen < len(tops) else None
            number = next((n for k, n in known if k.shares(ident)), None)
            if nxt is None:
                continue  # no numbered heading follows: an identifier run alone never splits
            if number is None:
                # a new report starts only where the numbering restarts below the current context's last number
                if number_now not in last_top or nxt >= last_top[number_now]:
                    continue
                number = len(known) + 1
                known.append((ident, number))
            elif number not in last_top or nxt <= last_top[number]:
                continue  # back to an earlier context only where its own numbering goes on
            current, number_now = next(k for k, n in known if n == number), number
            starts.append((rows[i].block_index, current, number))
    by_index = {r.block_index: r for r in every}
    segments = []
    for n, (first, ident, number) in enumerate(starts):
        last = starts[n + 1][0] - 1 if n + 1 < len(starts) else last_block
        own = [r for r in every if first <= r.block_index <= last]
        heading = next((r.block_index for r in own if r.kind == "heading"), None)
        pages = [r.page for r in own if r.page]
        start = None
        if n > 0:
            b = by_index[first]
            start = (b.page or 0, top_of(b))
        segments.append(Segment(number, ident, first, last, heading, start, min(pages) if pages else None,
                                max(pages) if pages else None))
    return Contexts(version_id, tuple(segments))


# --- per reading -----------------------------------------------------------------------------------------------------

_CACHE: OrderedDict[tuple[str, str | None], Contexts] = OrderedDict()
_LOCK = threading.Lock()

_FIRST_BLOCKS_SQL = (
    "SELECT b.block_index, b.page, b.bbox, p.mediabox, p.cropbox, p.rotation, p.display_width, p.display_height,"
    " p.geometry_issue FROM document_blocks b LEFT JOIN pages p ON p.version_id = b.version_id AND p.page_no = b.page"
    " WHERE b.version_id = :v AND b.block_index = ANY(:b)")
_HEADINGS_SQL = "SELECT block_index FROM document_blocks WHERE kind = 'heading' AND version_id = :v ORDER BY block_index"


def _unit_identity(u) -> Identity:
    """A unit's identity: its labelled identifiers (stored with how they were written, which read again give the
    street words the matching needs) and its title and aliases as names."""
    stored = u.identifiers or {}
    shown = tuple(stored.get("shown") or ())
    ident = identifiers("\n".join(shown))
    if ident.addresses != {tuple(a) for a in stored.get("addresses") or ()} or ident.parcels != {
            tuple(p) for p in stored.get("parcels") or ()}:
        ident = Identity(frozenset(tuple(a) for a in stored.get("addresses") or ()),
                         frozenset(tuple(p) for p in stored.get("parcels") or ()), shown)
    names = tuple(dict.fromkeys(k for x in ([u.title] if u.title else []) + list(u.aliases) if (k := name_key(x))))
    return Identity(ident.addresses, ident.parcels, shown, ident.streets, names)


def from_units(units: Sequence, first_blocks: dict, headings: Sequence[int], version_id: str,
               reading_id: str | None = None) -> Contexts:
    """The contexts of a reading's units, one segment per unit in reading order. ``first_blocks``: each unit's first
    block by index (``block_index``, ``page``, ``bbox`` and its page's geometry, for ``top_of``); ``headings``: the
    indexes of the file's heading blocks, in order (a segment's ``lead`` is the first one inside its unit)."""
    segments = []
    for n, u in enumerate(units):
        at = bisect_left(headings, u.block_first)
        lead = headings[at] if at < len(headings) and headings[at] <= u.block_last else None
        b = first_blocks.get(u.block_first)
        start = (b.page or 0, top_of(b)) if n > 0 and b is not None else None
        segments.append(Segment(u.unit_no, _unit_identity(u), u.block_first, u.block_last, lead, start,
                                u.page_first, u.page_last, u.kind, u.certainty, u.title))
    return Contexts(version_id, tuple(segments), reading_id)


def _of_units(conn: Connection, version_id: str, reading_id: str | None) -> Contexts | None:
    """The contexts of the reading's stored units (built on first use: ``units.ensure``); None when the version has
    none (no reading) or they cannot be had."""
    from app.chat import units as unit_store  # units derives with this module's helpers

    try:
        with conn.begin_nested():
            found = unit_store.ensure(conn, version_id)
            if not found:
                return None
            v = UUID(version_id)
            if reading_id is None:
                reading = unit_store.reading_of(conn, v)
                reading_id = reading.reading_id if reading is not None else None
            first = {r.block_index: r for r in conn.execute(
                text(_FIRST_BLOCKS_SQL), {"v": v, "b": [u.block_first for u in found[1:]]})}
            headings = [r.block_index for r in conn.execute(text(_HEADINGS_SQL), {"v": v})]
    except Exception:  # noqa: BLE001 - the contexts of the blocks serve instead
        logger.exception("contexts of version %s could not be built from its units", version_id)
        return None
    return from_units(found, first, headings, version_id, reading_id)


def of_version(conn: Connection, version_id: UUID | str, reading_id: str | None) -> Contexts:
    """The contexts of a version's reading, built once per reading (the caller resolved the version under the user's
    permissions, in ``conn``): from its stored units (``from_units``), else — a version with no reading — derived
    from its blocks. In the fallback only headings and blocks that may hold a labelled identifier bring their text,
    never a table's or a picture's (``derive`` never reads those for identifiers), each block with its page's stored
    geometry in the same query, so a segment's start is placed on the rendered page (``top_of``)."""
    key = (str(version_id), reading_id)
    with _LOCK:
        found = _CACHE.get(key)
        if found is not None:
            _CACHE.move_to_end(key)
            return found
    found = _of_units(conn, str(version_id), reading_id)
    if found is None:
        rows = conn.execute(text(
            "SELECT b.block_index, b.kind, b.page, b.bbox, b.status, b.table_index,"
            " CASE WHEN b.kind NOT IN ('table', 'image') AND b.table_index IS NULL"
            " AND (b.kind = 'heading' OR b.text ~ :pat) THEN b.text END AS text,"
            " p.mediabox, p.cropbox, p.rotation, p.display_width, p.display_height, p.geometry_issue"
            " FROM document_blocks b LEFT JOIN pages p ON p.version_id = b.version_id AND p.page_no = b.page"
            " WHERE b.version_id = :v ORDER BY b.block_index"),
            {"v": UUID(str(version_id)), "pat": _CANDIDATE_SQL}).all()
        found = derive(rows, str(version_id))
    with _LOCK:
        _CACHE[key] = found
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)
    return found


def enforced() -> bool:
    """Whether the context checks are enforced (``chat_appraisal_context_enforced``, on by default since the contexts
    were counted per report on the regression reports — KTD7, "measured before enforced"; false shows the labels
    only)."""
    from app.config import get_settings

    return bool(getattr(get_settings(), "chat_appraisal_context_enforced", False))
