"""Round 23 closure, diagnostics: how the confirming (second) readings and the timeout reuse of a verification ended, as the turn's diagnostics keep them.

``VerifyReport.confirm_record()`` is the record the engine adds to the repair-gate entry of each verification round: the judge status, the confirm
summary (calls, claims asked, flips, removed, reused, skipped by reason) with the {claim id, outcome} list, and the units that took an earlier verdict
on a timeout. Ids, counts and labels only - never a word of the answer or of a document. Reading it changes nothing: the answer the report applies
is byte-identical, and the same on the report that was never asked. The judge is scripted; texts are invented."""

from __future__ import annotations

import json

import pytest

from app.chat import verify
from app.chat.verify import VerdictCache, verify_answer
from app.config import get_settings
from tests.unit.test_verify_confirm_policy import (
    FENCE,
    FIRST,
    FLOORS,
    GATE,
    SENTENCE,
    SOURCE,
    _claim,
    _reading,
    _run,
)
from tests.unit.test_verify_reread import _answer, _same_doc_ws, _two_calls
from tests.unit.test_verify_timeout_reuse import CHANGED, VALUE, Judge, _rounds


@pytest.fixture(autouse=True)
def _confirm_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 3)


# the answers of the existing confirm tests, as the code before this record produced them (measured on that code)
FLIPPED_MD = "הנכס בן 3 קומות, עלות הגידור היא 52,000 ₪ ועלות השער 7,000 ₪ [S1]. *(אומת חלקית)*"
NOT_CONFIRMED_MD = "הנכס בן 3 קומות, עלות הגידור היא 52,000 ₪ ו«[1: חלק שלא אומת הושמט]» [S1]."


def _no_text(record: dict, *texts: str) -> None:
    dumped = json.dumps(record, ensure_ascii=False)
    assert not any(t in dumped for t in texts), dumped


def test_a_flipped_claim_is_recorded_with_its_id_and_the_answer_is_what_it_was():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), _claim(GATE)))
    assert out == FLIPPED_MD  # byte-identical to the answer before the record existed
    rec = report.confirm_record()
    (entry,) = report.confirm
    assert rec["judge_status"] == "ok"
    assert rec["confirm"] == {"calls": 1, "claims": 1, "flips": 1, "removed": 0, "reused": 0, "skipped": {},
                              "outcomes": [{"claim": entry["claim"], "outcome": "flipped"}]}
    assert rec["reused_on_timeout"] == []
    _no_text(rec, FLOORS, FENCE, GATE, "7,000")
    assert report.apply(_answer(f"{SENTENCE} [S1].")).answer_markdown == FLIPPED_MD  # reading the record again changes nothing


def test_a_claim_no_second_reading_claim_confirmed_is_recorded_not_confirmed():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE)))
    assert out == NOT_CONFIRMED_MD
    rec = report.confirm_record()
    assert [e["outcome"] for e in rec["confirm"]["outcomes"]] == ["not_confirmed"]
    assert rec["confirm"]["calls"] == 1 and rec["confirm"]["claims"] == 0 and rec["confirm"]["skipped"] == {"not_confirmed": 1}
    _no_text(rec, FLOORS, FENCE, GATE)


def test_a_claim_both_readings_failed_and_a_failed_call_are_recorded():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), _claim(GATE, "unsupported")))
    rec = report.confirm_record()
    assert [e["outcome"] for e in rec["confirm"]["outcomes"]] == ["both_failed"] and rec["confirm"]["removed"] == 1
    from app.providers.llm import CallStatus
    from tests.unit.test_verify_reread import _answer as answer

    def respond(instructions: str, input: str):
        return CallStatus.TIMEOUT if verify.JUDGE_CONFIRM_POLICY in instructions else {
            "verdicts": [{"index": 0, "reason": "בדיקה"} | FIRST]}

    from tests.support.scripted_provider import ScriptedProvider
    failed = verify_answer(ScriptedProvider().on(verify.Purpose.VERIFY, respond, repeat=True), answer(f"{SENTENCE} [S1]."),
                           _same_doc_ws(SOURCE), "?", [])
    rec = failed.confirm_record()
    assert [e["outcome"] for e in rec["confirm"]["outcomes"]] == ["check_failed"] and rec["confirm"]["skipped"] == {"check_failed": 1}


