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
from app.chat import units as unit_store
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.helpers import upload
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_attribution import _meaning
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.unit_files import READING, reread, unit_file

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


def _unit_id(office, number: int) -> str:
    """The stable id of a unit of the ingested file as the current derivation numbers it (version, reading, number and —
    after ``units-v4`` — the derivation's tag, round 15 U3)."""
    return contexts.unit_key(office.ver, _reading(office, office.ver), number, unit_store.current_tag())


def _unit_handle(out: str, title: str) -> str:
    for line in out.splitlines():
        m = re.search(r"\b(U\d+)\b", line)
        if m and title in line:
            return m.group(1)
    raise AssertionError(out)


def _sources(out: str) -> list[str]:
    return re.findall(r'<source id="(S\d+)"', out)


def _offer(doc: str, *units: str) -> dict:
    """The focus of a model clarification that offers the given units (U#, in the order it writes them): no datum, the units
    in ``units`` (round 15 U6, KTD1)."""
    return {"metric_as_written": "", "metric_kind": "unknown", "unit": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "value_role": "unknown", "document_ids": [doc], "units": list(units)}


def _card_titles(content: str) -> list[str]:
    """The titles of the cards of the candidates block of the turn's first message, in order."""
    return re.findall(r"^- U\d+ «([^»]*)»", content, re.M)


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
    key = _unit_id(r9a, 2)
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
    key = _unit_id(r9a, 2)
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


def test_a_unit_search_with_no_hit_next_to_a_probable_edge_never_widens_to_the_file_and_names_the_edge(r9a):
    """Round 15 U2, R8, KTD10: the silent widen-once is gone. The first appraisal's name is in neither the second unit nor
    the third (which the probable edge lets the search reach): the answer is the no-hit report, with no hit from the
    first unit, and it names the uncertain edge so the model widens by an explicit call."""
    _cut_early_and_mark_probable(r9a, r9a.ver)
    ws = T.Workspace(ctx=r9a.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, UNITS[1]["aliases"][0]), UNITS[1]["title"])
    seen = []
    real = T.memoized_search
    T.memoized_search = lambda ws_, conn, q, limit, scope: (seen.append(scope), real(ws_, conn, q, limit, scope))[1]
    try:
        out = T.tool_search(ws, UNITS[0]["aliases"][0], None, None, handle)  # the first appraisal's name: not in 2 or 3
    finally:
        T.memoized_search = real
    assert "הרחבתי" not in out and "היחידה נחקרה" in out and "לא חיפשתי בשאר הקובץ" in out
    assert "גבול היחידה אינו ודאי" in out  # the edge is named, with the neighbour the search reached
    third = T.unit_handle_of(ws, r9a.doc, r9a.ver, _reading(r9a, r9a.ver), 3)
    assert third in out
    assert len(seen) == 1 and seen[0].blocks is not None  # one search, inside the unit and its reach: never a file-wide one
    assert [s for s in ws.sources.values() if not s.is_listing] == []  # no passage of the first unit was returned


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
    labels = [entities.candidate_label(d.as_dict()) for d in out.documents]  # how a candidate is named to the model and the user
    assert labels[0].startswith("«מתחם הגפן א» (עמוד 1;") and labels[1].startswith("«מתחם הגפן ב» (עמוד 2;")
    assert all(MANIFEST["similar_names"]["title"] in label for label in labels)


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


# --- the first message of a conversation (R13, AE4) ------------------------------------------------------------------

R9B_TITLE = MANIFEST["similar_names"]["title"]
SIMILAR_QUESTION = "מה דמי השכירות החודשיים במתחם הגפן?"


@pytest.fixture
def statements():
    """The SELECTs executed while the test runs (the connection setup of ``tenant_tx`` excluded)."""
    from sqlalchemy import event

    from app.db import get_engine

    seen: list[str] = []

    def record(conn, cursor, statement, *args):  # noqa: ARG001
        if statement.lstrip().upper().startswith(("SELECT", "WITH")) and "set_config" not in statement:
            seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def test_a_first_message_whose_words_fit_two_units_equally_reaches_the_model_with_both_as_candidates(
        client, office, monkeypatch):
    doc, _ = ingest(office, R9B)
    # the question names a phrase both units' names hold ("מתחם הגפן"): hard evidence of a reference. Since round 15 U6 (KTD1,
    # R2) the server does not ask between them with no model call: both are candidates, in name-fit order, and the model, which
    # asks only for a material ambiguity, asks here in its own words, offering the two units it was shown
    asked = "בקובץ שני מתחמים: מתחם הגפן א ומתחם הגפן ב. לאיזה התכוונת?"
    agent = ScriptedAgent([final(asked, "clarification", clarification=asked, focus=_offer(doc, "U1", "U2"))],
                          intent="property")
    cloud(monkeypatch, office, agent)
    m = send(client, new_conversation(client), SIMILAR_QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["status"] == "clarification" and a["markdown"] == asked  # the model's question, not the server's
    assert len(agent.seen) == 1  # the model was called
    content = agent.seen[0][0]["content"]
    assert _card_titles(content) == ["מתחם הגפן א", "מתחם הגפן ב"] and R9B_TITLE in content
    assert [(c["document_id"], c["unit"]["unit_no"]) for c in a["offered_units"]] == [(doc, 1), (doc, 2)]
    assert not (a.get("request") or {}).get("candidates")


def test_a_first_message_is_left_to_the_model_unless_it_names_two_or_more_units_without_a_better_fit(
        office, r9a, monkeypatch):
    ingest(office, R9B)
    ctx = office.ctx()
    tie = entities.first_message_ambiguity(ctx, SIMILAR_QUESTION)
    assert tie is not None and [d.title for d in tie[0].documents] == ["מתחם הגפן א", "מתחם הגפן ב"]
    for question in ["מה דמי השכירות החודשיים במתחם הגפן ב?",  # the whole name of one unit
                     "מה דמי השכירות החודשיים?",  # no unit named
                     "מה שווי הנכס בשומה?",  # a word of the trade, in no name alone
                     f"מה שווי הנכס ב{UNITS[1]['aliases'][0]}?",  # a name only one unit holds
                     "כמה שומות יש בקובץ?"]:
        assert entities.first_message_ambiguity(ctx, question) is None, question


def test_a_first_message_naming_files_of_one_unit_each_is_left_to_the_model(db):
    a = make_office(db, "משרד א", "a@example.test")
    for n, name in enumerate(["מתחם הגפן א", "מתחם הגפן ב"]):
        unit_file(a, a.default_group_id, f"קובץ {name}", [{"title": name, "aliases": [name], "blocks": [name, "שווי 1 ₪"]}],
                  sha=str(n) * 64)
    assert entities.first_message_ambiguity(a.ctx(), SIMILAR_QUESTION) is None


def test_the_first_message_check_is_one_query_when_nothing_is_named(office, r9a, statements):
    ingest(office, R9B)
    statements.clear()
    assert entities.first_message_ambiguity(office.ctx(), "מה דמי השכירות החודשיים?") is None
    assert len(statements) == 1, statements


# --- the reply that is a unit's full name (R13) ------------------------------------------------------------------------


def _task_unit(agent: ScriptedAgent) -> str:
    """The unit the agent's task says the request is in (its «title» on the line `requested_block` writes)."""
    return re.search(r"- יחידה בקובץ: U\d+ «([^»]*)»", agent.seen[0][0]["content"]).group(1)


def _reply_turn(client, monkeypatch, cid: str, message: str, relation: str) -> tuple[ScriptedAgent, dict]:
    """The reply ``message`` as the resolving model parses it (``relation``, the words as a new subject), then the
    agent's turn; nothing the agent says matters here: the request is what the server resolved."""
    agent = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None, unit=None)],
                           final("בדיקה.", "not_found")])
    agent.on("resolve", _parse(relation=relation, changed_fields=[{"field": "subject", "user_words": message}],
                               subject=message))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, message)
    assert m["status"] == "done", m
    return agent, m


