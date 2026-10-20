"""Reproductions of the round-19 failure shapes, recorded failing on the round-18 build (round 19 U8: R1-R17; AE1, AE3-AE7;
KTD1-KTD5).

Each sequence is a ScriptedAgent turn over a synthetic document (office B): the model and the judge are scripted, so a test
proves what the server does with an answer that is CORRECT for its fixture, not the judgement of a model. Each open case is
marked ``xfail(strict=True, raises=AssertionError)`` with the unit that owns the fix and what fails, and the owning unit removes
the marker when its fix lands (a strict xfail that starts passing fails the suite, so the marker cannot be forgotten). A
protection sequence (a wrong claim that must still be removed, a true omission that must still get its line) is no xfail: it
passes on the round-18 build and must keep passing. Every assertion is on what the user sees (the final message and the
server's lines in it) or on the diagnostics the message stores, never on a call inside the server.

1. Calculation pages whose first line is a descriptive sentence ending in a colon (AE3, U2): the derivation cuts them into a
   context of their own, which the colon sentence names; the table the answer cites states the parcel of the asked property.
   The values are the asked property's own. Protection: a value of the property of another parcel in the same file is removed.
2. The same pages whose first line is OCR digit noise (U2): the noise is no identity.
3. A comparison of two appraisals of one file whose second side the question writes by its pages, not by its name (AE4, U2):
   each cell is checked against the side it is written under and cites; a cell that cites side B under side A is removed.
4. One table row that answers two request components (AE1, U3): the agent declares it under one; the other component keeps
   its own item and each has its own state.
5. A column headed with an abbreviation for "total per year" and a monthly-rent column whose header writes the abbreviated
   plural noun (AE5, U1): the values keep their periods.
6. A table that writes 13,2 in a cell and 3,596,696 elsewhere, and a negative percent (AE6, U1).
7. A list item that states an item over two sentences while the item's value also holds a plan identifier (AE7, U4): no "read
   and not used" line; an item with one of two figures stated gets a partial line naming the missing figure.
8. An item the agent declared not applicable that the answer states (U4): no line. Protection: a true omission still gets its line.

The documents are the synthetic fixtures of ``tests/fixtures/round19`` (``scripts/generate_fixtures.py --only round19``);
every name and figure is invented."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_round18_sequences import (
    DOCUMENT_OF,
    ask,
    ingest_pdf,
    lenient_judge,
    line_with,
    read_pages,
    read_table,
)
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round19" / "manifest.json").read_text(encoding="utf-8"))["documents"]
REPAIRS = 4  # the same answer again at each step a verification round may ask the agent for


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def removals(client, m: dict) -> list[tuple[str, str]]:
    """(check, failure kind) of each claim the turn removed, from the diagnostics the message stores."""
    removed = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["removed"]
    return [(r["check"], r["failure_kind"]) for r in removed]


def strict_xfail(unit: str, what: str):
    """The marker of an open case: it fails with an assertion on the round-18 build, for the reason ``what``, and ``unit`` owns the fix."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{unit}: {what}")


# --- 1 and 2. calculation pages cut into a context that a noise first line names (AE3, U2) ----------------------------------

CALCULATION = MANIFEST["calc_sentence"]
NOISE = MANIFEST["calc_noise"]
CALCULATION_QUESTION = "מהי ההכנסה נטו השנתית והשווי המהוון של מתחם גבעת הברוש בחישוב ההיוון?"
CALCULATION_ASK = "ההכנסה נטו השנתית והשווי המהוון של מתחם גבעת הברוש בחישוב ההיוון"
CALCULATION_ANSWER = ("בחישוב ההיוון של מתחם גבעת הברוש ההכנסה נטו השנתית היא 610,000 ₪ "
                      "והשווי המהוון הוא 9,384,615 ₪ [S1].")
OTHER = CALCULATION["other"]
OTHER_ANSWER = f"שווי הנכס בגישת ההשוואה של מתחם גבעת הברוש הוא {OTHER['value']} ₪ [S1]."


