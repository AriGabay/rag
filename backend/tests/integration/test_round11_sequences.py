"""Round 11 sequences, end to end with a scripted model (U12: R3, R4, R11, R13, R14, R16, R19, R20, R23; AE1, AE3, AE5, AE7).

The synthetic round-11 fixtures (``tests/fixtures/round11``: appraisals of one invented property, הצאלון 7, at three valuation
dates with report dates that differ from them, one appraisal with no valuation date, a neighbour on the same street, and two
rent opinions that differ in period and VAT; the two synthetic standard editions are the round-10 files) are read by the real
worker. The models are scripted: what is proven is what the server decides, stores and orders, never wording.

- (1) a retry after toggling runs with the state the message was sent with: ``resolution.time`` says ``none`` and the answer shows
  the switch off (AE5, R14);
- (2) a new conversation sends with the switch on, so its turn's mode is ``recent`` (R2);
- (3) an explicit year in the question is the mode ``explicit`` with the switch on and off (R11);
- (4) search-only (the office's cloud setting off): a property question reads the appraisals newest valuation first with their date
  lines and no standard in that group; an unknown question gets two headed groups and claims no intent (R19, R20, AE7);
- (5) a professional question after a property question is professional, looks nothing up and lists no property, whatever the
  switch says (R16, R23);
- (6) the timeline payload end to end when the scripted agent registers three (and then four) dated values of הצאלון 7 (R13, AE1).

Every name, address and amount is invented."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import worker
from app.chat import tools as T
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_search_only import groups as search_groups
from tests.integration.test_chat_timeline import _focus_of, cited, sids_by_document, take
from tests.integration.test_round10_sequences import (
    MISSES,
    _focus,
    assert_professional,
    calls,  # noqa: F401 - the recorded tool calls
    cites,
    professional_turn,
    property_turn,
    search,
)
from tests.integration.test_round10_sequences import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round11" / "manifest.json").read_text(encoding="utf-8"))["documents"]
HATZALON_7 = ("hatzalon7_a", "hatzalon7_b", "hatzalon7_c", "hatzalon7_undated", "rent_monthly_with_vat", "rent_yearly_without_vat")
ALL_APPRAISALS = (*HATZALON_7, "hatzalon11")
TIME_KEY = "time"

RENT_Q = "מה דמי השכירות הראויים לנכס ברחוב הצאלון 7?"
VALUE_Q = "מה שווי הנכס ברחוב הצאלון 7?"
YEAR_Q = "מה היה השווי ב-2022?"


# --- the world ---------------------------------------------------------------------------------------------------------

def ingest(office, key: str) -> str:
    """A round-11 (or reused round-10) fixture uploaded as the kind its manifest gives and read by the real worker."""
    d = MANIFEST[key]
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


def office_with(db, client, monkeypatch, calls, keys: tuple[str, ...], *, cloud_on: bool = True):  # noqa: F811
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client, a.monkeypatch, a.calls, a.ids = client, monkeypatch, calls, {}
    if cloud_on:
        cloud(monkeypatch, a, ScriptedAgent([]))
    for key in keys:
        ingest(a, key)
    return a


@pytest.fixture
def world(db, client, monkeypatch, calls):  # noqa: F811
    """Cloud on: the three dated appraisals of הצאלון 7, the undated one, the neighbour and the first standard edition."""
    return office_with(db, client, monkeypatch, calls, (*ALL_APPRAISALS, "standard_ed1"))


@pytest.fixture
def compact(db, client, monkeypatch, calls):  # noqa: F811
    """Cloud on: only the appraisals of the one property that the timeline sequences register values of."""
    return office_with(db, client, monkeypatch, calls, ("hatzalon7_a", "hatzalon7_b", "hatzalon7_c", "hatzalon7_undated", "hatzalon11"))


@pytest.fixture
def search_only(db, client, monkeypatch, calls):  # noqa: F811
    """Cloud off (search-only): every appraisal of the fixtures and both standard editions."""
    return office_with(db, client, monkeypatch, calls, (*ALL_APPRAISALS, "standard_ed1", "standard_ed2"), cloud_on=False)


def set_switch(client, cid: str, on: bool) -> None:
    r = client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": on})
    assert r.status_code == 200 and r.json()["prefer_recent"] is on, r.text


def time_of(client, message: dict) -> dict:
    return client.get(f"/api/chat/messages/{message['id']}/diagnostics").json()["resolution"][TIME_KEY]


def use(world, agent: ScriptedAgent) -> ScriptedAgent:
    world.monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    return agent


def rent_agent(doc: str) -> ScriptedAgent:
    """A property turn that searches the document of one appraisal and answers from it (the focus: the rent of הצאלון 7)."""
    return ScriptedAgent([search("דמי שכירות ראויים", [doc]), cites(("appraisal", doc), focus=_focus(doc, "הצאלון 7", "rent"))],
                         intent="property")


# =======================================================================================================================
# (1) a retry after toggling (AE5, R14)
# =======================================================================================================================

def test_a_retry_after_toggling_runs_with_the_state_the_message_was_sent_with_not_the_conversations(world):
    doc = world.ids["hatzalon7_c"]
    cid = new_conversation(world.client)
    use(world, rent_agent(doc))
    r = world.client.post(f"/api/chat/conversations/{cid}/messages", json={"content": RENT_Q, "prefer_recent": False})
    assert r.status_code == 200, r.text
    sent = world.client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    assert sent["status"] == "done" and not MISSES, (sent, MISSES)
    assert sent["time_preference"]["prefer_recent"] is False
    assert time_of(world.client, sent)["mode"] == "none" and time_of(world.client, sent)["prefer_recent"] is False

    set_switch(world.client, cid, True)  # the user turns the switch on afterwards
    use(world, rent_agent(doc))
    retried = world.client.post(f"/api/chat/messages/{sent['id']}/retry")
    assert retried.status_code == 200, retried.text
    again = world.client.get(f"/api/chat/messages/{retried.json()['id']}").json()
    assert again["status"] == "done" and not MISSES, (again, MISSES)
    t = time_of(world.client, again)
    assert (t["mode"], t["prefer_recent"]) == ("none", False), t
    assert again["time_preference"]["prefer_recent"] is False and again["time_preference"] == sent["time_preference"]
    assert again["answer"]["time"]["mode"] == "none" and again["answer"]["time"]["prefer_recent"] is False
    # the retry does not write the old state back over the user's later choice
    assert world.client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is True
    # and the next message of the conversation takes the toggled switch
    use(world, rent_agent(doc))
    nxt = send(world.client, cid, RENT_Q)
    assert time_of(world.client, nxt)["mode"] == "recent" and nxt["time_preference"]["prefer_recent"] is True


# =======================================================================================================================
# (2) a new conversation prefers recent data; (3) an explicit year overrides the switch
# =======================================================================================================================

def test_a_new_conversation_starts_on_and_its_first_turn_is_in_the_recent_mode(world):
    cid = new_conversation(world.client)
    assert world.client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is True
    t = property_turn(world, cid, VALUE_Q, world.ids["hatzalon7_c"], "הצאלון 7", "value", first=True, query="שווי הנכס")
    assert t.answer["status"] == "answered", t.answer
    res = time_of(world.client, t.message)
    assert (res["mode"], res["source"], res["prefer_recent"], res["requested"]) == ("recent", "switch", True, None), res
    assert t.message["time_preference"]["prefer_recent"] is True
    assert t.answer["time"]["mode"] == "recent" and t.answer["time"]["prefer_recent"] is True


@pytest.mark.parametrize("on", [True, False])
def test_an_explicit_year_in_the_question_is_the_explicit_mode_with_the_switch_on_and_off(world, on):
    cid = new_conversation(world.client)
    set_switch(world.client, cid, on)
    t = property_turn(world, cid, YEAR_Q, world.ids["hatzalon7_a"], "הצאלון 7", "value", first=True, query="שווי הנכס 2022")
    assert t.answer["status"] == "answered", t.answer
    res = time_of(world.client, t.message)
    assert (res["mode"], res["source"], res["prefer_recent"]) == ("explicit", "question", on), res
    assert res["requested"]["kind"] == "point" and res["requested"]["start"] == res["requested"]["end"] == "2022", res
    assert t.answer["time"]["mode"] == "explicit" and t.answer["time"]["requested"]["start"] == "2022"
    assert t.message["time_preference"]["prefer_recent"] is on


def test_without_a_time_in_the_question_the_switch_off_is_the_none_mode(world):
    cid = new_conversation(world.client)
    set_switch(world.client, cid, False)
    t = property_turn(world, cid, VALUE_Q, world.ids["hatzalon7_c"], "הצאלון 7", "value", first=True, query="שווי הנכס")
    res = time_of(world.client, t.message)
    assert (res["mode"], res["prefer_recent"]) == ("none", False) and t.answer["time"]["mode"] == "none"


# =======================================================================================================================
# (4) search-only: AE7, R19, R20
# =======================================================================================================================

def ask_only(office, question: str, prefer_recent: bool | None = None) -> dict:
    client = office.client
    cid = new_conversation(client)
    body = {"content": question} | ({} if prefer_recent is None else {"prefer_recent": prefer_recent})
    r = client.post(f"/api/chat/conversations/{cid}/messages", json=body)
    assert r.status_code == 200, r.text
    m = client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    assert m["status"] == "done" and m["answer"]["kind"] == "search_only", m
    return m["answer"]


def shown(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}/{m}/{y}"


def test_search_only_a_property_question_reads_the_appraisals_newest_valuation_first_with_dates_and_no_standard(search_only):
    """AE7 with the switch on: the property's appraisals ordered by valuation date, undated last, each with its date line."""
    answer = ask_only(search_only, "מה דמי השכירות בשומה של רחוב הצאלון 7?", True)
    found = search_groups(answer)
    assert list(found) == ["appraisal"] or list(found)[0] == "appraisal", list(found)
    appraisals = found["appraisal"]
    assert appraisals and all(s["source_kind"] == "appraisal" for s in appraisals)
    # the standards of the office sit in no appraisal group, and the pools are a property question's: no standard at all
    assert all(s["source_kind"] == "appraisal" for s in answer["sources"])
    # several appraisals share the address, so the name is ambiguous and nothing is narrowed to one unit (U3): the pooled
    # appraisal search may hold the neighbour's too; the order is by valuation date over whatever the group holds
    order = list(dict.fromkeys(s["document_id"] for s in appraisals))
    by_doc = {search_only.ids[k]: MANIFEST[k]["valuation_date"] for k in ALL_APPRAISALS}
    assert set(order) <= set(by_doc), [d for d in order if d not in by_doc]
    dated = [by_doc[d] for d in order if by_doc[d]]
    assert dated == sorted(dated, reverse=True), ("the dated appraisals are not newest first", dated)
    assert dated and dated[0] == "2025-01-20", dated
    unknown = [i for i, d in enumerate(order) if by_doc[d] is None]
    assert all(i >= len(order) - len(unknown) for i in unknown), ("an undated appraisal is not last", order)
    for iso in dated:  # each dated appraisal shows the date its own valuation states
        assert T.MSG_DATE_VALUATION.format(date=shown(iso)) in answer["markdown"], iso
    if unknown:
        assert T.MSG_DATE_UNKNOWN in answer["markdown"]


