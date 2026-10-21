"""Scopes below the property: the plan, parcel, lot, contract, alternative, stage or state a datum is written under (round 20 U3,
KTD3; R9-R13).

A property is right when a datum is still on the wrong sub-referent: the figure of plan B under plan A's column, the value of parcel
57 written for parcel 56, a footnote marked on one lot group's contract that becomes the condition of another group's items. The
property-level checks (``contexts``) cannot see these. This module reads the scope of a passage from its own explicit openers, at
read time, from the text the verification already holds (the cited source, the blocks above it that the fetcher preloaded); nothing is
stored, no model is called, and nothing changes in a unit that has no opener.

**A mention** is a scope noun with its identifier: «תכנית ג/20039», «חלקות 1-3, 8», «מגרש 501», «חוזה חכירה שני», «חלופה ב׳», «שלב א׳»,
«קבוצת מגרשים ב׳», or an approved / proposed state («מצב מוצע», «התכנית המאושרת»). The kinds are ``plan``, ``parcel``, ``lot``,
``group``, ``contract``, ``alternative``, ``stage`` and ``state``; an identifier is canonical (digits without leading zeros, a plan's
digit runs, a Hebrew letter or an ordinal word as its number), so «חלופה א׳», «חלופה 1» and «החלופה הראשונה» are one. A noun without an
identifier is no mention, and a number that is a date or a year is no plan.

**An opener** is a line that sets the scope of what follows it: a lettered or numbered paragraph that names a scope in its first
words («ג. בהתאם לתכנית בניין עיר מספר ג/20039»), a line that begins with the noun («חלקה 57 — …»), a heading or a short caption that
names one. A scope holds until the next opener of the SAME kind, inside the same section and not further than ``LOOKBACK`` blocks above
(``Layout.pieces``), and never crosses a heading that changes the section path.

**The scope of a datum** is read from where it stands: the header of its column and the label of its row in a table, else the opener
above it. **Three states** (``read``): ``same`` (the claim's referent and an occurrence of the datum share the identifier of every kind
both name), ``different`` (EVERY occurrence of the datum in the cited source is under another identifier of a kind the claim names),
``unknown`` (anything else: no occurrence, no scope on one side, or a mix). Only ``different`` is a reason to cut; a source that
states the datum under the claimed scope, or under none, never is one.

**Footnotes.** A note that opens with a mark («*», «**», «(1)», «1)», a superscript digit) belongs to the cells that carry the same mark
(``note_rows``): a mark in the header belongs to every row, a mark in a body row to that row alone. On a text-layer page a note whose
first word is a small digit (``Piece.note``: the word is smaller than its line) belongs to the nearest earlier block of the page
that ends a word with the same small digit (``bind_geometry``), and so to the scope of that block and to the lettered paragraph («ג. …») it is in
(``item_at``: a contract described in its own paragraph is one item even when the paragraph names no identifier). A mark that cannot be bound
binds the note to nothing: the server attaches it to no item, and the judge is told."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

SAME, DIFFERENT, UNKNOWN = "same", "different", "unknown"
PLAN, PARCEL, LOT, GROUP, CONTRACT, ALTERNATIVE, STAGE, STATE = (
    "plan", "parcel", "lot", "group", "contract", "alternative", "stage", "state")
KINDS = (PLAN, PARCEL, LOT, GROUP, CONTRACT, ALTERNATIVE, STAGE, STATE)
KIND_NAMES = {PLAN: "תכנית", PARCEL: "חלקה", LOT: "מגרש", GROUP: "קבוצת מגרשים", CONTRACT: "חוזה", ALTERNATIVE: "חלופה",
              STAGE: "שלב", STATE: "מצב"}
HEADING_WORDS = 8  # a heading cell with more words than this is a sentence, not a heading
LOOKBACK = 20  # the blocks above a source whose openers its scope may come from
SMALL_TEXT = 0.8  # a word is a superscript mark when it is smaller than this fraction of the median word height of its block
RANGE_MAX = 60  # the largest range of numbers ("1-60") that is expanded into its members

_PFX = "הולבמשכ"
_GERESH = "׳'’`"
_OPEN_WORD = r"(?<![א-ת])"
_SPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """The text as the mentions are read from it: the geresh and the gershayim unified, the maqaf a hyphen, the spaces one."""
    out = (text or "").replace("־", "-").replace("–", "-").replace("—", "-").replace("״", '"').replace("”", '"').replace("“", '"')
    for g in _GERESH:
        out = out.replace(g, "׳")
    return _SPACE.sub(" ", out)


_ALPHABET = "אבגדהוזחטיכלמנסעפצקרשת"
_FINAL = str.maketrans("ךםןףץ", "כמנפצ")
_ORDINALS = {"ראשון": 1, "ראשונה": 1, "שני": 2, "שנייה": 2, "שניה": 2, "שלישי": 3, "שלישית": 3, "רביעי": 4, "רביעית": 4,
             "חמישי": 5, "חמישית": 5, "שישי": 6, "שישית": 6, "שביעי": 7, "שביעית": 7}
_ORDINAL = "|".join(sorted(_ORDINALS, key=len, reverse=True))


def letter_id(letter: str) -> str | None:
    """The number of a Hebrew letter used as an ordinal (א = 1 … ת = 22), as a string; None for anything else."""
    letter = letter.translate(_FINAL)
    return str(_ALPHABET.index(letter) + 1) if len(letter) == 1 and letter in _ALPHABET else None


# --- the nouns --------------------------------------------------------------------------------------------------------------

_NOUNS = {
    PLAN: r"תכניות|תוכניות|תכנית|תוכנית|תב\"ע|תבע|תמ\"א|תמ\"מ|תת\"ל|תמא|תממ",
    PARCEL: r"חלקות|חלקה",
    LOT: r"תאי\s+שטח|תא\s+שטח|מגרשים|מגרש",
    GROUP: r"קבוצות\s+מגרשים|קבוצת\s+מגרשים|קבוצות|קבוצה",
    CONTRACT: r"חוזי\s+חכירה|חוזה\s+חכירה|הסכמי\s+חכירה|הסכם\s+חכירה|חוזים|חוזה|הסכמים|הסכם",
    ALTERNATIVE: r"חלופות|חלופה|חלופת|אלטרנטיבות|אלטרנטיבה|תרחישים|תרחיש",
    STAGE: r"שלבים|שלב",
}
_NOUN_RE = {k: re.compile(rf"{_OPEN_WORD}[{_PFX}]{{0,2}}(?:{v})(?![א-ת])") for k, v in _NOUNS.items()}
# the nouns of a plan carry the national and district plans' own numbers («תמ״א 38»)
_ID_LIST_ITEM = r"(?:\d{1,6}(?:/\d{1,6})?(?:\s*-\s*\d{1,6})?)"
_LETTER_ITEM = r"(?:[א-ת]׳|[א-ת](?![א-ת׳\"]))"
_ORD_ITEM = rf"(?:ה?(?:{_ORDINAL})(?![א-ת]))"
_JOIN = r"(?:\s*,\s*|\s*ו-?\s*|\s+או\s+|\s+עד\s+|\s*-\s*)"


def _item(letters: bool, ordinals: bool) -> str:
    return "|".join([_ID_LIST_ITEM, *([_LETTER_ITEM] if letters else []), *([_ORD_ITEM] if ordinals else [])])


def _list_re(letters: bool, ordinals: bool, before: str = "") -> re.Pattern:
    item = _item(letters, ordinals)
    return re.compile(rf"\s*{before}(?:(?:מס(?:פר|׳|\.)?)\s*)?(?P<first>{item})(?P<rest>(?:{_JOIN}(?:{item}))*)")


# a contract's identifier may follow the kind of contract («החוזה החכירה»): the article and the word «חכירה» are skipped
_LISTS = {PARCEL: _list_re(False, False), LOT: _list_re(False, False), GROUP: _list_re(True, True),
          CONTRACT: _list_re(True, True, r"(?:ה?חכירה\s+)?"), ALTERNATIVE: _list_re(True, True), STAGE: _list_re(True, True)}
_ITEMS = {k: re.compile(rf"{_JOIN}({_item(k not in (PARCEL, LOT), k not in (PARCEL, LOT))})") for k in _LISTS}
_PLAN_ID = re.compile(r"(?:[א-ת]{1,3}[/-])*\d{1,6}(?:[/-](?:\d{1,6}|[א-ת]׳?))*")
_DATE_LIKE = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}")
_PLAN_TOKEN = re.compile(r"(?<![\w/])(?:[א-ת]{1,3}/)+\d{3,6}(?:/(?:\d{1,6}|[א-ת]׳?))*")
_MORE_PLANS = re.compile(r"\s*(?:,|ו-?|או)\s*(?P<p>(?:[א-ת]{1,3}[/-])+\d{1,6}(?:[/-](?:\d{1,6}|[א-ת]׳?))*)")
_PLAN_FILLER = frozenset("בניין בנין עיר מתאר מקומית מקומי מפורטת מפורט מחוזית מחוזי ארצית ארצי נקודתית כוללנית קודמת תקפה חדשה "
                         "בתוקף מאושרת חלופית מיוחדת שמספרה שמספר מספר מס מס׳ מס\"".split())
_PLAN_MARKER = frozenset("מספר מס מס׳ מס\"".split())
_TOKEN = re.compile(r"\S+")


def _digits(token: str) -> list[str]:
    return [d.lstrip("0") or "0" for d in re.findall(r"\d+", token)]


def _numeric_ids(items: Sequence[str], kind: str) -> frozenset[str]:
    """The canonical identifiers of the items of a list written after a noun («1-3, 8», «12 ו-14», «56/57»): a range is its members
    (``RANGE_MAX``), a pair with a block number («8401/40») is its parcel, a letter or an ordinal word is its number."""
    out: set[str] = set()
    for item in items:
        if m := re.fullmatch(r"(\d{1,6})\s*-\s*(\d{1,6})", item):
            lo, hi = int(m.group(1)), int(m.group(2))
            out.update(str(n) for n in range(lo, hi + 1)) if 0 <= hi - lo <= RANGE_MAX else out.update({str(lo), str(hi)})
        elif "/" in item:
            first, second = (d.lstrip("0") or "0" for d in item.split("/", 1))
            out.update({second} if kind in (PARCEL, LOT) and len(first) >= 4 else {first, second})
        elif item[:1].isdigit():
            out.add(item.lstrip("0") or "0")
        elif (word := item[1:] if item[:1] == "ה" and item[1:] in _ORDINALS else item) in _ORDINALS:
            out.add(str(_ORDINALS[word]))
        elif (n := letter_id(item.rstrip("׳"))) is not None:
            out.add(n)
    return frozenset(out)


def _plan_ids(text: str, pos: int) -> tuple[frozenset[str], int] | None:
    """The identifier of a plan written after its noun: the first token that is a plan number (letters and digits joined by slashes or
    hyphens, «ג/מק/12078/א׳»), reached through the closed words a plan description puts between (``_PLAN_FILLER``) or any word before the
    word «מספר»; a lone letter right after the noun («תכנית א׳»). A date and a bare year are no plan number."""
    tokens = list(_TOKEN.finditer(text, pos, min(len(text), pos + 160)))[:9]
    for n, tok in enumerate(tokens):
        word = tok.group(0).strip("(),.;:\"")
        if n == 0 and re.fullmatch(_LETTER_ITEM, word):
            return frozenset({letter_id(word.rstrip("׳"))} - {None}), tok.end()
        if _PLAN_ID.fullmatch(word) and re.search(r"\d", word) and not _DATE_LIKE.fullmatch(word):
            marked = n > 0 and tokens[n - 1].group(0).strip("(),.;:\"") in _PLAN_MARKER
            if re.fullmatch(r"(?:19|20)\d\d", word) and not marked:
                return None  # a year
            runs = _digits(word)
            long = [r for r in runs if len(r) >= 3]
            return frozenset(long or runs), tok.end()
        if word in _PLAN_FILLER:
            continue
        ahead = [t.group(0).strip("(),.;:\"") for t in tokens[n + 1:n + 3]]
        if n < 4 and any(a in _PLAN_MARKER for a in ahead) and re.search(r"[א-ת]", word):
            continue  # a word of the plan's name before «מספר»
        return None
    return None


_STATE_WORDS = {
    "approved": "מאושר|מאושרת|מאושרים|מאושרות|בתוקף",
    "proposed": "מוצע|מוצעת|מוצעים|מוצעות|מבוקש|מבוקשת|מבוקשים|מבוקשות",
    "existing": "קיים|קיימת|קיימים|קיימות",
}
_STATE_LEAD = (rf"{_OPEN_WORD}[{_PFX}]{{0,2}}(?:מצב|תכנית|תוכנית|חלופה|זכויות|בנייה|בניה|שימוש|מבנה|שטח|שטחים)\s+(?:ה)?(?P<w>%s)"
               r"(?![א-ת])")
_STATE_RE = {k: re.compile(_STATE_LEAD % v) for k, v in _STATE_WORDS.items() if k != "existing"}
_STATE_RE["existing"] = re.compile(rf"{_OPEN_WORD}[{_PFX}]{{0,2}}מצב\s+(?:ה)?(?P<w>{_STATE_WORDS['existing']})(?![א-ת])")
_STATE_CELL = {k: re.compile(rf"(?:ה?(?:{v}))") for k, v in _STATE_WORDS.items()}


@dataclass(frozen=True)
class Mention:
    """One scope a text names: its ``kind``, its canonical ``ids``, the words as written (``said``) and where it stands."""

    kind: str
    ids: frozenset[str]
    said: str
    start: int
    end: int


def mentions(text: str) -> list[Mention]:
    """Every scope the ``text`` names, in order of appearance (a noun with its identifier, or a state)."""
    return list(_mentions(text))


@lru_cache(maxsize=8192)
def _mentions(text: str) -> tuple[Mention, ...]:
    flat = _clean(text)
    out: list[Mention] = []
    for kind, noun in _NOUN_RE.items():
        for m in noun.finditer(flat):
            if kind == PLAN:
                found = _plan_ids(flat, m.end())
                if found is None or not found[0]:
                    continue
                ids, end = found
                while more := _MORE_PLANS.match(flat, end):  # «תכניות ג/1000 ו-ג/2000»
                    word = more.group("p")
                    if _DATE_LIKE.fullmatch(word):
                        break
                    runs = _digits(word)
                    ids, end = ids | frozenset([r for r in runs if len(r) >= 3] or runs), more.end()
            else:
                lm = _LISTS[kind].match(flat, m.end())
                if lm is None:
                    continue
                ids = _numeric_ids([lm.group("first"), *_ITEMS[kind].findall(lm.group("rest"))], kind)
                end = lm.end()
                if not ids:
                    continue
            out.append(Mention(kind, ids, flat[m.start():end].strip(), m.start(), end))
    for kind, pattern in _STATE_RE.items():
        out.extend(Mention(STATE, frozenset({kind}), m.group(0).strip(), m.start(), m.end()) for m in pattern.finditer(flat))
    out.sort(key=lambda x: (x.start, x.kind))
    return tuple(_dedupe(out))


def _dedupe(found: list[Mention]) -> list[Mention]:
    """The mentions without those inside another's span of the same kind (a noun the plural form also matched)."""
    kept: list[Mention] = []
    for m in found:
        if any(k.kind == m.kind and k.start <= m.start and m.end <= k.end and (k.start, k.end) != (m.start, m.end) for k in found):
            continue
        if not any(k.kind == m.kind and (k.start, k.end) == (m.start, m.end) for k in kept):
            kept.append(m)
    return kept