@pytest.mark.parametrize("spec, heading", [
    pytest.param(CALCULATION, "a colon sentence"),
    pytest.param(NOISE, "digit noise"),
])
def test_values_on_calculation_pages_are_the_asked_propertys_when_their_first_line_names_nothing_and_the_table_states_its_parcel(
        client, office, monkeypatch, spec, heading):
    m = ask(client, office, monkeypatch, spec, read_pages(3, 3), CALCULATION_ANSWER, CALCULATION_QUESTION,
            CALCULATION_ASK)
    md = m["answer"]["markdown"]
    assert line_with(md, "610,000") == CALCULATION_ANSWER, (heading, md)
    assert "נכס אחר" not in md and "הוסר" not in md, (heading, md)
    assert removals(client, m) == [], (heading, md)


@pytest.mark.parametrize("spec", [CALCULATION, NOISE])
def test_protection_a_value_of_the_property_of_another_parcel_in_the_same_file_is_still_removed(
        client, office, monkeypatch, spec):
    m = ask(client, office, monkeypatch, spec, read_pages(5, 5), OTHER_ANSWER,
            "מהו שווי הנכס בגישת ההשוואה של מתחם גבעת הברוש?", "שווי הנכס בגישת ההשוואה של מתחם גבעת הברוש")
    md = m["answer"]["markdown"]
    assert OTHER["value"] not in md, md
    # the claim rests on the context of the property of another parcel (block 33422, parcel 9): removed by the context check
    assert removals(client, m) == [("context", "wrong_subject")], md


# --- 3. a comparison whose second side the question writes by its pages (AE4, U2) ------------------------------------------

COMPARISON = MANIFEST["comparison"]
SIDE_A, SIDE_B = COMPARISON["reports"]
COMPARISON_QUESTION = "השווה בין שווי הנכס ושטחו ברחוב השקד 7 לבין השומה בעמודים 3–4"
COMPARISON_SUBJECT = "הנכס ברחוב השקד 7 והשומה בעמודים 3–4"
COMPARISON_ASK = "השוואת שווי הנכס ושטחו בין הנכס ברחוב השקד 7 לבין השומה בעמודים 3–4"
COMPARISON_HEAD = "| נתון | הנכס ברחוב השקד 7 | השומה בעמודים 3–4 |\n|---|---|---|\n"


def read_both_sides(items) -> list[dict]:
    """One scripted step that reads the pages of side A (S1) and the pages of side B (S2)."""
    return [read(pages={"document": DOCUMENT_OF[0], "from_page": 1, "to_page": 2}),
            read(pages={"document": DOCUMENT_OF[0], "from_page": 3, "to_page": 4})]


def compare(client, office, monkeypatch, table: str) -> dict:
    """A turn that compares the two sides: the agent reads both, answers ``table`` (then the same again at each repair)."""
    answer = "השוואה בין שתי השומות:\n\n" + COMPARISON_HEAD + table
    doc = ingest_pdf(office, monkeypatch, COMPARISON)
    DOCUMENT_OF[0] = doc
    steps = [[call("outline", document=doc)], read_both_sides, *[final(answer, documents=[doc]) for _ in range(1 + REPAIRS)]]
    request = [component(COMPARISON_ASK, subject=COMPARISON_SUBJECT)]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=lenient_judge, request=request))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), COMPARISON_QUESTION)
    assert m["status"] == "done", m
    return m


def comparison_row(label: str, a: str, b: str) -> str:
    return f"| {label} | {a} | {b} |\n"


OWN_CELLS = (comparison_row("שווי הנכס בגישת ההשוואה", f"{SIDE_A['value']} ₪ [S1]", f"{SIDE_B['value']} ₪ [S2]")
             + comparison_row("שטח הנכס", f"{SIDE_A['area']} מ״ר [S1]", f"{SIDE_B['area']} מ״ר [S2]"))


def test_each_cell_of_a_comparison_is_checked_against_its_own_side_when_the_second_side_is_written_by_its_pages(
        client, office, monkeypatch):
    m = compare(client, office, monkeypatch, OWN_CELLS)
    md = m["answer"]["markdown"]
    for value in (SIDE_A["value"], SIDE_A["area"], SIDE_B["value"], SIDE_B["area"]):
        assert value in md, (value, md)
    assert removals(client, m) == [], md


