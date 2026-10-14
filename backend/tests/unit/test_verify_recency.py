"""A claim that calls a value the latest must cite the latest-dated value of its group (round 11 U9: R13; KTD8, KTD9).

``verify.deterministic`` flags a unit whose words call a value "העדכני" or "האחרון" while the turn registered a later-dated
value comparable to a cited one (same property, family, unit, period, VAT, area basis, scenario and stance). The problem is
a repairable partial one with the check name ``recency_claim``: the unit is kept, marked, and sent to the repair round —
never removed, since the number it states is supported. The property of a value is a stand-in here (the units lookup is
proven in ``tests/integration/test_chat_timeline.py``). Synthetic data only."""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

from app.chat import calc, timeline, verify
from app.chat.engine import FinalAnswer
from app.chat.timeframe import TimeMode
from app.chat.tools import Workspace
from app.chat.verify import split_units, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider

ANCHOR = date(2026, 10, 14)
PROV = {"kind": "source", "unit": "source", "period": "source", "vat": "source", "area_basis": "source",
        "stance": "source", "scenario": "not_stated"}


def _as_of(iso: str, precision: str = "day", forecast: bool = False) -> dict:
    return {"date": iso, "precision": precision, "end": None, "provenance": "unit_valuation", "forecast": forecast,
            "locator": "block 1"}


def _ws(*values: tuple[str, str, str], same_property: bool = True, **fields) -> Workspace:
    """A workspace registering a value of ``(id, amount, ISO date)`` for each, one document each, all of one property."""
    ws = Workspace(ctx=None)
    ws.time = TimeMode("recent", "switch", ANCHOR, None, True)
    docs = {}
    for vid, amount, iso in values:
        doc = docs[vid] = uuid.uuid4()
        quote = f"השווי נקבע בסך {amount} ₪"
        v = calc.Value(vid, Decimal(amount.replace(",", "")), amount, "S1", doc, uuid.uuid4(), "r1", f"שומה {iso[:4]}",
                       "סעיף 3", "שווי הנכס", fields.get("kind", "value"), "ILS", "none", fields.get("vat", "included"),
                       "", "הנכס", "appraiser_determination", PROV | fields.get("provenance", {}), {"quote": quote},
                       quote, stance="adopted")
        v.as_of = _as_of(iso, forecast=fields.get("forecast", {}).get(vid, False))
        ws.values[vid] = v
    timeline.set_identity(ws, lambda d: timeline.Identity("group:הצאלון 7" if same_property else f"group:{d.document_id}"))
    return ws


V = (("V1", "2,100,000", "2022-03-10"), ("V2", "2,280,000", "2024-06-15"), ("V3", "2,410,000", "2025-01-20"))


def _check(text: str, ws: Workspace) -> list:
    return [p for p in verify.deterministic(split_units(text), ws, "?") if p.check == "recency_claim"]


def test_a_claim_calling_an_older_value_the_latest_is_a_repairable_partial_problem_naming_the_later_date():
    (p,) = _check("השווי העדכני הוא 2,100,000 ₪ [V1].", _ws(*V))
    assert p.check == "recency_claim" and p.severity == "partial" and p.kind == "claim"
    assert p.repairable and not p.removes_unit and not p.fails_claim
    assert "20/01/2025" in p.reason and "V3" in p.reason  # the later date and the value that has it
    assert p.checked_ids == ["V1"] and p.failure_kind in verify.FAILURE_KINDS


def test_the_same_claim_citing_the_latest_value_is_no_problem():
    assert verify.deterministic(split_units("השווי העדכני הוא 2,410,000 ₪ [V3]."), _ws(*V), "?") == []


def test_citing_the_latest_beside_an_older_one_is_enough():
    assert _check("השווי העדכני הוא 2,410,000 ₪ [V3], לעומת 2,100,000 ₪ [V1].", _ws(*V)) == []


def test_other_words_for_the_latest_count():
    for words in ("השווי האחרון הוא", "השווי המעודכן הוא", "השווי העדכנית הוא", "הערכת השווי האחרונה היא"):
        assert len(_check(f"{words} 2,100,000 ₪ [V1].", _ws(*V))) == 1, words