def by_kind(found: Iterable[Mention]) -> dict[str, frozenset[str]]:
    """The identifiers of each kind the mentions name (several mentions of one kind are one set)."""
    out: dict[str, set[str]] = {}
    for m in found:
        out.setdefault(m.kind, set()).update(m.ids)
    return {k: frozenset(v) for k, v in out.items()}


_NOUN_START = re.compile(r"^\s*(?:" + "|".join(f"[{_PFX}]{{0,2}}(?:{v})" for v in _NOUNS.values()) + r")(?![א-ת])")


def keyed_label(header: str, cell: str) -> str:
    """The label of a table row with the noun of its column in front, when the column's header begins with a scope noun («חלקות בגוש 11588» over
    «1–3 ו־8 בשלמות» is «חלקות 1–3 ו־8 בשלמות»): the cell holds the identifiers of that kind. The cell as it is for any other header."""
    m = _NOUN_START.match(_clean(header))
    return f"{m.group(0).strip()} {cell}" if m and not mentions(cell) else cell


def cell_scope(cell: str) -> list[Mention]:
    """The scope a table heading or label cell names (``_cell_scope``)."""
    return list(_cell_scope(cell))


@lru_cache(maxsize=8192)
def _cell_scope(cell: str) -> tuple[Mention, ...]:
    """The scope a table heading or label cell names: its mentions, else the cell as a whole — the bare plan number of a column
    («ג/25083», a number of five digits or more, a district-prefixed one) or the lone state word («מאושר», «מוצע»)."""
    found = mentions(cell)
    if found:
        return tuple(found)
    flat = _clean(cell)
    if tokens := [m for m in _PLAN_TOKEN.finditer(flat) if not _DATE_LIKE.fullmatch(m.group(0))]:
        # a heading cell that writes a district-prefixed plan number beside a word or two («ג/25083 — המאוחרת»)
        return tuple(Mention(PLAN, frozenset([r for r in _digits(m.group(0)) if len(r) >= 3] or _digits(m.group(0))), m.group(0), m.start(), m.end())
                     for m in tokens if len(flat.split()) <= HEADING_WORDS)
    word = flat.strip(" *:.()\"")
    if not word:
        return ()
    for state, pattern in _STATE_CELL.items():
        if pattern.fullmatch(word):
            return (Mention(STATE, frozenset({state}), word, 0, len(word)),)
    if _PLAN_ID.fullmatch(word) and not _DATE_LIKE.fullmatch(word):
        runs = _digits(word)
        long = [r for r in runs if len(r) >= 3]
        slashed = "/" in word and re.match(r"[א-ת]", word)
        if long and (len(long[0]) >= 5 or slashed) and not re.fullmatch(r"(?:19|20)\d\d", word):
            return (Mention(PLAN, frozenset(long), word, 0, len(word)),)
    return ()