def test_search_only_the_switch_off_still_shows_the_date_lines(search_only):
    answer = ask_only(search_only, "מה דמי השכירות בשומה של רחוב הצאלון 7?", False)
    assert answer["sources"] and all(s["source_kind"] == "appraisal" for s in answer["sources"])
    by_doc = {search_only.ids[k]: MANIFEST[k]["valuation_date"] for k in ALL_APPRAISALS}
    for s in answer["sources"]:  # the dates the sources give are shown whatever the switch: the valuation date, or unknown
        iso = by_doc[s["document_id"]]
        assert s["date_line"] == (T.MSG_DATE_VALUATION.format(date=shown(iso)) if iso else T.MSG_DATE_UNKNOWN), s


def test_search_only_an_unknown_question_gets_two_headed_groups_and_claims_no_intent(search_only):
    answer = ask_only(search_only, "מה זה גישת ההשוואה?", True)
    found = search_groups(answer)
    assert list(found) == ["appraisal", "professional"], list(found)
    assert {s["source_kind"] for s in found["appraisal"]} == {"appraisal"}
    assert {s["source_kind"] for s in found["professional"]} == {"standard"}
    assert T.LABEL_GROUP_APPRAISALS in answer["markdown"] and T.LABEL_GROUP_PROFESSIONAL in answer["markdown"]
    assert T.LABEL_NOT_PROPERTY_DATA in answer["markdown"]  # R20: the standard is no source of a property's data
    for claim in ("שאלה מקצועית", "שאלה על נכס", "שאלה מעורבת", "intent"):
        assert claim not in answer["markdown"]


