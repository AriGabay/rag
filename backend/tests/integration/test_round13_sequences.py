"""Round 13 sequences, end to end with a scripted model (U15: R3, R8, R12-R19, R24; AE1-AE9).

The synthetic round-13 fixtures (``tests/fixtures/round13``: single appraisals read by the real worker, and an office of
unidentified units built with ``tests.support.unit_files``) go through the whole turn: the scripted agent searches, registers
values and answers. What is proven is what the server decides, stores and shows, never wording, and each layer on its own:

- (a) the verification report: the answer's ``verification`` counts and the diagnostics' removed decisions;
- (b) the coverage record: ``answer.time.coverage``;
- (c) the timeline and the breakdown (the components table): ``answer.time.timeline`` and ``answer.time.breakdown``, the marks
  of their rows and the counterparts they name (``undecided_with``, ``conflict_with``);
- (d) the stored answer read back with ``GET /api/chat/messages/{id}`` after the turn: its time block, its values with their
  facets and its coverage note equal the payload the turn built (R24).

Sequences: (1) complementary coefficients (AE1); (2) rates of different cash flows (AE2); (3) a conflict across appraisals and
equal written forms (AE3, AE4); (4) a derived reduction and the server-held date (AE5, AE6); (5) a forced removal (AE7); (6) the
undetermined appraisals of an office (AE9). Every name, address and amount is invented."""

# ruff: noqa: F811 - the tests take the imported ``calls`` fixture by name, as the round-12 sequences do
from __future__ import annotations

import hashlib
import json
import re

import pytest

from app.chat import api as chat_api
from app.chat import timeframe as TF
from app.chat import timeline as TL
from app.chat.verify import SERVER_HELD
from tests.integration.test_chat import new_conversation
from tests.integration.test_chat_timeline import ADOPTED, _focus_of
from tests.integration.test_property_scope import FIXTURES, upload
from tests.integration.test_round10_sequences import (
    calls,  # noqa: F401 - the recorded tool calls (the fixture)
)
from tests.integration.test_round10_sequences import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.integration.test_round12_inventory import UNIT_KEYS
from tests.integration.test_round12_sequences import bare_world, first_turn, sids_of
from tests.support.scripted_agent import call, final
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

MANIFEST13 = json.loads((FIXTURES / "round13" / "manifest.json").read_text(encoding="utf-8"))
DOCS13 = MANIFEST13["documents"]
DATUM = {d["id"]: d | {"doc": key} for key, spec in DOCS13.items() for d in spec.get("data", [])}
NO_FACET = TL.MSG_NO_FACET  # "רכיב לא צוין במקור"
FACET_KEYS = ("status", "written", "part", "tier")


# --- the world ---------------------------------------------------------------------------------------------------------

def world13(db, client, monkeypatch, calls, keys: tuple[str, ...], email: str = "admin-r13s@example.test"):
    """A synthetic office with the given round-13 fixtures uploaded through the worker (single appraisals), cloud use on."""
    o = bare_world(db, client, monkeypatch, calls, email)
    for key in keys:
        spec = DOCS13[key]
        o.ids[key] = upload(o, FIXTURES / spec["file"], spec["source_kind"], monkeypatch)
    o.by_doc = {v: k for k, v in o.ids.items()}
    return o


@pytest.fixture
def live(monkeypatch) -> list[dict]:
    """The payload the turn built (``api._answer_payload``), as JSON, in order: what the live answer carried before it was stored."""
    seen: list[dict] = []
    real = chat_api._answer_payload

    def payload(*args, **kwargs):
        out = real(*args, **kwargs)
        seen.append(json.loads(json.dumps(out, ensure_ascii=False, default=str)))
        return out

    monkeypatch.setattr(chat_api, "_answer_payload", payload)
    return seen


# --- scripts -----------------------------------------------------------------------------------------------------------

def number_of(d: dict) -> str:
    """The digits a datum is written with ("6.5%" -> "6.5")."""
    return re.sub(r"[^\d.,]", "", d["shown"].split()[0])


def take(o, datum_id: str, items: list, **meaning) -> dict:
    """A ``take_value`` of a manifest datum: its table cell (row label, column header) or its quote, from the hit of its
    document that holds it, with the kind and unit the manifest gives."""
    d = DATUM[datum_id]
    doc, where = o.ids[d["doc"]], d["where"]
    if "table" in where:
        t = where["table"]
        locator = {"table": None, "row": t["row_label"], "row_number": None, "column": t["column_header"],
                   "column_number": None, "quote": None, "number": number_of(d)}
        needles = (t["row_label"], number_of(d))  # the table's hit: the one that holds the row and the number
    else:
        quote = where["quote"].rstrip(".")
        locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": quote,
                   "number": number_of(d)}
        needles = (quote,)
    base = ADOPTED | {"kind": d["kind"], "unit": d["unit"], "subject": "הנכס"}
    return call("take_value", source=hit_with(items, doc, *needles), locator=locator, meaning=base | meaning, label=datum_id)


def hit_with(items: list, doc: str, *needles: str) -> str:
    """The S# of the hit of ``doc`` whose body holds every needle."""
    hits = sids_of(items).get(doc, [])
    for hit in hits:
        if all(n in hit["body"] for n in needles):
            return hit["id"]
    raise AssertionError(f"no hit of {doc} holds {needles!r}: {[(h['id'], h['body']) for h in hits]}")


def sentence_of(datum_id: str) -> str:
    """How the answer writes a datum: its facet's words and its number as the source writes it."""
    d = DATUM[datum_id]
    return f"{(d['facet'] or {}).get('written') or 'שיעור היוון'} הוא {d['shown'].split()[0]}"


