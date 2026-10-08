"""The reading report on the documents API and the PDF page/region view (U6).

What a version's reading covered is reported per page and region (unread and uncertain regions with their reason,
corrected text with its count, repeated pictures read once), and a PDF page or a stored region is served as an
image only to someone who may see the version. Synthetic fixtures only; OCR is off and no model is called."""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction import fontmap
from app.platform import documents, pipeline
from app.platform.storage import get_storage, storage_key
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_fontmap_repair import BROKEN, CLEAN
from tests.unit.test_pdf_blocks import B1
from tests.unit.test_regions import R1

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_office(db, "משרד ב", "admin-b@example.test")
    other = make_group(a, "קבוצה אחרת")
    make_user(a, "emp-other@example.test", [other])
    make_user(a, "emp@example.test", [a.default_group_id])
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    return a


def ingest(office, path, title: str | None = None) -> tuple[str, str]:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(office.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(office.ctx()) as conn:
        doc = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g, :t)"
                                " RETURNING id"), {"g": office.default_group_id, "t": title or path.stem}).scalar_one()
        ver = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, :f, 'application/pdf', :b, :k,"
            " :u) RETURNING id"),
            {"d": doc, "s": sha, "f": path.name, "b": len(data), "k": key, "u": office.admin_id}).scalar_one()
    pipeline.process_version(office.office_id, ver)
    return str(doc), str(ver)


def broken_fontmap_repaired(monkeypatch):
    """Font-map repair verified by the intact twin's words standing in for OCR of the glyphs."""
    from tests.unit.test_fontmap import CleanTwinReader

    real = fontmap.detect_and_repair
    monkeypatch.setattr(fontmap, "detect_and_repair", lambda words, pdf_doc=None, config=None, reader=None:
                        real(words, pdf_doc, config, CleanTwinReader(CLEAN)))


def reading_of(client, doc: str) -> dict:
    return client.get(f"/api/documents/{doc}").json()["current_version"]["reading"]


def png(response) -> Image.Image:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"].startswith("private")
    return Image.open(io.BytesIO(response.content))


def source_views(office) -> int:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'source_view'")).scalar_one()


# --- the reading report -------------------------------------------------------------------------------------------

def test_a_partly_read_pdf_lists_its_page_and_region_with_the_reason(client, office):
    doc, ver = ingest(office, R1)
    login(client, "admin-a@example.test")
    reading = reading_of(client, doc)
    assert reading["partial"] is True
    pages = {p["page"]: p for p in reading["coverage"]}
    assert set(pages) == {1}  # the logo repeated on every page is read once, not reported per page
    region = next(r for r in pages[1]["regions"] if r["status"] == "unread")
    assert region["kind"] == "image" and region["reason"] and len(region["bbox"]) == 4
    assert isinstance(region["block"], int) and pages[1]["ok"] is True
    assert reading["repeated_images"] >= 1
    # the region's block is the one the source view opens
    blocks = client.get(f"/api/documents/{doc}/versions/{ver}/blocks",
                        params={"start": region["block"], "end": region["block"]}).json()["blocks"]
    assert blocks[0]["status"] == "unread" and blocks[0]["bbox"] == region["bbox"]
    assert blocks[0]["region_url"] == f"/api/documents/{doc}/versions/{ver}/regions/{region['block']}/image"
    assert blocks[0]["page_url"] == f"/api/documents/{doc}/versions/{ver}/pages/1/image"


def test_a_fully_read_pdf_has_no_partial_badge_and_no_regions(client, office):
    doc, _ = ingest(office, B1)
    login(client, "admin-a@example.test")
    reading = reading_of(client, doc)
    assert reading["partial"] is False and reading["coverage"] == []
    assert reading["corrected_blocks"] == 0 and reading["uncertain_blocks"] == 0


def test_a_corrected_pdf_reports_the_corrected_text_with_its_count(client, office, monkeypatch):
    broken_fontmap_repaired(monkeypatch)
    doc, ver = ingest(office, BROKEN, "מגדל הנחל")
    login(client, "admin-a@example.test")
    reading = reading_of(client, doc)
    assert reading["partial"] is False and reading["uncertain_blocks"] == 0
    assert reading["corrections"] == 2  # one accepted mapping per font
    with tenant_tx(office.system) as conn:
        corrected = conn.execute(text("SELECT count(*) FROM document_blocks WHERE version_id = :v"
                                      " AND original_text IS NOT NULL"), {"v": ver}).scalar_one()
    assert reading["corrected_blocks"] == corrected > 0
    assert [(p["page"], p["corrected"], p["regions"]) for p in reading["coverage"]] == [(1, corrected, [])]