@pytest.mark.parametrize("relation", ["clarification_answer", "new_question"])
def test_after_the_models_clarification_between_units_the_reply_that_is_the_full_name_selects_that_unit_whatever_the_parse(
        client, office, monkeypatch, relation):
    doc, _ = ingest(office, R9B)
    asked = "בקובץ שני מתחמים: מתחם הגפן א ומתחם הגפן ב. לאיזה התכוונת?"
    cloud(monkeypatch, office, ScriptedAgent([final(asked, "clarification", clarification=asked,
                                                    focus=_offer(doc, "U1", "U2"))], intent="property"))
    cid = new_conversation(client)
    assert send(client, cid, SIMILAR_QUESTION)["answer"]["status"] == "clarification"
    agent, m = _reply_turn(client, monkeypatch, cid, "מתחם הגפן ב", relation)
    req = m["answer"]["request"]
    assert m["answer"]["status"] != "clarification"
    assert req["document_ids"] == [doc] and req["located_unit"]["display"] == "מתחם הגפן ב"
    assert req["located_unit"]["unit_no"] == 2 and req["entity_changed"] is True
    assert _task_unit(agent) == "מתחם הגפן ב"


def test_after_a_models_clarification_that_declared_no_unit_a_full_name_reply_is_an_ordinary_lookup_with_the_named_unit_first(
        client, office, monkeypatch):
    doc, _ = ingest(office, R9B)
    asked = "בקובץ שני מתחמים: מתחם הגפן א ומתחם הגפן ב. לאיזה התכוונת?"
    first = ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)],
                           final(asked, "clarification", clarification=asked)])
    cloud(monkeypatch, office, first)
    cid = new_conversation(client)
    m = send(client, cid, "מה דמי השכירות החודשיים?")
    assert m["answer"]["status"] == "clarification" and not m["answer"].get("offered_units")
    # a clarification that offered no unit binds nothing (KTD1): the reply is read as a new message, whose words the lookup
    # cannot tell apart ("מתחם הגפן ב" - the one letter is no search term), so both units reach the model, the named one first
    agent, m = _reply_turn(client, monkeypatch, cid, "מתחם הגפן ב", "new_question")
    req = m["answer"]["request"]
    assert req["located_unit"] is None and req["document_ids"] == [] and req["candidates"] == []
    content = agent.seen[0][0]["content"]
    assert _card_titles(content) == ["מתחם הגפן ב", "מתחם הגפן א"] and entities.BASIS_NAME in content


