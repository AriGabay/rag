"""Round 10 sequences, end to end with a scripted model (U11: R36, R37; AE1-AE9; KD15, KTD16).

One test per acceptance example, each a conversation of several turns over the synthetic round-10 fixtures
(``tests/fixtures/round10``: a synthetic standard 98.1 in two editions, a clarification of it, an appraisal titled
"תקן 98 - …" that is an appraisal and not the standard, and three appraisals whose titles hold "שומה"), ingested through
the real worker as a standard, a professional document or an appraisal as each is uploaded. The models are scripted: what
is proven is what the server decides and stores, never wording.

Every assertion is on a decision, not on a sentence:

- the settled intent of the turn (``answer.request.intent``, ``resolution.decisions``), and that no lookup ran and no
  clarification was asked where none should be;
- the pools of **every** tool call of the turn (``Workspace.pools`` at the moment of the call, recorded by wrapping
  ``tools.run_tool``) and what each call returned: the source kinds, and the label the server put on an appraisal under
  a professional question or on a standard under a property one;
- the sources the answer actually cites (``source_kind``, the standard's number, edition and section);
- the slots stored in ``answer.context`` (topic and items, property, sources, pending).

A script that cites a source it was not given fails on the kinds it cites, so a pool that stopped returning the standard
shows as a missing kind and not as a changed sentence. Synthetic data only; every name and number is invented."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app import worker
from app.chat import contexts
from app.chat import tools as T
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import ANSWER as CALC_ANSWER
from tests.integration.test_chat_calculation import QUESTION as CALC_QUESTION
from tests.integration.test_chat_calculation import RISE, _ae4_steps, _clarification_turn
from tests.integration.test_chat_calculation import add_document as add_calc_document
from tests.integration.test_chat_calculation import office as office  # noqa: F401 - the calculation fixture
from tests.integration.test_chat_units import R9B, SIMILAR_QUESTION
from tests.support.professional_files import professional_file
from tests.support.scripted_agent import ScriptedAgent, call, component, final, intent_part, requirement

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round10" / "manifest.json").read_text(encoding="utf-8"))

# the pools of an intent, as the plan states them (KTD12) - written out, not read back from the server's table
PROFESSIONAL_POOLS = {"professional": "primary", "appraisal": "example"}
PROPERTY_POOLS = {"appraisal": "primary", "professional": "method"}
MIXED_POOLS = {"appraisal": "primary", "professional": "primary"}

APPROACHES_Q = "מה הן שלוש גישות השומה הבסיסיות?"
APPROACHES_TOPIC = "שלוש גישות השומה הבסיסיות"
LIST_ANSWER = ("שלוש גישות השומה הן:\n1. **גישת ההשוואה** — השוואה לעסקאות דומות [{s0}]\n"
               "2. **גישת ההכנסות** — היוון ההכנסה [{s0}]\n3. **גישת העלות** — חישוב עלות הבנייה [{s0}]")
APPROACHES = ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]
THREE, FIVE = "שלוש עסקאות השוואה", "חמש עסקאות השוואה"  # the material difference of section 2.1 between the editions


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


# --- the tools, recorded -----------------------------------------------------------------------------------------------

def sources_of(output: str) -> list[dict]:
    """The header attributes of every ``<source>`` of a tool output, with its body; the kind of an appraisal is not
    written in the header (it is the default), so ``kind`` says it."""
    found = []
    for m in re.finditer(r"<source ([^>]*)>(.*?)</source>", output, re.S):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        found.append(attrs | {"body": m.group(2), "kind": attrs.get("source_kind", "appraisal")})
    return found


@dataclass
class ToolCall:
    """One tool call of a turn: the intent and the pools in force when it ran, what it asked and what it returned."""

    name: str
    args: dict
    intent: str
    pools: dict
    output: str
    absent: bool

    @property
    def sources(self) -> list[dict]:
        return sources_of(self.output)

    def kinds(self) -> set[str]:
        return {s["kind"] for s in self.sources}


@pytest.fixture
def calls(monkeypatch) -> list[ToolCall]:
    """Every tool call the engine makes, in order (``T.run_tool`` wrapped; the output is the one the model sees)."""
    seen: list[ToolCall] = []
    original = T.run_tool

    def run_tool(ws, name, arguments):
        intent, pools = ws.intent, ws.pools
        output = original(ws, name, arguments)
        seen.append(ToolCall(name, json.loads(arguments), intent, pools, output, ws.standards_absent))
        return output

    monkeypatch.setattr(T, "run_tool", run_tool)
    return seen


# --- the world ---------------------------------------------------------------------------------------------------------

def ingest(office, key: str) -> str:
    """A round-10 fixture uploaded as the kind its manifest gives and read by the real worker; its document id."""
    d = MANIFEST["documents"][key]
    path = FIXTURES / d["file"]
    r = office.client.post("/api/documents", files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))],
                           data={"group_id": str(office.default_group_id), "source_kind": d["source_kind"]})
    assert r.status_code == 200, r.text
    res = r.json()["results"][0]
    assert res["status"] == "accepted", res
    while worker.run_one("test-worker"):
        pass
    got = office.client.get(f"/api/documents/{res['document_id']}").json()
    assert got["title"] == d["title"] and got["source_kind"] == d["source_kind"], got
    assert got["current_version"]["status"] in ("ready", "needs_review"), got["current_version"]
    office.ids[key] = res["document_id"]
    return res["document_id"]


@pytest.fixture
def world(db, client, monkeypatch, calls):
    """An office whose documents are the synthetic standard (edition 1), the clarification, the appraisal titled as a
    standard and the three appraisals titled with "שומה"; a logged-in admin; cloud use on."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client, a.monkeypatch, a.calls, a.ids = client, monkeypatch, calls, {}
    cloud(monkeypatch, a, ScriptedAgent([]))
    for key in ("standard_ed1", "clarification", "appraisal_titled_as_standard", "appraisal_shape_1", "appraisal_shape_2",
                "appraisal_shape_3"):
        ingest(a, key)
    a.std, a.rent_doc, a.jasmine_doc, a.cypress_doc, a.d5 = (a.ids["standard_ed1"], a.ids["appraisal_shape_1"],
                                                            a.ids["appraisal_shape_2"], a.ids["appraisal_shape_3"],
                                                            a.ids["appraisal_titled_as_standard"])
    return a


