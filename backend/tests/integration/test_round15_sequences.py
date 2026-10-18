"""Model-led understanding with candidates (round 15 U6: R1-R4, KTD1-KTD3, KTD11; KD1, KD2).

The server no longer pins a question to one appraisal unit and no longer asks which unit was meant in the model's
place. A first message, and a follow-up that names another subject, reach the model with a block of candidate units
(a ``U#`` handle, pages, the identity text and the basis of the ranking), which is data; the model searches inside the
unit it chooses and declares it in ``focus.units``. The server validates the declaration against the units it offered
or the turn read, stores a model clarification's offered units for the reply, and keeps every permission check. A
call that names no unit is not silently scoped; the search-only path (no model) keeps its pin and its default scope.

Scripted multi-turn sequences on synthetic files only; every name and figure is invented. The models are scripted: these
tests prove what the server shows, binds, validates and refuses."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.chat import api, contexts, entities, resolve
from app.chat import tools as T
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_coverage import add_doc
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.unit_files import READING, unit_file

pytestmark = pytest.mark.db

PLANTS = ["הארז", "האשל", "הברוש", "התמר", "הדקל", "האורן", "הלוטם", "הצבר", "הרימון", "הסביון", "הנרקיס", "הצפצפה",
          "הדולב", "השקד", "הזית", "התאנה", "הרקפת", "החצב", "הכלנית", "הפרג", "השושן", "היסמין"]
LETTERS = ["א", "ב", "ג", "ד", "ה", "ו", "ז", "ח", "ט", "י", "כ", "ל"]


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client = client
    return a


def _value(n: int) -> str:
    return f"{n + 1},{n + 1}00,000"


@pytest.fixture
def compendium(office):
    """A file of 22 appraisals, each titled by an invented project name and stating its own value."""
    units = [{"title": f"פרויקט {w}", "aliases": [f"פרויקט {w}", w], "identifiers": [f"רחוב {w} {n + 1}"],
              "blocks": [f"פרויקט {w}", f"שווי הנכס {_value(n)} ₪.", f"דמי שכירות חודשיים {n + 1}0 ₪ למ\"ר."]}
             for n, w in enumerate(PLANTS)]
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ שומות גדול סינתטי", units, sha="c" * 64)
    return office


@pytest.fixture
def gafen(office):
    """Twelve appraisals that share the words «מתחם הגפן» and differ by a letter."""
    units = [{"title": f"מתחם הגפן {x}", "aliases": [f"מתחם הגפן {x}"], "identifiers": [f"רחוב הגפן {n + 1}"],
              "blocks": [f"מתחם הגפן {x}", f"שווי הנכס {_value(n)} ₪.", f"דמי שכירות חודשיים {n + 1}0 ₪ למ\"ר."]}
             for n, x in enumerate(LETTERS)]
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ מתחמי הגפן סינתטי", units, sha="g" * 64)
    return office


def _cards(content: str) -> list[tuple[str, str]]:
    """The (handle, title) of each card of the candidates block in the turn's first message."""
    return re.findall(r"^- (U\d+) «([^»]*)»", content, re.M)


def _next(monkeypatch, agent) -> None:
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)


def _focus(doc: str, subject: str = "", units: list[str] | None = None) -> dict:
    return {"metric_as_written": "שווי הנכס", "metric_kind": "value", "unit": "ILS", "period": "none",
            "area_basis": "", "vat": "unknown", "subject": subject, "value_role": "appraiser_determination",
            "document_ids": [doc], "units": units or []}


