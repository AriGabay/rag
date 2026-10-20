"""Reproductions of the round-20 failure shapes that need no document, recorded failing on the round-19 build (round 20 U8: R1-R18;
AE1-AE5, AE9, AE10; KTD1, KTD2, KTD4, KTD5).

Each shape is a pure test: the ledger's merge (U1), the final check of a table row on the offline engine (U2), the ingestion
order of a Hebrew-free page (U4), and the coverage of an item (U5). The model and the judge are scripted, so a test proves what
the server does with an answer that is CORRECT for its fixture, not the judgement of a model. Each open case is marked
``xfail(strict=True, raises=AssertionError)`` with the unit that owns the fix and what fails, and the owning unit removes the
marker when its fix lands (a strict xfail that starts passing fails the suite, so the marker cannot be forgotten). A protection
test (a wrong datum that must still be removed or flagged, a handling that must stay) is no xfail: it passes on the round-19 build
and must keep passing. The shapes that need a document (attribution below the property) are in
``tests/integration/test_round20_sequences.py``. Synthetic text and values only; every name and figure is invented."""

# ruff: noqa: F811 - the tests take the imported ``offline`` fixture by name, as the round-13 and round-14 sequences do

from __future__ import annotations

import json
import time
from functools import cache
from pathlib import Path

import pytest

from app.chat.compose import text_digest
from app.config import get_settings
from app.extraction.pdf import extract_pdf
from tests.support.scripted_agent import ScriptedAgent, final
from tests.unit.test_final_check import (  # noqa: F401 - the fixture, taken by name
    SEARCH,
    Judge,
    _turn,
    offline,
)


