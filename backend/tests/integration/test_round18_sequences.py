"""Reproductions of the round-18 failure shapes, recorded failing on the round-17 build (round 18 U8: R1-R4, R8-R11; AE1,
AE7, AE8, AE9; KTD1, KTD3, KTD4, KTD5).

Each sequence is a ScriptedAgent turn over a synthetic document (office B): the model and the judge are scripted, so a test
proves what the server does with an answer that is CORRECT for its fixture, not the judgement of a model. Each is marked
``xfail(strict=True)`` with the unit that owns the fix and what fails; the owning unit removes the marker when its fix lands
(a strict xfail that starts passing fails the suite, so the marker cannot be forgotten). Every assertion is on what the user
sees (the final message and the server's lines in it), never on a call inside the server.

1. A split period word (AE9, U1). The text layer holds "36 חודשי ם" (the plural noun split) and "לחוד ש" (the monthly word
   split): the 36 is a duration and carries no period note, and the monthly rent is not edited or removed.
2. A two-row header (U1). A page holds a table of areas and then a table whose upper header row carries the annual period:
   the annual amounts keep their period.
3. Years and a thousands abbreviation (U2). "2021,2022,2023" is a list of years and "50K" is 50,000: neither is an unstated
   number when the answer writes the years apart and the amount in full digits.
4. VAT across number forms (U2). The source writes 16,070,000 with VAT, the answer 16.07 million with the same status: the
   VAT wording is not edited.
5. Calculation pages that open with a short heading line ending in a colon (U3): their printed numbering restarts, which
   opens a second context that names no property; their values are the appraisal's own, not "another property".
6. A file of two reports (U3): a passage of the second report is not judged as the first report's subject because the
   header the judge is shown carries the file's title, which names the first.
7. A condition on a single item (AE1, U5): "only by an approved plan" reaches the final answer with the value kept.
8. A note after a table that points back to it (AE7, U4), with another table on the next page after unrelated blocks: the
   note scopes the first table (the read tags it "between two tables" and the server then refuses it).
9. A numbered method heading over a table (AE8, U4/U5): the method reaches a value of the table, a later section's note does
   not.

The documents are the synthetic fixtures of ``tests/fixtures/round18`` (``scripts/generate_fixtures.py --only round18``);
every name and figure is invented."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import _outputs, handle_of
from tests.integration.test_documents_api import ingest
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round18" / "manifest.json").read_text(encoding="utf-8"))["documents"]
REPAIRS = 4  # the same answer again at each step a verification round may ask the agent for


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def ingest_pdf(office, monkeypatch, spec: dict, title: str | None = None) -> str:
    """A round-18 fixture ingested without OCR or vision, in the office's default group; the document id."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    path = FIXTURES / spec["file"]
    return ingest(office, path, title or path.stem)[0]


def open_section(heading: str):
    """A scripted step that reads the section named ``heading`` through the handle the first step's outline gave."""
    return lambda items: [read(section=handle_of(_outputs(items)[0], heading))]


def read_table(caption: str):
    """A scripted step that reads the table captioned ``caption`` through the handle the first step's outline gave."""
    return lambda items: [read(table=handle_of(_outputs(items)[0], caption))]


def read_pages(first: int, last: int):
    """A scripted step that reads the pages ``first`` to ``last`` of the document the turn is about."""
    return lambda items: [read(pages={"document": DOCUMENT_OF[0], "from_page": first, "to_page": last})]


DOCUMENT_OF: list[str] = [""]  # the id of the document the running turn ingested (``read_pages`` names it)


def units_of(input: str) -> dict[int, str]:
    """The units a judge call shows, by index."""
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def scored(input: str, verdicts: list[dict]) -> dict:
    """A judge's reply: ``verdicts`` for the units, and every component of the call ``full`` over the units shown."""
    ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
    return {"verdicts": verdicts} | ({"requirements": [requirement(id=r, units=list(units_of(input))) for r in ids]}
                                     if ids else {})


