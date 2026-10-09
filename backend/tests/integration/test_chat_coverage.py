"""Answers about a set of documents say what they covered: the server's coverage ledger and note.

The model is scripted; these tests prove what the server computes and adds (the set, what was checked, the
note, the status cap, paging), not the model's choices. Synthetic documents only; the place name is invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, final, read

pytestmark = pytest.mark.db

PLACE = "ברושים"


def add_doc(office, group, title: str, texts: list[str], sha: str):
    doc, ver = make_document(office, group, title, sha=sha)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    result = ExtractionResult(1, [PageResult(1, "\n".join(texts), "text_layer", 1.0, True)], [],
                              [ChunkResult(i, "text", [1], None, t) for i, t in enumerate(texts)])
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(office.system, info, 1e18)
    return str(doc)


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = a.default_group_id
    a.d1 = add_doc(a, g, f"שומה הגפן 12 {PLACE}", [f"שומה לנכס בשכונת {PLACE}: השווי למ\"ר בנוי הוא 9,500 ₪."], "1" * 64)
    a.d2 = add_doc(a, g, f"שומה הזית 7 {PLACE}", [f"שומה לנכס ב{PLACE}: השווי למ\"ר בנוי הוא 11,000 ₪."], "2" * 64)
    a.d3 = add_doc(a, g, "שומה הכרמל 3", ["שומה לנכס בעיר אחרת: השווי למ\"ר בנוי הוא 8,000 ₪."], "3" * 64)
    return a


def _source_of(items: list, document_id: str) -> str:
    """The S# of the given document in the tool outputs the model saw."""
    for i in items:
        if isinstance(i, dict) and i.get("type") == "function_call_output":
            for m in re.finditer(r'<source id="(S\d+)" document_id="([^"]+)"', i["output"]):
                if m.group(2) == document_id:
                    return m.group(1)
    raise AssertionError("document not in the tool outputs")


def _ask(client, setup, monkeypatch, steps, question=f"מה השווי למ\"ר ב{PLACE}?"):
    agent = ScriptedAgent(steps)
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return m["answer"], agent


def _titles(entries) -> set[str]:
    return {e["title"] for e in entries or []}


def test_set_answer_that_read_one_of_two_matching_documents_says_so(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query="שווי למ\"ר", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S2].", scope="set", scope_query=PLACE)])  # S1: the listing
    led = a["ledger"]
    assert _titles(led["matching"]) == {f"שומה הגפן 12 {PLACE}", f"שומה הזית 7 {PLACE}"}  # not the third
    assert _titles(led["not_checked"]) == {f"שומה הזית 7 {PLACE}"} and led["complete"] is False
    assert a["status"] == "partial"
    assert "**כיסוי:** 2 מסמכים מתאימים" in a["markdown"] and "הפירוט בחלונית המקורות" in a["markdown"]
    assert f"שומה הזית 7 {PLACE}" not in a["markdown"]  # the names are listed in the panel (ledger), not the answer


def test_a_matching_document_that_came_up_in_search_but_was_not_used_is_named(client, setup, monkeypatch):
    # the reported failure: the search returned passages of both appraisals, the answer used one
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query=f"השווי למ\"ר ב{PLACE}", document_ids=None, limit=None)],
        lambda items: final(f"השווי למ\"ר בנוי הוא 9,500 ₪ [{_source_of(items, setup.d1)}].", scope="set",
                            scope_query=PLACE)])
    led = a["ledger"]
    assert _titles(led["unused"]) == {f"שומה הזית 7 {PLACE}"} and not led["not_checked"]
    assert a["status"] == "partial" and "הפירוט בחלונית המקורות" in a["markdown"]  # the names: in the ledger


def test_a_set_answer_from_retrieved_passages_covers_the_documents_but_says_they_were_not_read(client, setup,
                                                                                               monkeypatch):
    # AE7: a passage from every matching document, none opened: document coverage full, section reading none
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query="שווי", document_ids=[setup.d1], limit=None),
         call("search", query="שווי", document_ids=[setup.d2], limit=None)],
        lambda items: final(f"בהגפן 9,500 ₪ למ\"ר בנוי [{_source_of(items, setup.d1)}] ובהזית 11,000 ₪ למ\"ר בנוי "
                            f"[{_source_of(items, setup.d2)}].", scope="set", scope_query=PLACE)])
    led = a["ledger"]
    assert led["complete"] is True and a["status"] == "answered"
    assert len(led["with_data"]) == 2 and led["read"] == [] and len(led["retrieved_only"]) == 2
    assert set(led["levels"].values()) == {"verified"}
    assert "נקראו (סעיף/טבלה) 0 · נשלפו קטעים בלבד מ-2 · נתון מאומת מ-2" in a["markdown"]
    assert "ייתכנו בהם נתונים נוספים" in a["markdown"]


