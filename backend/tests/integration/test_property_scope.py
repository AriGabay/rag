"""A time question about the property in focus reads every appraisal of that property (round 11 U14; R4, R9, R11, R12,
R16; AE1, AE3; KTD6, KTD9) — and, since round 12 U6 (R1-R4; AE1, AE2; KTD4, KTD5), only appraisals linked by proof.

The conversation's focus is one unit (one document); the property is its gathered set in the turn's property graph
(``app.chat.property``): units linked by a shared block and parcel, or by the same street, number and city, each link with
its reason. In a ``recent`` or ``explicit`` turn, a ``search`` that would stay inside the focus unit reads the units of
that set instead, ordered by the turn's time mode, and says so. Mode ``none`` keeps the unit search. The fixtures are
synthetic: the round-11 ones are one invented property appraised at three valuation dates (2022, 2024, 2025) and once with
none, and a neighbouring property on the same street; the round-12 ones add two synthetic cities with the same street and
number, a property over two parcels, sub-parcels and two compendia of six appraisal units (built with
``tests.support.unit_files``, since a DOCX is one unit). Every name, address and amount is invented."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app import worker
from app.chat import property as P
from app.chat import timeline
from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_tools_pools import run, sources_of, workspace
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round11" / "manifest.json").read_text(encoding="utf-8"))["documents"]
MANIFEST12 = json.loads((FIXTURES / "round12" / "manifest.json").read_text(encoding="utf-8"))
DOCS12 = MANIFEST12["documents"]
ANCHOR = date(2026, 10, 14)
RECENT = TimeMode("recent", "switch", ANCHOR, None, True)
NONE = TimeMode("none", "switch", ANCHOR, None, False)
EXPLICIT_2022 = TimeMode("explicit", "question", ANCHOR, Requested("point", "2022", "2022", "year", "ב-2022"), True)
QUERY = "שווי הנכס ברחוב הצאלון 7"
KEYS = ("hatzalon7_a", "hatzalon7_b", "hatzalon7_c", "hatzalon7_undated", "hatzalon11")  # 2022, 2024, 2025, none, other


def upload(office, path: Path, kind: str | None, monkeypatch) -> str:
    """A fixture uploaded and read by the worker without OCR, vision or the measurement pass: text only."""
    from app.extraction import ocr

    with monkeypatch.context() as m:
        m.setattr(ocr, "ocr_available", lambda languages: False)
        m.setattr(pipeline, "vision_reader", lambda ctx: None)
        m.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
        data = {"group_id": str(office.default_group_id)} | ({"source_kind": kind} if kind else {})
        r = office.client.post("/api/documents", files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))],
                               data=data)
        assert r.status_code == 200, r.text
        while worker.run_one("property-scope"):
            pass
    return r.json()["results"][0]["document_id"]


@pytest.fixture
def office(db, client, monkeypatch):
    o = make_office(db, "משרד סינתטי", "admin-ps@example.test")
    login(client, "admin-ps@example.test")
    o.client = client
    o.ids = {k: upload(o, FIXTURES / MANIFEST[k]["file"], MANIFEST[k]["source_kind"], monkeypatch) for k in KEYS}
    return o


def version_of(office, doc: str) -> tuple[str, str | None]:
    with tenant_tx(office.ctx()) as conn:
        r = conn.execute(text("SELECT id, ingestion->>'reading_id' AS reading FROM document_versions WHERE document_id = :d"
                              " AND is_current"), {"d": doc}).one()
    return str(r.id), r.reading


def focused(office, time: TimeMode, doc: str, unit_no: int = 1) -> T.Workspace:
    """A workspace whose conversation is on unit ``unit_no`` of ``doc`` (as a prior turn leaves it), a call that names no unit
    working inside it: the server's pin (``unit_default``, round 15 U6) — the call a model turn makes with ``unit=U#`` takes
    the same path (``unfocused`` and the tests of a named unit)."""
    ws = workspace(office, "property")
    ws.time = time
    ver, reading = version_of(office, doc)
    handle = T.unit_handle_of(ws, doc, ver, reading, unit_no)
    ws.unit_focus = {"handle": handle, "document_id": doc, "unit_id": None}
    ws.unit_default = True
    return ws


def order(out: str) -> list[str]:
    seen: list[str] = []
    for s in sources_of(out):
        if s["document_id"] not in seen:
            seen.append(s["document_id"])
    return seen


def graph_of(office) -> P.PropertyGraph:
    with tenant_tx(office.ctx()) as conn:
        return P.build_graph(conn)


def test_the_identities_of_the_fixtures_gather_one_property_and_the_neighbour_is_another(office):
    """Round 11's fixtures state their city, block and parcel: the four appraisals of the property link by their parcel
    (reason "parcel 7101/15") and the neighbour at number 11 (parcel 7101/16) is another property."""
    graph = graph_of(office)
    node = {k: graph.find(version_of(office, office.ids[k])[0], 1) for k in KEYS}
    assert all(node.values()) and all(n.identity.identified for n in node.values())
    focus = graph.gather(node["hatzalon7_c"].key)
    assert {str(m.document_id) for m in focus.members} == {office.ids[k] for k in KEYS[:4]}
    assert set(focus.reasons().values()) == {"parcel 7101/15"}
    assert graph.link(node["hatzalon7_a"].key, node["hatzalon11"].key).relation == P.OTHER
    assert not focus.candidates


# --- the search -------------------------------------------------------------------------------------------------------

def test_ae3_an_explicit_2022_question_reads_the_2022_appraisal_though_the_conversation_is_on_the_2025_one(office):
    ids = office.ids
    out = run(focused(office, EXPLICIT_2022, ids["hatzalon7_c"]), "search", query=QUERY, limit=6)
    got = order(out)
    assert got[0] == ids["hatzalon7_a"], "the 2022 appraisal answers first"
    assert set(got) == {ids[k] for k in KEYS[:4]}, "every appraisal of the property, and none of the neighbour's"
    assert T.MSG_PROPERTY_SEARCHED in out


def test_ae1_the_switch_reads_all_appraisals_of_the_property_the_newest_first(office):
    ids = office.ids
    got = order(run(focused(office, RECENT, ids["hatzalon7_a"]), "search", query=QUERY, limit=6))
    assert got == [ids["hatzalon7_c"], ids["hatzalon7_b"], ids["hatzalon7_a"], ids["hatzalon7_undated"]]


def test_without_a_time_mode_the_search_stays_in_the_focus_unit(office):
    ids = office.ids
    ws = focused(office, NONE, ids["hatzalon7_c"])
    out = run(ws, "search", query=QUERY, limit=6)
    assert set(order(out)) == {ids["hatzalon7_c"]} and T.MSG_PROPERTY_SEARCHED not in out
    assert T.MSG_UNIT_FOCUS.format(unit=ws.unit_focus["handle"]) in out


def test_a_different_property_on_the_same_street_is_never_included_and_a_lone_unit_keeps_the_unit_search(office):
    ids = office.ids
    for time in (RECENT, EXPLICIT_2022):
        out = run(focused(office, time, ids["hatzalon11"]), "search", query="שווי הנכס ברחוב הצאלון 11", limit=6)
        assert set(order(out)) == {ids["hatzalon11"]} and T.MSG_PROPERTY_SEARCHED not in out
        assert ids["hatzalon11"] not in order(run(focused(office, time, ids["hatzalon7_b"]), "search", query=QUERY, limit=6))


def test_a_call_naming_another_document_stays_as_asked_and_one_naming_another_unit_follows_that_unit(office):
    """Naming another property's document stays as asked (U16; a document of the same property with no unit is widened, see
    the tests below). Round 15 U2 (R8, KTD10): the time widening follows the unit a call names, whether or not the
    conversation kept it — a different unit of the same property reads the property from that unit's seat."""
    ids = office.ids
    ws = focused(office, RECENT, ids["hatzalon7_c"])
    out = run(ws, "search", query=QUERY, limit=6, document_ids=[ids["hatzalon11"]])  # another property's document
    assert set(order(out)) == {ids["hatzalon11"]} and T.MSG_PROPERTY_SEARCHED not in out
    for time in (RECENT, EXPLICIT_2022):  # a fresh workspace each: a source already returned in a turn is not repeated
        ws = focused(office, time, ids["hatzalon7_c"])
        ver, reading = version_of(office, ids["hatzalon7_b"])
        other = T.unit_handle_of(ws, ids["hatzalon7_b"], ver, reading, 1)
        assert other != ws.unit_focus["handle"]
        out = run(ws, "search", query=QUERY, limit=6, unit=other)
        assert T.MSG_PROPERTY_SEARCHED in out and set(order(out)) == {ids[k] for k in KEYS[:4]}, (time.mode, out[:300])
        assert ids["hatzalon11"] not in order(out)  # the neighbour at number 11 is another property


