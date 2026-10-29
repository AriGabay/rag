"""The source viewer's page and region images without a database (U5, KTD5, R5, R11, R12).

A page renders at one of two scales, each with a long-side cap that lets a whole page reach it; the response names the
rendered page frame's size; images are never cached; a region request names the reading its anchor was made from and
is refused as stale when the stored reading differs; once the version is visible, a missing file or a page that cannot
be rendered is a typed failure, never the uniform 404 of a version the user may not see. Every served view is audited,
and opening a page of a current PDF read before positions queues the geometry-only backfill without waiting for it.

The routes run against fakes of the transaction, the reader, storage, audit and the job queue; the PDFs are synthetic
blank pages made here. The same behaviour against Postgres and RLS is in ``tests/integration/test_documents_api.py``.
"""

from __future__ import annotations

import io
import math
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pypdfium2 as pdfium
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.chat.reader import Version
from app.deps import get_ctx
from app.extraction import render
from app.extraction.geometry import POSITIONS_VERSION
from app.platform import documents

A4 = (595.0, 842.0)
A0 = (2384.0, 3370.0)
READING = "reading-1"


def synthetic_pdf(*sizes: tuple[float, float], cropbox: tuple | None = None) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for w, h in sizes:
        page = pdf.new_page(w, h)
        if cropbox is not None:
            page.set_cropbox(*cropbox)
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


