"""The timeline block and the recency claim through the whole turn (round 11 U9: R4, R9, R10, R11, R13; KTD8, KTD9; AE1).

Three synthetic appraisals of one invented property (valuation dates 2022, 2024, 2025, each written in its own document)
and a neighbouring property are stored as units with identifiers (``tests/support/unit_files``). The model is scripted: it
searches, registers the values it chooses and answers. What is proven is what the server adds: ``answer.time`` in the
message the API returns — the mode, the comparable group filled from every registered value (cited or not), the rows in
the turn's order with their sources and marks, a plain-text copy — while ``answer.markdown`` is the text the model wrote
and the check verified; and a false "latest" claim is sent to the repair round instead of being kept. Every name, address
and amount is invented."""

from __future__ import annotations

import json
import re

import pytest

from app.chat import contexts
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_intent import _focus
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

ADDRESS = "רחוב הצאלון 7"
QUESTION = "מה שווי הנכס ברחוב הצאלון 7?"
ADOPTED = {"kind": "value", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "", "subject": "הנכס",
           "role": "other", "stance": "adopted"}
# (key, valuation date as the report writes it, amount)
APPRAISALS = (("y2025", "20/01/2025", "2,410,000"), ("y2024", "15/06/2024", "2,280,000"),
              ("y2022", "10/03/2022", "1,950,000"))


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


def appraisal(office, key: str, valuation: str, amount: str, address: str = ADDRESS) -> str:
    doc, _ = unit_file(office, office.default_group_id, f"שומה סינתטית {address} {key}", [{
        "title": f"שומה סינתטית {key}", "identifiers": [f"כתובת הנכס: {address}"],
        "blocks": ["מסמך בדיקה סינתטי — נתונים בדויים", "עיר: עיר-סינתטית", f"כתובת הנכס: {address}", f"המועד הקובע: {valuation}",
                   f"השווי נקבע בסך {amount} ₪ כולל מע״מ, לפי גישת ההשוואה."]}], sha=(key * 20)[:64].ljust(64, "0"))
    return doc


@pytest.fixture
def setup(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.docs = {key: appraisal(a, key, valuation, amount) for key, valuation, amount in APPRAISALS}
    a.neighbour = appraisal(a, "n2024", "01/09/2024", "2,730,000", "רחוב הצאלון 11")
    login(client, "admin-a@example.test")
    return a


def sids_by_document(items: list) -> dict[str, str]:
    """document id -> the S# of the search hit that holds its valuation sentence."""
    out = {}
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            for m in re.finditer(r"<source ([^>]*)>(.*?)</source>", it["output"], re.S):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                if "השווי נקבע בסך" in m.group(2) and attrs.get("id") and attrs.get("document_id"):
                    out.setdefault(attrs["document_id"], attrs["id"])
    return out


def take(sid: str, amount: str) -> dict:
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None,
               "quote": f"השווי נקבע בסך {amount} ₪", "number": amount}
    return call("take_value", source=sid, locator=locator, meaning=ADOPTED, label="שווי הנכס")


def agent(setup, order: list[str], answer, **kw) -> ScriptedAgent:
    """Searches the three appraisals, registers their values in ``order`` (V1, V2, V3 …), then answers."""
    amounts = {key: amount for key, _, amount in APPRAISALS}

    def register(items):
        sids = sids_by_document(items)
        return [take(sids[setup.docs[k]], amounts[k]) for k in order]

    docs = list(setup.docs.values())
    return ScriptedAgent([[call("search", query="השווי נקבע בסך כולל מע״מ לפי גישת ההשוואה", document_ids=docs, limit=None)], register,
                          answer], intent="property", **kw)


def cited(vid: str, text: str = "השווי העדכני") -> dict:
    return final(f"{text} הוא 2,410,000 ₪ כולל מע״מ [{vid}].", focus=_focus_of(),
                 claims=[{"text": f"{text} הוא 2,410,000 ₪ כולל מע״מ", "source_ids": [vid], "basis": "explicit"}])


def _focus_of() -> dict:
    return _focus("") | {"metric_kind": "value", "period": "none", "vat": "included", "subject": "הצאלון 7",
                         "document_ids": []}


def ask(client, setup, monkeypatch, steps: ScriptedAgent, question: str = QUESTION, *, prefer_recent: bool = True) -> dict:
    cloud(monkeypatch, setup, steps)
    cid = new_conversation(client)
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": prefer_recent}).status_code == 200
    m = send(client, cid, question)
    assert m["status"] == "done", m
    return m


def rows(message: dict) -> list[dict]:
    (group,) = message["answer"]["time"]["timeline"]
    return group["rows"]


# --- AE1 ------------------------------------------------------------------------------------------------------------------

