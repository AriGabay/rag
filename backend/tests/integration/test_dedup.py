"""Fact publishing, validation flags, and office-scoped deduplication (U6)."""

import json
import threading

import pytest
from sqlalchemy import text

from app.appraisal.publish import publish_version
from app.db import TenantContext, tenant_tx
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

HEADERS = ["כתובת", "גוש/חלקה", "תאריך עסקה", "סוג נכס", "חדרים", "שטח (מ״ר)", "סוג שטח", "מחיר (₪)", "מחיר למ״ר (₪)"]
HEADER_TEXT = (
    "שומת מקרקעין - מסמך סינתטי לדמו\nעיר: רמת גן\nשכונה: חרוזים\nכתובת הנכס: הרואה 10\n"
    "גוש: 6158 חלקה: 42 תת חלקה: 7\nהמועד הקובע: 15/03/2024\nתאריך עריכת השומה: 01/04/2024\n"
    "שטח הנכס: 95 מ״ר נטו\nשווי הנכס: 2,500,000 ₪\n"
)
ROW_SHARED = ["הרואה 5", "6158/40", "10/01/2024", "דירה", "4", "95", "נטו", "2,375,000", "25,000"]


def add_version(office, group_id, rows, sha, header=HEADER_TEXT):
    doc_id, ver_id = make_document(office, group_id, f"דוח {sha[:4]}", sha=sha)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET status = 'processing', is_current = false WHERE id = :v"),
                     {"v": ver_id})
        conn.execute(
            text("INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok)"
                 " VALUES (app_office(), :d, :v, 1, :t, 'text_layer', 0.99, true)"),
            {"d": doc_id, "v": ver_id, "t": header},
        )
        structure = {"headers": HEADERS, "units": [], "ocr": False, "section": "3. עסקאות השוואה",
                     "rows": [{"page": 2, "cells": r} for r in rows]}
        conn.execute(
            text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start, page_end,"
                 " structure) VALUES (app_office(), :d, :v, 0, 2, 2, CAST(:s AS jsonb))"),
            {"d": doc_id, "v": ver_id, "s": json.dumps(structure, ensure_ascii=False)},
        )
    return doc_id, ver_id


def publish(office, doc_id, ver_id):
    with tenant_tx(office.system) as conn:
        return publish_version(conn, ver_id, doc_id, "test")


@pytest.fixture
def offices(db):
    return make_office(db, "משרד א", "a@example.test"), make_office(db, "משרד ב", "b@example.test")


def q(office, sql, **params):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), params).all()


def test_records_get_provenance_lineage_and_flags(offices):
    a, _ = offices
    rows = [ROW_SHARED, ["ביאליק 3", "6158/41", "05/02/2024", "דירה", "3", "50", "נטו", "1,000,000", "21,000"],
            ["ז'בוטינסקי 1", "6158/43", "06/02/2024", "דירה", "3", "", "נטו", "1,100,000", ""]]
    doc, ver = add_version(a, a.default_group_id, rows, "1" * 64)
    assert publish(a, doc, ver) == "needs_review"
    occ = {r.address: r for r in q(a, "SELECT * FROM occurrences WHERE data_kind = 'transaction_price'")}
    assert str(occ["ביאליק 3"].price_per_sqm_computed) == "20000.00" and occ["ביאליק 3"].conflict_flag
    assert str(occ["ביאליק 3"].price_per_sqm_stated) == "21000.00"
    assert occ["ז׳בוטינסקי 1"].missing_critical == ["area"]
    assert occ["ז׳בוטינסקי 1"].price_per_sqm_computed is None
    ok = occ["הרואה 5"]
    assert ok.verification_status == "auto_extracted" and ok.page_no == 2 and ok.row_index == 0
    assert ok.city == "רמת גן" and ok.neighborhood == "חרוזים" and str(ok.valuation_date) == "2024-03-15"
    lineage = q(a, "SELECT original_text, source_path FROM fact_values WHERE occurrence_id = :o"
                   " AND field = 'price_per_sqm_computed'", o=ok.id)[0]
    assert lineage.source_path["lineage"] == ["price", "area"] and "2375000" in lineage.original_text
    inherited = q(a, "SELECT source_path FROM fact_values WHERE occurrence_id = :o AND field = 'neighborhood'", o=ok.id)
    assert inherited[0].source_path["inherited_from"] == "report_header"
    subject = q(a, "SELECT * FROM occurrences WHERE data_kind = 'appraised_value'")[0]
    assert str(subject.price) == "2500000.00" and subject.area_type == "net" and subject.page_no == 1


