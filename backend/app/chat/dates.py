"""The dates of an appraisal and of its data: the lazily derived valuation-date record and a datum's own date (round 11
U6b: R5, R6, R7, R10; KTD3, KTD4; AE2).

**What a datum's date is** (R5): the date it refers to, never the upload, processing or report-writing date. Two
things give it, both found by the server in the source, never typed by the model:

1. the date the datum itself states: in its own row, cell or sentence, or the range of its table's caption (`resolve_as_of`);
2. for the unit's own conclusion, the unit's valuation date (המועד הקובע), which the report states once for the unit.

**The dates record** (KTD3): `version_dates` holds one row per stated valuation date of an appraisal version, derived on
first use from its stored blocks (`derive`: no file, no model call, no re-read) and kept per `DATES_VERSION`. A date is
taken only when it follows a valuation label ("המועד הקובע", "מועד קובע", also "למועד הקובע לשומה ליום ..." and a
label alone in a cell with the date in the next one). The report-writing and signature labels ("תאריך עריכת השומה",
"תאריך הדוח", "נחתם", "חתימה") are recognised (`report_dates`) only so that their dates are never taken as valuation
dates; they are not stored. A version where nothing is found gets one marker row (`block_index` -1, `iso` NULL) so it is
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
2. a range in the table's caption or header: `source_table`;
3. the unit's valuation date, when the value is the unit's own conclusion (stance `adopted` with source provenance, prose,
   heading or table cell alike) and its sentence or row holds no date at all: `unit_valuation`; a conflict, a unit with no
   date or any other stance is unknown.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TypedDict
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat import units as unit_store
from app.extraction.hebrew import strip_bidi_controls
from app.platform import dates as shared
from app.platform.dates import Parsed
from app.platform.source_kinds import APPRAISALS_SQL
from app.professional.sections import flight, landed

logger = logging.getLogger(__name__)

DATES_VERSION = "dates-v1"
MARKER_BLOCK = -1       # the block index of the row that records "derived, none found"
DOCUMENT_SCOPE = 0      # the unit number of a version that has no units: the document is the scope

SOURCE, SOURCE_TABLE, UNIT_VALUATION = "source", "source_table", "unit_valuation"


class AsOf(TypedDict):
    """A datum's date (the contract of round 11): ISO text in `precision`, `end` for a range, where it came from, and
    whether it is a forecast. `locator` says where the date was read: "text" (the datum's row or sentence), "quote",
    "caption" or "block N" (the unit's valuation date)."""
    date: str
    precision: str
    end: str | None
    provenance: str
    forecast: bool
    locator: str


@dataclass(frozen=True)
class Found:
    """One valuation date the blocks state, before it is assigned to a unit."""
    block_index: int
    iso: str
    precision: str
    written: str


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
# "המועד הקובע", "מועד קובע", "למועד הקובע לשומה": at most two prefix letters
_VALUATION = re.compile(r"(?<![א-ת])[בלוכמשה]{0,2}מועד\s+ה?קובע(?:\s+ל?(?:ה?שומה|ה?הערכה|ה?שווי))?(?![א-ת])")
# the report-writing and signature labels; they are recognised so their dates are never taken as valuation dates
_REPORT = re.compile(
    rf"(?<![א-ת])[בלוכמשה]{{0,2}}(?:תאריך\s+(?:עריכת\s+)?(?:ה?שומה|ה?דו{_Q}?ח|ה?חוות\s+דעת|חתימה|עריכה)"
    r"|נחתם|נחתמ(?:ה|ו)|חתימה|ניתן)(?![א-ת])")
# the words allowed between a label and its date: "ליום", "הוא", "הינו", signs and spaces; anything else (a word of
# the sentence) ends the label's reach
_GAP_WORDS = ("לשומה", "להערכה", "ליום", "ביום", "מיום", "יום", "הוא", "הינו", "הנו", "הנה", "הינה", "היה", "לתאריך",
              "בתאריך", "תאריך", "של", "השומה", "הנדון")
_GAP = re.compile(r"(?:[\s:|\-–—־,()\[\]]|(?<![א-ת])(?:" + "|".join(_GAP_WORDS) + r")(?![א-ת]))*")
_REACH = 120   # characters after a label that are read for its date
_PREFIX = re.compile(r"[א-ת\-־]{0,2}")
_TEXT_KINDS_SKIPPED = ("image",)


def _date_after(text_: str, pos: int) -> tuple[Parsed | None, bool]:
    """(the date that follows a label ending at ``pos``, whether only gap is left after it). The date comes right after
    the gap (a Hebrew prefix letter of a month name may precede it); a range is no valuation date. The span is in
    ``text_``'s offsets."""
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
    return None, False


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


def derive(blocks: Iterable) -> list[Found]:
    """The labelled valuation dates of stored blocks (anything with ``block_index``, ``kind`` and ``text``), in block
    order. Pure: no database, no model. A date is taken when it follows a valuation label in the block, or, for a label
    alone in a cell, when the next block starts with a date."""
    rows = [(int(b.block_index), strip_bidi_controls(b.text or "")) for b in blocks
            if getattr(b, "kind", "paragraph") not in _TEXT_KINDS_SKIPPED]
    out: list[Found] = []
    seen: set[tuple[int, str, str]] = set()

    def take(block: int, p: Parsed) -> None:
        if (block, p.iso, p.precision) not in seen:
            seen.add((block, p.iso, p.precision))
            out.append(Found(block, p.iso, p.precision, p.written))

    for i, (index, body) in enumerate(rows):
        report = {p.span for p in report_dates(body)}
        for m in _VALUATION.finditer(body):
            p, only_gap = _date_after(body, m.end())
            if p is not None and p.span not in report:
                take(index, p)
            elif only_gap and i + 1 < len(rows):
                nxt_index, nxt = rows[i + 1]
                q, _ = _date_after(nxt, 0)
                if q is not None and not any(m2.start() < q.span[0] for m2 in _REPORT.finditer(nxt)):
                    take(nxt_index, q)
    return out


# --- the units: assignment and the unit's valuation date ---------------------------------------------------------------

def _agreeing(dates: Sequence[StatedDate]) -> StatedDate | None:
    """The finest date when every date is that one or a coarser form of it (a month of the day's month), else None."""
    finest = max(dates, key=lambda d: len(d.iso))
    return finest if all(finest.iso.startswith(d.iso) for d in dates) else None


def assign(found: Iterable[Found], units: Sequence) -> list[StatedDate]:
    """The dates with the unit that holds each block now and the status of that unit: `conflict` when its dates do not
    agree. A version with no units is one scope (`DOCUMENT_SCOPE`); a block outside every unit has no unit."""
    owned: list[tuple[Found, int | None]] = []
    for f in sorted(found, key=lambda x: (x.block_index, x.iso)):
        if not units:
            owner: int | None = DOCUMENT_SCOPE
        else:
            owner = next((u.unit_no for u in units if u.block_first <= f.block_index <= u.block_last), None)
        owned.append((f, owner))
    plain = [StatedDate(f.block_index, f.iso, f.precision, f.written, "stated", owner) for f, owner in owned]
    by_unit: dict[int, list[StatedDate]] = defaultdict(list)
    for d in plain:
        if d.unit_no is not None:
            by_unit[d.unit_no].append(d)
    conflicted = {no for no, ds in by_unit.items() if _agreeing(ds) is None}
    return [StatedDate(d.block_index, d.iso, d.precision, d.written, "conflict" if d.unit_no in conflicted else "stated",
                       d.unit_no) for d in plain]


def resolve_unit(dates: Sequence[StatedDate], unit_no: int | None) -> UnitValuation:
    """The valuation date of one unit from its own dates (the caller passes only the unit's)."""
    if not dates:
        return UnitValuation("none", None, (), unit_no)
    best = _agreeing(dates)
    if best is None:
        return UnitValuation("conflict", None, tuple(dates), unit_no)
    return UnitValuation("stated", best, tuple(dates), unit_no)


# --- the store -----------------------------------------------------------------------------------------------------------

_VISIBLE_SQL = ("SELECT v.id AS version_id, v.document_id FROM document_versions v JOIN documents d"
                f" ON d.id = v.document_id WHERE v.id = ANY(:v) AND {APPRAISALS_SQL}")
_ROWS_SQL = ("SELECT version_id, block_index, iso, precision, written FROM version_dates"
             " WHERE version_id = ANY(:v) AND dates_version = :dv ORDER BY version_id, block_index, iso")
_BLOCKS_SQL = ("SELECT block_index, kind, text FROM document_blocks WHERE version_id = :v AND kind <> 'image'"
               " ORDER BY block_index")
_INSERT = ("INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
           " written, status) VALUES (app_office(), :d, :v, :dv, :b, :i, :p, :w, :s)")


def _stored(conn: Connection, version_ids: Sequence[UUID]) -> dict[UUID, list[Found]]:
    """The version's rows of the current `DATES_VERSION` (a version with a marker row only maps to ``[]``)."""
    out: dict[UUID, list[Found]] = {}
    for r in conn.execute(text(_ROWS_SQL), {"v": list(version_ids), "dv": DATES_VERSION}):
        out.setdefault(r.version_id, [])
        if r.iso is not None and r.block_index != MARKER_BLOCK:
            out[r.version_id].append(Found(r.block_index, r.iso, r.precision, r.written or r.iso))
    return out


def _units_of(conn: Connection, version_id: UUID, cache: dict) -> tuple:
    key = ("units", version_id)
    if key not in cache:
        cache[key] = tuple(unit_store.ensure(conn, version_id))
    return cache[key]


def _derive_and_store(conn: Connection, version_id: UUID, document_id: UUID, cache: dict) -> list[Found]:
    """Derive the version's valuation dates from its stored blocks and store them (or the marker row) once: a second
    caller in the process waits for the first, and across processes the advisory lock makes the second writer find the
    first's rows. A transaction that cannot write still gets its dates."""
    blocks = conn.execute(text(_BLOCKS_SQL), {"v": version_id}).all()
    found = derive(blocks)
    assigned = assign(found, _units_of(conn, version_id, cache))
    lock = flight("dates", str(version_id), DATES_VERSION)
    try:
        with lock:
            try:
                with conn.begin_nested():
                    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                                 {"k": f"dates:{version_id}:{DATES_VERSION}"})
                    existing = _stored(conn, [version_id]).get(version_id)
                    if existing is not None:   # another process wrote them while this one waited for the lock
                        return existing
                    params = {"d": document_id, "v": version_id, "dv": DATES_VERSION}
                    if not assigned:
                        conn.execute(text(_INSERT), params | {"b": MARKER_BLOCK, "i": None, "p": None, "w": None,
                                                              "s": "stated"})
                    for d in assigned:
                        conn.execute(text(_INSERT), params | {"b": d.block_index, "i": d.iso, "p": d.precision,
                                                              "w": d.written, "s": d.status})
            except Exception:  # noqa: BLE001 - a read-only transaction or a vanished version: the dates still serve
                logger.info("dates of version %s not stored", version_id, exc_info=True)
            return found
    finally:
        landed("dates", str(version_id), DATES_VERSION, lock)


def valuation_dates(conn: Connection, version_ids: Iterable[UUID | str], *,
                    cache: dict | None = None) -> dict[UUID, tuple[StatedDate, ...]]:
    """The stated valuation dates of each version, with their unit and current status, derived and stored on first use.
    A version that is not an appraisal, does not exist or is not visible to the caller is not in the result. ``cache``
    is the turn's dict: a version already in it costs no query."""
    cache = {} if cache is None else cache
    ids = list(dict.fromkeys(UUID(str(v)) for v in version_ids))
    missing = [v for v in ids if ("dates", v) not in cache]
    if missing:
        visible = {r.version_id: r.document_id for r in conn.execute(text(_VISIBLE_SQL), {"v": missing})}
        stored = _stored(conn, list(visible))
        for v in missing:
            if v not in visible:
                cache[("dates", v)] = None
                continue
            found = stored.get(v)
            if found is None:
                found = _derive_and_store(conn, v, visible[v], cache)
            cache[("dates", v)] = tuple(assign(found, _units_of(conn, v, cache)))
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
    units = _units_of(conn, v, cache)
    if not units:
        unit_no: int | None = DOCUMENT_SCOPE
    else:
        unit_no = next((u.unit_no for u in units if u.block_first <= block_index <= u.block_last), None)
    if unit_no is None:
        return UnitValuation("none", None, (), None)
    return resolve_unit([d for d in dates if d.unit_no == unit_no], unit_no)


# --- forecast ---------------------------------------------------------------------------------------------------------

_P = r"(?<![א-ת])[ובהכלמש]{0,2}"
_FORECAST = re.compile(
    rf"{_P}(?:צפוי(?:ה|ים|ות)?|תחזית|יהיה|תהיה|יהיו|עתיד(?:י|יים|ית|יות)?)(?![א-ת])"
    rf"|{_P}הערכה\s+ל[\-־]?\s*(?:שנת\s*)?\d")   # "הערכה ל" is a forecast only before a date; "הערכה לשווי" is not
_CUT = {"year": 4, "month": 7, "day": 10}


def _later_than(iso: str, precision: str, anchor: date) -> bool:
    cut = _CUT.get(precision, 10)
    return iso[:cut] > anchor.isoformat()[:cut]


def is_forecast(sentence: str, as_of: AsOf | dict | None, anchor: date) -> bool:
    """True when the sentence is worded as a forecast, or the datum's date is later than the anchor date (by its
    precision; for a range, its end). The stance is not used (R7, KTD4)."""
    if _FORECAST.search(strip_bidi_controls(sentence or "")):
        return True
    if not as_of:
        return False
    return _later_than(as_of.get("end") or as_of["date"], as_of["precision"], anchor)


# --- as_of ----------------------------------------------------------------------------------------------------------------

def _build(p_iso: str, precision: str, end: str | None, provenance: str, locator: str, sentence: str,
           anchor: date) -> AsOf:
    partial = {"date": p_iso, "precision": precision, "end": end}
    return AsOf(date=p_iso, precision=precision, end=end, provenance=provenance,
                forecast=is_forecast(sentence, partial, anchor), locator=locator)


def resolve_as_of(text_: str, *, table_caption: str | None = None, stance: str | None = None,
                  stance_provenance: str | None = None, unit_valuation: UnitValuation | None = None,
                  anchor: date, locator: str = "text") -> AsOf | None:
    """The datum's date by the KTD4 order (see the module docstring), or None (unknown). ``text_`` is the datum's own row,
    cell or sentence; ``table_caption`` the caption or header of its table; ``stance`` and ``stance_provenance`` the
    value's stance and where its provenance says the stance came from; ``unit_valuation`` the valuation of the unit that
    holds the datum (`unit_valuation`)."""
    sentence = strip_bidi_controls(text_ or "")
    own = shared.find_all(sentence)
    if len({(p.iso, p.precision, p.end) for p in own}) > 1:
        return None   # ambiguous: two different dates in the row or sentence
    if own:
        return _build(own[0].iso, own[0].precision, own[0].end, SOURCE, locator, sentence, anchor)
    caption = shared.find_all(strip_bidi_controls(table_caption or ""))
    if len(caption) == 1 and caption[0].end is not None:
        c = caption[0]
        return _build(c.iso, c.precision, c.end, SOURCE_TABLE, "caption", sentence, anchor)
    if (stance == "adopted" and stance_provenance == "source" and unit_valuation is not None
            and unit_valuation.status == "stated" and unit_valuation.date is not None):
        d = unit_valuation.date
        return _build(d.iso, d.precision, None, UNIT_VALUATION, f"block {d.block_index}", sentence, anchor)
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
