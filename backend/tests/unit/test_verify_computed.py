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
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
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


def test_an_intermediate_step_of_a_calculation_whose_input_the_unit_cites_is_not_an_unstated_number():
    # round 9 U12: "1.05" (1 + A1%) is a step of C1 whose inputs the unit cites; it is C1's, not a missing source number
    ws = _scenario()
    md = "העלויות הוגדלו במקדם 1.05 [V2][A1]."
    units = split_units(md)
    assert verify.bind_computations(units, ws, QUESTION) == {0: ["C1"]}
    assert units[0].ids == ["V2", "A1", "C1"]
    assert _numbers_only(ws, md) == []


def test_an_intermediate_step_with_no_input_of_its_calculation_cited_stays_unstated():
    ws = _scenario()
    _value(ws, "7,000", "שטח", "area", "income", unit="ILS")
    units = split_units("העלויות הוגדלו במקדם 1.05 [V3].")
    assert verify.bind_computations(units, ws, QUESTION) == {}
    (problem,) = deterministic(units, ws, QUESTION, meanings={0: []})
    assert problem.check == "unstated_number" and "1.05" in problem.reason


def test_a_wrong_step_is_not_bound_even_when_an_input_is_cited():
    ws = _scenario()
    units = split_units("העלויות הוגדלו במקדם 1.07 [V2][A1].")
    assert verify.bind_computations(units, ws, QUESTION) == {}


# --- a stated date is never an unstated number in a calculation unit (round 18 U2, KTD3, R8) ---------------------------------

def _dated_scenario(date_line: str | None = "למועד הקובע 15/04/2025", text: str = "סיכום השומה: ההכנסות והעלויות של הפרויקט.") -> Workspace:
    ws = _scenario()
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section="סיכום", location="עמוד 4",
                  kind="context", text=text)
    ws.sources["S1"].date_line = date_line
    return ws


@pytest.mark.parametrize("written", ["15.04.2025", "15/04/2025", "15.4.2025"])
def test_the_digits_of_a_valuation_date_the_calculations_source_states_are_not_unstated_in_a_calculation_unit(written):
    ws = _dated_scenario()
    assert _numbers_only(ws, f"נכון ל-{written}, הרווח בתרחיש יהיה 1,530,000 ₪ [C1][A1].") == []


def test_a_date_the_calculations_source_writes_in_its_text_is_not_unstated_in_a_calculation_unit():
    ws = _dated_scenario(date_line=None, text="סיכום השומה למועד 15/04/2025: ההכנסות והעלויות של הפרויקט.")
    assert _numbers_only(ws, "נכון ל-15.04.2025, הרווח בתרחיש יהיה 1,530,000 ₪ [C1][A1].") == []


@pytest.mark.parametrize("other", ["16.04.2025", "15.05.2025", "15.04.2024"])
def test_another_date_in_a_calculation_unit_is_still_unstated(other):
    ws = _dated_scenario()
    problems = _numbers_only(ws, f"נכון ל-{other}, הרווח בתרחיש יהיה 1,530,000 ₪ [C1][A1].")
    assert problems and problems[0].check == "computation_mismatch"


def test_an_invented_number_in_a_calculation_unit_is_still_removed_with_a_stated_date():
    ws = _dated_scenario()
    (problem,) = _numbers_only(ws, "נכון ל-15.04.2025, הרווח בתרחיש יהיה 1,530,000 ₪ על 7,777 מ\"ר [C1][A1].")
    assert problem.check == "computation_mismatch" and "7777" in problem.reason and "2025" not in problem.reason


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


@pytest.mark.parametrize("phrase, ok", [("1,530,000 ₪ ללא מע״מ", True), ("1,530,000 ₪ כולל מע״מ", False)])
def test_a_calculation_over_values_with_model_asserted_vat_keeps_its_vat_outcome(phrase, ok):
    """Pinned (round 11 U2, Scope Boundaries): ``_computation_vat`` reads the VAT recorded for the inputs and the VAT
    basis they share whatever its provenance. Tightening it to source-given VAT only is deferred (no failure was
    observed, and it risks removing calculation answers), so today's outcome is recorded, not changed."""
    ws = _scenario()
    for v in ws.values.values():
        v.provenance["vat"] = "model_asserted"
    problems = _numbers_only(ws, f"הרווח בתרחיש יהיה {phrase} [C1].")
    assert (problems == []) is ok, problems


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


def test_the_policy_asks_the_answer_to_state_its_calculation_basis_and_give_the_other_when_they_differ():
    """Round 16 U6 (R11, KTD8, AE5): the basis is a line of the agent's policy, not a phrase list in code. It names stating the
    basis, giving the other basis when a rounded conclusion and a computed sum differ, and never showing a computed result as
    a figure written in the document."""
    line = POLICY[POLICY.index("אמור באיזה בסיס חושבה התוצאה"):].split("\n  אם calculate מסרב")[0]
    assert "מסקנה מעוגלת שכתובה במסמך" in line and "סכום שחושב עכשיו" in line
    assert "תן גם את" in line and "ההפרש" in line
    assert "אל תציג תוצאה שחושבה כמספר שכתוב במסמך" in line
    assert POLICY.count("אמור באיזה בסיס חושבה התוצאה") == 1


