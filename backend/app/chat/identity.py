"""The identification facts of an appraisal and of its units: the lazily derived `version_identities` record, read only
from the appraisal's own identification parts (round 12 U5: R1, R2, R5, R13; KTD1, KTD2, KTD3, KTD15).

**What an identity is.** A unit's identity is what the report itself states about *which property it values*: its parcels
(block, parcel and, when written, the sub-parcel), its apartment, its address (street core and house number) and its
city. It is never inferred from a comparable, an index, a neighbour or a title the model produced.

**Where the reader reads** (`derive`, KTD2: a positive scope, never "anywhere not excluded"):

1. the unit's *title run*: the unit's first blocks up to its first heading (at most `units.RUN_MAX` blocks, and not past a
   long paragraph), read as free statements (a labelled street with a house number, a block and parcel statement);
2. blocks under an *identification or legal-status heading* ("זיהוי הנכס", "פרטי הנכס", "תיאור הנכס", "הנכס הנדון",
   "נשוא השומה", "מיקום", "רישום", "מצב משפטי", "נסח"), read as free statements; the heading may be a real heading or a
   short paragraph that is the heading's words and nothing else;
3. *labelled lines and cells* anywhere else (`label: value`: גוש, חלקה, תת חלקה, דירה, כתובת, עיר, ישוב/יישוב), and the
   cells of a key-value table or of a one-row table whose header names the labels.

It never reads a comparables or appendix section (by its heading or any ancestor in the section path), a table with
several data rows (a comparables table, a cover or an index table), a sentence about an adjacent property ("גובלת",
"סמוכה", "שכנה"), or a bare slash pair. A fact is stored per *block*; its owner is found at read time (below), so a
re-derivation of the units needs no rewrite and `document_units.identifiers` and `UNITS_VERSION` stay untouched.

**Parcels** expand lists and ranges ("חלקות 24, 40, 42-44", "ו-" joiners; at most `MAX_PARCELS` per statement) and
pair each parcel with the nearest block before it in the statement, or in the last few blocks. A sub-parcel written in the
statement of a single parcel, or on a labelled line right after it, belongs to that parcel; next to several parcels it
belongs to none and is dropped (it would be a guess).

**Cities** (KTD3). A labelled city (`עיר:`, `ישוב:`, a city column) is accepted without a vocabulary. A city written after
a street ("רחוב X 7, CITY", "... ב CITY") is stored as unlabelled and is accepted at read time only when it is in
`answering.places.KNOWN_CITIES` or equals a labelled city of the same unit. "שדרות" is a street label before the street
and a city only after the house number. A unit that states two different subject cities links as cityless (`link_city`
None; `cities` keeps the raw set). A single-unit file may take the header city (`answering.metadata.report_headers`) when
the body states none and it is exactly one city; a file with several subject units never donates one, and
`VersionMetadata.cities` (whose fallback mixes in comparables' cities) is never used.

**The record** (`version_identities`): one row per fact, or a marker row (`block_index` -1, kind 'marker') that says the
derivation found none, kept per `IDENTITY_VERSION`; older rows are kept, never read. Only appraisal versions derive
(`APPRAISALS_SQL`). Every write goes through the caller's transaction (RLS: a user outside the document's group neither
reads nor builds a row), in a savepoint, so a transaction that cannot write still gets its identities. A document
re-read deletes its rows (`pipeline._delete_outputs`); nothing else ever deletes them.

**The owning unit is found at read time** from the current `document_units` (`assign`): a fact belongs to the unit whose
block range holds its block. A fact before the first unit, in a unit that is not a subject (an appendix, a comparables
part, a cover) and in an *uncertain unit edge* belongs to none. The edge of a `probable` boundary is the pair of pages that
touch it (the previous unit's last page and the opening unit's first page; without pages, `EDGE_BLOCKS` blocks each
side): the cut may be off by a page, so what stands there is not attributed to either side.

The scope per turn: `cache` is a dict the caller keeps for one turn; a version's facts, identities and units are read once
into it."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from itertools import islice
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.entities import _canon, _strip_prefix
from app.answering.metadata import place_matches, report_headers
from app.answering.places import KNOWN_CITIES
from app.appraisal.normalize import normalize_place, parse_block_parcel
from app.chat import contexts
from app.chat import units as unit_store
from app.chat.dates import (
    DOCUMENT_SCOPE,
    MARKER_BLOCK,
    VISIBLE_SQL,
    is_comparables_table,
    owner_of_block,
    units_of,
)
from app.extraction.hebrew import strip_bidi_controls
from app.professional.sections import flight, landed

logger = logging.getLogger(__name__)

IDENTITY_VERSION = "identity-v1"

PARCEL, ADDRESS, CITY, APARTMENT, MARKER = "parcel", "address", "city", "apartment", "marker"
TITLE, IDENTIFICATION, LABEL, TABLE = "title", "identification", "label", "table"

MAX_PARCELS = 30        # parcels one statement expands to
EDGE_BLOCKS = 2         # blocks each side of an uncertain cut that are withheld when the reading has no pages
CARRY_BLOCKS = 3        # blocks a block (or a parcel) is remembered for by a later labelled line
LONG_PROSE = 160        # a paragraph longer than this ends the title run
TAIL_WORDS = 3          # words of a city written after a street

# --- the vocabulary (KTD2); never question words ---------------------------------------------------------------------

# headings that open an identification or legal-status section
IDENTIFICATION_HEADINGS = ("זיהוי הנכס", "פרטי הנכס", "תיאור הנכס", "הנכס הנדון", "נשוא השומה", "נשוא חוות הדעת",
                           "נשוא חוות דעת", "מיקום", "רישום", "מצב משפטי", "נסח")
# headings that close the reading: appendices and comparables (the unit vocabulary, one place)
EXCLUDED_HEADINGS = (*unit_store.APPENDIX_WORDS, *unit_store.COMPARABLES_WORDS, "נתוני עסקאות")
CITY_LABELS = frozenset({"עיר", "ישוב", "יישוב", "עיר ישוב", "ישוב עיר", "עיר יישוב"})
ADDRESS_LABELS = frozenset({"כתובת", "הכתובת", "כתובת הנכס", "כתובת הנכס הנדון", "כתובת הנכס נשוא השומה", "מען",
                            "מען הנכס", "רחוב"})
# header cells a one-row identification table may carry besides the labels; a price, an area or a date column makes the
# table a list of transactions
NEUTRAL_HEADERS = frozenset({"מס", "מספר", "סוג נכס", "סוג", "שכונה", "ייעוד", "יעוד"})
PARCEL_LABELS = frozenset({"גוש", "חלקה", "חלקות", "תת חלקה", "תת חלקות", "גוש וחלקה", "גוש חלקה", "גוש ו חלקה",
                           "גוש חלקות", "גושים"})
APARTMENT_LABELS = frozenset({"דירה", "דירה מספר", "מספר דירה", "דירה מס", "מס דירה"})

_KNOWN = frozenset(c for c in (normalize_place(x) for x in KNOWN_CITIES) if c)

# --- the pieces of a statement -----------------------------------------------------------------------------------------

_N = r"\d{1,5}(?!\d)(?![.,]\d)"      # a parcel number; a thousands figure ("1,200") is no second parcel
_LIST = rf"{_N}(?:\s*[-–־]\s*{_N})?(?:\s*(?:,|ו[-־]?)\s*{_N}(?:\s*[-–־]\s*{_N})?)*"
_NO = r"(?:מס(?:פר|׳|')?\.?\s*)?"
_BLOCK = re.compile(rf"(?<![א-ת])[וב]{{0,2}}גוש(?:ים)?\s*[:：]?\s*{_NO}(\d{{3,6}})(?!\d)")
_PARCEL = re.compile(rf"(?<![א-ת])[וב]{{0,2}}חלק(?:ה|ות)\s*[:：]?\s*{_NO}({_LIST})")
_SUB = re.compile(rf"(?<![א-ת])[וב]{{0,2}}תת[\s\-־]*חלק(?:ה|ות)?\s*[:：]?\s*{_NO}(\d{{1,4}})(?!\d)")
_PARCEL_START = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?(?:גוש|חלק(?:ה|ות)|תת[\s\-־]*חלק)")
_ADJACENT = re.compile(r"גובל|סמוכ|שכנ|בצמוד|צמוד")
_LABELLED = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?(?P<label>[^:：|\n]{1,40}?)\s*[:：]\s*(?P<value>\S.*)$")
_APARTMENT_LINE = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?(?:דירה(?:\s+מס(?:פר|׳|')?\.?)?|מספר\s+דירה|מס['׳.]\s*דירה)"
                             r"\s*[:：]?\s*(\d{1,3}[א-ת]?)\s*[.]?\s*$")
_APARTMENT_TAIL = re.compile(r"^\s*,?\s*דירה(?:\s+מס(?:פר|׳|')?\.?)?\s*[:：]?\s*(\d{1,3}[א-ת]?)(?![\dא-ת])")
_WORD = r"[א-ת][א-ת\-־״׳]*"
_WORDS = re.compile(rf"{_WORD}(?:\s+{_WORD}){{0,{TAIL_WORDS - 1}}}")
_SECTION_NUMBER = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[א-ת][.)])\s*")
_SENTENCE = re.compile(r"(?<=[.;!?])\s+")
_SPLIT_CITY = re.compile(r"[,;|(]")
_MANY_CITIES = re.compile(r"[,;/|]|(?<![א-ת])ו[-־]?[א-ת]|(?<![א-ת])או(?![א-ת])")


@dataclass(frozen=True)
class Fact:
    """One identification fact of a block. ``kind`` is `parcel` (``block``, ``parcel``, ``sub_parcel``), `address`
    (``street`` core, ``number``), `city` (``city``; ``labelled`` when a label named it) or `apartment`
    (``apartment``); ``written`` the words as the document writes them and ``section`` where they were read. ``unit_no``
    is set by `assign` (None before it)."""
    block_index: int
    kind: str
    section: str
    written: str
    block: str | None = None
    parcel: str | None = None
    sub_parcel: str | None = None
    apartment: str | None = None
    street: str | None = None
    number: str | None = None
    city: str | None = None
    labelled: bool = False
    page: int | None = None
    unit_no: int | None = None


@dataclass(frozen=True)
class UnitIdentity:
    """What a unit says it is. ``parcels`` are `(block, parcel, sub_parcel)` (a parcel written both with and without a
    sub-parcel keeps only the specific form); ``addresses`` are `(street core, number)`; ``cities`` is the raw set of the
    unit's own subject cities (two different ones are a conflict: it links as cityless); ``header_city`` is the single
    city the report's header donates to a one-unit file whose body states none. `identified` is the R13 test: the unit
    names a parcel or an address."""
    unit_no: int
    parcels: frozenset = frozenset()
    apartments: frozenset = frozenset()
    addresses: frozenset = frozenset()
    cities: frozenset = frozenset()
    header_city: str | None = None

    @property
    def apartment(self) -> str | None:
        return next(iter(self.apartments)) if len(self.apartments) == 1 else None

    @property
    def identified(self) -> bool:
        return bool(self.parcels or self.addresses)

    @property
    def city_conflict(self) -> bool:
        return len(_collapse(self.cities)) > 1

    @property
    def link_city(self) -> str | None:
        """The city a link may rely on: the unit's single city (the most specific spelling), else the header city; None
        when the unit states none or two different ones."""
        if not self.cities:
            return self.header_city
        groups = _collapse(self.cities)
        return max(groups[0], key=len) if len(groups) == 1 else None


def unpad(number: str) -> str:
    """A block, parcel, sub-parcel or house number without its leading zeros ("0" stays "0")."""
    return number.lstrip("0") or "0"


def same_place(a: str, b: str) -> bool:
    """Whether two normalized city names name one place: equal, or two names the vocabulary knows that one extends with a
    hyphen ("תל אביב" and "תל אביב-יפו"). `metadata.place_matches` alone is not used: it takes any hyphen extension for the
    same place, which would make "עיר-סינתטית" and "עיר-סינתטית-ב" one city (R1: two cities are never one property)."""
    return a == b or (a in _KNOWN and b in _KNOWN and place_matches(a, frozenset({b})))


def _collapse(cities: Iterable[str]) -> list[list[str]]:
    """The cities grouped by naming one place (`same_place`)."""
    groups: list[list[str]] = []
    for c in sorted(set(cities), key=lambda x: (len(x), x)):
        for g in groups:
            if any(same_place(c, x) for x in g):
                g.append(c)
                break
        else:
            groups.append([c])
    return groups


# --- the reader ------------------------------------------------------------------------------------------------------------

def _light(raw: str | None) -> str:
    """The text as the statements are read: width forms folded, bidi controls and marks dropped, spaces collapsed. It
    keeps thousands separators (`base_normalize` drops them, which would turn "1,200" into a parcel number)."""
    t = unicodedata.normalize("NFKC", strip_bidi_controls(raw or ""))
    t = re.sub(r"[֑-ׇ]", "", t)
    return re.sub(r"[^\S\n]+", " ", t).strip()


def _label_of(raw: str) -> str:
    return " ".join(re.sub(r"[\-־/.]", " ", _light(raw).rstrip(":：")).split())


def _label_kind(raw: str) -> str | None:
    label = _label_of(raw)
    if label in CITY_LABELS:
        return CITY
    if label in ADDRESS_LABELS:
        return ADDRESS
    if label in PARCEL_LABELS:
        return PARCEL
    if label in APARTMENT_LABELS:
        return APARTMENT
    return None


def _section_of(raw: str) -> str | None:
    """Whether a heading opens an identification section (`identification`), closes the reading (`excluded`), or neither."""
    head = " ".join(_canon(_SECTION_NUMBER.sub("", _light(raw))).replace("׳", "'").split())
    if not head:
        return None
    if any(w in head for w in EXCLUDED_HEADINGS) and len(head.split()) <= 8:
        return "excluded"
    if any(head.startswith(_canon(w)) for w in IDENTIFICATION_HEADINGS):
        return "identification"
    return None


@dataclass(frozen=True)
class _Row:
    index: int
    kind: str
    text: str
    page: int | None
    path: tuple[str, ...]


def _rows(blocks: Iterable) -> list[_Row]:
    out = []
    for b in blocks:
        kind = getattr(b, "kind", "paragraph")
        if kind == "image":
            continue
        out.append(_Row(int(b.block_index), kind, (getattr(b, "text", None) or ""), getattr(b, "page", None),
                        tuple(getattr(b, "section_path", None) or ())))
    return out


def _heading_like(row: _Row) -> bool:
    """A paragraph that is a heading's words and nothing else (a PDF reading may not mark its headings)."""
    t = _light(row.text)
    return row.kind in ("paragraph", "textbox") and "\n" not in t and len(t.split()) <= 8 and ":" not in t \
        and _section_of(t) is not None