def test_a_call_naming_the_focus_unit_itself_is_the_focus_widened_in_a_time_mode_and_unchanged_without_one(office):
    """The model sees the focus unit's handle in the context and names it (round 11 U16): that call is the conversation's
    unit, so it reads the property exactly as a call that names no unit does; mode none keeps the unit search."""
    ids = office.ids
    for time, first in ((EXPLICIT_2022, ids["hatzalon7_a"]), (RECENT, ids["hatzalon7_c"])):
        ws = focused(office, time, ids["hatzalon7_c"])
        named = run(ws, "search", query=QUERY, limit=6, unit=ws.unit_focus["handle"])
        unnamed = run(focused(office, time, ids["hatzalon7_c"]), "search", query=QUERY, limit=6)
        assert T.MSG_PROPERTY_SEARCHED in named and order(named)[0] == first, time.mode
        assert order(named) == order(unnamed) and set(order(named)) == {ids[k] for k in KEYS[:4]}, time.mode
        assert T.MSG_UNIT_FOCUS.format(unit=ws.unit_focus["handle"]) in named
    ws = focused(office, NONE, ids["hatzalon7_c"])
    out = run(ws, "search", query=QUERY, limit=6, unit=ws.unit_focus["handle"])
    assert set(order(out)) == {ids["hatzalon7_c"]} and T.MSG_PROPERTY_SEARCHED not in out


