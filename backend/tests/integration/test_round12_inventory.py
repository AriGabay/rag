"""The inventory of a property's appraisals, the focused per-unit search and the coverage record (round 12 U7: R9-R14, R20;
AE3, AE4; KTD6, KTD7).

In a ``recent`` or ``explicit`` turn about an identified property the server lists the property's appraisals and their
valuation dates before it searches (``tools.property_inventory``), searches each selected unit on its own (cap eight, two
slots for undated ones), ranks per (version, unit) and records what it checked (``Workspace.time_coverage``). Fixtures are
the synthetic round-12 ones: single appraisals uploaded through the worker, and files of several units built with
``tests.support.unit_files`` (a DOCX is one unit). Every name, address and amount is invented."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.chat import property as P
from app.chat import timeframe as TF
from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_property_scope import (
    DOCS12,
    FIXTURES,
    focused,
    office12,  # noqa: F401 - the fixture of the round-12 relations (parcels, cities, apartments)
    order,
    unfocused,
    upload,
    version_of,
)
from tests.integration.test_tools_pools import run, sources_of
from tests.support.unit_files import unit_file

# ruff: noqa: F811 - the tests take the imported ``office12`` fixture by name
pytestmark = pytest.mark.db

ANCHOR = date(2026, 10, 14)
RECENT = TimeMode("recent", "switch", ANCHOR, None, True)
NONE = TimeMode("none", "switch", ANCHOR, None, False)
WINDOW = [f"window_{n:02d}" for n in range(1, 15)] + ["window_latest"]
WINDOW_QUERY = "שווי הנכס הערבה 2"


@pytest.fixture
def window_office(db, client, monkeypatch):
    """Fifteen appraisals of one property (the beyond-window fixtures): fourteen state "שווי הנכס", the 2026 one states its
    amount in other words."""
    o = make_office(db, "משרד סינתטי חלון", "admin-r12w@example.test")
    login(client, "admin-r12w@example.test")
    o.client = client
    o.ids = {k: upload(o, FIXTURES / DOCS12[k]["file"], DOCS12[k]["source_kind"], monkeypatch) for k in WINDOW}
    return o


def test_ae3_the_latest_of_fifteen_appraisals_beyond_the_relevance_window_is_found_and_first(window_office):
    """Fourteen linked appraisals match the query words better than the 2026 one (fifteen in all, more than the 12-document
    window). The assertion is unconditional: the 2026 appraisal's hit is first and its coverage status is ``hit``."""
    ids = window_office.ids
    ws = focused(window_office, RECENT, ids["window_01"])
    out = run(ws, "search", query=WINDOW_QUERY, limit=6)
    got = order(out)
    assert got[0] == ids["window_latest"], "the latest appraisal answers first: it is found though it ranks 15th by relevance"
    record = ws.time_coverage
    units = {u["document_id"]: u for u in record["units"]}
    assert units[ids["window_latest"]]["status"] == T.LOCATED
    assert [u["document_id"] for u in record["units"]][0] == ids["window_latest"], "the inventory lists the newest first"
    assert sum(u["status"] == T.LOCATED for u in record["units"]) == 8 and sum(
        u["status"] == T.NOT_SEARCHED_CAP for u in record["units"]) == 7, "eight units are read, the seven oldest are not"
    assert record["complete"] and record["property"]["reasons"] == ["parcel 8801/9"] and record["property"]["units"] == 15


# --- fixtures of one property built with ``unit_file`` -------------------------------------------------------------------

CITY = "עיר: עיר-סינתטית"
PROPERTY = [CITY, "כתובת הנכס: הצאלון 7", "גוש: 7101 חלקה: 15"]
QUERY = "שווי הנכס ברחוב הצאלון 7"
RENT_QUERY = "דמי שכירות"


@pytest.fixture
def office(db):
    return make_office(db, "משרד סינתטי מלאי", "admin-r12i@example.test")


def appraisal(office, key: str, valuation: str | None, identification: list[str] | None = None,
              rent: bool = True, place: str | None = "הצאלון 7", group=None) -> str:
    """A one-unit appraisal of the property (``PROPERTY`` identification unless given) whose unit states ``valuation``
    (None: no valuation date). Its value sentence matches ``QUERY`` and names the street ``place`` (None: no street, so a
    unit with no identification line states no identity at all); its rent sentence matches ``RENT_QUERY`` unless ``rent``
    is False. Returns its document id."""
    blocks = ["מסמך סינתטי — נתונים בדויים", *(PROPERTY if identification is None else identification),
              *([f"המועד הקובע: {valuation}"] if valuation else []),
              f"שווי הנכס{f' ברחוב {place}' if place else ''} הוא 2,450,000 ₪ (סינתטי).",
              *(["דמי שכירות חודשיים 8,000 ₪ (סינתטי)."] if rent else [])]
    doc, _ = unit_file(office, group or office.default_group_id, f"מסמך {key}", [{"title": f"מסמך {key}", "blocks": blocks}],
                       sha=hashlib.sha256(key.encode()).hexdigest())
    return doc


def dates_of(ws) -> list[str | None]:
    """The inventory dates of the turn's coverage, in the order the record lists the units."""
    return [u["date"] for u in ws.time_coverage["units"]]


def status_of(ws) -> dict[str, str]:
    return {u["document_id"]: u["status"] for u in ws.time_coverage["units"]}


def test_a_compendium_of_one_property_is_six_groups_ordered_by_date_never_one_group_for_the_file(office):
    from tests.integration.test_property_scope import compendium

    doc, first = compendium(office, "compendium_one")
    ws = focused(office, RECENT, doc, first)
    out = run(ws, "search", query="שווי הנכס", limit=6)
    lines = list(dict.fromkeys(s.date_line for s in ws.sources.values() if s.document_id and str(s.document_id) == doc))
    expected = [T.MSG_DATE_VALUATION.format(date=d) for d in
                ("30/06/2025", "30/06/2024", "30/06/2023", "30/06/2022", "30/06/2021", "30/06/2020")]
    assert lines == expected, "six units of one file, each dated by its own unit, newest first"
    assert T.MSG_PROPERTY_SEARCHED in out
    record = ws.time_coverage
    assert [u["date"] for u in record["units"]] == ["2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30", "2021-06-30",
                                                     "2020-06-30"]
    assert {u["status"] for u in record["units"]} == {T.LOCATED} and record["complete"] and not record["blocking"]