def test_the_policy_asks_for_every_asked_item_of_the_asked_property_with_its_condition_and_citation():
    """Round 16: brevity never drops an asked item. The sentence sits in the answer-wording section (after the direct-answer
    line), names the item kinds, the attached condition/caveat/exception and the citation, and keeps another property's items out."""
    start = POLICY.index("ניסוח התשובה (answer_markdown):")
    section = POLICY[start:]
    sentence = section[section.index("כשהבקשה מבקשת פריטים מסוג מסוים"):].split("\n- ")[0]
    assert POLICY.count("כשהבקשה מבקשת פריטים מסוג מסוים") == 1
    assert "ערכים, שיעורים, תנאים, סיבות, חברי רשימה" in sentence
    assert "כל פריט כזה שהקטעים שקראת קובעים לאותו נכס" in sentence
    assert "תנאי, הסתייגות או חריג" in sentence and "מראה המקום שלו" in sentence
    assert "קיצור אינו מוריד פריט שנשאל" in sentence and "פריטים של נכס אחר אינם מתווספים" in sentence
    assert section.index("התשובה הישירה קודם, בקצרה") < section.index(sentence)


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


def test_a_computation_mismatch_is_removed_up_front_even_when_the_unit_holds_more_content():
    # only a number no source states is left to the judge to split; a result that is not the computation's is not
    report, _ = _verify(_scenario(), "הרווח בתרחיש יהיה 1,600,000 ₪ [C1], וכל הנתונים האלה נבדקו מול המסמכים שנקראו בתור הזה.")
    (decision,) = report.removals()
    assert (decision.failure_kind, decision.check) == ("wrong_calculation", "computation_mismatch")
    assert 0 not in report.verdicts and report.units[0].unstated == []


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


def _composed(ws: Workspace, markdown: str, rule=lambda text: "supported"):
    """The answer as the composer finishes it: the body with its short tags, then the conditional note (R23, KTD10)."""
    from app.chat import coverage

    answer = _answer(markdown)
    report = verify_answer(_judge(rule), answer, ws, COST_QUESTION, [])
    edited, _ = coverage.compose_components(ws, answer, report)
    return report, edited.doc.text, [(edited.doc.text[s.start:s.end], s.kind) for s in edited.doc.spans()]


def _second_conditional(ws: Workspace, expression: str = "V1 - V2", note: str | None = None) -> str:
    """A second calculation conditional on the same inputs, as ``tool_calculate`` records it."""
    from app.chat.tools import MSG_UNCERTAIN_INPUTS

    c = _compute(ws, expression, "הפרש העלויות")
    c.outcome.conditional.append(note or MSG_UNCERTAIN_INPUTS.format(ids="V1, V2") + " (V1: התא לא אושר)")
    c.uncertain = ["V1", "V2"]
    return c.cid


def test_a_result_of_a_conditional_calculation_shown_without_a_hedge_is_kept_with_the_servers_qualifier():
    ws = _conditional_sum()
    seen: list = []
    report, applied = _qualified(ws, "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].", seen=seen)
    assert not report.removed_units() and report.removals() == [], report.problems_text()
    assert report.ok  # the server writes the qualifier itself: no repair round for it alone
    (p,) = [p for p in report.problems if p.annotatable]
    assert p.conditional and p.cite == "C1" and p.number == "20,220,000"
    # only the short tag stays by the number; the reason is said once, in the note (R23)
    assert "20,220,000 ₪ [C1] (תוצאה מותנית)." in applied.answer_markdown, applied.answer_markdown
    assert "אינם ודאיים" not in applied.answer_markdown
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
    _, text, _ = _composed(_conditional_sum(("V2",)), "סך העלות הוא 20,220,000 ₪ [C1].")
    assert "20,220,000 ₪ [C1] (תוצאה מותנית)." in text
    assert "התוצאה 20,220,000 ₪ מותנית: הערך «חניון תת-קרקעי» (4,620,000) אינו ודאי." in text


def test_a_conditional_calculation_on_a_justified_mix_is_qualified_with_the_mix_it_rests_on():
    ws = _scenario(cost_vat="unknown")
    answer = _answer("לפי הנחתך [A1], הרווח בתרחיש יהיה 1,530,000 ₪ [C1].")
    report = verify_answer(_judge(), answer, ws, QUESTION, [])
    assert not report.removed_units(), report.problems_text()
    from app.chat import coverage

    text = coverage.compose_components(ws, answer, report)[0].doc.text
    body, note = text.split("\n\n")
    assert "1,530,000 ₪ [C1] (תוצאה מותנית)." in body and "לפי הצדקה" not in body
    assert note.startswith("התוצאה 1,530,000 ₪ מותנית: לפי הצדקה לערבוב נתונים שאינם תואמים — מע״מ:"), note