# --- a call that names documents of one property and no unit (round 11 U16) -----------------------------------------------

def unfocused(office, time: TimeMode) -> T.Workspace:
    """A workspace on no unit: the model passes ``document_ids`` taken from the answer before."""
    ws = workspace(office, "property")
    ws.time = time
    return ws


@pytest.fixture
def rent(office, monkeypatch):
    """The office with a rent opinion of the same property (a current appraisal-pool document, same key)."""
    m = MANIFEST["rent_monthly_with_vat"]
    return upload(office, FIXTURES / m["file"], m["source_kind"], monkeypatch)


def test_explicit_2022_naming_only_the_2025_appraisal_and_no_unit_reads_the_whole_property_2022_first(office):
    ids = office.ids
    out = run(unfocused(office, EXPLICIT_2022), "search", query=QUERY, limit=6, document_ids=[ids["hatzalon7_c"]])
    got = order(out)
    assert got[0] == ids["hatzalon7_a"], "the 2022 appraisal answers first"
    assert set(got) == {ids[k] for k in KEYS[:4]} and ids["hatzalon11"] not in got
    assert T.MSG_PROPERTY_SEARCHED in out


def test_recent_naming_one_rent_opinion_of_the_property_reads_all_its_appraisals(office, rent):
    ids = office.ids
    out = run(unfocused(office, RECENT), "search", query=QUERY, limit=8, document_ids=[rent])
    got = order(out)
    assert {ids[k] for k in KEYS[:4]} <= set(got) and ids["hatzalon11"] not in got
    assert got[0] == ids["hatzalon7_c"] and T.MSG_PROPERTY_SEARCHED in out


def test_naming_documents_of_two_properties_is_left_as_it_is(office):
    ids = office.ids
    for time in (RECENT, EXPLICIT_2022):
        out = run(unfocused(office, time), "search", query=QUERY, limit=6,
                  document_ids=[ids["hatzalon7_c"], ids["hatzalon11"]])
        assert set(order(out)) <= {ids["hatzalon7_c"], ids["hatzalon11"]} and T.MSG_PROPERTY_SEARCHED not in out


