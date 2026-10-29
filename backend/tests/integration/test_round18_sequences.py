"""Reproductions of the round-18 failure shapes, recorded failing on the round-17 build (round 18 U8: R1-R4, R8-R11; AE1,
AE7, AE8, AE9; KTD1, KTD3, KTD4, KTD5).

Each sequence is a ScriptedAgent turn over a synthetic document (office B): the model and the judge are scripted, so a test
proves what the server does with an answer that is CORRECT for its fixture, not the judgement of a model. Each was
marked ``xfail(strict=True)`` with the unit that owned the fix and what failed, and the owning unit removed the marker when its
fix landed (a strict xfail that starts passing fails the suite, so the marker could not be forgotten). Every assertion is on what
the user sees (the final message and the server's lines in it), never on a call inside the server.

1. A split period word (AE9, U1). The text layer holds "36 חודשי ם" (the plural noun split) and "לחוד ש" (the monthly word
   split): the 36 is a duration and carries no period note, and the monthly rent is not edited or removed.
2. A two-row header (U1). A page holds a table of areas and then a table whose upper header row carries the annual period:
   the annual amounts keep their period.
3. Years and a thousands abbreviation (U2). "2021,2022,2023" is a list of years and "50K" is 50,000: neither is an unstated
   number when the answer writes the years apart and the amount in full digits.
4. VAT across number forms (U2). The source writes 16,070,000 with VAT, the answer 16.07 million with the same status: the
   VAT wording is not edited.
5. Calculation pages that open with a short heading line ending in a colon (U3): their printed numbering restarts, which
   opens a second context that names no property; their values are the appraisal's own, not "another property" (the
   context continues the one before it).
6. A file of two reports (U3): a passage of the second report is not judged as the first report's subject because the
   header the judge is shown names the second report's own context first and the file's title (which names the first) only
   as the file.
7. A condition on a single item (AE1, U5): "only by an approved plan" reaches the final answer with the value kept.
8. A note after a table that points back to it (AE7, U4), with another table on the next page after unrelated blocks: the
   note scopes the first table (the read tags it "between two tables" and the server then refuses it).
9. A numbered method heading over a table (AE8, U4/U5): the method reaches a value of the table, a later section's note does
   not.
10. A per-use rate table of which the answer cites one row (AE5, AE6, U6): the agent declared no item; the other rows are
    candidates, the judge marks the three rates of use as asked and the annual subscription row as not asked, and the completion
    repair (tools off, no search) adds the asked rows from the table already read.

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
from tests.integration.test_chat_calculation import _outputs, cell, handle_of
from tests.integration.test_chat_timeline import ADOPTED
from tests.integration.test_documents_api import ingest
from tests.integration.test_round16_sequences import RecordingAgent, repair_calls, repair_prompt
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
    assert_no_false_not_used(m["answer"]["markdown"])
    return m


def assert_no_false_not_used(markdown: str) -> None:
    """Round 18 U7 (R11): no "read and not used" line names an item the answer gives: the name the line carries (the source's own label
    of the row) is nowhere in the answer's other lines."""
    lines = markdown.splitlines()
    for line in (ln for ln in lines if "לא נעשה בו שימוש" in ln and ln.startswith("«")):
        name = line[1:line.index("»")]
        assert not any(name in other for other in lines if "לא נעשה בו שימוש" not in other), (name, markdown)


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


def test_a_list_of_years_joined_by_commas_and_an_amount_the_source_abbreviates_are_not_unstated_numbers(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, NUMBERS, open_section(NUMBERS["years_heading"]), f"{YEARS_LINE}\n{UNIT_LINE}",
            "לפי איזו שיטה נקבע שווי הנכס, ומהו מחיר ליחידת דיור?", "שיטת קביעת השווי ומחיר ליחידת דיור")
    md = m["answer"]["markdown"]
    assert line_with(md, "2021") == YEARS_LINE, md
    assert line_with(md, "50,000") == UNIT_LINE, md


# --- 4. VAT across number forms (U2) ------------------------------------------------------------------------------------

VAT_ANSWER = "שווי הנכס בגישת ההשוואה הוא 16.07 מיליון ₪ כולל מע״מ [S1]."


