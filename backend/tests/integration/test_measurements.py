"""Measurements end to end: extraction through a scripted model, storage that keeps reviewers' decisions,
the review API (permissions, stale protection, history), and compute refusing to mix meanings."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.config import get_settings
from app.db import tenant_tx
from app.extraction.docx import extract_docx
from app.measurements.extract import EXTRACTION_VERSION, extract_version
from app.platform import pipeline
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_docx_blocks import FIXTURE

pytestmark = pytest.mark.db

SENTENCE_VALUE = {
    "metric_quote": 'השווי למ"ר בנוי ברוטו למסחר', "metric": 'שווי למ"ר בנוי ברוטו למסחר',
    "metric_kind": "value_per_area", "value_text": "9,500 ₪", "unit": "ILS_per_sqm", "period": "none",
    "period_quote": "", "area_basis": "בנוי ברוטו", "vat": "excluded", "vat_quote": 'ללא מע"מ',
}
SENTENCE_RENT = {
    "metric_quote": 'דמ"ש ראויים למ"ר', "metric": 'דמי שכירות ראויים למ"ר', "metric_kind": "rent_per_area",
    "value_text": "55 ₪", "unit": "ILS_per_sqm", "period": "month", "period_quote": 'למ"ר/חודש', "area_basis": "",
    # the model claims the VAT words of the value for the rent as well: validation must not keep it
    "vat": "excluded", "vat_quote": 'ללא מע"מ',
}


def _model(instructions: str, input: str) -> dict:
    """A scripted extraction answer for the fixture's passages: the two-value sentence, and the rent table."""
    import re

    out = {"measurements": [], "tables": []}
    for pid, body in re.findall(r'<passage id="(P\d+)" kind="\w+"[^>]*>\n(.*?)\n</passage>', input, re.S):
        if "9,500" in body:
            quote = next(line for line in body.splitlines() if "9,500" in line)
            for m in (SENTENCE_VALUE, SENTENCE_RENT):
                out["measurements"].append({"passage_id": pid, "quote": quote, "subject": "הנכס הנישום",
                                            "subject_role": "appraised_property",
                                            "value_role": "appraiser_determination", "effective_date": "", **m})
        if "שכ\"ד למ\"ר" in body and "[שורות לדוגמה]" in body:
            ann = lambda key, kind, unit, period="none", skip=False: {  # noqa: E731
                "key": key, "skip": skip, "metric": key, "metric_kind": kind, "unit": unit, "period": period,
                "area_basis": "", "vat": "unknown", "qualifier_quote": key}
            out["tables"].append({"passage_id": pid, "orientation": "columns", "subject_key": "כתובת",
                                  "subject_role": "asking", "value_role": "asking_price", "annotations": [
                                      ann("כתובת", "other", "other", skip=True), ann('שטח במ"ר', "area", "sqm"),
                                      ann('שכ"ד חודשי', "rent", "ILS", "month"),
                                      ann('שכ"ד למ"ר', "rent_per_area", "ILS_per_sqm", "month")]})
    return out


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    g2 = make_group(a, "קבוצה 2")
    make_user(a, "emp@example.test", [g2])
    doc, ver = make_document(a, a.default_group_id, "דוח סינתטי", sha="9" * 64)
    info = pipeline.VersionInfo(ver, doc, "k", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                None)
    result = extract_docx(FIXTURE.read_bytes(), time.monotonic() + 60, get_settings())
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    a.doc, a.ver, a.other = doc, ver, b
    return a


def run(office):
    provider = ScriptedProvider().on(Purpose.MEASURE, _model, repeat=True)
    return extract_version(office.system, office.ver, provider)


def rows(office):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT * FROM measurements ORDER BY metric_kind, row_index NULLS FIRST")).all()


def test_extraction_keeps_each_value_with_its_own_meaning(office):
    r = run(office)
    assert r.state == "done" and r.found > 0
    ms = rows(office)
    value = next(m for m in ms if m.metric_kind == "value_per_area")
    rent = next(m for m in ms if m.metric_kind == "rent_per_area" and m.table_index is None)
    assert (value.vat, value.area_basis, value.value_form, value.status) == ("excluded", "בנוי ברוטו", "approximate",
                                                                            "auto_validated")
    assert (rent.period, rent.vat, rent.status) == ("month", "unknown", "needs_review")
    assert any("מע״מ" in i for i in rent.issues)
    table_rent = [m for m in ms if m.metric_kind == "rent_per_area" and m.table_index is not None]
    assert sorted(str(m.value) for m in table_rent) == ["52", "55", "58", "61"]
    assert {m.value_role for m in table_rent} == {"asking_price"}
    with tenant_tx(office.system) as conn:
        state = conn.execute(text("SELECT state FROM measurement_runs WHERE extraction_version = :e"),
                             {"e": EXTRACTION_VERSION}).scalar_one()
    assert state == "done"


def test_review_decisions_survive_re_extraction_and_disagreement_is_flagged(client, office):
    run(office)
    login(client, "admin-a@example.test")
    items = client.get("/api/review/measurements", params={"q": "9,500"}).json()["items"]
    value = next(i for i in items if i["metric_kind"] == "value_per_area")
    r = client.post(f"/api/review/measurements/{value['id']}/correct",
                    json={"expected_status": value["status"], "value_text": "9,600 ₪", "note": "תוקן מול המקור"})
    assert r.status_code == 200 and r.json()["status"] == "corrected" and r.json()["previous"]
    # the same action on the list as it was is refused
    stale = client.post(f"/api/review/measurements/{value['id']}/verify", json={"expected_status": value["status"]})
    assert stale.status_code == 409
    run(office)  # the model reads 9,500 again: the person's 9,600 stays, the new reading waits for review
    ms = rows(office)
    corrected = next(m for m in ms if m.status == "corrected")
    assert str(corrected.value) == "9600" and corrected.conflict is not None
    again = [m for m in ms if m.metric_kind == "value_per_area" and m.status != "corrected"]
    assert len(again) == 1 and again[0].status == "needs_review"


