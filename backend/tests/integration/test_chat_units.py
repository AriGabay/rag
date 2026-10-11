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
from tests.support.unit_files import reread, unit_file

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
R9A, R9B = FIXTURES / MANIFEST["compendium"]["file"], FIXTURES / MANIFEST["similar_names"]["file"]
R9A_TITLE = MANIFEST["compendium"]["title"]
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


# --- the unit in the conversation's focus (round 9 U10: KTD8, R14, AE5) ----------------------------------------------


def _parse(**kw) -> dict:
    """The resolving model's parse of a follow-up."""
    return {"relation": "metric_change", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _datum(doc: str, subject: str) -> dict:
    return {"metric_as_written": "שווי הנכס", "metric_kind": "value", "unit": "ILS", "period": "none", "area_basis": "",
            "vat": "unknown", "subject": subject, "value_role": "appraiser_determination", "document_ids": [doc]}


def _sid(items, needle: str) -> str:
    outs = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
    return next(m.group(1) for o in outs for m in re.finditer(r'<source id="(S\d+)"[^>]*>[^<]*?' + re.escape(needle), o))


def _value_turn(client, office, monkeypatch, unit: int, value: str, steps_before=()) -> tuple[str, dict]:
    """The first turn: the sale value of the appraisal ``UNITS[unit]``, found by its name and searched inside it."""
    name = UNITS[unit]["aliases"][0]

    def answer(items) -> dict:
        sid = _sid(items, value)
        return final(f"שווי הנכס הוא {value} ₪ [{sid}].", focus=_datum(office.doc, UNITS[unit]["title"]),
                     claims=[{"text": f"שווי הנכס הוא {value} ₪", "source_ids": [sid], "basis": "explicit"}])

    agent = ScriptedAgent([[call("find_documents", query=name, page=None)],
                           [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")], answer])
    cloud(monkeypatch, office, agent)
    cid = new_conversation(office.client)
    m = send(office.client, cid, f"מה שווי הנכס ב{name}?")
    assert m["status"] == "done", m
    return cid, m


def _follow(client, cid: str, monkeypatch, message: str, steps: list, parse: dict) -> tuple[ScriptedAgent, dict]:
    agent = ScriptedAgent(steps)
    agent.on("resolve", parse)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, message)
    assert m["status"] == "done", m
    return agent, m


def test_the_first_answer_leaves_the_unit_it_was_found_in_as_the_conversations_focus(client, r9a, monkeypatch):
    _, m = _value_turn(client, r9a, monkeypatch, 1, SECOND)
    a = m["answer"]
    unit = a["unit"]
    assert (unit["document_id"], unit["unit_no"], unit["display"]) == (r9a.doc, 2, UNITS[1]["title"])
    assert unit["reading_id"] == _reading(r9a, r9a.ver) and unit["first_label"] == "1" and unit["pages"] == [3, 4]
    assert unit["title"] == contexts.name_key(UNITS[1]["title"]) and "גבעת הרקפות" in unit["aliases"]
    assert a["focus"]["located_unit"] == unit and "unit_scope" not in a  # the unit was not kept from a turn before


def test_price_then_rent_stays_on_the_same_unit_without_a_clarification(client, r9a, monkeypatch):
    """AE5. The rent of R9a is written in the third appraisal only: a search that left the unit would find it there."""
    cid, first = _value_turn(client, r9a, monkeypatch, 1, SECOND)
    agent, m = _follow(client, cid, monkeypatch, "ומה דמי השכירות?", [
        [call("search", query="דמי השכירות החודשיים", document_ids=None, limit=None, unit=None)],
        final("לא נמצאו דמי שכירות ביחידה שנשאלה.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    task = agent.seen[0][0]["content"]
    assert UNITS[1]["title"] in task and "U1" in task and UNITS[2]["title"] not in task
    searched = agent.tool_outputs(1)[0]  # no unit named in the call: the conversation's unit is the scope
    assert "חיפוש בתוך היחידה U1" in searched and "(היחידה שהשיחה עוסקת בה: U1)" in searched
    assert "92" not in searched and set(re.findall(r'unit_id="[^"]*#(\d+)"', searched)) == {"2"}
    a = m["answer"]
    assert a["status"] != "clarification" and a["request"]["entity_changed"] is False
    assert (a["request"]["located_unit"]["unit_no"], a["request"]["unit_kept"]) == (2, True)
    assert a["unit_scope"] == {"title": UNITS[1]["title"], "pages": [3, 4], "kept": True, "file_title": R9A_TITLE}


def test_naming_another_project_moves_the_conversation_to_its_unit(client, r9a, monkeypatch):
    cid, _ = _value_turn(client, r9a, monkeypatch, 1, SECOND)

    def answer(items) -> dict:
        sid = _sid(items, "92")
        return final(f"דמי השכירות החודשיים למ\"ר הם 92 ₪ [{sid}].", focus=_datum(r9a.doc, UNITS[2]["title"]),
                     claims=[{"text": "דמי השכירות החודשיים למ\"ר הם 92 ₪", "source_ids": [sid], "basis": "explicit"}])

    agent, m = _follow(client, cid, monkeypatch, "ובפרויקט נחל הדס?", [
        [call("search", query="דמי השכירות החודשיים", document_ids=None, limit=None, unit=None)], answer],
        _parse(relation="correction", changed_fields=[{"field": "subject", "user_words": "בפרויקט נחל הדס"}],
               subject="פרויקט נחל הדס"))
    a = m["answer"]
    assert a["request"]["document_ids"] == [r9a.doc]  # the same file, another unit
    assert a["request"]["entity_changed"] is True and a["request"]["unit_kept"] is False
    assert a["request"]["located_unit"]["unit_no"] == 3 and a["request"]["located_unit"]["display"] == UNITS[2]["title"]
    task = agent.seen[0][0]["content"]
    assert UNITS[2]["title"] in task and UNITS[1]["title"] not in task and "הנתון שבמרכז השיחה" not in task
    assert "92" in agent.tool_outputs(1)[0] and SECOND not in agent.tool_outputs(1)[0]
    assert a["unit"]["unit_no"] == 3 and a["focus"]["located_unit"]["unit_no"] == 3 and "unit_scope" not in a
    assert {s["unit"]["no"] for s in a["sources"] if s.get("unit")} == {3}


def test_a_turn_that_ended_on_the_time_limit_keeps_the_unit_and_continue_reads_on_inside_it(client, r9a, monkeypatch):
    from app.config import get_settings
    from tests.integration.test_chat_inspect import Clock

    for k, v in {"chat_turn_seconds": 150, "chat_verify_reserve_seconds": 40, "chat_verify_min_seconds": 15}.items():
        monkeypatch.setattr(get_settings(), k, v)
    clock = Clock(monkeypatch)
    agent = ScriptedAgent([[call("find_documents", query=UNITS[1]["aliases"][0], page=None)],
                           [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
                           final("עצרתי במגבלת הזמן לפני שהספקתי לקרוא את הערכת השווי.", "partial")])
    agent.on_step = lambda i: setattr(clock, "offset", 112) if i == 1 else None
    cloud(monkeypatch, r9a, agent)
    cid = new_conversation(client)
    first = send(client, cid, f"מה שווי הנכס ב{UNITS[1]['aliases'][0]}?")
    assert first["status"] == "done", first
    a = first["answer"]
    assert a["limits_hit"] == ["time_limit"] and a["focus"] is None  # nothing was cited: no datum is the focus...
    assert a["unit"]["unit_no"] == 2  # ...and the unit the turn worked in is still the conversation's
    clock.offset = 0

    def answer(items) -> dict:
        sid = _sid(items, SECOND)
        return final(f"שווי הנכס הוא {SECOND} ₪ [{sid}].", focus=_datum(r9a.doc, UNITS[1]["title"]),
                     claims=[{"text": f"שווי הנכס הוא {SECOND} ₪", "source_ids": [sid], "basis": "explicit"}])

    agent2, m = _follow(client, cid, monkeypatch, "המשך", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit=None)], answer],
        _parse(relation="same_datum"))
    assert UNITS[1]["title"] in agent2.seen[0][0]["content"]
    out = agent2.tool_outputs(1)[0]
    assert SECOND in out and FIRST not in out and THIRD not in out
    assert m["answer"]["request"]["located_unit"]["unit_no"] == 2 and m["answer"]["unit_scope"]["kept"] is True


# the file read again: a new reading id and, in the synthetic file, other numbers

SYN = "קובץ שומות סינתטי"


@pytest.fixture
def syn(office):
    office.doc, office.ver = unit_file(office, office.default_group_id, SYN, [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"],
         "blocks": ["פרויקט הארז", "שווי הנכס 1,000,000 ₪."]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "identifiers": ["רחוב האשל 9"],
         "blocks": ["מתחם האשל", "שווי הנכס 2,000,000 ₪."]},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"],
         "blocks": ["פרויקט הברוש", "שווי הנכס 3,000,000 ₪."]}])
    return office


def _syn_turn(client, office, monkeypatch) -> str:
    def answer(items) -> dict:
        sid = _sid(items, "2,000,000")
        return final(f"שווי הנכס הוא 2,000,000 ₪ [{sid}].", focus=_datum(office.doc, "מתחם האשל"),
                     claims=[{"text": "שווי הנכס הוא 2,000,000 ₪", "source_ids": [sid], "basis": "explicit"}])

    agent = ScriptedAgent([[call("find_documents", query="האשל", page=None)],
                           [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")], answer])
    cloud(monkeypatch, office, agent)
    cid = new_conversation(client)
    m = send(client, cid, "מה שווי הנכס במתחם האשל?")
    assert m["status"] == "done" and m["answer"]["unit"]["unit_no"] == 2, m
    return cid


def test_after_a_reindex_the_unit_is_found_again_by_its_anchor_and_never_by_its_number(client, syn, monkeypatch):
    cid = _syn_turn(client, syn, monkeypatch)
    # read again: a new front-matter unit shifts the numbers, so the old number 2 is now the first appraisal
    reread(syn, syn.ver, "reading-units-2", [
        {"kind": "front_matter", "title": None, "blocks": (0, 0), "pages": (1, 1)},
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (1, 1), "pages": (1, 1)},
        {"title": "מתחם האשל", "aliases": ["האשל"], "identifiers": ["רחוב האשל 9"], "blocks": (2, 3), "pages": (2, 2)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (4, 5),
         "pages": (3, 3)}])
    contexts._CACHE.clear()
    agent, m = _follow(client, cid, monkeypatch, "ומה דמי השכירות?", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit=None)],
        final("לא נמצאו דמי שכירות.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    located = m["answer"]["request"]["located_unit"]
    assert (located["unit_no"], located["reading_id"], located["display"]) == (3, "reading-units-2", "מתחם האשל")
    out = agent.tool_outputs(1)[0]
    assert "2,000,000" in out and "1,000,000" not in out and "3,000,000" not in out  # inside the unit now numbered 3
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]
    assert d["decisions"]["unit"] == "re-resolved"
    assert m["answer"]["unit"]["unit_no"] == 3 and m["answer"]["unit"]["reading_id"] == "reading-units-2"


def test_a_unit_that_is_gone_after_a_reindex_asks_and_the_old_number_selects_nothing(client, syn, monkeypatch):
    cid = _syn_turn(client, syn, monkeypatch)
    # read again with no appraisal of that name: the old number 2 is now the third appraisal
    reread(syn, syn.ver, "reading-units-3", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (0, 1), "pages": (1, 1)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (2, 5),
         "pages": (2, 3)}])
    contexts._CACHE.clear()
    agent, m = _follow(client, cid, monkeypatch, "ומה דמי השכירות?", [],
                       _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}],
                              metric_kind="rent"))
    a = m["answer"]
    assert a["status"] == "clarification" and agent.seen == []  # asked, and no answering step
    assert "מתחם האשל" in a["markdown"] and "פרויקט הארז" in a["markdown"] and "פרויקט הברוש" in a["markdown"]
    assert a["request"]["located_unit"] is None and a["request"]["document_ids"] == []
    assert {c["unit"]["display"] for c in a["request"]["candidates"]} == {"פרויקט הארז", "פרויקט הברוש"}
    assert "unit" not in a or a["unit"] is None
    # the reply picks among the units named; the chosen one becomes the conversation's
    agent3, m3 = _follow(client, cid, monkeypatch, "השני", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit=None)],
        final("לא נמצא.", "not_found")], _parse(relation="clarification_answer"))
    assert m3["answer"]["request"]["located_unit"]["display"] == "פרויקט הברוש"
    assert "3,000,000" in agent3.tool_outputs(1)[0] and "1,000,000" not in agent3.tool_outputs(1)[0]


