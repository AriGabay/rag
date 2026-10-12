"""Hybrid retrieval with Hebrew normalization and authorization (U9)."""

from uuid import UUID, uuid4

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


# --- U5: search generalization ------------------------------------------------------------------

def _search(ctx, queries, limit=8, **kw):
    from app.platform.search import search_evidence

    with tenant_tx(ctx) as conn:
        return search_evidence(conn, queries, limit, **kw)


def _add_occurrence(office, doc, ver, *, city=None, neighborhood=None, valuation_date=None, header=True,
                    data_kind="appraised_value", record_index=0):
    """One occurrence with its city/neighborhood facts; ``header`` marks them as report-header facts."""
    import json

    with tenant_tx(office.system) as conn:
        txn = conn.execute(text("INSERT INTO transactions (office_id, data_kind) VALUES (app_office(), :k)"
                                " RETURNING id"), {"k": data_kind}).scalar_one()
        occ = conn.execute(
            text("INSERT INTO occurrences (office_id, transaction_id, document_id, version_id, record_index,"
                 " extraction_version, data_kind, city, neighborhood, valuation_date)"
                 " VALUES (app_office(), :t, :d, :v, :i, 'rules-v1', :k, :c, :n, :vd) RETURNING id"),
            {"t": txn, "d": doc, "v": ver, "i": record_index, "k": data_kind, "c": city, "n": neighborhood,
             "vd": valuation_date},
        ).scalar_one()
        for field, value in (("city", city), ("neighborhood", neighborhood)):
            if value is None:
                continue
            source = {"page": 1, "label": "עיר" if field == "city" else "שכונה"} if header else {"page": 1, "col": 0}
            conn.execute(
                text("INSERT INTO fact_values (office_id, document_id, occurrence_id, field, original_text,"
                     " normalized_value, source_path, extraction_version)"
                     " VALUES (app_office(), :d, :o, :f, :v, :v, CAST(:sp AS jsonb), 'rules-v1')"),
                {"d": doc, "o": occ, "f": field, "v": value, "sp": json.dumps(source, ensure_ascii=False)},
            )


def _add_version(office, doc, texts, version_no=2):
    """A new current version of ``doc``; the previous one becomes superseded."""
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE document_versions SET is_current = false, status = 'superseded'"
                          " WHERE document_id = :d"), {"d": doc})
        ver = conn.execute(
            text("INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
                 " size_bytes, storage_key, status, is_current) VALUES (app_office(), :d, :n, :s, 'f.pdf',"
                 " 'application/pdf', 10, 'k', 'ready', true) RETURNING id"),
            {"d": doc, "n": version_no, "s": str(version_no) * 64},
        ).scalar_one()
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    result = ExtractionResult(1, [PageResult(1, "\n".join(texts), "text_layer", 1.0, True)], [],
                              [ChunkResult(i, "text", [1], None, t) for i, t in enumerate(texts)])
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(office.system, info, 1e18)
    return ver


@pytest.fixture
def office(db):
    a = make_office(db, "משרד ג", "c@example.test")
    hidden = make_group(a, "נסתר")
    emp = make_user(a, "emp@example.test", [a.default_group_id])
    return a, hidden, emp


def test_plural_question_finds_singular_passage(office):
    a, *_ = office
    doc, _ = add_chunks(a, a.default_group_id, ["לדירה מרפסת שמש פתוחה לכיוון מערב."], "4" * 64)
    hits = _search(a.ctx(), ["אילו מרפסות יש בדירות?"]).hits
    assert hits and hits[0]["document_id"] == doc and hits[0]["lexical_support"]


def test_construct_plural_question_finds_definite_plural_passage(office):
    # "שיקולי" (construct of "שיקולים") finds "השיקולים"; the passage shares no other word with the question.
    a, *_ = office
    doc, _ = add_chunks(a, a.default_group_id, ["השיקולים שנבחנו כללו את מצב התחזוקה של הבניין."], "7" * 64)
    hits = _search(a.ctx(), ["מה היו שיקולי הוועדה?"]).hits
    assert hits and hits[0]["document_id"] == doc and hits[0]["lexical_support"]


def test_shared_place_name_alone_is_not_evidence(office):
    a, *_ = office
    place_doc, _ = add_chunks(a, a.default_group_id, ["הנכס ממוקם ברמת גן, ברחוב ביאליק."], "5" * 64)
    topic_doc, _ = add_chunks(a, a.default_group_id, ["לדירה מרפסת שמש גדולה."], "6" * 64)
    out = _search(a.ctx(), ["מה נכתב על מרפסות ברמת גן?"], place_terms=["רמת גן"])
    support = {h["document_id"]: h["lexical_support"] for h in out.hits}
    assert support[topic_doc] is True
    assert support[place_doc] is False  # retrieved (shares the place), but not admitted as evidence
    # The unchanged three-argument call uses the gazetteer for the same rule.
    with tenant_tx(a.ctx()) as conn:
        legacy = {h["document_id"]: h["lexical_support"] for h in hybrid_search(conn, "מרפסות ברמת גן", 8)}
    assert legacy[topic_doc] is True and legacy[place_doc] is False


def test_table_row_renders_units_and_is_found_by_header_term(office):
    from app.extraction.base import TableResult, TableRow
    from app.extraction.chunking import chunk_document

    a, *_ = office
    doc, ver = make_document(a, a.default_group_id, "דוח טבלה", sha="7" * 64)
    table = TableResult(0, ["כתובת", "שטח ממ״ד", "מחיר (₪)"], [None, "מ״ר", "₪"],
                        [TableRow(1, ["הרצל 5", "12", "2,000,000"])], 1, 1)
    chunks = chunk_document([(1, "3. עסקאות השוואה")], [table])
    row = next(c for c in chunks if c.kind == "table_row")
    assert "שטח ממ״ד (מ״ר): 12" in row.text and "מחיר (₪): 2,000,000" in row.text
    assert "(₪) (₪)" not in row.text
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, ExtractionResult(1, [PageResult(1, "x", "text_layer", 1.0, True)],
                                                                 [table], chunks))
    pipeline.embed_stage(a.system, info, 1e18)
    hits = _search(a.ctx(), ["מה שטח הממ״ד?"]).hits
    assert hits and hits[0]["kind"] == "table_row" and hits[0]["lexical_support"]
    assert (hits[0]["table_index"], hits[0]["row_index"]) == (0, 0)


def test_city_filter_excludes_other_city_and_reports_unknown(office):
    from app.answering.metadata import MetadataFilters

    a, *_ = office
    rg, rg_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בדירה ברחוב ביאליק."], "8" * 64)
    hf, hf_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בדירה ברחוב הנביאים."], "9" * 64)
    unk, unk_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בדירה ללא פרטי מיקום."], "a" * 64)
    _add_occurrence(a, rg, rg_v, city="רמת גן")
    _add_occurrence(a, hf, hf_v, city="חיפה")
    out = _search(a.ctx(), ["מרפסת"], filters=MetadataFilters(city="רמת גן"))
    assert {h["document_id"] for h in out.hits} == {rg}
    report = out.filter_report
    assert report.matched == [rg_v] and report.excluded == [hf_v]
    assert report.unknown == {"city": [unk_v]}
    assert unk not in {h["document_id"] for h in out.hits}


