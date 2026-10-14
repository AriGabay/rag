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

    def doc(self, address: str | None = "addr:הצאלון 7", title: str = "שומה סינתטית") -> uuid.UUID:
        d = uuid.uuid4()
        self.property[str(d)] = address
        self.titles = getattr(self, "titles", {}) | {d: title}
        return d

    def value(self, doc: uuid.UUID, amount: str, when: dict | None, *, kind: str = "value", unit: str = "ILS",
              period: str = "none", vat: str = "included", area_basis: str = "", provenance: dict | None = None,
              title: str | None = None, **kw) -> str:
        vid = f"V{len(self.ws.values) + 1}"
        prov = SOURCE | (provenance or {})
        v = calc.Value(vid, Decimal(amount.replace(",", "")), amount, "S1", doc, uuid.uuid4(), "r1",
                       title or self.titles.get(doc, "שומה סינתטית"), "סעיף 3", "שווי הנכס", kind, unit, period, vat,
                       area_basis, "הנכס", "appraiser_determination", prov, {"quote": f"{amount} ₪"}, f"{amount} ₪",
                       stance="adopted", **kw)
        v.as_of = when
        self.ws.values[vid] = v
        return vid

    def measurement(self, doc: uuid.UUID, amount: str, when: dict | None, *, metric_kind: str = "rent", unit: str = "ILS",
                    period: str = "month", vat: str = "included", area_basis: str = "", stance: str = "adopted") -> str:
        mid = f"M{len(self.ws.measurements) + 1}"
        row = SimpleNamespace(id=uuid.uuid4(), metric="דמי שכירות", metric_kind=metric_kind, value=Decimal(amount.replace(",", "")),
                              value_text=f"{amount} ₪", unit=unit, period=period, vat=vat, area_basis=area_basis,
                              subject="הנכס", scenario="", stance=stance, status="auto_validated", quote="x",
                              section="סעיף 4", block_index=1, table_index=None)
        m = Measurement(mid, row.id, doc, uuid.uuid4(), self.titles.get(doc, "שומה סינתטית"), row)
        m.as_of = when
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
    stray = o.value(o.doc("addr:האלון 3"), "1,000,000", as_of("2023-01-01"))  # another property
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
    a = o.value(o.doc("addr:הצאלון 7"), "2,000,000", as_of("2024-01-01"))
    o.value(o.doc("addr:הצאלון 7 קומה 2"), "2,100,000", as_of("2025-01-01"))
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

def test_a_single_dated_value_gives_no_block_and_two_values_with_one_dated_give_none():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    assert o.blocks({a}) == []
    o.value(o.doc(), "2,100,000", None)
    assert o.blocks({a}) == []


def test_undated_values_follow_the_dated_ones_with_the_unknown_date_mark():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024-01-01"))
    b = o.value(o.doc(), "2,100,000", as_of("2025-01-01"))
    u = o.value(o.doc(), "2,050,000", None)
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
    f = o.value(o.doc(), "3,000,000", as_of(None, forecast=True))
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
    u = o.value(o.doc(), "2,050,000", None)
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
    assert p == {"mode": "none", "prefer_recent": False, "requested": None, "timeline": [], "text": None}


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
    o.value(o.doc("addr:האלון 3"), "2,600,000", as_of("2025-06-01"))
    o.value(o.doc(), "2,700,000", as_of("2025-06-01"), vat="excluded")
    assert timeline.later_than(o.ws, [a]) == []


def test_a_month_is_later_than_the_year_before_it_but_not_than_a_year_that_holds_it():
    o = Office()
    a = o.value(o.doc(), "2,000,000", as_of("2024", "year", provenance="source"))
    o.value(o.doc(), "2,100,000", as_of("2024-06", "month", provenance="source"))
    assert timeline.later_than(o.ws, [a]) == []
    b = o.value(o.doc(), "2,200,000", as_of("2025-02", "month", provenance="source"))
    assert [later.ref for _, later in timeline.later_than(o.ws, [a])] == [b]