def test_mode_none_naming_a_document_reads_that_document_only(office):
    ids = office.ids
    out = run(unfocused(office, NONE), "search", query=QUERY, limit=6, document_ids=[ids["hatzalon7_c"]])
    assert set(order(out)) == {ids["hatzalon7_c"]} and T.MSG_PROPERTY_SEARCHED not in out


def test_a_named_unit_is_widened_in_a_time_mode_whatever_its_own_documents_and_refuses_another_file(office):
    """Round 15 U2: the call names a unit and the turn has a time mode, so the property is read; the documents the call
    comes with must be the unit's own file (the unit fixes the document)."""
    ids = office.ids
    ws = unfocused(office, EXPLICIT_2022)
    ver, reading = version_of(office, ids["hatzalon7_c"])
    own = T.unit_handle_of(ws, ids["hatzalon7_c"], ver, reading, 1)
    out = run(ws, "search", query=QUERY, limit=6, unit=own, document_ids=[ids["hatzalon7_c"]])
    assert T.MSG_PROPERTY_SEARCHED in out and set(order(out)) == {ids[k] for k in KEYS[:4]}
    refused = run(ws, "search", query=QUERY, limit=6, unit=own, document_ids=[ids["hatzalon11"]])
    assert T.MSG_UNIT_ARGS in refused


# --- units without a shared key; a file of several properties ---------------------------------------------------------

def plain(office, key: str, valuation: str, identifiers: list[str], blocks_after: list[str] | None = None,
          place: str = "הצאלון 7", identification: list[str] | None = None) -> str:
    """A one-unit appraisal whose stored title identifiers are ``identifiers`` and whose body states ``identification``
    (labelled lines, read by the identity reader) and a value sentence about ``place``."""
    body = ["מסמך סינתטי — נתונים בדויים", *(identification or []), f"המועד הקובע: {valuation}",
            f"שווי הנכס ברחוב {place} הוא 2,450,000 ₪ (סינתטי).", *(blocks_after or [])]
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}",
                       [{"title": f"מסמך {key}", "blocks": body, "identifiers": identifiers}],
                       sha=hashlib.sha256(key.encode()).hexdigest())
    return doc


SEVEN = ["רחוב הצאלון 7", "גוש 7101 חלקה 15"]


@pytest.fixture
def bare(db):
    return make_office(db, "משרד ב סינתטי", "admin-ps2@example.test")


def test_a_unit_without_identifiers_matches_no_other_unit(bare):
    a = plain(bare, "no-ids-a", "01/01/2022", [])
    b = plain(bare, "no-ids-b", "01/01/2025", [])
    c = plain(bare, "with-ids", "01/01/2024", SEVEN)
    for focus in (a, b):
        out = run(focused(bare, RECENT, focus), "search", query=QUERY, limit=6)
        assert set(order(out)) == {focus} and T.MSG_PROPERTY_SEARCHED not in out
    assert c not in order(run(focused(bare, RECENT, a), "search", query=QUERY, limit=6))


CITY_X, CITY_Y = "עיר: עיר-סינתטית", "עיר: עיר-סינתטית-ב"


def at(city: str | None, address: str = "הצאלון 7") -> list[str]:
    return [*([city] if city else []), f"כתובת הנכס: {address}"]


def test_the_same_street_number_and_city_link_by_address_city_with_its_reason(bare):
    """Round 12 R3: with no block and parcel, the same street, number AND city prove the link."""
    a = plain(bare, "addr-a", "01/01/2022", ["רחוב הצאלון 7"], identification=at(CITY_X))
    b = plain(bare, "addr-b", "01/01/2025", ["רחוב הצאלון 7"], identification=at(CITY_X))
    plain(bare, "addr-other", "01/01/2024", ["רחוב הצאלון 11"], place="הצאלון 11", identification=at(CITY_X, "הצאלון 11"))
    plain(bare, "bp-only", "01/01/2023", ["גוש 7101 חלקה 15"], place="הצאלון 3")
    got = order(run(focused(bare, RECENT, a), "search", query=QUERY, limit=6))
    assert got == [b, a]
    with tenant_tx(bare.ctx()) as conn:
        graph = P.build_graph(conn)
        focus = graph.gather(next(n.key for n in graph.nodes if str(n.document_id) == a))
    assert [(str(j.node.document_id), j.link.relation, j.link.reason) for j in focus.joined] == [(b, P.ADDRESS_CITY, "address_city")]