def test_an_amount_written_in_millions_keeps_its_vat_wording_when_the_source_writes_it_in_full_digits(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, NUMBERS, open_section(NUMBERS["vat_heading"]), VAT_ANSWER,
            "מהו שווי הנכס בגישת ההשוואה, והאם הוא כולל מע״מ?", "שווי הנכס בגישת ההשוואה וסטטוס המע״מ שלו")
    md = m["answer"]["markdown"]
    assert line_with(md, "16.07") == VAT_ANSWER, md
    assert "הוסר" not in md and "לא צוין" not in md, md


def test_ae10_an_item_the_answer_gives_in_millions_against_full_digits_gets_no_read_and_not_used_line(
        client, office, monkeypatch):
    """R11 (U7): the item is declared with its full-digit value; the answer writes 16.07 million. The ledger covers it by value form."""
    item = {"component": "N1", "label": "שווי הנכס בגישת ההשוואה", "source": "S1", "quote": NUMBERS["vat_line"],
            "value": "16,070,000 ₪", "condition": "", "state": "included", "reason": ""}
    m = ask(client, office, monkeypatch, NUMBERS, open_section(NUMBERS["vat_heading"]), VAT_ANSWER,
            "מהו שווי הנכס בגישת ההשוואה, והאם הוא כולל מע״מ?", "שווי הנכס בגישת ההשוואה וסטטוס המע״מ שלו", items=[item])
    md = m["answer"]["markdown"]
    assert line_with(md, "16.07") == VAT_ANSWER, md
    assert "לא נעשה בו שימוש" not in md and "הוסר" not in md, md
    assert m["answer"]["verification"]["completeness"]["status"] == "full"


# --- 5. calculation pages that open with a colon heading (U3) -----------------------------------------------------------

CALCULATION = MANIFEST["calculation_pages"]
CALCULATION_ANSWER = ("בחישוב ההיוון של מתחם גבעת הברוש ההכנסה נטו השנתית היא 610,000 ₪ "
                      "והשווי המהוון הוא 9,384,615 ₪ [S1].")


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


def first_named_report(head: str) -> str:
    """Which report a header line names first, reading it left to right: the first report's subject (``FIRST_SUBJECT`` or its
    street) or the second's (``SECOND_SUBJECT`` or its street); "" when it names neither."""
    at = {name: head.find(word) for name, word in (("first", "הכליל"), ("second", "הצבר")) if word in head}
    return min(at, key=at.get) if at else ""