def test_ae4_ten_dated_units_with_cap_eight_are_complete_for_latest_when_the_two_skipped_are_older_than_the_newest_hit(office):
    docs = {year: appraisal(office, f"cap-{year}", f"30/06/{year}") for year in range(2015, 2025)}
    ws = focused(office, RECENT, docs[2018])
    out = run(ws, "search", query=QUERY, limit=6)
    assert order(out)[0] == docs[2024]
    record = ws.time_coverage
    assert record["scope"] == "property" and record["identified"] and record["via"] == "focus" and len(record["units"]) == 10
    status = status_of(ws)
    assert [status[docs[y]] for y in range(2024, 2016, -1)] == [T.LOCATED] * 8
    assert status[docs[2016]] == status[docs[2015]] == T.NOT_SEARCHED_CAP, "the skipped two are the oldest"
    assert record["complete"] and record["blocking"] == [], "older units left unsearched do not make the latest partial"
    assert not any(u["blocks"] for u in record["units"])


def test_ae4_an_undated_unit_that_was_not_searched_keeps_the_record_partial_and_is_named(office):
    dated = {y: appraisal(office, f"und-{y}", f"30/06/{y}") for y in range(2018, 2025)}  # seven dated
    undated = [appraisal(office, f"und-none-{n}", None) for n in range(3)]  # three undated: one is left out by the cap
    ws = focused(office, RECENT, dated[2020])
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    status = status_of(ws)
    assert sum(status[d] == T.NOT_SEARCHED_CAP for d in undated) == 1 and sum(status[d] == T.LOCATED for d in undated) == 2
    assert status[dated[2018]] == T.NOT_SEARCHED_CAP and all(status[dated[y]] == T.LOCATED for y in range(2019, 2025))
    blocking_units = {u["document_id"] for u in record["units"] if u["blocks"]}
    assert blocking_units == set(undated), "every undated unit is named; the older dated unit left out is not"
    assert not record["complete"] and "undated" in record["blocking"]
    assert all(u["undated"] and u["date"] is None for u in record["units"] if u["document_id"] in undated)


def test_a_searched_undated_unit_still_keeps_the_record_from_being_complete(office):
    a = appraisal(office, "und1-a", "30/06/2022")
    b = appraisal(office, "und1-b", "30/06/2024")
    c = appraisal(office, "und1-c", None)
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert status_of(ws) == {a: T.LOCATED, b: T.LOCATED, c: T.LOCATED} and dates_of(ws) == ["2024-06-30", "2022-06-30", None]
    assert not record["complete"] and record["blocking"] == ["undated"]


def test_the_newest_unit_searched_without_a_match_makes_recent_partial_and_names_its_date(office):
    old = appraisal(office, "nm-old", "30/06/2022")
    mid = appraisal(office, "nm-mid", "30/06/2023")
    newest = appraisal(office, "nm-new", "30/06/2025", rent=False)
    ws = focused(office, RECENT, old)
    out = run(ws, "search", query=RENT_QUERY, limit=6)
    assert order(out) == [mid, old], "the newest appraisal states no rent: the hits are the older ones, newest first"
    record = ws.time_coverage
    assert status_of(ws) == {newest: T.NO_MATCH, mid: T.LOCATED, old: T.LOCATED}
    assert not record["complete"] and record["blocking"] == ["newer_no_match"]
    assert [(u["document_id"], u["date"]) for u in record["units"] if u["blocks"]] == [(newest, "2025-06-30")]


def explicit(start: str, precision: str = "year") -> TimeMode:
    return TimeMode("explicit", "question", ANCHOR, Requested("point", start, start, precision, f"ב-{start}"), True)


def test_explicit_2022_orders_the_2022_unit_then_the_later_ones_then_the_undated_then_the_earlier(office):
    early = appraisal(office, "ex-2019", "30/06/2019")
    in_range = appraisal(office, "ex-2022", "30/06/2022")
    later = appraisal(office, "ex-2024", "30/06/2024")
    undated = appraisal(office, "ex-none", None)
    ws = focused(office, explicit("2022"), later)
    out = run(ws, "search", query=QUERY, limit=6)
    assert [u["document_id"] for u in ws.time_coverage["units"]] == [in_range, later, undated, early]
    assert order(out)[:2] == [in_range, later]
    assert not ws.time_coverage["complete"], "the undated unit remains"


def test_explicit_is_complete_when_every_unit_that_can_hold_the_range_was_searched_and_the_earlier_unsearched_do_not_matter(office):
    docs = {y: appraisal(office, f"exc-{y}", f"30/06/{y}") for y in (2022, 2024)}
    older = [appraisal(office, f"exc-old-{n}", f"30/06/{2010 + n}") for n in range(8)]  # eight earlier units: cap leaves them
    ws = focused(office, explicit("2022"), docs[2022])
    run(ws, "search", query=QUERY, limit=6)
    status = status_of(ws)
    assert status[docs[2022]] == status[docs[2024]] == T.LOCATED
    assert sum(status[o] == T.NOT_SEARCHED_CAP for o in older) == 2, "eight slots: the two in or after the range and six earlier"
    assert ws.time_coverage["complete"] and ws.time_coverage["blocking"] == []


def test_explicit_is_partial_when_a_later_unit_was_not_searched(office):
    in_range = [appraisal(office, f"exp-{n}", f"{n + 1:02d}/03/2022") for n in range(8)]  # eight units dated in 2022
    later = appraisal(office, "exp-2024", "30/06/2024")
    ws = focused(office, explicit("2022"), in_range[0])
    run(ws, "search", query=QUERY, limit=6)
    status = status_of(ws)
    assert status[later] == T.NOT_SEARCHED_CAP, "eight units are read: the eight of 2022 come first, the 2024 one is the ninth"
    record = ws.time_coverage
    assert not record["complete"] and "range_not_searched" in record["blocking"]
    assert later in {u["document_id"] for u in record["units"] if u["blocks"]}


