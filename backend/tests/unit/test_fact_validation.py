"""Mention validation without a database: a value must belong to the attribute, the subject and the cited place.

Counter-examples from the code review and the round-6 failure analysis: a quote match alone never accepts a value
that belongs to another attribute, another property (a comparable's row) or another counted noun.
"""

from __future__ import annotations

import uuid

import pytest

from app.answering import facts, units
from app.answering.attributes import AttributeDef, canonical_unit_for
from app.answering.facts import Mention, Source, VersionContent, fact_rows, validate_mention


def attr(label: str, dimension: str | None = "count", aliases: tuple[str, ...] = (), value_type: str = "numeric"):
    return AttributeDef(id=uuid.uuid4(), key="x_test", label=label, unit_dimension=dimension,
                        canonical_unit=canonical_unit_for(dimension), source="extracted", structured_column=None,
                        facts_version=1, extraction_prompt_version="x7", value_type=value_type,
                        aliases=[label, *aliases])


def chunk(handle: str, text: str, chunk_id=None) -> Source:
    return Source(handle, chunk_id or uuid.uuid4(), 1, text)


def cell(handle: str, value: str, header: str, row_label: str = "") -> Source:
    unit = units.parse_unit(header) or (units.parse_unit(row_label) if row_label else None)
    context = "\n".join(x for x in (f"{header}: {value}", f"{row_label}: {value}" if row_label else "") if x)
    t, rest = handle[1:].split("R")
    r, c = rest.split("C")
    return Source(handle, uuid.uuid4(), 1, value, context, unit, int(t) - 1, int(r) - 1, int(c) - 1,
                  " ".join(x for x in (header, row_label) if x))


def content(*sources: Source) -> VersionContent:
    return VersionContent(uuid.uuid4(), uuid.uuid4(), "שומה", {s.handle: s for s in sources}, "", False, None)


def mention(quote: str, value: str, term: str, source: str = "C1", role: str = "subject", unit: str | None = None):
    return Mention(entity_role=role, entity_descriptor=None, value_text=value, unit_text=unit, quote=quote,
                   source=source, attribute_term=term)


def status(accepted, c) -> str:
    rows = fact_rows([accepted], c)
    return rows[0]["status"]


BALCONIES = attr("מספר המרפסות")


# --- a count counts its own noun -----------------------------------------------------------------------------

def test_floor_number_is_not_a_count_of_another_noun():
    c = content(chunk("C1", "לדירה 2 מרפסות בקומה 3"))
    assert validate_mention(mention("לדירה 2 מרפסות בקומה 3", "3", "מרפסות"), c, BALCONIES) == "counted_other"
    ok = validate_mention(mention("לדירה 2 מרפסות בקומה 3", "2", "מרפסות"), c, BALCONIES)
    assert not isinstance(ok, str) and ok.canonical == 2
    assert status(ok, c) == "auto_validated"


def test_rooms_are_not_balconies_but_are_rooms():
    c = content(chunk("C1", "דירת 4 חדרים בקומה 3 עם 2 מרפסות"))
    assert validate_mention(mention("דירת 4 חדרים בקומה 3 עם 2 מרפסות", "4", "מרפסות"), c, BALCONIES) == \
        "counted_other"
    rooms = attr("מספר חדרים")
    ok = validate_mention(mention("דירת 4 חדרים בקומה 3", "4", "חדרים"), c, rooms)
    assert not isinstance(ok, str) and ok.canonical == 4
    assert validate_mention(mention("דירת 4 חדרים בקומה 3", "3", "חדרים"), c, rooms) == "counted_other"


def test_floors_of_a_building_are_a_count_of_floors():
    floors = attr("מספר הקומות בבניין")
    c = content(chunk("C1", "בניין בן 8 קומות"))
    ok = validate_mention(mention("בניין בן 8 קומות", "8", "קומות"), c, floors)
    assert not isinstance(ok, str) and ok.canonical == 8


def test_count_in_words_with_its_noun():
    c = content(chunk("C1", "בדירה שתי מרפסות"))
    ok = validate_mention(mention("בדירה שתי מרפסות", "שתי", "מרפסות"), c, BALCONIES)
    assert not isinstance(ok, str) and ok.canonical == 2 and status(ok, c) == "auto_validated"