def test_year_filter_uses_date_field_and_unknown_never_matches(office):
    from datetime import date

    from app.answering.metadata import MetadataFilters

    a, *_ = office
    new, new_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בשומה חדשה."], "b" * 64)
    old, old_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בשומה ישנה."], "c" * 64)
    nod, nod_v = add_chunks(a, a.default_group_id, ["מרפסת שמש בשומה ללא תאריך."], "d" * 64)
    _add_occurrence(a, new, new_v, city="חיפה", valuation_date=date(2024, 3, 1))
    _add_occurrence(a, old, old_v, city="חיפה", valuation_date=date(2019, 3, 1))
    _add_occurrence(a, nod, nod_v, city="חיפה")
    out = _search(a.ctx(), ["מרפסת"], filters=MetadataFilters(year_from=2023, year_to=2025))
    assert {h["document_id"] for h in out.hits} == {new}
    assert out.filter_report.unknown == {"valuation_date": [nod_v]}


def test_include_noncurrent_only_for_explicit_visible_versions(office):
    from app.platform.search import SearchScope

    a, hidden, emp = office
    doc, old_v = add_chunks(a, a.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(a, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    current = _search(a.ctx(), ["שיעור ההתאמה לגודל"]).hits
    assert current and {h["version_id"] for h in current} == {new_v}
    # include_noncurrent without explicit ids changes nothing
    assert {h["version_id"] for h in _search(a.ctx(), ["שיעור ההתאמה לגודל"],
                                             scope=SearchScope(include_noncurrent=True)).hits} == {new_v}
    both = _search(a.ctx(), ["שיעור ההתאמה לגודל"],
                   scope=SearchScope(version_ids=(old_v, new_v), include_noncurrent=True)).hits
    assert {h["version_id"] for h in both} == {old_v, new_v}
    # explicit old id without include_noncurrent: old versions never appear
    assert _search(a.ctx(), ["שיעור ההתאמה לגודל"], scope=SearchScope(version_ids=(old_v,))).hits == []
    # visible to the employee through the default group
    emp_ctx = TenantContext(a.office_id, emp, "employee")
    assert {h["version_id"] for h in _search(emp_ctx, ["שיעור ההתאמה לגודל"], scope=SearchScope(
        version_ids=(old_v,), include_noncurrent=True)).hits} == {old_v}


def test_employee_never_sees_hidden_group_even_with_explicit_scope(office):
    from app.platform.search import SearchScope

    a, hidden, emp = office
    secret, secret_v = add_chunks(a, hidden, ["סודי: מרפסת שמש בפרויקט מוגן."], "f" * 64)
    secret_new = _add_version(a, secret, ["סודי: מרפסת שמש בפרויקט מוגן, גרסה 2."])
    emp_ctx = TenantContext(a.office_id, emp, "employee")
    for kw in ({}, {"scope": SearchScope(document_ids=(secret,))},
               {"scope": SearchScope(version_ids=(secret_v, secret_new), include_noncurrent=True)}):
        assert all(h["document_id"] != secret for h in _search(emp_ctx, ["מרפסת שמש בפרויקט מוגן"], **kw).hits)
    assert any(h["document_id"] == secret for h in _search(a.ctx(), ["מרפסת שמש בפרויקט מוגן"]).hits)


def test_multi_query_fusion_finds_what_any_variant_finds(office):
    a, *_ = office
    doc, _ = add_chunks(a, a.default_group_id, ["השמאי קבע הפחתה בשל היטל השבחה."], "1a" * 32)
    hits = _search(a.ctx(), ["זריחה כחולה מעל הים", "היטל השבחה"]).hits
    assert any(h["document_id"] == doc and h["lexical_support"] for h in hits)


def _load_reindex():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "reindex_text_for_tests", Path(__file__).resolve().parents[2] / "scripts" / "reindex_text.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reindex_updates_table_rows_idempotently(office):
    import json

    a, *_ = office
    doc, ver = make_document(a, a.default_group_id, "דוח ישן", sha="2b" * 32)
    structure = {"headers": ["כתובת", "שטח ממ״ד"], "units": [None, "מ״ר"], "ocr": False, "section": None,
                 "rows": [{"page": 1, "cells": ["הרצל 5", "12"]}, {"page": 1, "cells": ["", ""]},
                          {"page": 2, "cells": ["ביאליק 3", "9"]}]}
    old_rows = ["כתובת: הרצל 5 | שטח ממ״ד: 12", "כתובת: ביאליק 3 | שטח ממ״ד: 9"]
    with tenant_tx(a.system) as conn:
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index,"
                          " page_start, page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 2,"
                          " CAST(:s AS jsonb))"), {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
        # Pre-U5 rows: no units, no table/row index, and the old normalization (no inflection variants).
        for i, t in enumerate(["3. עסקאות השוואה: מרפסות", *old_rows]):
            conn.execute(text("INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list,"
                              " text, normalized_text) VALUES (app_office(), :d, :v, :i, :k, :p, :t, :t)"),
                         {"d": doc, "v": ver, "i": i, "k": "text" if i == 0 else "table_row", "p": [1], "t": t})
    pipeline.embed_stage(a.system, pipeline.VersionInfo(ver, doc, "k", "application/pdf", None), 1e18)
    reindex = _load_reindex()
    first = reindex.reindex_office(a.system)
    assert first["rows_rewritten"] == 2 and first["normalized"] == 3
    with tenant_tx(a.system) as conn:
        rows = conn.execute(text("SELECT text, normalized_text, table_index, row_index, embedding IS NOT NULL AS e"
                                 " FROM chunks WHERE kind = 'table_row' ORDER BY chunk_index")).all()
        head = conn.execute(text("SELECT normalized_text FROM chunks WHERE kind = 'text'")).scalar_one()
    assert [r.text for r in rows] == ["כתובת: הרצל 5 | שטח ממ״ד (מ״ר): 12", "כתובת: ביאליק 3 | שטח ממ״ד (מ״ר): 9"]
    assert [(r.table_index, r.row_index) for r in rows] == [(0, 0), (0, 2)]
    assert all(r.e for r in rows) and "מרפסת" in head.split()
    second = reindex.reindex_office(a.system)
    assert second == {"rows_rewritten": 0, "normalized": 0, "reembedded": 0, "versions_skipped": 0}


def test_records_less_report_takes_place_and_date_from_its_header(office):
    """A narrative report with no records still has a city, neighborhood and valuation date for
    filters, read from its own "label: value" header lines; the gazetteer learns those places too."""
    from datetime import date

    from app.answering.metadata import MetadataFilters, header_places

    a, *_ = office
    header = "עיר: רמת גן\nשכונה: הבורסה\nהמועד הקובע: 15/03/2024"
    doc, ver = add_chunks(a, a.default_group_id, [header, "תיאור הנכס: דירה בבניין משותף."], "c" * 64)
    other, other_v = add_chunks(a, a.default_group_id, ["עיר: חיפה", "תיאור הנכס: דירה בבניין משותף."], "d" * 64)
    out = _search(a.ctx(), ["דירה בבניין"], filters=MetadataFilters(city="רמת גן"))
    assert out.filter_report.matched == [ver] and out.filter_report.excluded == [other_v]
    out = _search(a.ctx(), ["דירה בבניין"],
                  filters=MetadataFilters(neighborhood="הבורסה", year_from=2024, year_to=2024))
    assert out.filter_report.matched == [ver]
    with tenant_tx(a.ctx()) as conn:
        from app.answering.metadata import version_metadata

        assert version_metadata(conn, [ver])[ver].dates["valuation_date"] == frozenset({date(2024, 3, 15)})
        places = header_places(conn)
    assert {(None, "רמת גן"), ("רמת גן", "הבורסה"), (None, "חיפה")} <= places


# --- Retrieval round 2: small scopes keep every page, locate ranks documents ---------------------------

def _doc(office, chunks, title="מסמך", group=None):
    from tests.integration.test_entities import add_doc

    return add_doc(office, group or office.default_group_id, chunks, title)


def test_small_scope_keeps_the_best_passage_of_every_page(office):
    """A narrow scope (the documents an address resolved to) returns each page's best supported passage,
    even when the address words fill page 1 and global ranking would cut page 2."""
    from app.platform.search import SearchScope

    a, *_ = office
    page1 = [(f"כתובת הנכס: הדקלים 7. סעיף {i}: הדירה ברחוב הדקלים 7 בקומה {i}.", [1]) for i in range(10)]
    doc, _ = _doc(a, [*page1, ("סוג הקרקע לפי התכנית החלה: חקלאית.", [2])])
    other, _ = _doc(a, [("סוג הקרקע: מגורים. הנכס ברחוב אחר.", [1])])
    for i in range(12):  # the topic words are common in the office, the address words are rare
        _doc(a, [(f"סוג הקרקע: מגורים {i}.", [1])])
    out = _search(a.ctx(), ["מה סוג הקרקע ברחוב הדקלים 7?"], 4, scope=SearchScope(document_ids=(doc,)),
                  place_terms=["רחוב הדקלים 7"])
    assert all(h["document_id"] == doc for h in out.hits)
    supported = [h for h in out.hits if h["lexical_support"]]
    assert supported and supported[0]["page_list"] == [2] and "חקלאית" in supported[0]["text"]
    assert other not in {h["document_id"] for h in out.hits}


def test_small_scope_returns_each_documents_pages(office):
    from app.platform.search import SearchScope

    a, *_ = office
    filler = [(f"הערה כללית {i} על הבניין ועל הסביבה.", [1]) for i in range(8)]
    one, _ = _doc(a, [*filler, ("שיעור ההיוון שנקבע: 5%.", [2]), ("שיעור ההיוון אושר בוועדה.", [3])])
    two, _ = _doc(a, [*filler, ("שיעור ההיוון שנקבע: 6%.", [2])])
    hits = _search(a.ctx(), ["שיעור ההיוון"], 2, scope=SearchScope(document_ids=(one, two))).hits
    pages = {(h["document_id"], h["page_list"][0]) for h in hits if h["lexical_support"]}
    assert pages == {(one, 2), (one, 3), (two, 2)}


def _locate(ctx, queries, **kw):
    from app.platform.search import locate_documents

    with tenant_tx(ctx) as conn:
        return locate_documents(conn, queries, **kw)


def test_locate_keeps_only_documents_with_full_topic_support(office):
    a, *_ = office
    exact, _ = _doc(a, [("רקע כללי.", [1]), ("ניתן אישור חריגה לתוספת הקומה.", [2])])
    inflected, _ = _doc(a, [("האישור לחריגה התקבל בשנת 2019.", [1])])
    approval_only, _ = _doc(a, [("אישור הוועדה המקומית לתוכנית.", [1])])
    deviation_only, _ = _doc(a, [("קיימת חריגה מקו הבניין.", [1])])
    far_apart, _ = _doc(a, [("אישור המשכנתא התקבל. בדירה נמצאה גם חריגה קלה בחלון.", [1])])
    _doc(a, [("שומות קודמות בבניין.", [1])])
    ranked = _locate(a.ctx(), ["באילו שומות מוזכר אישור חריגה?"])
    assert {d.document_id for d in ranked} == {exact, inflected}
    first = next(d for d in ranked if d.document_id == exact)
    assert first.full_support and first.pages == [2]
    assert first.passages and "אישור חריגה" in first.passages[0]["text"]
    assert first.passages[0]["lexical_support"] and first.passages[0]["document_id"] == exact
    assert {approval_only, deviation_only, far_apart}.isdisjoint({d.document_id for d in ranked})


def test_locate_negation_question_finds_the_negated_passage(office):
    a, *_ = office
    none_word, _ = _doc(a, [("בבניין אין גינה משותפת.", [1])])
    without, _ = _doc(a, [("דירה ללא גינה, בקומה שנייה.", [1])])
    not_incl, _ = _doc(a, [("הבניין נבנה בשנת 1972 ואינו כולל גינה.", [1])])
    has_it, _ = _doc(a, [("לבניין גינה מטופחת.", [1])])
    other_sentence, _ = _doc(a, [("הגינה מטופחת ומוארת. אין חניה.", [1])])
    for question in ("באילו שומות כתוב שאין גינה?", "איפה מצוין שאין גינה בכלל?"):
        ranked = _locate(a.ctx(), [question])
        assert {d.document_id for d in ranked} == {none_word, without, not_incl}, question
        for d in ranked:
            assert any(w in d.passages[0]["text"] for w in ("אין", "ללא", "אינו")), question
    assert {has_it, other_sentence}.isdisjoint({d.document_id for d in _locate(a.ctx(), ["שאין גינה"])})


def test_locate_partial_support_keeps_the_best_within_a_relative_threshold(office):
    a, *_ = office
    strong, _ = _doc(a, [("חוות הדעת מתייחסת לפיצול הדירה לשתי יחידות.", [1])])
    weak, _ = _doc(a, [("הדירה בקומה שלישית.", [1])])
    ranked = _locate(a.ctx(), ["איפה מוזכר פיצול של נכס מסחרי?"])
    assert [d.document_id for d in ranked] == [strong]
    assert not ranked[0].full_support and "פיצול" in ranked[0].matched_terms
    assert weak not in {d.document_id for d in ranked}


def test_locate_word_present_in_every_document_does_not_count(office):
    a, *_ = office
    docs = [_doc(a, [(f"הדירה בקומה {i}.", [1])])[0] for i in range(4)]
    sukkah, _ = _doc(a, [("לדירה מרפסת סוכה פתוחה.", [1])])
    ranked = _locate(a.ctx(), ["באילו דירות יש סוכה?"])
    assert [d.document_id for d in ranked] == [sukkah]
    assert set(docs).isdisjoint({d.document_id for d in ranked})


def test_locate_table_question_prefers_a_table_row(office):
    a, *_ = office
    table, _ = _doc(a, [("נתוני היחידה", [1]), ("כתובת: הנרקיס 2 | שטח מרתף (מ״ר): 30", [1], "table_row")])
    prose, _ = _doc(a, [("למרתף שטח של 30 מ״ר.", [1])])
    ranked = _locate(a.ctx(), ["באיזו שומה יש טבלה עם עמודה של שטח מרתף?"])
    assert [d.document_id for d in ranked] == [table]
    assert ranked[0].passages[0]["kind"] == "table_row"
    assert prose not in {d.document_id for d in ranked}


def test_locate_respects_scope_and_groups(office):
    from app.platform.search import SearchScope

    a, hidden, emp = office
    one, _ = _doc(a, [("הוגשה התנגדות לתוכנית.", [1])])
    two, _ = _doc(a, [("הוגשה התנגדות נוספת.", [1])])
    secret, _ = _doc(a, [("סודי: הוגשה התנגדות.", [1])], group=hidden)
    assert {d.document_id for d in _locate(a.ctx(), ["התנגדות"])} == {one, two, secret}
    assert [d.document_id for d in _locate(a.ctx(), ["התנגדות"], scope=SearchScope(document_ids=(two,)))] == [two]
    emp_ctx = TenantContext(a.office_id, emp, "employee")
    assert {d.document_id for d in _locate(emp_ctx, ["התנגדות"])} == {one, two}
    assert _locate(a.ctx(), ["מילה שאינה קיימת בשום מקום"]) == []


def test_locate_treats_query_variants_as_alternatives(office):
    """Found with the real model: a rephrasing that added a word no document uses ("לא קיימת") pooled into
    the question and pushed out documents another variant named exactly."""
    from app.platform.search import locate_evidence

    a, *_ = office
    d1, _ = add_chunks(a, a.default_group_id, ["בבניין אין מעלית ויש מדרגות בלבד."], "e1" * 32)
    d2, _ = add_chunks(a, a.default_group_id, ["הבניין ללא מעלית."], "e2" * 32)
    add_chunks(a, a.default_group_id, ["בבניין מעלית חדשה."], "e3" * 32)
    with tenant_tx(a.ctx()) as conn:
        out = locate_evidence(conn, ["מעלית בבניין לא קיימת", "ללא מעלית", "אין מעלית בבניין"])
    assert {d.document_id for d in out.documents} == {d1, d2}


def _locate_out(ctx, queries, **kw):
    from app.platform.search import locate_evidence

    with tenant_tx(ctx) as conn:
        return locate_evidence(conn, queries, **kw)


def test_locate_keeps_every_fully_supported_document_despite_a_phrase_bonus(office):
    """GQ50: the document whose words stood next to each other scored 1.25, two others that fully said "no
    elevator" scored 1.0, and the relative threshold (0.9 x 1.25) dropped them."""
    a, *_ = office
    adjacent, _ = _doc(a, [("בדירה אין מעלית בבניין ישן.", [1])])
    far, _ = _doc(a, [("הדירה בקומה השלישית בבניין בן ארבע קומות ללא מעלית.", [1])])
    other_sentence, _ = _doc(a, [("הדירה בקומה שנייה. הבניין ישן, ללא מעלית.", [2])])
    has_it, _ = _doc(a, [("לבניין יש מעלית ואין חניה.", [1])])
    for queries in (["אין מעלית בבניין"], ["אין מעלית בבניין", "ללא מעלית בבניין"]):
        out = _locate_out(a.ctx(), queries)
        assert {d.document_id for d in out.documents} == {adjacent, far, other_sentence}, queries
        assert all(d.full_support for d in out.documents) and out.documents[0].document_id == adjacent
        assert out.absent_terms == []


def test_locate_negation_must_govern_the_topic_term(office):
    """Testing review P2: a negation of another term in the same sentence is not "no elevator"."""
    a, *_ = office
    no_parking, _ = _doc(a, [("לבניין יש מעלית ואין חניה.", [1])])
    but, _ = _doc(a, [("אין חניה, אך הבניין כולל מעלית.", [1])])
    without, _ = _doc(a, [("בבניין ללא מעלית.", [1])])
    not_incl, _ = _doc(a, [("הבניין אינו כולל מעלית.", [1])])
    out = _locate_out(a.ctx(), ["באילו שומות אין מעלית"])
    assert {d.document_id for d in out.documents} == {without, not_incl}
    assert {no_parking, but}.isdisjoint({d.document_id for d in out.documents})


def test_locate_question_word_found_nowhere_lists_nothing(office):
    """GQ43: "pool" occurs in no document, so "building" alone made a passage complete and an unrelated
    document was listed as relevant. Nothing is listed, and the absent word is reported."""
    from app.platform.search import SearchScope

    a, *_ = office
    docs = [_doc(a, [(f"שומת מקרקעין — הדקלים {i}. תיאור הנכס והבניין: דירה בבניין בן {i + 3} קומות.", [1])])[0]
              for i in range(4)]
    _doc(a, [("עסקאות השוואה באזור.", [1])])
    out = _locate_out(a.ctx(), ["באילו שומות יש בריכה בבניין"])
    assert out.documents == [] and out.absent_terms == ["בריכה"]
    scoped = _locate_out(a.ctx(), ["הדקלים 1 בריכה בניין", "בריכה ברחוב הדקלים 1", "הדקלים 1 יש בריכה"],
                         scope=SearchScope(document_ids=(docs[1],)))
    assert scoped.documents == [] and "בריכה" in scoped.absent_terms


def test_locate_absent_word_in_one_variant_does_not_block_another(office):
    """Counter-test: variants are alternatives. A word only one rephrasing adds, found nowhere, does not hide
    the documents another variant fully supports."""
    a, *_ = office
    sukkah, _ = _doc(a, [("לדירה מרפסת סוכה פתוחה.", [1])])
    _doc(a, [("הדירה בקומה שנייה.", [1])])
    out = _locate_out(a.ctx(), ["באילו שומות יש סוכה מפוארת", "מרפסת סוכה"])
    assert [d.document_id for d in out.documents] == [sukkah] and out.documents[0].full_support
    assert out.absent_terms == []


def test_locate_absent_word_keeps_partial_documents_with_a_distinctive_word(office):
    """Counter-test: a document naming a word that tells documents apart is still listed (partially), and
    the word found nowhere is reported for the caller."""
    a, *_ = office
    strong, _ = _doc(a, [("חוות הדעת מתייחסת לפיצול הדירה לשתי יחידות.", [1])])
    for i in range(3):
        _doc(a, [(f"הדירה בקומה {i + 1}.", [1])])
    out = _locate_out(a.ctx(), ["איפה מוזכר פיצול של נכס מסחרי?"])
    assert [d.document_id for d in out.documents] == [strong] and not out.documents[0].full_support
    assert set(out.absent_terms) == {"נכס", "מסחרי"}


def test_locate_document_keeps_the_passages_of_every_supporting_variant(office):
    """GQ57 (regression from d405ae2): only the best variant's passages were kept, so the page-1 passage that
    another variant fully supported was lost."""
    a, *_ = office
    doc, _ = _doc(a, [("המרפסת הפונה לחזית נסגרה בתריסים ללא היתר בנייה.", [1]),
                      ("סגירת המרפסת ללא היתר עלולה לחייב הריסה.", [2])])
    _doc(a, [("הדירה בקומה שנייה.", [1])])
    out = _locate_out(a.ctx(), ["מרפסת נסגרה בלי היתר", "סגירת מרפסת ללא היתר"])
    [found] = out.documents
    assert found.document_id == doc and found.full_support and found.pages == [1, 2]
    texts = [p["text"] for p in found.passages]
    assert texts[0].startswith("סגירת") and any("נסגרה" in t for t in texts)


def test_an_unnegated_or_quoted_variant_cannot_list_documents_stating_the_opposite(setup):
    """Round 7 (GQ50, HV64): the interpreter added a bare variant ("X") next to "אין X", and quoted another; the bare
    one listed every document that has X. Only the negated variants count, and quotes do not hide the negation."""
    from app.platform.search import locate_evidence

    a = setup[0]
    add_chunks(a, a.default_group_id, ["בבניין יש מעלית חדשה ומרווחת."], "7" * 64)
    add_chunks(a, a.default_group_id, ["הבניין בן ארבע קומות ללא מעלית."], "8" * 64)
    with tenant_tx(a.ctx()) as conn:
        out = locate_evidence(conn, ["אין מעלית בבניין", "\"בניין ללא מעלית\"", "מעלית"])
    titles = [d.title for d in out.documents if d.full_support]
    assert titles == ["דוח 888"]


def test_a_negated_variant_added_to_a_question_about_presence_is_dropped(setup):
    """Final run (GQ12): for "which appraisals mention X", the rephrasing added "ללא X"; keeping only negated
    variants listed only the document without X. The user's question decides the polarity."""
    from app.platform.search import locate_evidence

    a = setup[0]
    add_chunks(a, a.default_group_id, ["בבניין יש מעלית חדשה ומרווחת."], "7" * 64)
    add_chunks(a, a.default_group_id, ["הבניין בן ארבע קומות ללא מעלית."], "8" * 64)
    with tenant_tx(a.ctx()) as conn:
        positive = locate_evidence(conn, ["מעלית", "יש מעלית", "ללא מעלית"], question="אילו שומות מזכירות מעלית?")
        negative = locate_evidence(conn, ["מעלית", "אין מעלית"], question="באילו שומות אין מעלית?")
    assert "דוח 777" in [d.title for d in positive.documents if d.full_support]
    assert [d.title for d in negative.documents if d.full_support] == ["דוח 888"]


# --- U12: search on corrected content, abbreviations, table hints and title naming -------------------------------

def _passages(ctx, query, limit=8, **kw):
    from app.platform.search import search_passages

    with tenant_tx(ctx) as conn:
        return search_passages(conn, query, limit, **kw)


def _named(ctx, query):
    from app.platform.search import documents_named

    with tenant_tx(ctx) as conn:
        return [d for d, _ in documents_named(conn, query)]


_STREETS = ("האלון", "הברוש", "האורן", "הדולב", "התאנה", "הזית")


def _reports_sharing_professional_words(a, n=6):
    """Reports whose text carries the office's common professional words ("השווי", "מצב התחזוקה")."""
    return [_doc(a, [(f"השווי של הנכס נקבע בגישת ההשוואה. מצב התחזוקה של הבניין טוב. הדירה בקומה {i + 1}.", [1])],
                 title=f"שומה — רחוב {_STREETS[i]} {i + 10}")[0] for i in range(n)]


def _generic_title_report(a):
    """A long report whose title holds the generic word "שווי" next to its address."""
    return _doc(a, [(f"סעיף {i}: שווי השוק של הנכס נבחן מול עסקאות באזור, והשווי נקבע בהתאם לנתוני הסביבה.", [i + 1])
                    for i in range(12)] + [("מצב התחזוקה של הבניין סביר.", [13])],
                title="שומה שווי שוק — רחוב התמר 5, כפר הדר")[0]


def test_a_generic_word_in_one_title_does_not_capture_the_results(office):
    """Real content: every top result of a value question came from the one report whose title happened to hold the
    generic word "שווי"; the passage that answered it, in another report, was pushed out. A word most documents'
    text carries names no document, and one word never names a document twice through its prefix-stripped form."""
    a, *_ = office
    _reports_sharing_professional_words(a)
    generic = _generic_title_report(a)
    answer, _ = _doc(a, [("רקע כללי על הסביבה.", [1]), ('שווי מ"ר מבונה בשפ"פ נקבע ל-4,000 ₪.', [2])],
                     title="שומה — רחוב הדקל 3")
    for query in ('שווי מ"ר מבונה שפ"פ', 'שווי מ"ר מבונה', "שווי מטר מרובע מבונה"):
        assert _named(a.ctx(), query) == [], query
        hits = _passages(a.ctx(), query)
        assert any(h["document_id"] == answer and "4,000" in h["text"] for h in hits), query
        assert {h["document_id"] for h in hits} != {generic}, query


def test_a_distinctive_name_or_address_in_the_query_still_leads_with_its_document(office):
    a, *_ = office
    reports = _reports_sharing_professional_words(a)
    generic = _generic_title_report(a)
    towers, _ = _doc(a, [("מצב התחזוקה של הבניין טעון שיפוץ.", [1])], title="חוות דעת — מגדלי הנחל")
    # an address: a title word with its house number, though the title also holds a generic word
    assert _named(a.ctx(), "מה מצב התחזוקה בהתמר 5?") == [generic]
    hits = _passages(a.ctx(), "מה מצב התחזוקה בהתמר 5?")
    assert hits[0]["document_id"] == generic and "סביר" in hits[0]["text"]
    # a name: two words no other title and few documents hold
    assert _named(a.ctx(), "מה מצב התחזוקה במגדלי הנחל?") == [towers]
    assert _passages(a.ctx(), "מה מצב התחזוקה במגדלי הנחל?")[0]["document_id"] == towers
    # a word most titles hold, with a number, names nothing ("שומה 12" is any report's number)
    assert _named(a.ctx(), "שומה 12") == []
    assert _named(a.ctx(), "מה השווי בהאורן 12?") == [reports[2]]


def test_an_abbreviation_and_its_spelled_out_form_retrieve_the_same_passage(office):
    """Each spelling finds the passage written in the other, as lexical evidence, among reports that share the other
    words of the question."""
    a, *_ = office
    for i in range(4):
        _doc(a, [(f"שטח הדירה {80 + i} מ\"ר. הדירה כוללת מחסן ושכירות החניה כלולה.", [1])],
             title=f"שומה — רחוב האלה {i + 1}")
    rent, _ = _doc(a, [("דמ\"ש ראויים לנכס: 60 ₪ למ\"ר לחודש.", [1])], title="שומה — רחוב הרימון 4")
    open_space, _ = _doc(a, [('לדירה צמוד שפ"פ מגונן.', [1])], title="שומה — רחוב השקד 8")
    planning, _ = _doc(a, [("לפי תכנית בניין עיר החלה, ייעוד המגרש למגורים.", [1])], title="שומה — רחוב הארז 6")
    for short, full, doc in (('מה הדמ"ש הראויים?', "מה דמי השכירות הראויים?", rent),
                             ('מה שטח השפ"פ?', "מה השטח הפרטי הפתוח?", open_space),
                             ('מה קובעת התב"ע?', "מה קובעת תכנית בניין עיר?", planning)):
        for query in (short, full):
            assert any(h["document_id"] == doc and h["lexical_support"] for h in _passages(a.ctx(), query)), query


def test_corrected_text_is_what_lexical_and_semantic_search_index(office):
    """A block whose font map was repaired keeps its original text for audit only: the corrected text is indexed
    for lexical search and embedded, and the broken form finds nothing."""
    from app.extraction.base import Block
    from app.extraction.chunking import chunk_blocks
    from app.platform.search import _lexical, _scope_sql, _semantic
    from app.providers.embeddings import get_embedding_provider, to_pgvector

    a, *_ = office
    _reports_sharing_professional_words(a, 3)
    doc, ver = make_document(a, a.default_group_id, "שומה — רחוב הערבה 2", sha="5c" * 32)
    corrected, original = "הנכס נמצא בשכונה שקטה ומבוקשת.", "הðכס ðמצא בשכוðה שקטה ומבוקשת."
    blocks = [Block(0, "heading", "1. תיאור הסביבה", section="1. תיאור הסביבה", page=1),
              Block(1, "paragraph", corrected, section="1. תיאור הסביבה", page=1, original_text=original)]
    result = ExtractionResult(1, [PageResult(1, corrected, "text_layer", 1.0, True)], [],
                              chunk_blocks(blocks, []), blocks=blocks)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(a.system, info, 1e18)
    with tenant_tx(a.ctx()) as conn:
        [chunk] = conn.execute(text("SELECT id, normalized_text, embedding::text AS e FROM chunks WHERE version_id = :v"),
                               {"v": ver}).all()
        assert "ð" not in chunk.normalized_text and "שכונה" in chunk.normalized_text.split()
        assert _lexical(conn, ["שכונה"], _scope_sql(None))[0] == chunk.id
        assert _lexical(conn, ["בשכוðה"], _scope_sql(None)) == []
        assert _semantic(conn, "שכונה שקטה ומבוקשת", _scope_sql(None))[0] == chunk.id
    expected = to_pgvector(get_embedding_provider().embed_passages([chunk_blocks(blocks, [])[0].text])[0])
    assert [round(float(x), 4) for x in chunk.e.strip("[]").split(",")] == [
        round(float(x), 4) for x in expected.strip("[]").split(",")]


def _table_report(a, title="שומה — רחוב הברוש 9"):
    from app.extraction.base import Block, TableResult, TableRow
    from app.extraction.chunking import chunk_blocks

    doc, ver = make_document(a, a.default_group_id, title, sha="7d" * 32)
    rows = [["חנות 1", "40", "קרקע"], ["חנות 2", "35", "קרקע"], ["חנות 3", "50", "קרקע"], ["משרד 4", "80", "א"],
            ["משרד 5", "90", "ב"]]
    table = TableResult(0, ["יחידה", "שטח", "קומה"], [None, "מ״ר", None], [TableRow(1, r) for r in rows], 1, 1,
                        section="3. פירוט היחידות", caption="להלן פירוט היחידות בבניין:", block_index=2)
    blocks = [Block(0, "heading", "3. פירוט היחידות", section="3. פירוט היחידות", page=1),
              Block(1, "paragraph", "להלן פירוט היחידות בבניין:", section="3. פירוט היחידות", page=1),
              Block(2, "table", "\n".join(" | ".join(r) for r in rows), section="3. פירוט היחידות", page=1,
                    table_index=0)]
    result = ExtractionResult(1, [PageResult(1, "x", "text_layer", 1.0, True)], [table], chunk_blocks(blocks, [table]),
                              blocks=blocks)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(a.system, info, 1e18)
    return doc, ver


def test_rows_capped_by_the_diversity_cap_keep_the_table_and_name_its_handle(office):
    """A query matching three rows of one table: the row cap shows two of them, the table passage stands in for the
    rest, and the search result names the table's handle so the whole table can be opened."""
    from app.chat import tools as T

    a, *_ = office
    _reports_sharing_professional_words(a, 3)
    doc, ver = _table_report(a)
    hits = _passages(a.ctx(), "חנות בקומת קרקע", 3)
    assert sum(h["kind"] == "table_row" for h in hits) == 2
    assert any(h["kind"] == "table" and h["table_index"] == 0 for h in hits)
    ws = T.Workspace(ctx=a.ctx())
    out = T.tool_search(ws, "חנות בקומת קרקע", None, 3)
    handle = next(h for h, v in ws.handles.items() if v["kind"] == "T" and v["table_index"] == 0)
    assert 'kind="table"' in out and "להלן פירוט היחידות בבניין:" in out
    hint = out.split("</source>")[-1]
    assert handle in hint and "read" in hint and "table=" in hint


# --- search latency (round 9 U2, R16, KTD3): the trigram stage reads each chunk once -----------------------------

TRIGRAM_QUERIES = ["שמאי מכריע", "היטל השבחה", "גוש חלקה", "פארק לאומי", "חזית לרחוב שקט", "עסקאות השוואה"]
LEGACY_TRIGRAM = ("SELECT c.id, word_similarity(:q, c.normalized_text) AS sim"
                  " FROM chunks c JOIN document_versions v ON v.id = c.version_id"
                  " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL"
                  " WHERE v.is_current AND word_similarity(:q, c.normalized_text) > :th"
                  " ORDER BY sim DESC LIMIT :n")


def _many_reports(a, count):
    """``count`` current documents of one version each, every one carrying the trigram stage's words."""
    for i in range(count):
        add_chunks(a, a.default_group_id, [f"{TEXTS[0]} מספר {i}", f"{TEXTS[1]} מספר {i}", f"{TEXTS[2]} מספר {i}"],
                   f"{i + 10:064d}")


def _legacy_trigram(conn, query, n=40):
    from app.platform.search import TRIGRAM_THRESHOLD

    return [(r.id, round(r.sim, 6)) for r in conn.execute(text(LEGACY_TRIGRAM),
                                                          {"q": query, "th": TRIGRAM_THRESHOLD, "n": n})]


def _trigram_scope(conn, scope=None):
    """The SQL scope of ``scope`` with its visible versions resolved under row security, as a search resolves them."""
    from dataclasses import replace

    from app.platform.search import _prepare, _scoped_versions

    sql, _, _ = _prepare(conn, scope, None)
    return sql if sql.version_ids is not None else replace(sql, version_ids=[v for v, _ in _scoped_versions(conn, scope)])


def test_trigram_candidates_match_the_word_similarity_predicate(office):
    """The candidate chunks of the trigram stage are today's ``word_similarity > threshold`` rows (KTD3 parity)."""
    from app.platform.search import _trigram

    a, *_ = office
    _many_reports(a, 5)
    with tenant_tx(a.ctx()) as conn:
        for q in TRIGRAM_QUERIES:
            expected = _legacy_trigram(conn, q)
            got = _trigram(conn, q, _trigram_scope(conn), 40)
            assert expected, q
            assert set(got) == {i for i, _ in expected}, q
            sims = dict(expected)
            ordered = [sims[i] for i in got]
            assert ordered == sorted(ordered, reverse=True), q  # the same similarity order, ties aside


def test_trigram_parity_holds_in_a_narrowed_scope(office):
    from app.platform.search import SearchScope, _trigram

    a, *_ = office
    _many_reports(a, 4)
    with tenant_tx(a.ctx()) as conn:
        doc = conn.execute(text("SELECT id FROM documents ORDER BY title, id LIMIT 1")).scalar_one()
        got = _trigram(conn, "היטל השבחה", _trigram_scope(conn, SearchScope(document_ids=(doc,))), 40)
        everything = _trigram(conn, "היטל השבחה", _trigram_scope(conn), 40)
        owners = {r.id: r.document_id for r in conn.execute(text("SELECT id, document_id FROM chunks"))}
        assert got and {owners[i] for i in got} == {doc}
        assert got == [i for i in everything if owners[i] == doc]


def test_a_typo_still_matches_through_trigram(office):
    from app.platform.search import _trigram

    a, *_ = office
    doc, _ = add_chunks(a, a.default_group_id, TEXTS, "5" * 64)
    with tenant_tx(a.ctx()) as conn:
        ids = _trigram(conn, "היטל השבחא", _trigram_scope(conn), 40)  # one wrong letter
        text_of = {r.id: r.normalized_text for r in conn.execute(text("SELECT id, normalized_text FROM chunks"))}
        assert ids and "השבחה" in text_of[ids[0]]


def test_fused_order_is_the_order_the_legacy_trigram_gives(office, monkeypatch):
    """Swapping only the trigram stage back to the old predicate leaves the fused top-k order unchanged (KTD3)."""
    from app.platform import search as S

    a, *_ = office
    _many_reports(a, 5)

    def legacy(conn, normalized_query, scope, n=S.CANDIDATES):
        return list(conn.execute(
            text(f"SELECT c.id{scope.joins} WHERE {scope.where} AND word_similarity(:q, c.normalized_text) > :th"
                 " ORDER BY word_similarity(:q, c.normalized_text) DESC, c.id LIMIT :n"),
            scope.params | {"q": normalized_query, "th": S.TRIGRAM_THRESHOLD, "n": n}).scalars())

    queries = [["שמאי מכריע"], ["היטל השבחה בגוש"], ["פארק לאומי", "חזית לרחוב"], ["השבחא"]]
    with tenant_tx(a.ctx()) as conn:
        now = [[h["chunk_id"] for h in S.search_evidence(conn, q, 8).hits] for q in queries]
        monkeypatch.setattr(S, "_trigram", legacy)
        before = [[h["chunk_id"] for h in S.search_evidence(conn, q, 8).hits] for q in queries]
    assert now == before and all(now)


def _plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def test_trigram_reads_the_chunks_once_however_many_versions_are_visible(office, db):
    """RLS makes the planner expect one row from ``documents`` and ``document_versions``; the old join order then
    scanned ``chunks`` once per version x document and evaluated the predicate on each scan. The candidates now come
    from one ``trigram_candidates`` call, and ``chunks`` is read only by id for them (measured on the office: 256
    loops of the predicate, 10.8 s)."""
    import json

    from app.platform import search as S

    a, *_ = office
    _many_reports(a, 16)
    with db.begin() as owner:  # planner statistics, as the office has them
        owner.execute(text("ANALYZE chunks, documents, document_versions"))
    with tenant_tx(a.ctx()) as conn:
        statement, params = S._trigram_statement(_trigram_scope(conn), 40)

        def plan_of(sql, parameters):
            raw = conn.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + sql), parameters | {"q": "היטל השבחה"}).scalar_one()
            return list(_plan_nodes((raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]))

        old = [n for n in plan_of(LEGACY_TRIGRAM, {"th": S.TRIGRAM_THRESHOLD, "n": 40}) if n.get("Relation Name") == "chunks"]
        assert sum(n["Actual Loops"] for n in old) > 1  # the fixture shows the old plan
        nodes = plan_of(statement, params)
        calls = [n for n in nodes if n.get("Function Name") == "trigram_candidates"]
        assert len(calls) == 1 and calls[0]["Actual Loops"] == 1
        assert not any("word_similarity" in str(n.get("Filter", "")) for n in nodes)  # no chunk is tested in the query


