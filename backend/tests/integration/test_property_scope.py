"""A time question about the property in focus reads every appraisal of that property (round 11 U14; R4, R9, R11, R12,
R16; AE1, AE3; KTD6, KTD9).

The conversation's focus is one unit (one document); the property is every appraisal of it. In a ``recent`` or
``explicit`` turn, a ``search`` that would stay inside the focus unit reads the subject units that share its property
key instead, ordered by the turn's time mode, and says so. The key is the normalized block and parcel (else street and
house number) of the unit's stored identifiers (``app.chat.property``). Mode ``none`` keeps the unit search. The round-11
fixtures are synthetic: one invented property appraised at three valuation dates (2022, 2024, 2025) and once with none,
and a neighbouring property on the same street; every name, address and amount is invented."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app import worker
from app.chat import property as P
from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_tools_pools import run, sources_of, workspace
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round11" / "manifest.json").read_text(encoding="utf-8"))["documents"]
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
    """A workspace whose conversation is on unit ``unit_no`` of ``doc`` (as a prior turn leaves it)."""
    ws = workspace(office, "property")
    ws.time = time
    ver, reading = version_of(office, doc)
    handle = T.unit_handle_of(ws, doc, ver, reading, unit_no)
    ws.unit_focus = {"handle": handle, "document_id": doc, "unit_id": None}
    return ws


def order(out: str) -> list[str]:
    seen: list[str] = []
    for s in sources_of(out):
        if s["document_id"] not in seen:
            seen.append(s["document_id"])
    return seen


def test_the_stored_identifiers_of_the_fixtures_give_one_key_for_one_property(office):
    with tenant_tx(office.ctx()) as conn:
        keys = {k: P.property_key(conn.execute(text("SELECT identifiers FROM document_units WHERE document_id = :d"),
                                               {"d": office.ids[k]}).scalar_one()) for k in KEYS}
    assert keys["hatzalon7_a"] and len({keys[k] for k in KEYS[:4]}) == 1, keys
    assert keys["hatzalon11"] not in (None, keys["hatzalon7_a"]), keys


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


def test_a_call_that_names_a_unit_or_another_document_is_left_as_it_is(office):
    ids = office.ids
    ws = focused(office, RECENT, ids["hatzalon7_c"])
    out = run(ws, "search", query=QUERY, limit=6, document_ids=[ids["hatzalon7_a"]])
    assert set(order(out)) == {ids["hatzalon7_a"]} and T.MSG_PROPERTY_SEARCHED not in out
    ver, reading = version_of(office, ids["hatzalon7_b"])
    own = T.unit_handle_of(ws, ids["hatzalon7_b"], ver, reading, 1)
    out = run(ws, "search", query=QUERY, limit=6, unit=own)
    assert set(order(out)) == {ids["hatzalon7_b"]} and T.MSG_PROPERTY_SEARCHED not in out


# --- units without a shared key; a file of several properties ---------------------------------------------------------

def plain(office, key: str, valuation: str, identifiers: list[str], blocks_after: list[str] | None = None) -> str:
    body = ["מסמך סינתטי — נתונים בדויים", f"המועד הקובע: {valuation}", "שווי הנכס ברחוב הצאלון 7 הוא 2,450,000 ₪ (סינתטי).",
            *(blocks_after or [])]
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}",
                       [{"title": f"מסמך {key}", "blocks": body, "identifiers": identifiers}],
                       sha=__import__("hashlib").sha256(key.encode()).hexdigest())
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


def test_the_street_and_number_key_a_unit_that_states_no_block_and_parcel(bare):
    a = plain(bare, "addr-a", "01/01/2022", ["רחוב הצאלון 7"])
    b = plain(bare, "addr-b", "01/01/2025", ["רחוב הצאלון 7"])
    plain(bare, "addr-other", "01/01/2024", ["רחוב הצאלון 11"])
    other_parcel = plain(bare, "bp-only", "01/01/2023", ["גוש 7101 חלקה 15"])
    got = order(run(focused(bare, RECENT, a), "search", query=QUERY, limit=6))
    assert got == [b, a] and other_parcel not in got


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


# --- the key ----------------------------------------------------------------------------------------------------------

def test_the_property_key_prefers_block_and_parcel_normalizes_and_is_none_without_identifiers():
    bp = {"parcels": [["07101", "015"]], "addresses": [["צאלון", "7"]], "shown": []}
    same = {"parcels": [["7101", "15"]], "addresses": [["אחר", "9"]], "shown": []}
    assert P.property_key(bp) == P.property_key(same) is not None
    assert P.property_key({"addresses": [["צאלון", "7"]], "parcels": []}) == P.property_key({"shown": ["רחוב הצאלון 7"]})
    assert P.property_key({"addresses": [["צאלון", "7"]]}) != P.property_key({"addresses": [["צאלון", "11"]]})
    assert P.property_key({}) is None and P.property_key(None) is None and P.property_key({"shown": []}) is None
