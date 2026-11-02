"""Kind-aware verification over stored standards (round 10 U9: R31, R33, R34, R35; KTD14).

The sources are the ones the tools return from a synthetic standard stored through ``professional_file``: a passage
carries the limits and the wording of the section it is in, read from the stored section structure; the verifier's
header shows them to the judge; a property datum cited only to such a source fails before the judge; and a professional
turn written through the whole chat path keeps its supported sentences and gets no gap line about a property. The judge
is scripted. Synthetic data only: every standard says so and every number is invented."""

from __future__ import annotations

import json
import re

import pytest

from app.chat import tools as T
from app.chat import verify
from app.chat.engine import FinalAnswer
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.professional_files import (
    APPROACH,
    EXCEPTION,
    RECOMMENDATION,
    professional_file,
    standard_blocks,
)
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

WORKED = "דוגמת חישוב סינתטית: נכס בשטח 12 מ\"ר ושווי 2,400,000 ₪."
PATH = ["2. שיטות", "2.1 גישת ההשוואה"]
CHECK = "property_datum_from_standard"


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד א", "admin-a@example.test")


def standard(office, with_example: bool = False):
    blocks = standard_blocks("98.1")
    if with_example:
        at = next(i for i, b in enumerate(blocks) if b[3] == EXCEPTION)
        blocks.insert(at + 1, ("paragraph", None, PATH, WORKED, 4))
    return professional_file(office, office.default_group_id, blocks, title="תקן מדידה", source_kind="standard")


def workspace(office, intent="professional") -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx())
    ws.intent = intent
    return ws


def run(ws, name, **args) -> str:
    return T.run_tool(ws, name, json.dumps(args, ensure_ascii=False))


def cited(ws, doc, words) -> T.Source:
    return next(s for s in ws.sources.values() if str(s.document_id) == str(doc) and words in s.text)


def answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def supported_judge(seen: list[str] | None = None) -> ScriptedProvider:
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        indexes = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in indexes]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


# --- the section's limits and wording travel with the passage ---------------------------------------------------------

def test_a_passage_of_a_standard_carries_the_limits_and_wording_of_its_section(office):
    std, _ = standard(office)
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    rules = cited(ws, std, APPROACH).rules
    assert rules is not None and any(EXCEPTION in lim for lim in rules["limits"])
    assert rules["modality"]["requirement"] >= 1 and rules["modality"]["exception"] >= 1
    soft = workspace(office)
    run(soft, "search", query=RECOMMENDATION)
    advice = cited(soft, std, RECOMMENDATION).rules
    assert advice["modality"]["recommendation"] >= 1 and advice["modality"]["requirement"] == 0


def test_a_section_read_whole_carries_them_too_and_an_appraisal_carries_none(office):
    std, _ = standard(office)
    prop, _ = professional_file(office, office.default_group_id, [("paragraph", None, [], APPROACH + " א", 1)],
                                title="שומה (סינתטי)", source_kind="appraisal")
    ws = workspace(office, "mixed")
    run(ws, "search", query=APPROACH)
    handle = next(h for h, d in ws.handles.items() if d.get("psection") is not None and d["document_id"] == std)
    run(ws, "read", target={"source": None, "pages": None, "section": handle, "table": None, "cursor": None})
    read = next(s for s in ws.sources.values() if s.status and s.standard)
    assert any(EXCEPTION in lim for lim in read.rules["limits"])
    assert cited(ws, prop, APPROACH + " א").rules is None


def test_the_judges_header_shows_the_real_sections_limit_and_wording(office):
    std, _ = standard(office)
    ws = workspace(office)
    run(ws, "search", query=APPROACH)
    sid = cited(ws, std, APPROACH).sid
    head, body, _ = verify._source_parts(ws, sid)
    assert EXCEPTION in head and "ניסוח חובה" in head and "98.1" in head and "גרסה 2.0" in head
    assert EXCEPTION not in body
    seen: list[str] = []
    verify.verify_answer(supported_judge(seen), answer(f"יש להתאים את מחירי ההשוואה [{sid}]."), ws, "?", [])
    assert EXCEPTION in seen[0]
    soft = workspace(office)
    run(soft, "search", query=RECOMMENDATION)
    head = verify._source_parts(soft, cited(soft, std, RECOMMENDATION).sid)[0]
    assert "המלצה" in head and "אין בו ניסוח חובה" in head


# --- a property datum cited only to the standard ---------------------------------------------------------------------

def test_a_property_datum_cited_to_a_stored_standards_worked_example_is_removed_before_the_judge(office):
    std, _ = standard(office, with_example=True)
    ws = workspace(office)
    run(ws, "search", query="דוגמת חישוב נכס בשטח")
    sid = cited(ws, std, WORKED).sid
    seen: list[str] = []
    md = f"שווי הנכס 2,400,000 ₪ [{sid}].\n\nלפי סעיף 2.1, בדוגמת החישוב שטח של 12 מ\"ר [{sid}]."
    report = verify.verify_answer(supported_judge(seen), answer(md), ws, "?", [])
    assert [(p.check, p.unit.index) for p in report.problems] == [(CHECK, 0)] and report.removed_units() == {0}
    assert '<unit index="0"' not in "".join(seen) and '<unit index="1"' in "".join(seen)


# --- the whole path: a professional turn -------------------------------------------------------------------------------

def test_a_professional_turn_drops_a_property_datum_cited_to_the_standard_and_keeps_the_rest_with_no_property_gap(
        client, office, monkeypatch):
    std, _ = standard(office, with_example=True)

    def final_answer(items) -> dict:
        out = "\n".join(i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")

        def sid(words: str) -> str:
            return re.search(rf'<source id="(S\d+)" document_id="{std}"[^>]*>\n[^\n]*{words}', out).group(1)

        a, w = sid("על השמאי לבסס"), sid("דוגמת חישוב")
        return final(f"על פי סעיף 2.1, יש לבסס את ההערכה על עסקאות השוואה בנכסים דומים [{a}].\n\n"
                     f"שווי הנכס 2,400,000 ₪ [{w}].", documents=[std])

    agent = ScriptedAgent([[call("search", query="על השמאי לבסס את הערכתו על עסקאות השוואה", document_ids=None,
                                 limit=None),
                            call("search", query="דוגמת חישוב נכס בשטח", document_ids=None, limit=None)],
                           final_answer, final_answer, final_answer], intent="professional")
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דורשת גישת ההשוואה בתקן?")
    assert m["status"] == "done", m
    text = m["answer"]["markdown"]
    assert "עסקאות השוואה בנכסים דומים" in text and "2,400,000" not in text
    assert "לא אותרה" not in text and m["answer"]["verification"]["removed"] == 1
    assert m["answer"]["verification"]["removals"][0]["failure_kind"] == "wrong_subject"
