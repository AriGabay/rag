"""Storing measurements without overriding people.

A re-extraction replaces the unreviewed measurements of what it read (the whole version, or, after a partial
run, only the passages that were read again). A reviewed measurement (verified, corrected or rejected) is never
touched. A new value is matched to a reviewed one by a stable identity — the same table cell, or a quote that
shares most of its wording — not by block numbers, which change when a document is read again:

- the same number: the new value is dropped and the review stands (a rejected value does not come back);
- another number for the same kind of metric: the new value is stored for review and the disagreement is added to
  the reviewed measurement's ``conflict`` list, so the review screen shows it instead of silently replacing a
  person's decision.

Reprocessing (KTD7) replaces a version's reading, so block, table and row numbers change. In the same transaction
every measurement is re-anchored in the new reading (``reanchor_measurements``): a passage value by its normalized
quote with its value inside it, a table value by its table's caption and headers and its row label with the value in
the row — never by index. The stored quote first gets what the new reader fixed: a bold word an older reader took
twice is read once, and a letter of a broken font map may be any letter the version's accepted corrections map it
to. A reviewed measurement that is not found again keeps its decision, loses its positions and records where it
was (``anchor_lost``); an unreviewed one is dropped (re-extraction reads the new reading again).
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Connection, text

from app.extraction.normalize_text import undouble_word
from app.measurements.extract import Row, _cell_key, _norm, load_passages

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


# --- re-anchoring after reprocessing (KTD7) ------------------------------------------------------------------------

def measurement_anchors(conn: Connection, version_id: UUID) -> dict[UUID, dict]:
    """Where each measurement of the current reading sits, read before the reading is replaced: its positions and
    the reading's id, and for a table value its table's caption, headers and row. A measurement already lost keeps
    the place it was last anchored."""
    reading = conn.execute(text("SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v"),
                           {"v": version_id}).scalar_one_or_none()
    tables = {r.table_index: r.structure or {} for r in conn.execute(text(
        "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v"), {"v": version_id})}
    out: dict[UUID, dict] = {}
    for m in conn.execute(text("SELECT id, block_index, table_index, row_index, statement_key, anchor_lost"
                               " FROM measurements WHERE version_id = :v"), {"v": version_id}):
        if m.anchor_lost is not None:
            out[m.id] = m.anchor_lost
            continue
        table = None
        if m.table_index is not None and m.table_index in tables:
            st = tables[m.table_index]
            rows = st.get("rows") or []
            cells = (rows[m.row_index].get("cells") or []) if m.row_index is not None and \
                0 <= m.row_index < len(rows) else None
            table = {"caption": st.get("caption"), "headers": st.get("headers") or [], "row": cells}
        out[m.id] = {"reading_id": reading, "block_index": m.block_index, "table_index": m.table_index,
                     "row_index": m.row_index, "statement_key": m.statement_key, "table": table}
    return out


def _letter_classes(corrections: list[dict]) -> dict[str, str]:
    """For each character a font-map correction replaced, every letter it may stand for in this version (two fonts
    may map one character to different letters) and the character itself (a Latin word keeps it)."""
    out: dict[str, set[str]] = {}
    for c in corrections:
        if c.get("from") and c.get("to"):
            out.setdefault(c["from"], {c["from"]}).update(x for x in (c["to"], c.get("to_final")) if x)
    return {k: "".join(sorted(v)) for k, v in out.items()}


def _pattern(s: str, classes: dict[str, str]) -> re.Pattern:
    return re.compile("".join(f"[{re.escape(classes[ch])}]" if ch in classes else re.escape(ch) for ch in s))


def _variants(s: str) -> list[str]:
    """A stored text normalized, and read once where an older reader took a bold word twice."""
    norm = _norm(s or "")
    return list(dict.fromkeys([norm, " ".join(undouble_word(w) for w in norm.split())]))


def _states_value(quote: str, values: list[str], classes: dict[str, str]) -> bool:
    return any(_pattern(v, classes).search(quote) for value in values for v in _variants(value) if v)


def _same_cell(old: str | None, new: str | None, classes: dict[str, str]) -> bool:
    return any(_pattern(_cell_key(v), classes).fullmatch(_cell_key(_norm(new or ""))) for v in _variants(old or ""))


def _find_row(tables: list, old: dict, values: list[str], classes: dict[str, str]):
    """The new table and row with the old table's caption and headers, the old row's label and the value."""
    row_cells = old.get("row") or []
    if not row_cells:
        return None
    for table_index, st in tables:
        if not _same_cell(old.get("caption") or "", st.get("caption") or "", classes):
            continue
        headers = st.get("headers") or []
        if len(headers) != len(old.get("headers") or []) or not all(
                _same_cell(a, b, classes) for a, b in zip(old.get("headers") or [], headers, strict=True)):
            continue
        for row_index, r in enumerate(st.get("rows") or []):
            cells = r.get("cells") or []
            if cells and _same_cell(row_cells[0], cells[0], classes) and any(
                    _same_cell(v, c, classes) for v in values for c in cells):
                return table_index, row_index, st, cells
    return None


def _row_quote(old_quote: str, old_cells: list[str], headers: list[str], cells: list[str]) -> str:
    """The new row in the form its measurement was quoted in (``expand_table``): its cells, or each cell after its
    column header."""
    if _norm(old_quote) == _norm(" | ".join(c for c in old_cells if c)):
        return " | ".join(c for c in cells if c)
    return " | ".join(f"{headers[i] if i < len(headers) and headers[i] else ''}: {c}".strip(": ")
                      for i, c in enumerate(cells) if c)


def _find_quote(passages: list, blocks: list, quote: str, values: list[str], classes: dict[str, str],
                exact: bool) -> tuple[int | None, str] | None:
    """The passage of the new reading that holds the quote (as the text of record now reads it) with the value
    in it: the block where the passage starts, as extraction anchors a passage value, or with ``exact`` the block
    that holds the quote when one does."""
    for variant in _variants(quote):
        if not variant:
            continue
        pattern = _pattern(variant, classes)
        for block_index, body in passages:
            hit = pattern.search(body)
            if hit is None or not _states_value(hit.group(0), values, classes):
                continue
            if exact:
                block_index = next((b for b, t in blocks if pattern.search(t)), block_index)
            return block_index, hit.group(0)
    return None


def reanchor_measurements(conn: Connection, version_id: UUID, anchors: dict[UUID, dict],
                          corrections: list[dict]) -> dict[str, int]:
    """Anchor the version's measurements in its new reading (written in this transaction). ``anchors``: where
    each sat in the replaced reading (``measurement_anchors``); ``corrections``: the version's accepted font-map
    corrections from the new ingestion report. Returns how many were anchored, lost (reviewed) and dropped."""
    classes = _letter_classes(corrections)
    passages = [(p.block_index, _norm(p.text)) for p in load_passages(conn, version_id) if p.kind == "text"]
    blocks = [(r.block_index, _norm(r.text)) for r in conn.execute(text(
        "SELECT block_index, text FROM document_blocks WHERE version_id = :v AND text <> '' ORDER BY block_index"),
        {"v": version_id})]
    tables = [(r.table_index, r.structure or {}) for r in conn.execute(text(
        "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v ORDER BY table_index"),
        {"v": version_id})]
    counts = {"anchored": 0, "lost": 0, "dropped": 0}
    for m in conn.execute(text("SELECT id, status, quote, value_text, previous, anchor_lost FROM measurements"
                               " WHERE version_id = :v"), {"v": version_id}).all():
        anchor = anchors.get(m.id) or {}
        reviewed = m.status in REVIEWED
        values = [m.value_text] + [p["value_text"] for p in m.previous or [] if p.get("value_text")]
        update = None
        if anchor.get("table_index") is not None:
            old = anchor.get("table")
            found = _find_row(tables, old, values, classes) if old else None
            if found is not None:
                ti, ri, st, cells = found
                suffix = (anchor.get("statement_key") or "").split(":", 2)[-1]
                update = {"bi": st.get("block_index"), "ti": ti, "ri": ri, "sk": f"t{ti}:r{ri}:{suffix}",
                          "q": _row_quote(m.quote, old.get("row") or [], st.get("headers") or [], cells)}
        else:
            found = _find_quote(passages, blocks, m.quote, values, classes, exact=reviewed)
            if found is not None:
                bi, quote = found
                update = {"bi": bi, "ti": None, "ri": None, "q": quote,
                          "sk": f"b{bi}:" + hashlib.sha1(quote.encode()).hexdigest()[:10]}
        if update is not None:
            conn.execute(text(
                "UPDATE measurements SET block_index = :bi, table_index = :ti, row_index = :ri, statement_key = :sk,"
                " quote = :q, anchor_lost = NULL, updated_at = now() WHERE id = :id"), update | {"id": m.id})
            counts["anchored"] += 1
        elif reviewed:
            conn.execute(text(
                "UPDATE measurements SET block_index = NULL, table_index = NULL, row_index = NULL,"
                " statement_key = 'lost:' || :sk, anchor_lost = CAST(:a AS jsonb), updated_at = now() WHERE id = :id"),
                {"id": m.id, "sk": anchor.get("statement_key") or "", "a": json.dumps(anchor, ensure_ascii=False)})
            counts["lost"] += 1
        else:
            conn.execute(text("DELETE FROM measurements WHERE id = :id"), {"id": m.id})
            counts["dropped"] += 1
    return counts