def search_for(o, query: str, keys: tuple[str, ...], datum_ids: list[str] = ()) -> list:
    """The model's first step: a search per document with ``query``, and one per datum it will take, by that datum's own row or
    sentence (a search returns the passages that rank best, and of a table only some rows, so a row a test takes a value from must
    be asked for)."""
    asked = [(o.ids[k], query) for k in keys]
    for i in datum_ids:
        d = DATUM[i]
        asked.append((o.ids[d["doc"]], d["where"]["table"]["row_label"] if "table" in d["where"] else d["where"]["quote"].rstrip(".")))
    return [call("search", query=q, document_ids=[doc], limit=None) for doc, q in dict.fromkeys(asked)]


def registering(o, datum_ids: list[str], **meaning):
    def register(items):
        return [take(o, i, items, **meaning) for i in datum_ids]

    return register


def answer_of(body: str, subject: str, kind: str = "coefficient", unit: str = "ratio", documents: list[str] = (), **extra) -> dict:
    """An answer whose text is ``body`` (it cites V# itself); its focus names ``documents``."""
    focus = _focus_of() | {"subject": subject, "metric_kind": kind, "unit": unit, "vat": "unknown", "metric_as_written": subject,
                           "document_ids": list(documents)}
    return final(body, focus=focus, claims=[], **extra)


def listing(datum_ids: list[str]) -> str:
    """One sentence that gives every datum in turn and cites it (V1, V2 ... in the order registered)."""
    return "; ".join(f"{sentence_of(i)} [V{n}]" for n, i in enumerate(datum_ids, 1)) + "."


def ask13(o, question: str, steps: list, **agent):
    return first_turn(o, new_conversation(o.client), question, steps, **agent)


# --- the layers --------------------------------------------------------------------------------------------------------

def diagnostics_removed(o, t) -> list[dict]:
    """The decisions of what the turn took out of the answer (diagnostics, the owner's)."""
    return o.client.get(f"/api/chat/messages/{t.message['id']}/diagnostics").json()["removed"]


def stored_of(o, t) -> dict:
    """The answer read back after the turn (``GET /api/chat/messages/{id}``)."""
    r = o.client.get(f"/api/chat/messages/{t.message['id']}")
    assert r.status_code == 200, r.text
    return r.json()["answer"]


def facets_of(answer: dict) -> dict[str, dict]:
    return {v["id"]: {k: v["facet"].get(k) for k in FACET_KEYS} for v in answer["values"]}


def rows_in(blocks: list[dict]) -> dict[str, dict]:
    """sid -> row, over the rows of the blocks given (a timeline's or a breakdown's)."""
    return {r["sid"]: r for b in blocks for r in b["rows"]}


def marks_of(blocks: list[dict]) -> dict[str, list[str]]:
    return {sid: r["marks"] for sid, r in rows_in(blocks).items()}


def refreshed_equals_live(o, t, live: list[dict]) -> dict:
    """(d) R24: the answer read back holds the same time block (timeline, breakdown, marks, counterparts, coverage record and
    note, copy text), the same values with their facets and the same markdown as the payload the turn built. Returns it."""
    stored, built = stored_of(o, t), live[-1]
    assert stored["time"] == built["time"], "the saved time block differs from the live one"
    assert stored["markdown"] == built["markdown"] and facets_of(stored) == facets_of(built)
    assert [v["id"] for v in stored["values"]] == [v["id"] for v in built["values"]]
    assert stored["time"].get("coverage", {}).get("note") == built["time"].get("coverage", {}).get("note")
    assert stored["time"]["text"] == built["time"]["text"]
    return stored


def kept_whole(o, t) -> None:
    """(a) The verification report: nothing was removed, cut, edited or rewritten, and the answer is verified."""
    v = t.answer["verification"]
    assert v["removed"] == 0 and v["edited"] == 0 and v["correctness"] == "verified", v
    assert not v["removals"] and not any(v["actions"][k] for k in ("removed", "cut", "rewritten", "withheld")), v["actions"]
    assert diagnostics_removed(o, t) == []


# =======================================================================================================================
# (1) complementary coefficients in one table (AE1, R1, R3, R8, R9)
# =======================================================================================================================

LAND = ["land.coef_building", "land.coef_sharing"]
LAND_Q = "מהם מקדמי ההפחתה בנכס ברחוב הצבעוני 14?"


def land_turn(db, client, monkeypatch, calls, body: str | None = None):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    text = body or listing(LAND)
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), answer_of(text, "הצבעוני 14")]
    return o, ask13(o, LAND_Q, steps)


def test_ae1_two_coefficients_of_two_considerations_are_two_components_with_no_mark_and_no_timeline_block(
        db, client, monkeypatch, calls, live):
    o, t = land_turn(db, client, monkeypatch, calls)
    a = t.answer
    assert a["status"] == "answered"
    # (a) the verification report: both numbers are grounded, nothing is removed
    kept_whole(o, t)
    assert "0.8" in a["markdown"] and "0.9" in a["markdown"]
    # the values carry the facets the table's rows give them
    expected = {f"V{n}": {k: DATUM[i]["facet"][k] for k in FACET_KEYS} for n, i in enumerate(LAND, 1)}
    assert facets_of(a) == expected
    # (b) the coverage record: the one appraisal that was read, the search window's
    cov = a["time"]["coverage"]
    assert [(u["document_id"], u["status"], u["date"]) for u in cov["units"]] == [(o.ids["land_table"], "read", "2023-03-01")]
    assert cov["scope"] == "window" and not cov["complete"] and cov["candidates"]["count"] == 0
    assert a["time"]["text"].endswith(cov["note"])
    # (c) the timeline holds no block that joins them; the breakdown is one block of the unit at its date, a row per consideration
    t_ = a["time"]
    assert t_["timeline"] == []
    (block,) = t_["breakdown"]
    assert block["as_of"]["date"] == "2023-03-01" == DOCS13["land_table"]["valuation_date"]
    assert [(r["sid"], r["facet"], r["value_text"]) for r in block["rows"]] == [
        ("V1", DATUM[LAND[0]]["facet"]["written"], "0.8"), ("V2", DATUM[LAND[1]]["facet"]["written"], "0.9")]
    assert all(r["marks"] == [] and "undecided_with" not in r and "conflict_with" not in r for r in block["rows"])
    assert "conflict" not in json.dumps(t_, ensure_ascii=False) and "undecided" not in json.dumps(t_, ensure_ascii=False)
    assert t_["text"].startswith(TL.BREAKDOWN_HEADING)
    # (d) after a refresh
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# (2) rates of different cash flows in one section (AE2, R1, R2, R3, R6, R10)
# =======================================================================================================================