def subject_head_judge(input: str) -> dict:
    """A judge that reads a passage by the header line it is shown, as a reader does: the subject the header names FIRST is the
    passage's. A unit that cites a source whose header names the first report before the asked second one is a passage of
    another property (``wrong_subject``); else supported."""
    cites = dict(re.findall(r'<unit index="(\d+)" cites="([^"]*)">', input))
    verdicts = []
    for i in units_of(input):
        heads = [head_of(input, c) for c in re.split(r"[,\s]+", cites.get(str(i), "")) if c]
        if any(first_named_report(h) == "first" for h in heads):
            verdicts.append({"index": i, "verdict": "unsupported", "failure": "wrong_subject", "reason": "בדיקה",
                             "supported_by": []})
        else:
            verdicts.append({"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []})
    return scored(input, verdicts)


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


def condition_judge(outcome: str, quote: str):
    """A judge that supports every unit, scores every component full over them and answers each item a call asks for its
    condition (``check`` in the item ledger) with ``outcome`` and ``quote``, a verbatim part of the item's span (round 18 U5)."""
    def judge(input: str) -> dict:
        out = lenient_judge(input)
        asked = re.findall(r'<item id="(I\d+)"[^>]*check="\w+"', input)
        for r in out.get("requirements", []):
            r["conditions"] = [{"item": i, "outcome": outcome, "quote": quote} for i in asked]
        return out
    return judge


def test_a_single_items_approved_plan_condition_reaches_the_final_answer_and_its_value_is_kept(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, PLAN, read_table(PLAN["caption"]), PLAN_BARE, "מהו השטח הכולל לבנייה בנכס?",
            "השטח הכולל לבנייה בנכס", repaired=PLAN_WITH_CONDITION, items=plan_items(),
            judge=condition_judge("missing", PLAN["condition_phrase"]))
    md = m["answer"]["markdown"]
    # AE1: the value stays, and the line that gives it carries the limit (written by the repair or attached by the server)
    line = line_with(md, "1,200")
    assert "תכנית מאושרת" in line and re.search(r"\[S\d+\]", line), md


def test_a_condition_the_repair_does_not_write_is_attached_by_the_server_with_its_citation_and_the_value_is_kept(
        client, office, monkeypatch):
    m = ask(client, office, monkeypatch, PLAN, read_table(PLAN["caption"]), PLAN_BARE, "מהו השטח הכולל לבנייה בנכס?",
            "השטח הכולל לבנייה בנכס", repaired=PLAN_BARE, items=plan_items(),
            judge=condition_judge("missing", PLAN["condition_phrase"]))
    md = m["answer"]["markdown"]
    line = line_with(md, "1,200")
    # the verbatim span of the row's note (the sentence that holds the judge's quote), written next to the value with its citation
    assert f"(התנאי לפי המקור: {PLAN['rows'][0][2]})" in line and re.search(r"\[S\d+\]", line), md
    assert "הוסר" not in md and "לא נמצא" not in md, md


def test_a_condition_left_out_entirely_is_completed_by_the_server_when_the_judge_says_nothing(client, office, monkeypatch):
    """R1: the operator's class (only) and its operand are absent from the sentence: the server needs no judge outcome. The repair
    does not write it, so the server attaches the verbatim span with its citation; the value stays."""
    m = ask(client, office, monkeypatch, PLAN, read_table(PLAN["caption"]), PLAN_BARE, "מהו השטח הכולל לבנייה בנכס?",
            "השטח הכולל לבנייה בנכס", repaired=PLAN_BARE, items=plan_items(), judge=lenient_judge)
    line = line_with(m["answer"]["markdown"], "1,200")
    assert f"(התנאי לפי המקור: {PLAN['rows'][0][2]})" in line and re.search(r"\[S\d+\]", line), m["answer"]["markdown"]
    assert "הוסר" not in m["answer"]["markdown"]


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
    "the_agent_writes_the_scope",  # the judge is shown the note, attributed to the table above it
    "the_agent_omits_the_scope",  # the note is adjacent and points back to its table: the server attributes it (U4)
])
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


def test_the_method_a_numbered_heading_names_reaches_a_value_of_its_table_and_a_later_sections_note_does_not(
        client, office, monkeypatch):
    # the agent declares no item: the cited row makes one (origin source), and the heading above its table is its condition
    m = ask(client, office, monkeypatch, METHOD, read_table(METHOD["caption"]), METHOD_BARE,
            "מהו השווי המהוון של הנכס?", "השווי המהוון של הנכס", repaired=METHOD_WITH,
            judge=condition_judge("missing", METHOD["method_phrase"]))
    md = m["answer"]["markdown"]
    # AE8: the method of the heading above the table is a condition of its value, and the later section's method is not
    line = line_with(md, METHOD_VALUE)
    assert METHOD["method_phrase"] in line and re.search(r"\[S\d+\]", line), md
    assert "גישת ההשוואה" not in md, md


# --- 9b. the same heading, the value cited through a registered value (V#) and the server's own citation (round 18 follow-up) -----

METHOD_ROW = METHOD["rows"][1][0]
METHOD_BOLD = f"השווי המהוון של הנכס הוא **{METHOD_VALUE} ₪**"


def read_and_take(caption: str):
    """A scripted step that reads the table captioned ``caption`` and registers its value cell (V1) in the same step, as the model does."""
    def step(items):
        return [read(table=handle_of(_outputs(items)[0], caption)),
                call("take_value", source="S1", locator=cell(METHOD_ROW, None, METHOD_VALUE) | {"column_number": 2}, meaning=ADOPTED,
                     label=METHOD_ROW)]
    return step


