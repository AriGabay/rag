"""Round 23 U2 (M2; R3, R4, R8, R19, R20; KTD3, KTD7): a server sentence about what the source says needs evidence.

A provenance flag (model_asserted / stated_different / silent) is never turned into "the source does not state X" or "the source states
another X" unless a complete read of the datum's own parts (cell, row label, every header row, caption, table notes, the governing note, the
sentence) shows it. An incomplete read is worded "could not be confirmed"; a raw enum ("other") is never shown to the user. The vocabulary
and scale holes the diagnosis found (G01-G04, G09-G12) are closed in the shared readers, each with a negative control.

Invented Hebrew and English texts only; database-free."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.answering.verify import not_an_amount, numbers_in
from app.chat import calc, meaning, verify
from app.chat import tools as T
from tests.unit.test_take_value_resolve import _loc, _table, _take, _workspace
from tests.unit.test_verify_units import _registered, split_units


def _forms(written: str) -> frozenset[str]:
    return frozenset(numbers_in(written))


# --- G01: the area basis "built" in its other written form ---------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "מחיר למ\"ר מבונה 12,000 ₪", "שטח מבונה 100 מ\"ר", "100 מ\"ר מבונים", "שווי מ\"ר מבונה: 9,500 ₪", "מחיר ל-מ\"ר מבונה 8,000 ₪",
])
def test_g01_the_built_area_basis_is_read_in_both_spellings(text):
    assert meaning._all(text).keys("basis") == {"בנוי"}
    assert meaning.basis_key(text) == meaning.basis_key(text.replace("מבונה", "בנוי").replace("מבונים", "בנוי"))


@pytest.mark.parametrize("text", [
    "הבניין מבונה בבטון 12,000 ₪",        # the word alone, with no area word beside it: no basis
    "מבונה 12,000 ₪",
    "מחיר למ\"ר מבוטן 12,000 ₪",          # a different word
])
def test_g01_negative_a_built_word_with_no_area_beside_it_states_no_basis(text):
    assert meaning._all(text).keys("basis") == set()


# --- G02: a plot or block number is no second amount of its clause -------------------------------------------------------------------------

@pytest.mark.parametrize("source", [
    "אומדן שווי המקרקעין בגוש 1234 חלקה 5 הוא 3,100,000 ₪.",
    "אומדן שווי חלקות 12-14 הוא 3,100,000 ₪.",
    "אומדן שווי גוש 1234/56 הוא 3,100,000 ₪.",
    "אומדן שווי חלקות 12 ו-13 בגוש 1234 הוא 3,100,000 ₪.",
    "אומדן שווי הקרקע בגושים 1234 ו-1235 הוא 3,100,000 ₪.",
    "אומדן שווי מגרשים 7-9 הוא 3,100,000 ₪.",
    "אומדן שווי תאי שטח 101-103 הוא 3,100,000 ₪.",
])
def test_g02_a_clause_with_plot_numbers_and_one_amount_confirms_the_kind(source):
    kind, unit, clause = meaning.confirmed_in_source(source, _forms("3,100,000"), "3,100,000 ₪", "value", "ILS")
    assert kind and unit and "3,100,000" in clause


@pytest.mark.parametrize("source", [
    "אומדן שווי חלקה 5 הוא 3,100,000 ₪ ושטחה 500 מ\"ר.",           # a second amount (an area)
    "אומדן שווי חלקות 12-14 הוא 3,100,000 ₪ מתוך 4,200,000 ₪.",    # a second amount
    "אומדן שווי הוא 3,100,000 ₪ לעומת 2,900,000 ₪ בגוש 1234.",
])
def test_g02_negative_a_clause_with_two_amounts_does_not_confirm_the_kind(source):
    assert meaning.confirmed_in_source(source, _forms("3,100,000"), "3,100,000 ₪", "value", "ILS") == (False, False, "")


@pytest.mark.parametrize("text, number, expected", [
    ("חלקות 12-14 בגוש", "14", True), ("חלקות 12 ו-13", "13", True), ("גוש 1234/56", "56", True), ("מגרשים 7-9", "9", True),
    ("הוצאות 12-14 ₪", "14", False),                 # no label: an amount
    ("הקצאה 1234/56 ₪ לחודש", "56", False),
])
def test_g02_the_continuation_of_an_identifier_list_is_an_identifier(text, number, expected):
    at = text.rindex(number)
    assert not_an_amount(text, at, at + len(number), years=False) is expected


# --- G03: "מס'" is a number label, "מס" is a levy -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("context", ["מס' 4", "מס׳ 4", "דירה מס' 12 | 1,000,000", "מס. 4", "מס\" 7", "מס 4"])
def test_g03_the_abbreviation_for_number_is_not_the_levy_kind(context):
    assert not meaning.kind_attested("levy", context)
    assert meaning.kind_written("levy", context) == ""
    assert not meaning.kind_attested("count", context)  # nor a count: "מס' 4" counts nothing


@pytest.mark.parametrize("context", ["מס רכישה", "מס שבח 1,200,000", "היטל השבחה", "חיוב מס' 4 - מס רכישה"])
def test_g03_negative_the_word_for_tax_is_still_the_levy_kind(context):
    assert meaning.kind_attested("levy", context)


def test_g03_a_count_word_beside_a_number_still_attests_a_count():
    assert meaning.kind_attested("count", "מספר חדרים 4")
    assert meaning.kind_attested("count", "מספר הדירות בבניין | 12")
    assert not meaning.kind_attested("count", "מספר 4")


def test_g03_a_row_numbered_with_the_abbreviation_states_no_other_kind_for_a_value():
    kind, stated = T._kind_state("value", "ILS", "דירה מס' 4 | שווי", whole=True)
    assert (kind, stated) == ("source", "")
    kind, stated = T._kind_state("income", "ILS", "מס' 4 | סכום", whole=True)
    assert kind == "silent" and stated == ""


# --- G09: English period headers, quarterly columns -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("header, period", [
    ("For the Years Ending", "year"), ("for the year ending", "year"), ("Year 1 | Year 2 | Year 3", "year"), ("Year 10", "year"),
    ("Annual", "year"), ("Quarterly", "quarter"), ("per quarter", "quarter"), ("/quarter", "quarter"), ("Monthly", "month"),
    ("רבעוני", "quarter"), ("לרבעון", "quarter"),
])
def test_g09_period_headers_are_periods(header, period):
    assert meaning._all(header).keys("period") == {period}


@pytest.mark.parametrize("text", [
    "in the year 2020 the rent was 5,000", "Year-end", "the years of experience", "Year", "built in year", "for the years",
    "quarters", "a quarter of the area",
])
def test_g09_negative_other_uses_of_the_words_are_no_period(text):
    assert meaning._all(text).keys("period") == set()


def test_g09_a_stated_quarterly_period_is_not_read_as_silence_for_a_year_claim():
    # the source states "quarterly"; the answer's "לשנה" is a different period, not an unstated one
    attested = meaning._all("Quarterly rent 1,250")
    assert attested.keys("period") == {"quarter"}
    assert meaning.PERIOD_SHOWN["quarter"]


def test_g09_a_quarterly_source_period_is_never_filled_into_a_value_as_a_period():
    d_table = _table(["Item", "Quarterly"], [["Rent", "1,250"]])
    import pytest as _p

    mp = _p.MonkeyPatch()
    try:
        d = _workspace(mp, d_table)
        with pytest.raises(T.ToolError, match="רבעון"):
            _take(d, _loc(row="Rent", column="Quarterly"), kind="rent", unit="ILS", period="month")
        _take(d, _loc(row="Rent", column="Quarterly"), kind="rent", unit="ILS", period="unknown")
        v = d.ws.values["V1"]
        assert v.period == "unknown" and v.provenance["period"] != "source"
    finally:
        mp.undo()


# --- G10: a negated period word claims no period -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "הסכום 5,000 ₪ אינו מוצג כתשלום חודשי",
    "הסכום 5,000 ₪ אינו מוצג במקור כתשלום חודשי",
    "הסכום 5,000 ₪ אינו נקבע על ידי הצדדים על בסיס חודשי",
    "הסכום 5,000 ₪ לא שולם על פי המקור כסכום חודשי",
    "הסכום אינו מוצג על ידי המעריך כתשלום שנתי של 5,000 ₪",
])
def test_g10_a_period_word_in_a_negated_part_of_its_clause_states_no_period(text):
    entries, general = meaning.attached(text)
    assert not general.keys("period") and all(not q.keys("period") for _, _, q in entries)
    assert meaning._all(text).keys("period") == set()


@pytest.mark.parametrize("text, period", [
    ("הסכום לא הושכר בפועל ולכן הוערך לפי 5,000 ₪ לחודש", "month"),          # the negation belongs to the earlier verb
    ("לא ידוע, אבל התשלום 5,000 ₪ חודשי", "month"),                            # a comma and a conjunction close it
    ("הסכום אינו כולל מע\"מ והתשלום 5,000 ₪ חודשי", "month"),
    ("התשלום 5,000 ₪ חודשי ואינו צמוד", "month"),                              # the negation after the word
    ("אין חריגה והתשלום הוא 5,000 ₪ לשנה", "year"),
])
def test_g10_negative_a_negation_elsewhere_leaves_the_period_word_a_period(text, period):
    assert period in meaning._all(text).keys("period")


# --- G12: the unit word of a quote is a unit, not a stated kind -----------------------------------------------------------------------------

@pytest.mark.parametrize("context", ["זכויות בנייה נוספות 120 מ\"ר", "120 מ\"ר", "שטח הזכויות המותר 120 מ\"ר"])
def test_g12_a_unit_word_beside_a_number_is_not_another_kind_than_the_registered_rights_area(context):
    kind, stated = T._kind_state("rights_area", "sqm", context, whole=True)
    assert kind in ("source", "silent") and stated == ""


def test_g12_the_unit_mark_is_not_a_kind_but_the_word_for_an_area_still_is():
    # a registered rights area against "שטח" in the row: the source names an area kind; against only "מ\"ר": the unit
    assert T._kind_state("value", "ILS", "שווי המגרש 120 מ\"ר", whole=True)[0] == "source"
    kind, stated = T._kind_state("rights_area", "sqm", "שטח מגרש | מ\"ר", whole=True)
    assert kind == "stated_different" and stated == "שטח"


# --- G04 / R3: the unit row that spans a table ---------------------------------------------------------------------------------------------

def test_g04_a_unit_row_that_spans_the_table_is_the_cells_unit_and_scale(monkeypatch):
    table = _table(["", ""], [["(באלפי ₪)", ""], ["שווי נכס", "9,500"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="שווי נכס", column_number=2), kind="value", unit="ILS")
    v = d.ws.values["V1"]
    assert v.unit == "ILS" and v.provenance["unit"] == "source" and v.scale == 1000
    assert T.unconfirmed_attributes(d.ws, "V1") == []


def test_g04_a_spanning_row_below_the_first_row_of_numbers_is_not_a_header(monkeypatch):
    table = _table(["", ""], [["שווי נכס", "9,500"], ["(באלפי ₪)", ""], ["שווי אחר", "8,000"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="שווי אחר", column_number=2), kind="value", unit="ILS")
    v = d.ws.values["V1"]
    assert v.provenance["unit"] == "model_asserted" and v.scale == 1


@pytest.mark.parametrize("unit", ["other", "ratio"])
@pytest.mark.parametrize("complete", [True, False])
def test_g04_a_note_never_prints_a_unit_enum(unit, complete):
    v = _value(unit=unit, prov={"unit": "model_asserted"}, read={"unit": complete})
    ws = T.Workspace(ctx=None)
    ws.values["V1"] = v
    (clause,) = T.attribute_clauses(ws, "V1", ["unit"])
    assert "other" not in clause and "ratio" not in clause and "והחישוב מניח" not in clause


# --- R3: wording follows the completeness of the read ----------------------------------------------------------------------------------------

def _value(*, kind: str = "income", unit: str = "ILS", prov: dict | None = None, read: dict | None = None,
           stated: dict | None = None) -> calc.Value:
    base = {"unit": "source", "kind": "source", "period": "source", "vat": "source", "area_basis": "source"}
    v = calc.Value("V1", Decimal("5200000"), "5,200,000", "S1", uuid.uuid4(), uuid.uuid4(), None, "דוח בדיקה", "עמוד 1",
                   "ערך", kind, unit, "none", "excluded", "", "נכס הדגמה", "other", base | (prov or {}), {"quote": "5,200,000"},
                   "5,200,000")
    v.read = read or {}
    v.stated = stated or {}
    return v


def _ws_of(v: calc.Value) -> T.Workspace:
    ws = T.Workspace(ctx=None)
    ws.values[v.vid] = v
    return ws


def test_r3_a_kind_nothing_supports_after_a_complete_read_may_be_called_not_stated():
    ws = _ws_of(_value(prov={"kind": "model_asserted"}, read={"kind": True}))
    (clause,) = T.attribute_clauses(ws, "V1", ["kind"])
    assert clause == T.MSG_ATTR_KIND_ASSERTED.format(assumed="הכנסות") and "לא נאמר במקור" in clause


@pytest.mark.parametrize("read", [{}, {"kind": False}, {"unit": True}])
def test_r3_a_kind_with_an_incomplete_read_is_never_called_not_stated_in_the_source(read):
    ws = _ws_of(_value(prov={"kind": "model_asserted"}, read=read))
    (clause,) = T.attribute_clauses(ws, "V1", ["kind"])
    assert "לא נאמר" not in clause and "לא ניתן היה לאשר" in clause and "הכנסות" in clause


@pytest.mark.parametrize("read", [{}, {"unit": False}])
def test_r3_a_unit_with_an_incomplete_read_is_worded_could_not_be_confirmed(read):
    ws = _ws_of(_value(prov={"unit": "model_asserted"}, read=read))
    (clause,) = T.attribute_clauses(ws, "V1", ["unit"])
    assert "לא נאמרה" not in clause and "לא ניתן היה לאשר" in clause and "₪" in clause


def test_r3_the_source_states_another_kind_only_with_the_sources_words():
    ws = _ws_of(_value(prov={"kind": "stated_different"}, read={"kind": True}, stated={"kind": "שווי"}))
    (clause,) = T.attribute_clauses(ws, "V1", ["kind"])
    assert "המקור מציין «שווי»" in clause
    ws = _ws_of(_value(prov={"kind": "stated_different"}, read={"kind": True}))  # the words were not kept: nothing to quote
    (clause,) = T.attribute_clauses(ws, "V1", ["kind"])
    assert "המקור מציין" not in clause and "לא ניתן היה לאשר" in clause


def test_r3_a_table_cell_read_whole_registers_a_complete_read(monkeypatch):
    table = _table(["נכס", "סכום"], [["הכנסה", "9,500"]], caption="טבלה")
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="הכנסה", column="סכום"), kind="income", unit="ILS")
    v = d.ws.values["V1"]
    assert v.provenance["unit"] == "model_asserted" and v.read["kind"] is True and v.read["unit"] is True
    (clause,) = T.attribute_clauses(d.ws, "V1", ["unit"])
    assert "לא נאמרה במקור" in clause and "other" not in clause


def _quote_meaning(full: str, quote: str, number: str):
    src = T.Source(sid="S1", document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה", section=None, location="עמוד 1",
                   kind="context", text=full, block_start=0, block_end=0, reading_id="r1", status="complete")
    taken = T._take_quote(src, full, {"quote": quote, "number": number}, [(0, full, 1)])
    given = {"kind": "income", "unit": "ILS", "period": "unknown", "vat": "unknown", "area_basis": ""}
    _, prov, anchors = T._settle_meaning(taken, given)
    return prov, anchors["read"]


def test_r3_a_quote_whose_clause_cannot_be_isolated_is_an_incomplete_read():
    prov, read = _quote_meaning("בנכס 9,500 ₪ ו-3,000 ₪ בשנה.", "בנכס 9,500 ₪ ו-3,000 ₪ בשנה", "9,500")
    assert prov["kind"] == "model_asserted" and read.get("kind") is not True


def test_r3_a_quote_whose_clause_is_the_numbers_alone_is_a_complete_read():
    prov, read = _quote_meaning("בנכס 9,500 ₪ בשנה.", "בנכס 9,500 ₪", "9,500")
    assert prov["kind"] == "silent" and read["kind"] is True and read["unit"] is True


# --- the calculation's note names only what its operation read -----------------------------------------------------------------------------

def test_calc_a_calculation_that_read_no_kind_or_unit_is_not_conditional_on_them():
    from app.chat.verify import conditional_qualifier

    ws = _ws_of(_value(prov={"kind": "model_asserted"}, read={"kind": True}))
    ws.values["V1"].reading = "uncertain"
    out = calc.Outcome.__new__(calc.Outcome)
    out.consulted = frozenset()
    out.conditional = []

    class C:  # a computation record that consulted nothing of V1
        uncertain = ["V1"]
        outcome = out

    note = conditional_qualifier(C(), True, ws)
    assert "סוג הנתון" not in note and "אינו ודאי" in note


# --- G11: the VAT of a value in a scaled table backs the amount restated in another scale --------------------------------------------------------

def _scaled_value(written="12,000", scale=1000, vat="excluded"):
    ws = _registered(written, f"שווי הנכס: {written} ללא מע\"מ", vat=vat)
    ws.values["V1"].scale = scale
    return ws


@pytest.mark.parametrize("claim", [
    "שווי הנכס הוא 12 מיליון ₪, ללא מע\"מ [V1].",
    "שווי הנכס הוא 12,000 אלף ₪, ללא מע\"מ [V1].",
    "שווי הנכס הוא 12,000,000 ₪, ללא מע\"מ [V1].",
    "שווי הנכס הוא 12,000 ₪, ללא מע\"מ [V1].",
])
def test_g11_a_thousands_table_value_backs_its_vat_in_every_restated_scale(claim):
    ws = _scaled_value()
    unit = split_units(claim)[0]
    assert verify._wrong_vat(unit, ws) == [] and verify._vat_problems(unit, ws) == []


@pytest.mark.parametrize("claim", [
    "שווי הנכס הוא 13 מיליון ₪, ללא מע\"מ [V1].",          # a different value
    "שווי הנכס הוא 12 מיליון ₪, כולל מע\"מ [V1].",          # the opposite status
    "שווי הנכס הוא 12,000,001 ₪, ללא מע\"מ [V1].",
])
def test_g11_negative_a_different_value_or_status_is_still_wrong(claim):
    unit = split_units(claim)[0]
    assert verify._wrong_vat(unit, _scaled_value())


def test_g11_without_a_source_scale_a_restated_amount_is_still_not_backed():
    unit = split_units("שווי הנכס הוא 12 מיליון ₪, ללא מע\"מ [V1].")[0]
    assert verify._wrong_vat(unit, _scaled_value(scale=1))


# --- the qualifier edit reads the column's period too ----------------------------------------------------------------------------------------

def test_edit_a_period_the_columns_header_states_is_not_silence(monkeypatch):
    ws = _registered("5,000", "שכר דירה 5,000 ₪", period="month", provenance={"period": "model_asserted"})
    unit = split_units("שכר הדירה הוא 5,000 ₪ לחודש [V1].")[0]
    column = meaning.Qualifiers()
    column.add("period", "year", "Annual", "column")
    occurrence = meaning.Occurrence(meaning.Qualifiers(), "שכר דירה 5,000 ₪", column_period=column)
    monkeypatch.setattr(meaning, "_evidence", lambda u, w, forms: ([occurrence], meaning.Qualifiers()))
    _, attested = verify._attested_qualifiers(unit, ws, frozenset({"5000"}))
    assert attested.keys("period") == {"year"}


# --- the qualifier edit's sentences about the source need a read in full ----------------------------------------------------------------------

def _edit_ws(unclear: bool):
    from tests.unit.test_verify_edit_ladder import ASSERTED, RENT

    ws = _registered("5,000", RENT, period="month", provenance=ASSERTED)
    if unclear:
        ws.uncertain_values["V1"] = "האזור לא נקרא בבירור"
    return ws


def _edit_report(unclear: bool, question: str):
    from tests.unit.test_verify_edit_ladder import _report

    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _edit_ws(unclear), question)
    return a, r, r.edit(a)


def test_edit_a_period_the_source_does_not_state_is_called_not_stated_after_a_read_in_full():
    a, r, edited = _edit_report(False, "מה שכר הדירה לחודש?")
    assert edited.doc.text.split("\n")[0] == "שכר הדירה הוא 5,000 ₪ (התקופה לא צוינה במקור) [V1]."
    assert "שלא נכתב במקור" in edited.doc.text and "שלא אומת" not in edited.doc.text


def test_edit_a_value_whose_region_stayed_unclear_is_never_called_unstated():
    a, r, edited = _edit_report(True, "מה שכר הדירה לחודש?")
    text = edited.doc.text
    assert r.qualifier_edits  # the qualifier still goes (the source gives it no support), but the server does not say the source is silent
    assert "לא צוינה במקור" not in text and "שלא נכתב במקור" not in text
    assert "שלא אומת מהמקור" in text and text.startswith("שכר הדירה הוא 5,000 ₪ [V1].")


def test_edit_the_unconfirmed_edit_note_is_counted_with_its_own_wording():
    edits = [verify.QualifierEdit(0, "period", 0, 4, "לחודש", "", confirmed=False)]
    assert verify.MSG_QUALIFIER_UNCONFIRMED.format(label=verify.QUALIFIER_LABELS["period"], n=len(edits)).startswith("> הוסר ניסוח של התקופה")
    assert "שלא אומת מהמקור" in verify.MSG_QUALIFIER_UNCONFIRMED and "שלא נכתב במקור" not in verify.MSG_QUALIFIER_UNCONFIRMED


@pytest.mark.parametrize("status, clipped, regions, expected", [
    (None, False, [], True), ("complete", False, [], True), ("clipped", True, [], False), ("uncertain_reading", False, [], False),
    ("has_unread_regions", False, ["R1"], False), ("complete", False, ["R2"], False),
])
def test_edit_a_cited_passage_is_read_in_full_only_when_it_was_not_clipped_or_left_regions_unread(status, clipped, regions, expected):
    from tests.unit.test_verify_units import _ws

    ws = _ws("שכר דירה 5,000 ₪")
    src = ws.sources["S1"]
    src.status, src.clipped, src.unread_regions = status, clipped, regions
    assert verify._read_in_full(split_units("שכר הדירה הוא 5,000 ₪ [S1].")[0], ws) is expected


# --- G01 flow: the basis a source states in either spelling is the draft's basis, not a second one ---------------------------------------------

@pytest.mark.parametrize("stated, said", [("בנוי", "בנוי"), ("מבונה", "מבונה"), ("מבונה", "בנוי"), ("בנוי", "מ\"ר מבונה"), ("מבונה", "מ\"ר בנוי")])
def test_g01_an_area_basis_the_agent_gives_in_the_other_spelling_is_the_sources(monkeypatch, stated, said):
    d = _workspace(monkeypatch, _table(["נכס", f"מחיר למ\"ר {stated}"], [["דירה", "9,500 ₪"]]))
    _take(d, _loc(row="דירה", column_number=2), kind="value", unit="ILS_per_sqm", area_basis=said)
    v = d.ws.values["V1"]
    assert v.provenance["area_basis"] == "source" and meaning.basis_key(v.area_basis) == "בנוי"


def test_g01_negative_another_basis_is_still_refused(monkeypatch):
    d = _workspace(monkeypatch, _table(["נכס", "מחיר למ\"ר מבונה"], [["דירה", "9,500 ₪"]]))
    for said in ("ברוטו", "מ\"ר נטו", "שווי"):
        with pytest.raises(T.ToolError, match="בסיס שטח"):
            _take(d, _loc(row="דירה", column_number=2), kind="value", unit="ILS_per_sqm", area_basis=said)


def test_g01_two_spellings_of_the_built_basis_are_one_basis_for_a_calculation():
    assert meaning.basis_key("מבונה") == meaning.basis_key("מ\"ר בנוי") == meaning.basis_key("בנוי")
    assert meaning.basis_key("ברוטו") != meaning.basis_key("מבונה")


# --- follow-up 1: unknown is not absence - an attribute the answer states that the registration lacks is kept ------------------------------------------

BUILT_ROW = "דירה 4,969 | 500 ₪"


def _per_sqm(basis_prov: str = "not_stated", basis: str = "", **read) -> T.Workspace:
    ws = _registered("500", BUILT_ROW, unit="ILS_per_sqm", area_basis=basis, provenance={"area_basis": basis_prov})
    ws.values["V1"].read = dict(read)
    return ws


BUILT_CLAIM = "השטח הוא 4,969 מ״ר עיקרי, לפי 500 ₪ למ״ר מבונה [V1]."


def test_f1_a_basis_the_answer_states_and_the_registration_lacks_is_kept_not_removed():
    ws = _per_sqm()  # a value taken by older code: no basis, no read flags
    unit = split_units(BUILT_CLAIM)[0]
    assert [p for p in verify.deterministic([unit], ws, "?") if p.removes_unit] == []
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking] == []


def test_f1_a_registration_read_whole_that_states_no_basis_still_lets_the_unsupported_basis_go():
    ws = _per_sqm(period=True, area_basis=True, vat=True, kind=True, unit=True)
    unit = split_units(BUILT_CLAIM)[0]
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking]  # the source was read in full and is silent: the qualifier is the model's


def test_f1_a_basis_the_source_states_differently_is_still_a_removal():
    ws = _per_sqm("source", "מ\"ר ברוטו")  # registered from the source's own header: gross
    ws.values["V1"].read = {"area_basis": True}
    unit = split_units(BUILT_CLAIM)[0]
    blocking = [m for m in verify.check_meaning(unit, ws) if m.blocking]
    assert blocking and blocking[0].kind == "basis"
    assert verify._silent_qualifier_spans(unit, ws, meaning._norm(unit.text), meaning.attached(meaning._norm(unit.text))[0], blocking[0], []) is None


def test_f1_a_basis_the_source_states_differently_is_removed_even_without_a_read_flag():
    ws = _per_sqm("source", "מ\"ר ברוטו")  # an older value: the stated other value is the evidence, whatever the flags
    unit = split_units(BUILT_CLAIM)[0]
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking]


def test_f1_a_period_the_registration_lacks_is_kept_without_a_read_flag_and_cut_with_one():
    from tests.unit.test_verify_edit_ladder import ASSERTED, RENT

    ws = _registered("5,000", RENT, period="month", provenance=ASSERTED)
    ws.values["V1"].read = {}  # a value taken by older code
    unit = split_units("שכר הדירה הוא 5,000 ₪ לחודש [V1].")[0]
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking] == []
    ws.values["V1"].read = {"period": True}
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking]


def test_f1_a_passage_that_was_clipped_never_calls_the_period_unstated():
    from tests.unit.test_verify_units import _ws

    ws = _ws("שכר דירה 5,000 ₪")
    unit = split_units("שכר הדירה הוא 5,000 ₪ לחודש [S1].")[0]
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking]
    ws.sources["S1"].clipped = True
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking] == []


def test_f1_a_vat_the_registration_lacks_is_kept_when_the_read_is_not_complete():
    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat="included", provenance={"vat": "model_asserted"})
    ws.values["V1"].read = {}  # a value taken by older code
    unit = split_units("שווי הנכס הוא 2,450,000 ₪, כולל מע\"מ [V1].")[0]
    assert [p for p in verify.deterministic([unit], ws, "?") if p.check == "vat"] == []
    ws.values["V1"].read = {"vat": True}
    assert [p.check for p in verify.deterministic([unit], ws, "?")] == ["vat"]
    ws2 = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat="excluded", provenance={"vat": "source"})  # the source states the other VAT
    assert [p.check for p in verify.deterministic([unit], ws2, "?")] == ["vat"]


@pytest.mark.parametrize("header", ["שווי מ\"ר מבונה", "שווי למ\"ר בנוי", "מחיר ₪ למ\"ר מבונה"])
def test_f1_a_cell_under_a_built_area_header_registers_the_built_basis(monkeypatch, header):
    d = _workspace(monkeypatch, _table(["נכס", header], [["דירה", "500"]]))
    _take(d, _loc(row="דירה", column_number=2), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.provenance["area_basis"] == "source" and meaning.basis_key(v.area_basis) == "בנוי"
    assert v.read.get("area_basis") is True and v.read.get("period") is True and v.read.get("vat") is True


def test_f1_a_cell_on_a_page_the_header_may_not_reach_has_no_complete_read_of_its_qualifiers(monkeypatch):
    table = _table(["נכס", "שווי מ\"ר מבונה"], [["דירה", "500"]])
    d = _workspace(monkeypatch, table)
    taken = T._cell_of(d.ws.sources["S1"], d.ws.sources["S1"].text, _loc(row="דירה", column_number=2), table, 0, "", unconfirmed={1: "כותרת"})
    assert taken["read"] == {"kind": False, "unit": False, "period": False, "area_basis": False, "vat": False}


def test_f1_the_whole_verification_keeps_the_sentence_and_does_not_write_a_not_as_in_the_source_line():
    from tests.unit.test_verify_edit_ladder import _report

    ws = _per_sqm()  # older value: no basis, no read flags
    a, r = _report(BUILT_CLAIM, ws)
    assert r.removed_units() == set() and r.removals() == [] and not r.qualifier_edits
    assert "הוסרה טענה" not in r.apply(a).answer_markdown
    ws2 = _per_sqm("source", "מ\"ר ברוטו")  # the source's own header says gross: the built claim is wrong
    a2, r2 = _report(BUILT_CLAIM, ws2)
    assert r2.removed_units() == {0} and r2.qualifier_edits == {} and [p.failure_kind for p in r2.problems] == ["wrong_unit"]


# --- follow-up 2: the OCR spellings of "מ״ר", and a basis compared per number against its own column ---------------------------------------------------

OCR_TABLE = 'קומה | יעוד | שטח עיקרי במ" ר | שווי מ" ר מבונה | סך הכל\nעליונות | מסחר/ משרדים | 4,969 | 500 ₪ | 2,480,000 ₪'
OCR_CLAIM = "הוערכו זכויות בקומות העליונות לשימוש מסחר/משרדים: 4,969 מ״ר עיקרי, לפי 500 ₪ למ״ר מבונה [S1]."


@pytest.mark.parametrize("spelling", ['מ"ר', 'מ" ר', 'מ "ר', 'מ " ר', "מ''ר", "מ'ר", "מ״ר", "מ׳׳ר", "מ” ר"])
def test_f2_every_spelling_of_the_square_metre_abbreviation_is_the_one_unit_word(spelling):
    assert meaning.units_attested(f"שטח עיקרי ב{spelling}") == {"sqm"}
    assert meaning._all(f"שווי {spelling} מבונה").keys("basis") == {"בנוי"}
    assert meaning._all(f"שטח {spelling} אקוו׳").keys("basis") == {"אקוו"}
    assert meaning.without_unit_marks(f"120 {spelling}").strip() == "120"
    assert len(meaning._norm(f"120 {spelling} בנוי")) == len(f"120 {spelling} בנוי".replace("״", '"').replace("׳", "'"))


@pytest.mark.parametrize("text", ["מרתף בנוי 120", "שווי מר מבונה", "המ ר מבונה", "מ' 120 ר"])
def test_f2_negative_other_words_are_not_the_square_metre_abbreviation(text):
    assert meaning._all(text).keys("basis") == set() and "sqm" not in meaning.units_attested(text)


def test_f2_each_number_of_a_row_gets_the_basis_of_its_own_column():
    assert meaning.number_qualifiers(OCR_TABLE, frozenset({"500"}), "500").keys("basis") == {"בנוי"}
    assert meaning.number_qualifiers(OCR_TABLE, frozenset({"4969"}), "4,969").keys("basis") == {"עיקרי"}


def test_f2_a_sentence_that_states_one_basis_per_number_as_the_columns_do_is_as_in_the_source():
    from tests.unit.test_verify_units import _ws

    ws = _ws(OCR_TABLE)
    unit = split_units(OCR_CLAIM)[0]
    assert [m for m in verify.check_meaning(unit, ws) if m.blocking] == []
    assert [p for p in verify.deterministic([unit], ws, "?") if p.kind == "claim" and p.removes_unit] == []


def test_f2_the_whole_verification_keeps_that_sentence():
    from tests.unit.test_verify_edit_ladder import _report
    from tests.unit.test_verify_units import _ws

    a, r = _report(OCR_CLAIM, _ws(OCR_TABLE))
    assert r.removed_units() == set() and r.qualifier_edits == {} and "הוסרה טענה" not in r.apply(a).answer_markdown


@pytest.mark.parametrize("claim, wrong", [
    ("4,969 מ״ר מבונה, לפי 500 ₪ למ״ר עיקרי", {"4,969", "500"}),     # the two bases swapped
    ("4,969 מ״ר עיקרי, לפי 500 ₪ למ״ר ברוטו", {"500"}),               # a basis no column states for the number
    ("4,969 מ״ר ברוטו, לפי 500 ₪ למ״ר מבונה", {"4,969"}),
    ("4,969 מ״ר מבונה, לפי 500 ₪ למ״ר מבונה", {"4,969"}),             # the built basis belongs to the price column only
])
def test_f2_negative_a_number_whose_own_column_states_another_basis_is_still_wrong(claim, wrong):
    from tests.unit.test_verify_units import _ws

    problems = [m for m in verify.check_meaning(split_units(f"זכויות: {claim} [S1].")[0], _ws(OCR_TABLE)) if m.blocking]
    assert {m.number for m in problems} == wrong


def test_f2_negative_in_a_table_of_one_basis_another_basis_is_still_wrong():
    from tests.unit.test_verify_units import _ws

    ws = _ws('קומה | שטח עיקרי במ" ר | שווי למ" ר | סך הכל\nעליונות | 4,969 | 500 ₪ | 2,480,000 ₪')
    wrong = [m for m in verify.check_meaning(split_units("זכויות: 4,969 מ״ר עיקרי, לפי 500 ₪ למ״ר מבונה [S1].")[0], ws) if m.blocking]
    assert [m.number for m in wrong] == ["500"]
    right = verify.check_meaning(split_units("זכויות: 4,969 מ״ר עיקרי, לפי 500 ₪ למ״ר [S1].")[0], ws)
    assert [m for m in right if m.blocking] == []


def test_f2_the_cell_taken_under_an_ocr_spelled_header_registers_the_built_basis(monkeypatch):
    d = _workspace(monkeypatch, _table(["קומה", 'שווי מ" ר מבונה'], [["עליונות", "500 ₪"]]))
    _take(d, _loc(row="עליונות", column_number=2), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.provenance["area_basis"] == "source" and meaning.basis_key(v.area_basis) == "בנוי" and v.provenance["unit"] == "source"


# --- follow-up 1 (R3, claim-datum-place link): an incomplete read still tries to ATTEST the attribute from the stored context -------------------------------

VAT_CLAIM = "שווי הנכס הוא 2,450,000 ₪, כולל מע\"מ [V1]."


def _vat_turn(note: str | None, *, read_in_full: bool, vat_note_status: str = "כוללים"):
    """A turn whose V1 asserts a VAT the quote does not state; the stored table's notes (the fake fetcher's ``footnotes``) say ``note`` or nothing."""
    from app.chat.verify import Expansion, verify_answer
    from tests.unit.test_verify_reread import FakeFetcher
    from tests.unit.test_verify_units import _answer, _judge

    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat="included", provenance={"vat": "model_asserted"})
    if not read_in_full:
        ws.values["V1"].read = {}  # a value taken by older code: its read of the VAT is not complete
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section="סעיף 3", location="סעיף 3", kind="table",
                  text="שווי הנכס: 2,450,000 ₪", table_index=0, block_start=2, block_end=2)
    ws.values["V1"].source_id = "S1"
    fetcher = FakeFetcher({("S1", "footnotes"): Expansion(note, "הערות")} if note else {}, ws=ws)
    answer = _answer(VAT_CLAIM)
    report = verify_answer(_judge(lambda t: "supported"), answer, ws, "?", [], reread=fetcher)
    return ws, answer, report, fetcher


@pytest.mark.parametrize("note", ["הערה: כל הסכומים כולל מע\"מ.", "(*) כולל מע\"מ"])
def test_f1_an_incomplete_read_that_the_stored_notes_complete_cites_the_notes_and_edits_nothing(note):
    ws, answer, report, fetcher = _vat_turn(note, read_in_full=False)
    cites = [p.cite for p in report.problems if p.kind == "needs_citation"]
    assert len(cites) == 1 and cites[0] in ws.sources and "מע\"מ" in ws.sources[cites[0]].text and ws.sources[cites[0]].server_made
    out = report.apply(answer).answer_markdown
    assert report.qualifier_edits == {} and report.removed_units() == set() and "כולל מע\"מ" in out and f"[{cites[0]}]" in out, out
    assert fetcher.calls and fetcher.calls[0] == ("S1", "table_headers")


@pytest.mark.parametrize("note", [None, "הערה: הנתונים נכונים למועד הקובע.", "הערה: מספר השורות בטבלה נמוך."])
def test_f1_an_incomplete_read_that_nothing_attests_keeps_the_sentence_with_no_edit_and_no_claim_about_the_source(note):
    ws, answer, report, fetcher = _vat_turn(note, read_in_full=False)
    out = report.apply(answer).answer_markdown
    assert report.qualifier_edits == {} and report.removed_units() == set() and report.removals() == [], out
    assert "כולל מע\"מ" in out and "לא צוין במקור" not in out and "הוסר" not in out and "הוסרה" not in out, out
    assert not [p for p in report.problems if p.kind == "needs_citation"] and list(ws.sources) == ["S1"]  # nothing registered
    assert not any(p.unconfirmed for p in report.problems) and fetcher.calls  # the context was read, and the unconfirmed question dropped


def test_f1_negative_controls_a_complete_read_or_notes_that_state_the_other_status_keep_the_edit_path():
    # the read was complete and nothing states the VAT: the qualifier is edited out, as before
    ws, answer, report, _ = _vat_turn("הערה: הנתונים נכונים למועד הקובע.", read_in_full=True)
    assert report.qualifier_edits or report.removed_units() == {0}
    assert "כולל מע\"מ" not in report.apply(answer).answer_markdown
    # incomplete read, but the notes state the OTHER status: they are the evidence, the claim does not stand
    ws, answer, report, _ = _vat_turn("הערה: כל הסכומים בטבלה אינם כוללים מע\"מ.", read_in_full=False)
    assert report.qualifier_edits or report.removed_units() == {0}
    assert "כולל מע\"מ" not in report.apply(answer).answer_markdown