def _inner_candidate_query(owner_conn):
    """The ``RETURN QUERY`` statement of ``trigram_candidates``, with its parameters as bind names."""
    import re

    src = owner_conn.execute(text("SELECT prosrc FROM pg_proc WHERE proname = 'trigram_candidates'")).scalar_one()
    body = re.search(r"RETURN QUERY\s+(.*?);\s*END", src, re.S).group(1)
    for name in ("p_q", "p_versions", "p_th", "p_n"):
        body = re.sub(rf"\b{name}\b", ":" + name, body)
    return body


def test_the_candidate_scan_uses_the_trigram_index(office, db):
    """Inside the function (no row security on its scan) the planner reads ``<%`` through the GIN index; as
    ``rag_app`` on ``chunks`` it never can (the operator is not leakproof). The function's own settings are applied."""
    a, *_ = office
    _many_reports(a, 8)
    with db.begin() as owner:
        owner.execute(text("ANALYZE chunks"))
        config = owner.execute(text("SELECT proconfig FROM pg_proc WHERE proname = 'trigram_candidates'")).scalar_one()
        assert "enable_seqscan=off" in config
        owner.execute(text("SELECT set_config('app.office_id', :o, true)"), {"o": str(a.office_id)})
        owner.execute(text("SET LOCAL ROLE rag_lookup"))  # BYPASSRLS, as the function runs
        owner.execute(text("SET LOCAL enable_seqscan = off"))
        owner.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.35"))
        inner = _inner_candidate_query(owner)
        plan = "\n".join(r[0] for r in owner.execute(text("EXPLAIN " + inner), {
            "p_q": "היטל השבחה", "p_versions": [uuid4()], "p_th": 0.35, "p_n": 40}))
    assert "chunks_trgm_idx" in plan, plan