def test_ae1_the_same_street_and_number_in_two_cities_never_link_and_a_cityless_one_is_a_named_candidate(bare):
    x = plain(bare, "city-x", "01/01/2022", ["רחוב הצאלון 7"], identification=at(CITY_X))
    y = plain(bare, "city-y", "01/01/2025", ["רחוב הצאלון 7"], identification=at(CITY_Y))
    none = plain(bare, "city-none", "01/01/2024", ["רחוב הצאלון 7"], identification=at(None))
    for time in (RECENT, EXPLICIT_2022):
        out = run(focused(bare, time, x), "search", query=QUERY, limit=6)
        assert set(order(out)) == {x} and T.MSG_PROPERTY_SEARCHED not in out, time.mode
    with tenant_tx(bare.ctx()) as conn:
        graph = P.build_graph(conn)
        focus = graph.gather(next(n.key for n in graph.nodes if str(n.document_id) == x))
    assert not focus.joined
    assert [(str(c.node.document_id), c.link.reason) for c in focus.candidates] == [(none, P.REASON_CITY_MISSING)]
    assert y not in {str(c.node.document_id) for c in focus.candidates}, "a stated other city proves another property"


def test_a_unit_whose_only_identity_is_its_stored_title_identifiers_still_has_one(bare):
    """The stored identifiers are a fallback source of the same facts (a parcel and an address, no city): two such units
    with the same parcel link, with the same address only they are candidates (no city)."""
    a = plain(bare, "stored-a", "01/01/2022", ["רחוב הצאלון 7", "גוש 7101 חלקה 15"], place="נתן 3")
    b = plain(bare, "stored-b", "01/01/2025", ["גוש 7101 חלקה 15"], place="נתן 3")
    got = order(run(focused(bare, RECENT, a), "search", query=QUERY, limit=6))
    assert got == [b, a]


def test_the_other_property_of_a_multi_unit_file_is_not_pulled_in(bare):
    other = "פרויקט האחר בעיר סינתטית"
    units = [{"title": "שומה הצאלון 7 סינתטית", "blocks": ["מסמך סינתטי — נתונים בדויים", "המועד הקובע: 01/01/2023",
                                                          "שווי הנכס ברחוב הצאלון 7 הוא 2,100,000 ₪ (סינתטי)."],
              "identifiers": SEVEN},
             {"title": other, "blocks": ["המועד הקובע: 01/01/2026", "שווי הנכס ברחוב הרקפות 3 הוא 9,999,000 ₪ (סינתטי)."],
              "identifiers": ["רחוב הרקפות 3", "גוש 8000 חלקה 3"]}]
    multi, _ = unit_file(bare, bare.default_group_id, "קובץ שומות סינתטי", units, sha="m" * 64)
    mine = plain(bare, "mine", "01/01/2025", SEVEN)
    out = run(focused(bare, RECENT, mine), "search", query="שווי הנכס", limit=6)
    assert set(order(out)) == {mine, multi} and T.MSG_PROPERTY_SEARCHED in out
    assert "9,999,000" not in out and "2,100,000" in out, "the file's first unit only"
    assert order(out)[0] == mine


# --- the stored identifiers --------------------------------------------------------------------------------------------

def test_the_stored_title_identifiers_fill_only_the_kind_of_fact_the_reader_found_none_of():
    from app.chat.identity import UnitIdentity

    found = UnitIdentity(1, addresses=frozenset({("צאלון", "7")}))
    merged = P.with_stored(found, {"parcels": [["07101", "015"]], "addresses": [["אחר", "9"]], "shown": []})
    assert merged.parcels == {("7101", "15", None)}, "no leading zeros; the reader found no parcel"
    assert merged.addresses == {("צאלון", "7")}, "the reader's address wins over the stored one"
    older = P.with_stored(UnitIdentity(1), {"shown": ["רחוב הצאלון 7"]})
    assert older.addresses == {("צאלון", "7")} and not older.parcels, "a row with only its shown strings is read from them"
    assert P.with_stored(UnitIdentity(1), None).identified is False


