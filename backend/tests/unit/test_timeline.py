"""The server-built timeline of comparable dated values (round 11 U9: R9, R10, R11, R13; KTD8, KTD9; AE1, AE4).

The workspace is built by hand: registered values (V#) and listed measurements (M#) with the ``as_of`` the tools give
them, and a property identity per document given by a stand-in for the units lookup (the database part is proven in
``tests/integration/test_chat_timeline.py``). Synthetic names, dates and amounts only."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chat import calc, coverage, timeline
from app.chat import property as P
from app.chat.identity import UnitIdentity
from app.chat.timeframe import Requested, TimeMode
from app.chat.tools import Measurement, Workspace

ANCHOR = date(2026, 10, 14)
RECENT = TimeMode("recent", "switch", ANCHOR, None, True)
NONE = TimeMode("none", "switch", ANCHOR, None, False)
EXPLICIT_2022 = TimeMode("explicit", "question", ANCHOR, Requested("point", "2022", "2022", "year", "ב-2022"), True)
SOURCE = {"kind": "source", "unit": "source", "period": "source", "vat": "source", "area_basis": "source",
          "stance": "source", "scenario": "not_stated"}


def as_of(iso: str | None, precision: str = "day", provenance: str = "unit_valuation", forecast: bool = False,
          end: str | None = None) -> dict | None:
    if iso is None and not forecast:
        return None
    return {"date": iso, "precision": precision if iso else None, "end": end, "provenance": provenance if iso else None,
            "forecast": forecast, "locator": "block 1" if iso else None}


class Office:
    """A workspace with a property identity for each document, set by the test (None: the unit states none)."""

    def __init__(self, time: TimeMode = RECENT) -> None:
        self.ws = Workspace(ctx=None)
        self.ws.time = time
        self.property: dict[str, str | None] = {}
        timeline.set_identity(self.ws, lambda d: timeline.Identity(self.property.get(d.document_id), d.title))

    def doc(self, address: str | None = "group:הצאלון 7", title: str = "שומה סינתטית") -> uuid.UUID:
        d = uuid.uuid4()
        self.property[str(d)] = address
        self.titles = getattr(self, "titles", {}) | {d: title}
        return d

    def value(self, doc: uuid.UUID, amount: str, when: dict | None, *, kind: str = "value", unit: str = "ILS",
              period: str = "none", vat: str = "included", area_basis: str = "", provenance: dict | None = None,
              title: str | None = None, version: uuid.UUID | None = None, **kw) -> str:
        """``version``: the document version it is in (a new one for each value when not given); ``kw``: more ``calc.Value``
        fields (``facet``, ``table``, ``section``, ``scale`` ...)."""
        vid = f"V{len(self.ws.values) + 1}"
        prov = SOURCE | (provenance or {})
        v = calc.Value(vid, Decimal(amount.replace(",", "")), amount, "S1", doc, version or uuid.uuid4(), "r1",
                       title or self.titles.get(doc, "שומה סינתטית"), "סעיף 3", "שווי הנכס", kind, unit, period, vat,
                       area_basis, "הנכס", "appraiser_determination", prov, {"quote": f"{amount} ₪"}, f"{amount} ₪",
                       stance="adopted", **kw)
        v.as_of = when
        self.ws.values[vid] = v
        return vid

    def measurement(self, doc: uuid.UUID, amount: str, when: dict | None, *, metric_kind: str = "rent", unit: str = "ILS",
                    period: str = "month", vat: str = "included", area_basis: str = "", stance: str = "adopted",
                    value_text: str | None = None, facet: dict | None = None) -> str:
        mid = f"M{len(self.ws.measurements) + 1}"
        row = SimpleNamespace(id=uuid.uuid4(), metric="דמי שכירות", metric_kind=metric_kind, value=Decimal(amount.replace(",", "")),
                              value_text=value_text or f"{amount} ₪", unit=unit, period=period, vat=vat,
                              area_basis=area_basis, subject="הנכס", scenario="", stance=stance, status="auto_validated",
                              quote="x", section="סעיף 4", block_index=1, table_index=None)
        m = Measurement(mid, row.id, doc, uuid.uuid4(), self.titles.get(doc, "שומה סינתטית"), row)
        m.as_of = when
        m.facet = facet
        self.ws.measurements[mid] = m
        return mid

    def blocks(self, cited: set[str]) -> list[dict]:
        return timeline.build(self.ws, cited)


def dates_of(group: dict) -> list[str | None]:
    return [r["date"] for r in group["rows"]]


def three(office: Office) -> tuple[str, str, str]:
    """One property appraised at three valuation dates, a document each (AE1): V1 2022, V2 2024, V3 2025."""
    ids = [office.value(office.doc(title=f"שומה {y}"), amount, as_of(iso)) for y, amount, iso in
           (("2022", "1,950,000", "2022-03-10"), ("2024", "2,280,000", "2024-06-15"), ("2025", "2,410,000", "2025-01-20"))]
    return ids[0], ids[1], ids[2]


# --- AE1 ------------------------------------------------------------------------------------------------------------------

def test_ae1_three_appraisals_of_one_property_form_one_group_newest_first_with_their_sources():
    o = Office()
    v22, v24, v25 = three(o)
    (g,) = o.blocks({v25})
    assert dates_of(g) == ["2025-01-20", "2024-06-15", "2022-03-10"]
    assert [r["sid"] for r in g["rows"]] == [v25, v24, v22]
    assert [r["source"]["title"] for r in g["rows"]] == ["שומה 2025", "שומה 2024", "שומה 2022"]
    assert all(r["source"]["document_id"] and r["marks"] == [] and r["date_kind"] == "valuation" for r in g["rows"])
    assert [r["value_text"] for r in g["rows"]] == ["2,410,000", "2,280,000", "1,950,000"]
    assert g["label"]


def test_ae1_three_values_of_different_dates_are_a_history_not_a_sources_conflict():
    o = Office()
    three(o)
    assert not coverage.conflicting(o.ws)


def test_the_answer_cites_only_the_newest_while_the_older_were_registered_all_three_rows_are_in_the_block():
    o = Office()
    v22, v24, v25 = three(o)
    (g,) = o.blocks({v25})
    assert {r["sid"] for r in g["rows"]} == {v22, v24, v25}


def test_a_group_nobody_cited_has_no_block():
    o = Office()
    three(o)
    stray = o.value(o.doc("group:האלון 3"), "1,000,000", as_of("2023-01-01"))  # another property
    assert o.blocks({stray}) == []


# --- the key (KTD9) -------------------------------------------------------------------------------------------------------

def test_two_totals_with_area_basis_and_periodicity_not_stated_and_vat_included_on_both_are_one_group():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"), period="none", area_basis="",
                provenance={"period": "not_stated", "area_basis": "not_stated"})
    b = o.value(o.doc(), "2,100,000", as_of("2025-01-01"), period="none", area_basis="",
                provenance={"period": "not_stated", "area_basis": "not_stated"})
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [2]
    assert len(o.blocks({a})) == 1 and {r["sid"] for r in o.blocks({a})[0]["rows"]} == {a, b}


def test_units_that_state_no_identifier_never_share_a_group():
    o = Office()
    a = o.value(o.doc(None), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc(None), "2,100,000", as_of("2025-01-01"))
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]
    assert o.blocks({a}) == []


def test_two_values_of_one_unit_with_no_identifier_stay_together():
    o = Office()
    doc = o.doc(None)
    o.property[str(doc)] = None
    timeline.set_identity(o.ws, lambda d: timeline.Identity(None, d.title))
    a = o.value(doc, "2,000,000", as_of("2024-01-01"))
    o.value(doc, "2,100,000", as_of("2025-01-01"))
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [2]


def test_ae4_a_monthly_rent_with_vat_and_a_yearly_rent_without_it_are_two_timelines():
    o = Office()
    monthly = [o.value(o.doc(), m, as_of(d), kind="rent", period="month", vat="included")
               for m, d in (("6,600", "2024-07-01"), ("6,900", "2025-07-01"))]
    yearly = [o.value(o.doc(), y, as_of(d), kind="rent", period="year", vat="excluded")
              for y, d in (("75,600", "2024-09-01"), ("79,000", "2025-09-01"))]
    assert sorted(len(g) for g in timeline.groups(o.ws, {monthly[0], yearly[0]})) == [2, 2]
    blocks = o.blocks({monthly[0], yearly[0]})
    assert len(blocks) == 2
    assert {frozenset(r["sid"] for r in g["rows"]) for g in blocks} == {frozenset(monthly), frozenset(yearly)}
    assert blocks[0]["label"] != blocks[1]["label"]


def test_the_same_address_with_another_unit_or_floor_is_another_group():
    o = Office()
    a = o.value(o.doc("group:הצאלון 7"), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc("group:הצאלון 7 קומה 2"), "2,100,000", as_of("2025-01-01"))
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


@pytest.mark.parametrize("kind,unit", [("value_per_area", "ILS_per_sqm"), ("rent_per_area", "ILS_per_sqm")])
def test_net_versus_gross_area_basis_are_separate_groups(kind, unit):
    o = Office()
    net = o.value(o.doc(), "9,500", as_of("2024-01-01"), kind=kind, unit=unit, area_basis="נטו", period="month")
    o.value(o.doc(), "9,900", as_of("2025-01-01"), kind=kind, unit=unit, area_basis="ברוטו", period="month")
    o.value(o.doc(), "9,700", as_of("2025-06-01"), kind=kind, unit=unit, area_basis="נטו", period="month")
    sizes = sorted(len(g) for g in timeline.groups(o.ws, {net}))
    assert sizes == [2]  # the group of the cited net value holds the other net value only


def test_a_total_and_a_per_area_value_of_one_property_are_separate_groups():
    o = Office()
    doc = o.doc()
    a = o.value(doc, "2,000,000", as_of("2024-01-01"), kind="value", unit="ILS")
    o.value(o.doc(), "21,000", as_of("2025-01-01"), kind="value_per_area", unit="ILS_per_sqm", area_basis="נטו")
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


def test_vat_not_stated_on_both_values_does_not_group_them():
    o = Office()
    a = o.value(o.doc(), "6,600", as_of("2024-07-01"), kind="rent", period="month", vat="unknown",
                provenance={"vat": "not_stated"})
    o.value(o.doc(), "6,900", as_of("2025-07-01"), kind="rent", period="month", vat="unknown",
            provenance={"vat": "not_stated"})
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


def test_a_field_the_model_asserted_never_equals_another():
    o = Office()
    a = o.value(o.doc(), "6,600", as_of("2024-07-01"), kind="rent", period="month", vat="included",
                provenance={"vat": "model_asserted"})
    o.value(o.doc(), "6,900", as_of("2025-07-01"), kind="rent", period="month", vat="included",
            provenance={"vat": "model_asserted"})
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


def test_a_rent_whose_period_is_not_stated_is_not_grouped_but_a_total_with_none_is():
    o = Office()
    a = o.value(o.doc(), "6,600", as_of("2024-07-01"), kind="rent", period="none", provenance={"period": "not_stated"})
    o.value(o.doc(), "6,900", as_of("2025-07-01"), kind="rent", period="none", provenance={"period": "not_stated"})
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


def test_a_different_scenario_or_stance_is_another_group():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc(), "2,100,000", as_of("2025-01-01"), scenario="לאחר מימוש התכנית",
            provenance={"scenario": "source"})
    c = o.value(o.doc(), "2,200,000", as_of("2025-02-01"))
    c_value = o.ws.values[c]
    c_value.stance = "claim"
    c_value.provenance = SOURCE | {"stance": "source"}
    assert [len(g) for g in timeline.groups(o.ws, {a})] == [1]


def test_a_scenario_only_the_model_asserted_neither_splits_nor_drops_the_history():
    """Real model, round 11: the model wrote each value's date into its free-text scenario ("המועד הקובע 20/01/2025",
    provenance model_asserted) and the rents fell out of every timeline. A scenario the source does not state is none."""
    o = Office()
    rows = [o.value(o.doc(), amount, as_of(date), scenario=f"המועד הקובע {date}",
                    provenance={"scenario": "model_asserted"})
            for amount, date in (("5,200", "2022-03-10"), ("6,100", "2024-06-15"), ("6,400", "2025-01-20"))]
    assert [len(g) for g in timeline.groups(o.ws, {rows[0]})] == [3]


def test_adopted_and_unknown_stance_values_of_one_property_form_one_group_in_date_order():
    """The appraisal's own data is one class (round 11 U16, KTD9): a value registered as adopted ("השווי נקבע") and the ones
    registered from header lines, whose stance is unknown, are one timeline — the newest row is not lost."""
    o = Office()
    old = o.value(o.doc(), "1,950,000", as_of("2022-03-10"))
    mid = o.value(o.doc(), "2,280,000", as_of("2024-06-15"))
    new = o.value(o.doc(), "2,410,000", as_of("2025-01-20"))
    for ref in (old, mid):
        o.ws.values[ref].stance = "unknown"
    o.ws.values[mid].provenance = SOURCE | {"stance": "not_stated"}
    (g,) = o.blocks({new})
    assert [r["sid"] for r in g["rows"]] == [new, mid, old] and dates_of(g)[0] == "2025-01-20"
    assert [len(x) for x in timeline.groups(o.ws, {old})] == [3]


def test_a_claim_stays_apart_from_the_appraisals_own_data_even_next_to_unknown_stance_values():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    o.ws.values[a].stance = "unknown"
    o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    c = o.value(o.doc(), "2,200,000", as_of("2025-02-01"))
    o.ws.values[c].stance = "claim"
    c2 = o.value(o.doc(), "2,300,000", as_of("2025-03-01"))
    o.ws.values[c2].stance = "claim"
    assert sorted(len(g) for g in timeline.groups(o.ws, {a, c})) == [2, 2]
    for other in ("proposal", "estimate", "other"):
        p = o.value(o.doc(), "2,400,000", as_of("2025-04-01"))
        o.ws.values[p].stance = other
        assert sorted(len(g) for g in timeline.groups(o.ws, {a, c, p})) == [1, 2, 2], other
        del o.ws.values[p]


def test_a_measurement_without_a_stance_joins_an_adopted_value():
    o = Office()
    v = o.value(o.doc(), "6,600", as_of("2024-07-01"), kind="rent", period="month", vat="included")
    m = o.measurement(o.doc(), "6,900", as_of("2025-07-01", provenance="source"), stance="")
    (g,) = o.blocks({v})
    assert [r["sid"] for r in g["rows"]] == [m, v]


def test_the_conflict_check_reads_the_same_stance_class_as_the_timeline():
    o = Office()
    o.value(o.doc(), "2,410,000", as_of("2025-01-20"))
    u = o.value(o.doc(), "2,300,000", as_of("2025-01-20"))
    o.ws.values[u].stance = "unknown"
    assert coverage.conflicting(o.ws) is True  # adopted and unknown are one datum: the same date, two amounts
    o.ws.values[u].stance = "claim"
    assert coverage.conflicting(o.ws) is False  # a claim is another datum


def test_measurements_join_the_group_of_a_cited_value():
    o = Office()
    doc = o.doc()
    v = o.value(doc, "6,600", as_of("2024-07-01"), kind="rent", period="month", vat="included")
    m = o.measurement(o.doc(), "6,900", as_of("2025-07-01", provenance="source"))
    (g,) = o.blocks({v})
    assert [r["sid"] for r in g["rows"]] == [m, v]
    assert g["rows"][0]["date_kind"] == "stated"


# --- the order and the marks (R4, R7, R10, R11, KTD6) -----------------------------------------------------------------------

def test_a_single_dated_value_gives_no_block_and_two_values_with_one_dated_give_none_unless_they_conflict():
    """Round 12 U11 (R18): the undated second value is history here, so it conflicts with nothing and no block is built; an
    undated value with no such origin and another amount is a conflict and does build one (``test_ae7_*``)."""
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    assert o.blocks({a}) == []
    o.value(o.doc(), "2,100,000", withheld("history"))
    assert o.blocks({a}) == []


def test_undated_values_follow_the_dated_ones_with_the_unknown_date_mark():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    b = o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    u = o.value(o.doc(), "2,050,000", withheld("comparable"))  # round 12 U11: an unmarked undated value would conflict
    (g,) = o.blocks({a})
    assert [r["sid"] for r in g["rows"]] == [b, a, u]
    assert g["rows"][2]["date"] is None and g["rows"][2]["marks"] == ["unknown_date"] and g["rows"][2]["date_kind"] is None


def test_a_forecast_is_marked_and_never_first_even_when_the_switch_is_on_or_off():
    for mode in (RECENT, NONE):
        o = Office(mode)
        a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
        b = o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
        f = o.value(o.doc(), "3,000,000", as_of("2027-01-01", provenance="source", forecast=True))
        (g,) = o.blocks({a})
        assert g["rows"][0]["sid"] != f, mode
        row = next(r for r in g["rows"] if r["sid"] == f)
        assert "forecast" in row["marks"]
        assert [r["sid"] for r in g["rows"]][-1] == f
        assert b in [r["sid"] for r in g["rows"]]


def test_a_forecast_with_no_date_is_marked_forecast_and_unknown_and_listed_last():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    f = o.value(o.doc(), "3,000,000", withheld("comparable", forecast=True))  # round 12 U11: undated, but no conflict
    (g,) = o.blocks({a})
    assert g["rows"][-1]["sid"] == f and set(g["rows"][-1]["marks"]) == {"forecast", "unknown_date"}


def test_mode_none_lists_the_group_chronologically_oldest_first_with_unknown_last():
    o = Office(NONE)
    c = o.value(o.doc(), "2,300,000", as_of("2025-01-01"))
    u = o.value(o.doc(), "2,100,000", None)
    a = o.value(o.doc(), "2,000,000", as_of("2022-01-01"))
    b = o.value(o.doc(), "2,200,000", as_of("2024-01-01"))
    (g,) = o.blocks({c})
    assert [r["sid"] for r in g["rows"]] == [a, b, c, u]


def test_explicit_2022_lists_2022_first_and_marks_the_later_ones_outside_the_requested_time():
    o = Office(EXPLICIT_2022)
    v22, v24, v25 = three(o)
    (g,) = o.blocks({v22})
    assert [r["sid"] for r in g["rows"]] == [v22, v24, v25]  # in the range, then the nearest
    assert g["rows"][0]["marks"] == []
    assert all("outside_requested" in r["marks"] for r in g["rows"][1:])


def test_outside_requested_is_only_for_an_explicit_mode_and_an_undated_row_is_not_outside():
    o = Office(EXPLICIT_2022)
    a = o.value(o.doc(), "2,000,000", as_of("2022-05-01"))
    o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    u = o.value(o.doc(), "2,050,000", withheld("history"))  # round 12 U11: an unmarked undated value would conflict
    (g,) = o.blocks({a})
    unknown = next(r for r in g["rows"] if r["sid"] == u)
    assert unknown["marks"] == ["unknown_date"]
    o2 = Office(RECENT)
    three(o2)
    assert all("outside_requested" not in r["marks"] for g in o2.blocks({"V3"}) for r in g["rows"])


def test_same_date_with_different_amounts_is_a_conflict_not_a_trend():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2025-01-20"))
    b = o.value(o.doc(), "2,100,000", as_of("2025-01-20"))
    c = o.value(o.doc(), "1,900,000", as_of("2024-01-20"))
    (g,) = o.blocks({a})
    marks = {r["sid"]: r["marks"] for r in g["rows"]}
    assert marks[a] == marks[b] == ["conflict"] and marks[c] == []
    assert coverage.conflicting(o.ws)


def test_the_same_date_and_amount_from_one_document_is_one_row():
    o = Office()
    doc = o.doc()
    a = o.value(doc, "2,000,000", as_of("2025-01-20"))
    o.value(doc, "2,000,000", as_of("2025-01-20"))
    o.value(o.doc(), "1,900,000", as_of("2024-01-20"))
    (g,) = o.blocks({a})
    assert len(g["rows"]) == 2


def test_the_date_kind_follows_the_provenance_of_the_date():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2025-01-01", provenance="unit_valuation"))
    r = o.value(o.doc(), "2,100,000", as_of("2022", "year", provenance="source_table", end="2023"))
    s = o.value(o.doc(), "2,200,000", as_of("2024-03-01", provenance="source"))
    t = o.value(o.doc(), "2,150,000", as_of("2024-04-01", provenance="source"))
    o.ws.values[t].role = "comparison"
    (g,) = o.blocks({a})
    kinds = {row["sid"]: row["date_kind"] for row in g["rows"]}
    assert kinds == {a: "valuation", r: "range", s: "stated", t: "transaction"}
    ranged = next(row for row in g["rows"] if row["sid"] == r)
    assert (ranged["date"], ranged["precision"]) == ("2022", "year")


# --- a reading that is not certain (review #3) -------------------------------------------------------------------------------

def test_a_value_whose_region_stayed_unclear_is_marked_uncertain_in_the_row_and_the_plain_text():
    o = Office()
    a, b, c = three(o)
    o.ws.uncertain_values[a] = "synthetic note"
    (g,) = o.blocks({c})
    marks = {r["sid"]: r["marks"] for r in g["rows"]}
    assert marks == {a: ["uncertain"], b: [], c: []}
    assert timeline.MARKS["uncertain"] in timeline.plain_text([g])


def test_a_measurement_whose_place_was_lost_or_that_awaits_review_is_marked_uncertain():
    o = Office()
    d = o.doc()
    v = o.value(d, "4,000", as_of("2025-01-01"), kind="rent", period="month")
    lost = o.measurement(d, "3,800", as_of("2023-01-01"))
    flagged = o.measurement(d, "3,900", as_of("2024-01-01"))
    o.ws.measurements[lost].row.anchor_lost = {"reason": "synthetic"}
    o.ws.measurements[flagged].row.status = "needs_review"
    (g,) = o.blocks({lost, flagged, v})
    marks = {r["sid"]: r["marks"] for r in g["rows"]}
    assert marks[lost] == ["uncertain"] and marks[flagged] == ["uncertain"] and "uncertain" not in marks.get(v, [])


# --- the payload (R13) ---------------------------------------------------------------------------------------------------------

def test_the_payload_carries_the_mode_the_request_the_timeline_and_a_plain_text_copy():
    o = Office(EXPLICIT_2022)
    v22, v24, v25 = three(o)
    p = timeline.payload(o.ws, {v25})
    assert p["mode"] == "explicit" and p["prefer_recent"] is True
    assert p["requested"] == {"kind": "point", "start": "2022", "end": "2022", "precision": "year", "text": "ב-2022"}
    assert len(p["timeline"]) == 1 and p["timeline"][0]["rows"][0]["sid"] == v22
    text = p["text"].splitlines()
    assert text[0] == timeline.HEADING
    assert timeline.DATE_KINDS["valuation"] in p["text"] and timeline.MARKS["outside_requested"] in p["text"]
    assert "2,410,000" in p["text"] and "שומה 2025" in p["text"] and "20/01/2025" in p["text"]
    assert len([ln for ln in text if ln.startswith("- ")]) == 3


def test_the_payload_without_a_timeline_has_no_text():
    o = Office(NONE)
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    p = timeline.payload(o.ws, {a})
    assert p == {"mode": "none", "prefer_recent": False, "requested": None, "timeline": [], "breakdown": [], "text": None}


def test_the_ids_of_the_rows_are_the_values_and_measurements_the_payload_must_carry():
    o = Office()
    v22, v24, v25 = three(o)
    assert timeline.row_ids(o.blocks({v25})) == {v22, v24, v25}


# --- the later comparable value of a cited one (the recency claim) --------------------------------------------------------------

def test_a_cited_value_with_a_later_dated_comparable_value_names_it():
    o = Office()
    v22, v24, v25 = three(o)
    found = timeline.later_than(o.ws, [v22])
    assert [(c.ref, later.ref) for c, later in found] == [(v22, v25)]


def test_a_cited_latest_value_has_no_later_one_and_citing_the_latest_with_it_is_enough():
    o = Office()
    v22, v24, v25 = three(o)
    assert timeline.later_than(o.ws, [v25]) == []
    assert timeline.later_than(o.ws, [v22, v25]) == []


def test_a_later_forecast_or_undated_or_not_comparable_value_is_no_later_value():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc(), "3,000,000", as_of("2027-01-01", provenance="source", forecast=True))
    o.value(o.doc(), "2,500,000", None)
    o.value(o.doc("group:האלון 3"), "2,600,000", as_of("2025-06-01"))
    o.value(o.doc(), "2,700,000", as_of("2025-06-01"), vat="excluded")
    assert timeline.later_than(o.ws, [a]) == []


def test_a_month_is_later_than_the_year_before_it_but_not_than_a_year_that_holds_it():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024", "year", provenance="source"))
    o.value(o.doc(), "2,100,000", as_of("2024-06", "month", provenance="source"))
    assert timeline.later_than(o.ws, [a]) == []
    b = o.value(o.doc(), "2,200,000", as_of("2025-02", "month", provenance="source"))
    assert [later.ref for _, later in timeline.later_than(o.ws, [a])] == [b]


def test_u6_values_of_gathered_units_share_one_group_and_values_of_candidates_never_do():
    """The property of a datum is its unit's group in the turn's graph (round 12 U6, KTD5): two units of one street, number
    and city gather and form one block; two units of that street and number that state no city are candidates of the
    focus, not of one another, so each stays a group of its own and there is no block."""
    o = Office()
    d1, d2, d3, d4 = (o.doc(None) for _ in range(4))
    ref = {k: o.value(d, "2,000,000", as_of(when)) for k, d, when in (
        ("a", d1, "2023-01-01"), ("b", d2, "2025-01-01"), ("c", d3, "2024-01-01"), ("e", d4, "2022-01-01"))}
    ids = {k: o.ws.values[v] for k, v in ref.items()}

    def unit(k: str, city: str | None) -> P.Node:
        who = UnitIdentity(1, addresses=frozenset({("צאלון", "7")}), cities=frozenset({city} if city else ()))
        return P.Node(ids[k].document_id, ids[k].version_id, 1, who, k)

    nodes = {"a": unit("a", "עיר סינתטית"), "b": unit("b", "עיר סינתטית"), "c": unit("c", None), "e": unit("e", None)}
    graph = P.PropertyGraph(nodes.values())
    focus = graph.gather(nodes["a"].key)
    assert [m.title for m in focus.members] == sorted(["a", "b"], key=lambda k: nodes[k].key)
    assert {c.node.title for c in focus.candidates} == {"c", "e"}
    who = timeline._UnitIdentity(o.ws)
    who.partition = graph.partition(focus)
    timeline.set_identity(o.ws, who)
    blocks = o.blocks(set(ref.values()))
    assert [[r["sid"] for r in b["rows"]] for b in blocks] == [[ref["b"], ref["a"]]], "the gathered units only, newest first"
    assert "צאלון 7" in blocks[0]["label"]
    assert o.ws.values[ref["c"]].document_id != o.ws.values[ref["e"]].document_id
    keys = {k: who(timeline.datums(o.ws)[i]).key for i, k in enumerate(ref)}
    assert keys["a"] == keys["b"] and len({keys["a"], keys["c"], keys["e"]}) == 3


# --- pairwise conflicts (round 12 U11: R18, AE7; KTD13) ---------------------------------------------------------------------------

def withheld(origin: str, forecast: bool = False) -> dict:
    """The ``as_of`` of a datum undated only because it is history or a comparable (round 12 U4, KTD12)."""
    return {"date": None, "precision": None, "end": None, "provenance": None, "forecast": forecast, "locator": None,
            "date_origin": origin}


def marks_of(g: dict) -> dict[str, list[str]]:
    return {r["sid"]: r["marks"] for r in g["rows"]}


def test_ae7_a_dated_and_an_undated_value_of_one_property_conflict_and_both_rows_carry_the_mark():
    o = Office()
    v1 = o.value(o.doc(), "2,450,000", as_of("2025-03-31"))
    v2 = o.value(o.doc(), "2,300,000", None)
    assert coverage.conflicting(o.ws)
    (g,) = o.blocks({v1})  # one dated row only: the block exists so that the mark is visible
    assert marks_of(g) == {v1: ["conflict"], v2: ["conflict", "unknown_date"]}
    assert timeline.MARKS["conflict"] in timeline.plain_text([g])


def test_ae7_an_undated_value_of_history_is_no_conflict():
    o = Office()
    v1 = o.value(o.doc(), "2,450,000", as_of("2025-03-31"))
    o.value(o.doc(), "2,300,000", withheld("history"))
    assert not coverage.conflicting(o.ws)
    assert o.blocks({v1}) == []  # one dated value and no conflict: no timeline


def test_an_undated_comparable_is_no_conflict_either():
    o = Office()
    v1 = o.value(o.doc(), "2,450,000", as_of("2025-03-31"))
    o.value(o.doc(), "2,300,000", withheld("comparable"))
    assert not coverage.conflicting(o.ws)
    assert o.blocks({v1}) == []


def test_two_dated_values_without_overlap_are_a_history_not_a_conflict():
    o = Office()
    a = o.value(o.doc(), "100", as_of("2023-06-01"))
    b = o.value(o.doc(), "120", as_of("2024-06-01"))
    assert not coverage.conflicting(o.ws)
    (g,) = o.blocks({a})
    assert marks_of(g) == {a: [], b: []}


def test_a_year_and_a_day_inside_it_overlap_so_different_amounts_conflict():
    o = Office()
    a = o.value(o.doc(), "100", as_of("2023", "year", provenance="source"))
    b = o.value(o.doc(), "120", as_of("2023-12-31"))
    assert coverage.conflicting(o.ws)
    (g,) = o.blocks({a})
    assert marks_of(g) == {a: ["conflict"], b: ["conflict"]}


def test_a_conflict_is_between_the_pair_not_the_group_an_undated_value_conflicts_with_the_amount_it_differs_from():
    o = Office()
    a = o.value(o.doc(), "100", as_of("2023-06-01"))
    b = o.value(o.doc(), "120", as_of("2024-06-01"))
    c = o.value(o.doc(), "100", None)
    assert coverage.conflicting(o.ws)
    (g,) = o.blocks({a})
    assert marks_of(g) == {a: [], b: ["conflict"], c: ["conflict", "unknown_date"]}


def test_values_of_different_gathered_properties_never_conflict_even_when_undated_or_overlapping():
    o = Office()
    a = o.value(o.doc("group:הצאלון 7"), "100", as_of("2025-03-31"))
    b = o.value(o.doc("group:האלון 3"), "120", as_of("2025-03-31"))
    o.value(o.doc("group:האלון 3"), "130", None)
    assert [len(g) for g in timeline.groups(o.ws, {a, b})] == [1, 2]
    assert not coverage.conflicting(o.ws, ids={a, b})
    assert o.blocks({a}) == []


def test_values_of_one_gathered_property_in_different_documents_conflict_whatever_subject_the_model_gave_them():
    o = Office()
    a = o.value(o.doc(), "100", as_of("2025-03-31"))
    b = o.value(o.doc(), "120", as_of("2025-03-31"))
    o.ws.values[b].subject = "הנכס הנדון"  # another wording of the same property: the graph decides, not the words
    assert coverage.conflicting(o.ws)
    (g,) = o.blocks({a})
    assert marks_of(g) == {a: ["conflict"], b: ["conflict"]}


def test_two_undated_values_with_different_amounts_still_conflict_and_make_a_block():
    o = Office()
    a = o.value(o.doc(), "100", None)
    b = o.value(o.doc(), "120", None)
    assert coverage.conflicting(o.ws)
    (g,) = o.blocks({a})
    assert marks_of(g) == {a: ["conflict", "unknown_date"], b: ["conflict", "unknown_date"]}


def test_an_undated_history_value_and_an_undated_unmarked_one_do_not_conflict():
    o = Office()
    a = o.value(o.doc(), "100", withheld("history"))
    o.value(o.doc(), "120", None)
    assert not coverage.conflicting(o.ws)
    assert o.blocks({a}) == []


def test_validate_requested_keeps_sources_conflict_exactly_when_the_timeline_shows_a_conflict_mark():
    def claimed(o: Office, cited: str) -> tuple[str, bool]:
        docs = [str(d) for d in o.property]
        for d in docs:
            o.ws.activity[d] = {"title": "שומה סינתטית", "partial": False}
        req = SimpleNamespace(document_ids=docs, status="sources_conflict", component="N1", label="שווי",
                              checked_where=None)
        (out,) = coverage.validate_requested(o.ws, [req])
        marked = any("conflict" in r["marks"] for g in o.blocks({cited}) for r in g["rows"])
        return out["status"], marked

    for second, expected in ((None, True), (as_of("2024-03-31"), False), (withheld("history"), False)):
        o = Office()
        v1 = o.value(o.doc(), "2,450,000", as_of("2025-03-31"))
        o.value(o.doc(), "2,300,000", second)
        status, marked = claimed(o, v1)
        assert marked is expected and (status == "sources_conflict") is expected


# --- one datum decision (round 13 U4: R3, R4, R6; KTD3, KTD4) ----------------------------------------------------------------------

from app.chat import datum  # noqa: E402


def _coefficient(o: Office, doc, amount: str, when: dict | None, written: str | None, *, version=None,
                 tier: str = "own") -> str:
    """A coefficient of a table row naming ``written`` (None: no facet stated), in one section."""
    facet = datum.facet(written, "row", tier) if written else datum.none()
    return o.value(doc, amount, when, kind="coefficient", unit="ratio", version=version or uuid.uuid4(), facet=facet,
                   section="3. מקדמים")


def test_two_coefficients_that_could_not_be_compared_are_undecided_not_a_conflict_and_the_status_follows():
    """The parity test of the conflict reason, for a pair that is neither: undecided on the timeline, and
    ``sources_conflict`` is not kept for it."""
    o = Office()
    a = _coefficient(o, o.doc(), "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה")
    b = _coefficient(o, o.doc(), "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין")  # another unit: no evidence
    docs = [str(d) for d in o.property]
    for d in docs:
        o.ws.activity[d] = {"title": "שומה סינתטית", "partial": False}
    req = SimpleNamespace(document_ids=docs, status="sources_conflict", component="N1", label="שווי", checked_where=None)
    (out,) = coverage.validate_requested(o.ws, [req])
    assert out["status"] != "sources_conflict" and not coverage.conflicting(o.ws)
    assert o.blocks({a, b}) == []  # one row each: no timeline block (the components table is U6's)
    ((x, y),) = timeline.buckets(o.ws, {a, b})[0].undecided()
    assert {x.ref, y.ref} == {a, b}


def test_an_undecided_mark_is_on_both_rows_and_carries_the_counterparts_sid():
    o = Office()
    facet = "מקדם הפחתה בשל מגבלות בנייה"
    old = _coefficient(o, o.doc(), "0.8", as_of("2023-03-31"), facet)
    new = _coefficient(o, o.doc(), "0.8", as_of("2025-03-31"), facet)  # one datum over two years: a history, two rows
    other = _coefficient(o, o.doc(), "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין")
    (g,) = o.blocks({new})
    rows = {r["sid"]: r for r in g["rows"]}
    assert set(rows) == {old, new}
    assert rows[new]["marks"] == ["undecided"] and rows[new]["undecided_with"] == [other]
    assert rows[old]["marks"] == [] and "undecided_with" not in rows[old]  # 2023 does not overlap 2025
    assert timeline.MARKS["undecided"] == "השוואה לא הוכרעה"
    assert f"{timeline.MARKS['undecided']} ({other})" in timeline.plain_text([g])
    assert not coverage.conflicting(o.ws)


def test_a_pair_of_equal_amounts_is_never_marked_undecided():
    o = Office()
    a = _coefficient(o, o.doc(), "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה")
    b = _coefficient(o, o.doc(), "0.80", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין")
    assert timeline.buckets(o.ws, {a, b})[0].undecided() == []


def test_the_base_key_normalizes_the_unit_so_a_percent_and_a_ratio_of_a_coefficient_meet_but_a_value_does_not_split():
    o = Office()
    doc = o.doc()
    fraction = o.value(doc, "0.8", as_of("2025-03-31"), kind="coefficient", unit="ratio")
    percent = o.value(o.doc(), "80", as_of("2025-03-31"), kind="coefficient", unit="percent")
    assert [len(g) for g in timeline.groups(o.ws, {fraction})] == [2]
    other = o.value(doc, "80", as_of("2025-03-31"), kind="value", unit="percent")  # not a coefficient or a proportion
    assert all(other not in {d.ref for d in g} for g in timeline.groups(o.ws, {fraction}))
    assert not coverage.conflicting(o.ws, ids={fraction, percent})  # 0.8 = 80%: one amount at one date


def test_a_property_graph_that_cannot_be_read_gives_every_datum_no_identity_and_the_conflict_check_no_key(monkeypatch):
    """The timeline is an addition (round 12 U6): when ``property_partition`` raises, each datum keeps its own identity
    (``Identity(None)``) and ``property_key`` is None, instead of the turn failing."""
    from contextlib import contextmanager

    o = Office()
    v1 = o.value(o.doc(), "2,450,000", as_of("2025-03-31"))
    v2 = o.value(o.doc(), "2,300,000", as_of("2024-03-31"))
    ws = o.ws
    ws.__dict__.pop(timeline._ATTR)   # the stand-in goes: the real reader runs, under a context
    ws.ctx = object()

    @contextmanager
    def tx(ctx):
        yield None

    def broken(ws_, conn):
        raise RuntimeError("the graph could not be built")

    monkeypatch.setattr(timeline, "tenant_tx", tx)
    monkeypatch.setattr(timeline, "property_partition", broken)
    who = timeline._UnitIdentity(ws)
    assert [who(d) for d in timeline.datums(ws)] == [timeline.Identity(None)] * 2
    assert who.partition is False, "the failure is remembered: the graph is not read again"
    for ref in (v1, v2):
        value = ws.values[ref]
        assert timeline.property_key(ws, ref, value.document_id, value.version_id, value.context) is None


# --- the components table (round 13 U6: R8, R9, R10, R24; KTD7) -------------------------------------------------------------------

def _unit(o: Office) -> tuple[uuid.UUID, uuid.UUID]:
    """One appraisal unit: a document and its version, for the coefficients that sit side by side in it."""
    return o.doc(), uuid.uuid4()


def test_ae1_two_parallel_coefficients_are_one_components_block_naming_their_row_labels_and_no_timeline_block():
    o = Office()
    doc, ver = _unit(o)
    a = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    b = _coefficient(o, doc, "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין", version=ver)
    assert o.blocks({a, b}) == []
    (block,) = timeline.breakdown(o.ws, {a})  # one cited coefficient brings its parallel component with it
    assert [r["sid"] for r in block["rows"]] == [a, b]
    assert [r["facet"] for r in block["rows"]] == ["מקדם הפחתה בשל מגבלות בנייה", "מקדם הפחתה בשל שיתוף במקרקעין"]
    assert [r["value_text"] for r in block["rows"]] == ["0.8", "0.9"]
    assert all(r["marks"] == [] and r["source"]["title"] and r["source"]["document_id"] for r in block["rows"])
    assert block["as_of"]["date"] == "2025-03-31" and block["as_of"]["date_kind"] == "valuation" and block["label"]
    assert not coverage.conflicting(o.ws)


def test_one_facet_over_two_dates_is_a_timeline_labelled_with_the_facet_and_no_breakdown():
    o = Office()
    facet = "מקדם הפחתה בשל מגבלות בנייה"
    old = _coefficient(o, o.doc(), "0.8", as_of("2023-03-31"), facet)
    new = _coefficient(o, o.doc(), "0.85", as_of("2025-03-31"), facet)
    (g,) = o.blocks({new})
    assert [r["sid"] for r in g["rows"]] == [new, old] and facet in g["label"]
    assert timeline.breakdown(o.ws, {new}) == []


def test_one_facet_on_two_dates_beside_a_second_facet_on_the_later_date_splits_the_rows_between_the_two_blocks():
    o = Office()
    doc, ver = _unit(o)
    first, second = "מקדם הפחתה בשל מגבלות בנייה", "מקדם הפחתה בשל שיתוף במקרקעין"
    early = _coefficient(o, doc, "0.8", as_of("2023-03-31"), first, version=ver)
    late = _coefficient(o, doc, "0.85", as_of("2025-03-31"), first, version=ver)
    other = _coefficient(o, doc, "0.9", as_of("2025-03-31"), second, version=ver)
    (comp,) = timeline.breakdown(o.ws, {late})
    assert [r["sid"] for r in comp["rows"]] == [late, other]
    (g,) = o.blocks({late})  # the first facet's timeline holds its other row only, and is still shown with one row
    assert [r["sid"] for r in g["rows"]] == [early] and first in g["label"]
    assert timeline.row_ids([g]).isdisjoint(timeline.row_ids([comp])), "a row belongs to exactly one block"
    assert o.blocks({other}) == [], "the second facet has no other row: no timeline block"
    assert timeline.row_ids(timeline.build(o.ws, {late, other}) + timeline.breakdown(o.ws, {late, other})) == {early, late, other}


def test_ae4_rows_of_one_class_show_each_their_own_wording_and_the_label_the_first_rows():
    o = Office()
    doc, ver = _unit(o)
    plural = _coefficient(o, doc, "0.8", as_of("2025-03-31"), 'מקדמי הפחתה לנכס "מושע"', version=ver)
    singular = _coefficient(o, o.doc(), "0.8", as_of("2024-03-31"), "מקדם הפחתה לנכס מושע")  # same datum, another spelling
    other = _coefficient(o, doc, "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    (comp,) = timeline.breakdown(o.ws, {plural})
    assert {r["sid"]: r["facet"] for r in comp["rows"]} == {plural: 'מקדמי הפחתה לנכס "מושע"',
                                                              other: "מקדם הפחתה בשל מגבלות בנייה"}
    (g,) = o.blocks({plural})
    assert [r["sid"] for r in g["rows"]] == [singular]
    both = timeline.build(o.ws, {singular, plural})  # the same datum on two dates, neither in a components block here
    assert both and "מקדם הפחתה לנכס מושע" in both[0]["label"]


def test_the_label_of_a_class_shows_the_first_rows_written_facet_in_server_order():
    o = Office()
    new = _coefficient(o, o.doc(), "0.8", as_of("2025-03-31"), 'מקדמי הפחתה לנכס "מושע"')
    old = _coefficient(o, o.doc(), "0.8", as_of("2022-03-31"), "מקדם הפחתה לנכס מושע")
    (g,) = o.blocks({new})
    assert [r["sid"] for r in g["rows"]] == [new, old]
    assert 'מקדמי הפחתה לנכס "מושע"' in g["label"] and "מקדם הפחתה לנכס מושע" not in g["label"]


def test_a_row_whose_source_states_no_facet_shows_that_it_names_none():
    o = Office()
    doc, ver = _unit(o)
    named = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    bare = _coefficient(o, doc, "0.9", as_of("2025-03-31"), None, version=ver)
    unread = o.value(doc, "0.95", as_of("2025-03-31"), kind="coefficient", unit="ratio", version=ver,
                     facet=datum.not_read(), section="3. מקדמים")
    (comp,) = timeline.breakdown(o.ws, {named})
    facets = {r["sid"]: r["facet"] for r in comp["rows"]}
    assert facets[bare] == facets[unread] == timeline.MSG_NO_FACET == "רכיב לא צוין במקור"
    assert facets[named] == "מקדם הפחתה בשל מגבלות בנייה"


def test_an_undecided_pair_in_one_unit_is_a_components_row_with_the_mark_and_its_counterpart():
    o = Office()
    doc, ver = _unit(o)
    a = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    b = _coefficient(o, doc, "0.9", as_of("2025-03-31"), None, version=ver)  # no facet: not comparable with a stated one
    (comp,) = timeline.breakdown(o.ws, {a})
    rows = {r["sid"]: r for r in comp["rows"]}
    # the mark is on the unresolved side: the row without a facet, naming the stated one (round 13 follow-up)
    assert rows[a]["marks"] == [] and "undecided_with" not in rows[a]
    assert rows[b]["marks"] == ["undecided"] and rows[b]["undecided_with"] == [a]
    assert timeline.MARKS["undecided"] in timeline.plain_text([], [comp])


def test_two_equal_numbers_of_two_facets_stay_two_rows_of_the_breakdown():
    o = Office()
    doc, ver = _unit(o)
    a = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    b = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין", version=ver)
    (comp,) = timeline.breakdown(o.ws, {a})
    assert [r["sid"] for r in comp["rows"]] == [a, b] and comp["rows"][0]["value_text"] == comp["rows"][1]["value_text"]


def test_a_breakdown_nobody_cited_is_not_built_and_the_dates_of_two_units_are_not_mixed():
    o = Office()
    d1, v1 = _unit(o)
    d2, v2 = _unit(o)
    _coefficient(o, d1, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=v1)
    _coefficient(o, d1, "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין", version=v1)
    lone = _coefficient(o, d2, "0.7", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=v2)
    assert timeline.breakdown(o.ws, {lone}) == [] and timeline.breakdown(o.ws, set()) == []


def test_plain_text_copies_the_breakdown_after_the_timeline_under_its_heading():
    o = Office()
    doc, ver = _unit(o)
    a = _coefficient(o, doc, "0.8", as_of("2025-03-31"), "מקדם הפחתה בשל מגבלות בנייה", version=ver)
    b = _coefficient(o, doc, "0.9", as_of("2025-03-31"), "מקדם הפחתה בשל שיתוף במקרקעין", version=ver)
    p = timeline.payload(o.ws, {a, b})
    assert p["timeline"] == [] and len(p["breakdown"]) == 1
    lines = p["text"].splitlines()
    assert lines[0] == timeline.BREAKDOWN_HEADING == "רכיבי השומה"
    rows = [ln for ln in lines if ln.startswith("- ")]
    assert len(rows) == 2 and "מקדם הפחתה בשל מגבלות בנייה" in rows[0] and "0.9" in rows[1] and "31/03/2025" in lines[1]
    assert timeline.plain_text([], []) == ""
    both = timeline.plain_text(o.blocks({a}), p["breakdown"])
    assert both == timeline.plain_text([], p["breakdown"])  # no timeline block: the copy is the breakdown alone


def test_ae3_each_conflicting_row_names_its_counterpart_even_in_another_breakdown_block():
    """Round 13 U6: two appraisals of one property state the same consideration as 0.8 and 0.85 at one date, each beside another
    coefficient, so each conflicting row is in its own appraisal's components block. The mark names the other row's sid."""
    o = Office()
    d1, v1 = _unit(o)
    d2, v2 = _unit(o)
    facet, other = "מקדם הפחתה בשל מגבלות בנייה", "מקדם התאמה בשל שיתוף במקרקעין"
    a1 = _coefficient(o, d1, "0.8", as_of("2025-03-31"), facet, version=v1)
    b1 = _coefficient(o, d1, "0.9", as_of("2025-03-31"), other, version=v1)
    a2 = _coefficient(o, d2, "0.85", as_of("2025-03-31"), facet, version=v2)
    b2 = _coefficient(o, d2, "0.9", as_of("2025-03-31"), other, version=v2)
    assert o.blocks({a1, a2}) == []
    blocks = timeline.breakdown(o.ws, {a1, a2})
    assert [[r["sid"] for r in b["rows"]] for b in blocks] == [[a1, b1], [a2, b2]]
    rows = {r["sid"]: r for b in blocks for r in b["rows"]}
    assert "conflict" in rows[a1]["marks"] and rows[a1]["conflict_with"] == [a2]
    assert "conflict" in rows[a2]["marks"] and rows[a2]["conflict_with"] == [a1]
    for equal in (b1, b2):  # 0.9 = 0.9: no conflict, and no counterpart key at all
        assert "conflict" not in rows[equal]["marks"] and "conflict_with" not in rows[equal]
    # the 0.8 / 0.9 pair across the appraisals is undecided as a pair, but its classes are decided different (a1 against b1, side by
    # side in one table), so no row carries an undecided mark
    assert all("undecided" not in r["marks"] and "undecided_with" not in r for r in rows.values())
    assert timeline.buckets(o.ws, {a1})[0].undecided() == []
    text = timeline.plain_text([], blocks)
    assert f"{timeline.MARKS['conflict']} ({a2})" in text and f"{timeline.MARKS['conflict']} ({a1})" in text


def test_a_timeline_row_in_a_conflict_names_every_counterpart_and_a_clean_row_names_none():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2025-01-01"))
    b = o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    c = o.value(o.doc(), "2,200,000", as_of("2025-01-01"))
    old = o.value(o.doc(), "1,900,000", as_of("2022-01-01"))
    (g,) = o.blocks({a})
    rows = {r["sid"]: r for r in g["rows"]}
    assert rows[a]["conflict_with"] == [b, c] and rows[b]["conflict_with"] == [a, c]
    assert "conflict_with" not in rows[old] and rows[old]["marks"] == []
    assert f"{timeline.MARKS['conflict']} ({b}, {c})" in timeline.plain_text([g])


def test_a_breakdown_of_four_own_facet_rates_in_two_sections_and_two_bare_rates_marks_only_the_bare_rows():
    """Round 13 follow-up: own facets of one appraisal unit are different in any section, and an undecided pair is marked on the
    side whose facet is unresolved, which names its counterparts."""
    o = Office()
    doc, ver = _unit(o)
    when = as_of("2025-03-31")

    def rate(amount: str, written: str | None, section: str) -> str:
        facet = datum.facet(written, "sentence", "own") if written else datum.none()
        return o.value(doc, amount, when, kind="rate", unit="percent", version=ver, facet=facet, section=section)

    stated = [rate("6", "התזרים דמי שכירות ממשרדים", "3. היוון"), rate("7", "התזרים מחניות", "3. היוון"),
              rate("8", "התזרים ממחסנים", "5. סיכום"), rate("9", "התזרים משלטי פרסום", "5. סיכום")]
    bare = [rate("10", None, "5. סיכום"), rate("11", None, "3. היוון")]
    (block,) = timeline.breakdown(o.ws, set(stated))
    rows = {r["sid"]: r for r in block["rows"]}
    assert set(rows) == {*stated, *bare}
    assert all(rows[s]["marks"] == [] and "undecided_with" not in rows[s] for s in stated), "resolved own facets: no mark"
    for b in bare:
        assert rows[b]["marks"] == ["undecided"]
        assert set(rows[b]["undecided_with"]) >= set(stated)  # every stated rate; the other bare rate too (both unresolved)
    assert not coverage.conflicting(o.ws)


def _rate(o: Office, doc, ver, amount: str, written: str, scenario: str, *, when: dict | None = None) -> str:
    return o.value(doc, amount, when or as_of("2025-03-31"), kind="rate", unit="percent", version=ver,
                   facet=datum.facet(written, "sentence", "own"), section="3. היוון", scenario=scenario,
                   provenance={"scenario": "source"})


def test_a_scenario_that_restates_the_facet_is_no_part_of_the_key_so_the_rates_form_one_components_block():
    """Round 13 follow-up: the real model registers the cash-flow phrase as the scenario. It is in the quote (so stated), but the
    facet already carries it: the base key must not split one rate per scenario."""
    o = Office()
    doc, ver = _unit(o)
    a = _rate(o, doc, ver, "6", "התזרים דמי שכירות ממשרדים", "תזרים דמי שכירות ממשרדים")
    b = _rate(o, doc, ver, "7", "התזרים מחניות", "תזרים מחניות")
    assert all(("scenario", "") in d.fields for d in timeline.datums(o.ws)), "the restating scenario is not in the key"
    (block,) = timeline.breakdown(o.ws, {a})
    assert [r["sid"] for r in block["rows"]] == [a, b]
    assert [r["facet"] for r in block["rows"]] == ["התזרים דמי שכירות ממשרדים", "התזרים מחניות"]


def test_a_scenario_that_names_something_the_facet_does_not_still_splits_the_key():
    o = Office()
    doc, ver = _unit(o)
    a = _rate(o, doc, ver, "6", "התזרים דמי שכירות ממשרדים", "תזרים דמי שכירות ממשרדים")
    stage = _rate(o, doc, ver, "7", "התזרים מחניות", "תזרים מחניות בשלב ב")  # "שלב ב" names a stage the facet lacks
    assert timeline.restates("תזרים דמי שכירות ממשרדים", "proportion", datum.facet("התזרים דמי שכירות ממשרדים", "row", "own"))
    assert not timeline.restates("תזרים מחניות בשלב ב", "proportion", datum.facet("התזרים מחניות", "row", "own"))
    assert not timeline.restates("שלב ב", "proportion", None) and not timeline.restates("", "proportion", datum.none())
    assert timeline.breakdown(o.ws, {a}) == [] and timeline.breakdown(o.ws, {stage}) == []


def test_of_duplicate_registrations_the_cited_one_is_the_row_and_the_block_is_built():
    """Round 13 follow-up: V1 (uncited) and V3 (its cited duplicate, registered again after a note) are one row: V3's."""
    o = Office()
    doc, ver = _unit(o)
    first, second = "מקדם הפחתה בשל מגבלות בנייה", "מקדם הפחתה בשל שיתוף במקרקעין"
    v1 = _coefficient(o, doc, "0.8", as_of("2025-03-31"), first, version=ver)
    v2 = _coefficient(o, doc, "0.9", as_of("2025-03-31"), second, version=ver)
    v3 = _coefficient(o, doc, "0.8", as_of("2025-03-31"), first, version=ver)  # the same figure registered again
    (block,) = timeline.breakdown(o.ws, {v3})
    assert [r["sid"] for r in block["rows"]] == [v2, v3]  # registration order, with the cited duplicate
    assert v1 not in {r["sid"] for r in block["rows"]}
    (uncited,) = timeline.breakdown(o.ws, {v1})
    assert v1 in {r["sid"] for r in uncited["rows"]} and v3 not in {r["sid"] for r in uncited["rows"]}
    assert [d.ref for d in timeline._dedupe(timeline.datums(o.ws))] == [v1, v2]  # nothing cited: the first registration