def _parse(**kw) -> dict:
    return {"relation": "metric_change", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _diagnostics(client, m: dict) -> dict:
    return client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()


def _sid(items, needle: str) -> str:
    outs = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
    return next(m.group(1) for o in outs for m in re.finditer(r'<source id="(S\d+)"[^>]*>[^<]*?' + re.escape(needle), o))


# --- R1: the claim decides the intent; a title phrase is a hint -------------------------------------------------------


def test_a_professional_question_whose_phrase_is_a_document_title_is_professional_with_no_lookup(client, office,
                                                                                                monkeypatch):
    add_doc(office, office.default_group_id, "היוון ההכנסה", ["היוון הוא המרת הכנסה עתידית לשווי נוכחי."], "1" * 64)
    add_doc(office, office.default_group_id, "שומה מקרקעין רחוב הדקל 5 עיר-א (סינתטי)", ["שומה לפי גישת ההכנסות."],
            "2" * 64)
    agent = ScriptedAgent([[call("search", query="היוון הכנסה", document_ids=None, limit=None)],
                           final("היוון הוא המרת הכנסה עתידית לשווי נוכחי [S1].", claims=[
                               {"text": "היוון הוא המרת הכנסה עתידית לשווי נוכחי", "source_ids": ["S1"],
                                "basis": "explicit"}])], intent="professional")
    cloud(monkeypatch, office, agent)
    m = send(client, new_conversation(client), "איך מבצעים היוון ההכנסה בשומה?")
    assert m["status"] == "done", m
    res = _diagnostics(client, m)["resolution"]
    assert res["intent"]["value"] == "professional" and res["decisions"]["intent"] == "professional"
    assert [e["text"] for e in res["intent"]["evidence"]] == ["היוון ההכנסה"]  # recorded as a hint, decides nothing
    assert "lookup" not in res and resolve.CANDIDATES_HEAD not in agent.seen[0][0]["content"]


# --- R3: the model sees candidates, never a pin, and the server asks nothing ------------------------------------------


def test_a_distinctive_name_in_a_22_unit_compendium_is_the_first_card_and_nothing_is_asked(client, compendium,
                                                                                             monkeypatch):
    target = "הדולב"
    agent = ScriptedAgent([[call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
                           lambda items: final(
                               f"שווי הנכס הוא {_value(12)} ₪ [{_sid(items, _value(12))}].",
                               focus=_focus(compendium.doc, f"פרויקט {target}", ["U1"]),
                               claims=[{"text": f"שווי הנכס הוא {_value(12)} ₪",
                                        "source_ids": [_sid(items, _value(12))], "basis": "explicit"}])],
                          intent="property")
    cloud(monkeypatch, compendium, agent)
    m = send(client, new_conversation(client), f"מה שווי הנכס בפרויקט {target}?")
    assert m["status"] == "done" and m["answer"]["status"] == "answered", m
    content = agent.seen[0][0]["content"]
    cards = _cards(content)
    assert cards and cards[0] == ("U1", f"פרויקט {target}") and len(cards) <= 8
    assert "אתה בוחר" in content or resolve.CANDIDATES_HEAD in content
    searched = agent.tool_outputs(1)[0]
    assert _value(12) in searched and _value(0) not in searched  # inside the unit the model named
    assert m["answer"]["unit"]["display"] == f"פרויקט {target}"
    assert "unit=U1" not in content.split(resolve.CANDIDATES_HEAD)[0]  # no scope rule outside the candidates block


def test_a_word_shared_by_all_units_gives_up_to_eight_cards_with_their_basis_and_reaches_the_model(client, gafen,
                                                                                                  monkeypatch):
    agent = ScriptedAgent([final("לאיזה מתחם התכוונת?", "clarification", clarification="לאיזה מתחם התכוונת?",
                                 focus=_focus(gafen.doc, "", ["U1", "U2"]))], intent="property")
    cloud(monkeypatch, gafen, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס במתחם הגפן?")
    assert m["status"] == "done", m
    assert agent.seen, "the turn reached the model: the server asked nothing in its place"
    content = agent.seen[0][0]["content"]
    cards = _cards(content)
    assert len(cards) == 8 and [c[0] for c in cards] == [f"U{i}" for i in range(1, 9)]
    assert re.search(r"ועוד 4 ", content)  # the rest is counted, never listed
    assert resolve.BASIS_PREFIX in content  # each card says why it was ranked
    assert not any(t.endswith(("י", "כ", "ל")) for _, t in cards)  # the cut: the ninth to twelfth are not shown
    res = _diagnostics(client, m)["resolution"]
    assert "candidates" in res["decisions"]


def test_a_card_from_a_document_the_user_may_not_see_is_never_rendered_and_the_count_does_not_reveal_it(db):
    a = make_office(db, "משרד א", "admin@example.test")
    secret = make_group(a, "קבוצה 2")
    emp = make_user(a, "e@example.test", [a.default_group_id])
    unit_file(a, a.default_group_id, "קובץ גלוי סינתטי", [
        {"title": "מתחם הגפן א", "aliases": ["מתחם הגפן א"], "blocks": ["מתחם הגפן א", "שווי 1,000,000 ₪."]},
        {"title": "מתחם הגפן ב", "aliases": ["מתחם הגפן ב"], "blocks": ["מתחם הגפן ב", "שווי 2,000,000 ₪."]}], sha="v" * 64)
    unit_file(a, secret, "קובץ סודי סינתטי", [
        {"title": "מתחם הגפן ג", "aliases": ["מתחם הגפן ג"], "blocks": ["מתחם הגפן ג", "שווי 3,000,000 ₪."]},
        {"title": "מתחם הגפן ד", "aliases": ["מתחם הגפן ד"], "blocks": ["מתחם הגפן ד", "שווי 4,000,000 ₪."]}], sha="s" * 64)
    seen = entities.rank_candidates(a.ctx("employee", emp), "מה שווי הנכס במתחם הגפן?")
    assert seen is not None and seen.total == 2 and len(seen.cards) == 2
    ws = T.Workspace(ctx=a.ctx("employee", emp))
    block = resolve.candidates_block(seen.cards, lambda c: T.unit_handle_of(
        ws, c.row["document_id"], c.row["version_id"], c.row["reading_id"], c.row["unit_no"]), seen.total - len(seen.cards))
    assert "הגפן ג" not in block and "הגפן ד" not in block and "סודי" not in block and "ועוד" not in block
    everything = entities.rank_candidates(a.ctx(), "מה שווי הנכס במתחם הגפן?")
    assert everything.total == 4


# --- R2: a model clarification offers units, and the reply binds to them -----------------------------------------------


def _ask_between(client, gafen, monkeypatch, units: list[str]) -> str:
    asked = "בקובץ כמה מתחמים. לאיזה התכוונת?"
    agent = ScriptedAgent([final(asked, "clarification", clarification=asked,
                                 focus=_focus(gafen.doc, "", units))], intent="property")
    cloud(monkeypatch, gafen, agent)
    cid = new_conversation(client)
    m = send(client, cid, "מה שווי הנכס במתחם הגפן?")
    assert m["status"] == "done" and m["answer"]["status"] == "clarification", m
    return cid


def _reply(client, cid: str, monkeypatch, message: str, steps: list, parse: dict) -> tuple[ScriptedAgent, dict]:
    agent = ScriptedAgent(steps, intent="property")
    agent.on("resolve", parse)
    _next(monkeypatch, agent)
    m = send(client, cid, message)
    assert m["status"] == "done", m
    return agent, m


def test_a_clarification_offering_two_units_answered_the_second_binds_to_the_second_offered_unit(client, gafen,
                                                                                                monkeypatch):
    cid = _ask_between(client, gafen, monkeypatch, ["U3", "U5"])  # the cards are in the order of the ranking
    agent, m = _reply(client, cid, monkeypatch, "השני", [[call("search", query="שווי הנכס", document_ids=None, limit=None,
                                                              unit="U1")],
                                                       final("אין מידע.", "not_found")],
                      _parse(relation="clarification_answer"))
    req = m["answer"]["request"]
    assert req["located_unit"] is not None and req["located_unit"]["display"] == "מתחם הגפן ה"
    assert req["entity_changed"] is True
    assert "מתחם הגפן ה" in agent.seen[0][0]["content"] and "מתחם הגפן ג" not in agent.seen[0][0]["content"]


def test_a_clarification_that_declares_no_unit_binds_nothing_and_the_reply_is_read_as_a_new_message(client, gafen,
                                                                                                   monkeypatch):
    cid = _ask_between(client, gafen, monkeypatch, [])
    agent, m = _reply(client, cid, monkeypatch, "השני", [final("לא ברור לאיזה מתחם הכוונה.", "not_found")],
                      _parse(relation="clarification_answer"))
    req = m["answer"]["request"]
    assert req["located_unit"] is None and req["document_ids"] == []
    assert "pending" not in _diagnostics(client, m)["resolution"]["decisions"]


def test_a_declared_unit_the_server_never_offered_nor_the_model_read_is_dropped(client, gafen, monkeypatch):
    cid = _ask_between(client, gafen, monkeypatch, ["U2", "U40"])
    agent, m = _reply(client, cid, monkeypatch, "הראשון", [final("אין מידע.", "not_found")],
                      _parse(relation="clarification_answer"))
    # only U2 was offered: the bogus handle is dropped, so one unit was offered and "the first" binds to it
    assert m["answer"]["request"]["located_unit"]["display"] == "מתחם הגפן ב"


def test_a_reply_after_the_offered_units_document_was_hidden_does_not_resolve_to_it(client, gafen, monkeypatch):
    cid = _ask_between(client, gafen, monkeypatch, ["U1", "U2"])
    with tenant_tx(gafen.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": gafen.doc})
    agent, m = _reply(client, cid, monkeypatch, "השני", [final("אין לי גישה לנתון הזה.", "not_found")],
                      _parse(relation="clarification_answer"))
    req = m["answer"]["request"]
    assert req is None or (req["located_unit"] is None and req["document_ids"] == [])
    assert "מתחם הגפן ב" not in m["answer"]["markdown"]


# --- R4: follow-ups keep their behaviour --------------------------------------------------------------------------------


def _first_value_turn(client, compendium, monkeypatch, plant: str, n: int) -> str:
    agent = ScriptedAgent([
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
        lambda items: final(f"שווי הנכס הוא {_value(n)} ₪ [{_sid(items, _value(n))}].",
                            focus=_focus(compendium.doc, f"פרויקט {plant}", ["U1"]),
                            claims=[{"text": f"שווי הנכס הוא {_value(n)} ₪",
                                     "source_ids": [_sid(items, _value(n))], "basis": "explicit"}])], intent="property")
    cloud(monkeypatch, compendium, agent)
    cid = new_conversation(client)
    m = send(client, cid, f"מה שווי הנכס בפרויקט {plant}?")
    assert m["status"] == "done" and m["answer"]["status"] == "answered", m
    assert _cards(agent.seen[0][0]["content"])[0] == ("U1", f"פרויקט {plant}")
    return cid


def test_a_metric_change_keeps_the_focus_unit_and_only_its_card_is_rendered(client, compendium, monkeypatch):
    cid = _first_value_turn(client, compendium, monkeypatch, "הדולב", 12)
    agent, m = _reply(client, cid, monkeypatch, "ומה דמי השכירות?", [
        [call("search", query="דמי שכירות חודשיים", document_ids=None, limit=None, unit="U1")],
        final("אין מידע.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    content = agent.seen[0][0]["content"]
    assert _cards(content) == [("U1", "פרויקט הדולב")]  # the focus unit's card alone
    assert "פרויקט הארז" not in content and "ועוד" not in content
    req = m["answer"]["request"]
    assert req["unit_kept"] is True and req["located_unit"]["display"] == "פרויקט הדולב"
    searched = agent.tool_outputs(1)[0]
    assert "130 ₪" in searched
    assert set(re.findall(r'unit_id="[^"]*#(\d+)(?:@[^"]*)?"', searched)) == {"13"}  # inside the 13th appraisal


def test_a_call_that_names_no_unit_is_not_silently_scoped_to_the_focus_unit(client, compendium, monkeypatch):
    cid = _first_value_turn(client, compendium, monkeypatch, "הדולב", 12)
    agent, m = _reply(client, cid, monkeypatch, "ומה דמי השכירות?", [
        [call("search", query="דמי שכירות חודשיים", document_ids=None, limit=12, unit=None)],
        final("אין מידע.", "not_found")],
        _parse(changed_fields=[{"field": "metric_kind", "user_words": "דמי השכירות"}], metric_kind="rent"))
    searched = agent.tool_outputs(1)[0]
    assert "חיפוש בתוך היחידה" not in searched and "(היחידה שהשיחה עוסקת בה" not in searched
    assert len(set(re.findall(r'unit_id="[^"]*#(\d+)(?:@[^"]*)?"', searched))) > 1  # hits of several units


def test_naming_another_project_renders_new_candidates_and_returning_resolves_through_the_stored_focus(
        client, compendium, monkeypatch):
    cid = _first_value_turn(client, compendium, monkeypatch, "הדולב", 12)

    def answer(plant: str, n: int):
        return lambda items: final(
            f"שווי הנכס הוא {_value(n)} ₪ [{_sid(items, _value(n))}].", focus=_focus(compendium.doc, f"פרויקט {plant}", ["U1"]),
            claims=[{"text": f"שווי הנכס הוא {_value(n)} ₪", "source_ids": [_sid(items, _value(n))], "basis": "explicit"}])

    agent, m = _reply(client, cid, monkeypatch, "ובפרויקט האשל?", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")], answer("האשל", 1)],
        _parse(relation="correction", changed_fields=[{"field": "subject", "user_words": "בפרויקט האשל"}],
               subject="פרויקט האשל"))
    assert _cards(agent.seen[0][0]["content"]) == [("U1", "פרויקט האשל")]
    assert m["answer"]["request"]["entity_changed"] is True and m["answer"]["unit"]["display"] == "פרויקט האשל"
    # back to the first topic: the stored focus is the unit the conversation is in now; the model is told so
    agent, m = _reply(client, cid, monkeypatch, "ובפרויקט הדולב?", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")], answer("הדולב", 12)],
        _parse(relation="correction", changed_fields=[{"field": "subject", "user_words": "בפרויקט הדולב"}],
               subject="פרויקט הדולב"))
    assert _cards(agent.seen[0][0]["content"]) == [("U1", "פרויקט הדולב")]
    assert m["answer"]["unit"]["display"] == "פרויקט הדולב"


def test_a_declared_unit_becomes_the_turns_unit_and_a_bogus_one_is_dropped(client, compendium, monkeypatch):
    # offered but never searched: still the declaration of a turn that found nothing
    agent = ScriptedAgent([final("לא נמצא נתון.", "not_found", focus=_focus(compendium.doc, "", ["U1"]))],
                          intent="property")
    cloud(monkeypatch, compendium, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס בפרויקט הדולב?")
    assert m["status"] == "done" and m["answer"]["unit"]["display"] == "פרויקט הדולב"
    bogus = ScriptedAgent([final("לא נמצא נתון.", "not_found", focus=_focus(compendium.doc, "", ["U77"]))],
                          intent="property")
    _next(monkeypatch, bogus)
    m = send(client, new_conversation(client), "מה שווי הנכס בפרויקט הדולב?")
    assert m["status"] == "done" and not m["answer"].get("unit")


# --- tools: the focus unit scopes only the calls that name it; find_documents focuses nothing -------------------------


def test_find_documents_lists_a_single_unit_without_making_it_the_default_scope(compendium):
    ws = T.Workspace(ctx=compendium.ctx())
    found = T.tool_find_documents(ws, "הדולב")
    assert re.search(r"\s+- U\d+ \|", found) and ws.unit_focus is None and ws.located_unit is None
    assert "מוגבלים ליחידה" not in found
    unscoped = T.tool_search(ws, "שווי הנכס", None, 12)
    assert len(set(re.findall(r'unit_id="[^"]*#(\d+)(?:@[^"]*)?"', unscoped))) > 1


def _focused(compendium) -> tuple[T.Workspace, str]:
    """A workspace whose conversation is in the 13th appraisal (a source returned twice in one turn is shown once, so each
    check starts from its own)."""
    ws = T.Workspace(ctx=compendium.ctx())
    handle = T.unit_handle_of(ws, compendium.doc, compendium.ver, READING, 13)
    ws.unit_focus = {"handle": handle, "document_id": compendium.doc, "display": "x"}
    return ws, handle


def test_the_focus_unit_scopes_only_a_call_that_names_it(compendium):
    ws, handle = _focused(compendium)
    assert T.focus_unit_for(ws, None) is None and T.focus_unit_for(ws, [compendium.doc]) is None
    unscoped = T.tool_search(ws, "שווי הנכס", None, 12)
    assert "(היחידה שהשיחה עוסקת בה" not in unscoped
    assert len(set(re.findall(r'unit_id="[^"]*#(\d+)(?:@[^"]*)?"', unscoped))) > 1
    ws, handle = _focused(compendium)
    scoped = T.tool_search(ws, "שווי הנכס", None, 12, handle)
    assert _value(12) in scoped and _value(0) not in scoped  # naming the focus unit works inside it
    # the server's pin of the search-only path keeps its default scope
    ws, handle = _focused(compendium)
    ws.unit_default = True
    assert T.focus_unit_for(ws, None) == handle
    pinned = T.tool_search(ws, "שווי הנכס", None, 12)
    assert _value(12) in pinned and _value(0) not in pinned and f"(היחידה שהשיחה עוסקת בה: {handle})" in pinned


# --- search-only mode: exactly as before -------------------------------------------------------------------------------


def test_search_only_keeps_its_pin_its_default_scope_and_its_deterministic_intent(compendium):
    ws = T.Workspace(ctx=compendium.ctx())
    api._search_only(ws, "מה שווי הנכס בפרויקט הדולב?")
    assert ws.intent == "property" and ws.unit_focus is not None and ws.unit_default is True
    assert ws.located_unit["anchor"]["display"] == "פרויקט הדולב"
    shown = " ".join(s.text for s in ws.sources.values() if not s.is_listing)
    assert _value(12) in shown and _value(0) not in shown  # the pooled search stayed inside the pinned unit
    # an ambiguous name pins nothing and asks nothing: no model, no server question
    both = T.Workspace(ctx=compendium.ctx())
    api._search_only(both, "מה שווי הנכס בפרויקט?")
    assert both.unit_focus is None and both.intent == "unknown"


# --- ranking: name fit, never the number of passages; the basis is shown ------------------------------------------------


def test_the_ranking_puts_a_stated_name_first_and_shows_why_and_never_counts_passages(office):
    many = ["הארז נזכר כאן שוב ושוב."] * 9
    unit_file(office, office.default_group_id, "קובץ דירוג סינתטי", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", "שווי 1,000,000 ₪."]},
        {"title": "פרויקט הארז הצפוני", "aliases": ["הארז הצפוני"], "blocks": ["פרויקט הארז הצפוני", *many]},
        {"title": "פרויקט האשל", "aliases": ["האשל"], "blocks": ["פרויקט האשל", "שווי 3,000,000 ₪."]}], sha="r" * 64)
    ranked = entities.rank_candidates(office.ctx(), "מה שווי הנכס בפרויקט הארז?")
    assert [c.row["title"] for c in ranked.cards] == ["פרויקט הארז", "פרויקט הארז הצפוני"]
    assert all(c.basis for c in ranked.cards) and ranked.total == 2
    assert ranked.cards[0].basis != ranked.cards[1].basis  # a stated name, then the words of a longer name
    assert entities.rank_candidates(office.ctx(), "כמה שומות יש בקובץ?") is None


# --- U7: the answer declares its unit, and verification reads distinguishing identity only (KTD7, KTD8; R3, R10, R11) -----

@pytest.fixture
def shared(office):
    """Four appraisals of one compendium that share the project word «פרויקט הגפן» (an alias of every one of them) and
    differ by a letter; each states its own value."""
    units = [{"title": f"פרויקט הגפן {x}", "aliases": ["פרויקט הגפן", f"הגפן {x}"], "identifiers": [f"רחוב הגפן {n + 1}"],
              "blocks": [f"פרויקט הגפן {x}", f"שווי הנכס {_value(n)} ₪.", f"דמי שכירות חודשיים {n + 1}0 ₪ למ\"ר."]}
             for n, x in enumerate(LETTERS[:4])]
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ פרויקטי הגפן סינתטי", units, sha="h" * 64)
    return office


def _cites(office, n: int, declared: list[str], claim: str = "שווי הנכס"):
    """A scripted final answer that states the value of the ``n``-th appraisal as the passage the turn read, declaring
    ``declared`` (U# handles). A function of the turn's items: the passage's id is the turn's."""
    def make(items):
        sid = _sid(items, _value(n))
        return final(f"{claim} הוא {_value(n)} ₪ [{sid}].", focus=_focus(office.doc, "", declared),
                     claims=[{"text": f"{claim} הוא {_value(n)} ₪", "source_ids": [sid], "basis": "explicit"}])
    return make


def _read_all():
    return [call("search", query="שווי הנכס", document_ids=None, limit=None, unit=None)]


def _ask(client, office, monkeypatch, question: str, steps: list) -> tuple[ScriptedAgent, dict]:
    agent = ScriptedAgent([_read_all(), *steps], intent="property")
    cloud(monkeypatch, office, agent)
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return agent, m


def test_a_value_of_the_declared_unit_is_kept_in_a_compendium_whose_units_share_a_project_word(client, shared, monkeypatch):
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [_cites(shared, 0, ["U1"])])
    a = m["answer"]
    assert a["status"] == "answered" and _value(0) in a["markdown"] and a["verification"]["removed"] == 0, a
    assert a["unit"]["display"] == "פרויקט הגפן א"


def test_a_value_of_another_unit_than_the_declared_one_is_removed_after_the_repair_round(client, shared, monkeypatch):
    wrong = _cites(shared, 1, ["U1"])  # the answer declares the first appraisal and states the second one's value
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [wrong, wrong, wrong])
    a = m["answer"]
    assert len(agent.seen) >= 3, "the model was asked to repair before anything was removed"
    assert _value(1) not in a["markdown"] and a["verification"]["removed"] >= 1, a
    assert [r["failure_kind"] for r in a["verification"]["removals"]] == ["wrong_subject"]


def test_a_question_naming_one_unit_distinctively_outranks_an_answer_declaring_and_citing_another(client, shared,
                                                                                                monkeypatch):
    wrong = _cites(shared, 1, ["U2"])  # declares and cites the second appraisal under a question about the first
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן א?", [wrong, wrong, wrong])
    a = m["answer"]
    assert _value(1) not in a["markdown"] and a["verification"]["removed"] >= 1, a
    assert [r["failure_kind"] for r in a["verification"]["removals"]] == ["wrong_subject"]
    # the same question answered from the unit it names keeps its value
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן א?", [_cites(shared, 0, ["U1"])])
    assert _value(0) in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] == 0


def test_an_answer_declaring_no_unit_whose_claim_names_the_asked_unit_distinctively_loses_a_siblings_value(client, shared,
                                                                                                         monkeypatch):
    wrong = _cites(shared, 1, [], claim="שווי הנכס בפרויקט הגפן א")
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [wrong, wrong, wrong])
    assert _value(1) not in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] >= 1
    right = _cites(shared, 0, [], claim="שווי הנכס בפרויקט הגפן א")
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [right])
    assert _value(0) in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] == 0