def test_a_named_document_with_no_identity_or_a_file_of_two_properties_leaves_the_call_as_asked(bare):
    a = plain(bare, "named-a", "01/01/2022", SEVEN)
    b = plain(bare, "named-b", "01/01/2025", SEVEN)
    nokey = plain(bare, "named-nokey", "01/01/2024", [])
    units = [{"title": "שומה הצאלון 7 סינתטית", "blocks": ["המועד הקובע: 01/01/2023", "שווי הנכס ברחוב הצאלון 7 הוא 2,100,000 ₪ (סינתטי)."],
              "identifiers": SEVEN},
             {"title": "פרויקט האחר", "blocks": ["המועד הקובע: 01/01/2026", "שווי הנכס ברחוב הרקפות 3 הוא 9,999,000 ₪ (סינתטי)."],
              "identifiers": ["רחוב הרקפות 3", "גוש 8000 חלקה 3"]}]
    multi, _ = unit_file(bare, bare.default_group_id, "קובץ שומות סינתטי", units, sha="n" * 64)
    for time in (RECENT, EXPLICIT_2022):
        out = run(unfocused(bare, time), "search", query=QUERY, limit=6, document_ids=[a, nokey])
        assert b not in order(out) and T.MSG_PROPERTY_SEARCHED not in out, "a document with no key"
        out = run(unfocused(bare, time), "search", query=QUERY, limit=6, document_ids=[multi])
        assert set(order(out)) <= {multi} and T.MSG_PROPERTY_SEARCHED not in out, "a file of two properties"
    out = run(unfocused(bare, RECENT), "search", query=QUERY, limit=6, document_ids=[a])
    assert set(order(out)) == {a, b, multi} and T.MSG_PROPERTY_SEARCHED in out, "the control: one keyed document is widened"


# --- round 12: the fixtures' relations, AE1, AE2 (U6, KTD4) -------------------------------------------------------------

KEYS12 = ("cities_a", "cities_b", "cities_nocity", "parcels_ab", "parcels_b", "parcels_c", "apartments_3", "apartments_4",
          "apartments_none")


@pytest.fixture
def office12(db, client, monkeypatch):
    o = make_office(db, "משרד סינתטי יב", "admin-ps12@example.test")
    login(client, "admin-ps12@example.test")
    o.client = client
    o.ids = {k: upload(o, FIXTURES / DOCS12[k]["file"], DOCS12[k]["source_kind"], monkeypatch) for k in KEYS12}
    return o


def node_of(office, graph: P.PropertyGraph, key: str, unit_no: int = 1) -> P.Node:
    return graph.find(version_of(office, office.ids[key])[0], unit_no)


@pytest.mark.parametrize("group", ["cities", "parcels", "apartments"])
def test_the_manifest_relations_of_the_uploaded_fixtures_are_reproduced_end_to_end(office12, group):
    """The reader reads the DOCX, the relation relates: every expected relation, reason and gathered flag of the manifest."""
    graph = graph_of(office12)
    for r in MANIFEST12["groups"][group]["relations"]:
        a, b = (node_of(office12, graph, r[side]["document"], r[side]["unit"]) for side in ("a", "b"))
        link = graph.link(a.key, b.key)
        assert (link.relation, link.reason, link.gathered) == (r["relation"], r["reason"], r["gathered"]), r