def test_trigram_candidates_carry_ids_and_similarity_only(office):
    a, *_ = office
    add_chunks(a, a.default_group_id, TEXTS, "7" * 64)
    with tenant_tx(a.ctx()) as conn:
        versions = [r.id for r in conn.execute(text("SELECT id FROM document_versions"))]
        result = conn.execute(text("SELECT * FROM trigram_candidates(:q, :v, CAST(0.35 AS real), 40)"),
                              {"q": "היטל השבחה", "v": versions})
        assert list(result.keys()) == ["chunk_id", "sim"]
        rows = result.all()
    assert rows and all(0.35 < r.sim <= 1 for r in rows)


def test_another_offices_version_ids_return_nothing(setup):
    a, b, _, da, dg2, dbb = setup
    with tenant_tx(b.ctx()) as conn:
        b_versions = [r.id for r in conn.execute(text("SELECT id FROM document_versions"))]
    assert b_versions
    with tenant_tx(a.ctx()) as conn:
        rows = conn.execute(text("SELECT * FROM trigram_candidates(:q, :v, CAST(0.35 AS real), 40)"),
                            {"q": "השמאי המכריע קבע הפחתה", "v": b_versions}).all()
    assert rows == []


def test_a_version_the_user_cannot_see_yields_no_row_after_the_join_back(setup):
    """The function reads inside the version ids it is given; the statement joins the ids back under row security,
    so a version of a group the user may not see (passed explicitly) never reaches the result."""
    from dataclasses import replace

    from app.platform.search import _scope_sql, _trigram

    a, _, emp, da, dg2, _ = setup
    employee = TenantContext(a.office_id, emp, "employee")
    with tenant_tx(a.ctx()) as conn:
        secret = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": dg2}).scalar_one()
        secret_chunk = conn.execute(text("SELECT id FROM chunks WHERE version_id = :v"), {"v": secret}).scalar_one()
        mine = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": da}).scalar_one()
        admin_hits = _trigram(conn, "השמאי המכריע בפרויקט אחר", replace(_scope_sql(None), version_ids=[secret, mine]), 40)
    assert secret_chunk in admin_hits
    with tenant_tx(employee) as conn:
        hits = _trigram(conn, "השמאי המכריע בפרויקט אחר", replace(_scope_sql(None), version_ids=[secret, mine]), 40)
        assert secret_chunk not in hits and hits  # the visible version's chunks still come back
        assert secret not in {r.id for r in conn.execute(text("SELECT id FROM document_versions"))}


