"""Hebrew unit parsing and conversion by dimension (U7, KTD8 step 4).

The server, not the model, decides a mention's value and unit: the number is read from the quote and
the unit is the unit word written right after it (or, for currency signs and counted nouns such as
"קומה", right before it). Every unit belongs to one dimension (area, length, currency, ...) and has a
factor to that dimension's canonical unit (``attributes.CANONICAL_UNITS``). A value converts only
within its dimension; anything else raises ``DimensionMismatch``, so a floor number next to an area
("בקומה 3, שטח 12 מ״ר") can never become an area. All arithmetic is ``Decimal``.

Text is compared after ``base_normalize`` (gershayim/geresh unified, thousands separators dropped), so
'מ"ר', "מ״ר" and "מ'ר" are the same unit.

For a count, Hebrew number words one to twenty ("שתי", "שלושה", "שתים עשרה") are numbers too, and "אין" /
"ללא" ("none") is zero. Two lengths written as W×H ("300 על 350 ס״מ") give an area only through
``area_from_dimensions``, always as an assumption (the fact goes to review).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.extraction.normalize_text import base_normalize

_PREFIX_LETTERS = "והבלמשכ"
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d])")
_WORDY = re.compile(r"[\w״׳²]")
_TOKEN = re.compile(r"[^\W\d_][\w״׳]*")
_PAIR_SEPARATORS = ("×", "x", "*", "על")
# Dimensions whose values may be written without a unit (a count, a calendar year, a ratio).
UNITLESS_OK = frozenset({"count", "year", "ratio"})


class DimensionMismatch(ValueError):
    """The quoted unit does not measure the attribute's dimension (or a required unit is missing)."""


@dataclass(frozen=True)
class Unit:
    code: str
    dimension: str
    factor: Decimal  # multiply by this to get the dimension's canonical unit
    before_ok: bool = False  # may precede the number ("₪ 100", "קומה 3")


@dataclass(frozen=True)
class Quantity:
    value: Decimal
    unit: Unit | None
    start: int  # span of the number in the normalized text
    end: int
    unit_end: int | None = None  # end of the unit word when it is written after the number
    zero_word: bool = False  # "אין" / "ללא": a stated absence read as zero


@dataclass(frozen=True)
class Conversion:
    value: Decimal
    canonical_unit: str | None
    assumed: bool = False  # converted under an assumption (the fact goes to review)


def _u(code: str, dimension: str, factor: str | int, before_ok: bool = False) -> Unit:
    return Unit(code, dimension, Decimal(str(factor)), before_ok)


SQM = _u("sqm", "area", 1)
METER = _u("m", "length", 1)
ILS = _u("ILS", "currency", 1, before_ok=True)
_AREA_WORDS = ["מ״ר", "מ׳ר", "מטר רבוע", "מטר מרובע", "מטרים רבועים", "מטרים מרובעים", "מ2", "מ²", "m2", "m²", "sqm"]
_CURRENCY_WORDS = ["₪", "ש״ח", "שקל", "שקלים", "שקלים חדשים", "nis", "ils"]
_SURFACES: dict[str, Unit] = {
    **{w: SQM for w in _AREA_WORDS},
    "סמ״ר": _u("sqcm", "area", "0.0001"),
    "קמ״ר": _u("sqkm", "area", 1_000_000),
    "דונם": _u("dunam", "area", 1000), "דונמים": _u("dunam", "area", 1000),
    "מטר": METER, "מטרים": METER, "מ׳": METER,
    "ס״מ": _u("cm", "length", "0.01"), "סנטימטר": _u("cm", "length", "0.01"),
    "סנטימטרים": _u("cm", "length", "0.01"), "cm": _u("cm", "length", "0.01"),
    "מ״מ": _u("mm", "length", "0.001"), "מילימטר": _u("mm", "length", "0.001"),
    "ק״מ": _u("km", "length", 1000), "קילומטר": _u("km", "length", 1000),
    "מ״ק": _u("m3", "volume", 1), "קוב": _u("m3", "volume", 1), "מטר מעוקב": _u("m3", "volume", 1),
    "ליטר": _u("liter", "volume", "0.001"), "ליטרים": _u("liter", "volume", "0.001"),
    **{w: ILS for w in _CURRENCY_WORDS},
    "%": _u("percent", "percent", 1), "אחוז": _u("percent", "percent", 1), "אחוזים": _u("percent", "percent", 1),
    "שנה": _u("year", "duration", 12), "שנים": _u("year", "duration", 12),
    "חודש": _u("month", "duration", 1), "חודשים": _u("month", "duration", 1),
}
for _word, _code in (("קומה", "floor"), ("קומות", "floor"), ("יח׳", "dwelling_unit"), ("יח״ד", "dwelling_unit"),
                     ("יחידות", "dwelling_unit"), ("יחידות דיור", "dwelling_unit"), ("דירות", "dwelling_unit"),
                     ("חדר", "room"), ("חדרים", "room")):
    _SURFACES[_word] = _u(_code, "count", 1, before_ok=True)
