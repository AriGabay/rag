"""Round 12 sequences, end to end with a scripted model (U12: R19, R20, R22; AE1-AE7; R14).

The synthetic round-12 fixtures (``tests/fixtures/round12``: single appraisals read by the real worker, and files of several
appraisal units built with ``tests.support.unit_files`` since a DOCX is one unit) go through the whole turn: the scripted
agent searches, registers values and answers; what is proven is what the server decides, stores and orders - the sources
the search returned and the answer cites, the coverage record and its note in ``answer.time``, the timeline groups, the
qualifier edit - never wording. Every name, address and amount is invented.

- (1) two cities (AE1): the answer cites only the focus city's appraisal and the note counts the cityless one;
- (2) a property over two parcels (AE2): the history holds both appraisals in date order and the separate lot never appears;
- (3) a compendium (R10): "ומה היה השווי ב-2022?" after a recent question answers from the 2022 unit of the same file;
- (4) beyond the window (AE3, R20): the latest of fifteen appraisals is found and first, unconditionally, three times;
- (5) partial coverage (AE4): the note names what was not searched, and the answer that says "the latest" is repaired;
- (6) an invented attribute (AE5): a rent that states no period keeps its number and never carries one as the source's;
- (7) history (AE6, R16, R17): prior values keep their own dates and never the report's valuation date;
- (8) a conflict (AE7, R18): a dated and an undated value of one property conflict, a history value does not;
- (9) off mode (R14): the search, the order and the time wording are round 11's; the meaning corrections still apply;
- (10) U12b: the link reason reaches the model and the judge (S2); a timed turn answered from a read source records a coverage
  that says what was only read (S3, S2); a cited value's server-resolved date backs the same date wherever the model wrote it."""

# ruff: noqa: F811 - the tests take the imported ``calls`` fixture by name, as the round-11 sequences do
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter

import pytest

from app.chat import tools as T
from app.chat.timeframe import LATEST_CHECKED
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation
from tests.integration.test_chat_timeline import ADOPTED, _focus_of, m_diagnostics
from tests.integration.test_property_scope import DOCS12, FIXTURES, compendium, upload
from tests.integration.test_round10_sequences import (
    Turn,
    _resolution,
    calls,  # noqa: F401 - the recorded tool calls (the fixture)
    turn,
)
from tests.integration.test_round10_sequences import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part, read
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

VALUE_Q = "מה שווי הנכס ברחוב {street}?"
YEAR_Q = "ומה היה השווי ב-2022?"
VALUE_SEARCH = "שווי הנכס השווי נקבע בסך"
DOCX_SENTENCE = "השווי נקבע בסך {amount} ₪"  # how the DOCX fixtures state the value
UNIT_SENTENCE = "שווי הנכס: {amount} ₪"  # how the files built with unit_file state it
UNIT_VAT = "השווי נקבע בסך {amount} ₪ כולל מע״מ"  # how unit_appraisal states it
QUALIFIED = LATEST_CHECKED  # the phrase the answer uses under partial coverage


# --- the world ---------------------------------------------------------------------------------------------------------

def world(db, client, monkeypatch, calls, keys: tuple[str, ...], email: str = "admin-r12s@example.test"):
    """An office with the given round-12 fixtures uploaded through the worker (single appraisals), cloud use on."""
    o = make_office(db, "משרד סינתטי יב", email)
    login(client, email)
    o.client, o.monkeypatch, o.calls = client, monkeypatch, calls
    cloud(monkeypatch, o, ScriptedAgent([]))
    o.ids = {k: upload(o, FIXTURES / DOCS12[k]["file"], DOCS12[k]["source_kind"], monkeypatch) for k in keys}
    o.by_id = {v: k for k, v in o.ids.items()}
    return o


def sids_of(items: list) -> dict[str, list[dict]]:
    """document id -> the search hits (attributes and body) of the tool outputs the model has seen, in order."""
    out: dict[str, list[dict]] = {}
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            for m in re.finditer(r"<source ([^>]*)>(.*?)</source>", it["output"], re.S):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                if attrs.get("id") and attrs.get("document_id"):
                    out.setdefault(attrs["document_id"], []).append(attrs | {"body": m.group(2)})
    return out


def sid_with(items: list, doc: str, needle: str) -> str:
    """The S# of the hit of ``doc`` whose body holds ``needle``."""
    hits = sids_of(items).get(doc, [])
    for hit in hits:
        if needle in hit["body"]:
            return hit["id"]
    raise AssertionError(f"no hit of {doc} holds {needle!r}: {[(h['id'], h['body'][:60]) for h in hits]}")


def amount_take(sid: str, amount: str, label: str = "שווי הנכס", sentence: str = DOCX_SENTENCE, **meaning) -> dict:
    """A ``take_value`` of the value ``amount`` quoted from the passage ``sid`` (adopted value unless ``meaning`` says
    otherwise). A sentence that says "כולל מע״מ" registers the value as including VAT."""
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None,
               "quote": sentence.format(amount=amount), "number": amount}
    base = ADOPTED | ({"vat": "included"} if "כולל מע״מ" in sentence else {})
    return call("take_value", source=sid, locator=locator, meaning=base | meaning, label=label)


def steps_for(picks: list[tuple], answer, documents: list[str] | None = None, query: str = VALUE_SEARCH,
              sentence: str = DOCX_SENTENCE, also: tuple = (), **meaning) -> list:
    """Searches (``documents``: those named, None: the whole window), registers the value of each ``(document id, amount[,
    sentence])`` of ``picks`` (V1, V2 ...) from the hit of that document that holds the amount, then answers with ``answer``
    (a step, or a list of steps: the repair rounds' answers follow it). ``also``: further calls of the first step."""
    def register(items):
        return [amount_take(sid_with(items, p[0], p[1]), p[1], sentence=p[2] if len(p) > 2 else sentence, **meaning)
                for p in picks]

    return [[call("search", query=query, document_ids=documents, limit=None), *also], register,
            *(answer if isinstance(answer, list) else [answer])]


def set_switch(o, cid: str, on: bool) -> None:
    r = o.client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": on})
    assert r.status_code == 200 and r.json()["prefer_recent"] is on, r.text


def follow_up(o, cid: str, question: str, steps: list) -> Turn:
    """A turn after another about the same property: the parse keeps the conversation's property."""
    return turn(o, cid, question, steps, _resolution(
        relation="same_datum", standalone_question=question, intent=intent_part("property", "conversation_property")))


def first_turn(o, cid: str, question: str, steps: list, **agent) -> Turn:
    return turn(o, cid, question, steps, None, **({"intent": "property"} | agent))


def answer_with(vid: str, amount: str, subject: str, text: str = "השווי העדכני", vat: bool = False) -> dict:
    """An answer that gives one value and cites it (``vat``: the source states the value includes VAT)."""
    body = f"{text} הוא {amount} ₪{' כולל מע״מ' if vat else ''} [{vid}]."
    return final(body, focus=_focus_of() | {"subject": subject, "vat": "included" if vat else "unknown",
                                            "metric_as_written": "שווי הנכס"},
                 claims=[{"text": body.rsplit(" [", 1)[0], "source_ids": [vid], "basis": "explicit"}])


