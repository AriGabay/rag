"""Does each number an answer presents keep the meaning its evidence gives it?

For a number in an answer unit that cites sources, the qualifiers its evidence attaches to it are read in the
same way the VAT check reads VAT:

- **area basis** as written (מ״ר אקוו׳, פלדלת, ברוטו, נטו, עיקרי, רשום, מ״ר בנוי);
- **period** (לחודש / לשנה);
- **approximation** ("כ-21,000", "בערך").

A qualifier belongs to the nearest amount (a number written with ₪, מ״ר, דונם or %) before it in its clause, or,
with none before it, to the first amount after it; in a clause without amounts, to the nearest number before it.
In "השווי למ״ר אקוו׳ לנכס ברחוב הצאלון 7 נקבע ל-14,250 ₪" the basis is the 14,250's, not the house number's; in
"63 ₪ למ״ר לחודש" the period is the 63's. In a table row it also comes from the row's label cell and the column's header. A source states a
basis for all its numbers only in a table's title, caption or notes (a sentence with no number elsewhere gives no basis to
other numbers), and an adjective such as ברוטו or נטו names a basis only next to the word for area. A
period it states beyond a number's own clause attests that number only from the number's own line or sentence (a
clause with no number in its line, a period word opening the next line of a wrapped sentence), the line above that
governs it (the nearest line with no number stating a period), or its table's title, caption or notes — never from
another sentence of a neighbouring paragraph. A rate's period word ("בריבית שנתית", "שיעור היוון שנתי") is the
rate's: it qualifies a percent of its clause and never an amount, so a feasibility check's totals do not become
"לשנה" because the financing paragraph after them speaks of an annual interest rate.

Two kinds of problem:

- **missing** — the evidence for the number (a cited measurement with that value, or the source passages that
  state it, the ones closest in wording to the unit) gives it a basis, period or approximation, and the unit says
  none of that kind anywhere. The number keeps a meaning only with it: "השווי למ״ר" without "אקוו׳" reads as
  another value. The unit is still judged; the repair round asks for the qualifier, and when exactly one is
  attested and the unit is otherwise supported, the server finally writes it next to the number, marked as
  taken from the source.
- **unsupported** — the unit gives the number a basis or period its evidence does not give it, or contradicts
  it ("לחודש" where the source says nothing about a period, or says "לשנה"). Nothing is added that the evidence
  lacks; unknown stays unknown.

Round 18 (U1, KTD4, R10): a number is a duration ("36 חודשים": months or years) or a count ("12 דירות") when the words after
it say so; it never owns or claims a period, and no header, caption or line above gives it one. A period word is read
once: a word the text layer split ("חודש י") is joined first, the plural adjective ("חודשיים") is a period after a plural noun
only, and neither "בחודש מאי", "לשנה אחת" nor a negated "אינו חודשי" is a period. Each table of a source has its own
header, aligned to its data rows by their non-empty cells, and an empty header cell continues the cell before it.

Round 19 (U1, KTD3, R9, R11, R12): a letter that follows a quote mark ends an abbreviation ("סה"כ", "מע"מ", "דמ"ש") and is never
joined to the next word, so "סה"כ לשנה" stays an annual header; an abbreviated construct plural ("דמ"ש") is a plural noun before
"חודשיים". A number is read with its sign and its decimal comma (``answering.numread``): "13,2" is 13.2 in a cell or next to a unit,
"3,4" in a sentence keeps both readings in a source, "6,7,8" is a list, "(3.5)%" is negative. A number the source writes as an
identifier (after תא שטח, מגרש, חלקה, גוש, סעיף, עמוד, נספח) carries no row, header or caption qualifier, and a number whose evidence
occurrences are all identifiers is not checked.

Round 9 (U12, KTD10, R22): a number that is no quantity — an identifier after מס׳ / מספר / No. / #, a date's day, month
or year, a bare year — gets no check, and neither does a clause saying the source does not state something ("לא צוין
אם הסכום כולל מע״מ"): it claims no basis, period or VAT.

Only the turn's own evidence is read: cited passages and measurements. No answer field is fixed in advance; the
model still writes the answer.

In a file holding several appraisals (round 7 U6, KTD7, R20), the subject property's stored measurements a check
reads (``Fetcher.subject_measurements``) are, when the context checks are enforced, only those of the appraisal
context the unit's cited evidence is in: another appraisal's, or an appendix's comparison property's, figure never
attests a number of this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache, cached_property, lru_cache
from typing import TYPE_CHECKING

from app.answering.numread import NUMBER as _NUM_AT
from app.answering.numread import number_spans, read_separators
from app.answering.verify import _QUANTITY_AFTER as _QUANTITY_AT
from app.answering.verify import not_an_amount, numbers_in
from app.chat.datum import STATUS_NOT_READ
from app.chat.evidence import _content_words
from app.chat.scopes import row_cells as _row_cells
from app.measurements.extract import (
    _MONTH_WORDS,
    _YEAR_WORDS,
    PERIOD_ADJECTIVE_ENDINGS,
    PERIOD_ADJECTIVES,
    PERIOD_LABELS,
    PERIOD_NOUNS,
)

if TYPE_CHECKING:
    from app.chat.tools import Workspace
    from app.chat.verify import Unit

# (key, pattern) — the key compares spellings; the matched text is the qualifier as written
_BASIS = [
    # אקוו׳, אקווי׳, אקוי׳, אקו׳, אקוו, אקווי, אקוי, אקוויוולנטי(ים), אקוולנטי: one basis however it is spelt
    ("אקוו", r"(?:מ\"ר\s*)?אקו(?:ו?י?ו?ולנטי(?:ים)?|ו?י?'|ו?י|ו)(?![א-ת])"),
    ("פלדלת", r"(?:מ\"ר\s*)?(?<![א-ת])[ולב]?פלדלת"),
    ("ברוטו", r"(?:מ\"ר\s*)?(?<![א-ת])[ו]?ברוטו(?![א-ת])"),
    ("נטו", r"(?:מ\"ר\s*)?(?<![א-ת])[ו]?נטו(?![א-ת])"),
    ("עיקרי", r"(?:מ\"ר\s*|שטח\s*)?(?<![א-ת])עיקרי(?![א-ת])"),
    ("רשום", r"(?:מ\"ר\s*|שטח\s*)(?<![א-ת])רשום(?![א-ת])"),
    ("בנוי", r"מ\"ר\s+בנוי(?![א-ת])"),
]
_MONTH_NAMES = (r"(?:[בל]?(?:ינואר|פברואר|מרץ|מרס|אפריל|מאי|יוני|יולי|אוגוסט|ספטמבר|אוקטובר|נובמבר|דצמבר|תשרי|חשוון|"
                r"חשון|כסלו|טבת|שבט|אדר|ניסן|אייר|סיוון|סיון|תמוז|אב|אלול)(?![א-ת]))")
# "for the month of May" / "for one year" name a date or a duration, not a period
_NOT_A_PERIOD_AFTER = rf"(?!\s+(?:{_MONTH_NAMES}|(?:אחד|אחת|נוסף|נוספת)(?![א-ת])))"


def _period_pattern(key: str, abbreviations: str = "") -> str:
    """The period words of one period, built from the vocabulary the measurement extractor reads (``PERIOD_NOUNS``,
    ``PERIOD_ADJECTIVES``): the noun after a prefix letter or a slash ("לחודש", "/חודש"), the adjective with its endings
    ("חודשי", "חודשית", "החודשיים", and the plural "חודשיים" that ``_period_word_ok`` reads only after a plural noun)."""
    noun, adj = PERIOD_NOUNS[key], PERIOD_ADJECTIVES[key]
    endings = "|".join(PERIOD_ADJECTIVE_ENDINGS)
    return (rf"(?:(?<![א-ת])[לב]|/\s?){noun}(?![א-ת]){_NOT_A_PERIOD_AFTER}"
            rf"|(?<![א-ת])(?:ו?ה?{adj}(?:{endings})?{abbreviations})(?![א-ת])")


_PERIOD = [
    # "לחודש", "חודשי"; not "לפני שנה", "12 חודשים" (a duration), "בחודש מאי" or "לשנה אחת" (a date, a duration)
    ("month", _period_pattern("month", "|לח'|לחו'")),
    ("year", _period_pattern("year")),
]
# "כ-" right before a number; not the כ that ends an abbreviation (סה"כ)
_APPROX_BEFORE = re.compile(r"(?:(?<![א-ת\"'])[בו]?כ[-־–]?\s?|בערך\s|בסביבות\s|כמעט\s)$")
_AMOUNT_AFTER = re.compile(r"\s*(?:₪|ש\"ח|מ\"ר|דונם|%|אלף|מיליון)")
_APPROX_WORDS = re.compile(r"(?<![א-ת])(?:מקורב|בקירוב|בערך|בסביבות)(?![א-ת])")
# a clause ends at a full stop, a semicolon or a line end, but not inside the dotted VAT abbreviation "מ.ע.מ." (round 13)
_CLAUSE_END = re.compile(r"(?<!מ\.ע)[.;\n](?!\d)(?!ע\.?מ(?![א-ת]))")
_ROW = " | "
# a clause saying the source does not state / does not show something ("לא צוין אם הסכום כולל מע״מ", "אינו מופיע"): it
# claims no basis, period or VAT for the numbers it names, so no number-meaning check applies to it (round 9 U12)
ABSENCE_SENTENCE = re.compile(
    r"(?<![א-ת])(?:לא|אינו|אינה|אינם|אינן)\s+(?:צוין|צוינה|צוינו|מצוין|מצוינת|מצוינים|מצוינות|ציין|ציינה|ציינו|נכתב|"
    r"נכתבה|נכתבו|נאמר|נאמרה|פורט|פורטה|פורטו|מפורט|מפורטת|הוזכר|הוזכרה|הוזכרו|מוזכר|מוזכרת|נמסר|נמסרה|ידוע|ידועה|"
    r"ברור|ברורה|נמצא|נמצאה|נמצאו|נמצאים|אותר|אותרה|אותרו|מופיע|מופיעה|מופיעים|מופיעות)(?![א-ת])"
    r"|(?<![א-ת])אין\s+(?:ציון|פירוט|אזכור|התייחסות|מידע|נתון)(?![א-ת])")
# a period word right after a rate's noun ("ריבית שנתית", "שיעור היוון שנתי", "תשואה לשנה") is that rate's: it
# qualifies a percent of its own clause ("בריבית שנתית של 6%") and never an amount, and it states nothing for the
# source's other numbers
_RATE_NOUN = re.compile(r"(?<![א-ת])[הבולמ]?(?:ריבית|תשואה|היוון|הצמדה|אינפלציה|צמיחה|שיעור)\s+$")
_PERCENT_AFTER = re.compile(r"\s?%")
_SENTENCE_END = re.compile(r"[.;:!?]\s*$")
# lines looked at above a number for the line that states its period (the scale note's rule, ``calc.NOTE_WINDOW``)
NOTE_LINES = 6
# a heading's enumeration ("7.", "7.2", "(3)") is not a number of its own (``calc.governing_note``)
_ENUMERATION = re.compile(r"^\s*\(?\d+(?:\.\d+)*[.)]?\s")

KIND_LABELS = {"basis": "בסיס השטח", "period": "התקופה", "approx": "היותו ערך מקורב"}
PERIOD_TEXT = {k: PERIOD_LABELS[k] for k in ("month", "year")}
MEASUREMENT_PERIODS = set(PERIOD_TEXT)


def display(written: str) -> str:
    """A qualifier as the answer shows it, with Hebrew abbreviation marks."""
    return written.replace('"', "״").replace("'", "׳")


def _norm(text: str) -> str:
    """One spelling of the Hebrew abbreviation marks (same length, so positions stay valid)."""
    return (text or "").replace("״", '"').replace("׳", "'").replace("”", '"').replace("’", "'")


PARTS = ("cell", "header", "caption", "note", "row", "sentence", "paragraph")


@dataclass
class Qualifiers:
    """kind -> {key: as written}; ``parts``: kind -> {key: the part of the source that states it} (round 12 U9, KTD9:
    one of ``PARTS``). A qualifier added without a part has none recorded."""

    found: dict[str, dict[str, str]] = field(default_factory=dict)
    parts: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, kind: str, key: str, written: str, part: str = "") -> None:
        self.found.setdefault(kind, {}).setdefault(key, written.strip())
        known = self.parts.get(kind, {}).get(key)
        # the first part recorded stays, except that "paragraph" (what the source states for all its numbers) gives way
        # to the part of the number's own place ("header" for a table's column, not the table's size line echoing it)
        if part and (known is None or (known == "paragraph" and part != "paragraph")):
            self.parts.setdefault(kind, {})[key] = part

    def merge(self, other: Qualifiers, part: str = "") -> Qualifiers:
        """``other``'s qualifiers, each in the part ``other`` recorded for it, else in ``part``."""
        for kind, values in other.found.items():
            for key, written in values.items():
                self.add(kind, key, written, other.parts.get(kind, {}).get(key) or part)
        return self

    def part_of(self, kind: str, key: str) -> str:
        return self.parts.get(kind, {}).get(key, "")

    def keys(self, kind: str) -> set[str]:
        return set(self.found.get(kind, {}))


