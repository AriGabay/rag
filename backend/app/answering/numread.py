"""One reading of the separators and signs of a number written in a document (round 19 U1, KTD3, R11, R12).

The answering number reader (``answering.verify.numbers_in``), the stored-amount parser (``measurements.values.parse_amount``) and the
meaning reader (``chat.meaning``) all go through this module, so a comma and a sign mean the same in all three.

**A comma is read by context and by the convention of the text it stands in:**

- two or more ",ddd" groups are thousands ("3,596,696");
- when both separators appear, the later one is the decimal ("1.234,56" and "1,234.56" are 1234.56);
- a comma after a zero ("0,125") is a decimal comma;
- a single comma with one or two digits after it ("13,2") is a decimal comma when the token is a whole table cell, or sits next to a
  percent, a currency or a unit ("13,2%", "€ 13,2"), or when the same text already shows a certain decimal comma (a decimal
  convention); otherwise it is ambiguous: a list ("3,4" as three and four) or a decimal. A text read as a *source* keeps both readings
  ({3.4, 3, 4}); a *claim* takes the decimal reading only when its cited source's convention is decimal (``decimal_convention``);
- a chain ("6,7,8") is a list, never a decimal.

Residual ambiguity: a single ",ddd" ("1,250") always stays a thousands separator, so a decimal comma with three decimals ("1,250"
for 1.250) is read as 1250, as it was; and a pair "d,d" with no cell, unit or convention around it stays a list (claim) or both
readings (source). The reading is length-preserving: every position of the text stays valid (the European "1.234,56" keeps its
digits and its start, and trailing blanks fill the room of its dropped thousands points).

**A sign** is read from the text around a number: a minus (hyphen or U+2212) right before it and not after a letter or a digit
("ב-2019" and "10-20" are no negatives); in a source also accounting brackets ("(3.5)%", "(₪ 1,197,500)") and a trailing minus
("3.5-"). A negative number gives both its signed and its unsigned forms: an unsigned claim still finds it, a negative claim finds
only a negative source.

**A number written in Hebrew words** (round 22 U10, KTD9, R17) is read here too, by ``number_words``: the one table of the project (the
three that were kept in ``answering.verify``, ``measurements.values`` and ``answering.units`` are gone). A phrase is one number, whole:
units, teens and compounds ("שנים עשר", "שתים עשרה", "שבעה עשר"), tens ("עשרים וחמישה"), hundreds ("מאה", "מאתיים", "שלוש מאות") and
simple thousands ("אלפיים", "שלושת אלפים", "מאה וחמישים אלף"), masculine, feminine and construct forms, the prefix letters outside the
value, "and" between the parts. A word is read whole: "עשרים" inside a longer word is nothing, "שנים" alone is the plural of year, an
ordinal ("השני", "חמישית") and a scale word after a digit ("250 אלף") are no amount.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import NamedTuple

# A number may follow a Hebrew prefix letter ("ב2019"), but not a Latin letter, digit or underscore. A comma is a thousands
# separator only in groups of three ("1,250,000"): "2018,2019,2021" is a list of three years.
NUMBER = re.compile(r"(?<![0-9A-Za-z_])(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?")

CURRENCY = "₪$€£"
# what stands right after / before a number that makes its comma a decimal: a percent, a currency or a unit
_UNIT_AFTER = re.compile(r"\s?(?:%|[" + CURRENCY + r"]|ש[\"״]ח|שקל|מ[\"״][רק]|דונם|אלף|מיליון|מטר|ק[\"״]מ)")
_CURRENCY_BEFORE = re.compile(r"[" + CURRENCY + r"]\s?$")
# a European number: thousands points, then the decimal comma
_EURO = re.compile(r"(?<![0-9A-Za-z_.,])(\d{1,3}(?:\.\d{3})+),(\d+)(?!\d)(?![.,]\d)")
# a number with one comma that does not end a chain ("6,7,8") nor continue a thousands group ("1,250,5")
_SINGLE = re.compile(r"(?<![0-9A-Za-z_.,])(\d+),(\d+)(?!\d)(?![.,]\d)")
_CELL_BEFORE = re.compile(r"(?:.*\|)?\s*[-\u2212(]?\s*(?:[" + CURRENCY + r"]\s*)?")
_CELL_AFTER = re.compile(r"\s*\)?\s*(?:\|.*)?")


def _whole_cell(text: str, start: int, end: int) -> bool:
    """Whether the token ``text[start:end]`` is all of its table cell (or of its line): only blanks, a sign, a bracket or a currency
    sign around it between the cell's pipes."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    return bool(_CELL_BEFORE.fullmatch(text[line_start:start]) and _CELL_AFTER.fullmatch(text[end:line_end]))