# --- the openers ------------------------------------------------------------------------------------------------------------

_ENUM = re.compile(r"^\s*(?:[-•·*]\s+)?(?:\(?[א-ת]\)|\(?[א-ת]׳?\.|\(?\d{1,2}\)|\d{1,2}\.(?=\s)|\d{1,2}(?:\.\d{1,2})+\.?)\s+")
_MARKUP = re.compile(r"^\s*(?:[-•·]\s+)?(?:#{1,6}\s+)?(?:\*\*|__)?")  # a bullet, a heading mark and bold marks before the words
_HEADING_MARK = re.compile(r"^\s*#{1,6}\s")
_FIRST_SENTENCE = re.compile(r"(?<=[.!?:;])\s+(?=[^\d])")
CAPTION_WORDS = 10  # a short line that names a scope and states no figure is a caption or a heading
_LEAD_END = re.compile(r"[:\-,;]")
ENUMERATED_REACH = 110  # characters of an enumerated paragraph in which its scope is named


def opener(line: str, *, heading: bool = False, caption: bool = False) -> list[Mention]:
    """The scopes a line opens, or none when it is no opener: a lettered or numbered paragraph that names one in its first words, a line that
    begins with the noun, a markdown heading or ``heading`` (a heading block) that names one, or — only for ``caption``, a line that is a whole
    block or a context part of its own — a short caption that states no figure and ends with the scope it names."""
    return list(_opener(line, heading, caption))


