"""Round 23 closure, P1-P3: what the confirming (second) reading may keep, which claims it protects, and what a settled decision may be reused for.

P1: a claim the first reading failed is kept by the second reading only when the second reading returns a claim for that exact span with
verdict ``supported`` and valid shown evidence. A ``partial`` claim with failing spans exactly inside it keeps only the supported part (the edit
ladder cuts the failing span), unless the cut would separate a kept number from its condition, negation, period, unit, property or component
words. The unit/sentence verdict alone, a missing claim, invalid output, a timeout or a provider error are no confirmation: the first verdict
stands, the outcome is recorded, nothing is shown as verified. P2: protection follows the content that would vanish (a claim that cites
registered values (V#) whose own passage holds its other numbers). P3: a settled decision is reused only for the same text, evidence digest
and context; a kept claim keeps its condition words. The judge is scripted; texts are invented."""

from __future__ import annotations

import re

import pytest

from app.chat import verify
from app.chat.verify import VerdictCache, verify_answer
from app.config import get_settings
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_verify_reread import _answer, _purposes, _same_doc_ws, _two_calls, _units


@pytest.fixture(autouse=True)
def _confirm_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 3)


# a sentence of three claims: two stand in the source, the last ("the gate") does not - the first reading fails it, the second is asked
FLOORS, FENCE, GATE = "הנכס בן 3 קומות", "עלות הגידור היא 52,000 ₪", "עלות השער 7,000 ₪"
SENTENCE = f"{FLOORS}, {FENCE} ו{GATE}"
SOURCE = "הנכס בן 3 קומות. עלות הגידור היא 52,000 ₪. עלות השילוט 7,000 ₪."


def _claim(span: str, verdict: str = "supported", **kw) -> dict:
    return {"span": span, "verdict": verdict} | kw


def _reading(*claims: dict, verdict: str = "partial", **kw) -> dict:
    return {"verdict": verdict, "defect": "part", "claims": list(claims)} | kw


FIRST = _reading(_claim(FLOORS), _claim(FENCE), _claim(GATE, "unsupported"))


def _run(second, md: str = f"{SENTENCE} [S1].", first=FIRST, source: str = SOURCE, cache: VerdictCache | None = None, **kw):
    usage: list = []
    ws = _same_doc_ws(source)
    report = verify_answer(_two_calls(lambda t: first, lambda t: second, **kw), _answer(md), ws, "?", usage, cache=cache)
    return report, report.apply(_answer(md)).answer_markdown, usage


def _outcomes(report) -> list[str]:
    return [e["outcome"] for e in report.confirm]


# --- P1: the unit or the sentence verdict alone, or a missing claim, is no confirmation ------------------------------------------------------

@pytest.mark.parametrize("unit_verdict", ["partial", "supported"])
def test_a_second_reading_with_no_claim_for_the_failed_span_does_not_keep_it_whatever_its_unit_verdict(unit_verdict):
    """The second reading answers the unit ``partial`` / ``supported`` and returns claims for the other spans only: nobody confirmed the gate."""
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), verdict=unit_verdict))
    assert "7,000" not in out and "52,000" in out and "3 קומות" in out, out
    assert _outcomes(report) == ["not_confirmed"] and report.confirm_summary()["flips"] == 0
    assert report.confirm_summary()["skipped"] == {"not_confirmed": 1}
    assert _purposes(usage) == ["verify", "verify_confirm"]
    assert all(r.verdict != "partial" for r in report.claims[0] if GATE in r.text)  # never shown as partly verified


@pytest.mark.parametrize("unit_verdict", ["partial", "not_factual", "supported"])
def test_a_unit_verdict_with_no_claim_list_does_not_keep_a_claim_the_first_reading_failed(unit_verdict):
    """The first reading failed the whole unit; the second answers the unit alone, ``partial`` or ``not_factual``: no claim confirms anything."""
    report, out, usage = _run({"verdict": unit_verdict}, md=f"{GATE} [S1].", first={"verdict": "unsupported"})
    assert report.removed_units() == {0} and "7,000" not in out
    assert _outcomes(report) == ["not_confirmed"] and not report.confirm_summary()["flips"]


def test_nothing_is_settled_when_the_second_reading_decided_nothing():
    """Not confirmed is not "removed": no decision is recorded, no verdict is cached from the second reading, the first verdict stands."""
    cache = VerdictCache()
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE)), cache=cache)
    assert _outcomes(report) == ["not_confirmed"]
    assert not any(e.decision == "removed" for es in cache.settled.values() for e in es) and not cache.settled
    assert all(v.verdict != "supported" for v in cache.verdicts.values())  # (the cached verdict is the first reading's)


def test_an_explicit_failure_of_the_claim_is_settled_as_removed():
    cache = VerdictCache()
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), _claim(GATE, "unsupported")), cache=cache)
    assert "7,000" not in out and _outcomes(report) == ["both_failed"]
    assert [e.decision for es in cache.settled.values() for e in es] == ["removed"]


# --- P1: a technical failure decides nothing -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [CallStatus.TIMEOUT, CallStatus.INVALID, CallStatus.ERROR])
def test_a_technical_failure_of_the_second_reading_keeps_the_first_verdict_and_settles_nothing(status):
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [{"index": n, "reason": "בדיקה"} | FIRST for n in _units(input)]})
    p.on(Purpose.VERIFY, status, repeat=True)
    cache = VerdictCache()
    report = verify_answer(p, _answer(f"{SENTENCE} [S1]."), _same_doc_ws(SOURCE), "?", [], cache=cache)
    assert "7,000" not in report.apply(_answer(f"{SENTENCE} [S1].")).answer_markdown
    assert _outcomes(report) == ["check_failed"] and not cache.settled


