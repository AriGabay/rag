"""Round 23 U6 (M8; R3, R8, R19, R20): ``take_value`` does not refuse a value it can read honestly.

Two traced causes of refusal, both on invented texts and tables:

- the verbatim test: a PDF's stored text splits a word with a line break, extra spaces or a hyphenated wrap, and the agent's quote joins it.
  The comparison is tolerant of whitespace and a wrap hyphen inside a word, and maps back to the EXACT positions of the source's own words
  (the citation is never a shifted offset); a word or a number that differs is still refused, and a space between two digits is never closed;
- the unit of a cell: ranked cell > all header rows (the leaf header, the unit row under it) > row label > caption; a percentage inside a row's
  NAME is no unit of its cell; the area word of a money column is its denominator; a header that conflicts with another header row registers
  the value as ambiguous (the existing flag), never a refusal. A percent cell is percent and a "אלפי ₪" header is thousands of shekels.

Database-free."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.chat import anchors, meaning
from app.chat import tools as T
from tests.unit.test_take_value_resolve import _loc, _table, _take, _workspace

DOC, VER = uuid.UUID(int=11), uuid.UUID(int=12)


def _source(text: str, **kw) -> T.Source:
    base = {"sid": "S1", "document_id": DOC, "version_id": VER, "title": "שומה סינתטית", "section": "5", "location": "עמוד 3",
            "kind": "context", "text": text, "block_start": 0, "block_end": 0, "reading_id": "r1", "status": "complete"}
    return T.Source(**(base | kw))


# --- the shared tolerant comparison: whitespace and a wrap hyphen inside a word ------------------------------------------------------------

@pytest.mark.parametrize("text, quote", [
    ("הערכת שווי ה\nשוק של הנכס", "שווי השוק"),            # a line break inside a word
    ("הערכת שווי  השוק של הנכס", "שווי השוק"),            # a run of spaces
    ("הערכת שווי הש-\nוק של הנכס", "שווי השוק"),          # a hyphenated wrap
    ("הערכת שו\nוי ה שוק של הנכס", "שווי השוק"),          # two splits, the split position varied
    ("דמי ניהול\n  חודשיים נקבעו", "דמי ניהול חודשיים"),  # between words: as before
    ("דמי ניהול חודשיים נקבעו", "דמיניהול חודשיים"),       # the same letters with a space missing in the quote
])
def test_a_quote_whose_source_splits_a_word_is_found_at_the_source_words(text, quote):
    (span,) = meaning.loose_spans(text, quote)
    found = text[span[0]:span[1]]
    assert found[0] == quote[0] and found[-1] == quote[-1] and found.replace("\n", "").replace(" ", "").replace("-", "") \
        == quote.replace(" ", "")


@pytest.mark.parametrize("text, quote", [
    ("הערכת שווי הסוק של הנכס", "שווי השוק"),             # another word
    ("הערכת שווי השוק של הנכס", "שווי השוקק"),            # another letter
    ("שווי 1 2500 ₪", "שווי 12500 ₪"),                    # a space between digits is never closed
    ("שווי 12,500 ₪", "שווי 12,600 ₪"),                   # another number
    ("שווי -5", "שווי 5"),                                 # a sign is not a wrap hyphen
    ("שווי השוק", ""),
])
def test_a_quote_whose_words_or_numbers_differ_is_not_found(text, quote):
    assert meaning.loose_spans(text, quote) == []


def test_the_exact_quote_is_found_where_it_is_and_every_occurrence_is_listed():
    text = "שווי השוק 5. שווי  השוק 6. שווי ה\nשוק 7."
    assert [text[a:b] for a, b in meaning.loose_spans(text, "שווי השוק")] == ["שווי השוק", "שווי  השוק", "שווי ה\nשוק"]
    assert meaning.loose_contains(text, "שווי השוק") and not meaning.loose_contains(text, "שווי השוקים")


# --- take_value: the quote ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("split", ["שווי ה\nשוק", "שווי  השוק", "שווי הש-\nוק", "שווי\nה\nשוק", "שו\nוי השוק"])
def test_a_quote_the_source_splits_is_taken_with_the_exact_citation_positions(split):
    full = f"הערכה.\n{split} של הנכס נקבע ל-4,200,000 ₪ לפי חוות הדעת."
    src = _source(full)
    taken = T._take_quote(src, full, {"quote": "שווי השוק של הנכס נקבע ל-4,200,000 ₪", "number": "4,200,000"}, [(0, full, 3)])
    assert taken["value"] == Decimal("4200000") and taken["units"] == {"ILS"}
    (seg,) = taken["anchor"]["segments"]
    cited = full[seg[1]:seg[2]]
    assert cited == f"{split} של הנכס נקבע ל-4,200,000 ₪"  # the source's own words, from its first letter to its last
    block, a, b = taken["anchor"]["number"]
    assert block == 0 and full[a:b] == "4,200,000"
    assert taken["quote"] in meaning._flat(full) and taken["locator"]["quote"] == taken["quote"]  # stored as the source writes it


def test_a_quote_split_between_two_blocks_cites_each_block_at_its_own_words():
    first, second = "פרק ד. שווי ה", "שוק של הנכס נקבע ל-4,200,000 ₪."
    full = first + "\n" + second
    src = _source(full, block_start=0, block_end=1)
    taken = T._take_quote(src, full, {"quote": "שווי השוק של הנכס נקבע ל-4,200,000 ₪", "number": "4,200,000"},
                          [(0, first, 3), (1, second, 3)])
    (a, b) = taken["anchor"]["segments"]
    assert a[0] == 0 and first[a[1]:a[2]] == "שווי ה" and b[0] == 1 and second[b[1]:b[2]] == "שוק של הנכס נקבע ל-4,200,000 ₪"
    block, s, e = taken["anchor"]["number"]
    assert block == 1 and second[s:e] == "4,200,000"


def test_a_quote_whose_word_really_differs_is_still_refused():
    full = "שווי ה\nשוק של הנכס נקבע ל-4,200,000 ₪."
    with pytest.raises(T.ToolError, match="אינו מופיע כלשונו"):
        T._take_quote(_source(full), full, {"quote": "שווי הקרקע של הנכס נקבע ל-4,200,000 ₪", "number": "4,200,000"}, [(0, full, 3)])


def test_a_quote_with_another_number_than_the_split_source_is_still_refused():
    full = "שווי ה\nשוק של הנכס נקבע ל-4,200,000 ₪."
    with pytest.raises(T.ToolError):
        T._take_quote(_source(full), full, {"quote": "שווי השוק של הנכס נקבע ל-4,300,000 ₪", "number": "4,300,000"}, [(0, full, 3)])


def test_a_number_the_source_writes_with_a_space_is_not_closed_by_the_quote():
    full = "העלות נקבעה ל-1 250 ₪."
    with pytest.raises(T.ToolError):
        T._take_quote(_source(full), full, {"quote": "העלות נקבעה ל-1250 ₪", "number": "1250"}, [(0, full, 3)])


def test_locate_quote_maps_a_split_word_to_exact_token_positions():
    block = "תחילת הפסקה. שווי ה\nשוק הוא 9,500 ₪ למ\"ר. סוף."
    found = anchors.locate_quote([(7, block, 2)], "שווי השוק הוא 9,500", Decimal("9500"))
    assert found is not None and found.blocks == [7]
    (seg,) = found.segments
    assert block[seg[1]:seg[2]] == "שווי ה\nשוק הוא 9,500"
    assert block[found.number[1]:found.number[2]] == "9,500"


def test_locate_quote_still_finds_an_unsplit_quote_and_nothing_for_a_different_word():
    block = "השווי הוא 9,500 ₪."
    found = anchors.locate_quote([(0, block, 1)], "השווי הוא 9,500", Decimal("9500"))
    assert found is not None and block[found.segments[0][1]:found.segments[0][2]] == "השווי הוא 9,500"
    assert anchors.locate_quote([(0, block, 1)], "ההערכה היא 9,500", Decimal("9500")) is None


# --- take_value: the unit of a cell --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["תשואה 7%", "היוון 6.5%", "ריבית צמודה 4.25% לשנה", "נכס (תשואה 8%)"])
@pytest.mark.parametrize("cell, headers", [("1,200,000 ₪", ["סעיף", "סכום"]), ("1,200,000", ["סעיף", "סכום (₪)"]),
                                           ("1,200,000", ["סעיף", "סכום"])])
def test_a_percentage_in_the_row_label_is_not_the_unit_of_a_currency_cell(monkeypatch, label, cell, headers):
    d = _workspace(monkeypatch, _table(headers, [[label, cell], ["שורה אחרת", "5"]]))
    _take(d, _loc(row=label, column=headers[1]), kind="value", unit="ILS")
    v = d.ws.values["V1"]
    assert v.unit == "ILS" and "V1" not in d.ws.ambiguous_units
    assert v.provenance["unit"] == ("source" if cell != "1,200,000" or "₪" in headers[1] else "model_asserted")


def test_a_percent_cell_is_percent_whatever_the_row_label_says(monkeypatch):
    d = _workspace(monkeypatch, _table(["סעיף", "ערך"], [["תשואה 7%", "6.5%"], ["שכירות", "9,000 ₪"]]))
    _take(d, _loc(row="תשואה 7%", column="ערך"), kind="rate", unit="percent")
    assert d.ws.values["V1"].unit == "percent" and d.ws.values["V1"].provenance["unit"] == "source"
    with pytest.raises(T.ToolError, match="יחידה"):
        _take(d, _loc(row="תשואה 7%", column="ערך"), kind="value", unit="ILS")


def test_a_percent_sign_that_is_the_labels_unit_mark_still_gives_the_cell_percent(monkeypatch):
    d = _workspace(monkeypatch, _table(["סעיף", "ערך"], [["שיעור היוון (%)", "6.5"]]))
    _take(d, _loc(row="שיעור היוון (%)", column="ערך"), kind="rate", unit="percent")
    assert d.ws.values["V1"].unit == "percent" and d.ws.values["V1"].provenance["unit"] == "source"
    with pytest.raises(T.ToolError):
        _take(d, _loc(row="שיעור היוון (%)", column="ערך"), kind="rate", unit="ILS")


def test_a_header_saying_thousands_of_shekels_is_thousands_for_a_cell_with_no_unit(monkeypatch):
    d = _workspace(monkeypatch, _table(["סעיף", "סכום (באלפי ₪)"], [["שווי לפי היוון 7%", "1,200"]]))
    _take(d, _loc(row="שווי לפי היוון 7%", column="סכום (באלפי ₪)"), kind="value", unit="ILS")
    v = d.ws.values["V1"]
    assert v.unit == "ILS" and v.scale == 1000 and v.meaning_from["unit"] == "header"


@pytest.mark.parametrize("under", [["", "₪ למ\"ר"], ["יחידות", "₪ למ\"ר"], ["", "(₪ למ\"ר)"]])
@pytest.mark.parametrize("label", ["דירה 80 מ\"ר", "דירה ב'", "משרד 120 מ\"ר בקומה 3"])
def test_a_unit_in_the_second_header_row_is_the_cells_unit(monkeypatch, under, label):
    table = _table(["נכס", "שווי"], [under, [label, "9,500"], ["סה\"כ", "12"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row=label, column="שווי"), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.unit == "ILS_per_sqm" and v.provenance["unit"] == "source" and v.meaning_from["unit"] == "header"
    assert "V1" not in d.ws.ambiguous_units


def test_a_second_header_row_that_states_an_area_is_the_area(monkeypatch):
    table = _table(["נכס", "שטח"], [["", "מ\"ר"], ["דירה", "96"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="דירה", column="שטח"), kind="area", unit="sqm")
    assert d.ws.values["V1"].unit == "sqm" and d.ws.values["V1"].provenance["unit"] == "source"


def test_the_first_row_of_data_is_not_read_as_a_header_row(monkeypatch):
    table = _table(["נכס", "שווי"], [["דירה א", "9,500"], ["", "₪ למ\"ר"], ["דירה ב", "8,000"]])
    d = _workspace(monkeypatch, table)
    _take(d, _loc(row="דירה ב", column="שווי"), kind="value", unit="ILS")
    assert d.ws.values["V1"].provenance["unit"] == "model_asserted"  # the row below a number is not a header


@pytest.mark.parametrize("header", ["מחיר (מ\"ר)", "שווי מ\"ר", "מחיר במ\"ר מבונה", "מחיר ממוצע (מ\"ר)"])
@pytest.mark.parametrize("cell", ["9,500", "9,500 ₪"])
def test_the_area_word_of_a_money_column_is_its_denominator_not_its_unit(monkeypatch, header, cell):
    d = _workspace(monkeypatch, _table(["נכס", header], [["דירה", cell]]))
    _take(d, _loc(row="דירה", column=header), kind="value", unit="ILS_per_sqm")
    v = d.ws.values["V1"]
    assert v.unit == "ILS_per_sqm" and v.provenance["unit"] == "source"


def test_the_area_word_of_an_area_column_is_still_the_area(monkeypatch):
    d = _workspace(monkeypatch, _table(["נכס", "שטח (מ\"ר)"], [["דירה", "96"]]))
    _take(d, _loc(row="דירה", column="שטח (מ\"ר)"), kind="area", unit="sqm")
    assert d.ws.values["V1"].unit == "sqm"
    with pytest.raises(T.ToolError):
        _take(d, _loc(row="דירה", column="שטח (מ\"ר)"), kind="value", unit="ILS_per_sqm")


@pytest.mark.parametrize("pair", [("תשואה (%)", "₪"), ("שווי (₪)", "%")])
def test_header_rows_that_state_different_units_register_the_value_as_ambiguous_not_refused(monkeypatch, pair):
    top, under = pair
    table = _table(["נכס", top], [["", under], ["דירה", "12,000"]])
    d = _workspace(monkeypatch, table)
    out = _take(d, _loc(row="דירה", column=top), kind="value", unit="ILS")
    assert d.ws.values["V1"].unit == "ILS" and "V1" in d.ws.ambiguous_units
    assert T.MSG_UNIT_AMBIGUOUS_TAKEN.split("{")[0] in out


def test_a_cell_with_its_own_unit_outranks_a_conflicting_header_and_a_wrong_claim_is_refused(monkeypatch):
    d = _workspace(monkeypatch, _table(["נכס", "תשואה (%)"], [["דירה", "9,000 ₪"]]))
    with pytest.raises(T.ToolError, match="יחידה"):
        _take(d, _loc(row="דירה", column="תשואה (%)"), kind="rate", unit="percent")
    _take(d, _loc(row="דירה", column="תשואה (%)"), kind="value", unit="ILS")
    assert d.ws.values["V1"].unit == "ILS" and d.ws.values["V1"].meaning_from["unit"] == "cell"
