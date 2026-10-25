"""Round 23 closure, P4: a verification round whose last judge batch runs out of time.

Traced shape: in a repair round, ``VerdictCache.reuse`` drops two UNCHANGED units because a requirement vote they gave rests on a changed unit; the
judge batches ran in order, and the last one (the unchanged units) timed out and was withheld as "not checked". Now (a) the units the turn judged
before are asked LAST and (b) a unit whose call timed out takes the verdict the turn gave the same unit (or the same datum, wording and context)
before - a supported one stays supported, a partial one keeps its mark, nothing is upgraded, no requirement vote is reused; a unit with no such
verdict is withheld. The judge is scripted; texts are invented."""

from __future__ import annotations

import re

import pytest

from app.chat import verify
from app.chat.verify import TurnRequirements, VerdictCache, verify_answer
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_verify_cache import REPAIRED, SOURCE, _answer, _req, _units, _ws

CHANGED = REPAIRED.replace("55 ₪ למ\"ר לחודש", "55 ₪ למ\"ר לחודש, לפי הסקר")  # the second line changes; the first (the value) and the third do not
VALUE = "השווי למ\"ר הוא 9,500 ₪"  # the unchanged line a requirement rests on together with the changed one
NEW = "\nדמי הניהול הם 12 ₪ למ\"ר [S1]."