CAP = ["cap.offices", "cap.parking", "cap.storage", "cap.residual", "cap.billboard", "cap.bare"]
CAP_Q = "מהם שיעורי ההיוון בנכס ברחוב הכלנית 8?"


def cap_turn(db, client, monkeypatch, calls, datum_ids: list[str] | None = None):
    o = world13(db, client, monkeypatch, calls, ("cap_rates",))
    ids = datum_ids or CAP
    steps = [search_for(o, "שיעור היוון", ("cap_rates",), ids), registering(o, ids, kind="rate", unit="percent"),
             answer_of(listing(ids), "הכלנית 8", "rate", "percent")]
    return o, ask13(o, CAP_Q, steps)


def test_ae2_six_rates_of_five_cash_flows_and_a_bare_one_are_six_components_the_bare_one_alone_undecided_never_a_conflict(
        db, client, monkeypatch, calls, live):
    o, t = cap_turn(db, client, monkeypatch, calls)
    a = t.answer
    # (a) the verification report
    kept_whole(o, t)
    for amount in ("6.5%", "7.25%", "8.5%", "9%", "10%"):
        assert amount in a["markdown"]
    # the values: a facet of its own sentence, one of the list's lead-in, none
    facets = facets_of(a)
    assert {f"V{n}": facets[f"V{n}"]["status"] for n in range(1, 7)} == {
        f"V{n}": DATUM[i]["facet"]["status"] for n, i in enumerate(CAP, 1)}
    assert [facets[f"V{n}"]["written"] for n in range(1, 6)] == [DATUM[i]["facet"]["written"] for i in CAP[:5]]
    assert facets["V5"] == {k: DATUM["cap.billboard"]["facet"][k] for k in FACET_KEYS} and facets["V5"]["part"] == "lead_in"
    assert facets["V6"] == {"status": "none", "written": None, "part": None, "tier": None}
    # (b) the coverage record: one appraisal, read
    cov = a["time"]["coverage"]
    assert [(u["document_id"], u["status"]) for u in cov["units"]] == [(o.ids["cap_rates"], "read")]
    # (c) no timeline block; one components block of the unit and its date, a row for every rate: the two 7.25% stay two rows
    assert a["time"]["timeline"] == []
    (block,) = a["time"]["breakdown"]
    assert block["as_of"]["date"] == "2023-09-30"
    rows = block["rows"]
    assert [r["sid"] for r in rows] == [f"V{n}" for n in range(1, 7)]
    assert [r["value_text"] for r in rows] == ["6.5", "7.25", "7.25", "8.5", "9", "10"]
    assert [r["facet"] for r in rows] == [DATUM[i]["facet"]["written"] for i in CAP[:5]] + [TL.FACET_TEXT[("none", "not_in_range")]]
    # the bare rate could not be compared with any other rate: only its row carries the undecided mark, naming the five it was
    # set against (a row with a stated component carries none because of a bare one); never a conflict
    by_sid = rows_in(a["time"]["breakdown"])
    assert by_sid["V6"]["marks"] == ["undecided"] and by_sid["V6"]["undecided_with"] == ["V1", "V2", "V3", "V4", "V5"]
    assert all(by_sid[f"V{n}"]["marks"] == [] and "undecided_with" not in by_sid[f"V{n}"] for n in range(1, 6))
    assert not any("conflict" in r["marks"] or "conflict_with" in r for r in rows)
    assert TL.MARKS["undecided"] in a["time"]["text"] and TL.MARKS["conflict"] not in a["time"]["text"]
    assert a["requested"] == [] and a["gaps"] == [], "no conflict was claimed or kept"
    # (d) after a refresh
    stored = refreshed_equals_live(o, t, live)
    assert rows_in(stored["time"]["breakdown"])["V6"]["undecided_with"] == ["V1", "V2", "V3", "V4", "V5"]
    assert rows_in(stored["time"]["breakdown"])["V1"]["marks"] == []


def test_ae2_rates_whose_cash_flows_are_all_named_stay_apart_with_no_mark_of_any_kind(db, client, monkeypatch, calls, live):
    o, t = cap_turn(db, client, monkeypatch, calls, CAP[:5])
    kept_whole(o, t)
    (block,) = t.answer["time"]["breakdown"]
    assert [r["marks"] for r in block["rows"]] == [[]] * 5 and t.answer["time"]["timeline"] == []
    assert [r["value_text"] for r in block["rows"]] == ["6.5", "7.25", "7.25", "8.5", "9"]
    assert "undecided" not in json.dumps(t.answer["time"], ensure_ascii=False)
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# (3) one coefficient in four appraisals of one property: a conflict, and equal written forms (AE3, AE4, R4, R5)
# =======================================================================================================================

CONFLICT_KEYS = ("conflict_1", "conflict_2", "conflict_3", "conflict_4")
CONFLICT_COEF = [f"{k}.coef" for k in CONFLICT_KEYS]
CONFLICT_Q = "מה מקדם ההפחתה בשל מגבלות בנייה בנכס ברחוב הצפצפה 21?"
CONFLICT_LABEL = "מקדם הפחתה בשל מגבלות בנייה"


