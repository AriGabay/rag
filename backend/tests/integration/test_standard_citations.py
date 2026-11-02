"""A standard is cited by its number, its edition and its section, and the citation opens at that place (round 10 U8:
R30-R32; KTD13; AE9).

A source read from a standard carries its kind and its identity (number, name, edition, effective date, section) in its
public payload, and renders them in its title and location, so the verifier's number check finds a cited section or
edition; the stored anchor snapshots them (version 2; a version-1 snapshot stays readable) and opens at the right page
and block of a PDF, and at the right paragraph of a DOCX; the answer's context slots take the kind from the real
source. The historical-appraisal scenario shows the model both editions with their dates and whether their order is
known. Synthetic data only."""

from __future__ import annotations

import json
import re
from uuid import UUID

import pytest
from sqlalchemy import text

from app.chat import anchors, engine, verify
from app.chat import api as chat_api
from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.professional_files import APPROACH, professional_file, standard_blocks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
THREE = "על השמאי להסתמך על שלוש עסקאות השוואה לפחות."
FIVE = "על השמאי להסתמך על חמש עסקאות השוואה לפחות."
PAGE_W, PAGE_H = 600.0, 800.0
BOX = [60, 200, 540, 230]


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד א", "admin-a@example.test")


def standard(office, number="98.1", **kw):
    title = kw.pop("title", "תקן מדידה")
    sha = kw.pop("sha", None)
    return professional_file(office, office.default_group_id, standard_blocks(number, **kw), title=title,
                             source_kind="standard", sha=sha)


def workspace(office, intent="professional") -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx())
    ws.intent = intent
    return ws


def run(ws, name, **args) -> str:
    return T.run_tool(ws, name, json.dumps(args, ensure_ascii=False))


def cited(ws, doc, words=APPROACH) -> T.Source:
    return next(s for s in ws.sources.values() if str(s.document_id) == str(doc) and words in s.text)


def with_geometry(office, version) -> None:
    """Give the synthetic PDF the pages' frames and the blocks their boxes (as the reader stores them)."""
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE pages SET mediabox = CAST(:mb AS jsonb), cropbox = CAST(:mb AS jsonb), rotation = 0,"
                          " display_width = :w, display_height = :h WHERE version_id = :v"),
                     {"v": version, "mb": json.dumps([0, 0, PAGE_W, PAGE_H]), "w": PAGE_W, "h": PAGE_H})
        conn.execute(text("UPDATE document_blocks SET bbox = CAST(:b AS jsonb) WHERE version_id = :v"),
                     {"v": version, "b": json.dumps(BOX)})


# --- the payload -----------------------------------------------------------------------------------------------

def test_a_standard_source_carries_its_kind_and_identity_in_its_public_payload(office):
    std, _ = standard(office)
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    pub = chat_api._public_source(cited(ws, std))
    assert pub["source_kind"] == "standard"
    assert pub["standard"] == {"number": "98.1", "title": "מדידת שטחים סינתטית", "edition": "גרסה 2.0",
                               "effective": "05.10.2025", "section": "2.1", "section_title": "גישת ההשוואה"}
    # the identity is the source's title and location, not only a field beside them
    assert pub["title"] == "תקן 98.1 (גרסה 2.0, בתוקף מ-05.10.2025)"
    assert pub["location"] == "סעיף 2.1, עמוד 3"


def test_an_appraisal_source_is_an_appraisal_with_no_standard(office):
    doc, _ = professional_file(office, office.default_group_id, [("paragraph", None, [], APPROACH, 1)],
                               title="שומה", source_kind="appraisal")
    ws = workspace(office, "property")
    run(ws, "search", query=APPROACH)
    pub = chat_api._public_source(cited(ws, doc))
    assert pub["source_kind"] == "appraisal" and pub["standard"] is None


def test_a_section_read_carries_the_identity_too_and_a_standard_without_stated_data_falls_back_to_its_title(office):
    std, _ = standard(office)
    bare, _ = professional_file(office, office.default_group_id,
                                [("paragraph", None, [], "מסמך סינתטי", 1), ("paragraph", None, [], APPROACH + " א", 1)],
                                title="חוזר ללא פרטים", source_kind="professional")
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    handle = next(h for h, d in ws.handles.items() if d.get("psection") is not None and d["document_id"] == std)
    run(ws, "read", target={"source": None, "pages": None, "section": handle, "table": None, "cursor": None})
    read = next(s for s in ws.sources.values() if s.status and s.standard)
    assert read.source_kind == "standard" and read.standard["section"] == "2.1" and read.title.startswith("תקן 98.1")
    other = chat_api._public_source(cited(ws, bare, APPROACH + " א"))
    assert other["source_kind"] == "professional"
    assert other["standard"]["number"] is None and other["title"].endswith("חוזר ללא פרטים")


