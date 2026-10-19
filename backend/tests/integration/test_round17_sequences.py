"""The three round-16 failures, reproduced on the round-16 build (round 17 U10: R16; AE1, AE3, AE5; KTD1, KTD3, KTD5).

Each sequence is a ScriptedAgent turn over synthetic documents (office B): the model and the judge are scripted, so a
test proves what the server does with the judge's scores and the model's answers, not the judgement of either. They are
recorded failing before any fix and marked ``xfail(strict=True)`` with the unit that owns the fix; the owning unit removes
the marker when it lands (a strict xfail that starts passing fails the suite, so the marker cannot be forgotten).

1. An old ``full`` hides a gap (AE1, owned by U1/U3). Round 1 gives the component ``full`` over two rate sentences while a
   third sentence of the answer fails; the repair rewrites that sentence only. The fresh requirements call over the
   edited answer scores the component ``partial`` with a validated quote of the rate the answer omits. The vote carried
   from the unchanged sentences wins by "best status", so the component ends ``full`` and the gap is never asked for. U3 closed
   this sequence (the validated support becomes an omitted ledger item, which caps the component at partial whatever a vote
   says); U1's version rules (reuse and history of decisions) are still its own.
2. A list answered in part (AE3, owned by U3 with the ledger of U2). A read table lists three adjustments, the answer gives
   two with citations and a lenient judge scores the component ``full``: nothing asks for the third. Expressed in
   observable outcomes (what the answer states, the repair request, the tools called); once the item ledger exists, the
   scripted final answers gain ``items`` (the three adjustments, the third declared ``included``) and the same
   outcomes hold.
3. A scope qualifier that disappears (AE5, owned by U4 for the paths in which the table read lacks the paragraph and by U5 for
   the value written without it). A sensitivity table is followed by a short paragraph saying
   that it refers to one valuation approach only. Three paths of the observed failure: (a) the agent writes the value with
   no scope; (b) it writes the scope and the judge cuts it as unsupported (the table read never held the paragraph); (c) the
   final check judges the cut sentence unsupported because it lacks the limitation and removes it. In each the final answer
   must keep the value with the scope phrase and a citation, and never show the value as the property's total.

The two PDFs are the synthetic fixtures of ``tests/fixtures/round17`` (``scripts/generate_fixtures.py --only round17``);
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
from tests.integration.test_round16_sequences import (
    RecordingAgent,
    _source_line,
    block_document,
    checks_of,
    open_section,
    repair_calls,
    repair_prompt,
)
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round17" / "manifest.json").read_text(encoding="utf-8"))["documents"]
SENSITIVITY, ADJUSTMENTS = MANIFEST["sensitivity"], MANIFEST["adjustments"]

# the owning unit removes its marker when it lands (strict: a test that starts passing with the marker left fails)
U5 = pytest.mark.xfail(strict=True, reason="U5 not landed: a table value is written without the scope its paragraph states")


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def login_b(client) -> None:
    login(client, "admin-b@example.test")


def ingest_pdf(office, monkeypatch, spec: dict) -> str:
    """A round-17 fixture ingested without OCR or vision, in the office's default group; the document id."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    path = FIXTURES / spec["file"]
    return ingest(office, path, path.stem)[0]


def read_table(caption: str):
    """A scripted step that reads the table captioned ``caption`` through the handle the first step's outline gave."""
    return lambda items: [read(table=handle_of(_outputs(items)[0], caption))]


def units_of(input: str) -> dict[int, str]:
    """The units a judge call shows, by index."""
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def asked_of(input: str) -> list[str]:
    """The ids of the requirements a judge call scores."""
    return re.findall(r'<requirement id="(N[\d.]+)"', input)


def trace_of(client, m: dict, component_id: str) -> list[dict]:
    """The component's entry in each verification round's requirements trace, in order (diagnostics)."""
    trace = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["requirements_trace"]
    return [c for rnd in trace for c in rnd["components"] if c["id"] == component_id]


# --- 1. an old full vote hides a gap (AE1) ---------------------------------------------------------------------------