def _title_runs(rows: Sequence[_Row], units: Sequence) -> set[int]:
    """The block indexes of the unit title runs: from each unit's first block through at most `RUN_MAX` blocks, ending
    before the first heading after the first block and before a long paragraph. Without units, the file is one run."""
    starts = sorted({u.block_first for u in units}) or ([rows[0].index] if rows else [])
    ends = {u.block_first: u.block_last for u in units}
    run: set[int] = set()
    for s in starts:
        taken = 0
        for r in rows:
            if r.index < s or r.index > ends.get(s, r.index):
                continue
            if taken and (r.kind == "heading" or len(_light(r.text)) > LONG_PROSE):
                break
            run.add(r.index)
            taken += 1
            if taken >= unit_store.RUN_MAX:
                break
    return run


def _street_matches(raw: str) -> tuple[list[tuple[str, str, str, int]], str]:
    """The labelled streets with a house number in a text: (street core, number with its letter, as written, end), and the
    normalized text the ends are offsets of. The regex and the core are those of `contexts.identifiers`."""
    norm = _canon(raw)
    out = []
    for m in contexts._ADDRESS.finditer(norm):
        words = m.group(1).split()
        while words and words[-1] in contexts._NOT_STREET:
            words.pop()
        if not words or words[0] in contexts._NOT_STREET:
            continue
        number, letter = unpad(m.group(2)), m.group(3) or ""
        keep = 2 if len(words) > 1 else 3
        core = " ".join([_strip_prefix(words[0], keep), *words[1:]])
        out.append((core, number + letter, m.group(0).strip(), m.end()))
    return out, norm


