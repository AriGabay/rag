"""Parametric SQL over every authorized, verified, current record (KTD2, R20, R21).

Only whitelisted predicates are composed; every value is a bound parameter. Runs inside the
user's tenant transaction, so RLS limits it to documents the user may see. Statistics are over
unique transactions; a transaction counts once however many reports cite it."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from app.answering.conditions import QueryConditions

VERIFIED = ("human_verified", "corrected")
AWAITING = ("auto_extracted", "needs_review")
_DATE_COLUMNS = {"transaction_date": "o.transaction_date", "valuation_date": "o.valuation_date",
                 "report_date": "o.report_date"}


@dataclass
class Stats:
    count: int
    mean: Decimal | None
    weighted: Decimal | None
    median: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    transaction_ids: list[UUID] = field(default_factory=list)


def _filters(c: QueryConditions, statuses: tuple[str, ...]) -> tuple[str, dict]:
    clauses = [
        "o.verification_status = ANY(:statuses)",
        "o.data_kind = :kind",
        "v.is_current",
        "d.deleted_at IS NULL",
    ]
    params: dict = {"statuses": list(statuses), "kind": c.data_kind}
    if c.city:
        clauses.append("o.city = :city")
        params["city"] = c.city
    if c.neighborhood:
        clauses.append("o.neighborhood = :neighborhood")
        params["neighborhood"] = c.neighborhood
    if c.property_type:
        clauses.append("o.property_type = :property_type")
        params["property_type"] = c.property_type
    if c.area_type:
        clauses.append("t.area_type = :area_type")
        params["area_type"] = c.area_type
    if c.vat_basis:
        clauses.append("o.vat_basis = :vat_basis")
        params["vat_basis"] = c.vat_basis
    rng = c.date_range()
    if rng is not None:
        column = _DATE_COLUMNS[c.date_field]  # date_field is a Literal; never user text
        clauses.append(f"{column} >= :date_from AND {column} < :date_to")
        params |= {"date_from": rng[0], "date_to": rng[1]}
    return " AND ".join(clauses), params


_BASE = (
    " FROM transactions t JOIN occurrences o ON o.transaction_id = t.id"
    " JOIN document_versions v ON v.id = o.version_id"
    " JOIN documents d ON d.id = o.document_id"
)


def eligible_ids(conn: Connection, c: QueryConditions, statuses: tuple[str, ...] = VERIFIED) -> list[UUID]:
    where, params = _filters(c, statuses)
    return list(conn.execute(text(f"SELECT DISTINCT t.id{_BASE} WHERE {where}"), params).scalars())


def compute_stats(conn: Connection, c: QueryConditions) -> Stats:
    """One statement over all matching unique transactions with a price and a positive area."""
    where, params = _filters(c, VERIFIED)
    row = conn.execute(
        text(
            "WITH eligible AS (SELECT DISTINCT t.id, t.price, t.area, t.price_per_sqm"
            f"{_BASE} WHERE {where} AND t.price IS NOT NULL AND t.area > 0)"
            " SELECT count(*) AS n, round(avg(price_per_sqm), 2) AS mean,"
            " round(sum(price) / NULLIF(sum(area), 0), 2) AS weighted,"
            " round(CAST(percentile_cont(0.5) WITHIN GROUP (ORDER BY price_per_sqm) AS numeric), 2) AS median,"
            " min(price_per_sqm) AS mn, max(price_per_sqm) AS mx, array_agg(id) AS ids FROM eligible"
        ),
        params,
    ).one()
    return Stats(row.n, row.mean, row.weighted, row.median, row.mn, row.mx, list(row.ids or []))


def distinct_values(conn: Connection, c: QueryConditions, column: str) -> list[tuple[str, int]]:
    """Distinct values of a result-changing attribute among matching verified records."""
    allowed = {"area_type": "t.area_type", "property_type": "o.property_type", "vat_basis": "o.vat_basis"}
    col = allowed[column]
    where, params = _filters(c, VERIFIED)
    rows = conn.execute(
        text(f"SELECT {col} AS v, count(DISTINCT t.id) AS n{_BASE} WHERE {where} AND t.price IS NOT NULL"
             f" AND t.area > 0 GROUP BY {col} ORDER BY n DESC"),
        params,
    ).all()
    return [(r.v, r.n) for r in rows if r.v is not None]


def sources_for(conn: Connection, txn_ids: list[UUID]) -> list[dict]:
    if not txn_ids:
        return []
    rows = conn.execute(
        text(
            "SELECT o.id AS occurrence_id, o.transaction_id, o.document_id, o.version_id, o.page_no, o.row_index,"
            " o.text_span, o.address, o.conflict_flag, d.title, v.mime_type"
            " FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
            " WHERE o.transaction_id = ANY(:ids) ORDER BY d.title, o.page_no, o.row_index"
        ),
        {"ids": txn_ids},
    ).all()
    return [dict(r._mapping) for r in rows]


def uncertain_duplicates(conn: Connection, txn_ids: list[UUID]) -> int:
    if not txn_ids:
        return 0
    return conn.execute(
        text("SELECT count(*) FROM dedup_candidates WHERE status = 'open'"
             " AND transaction_a = ANY(:ids) AND transaction_b = ANY(:ids)"),
        {"ids": txn_ids},
    ).scalar_one()


def conflicts(conn: Connection, txn_ids: list[UUID]) -> int:
    if not txn_ids:
        return 0
    return conn.execute(
        text("SELECT count(DISTINCT transaction_id) FROM occurrences WHERE conflict_flag AND transaction_id = ANY(:ids)"),
        {"ids": txn_ids},
    ).scalar_one()
