"""Per-version metadata for search filters (KTD10, R9).

A version's city and neighborhood come from the report header: occurrences whose city/neighborhood
fact was read from a header label or inherited from it, and the subject's appraised-value record. Only
when the header is silent do the version's other current occurrences count. Dates come from the
occurrences' date columns. Rejected occurrences never count, and reviewer corrections (stored on the
occurrence) win over the extracted value. A version with no records at all (a narrative report) falls
back to its own "label: value" header lines on the first pages. A missing value is "unknown": it never
matches a filter and is reported separately, so the caller can say how many documents could not be checked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from sqlalchemy import Connection, text

from app.extraction.normalize_text import base_normalize

HEADER_PAGES = 2  # report headers sit on the first pages

DATE_FIELDS = ("valuation_date", "report_date", "transaction_date")
_HEADER_FACT = (
    "EXISTS (SELECT 1 FROM fact_values f WHERE f.occurrence_id = o.id AND f.field = '{field}'"
    " AND (f.source_path ? 'label' OR f.source_path->>'inherited_from' = 'report_header'))"
)


@dataclass(frozen=True)
class MetadataFilters:
    city: str | None = None
    neighborhood: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    date_field: str = "valuation_date"

    def __post_init__(self) -> None:
        if self.date_field not in DATE_FIELDS:
            raise ValueError(f"unsupported date field {self.date_field!r}")

    @property
    def active(self) -> bool:
        return bool(self.city or self.neighborhood or self.year_from is not None or self.year_to is not None)


@dataclass
class VersionMetadata:
    version_id: UUID
    document_id: UUID
    cities: frozenset[str] = frozenset()
    neighborhoods: frozenset[str] = frozenset()
    dates: dict[str, frozenset[date]] = field(default_factory=dict)


@dataclass
class FilterReport:
    """``matched`` pass every filter; ``excluded`` contradict one; ``unknown`` maps a filter field to
    the versions that state no value for it (and contradict nothing)."""

    matched: list[UUID] = field(default_factory=list)
    excluded: list[UUID] = field(default_factory=list)
    unknown: dict[str, list[UUID]] = field(default_factory=dict)

    @property
    def unknown_count(self) -> int:
        return len({v for ids in self.unknown.values() for v in ids})


def _place(value: str | None) -> str | None:
    if not value:
        return None
    return base_normalize(value).strip(" .,:;") or None


def version_metadata(conn: Connection, version_ids: Sequence[UUID]) -> dict[UUID, VersionMetadata]:
    """Metadata of the given versions the caller can see (RLS); every visible version gets an entry."""
    if not version_ids:
        return {}
    ids = list(version_ids)
    meta = {r.id: VersionMetadata(r.id, r.document_id) for r in conn.execute(
        text("SELECT v.id, v.document_id FROM document_versions v"
             " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL WHERE v.id = ANY(:ids)"),
        {"ids": ids},
    ).all()}
    rows = conn.execute(
        text("SELECT o.version_id, o.data_kind, o.city, o.neighborhood, o.valuation_date, o.report_date,"
             " o.transaction_date, " + _HEADER_FACT.format(field="city") + " AS city_hdr, "
             + _HEADER_FACT.format(field="neighborhood") + " AS hood_hdr"
             " FROM occurrences o WHERE o.version_id = ANY(:ids) AND o.verification_status <> 'rejected'"),
        {"ids": list(meta)},
    ).all()
    acc: dict[UUID, dict[str, set]] = {v: {"city_h": set(), "city": set(), "hood_h": set(), "hood": set(),
                                           **{f: set() for f in DATE_FIELDS}} for v in meta}
    for r in rows:
        a = acc[r.version_id]
        subject = r.data_kind == "appraised_value"
        if (city := _place(r.city)) is not None:
            a["city_h" if (r.city_hdr or subject) else "city"].add(city)
        if (hood := _place(r.neighborhood)) is not None:
            a["hood_h" if (r.hood_hdr or subject) else "hood"].add(hood)
        for f in DATE_FIELDS:
            if getattr(r, f) is not None:
                a[f].add(getattr(r, f))
    silent = [v for v, a in acc.items() if not (a["city_h"] or a["city"] or a["hood_h"] or a["hood"]
                                                   or any(a[f] for f in DATE_FIELDS))]
    for vid, header in _page_headers(conn, silent).items():
        a = acc[vid]
        if (city := _place(header.get("city"))) is not None:
            a["city_h"].add(city)
        if (hood := _place(header.get("neighborhood"))) is not None:
            a["hood_h"].add(hood)
        for f in ("valuation_date", "report_date"):
            if isinstance(header.get(f), date):
                a[f].add(header[f])
    for vid, m in meta.items():
        a = acc[vid]
        m.cities = frozenset(a["city_h"] or a["city"])
        m.neighborhoods = frozenset(a["hood_h"] or a["hood"])
        m.dates = {f: frozenset(a[f]) for f in DATE_FIELDS}
    return meta


def _page_headers(conn: Connection, version_ids: Sequence[UUID]) -> dict[UUID, dict]:
    """Header fields ("עיר:", "שכונה:", "המועד הקובע:" ...) read from each version's first pages."""
    from app.appraisal.extract import parse_header  # appraisal layer; imported lazily to keep search light

    if not version_ids:
        return {}
    pages: dict[UUID, list[tuple[int, str]]] = {}
    for r in conn.execute(
        text("SELECT version_id, page_no, text FROM pages WHERE version_id = ANY(:ids) AND page_no <= :n"
             " ORDER BY version_id, page_no"),
        {"ids": list(version_ids), "n": HEADER_PAGES},
    ).all():
        pages.setdefault(r.version_id, []).append((r.page_no, r.text))
    return {vid: {k: fv.value for k, fv in parse_header(p).fields.items()} for vid, p in pages.items()}