for _cur in _CURRENCY_WORDS:
    for _mult, _code, _factor in (("אלף", "thousand_ILS", 1000), ("אלפי", "thousand_ILS", 1000),
                                  ("מיליון", "million_ILS", 1_000_000), ("מליון", "million_ILS", 1_000_000),
                                  ("מ׳", "million_ILS", 1_000_000)):
        _SURFACES[f"{_mult} {_cur}"] = _u(_code, "currency", _factor)
    for _area in ("מ״ר", "מ׳ר", "מטר", "מ2"):
        for _sep in (" ל", "/", " / ", " ל-"):
            _SURFACES[f"{_cur}{_sep}{_area}"] = _u("ILS/sqm", "currency_per_area", 1)
_ORDERED = sorted(_SURFACES, key=len, reverse=True)

# Hebrew number words (masculine, feminine and construct forms). Standalone "שנים" is not listed: it is also
# the plural of "year"; it counts only inside a compound ("שנים עשר").
_ONES: dict[int, tuple[str, ...]] = {
    1: ("אחד", "אחת"), 2: ("שניים", "שתיים", "שני", "שתי", "שתים"), 3: ("שלוש", "שלושה", "שלושת"),
    4: ("ארבע", "ארבעה", "ארבעת"), 5: ("חמש", "חמישה", "חמשה", "חמשת"), 6: ("שש", "שישה", "ששה", "ששת"),
    7: ("שבע", "שבעה", "שבעת"), 8: ("שמונה", "שמונת"), 9: ("תשע", "תשעה", "תשעת"),
    10: ("עשר", "עשרה", "עשרת"),
}
NUMBER_WORDS: dict[str, int] = {w: n for n, words in _ONES.items() for w in words} | {"עשרים": 20}
for _n, _words in _ONES.items():
    if _n < 10:
        for _w in (*_words, *(("שנים",) if _n == 2 else ())):
            for _ten in ("עשר", "עשרה"):
                NUMBER_WORDS[f"{_w} {_ten}"] = 10 + _n
ZERO_WORDS = frozenset({"אין", "ללא"})
# Units that are counted nouns: a count of floors, rooms or dwellings is a count of that noun only, never of
# whatever else the attribute counts ("2 X בקומה 3": the 3 counts floors, not X).
COUNTED_NOUN_CODES = frozenset({"floor", "room", "dwelling_unit"})
COUNTED_NOUN_WORDS: dict[str, tuple[str, ...]] = {
    code: tuple(w for w, u in _SURFACES.items() if u.code == code) for code in COUNTED_NOUN_CODES}


def _norm(text: str) -> str:
    return base_normalize(text or "")


def _ends_word(text: str, end: int) -> bool:
    return end >= len(text) or not _WORDY.match(text[end])


def _unit_after_span(text: str, end: int) -> tuple[Unit, int] | None:
    """The unit written right after ``end`` and where its word ends."""
    rest = text[end:]
    stripped = rest.lstrip()
    offset = end + len(rest) - len(stripped)
    for surface in _ORDERED:
        if stripped.startswith(surface) and (surface == "%" or _ends_word(text, offset + len(surface))):
            return _SURFACES[surface], offset + len(surface)
    return None


def _unit_after(text: str, end: int) -> Unit | None:
    found = _unit_after_span(text, end)
    return found[0] if found else None