def rows_of(t: Turn) -> list[dict]:
    return [r for g in t.answer["time"]["timeline"] for r in g["rows"]]


def source_documents(t: Turn) -> set[str]:
    """The documents of the sources the answer carries (its search scope) and of the rows of its timeline."""
    return {s["document_id"] for s in t.answer["sources"] if s.get("document_id")} | {r["source"]["document_id"] for r in rows_of(t)}


def first_search_documents(t: Turn) -> list[str]:
    """The documents of the first search call's hits, in the order the model read them."""
    out = []
    for s in next(c for c in t.tools if c.name == "search").sources:
        if s["document_id"] not in out:
            out.append(s["document_id"])
    return out


def unit_appraisal(o, key: str, city: str | None, valuation: str | None, amount: str, street: str = "הגפן",
                   number: str = "15", block: str = "8701", parcel: str | None = "3", vat: bool = False,
                   extra: list[str] | None = None, value: str | None = None) -> str:
    """A one-unit appraisal built with ``unit_file`` from the fixtures' writing forms: its identification lines (the city
    only when given), its valuation date (None: it states none), its value sentence (``vat``: "כולל מע״מ", so the value is
    comparable) and ``extra`` blocks; ``value`` replaces the value sentence. Returns its document id."""
    blocks = ["מסמך סינתטי — נתונים בדויים", *([f"עיר: {city}"] if city else []), f"כתובת הנכס: {street} {number}",
              *([f"גוש: {block} חלקה: {parcel}"] if parcel else []), *([f"המועד הקובע: {valuation}"] if valuation else []),
              value or f"השווי נקבע בסך {amount} ₪{' כולל מע״מ' if vat else ''}, לפי גישת ההשוואה (סינתטי).", *(extra or [])]
    doc, _ = unit_file(o, o.default_group_id, f"שומה סינתטית {key}", [{"title": f"שומה סינתטית {key}", "blocks": blocks}],
                       sha=hashlib.sha256(key.encode()).hexdigest())
    return doc


def bare_world(db, client, monkeypatch, calls, email: str = "admin-r12b@example.test"):
    o = make_office(db, "משרד סינתטי יב", email)
    login(client, email)
    o.client, o.monkeypatch, o.calls, o.ids = client, monkeypatch, calls, {}
    cloud(monkeypatch, o, ScriptedAgent([]))
    return o


# =======================================================================================================================
# (1) two cities, one street and number (AE1)
# =======================================================================================================================

X, Y = "עיר-סינתטית", "עיר-סינתטית-ב"  # compendium_one is appraised in X


def test_ae1_two_cities_a_time_question_never_ranks_the_other_city_and_the_note_counts_the_cityless_one(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    gafen, _ = compendium(o, "compendium_one")  # six appraisals of הגפן 15 in city X, 2020-2025
    other_city = unit_appraisal(o, "gafen-y", Y, "31/12/2025", "4,000,000", block="8702", parcel="9")
    cityless = unit_appraisal(o, "gafen-none", None, "31/01/2026", "4,100,000", parcel=None)
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"), steps_for(
        [(gafen, "3,100,000")], answer_with("V1", "3,100,000", "הגפן 15"), [gafen], sentence=UNIT_SENTENCE))
    a = t.answer
    assert a["status"] == "answered"
    assert "3,100,000" in a["markdown"] and not {"4,000,000", "4,100,000"} & set(re.findall(r"\d[\d,]+", a["markdown"]))
    assert source_documents(t) == {gafen}, "no appraisal of the other city or of the cityless one is a source"
    assert set(first_search_documents(t)) == {gafen}
    cov = a["time"]["coverage"]
    assert cov["scope"] == "property" and cov["via"] == "documents" and len(cov["units"]) == 6
    assert {u["document_id"] for u in cov["units"]} == {gafen} and other_city not in json.dumps(cov)
    assert not cov["complete"] and "candidates" in cov["blocking"]
    assert [(c["document_id"], c["reason"]) for c in cov["candidates"]["named"]] == [(cityless, "עיר לא צוינה")]
    assert cov["candidates"]["count"] == 1 and "שומה סינתטית gafen-none" in cov["note"]
    assert a["time"]["text"].endswith(cov["note"])


# =======================================================================================================================
# (2) a property over two parcels, and sub-parcels (AE2)
# =======================================================================================================================

def test_ae2_the_history_of_the_two_parcel_property_holds_a_and_b_in_date_order_and_the_separate_lot_never_appears(
        db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("parcels_ab", "parcels_b", "parcels_c"))
    a_doc, b_doc, c_doc = o.ids["parcels_ab"], o.ids["parcels_b"], o.ids["parcels_c"]
    focus = _focus_of() | {"subject": "הדס 5", "vat": "unknown", "metric_as_written": "שווי הנכס"}
    body = "השווי העדכני הוא 3,650,000 ₪ [V1]. קודם לכן, ב-30/06/2023, השווי היה 3,200,000 ₪ [V2]."
    answer = final(body, focus=focus, claims=[
        {"text": "השווי העדכני הוא 3,650,000 ₪", "source_ids": ["V1"], "basis": "explicit"},
        {"text": "קודם לכן, ב-30/06/2023, השווי היה 3,200,000 ₪", "source_ids": ["V2"], "basis": "explicit"}])
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הדס 5"), steps_for(
        [(b_doc, "3,650,000"), (a_doc, "3,200,000")], answer, [b_doc]))
    a = t.answer
    assert a["status"] == "answered" and "3,650,000" in a["markdown"] and "3,200,000" in a["markdown"]
    # the history: B then A, each value with its own valuation date and its own document (DOCX values state no VAT, so
    # the server draws no timeline group of them: R8)
    assert [(v["id"], v["as_of"]["date"], v["document_id"]) for v in a["values"]] == [
        ("V1", "2025-06-30", b_doc), ("V2", "2023-06-30", a_doc)]
    assert source_documents(t) == {a_doc, b_doc}
    assert set(first_search_documents(t)) == {a_doc, b_doc}, "the separate lot is never read"
    assert c_doc not in json.dumps(a, ensure_ascii=False)
    cov = a["time"]["coverage"]
    assert cov["scope"] == "property" and cov["complete"] and [u["document_id"] for u in cov["units"]] == [b_doc, a_doc]
    assert [u["date"] for u in cov["units"]] == ["2025-06-30", "2023-06-30"]
    assert cov["property"]["reasons"] == ["parcel 8401/40"] and cov["candidates"]["count"] == 0
    assert cov["note"] == "נבדקו כל השומות של הנכס הזמינות לך (2)." and a["time"]["text"] == cov["note"]


