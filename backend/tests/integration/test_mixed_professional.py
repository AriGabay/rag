"""A mixed question reaches the professional pool when no other cited source supports its professional statement
(round 13 U14; R23; KTD18; the Scope Boundaries rule on mixed questions).

Diagnosis (the v16 results of W9-W12, in the round record): in no item was the glossary never searched; three of the four
cited it under a document whose stored title is garbled (the eval's title fragment missed it), and the fourth lost every
property claim to a unit attribution defect. What the product could still do wrong, and what these tests pin:

- once a turn has a unit in focus (a located unit, or the conversation's own) a ``search`` that names no document was
  scoped to that unit whatever its ``pool`` said, so the professional pool was unreachable for a mixed question;
- a mixed answer that cites no professional source, either because the turn never read the professional pool or because
  what it read was left uncited, was verified as it stood. The server now asks once, in the turn, for the professional
  part (``engine.MIXED_UNREAD`` / ``MIXED_UNCITED``); an answer that the appraisal's own text supports stays as it is.

Synthetic data only: the glossary and the appraisal are stored through ``professional_file`` (the synthetic office), every
name and number is invented. The model is scripted."""

from __future__ import annotations

import re

import pytest

from app.chat import engine
from app.chat import tools as T
from app.providers.llm import CallStatus
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_tools_pools import GLOSSARY, appraisal, run, sources_of, workspace
from tests.support.professional_files import READING, professional_file
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

TERM_QUERY = "גישת ההשוואה היא שיטה להערכת שווי"
GLOSSARY_TEXT = GLOSSARY + " (מילון סינתטי)"
UNREAD = engine.MIXED_UNREAD[:40]  # the start of each notice: what a test looks for in the items the model was shown
UNCITED = engine.MIXED_UNCITED.split("(")[0].strip()
QUESTION = "באיזו גישה נקבע השווי בשומה של הרקפות 5, ומה כתוב במילון המקצועי על גישת ההשוואה?"


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד ב", "admin-b@example.test")
    o.appraisal, o.version = appraisal(o)
    o.glossary, _ = professional_file(o, o.default_group_id, [("paragraph", None, [], GLOSSARY_TEXT, 1)],
                                      title="מילון מונחים (סינתטי)", source_kind="professional")
    return o