def test_a_unit_that_already_says_its_result_is_conditional_gets_only_the_short_reason():
    _, body, _ = _composed(_conditional_sum(), "בכפוף לאימות הקלטים, התוצאה מותנית: 20,220,000 ₪ [C1].")
    first, note = body.split("\n\n")
    assert first == "בכפוף לאימות הקלטים, התוצאה מותנית: 20,220,000 ₪ [C1]."  # the unit says it: no tag, no copy
    assert note == "התוצאה 20,220,000 ₪ מותנית: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים."


def test_a_wrong_number_for_a_conditional_calculation_is_still_removed_as_a_wrong_calculation():
    report, applied = _qualified(_conditional_sum(), "עלות הבנייה העילית והחניון יחד היא 20,230,000 ₪ [C1].")
    assert [(d.failure_kind, d.check) for d in report.removals()] == [("wrong_calculation", "computation_mismatch")]
    assert "תוצאה מותנית" not in applied.answer_markdown
    assert report.conditional_note() == ""  # a removed result is covered by no note


def test_a_conditional_result_the_judge_finds_wrong_is_still_removed_and_not_qualified():
    report, applied = _qualified(_conditional_sum(), "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].",
                                 rule=lambda text: "unsupported")
    assert [d.failure_kind for d in report.removals()] == ["wrong_calculation"]
    assert "תוצאה מותנית" not in applied.answer_markdown and report.counts()["conditional"] == 0


def test_a_result_of_a_calculation_that_is_not_conditional_gets_no_qualifier():
    report, applied = _qualified(_conditional_sum(()), "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].")
    assert not report.problems, report.problems_text()
    assert "מותנית" not in applied.answer_markdown and report.counts()["correctness"] == "verified"
    assert report.conditional_note() == ""


def test_a_conditional_result_bound_to_a_unit_that_cites_only_its_inputs_is_qualified_too():
    _, text, _ = _composed(_conditional_sum(), "סך העלות הוא 20,220,000 ₪ [V1][V2].")
    assert "20,220,000 ₪ [V1][V2][C1] (תוצאה מותנית)." in text
    assert text.endswith("התוצאה 20,220,000 ₪ מותנית: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים.")


def test_r23_two_results_of_one_calculation_carry_a_short_tag_each_and_one_caveat_names_them():
    """The long justification is said once, after the body (R23, AE: it was repeated after every number)."""
    ws = _conditional_sum()
    markdown = ("עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].\n"
                "כלומר סך העלות הכולל הוא 20,220,000 ₪ [C1], ובמיליונים 20.22 מיליון ₪ [C1].")
    report, text, spans = _composed(ws, markdown)
    body, note = text.split("\n\n")
    # a tag by the result of each sentence that shows it (nothing looks certain), and the reason is not repeated
    assert body.count("(תוצאה מותנית)") == 2 and "אינם ודאיים" not in body
    assert text.count("אינם ודאיים") == 1 and text.count("התוצאה 20,220,000 ₪ מותנית") == 1
    assert note == "התוצאה 20,220,000 ₪ מותנית: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים."
    assert [k for t, k in spans if k == "conditional_note"] == ["conditional_note"]
    assert [t for t, k in spans if k == "conditional_note"] == ["\n\n" + note]
    assert report.counts()["conditional"] == 2  # the units are still counted as conditional


def test_r23_calculations_with_the_same_reason_share_one_line_that_names_all_their_results():
    ws = _conditional_sum()
    other = _second_conditional(ws)
    assert other == "C2"
    markdown = "העלות הכוללת היא 20,220,000 ₪ [C1].\nההפרש בין העלויות הוא 10,980,000 ₪ [C2]."
    _, text, spans = _composed(ws, markdown)
    body, note = text.split("\n\n")
    assert note == "התוצאות 20,220,000 ₪ ו־10,980,000 ₪ מותנות: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים."
    assert body.count("(תוצאה מותנית)") == 2 and text.count("אינם ודאיים") == 1


def test_r23_calculations_with_different_reasons_get_a_line_each():
    ws = _conditional_sum()
    _second_conditional(ws, note="בסיס שטח: «עיקרי» מול «פלדלת»")
    ws.computations["C2"].uncertain = []  # its reason is the mix alone
    markdown = "העלות הכוללת היא 20,220,000 ₪ [C1].\nההפרש בין העלויות הוא 10,980,000 ₪ [C2]."
    _, text, _ = _composed(ws, markdown)
    note = text.split("\n\n")[1]
    assert note.split("\n")[0] == "התוצאה 20,220,000 ₪ מותנית: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים."
    assert note.split("\n")[1].startswith("התוצאה 10,980,000 ₪ מותנית: ") and "בסיס שטח" in note.split("\n")[1]