def _known_city(words: list[str]) -> tuple[str, int] | None:
    """The longest known city that opens the words, with its word count (a prefix letter "ב" is tried off too)."""
    for n in range(min(len(words), TAIL_WORDS), 0, -1):
        cand = normalize_place(" ".join(words[:n]))
        if cand in _KNOWN:
            return cand, n
        if words[0].startswith("ב") and len(words[0]) > 2:
            cand = normalize_place(" ".join([words[0][1:], *words[1:n]]))
            if cand in _KNOWN:
                return cand, n
    return None


class _Reader:
    """The facts of the rows, in order, with the small state a statement carries over to the next lines: the nearest
    block and the last single parcel (for a sub-parcel on its own line)."""

    def __init__(self) -> None:
        self.facts: list[Fact] = []
        self._seen: set = set()
        self._block: tuple[str, int] | None = None
        self._last_parcel: tuple[int, int] | None = None   # (index in facts, row index) of the last single parcel
        self.row: _Row | None = None
        self.section = LABEL

    def reset(self) -> None:
        self._block = self._last_parcel = None

    def add(self, **kw) -> int | None:
        row = self.row
        fact = Fact(block_index=row.index, section=self.section, page=row.page, **kw)
        key = (fact.kind, fact.block, fact.parcel, fact.sub_parcel, fact.apartment, fact.street, fact.number,
               fact.city, fact.labelled)
        if (row.index, key) in self._seen:
            return None
        self._seen.add((row.index, key))
        self.facts.append(fact)
        return len(self.facts) - 1

    # parcels

    def parcels(self, raw: str) -> None:
        """The block and parcel statements of a line (sentences about an adjacent property are skipped)."""
        for sentence in _SENTENCE.split(_light(raw)):
            if _ADJACENT.search(sentence):
                continue
            self._statement(sentence)

    def _statement(self, sentence: str) -> None:
        subs = [(m.start(), m.end(), m.group(1)) for m in _SUB.finditer(sentence)]
        taken = [(a, b) for a, b, _ in subs]
        items = [(m.start(), "block", m) for m in _BLOCK.finditer(sentence)]
        items += [(m.start(), "parcel", m) for m in _PARCEL.finditer(sentence)
                  if not any(a <= m.start() < b for a, b in taken)]
        items += [(a, "sub", g) for a, _, g in subs]
        group: list[int] = []     # indexes (in facts) of the parcels of the statement's last parcel clause
        for _, what, m in sorted(items, key=lambda x: x[0]):
            if what == "block":
                self._block = (unpad(m.group(1)), self.row.index)
            elif what == "parcel":
                group = []
                block = self._block if self._block and self.row.index - self._block[1] <= CARRY_BLOCKS else None
                if block is None:
                    continue
                for p in islice(_expand(m.group(1)), MAX_PARCELS):
                    i = self.add(kind=PARCEL, block=block[0], parcel=p, written=m.group(0).strip())
                    if i is not None:
                        group.append(i)
                self._last_parcel = (group[0], self.row.index) if len(group) == 1 else None
            elif len(group) == 1:   # the sub-parcel of the statement's single parcel
                self._set_sub(group[0], m)
            elif not group and self._last_parcel and self.row.index - self._last_parcel[1] <= CARRY_BLOCKS:
                self._set_sub(self._last_parcel[0], m)   # on its own line, right after the single parcel
            # next to several parcels, or with no parcel before it: dropped (a guess)

    def _set_sub(self, index: int, sub: str) -> None:
        self.facts[index] = replace(self.facts[index], sub_parcel=unpad(sub))

    # addresses and cities

    def address(self, raw: str, *, fielded: bool) -> None:
        """The labelled streets of a text. A fielded value (`כתובת: הדולב 7`) may omit the street label."""
        found, norm = _street_matches(raw)
        if not found and fielded:
            found, norm = _street_matches("רחוב " + _light(raw))
        for core, number, written, end in found:
            self.add(kind=ADDRESS, street=core, number=number, written=written)
            rest = norm[end:]
            if (apt := _APARTMENT_TAIL.match(rest)) is not None:
                self.add(kind=APARTMENT, apartment=apt.group(1), written=apt.group(0).strip(" ,"))
                continue
            self._tail_city(rest)

    def _tail_city(self, rest: str) -> None:
        """The city written after a street: after a comma, with the prefix "ב", or a known city name."""
        sep = re.match(r"\s*,\s*|\s+", rest)
        words_m = _WORDS.match(rest[sep.end():]) if sep else None
        if words_m is None:
            return
        words = words_m.group(0).split()
        known = _known_city(words)
        if known is not None:
            self.add(kind=CITY, city=known[0], written=" ".join(words[:known[1]]))
        elif "," in sep.group(0):
            self.add(kind=CITY, city=normalize_place(" ".join(words)), written=" ".join(words))
        elif words[0].startswith("ב") and len(words[0].lstrip("ב-־")) > 1:   # "בעיר-סינתטית": the prefix is not the name
            bare = [words[0].lstrip("ב-־") if words[0][1:2] in "-־" else words[0][1:], *words[1:]]
            self.add(kind=CITY, city=normalize_place(" ".join(bare)), written=" ".join(words))

    def city(self, raw: str) -> None:
        value = _SPLIT_CITY.split(_light(raw))[0].strip(" .")
        if value and not value.isdigit() and len(value) <= 40:
            self.add(kind=CITY, city=normalize_place(value), labelled=True, written=value)

    def apartment(self, value: str) -> None:
        self.add(kind=APARTMENT, apartment=value, written=value)

    # one labelled pair (a line "label: value" or a table cell under a header)

    def pair(self, kind: str, label: str, value: str) -> None:
        if kind == CITY:
            self.city(value)
        elif kind == ADDRESS:
            if not _ADJACENT.search(value):
                self.address(value, fielded=True)
        elif kind == APARTMENT:
            if (m := re.fullmatch(r"\s*(\d{1,3}[א-ת]?)\s*[.]?\s*", _light(value))):
                self.apartment(m.group(1))
        else:
            before = len(self.facts)
            self.parcels(f"{_label_of(label)} {value}")
            if len(self.facts) == before and (m := parse_block_parcel(_light(value))) != (None, None, None):
                b, p, s = m
                if b and p and not _ADJACENT.search(value):
                    self._block = (unpad(b), self.row.index)
                    i = self.add(kind=PARCEL, block=unpad(b), parcel=unpad(p),
                                 sub_parcel=unpad(s) if s else None, written=_light(value))
                    self._last_parcel = (i, self.row.index) if i is not None and not s else None

    def line(self, line: str, *, free: bool) -> None:
        """One line of a text block: a labelled field, a parcel line, and in a title run or an identification section any
        free statement."""
        if (a := _APARTMENT_LINE.match(_light(line))) is not None:
            self.apartment(a.group(1))
            return
        lab = _LABELLED.match(_light(line))
        kind = _label_kind(lab.group("label")) if lab else None
        if kind is not None:
            self.pair(kind, lab.group("label"), lab.group("value"))
        elif _PARCEL_START.match(_light(line)):
            self.parcels(line)
        if free and kind is None:
            for sentence in _SENTENCE.split(_light(line)):
                if _ADJACENT.search(sentence):
                    continue
                self.address(sentence, fielded=False)
                if not _PARCEL_START.match(sentence):
                    self._statement(sentence)

    def table(self, raw: str) -> None:
        """A key-value table (rows of label and value) or a table of one header row and one data row; any table with
        more data rows is a list (comparables, a cover, an index) and gives nothing."""
        lines = [[c.strip() for c in ln.split("|")] for ln in raw.split("\n") if ln.strip()]
        if lines and all(len(ln) == 2 for ln in lines):
            for label, value in lines:
                if (kind := _label_kind(label)) is not None and value:
                    self.pair(kind, label, value)
        elif (len(lines) == 2 and len(lines[0]) >= 2 and len(lines[0]) == len(lines[1])
              and all(_label_kind(c) or _label_of(c) in NEUTRAL_HEADERS for c in lines[0] if c)
              and not is_comparables_table(lines[0])):
            for label, value in zip(*lines, strict=True):
                if (kind := _label_kind(label)) is not None and value:
                    self.pair(kind, label, value)