def test_r13_a_focus_unit_with_no_identification_is_searched_alone_and_the_record_says_it_is_not_identified(office):
    focus = appraisal(office, "ni-focus", "30/06/2022", identification=[], place=None)
    other = appraisal(office, "ni-other", "30/06/2025")
    for time in (RECENT, explicit("2022")):
        ws = focused(office, time, focus)
        out = run(ws, "search", query=QUERY, limit=6)
        assert set(order(out)) == {focus} and T.MSG_PROPERTY_SEARCHED not in out and other not in order(out)
        record = ws.time_coverage
        assert record["scope"] == "unit" and record["identified"] is False and record["property"] is None
        assert [u["document_id"] for u in record["units"]] == [focus] and record["units"][0]["status"] == T.LOCATED
        assert not record["complete"] and "not_identified" in record["blocking"]


def test_a_visible_newer_appraisal_with_no_identity_facts_and_no_mention_is_unlinkable_and_blocks_nothing(office):
    """Round 13 U8 (R18, KTD15; round 12 counted every such unit as undetermined and made the record partial): an
    appraisal that states no identity and does not mention the property is counted apart and the record stays complete."""
    a = appraisal(office, "ud-a", "30/06/2022")
    appraisal(office, "ud-newer", "30/06/2025", identification=[], place=None)
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert record["v"] == T.COVERAGE_V == 2 and "undetermined" not in record
    assert (record["unlinkable"], record["excluded"], record["possible"]["count"]) == (1, 0, 0)
    assert record["candidates"] == {"named": [], "count": 0}
    assert [u["document_id"] for u in record["units"]] == [a] and record["complete"] and record["blocking"] == []
    assert TF.coverage_note(record).splitlines() == [TF.MSG_NOTE_COMPLETE.format(n=1), f"{TF.MSG_GROUP_UNLINKABLE}: 1."]


def test_an_appraisal_of_the_same_parcel_without_a_sub_parcel_is_a_named_candidate_and_the_record_is_partial(office):
    from app.chat.property import REASON_SUB_MISSING

    focus = appraisal(office, "cd-focus", "30/06/2022", identification=[CITY, "כתובת הנכס: הצאלון 7", "גוש: 7101 חלקה: 15 תת חלקה: 3"])
    bare = appraisal(office, "cd-bare", "30/06/2025", identification=[CITY, "כתובת הנכס: הצאלון 7", "גוש: 7101 חלקה: 15"])
    ws = focused(office, RECENT, focus)
    out = run(ws, "search", query=QUERY, limit=6)
    assert set(order(out)) == {focus}
    record = ws.time_coverage
    assert record["candidates"]["count"] == 1
    named = record["candidates"]["named"][0]
    assert (named["document_id"], named["reason"], named["date"]) == (bare, REASON_SUB_MISSING, "2025-06-30")
    assert not record["complete"] and record["blocking"] == ["candidates"]


def test_a_question_naming_an_address_no_stored_identifier_matches_but_an_identity_fact_does_identifies_the_property(office):
    a = appraisal(office, "q-a", "30/06/2022")
    b = appraisal(office, "q-b", "30/06/2025")
    other = appraisal(office, "q-other", "30/06/2026", identification=[CITY, "כתובת הנכס: הצאלון 11", "גוש: 7101 חלקה: 16"],
                      place="הצאלון 11")
    ws = unfocused(office, RECENT)
    ws.user_messages = [{"turn": 1, "text": "מה היה השווי של הנכס ברחוב הצאלון 7?", "current": True}]
    out = run(ws, "search", query=QUERY, limit=6)
    assert order(out) == [b, a] and other not in order(out) and T.MSG_PROPERTY_SEARCHED in out
    record = ws.time_coverage
    assert (record["scope"], record["via"], record["identified"]) == ("property", "question", True)
    assert {u["document_id"] for u in record["units"]} == {a, b} and record["complete"]


def test_a_question_naming_an_address_of_two_cities_identifies_no_property(office):
    appraisal(office, "q2-x", "30/06/2022")
    appraisal(office, "q2-y", "30/06/2025", identification=["עיר: עיר-סינתטית-ב", "כתובת הנכס: הצאלון 7", "גוש: 7102 חלקה: 15"])
    ws = unfocused(office, RECENT)
    ws.user_messages = [{"turn": 1, "text": "מה היה השווי של הנכס ברחוב הצאלון 7?", "current": True}]
    out = run(ws, "search", query=QUERY, limit=6)
    assert T.MSG_PROPERTY_SEARCHED not in out
    assert ws.time_coverage["scope"] == "window" and not ws.time_coverage["complete"]


def test_documents_of_one_property_named_by_the_call_are_the_property_via_documents(office):
    a = appraisal(office, "nd-a", "30/06/2022")
    b = appraisal(office, "nd-b", "30/06/2025")
    ws = unfocused(office, RECENT)
    out = run(ws, "search", query=QUERY, limit=6, document_ids=[a])
    assert order(out) == [b, a]
    assert (ws.time_coverage["via"], ws.time_coverage["scope"], ws.time_coverage["complete"]) == ("documents", "property", True)


def test_a_window_without_an_identified_property_records_the_window_and_is_never_complete(office):
    a = appraisal(office, "w-a", "30/06/2022")
    ws = unfocused(office, RECENT)
    out = run(ws, "search", query=QUERY, limit=6)
    assert order(out) == [a]
    record = ws.time_coverage
    assert record["scope"] == "window" and record["identified"] is False and not record["complete"]
    assert record["blocking"] == ["window"] and [u["document_id"] for u in record["units"]] == [a]


def test_off_mode_records_no_coverage_and_searches_as_in_round_11(office):
    a = appraisal(office, "off-a", "30/06/2022")
    b = appraisal(office, "off-b", "30/06/2025")
    ws = focused(office, NONE, a)
    out = run(ws, "search", query=QUERY, limit=6)
    assert set(order(out)) == {a} and T.MSG_PROPERTY_SEARCHED not in out and b not in order(out)
    assert ws.time_calls == [] and ws.time_coverage is None and T.coverage_for(ws) is None
    unfocused_ws = unfocused(office, NONE)
    run(unfocused_ws, "search", query=QUERY, limit=6)
    assert unfocused_ws.time_calls == [] and unfocused_ws.time_coverage is None


