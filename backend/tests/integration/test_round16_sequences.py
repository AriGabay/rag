"""Text-only evidence end to end (round 16 U7: R1-R6, R13; AE1, AE3; KTD1-KTD4).

A part of the question that no registered value, measurement or calculation backs, only a passage of text the turn read,
is completed in the answer: the scripted agent reads a passage and answers without one part; the scripted judge scores
the component ``partial`` and names ``support`` (the S# and a quote of that passage); the server validates it
(``REQ_TEXT_FOUND``), the repair step runs with ``tool_choice`` none and calls no search or read, and the repaired answer
carries the part with a citation to the passage. Four shapes (R13): a professional explanation, a condition, a table row
and a comparison between two appraisals, each completed with the attribution of its own passage. A last scenario runs
the whole chain of U2 and U3: the repair adds the part and rephrases away a verified claim, and the final answer holds
both.

The model and the judge are scripted: these tests prove what the server does with the judge's scores and the model's
repair, not the judgement of either. Synthetic documents only (office B); every name and figure is invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import _outputs, handle_of
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


# --- documents -------------------------------------------------------------------------------------------------------


def block_document(office, title: str, sections: list[tuple[str, list[str]]], sha: str, source_kind: str = "appraisal",
                   table: dict | None = None) -> str:
    """A one-page document of ``sections`` (a heading and its paragraphs each), and, when ``table`` is given
    (``{"section", "caption", "headers", "rows"}``), a table block after the paragraphs of its section, with its stored
    structure (what ``read(table=...)`` renders). Returns the document id."""
    doc, ver = make_document(office, office.default_group_id, title, sha=sha, source_kind=source_kind)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        index = 0

        def block(kind: str, body: str, section: str, table_index: int | None = None) -> None:
            nonlocal index
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": index, "k": kind, "s": section, "sp": [section], "t": body, "ti": table_index})
            index += 1

        for heading, paragraphs in sections:
            block("heading", heading, heading)
            for p in paragraphs:
                block("paragraph", p, heading)
            if table and table["section"] == heading:
                block("table", table["caption"], heading, 0)
        if table:
            structure = {"headers": table["headers"], "caption": table["caption"], "title": [], "notes": [],
                         "section": table["section"], "block_index": index - 1,
                         "rows": [{"cells": r} for r in table["rows"]]}
            conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                              " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                         {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc)


# --- the scripted judge and agent -----------------------------------------------------------------------------------


def _shown(input: str) -> str:
    """The judge's input without its units and its requirements: what it is shown of the sources (``<sources>``) and of the
    passages the turn read (``<read_passages>``), where a quote is looked for."""
    input = re.sub(r"<unit index.*?</unit>", "", input, flags=re.S)
    return re.sub(r"<requirement .*?</requirement>|<hint>.*?</hint>", "", input, flags=re.S)


def _source_line(input: str, marker: str) -> tuple[str, str] | None:
    """``(S#, line)``: the first line of a shown source or read passage that holds ``marker``, and the id of the element
    it stands in; ``None`` when the call shows no such line."""
    sid = ""
    for ln in _shown(input).splitlines():
        if m := re.match(r'\s*<(?:source|read_passage) id="(S\d+)"', ln):
            sid = m.group(1)
        if sid and marker in ln and not ln.lstrip().startswith("<"):
            return sid, ln.strip()
    return None


def judge_for(parts: dict[str, tuple[str, str | None]], seen: list | None = None, status: str = "partial"):
    """A judge that supports every unit and scores each requirement ``text -> (marker, quote)``: ``full`` when a unit of the
    answer holds the marker (the words that name the omitted part) next to the requirement's own keyword, else ``status``
    with the support the passage shows — ``quote`` (a sentence of it), or, when ``None``, the shown line that holds the
    marker (a table row as the call renders it). A requirement of no ``parts`` entry is ``full``."""
    def judge(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = dict((int(i), t) for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input,
                                                         re.S))
        verdicts = [{"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []} for i in units]
        asked = re.findall(r'<requirement id="(N[\d.]+)"[^>]*>(.*?)</requirement>', input, re.S)
        scored = []
        for rid, body in asked:
            key = next((k for k in parts if k in body), None)
            if key is None or any(parts[key][0] in t for t in units.values()):
                scored.append(requirement(id=rid, units=list(units)))
                continue
            marker, quote = parts[key]
            found = _source_line(input, marker)
            support = [{"id": found[0], "quote": quote or found[1]}] if found else []
            scored.append(requirement(id=rid, status=status, units=list(units)) | {"missing": key, "support": support})
        return {"verdicts": verdicts, "requirements": scored} if asked else {"verdicts": verdicts}
    return judge


class RecordingAgent(ScriptedAgent):
    """A scripted agent that keeps the ``tool_choice`` each step was asked with."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.choices: list = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.choices.append(kw.get("tool_choice"))
        return super().agent_step(instructions, items, tools, final_schema, **kw)


def open_section(heading: str):
    """A scripted step that reads the section named ``heading`` through the handle the first step's outline gave."""
    return lambda items: [read(section=handle_of(_outputs(items)[0], heading))]


def sid_in(items, document: str, needle: str) -> str:
    """The S# of the source of ``document`` whose text holds ``needle``, among the tool outputs the model saw."""
    out = "\n".join(i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")
    return re.search(rf'<source id="(S\d+)" document_id="{document}"[^>]*>[^<]*?{re.escape(needle)}', out, re.S).group(1)


def repair_prompt(agent: ScriptedAgent) -> str:
    """The server's repair request the last model step was shown."""
    return next(i["content"] for i in reversed(agent.seen[-1]) if isinstance(i, dict) and i.get("role") == "user"
                and "בדיקת האימות" in str(i.get("content")))


def repair_calls(agent: ScriptedAgent) -> list[str]:
    """The tool calls the last model step added to the items of the one before it: none for a step with tools off."""
    before = len(agent.seen[-2]) if len(agent.seen) > 1 else 0
    return [i["name"] for i in agent.seen[-1][before:] if isinstance(i, dict) and i.get("type") == "function_call"]


def checks_of(client, m: dict) -> list[list[tuple[str, str | None]]]:
    """The checks of each verification round of the turn, as ``(check, component)`` (the diagnostics' ``rounds``)."""
    rounds = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["rounds"]
    return [[(p["check"], p["component"]) for p in r] for r in rounds]


def assert_text_completion(client, agent: ScriptedAgent, m: dict, *, steps: int) -> dict:
    """The shape every scenario shares: the turn is done; the first verification round holds one ``text_found`` problem
    (``REQ_TEXT_FOUND``) and nothing else; the last model step is the repair step, with tools off and no search or read
    call in it; completeness is full and the answer is verified."""
    assert m["status"] == "done", m
    a = m["answer"]
    assert checks_of(client, m)[0] == [("text_found", a["ledger"]["requirements"][-1]["id"])]
    assert len(agent.seen) == steps and agent.choices[-1] == "none" and agent.choices[:-1] == [None] * (steps - 1)
    assert not repair_calls(agent)
    assert a["verification"]["completeness"]["status"] == "full" and a["verification"]["completeness"]["missing"] == []
    assert a["verification"]["correctness"] == "verified"
    assert "לא נמצא" not in a["markdown"] and "לא אותר" not in a["markdown"]
    return a


def login_b(client) -> None:
    login(client, "admin-b@example.test")


# --- 1. a professional explanation ----------------------------------------------------------------------------------

STD_TITLE = "תקן בדיקה סינתטי להיוון הכנסה"
STD_RATE_HEAD, STD_REASON_HEAD = "3. שיעור ההיוון", "4. נימוק לשימוש בשיעור שוק"
STD_RATE = "שיעור ההיוון הוא המקדם שבו ממירים הכנסה שנתית לשווי נכס."
STD_REASON = "הסיבה לשימוש בשיעור שוק היא שהוא משקף את התשואה שמשקיע מקבל מנכס חלופי בסיכון דומה."
STD_QUESTION = "מהו שיעור ההיוון לפי התקן, ומדוע משתמשים בו כשיעור שוק?"
STD_RATE_ASK, STD_REASON_ASK = "מהו שיעור ההיוון", "הסיבה לשימוש בשיעור שוק"
STD_FIRST = "שיעור ההיוון הוא המקדם שבו ממירים הכנסה שנתית לשווי נכס [S1]."
STD_FULL = STD_FIRST + "\nהסיבה לשימוש בשיעור שוק היא שהוא משקף את התשואה שמשקיע מקבל מנכס חלופי בסיכון דומה [S2]."


@pytest.fixture
def standard(office) -> str:
    return block_document(office, STD_TITLE, [(STD_RATE_HEAD, [STD_RATE]), (STD_REASON_HEAD, [STD_REASON])], "5" * 64,
                          source_kind="standard")


def test_a_reason_the_standard_states_and_the_answer_omits_is_added_from_its_passage_by_a_step_without_tools(
        client, office, standard, monkeypatch):
    doc = standard
    steps = [[call("outline", document=doc)],
             open_section(STD_RATE_HEAD),
             open_section(STD_REASON_HEAD),
             final(STD_FIRST, documents=[doc]),
             final(STD_FULL, documents=[doc])]
    seen: list[str] = []
    agent = RecordingAgent(steps, judge=judge_for({STD_REASON_ASK: ("הסיבה לשימוש", STD_REASON)}, seen),
                           request=[component(STD_RATE_ASK), component(STD_REASON_ASK)], intent="professional")
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), STD_QUESTION)
    a = assert_text_completion(client, agent, m, steps=5)
    # the judge was shown the passage the answer left uncited, in the read-passages block, and named it
    assert '<read_passage id="S2"' in seen[0] and "<read_passages>" in seen[0]
    repair = repair_prompt(agent)
    assert "S2" in repair and STD_REASON.rstrip(".") in repair and "חיפוש" in repair
    assert STD_REASON[:-1] + " [S2]" in a["markdown"]
    assert {s["source_kind"] for s in a["sources"]} == {"standard"}