def _expand(listing: str) -> Iterable[str]:
    """The parcels of "24, 40, 42-44" (a reversed or huge range gives its two ends only)."""
    for part in re.split(r"\s*(?:,|ו[-־]?)\s*", listing.strip()):
        m = re.fullmatch(r"(\d+)\s*[-–־]\s*(\d+)", part)
        if m is None:
            if part.isdigit():
                yield unpad(part)
            continue
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi and hi - lo < MAX_PARCELS:
            yield from (str(n) for n in range(lo, hi + 1))
        elif lo <= hi:
            yield from (str(n) for n in range(lo, lo + MAX_PARCELS))
        else:
            yield from (str(lo), str(hi))


def derive(blocks: Iterable, units: Sequence = ()) -> list[Fact]:
    """The identification facts of stored blocks (anything with ``block_index``, ``kind``, ``text`` and, when known,
    ``page`` and ``section_path``), in block order. Pure: no database, no model. ``units`` give the title runs and where
    a section ends; a fact is stored per block and assigned to a unit at read time (`assign`)."""
    rows = _rows(blocks)
    runs = _title_runs(rows, units)
    starts = {u.block_first for u in units}
    reader = _Reader()
    state: str | None = None
    for row in rows:
        if row.index in starts:
            reader.reset()
            state = None
        heading = row.kind == "heading" or _heading_like(row)
        if heading:
            opened = _section_of(row.text)
            if opened is not None or row.kind == "heading":
                state = opened
        excluded = state == "excluded" or any(_section_of(p) == "excluded" for p in row.path)
        if excluded:
            continue
        in_run = row.index in runs
        if heading and not in_run:
            continue
        identification = state == "identification" or any(_section_of(p) == "identification" for p in row.path)
        reader.row = row
        reader.section = TITLE if in_run else IDENTIFICATION if identification else LABEL
        if row.kind == "table":
            reader.section = TABLE
            reader.table(_light(row.text))
            continue
        free = in_run or identification
        for line in row.text.split("\n"):
            if line.strip():
                reader.line(line, free=free)
    return reader.facts