def test_a_unit_of_a_file_the_user_can_no_longer_see_is_dropped_silently(client, r9a, monkeypatch):
    cid, _ = _value_turn(client, r9a, monkeypatch, 1, SECOND)
    with tenant_tx(r9a.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": r9a.doc})
    agent, m = _follow(client, cid, monkeypatch, "ומה דמי השכירות?", [final("אין לי גישה לנתון הזה.", "not_found")],
                       _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}],
                              metric_kind="rent"))
    a = m["answer"]
    assert a["request"] is None or a["request"]["located_unit"] is None
    assert SECOND not in a["markdown"] and "unit_scope" not in a


def test_the_conversations_unit_is_the_default_scope_of_a_search_or_outline_that_names_no_other_document(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = T.unit_handle_of(ws, r9a.doc, r9a.ver, _reading(r9a, r9a.ver), 2)
    ws.unit_focus = {"handle": handle, "document_id": r9a.doc, "unit_id": None}
    searched = T.tool_search(ws, "שווי הנכס", None, 12)
    assert SECOND in searched and FIRST not in searched and THIRD not in searched
    assert f"(היחידה שהשיחה עוסקת בה: {handle})" in searched
    # the document asked for by its id or its handle is the unit's file: still inside the unit
    assert FIRST not in T.tool_search(ws, "שווי הנכס", [r9a.doc], 12)
    assert FIRST not in T.tool_search(ws, "שווי הנכס", [ws.doc_handle(r9a.doc)], 12)
    outline = T.tool_outline(ws, r9a.doc)
    assert f"סעיפי היחידה {handle} בלבד" in outline and "8. תיאור הנכס" in outline and "13. מבוא" not in outline
    # another document is not the conversation's unit: the call is left as it is
    other = "00000000-0000-0000-0000-000000000000"
    assert T.focus_unit_for(ws, [other]) is None and T.focus_unit_for(ws, [r9a.doc, other]) is None
    assert T.focus_unit_for(T.Workspace(ctx=r9a.ctx()), None) is None  # no unit in the conversation: nothing changes
    # an explicit unit of the call wins over the default
    first = _unit_handle(T.tool_find_documents(ws, UNITS[0]["aliases"][0]), UNITS[0]["title"])
    assert FIRST in T.tool_search(ws, "שווי הנכס", None, 12, first)


def test_the_unit_of_an_answer_is_a_document_the_answer_rests_on_for_the_permission_check():
    from app.chat import api

    assert api._answer_documents({"unit": {"document_id": "d-7"}}) == {"d-7"}
    assert api._answer_documents({"unit": None}) == set()
    focus = api._with_unit({"document_ids": ["d-1"], "metric_kind": "value"}, {"document_id": "d-1", "unit_no": 2})
    assert focus["located_unit"]["unit_no"] == 2 and focus["metric_kind"] == "value"
    assert "located_unit" not in api._with_unit({"document_ids": ["d-1"]}, {"document_id": "d-9", "unit_no": 2})
    bare = api._with_unit(None, {"document_id": "d-1", "unit_no": 2, "display": "פרויקט הארז"})
    assert bare["document_ids"] == ["d-1"] and bare["metric_kind"] == "unknown" and bare["subject"] == "פרויקט הארז"
    assert api._with_unit(None, None) is None


def test_pages_of_the_conversations_file_are_read_within_its_unit(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = T.unit_handle_of(ws, r9a.doc, r9a.ver, _reading(r9a, r9a.ver), 2)
    ws.unit_focus = {"handle": handle, "document_id": r9a.doc, "unit_id": None}
    out = T.tool_read(ws, {"pages": {"document": r9a.doc, "from_page": 1, "to_page": 8}})
    assert SECOND in out and FIRST not in out and THIRD not in out and "הטווח נחתך לעמודי היחידה" in out
    refused = T._run_tool(ws, "read", json.dumps({"target": {"pages": {"document": r9a.doc, "from_page": 6, "to_page": 7},
                                                              "source": None, "section": None, "table": None,
                                                              "cursor": None}}))
    assert "אינם ביחידה" in refused and "לא נפתח דבר" in refused