def test_exact_limit_keeps_the_search_only_cut_per_unit(office):
    docs = [appraisal(office, f"ex-lim-{y}", f"30/06/{y}") for y in range(2018, 2025)]
    ws = focused(office, RECENT, docs[0])
    ws.exact_limit = True
    run(ws, "search", query=QUERY, limit=3)
    shown = {s.document_id for s in ws.sources.values()}
    assert len(ws.sources) <= 3 and len(shown) <= 3, "a search-only call shows at most `limit` hits"
    assert len(ws.time_coverage["units"]) == 7, "the inventory still covers the whole property"


def test_two_calls_of_one_turn_keep_the_coverage_of_the_call_that_produced_each_datum(office):
    docs = {y: appraisal(office, f"two-{y}", f"30/06/{y}") for y in range(2015, 2025)}  # ten units: the first call reads eight
    ws = focused(office, RECENT, docs[2020])
    run(ws, "search", query=QUERY, limit=6)
    first_sids = set(ws.time_calls[0].sids)
    ver, reading = (version_of(office, docs[2015]))
    oldest = T.unit_handle_of(ws, docs[2015], ver, reading, 1)
    run(ws, "search", query=QUERY, limit=6, unit=oldest)  # a call naming another unit: that unit alone
    second_sids = set(ws.time_calls[1].sids) - first_sids
    assert first_sids and second_sids
    one = T.coverage_for(ws, first_sids)
    two = T.coverage_for(ws, second_sids)
    assert one["scope"] == "property" and len(one["units"]) == 10 and one["complete"] and "calls" not in one
    assert two["scope"] == "unit" and [u["document_id"] for u in two["units"]] == [docs[2015]] and not two["complete"]
    both = T.coverage_for(ws, first_sids | second_sids)
    assert both["calls"] == 2 and "several_scopes" in both["blocking"] and not both["complete"]
    assert T.coverage_for(ws, {"S999"})["calls"] == 2, "no cited call: every call of the turn"


# --- pure functions: the cap, the order, the merge --------------------------------------------------------------------

def unit(n: int, date_: str | None, status: str = T.NOT_SEARCHED_CAP) -> T.InventoryUnit:
    return T.InventoryUnit(f"d{n:03d}", f"v{n:03d}", 1, f"t{n}", None if date_ is None else (date_, "day"),
                           "none" if date_ is None else "stated", None, n == 1, status)


@pytest.mark.parametrize("dated, undated, kept_dated, kept_undated", [
    (10, 3, 6, 2), (3, 10, 3, 5), (10, 0, 8, 0), (10, 1, 7, 1), (5, 1, 5, 1), (0, 12, 0, 8)])
def test_the_cap_is_eight_units_with_two_slots_kept_for_undated_ones(dated, undated, kept_dated, kept_undated):
    ordered = [unit(n, f"20{10 + n:02d}-06-30") for n in range(dated)] + [unit(100 + n, None) for n in range(undated)]
    chosen = T.select_units(ordered)
    assert (sum(not u.undated for u in chosen), sum(u.undated for u in chosen)) == (kept_dated, kept_undated)
    assert [u.key for u in chosen] == [u.key for u in ordered if u.key in {c.key for c in chosen}], "the mode's order is kept"
    assert [u for u in chosen if not u.undated] == [u for u in ordered if not u.undated][:kept_dated], "the first dated ones"
    assert [u for u in chosen if u.undated] == [u for u in ordered if u.undated][:kept_undated], "undated in document order"


def record_of(statuses: list[str], dates: list[str | None], mode: TimeMode = RECENT, candidates: int = 0,
              undetermined: int = 0, scope: str = "property") -> dict:
    units = [{"document_id": f"d{n}", "version_id": f"v{n}", "unit_no": 1, "title": f"t{n}", "date": d, "precision": "day",
              "date_status": "stated" if d else "none", "undated": d is None, "status": s, "link": None, "focus": n == 0,
              "blocks": False} for n, (s, d) in enumerate(zip(statuses, dates, strict=True))]
    return T.assess_coverage(mode, {"v": 1, "scope": scope, "identified": scope == "property", "units": units,
                                    "candidates": {"named": [], "count": candidates}, "undetermined": undetermined})


def test_recent_completeness_rule():
    H, N, C = T.LOCATED, T.NO_MATCH, T.NOT_SEARCHED_CAP
    new, mid, old = "2025-06-30", "2023-06-30", "2020-06-30"
    assert record_of([H, H, C], [new, mid, old])["complete"], "the newest holds the newest datum; the oldest is left out"
    assert record_of([H, C, C], [new, mid, old])["complete"]
    assert record_of([H, N], [new, new])["complete"], "a unit tying the newest date is not newer"
    assert not record_of([C, H, H], [new, mid, old])["complete"], "a newer unit not searched"
    partial = record_of([N, H, H], [new, mid, old])
    assert not partial["complete"] and partial["blocking"] == ["newer_no_match"] and partial["units"][0]["blocks"]
    assert not record_of([H, H], [new, None])["complete"], "an undated unit"
    assert not record_of([H], [new], candidates=1)["complete"] and not record_of([H], [new], undetermined=2)["complete"]
    assert not record_of([H], [new], scope="window")["complete"] and not record_of([H], [new], scope="unit")["complete"]
    nothing = record_of([N, N], [new, mid])
    assert not nothing["complete"], "nothing found anywhere is not 'the latest'"


def test_explicit_completeness_rule_reads_the_units_that_can_hold_the_requested_time():
    H, C = T.LOCATED, T.NOT_SEARCHED_CAP
    mode = explicit("2022")
    assert record_of([H, H, C], ["2022-03-31", "2024-03-31", "2019-03-31"], mode)["complete"], "an earlier unit cannot hold 2022"
    assert not record_of([H, C], ["2022-03-31", "2024-03-31"], mode)["complete"], "a later unit may list earlier data"
    assert record_of([H, T.NO_MATCH], ["2022-03-31", "2024-03-31"], mode)["complete"], "searched without a match is searched"


