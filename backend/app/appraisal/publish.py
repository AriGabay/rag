"""Publish a processed version (U6): facts, validation, dedup, status, data version — one transaction.

Runs under the worker's ``system`` context. Idempotent: prior facts of this version are removed
first, so a retried job publishes exactly one set. The office advisory lock serializes dedup
across workers (two reports sharing a comparable never create two transactions)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from app.appraisal.dedup import TxnFacts, attach_transaction, delete_orphans, find_uncertain, lock_office
from app.appraisal.extract import RecordDraft, as_date, as_decimal, extract_records
from app.appraisal.validate import validate
from app.db import bump_data_version

OCC_FIELDS = (
    "city", "neighborhood", "address", "block", "parcel", "sub_parcel", "property_type", "rooms",
    "transaction_date", "valuation_date", "report_date", "area", "area_type", "price", "vat_basis",
    "price_per_sqm_stated",
)


def _json_value(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, Decimal)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    if isinstance(value, tuple):
        return "/".join(v for v in value if v)
    return str(value)


def facts_of(rec: RecordDraft, computed: Decimal | None, definition: str | None) -> TxnFacts:
    def s(name):
        v = rec.get(name)
        return v if isinstance(v, str) else None

    return TxnFacts(
        data_kind=rec.data_kind, block=s("block"), parcel=s("parcel"), sub_parcel=s("sub_parcel"),
        address=s("address"), property_type=s("property_type"),
        transaction_date=as_date(rec.get("transaction_date")), valuation_date=as_date(rec.get("valuation_date")),
        report_date=as_date(rec.get("report_date")), price=as_decimal(rec.get("price")),
        area=as_decimal(rec.get("area")), area_type=s("area_type"), price_per_sqm=computed,
        calc_definition=definition,
    )


def insert_occurrence(conn: Connection, version_id: UUID, document_id: UUID, record_index: int, rec: RecordDraft,
                      txn_id: UUID, extraction_version: str) -> UUID:
    val = validate(rec)
    params = {name: rec.get(name) for name in OCC_FIELDS}
    occ_id = conn.execute(
        text(
            "INSERT INTO occurrences (office_id, transaction_id, document_id, version_id, record_index, page_no,"
            " table_index, row_index, text_span, extraction_version, data_kind, city, neighborhood, address, block,"
            " parcel, sub_parcel, property_type, rooms, transaction_date, valuation_date, report_date, area,"
            " area_type, price, currency, vat_basis, price_per_sqm_stated, price_per_sqm_computed, conflict_flag,"
            " missing_critical, ocr, verification_status)"
            " VALUES (app_office(), :txn, :doc, :ver, :idx, :page, :ti, :ri, :span, :ev, :kind, :city, :neighborhood,"
            " :address, :block, :parcel, :sub_parcel, :property_type, :rooms, :transaction_date, :valuation_date,"
            " :report_date, :area, :area_type, :price, 'ILS', :vat_basis, :price_per_sqm_stated, :computed, :conflict,"
            " :missing, :ocr, :status) RETURNING id"
        ),
        params | {"txn": txn_id, "doc": document_id, "ver": version_id, "idx": record_index, "page": rec.page_no,
                  "ti": rec.table_index, "ri": rec.row_index, "span": rec.text_span, "ev": extraction_version,
                  "kind": rec.data_kind, "computed": val.computed_ppsqm, "conflict": val.conflict,
                  "missing": val.missing_critical, "ocr": rec.ocr, "status": val.status},
    ).scalar_one()
    for name, fv in rec.fields.items():
        if name not in OCC_FIELDS:
            continue
        conn.execute(
            text(
                "INSERT INTO fact_values (office_id, document_id, occurrence_id, field, original_text,"
                " normalized_value, source_path, extraction_version, status)"
                " VALUES (app_office(), :doc, :occ, :f, :o, :n, CAST(:sp AS jsonb), :ev, 'extracted')"
            ),
            {"doc": document_id, "occ": occ_id, "f": name, "o": fv.original, "n": _json_value(fv.value),
             "sp": json.dumps(fv.source | {"version_id": str(version_id)}, ensure_ascii=False), "ev": extraction_version},
        )
    if val.computed_ppsqm is not None:
        conn.execute(
            text(
                "INSERT INTO fact_values (office_id, document_id, occurrence_id, field, original_text,"
                " normalized_value, source_path, extraction_version, status)"
                " VALUES (app_office(), :doc, :occ, 'price_per_sqm_computed', :o, :n, CAST(:sp AS jsonb), :ev, 'computed')"
            ),
            {"doc": document_id, "occ": occ_id, "o": val.calc_definition, "n": str(val.computed_ppsqm),
             "sp": json.dumps({"lineage": ["price", "area"], "version_id": str(version_id)}), "ev": extraction_version},
        )
    return occ_id


def clear_version_facts(conn: Connection, version_id: UUID) -> None:
    conn.execute(
        text("DELETE FROM fact_values WHERE occurrence_id IN (SELECT id FROM occurrences WHERE version_id = :v)"),
        {"v": version_id},
    )
    conn.execute(text("DELETE FROM occurrences WHERE version_id = :v"), {"v": version_id})
    delete_orphans(conn)


def refresh_version_status(conn: Connection, version_id: UUID) -> str:
    """Recompute counts and status (ready / needs_review) for a published version."""
    row = conn.execute(
        text(
            "SELECT count(*) AS total,"
            " count(*) FILTER (WHERE verification_status = 'needs_review') AS review,"
            " (SELECT pages_incomplete FROM document_versions WHERE id = :v) AS pages_incomplete,"
            " (SELECT count(*) FROM dedup_candidates c WHERE c.status = 'open' AND (c.transaction_a IN"
            "   (SELECT transaction_id FROM occurrences WHERE version_id = :v) OR c.transaction_b IN"
            "   (SELECT transaction_id FROM occurrences WHERE version_id = :v))) AS open_candidates"
            " FROM occurrences WHERE version_id = :v"
        ),
        {"v": version_id},
    ).one()
    status = "needs_review" if (row.review or row.pages_incomplete or row.open_candidates) else "ready"
    conn.execute(
        text(
            "UPDATE document_versions SET records_total = :t, records_needing_review = :r,"
            " status = CASE WHEN status IN ('ready', 'needs_review', 'processing') THEN :s ELSE status END"
            " WHERE id = :v"
        ),
        {"t": row.total, "r": row.review, "s": status, "v": version_id},
    )
    return status


def publish_version(conn: Connection, version_id: UUID, document_id: UUID, extraction_version: str) -> str:
    lock_office(conn)
    clear_version_facts(conn, version_id)

    pages = conn.execute(
        text("SELECT page_no, text, method FROM pages WHERE version_id = :v ORDER BY page_no"), {"v": version_id}
    ).all()
    tables = conn.execute(
        text("SELECT table_index, structure FROM extracted_tables WHERE version_id = :v ORDER BY table_index"),
        {"v": version_id},
    ).all()
    drafts = extract_records(
        [(p.page_no, p.text) for p in pages],
        [{"index": t.table_index, **t.structure} for t in tables],
        {p.page_no for p in pages if p.method == "ocr"},
    )
    for idx, rec in enumerate(drafts):
        val = validate(rec)
        facts = facts_of(rec, val.computed_ppsqm, val.calc_definition)
        txn_id, _ = attach_transaction(conn, facts)
        insert_occurrence(conn, version_id, document_id, idx, rec, txn_id, extraction_version)
        find_uncertain(conn, txn_id, facts, version_id)

    conn.execute(
        text(
            "UPDATE document_versions SET is_current = false, status = 'superseded'"
            " WHERE document_id = :d AND id <> :v AND is_current"
        ),
        {"d": document_id, "v": version_id},
    )
    conn.execute(
        text("UPDATE document_versions SET is_current = true, processed_at = now(), status_reason = NULL WHERE id = :v"),
        {"v": version_id},
    )
    status = refresh_version_status(conn, version_id)
    bump_data_version(conn)
    return status