# =======================================================================================================================
# (5) a professional question after a property question (R16, R23)
# =======================================================================================================================

def signature(t) -> tuple:
    return (t.intent, tuple(sorted(t.cited_kinds())), tuple((c.name, c.intent, tuple(sorted(c.pools.items()))) for c in t.tools))


@pytest.mark.parametrize("on", [True, False])
def test_a_professional_question_after_a_property_question_is_professional_lists_no_property_whatever_the_switch(world, on):
    cid = new_conversation(world.client)
    set_switch(world.client, cid, on)
    prop = property_turn(world, cid, RENT_Q, world.ids["hatzalon7_c"], "הצאלון 7", "rent", first=True)
    assert prop.answer["status"] == "answered" and prop.intent == "property", prop.settled
    before = prop.context
    t = professional_turn(world, cid)
    assert_professional(t, world)  # intent professional, no lookup/candidates/documents, the standard cited, no appraisal
    assert time_of(world.client, t.message)["prefer_recent"] is on
    assert t.message["time_preference"]["prefer_recent"] is on
    assert t.answer["time"]["timeline"] == [], t.answer["time"]  # no property history is shown beside a professional answer
    assert t.context["property"] == before["property"] and t.context["params"] == before["params"]  # dormant, not dropped
    assert not t.candidates and t.documents == []