def conflict_turn(db, client, monkeypatch, calls, keys: tuple[str, ...], extra: list[str] = (), claim: str | None = "sources_conflict"):
    o = world13(db, client, monkeypatch, calls, keys)
    ids = [f"{k}.coef" for k in keys] + list(extra)
    body = "; ".join(f"{k.replace('_', ' ')}: {DATUM[i]['facet']['written']} {DATUM[i]['shown']} [V{n}]" for n, (k, i) in enumerate(
        ((i.split('.')[0], i) for i in ids), 1)) + "."
    requested = [{"component": "", "label": CONFLICT_LABEL, "document_ids": [o.ids[k] for k in keys], "status": claim,
                  "checked_where": ""}] if claim else None
    steps = [search_for(o, CONFLICT_LABEL, keys, ids), registering(o, ids), answer_of(body, "הצפצפה 21", requested=requested)]
    return o, ask13(o, CONFLICT_Q, steps)


def test_ae3_a_coefficient_written_0_85_against_0_8_in_two_appraisals_of_one_property_is_a_conflict_the_equal_forms_are_not(
        db, client, monkeypatch, calls, live):
    o, t = conflict_turn(db, client, monkeypatch, calls, CONFLICT_KEYS)
    a = t.answer
    # (a) the verification report: the conflict is stated by the server, nothing is removed; the status is partial
    kept_whole(o, t)
    assert a["status"] == "partial"
    # the reason: the sources conflict (kept because the timeline shows a conflict mark), stated once as a gap
    (req,) = a["requested"]
    assert req["status"] == req["claimed"] == "sources_conflict" and req["kind"] == "sources_conflict"
    assert [g["reason"] for g in a["gaps"]] == ["sources_conflict"] and a["gaps"][0]["text"] in a["markdown"]
    # (b) the coverage record: the property's four appraisals, all read, nothing blocks
    cov = a["time"]["coverage"]
    assert cov["scope"] == "property" and cov["complete"] and cov["blocking"] == [] and len(cov["units"]) == 4
    assert {u["document_id"] for u in cov["units"]} == {o.ids[k] for k in CONFLICT_KEYS}
    assert cov["property"]["reasons"] == ["parcel 9103/5"] and cov["property"]["units"] == 4
    # (c) one timeline block of the one datum, no components block: the row of 0.85 conflicts with the three others; the three
    # equal forms (0.8, "80%", 0.80) conflict with it alone, never with each other
    assert a["time"]["breakdown"] == []
    (block,) = a["time"]["timeline"]
    rows = {r["sid"]: r for r in block["rows"]}
    assert {sid: r["value_text"] for sid, r in rows.items()} == {"V1": "0.8", "V2": "0.85", "V3": "80", "V4": "0.80"}
    assert all(r["marks"] == ["conflict"] for r in rows.values())
    assert {sid: r["conflict_with"] for sid, r in rows.items()} == {
        "V1": ["V2"], "V2": ["V1", "V3", "V4"], "V3": ["V2"], "V4": ["V2"]}
    assert not any("undecided" in r["marks"] for r in rows.values())
    assert TL.MARKS["conflict"] in a["time"]["text"] and "(V1, V3, V4)" in a["time"]["text"]
    # (d) after a refresh: the marks and the counterparts they name are the live ones
    stored = refreshed_equals_live(o, t, live)
    assert {r["sid"]: r["conflict_with"] for r in stored["time"]["timeline"][0]["rows"]}["V2"] == ["V1", "V3", "V4"]
    assert [g["reason"] for g in stored["gaps"]] == ["sources_conflict"]
    assert stored["requested"][0]["status"] == "sources_conflict"


def test_ae3_the_same_coefficient_written_as_a_fraction_a_percent_and_with_a_trailing_zero_is_equal_not_a_conflict(
        db, client, monkeypatch, calls, live):
    o, t = conflict_turn(db, client, monkeypatch, calls, ("conflict_1", "conflict_3", "conflict_4"))
    a = t.answer
    kept_whole(o, t)
    # the model claimed the sources conflict: the server does not keep it, there is no gap and no reason
    (req,) = a["requested"]
    assert req["claimed"] == "sources_conflict" and req["status"] == "found" and req["kind"] is None
    assert a["gaps"] == [] and a["status"] == "answered"
    # (c) the three forms are one datum: no conflict mark and no counterpart on any row, no components block
    marks = marks_of(a["time"]["timeline"])
    assert marks and all(m == [] for m in marks.values()), marks
    assert "conflict" not in json.dumps(a["time"], ensure_ascii=False) and a["time"]["breakdown"] == []
    # (b) the coverage record is the property's three appraisals
    assert len(a["time"]["coverage"]["units"]) == 3
    refreshed_equals_live(o, t, live)


def test_ae3_an_equal_location_coefficient_in_two_appraisals_is_no_conflict_beside_the_conflicting_reduction(
        db, client, monkeypatch, calls, live):
    o, t = conflict_turn(db, client, monkeypatch, calls, ("conflict_1", "conflict_2"),
                         extra=["conflict_1.location", "conflict_2.location"])
    a = t.answer
    kept_whole(o, t)
    # (c) each unit holds its reduction and its location coefficient side by side (a components block per appraisal), so no
    # timeline block is left; the conflict of the reduction is marked across the two blocks and names its counterpart there
    assert a["time"]["timeline"] == []
    blocks = a["time"]["breakdown"]
    assert [[r["sid"] for r in b["rows"]] for b in blocks] == [["V1", "V3"], ["V2", "V4"]]
    rows = rows_in(blocks)
    assert {sid: r["facet"] for sid, r in rows.items()} == {
        "V1": CONFLICT_LABEL, "V2": CONFLICT_LABEL, "V3": "מקדם מיקום", "V4": "מקדם מיקום"}
    assert rows["V1"]["marks"] == ["conflict"] and rows["V1"]["conflict_with"] == ["V2"]
    assert rows["V2"]["marks"] == ["conflict"] and rows["V2"]["conflict_with"] == ["V1"]
    assert rows["V3"]["marks"] == [] and rows["V4"]["marks"] == [], "1.1 against 1.1 is equal"
    assert "conflict_with" not in rows["V3"] and "conflict_with" not in rows["V4"]
    # the reason is stated once: the reduction's sources conflict, the location's do not
    assert [g["reason"] for g in a["gaps"]] == ["sources_conflict"]
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# (4) the same cash flow written in two forms; a datum with no label across years (AE4, KTD3)
# =======================================================================================================================