def test_ae2_two_sub_parcels_of_one_parcel_never_gather_each_other_the_search_stays_in_the_named_appraisal(
        db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("apartments_3", "apartments_4", "apartments_none"))
    three, four, none = o.ids["apartments_3"], o.ids["apartments_4"], o.ids["apartments_none"]
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הנרקיס 12"), steps_for(
        [(three, "1,750,000")], answer_with("V1", "1,750,000", "הנרקיס 12"), [three]))
    a = t.answer
    assert a["status"] == "answered" and source_documents(t) == {three} and set(first_search_documents(t)) == {three}
    cov = a["time"]["coverage"]
    assert four not in json.dumps(cov) and none not in json.dumps(cov), "no other appraisal is ranked or counted"
    # nothing proves a second appraisal of the apartment, so the search is the named appraisal alone and says so
    assert cov["scope"] == "window" and not cov["complete"] and cov["note"].splitlines()[0] == LATEST_CHECKED + "."


# =======================================================================================================================
# (3) a compendium: a later question about a year (R10, AE3 of round 11)
# =======================================================================================================================

def test_a_compendium_year_question_after_a_recent_question_answers_from_the_2022_unit_of_the_same_property(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    gafen, _ = compendium(o, "compendium_one")  # six units: 2020 .. 2025
    cid = new_conversation(client)
    first = first_turn(o, cid, VALUE_Q.format(street="הגפן 15"), steps_for(
        [(gafen, "3,100,000")], answer_with("V1", "3,100,000", "הגפן 15"), [gafen], sentence=UNIT_SENTENCE))
    shown = lambda t: list(dict.fromkeys(s["date"] for c in t.tools if c.name == "search" for s in c.sources))  # noqa: E731
    assert shown(first) == [f"למועד הקובע 30/06/{y}" for y in range(2025, 2019, -1)], "recent: newest unit first"
    assert first.answer["time"]["mode"] == "recent" and first.answer["time"]["coverage"]["complete"]

    steps = steps_for([(gafen, "2,500,000")], final(
        "השווי ב-2022 היה 2,500,000 ₪ [V1].", focus=_focus_of() | {"subject": "הגפן 15", "vat": "unknown", "metric_as_written": "שווי הנכס"},
        claims=[{"text": "השווי ב-2022 היה 2,500,000 ₪", "source_ids": ["V1"], "basis": "explicit"}]), [gafen], sentence=UNIT_SENTENCE)
    second = follow_up(o, cid, YEAR_Q, steps)
    t = second.answer["time"]
    assert (t["mode"], t["requested"]["start"], t["requested"]["end"]) == ("explicit", "2022", "2022")
    assert shown(second)[0] == "למועד הקובע 30/06/2022", "explicit: the 2022 unit answers first"
    assert "2,500,000" in second.answer["markdown"] and second.answer["status"] == "answered"
    (value,) = second.answer["values"]
    assert (value["value_text"], value["as_of"]["date"], value["document_id"]) == ("2,500,000", "2022-06-30", gafen)
    assert t["coverage"]["mode"] == "explicit" and t["coverage"]["complete"] and len(t["coverage"]["units"]) == 6
    assert [u["date"] for u in t["coverage"]["units"]][0] == "2022-06-30"


# =======================================================================================================================
# (4) beyond the relevance window (AE3, R20): the latest of fifteen appraisals, found and first, three times
# =======================================================================================================================

WINDOW = [f"window_{n:02d}" for n in range(1, 15)] + ["window_latest"]
LATEST_SENTENCE = "סכום ההערכה הסופי לדירה עומד על {amount} ₪"  # the 2026 appraisal never writes "שווי"
WINDOW_QUERY = "שווי הנכס הערבה 2"


def test_ae3_the_latest_of_fifteen_appraisals_beyond_the_window_is_found_first_and_cited_first_in_three_of_three_runs(
        db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, tuple(WINDOW))
    ids = o.ids
    body = "השווי העדכני הוא 4,350,000 ₪ [V1]. קודם לכן, ב-30/09/2025, השווי היה 3,700,000 ₪ [V2]."
    focus = _focus_of() | {"subject": "הערבה 2", "vat": "unknown", "metric_as_written": "שווי הנכס"}
    answer = final(body, focus=focus, claims=[
        {"text": "השווי העדכני הוא 4,350,000 ₪", "source_ids": ["V1"], "basis": "explicit"},
        {"text": "קודם לכן, ב-30/09/2025, השווי היה 3,700,000 ₪", "source_ids": ["V2"], "basis": "explicit"}])
    for run in range(3):  # three fresh conversations: the same answer each time, no condition
        t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הערבה 2"), steps_for(
            [(ids["window_latest"], "4,350,000", LATEST_SENTENCE), (ids["window_14"], "3,700,000", UNIT_SENTENCE)], answer,
            [ids["window_01"]], query=WINDOW_QUERY,
            also=(call("search", query="סכום ההערכה הסופי לדירה", document_ids=[ids["window_latest"]], limit=None),
                  call("search", query="שווי הנכס נקבע בסך", document_ids=[ids["window_14"]], limit=None))))
        a = t.answer
        assert first_search_documents(t)[0] == ids["window_latest"], f"run {run}: the 2026 appraisal's hit is first"
        assert a["status"] == "answered" and a["markdown"].startswith("השווי העדכני הוא 4,350,000"), f"run {run}"
        assert [(v["id"], v["as_of"]["date"], v["document_id"]) for v in a["values"]] == [
            ("V1", "2026-03-31", ids["window_latest"]), ("V2", "2025-09-30", ids["window_14"])], f"run {run}"
        cov = a["time"]["coverage"]
        assert [u["document_id"] for u in cov["units"]][0] == ids["window_latest"] and len(cov["units"]) == 15
        assert Counter(u["status"] for u in cov["units"]) == {"hit": 8, "not_searched_cap": 7}, "eight read, the seven oldest not"
        assert cov["complete"] and cov["property"]["units"] == 15 and cov["property"]["reasons"] == ["parcel 8801/9"]
        assert "latest" not in cov["note"] and cov["note"] == "נבדקו כל השומות של הנכס הזמינות לך (15)."


# =======================================================================================================================
# (5) the coverage wording (AE4, R12): complete for "latest" under the cap, partial with an undated unit
# =======================================================================================================================

PROPERTY_X = {"city": X, "street": "הגפן", "number": "15", "block": "8701", "parcel": "3"}


def amount_of(year: int) -> str:
    """The value of the appraisal of ``year`` in a ``chain`` (2,000,000 in 2014, 100,000 more each year)."""
    return f"{2_000_000 + 100_000 * (year - 2014):,}"


def chain(o, years: range, prefix: str, vat: bool = True) -> dict[int, str]:
    """One appraisal of הגפן 15 for each year, valued at 30/06."""
    return {y: unit_appraisal(o, f"{prefix}-{y}", valuation=f"30/06/{y}", amount=amount_of(y), vat=vat, **PROPERTY_X)
            for y in years}


def latest_claim(vid: str, amount: str, text: str) -> dict:
    body = f"{text} הוא {amount} ₪ כולל מע״מ [{vid}]."
    return final(body, focus=_focus_of() | {"subject": "הגפן 15", "metric_as_written": "שווי הנכס"},
                 claims=[{"text": body.rsplit(" [", 1)[0], "source_ids": [vid], "basis": "explicit"}])