def test_merging_calls_counts_a_unit_as_searched_only_if_every_call_searched_it():
    one = record_of([T.LOCATED, T.LOCATED, T.NOT_SEARCHED_CAP], ["2025-06-30", "2023-06-30", "2020-06-30"])
    two = record_of([T.NOT_SEARCHED_CAP, T.LOCATED, T.LOCATED], ["2025-06-30", "2023-06-30", "2020-06-30"])
    merged = T.merge_coverage(RECENT, [one, two])
    assert [u["status"] for u in merged["units"]] == [T.NOT_SEARCHED_CAP, T.LOCATED, T.NOT_SEARCHED_CAP]
    assert merged["calls"] == 2 and not merged["complete"] and merged["blocking"] == ["newer_not_searched"]
    assert [u["blocks"] for u in merged["units"]] == [True, False, False], "the second call never searched the newest unit"
    assert T.merge_coverage(RECENT, [one]) is one and T.merge_coverage(RECENT, []) is None
    assert one["units"][1]["status"] == T.LOCATED, "the merge does not change the calls' own records"


# --- U12b (S2): the link reason reaches the model --------------------------------------------------------------------------

def test_s2_every_source_of_a_linked_unit_carries_the_servers_link_and_the_search_lists_the_gathered_units_with_their_reasons(
        office12):
    """The property-wide search of the parcel-B appraisal (הדס 5) also returns the appraisal at another house number that shares
    parcel 8401/40 (הדס 3). The model disowned it ("refers to הדס 3, not הדס 5") because the search said only that the search
    covers the property's appraisals: every source of a gathered unit other than the focus now carries the link beside its date
    line, and the search result's property line lists the gathered units with their reasons."""
    ids = office12.ids
    ws = focused(office12, RECENT, ids["parcels_b"])
    out = run(ws, "search", query="שווי הנכס הדס", limit=6)
    linked = [s for s in sources_of(out) if s.get("document_id") == ids["parcels_ab"]]
    focus = [s for s in sources_of(out) if s.get("document_id") == ids["parcels_b"]]
    assert linked and focus
    want = T.MSG_LINK_SAME_PROPERTY.format(why=T.MSG_LINK_PARCEL.format(parcels="8401/40"))
    assert all(s.get("link") == want and s.get("date") for s in linked), [(s.get("link"), s.get("date")) for s in linked]
    assert not any("link" in s for s in focus), "the focus appraisal is the property itself: no link"
    assert all(s.public()["link"] == want for s in ws.sources.values() if str(s.document_id) == ids["parcels_ab"])
    # the property line: every gathered unit, its date and (but for the focus) its reason
    line = next(x for x in out.split("\n\n") if x.startswith(T.MSG_PROPERTY_UNITS_HEAD))
    assert want in line and line.count(want) == 1 and ws.doc_handle(ids["parcels_ab"]) in line and ws.doc_handle(ids["parcels_b"]) in line
    assert "30/06/2023" in line and "30/06/2025" in line
    assert T.MSG_PROPERTY_SEARCHED in out, "the round-11 sentence is kept"


def test_s2_a_unit_gathered_by_address_and_city_says_so_and_a_search_that_gathers_nothing_adds_no_link(office12):
    ids = office12.ids
    assert T.link_phrase("address_city") == T.MSG_LINK_SAME_PROPERTY.format(why=T.MSG_LINK_ADDRESS)
    assert T.link_phrase("parcel 8401/24, 8401/40") == T.MSG_LINK_SAME_PROPERTY.format(
        why=T.MSG_LINK_PARCEL.format(parcels="8401/24, 8401/40"))
    assert T.link_phrase(None) is None and T.link_phrase("") is None
    ws = focused(office12, RECENT, ids["apartments_3"])
    out = run(ws, "search", query="שווי הנכס הנרקיס", limit=6)  # the unit search stands: no gathered neighbour
    assert not any("link" in s for s in sources_of(out)) and T.MSG_PROPERTY_UNITS_HEAD not in out


def test_s2_the_off_mode_search_adds_no_link_and_no_unit_line(office12):
    ids = office12.ids
    ws = focused(office12, NONE, ids["parcels_b"])
    out = run(ws, "search", query="שווי הנכס הדס", limit=6)
    assert not any("link" in s for s in sources_of(out)) and T.MSG_PROPERTY_UNITS_HEAD not in out


# --- a document the user cannot see is no part of the property graph or the coverage record (review: privacy) --------------

def test_a_user_who_cannot_see_one_of_two_linked_appraisals_gets_no_link_no_title_and_no_count_of_it(office):
    """The graph, the link reasons, the candidates and the coverage record are built under the caller's permissions: the
    appraisal of a closed group is neither linked, named as a candidate nor counted, for a user outside the group; a member
    of the group sees both."""
    from tests.factories import make_group, make_user
    from tests.integration.test_tools_pools import workspace

    private = make_group(office, "קבוצה סגורה סינתטית")
    member = make_user(office, "member-r12i@example.test", [office.default_group_id, private])
    outsider = make_user(office, "outsider-r12i@example.test", [office.default_group_id])
    seen = appraisal(office, "visible-linked", "01/01/2022")
    hidden = appraisal(office, "hidden-linked", "01/01/2025", group=private)
    hidden_candidate = appraisal(office, "hidden-cityless", "01/01/2024", identification=[
        "כתובת הנכס: הצאלון 7"], group=private)   # states no city: a candidate, not a link

    def turn(user):
        ws = workspace(office, "property", user=user)
        ws.time = RECENT
        ver, reading = version_of(office, seen)
        ws.unit_focus = {"handle": T.unit_handle_of(ws, seen, ver, reading, 1), "document_id": seen, "unit_id": None}
        return ws, run(ws, "search", query=QUERY, limit=6)

    ws, out = turn(outsider)
    record = json.dumps(ws.time_coverage, ensure_ascii=False)
    for secret in (hidden, hidden_candidate, "hidden-linked", "hidden-cityless"):
        assert secret not in out and secret not in record, secret
    assert order(out) == [seen] and "link=" not in out and T.MSG_PROPERTY_UNITS_HEAD not in out
    assert [u["document_id"] for u in ws.time_coverage["units"]] == [seen]
    assert (ws.time_coverage.get("candidates") or {"count": 0})["count"] == 0
    assert (ws.time_coverage.get("property") or {}).get("units", 1) == 1

    ws, out = turn(member)    # the control: the member of the group sees what the outsider cannot
    assert set(order(out)) >= {seen, hidden} and "link=" in out
    assert {u["document_id"] for u in ws.time_coverage["units"]} == {seen, hidden}