# --- P1: the invalid evidence of a supported claim is no confirmation ----------------------------------------------------------------------

def test_a_supported_claim_whose_named_support_was_never_shown_is_no_confirmation():
    cache = VerdictCache()
    second = _reading(_claim(FLOORS), _claim(FENCE), _claim(GATE), verdict="supported", supported_by=["S9"])
    report, out, usage = _run(second, cache=cache)
    assert "7,000" not in out and _outcomes(report) == ["not_confirmed"] and not cache.settled


# --- P1: a supported claim for the exact span keeps it, a partial one keeps only the supported part ----------------------------------------

def test_an_explicit_supported_claim_for_the_exact_span_keeps_it_marked():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(FENCE), _claim(GATE)))
    assert "7,000" in out and "אומת חלקית" in out and _outcomes(report) == ["flipped"]


BOTH = f"{FENCE} ו{GATE}"
WIDE_FIRST = _reading(_claim(FLOORS), _claim(BOTH, "unsupported"))


def test_a_partial_claim_with_a_failing_span_inside_keeps_only_the_supported_part():
    """Today a second reading that returns the claim ``partial`` counts as not failed and the whole claim, the failing figure too, is kept."""
    second = _reading(_claim(FLOORS), _claim(BOTH, "partial"), _claim(FENCE), _claim(f"ו{GATE}", "unsupported"))
    report, out, usage = _run(second, first=WIDE_FIRST)
    assert "7,000" not in out and "52,000" in out and "3 קומות" in out, out
    assert "אומת חלקית" in out and _outcomes(report) == ["partly_kept"]
    assert report.removed_units() == set()
    assert [r.action for r in report.claims[0] if "7,000" in r.text] == ["cut"]


@pytest.mark.parametrize(("fence", "gate", "floors"), [("מחיר המעקה הוא 1,250 ₪", "מחיר השלט 480 ₪", "בבניין 12 דירות"),
                                                    ("דמי הניהול הם 9,300 ₪", "דמי החניה 2,000 ₪", "בנכס 2 חנויות")])
@pytest.mark.parametrize("failing_first", [True, False])
def test_the_supported_part_stays_whatever_the_words_the_numbers_and_the_order(fence, gate, floors, failing_first):
    wide = f"{gate} ו{fence}" if failing_first else f"{fence} ו{gate}"
    failing = gate if failing_first else f"ו{gate}"
    md = f"{floors}, {wide} [S1]."
    first = _reading(_claim(floors), _claim(wide, "unsupported"))
    second = _reading(_claim(floors), _claim(wide, "partial"), _claim(fence), _claim(failing, "unsupported"))
    report, out, usage = _run(second, md=md, first=first, source=f"{floors}. {fence}. הסכום האחר הוא {gate.split()[-2]} ₪.")
    gate_number = gate.split()[-2]
    assert gate_number not in out and fence.split()[-2] in out and floors.split()[1] in out, out
    assert _outcomes(report) == ["partly_kept"]


def test_a_partial_claim_with_no_exact_failing_span_is_no_confirmation():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(BOTH, "partial")), first=WIDE_FIRST)
    assert "7,000" not in out and _outcomes(report) == ["not_confirmed"]