def _classify(text: str, m: re.Match, *, cell: bool) -> str:
    """"decimal" (a certain decimal comma), "pair" (one or two digits after the comma, nothing settles it) or "" (thousands or a
    list: not a decimal comma) for the ``_SINGLE`` match ``m``."""
    whole, fraction = m.group(1), m.group(2)
    if whole.strip("0") == "" and whole:
        return "decimal"  # "0,125": a thousands group never follows a zero
    if len(fraction) > 2:
        return ""  # ",ddd" stays thousands (the residual ambiguity above); a longer tail is a list
    if cell or _UNIT_AFTER.match(text, m.end()) or _CURRENCY_BEFORE.search(text, max(0, m.start() - 3), m.start()) \
            or _whole_cell(text, m.start(), m.end()):
        return "decimal"
    return "pair"


@lru_cache(maxsize=512)
def _scan(text: str, cell: bool) -> tuple[tuple[re.Match, ...], tuple[re.Match, ...], tuple[re.Match, ...]]:
    """The European numbers, the certain decimal commas and the pairs of ``text``. A function of its arguments alone, kept: the same text
    is read for its convention, its separators and its spans (call it with both arguments positional, so that one text has one entry)."""
    euro = tuple(_EURO.finditer(text))
    decimals, pairs = [], []
    for m in _SINGLE.finditer(text):
        kind = _classify(text, m, cell=cell)
        if kind == "decimal":
            decimals.append(m)
        elif kind == "pair":
            pairs.append(m)
    return euro, tuple(decimals), tuple(pairs)


@lru_cache(maxsize=1024)
def decimal_convention(text: str) -> bool:
    """Whether ``text`` shows a certain decimal comma ("1.234,56", "0,125", "13,2%", a cell "13,2"): the text writes its decimals
    with a comma."""
    if "," not in text:
        return False
    euro, decimals, _ = _scan(text, False)
    return bool(euro or decimals)


@lru_cache(maxsize=1024)
def read_separators(text: str, *, cell: bool = False, decimal: bool = False) -> str:
    """``text`` with its decimal commas written as points ("13,2%" -> "13.2%", "1.234,56" -> "1234.56 "), the same length. ``cell``:
    the whole text is one table cell or value, so a single comma with one or two digits is a decimal. ``decimal``: the text is read
    under a decimal convention (a claim whose cited source writes decimals with a comma). A text with a certain decimal comma is read
    under that convention as a whole. Thousands groups, lists and chains stay as written."""
    if "," not in text:
        return text
    euro, decimals, pairs = _scan(text, cell)
    convention = decimal or cell or bool(euro or decimals)
    edits: list[tuple[int, int, str]] = []
    for m in euro:
        digits = m.group(1).replace(".", "")
        new = f"{digits}.{m.group(2)}"
        edits.append((m.start(), m.end(), new + " " * (m.end() - m.start() - len(new))))
    for m in (*decimals, *(pairs if convention else ())):
        edits.append((m.start(), m.end(), f"{m.group(1)}.{m.group(2)}"))
    if not edits:
        return text
    out = text
    for start, end, new in sorted(edits, reverse=True):
        out = out[:start] + new + out[end:]
    return out


def add_form(out: set[str], token: str) -> None:
    """The forms of one written number: ``str(Decimal)`` normalized, and its integer form when it is whole (2.80 and 2.8, 1,250 and 1250)."""
    try:
        value = Decimal(token.rstrip(".,").replace(",", ""))
    except InvalidOperation:
        return
    out.add(str(value.normalize()))
    if value == value.to_integral_value():
        out.add(str(int(value)))


_YEAR = re.compile(r"(?:19|20)\d\d")
# a minus glued to a number (a currency sign may stand between them), after a blank, a bracket or the start of the text: "- 3 חדרים", a
# list bullet, is no minus
_MINUS_BEFORE = re.compile(r"(?P<lead>^|[\s(\[:=;|])[-\u2212](?:[" + CURRENCY + r"]\s?)?$"
                           r"|[" + CURRENCY + r"]\s?[-\u2212]$")
_BRACKET_BEFORE = re.compile(r"\(\s?(?:[" + CURRENCY + r"]\s?)?$")
_BRACKET_AFTER = re.compile(r"\s?(?:%\s?)?\)")
_TRAILING_MINUS = re.compile(r"[-\u2212](?![\w\u0590-\u05FF])(?!\s?\d)")


