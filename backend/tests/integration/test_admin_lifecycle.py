"""Versions, deletion, user/group admin, provider setting and status, coverage (U12, U2)."""

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_tx
from app.providers.llm import CallStatus, Purpose
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_dedup import add_version, publish
from tests.integration.test_numeric_answers import AE3, approve_all, ask
from tests.integration.test_search import add_chunks
from tests.support.scripted_provider import ScriptedProvider

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
    assert s["cloud_llm_enabled"] is False and s["mode"] == "demo"
    assert client.put("/api/admin/settings", json={"cloud_llm_enabled": True}).status_code == 422
    on = client.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True}).json()
    assert on["cloud_llm_enabled"] and on["mode"] == "error" and on["acknowledged_at"]
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


# ---------------- Provider status and connection test (U2, R8, KTD5) ----------------

# Never a real key: the "." keeps it out of the secrets scan pattern, and nothing here reaches the network.
FAKE_KEY = "sk-test-FAKE.not-a-real-key-0123456789"
CONTENT_Q = "מה היו שיקולי השמאי לגבי היטל השבחה?"


@pytest.fixture
def no_key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai")
    monkeypatch.setattr(s, "openai_api_key", SecretStr(""))
    monkeypatch.setattr(s, "anthropic_api_key", SecretStr(""))

    def no_network():
        raise AssertionError("no provider may be constructed without a key")

    monkeypatch.setattr("app.providers.llm.get_selected_provider", no_network)


@pytest.fixture
def fake_key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai")
    monkeypatch.setattr(s, "openai_api_key", SecretStr(FAKE_KEY))
    scripted = ScriptedProvider()
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: scripted)
    return scripted


def echo(instructions, input):
    return {"echo": input}


def enable(client):
    r = client.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True})
    assert r.status_code == 200, r.text
    return r.json()


def assert_no_key(response):
    assert FAKE_KEY not in response.text and "FAKE" not in response.text


def test_no_key_status_and_test_without_network(client, office, no_key):
    login(client, "admin-a@example.test")
    s = client.get("/api/admin/settings").json()
    assert s["key_present"] is False and s["mode"] == "demo" and s["provider_name"] == "OpenAI"
    assert s["model"] == get_settings().openai_model and s["last_test"] is None
    r = client.post("/api/admin/provider/test")
    assert r.status_code == 200, r.text
    t = r.json()["last_test"]
    assert t["ok"] is False and t["status"] == "missing_key" and t["tested_at"]
    assert t["provider"] == "openai" and t["model"] == get_settings().openai_model
    assert r.json()["mode"] == "demo"  # allowed while cloud use is off; no office content is sent
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'provider_test'")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM provider_usage")).scalar() == 0


def test_limited_mode_without_demo(client, office, no_key, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_mode", False)
    login(client, "admin-a@example.test")
    assert client.get("/api/admin/settings").json()["mode"] == "limited"


def test_enabled_without_key_is_error_and_answers_limited_never_demo(client, office, no_key):
    add_chunks(office, office.default_group_id,
               ["4. שיקולי השמאי: בשכונת חרוזים השמאי המכריע קבע הפחתה של 10% בשל היטל השבחה."], "1" * 64)
    login(client, "admin-a@example.test")
    on = enable(client)
    assert on["mode"] == "error" and on["mode_status"] == "missing_key" and on["key_present"] is False
    a = ask(client, CONTENT_Q)["answer"]
    assert a["provider"] != "mock" and a["demo"] is False and "לפי מסמכי המשרד" not in a["text"]
    assert any("מפתח" in lim and "OpenAI" in lim for lim in a["limitations"]), a["limitations"]
    assert ask(client, CONTENT_Q)["answer"].get("cached") is None  # an error-mode answer is not cached


def test_auth_failure_is_persisted_shown_and_never_leaks_the_key(client, office, fake_key):
    fake_key.on(Purpose.TEST, CallStatus.AUTH)
    login(client, "admin-a@example.test")
    enable(client)
    r = client.post("/api/admin/provider/test")
    assert r.status_code == 200 and r.json()["mode"] == "error" and r.json()["mode_status"] == "auth"
    assert_no_key(r)
    assert fake_key.calls and fake_key.calls[0].purpose == Purpose.TEST
    assert all(word not in fake_key.calls[0].input for word in ("חרוזים", "שמאי"))  # synthetic prompt only
    s = client.get("/api/admin/settings")
    assert s.json()["last_test"]["status"] == "auth" and s.json()["last_test"]["ok"] is False
    assert s.json()["key_present"] is True
    assert_no_key(s)
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT provider_test_status FROM office_settings")).scalar() == "auth"
        details = conn.execute(text("SELECT details::text FROM audit_events WHERE action = 'provider_test'")).scalar()
        assert "auth" in details and FAKE_KEY not in details
        assert conn.execute(text("SELECT ok FROM provider_usage WHERE purpose = 'test'")).scalar() is False


def test_ok_test_gives_cloud_mode_with_time(client, office, fake_key):
    fake_key.on(Purpose.TEST, echo)
    login(client, "admin-a@example.test")
    on = enable(client)
    assert on["mode"] == "cloud" and on["untested"] is True and on["last_test"] is None
    r = client.post("/api/admin/provider/test")
    s = r.json()
    assert s["mode"] == "cloud" and s["untested"] is False and s["mode_status"] is None
    assert s["last_test"]["ok"] is True and s["last_test"]["status"] == "ok" and s["last_test"]["tested_at"]
    assert_no_key(r)


def test_wrong_echo_is_invalid(client, office, fake_key):
    fake_key.on(Purpose.TEST, {"echo": "משהו אחר"})
    login(client, "admin-a@example.test")
    r = client.post("/api/admin/provider/test").json()
    assert r["last_test"]["status"] == "invalid" and r["mode"] == "demo"


def test_employee_cannot_run_provider_test(client, office, fake_key):
    make_user(office, "e@example.test", [])
    login(client, "e@example.test")
    assert client.post("/api/admin/provider/test").status_code == 403
    assert fake_key.calls == []


def test_other_office_test_result_is_invisible(client, office, fake_key):
    fake_key.on(Purpose.TEST, CallStatus.AUTH)
    login(client, "admin-b@example.test")
    assert client.post("/api/admin/provider/test").json()["last_test"]["status"] == "auth"
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    assert client.get("/api/admin/settings").json()["last_test"] is None
    with tenant_tx(office.ctx()) as conn:
        rows = conn.execute(text("SELECT provider_test_status FROM office_settings")).all()
    assert rows == [(None,)]
