"""Login, sessions, and tenant context per request (U3)."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office

pytestmark = pytest.mark.db


@pytest.fixture
def offices(db):
    return make_office(db, "משרד א", "admin-a@example.test"), make_office(db, "משרד ב", "admin-b@example.test")


def test_login_sets_cookie_and_me_returns_office(client, offices):
    a, _ = offices
    login(client, "admin-a@example.test")
    me = client.get("/api/auth/me").json()
    assert me["office"]["id"] == str(a.office_id)
    assert me["user"]["role"] == "admin"
    assert "rag_session" in client.cookies


def test_wrong_password_is_generic_and_audited(client, offices):
    a, _ = offices
    r = client.post("/api/auth/login", json={"email": "admin-a@example.test", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "פרטי ההתחברות שגויים"
    r2 = client.post("/api/auth/login", json={"email": "ghost@example.test", "password": "nope"})
    assert r2.status_code == 401 and r2.json()["detail"] == r.json()["detail"]
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'login_failed'")).scalar() == 1


def test_repeated_failures_lock_the_account(client, offices):
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "admin-a@example.test", "password": "nope"})
    r = client.post("/api/auth/login", json={"email": "admin-a@example.test", "password": "secret-pass"})
    assert r.status_code == 429


def test_forged_office_parameter_is_ignored(client, offices):
    a, b = offices
    login(client, "admin-a@example.test")
    me = client.get("/api/auth/me", params={"office_id": str(b.office_id)}).json()
    assert me["office"]["id"] == str(a.office_id)


def test_expired_or_deleted_session_is_rejected(client, offices):
    a, _ = offices
    login(client, "admin-a@example.test")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE sessions SET expires_at = now() - interval '1 minute'"))
    assert client.get("/api/auth/me").status_code == 401
    login(client, "admin-a@example.test")
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_no_cookie_is_401(client, offices):
    assert client.get("/api/auth/me").status_code == 401


def test_concurrent_requests_on_one_connection_never_cross_offices(offices):
    """Pool of size 1: the GUC is transaction-local, so alternating offices never leak."""
    from sqlalchemy import create_engine

    from app.config import get_settings

    a, b = offices
    engine = create_engine(get_settings().database_url, pool_size=1, max_overflow=0)
    errors = []

    def worker(office, other):
        for _ in range(25):
            with tenant_tx(office.ctx(), engine) as conn:
                seen = set(conn.execute(text("SELECT office_id FROM users")).scalars())
                if seen != {office.office_id}:
                    errors.append(seen)
            with engine.connect() as conn:  # no tenant context on the same pooled connection
                if conn.execute(text("SELECT count(*) FROM users")).scalar() != 0:
                    errors.append("leak")

    threads = [threading.Thread(target=worker, args=(a, b)), threading.Thread(target=worker, args=(b, a))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    engine.dispose()
    assert errors == []


def test_health_reports_runtime_role(client):
    body = client.get("/api/health").json()
    assert body == {"status": "ok", "db_role": "rag_app", "db_bypass_rls": False}


def test_separate_clients_keep_separate_sessions(db, offices):
    from app.main import create_app

    with TestClient(create_app()) as c1, TestClient(create_app()) as c2:
        login(c1, "admin-a@example.test")
        login(c2, "admin-b@example.test")
        assert c1.get("/api/auth/me").json()["office"]["name"] == "משרד א"
        assert c2.get("/api/auth/me").json()["office"]["name"] == "משרד ב"
