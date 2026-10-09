"""A computed result is verified as a computation, never as a document claim (U8, KTD6, R14, R15, R17).

A unit whose number is a turn calculation's result, at the precision and scale it is shown in, is bound to that
calculation and cites it, unless it presents the number as written by the report, a party or the decision; the
calculation carries the VAT basis its inputs share; the judge reads the formula, inputs and assumptions. The judge
here is scripted: these tests prove what the server does with a computed claim and what the judge is shown, not the
model's judgement. Synthetic values only; the project is invented."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import pytest

from app.chat import calc, verify
from app.chat.engine import POLICY, FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import JUDGE_POLICY, deterministic, split_units, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider

SUBJECT = "פרויקט הדגמה"
QUESTION = "מה יהיה הרווח אם העלויות יעלו ב-5%, ומה שיעורו מההכנסות?"


def _value(ws: Workspace, written: str, label: str, kind: str, role: str, *, unit: str = "ILS", vat: str = "excluded",
           subject: str = SUBJECT) -> str:
    vid = f"V{len(ws.values) + 1}"
    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(),
                                None, "בדיקת כדאיות", "טבלת סיכום", label, kind, unit, "none", vat, "", subject, role,
                                {"unit": "source", "vat": "source"}, {"quote": f"{label} {written}"},
                                f"{label} {written}")
    return vid


def _assume(ws: Workspace, written: str, label: str, quote: str) -> str:
    aid = f"A{len(ws.assumptions) + 1}"
    ws.assumptions[aid] = calc.Assumption(aid, Decimal(written), written, "percent", label, quote, 1, True)
    return aid


def _name(ws: Workspace, i: str) -> str:
    for d in (ws.values, ws.assumptions, ws.computations):
        if i in d:
            return d[i].label
    return i


def _compute(ws: Workspace, expression: str, label: str, justification: str | None = None) -> calc.Computation:
    """A calculation registered as ``tools.tool_calculate`` registers it (without the database checks)."""
    node = calc.parse(expression)
    ids = list(dict.fromkeys(calc.ids_of(node)))
    operands = {i: ws.values[i].operand() if i in ws.values else ws.assumptions[i].operand()
                if i in ws.assumptions else calc.result_operand(i, ws.computations[i].outcome) for i in ids}
    out = calc.evaluate(node, operands, justification)
    inputs = []
    for i in out.inputs:
        kind = "value" if i in ws.values else "assumption" if i in ws.assumptions else "computation"
        value = (ws.values[i].value if i in ws.values else ws.assumptions[i].value if i in ws.assumptions
                 else ws.computations[i].value)
        entry = {"id": i, "label": _name(ws, i), "kind": kind, "value": str(value), "display": calc.fmt(value)}
        if i in ws.values:
            entry |= {"source_id": "S1", "value_text": ws.values[i].written}
        elif i in ws.assumptions:
            entry |= {"quote": ws.assumptions[i].quote, "value_text": ws.assumptions[i].written}
        inputs.append(entry)
    c = calc.Computation(f"C{len(ws.computations) + 1}", label, calc.render(node, lambda i: i),
                         calc.render(node, lambda i: f"«{_name(ws, i)}»", True), out, inputs, ["S1"], 1,
                         "scenario" if out.assumptions else "computed", justification if out.conditional else None,
                         "", None, [lf.id for lf in out.leaves])
    ws.computations[c.cid] = c
    return c


def _scenario(cost_vat: str = "excluded") -> Workspace:
    """V1 income 12,450,000 ₪ and V2 cost 10,400,000 ₪ (VAT excluded), A1 costs up 5%: C1 the profit (1,530,000 ₪),
    C2 its rate of the income (12.29%)."""
    ws = Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": QUESTION, "current": True}]
    _value(ws, "12,450,000", "סה״כ הכנסות", "income", "income")
    _value(ws, "10,400,000", "סה״כ עלויות", "cost", "cost", vat=cost_vat)
    _assume(ws, "5", "עליית העלויות", "העלויות יעלו ב-5%")
    justification = None if cost_vat == "excluded" else "בדיקה"
    _compute(ws, "V1 - V2*(1+A1%)", "הרווח בתרחיש", justification)
    _compute(ws, "C1 / V1", "שיעור הרווח מההכנסות", justification)
    return ws


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _judge(rule=lambda text: "supported", seen: list | None = None) -> ScriptedProvider:
    """A judge answering each unit by ``rule(unit_text)``; the inputs it was shown go to ``seen``."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append((instructions, input))
        return {"verdicts": [{"index": int(m.group(1)), "verdict": rule(m.group(2)), "reason": "בדיקה",
                              "supported_by": []}
                             for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _verify(ws: Workspace, markdown: str, rule=lambda text: "supported", seen: list | None = None,
            question: str = QUESTION):
    answer = _answer(markdown)
    report = verify_answer(_judge(rule, seen), answer, ws, question, [])
    return report, report.apply(answer)


def _numbers_only(ws: Workspace, markdown: str, question: str = QUESTION) -> list:
    units = split_units(markdown)
    verify.bind_computations(units, ws, question)
    return deterministic(units, ws, question, meanings={u.index: [] for u in units})


# --- a computed result cited only through its inputs ------------------------------------------------------------

def test_a_computed_profit_cited_only_through_its_input_values_is_kept_and_cites_its_calculation():
    ws = _scenario()
    seen: list = []
    report, applied = _verify(ws, "לפי הנחתך שהעלויות יעלו ב-5% [A1], הרווח בתרחיש יהיה 1,530,000 ₪ [V1][V2].",
                              seen=seen)
    assert not report.removed_units(), report.problems_text()
    assert report.ok
    assert "1,530,000 ₪ [V1][V2][C1]" in applied.answer_markdown and applied.status == "answered"
    # the judge reads the calculation the unit was bound to, with its formula and inputs
    assert any('<source id="C1"' in i and "נוסחה:" in i for _, i in seen)


def test_a_computed_result_in_a_unit_that_cites_nothing_is_bound_to_its_calculation():
    ws = _scenario()
    report, applied = _verify(ws, "הרווח בתרחיש יהיה 1,530,000 ₪.")
    assert not report.removed_units(), report.problems_text()
    assert "1,530,000 ₪ [C1]" in applied.answer_markdown


def test_a_unit_whose_other_numbers_are_not_the_calculations_stays_unbound():
    ws = _scenario()
    units = split_units("הרווח בתרחיש יהיה 1,530,000 ₪ על שטח של 4,321 מ״ר [V1][V2].")
    assert verify.bind_computations(units, ws, QUESTION) == {}
    assert units[0].ids == ["V1", "V2"]


# --- scale words -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shown, ok", [
    ("1.53 מיליון ₪", True), ("1.5 מיליון ₪", True), ("1,530 אלף ₪", True), ("1.53M ₪", True),
    ("1.53 מיל׳ ₪", True), ("₪1.53 מ׳", True), ("1,530,000 ₪", True),
    ("1.6 מיליון ₪", False), ("1.54 מיליון ₪", False), ("1.53 מיליארד ₪", False), ("1,531 אלף ₪", False),
    ("1.530 מיליון ₪", True), ("1.5300 מיליון ₪", True), ("1.53 ₪", False),
])
def test_a_result_shown_with_a_scale_word_is_its_value_at_the_shown_precision_and_scale(shown, ok):
    ws = _scenario()
    cited = _numbers_only(ws, f"הרווח בתרחיש יהיה {shown} [C1][A1].")
    assert (cited == []) is ok, cited
    through_inputs = _numbers_only(ws, f"הרווח בתרחיש יהיה {shown} [V1][V2][A1].")
    assert (through_inputs == []) is ok, through_inputs


