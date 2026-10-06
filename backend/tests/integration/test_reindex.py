"""Reading a processed document again (reindex): its reading is replaced, people's work is kept, and nothing
built on the old reading is served again."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.platform import pipeline
from app.platform.jobs import enqueue_reindex
from tests.conftest import login
from tests.factories import make_office
from tests.unit.test_docx_blocks import FIXTURE

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    groups = client.get("/api/admin/groups").json()["groups"]
    r = client.post("/api/documents", data={"group_id": groups[0]["id"]},
                    files={"files": (FIXTURE.name, FIXTURE.read_bytes(),
                                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 200, r.text
    a.version = r.json()["results"][0]["version_id"]
    a.document = r.json()["results"][0]["document_id"]
    while worker.run_one("test-worker"):
        pass
    return a


def scalar(office, sql, **p):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), p).scalar_one()


def test_reindex_replaces_the_reading_and_keeps_reviews(office):
    v = office.version
    assert scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v) in ("ready", "needs_review")
    assert scalar(office, "SELECT count(*) FROM document_blocks WHERE version_id = :v", v=v) > 0
    with tenant_tx(office.system) as conn:
        attr = conn.execute(text(
            "INSERT INTO attribute_definitions (office_id, key, label_he, value_type, source) VALUES (app_office(),"
            " 'k', 'תכונה', 'numeric', 'extracted') RETURNING id")).scalar_one()
        for status in ("verified", "needs_review"):
            conn.execute(text(
                "INSERT INTO facts (office_id, document_id, version_id, attribute_id, value_numeric, quote, source_path,"
                " extraction_version, status) VALUES (app_office(), :d, :v, :a, 1, 'q', CAST(:p AS jsonb), 'x7', :s)"),
                {"d": office.document, "v": v, "a": attr, "p": json.dumps({}), "s": status})
        conn.execute(text("INSERT INTO answer_cache (office_id, cache_key, payload) VALUES (app_office(), 'k', '{}')"))
        conn.execute(text("UPDATE document_blocks SET text = 'קריאה ישנה' WHERE version_id = :v"), {"v": v})
    before = scalar(office, "SELECT version FROM office_data_versions")
    status_before = scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v)

    with tenant_tx(office.system) as conn:
        assert enqueue_reindex(conn, v, pipeline.INGESTION_VERSION)
        assert not enqueue_reindex(conn, v, pipeline.INGESTION_VERSION)  # already queued: not twice
    while worker.run_one("test-worker"):
        pass

    assert scalar(office, "SELECT count(*) FROM document_blocks WHERE version_id = :v AND text = 'קריאה ישנה'",
                  v=v) == 0
    assert scalar(office, "SELECT ingestion->>'ingestion_version' FROM document_versions WHERE id = :v",
                  v=v) == pipeline.INGESTION_VERSION
    assert scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v) == status_before
    assert scalar(office, "SELECT count(*) FROM facts WHERE status = 'verified'") == 1  # a person's review stays
    assert scalar(office, "SELECT count(*) FROM facts WHERE status = 'needs_review'") == 0  # old reading's guess goes
    assert scalar(office, "SELECT count(*) FROM answer_cache") == 0
    assert scalar(office, "SELECT version FROM office_data_versions") > before
    assert scalar(office, "SELECT count(*) FROM chunks WHERE version_id = :v AND embedding IS NULL", v=v) == 0


def test_admin_reprocess_queues_only_outdated_versions_unless_all(office, client):
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 0
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion, '{ingestion_version}',"
                          " '\"old\"') WHERE id = :v"), {"v": office.version})
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 1