def lenient_judge(input: str) -> dict:
    """A judge that supports every unit: the server's own checks, not the judge, decide what is removed or edited."""
    return scored(input, [{"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []}
                          for i in units_of(input)])


def ask(client, office, monkeypatch, spec: dict, read_step, answer: str, question: str, component_text: str, *,
        repaired: str | None = None, judge=lenient_judge, title: str | None = None, items: list | None = None) -> dict:
    """A turn over the fixture ``spec`` in which the agent outlines the document, reads with ``read_step`` and answers
    ``answer`` (then ``repaired`` at each repair request, else ``answer`` again). Returns the message."""
    doc = ingest_pdf(office, monkeypatch, spec, title)
    DOCUMENT_OF[0] = doc
    again = answer if repaired is None else repaired
    steps = [[call("outline", document=doc)], read_step, final(answer, documents=[doc], items=items),
             *[final(again, documents=[doc], items=items) for _ in range(REPAIRS)]]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=judge, request=[component(component_text)]))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return m


def line_with(markdown: str, needle: str) -> str:
    """The one line of the answer that holds ``needle``."""
    found = [ln for ln in markdown.splitlines() if needle in ln]
    assert len(found) == 1, (needle, markdown)
    return found[0]


# --- 1. a duration and a monthly rent with a split period word (AE9, U1) ----------------------------------------------

LEASE = MANIFEST["lease"]
LEASE_DURATION = "תקופת השכירות בהסכם היא 36 חודשים [S1]."
LEASE_RENT = "דמי השכירות בהסכם הם 50 ₪ למ״ר לחודש [S1]."


def test_a_duration_gets_no_period_note_and_a_monthly_rent_is_kept_when_the_text_layer_splits_the_period_words(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, LEASE, open_section(LEASE["heading"]), f"{LEASE_DURATION}\n{LEASE_RENT}",
            "מהי תקופת השכירות ומהם דמי השכירות למ״ר לחודש?", "תקופת השכירות ודמי השכירות למ״ר לחודש")
    md = m["answer"]["markdown"]
    # the monthly rent stays as the agent wrote it, with its citation
    assert line_with(md, "50 ₪") == LEASE_RENT, md
    # AE9: the 36 is a duration: the server writes no period next to it
    assert line_with(md, "36") == LEASE_DURATION, md
    assert "הוסר" not in md, md


# --- 2. a two-row header on a page with another table (U1) ------------------------------------------------------------

HEADER = MANIFEST["two_row_header"]
HEADER_ANSWER = ("ההכנסה השנתית מדמי שכירות במשרדים היא 1,240,000 ₪ [S1].\n"
                 "ההכנסה השנתית מדמי שכירות במסחר היא 860,000 ₪ [S1].")


def test_annual_amounts_under_a_header_row_that_carries_the_period_keep_it_when_another_table_shares_the_page(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, HEADER, read_pages(1, 1), HEADER_ANSWER,
            "מהי ההכנסה השנתית מדמי שכירות במשרדים ובמסחר?", "ההכנסה השנתית מדמי שכירות במשרדים ובמסחר")
    md = m["answer"]["markdown"]
    for amount in ("1,240,000", "860,000"):
        assert line_with(md, amount).startswith("ההכנסה השנתית מדמי שכירות"), md
    assert "לא צוינ" not in md and "הוסר" not in md, md


# --- 3. years joined by commas and a thousands abbreviation (U2) ------------------------------------------------------

NUMBERS = MANIFEST["numbers"]
YEARS_LINE = "שווי הנכס נקבע לפי ממוצע שנות הבסיס 2021, 2022 ו-2023 ועומד על 4,800,000 ₪ [S1]."
UNIT_LINE = "מחיר ליחידת דיור בפרויקט הוא 50,000 ₪ [S1]."


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U2: 'unstated_number' removes both sentences: '2021,2022,2023' is read as one number, so the three years are"
    " unstated, and '50K' is read as 50, so 50,000 is unstated"))