def test_r23_a_conditional_note_is_after_the_body_and_before_the_gap_paragraph():
    """Order: body, conditional note, gap paragraph (R23)."""
    from app.chat import coverage
    from app.chat.verify import TurnRequirements

    ws = _conditional_sum()
    ws.searches.append("עלות")
    turn = TurnRequirements()
    from app.chat import request as request_analysis

    items, _ = request_analysis.freeze([request_analysis.Component(text="עלות הבנייה והחניון", kind="calculation"),
                                        request_analysis.Component(text="דמי הניהול", kind="information")], ["?"])
    turn.adopt(items, "analysis")
    answer = _answer("עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].")

    def judge(input: str) -> dict:
        units = {int(i) for i in re.findall(r'<unit index="(\d+)"', input)}
        reqs = [{"id": rid, "text": "", "calculation": False, "status": "full" if rid == "N1" else "missing",
                 "units": [0] if rid == "N1" else [], "related": ["C1"] if rid == "N1" else ["H1"], "reason": "בדיקה",
                 "absent": []} for rid in re.findall(r'<requirement id="(N[\d.]+)"', input)]
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": reqs}

    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: judge(input), repeat=True)
    report = verify_answer(p, answer, ws, COST_QUESTION, [], requirements=turn)
    final, _ = coverage.state_components(ws, answer, report, turn)
    parts = final.answer_markdown.split("\n\n")
    assert len(parts) == 3, parts
    assert "(תוצאה מותנית)" in parts[0] and parts[1].startswith("התוצאה 20,220,000 ₪ מותנית") and "דמי הניהול" in parts[2]


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
    assert "20.22 מיליון ₪ [C1] (תוצאה מותנית)" in applied.answer_markdown


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


# --- a number derived from cited, grounded values by a simple formula (round 13 U9, KTD9, R12, AE5) ----------------------------

def _ratio(ws: Workspace, written: str, label: str, *, unit: str = "ratio", kind: str = "coefficient",
           version: uuid.UUID | None = None) -> str:
    """A value of the invented appraisal unit ``version`` (a coefficient by default), as the registered record holds it."""
    vid = f"V{len(ws.values) + 1}"
    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(),
                                version or uuid.uuid4(), None, "שומת הדגמה", "טבלת מקדמים", label, kind, unit, "none",
                                "not_applicable", "", SUBJECT, "appraiser_determination",
                                {"unit": "source", "vat": "source"}, {"quote": f"{label} {written}"},
                                f"{label} {written}")
    return vid


def _expr(expression: str) -> str:
    """An expression as the calculator writes it (``calc.render``: "1 − V1")."""
    return calc.render(calc.parse(expression), lambda i: i)


def _derived(ws: Workspace) -> list[calc.Computation]:
    return [c for c in ws.computations.values() if c.result_kind == "derived"]


def test_a_reduction_that_is_one_minus_the_cited_coefficient_is_derived_registered_and_kept():
    """AE5: "מקדם 0.8 — הפחתה של 20%" cites the 0.8 cell; the 20% is 1 - 0.8, so the bullet stays and the judge reads the
    server's computation beside the cited cell."""
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    seen: list = []
    report, applied = _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].", seen=seen)
    (c,) = _derived(ws)
    assert (c.cid, c.expression, c.value, c.result_kind) == ("C1", _expr("1 - V1"), Decimal("0.2"), "derived")
    assert c.leaves == ["V1"] and c.sources == ["S1"]
    assert not report.removed_units() and report.ok, report.problems_text()
    assert applied.answer_markdown == "מקדם 0.8 — הפחתה של 20% [V1][C1]."
    shown = "\n".join(i for _, i in seen)
    assert '<source id="C1"' in shown and "נוסחה:" in shown and _expr("1 - V1") in shown and "נגזר" in shown


def test_a_value_whose_unit_is_ambiguous_is_no_operand_of_a_derived_computation():
    """Round 15 review (KTD9): ``calculate`` refuses a value whose unit its source leaves ambiguous, so the verifier's own
    derivation (``derive_numbers``) does not take one as an operand either: the same claim derives from an unflagged value."""
    claim = "מקדם 0.8 — הפחתה של 20% [V1]."
    clear = Workspace(ctx=None)
    _ratio(clear, "0.8", "מקדם הדגמה")
    _verify(clear, claim)
    assert [c.expression for c in _derived(clear)] == [_expr("1 - V1")]
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    ws.ambiguous_units["V1"] = "the source leaves the unit open"
    report, _ = _verify(ws, claim)
    assert _derived(ws) == []
    assert verify._derivations(ws, ["V1"], verify.Shown("20", True, 1, 0, 0), "הפחתה של 20%") == []
    assert [p.check for p in report.problems if p.removes_unit or p.resolution == "qualifier"] == ["unstated_number"]


@pytest.mark.parametrize("claim", ["מקדם 0.8 — הפחתה של 20%", "מקדם 0.8 — הפחתה של 20 אחוז", "מקדם 0.8 — הנחה של 0.2",
                                   "מקדם 0.8, בהנחה של 20 נקודות אחוז"])
def test_a_reduction_is_derived_in_every_written_form_of_the_complement(claim):
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    report, _ = _verify(ws, claim + " [V1].")
    assert [c.expression for c in _derived(ws)] == [_expr("1 - V1" if "נקודות" not in claim else "(1 - V1) * 100")]
    assert not report.removed_units()