FORMS = ["forms_1.first", "forms_1.second", "forms_2.first", "forms_2.second"]


def test_ae4_a_cash_flow_written_with_a_prefix_a_plural_and_quote_marks_is_compared_and_conflicts_while_equal_rates_do_not(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("forms_1", "forms_2"))
    steps = [search_for(o, "שיעור היוון", ("forms_1", "forms_2"), FORMS), registering(o, FORMS, kind="rate", unit="percent"),
             answer_of(listing(FORMS), "התבואה 5", "rate", "percent")]
    t = ask13(o, "מהם שיעורי ההיוון בנכס ברחוב התבואה 5?", steps)
    a = t.answer
    kept_whole(o, t)
    # (c) each appraisal is a components block of its two cash flows; the first flow (6.5% against 6.75%) is one datum across the
    # two appraisals and conflicts, the second (7.5% = 7.5%) is the same datum and equal; no pair is left undecided
    assert a["time"]["timeline"] == []
    blocks = a["time"]["breakdown"]
    assert [[r["sid"] for r in b["rows"]] for b in blocks] == [["V1", "V2"], ["V3", "V4"]]
    rows = rows_in(blocks)
    assert {sid: r["marks"] for sid, r in rows.items()} == {"V1": ["conflict"], "V2": [], "V3": ["conflict"], "V4": []}
    assert rows["V1"]["conflict_with"] == ["V3"] and rows["V3"]["conflict_with"] == ["V1"]
    assert "undecided" not in json.dumps(a["time"], ensure_ascii=False)
    # the facets are the sources' own words, written differently
    assert [rows[f"V{n}"]["facet"] for n in range(1, 5)] == [DATUM[i]["facet"]["written"] for i in FORMS]
    # (b) the coverage record: both appraisals of the property
    assert a["time"]["coverage"]["scope"] == "property" and len(a["time"]["coverage"]["units"]) == 2
    refreshed_equals_live(o, t, live)


def test_a_rate_with_no_cash_flow_in_two_appraisals_of_different_years_is_one_datum_in_a_history_not_a_conflict(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("history_1", "history_2"))
    ids = ["history_1.rate", "history_2.rate"]
    steps = [search_for(o, "שיעור ההיוון שאומץ", ("history_1", "history_2"), ids), registering(o, ids, kind="rate", unit="percent"),
             answer_of("שיעור ההיוון בשומה הקודמת 7% [V1]; בשומה המאוחרת 8% [V2].", "הערמון 3", "rate", "percent")]
    t = ask13(o, "מה שיעור ההיוון בנכס ברחוב הערמון 3?", steps)
    a = t.answer
    kept_whole(o, t)
    assert {v["id"]: v["facet"]["status"] for v in a["values"]} == {"V1": "none", "V2": "none"}
    # (c) one timeline block of the two years, newest first, no mark; the breakdown is empty
    assert a["time"]["breakdown"] == []
    (block,) = a["time"]["timeline"]
    assert [(r["date"], r["value_text"], r["marks"]) for r in block["rows"]] == [
        ("2024-06-30", "8", []), ("2021-06-30", "7", [])]
    assert a["time"]["requested"] is None and "conflict" not in json.dumps(a["time"], ensure_ascii=False)
    refreshed_equals_live(o, t, live)


PARTS = ["parts.caption", "parts.column_location", "parts.column_shape", "parts.sentence", "parts.heading"]


def test_every_part_of_the_source_gives_its_facet_a_caption_two_columns_a_sentence_and_a_heading(db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("facet_parts",))
    cells = [i for i in PARTS if "table" in DATUM[i]["where"]]
    quotes = [i for i in PARTS if i not in cells]
    steps = [search_for(o, "מקדם שיעור היוון", ("facet_parts",), PARTS),
             lambda items: [take(o, i, items, **({"kind": "rate", "unit": "percent"} if i in quotes else {})) for i in PARTS],
             answer_of("; ".join(f"{sentence_of(i)} [V{n}]" for n, i in enumerate(PARTS, 1)) + ".", "הסביון 9")]
    t = ask13(o, "מהם המקדמים והשיעורים בנכס ברחוב הסביון 9?", steps)
    a = t.answer
    kept_whole(o, t)
    assert facets_of(a) == {f"V{n}": {k: DATUM[i]["facet"][k] for k in FACET_KEYS} for n, i in enumerate(PARTS, 1)}
    assert {v["facet"]["part"] for v in a["values"]} == {"caption", "column", "sentence", "heading"}
    # the parallel components of the one unit and date are side by side; none conflicts
    assert "conflict" not in json.dumps(a["time"], ensure_ascii=False)
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# (5) a derived reduction and the date the server holds (AE5, AE6, R12, R13, R16)
# =======================================================================================================================

REDUCED = "- מקדם הפחתה בשל זכויות בנייה חלקיות הוא 0.8 — הפחתה של {pct} [V1]"
SHARING = "- מקדם הפחתה בשל שיתוף במקרקעין הוא 0.9 [V2]"