# --- zero words -----------------------------------------------------------------------------------------------

PARKING = attr("מספר מקומות חניה צמודים לדירה", aliases=("מספר החניות",))


@pytest.mark.parametrize("quote", ["לדירה אין חניה צמודה", "לדירה אין מקום חניה", "ללא חניה"])
def test_a_zero_word_governing_the_attribute_is_zero(quote):
    c = content(chunk("C1", quote))
    term = "חניה"
    ok = validate_mention(mention(quote, quote.split()[1] if quote.startswith("לדירה") else "ללא", term), c, PARKING)
    assert not isinstance(ok, str) and ok.canonical == 0
    assert status(ok, c) == "auto_validated"


@pytest.mark.parametrize("quote", ["אין צורך בחניה", "חניות: אין מידע", "אין מעלית בבניין, לדירה חניה"])
def test_a_zero_word_about_something_else_is_no_value(quote):
    c = content(chunk("C1", quote))
    assert isinstance(validate_mention(mention(quote, "אין", "חניה"), c, PARKING), str)


@pytest.mark.parametrize("quote", ["אין חניה נוספת", "אין חניה בבניין הסמוך"])
def test_a_qualified_zero_goes_to_review(quote):
    c = content(chunk("C1", quote))
    ok = validate_mention(mention(quote, "אין", "חניה"), c, PARKING)
    assert not isinstance(ok, str) and ok.canonical == 0 and ok.uncertain
    assert status(ok, c) == "needs_review"


def test_absence_of_another_room_is_not_zero_balconies():
    c = content(chunk("C1", "אין בדירה ממ״ד"))
    assert isinstance(validate_mention(mention("אין בדירה ממ״ד", "אין", "מרפסות"), c, BALCONIES), str)


def test_zero_in_a_cell_named_by_its_row_label():
    c = content(cell("T1R2C2", "אין", "פירוט", row_label="חניה"))
    ok = validate_mention(mention("אין", "אין", "חניה", source="T1R2C2"), c, PARKING)
    assert not isinstance(ok, str) and ok.canonical == 0 and status(ok, c) == "auto_validated"


# --- the cited source ----------------------------------------------------------------------------------------

def test_quote_held_only_by_a_comparable_row_is_not_moved_there():
    """The adversarial case: a subject value cited to the subject paragraph, whose quote occurs only in a
    comparable's table row, is rejected instead of being re-cited to the comparable."""
    c = content(chunk("C1", "לדירה צמודה חניה אחת בחניון התת קרקעי"),
                cell("T1R3C4", "2 חניות", "חניות", row_label="רחוב הרצל 10"))
    assert validate_mention(mention("2 חניות", "2", "חניות", source="C1"), c, PARKING) == "quote_not_found"


def test_row_handle_resolves_to_the_cell_of_that_row():
    c = content(cell("T1R3C2", "12", "שטח ממ״ד (מ״ר)"), cell("T1R4C2", "12", "שטח מחסן (מ״ר)"))
    area = attr("שטח ממ״ד", "area")
    ok = validate_mention(mention("12", "12", "ממ״ד", source="T1R3"), c, area)
    assert not isinstance(ok, str) and ok.source.handle == "T1R3C2" and not ok.uncertain


def test_a_sibling_cell_handle_resolves_within_its_row_only():
    c = content(cell("T1R3C2", "92", "שטח דירה (מ״ר)"), cell("T1R3C3", "12", "שטח ממ״ד (מ״ר)"))
    area = attr("שטח ממ״ד", "area")
    ok = validate_mention(mention("12", "12", "ממ״ד", source="T1R3C2"), c, area)
    assert not isinstance(ok, str) and ok.source.handle == "T1R3C3"
    # the model names the apartment's cell for the safe room's value: its own label says otherwise
    assert isinstance(validate_mention(mention("92", "92", "ממ״ד", source="T1R3C3"), c, area), str)


def test_unknown_handle_with_one_holder_goes_to_review():
    c = content(chunk("C1", "שטח הממ״ד 12 מ״ר"))
    area = attr("שטח ממ״ד", "area")
    ok = validate_mention(mention("שטח הממ״ד 12 מ״ר", "12", "ממ״ד", source="X9"), c, area)
    assert not isinstance(ok, str) and ok.uncertain and status(ok, c) == "needs_review"


