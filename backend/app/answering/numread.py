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
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# A number may follow a Hebrew prefix letter ("ב2019"), but not a Latin letter, digit or underscore. A comma is a thousands
# separator only in groups of three ("1,250,000"): "2018,2019,2021" is a list of three years.
NUMBER = re.compile(r"(?<![0-9A-Za-z_])(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?")

_CURRENCY = "₪$€£"
# what stands right after / before a number that makes its comma a decimal: a percent, a currency or a unit
_UNIT_AFTER = re.compile(r"\s?(?:%|[" + _CURRENCY + r"]|ש[\"״]ח|שקל|מ[\"״][רק]|דונם|אלף|מיליון|מטר|ק[\"״]מ)")
_CURRENCY_BEFORE = re.compile(r"[" + _CURRENCY + r"]\s?$")
# a European number: thousands points, then the decimal comma
_EURO = re.compile(r"(?<![0-9A-Za-z_.,])(\d{1,3}(?:\.\d{3})+),(\d+)(?!\d)(?![.,]\d)")
# a number with one comma that does not end a chain ("6,7,8") nor continue a thousands group ("1,250,5")
_SINGLE = re.compile(r"(?<![0-9A-Za-z_.,])(\d+),(\d+)(?!\d)(?![.,]\d)")
_CELL_BEFORE = re.compile(r"(?:.*\|)?\s*[-\u2212(]?\s*(?:[" + _CURRENCY + r"]\s*)?")
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


def _scan(text: str, cell: bool) -> tuple[list[re.Match], list[re.Match], list[re.Match]]:
    """The European numbers, the certain decimal commas and the pairs of ``text``."""
    euro = list(_EURO.finditer(text))
    decimals, pairs = [], []
    for m in _SINGLE.finditer(text):
        kind = _classify(text, m, cell=cell)
        if kind == "decimal":
            decimals.append(m)
        elif kind == "pair":
            pairs.append(m)
    return euro, decimals, pairs


def decimal_convention(text: str) -> bool:
    """Whether ``text`` shows a certain decimal comma ("1.234,56", "0,125", "13,2%", a cell "13,2"): the text writes its decimals
    with a comma."""
    if "," not in text:
        return False
    euro, decimals, _ = _scan(text, cell=False)
    return bool(euro or decimals)


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
    for m in decimals + (pairs if convention else []):
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
_MINUS_BEFORE = re.compile(r"(?P<lead>^|[\s(\[:=;|])[-\u2212](?:[" + _CURRENCY + r"]\s?)?$"
                           r"|[" + _CURRENCY + r"]\s?[-\u2212]$")
_BRACKET_BEFORE = re.compile(r"\(\s?(?:[" + _CURRENCY + r"]\s?)?$")
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


def number_spans(text: str, *, source: bool = False, decimal: bool = False) -> list[tuple[int, int, frozenset[str]]]:
    """Every number of ``text`` as (start, end, forms), in reading order; the positions are those of ``text`` itself (the reading keeps
    its length). The forms are the normalized forms (``add_form``) with the signed forms of a negative number. A ``source`` keeps both
    readings of an ambiguous pair ("3,4": 3.4, 3 and 4, one number spanning the pair)."""
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
        while end < len(read) and read[end] == " " and text[end] != " ":
            end += 1  # the room of a European number's dropped thousands points belongs to the number it was read from
        if _negative(read, start, end, source=source):
            for f in list(forms):
                forms.add("-" + f)
        out.append((start, end, frozenset(forms)))
    return out


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
        end = m.end()
        while end < len(read) and read[end] == " " and text[end] != " ":
            end += 1
        out.append((text[m.start():end], m.start(), end, m.group(0)))
    return out