def test_a_document_only_located_is_not_checked(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        lambda items: final(f"בהגפן 9,500 ₪ למ\"ר בנוי [{_source_of(items, setup.d1)}].", scope="set",
                            scope_query=PLACE)])
    led = a["ledger"]
    assert led["levels"][setup.d2] == "located" and _titles(led["not_checked"]) == {f"שומה הזית 7 {PLACE}"}
    assert led["complete"] is False and a["status"] == "partial"


def test_an_omission_the_answer_explains_is_not_flagged(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query="שווי", document_ids=[setup.d1], limit=None),
         call("search", query="שווי", document_ids=[setup.d2], limit=None)],
        lambda items: final(f"השווי למ\"ר בנוי הוא 9,500 ₪ [{_source_of(items, setup.d1)}].", scope="set",
                            scope_query=PLACE,
                            omitted=[{"document_id": setup.d2, "what": "שווי 11,000 ₪",
                                      "why": "נכס מסוג אחר מזה שנשאל"}])])
    led = a["ledger"]
    assert led["complete"] is True and not led["unused"]
    assert led["omitted"][0]["title"] == f"שומה הזית 7 {PLACE}"


def test_listing_all_documents_after_find_documents_keeps_the_set(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("list_documents", query=None, page=None)],
        [call("search", query="שווי", document_ids=[setup.d1], limit=None),
         call("search", query="שווי", document_ids=[setup.d2], limit=None)],
        lambda items: final(f"בהגפן 9,500 ₪ למ\"ר בנוי [{_source_of(items, setup.d1)}] ובהזית 11,000 ₪ למ\"ר בנוי "
                            f"[{_source_of(items, setup.d2)}].", scope="set", scope_query=PLACE)])
    assert a["ledger"]["scope_query"] == PLACE and a["ledger"]["complete"] is True
    assert len(a["ledger"]["matching"]) == 2


def test_every_document_a_ledger_names_is_seen_by_the_permission_check(client, setup, monkeypatch):
    from app.chat import api as chat_api
    from app.chat.coverage import LEDGER_DOCUMENT_KEYS

    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query=f"השווי למ\"ר ב{PLACE}", document_ids=None, limit=None)],
        lambda items: final(f"השווי למ\"ר בנוי הוא 9,500 ₪ [{_source_of(items, setup.d1)}].", scope="set",
                            scope_query=PLACE)])
    named = {d["document_id"] for k, v in a["ledger"].items() if isinstance(v, list)
             for d in v if isinstance(d, dict) and d.get("document_id")}
    assert named and named <= chat_api._answer_documents(a)
    assert {k for k, v in a["ledger"].items() if isinstance(v, list)} <= set(LEDGER_DOCUMENT_KEYS)


def test_without_find_documents_the_server_looks_the_set_up(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", scope="set", scope_query=PLACE)])
    assert _titles(a["ledger"]["not_checked"]) == {f"שומה הזית 7 {PLACE}"} and a["status"] == "partial"


def test_a_generic_word_of_the_scope_query_does_not_widen_the_set(setup):
    from app.platform.search import documents_matching

    with tenant_tx(setup.ctx()) as conn:
        found = documents_matching(conn, f"שווי {PLACE}")
    assert {str(d["document_id"]) for d in found} == {setup.d1, setup.d2}  # "שווי" alone is in all three


def test_focused_answer_names_other_documents_sharing_the_questions_title_word(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].")])
    assert _titles(a["ledger"]["also_matching"]) == {f"שומה הזית 7 {PLACE}"}
    assert a["status"] == "partial" and "**שימו לב:**" in a["markdown"]


def test_focused_answer_on_a_named_document_has_no_note(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].")], question="מה השווי בהגפן 12?")
    assert not a["ledger"]["also_matching"] and a["status"] == "answered"


def test_focused_guard_with_three_documents_in_the_place(client, setup, monkeypatch):
    for n in range(5):  # enough titles that a word in three of them is still distinctive
        add_doc(setup, setup.default_group_id, f"דוח שוק {n}", [f"סקירה כללית מספר {n}."], f"{n + 4}" * 64)
    d4 = add_doc(setup, setup.default_group_id, f"שומה האלה 5 {PLACE}", ["השווי למ\"ר הוא 10,000 ₪."], "9" * 64)
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].")])
    assert {e["document_id"] for e in a["ledger"]["also_matching"]} == {setup.d2, d4}