# --- the units: assignment and the unit's identity -----------------------------------------------------------------------

def is_subject(unit) -> bool:
    """Whether a unit is an appraisal of a property of the file (not an appendix, a comparables part or a cover)."""
    return unit.kind not in contexts.NON_SUBJECT_KINDS and unit.kind != unit_store.FRONT_MATTER


def _in_edge(ordered: Sequence, owner: int, fact: Fact) -> bool:
    """Whether the fact stands in the edge of a `probable` boundary (see the module docstring)."""
    for k, u in enumerate(ordered):
        if u.certainty != unit_store.PROBABLE or k == 0:
            continue
        prev = ordered[k - 1]
        if owner == u.unit_no:
            page_edge = fact.page is not None and u.page_first is not None and fact.page == u.page_first
            block_edge = (fact.page is None or u.page_first is None) and fact.block_index < u.block_first + EDGE_BLOCKS
            if page_edge or block_edge:
                return True
        if owner == prev.unit_no:
            page_edge = fact.page is not None and prev.page_last is not None and fact.page == prev.page_last
            block_edge = (fact.page is None or prev.page_last is None) and fact.block_index > prev.block_last - EDGE_BLOCKS
            if page_edge or block_edge:
                return True
    return False


def assign(found: Iterable[Fact], units: Sequence) -> list[Fact]:
    """The facts with the unit that holds each block now. A version with no units is one scope (`DOCUMENT_SCOPE`). A fact
    before the first unit, in a unit that is not a subject and in an uncertain unit edge belongs to none, and is left out."""
    ordered = sorted(units, key=lambda u: u.block_first)
    by_no = {u.unit_no: u for u in units}
    out = []
    for f in sorted(found, key=lambda x: (x.block_index, x.kind, x.written)):
        owner = owner_of_block(units, f.block_index)
        if owner is None or (units and not is_subject(by_no[owner])) or (units and _in_edge(ordered, owner, f)):
            continue
        out.append(replace(f, unit_no=owner))
    return out