def _negative(text: str, start: int, end: int, *, source: bool) -> bool:
    """Whether the number at ``text[start:end]`` is written negative: a minus before it that is neither the hyphen of a prefix, a range
    nor a bare year; in a ``source`` also accounting brackets around it and a trailing minus."""
    before = text[max(0, start - 6):start]
    m = _MINUS_BEFORE.search(before)
    if m and not _YEAR.fullmatch(text[start:end]):
        lead = m.group("lead")
        if lead is None or not lead.isspace() or not text[:start - len(before) + m.start("lead")].rstrip()[-1:].isdigit():
            return True
    if not source:
        return False
    return bool(_BRACKET_BEFORE.search(before) and _BRACKET_AFTER.match(text, end)) or bool(_TRAILING_MINUS.match(text, end))


def _span_end(read: str, text: str, end: int) -> int:
    """``end`` moved past the blanks ``read_separators`` wrote where ``text`` had the dropped thousands points of a European number
    ("1.234,56" is read "1234.56 "): the room belongs to the number it was read from, not to a blank the text itself wrote."""
    while end < len(read) and read[end] == " " and text[end] != " ":
        end += 1
    return end


@lru_cache(maxsize=1024)
def number_spans(text: str, *, source: bool = False, decimal: bool = False) -> tuple[tuple[int, int, frozenset[str]], ...]:
    """Every number of ``text`` as (start, end, forms), in reading order; the positions are those of ``text`` itself (the reading keeps
    its length). The forms are the normalized forms (``add_form``) with the signed forms of a negative number. A ``source`` keeps both
    readings of an ambiguous pair ("3,4": 3.4, 3 and 4, one number spanning the pair). A function of its arguments alone, kept (the same
    sentence is read by every check); a tuple, so that a caller cannot change what the next one reads."""
    read = read_separators(text, decimal=decimal)
    found = {m.start(): m for m in NUMBER.finditer(read)}
    pair_at = {m.start(): m for m in _SINGLE.finditer(read) if _classify(read, m, cell=False) == "pair"} if source else {}
    out: list[tuple[int, int, frozenset[str]]] = []
    skip = -1
    for m in found.values():
        if m.start() == skip:
            continue
        start, end, forms = m.start(), m.end(), set()
        add_form(forms, m.group(0))
        pair = pair_at.get(start)
        if pair is not None and pair.end(1) == end:
            tail = found.get(pair.start(2))
            if tail is not None:
                skip, end = tail.start(), tail.end()
                add_form(forms, tail.group(0))
                add_form(forms, f"{pair.group(1)}.{pair.group(2)}")
        end = _span_end(read, text, end)
        if _negative(read, start, end, source=source):
            for f in list(forms):
                forms.add("-" + f)
        out.append((start, end, frozenset(forms)))
    return tuple(out)


def number_forms(text: str, *, source: bool = False, decimal: bool = False) -> set[str]:
    """The forms of every number of ``text`` (``number_spans``)."""
    out: set[str] = set()
    for _, _, forms in number_spans(text, source=source, decimal=decimal):
        out |= forms
    return out


def numbers_written(text: str) -> list[tuple[str, int, int, str]]:
    """Every number of ``text`` as (as written in ``text``, start, end, the number as a plain token): the token is what ``Decimal`` reads
    ("13,2" -> "13.2", "1,250" -> "1,250" with its thousands commas, "1.234,56" -> "1234.56"). For a reader that has to find the written
    number again in the text."""
    read = read_separators(text)
    out = []
    for m in NUMBER.finditer(read):
        end = _span_end(read, text, m.end())
        out.append((text[m.start():end], m.start(), end, m.group(0)))
    return out


# --- Hebrew number words (round 22 U10, KTD9, R17) ----------------------------------------------------------------------

_PREFIX_LETTERS = "ובלמכהש"
_UNIT_WORDS = {
    "אחד": 1, "אחת": 1, "שניים": 2, "שתיים": 2, "שני": 2, "שתי": 2, "שתים": 2, "שלוש": 3, "שלושה": 3, "שלושת": 3, "שלש": 3,
    "שלשה": 3, "שלשת": 3, "ארבע": 4, "ארבעה": 4, "ארבעת": 4, "חמש": 5, "חמישה": 5, "חמשה": 5, "חמשת": 5, "חמישת": 5, "שש": 6,
    "שישה": 6, "ששה": 6, "ששת": 6, "שישת": 6, "שבע": 7, "שבעה": 7, "שבעת": 7, "שמונה": 8, "שמונת": 8, "תשע": 9, "תשעה": 9, "תשעת": 9,
}
_TEN_WORDS = {"עשר": 10, "עשרה": 10, "עשרת": 10}
_TENS_WORDS = {"עשרים": 20, "שלושים": 30, "שלשים": 30, "ארבעים": 40, "חמישים": 50, "חמשים": 50, "שישים": 60, "ששים": 60, "שבעים": 70,
               "שמונים": 80, "תשעים": 90}