def test_a_cell_that_cites_side_b_under_side_a_is_removed_and_the_cells_that_cite_their_own_side_stay(
        client, office, monkeypatch):
    misplaced = comparison_row("שטח הנכס", f"{SIDE_B['area']} מ״ר [S2]", "—")
    m = compare(client, office, monkeypatch,
                comparison_row("שווי הנכס בגישת ההשוואה", f"{SIDE_A['value']} ₪ [S1]", f"{SIDE_B['value']} ₪ [S2]") + misplaced)
    md = m["answer"]["markdown"]
    assert SIDE_A["value"] in md and SIDE_B["value"] in md, md
    # the area of side B written under side A's column is side B's datum under side A: not stated for side A
    assert SIDE_B["area"] not in md, md


# --- 4. one table row that answers two request components (AE1, U3) ---------------------------------------------------------

TWO = MANIFEST["two_components"]
TWO_ROW = TWO["rows"][0]
TWO_FIRST = "השטח הכולל לבנייה בנכס"
TWO_SECOND = "התנאי שבו חלות זכויות הבנייה"
TWO_ANSWER = (f"{TWO_FIRST} הוא {TWO_ROW[1]} מ״ר [S1].\n"
              f"הזכויות בשטח של {TWO_ROW[1]} מ״ר חלות {TWO['condition_phrase']} [S1].")


def ledger_of(client, m: dict) -> dict:
    return client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]["item_ledger"]


def test_a_row_the_agent_declares_under_one_component_still_gives_the_other_component_its_own_covered_item(
        client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, TWO)
    DOCUMENT_OF[0] = doc
    # the agent declares the row under the first component only
    item = {"component": "N1", "label": TWO_ROW[0], "source": "S1", "quote": " | ".join(TWO_ROW), "value": TWO_ROW[1],
            "condition": TWO["condition_phrase"], "state": "included", "reason": ""}
    steps = [[call("outline", document=doc)], read_table(TWO["caption"]),
             *[final(TWO_ANSWER, documents=[doc], items=[item]) for _ in range(1 + REPAIRS)]]
    request = [component(TWO_FIRST, id="a"), component(TWO_SECOND, id="b")]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=lenient_judge, request=request))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), "מהו השטח הכולל לבנייה בנכס ובאיזה תנאי חלות זכויות הבנייה?")
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    assert TWO_ANSWER in md, md
    states = sorted((i["component"], i["state"]) for i in ledger_of(client, m)["items"])
    assert states == [("N1", "covered"), ("N2", "covered")], states


def condition_missing_judge(input: str) -> dict:
    """A judge that supports every unit and answers every item a call asks for its condition with ``missing`` and the row's note."""
    out = lenient_judge(input)
    asked = re.findall(r'<item id="(I\d+)"[^>]*check="\w+"', input)
    for r in out.get("requirements", []):
        r["conditions"] = [{"item": i, "outcome": "missing", "quote": TWO["condition_phrase"]} for i in asked]
    return out


def test_a_row_the_server_gave_the_first_component_keeps_its_item_when_the_agent_later_declares_the_row_under_the_second(
        client, office, monkeypatch):
    """The other order: the first version declares nothing (the server builds the row's item from the first sentence that cites it,
    for the first component, with its condition), the repair version declares the row under the second component."""
    doc = ingest_pdf(office, monkeypatch, TWO)
    DOCUMENT_OF[0] = doc
    item = {"component": "N2", "label": TWO_ROW[0], "source": "S1", "quote": " | ".join(TWO_ROW), "value": TWO_ROW[1],
            "condition": TWO["condition_phrase"], "state": "included", "reason": ""}
    bare = f"{TWO_FIRST} הוא {TWO_ROW[1]} מ״ר [S1].\nהזכויות בשטח של {TWO_ROW[1]} מ״ר מפורטות בטבלה [S1]."
    steps = [[call("outline", document=doc)], read_table(TWO["caption"]), final(bare, documents=[doc]),
             *[final(TWO_ANSWER, documents=[doc], items=[item]) for _ in range(REPAIRS)]]
    request = [component(TWO_FIRST, id="a"), component(TWO_SECOND, id="b")]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=condition_missing_judge, request=request))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), "מהו השטח הכולל לבנייה בנכס ובאיזה תנאי חלות זכויות הבנייה?")
    assert m["status"] == "done", m
    states = sorted((i["component"], i["state"]) for i in ledger_of(client, m)["items"])
    assert states == [("N1", "covered"), ("N2", "covered")], (states, m["answer"]["markdown"])