def _of_rate(text: str, pos: int) -> bool:
    """Whether the period word at ``pos`` follows a rate's noun (``_RATE_NOUN``): the rate's period."""
    return bool(_RATE_NOUN.search(text[max(0, pos - 24):pos]))


# --- words the text layer split, and what a period word is not ------------------------------------------------------------------
#
# A PDF text layer splits a word before or after one letter ("חודש י", "חודשי ם", "ל חודש"). The pieces are one word again, before
# anything is scanned, only when the joined token is a period or time-unit word; the join moves the letter over the space, so
# the text keeps its length and every position stays valid.
# every written form of a month or a year (prefix letters, the noun, the adjective and the plural / dual nouns)
_TIME_FORM = re.compile(r"[והבלמשכ]{0,2}(?:חודש(?:י(?:ת|ים|ות)?|ים)?|שנ(?:ה|ים|ות|תי(?:ת|ים|ות)?))")
_SPLIT_TAIL = re.compile(r"(?<![א-ת])([א-ת]{2,}) ([א-ת])(?![א-ת])")
# a letter that follows a quote mark ends an abbreviation (סה"כ, מע"מ, בע"מ, דמ"ש), never a word split by the text layer: it is
# not joined to the next word ("סה"כ לשנה" must not become "סה"כל שנה")
_SPLIT_HEAD = re.compile(r"(?<![א-ת\"'״׳])([א-ת]) ([א-ת]{2,})(?![א-ת])")


def _join_time_form(m: re.Match) -> str:
    """The two pieces of a split word as one word when the joined token is a period or time-unit word (the space moves behind it)."""
    return f"{m.group(1)}{m.group(2)} " if _TIME_FORM.fullmatch(m.group(1) + m.group(2)) else m.group(0)


def _rejoin(text: str) -> str:
    return _SPLIT_HEAD.sub(_join_time_form, _SPLIT_TAIL.sub(_join_time_form, text))


def _read(text: str) -> str:
    """The text as the number-meaning checks read it: one spelling of the Hebrew marks (``_norm``), the split period words
    joined (``_rejoin``) and the decimal commas written as points (``numread.read_separators``: a comma is read by context and by the
    text's convention); the same length as ``text``."""
    return read_separators(_rejoin(_norm(text)))


# a negation that governs the period word: one of the ``NEGATION_TOKENS`` words before it, in the same part of the clause ("אינו מוצג
# כסכום חודשי", "לא נקבע לשנה"). A negation further back negates something else ("לא הושכר בפועל ולכן הוערך לפי 5,000 ₪ לחודש"). Not
# a comparison ("לא פחות מ-5,000 ₪ לחודש") nor "לא כולל מע״מ"; a comma, "אלא" and the end of the clause close the negation.
NEGATION_TOKENS = 3
_NEGATION = re.compile(r"(?<![א-ת])ו?(?:לא|אין|אינו|אינה|אינם|אינן)(?![א-ת])"
                       r"(?!\s+(?:פחות|יותר|כולל|כוללת|כוללים|נמוך|גבוה|עולה|עולים|מעל|מתחת|יעלה|יפחת|רק)(?![א-ת]))")
_NEGATION_END = re.compile(r"(?<!\d),|,(?!\d)|[.;\n]|(?<![א-ת])אלא(?![א-ת])")
_PLURAL_ENDING = re.compile(r"(?:ים|ות|מי)$")
# a preposition of a duration right before the word ("תקופה של חודשיים", "למשך שנתיים")
_DURATION_PREPOSITION = re.compile(r"(?<![א-ת])(?:של|למשך|במשך|תוך|עד|כ|מעל|בערך|כמעט)\s+$")
_NUMBER_BEFORE = re.compile(r"(\d[\d,.]*)\s*[-־]?\s*$")
_NOT_NUMBER_TAIL = re.compile(r"[^\d,.\s\-־]")  # a character a match of ``_NUMBER_BEFORE`` cannot hold
DURATION_PREPOSITION_CHARS = 4  # the longest preposition of ``_DURATION_PREPOSITION``


# The checks below read what stands BEFORE a word. They look at a window ending at the word and widen it (fourfold) only when the window
# cannot decide the answer, so the result is the one a read of the whole text before the word gives, without copying and scanning it.
# (``endpos`` cuts the text as a slice would, and a look-behind still sees the characters before the window.)
def _negated(text: str, pos: int) -> bool:
    """Whether a negation governs the word at ``pos``: it stands among the last ``NEGATION_TOKENS`` blank-separated words before ``pos`` and
    after the last mark that closes a part (``_NEGATION_END``)."""
    width = 64
    while True:
        lo = max(0, pos - width)
        words = list(re.finditer(r"\S+", text[lo:pos]))
        if lo == 0 or len(words) > NEGATION_TOKENS:  # the first word of a window that does not start the text may be cut: the others are whole
            break
        width *= 4
    words = words[-NEGATION_TOKENS:]
    if not words:
        return False
    start = lo + words[0].start()
    closings = list(_NEGATION_END.finditer(text, start, pos))
    return bool(_NEGATION.search(text, closings[-1].end() if closings else start, pos))


def _number_before(text: str, pos: int) -> str:
    """The number the text before ``pos`` ends with (``_NUMBER_BEFORE``) as written; an empty string when it ends with none. A match is made of
    digits, separators and blanks only, so a window that holds any other character holds the whole of every match; one that starts at
    the window's edge is read again in a wider window, so the number is returned whole (``_can_count_time`` reads its size)."""
    width = 32
    while True:
        lo = max(0, pos - width)
        m = _NUMBER_BEFORE.search(text, lo, pos)
        if m and (lo == 0 or m.start(1) > lo):  # a match that starts at the window's edge may go on before it: widen
            return m.group(1)
        if not m and (lo == 0 or _NOT_NUMBER_TAIL.search(text, lo, pos)):
            return ""
        width *= 4


# a count of months or years is small: a number of a thousand or more before "חודשי" / "חודשים" is an amount ("דמי שכירות 5,000 חודשי")
DURATION_MAX = Decimal(1000)


def _can_count_time(written: str) -> bool:
    """Whether the number as written can be a count of months, years, weeks or days: below ``DURATION_MAX``."""
    try:
        return Decimal(written.rstrip(",.").replace(",", "")) < DURATION_MAX
    except InvalidOperation:
        return True  # no number read: the words after it decide, as before


def _after_duration_preposition(text: str, pos: int) -> bool:
    """Whether the text before ``pos`` ends with a preposition of a duration and the blanks after it (``_DURATION_PREPOSITION``). A match is
    a word of at most ``DURATION_PREPOSITION_CHARS`` characters and blanks, so a window whose text up to its last blank holds that many
    characters holds the whole of every match."""
    width = 32
    while True:
        lo = max(0, pos - width)
        if _DURATION_PREPOSITION.search(text, lo, pos):
            return True
        if lo == 0 or len(text[lo:pos].rstrip()) >= DURATION_PREPOSITION_CHARS:
            return False
        width *= 4


def _words_before(text: str, pos: int, count: int) -> list[str]:
    """The last ``count`` blank-separated words of the text before ``pos``."""
    width = 16 * count
    while True:
        lo = max(0, pos - width)
        words = text[lo:pos].split()
        if lo == 0 or len(words) > count:  # the first word of a window that does not start the text may be cut: the others are whole
            return words[-count:]
        width *= 4


