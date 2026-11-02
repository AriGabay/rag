"""Round 13 U5 (R7; KTD6): certainty follows the attributes that define the number.

A value is "model_asserted" (uncertain, status "?", the request component limited as ``not_verifiable``) only when its kind or
its unit was asserted by the model. A stance, a speaker, a scenario, a period, an area basis or a VAT status the model
asserted counts as not stated in every reader (the timeline's key, calculations, the coverage reason) and never makes the
value uncertain; a claim that states such an attribute goes through the qualifier edit of round 12 U10 (``verify.py``, not
tested here). Database-free; synthetic data only."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from app.chat import calc, timeline
from app.chat import tools as T
from app.chat.verify import TurnRequirements
from tests.unit.test_coverage import _missing_with
from tests.unit.test_coverage import _value as _coverage_value
from tests.unit.test_coverage import _ws as _coverage_ws
from tests.unit.test_datum_decision import Case
from tests.unit.test_timeline import Office, as_of

DEFINING = ("kind", "unit")
OTHERS = ("stance", "stated_by", "scenario", "period", "area_basis", "vat")


def _prov(**overrides: str) -> dict[str, str]:
    base = {"unit": "source", "kind": "source", "period": "source", "vat": "source", "area_basis": "source",
            "stance": "source", "stated_by": "not_stated", "scenario": "not_stated"}
    return base | overrides


def _value(prov: dict, *, kind: str = "income", unit: str = "ILS", reading: str = "") -> calc.Value:
    return calc.Value("V1", Decimal("5200000"), "5,200,000", "S1", uuid.uuid4(), uuid.uuid4(), None, "דוח בדיקה", "עמוד 1",
                      "ערך", kind, unit, "none", "excluded", "", "נכס הדגמה", "other", prov, {"quote": "5,200,000"},
                      "5,200,000", reading=reading)


# --- certainty counts the kind and the unit only ---------------------------------------------------------------------------------

@pytest.mark.parametrize("field", DEFINING)
def test_an_asserted_kind_or_unit_leaves_the_value_uncertain(field):
    v = _value(_prov(**{field: "model_asserted"}))
    assert v.certainty == "model_asserted"
    ws = T.Workspace(ctx=None)
    ws.values["V1"] = v
    assert T.value_uncertain(ws, "V1") and T.value_status(ws, "V1") == T.STATUS_UNCERTAIN
    assert T.uncertain_inputs(ws, ["V1"]) == ["V1"]


@pytest.mark.parametrize("field", OTHERS)
def test_an_asserted_attribute_that_does_not_define_the_number_leaves_it_verified(field):
    v = _value(_prov(**{field: "model_asserted"}))
    assert v.certainty == "verified"
    ws = T.Workspace(ctx=None)
    ws.values["V1"] = v
    assert not T.value_uncertain(ws, "V1") and T.value_status(ws, "V1") == T.STATUS_AUTO
    assert T.uncertain_inputs(ws, ["V1"]) == []
    assert v.public()["provenance"][field] == "model_asserted"  # still recorded as the model's, never as found


def test_every_attribute_asserted_at_once_with_kind_and_unit_from_the_source_is_verified():
    assert _value(_prov(**{f: "model_asserted" for f in OTHERS})).certainty == "verified"


def test_an_unclear_region_still_makes_the_value_uncertain_reading():
    assert _value(_prov(stance="model_asserted"), reading="uncertain").certainty == "uncertain_reading"


# --- a calculation ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def calc_ws(monkeypatch) -> T.Workspace:
    monkeypatch.setattr(T, "_check_available", lambda ws, leaves: None)
    ws = T.Workspace(ctx=None)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section=None, location="עמוד 1",
                  kind="context", text="הכנסות 5,200,000 עלויות 4,300,000", status="complete")
    return ws


def _put(ws: T.Workspace, vid: str, amount: str, prov: dict) -> None:
    v = _value(prov)
    v.vid, v.value, v.written = vid, Decimal(amount), f"{int(amount):,}"
    ws.values[vid] = v


def test_a_calculation_over_values_with_an_asserted_stance_or_scenario_is_not_conditional(calc_ws):
    _put(calc_ws, "V1", "5200000", _prov(stance="model_asserted", stated_by="model_asserted"))
    _put(calc_ws, "V2", "4300000", _prov(scenario="model_asserted"))
    out = json.loads(T.tool_calculate(calc_ws, "V1 - V2", "הפרש"))
    assert out["conditional"] is False
    assert f"V1 {T.STATUS_AUTO}" in out["note"] and f"V2 {T.STATUS_AUTO}" in out["note"]
    assert "נקבעו ולא נמצאו במקור" not in out["note"]


def test_a_calculation_over_a_value_with_an_asserted_kind_is_still_conditional(calc_ws):
    _put(calc_ws, "V1", "5200000", _prov())
    _put(calc_ws, "V2", "4300000", _prov(kind="model_asserted"))
    out = json.loads(T.tool_calculate(calc_ws, "V1 - V2", "הפרש"))
    assert out["conditional"] is True and f"V2 {T.STATUS_UNCERTAIN}" in out["note"]


def test_an_asserted_vat_is_still_refused_in_a_calculation(calc_ws):
    # the round-12 regression (R8, KTD9): the VAT counts as not stated by a calculation, though it no longer makes the value
    # uncertain, so a model-asserted "excluded" against a source "excluded" is still refused
    _put(calc_ws, "V1", "5200000", _prov())
    _put(calc_ws, "V2", "4300000", _prov(vat="model_asserted"))
    assert calc_ws.values["V2"].certainty == "verified"
    assert calc_ws.values["V2"].operand().vat == "unknown"
    with pytest.raises(T.ToolError):
        T.tool_calculate(calc_ws, "V1 - V2", "הפרש")


# --- the timeline ---------------------------------------------------------------------------------------------------------------

def _marks(blocks: list[dict]) -> set[str]:
    return {m for b in blocks for r in b["rows"] for m in r["marks"]}


def test_a_value_with_an_asserted_stance_has_no_uncertain_mark_and_its_own_data_class():
    o = Office()
    a = o.value(o.doc(title="שומה 2023"), "5,000,000", as_of("2023-03-10"))
    b = o.value(o.doc(title="שומה 2024"), "5,200,000", as_of("2024-03-10"), provenance={"stance": "model_asserted"})
    c = o.value(o.doc(title="שומה 2025"), "5,400,000", as_of("2025-03-10"),
                provenance={"stance": "model_asserted", "stated_by": "model_asserted"})
    o.ws.values[b].stance = o.ws.values[c].stance = "claim"  # what the model asserted: the key reads it as not stated
    assert not any(T.value_uncertain(o.ws, i) for i in (a, b, c))
    datums = {d.ref: d for d in timeline.datums(o.ws)}
    assert not any(d.uncertain for d in datums.values())
    assert datums[b].fields == datums[a].fields == datums[c].fields  # one comparable key: an asserted stance is "adopted"
    assert timeline.stance_class("claim") == "claim"  # the class a stated claim keeps
    (g,) = o.blocks({a, b, c})
    assert [r["sid"] for r in g["rows"]] == [c, b, a] and "uncertain" not in _marks([g])


def test_a_value_with_an_asserted_kind_keeps_its_uncertain_mark():
    o = Office()
    a = o.value(o.doc(title="שומה 2023"), "5,000,000", as_of("2023-03-10"))
    b = o.value(o.doc(title="שומה 2024"), "5,200,000", as_of("2024-03-10"))
    o.ws.values[b].provenance["unit"] = "model_asserted"
    assert T.value_uncertain(o.ws, b) and not T.value_uncertain(o.ws, a)


# --- the round-13 fixture cap_rates -----------------------------------------------------------------------------------------------

def test_the_cap_rates_with_a_source_kind_and_unit_and_an_asserted_stance_show_no_uncertain_mark():
    c = Case("AE2")
    for vid in c.ids.values():
        c.ws.values[vid].provenance.update({"stance": "model_asserted", "stated_by": "model_asserted"})
    # an earlier appraisal of the same property gives the rates of the later one a history to show
    d2 = c.o.doc("group:הכלנית 8, עיר-סינתטית", title="שומה מוקדמת")
    earlier = c.o.value(d2, "6", as_of("2022-09-30"), kind="rate", unit="percent", provenance={"stance": "model_asserted"})
    assert all(T.value_status(c.ws, i) == T.STATUS_AUTO for i in [*c.ids.values(), earlier])
    assert not any(d.uncertain for d in timeline.datums(c.ws))
    blocks = c.o.blocks({*c.ids.values(), earlier})
    assert "uncertain" not in _marks(blocks)


# --- coverage -----------------------------------------------------------------------------------------------------------------------

def test_a_request_component_resting_on_such_a_value_is_not_limited_as_uncertain():
    ws = _coverage_ws()
    vid = _coverage_value(ws, "12", "דמי הניהול")
    ws.values[vid].provenance.update({"stance": "model_asserted", "stated_by": "model_asserted", "scenario": "model_asserted"})
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [vid])
    assert outcomes[1].get("variant") != "uncertain" and "אינן ודאיות" not in gap


def test_a_request_component_resting_on_an_asserted_kind_or_unit_is_still_limited_as_uncertain():
    ws = _coverage_ws()
    vid = _coverage_value(ws, "12", "דמי הניהול", asserted=True)  # the unit asserted
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [vid])
    assert outcomes[1]["limitation"] == "not_verifiable" and "אינן ודאיות" in gap