@pytest.mark.parametrize("text, scale", [
    ("1.53 מיליון ₪", 10**6), ("850 אלף ₪", 10**3), ("1,530 אלפי ₪", 10**3), ("2.1 מיליארד ₪", 10**9),
    ("1.53M", 10**6), ("850K", 10**3), ("₪1.53 מ׳", 10**6), ("1.53 מ׳ ש״ח", 10**6), ("1.53 מיל׳", 10**6),
    # a metre (מ׳) without a currency, square metres written מ'ר, and words that only start alike are no scale
    ("3.5 מ׳", 1), ("120 מ'ר", 1), ("12 אלפיות", 1), ("120 מ״ר", 1), ("5%", 1),
])
def test_scale_after_reads_hebrew_scale_words_and_their_abbreviations(text, scale):
    m = re.search(r"\d[\d,.]*\d|\d", text)
    assert calc.scale_after(text, m.start(), m.end())[0] == scale


def test_display_matches_takes_a_scale():
    value = Decimal("1530000.4")
    assert calc.display_matches("1.53", False, value, (("ILS", 1),), scale=10**6)
    assert not calc.display_matches("1.6", False, value, (("ILS", 1),), scale=10**6)
    assert calc.display_matches("1,530", False, value, (("ILS", 1),), scale=10**3)
    assert not calc.display_matches("1.53", False, value, (("ILS", 1),))


