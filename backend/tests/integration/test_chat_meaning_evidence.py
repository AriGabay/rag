"""A qualifier the cited passage does not repeat is looked for in the same calculation — the section around the
passage, or a stored measurement of the subject property — and cited; without such evidence it is removed, never
added. The server's reading for the check does not count as the turn having read the section (KTD5 of plan
2026-10-07-1643). Synthetic documents only; the judge is scripted."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.verify import verify_answer
from app.db import tenant_tx
from app.providers.llm import Purpose
from tests.factories import make_document, make_office
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

TITLE = "שומה - הערבה 2 גבעת השקד"


def _doc(office, blocks: list[tuple[str, str]], sha: str):
    doc, ver = make_document(office, office.default_group_id, TITLE, sha=sha)
    with tenant_tx(office.system) as conn:
        for i, (section, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, paragraph_no, text) VALUES (app_office(), :d, :v, :b, 'paragraph', :s, ARRAY[:s],"
                " :p, :t)"), {"d": doc, "v": ver, "b": i, "s": section, "p": i + 1, "t": t})
    return doc, ver


def _judge() -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": int(n), "verdict": "supported", "reason": "בדיקה"}
        for n in re.findall(r'<unit index="(\d+)"', input)]}, repeat=True)
    return p


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[])


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def _ws(office, doc, ver, block: int, passage: str) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx())
    ws.add_source(document_id=doc, version_id=ver, title=TITLE, section="תחשיב", location="סעיף \"תחשיב\"",
                  kind="text", text=passage, block_start=block, block_end=block)
    return ws


def test_a_period_stated_in_the_cited_passages_section_is_cited_and_kept(office):
    doc, ver = _doc(office, [("תחשיב", "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר."),
                             ("תחשיב", "לאחר התאמות לגודל ולמיקום, דמי השכירות נקבעו ל-70 ₪ למ\"ר לחודש."),
                             ("סקר שוק", "עסקת השוואה ברחוב הדולב: 64 ₪ למ\"ר לחודש.")], "1" * 64)
    ws = _ws(office, doc, ver, 0, "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר.")
    md = "דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1]."
    report = verify_answer(_judge(), _answer(md), ws, "?", [])
    final = report.apply(_answer(md))
    assert report.ok and "לחודש" in final.answer_markdown and "[S1][S2]" in final.answer_markdown
    assert "64" not in ws.sources["S2"].text  # the section of the calculation, not the survey's
    assert str(doc) not in ws.activity  # the server's own reading is not the turn's coverage


def test_without_evidence_in_the_calculation_the_period_is_removed(office):
    doc, ver = _doc(office, [("תחשיב", "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר."),
                             ("סקר שוק", "עסקת השוואה ברחוב הדולב: 70 ₪ למ\"ר לחודש.")], "2" * 64)
    ws = _ws(office, doc, ver, 0, "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר.")
    md = "דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1]."
    report = verify_answer(_judge(), _answer(md), ws, "?", [])
    assert not report.ok and any(p.removes_unit for p in report.problems)
    assert "לחודש" not in report.apply(_answer(md)).answer_markdown


def test_a_subject_measurement_with_the_period_is_cited(office):
    doc, ver = _doc(office, [("תחשיב", "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר.")], "3" * 64)
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric, metric_kind, value,"
            " value_form, value_text, unit, period, area_basis, vat, subject, subject_role, value_role, quote,"
            " section, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, 7, 'k7',"
            " 'דמי שכירות ראויים', 'rent_per_area', 70, 'exact', '70 ₪ למ\"ר', 'ILS_per_sqm', 'month', '',"
            " 'unknown', 'הנכס', 'appraised_property', 'appraiser_determination',"
            " 'דמי השכירות הראויים 70 ₪ למ\"ר לחודש', 'סיכום', 'test', 'test', 'auto_validated', '[]'::jsonb)"),
            {"d": doc, "v": ver})
    ws = _ws(office, doc, ver, 0, "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר.")
    md = "דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1]."
    report = verify_answer(_judge(), _answer(md), ws, "?", [])
    assert report.ok and "[S1][M1]" in report.apply(_answer(md)).answer_markdown