def test_a_reply_after_the_models_clarification_that_names_no_unit_in_full_is_not_looked_up(
        client, office, monkeypatch):
    ingest(office, R9B)
    asked = "בקובץ שני מתחמים: מתחם הגפן א ומתחם הגפן ב. לאיזה התכוונת?"
    cloud(monkeypatch, office, ScriptedAgent([[call("search", query="דמי שכירות", document_ids=None, limit=None)],
                                              final(asked, "clarification", clarification=asked)]))
    cid = new_conversation(client)
    send(client, cid, "מה דמי השכירות החודשיים?")
    # "הגפן" is a word both units share: no evidence of either (round 10 R8), so the server looks nothing up and asks
    # nothing; the model, which asked the question, asks it again
    again = "לאיזה מהמתחמים התכוונת: מתחם הגפן א או מתחם הגפן ב?"
    agent = ScriptedAgent([final(again, "clarification", clarification=again)])
    agent.on("resolve", _parse(relation="clarification_answer", changed_fields=[{"field": "subject", "user_words": "הגפן"}],
                               subject="הגפן"))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, "הגפן")
    assert m["answer"]["status"] == "clarification" and m["answer"]["markdown"] == again
    assert not m["answer"]["request"]["candidates"] and len(agent.seen) == 1  # the model asked again, in its own words


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
    key = _unit_id(r9a, 2)

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
        [call("search", query="דמי השכירות החודשיים", document_ids=None, limit=None, unit="U1")],  # the focus unit, named
        final("לא נמצאו דמי שכירות ביחידה שנשאלה.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    task = agent.seen[0][0]["content"]
    assert UNITS[1]["title"] in task and "U1" in task and UNITS[2]["title"] not in task
    assert _card_titles(task) == [UNITS[1]["title"]]  # the focus unit's card alone (R4)
    searched = agent.tool_outputs(1)[0]  # the call names the conversation's unit (round 15 U6: nothing is scoped silently)
    assert "חיפוש בתוך היחידה U1" in searched and "(היחידה שהשיחה עוסקת בה: U1)" in searched
    assert not re.search(r"(?<![\w-])92(?![\w-])", searched) and set(re.findall(r'unit_id="[^"]*#(\d+)(?:@[^"]*)?"', searched)) == {"2"}
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
    real_search = T.HANDLERS["search"]

    def slow_search(ws, args):  # the unit search runs; it takes the turn past its reading deadline (110 s)
        out = real_search(ws, args)
        clock.offset = 112
        return out

    monkeypatch.setitem(T.HANDLERS, "search", slow_search)
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
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")], answer],
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
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
        final("לא נמצאו דמי שכירות.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    located = m["answer"]["request"]["located_unit"]
    assert (located["unit_no"], located["reading_id"], located["display"]) == (3, "reading-units-2", "מתחם האשל")
    out = agent.tool_outputs(1)[0]
    assert "2,000,000" in out and "1,000,000" not in out and "3,000,000" not in out  # inside the unit now numbered 3
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]
    assert d["decisions"]["unit"] == "re-resolved"
    assert m["answer"]["unit"]["unit_no"] == 3 and m["answer"]["unit"]["reading_id"] == "reading-units-2"


def test_a_unit_that_is_gone_after_a_reindex_reaches_the_model_as_the_files_candidates_and_the_old_number_selects_nothing(
        client, syn, monkeypatch):
    cid = _syn_turn(client, syn, monkeypatch)
    # read again with no appraisal of that name: the old number 2 is now the third appraisal
    reread(syn, syn.ver, "reading-units-3", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "identifiers": ["רחוב הארז 4"], "blocks": (0, 1), "pages": (1, 1)},
        {"title": "פרויקט הברוש", "aliases": ["הברוש"], "identifiers": ["רחוב הברוש 2"], "blocks": (2, 5),
         "pages": (2, 3)}])
    contexts._CACHE.clear()
    asked = "השומה שדיברנו עליה כבר אינה בקובץ. לאיזו מהשומות שבו התכוונת?"
    agent, m = _follow(client, cid, monkeypatch, "ומה דמי השכירות?", [
        final(asked, "clarification", clarification=asked, focus=_offer(syn.doc, "U1", "U2"))],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    a = m["answer"]
    # round 15 U6 (KTD1, R2): the server did not ask in the model's place - the model was called, saw the file's appraisals as
    # candidates and a hint that the old unit is gone, and asked in its own words, offering the two
    assert a["status"] == "clarification" and a["markdown"] == asked and len(agent.seen) == 1
    content = agent.seen[0][0]["content"]
    assert _card_titles(content) == ["פרויקט הארז", "פרויקט הברוש"] and "מתחם האשל" in content
    assert a["request"]["located_unit"] is None and a["request"]["document_ids"] == []
    assert [c["unit"]["display"] for c in a["offered_units"]] == ["פרויקט הארז", "פרויקט הברוש"]
    assert "unit" not in a or a["unit"] is None
    # the reply picks among the units offered; the chosen one becomes the conversation's
    agent3, m3 = _follow(client, cid, monkeypatch, "השני", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
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


def test_the_search_only_pin_is_the_default_scope_of_a_search_or_outline_that_names_no_other_document(r9a):
    """Round 15 U6 (KTD3): only the server's pin of the search-only path scopes a call that names no unit; in a model turn
    the focus unit scopes the calls that name it (``test_round15_sequences``)."""
    ws = T.Workspace(ctx=r9a.ctx())
    handle = T.unit_handle_of(ws, r9a.doc, r9a.ver, _reading(r9a, r9a.ver), 2)
    ws.unit_focus = {"handle": handle, "document_id": r9a.doc, "unit_id": None}
    ws.unit_default = True
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


def test_pages_of_the_pinned_units_file_are_read_within_its_unit(r9a):
    ws = T.Workspace(ctx=r9a.ctx())
    handle = T.unit_handle_of(ws, r9a.doc, r9a.ver, _reading(r9a, r9a.ver), 2)
    ws.unit_focus = {"handle": handle, "document_id": r9a.doc, "unit_id": None}
    ws.unit_default = True  # the search-only pin (round 15 U6, KTD3)
    out = T.tool_read(ws, {"pages": {"document": r9a.doc, "from_page": 1, "to_page": 8}})
    assert SECOND in out and FIRST not in out and THIRD not in out and "הטווח נחתך לעמודי היחידה" in out
    refused = T._run_tool(ws, "read", json.dumps({"target": {"pages": {"document": r9a.doc, "from_page": 6, "to_page": 7},
                                                              "source": None, "section": None, "table": None,
                                                              "cursor": None}}))
    assert "אינם ביחידה" in refused and "לא נפתח דבר" in refused


# --- a source labelled with a unit never carries the next unit's blocks (round 9 R10) -----------------------------------

CROSS = "קובץ שומות חוצה סינתטי"
NEXT_TITLE = "פרויקט הברוש"


@pytest.fixture
def cross(office):
    """Three appraisals, a page each; one chunk begins in the second (its rent block 3) and runs on into the third's
    title block (4, on the third's page): its pages are [2, 3] and the second appraisal is page 2."""
    office.doc, office.ver = unit_file(office, office.default_group_id, CROSS, [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", "שווי הנכס 1,000,000 ₪."]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "blocks": ["מתחם האשל", "דמי שכירות חודשיים 5,400 ₪."]},
        {"title": NEXT_TITLE, "aliases": ["הברוש"], "blocks": [NEXT_TITLE, "שווי הנכס 3,000,000 ₪."]}],
        chunks=[(3, 4)])
    return office


def _rent_hit(ws, out: str):
    sid = next(s for s in _sources(out) if "5,400" in ws.sources[s].text)
    return ws.sources[sid]


def test_a_unit_scoped_hit_is_cut_at_its_units_last_block_and_its_pages_stay_inside_the_unit(cross):
    ws = T.Workspace(ctx=cross.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, "האשל"), "מתחם האשל")
    out = T.tool_search(ws, "דמי שכירות חודשיים", None, None, handle)
    s = _rent_hit(ws, out)
    assert s.unit["no"] == 2 and s.unit["pages"] == [2, 2]
    assert NEXT_TITLE not in s.text and "5,400" in s.text  # (the file's contexts note in `out` names every unit)
    assert s.block_start == 3 and s.block_end == 3 and s.page_list == [2]
    assert ws.anchors[s.sid]["pages"] == [2] and ws.anchors[s.sid]["block_end"] == 3


def test_the_same_hit_found_by_a_search_over_the_file_is_cut_the_same_way(cross):
    ws = T.Workspace(ctx=cross.ctx())
    s = _rent_hit(ws, T.tool_search(ws, "דמי שכירות חודשיים", None, 12))
    assert s.unit["no"] == 2 and NEXT_TITLE not in s.text and s.block_end == 3 and s.page_list == [2]


def test_a_unit_scoped_read_stops_at_the_units_last_block_though_the_next_title_is_on_its_last_page(cross):
    with tenant_tx(cross.system) as conn:  # the third's title follows the second's last paragraph on the same page
        conn.execute(text("UPDATE document_blocks SET page = 2 WHERE version_id = :v AND block_index = 4"),
                     {"v": cross.ver})
    ws = T.Workspace(ctx=cross.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, "האשל"), "מתחם האשל")
    out = T.tool_read(ws, {"pages": {"document": handle, "from_page": 2, "to_page": 2}})
    s = ws.sources[_sources(out)[0]]
    assert "5,400" in s.text and NEXT_TITLE not in s.text
    assert s.unit["no"] == 2 and s.block_end == 3 and s.contexts == [2]
    assert ws.anchors[s.sid]["block_end"] == 3 and ws.anchors[s.sid]["pages"] == [2]


# --- a unit located in the turn is the turn's unit, in a file whose units state no title (round 9, wrong-property fix) --

UNTITLED = "קובץ שומות ללא כותרות סינתטי"
U1_VALUE, U2_VALUE, U3_VALUE = "1,100,000", "2,200,000", "3,300,000"


@pytest.fixture
def untitled(office):
    """Three appraisals whose stored units carry no title and no alias: the project names are only in their passages
    (a word of the trade — «מתחם», «שומת מקרקעין» — is in every one). A page each."""
    office.doc, office.ver = unit_file(office, office.default_group_id, UNTITLED, [
        {"blocks": ["מתחם הנרקיס, שומת מקרקעין", f"שווי הנכס {U1_VALUE} ₪."]},
        {"blocks": ["מתחם הצפצפה, שומת מקרקעין", f"שווי הנכס {U2_VALUE} ₪."]},
        {"blocks": ["מתחם הדולב, שומת מקרקעין", f"שווי הנכס {U3_VALUE} ₪."]}])
    return office


def _values_in(out: str) -> set[str]:
    return {v for v in (U1_VALUE, U2_VALUE, U3_VALUE) if v in out}


