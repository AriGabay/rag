"""The geometry-only backfill of versions read before positions existed (U3, KTD3, R13).

A version is read with positions, then its positions are removed, which leaves it exactly as a version read before
them: same blocks, tables and ``reading_id``, no page geometry, spans or cell boxes, no ``positions`` marker. The
backfill job (queued by the admin, run by the worker) must give it back exactly the positions the reading stored,
keep its reading, record what aligned, change nothing a second time, and leave earlier citations openable. All
documents are synthetic (``tests/fixtures/positions``, ``tests/fixtures/fontmap``); OCR and the vision model are off.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.extraction import fontmap
from app.extraction.geometry import POSITIONS_VERSION
from app.platform import pipeline
from app.platform.jobs import enqueue_positions
from tests.conftest import login
from tests.factories import make_office
from tests.unit.test_positions_backfill import BROKEN, CLEAN, P1

pytestmark = pytest.mark.db

_STRIP = (
    "UPDATE pages SET mediabox = NULL, cropbox = NULL, rotation = NULL, display_width = NULL, display_height = NULL,"
    " printed_label = NULL, geometry_issue = NULL WHERE version_id = :v",
    "UPDATE document_blocks SET spans = NULL WHERE version_id = :v",
    "UPDATE extracted_tables SET structure = (structure - 'header_boxes') || jsonb_build_object('rows', COALESCE(("
    "SELECT jsonb_agg(r - 'cell_boxes' ORDER BY n) FROM jsonb_array_elements(structure->'rows') WITH ORDINALITY"
    " AS x(r, n)), '[]'::jsonb)) WHERE version_id = :v",
    "UPDATE document_versions SET ingestion = ingestion - 'positions' WHERE id = :v",
)


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("no OCR, vision or model call")

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(pipeline, "reindex_version", forbidden)


def drain() -> None:
    while worker.run_one("test-worker"):
        pass


def snapshot(office, v) -> dict:
    """Everything the backfill may write, and everything it must keep."""
    with tenant_tx(office.system) as conn:
        return {
            "pages": [tuple(r) for r in conn.execute(text(
                "SELECT page_no, text, method, mediabox, cropbox, rotation, display_width, display_height,"
                " printed_label, geometry_issue FROM pages WHERE version_id = :v ORDER BY page_no"), {"v": v})],
            "blocks": [tuple(r) for r in conn.execute(text(
                "SELECT block_index, kind, text, bbox, method, spans FROM document_blocks WHERE version_id = :v"
                " ORDER BY block_index"), {"v": v})],
            "tables": [tuple(r) for r in conn.execute(text(
                "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v ORDER BY table_index"),
                {"v": v})],
            "chunks": [tuple(r) for r in conn.execute(text(
                "SELECT chunk_index, text, block_start, block_end FROM chunks WHERE version_id = :v"
                " ORDER BY chunk_index"), {"v": v})],
            "ingestion": conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"),
                                      {"v": v}).scalar_one(),
            "status": conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": v}).scalar_one(),
        }


def strip_positions(office, v) -> None:
    with tenant_tx(office.system) as conn:
        for sql in _STRIP:
            conn.execute(text(sql), {"v": v})


def positions_jobs(office) -> list[tuple]:
    with tenant_tx(office.system) as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT version_id, status, last_error FROM jobs WHERE kind = 'positions' ORDER BY created_at"))]


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    r = client.post("/api/documents", data={"group_id": str(a.default_group_id)},
                    files={"files": (P1.name, P1.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    a.version = r.json()["results"][0]["version_id"]
    a.document = r.json()["results"][0]["document_id"]
    drain()
    a.fresh = snapshot(a, a.version)
    strip_positions(a, a.version)
    return a


def test_a_version_without_positions_gets_the_positions_of_a_fresh_reading_and_keeps_its_reading(office, client):
    v = office.version
    fresh = office.fresh
    assert any(b[5] for b in fresh["blocks"]) and fresh["tables"]
    old = snapshot(office, v)
    assert not any(b[5] for b in old["blocks"]) and "positions" not in old["ingestion"]

    r = client.post("/api/admin/positions")
    assert r.status_code == 200 and r.json()["queued"] == 1 and r.json()["versions"] == [v]
    drain()
    after = snapshot(office, v)

    assert after["blocks"] == fresh["blocks"]  # spans back, block numbers and texts unchanged
    assert after["tables"] == fresh["tables"]  # cell boxes and header boxes back
    assert after["pages"] == fresh["pages"]  # page geometry and printed numbers back
    assert after["chunks"] == old["chunks"] and after["status"] == old["status"]
    ing = after["ingestion"]
    assert ing["reading_id"] == old["ingestion"]["reading_id"]
    assert ing["ingestion_version"] == old["ingestion"]["ingestion_version"]
    assert ing["positions"] == POSITIONS_VERSION
    record = ing["positions_backfill"]
    assert record["blocks"]["aligned"] == sum(1 for b in fresh["blocks"] if b[5]) and record["blocks"]["unaligned"] == 0
    assert record["tables"] == {"aligned": len(fresh["tables"]), "unaligned": 0, "no_positions": 0}
    assert [(j[1], j[2]) for j in positions_jobs(office)] == [("done", None)]

    summary = client.get("/api/admin/jobs").json()
    assert summary["positions"]["pdf_versions"] == 1 and summary["positions"]["with_positions"] == 1
    assert summary["positions"]["blocks"]["aligned"] == record["blocks"]["aligned"]
    assert {"kind": "positions", "status": "done", "count": 1} in summary["jobs"]


def test_running_the_backfill_twice_changes_nothing_the_second_time(office, client):
    v = office.version
    assert client.post("/api/admin/positions").json()["queued"] == 1
    drain()
    first = snapshot(office, v)
    assert client.post("/api/admin/positions").json()["queued"] == 0  # nothing left without positions
    with tenant_tx(office.system) as conn:  # even a job forced onto the same version
        assert enqueue_positions(conn, v)
    drain()
    assert snapshot(office, v) == first
    assert pipeline.backfill_positions(office.office_id, v) == "present"


def test_an_existing_citation_of_the_version_opens_its_blocks_not_stale(office, client):
    v, d = office.version, office.document
    reading = snapshot(office, v)["ingestion"]["reading_id"]
    before = client.get(f"/api/documents/{d}/versions/{v}/blocks", params={"reading_id": reading}).json()
    assert before["stale"] is False
    client.post("/api/admin/positions")
    drain()
    after = client.get(f"/api/documents/{d}/versions/{v}/blocks", params={"reading_id": reading}).json()
    assert after["stale"] is False and after["reading_id"] == reading
    assert [(b["index"], b["text"]) for b in after["blocks"]] == [(b["index"], b["text"]) for b in before["blocks"]]


def test_a_missing_stored_file_ends_the_job_without_changes_and_records_why(office, client):
    v = office.version
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET storage_key = :k WHERE id = :v"),
                     {"k": f"{office.office_id}/{'0' * 64}", "v": v})
    before = snapshot(office, v)
    assert client.post("/api/admin/positions").json()["queued"] == 1
    drain()
    assert snapshot(office, v) == before
    [(_, status, error)] = positions_jobs(office)
    assert status == "failed" and error == pipeline.MSG_POSITIONS_NO_FILE


def test_another_office_neither_queues_nor_sees_the_version(office, db, client):
    make_office(db, "משרד ב", "admin-b@example.test")
    login(client, "admin-b@example.test")
    assert client.post("/api/admin/positions").json()["queued"] == 0
    assert client.get("/api/admin/jobs").json()["positions"]["pdf_versions"] == 0
    assert positions_jobs(office) == []


def test_a_version_read_again_meanwhile_is_left_to_its_new_reading(office, monkeypatch):
    from app.platform import positions

    v = office.version
    real = positions.read_positions

    def reread_meanwhile(*a, **k):
        found = real(*a, **k)
        with tenant_tx(office.system) as conn:
            conn.execute(text("UPDATE document_versions SET ingestion = ingestion || '{\"reading_id\": \"new\"}'"
                              " WHERE id = :v"), {"v": v})
        return found

    monkeypatch.setattr(positions, "read_positions", reread_meanwhile)
    with tenant_tx(office.system) as conn:
        enqueue_positions(conn, v)
    drain()
    after = snapshot(office, v)
    assert not any(b[5] for b in after["blocks"]) and "positions" not in after["ingestion"]
    assert [j[1] for j in positions_jobs(office)] == ["done"]


# --- a font-map-repaired version -----------------------------------------------------------------------------------

@pytest.fixture
def repaired(db, monkeypatch):
    from app.platform.storage import get_storage, storage_key
    from tests.unit.test_fontmap import CleanTwinReader

    office = make_office(db, "משרד ג", "admin-c@example.test")
    real = fontmap.detect_and_repair
    monkeypatch.setattr(fontmap, "detect_and_repair",
                        lambda words, pdf_doc=None, config=None, reader=None:
                        real(words, pdf_doc, config, CleanTwinReader(CLEAN)))
    data = BROKEN.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(office.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(office.ctx()) as conn:
        doc = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g, :t)"
                                " RETURNING id"), {"g": office.default_group_id, "t": "מסמך לדוגמה"}).scalar_one()
        ver = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, :f, 'application/pdf', :b, :k,"
            " :u) RETURNING id"),
            {"d": doc, "s": sha, "f": BROKEN.name, "b": len(data), "k": key, "u": office.admin_id}).scalar_one()
    pipeline.process_version(office.office_id, ver)
    # from here on nothing may verify a font map again: the backfill reads through the recorded corrections
    monkeypatch.setattr(fontmap, "detect_and_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("OCR")))
    office.version = ver
    office.fresh = snapshot(office, ver)
    strip_positions(office, ver)
    return office


def test_a_repaired_version_aligns_through_its_recorded_corrections(repaired):
    v = repaired.version
    fresh = repaired.fresh
    assert fresh["ingestion"]["fontmap"]["corrections"]
    corrected = {b[0] for b in fresh["blocks"] if b[5]}
    with tenant_tx(repaired.system) as conn:
        enqueue_positions(conn, v)
    drain()
    after = snapshot(repaired, v)
    assert after["blocks"] == fresh["blocks"] and corrected
    assert after["ingestion"]["reading_id"] == fresh["ingestion"]["reading_id"]
    assert after["ingestion"]["positions_backfill"]["blocks"]["unaligned"] == 0


def test_a_repaired_block_whose_reread_still_differs_gets_no_spans_and_the_others_do(repaired):
    v = repaired.version
    with tenant_tx(repaired.system) as conn:
        odd = conn.execute(text(
            "SELECT block_index FROM document_blocks WHERE version_id = :v AND original_text IS NOT NULL"
            " ORDER BY block_index LIMIT 1"), {"v": v}).scalar_one()
        conn.execute(text("UPDATE document_blocks SET text = text || ' נוסף' WHERE version_id = :v"
                          " AND block_index = :i"), {"v": v, "i": odd})
        enqueue_positions(conn, v)
    drain()
    after = {b[0]: b[5] for b in snapshot(repaired, v)["blocks"]}
    others = {b[0] for b in repaired.fresh["blocks"] if b[5]} - {odd}
    assert after[odd] is None and others and all(after[i] for i in others)
    record = snapshot(repaired, v)["ingestion"]["positions_backfill"]
    assert record["blocks"]["unaligned"] == 1