def test_a_list_of_years_joined_by_commas_and_an_amount_the_source_abbreviates_are_not_unstated_numbers(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, NUMBERS, open_section(NUMBERS["years_heading"]), f"{YEARS_LINE}\n{UNIT_LINE}",
            "לפי איזו שיטה נקבע שווי הנכס, ומהו מחיר ליחידת דיור?", "שיטת קביעת השווי ומחיר ליחידת דיור")
    md = m["answer"]["markdown"]
    assert line_with(md, "2021") == YEARS_LINE, md
    assert line_with(md, "50,000") == UNIT_LINE, md


# --- 4. VAT across number forms (U2) ------------------------------------------------------------------------------------

VAT_ANSWER = "שווי הנכס בגישת ההשוואה הוא 16.07 מיליון ₪ כולל מע״מ [S1]."


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U2: VAT backing compares the number as written: '16.07 מיליון' finds no VAT status for 16,070,000, so 'כולל מע״מ' is"
    " replaced by '(המע\"מ לא צוין במקור)'"))
def test_an_amount_written_in_millions_keeps_its_vat_wording_when_the_source_writes_it_in_full_digits(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, NUMBERS, open_section(NUMBERS["vat_heading"]), VAT_ANSWER,
            "מהו שווי הנכס בגישת ההשוואה, והאם הוא כולל מע״מ?", "שווי הנכס בגישת ההשוואה וסטטוס המע״מ שלו")
    md = m["answer"]["markdown"]
    assert line_with(md, "16.07") == VAT_ANSWER, md
    assert "הוסר" not in md and "לא צוין" not in md, md


# --- 5. calculation pages that open with a colon heading (U3) -----------------------------------------------------------

CALCULATION = MANIFEST["calculation_pages"]
CALCULATION_ANSWER = ("בחישוב ההיוון של מתחם גבעת הברוש ההכנסה נטו השנתית היא 610,000 ₪ "
                      "והשווי המהוון הוא 9,384,615 ₪ [S1].")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U3: the calculation pages (printed numbering restarts) are a second context with no identifier, and the claim, which"
    " names the project, is removed as 'wrong_subject' ('נתון של נכס אחר באותו קובץ')"))
def test_values_on_calculation_pages_that_open_with_a_colon_heading_are_the_appraisals_own_not_another_property(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, CALCULATION, read_pages(3, 3), CALCULATION_ANSWER,
            "מהי ההכנסה נטו השנתית והשווי המהוון של מתחם גבעת הברוש בחישוב ההיוון?",
            "ההכנסה נטו השנתית והשווי המהוון של מתחם גבעת הברוש בחישוב ההיוון")
    md = m["answer"]["markdown"]
    assert line_with(md, "610,000") == CALCULATION_ANSWER, md
    assert "נכס אחר" not in md and "הוסר" not in md, md


# --- 6. a file holding two reports, the answer about the second (U3) ----------------------------------------------------

REPORTS = MANIFEST["two_reports"]["reports"]
FIRST, SECOND = REPORTS
REPORTS_ANSWER = f"שווי הנכס בגישת ההשוואה בפרויקט נחל הצבר הוא {SECOND['value']} ₪ [S1]."
FIRST_SUBJECT = "גבעת הכליל"  # what the first report's subject line, and the file's title, name
SECOND_SUBJECT = "נחל הצבר"  # what the second report's subject line names


def head_of(input: str, source_id: str) -> str:
    """The header line the judge call shows for a source or a read passage (its ``title`` attribute)."""
    m = re.search(rf'<(?:source|read_passage) id="{source_id}"[^>]*? title="([^"]*)"', input)
    return m.group(1) if m else ""