# --- VAT basis -------------------------------------------------------------------------------------------------

def test_a_calculation_carries_the_vat_basis_its_money_inputs_share_and_shows_it_to_the_judge():
    ws = _scenario()
    assert ws.computations["C1"].vat == "excluded"
    assert ws.computations["C2"].vat is None  # a rate is not money
    assert "בסיס מע״מ של התוצאה ושל קלטיה הכספיים: ללא מע״מ" in verify.computation_text(ws.computations["C1"])
    assert ws.computations["C1"].public()["vat"] == "excluded"
    mixed = _scenario(cost_vat="unknown")
    assert mixed.computations["C1"].vat is None
    assert "בסיס מע״מ" not in verify.computation_text(mixed.computations["C1"])


@pytest.mark.parametrize("phrase, ok", [
    ("1,530,000 ₪ ללא מע״מ", True), ("1.53 מיליון ₪, לא כולל מע״מ", True), ("1,530,000 ₪ כולל מע״מ", False),
    ("1.53 מיליון ₪ כולל מע״מ", False),
])
def test_vat_phrasing_of_a_computed_amount_is_checked_against_its_inputs_basis(phrase, ok):
    ws = _scenario()
    problems = _numbers_only(ws, f"הרווח בתרחיש יהיה {phrase} [C1].")
    assert (problems == []) is ok, problems
    bound = _numbers_only(ws, f"הרווח בתרחיש יהיה {phrase} [V1][V2][A1].")
    assert (bound == []) is ok, bound


def test_vat_phrasing_of_a_result_whose_inputs_disagree_is_removed():
    ws = _scenario(cost_vat="unknown")
    problems = _numbers_only(ws, "הרווח בתרחיש יהיה 1,530,000 ₪ ללא מע״מ [C1].")
    assert problems and "מע\"מ" in problems[0].reason


# --- "the report states": a computed number is never framed as written in a document ---------------------------

@pytest.mark.parametrize("framed", [
    "השומה מציינת רווח של 1,530,000 ₪",
    "לפי השומה, הרווח בתרחיש הוא 1,530,000 ₪",
    "בדו״ח נכתב שהרווח יהיה 1,530,000 ₪",
    "לטענת היזם הרווח יהיה 1,530,000 ₪",
])
def test_a_computed_number_presented_as_stated_by_the_report_is_not_bound_and_is_removed(framed):
    ws = _scenario()
    units = split_units(f"{framed} [V1][V2].")
    assert verify.bind_computations(units, ws, QUESTION) == {}
    report, applied = _verify(ws, f"{framed} [V1][V2].")
    assert report.removed_units() == {0} and "[C1]" not in applied.answer_markdown
    # cited directly, the framing still fails: the report does not write the result
    direct = _numbers_only(ws, f"{framed} [C1].")
    assert direct and "חישוב" in direct[0].reason


def test_framing_the_inputs_as_the_reports_and_the_result_as_computed_is_bound():
    ws = _scenario()
    report, applied = _verify(ws, "השומה מציינת הכנסות של 12,450,000 ₪ ועלויות של 10,400,000 ₪, ולפי הנחתך [A1] "
                                  "הרווח המחושב יהיה 1,530,000 ₪ [V1][V2].")
    assert not report.removed_units(), report.problems_text()
    assert "[C1]" in applied.answer_markdown


