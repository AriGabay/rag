"""Expected answers computed independently from ``tests/fixtures/ground_truth.yaml`` (U15).

Nothing here imports application code: the numbers are recomputed from the answer key with
``Decimal`` and ROUND_HALF_UP to 0.01, so the acceptance suite and ``scripts/eval.py`` compare the
system against an independent oracle.

Semantics mirrored from the product contract (not from the implementation):

* Only current versions count (D1v2 replaces D1), only records a human verified count.
* A certain duplicate (same office, block/parcel/sub-parcel or address, data kind, transaction date,
  price, area and area type) is one transaction; a transaction matches when any of its visible,
  verified occurrences matches every condition.
* mean = mean of per-record price/sqm (each rounded to 0.01); weighted = sum(price) / sum(area);
  median = middle value (mean of the two middle values when even), shown only for >= 3 records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
CENT = Decimal("0.01")
DEDUP_KINDS = ("transaction_price", "asking_price", "adjusted_comparable")
MIXED_KEYS = ("area_type", "property_type", "vat_basis")  # the order the product asks about them
SEED_CHECK_FIELDS = ("price", "area", "area_type", "transaction_date", "valuation_date")

# demo users (scripts/seed_demo.py): office and visible document groups (None = admin, all groups).
# G3 is the held-out group "ידע כללי" (ground_truth.yaml general_facts.group): dana sees it, yossi does not.
USERS = {
    "admin-a@demo.test": ("A", None),
    "dana@demo.test": ("A", frozenset({"G1", "G3"})),
    "yossi@demo.test": ("A", frozenset({"G2"})),
    "admin-b@demo.test": ("B", None),
}

RECORD_FIELDS = (
    "data_kind", "city", "neighborhood", "address", "block", "parcel", "sub_parcel", "property_type", "rooms",
    "transaction_date", "valuation_date", "report_date", "area", "area_type", "price", "vat_basis",
    "price_per_sqm_stated",
)
CRITICAL_FIELDS = {
    "transaction_price": ("data_kind", "city", "price", "area", "area_type", "transaction_date"),
    "appraised_value": ("data_kind", "city", "price", "area", "area_type", "valuation_date"),
}


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@cache
def truth() -> dict:
    return yaml.safe_load((FIXTURES / "ground_truth.yaml").read_text(encoding="utf-8"))


@cache
def docs() -> dict[str, dict]:
    return {d["id"]: d for d in truth()["documents"]}


@cache
def by_filename() -> dict[str, dict]:
    return {d["filename"]: d for d in truth()["documents"]}


def replaced_docs() -> set[str]:
    return {d["version_of"] for d in truth()["documents"] if d.get("version_of")}


def title_of(doc_id: str) -> str:
    """Document title the upload API derives from the original file name."""
    d = docs()[doc_id]
    if d.get("version_of"):
        d = docs()[d["version_of"]]
    return Path(d["filename"]).stem.replace("_", " ")


@cache
def all_records() -> tuple[dict, ...]:
    out = []
    for d in truth()["documents"]:
        group = docs()[d["version_of"]]["group"] if d.get("version_of") else d["group"]
        for r in d.get("records") or []:
            out.append(r | {"_doc": d["id"], "_office": d["office"], "_group": group, "_filename": d["filename"]})
    return tuple(out)


@cache
def records_by_id() -> dict[str, dict]:
    return {r["id"]: r for r in all_records()}


def current_records(office: str, overrides: dict[str, dict] | None = None,
                    dropped_docs: frozenset | set = frozenset(), not_current: set | None = None) -> list[dict]:
    """Records of current versions. ``overrides`` patches fields of a record (a human correction);
    ``dropped_docs`` removes deleted documents; ``not_current`` replaces the default set of answer-key
    documents that are not current (D1, replaced by D1v2) — e.g. ``{"D1v2"}`` before D1v2 is uploaded."""
    gone = (replaced_docs() if not_current is None else set(not_current)) | set(dropped_docs)
    out = [r for r in all_records() if r["_office"] == office and r["_doc"] not in gone]
    if overrides:
        out = [r | overrides.get(r["id"], {}) for r in out]
    return out


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _date(value) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def ppsm(r: dict) -> Decimal | None:
    price, area = _dec(r["price"]), _dec(r["area"])
    if price is None or area is None or area <= 0:
        return None
    return q2(price / area)


def txn_key(r: dict) -> tuple:
    """Certain-duplicate identity (plan assumption); anything weaker stays a separate record."""
    if r["data_kind"] not in DEDUP_KINDS:
        return ("record", r["id"])
    if r.get("block") and r.get("parcel"):
        loc = f"bp:{r['block']}/{r['parcel']}/{r.get('sub_parcel') or '-'}"
    elif r.get("address"):
        loc = f"addr:{r['address']}"
    else:
        return ("record", r["id"])
    if None in (r["transaction_date"], r["price"], r["area"], r["area_type"]):
        return ("record", r["id"])
    return (r["_office"], loc, r["data_kind"], str(r["transaction_date"]), _dec(r["price"]).normalize(),
            _dec(r["area"]).normalize(), r["area_type"])


@dataclass(frozen=True)
class Filters:
    data_kind: str
    date_field: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    city: str | None = None
    neighborhood: str | None = None
    property_type: str | None = None
    area_type: str | None = None
    vat_basis: str | None = None

    @classmethod
    def of(cls, spec: dict) -> Filters:
        names = {f.name for f in fields(cls)}
        unknown = set(spec) - names
        if unknown:
            raise ValueError(f"unknown filter keys: {sorted(unknown)}")
        return cls(**spec)

    def with_(self, **kw) -> Filters:
        return Filters(**({f.name: getattr(self, f.name) for f in fields(self)} | kw))


def matches(r: dict, f: Filters) -> bool:
    if r["data_kind"] != f.data_kind:
        return False
    for key in ("city", "neighborhood", "property_type", "area_type", "vat_basis"):
        want = getattr(f, key)
        if want is not None and r.get(key) != want:
            return False
    if f.year_from is not None:
        if f.date_field is None:
            raise ValueError("a year filter needs a date field")
        value = _date(r.get(f.date_field))
        if value is None or not (f.year_from <= value.year <= (f.year_to or f.year_from)):
            return False
    return True


@dataclass
class Expected:
    count: int
    mean: Decimal | None
    weighted: Decimal | None
    median: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    record_ids: list[str] = field(default_factory=list)  # every matching occurrence (all duplicates)
    transactions: dict[tuple, list[str]] = field(default_factory=dict)

    def as_answer(self) -> dict:
        s = lambda v: None if v is None else str(v)  # noqa: E731
        return {"record_count": self.count, "mean_price_per_sqm": s(self.mean),
                "weighted_price_per_sqm": s(self.weighted), "median_price_per_sqm": s(self.median),
                "min_price_per_sqm": s(self.minimum), "max_price_per_sqm": s(self.maximum)}


def eligible(f: Filters, office: str = "A", groups: frozenset | set | None = None,
             exclude: set[str] | frozenset = frozenset(), **world) -> list[dict]:
    return [r for r in current_records(office, **world)
            if (groups is None or r["_group"] in groups) and r["id"] not in exclude and matches(r, f)]


def expected_stats(f: Filters, office: str = "A", groups=None, exclude=frozenset(), **world) -> Expected:
    recs = [r for r in eligible(f, office, groups, exclude, **world) if ppsm(r) is not None]
    txns: dict[tuple, list[str]] = {}
    first: dict[tuple, dict] = {}
    for r in recs:
        key = txn_key(r)
        txns.setdefault(key, []).append(r["id"])
        first.setdefault(key, r)
    values = sorted(ppsm(r) for r in first.values())
    n = len(values)
    if n == 0:
        return Expected(0, None, None, None, None, None, [], {})
    prices = sum(_dec(r["price"]) for r in first.values())
    areas = sum(_dec(r["area"]) for r in first.values())
    mid = n // 2
    median = values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2
    return Expected(
        count=n, mean=q2(sum(values) / n), weighted=q2(prices / areas),
        median=q2(median) if n >= 3 else None, minimum=values[0], maximum=values[-1],
        record_ids=[r["id"] for r in recs], transactions=txns,
    )


def mixed_keys(f: Filters, office: str = "A", groups=None, exclude=frozenset(), **world) -> list[str]:
    """Result-changing attributes the matching verified records still mix (product must clarify them)."""
    recs = [r for r in eligible(f, office, groups, exclude, **world) if ppsm(r) is not None]
    out = []
    for key in MIXED_KEYS:
        if getattr(f, key) is None and len({r.get(key) for r in recs if r.get(key) is not None}) > 1:
            out.append(key)
    return out


def distinct_values(f: Filters, key: str, office: str = "A", groups=None, exclude=frozenset(), **world) -> set[str]:
    recs = [r for r in eligible(f, office, groups, exclude, **world) if ppsm(r) is not None]
    return {r.get(key) for r in recs if r.get(key) is not None}


def expected_clarifications(f: Filters, stated: tuple[str, ...] = (), office: str = "A", groups=None,
                            exclude=frozenset(), **world) -> list[str]:
    """Mixed-attribute clarifications, in order, that a user answering with ``f``'s values goes through.

    ``stated`` names the area/property/VAT conditions the question itself already gives; the others
    start open and are added back as they are asked. A key ``f`` leaves open ends the list (the
    answer then stays a clarification)."""
    current = f.with_(**{k: None for k in MIXED_KEYS if k not in stated})
    asked = []
    while True:
        keys = mixed_keys(current, office, groups, exclude, **world)
        if not keys:
            return asked
        key = keys[0]
        asked.append(key)
        if getattr(f, key) is None:
            return asked
        current = current.with_(**{key: getattr(f, key)})


def largest_partition(f: Filters, office: str = "A", groups=None, exclude=frozenset(), **world) -> Filters:
    """Fill every still-mixed attribute with the value that keeps the most records (authoring aid)."""
    current = f
    while True:
        keys = mixed_keys(current, office, groups, exclude, **world)
        if not keys:
            return current
        key = keys[0]
        best = max(sorted(distinct_values(current, key, office, groups, exclude, **world)),
                   key=lambda v: expected_stats(current.with_(**{key: v}), office, groups, exclude, **world).count)
        current = current.with_(**{key: best})


# --- mapping system records back to the answer key -------------------------------------------------

def gt_record_for(filename: str, table_index: int | None, row_index: int | None, data_kind: str) -> dict | None:
    d = by_filename().get(filename)
    if d is None:
        return None
    for r in d.get("records") or []:
        if table_index is None and row_index is None:
            if r["table_index"] is None and r["data_kind"] == data_kind:
                return r | {"_doc": d["id"]}
        elif r["table_index"] == table_index and r["row_index"] == row_index:
            return r | {"_doc": d["id"]}
    return None


def unverified_from_queue(client, office: str | None = None) -> tuple[set[str], list[str]]:
    """Answer-key ids of records the office's review queue still lists (not human-verified).

    ``client`` is any logged-in httpx-compatible client (TestClient or httpx.Client) of an admin of
    ``office``. Files that belong to another office in the answer key (for example a copy of an office A
    fixture uploaded in office B by a browser test) are ignored."""
    items = client.get("/api/review/queue").json()["items"]
    filenames: dict[str, dict[str, str]] = {}
    ids, unmapped = set(), []
    for item in items:
        if item["kind"] != "record":
            continue
        detail = client.get(f"/api/review/records/{item['id']}").json()
        doc_id = detail["document"]["id"]
        if doc_id not in filenames:
            versions = client.get(f"/api/documents/{doc_id}").json().get("versions", [])
            filenames[doc_id] = {v["id"]: v["filename"] for v in versions}
        filename = filenames[doc_id].get(detail["version_id"])
        kind = item["summary"]["data_kind"]
        rec = gt_record_for(filename or "", detail["table_index"], detail["row_index"], kind)
        if rec is not None and office is not None and docs()[rec["_doc"]]["office"] != office:
            continue
        if rec is None:
            unmapped.append(f"{filename} table={detail['table_index']} row={detail['row_index']} {kind}")
        else:
            ids.add(rec["id"])
    return ids, unmapped


def price_token(rec_id: str) -> str:
    """The price exactly as the document prints it (digits and separators only)."""
    r = records_by_id()[rec_id]
    d = docs()[r["_doc"]]
    if r["table_index"] is not None:
        cell = d["tables"][r["table_index"]]["rows"][r["row_index"]]["cells"][7]
        return re.search(r"\d[\d,.]*", cell).group(0)
    return f"{int(Decimal(r['price'])):,}"


# --- extraction quality (tables and fields), separate from answer quality --------------------------

def _norm_cell(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_value(name: str, value):
    if value is None or value == "":
        return None
    if name in ("price", "area", "rooms", "price_per_sqm_stated"):
        try:
            return Decimal(str(value)).normalize()
        except Exception:  # noqa: BLE001
            return str(value)
    if name in ("transaction_date", "valuation_date", "report_date"):
        return str(value)[:10]
    return str(value).strip()


def table_accuracy(tables: list[dict], offices: tuple[str, ...] = ("A", "B")) -> dict:
    """``tables``: rows of {filename, table_index, structure} for current versions.

    Header accuracy = matching header cells / expected header cells; cell accuracy = matching body
    cells / expected body cells (a missing row counts all its cells wrong); row-page accuracy = rows
    whose physical page is right."""
    got = {(t["filename"], t["table_index"]): t["structure"] for t in tables}
    h_ok = h_all = c_ok = c_all = p_ok = p_all = 0
    per_doc = {}
    gone = replaced_docs()
    present_offices = {office for office in offices}
    for d in truth()["documents"]:
        if d["id"] in gone or d["office"] not in present_offices:
            continue
        for t in d.get("tables") or []:
            s = got.get((d["filename"], t["index"]))
            heads = [_norm_cell(h) for h in (s or {}).get("headers", [])]
            rows = (s or {}).get("rows", [])
            dh = sum(1 for i, h in enumerate(t["headers"]) if i < len(heads) and heads[i] == _norm_cell(h))
            dc = dp = 0
            for er in t["rows"]:
                gr = rows[er["row_index"]] if er["row_index"] < len(rows) else None
                cells = [_norm_cell(c) for c in (gr or {}).get("cells", [])]
                dc += sum(1 for i, c in enumerate(er["cells"]) if i < len(cells) and cells[i] == _norm_cell(c))
                dp += int(gr is not None and gr.get("page") == er["page"])
            ncells = sum(len(er["cells"]) for er in t["rows"])
            h_ok, h_all = h_ok + dh, h_all + len(t["headers"])
            c_ok, c_all = c_ok + dc, c_all + ncells
            p_ok, p_all = p_ok + dp, p_all + len(t["rows"])
            per_doc[d["id"]] = {"headers": f"{dh}/{len(t['headers'])}", "cells": f"{dc}/{ncells}",
                                "row_pages": f"{dp}/{len(t['rows'])}", "extra_rows": max(0, len(rows) - len(t["rows"]))}
    return {"header_accuracy": h_ok / h_all if h_all else None, "cell_accuracy": c_ok / c_all if c_all else None,
            "row_page_accuracy": p_ok / p_all if p_all else None, "headers": (h_ok, h_all), "cells": (c_ok, c_all),
            "row_pages": (p_ok, p_all), "per_doc": per_doc}


def field_accuracy(occurrences: list[dict], offices: tuple[str, ...] = ("A", "B")) -> dict:
    """``occurrences``: rows of {filename, table_index, row_index, data_kind, <RECORD_FIELDS>} for
    current versions. Compares every answer-key record of current documents with the extracted
    occurrence; a record the system never extracted counts as wrong in every field."""
    by_key = {}
    for o in occurrences:
        rec = gt_record_for(o["filename"], o["table_index"], o["row_index"], o["data_kind"])
        if rec is not None:
            by_key[rec["id"]] = o
    crit_ok = crit_all = all_ok = all_all = 0
    missing, wrong = [], []
    expected_ids = [r["id"] for office in offices for r in current_records(office)]
    for rid in expected_ids:
        r = records_by_id()[rid]
        o = by_key.get(rid)
        crit = CRITICAL_FIELDS.get(r["data_kind"], ())
        if o is None:
            missing.append(rid)
            crit_all += len(crit)
            all_all += len(RECORD_FIELDS)
            continue
        for name in RECORD_FIELDS:
            ok = _norm_value(name, o.get(name)) == _norm_value(name, r.get(name))
            all_ok += ok
            all_all += 1
            if name in crit:
                crit_ok += ok
                crit_all += 1
                if not ok:
                    wrong.append(f"{rid}.{name}: got {o.get(name)!r}, expected {r.get(name)!r}")
    return {"critical_field_accuracy": crit_ok / crit_all if crit_all else None,
            "all_field_accuracy": all_ok / all_all if all_all else None, "critical": (crit_ok, crit_all),
            "all": (all_ok, all_all), "records_expected": len(expected_ids), "records_found": len(by_key),
            "missing": missing, "critical_errors": wrong,
            "extra": len(occurrences) - len(by_key)}


EXTRACTION_SQL_TABLES = (
    "SELECT v.filename, t.table_index, t.structure FROM extracted_tables t"
    " JOIN document_versions v ON v.id = t.version_id AND v.is_current"
    " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
)
EXTRACTION_SQL_OCCURRENCES = (
    "SELECT v.filename, o.table_index, o.row_index, " + ", ".join(f"o.{f}" for f in RECORD_FIELDS)
    + " FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
    " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
)
