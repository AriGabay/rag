"""Round 23 closure, diagnostics: how a turn's confirming (second) readings ended, in the stored message's diagnostics.

The engine adds to the repair-gate entry of each verification round (``timing.repair_gate``, served by ``GET /api/chat/messages/{id}/diagnostics``)
the judge status, the confirm summary with the {claim id, outcome} of each claim it covered, and the units that took an earlier verdict on a timeout
(``VerifyReport.confirm_record``). Ids, counts and labels only. The record is read by no decision: the same turn with the record switched off stores the
same answer, spans and rounds. The judge is scripted (the first reading fails the numbers, the confirming one is told by its policy); synthetic
documents and the synthetic office only."""

from __future__ import annotations

import json
import re

import pytest

from app.chat import verify
from app.config import get_settings
from app.providers.llm import Purpose
from tests.integration.test_chat_limits import Reader, ask, diagnostics, final, pages, stored_answer
from tests.integration.test_chat_limits import (
    office as office,  # noqa: F401 - the synthetic office with its document
)
from tests.support.scripted_provider import _Entry

pytestmark = pytest.mark.db

# ruff: noqa: F811 - the tests take the imported ``office`` fixture by name

LOT, OTHER = "שטח הבניין 812 מ\"ר", "שטח החצר 812 מ\"ר"
TWO = f"{LOT} [S1].\n\n{OTHER} [S1]."


def claims(*texts: str) -> list[dict]:
    return [{"text": t, "source_ids": ["S1"], "basis": "explicit"} for t in texts]


def judged(agent: Reader, first: str, second) -> Reader:
    """The first reading fails every unit (``first``: its verdict word); the confirming reading answers by ``second(unit text)``: a verdict word,
    or None to leave the unit's claim out of its reading. The rest (requirements) is the scripted agent's own."""
    base = agent._entries[0].response

    def respond(instructions: str, input: str) -> dict:
        out = base(instructions, input)
        confirming = verify.JUDGE_CONFIRM_POLICY in instructions
        verdicts = []
        for m in re.finditer(r'<unit index="(\d+)"[^>]*>(.*?)</unit>', input, re.S):
            i, text = int(m.group(1)), m.group(2)
            if not confirming:
                verdicts.append({"index": i, "verdict": first, "reason": "בדיקה", "supported_by": []})
                continue
            word = second(text)
            span = next(s for s in (LOT, OTHER) if s in text)
            claim = [{"span": span, "verdict": word}] if word else []
            verdicts.append({"index": i, "verdict": word or "partial", "reason": "בדיקה", "supported_by": [], "claims": claim})
        out["verdicts"] = verdicts
        return out

    agent._entries.insert(0, _Entry(Purpose.VERIFY, respond, None, True))
    return agent


def turn(client, office, monkeypatch, second, md=TWO, texts=(LOT, OTHER), first="unsupported"):
    agent = judged(Reader([[pages(office.doc, 2)]], final(md, claims=claims(*texts))), first, second)
    return ask(client, office, monkeypatch, agent)


def gate(client, m) -> dict:
    d = diagnostics(client, m)
    return d["timing"]["repair_gate"][0]


def test_a_flipped_and_a_not_confirmed_claim_are_in_the_rounds_diagnostics_by_id(client, office, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 4)
    m = turn(client, office, monkeypatch, lambda t: "supported" if LOT in t else None)
    g = gate(client, m)
    outcomes = g["confirm"]["outcomes"]
    assert sorted(e["outcome"] for e in outcomes) == ["flipped", "not_confirmed"]
    assert g["confirm"]["calls"] >= 1 and g["confirm"]["flips"] == 1 and g["confirm"]["skipped"] == {"not_confirmed": 1}
    assert g["judge_status"] == "ok" and g["reused_on_timeout"] == []
    assert all(set(e) == {"claim", "outcome"} for e in outcomes)
    # ids and labels only: no word of the answer or of the document is in the record
    dumped = json.dumps({k: g[k] for k in ("confirm", "judge_status", "reused_on_timeout")}, ensure_ascii=False)
    assert "בניין" not in dumped and "חצר" not in dumped and "812" not in dumped


def test_a_claim_past_the_cap_is_recorded_as_cap(client, office, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 1)
    m = turn(client, office, monkeypatch, lambda t: "supported")
    g = gate(client, m)
    assert sorted(e["outcome"] for e in g["confirm"]["outcomes"]) == ["cap", "flipped"] and g["confirm"]["skipped"] == {"cap": 1}


def test_a_turn_that_asked_for_no_confirming_reading_records_an_empty_confirm(client, office, monkeypatch):
    agent = Reader([[pages(office.doc, 2)]], final(f"{LOT} [S1].", claims=claims(LOT)))
    m = ask(client, office, monkeypatch, agent)
    g = gate(client, m)
    assert g["confirm"]["outcomes"] == [] and g["confirm"]["calls"] == 0 and g["judge_status"] == "ok" and g["reused_on_timeout"] == []


def test_the_record_changes_nothing_of_the_stored_turn(client, office, monkeypatch):
    """The same scripted turn with the record switched off: the same answer, spans, status, rounds and gate decisions."""
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 4)

    def run():
        m = turn(client, office, monkeypatch, lambda t: "supported" if LOT in t else None)
        d = diagnostics(client, m)
        g = d["timing"]["repair_gate"]
        decisions = [{k: v for k, v in x.items() if k not in ("confirm", "judge_status", "reused_on_timeout", "seconds_to_deadline",
                                                              "seconds_to_gate", "answer_seconds", "gate_window_seconds")} for x in g]
        rounds = [[{k: v for k, v in p.items() if k != "claim_id"} for p in r] for r in d["rounds"]]  # (the ids hash this run's document ids)
        return stored_answer(office, m), rounds, decisions

    with_record = run()
    monkeypatch.setattr(verify.VerifyReport, "confirm_record", lambda self: (_ for _ in ()).throw(RuntimeError("off")))
    without_record = run()
    assert with_record == without_record
    # and it is the answer the turn gave before the record existed (measured on that code): the flipped claim marked, the other removed
    assert with_record[0] == {"markdown": "שטח הבניין 812 מ\"ר [S1]. *(אומת חלקית)*\n\n> הוסרה טענה שהמקורות שנבדקו אינם מציינים.",
                              "status": "partial", "limits_hit": [], "spans": [("partial_mark", 24, 39), ("removal_note", 39, 83)]}



def test_a_timeout_reuse_of_the_round_reaches_the_stored_diagnostics_unchanged(client, office, monkeypatch):
    """The reuse itself is made and recorded by ``verify_answer`` (``tests/unit/test_verify_confirm_record.py``, with the judge timing out); here the
    engine's part: whatever the report records for a round is stored in that round's gate entry and served as it is, with the status."""
    record = {"judge_status": "timeout_reused", "reused_on_timeout": [{"unit": "u1", "claims": ["kab"], "outcome": "reused_on_timeout",
                                                                      "basis": "datum", "verdict": "partial"}],
              "confirm": {"calls": 0, "claims": 0, "flips": 0, "removed": 0, "reused": 0, "skipped": {}, "outcomes": []}}
    monkeypatch.setattr(verify.VerifyReport, "confirm_record", lambda self: record)
    agent = Reader([[pages(office.doc, 2)]], final(f"{LOT} [S1].", claims=claims(LOT)))
    g = gate(client, ask(client, office, monkeypatch, agent))
    assert g["judge_status"] == "timeout_reused" and g["reused_on_timeout"] == record["reused_on_timeout"] and g["confirm"] == record["confirm"]