def single_city(header: str | None) -> str | None:
    """The header's city when it names exactly one."""
    value = normalize_place(header)
    return value if value and not _MANY_CITIES.search(value) else None


def _unit_cities(facts: Sequence[Fact]) -> frozenset:
    """The unit's subject cities: labelled ones; an unlabelled one that is a known city or that a labelled city of the unit
    names (the unit's own spelling wins)."""
    labelled = {f.city for f in facts if f.kind == CITY and f.labelled and f.city}
    out = set(labelled)
    for f in facts:
        if f.kind != CITY or f.labelled or not f.city:
            continue
        if f.city in _KNOWN:
            out.add(f.city)
        elif (named := next((c for c in sorted(labelled, key=len, reverse=True)
                             if f.city == c or f.city.startswith(c + " ")), None)) is not None:
            out.add(named)
    return frozenset(out)


def unit_identities(assigned: Sequence[Fact], units: Sequence, *, header_city: str | None = None) -> dict[int, UnitIdentity]:
    """The identity of each subject unit (a unit that names nothing is in the result, not identified), from the assigned
    facts. ``header_city`` is donated only to a version with exactly one subject unit, and only when that unit states no
    city of its own."""
    subject = [u.unit_no for u in units if is_subject(u)] if units else [DOCUMENT_SCOPE]
    by_unit: dict[int, list[Fact]] = defaultdict(list)
    for f in assigned:
        by_unit[f.unit_no].append(f)
    header = single_city(header_city) if len(subject) == 1 else None
    out = {}
    for no in subject:
        facts = by_unit.get(no, [])
        parcels = {(f.block, f.parcel, f.sub_parcel) for f in facts if f.kind == PARCEL}
        parcels -= {(b, p, None) for b, p, s in parcels if s is None and any(b == b2 and p == p2 and s2 for b2, p2, s2 in parcels)}
        cities = _unit_cities(facts)
        out[no] = UnitIdentity(
            no, frozenset(parcels), frozenset(f.apartment for f in facts if f.kind == APARTMENT),
            frozenset((f.street, f.number) for f in facts if f.kind == ADDRESS), cities,
            header if not cities else None)
    return out