def test_a_declared_unit_nobody_offered_or_read_is_dropped_and_verification_falls_back_to_the_names(client, shared,
                                                                                                  monkeypatch):
    bogus = _cites(shared, 0, ["U77"], claim="שווי הנכס בפרויקט הגפן ב")  # names the second unit, cites the first's value
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [bogus, bogus, bogus])
    a = m["answer"]
    assert not a.get("unit"), "the bogus declaration set no unit"
    assert _value(0) not in a["markdown"] and a["verification"]["removed"] >= 1
    # nothing distinctive was written and nothing valid was declared: the shared word decides nothing, so nothing is removed
    agent, m = _ask(client, shared, monkeypatch, "מה שווי הנכס בפרויקט הגפן?", [_cites(shared, 2, ["U77"])])
    assert _value(2) in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] == 0


@pytest.fixture
def edge(office):
    """Two appraisals sharing a project word whose boundary is only probable: a unit-scoped search of the first reaches
    over the edge into the second (round 9 U9)."""
    units = [{"title": "פרויקט הכרמל א", "aliases": ["פרויקט הכרמל", "הכרמל א"], "blocks": ["פרויקט הכרמל א", f"שווי הנכס {_value(0)} ₪."]},
             {"title": "פרויקט הכרמל ב", "aliases": ["פרויקט הכרמל", "הכרמל ב"], "certainty": "probable",
              "blocks": ["פרויקט הכרמל ב", f"שווי הנכס {_value(1)} ₪."]}]
    office.doc, office.ver = unit_file(office, office.default_group_id, "קובץ פרויקטי הכרמל סינתטי", units, sha="k" * 64)
    return office


def test_a_hit_across_a_probable_edge_is_evidence_for_the_neighbour_not_for_the_declared_unit(client, edge, monkeypatch):
    scoped = [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")]

    def answer(n):
        def make(items):
            sid = _sid(items, _value(n))
            return final(f"שווי הנכס הוא {_value(n)} ₪ [{sid}].", focus=_focus(edge.doc, "", ["U1"]),
                         claims=[{"text": f"שווי הנכס הוא {_value(n)} ₪", "source_ids": [sid], "basis": "explicit"}])
        return make

    agent = ScriptedAgent([scoped, answer(0)], intent="property")
    cloud(monkeypatch, edge, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס בפרויקט הכרמל?")
    searched = agent.tool_outputs(1)[0]
    assert _value(0) in searched and _value(1) in searched, "the search reached over the probable edge"
    assert _value(0) in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] == 0
    wrong = answer(1)
    agent = ScriptedAgent([scoped, wrong, wrong, wrong], intent="property")
    _next(monkeypatch, agent)
    m = send(client, new_conversation(client), "מה שווי הנכס בפרויקט הכרמל?")
    assert _value(1) not in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] >= 1
