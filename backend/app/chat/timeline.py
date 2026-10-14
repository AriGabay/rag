"""The server-built timeline of comparable dated values (round 11 U9: R9, R10, R11, R13; KTD8, KTD9; AE1, AE4).

An answer that cites one dated value of a changing datum says nothing of the earlier and later ones the turn also
registered. After verification the server takes every *comparability group* holding at least one value the answer rests
on (a V# or M#), fills it from **all** the values and measurements the turn registered (cited or not), and adds the
group as a block of ``answer.time.timeline`` when it holds two or more dated values. The block is the guarantee of the
order: the model is asked to open with the latest datum, the server asserts it. The block is part of the structured
payload and of a plain-text copy (``answer.time.text``); it is not added to ``answer.markdown``.

**The comparability key** (KTD9, R9) — the key ``recency_claim`` reads, and the one whose members ``coverage.conflicting_pairs``
compares:

- the *property group* of the appraisal unit the value is in (round 12 U6, KTD5): the turn's property graph
  (``property.PropertyGraph``), where appraisals are one property only through proving links (a shared parcel, or the same
  street, number and city) and the conversation's focus property is the compatible closure from the focus unit. Appraisals
  of one property in different documents share a group; two candidates (a city or a sub-parcel not stated) do not. A value
  whose unit states no identity keeps its own identity (its document and unit) and matches no other;
- the metric family (``resolve.family``), the unit, the periodicity, the area basis, the VAT basis, the scenario and the
  stance. A field counts only with source provenance (a measurement's record is source-stated): one that is not stated,
  or that the model only asserted, **never equals another** — such a value forms a group of its own. The stance is the
  exception (round 11 U16): "adopted", "unknown" and no stance are one class, the appraisal's own data (``stance_class``),
  so a value registered as adopted and one read from a header line are one timeline; a claim, a proposal, an estimate and
  "other" each stay a class of their own. A field that does not apply to the metric is left out of the key instead: the area basis of a total amount, the periodicity of a
  one-time value or of a datum that does not repeat, the VAT of a figure that is not money.

**Rows** are ordered by the turn's time mode with the ordering of ``tools.time_key`` (KTD6): ``recent`` — dated values not
later than the anchor and not forecast, newest first, then the later or forecast ones, then the unknown; ``explicit`` —
the requested time first, then the nearest, then the unknown (the rows outside it are marked); ``none`` — chronological,
oldest first, a forecast after the measured, the unknown last. A forecast is marked and never first. A row's marks:
``forecast``, ``conflict`` (another row of its group has a different amount and an overlapping period, or either has no date
unless it is history or a comparable — ``coverage.conflicting_pairs``, round 12 U11, R18; a group with such a conflict
gets a block even when it holds one dated row), ``outside_requested``
(an explicit mode, a dated row outside the requested time), ``uncertain`` (the value's or measurement's reading cannot be
presented as certain: ``tools.value_uncertain`` / ``measurement_uncertain``) and ``unknown_date``. A value cited twice from one document
(same date and amount) is one row.

**The property of a value** is its unit's group in the turn's property graph (the value's context number, else the file's
only subject group). That read goes through the caller's permissions (``tenant_tx``) and is made once per turn;
``set_identity`` replaces it (the tests of the pure parts). A read that fails leaves the value with its own identity: a
timeline is an addition to an answer, never the reason to lose it."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.answering.verify import numbers_in
from app.chat.dates import is_after_anchor
from app.chat.meaning import basis_key
from app.chat.timeframe import coverage_note
from app.chat.tools import (
    KIND_LABELS,
    Workspace,
    bounds,
    coverage_for,
    dated,
    measurement_uncertain,
    property_partition,
    read_coverage,
    time_key,
    value_uncertain,
)
from app.db import tenant_tx
from app.measurements.extract import PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
from app.platform.dates import shown_period

logger = logging.getLogger(__name__)

HEADING = "ציר זמן"
DATE_KINDS = {"valuation": "מועד קובע", "transaction": "מועד עסקה", "stated": "מועד שצוין", "range": "תקופה"}
MARKS = {"forecast": "תחזית", "conflict": "סתירה בין מקורות", "outside_requested": "מחוץ לזמן שביקשת",
         "uncertain": "קריאה לא ודאית", "unknown_date": "מועד לא ידוע"}
MSG_UNKNOWN_DATE = MARKS["unknown_date"]
MSG_NO_LOCATION = "נתון כמותי שחולץ"

MONEY_UNITS = frozenset({"ILS", "ILS_per_sqm"})
# the units a value per area or an area is stated in: its area basis (net, gross, ...) is part of what it measures
AREA_BASIS_UNITS = frozenset({"ILS_per_sqm", "sqm", "dunam"})
# the families whose amount repeats over a period: a rent per month and a rent per year are different timelines
PERIODIC_FAMILIES = frozenset({"rent", "management_fee", "income"})
UNSPECIFIC_KINDS = frozenset({"", "unknown", "other"})
# the stances that are the appraisal's own data (round 11 U16, KTD9): what it adopts, and what it states with no attribution
# (a header line, a table row — stance "unknown" or none). A claim, a proposal, an estimate and "other" are each their own class.
OWN_STANCES = frozenset({"adopted", "unknown", ""})


@dataclass(frozen=True)
class Identity:
    """The property a registered value is of: ``key`` (the group id in the turn's property graph, ``property.Group.id``;
    None when its unit is not in a group) and the short Hebrew description a group is labelled with."""

    key: str | None
    label: str = ""


@dataclass
class Datum:
    """One registered value (V#) or listed measurement (M#) as the timeline reads it."""

    ref: str
    index: int  # the order the turn registered it in (values, then measurements): the tie-break of equal dates
    document_id: str
    version_id: str
    title: str
    location: str
    unit_no: int | None  # the appraisal unit it is in, in a file holding several
    subject_unit: bool  # False: an appendix or a comparables part, never the asked property
    family: str
    unit: str
    fields: tuple | None  # the key's fields after the property; None: a field is unknown, so it matches no other
    written: str
    amount: Decimal | None
    as_of: dict | None
    role: str = ""  # a value's role, or a measurement's value role
    kind: str = ""
    period: str = ""
    vat: str = ""
    basis: str = ""
    uncertain: bool = False  # its reading cannot be presented as certain (review #3): shown, and marked as such

    @property
    def dated(self) -> bool:
        return bool(self.as_of and self.as_of.get("date"))

    @property
    def forecast(self) -> bool:
        return bool(self.as_of and self.as_of.get("forecast"))

    def date_key(self) -> tuple | None:
        return date_key(self.as_of)


# The identity of a datum's known date — (ISO date, precision, end) — or None when it has none (``tools.dated``: every
# ``as_of`` that holds a date holds its precision). Whether two values conflict is decided by their periods, not by this
# identity (``coverage.conflicting_pairs``, round 12 U11).
date_key = dated


# --- reading the turn's values -------------------------------------------------------------------------------------------

def family(kind: str) -> str:
    """The metric family of a kind (``resolve.family``; imported where used: ``resolve`` reaches ``coverage``, which reads this
    module)."""
    from app.chat.resolve import family as _family

    return _family(kind) if kind else ""


def stance_class(stance: str | None) -> str:
    """The stance as the comparability key reads it (KTD9): "adopted", "unknown" and no stance are one class — the appraisal's
    own data, so a value registered as adopted and one registered from a header line are one timeline — and any other
    stance (claim, proposal, estimate, other) is its own class. ``coverage.conflicting`` reads the same class."""
    stance = (stance or "").strip()
    return "adopted" if stance in OWN_STANCES else stance


def _stated(provenance: dict, field: str) -> bool:
    return provenance.get(field) == "source"


def _fields(family_: str, unit: str, period: str, vat: str, basis: str, scenario: str, stance: str,
            provenance: Callable[[str], bool]) -> tuple | None:
    """The key fields after the property (see the module doc), or None when a field that applies is unknown. ``provenance``:
    whether the source states a field."""
    if family_ in UNSPECIFIC_KINDS or not provenance("kind") or not unit or unit == "other" or not provenance("unit"):
        return None
    out: list = [family_, unit]
    if period == "one_time":
        pass  # a one-time value has no periodicity
    elif period in ("month", "year") and provenance("period"):
        out.append(("period", period))
    elif family_ in PERIODIC_FAMILIES:
        return None
    if unit in MONEY_UNITS:
        if vat == "not_applicable":
            pass
        elif vat in ("included", "excluded") and provenance("vat"):
            out.append(("vat", vat))
        else:
            return None
    if unit in AREA_BASIS_UNITS:
        if not (basis or "").strip() or not provenance("area_basis"):
            return None
        out.append(("basis", basis_key(basis)))
    # a scenario counts only when the source states it; one the model asserted — on the real model often the datum's
    # own date written as a label ("המועד הקובע 20/01/2025") — is no scenario: it neither splits a history nor drops the
    # value from every timeline (round 11, found in the real-model run of the rent sequence)
    scenario = " ".join((scenario or "").split()).casefold() if provenance("scenario") else ""
    out.append(("scenario", scenario))
    out.append(("stance", stance_class(stance if provenance("stance") else None)))
    return tuple(out)


def _context(ctx: dict | None) -> tuple[int | None, bool]:
    ctx = ctx or {}
    return ctx.get("number"), bool(ctx.get("subject", True))


def datums(ws: Workspace) -> list[Datum]:
    """Every value and measurement the turn registered, as the timeline reads it (values first, in registration order)."""
    out: list[Datum] = []
    for i, v in ws.values.items():
        prov = v.provenance or {}
        number, subject = _context(v.context)
        amount = v.value * (v.scale or 1) if v.value is not None else None
        fam = family(v.kind)
        out.append(Datum(
            i, len(out), str(v.document_id), str(v.version_id), v.title, v.location, number, subject, fam, v.unit,
            _fields(fam, v.unit, v.period, v.vat, v.area_basis or "", v.scenario or "", v.stance or "",
                    lambda f, p=prov: _stated(p, f)),
            v.written, amount, v.as_of, v.role or "", v.kind, v.period, v.vat, v.area_basis or "",
            value_uncertain(ws, i)))
    for i, m in ws.measurements.items():
        r = m.row
        number, subject = _context(m.context)
        kind = getattr(r, "metric_kind", "") or ""
        fam = family(kind)
        amount = getattr(r, "value", None)
        if amount is None:
            nums = sorted(numbers_in(r.value_text or ""))
            amount = Decimal(nums[0].replace(",", "")) if len(nums) == 1 and nums[0].replace(",", "").replace(".", "", 1).isdigit() else None
        out.append(Datum(
            i, len(out), str(m.document_id), str(m.version_id), m.title, _measurement_location(r), number, subject,
            fam, r.unit or "",
            _fields(fam, r.unit or "", r.period or "", r.vat or "", r.area_basis or "",
                    getattr(r, "scenario", "") or "", getattr(r, "stance", "") or "",
                    lambda f, r=r: _record_states(r, f)),
            r.value_text or "", Decimal(str(amount)) if amount is not None else None, m.as_of,
            getattr(r, "value_role", "") or "", kind, r.period or "", r.vat or "", r.area_basis or "",
            measurement_uncertain(r)))
    return out


# the key's fields -> the attribute of a stored measurement that holds each
_RECORD_FIELDS = {"kind": "metric_kind", "unit": "unit", "period": "period", "vat": "vat", "area_basis": "area_basis",
                  "stance": "stance", "scenario": "scenario"}


def _record_states(row, field: str) -> bool:
    """A stored measurement's field is the source's: the extraction validated it against the quote. An unknown or empty field
    is not stated (the key reads it as unknown); the stance and the scenario count when the row holds them."""
    attribute = _RECORD_FIELDS.get(field)
    value = getattr(row, attribute, "") if attribute else None
    return bool(value) and value not in ("unknown", "none")


def _measurement_location(row) -> str:
    section = getattr(row, "section", None)
    return f'סעיף "{section}"' if section else MSG_NO_LOCATION


# --- the property of a value ---------------------------------------------------------------------------------------------

class _UnitIdentity:
    """The property of a datum from the turn's property graph (round 12 U6, KTD5; ``tools.property_partition``): a group id
    and a label. For the conversation's focus property the group is the inventory's gathered set; the other units form a
    group only when connected by gathered links and pairwise compatible, so two candidates stay in separate groups and the
    grouping depends on neither the seed nor the document order. The graph is read once per turn, under the caller's
    permissions. A datum of a unit the graph does not hold (an appendix, a comparables part, a unit with no identity read, a
    file of units in several groups) keeps its own identity."""

    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self.partition = None  # the turn's partition; False: it could not be built

    def _partition(self):
        if self.partition is None:
            try:
                with tenant_tx(self.ws.ctx) as conn:
                    self.partition = property_partition(self.ws, conn)
            except Exception:  # noqa: BLE001 - a timeline is an addition: without its graph a value keeps its own identity
                logger.warning("the property graph could not be read for the timeline", exc_info=True)
                self.partition = False
        return self.partition

    def __call__(self, d: Datum) -> Identity:
        partition = self._partition()
        if not partition:
            return Identity(None)
        if d.unit_no is not None:
            group = partition.group_of_unit(d.version_id, d.unit_no) if d.subject_unit else None
        else:  # a datum of the file as a whole: the file's subject units must all be one property
            held = partition.groups_of_version(d.version_id)
            group = held[0] if len(held) == 1 else None
        return Identity(group.id, group.label) if group is not None else Identity(None)


def _own(d: Datum) -> Identity:
    return Identity(None)


_ATTR = "_timeline_identity"


def property_key(ws: Workspace, ref: str, document_id, version_id, context: dict | None) -> str | None:
    """The property group id of a registered value in the turn's graph — what ``coverage.conflicting`` takes for "the same
    property" (round 12 U11, KTD13) — or None when the turn has no way to read one (no tenant context and no stand-in given:
    the tests of the pure parts) or the value's unit is in no group. The same read as the timeline's (``identity_of``)."""
    if getattr(ws, "ctx", None) is None and _ATTR not in vars(ws):
        return None
    number, subject = _context(context)
    probe = Datum(ref, 0, str(document_id), str(version_id), "", "", number, subject, "", "", None, "", None, None)
    return identity_of(ws)(probe).key


def set_identity(ws: Workspace, fn: Callable[[Datum], Identity]) -> None:
    """Replace how the property of a datum is read (the tests of the pure parts give a stand-in)."""
    ws.__dict__[_ATTR] = fn


def identity_of(ws: Workspace) -> Callable[[Datum], Identity]:
    fn = ws.__dict__.get(_ATTR)
    if fn is None:
        fn = _UnitIdentity(ws) if getattr(ws, "ctx", None) is not None else _own
        ws.__dict__[_ATTR] = fn
    return fn


def _full_key(d: Datum, identity: Callable[[Datum], Identity], cache: dict) -> tuple:
    """The comparability key of a datum: its property and fields, or — when a field is unknown or the property is not
    stated — a key only it has (a document's own identity for the property, the datum itself for an unknown field)."""
    if d.fields is None:
        return ("unknown", d.ref)
    who = cache.get(d.ref)
    if who is None:
        who = cache[d.ref] = identity(d)
    prop = ("property", who.key) if who.key else ("own", d.document_id, d.unit_no if d.unit_no is not None else 0)
    return (prop, *d.fields)


def groups(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
           ) -> list[list[Datum]]:
    """The comparability groups that hold at least one of the ``cited`` ids (V#/M#), each filled from everything the turn
    registered, in registration order; the groups in the order of their first cited datum."""
    if not cited:
        return []
    identity = identity or identity_of(ws)
    everything = datums(ws)
    wanted = [d for d in everything if d.ref in cited]
    cache: dict = {}
    alone = {f for f, n in Counter(d.fields for d in everything if d.fields is not None).items() if n == 1}

    def key_of(d: Datum) -> tuple:
        # a datum whose fields no other datum has is alone in its group whatever its property is: the group is dropped by
        # every reader (``build``, ``later_than``: no second dated value, no later one), so its property is never read
        if d.fields in alone:
            return ("alone", d.ref)
        return _full_key(d, identity, cache)

    found: dict[tuple, list[Datum]] = {}
    for d in wanted:
        found.setdefault(key_of(d), [])
    # only a datum whose other fields match a cited one's could share its key: the rest never has its property read
    fields = {d.fields for d in wanted if d.fields is not None}
    for d in everything:
        if d.fields is None:
            if ("unknown", d.ref) in found:
                found[("unknown", d.ref)].append(d)
        elif d.fields in fields and (key := key_of(d)) in found:
            found[key].append(d)
    return list(found.values())


# --- order, marks, rows ---------------------------------------------------------------------------------------------------

def _order(ws: Workspace, d: Datum) -> tuple:
    """The sort key of a datum under the turn's time mode: ``tools.time_key`` (KTD6), with the chronological order of mode
    ``none`` (oldest first; a forecast after the measured; the unknown last)."""
    when = dated(d.as_of)
    if ws.time.mode == "none":
        if when is None:
            return (2, 0)
        iso, precision, end = when
        return (1 if d.forecast else 0, bounds(end or iso, precision)[0].toordinal())
    return time_key(ws.time, when, d.forecast)


def _dedupe(members: list[Datum]) -> list[Datum]:
    """One row for a datum registered twice from one document (same date and amount)."""
    seen: set[tuple] = set()
    out = []
    for d in members:
        k = (d.document_id, d.date_key(), d.amount if d.amount is not None else d.written)
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def _date_kind(d: Datum) -> str | None:
    if not d.dated:
        return None
    how = d.as_of.get("provenance")
    if how == "unit_valuation":
        return "valuation"
    if how == "source_table":
        return "range"
    return "transaction" if d.role in ("comparison", "comparable_transaction", "actual_contract") else "stated"


def _outside(ws: Workspace, d: Datum) -> bool:
    return ws.time.mode == "explicit" and ws.time.requested is not None and d.dated and _order(ws, d)[0] != 0


def _row(ws: Workspace, d: Datum, conflicts: set) -> dict:
    marks = []
    if d.forecast:
        marks.append("forecast")
    if d.ref in conflicts:
        marks.append("conflict")
    if _outside(ws, d):
        marks.append("outside_requested")
    if d.uncertain:
        marks.append("uncertain")
    if not d.dated:
        marks.append("unknown_date")
    return {"date": d.as_of["date"] if d.dated else None, "precision": d.as_of.get("precision") if d.dated else None,
            "end": d.as_of.get("end") if d.dated else None, "date_kind": _date_kind(d), "value_text": d.written,
            "unit": UNIT_LABELS.get(d.unit) or None, "sid": d.ref,
            "source": {"document_id": d.document_id, "title": d.title, "location": d.location or None},
            "marks": marks}


def _label(members: list[Datum], who: Identity) -> str:
    first = members[0]
    parts = [KIND_LABELS.get(first.kind, first.kind) if first.kind else ""]
    if first.period in ("month", "year"):
        parts.append(PERIOD_LABELS[first.period])
    if first.vat in ("included", "excluded") and first.unit in MONEY_UNITS:
        parts.append(VAT_LABELS[first.vat])
    if first.basis and first.unit in AREA_BASIS_UNITS:
        parts.append(f"בסיס שטח: {first.basis}")
    what = " ".join(p for p in parts if p)
    where = who.label or first.title
    return f"{what} — {where}" if what and where else what or where


def build(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None) -> list[dict]:
    """The timeline: a ``{"label", "rows"}`` block for every comparability group of the ``cited`` ids that holds two or more
    dated values, or a conflict that involves a value with no date (round 12 U11, KTD13: the mark must be visible), its rows
    ordered by the turn's time mode and marked (see the module doc). A row is marked ``conflict`` when
    ``coverage.conflicting_pairs`` pairs it with another row of its group."""
    from app.chat.coverage import Comparable, conflicting_pairs

    identity = identity or identity_of(ws)
    blocks = []
    for members in groups(ws, cited, identity):
        members = _dedupe(members)
        pairs = conflicting_pairs([Comparable(None, d.amount if d.amount is not None else d.written, d.as_of)
                                   for d in members])
        conflicts = {members[i].ref for pair in pairs for i in pair}
        if sum(1 for d in members if d.dated) < 2 and not any(not d.dated and d.ref in conflicts for d in members):
            continue
        ordered = sorted(members, key=lambda d: (_order(ws, d), d.index))
        blocks.append({"label": _label(ordered, identity(ordered[0])),
                       "rows": [_row(ws, d, conflicts) for d in ordered]})
    return blocks


def row_ids(blocks: Iterable[dict]) -> set[str]:
    """The V# and M# ids the rows of a timeline stand for."""
    return {r["sid"] for b in blocks for r in b["rows"]}


# --- the plain-text copy --------------------------------------------------------------------------------------------------

def shown(row: dict | None) -> str:
    """A date (a row, or an ``as_of``) as the reader sees it: DD/MM/YYYY, MM/YYYY, YYYY or a range of them; "מועד לא ידוע"
    when it has none."""
    if not row or not row.get("date"):
        return MSG_UNKNOWN_DATE
    return shown_period(row["date"], row["precision"], row.get("end"))


def plain_text(blocks: list[dict]) -> str:
    """The whole timeline as plain text: the heading, then for each group its label and one line per row — date, kind of
    date, value, source title, marks. "" when there is no timeline."""
    if not blocks:
        return ""
    lines = [HEADING]
    for b in blocks:
        lines.append(b["label"])
        for r in b["rows"]:
            value = f"{r['value_text']} {r['unit']}" if r["unit"] else r["value_text"]
            cells = [shown(r), DATE_KINDS.get(r["date_kind"]), value, r["source"]["title"],
                     ", ".join(MARKS[m] for m in r["marks"] if not (m == "unknown_date" and not r["date"]))]
            lines.append("- " + " · ".join(c for c in cells if c))
    return "\n".join(lines)


def sids_behind(ws: Workspace, ids: Iterable[str]) -> set[str]:
    """The passages (S#) behind cited ids (round 12 U8, KTD7): a cited S# itself; for a V# the passage it was verified in; for
    a V# or M# every passage of the turn read from the same document version (a measurement names no passage)."""
    sids: set[str] = set()
    versions = set()
    for i in ids:
        if i in ws.sources:
            sids.add(i)
        elif i in ws.values:
            sids.add(ws.values[i].source_id)
            versions.add(str(ws.values[i].version_id))
        elif i in ws.measurements:
            versions.add(str(ws.measurements[i].version_id))
    return sids | {s.sid for s in ws.sources.values() if s.version_id is not None and str(s.version_id) in versions}


def coverage_of(ws: Workspace, ids: Iterable[str]) -> dict | None:
    """The coverage record of the data the ids cite (KTD7): that of the time-ranked calls whose results they come from, else
    of the turn's calls together; None when the turn made no time-ranked call (off mode, R14). A timed turn that made none
    but read the sources it cites has the reads-only record (``read_coverage``, round 12 U12b)."""
    record = coverage_for(ws, sids_behind(ws, ids))
    return record if record is not None or ws.time_calls else read_coverage(ws, ids)


def partial_coverage(ws: Workspace, ids: Iterable[str]) -> dict | None:
    """The record of a time-ranked call that produced the data ``ids`` cite, when that record is not complete (a partial
    coverage or a property not identified); None for ids no time-ranked call produced, and for a complete record. The
    claim check reads it: a "latest" claim about data of a call that did not check every appraisal needs the qualified
    phrase (``verify._recency_claim``)."""
    if not ws.time_calls:  # no time-ranked search: the reads-only record, when the cited sources identify the property
        record = read_coverage(ws, ids)
        return None if record is None or record.get("complete") else record
    behind = sids_behind(ws, ids)
    if not any(behind & set(c.sids) for c in ws.time_calls):
        return None
    record = coverage_for(ws, behind)
    return None if record is None or record.get("complete") else record


def payload(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None,
            coverage: dict | None = None) -> dict:
    """``answer.time`` (the round-11 contract): the mode and switch the answer was produced with, the requested time, the
    timeline and its plain-text copy (None without a timeline). Round 12 U8 (R12, KTD8): ``coverage`` — the coverage record
    of the data the answer cites (``coverage_of``) — joins it as ``coverage`` with the server-written ``note``, and the copy
    text ends with the note whether or not the timeline has rows. Without a record (off mode, R14) there is no ``coverage``
    key, and an old message without one reads as "not recorded"."""
    blocks = build(ws, cited, identity) if cited else []
    requested = ws.time.requested
    out = {"mode": ws.time.mode, "prefer_recent": ws.time.prefer_recent,
           "requested": requested.as_dict() if requested is not None else None, "timeline": blocks,
           "text": plain_text(blocks) or None}
    if coverage is not None:
        note = coverage_note(coverage)
        out["coverage"] = coverage | {"note": note}
        out["text"] = "\n\n".join(x for x in (out["text"], note) if x) or None
    return out


# --- the later comparable value of a cited one ---------------------------------------------------------------------------------

def _end(d: Datum) -> date:
    """The last day of a dated datum's period (a range: of its end)."""
    return bounds(d.as_of.get("end") or d.as_of["date"], d.as_of["precision"])[1]


def _start(d: Datum) -> date:
    """The first day of a dated datum's period."""
    return bounds(d.as_of["date"], d.as_of["precision"])[0]


def _measured_before_anchor(ws: Workspace, d: Datum) -> bool:
    a = d.as_of
    return d.dated and not d.forecast and not is_after_anchor(a.get("end") or a["date"], a["precision"], ws.time.anchor)


def later_than(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
               ) -> list[tuple[Datum, Datum]]:
    """For every comparability group of the cited ids: when the latest-dated cited value is dated before a comparable value
    the turn registered — one dated, measured (not a forecast, not later than the anchor) and starting after the cited
    period ends — the pair (the latest cited value, the latest registered one). A group where a cited value is the latest
    gives nothing."""
    if not cited:
        return []
    identity = identity or identity_of(ws)
    out = []
    for members in groups(ws, cited, identity):  # groups holds only the datums (V#, M#) among the cited ids
        mine = [d for d in members if d.ref in cited and _measured_before_anchor(ws, d)]
        if not mine:
            continue
        newest = max(mine, key=lambda d: (_end(d), -d.index))
        later = [d for d in members if _measured_before_anchor(ws, d) and _start(d) > _end(newest)]
        if later:
            out.append((newest, max(later, key=lambda d: (_end(d), -d.index))))
    return out
