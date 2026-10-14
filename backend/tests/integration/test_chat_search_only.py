"""Search-only mode (an office whose cloud setting is off): the deterministic part of the turn, with no model call
(round 11 U3: R19, R20, R16; KTD11; AE7).

Hard evidence in the question (an address, a parcel, a complete unit name) narrows the search as it does in a model turn:
the intent settles to a property and the pools follow it. Without evidence no intent is claimed, and the results are two
headed groups by source kind - appraisals, then standards and professional documents, the second labelled as no source of
a property's data. An explicitly named unit of a file of several scopes the appraisal group to that unit. The tests
assert the groups, their order and their contents, not the wording. Synthetic data only; every name and figure is
invented."""

from __future__ import annotations

import pytest

from app.chat import tools as T
from app.chat.api import SEARCH_ONLY_APPRAISALS, SEARCH_ONLY_PROFESSIONALS
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import new_conversation, send
from tests.integration.test_tools_pools import appraisal
from tests.support.professional_files import professional_file
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

RENT = "דמי השכירות החודשיים בנכס ברחוב התמר 9 הם 4,200 ₪ (סינתטי)."
CONCEPT = "היוון הוא המרת הכנסה עתידית לשווי נוכחי לפי שיעור היוון (סינתטי)."


@pytest.fixture
def office(db, client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)  # the cloud setting stays off: search-only
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client = client
    return a


def professional_note(office, text_: str, title: str = "תקן בדיקה", kind: str = "standard", sha: str | None = None):
    return professional_file(office, office.default_group_id, [("paragraph", None, [], text_, 1)], title=title,
                             source_kind=kind, sha=sha)[0]


def ask(office, question: str) -> dict:
    m = send(office.client, new_conversation(office.client), question)
    assert m["status"] == "done" and m["answer"]["kind"] == "search_only", m
    return m["answer"]


def groups(answer: dict) -> dict[str, list[dict]]:
    """The answer's sources by the group the markdown lists them in (the lines under each heading)."""
    heads = {T.LABEL_GROUP_APPRAISALS: "appraisal", T.LABEL_GROUP_PROFESSIONAL: "professional"}
    by_id = {s["id"]: s for s in answer["sources"]}
    out: dict[str, list[dict]] = {}
    current = None
    for line in answer["markdown"].splitlines():
        head = next((kind for label, kind in heads.items() if line.startswith(f"**{label}**")), None)
        if head is not None:
            current = out.setdefault(head, [])
        elif current is not None and line.startswith("- "):
            current.append(by_id[line[line.rindex("[") + 1:line.rindex("]")]])
    return out


def test_an_address_in_the_question_reads_the_appraisal_first_and_no_standard_sits_in_its_group(office):
    """AE7."""
    prop, _ = appraisal(office, RENT, title="שומה רחוב התמר 9 (סינתטי)", opening="שומה לנכס ברחוב התמר 9")
    professional_note(office, "דמי שכירות ראויים נקבעים לפי עסקאות השוואה בנכסים דומים (סינתטי).", "תקן שכירות")
    answer = ask(office, "מה דמי השכירות בשומה של רחוב התמר 9?")
    assert "לא נוסחה תשובה מנותחת" in answer["markdown"] and answer["sources"]
    found = groups(answer)
    assert found["appraisal"] and {s["document_id"] for s in found["appraisal"]} == {prop}
    assert answer["sources"][0]["document_id"] == prop
    assert all(s["source_kind"] == "appraisal" for s in found["appraisal"])
    # the pools of a property question are the server's: no standard is read as the property's datum
    assert all(s["source_kind"] == "appraisal" for s in answer["sources"])


def test_a_question_with_no_evidence_gets_two_headed_groups_the_second_labelled_and_claims_no_intent(office):
    appraisal(office, CONCEPT, title="שומה גנרית (סינתטי)", opening="שומה כללית")
    std = professional_note(office, CONCEPT, "תקן היוון")
    answer = ask(office, "מה זה היוון?")
    found = groups(answer)
    assert set(found) == {"appraisal", "professional"}
    assert list(found) == ["appraisal", "professional"]  # the appraisals first, then the professional documents
    assert {s["source_kind"] for s in found["appraisal"]} == {"appraisal"}
    assert {s["document_id"] for s in found["professional"]} == {std}
    assert {s["source_kind"] for s in found["professional"]} == {"standard"}
    assert T.LABEL_NOT_PROPERTY_DATA in answer["markdown"]
    # no sentence names the question's intent
    for claim in ("שאלה מקצועית", "שאלה על נכס", "שאלה מעורבת", "intent"):
        assert claim not in answer["markdown"]


def test_each_group_is_capped_at_its_own_number_of_passages(office):
    for n in range(SEARCH_ONLY_APPRAISALS + 3):
        appraisal(office, f"{CONCEPT} שומה מספר {n}.", title=f"שומה כללית {n} (סינתטי)", opening=f"שומה כללית {n}")
    for n in range(SEARCH_ONLY_PROFESSIONALS + 3):
        professional_note(office, f"{CONCEPT} תקן מספר {n}.", f"תקן היוון {n}", sha=f"{n:064d}")
    found = groups(ask(office, "מה זה היוון?"))
    assert 0 < len(found["appraisal"]) <= SEARCH_ONLY_APPRAISALS
    assert 0 < len(found["professional"]) <= SEARCH_ONLY_PROFESSIONALS


def test_an_office_with_no_standards_gets_one_group_and_no_empty_professional_heading(office):
    appraisal(office, CONCEPT, title="שומה גנרית (סינתטי)", opening="שומה כללית")
    answer = ask(office, "מה זה היוון?")
    assert list(groups(answer)) == ["appraisal"]
    assert T.LABEL_GROUP_PROFESSIONAL not in answer["markdown"] and T.LABEL_NOT_PROPERTY_DATA not in answer["markdown"]


