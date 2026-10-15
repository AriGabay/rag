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

**The datum** (round 13 U4, KTD3, KTD4, KTD5): the key above is the *base key* — it has no facet, because the facet rule
(``datum.relation``) is not transitive. Inside a base key, two data are decided ``same``, ``different`` or ``undecided`` by that one
rule, and the ``same`` pairs are joined (union-find) into *classes*; a class is one datum, and every reader takes it as such: the
timeline blocks, the conflict pairs (``coverage.conflicting_pairs`` inside a class only), ``_dedupe``, the ``alone`` shortcut,
``later_than`` and ``coverage.conflicting``. Between two classes of one base key, a pair that is ``undecided`` whose amounts
differ and whose periods overlap is marked ``undecided`` (``undecided_with`` names the counterparts) on the row(s) whose facet is
unresolved — none or not read, or an empty core — and on both when both are resolved own facets (disjoint across units), unless the two
classes are decided different by any other pair across them (the class relation, as for recency). The unit is
normalized (a percent and a ratio of a coefficient or a proportion are one unit) and the amounts are compared in one canonical
form (``datum.canonical_amount``).

**The components table** (round 13 U6, KTD7, R8-R10): ``answer.time.breakdown`` holds one block for every appraisal unit and date
where two or more classes of one base key meet (their pairs are ``different`` or ``undecided``, never ``same``): parallel
components of one appraisal at one date, each row with its own written facet. A row belongs to exactly one block: the rows of a
unit and a date that has a components block are only there, and a class's timeline block holds its other dated rows (it is still
shown with a single one). A timeline label names the facet of the class's first row in server order; a row whose source states no
facet shows ``MSG_NO_FACET``. A row marked ``conflict`` names the datum it conflicts with (``conflict_with``, the sids of every
conflict pair it is in, read from its class), as ``undecided`` does: the counterpart may be a row of another block.

**Rows** are ordered by the turn's time mode with the ordering of ``tools.time_key`` (KTD6): ``recent`` — dated values not
later than the anchor and not forecast, newest first, then the later or forecast ones, then the unknown; ``explicit`` —
the requested time first, then the nearest, then the unknown (the rows outside it are marked); ``none`` — chronological,
oldest first, a forecast after the measured, the unknown last. A forecast is marked and never first. A row's marks:
``forecast``, ``conflict`` (another row of its group has a different amount and an overlapping period, or either has no date
unless it is history or a comparable — ``coverage.conflicting_pairs``, round 12 U11, R18; a group with such a conflict
gets a block even when it holds one dated row), ``undecided`` (a row of another class of its base key, whose facet could not
be compared with it, has a different amount and an overlapping period: neither a conflict nor agreement), ``outside_requested``
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
from dataclasses import dataclass, field
from datetime import date

from app.chat import datum as D
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
    read_places,
    settle_coverage,
    time_key,
    value_uncertain,
)
from app.db import tenant_tx
from app.measurements.extract import PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
from app.platform.dates import shown_period

logger = logging.getLogger(__name__)

HEADING = "ציר זמן"
BREAKDOWN_HEADING = "רכיבי השומה"  # not "רכיבי הבקשה": that is the request's components (KTD7)
MSG_NO_FACET = "רכיב לא צוין במקור"
DATE_KINDS = {"valuation": "מועד קובע", "transaction": "מועד עסקה", "stated": "מועד שצוין", "range": "תקופה"}
MARKS = {"forecast": "תחזית", "conflict": "סתירה בין מקורות", "undecided": "השוואה לא הוכרעה",
         "outside_requested": "מחוץ לזמן שביקשת", "uncertain": "קריאה לא ודאית", "unknown_date": "מועד לא ידוע"}
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
    amount: object  # the canonical amount (``datum.canonical_amount``): a Decimal, (low, high) of a range, or None
    as_of: dict | None
    role: str = ""  # a value's role, or a measurement's value role
    kind: str = ""
    period: str = ""
    vat: str = ""
    basis: str = ""
    uncertain: bool = False  # its reading cannot be presented as certain (review #3): shown, and marked as such
    # what the datum decision reads (round 13 U4, KTD3; ``datum.Site``): the source's facet of the datum, the appraisal unit
    # it is in, its table ((version, table index) of a cell) and the section path of its block
    facet: dict | None = None
    appraisal: tuple | None = None
    table: tuple | None = None
    section: str = ""

    @property
    def dated(self) -> bool:
        return bool(self.as_of and self.as_of.get("date"))

    @property
    def forecast(self) -> bool:
        return bool(self.as_of and self.as_of.get("forecast"))

    @property
    def comparable(self) -> object:
        """What the conflict rule compares: the canonical amount, else the words of a datum that holds no number."""
        return self.amount if self.amount is not None else " ".join((self.written or "").split())

    def date_key(self) -> tuple | None:
        return date_key(self.as_of)

    def as_comparable(self):
        """The datum as ``coverage.conflicting_pairs`` and ``clashing`` compare it: its comparable amount and its period."""
        from app.chat.coverage import Comparable

        return Comparable(None, self.comparable, self.as_of)


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