def strict_xfail(unit: str, what: str):
    """The marker of an open case: it fails with an assertion on the round-19 build, for the reason ``what``, and ``unit`` owns the fix."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{unit}: {what}")


# --- 4-6. the final check of a table row (AE4, AE5; U2) -------------------------------------------------------------------------

TABLE_SOURCE = "השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש."
TABLE_INTRO = "הטבלה מציגה את הנתונים העיקריים של הנכס [S1]."
TABLE_HEAD = "| נתון בנכס | ערך | הערה |\n|---|---|---|\n"
NOTE = "הערכה שמרנית של המודל"
ROW_A = f"| השווי למ\"ר | 9,500 ₪ [S1] | {NOTE} |"
ROW_B = "| דמי השכירות | 55 ₪ למ\"ר לחודש [S1] | — |"
TABLE_QUESTION = "מהם השווי למ\"ר ודמי השכירות בנכס?"
FINAL_PASSES_ALLOWED = 3  # the answer is scripted again for each repair round the engine may take


def cells(line: str) -> list[str]:
    """The cells of a Markdown table row."""
    return [c.strip() for c in line.strip().strip("|").split("|")]


def table_turn(offline, rule, answer: str):
    offline.texts = [TABLE_SOURCE]
    return _turn(ScriptedAgent([SEARCH, *[final(answer) for _ in range(FINAL_PASSES_ALLOWED)]], judge=Judge(rule)), TABLE_QUESTION)


def line_with(md: str, needle: str) -> str:
    found = [ln for ln in md.splitlines() if needle in ln]
    assert len(found) == 1, (needle, md)
    return found[0]


def removed_intro_rule(row_a_reply: dict):
    """A judge that finds the line above the table unsupported (it is removed), supports every unit the first time it reads it, and
    judges row A as ``row_a_reply`` once the line above its table is gone (the key of the row holds that line, so the final check
    reads the row again)."""

    def rule(text, input, n):
        if text.startswith("הטבלה מציגה"):
            return {"verdict": "unsupported", "failure": "absent_from_source"}
        if NOTE in text and "<table_intro>" not in input:
            return row_a_reply
        return "supported"

    return rule


@strict_xfail("U2", "row A is removed whole by the final check although only its note cell is unsupported: the whole row is judged again because the line above the table went")
def test_ae4_a_row_with_one_unsupported_note_cell_keeps_its_supported_cells_when_the_line_above_the_table_is_removed(offline):
    reply = {"verdict": "partial", "defect": "part", "claims": [
        {"span": "9,500 ₪", "verdict": "supported"},
        {"span": NOTE, "verdict": "unsupported", "failure": "absent_from_source"}]}
    out = table_turn(offline, removed_intro_rule(reply), f"{TABLE_INTRO}\n{TABLE_HEAD}{ROW_A}\n{ROW_B}")
    md = out.answer.answer_markdown
    assert "9,500" in md and "55 ₪" in md, md  # both rows stay
    assert NOTE not in md, md  # the note the sources do not hold is cut
    assert len(cells(line_with(md, "9,500"))) == len(cells(TABLE_HEAD.splitlines()[0])) == 3, md  # the columns stay aligned
    assert out.final_digest == text_digest(md)


@strict_xfail("U2", "the partial mark of a table row is written after the closing bar, as an extra cell, instead of inside the row's last cell")
def test_a_partly_verified_table_row_gets_its_mark_inside_the_last_cell_and_no_extra_cell(offline):
    def rule(text, input, n):
        return {"verdict": "partial", "defect": "none"} if NOTE in text else "supported"

    out = table_turn(offline, rule, f"{TABLE_HEAD}{ROW_A}\n{ROW_B}")
    md = out.answer.answer_markdown
    row = line_with(md, "9,500")
    assert "אומת חלקית" in row, md
    assert len(cells(row)) == 3, md  # the mark is no fourth cell
    assert "אומת חלקית" in cells(row)[-1] and row.rstrip().endswith("|"), md
    assert out.report.removals() == []


def test_protection_ae5_a_row_whose_key_cell_names_the_wrong_unit_is_still_removed_whole_by_the_final_check(offline):
    """The remaining cells would describe the wrong unit: the row goes with its key cell, however supported the figure is."""
    wrong = "| השווי למ\"ר בדירה האחורית | 9,500 ₪ [S1] | הערה |"
    reply = {"verdict": "partial", "defect": "part", "claims": [
        {"span": "בדירה האחורית", "verdict": "unsupported", "failure": "wrong_subject"},
        {"span": "9,500 ₪", "verdict": "supported"}]}

    def rule(text, input, n):
        if text.startswith("הטבלה מציגה"):
            return {"verdict": "unsupported", "failure": "absent_from_source"}
        if "בדירה האחורית" in text and "<table_intro>" not in input:  # judged again once the line above the table is gone
            return reply
        return "supported"

    out = table_turn(offline, rule, f"{TABLE_INTRO}\n{TABLE_HEAD}{wrong}\n{ROW_B}")
    md = out.answer.answer_markdown
    assert "9,500" not in md and "בדירה האחורית" not in md, md
    assert "55 ₪" in md, md  # the row of the asked unit stays
    gone = [p for p in out.report.removals() if "בדירה האחורית" in p.unit.text]
    assert [p.check for p in gone] == ["final"], md


@strict_xfail("U2", "a key cell the judge finds unsupported in the draft verification is cut to the marker and the row stays: its figure then stands under no label")
def test_ae5_a_key_cell_the_draft_judge_finds_unsupported_removes_the_row_whole_and_does_not_leave_its_figure_under_a_marker(offline):
    wrong = "| השווי למ\"ר בדירה האחורית | 9,500 ₪ [S1] | הערה |"

    def rule(text, input, n):
        if "בדירה האחורית" in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": "בדירה האחורית", "verdict": "unsupported", "failure": "wrong_subject"},
                {"span": "9,500 ₪", "verdict": "supported"}]}
        return "supported"

    out = table_turn(offline, rule, f"{TABLE_HEAD}{wrong}\n{ROW_B}")
    md = out.answer.answer_markdown
    assert "9,500" not in md, md
    assert "55 ₪" in md, md


# --- 1-3. the item ledger (AE1-AE3; U1) ---------------------------------------------------------------------------------------

PAYMENTS = ("התשלום הראשון בסך 400,000 ₪ ישולם ביום 01.03.2027 והתשלום השני בסך 650,000 ₪ ישולם ביום 01.09.2027. "
            "יתרת התמורה תשולם במועד המסירה.")
FIRST_PAYMENT = "התשלום הראשון בסך 400,000 ₪ ישולם ביום 01.03.2027"
SECOND_PAYMENT = "התשלום השני בסך 650,000 ₪ ישולם ביום 01.09.2027"
PAYMENTS_SENTENCE = f"{FIRST_PAYMENT} והתשלום {SECOND_PAYMENT.removeprefix('התשלום ')}"
RIGHTS_SENTENCE = ("השטח העיקרי בנכס הוא 1,200 מ״ר רק על פי תכנית מאושרת, ובמצב מוצע השטח העיקרי הוא 1,200 מ״ר רק לאחר פינוי המגרש.")
APPROVED_CLAUSE = "השטח העיקרי בנכס הוא 1,200 מ״ר רק על פי תכנית מאושרת"
EVACUATION_CLAUSE = "השטח העיקרי הוא 1,200 מ״ר רק לאחר פינוי המגרש"
LEDGER_COMPONENTS = [{"id": "N1", "text": "התשלומים וזכויות הבנייה בנכס"}]


def declared(ws, markdown: str, ledger, *raw: dict, version: int = 1):
    """One answer version's declarations validated and merged into the turn's ledger, as the verification does."""
    from app.chat import items as ledger_items
    from app.chat.verify import split_units

    res = ledger_items.validate(list(raw), ws, split_units(markdown), LEDGER_COMPONENTS, ledger=ledger)
    assert res.excluded == [], res.excluded
    ledger.merge(res.kept, excluded=res.excluded, version=version, misattributed=res.misattributed)
    return res.kept


def fill(label: str, quote: str, value: str, condition: str = "", state: str = "included", reason: str = "") -> dict:
    return {"component": "N1", "label": label, "source": "S1", "quote": quote, "value": value, "condition": condition,
            "state": state, "reason": reason}


def ledger_ws(text: str):
    import uuid

    from app.chat.tools import Workspace

    ws = Workspace(ctx=None)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="תשלומים", location="סעיף 4",
                  kind="context", text=text)
    return ws


@pytest.mark.parametrize("narrow_first", [True, False], ids=["narrow-first", "wide-first"])
def test_ae1_two_payments_in_one_quoted_sentence_declared_with_nested_quotes_in_one_batch_stay_two_items(narrow_first):
    from app.chat import items as ledger_items

    ws = ledger_ws(PAYMENTS)
    first = fill("התשלום הראשון", FIRST_PAYMENT, "400,000 ₪ ביום 01.03.2027")
    second = fill("התשלום השני", PAYMENTS_SENTENCE, "650,000 ₪ ביום 01.09.2027")
    ledger = ledger_items.Ledger()
    declared(ws, "התשלום הראשון הוא 400,000 ₪ [S1].", ledger, *((first, second) if narrow_first else (second, first)))
    got = sorted((i.label, i.value) for i in ledger.items.values())
    assert got == [("התשלום הראשון", "400,000 ₪ ביום 01.03.2027"), ("התשלום השני", "650,000 ₪ ביום 01.09.2027")], got


def test_ae2_a_not_applicable_decision_on_one_of_two_items_with_the_same_value_is_not_carried_to_the_other():
    from app.chat import items as ledger_items

    ws = ledger_ws(RIGHTS_SENTENCE)
    closed = fill("השטח העיקרי לפי תכנית מאושרת", APPROVED_CLAUSE, "1,200 מ״ר", "רק על פי תכנית מאושרת", "not_applicable",
                  "התכנית המאושרת אינה חלה על הבקשה")
    open_item = fill("השטח העיקרי לאחר פינוי", RIGHTS_SENTENCE, "1,200 מ״ר", "רק לאחר פינוי המגרש")
    ledger = ledger_items.Ledger()
    declared(ws, "השטח העיקרי הוא 1,200 מ״ר [S1].", ledger, closed, open_item)
    got = sorted((i.condition, i.state) for i in ledger.items.values())
    assert got == [("רק לאחר פינוי המגרש", "included"), ("רק על פי תכנית מאושרת", "not_applicable")], got


CONDITIONAL_ANSWER = f"{APPROVED_CLAUSE} [S1]."


@pytest.mark.parametrize("second_quote", [APPROVED_CLAUSE, RIGHTS_SENTENCE.split(", ובמצב")[0]], ids=["same-quote", "narrower-quote"])
def test_a_condition_the_second_version_adds_to_an_item_the_first_declared_without_one_leaves_one_open_item_and_no_gap(second_quote):
    """Guard for U1 (it passes on the round-19 build, where the quotes of one place merge): the stricter gate must keep a corrected
    or completed declaration of the same datum from leaving the first version's item open as a gap line."""
    from app.chat import items as ledger_items
    from app.chat.verify import split_units

    ws = ledger_ws(RIGHTS_SENTENCE)
    ledger = ledger_items.Ledger()
    declared(ws, CONDITIONAL_ANSWER, ledger, fill("השטח העיקרי בנכס", RIGHTS_SENTENCE.split(", ובמצב")[0], "1,200 מ״ר"), version=1)
    declared(ws, CONDITIONAL_ANSWER, ledger, fill("השטח העיקרי בנכס", second_quote, "1,200 מ״ר", "רק על פי תכנית מאושרת"), version=2)
    rows = ledger.outcomes(split_units(CONDITIONAL_ANSWER), ws)
    assert [r for r in rows if r["state"] == "omitted"] == [], [(r["id"], r["state"]) for r in rows]
    assert [r["condition"] for r in rows if r["state"] == "covered"] == ["רק על פי תכנית מאושרת"]


