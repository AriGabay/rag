"""Versions, deletion, user/group admin, provider setting, coverage (U12)."""

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_dedup import add_version, publish
from tests.integration.test_numeric_answers import AE3, approve_all, ask

pytestmark = pytest.mark.db
Q = "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים"


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_office(db, "משרד ב", "admin-b@example.test")
    return a


def test_new_version_replaces_old_in_answers(client, office):
    doc, v1 = add_version(office, office.default_group_id, AE3, "1" * 64)
    publish(office, doc, v1)
    approve_all(office)
    with tenant_tx(office.system) as conn:
        v2 = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type, size_bytes,"
            " storage_key, status) SELECT office_id, document_id, 2, :s, 'v2.pdf', mime_type, 1, 'k', 'processing'"
            " FROM document_versions WHERE id = :v RETURNING id"), {"s": "2" * 64, "v": v1}).scalar_one()
        for t in ("pages", "extracted_tables"):
            cols = "page_no, text, method, quality, ok" if t == "pages" else "table_index, page_start, page_end, structure"
            conn.execute(text(f"INSERT INTO {t} (office_id, document_id, version_id, {cols}) SELECT office_id,"
                              f" document_id, :n, {cols} FROM {t} WHERE version_id = :v"), {"n": v2, "v": v1})
        conn.execute(text("UPDATE extracted_tables SET structure = jsonb_set(structure, '{rows,0,cells,7}', '\"1,200,000\"')"
                          " WHERE version_id = :n"), {"n": v2})
    publish(office, doc, v2)
    approve_all(office)
    login(client, "admin-a@example.test")
    a = ask(client, Q)["answer"]
    assert a["numeric"]["mean_price_per_sqm"] == "27000.00"
    assert {s["version_id"] for s in a["sources"]} == {str(v2)}
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": v1}).scalar() == "superseded"


def test_delete_removes_from_answers_and_file_access(client, office):
    doc, v1 = add_version(office, office.default_group_id, AE3, "1" * 64)
    publish(office, doc, v1)
    approve_all(office)
    login(client, "admin-a@example.test")
    assert ask(client, Q)["answer"]["numeric"]["record_count"] == 2
    assert client.delete(f"/api/documents/{doc}").json() == {"ok": True}
    assert ask(client, Q)["answer"]["kind"] == "abstain"
    assert client.get(f"/api/documents/{doc}/versions/{v1}/file").status_code == 404


def test_removing_group_membership_removes_sources(client, office):
    g1 = make_group(office, "G1")
    uid = make_user(office, "e@example.test", [g1])
    doc, v1 = add_version(office, g1, AE3, "1" * 64)
    publish(office, doc, v1)
    approve_all(office)
    login(client, "e@example.test")
    assert ask(client, Q)["answer"]["numeric"]["record_count"] == 2
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    assert client.patch(f"/api/admin/users/{uid}", json={"group_ids": []}).json() == {"ok": True}
    client.post("/api/auth/logout")
    login(client, "e@example.test")
    assert ask(client, Q)["answer"]["kind"] == "abstain"


def test_employee_cannot_use_admin_endpoints(client, office):
    make_user(office, "e@example.test", [])
    login(client, "e@example.test")
    for method, url in (("get", "/api/admin/users"), ("get", "/api/admin/settings"), ("get", "/api/admin/coverage"),
                        ("post", "/api/admin/groups")):
        assert getattr(client, method)(url, **({"json": {"name": "x"}} if method == "post" else {})).status_code == 403


def test_user_lifecycle_and_deactivation_revokes_sessions(client, office):
    login(client, "admin-a@example.test")
    g = client.post("/api/admin/groups", json={"name": "פרויקטים"}).json()["id"]
    uid = client.post("/api/admin/users", json={"email": "new@example.test", "full_name": "חדש", "password": "longpass1",
                                                "role": "employee", "can_upload": True, "group_ids": [g]}).json()["id"]
    assert client.post("/api/admin/users", json={"email": "new@example.test", "full_name": "x", "password": "longpass1",
                                                 "role": "employee"}).status_code == 409
    users = {u["email"]: u for u in client.get("/api/admin/users").json()["users"]}
    assert users["new@example.test"]["group_ids"] == [g] and users["new@example.test"]["can_upload"]
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app()) as other:
        login(other, "new@example.test", "longpass1")
        assert client.patch(f"/api/admin/users/{uid}", json={"is_active": False}).json() == {"ok": True}
        assert other.get("/api/auth/me").status_code == 401
    assert client.patch(f"/api/admin/users/{office.admin_id}", json={"role": "employee"}).status_code == 422


def test_cloud_setting_requires_acknowledgment(client, office):
    login(client, "admin-a@example.test")
    s = client.get("/api/admin/settings").json()
    assert s["cloud_llm_enabled"] is False and s["effective_provider"] == "demo_mock"
    assert client.put("/api/admin/settings", json={"cloud_llm_enabled": True}).status_code == 422
    on = client.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True}).json()
    assert on["cloud_llm_enabled"] and on["effective_provider"] == "enabled_no_key" and on["acknowledged_at"]
    off = client.put("/api/admin/settings", json={"cloud_llm_enabled": False, "acknowledge": False})
    assert off.status_code == 200 and off.json()["cloud_llm_enabled"] is False
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'provider_setting'")).scalar() == 2


def test_coverage_summary(client, office):
    doc, v1 = add_version(office, office.default_group_id, AE3, "1" * 64)
    publish(office, doc, v1)
    login(client, "admin-a@example.test")
    c = client.get("/api/admin/coverage").json()
    assert c["records"]["total"] == 3 and c["records"]["verified"] == 0
    assert c["documents_by_status"] == {"ready": 1}