def test_a_value_cited_only_as_a_registered_value_is_checked_against_the_headings_method_and_gets_it(client, office, monkeypatch):
    """c3 attempt 3 on the real model: the answer cites the table value as [V1] alone (the round-17 attribution rule: a registered value
    cites the source it was read from). The V# is followed to its table's row and heading (``items.source_items``): the origin-``source``
    item exists, its condition (the heading's method) is asked of the requirements call, and a ``missing`` outcome puts the method next to
    the value. (That a silent call leaves the value as it is, is the contract: the heading is the call's to read, a generic label such as
    "the valuation method" being no condition.)"""
    bare = f"{METHOD_BOLD} [V1]."
    seen: list[str] = []

    def judge(input: str) -> dict:
        seen.append(input)
        return condition_judge("missing", METHOD["method_phrase"])(input)

    m = ask(client, office, monkeypatch, METHOD, read_and_take(METHOD["caption"]), bare, "מהו השווי המהוון של הנכס?",
            "השווי המהוון של הנכס", repaired=f"לפי {METHOD['method_phrase']} {bare}", judge=judge)
    line = line_with(m["answer"]["markdown"], METHOD_VALUE)
    assert METHOD["method_phrase"] in line and re.search(r"\[[SV]\d+\]", line), m["answer"]["markdown"]
    assert "גישת ההשוואה" not in m["answer"]["markdown"]
    first = next(x for x in seen if "<item_ledger>" in x)
    ledger = first[first.index("<item_ledger>"):first.index("</item_ledger>")]  # the V# was followed to its table: item I1 of S1
    assert '<item id="I1" source="S1"' in ledger and 'check="undecided"' in ledger and f'span="4. {METHOD["method_phrase"]}"' in ledger


def candidate_and_source_judge(input: str) -> dict:
    """A judge that names the table (S1) as the support of the sentence that states the value but cites nothing, assigns the capitalised
    value's row as asked, and supports every unit."""
    out = lenient_judge(input)
    for v in out["verdicts"]:
        if METHOD_VALUE in units_of(input)[v["index"]] and "[" not in units_of(input)[v["index"]]:
            v["supported_by"] = ["S1"]
    asks = [{"candidate": c, "words": METHOD_ROW} for c, label in re.findall(r'<candidate id="(K\d+)"[^>]*label="([^"]*)"', input)
            if label == METHOD_ROW]
    for r in out.get("requirements", []):
        r["candidates"] = asks
    return out


def test_a_value_stated_in_a_sentence_the_server_cites_is_not_read_and_not_used(client, office, monkeypatch):
    """c3 attempt 1 on the real model: the first sentence states the bold value without a citation, the next cites the table ([V1][S1]).
    The server attaches the citation the judge named to the first sentence; the item of that row (a candidate the judge assigned) is
    then given by it, and no line says it was read and not used."""
    draft = (f"{METHOD_BOLD}; מועד הנתון לא ידוע. הוא מופיע בטבלת תוצאות החישוב בסעיף „{METHOD['method_phrase']}” [V1][S1].")
    m = ask(client, office, monkeypatch, METHOD, read_and_take(METHOD["caption"]), draft, "מהו השווי המהוון של הנכס?",
            "השווי המהוון של הנכס", judge=candidate_and_source_judge)
    md = m["answer"]["markdown"]
    assert "לא נעשה בו שימוש" not in md and METHOD_VALUE in md, md
    # round 19 U4: the paragraph states the value in one sentence and cites the table in the next, which is one block: the row is given by
    # it from the start, so the judge is offered no open candidate for it and no item of the ledger is left open
    ledger = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"].get("item_ledger") or {}
    assert [i["state"] for i in ledger.get("items", [])] in ([], ["covered"]), ledger.get("items")


# --- 10. a per-use rate table of which the answer cites one row (AE5, AE6, U6) -----------------------------------------------

RATE = MANIFEST["rate_table"]
(RATE_SINGLE, RATE_NIGHT, RATE_WEEKEND, RATE_SUBSCRIPTION) = RATE["rows"]
RATE_ASK = "התעריפים לשימוש בחניון"
RATE_QUESTION = "מהם התעריפים לשימוש בחניון?"
RATE_CITED = f"התעריף ל{RATE_SINGLE[0]} הוא {RATE_SINGLE[1]} ₪ [S1]."
RATE_COMPLETED = (RATE_CITED + f"\nהתעריף ל{RATE_NIGHT[0]} הוא {RATE_NIGHT[1]} ₪ [S1]."
                  f"\nהתעריף ל{RATE_WEEKEND[0]} הוא {RATE_WEEKEND[1]} ₪ [S1].")