def test_a_unit_found_by_find_documents_is_listed_not_chosen_and_the_servers_pin_is_the_search_only_default(untitled):
    ws = T.Workspace(ctx=untitled.ctx())
    found = T.tool_find_documents(ws, "הצפצפה")
    handle = _unit_handle(found, "ללא כותרת")
    # round 15 U6 (KTD1): the listing stays, the auto-focus is gone - a unit listed is a unit offered, the model names the one it
    # works in
    assert ws.unit_focus is None and ws.located_unit is None and handle in found and "עמוד 2" in found
    assert "מוגבלים ליחידה" not in found
    assert _values_in(T.tool_search(ws, "שווי הנכס", None, 12, handle)) == {U2_VALUE}  # naming the unit works inside it
    assert _values_in(T.tool_search(T.Workspace(ctx=untitled.ctx()), "שווי הנכס", None, 12)) == {U1_VALUE, U2_VALUE, U3_VALUE}
    # the search-only path pins the unit its question names (``locate_unit``): that unit is the default scope of the calls that
    # name no unit and no other document
    pin = T.Workspace(ctx=untitled.ctx())
    first = entities.first_message(untitled.ctx(), "מה שווי הנכס בפרויקט הצפצפה?")
    assert T.locate_unit(pin, first.unit, first.version_id)
    pinned = pin.unit_focus["handle"]
    assert pin.unit_default and pinned and pin.located_unit["anchor"]["unit_no"] == 2
    searched = T.tool_search(pin, "שווי הנכס", None, 12)  # no unit argument: the search stays in the unit pinned
    assert _values_in(searched) == {U2_VALUE} and f"(היחידה שהשיחה עוסקת בה: {pinned})" in searched
    again = T.tool_search(pin, "הנכס שווי", [untitled.doc], 12)  # its own file: still inside
    assert f"חיפוש בתוך היחידה {pinned}" in again and not _values_in(again) - {U2_VALUE}
    assert f"סעיפי היחידה {pinned} בלבד" in T.tool_outline(pin, untitled.doc)
    assert "הטווח נחתך לעמודי היחידה" in T.tool_read(pin, {"pages": {"document": untitled.doc, "from_page": 1, "to_page": 3}})
    # the call that names a unit of its own is left as it is
    other = T.unit_handle_of(pin, untitled.doc, untitled.ver, _reading(untitled, untitled.ver), 3)
    assert _values_in(T.tool_search(pin, "שווי הנכס", None, 12, other)) == {U3_VALUE}


def test_a_listing_of_several_units_chooses_none_and_a_second_pinned_unit_drops_the_default_scope(untitled):
    ws = T.Workspace(ctx=untitled.ctx())
    T.tool_find_documents(ws, "מתחם")  # the word of every unit
    assert ws.unit_focus is None and ws.located_unit is None
    assert _values_in(T.tool_search(ws, "שווי הנכס", None, 12)) == {U1_VALUE, U2_VALUE, U3_VALUE}
    ws = T.Workspace(ctx=untitled.ctx())
    ctx = untitled.ctx()
    for question in ("מה שווי הנכס בפרויקט הצפצפה?", "מה שווי הנכס בפרויקט הדולב?"):
        found = entities.first_message(ctx, question)
        T.locate_unit(ws, found.unit, found.version_id)
    # two units pinned in one turn: a comparison, not one property
    assert ws.unit_focus is None and ws.located_unit is None and not ws.unit_default
    assert _values_in(T.tool_search(ws, "שווי הנכס", None, 12)) == {U1_VALUE, U2_VALUE, U3_VALUE}


def test_a_first_message_is_looked_up_by_the_passages_when_the_units_state_no_name(untitled):
    ctx = untitled.ctx()
    found = entities.first_message(ctx, "מה שווי הנכס בפרויקט הצפצפה?")
    assert found is not None and found.ambiguity is None and found.unit["unit_no"] == 2
    assert found.unit["document_id"] == untitled.doc and found.unit["pages"] == [2, 2]
    assert entities.first_message(ctx, "מה שווי הנכס בפרויקט הדולב?").unit["unit_no"] == 3
    # no unit word: a question about the file; a word no unit holds; words held by different units
    for question in ["מה שווי הנכס?", "מה שווי הנכס בפרויקט האלון?", "מה שווי הנכס בפרויקט הצפצפה והדולב?",
                     "כמה שומות יש בקובץ?"]:
        assert entities.first_message(ctx, question) is None, question


def test_a_single_appraisal_file_that_holds_the_deciding_words_too_is_a_competitor_of_the_unit(untitled):
    ctx = untitled.ctx()
    question = "מה שווי הנכס בפרויקט הצפצפה?"
    assert entities.first_message(ctx, question).unit["unit_no"] == 2  # alone, the second unit holds the word
    unit_file(untitled, untitled.default_group_id, "שומה בודדת סינתטית", [
        {"blocks": ["שומת מקרקעין, פרויקט הצפצפה", f"שווי הנכס {U1_VALUE} ₪."]}], sha="s" * 64)
    assert entities.first_message(ctx, question) is None  # the question may be about that file
    # a word of another unit only: the single file does not hold it
    assert entities.first_message(ctx, "מה שווי הנכס בפרויקט הדולב?").unit["unit_no"] == 3