def _unit_before(text: str, start: int) -> Unit | None:
    head = text[:start].rstrip()
    for surface in _ORDERED:
        unit = _SURFACES[surface]
        if not unit.before_ok or not head.endswith(surface):
            continue
        word_start = head.rfind(" ", 0, len(head) - len(surface)) + 1
        prefix = head[word_start:len(head) - len(surface)]
        if len(prefix) <= 2 and all(ch in _PREFIX_LETTERS for ch in prefix):
            return unit
    return None


def find_quantities(text: str) -> list[Quantity]:
    """Every number in ``text`` (normalized) with the unit written after it, else before it."""
    norm = _norm(text)
    out = []
    for m in _NUMBER.finditer(norm):
        try:
            value = Decimal(m.group(0))
        except InvalidOperation:  # pragma: no cover - the pattern only matches decimals
            continue
        after = _unit_after_span(norm, m.end())
        unit = after[0] if after else _unit_before(norm, m.start())
        out.append(Quantity(value, unit, m.start(), m.end(), after[1] if after else None))
    return out


def parse_unit(text: str) -> Unit | None:
    """The first unit written anywhere in ``text`` (a table column header such as "שטח (מ״ר)")."""
    norm = _norm(text)
    best: tuple[int, Unit] | None = None
    for surface in _ORDERED:
        for m in re.finditer(re.escape(surface), norm):
            before_ok = m.start() == 0 or not _WORDY.match(norm[m.start() - 1])
            if before_ok and (surface == "%" or _ends_word(norm, m.end())) and (best is None or m.start() < best[0]):
                best = (m.start(), _SURFACES[surface])
    return best[1] if best else None


def parse_number(text: str) -> Decimal | None:
    m = _NUMBER.search(_norm(text))
    return Decimal(m.group(0)) if m else None


def _bare_variants(token: str) -> list[str]:
    """The token, then the token without a one- or two-letter prefix ("ושתי" -> "שתי", "והשתיים" -> "שתיים")."""
    out = [token]
    for k in (1, 2):
        if len(token) - k >= 2 and all(ch in _PREFIX_LETTERS for ch in token[:k]):
            out.append(token[k:])
    return out


def _word_number(words: list[str], with_zero: bool) -> tuple[int, int] | None:
    """(value, tokens used) of the number word starting ``words``: a two-word teen first, then one word."""
    if len(words) >= 2:
        for first in _bare_variants(words[0]):
            n = NUMBER_WORDS.get(f"{first} {words[1]}")
            if n is not None and n > 10:
                return n, 2
    for first in _bare_variants(words[0]):
        if first in NUMBER_WORDS:
            return NUMBER_WORDS[first], 1
        if with_zero and first in ZERO_WORDS:
            return 0, 1
    return None


def find_word_quantities(text: str, *, with_zero: bool = True) -> list[Quantity]:
    """Counts written as Hebrew words (one to twenty; "אין"/"ללא" as zero when ``with_zero``)."""
    norm = _norm(text)
    tokens = list(_TOKEN.finditer(norm))
    out, i = [], 0
    while i < len(tokens):
        found = _word_number([t.group(0) for t in tokens[i:i + 2]], with_zero)
        if found is None:
            i += 1
            continue
        value, used = found
        start, end = tokens[i].start(), tokens[i + used - 1].end()
        after = _unit_after_span(norm, end)
        unit = after[0] if after else _unit_before(norm, start)
        zero = value == 0 and any(v in ZERO_WORDS for v in _bare_variants(tokens[i].group(0)))
        out.append(Quantity(Decimal(value), unit, start, end, after[1] if after else None, zero))
        i += used
    return out


def _value_of(value_text: str, dimension: str | None) -> Decimal | None:
    wanted = parse_number(value_text)
    if wanted is not None or dimension != "count":
        return wanted
    words = [t.group(0) for t in _TOKEN.finditer(_norm(value_text))]
    found = _word_number(words, with_zero=True) if words else None
    return Decimal(found[0]) if found is not None and found[1] == len(words) else None


def parse_mention_quantity(quote: str, value_text: str, dimension: str | None = None) -> Quantity | None:
    """The quantity in ``quote`` whose number equals the number in ``value_text``; None when the quote
    does not contain that number. Among several equal numbers, one whose unit measures ``dimension``
    wins, then one with any unit. For a ``count``, number words and "none" words count as numbers, both in
    the quote and in ``value_text``."""
    wanted = _value_of(value_text, dimension)
    if wanted is None:
        return None
    found = find_quantities(quote) + (find_word_quantities(quote) if dimension == "count" else [])
    matches = [q for q in found if q.value == wanted]
    if not matches:
        return None
    for q in matches:
        if dimension and q.unit is not None and q.unit.dimension == dimension:
            return q
    return next((q for q in matches if q.unit is not None), matches[0])