def test_same_comparable_in_two_reports_is_one_transaction(offices):
    a, _ = offices
    d1, v1 = add_version(a, a.default_group_id, [ROW_SHARED], "1" * 64)
    d2, v2 = add_version(a, a.default_group_id, [ROW_SHARED], "2" * 64)
    publish(a, d1, v1)
    publish(a, d2, v2)
    txns = q(a, "SELECT transaction_id, count(*) AS n FROM occurrences WHERE address = 'הרואה 5' GROUP BY 1")
    assert len(txns) == 1 and txns[0].n == 2


def test_different_area_basis_is_a_candidate_not_a_merge(offices):
    a, _ = offices
    gross = ["הרואה 5", "6158/40", "10/01/2024", "דירה", "4", "110", "ברוטו", "2,375,000", ""]
    d1, v1 = add_version(a, a.default_group_id, [ROW_SHARED], "1" * 64)
    d2, v2 = add_version(a, a.default_group_id, [gross], "2" * 64)
    publish(a, d1, v1)
    assert publish(a, d2, v2) == "needs_review"
    assert len(q(a, "SELECT DISTINCT transaction_id FROM occurrences WHERE address = 'הרואה 5'")) == 2
    cand = q(a, "SELECT reason, status FROM dedup_candidates")
    assert len(cand) == 1 and cand[0].status == "open" and "סוג שטח" in cand[0].reason


def test_address_match_with_missing_area_type_is_a_candidate(offices):
    a, _ = offices
    no_type = ["הרואה 5", "", "10/01/2024", "דירה", "4", "95", "", "2,375,000", ""]
    no_bp = ["הרואה 5", "", "10/01/2024", "דירה", "4", "95", "נטו", "2,375,000", ""]
    d1, v1 = add_version(a, a.default_group_id, [no_bp], "1" * 64)
    d2, v2 = add_version(a, a.default_group_id, [no_type], "2" * 64)
    publish(a, d1, v1)
    publish(a, d2, v2)
    assert len(q(a, "SELECT DISTINCT transaction_id FROM occurrences WHERE address = 'הרואה 5'")) == 2
    assert len(q(a, "SELECT 1 FROM dedup_candidates")) == 1


def test_same_comparable_in_two_offices_stays_separate(offices):
    a, b = offices
    da, va = add_version(a, a.default_group_id, [ROW_SHARED], "1" * 64)
    db_, vb = add_version(b, b.default_group_id, [ROW_SHARED], "1" * 64)
    publish(a, da, va)
    publish(b, db_, vb)
    ta = q(a, "SELECT id FROM transactions WHERE data_kind = 'transaction_price'")
    tb = q(b, "SELECT id FROM transactions WHERE data_kind = 'transaction_price'")
    assert len(ta) == 1 and len(tb) == 1 and ta[0].id != tb[0].id
    assert q(a, "SELECT 1 FROM dedup_candidates") == []


def test_merged_transaction_shows_only_visible_occurrence_and_fields(offices):
    a, _ = offices
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    emp = make_user(a, "g1@example.test", [g1])
    no_neighborhood = HEADER_TEXT.replace("שכונה: חרוזים\n", "")
    d1, v1 = add_version(a, g1, [ROW_SHARED], "1" * 64, header=no_neighborhood)
    d2, v2 = add_version(a, g2, [ROW_SHARED], "2" * 64)
    publish(a, d1, v1)
    publish(a, d2, v2)
    with tenant_tx(TenantContext(a.office_id, emp, "employee")) as conn:
        occ = conn.execute(text("SELECT document_id, neighborhood FROM occurrences WHERE address = 'הרואה 5'")).all()
        assert [o.document_id for o in occ] == [d1]
        assert occ[0].neighborhood is None  # stated only in the G2 report
        assert conn.execute(text("SELECT count(*) FROM occurrences WHERE neighborhood = 'חרוזים'")).scalar() == 0


def test_republish_is_idempotent(offices):
    a, _ = offices
    d1, v1 = add_version(a, a.default_group_id, [ROW_SHARED], "1" * 64)
    publish(a, d1, v1)
    publish(a, d1, v1)
    assert len(q(a, "SELECT 1 FROM occurrences")) == 2
    assert len(q(a, "SELECT 1 FROM transactions")) == 2


def test_concurrent_publishes_share_one_transaction(offices):
    a, _ = offices
    versions = [add_version(a, a.default_group_id, [ROW_SHARED], f"{i}" * 64) for i in (1, 2)]
    errors = []

    def run(doc, ver):
        try:
            publish(a, doc, ver)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=v) for v in versions]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(q(a, "SELECT DISTINCT transaction_id FROM occurrences WHERE address = 'הרואה 5'")) == 1