def test_a_first_message_naming_a_word_of_one_unit_only_gets_that_unit_as_a_candidate_with_the_passages_basis(
        client, untitled, monkeypatch):
    def answer(items) -> dict:
        sid = _sid(items, U2_VALUE)
        return final(f"שווי הנכס הוא {U2_VALUE} ₪ [{sid}].", focus=_offer(untitled.doc, "U1") | {"subject": "מתחם הצפצפה"},
                     claims=[{"text": f"שווי הנכס הוא {U2_VALUE} ₪", "source_ids": [sid], "basis": "explicit"}])

    # the unit states no name: the one word only its passages hold ranks it, as a candidate with that basis shown (round 15 U6,
    # KTD1) - no pin, no wait for the request analysis; the model searches inside the unit it chooses and declares it
    agent = ScriptedAgent([[call("search", query="שווי הנכס פרויקט הצפצפה", document_ids=None, limit=None, unit="U1")],
                           answer], intent="property")
    cloud(monkeypatch, untitled, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס בפרויקט הצפצפה?")
    assert m["status"] == "done", m
    task = agent.seen[0][0]["content"]
    assert re.search(r"^- U1 «[^»]*» \| עמוד 2", task, re.M) and entities.BASIS_PASSAGES in task
    assert "אלא אם המשתמש מזכיר" not in task  # the block is data, not a scope rule
    searched = agent.tool_outputs(1)[0]
    assert _values_in(searched) == {U2_VALUE} and "חיפוש בתוך היחידה U1" in searched
    a = m["answer"]
    assert a["status"] == "answered" and U2_VALUE in a["markdown"]
    assert a["unit"]["unit_no"] == 2 and a["unit"]["document_id"] == untitled.doc


def test_a_general_question_over_an_untitled_file_is_not_scoped_to_a_unit(client, untitled, monkeypatch):
    agent = ScriptedAgent([[call("search", query="שווי הנכס", document_ids=None, limit=None, unit=None)],
                           final("לא נבחרה שומה.", "not_found")])
    cloud(monkeypatch, untitled, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס?")
    assert m["status"] == "done", m
    assert resolve.CANDIDATES_HEAD not in agent.seen[0][0]["content"]  # no word names a unit: no candidates block
    assert _values_in(agent.tool_outputs(1)[0]) == {U1_VALUE, U2_VALUE, U3_VALUE}


def test_a_claim_about_the_located_unit_resting_on_another_units_value_is_left_to_the_judge_with_an_ownership_fact(untitled):
    from app.chat import verify

    ws = T.Workspace(ctx=untitled.ctx())
    ws.requirements = verify.TurnRequirements()
    found = entities.first_message(untitled.ctx(), "מה שווי הנכס בפרויקט הצפצפה?")  # the unit a pin or a declaration names
    T.locate_unit(ws, found.unit, found.version_id)  # the turn's unit: the second
    sids = {}
    for n, value in ((2, U2_VALUE), (3, U3_VALUE)):  # each read through its own unit handle
        handle = T.unit_handle_of(ws, untitled.doc, untitled.ver, _reading(untitled, untitled.ver), n)
        sids[n] = next(s for s in _sources(T.tool_search(ws, "שווי הנכס", None, 12, handle)) if value in ws.sources[s].text)
    answer = (f"שווי הנכס הוא {U2_VALUE} ₪ [{sids[2]}].\n\nשווי הנכס הוא {U3_VALUE} ₪ [{sids[3]}].")
    units = verify.split_units(answer)
    problems = verify.deterministic(units, ws, "מה שווי הנכס בפרויקט הצפצפה?")
    # round 19 U2, R6: units with no stored identity may be one property, so absence of identity is not "another property": nothing is
    # removed; the claim on unit 3's value rides to the judge with the neutral ownership fact, the located unit's own claim with none
    assert not [p for p in problems if p.failure_kind == "wrong_subject"], [p.as_dict() for p in problems]
    question = "מה שווי הנכס בפרויקט הצפצפה?"
    assert verify._context_check(units[0], ws, question).facts == []
    assert len(verify._context_check(units[1], ws, question).facts) == 1
    assert "<ownership" not in verify._ownership_note(units[0], ws)
    assert "<ownership" in verify._ownership_note(units[1], ws)
    # without a located unit nothing says which of the two is the asked property's: neither is removed here
    bare = T.Workspace(ctx=untitled.ctx())
    bare.requirements = verify.TurnRequirements()
    bare.sources, bare.contexts = ws.sources, ws.contexts
    assert not [p for p in verify.deterministic(units, bare, "מה שווי הנכס בפרויקט הצפצפה?")
                if p.failure_kind == "wrong_subject"]


def test_a_first_message_prefers_the_name_a_unit_states_and_a_tie_is_still_the_servers_clarification(office, r9a):
    ingest(office, R9B)
    ctx = office.ctx()
    named = entities.first_message(ctx, f"מה שווי הנכס ב{UNITS[1]['aliases'][0]}?")  # a name only one unit holds
    assert named.ambiguity is None and (named.unit["document_id"], named.unit["unit_no"]) == (r9a.doc, 2)
    whole = entities.first_message(ctx, "מה דמי השכירות החודשיים במתחם הגפן ב?")  # the whole name of one unit
    assert whole.ambiguity is None and whole.unit["title"] == contexts.name_key("מתחם הגפן ב")
    tie = entities.first_message(ctx, SIMILAR_QUESTION)  # two units alike: the question between them
    assert tie.unit is None and [d.title for d in tie.ambiguity[0].documents] == ["מתחם הגפן א", "מתחם הגפן ב"]
    assert [d.title for d in entities.first_message_ambiguity(ctx, SIMILAR_QUESTION)[0].documents] == [
        "מתחם הגפן א", "מתחם הגפן ב"]


LONG_NAMES = ["בית הדקל הירוק", "בית האלון הישן", "בית השקד הלבן"]


@pytest.fixture
def long_named(office):
    """Three appraisals whose names are three words long and share one word; a town word only the second one's prose holds."""
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ שומות בשמות ארוכים סינתטי", [
        {"title": LONG_NAMES[0], "aliases": [LONG_NAMES[0]], "blocks": [LONG_NAMES[0], "הנכס ברחוב הרימון, שווי 1,100,000 ₪."]},
        {"title": LONG_NAMES[1], "aliases": [LONG_NAMES[1]], "blocks": [LONG_NAMES[1], "הנכס בכפר ורדון, שווי 2,200,000 ₪."]},
        {"title": LONG_NAMES[2], "aliases": [LONG_NAMES[2]], "blocks": [LONG_NAMES[2], "הנכס ברחוב התמר, שווי 3,300,000 ₪."]}])
    return office


def test_one_shared_word_of_long_names_is_no_ambiguity_and_the_passages_locate_the_unit(long_named):
    ctx = long_named.ctx()
    question = "מה פרטי הרישום של הדירה בכפר ורדון לפי נסח הבית המשותף?"
    assert entities.first_message_ambiguity(ctx, question) is None
    found = entities.first_message(ctx, question, passages=True)
    assert found is not None and found.ambiguity is None and found.unit["unit_no"] == 2
    # the same word with a second word of the name stated is still a name
    named = entities.first_message(ctx, "מה השווי של בית הדקל?")
    assert named.ambiguity is None and named.unit["unit_no"] == 1


def test_two_words_of_names_that_differ_by_one_word_still_get_the_clarification(office, r9a):
    ingest(office, R9B)
    tie = entities.first_message(office.ctx(), SIMILAR_QUESTION)  # "מתחם הגפן": 2 words stated, 1 unsaid
    assert tie.unit is None and [d.title for d in tie.ambiguity[0].documents] == ["מתחם הגפן א", "מתחם הגפן ב"]


@pytest.fixture
def town_words(office):
    """Three untitled appraisals; a town word held by one unit each, and a word that the first and third prose hold."""
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ שומות בלי כותרות סינתטי", [
        {"blocks": ["שומת מקרקעין, העיר אילנית", "התפוסה יורדת בתרחיש הרגישות."]},
        {"blocks": ["שומת מקרקעין, העיר ורדית", "התפוסה נשארת בתרחיש הרגישות."]},
        {"blocks": ["שומת מקרקעין, העיר תמרית", "התפוסה יורדת בתרחיש הרגישות."]}])
    return office


def test_a_unique_word_locates_its_unit_though_an_incidental_word_is_held_by_other_units(town_words):
    ctx = town_words.ctx()
    found = entities.first_message(ctx, "בניתוח הרגישות בשומת ורדית, מה השווי כשהתפוסה יורדת?")
    assert found is not None and found.unit["unit_no"] == 2  # no unit holds both; the town word is held by one only
    assert entities.first_message(ctx, "בניתוח הרגישות בשומת ורדית, מה השווי?").unit["unit_no"] == 2
    assert entities.first_message(ctx, "בשומת אילנית, מה השווי כשהתפוסה יורדת?").unit["unit_no"] == 1  # the first rule


