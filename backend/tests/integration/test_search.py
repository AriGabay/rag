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
