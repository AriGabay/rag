"""Storing measurements without overriding people.

A re-extraction replaces the unreviewed measurements of what it read (the whole version, or, after a partial
run, only the passages that were read again). A reviewed measurement (verified, corrected or rejected) is never
touched. A new value is matched to a reviewed one by a stable identity — the same table cell, or a quote that
shares most of its wording — not by block numbers, which change when a document is read again:

- the same number: the new value is dropped and the review stands (a rejected value does not come back);
- another number for the same kind of metric: the new value is stored for review and the disagreement is added to
  the reviewed measurement's ``conflict`` list, so the review screen shows it instead of silently replacing a
  person's decision.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from app.measurements.extract import Row

REVIEWED = ("verified", "corrected", "rejected")
QUOTE_OVERLAP = 0.5


def _numbers(value, low, high) -> set[Decimal]:
    return {Decimal(str(x)) for x in (value, low, high) if x is not None}


def _words(quote: str) -> set[str]:
    return set(re.findall(r"[\u05D0-\u05EA]{2,}|\d[\d,.]*", (quote or "").replace("״", '"')))


def _same_statement(row: Row, m) -> bool:
    if row.table_index is not None or m.table_index is not None:
        return row.statement_key == m.statement_key
    a, b = _words(row.quote), _words(m.quote)
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= QUOTE_OVERLAP


def _kinds(m) -> set[str]:
    """The reviewed measurement's kind now and as first extracted (a correction may have changed it)."""
    kinds = {m.metric_kind}
    for prev in m.previous or []:
        if prev.get("metric_kind"):
            kinds.add(prev["metric_kind"])
    return kinds


def store_rows(conn: Connection, document_id: UUID, version_id: UUID, rows: list[Row], extraction_version: str,
               model: str | None, read: tuple[set, set] | None = None) -> int:
    if read is None:
        conn.execute(text("DELETE FROM measurements WHERE version_id = :v AND NOT (status = ANY(:r))"),
                     {"v": version_id, "r": list(REVIEWED)})
    else:
        blocks, tables = read
        conn.execute(text(
            "DELETE FROM measurements WHERE version_id = :v AND NOT (status = ANY(:r)) AND ((table_index IS NULL AND"
            " block_index = ANY(:b)) OR table_index = ANY(:t))"),
            {"v": version_id, "r": list(REVIEWED), "b": [b for b in blocks if b is not None],
             "t": [t for t in tables if t is not None]})
    reviewed = conn.execute(text(
        "SELECT id, statement_key, metric_kind, value, value_low, value_high, table_index, quote, previous, status"
        " FROM measurements WHERE version_id = :v AND status = ANY(:r)"), {"v": version_id, "r": list(REVIEWED)}).all()
    kept = conn.execute(text("SELECT statement_key, metric_kind, value, value_low, value_high, metric FROM measurements"
                             " WHERE version_id = :v AND NOT (status = ANY(:r))"),
                        {"v": version_id, "r": list(REVIEWED)}).all()
    seen: set[tuple] = {(k.statement_key, k.metric_kind, k.value, k.value_low, k.value_high, k.metric) for k in kept}
    stored = 0
    for r in rows:
        key = (r.statement_key, r.metric_kind, r.value, r.low, r.high, r.metric)
        if key in seen:
            continue
        seen.add(key)
        same = [m for m in reviewed if _same_statement(r, m)]
        numbers = _numbers(r.value, r.low, r.high)
        if any(_numbers(m.value, m.value_low, m.value_high) == numbers for m in same):
            continue  # a person already decided on this value
        disagreeing = [m for m in same if r.metric_kind in _kinds(m)]
        issues = list(r.issues)
        if disagreeing:
            issues.append("חילוץ חדש סותר החלטת סוקר קודמת")
        new_id = conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
            " statement_key, metric, metric_kind, value, value_low, value_high, value_form, value_text, unit, period,"
            " area_basis, vat, subject, subject_role, value_role, effective_date, quote, section, extraction_version,"
            " model, status, issues) VALUES (app_office(), :d, :v, :bi, :ti, :ri, :sk, :m, :mk, :val, :lo, :hi, :f,"
            " :vt, :u, :p, :ab, :vat, :s, :sr, :vr, :ed, :q, :sec, :e, :model, :st, CAST(:iss AS jsonb)) RETURNING id"),
            {"d": document_id, "v": version_id, "bi": r.block_index, "ti": r.table_index, "ri": r.row_index,
             "sk": r.statement_key, "m": r.metric[:300], "mk": r.metric_kind, "val": r.value, "lo": r.low,
             "hi": r.high, "f": r.form, "vt": r.value_text[:200], "u": r.unit, "p": r.period, "ab": r.area_basis,
             "vat": r.vat, "s": r.subject, "sr": r.subject_role, "vr": r.value_role, "ed": r.effective_date,
             "q": r.quote[:2000], "sec": r.section, "e": extraction_version, "model": model,
             "st": "needs_review" if issues else "auto_validated",
             "iss": json.dumps(issues, ensure_ascii=False)}).scalar_one()
        entry = json.dumps([{"measurement_id": str(new_id), "value_text": r.value_text,
                             "extraction_version": extraction_version}], ensure_ascii=False)
        for m in disagreeing:
            conn.execute(text(
                "UPDATE measurements SET conflict = COALESCE(conflict, '[]'::jsonb) || CAST(:c AS jsonb),"
                " updated_at = now() WHERE id = :id"), {"id": m.id, "c": entry})
        stored += 1
    return stored