# --- the store ---------------------------------------------------------------------------------------------------------------

_ROWS_SQL = ("SELECT version_id, block_index, page, kind, block, parcel, sub_parcel, apartment, street, number, city,"
             " labelled, written, section FROM version_identities WHERE version_id = ANY(:v) AND identity_version = :iv"
             " ORDER BY version_id, block_index, kind, written")
_BLOCKS_SQL = ("SELECT block_index, kind, text, page, section_path FROM document_blocks WHERE version_id = :v"
               " AND kind <> 'image' ORDER BY block_index")
_INSERT = ("INSERT INTO version_identities (office_id, document_id, version_id, identity_version, block_index, page,"
           " kind, block, parcel, sub_parcel, apartment, street, number, city, labelled, written, section) VALUES"
           " (app_office(), :d, :v, :iv, :b, :pg, :k, :bl, :pa, :sp, :ap, :st, :nu, :ci, :lb, :w, :se)")


def _stored(conn: Connection, version_ids: Sequence[UUID]) -> dict[UUID, list[Fact]]:
    """The versions' rows of the current `IDENTITY_VERSION` (a version with a marker row only maps to ``[]``)."""
    out: dict[UUID, list[Fact]] = {}
    for r in conn.execute(text(_ROWS_SQL), {"v": list(version_ids), "iv": IDENTITY_VERSION}):
        out.setdefault(r.version_id, [])
        if r.kind != MARKER and r.block_index != MARKER_BLOCK:
            out[r.version_id].append(Fact(r.block_index, r.kind, r.section, r.written or "", r.block, r.parcel,
                                          r.sub_parcel, r.apartment, r.street, r.number, r.city, r.labelled, r.page))
    return out


def prime_units(conn: Connection, version_ids: Iterable[UUID | str], cache: dict) -> None:
    """The stored units of many appraisal versions in one query, into the cache keys `units_of` reads, before their
    identities are read together; a version whose units the cache holds, and one with none stored, is left alone."""
    todo = sorted((v for v in dict.fromkeys(UUID(str(v)) for v in version_ids) if ("units", v) not in cache), key=str)
    for v, units in unit_store.stored_many(conn, todo).items():
        cache[("units", v)] = tuple(units)