def test_an_office_with_only_standards_gets_the_professional_group_alone(office):
    professional_note(office, CONCEPT, "תקן היוון")
    answer = ask(office, "מה זה היוון?")
    assert list(groups(answer)) == ["professional"]
    assert T.LABEL_GROUP_APPRAISALS not in answer["markdown"]


def test_an_explicitly_named_unit_of_a_file_of_several_scopes_the_appraisal_group_to_that_unit(office):
    units = [{"title": "מתחם הגפן א", "aliases": ["הגפן א"], "blocks": ["מתחם הגפן א (סינתטי)", "דמי השכירות במתחם הגפן א הם 3,100 ₪."]},
             {"title": "מתחם הברוש ב", "aliases": ["הברוש ב"], "blocks": ["מתחם הברוש ב (סינתטי)", "דמי השכירות במתחם הברוש ב הם 5,700 ₪."]}]
    unit_file(office, office.default_group_id, "קובץ מתחמים (סינתטי)", units)
    professional_note(office, "דמי שכירות ראויים נקבעים לפי עסקאות השוואה (סינתטי).", "תקן שכירות")
    answer = ask(office, "מה דמי השכירות במתחם הברוש ב?")
    appraisals = groups(answer)["appraisal"]
    assert appraisals and all(s["unit"]["title"] == "מתחם הברוש ב" for s in appraisals)
    assert "5,700" in " ".join(s["text"] for s in appraisals) and "3,100" not in " ".join(s["text"] for s in appraisals)


# --- the recorded switch orders the appraisal group (round 11 U8: KTD6, KTD11; R3, R4, R16) ---------------------------------

def ask_with(office, question: str, prefer_recent: bool | None) -> dict:
    """A search-only answer to ``question`` sent in a new conversation with the switch as given."""
    client = office.client
    cid = new_conversation(client)
    body = {"content": question} | ({} if prefer_recent is None else {"prefer_recent": prefer_recent})
    r = client.post(f"/api/chat/conversations/{cid}/messages", json=body)
    assert r.status_code == 200, r.text
    m = client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    assert m["status"] == "done" and m["answer"]["kind"] == "search_only", m
    return m["answer"]


def dated_documents(office) -> dict[str, str]:
    """Four appraisals: the strongest match is the oldest, the weakest the newest, one states no date."""
    from tests.integration.test_tools_recency import BODY, WEAK, dated

    return {
        "2022": dated(office, "so22", "20/05/2022", body=BODY + " " + BODY, amount="2,100,000"),
        "2024": dated(office, "so24", "15/03/2024", body=BODY, amount="2,300,000"),
        "unknown": dated(office, "sounk", None, body=BODY, amount="2,000,000"),
        "2025": dated(office, "so25", "15/04/2025", body=WEAK, amount="2,450,000"),
    }


SO_QUESTION = "שווי הנכס ברחוב הרקפות"


def appraisal_order(answer: dict) -> list[str]:
    return list(dict.fromkeys(s["document_id"] for s in groups(answer)["appraisal"]))


def test_with_the_switch_on_the_appraisal_group_is_newest_first_with_date_lines(office):
    docs = dated_documents(office)
    answer = ask_with(office, SO_QUESTION, True)
    order = appraisal_order(answer)
    assert order[:2] == [docs["2025"], docs["2024"]] and order.index(docs["2022"]) == 2 and order[-1] == docs["unknown"]
    assert T.MSG_DATE_VALUATION.format(date="15/04/2025") in answer["markdown"]
    assert answer["markdown"].count(T.MSG_DATE_UNKNOWN) == 1


def test_with_the_switch_off_the_appraisal_group_keeps_relevance_order_and_the_date_lines(office):
    from app.db import tenant_tx
    from app.platform.search import SearchScope, search_passages

    docs = dated_documents(office)
    answer = ask_with(office, SO_QUESTION, False)
    with tenant_tx(office.ctx()) as conn:
        plain = search_passages(conn, SO_QUESTION, SEARCH_ONLY_APPRAISALS, scope=SearchScope(source_kinds=("appraisal",)))
    relevance = list(dict.fromkeys(str(h["document_id"]) for h in plain))
    assert appraisal_order(answer) == relevance and relevance[0] == docs["2022"]
    assert T.MSG_DATE_VALUATION.format(date="20/05/2022") in answer["markdown"]
    assert answer["markdown"].count(T.MSG_DATE_UNKNOWN) == 1


def test_a_year_in_the_question_orders_the_group_whatever_the_switch(office):
    docs = dated_documents(office)
    for switch in (True, False):
        order = appraisal_order(ask_with(office, f"{SO_QUESTION} ב-2025", switch))
        assert order[0] == docs["2025"], switch


def test_the_professional_group_keeps_its_order_and_carries_no_date_whatever_the_switch(office):
    dated_documents(office)
    for n in range(2):
        professional_note(office, f"{SO_QUESTION} נקבע לפי עסקאות השוואה (סינתטי) {n}.", f"תקן שווי {n}", sha=f"{n:064d}")
    on, off = ask_with(office, SO_QUESTION, True), ask_with(office, SO_QUESTION, False)
    found_on, found_off = groups(on)["professional"], groups(off)["professional"]
    assert [s["document_id"] for s in found_on] == [s["document_id"] for s in found_off] and found_on
    professional_lines = [ln for ln in on["markdown"].splitlines() if ln.startswith("- ")
                          and any(s["id"] in ln for s in found_on)]
    assert professional_lines and not any(T.MSG_DATE_UNKNOWN in ln or "למועד הקובע" in ln for ln in professional_lines)