def test_ae3_a_later_version_that_rewords_an_item_with_a_narrower_quote_of_the_same_datum_leaves_one_item_with_its_history():
    from app.chat import items as ledger_items

    ws = ledger_ws(PAYMENTS)
    ledger = ledger_items.Ledger()
    declared(ws, "התשלום הראשון הוא 400,000 ₪ [S1].", ledger, fill("התשלום הראשון", PAYMENTS_SENTENCE.split(" והתשלום")[0], "400,000 ₪"), version=1)
    declared(ws, "התשלום הראשון הוא 400,000 ₪ [S1].", ledger, fill("תשלום ראשון בתמורה", "בסך 400,000 ₪ ישולם ביום 01.03.2027", "400,000 ₪"), version=2)
    (only,) = ledger.items.values()
    assert only.label == "תשלום ראשון בתמורה" and [h["version"] for h in only.history] == [1, 2]


# --- 12. the order of a line and a table on a page with no Hebrew, inside a Hebrew file written in visual order (AE9; U4) --------

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@cache
def _manifest() -> dict:
    return json.loads((FIXTURES / "round20" / "manifest.json").read_text(encoding="utf-8"))["documents"]


@cache
def _visual_file():
    """The Hebrew-visual fixture as the pipeline would read it (no OCR): page 1 and 3 hold Hebrew, page 2 only English and numbers,
    page 4 only numbers."""
    path = FIXTURES / _manifest()["hebrew_visual"]["file"]
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings())