def test_an_attribution_of_the_inputs_does_not_reach_a_result_in_a_clause_of_its_own():
    ws = _scenario()
    units = split_units("השומה מציינת הכנסות של 12,450,000 ₪ ועלויות של 10,400,000 ₪, והרווח בתרחיש של עלייה ב-5% "
                        "[A1] יהיה 1,530,000 ₪ [V1][V2].")
    assert verify.bind_computations(units, ws, QUESTION) == {0: ["C1"]}


def test_a_result_the_report_does_write_may_be_attributed_to_it():
    ws = _scenario()
    ws.computations["C1"].reproduces = {"source": "S1", "as_written": "1,530,000"}
    assert _numbers_only(ws, "השומה מציינת רווח של 1,530,000 ₪ [C1].") == []


# --- calculations of every shape are kept -----------------------------------------------------------------------

def _shapes() -> Workspace:
    ws = Workspace(ctx=None)
    _value(ws, "12,450,000", "סה״כ הכנסות", "income", "income")  # V1
    _value(ws, "10,400,000", "סה״כ עלויות", "cost", "cost")  # V2
    _value(ws, "2,000,000", "סף הרווח הנדרש", "other", "other")  # V3
    _value(ws, "9,500", "שווי למ״ר", "value_per_area", "other", unit="ILS_per_sqm")  # V4
    _value(ws, "0.95", "מקדם קומה", "coefficient", "other", unit="ratio", vat="not_applicable")  # V5
    _value(ws, "120", "שטח הדירה", "area", "other", unit="sqm", vat="not_applicable")  # V6
    _compute(ws, "V1 - V2", "הרווח")  # C1 2,050,000
    _compute(ws, "C1 / V1", "שיעור הרווח")  # C2 16.47%
    _compute(ws, "C1 - V3", "הרווח מעל הסף")  # C3 50,000
    _compute(ws, "V4 * V5 * V6", "שווי מתואם")  # C4 1,083,000
    return ws


@pytest.mark.parametrize("markdown", [
    "ההפרש בין ההכנסות לעלויות הוא 2,050,000 ₪ [C1].",
    "ההפרש בין ההכנסות לעלויות הוא 2,050,000 ₪ [V1][V2].",
    "שיעור הרווח מההכנסות הוא 16.5% [C2].",
    "שיעור הרווח מההכנסות הוא 16.47% [V1][V2].",
    "הרווח עולה על הסף הנדרש ב-50,000 ₪ [C3].",
    "הרווח עולה על הסף הנדרש ב-50 אלף ₪ [V1][V2][V3].",
    "השווי לאחר החלת מקדם הקומה הוא 1,083,000 ₪ [C4].",
    "השווי לאחר החלת מקדם הקומה הוא כ-1.08 מיליון ₪ [V4][V5][V6].",
    "הרווח הוא 2,050,000 ₪, שהם 16.5% מההכנסות [C2].",
])
def test_a_difference_a_rate_a_threshold_coefficients_and_a_chain_are_kept(markdown):
    ws = _shapes()
    report, applied = _verify(ws, markdown, question="מה הרווח?")
    assert not report.removed_units(), report.problems_text()
    assert re.search(r"\[C\d\]", applied.answer_markdown)


# --- the judge checks a computed claim as a computation ---------------------------------------------------------

def test_the_judge_policy_checks_inputs_scenario_assumptions_formula_base_and_units():
    for words in ("קלטים", "שלב", "תרחיש", "הנחה", "נוסחה", "מכנה", "בסיס האחוז", "יחידות", "כפי שנכתב"):
        assert words in JUDGE_POLICY, words


def test_a_rate_over_the_wrong_denominator_is_shown_to_the_judge_with_its_formula_and_removed_when_marked():
    ws = _scenario()
    seen: list = []
    report, applied = _verify(
        ws, "הרווח בתרחיש הוא 12.3% מהעלויות [C2].", seen=seen,
        rule=lambda text: "unsupported" if "מהעלויות" in text else "supported",
        question="מה יהיה שיעור הרווח מהעלויות אם העלויות יעלו ב-5%?")
    judged = "\n".join(i for _, i in seen)
    assert "«הרווח בתרחיש» ÷ «סה״כ הכנסות»" in judged  # the denominator the judge checks against the request
    assert report.removed_units() == {0} and "12.3%" not in applied.answer_markdown