def subject_head_judge(input: str) -> dict:
    """A judge that reads a passage by the header line it is shown: a unit that cites a source whose header names the first
    report's subject and not the asked report's own is a passage of another property (``wrong_subject``); else supported."""
    cites = dict(re.findall(r'<unit index="(\d+)" cites="([^"]*)">', input))
    verdicts = []
    for i in units_of(input):
        heads = " ".join(head_of(input, c) for c in re.split(r"[,\s]+", cites.get(str(i), "")) if c)
        if FIRST_SUBJECT in heads and SECOND_SUBJECT not in heads:
            verdicts.append({"index": i, "verdict": "unsupported", "failure": "wrong_subject", "reason": "בדיקה",
                             "supported_by": []})
        else:
            verdicts.append({"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []})
    return scored(input, verdicts)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U3: the header a source is shown in a file of two reports is the file's title (the first report's subject) plus a"
    " street label, never the second report's own subject, so a passage of the second report is judged 'wrong_subject'"))
def test_a_passage_of_the_second_report_is_not_judged_as_the_first_reports_subject(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, MANIFEST["two_reports"], read_pages(3, 4), REPORTS_ANSWER,
            "מהו שווי הנכס בגישת ההשוואה בפרויקט נחל הצבר?", "שווי הנכס בגישת ההשוואה בפרויקט נחל הצבר",
            judge=subject_head_judge, title=FIRST["subject"])
    md = m["answer"]["markdown"]
    assert line_with(md, SECOND["value"]) == REPORTS_ANSWER, md
    assert "הוסר" not in md, md


# --- 7. a single item whose row says its value holds only for an approved plan (AE1, U5) --------------------------------

PLAN = MANIFEST["approved_plan"]
PLAN_MAIN = PLAN["rows"][0]
PLAN_BARE = "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1]."
PLAN_WITH_CONDITION = f"השטח הכולל לבנייה בנכס הוא 1,200 מ״ר, {PLAN['condition_phrase']} [S1]."


def plan_items() -> list[dict]:
    """The item ledger the agent declares: the total area, with the condition its row's note states."""
    return [{"component": "N1", "label": PLAN_MAIN[0], "source": "S1", "quote": " | ".join(PLAN_MAIN),
             "value": PLAN_MAIN[1], "condition": PLAN_MAIN[2], "state": "included", "reason": ""}]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U5: a condition is asked only to tell apart items that share a value, so the one item's 'רק על פי תכנית מאושרת' is"
    " never checked, no repair is asked and the final answer gives 1,200 with no limit"))
def test_a_single_items_approved_plan_condition_reaches_the_final_answer_and_its_value_is_kept(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, PLAN, read_table(PLAN["caption"]), PLAN_BARE, "מהו השטח הכולל לבנייה בנכס?",
            "השטח הכולל לבנייה בנכס", repaired=PLAN_WITH_CONDITION, items=plan_items())
    md = m["answer"]["markdown"]
    # AE1: the value stays, and the line that gives it carries the limit (written by the repair or attached by the server)
    line = line_with(md, "1,200")
    assert "תכנית מאושרת" in line and re.search(r"\[S\d+\]", line), md


# --- 8. a note after a table that points back to it, then another table on the next page (AE7, U4) ----------------------

NOTE = MANIFEST["deictic_note"]
SCOPE_PHRASE = NOTE["scope_phrase"]
SCOPE_VALUE = NOTE["rows"][1][1]  # 11,800: the value at a 6.0% rate
SCOPE_CLAUSE = f"לפי {SCOPE_PHRASE}"
SCOPE_VALUE_CLAUSE = f"בשיעור היוון 6.0% שווי הנכס הוא {SCOPE_VALUE} אלפי ₪"
SCOPE_WITHOUT = f"{SCOPE_VALUE_CLAUSE} [S1]."
SCOPE_WITH = f"{SCOPE_VALUE_CLAUSE} {SCOPE_CLAUSE} [S1]."