def report_header(conn: Connection, version_id: UUID) -> dict:
    """The "label: value" header fields of one version's first pages (block_parcel is a tuple)."""
    return _page_headers(conn, [version_id]).get(version_id) or {}


def header_places(conn: Connection) -> set[tuple[str | None, str]]:
    """(city, neighborhood) pairs and (None, city) entries named by the headers of current, visible
    narrative reports (versions without records), so the gazetteer knows places those reports cover."""
    ids = [r.id for r in conn.execute(text(
        "SELECT v.id FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
        " WHERE v.is_current AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.version_id = v.id)")).all()]
    places: set[tuple[str | None, str]] = set()
    for header in _page_headers(conn, ids).values():
        city, hood = header.get("city"), header.get("neighborhood")
        if isinstance(city, str) and city.strip():
            places.add((None, city.strip()))
        if isinstance(hood, str) and hood.strip():
            places.add((city.strip() if isinstance(city, str) and city.strip() else None, hood.strip()))
    return places


def place_matches(wanted: str, values: frozenset[str]) -> bool:
    """Whether a named place is one of a version's places (public form, used by entity resolution)."""
    return _place_matches(wanted, values)


def _place_matches(wanted: str, values: frozenset[str]) -> bool:
    """Equal after normalization, or one names the other with a suffix ('תל אביב' ~ 'תל אביב-יפו')."""
    w = _place(wanted) or ""
    for v in values:
        if v == w or v.startswith((w + " ", w + "-")) or w.startswith((v + " ", v + "-")):
            return True
    return False


def apply_filters(metadata: dict[UUID, VersionMetadata], filters: MetadataFilters) -> FilterReport:
    report = FilterReport()
    for vid, m in metadata.items():
        excluded = False
        unknown: list[str] = []
        checks: list[tuple[str, bool | None]] = []
        if filters.city:
            checks.append(("city", _place_matches(filters.city, m.cities) if m.cities else None))
        if filters.neighborhood:
            checks.append(("neighborhood",
                           _place_matches(filters.neighborhood, m.neighborhoods) if m.neighborhoods else None))
        if filters.year_from is not None or filters.year_to is not None:
            dates = m.dates.get(filters.date_field) or frozenset()
            lo = filters.year_from if filters.year_from is not None else 1
            hi = filters.year_to if filters.year_to is not None else 9999
            checks.append((filters.date_field, any(lo <= d.year <= hi for d in dates) if dates else None))
        for name, ok in checks:
            if ok is None:
                unknown.append(name)
            elif not ok:
                excluded = True
        if excluded:
            report.excluded.append(vid)
        elif unknown:
            for name in unknown:
                report.unknown.setdefault(name, []).append(vid)
        else:
            report.matched.append(vid)
    return report