def _words_after(text: str, pos: int, count: int) -> list[str]:
    """The first ``count`` blank-separated words of the text from ``pos``."""
    width = 16 * count
    while True:
        hi = min(len(text), pos + width)
        words = text[pos:hi].split()
        if hi == len(text) or len(words) > count:  # the last word of a window that does not end the text may be cut: the others are whole
            return words[:count]
        width *= 4


# the abbreviated construct plural of a rent or a fee ("דמ"ש", "דמ"נ": דמי שכירות, דמי ניהול), with its prefix letters
_ABBREVIATED_PLURAL = re.compile(r"^[והבלמשכ]{0,2}דמ\"[א-ת]$")


def _after_plural_noun(text: str, pos: int) -> bool:
    words = re.findall(r"[א-ת\"']+", text[max(0, pos - 40):pos])[-2:]
    return any(_PLURAL_ENDING.search(w) or _ABBREVIATED_PLURAL.match(w) for w in words)


def _period_word_ok(text: str, m: re.Match) -> bool:
    """Whether the period word ``m`` states a period: not in a negated part of its clause, and — as an adjective — not right
    after a number ("36 חודשי שכירות", "2 חודשיים": a duration), the unprefixed plural ("חודשיים", "שנתיות") only after a
    plural noun ("דמי שכירות חודשיים") and not after a preposition of a duration ("של חודשיים")."""
    if _negated(text, m.start()):
        return False
    word = m.group(0).lstrip("ו")
    if word[:1] in ("ל", "ב", "/"):  # a noun after its prefix or a slash, or an abbreviation
        return True
    if (written := _number_before(text, m.start())) and _can_count_time(written):
        return False
    if word[:1] != "ה" and word.endswith(("ים", "ות")):
        return not _after_duration_preposition(text, m.start()) and _after_plural_noun(text, m.start())
    return True


# an area adjective names a basis only next to the word for area ("מ״ר ברוטו", "שטח עיקרי", "ברוטו למ״ר", "השטחים בטבלה הם
# ברוטו"): "הסכום ברוטו" and "הרכיב העיקרי" are no area basis
_NEEDS_AREA_WORD = {"ברוטו", "נטו", "עיקרי"}
_AREA_WORD_NEAR = re.compile(r"(?<![א-ת])[הלב]?(?:מ\"ר|שטח|שטחים)(?![א-ת])")
AREA_WORD_WINDOW = (3, 2)  # words before and after the adjective in which the area word stands


def _basis_ok(text: str, key: str, m: re.Match) -> bool:
    if key not in _NEEDS_AREA_WORD or m.group(0).startswith(("מ\"ר", "שטח")):
        return True
    before = _words_before(text, m.start(), AREA_WORD_WINDOW[0])
    after = _words_after(text, m.end(), AREA_WORD_WINDOW[1])
    return any(_AREA_WORD_NEAR.search(w) for w in before + after)


def _scan(text: str, rates: bool = True) -> list[tuple[int, str, str, str]]:
    """Every qualifier in the text, in reading order: (position, kind, key, as written). ``rates=False``: without the
    period words of a rate (``_of_rate``) — what a text states for numbers other than that rate."""
    text = _rejoin(text)
    out = []
    for kind, table in (("basis", _BASIS), ("period", _PERIOD)):
        for key, pattern in table:
            for m in re.finditer(pattern, text):
                if kind == "basis" and not _basis_ok(text, key, m):
                    continue
                if kind == "period" and not (_period_word_ok(text, m) and (rates or not _of_rate(text, m.start()))):
                    continue
                out.append((m.start(), kind, key, m.group(0)))
    return sorted(out)


@lru_cache(maxsize=512)
def _spans(text: str) -> tuple[tuple[int, int, frozenset[str]], ...]:
    return tuple(number_spans(text, source=True))


@lru_cache(maxsize=512)
def _span_ends(text: str) -> dict[int, int]:
    """Where each number of ``text`` ends, by the position it starts at (the first one, were two to start at one position): the lookup of
    ``written_at``, kept next to the spans it is built from. Private: a caller never changes it."""
    ends: dict[int, int] = {}
    for start, end, _ in _spans(text):
        ends.setdefault(start, end)
    return ends


def _numbers(text: str) -> list[tuple[int, int, frozenset[str]]]:
    """Every number of the text as (start, end, forms): the forms keep the sign, and an ambiguous pair ("3,4") is one number with
    both readings (``numread.number_spans``; the text is evidence or answer, and both read alike here)."""
    return list(_spans(text))


def written_at(text: str, pos: int) -> str:
    """The number written at ``pos`` as the text shows it (its own digits, separators and all, so a decimal comma stays a comma),
    without the comma that ends a list item ("בשנים 2021, 2022"), so a year is tested as a year and an identifier as an identifier.
    An ambiguous pair ("3,4") is written whole."""
    end = _span_ends(text).get(pos)
    if end is not None:
        return text[pos:end].rstrip(", ")
    return _NUM_AT.match(text, pos).group(0).rstrip(",")


# What the words right after a number make of it: a number of months or years (a duration) and a number of things (a count)
# are no amount of money, so they never own a period of their clause, never claim one, and never receive one from a header,
# a caption or a line above (round 18 U1, KTD4, R10).
_DURATION_AFTER = re.compile(
    r"[\s\-־]*(?:חודש(?:ים|יים|י)?|שנה|שנים|שנתיים|שנות|שבוע|שבועות|שבועיים|יום|ימים|ימי)(?![א-ת])")
_COUNT_AFTER = re.compile(
    r"[\s\-־]*(?:יחידות|יח\"ד|דירות|חדרים|קומות|חניות|נכסים|עסקאות|מבנים|בניינים|דיירים|חוזים|מקומות|מחסנים|חנויות|משרדים)"
    r"(?![א-ת])")


def _number_ending(text: str, end: int) -> str:
    """The number that ends at ``end`` as written ("" when none does)."""
    found = list(_NUM_AT.finditer(text, max(0, end - 32), end))
    return found[-1].group(0) if found and found[-1].end() == end else ""


def after_kind(text: str, end: int) -> str:
    """"duration" or "count" when the words right after the number that ends at ``end`` make it one; else an empty string."""
    if _DURATION_AFTER.match(text, end) and _can_count_time(_number_ending(text, end)):
        return "duration"
    return "count" if _COUNT_AFTER.match(text, end) else ""


def after_noun_end(text: str, end: int) -> int | None:
    """Where the noun of a count or a duration ends, when the words right after the number that ends at ``end`` make it one ("59 דירות",
    "36 חודשים"; ``after_kind``); None for any other number."""
    if not after_kind(text, end):
        return None
    noun = _COUNT_AFTER.match(text, end) or _DURATION_AFTER.match(text, end)
    return noun.end() if noun else None


def _owner(nums: list, amounts: list[int], pos: int, clause: str) -> int | None:
    """The number a qualifier at ``pos`` belongs to: the nearest amount before it, else the first amount after
    it — unless a comma separates it from the amount before and not from the one after ("בשטח 120 מ"ר, דמי
    השכירות לחודש 7,560 ₪": the period is the 7,560's); in a clause without amounts, the nearest number before
    it, else the first. A duration or a count (``after_kind``) owns nothing: None when no other number is there."""
    eligible = [i for i, (_, b, _) in enumerate(nums) if not after_kind(clause, b)]
    if not eligible:
        return None
    amounts = [i for i in amounts if i in eligible]
    if amounts:
        before = [i for i in amounts if nums[i][0] < pos]
        after = [i for i in amounts if nums[i][0] > pos]
        if before and after and ", " in clause[nums[before[-1]][1]:pos] and ", " not in clause[pos:nums[after[0]][0]]:
            return after[0]
        return before[-1] if before else (after[0] if after else amounts[-1])
    before = [i for i in eligible if nums[i][0] < pos]
    return before[-1] if before else eligible[0]


def _noun_period(clause: str, owner: tuple, after: int) -> bool:
    """Whether a period word that ends at ``after`` is a noun's ("הרווח השנתי הממוצע"), not the percent's that the owner is (round 20 U9, F4): the
    owner is a percent that stands after the word, with a Hebrew word between them ("בשיעור 7.15%"). An adjacent rate ("שנתי 7.15%") and
    a rate with its noun before the word (``_of_rate``) are the percent's."""
    a, b, _ = owner
    if a < after or not (_PERCENT_AFTER.match(clause, b) or clause[max(0, a - 1):a] == "%"):
        return False
    return any(w != "של" for w in re.findall(r"[א-ת]{2,}", clause[after:a]))


def attached(text: str, noun_period: bool = True) -> tuple[list[tuple[frozenset[str], int, Qualifiers]], Qualifiers]:
    """For each number in the text, its forms, position and the qualifiers attached to it; and the qualifiers of
    clauses with no number (what the text states beyond its own numbers). A rate's period word ("בריבית שנתית")
    belongs to a percent of its clause — the first after it, else the last before it — and with none, to nothing. ``noun_period``
    (the answer's side; a source is read without it, round 20 U9, F4): a period word of a noun that stands before a percent with a
    Hebrew word between them is the clause's, not the percent's."""
    text = _read(text)
    out: list[tuple[frozenset[str], int, Qualifiers]] = []
    general = Qualifiers()
    start = 0
    for end_m in [*_CLAUSE_END.finditer(text), None]:
        end = end_m.end() if end_m else len(text)
        clause = text[start:end]
        nums = _numbers(clause)
        quals = [Qualifiers() for _ in nums]
        amounts = [i for i, (_, b, _) in enumerate(nums) if _AMOUNT_AFTER.match(clause, b)]
        for pos, kind, key, written in _scan(clause):
            if kind == "period" and _of_rate(clause, pos):
                percents = [i for i, (a, b, _) in enumerate(nums)
                            if _PERCENT_AFTER.match(clause, b) or clause[max(0, a - 1):a] == "%"]
                after = [i for i in percents if nums[i][0] > pos]
                if percents:
                    quals[after[0] if after else percents[-1]].add(kind, key, written)
                continue
            owner = _owner(nums, amounts, pos, clause) if nums else None
            if owner is None or (noun_period and kind == "period" and _noun_period(clause, nums[owner], pos + len(written))):
                general.add(kind, key, written)
                continue
            quals[owner].add(kind, key, written)
        for i, (a, _, forms) in enumerate(nums):
            if _APPROX_BEFORE.search(clause[max(0, a - 12):a]):
                quals[i].add("approx", "approx", "מקורב")
            out.append((forms, start + a, quals[i]))
        start = end
    return out, general