def shown_scope_judge(input: str) -> dict:
    """A judge that holds the source to what it shows: a sentence that states the scope phrase is supported when a shown
    source states it too, else its scope clause is the one unsupported span of a partly supported sentence (the cut). A
    sentence that states the value without the scope, when a shown source states it, is supported with defect ``scope`` and
    the phrase (the judge reads the context)."""
    shown = re.sub(r"<requirement .*?</requirement>|<hint>.*?</hint>", "",
                   re.sub(r"<unit index.*?</unit>", "", input, flags=re.S), flags=re.S)
    verdicts = []
    for i, t in units_of(input).items():
        if SCOPE_VALUE not in t:
            verdict = {"verdict": "supported"}
        elif SCOPE_PHRASE in t and SCOPE_PHRASE not in shown:
            verdict = {"verdict": "partial", "defect": "part",
                       "claims": [{"span": t[:t.index(SCOPE_CLAUSE)].rstrip(" ,"), "verdict": "supported"},
                                  {"span": SCOPE_CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
        elif SCOPE_PHRASE not in t and SCOPE_PHRASE in shown:
            verdict = {"verdict": "supported", "defect": "scope", "scope": SCOPE_PHRASE}
        else:
            verdict = {"verdict": "supported"}
        verdicts.append({"index": i, "reason": "בדיקה", "supported_by": []} | verdict)
    return scored(input, verdicts)


@pytest.mark.parametrize("first", [
    "the_agent_writes_the_scope",  # holds today: the judge is shown the note, tagged as lying between two tables
    pytest.param("the_agent_omits_the_scope", marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
        "U4: the note is tagged 'בין שתי טבלאות' because another table follows within the scan, so the server does not"
        " attribute it to the table above even though it says 'הטבלה שלעיל': the value is shown with"
        " 'היקף הערך במקור לא אומת (הפסקה שליד הטבלה משותפת לשתי טבלאות)' instead of the scope")))])
def test_a_note_that_points_back_to_the_table_above_scopes_its_values_though_another_table_follows_on_the_next_page(
        client, office, monkeypatch, first):
    m = ask(client, office, monkeypatch, NOTE, read_table(NOTE["caption"]),
            SCOPE_WITH if first == "the_agent_writes_the_scope" else SCOPE_WITHOUT,
            "מהו שווי הנכס בשיעור היוון של 6.0% לפי טבלת הרגישות?", "שווי הנכס בשיעור היוון של 6.0% לפי טבלת הרגישות",
            repaired=SCOPE_WITH, judge=shown_scope_judge)
    md = m["answer"]["markdown"]
    # the value stays; the line that gives it names the scope phrase and cites a source, so it is never the property's total
    line = line_with(md, SCOPE_VALUE)
    assert SCOPE_PHRASE in line and re.search(r"\[S\d+\]", line), md
    assert "משותפת לשתי טבלאות" not in md, md


# --- 9. a numbered method heading over a table, a later section with a note of its own (AE8, U4/U5) ----------------------

METHOD = MANIFEST["method_heading"]
METHOD_VALUE = METHOD["rows"][1][1]  # 9,384,615
METHOD_BARE = f"השווי המהוון של הנכס הוא {METHOD_VALUE} ₪ [S1]."
METHOD_WITH = f"לפי {METHOD['method_phrase']} השווי המהוון של הנכס הוא {METHOD_VALUE} ₪ [S1]."


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "U4/U5: the heading '4. גישת היוון ההכנסות' is not a condition of the value under it: nothing asks for the method, so the"
    " final answer gives 9,384,615 with no method"))
def test_the_method_a_numbered_heading_names_reaches_a_value_of_its_table_and_a_later_sections_note_does_not(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, METHOD, read_table(METHOD["caption"]), METHOD_BARE,
            "מהו השווי המהוון של הנכס?", "השווי המהוון של הנכס", repaired=METHOD_WITH)
    md = m["answer"]["markdown"]
    # AE8: the method of the heading above the table is a condition of its value, and the later section's method is not
    line = line_with(md, METHOD_VALUE)
    assert METHOD["method_phrase"] in line and re.search(r"\[S\d+\]", line), md
    assert "גישת ההשוואה" not in md, md
