"""The shared date parser (round 11 U6a: R5, R6; KTD4 "Partial dates" and "Parsing").

One pure function pair every date reader of the chat shares: `parse` reads the one date a short text holds, `find_all`
finds the date expressions inside a sentence, with their spans. It is built on `professional.profile.parse_date` (the
month names and the calendar check come from there; that function is not changed) and adds the formats a datum's row
or sentence uses:

- day: `dd/mm/yyyy`, `d.m.yyyy`, `dd-mm-yyyy` (one separator), `yyyy.mm.dd` / `yyyy-mm-dd` / `yyyy/mm/dd` (the year
  first, so it cannot be read as day-month), `[day] <Hebrew month> yyyy` with the Hebrew prefixes (באפריל, לאפריל);
- month: `mm/yyyy`, `m.yyyy`, `<Hebrew month> yyyy`;
- year: a bare four-digit year 1950-2100, only after a date word or prefix (בשנת, שנת, לשנת, משנת, ב-, מ-, ל-, עד, בין,
  מאז) or as an end of a range;
- ranges: `yyyy-yyyy`, `בין X ל-Y`, `מ-X עד Y`, `X עד Y`.

Precision is never widened: "03/2024" is a month and never the 1st of March; a range whose ends differ in precision is
cut to the coarser one (it then covers, it never invents a month). What is no supported date is unknown, never a
guess: two-digit years, Hebrew-calendar years, quarters, impossible dates (31/02/2024, and "31 באפריל 2024" is not
read as "אפריל 2024" either), and numbers that are not dates. `find_all` also keeps these out: a number after גוש,
חלקה, מגרש, תקן, סעיף or a street, a price (₪, ש״ח, a thousands separator), an area (מ״ר, דונם) and a percentage.
No database, no clock: the caller owns the anchor date."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.professional.profile import _MONTH_NAMES, _MONTHS
from app.professional.profile import parse_date as _profile_parse_date

EXPLICIT_YEARS = (1900, 2100)   # the years a day or month date may have; a bare year is narrower (_BARE below)
PRECISION_RANK = {"year": 0, "month": 1, "day": 2}   # coarser first: the order every consumer compares precisions by
_CUT = {"year": 4, "month": 7, "day": 10}
_PRECISION_OF_LENGTH = {4: "year", 7: "month"}   # any other length is a day


def cut(iso: str, precision: str) -> str:
    """``iso`` cut to ``precision`` (YYYY, YYYY-MM or YYYY-MM-DD); an unknown precision keeps the day."""
    return iso[:_CUT.get(precision, 10)]


def coarser(a: str, b: str) -> str:
    """The coarser of two precisions."""
    return min(a, b, key=PRECISION_RANK.__getitem__)


def precision_of(iso: str) -> str:
    """The precision an ISO text is written in, by its length."""
    return _PRECISION_OF_LENGTH.get(len(iso), "day")


def shown_date(iso: str, precision: str) -> str:
    """A date in the precision it was given: DD/MM/YYYY, MM/YYYY or YYYY (a month is never shown as a day)."""
    y, m, d = (iso.split("-") + [None, None])[:3]
    return f"{d}/{m}/{y}" if precision == "day" else f"{m}/{y}" if precision == "month" else y


def shown_period(iso: str, precision: str, end: str | None) -> str:
    """A date, or a range ``first–end`` of dates in the same precision, as the reader sees it."""
    first = shown_date(iso, precision)
    return f"{first}–{shown_date(end, precision)}" if end else first


@dataclass(frozen=True)
class Parsed:
    """One date expression. ``iso`` is YYYY-MM-DD, YYYY-MM or YYYY by ``precision``; ``end`` is the end of a range in the
    same precision (None for a single date); ``written`` is the text as the source wrote it and ``span`` its offsets
    in the searched text (the date word or Hebrew prefix is not part of it)."""
    iso: str
    precision: str
    end: str | None
    written: str
    span: tuple[int, int]


@dataclass(frozen=True)
class _Atom:
    start: int                 # the span of the expression itself
    end: int
    ctx: int                   # where the token begins, prefix letters included: the text before it is the context
    iso: str | None            # None: a date-shaped text that is no valid date (it still blocks other readings)
    precision: str
    bare: bool                 # a bare year (needs a date word or a range)
    pre: str = ""              # the Hebrew prefix letters of a month name


# --- patterns --------------------------------------------------------------------------------------------------------

# A digit run is never cut: no digit (or digit and a separator) before, no digit (or separator and digit) after.
_L = r"(?<!\d)(?<!\d[./,])"
_R = r"(?!\d)(?![./,]\d)"
_DMY = re.compile(rf"{_L}(?P<d>\d{{1,2}})(?P<s>[./-])(?P<m>\d{{1,2}})(?P=s)(?P<y>\d{{4}}){_R}")
_YMD = re.compile(rf"{_L}(?P<y>\d{{4}})(?P<s>[./-])(?P<m>\d{{1,2}})(?P=s)(?P<d>\d{{1,2}}){_R}")
_NAMED = re.compile(rf"(?:(?<!\d)(?P<d>\d{{1,2}})\s+)?(?<![א-ת])(?P<pre>ו?[בלמ]|ו)?(?P<mon>{_MONTH_NAMES})\s+(?P<y>\d{{4}}){_R}")
_MONTHNUM = re.compile(rf"{_L}(?P<m>\d{{1,2}})(?P<s>[./])(?P<y>\d{{4}}){_R}")
BARE = re.compile(rf"{_L}(?P<y>19[5-9]\d|20\d\d|2100){_R}")

_HYPHEN = "-־‐‑–—"
_DATE_WORD = re.compile(rf"(?<![א-ת])(?:(?:בשנת|לשנת|משנת|שנת|ועד|עד|בין|מאז)\s*|[בלמ][{_HYPHEN}]\s*|[בלמ])$")
_DASH = re.compile(rf"\s*[{_HYPHEN}]\s*")
_UNTIL = re.compile(rf"\s*(?:ועד|עד)\s*(?:ל[{_HYPHEN}]?)?\s*")
_BETWEEN = re.compile(rf"\s*(?:לבין|ובין|ל|ו)[{_HYPHEN}]?\s*")
_BETWEEN_WORD = re.compile(r"(?<![א-ת])בין\s*$")
_FROM_WORD = re.compile(rf"(?<![א-ת])מ[{_HYPHEN}]?\s*$")

_Q = '["״\'׳]'
_UNIT_AFTER = re.compile(
    rf"\s*(?:₪|%|\$|€|ש{_Q}?ח(?![א-ת])|שקל(?:ים)?(?![א-ת])|אלפים?(?![א-ת])|מיליון(?![א-ת])|מ{_Q}ר(?![א-ת])"
    rf"|מ{_Q}(?![א-ת])|מטר(?:ים)?(?![א-ת])|דונם(?![א-ת])|דונמים(?![א-ת])|אחוז(?:ים)?(?![א-ת])|דולר(?:ים)?(?![א-ת]))")
_MONEY_BEFORE = re.compile(r"(?:₪|\$|€)\s*$")
_LABEL_BEFORE = re.compile(
    rf"(?<![א-ת])[בלהמוש]{{0,2}}(?:גוש(?:ים)?|חלק(?:ה|ות)|מגרש(?:ים)?|תקן(?:ים)?|ת{_Q}י|סעיפים|סעיף|רחוב|רח['׳])"
    rf"\s*(?:ישראלי\s+)?(?:מס(?:פר|['׳.])?\s*)?[:{_HYPHEN}]?\s*$")
_STREET_BEFORE = re.compile(r"(?<![א-ת])[בלהמוש]?רחוב\s+(?:[א-ת\"״'׳]+\s+){1,3}$")
_QUARTER_BEFORE = re.compile(r"(?:\bQ[1-4]|רבעון|הרבעון|ברבעון|רבעוני)[^.;,\n]{0,25}$")
_QUARTER_AFTER = re.compile(r"\s*[,\-]?\s*Q[1-4]\b")


# --- reading ---------------------------------------------------------------------------------------------------------

def _valid_year(y: int) -> bool:
    return EXPLICIT_YEARS[0] <= y <= EXPLICIT_YEARS[1]


def _day_iso(y: int, m: int, d: int) -> str | None:
    """The day date, checked by the profile parser (the calendar and the month table are its)."""
    if not _valid_year(y) or d < 1:   # the profile parser reads day 0 as "no day" and answers the month
        return None
    iso = _profile_parse_date(f"{d}.{m}.{y}")
    return iso if iso and len(iso) == 10 else None


def _atoms(text: str) -> list[_Atom]:
    """Every date-shaped text, in text order, without overlaps: the more specific reading wins (a full date is not also
    its month or year). A shaped text that is no valid date stays as an invalid atom so no other reading takes it."""
    taken: list[_Atom] = []

    def add(atom: _Atom) -> None:
        if all(atom.end <= t.start or atom.start >= t.end for t in taken):
            taken.append(atom)

    for m in _DMY.finditer(text):
        add(_Atom(m.start(), m.end(), m.start(), _day_iso(int(m["y"]), int(m["m"]), int(m["d"])), "day", False))
    for m in _YMD.finditer(text):
        add(_Atom(m.start(), m.end(), m.start(), _day_iso(int(m["y"]), int(m["m"]), int(m["d"])), "day", False))
    for m in _NAMED.finditer(text):
        month, year = _MONTHS[m["mon"]], int(m["y"])
        if m["d"]:
            iso, precision, start = _day_iso(year, month, int(m["d"])), "day", m.start("d")
        else:
            iso, precision, start = (f"{year:04d}-{month:02d}" if _valid_year(year) else None), "month", m.start("mon")
        add(_Atom(start, m.end(), m.start(), iso, precision, False, m["pre"] or ""))
    for m in _MONTHNUM.finditer(text):
        month, year = int(m["m"]), int(m["y"])
        iso = f"{year:04d}-{month:02d}" if 1 <= month <= 12 and _valid_year(year) else None
        add(_Atom(m.start(), m.end(), m.start(), iso, "month", False))
    for m in BARE.finditer(text):
        add(_Atom(m.start(), m.end(), m.start(), m["y"], "year", True))
    return sorted(taken, key=lambda a: a.start)


def _masked(text: str, first: _Atom, last: _Atom, *, word: bool) -> bool:
    """True when the number is not a date: a parcel, standard, section or street number, a price, an area, a percentage
    or a quarter. ``word`` is True for a bare year read after a date word: the word already says it is a year, so the
    street rule (a house number) does not apply."""
    before, after = text[:first.ctx], text[last.end:]
    if _MONEY_BEFORE.search(before) or _UNIT_AFTER.match(after) or _LABEL_BEFORE.search(before):
        return True
    if _QUARTER_BEFORE.search(before) or _QUARTER_AFTER.match(after):
        return True
    return not word and bool(_STREET_BEFORE.search(before))


def _coarse(a: _Atom, b: _Atom) -> tuple[str, str, str]:
    precision = coarser(a.precision, b.precision)
    return cut(a.iso, precision), cut(b.iso, precision), precision  # type: ignore[arg-type]


def _range_of(text: str, a: _Atom, b: _Atom) -> bool:
    """True when ``a`` and ``b`` (neighbours) are the two ends of one range, by the words and signs between them."""
    between, before = text[a.end:b.start], text[:a.ctx]
    if a.iso is None or b.iso is None:
        return False
    if _DASH.fullmatch(between):
        return a.bare == b.bare
    if _UNTIL.fullmatch(between):
        return a.bare == b.bare or a.pre == "מ" or bool(_FROM_WORD.search(before))
    return bool(_BETWEEN_WORD.search(before) and _BETWEEN.fullmatch(between))


def find_all(text: str) -> list[Parsed]:
    """The date expressions of ``text`` in text order, each with its span (see the module docstring for the formats
    and for what is refused)."""
    text = text or ""
    atoms = _atoms(text)
    found: list[Parsed] = []
    i = 0
    while i < len(atoms):
        a = atoms[i]
        b = atoms[i + 1] if i + 1 < len(atoms) else None
        if b is not None and _range_of(text, a, b):
            start, end, precision = _coarse(a, b)
            if start <= end:
                if not _masked(text, a, b, word=False):
                    found.append(Parsed(start, precision, end, text[a.start:b.end], (a.start, b.end)))
                i += 2
                continue
        i += 1
        if a.iso is None:
            continue
        word = a.bare and bool(_DATE_WORD.search(text[:a.ctx]))
        if a.bare and not word:
            continue
        if not _masked(text, a, a, word=word):
            found.append(Parsed(a.iso, a.precision, None, text[a.start:a.end], (a.start, a.end)))
    return found


def parse(text: str | None) -> Parsed | None:
    """The date a short text holds, or None (unknown) when it holds none or more than one: a text with two dates is
    not one expression, and the caller then reads its sentence by `find_all`. A bare year without a date word is no
    date here either ("2024" alone is a number)."""
    found = find_all(text or "")
    return found[0] if len(found) == 1 else None