# --- 5. headers written with abbreviations: "total per year" and a monthly-rent noun in the abbreviated plural (AE5, U1) --------

ABBREVIATIONS = MANIFEST["abbreviations"]
MONTHLY_LINE = "דמי השכירות החודשיים ממשרדים הם 100,000 ₪ לחודש [S1]."
ANNUAL_LINE = "ההכנסה השנתית מדמי שכירות ממשרדים היא 1,200,000 ₪ לשנה [S1]."
YEARLY_LINE = "עלות הביטוח היא 24,000 ₪ לשנה [S1]."
ABBREVIATIONS_ANSWER = f"{MONTHLY_LINE}\n{ANNUAL_LINE}\n{YEARLY_LINE}"


def test_values_under_headers_written_with_abbreviations_keep_their_periods(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, ABBREVIATIONS, read_pages(1, 1), ABBREVIATIONS_ANSWER,
            "מהם דמי השכירות החודשיים ממשרדים, ההכנסה השנתית מדמי שכירות ממשרדים ועלות הביטוח?",
            "דמי השכירות החודשיים וההכנסה השנתית ממשרדים ועלות הביטוח")
    md = m["answer"]["markdown"]
    for line in (MONTHLY_LINE, ANNUAL_LINE, YEARLY_LINE):
        assert line in md.splitlines(), md
    assert "לא צוינ" not in md and "הוסר" not in md, md
    assert removals(client, m) == [], md


# --- 6. a decimal comma beside a thousands comma, and a negative percent (AE6, U1) ------------------------------------------

DECIMAL = MANIFEST["decimal_comma"]
RATE_LINE = "שיעור התשואה הגולמית הוא 13.2% [S1]."
CHANGE_LINE = "השינוי השנתי במחירי הנכסים באזור הוא −3.5% [S1]."
VALUE_LINE = "שווי הנכס הוא 3,596,696 ₪ [S1]."
DECIMAL_QUESTION = "מהם שיעור התשואה הגולמית, השינוי השנתי במחירי הנכסים באזור ושווי הנכס?"
DECIMAL_ASK = "שיעור התשואה הגולמית, השינוי השנתי במחירי הנכסים באזור ושווי הנכס"


def test_a_decimal_comma_in_a_cell_is_read_as_a_decimal_when_the_table_writes_thousands_elsewhere(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, DECIMAL, read_pages(1, 1), f"{RATE_LINE}\n{VALUE_LINE}", DECIMAL_QUESTION,
            DECIMAL_ASK)
    md = m["answer"]["markdown"]
    assert line_with(md, "13.2") == RATE_LINE, md
    assert line_with(md, "3,596,696") == VALUE_LINE, md
    assert removals(client, m) == [], md


def test_a_negative_percent_the_source_writes_with_a_decimal_comma_is_kept_with_its_sign(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, DECIMAL, read_pages(1, 1), CHANGE_LINE, DECIMAL_QUESTION, DECIMAL_ASK)
    md = m["answer"]["markdown"]
    assert line_with(md, "3.5") == CHANGE_LINE, md
    assert removals(client, m) == [], md


def test_protection_the_claim_of_132_percent_is_still_not_supported_by_a_cell_that_writes_13_2(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, DECIMAL, read_pages(1, 1), "שיעור התשואה הגולמית הוא 132% [S1].",
            DECIMAL_QUESTION, DECIMAL_ASK)
    assert "132" not in m["answer"]["markdown"], m["answer"]["markdown"]


# --- 7 and 8. coverage: the lines the server writes for items (AE7, U4) --------------------------------------------------------

ROWS = MANIFEST["coverage_rows"]
MAIN_ROW, BASEMENT_ROW = ROWS["rows"]
ROWS_QUESTION = "מהן זכויות הבנייה בנכס?"
ROWS_ASK = "זכויות הבנייה בנכס"
NOT_USED = "לא נעשה בו שימוש"
NOT_APPLICABLE = "אינו חל על הבקשה"
MAIN_ITEM = {"component": "N1", "label": MAIN_ROW[0], "source": "S1", "quote": " | ".join(MAIN_ROW),
             "value": f"{MAIN_ROW[1]} מ״ר; תכנית {MAIN_ROW[3]}", "condition": "", "state": "included", "reason": ""}
