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
from tests.support.scripted_agent import ScriptedAgent, call, final

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
        final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].", scope="set", scope_query=PLACE)])
    led = a["ledger"]
    assert _titles(led["matching"]) == {f"שומה הגפן 12 {PLACE}", f"שומה הזית 7 {PLACE}"}  # not the third
    assert _titles(led["not_checked"]) == {f"שומה הזית 7 {PLACE}"} and led["complete"] is False
    assert a["status"] == "partial"
    assert "**כיסוי:** 2 מסמכים מתאימים" in a["markdown"] and f"לא נבדקו: שומה הזית 7 {PLACE}" in a["markdown"]


def test_a_matching_document_that_came_up_in_search_but_was_not_used_is_named(client, setup, monkeypatch):
    # the reported failure: the search returned passages of both appraisals, the answer used one
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query=f"השווי למ\"ר ב{PLACE}", document_ids=None, limit=None)],
        lambda items: final(f"השווי למ\"ר בנוי הוא 9,500 ₪ [{_source_of(items, setup.d1)}].", scope="set",
                            scope_query=PLACE)])
    led = a["ledger"]
    assert _titles(led["unused"]) == {f"שומה הזית 7 {PLACE}"} and not led["not_checked"]
    assert a["status"] == "partial" and "לא נמצא בהם נתון שנכלל בתשובה" in a["markdown"]


def test_a_set_answer_using_every_matching_document_has_no_note(client, setup, monkeypatch):
    a, _ = _ask(client, setup, monkeypatch, [
        [call("find_documents", query=PLACE, page=None)],
        [call("search", query="שווי", document_ids=[setup.d1], limit=None),
         call("search", query="שווי", document_ids=[setup.d2], limit=None)],
        lambda items: final(f"בהגפן 9,500 ₪ [{_source_of(items, setup.d1)}] ובהזית 11,000 ₪ "
                            f"[{_source_of(items, setup.d2)}].", scope="set", scope_query=PLACE)])
    assert a["ledger"]["complete"] is True and a["status"] == "answered" and "כיסוי" not in a["markdown"]


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
        lambda items: final(f"בהגפן 9,500 ₪ [{_source_of(items, setup.d1)}] ובהזית 11,000 ₪ "
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
    assert first.startswith("עמוד 1 מתוך 2; סה\"כ 75 מסמכים") and "page=2" in first
    assert len(ws.scope["matching"]) == 75 and ws.scope["pages_read"] == {1}
    second = T.tool_list_documents(ws, None, 2)
    assert second.startswith("עמוד 2 מתוך 2") and ws.scope["pages_read"] == {1, 2}
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
    partial = json.loads(T.tool_compute(ws, "mean", ids))
    assert "חישוב חלקי" in partial["note"]
    T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"], page=2)
    assert len(ws.measurements) == 130  # every value exactly once
    full = json.loads(T.tool_compute(ws, "mean", list(ws.measurements)))
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
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "ריבוי ערכים"} for i in range(n)]}

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
    table = T.tool_open_source(ws, sid, "table")
    assert "הטבלה: 9 שורות; ערכים מספריים לפי עמודה:" in table and "שכ\"ד למ\"ר 9" in table
