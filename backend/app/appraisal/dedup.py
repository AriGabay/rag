"""Transaction identity and deduplication within one office (R14, KTD2).

A certain duplicate shares location (block/parcel/sub-parcel, or full normalized address when no
block/parcel), data kind, transaction date, price, area and area type, and does not disagree on
property type. Those occurrences attach to one transaction. Anything weaker that still looks
alike (same location and same date or same price) becomes a separate transaction plus an open
``dedup_candidates`` row for a reviewer — never a silent merge.

Callers hold the per-office advisory lock (``lock_office``) so concurrent publishes cannot race.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

DEDUP_KINDS = ("transaction_price", "asking_price", "adjusted_comparable")


@dataclass
class TxnFacts:
    data_kind: str
    block: str | None
    parcel: str | None
    sub_parcel: str | None
    address: str | None
    property_type: str | None
    transaction_date: date | None
    valuation_date: date | None
    report_date: date | None
    price: Decimal | None
    area: Decimal | None
    area_type: str | None
    price_per_sqm: Decimal | None
    calc_definition: str | None


def lock_office(conn: Connection) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(app_office()::text, 7))"))


def location_key(f: TxnFacts) -> str | None:
    if f.block and f.parcel:
        return f"bp:{f.block}/{f.parcel}/{f.sub_parcel or '-'}"
    if f.address:
        return f"addr:{f.address}"
    return None


def match_key(f: TxnFacts) -> str | None:
    loc = location_key(f)
    if f.data_kind not in DEDUP_KINDS or not loc:
        return None
    if None in (f.transaction_date, f.price, f.area, f.area_type):
        return None
    return f"{loc}|{f.data_kind}|{f.transaction_date.isoformat()}|{f.price.normalize()}|{f.area.normalize()}|{f.area_type}"


def _insert_transaction(conn: Connection, f: TxnFacts, key: str | None) -> UUID | None:
    return conn.execute(
        text(
            "INSERT INTO transactions (office_id, data_kind, match_key, price, currency, area, area_type,"
            " transaction_date, valuation_date, report_date, price_per_sqm, calc_definition)"
            " VALUES (app_office(), :k, :mk, :p, 'ILS', :a, :at, :td, :vd, :rd, :pp, :cd)"
            " ON CONFLICT (office_id, match_key) WHERE match_key IS NOT NULL DO NOTHING RETURNING id"
        ),
        {"k": f.data_kind, "mk": key, "p": f.price, "a": f.area, "at": f.area_type, "td": f.transaction_date,
         "vd": f.valuation_date if f.data_kind == "appraised_value" else None,
         "rd": f.report_date if f.data_kind == "appraised_value" else None,
         "pp": f.price_per_sqm, "cd": f.calc_definition},
    ).scalar_one_or_none()


def _property_types(conn: Connection, txn_id: UUID) -> set[str]:
    return set(
        conn.execute(
            text("SELECT DISTINCT property_type FROM occurrences WHERE transaction_id = :t AND property_type IS NOT NULL"),
            {"t": txn_id},
        ).scalars()
    )


def add_candidate(conn: Connection, a: UUID, b: UUID, reason: str) -> None:
    first, second = sorted([a, b], key=str)
    conn.execute(
        text(
            "INSERT INTO dedup_candidates (office_id, transaction_a, transaction_b, reason)"
            " VALUES (app_office(), :a, :b, :r) ON CONFLICT (transaction_a, transaction_b) DO NOTHING"
        ),
        {"a": first, "b": second, "r": reason},
    )


def attach_transaction(conn: Connection, f: TxnFacts) -> tuple[UUID, bool]:
    """Return (transaction_id, merged_with_existing)."""
    key = match_key(f)
    if key:
        existing = conn.execute(text("SELECT id FROM transactions WHERE match_key = :k"), {"k": key}).scalar_one_or_none()
        if existing:
            types = _property_types(conn, existing)
            if not f.property_type or not types or f.property_type in types:
                return existing, True
            new_id = _insert_transaction(conn, f, None)
            add_candidate(conn, existing, new_id, "התאמה במיקום, תאריך, מחיר ושטח אך סוג נכס שונה")
            return new_id, False
        new_id = _insert_transaction(conn, f, key)
        if new_id is None:  # lost a race despite the lock: take the winner
            new_id = conn.execute(text("SELECT id FROM transactions WHERE match_key = :k"), {"k": key}).scalar_one()
            return new_id, True
        return new_id, False
    return _insert_transaction(conn, f, None), False


def find_uncertain(conn: Connection, txn_id: UUID, f: TxnFacts, version_id: UUID) -> int:
    """Open candidates against look-alike transactions in current versions. Returns how many."""
    if f.data_kind not in DEDUP_KINDS:
        return 0
    if f.block and f.parcel:
        where = "o.block = :b AND o.parcel = :p AND coalesce(o.sub_parcel, '-') = coalesce(:s, '-')"
    elif f.address:
        where = "o.address = :addr"
    else:
        return 0
    rows = conn.execute(
        text(
            "SELECT DISTINCT o.transaction_id FROM occurrences o"
            " JOIN document_versions v ON v.id = o.version_id"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
            f" WHERE {where} AND o.data_kind = :k AND o.transaction_id <> :t"
            " AND (v.is_current OR v.id = :v)"
            " AND (o.transaction_date = :td OR o.price = :pr)"
        ),
        {"b": f.block, "p": f.parcel, "s": f.sub_parcel, "addr": f.address, "k": f.data_kind, "t": txn_id,
         "v": version_id, "td": f.transaction_date, "pr": f.price},
    ).scalars().all()
    for other in rows:
        add_candidate(conn, txn_id, other, _reason(conn, other, f))
    return len(rows)


def _reason(conn: Connection, other: UUID, f: TxnFacts) -> str:
    row = conn.execute(
        text("SELECT transaction_date, price, area, area_type FROM transactions WHERE id = :t"), {"t": other}
    ).one()
    diffs = []
    if row.transaction_date != f.transaction_date:
        diffs.append("תאריך")
    if row.price != f.price:
        diffs.append("מחיר")
    if row.area != f.area:
        diffs.append("שטח")
    if row.area_type != f.area_type:
        diffs.append("סוג שטח")
    return "אותו מיקום; שונה: " + (", ".join(diffs) if diffs else "פרטים חסרים")


def delete_orphans(conn: Connection) -> None:
    conn.execute(
        text(
            "DELETE FROM dedup_candidates c WHERE NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.transaction_id = c.transaction_a)"
            " OR NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.transaction_id = c.transaction_b)"
        )
    )
    conn.execute(
        text("DELETE FROM transactions t WHERE NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.transaction_id = t.id)")
    )