def size_of(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


# --- scales -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tier", list(render.PAGE_SCALES))
@pytest.mark.parametrize("long_side", [792.0, 842.0, render.FULL_PAGE_POINTS])
def test_each_tier_lets_a_whole_page_reach_its_scale(tier, long_side):
    scale = render.PAGE_SCALES[tier]
    max_side = render.page_max_side(scale)
    assert max_side >= render.RENDER_MAX_SIDE  # never a smaller cap than the page view had
    assert render.applied_scale(long_side, scale, max_side) == scale


def test_the_zoom_tier_is_larger_than_the_normal_one():
    assert render.PAGE_SCALES["zoom"] > render.PAGE_SCALES["normal"] == render.PAGE_SCALE


def test_a_page_larger_than_the_cap_is_scaled_down_to_it():
    scale = render.PAGE_SCALES["zoom"]
    max_side = render.page_max_side(scale)
    assert render.applied_scale(A0[1], scale, max_side) == pytest.approx(max_side / A0[1])


@pytest.mark.parametrize("tier", list(render.PAGE_SCALES))
def test_a_page_renders_at_the_tier_with_its_display_size(tier):
    scale = render.PAGE_SCALES[tier]
    out = render.render_page(synthetic_pdf(A4), 1, scale=scale, max_side=render.page_max_side(scale))
    assert (out.width, out.height) == A4 and out.scale == scale
    assert size_of(out.png) == (math.ceil(A4[0] * scale), math.ceil(A4[1] * scale))


def test_a_rotated_page_reports_its_turned_frame():
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(*A4).set_rotation(90)
    buf = io.BytesIO()
    pdf.save(buf)
    out = render.render_page(buf.getvalue(), 1, scale=1.0, max_side=4000)
    assert (out.width, out.height) == (A4[1], A4[0])
    assert size_of(out.png) == (842, 595)


def test_render_png_keeps_its_page_and_region_defaults():
    data = synthetic_pdf(A4)
    assert size_of(render.render_png(data, 1, None)) == (math.ceil(595 * 1.5), math.ceil(842 * 1.5))
    crop = size_of(render.render_png(data, 1, [100.0, 100.0, 200.0, 150.0]))
    assert crop == (math.ceil(108 * render.REGION_SCALE), math.ceil(58 * render.REGION_SCALE))
    with pytest.raises(render.RenderError):
        render.render_png(data, 2, None)
    with pytest.raises(render.RenderError):
        render.render_page(b"not a pdf", 1, scale=1.5, max_side=2000)


# --- routes over fakes --------------------------------------------------------------------------------------------

class Rows:
    def __init__(self, row):
        self.row = row

    def first(self):
        return self.row


class FakeConn:
    """Answers the few statements the page and region routes send, by what they read."""

    def __init__(self, world):
        self.world = world

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "FROM document_blocks" in sql:
            return Rows(self.world.blocks.get(params["i"]))
        if "FROM pages" in sql:
            return Rows(self.world.pages.get(params["n"]))
        if "FROM document_versions" in sql:
            return Rows(self.world.state)
        raise AssertionError(f"unexpected statement: {sql}")

    def begin_nested(self):
        return nullcontext()


@pytest.fixture
def world(monkeypatch):
    doc, ver = uuid4(), uuid4()
    w = SimpleNamespace(
        doc=doc, ver=ver, visible=True, mime="application/pdf", is_current=True, page_count=2, reading=READING,
        files={"key": synthetic_pdf(A4, A0)}, blocks={}, pages={}, audits=[], enqueued=[],
        state=SimpleNamespace(status="ready", positions=POSITIONS_VERSION, job=None), enqueue_error=None,
    )

    def version(conn, version_id, document_id=None):
        if not w.visible or version_id != w.ver or (document_id is not None and document_id != w.doc):
            return None
        return Version(w.doc, w.ver, "שומה לדוגמה", w.reading, w.mime, w.is_current, w.page_count, False, "key",
                       "sample.pdf")

    @contextmanager
    def tx(ctx):
        yield FakeConn(w)

    def get(key):
        if key not in w.files:
            raise FileNotFoundError(key)
        return w.files[key]

    def enqueue(conn, version_id):
        if w.enqueue_error:
            raise w.enqueue_error
        w.enqueued.append(version_id)
        return True

    monkeypatch.setattr(documents.reader, "version", version)
    monkeypatch.setattr(documents, "tenant_tx", tx)
    monkeypatch.setattr(documents, "get_storage", lambda: SimpleNamespace(get=get))
    monkeypatch.setattr(documents, "audit", lambda conn, action, user, kind, target, **d: w.audits.append(
        (action, kind, target, d)))
    monkeypatch.setattr(documents, "enqueue_positions", enqueue)
    return w


@pytest.fixture
def client(world):
    app = FastAPI()
    app.include_router(documents.router)
    app.dependency_overrides[get_ctx] = lambda: SimpleNamespace(user_id=uuid4(), is_admin=False)
    return TestClient(app)


def base(w) -> str:
    return f"/api/documents/{w.doc}/versions/{w.ver}"


def image(response) -> tuple[int, int]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    return size_of(response.content)


def typed(response, status: int, state: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["state"] == state and isinstance(body["detail"], str) and body["detail"]
    assert response.headers["x-source-state"] == state
    assert response.headers["cache-control"] == "private, no-store"
    return body


def test_a_page_at_both_tiers_with_display_size_headers(client, world):
    assert image(client.get(f"{base(world)}/pages/1/image")) == (math.ceil(595 * 1.5), math.ceil(842 * 1.5))
    r = client.get(f"{base(world)}/pages/1/image", params={"scale": "zoom"})
    assert image(r) == (math.ceil(595 * 3.0), math.ceil(842 * 3.0))
    assert (r.headers["x-display-width"], r.headers["x-display-height"]) == ("595", "842")
    assert float(r.headers["x-render-scale"]) == 3.0 and r.headers["x-scale-tier"] == "zoom"
    assert [a[3] for a in world.audits] == [{"page": 1, "scale": "normal"}, {"page": 1, "scale": "zoom"}]
    assert {a[0] for a in world.audits} == {"source_view"}


def test_a_large_page_is_rendered_with_the_cap_applied(client, world):
    r = client.get(f"{base(world)}/pages/2/image", params={"scale": "zoom"})
    cap = render.page_max_side(render.PAGE_SCALES["zoom"])
    scale = cap / A0[1]
    assert image(r) == (math.ceil(A0[0] * scale), math.ceil(A0[1] * scale))
    assert max(size_of(r.content)) <= cap


def test_an_unknown_scale_is_refused(client, world):
    assert client.get(f"{base(world)}/pages/1/image", params={"scale": "9"}).status_code == 422


@pytest.mark.parametrize("path", ["pages/0/image", "pages/3/image", "pages/x/image"])
def test_a_page_outside_the_document_is_not_found(client, world, path):
    assert client.get(f"{base(world)}/{path}").status_code == 404
    assert world.audits == []


def test_a_version_the_user_may_not_see_is_not_found_for_page_region_and_file(client, world):
    world.visible = False
    world.blocks[0] = SimpleNamespace(page=1, bbox=[10, 10, 50, 50])
    urls = [f"{base(world)}/pages/1/image", f"{base(world)}/regions/0/image?reading_id={READING}",
            f"{base(world)}/file"]
    assert [client.get(u).status_code for u in urls] == [404, 404, 404]
    assert {client.get(u).json()["detail"] for u in urls} == {documents.NOT_FOUND}
    assert world.audits == [] and world.enqueued == []


def test_a_docx_has_no_page_images(client, world):
    world.mime = documents.DOCX_MIME
    world.blocks[0] = SimpleNamespace(page=1, bbox=[10, 10, 50, 50])
    assert client.get(f"{base(world)}/pages/1/image").status_code == 404
    assert client.get(f"{base(world)}/regions/0/image", params={"reading_id": READING}).status_code == 404
    assert world.audits == []


def test_a_missing_file_is_a_typed_failure_after_the_visibility_check(client, world):
    world.files.clear()
    world.blocks[0] = SimpleNamespace(page=1, bbox=[10, 10, 50, 50])
    for url in (f"{base(world)}/pages/1/image", f"{base(world)}/regions/0/image?reading_id={READING}",
                f"{base(world)}/file"):
        typed(client.get(url), 422, documents.STATE_FILE_MISSING)


def test_a_file_that_cannot_be_rendered_is_a_typed_failure(client, world):
    world.files["key"] = b"%PDF-1.7 truncated"
    world.blocks[0] = SimpleNamespace(page=1, bbox=[10, 10, 50, 50])
    typed(client.get(f"{base(world)}/pages/1/image"), 422, documents.STATE_RENDER_FAILED)
    typed(client.get(f"{base(world)}/regions/0/image", params={"reading_id": READING}), 422,
          documents.STATE_RENDER_FAILED)


def test_a_page_the_stored_count_promises_but_the_file_lacks_is_a_render_failure(client, world):
    world.page_count = 3
    typed(client.get(f"{base(world)}/pages/3/image"), 422, documents.STATE_RENDER_FAILED)


def test_a_region_requires_the_anchor_reading(client, world):
    world.blocks[0] = SimpleNamespace(page=1, bbox=[100, 100, 200, 150])
    assert client.get(f"{base(world)}/regions/0/image").status_code == 422
    assert world.audits == []


def test_a_region_of_an_older_reading_is_stale_and_never_the_new_block(client, world):
    world.blocks[0] = SimpleNamespace(page=1, bbox=[100, 100, 200, 150])
    body = typed(client.get(f"{base(world)}/regions/0/image", params={"reading_id": "reading-0"}), 409,
                 documents.STATE_STALE)
    assert "reading_id" not in body
    typed(client.get(f"{base(world)}/regions/0/image", params={"reading_id": documents.NO_READING}), 409,
          documents.STATE_STALE)
    assert world.audits == []


def test_a_region_of_the_stored_reading_is_served_and_audited_with_its_block(client, world):
    world.blocks[0] = SimpleNamespace(page=1, bbox=[100, 100, 200, 150])
    crop = image(client.get(f"{base(world)}/regions/0/image", params={"reading_id": READING}))
    assert crop == (math.ceil(108 * render.REGION_SCALE), math.ceil(58 * render.REGION_SCALE))
    assert world.audits == [("source_view", "document_version", world.ver, {"block": 0})]


def test_a_reading_from_before_reading_ids_matches_none(client, world):
    world.reading = None
    world.blocks[0] = SimpleNamespace(page=1, bbox=[100, 100, 200, 150])
    image(client.get(f"{base(world)}/regions/0/image", params={"reading_id": documents.NO_READING}))
    typed(client.get(f"{base(world)}/regions/0/image", params={"reading_id": READING}), 409, documents.STATE_STALE)


def test_a_region_is_cut_in_the_rendered_frame_of_its_page(client, world):
    world.files["key"] = synthetic_pdf(A4, cropbox=(100, 100, 595, 842))
    world.blocks[0] = SimpleNamespace(page=1, bbox=[400.0, 50.0, 590.0, 100.0])  # the reader's frame
    world.pages[1] = SimpleNamespace(mediabox=[0, 0, 595, 842], cropbox=[100, 100, 595, 842], rotation=0,
                                     display_width=495.0, display_height=742.0, geometry_issue=None)
    crop = image(client.get(f"{base(world)}/regions/0/image", params={"reading_id": READING}))
    # [300, 50, 490, 100] on the rendered page, with the margin
    assert crop == (math.ceil(198 * render.REGION_SCALE), math.ceil(58 * render.REGION_SCALE))


def test_a_page_says_when_the_anchor_reading_is_no_longer_stored(client, world):
    r = client.get(f"{base(world)}/pages/1/image", params={"reading_id": "reading-0"})
    image(r)  # the page belongs to the file and never changes: the snapshot highlight is drawn over it
    assert r.headers["x-reading-state"] == "stale"
    r = client.get(f"{base(world)}/pages/1/image", params={"reading_id": READING})
    assert r.headers["x-reading-state"] == "current"
    assert "x-reading-state" not in client.get(f"{base(world)}/pages/1/image").headers


def test_the_file_is_served_privately_and_audited(client, world):
    r = client.get(f"{base(world)}/file")
    assert r.status_code == 200 and r.content == world.files["key"]
    assert r.headers["cache-control"] == "private, no-store"
    assert world.audits == [("source_view", "document_version", world.ver, {})]


# --- lazy positions backfill ----------------------------------------------------------------------------------------

def test_opening_a_page_of_a_current_pdf_without_positions_queues_the_backfill(client, world):
    world.state = SimpleNamespace(status="ready", positions=None, job=None)
    image(client.get(f"{base(world)}/pages/1/image"))
    assert world.enqueued == [world.ver]


@pytest.mark.parametrize("change", [
    {"positions": POSITIONS_VERSION},  # already positioned
    {"job": "queued"}, {"job": "running"},  # already on its way
    {"job": "failed"},  # failed for good (a missing file): not again on every view
    {"status": "processing"},  # not read yet
])
def test_the_backfill_is_not_queued_again(client, world, change):
    world.state = SimpleNamespace(**({"status": "ready", "positions": None, "job": None} | change))
    image(client.get(f"{base(world)}/pages/1/image"))
    assert world.enqueued == []


def test_an_older_version_is_not_backfilled_from_the_viewer(client, world):
    world.is_current = False
    world.state = SimpleNamespace(status="superseded", positions=None, job=None)
    image(client.get(f"{base(world)}/pages/1/image"))
    assert world.enqueued == []


def test_a_failing_queue_never_fails_the_view(client, world):
    world.state = SimpleNamespace(status="ready", positions=None, job=None)
    world.enqueue_error = RuntimeError("queue down")
    image(client.get(f"{base(world)}/pages/1/image"))
    assert world.enqueued == []