def test_ae1_the_answer_cites_only_the_newest_and_the_timeline_lists_all_three_newest_first(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))
    a = m["answer"]
    assert a["markdown"] == "השווי העדכני הוא 2,410,000 ₪ כולל מע״מ [V1]."  # the verified text, no timeline in it
    assert "ציר זמן" not in a["markdown"]
    t = a["time"]
    assert (t["mode"], t["prefer_recent"], t["requested"]) == ("recent", True, None)
    got = rows(m)
    assert [r["date"] for r in got] == ["2025-01-20", "2024-06-15", "2022-03-10"]
    assert [r["sid"] for r in got] == ["V1", "V2", "V3"]
    assert [r["value_text"] for r in got] == ["2,410,000", "2,280,000", "1,950,000"]
    assert all(r["date_kind"] == "valuation" and r["precision"] == "day" and r["marks"] == [] for r in got)
    assert [r["source"]["document_id"] for r in got] == [setup.docs[k] for k in ("y2025", "y2024", "y2022")]
    assert all(r["source"]["title"] and r["source"]["location"] for r in got)
    # the neighbouring property is not this one's history
    assert setup.neighbour not in {r["source"]["document_id"] for r in got}
    # the timeline is the group of the one cited value: one group, labelled
    assert t["timeline"][0]["label"]


def test_the_uncited_rows_open_like_citations_their_values_and_sources_are_in_the_payload(client, setup, monkeypatch):
    a = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))["answer"]
    assert {v["id"] for v in a["values"]} == {"V1", "V2", "V3"}
    assert {s["id"] for s in a["sources"]} >= {v["source_id"] for v in a["values"]}
    assert {d["document_id"] for d in a["documents"]} >= {setup.docs[k] for k in setup.docs}


def test_the_copy_of_the_timeline_is_plain_text_outside_the_markdown(client, setup, monkeypatch):
    a = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))["answer"]
    text = a["time"]["text"]
    assert text.splitlines()[0] == "ציר זמן"
    for needle in ("20/01/2025", "15/06/2024", "10/03/2022", "2,410,000", "1,950,000", "מועד קובע", "שומה סינתטית"):
        assert needle in text, needle
    assert len([ln for ln in text.splitlines() if ln.startswith("- ")]) == 3
    assert "20/01/2025" not in a["markdown"]


def test_no_sources_conflict_line_for_values_of_different_dates(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))
    assert "סותרים" not in m["answer"]["markdown"]
    assert not any(g.get("reason") == "sources_conflict" for g in m["answer"].get("gaps") or [])


def test_the_order_does_not_depend_on_the_order_the_values_were_registered_in(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2022", "y2024", "y2025"], cited("V3")))
    assert [r["date"] for r in rows(m)] == ["2025-01-20", "2024-06-15", "2022-03-10"]
    assert [r["sid"] for r in rows(m)] == ["V3", "V2", "V1"]


# --- the other modes ------------------------------------------------------------------------------------------------------

def test_with_the_switch_off_the_timeline_is_chronological_and_still_shown(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1", "השווי")), prefer_recent=False)
    t = m["answer"]["time"]
    assert (t["mode"], t["prefer_recent"]) == ("none", False)
    assert [r["date"] for r in rows(m)] == ["2022-03-10", "2024-06-15", "2025-01-20"]


def test_an_explicit_year_puts_that_year_first_and_marks_the_later_ones_as_outside_the_requested_time(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], final(
        "השווי ב-2022 היה 1,950,000 ₪ כולל מע״מ [V3].", focus=_focus_of(),
        claims=[{"text": "השווי ב-2022 היה 1,950,000 ₪ כולל מע״מ", "source_ids": ["V3"], "basis": "explicit"}])),
        "מה היה השווי ב-2022?")
    t = m["answer"]["time"]
    assert t["mode"] == "explicit" and t["requested"]["start"] == t["requested"]["end"] == "2022"
    got = rows(m)
    assert [r["sid"] for r in got] == ["V3", "V2", "V1"]
    assert got[0]["marks"] == [] and all(r["marks"] == ["outside_requested"] for r in got[1:])