_HUNDRED_WORDS = {"מאה": 100, "מאתיים": 200, "מאתים": 200}
_THOUSAND_WORDS = {"אלף": 1000, "אלפיים": 2000, "אלפים": 1000, "אלפי": 1000}
# the construct of a unit in the dual: "שני" and "שתי" mean two only in front of a noun, so "השני" (with the article) is "the second"
_NO_ARTICLE = frozenset({"שני", "שתי"})
# a plural of year, not two: it is a number only right before "עשר" ("שנים עשר")
_SHNAYIM = "שנים"
_WORD_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[א-ת]+(?![A-Za-z0-9_])")
_JOIN = re.compile(r"[ \t\u00a0]+|[ \t\u00a0]*[-\u05be][ \t\u00a0]*")
_FOLLOWED_BY_WORD = re.compile(r"[ \t\u00a0]+[א-ת]")
_DIGIT_BEFORE = re.compile(r"[\d,.][ \t\u00a0]*[-\u05be]?[ \t\u00a0]*$")


class NumberWord(NamedTuple):
    """One number written in words: its value, its place in the text (the prefix letters included) and whether the article stands in
    its prefix ("השלושה")."""

    value: int
    start: int
    end: int
    definite: bool = False


class _Tok(NamedTuple):
    base: str
    kind: str  # unit | ten | shnayim | tens | hundred | meot | thousand | thousands
    value: int
    start: int
    end: int
    prefix: str


def _word_kind(base: str, prefix: str) -> tuple[str, int] | None:
    if base in _UNIT_WORDS and not (prefix.endswith("ה") and base in _NO_ARTICLE):
        return "unit", _UNIT_WORDS[base]
    if base in _TEN_WORDS:
        return "ten", 10
    if base == _SHNAYIM:
        return "shnayim", 2
    if base in _TENS_WORDS:
        return "tens", _TENS_WORDS[base]
    if base in _HUNDRED_WORDS and "ה" not in prefix:  # "המאה" is the century, "האלף" the thousand
        return "hundred", _HUNDRED_WORDS[base]
    if base == "מאות":
        return "meot", 100
    if base in _THOUSAND_WORDS and "ה" not in prefix:
        return ("thousand" if base in ("אלף", "אלפיים") else "thousands"), _THOUSAND_WORDS[base]
    return None


def _split(word: str) -> tuple[str, str, str, int] | None:
    """(prefix, base, kind, value) of a word: the base is a number word, the prefix zero to two prefix letters (the shortest first)."""
    for k in (0, 1, 2):
        if len(word) - k < 2 or not all(ch in _PREFIX_LETTERS for ch in word[:k]):
            continue
        found = _word_kind(word[k:], word[:k])
        if found is not None:
            return word[:k], word[k:], found[0], found[1]
    return None


def _tokens(text: str) -> list[_Tok]:
    """The number words of ``text`` in order, as tokens: every Hebrew word that is a number word, whole, with its prefix letters
    apart ("ב-שנים": the hyphened prefix is the prefix of the word after it)."""
    words = [(m.start(), m.end(), m.group(0)) for m in _WORD_TOKEN.finditer(text)]
    out: list[_Tok] = []
    for n, (start, end, word) in enumerate(words):
        found = _split(word)
        if found is None:
            continue
        prefix, base, kind, value = found
        if not prefix and n and (prev := words[n - 1])[1] + 1 == start and text[prev[1]] in "-\u05be" \
                and len(prev[2]) <= 2 and all(ch in _PREFIX_LETTERS for ch in prev[2]):
            start, prefix = prev[0], prev[2]  # "ב-שנים עשר": the hyphened prefix letter
            if prefix.endswith("ה") and base in _NO_ARTICLE:
                continue
        out.append(_Tok(base, kind, value, start, end, prefix))
    # "שנים" alone is the plural of year: it is kept only right before "עשר"/"עשרה"
    return [t for i, t in enumerate(out) if t.kind != "shnayim" or _joins(text, out, i + 1) and out[i + 1].kind == "ten"]


def _joins(text: str, toks: list[_Tok], k: int) -> bool:
    """Whether ``toks[k]`` continues the phrase of ``toks[k - 1]``: only blanks or a hyphen between them, and no prefix letter on it but
    "ו" ("and")."""
    return 0 < k < len(toks) and toks[k].prefix in ("", "ו") and _JOIN.fullmatch(text, toks[k - 1].end, toks[k].start) is not None