# --- round 13 U8: the three classes of the rest of the office, located versus read (R18-R20; AE9; KTD15, KTD16) -----------

MANIFEST13 = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round13" / "manifest.json")
                        .read_text(encoding="utf-8"))
UNIT_KEYS = ("title", "aliases", "kind", "certainty", "identifiers", "blocks")


def ae9_office(office) -> dict:
    """The synthetic office of AE9 (``manifest["office_ae9"]``): the focus appraisal, one appraisal of another property and
    twenty appraisals with no identity facts, two of which mention the focus street and number (units 7 and 15; the manifest
    lists them as the candidates). Returns the document ids keyed "focus", "other" and the unidentified unit's number."""
    docs = {}
    for spec in MANIFEST13["office_ae9"]["units"]:
        key = {"focus": "focus", "other_property": "other"}.get(spec["role"], spec.get("n"))
        title = spec["title"] or f"שומה בדויה מספר {key}"
        docs[key], _ = unit_file(office, office.default_group_id, title, [{k: spec[k] for k in UNIT_KEYS}],
                                 sha=hashlib.sha256(f"ae9-{key}".encode()).hexdigest())
    return docs


def probe_searches(monkeypatch) -> list:
    """The searches the probe makes: every ``search_passages`` call whose scope names several versions (a per-unit search
    names one)."""
    calls = []
    real = T.search_passages

    def spy(conn, query, limit=8, *, scope=None, window=False):
        calls.append(scope)
        return real(conn, query, limit, scope=scope, window=window)

    monkeypatch.setattr(T, "search_passages", spy)
    return calls


def test_ae9_two_of_twenty_unidentified_units_mention_the_street_and_number(office, monkeypatch):
    """The two units whose opening names the street and number (without a city) are the named candidates; the eighteen with
    nothing to compare are only counted; the appraisal of another property is in the record's count and nowhere else."""
    docs = ae9_office(office)
    calls = probe_searches(monkeypatch)
    ws = focused(office, RECENT, docs["focus"])
    run(ws, "search", query="שטח הנכס", limit=6)
    record = ws.time_coverage
    expected = MANIFEST13["office_ae9"]["expected"]
    candidates = record["candidates"]
    assert candidates["count"] == expected["candidates"] == 2
    assert [c["document_id"] for c in candidates["named"]] == [docs[n] for n in expected["named_candidates"]], "newest first"
    assert [c["date"] for c in candidates["named"]] == ["2022-08-28", "2020-04-28"]
    assert record["unlinkable"] == expected["unlinkable"] == 18 and record["excluded"] == expected["other_properties"] == 1
    assert record["possible"]["count"] == 0 and [u["document_id"] for u in record["units"]] == [docs["focus"]]
    assert not record["complete"] and record["blocking"] == [TF.B_CANDIDATES], "only the two candidates block"
    note = TF.coverage_note(record)
    assert f"{TF.MSG_GROUP_UNLINKABLE}: 18." in note.splitlines() and TF.MSG_GROUP_CANDIDATES in note
    assert "שומה בדויה מספר 7" in note and "שומה בדויה מספר 15" in note and "הנחל" not in note, "the other property is not named"
    one_probe = [c for c in calls if len(c.version_ids) > 1]
    assert len(one_probe) == 1 and len(one_probe[0].version_ids) == 20, "one search over the files of the 20 unidentified units"
    run(ws, "search", query="המועד הקובע", limit=6)
    assert len([c for c in calls if len(c.version_ids) > 1]) == 1, "a second call of the turn reuses the probe"


def test_ae9_the_candidate_whose_mention_the_turn_read_inside_its_comparison_leaves_the_class(office):
    """Unit 15 names the property only as one examined for comparison; reading it moves it out of the candidate class
    (counted apart). Unit 7 describes the property as its own subject: reading it changes nothing."""
    docs = ae9_office(office)
    ws = focused(office, RECENT, docs["focus"])
    run(ws, "search", query="שטח הנכס", limit=6)
    unit15, unit7 = docs[15], docs[7]
    assert {i["document_id"]: i["elsewhere"] for i in ws.time_coverage["candidates"]["items"]} == {unit7: False, unit15: True}
    ver7, reading7 = version_of(office, unit7)
    run(ws, "read", target={"pages": {"document": T.unit_handle_of(ws, unit7, ver7, reading7, 1), "from_page": 1, "to_page": 1}})
    still = T.settle_coverage(ws, ws.time_coverage, T.opened_places(ws))["candidates"]
    assert (still["count"], still["read"]) == (2, 0), "read, but about the property itself: it stays a candidate"
    ver, reading = version_of(office, unit15)
    run(ws, "read", target={"pages": {"document": T.unit_handle_of(ws, unit15, ver, reading, 1), "from_page": 1, "to_page": 1}})
    settled = T.settle_coverage(ws, ws.time_coverage, T.opened_places(ws))
    candidates = settled["candidates"]
    assert (candidates["count"], candidates["read"]) == (1, 1) and [c["document_id"] for c in candidates["named"]] == [unit7]
    assert settled["blocking"] == [TF.B_CANDIDATES] and settled["unlinkable"] == 18
    assert ws.time_coverage["candidates"]["count"] == 2, "the call's own record is not changed"


def body_unit(office, key: str, valuation: str, *body: str) -> str:
    """An appraisal with no identification: a title, its valuation date and a numbered section holding ``body``. (A street
    written with its label, "ברחוב", is read as the unit's own address; the bodies here write it without one.)"""
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}", [{"title": None, "blocks": [
        "שומה בדויה — נתונים סינתטיים", f"המועד הקובע: {valuation}", "1. רקע", *body]}],
        sha=hashlib.sha256(f"body-{key}".encode()).hexdigest())
    return doc


