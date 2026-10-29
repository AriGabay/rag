"""Entity scoping: documents named by an address, a block/parcel or a title fragment (KTD10, R9-R11)."""

import itertools

import pytest
from sqlalchemy import text

from app.db import TenantContext, tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.platform import pipeline
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

_SHA = itertools.count(1)


def _persist(office, doc, ver, chunks):
    """``chunks``: (text, pages) or (text, pages, kind) tuples, in reading order."""
    pages = sorted({p for c in chunks for p in c[1]}) or [1]
    page_results = [PageResult(p, "\n".join(c[0] for c in chunks if p in c[1]), "text_layer", 1.0, True)
                    for p in pages]
    result = ExtractionResult(len(pages), page_results, [],
                              [ChunkResult(i, c[2] if len(c) > 2 else "text", list(c[1]), None, c[0])
                               for i, c in enumerate(chunks)])
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(office.system, info, 1e18)


def add_doc(office, group_id, chunks, title="מסמך"):
    doc, ver = make_document(office, group_id, title, sha=f"{next(_SHA):064x}")
    _persist(office, doc, ver, chunks)
    return doc, ver


def add_version(office, doc, chunks):
    """A new current version of ``doc``; the previous one becomes superseded."""
    with tenant_tx(office.ctx()) as conn:
        n = conn.execute(text("SELECT max(version_no) FROM document_versions WHERE document_id = :d"),
                         {"d": doc}).scalar_one()
        conn.execute(text("UPDATE document_versions SET is_current = false, status = 'superseded'"
                          " WHERE document_id = :d"), {"d": doc})
        ver = conn.execute(
            text("INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
                 " size_bytes, storage_key, status, is_current) VALUES (app_office(), :d, :n, :s, 'f.pdf',"
                 " 'application/pdf', 10, 'k', 'ready', true) RETURNING id"),
            {"d": doc, "n": n + 1, "s": f"{next(_SHA):064x}"},
        ).scalar_one()
    _persist(office, doc, ver, chunks)
    return ver


def report(place, city, body="תיאור הנכס: דירת 4 חדרים בקומה שנייה.", extra=()):
    """A short appraisal: a heading naming the subject address, a labeled address line, then a body."""
    return [(f"שומת מקרקעין — {place}, {city}\nעיר: {city}\nכתובת הנכס: {place}", [1]), (body, [1]), *extra]


def resolve(ctx, entities, question=""):
    from app.answering.entities import resolve_entities

    with tenant_tx(ctx) as conn:
        return resolve_entities(conn, entities, question=question)


@pytest.fixture
def office(db):
    a = make_office(db, "משרד ישויות", "ent@example.test")
    hidden = make_group(a, "נסתר")
    emp = make_user(a, "ent-emp@example.test", [a.default_group_id])
    return a, hidden, emp


def test_street_and_number_with_prefixes_and_article(office):
    a, *_ = office
    g = a.default_group_id
    seven, seven_v = add_doc(a, g, report("הדקלים 7", "חולון"))
    nine, _ = add_doc(a, g, report("הדקלים 9", "חולון"))
    # a comparables table elsewhere names "הדקלים 7": a body mention never outranks the subject's own report
    add_doc(a, g, [*report("התאנה 3", "חולון"), ("כתובת: הדקלים 7 | חדרים: 4 | מחיר (₪): 2,000,000", [2], "table_row")])
    for entity in ("רחוב הדקלים 7", "הדקלים 7", "דקלים 7", "ברח׳ הדקלים 7", "רח' הדקלים מס' 7", "בדקלים 7"):
        m = resolve(a.ctx(), [entity])
        assert m.status == "unique", entity
        assert m.document_ids == (seven,), entity
        assert m.version_ids == (seven_v,)
        ev = m.resolutions[0].documents[0].evidence[0]
        assert ev.where == "header" and ev.page_list == [1] and "הדקלים 7" in ev.text
    assert resolve(a.ctx(), ["הדקלים 9"]).document_ids == (nine,)
    none = resolve(a.ctx(), ["הדקלים 70"])
    assert none.status == "none" and none.document_ids == ()
    assert resolve(a.ctx(), ["הדקלים 7"]).resolutions[0].kind == "address"


def test_same_address_in_two_documents_is_ambiguous_and_a_named_city_narrows_it(office):
    a, *_ = office
    g = a.default_group_id
    ks, _ = add_doc(a, g, report("ויצמן 40", "כפר סבא"))
    gv, _ = add_doc(a, g, report("ויצמן 40", "גבעתיים"))
    both = resolve(a.ctx(), ["ויצמן 40"])
    assert both.status == "ambiguous" and set(both.document_ids) == {ks, gv}
    assert resolve(a.ctx(), ["ויצמן 40"], question="מה שטח הדירה בויצמן 40 בגבעתיים?").document_ids == (gv,)
    assert resolve(a.ctx(), ["ויצמן 40, כפר סבא"]).document_ids == (ks,)