def test_the_search_tool_repeats_no_search_for_an_identical_call_in_a_turn(office, monkeypatch):
    """An identical second ``search`` call of a turn reads the memo; a data-version bump (a document changed
    mid-turn) makes the next one search again (R16, KTD3)."""
    from app.chat import tools as T
    from app.db import bump_data_version

    a, *_ = office
    add_chunks(a, a.default_group_id, TEXTS, "6" * 64)
    searched = []
    real = T.search_passages
    monkeypatch.setattr(T, "search_passages", lambda *args, **kw: (searched.append(args[1]), real(*args, **kw))[1])
    ws = T.Workspace(ctx=a.ctx())
    first = T.tool_search(ws, "היטל השבחה", None, 4)
    again = T.tool_search(ws, "היטל השבחה", None, 4)
    assert searched == ["היטל השבחה"] and "היטל השבחה" in first and again
    with tenant_tx(a.ctx()) as conn:
        bump_data_version(conn)
    T.tool_search(ws, "היטל השבחה", None, 4)
    assert searched == ["היטל השבחה"] * 2


# --- round 9 U9: units, the second level of retrieval (KTD7; R10–R13; AE2, AE3) ----------------------------------------

def _compendium(office, group, **kw):
    """Three appraisals and an appendix of comparables in one file; the second appraisal states no name (its
    project is named only in its passages); a property that is a comparable in the third appraisal's table is the
    second appraisal's project. Every name and figure is invented."""
    from tests.support.unit_files import unit_file

    return unit_file(office, group, "קובץ מרוכז סינתטי", [
        {"title": "פרויקט שדרות האלון", "aliases": ["שדרות האלון", "האלון"], "identifiers": ["שדרות האלון 14"],
         "blocks": ["פרויקט שדרות האלון", "הנכס הוא בניין משרדים בשטח 5,200 מ״ר.", "שווי הנכס הוא 14,850,000 ₪."]},
        {"kind": "appraisal", "certainty": kw.get("second_certainty", "certain"),
         "blocks": ["שומת מגדל הים", "הנכס מגדל הים הוא בניין מגורים בשטח 640 מ״ר.", "שווי מגדל הים הוא 11,776,000 ₪.",
                    "מגדל הים נמצא בסמוך לים."]},
        {"title": "פרויקט נחל הדס", "aliases": ["נחל הדס", "הדס"],
         "blocks": ["פרויקט נחל הדס", "שווי הנכס הוא 9,430,000 ₪.", "|עסקת השוואה: מגדל הים 6,900,000 ₪"]},
        {"kind": "appendix", "title": "נספח: עסקאות השוואה",
         "blocks": ["נספח: עסקאות השוואה", "|בית השקד 4,413,000 ₪", "|מגדל הים 6,900,000 ₪"]},
    ], sha=kw.get("sha", "c" * 64), chunks=kw.get("chunks"))