def test_a_claim_that_does_not_call_the_value_the_latest_is_not_checked():
    assert _check("השווי בשומה משנת 2022 היה 2,100,000 ₪ [V1].", _ws(*V)) == []


def test_a_time_phrase_is_not_a_claim_of_the_latest_value():
    for text in ("בשנה האחרונה נקבע שווי של 2,100,000 ₪ [V1].", "בשלוש השנים האחרונות נקבע שווי 2,100,000 ₪ [V1].",
                 "בחודש האחרון של 2022 נקבע שווי 2,100,000 ₪ [V1]."):
        assert _check(text, _ws(*V)) == [], text


def test_a_later_value_of_another_group_is_not_a_later_value():
    ws = _ws(*V[:2])
    ws.values["V3"] = _ws(V[2]).values["V3"]
    ws.values["V3"].vat = "excluded"  # another VAT basis: not comparable
    assert _check("השווי העדכני הוא 2,280,000 ₪ [V2].", ws) == []
    other = _ws(*V[:2], same_property=False)  # every value its own property
    assert _check("השווי העדכני הוא 2,100,000 ₪ [V1].", other) == []


def test_a_later_forecast_or_undated_value_does_not_make_the_cited_one_stale():
    ws = _ws(*V[:2], ("V3", "3,000,000", "2027-01-01"), forecast={"V3": True})
    assert _check("השווי העדכני הוא 2,280,000 ₪ [V2].", ws) == []
    ws = _ws(*V[:2])
    ws.values["V3"] = _ws(("V3", "2,500,000", "2025-01-01")).values["V3"]
    ws.values["V3"].as_of = None
    assert _check("השווי העדכני הוא 2,280,000 ₪ [V2].", ws) == []


def test_the_check_applies_in_every_time_mode():
    for mode in ("none", "recent", "explicit"):
        ws = _ws(*V)
        ws.time = TimeMode(mode, "question" if mode == "explicit" else "switch", ANCHOR, None, mode != "none")
        assert len(_check("השווי העדכני הוא 2,100,000 ₪ [V1].", ws)) == 1, mode


def test_a_unit_another_check_already_failed_is_not_also_flagged_for_recency():
    problems = verify.deterministic(split_units("השווי העדכני הוא 9,999,999 ₪ [V1]."), _ws(*V), "?")
    assert [p.check for p in problems] == ["unstated_number"]


def test_a_unit_that_cites_no_value_is_not_checked():
    assert _check("השווי העדכני נקבע לפי גישת ההשוואה.", _ws(*V)) == []


# --- inside the verification: the unit is judged, kept and repaired, never removed -----------------------------------------

def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _supporting_judge() -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: {"verdicts": [
        {"index": int(i), "verdict": "supported", "reason": "בדיקה", "supported_by": []}
        for i in re.findall(r'<unit index="(\d+)"', input)]}, repeat=True)
    return p


def test_a_recency_claim_unit_is_still_judged_and_kept_and_the_report_asks_for_a_repair():
    ws = _ws(*V)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה 2022", section="סיכום",
                 location="סעיף 3", kind="context", text="השווי נקבע בסך 2,100,000 ₪")
    report = verify_answer(_supporting_judge(), _answer("השווי העדכני הוא 2,100,000 ₪ [V1]."), ws, "?", [])
    assert report.judged and report.verdicts == {0: "supported"}  # the unit reached the judge
    (p,) = [x for x in report.problems if x.check == "recency_claim"]
    assert not report.ok and p.repairable
    assert report.removed_units() == set() and report.failed_units() == set()
    assert report.correctness() == "partial"
    assert "recency_claim" in {x["check"] for x in (q.as_dict() for q in report.problems)}


def test_the_corrected_claim_passes_verification_clean():
    ws = _ws(*V)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה 2025", section="סיכום",
                 location="סעיף 3", kind="context", text="השווי נקבע בסך 2,410,000 ₪")
    report = verify_answer(_supporting_judge(), _answer("השווי העדכני הוא 2,410,000 ₪ [V3]."), ws, "?", [])
    assert report.ok and report.problems == [] and report.correctness() == "verified"