def test_one_cited_value_of_a_single_document_gives_no_timeline(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025"], cited("V1")))
    t = m["answer"]["time"]
    # no timeline text; the copy text is the coverage note alone (round 12 U8)
    assert t["timeline"] == [] and t["text"] == t["coverage"]["note"]


# --- the recency claim ----------------------------------------------------------------------------------------------------

def test_a_false_latest_claim_is_repaired_not_kept(client, setup, monkeypatch):
    wrong = final("השווי העדכני הוא 1,950,000 ₪ כולל מע״מ [V3].", focus=_focus_of(),
                  claims=[{"text": "השווי העדכני הוא 1,950,000 ₪ כולל מע״מ", "source_ids": ["V3"], "basis": "explicit"}])
    steps = agent(setup, ["y2025", "y2024", "y2022"], wrong)
    both = final("השווי העדכני הוא 2,410,000 ₪ כולל מע״מ [V1].\nבשומה קודמת השווי היה 1,950,000 ₪ כולל מע״מ [V3].",
                 focus=_focus_of(),
                 claims=[{"text": "השווי העדכני הוא 2,410,000 ₪ כולל מע״מ", "source_ids": ["V1"], "basis": "explicit"},
                         {"text": "בשומה קודמת השווי היה 1,950,000 ₪ כולל מע״מ", "source_ids": ["V3"], "basis": "explicit"}])
    steps.steps.append(both)  # the repair round's answer: the latest first, the older one kept as an earlier value
    m = ask(client, setup, monkeypatch, steps)
    a = m["answer"]
    assert "העדכני הוא 2,410,000 ₪ כולל מע״מ [V1]" in a["markdown"] and "1,950,000" in a["markdown"]
    assert "אומת חלקית" not in a["markdown"]
    repair = next(i for i in steps.seen[-1] if isinstance(i, dict) and "recency" not in str(i.get("type")) and
                  "מאוחר יותר" in str(i.get("content", "")))
    assert "V1" not in repair["content"].split("מאוחר")[0] and "V3" in repair["content"]
    diagnostics = m_diagnostics(client, m)
    assert "recency_claim" in json.dumps(diagnostics, ensure_ascii=False)
    assert a["verification"]["removed"] == 0


def m_diagnostics(client, message: dict) -> dict:
    return client.get(f"/api/chat/messages/{message['id']}/diagnostics").json()


def test_a_false_latest_claim_that_stays_is_kept_marked_and_the_timeline_still_tells_the_order(client, setup, monkeypatch):
    wrong = final("השווי העדכני הוא 1,950,000 ₪ כולל מע״מ [V3].", focus=_focus_of(),
                  claims=[{"text": "השווי העדכני הוא 1,950,000 ₪ כולל מע״מ", "source_ids": ["V3"], "basis": "explicit"}])
    steps = agent(setup, ["y2025", "y2024", "y2022"], wrong)
    steps.steps += [wrong, wrong]  # the model insists through the repair rounds
    m = ask(client, setup, monkeypatch, steps)
    a = m["answer"]
    assert "1,950,000" in a["markdown"] and a["verification"]["removed"] == 0  # supported content is never removed for it
    assert [r["sid"] for r in rows(m)] == ["V1", "V2", "V3"]  # the block asserts the order whatever the prose said


# --- the coverage note (round 12 U8: R12, R13, R14; KTD7, KTD8; AE4) ---------------------------------------------------------

QUALIFIED = "הנתון המאוחר ביותר במקורות שנבדקו"
UNDATED_TITLE = "שומה סינתטית ללא מועד קובע"


@pytest.fixture
def partial(setup):
    """The three dated appraisals and a fourth of the same property that states no valuation date: coverage of the property
    can never be complete while an undated unit remains (KTD7)."""
    doc, _ = unit_file(setup, setup.default_group_id, UNDATED_TITLE, [{
        "title": UNDATED_TITLE, "identifiers": [f"כתובת הנכס: {ADDRESS}"],
        "blocks": ["מסמך בדיקה סינתטי — נתונים בדויים", "עיר: עיר-סינתטית", f"כתובת הנכס: {ADDRESS}",
                   "השווי נקבע בסך 2,000,000 ₪ כולל מע״מ, לפי גישת ההשוואה."]}], sha="undated".ljust(64, "0"))
    setup.docs = setup.docs | {"undated": doc}
    return setup


def qualified(vid: str) -> dict:
    return final(f"{QUALIFIED} הוא 2,410,000 ₪ כולל מע״מ [{vid}].", focus=_focus_of(),
                 claims=[{"text": f"{QUALIFIED} הוא 2,410,000 ₪ כולל מע״מ", "source_ids": [vid], "basis": "explicit"}])


def test_complete_coverage_records_a_note_that_never_claims_the_latest_in_the_repository(client, setup, monkeypatch):
    a = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))["answer"]
    cov = a["time"]["coverage"]
    assert cov["v"] == 1 and cov["complete"] is True and cov["blocking"] == []
    assert [u["status"] for u in cov["units"]] == ["hit", "hit", "hit"] and not any(u["undated"] for u in cov["units"])
    assert cov["note"] == "נבדקו כל השומות של הנכס הזמינות לך (3)." and "במאגר" not in cov["note"]
    assert a["time"]["text"].endswith(cov["note"]), "the copy text ends with the note, after the timeline"