def test_ae4_ten_dated_appraisals_with_a_cap_of_eight_are_complete_for_the_latest_and_the_latest_wording_is_no_problem(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2015, 2025), "cap")
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"), steps_for(
        [(docs[2024], amount_of(2024)), (docs[2023], amount_of(2023))], latest_claim("V1", amount_of(2024), "השווי העדכני"),
        [docs[2018]],
        sentence=UNIT_VAT))
    a = t.answer
    cov = a["time"]["coverage"]
    assert cov["complete"] and cov["blocking"] == [] and len(cov["units"]) == 10
    assert Counter(u["status"] for u in cov["units"]) == {"hit": 8, "not_searched_cap": 2}
    assert {u["document_id"] for u in cov["units"] if u["status"] == "not_searched_cap"} == {docs[2015], docs[2016]}
    assert a["status"] == "answered" and a["markdown"].startswith(f"השווי העדכני הוא {amount_of(2024)}"), "no repair"
    assert "recency" not in json.dumps(m_diagnostics(client, t.message), ensure_ascii=False)
    assert [(r["date"], r["marks"]) for r in rows_of(t)] == [("2024-06-30", []), ("2023-06-30", [])]
    assert cov["note"] == "נבדקו כל השומות של הנכס הזמינות לך (10)." and a["time"]["text"].endswith(cov["note"])


def test_ae4_an_undated_appraisal_that_was_not_searched_makes_the_wording_partial_and_the_note_names_it(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2022, 2025), "und")
    undated = unit_appraisal(o, "und-none", valuation=None, amount="2,000,000", vat=True, **PROPERTY_X)
    steps = steps_for([(docs[2024], amount_of(2024)), (docs[2023], amount_of(2023))],
                      [latest_claim("V1", amount_of(2024), "השווי העדכני"),  # unqualified: the server sends it to repair
                       latest_claim("V1", amount_of(2024), QUALIFIED)], [docs[2022]], sentence=UNIT_VAT)
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"), steps)
    a = t.answer
    assert a["markdown"] == f"{QUALIFIED} הוא {amount_of(2024)} ₪ כולל מע״מ [V1]." and a["verification"]["removed"] == 0
    assert "recency" in json.dumps(m_diagnostics(client, t.message), ensure_ascii=False), "a repairable problem, not a removal"
    cov = a["time"]["coverage"]
    assert not cov["complete"] and "undated" in cov["blocking"]
    assert cov["note"].splitlines()[0] == QUALIFIED + "." and "שומה סינתטית und-none" in cov["note"]
    assert [u["document_id"] for u in cov["units"] if u["blocks"]] == [undated]
    # the model read the phrase rule with the search result (after the results, never before them)
    search = t.agent.tool_outputs(1)[0]
    assert QUALIFIED in search and search.index("<source") < search.index(QUALIFIED)
    assert a["time"]["text"].endswith(cov["note"])


# =======================================================================================================================
# (6) an attribute the source does not state (AE5, R6, R7): the number stays, the invented period does not
# =======================================================================================================================

RENT_FOCUS = {"metric_as_written": "שכר דירה", "metric_kind": "rent", "unit": "ILS", "period": "month", "area_basis": "",
              "vat": "unknown", "value_role": "appraiser_determination", "document_ids": []}
RENT_SENTENCE = "שכר דירה {amount} ₪"


def rent_answer(text: str, amount: str, subject: str) -> dict:
    return final(f"{text} [V1].", focus=RENT_FOCUS | {"subject": subject},
                 claims=[{"text": text, "source_ids": ["V1"], "basis": "explicit"}])


def rent_steps(doc: str, amount: str, period: str, *answers) -> list:
    """Searches the rent, registers the rent as the model reads it (``period``), then gives ``answers`` in turn."""
    return steps_for([(doc, amount)], list(answers), [doc], query="שכר דירה", sentence=RENT_SENTENCE, kind="rent",
                     period=period, stance="unknown")


@pytest.mark.parametrize("on", [True, False])
def test_ae5_a_rent_with_no_stated_period_keeps_its_number_and_never_states_a_period_as_the_sources(db, client, monkeypatch, calls, on):
    """Every mode: the meaning corrections do not depend on the time switch (R14)."""
    o = world(db, client, monkeypatch, calls, ("rent_no_period",))
    doc = o.ids["rent_no_period"]
    cid = new_conversation(client)
    set_switch(o, cid, on)
    t = first_turn(o, cid, "מה שכר הדירה בנכס ברחוב השקד 2?", rent_steps(
        doc, "5,000", "month", rent_answer("שכר הדירה הוא 5,000 ₪ לחודש", "5,000", "השקד 2")))
    a = t.answer
    assert "5,000" in a["markdown"] and "[V1]" in a["markdown"], "the source-backed number stays"
    assert "לחודש" not in a["markdown"], "the period the model asserted is not given as the source's"
    assert a["verification"]["removed"] == 0 and a["verification"]["actions"]["qualified"] == 1, a["verification"]
    (v,) = a["values"]
    assert v["value_text"] == "5,000" and v["provenance"]["period"] != "source", "the registered period is the model's own"
    assert (a["time"]["mode"], "coverage" in a["time"]) == (("recent", True) if on else ("none", False))


def test_ae5_a_source_that_states_a_period_keeps_the_correction_path_for_another_period(db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("rent_yearly",))
    doc = o.ids["rent_yearly"]
    wrong = rent_answer("שכר הדירה הוא 60,000 ₪ לחודש", "60,000", "השקד 4")
    t = first_turn(o, new_conversation(client), "מה שכר הדירה בנכס ברחוב השקד 4?",
                   rent_steps(doc, "60,000", "year", wrong, wrong, wrong))
    a = t.answer
    assert "60,000 ₪ לחודש" not in a["markdown"], "a period the source states otherwise is never kept"
    assert a["verification"]["actions"]["qualified"] == 0 and a["verification"]["removed"] == 1, a["verification"]
    assert [r["failure_kind"] for r in a["verification"]["removals"]] == ["wrong_unit"], "the correction/removal path, not the edit"


# =======================================================================================================================
# (7) history keeps its own dates (AE6, R15-R17)
# =======================================================================================================================

HISTORY_MEANING = {"kind": "value", "role": "other", "stance": "unknown"}


def cell_take(sid: str, row: str, column: str, amount: str) -> dict:
    locator = {"table": None, "row": row, "row_number": None, "column": column, "column_number": None, "quote": None,
               "number": amount}
    return call("take_value", source=sid, locator=locator, meaning=ADOPTED | HISTORY_MEANING, label="שווי הנכס")


