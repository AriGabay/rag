"""The identity of a property across appraisals (round 11 U14, KTD9; the timeline unit shares it).

A conversation's focus is one unit — one appraisal; the property is every appraisal of it. Two units are of one property
when their stored identifiers (``document_units.identifiers``: the labelled addresses and block/parcel pairs the unit's
title part states, ``contexts.identifiers``) give the same key:

- the block and parcel pairs, normalized (leading zeros dropped), when the unit states any — the registry's name for a
  property, which a street name's spelling cannot change;
- else the street and house number, normalized the way the identifiers store them (the street core without its
  prefixes, the number with its letter);
- else nothing: a unit that states no identifier has no key and matches no other unit, not even another without one.

An address carries no city in the identifiers (an address is read as a street and a number), so two cities' same street
and number share a key when neither unit states a block and parcel; a pair, when stated, separates them.

``same_property_units`` lists the subject units (never an appendix or a comparables part) of the visible appraisals that
share a key, under the caller's permissions (``tenant_tx``) and only in each file's current reading. A file whose units
were not stored yet is read through ``units.ensure``, which stores them."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat import contexts
from app.chat import units as unit_store
from app.platform.source_kinds import APPRAISALS_SQL


def _sets(identifiers: Mapping | None) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(the block/parcel pairs, the street/number pairs) of a unit's stored identifiers. The stored lists win; a unit
    that has only its ``shown`` strings (an older row) is read from them."""
    stored = identifiers or {}
    parcels = {(str(b), str(p)) for b, p in stored.get("parcels") or ()}
    addresses = {(str(s), str(n)) for s, n in stored.get("addresses") or ()}
    if not parcels and not addresses and stored.get("shown"):
        ident = contexts.identifiers("\n".join(str(x) for x in stored["shown"]))
        parcels, addresses = set(ident.parcels), set(ident.addresses)
    return parcels, addresses


def property_key(identifiers: Mapping | None) -> str | None:
    """The key of the property a unit's identifiers name (see the module doc), or None when it states none."""
    parcels, addresses = _sets(identifiers)
    if parcels:
        return "bp:" + ";".join(sorted(f"{b.lstrip('0') or '0'}/{p.lstrip('0') or '0'}" for b, p in parcels))
    if addresses:
        return "addr:" + ";".join(sorted(f"{' '.join(s.split())} {n.strip().lstrip('0') or '0'}" for s, n in addresses))
    return None


def current_appraisal_files(conn: Connection, document_ids: Iterable | None = None) -> list:
    """(``document_id``, ``version_id``) of the current version of every visible appraisal, in document order; only of
    ``document_ids`` when they are given."""
    named = document_ids is not None
    return conn.execute(text(
        "SELECT d.id AS document_id, v.id AS version_id FROM documents d JOIN document_versions v ON v.document_id = d.id"
        f" AND v.is_current WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}" + (" AND d.id = ANY(:d)" if named else "")
        + " ORDER BY d.id"), {"d": list(document_ids)} if named else {}).all()


def subject_units(conn: Connection, files: Sequence) -> list[tuple]:
    """(file, unit) of every subject unit — never an appendix or a comparables part — of the given files, in file and unit
    order. The stored units are read in one query; a file whose units were not stored yet is read through
    ``units.ensure``, which stores them."""
    stored = unit_store.stored_many(conn, [f.version_id for f in files])
    found = []
    for f in files:
        units = stored.get(f.version_id) or unit_store.ensure(conn, f.version_id)
        found += [(f, u) for u in units if u.kind not in contexts.NON_SUBJECT_KINDS]
    return found


def same_property_units(conn: Connection, subject: str | Mapping | None) -> list[tuple[UUID, UUID, int]]:
    """(document id, version id, unit number) of every subject unit of a visible, current appraisal whose property key is
    ``subject`` (a key, or the identifiers it is taken from), in document and unit order. No key: no unit."""
    key = subject if isinstance(subject, str) else property_key(subject)
    if key is None:
        return []
    return [(f.document_id, f.version_id, u.unit_no)
            for f, u in subject_units(conn, current_appraisal_files(conn)) if property_key(u.identifiers) == key]