def test_find_documents_respects_permissions(setup):
    g2 = make_group(setup, "קבוצה 2")
    hidden = add_doc(setup, g2, f"שומה סודית {PLACE}", [f"נכס ב{PLACE}."], "a" * 64)
    make_user(setup, "emp@example.test", [setup.default_group_id])
    with tenant_tx(setup.system) as conn:
        uid = conn.execute(text("SELECT id FROM users WHERE email = 'emp@example.test'")).scalar_one()
    ws = T.Workspace(ctx=setup.ctx("employee", uid))
    out = T.tool_find_documents(ws, PLACE)
    assert "סה\"כ 2 מסמכים" in out and hidden not in out


# --- paging --------------------------------------------------------------------------------------------------

def test_list_documents_pages_and_records_the_scope(setup):
    for n in range(72):
        make_document(setup, setup.default_group_id, f"מסמך {n:03d}", sha=f"{n:064d}")
    ws = T.Workspace(ctx=setup.ctx())
    first = T.tool_list_documents(ws, None)
    assert "עמוד 1 מתוך 2; סה\"כ 75 מסמכים" in first and "page=2" in first
    assert len(ws.scope["matching"]) == 75 and ws.scope["pages_read"] == {1}
    second = T.tool_list_documents(ws, None, 2)
    assert "עמוד 2 מתוך 2" in second and ws.scope["pages_read"] == {1, 2}
    titles = re.findall(r'"(מסמך \d{3}|שומה [^"]+)"', first + second)
    assert len(titles) == len(set(titles)) == 75  # every document exactly once across the pages
    assert "אין עמוד 3" in T.run_tool(ws, "list_documents", json.dumps({"query": None, "page": 3}))


def _measure(conn, doc: str, ver: str, value: int, block: int, row: int) -> None:
    conn.execute(text(
        "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
        " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
        " value_role, quote, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :b, 0, :r,"
        " :k, 'דמי שכירות למ\"ר', 'rent_per_area', :val, 'exact', :vt, 'ILS_per_sqm', 'month', 'unknown', 'asking',"
        " 'asking_price', :q, :e, 'test', 'auto_validated', '[]'::jsonb)"),
        {"d": doc, "v": ver, "b": block, "r": row, "k": f"t0:r{row}:{value}", "val": value, "vt": f"{value} ₪",
         "q": f"שורה {row}: {value} ₪", "e": EXTRACTION_VERSION})


