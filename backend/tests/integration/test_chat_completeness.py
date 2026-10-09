"""Completeness end to end (U9, KTD7, R18–R22, AE6): the judge derives what the question requires, a requirement
whose data the turn found is completed by a repair round with tools, and what is still missing is stated with the
reason the turn supports — a calculation not completed, insufficient to conclude, not found after a search — never
"not found in the search" for values that were found. Correctness and completeness are reported apart.

The model and the judge are scripted: these tests prove what the server does with the judge's requirements and
scores, not the model's judgement. Synthetic documents only (the project of ``test_chat_calculation``)."""

from __future__ import annotations

import re

import pytest

from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import (
    CAPTION,
    COST,
    INCOME,
    _ae4_steps,
    _last_output,
    add_document,
    cell,
    handle_of,
    meaning,
    take,
)
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.doc, a.ver = add_document(a)
    return a

QUESTION = "מה ההכנסות הכוללות, מה יהיה הרווח אם העלויות יעלו ב-5%, ומה שיעור הרווח מההכנסות?"
INCOME_ASK, PROFIT_ASK, RATE_ASK = "ההכנסות הכוללות", "הרווח בתרחיש", "שיעור הרווח מההכנסות"
FIRST = ("ההכנסות הכוללות הן 12,450,000 ₪ [V1]. לפי הנחתך שהעלויות יעלו ב-5% [A1], הרווח בתרחיש יהיה "
         "1,530,000 ₪ [C1].")
COMPLETE = FIRST + " שיעור הרווח מההכנסות הוא 12.3% [C2]."


def _judge(asks: list[tuple[str, str, tuple[str, ...]]], absent: dict[str, str] | None = None, seen: list | None = None):
    """A judge that supports every unit and scores each requirement ``(text, keyword, related words)``: ``full``
    when a unit names its keyword (and does not say it was not found), else ``absent[text]`` (default ``missing``)
    with the units or statements that name it; ``related``: the ``<workspace>`` ids whose line holds one of its
    related words."""
    absent = absent or {}

    def respond(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = dict((int(i), t) for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input,
                                                         re.S))
        statements = dict((int(i), t) for i, t in re.findall(r'<statement index="(\d+)">\n(.*?)\n</statement>',
                                                              input, re.S))
        block = re.search(r"<workspace>\n(.*?)\n</workspace>", input, re.S)
        listing = re.findall(r"^([A-Z]\d+): (.*)$", block.group(1) if block else "", re.M)
        frozen = dict((t, i) for i, t in re.findall(r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input,
                                                   re.S))
        out = []
        for text, keyword, words in asks:
            hits = [i for i, t in units.items() if keyword in t and "לא נמצא" not in t]
            said = [i for i, t in (units | statements).items() if keyword in t and i not in hits]
            related = [i for i, line in listing if any(w in line for w in words)]
            out.append(requirement(text="" if frozen else text, id=frozen.get(text, ""),
                                   status="full" if hits else absent.get(text, "missing"), units=hits or said,
                                   related=related, calculation=text == RATE_ASK))
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": out}

    return respond


ASKS = [(INCOME_ASK, "ההכנסות הכוללות", ("הכנסות",)), (PROFIT_ASK, "הרווח בתרחיש", ("הרווח בתרחיש",)),
        (RATE_ASK, "שיעור הרווח", ("הכנסות", "הרווח בתרחיש", "שיעור"))]


def _ask(client, office, monkeypatch, steps: list, judge, question: str = QUESTION) -> tuple[dict, ScriptedAgent]:
    agent = ScriptedAgent(steps, judge=judge)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return m["answer"], agent


def _verify_inputs(agent: ScriptedAgent) -> list[str]:
    return [c.input for c in agent.calls if c.purpose.value == "verify"]