def history_answer(parts: list[tuple[str, str]], subject: str) -> dict:
    """An answer that gives each ``(sentence, amount)`` of ``parts`` and cites V1, V2 ... in turn."""
    body = " ".join(f"{text} [V{n}]." for n, (text, _) in enumerate(parts, 1))
    return final(body, focus=_focus_of() | {"subject": subject, "vat": "unknown", "metric_as_written": "שווי הנכס"},
                 claims=[{"text": text, "source_ids": [f"V{n}"], "basis": "explicit"} for n, (text, _) in enumerate(parts, 1)])


def test_a_history_table_gives_each_prior_value_its_own_date_never_the_reports_valuation_date(db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("dp_history_table",))
    doc = o.ids["dp_history_table"]
    unit_date, report_date = "2025-06-30", "2026-01-20"

    def register(items):
        table = sid_with(items, doc, "31/12/2023 | 2,300,000")
        return [cell_take(table, "31/12/2021", "שווי (₪)", "2,050,000"), cell_take(table, "31/12/2023", "שווי (₪)", "2,300,000")]

    answer = history_answer([("ב-31/12/2021 השווי היה 2,050,000 ₪", "2,050,000"),
                             ("ב-31/12/2023 השווי היה 2,300,000 ₪", "2,300,000")], "הרימון 8")
    t = first_turn(o, new_conversation(client), "מה היו ההערכות הקודמות לנכס ברחוב הרימון 8?", [
        [call("search", query="ריכוז הערכות לנכס שווי", document_ids=[doc], limit=None)], register, answer])
    a = t.answer
    assert [(v["value_text"], v["as_of"]["date"]) for v in a["values"]] == [("2,050,000", "2021-12-31"), ("2,300,000", "2023-12-31")]
    assert not {unit_date, report_date} & {v["as_of"]["date"] for v in a["values"]}, "never the valuation or the report date"
    assert a["time"]["coverage"]["units"][0]["date"] == unit_date, "the unit itself is dated by its stated valuation date"


def test_ae6_prior_appraisals_in_a_history_section_never_take_the_units_date_and_the_unit_keeps_its_own(
        db, client, monkeypatch, calls):
    """The fixture writes the prior dates with two-digit years ("31.12.19"). The shared parser reads a two-digit year only
    at a valuation label's gap (KTD11), and so does the datum's own-sentence reading (round 12 U12b): "לתאריך הקובע 31.12.19"
    dates the prior value 2019-12-31 and never 2023-12-31, the unit's date."""
    o = world(db, client, monkeypatch, calls, ("dp_prior_section_ae6",))
    doc = o.ids["dp_prior_section_ae6"]
    sentences = {"1,800,000": "לתאריך הקובע 31.12.19 הוערך הנכס ב-{amount} ₪",
                 "2,000,000": "בשומה קודמת, לתאריך הקובע 31.12.21, הוערך הנכס ב-{amount} ₪",
                 "1,500,000": "הנכס נרכש ביום 15/05/2017 תמורת {amount} ₪"}

    def register(items):
        return [amount_take(sid_with(items, doc, amount), amount, sentence=sentence, **HISTORY_MEANING)
                for amount, sentence in sentences.items()]

    answer = history_answer([("ב-31.12.19 הנכס הוערך ב-1,800,000 ₪", "1,800,000"),
                             ("ב-31.12.21 הנכס הוערך ב-2,000,000 ₪", "2,000,000"),
                             ("ב-15/05/2017 הנכס נרכש תמורת 1,500,000 ₪", "1,500,000")], "הרימון 10")
    t = first_turn(o, new_conversation(client), "מה היו ההערכות הקודמות לנכס ברחוב הרימון 10?", [
        [call("search", query="שומות קודמות הוערך הנכס", document_ids=[doc], limit=None)], register, answer])
    a = t.answer
    dates = {v["value_text"]: v["as_of"]["date"] for v in a["values"]}
    assert "2023-12-31" not in dates.values(), "a prior appraisal is never dated by the unit's valuation date"
    assert dates == {"1,800,000": "2019-12-31", "2,000,000": "2021-12-31", "1,500,000": "2017-05-15"}, (
        "each prior value is dated by its own sentence: the labelled two-digit dates and the purchase's four-digit date")
    assert {v["value_text"]: v["as_of"]["provenance"] for v in a["values"]} == dict.fromkeys(dates, "source")
    assert a["time"]["coverage"]["units"][0]["date"] == "2023-12-31", "the unit's date is the labelled valuation date (AE6)"
    assert a["status"] == "answered"


def test_ae6_prior_appraisals_written_with_four_digit_years_are_dated_by_their_own_text(db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("dp_prior_section_4digit",))
    doc = o.ids["dp_prior_section_4digit"]

    def register(items):
        return [amount_take(sid_with(items, doc, "1,800,000"), "1,800,000", sentence="לתאריך הקובע 31/12/2019 הוערך הנכס ב-{amount} ₪",
                            **HISTORY_MEANING)]

    answer = history_answer([("ב-31/12/2019 הנכס הוערך ב-1,800,000 ₪", "1,800,000")], "הרימון 11")
    t = first_turn(o, new_conversation(client), "מה הייתה ההערכה הקודמת לנכס ברחוב הרימון 11?", [
        [call("search", query="שומות קודמות הוערך הנכס", document_ids=[doc], limit=None)], register, answer])
    (v,) = t.answer["values"]
    assert (v["value_text"], v["as_of"]["date"], v["as_of"]["provenance"]) == ("1,800,000", "2019-12-31", "source")
    assert t.answer["time"]["coverage"]["units"][0]["date"] == "2023-12-31", "never the unit's date, and the unit keeps its own"


# =======================================================================================================================
# (8) one conflict rule (AE7, R18)
# =======================================================================================================================

def conflict_world(db, client, monkeypatch, calls, second_value: str | None = None, second_date: str | None = None):
    """The property of הגפן 15 (parcel 8701/3) appraised at 31/03/2025 for 2,450,000 including VAT, and a second appraisal of it
    that states no valuation date: its value sentence is ``second_value`` (default: 2,300,000 including VAT)."""
    o = bare_world(db, client, monkeypatch, calls)
    first = unit_appraisal(o, "ae7-dated", valuation="31/03/2025", amount="2,450,000", vat=True, **PROPERTY_X)
    second = unit_appraisal(o, "ae7-second", valuation=second_date, amount="2,300,000", vat=True, value=second_value, **PROPERTY_X)
    return o, first, second


def conflict_steps(first: str, second: str, quote: str = UNIT_VAT, **meaning) -> list:
    answer = final("השווי הוא 2,450,000 ₪ כולל מע״מ [V1], אך נמצא ערך אחר: 2,300,000 ₪ כולל מע״מ [V2].",
                   focus=_focus_of() | {"subject": "הגפן 15", "metric_as_written": "שווי הנכס"}, claims=[
                       {"text": "השווי הוא 2,450,000 ₪ כולל מע״מ", "source_ids": ["V1"], "basis": "explicit"},
                       {"text": "נמצא ערך אחר: 2,300,000 ₪ כולל מע״מ", "source_ids": ["V2"], "basis": "explicit"}])

    def register(items):
        return [amount_take(sid_with(items, first, "2,450,000"), "2,450,000", sentence=UNIT_VAT),
                amount_take(sid_with(items, second, "2,300,000"), "2,300,000", sentence=quote, **meaning)]

    return [[call("search", query="שווי הנכס השווי נקבע בסך בעבר הוערך הנכס", document_ids=[first, second], limit=None)], register, answer]