def test_review_is_scoped_by_document_permissions_and_office(client, office):
    run(office)
    login(client, "emp@example.test")  # sees only group 2; the document is in the default group
    assert client.get("/api/review/measurements").json()["items"] == []
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.get("/api/review/measurements").json()["items"] == []
    with tenant_tx(office.system) as conn:
        mid = conn.execute(text("SELECT id FROM measurements LIMIT 1")).scalar_one()
    assert client.post(f"/api/review/measurements/{mid}/verify",
                       json={"expected_status": "auto_validated"}).status_code == 404


def test_reject_needs_a_reason(client, office):
    run(office)
    login(client, "admin-a@example.test")
    item = client.get("/api/review/measurements").json()["items"][0]
    assert client.post(f"/api/review/measurements/{item['id']}/reject",
                       json={"expected_status": item["status"]}).status_code == 422


def test_compute_refuses_to_mix_rent_with_value_and_computes_within_one_kind(office):
    run(office)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_find_measurements(ws, "למ\"ר", None, None, None)
    assert "כיסוי: 1 מתוך 1" in out
    ids = {m.row.metric_kind: [] for m in ws.measurements.values()}
    for mid, m in ws.measurements.items():
        ids[m.row.metric_kind].append(mid)
    mixed = T.tool_compute(ws, "mean", ids["value_per_area"] + ids["rent_per_area"][:1])
    assert mixed.startswith("אי אפשר לחשב")
    table = [mid for mid, m in ws.measurements.items()
             if m.row.metric_kind == "rent_per_area" and m.row.table_index is not None]
    result = T.tool_compute(ws, "mean", table)
    assert '"result": "56.50"' in result and '"n": 4' in result
    assert "אי אפשר לסכם" in T.tool_compute(ws, "sum", table)


def test_blocks_and_media_follow_document_permissions(client, office):
    login(client, "admin-a@example.test")
    url = f"/api/documents/{office.doc}/versions/{office.ver}/blocks"
    body = client.get(url).json()
    assert body["total"] == len(body["blocks"]) > 0
    table = next(b for b in body["blocks"] if b.get("table") and b["table"]["source"] == "emf")
    assert table["table"]["headers"][0] == "כתובת" and table["media"] == "image1.emf"
    assert "media_url" not in table  # a vector picture is shown as its table, not served as an image
    window = client.get(url, params={"start": 2, "end": 4}).json()["blocks"]
    assert [b["index"] for b in window] == [2, 3, 4]
    assert client.get(f"/api/documents/{office.doc}/versions/{office.ver}/media/image1.emf").status_code == 404
    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    assert client.get(url).status_code == 404
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.get(url).status_code == 404


def test_documents_list_reports_reading_apart_from_records(client, office):
    login(client, "admin-a@example.test")
    doc = client.get("/api/documents").json()["documents"][0]
    reading = doc["current_version"]["reading"]
    assert reading["passages"] > 0 and reading["tables"] == 2 and reading["partial"] is False
    assert reading["images"] == {"read": 1}
    assert doc["current_version"]["records_total"] in (None, 0)


def test_a_rejected_value_does_not_come_back_when_the_document_is_read_again(client, office):
    run(office)
    login(client, "admin-a@example.test")
    rent = next(i for i in client.get("/api/review/measurements", params={"q": "55"}).json()["items"]
                if i["metric_kind"] == "rent_per_area" and i["table_index"] is None)
    assert client.post(f"/api/review/measurements/{rent['id']}/reject",
                       json={"expected_status": rent["status"], "note": "בדיקה"}).status_code == 200
    with tenant_tx(office.system) as conn:  # a new reading shifts block numbers
        conn.execute(text("UPDATE measurements SET block_index = block_index + 50 WHERE status = 'rejected'"))
    run(office)
    again = [m for m in rows(office) if m.metric_kind == "rent_per_area" and m.table_index is None]
    assert [m.status for m in again] == ["rejected"]


def test_a_failed_extraction_keeps_the_measurements_and_fails_the_job(office):
    from app.measurements.extract import MeasurementRunFailed
    from app.providers.llm import CallStatus

    run(office)
    before = len(rows(office))
    broken = ScriptedProvider().on(Purpose.MEASURE, CallStatus.RATE_LIMITED, repeat=True)
    with pytest.raises(MeasurementRunFailed):
        extract_version(office.system, office.ver, broken)
    assert len(rows(office)) == before


def test_finding_the_same_value_twice_does_not_count_it_twice(office):
    run(office)
    ws = T.Workspace(ctx=office.ctx())
    T.tool_find_measurements(ws, "שכ", None, None, None)
    first = {mid for mid, m in ws.measurements.items() if m.row.table_index is not None
             and m.row.metric_kind == "rent_per_area"}
    T.tool_find_measurements(ws, "", ["rent_per_area"], None, None)
    table = [mid for mid, m in ws.measurements.items()
             if m.row.metric_kind == "rent_per_area" and m.row.table_index is not None]
    assert set(table) == first
    assert '"n": 4' in T.tool_compute(ws, "count", table + table)
