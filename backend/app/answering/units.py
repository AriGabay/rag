"""Hebrew unit parsing and conversion by dimension (U7, KTD8 step 4).

The server, not the model, decides a mention's value and unit: the number is read from the quote and
the unit is the unit word written right after it (or, for currency signs and counted nouns such as
"קומה", right before it). Every unit belongs to one dimension (area, length, currency, ...) and has a
factor to that dimension's canonical unit (``attributes.CANONICAL_UNITS``). A value converts only
within its dimension; anything else raises ``DimensionMismatch``, so a floor number next to an area
("בקומה 3, שטח 12 מ״ר") can never become an area. All arithmetic is ``Decimal``.

Text is compared after ``base_normalize`` (gershayim/geresh unified, thousands separators dropped), so
'מ"ר', "מ״ר" and "מ'ר" are the same unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.extraction.normalize_text import base_normalize

_PREFIX_LETTERS = "והבלמשכ"
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d])")
_WORDY = re.compile(r"[\w״׳²]")
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


def _norm(text: str) -> str:
    return base_normalize(text or "")


def _ends_word(text: str, end: int) -> bool:
    return end >= len(text) or not _WORDY.match(text[end])


def _unit_after(text: str, end: int) -> Unit | None:
    rest = text[end:]
    stripped = rest.lstrip()
    offset = end + len(rest) - len(stripped)
    for surface in _ORDERED:
        if stripped.startswith(surface) and (surface == "%" or _ends_word(text, offset + len(surface))):
            return _SURFACES[surface]
    return None


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
        unit = _unit_after(norm, m.end()) or _unit_before(norm, m.start())
        out.append(Quantity(value, unit, m.start(), m.end()))
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


def parse_mention_quantity(quote: str, value_text: str, dimension: str | None = None) -> Quantity | None:
    """The quantity in ``quote`` whose number equals the number in ``value_text``; None when the quote
    does not contain that number. Among several equal numbers, one whose unit measures ``dimension``
    wins, then one with any unit."""
    wanted = parse_number(value_text)
    if wanted is None:
        return None
    matches = [q for q in find_quantities(quote) if q.value == wanted]
    if not matches:
        return None
    for q in matches:
        if dimension and q.unit is not None and q.unit.dimension == dimension:
            return q
    return next((q for q in matches if q.unit is not None), matches[0])


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