@pytest.mark.parametrize("on", [True, False])
def test_ae7_a_dated_and_an_undated_value_of_one_property_with_different_amounts_conflict_in_every_mode(
        db, client, monkeypatch, calls, on):
    o, first, second = conflict_world(db, client, monkeypatch, calls)
    cid = new_conversation(client)
    set_switch(o, cid, on)
    t = first_turn(o, cid, VALUE_Q.format(street="הגפן 15"), conflict_steps(first, second))
    (group,) = t.answer["time"]["timeline"]
    by_doc = {r["source"]["document_id"]: r for r in group["rows"]}
    assert by_doc[first]["marks"] == ["conflict"] and by_doc[first]["date"] == "2025-03-31"
    assert by_doc[second]["marks"] == ["conflict", "unknown_date"] and by_doc[second]["date"] is None
    assert t.answer["time"]["mode"] == ("recent" if on else "none")


def test_ae7_a_value_that_is_undated_only_because_it_is_history_conflicts_with_nothing(db, client, monkeypatch, calls):
    history = "בעבר הוערך הנכס ב-2,300,000 ₪ כולל מע״מ."
    o, first, second = conflict_world(db, client, monkeypatch, calls, second_value=history)
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"), conflict_steps(
        first, second, quote="בעבר הוערך הנכס ב-{amount} ₪ כולל מע״מ", **HISTORY_MEANING))
    a = t.answer
    values = {v["document_id"]: v for v in a["values"]}
    assert values[second]["as_of"]["date"] is None and values[second]["as_of"]["date_origin"] == "history"
    assert values[first]["as_of"]["date"] == "2025-03-31"
    assert a["time"]["timeline"] == [], "one dated value and a history value: no group, no conflict mark"
    assert "conflict" not in json.dumps(a["time"], ensure_ascii=False)


# =======================================================================================================================
# (9) the switch off (R14): round 11's search, order and wording; no inventory, no coverage record, no coverage wording
# =======================================================================================================================

def test_off_mode_reads_the_named_appraisal_only_orders_chronologically_and_carries_no_coverage(db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2022, 2025), "off")
    old, mid, new = docs[2022], docs[2023], docs[2024]

    def ask_off(documents: list[str], picks: list[tuple], answer, on: bool):
        cid = new_conversation(client)
        set_switch(o, cid, on)
        return first_turn(o, cid, VALUE_Q.format(street="הגפן 15"), steps_for(picks, answer, documents, sentence=UNIT_VAT))

    # one named appraisal: off reads it alone; on reads the property
    off = ask_off([mid], [(mid, amount_of(2023))], answer_with("V1", amount_of(2023), "הגפן 15", vat=True), on=False)
    assert set(first_search_documents(off)) == {mid} and source_documents(off) == {mid}
    output = next(c for c in off.tools if c.name == "search").output
    for wording in (T.MSG_PROPERTY_SEARCHED, "כיסוי השומות", LATEST_CHECKED):
        assert wording not in output, wording
    assert off.answer["time"] == {"mode": "none", "prefer_recent": False, "requested": None, "timeline": [], "breakdown": [],
                                    "text": None}
    on = ask_off([mid], [(mid, amount_of(2023))], answer_with("V1", amount_of(2023), "הגפן 15", vat=True), on=True)
    assert set(first_search_documents(on)) == {old, mid, new} and T.MSG_PROPERTY_SEARCHED in next(
        c for c in on.tools if c.name == "search").output
    assert on.answer["time"]["coverage"]["complete"]

    # the appraisals the model names itself: off keeps the chronological order of the timeline, oldest first
    picks = [(new, amount_of(2024)), (old, amount_of(2022)), (mid, amount_of(2023))]
    off_three = ask_off([old, mid, new], picks, answer_with("V1", amount_of(2024), "הגפן 15", vat=True), on=False)
    assert [r["date"] for r in rows_of(off_three)] == ["2022-06-30", "2023-06-30", "2024-06-30"]
    assert "coverage" not in off_three.answer["time"] and off_three.answer["time"]["text"] is not None
    assert LATEST_CHECKED not in off_three.answer["time"]["text"]


# =======================================================================================================================
# (10) U12b: the date a cited value's record resolves backs the same date wherever the model wrote it (S3 trace)
# =======================================================================================================================

MODEL_ASSERTED = "(קביעת המודל, לא נמצא במקור)"


def date_in_record_judge(input: str) -> dict:
    """The judge of the round-12 S3 trace: shown a value whose scenario/date is marked as the model's assertion (not found in
    the source), it supports the amount and cuts the date words ("the date is not in the quote and was recorded as the
    model's"); shown a record that does not mark the date so, it supports the unit."""
    indexes = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
    asserted = re.search(r"תרחיש/מועד: [^;\n]*" + re.escape(MODEL_ASSERTED), input) is not None
    verdict = {"verdict": "partial", "defect": "part", "reason": "הסכום נתמך; התאריך אינו מופיע בציטוט המקור ונרשם בו כקביעת המודל"
               } if asserted else {"verdict": "supported", "defect": "none", "reason": "בדיקה"}
    return {"verdicts": [{"index": i, "supported_by": [], **verdict} for i in indexes]}


def headline_steps(gafen: str, scenario: str, written: str) -> list:
    """The headline value of the compendium (3,100,000, valued 30/06/2025), registered with ``scenario`` and given with the
    date ``written`` in the answer."""
    body = f"השווי למועד הקובע {written} הוא 3,100,000 ₪ [V1]."
    answer = final(body, focus=_focus_of() | {"subject": "הגפן 15", "vat": "unknown", "metric_as_written": "שווי הנכס"},
                   claims=[{"text": body.rsplit(" [", 1)[0], "source_ids": ["V1"], "basis": "explicit"}])
    return steps_for([(gafen, "3,100,000")], answer, [gafen], sentence=UNIT_SENTENCE, scenario=scenario)


def test_s3_a_scenario_that_is_the_values_own_resolved_date_is_no_assertion_of_the_model_and_the_headline_keeps_its_date(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    gafen, _ = compendium(o, "compendium_one")  # 3,100,000 valued 30/06/2025; the unit's quote holds no date
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"),
                   headline_steps(gafen, "למועד הקובע 30/06/2025", "30/06/2025"), judge=date_in_record_judge)
    a = t.answer
    (v,) = a["values"]
    assert v["as_of"]["date"] == "2025-06-30" and v["as_of"]["provenance"] == "unit_valuation"
    assert v["provenance"]["scenario"] == "not_stated" and not v["scenario"], "the date is the record's, not a scenario"
    assert a["markdown"] == "השווי למועד הקובע 30/06/2025 הוא 3,100,000 ₪ [V1]." and a["verification"]["removed"] == 0
    assert a["verification"]["actions"]["cut"] == 0 and a["status"] == "answered"
    judged = "\n".join(c.input for c in t.agent.calls if "<unit index" in c.input)
    assert "תרחיש/מועד:" not in judged, "the date is the record's (as_of), so no scenario reaches the judge"
    assert not re.search(r"תרחיש/מועד: [^;\n]*" + re.escape(MODEL_ASSERTED), judged), "never read as the model's"