def test_an_unidentified_unit_that_mentions_the_property_in_its_body_is_possible_until_read_in_a_comparison(office):
    focus = appraisal(office, "mb-focus", "30/06/2022")
    own = body_unit(office, "own", "30/06/2024", "בסקירה נזכר הנכס שבהצאלון 7 ובו דירה אחת.")
    cmp_ = body_unit(office, "cmp", "30/06/2023", "לצורך הערכה נבדקה עסקת השוואה בהצאלון 7.")
    nothing = body_unit(office, "none", "30/06/2025", "הדירה משופצת ומוארת.")
    ws = focused(office, RECENT, focus)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    possible = record["possible"]
    assert [c["document_id"] for c in possible["named"]] == [own, cmp_] and possible["count"] == 2
    assert [c["reason"] for c in possible["named"]] == [P.REASON_MENTIONED] * 2
    assert record["candidates"]["count"] == 0 and record["unlinkable"] == 1 and nothing not in {
        c["document_id"] for c in possible["items"]}
    assert not record["complete"] and record["blocking"] == [TF.B_POSSIBLE]
    ver, reading = version_of(office, cmp_)
    run(ws, "read", target={"pages": {"document": T.unit_handle_of(ws, cmp_, ver, reading, 1), "from_page": 1, "to_page": 1}})
    after = T.settle_coverage(ws, ws.time_coverage, T.opened_places(ws))["possible"]
    assert (after["count"], after["read"]) == (1, 1) and [c["document_id"] for c in after["named"]] == [own]


def test_a_unit_whose_opening_was_not_read_is_a_possible_candidate_even_with_no_mention(office):
    a = appraisal(office, "pr-a", "30/06/2022")
    partly = appraisal(office, "pr-partly", "30/06/2024", identification=[], place=None)
    whole = appraisal(office, "pr-whole", "30/06/2025", identification=[], place=None)
    ver, _ = version_of(office, partly)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET status = 'unread' WHERE version_id = :v AND block_index = 2"), {"v": ver})
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert [c["document_id"] for c in record["possible"]["named"]] == [partly] and record["possible"]["count"] == 1
    assert record["possible"]["named"][0]["reason"] == P.REASON_PARTLY_READ
    assert record["unlinkable"] == 1 and whole not in {c["document_id"] for c in record["possible"]["items"]}
    assert not record["complete"] and record["blocking"] == [TF.B_POSSIBLE]


def long_unit(office, key: str, valuation: str) -> str:
    """An appraisal with no identification and a long body: eleven blocks on its first page (past the title run), then a
    second page (``deep``)."""
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}", [{"title": None, "blocks": [
        "שומה בדויה — נתונים סינתטיים", f"המועד הקובע: {valuation}", *[f"סעיף מספר {n} בדוי." for n in range(9)]],
        "deep": [f"עמוד שני, פסקה {n} בדויה." for n in range(4)]}], sha=hashlib.sha256(f"long-{key}".encode()).hexdigest())
    return doc


def test_uncertain_blocks_or_an_unread_block_deep_in_a_large_unit_do_not_make_it_a_candidate(office):
    """Almost every large real unit has a block that was read uncertainly or not at all somewhere in its body: only an
    unread block of the opening (the title run, or the first page) puts the identification in doubt."""
    a = appraisal(office, "ur-a", "30/06/2022")
    uncertain = long_unit(office, "uncertain", "30/06/2023")
    deep = long_unit(office, "deep", "30/06/2024")
    opening = long_unit(office, "opening", "30/06/2025")
    with tenant_tx(office.system) as conn:
        v_unc, _ = version_of(office, uncertain)
        v_deep, _ = version_of(office, deep)
        v_open, _ = version_of(office, opening)
        conn.execute(text("UPDATE document_blocks SET status = 'read_uncertain' WHERE version_id = :v"), {"v": v_unc})
        conn.execute(text("UPDATE document_blocks SET status = 'unread' WHERE version_id = :v AND page = 2"), {"v": v_deep})
        conn.execute(text("UPDATE document_blocks SET status = 'unread' WHERE version_id = :v AND block_index = 9"),
                     {"v": v_open})  # past the title run, but on the unit's first page
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert [c["document_id"] for c in record["possible"]["named"]] == [opening], "only the unread opening"
    assert record["unlinkable"] == 2 and record["possible"]["count"] == 1
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET status = 'read' WHERE version_id = :v"), {"v": v_open})
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    assert ws.time_coverage["possible"]["count"] == 0 and ws.time_coverage["complete"]
    assert ws.time_coverage["unlinkable"] == 3


def test_focus_closure_with_only_unlinkable_units_outside_it_is_complete_and_the_note_counts_them(office):
    docs = [appraisal(office, f"cl-{y}", f"30/06/{y}") for y in (2022, 2024)]
    for n in range(3):
        appraisal(office, f"cl-blank-{n}", f"30/06/202{n}", identification=[], place=None)
    ws = focused(office, RECENT, docs[0])
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert record["complete"] and record["unlinkable"] == 3 and record["possible"]["count"] == 0
    assert TF.coverage_note(record).splitlines()[-1] == f"{TF.MSG_GROUP_UNLINKABLE}: 3."


# located versus read ------------------------------------------------------------------------------------------------

VALUED = "השווי נקבע בסך {amount} ₪ כולל מע״מ, לפי גישת ההשוואה."
VALUE_QUERY = "השווי נקבע בסך כולל מע״מ לפי גישת ההשוואה"
ADOPTED = {"kind": "value", "unit": "ILS", "period": "none", "vat": "included", "area_basis": "", "subject": "הנכס",
           "role": "other", "stance": "adopted"}


def valued(office, key: str, valuation: str, amount: str) -> str:
    """A one-unit appraisal of the property (``PROPERTY`` identification) whose value sentence states its amount, VAT included."""
    blocks = ["מסמך סינתטי — נתונים בדויים", *PROPERTY, f"המועד הקובע: {valuation}", VALUED.format(amount=amount)]
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}", [{"title": f"מסמך {key}", "blocks": blocks}],
                       sha=hashlib.sha256(f"valued-{key}".encode()).hexdigest())
    return doc


def sid_of(out: str, doc: str) -> str:
    return next(s["id"] for s in sources_of(out) if s["document_id"] == doc and "השווי נקבע בסך" in s["body"])


