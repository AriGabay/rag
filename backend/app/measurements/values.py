"""Reading numbers as written in Hebrew appraisal text and tables.

``parse_amount`` turns a written value into a Decimal and its form: "₪ 9,500" -> 9500 exact, "כ-21,000 ₪" ->
21000 approximate, "65-85 ₪" -> range 65..85, "(₪ 1,197,500)" -> -1197500 (accounting negative), "6.70%" -> 6.70,
"שישה" -> 6 (a number in words is ``numread.number_word``, the project's one reader). Nothing is guessed: text without a number gives None.

A written value is one table cell, so its comma is read by ``numread`` as a cell's: "13,2" is 13.2, "1,250" is 1250, "1.234,56" is
1234.56, a chain "6,7,8" is a list. A negative is a minus (hyphen or U+2212) before the number, a trailing minus ("104,610-"), or
accounting brackets with an optional percent or currency ("(3.5)%", "(₪ 1,197,500)").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.answering.numread import CURRENCY, number_word, read_separators

_MINUS = "[-\u2212]"
_SIGN_NOISE = re.compile(rf"[{CURRENCY}%\s]")  # what may stand beside the brackets of an accounting negative: "(3.5)%", "(₪ 1,197,500)"
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RANGE = re.compile(rf"({_NUM})\s*(?:-|–|—|עד|ל-)\s*({_NUM})")
_ONE = re.compile(_NUM)
_APPROX = re.compile(r"(?:^|[\s(])(?:כ-?|כ־|כ\s|בערך|כמעט|בסביבות|בגבולות(?:\s+של)?|כ-\s?|~)")
_MIN = re.compile(r"(?:החל\s+מ|לפחות|מינימום|לא\s+פחות\s+מ|מעל)")
_MAX = re.compile(r"(?:עד\s|לכל\s+היותר|מקסימום|לא\s+יותר\s+מ|פחות\s+מ)")

@dataclass(frozen=True)
class Amount:
    value: Decimal | None
    low: Decimal | None
    high: Decimal | None
    form: str  # exact | approximate | range | minimum | maximum


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def parse_amount(text: str) -> Amount | None:
    t = (text or "").replace("‏", "").replace("‎", "").strip()
    if not t:
        return None
    t = read_separators(t, cell=True)
    negative = bool(re.fullmatch(r"\(.*\d.*\)", _SIGN_NOISE.sub("", t))) or bool(
        re.match(rf"^\s*{_MINUS}\s*[{CURRENCY}]?\s*\d", t) or re.match(rf"^\s*[{CURRENCY}]\s*{_MINUS}\s*\d", t)
        or re.search(rf"\d[\s%{CURRENCY}]*{_MINUS}\s*$", t))
    m = _RANGE.search(t)
    if m and not re.search(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", t):
        low, high = _dec(m.group(1)), _dec(m.group(2))
        if low is not None and high is not None and low <= high:
            return Amount(None, low, high, "range")
    m = _ONE.search(t)
    if m is None:
        v = number_word(t)
        return Amount(v, None, None, "exact") if v is not None else None
    value = _dec(m.group(0))
    if value is None:
        return None
    if negative:
        value = -value
    before = t[: m.start()]
    if _APPROX.search(" " + before):
        form = "approximate"
    elif _MIN.search(before):
        form = "minimum"
    elif _MAX.search(before):
        form = "maximum"
    else:
        form = "exact"
    return Amount(value, None, None, form)


def form_before(text_before: str) -> str | None:
    """The form a value takes from the words right before it ("בגבולות של 9,500", "החל מ- 8,000"), or None."""
    tail = " " + text_before[-30:]
    if _APPROX.search(tail):
        return "approximate"
    if _MIN.search(tail):
        return "minimum"
    if _MAX.search(tail):
        return "maximum"
    return None


def digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def same_number(written: str, value: Decimal | None) -> bool:
    """Whether ``written`` states ``value`` (digits compared without separators; a number word by its value)."""
    if value is None:
        return False
    a = parse_amount(written)
    if a is None:
        return False
    candidates = [a.value, a.low, a.high]
    return any(c is not None and abs(c) == abs(value) for c in candidates)
