"""Review queue, approve/correct/reject, dedup resolution, group scoping (U7)."""

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_dedup import ROW_SHARED, add_version, publish

pytestmark = pytest.mark.db

ROW_B = ["ביאליק 3", "6158/41", "05/02/2024", "דירה", "3", "50", "נטו", "1,000,000", "21,000"]
GROSS = ["הרואה 5", "6158/40", "10/01/2024", "דירה", "4", "110", "ברוטו", "2,375,000", ""]


@pytest.fixture
def setup(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    doc, ver = add_version(a, a.default_group_id, [ROW_SHARED, ROW_B], "1" * 64)
    publish(a, doc, ver)
    return a, b, doc, ver


def occ_id(office, address, data_kind="transaction_price"):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT id FROM occurrences WHERE address = :a AND data_kind = :k"),
                            {"a": address, "k": data_kind}).scalar_one()


def data_version(office):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT version FROM office_data_versions")).scalar_one()


def test_queue_lists_records_with_flags(client, setup):
    a, *_ = setup
    login(client, "admin-a@example.test")
    items = client.get("/api/review/queue").json()["items"]
    records = {i["summary"]["address"]: i for i in items if i["kind"] == "record"}
    assert records["ביאליק 3"]["flags"]["conflict"] is True
    assert records["ביאליק 3"]["verification_status"] == "needs_review"
    assert records["הרואה 5"]["verification_status"] == "auto_extracted"


def test_record_detail_shows_original_normalized_and_source(client, setup):
    a, *_ = setup
    login(client, "admin-a@example.test")
    d = client.get(f"/api/review/records/{occ_id(a, 'ביאליק 3')}").json()
    price = next(f for f in d["fields"] if f["field"] == "price")
    assert price["original_text"] == "1,000,000" and price["normalized_value"] == "1000000.00"
    assert d["computed_price_per_sqm"] == "20000.00" and d["stated_price_per_sqm"] == "21000.00"
    assert d["file_url"].endswith("#page=2") and d["is_docx"] is False


def test_approve_makes_record_verified_and_bumps_data_version(client, setup):
    a, *_ = setup
    login(client, "admin-a@example.test")
    before = data_version(a)
    r = client.post(f"/api/review/records/{occ_id(a, 'הרואה 5')}/approve", json={"note": "נבדק"})
    assert r.json()["verification_status"] == "human_verified"
    assert data_version(a) == before + 1


def test_correct_area_recomputes_and_keeps_history(client, setup):
    a, *_ = setup
    login(client, "admin-a@example.test")
    oid = occ_id(a, "ביאליק 3")
    bad = client.post(f"/api/review/records/{oid}/correct", json={"field": "area", "value": "abc", "note": "x"})
    assert bad.status_code == 422 and "מספר" in bad.json()["detail"]
    no_note = client.post(f"/api/review/records/{oid}/correct", json={"field": "area", "value": "47.62", "note": " "})
    assert no_note.status_code == 422
    d = client.post(f"/api/review/records/{oid}/correct",
                    json={"field": "area", "value": "47.62", "note": "שטח לפי נסח"}).json()
    assert d["verification_status"] == "corrected"
    assert d["computed_price_per_sqm"] == "20999.58" and d["conflict_flag"] is False
    area = next(f for f in d["fields"] if f["field"] == "area")
    assert area["status"] == "corrected" and area["previous"][0]["value"] == "50.00"
    assert d["calc_definition"] == "price / area = 1000000.00 / 47.62"


def test_reject_requires_note(client, setup):
    a, *_ = setup
    login(client, "admin-a@example.test")
    oid = occ_id(a, "ביאליק 3")
    assert client.post(f"/api/review/records/{oid}/reject", json={"note": ""}).status_code == 422
    assert client.post(f"/api/review/records/{oid}/reject", json={"note": "שורת סיכום"}).json()[
        "verification_status"] == "rejected"


def test_version_becomes_ready_when_flags_resolved(client, setup):
    a, _, _, ver = setup
    login(client, "admin-a@example.test")
    client.post(f"/api/review/records/{occ_id(a, 'ביאליק 3')}/approve", json={})
    with tenant_tx(a.system) as conn:
        assert conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": ver}).scalar() == "ready"


def test_other_office_cannot_read_or_modify(client, setup):
    a, *_ = setup
    oid = occ_id(a, "ביאליק 3")
    login(client, "admin-b@example.test")
    assert client.get(f"/api/review/records/{oid}").status_code == 404
    assert client.post(f"/api/review/records/{oid}/approve", json={}).status_code == 404
    assert client.get("/api/review/queue").json()["items"] == []


def test_merge_and_keep_separate(client, setup):
    a, *_ = setup
    doc2, ver2 = add_version(a, a.default_group_id, [GROSS], "2" * 64)
    publish(a, doc2, ver2)
    login(client, "admin-a@example.test")
    cands = [i for i in client.get("/api/review/queue").json()["items"] if i["kind"] == "dedup"]
    assert len(cands) == 1 and len(cands[0]["a"]["sources"]) == 1
    assert client.post(f"/api/review/dedup/{cands[0]['id']}/merge").json() == {"ok": True}
    with tenant_tx(a.system) as conn:
        assert conn.execute(text("SELECT count(DISTINCT transaction_id) FROM occurrences WHERE address = 'הרואה 5'")).scalar() == 1
    assert [i for i in client.get("/api/review/queue").json()["items"] if i["kind"] == "dedup"] == []


def test_group_scope_on_merged_records(client, db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    make_user(a, "g1@example.test", [g1])
    d1, v1 = add_version(a, g1, [ROW_SHARED], "1" * 64)
    d2, v2 = add_version(a, g2, [ROW_SHARED], "2" * 64)
    d3, v3 = add_version(a, g2, [GROSS], "3" * 64)
    for d, v in ((d1, v1), (d2, v2), (d3, v3)):
        publish(a, d, v)
    with tenant_tx(a.system) as conn:
        shared = conn.execute(text("SELECT id FROM occurrences WHERE document_id = :d AND address = 'הרואה 5'"),
                              {"d": d1}).scalar_one()
        cand = conn.execute(text("SELECT id FROM dedup_candidates")).scalar_one()
    login(client, "g1@example.test")
    assert client.post(f"/api/review/dedup/{cand}/merge").status_code == 404
    r = client.post(f"/api/review/records/{shared}/correct", json={"field": "area", "value": "96", "note": "x"})
    assert r.status_code == 403
    assert client.get(f"/api/review/records/{shared}").status_code == 404
