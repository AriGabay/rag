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
# The table's own note says the amounts are without VAT (``add_document``), a status the sentences above leave out. The
# server therefore completes the sentence that gives the income with that note, verbatim and cited (round 18 U5, KTD1, R4);
# the value and its citation stay.
VAT_NOTE = "(התנאי לפי המקור: הסכומים ללא מע\"מ. [S1])"
INCOME_SENTENCE = f"ההכנסות הכוללות הן 12,450,000 ₪ {VAT_NOTE} [V1]."


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
    assert a["markdown"].startswith(INCOME_SENTENCE)
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
    # the table's note on VAT is written next to the amount the table gives: the style instruction is met and the value stays
    assert a["markdown"] == tabled.replace("₪ [V1]", f"₪ {VAT_NOTE} [V1]") and a["status"] == "answered"
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 2, "missing": []}


def test_a_requirement_a_repair_round_completed_is_marked_attempted_in_the_stored_rounds_and_the_answer_is_unchanged(
        client, office, monkeypatch):
    """Round 23 U0 (KTD11): the rounds the diagnostics keep were serialised before the repair prompt, so a repaired problem showed
    ``repair_attempted`` false, and a requirement (no unit) was never stamped. The answer is the one ``test_ae6`` asserts."""
    steps = [*_ae4_steps(office.doc)[:4], final(FIRST, documents=[office.doc]),
             [call("calculate", expression="C1 / V1", label="שיעור הרווח מההכנסות", justification=None)],
             final(COMPLETE, documents=[office.doc])]
    agent = ScriptedAgent(steps, judge=_judge(ASKS))
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["status"] == "answered" and a["markdown"].endswith("שיעור הרווח מההכנסות הוא 12.3% [C2].")
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 3, "missing": []}
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()
    first, *later = d["rounds"]
    (asked,) = [p for p in first if p["kind"] == "requirement"]
    assert asked["component"] == "N3" and asked["repair_attempted"] is True  # the prompt named the rate the answer omitted
    assert all(not p["repair_attempted"] for r in later for p in r if p["kind"] == "requirement")  # nothing left to name
    gates = d["timing"]["repair_gate"]
    assert gates[0]["decision"] == "repair" and gates[0]["repairable_kinds"] == {"requirement": 1}
    assert gates[-1]["decision"] == "refused" and "settled" in gates[-1]["refused_by"]


# --- a calculation component is full only through a computation (round 7 KTD3, R6, R7) -------------------------------

FORMULA_ASK, SCENARIO_ASK = "איך מחושב הרווח הכולל", "הרווח אם כל העלויות יעלו ב-5%"
SCENARIO_QUESTION = "איך מחושב הרווח הכולל, ומה יקרה אם כל העלויות יעלו ב-5% וההכנסות יישארו קבועות?"
EXPLAINED = ("הרווח הכולל מחושב כהכנסות פחות העלויות [V1][V2]. אם כל העלויות יעלו ב-5% [A1] וההכנסות יישארו "
             "קבועות, הרווח יקטן; הסכום החדש לא חושב כאן.")
COMPUTED = ("הרווח הכולל מחושב כהכנסות פחות העלויות [V1][V2]. אם כל העלויות יעלו ב-5% [A1] וההכנסות יישארו "
            "קבועות, הרווח יהיה 1,530,000 ₪ [C1].")
SCENARIO_ASKS = [(FORMULA_ASK, "מחושב", ("הכנסות", "עלויות")),
                 (SCENARIO_ASK, "יעלו ב-5%", ("הכנסות", "עלויות", "עליית"))]
SCENARIO_COMPONENTS = [component(FORMULA_ASK), component(SCENARIO_ASK, kind="calculation")]


def _scenario_ask(client, office, monkeypatch, steps: list) -> tuple[dict, ScriptedAgent]:
    agent = ScriptedAgent(steps, judge=_judge(SCENARIO_ASKS), request=SCENARIO_COMPONENTS)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), SCENARIO_QUESTION)
    assert m["status"] == "done", m
    return m["answer"], agent