RATES_TITLE = "שומת הדגמה לשיעורי היוון לפי שימוש"
RATES_HEAD = "7. שיעורי ההיוון"
RATES_LINES = ["שיעור היוון למשרדים 6.5%.", "שיעור היוון למסחר 7.0%.", "שיעור היוון לחניה 8.25%."]
RATES_NOTE = "שיעורי ההיוון נקבעו לפי סוג השימוש בנכס."
RATES_QUESTION = "אילו שיעורי היוון נקבעו בשומה?"
RATES_ASK = "שיעורי ההיוון שנקבעו בשומה"
RATES_OMITTED = "8.25%"
RATES_FIRST = ("שיעור היוון למשרדים 6.5% [S1].\nשיעור היוון למסחר 7.0% [S1].\n"
               "שיעורי ההיוון נקבעו על ידי ועדה מקצועית [S1].")  # the last sentence is not in the source
RATES_EDITED = ("שיעור היוון למשרדים 6.5% [S1].\nשיעור היוון למסחר 7.0% [S1].\n"
                "שיעורי ההיוון נקבעו לפי סוג השימוש [S1].")  # only the failing sentence is rewritten
RATES_REPAIRED = RATES_EDITED + "\nשיעור היוון לחניה 8.25% [S1]."  # what the owning unit's repair adds


def rates_judge(seen: list[str]):
    """A judge that finds the sentence about a committee unsupported and scores each component over the units it is shown:
    ``full`` when a shown unit states a rate (a lenient judge sees two rates of three), else ``partial`` over the shown
    units with the validated quote of the rate the answer omits — as a call over a repair that edited only the sentence
    which does not state a rate would."""
    def judge(input: str) -> dict:
        seen.append(input)
        units = units_of(input)
        out = {"verdicts": [{"index": i, "verdict": "unsupported" if "ועדה" in t else "supported", "reason": "בדיקה",
                             "supported_by": []} for i, t in units.items()]}
        ids = asked_of(input)
        if not ids:
            return out
        giving = [i for i, t in units.items() if "%" in t]
        found = _source_line(input, RATES_OMITTED)
        scored = []
        for rid in ids:
            if giving or RATES_OMITTED in "".join(units.values()):
                scored.append(requirement(id=rid, units=giving or list(units)))
            else:
                support = [{"id": found[0], "quote": found[1]}] if found else []
                scored.append(requirement(id=rid, status="partial", units=list(units)) | {"missing": RATES_ASK,
                                                                                         "support": support})
        return out | {"requirements": scored}
    return judge


def test_an_old_full_vote_does_not_hide_a_rate_a_fresh_vote_on_the_edited_answer_validated_as_omitted(
        client, office, monkeypatch):
    doc = block_document(office, RATES_TITLE, [(RATES_HEAD, [*RATES_LINES, RATES_NOTE])], "c" * 64)
    steps = [[call("outline", document=doc)], open_section(RATES_HEAD), final(RATES_FIRST, documents=[doc]),
             final(RATES_EDITED, documents=[doc]), final(RATES_REPAIRED, documents=[doc])]
    seen: list[str] = []
    agent = ScriptedAgent(steps, judge=rates_judge(seen), request=[component(RATES_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), RATES_QUESTION)
    assert m["status"] == "done", m
    (n1,) = [r["id"] for r in m["answer"]["ledger"]["requirements"]]
    # the scenario as scripted: round 1 gives N1 full over the unchanged sentences and fails the committee sentence; the
    # repair edits that sentence only; round 2 scores N1 partial, with the omitted rate accepted as validated support,
    # beside the full vote carried from the unchanged sentences
    trace = trace_of(client, m, n1)
    assert [c["final_status"] for c in trace][0] == "full", trace
    assert len(agent.seen) >= 4 and len(trace) == 2, (len(agent.seen), trace)
    votes = trace[1]["votes"]
    assert {(v["status"], bool(v["reused"])) for v in votes} == {("full", True), ("partial", False)}, votes
    (fresh,) = [v for v in votes if v["status"] == "partial"]
    assert [e["outcome"] for e in fresh["support"]] == ["accepted"], fresh
    # AE1: the decision is made on the current answer; the old full vote is history only
    assert trace[1]["final_status"] == "partial", trace[1]
    assert ("text_found", n1) in checks_of(client, m)[1]  # the omitted rate is asked for (REQ_TEXT_FOUND)
    assert m["answer"]["verification"]["completeness"]["status"] == "partial"


# --- 2. a list answered in part (AE3) ----------------------------------------------------------------------------------

ADJ_CAPTION = ADJUSTMENTS["caption"]
ADJ_QUESTION = "אילו התאמות יושמו בשומה?"
ADJ_ASK = "ההתאמות שיושמו בשומה"
(ADJ_LOCATION, ADJ_AREA, ADJ_FLOOR) = [(row[0], row[1]) for row in ADJUSTMENTS["rows"]]
ADJ_TWO = f"{ADJ_LOCATION[0]}: {ADJ_LOCATION[1]} [S1].\n{ADJ_AREA[0]}: {ADJ_AREA[1]} [S1]."
ADJ_THREE = ADJ_TWO + f"\n{ADJ_FLOOR[0]}: {ADJ_FLOOR[1]} [S1]."


def lenient_judge(seen: list[str] | None = None):
    """A lenient judge, as observed in round 16: every unit supported and every component ``full`` over the units shown."""
    def judge(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = units_of(input)
        out = {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []} for i in units]}
        ids = asked_of(input)
        return out | ({"requirements": [requirement(id=r, units=list(units)) for r in ids]} if ids else {})
    return judge