def test_a_partial_claim_whose_failing_span_is_not_inside_it_is_no_confirmation():
    second = _reading(_claim(FLOORS, "unsupported"), _claim(BOTH, "partial"))
    report, out, usage = _run(second, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "7,000" not in out


QUALIFIED_SOURCE = "הערך הכללי הוא 52,000 ₪. נחתם ביום 12.03.2019. נקבע בשנת 2020. התנאי: המבנה פנוי. ללא חניה."


@pytest.mark.parametrize("qualifier", ["בתנאי שהמבנה פנוי", "ללא חניה", "בהסכם מיום 12.03.2019", "בשנת 2020"])
def test_a_cut_that_would_leave_a_number_without_its_condition_negation_or_period_is_not_made(qualifier):
    """The failing span is the kept number's own condition/period: the first verdict stands (the claim goes), nothing is shown as verified."""
    claim = f"{FENCE} {qualifier}"
    md = f"{FLOORS}, {claim} [S1]."
    first = _reading(_claim(FLOORS), _claim(claim, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(claim, "partial"), _claim(qualifier, "unsupported"))
    report, out, usage = _run(second, md=md, first=first, source=f"{FLOORS}. {QUALIFIED_SOURCE}")
    assert "52,000" not in out and "3 קומות" in out, out
    assert _outcomes(report) == ["not_confirmed"] and "אומת חלקית" not in out


def test_a_failing_span_that_is_only_words_of_the_kept_number_is_not_cut_from_it():
    claim = f"{FENCE} בקומת הקרקע"
    first = _reading(_claim(FLOORS), _claim(claim, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(claim, "partial"), _claim("בקומת הקרקע", "unsupported"))
    report, out, usage = _run(second, md=f"{FLOORS}, {claim} [S1].", first=first)
    assert "52,000" not in out and _outcomes(report) == ["not_confirmed"]


def test_the_part_that_stays_is_decided_once_and_a_later_round_asks_again_nothing():
    cache = VerdictCache()
    second = _reading(_claim(FLOORS), _claim(BOTH, "partial"), _claim(FENCE), _claim(f"ו{GATE}", "unsupported"))
    md = f"{FLOORS}, {BOTH} [S1]."
    report, out, usage = _run(second, md=md, first=WIDE_FIRST, cache=cache)
    n = len(usage)
    ws = _same_doc_ws(SOURCE)
    again = verify_answer(_two_calls(lambda t: WIDE_FIRST, lambda t: second), _answer(md), ws, "?", usage, cache=cache)
    assert len(usage) == n  # the same datum: neither reading is asked again
    out2 = again.apply(_answer(md)).answer_markdown
    assert "7,000" not in out2 and "52,000" in out2 and "אומת חלקית" in out2, out2


# --- P2: protection follows the content that would vanish ------------------------------------------------------------------------------------
#
# A sentence gives an amount and a count per agreement, with the date of each agreement, and cites only the registered values of the amounts
# (V#). The dates are in the same stored passage as the amounts, on another line, so the first reading - shown the value quotes only - fails
# each claim for "numbers not in the cited sources"; the number re-reads (next page, continuation, preceding) found nothing. The confirming reading
# must still run, and is shown the passage the values were registered from.

import uuid  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.chat import calc  # noqa: E402
from app.chat.tools import Workspace  # noqa: E402

PROV = {"unit": "source", "vat": "source"}


def _passage(*agreements: tuple[str, str, str, str]) -> str:
    """The stored passage: for each agreement a line with its date and a line with its amount and count."""
    return "\n".join(f"הסכם {name} נחתם ביום {date}.\nבהסכם {name} שולם סכום של {amount} ₪ עבור {count} דירות." for name, date, amount, count in agreements)


def _values(ws: Workspace, passage: str, *amounts: str) -> list[str]:
    """V1.. registered from the lines of ``passage`` that write each amount (the quote is the amount line only)."""
    doc, ver = uuid.uuid4(), uuid.uuid4()
    ws.add_source(document_id=doc, version_id=ver, title="דוח בדיקה", section="עסקאות", location="סעיף \"עסקאות\"", kind="context", text=passage)
    ids = []
    for n, amount in enumerate(amounts, 1):
        line = next(x for x in passage.splitlines() if amount in x)
        ws.values[f"V{n}"] = calc.Value(f"V{n}", Decimal(amount.replace(",", "")), amount, "S1", doc, ver, None, "דוח בדיקה", "סעיף \"עסקאות\"",
                                        "סכום", "value", "ILS", "none", "included", "", "הנכס", "appraiser_determination", PROV,
                                        {"quote": line}, line)
        ids.append(f"V{n}")
    return ids


DEALS = (("א", "12.03.2019", "2,300,000", "4"), ("ב", "05.09.2021", "3,100,000", "5"))
DEALS_B = (("ג", "03.11.2018", "870,000", "2"), ("ד", "19.05.2020", "1,450,000", "3"), ("ה", "27.01.2022", "990,000", "6"))


def _deal_claims(deals=DEALS, reverse: bool = False, dates: tuple[str, ...] | None = None) -> list[str]:
    """One claim per agreement: its date, amount and count (``dates``: the dates the sentence gives instead, to put one with another agreement's amount)."""
    rows = [(dates[n] if dates else d, a, c) for n, (_, d, a, c) in enumerate(deals)]
    rows = rows[::-1] if reverse else rows
    return [f"בהסכם מיום {d} שולמו {a} ₪ עבור {c} דירות" for d, a, c in rows]


def _deal_md(claims: list[str], count: int) -> str:
    return ", ו".join(claims) + " " + "".join(f"[V{n}]" for n in range(1, count + 1)) + "."


def _reread_log(ws: Workspace, md: str, outcome: str = "not_found") -> None:
    """The unit's re-reads (``ws.reread_log`` under the unit's key): by default the number re-reads of the next page, the continuation and the
    preceding part, which found nothing."""
    units = verify.split_units(md)
    verify.assign_ids(units, ws.claim_salt)
    for u in units:
        for kind in verify.UNSTATED_EXPANSIONS:
            verify._log_reread(ws, "unit:" + u.id, source="S1", scope=kind, outcome=outcome)


def _deal_run(first, second, deals=DEALS, claims: list[str] | None = None, outcome: str = "not_found", **kw):
    ws = Workspace(ctx=None)
    _values(ws, _passage(*deals), *[d[2] for d in deals])
    md = _deal_md(claims or _deal_claims(deals), len(deals))
    _reread_log(ws, md, outcome)
    usage: list = []
    report = verify_answer(_two_calls(lambda t: first, lambda t: second(t) if callable(second) else second, **kw), _answer(md), ws, "?", usage)
    return report, report.apply(_answer(md)).answer_markdown, usage, ws


def _by_passage(deals):
    """A second reading that checks each claim against the stored passage: supported only when its date, amount and count stand together."""
    truth = {(d, a, c) for _, d, a, c in deals}

    def second(t: str) -> dict:
        found = re.findall(r"בהסכם מיום (\S+) שולמו (\S+) ₪ עבור (\d+) דירות", t)
        return _reading(*[_claim(f"בהסכם מיום {d} שולמו {a} ₪ עבור {c} דירות", "supported" if (d, a, c) in truth else "unsupported") for d, a, c in found])

    return second


@pytest.mark.parametrize("deals", [DEALS, DEALS_B], ids=["two", "three"])
@pytest.mark.parametrize("reverse", [False, True])
def test_claims_whose_dates_only_the_values_passage_holds_are_confirmed_when_the_number_rereads_found_nothing(deals, reverse):
    """The traced shape: the first reading failed every claim for dates the cited values do not state, the re-reads found nothing; a confirming
    reading is shown the passage the values were registered from (dates and amounts on different lines) and keeps what it supports."""
    claims = _deal_claims(deals, reverse)
    first = _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported")
    seen: list = []
    report, out, usage, ws = _deal_run(first, _by_passage(deals), deals, claims, confirm_seen=seen)
    assert report.removed_units() == set() and all(a in out for _, _, a, _ in deals), out
    assert _purposes(usage) == ["verify", "verify_confirm"] and usage[1]["claims"] == len(deals)
    assert _outcomes(report) == ["flipped"] * len(deals) and "אומת חלקית" in out
    assert len(seen) == 1 and 'id="S1"' in seen[0] and 'candidate="true"' in seen[0]
    assert all(d in seen[0] and a in seen[0] for _, d, a, _ in deals)  # the date lines and the amount lines of the passage are shown


def test_a_date_that_belongs_to_another_agreement_is_removed_and_the_claim_that_stands_stays():
    claims = _deal_claims(DEALS, dates=("05.09.2021", "05.09.2021"))  # the first agreement's amount is given the second one's date
    first = _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported")
    report, out, usage, ws = _deal_run(first, _by_passage(DEALS), DEALS, claims)
    assert "2,300,000" not in out and "3,100,000" in out, out
    assert sorted(_outcomes(report)) == ["both_failed", "flipped"]


def test_nothing_is_kept_when_the_second_reading_fails_every_claim_or_is_partial_on_the_amount_itself():
    claims = _deal_claims(DEALS)
    first = _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported")
    report, out, usage, ws = _deal_run(first, _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported"))
    assert report.removed_units() == {0} and "2,300,000" not in out and _outcomes(report) == ["both_failed"] * 2
    partial = _reading(*[_claim(c, "partial") for c in claims], *[_claim(f"{a} ₪", "unsupported") for _, _, a, _ in DEALS])
    report, out, usage, ws = _deal_run(first, partial)
    assert "2,300,000" not in out and "3,100,000" not in out and _outcomes(report) == ["not_confirmed"] * 2


def test_a_claim_a_reread_already_showed_its_passage_for_is_not_asked_again():
    """The exclusion stays for a claim whose own source passage a re-read showed a judge (the unit was judged again with it, or the number re-read
    found it): the confirming reading would read the same text."""
    claims = _deal_claims(DEALS)
    first = _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported")
    for outcome in ("unsupported", "found"):
        report, out, usage, ws = _deal_run(first, _by_passage(DEALS), DEALS, claims, outcome=outcome)
        assert _purposes(usage) == ["verify"] and report.removed_units() == {0}, outcome
    report, out, usage, ws = _deal_run(first, _by_passage(DEALS), DEALS, claims, outcome="bound")  # a bound / timed-out read showed nothing
    assert _purposes(usage) == ["verify", "verify_confirm"] and report.removed_units() == set()


def test_only_the_values_own_passage_is_shown_not_another_document_that_states_the_same_numbers():
    """No broad widening: a passage of another document that writes the claim's numbers (here its dates) is not offered."""
    ws = Workspace(ctx=None)
    _values(ws, _passage(*DEALS), *[d[2] for d in DEALS])
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח אחר", section="סיכום", location="סיכום", kind="context",
                  text="הערת מסמך אחר: 12.03.2019 ו-05.09.2021 הם תאריכי ישיבות, 2,300,000 ₪ הוא סכום שאינו קשור.")
    claims = _deal_claims(DEALS)
    md = _deal_md(claims, len(DEALS))
    _reread_log(ws, md)
    first = _reading(*[_claim(c, "unsupported") for c in claims], verdict="unsupported")
    seen: list = []
    verify_answer(_two_calls(lambda t: first, _by_passage(DEALS), confirm_seen=seen), _answer(md), ws, "?", [])
    assert len(seen) == 1 and 'id="S1"' in seen[0] and 'id="S2"' not in seen[0] and "הערת מסמך אחר" not in seen[0]


def test_a_claim_the_number_rereads_found_nothing_for_is_rescued_by_a_source_the_turn_read():
    from tests.unit.test_verify_reread import HELD, _by_text

    claim, cited, holder = HELD[0]
    md = f"{claim} [S1]."
    for outcome, rescued in (("not_found", True), ("supported", False)):
        ws = _same_doc_ws(cited, holder)
        _reread_log(ws, md, outcome)
        report = verify_answer(_by_text(lambda t: "unsupported"), _answer(md), ws, "?", [])
        assert (report.removed_units() == set()) is rescued and (report.claims[0][0].rescue == "S2") is rescued, outcome


# a claim the first reading left partly supported, whose number the judge found partly absent, has the protection of a removal (round 23 closure, P2)
PARTLY = _reading(_claim(FLOORS), _claim(GATE, "partial", failure="absent_from_source"))
PARTLY_MD = f"{FLOORS} ו{GATE} [S1]."


def test_a_partly_supported_claim_whose_number_is_partly_absent_is_read_a_second_time_and_marked_when_kept():
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(GATE)), md=PARTLY_MD, first=PARTLY)
    assert _purposes(usage) == ["verify", "verify_confirm"] and _outcomes(report) == ["flipped"]
    assert "7,000" in out and "אומת חלקית" in out
    assert [p.check for p in report.problems if p.severity == "partial"] == [verify.CONFIRM_CHECK]
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(GATE, "unsupported")), md=PARTLY_MD, first=PARTLY)
    assert _outcomes(report) == ["not_confirmed"] and "7,000" in out  # (the first verdict was partial: it stands, nothing is removed for it)