def test_an_input_from_another_stage_is_shown_to_the_judge_with_its_subject_and_removed_when_marked():
    ws = Workspace(ctx=None)
    _value(ws, "12,450,000", "סה״כ הכנסות", "income", "income")
    _value(ws, "4,300,000", "עלויות", "cost", "cost", subject=f"{SUBJECT} — שלב א")
    _compute(ws, "V1 - V2", "הרווח", "בדיקה")
    seen: list = []
    report, _ = _verify(ws, "הרווח בפרויקט כולו הוא 8,150,000 ₪ [C1].", seen=seen,
                        rule=lambda text: "unsupported", question="מה הרווח בפרויקט כולו?")
    judged = "\n".join(i for _, i in seen)
    assert "V2 עלויות = 4,300,000 (נושא: פרויקט הדגמה — שלב א" in judged
    assert report.removed_units() == {0}


def test_the_assumption_a_scenario_rests_on_is_shown_with_the_users_words_and_a_conditional_result_as_conditional():
    ws = _scenario(cost_vat="unknown")
    seen: list = []
    _verify(ws, "בתרחיש המותנה הרווח יהיה 1,530,000 ₪ [C1].", seen=seen)
    judged = "\n".join(i for _, i in seen)
    assert "«העלויות יעלו ב-5%»" in judged and "מותנה" in judged


# --- the model's policy: compute when the inputs are found, ask or condition, a failure is a failure ------------

def test_the_policy_computes_from_found_inputs_asks_or_conditions_a_missing_assumption_and_names_a_failure():
    assert "גם אם המסמך עצמו אינו מציג את החישוב" in POLICY
    assert "תרחיש מותנה" in POLICY and "שאל" in POLICY
    assert "החישוב נכשל" in POLICY and "חסרים נתונים" in POLICY
    assert "נכתבו במסמך" in POLICY and "חושבו עכשיו" in POLICY


def test_a_division_by_zero_on_found_values_is_a_calculation_failure_not_missing_data():
    zero = calc.operand("V2", "0", "ILS", kind="cost", role="cost")
    income = calc.operand("V1", "100", "ILS", kind="income", role="income")
    with pytest.raises(calc.CalcError) as e:
        calc.evaluate(calc.parse("V1 / V2"), {"V1": income, "V2": zero})
    assert "החישוב נכשל" in str(e.value) and "לא נתון חסר" in str(e.value)


# --- structured removal decisions for computed claims (round 7 U4: KTD5, R12, R14) --------------------------------

@pytest.mark.parametrize("markdown, check", [
    ("הרווח בתרחיש יהיה 1,600,000 ₪ [C1].", "computation_mismatch"),  # not the result at the precision shown
    ("השומה מציינת רווח של 1,530,000 ₪ [C1].", "framed_result"),  # computed now, presented as the report's
])
def test_a_computed_claim_that_fails_a_deterministic_check_is_a_wrong_calculation(markdown, check):
    report, _ = _verify(_scenario(), markdown)
    (decision,) = report.removals()
    assert (decision.failure_kind, decision.check) == ("wrong_calculation", check)
    assert "C1" in decision.checked_ids


def test_a_judges_unsupported_computed_claim_is_a_wrong_calculation_unless_it_names_another_kind():
    question = "מה יהיה שיעור הרווח מהעלויות אם העלויות יעלו ב-5%?"
    report, _ = _verify(_scenario(), "הרווח בתרחיש הוא 12.3% מהעלויות [C2].", rule=lambda text: "unsupported",
                        question=question)
    assert [(d.failure_kind, d.check) for d in report.removals()] == [("wrong_calculation", "judge")]


@pytest.mark.parametrize("shown", ["1.53 מיליון ₪", "כ-1.5 מיליון ₪", "1,530,000 ₪"])
def test_a_computed_result_shown_in_an_equivalent_form_or_marked_rounding_is_kept(shown):
    report, _ = _verify(_scenario(), f"לפי הנחתך [A1], הרווח בתרחיש יהיה {shown} [C1].")
    assert not report.removed_units(), report.problems_text()
