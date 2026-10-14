"""The dates of an appraisal and of its data: the lazily derived valuation-date record and a datum's own date (round 11
U6b: R5, R6, R7, R10; KTD3, KTD4; AE2; round 12 U3: R15, R16, R21; KTD11, KTD12; AE6).

**What a datum's date is** (R5): the date it refers to, never the upload, processing or report-writing date. Two
things give it, both found by the server in the source, never typed by the model:

1. the date the datum itself states: in its own row, cell or sentence, or the range of its table's caption (`resolve_as_of`);
2. for the unit's own conclusion, the unit's valuation date (המועד הקובע), which the report states once for the unit.

**The dates record** (KTD3): `version_dates` holds one row per stated valuation date of an appraisal version, derived on
first use from its stored blocks (`derive`: no file, no model call, no re-read) and kept per `DATES_VERSION`. A date is
taken only when it follows a valuation label ("המועד הקובע", "מועד קובע", also "למועד הקובע לשומה ליום ..." and a
label alone in a cell with the date in the next one). The report-writing and signature labels ("תאריך עריכת השומה",
"תאריך הדוח", "נחתם", "חתימה") are recognised (`report_dates`) only so that their dates are never taken as valuation
dates; they are not stored. **From dates-v2 on** (round 12 U3, R15, KTD11, KTD12; `DATES_VERSION` is the current one) the reader reads further evidenced structures, all inside
`_date_after`/`_date_before` so `report_dates` shares them: the label "(ה)תאריך (ה)קובע", the gap words "זו", "קרי" ... and
the visit phrase (a gap only, never a label), a label-anchored two-digit year (00-69 -> 20YY, day-first, never a range;
70-99 refused), a date right before its label with a direct connector, a label-only heading followed by a block that starts
with a date, and a one-row table whose header cell holds the label (the date of its column; more rows are history). A
labelled date that the shared history predicate (`is_history`) marks, of any reading, is never the unit's date. A version
where nothing is found gets one marker row (`block_index` -1, `iso` NULL) so it is
not derived again; a version with any row of the current `DATES_VERSION` never is. Older rows are kept, never read.
Only appraisal-kind versions derive (`APPRAISALS_SQL`); a version changed to a professional kind keeps its rows
unread. Every write goes through the caller's transaction (RLS: a user outside the document's group neither reads nor
builds a row), in a savepoint, so a transaction that cannot write still gets its dates.

**The owning unit is found at read time** from the current `document_units` (units are re-derived and renumbered): a
date belongs to the unit whose block range holds its block (`assign`); a version with no units is one scope, the
document (`unit_no` 0); a date in a block outside every unit belongs to none (`unit_no` None). The conflict status is
settled the same way at read time (`StatedDate.status` is always the current unit's); the stored `status` is the
snapshot of the derivation. Two differing dates in one unit are a `conflict`, which ranks as unknown and shows both; a
month and a day of the same month agree (the finer one is the unit's date); the same date repeated is one date.

**`as_of`** (`resolve_as_of`, KTD4, in order; the first that applies decides):

1. a single date in the datum's own row, cell or sentence: provenance `source`; two or more different dates there are
   ambiguous: unknown, nothing below is tried (a range is one date with an end);
2. a range in the table's caption or header, or the one date a caption says the table is "for" ("לשנת 2023"; round 12
   U4): `source_table`;
3. the unit's valuation date, for a value of the appraisal unit whose own sentence or row holds no date at all:
   `unit_valuation`, prose, heading or table cell alike (round 11 U15, KTD4 as decided after the real-model run: the
   header line "שווי הנכס: ..." is registered with the stance *unknown*, so an adopted stance cannot be required). It
   is withheld from a datum that is not the unit's own: a party's claim, proposal or estimate (any provenance), a value
   attributed to another party (a speaker the source names, who is not the appraiser), a comparable or transaction (its
   role), and a row of a comparables table (a date column, or a caption or header that marks comparables or
   transactions). A conflict, or a unit with no date, is unknown. **History** (round 12 U4, R16, KTD12) is withheld too: a
   datum whose sentence or section the history predicate marks (`is_history`) never takes the unit's date, and an
   undated history datum, comparable or comparables-table row is returned as a date-less `AsOf` whose `date_origin`
   ("history" first, else "comparable") says why it has no date (U11 reads it: such a datum conflicts with nothing).

A date the sources do not give stays unknown (None), and a partial date keeps its precision. **Forecast** (R7,
`is_forecast`) is a property of the wording and of the date, independent of whether a date was found, so it has its own
function: a forecast marker in the sentence, or a date later than the anchor date (by its precision: this month is not
later than today). The stance plays no part: `estimate` is a party's estimate, not a forecast.

**Measurements** (`measurement_as_of`): the same resolution runs on the measurement's own quote; the date the extraction
model stored (`effective_date`) counts only when the shared parser finds the same date text in the quote.

The scope per turn: `cache` is a dict the caller keeps for one turn; a version's dates and units are read once into it."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import NotRequired, TypedDict
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat import units as unit_store
from app.extraction.hebrew import strip_bidi_controls
from app.platform import dates as shared
from app.platform.dates import Parsed
from app.platform.source_kinds import APPRAISALS_SQL
from app.professional.sections import flight, landed

logger = logging.getLogger(__name__)

DATES_VERSION = "dates-v4"
MARKER_BLOCK = -1       # the block index of the row that records "derived, none found"
DOCUMENT_SCOPE = 0      # the unit number of a version that has no units: the document is the scope

SOURCE, SOURCE_TABLE, UNIT_VALUATION = "source", "source_table", "unit_valuation"


class AsOf(TypedDict):
    """A datum's date (the contract of round 11): ISO text in `precision`, `end` for a range, where it came from, and
    whether it is a forecast. `locator` says where the date was read: "text" (the datum's row or sentence), "quote",
    "caption" or "block N" (the unit's valuation date).

    A datum that stays undated because it is history or a comparable carries no date and the reason (round 12 U4,
    KTD12): `date` None and `date_origin` "history" or "comparable" (the key is absent from every other `as_of`), so
    the conflict rule (KTD13) can tell "undated only because it is history or a comparable" from "undated"."""
    date: str | None
    precision: str | None
    end: str | None
    provenance: str | None
    forecast: bool
    locator: str | None
    date_origin: NotRequired[str]


HISTORY_ORIGIN, COMPARABLE_ORIGIN = "history", "comparable"
# the provenances of a datum's date that the server read from the source (round 11 U15 B): never one the model typed
STATED_BY_SOURCE = frozenset({SOURCE, SOURCE_TABLE, UNIT_VALUATION})


def stated_dates(as_of: Mapping | None) -> list[tuple[str, str]]:
    """The (ISO, precision) dates a datum's ``as_of`` states from the source: its date and, for a range, its end. Nothing for
    an unknown date, a forecast with no date, or a provenance that is not the source's."""
    if not as_of or not as_of.get("date") or as_of.get("provenance") not in STATED_BY_SOURCE:
        return []
    return [(d, as_of["precision"]) for d in (as_of["date"], as_of.get("end")) if d]


def holds_date(stated: Iterable[tuple[str, str]], iso: str, precision: str) -> bool:
    """Whether a written date is, at its precision or coarser, one of the stated dates (``stated_dates``)."""
    return any(iso == day[:len(iso)] and shared.PRECISION_RANK[precision] <= shared.PRECISION_RANK[rank]
               for day, rank in stated)


@dataclass(frozen=True)
class Found:
    """One valuation date the blocks state, before it is assigned to a unit."""
    block_index: int
    iso: str
    precision: str
    written: str
    primary: bool = False   # read from the report's own valuation-date section (round 12 U3b, KTD11)


@dataclass(frozen=True)
class StatedDate:
    """A stated valuation date with its owning unit (`unit_no`: 0 for the document of a version with no units, None for
    a block outside every unit) and the current status of its unit (`stated` or `conflict`)."""
    block_index: int
    iso: str
    precision: str
    written: str
    status: str
    unit_no: int | None
    primary: bool = False


@dataclass(frozen=True)
class UnitValuation:
    """What a unit says its valuation date is: `stated` with `date` (the finest of its agreeing dates), `conflict` with
    `date` None and every date in `dates`, or `none`."""
    status: str
    date: StatedDate | None
    dates: tuple[StatedDate, ...]
    unit_no: int | None


# --- the labels ------------------------------------------------------------------------------------------------------

_Q = '["״\'׳]'
def _loose(word: str) -> str:
    """A suffix word that tolerates one space inside it: the reader splits a word at a line wrap or a justified line
    ("להערכ ה", "שומ ה"). Only the label's suffix words are loosened, never "מועד" or "קובע" (round 12 U3b, KTD11)."""
    return r"\s?".join(word)


# the suffix after "המועד הקובע": "לשומה", "להערכה", "לשווי", "לחוות (ה)דעת" (round 12 U3b)
_SUFFIX = (r"\s+ל?\s?ה?(?:" + "|".join((_loose("שומה"), _loose("הערכה"), _loose("שווי"),
                                         _loose("חוות") + r"\s+ה?\s?" + _loose("דעת"))) + ")")
# a title that says what the valuation is as of: "הערכה ליום", "שומה ליום", "הערכת שווי (נכון) ליום" (round 12 U3b). The
# noun is required ("ליום" alone is no label) and "תאריך הערכה ..." is the report label, never this one.
_TITLE = (r"(?<!תאריך )(?<![א-ת])[בה]?(?:" + "|".join((_loose("שומה"), _loose("הערכה"), r"הערכת\s+שווי",
                                                      r"שומת(?:\s+מקרקעין)?")) + r")\s+(?:נכון\s+)?ליום(?![א-ת])")
# "המועד הקובע", "מועד קובע", "(ה)תאריך (ה)קובע" (round 12 U3, KTD11), "למועד הקובע לשומה": at most two prefix letters
_VALUATION = re.compile(r"(?<![א-ת])[בלוכמשה]{0,2}(?:מועד|תאריך)\s+ה?קובע(?:" + _SUFFIX + r")?(?![א-ת])|" + _TITLE)
# the report-writing and signature labels; they are recognised so their dates are never taken as valuation dates
_REPORT = re.compile(
    rf"(?<![א-ת])[בלוכמשה]{{0,2}}(?:תאריך\s+(?:עריכת\s+)?(?:ה?שומה|ה?דו{_Q}?ח|ה?חוות\s+דעת|חתימה|עריכה|ה?הפקה)"
    r"|נחתם|נחתמ(?:ה|ו)|חתימה|ניתן)(?![א-ת])")
# the words allowed between a label and its date: "ליום", "הוא", "הינו", signs and spaces; anything else (a word of
# the sentence) ends the label's reach. Round 12 U3 (KTD11) adds "זו", "זה", "קרי", "כלומר", "דהיינו" ...
_GAP_WORDS = ("לשומה", "להערכה", "ליום", "ביום", "מיום", "יום", "הוא", "הינו", "הנו", "הנה", "הינה", "היה", "לתאריך",
              "בתאריך", "תאריך", "של", "השומה", "הנדון", "זו", "זה", "קרי", "כלומר", "דהיינו")
# ... and the visit phrase "מועד הביקור (בנכס)", which counts only here, between a label and its date: it is never a
# label alone (a visit date is not a valuation date)
_VISIT = r"(?<![א-ת])[בלה]?מועד\s+ה?ביקור(?:\s+ב?נכס)?(?![א-ת])"
_GAP = re.compile(r"(?:[\s:|\-–—־,()\[\]]|(?<![א-ת])(?:" + "|".join(_GAP_WORDS) + r")(?![א-ת])|" + _VISIT + r")*")
_REACH = 120   # characters after a label that are read for its date
_PREFIX = re.compile(r"[א-ת\-־]{0,2}")
_TEXT_KINDS_SKIPPED = ("image",)

# a two-digit year, read only at the gap's end after a label (round 12 U3, KTD11): day-first, never a range, never cut
# out of a longer number
_TWO_DIGIT = re.compile(r"(?<![\d./,])(?P<d>\d{1,2})(?P<s>[./-])(?P<m>\d{1,2})(?P=s)(?P<y>\d{2})(?!\d)(?![./,]\d)")
_RANGE_AFTER = re.compile(r"\s*(?:[\-–—־‐‑]|ו?עד(?![א-ת]))\s*\d")
_TWO_DIGIT_LAST = 69   # 00-69 -> 20YY; 70-99 is refused, never read as 19YY (date-dependent guesses are no evidence)

# a date right before its label needs a direct connector ("שהינו גם התאריך הקובע") and no report label before it
_CONNECTOR = r"(?<![א-ת])(?:ש?הינו|ש?הוא|ש?הנו|והוא|גם)(?![א-ת])"
_BEFORE_TAIL = re.compile(rf"[\s,;:\-–—()]*{_CONNECTOR}(?:[\s,]*{_CONNECTOR})*[\s,]*$")
_BEFORE_WINDOW = 60   # characters before the date that are read for a report label


def _two_digit_after(rest: str, gap: int) -> tuple[Parsed | None, bool]:
    """(the two-digit-year date at the gap's end, whether one is written there at all): 00-69 -> 20YY, day-first, the
    calendar checked; 70-99, an impossible date and a range are written there but refused."""
    m = _TWO_DIGIT.match(rest, gap)
    if m is None:
        return None, False
    year = int(m["y"])
    if year > _TWO_DIGIT_LAST or _RANGE_AFTER.match(rest, m.end()):
        return None, True
    try:
        iso = date(2000 + year, int(m["m"]), int(m["d"])).isoformat()
    except ValueError:
        return None, True
    return Parsed(iso, "day", None, m.group(0), (m.start(), m.end())), True


def _date_after(text_: str, pos: int) -> tuple[Parsed | None, bool]:
    """(the date that follows a label ending at ``pos``, whether only gap is left after it). The date comes right after
    the gap (a Hebrew prefix letter of a month name may precede it); a range is no valuation date. A date with a
    two-digit year is read here too, and only here (round 12 U3, KTD11). The span is in ``text_``'s offsets."""
    rest = text_[pos:pos + _REACH]
    gap = _GAP.match(rest).end()
    if gap >= len(rest.rstrip()):
        return None, True
    for p in shared.find_all(rest):
        lead = p.span[0] - gap
        if lead < 0:
            continue
        if lead <= 2 and _PREFIX.fullmatch(rest[gap:p.span[0]]):
            if p.end is not None:
                return None, False
            return Parsed(p.iso, p.precision, None, p.written, (p.span[0] + pos, p.span[1] + pos)), False
        break
    q, _ = _two_digit_after(rest, gap)
    if q is None:
        return None, False
    return Parsed(q.iso, q.precision, None, q.written, (q.span[0] + pos, q.span[1] + pos)), False


def _date_before(text_: str, label_start: int) -> Parsed | None:
    """The date written right before a label that starts at ``label_start``, joined to it by a direct connector
    ("17.08.2025, שהינו גם התאריך הקובע"), or None. A date beside a report or signature label in the words before it is
    that label's, never the valuation date. Four-digit years only: the two-digit year is read at the gap's end alone."""
    head = text_[:label_start]
    tail = _BEFORE_TAIL.search(head)
    if tail is None:
        return None
    before = head[:tail.start()]
    dates = shared.find_all(before)
    if not dates or dates[-1].end is not None or before[dates[-1].span[1]:].strip(" ,;:-–—()"):
        return None
    p = dates[-1]
    window = before[max(0, p.span[0] - _BEFORE_WINDOW):p.span[0]]
    cut = list(_SENTENCE_BREAK.finditer(window))
    if _REPORT.search(window[cut[-1].end():] if cut else window):
        return None
    return p


def report_dates(text_: str) -> list[Parsed]:
    """The dates that stand beside a report-writing or signature label in ``text_`` (with their spans). They are never
    valuation dates: `derive` drops a candidate on the same span, and this is what the tests of that rule read."""
    t = strip_bidi_controls(text_ or "")
    found: list[Parsed] = []
    for m in _REPORT.finditer(t):
        p, _ = _date_after(t, m.end())
        if p is not None:
            found.append(p)
    return found


# --- history (round 12 U3, KTD12) --------------------------------------------------------------------------------------
# one predicate for "this datum belongs to history, not to the appraisal's own valuation": `derive` withholds a labelled
# date from the unit with it, and `resolve_as_of` (U4) withholds the unit's date from a datum with it

_HISTORY_HEADING = re.compile(
    r"שומ(?:ה|ות)(?:\s+מקרקעין)?\s+קודמ(?:ת|ות)|(?<![א-ת])היסטוריה(?![א-ת])|(?:רכישות|עסקאות)\s+קודמות")
_HISTORY_CUE = re.compile(
    r"(?<![א-ת])[שוכ]?נ(?:רכש|מכר)(?:ה|ו|ים)?(?![א-ת])|שומ(?:ה|ות)(?:\s+מקרקעין)?\s+קודמ(?:ת|ות)(?![א-ת])")
# further cues of round 12 U3b: a prior appraisal "that was prepared", a purchase, a references lead
_HISTORY_CUE_MORE = re.compile(
    r"שומ(?:ה|ת|ות)(?:\s+מקרקעין)?(?:\s+מקיפה)?\s+שנערכ(?:ה|ו)(?![א-ת])|(?<![א-ת])ה?רכיש(?:ה|ת)(?![א-ת])")
_REFERENCES = r"סימוכין(?![א-ת])"
# what leads a block before its first word: a bullet glyph (a private-use bullet of a Word list, "•", "-", "*", "–") and/or
# an enumeration ("2.", "2.1.", "(2)", "א.") (round 12 U3b)
_ENUMERATION = (r"[\s\u00a0]*(?:[\uf000-\uf8ff•●▪◦·*\-–—]+[\s\u00a0]*)?"
                r"(?:\(?(?:\d+(?:\.\d+)*|[א-ת])[.)][\s\u00a0]*)?")
_REFERENCES_LEAD = re.compile(_ENUMERATION + _REFERENCES)
_VALUED = re.compile(r"(?<![א-ת])ו?הוער(?:ך|כה|כו)(?![א-ת])")
_PAST_CUE = re.compile(r"(?<![א-ת])(?:בעבר|קודם|קודמת|קודמות)(?![א-ת])")
_SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+|\n")


def is_history(sentence: str, section_path: Iterable[str] = ()) -> bool:
    """Whether a sentence is history (KTD12): it sits under a history or prior-appraisal heading ("שומות קודמות",
    "שומות מקרקעין קודמות", "היסטוריה", "רכישות קודמות", "עסקאות קודמות"), or it carries "נרכש", "נמכר", "שומה קודמת" or
    "שומות קודמות", or it carries "הוערך" together with an explicit past cue ("בעבר", "קודם", "קודמת"). Round 12 U3b adds a
    prior appraisal "that was prepared" ("שומת מקרקעין (מקיפה) שנערכה"), a purchase ("רכישה", "רכישת", "מחיר הרכישה") and
    a sentence that leads with "סימוכין" (a list of references). A bare "הוערך"
    outside history is not: current conclusions are written that way ("שווי הזכויות בנכס הוערך ב-X ₪ נכון למועד הקובע")."""
    if any(_HISTORY_HEADING.search(strip_bidi_controls(h or "")) for h in section_path):
        return True
    s = strip_bidi_controls(sentence or "")
    return bool(_HISTORY_CUE.search(s) or _HISTORY_CUE_MORE.search(s) or _REFERENCES_LEAD.match(s)
                or (_VALUED.search(s) and _PAST_CUE.search(s)))


def _sentence_bounds(body: str, start: int, end: int) -> tuple[int, int]:
    """(from, to) of the sentence of ``body`` that holds the span: from the break before ``start`` to the break after
    ``end``."""
    lo = 0
    for m in _SENTENCE_BREAK.finditer(body, 0, start):
        lo = m.end()
    nxt = _SENTENCE_BREAK.search(body, end)
    return lo, nxt.start() if nxt else len(body)


def _sentence(body: str, start: int, end: int) -> str:
    """The sentence of ``body`` that holds the span."""
    lo, hi = _sentence_bounds(body, start, end)
    return body[lo:hi]


# --- what a label's position says (round 12 U3b, KTD11) ----------------------------------------------------------------

# a label right after one of these words refers to the valuation date ("הסמוכות למועד הקובע", "טרם המועד הקובע"); it
# never states it
_RELATIONAL_TAIL = re.compile(
    r"(?<![א-ת])[ושמבכל]{0,2}(?:ה?סמוכ(?:ה|ות|ים)|ה?סמוך|טרם|לפני|לאחר|מאז|עד)[\s\u00a0(]*$")
_ENUMERATION_RE = re.compile(_ENUMERATION)


def _is_relational(body: str, label_start: int) -> bool:
    """Whether the label at ``label_start`` follows a relational word directly: a reference to the valuation date."""
    return _RELATIONAL_TAIL.search(body, max(0, label_start - 24), label_start) is not None


def _begins_with(body: str, label_start: int) -> bool:
    """Whether the block's text begins with the label at ``label_start``, after an enumeration like "2." or "(2)": the
    block is the report's statement of its valuation date, not a passing mention."""
    return _ENUMERATION_RE.match(body).end() == label_start


def _heading_is_label(path: Sequence) -> bool:
    """Whether the last section heading of a path is the valuation label ("2. המועד הקובע")."""
    return bool(path) and _VALUATION.search(strip_bidi_controls(path[-1] or "")) is not None


# --- derivation ----------------------------------------------------------------------------------------------------------

def _cells(row):
    """The cells of a stored table row: a row is its list of cells or a dict that holds them under ``cells``."""
    return row.get("cells") if isinstance(row, dict) else row


def _table_dates(headers: Sequence, rows: Sequence) -> list[tuple[Parsed, str]]:
    """A table whose header cell holds a valuation label gives the date in the same column, only for a table with ONE
    data row (round 12 U3, KTD11); a table with more rows is a dated history, whose rows hold their own dates. Each date
    comes with the header and cell text it was read from, for the history predicate."""
    if len(rows) != 1:
        return []
    row = _cells(rows[0])
    out: list[tuple[Parsed, str]] = []
    for col, head in enumerate(headers):
        if not isinstance(head, str) or not _VALUATION.search(strip_bidi_controls(head)):
            continue
        if not isinstance(row, Sequence) or col >= len(row) or not isinstance(row[col], str):
            continue
        cell = strip_bidi_controls(row[col])
        p, _ = _date_after(cell, 0)
        if p is not None:
            out.append((p, f"{strip_bidi_controls(head)} {cell}"))
    return out


def _without_header(body: str, headers: Sequence) -> str:
    """A table block's text without its header line (after the title lines, if any), which `_table_dates` reads cell by
    cell: a label in the header must not take the first cell of the next line as its date."""
    line = " ".join(" | ".join(strip_bidi_controls(h) if isinstance(h, str) else "" for h in headers).split())
    if not line.strip(" |"):
        return body
    lines = body.split("\n")
    for k, candidate in enumerate(lines):
        if " ".join(candidate.split()) == line:
            return "\n".join(lines[:k] + lines[k + 1:])
    return body


def _first_row_header(rows: Sequence) -> tuple[list[str], Sequence]:
    """For a stored table whose ``headers`` are empty: the first row as the header when one of its cells holds the
    valuation label, with the rows left as the data rows (round 12 U3b, KTD11); otherwise no header."""
    if not rows:
        return [], rows
    first = _cells(rows[0])
    if not isinstance(first, Sequence) or isinstance(first, str):
        return [], rows
    cells = [c if isinstance(c, str) else "" for c in first]
    if any(_VALUATION.search(strip_bidi_controls(c)) for c in cells):
        return cells, rows[1:]
    return [], rows


def derive(blocks: Iterable, tables: Mapping[int, Mapping] | None = None) -> list[Found]:
    """The labelled valuation dates of stored blocks (anything with ``block_index``, ``kind`` and ``text``, and
    optionally ``section_path`` and ``table_index``), in block order. Pure: no database, no model. A date is taken when
    it follows a valuation label in the block, when a direct connector joins it to the label that follows it, when the
    label stands alone (a cell, a heading) and the next block starts with a date, or when a one-row table's header
    cell holds the label (``tables``: `extracted_tables.structure` by table index; a table with empty headers whose
    first row holds the label is read with that row as the header). A date the history predicate marks (`is_history`:
    its sentence or its section) is never taken, of any of these readings (round 12 U3, KTD11).

    Round 12 U3b (KTD11) adds three position rules, none of which looks at the date itself:

    - a label right after a relational word ("הסמוכות למועד", "טרם המועד") refers to the valuation date and takes no date
      by any reading;
    - a sentence that holds two or more different labelled dates compares or references them: none is a statement of
      the unit's date;
    - a date read from a block that begins with the label (after an enumeration), or from a block whose last section
      heading is the label, is *primary*: the report's own statement of the date (`Found.primary`; `resolve_unit`)."""
    rows: list[tuple[int, str, tuple, list[tuple[Parsed, str]]]] = []
    for b in blocks:
        if getattr(b, "kind", "paragraph") in _TEXT_KINDS_SKIPPED:
            continue
        body = strip_bidi_controls(b.text or "")
        path = tuple(getattr(b, "section_path", None) or ())
        table_dates: list[tuple[Parsed, str]] = []
        structure = (tables or {}).get(getattr(b, "table_index", None))
        headers = (structure or {}).get("headers") or []
        data_rows = (structure or {}).get("rows") or []
        if structure and not any(headers):
            headers, data_rows = _first_row_header(data_rows)
        if structure and any(headers):
            table_dates = _table_dates(headers, data_rows)
            body = _without_header(body, headers)
        rows.append((int(b.block_index), body, path, table_dates))
    out: list[Found] = []
    at: dict[tuple[int, str, str], int] = {}

    def take(block: int, p: Parsed, primary: bool) -> None:
        key = (block, p.iso, p.precision)
        if key not in at:
            at[key] = len(out)
            out.append(Found(block, p.iso, p.precision, p.written, primary))
        elif primary and not out[at[key]].primary:
            out[at[key]] = replace(out[at[key]], primary=True)

    for i, (index, body, path, table_dates) in enumerate(rows):
        section_primary = _heading_is_label(path)
        lead_history = _REFERENCES_LEAD.match(body) is not None   # a references block: all of it is history
        for p, sentence in table_dates:
            if not lead_history and not is_history(sentence, path):
                take(index, p, section_primary)
        labels = list(_VALUATION.finditer(body))
        if not labels:
            continue
        report = {p.span for p in report_dates(body)}
        # (the date, its sentence's bounds, the text history is judged on, primary) of each label read in this block
        read: list[tuple[Parsed, tuple[int, int], str, bool]] = []
        lead_sentence: tuple[int, int] | None = None   # the lead sentence of a label-led block whose lead took no date
        for m in labels:
            if _is_relational(body, m.start()):
                continue
            leads = _begins_with(body, m.start())
            # a label-led block that restates the label in its lead sentence ("2. המועד הקובע לשומה: המועד הקובע לשומה הינו
            # ... 01/07/2020"): the restated label is the statement, so it is primary too
            primary = (section_primary or leads
                       or (lead_sentence is not None and lead_sentence[0] <= m.start() < lead_sentence[1]))
            p, only_gap = _date_after(body, m.end())
            if leads and p is None and _date_before(body, m.start()) is None and not only_gap:
                lead_sentence = _sentence_bounds(body, m.start(), m.end())
            if p is not None:
                if p.span not in report:
                    read.append((p, _sentence_bounds(body, m.start(), p.span[1]), _sentence(body, m.start(), p.span[1]),
                                 primary))
                continue
            q = _date_before(body, m.start())
            if q is not None:
                if q.span not in report:
                    read.append((q, _sentence_bounds(body, q.span[0], m.end()), _sentence(body, q.span[0], m.end()),
                                 primary))
                continue
            if only_gap and i + 1 < len(rows):
                nxt_index, nxt, nxt_path, _ = rows[i + 1]
                r, _ = _date_after(nxt, 0)
                if r is not None and not any(m2.start() < r.span[0] for m2 in _REPORT.finditer(nxt)):
                    sentence = f"{_sentence(body, m.start(), m.end())} {_sentence(nxt, r.span[0], r.span[1])}"
                    if not lead_history and not is_history(sentence, path + nxt_path):
                        take(nxt_index, r, primary or _heading_is_label(nxt_path))
        by_sentence: dict[tuple[int, int], list[Parsed]] = defaultdict(list)
        for p, bounds, _, _ in read:
            by_sentence[bounds].append(p)
        for p, bounds, sentence, primary in read:
            if _agreeing(by_sentence[bounds]) is None:
                continue   # two different labelled dates in one sentence: a comparison, none of them a statement
            if not lead_history and not is_history(sentence, path):
                take(index, p, primary)
    return out


# --- the units: assignment and the unit's valuation date ---------------------------------------------------------------

def _agreeing[D: (StatedDate, Parsed)](dates: Sequence[D]) -> D | None:
    """The finest date when every date is that one or a coarser form of it (a month of the day's month), else None. A
    date is a `StatedDate` or a parsed one (the labelled dates of one sentence): both carry the ISO text."""
    finest = max(dates, key=lambda d: len(d.iso))
    return finest if all(finest.iso.startswith(d.iso) for d in dates) else None


def _settled(dates: Sequence[StatedDate]) -> StatedDate | None:
    """The unit's date from its dates, or None when they conflict. Primary dates (the report's own valuation-date
    section) decide when the unit has any: the other dates are references, listed but never in conflict with them; the
    primary dates must agree among themselves. With none, every date counts (round 12 U3b, KTD11)."""
    primary = [d for d in dates if d.primary]
    return _agreeing(primary or dates)


def owner_of_block(units: Sequence, block_index: int) -> int | None:
    """The number of the unit that holds a block: `DOCUMENT_SCOPE` for a version with no units, None for a block outside
    every unit."""
    if not units:
        return DOCUMENT_SCOPE
    return next((u.unit_no for u in units if u.block_first <= block_index <= u.block_last), None)


def assign(found: Iterable[Found], units: Sequence) -> list[StatedDate]:
    """The dates with the unit that holds each block now and the status of that unit: `conflict` when its dates do not
    agree. A version with no units is one scope (`DOCUMENT_SCOPE`); a block outside every unit has no unit."""
    owned = [(f, owner_of_block(units, f.block_index)) for f in sorted(found, key=lambda x: (x.block_index, x.iso))]
    plain = [StatedDate(f.block_index, f.iso, f.precision, f.written, "stated", owner, f.primary) for f, owner in owned]
    by_unit: dict[int, list[StatedDate]] = defaultdict(list)
    for d in plain:
        if d.unit_no is not None:
            by_unit[d.unit_no].append(d)
    conflicted = {no for no, ds in by_unit.items() if _settled(ds) is None}
    return [StatedDate(d.block_index, d.iso, d.precision, d.written, "conflict" if d.unit_no in conflicted else "stated",
                       d.unit_no, d.primary) for d in plain]


def resolve_unit(dates: Sequence[StatedDate], unit_no: int | None) -> UnitValuation:
    """The valuation date of one unit from its own dates (the caller passes only the unit's)."""
    if not dates:
        return UnitValuation("none", None, (), unit_no)
    best = _settled(dates)
    if best is None:
        return UnitValuation("conflict", None, tuple(dates), unit_no)
    return UnitValuation("stated", best, tuple(dates), unit_no)


# --- the store -----------------------------------------------------------------------------------------------------------

VISIBLE_SQL = ("SELECT v.id AS version_id, v.document_id FROM document_versions v JOIN documents d"
                f" ON d.id = v.document_id WHERE v.id = ANY(:v) AND {APPRAISALS_SQL}")
_ROWS_SQL = ("SELECT version_id, block_index, iso, precision, written, primary_section FROM version_dates"
             " WHERE version_id = ANY(:v) AND dates_version = :dv ORDER BY version_id, block_index, iso")
# the blocks with their section path (the history predicate) and, for a table block, the table's stored structure
# (a header label gives the date of its column: `derive`'s ``tables``)
_BLOCKS_SQL = ("SELECT b.block_index, b.kind, b.text, b.section_path, b.table_index, t.structure FROM document_blocks b"
               " LEFT JOIN extracted_tables t ON t.version_id = b.version_id AND t.table_index = b.table_index"
               " WHERE b.version_id = :v AND b.kind <> 'image' ORDER BY b.block_index")
_INSERT = ("INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
           " written, status, primary_section) VALUES (app_office(), :d, :v, :dv, :b, :i, :p, :w, :s, :pr)")


def _stored(conn: Connection, version_ids: Sequence[UUID]) -> dict[UUID, list[Found]]:
    """The version's rows of the current `DATES_VERSION` (a version with a marker row only maps to ``[]``)."""
    out: dict[UUID, list[Found]] = {}
    for r in conn.execute(text(_ROWS_SQL), {"v": list(version_ids), "dv": DATES_VERSION}):
        out.setdefault(r.version_id, [])
        if r.iso is not None and r.block_index != MARKER_BLOCK:
            out[r.version_id].append(Found(r.block_index, r.iso, r.precision, r.written or r.iso,
                                           bool(r.primary_section)))
    return out


def units_of(conn: Connection, version_id: UUID, cache: dict) -> tuple:
    key = ("units", version_id)
    if key not in cache:
        cache[key] = tuple(unit_store.ensure(conn, version_id))
    return cache[key]


def prime_units(conn: Connection, version_ids: Iterable[UUID | str], cache: dict) -> None:
    """The stored units of many appraisal versions in one query, into the cache keys `units_of` reads, before their
    dates are read together (`valuation_dates`); a version whose dates or units the cache holds, and one with no units
    stored, is left alone (`units_of`: `units.ensure` builds them)."""
    todo = sorted((v for v in dict.fromkeys(UUID(str(v)) for v in version_ids)
                   if ("dates", v) not in cache and ("units", v) not in cache), key=str)
    for v, units in unit_store.stored_many(conn, todo).items():
        cache[("units", v)] = tuple(units)


def _derive_and_store(conn: Connection, version_id: UUID, document_id: UUID, cache: dict) -> tuple[StatedDate, ...]:
    """Derive the version's valuation dates from its stored blocks and store them (or the marker row) once, and return
    them assigned to the current units (what `valuation_dates` caches): a second caller in the process waits for the
    first, and across processes the advisory lock makes the second writer find the first's rows. A transaction that
    cannot write still gets its dates."""
    blocks = conn.execute(text(_BLOCKS_SQL), {"v": version_id}).all()
    tables = {b.table_index: b.structure for b in blocks if b.table_index is not None and b.structure}
    units = units_of(conn, version_id, cache)
    assigned = tuple(assign(derive(blocks, tables), units))
    lock = flight("dates", str(version_id), DATES_VERSION)
    try:
        with lock:
            try:
                with conn.begin_nested():
                    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                                 {"k": f"dates:{version_id}:{DATES_VERSION}"})
                    existing = _stored(conn, [version_id]).get(version_id)
                    if existing is not None:   # another process wrote them while this one waited for the lock
                        return tuple(assign(existing, units))
                    params = {"d": document_id, "v": version_id, "dv": DATES_VERSION}
                    rows = [{"b": d.block_index, "i": d.iso, "p": d.precision, "w": d.written, "s": d.status,
                             "pr": d.primary} for d in assigned] or [{"b": MARKER_BLOCK, "i": None, "p": None, "w": None,
                                                                      "s": "stated", "pr": False}]
                    conn.execute(text(_INSERT), [params | row for row in rows])
            except Exception:  # noqa: BLE001 - a read-only transaction or a vanished version: the dates still serve
                logger.info("dates of version %s not stored", version_id, exc_info=True)
            return assigned
    finally:
        landed("dates", str(version_id), DATES_VERSION, lock)


def valuation_dates(conn: Connection, version_ids: Iterable[UUID | str], *,
                    cache: dict | None = None) -> dict[UUID, tuple[StatedDate, ...]]:
    """The stated valuation dates of each version, with their unit and current status, derived and stored on first use.
    A version that is not an appraisal, does not exist or is not visible to the caller is not in the result. ``cache``
    is the turn's dict: a version already in it costs no query."""
    cache = {} if cache is None else cache
    ids = list(dict.fromkeys(UUID(str(v)) for v in version_ids))
    # derived in a stable order: each derivation holds its version's advisory lock until the transaction ends, so two
    # concurrent cold turns over overlapping versions must take them in the same order to never wait on each other
    missing = sorted((v for v in ids if ("dates", v) not in cache), key=str)
    if missing:
        visible = {r.version_id: r.document_id for r in conn.execute(text(VISIBLE_SQL), {"v": missing})}
        stored = _stored(conn, list(visible))
        for v in missing:
            if v not in visible:
                cache[("dates", v)] = None
                continue
            found = stored.get(v)
            cache[("dates", v)] = (tuple(assign(found, units_of(conn, v, cache))) if found is not None
                                   else _derive_and_store(conn, v, visible[v], cache))
    return {v: cache[("dates", v)] for v in ids if cache.get(("dates", v)) is not None}


def unit_valuation(conn: Connection, version_id: UUID | str, block_index: int, *,
                   cache: dict | None = None) -> UnitValuation:
    """The valuation date of the unit that holds ``block_index`` of the version, as the current units cut the file:
    `none` for a version the caller cannot read, a professional document, a block outside every unit and a unit that
    states no date."""
    cache = {} if cache is None else cache
    v = UUID(str(version_id))
    dates = valuation_dates(conn, [v], cache=cache).get(v)
    if dates is None:
        return UnitValuation("none", None, (), None)
    unit_no = owner_of_block(units_of(conn, v, cache), block_index)
    if unit_no is None:
        return UnitValuation("none", None, (), None)
    return resolve_unit([d for d in dates if d.unit_no == unit_no], unit_no)


# --- forecast ---------------------------------------------------------------------------------------------------------

_P = r"(?<![א-ת])[ובהכלמש]{0,2}"
_FORECAST = re.compile(
    rf"{_P}(?:צפוי(?:ה|ים|ות)?|תחזית|יהיה|תהיה|יהיו|עתיד(?:י|יים|ית|יות)?)(?![א-ת])"
    rf"|{_P}הערכה\s+ל[\-־]?\s*(?:שנת\s*)?\d")   # "הערכה ל" is a forecast only before a date; "הערכה לשווי" is not


def is_after_anchor(iso: str, precision: str, anchor: date) -> bool:
    """Whether a date is later than the anchor date, by its precision (this month is not later than today)."""
    return shared.cut(iso, precision) > shared.cut(anchor.isoformat(), precision)


def is_forecast(sentence: str, as_of: AsOf | dict | None, anchor: date) -> bool:
    """True when the sentence is worded as a forecast, or the datum's date is later than the anchor date (by its
    precision; for a range, its end). The stance is not used (R7, KTD4)."""
    if _FORECAST.search(strip_bidi_controls(sentence or "")):
        return True
    if not as_of:
        return False
    return is_after_anchor(as_of.get("end") or as_of["date"], as_of["precision"], anchor)


# --- as_of ----------------------------------------------------------------------------------------------------------------

def _build(p_iso: str, precision: str, end: str | None, provenance: str, locator: str, sentence: str,
           anchor: date) -> AsOf:
    partial = {"date": p_iso, "precision": precision, "end": end}
    return AsOf(date=p_iso, precision=precision, end=end, provenance=provenance,
                forecast=is_forecast(sentence, partial, anchor), locator=locator)


# the stances that are a party's own position, not the unit's conclusion (`extract.STANCES`); any provenance
PARTY_STANCES = frozenset({"claim", "proposal", "estimate"})
# the roles (`calc.ROLES`: "comparison"; the other two are defensive) that mark a datum as a comparable or a transaction
COMPARABLE_ROLES = frozenset({"comparison", "comparable", "transaction"})
# a speaker who is the unit's author or its decision maker: not "another party"
_AUTHOR = re.compile(r"ה?(?:שמאי(?:ת)?(?:\s+ה?מכריע(?:ה)?)?|מעריך(?:ה)?|מכריע(?:ה)?|כותב(?:ת)?|ועדה(?:\s+ה?מקומית)?"
                     r"|ועדת\s+ה?ערר|בית\s+ה?משפט)")
# a table is a comparables table by its caption or headers ...
_COMPARABLES = re.compile(r"עסקאות|עסקת|עסקה|השוואה|השוואת|השוואתי")
# ... or by a date column of its own (a header that names a date of the row; "שנת בנייה" is not one)
_DATE_HEADER = re.compile(r"תאריך|מועד|(?:חודש|שנת|שנה)\s+ה?(?:עסקה|מכירה|חוזה)")


def is_comparables_table(headers: Iterable, *captions: str | None) -> bool:
    """True for a table whose rows are comparables or transactions (round 11 U15, KTD4): a header that names a date, or a
    caption or header that names transactions or a comparison. Such a row without a date of its own stays undated: it
    is another property's datum, never the unit's conclusion."""
    heads = [strip_bidi_controls(h) for h in headers if isinstance(h, str)]
    caps = [strip_bidi_controls(c) for c in captions if c]
    return (any(_DATE_HEADER.search(h) for h in heads)
            or any(_COMPARABLES.search(x) for x in (*heads, *caps)))


# a caption that says what a table is "for" ("דמי שכירות לשנת 2023", "... לחודש 03/2023") states the period of its rows;
# "עסקאות בשנת 2022" names when transactions happened and dates nothing (KTD4: only a range or a "for" period)
_PERIOD_FOR = re.compile(r"(?<![א-ת])ל(?:שנת|חודש)\s*$")


def _caption_date(caption: str, sentence: str, anchor: date) -> AsOf | None:
    """The date a table caption (with its column header) gives its rows: the one range it states, or the one date that
    follows "לשנת"/"לחודש" (round 12 U4, KTD14: an adopted measurement of a table "for 2023" is dated 2023)."""
    found = shared.find_all(caption)
    if len(found) != 1:
        return None
    c = found[0]
    if c.end is None and not _PERIOD_FOR.search(caption[:c.span[0]]):
        return None
    return _build(c.iso, c.precision, c.end, SOURCE_TABLE, "caption", sentence, anchor)


def _withheld(origin: str, sentence: str, anchor: date) -> AsOf:
    """No date, and why (history or a comparable): the forecast wording of the sentence still marks it."""
    return AsOf(date=None, precision=None, end=None, provenance=None, forecast=is_forecast(sentence, None, anchor),
                locator=None, date_origin=origin)


def _labelled_two_digit(sentence: str, own: Sequence[Parsed]) -> list[Parsed]:
    """The dates a datum's own sentence writes with a two-digit year right after a valuation label ("לתאריך הקובע 31.12.19,
    הוערך הנכס ..."), read by the same label-anchored helper the unit's valuation date uses (`_date_after`, round 12 U12b,
    KTD11, AE6). The shared parser refuses a two-digit year (``own`` are the dates it found), so such a date is never found
    anywhere else: an unlabelled "5.3.24" in a cell or a sentence, a report or signature label's date, a range, 70-99 and an
    impossible date stay undated. Only the valuation labels count here: a report date is not the datum's date."""
    held = {p.span for p in own}
    out: list[Parsed] = []
    for m in _VALUATION.finditer(sentence):
        p, _ = _date_after(sentence, m.end())
        if p is not None and p.span not in held and not any(_overlap(p.span, h) for h in held):
            out.append(p)
    return out


# what a restated date may be written with besides the date: a valuation label ("למועד הקובע", "תאריך קובע"), the gap words
# between a label and its date, "נכון ל" and signs
_RESTATING = re.compile(r"(?:[\s:|\-–—־,;.()\[\]]|(?<![א-ת])(?:" + "|".join((*_GAP_WORDS, "נכון", "ל", "ב", "מ", "ה")) + r")(?![א-ת]))*")


def restates_as_of(text_: str | None, as_of: Mapping | None) -> bool:
    """Whether a text is nothing but the datum's own resolved date (round 12 U12b, S3): every date it writes is, at its
    precision or coarser, a date the server read for the datum (``stated_dates``), and the rest of it is a valuation label
    and gap words ("למועד הקובע 30/06/2025", "נכון ל-2025"). A different date, or a word of the writer's own ("תרחיש
    אופטימי"), is not a restatement."""
    t = strip_bidi_controls(text_ or "")
    found = shared.find_all(t)
    stated = stated_dates(as_of)
    if not found or not stated:
        return False
    if not all(holds_date(stated, p.iso, p.precision) and (p.end is None or holds_date(stated, p.end, p.precision))
               for p in found):
        return False
    rest = t
    for p in sorted(found, key=lambda x: x.span, reverse=True):
        rest = rest[:p.span[0]] + " " + rest[p.span[1]:]
    return _RESTATING.fullmatch(_VALUATION.sub(" ", rest)) is not None


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _is_unit_own(stance: str | None, role: str | None, stated_by: str | None, stated_by_provenance: str | None,
                 comparables: bool) -> bool:
    """Whether a value may take its unit's valuation date: not a party's position, not another party's, not a comparable
    or a transaction, not a row of a comparables table."""
    if comparables or stance in PARTY_STANCES or role in COMPARABLE_ROLES:
        return False
    who = " ".join(strip_bidi_controls(stated_by or "").split())
    return not (who and stated_by_provenance == "source" and not _AUTHOR.fullmatch(who))


def resolve_as_of(text_: str, *, table_caption: str | None = None, stance: str | None = None, role: str | None = None,
                  stated_by: str | None = None, stated_by_provenance: str | None = None, comparables: bool = False,
                  unit_valuation: UnitValuation | None = None, section_path: Iterable[str] = (), anchor: date,
                  locator: str = "text") -> AsOf | None:
    """The datum's date by the KTD4 order (see the module docstring), or None (unknown). ``text_`` is the datum's own row,
    cell or sentence; ``table_caption`` the caption or header of its table; ``stance``, ``role``, ``stated_by`` (with
    ``stated_by_provenance``: only a speaker the source names counts) and ``comparables`` (the datum is a row of a
    comparables table, `is_comparables_table`) say whether it is the unit's own datum; ``unit_valuation`` the valuation
    of the unit that holds the datum; ``section_path`` the sections the datum sits under (the history predicate).

    A datum that belongs to history (`is_history`: its sentence or its section) never takes the unit's valuation date,
    and one that is a comparable or a row of a comparables table never does either; when they stay undated the result
    is a date-less `AsOf` that says why (`date_origin`: "history" or "comparable"; history first). The datum's own date
    and its table's range still win (round 12 U4, R16, KTD12)."""
    sentence = strip_bidi_controls(text_ or "")
    own = shared.find_all(sentence)
    labelled = _labelled_two_digit(sentence, own)
    if labelled:   # a valuation label's own two-digit date is the datum's date, whatever other dates the sentence names
        if len({p.iso for p in labelled}) > 1:
            return None   # two labelled dates: ambiguous
        return _build(labelled[0].iso, labelled[0].precision, None, SOURCE, locator, sentence, anchor)
    if len({(p.iso, p.precision, p.end) for p in own}) > 1:
        return None   # ambiguous: two different dates in the row or sentence
    if own:
        return _build(own[0].iso, own[0].precision, own[0].end, SOURCE, locator, sentence, anchor)
    from_caption = _caption_date(strip_bidi_controls(table_caption or ""), sentence, anchor)
    if from_caption is not None:
        return from_caption
    if is_history(sentence, section_path):
        return _withheld(HISTORY_ORIGIN, sentence, anchor)
    if (unit_valuation is not None and unit_valuation.status == "stated" and unit_valuation.date is not None
            and _is_unit_own(stance, role, stated_by, stated_by_provenance, comparables)):
        d = unit_valuation.date
        return _build(d.iso, d.precision, None, UNIT_VALUATION, f"block {d.block_index}", sentence, anchor)
    if comparables or role in COMPARABLE_ROLES:
        return _withheld(COMPARABLE_ORIGIN, sentence, anchor)
    return None


_STORED_YEAR, _STORED_MONTH, _STORED_DAY = (re.compile(r"\d{4}"), re.compile(r"\d{4}-\d{2}"),
                                           re.compile(r"\d{4}-\d{2}-\d{2}"))


def _stored_date(value: str | None) -> tuple[str, str] | None:
    """(iso, precision) of the date the extraction model stored, or None when it is no supported date."""
    s = (value or "").strip()
    if _STORED_DAY.fullmatch(s):
        try:
            date.fromisoformat(s)
        except ValueError:
            return None
        return s, "day"
    if _STORED_MONTH.fullmatch(s) and 1 <= int(s[5:]) <= 12:
        return s, "month"
    if _STORED_YEAR.fullmatch(s):
        return s, "year"
    p = shared.parse(s)
    return (p.iso, p.precision) if p is not None and p.end is None else None


def measurement_as_of(quote: str, effective_date: str | None, *, table_caption: str | None = None,
                      anchor: date) -> AsOf | None:
    """A measurement's date: the stored ``effective_date`` counts only when the shared parser finds the same date (same
    ISO text, same precision) in the quote; it then picks that date even when the quote holds several. Otherwise the
    quote is resolved on its own (its single date, the table's range); a stored date the quote does not hold is unknown."""
    text_ = strip_bidi_controls(quote or "")
    stored = _stored_date(effective_date)
    if stored is not None:
        for p in shared.find_all(text_):
            if p.end is None and (p.iso, p.precision) == stored:
                return _build(p.iso, p.precision, None, SOURCE, "quote", text_, anchor)
    return resolve_as_of(text_, table_caption=table_caption, anchor=anchor, locator="quote")