@pytest.mark.parametrize("defect", ["none", "part"])
def test_a_partly_supported_claim_that_is_only_a_mark_is_not_read_again(defect):
    """No failure named for the claim: it stays on the page marked whatever the unit's defect - a second call for it would buy nothing."""
    only_mark = _reading(_claim(FLOORS), _claim(GATE, "partial"), defect=defect)
    report, out, usage = _run(_reading(_claim(FLOORS), _claim(GATE)), md=PARTLY_MD, first=only_mark)
    assert _purposes(usage) == ["verify"] and report.confirm == []


def test_a_table_cell_the_second_reading_gives_partly_is_never_divided_the_row_follows_the_first_verdict():
    """The division is for a sentence: a table row is cut by its cells (``row_cut_safe``), so a ``partial`` cell with a failing span inside is no
    confirmation and the cell is cut as before."""
    ws = _same_doc_ws("מקדם ב הוא 5.10. משך החכירה 36 חודשים.")
    md = "| נתון | ערך | משך |\n|---|---|---|\n| רכיב א | 5.10 במרפסת [S1] | 36 חודשים [S1] |"
    cell = "5.10 במרפסת"
    first = _reading(_claim(cell, "unsupported"), _claim("36 חודשים"))
    second = _reading(_claim(cell, "partial"), _claim("במרפסת", "unsupported"), _claim("36 חודשים"))
    usage: list = []
    report = verify_answer(_two_calls(lambda t: first, lambda t: second), _answer(md), ws, "?", usage)
    out = report.apply(_answer(md)).answer_markdown
    assert "5.10" not in out and "36 חודשים" in out and _outcomes(report) == ["not_confirmed"], out