@pytest.fixture
def compendium(db):
    a = make_office(db, "משרד א", "a@example.test")
    a.doc, a.ver = _compendium(a, a.default_group_id)
    return a


def _matches(office, query):
    from app.platform.search import units_matching

    with tenant_tx(office.ctx()) as conn:
        return units_matching(conn, query)


def test_a_unit_is_found_by_the_name_the_document_states_and_nothing_else_is_a_candidate(compendium):
    m = _matches(compendium, "נחל הדס")
    (doc,) = m.documents
    assert [u["unit_no"] for u in doc["units"]] == [3] and doc["units"][0]["matched_by"] == "alias"
    assert doc["units"][0]["identifiers"] == [] and doc["units"][0]["page_first"] == 3
    assert m.split == [] and m.comparables == []


def test_terms_split_between_two_units_match_no_unit(compendium):
    """AE3: one term in the first appraisal, the other in the third; the file holds both, no appraisal does."""
    from app.platform.search import documents_matching

    m = _matches(compendium, "האלון נחל הדס")
    assert m.documents == [] and [s["title"] for s in m.split] == ["קובץ מרוכז סינתטי"]
    with tenant_tx(compendium.ctx()) as conn:
        assert [str(d["document_id"]) for d in documents_matching(conn, "האלון נחל הדס")] == [compendium.doc]


