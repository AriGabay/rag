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


# --- a conditional result is shown as conditional, never lost (R18, R24, R28; KTD5, KTD10) ------------------------

COST_QUESTION = "מה עלות הבנייה העילית והחניון יחד?"


def _conditional_sum(uncertain: tuple[str, ...] = ("V1", "V2")) -> Workspace:
    """V1 15,600,000 ₪ and V2 4,620,000 ₪ read from an image table; C1 their sum (20,220,000 ₪), conditional on the
    inputs in ``uncertain`` as ``tools.tool_calculate`` records it."""
    from app.chat.tools import MSG_UNCERTAIN_INPUTS

    ws = Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": COST_QUESTION, "current": True}]
    _value(ws, "15,600,000", "בנייה עילית", "cost", "cost")
    _value(ws, "4,620,000", "חניון תת-קרקעי", "cost", "cost")
    c = _compute(ws, "V1 + V2", "עלות הבנייה והחניון")
    if uncertain:
        c.outcome.conditional.append(MSG_UNCERTAIN_INPUTS.format(ids=", ".join(uncertain)) + " (V1: התא לא אושר)")
        c.uncertain = list(uncertain)
    return ws


def _qualified(ws: Workspace, markdown: str, seen: list | None = None, rule=lambda text: "supported"):
    return _verify(ws, markdown, rule=rule, seen=seen, question=COST_QUESTION)


def test_a_result_of_a_conditional_calculation_shown_without_a_hedge_is_kept_with_the_servers_qualifier():
    ws = _conditional_sum()
    seen: list = []
    report, applied = _qualified(ws, "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].", seen=seen)
    assert not report.removed_units() and report.removals() == [], report.problems_text()
    assert report.ok  # the server writes the qualifier itself: no repair round for it alone
    (p,) = [p for p in report.problems if p.annotatable]
    assert p.conditional and p.cite == "C1" and p.number == "20,220,000"
    assert ("20,220,000 ₪ (תוצאה מותנית: הערכים V1, V2 אינם ודאיים) [C1]."
            in applied.answer_markdown), applied.answer_markdown
    assert "כפי שנכתב במקור" not in applied.answer_markdown  # the server's, never presented as the source's
    # shown as conditional, never as verified certainty; not a removal and not a source qualifier
    counts = report.counts()
    assert counts["conditional"] == 1 and counts["annotated"] == 0 and counts["removed"] == 0
    assert counts["correctness"] == "partial" and counts["removals"] == []
    # the judge reads the unit with the qualifier the server will write, and is told so
    judged = "\n".join(i for _, i in seen)
    assert '<server_qualifier unit="0">(תוצאה מותנית: הערכים V1, V2 אינם ודאיים)</server_qualifier>' in judged
    assert "server_qualifier" in JUDGE_POLICY


def test_a_conditional_qualifier_names_one_uncertain_input_in_the_singular():
    _, applied = _qualified(_conditional_sum(("V2",)), "סך העלות הוא 20,220,000 ₪ [C1].")
    assert "20,220,000 ₪ (תוצאה מותנית: הערך V2 אינו ודאי) [C1]" in applied.answer_markdown


def test_a_conditional_calculation_on_a_justified_mix_is_qualified_with_the_mix_it_rests_on():
    ws = _scenario(cost_vat="unknown")
    report, applied = _verify(ws, "לפי הנחתך [A1], הרווח בתרחיש יהיה 1,530,000 ₪ [C1].")
    assert not report.removed_units(), report.problems_text()
    assert "1,530,000 ₪ (תוצאה מותנית: לפי הצדקה לערבוב נתונים שאינם תואמים — מע״מ:" in applied.answer_markdown


def test_a_unit_that_already_says_its_result_is_conditional_gets_only_the_short_reason():
    _, applied = _qualified(_conditional_sum(), "בכפוף לאימות הקלטים, התוצאה מותנית: 20,220,000 ₪ [C1].")
    assert "20,220,000 ₪ (הערכים V1, V2 אינם ודאיים) [C1]" in applied.answer_markdown
    assert applied.answer_markdown.count("מותנית") == 1


def test_a_wrong_number_for_a_conditional_calculation_is_still_removed_as_a_wrong_calculation():
    report, applied = _qualified(_conditional_sum(), "עלות הבנייה העילית והחניון יחד היא 20,230,000 ₪ [C1].")
    assert [(d.failure_kind, d.check) for d in report.removals()] == [("wrong_calculation", "computation_mismatch")]
    assert "תוצאה מותנית" not in applied.answer_markdown