def test_the_derived_computation_reaches_the_judge_as_the_units_own_evidence_and_the_unit_is_still_judged():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    seen: list = []
    report, _ = _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].", rule=lambda t: "unsupported", seen=seen)
    assert seen, "the unit was not sent to the judge"
    assert report.removed_units() == {0}  # the judge's verdict still decides: the derivation only settles the number check
    assert report.removals()[0].check == "judge"


def test_an_addition_is_derived_only_behind_addition_wording():
    ws = Workspace(ctx=None)
    _ratio(ws, "1.2", "מקדם הדגמה")
    _verify(ws, "מקדם 1.2 — תוספת של 20% [V1].")
    assert [c.expression for c in _derived(ws)] == [_expr("V1 - 1")]


@pytest.mark.parametrize("claim", [
    "מקדם 0.8 — תוספת של 20% [V1].",  # addition wording on a coefficient below 1: negative
    "מקדם 0.8 — פרמיה של 20% [V1].",
    "מקדם 0.8 — שיעור של 20% [V1].",  # no wording at all
    "מקדם 0.8 — הפחתה של 25% [V1].",  # the wrong complement
    "מקדם 1.2 — הפחתה של 20% [V1].",  # 1 - 1.2 is not a reduction
])
def test_the_wording_gate_and_the_sign_keep_a_coincidence_from_deriving(claim):
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8" if "1.2" not in claim else "1.2", "מקדם הדגמה")
    report, _ = _verify(ws, claim)
    assert _derived(ws) == []
    assert [p.check for p in report.problems if p.removes_unit or p.resolution == "qualifier"] == ["unstated_number"]


def test_the_percent_form_of_the_same_cited_value_is_derived_in_both_directions():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    _ratio(ws, "20", "שיעור הדגמה", unit="percent", kind="rate")
    _verify(ws, "מקדם 80% [V1].")
    _verify(ws, "שיעור 0.2 [V2].")
    assert sorted(c.expression for c in _derived(ws)) == sorted([_expr("V1 * 100"), _expr("V2 / 100")])


def test_the_percent_form_is_not_a_license_for_a_plain_number_equal_to_a_hundred_times_a_value():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    report, _ = _verify(ws, "שטח הדירה 80 מ״ר [V1].")
    assert _derived(ws) == [] and report.removed_units() == {0}


@pytest.mark.parametrize("wording, expression, shown", [
    ("סה״כ", "V1 + V2", "350"), ("סך הכל", "V1 + V2", "350"), ("הפרש", "V2 - V1", "150"),
    ("מכפלת", "V1 * V2", "25,000"), ("ביחס ל", "V1 / V2", "0.4"),
])
def test_two_operand_forms_derive_only_when_the_claim_names_the_relation_and_both_come_from_one_unit(
        wording, expression, shown):
    unit = uuid.uuid4()
    ws = Workspace(ctx=None)
    _ratio(ws, "100", "רכיב א", unit="other", kind="cost", version=unit)
    _ratio(ws, "250", "רכיב ב", unit="other", kind="cost", version=unit)
    named = f"{wording} {shown} [V1][V2]."
    unnamed = f"הערך {shown} [V1][V2]."
    _verify(ws, unnamed)
    assert _derived(ws) == []  # a number equal by chance to a sum of two cited values derives nothing
    report, _ = _verify(ws, named)
    assert [c.expression for c in _derived(ws)] == [_expr(expression)] and not report.removed_units()


def test_two_operands_of_different_appraisal_units_are_never_combined():
    ws = Workspace(ctx=None)
    _ratio(ws, "100", "רכיב א", unit="other", kind="cost")
    _ratio(ws, "250", "רכיב ב", unit="other", kind="cost")  # another unit of the file set: another version
    report, _ = _verify(ws, "סה״כ 350 [V1][V2].")
    assert _derived(ws) == [] and report.removed_units() == {0}


def test_a_value_the_unit_does_not_cite_is_never_an_operand():
    unit = uuid.uuid4()
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם א", version=unit)
    _ratio(ws, "0.7", "מקדם ב", version=unit)
    report, _ = _verify(ws, "מקדם 0.8 — הפחתה של 30% [V1].")
    assert _derived(ws) == [] and report.counts()["edited"] == 1


def test_a_second_verification_of_the_same_answer_reuses_the_derived_computation():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].")
    _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].")
    assert len(_derived(ws)) == 1 and len(ws.computations) == 1


def test_a_derived_result_lends_its_number_only_to_a_unit_that_cites_its_inputs():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    _ratio(ws, "0.9", "מקדם אחר")
    _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].")
    units = split_units("מקדם אחר 0.9 [V2] — ובנפרד 20% [V2].")
    assert verify.bind_computations(units, ws, QUESTION) == {}


