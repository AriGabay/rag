"""A unit that cites a registered value (V#) is checked against the numbers its cited sources state, and a V# states the text
it was registered from: the row of the table whose cell it is (or the paragraph of its quote), not only the value (round 15,
R10/R11 spirit; KD3). A table row rendered with a V# for its price keeps the floor number from the same source row; a number
that is in neither that row nor a cited source is still removed. Synthetic text only."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.chat import calc, verify
from app.chat.tools import Workspace
from app.chat.verify import split_units

PROV = {"unit": "source", "vat": "source"}
TABLE = ("שווי הדירות\n"
         "דירה | קומה | שווי\n"
         "דירה אלף | 3 | 1,761,000\n"
         "דירה בית | 4 | 1,802,000")
PROSE = "בחוות הדעת נקבע כי השווי של הדירה הוא 2,450,000 ₪ לפי 4 חדרים.\nבפסקה אחרת נאמר כי נבנו 12 דירות."


def _ws(text: str = TABLE, other: str | None = None) -> Workspace:
    ws = Workspace(ctx=None)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="טבלה",
                  location="טבלה 1", kind="context", text=text)
    if other is not None:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח אחר", section="סיכום",
                      location="סיכום", kind="context", text=other)
    return ws


def _cell(ws: Workspace, written: str, row: str, quote: str | None = None) -> str:
    """A V# registered from a table cell whose quote is the cell alone (the row's other cells are in the source)."""
    ws.values["V1"] = calc.Value("V1", Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(), None,
                                 "דוח בדיקה", "טבלה 1", "שווי", "value", "ILS", "none", "included", "", row,
                                 "appraiser_determination", PROV,
                                 {"table_index": 0, "row": row, "row_number": 1, "column": "שווי", "column_number": 3},
                                 quote or written)
    return "V1"


def _quote_value(ws: Workspace, written: str, quote: str) -> str:
    ws.values["V1"] = calc.Value("V1", Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(), None,
                                 "דוח בדיקה", "סעיף 2", "שווי", "value", "ILS", "none", "included", "", "הדירה",
                                 "appraiser_determination", PROV, {"quote": quote}, quote)
    return "V1"


def _unstated(text: str, ws: Workspace) -> list[str]:
    return [n for p in verify.deterministic(split_units(text), ws, "?") if p.check == "unstated_number"
            for n in p.unstated]


def test_a_table_row_citing_the_price_keeps_the_floor_number_of_the_same_source_row():
    ws = _ws()
    _cell(ws, "1,761,000", "דירה אלף")
    assert verify.deterministic(split_units("| דירה אלף | 3 | ₪1,761,000 [V1] |"), ws, "?") == []


def test_a_number_in_neither_the_values_source_row_nor_a_cited_source_is_still_removed():
    ws = _ws()
    _cell(ws, "1,761,000", "דירה אלף")
    assert _unstated("| דירה אלף | 7 | ₪1,761,000 [V1] |", ws) == ["7"]


def test_a_number_of_another_row_of_the_same_passage_is_not_the_values_row():
    ws = _ws()
    _cell(ws, "1,761,000", "דירה אלף")
    assert _unstated("| דירה אלף | 4 | ₪1,761,000 [V1] |", ws) == ["4"]


def test_a_number_only_an_uncited_source_states_is_still_removed():
    ws = _ws(other="בסיכום נאמר כי יש 9 קומות.")
    _cell(ws, "1,761,000", "דירה אלף")
    assert _unstated("| דירה אלף | 9 | ₪1,761,000 [V1] |", ws) == ["9"]


def test_a_quote_value_keeps_a_number_of_its_own_sentence_and_not_one_of_another_paragraph():
    ws = _ws(PROSE)
    _quote_value(ws, "2,450,000", "השווי של הדירה הוא 2,450,000 ₪")
    assert verify.deterministic(split_units("השווי הוא 2,450,000 ₪ [V1] לפי 4 חדרים."), ws, "?") == []
    assert _unstated("השווי הוא 2,450,000 ₪ [V1] מתוך 12 דירות.", ws) == ["12"]


def test_citing_the_passage_too_still_states_its_numbers():
    ws = _ws()
    _cell(ws, "1,761,000", "דירה אלף")
    assert verify.deterministic(split_units("| דירה בית | 4 | ₪1,761,000 [V1] [S1] |"), ws, "?") == []