def _all(text: str, rates: bool = True) -> Qualifiers:
    q = Qualifiers()
    for _, kind, key, written in _scan(_read(text), rates):
        q.add(kind, key, written)
    return q


def _only(q: Qualifiers, kind: str, keep: bool = True) -> Qualifiers:
    """The qualifiers of one kind (``keep``), or all but that kind."""
    return Qualifiers({k: dict(v) for k, v in q.found.items() if (k == kind) == keep},
                      {k: dict(v) for k, v in q.parts.items() if (k == kind) == keep})


def absence_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the clauses of ``text`` (split as ``attached`` splits them) that state something is not stated."""
    out, start = [], 0
    for end_m in [*_CLAUSE_END.finditer(text), None]:
        end = end_m.end() if end_m else len(text)
        if ABSENCE_SENTENCE.search(text[start:end]):
            out.append((start, end))
        start = end
    return out


@dataclass
class Occurrence:
    """One place the evidence states the number, with what it attaches to it there."""

    qualifiers: Qualifiers
    context: str  # the words around it, to choose the occurrence closest to the unit
    measurement: bool = False
    area: bool = False  # its row or column names it a per-area amount, or its row label or cell an area
    identifier: bool = False  # the source writes it as an identifier (a plot, a unit, a section): it carries no qualifiers

    @cached_property
    def context_words(self) -> set[str]:
        return _content_words(self.context)


# a per-area amount: its row or column says "למ״ר", "/מ״ר", "למטר", "לדונם"
_PER_AREA = re.compile(r"(?:(?<![א-ת])ל|/\s?)(?:מ\"ר|מטר|דונם)(?![א-ת])")


_SEGMENT_END = re.compile(r"[.;\n](?!\d)|,\s")  # a clause or a comma-separated part of one ("41,260,500" stays whole)
_AREA_AFTER = re.compile(r"\s*(?:מ\"ר|מטר|דונם)(?![א-ת])")


_AREA_WORD = re.compile(r"(?<![א-ת])[לב]?(?:מ\"ר|מטר|דונם)(?![א-ת])")


def _area_amount(text: str, pos: int, written: str, closest: list[Occurrence]) -> bool:
    """Whether the number at ``pos`` is an area or a per-area amount, the only numbers an area basis qualifies:
    the answer presents it so ("145 מ״ר", or its clause says "למ״ר"), or its own line in the source does ("שווי
    למ״ר בנוי | 8,700"). A total in ₪ or a factor in the rows of a calculation table whose area column is "מ״ר
    אקוו׳" is neither, though the column's header binds the basis to it."""
    if _AREA_AFTER.match(text, pos + len(written)):
        return True
    # its own part of the sentence: a list of figures ("2,870 ₪ למ״ר, שווי כזמין של 41,260,500 ₪, מקדם 0.8712")
    # gives "למ״ר" to the figure it follows, not to the others
    starts = [m.end() for m in _SEGMENT_END.finditer(text, 0, pos)]
    end = _SEGMENT_END.search(text, pos)
    if _PER_AREA.search(text[starts[-1] if starts else 0:end.start() if end else len(text)]):
        return True
    return any(o.area or _AREA_WORD.search(o.context) for o in closest)


TABLE_AROUND = 4  # lines around a table that are its own (caption, title, size, notes); more are a passage's


def _table_definitions(lines: list[str], is_row: list[bool], heads: list[str]) -> Qualifiers:
    """What a calculation table states once for its per-area amounts: the area basis of its area row ("סה״כ מ״ר
    אקווי׳ | 2,480", the part "row") or of its title, caption or notes, and the period of its title, caption or notes
    ("דמי השכירות בטבלה הם לחודש"; the part "caption" above the rows, "note" below them). Text around a table in a
    longer passage is not the table's."""
    found = Qualifiers()
    for line, row in zip(lines, is_row, strict=True):
        if not row:
            continue
        cells = _row_cells(line)
        label = cells[0] if cells else ""
        if 'מ"ר' in label or any('מ"ר' in h for h in heads[:1]):
            for _, kind, key, written in _scan(label):
                if kind == "basis" and not _PER_AREA.search(label):
                    found.add("basis", key, written, "row")
    # a caption, a title, a size line and notes; a longer passage is not the table's
    if sum(1 for row in is_row if not row) <= TABLE_AROUND:
        seen_row = False
        for line, row in zip(lines, is_row, strict=True):
            seen_row = seen_row or row
            if not row:
                for _, kind, key, written in _scan(line, rates=False):
                    found.add(kind, key, written, "note" if seen_row else "caption")
    return found


def _tagged(q: Qualifiers, part: str) -> Qualifiers:
    """``q`` with ``part`` recorded for each qualifier that has none."""
    return Qualifiers().merge(q, part)


def _period_lines(lines: list[str], is_row: list[bool]):
    """The period a source states for each number's line beyond the number's own clause, by line index: a function
    ``(i) -> Qualifiers``. A period word attests a number of its own line, sentence or governing line only, never one
    of another sentence of a neighbouring paragraph, and a rate's period word (``_of_rate``) never attests one:

    - a clause with no number in the number's own line;
    - a period word opening the next line when the number's line does not end its sentence ("נקבעו ל-310,000 ₪" /
      "לשנה, בהתאם להסכם": a sentence wrapped across lines);
    - the nearest line above, within ``NOTE_LINES``, that has no number and states a period ("ההכנסות להלן הן
      לשנה") — never across a table's rows, and never a wrapped sentence's end, which is the number's above it;
    - for a table's row: its title, caption and notes — every line around the table when they are few
      (``TABLE_AROUND``), else the line above it by the rule above and the lines with no number right after it."""
    has_num = [bool(_numbers(_ENUMERATION.sub("", ln))) for ln in lines]
    stated = [Qualifiers() if row else _only(attached(ln, noun_period=False)[1], "period") for ln, row in zip(lines, is_row, strict=True)]

    def opening(j: int) -> Qualifiers:  # a period word that opens line j, continuing the sentence of the line above
        if j <= 0 or j >= len(lines) or is_row[j] or is_row[j - 1] or not has_num[j - 1] \
                or _SENTENCE_END.search(lines[j - 1]):
            return Qualifiers()
        line = lines[j]
        found = _scan(line.lstrip(), rates=False)
        q = Qualifiers()
        if found and found[0][0] == 0 and found[0][1] == "period":
            q.add("period", found[0][2], found[0][3])
        return q

    def above(i: int) -> list[tuple[str, Qualifiers]]:
        for k in range(i - 1, max(-1, i - 1 - NOTE_LINES), -1):
            if is_row[k] or _ENUMERATION.match(lines[k]):
                break
            if has_num[k] or opening(k).found:
                continue
            if stated[k].found:
                return [(lines[k], stated[k])]
        return []

    around = [i for i, row in enumerate(is_row) if not row]
    few = len(around) <= TABLE_AROUND
    first_row = next((i for i, row in enumerate(is_row) if row), len(lines))
    table = []
    if any(is_row) and few:
        for i in around:
            table.append((lines[i], _tagged(_only(_all(lines[i], rates=False), "period"),
                                            "note" if i > first_row else "caption")))

    def scoped(notes, context: str) -> Qualifiers:
        q = Qualifiers()
        money_kinds = {"value", "price", "rent", "management_fee", "cost", "income", "profit", "levy"}
        recurring = {"rent", "management_fee", "income"}
        row_kinds = {k for k in money_kinds if kind_attested(k, context)}
        for note, quals in notes:
            kinds = {k for k in money_kinds if kind_attested(k, note)}
            if not kinds or not row_kinds or kinds & row_kinds or kinds & recurring and row_kinds & recurring:
                q.merge(quals)
        return q

    @cache
    def period(i: int, context: str = "") -> Qualifiers:
        if not is_row[i]:
            # the line above governs only a number of a compatible kind: a note on management fees "לחודש" gives
            # no month to a market value in the next sentence
            return (Qualifiers().merge(stated[i], "sentence").merge(opening(i + 1), "sentence")
                    .merge(scoped(above(i), context or lines[i]), "paragraph"))
        if few:
            return scoped(table, context)
        first, last = i, i
        while first > 0 and is_row[first - 1]:
            first -= 1
        while last + 1 < len(lines) and is_row[last + 1]:
            last += 1
        notes = []
        for k in range(first - 1, max(-1, first - 1 - NOTE_LINES), -1):
            if is_row[k] or _ENUMERATION.match(lines[k]):
                break
            if not has_num[k] and stated[k].found and not opening(k).found:
                notes.append((lines[k], _tagged(stated[k], "caption")))
                break
        for j in range(last + 1, min(len(lines), last + 3)):
            if is_row[j] or has_num[j]:
                break
            notes.append((lines[j], _tagged(stated[j], "note")))
        return scoped(notes, context)

    return period


_SEPARATOR_ROW = re.compile(r"[\s|:\-]+")
# a time word naming how long: the label of a column or row of durations ("תקופת השכירות (חודשים)")
_DURATION_LABEL = re.compile(r"(?<![א-ת])(?:חודשים|שנים|שנות|שבועות|ימים|תקופת|משך)(?![א-ת])")


def context_kind(context: str) -> str:
    """The kind of quantity the words around a number (its row label, its cell and its column header) make it:

    - "rate": an amount that can be a periodic rate — money (₪, ₪ למ״ר), or a quantity with no unit those words rule out;
    - "duration": a number of months or years (``_DURATION_LABEL``) and "count": a number of things (``units_attested``);
    - "other": a percent, an area, a ratio or a coefficient."""
    units = units_attested(context)
    if units & {"ILS", "ILS_per_sqm"}:
        return "rate"
    if "units" in units:
        return "count"
    if units:
        return "other"
    c = _LABEL_NUMBER.sub(" ", _norm(context))
    if _DURATION_LABEL.search(c):
        return "duration"
    return "other" if nonmonetary(c) else "rate"


def _gated(q: Qualifiers, kind: str, owned: bool) -> Qualifiers:
    """What a row label or a column header gives a cell of ``kind``: a period only to a cell that can be a rate, or — for the
    row's own label and the column's own header cell — to a quantity other than a duration or a count. A period stated above
    or across columns (an upper or spanned header cell) reaches amounts only."""
    if kind == "rate" or (owned and kind == "other"):
        return q
    return _only(q, "period", keep=False)


@dataclass(frozen=True)
class _Table:
    """A run of table rows: its header rows (the leading rows with no digit) as cells, and the line indexes they stand on."""

    first: int
    last: int
    head_lines: frozenset[int]
    head_rows: tuple[tuple[str, ...], ...]


def _tables(lines: list[str], is_row: list[bool]) -> list[_Table]:
    """Each table of the source (a run of row lines) with its own header: the rows from its start up to its first row that
    holds a digit. A table with no such row after them has none."""
    out: list[_Table] = []
    i = 0
    while i < len(lines):
        if not is_row[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(lines) and is_row[j + 1]:
            j += 1
        k = i
        while k <= j and not re.search(r"\d", lines[k]):
            k += 1
        head = [n for n in range(i, k) if not _SEPARATOR_ROW.fullmatch(lines[n])] if k <= j else []
        out.append(_Table(i, j, frozenset(head), tuple(tuple(_row_cells(lines[n])) for n in head)))
        i = j + 1
    return out


def _column_heads(table: _Table, cells: list[str]) -> tuple[list[str], list[str]]:
    """For each cell of a data row, the table's header text of its column: (the column's own header cell, the header text
    above it or spanned over it). A header row as wide as the data row is read cell by cell, and an empty header cell continues
    the cell before it (a spanned header). A header row of another width is aligned to the data row by its non-empty cells,
    from the right: a header with one cell fewer names the columns after the row label."""
    n = len(cells)
    own = [""] * n
    above: list[list[str]] = [[] for _ in range(n)]
    for r, row in enumerate(table.head_rows):
        placed: list[tuple[str, bool]] = [("", False)] * n  # (header text, continued from the cell before it)
        if len(row) == n:
            carried = ""
            for i, c in enumerate(row):
                carried = c or carried
                placed[i] = (carried, not c)
        elif len(row) > n:
            placed = [(c, False) for c in row[:n]]
        else:
            slots = [i for i, c in enumerate(cells) if c]
            for text, i in zip(reversed([c for c in row if c]), reversed(slots), strict=False):
                placed[i] = (text, False)
        for i, (text, continued) in enumerate(placed):
            if text and r == len(table.head_rows) - 1 and not continued:
                own[i] = text
            elif text:
                above[i].append(text)
    return own, [" ".join(a) for a in above]


def _caption_lines(lines: list[str], is_row: list[bool], tables: list[_Table]) -> set[int]:
    """The lines that are a table's own caption, title or notes: every line around the tables when they are few
    (``TABLE_AROUND``), else the two lines right before a table and the two right after it."""
    around = [i for i, row in enumerate(is_row) if not row]
    if not tables:
        return set()
    if len(around) <= TABLE_AROUND:
        return set(around)
    near: set[int] = set()
    for t in tables:
        near |= {i for i in range(t.first - 2, t.first) if 0 <= i < len(lines) and not is_row[i]}
        near |= {i for i in range(t.last + 1, t.last + 3) if i < len(lines) and not is_row[i]}
    return near


_NOTE_WORD = re.compile(r"\s+([א-ת][א-ת\"״׳']*)")
# a quantity, currency or period word right after the opening number makes it a quantity, not a footnote mark ("10 אחוזים", "5 אלפי ₪")
_NOTE_QUANTITY = re.compile(
    r"(?:אחוז(?:ים)?|אל(?:ף|פי)|מיליון|מיליארד|דונ(?:ם|מים)|ש[\"״]?ח|שקל(?:ים)?|דולר(?:ים)?|יורו|נקוד(?:ות|ה)|ליחידה|שנים|שנה|חודשים|חודש|"
    r"יחידות|יחידה|מ[\"״]ר)")


def note_mark_at(text: str, at: int, end: int) -> bool:
    """Whether the number at ``text[at:end]`` is the mark of a footnote and no quantity (round 20 U9, F3): one or two digits that open the
    line and are followed by a Hebrew word that is no unit, no noun of a count or a duration and no currency or area word ("1 שטח ברוטו
    (עילי + תחתי)"), or one or two digits after a word and before " :" ("שטח בנוי 1 : כ-12,480 מ״ר"). The source writes it as a mark, so
    it carries no period or basis word of the line around it."""
    if not 1 <= end - at <= 2 or not text[at:end].isdigit():
        return False
    if not text[:at].strip():
        word = _NOTE_WORD.match(text, end)
        return (bool(word) and not after_kind(text, end) and not _QUANTITY_AT.match(text, end) and not _NOTE_QUANTITY.fullmatch(word.group(1))
                and not units_attested(word.group(1)) and not _all(word.group(1)).keys("period"))
    return text.startswith(" :", end) and bool(re.search(r"[א-ת]\s+$", text[:at]))


def _specific_number(forms: frozenset[str], written: str) -> bool:
    """Whether a number is specific enough to be the same figure in another table of an appraisal (round 20 U9, F6): five digits or more
    in the whole part, or written with a thousands separator. A short number ("12", "3.5") recurs everywhere with other meanings."""
    return bool(re.search(r"\d,\d{3}(?!\d)", written)) or any(len(re.sub(r"\D", "", f.split(".")[0])) >= 5 for f in forms)


@lru_cache(maxsize=256)
def parse_source(text: str) -> tuple[tuple[tuple[frozenset[str], Occurrence], ...], Qualifiers]:
    """Every number a source states with what it attaches to it there, and what the source states for all its
    numbers. Parsed once per source text: the same sources are read for every number, unit and round.

    In a table, a number also gets its row's label and its column's header (each table of the source has its own header: its
    leading rows with no digit, aligned to the data rows by their non-empty cells); a per-area amount ("דמ״ש למ״ר |
    75") gets the area basis and the period the table states once for the whole calculation — in its area row, its
    title, caption or notes — when the table states exactly one of each kind. A table with two bases binds none. A period
    of a header, a caption or a line above reaches only a number that can be a rate: never a duration ("36 חודשים") or a count
    (``context_kind``, ``after_kind``).
    What the source states for all its numbers is an area basis only, and only in a table's caption, title or notes (a
    sentence with no number elsewhere in the source gives no basis to other numbers): a period is stated for the numbers of its
    own line, sentence or governing line, or of its table (``_period_lines``) — "בריבית שנתית"
    in a paragraph after a feasibility check's totals is the interest rate's, not the totals'."""
    text = _read(text)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    is_row = [_ROW in ln or ln.strip().startswith("|") for ln in lines]
    tables = _tables(lines, is_row)
    table_of = {i: t for t in tables for i in range(t.first, t.last + 1)}
    first_head = tables[0].head_rows[0] if tables and tables[0].head_rows else ()
    table = _table_definitions(lines, is_row, list(first_head)) if tables else Qualifiers()
    captions = _caption_lines(lines, is_row, tables)
    period = _period_lines(lines, is_row)
    occurrences: list[tuple[frozenset[str], Occurrence]] = []
    general = Qualifiers()
    for n, (line, row) in enumerate(zip(lines, is_row, strict=True)):
        if row:
            tbl = table_of[n]
            if n in tbl.head_lines:
                continue
            cells = _row_cells(line)
            own_heads, upper_heads = _column_heads(tbl, cells)
            for i, cell in enumerate(cells):
                head = f"{own_heads[i]} {upper_heads[i]}".strip()
                found, _ = attached(cell, noun_period=False)
                for f, at, q in found:
                    if not_an_amount(cell, at, at + len(written_at(cell, at)), years=False) \
                            or note_mark_at(cell, at, at + len(written_at(cell, at))):
                        # a plot, a unit or a section label (תא שטח 101): the source writes it as a name, so no row, header or
                        # caption qualifier belongs to it (round 19 U1, KTD3)
                        occurrences.append((f, Occurrence(Qualifiers(), line, identifier=True)))
                        continue
                    own = cells[0] + " " + cell
                    row_meaning = own + " " + head
                    kind = after_kind(cell, at + len(written_at(cell, at))) or context_kind(row_meaning)
                    q = _tagged(q, "cell")
                    if i > 0:
                        q.merge(_gated(_all(cells[0]), kind, True), "row")  # the row's label
                    q.merge(_gated(_all(own_heads[i]), kind, True), "header")  # the column's header
                    q.merge(_gated(_all(upper_heads[i]), kind, False), "header")  # a header above it or spanned over it
                    if _PER_AREA.search(cells[0] + " " + head + " " + cell):
                        defined = table.found.get("basis", {})
                        if len(defined) == 1 and not q.keys("basis"):
                            key, written = next(iter(defined.items()))
                            q.add("basis", key, written, table.part_of("basis", key))
                    # a per-area amount by its label, header or cell ("שווי למ״ר"); an area by its own label or cell —
                    # a column header of areas ("סה״כ מ״ר אקוו׳") also heads a calculation's totals and factors
                    area = bool(_PER_AREA.search(own + " " + head) or _AREA_WORD.search(own))
                    # A caption also governs absolute monetary amounts, not only rates. Capital values and
                    # area/factor rows do not inherit a recurring period from neighbouring rent notes, even
                    # without a header; a duration or a count never does. An explicit row/header period takes
                    # precedence over the caption.
                    units = units_attested(row_meaning)
                    monetary = bool(units & {"ILS", "ILS_per_sqm"})
                    rent_rate = bool(_PER_AREA.search(row_meaning) and kind_attested("rent", row_meaning))
                    if (not q.keys("period") and kind not in ("duration", "count")
                            and (monetary or rent_rate or not units and not tbl.head_rows)):
                        q.merge(period(n, row_meaning))
                    occurrences.append((f, Occurrence(q, line, area=area)))
            continue
        found, _ = attached(line, noun_period=False)
        # a table's title, caption or notes state an area basis for its numbers — a period, for the numbers it governs
        # (``_period_lines``); a sentence with no number elsewhere gives no basis to other numbers
        if n in captions:
            general.merge(_only(_all(line), "period", keep=False), "paragraph")
        for i, (f, at, q) in enumerate(found):
            if not_an_amount(line, at, at + len(written_at(line, at)), years=False) \
                    or note_mark_at(line, at, at + len(written_at(line, at))):
                occurrences.append((f, Occurrence(Qualifiers(), line, identifier=True)))
                continue
            q = _tagged(q, "sentence")
            if not after_kind(line, at + len(written_at(line, at))):
                q.merge(period(n))
            # A wrapped rate's period belongs solely to the percentage immediately following it.
            if i == 0 and n > 0 and not _SENTENCE_END.search(lines[n - 1]) \
                    and not _numbers(_ENUMERATION.sub("", lines[n - 1])) \
                    and re.match(r"\s*(?:של\s+)?[\d.,]+\s?%", line):
                for pos, kind, key, written in _scan(lines[n - 1]):
                    if kind == "period" and _of_rate(lines[n - 1], pos):
                        q.add(kind, key, written, "sentence")
            occurrences.append((f, Occurrence(q, line)))
    return tuple(occurrences), general


def source_occurrences(text: str, forms: set[str]) -> tuple[list[Occurrence], Qualifiers]:
    """Where a source states one of the number's forms, and what the source states for all its numbers."""
    parsed, general = parse_source(text)
    return [o for f, o in parsed if f & forms], general


def _flat(text: str) -> str:
    return " ".join(_norm(text).split())


def _one_point(text: str) -> str:
    """``text`` with every separator between digits written as a point: a quote is the same line of the source whether its decimal
    comma was read as a decimal in the line or in the quote alone (the reading depends on what surrounds the comma)."""
    return re.sub(r"(?<=\d)[.,](?=\d)", ".", text)


def number_qualifiers(text: str, forms: frozenset[str], within: str) -> Qualifiers:
    """What a source attaches to a number where it states it inside ``within`` (a quote of the source, or a table
    row as read), with the period stated for it there and what the source states for all
    its numbers — the qualifiers a value taken from the source may carry as the source's own
    (``tools.tool_take_value``). The same number elsewhere in the source does not count."""
    parsed, general = parse_source(text)
    w = _one_point(_flat(_read(within)))
    q = Qualifiers().merge(general)
    for f, o in parsed:
        if not f & forms:
            continue  # the number is not the one asked: its line is not read
        line = _one_point(_flat(o.context))
        if line and (w in line or line in w):
            q.merge(o.qualifiers)
    return q


def basis_key(text: str | None) -> str:
    """One spelling of an area basis for comparing two values ("מ״ר אקוו׳" and "אקווי'" are one basis); a basis
    the vocabulary does not know is compared as written."""
    keys = sorted({key for _, kind, key, _ in _scan(_norm(text or "")) if kind == "basis"})
    return " ".join(keys) if keys else " ".join(_norm(text or "").split())


_CURRENCY = re.compile(r"₪|ש\"ח|(?<![א-ת])שקל")
# a scale the unit vocabulary does not carry ("באלפי ₪"): the unit is then not attested
_PER_SQM = re.compile(r"(?:(?<![א-ת])ל|/\s?)(?:מ\"ר|מטר)(?![א-ת])")


def units_attested(context: str) -> set[str]:
    """The units (measurement vocabulary) the words around a number give it: ₪, ₪ למ״ר, מ״ר, דונם or %; none when
    they state no unit. A currency stated with a scale ("באלפי ₪") attests the currency: the scale itself is carried
    apart, as the value's source scale (``calc.stated_scale``, round 7 R14), so it no longer hides the unit."""
    c = _norm(context)
    if "%" in c:
        return {"percent"}
    money = bool(_CURRENCY.search(c))
    if money:
        return {"ILS_per_sqm"} if _PER_SQM.search(c) else ({"ILS"} if not re.search(r"(?<![א-ת])לדונם", c) else set())
    if re.search(r"(?<![א-ת])דונם", c):
        return {"dunam"}
    if re.search(r"מ\"ר|מטר רבוע", c):
        return {"sqm"}
    if any(kind_attested(k, c) for k in MONEY_KINDS):
        return set()  # "מחיר ליח״ד", "השווי לאחר מקדם התאמה": an amount, its currency stated elsewhere
    label = _LABEL_NUMBER.sub(" ", c)
    if kind_attested("coefficient", label):
        return {"ratio"}
    if kind_attested("count", label) and not (kind_attested("duration", label)
                                              or re.search(r"חודש|(?<![א-ת])שנ(?:ים|ות|ה)", label)):
        return {"units"}  # "מספר השנים", "מספר חודשי הגרייס" count time: a duration, not units
    return set()


MONEY_KINDS = ("value", "price", "rent", "management_fee", "cost", "income", "profit", "levy")
# a label's number ("טבלה מספר 4", "מס' 7") counts nothing
_LABEL_NUMBER = re.compile(r"(?:מספר|מס['\"]?)\s*\d+")


def metric_word(token: str) -> bool:
    """A word that is wholly a name of a kind of value ("שווי", "לשכירות", "הרווח": ``KIND_WORDS`` with up to two
    prefix letters): it names the datum, never a property or a document. A name that merely contains its letters
    ("שוויצר") is not one."""
    word = _norm(token)
    return any(re.fullmatch(rf"[והבלמשכ]{{0,2}}(?:{rx})", word) for rx in KIND_WORDS.values())


def nonmonetary(context: str) -> bool:
    """Whether the words around a number name a quantity that is not money — a duration, count, coefficient, area
    or rate — and no amount: such a number never inherits the currency or the scale of a heading ("ממצאי הבדיקה
    באלפי ₪" over "תקופת הבנייה 36 חודשים")."""
    c = _LABEL_NUMBER.sub(" ", _norm(context))
    if any(kind_attested(k, c) for k in MONEY_KINDS):
        return False
    return any(kind_attested(k, c) for k in ("duration", "count", "coefficient", "area", "rights_area", "rate"))


# the words that name a kind of value in its row, column or sentence (professional vocabulary, R26)
KIND_WORDS = {
    "value": r"שווי", "price": r"מחיר|תמורה", "rent": r"שכירות|שכ\"ד|דמ\"ש|שכר\s+דירה",
    "management_fee": r"ניהול|דמ\"נ", "cost": r"עלות|עלויות|הוצאות|הוצאה", "income": r"הכנסות|הכנסה|תקבולים|פדיון",
    "area": r"שטח|מ\"ר", "rights_area": r"זכויות", "levy": r"היטל|(?<![א-ת])מס(?![א-ת])",
    "rate": r"שיעור|%|תשואה|היוון", "coefficient": r"מקדם", "count": r"מספר|כמות|יח\"ד", "duration": r"שנים|חודשים|תקופת",
    "profit": r"רווח",
}


def kind_attested(kind: str, context: str) -> bool:
    """Whether the words around a number name its kind (a per-area kind also needs "למ״ר")."""
    c = _norm(context)
    base = kind.removesuffix("_per_area")
    words = KIND_WORDS.get(base)
    if not words or not re.search(words, c):
        return False
    return not kind.endswith("_per_area") or bool(_PER_AREA.search(c))


def vat_attested(context: str, forms: frozenset[str], general_text: str = "") -> set[str]:
    """The VAT statuses the source gives the number: written for it in ``context`` (the VAT phrase belongs to the
    nearest number before it), or stated for the whole source in a clause with no number."""
    from app.chat.verify import vat_attachments

    pairs, general = vat_attachments(_norm(context))
    out = {pol for f, pol in pairs if f in forms} | general
    if general_text:
        out |= vat_attachments(_norm(general_text))[1]
    return out


def vat_words(text: str) -> dict[str, str]:
    """The VAT phrases a text writes, by status ("included" / "excluded") -> the first phrase as written."""
    from app.chat.verify import _VAT, _vat_polarity

    out: dict[str, str] = {}
    for m in _VAT.finditer(_norm(text)):
        if polarity := _vat_polarity(m):
            out.setdefault(polarity, m.group(0).strip())
    return out


def kind_written(kind: str, context: str) -> str:
    """The words of ``context`` that name the kind of value (``KIND_WORDS``), as written; empty when none do."""
    words = KIND_WORDS.get(kind.removesuffix("_per_area"))
    found = re.search(words, _norm(context)) if words else None
    return found.group(0) if found else ""


_PERIOD_WORDS = {"month": _MONTH_WORDS, "year": _YEAR_WORDS}   # the words that state each stored period


def measurement_states(row, field_: str) -> bool:
    """Whether a stored measurement's period, area basis or VAT is the source's, so that it may back a claim (round 12
    U9, KTD9, R6): the extraction validated it (``timeline._record_states``) and its own words are in the measurement's
    quote, its table caption or its column header — the checks the extraction applied to the model's quote
    (``measurements.extract``: ``period_quote`` is read with the month/year words, ``vat_quote`` with the VAT words, the
    basis as written). A stored value no words state is the model's, however well the extraction validated it. The
    caption and header are read when the row carries them (``caption``, ``header``); the quote of a table row carries
    its header when the table is read by column."""
    from app.chat.timeline import _record_states

    if not _record_states(row, field_):
        return False
    text = _flat(" ".join(str(x) for x in (getattr(row, a, None) for a in ("quote", "caption", "header")) if x))
    value = getattr(row, field_)
    if field_ == "period":
        words = _PERIOD_WORDS.get(value)
        return bool(words and words.search(text))
    if field_ == "area_basis":
        basis = _flat(value)
        return basis in text or bool(_all(basis).keys("basis") & _all(text).keys("basis"))
    if field_ == "vat":
        from app.chat.verify import vat_attachments

        pairs, general = vat_attachments(text)
        return value in {polarity for _, polarity in pairs} | general
    return False


def _stored_qualifiers(area_basis: str | None, period: str | None, approx: bool) -> Qualifiers:
    q = Qualifiers()
    basis = (area_basis or "").strip()
    if basis:
        keys = _all(basis).keys("basis") or {basis}
        for key in keys:
            q.add("basis", key, basis)
    if period in MEASUREMENT_PERIODS:
        q.add("period", period, PERIOD_TEXT[period])
    if approx:
        q.add("approx", "approx", "מקורב")
    return q


def measurement_qualifiers(row) -> Qualifiers:
    """What a stored measurement attaches to its number as evidence: its area basis and period only where its own words
    are in what it quotes (``measurement_states``), and its approximation."""
    return _stored_qualifiers(row.area_basis if measurement_states(row, "area_basis") else None,
                              row.period if measurement_states(row, "period") else None,
                              getattr(row, "value_form", None) == "approx")


def value_qualifiers(v, forms: frozenset[str]) -> Qualifiers:
    """What a registered value (V#) attaches to its number as evidence (round 12 U9, KTD9, R6): the area basis and the
    period of its record only when their provenance is the source's — a field the model asserted is no evidence — and
    otherwise only what the words of its own quote attach to the number."""
    provenance = v.provenance or {}
    own = number_qualifiers(v.quote, forms, v.quote)
    stored = _stored_qualifiers(v.area_basis if provenance.get("area_basis") == "source" else None,
                                v.period if provenance.get("period") == "source" else None, bool(v.approx))
    q = Qualifiers()
    for kind, field_ in (("basis", "area_basis"), ("period", "period")):
        q.merge(_only(stored if provenance.get(field_) == "source" else own, kind))
    return q.merge(_only(stored, "approx"))


@dataclass
class MeaningProblem:
    number: str  # as written in the unit
    kind: str
    reason: str
    blocking: bool
    annotation: str | None = None  # the qualifier as written, when exactly one is attested
    cite: str | None = None  # the source (S#/M#) the server found that states it, cited with it
    needs_citation: bool = False  # the unit is right; only the citation of ``cite`` is missing
    at: int | None = None  # where the number stands in the unit's text: the occurrence the problem is about


SUBJECT_ROLES = ("appraiser_determination", "actual_contract", "calculation")

# A stored measurement of a table cell, read together with the table's caption and the header of the cell's column (round 12
# U4, KTD14, U9): the caption dates an adopted measurement, and the caption and the header are where `measurement_states`
# looks for a period the quote does not hold. The column is the header the row's quote renders in front of the cell
# ("header: cell", as the extraction stores a table row); a row outside a table, or quoted by row label, has no header.
TABLE_TEXT_JOIN = (" LEFT JOIN extracted_tables et ON et.version_id = m.version_id AND et.table_index = m.table_index")
TABLE_TEXT_COLUMNS = (
    "et.structure->>'caption' AS caption, (SELECT h FROM jsonb_array_elements_text(CASE WHEN"
    " jsonb_typeof(et.structure->'headers') = 'array' THEN et.structure->'headers' ELSE '[]'::jsonb END) AS h"
    " WHERE h <> '' AND position(h || ': ' || m.value_text IN m.quote) > 0 ORDER BY length(h) DESC LIMIT 1) AS header,"
    # what the facet of a table row reads (`tools._listed_facet`, round 13 U3): whether the table is stored, the cells of the
    # row itself (a negative index would count from the end) and the table's title lines — never the whole structure
    " (et.version_id IS NOT NULL) AS table_stored, CASE WHEN m.row_index >= 0 THEN"
    " et.structure->'rows'->(m.row_index::int)->'cells' END AS row_cells, et.structure->'title' AS title_lines")
READS = 6  # database reads per verification for evidence beyond the cited passages


@dataclass
class Fetcher:
    """Evidence for a number beyond the passages a unit cites, read by the server for this check only: the same
    calculation's other passages that the turn already has, the whole table or section around a cited passage,
    and the stored measurements of the subject property in the cited documents. What it finds and uses is
    registered as a source of the turn (cited with the number); what it does not use is dropped. A comparables or
    survey row never counts: only the same table or section, or a measurement of the subject property still
    anchored in the document's current reading (``anchor_lost`` is not)."""

    ws: Workspace
    reads: int = READS  # per verification round; what was read is kept for the turn's later rounds

    @property
    def cache(self) -> dict:
        return self.ws.fetched

    def _read(self, key: tuple, load):
        if key not in self.cache:
            if self.reads <= 0:
                return None
            self.reads -= 1
            try:
                self.cache[key] = load()
            except Exception:  # noqa: BLE001 - evidence not read is evidence not found; the check stays strict
                self.cache[key] = None
        return self.cache[key]

    def expansions(self, sid: str) -> list:
        """What is around a cited passage, nearest first: the whole table of a passage from a table, then the whole
        section around the passage or its table (a table's notes are often a paragraph after it)."""
        from app.chat.tools import read_scope

        src = self.ws.sources.get(sid)
        if src is None:
            return []
        out = []
        # a part of a table read in parts (``read``) is not the whole table
        whole_table = (src.kind == "table" and src.chunk_id is None and src.table_index is not None
                       and not src.table_part)
        if src.table_index is not None and not whole_table:
            table = self._read(("x", str(src.version_id), src.table_index, src.block_start, src.block_end, "table"),
                               lambda: read_scope(self.ws, sid, "table"))
            if table is not None:
                out.append(table)
        anchor = src if src.block_start is not None else next((x for x in out if x.block_start is not None), None)
        if anchor is not None:
            ref = {"version_id": anchor.version_id, "block_start": anchor.block_start, "block_end": anchor.block_end,
                   "table_index": anchor.table_index, "chunk_id": None}
            section = self._read(("x", str(src.version_id), None, anchor.block_start, anchor.block_end, "section"),
                                 lambda: read_scope(self.ws, sid, "section", ref=ref))
            if section is not None:
                out.append(section)
        return [x for x in out if len(x.text) > len(src.text)]

    def same_calculation(self, sid: str) -> list:
        """The turn's other passages of the same table or section of the same document version."""
        src = self.ws.sources.get(sid)
        if src is None:
            return []
        return [o for o in self.ws.sources.values() if o.sid != sid and o.version_id == src.version_id and (
            (src.table_index is not None and o.table_index == src.table_index)
            or (src.section and o.section == src.section))]

    def subject_measurements(self, document_ids: set, unit: Unit | None = None) -> list:
        """The subject property's stored measurements in the documents; with ``unit`` and the context checks
        enforced, only those of the appraisal contexts the unit's cited evidence is in (KTD7)."""
        rows = self._subject_rows(document_ids)
        return rows if unit is None else self._in_context(rows, unit)

    def _in_context(self, rows: list, unit: Unit) -> list:
        from app.chat import contexts

        if not rows or not contexts.enforced():
            return rows
        cited: dict[str, set[int]] = {}
        for i in unit.ids:
            src = self.ws.sources.get(i)
            if src is not None and src.contexts:
                cited.setdefault(str(src.version_id), set()).update(src.contexts)
            v = self.ws.values.get(i)
            if v is not None and v.context:
                cited.setdefault(str(v.version_id), set()).add(v.context["number"])
            m = self.ws.measurements.get(i)
            if m is not None and m.context:
                cited.setdefault(str(m.version_id), set()).add(m.context["number"])
        out = []
        for r in rows:
            cx = self.ws.contexts.get(str(r.version_id))
            own = cited.get(str(r.version_id))
            if cx is None or not cx.multi or not own or cx.at_block(r.block_index) in own:
                out.append(r)
        return out

    def _subject_rows(self, document_ids: set) -> list:
        from sqlalchemy import text as sql

        from app.db import tenant_tx

        def load():
            with tenant_tx(self.ws.ctx) as conn:
                return conn.execute(sql(
                    "SELECT m.*, d.title, v.ingestion->>'reading_id' AS reading_id, " + TABLE_TEXT_COLUMNS
                    + " FROM measurements m JOIN document_versions v ON v.id = m.version_id"
                    " AND v.is_current JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL"
                    + TABLE_TEXT_JOIN +
                    " WHERE m.document_id = ANY(:d) AND m.status <> 'rejected' AND m.anchor_lost IS NULL"
                    " AND m.subject_role ="
                    " 'appraised_property' AND m.value_role = ANY(:r) ORDER BY m.block_index NULLS FIRST, m.id"
                    " LIMIT 400"), {"d": sorted(document_ids), "r": list(SUBJECT_ROLES)}).all()
        return self._read(("m", tuple(sorted(str(d) for d in document_ids))), load) or []

    def adopt(self, found) -> str:
        """The id of a source or measurement the check uses: registered as the turn's, once. A measurement is dated like
        one `find_measurements` lists (the quote, else the table's caption its row carries: round 12 U4, R17, KTD14), so
        none leaves verification with its date unset when its quote or caption gives one."""
        from app.chat.tools import Source, measurement_date

        if isinstance(found, Source):
            if not found.sid or self.ws.sources.get(found.sid) is not found:
                found.server_made = True
                self.ws.adopt(found)
            return found.sid
        adopted = self.ws.add_measurement(found)
        adopted.as_of = measurement_date(self.ws, found)
        if adopted.facet["status"] == STATUS_NOT_READ and self.ws.ctx is not None:
            # a table row that does not carry its table's text (the listing columns) is read through the stored table now, so
            # the cell the turn also cites as a V# is one component with it, never a second "not stated" row (round 13 U3)
            from app.chat.tools import _measurement_facet_of
            from app.db import tenant_tx

            try:
                with tenant_tx(self.ws.ctx) as conn:
                    adopted.facet = _measurement_facet_of(self.ws, conn, found)
            except Exception:  # noqa: BLE001 - a facet not read is a facet not read; the check stays strict
                pass
        return adopted.mid

    def attesting(self, unit: Unit, forms: frozenset[str], kind: str, key: str, words: set[str], written: str = "") -> str | None:
        """The id of evidence that states ``kind`` ``key`` for the number, beyond the unit's citations. ``written``: the number as the
        unit writes it, which decides whether it is specific enough to be looked for in the rest of the appraisal (round 20 U9, F6)."""
        def states(cand) -> bool:
            if cand is None or cand.sid in unit.ids:
                return False
            occ, _ = source_occurrences(cand.text, set(forms))
            return any(key in o.qualifiers.keys(kind) for o in occ)

        cited = [i for i in unit.ids if i in self.ws.sources]
        for sid in cited:
            # the passages the turn already has first; the table or section is read only when they do not say it
            for cand in self.same_calculation(sid):
                if states(cand):
                    return self.adopt(cand)
            for expansion in self.expansions(sid):
                if states(expansion):
                    return self.adopt(expansion)
        docs = {self.ws.sources[i].document_id for i in cited if self.ws.sources[i].document_id}
        docs |= {self.ws.measurements[i].document_id for i in unit.ids if i in self.ws.measurements}
        for row in self.subject_measurements(docs, unit) if docs else []:
            if not forms & numbers_in(row.value_text or ""):
                continue
            if key in measurement_qualifiers(row).keys(kind) and words & _content_words(f"{row.metric} {row.quote}"):
                return self.adopt(row)
        if _specific_number(forms, written):
            # another table of the same appraisal states it ("תוספת שנתית | 846,215" beside a paragraph that gives no period): the
            # sources of the unit's version and context, never those of another appraisal of the file (round 20 U9, F6)
            own = {(str(self.ws.sources[i].version_id), c) for i in cited for c in self.ws.sources[i].contexts}
            for cand in self.ws.sources.values():
                if own and any((str(cand.version_id), c) in own for c in cand.contexts) and states(cand):
                    return self.adopt(cand)
        return None

    def subject_statement(self, unit: Unit, forms: frozenset[str], kind: str, words: set[str]):
        """The subject property's stored measurement of the number with the same metric (its words in the unit),
        when every such measurement gives ``kind`` one value; else None."""
        docs = {self.ws.sources[i].document_id for i in unit.ids
                if i in self.ws.sources and self.ws.sources[i].document_id}
        rows = [r for r in self.subject_measurements(docs, unit) if forms & numbers_in(r.value_text or "")
                and words & _content_words(r.metric or "")] if docs else []
        values = {tuple(sorted(measurement_qualifiers(r).keys(kind))) for r in rows}
        if len(values) != 1 or not next(iter(values)):
            return None
        return rows[0]

    def expanded_occurrences(self, unit: Unit, forms: frozenset[str]) -> list[list[tuple[Occurrence, object]]]:
        """The number's occurrences around the unit's cited passages, by ring: the nearest expansion of each cited
        passage first (its table), then the next (its section)."""
        rings: list[list[tuple[Occurrence, object]]] = []
        for sid in [i for i in unit.ids if i in self.ws.sources]:
            for depth, exp in enumerate(self.expansions(sid)):
                while len(rings) <= depth:
                    rings.append([])
                rings[depth] += [(o, exp) for o in source_occurrences(exp.text, set(forms))[0]]
        return rings


def _evidence(unit: Unit, ws: Workspace, forms: frozenset[str]) -> tuple[list[Occurrence], Qualifiers]:
    occurrences: list[Occurrence] = []
    general = Qualifiers()
    for sid in unit.ids:
        if sid in ws.measurements:
            row = ws.measurements[sid].row
            if forms & numbers_in(row.value_text or ""):
                occurrences.append(Occurrence(measurement_qualifiers(row), row.quote or "", measurement=True))
            continue
        if sid in ws.values:  # a value the server verified in its source, with the meaning recorded for it
            v = ws.values[sid]
            if forms & numbers_in(v.written):
                occurrences.append(Occurrence(value_qualifiers(v, frozenset(forms)), v.quote, measurement=True))
            continue
        src = ws.sources.get(sid)
        if src is None:
            continue
        occ, gen = source_occurrences(src.text, set(forms))
        occurrences += occ
        general.merge(gen)
    return occurrences, general


def _closest(occurrences: list[Occurrence], words: set[str]) -> list[Occurrence]:
    """The occurrences the unit most likely reports: a cited measurement with the value, else the passages
    sharing most of the unit's words (the same number may mean different things in one source)."""
    measured = [o for o in occurrences if o.measurement]
    if measured:
        return measured
    scored = [(len(o.context_words & words), o) for o in occurrences]
    best = max((s for s, _ in scored), default=0)
    return [o for s, o in scored if s == best]


def _as_written(kind: str, found: dict[str, str]) -> str:
    """A qualifier as the answer shows it: a period by its label, a basis in the source's words."""
    return PERIOD_TEXT[sorted(found)[0]] if kind == "period" else display(" ".join(found.values()))


def check(unit: Unit, ws: Workspace, fetcher: Fetcher | None = None) -> list[MeaningProblem]:
    """The meaning problems of one cited unit. With a ``fetcher``, a qualifier the cited passages do not state is
    looked for in the same calculation before it counts as unsupported, and a needed one the cited passage omits
    may come from the table or section around it; either is then cited (``cite``)."""
    if not unit.ids:
        return []
    text = _read(unit.text)
    unit_text = _norm(unit.text)  # the number as the unit writes it (a decimal comma stays a comma), at the positions of ``text``
    whole = _all(text + "\n" + _norm(unit.context))
    approx_anywhere = bool(_APPROX_WORDS.search(text))
    words = _content_words(text)
    problems: list[MeaningProblem] = []
    in_unit, _ = attached(text)
    absent = absence_spans(text)
    for forms, pos, claimed in in_unit:
        if not forms:
            continue
        written = written_at(unit_text, pos)
        # an identifier, a date's part or a year is no amount; a number in a clause saying the source does not state
        # something claims no meaning (round 9 U12)
        if not_an_amount(text, pos, pos + len(written)) or any(a <= pos < b for a, b in absent):
            continue
        shape = after_kind(text, pos + len(written))  # a duration or a count has no period of its own
        occurrences, general = _evidence(unit, ws, forms)
        if not occurrences:
            continue  # not a number of the cited evidence: the number check owns it
        if all(o.identifier for o in occurrences):
            continue  # the evidence writes it as an identifier (a plot, a unit, a section), not as an amount
        # attested: what the evidence attaches to the number, and anything its source line says at all — a
        # qualifier phrased before the amount ("השטח 120 מ"ר, דמי השכירות לחודש 7,560 ₪") is never "added"
        attested = Qualifiers().merge(general)
        for o in occurrences:
            attested.merge(o.qualifiers).merge(_all(o.context, rates=False))
        # unsupported: a basis or period given to the number that its evidence does not give it
        for kind in ("basis", "period"):
            if kind == "period" and shape:
                continue
            extra = claimed.keys(kind) - attested.keys(kind)
            found_in = {k: fetcher.attesting(unit, forms, kind, k, words, written) for k in sorted(extra)} if fetcher else {}
            for k, where in found_in.items():
                if where is not None:
                    # stated in the same calculation: judged and shown with that evidence, not removed
                    if where not in unit.ids:
                        unit.ids.append(where)
                    problems.append(MeaningProblem(
                        written, kind, f"{KIND_LABELS[kind]} \"{display(claimed.found[kind][k])}\" של {written} נכתב "
                        f"ב-{where}; צטט אותו", blocking=False, cite=where, needs_citation=True, at=pos))
            extra = {k for k in extra if found_in.get(k) is None}
            if extra:
                said = claimed.found[kind][sorted(extra)[0]]
                problems.append(MeaningProblem(
                    written, kind, f"התשובה מייחסת ל-{written} {KIND_LABELS[kind]} \"{display(said)}\" שלא נכתב לגבי ערך זה "
                    "במקור; אין להוסיף תקופה או בסיס שטח שלא נכתבו", blocking=True, at=pos))
        # missing: what every closest occurrence attaches to the number, and the unit does not say at all; when the
        # cited passage does not say it, the table or section around it may (a basis in the table's area row)
        closest = _closest(occurrences, words)
        expanded: list | None = None  # read only for a qualifier the cited passages do not attach
        for kind in ("basis", "period", "approx"):
            if kind == "basis" and not _area_amount(text, pos, written, closest):
                continue  # an area basis qualifies an area or a per-area amount only
            if kind == "period" and shape:
                continue  # a duration or a count never receives a period
            if not all(o.qualifiers.keys(kind) for o in closest):
                if kind == "approx" or fetcher is None or whole.keys(kind):
                    continue
                if expanded is None:
                    expanded = fetcher.expanded_occurrences(unit, forms)
                # the nearest ring that states it for every closest occurrence, with one value
                ring = next((r for r in expanded if (near := _closest([o for o, _ in r], words))
                             and all(o.qualifiers.keys(kind) for o in near)
                             and (kind != "period" or all(len(o.qualifiers.keys(kind)) == 1 for o in near))
                             and len({tuple(sorted(o.qualifiers.found[kind])) for o in near}) == 1), None)
                if ring is None:
                    # the subject property's stored measurement of the same metric states it once
                    row = fetcher.subject_statement(unit, forms, kind, words)
                    if row is None:
                        continue
                    cite = fetcher.adopt(row)
                    as_written = _as_written(kind, measurement_qualifiers(row).found[kind])
                    if cite not in unit.ids:
                        unit.ids.append(cite)
                    problems.append(MeaningProblem(
                        written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב ב-{cite} (\"{as_written}\"); "
                        f"כתוב אותו ליד המספר וצטט את {cite}", blocking=False, annotation=as_written, cite=cite, at=pos))
                    continue
                near = _closest([o for o, _ in ring], words)
                o = near[0]
                cite = fetcher.adopt(next(src for x, src in ring if x is o))
                as_written = _as_written(kind, o.qualifiers.found[kind])
                if cite not in unit.ids:
                    unit.ids.append(cite)
                problems.append(MeaningProblem(
                    written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב ב-{cite} (\"{as_written}\"); "
                    f"כתוב אותו ליד המספר וצטט את {cite}", blocking=False, annotation=as_written, cite=cite, at=pos))
                continue
            if kind == "approx":
                if claimed.keys("approx") or approx_anywhere:
                    continue
            elif whole.keys(kind):
                continue
            # one value per occurrence: "מ״ר בנוי ברוטו" is one basis, two occurrences may disagree
            needed: dict[tuple, str] = {}
            for o in closest:
                values = o.qualifiers.found[kind]
                needed.setdefault(tuple(sorted(values)), " ".join(values.values()))
            if len(needed) == 1:
                keys, written_q = next(iter(needed.items()))
                if kind == "period" and len(keys) != 1:
                    continue  # agreeing on two periods does not establish which one belongs to this number
                as_written = PERIOD_TEXT[keys[0]] if kind == "period" else display(written_q)  # one occurrence's words
                problems.append(MeaningProblem(
                    written, kind, f"למספר {written} חסר {KIND_LABELS[kind]} כפי שנכתב במקור (\"{as_written}\"); "
                    "כתוב אותו ליד המספר", blocking=False, annotation=as_written, at=pos))
            else:
                shown = ", ".join(f"\"{display(w)}\"" for w in needed.values())
                problems.append(MeaningProblem(
                    written, kind, f"במקורות מופיעים לגבי {written} כמה ערכים של {KIND_LABELS[kind]} ({shown}); כתוב "
                    "ליד המספר את זה שנכתב לגבי הערך שבתשובה", blocking=False, at=pos))
    return problems