def test_the_verifiers_head_of_a_cited_standard_holds_its_number_edition_and_section(office):
    std, _ = standard(office)
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    sid = cited(ws, std).sid
    head, body, _ = verify._source_parts(ws, sid)
    assert "98.1" in head and "גרסה 2.0" in head and "סעיף 2.1" in head and "עמוד 3" in head
    assert APPROACH in body


# --- the anchor ------------------------------------------------------------------------------------------------

def test_the_anchor_of_a_standard_in_a_pdf_opens_its_page_and_block_and_snapshots_the_identity(office):
    std, ver = standard(office)
    with_geometry(office, ver)
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    src = cited(ws, std)
    assert ws.anchors[src.sid]["standard"]["number"] == "98.1"
    payload = {"sources": [chat_api._public_source(src)]}
    anchors.attach(ws, payload)
    a = payload["sources"][0]["anchor"]
    assert a["v"] == anchors.SNAPSHOT_VERSION == 2 and a["precision"] == "block"
    assert a["block_start"] == a["block_end"] == src.block_start and [p["page"] for p in a["pages"]] == [3]
    assert a["pages"][0]["rects"] == [[0.1, 0.25, 0.9, 0.2875]]
    assert a["standard"]["number"] == "98.1" and a["standard"]["section"] == "2.1"
    assert "סעיף" in a["location"]["label"] and "עמוד 3" in a["location"]["label"]
    # the opening of that block is the one the source panel shows
    with tenant_tx(office.ctx()) as conn:
        text_ = conn.execute(text("SELECT text FROM document_blocks WHERE version_id = :v AND block_index = :b"),
                             {"v": UUID(str(ver)), "b": a["block_start"]}).scalar_one()
    assert APPROACH in text_


def test_the_anchor_of_a_standard_in_a_docx_opens_its_paragraph_with_the_section_path(office):
    std, ver = standard(office)
    with tenant_tx(office.system) as conn:  # the same blocks as a DOCX has them: no pages
        conn.execute(text("UPDATE document_versions SET mime_type = :m, filename = 'f.docx' WHERE id = :v"),
                     {"v": ver, "m": DOCX})
        conn.execute(text("UPDATE document_blocks SET page = NULL WHERE version_id = :v"), {"v": ver})
        conn.execute(text("UPDATE chunks SET page_list = NULL WHERE version_id = :v"), {"v": ver})
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    src = cited(ws, std)
    assert src.location == "סעיף 2.1" and src.standard["section"] == "2.1"
    payload = {"sources": [chat_api._public_source(src)]}
    anchors.attach(ws, payload)
    a = payload["sources"][0]["anchor"]
    assert a["precision"] == "structured" and a["pages"] == [] and a["block_start"] == src.block_start
    assert a["structured"]["section_path"] == ["2. שיטות", "2.1 גישת ההשוואה"] and APPROACH in a["structured"]["text"]
    assert a["standard"]["edition"] == "גרסה 2.0" and a["v"] == 2


def test_a_version_1_snapshot_is_still_readable(office):
    old = {"v": 1, "precision": "block", "document_id": "d", "version_id": "v", "pages": [], "location": None}
    assert anchors.standard_of(old) is None
    assert anchors.standard_of(old | {"v": 2, "standard": {"number": "98.1"}}) == {"number": "98.1"}
    assert anchors.anchored_documents({"sources": [{"anchor": old}]}) == {"d"}


# --- the answer ------------------------------------------------------------------------------------------------