def test_find_measurements_pages_without_skipping_rows_and_compute_says_partial(setup):
    with tenant_tx(setup.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": setup.d1}).scalar_one()
        for n in range(130):  # pairs of values share a table row, so the order needs its tie-breaker
            _measure(conn, setup.d1, str(ver), 100 + n, 5, n // 2)
    ws = T.Workspace(ctx=setup.ctx())
    first = T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"])
    assert "עמוד 1 מתוך 2; סה\"כ 130" in first
    ids = list(ws.measurements)
    partial = json.loads(T.tool_calculate(ws, f"mean({', '.join(ids)})", "ממוצע"))
    assert "חישוב חלקי" in partial["note"]
    T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"], page=2)
    assert len(ws.measurements) == 130  # every value exactly once
    full = json.loads(T.tool_calculate(ws, f"mean({', '.join(ws.measurements)})", "ממוצע"))
    assert "חישוב חלקי" not in full["note"] and full["n"] == 130


# --- table multiplicity ----------------------------------------------------------------------------------------

def _rent_table(office) -> str:
    doc, ver = make_document(office, office.default_group_id, "סקר היצע משרדים", sha="t" * 64)
    rows = [[f"רחוב הדגמה {i}", f"{100 + i}", f"₪ {50 + i}"] for i in range(1, 10)]
    structure = {"headers": ["כתובת", "שטח במ\"ר", "שכ\"ד למ\"ר"], "caption": "נתוני היצע משרדים:", "title": [],
                 "notes": [], "section": "סקר", "rows": [{"cells": r} for r in rows]}
    with tenant_tx(office.system) as conn:
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
        for i, r in enumerate(rows):
            t = f"נתוני היצע משרדים: כתובת: {r[0]} | שטח במ\"ר: {r[1]} | שכ\"ד למ\"ר: {r[2]}"
            conn.execute(text("INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list,"
                              " text, normalized_text, table_index, row_index) VALUES (app_office(), :d, :v, :i,"
                              " 'table_row', :p, :t, :t, 0, :i)"), {"d": doc, "v": ver, "i": i, "p": [1], "t": t})
    pipeline.embed_stage(office.system, pipeline.VersionInfo(ver, doc, "k", "application/pdf", None), 1e18)
    return str(doc)


def test_a_table_row_hit_carries_the_table_size_to_the_model_and_the_judge(client, setup, monkeypatch):
    doc = _rent_table(setup)
    seen: list[str] = []

    def judge(input: str) -> dict:
        seen.append(input)
        n = input.count("<unit index=")
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "ריבוי ערכים", "defect": "multiple_values"}
                             for i in range(n)]}

    def answer(items):
        return final(f"שכר הדירה למ\"ר הוא 53 ₪ [{_source_of(items, doc)}].", documents=[doc])

    # the partial verdict is repaired, then rewritten; the answer stays the same and is marked
    agent = ScriptedAgent([[call("search", query="שכ\"ד למ\"ר רחוב הדגמה 3", document_ids=[doc], limit=None)],
                           answer, answer, answer], judge=judge)
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה שכר הדירה בסקר?")
    assert "הטבלה: 9 שורות" in agent.tool_outputs(1)[0]
    assert seen and "הטבלה: 9 שורות" in seen[0]
    assert "אומת חלקית" in m["answer"]["markdown"]


def test_open_table_states_its_size(setup):
    doc = _rent_table(setup)
    ws = T.Workspace(ctx=setup.ctx())
    out = T.tool_search(ws, "רחוב הדגמה 4", [doc])
    sid = re.search(r'<source id="(S\d+)"', out).group(1)
    table = T.tool_read(ws, {"source": sid})  # a row's source opens its whole table
    assert "הטבלה: 9 שורות; ערכים מספריים לפי עמודה:" in table and "שכ\"ד למ\"ר 9" in table
    assert 'kind="table"' in table and 'status="complete"' in table and "רחוב הדגמה 9 |" in table


# --- reading levels and the dedup key ----------------------------------------------------------------------------

def _long_section(office) -> tuple[str, object]:
    doc, ver = make_document(office, office.default_group_id, "שומה רחוב השיטה 5", sha="s" * 64)
    with tenant_tx(office.system) as conn:
        for i in range(7):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, paragraph_no, text) VALUES (app_office(), :d, :v, :b, 'paragraph', 'תיאור הנכס',"
                " ARRAY['תיאור הנכס'], :p, :t)"),
                {"d": doc, "v": ver, "b": i + 2, "p": i + 1, "t": f"פסקה {i + 1}: " + "תיאור מפורט של המבנה. " * 40})
    return str(doc), ver


def test_a_section_opened_after_the_paragraphs_around_the_same_place_sends_only_what_is_new(setup):
    doc, ver = _long_section(setup)
    ws = T.Workspace(ctx=setup.ctx())
    ws.add_source(document_id=doc, version_id=ver, title="שומה רחוב השיטה 5", section="תיאור הנכס",
                  location="סעיף", kind="text", text="פסקה 4", block_start=5, block_end=5)
    near = T.tool_read(ws, {"source": "S1"})
    assert 'status="clipped"' in near and ws.activity[doc]["read"] is False
    handle = re.search(r"(§\d+) «תיאור הנכס»", near).group(1)
    assert f'more="{handle}"' in near  # the paragraphs around it read on as its section
    section = T.tool_read(ws, {"section": handle})
    assert "same_as" not in section and "פסקה 6:" in section and "כבר הוחזרו בתור הזה ב-S2" in section
    assert "פסקה 4:" not in section and "פסקה 4:" in ws.sources["S3"].text  # the source keeps the full text
    assert 'status="clipped"' in section and "פסקה 7:" not in section
    rest = T.tool_read(ws, {"cursor": re.search(r'more="(K\d+)"', section).group(1)})
    assert 'status="complete"' in rest and "פסקה 7:" in rest and "פסקה 6:" not in rest
    a = ws.activity[doc]
    target = ("section", str(ver), ("תיאור הנכס",))
    assert a["read"] is True and a["read_complete"] is True
    assert a["openings"] == [{"sid": sid, "scope": "section", "name": "תיאור הנכס", "target": target}
                             for sid in ("S3", "S4")]
    # the same section opened again is a reference to the first
    assert 'same_as="S3"' in T.tool_read(ws, {"section": handle})