@lru_cache(maxsize=8192)
def _opener(line: str, heading: bool, caption: bool) -> tuple[Mention, ...]:
    flat = _clean(line).strip()
    if not flat:
        return ()
    marked = bool(_HEADING_MARK.match(flat)) or heading
    enumerated = _ENUM.match(flat)
    body = _MARKUP.sub("", flat[enumerated.end():] if enumerated else flat, count=1).lstrip("*_ ")
    first = _FIRST_SENTENCE.split(body, maxsplit=1)[0]
    found = mentions(first)
    if not found:
        return ()
    at = found[0].start
    if marked:
        return tuple(found)
    if enumerated:
        return tuple(m for m in found if m.start <= ENUMERATED_REACH)
    if at <= 3:  # a line that begins with the noun sets the scope its leading phrase names (up to the colon, dash or comma), not every mention after
        cut = _LEAD_END.search(first, found[0].end)
        return tuple(m for m in found if cut is None or m.start < cut.start())
    rest = first
    for m in sorted(found, key=lambda x: -x.start):
        rest = rest[:m.start] + rest[m.end:]
    rest = re.sub(r"טבלה\s*\d+(?:\.\d+)*", "", rest)  # the number of the table is no figure
    if (caption and len(first.split()) <= CAPTION_WORDS and not re.search(r"\d", rest) and not first.rstrip().endswith(".")
            and len(first[found[-1].end:].split()) <= 2):
        return tuple(found)
    return ()


_SENTENCE = re.compile(r"(?<=[.!?;:])\s+")


def sentences(line: str) -> list[str]:
    """The sentences of a line of a passage (``_sentences``)."""
    return list(_sentences(line))


