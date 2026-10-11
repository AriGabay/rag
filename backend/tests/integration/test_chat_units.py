"""Two-level retrieval: locate the unit, then search and read inside it (round 9 U9: KTD7, KTD8; R10–R13, R15; AE2, AE3,
AE4).

The synthetic compendium R9a (three appraisals titled only by a project name, then an appendix of comparables) and the
similar-names file R9b are ingested through the real worker with OCR, vision and the measurement pass off; the
mis-cut, name-fit and permission cases are built from blocks, chunks and stored units (``tests/support/unit_files``).
The model is scripted: these tests prove what the server finds, scopes, labels and asks. Nothing here is customer
data; every name and figure is invented."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app import worker
from app.chat import contexts, entities, resolve
from app.chat import tools as T
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.helpers import upload
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_attribution import _meaning
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
R9A, R9B = FIXTURES / MANIFEST["compendium"]["file"], FIXTURES / MANIFEST["similar_names"]["file"]
FACTS = MANIFEST["compendium"]["facts"]
UNITS = MANIFEST["compendium"]["units"]
SECOND, FIRST, THIRD = FACTS["second_value"]["value"], FACTS["first_value"]["value"], FACTS["third_value"]["value"]


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client = client
    return a


def ingest(office, path: Path) -> tuple[str, str]:
    r = upload(office.client, [(path.name, path.read_bytes())], group_id=office.default_group_id)
    assert r.status_code == 200, r.text
    while worker.run_one("test-worker"):
        pass
    res = r.json()["results"][0]
    return res["document_id"], res["version_id"]


@pytest.fixture
def r9a(office):
    office.doc, office.ver = ingest(office, R9A)
    return office


def _reading(office, ver: str) -> str:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v"),
                            {"v": ver}).scalar_one()


def _unit_handle(out: str, title: str) -> str:
    for line in out.splitlines():
        m = re.search(r"\b(U\d+)\b", line)
        if m and title in line:
            return m.group(1)
    raise AssertionError(out)


def _sources(out: str) -> list[str]:
    return re.findall(r'<source id="(S\d+)"', out)


# --- locating the unit (AE2, AE3) -------------------------------------------------------------------------------------


def test_the_second_project_is_located_by_its_name_with_its_pages_and_identifiers_and_the_appendix_is_not_offered(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    out = T.tool_find_documents(ws, UNITS[1]["aliases"][0])  # "גבעת הרקפות": in no file title
    unit_lines = [ln for ln in out.splitlines() if re.match(r"\s+- U\d+ \|", ln)]
    assert len(unit_lines) == 1, out
    line = unit_lines[0]
    assert f"«{UNITS[1]['title']}»" in line and "עמודים 3–4" in line and "שומה" in line
    assert "רחוב הרקפת 8" in line and "גוש 31140 חלקה 22" in line  # the identifiers it states
    assert UNITS[0]["title"] not in out and UNITS[2]["title"] not in out and "נספח" not in line
    assert ws.scope["matching"] == [{"document_id": r9a.doc, "title": MANIFEST["compendium"]["title"]}]


def test_a_property_that_is_only_a_comparable_is_reported_as_one_never_as_an_appraisal(r9a):
    row = MANIFEST["compendium"]["facts"]["appendix_table"]["rows"][0][0]  # a comparable the appendix table lists
    ws = T.Workspace(ctx=r9a.ctx())
    out = T.tool_find_documents(ws, row)
    assert not re.search(r"\s+- U\d+ \|", out)
    assert "רק כנכס השוואה" in out and "אינה שומה של הנכס" in out and UNITS[3]["title"] in out


def test_terms_split_between_the_first_and_the_third_appraisal_match_no_unit(r9a):
    """AE3."""
    ws = T.Workspace(ctx=r9a.ctx())
    out = T.tool_find_documents(ws, f"{UNITS[0]['aliases'][1]} {UNITS[2]['aliases'][0]}")  # "האלון נחל הדס"
    assert not re.search(r"\s+- U\d+ \|", out)
    assert "ביחידות שונות" in out and "אין בו יחידה אחת שמכילה את כולם" in out
    assert ws.scope["matching"] == []


# --- searching, reading and outlining inside the unit (R10, R15) ------------------------------------------------------


def test_a_search_inside_the_second_unit_returns_only_its_pages_and_labels_every_hit(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    out = T.tool_search(ws, "שווי הנכס", None, None, handle)
    assert SECOND in out and FIRST not in out and THIRD not in out
    assert f"חיפוש בתוך היחידה {handle}" in out
    sids = _sources(out)
    assert sids
    key = f"{r9a.ver}#{_reading(r9a, r9a.ver)}#2"
    for sid in sids:
        s = ws.sources[sid]
        assert s.unit["id"] == key and s.unit["no"] == 2 and s.unit["title"] == UNITS[1]["title"]
        assert s.unit["pages"] == [3, 4] and set(s.page_list or []) <= {3, 4, 5}
        assert f'unit_id="{key}"' in out
        assert s.public()["unit"]["id"] == key and ws.anchors[sid]["unit"]["id"] == key
    assert not any(p in (1, 2, 6, 7, 8) for sid in sids for p in ws.sources[sid].page_list or [])


def test_the_same_search_over_the_file_labels_each_hit_by_the_unit_it_is_in(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    out = T.tool_search(ws, "שווי הנכס", None, 12)
    keys = {ws.sources[s].unit["no"] for s in _sources(out) if ws.sources[s].unit}
    assert {1, 2, 3} <= keys
    for sid in _sources(out):
        assert ws.sources[sid].unit is not None and ws.sources[sid].tags["unit"].startswith("U")


def test_a_unit_search_with_no_hit_in_a_certain_unit_says_it_was_searched_and_nothing_was_found(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    seen = []
    real = T.memoized_search
    T.memoized_search = lambda ws_, conn, q, limit, scope: (seen.append(scope), real(ws_, conn, q, limit, scope))[1]
    try:
        out = T.tool_search(ws, "משהו שלא כתוב בשום מקום זבנג", None, None, handle)
    finally:
        T.memoized_search = real
    assert "היחידה נחקרה" in out and "לא חיפשתי בשאר הקובץ" in out and "הרחבתי" not in out
    assert len(seen) == 1 and seen[0].blocks is not None  # one search, inside the unit: never a silent whole-file one
    assert [s for s in ws.sources.values() if not s.is_listing] == []  # the search registered no passage


def test_the_outline_lists_the_units_and_then_only_the_chosen_units_sections(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    whole = T.tool_outline(ws, r9a.doc)
    assert "יחידות בקובץ" in whole and whole.count(" unit_id=") == 4
    assert "סעיפים:" not in whole and "outline עם unit=U#" in whole  # more than three units: the unit is chosen first
    handle = _unit_handle(whole, UNITS[1]["title"])
    one = T.tool_outline(ws, r9a.doc, handle)
    assert f"סעיפי היחידה {handle} בלבד" in one and "8. תיאור הנכס" in one and "12. סיכום ומסקנות" in one
    assert "2. תיאור הנכס" not in one.replace("12. תיאור", "") and "13. מבוא" not in one and "6. סיכום" not in one
    assert T.tool_outline(ws, handle) == one  # a unit handle may stand for the document
    appendix = T.tool_outline(ws, r9a.doc, _unit_handle(whole, UNITS[3]["title"]))
    assert "טבלאות:" in appendix and "נספח" in appendix


def test_pages_of_a_unit_are_read_within_it(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    out = T.tool_read(ws, {"pages": {"document": handle, "from_page": 1, "to_page": 8}})
    assert SECOND in out and FIRST not in out and THIRD not in out
    assert "הטווח נחתך לעמודי היחידה" in out
    assert ws.sources[_sources(out)[0]].unit["no"] == 2
    refused = T._run_tool(ws, "read", json.dumps({"target": {"pages": {"document": handle, "from_page": 6,
                                                                       "to_page": 7}, "source": None, "section": None,
                                                              "table": None, "cursor": None}}))
    assert "אינם ביחידה" in refused and "לא נפתח דבר" in refused


# --- a value, a measurement and a citation carry the unit ------------------------------------------------------------


def test_a_stored_value_and_a_measurement_and_the_citation_carry_the_unit_id(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    out = T.tool_search(ws, f"שווי הנכס: {SECOND}", None, None, handle)
    sid = next(s for s in _sources(out) if SECOND in ws.sources[s].text)
    T.tool_take_value(ws, sid, {"quote": f"שווי הנכס: {SECOND} ₪.", "number": SECOND},
                      _meaning(kind="value", unit="ILS", subject=UNITS[1]["title"]), "שווי")
    value = list(ws.values.values())[-1]
    key = f"{r9a.ver}#{_reading(r9a, r9a.ver)}#2"
    assert value.context["unit_id"] == key and value.context["title"] == UNITS[1]["title"]
    assert ws.anchors[value.vid]["unit"]["id"] == key
    # a stored measurement of a block in the second unit carries it too
    with tenant_tx(r9a.system) as conn:
        block = conn.execute(text("SELECT block_first FROM document_units WHERE version_id = :v AND unit_no = 2"),
                             {"v": r9a.ver}).scalar_one() + 3
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
            " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
            " extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :b, 'b:x', 'שווי', 'value',"
            " 11776000, 'exact', '11,776,000 ₪', 'ILS', 'none', 'unknown', 'appraised_property', 'other', :q,"
            " :e, 'test', 'auto_validated', '[]'::jsonb)"),
            {"d": r9a.doc, "v": r9a.ver, "b": block, "q": f"שווי הנכס: {SECOND} ₪.",
             "e": EXTRACTION_VERSION})
    T.tool_find_measurements(ws, "שווי הנכס", ["value"])
    m = next(iter(ws.measurements.values()))
    assert m.context["unit_id"] == key and m.public()["unit_id"] == key and ws.anchors[m.mid]["unit"]["id"] == key


# --- a boundary that is only probable (R8, KTD7) ----------------------------------------------------------------------


def _cut_early_and_mark_probable(office, ver: str) -> None:
    """Move the cut between the second and the third appraisal to just before the second's valuation chapter, as a
    mis-cut would: the second appraisal's value sits just past the cut, in what is now the third unit, whose opening
    edge is only probable."""
    with tenant_tx(office.system) as conn:
        cut = conn.execute(text("SELECT block_index FROM document_blocks WHERE version_id = :v"
                                " AND text LIKE '11. %'"), {"v": ver}).scalar_one()
        conn.execute(text("UPDATE document_units SET block_last = :c WHERE version_id = :v AND unit_no = 2"),
                     {"v": ver, "c": cut - 1})
        conn.execute(text("UPDATE document_units SET block_first = :c, certainty = 'probable' WHERE version_id = :v"
                          " AND unit_no = 3"), {"v": ver, "c": cut})
    contexts._CACHE.clear()


def test_an_answer_just_past_a_probable_cut_is_still_found_and_the_extension_is_stated(r9a):
    ws0 = T.Workspace(ctx=r9a.ctx())
    handle0 = _unit_handle(T.tool_find_documents(ws0, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    assert handle0
    _cut_early_and_mark_probable(r9a, r9a.ver)
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    out = T.tool_search(ws, f"שווי הנכס {SECOND}", None, None, handle)
    assert SECOND in out
    assert "גבול היחידה אינו ודאי" in out and "הרחבתי" not in out  # extended over the neighbour, not the whole file
    assert FIRST not in out


def test_a_unit_search_with_no_hit_next_to_a_probable_edge_widens_once_to_the_file_and_says_so(r9a):
    _cut_early_and_mark_probable(r9a, r9a.ver)
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    out = T.tool_search(ws, UNITS[0]["aliases"][0], None, None, handle)  # the first appraisal's name: not in 2 or 3
    assert "הרחבתי פעם אחת את החיפוש לכל הקובץ" in out
    hit = next(s for s in _sources(out) if UNITS[0]["aliases"][0] in ws.sources[s].text)
    assert ws.sources[hit].unit["no"] == 1  # labelled by its own unit, not the unit asked about
    nothing = T.tool_search(ws, "זבנג שאין בשום מקום", None, None, handle)
    assert "היחידה נחקרה" in nothing and "גם הרחבה לכל הקובץ לא מצאה דבר" in nothing


# --- clarification between similar names (AE4, R13) ------------------------------------------------------------------


def test_similar_names_give_a_clarification_listing_both_units_in_name_order_with_pages_and_the_file(office):
    doc, ver = ingest(office, R9B)
    out = entities.lookup(office.ctx(), "הגפן", set())
    assert out.kind == "ambiguous"
    assert [d.title for d in out.documents] == ["מתחם הגפן א", "מתחם הגפן ב"]
    anchors = [d.unit for d in out.documents]
    assert [a["pages"] for a in anchors] == [[1, 1], [2, 2]] and {a["document_id"] for a in anchors} == {doc}
    assert [a["first_label"] for a in anchors] == ["1", "1"] and anchors[0]["aliases"][0] == "הגפן א"
    assert anchors[0]["reading_id"] == _reading(office, ver) and "unit_no" in anchors[0]
    asked = resolve._clarify_titles(out, "הגפן")
    assert "1. «מתחם הגפן א» (עמוד 1;" in asked and "2. «מתחם הגפן ב» (עמוד 2;" in asked
    assert MANIFEST["similar_names"]["title"] in asked


def test_replying_the_second_or_a_letter_only_one_unit_holds_picks_that_unit(office):
    ingest(office, R9B)
    out = entities.lookup(office.ctx(), "הגפן", set())
    cands = [d.as_dict() for d in out.documents]
    allowed = {c["document_id"] for c in cands}
    for reply, title in [("השני", "מתחם הגפן ב"), ("הראשון", "מתחם הגפן א"), ("2", "מתחם הגפן ב"),
                         ("ב", "מתחם הגפן ב"), ("הגפן א", "מתחם הגפן א"), ("תכוון לזה שנקרא מתחם הגפן ב", "מתחם הגפן ב")]:
        picked = entities.among(cands, reply, allowed)
        assert picked.kind == "resolved" and [d.title for d in picked.documents] == [title], reply
        assert picked.documents[0].unit["display"] == title
    assert entities.among(cands, "הגפן", allowed).kind == "ambiguous"  # a word both hold: ask again
    assert entities.among(cands, "לא יודע", allowed).kind == "ambiguous"
    # the chosen unit becomes the request's unit
    chosen = entities.among(cands, "השני", allowed)
    req = resolve.Request("clarification_answer", "מה השכירות?")
    resolve._take(req, chosen, "השני", {})
    assert req.located_unit["display"] == "מתחם הגפן ב" and req.located_unit["pages"] == [2, 2]
    assert req.located_unit["title"] == "מתחם הגפן ב"


# --- name fit, never passage counts (R13) ------------------------------------------------------------------------------

G = "משרד א"


@pytest.fixture
def fits(db):
    a = make_office(db, G, "a@example.test")
    many = ["הנכס מגדל הים כתוב כאן שוב."] * 6
    a.doc, a.ver = unit_file(a, a.default_group_id, "קובץ שמות סינתטי", [
        {"title": "מגדל הים הצפוני", "aliases": ["מגדל הים הצפוני", "הים הצפוני"], "blocks": ["מגדל הים הצפוני", *many]},
        {"title": "מגדל הים", "aliases": ["מגדל הים"], "blocks": ["מגדל הים", "שווי הנכס 1,000,000 ₪."]},
        {"aliases": [], "blocks": ["שומת הנכס בקומה", *many, "הים הצפוני ליד הנכס"]},
    ])
    return a


def test_a_unique_name_selects_its_unit_even_when_another_unit_has_more_passages(db):
    a = make_office(db, G, "a@example.test")
    unit_file(a, a.default_group_id, "קובץ שמות סינתטי", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", "שווי הנכס 1,000,000 ₪."]},
        {"aliases": [], "blocks": ["שומת הנכס", *["הארז נזכר בפסקה."] * 8]},
    ])
    out = entities.lookup(a.ctx(), "הארז", set())
    assert out.kind == "resolved" and [d.unit["display"] for d in out.documents] == ["פרויקט הארז"]
    assert out.documents[0].hits <= 2  # chosen by its name, not by the 8 passages of the other unit


def test_ambiguous_units_are_ordered_by_name_fit_not_by_the_number_of_passages(fits):
    out = entities.lookup(fits.ctx(), "מגדל הים", set())
    assert out.kind == "ambiguous"
    assert [d.unit["display"] for d in out.documents] == ["מגדל הים", "מגדל הים הצפוני"]
    with tenant_tx(fits.system) as conn:  # the setup: the better fit has fewer passages and still leads
        counts = dict(conn.execute(text(
            "SELECT u.unit_no, count(c.id) FROM document_units u JOIN chunks c ON c.version_id = u.version_id"
            " AND c.block_start BETWEEN u.block_first AND u.block_last AND c.tsv @@ plainto_tsquery('simple', 'מגדל')"
            " WHERE u.version_id = :v GROUP BY u.unit_no"), {"v": fits.ver}).all())
    assert counts[1] > counts[2]


def test_the_cut_off_never_drops_a_better_fitting_unit_for_one_with_more_passages(db):
    a = make_office(db, G, "a@example.test")
    many = ["מגדל הים נזכר כאן."] * 7
    units = [{"title": f"מגדל הים {w}", "aliases": [f"מגדל הים {w}"], "blocks": [f"מגדל הים {w}", *many]}
             for w in ("הצפוני", "הדרומי", "המזרחי", "המערבי", "העליון")]
    units.append({"title": "מגדל הים", "aliases": ["מגדל הים"], "blocks": ["מגדל הים", "שווי הנכס 1,000,000 ₪."]})
    unit_file(a, a.default_group_id, "קובץ שמות סינתטי", units)
    out = entities.lookup(a.ctx(), "מגדל הים", set())
    assert out.kind == "ambiguous" and len(out.documents) == entities.CLARIFY_TITLES
    assert out.documents[0].unit["display"] == "מגדל הים"  # six fit; the best one stays and leads the five shown


# --- permissions (R13, KTD4) -------------------------------------------------------------------------------------------


def test_units_of_a_group_the_user_cannot_see_never_appear_as_candidates_or_vocabulary(db):
    a = make_office(db, G, "a@example.test")
    secret = make_group(a, "קבוצה 2")
    emp = make_user(a, "e@example.test", [a.default_group_id])
    unit_file(a, secret, "קובץ סודי סינתטי", [
        {"title": "פרויקט הסלעית", "aliases": ["הסלעית"], "blocks": ["פרויקט הסלעית", "שווי הנכס 2,000,000 ₪."]},
        {"title": "פרויקט האשל", "aliases": ["האשל"], "blocks": ["פרויקט האשל", "שווי הנכס 3,000,000 ₪."]},
    ])
    ctx = a.ctx("employee", emp)
    out = entities.lookup(ctx, "הסלעית", set())
    assert out.kind == "not_found" and out.documents == [] and out.split == []
    assert "הסלעית" not in entities.titles_of(ctx) and "הסלעית" in entities.titles_of(a.ctx())
    ws = T.Workspace(ctx=ctx)
    assert "U1" not in T.tool_find_documents(ws, "הסלעית")


def test_unit_names_join_the_vocabulary_but_a_comparables_part_names_no_document(r9a):
    titles = entities.titles_of(r9a.ctx())
    assert "גבעת הרקפות" in titles and "פרויקט נחל הדס" in titles
    assert UNITS[3]["title"] not in titles
    # a follow-up that names the project alone is an identifying word although no file title holds it
    assert entities.identifying("ומה השכירות בהרקפות?", titles, bare_numbers=False) == ["בהרקפות"]
    assert entities.identifying("ומה השכירות בהרקפות?", [MANIFEST["compendium"]["title"]], bare_numbers=False) == []


# --- a scripted turn on R9a reaches the second appraisal's facts ----------------------------------------------------


def test_a_scripted_turn_locates_the_second_appraisal_and_answers_from_its_pages_only(client, r9a, monkeypatch):
    key = f"{r9a.ver}#{_reading(r9a, r9a.ver)}#2"

    def answer(items) -> dict:
        outs = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
        sid = next(m.group(1) for o in outs for m in re.finditer(r'<source id="(S\d+)"[^>]*>[^<]*?' + SECOND, o))
        text_ = f"שווי הנכס הוא {SECOND} ₪ [{sid}]."
        return final(text_, claims=[{"text": f"שווי הנכס הוא {SECOND} ₪", "source_ids": [sid], "basis": "explicit"}])

    agent = ScriptedAgent([
        [call("find_documents", query=UNITS[1]["aliases"][0], page=None)],
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
        answer])
    cloud(monkeypatch, r9a, agent)
    m = send(client, new_conversation(client), f"מה שווי הנכס ב{UNITS[1]['aliases'][0]}?")
    assert m["status"] == "done", m
    seen = agent.tool_outputs(1)[0]
    assert "U1" in seen and f"«{UNITS[1]['title']}»" in seen and "עמודים 3–4" in seen
    searched = agent.tool_outputs(2)[1]
    assert SECOND in searched and FIRST not in searched and THIRD not in searched
    a = m["answer"]
    assert SECOND in a["markdown"]
    units = [s["unit"] for s in a["sources"] if s.get("unit")]
    assert units and all(u["id"] == key and u["title"] == UNITS[1]["title"] and u["pages"] == [3, 4] for u in units)


# --- handles ----------------------------------------------------------------------------------------------------------


def test_a_unit_handle_that_was_never_issued_is_refused_and_one_of_a_deleted_file_is_unavailable(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    args = {"query": "שווי הנכס", "document_ids": None, "limit": None, "unit": "U9"}
    assert "מזהה לא מוכר: U9" in T.run_tool(ws, "search", json.dumps(args))
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    with tenant_tx(r9a.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": r9a.doc})
    gone = T.run_tool(ws, "search", json.dumps(args | {"unit": handle}))
    assert T.MSG_UNAVAILABLE in gone and SECOND not in gone
    assert T.MSG_UNAVAILABLE in T.run_tool(ws, "outline", json.dumps({"document": handle, "unit": None}))