# --- scripts -----------------------------------------------------------------------------------------------------------

def _resolution(**kw) -> dict:
    return {"relation": "new_question", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _focus(doc: str, subject: str, kind: str = "rent") -> dict:
    return {"metric_as_written": {"rent": "דמי שכירות ראויים", "value": "שווי"}.get(kind, kind), "metric_kind": kind,
            "unit": "ILS", "period": "month" if kind == "rent" else "unknown", "area_basis": "", "vat": "unknown",
            "subject": subject, "value_role": "appraiser_determination", "document_ids": [doc]}


MISSES: list[str] = []  # a wanted source no tool output held (reported by ``turn``, which clears it)
SENTENCES = {"standard": "גישת ההשוואה נשענת על עסקאות השוואה בנכסים דומים", "professional": "ההבהרה מסבירה את סעיף 2.1",
             "appraisal": "בשומה נקבע השווי לפי גישת ההשוואה"}


def cites(*wanted: tuple[str, str | None], body: str | None = None, focus: dict | None = None):
    """A step that answers from the sources the earlier tool calls returned: one claim and one citation per ``(kind,
    document id or None)`` wanted. ``body`` is the answer's text with ``{s0}``, ``{s1}``... for the citations. A wanted
    source that no output holds is a step the pool did not give: the turn then answers ``not_found`` and cites nothing, so
    the test fails on the kinds it cites."""

    def step(items):
        out = "\n".join(i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")
        found = sources_of(out)
        ids, claims, docs = [], [], []
        for kind, doc in wanted:
            hit = next((s for s in found if s["kind"] == kind and (doc is None or s["document_id"] == doc)), None)
            if hit is None:
                MISSES.append(f"wanted a {kind} source (document {doc}); the outputs held "
                              f"{[(x['kind'], x['document_id'][:8], x.get('location')) for x in found]}")
                return final("לא נמצא מקור מתאים.", "not_found")
            ids.append(hit["id"])
            docs.append(hit["document_id"])
            claims.append({"text": SENTENCES[kind], "source_ids": [hit["id"]], "basis": "explicit"})
        keys = {f"s{n}": f"{sid}" for n, sid in enumerate(ids)}
        markdown = body.format(**keys) if body else " ".join(f"{SENTENCES[k]} [{sid}]." for (k, _), sid in zip(wanted, ids, strict=True))
        return final(markdown, claims=claims, documents=list(dict.fromkeys(docs)), focus=focus)

    return step


def search(query: str, documents: list[str] | None = None) -> list:
    return [call("search", query=query, document_ids=documents, limit=None)]


# --- a turn, and what it left ------------------------------------------------------------------------------------------

@dataclass
class Turn:
    message: dict
    agent: ScriptedAgent
    tools: list[ToolCall]
    world: SimpleNamespace

    @property
    def answer(self) -> dict:
        return self.message["answer"]

    @property
    def request(self) -> dict:
        """The answer's request (a first turn has none: the analysis settled its intent, read from the diagnostics)."""
        return self.answer.get("request") or {}

    @property
    def settled(self) -> dict:
        """The settled intent of the turn, with the evidence and the reasons, from the diagnostics."""
        return self.diagnostics["intent"]

    @property
    def intent(self) -> str:
        return self.settled["value"]

    @property
    def documents(self) -> list[str]:
        return self.request.get("document_ids") or []

    @property
    def candidates(self) -> list[dict]:
        return self.request.get("candidates") or []

    @property
    def diagnostics(self) -> dict:
        return self.world.client.get(f"/api/chat/messages/{self.message['id']}/diagnostics").json()["resolution"]

    @property
    def decisions(self) -> dict:
        return self.diagnostics["decisions"]

    @property
    def context(self) -> dict:
        with tenant_tx(self.world.system) as conn:
            return conn.execute(text("SELECT answer->'context' FROM messages WHERE id = :m"),
                                {"m": self.message["id"]}).scalar_one()

    @property
    def cited(self) -> list[dict]:
        return self.answer["sources"]

    def cited_kinds(self) -> set[str]:
        return {s["source_kind"] for s in self.cited}

    @property
    def task(self) -> str:
        return self.agent.seen[0][0]["content"]

    def searches(self) -> list[ToolCall]:
        return [c for c in self.tools if c.name == "search"]


def turn(world, cid: str, question: str, steps: list, resolve: dict | None = None, **agent) -> Turn:
    """One turn: the scripted agent that answers it (and, on a follow-up, the parse the resolve call returns), the finished
    message and the tool calls it made."""
    scripted = ScriptedAgent(steps, **agent)
    if resolve is not None:
        scripted.on("resolve", resolve)
    world.monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: scripted)
    before = len(world.calls)
    m = send(world.client, cid, question)
    missed, MISSES[:] = list(MISSES), []
    assert not missed, missed
    assert m["status"] == "done", m
    return Turn(m, scripted, list(world.calls[before:]), world)


def conversation(world) -> str:
    return new_conversation(world.client)


def property_turn(world, cid: str, question: str, doc: str, subject: str, kind: str = "rent", first: bool = False,
                  resolve: dict | None = None, query: str = "דמי שכירות ראויים") -> Turn:
    """A turn about the property of ``doc``: a search of that document and an answer from it with the property as focus."""
    steps = [search(query, [doc]), cites(("appraisal", doc), focus=_focus(doc, subject, kind))]
    if first:
        return turn(world, cid, question, steps, None, intent="property")
    return turn(world, cid, question, steps, resolve or _resolution(
        relation="same_datum", intent=intent_part("property", "conversation_property")))


def professional_turn(world, cid: str, question: str = APPROACHES_Q, first: bool = False, kind: str = "standard",
                      query: str = "שלוש גישות השומה הבסיסיות", body: str | None = None,
                      topic: str = APPROACHES_TOPIC, reference: str = "topic", item: int | None = None,
                      claim: str = "professional") -> Turn:
    """A professional question answered from the standard (a first turn or a follow-up whose parse says ``claim``)."""
    steps = [search(query), cites((kind, None), body=body)]
    if first:
        return turn(world, cid, question, steps, None, intent=intent_part(claim, "none", topic=topic))
    return turn(world, cid, question, steps, _resolution(
        standalone_question=question, intent=intent_part(claim, reference, item=item, topic=topic)))


# --- what a professional turn must look like ---------------------------------------------------------------------------

def assert_professional(t: Turn, world, kinds: set[str] = frozenset({"standard"})) -> None:
    """The turn was answered as a professional question: its intent settled so, no lookup, no property, no clarification;
    every tool call ran under the professional pools and its hits are labelled by them; the answer cites the standard."""
    assert t.answer["status"] == "answered", t.answer
    assert t.intent == "professional" and t.decisions["intent"] == "professional", t.settled
    assert not t.candidates and t.documents == [] and t.request.get("located_unit") is None
    res = t.diagnostics
    assert "lookup" not in res and "entity" not in res["decisions"], res
    assert t.tools, "the turn made no tool call"
    for c in t.tools:
        assert c.intent == "professional" and c.pools == PROFESSIONAL_POOLS, (c.name, c.intent, c.pools)
    assert any(c.name == "search" for c in t.tools)
    for c in t.searches():
        assert "standard" in c.kinds(), [s["kind"] for s in c.sources]
        for s in c.sources:
            if s["kind"] == "appraisal":  # an appraisal under a professional question is an example, never a rule
                assert s.get("label", "").startswith(T.LABEL_EXAMPLE) or s.get("label") == T.LABEL_APPRAISAL_KIND, s
            else:
                assert "label" not in s, s
    assert t.cited_kinds() == kinds, [(s["source_kind"], s["title"]) for s in t.cited]
    assert {s["source_kind"] for s in t.context["sources"]} == kinds


def standard_of(source: dict) -> tuple:
    s = source["standard"] or {}
    return s.get("number"), s.get("edition"), s.get("section")


# =======================================================================================================================
# AE1: the approaches question is a professional question wherever it is asked
# =======================================================================================================================

def test_ae1_the_same_professional_question_is_answered_the_same_in_a_new_conversation_and_after_rent_value_property_and_a_calculation(
        world):
    calc_doc, _ = add_calc_document(world)
    rent_q, value_q = "מה דמי השכירות הראויים לנכס ברחוב התמר 9?", "מה שווי הנכס ברחוב התמר 9?"
    prop_q = "מה הגוש והחלקה של הנכס ברחוב היסמין 3?"
    preludes = {
        "new conversation": None,
        "after rent": lambda cid: property_turn(world, cid, rent_q, world.rent_doc, "התמר 9", "rent", first=True),
        "after value": lambda cid: property_turn(world, cid, value_q, world.rent_doc, "התמר 9", "value", first=True,
                                                 query="שווי הנכס"),
        "after property": lambda cid: property_turn(world, cid, prop_q, world.jasmine_doc, "היסמין 3", "other", first=True,
                                                    query="גוש וחלקה"),
        "after a calculation": lambda cid: turn(world, cid, CALC_QUESTION, [*_ae4_steps(calc_doc), final(CALC_ANSWER, documents=[calc_doc])],
                                                None, intent="property"),
    }
    signatures = {}
    for name, prelude in preludes.items():
        cid = conversation(world)
        before = None
        if prelude is None:
            t = professional_turn(world, cid, first=True)
        else:
            first = prelude(cid)
            assert first.answer["status"] == "answered", (name, first.answer)
            before = first.context
            t = professional_turn(world, cid)
        assert_professional(t, world)
        assert t.context["topic"]["text"] == APPROACHES_TOPIC, name
        if before is not None:  # the property the conversation was about is left where it was: dormant, not dropped
            assert t.context["property"] == before["property"] and t.context["params"] == before["params"], name
            assert "הנתון שבמרכז השיחה" not in t.task  # and no focus block reaches the professional question (KTD7)
        signatures[name] = (t.intent, tuple(sorted(t.cited_kinds())), tuple(standard_of(s) for s in t.cited),
                            tuple((c.name, c.intent, tuple(sorted(c.pools.items())), tuple(sorted(c.kinds())))
                                  for c in t.tools))
    assert len(set(signatures.values())) == 1, signatures  # the same decisions in every context


def test_the_reported_conversation_a_general_question_twice_and_then_the_approaches_is_never_a_choice_of_properties(world):
    """The office-A conversation: "מה זה דמ״ש ראויים ?" → "מה זה היוון הכנסות ?" → the approaches question, over three
    appraisals whose titles hold "שומה". Whatever the third parse claims, nothing is looked up and nothing is asked."""
    questions = ["מה זה דמ״ש ראויים ?", "מה זה היוון הכנסות ?"]
    for claim, settled in (("professional", "professional"), ("unknown", "unknown"), ("property", "property")):
        cid = conversation(world)
        first = professional_turn(world, cid, questions[0], first=True, query="דמי שכירות ראויים", topic="דמי שכירות ראויים")
        second = professional_turn(world, cid, questions[1], query="היוון הכנסות", topic="היוון הכנסות")
        for t in (first, second):
            assert_professional(t, world)
        # a property claim reads appraisals only (a standard would be a labelled method on request): it answers from them
        third = professional_turn(world, cid, APPROACHES_Q, claim=claim, kind="appraisal" if claim == "property" else "standard")
        assert third.answer["status"] != "clarification" and not third.candidates, (claim, third.answer)
        assert third.intent == settled and third.decisions["intent"] == settled, claim
        res = third.diagnostics
        assert "lookup" not in res and "entity" not in res["decisions"], (claim, res)
        assert third.documents == []
        if claim == "professional":
            assert_professional(third, world)


# =======================================================================================================================
# AE2: an ordinal names the item of the previous answer's list
# =======================================================================================================================

def test_ae2_the_second_names_the_second_item_of_the_list_and_stays_a_professional_question(world):
    cid = conversation(world)
    first = professional_turn(world, cid, first=True, body=LIST_ANSWER)
    assert first.context["topic"]["items"] == APPROACHES and first.context["topic"]["message_id"] == first.message["id"]
    for question, item in (("הסבר את הגישה השנייה", 2), ("הסבר את השנייה", 2), ("ואת השלישית", 3)):
        t = professional_turn(world, cid, question, query="גישת ההכנסות", reference="prior_answer_item", item=item)
        assert_professional(t, world)
        intent = t.settled
        assert intent["item"] == item and intent["item_label"] == APPROACHES[item - 1] and intent["reference"] == "prior_answer_item"
        assert APPROACHES[item - 1] in t.request["standalone_question"] and t.request["topic"] == APPROACHES[item - 1]
        assert t.decisions["item"].startswith(f"bound: {item}"), t.decisions
        assert t.context["topic"]["items"] == APPROACHES  # a later answer lists none: the list persists
        assert t.cited_kinds() == {"standard"}


def test_an_ordinal_the_previous_answer_gave_no_list_for_binds_to_nothing(world):
    cid = conversation(world)
    professional_turn(world, cid, "מה זה שווי שוק?", first=True, query="שווי שוק", topic="שווי שוק")
    t = professional_turn(world, cid, "הסבר את השנייה", reference="prior_answer_item", item=2, query="גישה")
    assert t.settled["item"] is None and t.settled["reference"] == "none"
    assert t.decisions["item"].startswith("rejected") and "list" in t.decisions["item"]
    assert t.answer["status"] != "clarification" and t.documents == []


# =======================================================================================================================
# AE3: a general explanation, then its application to the property in the conversation
# =======================================================================================================================

def test_ae3_how_to_apply_it_to_this_property_is_mixed_with_the_topic_the_property_and_both_kinds_of_source(world):
    cid = conversation(world)
    property_turn(world, cid, "מה דמי השכירות הראויים לנכס ברחוב התמר 9?", world.rent_doc, "התמר 9", first=True)
    listed = professional_turn(world, cid, body=LIST_ANSWER)
    assert_professional(listed, world)
    assert listed.context["property"]["document_ids"] == [world.rent_doc]  # dormant beside the topic
    t = turn(world, cid, "איך מיישמים אותה בנכס הזה?",
             [search("גישת ההשוואה", [world.rent_doc]) + search("גישת ההשוואה"),
              cites(("standard", None), ("appraisal", world.rent_doc))],
             _resolution(standalone_question="איך מיישמים את הגישה בנכס הזה?",
                         intent=intent_part("mixed", "topic", topic=APPROACHES_TOPIC)))
    assert t.answer["status"] == "answered", t.answer
    assert t.intent == "mixed" and t.decisions["intent"] == "mixed"
    assert t.documents == [world.rent_doc] and t.request["topic"] == APPROACHES_TOPIC
    assert t.decisions["documents"].startswith("reinstated"), t.decisions
    assert not t.candidates and "lookup" not in t.diagnostics
    for c in t.tools:
        assert c.intent == "mixed" and c.pools == MIXED_POOLS, (c.name, c.intent, c.pools)
    own, general = t.searches()
    assert own.args["document_ids"] == [world.rent_doc] and own.kinds() == {"appraisal"}
    assert {"standard", "appraisal"} <= general.kinds()
    for src in [x for x in own.sources + general.sources if "same_as" not in x]:  # a pointer to a source already sent has no label; each kind keeps its own label in the model's reading: the rule apart from the data
        assert src.get("label") == (T.LABEL_PROFESSIONAL if src["kind"] in ("standard", "professional") else T.LABEL_APPRAISAL_DATA), src
    assert t.cited_kinds() == {"standard", "appraisal"}
    appraisal = next(x for x in t.cited if x["source_kind"] == "appraisal")
    assert appraisal["document_id"] == world.rent_doc  # the property's own appraisal, not another property's
    ctx = t.context
    assert ctx["property"]["document_ids"] == [world.rent_doc] and ctx["topic"]["text"] == APPROACHES_TOPIC
    assert ctx["topic"]["items"] == APPROACHES
    assert {x["source_kind"] for x in ctx["sources"]} == {"standard", "appraisal"}


# =======================================================================================================================
# AE4: a professional question in the middle of a property conversation, and back
# =======================================================================================================================

def test_ae4_a_professional_question_applies_no_property_filter_and_the_value_question_after_it_returns_to_the_property(
        world):
    cid = conversation(world)
    property_turn(world, cid, "מה דמי השכירות הראויים לנכס ברחוב התמר 9?", world.rent_doc, "התמר 9", first=True)
    prof = professional_turn(world, cid)
    assert_professional(prof, world)
    for c in prof.searches():
        assert not c.args.get("document_ids")  # no property filter reaches the search
    assert prof.context["property"]["document_ids"] == [world.rent_doc]
    t = property_turn(world, cid, "ומה השווי שם?", world.rent_doc, "התמר 9", "value", query="שווי הנכס",
                      resolve=_resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                                          standalone_question="מה השווי בנכס ברחוב התמר 9?", metric_kind="value"))
    assert t.answer["status"] == "answered" and t.intent == "property", t.settled
    assert t.documents == [world.rent_doc] and not t.candidates
    for c in t.tools:
        assert c.intent == "property" and c.pools == PROPERTY_POOLS, (c.name, c.intent, c.pools)
    for c in t.searches():  # a property question reads appraisals; a standard would only be labelled as a method
        assert all(s["kind"] == "appraisal" and "label" not in s for s in c.sources), [(s["kind"], s.get("label")) for s in c.sources]
    assert t.cited_kinds() == {"appraisal"} and {s["document_id"] for s in t.cited} == {world.rent_doc}
    assert t.context["property"]["document_ids"] == [world.rent_doc]
    assert t.context["topic"]["text"] == APPROACHES_TOPIC  # the topic is dormant in its turn