def test_the_switch_changes_nothing_of_a_professional_turn_but_its_recorded_state(world):
    seen = {}
    for on in (True, False):
        cid = new_conversation(world.client)
        set_switch(world.client, cid, on)
        property_turn(world, cid, RENT_Q, world.ids["hatzalon7_c"], "הצאלון 7", "rent", first=True)
        t = professional_turn(world, cid)
        assert_professional(t, world)
        seen[on] = (signature(t), t.context["topic"]["text"], t.context["property"])
    assert seen[True] == seen[False], seen


def test_an_ordinal_after_the_professional_list_binds_the_second_item_with_the_switch_on_and_off(world):
    from tests.integration.test_round10_sequences import APPROACHES, LIST_ANSWER

    for on in (True, False):
        cid = new_conversation(world.client)
        set_switch(world.client, cid, on)
        professional_turn(world, cid, first=True, body=LIST_ANSWER)
        t = professional_turn(world, cid, "הסבר את השנייה", query="גישת ההכנסות", reference="prior_answer_item", item=2)
        assert_professional(t, world)
        assert t.settled["item"] == 2 and t.settled["item_label"] == APPROACHES[1], (on, t.settled)
        assert t.decisions["item"].startswith("bound: 2"), (on, t.decisions)


# =======================================================================================================================
# (6) the timeline payload end to end (R13, AE1)
# =======================================================================================================================

AMOUNTS = {"y2025": ("hatzalon7_c", "2,410,000"), "y2024": ("hatzalon7_b", "2,280,000"), "y2022": ("hatzalon7_a", "1,950,000"),
           "undated": ("hatzalon7_undated", "2,150,000")}


def registering(world, order: list[str], answer, intent: str = "property") -> ScriptedAgent:
    """Searches the appraisals of the property, registers the values of ``order`` (V1, V2 …), then answers."""
    docs = [world.ids[k] for k in ("hatzalon7_a", "hatzalon7_b", "hatzalon7_c", "hatzalon7_undated")]

    def register(items):
        sids = sids_by_document(items)
        return [take(sids[world.ids[AMOUNTS[k][0]]], AMOUNTS[k][1]) for k in order]

    return ScriptedAgent([[call("search", query="השווי נקבע בסך כולל מע״מ לפי גישת ההשוואה", document_ids=docs, limit=None)],
                          register, answer], intent=intent)