def candidate_judge(seen: list[str] | None = None):
    """A judge that supports every unit, scores every component full over the units shown (the lenient judge of round 16) and,
    in the requirements call, assigns each candidate row of a use ("כניסה") to the component with the request words it answers
    (the subscription row is not asked: it gets no assignment)."""
    def judge(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        out = lenient_judge(input)
        asks = [{"candidate": c, "words": "תעריף לשימוש"} for c, label in
                re.findall(r'<candidate id="(K\d+)"[^>]*label="([^"]*)"', input) if label.startswith("כניסה")]
        for r in out.get("requirements", []):
            r["candidates"] = asks
        return out
    return judge


def test_rows_of_a_cited_rate_table_the_judge_marks_asked_are_added_from_the_table_without_a_search_and_the_rest_is_not(
        client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, RATE)
    DOCUMENT_OF[0] = doc
    seen: list[str] = []
    steps = [[call("outline", document=doc)], read_table(RATE["caption"]), final(RATE_CITED, documents=[doc]),
             final(RATE_COMPLETED, documents=[doc])]
    agent = RecordingAgent(steps, judge=candidate_judge(seen), request=[component(RATE_ASK)])
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), RATE_QUESTION)
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    assert_no_false_not_used(md)
    # AE5: the two rows the agent never listed nor wrote are in the answer, from the table already read
    for row in (RATE_SINGLE, RATE_NIGHT, RATE_WEEKEND):
        assert any(row[1] in ln and re.search(r"\[S\d+\]", ln) for ln in md.splitlines()), md
    # AE6: the annual subscription row was not asked for, and it is neither written nor stated as "read and not used"
    assert RATE_SUBSCRIPTION[0] not in md and RATE_SUBSCRIPTION[1] not in md, md
    assert "לא נעשה בו שימוש" not in md and "לא נמצא" not in md, md
    # no search: the whole turn called the outline and the table once, and the repair step ran with its tools off
    called = [i["name"] for i in agent.seen[-1] if isinstance(i, dict) and i.get("type") == "function_call"]
    assert called == ["outline", "read"], called
    assert len(agent.seen) == 4 and agent.choices[-1] == "none" and agent.choices[:-1] == [None] * 3
    assert not repair_calls(agent)
    repair = repair_prompt(agent)
    assert RATE_NIGHT[0] in repair and RATE_WEEKEND[0] in repair and RATE_SUBSCRIPTION[0] not in repair
    assert m["answer"]["verification"]["completeness"]["status"] == "full"
    # the candidates the first call was shown: the three rows besides the cited one, rendered compactly
    first = next(x for x in seen if "<candidates>" in x)
    assert first.count("<candidate ") == 3 and "<candidates>" in first and RATE_SUBSCRIPTION[0] in first
    # the diagnostics hold the items the server built from the judge's assignment
    ledger = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]["item_ledger"]
    assert [(i["label"], i["origin"], i["candidate"], i["state"]) for i in ledger["items"]] == [
        (RATE_NIGHT[0], "source", True, "covered"), (RATE_WEEKEND[0], "source", True, "covered")]
    assert ledger["candidates"]["offered"] and ledger["candidates"]["left_out"] == {}


def test_a_rate_row_the_repair_never_writes_is_stated_as_read_and_not_used_with_the_name_the_source_gives_it(
        client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, RATE)
    DOCUMENT_OF[0] = doc
    steps = [[call("outline", document=doc)], read_table(RATE["caption"]), final(RATE_CITED, documents=[doc]),
             final(RATE_CITED, documents=[doc]), final(RATE_CITED, documents=[doc]), final(RATE_CITED, documents=[doc])]
    agent = ScriptedAgent(steps, judge=candidate_judge(), request=[component(RATE_ASK)])
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), RATE_QUESTION)
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    assert_no_false_not_used(md)
    lines = [ln for ln in md.splitlines() if "לא נעשה בו שימוש" in ln]
    assert [ln.split("»")[0] for ln in lines] == [f"«{RATE_NIGHT[0]}", f"«{RATE_WEEKEND[0]}"], md
    assert all("S1" in ln and "נקרא" in ln for ln in lines) and RATE_SUBSCRIPTION[0] not in md, md
    assert not re.search(r"לא נמצא|לא אותר|לא מופיע", md)
    assert m["answer"]["verification"]["completeness"]["status"] == "partial"
