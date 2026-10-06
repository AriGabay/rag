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


def test_a_handle_of_another_dimension_is_ignored_for_the_description(offices):
    """GQ29: asked "the balconies' area", the interpreter chose the handle of "the number of balconies": the
    plan's own dimension contradicts the handle, so the description resolves (here to its exact definition)."""
    a, _ = offices
    count = resolve(a, "מספר המרפסות", unit_dimension="count")
    area = resolve(a, "שטח המרפסות", unit_dimension="area")
    shown = {h["id"]: h["handle"] for h in handles(a)}
    got = resolve(a, "שטח המרפסות", handle=shown[count.id], unit_dimension="area")
    assert (got.id, got.unit_dimension) == (area.id, "area")
    assert resolve(a, "מספר המרפסות", handle=shown[count.id], unit_dimension="count").id == count.id
    # "price" is a loose word for a price per m², not a conflict
    ppsm = next(h for h in handles(a) if h["key"] == "price_per_sqm")
    assert resolve(a, "מחיר ממוצע למטר", handle=ppsm["handle"], unit_dimension="price").key == "price_per_sqm"


def test_a_handle_sharing_no_word_with_a_description_of_another_definition_is_ignored(offices):
    a, _ = offices
    storage = resolve(a, "שטח המחסן")
    yard = resolve(a, "שטח החצר")
    shown = {h["id"]: h["handle"] for h in handles(a)}
    assert resolve(a, "שטח החצר", handle=shown[storage.id]).id == yard.id
    # a synonym that shares no word but names no other definition stays the interpreter's match (KTD7)
    assert resolve(a, "גודל מקום האחסון", handle=shown[storage.id]).id == storage.id
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


def test_text_value_type_is_honored_and_never_reuses_a_numeric_definition(offices):
    """Found with the real model: text attributes (zoning, planning status) were always numeric, so no text value
    could be accepted. A caller that says text gets a text definition, even when a numeric one has the label."""
    a, _ = offices
    numeric = resolve(a, "ייעוד המגרש", unit_dimension=None)  # an earlier turn created it without a type
    assert numeric.value_type == "numeric"
    zoning = resolve(a, "ייעוד המגרש", unit_dimension="area", value_type="text")
    assert zoning.created and zoning.value_type == "text" and zoning.id != numeric.id
    assert (zoning.unit_dimension, zoning.canonical_unit) == (None, None)  # a text value has no unit
    assert resolve(a, "ייעוד המגרש", unit_dimension=None, value_type="text").id == zoning.id
    assert resolve(a, "ייעוד המגרש", unit_dimension=None).id == numeric.id  # no type asked: the oldest match
    shown = next(h["handle"] for h in handles(a) if h["id"] == numeric.id)
    assert resolve(a, None, handle=shown, unit_dimension=None, value_type="text").id == zoning.id
    assert resolve(a, "מחיר", unit_dimension=None, value_type="text").source == "structured"
    with pytest.raises(ValueError):
        resolve(a, "צבע החזית", unit_dimension=None, value_type="color")


def test_a_definition_created_without_a_dimension_takes_one_named_later(db):
    """Found with the real model: a definition created with no dimension made every value "unit assumed"
    (all went to review). A later request naming the dimension upgrades it and re-reads its documents."""
    from app.answering.attributes import normalize_dimension

    a = make_office(db, "משרד א", "a@example.test")
    with tenant_tx(a.ctx()) as conn:
        first = resolve_attribute(conn, handle=None, description="גובה החלל", unit_dimension=None)
        assert first.unit_dimension is None
        again = resolve_attribute(conn, handle=None, description="גובה החלל", unit_dimension="height")
    assert again.id == first.id and again.unit_dimension == "length" and again.canonical_unit == "m"
    assert again.facts_version == first.facts_version + 1
    assert normalize_dimension("Quantity") == "count" and normalize_dimension("משהו") is None