def test_a_derived_result_backs_no_number_of_a_unit_that_cites_nothing():
    """Round 13 U9 (KTD9): the model's own C# may back an uncited unit; a server-derived one only backs a unit that cites
    (or is bound to) one of its inputs."""
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    _verify(ws, "מקדם 0.8 — הפחתה של 20% [V1].")
    assert [c.value for c in _derived(ws)] == [Decimal("0.2")]
    (problem,) = _numbers_only(ws, "שיעור הוצאות ייעודי של 20% בלבד.")
    assert problem.check == "unstated_number" and "20" in problem.unstated
    assert _numbers_only(_scenario(), "הרווח בתרחיש יהיה 1,530,000 ₪.") == []  # a model-made C# keeps the old behavior


# --- round 14 U4: the reduction a coefficient implies, registered through the same derivation (R8, KTD6, AE6) -----------------------

def test_the_reduction_of_a_coefficient_is_registered_as_one_minus_it_once():
    """``derive_reduction`` is ``_derived_computation`` for ``1 − Vn`` shown as the fraction it is: a C# of result kind
    ``derived`` whose only input is the coefficient, reused on the second call."""
    ws = Workspace(ctx=None)
    _ratio(ws, "0.9", "מקדם הדגמה")
    cid = verify.derive_reduction(ws, "V1")
    c = ws.computations[cid]
    assert (c.result_kind, c.expression, c.value, c.leaves) == ("derived", _expr("1 - V1"), Decimal("0.1"), ["V1"])
    assert [i["id"] for i in c.inputs] == ["V1"]
    assert verify.derive_reduction(ws, "V1") == cid and len(ws.computations) == 1


def test_the_reduction_reuses_the_percent_form_the_verifier_already_derived():
    ws = Workspace(ctx=None)
    _ratio(ws, "0.8", "מקדם הדגמה")
    _verify(ws, "מקדם 0.8, בהנחה של 20 נקודות אחוז [V1].")
    (existing,) = _derived(ws)
    assert existing.expression == _expr("(1 - V1) * 100")
    assert verify.derive_reduction(ws, "V1") == existing.cid and len(ws.computations) == 1


def test_no_reduction_is_derived_where_the_calculator_refuses_the_coefficient():
    ws = Workspace(ctx=None)
    _ratio(ws, "90", "מקדם באחוזים", unit="percent")  # 90 percent: a literal 1 beside a percent does not combine
    _ratio(ws, "1.2", "מקדם מעל אחד")  # one minus it is negative: a reduction is a magnitude
    assert verify.derive_reduction(ws, "V1") is None and verify.derive_reduction(ws, "V2") is None
    assert verify.derive_reduction(ws, "V99") is None and ws.computations == {}


# --- round 16 U6: a result sentence is bound to its conditional calculation without citing it (R12, KTD7, AE5) -----------------

AVERAGE_QUESTION = "מה הערך הממוצע לדירה?"
_COMPARABLES = ("| דירה | מחיר |\n| --- | --- |\n| א | 1,234,000 |\n| ב | 1,301,500 |\n| ג | 1,299,999 |")
_AVERAGE_ROW = "\n| ממוצע | 1,278,500 |"  # a row of the same table that writes the result of the calculation


def _average(uncertain: tuple[str, ...] = ("V3",), table_writes_average: bool = False) -> Workspace:
    """S1 a comparables table (its own average row when ``table_writes_average``) and S2 the passage that dates the
    appraisal; S3 a table of another building, that shares no input with C1; V1–V3 the three prices read from S1 and C1 their mean (1,278,499.67 ₪),
    conditional on the price read uncertainly (V3) as ``tools.tool_calculate`` records it."""
    from app.chat.tools import MSG_UNCERTAIN_INPUTS

    ws = Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": AVERAGE_QUESTION, "current": True}]
    for title, text in (("טבלת מקרים להשוואה", _COMPARABLES + (_AVERAGE_ROW if table_writes_average else "")),
                        ("פרק מבוא", "הערכת השווי נעשתה למועד הקובע 15/04/2025."),
                        ("טבלת מקרים בבניין אחר", "| דירה | מחיר |\n| --- | --- |\n| ד | 987,000 |\n| ה | 1,020,000 |\n| ו | 1,278,500 |")):
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title=title, section="סיכום", location="עמוד 4",
                      kind="context", text=text)
    ws.sources["S2"].date_line = "למועד הקובע 15/04/2025"
    for written, label in (("1,234,000", "דירה א"), ("1,301,500", "דירה ב"), ("1,299,999", "דירה ג")):
        _value(ws, written, label, "price", "comparable", vat="included")
    c = _compute(ws, "mean(V1, V2, V3)", "ממוצע המחירים לדירה")
    if uncertain:
        c.outcome.conditional.append(MSG_UNCERTAIN_INPUTS.format(ids=", ".join(uncertain)) + " (V3: התא לא אושר)")
        c.uncertain = list(uncertain)
    return ws


def _bound(ws: Workspace, markdown: str):
    units = split_units(markdown)
    return units, verify.bind_computations(units, ws, AVERAGE_QUESTION)


SHOWN_RESULT = ["1,278,500 ₪", "כ־1,278,500 ₪", "כ-1,278,500 ₪", "בערך 1,278,500 ₪", "כ־1.28 מיליון ₪", "1.28 מיליון ₪",
                "1,278.5 אלף ₪"]