def test_ae6_an_omitted_part_whose_inputs_were_found_is_added_by_a_repair_round_with_tools(client, office,
                                                                                            monkeypatch):
    steps = [*_ae4_steps(office.doc)[:4], final(FIRST, documents=[office.doc]),
             # the repair round calls the calculator, then answers in full
             [call("calculate", expression="C1 / V1", label="שיעור הרווח מההכנסות", justification=None)],
             final(COMPLETE, documents=[office.doc])]
    a, agent = _ask(client, office, monkeypatch, steps, _judge(ASKS))
    assert a["status"] == "answered" and "12.3%" in a["markdown"] and "לא נמצא" not in a["markdown"]
    assert a["verification"]["correctness"] == "verified"
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 3, "missing": []}
    first, *later = _verify_inputs(agent)
    assert "<derive_requirements>" in first and later
    # the repair round's re-judge scores the same requirements by the same ids
    assert all('<requirement id="N3"' in x and "<derive_requirements>" not in x for x in later)
    assert [(r["id"], r["status"]) for r in a["ledger"]["requirements"]] == [("N1", "full"), ("N2", "full"),
                                                                             ("N3", "full")]
    repair = next(i["content"] for i in agent.seen[5] if isinstance(i, dict) and i.get("role") == "user"
                  and "בדיקת האימות" in str(i.get("content")))
    assert RATE_ASK in repair and "calculate" in repair


def test_a_calculation_that_still_fails_is_stated_as_not_completed_not_as_not_found(client, office, monkeypatch):
    steps = [*_ae4_steps(office.doc)[:4], final(FIRST, documents=[office.doc]),
             [call("calculate", expression="C1 / (V1 - V1)", label="שיעור הרווח מההכנסות", justification=None)],
             final(FIRST, documents=[office.doc])]
    a, agent = _ask(client, office, monkeypatch, steps, _judge(ASKS))
    assert agent.tool_outputs(6)[-1].startswith("שגיאה")
    assert a["status"] == "partial"
    # the answer as verified, then the server's one gap paragraph (KTD4): the reason is said once, after it
    assert a["markdown"].startswith("ההכנסות הכוללות הן 12,450,000 ₪ [V1].")
    assert [line for line in a["markdown"].splitlines() if RATE_ASK in line] == [
        "**שיעור הרווח מההכנסות**: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."]
    assert "לא נמצא בחיפוש" not in a["markdown"]
    (missing,) = a["verification"]["completeness"]["missing"]
    assert missing["id"] == "N3" and missing["reason"] == "calculation_incomplete"
    # the payload keeps the gap paragraph grouped by reason, and each component's outcome for the UI to expand
    assert a["gaps"] == [{"reason": "calculation_incomplete", "reason_text": "החישוב לא הושלם", "components": ["N3"],
                          "texts": [RATE_ASK], "text": "**שיעור הרווח מההכנסות**: החישוב נכשל על הנתונים שנמצאו, "
                                                       "ולכן התוצאה לא הושלמה."}]
    assert [(c["id"], c["status"], c["limitation"], c["stated"]) for c in a["components"]] == [
        ("N1", "full", None, False), ("N2", "full", None, False), ("N3", "not_answered", "calculation_incomplete", True)]
    assert a["verification"]["completeness"]["status"] == "partial"
    assert a["verification"]["correctness"] == "verified"  # what the answer claims is correct, only incomplete