@lru_cache(maxsize=8192)
def _sentences(line: str) -> tuple[str, ...]:
    """The sentences of a line of a passage, each a unit of its own for the openers and the figures (a paragraph written as one line can
    open another scope in its second sentence): split after a full stop, a question or exclamation mark, a semicolon or a colon; the
    enumerator that opens a line stays with its sentence."""
    flat = _clean(line).strip()
    lead = _ENUM.match(flat)
    head, rest = (flat[:lead.end()], flat[lead.end():]) if lead else ("", flat)
    parts = [x for x in _SENTENCE.split(rest) if x.strip()]
    if not parts:
        return (flat,) if flat else ()
    parts[0] = head + parts[0]
    return tuple(parts)


# --- the footnotes ----------------------------------------------------------------------------------------------------------

_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_NOTE_START = re.compile(r"^\s*(?P<m>\*{1,4}|\(\d{1,2}\)|\d{1,2}\)|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*(?=\S)")
_CELL_MARK_END = re.compile(r"(?P<m>\*{1,4}|\(\d{1,2}\)|\d{1,2}\)|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*$")
_CELL_MARK_START = re.compile(r"^\s*(?P<m>\*{1,4}|\(\d{1,2}\)|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)(?=\s*[^\s\d])")


def _mark_key(mark: str) -> str:
    """A mark as it is compared: «(1)», «1)» and the superscript one are the note 1; asterisks stay as they are."""
    mark = mark.translate(_SUPER)
    return "*" * len(mark) if set(mark) == {"*"} else "#" + re.sub(r"\D", "", mark)


def note_mark(line: str) -> str:
    """The mark a note line opens with («*», «**», «(1)», «1)», a superscript digit), as ``_mark_key`` compares it; "" for a line that is
    no note by its text."""
    m = _NOTE_START.match(_clean(line))
    return _mark_key(m.group("m")) if m else ""


def cell_marks(cell: str) -> frozenset[str]:
    """The note marks a table cell carries at its end or its start («חוזה חכירה ראשון*», «מעל מפלס הכניסה**», «(1) שטח»)."""
    flat = _clean(cell)
    return frozenset(_mark_key(m.group("m")) for p in (_CELL_MARK_END, _CELL_MARK_START) if (m := p.search(flat)))


def row_cells(line: str) -> list[str]:
    """The cells of a table row line, the one reading every cell index of a row comes from (the scope reader's, the verifier's cell list and
    ``meaning``'s). A markdown row ("| a | b |") has its outer pipes as delimiters, at the line's edge; a row as the table text renders it
    (cells joined by " | ") has none, so an empty first or last cell leaves a pipe with a space before it or after it (" | x | ") and stays a
    cell - dropping it shifts every later cell one column left."""
    raw = line.rstrip() if line.startswith("|") else line
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def note_rows(mark: str, header: Sequence[str], rows: Sequence[str]) -> frozenset[int] | None:
    """The body rows (indexes into ``rows``) a note with ``mark`` belongs to: every row when a header cell carries the mark, else the rows
    with a cell that does. None when no cell carries it (the note is bound to nothing)."""
    if not mark:
        return None
    if any(mark in cell_marks(c) for c in header):
        return frozenset(range(len(rows)))
    held = frozenset(i for i, r in enumerate(rows) if any(mark in cell_marks(c) for c in row_cells(r)))
    return held or None


# --- the layout the verification preloads -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Piece:
    """One block of a document as the scope reader sees it: its ``index``, ``text``, ``kind`` (paragraph, heading, table, image), ``page``
    and ``section`` (the heading path); ``small`` — the small digit words (superscript marks) the block writes after a word; ``note`` — the
    small digit the block opens with (it is the note of that mark)."""

    index: int
    text: str
    kind: str = "paragraph"
    page: int | None = None
    section: tuple[str, ...] = ()
    small: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Layout:
    """The blocks above a source and the source's own (``Piece``), in reading order, as the fetcher read them with their word geometry:
    ``first`` — the index of the source's first block."""

    pieces: tuple[Piece, ...] = ()
    first: int | None = None


