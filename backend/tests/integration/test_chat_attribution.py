"""Value stance and attribution through the stored reading (U10, R10, R23, R24, AE7).

A synthetic decision: the respondent's section states one figure, the decision's section adopts another. Read from
the database, ``take_value`` records who stated each figure and how, from the words around it or its section, and
the section path of its block; an answer giving the respondent's figure as the decision's is caught, while the
adopted figure, and the respondent's figure attributed to the respondent, are kept. A stored measurement's stance is
listed by ``find_measurements``. Synthetic text only; the parties, property and figures are invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat import verify
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db

TITLE = "החלטה בערר סינתטי על שווי קרקע"
RESPONDENT = "4. טענות המשיבה"
DECISION = "6. דיון והכרעה"
CLAIM = "המשיבה טוענת כי שווי הקרקע הוא 200 ₪ למ\"ר."
SECTION_ONLY = "שווי המבנה הוא 120 ₪ למ\"ר."
ADOPT = "לאחר שבחנתי את טענות הצדדים, אני קובע כי שווי הקרקע הוא 190 ₪ למ\"ר."
PLAIN = "שווי החניה הוא 80 ₪ למ\"ר."


def _decision(office) -> tuple[str, object]:
    doc, ver = make_document(office, office.default_group_id, TITLE)
    blocks = [("heading", RESPONDENT, [RESPONDENT]), ("paragraph", CLAIM, [RESPONDENT]),
              ("paragraph", SECTION_ONLY, [RESPONDENT]), ("heading", DECISION, [DECISION]),
              ("paragraph", ADOPT, [DECISION]), ("paragraph", PLAIN, [DECISION])]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, path) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1], "sp": path, "t": t})
    return str(doc), ver


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.doc, a.ver = _decision(a)
    return a


def _read_page(ws: T.Workspace, doc: str) -> str:
    out = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    return re.search(r'<source id="(S\d+)"', out).group(1)


def _quote(q: str, number: str) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": q,
            "number": number}


def _meaning(**kw) -> dict:
    return {"kind": "value_per_area", "unit": "ILS_per_sqm", "period": "none", "vat": "unknown", "area_basis": "",
            "subject": "הקרקע", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def _take(ws, sid: str, q: str, number: str, **kw):
    T.tool_take_value(ws, sid, _quote(q, number), _meaning(**kw), "שווי למ\"ר")
    return list(ws.values.values())[-1]


def test_each_figure_is_attributed_by_its_own_words_or_section(office):
    ws = T.Workspace(ctx=office.ctx())
    sid = _read_page(ws, office.doc)
    claim = _take(ws, sid, CLAIM, "200")
    assert (claim.stance, claim.stated_by, claim.section) == ("claim", "המשיבה", RESPONDENT)
    assert claim.provenance["stance"] == claim.provenance["stated_by"] == "source"
    by_section = _take(ws, sid, SECTION_ONLY, "120")
    assert (by_section.stance, by_section.stated_by) == ("claim", "המשיבה")
    adopted = _take(ws, sid, ADOPT, "190", stance="adopted")
    assert adopted.stance == "adopted" and adopted.provenance["stance"] == "source" and adopted.section == DECISION
    unknown = _take(ws, sid, PLAIN, "80")
    assert unknown.stance == "unknown" and unknown.certainty == "verified"
    asserted = _take(ws, sid, PLAIN, "80", stance="adopted", stated_by="השמאי המכריע")
    assert asserted.certainty == "model_asserted" and T.value_status(ws, asserted.vid) == T.STATUS_UNCERTAIN
    body = verify._source_parts(ws, claim.vid)[1]
    assert RESPONDENT in body and "המשיבה" in body and "נמצא במקור" in body
    assert "ייחוס: לא ידוע" in verify._source_parts(ws, unknown.vid)[1]


def test_ae7_the_respondents_figure_as_the_decisions_is_caught_and_the_rest_is_kept(office):
    ws = T.Workspace(ctx=office.ctx())
    sid = _read_page(ws, office.doc)
    claim = _take(ws, sid, CLAIM, "200")
    adopted = _take(ws, sid, ADOPT, "190", stance="adopted")
    answer = (f"ההחלטה קבעה שווי של 200 ₪ למ\"ר [{claim.vid}].\n\n"
              f"השמאי המכריע קבע שווי של 190 ₪ למ\"ר [{adopted.vid}].\n\n"
              f"לטענת המשיבה השווי 200 ₪ למ\"ר [{claim.vid}].")
    units = verify.split_units(answer)
    problems = verify.deterministic(units, ws, "מה שווי הקרקע שנקבע בהחלטה?")
    flagged = {p.unit.index for p in problems}
    assert len(flagged) == 1 and "200" in units[min(flagged)].text and "ההחלטה" in units[min(flagged)].text
    assert all(p.removes_unit for p in problems)


def test_find_measurements_lists_a_stored_stance(office):
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
            " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
            " extraction_version, model, status, issues, stated_by, stance) VALUES (app_office(), :d, :v, 1, 'b1:x',"
            " 'שווי קרקע', 'value_per_area', 200, 'exact', '200 ₪', 'ILS_per_sqm', 'none', 'unknown', 'general',"
            " 'other', :q, :e, 'test', 'auto_validated', '[]'::jsonb, 'המשיבה', 'claim')"),
            {"d": office.doc, "v": office.ver, "q": CLAIM, "e": EXTRACTION_VERSION})
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_find_measurements(ws, "שווי קרקע", ["value_per_area"])
    assert "ייחוס: טענה של המשיבה" in out
    mid = next(iter(ws.measurements))
    assert "טענה" in verify._source_parts(ws, mid)[1]