def test_a_calculation_the_judge_scored_full_with_no_computation_is_sent_to_a_repair_round_to_compute_it(
        client, office, monkeypatch):
    steps = [*_ae4_steps(office.doc)[:3], final(EXPLAINED, documents=[office.doc]),
             # the repair round computes it, then answers with the result
             [call("calculate", expression="V1 - V2*(1+A1%)", label="הרווח בתרחיש", justification=None)],
             final(COMPUTED, documents=[office.doc])]
    a, agent = _scenario_ask(client, office, monkeypatch, steps)
    repair = next(i["content"] for i in agent.seen[4] if isinstance(i, dict) and i.get("role") == "user"
                  and "בדיקת האימות" in str(i.get("content")))
    assert SCENARIO_ASK in repair and "calculate" in repair and FORMULA_ASK not in repair
    assert agent.tool_outputs(5)[-1].startswith("{") and '"C1"' in agent.tool_outputs(5)[-1]
    assert a["status"] == "answered" and "1,530,000 ₪ [C1]" in a["markdown"]
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 2, "missing": []}


def test_a_calculation_still_not_computed_after_the_repair_bound_is_partial_with_the_calculation_not_completed(
        client, office, monkeypatch):
    steps = [*_ae4_steps(office.doc)[:3], *[final(EXPLAINED, documents=[office.doc])] * 4]
    a, _ = _scenario_ask(client, office, monkeypatch, steps)
    assert a["verification"]["removed"] == 0 and a["verification"]["correctness"] == "verified"
    assert [(c["id"], c["status"], c["limitation"]) for c in a["components"]] == [
        ("N1", "full", None), ("N2", "partial", "calculation_incomplete")]
    assert a["verification"]["completeness"]["status"] == "partial"


def test_a_calculation_filled_by_a_unit_citing_its_computation_stays_full_with_no_repair_round(
        client, office, monkeypatch):
    steps = [*_ae4_steps(office.doc)[:4], final(COMPUTED, documents=[office.doc])]
    a, agent = _scenario_ask(client, office, monkeypatch, steps)
    assert not agent.steps  # no repair round was asked for
    assert [(c["id"], c["status"]) for c in a["components"]] == [("N1", "full"), ("N2", "full")]
    assert a["status"] == "answered"


# --- round 16 U2 (AE1): a rate listed in a passage the turn read is added from it, with its citation, by a repair step ---------

RATES_TITLE = "שומת הדגמה לשיעורי היוון"
RATES_SECTION = "6. שיעורי היוון"
RATE_PARAGRAPHS = ["שיעורי ההיוון שנקבעו בשומה:", "שיעור היוון למשרדים 6.5%", "שיעור היוון למסחר 7.0%",
                   "שיעור היוון לחניה 8.25%", "שיעור היוון למחסנים 7.0%"]  # the fourth rate repeats the commerce rate's value
RATES_ASK = "שיעורי ההיוון שנקבעו"
RATES_QUESTION = "אילו שיעורי היוון נקבעו בשומה, לפי סוג השימוש?"
THREE_RATES = ("שיעור היוון למשרדים 6.5% [S1].\nשיעור היוון למסחר 7.0% [S1].\nשיעור היוון לחניה 8.25% [S1].")
ALL_RATES = THREE_RATES + "\nשיעור היוון למחסנים 7.0% [S1]."


def _rates_document(office) -> str:
    import json as _json

    from sqlalchemy import text

    from app.db import tenant_tx
    from tests.factories import make_document

    doc, ver = make_document(office, office.default_group_id, RATES_TITLE, sha="d" * 64)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": _json.dumps({"reading_id": "reading-1"})})
        blocks = [("heading", RATES_SECTION), *(("paragraph", t) for t in RATE_PARAGRAPHS)]
        for i, (kind, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": RATES_SECTION, "sp": [RATES_SECTION], "t": t})
    return str(doc)


def _rates_judge(seen: list):
    """Scores the one component ``full`` when the answer gives every rate, else ``partial`` with the quote of the omitted
    rate's line, taken from the passage the call shows (the cited S1 or the read-passages block)."""
    def judge(input: str) -> dict:
        seen.append(input)
        units = dict((int(i), t) for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input,
                                                         re.S))
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        verdicts = [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]
        if not ids:
            return {"verdicts": verdicts}
        line = next((ln for ln in input.splitlines() if "למחסנים" in ln and not ln.startswith("<unit")
                     and "[S1]" not in ln), "")
        given = any("למחסנים" in t for t in units.values())
        scored = (requirement(id=ids[0], units=list(units)) if given else
                  requirement(id=ids[0], status="partial", units=list(units))
                  | {"missing": "שיעורי ההיוון", "support": [{"id": "S1", "quote": line.strip()}]})
        return {"verdicts": verdicts, "requirements": [scored]}
    return judge