def test_unique_words_of_two_different_units_locate_nothing(town_words):
    ctx = town_words.ctx()
    assert entities.first_message(ctx, "בניתוח הרגישות בשומת ורדית ובשומת תמרית, מה השווי?") is None
    assert entities.first_message(ctx, "בשומת ורדית, מה השווי כשהתפוסה נשארת, העיר אילנית?") is None


@pytest.fixture
def deep_words(office):
    """Three untitled appraisals of two pages each; the town word of each is on its second page only (deep in its body);
    a word held by the first and third units opens the first page."""
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ שומות עמוקות סינתטי", [
        {"blocks": ["שומת מקרקעין, התפוסה יורדת"], "deep": ["סיכום, העיר אילנית"]},
        {"blocks": ["שומת מקרקעין, התפוסה נשארת"], "deep": ["סיכום, העיר ורדית"]},
        {"blocks": ["שומת מקרקעין, התפוסה יורדת"], "deep": ["סיכום, העיר תמרית"]}])
    return office


def test_a_unique_word_that_is_only_deep_in_a_units_body_locates_nothing(deep_words):
    # no unit holds both words: the unique one decides, and it is not in the unit's opening
    assert entities.first_message(deep_words.ctx(), "מה השווי כשהתפוסה יורדת, העיר ורדית?") is None


def test_a_unique_word_on_the_opening_page_of_a_unit_locates_it(town_words):
    found = entities.first_message(town_words.ctx(), "מה השווי כשהתפוסה יורדת, העיר ורדית?")
    assert found is not None and found.unit["unit_no"] == 2


def test_a_word_that_is_in_no_name_but_a_word_of_the_trade_asks_no_clarification(office):
    unit_file(office, office.default_group_id, "קובץ שומות דיור סינתטי", [
        {"title": "דיור מוגן", "aliases": ["דיור מוגן"], "blocks": ["דיור מוגן", "הנכס ברחוב הרימון."]},
        {"title": "דיור צעיר", "aliases": ["דיור צעיר"], "blocks": ["דיור צעיר", "הנכס ברחוב התמר."]}])
    assert entities.first_message_ambiguity(office.ctx(), "מה השווי של יחידת דיור ברחוב הרימון?") is None


def test_a_unique_word_of_one_unit_locates_nothing_when_the_questions_other_words_are_a_single_appraisal_files(town_words):
    unit_file(town_words, town_words.default_group_id, "שומה בודדת סינתטית", [
        {"blocks": ["שומת מקרקעין, רחוב הסביון, שכונת הגבעה", "הנכס ברחוב הסביון בשכונת הגבעה."]}], sha="s" * 64)
    ctx = town_words.ctx()
    # "ורדית" (an incidental word here) is held by one unit only, and no unit holds it with "יורדת"; the street and the neighbourhood are the single file's
    assert entities.first_message(ctx, "בניתוח הרגישות ברחוב הסביון שכונת הגבעה, מה השווי כשהתפוסה יורדת, העיר ורדית?") is None
    # the word of the unit alone, without the single file's words, still locates it
    assert entities.first_message(ctx, "בניתוח הרגישות בשומת ורדית, מה השווי?").unit["unit_no"] == 2
    # a word that only the single file holds is no unit's
    assert entities.first_message(ctx, "בניתוח הרגישות ברחוב הסביון, מה השווי?") is None


def test_a_unit_kept_from_the_conversation_is_never_replaced_by_one_the_turn_locates(untitled):
    ws = T.Workspace(ctx=untitled.ctx())
    handle = T.unit_handle_of(ws, untitled.doc, untitled.ver, _reading(untitled, untitled.ver), 3)
    kept = {"handle": handle, "document_id": untitled.doc, "display": "x"}
    ws.unit_focus = dict(kept)
    ws.unit_default = True
    found = entities.first_message(untitled.ctx(), "מה שווי הנכס בפרויקט הצפצפה?")
    assert not T.locate_unit(ws, found.unit, found.version_id)
    assert ws.unit_focus == kept and ws.located_unit is None
    assert _values_in(T.tool_search(ws, "שווי הנכס", None, 12)) == {U3_VALUE}


def test_a_question_with_no_word_that_can_tell_appraisals_apart_is_looked_up_in_one_query(untitled, statements):
    statements.clear()
    assert entities.first_message(untitled.ctx(), "מה שווי הנכס?") is None  # a kind of value, nothing to name a unit by
    assert len(statements) == 1, statements


# --- a file of one subject unit states its identity for the judge, and nothing else about it changes ----------------

ONE_LABEL = "שומת מקרקעין מלאה · רחוב הצבר 5"


def test_the_sources_of_a_single_appraisal_file_carry_its_label_as_subject_shown_to_the_model_and_no_context_or_unit(office):
    office.doc, office.ver = unit_file(office, office.default_group_id, "דוח שומה בודד סינתטי", [
        {"title": "שומת מקרקעין מלאה", "kind": "appraisal", "identifiers": ["רחוב הצבר 5"],
         "blocks": ["שומת מקרקעין מלאה", "הננו שמים את שווי הנכס ב-4,200,000 ₪ ללא מע\"מ."]}])
    assert contexts_label(office) == ONE_LABEL
    ws = T.Workspace(ctx=office.ctx())
    found = [ws.sources[s] for s in _sources(T.tool_search(ws, "שווי הנכס", None, 5))]
    assert found
    read = ws.sources[_sources(T.tool_read(ws, {"pages": {"document": office.doc, "from_page": 1, "to_page": 1}}))[0]]
    assert read.kind == "context"
    for s in [*found, read]:
        assert s.subject == ONE_LABEL
        assert s.context is None and not s.contexts and s.unit is None and "unit" not in s.tags
        assert "context" not in s.public() and "unit" not in s.public()
    for s in [*found, read]:  # the model sees the identity too, as the file's subject, never as a context
        rendered = T._render_source(s)
        assert f'subject="{ONE_LABEL}"' in rendered and 'context="' not in rendered


def contexts_label(office) -> str:
    with tenant_tx(office.system) as conn:
        cx = contexts.of_version(conn, office.ver, READING)
    assert not cx.multi and cx.subjects == [1]
    return cx.label(1)


def test_the_sources_of_a_file_of_several_appraisals_keep_their_context_and_no_subject(cross):
    ws = T.Workspace(ctx=cross.ctx())
    found = [ws.sources[s] for s in _sources(T.tool_search(ws, "שווי הנכס", None, 5))]
    read = ws.sources[_sources(T.tool_read(ws, {"pages": {"document": cross.doc, "from_page": 1, "to_page": 3}}))[0]]
    assert found and read.context
    for s in [*found, read]:
        assert s.subject == "" and s.context and s.unit is not None


# --- round 15 U2: a unit search stays inside the unit, compact hits (R8, R9; KTD10) -----------------------------------------

SHARED = "קובץ שלוש שומות עם מילים משותפות סינתטי"
RENT = "דמי שכירות חודשיים"