def test_an_older_report_without_coverage_still_lists_its_unread_pictures(client, office):
    doc, ver = ingest(office, B1)
    with tenant_tx(office.system) as conn:  # a report written before per-page coverage existed
        conn.execute(text("UPDATE document_versions SET ingestion = CAST(:g AS jsonb) WHERE id = :v"),
                     {"v": ver, "g": '{"partial": true, "images": {"unread": 1}, "unread": [{"media": null,'
                                     ' "section": "3. תיאור", "reason": "סיבה", "page": 2}]}'})
    login(client, "admin-a@example.test")
    reading = reading_of(client, doc)
    assert reading["partial"] is True
    assert reading["coverage"] == [{"page": 2, "ok": True, "method": None, "corrected": 0, "regions": [
        {"block": None, "kind": "image", "status": "unread", "reason": "סיבה", "bbox": None, "section": "3. תיאור",
         "media": None}]}]


def test_the_model_sees_partial_uncertain_and_corrected_reading_per_document(office, monkeypatch):
    partly, _ = ingest(office, R1, "שומה חלקית")
    broken_fontmap_repaired(monkeypatch)
    corrected, _ = ingest(office, BROKEN, "שומה מתוקנת")
    whole, _ = ingest(office, B1, "שומה מלאה")
    out = T.tool_list_documents(T.Workspace(ctx=office.ctx()), None)
    line = {d: next(x for x in out.splitlines() if d in x) for d in (partly, corrected, whole)}
    assert "קריאה: נקרא חלקית" in line[partly] and "לא נקראו בעמוד 1" in line[partly]
    assert "קריאה: נקרא במלואו" in line[corrected] and "טקסט תוקן" in line[corrected]
    assert "קריאה: נקרא במלואו" in line[whole] and "(" not in line[whole].split("קריאה:")[1].split("|")[0]


# --- the page and region view -----------------------------------------------------------------------------------

def test_a_permitted_user_gets_the_page_and_a_region_as_images(client, office):
    doc, ver = ingest(office, R1)
    login(client, "emp@example.test")
    base = f"/api/documents/{doc}/versions/{ver}"
    before = source_views(office)
    page = png(client.get(f"{base}/pages/1/image"))
    assert max(page.size) <= documents.RENDER_MAX_SIDE
    region = next(r for p in reading_of(client, doc)["coverage"] for r in p["regions"])
    crop = png(client.get(f"{base}/regions/{region['block']}/image"))
    assert crop.size[0] < page.size[0] * documents.REGION_SCALE / documents.PAGE_SCALE
    assert max(crop.size) <= documents.RENDER_MAX_SIDE
    assert source_views(office) == before + 2  # one audit per image request


def test_the_view_is_refused_to_users_outside_the_group_other_offices_and_deleted_documents(client, office):
    doc, ver = ingest(office, R1)
    base = f"/api/documents/{doc}/versions/{ver}"
    block = reading_of(login(client, "admin-a@example.test"), doc)["coverage"][0]["regions"][0]["block"]
    urls = (f"{base}/pages/1/image", f"{base}/regions/{block}/image")
    for email in ("emp-other@example.test", "admin-b@example.test"):
        client.post("/api/auth/logout")
        login(client, email)
        assert [client.get(u).status_code for u in urls] == [404, 404]
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    assert client.delete(f"/api/documents/{doc}").status_code == 200
    assert [client.get(u).status_code for u in urls] == [404, 404]


@pytest.mark.parametrize("path", ["pages/0/image", "pages/11/image", "pages/-1/image", "pages/x/image",
                                  "regions/9999/image", "regions/-1/image"])
def test_a_page_outside_the_document_or_an_unknown_region_is_not_found(client, office, path):
    doc, ver = ingest(office, R1)  # ten pages
    login(client, "admin-a@example.test")
    assert client.get(f"/api/documents/{doc}/versions/{ver}/{path}").status_code in (404, 422)
    assert client.get(f"/api/documents/{doc}/versions/{ver}/pages/10/image").status_code == 200


def test_a_region_without_a_place_on_a_page_is_not_found(client, office):
    doc, ver = ingest(office, B1)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET bbox = NULL WHERE version_id = :v AND block_index = 0"),
                     {"v": ver})
    login(client, "admin-a@example.test")
    assert client.get(f"/api/documents/{doc}/versions/{ver}/regions/0/image").status_code == 404
    assert client.get(f"/api/documents/{doc}/versions/{ver}/regions/1/image").status_code == 200
