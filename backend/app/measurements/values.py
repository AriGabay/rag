"""Reading numbers as written in Hebrew appraisal text and tables.

``parse_amount`` turns a written value into a Decimal and its form: "₪ 9,500" -> 9500 exact, "כ-21,000 ₪" ->
21000 approximate, "65-85 ₪" -> range 65..85, "(₪ 1,197,500)" -> -1197500 (accounting negative), "6.70%" -> 6.70,
"שישה" -> 6. Nothing is guessed: text without a number gives None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_RANGE = re.compile(rf"({_NUM})\s*(?:-|–|—|עד|ל-)\s*({_NUM})")
_ONE = re.compile(_NUM)
_APPROX = re.compile(r"(?:^|[\s(])(?:כ-?|כ־|כ\s|בערך|כמעט|בסביבות|בגבולות(?:\s+של)?|כ-\s?|~)")
_MIN = re.compile(r"(?:החל\s+מ|לפחות|מינימום|לא\s+פחות\s+מ|מעל)")
_MAX = re.compile(r"(?:עד\s|לכל\s+היותר|מקסימום|לא\s+יותר\s+מ|פחות\s+מ)")

# Hebrew number words (both genders, construct forms) used for counts and durations in appraisal prose.
_UNITS = {
    "אחד": 1, "אחת": 1, "שניים": 2, "שתיים": 2, "שתים": 2, "שני": 2, "שתי": 2, "שלושה": 3, "שלוש": 3, "שלושת": 3,
    "ארבעה": 4, "ארבע": 4, "ארבעת": 4, "חמישה": 5, "חמש": 5, "חמשת": 5, "שישה": 6, "שש": 6, "ששת": 6,
    "שבעה": 7, "שבע": 7, "שבעת": 7, "שמונה": 8, "שמונת": 8, "תשעה": 9, "תשע": 9, "תשעת": 9,
    "עשרה": 10, "עשר": 10, "עשרת": 10,
}
_TENS = {"עשרים": 20, "שלושים": 30, "ארבעים": 40, "חמישים": 50, "שישים": 60, "שבעים": 70, "שמונים": 80,
         "תשעים": 90, "מאה": 100, "מאתיים": 200, "אלף": 1000, "אלפיים": 2000}


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


def number_word(text: str) -> Decimal | None:
    """The value of a Hebrew number phrase ("שישה", "עשרים וחמישה", "שתים עשרה"), or None."""
    words = [w[1:] if w.startswith("ו") and w[1:] in {**_UNITS, **_TENS} else w
             for w in re.findall(r"[א-ת]+", text)]
    if not words:
        return None
    total = 0
    found = False
    for i, w in enumerate(words):
        if w in ("עשר", "עשרה") and i > 0 and words[i - 1] in _UNITS and _UNITS[words[i - 1]] < 10:
            continue  # the teen's second word, counted with its first ("שתים עשרה")
        if w in _TENS:
            total += _TENS[w]
            found = True
        elif w in _UNITS:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            total += _UNITS[w] + (10 if nxt in ("עשר", "עשרה") and _UNITS[w] < 10 else 0)
            found = True
        elif found:
            break
    return Decimal(total) if found else None


def parse_amount(text: str) -> Amount | None:
    t = (text or "").replace("‏", "").replace("‎", "").strip()
    if not t:
        return None
    negative = bool(re.fullmatch(r"\(.*\d.*\)", t.replace("₪", "").strip())) or bool(
        re.match(r"^\s*-\s*₪?\s*\d", t) or re.match(r"^\s*₪\s*-\s*\d", t) or re.match(r"^-₪", t))
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