def unit_key(family_: str, unit: str) -> str:
    """The unit as the key reads it (KTD5): a coefficient or a proportion written as a percent ("80%") and as a fraction (0.8)
    is one datum, so their units meet; any other unit is its own."""
    return "fraction" if unit in ("percent", "ratio") and D.needs_facet(family_) else unit


def restates(scenario: str, family_: str, facet: dict | None) -> bool:
    """Whether a scenario only restates the datum's own facet (round 13 follow-up): the model often registers the component
    ("תזרים דמי שכירות ממשרדים") as the scenario. Its core (``datum.core``) is then equal to, or a subset of, the facet's core, and
    the facet already carries it, so it is no part of the key; a scenario that names something the facet does not (a project
    stage) still splits. A scenario with no content word of its own restates nothing."""
    words = D.core(scenario, family_)
    return bool(words) and words <= D.core_of(D.Site(family_, facet))


def _fields(family_: str, unit: str, period: str, vat: str, basis: str, scenario: str, stance: str,
            provenance: Callable[[str], bool], facet: dict | None = None) -> tuple | None:
    """The key fields after the property (see the module doc), or None when a field that applies is unknown. ``provenance``:
    whether the source states a field; ``facet``: the datum's facet, which a scenario restating it does not add to the key."""
    if family_ in UNSPECIFIC_KINDS or not provenance("kind") or not unit or unit == "other" or not provenance("unit"):
        return None
    out: list = [family_, unit_key(family_, unit)]
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
    if scenario and restates(scenario, family_, facet):
        scenario = ""  # the facet carries it (round 13 follow-up)
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
        fam = family(v.kind)
        out.append(Datum(
            i, len(out), str(v.document_id), str(v.version_id), v.title, v.location, number, subject, fam, v.unit,
            _fields(fam, v.unit, v.period, v.vat, v.area_basis or "", v.scenario or "", v.stance or "",
                    lambda f, p=prov: _stated(p, f), v.facet),
            v.written, D.canonical_amount(v.value, scale=v.scale, unit=v.unit, family=fam), v.as_of, v.role or "",
            v.kind, v.period, v.vat, v.area_basis or "", value_uncertain(ws, i),
            v.facet, (str(v.version_id), number), tuple(v.table) if v.table else None, v.section or ""))
    for i, m in ws.measurements.items():
        r = m.row
        number, subject = _context(m.context)
        kind = getattr(r, "metric_kind", "") or ""
        fam = family(kind)
        table_index = getattr(r, "table_index", None)
        out.append(Datum(
            i, len(out), str(m.document_id), str(m.version_id), m.title, _measurement_location(r), number, subject,
            fam, r.unit or "",
            _fields(fam, r.unit or "", r.period or "", r.vat or "", r.area_basis or "",
                    getattr(r, "scenario", "") or "", getattr(r, "stance", "") or "",
                    lambda f, r=r: _record_states(r, f), m.facet),
            r.value_text or "",
            D.canonical_measurement(getattr(r, "value", None), r.value_text or "", unit=r.unit or "", family=fam),
            m.as_of, getattr(r, "value_role", "") or "", kind, r.period or "", r.vat or "", r.area_basis or "",
            measurement_uncertain(r), m.facet, (str(m.version_id), number),
            (str(m.version_id), table_index) if table_index is not None else None, getattr(r, "section", "") or ""))
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