def test_s3_a_scenario_that_is_not_the_values_resolved_date_stays_the_models_and_the_answer_is_not_verified_as_clean(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    gafen, _ = compendium(o, "compendium_one")
    # the model writes another unit's date for the headline value (valued 30/06/2025): the record does not back it, so the date
    # stays the model's assertion and the judge still reads it as one
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"),
                   headline_steps(gafen, "למועד הקובע 30/06/2024", "30/06/2024"), judge=date_in_record_judge)
    (v,) = t.answer["values"]
    assert v["as_of"]["date"] == "2025-06-30" and v["provenance"]["scenario"] == "model_asserted"
    assert t.answer["verification"]["partial"] == 1 and t.answer["verification"]["correctness"] == "partial"
    # the unit's date with a word of the model's own is an assertion too, not the date alone
    t2 = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"),
                    headline_steps(gafen, "תרחיש אופטימי למועד הקובע 30/06/2025", "30/06/2025"), judge=date_in_record_judge)
    assert t2.answer["values"][0]["provenance"]["scenario"] == "model_asserted"
    assert t2.answer["verification"]["partial"] == 1


def test_s3_two_appraisals_of_one_property_whose_scenarios_restate_their_own_dates_stay_one_timeline_group(
        db, client, monkeypatch, calls):
    """A scenario that is only the value's own date is the date (``as_of``), not a scenario: it must not split the property's
    appraisals into one timeline group per date (review finding 1)."""
    o = bare_world(db, client, monkeypatch, calls)
    old = unit_appraisal(o, "own-date-2022", valuation="15/04/2022", amount=amount_of(2022), vat=True, **PROPERTY_X)
    new = unit_appraisal(o, "own-date-2025", valuation="30/06/2025", amount=amount_of(2025), vat=True, **PROPERTY_X)
    body = (f"השווי העדכני הוא {amount_of(2025)} ₪ כולל מע״מ [V1]. קודם לכן, ב-15/04/2022, השווי היה {amount_of(2022)} ₪ "
            "כולל מע״מ [V2].")
    answer = final(body, focus=_focus_of() | {"subject": "הגפן 15", "metric_as_written": "שווי הנכס"}, claims=[
        {"text": f"השווי העדכני הוא {amount_of(2025)} ₪ כולל מע״מ", "source_ids": ["V1"], "basis": "explicit"},
        {"text": f"קודם לכן, ב-15/04/2022, השווי היה {amount_of(2022)} ₪ כולל מע״מ", "source_ids": ["V2"], "basis": "explicit"}])

    def register(items):
        return [amount_take(sid_with(items, new, amount_of(2025)), amount_of(2025), sentence=UNIT_VAT,
                            scenario="למועד הקובע 30/06/2025"),
                amount_take(sid_with(items, old, amount_of(2022)), amount_of(2022), sentence=UNIT_VAT,
                            scenario="למועד הקובע 15/04/2022")]

    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"),
                   [[call("search", query=VALUE_SEARCH, document_ids=[new, old], limit=None)], register, answer])
    a = t.answer
    assert [(v["as_of"]["date"], v["provenance"]["scenario"], v["scenario"]) for v in a["values"]] == [
        ("2025-06-30", "not_stated", ""), ("2022-04-15", "not_stated", "")], "each scenario is just its own date"
    groups = a["time"]["timeline"]
    assert len(groups) == 1, "one property, one history"
    assert [r["date"] for r in groups[0]["rows"]] == ["2025-06-30", "2022-04-15"]


# =======================================================================================================================
# (11) U12b (S2): the server's link reaches the judge, so a linked appraisal's value at another address is kept
# =======================================================================================================================

LINKED = "שומה של אותו נכס"


def subject_judge(input: str) -> dict:
    """The judge of the S2 trace: shown the value of the appraisal at another house number (הדס 3) for a question about הדס 5,
    it marks the claim ``wrong_subject`` unless the record it is shown says the server linked that appraisal to the property
    ("שומה של אותו נכס: ...")."""
    out = []
    for m in re.finditer(r'<unit index="(\d+)"[^>]*>(.*?)</unit>', input, re.S):
        disowned = "3,200,000" in m[2] and LINKED not in input
        out.append({"index": int(m[1]), "supported_by": [], "reason": "הערך שייך לנכס אחר (הדס 3)" if disowned else "בדיקה",
                    **({"verdict": "unsupported", "failure": "wrong_subject"} if disowned else {"verdict": "supported"})})
    return {"verdicts": out}


def test_s2_the_linked_appraisal_at_another_house_number_carries_the_link_to_the_model_and_the_judge_and_its_value_is_kept(
        db, client, monkeypatch, calls):
    o = world(db, client, monkeypatch, calls, ("parcels_ab", "parcels_b", "parcels_c"))
    a_doc, b_doc = o.ids["parcels_ab"], o.ids["parcels_b"]
    focus = _focus_of() | {"subject": "הדס 5", "vat": "unknown", "metric_as_written": "שווי הנכס"}
    body = "השווי העדכני הוא 3,650,000 ₪ [V1]. קודם לכן, ב-30/06/2023, השווי היה 3,200,000 ₪ [V2]."
    answer = final(body, focus=focus, claims=[
        {"text": "השווי העדכני הוא 3,650,000 ₪", "source_ids": ["V1"], "basis": "explicit"},
        {"text": "קודם לכן, ב-30/06/2023, השווי היה 3,200,000 ₪", "source_ids": ["V2"], "basis": "explicit"}])
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הדס 5"), steps_for(
        [(b_doc, "3,650,000"), (a_doc, "3,200,000")], answer, [b_doc]), judge=subject_judge)
    a = t.answer
    assert a["status"] == "answered" and "3,200,000" in a["markdown"] and a["verification"]["removed"] == 0, a["verification"]
    # the model read the link in the search result: on the linked appraisal's sources and in the property line
    search = t.agent.tool_outputs(1)[0]
    want = T.MSG_LINK_SAME_PROPERTY.format(why=T.MSG_LINK_PARCEL.format(parcels="8401/40"))
    assert f'link="{want}"' in search and T.MSG_PROPERTY_UNITS_HEAD in search
    assert not re.search(rf'document_id="{b_doc}"[^>]*link=', search), "the focus appraisal has no link"
    assert {s["link"] for s in a["sources"] if s["document_id"] == a_doc} == {want}
    # the judge read it with that source's evidence (a V#'s source, a passage's header)
    judged = "\n".join(c.input for c in t.agent.calls if "<unit index" in c.input)
    assert want in judged


# =======================================================================================================================
# (12) U12b (S3): a timed turn answered from a read source records what was only read
# =======================================================================================================================