# --- P3: a decision is reused only for the same text, evidence and context; a kept claim keeps its condition words -----------------------------

def test_a_settled_decision_is_not_reused_when_the_evidence_digest_changed():
    cache, md = VerdictCache(), f"{GATE} [S1]."
    judge = _two_calls(lambda t: "unsupported", lambda t: "supported")
    usage: list = []
    verify_answer(judge, _answer(md), _same_doc_ws("עלות השילוט 7,000 ₪."), "?", usage, cache=cache)
    assert _purposes(usage) == ["verify", "verify_confirm"]
    n = len(usage)
    verify_answer(judge, _answer(md), _same_doc_ws("עלות השילוט 7,000 ₪."), "?", usage, cache=cache)
    assert len(usage) == n  # the same text over the same evidence: decided
    changed = verify_answer(judge, _answer(md), _same_doc_ws("עלות השילוט 7,000 ₪. הערה חדשה על המבנה."), "?", usage, cache=cache)
    assert _purposes(usage[n:]) == ["verify", "verify_confirm"]  # the cited passage changed: another datum, read twice again
    assert "אומת חלקית" in changed.apply(_answer(md)).answer_markdown


def test_a_decision_for_one_row_is_not_reused_for_another_rows_equal_number():
    cache = VerdictCache()
    source = "מקדם ב הוא 5.10. משך החכירה 36 חודשים."

    def first(t):
        return _reading(_claim("5.10", "unsupported"), _claim("36 חודשים")) if "5.10" in t else None

    def second(t):
        return _reading(_claim("5.10"), _claim("36 חודשים"), verdict="supported")

    def table(label: str) -> str:
        return f"| נתון | ערך | משך |\n|---|---|---|\n| {label} | 5.10 [S1] | 36 חודשים [S1] |"

    usage: list = []
    judge = _two_calls(first, second)
    one = verify_answer(judge, _answer(table("רכיב א")), _same_doc_ws(source), "?", usage, cache=cache)
    assert _purposes(usage) == ["verify", "verify_confirm"] and _outcomes(one) == ["flipped"]
    n = len(usage)
    verify_answer(judge, _answer(table("רכיב א")), _same_doc_ws(source), "?", usage, cache=cache)
    assert len(usage) == n  # the same row: decided
    verify_answer(judge, _answer(table("רכיב ב")), _same_doc_ws(source), "?", usage, cache=cache)
    assert _purposes(usage[n:]) == ["verify", "verify_confirm"]  # the equal number of another row is asked again