class Judge:
    """A scripted judge: answers each unit by ``verdicts`` (text fragment -> verdict word or a verdict dict, default supported), scores one requirement given jointly by
    the value line and the rent line, times out for every call that holds a unit with a fragment of ``lose`` (both the call and its retry), and
    records the unit texts of each call in order."""

    def __init__(self, verdicts: dict[str, str] | None = None, lose: tuple[str, ...] = ()):
        self.verdicts, self.lose, self.calls = verdicts or {}, lose, []
        self.provider = ScriptedProvider().on(Purpose.VERIFY, self.respond, repeat=True)

    def respond(self, instructions: str, input: str):
        units = _units(input)
        self.calls.append(list(units.values()))
        if any(x in t for t in units.values() for x in self.lose):
            return CallStatus.TIMEOUT
        def verdict(t: str) -> dict:
            v = next((v for k, v in self.verdicts.items() if k in t), "supported")
            return v if isinstance(v, dict) else {"verdict": v}

        out = {"verdicts": [{"index": i, "reason": "בדיקה"} | verdict(t) for i, t in units.items()]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            both = [i for i, t in units.items() if "השווי" in t or "השכירות" in t]
            frozen = None if "<derive_requirements>" in input else re.findall(r'<requirement id="(N[\d.]+)"', input)
            out["requirements"] = [_req("השווי ודמי השכירות", "full" if len(both) == 2 else "partial" if both else "missing", both, (),
                                        frozen[0] if frozen else "")]
        return out


def _rounds(first: Judge, second: Judge, md2: str = CHANGED, ws2=None, request2: str = "?", requirements: bool = True):
    """Round one judges ``REPAIRED`` in one call; round two (a repair round over the same cache) judges ``md2`` with one unit per call - the batches
    the trace had (two units citing a very long source take a call each)."""
    ws, cache = _ws(), VerdictCache()
    turn = TurnRequirements() if requirements else None
    one = verify_answer(first.provider, _answer(REPAIRED), ws, "?", [], requirements=turn, cache=cache)
    keep, verify.JUDGE_MAX_UNITS = verify.JUDGE_MAX_UNITS, 1
    try:
        two = verify_answer(second.provider, _answer(md2), ws2 or ws, request2, [], requirements=turn, cache=cache)
    finally:
        verify.JUDGE_MAX_UNITS = keep
    return one, two, cache


def _verdicts(report) -> dict[str, str]:
    return {r.text: r.verdict for recs in report.claims.values() for r in recs}


def test_the_units_judged_before_are_asked_last_and_the_one_that_times_out_takes_its_earlier_verdict():
    first, second = Judge(), Judge(lose=(VALUE,))
    one, two, cache = _rounds(first, second)
    assert [c[0].startswith("דמי השכירות") for c in second.calls[:1]] == [True]  # the changed unit goes first
    assert second.calls[1][0].startswith(VALUE) and len(second.calls) == 2 + 1  # (the lost call is asked again once)
    assert [r.verdict for recs in two.claims.values() for r in recs if VALUE in r.text] == ["supported"]
    assert not two.removed_units() and not any(p.failure_kind == "not_checked" for p in two.problems)
    (log,) = two.reused_on_timeout
    assert log["outcome"] == "reused_on_timeout" and log["basis"] == "unit_key" and log["verdict"] == "supported" and log["claims"]


def test_the_batches_run_in_order_the_changed_unit_first_and_the_earlier_judged_one_last():
    second = Judge()
    one, two, cache = _rounds(Judge(), second)
    assert [c[0][:10] for c in second.calls] == ["דמי השכירו", "השווי למ\"ר"]  # (at the old order the unchanged value line was asked first)


def test_the_requirement_votes_of_the_unit_that_timed_out_are_not_reused():
    one, two, cache = _rounds(Judge(), Judge(lose=(VALUE,)))
    (outcome,) = two.requirement_outcomes()
    value_unit = next(u.index for u in two.units if VALUE in u.text)
    assert value_unit not in outcome["units"] and outcome["status"] != "full"  # the component stands as the other votes leave it


def test_an_earlier_partial_verdict_stays_partial_never_upgraded():
    one, two, cache = _rounds(Judge({VALUE: "partial"}), Judge(lose=(VALUE,)))
    mine = [r for recs in two.claims.values() for r in recs if VALUE in r.text]
    assert [r.verdict for r in mine] == ["partial"] and not two.removed_units()
    assert two.reused_on_timeout[0]["verdict"] == "partial"
    assert "אומת חלקית" in two.apply(_answer(CHANGED)).answer_markdown


def test_an_earlier_unsupported_verdict_is_not_taken_the_unit_is_withheld():
    from tests.unit.test_verify_cache import THREE

    ws, cache = _ws(), VerdictCache()
    verify_answer(Judge({"מושכר": "unsupported"}).provider, _answer(THREE), ws, "?", [], cache=cache)  # (no source holds it: it is removed)
    two = verify_answer(Judge(lose=("מושכר",)).provider, _answer(THREE), ws, "בקשה אחרת", [], cache=cache)
    lost = next(u.id for u in two.units if "מושכר" in u.text)
    assert lost not in {e["unit"] for e in two.reused_on_timeout} and len(two.reused_on_timeout) == 2  # (its two call-mates took theirs)
    assert any(p.failure_kind == "not_checked" and "מושכר" in p.unit.text for p in two.problems)


def test_a_unit_the_turn_never_judged_is_withheld_when_its_call_times_out():
    one, two, cache = _rounds(Judge(), Judge(lose=("דמי הניהול",)), md2=CHANGED + NEW)
    assert not two.reused_on_timeout
    assert any(p.failure_kind == "not_checked" and "דמי הניהול" in p.unit.text for p in two.problems)


def test_a_unit_whose_cited_evidence_changed_does_not_take_the_earlier_verdict():
    other = _ws(SOURCE + " הערה נוספת על הנכס.")
    one, two, cache = _rounds(Judge(), Judge(lose=(VALUE,)), ws2=other)
    assert not two.reused_on_timeout and any(p.failure_kind == "not_checked" and VALUE in p.unit.text for p in two.problems)


def test_without_a_timeout_the_units_are_judged_normally_and_nothing_is_reused():
    second = Judge({VALUE: "partial"})
    one, two, cache = _rounds(Judge(), second)
    assert not two.reused_on_timeout and _verdicts(two)[next(t for t in _verdicts(two) if VALUE in t)] == "partial"  # the new reading decides
    assert [c[0].startswith("דמי השכירות") for c in second.calls[:1]] == [True] and second.calls[1][0].startswith(VALUE)


def test_a_unit_with_the_same_datum_wording_and_context_takes_the_earlier_verdict_though_its_unit_key_is_new():
    """Another request: the unit keys all change, the datum (the text and the sources' content), wording and context do not."""
    one, two, cache = _rounds(Judge(), Judge(lose=(VALUE,)), md2=REPAIRED, request2="בקשה אחרת", requirements=False)
    (log,) = two.reused_on_timeout
    assert log["basis"] == "datum" and log["verdict"] == "supported" and not two.removed_units()


def test_the_datum_is_not_taken_by_another_wording_or_other_evidence():
    ws, cache = _ws(), VerdictCache()
    verify_answer(Judge().provider, _answer(REPAIRED), ws, "?", [], cache=cache)
    keep, verify.JUDGE_MAX_UNITS = verify.JUDGE_MAX_UNITS, 1
    try:
        reworded = REPAIRED.replace("הוא 9,500", "עומד על 9,500")
        two = verify_answer(Judge(lose=("עומד",)).provider, _answer(reworded), ws, "בקשה אחרת", [], cache=cache)
        assert not two.reused_on_timeout  # (the unchanged lines were answered by their own calls)
        assert any(p.failure_kind == "not_checked" and "עומד" in p.unit.text for p in two.problems)
        three = verify_answer(Judge(lose=(VALUE,)).provider, _answer(REPAIRED), _ws(SOURCE + " הערה נוספת על הנכס."), "בקשה אחרת", [], cache=cache)
        assert len(three.reused_on_timeout) == 0 and any(p.failure_kind == "not_checked" and VALUE in p.unit.text for p in three.problems)
    finally:
        verify.JUDGE_MAX_UNITS = keep


def test_other_failures_than_a_timeout_do_not_take_the_earlier_verdict():
    class Broken(Judge):
        def respond(self, instructions, input):
            out = super().respond(instructions, input)
            return CallStatus.ERROR if any(VALUE in t for t in _units(input).values()) else out

    one, two, cache = _rounds(Judge(), Broken())
    assert not two.reused_on_timeout and any(p.failure_kind == "not_checked" for p in two.problems)


# --- review R5: what a datum may take, and what takes it away ------------------------------------------------------------------------------

def _datum_rounds(first: Judge, second: Judge, md1: str = REPAIRED, md2: str = REPAIRED, ws1=None, ws2=None, request2: str = "בקשה אחרת"):
    """Round one judges ``md1``; round two judges ``md2`` under another request (every unit key is new, the datum of an unchanged line is not)."""
    ws, cache = ws1 or _ws(), VerdictCache()
    one = verify_answer(first.provider, _answer(md1), ws, "?", [], cache=cache)
    keep, verify.JUDGE_MAX_UNITS = verify.JUDGE_MAX_UNITS, 1
    try:
        two = verify_answer(second.provider, _answer(md2), ws2 or ws, request2, [], cache=cache)
    finally:
        verify.JUDGE_MAX_UNITS = keep
    return one, two, cache


@pytest.mark.parametrize("defect", ["units", "scope", "part"])
def test_a_partly_supported_verdict_with_a_defect_is_never_taken_for_a_unit_that_timed_out(defect):
    first = Judge({VALUE: {"verdict": "partial", "defect": defect}})
    one, two, cache = _datum_rounds(first, Judge(lose=(VALUE,)))
    assert not two.reused_on_timeout and any(p.failure_kind == "not_checked" and VALUE in p.unit.text for p in two.problems)
    one, two, cache = _rounds(first, Judge(lose=(VALUE,)))  # the unit's own key does not take it either
    assert not two.reused_on_timeout and any(p.failure_kind == "not_checked" and VALUE in p.unit.text for p in two.problems)


def test_a_partly_supported_verdict_with_no_defect_is_taken_as_partly_supported_by_its_datum():
    one, two, cache = _datum_rounds(Judge({VALUE: "partial"}), Judge(lose=(VALUE,)))
    (log,) = two.reused_on_timeout
    assert log["basis"] == "datum" and log["verdict"] == "partial"
    assert [r.verdict for recs in two.claims.values() for r in recs if VALUE in r.text] == ["partial"]


def test_the_datum_is_not_taken_under_another_ownership_fact():
    """The fact the context check could not settle (rule 34) is part of what the earlier reading judged: it must be the same one."""
    ws, cache = _ws(), VerdictCache()
    (unit,) = [u for u in verify.split_units(REPAIRED) if VALUE in u.text]
    cache.remember(verify.datum_key(unit, unit.text, ws, {}), verify.Settled("supported", verify._unit_wording(unit), "", (), verify.unit_context(unit),
                                                                              fact="נתון זה כתוב תחת נכס א"))
    for fact, taken in (("נתון זה כתוב תחת נכס ב", False), ("", False), ("נתון זה כתוב תחת נכס א", True)):
        ws.ownership_facts.clear()
        if fact:
            ws.ownership_facts[verify._fact_key(unit)] = fact
        got = verify.earlier_verdicts([unit], cache, {unit.index: "other-key"}, ws)
        assert (unit.index in got) is taken and (not taken or got[unit.index][1] == "datum"), fact


def test_the_same_claim_under_another_heading_does_not_take_the_earlier_verdict():
    one, two, cache = _datum_rounds(Judge(), Judge(lose=(VALUE,)), md1=f"## מצב מוצע\n{REPAIRED}", md2=f"## מצב מאושר\n{REPAIRED}")
    assert not two.reused_on_timeout and any(p.failure_kind == "not_checked" and VALUE in p.unit.text for p in two.problems)


def test_the_latest_reading_of_a_datum_wins_a_later_unsupported_reading_takes_the_earlier_one_away():
    from tests.unit.test_verify_cache import THREE

    ws, cache = _ws(), VerdictCache()
    verify_answer(Judge().provider, _answer(THREE), ws, "?", [], cache=cache)  # supported: remembered
    verify_answer(Judge({"מושכר": "unsupported"}).provider, _answer(THREE), ws, "בקשה ב", [], cache=cache)  # the latest reading: it does not stand
    three = verify_answer(Judge(lose=("מושכר",)).provider, _answer(THREE), ws, "בקשה ג", [], cache=cache)
    lost = next(u.id for u in three.units if "מושכר" in u.text)
    assert lost not in {e["unit"] for e in three.reused_on_timeout}
    assert any(p.failure_kind == "not_checked" and "מושכר" in p.unit.text for p in three.problems)


# --- review R6: said in the diagnostics --------------------------------------------------------------------------------------------------

def test_a_verification_no_call_answered_says_so_and_still_takes_the_earlier_verdicts():
    all_lost = Judge(lose=("השווי", "השכירות", "פנוי"))
    one, two, cache = _datum_rounds(Judge(), all_lost, md2=REPAIRED)
    assert two.judge_status == "timeout_reused" and len(two.reused_on_timeout) == 3 and not two.removed_units()
    assert one.judge_status == "ok"
    one, two, cache = _rounds(Judge(), Judge(lose=(VALUE,)))  # some call answered: ok
    assert two.judge_status == "ok"
