"""Cheaper repair rounds end to end (U11, KTD10, R31): a repair round re-judges only the units that changed, a
problem the server resolves itself starts no round, and a simple first-turn question takes its agent steps and one
verify call. Usage stays one record per model call.

The model and the judge are scripted: these tests prove which units reach the judge and how many calls a turn makes,
not the model's judgement. Synthetic documents only (those of ``test_chat``)."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import ANSWER, TEXTS, cloud, new_conversation, send
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)  # the turn runs inside the request
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = add_chunks(a, a.default_group_id, TEXTS, "1" * 64)
    a.doc, a.ver = str(doc), ver
    return a

SEARCH = [call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)]  # S1: the summary passage


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _judge(bad: str, seen: list[str]):
    """Every unit supported except one holding ``bad``; the judge's inputs go to ``seen``."""
    def respond(input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": i, "verdict": "unsupported" if bad in t else "supported", "reason": "בדיקה"}
                             for i, t in _units(input).items()]}
    return respond


def test_a_simple_first_turn_question_takes_its_agent_steps_and_one_verify_call(client, office, monkeypatch):
    agent = ScriptedAgent([[call("search", query="דמי שכירות ראויים", document_ids=None, limit=None)], ANSWER])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות הראויים?")
    assert m["status"] == "done", m
    # no resolve call on a first turn, no repair round: the per-call records stay one per call; the request analysis
    # (round 7 KTD1) runs beside the first step and is recorded when it is collected, before the second
    assert [u["purpose"] for u in m["usage"]] == ["agent", "request", "agent", "verify"]
    assert m["answer"]["verification"]["correctness"] == "verified"


VALUE = "השווי למ\"ר בנוי ברוטו למסחר הוא 9,500 ₪, ללא מע\"מ [S1]."
RENT = "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש [S1]."


def test_a_repair_round_that_changes_one_sentence_re_judges_one_unit(client, office, monkeypatch):
    first = final(f"{VALUE}\n{RENT}\nהשווי נקבע לפי גישת ההכנסות [S1].")
    repaired = final(f"{VALUE}\n{RENT}\nהנתונים מופיעים בסיכום השומה [S1].")
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, first, repaired], judge=_judge("גישת ההכנסות", seen))
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי ודמי השכירות, ולפי איזו גישה?")
    assert m["status"] == "done", m
    assert len(agent.seen) == 3 and len(seen) == 2
    assert len(_units(seen[0])) == 3
    again = list(_units(seen[1]).values())
    assert len(again) == 1 and "בסיכום השומה" in again[0]  # the scripted judge sees one unit in its second call
    a = m["answer"]
    assert "בסיכום השומה" in a["markdown"] and "גישת ההכנסות" not in a["markdown"]
    assert a["verification"]["removed"] == 0
    assert [u["purpose"] for u in m["usage"]] == ["agent", "request", "agent", "verify", "agent", "verify"]


def test_a_repair_that_changes_a_tables_column_header_re_judges_the_rows_under_it(client, office, monkeypatch):
    rows = ("|---|---|\n| שווי למ\"ר בנוי ברוטו למסחר | 9,500 ₪, ללא מע\"מ [S1] |\n"
            "| דמי שכירות ראויים למ\"ר לחודש | 55 ₪ [S1] |")
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final("| נתון | סכום שנתי |\n" + rows), final("| נתון | סכום |\n" + rows)],
                          judge=_judge("שנתי", seen))
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי ודמי השכירות?")
    assert m["status"] == "done", m
    assert len(seen) == 2
    rows_judged = [t for t in _units(seen[0]).values() if "סכום" not in t]
    again = list(_units(seen[1]).values())
    assert rows_judged and all(t in again for t in rows_judged)  # the rows under the changed header, judged again
    assert any("סכום" in t and "שנתי" not in t for t in again)


def test_an_answer_whose_only_problem_is_a_missing_qualifier_gets_no_repair_round(client, office, monkeypatch):
    bare = final("השווי למ\"ר הוא 9,500 ₪, ללא מע\"מ [S1].")  # the source says "למ"ר בנוי ברוטו"
    agent = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)], bare, bare])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה השווי למ\"ר?")
    a = m["answer"]
    assert len(agent.seen) == 2 and [u["purpose"] for u in m["usage"]] == ["agent", "request", "agent", "verify"]
    assert "9,500 ₪ (מ״ר בנוי ברוטו, כפי שנכתב במקור)" in a["markdown"]
    assert a["verification"]["annotated"] == 1 and a["verification"]["removed"] == 0


# --- round 8 U6: the saved text is the checked text ------------------------------------------------------------------

MIXED = "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש, שאושרו בוועדה המחוזית [S1]."


def _mixed_judge(seen: list[str]):
    """The rent is in the passage and the approval is not: the first check splits the sentence; the sentence as saved
    (the cut in it) is supported."""
    def respond(input: str) -> dict:
        seen.append(input)
        out = []
        for i, t in _units(input).items():
            if "שאושרו" in t and "חלק שלא אומת" not in t:
                out.append({"index": i, "verdict": "partial", "reason": "האישור אינו במקור", "defect": "part", "claims": [
                    {"span": "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש", "verdict": "supported"},
                    {"span": "שאושרו בוועדה המחוזית", "verdict": "unsupported", "failure": "absent_from_source"}]})
            else:
                out.append({"index": i, "verdict": "supported", "reason": "בדיקה"})
        return {"verdicts": out}
    return respond


def test_a_sentence_with_a_span_cut_costs_one_final_check_call_and_is_saved_as_it_was_checked(client, office,
                                                                                              monkeypatch):
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final(MIXED)], judge=_mixed_judge(seen))
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות הראויים?")
    assert m["status"] == "done", m
    # the cut is the server's: no repair round; the changed sentence is judged once more, as it is saved
    assert [u["purpose"] for u in m["usage"]] == ["agent", "request", "agent", "verify", "verify_final"]
    assert len(seen) == 2 and "<server>«[1: חלק שלא אומת הושמט]»</server>" in seen[1]
    a = m["answer"]
    assert a["markdown"] == "דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש, «[1: חלק שלא אומת הושמט]» [S1]."
    assert m["content"] == a["markdown"] and a["status"] == "partial"


def test_a_follow_up_reads_the_earlier_answer_without_the_words_the_server_wrote_in_it(client, office, monkeypatch):
    first = final("דמי השכירות הראויים הם 55 ₪ למ\"ר לחודש [S1]. הנכס מושכר לטווח ארוך [S1].")
    agent = ScriptedAgent([SEARCH, first, first, first], judge=_judge("מושכר", []))
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    m = send(client, cid, "מה דמי השכירות והאם הנכס מושכר?")
    md = m["answer"]["markdown"]
    note = next(line for line in md.split("\n") if "הוסרו מהתשובה" in line)
    assert "מושכר" not in md and note in md
    # what U7 saves with the message: the registry of the server's spans (offsets and a kind, never text)
    start = md.index("\n\n> " + note.removeprefix("> "))
    registry = [{"start": start, "end": len(md), "kind": "removal_note"}]
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE messages SET answer = answer || CAST(:s AS jsonb) WHERE id = :m"),
                     {"s": json.dumps({"server_spans": registry}), "m": m["id"]})
    second = ScriptedAgent([final("אין עוד מידע במקורות.", "not_found")])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    send(client, cid, "ומה עוד?")
    context = second.seen[0][0]["content"]
    assert "דמי השכירות הראויים הם 55" in context and "הוסרו מהתשובה" not in context