def test_quote_held_by_two_cells_of_the_cited_row_is_ambiguous():
    c = content(cell("T1R3C2", "12", "שטח (מ״ר)"), cell("T1R3C3", "12", "שטח נוסף (מ״ר)"))
    area = attr("שטח ממ״ד", "area")
    assert validate_mention(mention("12", "12", "ממ״ד", source="T1R3"), c, area) == "ambiguous_source"


# --- naming --------------------------------------------------------------------------------------------------

def test_term_in_another_form_of_the_same_word_is_found():
    """GQ29: the plural term for a singular quote ("מרפסות" / "מרפסת סלון") names the value."""
    area = attr("שטח המרפסות", "area")
    c = content(chunk("C1", "בדירה שתי מרפסות: מרפסת סלון בשטח 12 מ״ר ומרפסת חדר שינה בשטח 6 מ״ר"))
    ok = validate_mention(mention("מרפסת סלון בשטח 12 מ״ר", "12", "מרפסות"), c, area)
    assert not isinstance(ok, str) and ok.canonical == 12


def test_a_term_naming_another_attribute_is_rejected():
    storage = attr("שטח המחסן", "area")
    c = content(chunk("C1", "שטח הנכס: 72 מ״ר נטו"))
    others = [["הנכס"]]
    assert validate_mention(mention("שטח הנכס: 72 מ״ר נטו", "72", "שטח הנכס"), c, storage, others) == \
        "other_attribute"
    # without a registry entry for it, another phrasing still never enters a figure directly
    ok = validate_mention(mention("שטח הנכס: 72 מ״ר נטו", "72", "שטח הנכס"), c, storage)
    assert not isinstance(ok, str) and status(ok, c) == "needs_review"


def test_a_synonym_whose_words_are_partly_another_attributes_is_not_rejected():
    area = attr("שטח ממ״ד", "area")
    c = content(chunk("C1", "המרחב המוגן בדירה בשטח 12 מ״ר"))
    ok = validate_mention(mention("המרחב המוגן בדירה בשטח 12 מ״ר", "12", "המרחב המוגן בדירה"), c, area,
                          [["המרפסות", "בדירה"]])
    assert not isinstance(ok, str) and ok.synonym


def test_quote_words_name_the_value_whatever_term_the_model_chose():
    year = attr("שנת הבנייה של הבניין", "year")
    c = content(chunk("C1", "הבניין הושלם בשנת 2004"))
    a = validate_mention(mention("הבניין הושלם בשנת 2004", "2004", "הושלם בשנת"), c, year)
    b = validate_mention(mention("הבניין הושלם בשנת 2004", "2004", "הבניין הושלם בשנת"), c, year)
    assert not isinstance(a, str) and not isinstance(b, str)
    # the quote's own words decide, not the model's choice of term: the same verdict both ways
    assert status(a, c) == status(b, c) == "auto_validated"


def test_several_unitless_counts_without_a_binding_go_to_review():
    c = content(chunk("C1", "בבניין 3 ו-4"))
    ok = validate_mention(mention("בבניין 3 ו-4", "3", "מרפסות"), c, BALCONIES)
    assert isinstance(ok, str) or status(ok, c) == "needs_review"


def test_facts_module_reexports_the_validation_contract():
    assert facts.GENERIC_MEASURE_WORDS


# --- final review counter-examples ----------------------------------------------------------------------------

AREA = attr("שטח הדירה", "area")


@pytest.mark.parametrize("quote", ["שטח המגרש עליו בנויה הדירה 500 מ״ר", "הדירה נמכרה; שטח החניה 12.5 מ״ר"])
def test_scattered_term_words_name_nothing(quote):
    c = content(chunk("C1", quote))
    value = "500" if "500" in quote else "12.5"
    ok = validate_mention(mention(quote, value, "שטח הדירה"), c, AREA)
    assert isinstance(ok, str) or status(ok, c) == "needs_review"


@pytest.mark.parametrize(("quote", "term"), [("לדירה צמוד מחסן בשטח 6 מ״ר", "מחסן"),
                                             ("בדירה מרפסת בשטח 12 מ״ר", "מרפסת")])