def test_ae4_an_undated_unit_makes_the_coverage_partial_and_the_note_names_it_in_the_payload_and_the_copy(client, partial, monkeypatch):
    a = ask(client, partial, monkeypatch, agent(partial, ["y2025", "y2024", "y2022"], qualified("V1")))["answer"]
    cov = a["time"]["coverage"]
    assert cov["v"] == 1 and cov["complete"] is False and "undated" in cov["blocking"]
    assert cov["note"].splitlines()[0] == QUALIFIED + "."
    assert UNDATED_TITLE in cov["note"]
    assert any(u["undated"] and u["blocks"] for u in cov["units"])
    assert a["time"]["timeline"] and a["time"]["text"].endswith(cov["note"])
    assert a["markdown"] == f"{QUALIFIED} הוא 2,410,000 ₪ כולל מע״מ [V1]."  # the note is not in the model's text


def test_a_passage_only_answer_under_partial_coverage_still_carries_the_note(client, partial, monkeypatch):
    def passage_answer(items):
        sid = sids_by_document(items)[partial.docs["y2025"]]
        return final(f"{QUALIFIED} הוא 2,410,000 ₪ כולל מע״מ [{sid}].", focus=_focus_of(),
                     claims=[{"text": f"{QUALIFIED} הוא 2,410,000 ₪ כולל מע״מ", "source_ids": [sid], "basis": "explicit"}])

    docs = list(partial.docs.values())
    steps = ScriptedAgent([[call("search", query="השווי נקבע בסך כולל מע״מ לפי גישת ההשוואה", document_ids=docs, limit=None)],
                           passage_answer], intent="property")
    a = ask(client, partial, monkeypatch, steps)["answer"]
    t = a["time"]
    assert t["timeline"] == [], "an answer that cites a passage only has no timeline rows"
    assert t["coverage"]["complete"] is False and UNDATED_TITLE in t["coverage"]["note"]
    assert t["text"] == t["coverage"]["note"], "the copy text is the note even with no timeline"


def test_a_false_latest_claim_under_partial_coverage_is_repaired_to_the_qualified_phrase_not_removed(client, partial, monkeypatch):
    steps = agent(partial, ["y2025", "y2024", "y2022"], cited("V1"))  # "השווי העדכני" with no qualification
    steps.steps.append(qualified("V1"))  # the repair round's answer
    m = ask(client, partial, monkeypatch, steps)
    a = m["answer"]
    assert QUALIFIED in a["markdown"] and a["verification"]["removed"] == 0
    assert "recency_claim" in json.dumps(m_diagnostics(client, m), ensure_ascii=False)


def test_the_same_latest_claim_under_complete_coverage_is_no_problem(client, setup, monkeypatch):
    m = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1")))
    assert "recency_claim" not in json.dumps(m_diagnostics(client, m), ensure_ascii=False)


def test_with_the_switch_off_and_no_requested_time_there_is_no_coverage_key(client, setup, monkeypatch):
    a = ask(client, setup, monkeypatch, agent(setup, ["y2025", "y2024", "y2022"], cited("V1", "השווי")), prefer_recent=False)["answer"]
    assert "coverage" not in a["time"], "R14: no inventory, no record, no wording"
    assert "במקורות שנבדקו" not in (a["time"]["text"] or "")


def test_the_model_reads_the_coverage_and_the_phrase_rule_after_a_partial_time_search(client, partial, monkeypatch):
    steps = agent(partial, ["y2025", "y2024", "y2022"], qualified("V1"))
    ask(client, partial, monkeypatch, steps)
    outputs = [i["output"] for i in steps.seen[-1] if isinstance(i, dict) and i.get("type") == "function_call_output"]
    search = outputs[0]
    assert UNDATED_TITLE in search and QUALIFIED in search, "the search result carries the coverage summary and the rule"
    # the property line (U12b) names the gathered units before the results; the coverage summary comes after them
    summary = "כיסוי השומות בחיפוש הזה"
    assert search.index("<source") < search.index(summary) < search.rindex(UNDATED_TITLE), "after the results, never before them"
    assert all(UNDATED_TITLE not in o for o in outputs[1:]), "only a time-ranked search says it"


def test_the_model_reads_no_coverage_in_off_mode(client, setup, monkeypatch):
    steps = agent(setup, ["y2025", "y2024", "y2022"], cited("V1", "השווי"))
    ask(client, setup, monkeypatch, steps, prefer_recent=False)
    outputs = [i["output"] for i in steps.seen[-1] if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert not any("כיסוי השומות" in o for o in outputs)
