"""Attribute registry over the database (U6, KTD7): structured seeding, handle and exact-label matching,
proposed definitions, trigram ranking that never merges, and office isolation."""

import pytest
from sqlalchemy import text

from app.answering.attributes import (
    STRUCTURED_ATTRIBUTES,
    bump_facts_version,
    ensure_structured_attributes,
    handle_map,
    list_attribute_handles,
    rank_candidates,
    resolve_attribute,
)
from app.db import tenant_tx
from tests.factories import make_office, make_user

pytestmark = pytest.mark.db


@pytest.fixture
def offices(db):
    return make_office(db, "משרד א", "a@example.test"), make_office(db, "משרד ב", "b@example.test")


def resolve(office, description=None, handle=None, unit_dimension="area", ctx=None, **kw):
    with tenant_tx(ctx or office.ctx()) as conn:
        return resolve_attribute(conn, handle=handle, description=description, unit_dimension=unit_dimension, **kw)


def handles(office):
    with tenant_tx(office.ctx()) as conn:
        return list_attribute_handles(conn)


def count_defs(office, where="true"):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(f"SELECT count(*) FROM attribute_definitions WHERE {where}")).scalar_one()


def test_ensure_structured_attributes_is_idempotent(offices):
    a, _ = offices
    assert count_defs(a) == 0  # offices created outside seed_demo start without registry entries
    with tenant_tx(a.system) as conn:
        assert ensure_structured_attributes(conn) == 4
    with tenant_tx(a.system) as conn:
        assert ensure_structured_attributes(conn) == 0
    with tenant_tx(a.system) as conn:
        rows = conn.execute(text("SELECT key, source, structured_column, status FROM attribute_definitions"
                                 " ORDER BY key")).all()
    assert [(r.key, r.structured_column) for r in rows] == sorted(
        (x["key"], x["column"]) for x in STRUCTURED_ATTRIBUTES)
    assert {(r.source, r.status) for r in rows} == {("structured", "active")}


def test_handles_list_structured_entries_and_resolve_by_handle(offices):
    a, _ = offices
    hs = handles(a)  # ensures the structured entries on first use
    assert [h["handle"] for h in hs] == ["A1", "A2", "A3", "A4"]
    assert {h["key"] for h in hs} == {"price_per_sqm", "price", "area", "rooms"}
    assert set(hs[0]) >= {"handle", "key", "label", "aliases", "unit_dimension", "canonical_unit", "source", "id"}
    area = next(h for h in hs if h["key"] == "area")
    assert handle_map(hs)[area["handle"]] == area["id"]
    attr = resolve(a, "גודל הדירה במטרים", handle=area["handle"])
    assert (attr.id, attr.key, attr.structured_column, attr.created) == (area["id"], "area", "transactions.area", False)
    assert handles(a) == hs  # numbering is stable across calls


def test_exact_alias_match_reaches_structured_attribute(offices):
    a, _ = offices
    attr = resolve(a, 'מחיר למ"ר', unit_dimension="currency_per_area")
    assert (attr.key, attr.source, attr.created) == ("price_per_sqm", "structured", False)


def test_new_description_creates_one_proposed_definition_then_matches(offices):
    a, _ = offices
    created = resolve(a, "גודל ממ״ד")
    assert created.created and created.source == "extracted" and created.structured_column is None
    assert (created.label, created.unit_dimension, created.canonical_unit) == ("גודל ממ״ד", "area", "sqm")
    assert created.facts_version == 1 and created.extraction_prompt_version
    with tenant_tx(a.system) as conn:
        row = conn.execute(text("SELECT status, aliases, value_type FROM attribute_definitions WHERE id = :i"),
                           {"i": created.id}).one()
    assert (row.status, row.aliases, row.value_type) == ("proposed", ["גודל ממ״ד"], "numeric")
    # exact match after article normalization: no new definition
    again = resolve(a, "גודל הממ״ד")
    assert (again.id, again.created) == (created.id, False)
    # a semantic synonym merges only through the interpreter-chosen handle
    handle = next(h["handle"] for h in handles(a) if h["id"] == created.id)
    via_handle = resolve(a, "שטח הממ״ד", handle=handle)
    assert (via_handle.id, via_handle.created) == (created.id, False)
    assert count_defs(a, "source = 'extracted'") == 1


def test_article_variant_matches_and_similar_label_stays_separate(offices):
    a, _ = offices
    mamad = resolve(a, "שטח ממ״ד")
    assert mamad.created
    assert resolve(a, "שטח הממ״ד").id == mamad.id
    storage = resolve(a, "שטח מחסן")
    assert storage.created and storage.id != mamad.id and storage.key != mamad.key
    with tenant_tx(a.ctx()) as conn:
        ranked = rank_candidates(conn, "שטח מחסן", limit=3)
    assert ranked[0]["id"] == storage.id
    near = next(r for r in ranked if r["id"] == mamad.id)  # offered to the interpreter as a candidate only
    assert 0 < near["score"] < 1 and near["handle"].startswith("A")
    assert count_defs(a, "source = 'extracted'") == 2


def test_unknown_handle_falls_back_to_description(offices):
    a, _ = offices
    attr = resolve(a, "שטח מרפסת", handle="A99")
    assert attr.created and attr.label == "שטח מרפסת"


def test_employee_can_resolve_and_bump_facts_version(offices):
    a, _ = offices
    emp = make_user(a, "e@example.test", [a.default_group_id])
    ctx = a.ctx(role="employee", user_id=emp)
    attr = resolve(a, "קומה", unit_dimension="count", ctx=ctx)
    assert attr.created and attr.canonical_unit == "unit"
    with tenant_tx(ctx) as conn:
        assert bump_facts_version(conn, attr.id) == 2
    assert resolve(a, "קומה", unit_dimension="count", ctx=ctx).facts_version == 2


def test_other_office_registry_is_invisible(offices):
    a, b = offices
    b_attr = resolve(b, "שטח ממ״ד")
    b_handles = handles(b)
    a_handles = handles(a)
    assert b_attr.id not in {h["id"] for h in a_handles}
    assert {h["key"] for h in a_handles} == {"price_per_sqm", "price", "area", "rooms"}
    b_handle = next(h["handle"] for h in b_handles if h["id"] == b_attr.id)
    a_attr = resolve(a, "שטח ממ״ד", handle=b_handle)  # B's handle means nothing in A
    assert a_attr.created and a_attr.id != b_attr.id
    with tenant_tx(a.ctx()) as conn:
        assert b_attr.id not in {r["id"] for r in rank_candidates(conn, "שטח ממ״ד", limit=10)}
        assert bump_facts_version(conn, b_attr.id) is None
    assert resolve(b, "שטח ממ״ד").facts_version == 1