def test_a_section_opened_from_a_sub_section_reads_its_whole_top_level_section(setup):
    """PDF blocks carry sub-section paths (["3. תיאור הנכס", "3.1 הבניין"]): opening the section from a block of
    3.1 still opens all of 3, its sub-sections included, and nothing of 4."""
    doc, ver = make_document(setup, setup.default_group_id, "שומה רחוב הצאלון 2", sha="u" * 64)
    blocks = [("heading", ["3. תיאור הנכס"], "3. תיאור הנכס"),
              ("heading", ["3. תיאור הנכס", "3.1 הבניין"], "3.1 הבניין"),
              ("paragraph", ["3. תיאור הנכס", "3.1 הבניין"], "הבניין בן ארבע קומות."),
              ("heading", ["3. תיאור הנכס", "3.2 הדירה"], "3.2 הדירה"),
              ("paragraph", ["3. תיאור הנכס", "3.2 הדירה"], "הדירה בקומה השנייה."),
              ("heading", ["4. התחשיב"], "4. התחשיב"),
              ("paragraph", ["4. התחשיב"], "שווי הנכס 990,000 ₪.")]
    with tenant_tx(setup.system) as conn:
        for i, (kind, path, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 2, :t)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1], "sp": path, "t": t})
    ws = T.Workspace(ctx=setup.ctx())
    ws.add_source(document_id=str(doc), version_id=ver, title="שומה רחוב הצאלון 2", section="3.1 הבניין",
                  location="עמוד 2", kind="text", text="הבניין בן ארבע קומות.", block_start=2, block_end=2)
    near = T.tool_read(ws, {"source": "S1"})
    top, sub = re.search(r"בסעיף: (§\d+) «3\. תיאור הנכס» / (§\d+) «3\.1 הבניין»", near).groups()
    T.tool_read(ws, {"section": top})
    whole = ws.sources["S3"]
    assert "3.1 הבניין" in whole.text and "הדירה בקומה השנייה." in whole.text and "990,000" not in whole.text
    assert (whole.block_start, whole.block_end) == (0, 4)
    T.tool_read(ws, {"section": sub})  # a sub-section is its own part
    assert "הבניין בן ארבע קומות." in ws.sources["S4"].text and "הדירה" not in ws.sources["S4"].text


def test_a_table_answer_states_the_rows_it_presented(client, setup, monkeypatch):
    doc = _rent_table(setup)

    def answer(items):  # cites the opened table, not the row hits that led to it
        outputs = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
        sid = re.search(r'<source id="(S\d+)"[^>]*kind="table"', outputs[-1]).group(1)
        return final(f"ברחוב הדגמה 3 שכ\"ד למ\"ר 53 ₪, וברחוב הדגמה 4 שכ\"ד למ\"ר 54 ₪ [{sid}].",
                     documents=[doc], scope="set", scope_query="סקר היצע משרדים")

    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query="סקר היצע משרדים", page=None)],
        [call("search", query="רחוב הדגמה 4", document_ids=[doc], limit=None)],
        lambda items: [read(source=_source_of(items, doc))],
        answer], question="מה שכר הדירה בסקר ההיצע?")
    (table,) = a["ledger"]["tables"]
    assert table["rows"] == 9 and table["presented"] == 2
    assert "הוצגו ערכים מ-2 מתוך 9 שורות" in a["markdown"]


# --- explicit absence (AE6) ---------------------------------------------------------------------------------