NUMBER_LINE = "1,200 1,450 1,600"


@strict_xfail("U4", "a line of numbers on a page with no Hebrew is reversed when the file's default orientation is visual: fix_line reverses a Hebrew-free line without Latin letters")
def test_ae9_a_number_only_line_on_a_page_with_no_hebrew_keeps_its_stored_left_to_right_order():
    from app.extraction.hebrew import fix_text_lines

    assert fix_text_lines([NUMBER_LINE], default_visual=True) == [NUMBER_LINE]
    assert fix_text_lines(["Quarterly revenue by property", NUMBER_LINE], default_visual=True)[1] == NUMBER_LINE


@strict_xfail("U4", "the stored page and block of the Hebrew-free page hold the number line reversed (1,600 1,450 1,200): orient_pages applies the file's visual orientation to it")
def test_ae9_the_stored_page_and_block_of_a_hebrew_free_page_hold_the_number_line_as_drawn():
    result = _visual_file()
    page = next(p for p in result.pages if p.page_no == 2)
    assert NUMBER_LINE in page.text.splitlines(), page.text
    assert NUMBER_LINE in [b.text for b in result.blocks if b.page == 2], [b.text for b in result.blocks if b.page == 2]


def test_protection_ae9_the_same_number_line_on_a_page_with_hebrew_keeps_todays_handling():
    from app.extraction.hebrew import fix_text_lines

    assert fix_text_lines(["ןג תמר :ריע", NUMBER_LINE], default_visual=True) == ["עיר: רמת גן", "1,600 1,450 1,200"]