def test_two_appraisals_of_one_apartment_both_match(office):
    a, *_ = office
    g = a.default_group_id
    first, _ = add_doc(a, g, report("בן צבי 14", "חיפה"))
    second, _ = add_doc(a, g, [("חוות דעת שמאית\nכתובת: שדרות בן צבי 14, חיפה", [1]), ("תיאור הבניין.", [1])])
    m = resolve(a.ctx(), ["רחוב בן צבי 14"])
    assert m.status == "ambiguous" and set(m.document_ids) == {first, second}


def test_gershayim_variants_match(office):
    a, *_ = office
    doc, _ = add_doc(a, a.default_group_id, report("רמב״ם 3", "נתניה"))
    assert resolve(a.ctx(), ['רמב"ם 3']).document_ids == (doc,)
    assert resolve(a.ctx(), ["רמב''ם 3"]).document_ids == (doc,)


def test_block_and_parcel(office):
    a, *_ = office
    g = a.default_group_id
    doc, _ = add_doc(a, g, [("שומת מקרקעין — הנרקיס 2, חולון\nגוש: 6158 חלקה: 42 תת חלקה: 3", [1])])
    add_doc(a, g, [("שומת מקרקעין — הנרקיס 4, חולון\nגוש: 6158 חלקה: 420", [1])])
    for entity in ("גוש 6158 חלקה 42", "גוש/חלקה 6158/42", "6158/42"):
        m = resolve(a.ctx(), [entity])
        assert m.document_ids == (doc,), entity
        assert m.resolutions[0].kind == "block_parcel"


def test_document_versions_are_listed_and_current_text_matches(office):
    a, *_ = office
    doc, v1 = add_doc(a, a.default_group_id, report("הגפן 30", "רחובות"))
    v2 = add_version(a, doc, report("הגפן 30", "רחובות", body="גרסה מעודכנת."))
    m = resolve(a.ctx(), ["הגפן 30"])
    matched = m.resolutions[0].documents[0]
    assert matched.current_version_id == v2 and m.version_ids == (v2,)
    assert [(v.version_id, v.is_current) for v in matched.versions] == [(v2, True), (v1, False)]
    assert [v.version_no for v in matched.versions] == [2, 1]


def test_title_fragment(office):
    a, *_ = office
    g = a.default_group_id
    doc, _ = add_doc(a, g, [("תיאור כללי של המגרש.", [1])], title="שומה מגדל הים 2023")
    add_doc(a, g, [("תיאור כללי של המגרש.", [1])], title="שומה אחרת")
    m = resolve(a.ctx(), ["מגדל הים"])
    assert m.document_ids == (doc,) and m.resolutions[0].kind == "title"
    assert m.resolutions[0].documents[0].evidence[0].where == "title"


def test_question_is_the_fallback_when_no_entity_is_given(office):
    a, *_ = office
    g = a.default_group_id
    doc, _ = add_doc(a, g, report("השקדים 5", "יבנה"))
    add_doc(a, g, report("השקדים 50", "יבנה"))
    m = resolve(a.ctx(), [], question="מה גודל החדר בשקדים 5 ביבנה לפי השומה מ-2024?")
    assert m.document_ids == (doc,)
    assert "השקדים 5" in " ".join(m.terms) or "שקדים 5" in " ".join(m.terms)
    assert resolve(a.ctx(), [], question="מה הממוצע ב-2024?").status == "none"


def test_terms_strip_the_entity_from_topic_tokens(office):
    from app.platform.search import topic_tokens

    a, *_ = office
    add_doc(a, a.default_group_id, report("הדקלים 7", "חולון"))
    m = resolve(a.ctx(), ["רחוב הדקלים 7"])
    assert topic_tokens("מה סוג הקרקע ברחוב הדקלים 7?", m.terms) == topic_tokens("מה סוג הקרקע?")


def test_employee_never_resolves_a_hidden_document(office):
    a, hidden, emp = office
    add_doc(a, hidden, report("הזית 8", "לוד"))
    emp_ctx = TenantContext(a.office_id, emp, "employee")
    assert resolve(emp_ctx, ["הזית 8"]).status == "none"
    assert resolve(a.ctx(), ["הזית 8"]).status == "unique"


def test_several_entities_each_resolved(office):
    a, *_ = office
    g = a.default_group_id
    one, _ = add_doc(a, g, report("הדקלים 7", "חולון"))
    two, _ = add_doc(a, g, report("התמר 11", "חולון"))
    m = resolve(a.ctx(), ["הדקלים 7", "התמר 11"])
    assert m.status == "unique" and m.document_ids == (one, two)
    assert [r.status for r in m.resolutions] == ["unique", "unique"]
    partial = resolve(a.ctx(), ["הדקלים 7", "התמר 99"])
    assert partial.status == "partial" and partial.document_ids == (one,)
    scope = m.scope()
    assert scope.document_ids == (one, two)