def _described_property(office) -> str:
    doc, ver = make_document(office, office.default_group_id, "שומה רחוב האשל 9", sha="e" * 64)
    blocks = ["תיאור הנכס", "המבנה בן שתי קומות מעל קרקע, בנוי בבנייה קשיחה.",
              "השטח הבנוי של המבנה הוא 184 מ\"ר.", "לנכס חצר מגוננת וחניה מקורה."]
    with tenant_tx(office.system) as conn:
        for i, t in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, paragraph_no, text) VALUES (app_office(), :d, :v, :b, :k, 'תיאור הנכס',"
                " ARRAY['תיאור הנכס'], :p, :t)"),
                {"d": doc, "v": ver, "b": i, "k": "heading" if i == 0 else "paragraph", "p": i or None, "t": t})
        conn.execute(text("INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, text,"
                          " normalized_text, section, block_start, block_end) VALUES (app_office(), :d, :v, 0, 'text',"
                          " :p, :t, :t, 'תיאור הנכס', 2, 2)"), {"d": doc, "v": ver, "p": [1], "t": blocks[2]})
    pipeline.embed_stage(office.system, pipeline.VersionInfo(ver, doc, "k", "application/pdf", None), 1e18)
    return str(doc)


def test_a_missing_datum_is_said_first_with_the_section_that_was_checked(client, setup, monkeypatch):
    doc = _described_property(setup)
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שטח המגרש", document_ids=[doc], limit=None)],
        [call("outline", document=doc)],
        lambda items: [read(section=re.search(r"(§\d+) «תיאור הנכס»", items[-1]["output"]).group(1))],
        final("השטח הבנוי (נתון אחר) הוא 184 מ\"ר [S2].", documents=[doc],
              requested=[{"label": "שטח המגרש", "document_ids": [doc], "status": "section_checked_absent",
                          "checked_where": "S2"}])], question="מה שטח המגרש באשל 9?")
    # stated once by the server, after the answer (round 7 KTD4)
    assert a["markdown"].endswith("\n\n**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S2].")
    assert "(נתון אחר)" in a["markdown"] and a["status"] == "partial"
    (r,) = a["requested"]
    assert r["status"] == "section_checked_absent" and r["section"] == "תיאור הנכס"


def test_a_section_read_whole_through_the_paragraphs_around_a_hit_is_named_as_the_section_checked(
        client, setup, monkeypatch):
    """The paragraphs around a search hit (read(source)) hold every block of the hit's section: that read is a
    complete opening of the section, so a claim that it is not there names the section (round 7 KTD4, R8, R10)."""
    doc = _described_property(setup)
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שטח המגרש", document_ids=[doc], limit=None)],
        [read(source="S1")],
        final("השטח הבנוי (נתון אחר) הוא 184 מ\"ר [S2].", documents=[doc],
              requested=[{"label": "שטח המגרש", "document_ids": [doc], "status": "section_checked_absent",
                          "checked_where": "S2"}])], question="מה שטח המגרש באשל 9?")
    assert a["markdown"].endswith("\n\n**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S2].")
    (r,) = a["requested"]
    assert (r["status"], r["section"], r["scope"]) == ("section_checked_absent", "תיאור הנכס", "section")