@strict_xfail("U4", "the columns of a table with Latin headers on a page with no Hebrew are reversed (logical_row reverses every text-layer table): the first column is read last")
def test_a_table_with_latin_headers_on_a_page_with_no_hebrew_keeps_its_columns_in_box_order():
    result = _visual_file()
    table = next(t for t in result.tables if t.page_start == 2)
    spec = _manifest()["hebrew_visual"]
    rows = ([t for t in [table.headers] if t] or []) + [r.cells for r in table.rows]
    assert rows == [spec["english_headers"], *spec["english_rows"]], rows
    block = next(b for b in result.blocks if b.kind == "table" and b.page == 2)
    assert block.text.splitlines()[0] == " | ".join(spec["english_headers"]), block.text


def test_protection_a_hebrew_line_and_a_hebrew_table_of_the_same_file_keep_their_logical_order():
    result = _visual_file()
    spec = _manifest()["hebrew_visual"]
    first = next(p for p in result.pages if p.page_no == 1)
    assert first.text.splitlines()[:4] == spec["hebrew_lines"], first.text
    table = next(t for t in result.tables if t.page_start == 3)
    assert table.headers == spec["hebrew_headers"] and [r.cells for r in table.rows] == spec["hebrew_rows"]


def test_protection_a_table_of_numbers_only_with_no_left_to_right_cue_keeps_todays_handling():
    """The columns of a numbers-only table are not decided by the page having no Hebrew letter: the round-19 reading (columns
    reversed) stands until a cue says otherwise."""
    result = _visual_file()
    spec = _manifest()["hebrew_visual"]
    table = next(t for t in result.tables if t.page_start == 4)
    assert [r.cells for r in table.rows] == [list(reversed(r)) for r in spec["numbers_rows"]]


# --- 13-15. gap lines and the completeness of a component (AE10; U5) ----------------------------------------------------------

def test_protection_an_item_the_answer_does_not_state_still_gets_its_read_and_not_used_line_naming_its_place():
    from tests.unit.test_coverage import MAIN_TWO_SENTENCES, ROOF_FIGURE, _finish, _item_lines, _rights_check

    ws, r, turn, answer, _ = _rights_check(MAIN_TWO_SENTENCES + "\n" + ROOF_FIGURE)
    final, outcomes = _finish(ws, answer, r, turn)
    assert outcomes[0]["status"] == "partial" and outcomes[0]["omitted_items"] == ["I2"]
    (line,) = _item_lines(final)
    assert line.startswith("«שטח מרתף»") and "S2" in line and "לא נעשה בו שימוש" in line


def test_the_same_twin_items_are_covered_when_the_answer_writes_the_compound_with_a_hyphen():
    """The control of the maqaf case below: only the character that joins the compound differs."""
    from tests.unit.test_items import _item, _states, _ws

    ws = _ws("\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בצפון־מזרח | 7%", "חנויות בדרום־מערב | 7%"]))
    north = _item(label="שיעור למשרדים", quote="משרדים בצפון־מזרח | 7%", value="7%", condition="משרדים בצפון-מזרח")
    south = _item(label="שיעור לחנויות", quote="חנויות בדרום־מערב | 7%", value="7%", condition="חנויות בדרום-מערב")
    got = _states(ws, "- השיעור למשרדים בצפון-מזרח הוא 7% [S1].\n- השיעור לחנויות בדרום-מערב הוא 7% [S1].", north, south)
    assert {k: o["state"] for k, o in got.items()} == {"משרדים בצפון־מזרח": "covered", "חנויות בדרום־מערב": "covered"}, got