def small_marks(words: Sequence[tuple[str, float]]) -> tuple[tuple[str, ...], str]:
    """From the words of a block with their heights ``(text, height)``: the small digit words that follow another word (superscript marks) and
    the small digit the block opens with (its note mark). A word is small when it is a one or two digit number and its height is under
    ``SMALL_TEXT`` of the block's median height."""
    heights = sorted(h for _, h in words if h > 0)
    if len(heights) < 3:
        return (), ""
    median = heights[len(heights) // 2]
    small = [bool(re.fullmatch(r"\d{1,2}\)?", w.strip()) and 0 < h < SMALL_TEXT * median) for w, h in words]
    note = re.sub(r"\D", "", words[0][0]) if small and small[0] else ""
    marks = tuple(re.sub(r"\D", "", words[i][0]) for i in range(1, len(words)) if small[i])
    return marks, note


def piece_of(index: int, kind: str, text: str, section: Sequence[str] = (), page: int | None = None,
             spans: Sequence[Sequence[float]] | None = None) -> Piece:
    """A stored block as a ``Piece``: its small digit words from the word geometry its row holds (``document_blocks.spans``: ``[line, word,
    start, end, x0, top, x1, bottom]`` per word, the offsets into the block's text), for a paragraph or heading of a text-layer page."""
    marks: tuple[str, ...] = ()
    note = ""
    if spans and kind in ("paragraph", "heading") and text:
        words = []
        for sp in sorted(spans, key=lambda x: (x[2], x[3])):
            try:
                words.append((text[int(sp[2]):int(sp[3])], float(sp[7]) - float(sp[5])))
            except (IndexError, TypeError, ValueError):
                continue
        marks, note = small_marks(words)
    return Piece(index, text or "", kind, page, tuple(section), marks, note)


def bind_geometry(layout: Layout) -> dict[int, int]:
    """The note blocks of a layout and the block each belongs to: a block that opens with a small digit is bound to the nearest EARLIER block
    of its page that writes the same small digit after a word. A note with no such block is absent from the result."""
    bound: dict[int, int] = {}
    latest: dict[tuple[int | None, str], int] = {}  # (page, small digit) -> the nearest block so far that writes it after a word
    for piece in layout.pieces:
        if piece.note and (earlier := latest.get((piece.page, piece.note))) is not None:
            bound[piece.index] = earlier
        for mark in piece.small:
            latest[(piece.page, mark)] = piece.index
    return bound


# --- the scope of a datum ---------------------------------------------------------------------------------------------------

def merge(base: Mapping[str, frozenset[str]], found: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """The scope after an opener: the kinds it names replace the same kinds of ``base`` (until the next opener of the same kind)."""
    return {**base, **found}


def _related_sections(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Whether two section paths are one section or one is inside the other: the blocks of related sections share their scopes."""
    return a == b or a[:len(b)] == b or b[:len(a)] == a


def _own_first(layout: Layout) -> list[Piece]:
    """The pieces of a layout, the source's own blocks first in reading order, then the blocks above it nearest first."""
    first = layout.first if layout.first is not None else 0
    return sorted(layout.pieces, key=lambda p: (p.index < first, -p.index if p.index < first else p.index))


def _openers(pieces: Sequence[Piece], section: tuple[str, ...]) -> dict[str, frozenset[str]]:
    """The scope the ``pieces`` (in reading order) set by their openers, inside ``section``: a heading that changes the section path ends every
    scope above it; a table, a picture and a note open nothing."""
    out: dict[str, frozenset[str]] = {}
    for piece in pieces:
        if not _related_sections(piece.section, section):
            out = {}
            continue
        if piece.kind in ("table", "image") or piece.note:
            continue
        for line in piece.text.splitlines():
            for sentence in sentences(line):
                out = merge(out, by_kind(opener(sentence, heading=piece.kind == "heading", caption=len(piece.text.split()) <= CAPTION_WORDS)))
    return out


def inherited(layout: Layout | None) -> dict[str, frozenset[str]]:
    """The scope the blocks above a source set when it begins: the nearest opener of each kind within ``LOOKBACK`` blocks and the source's
    own section. A paragraph note is no opener. Pieces from the source's first block on are the source's own text, read by ``occurrences``."""
    if layout is None or layout.first is None:
        return {}
    above = [p for p in layout.pieces if p.index < layout.first][-LOOKBACK:]
    if not above:
        return {}
    section = next((p.section for p in layout.pieces if p.index >= layout.first), above[-1].section)
    return _openers(above, section)


def scope_through(layout: Layout, index: int) -> dict[str, frozenset[str]]:
    """The scope in force at the end of block ``index`` of the layout (its own opener included)."""
    pieces = [p for p in layout.pieces if p.index <= index][-LOOKBACK:]
    here = next((p for p in pieces if p.index == index), None)
    return _openers(pieces, here.section) if here is not None else {}


def item_at(layout: Layout | None, index: int) -> int | None:
    """The lettered or numbered item a block is in (round 20 U3, KTD3): the index of the nearest block at or above ``index``, inside the same
    section and the lookback, whose first line is an enumerated paragraph («ג. …»). A contract, a plan or a lot group that a document
    describes in its own lettered paragraph is one item, whether or not the paragraph names an identifier. None when there is none."""
    if layout is None:
        return None
    here = next((p for p in layout.pieces if p.index == index), None)
    if here is None:
        return None
    for piece in reversed([p for p in layout.pieces if p.index <= index][-LOOKBACK:]):
        if not _related_sections(piece.section, here.section):
            return None
        if piece.kind in ("table", "image") or piece.note:
            continue
        first = piece.text.splitlines()[0] if piece.text.strip() else ""
        if _ENUM.match(_clean(first)):
            return piece.index
    return None


def item_of_text(layout: Layout | None, text: str) -> int | None:
    """The item (``item_at``) of the block of the layout that holds ``text`` (compared with the blanks collapsed), the source's own blocks first."""
    if layout is None or not text.strip():
        return None
    flat = _SPACE.sub(" ", _clean(text)).strip()
    for piece in _own_first(layout):
        if flat and flat in _SPACE.sub(" ", _clean(piece.text)):
            return item_at(layout, piece.index)
    return None


def item_of_referent(layout: Layout | None, referent: Mapping[str, frozenset[str]]) -> int | None:
    """The item (``item_at``) of the paragraph that opens with the identifiers a claim names: the first block of the layout, the source's own
    first, whose opener shares an identifier of a kind the referent names. None when no opener does."""
    if layout is None or not referent:
        return None
    for piece in _own_first(layout):
        if piece.kind in ("table", "image") or piece.note:
            continue
        for line in piece.text.splitlines():
            for sentence in sentences(line):
                found = by_kind(opener(sentence, heading=piece.kind == "heading"))
                if any(k in referent and referent[k] & v for k, v in found.items()):
                    return item_at(layout, piece.index)
    return None


@dataclass(frozen=True)
class NoteInfo:
    """A footnote block of a source as the geometry shows it: its block ``index``, its ``mark`` (the small digit it opens with), the block
    that carries the same mark (``marker``; None: the mark could not be bound), the ``scope`` in force there (None: unbound), the ``item`` the
    marker is in (``item_at``) and its text."""

    index: int
    mark: str
    marker: int | None
    scope: Mapping[str, frozenset[str]] | None
    text: str
    item: int | None = None


def notes(layout: Layout | None) -> list[NoteInfo]:
    """The footnote blocks inside a source (from its first block on) with the scope of the block each belongs to (``bind_geometry``)."""
    if layout is None or layout.first is None:
        return []
    return list(_notes(layout))


@lru_cache(maxsize=256)
def _notes(layout: Layout) -> tuple[NoteInfo, ...]:
    """``notes`` of a layout, kept per layout: every claim and item of a turn reads the notes of the same few sources."""
    bound = bind_geometry(layout)
    return tuple(NoteInfo(p.index, p.note, bound.get(p.index), scope_through(layout, bound[p.index]) if p.index in bound else None, p.text,
                          item_at(layout, bound[p.index]) if p.index in bound else None)
                 for p in layout.pieces if p.note and p.index >= (layout.first or 0))


def note_conflicts(note: NoteInfo, scope: Mapping[str, frozenset[str]], item: int | None = None) -> bool:
    """Whether a note belongs to another contract, lot group or plan than the datum it would limit. When the lettered paragraphs of both are known
    (``item_at``: the note's mark and the datum), they decide — the same paragraph is the same item, whatever sentences inside it re-open; else
    the identifiers do: a kind both name with nothing in common. An unbound note conflicts with nothing here (it is told to the judge)."""
    if note.scope is None:
        return False
    if note.item is not None and item is not None:
        return note.item != item
    return any(k in scope and not scope[k] & v for k, v in note.scope.items())


@dataclass(frozen=True)
class Located:
    """One occurrence of a datum in a cited source: the ``scope`` it is written under, the ``line`` it is in, ``base`` — the kinds whose
    identifiers came only from the blocks above the source (``inherited``), not from the source's own openers, headings, columns or labels —
    ``heard`` — the identifiers the source's own text had named by this line (its context parts, the lines before and the line itself) — and
    ``named`` — the identifiers the line itself names, in any form (``line_ids``)."""

    scope: Mapping[str, frozenset[str]]
    line: str
    base: frozenset[str] = frozenset()
    heard: Mapping[str, frozenset[str]] = field(default_factory=dict)
    named: Mapping[str, frozenset[str]] = field(default_factory=dict)


_HEADER_NUMBERS = re.compile(r"גוש\s*\d+|(?<!\d)(?:19|20)\d\d(?!\d)")  # a block number or a year in a heading is no figure of the table


def header_of(body: Sequence[str]) -> list[str]:
    """The header row of a table body: its first row, when no cell of it holds a figure (a plan number or a state in a cell is a heading, not a
    figure)."""
    first = next((ln for ln in body if "|" in ln), None)
    if first is None:
        return []
    cells = row_cells(first)
    figures = " ".join("" if cell_scope(c) else _HEADER_NUMBERS.sub(" ", without_mentions(c)) for c in cells)
    return cells if not re.search(r"\d", figures) else []


def line_ids(text: str) -> dict[str, frozenset[str]]:
    """The identifiers a line names in any form: its mentions, and a district-prefixed plan number written bare («תכנית מתאר מספר 217-0572784 (ג/23965)»)."""
    out = by_kind(mentions(text))
    for m in _PLAN_TOKEN.finditer(_clean(text)):
        if not _DATE_LIKE.fullmatch(m.group(0)):
            runs = _digits(m.group(0))
            out["plan"] = out.get("plan", frozenset()) | frozenset([r for r in runs if len(r) >= 3] or runs)
    return out


def _hear(heard: dict[str, frozenset[str]], ids: Mapping[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """``heard`` and the identifiers ``ids`` a text names."""
    for kind, found in ids.items():
        heard = {**heard, kind: heard.get(kind, frozenset()) | found}
    return heard


class _Spot(NamedTuple):
    """A place of a source where a datum may stand, before a claim asks for it: the ``text`` a claim's datum is looked for in (a cell, a
    sentence), the ``scope`` in force there, the ``line`` it is in, and ``base`` and ``heard`` as ``Located`` holds them."""

    text: str
    scope: Mapping[str, frozenset[str]]
    line: str
    base: frozenset[str]
    heard: Mapping[str, frozenset[str]]


def occurrences(parts: Sequence[tuple[str, str]], body: Sequence[str], layout: Layout | None,
                holds: Callable[[str], bool]) -> list[Located]:
    """Every occurrence of the claim's datum in a cited source, each with the scope it is written under. ``parts``: the context parts above
    the source's rows or text, ``(place, text)`` in reading order (``heading``, ``caption``, ``before``); ``body``: its lines (the rows of a
    table, the lines of a passage); ``layout``: the blocks above it, when the fetcher preloaded them; ``holds``: whether a line or a cell states
    the datum. A table row is read cell by cell: the header of the cell's column and the label of its row set the scope of that cell,
    over what the openers above it set; a line of a passage is under the last opener before it. The places of a source are read once
    (``_spots``) whatever the number of claims that look for a datum in it."""
    return [_located(s) for s in _spots(tuple((place, text) for place, text in parts), tuple(body), layout) if holds(s.text)]


def first_occurrence(parts: Sequence[tuple[str, str]], body: Sequence[str], layout: Layout | None,
                     holds: Callable[[str], bool]) -> Located | None:
    """The first of ``occurrences``, read without the others; None when there is none."""
    spot = next((s for s in _spots(tuple((place, text) for place, text in parts), tuple(body), layout) if holds(s.text)), None)
    return _located(spot) if spot is not None else None


def _located(spot: _Spot) -> Located:
    return Located(spot.scope, spot.line, spot.base, spot.heard, line_ids(spot.text))


@lru_cache(maxsize=256)
def _spots(parts: tuple[tuple[str, str], ...], body: tuple[str, ...], layout: Layout | None) -> tuple[_Spot, ...]:
    """The places of a source where a datum may stand, each with the scope it is written under (``occurrences``), in reading order."""
    scope = inherited(layout)
    own: set[str] = set()  # the kinds the source's own text names
    heard: dict[str, frozenset[str]] = {}
    for place, text in parts:
        heard = _hear(heard, line_ids(text))
        if place in ("heading", "caption", "before"):
            for line in text.splitlines() or [text]:
                found = by_kind(opener(line, heading=place == "heading", caption=True))
                scope, own = merge(scope, found), own | set(found)
    header = header_of(body)
    column_scope = [by_kind(cell_scope(c)) for c in header]
    out: list[_Spot] = []
    for line in body:
        heard = _hear(heard, line_ids(line))
        if "|" in line:
            cells = row_cells(line)
            if cells == header:
                continue
            label = by_kind(cell_scope(keyed_label(header[0], cells[0]) if header else cells[0])) if cells else {}
            for i, cell in enumerate(cells):
                column = column_scope[i] if i < len(column_scope) else {}
                here = merge(merge(scope, label), column)
                out.append(_Spot(cell, here, line, frozenset(k for k in here if k not in own and k not in label and k not in column), heard))
            continue
        for sentence in sentences(line):
            found = by_kind(opener(sentence))
            scope, own = merge(scope, found), own | set(found)
            out.append(_Spot(sentence, scope, sentence, frozenset(k for k in scope if k not in own), heard))
    return tuple(out)


@dataclass
class Reading:
    """The result of reading a claim against the occurrences of its datum: ``state`` (``SAME``, ``DIFFERENT``, ``UNKNOWN``) and ``other`` — the
    occurrences under other identifiers of a kind the claim names (what a reason or a fact line shows)."""

    state: str = UNKNOWN
    other: list[Located] = field(default_factory=list)


def read(claim: Mapping[str, frozenset[str]], found: Sequence[Located]) -> Reading:
    """Whether a claim's referent (``claim``: kind -> identifiers) matches the scope its datum is written under: ``same`` when one occurrence
    shares the identifiers of every kind both name, ``different`` when EVERY occurrence is under other identifiers of a kind the claim names,
    ``unknown`` when there is none to compare (no occurrence, no common kind) or the occurrences mix. A kind that only the blocks above the
    source set (``Located.base``) decides nothing when the source itself had already named the claim's identifier by the occurrence
    (``Located.heard``): the opener above is then not what the source is under."""
    if not claim or not found:
        return Reading()
    states = []
    for occ in found:
        common = [k for k in claim if k in occ.scope and not (k in occ.base and claim[k] & occ.heard.get(k, frozenset()))]
        if any(claim[k] & ids for k, ids in occ.named.items() if k in claim):
            states.append(SAME)  # the line itself names the scope the claim names
        elif not common:
            states.append(UNKNOWN)
        elif all(claim[k] & occ.scope[k] for k in common):
            states.append(SAME)
        else:
            states.append(DIFFERENT)
    other = [occ for occ, s in zip(found, states, strict=True) if s == DIFFERENT]
    if SAME in states:
        return Reading(SAME, other)
    return Reading(DIFFERENT if all(s == DIFFERENT for s in states) else UNKNOWN, other)


def without_mentions(text: str) -> str:
    """``text`` with the scopes it names taken out (the identifiers of a plan or a parcel are no figure of the claim's datum)."""
    flat = _clean(text)
    for m in sorted(mentions(flat), key=lambda x: -x.start):
        flat = flat[:m.start] + " " + flat[m.end:]
    return flat


def opening_scope(text: str, *, heading: bool = False) -> dict[str, frozenset[str]]:
    """The scope a lead, a heading or a line before a table sets when it is an opener (``opener``): what it names in its first sentence, and only
    when that sentence begins with the noun, is a lettered paragraph or a caption. A lead whose first sentence names no scope sets none, whatever
    its later sentences mention."""
    first = sentences(text)[0] if text.strip() else ""
    return by_kind(opener(first, heading=heading, caption=True))


def referent(own: str, cells: Sequence[str] = (), openings: Sequence[str] = (), headings: Sequence[str] = ()) -> dict[str, frozenset[str]]:
    """The scope a claim is about, each kind taken from the nearest text that names it: the claim's own words, then the heading and the label of
    its table cell (``cells``), then the leads and lines before it that open a scope (``openings``), then the headings above it (``headings``)."""
    out: dict[str, frozenset[str]] = {}
    layers = [by_kind(cell_scope(own) if len(own) < 80 else mentions(own)), *(by_kind(cell_scope(c)) for c in cells),
              *(opening_scope(t) for t in openings), *(opening_scope(t, heading=True) for t in headings)]
    for layer in layers:
        for kind, ids in layer.items():
            out.setdefault(kind, ids)
    return out


def describe(scope: Mapping[str, frozenset[str]]) -> str:
    """A scope in words, for a fact line or a reason: «תכנית 20039, חלקה 56»."""
    return ", ".join(f"{KIND_NAMES[kind]} {', '.join(sorted(scope[kind], key=lambda x: (len(x), x)))}" for kind in KINDS if kind in scope)