def _group(toks: list[_Tok], text: str, i: int) -> tuple[int, int]:
    """(value, next index) of the part below a thousand that starts at ``toks[i]``: hundreds, then tens and units or a teen. (0, i) when
    nothing there is a number."""
    v, j = 0, i
    if j < len(toks) and toks[j].kind == "hundred":
        v, j = toks[j].value, j + 1
    elif j < len(toks) and toks[j].kind == "unit" and 2 <= toks[j].value <= 9 and _joins(text, toks, j + 1) \
            and toks[j + 1].kind == "meot":
        v, j = toks[j].value * 100, j + 2
    if j < len(toks) and (j == i or _joins(text, toks, j)):
        t = toks[j]
        if t.kind == "tens":
            v, j = v + t.value, j + 1
            if _joins(text, toks, j) and toks[j].kind == "unit" and toks[j].value <= 9:
                v, j = v + toks[j].value, j + 1
        elif t.kind in ("unit", "shnayim") and _joins(text, toks, j + 1) and toks[j + 1].kind == "ten":
            v, j = v + 10 + t.value, j + 2
        elif t.kind in ("unit", "ten"):
            v, j = v + t.value, j + 1
    return v, j


def _phrase(toks: list[_Tok], text: str, i: int) -> tuple[int, int]:
    """(value, next index) of the number phrase that starts at ``toks[i]``: the part below a thousand, its thousands and the rest."""
    v, j = _group(toks, text, i)
    if j < len(toks) and toks[j].kind in ("thousand", "thousands") and (j == i or _joins(text, toks, j)):
        t = toks[j]
        if t.value == 2000:
            total = 2000 if not v else 0
        elif t.kind == "thousand":
            total = (v or 1) * 1000
        else:
            total = v * 1000
        if total:
            rest, k = _group(toks, text, j + 1) if _joins(text, toks, j + 1) else (0, j + 1)
            return total + rest, k
    return v, j


@lru_cache(maxsize=1024)
def _read_words(text: str) -> tuple[NumberWord, ...]:
    toks = _tokens(text)
    out: list[NumberWord] = []
    i = 0
    while i < len(toks):
        v, j = _phrase(toks, text, i)
        if j == i:
            i += 1
            continue
        first, last = toks[i], toks[j - 1]
        # a scale word after a digit ("250 אלף", "3 מאות") belongs to that digit
        if first.kind in ("hundred", "thousand", "thousands", "meot") and _DIGIT_BEFORE.search(text, 0, first.start):
            i = j
            continue
        out.append(NumberWord(v, first.start, last.end, "ה" in first.prefix))
        i = j
    return tuple(out)


def number_words(text: str, *, claim: bool = False) -> tuple[NumberWord, ...]:
    """Every number written in Hebrew words in ``text``, in reading order (``NumberWord``). The default reads a source: every phrase.
    ``claim``: the text is an answer's, which writes a count in words and also uses the same words as idioms, so a phrase is a number
    only when it is a count: not "one" ("אחד המסמכים", "בפעם אחת"), not with the article in its prefix ("השלושה"), and it stands right
    before a word (a noun) — "מצד שני" and "הוסכם על שתיים" state no count. (A claim's words only ever confirm a count, round 22 U10.)"""
    found = _read_words(text)
    if not claim:
        return found
    out = []
    for w in found:
        if w.value == 1 or w.definite or not _FOLLOWED_BY_WORD.match(text, w.end):
            continue
        out.append(w)
    return tuple(out)


def word_forms(text: str, *, claim: bool = False) -> set[str]:
    """The numbers ``text`` writes in Hebrew words, in the forms ``numbers_in`` gives the same number written in digits (``add_form``: "12" for
    "שנים עשר"; "20" and "2E+1" for "עשרים", as "20" has both)."""
    out: set[str] = set()
    for w in number_words(text, claim=claim):
        add_form(out, str(w.value))
    return out


def number_word(text: str) -> Decimal | None:
    """The value of the first Hebrew number phrase in ``text`` ("שישה", "עשרים וחמישה", "שתים עשרה"), or None."""
    found = _read_words(text or "")
    return Decimal(found[0].value) if found else None


def whole_number_word(text: str) -> int | None:
    """The value of ``text`` when all of it is one Hebrew number phrase ("שתים עשרה"), else None ("שתים עשרה יחידות")."""
    found = _read_words(text or "")
    if len(found) != 1:
        return None
    rest = (text[:found[0].start] + text[found[0].end:]).strip(" -\u05be\t")
    return found[0].value if not rest else None
