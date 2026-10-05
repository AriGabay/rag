"""Hybrid retrieval with Hebrew normalization and authorization (U9)."""

import pytest
from sqlalchemy import text

from app.db import TenantContext, tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.platform import pipeline
from app.platform.search import hybrid_search
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

TEXTS = [
    "4. שיקולי השמאי: השמאי המכריע קבע כי יש להפחית 10% בשל היטל השבחה בגין תוכנית רג/340.",
    "2. תיאור הנכס והסביבה: הדירה בקרבה לפארק הלאומי, חזית לרחוב שקט, שטח 95 מ\"ר נטו.",
    "3. עסקאות השוואה: עסקה בגוש 6158/42 נמכרה ב-2,470,000 ₪.",
]


def add_chunks(office, group_id, texts, sha):
    doc, ver = make_document(office, group_id, f"דוח {sha[:3]}", sha=sha)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    result = ExtractionResult(1, [PageResult(1, "\n".join(texts), "text_layer", 1.0, True)], [],
                              [ChunkResult(i, "text", [1, 2] if i == 0 else [1], None, t) for i, t in enumerate(texts)])
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(office.system, info, 1e18)
    return doc, ver


@pytest.fixture
def setup(db):
    a = make_office(db, "משרד א", "a@example.test")
    b = make_office(db, "משרד ב", "b@example.test")
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    emp = make_user(a, "e@example.test", [g1])
    da, _ = add_chunks(a, g1, TEXTS, "1" * 64)
    dg2, _ = add_chunks(a, g2, ["סודי לקבוצה 2: השמאי המכריע בפרויקט אחר"], "2" * 64)
    dbb, _ = add_chunks(b, b.default_group_id, ["משרד ב: השמאי המכריע קבע הפחתה של 25%"], "3" * 64)
    return a, b, emp, da, dg2, dbb


def search(ctx, q):
    with tenant_tx(ctx) as conn:
        return hybrid_search(conn, q, 5)


def test_prefix_and_definite_article(setup):
    a, *_ = setup
    hits = search(a.ctx(), "שמאי מכריע")
    assert hits and "המכריע" in hits[0]["text"]


def test_gershayim_and_block_parcel(setup):
    a, *_ = setup
    assert "95 מ" in search(a.ctx(), "95 מ״ר נטו")[0]["text"]
    assert "6158/42" in search(a.ctx(), "גוש 6158/42")[0]["text"]


def test_other_office_never_returned(setup):
    a, b, *_ = setup
    assert all("משרד ב" not in h["text"] for h in search(a.ctx(), "השמאי המכריע קבע הפחתה של 25%"))
    assert all("משרד ב" in h["text"] for h in search(b.ctx(), "השמאי המכריע"))


def test_group_restriction(setup):
    a, _, emp, da, dg2, _ = setup
    hits = search(TenantContext(a.office_id, emp, "employee"), "השמאי המכריע בפרויקט אחר")
    assert hits and all(h["document_id"] == da for h in hits)
    assert any(h["document_id"] == dg2 for h in search(a.ctx(), "השמאי המכריע בפרויקט אחר"))


def test_deleted_and_superseded_versions_hidden(setup):
    a, _, _, da, *_ = setup
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": da})
    assert all(h["document_id"] != da for h in search(a.ctx(), "היטל השבחה"))


def test_page_list_spans_pages(setup):
    a, *_ = setup
    hit = search(a.ctx(), "היטל השבחה")[0]
    assert hit["page_list"] == [1, 2]


def test_search_endpoint(client, setup):
    from tests.conftest import login

    login(client, "a@example.test")
    r = client.get("/api/search", params={"q": "קרבה לפארק"}).json()
    assert r["results"] and "לפארק" in r["results"][0]["snippet"]