def sid_of(items, doc: str, words: str) -> str:
    out = "\n".join(i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")
    return re.search(rf'<source id="(S\d+)" document_id="{doc}"[^>]*>\n[^\n]*{words}', out).group(1)


def asked(client, office, monkeypatch, agent, question: str = QUESTION) -> dict:
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    return send(client, new_conversation(client), question)


def nudges(agent, text: str) -> int:
    """How many of the scripted model's steps were shown the given server notice (in its latest items)."""
    return sum(1 for items in agent.seen if any(isinstance(i, dict) and i.get("role") == "user"
                                                and text in str(i.get("content")) for i in items))


# --- the search: a unit in focus does not hide the professional pool -----------------------------------------------------

def focused(office, intent: str) -> T.Workspace:
    ws = workspace(office, intent)
    handle = T.unit_handle_of(ws, office.appraisal, office.version, READING, 1)
    ws.unit_focus = {"handle": handle, "document_id": office.appraisal, "unit_id": None}
    ws.unit_default = True  # the server's pin (the search-only path, round 15 U6): a call that names no unit works inside it
    return ws


def test_a_mixed_search_naming_the_professional_pool_reads_it_though_a_unit_is_in_focus(office):
    out = run(focused(office, "mixed"), "search", query=TERM_QUERY, limit=6, pool="professional")
    hits = sources_of(out)
    assert hits and {s["document_id"] for s in hits} == {office.glossary}
    assert all(s.get("source_kind") == "professional" for s in hits)


def test_a_search_that_names_no_pool_stays_inside_the_unit_in_focus(office):
    out = run(focused(office, "mixed"), "search", query=TERM_QUERY, limit=6)
    assert {s["document_id"] for s in sources_of(out)} == {office.appraisal} and "U1" in out


def test_a_property_question_keeps_its_unit_whatever_pool_the_call_names(office):
    # the professional pool is only a method beside the appraisals for a property question: nothing to reach for
    out = run(focused(office, "property"), "search", query=TERM_QUERY, limit=6, pool="professional")
    assert {s["document_id"] for s in sources_of(out)} == {office.appraisal}


def test_a_call_that_names_a_unit_or_documents_is_left_as_asked(office):
    ws = focused(office, "mixed")
    out = run(ws, "search", query=TERM_QUERY, limit=6, pool="professional", unit=ws.unit_focus["handle"])
    assert {s["document_id"] for s in sources_of(out)} == {office.appraisal}


# --- the turn: one request for the professional part ------------------------------------------------------------------------

def test_a_mixed_answer_that_never_read_the_professional_pool_is_asked_once_to_search_it(client, office, monkeypatch):
    def property_only(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].", documents=[office.appraisal])

    def with_glossary(items) -> dict:
        a, g = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה"), sid_of(items, office.glossary, "גישת ההשוואה היא שיטה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].\n\nלפי המילון המקצועי: גישת ההשוואה היא שיטה להערכת שווי לפי "
                     f"עסקאות השוואה בנכסים דומים [{g}].", documents=[office.appraisal, office.glossary])

    agent = ScriptedAgent([[call("search", query="גישת ההשוואה בשומה הרקפות 5", document_ids=[office.appraisal],
                                 limit=None, unit=None, pool=None)],
                           property_only,
                           [call("search", query=TERM_QUERY, document_ids=None, limit=None, unit=None, pool="professional")],
                           with_glossary, with_glossary], intent="mixed")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert nudges(agent, UNREAD) >= 1 and nudges(agent, UNCITED) == 0
    assert "המילון המקצועי" in m["answer"]["markdown"]
    assert {s["source_kind"] for s in m["answer"]["sources"]} == {"appraisal", "professional"}


def test_a_mixed_answer_that_left_the_professional_hits_it_read_uncited_is_asked_to_cite_them(client, office, monkeypatch):
    def property_only(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].", documents=[office.appraisal])

    def with_glossary(items) -> dict:
        a, g = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה"), sid_of(items, office.glossary, "גישת ההשוואה היא שיטה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].\n\nלפי המילון המקצועי: גישת ההשוואה היא שיטה להערכת שווי לפי "
                     f"עסקאות השוואה בנכסים דומים [{g}].", documents=[office.appraisal, office.glossary])

    agent = ScriptedAgent([[call("search", query=TERM_QUERY, document_ids=None, limit=None, unit=None, pool=None)],
                           property_only, with_glossary, with_glossary], intent="mixed")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert nudges(agent, UNCITED) >= 1 and nudges(agent, UNREAD) == 0
    # it names the sources the turn already holds, so the model need not search again
    asked_items = next(items for items in agent.seen if any(
        isinstance(i, dict) and i.get("role") == "user" and UNCITED in str(i.get("content"))
        for i in items))
    notice = next(i["content"] for i in asked_items if isinstance(i, dict) and UNCITED in str(i.get("content")))
    assert re.search(r"S\d+", notice)
    assert any(s["source_kind"] == "professional" for s in m["answer"]["sources"])


def test_the_request_is_made_once_and_an_answer_the_appraisals_own_text_supports_is_kept(client, office, monkeypatch):
    def property_only(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים בסביבה [{a}].",
                     documents=[office.appraisal])

    agent = ScriptedAgent([[call("search", query="גישת ההשוואה בשומה הרקפות 5", document_ids=[office.appraisal],
                                 limit=None, unit=None, pool=None)],
                           property_only, property_only, property_only], intent="mixed")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert nudges(agent, UNREAD) == 1 and len(agent.seen) == 3  # asked once, then accepted
    assert "עסקאות השוואה בנכסים דומים" in m["answer"]["markdown"]
    assert {s["source_kind"] for s in m["answer"]["sources"]} == {"appraisal"}