def _three(office, certainty3: str = "certain") -> tuple[str, str]:
    """Three appraisals whose rent sentences share the same words. Only the first states "אלמוגים" and only the second
    states "ספינות"; the third's opening edge is ``certainty3``."""
    return unit_file(office, office.default_group_id, SHARED, [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", f"{RENT} 1,100 ₪.", "בחצר האלמוגים גדלים."]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "blocks": ["מתחם האשל", f"{RENT} 5,400 ₪.", "בנמל ספינות רבות."]},
        {"title": NEXT_TITLE, "aliases": ["הברוש"], "certainty": certainty3,
         "blocks": [NEXT_TITLE, f"{RENT} 3,300 ₪.", "בים גדול שקט."]}], sha="s" * 64)


def _handle_of(ws, office, doc: str, ver: str, number: int) -> str:
    return T.unit_handle_of(ws, doc, ver, READING, number)


def test_a_search_in_the_second_of_three_certain_units_returns_only_unit_two_though_every_unit_matches(office):
    doc, ver = _three(office)
    ws = T.Workspace(ctx=office.ctx())
    handle = _handle_of(ws, office, doc, ver, 2)
    out = T.tool_search(ws, RENT, None, None, handle)
    sids = _sources(out)
    assert sids and "5,400" in out and "1,100" not in out and "3,300" not in out
    assert {ws.sources[s].unit["no"] for s in sids} == {2}
    assert "גבול היחידה אינו ודאי" not in out


def test_across_a_probable_edge_the_neighbour_up_to_the_next_certain_edge_is_searched_and_each_hit_keeps_its_own_unit(office):
    doc, ver = _three(office, certainty3="probable")
    ws = T.Workspace(ctx=office.ctx())
    handle = _handle_of(ws, office, doc, ver, 2)
    out = T.tool_search(ws, RENT, None, None, handle)
    assert "1,100" not in out and "5,400" in out and "3,300" in out
    assert "גבול היחידה אינו ודאי" in out and _handle_of(ws, office, doc, ver, 3) in out  # the extension is reported
    by_amount = {a: ws.sources[s].unit["no"] for s in _sources(out) for a in ("5,400", "3,300") if a in ws.sources[s].text}
    assert by_amount == {"5,400": 2, "3,300": 3}  # the neighbour's passage is labelled with the neighbour, not with unit 2
    for sid in _sources(out):
        assert ws.sources[sid].tags["unit"] == _handle_of(ws, office, doc, ver, ws.sources[sid].unit["no"])


def test_no_hit_in_a_unit_with_a_probable_edge_reports_the_unit_and_the_edge_and_returns_no_passage_of_the_file(office):
    doc, ver = _three(office, certainty3="probable")
    ws = T.Workspace(ctx=office.ctx())
    handle = _handle_of(ws, office, doc, ver, 2)
    out = T.tool_search(ws, "אלמוגים", None, None, handle)  # only unit 1 states it
    assert _sources(out) == [] and "אלמוגים" in out and "היחידה נחקרה" in out and "לא חיפשתי בשאר הקובץ" in out
    assert "הרחבתי" not in out and "גבול היחידה אינו ודאי" in out
    assert _handle_of(ws, office, doc, ver, 3) in out  # the uncertain edge's neighbour: the model may search it on purpose
    assert [s for s in ws.sources.values() if not s.is_listing] == []


def test_a_table_chunk_that_starts_in_the_unit_and_runs_past_it_is_cut_at_the_units_last_block(cross):
    with tenant_tx(cross.system) as conn:  # the chunk over blocks 3-4 (the rent, then the next title) is a table's
        conn.execute(text("UPDATE chunks SET kind = 'table' WHERE version_id = :v AND block_start = 3 AND block_end = 4"),
                     {"v": cross.ver})
    ws = T.Workspace(ctx=cross.ctx())
    handle = _unit_handle(T.tool_find_documents(ws, "האשל"), "מתחם האשל")
    out = T.tool_search(ws, "דמי שכירות חודשיים", None, None, handle)
    s = _rent_hit(ws, out)
    assert s.kind == "table" and s.unit["no"] == 2
    assert NEXT_TITLE not in s.text and "5,400" in s.text
    assert s.block_start == 3 and s.block_end == 3 and s.page_list == [2]


def test_the_rows_of_a_table_past_the_units_contexts_are_cut_and_a_chunk_wholly_past_them_is_dropped():
    rows = [{"cells": ["א", "1"]}, {"cells": ["ב", "2"]}, {"cells": ["ג", "3"]}]
    groups = [{"context": 2, "first": 1, "last": 2}, {"context": 3, "first": 3, "last": 3}]
    chunk = "כותרת | ערך\nא | 1\nב | 2\nג | 3"
    assert T._rows_in_scope(chunk, rows, groups, {2, 3}) == chunk
    cut = T._rows_in_scope(chunk, rows, groups, {2})
    assert cut.startswith("כותרת | ערך\nא | 1\nב | 2") and "ג | 3" not in cut and T.MSG_TABLE_CUT in cut
    only_third = T._rows_in_scope(chunk, rows, groups, {3})
    assert "ג | 3" in only_third and "א | 1" not in only_third and "ב | 2" not in only_third
    assert T._rows_in_scope(chunk, rows, groups, {1}) is None
    assert T._rows_in_scope(chunk, [], [], {2}) == chunk  # a table stored without rows is not cut


LONG = "ארוך"