def test_a_unit_that_states_no_name_is_found_by_its_passages_inside_its_blocks(compendium):
    m = _matches(compendium, "מגדל הים שווי")
    (doc,) = m.documents
    assert [(u["unit_no"], u["matched_by"]) for u in doc["units"]] == [(2, "passage")]


def test_a_property_named_only_in_a_comparison_table_is_a_comparable_never_an_appraisal(compendium):
    # the appendix names "בית השקד"; the third appraisal's table names "מגדל הים" beside prose of the second
    m = _matches(compendium, "בית השקד")
    assert m.documents == [] and [(u["unit_no"], u["kind"]) for u in m.comparables] == [(4, "appendix")]
    m = _matches(compendium, "מגדל הים עסקת השוואה")
    assert {u["unit_no"] for d in m.documents for u in d["units"]} == set()
    assert {u["unit_no"] for u in m.comparables} == {3}  # a comparable in the third appraisal; the second is no table
    both = _matches(compendium, "מגדל הים")
    assert [u["unit_no"] for u in both.units] == [2]  # the prose of the second appraisal holds it
    assert {u["unit_no"] for u in both.comparables} == {3, 4}


def test_a_chunk_that_runs_into_the_next_unit_gives_the_next_unit_only_its_own_blocks(db):
    """The closing chapter of the first appraisal runs on into the second's title block (blocks 2-4 are one chunk):
    the title is the second appraisal's, the chapter's words are the first's."""
    a = make_office(db, "משרד א", "a@example.test")
    _compendium(a, a.default_group_id, chunks=[(2, 3)])  # block 2 closes unit 1; block 3 opens unit 2
    m = _matches(a, "האלון שומת")
    assert m.documents == [] and len(m.split) == 1
    m = _matches(a, "שווי הנכס 14,850,000")
    assert [u["unit_no"] for u in m.units] == [1]  # the chunk begins in, and holds a block of, the first unit