def test_ae1_two_cities_never_rank_each_other_and_the_cityless_appraisal_is_an_unlinked_candidate(office12):
    ids = office12.ids
    graph = graph_of(office12)
    focus = graph.gather(node_of(office12, graph, "cities_a").key)
    assert [str(m.document_id) for m in focus.members] == [ids["cities_a"]]
    assert [(str(c.node.document_id), c.link.reason) for c in focus.candidates] == [(ids["cities_nocity"], P.REASON_CITY_MISSING)]
    for time in (RECENT, EXPLICIT_2022):
        ws = focused(office12, time, ids["cities_a"])
        out = run(ws, "search", query="שווי הנכס הדולב 7", limit=6)
        assert set(order(out)) == {ids["cities_a"]} and T.MSG_PROPERTY_SEARCHED not in out, time.mode
        record = ws.time_coverage  # round 12 U7: the coverage counts the cityless one as a named unlinked candidate
        assert [u["document_id"] for u in record["units"]] == [ids["cities_a"]] and not record["complete"], time.mode
        assert [(c["document_id"], c["reason"]) for c in record["candidates"]["named"]] == [
            (ids["cities_nocity"], P.REASON_CITY_MISSING)] and record["candidates"]["count"] == 1, time.mode


def test_ae2_the_focus_on_the_parcel_b_gathers_the_two_parcel_property_with_its_reason_and_never_the_separate_lot(office12):
    ids = office12.ids
    graph = graph_of(office12)
    focus = graph.gather(node_of(office12, graph, "parcels_b").key)
    assert {str(m.document_id) for m in focus.members} == {ids["parcels_ab"], ids["parcels_b"]}
    assert set(focus.reasons().values()) == {"parcel 8401/40"}
    ws = focused(office12, RECENT, ids["parcels_b"])
    out = run(ws, "search", query="שווי הנכס הדס", limit=6)
    assert set(order(out)) == {ids["parcels_ab"], ids["parcels_b"]} and T.MSG_PROPERTY_SEARCHED in out
    assert ids["parcels_c"] not in order(out)
    record = ws.time_coverage  # round 12 U7: the inventory is the gathered set, with the link reason; the lot is no candidate
    assert {u["document_id"] for u in record["units"]} == {ids["parcels_ab"], ids["parcels_b"]}
    assert record["property"]["reasons"] == ["parcel 8401/40"] and record["candidates"]["count"] == 0


def test_ae2_two_sub_parcels_of_one_parcel_never_gather_each_other_and_the_unspecified_one_is_a_candidate(office12):
    ids = office12.ids
    graph = graph_of(office12)
    focus = graph.gather(node_of(office12, graph, "apartments_3").key)
    assert [str(m.document_id) for m in focus.members] == [ids["apartments_3"]]
    assert [(str(c.node.document_id), c.link.reason) for c in focus.candidates] == [(ids["apartments_none"], P.REASON_SUB_MISSING)]
    ws = focused(office12, RECENT, ids["apartments_3"])
    out = run(ws, "search", query="שווי הנכס הנרקיס", limit=6)
    assert set(order(out)) == {ids["apartments_3"]} and T.MSG_PROPERTY_SEARCHED not in out, "the unit search stands"
    record = ws.time_coverage
    assert [c["reason"] for c in record["candidates"]["named"]] == [P.REASON_SUB_MISSING] and not record["complete"]


# --- a file of several units; hidden units; the timeline groups -----------------------------------------------------------

def compendium(office, name: str, group=None, sha: str | None = None) -> tuple[str, int]:
    """The manifest's compendium as a stored multi-unit file (a DOCX is one unit): a cover unit with an index table listing
    every address, then each appraisal unit with its labelled identification. Returns (document id, the number of the first
    appraisal unit)."""
    units = DOCS12[name]["units"]
    built = [{"title": "תוכן עניינים", "kind": "front_matter", "blocks": ["תוכן עניינים — רשימת השומות בקובץ",
                                                                        *("|" + u["identity"]["address"] for u in units)]}]
    for u in units:
        i = u["identity"]
        built.append({"title": u["title"], "identifiers": [], "blocks": [
            u["title"], f"עיר: {i['city']}", f"כתובת הנכס: {i['street']} {i['number']}", f"גוש: {i['block']} חלקה: {i['parcels'][0]}",
            f"המועד הקובע: {u['valuation_date_text']}", f"שווי הנכס: {u['value']['text']} ₪ (סינתטי)."]})
    doc, _ = unit_file(office, group or office.default_group_id, DOCS12[name]["title"], built,
                       sha=sha or hashlib.sha256(name.encode()).hexdigest())
    return doc, 2