BASEMENT_ITEM = {"component": "N1", "label": BASEMENT_ROW[0], "source": "S1", "quote": " | ".join(BASEMENT_ROW),
                 "value": f"{BASEMENT_ROW[1]} מ״ר; {BASEMENT_ROW[2]} קומות", "condition": "", "state": "included", "reason": ""}
MAIN_SENTENCES = (f"- השטח העיקרי בנכס הוא {MAIN_ROW[1]} מ״ר [S1]. הוא נקבע לפי תכנית {MAIN_ROW[3]} [S1].")
BASEMENT_ONE_FIGURE = f"- שטח המרתף בנכס הוא {BASEMENT_ROW[1]} מ״ר [S1]."


def item_lines(md: str, name: str) -> list[str]:
    """The server's lines about the row whose name the source gives as ``name`` (the line opens with «name»)."""
    return [ln for ln in md.splitlines() if ln.startswith(f"«{name}»")]


@strict_xfail("U4", "an item a list item states over two sentences (the value field holds its figure and the plan identifier) is "
                    "reported as read and not used")
def test_a_list_item_that_states_an_item_over_two_sentences_leaves_no_read_and_not_used_line_when_the_value_holds_a_plan_identifier(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, ROWS, read_table(ROWS["caption"]), MAIN_SENTENCES, ROWS_QUESTION, ROWS_ASK,
            items=[MAIN_ITEM])
    md = m["answer"]["markdown"]
    assert MAIN_ROW[1] in md and MAIN_ROW[3] in md, md
    assert item_lines(md, MAIN_ROW[0]) == [] and NOT_USED not in md, md


@strict_xfail("U4", "an item of which one of two figures is stated is reported as read and not used, without the missing figure")
def test_an_item_with_one_of_two_figures_stated_gets_a_partial_line_that_names_the_missing_figure(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, ROWS, read_table(ROWS["caption"]), BASEMENT_ONE_FIGURE, ROWS_QUESTION, ROWS_ASK,
            items=[BASEMENT_ITEM])
    md = m["answer"]["markdown"]
    (line,) = item_lines(md, BASEMENT_ROW[0])
    # the figure the answer gave is given: the line is no "read and not used"; it names the floors the answer left out
    assert NOT_USED not in line and BASEMENT_ROW[2] in line and "קומות" in line, md


@strict_xfail("U4", "an item the agent declared not applicable is reported as not applicable although the answer states its value")
def test_an_item_the_agent_declared_not_applicable_that_the_answer_states_gets_no_line(client, office, monkeypatch):
    declared = BASEMENT_ITEM | {"value": BASEMENT_ROW[1], "state": "not_applicable",
                                "reason": "שטח המרתף אינו חלק משטח הבנייה שנשאל עליו"}
    answer = f"{BASEMENT_ONE_FIGURE}\n- השטח העיקרי בנכס הוא {MAIN_ROW[1]} מ״ר [S1]."
    m = ask(client, office, monkeypatch, ROWS, read_table(ROWS["caption"]), answer, ROWS_QUESTION, ROWS_ASK, items=[declared])
    md = m["answer"]["markdown"]
    assert BASEMENT_ROW[1] in md, md
    assert item_lines(md, BASEMENT_ROW[0]) == [] and NOT_APPLICABLE not in md, md


def test_protection_a_true_omission_still_gets_its_read_and_not_used_line(client, office, monkeypatch):
    answer = f"- השטח העיקרי בנכס הוא {MAIN_ROW[1]} מ״ר [S1]."
    omitted = BASEMENT_ITEM | {"value": f"{BASEMENT_ROW[1]} מ״ר"}
    m = ask(client, office, monkeypatch, ROWS, read_table(ROWS["caption"]), answer, ROWS_QUESTION, ROWS_ASK, items=[omitted])
    md = m["answer"]["markdown"]
    (line,) = item_lines(md, BASEMENT_ROW[0])
    assert NOT_USED in line and "S1" in line, md
    assert BASEMENT_ROW[1] not in md.replace(line, ""), md