@pytest.mark.parametrize("failure", [CallStatus.TIMEOUT, CallStatus.INCOMPLETE, {"final": {"status": "bogus"}}])
def test_a_failed_mixed_round_leaves_the_first_answer_to_be_verified_and_shown(client, office, monkeypatch, failure):
    """The extra round is best effort (round 13 U14): when the step after the notice fails (a provider failure or a final
    answer of an invalid schema) the first answer is verified as it stood, never a failed turn."""
    def property_only(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים בסביבה [{a}].",
                     documents=[office.appraisal])

    agent = ScriptedAgent([[call("search", query="גישת ההשוואה בשומה הרקפות 5", document_ids=[office.appraisal],
                                 limit=None, unit=None, pool=None)],
                           property_only, failure, property_only], intent="mixed")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert nudges(agent, UNREAD) == 1  # the round was asked, then failed
    assert "עסקאות השוואה בנכסים דומים" in m["answer"]["markdown"]
    assert {s["source_kind"] for s in m["answer"]["sources"]} == {"appraisal"}


def test_an_answer_that_already_cites_a_professional_source_is_not_asked(client, office, monkeypatch):
    def both(items) -> dict:
        a, g = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה"), sid_of(items, office.glossary, "גישת ההשוואה היא שיטה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].\n\nלפי המילון המקצועי: גישת ההשוואה היא שיטה להערכת שווי לפי "
                     f"עסקאות השוואה בנכסים דומים [{g}].", documents=[office.appraisal, office.glossary])

    agent = ScriptedAgent([[call("search", query=TERM_QUERY, document_ids=None, limit=None, unit=None, pool=None)],
                           both, both], intent="mixed")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert nudges(agent, UNREAD) == 0 and nudges(agent, UNCITED) == 0
    assert len(agent.seen) == 2


@pytest.mark.parametrize("intent", ["professional", "property", "unknown"])
def test_only_a_mixed_question_is_asked(client, office, monkeypatch, intent):
    def property_only(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההשוואה [{a}].", documents=[office.appraisal])

    agent = ScriptedAgent([[call("search", query="גישת ההשוואה בשומה הרקפות 5", document_ids=[office.appraisal],
                                 limit=None, unit=None, pool=None)],
                           property_only, property_only], intent=intent)
    # a professional claim that names a property is settled as mixed, so the professional one is a general question
    general = "מה כתוב במילון המקצועי על גישת ההשוואה?" if intent == "professional" else QUESTION
    m = asked(client, office, monkeypatch, agent, general)
    assert m["status"] == "done", m
    assert nudges(agent, UNREAD) == 0 and nudges(agent, UNCITED) == 0


def test_a_turn_with_no_source_at_all_states_what_it_could_not_give_instead_of_an_empty_answer(client, office, monkeypatch):
    """The W9 shape: nothing the turn wrote is supported, so no source is cited. The answer is not empty: the server
    states every part of the request that was not given (a stated gap), and cites nothing."""
    def ungrounded(items) -> dict:
        a = sid_of(items, office.appraisal, "נבחרה גישת ההשוואה")
        return final(f"בשומה נבחרה גישת ההיוון [{a}].", documents=[office.appraisal])

    agent = ScriptedAgent([[call("search", query="גישת ההשוואה בשומה הרקפות 5", document_ids=[office.appraisal],
                                 limit=None, unit=None, pool=None)],
                           ungrounded, ungrounded, ungrounded, ungrounded], intent="mixed", judge="unsupported")
    m = asked(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert m["answer"]["markdown"].strip() and not m["answer"]["sources"]
    assert m["answer"]["status"] in ("partial", "not_found")


def test_the_policy_tells_the_model_to_search_the_professional_pool_apart_for_a_mixed_question():
    rule = engine.POLICY[engine.POLICY.index("הנתונים של השומה (עם הנכס והמקור)"):][:700]
    assert "pool=professional" in rule and "בלי document_ids ובלי unit" in rule