def _three_sections(office) -> tuple[str, object]:
    """Page 1: a long section, a short one, another long one — a page window reads them in parts."""
    doc, ver = make_document(office, office.default_group_id, "שומה רחוב הערבה 4", sha="t" * 64)
    long_ = "תיאור מפורט של המבנה. " * 40
    blocks = [("heading", "1. תיאור הנכס", "1. תיאור הנכס"), *[("paragraph", "1. תיאור הנכס", f"פסקה {i}: {long_}")
                                                             for i in range(1, 5)],
              ("heading", "2. זכויות", "2. זכויות"), ("paragraph", "2. זכויות", "הזכויות רשומות בבעלות פרטית."),
              ("heading", "3. תחשיב", "3. תחשיב"), *[("paragraph", "3. תחשיב", f"שלב {i}: {long_}")
                                                     for i in range(1, 5)]]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1 WHERE id = :v"), {"v": ver})
        for i, (kind, section, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": section, "sp": [section], "t": t})
    return str(doc), ver


def _claim(doc: str, sid: str):
    from app.chat.engine import Requested

    return Requested(component="", label="שטח המגרש", document_ids=[doc], status="section_checked_absent",
                     checked_where=sid)


def test_a_page_windows_part_that_holds_a_whole_section_is_a_complete_opening_of_it(setup):
    from app.chat import coverage

    doc, _ = _three_sections(setup)
    ws = T.Workspace(ctx=setup.ctx())
    first = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    assert 'status="clipped"' in first and "2. זכויות" not in first
    second = T.tool_read(ws, {"cursor": re.search(r'more="(K\d+)"', first).group(1)})
    sid = re.search(r'<source id="(S\d+)"', second).group(1)
    assert 'status="clipped"' in second and "הזכויות רשומות" in second  # the page is not read to its end
    (r,) = coverage.validate_requested(ws, [_claim(doc, sid)])
    assert (r["status"], r["section"], r["scope"], r["checked_where"]) == ("section_checked_absent", "2. זכויות",
                                                                           "section", sid)
    assert coverage.absence_sentence(r) == f"**שטח המגרש** לא מופיע בסעיף \"2. זכויות\" שנבדק [{sid}]."
    # the first part holds only the start of «1. תיאור הנכס»: a part of a section opens nothing whole
    (r,) = coverage.validate_requested(ws, [_claim(doc, "S1")])
    assert r["status"] != "section_checked_absent"


def test_paragraphs_around_a_hit_that_hold_only_part_of_its_section_keep_not_located(client, setup, monkeypatch):
    from app.chat import coverage

    doc, ver = _long_section(setup)
    ws = T.Workspace(ctx=setup.ctx())
    ws.searches.append("שטח המגרש")
    ws.add_source(document_id=doc, version_id=ver, title="שומה רחוב השיטה 5", section="תיאור הנכס",
                  location="סעיף", kind="text", text="פסקה 4", block_start=5, block_end=5)
    near = T.tool_read(ws, {"source": "S1"})
    assert 'status="clipped"' in near
    (r,) = coverage.validate_requested(ws, [_claim(doc, "S2")])
    assert (r["status"], r["kind"]) == ("not_found_search", "not_located")
    assert ws.activity[doc]["openings"] == [] and ws.activity[doc]["read"] is False


def test_a_section_claim_after_a_search_only_says_not_found_in_search(client, setup, monkeypatch):
    doc = _described_property(setup)
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שטח המגרש", document_ids=[doc], limit=None)],
        final("השטח הבנוי (נתון אחר) הוא 184 מ\"ר [S1].", documents=[doc],
              requested=[{"label": "שטח המגרש", "document_ids": [doc], "status": "section_checked_absent",
                          "checked_where": "S1"}])], question="מה שטח המגרש באשל 9?")
    assert a["markdown"].endswith("\n\n**שטח המגרש** לא נמצא בחיפוש במסמכים שנבדקו.")
    assert a["requested"][0]["status"] == "not_found_search"


def test_a_not_found_sentence_is_added_after_verification_and_survives_a_strict_judge(client, setup, monkeypatch):
    doc = _described_property(setup)

    def judge(input: str) -> dict:  # a judge that rejects everything without sources
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="([^"]*)">', input):
            out.append({"index": int(m.group(1)), "verdict": "supported" if m.group(2) else "unsupported",
                        "reason": "בדיקה"})
        return {"verdicts": out}

    agent = ScriptedAgent([[call("search", query="שטח המגרש", document_ids=[doc], limit=None)],
                           final("השטח הבנוי (נתון אחר) הוא 184 מ\"ר [S1].", documents=[doc],
                                 requested=[{"label": "שטח המגרש", "document_ids": [doc],
                                             "status": "not_found_search", "checked_where": ""}])], judge=judge)
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), "מה שטח המגרש באשל 9?")["answer"]
    assert a["markdown"].endswith("\n\n**שטח המגרש** לא נמצא בחיפוש במסמכים שנבדקו.")
    assert a["verification"]["removed"] == 0


# --- every part of the request is answered or said to be missing (R19) ----------------------------------------

def test_a_document_whose_title_carries_fewer_of_the_questions_words_is_not_named(client, setup, monkeypatch):
    for n in range(5):  # enough titles that the place is a shared word and does not name a document by itself
        add_doc(setup, setup.default_group_id, f"דוח שוק {n}", [f"סקירה כללית מספר {n}."], f"{n + 4}" * 64)
    add_doc(setup, setup.default_group_id, f"שומה האלה 5 {PLACE}", ["השווי למ\"ר הוא 10,000 ₪."], "9" * 64)
    # the question carries the place (three titles) and the cited title's street: the others carry less of it
    a, _ = _ask(client, setup, monkeypatch, [
        [call("search", query="שווי", document_ids=[setup.d1], limit=None)],
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].")], question=f"מה השווי בהגפן ב{PLACE}?")
    assert not a["ledger"]["also_matching"]