def test_claims_past_the_cap_are_recorded_as_cap_with_the_confirmed_ones():
    texts = [f"עלות הרכיב {n} היא {n},100 ₪" for n in (1, 2, 3, 4)]
    source = " ".join(f"הערך הכללי של {n} הוא {n},100 ₪." for n in (1, 2, 3, 4))
    md = "\n\n".join(f"{t} [S1]." for t in texts)
    get_settings().chat_confirm_removals = 4

    def first(t):
        partial = {"verdict": "partial", "claims": [{"span": t, "verdict": "partial", "failure": "absent_from_source"}]}
        return "unsupported" if "הרכיב 4" in t else partial

    report = verify_answer(_two_calls(first, lambda t: {"verdict": "supported", "claims": [{"span": t, "verdict": "supported"}]}), _answer(md),
                           _same_doc_ws(source), "?", [], cache=VerdictCache())
    rec = report.confirm_record()
    assert sorted(e["outcome"] for e in rec["confirm"]["outcomes"]) == ["cap", "flipped", "flipped", "flipped"]
    assert rec["confirm"]["skipped"] == {"cap": 1} and rec["confirm"]["flips"] == 3 and rec["confirm"]["claims"] == 3
    assert {e["claim"] for e in rec["confirm"]["outcomes"]} == {e["claim"] for e in report.confirm}
    _no_text(rec, *texts)


def test_a_verification_with_no_confirming_reading_records_an_empty_confirm():
    report = verify_answer(_two_calls(lambda t: None, lambda t: None), _answer(f"{FLOORS} [S1]."), _same_doc_ws(SOURCE), "?", [])
    assert report.confirm_record() == {"judge_status": "ok", "reused_on_timeout": [],
                                       "confirm": {"calls": 0, "claims": 0, "flips": 0, "removed": 0, "reused": 0, "skipped": {}, "outcomes": []}}


def test_a_unit_that_took_the_earlier_verdict_on_a_timeout_is_recorded_with_its_basis_and_verdict():
    one, two, cache = _rounds(Judge(), Judge(lose=(VALUE,)))
    rec = two.confirm_record()
    (log,) = rec["reused_on_timeout"]
    assert log == {"unit": log["unit"], "claims": log["claims"], "outcome": "reused_on_timeout", "basis": "unit_key", "verdict": "supported"}
    assert log["claims"] and all(isinstance(c, str) for c in log["claims"]) and rec["judge_status"] == "ok"
    _no_text(rec, VALUE, "השכירות")
    assert one.confirm_record()["reused_on_timeout"] == []
    assert two.apply(_answer(CHANGED)).answer_markdown == two.apply(_answer(CHANGED)).answer_markdown


def test_a_verification_no_call_answered_records_the_status_timeout_reused():
    from tests.unit.test_verify_cache import REPAIRED, _ws

    ws, cache = _ws(), VerdictCache()
    verify_answer(Judge().provider, _answer(REPAIRED), ws, "?", [], cache=cache)
    two = verify_answer(Judge(lose=("",)).provider, _answer(REPAIRED), ws, "בקשה אחרת", [], cache=cache)
    rec = two.confirm_record()
    assert rec["judge_status"] == "timeout_reused" and rec["reused_on_timeout"]


def test_the_record_is_small_however_many_claims_were_asked(monkeypatch):
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), _claim(GATE)))
    report.confirm = [{"claim": f"k{n}", "outcome": "cap"} for n in range(500)]
    rec = report.confirm_record()
    assert len(rec["confirm"]["outcomes"]) == verify.CONFIRM_RECORD_KEPT and rec["confirm"]["omitted"] == 500 - verify.CONFIRM_RECORD_KEPT
    assert rec["confirm"]["skipped"] == {"cap": 500}  # the counts stay whole