class _RecordingAgent(ScriptedAgent):
    """A scripted agent that keeps the ``tool_choice`` each step was asked with."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.choices: list = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.choices.append(kw.get("tool_choice"))
        return super().agent_step(instructions, items, tools, final_schema, **kw)


def test_ae1_a_rate_the_passage_lists_and_the_answer_omits_is_added_from_it_by_a_repair_step_without_tools(
        client, office, monkeypatch):
    doc = _rates_document(office)
    steps = [[call("outline", document=doc)],
             lambda items: [read(section=handle_of(_last_output(items), RATES_SECTION))],
             final(THREE_RATES, documents=[doc]),
             final(ALL_RATES, documents=[doc])]
    seen: list[str] = []
    agent = _RecordingAgent(steps, judge=_rates_judge(seen), request=[component(RATES_ASK)])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), RATES_QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    # the repair step (the fourth model step) had tools off and called none; the first three kept them
    assert agent.choices == [None, None, None, "none"] and len(agent.seen) == 4
    assert not [i for i in agent.seen[3][len(agent.seen[2]):] if isinstance(i, dict) and i.get("type") == "function_call"]
    repair = next(i["content"] for i in agent.seen[3] if isinstance(i, dict) and i.get("role") == "user"
                  and "בדיקת האימות" in str(i.get("content")))
    assert "S1" in repair and "שיעור היוון למחסנים 7.0%" in repair and "חיפוש" in repair
    assert "למחסנים 7.0% [S1]" in a["markdown"] and "לא נמצא" not in a["markdown"] and "לא אותר" not in a["markdown"]
    assert a["verification"]["completeness"] == {"status": "full", "requirements": 1, "missing": []}
    assert a["verification"]["correctness"] == "verified"
    assert [c.purpose.value for c in agent.calls].count("verify") >= 2
    # the diagnostics (owner and admin only) record, per round and component, what the judge returned and what the server did
    assert "requirements_trace" not in m and "requirements_trace" not in a
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics")
    assert d.status_code == 200
    trace = d.json()["requirements_trace"]
    assert "requirements_trace" not in (d.json()["resolution"] or {})
    assert [t["round"] for t in trace][:2] == [1, 2]
    first = next(c for c in trace[0]["components"] if c["id"] == "N1")
    assert first["final_status"] == "partial"
    (vote,) = first["votes"]
    assert vote["status"] == "partial" and set(vote["passages"]) == {"rendered", "chars", "ids"}
    (entry,) = vote["support"]
    assert entry["id"] == "S1" and "למחסנים" in entry["quote"] and len(entry["quote"]) <= 160
    assert entry["outcome"] == "accepted"
    last = next(c for c in trace[-1]["components"] if c["id"] == "N1")
    assert last["final_status"] == "full" and last["votes"][-1]["status"] == "full"


def test_a_part_the_repair_step_still_does_not_add_is_stated_as_read_and_not_used(client, office, monkeypatch):
    doc = _rates_document(office)
    steps = [[call("outline", document=doc)],
             lambda items: [read(section=handle_of(_last_output(items), RATES_SECTION))],
             *[final(THREE_RATES, documents=[doc])] * 4]
    agent = _RecordingAgent(steps, judge=_rates_judge([]), request=[component(RATES_ASK)])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    a = send(client, new_conversation(client), RATES_QUESTION)["answer"]
    assert a["status"] == "partial"
    (c,) = a["components"]
    assert c["limitation"] == "not_verifiable" and c["stated"] is True
    gap = a["markdown"][len(THREE_RATES):].strip()
    assert RATES_TITLE in gap and RATES_SECTION in gap and "[S1]" in gap and "לא נעשה בו שימוש" in gap
    assert "לא נמצא" not in gap and "לא אותר" not in gap
