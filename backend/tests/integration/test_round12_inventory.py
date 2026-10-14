"""The inventory of a property's appraisals, the focused per-unit search and the coverage record (round 12 U7: R9-R14, R20;
AE3, AE4; KTD6, KTD7).

In a ``recent`` or ``explicit`` turn about an identified property the server lists the property's appraisals and their
valuation dates before it searches (``tools.property_inventory``), searches each selected unit on its own (cap eight, two
slots for undated ones), ranks per (version, unit) and records what it checked (``Workspace.time_coverage``). Fixtures are
the synthetic round-12 ones: single appraisals uploaded through the worker, and files of several units built with
``tests.support.unit_files`` (a DOCX is one unit). Every name, address and amount is invented."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_property_scope import (
    DOCS12,
    FIXTURES,
    focused,
    order,
    unfocused,
    upload,
    version_of,
)
from tests.integration.test_tools_pools import run
from tests.support.unit_files import unit_file

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
    assert units[ids["window_latest"]]["status"] == T.HIT
    assert [u["document_id"] for u in record["units"]][0] == ids["window_latest"], "the inventory lists the newest first"
    assert sum(u["status"] == T.HIT for u in record["units"]) == 8 and sum(
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
    assert {u["status"] for u in record["units"]} == {T.HIT} and record["complete"] and not record["blocking"]


def test_ae4_ten_dated_units_with_cap_eight_are_complete_for_latest_when_the_two_skipped_are_older_than_the_newest_hit(office):
    docs = {year: appraisal(office, f"cap-{year}", f"30/06/{year}") for year in range(2015, 2025)}
    ws = focused(office, RECENT, docs[2018])
    out = run(ws, "search", query=QUERY, limit=6)
    assert order(out)[0] == docs[2024]
    record = ws.time_coverage
    assert record["scope"] == "property" and record["identified"] and record["via"] == "focus" and len(record["units"]) == 10
    status = status_of(ws)
    assert [status[docs[y]] for y in range(2024, 2016, -1)] == [T.HIT] * 8
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
    assert sum(status[d] == T.NOT_SEARCHED_CAP for d in undated) == 1 and sum(status[d] == T.HIT for d in undated) == 2
    assert status[dated[2018]] == T.NOT_SEARCHED_CAP and all(status[dated[y]] == T.HIT for y in range(2019, 2025))
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
    assert status_of(ws) == {a: T.HIT, b: T.HIT, c: T.HIT} and dates_of(ws) == ["2024-06-30", "2022-06-30", None]
    assert not record["complete"] and record["blocking"] == ["undated"]


def test_the_newest_unit_searched_without_a_match_makes_recent_partial_and_names_its_date(office):
    old = appraisal(office, "nm-old", "30/06/2022")
    mid = appraisal(office, "nm-mid", "30/06/2023")
    newest = appraisal(office, "nm-new", "30/06/2025", rent=False)
    ws = focused(office, RECENT, old)
    out = run(ws, "search", query=RENT_QUERY, limit=6)
    assert order(out) == [mid, old], "the newest appraisal states no rent: the hits are the older ones, newest first"
    record = ws.time_coverage
    assert status_of(ws) == {newest: T.NO_MATCH, mid: T.HIT, old: T.HIT}
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
    assert status[docs[2022]] == status[docs[2024]] == T.HIT
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
        assert [u["document_id"] for u in record["units"]] == [focus] and record["units"][0]["status"] == T.HIT
        assert not record["complete"] and "not_identified" in record["blocking"]


def test_a_visible_newer_appraisal_with_no_identity_facts_is_undetermined_and_makes_the_record_partial(office):
    a = appraisal(office, "ud-a", "30/06/2022")
    appraisal(office, "ud-newer", "30/06/2025", identification=[], place=None)
    ws = focused(office, RECENT, a)
    run(ws, "search", query=QUERY, limit=6)
    record = ws.time_coverage
    assert record["undetermined"] == 1 and record["candidates"] == {"named": [], "count": 0}
    assert [u["document_id"] for u in record["units"]] == [a] and not record["complete"]
    assert record["blocking"] == ["undetermined"]


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
    H, N, C = T.HIT, T.NO_MATCH, T.NOT_SEARCHED_CAP
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
    H, C = T.HIT, T.NOT_SEARCHED_CAP
    mode = explicit("2022")
    assert record_of([H, H, C], ["2022-03-31", "2024-03-31", "2019-03-31"], mode)["complete"], "an earlier unit cannot hold 2022"
    assert not record_of([H, C], ["2022-03-31", "2024-03-31"], mode)["complete"], "a later unit may list earlier data"
    assert record_of([H, T.NO_MATCH], ["2022-03-31", "2024-03-31"], mode)["complete"], "searched without a match is searched"


def test_merging_calls_counts_a_unit_as_searched_only_if_every_call_searched_it():
    one = record_of([T.HIT, T.HIT, T.NOT_SEARCHED_CAP], ["2025-06-30", "2023-06-30", "2020-06-30"])
    two = record_of([T.NOT_SEARCHED_CAP, T.HIT, T.HIT], ["2025-06-30", "2023-06-30", "2020-06-30"])
    merged = T.merge_coverage(RECENT, [one, two])
    assert [u["status"] for u in merged["units"]] == [T.NOT_SEARCHED_CAP, T.HIT, T.NOT_SEARCHED_CAP]
    assert merged["calls"] == 2 and not merged["complete"] and merged["blocking"] == ["newer_not_searched"]
    assert [u["blocks"] for u in merged["units"]] == [True, False, False], "the second call never searched the newest unit"
    assert T.merge_coverage(RECENT, [one]) is one and T.merge_coverage(RECENT, []) is None
    assert one["units"][1]["status"] == T.HIT, "the merge does not change the calls' own records"