def whole_lines(markdown: str) -> None:
    """No orphan reference and no line made only of markers: every cited id (V#, S#, C#) is in the answer's payload, no line is just
    bullets, citations and punctuation, and no clause stops at a dangling dash or "של"."""
    for line in markdown.splitlines():
        body = re.sub(r"^[\s>*\-•]+", "", line).strip()
        if not body:
            continue
        assert re.sub(r"\[[A-Z]\d+\]|[\s.,;:—–\-()]|חלק שלא אומת הושמט", "", body), f"a line of markers only: {line!r}"
        assert not re.search(r"(?:^|\s)(?:של|—|–|-)\s*(?:\[[A-Z]\d+\])*\s*$", body), f"a dangling clause: {line!r}"


def references_resolve(a: dict) -> None:
    known = {x["id"] for k in ("sources", "values", "computations", "measurements", "assumptions") for x in a.get(k) or []}
    cited = set(re.findall(r"\[([SVCMA]\d+)\]", a["markdown"]))
    assert cited <= known, f"a reference with nothing behind it: {sorted(cited - known)}"


def reduction_turn(db, client, monkeypatch, calls, pct: str):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    ans = answer_of("\n".join([REDUCED.format(pct=pct), SHARING]), "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), ans, ans, ans]
    return o, ask13(o, LAND_Q, steps)


def test_ae5_a_reduction_that_is_one_minus_the_coefficient_is_kept_as_a_derived_computation_of_the_cited_cell(
        db, client, monkeypatch, calls, live):
    o, t = reduction_turn(db, client, monkeypatch, calls, "20%")
    a = t.answer
    # (a) the verification report: nothing removed or edited; the server cited the derivation it registered
    v = a["verification"]
    assert v["removed"] == 0 and v["edited"] == 0 and v["correctness"] == "verified" and v["actions"]["cited"] == 1, v
    assert [(d["action"], d["kind"]) for d in diagnostics_removed(o, t)] == [("cited", "needs_citation")], "a citation added, no claim lost"
    assert a["markdown"] == "\n".join([REDUCED.format(pct="20%").replace("[V1]", "[V1][C1]"), SHARING])
    # the derived C#: 1 − V1, shown as 20%, an input that is the cited coefficient, labelled as the server's own derivation
    (c,) = a["computations"]
    assert c["id"] == "C1" and c["result_kind"] == "derived" and c["expression"] == "1 − V1" and c["value"] == "0.2"
    assert c["display"]["percent"] == "20%" and [i["id"] for i in c["inputs"]] == ["V1"] and c["note"]
    whole_lines(a["markdown"])
    references_resolve(a)
    # (c) the derived result is no datum: the timeline holds nothing and the components table holds the two coefficients only
    assert a["time"]["timeline"] == [] and sorted(rows_in(a["time"]["breakdown"])) == ["V1", "V2"]
    assert marks_of(a["time"]["breakdown"]) == {"V1": [], "V2": []}
    # (d) after a refresh the computation and the citation are the live ones
    stored = refreshed_equals_live(o, t, live)
    assert [x["result_kind"] for x in stored["computations"]] == ["derived"] and stored["computations"] == live[-1]["computations"]


def test_ae5_a_reduction_of_25_percent_is_cut_alone_the_coefficient_and_its_bullet_stay_with_no_dangling_word(
        db, client, monkeypatch, calls, live):
    o, t = reduction_turn(db, client, monkeypatch, calls, "25%")
    a = t.answer
    lines = a["markdown"].splitlines()
    # (a) the verification report: an edit of the derived clause (counted as edited, not removed), nothing else
    v = a["verification"]
    assert v["removed"] == 0 and v["edited"] == 1 and v["actions"]["qualified"] == 1 and v["removals"] == [], v
    assert not any(v["actions"][k] for k in ("removed", "cut", "rewritten", "withheld"))
    (decision,) = diagnostics_removed(o, t)
    assert (decision["action"], decision["check"]) == ("qualified", "unstated_number") and "25" in decision["reason"]
    # the bullet keeps its coefficient and citation, loses only the clause; the other bullet is untouched
    assert lines[0] == "- מקדם הפחתה בשל זכויות בנייה חלקיות הוא 0.8 [V1]" and lines[1] == SHARING
    assert "25" not in lines[0] and "הפחתה של" not in a["markdown"].split("\n\n")[0]
    assert a["computations"] == [], "nothing derived was registered for a number that does not follow"
    whole_lines(a["markdown"])
    references_resolve(a)
    # (c) the coefficient is still cited, so it is still a component
    assert sorted(rows_in(a["time"]["breakdown"])) == ["V1", "V2"]
    refreshed_equals_live(o, t, live)


def date_judge(seen: list[str]):
    """The judge of the AE6 trace: a sentence that writes the appraisal's date (01.03.2023) is supported only when a source it cites
    states that date (as ``01/03/2023``) in the record the judge is shown: a passage's header carries it as the server's own
    fact (``SERVER_HELD``) though the passage does not hold the line."""
    def judge(input: str) -> dict:
        seen.append(input)
        out = []
        for index, cites, text in re.findall(r'<unit index="(\d+)" cites="([^"]*)">\n(.*?)\n</unit>', input, re.S):
            held = [m for sid in cites.split(",") if sid for m in re.findall(rf'(<source id="{sid}"[^>]*>.*?</source>)', input, re.S)]
            holds = any("01/03/2023" in m for m in held)
            dated = re.search(r"\d{2}[./]\d{2}[./]\d{4}", text) is not None
            verdict = {"verdict": "supported"} if holds or not dated else {"verdict": "unsupported", "failure": "absent_from_source"}
            out.append({"index": int(index), "reason": "בדיקה", "supported_by": []} | verdict)
        return {"verdicts": out}

    return judge


def test_ae6_the_valuation_date_the_server_holds_for_the_cited_unit_is_supported_and_not_cut(db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    seen: list[str] = []
    body = "המועד הקובע של השומה הוא 01.03.2023 [S1].\n- מקדם הפחתה בשל זכויות בנייה חלקיות הוא 0.8 למועד הקובע 01.03.2023 [V1]"
    ans = answer_of(body, "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND[:1]), registering(o, LAND[:1]), ans, ans, ans]
    t = ask13(o, LAND_Q, steps, judge=date_judge(seen))
    a = t.answer
    # (a) the verification report: the date is kept in both sentences
    kept_whole(o, t)
    assert a["markdown"] == body
    # what the judge was shown: the passage's header holds the unit's valuation date as the server's fact; the value's record its
    # own date (the unit's), and the number check passed on the same dates
    shown = "\n".join(seen)
    assert f"{SERVER_HELD}: למועד הקובע 01/03/2023" in shown and "מועד הנתון: 01/03/2023" in shown
    # (b) the coverage record dates the unit by the same stated date
    cov = a["time"]["coverage"]
    assert [(u["date"], u["date_status"]) for u in cov["units"]] == [("2023-03-01", "stated")]
    assert a["values"][0]["as_of"]["date"] == "2023-03-01" and a["values"][0]["as_of"]["provenance"] == "unit_valuation"
    refreshed_equals_live(o, t, live)


def test_ae6_a_date_the_server_does_not_hold_for_the_unit_is_not_supported(db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    body = "המועד הקובע של השומה הוא 01.04.2023 [S1]."
    ans = answer_of(body, "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND[:1]), registering(o, LAND[:1]), ans, ans, ans]
    t = ask13(o, LAND_Q, steps, judge=date_judge([]))
    assert t.answer["verification"]["removed"] == 1 and "01.04.2023" not in t.answer["markdown"]
    references_resolve(t.answer)


# =======================================================================================================================
# (6) a forced removal leaves no orphan reference and no marker-only line (AE7, R15, R16)
# =======================================================================================================================

LEAD = "מקדמי ההפחתה בחלקה 31:"
BULLETS = ["- מקדם הפחתה בשל זכויות בנייה חלקיות הוא 0.8 [V1]", "- מקדם הפחתה בשל שיתוף במקרקעין הוא 0.9 [V2]"]
BACK = "שני המקדמים לעיל חלים על כל החלקה [S1]."
LIST_ANSWER = "\n".join([LEAD, *BULLETS, BACK])


def coefficient_judge(removed: bool):
    """The judge of the AE7 trace: the two bullets are unsupported (``removed``) or supported; the sentence that refers back to them,
    shown by the final check with the units removed before it, names them in ``depends_on``."""
    def judge(input: str) -> dict:
        out = []
        for index, text in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
            if "לעיל" in text:
                shown = re.search(r'<preceding unit="\d+" removed_before="([\d,]+)"', input)
                verdict = {"verdict": "supported"} | ({"depends_on": [int(n) for n in shown.group(1).split(",")]} if shown else {})
            elif "מקדם הפחתה" in text and removed:
                verdict = {"verdict": "unsupported", "failure": "absent_from_source"}
            else:
                verdict = {"verdict": "supported"}
            out.append({"index": int(index), "reason": "בדיקה", "supported_by": []} | verdict)
        return {"verdicts": out}

    return judge


def list_turn(db, client, monkeypatch, calls, removed: bool):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    ans = answer_of(LIST_ANSWER, "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), ans, ans, ans]
    return o, ask13(o, LAND_Q, steps, judge=coefficient_judge(removed))


def test_ae7_when_both_bullets_are_forced_out_the_lead_in_and_the_back_reference_go_with_them_and_nothing_is_left_dangling(
        db, client, monkeypatch, calls, live):
    o, t = list_turn(db, client, monkeypatch, calls, removed=True)
    a = t.answer
    # (a) the verification report: four claims removed, the lead-in and the back-reference as dependents
    v = a["verification"]
    assert v["removed"] == 4 and v["actions"]["removed"] == 4 and v["correctness"] == "unverified", v
    decisions = diagnostics_removed(o, t)
    assert [(d["action"], d["check"]) for d in decisions] == [
        ("removed", "dependency"), ("removed", "judge"), ("removed", "judge"), ("removed", "dependency")]
    assert [d["text"] for d in decisions] == [LEAD, *BULLETS, BACK]
    # no orphan: no lead-in, no "above", no coefficient, no citation of a value that is gone; no line of markers only
    assert LEAD not in a["markdown"] and "לעיל" not in a["markdown"] and "0.8" not in a["markdown"] and "0.9" not in a["markdown"]
    references_resolve(a)
    whole_lines(a["markdown"])
    assert a["markdown"].strip() and a["status"] == "partial"
    # (c) nothing cited is left, so no component and no timeline row
    assert a["time"]["timeline"] == [] and a["time"]["breakdown"] == []
    # (b) the coverage record still says what was checked: the appraisal was only located, none of its data is cited
    cov = a["time"]["coverage"]
    assert [(u["document_id"], u["status"]) for u in cov["units"]] == [(o.ids["land_table"], "located")]
    # the final check judged the sentence that refers back once more, as saved
    assert "verify_final" in [u["purpose"] for u in t.message["usage"]]
    # (d) after a refresh
    stored = refreshed_equals_live(o, t, live)
    assert stored["verification"]["removed"] == 4


def test_ae7_when_the_bullets_stay_the_lead_in_and_the_back_reference_stay_and_nothing_is_judged_again(
        db, client, monkeypatch, calls, live):
    o, t = list_turn(db, client, monkeypatch, calls, removed=False)
    a = t.answer
    kept_whole(o, t)
    assert a["markdown"] == LIST_ANSWER
    assert "verify_final" not in [u["purpose"] for u in t.message["usage"]]
    references_resolve(a)
    assert sorted(rows_in(a["time"]["breakdown"])) == ["V1", "V2"]
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# (6b) the appraiser's office address is no part of the subject's identity (AE8, R11)
# =======================================================================================================================

@pytest.mark.parametrize("key", ["appraiser_office", "appraiser_office_table"])
def test_ae8_the_declaration_block_with_the_appraisers_office_in_another_city_leaves_one_address_in_the_stored_identity(
        db, client, monkeypatch, calls, key):
    """The fixtures are read by the worker, so the identity under test is the one stored for the file: the subject's address
    and city alone, no city conflict, and a group label that names one address."""
    from app.chat import contexts
    from app.chat import property as P
    from app.db import tenant_tx

    o = world13(db, client, monkeypatch, calls, ("appraiser_office", "appraiser_office_table"))
    want = DOCS13[key]["identity"]
    case = next(c for c in MANIFEST13["cases"] if c["id"] == "AE8")["expected"]
    with tenant_tx(o.ctx()) as conn:
        graph = P.build_graph(conn)
    (node,) = [n for n in graph.nodes if str(n.document_id) == o.ids[key]]
    (core,) = contexts.identifiers(f"רחוב {want['street']} {want['number']}").addresses
    assert node.identity.addresses == {core}
    assert node.identity.cities == {want["city"]} and not node.identity.city_conflict
    assert P.label_of([node]) == f"{core[0]} {core[1]}, {want['city']}" and P.label_of([node]).count(",") == 1
    office = case["no_facts_from"]
    assert not {c for c in node.identity.cities if c == office["city"]}
    assert all(office["street"] not in " ".join(a) for a in node.identity.addresses)


# =======================================================================================================================
# (7) undetermined appraisals of an office: candidates and unlinkable units (AE9, R18)
# =======================================================================================================================

def ae9_world(db, client, monkeypatch, calls) -> tuple:
    """The synthetic office of AE9 (``manifest["office_ae9"]``), built as ``test_round12_inventory.ae9_office`` builds it with one
    change: the focus appraisal is a file of two units of the property (the focus year and an earlier one), since the server
    reads the coverage of a property only when it holds two or more appraisals (a file of one unit is a search window)."""
    o = bare_world(db, client, monkeypatch, calls, "admin-r13s9@example.test")
    docs = {}
    for spec in MANIFEST13["office_ae9"]["units"]:
        key = {"focus": "focus", "other_property": "other"}.get(spec["role"], spec.get("n"))
        title = spec["title"] or f"שומה בדויה מספר {key}"
        units = [{k: spec[k] for k in UNIT_KEYS}]
        if spec["role"] == "focus":
            units.append(dict(units[0], blocks=[b.replace("31/05/2024", "31/05/2022") for b in spec["blocks"]]))
        docs[key], _ = unit_file(o, o.default_group_id, title, units, sha=hashlib.sha256(f"ae9-{key}".encode()).hexdigest())
    return o, docs


def test_ae9_two_of_twenty_unidentified_units_are_named_candidates_eighteen_are_only_counted_and_only_the_two_block(
        db, client, monkeypatch, calls, live):
    o, docs = ae9_world(db, client, monkeypatch, calls)
    expected = MANIFEST13["office_ae9"]["expected"]
    steps = [[call("search", query="שטח הנכס", document_ids=[docs["focus"]], limit=None)],
             answer_of("שטח הנכס הוא 90 מ״ר [S1].", "הדגנית 22", "area", "sqm", [docs["focus"]])]
    t = ask13(o, "מה שטח הנכס?", steps)
    a = t.answer
    # (a) the verification report: the answer itself is whole
    kept_whole(o, t)
    assert a["status"] == "answered"
    # (b) the coverage record: the property's two appraisals, the two candidates named, the eighteen counted, one other property
    cov = a["time"]["coverage"]
    assert cov["scope"] == "property" and cov["identified"] and cov["via"] == "documents" and len(cov["units"]) == 2
    assert [u["document_id"] for u in cov["units"]] == [docs["focus"]] * 2
    candidates = cov["candidates"]
    assert candidates["count"] == expected["candidates"] == 2
    assert [c["document_id"] for c in candidates["named"]] == [docs[n] for n in expected["named_candidates"]]
    assert {c["document_id"]: c["elsewhere"] for c in candidates["items"]} == {docs[7]: False, docs[15]: True}
    assert cov["unlinkable"] == expected["unlinkable"] == 18 and cov["excluded"] == expected["other_properties"] == 1
    assert cov["possible"]["count"] == 0
    assert not cov["complete"] and cov["blocking"] == [TF.B_CANDIDATES], "only the two candidates block a complete record"
    unnamed = {docs[n] for n in range(1, 21) if n not in expected["named_candidates"]}
    assert unnamed.isdisjoint(json.dumps(cov, ensure_ascii=False)), "an unlinkable unit is counted, never named"
    assert docs["other"] not in json.dumps(cov), "nor is the other property"
    note = cov["note"]
    assert f"{TF.MSG_GROUP_UNLINKABLE}: 18." in note.splitlines() and TF.MSG_GROUP_CANDIDATES in note
    assert "שומה בדויה מספר 7" in note and "שומה בדויה מספר 15" in note and "הנחל" not in note
    assert a["time"]["text"].endswith(note)
    # (c) the answer cites no datum: no timeline block and no components table
    assert a["time"]["timeline"] == [] and a["time"]["breakdown"] == []
    # (d) after a refresh the record and its note are the live ones
    stored = refreshed_equals_live(o, t, live)
    assert stored["time"]["coverage"]["candidates"]["count"] == 2 and stored["time"]["coverage"]["unlinkable"] == 18