def test_the_models_nearer_term_keeps_a_value_from_becoming_the_attributes(quote, term):
    c = content(chunk("C1", quote))
    value = "6" if " 6 " in quote else "12"
    ok = validate_mention(mention(quote, value, term), c, AREA)
    assert isinstance(ok, str) or status(ok, c) == "needs_review"


@pytest.mark.parametrize(("label", "quote", "value", "term"), [
    ("מספר הקומות בבניין", "הדירה ממוקמת בקומה 3", "3", "בקומה"),
    ("מספר החדרים", "חדר 2 משמש כמחסן", "2", "חדר"),
    ("מספר יחידות הדיור בבניין", "הנכס הנישום הוא דירה 5 בבניין", "5", "דירה"),
])
def test_an_ordinal_is_not_a_count(label, quote, value, term):
    """A position ("בקומה 3", "חדר 2", "דירה 5") never enters a count figure: rejected as an ordinal when the
    noun is a counted-noun unit, else held for review."""
    c = content(chunk("C1", quote))
    ok = validate_mention(mention(quote, value, term), c, attr(label))
    assert ok == "counted_other" or (not isinstance(ok, str) and status(ok, c) == "needs_review")


@pytest.mark.parametrize("quote", ["בבניין 12 דירות", "בבניין 12 יח״ד"])
def test_a_counted_noun_of_the_same_unit_counts_the_attribute(quote):
    c = content(chunk("C1", quote))
    ok = validate_mention(mention(quote, "12", quote.split()[-1]), c, attr("מספר יחידות הדיור"))
    assert not isinstance(ok, str) and ok.canonical == 12


def test_a_counted_noun_covering_only_part_of_the_name_is_uncertain():
    c = content(chunk("C1", "דירת 5 חדרים"))
    ok = validate_mention(mention("דירת 5 חדרים", "5", "חדרים"), c, attr("מספר חדרי שינה"))
    assert not isinstance(ok, str) and status(ok, c) == "needs_review"


@pytest.mark.parametrize("quote", ["ברחוב אין חניה", "בסביבה ללא חניה", "אין חניה בטאבו"])
def test_a_zero_with_a_place_or_a_register_is_not_a_plain_zero(quote):
    c = content(chunk("C1", quote))
    ok = validate_mention(mention(quote, "אין" if "אין" in quote else "ללא", "חניה"), c, attr("מספר מקומות החניה"))
    assert isinstance(ok, str) or status(ok, c) == "needs_review"


def test_another_attribute_of_another_dimension_never_rejects_a_value():
    c = content(chunk("C1", "שטח הדירה 95 מ״ר"))
    ok = validate_mention(mention("שטח הדירה 95 מ״ר", "95", "שטח הדירה"), c, attr("שטח הנכס", "area"), [])
    assert not isinstance(ok, str)


# --- completeness: when a figure may stand for the document set -----------------------------------------------

@pytest.mark.parametrize(("cov", "n", "op", "want"), [
    ({}, 3, "mean", "complete"),
    ({"not_stated": 5}, 3, "mean", "complete"),  # a document that does not state the datum is no gap
    ({"awaiting_review": 1}, 2, "mean", "subset"),
    ({"unknown_metadata": 1, "awaiting_review": 1}, 2, "mean", "subset"),
    ({"not_yet_extracted": 3}, 2, "mean", "insufficient"),  # more documents unread than values found
    ({"partial_scan": 1}, 0, "count", "insufficient"),
    ({"awaiting_review": 1}, 4, "sum", "insufficient"),  # a sum over part of the documents is not their sum
])
def test_completeness(cov, n, op, want):
    assert facts.completeness(cov, n, op) == want


def test_a_conflict_beyond_the_extreme_decides_min_and_max_only():
    from decimal import Decimal as D
    fig = facts.aggregate([D(1968), D(2017)], "min")
    held = [{"included": False, "values": [{"value": D(1958)}, {"value": D(1962)}]}]
    assert facts._conflict_decides(held, fig, "min")
    assert not facts._conflict_decides(held, facts.aggregate([D(1968), D(2017)], "max"), "max")
    assert not facts._conflict_decides(held, fig, "mean")
    assert not facts._conflict_decides([{**held[0], "included": True}], fig, "min")