def test_an_answer_that_declares_no_parts_gets_requirements_from_the_judge(client, office, monkeypatch):
    steps = [[call("outline", document=office.doc)],
             lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
             [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"), "סה״כ הכנסות")],
             final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].", documents=[office.doc])]
    asks = [(INCOME_ASK, "ההכנסות הכוללות", ("הכנסות",)), ("שטח המגרש", "שטח המגרש", (CAPTION,))]
    a, _ = _ask(client, office, monkeypatch, steps, _judge(asks), question="מה ההכנסות הכוללות ומה שטח המגרש?")
    # the table it was looked for in was read to its end: "not present in the part read", naming it (R8)
    assert a["markdown"].startswith("ההכנסות הכוללות הן 12,450,000 ₪ [V1].")
    assert a["markdown"].endswith(f"**שטח המגרש** לא מופיע בטבלה \"{CAPTION}\" שנבדק [S1].")
    assert a["status"] == "partial"
    assert [(m["id"], m["reason"]) for m in a["verification"]["completeness"]["missing"]] == [
        ("N2", "not_in_part_read")]


def test_a_value_found_but_written_as_not_found_is_corrected(client, office, monkeypatch):
    flawed = final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].\nהעלויות הכוללות לא נמצאו בחיפוש.", documents=[office.doc])
    steps = [[call("outline", document=office.doc)],
             lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
             [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"), "סה״כ הכנסות"),
              take("S1", cell("סה\"כ", COST), meaning("cost", role="cost", vat="excluded"), "סה״כ עלויות")],
             flawed, flawed, flawed]
    asks = [(INCOME_ASK, "ההכנסות הכוללות", ("הכנסות",)), ("העלויות הכוללות", "העלויות הכוללות", ("עלויות",))]
    a, _ = _ask(client, office, monkeypatch, steps, _judge(asks), question="מה ההכנסות הכוללות ומה העלויות הכוללות?")
    assert "לא נמצאו בחיפוש" not in a["markdown"] and "לא נמצא בחיפוש" not in a["markdown"]
    assert "**העלויות הכוללות**" in a["markdown"]
    (missing,) = a["verification"]["completeness"]["missing"]
    assert missing["reason"] != "not_found"


def test_an_undeterminable_requirement_is_stated_as_insufficient_to_conclude(client, office, monkeypatch):
    steps = [[call("outline", document=office.doc)],
             lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
             [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"), "סה״כ הכנסות")],
             final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].", documents=[office.doc])]
    asks = [(INCOME_ASK, "ההכנסות הכוללות", ("הכנסות",)), ("האם הפרויקט כדאי", "כדאי", (CAPTION,))]
    a, _ = _ask(client, office, monkeypatch, steps, _judge(asks, absent={"האם הפרויקט כדאי": "undeterminable"}),
                question="מה ההכנסות הכוללות, והאם הפרויקט כדאי?")
    assert a["markdown"].endswith("**האם הפרויקט כדאי**: המסמכים אינם מספיקים כדי להכריע בכך.")
    # "undeterminable" is the judge's evidence state on a component not answered, not a status of its own (KTD3)
    (missing,) = a["verification"]["completeness"]["missing"]
    assert missing["status"] == "not_answered" and missing["reason"] == "not_verifiable"
    (req,) = [r for r in a["ledger"]["requirements"] if r["id"] == "N2"]
    assert req["evidence_state"] == "undeterminable"


def test_an_unmet_style_instruction_triggers_a_repair_round_that_changes_the_answer_with_no_search_call(
        client, office, monkeypatch):
    """KTD2: an instruction is checked against the shown answer; unmet, it sends the answer back to be changed —
    never to search — and a changed answer that meets it is complete."""
    style = "כתיבה בטבלה"
    plain = "ההכנסות הכוללות הן 12,450,000 ₪ [V1]."
    tabled = "| נתון | סכום |\n|---|---|\n| ההכנסות הכוללות | 12,450,000 ₪ [V1] |"
    steps = [[call("outline", document=office.doc)],
             lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
             [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"), "סה״כ הכנסות")],
             final(plain, documents=[office.doc]), final(tabled, documents=[office.doc])]

    def judge(input: str) -> dict:
        units = dict((int(i), t) for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>',
                                                         input, re.S))
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        table = any(t.startswith("|") for t in units.values())  # the instruction is met by a table
        gives = [i for i, t in units.items() if "12,450,000" in t]
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": [requirement(id=ids[0], units=gives),
                                 requirement(id=ids[1], status="full" if table else "missing")]}

    agent = ScriptedAgent(steps, judge=judge, request=[component(INCOME_ASK),
                                                       component(style, "instruction", aspect="layout")])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), f"מה ההכנסות הכוללות? {style}.")["answer"]
    repair = next(i["content"] for i in agent.seen[4] if isinstance(i, dict) and i.get("role") == "user"
                  and "בדיקת האימות" in str(i.get("content")))
    assert style in repair and "לחפש" not in repair
    assert len(agent.seen) == 5  # the repair round answered at once: no tool was called, no search
    assert a["markdown"] == tabled and a["status"] == "answered"
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 2, "missing": []}