def _derive_and_store(conn: Connection, version_id: UUID, document_id: UUID, cache: dict) -> tuple[Fact, ...]:
    """Derive the version's identification facts from its stored blocks and store them (or the marker row) once, and return
    them assigned to the current units (what `facts` caches): a second caller in the process waits for the first, and
    across processes the advisory lock makes the second writer find the first's rows. A transaction that cannot write
    still gets its facts."""
    blocks = conn.execute(text(_BLOCKS_SQL), {"v": version_id}).all()
    units = units_of(conn, version_id, cache)
    derived = derive(blocks, units)
    lock = flight("identity", str(version_id), IDENTITY_VERSION)
    try:
        with lock:
            try:
                with conn.begin_nested():
                    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                                 {"k": f"identity:{version_id}:{IDENTITY_VERSION}"})
                    existing = _stored(conn, [version_id]).get(version_id)
                    if existing is not None:   # another process wrote them while this one waited for the lock
                        return tuple(assign(existing, units))
                    params = {"d": document_id, "v": version_id, "iv": IDENTITY_VERSION}
                    rows = [{"b": f.block_index, "pg": f.page, "k": f.kind, "bl": f.block, "pa": f.parcel,
                             "sp": f.sub_parcel, "ap": f.apartment, "st": f.street, "nu": f.number, "ci": f.city,
                             "lb": f.labelled, "w": f.written, "se": f.section} for f in derived]
                    rows = rows or [{"b": MARKER_BLOCK, "pg": None, "k": MARKER, "bl": None, "pa": None, "sp": None,
                                     "ap": None, "st": None, "nu": None, "ci": None, "lb": False, "w": None,
                                     "se": None}]
                    conn.execute(text(_INSERT), [params | row for row in rows])
            except Exception:  # noqa: BLE001 - a read-only transaction or a vanished version: the facts still serve
                logger.info("identities of version %s not stored", version_id, exc_info=True)
            return tuple(assign(derived, units))
    finally:
        landed("identity", str(version_id), IDENTITY_VERSION, lock)


def ensure(conn: Connection, version_id: UUID | str, document_id: UUID | str, *, cache: dict | None = None) -> tuple[Fact, ...]:
    """The version's assigned facts, derived and stored when the version has none of the current `IDENTITY_VERSION` (the
    publish hook next to `units.replace`, and the warm-up script)."""
    cache = {} if cache is None else cache
    v, d = UUID(str(version_id)), UUID(str(document_id))
    stored = _stored(conn, [v]).get(v)
    if stored is not None:
        return tuple(assign(stored, units_of(conn, v, cache)))
    return _derive_and_store(conn, v, d, cache)


def derive_at_publish(conn: Connection, version_id: UUID | str, document_id: UUID | str) -> None:
    """The publish hook, next to `units.replace` (KTD15): the version's identities are derived and stored in the publish
    transaction. They are a derived cache, so a failure here never fails the publish: the lazy read derives them again."""
    try:
        with conn.begin_nested():  # a database error in the reads rolls back to here: the publish transaction stays usable
            ensure(conn, version_id, document_id)
    except Exception:  # noqa: BLE001
        logger.warning("identities of version %s not derived at publish", version_id, exc_info=True)


def facts(conn: Connection, version_ids: Iterable[UUID | str], *, cache: dict | None = None) -> dict[UUID, tuple[Fact, ...]]:
    """The identification facts of each version, assigned to its current units, derived and stored on first use. A version
    that is not an appraisal, does not exist or is not visible to the caller is not in the result. ``cache`` is the turn's
    dict: a version already in it costs no query."""
    cache = {} if cache is None else cache
    ids = list(dict.fromkeys(UUID(str(v)) for v in version_ids))
    # derived in a stable order: each derivation holds its version's advisory lock until the transaction ends, so two
    # concurrent cold turns over overlapping versions must take them in the same order to never wait on each other
    missing = sorted((v for v in ids if ("identity", v) not in cache), key=str)
    if missing:
        visible = {r.version_id: r.document_id for r in conn.execute(text(VISIBLE_SQL), {"v": missing})}
        prime_units(conn, list(visible), cache)
        stored = _stored(conn, list(visible))
        for v in missing:
            if v not in visible:
                cache[("identity", v)] = None
                continue
            found = stored.get(v)
            cache[("identity", v)] = (tuple(assign(found, units_of(conn, v, cache))) if found is not None
                                      else _derive_and_store(conn, v, visible[v], cache))
    return {v: cache[("identity", v)] for v in ids if cache.get(("identity", v)) is not None}


def identities(conn: Connection, version_ids: Iterable[UUID | str], *,
               cache: dict | None = None) -> dict[UUID, dict[int, UnitIdentity]]:
    """The identity of each subject unit of each version (a version the caller cannot read is not in the result). The
    header city of the one-unit files whose body names no city is looked up for all of them in one query (round 12)."""
    cache = {} if cache is None else cache
    got = facts(conn, version_ids, cache=cache)
    bases: dict[UUID, tuple[dict[int, UnitIdentity], tuple]] = {}
    for v, assigned in got.items():
        if ("identities", v) not in cache:
            units = units_of(conn, v, cache)
            bases[v] = (unit_identities(assigned, units), (assigned, units))
    # the header city of every one-unit file whose body names none, read together (one query, however many such files)
    needing = [v for v, (base, _) in bases.items() if len(base) == 1 and not next(iter(base.values())).cities]
    headers = report_headers(conn, needing) if needing else {}
    for v, (base, (assigned, units)) in bases.items():
        header = (headers.get(v) or {}).get("city")
        header = header if isinstance(header, str) else None
        cache[("identities", v)] = unit_identities(assigned, units, header_city=header) if header else base
    return {v: cache[("identities", v)] for v in got}