@pytest.mark.parametrize("shown", SHOWN_RESULT)
def test_ae5_a_unit_showing_a_conditional_results_value_without_citing_it_is_bound_to_it(shown):
    ws = _average()
    units, bound = _bound(ws, f"לפי החישוב, הערך הממוצע הוא {shown} לדירה [S1].")
    assert bound == {0: ["C1"]}, shown
    assert units[0].ids == ["S1", "C1"]
    (note,) = verify.conditional_notes(units[0], ws)
    assert note[0] == "C1" and "V3" in note[2]


@pytest.mark.parametrize("shown", SHOWN_RESULT)
def test_ae5_a_result_the_cited_table_also_writes_is_still_bound_to_the_conditional_calculation_it_is_the_result_of(shown):
    """The table the calculation's inputs were read from writes the average as well (the calculation reproduces it): the
    sentence shows that calculation's result about the same data, so the server's qualifier is written (R12)."""
    ws = _average(table_writes_average=True)
    units, bound = _bound(ws, f"לפי החישוב, הערך הממוצע הוא {shown} לדירה [S1].")
    assert bound == {0: ["C1"]}, shown
    assert [n[0] for n in verify.conditional_notes(units[0], ws)] == ["C1"]


def test_ae5_the_full_sentence_with_a_date_and_vat_citing_the_table_and_the_date_line_gets_the_servers_qualifier():
    ws = _average(table_writes_average=True)
    seen: list = []
    markdown = "לפי החישוב, הערך הממוצע הוא כ־1,278,500 ₪ לדירה (כ־1.28 מיליון ₪), כולל מע\"מ, נכון ל־15/04/2025 [S1][S2]."
    report, applied = _verify(ws, markdown, seen=seen, question=AVERAGE_QUESTION)
    assert not report.removed_units(), report.problems_text()
    (p,) = [p for p in report.problems if p.conditional]
    assert p.cite == "C1" and "V3" in p.annotation
    assert "[S1][S2][C1]" in applied.answer_markdown and "תוצאה מותנית" in applied.answer_markdown
    judged = "\n".join(i for _, i in seen)
    assert '<server_qualifier unit="0">' in judged and "V3" in judged
    assert report.counts()["conditional"] == 1


def test_ae5_a_judge_that_removes_a_conditional_result_shown_without_a_qualifier_keeps_it_now_that_the_qualifier_is_shown():
    """The incident: the judge removed the unit as ``uncertain_reading`` ("presented without reservation") because it saw no
    ``<server_qualifier>``. It is shown now (the unit is bound to C1), so the judge, who is told that a conditional result with
    a server qualifier is no reason to remove, keeps it."""
    ws = _average(table_writes_average=True)
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">', input):
            qualified = f'<server_qualifier unit="{m.group(1)}">' in input
            out.append({"index": int(m.group(1)), "verdict": "supported" if qualified else "unsupported",
                        "reason": "בדיקה", "failure": "none" if qualified else "uncertain_reading", "supported_by": []})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    answer = _answer("לפי החישוב, הערך הממוצע הוא כ־1,278,500 ₪ לדירה [S1].")
    report = verify_answer(p, answer, ws, AVERAGE_QUESTION, [])
    assert not report.removed_units(), report.problems_text()
    assert "תוצאה מותנית" in report.apply(answer).answer_markdown


def test_ae5_a_specific_result_is_bound_though_the_unit_cites_only_other_sources():
    """The incident: the sentence cites an unrelated comparables table and a date line (none of them the calculation's sources
    or inputs) and shows the result of the conditional calculation at full precision — a number of seven digits that no other
    item equals by chance — so the server's qualifier is written and the judge, who removes a sentence without one, keeps it."""
    ws = _average()
    markdown = "לפי החישוב, הערך הממוצע הוא כ־1,278,500 ₪ לדירה, נכון ל־15/04/2025 [S2][S3]."
    units, bound = _bound(ws, markdown)
    assert bound == {0: ["C1"]} and units[0].ids == ["S2", "S3", "C1"]
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">', input):
            qualified = f'<server_qualifier unit="{m.group(1)}">' in input
            out.append({"index": int(m.group(1)), "verdict": "supported" if qualified else "unsupported",
                        "reason": "בדיקה", "failure": "none" if qualified else "uncertain_reading", "supported_by": []})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    answer = _answer(markdown)
    report = verify_answer(p, _answer(markdown), ws, AVERAGE_QUESTION, [])
    assert not report.removed_units(), report.problems_text()
    assert "תוצאה מותנית" in report.apply(answer).answer_markdown
    assert verify._specific("1,278,500") and verify._specific("1,278.5") and not verify._specific("1,300,000")