# =======================================================================================================================
# AE5: a metric change or a correction keeps the property; another address replaces it
# =======================================================================================================================

def test_ae5_a_metric_change_and_a_correction_keep_the_property_and_a_named_address_replaces_it(world):
    cid = conversation(world)
    property_turn(world, cid, "מה דמי השכירות הראויים לנכס ברחוב התמר 9?", world.rent_doc, "התמר 9", first=True)
    value = property_turn(world, cid, "ומה השווי?", world.rent_doc, "התמר 9", "value", query="שווי הנכס", resolve=_resolution(
        relation="metric_change", intent=intent_part("property", "conversation_property"),
        changed_fields=[{"field": "metric_kind", "user_words": "השווי"}], metric_kind="value"))
    back = property_turn(world, cid, "לא, התכוונתי לדמי השכירות", world.rent_doc, "התמר 9", "rent", resolve=_resolution(
        relation="correction", intent=intent_part("property", "conversation_property"),
        changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    for t in (value, back):
        assert t.documents == [world.rent_doc] and t.answer["status"] == "answered"
        assert t.context["property"]["document_ids"] == [world.rent_doc]
        assert all(c.pools == PROPERTY_POOLS for c in t.tools)
    # a word the titles share names no property: a "change of subject" that quotes only it changes nothing (R8)
    weak = property_turn(world, cid, "ומה כתוב בשומה על גישת ההשוואה?", world.rent_doc, "התמר 9", "rent",
                         query="גישת ההשוואה", resolve=_resolution(
                             intent=intent_part("property", "named_entity"),
                             changed_fields=[{"field": "subject", "user_words": "שומה"}], subject="שומה"))
    assert weak.answer["status"] != "clarification" and not weak.candidates, weak.answer
    assert "lookup" not in weak.diagnostics and weak.settled["evidence"] == [], weak.diagnostics
    assert weak.decisions["subject"].startswith("kept the focus"), weak.decisions
    assert weak.context["property"]["document_ids"] == [world.rent_doc]  # the property is neither replaced nor dropped
    # a real change: an address with its house number replaces the property
    moved = property_turn(world, cid, "ובהיסמין 3?", world.jasmine_doc, "היסמין 3", "rent", resolve=_resolution(
        intent=intent_part("property", "named_entity"), changed_fields=[{"field": "subject", "user_words": "היסמין 3"}],
        subject="היסמין 3"))
    assert moved.documents == [world.jasmine_doc] and moved.answer["status"] == "answered"
    assert "address" in {e["kind"] for e in moved.settled["evidence"]}  # an address, and the unit name it spells
    assert moved.context["property"]["document_ids"] == [world.jasmine_doc]
    assert {s["document_id"] for s in moved.cited} == {world.jasmine_doc}


def test_a_real_property_change_by_block_and_parcel_or_by_an_address_replaces_the_property_after_a_professional_turn(world):
    cid = conversation(world)
    property_turn(world, cid, "מה דמי השכירות הראויים לנכס ברחוב התמר 9?", world.rent_doc, "התמר 9", first=True)
    professional_turn(world, cid)
    by_parcel = property_turn(world, cid, "ומה דמי השכירות בגוש 7004 חלקה 41?", world.cypress_doc, "הברוש 12", "rent",
                              resolve=_resolution(intent=intent_part("property", "named_entity"),
                                                  changed_fields=[{"field": "subject", "user_words": "גוש 7004 חלקה 41"}],
                                                  subject="גוש 7004 חלקה 41"))
    assert by_parcel.documents == [world.cypress_doc] and by_parcel.answer["status"] == "answered"
    assert [e["kind"] for e in by_parcel.settled["evidence"]] == ["parcel"]
    assert by_parcel.context["property"]["document_ids"] == [world.cypress_doc]
    # the appraisal titled "תקן 98 - …" is an appraisal: an address finds it as one
    titled = property_turn(world, cid, "ומה השווי ברחוב הדקל 5?", world.d5, "הדקל 5", "value", query="שווי הנכס",
                           resolve=_resolution(intent=intent_part("property", "named_entity"),
                                               changed_fields=[{"field": "subject", "user_words": "הדקל 5"}],
                                               subject="הדקל 5"))
    assert titled.documents == [world.d5] and titled.cited_kinds() == {"appraisal"}
    assert all(c.pools == PROPERTY_POOLS for c in titled.tools)


def test_an_explicit_reference_to_a_specific_appraisal_in_a_professional_conversation_looks_it_up_and_mixes(world):
    cid = conversation(world)
    professional_turn(world, cid, first=True)
    t = turn(world, cid, "מה נקבע בשומה של רחוב היסמין 3 לגבי גישת ההשוואה?",
             [search("גישת ההשוואה", [world.jasmine_doc]) + search("גישת ההשוואה"),
              cites(("standard", None), ("appraisal", world.jasmine_doc),
                    focus=_focus(world.jasmine_doc, "היסמין 3", "other"))],
             _resolution(intent=intent_part("mixed", "named_entity", topic="גישת ההשוואה"),
                         changed_fields=[{"field": "subject", "user_words": "היסמין 3"}], subject="היסמין 3"))
    assert t.answer["status"] == "answered" and t.intent == "mixed", t.settled
    assert t.documents == [world.jasmine_doc], t.request
    assert "address" in {e["kind"] for e in t.settled["evidence"]}
    assert all(c.intent == "mixed" and c.pools == MIXED_POOLS for c in t.tools)
    assert t.cited_kinds() == {"standard", "appraisal"}
    assert {s["document_id"] for s in t.cited if s["source_kind"] == "appraisal"} == {world.jasmine_doc}


# =======================================================================================================================
# AE6: a pending clarification lives one message
# =======================================================================================================================

def _clarification(world) -> tuple[str, Turn]:
    """A property question whose words fit two appraisals of one file equally: the server's own clarification, no agent step."""
    r = world.client.post("/api/documents", files=[("files", (R9B.name, R9B.read_bytes(), "application/octet-stream"))],
                          data={"group_id": str(world.default_group_id)})
    assert r.status_code == 200, r.text
    while worker.run_one("test-worker"):
        pass
    cid = conversation(world)
    t = turn(world, cid, SIMILAR_QUESTION, [], None, intent="property")
    assert t.answer["status"] == "clarification" and t.agent.seen == [], t.answer
    assert t.context["pending"]["kind"] == "candidates"
    assert [c["unit"]["unit_no"] for c in t.candidates] == [1, 2]
    return cid, t


def test_ae6_a_general_question_after_a_clarification_is_answered_as_one_and_the_clarification_expires(world):
    cid, asked = _clarification(world)
    t = professional_turn(world, cid, "מה זה שווי שוק?", query="שווי שוק", topic="שווי שוק")
    assert_professional(t, world)
    assert t.decisions["pending"].startswith("expired"), t.decisions
    assert t.context["pending"] is None
    # the expired clarification binds nothing and is not asked again, whatever comes next
    nxt = turn(world, cid, "השנייה", [final("בדיקה.", "not_found")], _resolution(standalone_question="השנייה"))
    assert nxt.answer["status"] != "clarification" and nxt.documents == [] and not nxt.candidates


def test_ae6_the_second_after_a_clarification_chooses_the_second_candidate(world):
    cid, asked = _clarification(world)
    t = turn(world, cid, "השנייה", [final("בדיקה.", "not_found")], _resolution(
        relation="clarification_answer", standalone_question="מה דמי השכירות במתחם הגפן?",
        intent=intent_part("property", "clarification")))
    assert t.answer["status"] != "clarification"
    assert t.request["located_unit"]["display"] == "מתחם הגפן ב" and t.decisions["pending"].startswith("bound")
    assert t.context["pending"] is None


# =======================================================================================================================
# AE7: a pending calculation parameter is bound only by its answer
# =======================================================================================================================

def test_ae7_a_new_question_with_a_year_or_a_professional_question_is_not_bound_as_the_pending_parameter(
        client, office, monkeypatch, calls):
    for claim, question in (("property", "ומה היה השווי ב-2024?"), ("professional", "מה זה שווי שוק?")):
        cid, doc, pending = _clarification_turn(client, office, monkeypatch, [RISE])
        with tenant_tx(office.system) as conn:
            stored = conn.execute(text("SELECT answer->'context'->'pending' FROM messages WHERE conversation_id = :c"
                                       " AND role = 'assistant'"), {"c": cid}).scalar_one()
        assert stored["kind"] == "parameter" and pending["parameters"] == ["שיעור העלייה של העלויות"]
        before = len(calls)
        agent = ScriptedAgent([search("שווי"), final("בדיקה.", "not_found", documents=[doc])])
        agent.on("resolve", _resolution(standalone_question=question, intent=intent_part(claim, "none")))
        monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda agent=agent: agent)
        m = send(client, cid, question)
        assert m["status"] == "done", m
        assert "תשובה לשאלת ההבהרה" not in agent.seen[0][0]["content"], claim  # not bound as the parameter's value
        assert not m["answer"].get("assumptions") and m["answer"].get("pending") is None, claim
        diag = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]
        assert diag["decisions"]["pending"].startswith("expired"), (claim, diag["decisions"])
        assert client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]["intent"]["value"] == claim
        pools = PROFESSIONAL_POOLS if claim == "professional" else PROPERTY_POOLS
        assert [c.pools for c in calls[before:]] == [pools], (claim, [c.pools for c in calls[before:]])
        with tenant_tx(office.system) as conn:
            context = conn.execute(text("SELECT answer->'context' FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        assert context["pending"] is None


# =======================================================================================================================
# AE8: a standard number is a professional reference; an appraisal titled with one is an appraisal
# =======================================================================================================================

def test_ae8_a_question_about_standard_98_1_section_2_1_is_professional_and_the_appraisal_titled_98_is_not_the_standard(world):
    ask = "מה דורש תקן 98.1 בסעיף 2.1?"
    refs = [{"standard": "98.1", "section": "2.1"}]
    ids = {}
    for claim in ("professional", "property"):  # the model's claim may be wrong: the number is no address (R9)
        cid = conversation(world)
        # under a property claim a standard is reachable only as a labelled method, by naming the pool (KTD12)
        pool = {"pool": "professional"} if claim == "property" else {}
        steps = [[call("search", query="עסקאות השוואה לפחות", document_ids=None, limit=None, **pool)],
                 cites(("standard", None))]
        t = turn(world, cid, ask, steps, None, intent={**intent_part(claim, "named_entity" if claim == "property" else "none",
                                                                     standard_refs=refs)})
        assert t.answer["status"] == "answered", (claim, t.answer)
        assert not t.candidates and t.documents == [], claim
        assert t.settled["evidence"] == [], (claim, t.settled)  # "98.1" and "2.1" are no house numbers
        assert t.settled["standard_refs"] == refs
        res = t.diagnostics
        assert "lookup" not in res and "entity" not in res["decisions"], (claim, res)
        assert t.cited_kinds() == {"standard"}
        (src,) = [s for s in t.cited if s["source_kind"] == "standard"][:1]
        assert src["document_id"] == world.std and standard_of(src)[0] == "98.1" and standard_of(src)[2] == "2.1"
        ids[claim] = t
        for c in t.searches():
            hit = [s for s in c.sources if s["document_id"] == world.d5]
            assert all(s["kind"] == "appraisal" for s in hit)  # the appraisal never passes for the standard
            if claim == "property":  # the standard it reached is a labelled method, never a property's datum
                assert all(s.get("label") == T.LABEL_METHOD for s in c.sources if s["kind"] == "standard" and "same_as" not in s)
            if claim == "professional":
                assert all(s.get("label", "").startswith(T.LABEL_EXAMPLE) or s.get("label") == T.LABEL_APPRAISAL_KIND
                           for s in hit), hit
    assert ids["professional"].intent == "professional" and ids["professional"].cited[0]["document_id"] == world.std
    assert all(c.pools == PROFESSIONAL_POOLS for c in ids["professional"].tools)
    assert ids["property"].intent == "property" and all(c.pools == PROPERTY_POOLS for c in ids["property"].tools)
    # the appraisal is an appraisal in every surface the user sees
    row = next(d for d in world.client.get("/api/documents").json()["documents"] if d["id"] == world.d5)
    assert row["source_kind"] == "appraisal" and "professional" not in row


# =======================================================================================================================
# AE9: two editions, and no standards
# =======================================================================================================================

def test_ae9_two_editions_are_both_shown_with_their_dates_and_an_office_with_no_standard_is_told_so(world, db, client,
                                                                                             monkeypatch, calls):
    ed2 = ingest(world, "standard_ed2")
    ask = "מה הדרישה הנוכחית בסעיף 2.1 של תקן 98.1?"
    cid = conversation(world)
    t = turn(world, cid, ask,
             [[call("find_standards", query=None, page=None), call("search", query="עסקאות השוואה לפחות", document_ids=None, limit=None)],
              cites(("standard", world.std), ("standard", ed2))], None, intent="professional")
    assert t.answer["status"] == "answered" and t.intent == "professional"
    assert [c.name for c in t.tools] == ["find_standards", "search"]
    assert all(c.pools == PROFESSIONAL_POOLS for c in t.tools)
    listing = t.tools[0].output
    assert "סדר המהדורות: ידוע" in listing and T.MSG_ORDER_UNKNOWN not in listing
    assert "05.10.2025" in listing and "01.04.2026" in listing and "גרסה 1.0" in listing and "גרסה 2.0" in listing
    assert re.search(r"האחרונה[^\n]*01\.04\.2026|המאוחרת[^\n]*01\.04\.2026", listing), listing
    found: dict[str, str] = {}
    for s in t.tools[1].sources:
        if s["kind"] == "standard":
            found[s["document_id"]] = found.get(s["document_id"], "") + s["body"]
    assert THREE in found[world.std] and FIVE in found[ed2]  # each edition's own provision, under its own edition
    cited = {s["document_id"]: s for s in t.cited}
    assert set(cited) == {world.std, ed2}
    assert standard_of(cited[world.std]) == ("98.1", "גרסה 1.0", "2.1") and standard_of(cited[ed2]) == ("98.1", "גרסה 2.0", "2.1")
    assert cited[world.std]["title"] == "תקן 98.1 (גרסה 1.0, בתוקף מ-05.10.2025)"
    assert cited[ed2]["title"] == "תקן 98.1 (גרסה 2.0, בתוקף מ-01.04.2026)"
    assert {(x["standard"]["edition"], x["standard"]["section"]) for x in t.context["sources"]} == {("גרסה 1.0", "2.1"), ("גרסה 2.0", "2.1")}

    # an office with no standard or professional document at all: the pool is empty, and the turn records the gap
    bare = make_office(db, "משרד בלי תקנים", "admin-bare@example.test")
    professional_file(bare, bare.default_group_id, [("paragraph", None, [], "נכס ברחוב הרקפות 5. עסקאות השוואה בנכסים דומים.", 1)],
                      title="שומה הרקפות 5 (סינתטי)", source_kind="appraisal")
    client.post("/api/auth/logout")
    login(client, "admin-bare@example.test")
    def judge(input: str) -> dict:  # the requirement is judged missing: no source answers it
        units = [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": [requirement(id=i, status="missing") for i in ids]}

    agent = ScriptedAgent([[call("find_standards", query=None, page=None),
                            call("search", query="עסקאות השוואה לפחות", document_ids=None, limit=None)],
                           final("נוסח התקן אינו במאגר.", "not_found")], intent="professional", judge=judge,
                          request=[component("הדרישה בסעיף 2.1 של תקן 98.1")])
    cloud(monkeypatch, bare, agent)
    before = len(calls)
    m = send(client, new_conversation(client), ask)
    assert m["status"] == "done", m
    made = calls[before:]
    assert made[0].name == "find_standards" and made[0].output == T.MSG_NO_STANDARDS and made[0].absent
    assert T.MSG_NO_STANDARD_POOL in made[1].output and made[1].absent
    assert all(c.pools == PROFESSIONAL_POOLS for c in made)
    assert all(s["kind"] == "appraisal" for s in made[1].sources)  # the other permitted pool still answers, as examples
    assert not [s for s in m["answer"]["sources"] if s["source_kind"] != "appraisal"]
    (missing,) = [c for c in m["answer"]["ledger"]["requirements"] if c["limitation"]]
    assert missing["limitation"] == "standard_absent" and missing["limitation_text"] == "נוסח התקן אינו במאגר"
    assert "נוסח התקן אינו במאגר" in m["answer"]["markdown"]  # it says what the repository lacks, not that a search failed
    assert "לא נמצא בחיפוש" not in m["answer"]["markdown"]
