"""The calculator's expression language and its operation-dependent compatibility (U10, KTD10, R15–R18).

Pure tests: operands are built directly, as the tools build them from verified values (V#), user assumptions
(A#), stored measurements (M#) and earlier results (C#). Synthetic values only. Round 7 U7 (KTD8, KTD9, R23–R25):
the literals stay structural, a written rate has a rounding interval, and verification flags a result built from a
rounded rate beside a stated amount (``input_choice``) and a scenario number nobody gave (``assumption``)."""

from decimal import Decimal

import pytest

from app.chat import calc
from app.chat.calc import CalcError, operand

TABLE = ("v1", 0)  # one synthetic table: its total row and its stage rows


def ops(*items: calc.Operand) -> dict[str, calc.Operand]:
    return {o.id: o for o in items}


def run(expression: str, operands: dict, justification: str | None = None) -> calc.Outcome:
    return calc.evaluate(calc.parse(expression), operands, justification)


INCOME = operand("V1", "12450000", "ILS", kind="income", role="income", subject="פרויקט הדגמה", total=True,
                 table=TABLE, vat="excluded")
COST = operand("V2", "10400000", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה", total=True, table=TABLE,
               vat="excluded")
RISE = operand("A1", "5", "percent", assumption=True)


# --- the parser: a whitelist of nodes, never evaluation of code ------------------------------------------------

@pytest.mark.parametrize("expression", [
    "V1 - V2*(1+A1%)", "V1 − V2 × (1 + A1%)", "C1 ÷ V1", "mean(M1, M2, M3)", "sum(V1,V2) / 12", "V1 * 100",
    "(V1 - V2) / V2 * 100", "count(M1)", "max(V1, V2) - min(V1, V2)", " V1 + V2 ",
])
def test_the_grammar_accepts_ids_operators_percent_aggregates_and_structural_literals(expression):
    calc.parse(expression)


@pytest.mark.parametrize("expression, reason", [
    ("x + V1", "שם"),
    ("V1.value", "מאפיין"),
    ("__import__('os')", "שם"),
    ("eval(V1)", "פונקציה"),
    ("open(V1)", "פונקציה"),
    ("V1 ** 2", "חזקה"),
    ("V1 ^ 2", "חזקה"),
    ("V1 + 5", "קבוע"),
    ("V1 * 1.05", "קבוע"),
    ("-V1", "מינוס"),
    ("V1 // V2", "//"),
    ("V1 = 3", "="),
    ("mean(V1 + V2)", "מזהים"),
    ("mean()", "מזהים"),
    ("", "ריק"),
    ("V1 V2", "אופרטור"),
    ("(V1 + V2", "סוגר"),
    ("V1 + V2)", "סוגר"),
    ("V1[0]", "["),
    ("lambda: V1", "שם"),
])
def test_the_grammar_rejects_names_attributes_calls_powers_and_other_literals(expression, reason):
    with pytest.raises(CalcError) as e:
        calc.parse(expression)
    assert reason in str(e.value)


def test_the_formula_is_rendered_with_ids_and_with_labels():
    node = calc.parse("V1-V2*(1+A1%)")
    assert calc.render(node, lambda i: i) == "V1 − V2 × (1 + A1%)"
    names = {"V1": "סה״כ הכנסות", "V2": "סה״כ עלויות", "A1": "עליית עלויות"}
    assert calc.render(node, lambda i: f"«{names[i]}»") == "«סה״כ הכנסות» − «סה״כ עלויות» × (1 + «עליית עלויות»%)"
    assert calc.render(calc.parse("(V1 - V2) / V2"), lambda i: i) == "(V1 − V2) ÷ V2"


# --- AE4: a scenario on totals, chained, at full precision -----------------------------------------------------

def test_ae4_income_minus_raised_cost_and_the_ratio_on_it_keep_full_precision():
    profit = run("V1 - V2*(1+A1%)", ops(INCOME, COST, RISE))
    assert profit.value == Decimal("1530000") and profit.dims == (("ILS", 1),)
    assert profit.kind == "profit" and profit.conditional == [] and profit.assumptions == ["A1"]
    chained = calc.result_operand("C1", profit)
    ratio = run("C1 / V1", ops(chained, INCOME))
    assert ratio.value == Decimal(1530000) / Decimal(12450000)  # never rounded on the way
    assert len(str(ratio.value)) > 20 and ratio.dims == () and ratio.kind == "ratio"
    assert ratio.assumptions == ["A1"]  # a scenario stays one through the chain
    percent = run("C1 / V1 * 100", ops(chained, INCOME))
    assert percent.dims == (("%", 1),) and percent.value == ratio.value * 100


def test_intermediate_steps_are_kept_for_verification():
    profit = run("V1 - V2*(1+A1%)", ops(INCOME, COST, RISE))
    values = {text: value for text, value in profit.steps}
    assert values["1 + A1%"] == Decimal("1.05") and values["V2 × (1 + A1%)"] == Decimal("10920000")


# --- AE5 and R17: compatibility depends on the operation ------------------------------------------------------

def test_ae5_a_sum_of_a_total_row_with_its_own_component_rows_is_refused():
    stage1 = operand("V3", "5200000", "ILS", kind="income", role="component", subject="שלב א", table=TABLE,
                     vat="excluded")
    stage2 = operand("V4", "7250000", "ILS", kind="income", role="component", subject="שלב ב", table=TABLE,
                     vat="excluded")
    for expression in ("sum(V1, V3, V4)", "V1 + V3 + V4"):
        with pytest.raises(CalcError) as e:
            run(expression, ops(INCOME, stage1, stage2), justification="בדיקה")
        assert "סה״כ" in str(e.value) and "רכיבים" in str(e.value)
    # the components alone sum fine (different stages are parts of one whole)
    assert run("V3 + V4", ops(stage1, stage2)).value == Decimal("12450000")