def test_a_kept_claim_keeps_its_condition_and_the_cut_takes_only_the_failing_span():
    kept = f"{FENCE} בתנאי שהמבנה פנוי"
    wide = f"{kept} ו{GATE}"
    first = _reading(_claim(FLOORS), _claim(wide, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(wide, "partial"), _claim(kept), _claim(f"ו{GATE}", "unsupported"))
    report, out, usage = _run(second, md=f"{FLOORS}, {wide} [S1].", first=first, source=f"{SOURCE} התנאי: המבנה פנוי.")
    assert kept in out and "7,000" not in out and _outcomes(report) == ["partly_kept"], out


# --- review R1-R3: what the second reading's claims may confirm -------------------------------------------------------------------------------

def test_a_supported_claim_for_a_longer_number_does_not_confirm_a_shorter_one():
    """R1: "11,200" holds the digits "1,200" but is not the number: the claim must lie in the wider one on token boundaries."""
    text = "עלות השער 11,200 ₪"
    first = _reading(_claim("1,200 ₪", "unsupported"), verdict="unsupported")
    report, out, usage = _run(_reading(_claim(text), verdict="supported"), md=f"{text} [S1].", first=first, source="עלות השער 11,200 ₪.")
    assert _outcomes(report) == ["not_confirmed"] and report.confirm_summary()["flips"] == 0


@pytest.mark.parametrize(("failed", "wider"), [("1,200 ₪", "עלות הגדר 91,200 ₪"), ("4,100", "דמי 14,100"), ("12 דירות", "בבניין 112 דירות")])
def test_a_wider_claim_that_only_contains_the_digits_is_no_claim_for_the_span(failed, wider):
    first = _reading(_claim(failed, "unsupported"), verdict="unsupported")
    report, out, usage = _run(_reading(_claim(wider), verdict="supported"), md=f"{wider} [S1].", first=first, source=f"{wider}.")
    assert _outcomes(report) == ["not_confirmed"]


def test_a_wider_claim_that_holds_the_span_on_token_boundaries_confirms_it():
    first = _reading(_claim("1,200 ₪", "unsupported"), verdict="unsupported")
    wider = "עלות השער 1,200 ₪"
    report, out, usage = _run(_reading(_claim(wider), verdict="supported"), md=f"{wider} [S1].", first=first, source="הערך הכללי הוא 1,200 ₪.")
    assert _outcomes(report) == ["flipped"]


def test_a_supported_claim_made_of_a_unit_verdict_alone_never_confirms_it_the_stray_record_of_a_fallback_neither():
    """R2: the whole-unit record the server makes of a unit verdict (or of spans the unit does not hold) is not a claim of the second reading."""
    report, out, usage = _run(_reading(_claim("טקסט שאינו ביחידה"), verdict="supported"), md=f"{GATE} [S1].", first={"verdict": "unsupported"})
    assert report.removed_units() == {0} and _outcomes(report) == ["not_confirmed"]


PAINT = "עלות הצביעה 3,000 ₪"
THREE_PARTS = f"{FENCE}, {GATE} ו{PAINT}"


def test_a_number_of_the_partial_parent_that_no_supported_claim_covers_is_not_kept():
    """R3: the failing span is cut, but the third datum stands in no supported claim of the second reading: it would be kept unchecked."""
    first = _reading(_claim(FLOORS), _claim(THREE_PARTS, "unsupported"))
    source = f"{SOURCE} עלות הצביעה האחרת 3,000 ₪."
    md = f"{FLOORS}, {THREE_PARTS} [S1]."
    uncovered = _reading(_claim(FLOORS), _claim(THREE_PARTS, "partial"), _claim(FENCE), _claim(GATE, "unsupported"))
    report, out, usage = _run(uncovered, md=md, first=first, source=source)
    assert _outcomes(report) == ["not_confirmed"] and "3,000" not in out and "52,000" not in out, out
    covered = _reading(_claim(FLOORS), _claim(THREE_PARTS, "partial"), _claim(FENCE), _claim(f"ו{PAINT}"), _claim(GATE, "unsupported"))
    report, out, usage = _run(covered, md=md, first=first, source=source)
    assert _outcomes(report) == ["partly_kept"] and "3,000" in out and "52,000" in out and "7,000" not in out, out


# --- the split path of the second reading ------------------------------------------------------------------------------------------------------

def test_a_second_reading_that_splits_the_claim_keeps_the_part_in_its_supported_claims_and_cuts_the_failing_one():
    second = _reading(_claim(FLOORS), _claim(FENCE), _claim(f"ו{GATE}", "unsupported"))  # (no claim for the whole: it split it)
    report, out, usage = _run(second, first=WIDE_FIRST)
    assert _outcomes(report) == ["partly_kept"] and FENCE in out and "7,000" not in out and "אומת חלקית" in out, out


def test_a_number_that_stays_in_no_supported_claim_of_a_split_reading_is_not_confirmed():
    second = _reading(_claim(FLOORS), _claim(f"ו{GATE}", "unsupported"))
    report, out, usage = _run(second, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "52,000" not in out


def test_when_every_number_of_the_claim_is_in_a_failing_span_both_readings_failed_it_and_the_removal_is_settled():
    cache = VerdictCache()
    second = _reading(_claim(FLOORS), _claim(BOTH, "partial"), _claim(FENCE, "unsupported"), _claim(f"ו{GATE}", "unsupported"))
    report, out, usage = _run(second, first=WIDE_FIRST, cache=cache)
    assert _outcomes(report) == ["both_failed"] and "52,000" not in out and "7,000" not in out
    assert [e.decision for es in cache.settled.values() for e in es] == ["removed"]


def test_a_supported_claim_with_a_failing_span_inside_it_is_no_confirmation():
    second = _reading(_claim(FLOORS), _claim(BOTH, "supported"), _claim(f"ו{GATE}", "unsupported"))
    report, out, usage = _run(second, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "52,000" not in out


# --- review R4: the value's own passage, and only the block it stands in ----------------------------------------------------------------

FAR = ["הסכם נחתם ביום 12.03.2019.", "פסקה על תנאי התשלום.", "פסקה על המסירה.", "שולם סכום של 2,300,000 ₪ עבור 4 דירות."]
NEAR = [FAR[1], FAR[0], FAR[3], FAR[2]]


def _one_deal_unit(lines: list[str], failing_by_judge: bool = True):
    ws = Workspace(ctx=None)
    _values(ws, "\n".join(lines), "2,300,000")
    claim = "בהסכם מיום 12.03.2019 שולמו 2,300,000 ₪ עבור 4 דירות"
    md = f"{claim} [V1]."
    _reread_log(ws, md)
    first = _reading(_claim(claim, "unsupported"), verdict="unsupported")
    usage: list = []
    report = verify_answer(_two_calls(lambda t: first, lambda t: _reading(_claim(claim), verdict="supported")), _answer(md), ws, "?", usage)
    return report, usage, ws, claim


def test_a_judge_failed_claim_with_a_number_far_from_the_values_lines_is_not_asked():
    """R4: the date is in the stored passage, but three lines from the value's quote: not the value's own block. (The judge failed the claim: it is
    not in the server's reasons, and it was asked regardless before.)"""
    report, usage, ws, claim = _one_deal_unit(FAR)
    assert _purposes(usage) == ["verify"] and report.removed_units() == {0}


def test_a_number_on_the_line_next_to_the_values_quote_is_asked_and_kept():
    report, usage, ws, claim = _one_deal_unit(NEAR)
    assert _purposes(usage) == ["verify", "verify_confirm"] and report.removed_units() == set()


def test_the_value_window_is_the_lines_of_its_quote_and_the_line_before_and_after():
    ws = Workspace(ctx=None)
    _values(ws, "\n".join(FAR), "2,300,000")
    window = verify._value_window(ws, "V1")
    assert FAR[3] in window and FAR[2] in window and FAR[0] not in window and FAR[1] not in window
    assert verify._value_window(ws, "V9") == ""


def _record(text: str) -> verify.ClaimRecord:
    return verify.ClaimRecord("k", "u", 0, 0, 0, text, "datum", "unsupported")


def test_the_unstated_numbers_must_stand_as_written_in_the_values_window():
    ws = Workspace(ctx=None)
    _values(ws, "\n".join(NEAR), "2,300,000")
    claim = "בהסכם מיום 12.03.2019 שולמו 2,300,000 ₪ עבור 4 דירות"
    (u,) = verify.split_units(f"{claim} [V1].")
    u.unstated = ["12.03.2019"]
    assert verify._stated_by_value_passage(u, _record(claim), ws)
    u.unstated = ["12.03.2019", "05.09.2021"]  # a second date no line of the window writes
    assert not verify._stated_by_value_passage(u, _record(f"{claim} ובהסכם מיום 05.09.2021"), ws)
    u.unstated = ["12.03.2019"]
    far = Workspace(ctx=None)
    _values(far, "\n".join(FAR), "2,300,000")
    assert not verify._stated_by_value_passage(u, _record(claim), far)
    u.ids = ["S1"]  # a unit that cites no value has no window
    assert not verify._stated_by_value_passage(u, _record(claim), ws)


# --- review R7: a locator is never cut away from the number it locates --------------------------------------------------------------------------

LOCATOR_SOURCE = "הנכס בן 3 קומות. בקומה 7 נמצא המחסן. בבניין 4 יש מעלית. עלות הגידור היא 1,200 ₪."
COST = "עלות הגידור היא 1,200 ₪"


@pytest.mark.parametrize(("wide", "locator"), [(f"בקומה 7 {COST}", "בקומה 7"), (f"{COST} בבניין 4", "בבניין 4"), (f"{COST} בקומה 7", "בקומה 7")])
def test_a_locator_that_shares_a_clause_with_the_number_it_locates_is_not_cut_away(wide, locator):
    first = _reading(_claim(FLOORS), _claim(wide, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(wide, "partial"), _claim(COST), _claim(locator, "unsupported"))
    report, out, usage = _run(second, md=f"{FLOORS}, {wide} [S1].", first=first, source=LOCATOR_SOURCE)
    assert _outcomes(report) == ["not_confirmed"] and "1,200" not in out and "אומת חלקית" not in out, out


def test_a_locator_in_a_clause_of_its_own_may_be_cut():
    wide = f"{COST}; קומה 7"
    first = _reading(_claim(FLOORS), _claim(wide, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(wide, "partial"), _claim(COST), _claim("קומה 7", "unsupported"))
    report, out, usage = _run(second, md=f"{FLOORS}, {wide} [S1].", first=first, source=LOCATOR_SOURCE)
    assert _outcomes(report) == ["partly_kept"] and "1,200" in out and "קומה 7" not in out, out


def test_a_failing_span_that_is_a_datum_of_its_own_with_a_unit_and_words_may_share_the_clause():
    assert verify._own_datum("ועלות השער 7,000 ₪") and verify._own_datum("דמי הניהול הם 9,300 ₪")
    assert not verify._own_datum("בקומה 7") and not verify._own_datum("בבניין 4") and not verify._own_datum("7,000 ₪")


# --- the guards of the division, and the reuse of a cut ------------------------------------------------------------------------------------

SPLIT_SECOND = _reading(_claim(FLOORS), _claim(BOTH, "partial"), _claim(FENCE), _claim(f"ו{GATE}", "unsupported"))


def test_the_division_is_not_made_when_a_part_or_a_span_is_not_once_in_the_unit():
    (u,) = verify.split_units(f"{FENCE} ו{GATE}, {FENCE} [S1].")
    whole = verify.ClaimRecord("k", "u", 0, 0, 0, u.text, "datum", "unsupported", whole_unit=True)
    assert verify._cut_claims(u, whole, [f"ו{GATE}"]) is None  # the part that stands is twice in the unit
    assert verify._cut_claims(u, whole, [FENCE]) is None  # a span that is twice in the unit
    (u,) = verify.split_units(f"{FENCE} ו{GATE} [S1].")
    whole = verify.ClaimRecord("k", "u", 0, 0, 0, u.text, "datum", "unsupported", whole_unit=True)
    assert verify._cut_claims(u, whole, [f"ו{GATE}"]) == ([FENCE], [f"ו{GATE}"])
    assert verify._cut_claims(u, whole, ["עלות שאינה ביחידה"]) is None and verify._cut_claims(u, whole, []) is None
    assert verify._cut_claims(u, whole, [GATE, "7,000 ₪"]) is None  # overlapping spans


def test_a_division_whose_part_is_a_claim_of_the_unit_already_is_refused(monkeypatch):
    real = verify._cut_claims
    monkeypatch.setattr(verify, "_cut_claims", lambda u, r, spans: ([FLOORS, *real(u, r, spans)[0]], real(u, r, spans)[1]))  # (the id of the sibling claim)
    report, out, usage = _run(SPLIT_SECOND, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "7,000" not in out and "52,000" not in out


def test_a_division_the_ladder_cannot_cut_is_refused(monkeypatch):
    monkeypatch.setattr(verify, "_leaves_words", lambda u, groups: False)
    report, out, usage = _run(SPLIT_SECOND, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "52,000" not in out
    monkeypatch.undo()
    monkeypatch.setattr(verify, "_cut_groups", lambda u, recs: None)
    report, out, usage = _run(SPLIT_SECOND, first=WIDE_FIRST)
    assert _outcomes(report) == ["not_confirmed"] and "52,000" not in out


def test_a_span_that_holds_a_citation_inside_it_is_not_cut():
    """The failing span covers a citation of a claim that stays: the ladder does not cut it, so the division is refused."""
    md = f"{FLOORS}, {FENCE} [S1] ו{GATE} [S1]."
    (u,) = verify.split_units(md)
    both = u.text.split(", ", 1)[1].rstrip(" .")
    first = _reading(_claim(FLOORS), _claim(both, "unsupported"))
    second = _reading(_claim(FLOORS), _claim(both, "partial"), _claim(FLOORS), _claim(both, "unsupported"))
    report, out, usage = _run(second, md=md, first=first)
    assert _outcomes(report) != ["partly_kept"] and "7,000" not in out


def test_a_reused_cut_that_cannot_be_made_again_falls_back_to_the_first_verdict(monkeypatch):
    cache = VerdictCache()
    report, out, usage = _run(SPLIT_SECOND, first=WIDE_FIRST, cache=cache)
    assert _outcomes(report) == ["partly_kept"]
    n = len(usage)
    monkeypatch.setattr(verify, "_leaves_words", lambda u, groups: False)
    again = verify_answer(_two_calls(lambda t: WIDE_FIRST, lambda t: SPLIT_SECOND), _answer(f"{SENTENCE} [S1]."), _same_doc_ws(SOURCE), "?", usage, cache=cache)
    assert len(usage) == n and _outcomes(again) == ["not_confirmed"]
    assert "52,000" not in again.apply(_answer(f"{SENTENCE} [S1].")).answer_markdown


# --- P2: which partly supported claims are at risk, and how many calls they may take (review R8) ----------------------------------------

@pytest.mark.parametrize(("changes", "at_risk"), [
    ({}, True), ({"failure": "contradicts_source"}, True), ({"failure": "none"}, False), ({"failure": "wrong_unit"}, False),
    ({"rescue": "S2"}, False), ({"kind": "computed"}, False), ({"text": "ללא מספר כלל"}, False)])
def test_which_partly_supported_claims_have_the_protection_of_a_removal(changes, at_risk):
    base = dict(id="k", unit_id="u", unit=0, start=0, end=0, text=FENCE, kind="datum", verdict="partial", failure="absent_from_source")
    assert verify._value_at_risk(verify.ClaimRecord(**(base | changes))) is at_risk


def test_the_partly_supported_claims_take_at_most_half_the_cap_after_the_failing_ones(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 4)
    texts = [f"עלות הרכיב {n} היא {n},100 ₪" for n in (1, 2, 3, 4)]
    source = " ".join(f"הערך הכללי של {n} הוא {n},100 ₪." for n in (1, 2, 3, 4))
    md = "\n\n".join(f"{t} [S1]." for t in texts)

    def first(t):
        partial = {"verdict": "partial", "claims": [{"span": t, "verdict": "partial", "failure": "absent_from_source"}]}
        return "unsupported" if "הרכיב 4" in t else partial

    usage: list = []
    cache = VerdictCache()
    report = verify_answer(_two_calls(first, lambda t: {"verdict": "supported", "claims": [{"span": t, "verdict": "supported"}]}), _answer(md),
                           _same_doc_ws(source), "?", usage, cache=cache)
    assert [u["claims"] for u in usage if u["purpose"] == "verify_confirm"] == [3]  # the failing one and two of the three partly supported claims
    assert sorted(_outcomes(report)) == ["cap", "flipped", "flipped", "flipped"] and cache.soft_spent == 2