@dataclass
class Bucket:
    """The data of one base key (``_full_key``), the classes the datum decision joins them into (round 13 U4, KTD3, KTD4) and the
    decision of every pair. ``classes`` holds each class in registration order (a class is one datum); ``relation`` and
    ``class_relation`` answer for two data and two classes. U6 reads the classes of a unit and a date from here."""

    key: tuple
    members: list[Datum]
    classes: list[list[Datum]]
    pairs: dict[tuple[str, str], str] = field(default_factory=dict)  # (ref, ref) in registration order -> the decision

    def __post_init__(self) -> None:
        self._class = {d.ref: n for n, c in enumerate(self.classes) for d in c}
        self._relations: dict[tuple[int, int], str] = {}  # (class number, class number) -> ``class_relation``

    def class_of(self, d: Datum) -> int:
        return self._class[d.ref]

    def relation(self, a: Datum, b: Datum) -> str:
        """The decision for two data of this key: ``same`` when they are of one class (even where the pair itself was not
        decided so: the classes close the relation), else the pair's own decision."""
        if self.class_of(a) == self.class_of(b):
            return D.SAME
        return self.pairs[(a.ref, b.ref) if a.index < b.index else (b.ref, a.ref)]

    def class_relation(self, x: list[Datum], y: list[Datum]) -> str:
        """The decision for two classes: ``different`` as soon as one pair across them is decided different (two data that are
        each one datum with their class-mates, and two of them parallel components, make the classes two data), else
        ``undecided``. Two classes are never ``same``. Kept per pair of classes."""
        key = (self.class_of(x[0]), self.class_of(y[0]))
        found = self._relations.get(key)
        if found is None:
            found = self._relations[key] = D.DIFFERENT if any(
                self.relation(a, b) == D.DIFFERENT for a in x for b in y) else D.UNDECIDED
        return found

    def conflicts(self, keep: Callable[[Datum], bool] | None = None) -> list[tuple[Datum, Datum]]:
        """The pairs inside a class whose amounts differ and whose periods overlap (``coverage.conflicting_pairs``): the one
        conflict rule. ``keep``: only the data it accepts are compared (a class is still the whole class)."""
        from app.chat.coverage import conflicting_pairs

        out = []
        for cls in self.classes:
            seen = [d for d in cls if keep is None or keep(d)]
            pairs = conflicting_pairs([d.as_comparable() for d in seen])
            out.extend((seen[i], seen[j]) for i, j in pairs)
        return out

    def undecided(self) -> list[tuple[Datum, Datum]]:
        """The pairs across two classes that could not be compared whose amounts differ and whose periods overlap (the same
        period rule as a conflict): neither a conflict nor agreement (R6). Decided at class level, as recency is (round 13 U6,
        KTD4): when any pair across two classes is ``different`` the classes are two components — a member of one is the
        same datum as its class-mates, which are structurally different from the other class — so none of their members is
        marked; a pair is marked only when the class relation is ``undecided`` and the pair itself is."""
        from app.chat.coverage import clashing

        out = []
        for n, a in enumerate(self.members):
            for b in self.members[n + 1:]:
                if self.class_of(a) == self.class_of(b) or self.pairs[(a.ref, b.ref)] != D.UNDECIDED:
                    continue
                if self.class_relation(self.classes[self.class_of(a)], self.classes[self.class_of(b)]) != D.UNDECIDED:
                    continue
                if clashing(a.as_comparable(), b.as_comparable()):
                    out.append((a, b))
        return out

    def undecided_with(self) -> dict[str, list[str]]:
        """``undecided`` by datum: the ids of the data each could not be compared with (the counterpart's sid, R6). The mark is
        asymmetric (round 13 U6, KTD4): a pair marks only the side(s) whose facet is unresolved (``unresolved``); when both sides
        are resolved own facets (disjoint across units) both are marked. A datum with a stated facet whose only undecided
        partners are unresolved carries no mark."""
        out: dict[str, list[str]] = {}
        open_: dict[str, bool] = {}  # a datum -> ``unresolved``, read once

        def is_open(d: Datum) -> bool:
            if d.ref not in open_:
                open_[d.ref] = unresolved(d)
            return open_[d.ref]

        for a, b in self.undecided():
            both = not is_open(a) and not is_open(b)
            if both or is_open(a):
                out.setdefault(a.ref, []).append(b.ref)
            if both or is_open(b):
                out.setdefault(b.ref, []).append(a.ref)
        return out