def test_a_chunk_that_begins_in_a_unit_and_runs_into_the_next_title_gives_the_next_unit_none_of_its_words(db):
    """The second appraisal's last block and the third's title block (6 and 7) are one chunk: the title's words are the
    third's, the closing line's ("בסמוך") the second's, so no appraisal holds both (R10, R11)."""
    a = make_office(db, "משרד א", "a@example.test")
    _compendium(a, a.default_group_id, chunks=[(6, 7)])
    m = _matches(a, "נחל הדס בסמוך")
    assert m.documents == [] and len(m.split) == 1
    m = _matches(a, "בסמוך לים")
    assert [u["unit_no"] for u in m.units] == [2]  # the closing line is the second's, the chunk begins in it


def test_a_unit_scope_searches_only_the_chunks_that_begin_inside_its_blocks(compendium):
    from app.platform.search import SearchScope

    with tenant_tx(compendium.ctx()) as conn:
        from app.chat import units as unit_store

        u2 = unit_store.ensure(conn, compendium.ver)[1]
        scoped = hybrid_search(conn, "שווי", 12, scope=SearchScope(version_ids=(UUID(compendium.ver),),
                                                                    blocks=(u2.block_first, u2.block_last)))
        whole = hybrid_search(conn, "שווי", 12, scope=SearchScope(version_ids=(UUID(compendium.ver),)))
    assert scoped and {h["page_list"][0] for h in scoped} == {2}  # each unit is a page of its own here
    assert {h["page_list"][0] for h in whole} > {2}


def test_units_of_a_group_the_user_cannot_see_are_never_candidates(db):
    a = make_office(db, "משרד א", "a@example.test")
    g2 = make_group(a, "קבוצה 2")
    emp = make_user(a, "e@example.test", [a.default_group_id])
    _compendium(a, g2)
    ctx = TenantContext(a.office_id, emp, "employee")
    with tenant_tx(ctx) as conn:
        from app.platform.search import units_matching

        m = units_matching(conn, "נחל הדס")
    assert m.documents == [] and m.comparables == [] and m.split == []
    assert [d for d in _matches(a, "נחל הדס").documents] != []