@strict_xfail("U5", "an item whose condition is a direction word the source writes with a maqaf stays omitted: the item's words are split at the maqaf and the answer's block words are not")
def test_twin_items_told_apart_by_a_direction_word_the_answer_writes_as_a_maqaf_compound_are_covered():
    from tests.unit.test_items import _item, _states, _ws

    ws = _ws("\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בצפון־מזרח | 7%", "חנויות בדרום־מערב | 7%"]))
    north = _item(label="שיעור למשרדים", quote="משרדים בצפון־מזרח | 7%", value="7%", condition="משרדים בצפון־מזרח")
    south = _item(label="שיעור לחנויות", quote="חנויות בדרום־מערב | 7%", value="7%", condition="חנויות בדרום־מערב")
    got = _states(ws, "- השיעור למשרדים בצפון־מזרח הוא 7% [S1].\n- השיעור לחנויות בדרום־מערב הוא 7% [S1].", north, south)
    assert {k: o["state"] for k, o in got.items()} == {"משרדים בצפון־מזרח": "covered", "חנויות בדרום־מערב": "covered"}, got


@strict_xfail("U5", "a count the answer writes as a word (שתי קומות) is not the digit the source writes (2): the item is partial and its line says the floors were left out")
def test_a_count_written_as_a_word_before_the_noun_the_source_writes_after_the_digit_is_given():
    from tests.unit.test_items import BASEMENT, RIGHTS, _item, _states, _ws

    ws = _ws(RIGHTS)
    got = _states(ws, "- שטח המרתף בנכס הוא 340 מ״ר, בשתי קומות [S1].", _item(**BASEMENT))["שטח מרתף"]
    assert got["state"] == "covered" and got["outcome"] == "covered" and got["missing"] == [], got


@strict_xfail("U5", "the value of a candidate row under a two-level header is the first figure of the row (the monthly one), not the figure of the column the request names")
def test_a_candidate_row_under_a_two_level_header_is_valued_by_the_column_the_request_names():
    from tests.unit.test_items import _offer, _ws

    table = "\n".join(["הכנסות מהנכס", "שימוש | הכנסה | הכנסה", " | חודשית | שנתית", "משרדים | 100,000 | 1,200,000", "מסחר | 45,000 | 540,000"])
    ws = _ws(table)
    offer = _offer(ws, "ההכנסה השנתית ממשרדים היא 1,200,000 ₪ [S1].", [{"id": "N1", "text": "ההכנסה השנתית מכל שימוש"}])
    assert [(c.label, c.value) for c in offer.candidates] == [("מסחר", "540,000")], [(c.label, c.value) for c in offer.candidates]


@strict_xfail("U5", "a judge's missing text equal to the whole component is accepted, so the gap line denies the whole component although the answer gives part of it")
def test_a_missing_text_that_equals_the_whole_component_is_rejected_and_a_part_of_it_is_kept():
    from app.chat.verify import valid_missing

    component = "שווי הנכס בגישת ההשוואה"
    request = "מהו שווי הנכס בגישת ההשוואה?"
    assert valid_missing("השטח", "partial", "השטח והשווי של הנכס", "מהם השטח והשווי של הנכס?") == "השטח"  # a part is kept
    assert valid_missing(component, "partial", component, request) == ""


@strict_xfail("U5", "an item whose coverage check could not be completed leaves its component full: the lenient judge's vote stands and nothing caps it")
def test_ae10_an_unchecked_item_makes_its_component_partial_with_a_line_that_the_check_could_not_be_completed_and_no_repair_request(
        monkeypatch):
    from app.chat import items as ledger_items
    from tests.unit.test_coverage import MAIN_TWO_SENTENCES, _completions, _finish, _item_lines, _rights_check

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(ledger_items, "_check_block", broken)
    ws, r, turn, answer, _ = _rights_check(MAIN_TWO_SENTENCES)
    final, outcomes = _finish(ws, answer, r, turn)
    assert {s["state"] for s in r.item_states()} == {"unchecked"}
    (o,) = outcomes
    assert o["status"] == "partial", o["status"]
    lines = _item_lines(final)
    assert lines and all("לא ניתן היה להשלים את הבדיקה" in ln for ln in lines), final.answer_markdown
    assert not _completions(r)  # no repair is requested for a check the server could not complete