def test_an_answer_citing_a_standard_stores_its_kind_and_identity_in_the_payload_and_the_context(client, office,
                                                                                           monkeypatch):
    std, ver = standard(office)
    agent = ScriptedAgent([[call("search", query=APPROACH, document_ids=None, limit=None)],
                           final(f"{APPROACH} [S1]", documents=[std],
                                 claims=[{"text": APPROACH, "source_ids": ["S1"], "basis": "explicit"}])],
                          intent="professional")
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דורשת גישת ההשוואה בתקן?")
    assert m["status"] == "done", m
    (source,) = [s for s in m["answer"]["sources"] if s["id"] == "S1"]
    assert source["source_kind"] == "standard" and source["standard"]["number"] == "98.1"
    assert source["anchor"]["standard"]["section"] == "2.1" and source["anchor"]["v"] == 2
    with tenant_tx(office.system) as conn:
        context = conn.execute(text("SELECT answer->'context' FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
    (slot,) = context["sources"]
    assert slot["source_kind"] == "standard" and slot["document_id"] == std
    assert slot["standard"]["number"] == "98.1" and slot["standard"]["edition"] == "גרסה 2.0"


# --- editions, dates and a historical appraisal ----------------------------------------------------------------------

def test_the_model_is_shown_both_editions_with_their_dates_for_an_appraisal_older_than_the_newer_one(client, office,
                                                                                               monkeypatch):
    old, _ = standard(office, title="מהדורה ישנה", sha="1" * 64, edition="גרסה 1.0", effective="01.01.2022",
                      approach=THREE)
    new, _ = standard(office, title="מהדורה חדשה", sha="2" * 64, edition="גרסה 2.0", effective="05.10.2025",
                      approach=FIVE)
    prop, _ = professional_file(
        office, office.default_group_id,
        [("paragraph", None, [], "הנכס ברחוב הרקפות 5. השומה נערכה ביום 15.03.2023 לפי שלוש עסקאות השוואה.", 1)],
        title="שומה הרקפות 5 (סינתטי)", source_kind="appraisal")
    def answer(items) -> dict:
        out = "\n".join(i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")

        def sid(doc: str, words: str) -> str:
            return re.search(rf'<source id="(S\d+)" document_id="{doc}"[^>]*>\n[^\n]*{words}', out).group(1)

        a, b, c = sid(old, "שלוש"), sid(new, "חמש"), sid(prop, "שלוש")
        return final(f"השומה נערכה ב-15.03.2023 ובה שלוש עסקאות השוואה [{c}]. המהדורה שנכנסה לתוקף ב-05.10.2025 דורשת "
                     f"חמש עסקאות [{b}], והקודמת (מ-01.01.2022) דרשה שלוש [{a}]. לא נקבע שהשומה חלה תחת המהדורה "
                     "החדשה, ולכן אי אפשר לקבוע שהיא אינה עומדת בתקן.", documents=[old, new, prop])

    agent = ScriptedAgent([[call("find_standards", query=None, page=None),
                            call("search", query="עסקאות השוואה לפחות", document_ids=None, limit=None)], answer],
                          intent="mixed")
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "האם השומה עומדת בתקן 98.1?")
    assert m["status"] == "done", m
    seen = "\n".join(agent.tool_outputs(1))
    assert "01.01.2022" in seen and "05.10.2025" in seen and "סדר המהדורות: ידוע" in seen
    assert THREE in seen and FIVE in seen  # both editions' provisions, each under its own edition
    editions = {s["standard"]["edition"]: s["title"] for s in m["answer"]["sources"] if s.get("standard")}
    assert editions == {"גרסה 1.0": "תקן 98.1 (גרסה 1.0, בתוקף מ-01.01.2022)",
                        "גרסה 2.0": "תקן 98.1 (גרסה 2.0, בתוקף מ-05.10.2025)"}  # each cited edition names itself
    assert {s["source_kind"] for s in m["answer"]["sources"]} == {"standard", "appraisal"}
    # the rule of the policy: an earlier-edition appraisal is not judged against the newer one without shown
    # applicability, and a departure is reported with both sources and the applicability checks
    assert "מהדורה מוקדמת" in engine.POLICY and "בדיקות התחולה" in engine.POLICY


def test_the_policy_answers_by_intent_in_general_terms():
    for rule in ("מקורות מקצועיים", "דוגמה לבחירה של נכס מסוים", "הדרישה של התקן", "הנתונים של השומה",
                 "אין לקבוע שמהדורה היא האחרונה", "נוסח התקן אינו במאגר", "מהדורה מוקדמת", "בדיקות התחולה"):
        assert rule in engine.POLICY, rule
    assert "find_standards" in engine.POLICY and "pool" in engine.POLICY


def test_find_standards_and_the_pool_argument_are_in_the_tool_schema():
    tools = {t["name"]: t for t in T.TOOLS}
    assert "find_standards" in tools and "find_standards" in T.HANDLERS
    search = tools["search"]["parameters"]
    assert "pool" in search["properties"] and "pool" in search["required"]
    assert search["properties"]["pool"]["type"] == ["string", "null"]
    assert set(tools["find_standards"]["parameters"]["required"]) == set(tools["find_standards"]["parameters"]["properties"])
    assert re.search(r"[א-ת]", tools["find_standards"]["description"])