# --- 2. a condition ------------------------------------------------------------------------------------------------

COND_TITLE = "שומת הדגמה לשיעור היוון ותנאיו"
COND_HEAD = "5. שיעור ההיוון"
COND_RATE = "שיעור ההיוון לדירות מגורים שנקבע בשומה הוא 5.5%."
COND_CONDITION = "שיעור זה חל רק בתנאי שהנכס מושכר במלואו ביום הקובע."
COND_QUESTION = "מהו שיעור ההיוון שנקבע לדירות מגורים, ובאיזה תנאי הוא חל?"
COND_RATE_ASK, COND_CONDITION_ASK = "שיעור ההיוון לדירות מגורים", "התנאי לתחולת שיעור ההיוון"
COND_FIRST = "שיעור ההיוון לדירות מגורים שנקבע בשומה הוא 5.5% [S1]."
COND_FULL = COND_FIRST + "\nשיעור זה חל רק בתנאי שהנכס מושכר במלואו ביום הקובע [S1]."


def test_a_condition_the_passage_states_for_a_rate_and_the_answer_omits_is_added_from_it_by_a_step_without_tools(
        client, office, monkeypatch):
    doc = block_document(office, COND_TITLE, [(COND_HEAD, [COND_RATE, COND_CONDITION])], "6" * 64)
    steps = [[call("outline", document=doc)], open_section(COND_HEAD),
             final(COND_FIRST, documents=[doc]), final(COND_FULL, documents=[doc])]
    agent = RecordingAgent(steps, judge=judge_for({COND_CONDITION_ASK: ("חל רק בתנאי", COND_CONDITION)}),
                           request=[component(COND_RATE_ASK), component(COND_CONDITION_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), COND_QUESTION)
    a = assert_text_completion(client, agent, m, steps=4)
    repair = repair_prompt(agent)
    assert "S1" in repair and COND_CONDITION.rstrip(".") in repair and "חיפוש" in repair
    assert COND_CONDITION[:-1] + " [S1]" in a["markdown"] and COND_RATE[:-1] in a["markdown"]


# --- 3. a table row ------------------------------------------------------------------------------------------------

TABLE_TITLE = "שומת הדגמה עם טבלת שיעורי היוון"
TABLE_HEAD, TABLE_CAPTION = "6. שיעורי היוון לפי שימוש", "שיעורי היוון לפי שימוש"
TABLE_ROWS = [["משרדים", "6.5%"], ["מסחר", "7.0%"], ["חניה", "8.25%"], ["מחסנים", "9.0%"]]
TABLE_QUESTION = "מהו שיעור ההיוון שנקבע לכל סוג שימוש?"
TABLE_ASK = "שיעור ההיוון לכל סוג שימוש"
TABLE_FIRST = "שיעור היוון למשרדים 6.5% [S1].\nשיעור היוון למסחר 7.0% [S1].\nשיעור היוון לחניה 8.25% [S1]."
TABLE_FULL = TABLE_FIRST + "\nשיעור היוון למחסנים 9.0% [S1]."


def test_a_row_of_a_table_the_turn_read_and_the_answer_omits_is_added_from_the_table_by_a_step_without_tools(
        client, office, monkeypatch):
    doc = block_document(office, TABLE_TITLE, [(TABLE_HEAD, ["השיעורים שנקבעו לפי סוג שימוש מפורטים בטבלה."])], "7" * 64,
                         table={"section": TABLE_HEAD, "caption": TABLE_CAPTION, "headers": ["סוג שימוש", "שיעור היוון"],
                                "rows": TABLE_ROWS})
    steps = [[call("outline", document=doc)],
             lambda items: [read(table=handle_of(_outputs(items)[0], TABLE_CAPTION))],
             final(TABLE_FIRST, documents=[doc]), final(TABLE_FULL, documents=[doc])]
    agent = RecordingAgent(steps, judge=judge_for({TABLE_ASK: ("מחסנים", None)}), request=[component(TABLE_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), TABLE_QUESTION)
    a = assert_text_completion(client, agent, m, steps=4)
    repair = repair_prompt(agent)
    assert "S1" in repair and "מחסנים" in repair and "9.0%" in repair
    assert "שיעור היוון למחסנים 9.0% [S1]" in a["markdown"]


# --- 4. a comparison between two appraisals --------------------------------------------------------------------------

OAK_TITLE, GAFEN_TITLE = "שומת האלון 3 (הדגמה סינתטית)", "שומת הגפן 8 (הדגמה סינתטית)"
CMP_HEAD = "4. פרמטרי ההיוון וההשכרה"
OAK_RATE, OAK_TERM = "שיעור ההיוון בשומת האלון הוא 6.0%.", "תקופת ההשכרה בשומת האלון היא 3 שנים."
GAFEN_RATE, GAFEN_TERM = "שיעור ההיוון בשומת הגפן הוא 7.5%.", "תקופת ההשכרה בשומת הגפן היא 5 שנים."
CMP_QUESTION = "השווה בין שומת האלון 3 לשומת הגפן 8: מהם שיעור ההיוון ותקופת ההשכרה בכל אחת?"
CMP_RATES_ASK, CMP_OAK_ASK, CMP_GAFEN_ASK = ("שיעור ההיוון בכל אחת מהשומות", "תקופת ההשכרה בשומת האלון",
                                             "תקופת ההשכרה בשומת הגפן")


def _comparison(client, office, monkeypatch, answers):
    """The two appraisals read, and the answers ``answers(items) -> [first, repaired]`` they are given in turn."""
    oak = block_document(office, OAK_TITLE, [(CMP_HEAD, [OAK_RATE, OAK_TERM])], "8" * 64)
    gafen = block_document(office, GAFEN_TITLE, [(CMP_HEAD, [GAFEN_RATE, GAFEN_TERM])], "9" * 64)

    def pick(n: int):
        return lambda items: final(answers(items, oak, gafen)[n], documents=[oak, gafen])

    steps = [[call("outline", document=oak)], [call("outline", document=gafen)],
             lambda items: [read(section=handle_of(_outputs(items)[0], CMP_HEAD))],
             lambda items: [read(section=handle_of(_outputs(items)[1], CMP_HEAD))], pick(0), pick(1)]
    judge = judge_for({CMP_GAFEN_ASK: ("תקופת ההשכרה בשומת הגפן", GAFEN_TERM)})
    agent = RecordingAgent(steps, judge=judge, request=[component(CMP_RATES_ASK), component(CMP_OAK_ASK),
                                                        component(CMP_GAFEN_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    return agent, send(client, new_conversation(client), CMP_QUESTION), oak, gafen


def test_an_item_of_the_second_appraisal_that_was_read_and_omitted_is_added_with_the_attribution_of_its_own_passage(
        client, office, monkeypatch):
    def answers(items, oak, gafen):
        a, g = sid_in(items, oak, OAK_RATE), sid_in(items, gafen, GAFEN_RATE)
        first = (f"בשומת האלון שיעור ההיוון הוא 6.0% [{a}].\nבשומת הגפן שיעור ההיוון הוא 7.5% [{g}].\n"
                 f"תקופת ההשכרה בשומת האלון היא 3 שנים [{a}].")
        return [first, first + f"\nתקופת ההשכרה בשומת הגפן היא 5 שנים [{g}]."]

    agent, m, oak, gafen = _comparison(client, office, monkeypatch, answers)
    a = assert_text_completion(client, agent, m, steps=6)
    other = sid_in(agent.seen[-1], oak, OAK_RATE)
    own = sid_in(agent.seen[-1], gafen, GAFEN_RATE)
    assert own != other
    repair = repair_prompt(agent)
    assert own in repair and GAFEN_TERM.rstrip(".") in repair and OAK_TERM.rstrip(".") not in repair
    (added,) = [ln for ln in a["markdown"].splitlines() if "תקופת ההשכרה בשומת הגפן" in ln]
    assert "5 שנים" in added and f"[{own}]" in added and f"[{other}]" not in added
    assert {s["document_id"] for s in a["sources"] if f"[{s['id']}]" in added} == {gafen}


# --- 5. the whole chain: the repair adds the part and rephrases away a verified claim ------------------------------

CHAIN_TITLE = "שומת הדגמה לשיעורי היוון שונים"
CHAIN_HEAD = "7. שיעורי ההיוון"
CHAIN_LINES = ["שיעור היוון למשרדים 6.5%", "שיעור היוון למסחר 7.0%", "שיעור היוון לחניה 8.25%", "שיעור היוון למחסנים 9.0%"]
CHAIN_QUESTION = "אילו שיעורי היוון נקבעו בשומה, לפי סוג השימוש?"
CHAIN_ASK = "שיעורי ההיוון שנקבעו"
CHAIN_FIRST = "\n".join(f"{x} [S1]." for x in CHAIN_LINES[:3])
CHAIN_REPAIRED = "\n".join(f"{x} [S1]." for x in CHAIN_LINES[1:])  # the warehouses are added; the offices rate is gone


def test_a_repair_that_adds_the_omitted_part_and_rephrases_away_a_verified_claim_ends_with_both(client, office,
                                                                                            monkeypatch):
    doc = block_document(office, CHAIN_TITLE, [(CHAIN_HEAD, [f"{x}." for x in CHAIN_LINES])], "a" * 64)
    steps = [[call("outline", document=doc)], open_section(CHAIN_HEAD), final(CHAIN_FIRST, documents=[doc]),
             final(CHAIN_REPAIRED, documents=[doc]), final(CHAIN_REPAIRED, documents=[doc])]
    agent = RecordingAgent(steps, judge=judge_for({CHAIN_ASK: ("למחסנים", CHAIN_LINES[3] + ".")}),
                           request=[component(CHAIN_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), CHAIN_QUESTION)
    a = assert_text_completion(client, agent, m, steps=4)
    for line in CHAIN_LINES:
        assert f"{line} [S1]" in a["markdown"], a["markdown"]
    assert a["markdown"].count("6.5%") == 1 and a["markdown"].count("9.0%") == 1
    # the lost claim is restored, not discarded: the round entry names it, and no round says it was dropped
    flat = [c for r in checks_of(client, m) for c, _ in r]
    assert flat.count("repair_restored_supported") == 1 and "repair_lost_supported" not in flat