def ask_timeline(world, agent: ScriptedAgent, question: str = VALUE_Q, *, prefer_recent: bool = True) -> dict:
    use(world, agent)
    cid = new_conversation(world.client)
    set_switch(world.client, cid, prefer_recent)
    m = send(world.client, cid, question)
    assert m["status"] == "done", m
    return m


def test_the_timeline_payload_lists_the_three_dated_values_newest_first_with_their_documents(compact):
    m = ask_timeline(compact, registering(compact, ["y2025", "y2024", "y2022"], cited("V1")))
    a = m["answer"]
    assert "ציר זמן" not in a["markdown"] and "[V1]" in a["markdown"]  # the timeline is beside the markdown, not in it
    t = a["time"]
    assert (t["mode"], t["prefer_recent"], t["requested"]) == ("recent", True, None)
    (group,) = t["timeline"]
    rows = group["rows"]
    assert [r["date"] for r in rows] == ["2025-01-20", "2024-06-15", "2022-03-10"]
    assert [r["value_text"] for r in rows] == ["2,410,000", "2,280,000", "1,950,000"]
    assert [r["source"]["document_id"] for r in rows] == [compact.ids[AMOUNTS[k][0]] for k in ("y2025", "y2024", "y2022")]
    assert [r["source"]["title"] for r in rows] == [MANIFEST[AMOUNTS[k][0]]["title"] for k in ("y2025", "y2024", "y2022")]
    assert all(r["date_kind"] == "valuation" and r["precision"] == "day" and r["marks"] == [] for r in rows)
    assert compact.ids["hatzalon11"] not in {r["source"]["document_id"] for r in rows}  # the neighbour is not this history
    # the report dates (written later than the valuation dates) never date a row
    reports = {MANIFEST[k]["report_date"] for k in ("hatzalon7_a", "hatzalon7_b", "hatzalon7_c")}
    assert not reports & {r["date"] for r in rows}
    assert a["time"]["text"].splitlines()[0] == "ציר זמן" and "05/02/2025" not in a["time"]["text"]


def test_an_undated_value_comes_last_marked_unknown_and_the_switch_off_orders_the_dated_rows_oldest_first(compact):
    steps = lambda: registering(compact, ["y2022", "undated", "y2025", "y2024"], cited("V3"))  # noqa: E731
    on = ask_timeline(compact, steps())["answer"]["time"]
    (group,) = on["timeline"]
    assert [r["date"] for r in group["rows"]] == ["2025-01-20", "2024-06-15", "2022-03-10", None]
    assert group["rows"][-1]["marks"] == ["unknown_date"] and group["rows"][-1]["date_kind"] is None
    assert group["rows"][-1]["source"]["document_id"] == compact.ids["hatzalon7_undated"]
    off = ask_timeline(compact, steps(), prefer_recent=False)["answer"]["time"]
    assert (off["mode"], off["prefer_recent"]) == ("none", False)
    dated = [r["date"] for g in off["timeline"] for r in g["rows"] if r["date"]]
    assert dated == sorted(dated), dated  # the order of a switch-off turn is chronological


def test_an_explicit_year_puts_that_valuation_first_and_marks_the_others_outside_the_requested_time(compact):
    answer = final("השווי ב-2022 היה 1,950,000 ₪ כולל מע״מ [V3].", focus=_focus_of(),
                   claims=[{"text": "השווי ב-2022 היה 1,950,000 ₪ כולל מע״מ", "source_ids": ["V3"], "basis": "explicit"}])
    for on in (True, False):
        m = ask_timeline(compact, registering(compact, ["y2025", "y2024", "y2022"], answer), YEAR_Q, prefer_recent=on)
        t = m["answer"]["time"]
        assert t["mode"] == "explicit" and t["requested"]["start"] == t["requested"]["end"] == "2022", (on, t)
        rows = t["timeline"][0]["rows"]
        assert rows[0]["date"] == "2022-03-10" and rows[0]["marks"] == [], (on, rows)
        assert all(r["marks"] == ["outside_requested"] for r in rows[1:]), (on, rows)