def test_the_interpreter_sees_only_definitions_that_proved_useful(db):
    from app.answering.attributes import list_attribute_handles

    a = make_office(db, "משרד א", "a@example.test")
    with tenant_tx(a.ctx()) as conn:
        resolve_attribute(conn, handle=None, description="ניסוח רופף שלא הניב דבר", unit_dimension="area")
        everything = {h["label"] for h in list_attribute_handles(conn)}
        shown = {h["label"] for h in list_attribute_handles(conn, useful_only=True)}
    assert "ניסוח רופף שלא הניב דבר" in everything and "ניסוח רופף שלא הניב דבר" not in shown
    assert {"שטח", "מחיר", "מחיר למ״ר", "מספר חדרים"} <= shown


# --- stable identity: one definition per meaning (the user's item 3) -------------------------------------------

def test_another_form_of_the_same_words_reuses_the_definition_and_records_the_phrasing(offices):
    a, _ = offices
    first = resolve(a, "שטח המרפסת")
    again = resolve(a, "שטח המרפסות")
    word_order = resolve(a, "המרפסות שטח")
    assert first.created and not again.created and not word_order.created
    assert again.id == first.id == word_order.id
    assert count_defs(a, "source = 'extracted'") == 1
    with tenant_tx(a.ctx()) as conn:
        aliases = conn.execute(text("SELECT aliases FROM attribute_definitions WHERE id = :i"), {"i": first.id}).scalar()
    assert "שטח המרפסות" in aliases  # matched exactly from now on, and shown to the interpreter


@pytest.mark.parametrize(("one", "dim_one", "other", "dim_other"), [
    ("גובה החלון", "length", "רוחב החלון", "length"),  # another measure word
    ("שטח המחסן נטו", "area", "שטח המחסן ברוטו", "area"),  # another qualifier
    ("שטח המחסן", "area", "מספר המחסנים", "count"),  # another dimension and measure word
    ("מספר החניות", "count", "מספר החניות בבניין", "count"),  # another word
    ("שטח החצר", "area", "שטח החצר המשותפת", "area"),
])
def test_names_that_differ_in_meaning_or_unit_never_merge(offices, one, dim_one, other, dim_other):
    a, _ = offices
    x = resolve(a, one, unit_dimension=dim_one)
    y = resolve(a, other, unit_dimension=dim_other)
    assert x.id != y.id and y.created


def test_the_same_label_with_a_conflicting_dimension_is_another_attribute(offices):
    """The exact-label path checked no dimension: "the area of X" then "the number of X" phrased with one label
    must not read the area definition's facts as counts."""
    a, _ = offices
    area = resolve(a, "המחסן", unit_dimension="area")
    count = resolve(a, "המחסן", unit_dimension="count")
    assert area.id != count.id and area.unit_dimension == "area"


def test_text_and_boolean_requests_share_one_definition_but_never_a_numeric_one(offices):
    a, _ = offices
    as_text = resolve(a, "שיפוץ הדירה", unit_dimension=None, value_type="text")
    as_bool = resolve(a, "שיפוץ הדירה", unit_dimension=None, value_type="boolean")
    as_number = resolve(a, "שיפוץ הדירה", unit_dimension="year", value_type="numeric")
    assert as_bool.id == as_text.id and not as_bool.created
    assert as_number.id != as_text.id and as_number.value_type == "numeric"


def test_interpreter_handles_keep_their_numbers_and_show_the_value_type(offices):
    a, _ = offices
    resolve(a, "שטח המרפסת")
    with tenant_tx(a.ctx()) as conn:
        everything = list_attribute_handles(conn)
        shown = list_attribute_handles(conn, useful_only=True)
    assert {h["handle"] for h in shown} <= {h["handle"] for h in everything}
    assert all("value_type" in h for h in shown)
    by_id = {h["id"]: h["handle"] for h in everything}
    assert all(by_id[h["id"]] == h["handle"] for h in shown)  # hiding a definition never renumbers the others
