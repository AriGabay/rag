"""The calculator's expression language and its operation-dependent compatibility (U10, KTD10, R15–R18).

Pure tests: operands are built directly, as the tools build them from verified values (V#), user assumptions
(A#), stored measurements (M#) and earlier results (C#). Synthetic values only."""

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