def test_a_conditional_result_the_judge_finds_wrong_is_still_removed_and_not_qualified():
    report, applied = _qualified(_conditional_sum(), "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].",
                                 rule=lambda text: "unsupported")
    assert [d.failure_kind for d in report.removals()] == ["wrong_calculation"]
    assert "תוצאה מותנית" not in applied.answer_markdown and report.counts()["conditional"] == 0


def test_a_result_of_a_calculation_that_is_not_conditional_gets_no_qualifier():
    report, applied = _qualified(_conditional_sum(()), "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].")
    assert not report.problems, report.problems_text()
    assert "מותנית" not in applied.answer_markdown and report.counts()["correctness"] == "verified"


def test_a_conditional_result_bound_to_a_unit_that_cites_only_its_inputs_is_qualified_too():
    _, applied = _qualified(_conditional_sum(), "סך העלות הוא 20,220,000 ₪ [V1][V2].")
    assert "20,220,000 ₪ (תוצאה מותנית: הערכים V1, V2 אינם ודאיים) [V1][V2][C1]" in applied.answer_markdown


# --- amounts a source states in a scale (R14): "באלפי ₪" -------------------------------------------------------------
#
# A table states its amounts in thousands; the values carry that scale (``Value.scale``) and so do the results over
# them (``Outcome.scale``). A number shown in another equivalent representation — with a scale word, or in full — is
# the same amount; a number wrong at its scale never is.

THOUSANDS_QUESTION = "מה יהיה הרווח אם העלויות יעלו ב-5%, ומה עמלת השיווק של 1.5% מההכנסות?"


def _thousands() -> Workspace:
    """V1 income 412,300 and V2 cost 368,150, both in thousands of ₪ (a table "באלפי ₪"); A1 costs up 5%, A2 a
    1.5% fee: C1 the profit in the scenario (25,742.5 thousand ₪ = 25.74 million ₪), C2 the fee (6,184.5 thousand
    ₪)."""
    ws = Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": THOUSANDS_QUESTION, "current": True}]
    for vid in (_value(ws, "412,300", "סה״כ הכנסות", "income", "income"),
                _value(ws, "368,150", "סה״כ עלויות", "cost", "cost")):
        ws.values[vid].scale = 1000
    _assume(ws, "5", "עליית העלויות", "העלויות יעלו ב-5%")
    _assume(ws, "1.5", "עמלת שיווק", "עמלת השיווק של 1.5%")
    _compute(ws, "V1 - V2*(1+A1%)", "הרווח בתרחיש")
    _compute(ws, "V1 * A2%", "עמלת השיווק")
    return ws


def test_a_result_over_amounts_in_thousands_is_in_thousands():
    ws = _thousands()
    c1, c2 = ws.computations["C1"], ws.computations["C2"]
    assert (c1.value, c1.scale, c1.unit_label) == (Decimal("25742.5"), 1000, "אלפי ₪")
    assert (c2.value, c2.scale) == (Decimal("6184.5"), 1000)
    # the judge reads the result with its scale and the amount it is in units
    assert "25,742,500 ביחידות מלאות" in verify.computation_text(c1, ws)


@pytest.mark.parametrize("shown", [
    "25.74 מיליון ₪", "כ-25.7 מיליון ₪", "25,742.5 אלף ₪", "25,742,500 ₪", "25,742.5 ₪", "26 מיליון ₪",
])
def test_a_result_in_thousands_shown_in_an_equivalent_representation_is_kept(shown):
    report, _ = _verify(_thousands(), f"לפי הנחתך [A1], הרווח בתרחיש יהיה {shown} [C1].",
                        question=THOUSANDS_QUESTION)
    assert not report.removed_units(), report.problems_text()


@pytest.mark.parametrize("shown", ["6,184.5 אלף ₪", "6.18 מיליון ₪", "6.2 מיליון ₪"])
def test_a_fee_in_thousands_shown_in_thousands_or_millions_is_kept(shown):
    report, _ = _verify(_thousands(), f"עמלת השיווק תהיה {shown} [C2][A2].", question=THOUSANDS_QUESTION)
    assert not report.removed_units(), report.problems_text()