def read_steps(doc: str, amount: str, answer, sentence: str = UNIT_VAT) -> list:
    """No time-ranked search: the model reads page 1 of the appraisal it already knows, registers the value and answers."""
    def register(items):
        return [amount_take(sid_with(items, doc, amount), amount, sentence=sentence)]

    return [[read(pages={"document": doc, "from_page": 1, "to_page": 1})], register,
            *(answer if isinstance(answer, list) else [answer])]


def test_s3_a_property_named_by_the_documents_it_read_has_a_coverage_of_reads_only_that_is_partial_and_names_the_unchecked(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2022, 2025), "ro")
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"),
                   read_steps(docs[2024], amount_of(2024), latest_claim("V1", amount_of(2024), QUALIFIED)))
    a = t.answer
    assert [c.name for c in t.tools if c.name == "search"] == [], "the model read, it did not search"
    cov = a["time"]["coverage"]
    assert cov["scope"] == "property" and cov["identified"] and cov["via"] == "documents" and cov["read_only"] is True
    assert {u["document_id"]: u["status"] for u in cov["units"]} == {
        docs[2024]: T.READ, docs[2023]: T.NOT_SEARCHED, docs[2022]: T.NOT_SEARCHED}
    assert [u["date"] for u in cov["units"]] == ["2024-06-30", "2023-06-30", "2022-06-30"]
    assert not cov["complete"] and cov["blocking"] == [T.B_NOT_SEARCHED]
    assert [u["document_id"] for u in cov["units"] if u["blocks"]] == [docs[2023], docs[2022]]
    lines = cov["note"].splitlines()
    assert lines[0] == QUALIFIED + "." and lines[1].startswith("נבדקו רק המקורות שנקראו; לא נבדקו")
    assert "שומה סינתטית ro-2023" in lines[1] and "שומה סינתטית ro-2022" in lines[1] and "ro-2024" not in lines[1]
    assert a["time"]["text"].endswith(cov["note"])
    assert a["status"] == "answered" and a["markdown"].startswith(QUALIFIED)


def test_s3_the_unqualified_latest_claim_of_a_reads_only_answer_is_repaired_like_any_partial_coverage(db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2022, 2025), "rr")
    t = first_turn(o, new_conversation(client), VALUE_Q.format(street="הגפן 15"), read_steps(
        docs[2024], amount_of(2024), [latest_claim("V1", amount_of(2024), "השווי העדכני"),
                                      latest_claim("V1", amount_of(2024), QUALIFIED)]))
    assert t.answer["markdown"] == f"{QUALIFIED} הוא {amount_of(2024)} ₪ כולל מע״מ [V1]." and t.answer["verification"]["removed"] == 0
    assert "recency" in json.dumps(m_diagnostics(client, t.message), ensure_ascii=False)


def test_s3_a_follow_up_in_the_conversations_property_that_reads_one_appraisal_is_partial_and_off_mode_records_nothing(
        db, client, monkeypatch, calls):
    o = bare_world(db, client, monkeypatch, calls)
    docs = chain(o, range(2022, 2025), "rf")
    cid = new_conversation(client)
    first = first_turn(o, cid, VALUE_Q.format(street="הגפן 15"), steps_for(
        [(docs[2024], amount_of(2024))], answer_with("V1", amount_of(2024), "הגפן 15", vat=True), [docs[2024]], sentence=UNIT_VAT))
    assert first.answer["time"]["coverage"]["complete"], "the search checked the property"
    answer = final(f"השווי ב-2023 היה {amount_of(2023)} ₪ כולל מע״מ [V1].",
                   focus=_focus_of() | {"subject": "הגפן 15", "metric_as_written": "שווי הנכס"},
                   claims=[{"text": f"השווי ב-2023 היה {amount_of(2023)} ₪ כולל מע״מ", "source_ids": ["V1"], "basis": "explicit"}])
    second = follow_up(o, cid, "ומה היה השווי ב-2023?", read_steps(docs[2023], amount_of(2023), answer))
    cov = second.answer["time"]["coverage"]
    assert second.answer["time"]["mode"] == "explicit" and [c.name for c in second.tools if c.name == "search"] == []
    assert cov["read_only"] is True and cov["scope"] == "property"
    assert {u["document_id"]: u["status"] for u in cov["units"]} == {
        docs[2024]: T.NOT_SEARCHED, docs[2023]: T.READ, docs[2022]: T.NOT_SEARCHED}
    assert not cov["complete"] and cov["note"].splitlines()[0] == "הנתונים לתקופה שביקשת נמצאו רק במקורות שנבדקו."
    assert "נבדקו רק המקורות שנקראו; לא נבדקו" in cov["note"] and "ro" not in cov["note"]
    # the switch off and no requested time: round 11's answer, no coverage record (R14)
    off_cid = new_conversation(client)
    set_switch(o, off_cid, False)
    off = first_turn(o, off_cid, VALUE_Q.format(street="הגפן 15"), read_steps(
        docs[2024], amount_of(2024), answer_with("V1", amount_of(2024), "הגפן 15", vat=True)))
    assert off.answer["time"]["mode"] == "none" and "coverage" not in off.answer["time"]


def test_s3_a_page_read_of_a_compendium_marks_only_the_unit_of_that_page_as_read(db, client, monkeypatch, calls):
    """A file of six units of one property (one page each, the cover on page 1): the 2022 unit is page 4. The answer cites the
    value taken from that page, so that unit is ``read`` and the five others are ``not_searched`` (R10: per unit, never per file)."""
    o = bare_world(db, client, monkeypatch, calls)
    gafen, _ = compendium(o, "compendium_one")
    body = "השווי ב-2022 היה 2,500,000 ₪ [V1]."
    answer = final(body, focus=_focus_of() | {"subject": "הגפן 15", "vat": "unknown", "metric_as_written": "שווי הנכס"},
                   claims=[{"text": "השווי ב-2022 היה 2,500,000 ₪", "source_ids": ["V1"], "basis": "explicit"}])
    steps = [[read(pages={"document": gafen, "from_page": 4, "to_page": 4})],
             lambda items: [amount_take(sid_with(items, gafen, "2,500,000"), "2,500,000", sentence=UNIT_SENTENCE)], answer]
    t = first_turn(o, new_conversation(client), "מה היה השווי ב-2022 של הנכס ברחוב הגפן 15?", steps)
    cov = t.answer["time"]["coverage"]
    assert t.answer["time"]["mode"] == "explicit" and cov["scope"] == "property" and cov["read_only"] is True
    assert {u["date"]: u["status"] for u in cov["units"]} == {
        "2025-06-30": T.NOT_SEARCHED, "2024-06-30": T.NOT_SEARCHED, "2023-06-30": T.NOT_SEARCHED, "2022-06-30": T.READ,
        "2021-06-30": T.NOT_SEARCHED, "2020-06-30": T.NOT_SEARCHED}
    assert not cov["complete"] and cov["blocking"] == [T.B_NOT_SEARCHED]