def _plain(value: Decimal) -> Decimal:
    """``value`` without trailing zeros and without an exponent (10.5000 -> 10.5, 1E+1 -> 10)."""
    value = value.normalize()
    return value.quantize(Decimal(1)) if value.as_tuple().exponent > 0 else value


def _after_unit_surface(text: str) -> str:
    """``text`` (what follows a number) without the unit word it starts with."""
    stripped = text.lstrip()
    for surface in _ORDERED:
        if stripped.startswith(surface):
            return stripped[len(surface):]
    return stripped


def area_from_dimensions(quote: str, value_text: str) -> Conversion | None:
    """An area from exactly one pair of explicit lengths written W×H in ``quote`` ("300 על 350 ס״מ",
    "2.5 x 3 מ׳"), with the separator ×, x, * or על. Both lengths need a length unit (the second's unit
    covers a bare first). The numbers of ``value_text`` must be the two lengths or the area itself.
    Always ``assumed``: a room's inner dimensions are not necessarily its stated area. None otherwise."""
    norm = _norm(quote)
    found = find_quantities(norm)
    pairs = []
    for a, b in zip(found, found[1:], strict=False):
        between = norm[a.end:b.start]
        own_a = _unit_after(norm, a.end)
        if own_a is not None:
            between = _after_unit_surface(between)
        if between.strip() not in _PAIR_SEPARATORS:
            continue
        own_b = _unit_after(norm, b.end)
        unit_a = own_a or own_b
        if unit_a is None or own_b is None or unit_a.dimension != "length" or own_b.dimension != "length":
            return None  # two numbers joined like dimensions, but not as two lengths
        pairs.append((a.value * unit_a.factor, b.value * own_b.factor, a.value, b.value))
    if len(pairs) != 1:
        return None
    width, height, raw_a, raw_b = pairs[0]
    area = _plain(width * height)
    stated = [Decimal(m.group(0)) for m in _NUMBER.finditer(_norm(value_text))]
    if not stated or any(v not in (raw_a, raw_b, area) for v in stated):
        return None
    from app.answering.attributes import canonical_unit_for

    return Conversion(area, canonical_unit_for("area"), assumed=True)


def convert(value: Decimal, unit: Unit | None, dimension: str | None) -> Conversion:
    """``value`` in ``unit`` converted to the canonical unit of ``dimension``.

    Bare meters for an area ("שטח של 12 מטר") are read as square meters with ``assumed=True``.
    An attribute without a dimension takes unitless numbers as they are and any unit as an assumption."""
    from app.answering.attributes import canonical_unit_for

    if dimension is None:
        if unit is None:
            return Conversion(value, None)
        return Conversion(value * unit.factor, canonical_unit_for(unit.dimension), assumed=True)
    canonical = canonical_unit_for(dimension)
    if unit is None:
        if dimension in UNITLESS_OK:
            return Conversion(value, canonical)
        raise DimensionMismatch(f"a {dimension} value needs a unit")
    if unit.dimension == dimension:
        return Conversion(value * unit.factor, canonical)
    if dimension == "area" and unit.code == "m":
        return Conversion(value, canonical, assumed=True)
    raise DimensionMismatch(f"{unit.code} measures {unit.dimension}, not {dimension}")


def per_area(unit: Unit) -> Unit | None:
    """A currency unit as a rate per m² ("₪" under the label "שווי למ״ר"): same factor, per-area dimension."""
    if unit.dimension != "currency":
        return None
    return Unit(f"{unit.code}/sqm", "currency_per_area", unit.factor, unit.before_ok)


def names_per_area(words: list[str]) -> bool:
    """Whether the words state a rate per area: "ל" + an area unit ("למ״ר", "למטר רבוע") or "/מ״ר"."""
    for w in words:
        for cand in (w[1:] if w.startswith("ל") else None, w.lstrip("/") if w.startswith("/") else None):
            if cand and (u := parse_unit(cand)) is not None and u.dimension == "area":
                return True
    return False