def take(ws, out: str, doc: str, amount: str) -> str:
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None,
               "quote": f"השווי נקבע בסך {amount} ₪", "number": amount}
    got = run(ws, "take_value", source=sid_of(out, doc), locator=locator, meaning=ADOPTED, label="שווי הנכס")
    assert got.startswith("V"), got
    return got.split()[0]


def test_a_newer_unit_only_located_blocks_with_b_located_and_registering_its_datum_makes_it_read(office):
    from app.chat import timeline

    old, mid, new = (valued(office, f"lr-{y}", f"30/06/{y}", amount) for y, amount in
                     ((2022, "1,950,000"), (2023, "2,100,000"), (2025, "2,410,000")))
    ws = focused(office, RECENT, old)
    out = run(ws, "search", query=VALUE_QUERY, limit=6)
    assert {u["status"] for u in ws.time_coverage["units"]} == {T.LOCATED} and ws.time_coverage["complete"]
    v_mid = take(ws, out, mid, "2,100,000")

    record = timeline.coverage_of(ws, [v_mid])
    status = {u["document_id"]: u["status"] for u in record["units"]}
    assert status == {new: T.LOCATED, mid: T.READ, old: T.LOCATED}, "the cited datum is read from the 2023 unit only"
    assert not record["complete"] and record["blocking"] == [TF.B_LOCATED]
    assert [u["document_id"] for u in record["units"] if u["blocks"]] == [new], "the 2022 unit is older: it does not matter"
    note = TF.coverage_note(record)
    assert TF.MSG_LOCATED.format(date="30/06/2025") in note.splitlines() and TF.LATEST_CHECKED in note
    assert timeline.partial_coverage(ws, [v_mid]) is not None
    assert ws.time_coverage["complete"], "the call's own record is the turn's search: nothing read yet at that point"

    v_new = take(ws, out, new, "2,410,000")  # the datum of the same base key, registered from the newest unit
    done = timeline.coverage_of(ws, [v_mid])
    assert {u["document_id"]: u["status"] for u in done["units"]}[new] == T.READ
    assert done["complete"] and not done["blocking"] and timeline.partial_coverage(ws, [v_mid]) is None
    assert timeline.coverage_of(ws, [v_new])["complete"]


def test_a_section_the_turn_opened_in_the_newer_unit_makes_it_read(office):
    from app.chat import timeline

    old, new = valued(office, "so-2022", "30/06/2022", "1,950,000"), valued(office, "so-2025", "30/06/2025", "2,410,000")
    ws = focused(office, RECENT, old)
    out = run(ws, "search", query=VALUE_QUERY, limit=6)
    v_old = take(ws, out, old, "1,950,000")
    assert not timeline.coverage_of(ws, [v_old])["complete"]
    ver, reading = version_of(office, new)
    run(ws, "read", target={"pages": {"document": T.unit_handle_of(ws, new, ver, reading, 1), "from_page": 1, "to_page": 1}})
    done = timeline.coverage_of(ws, [v_old])
    assert {u["document_id"]: u["status"] for u in done["units"]} == {new: T.READ, old: T.READ} and done["complete"]
    assert ws.time_coverage["complete"] and {u["status"] for u in ws.time_coverage["units"]} == {T.LOCATED}, "unchanged"


def test_the_model_summary_names_a_located_unit_once_the_turn_read_another(office):
    old, new = valued(office, "sm-2022", "30/06/2022", "1,950,000"), valued(office, "sm-2025", "30/06/2025", "2,410,000")
    ws = focused(office, RECENT, old)
    run(ws, "search", query=VALUE_QUERY, limit=6)
    assert TF.MSG_LOCATED.format(date="30/06/2025") not in TF.coverage_summary(ws.time_coverage), "nothing read yet"
    ver, reading = version_of(office, old)
    run(ws, "read", target={"pages": {"document": T.unit_handle_of(ws, old, ver, reading, 1), "from_page": 1, "to_page": 1}})
    run(ws, "search", query="המועד הקובע", limit=6)
    summary = TF.coverage_summary(ws.time_coverage)
    assert TF.MSG_LOCATED.format(date="30/06/2025") in summary and TF.PHRASE_RULE in summary, new


def test_a_newer_datum_of_an_undecided_class_is_named_in_the_record_and_the_note(office, monkeypatch):
    """U4's ``later_undecided`` reaches the record: the cited 2022 value and a 2025 value of the same base key whose facets
    could not be compared. (The facet decision is U4's and is proven in test_timeline; here the pair is given.)"""
    from app.chat import timeline

    old, new = valued(office, "lu-2022", "30/06/2022", "1,950,000"), valued(office, "lu-2025", "30/06/2025", "2,410,000")
    ws = focused(office, RECENT, old)
    out = run(ws, "search", query=VALUE_QUERY, limit=6)
    v_old, v_new = take(ws, out, old, "1,950,000"), take(ws, out, new, "2,410,000")
    by_ref = {d.ref: d for d in timeline.datums(ws)}
    monkeypatch.setattr(timeline, "later_undecided", lambda ws_, cited, identity=None: [(by_ref[v_old], by_ref[v_new])])
    record = timeline.coverage_of(ws, [v_old])
    (later,) = record["later_undecided"]
    assert (later["older"], later["newer"], later["document_id"]) == (v_old, v_new, new) and later["when"] == "30/06/2025"
    assert not record["complete"] and TF.B_LATER_UNDECIDED in record["blocking"]
    note = TF.coverage_note(record)
    assert TF.LATEST_CHECKED in note and "מסמך lu-2025" in note and "30/06/2025" in note
    assert timeline.partial_coverage(ws, [v_old]) is not None
    monkeypatch.setattr(timeline, "later_undecided", lambda ws_, cited, identity=None: [])
    assert "later_undecided" not in timeline.coverage_of(ws, [v_old]) and timeline.coverage_of(ws, [v_old])["complete"]
    payload = timeline.payload(ws, {v_old}, coverage=timeline.coverage_of(ws, [v_old]))
    assert payload["coverage"]["note"] == TF.coverage_note(timeline.coverage_of(ws, [v_old]))