@pytest.fixture
def long_hit(office):
    """One unit whose rent sentence sits in the middle of a 2,400-character block (a marker at each end)."""
    filler = "מילה " * 200
    body = f"ראשית-המקטע {filler}{RENT} 5,400 ₪. {filler}סוף-המקטע"
    office.doc, office.ver = unit_file(office, office.default_group_id, LONG, [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", "שורה קצרה."]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "blocks": ["מתחם האשל", body]}], sha="l" * 64)
    return office


def test_a_scoped_hit_is_clipped_around_the_matched_terms_with_a_pointer_and_read_returns_the_whole_passage(long_hit):
    ws = T.Workspace(ctx=long_hit.ctx())
    handle = T.unit_handle_of(ws, long_hit.doc, long_hit.ver, READING, 2)
    out = T.tool_search(ws, RENT, None, None, handle)
    s = ws.sources[next(x for x in _sources(out) if "5,400" in ws.sources[x].text)]
    assert len(s.text) <= T.SCOPED_PASSAGE_CHARS and "5,400" in s.text
    assert "ראשית-המקטע" not in s.text and "סוף-המקטע" not in s.text
    assert f"read(source={s.sid})" in out
    whole = T.tool_read(ws, {"source": s.sid})
    assert "ראשית-המקטע" in whole and "סוף-המקטע" in whole and "5,400" in whole


def test_a_hit_of_a_call_that_names_no_unit_keeps_the_longer_clip(long_hit):
    ws = T.Workspace(ctx=long_hit.ctx())
    out = T.tool_search(ws, RENT, None, None)
    s = ws.sources[next(x for x in _sources(out) if "5,400" in ws.sources[x].text)]
    assert T.SCOPED_PASSAGE_CHARS < len(s.text) <= 1700 and f"read(source={s.sid})" not in out


# --- round 17 U6: continuation units follow the subject unit they belong to (KTD6 item 5) -----------------------------------

def _continuations(db, extra_units: list[dict] | None = None, sha: str = "e1" * 32):
    """A named appraisal followed by two untitled appendix units, then another named appraisal with one untitled appendix."""
    a = make_office(db, G, "a@example.test")
    units = [
        {"title": "מתחם הברוש", "aliases": ["הברוש"], "blocks": ["מתחם הברוש", "שווי הנכס 2,000,000 ₪."]},
        {"title": None, "kind": "appendix", "blocks": ["טבלת חישוב שנספחה", "חישוב הצמדה 120 ₪."]},
        {"title": None, "kind": "appendix", "blocks": ["נתוני עזר שנספחו", "מקדם 0.9."]},
        {"title": "מתחם האלון", "aliases": ["האלון"], "blocks": ["מתחם האלון", "שווי הנכס 3,000,000 ₪."]},
        {"title": None, "kind": "appendix", "blocks": ["נספח לאלון", "שטח מדוד 88 מטר."]},
        *(extra_units or [])]
    unit_file(a, a.default_group_id, "קובץ מתחמים סינתטי", units, sha=sha)
    return a


def _lines(out: str, marker: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.lstrip().startswith(marker)]


def test_find_documents_lists_the_untitled_units_after_a_named_unit_as_its_continuations_never_as_subjects(db):
    a = _continuations(db)
    ws = T.Workspace(ctx=a.ctx())
    out = T.tool_find_documents(ws, "הברוש")
    subjects = [ln for ln in out.splitlines() if re.match(r"\s+- U\d+ \|", ln)]
    assert len(subjects) == 1 and "מתחם הברוש" in subjects[0]
    subject = re.match(r"\s+- (U\d+)", subjects[0]).group(1)
    cont = _lines(out, "↳")
    assert len(cont) == 2 and all(f"המשך של {subject}" in ln for ln in cont), out
    assert all(ln.lstrip().startswith("↳ U") for ln in cont)
    # the continuations' handles are units the model was shown: they may be declared in a clarification
    for ln in cont:
        assert re.search(r"U\d+", ln).group(0) in ws.listed_units
    assert subject in ws.listed_units
    assert "מתחם האלון" not in out and "נספח לאלון" not in out  # the unit with an identity of its own ends the chain


def test_a_continuation_is_not_counted_among_the_several_units_that_need_a_question_to_the_user(db):
    a = _continuations(db)
    out = T.tool_find_documents(T.Workspace(ctx=a.ctx()), "הברוש")
    assert T.MSG_SEVERAL_UNITS not in out  # one subject unit, however many continuations it has


def test_a_unit_with_its_own_identity_after_a_subject_unit_is_not_its_continuation(db):
    a = _continuations(db)
    out = T.tool_find_documents(T.Workspace(ctx=a.ctx()), "האלון")
    subjects = [ln for ln in out.splitlines() if re.match(r"\s+- U\d+ \|", ln)]
    assert len(subjects) == 1 and "מתחם האלון" in subjects[0]
    cont = _lines(out, "↳")
    assert len(cont) == 1 and "נספח" in cont[0], out  # only its own appendix; the Brosh units before it are not listed
    assert "מתחם הברוש" not in out


def test_the_last_named_units_continuation_ends_with_the_file(db):
    a = make_office(db, G, "a@example.test")
    unit_file(a, a.default_group_id, "קובץ מתחמים סינתטי", [
        {"title": "מתחם הברוש", "aliases": ["הברוש"], "blocks": ["מתחם הברוש", "שווי הנכס 2,000,000 ₪."]},
        {"title": None, "kind": "appendix", "blocks": ["נתוני עזר", "מקדם 0.9."]}], sha="e2" * 32)
    out = T.tool_find_documents(T.Workspace(ctx=a.ctx()), "הברוש")
    assert len(_lines(out, "↳")) == 1


def test_a_titled_report_with_an_identifier_of_its_own_is_never_a_continuation(db):
    a = make_office(db, G, "a@example.test")
    unit_file(a, a.default_group_id, "קובץ מתחמים סינתטי", [
        {"title": "מתחם הברוש", "aliases": ["הברוש"], "blocks": ["מתחם הברוש", "שווי הנכס 2,000,000 ₪."]},
        {"title": None, "kind": "appraisal", "identifiers": ["גוש 7000 חלקה 3"], "blocks": ["חוות דעת נוספת", "שווי 1,000,000 ₪."]}],
        sha="e3" * 32)
    out = T.tool_find_documents(T.Workspace(ctx=a.ctx()), "הברוש")
    assert not _lines(out, "↳")


def test_a_unit_that_is_a_continuation_is_not_reported_as_a_comparable_of_its_subject_file(db):
    a = make_office(db, G, "a@example.test")
    unit_file(a, a.default_group_id, "קובץ מתחמים סינתטי", [
        {"title": "מתחם הברוש", "aliases": ["הברוש"], "blocks": ["מתחם הברוש", "שווי הנכס 2,000,000 ₪."]},
        {"title": None, "kind": "appendix", "blocks": ["נספח", "הברוש הוזכר בנספח: מקדם 0.9."]}], sha="e4" * 32)
    out = T.tool_find_documents(T.Workspace(ctx=a.ctx()), "הברוש")
    assert len(_lines(out, "↳")) == 1 and "רק כנכס השוואה" not in out


def test_units_matching_returns_the_continuations_flagged_with_their_subject_unit_and_apart_from_the_subjects(db):
    from app.platform.search import units_matching

    a = _continuations(db)
    with tenant_tx(a.ctx()) as conn:
        m = units_matching(conn, "הברוש")
    assert [u["unit_no"] for u in m.units] == [1]
    assert [(u["unit_no"], u["continuation_of"], u["matched_by"]) for u in m.continuations] == [
        (2, 1, "continuation"), (3, 1, "continuation")]
    assert m.comparables == [] and m.documents[0]["units"][0]["unit_no"] == 1


def test_units_continuing_stops_at_a_unit_with_an_identity_a_subject_or_front_matter():
    from types import SimpleNamespace

    from app.platform.search import units_continuing

    def unit(n, kind="appraisal", title=None, aliases=(), shown=()):
        return SimpleNamespace(unit_no=n, kind=kind, title=title, aliases=aliases, identifiers={"shown": list(shown)})

    units = [unit(1, title="א"), unit(2, kind="appendix", title="נספח ב"), unit(3), unit(4, title="ב"), unit(5),
             unit(6, kind="front_matter"), unit(7), unit(8, title="ג"), unit(9, shown=["גוש 1 חלקה 2"])]
    assert units_continuing(units, [1]) == {2: 1, 3: 1}  # a titled appendix and an untitled part; the titled report ends it
    assert units_continuing(units, [1, 4]) == {2: 1, 3: 1, 5: 4}  # front matter opens the next report
    assert units_continuing(units, [8]) == {}  # the next unit states an identifier of its own
    assert units_continuing(units, [3, 4]) == {5: 4}  # unit 4 is a subject: it ends the chain of 3 and has its own