def test_ae5_a_three_digit_number_equal_by_chance_to_a_conditional_result_is_not_bound_without_a_shared_source():
    from app.chat.tools import MSG_UNCERTAIN_INPUTS

    ws = Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": AVERAGE_QUESTION, "current": True}]
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="טבלת שטחים", section="סיכום", location="עמוד 2",
                  kind="context", text="| יחידה | שטח |\n| א | 120 |\n| ב | 130 |")
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="נספח אחר", section="סיכום", location="עמוד 9",
                  kind="context", text="בקומה העליונה 250 מ\"ר של מרפסות.")
    _value(ws, "120", "שטח א", "area", "income", unit="sqm")
    _value(ws, "130", "שטח ב", "area", "income", unit="sqm")
    c = _compute(ws, "V1 + V2", "סך השטחים")
    c.outcome.conditional.append(MSG_UNCERTAIN_INPUTS.format(ids="V2") + " (V2: התא לא אושר)")
    c.uncertain = ["V2"]
    units = split_units("בקומה העליונה יש 250 מ\"ר של מרפסות [S2].")
    assert verify.bind_computations(units, ws, AVERAGE_QUESTION) == {} and units[0].ids == ["S2"]
    assert verify.conditional_notes(units[0], ws) == []
    # the same number in a sentence that cites the calculation's own source is its result
    units = split_units("סך השטחים הוא 250 מ\"ר [S1].")
    assert verify.bind_computations(units, ws, AVERAGE_QUESTION) == {0: ["C1"]}


def test_ae5_a_number_the_unit_presents_as_written_by_the_report_is_not_bound_to_a_calculation():
    ws = _average(table_writes_average=True)
    units, bound = _bound(ws, "השומה מציינת שהערך הממוצע הוא 1,278,500 ₪ לדירה [S1].")
    assert bound == {} and units[0].ids == ["S1"]


def test_ae5_a_result_of_a_calculation_that_is_not_conditional_needs_no_binding_when_the_table_writes_it():
    ws = _average(uncertain=(), table_writes_average=True)
    units, bound = _bound(ws, "הערך הממוצע הוא 1,278,500 ₪ לדירה [S1].")
    assert bound == {} and units[0].ids == ["S1"]


def test_ae5_a_date_or_a_count_that_happens_to_equal_nothing_of_the_result_is_left_alone():
    ws = _average(table_writes_average=True)
    units, bound = _bound(ws, "הערכת השווי נעשתה למועד הקובע 15/04/2025 [S2].")
    assert bound == {} and units[0].ids == ["S2"]


# --- round 18 U7: a conditional note names the values in words and stands after the phrase (KTD6, R12) ---------------------------

def test_the_conditional_note_names_the_result_and_its_uncertain_values_in_words_never_by_internal_ids():
    _, text, _ = _composed(_conditional_sum(), "סך העלות הוא 20,220,000 ₪ [C1].")
    note = text.split("\n\n")[1]
    assert note == "התוצאה 20,220,000 ₪ מותנית: הערכים «בנייה עילית» (15,600,000) ו־«חניון תת-קרקעי» (4,620,000) אינם ודאיים."
    assert not re.search(r"\b[VCMA]\d+\b", note)
    _, text, _ = _composed(_conditional_sum(("V2",)), "סך העלות הוא 20,220,000 ₪ [C1].")
    assert text.split("\n\n")[1] == "התוצאה 20,220,000 ₪ מותנית: הערך «חניון תת-קרקעי» (4,620,000) אינו ודאי."


def test_calculations_that_share_a_reason_are_named_together_by_the_numbers_they_show_in_words():
    ws = _conditional_sum()
    _second_conditional(ws)
    markdown = "העלות הכוללת היא 20,220,000 ₪ [C1].\nההפרש בין העלויות הוא 10,980,000 ₪ [C2]."
    _, text, _ = _composed(ws, markdown)
    note = text.split("\n\n")[1]
    assert note.startswith("התוצאות 20,220,000 ₪ ו־10,980,000 ₪ מותנות: ") and not re.search(r"\b[VCMA]\d+\b", note)


def test_a_result_the_unit_shows_no_number_of_is_named_by_the_calculations_label():
    ws = _conditional_sum()
    _, text, _ = _composed(ws, "העלות הכוללת של הבנייה והחניון נבדקה [C1].")
    note = text.split("\n\n")[-1]
    assert note.startswith("התוצאה «עלות הבנייה והחניון» מותנית: ") and "C1" not in note


def test_the_short_tag_of_a_conditional_result_stands_after_its_sentence_not_inside_the_phrase():
    ws = _conditional_sum()
    _, text, _ = _composed(ws, "העלות הכוללת היא 20,220,000 ₪ [C1] לפי חישוב המערכת.")
    body = text.split("\n\n")[0]
    assert body == "העלות הכוללת היא 20,220,000 ₪ [C1] לפי חישוב המערכת (תוצאה מותנית)."
    assert "₪ (תוצאה מותנית)" not in body


def test_the_short_tag_of_a_conditional_result_in_a_table_row_stays_in_its_cell():
    ws = _conditional_sum()
    markdown = "| פריט | סכום |\n| --- | --- |\n| עלות כוללת | 20,220,000 ₪ [C1] |"
    _, text, _ = _composed(ws, markdown)
    assert "| עלות כוללת | 20,220,000 ₪ (תוצאה מותנית) [C1] |" in text