def test_the_cover_index_is_no_identity_and_a_compendium_of_two_properties_is_two_gathered_sets(bare):
    doc, first = compendium(bare, "compendium_two")
    graph = graph_of(bare)
    assert [n.unit_no for n in graph.nodes] == [2, 3, 4, 5, 6, 7], "the cover is no node"
    expected = MANIFEST12["groups"]["compendium_two"]["relations"]
    for r in expected:
        a, b = (graph.find(version_of(bare, doc)[0], r[side]["unit"] + 1) for side in ("a", "b"))
        link = graph.link(a.key, b.key)
        assert (link.relation, link.reason, link.gathered) == (r["relation"], r["reason"], r["gathered"]), r
    p = graph.gather(graph.find(version_of(bare, doc)[0], first).key)
    assert [m.unit_no for m in p.members] == [2, 4, 6]


def test_named_property_of_a_compendium_of_two_properties_is_none_and_of_one_property_is_all_six_units(bare):
    two, _ = compendium(bare, "compendium_two")
    one, _ = compendium(bare, "compendium_one")
    ws = unfocused(bare, RECENT)
    with tenant_tx(bare.ctx()) as conn:
        assert T._named_property(ws, conn, [UUID(two)]) is None
        found = T._named_property(ws, conn, [UUID(one)])
    assert sorted(n for _, _, n in found) == [2, 3, 4, 5, 6, 7] and {str(d) for d, _, _ in found} == {one}


def test_a_hidden_unit_is_not_a_node_so_a_user_outside_its_group_does_not_gather_it(bare):
    private = make_group(bare, "קבוצה סגורה")
    member = make_user(bare, "member-ps@example.test", [bare.default_group_id, private])
    outsider = make_user(bare, "outsider-ps@example.test", [bare.default_group_id])
    place = ["עיר: עיר-סינתטית", "כתובת הנכס: הצאלון 7"]
    a = plain(bare, "hid-a", "01/01/2022", [], identification=[*place, "גוש: 7101 חלקה: 15"])
    doc, _ = unit_file(bare, private, "hid-b", [{"title": "hid-b", "identifiers": [], "blocks": [
        "מסמך סינתטי — נתונים בדויים", *place, "גוש: 7101 חלקה: 15", "המועד הקובע: 01/01/2025", "שווי הנכס 3,000,000 ₪ (סינתטי)."]}],
        sha="h" * 64)
    for user, expected in ((member, {a, doc}), (outsider, {a})):
        with tenant_tx(bare.ctx("employee", user)) as conn:
            graph = P.build_graph(conn)
            focus = graph.gather(next(n.key for n in graph.nodes if str(n.document_id) == a))
        assert {str(m.document_id) for m in focus.members} == expected


def test_the_timeline_groups_gathered_units_together_and_candidates_apart(bare):
    x1 = plain(bare, "tl-x1", "01/01/2022", [], identification=at(CITY_X))
    x2 = plain(bare, "tl-x2", "01/01/2025", [], identification=at(CITY_X))
    none = plain(bare, "tl-none", "01/01/2024", [], identification=at(None))
    other = plain(bare, "tl-other", "01/01/2023", [], identification=at(CITY_Y))
    ws = focused(bare, RECENT, x1)
    who = timeline.identity_of(ws)

    def key(doc: str):
        ver, _ = version_of(bare, doc)
        return who(timeline.Datum("V1", 0, doc, ver, "t", "", 1, True, "value", "ILS", (), "1", Decimal(1), None))

    assert key(x1).key and key(x1).key == key(x2).key, "two values from gathered units share one group"
    assert len({key(x1).key, key(none).key, key(other).key}) == 3, "candidates and other cities stay in separate groups"
    assert "צאלון 7" in key(x1).label
    ver, _ = version_of(bare, x1)
    assert who(timeline.Datum("V2", 1, x1, ver, "t", "", 1, False, "value", "ILS", (), "1", Decimal(1), None)).key is None, \
        "a datum of an appendix or a comparables part has no group"