def test_ae5_monthly_rent_plus_capital_value_is_refused_even_with_a_justification():
    rent = operand("V5", "7560", "ILS", kind="rent", role="income", period="month", subject="הנכס")
    value = operand("V6", "1800000", "ILS", kind="value", role="other", subject="הנכס")
    for expression in ("V5 + V6", "V6 - V5", "mean(V5, V6)"):
        with pytest.raises(CalcError) as e:
            run(expression, ops(rent, value), justification="המשתמש ביקש")
        assert "שכירות" in str(e.value) and "שווי" in str(e.value)


def test_income_minus_cost_in_the_same_unit_vat_and_scope_is_accepted():
    out = run("V1 - V2", ops(INCOME, COST))
    assert out.value == Decimal("2050000") and out.kind == "profit" and out.conditional == []


def test_a_vat_mismatch_needs_a_justification_and_then_is_conditional():
    gross = operand("V7", "10400000", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה", vat="included")
    with pytest.raises(CalcError) as e:
        run("V1 - V7", ops(INCOME, gross))
    assert "מע״מ" in str(e.value) and "justification" in str(e.value)
    out = run("V1 - V7", ops(INCOME, gross), justification="המשתמש ביקש להתעלם מהמע״מ")
    assert out.conditional and "מע״מ" in out.conditional[0]


def test_different_units_never_add():
    area = operand("V8", "120", "sqm", kind="area", subject="פרויקט הדגמה")
    with pytest.raises(CalcError) as e:
        run("V1 + V8", ops(INCOME, area), justification="בדיקה")
    assert "יחידות" in str(e.value)


def test_a_per_area_value_times_an_area_is_a_total_with_a_derived_unit():
    price = operand("V9", "9500", "ILS_per_sqm", kind="value_per_area", basis="אקוו", subject="הנכס")
    area = operand("V10", "120", "sqm", kind="area", basis="אקוו", subject="הנכס")
    out = run("V9 * V10", ops(price, area))
    assert out.value == Decimal("1140000") and out.dims == (("ILS", 1),) and out.kind == "value"
    assert out.conditional == [] and calc.unit_label(out.dims, out.period) == "₪"


def test_mixed_area_bases_are_refused_without_a_justification_and_conditional_with_one():
    price = operand("V9", "9500", "ILS_per_sqm", kind="value_per_area", basis="אקוו", subject="הנכס")
    gross = operand("V11", "135", "sqm", kind="area", basis="ברוטו", subject="הנכס")
    with pytest.raises(CalcError) as e:
        run("V9 * V11", ops(price, gross))
    assert "בסיס שטח" in str(e.value) and "justification" in str(e.value)
    out = run("V9 * V11", ops(price, gross), justification="השמאי קבע מקדם אקוו׳ 1 לשטח הזה")
    assert out.value == Decimal("1282500") and out.conditional and "בסיס שטח" in out.conditional[0]


def test_a_percentage_applies_only_through_percent():
    with pytest.raises(CalcError) as e:
        run("V2 * A1", ops(COST, RISE))
    assert "A1%" in str(e.value)
    assert run("V2 * A1%", ops(COST, RISE)).value == Decimal("520000")


def test_monthly_times_twelve_is_yearly_and_months_and_years_never_add():
    rent = operand("V5", "7560", "ILS", kind="rent", period="month", subject="הנכס")
    yearly = run("V5 * 12", ops(rent))
    assert yearly.value == Decimal("90720") and yearly.period == "year"
    annual = operand("V12", "90000", "ILS", kind="rent", period="year", subject="הנכס")
    with pytest.raises(CalcError) as e:
        run("V5 + V12", ops(rent, annual), justification="בדיקה")
    assert "12" in str(e.value)
    assert run("V5 * 12 - V12", ops(rent, annual)).value == Decimal("720")


def test_aggregates_fold_in_from_compute():
    rents = [operand(f"M{i}", str(v), "ILS_per_sqm", kind="rent_per_area", period="month", group="asking_price",
                     same=f"m{i}") for i, v in enumerate((55, 56, 57, 58), start=1)]
    o = ops(*rents)
    assert run("mean(M1, M2, M3, M4)", o).value == Decimal("56.5")
    assert run("median(M1, M2, M3, M4)", o).value == Decimal("56.5")
    assert run("max(M1, M2, M3, M4) - min(M1, M2, M3, M4)", o).value == Decimal("3")
    with pytest.raises(CalcError) as e:
        run("sum(M1, M2)", o)
    assert "ליחידת שטח" in str(e.value)
    again = operand("M5", "55", "ILS_per_sqm", kind="rent_per_area", period="month", group="asking_price", same="m1")
    counted = run("count(M1, M2, M3, M4, M5, M1)", ops(*rents, again))
    assert counted.value == 4 and counted.n == 4  # one stored value is counted once
    value = operand("M6", "9500", "ILS_per_sqm", kind="value_per_area", group="appraiser_determination", same="m6")
    with pytest.raises(CalcError):
        run("mean(M1, M6)", ops(*rents, value))


def test_division_by_zero_is_refused():
    zero = operand("V13", "0", "ILS", kind="cost", subject="פרויקט הדגמה")
    with pytest.raises(CalcError) as e:
        run("V1 / V13", ops(INCOME, zero))
    assert "אפס" in str(e.value)


def test_an_unknown_id_is_refused():
    with pytest.raises(CalcError) as e:
        run("V1 + V99", ops(INCOME))
    assert "V99" in str(e.value)


# --- display: a shown number matches a result rounded to the shown precision ----------------------------------

@pytest.mark.parametrize("written, percent, value, dims, ok", [
    ("14.3", True, Decimal("0.1431554524361948955916473318"), (), True),
    ("14.32", True, Decimal("0.1431554524361948955916473318"), (), True),
    ("14", True, Decimal("0.1431554524361948955916473318"), (), True),
    ("14.4", True, Decimal("0.1431554524361948955916473318"), (), False),
    ("14.30", True, Decimal("0.1431554524361948955916473318"), (), False),
    ("0.14", False, Decimal("0.1431554524361948955916473318"), (), True),
    ("8.87", True, Decimal("8.871903"), (("%", 1),), True),
    ("8.9", True, Decimal("8.871903"), (("%", 1),), True),
    ("8.8", True, Decimal("8.871903"), (("%", 1),), False),
    ("1,530,000", False, Decimal("1530000.4"), (("ILS", 1),), True),
    ("1,500,000", False, Decimal("1530000.4"), (("ILS", 1),), False),
    ("1,530,001", False, Decimal("1530000.4"), (("ILS", 1),), False),
])
def test_a_displayed_number_matches_the_full_value_rounded_to_its_shown_precision(written, percent, value, dims, ok):
    assert calc.display_matches(written, percent, value, dims) is ok


# --- the verifier: a result shown rounded is supported only at the precision it shows --------------------------

def _workspace_with_ratio():
    from app.chat import tools as T

    ws = T.Workspace(ctx=None)
    a = operand("V1", "143155", "ILS", kind="cost", subject="הפרויקט")
    b = operand("V2", "1000000", "ILS", kind="income", subject="הפרויקט")
    out = run("V1 / V2", ops(a, b))
    ws.computations["C1"] = calc.Computation("C1", "שיעור העלות", "V1 ÷ V2", "«עלות» ÷ «הכנסות»", out,
                                             [{"id": "V1", "label": "עלות", "kind": "value", "value": "143155",
                                               "display": "143,155"}], [], 1, "computed", None, "", None, [])
    return ws


@pytest.mark.parametrize("shown, ok", [("14.3%", True), ("14.32%", True), ("0.14", True), ("14.4%", False),
                                       ("14.30%", False), ("15%", False)])
def test_the_verifier_accepts_a_result_shown_at_its_precision_and_nothing_else(shown, ok):
    from app.chat.verify import deterministic, split_units

    ws = _workspace_with_ratio()
    units = split_units(f"שיעור העלות מההכנסות הוא {shown} [C1].")
    problems = deterministic(units, ws, "מה שיעור העלות?", meanings={u.index: [] for u in units})
    assert (problems == []) is ok, problems


def test_the_tool_list_has_the_calculator_and_strict_schemas():
    from app.chat import tools as T

    names = [t["name"] for t in T.TOOLS]
    assert {"take_value", "assume", "calculate"} <= set(names) and "compute" not in names
    assert set(T.HANDLERS) == set(names)

    def strict(schema: dict) -> None:
        if "object" in (schema.get("type") if isinstance(schema.get("type"), list) else [schema.get("type")]):
            props = schema.get("properties") or {}
            assert schema.get("additionalProperties") is False and set(schema.get("required") or []) == set(props)
            for p in props.values():
                strict(p)

    for t in T.TOOLS:
        strict(t["parameters"])


def test_a_count_is_shown_as_a_number_while_a_ratio_keeps_its_percentage():
    rents = [operand(f"M{i}", str(v), "ILS_per_sqm", kind="rent_per_area", period="month", group="asking_price",
                     same=f"m{i}") for i, v in enumerate((55, 56, 57, 58, 59), start=1)]
    counted = run("count(M1, M2, M3, M4, M5)", ops(*rents))
    assert counted.dims == () and counted.kind == "count"
    assert calc.display(counted.value, counted.dims, counted.kind) == {"value": "5"}
    assert not calc.display_matches("500", True, counted.value, counted.dims, counted.kind)
    ratio = run("V2 / V1", ops(INCOME, COST))
    assert calc.display(ratio.value, ratio.dims, ratio.kind)["percent"] == "83.53%"
    assert calc.display_matches("83.5", True, ratio.value, ratio.dims, ratio.kind)
    share = run("count(M1, M2) / count(M1, M2, M3, M4, M5)", ops(*rents))
    assert share.kind == "ratio" and calc.display(share.value, share.dims, share.kind)["percent"] == "40%"


# --- round 7 U7: literals stay structural; a written rate has a rounding interval (KTD8, R23–R25) -------------

RENT = operand("V5", "7560", "ILS", kind="rent", period="month", subject="הנכס")
SHARE = operand("V3", "17", "percent", kind="rate", role="rate", subject="פרויקט הדגמה")


@pytest.mark.parametrize("expression", [
    "V2 * 12%",                # the structural 12 as a rate
    "V1 - V2 * (1 + 12%)",     # an increase nobody gave (round 7 F8)
    "V1 - V2 * (1 + 1%)",
    "V2 * 100%",
    "V2 * 12 / 100",           # 12 of every 100: a rate again
    "V2 * (1 + 12 / 100)",
    "V2 * (12 + 1)",           # literals combined into another number
    "V1 / 100",                # an amount over 100 is a rate of it, not a conversion
    "V2 * 100",                # an amount times 100 is not a percentage
    "12 / V2",
])
def test_a_structural_literal_never_acts_as_a_rate_or_an_assumption(expression):
    with pytest.raises(CalcError) as e:
        run(expression, ops(INCOME, COST))
    assert "קבוע" in str(e.value) or "שיעור" in str(e.value), str(e.value)


def test_the_structural_literals_keep_their_structural_uses():
    rent, annual = RENT, operand("V12", "90720", "ILS", kind="rent", period="year", subject="הנכס")
    assert run("V5 * 12", ops(rent)).period == "year"
    assert run("V12 / 12", ops(annual)).value == Decimal("7560")
    assert run("V1 - V2*(1+A1%)", ops(INCOME, COST, RISE)).value == Decimal("1530000")
    assert run("V2 * (1 - A1%)", ops(COST, RISE)).value == Decimal("9880000")
    ratio = calc.result_operand("C1", run("V2 / V1", ops(INCOME, COST)))
    assert run("C1 * 100", ops(ratio)).dims == (("%", 1),)
    assert run("V3 / 100", ops(SHARE)).value == Decimal("0.17")
    assert run("V2 * V3%", ops(COST, SHARE)).value == Decimal("1768000")


@pytest.mark.parametrize("written, low, high", [
    ("17", "16.5", "17.5"), ("כ-17%", "16.5", "17.5"), ("20", "19.5", "20.5"), ("17.5", "17.45", "17.55"),
    ("6.25%", "6.245", "6.255"), ("0.5", "0.45", "0.55"),
])
def test_a_written_rate_has_the_rounding_interval_of_its_last_written_digit(written, low, high):
    assert calc.rounding_interval(written) == (Decimal(low), Decimal(high))


@pytest.mark.parametrize("written", ["", "כ-", "17 או 18"])
def test_a_text_without_one_number_has_no_rounding_interval(written):
    assert calc.rounding_interval(written) is None


def test_a_rate_applied_through_percent_is_found_in_the_expression():
    assert calc.applied_rates(calc.parse("V1 - V2 * (1 + A1%)")) == ["A1"]
    assert calc.applied_rates(calc.parse("V2 * (V3 / 100) + V2 * C2%")) == ["V3", "C2"]
    assert calc.applied_rates(calc.parse("V1 - V2")) == []
    products = calc.rate_products(calc.parse("V1 - V2 * (1 + V3%)"))
    assert [(calc.render(n, lambda i: i), i) for n, i in products] == [("V2 × (1 + V3%)", "V3")]


# --- round 7 U7: verification of a rounded rate's product and of a parameter nobody gave (KTD8, KTD9) ----------

def _workspace_with(computation_kwargs: dict, requirements: list[dict] | None = None):
    from app.chat import tools as T
    from app.chat.verify import TurnRequirements

    ws = T.Workspace(ctx=None)
    ws.user_messages = [{"turn": 1, "text": "מה הפער בין הרווח היזמי לרווח הנדרש?", "current": True}]
    cost = operand("V1", "18350000", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה")
    rate = operand("V2", "17", "percent", kind="rate", role="rate", subject="פרויקט הדגמה")
    out = run("V1 * V2%", ops(cost, rate))
    inputs = [{"id": "V1", "label": "סך העלויות", "kind": "value", "value": "18350000", "display": "18,350,000",
               "value_text": "18,350,000"},
              {"id": "V2", "label": "שיעור הרווח היזמי", "kind": "value", "value": "17", "display": "17",
               "value_text": "17"}]
    ws.computations["C1"] = calc.Computation("C1", "הרווח היזמי", "V1 × V2%", "«סך העלויות» × «שיעור הרווח»%", out,
                                             inputs, [], 1, "computed", None, "", None, ["V1", "V2"],
                                             **computation_kwargs)
    if requirements is not None:
        turn = TurnRequirements()
        turn.adopt(requirements, "analysis")
        ws.requirements = turn
    return ws


NEAR_MISS = {"amount": "3,210,000", "value": "3210000", "source": "S1", "quote": "סכום הרווח היזמי בתחשיב: 3,210,000 ₪",
             "rate": "V2", "rate_written": "17", "interval": ["16.5", "17.5"], "computed": "3119500",
             "range": ["3027750", "3211250"], "from": None}


def test_a_result_built_from_a_rounded_rate_beside_a_stated_amount_is_an_input_choice_problem():
    from app.chat.verify import deterministic, split_units

    ws = _workspace_with({"explicit_amount": NEAR_MISS, "rates": ["V2"]})
    units = split_units("לפי החישוב, הרווח היזמי הוא 3,119,500 ₪ [C1].")
    (p,) = deterministic(units, ws, "מה הרווח היזמי?", meanings={u.index: [] for u in units})
    assert p.kind == "input_choice" and p.failure_kind == "wrong_calculation" and p.check == "input_choice"
    assert p.repairable and p.removes_unit
    assert {"C1", "V2", "S1"} <= set(p.checked_ids) and "3,210,000" in p.reason


def test_the_users_own_rate_is_never_flagged():
    from app.chat.verify import deterministic, split_units

    ws = _workspace_with({"explicit_amount": None, "rates": ["A1"]})
    ws.user_messages = [{"turn": 1, "text": "חשב את הרווח לפי 17% מהעלויות", "current": True}]
    units = split_units("לפי בקשתך, הרווח היזמי הוא 3,119,500 ₪ [C1].")
    assert deterministic(units, ws, "חשב את הרווח לפי 17% מהעלויות", meanings={u.index: [] for u in units}) == []
    # a document rate the user asked for by its number is not an input choice either
    ws = _workspace_with({"explicit_amount": NEAR_MISS, "rates": ["V2"]})
    ws.user_messages = [{"turn": 1, "text": "חשב את הרווח לפי 17% מהעלויות", "current": True}]
    assert deterministic(units, ws, "חשב את הרווח לפי 17% מהעלויות", meanings={u.index: [] for u in units}) == []


RISE_COMPONENT = {"id": "N1", "text": "הרווח אם העלויות יעלו", "kind": "calculation",
                  "parameters": [{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""}]}


def test_a_parameter_nobody_gave_is_pending_until_a_user_assumption_or_a_document_rate_fills_it():
    from app.chat.verify import unfilled_parameters

    ws = _workspace_with({"explicit_amount": None, "rates": []}, [RISE_COMPONENT])
    item = ws.requirements.items[0]
    ws.computations.clear()
    assert unfilled_parameters(ws, item) == ["שיעור העלייה של העלויות"]
    # a computation that applies a document rate (a scenario the report states) fills it
    filled = _workspace_with({"explicit_amount": None, "rates": ["V2"]}, [RISE_COMPONENT])
    assert unfilled_parameters(filled, filled.requirements.items[0]) == []
    # so does the user's own number
    ws.assumptions["A1"] = calc.Assumption("A1", Decimal("8"), "8", "percent", "עליית העלויות", "8%", 2, True)
    assert unfilled_parameters(ws, item) == []
    # a parameter the user gave, or a component that is no calculation, is never pending
    given = dict(RISE_COMPONENT, parameters=[{"name": "שיעור", "source": "given_by_user", "quote": "8%"}])
    assert unfilled_parameters(ws, given) == [] and unfilled_parameters(ws, dict(RISE_COMPONENT, kind="information")) == []


# A parameter the analysis marked as not given may be document data ("the developer profit in the calculation"): a
# computation of the component resting only on the turn's registered document values and the user's assumptions
# fills it, since then either it was document data or the report states the scenario (U9 residual, KTD9). A literal
# in it, or no computation at all, leaves it pending (F8).

GAP_COMPONENT = {"id": "N1", "text": "הפער בין הרווח היזמי לרווח המינימלי הנדרש", "kind": "calculation",
                 "parameters": [{"name": "הרווח היזמי בתחשיב", "source": "not_given_by_user", "quote": ""},
                                {"name": "הסף המינימלי הנדרש", "source": "not_given_by_user", "quote": ""}]}


def _document_value(ws, vid: str, written: str, label: str):
    import uuid

    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(),
                                None, "בדיקת כדאיות", "עמוד 1", label, "profit", "ILS", "none", "unknown", "",
                                "פרויקט הדגמה", "profit", {"unit": "source"}, {"quote": written}, written)
    return ws.values[vid].operand()


def _gap_workspace(expression: str, requirements: list[dict]):
    from app.chat import tools as T
    from app.chat.verify import TurnRequirements

    ws = T.Workspace(ctx=None)
    a = _document_value(ws, "V1", "3,210,000", "סכום הרווח היזמי")
    b = _document_value(ws, "V2", "2,800,000", "הרווח המינימלי הנדרש")
    out = run(expression, ops(a, b))
    ws.computations["C1"] = calc.Computation("C1", "הפער", expression, expression, out, [], ["S1"], 1, "computed",
                                             None, "", None, ["V1", "V2"])
    turn = TurnRequirements()
    turn.adopt(requirements, "analysis")
    ws.requirements = turn
    return ws


def test_a_parameter_that_was_document_data_is_filled_by_a_computation_resting_on_document_values():
    from app.chat.verify import pending_parameters, unfilled_parameters

    ws = _gap_workspace("V1 - V2", [GAP_COMPONENT])
    assert unfilled_parameters(ws, ws.requirements.items[0]) == [] and pending_parameters(ws) == {}
    # a result built on an earlier one (a C# over V#s) rests on the values under it
    ws.computations["C2"] = calc.Computation("C2", "הפער", "C1", "C1", ws.computations["C1"].outcome, [], [], 1,
                                             "computed", None, "", None, ["V1", "V2"])
    assert unfilled_parameters(ws, ws.requirements.items[0], linked=["C2"]) == []
    del ws.computations["C1"]
    assert unfilled_parameters(ws, ws.requirements.items[0], linked=["C2"]) == ["הרווח היזמי בתחשיב",
                                                                               "הסף המינימלי הנדרש"]


def test_a_computation_with_a_literal_or_an_unregistered_input_does_not_fill_a_parameter():
    from app.chat.verify import unfilled_parameters

    ws = _gap_workspace("V1 * 12", [GAP_COMPONENT])  # a structural literal fills nothing of a scenario
    assert unfilled_parameters(ws, ws.requirements.items[0]) == ["הרווח היזמי בתחשיב", "הסף המינימלי הנדרש"]
    ws = _gap_workspace("V1 - V2", [GAP_COMPONENT])
    del ws.values["V2"]  # an input the turn holds no registered value for
    assert unfilled_parameters(ws, ws.requirements.items[0]) == ["הרווח היזמי בתחשיב", "הסף המינימלי הנדרש"]


def test_with_two_calculation_components_only_a_computation_the_judge_links_fills_one():
    from app.chat.verify import (
        JudgeRequirement,
        VerifyReport,
        pending_parameters,
        requirement_item,
        unfilled_parameters,
    )

    other = dict(RISE_COMPONENT, id="N2")
    ws = _gap_workspace("V1 - V2", [GAP_COMPONENT, other])
    gap, rise = ws.requirements.items
    # no link: neither is assumed to be the computation's component
    assert set(pending_parameters(ws)) == {"N1", "N2"}
    assert unfilled_parameters(ws, gap, linked=["C1"]) == []
    assert unfilled_parameters(ws, rise, linked=[]) == ["שיעור העלייה של העלויות"]
    # the judge names C1 for the gap: the gap's parameters are filled, the rise still waits for the user
    report = VerifyReport([], requirements=[requirement_item("N1", gap["text"], "calculation",
                                                             parameters=gap["parameters"]),
                                            requirement_item("N2", rise["text"], "calculation",
                                                             parameters=rise["parameters"])],
                          pending_parameters=pending_parameters(ws))
    report.requirement_votes = {"N1": [JudgeRequirement(id="N1", status="full", related=["C1", "V1"])],
                                "N2": [JudgeRequirement(id="N2", status="missing")]}
    report.settle_parameters(ws)
    assert report.pending_parameters == {"N2": ["שיעור העלייה של העלויות"]}
    statuses = {o["id"]: o["status"] for o in report.requirement_outcomes()}
    assert statuses["N2"] == "needs_clarification" and statuses["N1"] != "needs_clarification"


def test_a_cost_increase_with_no_rate_stays_pending_whatever_the_turn_computed_from_a_literal():
    from app.chat.verify import pending_parameters

    ws = _gap_workspace("V1 * 12", [RISE_COMPONENT])
    assert pending_parameters(ws) == {"N1": ["שיעור העלייה של העלויות"]}


def test_a_number_the_answer_assumes_for_a_pending_parameter_is_an_unrequested_assumption():
    from app.chat.verify import deterministic, split_units

    ws = _workspace_with({"explicit_amount": None, "rates": []}, [RISE_COMPONENT])
    ws.computations.clear()
    units = split_units("אם העלויות יעלו ב-10%, הרווח יהיה כ-4.4 מיליון ₪.")
    (p,) = deterministic(units, ws, "מה יהיה הרווח אם העלויות יעלו?", meanings={u.index: [] for u in units})
    assert p.kind == "assumption" and p.failure_kind == "wrong_calculation" and p.check == "unrequested_assumption"
    assert p.repairable and "שיעור העלייה של העלויות" in p.reason


TWO_PARAMETERS = {"id": "N1", "text": "הרווח אם העלויות יעלו לאורך תקופה", "kind": "calculation",
                  "parameters": [{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""},
                                 {"name": "תקופת העלייה", "source": "not_given_by_user", "quote": ""}]}


def _assume(ws, aid: str, written: str, quote: str, parameter: str | None = None) -> None:
    ws.assumptions[aid] = calc.Assumption(aid, Decimal(written), written, "percent", "הנחת המשתמש", quote, 2, True,
                                          parameter)


def test_one_user_assumption_fills_one_parameter_never_every_missing_one():
    """Round 7 KTD9: an A# fills the parameter it was registered for (by name, ``Assumption.parameter``); an A# with no
    parameter link fills one parameter, in the component's order — never every not-given parameter of the turn."""
    from app.chat.verify import pending_parameters, unfilled_parameters

    ws = _workspace_with({"explicit_amount": None, "rates": []}, [TWO_PARAMETERS])
    ws.computations.clear()
    item = ws.requirements.items[0]
    assert unfilled_parameters(ws, item) == ["שיעור העלייה של העלויות", "תקופת העלייה"]
    # linked by name: the other parameter still waits, whichever of the two the user gave
    _assume(ws, "A1", "8", "8%", parameter="תקופת העלייה")
    assert unfilled_parameters(ws, item) == ["שיעור העלייה של העלויות"]
    assert pending_parameters(ws) == {"N1": ["שיעור העלייה של העלויות"]}
    ws.assumptions.clear()
    # no link: one assumption counts as one parameter, in order
    _assume(ws, "A1", "8", "8%")
    assert unfilled_parameters(ws, item) == ["תקופת העלייה"]
    _assume(ws, "A2", "3", "3 שנים")
    assert unfilled_parameters(ws, item) == [] and pending_parameters(ws) == {}


def test_an_assumption_for_a_parameter_the_user_gave_fills_no_missing_one():
    from app.chat.verify import unfilled_parameters

    given = dict(TWO_PARAMETERS, parameters=[{"name": "שיעור העלייה של העלויות", "source": "given_by_user",
                                              "quote": "יעלו ב-8%"}, TWO_PARAMETERS["parameters"][1]])
    ws = _workspace_with({"explicit_amount": None, "rates": []}, [given])
    ws.computations.clear()
    _assume(ws, "A1", "8", "יעלו ב-8%")  # the rate the user wrote: the period still waits
    assert unfilled_parameters(ws, ws.requirements.items[0]) == ["תקופת העלייה"]


def test_a_calculation_component_waiting_for_a_detail_nobody_gave_needs_clarification():
    from app.chat.verify import VerifyReport, requirement_item

    item = requirement_item("N1", RISE_COMPONENT["text"], "calculation", parameters=RISE_COMPONENT["parameters"])
    other = requirement_item("N2", "ההכנסות", "information")
    report = VerifyReport([], requirements=[item, other], pending_parameters={"N1": ["שיעור העלייה של העלויות"]})
    statuses = {o["id"]: o["status"] for o in report.requirement_outcomes()}
    assert statuses == {"N1": "needs_clarification", "N2": "not_answered"}
    assert VerifyReport([], requirements=[item]).requirement_outcomes()[0]["status"] == "not_answered"


# --- the scale a source states its amounts in (R14): "באלפי ₪" -------------------------------------------------------

K_INCOME = operand("V1", "412300", "ILS", kind="income", role="income", subject="פרויקט הדגמה", vat="excluded",
                   scale=1000)
K_COST = operand("V2", "368150", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה", vat="excluded", scale=1000)
UNITS_COST = operand("V3", "1250000", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה", vat="excluded")
AREA = operand("V4", "2000", "sqm", subject="פרויקט הדגמה")


@pytest.mark.parametrize("expression, value, scale", [
    ("V1 - V2", "44150", 1000),  # a difference of amounts of one scale keeps it
    ("V1 + V2", "780450", 1000),
    ("max(V2, V5)", "368150", 1000),
    ("V1 - V2*(1+A1%)", "25742.5", 1000),  # an amount × a rate keeps it
    ("V1 * 12", "4947600", 1000),
    ("(V1 - V2) / V2", "0.1199239440445470596224365069", 1),  # amount ÷ amount: the scale cancels
    ("V1 / V4", "206.15", 1000),  # thousands of ₪ per m²
    ("V1 / V4 * V4", "412300", 1000),
])
def test_a_result_carries_the_scale_its_inputs_are_stated_in(expression, value, scale):
    other = operand("V5", "300000", "ILS", kind="cost", role="cost", subject="פרויקט הדגמה", vat="excluded", scale=1000)
    out = run(expression, ops(K_INCOME, K_COST, RISE, AREA, other))
    assert (out.value, out.scale, out.rescaled) == (Decimal(value), scale, False)


def test_amounts_of_different_scales_are_brought_to_units_before_they_are_combined():
    out = run("V1 - V3", ops(K_INCOME, UNITS_COST))
    assert (out.value, out.scale, out.rescaled) == (Decimal("411050000"), 1, True)
    # a ratio of amounts in different scales is a plain ratio, never off by the scale
    ratio = run("V3 / V1", ops(K_INCOME, UNITS_COST))
    assert (ratio.value.quantize(Decimal("0.000001")), ratio.scale) == (Decimal("0.003032"), 1)
    # an earlier result keeps its scale as an input
    c1 = calc.result_operand("C1", run("V1 - V2", ops(K_INCOME, K_COST)))
    assert run("C1 * A1%", ops(c1, RISE)).scale == 1000


def test_a_display_in_a_scale_is_checked_against_the_amount_the_value_is():
    value = Decimal("25742.5")  # thousands of ₪
    ils = (("ILS", 1),)
    assert calc.display_matches("25.74", False, value, ils, scale=10**6, source_scale=1000)
    assert calc.display_matches("25,742.5", False, value, ils, scale=10**3, source_scale=1000)
    assert calc.display_matches("25,742,500", False, value, ils, source_scale=1000)
    assert calc.display_matches("25,742.5", False, value, ils, source_scale=1000)  # as the source writes it
    assert not calc.display_matches("27.4", False, value, ils, scale=10**6, source_scale=1000)
    assert not calc.display_matches("25,742.5", False, value, ils, scale=10**6, source_scale=1000)
    assert not calc.display_matches("25.74", False, value, ils, scale=10**3, source_scale=1000)


@pytest.mark.parametrize("own, contexts, scale", [
    ("412,300", ["הכנסות", "טבלה 4: תחזית (באלפי ₪)"], 1000),
    ("412,300", ["הכנסות (אלפי ש״ח)", ""], 1000),
    ("412,300", ["סכום (K ₪)"], 1000),
    ("412,300", ["הכנסות אש\"ח"], 1000),
    ("412.3", ["סכום (במיליוני ₪)"], 10**6),
    ("412,300", ["₪ באלפים"], 1000),
    ("5,600 אלף ₪", ["הכנסות"], 1000),  # the number's own scale word
    ("1.53 מיליון ₪", [], 10**6),
    ("5,000,000 ₪", ["הנתונים בטבלה באלפי ₪"], 1),  # a currency right after it: in units
    ("412,300", ["הכנסות", "הכנסות (באלפי ₪) ועלויות (במיליוני ₪)"], 1),  # two scales say nothing
    ("412,300", ["עלות (מיליוני ₪)", "טבלה (באלפי ₪)"], 10**6),  # the nearest note wins
    ("412,300", ["השווי 1,530 אלפי ₪"], 1),  # another number's own scale word is no note
    ("412,300", ["הכנסות", "סיכום"], 1),
])
def test_the_scale_a_source_states_a_number_in(own, contexts, scale):
    m = calc._WRITTEN_NUMBER.search(own)
    assert calc.stated_scale(own, m.start(), m.end(), *contexts) == scale


def test_a_value_taken_from_a_table_in_thousands_or_a_quote_carries_its_scale():
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    st = {"headers": ["סעיף", "2025"], "rows": [{"cells": ["סה״כ הכנסות", "412,300"]}],
          "caption": "טבלה 4: תחזית הכנסות ועלויות (באלפי ₪)", "title": [], "notes": []}
    full = "טבלה 4: תחזית הכנסות ועלויות (באלפי ₪)\nסה״כ הכנסות | 412,300"
    taken = tools._cell_of(src, full, {"row": "סה״כ הכנסות", "column": "2025"}, st, 0)
    assert taken["scale"] == 1000
    st["caption"] = "טבלה 4: תחזית הכנסות ועלויות"
    assert tools._cell_of(src, full, {"row": "סה״כ הכנסות", "column": "2025"}, st, 0)["scale"] == 1
    text = "סך ההכנסות הצפויות (באלפי ₪) הוא 412,300, והעלויות 368,150."
    taken = tools._take_quote(src, text, {"quote": "סך ההכנסות הצפויות (באלפי ₪) הוא 412,300", "number": "412,300"})
    assert taken["scale"] == 1000
    taken = tools._take_quote(src, "עלות היתר 1,250,000 ₪.", {"quote": "עלות היתר 1,250,000 ₪", "number": "1,250,000"})
    assert taken["scale"] == 1


# --- a scale note on a heading-like line governs the figures under it (final evaluation, round 7) ------------------

NOTE = "ממצאי בדיקת הכדאיות לפרויקט באלפי ₪ לא כולל מע״מ"


@pytest.mark.parametrize("status", ["read", "read_uncertain"])
def test_a_quote_inherits_its_currency_only_from_a_clearly_read_governing_note(status):
    from types import SimpleNamespace

    from app.chat import tools

    text = 'רווח שוטף 13,250'
    note = tools._ScaleNote(NOTE, 1, status)
    src = SimpleNamespace(sid="S1", version_id="v1")
    if status == "read_uncertain":
        with pytest.raises(tools.ToolError, match="לא ודאי"):
            tools._take_quote(src, text, {"quote": text, "number": "13,250"}, [(2, text, 1)], lambda _: note)
    else:
        taken = tools._take_quote(src, text, {"quote": text, "number": "13,250"}, [(2, text, 1)], lambda _: note)
        assert taken["units"] == {"ILS"}
        assert taken["meaning_from"]["unit"] == "governing_note"


def test_a_table_inherits_currency_from_its_governing_note_but_its_own_unit_wins():
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    st = {"headers": ["רכיב", "סכום"], "rows": [{"cells": ["הכנסה", "13,250"]}],
          "caption": "תוצאות", "title": [], "notes": []}
    taken = tools._cell_of(src, 'הכנסה | 13,250', {"row": "הכנסה", "column": "סכום"}, st, 0,
                          tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {"ILS"} and taken["meaning_from"]["unit"] == "governing_note"
    st["headers"][1] = "שטח במ״ר"
    taken = tools._cell_of(src, 'הכנסה | 13,250', {"row": "הכנסה", "column": "שטח במ״ר"}, st, 0,
                          tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {"sqm"} and taken["scale"] == 1


@pytest.mark.parametrize("label, unit", [('מספר יח״ד', 'units'), ('מקדם התאמה', 'ratio')])
def test_a_governing_currency_does_not_replace_a_count_or_a_coefficient(label, unit):
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    st = {"headers": ["רכיב", "ערך"], "rows": [{"cells": [label, "80"]}],
          "caption": "תוצאות", "title": [], "notes": []}
    taken = tools._cell_of(src, f'{label} | 80', {"row": label, "column": "ערך"}, st, 0,
                          tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {unit}
    assert taken["scale"] == 1
    quote = f'{label} 80'
    taken = tools._take_quote(src, quote, {"quote": quote, "number": "80"}, [(2, quote, 1)],
                              lambda _: tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {unit} and taken["scale"] == 1


def test_a_nonmonetary_quantity_keeps_its_own_explicit_scale():
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    quote = 'שטח המבנה 10 אלף מ״ר'
    taken = tools._take_quote(src, quote, {"quote": quote, "number": "10"}, [(2, quote, 1)],
                              lambda _: tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {"sqm"} and taken["scale"] == 1000


@pytest.mark.parametrize("preceding, governing", [
    # (kind, text, same section), nearest first: the heading-like paragraph above the figures' paragraphs
    ([("paragraph", "סה״כ הכנסות היזם 117,860", True), ("paragraph", NOTE, True), ("heading", "7. כדאיות", True)],
     NOTE),
    ([("heading", "7. בדיקת כדאיות (באלפי ₪)", True)], "7. בדיקת כדאיות (באלפי ₪)"),  # a heading's own note
    ([("heading", "7. בדיקת כדאיות", True), ("paragraph", NOTE, True)], ""),  # not across the section's heading
    ([("paragraph", NOTE, False)], ""),  # another section's note
    ([("table", "טבלה 3 (במיליוני ₪)", True), ("paragraph", NOTE, True)], ""),  # a table with its own note
    ([("table", "טבלה 3: לוח זמנים", True), ("paragraph", NOTE, True)], NOTE),  # a table without one
    ([("paragraph", "העלויות (באלפי ₪) 104,610", True), ("paragraph", NOTE, True)], ""),  # another figure's note
    ([("paragraph", f"שורה {i} ללא הערה", True) for i in range(calc.NOTE_WINDOW)] + [("paragraph", NOTE, True)],
     ""),  # beyond the window
])
def test_the_note_that_governs_a_figure_is_the_nearest_heading_like_line_of_its_section(preceding, governing):
    assert calc.governing_note(preceding) == governing


def test_a_governing_note_is_the_farthest_context_and_never_scales_a_percent():
    own = "רווח שוטף (הפסד) 13,250"
    m = calc._WRITTEN_NUMBER.search(own)
    assert calc.stated_scale(own, m.start(), m.end(), own, NOTE) == 1000
    assert calc.stated_scale(own, m.start(), m.end(), "טבלה (במיליוני ₪)", NOTE) == 10**6  # the nearer note wins
    rate = "שיעור רווח לעלות (הפסד) 12.7%"
    m = calc._WRITTEN_NUMBER.search(rate)
    assert calc.stated_scale(rate, m.start(), m.end(), rate, NOTE) == 1


@pytest.mark.parametrize("a, b, same", [
    ("רווח פרויקט הדגמה", "עלויות פרויקט הדגמה", True),  # the metric's own words are not the scope
    ("רווח פרויקט הדגמה שלב א", "עלויות פרויקט הדגמה שלב ב", False),  # each names a stage the other lacks
    ("הפרויקט במתחם הדגמה", "מתחם הדגמה", True),  # one is a narrower wording of the other
    ("דירה 3", "דירה 5", False),
    ("שלב א", "שלב ב", False),
    ("מחיר הדירה", "מחיר דירה 5 עסקת השוואה", False),  # a generic wording against a numbered comparable
])
def test_two_subjects_are_one_scope_without_the_metric_words(a, b, same):
    assert calc.same_subject(a, b) is same


def test_only_a_whole_metric_word_leaves_a_subject():
    assert len(calc._subject_words("שטחים מסחריים בפרויקט הדגמה")) == 4  # "שטחים" merely contains "שטח"
    assert "עלויות" not in calc._subject_words("עלויות פרויקט הדגמה")


def test_a_profit_less_a_cost_of_the_same_project_is_not_a_mix_of_subjects():
    profit = operand("V1", "47680", "ILS", kind="profit", subject="רווח פרויקט הדגמה", vat="excluded")
    cost = operand("V2", "192815", "ILS", kind="cost", role="cost", subject="עלויות פרויקט הדגמה", vat="excluded")
    out = run("V1 - V2 * A1%", ops(profit, cost, RISE))
    assert out.value == Decimal("38039.25") and not out.conditional
    # another property's figure still needs a justification
    other = operand("V3", "9000", "ILS", kind="cost", role="cost", subject="פרויקט אחר", vat="excluded")
    with pytest.raises(CalcError):
        run("V1 - V3", ops(profit, other))


@pytest.mark.parametrize("label", ['מחיר ליח״ד', 'שווי ליח״ד'])
def test_a_price_per_unit_row_is_money_from_its_caption_not_a_count(label):
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    st = {"headers": ["פריט", "סכום"], "rows": [{"cells": [label, "1,850"]}],
          "caption": "עסקאות השוואה (באלפי ₪)", "title": [], "notes": []}
    taken = tools._cell_of(src, f'{label} | 1,850', {"row": label, "column": "סכום"}, st, 0, None)
    assert taken["units"] == {"ILS"} and taken["scale"] == 1000


def test_a_table_number_in_a_caption_is_not_a_count():
    from app.chat.meaning import units_attested

    assert units_attested("טבלה מספר 4: נתוני עסקאות") == set()


@pytest.mark.parametrize("quote, number", [('תקופת הבנייה 36 חודשים', '36'),
                                            ('מספר השנים שנותרו בחכירה 37', '37')])
def test_a_duration_under_a_heading_in_thousands_takes_neither_its_currency_nor_its_scale(quote, number):
    from types import SimpleNamespace

    from app.chat import tools

    src = SimpleNamespace(sid="S1", version_id="v1")
    taken = tools._take_quote(src, quote, {"quote": quote, "number": number}, [(2, quote, 1)],
                              lambda _: tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == set() and taken["scale"] == 1
    # an amount under the same heading still takes both
    amount = 'רווח שוטף 13,250'
    taken = tools._take_quote(src, amount, {"quote": amount, "number": "13,250"}, [(2, amount, 1)],
                              lambda _: tools._ScaleNote(NOTE, 1, "read"))
    assert taken["units"] == {"ILS"} and taken["scale"] == 1000