def adjustment_items(state_of_third: str = "included") -> list[dict]:
    """The item ledger the answering agent declares for the table: the three adjustments, each quoted from its row of the read
    table (S1), the third as given."""
    rows = (ADJ_LOCATION, ADJ_AREA, ADJ_FLOOR)
    return [{"component": "N1", "label": row[0], "source": "S1", "quote": f"{row[0]} | {row[1]}", "value": row[1],
             "condition": "", "state": state_of_third if row is ADJ_FLOOR else "included",
             "reason": "המקור נקרא בקריאה לא ודאית" if row is ADJ_FLOOR and state_of_third != "included" else ""}
            for row in rows]


def test_the_third_adjustment_a_read_table_lists_and_the_answer_omits_is_added_from_it_without_a_new_search(
        client, office, monkeypatch):
    # The scripted final answers declare ``items``: the three adjustments of the table, the third ``included`` (AE3). The
    # lenient judge scores the component full over the two it is shown; the ledger, not the vote, decides.
    doc = ingest_pdf(office, monkeypatch, ADJUSTMENTS)
    steps = [[call("outline", document=doc)], read_table(ADJ_CAPTION),
             final(ADJ_TWO, documents=[doc], items=adjustment_items()),
             final(ADJ_THREE, documents=[doc], items=adjustment_items())]
    agent = RecordingAgent(steps, judge=lenient_judge(), request=[component(ADJ_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), ADJ_QUESTION)
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    # a list the source gives in three is not shown as complete with two: the third item is stated, or the component is not full
    assert ADJ_FLOOR[0] in md or m["answer"]["verification"]["completeness"]["status"] != "full", md
    # AE3: it is added by a repair that names the table it was read in, and that calls no tool
    assert len(agent.seen) == 4, len(agent.seen)
    repair = repair_prompt(agent)
    assert ADJ_FLOOR[0] in repair and "S1" in repair
    assert not repair_calls(agent)
    assert f"{ADJ_FLOOR[0]}: {ADJ_FLOOR[1]} [S1]" in md
    (n1,) = [r["id"] for r in m["answer"]["ledger"]["requirements"]]
    assert checks_of(client, m)[0] == [("text_found", n1)]  # one REQ_TEXT_FOUND problem for the component, nothing else
    assert m["answer"]["verification"]["completeness"]["status"] == "full"
    # no search at any step of the turn: the table was read once and nothing else was looked for
    called = [i["name"] for i in agent.seen[-1] if isinstance(i, dict) and i.get("type") == "function_call"]
    assert called == ["outline", "read"], called  # the items of each step hold the earlier ones: the whole turn's calls
    assert agent.choices[-1] == "none" and agent.choices[:-1] == [None] * 3  # only the repair step ran with its tools off
    # the ledger is in the diagnostics: the three items, the third open in the first round and covered in the second
    diagnostics = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()
    ledger = diagnostics["resolution"]["item_ledger"]
    assert [(i["id"], i["state"]) for i in ledger["items"]] == [("I1", "covered"), ("I2", "covered"), ("I3", "covered")]
    rounds = trace_of(client, m, n1)
    assert [[i["state"] for i in c["items"]] for c in rounds] == [["covered", "covered", "omitted"], ["covered"] * 3]
    assert [c["final_status"] for c in rounds] == ["partial", "full"]


def test_an_item_the_answer_still_omits_after_the_repair_is_stated_as_read_and_not_used_never_as_not_found(
        client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, ADJUSTMENTS)
    steps = [[call("outline", document=doc)], read_table(ADJ_CAPTION),
             final(ADJ_TWO, documents=[doc], items=adjustment_items()),
             final(ADJ_TWO, documents=[doc], items=adjustment_items()),
             final(ADJ_TWO, documents=[doc], items=adjustment_items())]
    agent = ScriptedAgent(steps, judge=lenient_judge(), request=[component(ADJ_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), ADJ_QUESTION)
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    (line,) = [ln for ln in md.splitlines() if ADJ_FLOOR[0] in ln]
    assert "S1" in line and "לא נעשה בו שימוש" in line and "נקרא" in line, line
    assert not re.search(r"לא נמצא|לא אותר|לא מופיע", md)
    assert m["answer"]["status"] == "partial" and m["answer"]["verification"]["completeness"]["status"] == "partial"


def test_a_third_adjustment_declared_not_verifiable_with_a_reason_keeps_the_answer_full_and_is_stated_in_a_line(
        client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, ADJUSTMENTS)
    items = adjustment_items("not_verifiable")
    steps = [[call("outline", document=doc)], read_table(ADJ_CAPTION), final(ADJ_TWO, documents=[doc], items=items)]
    agent = ScriptedAgent(steps, judge=lenient_judge(), request=[component(ADJ_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), ADJ_QUESTION)
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    assert len(agent.seen) == 3 and checks_of(client, m)[0] == []  # nothing to repair: the item is explicitly dispositioned
    (line,) = [ln for ln in md.splitlines() if ADJ_FLOOR[0] in ln]
    assert "המקור נקרא בקריאה לא ודאית" in line and "S1" in line
    assert m["answer"]["verification"]["completeness"]["status"] == "full"


# --- 3. a scope qualifier after a table (AE5) --------------------------------------------------------------------------

SCOPE_CAPTION, SCOPE_PHRASE, SCOPE_NOTE = SENSITIVITY["caption"], SENSITIVITY["scope_phrase"], SENSITIVITY["scope_note"]
OTHER_CAPTION = SENSITIVITY["other_caption"]
VALUE = SENSITIVITY["rows"][1][1]  # 11,800: the value at a 6.0% rate
SCOPE_QUESTION = "מהו שווי הנכס בשיעור היוון של 6.0% לפי טבלת הרגישות?"
SCOPE_ASK = "שווי הנכס בשיעור היוון של 6.0% לפי טבלת הרגישות"
VALUE_CLAUSE = f"בשיעור היוון 6.0% שווי הנכס הוא {VALUE} אלפי ₪"
SCOPE_CLAUSE = f"לפי {SCOPE_PHRASE}"
WITHOUT_SCOPE = f"{VALUE_CLAUSE} [S1]."
WITH_SCOPE = f"{VALUE_CLAUSE} {SCOPE_CLAUSE} [S1]."


def scope_judge(seen: list[str] | None = None, final_rejects: bool = False):
    """A judge that holds the source to what it shows. A sentence that states the scope is supported when a shown source
    states it too; else its scope clause is the one unsupported span of a partly supported sentence (the cut). A sentence
    that carries the server's marker and states the value without the scope — the cut sentence, as the final check
    sees it — is unsupported when ``final_rejects`` (the limitation is missing), else supported. Components are ``full``."""
    def judge(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = units_of(input)
        shown = _source_text(input)
        verdicts = []
        for i, t in units.items():
            if VALUE not in t:
                verdict = {"verdict": "supported"}
            elif SCOPE_PHRASE in t and SCOPE_PHRASE not in shown:
                head = t[:t.index(SCOPE_CLAUSE)].rstrip(" ,")
                verdict = {"verdict": "partial", "defect": "part",
                           "claims": [{"span": head, "verdict": "supported"},
                                      {"span": SCOPE_CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
            elif SCOPE_PHRASE not in t and "<server>" in t and final_rejects:
                verdict = {"verdict": "unsupported", "failure": "absent_from_source"}
            else:
                verdict = {"verdict": "supported"}
            verdicts.append({"index": i, "reason": "בדיקה", "supported_by": []} | verdict)
        ids = asked_of(input)
        return {"verdicts": verdicts} | ({"requirements": [requirement(id=r, units=list(units)) for r in ids]}
                                         if ids else {})
    return judge


def _source_text(input: str) -> str:
    """What the judge call shows of the sources and the read passages (not its units or its requirements)."""
    input = re.sub(r"<unit index.*?</unit>", "", input, flags=re.S)
    return re.sub(r"<requirement .*?</requirement>|<hint>.*?</hint>", "", input, flags=re.S)


def scope_turn(client, office, monkeypatch, first: str, judge, repaired: str = WITH_SCOPE):
    """A turn that reads the sensitivity table and answers ``first`` (then ``repaired`` to a repair request)."""
    doc = ingest_pdf(office, monkeypatch, SENSITIVITY)
    steps = [[call("outline", document=doc)], read_table(SCOPE_CAPTION), final(first, documents=[doc]),
             final(repaired, documents=[doc]), final(repaired, documents=[doc])]
    agent = ScriptedAgent(steps, judge=judge, request=[component(SCOPE_ASK)])
    cloud(monkeypatch, office, agent)
    login_b(client)
    return agent, send(client, new_conversation(client), SCOPE_QUESTION)


def assert_value_keeps_its_scope(m: dict) -> None:
    """The value stays, every line that states it names the scope phrase and cites a source, so it is never shown as the
    property's total (AE5)."""
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    lines = [ln for ln in md.splitlines() if VALUE in ln]
    assert lines, md
    for ln in lines:
        assert SCOPE_PHRASE in ln and re.search(r"\[S\d+\]", ln), ln


def test_a_table_read_carries_the_short_paragraph_that_follows_it(client, office, monkeypatch):
    agent, m = scope_turn(client, office, monkeypatch, WITH_SCOPE, scope_judge())
    assert m["status"] == "done", m
    table_read = agent.tool_outputs(2)[-1]
    assert VALUE in table_read and SCOPE_NOTE in table_read, table_read


@pytest.mark.parametrize("path", [
    pytest.param("a_agent_omits_the_scope", marks=U5),
    pytest.param("b_judge_cuts_the_scope"),
    pytest.param("c_final_check_removes_the_sentence")])
def test_a_value_of_a_table_keeps_the_scope_its_following_paragraph_states_with_its_citation(
        client, office, monkeypatch, path):
    first, judge = {"a_agent_omits_the_scope": (WITHOUT_SCOPE, scope_judge()),
                    "b_judge_cuts_the_scope": (WITH_SCOPE, scope_judge()),
                    "c_final_check_removes_the_sentence": (WITH_SCOPE, scope_judge(final_rejects=True))}[path]
    _, m = scope_turn(client, office, monkeypatch, first, judge)
    assert_value_keeps_its_scope(m)


def test_a_value_of_another_table_does_not_take_the_scope_of_the_table_before_it(client, office, monkeypatch):
    """A guard that holds today and must hold after U4/U5 (R12): the note binds to its own table's values only."""
    other = SENSITIVITY["other_rows"][2]  # +10% rent: 12,700
    answer = f"בעלייה של 10% בדמי השכירות שווי הנכס הוא {other[1]} אלפי ₪ [S1]."
    doc = ingest_pdf(office, monkeypatch, SENSITIVITY)
    steps = [[call("outline", document=doc)], read_table(OTHER_CAPTION), final(answer, documents=[doc])]
    agent = ScriptedAgent(steps, judge=lenient_judge(), request=[component("שווי הנכס בעליית דמי שכירות של 10%")])
    cloud(monkeypatch, office, agent)
    login_b(client)
    m = send(client, new_conversation(client), "מהו שווי הנכס אם דמי השכירות עולים ב-10%?")
    assert m["status"] == "done", m
    md = m["answer"]["markdown"]
    assert other[1] in md and SCOPE_PHRASE not in md, md