@pytest.mark.parametrize("shown", [
    "27.4 מיליון ₪",  # a wrong digit
    "25,742.5 מיליון ₪",  # the digits of the result with a scale word that makes it another amount
    "25.74 אלף ₪",  # thousands of thousands, shown as thousands
    "2.57 מיליון ₪",
])
def test_a_result_in_thousands_wrong_at_its_scale_is_removed_as_a_wrong_calculation(shown):
    report, _ = _verify(_thousands(), f"לפי הנחתך [A1], הרווח בתרחיש יהיה {shown} [C1].",
                        question=THOUSANDS_QUESTION)
    (decision,) = report.removals()
    assert (decision.failure_kind, decision.check) == ("wrong_calculation", "computation_mismatch")


def test_a_result_in_thousands_shown_in_millions_without_citing_it_is_bound_to_its_calculation():
    report, applied = _verify(_thousands(), "לפי הנחתך [A1], הרווח בתרחיש יהיה 25.74 מיליון ₪ [V1][V2].",
                              question=THOUSANDS_QUESTION)
    assert not report.removed_units(), report.problems_text()
    assert "25.74 מיליון ₪ [V1][V2][C1]" in applied.answer_markdown


@pytest.mark.parametrize("shown, ok", [
    ("412,300,000 ₪", True), ("412.3 מיליון ₪", True), ("412,300 אלף ₪", True), ("412,300 ₪", True),
    ("כ-412 מיליון ₪", True),
    ("412,300 מיליון ₪", False), ("4.123 מיליון ₪", False), ("412,300,000,000 ₪", False),
])
def test_a_value_written_in_thousands_restated_in_full_or_with_a_scale_word(shown, ok):
    cited = _numbers_only(_thousands(), f"ההכנסות הכוללות הן {shown} [V1].", question=THOUSANDS_QUESTION)
    assert (cited == []) is ok, cited


def test_a_scale_word_on_a_value_whose_scale_no_source_states_is_checked_as_before():
    ws = _scenario()  # amounts in units: no scale stated
    assert _numbers_only(ws, "ההכנסות הכוללות הן 12.45 מיליון ₪ [V1].") == []
    (problem,) = _numbers_only(ws, "ההכנסות הכוללות הן 12.5 מיליון ₪ [V1].")
    assert "12.5" in problem.reason


def test_a_sum_of_an_amount_in_thousands_and_an_amount_in_units_is_computed_in_units():
    ws = _thousands()
    vid = _value(ws, "1,250,000", "עלות היתר", "cost", "cost")  # stated in units
    c = _compute(ws, f"V1 + {vid}", "סכום בדיקה")
    assert (c.value, c.scale, c.outcome.rescaled) == (Decimal("413550000"), 1, True)
    assert "הובאו ליחידות מלאות" in verify.computation_text(c, ws)
    for shown, ok in (("413.55 מיליון ₪", True), ("413,550,000 ₪", True), ("413,550 ₪", False),
                      ("1,662,300 ₪", False)):
        cited = _numbers_only(ws, f"הסכום הוא {shown} [{c.cid}].", question=THOUSANDS_QUESTION)
        assert (cited == []) is ok, (shown, cited)


def test_the_servers_qualifier_follows_a_result_shown_with_a_scale_word_after_the_word():
    _, applied = _qualified(_conditional_sum(), "סך העלות הוא 20.22 מיליון ₪ [C1].")
    assert "20.22 מיליון ₪ (תוצאה מותנית: הערכים V1, V2 אינם ודאיים) [C1]" in applied.answer_markdown


@pytest.mark.parametrize("raw, number, after", [
    ("שיעור הרווח לעלות יהיה כ־12.11% בתרחיש.", "12.11", "12.11%"),
    ("שיעור הרווח לעלות יהיה 12.11 % בתרחיש.", "12.11", "12.11 %"),
    ("הרווח יהיה 37,123.38 אלף ₪ בתרחיש.", "37,123.38", "37,123.38 אלף ₪"),
])
def test_a_qualifier_goes_after_the_number_with_its_percent_sign_or_currency(raw, number, after):
    """The server's qualifier never splits a number from its "%" ("12.11 (תוצאה מותנית: ...)%")."""
    from app.chat.verify import Unit, _after_number

    unit = Unit(0, raw, 0, len(raw))
    at = _after_number(unit, number)
    assert raw[:at].endswith(after)