def unresolved(d: Datum) -> bool:
    """Whether a datum's facet is unresolved (round 13 U6): the source states none or it was not read, or what it states reduces
    to generic words (an empty core). Such a datum is the one an undecided pair is marked on."""
    return not D.core_of(d)


def _classify(key: tuple, members: list[Datum]) -> Bucket:
    """Decide every pair of a base key (``datum.relation``) and join the ``same`` ones into classes (union-find). A datum is
    the headline datum of its appraisal unit when no other value of the key — counted by amount, so a value taken from the
    text and the stored measurement of the same figure are one — is in that unit."""
    amounts: dict[tuple, set] = {}
    for d in members:
        amounts.setdefault(d.appraisal, set()).add(d.comparable)
    count = {d.ref: len(amounts[d.appraisal]) for d in members}
    parent = list(range(len(members)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs: dict[tuple[str, str], str] = {}
    for i, a in enumerate(members):
        for j in range(i + 1, len(members)):
            b = members[j]
            decision = pairs[(a.ref, b.ref)] = D.relation(a, b, count[a.ref], count[b.ref])
            if decision == D.SAME:
                parent[root(j)] = root(i)
    by_root: dict[int, list[Datum]] = {}
    for i, d in enumerate(members):
        by_root.setdefault(root(i), []).append(d)
    return Bucket(key, members, sorted(by_root.values(), key=lambda c: c[0].index), pairs)


def buckets(ws: Workspace, cited: Collection[str] | None, identity: Callable[[Datum], Identity] | None = None,
            everything: list[Datum] | None = None) -> list[Bucket]:
    """The base-key buckets that hold at least one of the ``cited`` ids (V#/M#; None: every datum), each filled from everything
    the turn registered, in registration order, with its classes and decisions; the buckets in the order of their first cited
    datum. ``everything``: the turn's ``datums`` when the caller already has them."""
    if cited is not None and not cited:
        return []
    identity = identity or identity_of(ws)
    everything = datums(ws) if everything is None else everything
    wanted = everything if cited is None else [d for d in everything if d.ref in cited]
    cache: dict = {}
    alone = {f for f, n in Counter(d.fields for d in everything if d.fields is not None).items() if n == 1}

    def key_of(d: Datum) -> tuple:
        # a datum whose fields no other datum has is alone in its bucket whatever its property is: the bucket is dropped by
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
    return [_classify(k, members) for k, members in found.items()]


def groups(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
           ) -> list[list[Datum]]:
    """The datum classes that hold at least one of the ``cited`` ids (V#/M#), each filled from everything the turn registered
    (in registration order): one datum's history. A base key may hold several classes (parallel or undecided components)."""
    if not cited:
        return []
    return [c for b in buckets(ws, cited, identity) for c in b.classes if any(d.ref in cited for d in c)]


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


def _dedupe(members: list[Datum], cited: Collection[str] = ()) -> list[Datum]:
    """One row for a datum registered twice from one document (same date, amount and facet: round 13 U4, R10 — the same number
    of two components is two rows). Of the duplicates the first cited one is kept (the model often registers a value again after
    a note and cites the second: the answer's own id must be the row, round 13 follow-up), else the first registered; the rows
    keep the order of their first registration."""
    kept: dict[tuple, Datum] = {}
    for d in members:
        k = (d.document_id, d.date_key(), d.comparable, D.core_of(d))
        if k not in kept:
            kept[k] = d
        elif d.ref in cited and kept[k].ref not in cited:
            kept[k] = d
    return list(kept.values())


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


def _row(ws: Workspace, d: Datum, conflicts: dict[str, list[str]], undecided: dict[str, list[str]] | None = None) -> dict:
    """A timeline row. ``conflicts``: the counterparts' sids of every conflict pair a datum is in; ``undecided``: the same for
    the pairs that could not be compared."""
    marks = []
    if d.forecast:
        marks.append("forecast")
    if d.ref in conflicts:
        marks.append("conflict")
    if undecided and d.ref in undecided:
        marks.append("undecided")
    if _outside(ws, d):
        marks.append("outside_requested")
    if d.uncertain:
        marks.append("uncertain")
    if not d.dated:
        marks.append("unknown_date")
    row = {"date": d.as_of["date"] if d.dated else None, "precision": d.as_of.get("precision") if d.dated else None,
           "end": d.as_of.get("end") if d.dated else None, "date_kind": _date_kind(d), "value_text": d.written,
           "unit": UNIT_LABELS.get(d.unit) or None, "sid": d.ref,
           "source": {"document_id": d.document_id, "title": d.title, "location": d.location or None},
           "marks": marks}
    if undecided and d.ref in undecided:
        row["undecided_with"] = list(undecided[d.ref])  # the counterpart's sid (R6): the mark names the other value
    if conflicts.get(d.ref):
        row["conflict_with"] = list(conflicts[d.ref])  # the same for a conflict (round 13 U6): a row of another block is named
    return row


def _facet_text(d: Datum) -> str:
    """The facet of a datum as the reader sees it: the source's own words, else that the source states none (status ``none``
    or ``not_read``: round 13 U6, KTD7). No truncation: a long facet wraps."""
    f = d.facet or {}
    return (f.get("written") or MSG_NO_FACET) if f.get("status") == D.STATUS_STATED else MSG_NO_FACET


def _label(members: list[Datum], who: Identity, *, with_facet: bool = True) -> str:
    """The label of a block. The kind is followed by the written facet of the first row (``members`` are in server order;
    round 13 U6, R9): a coefficient or a rate whose source states none shows ``MSG_NO_FACET``, any other datum names a facet
    only when it has one. A components block names no facet: each of its rows does."""
    first = members[0]
    kind = KIND_LABELS.get(first.kind, first.kind) if first.kind else ""
    if with_facet and ((first.facet or {}).get("status") == D.STATUS_STATED or D.needs_facet(first.family)):
        kind = f"{kind}: {_facet_text(first)}" if kind else _facet_text(first)
    parts = [kind]
    if first.period in ("month", "year"):
        parts.append(PERIOD_LABELS[first.period])
    if first.vat in ("included", "excluded") and first.unit in MONEY_UNITS:
        parts.append(VAT_LABELS[first.vat])
    if first.basis and first.unit in AREA_BASIS_UNITS:
        parts.append(f"בסיס שטח: {first.basis}")
    what = " ".join(p for p in parts if p)
    where = who.label or first.title
    return f"{what} — {where}" if what and where else what or where


def _component_row(ws: Workspace, d: Datum, conflicts: dict[str, list[str]], undecided: dict[str, list[str]]) -> dict:
    """A row of the components table: the facet (its own written words), the value, unit, sid, source and marks; ``undecided_with``
    names the counterpart of an undecided mark, as in the timeline."""
    row = _row(ws, d, conflicts, undecided)
    out = {"facet": _facet_text(d), "value_text": row["value_text"], "unit": row["unit"], "sid": row["sid"],
           "source": row["source"], "marks": row["marks"]}
    for key in ("undecided_with", "conflict_with"):
        if key in row:
            out[key] = row[key]
    return out


def _block_as_of(d: Datum) -> dict:
    return {"date": d.as_of["date"], "precision": d.as_of.get("precision"), "end": d.as_of.get("end"),
            "date_kind": _date_kind(d)}


def compose(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
            ) -> tuple[list[dict], list[dict]]:
    """``(timeline, breakdown)`` for the ``cited`` ids: the two blocks that share the rows (round 13 U6, KTD7), so one pass places
    every row in exactly one of them.

    - **breakdown**: per base key, the rows of one appraisal unit and one date (a class deduped on its own) that come from two or
      more classes — parallel components, ``different`` or ``undecided`` — in one block, when a cited datum is among them. A block is
      ``{"label", "as_of", "rows"}``, its rows in the registration order (one date: the server order) with the facet each row
      states; the blocks follow the turn's time mode, then registration.
    - **timeline**: a block for every datum class of the cited ids that holds two or more dated values, or a conflict that involves a
      value with no date (round 12 U11: the mark must be visible), its rows ordered by the turn's time mode and marked (see the
      module doc). A row is marked ``conflict`` when ``coverage.conflicting_pairs`` pairs it with another row of its class, and
      ``undecided`` (round 13 U4, R6) when a datum of another class of its base key that could not be compared with it has a
      different amount in an overlapping period; the row then carries that datum's sid as ``undecided_with``. A class that has rows
      in a components block shows its other dated rows, even one."""
    from app.chat.coverage import conflicting_pairs

    identity = identity or identity_of(ws)
    timeline_blocks: list[dict] = []
    components: list[tuple[tuple, dict]] = []
    for bucket in buckets(ws, cited, identity):
        undecided = bucket.undecided_with()
        classes = [(cls, _dedupe(cls, cited)) for cls in bucket.classes]
        conflicts: dict[str, list[str]] = {}  # a datum -> the sids it conflicts with (its class's conflict pairs)
        for _, members in classes:
            for i, j in conflicting_pairs([d.as_comparable() for d in members]):
                conflicts.setdefault(members[i].ref, []).append(members[j].ref)
                conflicts.setdefault(members[j].ref, []).append(members[i].ref)
        # one appraisal unit at one date: the classes that meet there are parallel components
        at: dict[tuple, list[Datum]] = {}
        met: dict[tuple, set[int]] = {}
        for n, (_, members) in enumerate(classes):
            for d in members:
                if d.dated:
                    at.setdefault((d.appraisal, d.date_key()), []).append(d)
                    met.setdefault((d.appraisal, d.date_key()), set()).add(n)
        placed: set[str] = set()
        for key, rows in at.items():
            if len(met[key]) < 2 or not any(d.ref in cited for d in rows):
                continue
            rows = sorted(rows, key=lambda d: d.index)
            placed |= {d.ref for d in rows}
            components.append(((_order(ws, rows[0]), rows[0].index), {
                "label": _label(rows, identity(rows[0]), with_facet=False), "as_of": _block_as_of(rows[0]),
                "rows": [_component_row(ws, d, conflicts, undecided) for d in rows]}))
        for cls, members in classes:
            if not any(d.ref in cited for d in cls):
                continue
            rest = [d for d in members if d.ref not in placed]
            dated = sum(1 for d in rest if d.dated)
            undated_conflict = any(not d.dated and d.ref in conflicts for d in rest)
            if not ((dated >= 1 and len(rest) < len(members)) or dated >= 2 or undated_conflict):
                continue
            ordered = sorted(rest, key=lambda d: (_order(ws, d), d.index))
            timeline_blocks.append({"label": _label(ordered, identity(ordered[0])),
                                    "rows": [_row(ws, d, conflicts, undecided) for d in ordered]})
    return timeline_blocks, [block for _, block in sorted(components, key=lambda c: c[0])]


def build(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None) -> list[dict]:
    """The timeline: a ``{"label", "rows"}`` block for every datum class of the ``cited`` ids that holds dated values to show
    (``compose``): two or more, a conflict that involves a value with no date, or one when its class has its other rows in a
    components block."""
    return compose(ws, cited, identity)[0]


def breakdown(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None) -> list[dict]:
    """The components table of the answer (``answer.time.breakdown``, round 13 U6, KTD7): a ``{"label", "as_of", "rows"}`` block
    for every appraisal unit and date where two or more datum classes of one base key meet (``compose``); a row is
    ``{"facet", "value_text", "unit", "sid", "source", "marks"}`` and, for an undecided or a conflict mark, ``undecided_with`` or
    ``conflict_with`` (the counterparts' sids, wherever they sit)."""
    return compose(ws, cited, identity)[1]


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


def _mark_text(row: dict, mark: str) -> str:
    """A mark as the copy shows it; an undecided one names the datum it could not be compared with, a conflict the datum it
    conflicts with (round 13 U6)."""
    named = {"undecided": "undecided_with", "conflict": "conflict_with"}.get(mark)
    if named and row.get(named):
        return f"{MARKS[mark]} ({', '.join(row[named])})"
    return MARKS[mark]


def plain_text(blocks: list[dict], components: Iterable[dict] = ()) -> str:
    """The whole timeline as plain text: the heading, then for each group its label and one line per row — date, kind of
    date, value, source title, marks. The components table follows it (heading ``BREAKDOWN_HEADING``; round 13 U6): per block
    its label and date, then one line per row — facet, value, source title, marks. "" when there is neither."""
    def value_of(r: dict) -> str:
        return f"{r['value_text']} {r['unit']}" if r["unit"] else r["value_text"]

    parts = []
    if blocks:
        lines = [HEADING]
        for b in blocks:
            lines.append(b["label"])
            for r in b["rows"]:
                cells = [shown(r), DATE_KINDS.get(r["date_kind"]), value_of(r), r["source"]["title"],
                         ", ".join(_mark_text(r, m) for m in r["marks"] if not (m == "unknown_date" and not r["date"]))]
                lines.append("- " + " · ".join(c for c in cells if c))
        parts.append("\n".join(lines))
    components = list(components)
    if components:
        lines = [BREAKDOWN_HEADING]
        for b in components:
            lines.append(f"{b['label']} ({shown(b['as_of'])})")
            for r in b["rows"]:
                cells = [r["facet"], value_of(r), r["source"]["title"], ", ".join(_mark_text(r, m) for m in r["marks"])]
                lines.append("- " + " · ".join(c for c in cells if c))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


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


def _settled(ws: Workspace, ids: Collection[str], record: dict | None) -> dict | None:
    """The record with what the turn read and the newer datum it could not place (round 13 U8, R19, R20, KTD4, KTD16): a
    unit is read when a V# or M# of the cited datum's base key — registered from it, cited or not — came from it, or its
    section or table was read (``tools.read_places``); a newer measured datum of a class decided neither same nor different
    (``later_undecided``) is named in the record, so its note keeps "במקורות שנבדקו" and says which datum it could not place."""
    if record is None:
        return None
    cited = [i for i in ids if i in ws.values or i in ws.measurements]
    built = buckets(ws, cited) if cited else []
    datum_places = {(d.version_id, d.unit_no) for b in built for d in b.members}
    later = [{"older": a.ref, "newer": b.ref, "title": b.title, "when": shown(b.as_of), "document_id": b.document_id,
              "version_id": b.version_id, "unit_no": b.unit_no} for a, b in later_undecided(ws, cited, built=built)
             ] if cited else []
    return settle_coverage(ws, record, read_places(ws, ids, datum_places), later)


def coverage_of(ws: Workspace, ids: Iterable[str]) -> dict | None:
    """The coverage record of the data the ids cite (KTD7): that of the time-ranked calls whose results they come from, else
    of the turn's calls together; None when the turn made no time-ranked call (off mode, R14). A timed turn that made none
    but read the sources it cites has the reads-only record (``read_coverage``, round 12 U12b). Round 13 U8: settled with
    what the turn read (``_settled``)."""
    ids = list(ids)
    record = coverage_for(ws, sids_behind(ws, ids))
    return _settled(ws, ids, record if record is not None or ws.time_calls else read_coverage(ws, ids))


def partial_coverage(ws: Workspace, ids: Iterable[str]) -> dict | None:
    """The record of a time-ranked call that produced the data ``ids`` cite, when that record is not complete (a partial
    coverage or a property not identified); None for ids no time-ranked call produced, and for a complete record. The
    claim check reads it: a "latest" claim about data of a call that did not check every appraisal needs the qualified
    phrase (``verify._recency_claim``). Settled like ``coverage_of``, so a newer unit that was only located counts."""
    ids = list(ids)
    if not ws.time_calls:  # no time-ranked search: the reads-only record, when the cited sources identify the property
        record = _settled(ws, ids, read_coverage(ws, ids))
        return None if record is None or record.get("complete") else record
    behind = sids_behind(ws, ids)
    if not any(behind & set(c.sids) for c in ws.time_calls):
        return None
    record = _settled(ws, ids, coverage_for(ws, behind))
    return None if record is None or record.get("complete") else record


def payload(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None,
            coverage: dict | None = None) -> dict:
    """``answer.time`` (the round-11 contract): the mode and switch the answer was produced with, the requested time, the
    timeline and its plain-text copy (None without a timeline). Round 12 U8 (R12, KTD8): ``coverage`` — the coverage record
    of the data the answer cites (``coverage_of``) — joins it as ``coverage`` with the server-written ``note``, and the copy
    text ends with the note whether or not the timeline has rows. Without a record (off mode, R14) there is no ``coverage``
    key, and an old message without one reads as "not recorded". Round 13 U6 (KTD7): ``breakdown`` — the components table — sits
    beside the timeline and joins the copy text; an old message has no such key."""
    blocks, components = compose(ws, cited, identity) if cited else ([], [])
    requested = ws.time.requested
    out = {"mode": ws.time.mode, "prefer_recent": ws.time.prefer_recent,
           "requested": requested.as_dict() if requested is not None else None, "timeline": blocks,
           "breakdown": components, "text": plain_text(blocks, components) or None}
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


def _newest_after(ws: Workspace, members: list[Datum], newest: Datum) -> Datum | None:
    """The latest measured datum of ``members`` that starts after ``newest`` ends; None when there is none."""
    later = [d for d in members if _measured_before_anchor(ws, d) and _start(d) > _end(newest)]
    return max(later, key=lambda d: (_end(d), -d.index)) if later else None


def _later(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None,
           built: list[Bucket] | None = None) -> tuple[list[tuple[Datum, Datum]], list[tuple[Datum, Datum]]]:
    """``(decided, undecided)`` — the pairs (the latest cited value of a class, the latest later one) of ``later_than`` and of
    ``later_undecided``. A later datum of the cited datum's own class is decided; one of another class of its base key is
    ignored when that class is decided different from the cited one, and undecided otherwise (KTD4). ``built``: the
    ``buckets`` of the same ``cited`` and ``identity`` when the caller has them."""
    decided: list[tuple[Datum, Datum]] = []
    undecided: list[tuple[Datum, Datum]] = []
    for bucket in buckets(ws, cited, identity) if built is None else built:
        for cls in bucket.classes:
            mine = [d for d in cls if d.ref in cited and _measured_before_anchor(ws, d)]
            if not mine:
                continue
            newest = max(mine, key=lambda d: (_end(d), -d.index))
            if (found := _newest_after(ws, cls, newest)) is not None:
                decided.append((newest, found))
            for other in bucket.classes:
                if other is not cls and bucket.class_relation(cls, other) == D.UNDECIDED \
                        and (found := _newest_after(ws, other, newest)) is not None:
                    undecided.append((newest, found))
    return decided, undecided


def later_pairs(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
                ) -> tuple[list[tuple[Datum, Datum]], list[tuple[Datum, Datum]]]:
    """``(later_than, later_undecided)`` of the cited ids from one pass over the turn's buckets, for a caller that reads both."""
    if not cited:
        return [], []
    return _later(ws, cited, identity)


def later_than(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None
               ) -> list[tuple[Datum, Datum]]:
    """For every datum class of the cited ids: when the latest-dated cited value is dated before a value of the same datum the
    turn registered — one dated, measured (not a forecast, not later than the anchor) and starting after the cited period
    ends — the pair (the latest cited value, the latest registered one). A class where a cited value is the latest gives
    nothing, and a newer datum of another class gives nothing here: a newer datum of a class decided different (another
    component) is not a later value of this datum (round 13 U4, KTD4), and one of an undecided class is
    ``later_undecided``."""
    if not cited:
        return []
    return _later(ws, cited, identity)[0]


def later_undecided(ws: Workspace, cited: Collection[str], identity: Callable[[Datum], Identity] | None = None,
                    built: list[Bucket] | None = None) -> list[tuple[Datum, Datum]]:
    """The pairs (the latest cited value of a class, the latest measured value starting after it) of another class of the same
    base key whose facet could not be compared with the cited one's (round 13 U4, KTD4, R20): the server cannot say that the
    cited value is the latest datum, so a "latest" claim about it stays qualified, and the datum is named. ``built``: the
    ``buckets`` of the same ``cited`` (default identity) when the caller has them."""
    if not cited:
        return []
    return _later(ws, cited, identity, built)[1]
