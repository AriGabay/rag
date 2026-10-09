"""The typed request analysis (round 7 U2, KTD1, R1-R3, R5): a request is represented as typed components before
the answer, frozen with stable ids that no workspace handle uses, on a first turn by one small call and on a
follow-up by the resolve call's own schema (no extra call). A component part that is invalid falls back to the
judge's derivation without losing the rest of a follow-up's resolution.

The analysing model is scripted: these tests prove what the server does with what it returns (freezing, ids,
parent links, conditional flags, the user's evidence for a parameter, the fallback), not the model's judgement;
the policy is checked to state the rules the model is held to. Synthetic requests only; no database."""

from __future__ import annotations

import re
import threading
import time

import pytest

from app.chat import request as R
from app.chat import resolve
from app.chat.verify import TurnRequirements, _Coverage
from app.config import get_settings
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_agent import component
from tests.support.scripted_provider import ScriptedProvider

MIXED = ("פרט את ייעוד הקרקע, את מספר יחידות הדיור ואת מספר הקומות לפי התכנית המאושרת. כתוב בסגנון מקצועי, וצרף "
         "מראה מקום מדויק לכל נתון.")


def _provider(components) -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.RESOLVE, components if not isinstance(components, list) else {"components": components})
    return p


def _analyze(components, question: str = MIXED) -> R.Analysis:
    return R.analyze(_provider(components), question, deadline=None)


def _kinds(items: list[dict]) -> list[str]:
    return [i["kind"] for i in items]


# --- what a first turn's analysis freezes -------------------------------------------------------------------------

def test_a_mixed_request_freezes_its_information_and_its_instructions_apart():
    a = _analyze([component("ייעוד הקרקע לפי התכנית המאושרת"), component("מספר יחידות הדיור"),
                  component("מספר הקומות"), component("כתיבה בסגנון מקצועי", "instruction"),
                  component("מראה מקום מדויק לכל נתון", "instruction")])
    assert a.status == "ok"
    assert _kinds(a.items) == ["information"] * 3 + ["instruction"] * 2
    assert [i["id"] for i in a.items] == ["N1", "N2", "N3", "N4", "N5"]
    assert not any(i["calculation"] for i in a.items)
    # one call, on the resolve purpose's settings, recorded under its own label
    assert a.usage["purpose"] == "request" and a.usage["status"] == "ok"


def test_the_analysis_call_uses_the_resolve_purpose_and_the_question_alone():
    p = _provider([component("השווי למ\"ר")])
    R.analyze(p, "מה השווי למ\"ר?", deadline=None)
    (c,) = p.calls
    assert c.purpose == Purpose.RESOLVE and c.schema is R.RequestAnalysis
    assert "מה השווי למ\"ר?" in c.input and c.instructions == R.POLICY


def test_the_policy_states_the_kinds_and_the_rules_the_model_is_held_to():
    for kind in R.KINDS:
        assert kind in R.COMPONENTS_POLICY
    for source in ("given_by_user", "not_given_by_user"):
        assert source in R.COMPONENTS_POLICY
    # instructions are never information; a split never goes beyond what the user asked; a request to distinguish
    # statuses is one information and one instruction component (R5); the analysis answers nothing
    assert "instruction" in R.COMPONENTS_POLICY and "compares" in R.COMPONENTS_POLICY
    assert R.COMPONENTS_POLICY in R.POLICY and R.COMPONENTS_POLICY in resolve.POLICY


def test_the_policy_makes_a_parameter_only_what_the_user_decides_never_a_datum_of_the_documents():
    """A parameter is what only the user can decide for the calculation (a scenario's rate or change, a period, a
    choice between alternatives); a datum expected in the documents (an amount, a threshold, a profit the calculation
    takes from the report) is never one, even when the calculation depends on it and the request names it (U9: the
    analysis marked "the developer profit in the calculation" and "the minimal required threshold" as parameters)."""
    rule = R.COMPONENTS_POLICY[R.COMPONENTS_POLICY.index("- parameters"):]
    rule = " ".join(rule[:rule.index("\n- ", 1)].split())
    assert "רק מה שהמשתמש עצמו צריך להחליט" in rule
    for example in ("שיעור או שינוי של תרחיש", "תקופה", "בחירה בין חלופות"):
        assert example in rule
    assert "נתון שאמור להימצא במסמכים לעולם אינו parameter" in rule
    assert "וגם כשהבקשה נוקבת בו בשמו" in rule
    assert R.COMPONENTS_POLICY in resolve.POLICY  # a follow-up's resolution is held to the same rule


def test_distinguishing_statuses_is_one_information_and_one_instruction_component():
    a = _analyze([component("הסטטוס של כל נתון בתכנית: מאושר, מוצע או הנחת השמאי"),
                  component("הצגה ברורה של הסטטוס ליד כל נתון", "instruction")],
                 "פרט את נתוני התכנית והבחן בין מאושר למוצע.")
    assert _kinds(a.items) == ["information", "instruction"]


def test_as_far_as_they_appear_sets_the_conditional_flag_on_the_listed_children():
    a = _analyze([component("נתוני התכנית", id="1", conditional=True),
                  component("שימושים", id="1.1", parent="1"), component("יחידות דיור", id="1.2", parent="1",
                                                                       conditional=True)],
                 "פרט את השימושים ואת יחידות הדיור, ככל שהם מופיעים.")
    assert [(i["id"], i["parent"], i["conditional"]) for i in a.items] == [
        ("N1", "", True), ("N1.1", "N1", True), ("N1.2", "N1", True)]


def test_a_compound_request_becomes_children_of_one_parent():
    a = _analyze([component("נתוני התכנית", id="a"),
                  *(component(t, id=f"a{n}", parent="a") for n, t in enumerate(("שימושים", "יחידות דיור", "שטחים",
                                                                                 "גובה")))],
                 "פרט את נתוני התכנית: שימושים, יחידות דיור, שטחים וגובה.")
    parent, *children = a.items
    assert parent["id"] == "N1" and parent["parent"] == ""
    assert [c["id"] for c in children] == ["N1.1", "N1.2", "N1.3", "N1.4"]
    assert all(c["parent"] == "N1" for c in children)
    assert [c["text"] for c in children] == ["שימושים", "יחידות דיור", "שטחים", "גובה"]


def test_a_child_listed_before_its_parent_keeps_its_link_and_the_parent_comes_first():
    a = _analyze([component("גובה", id="2", parent="1"), component("נתוני התכנית", id="1")])
    assert [(i["id"], i["text"], i["parent"]) for i in a.items] == [("N1", "נתוני התכנית", ""),
                                                                    ("N1.1", "גובה", "N1")]


COST_RISE = "מה יהיה הרווח אם העלויות יעלו?"


def _rate(source: str, quote: str = "") -> dict:
    return {"name": "שיעור העלייה של העלויות", "source": source, "quote": quote}


def test_a_cost_increase_without_a_rate_has_a_parameter_the_user_did_not_give():
    a = _analyze([component("הרווח אם העלויות יעלו", "calculation", parameters=[_rate("not_given_by_user")])],
                 COST_RISE)
    (c,) = a.items
    assert c["kind"] == "calculation" and c["calculation"]
    assert c["parameters"] == [{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""}]


def test_a_rate_the_user_gave_is_a_parameter_given_by_the_user():
    a = _analyze([component("הרווח אם העלויות יעלו ב-5%", "calculation", parameters=[_rate("given_by_user", "ב-5%")])],
                 "מה יהיה הרווח אם העלויות יעלו ב-5%?")
    assert a.items[0]["parameters"][0]["source"] == "given_by_user"


def test_a_parameter_claimed_as_given_without_the_users_words_is_not_given():
    a = _analyze([component("הרווח אם העלויות יעלו", "calculation", parameters=[_rate("given_by_user", "ב-8%")])],
                 COST_RISE)
    assert a.items[0]["parameters"][0]["source"] == "not_given_by_user"
    assert any("parameter" in d for d in a.decisions)


def test_a_comparison_between_two_appraisals_names_both_subjects():
    a = _analyze([component("ההפרש בין השווי בשתי השומות", "calculation",
                            compares=["השומה ברחוב הגפן 3", "השומה ברחוב התאנה 8"])],
                 "מה ההפרש בין השווי בשומה ברחוב הגפן 3 לבין השווי בשומה ברחוב התאנה 8?")
    assert a.items[0]["compares"] == ["השומה ברחוב הגפן 3", "השומה ברחוב התאנה 8"]


def test_parameters_and_comparisons_belong_to_calculations_only():
    a = _analyze([component("ייעוד הקרקע", parameters=[_rate("not_given_by_user")], compares=["א", "ב"])])
    assert a.items[0]["parameters"] == [] and a.items[0]["compares"] == []


# --- ids: stable, and never a workspace handle -----------------------------------------------------------------

def test_component_ids_never_collide_with_workspace_handles():
    # the model's own labels may look like handles; the server's ids never do
    a = _analyze([component("השווי", id="S1"), component("דמי השכירות", id="Q1"), component("פירוט", id="V2",
                                                                                           parent="Q1")])
    ids = [i["id"] for i in a.items]
    assert ids == ["N1", "N2", "N2.1"] and a.items[2]["parent"] == "N2"
    assert all(re.fullmatch(r"N\d+(?:\.\d+)*", i) for i in ids)
    assert R.ID_PREFIX not in R.HANDLE_PREFIXES
    for prefix in ("S", "M", "V", "A", "C", "P", "Q", "H", "F", "E", "D", "T", "K", "R", "§"):
        assert prefix in R.HANDLE_PREFIXES
    # nor does the judge's fallback derivation use a handle's prefix (``Q#`` is a cached value)
    turn = TurnRequirements()
    turn.freeze([_score("השווי")])
    assert [i["id"] for i in turn.items] == ["N1"]


def _score(text: str, **kw):
    from app.chat.verify import JudgeRequirement

    return JudgeRequirement(text=text, status="full", **kw)


@pytest.mark.parametrize("components, why", [
    ([component("שימושים", id="1", parent="9")], "unknown parent"),
    ([component("א", id="1", parent="2"), component("ב", id="2", parent="1")], "cycle"),
    ([component("א", id="1", parent="1")], "own parent"),
    ([component("א", id="1"), component("ב", id="1")], "duplicate"),
    ([component("  ", id="1")], "empty text"),
])
def test_a_component_list_with_a_broken_structure_is_invalid(components, why):
    a = _analyze(components)
    assert a.status == "invalid" and a.items is None, why


@pytest.mark.parametrize("response, status", [(CallStatus.TIMEOUT, "timeout"), (CallStatus.ERROR, "error"),
                                              ({"components": []}, "empty"),
                                              ({"components": [{"id": "1", "text": "א", "kind": "wish"}]},
                                               "invalid")])
def test_an_analysis_that_fails_or_says_nothing_leaves_no_components(response, status):
    a = R.analyze(_provider(response), MIXED, deadline=None)
    assert a.status == status and a.items is None
    assert a.usage["purpose"] == "request"


# --- the worker thread -----------------------------------------------------------------------------------------

def test_the_analysis_runs_in_a_worker_thread_and_is_collected_once():
    gate = threading.Event()

    def slow(instructions, input):
        gate.wait(5)
        return {"components": [component("השווי")]}

    p = ScriptedProvider()
    p.on(Purpose.RESOLVE, slow)
    pending = R.start(p, "מה השווי?", deadline=time.monotonic() + 30)
    assert not pending.done()  # started, not waited for
    gate.set()
    a = pending.wait(lambda: False)
    assert a.status == "ok" and [i["id"] for i in a.items] == ["N1"]


def test_an_analysis_that_does_not_finish_in_time_is_abandoned_as_a_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_timeout_resolve_seconds", 1.2)
    monkeypatch.setattr(R, "ANALYSIS_GRACE_SECONDS", 0.0)
    gate = threading.Event()
    p = ScriptedProvider()
    p.on(Purpose.RESOLVE, lambda i, x: gate.wait(10) and {"components": [component("השווי")]})
    started = time.monotonic()
    pending = R.start(p, "מה השווי?", deadline=started + 100)
    a = pending.wait(lambda: False)
    gate.set()
    assert a.status == "timeout" and a.items is None and a.usage["status"] == "timeout"
    assert time.monotonic() - started < 5


def test_waiting_for_the_analysis_stops_when_the_turn_is_cancelled():
    gate = threading.Event()
    p = ScriptedProvider()
    p.on(Purpose.RESOLVE, lambda i, x: gate.wait(10) and {"components": []})
    pending = R.start(p, "מה השווי?", deadline=time.monotonic() + 30)
    a = pending.wait(lambda: True)
    gate.set()
    assert a.status == "cancelled" and a.items is None


# --- the judge scores the frozen components ---------------------------------------------------------------------

def test_the_judge_is_given_the_frozen_components_with_their_kind_parent_and_condition():
    a = _analyze([component("נתוני התכנית", id="1", conditional=True), component("גובה", id="2", parent="1"),
                  component("מראה מקום לכל נתון", "instruction")])
    turn = TurnRequirements()
    turn.adopt(a.items, "analysis")
    rendered = _Coverage(turn, ["רמז שאינו דרישה"], "?").render()
    assert "<derive_requirements>" not in rendered and "<hint>" not in rendered
    assert '<requirement id="N1" kind="information" conditional="true">' in rendered
    assert '<requirement id="N1.1" kind="information" parent="N1" conditional="true">' in rendered
    # an instruction reaches the judge in a list of its own, scored against the shown answer (round 7 KTD2)
    assert '<requirement id="N2" kind="instruction">' in rendered.split("<instructions>")[1]
    assert turn.derived and turn.origin == "analysis" and turn.fallback is None


def test_the_agents_item_lists_every_component_with_its_kind_and_missing_parameters():
    a = _analyze([component("נתוני התכנית", id="1", conditional=True), component("גובה", id="2", parent="1"),
                  component("הרווח אם העלויות יעלו", "calculation", parameters=[_rate("not_given_by_user")]),
                  component("מראה מקום לכל נתון", "instruction")], COST_RISE)
    text = R.block(a.items)
    for i in a.items:
        assert f"{i['id']} " in text and i["text"] in text
    assert "שיעור העלייה של העלויות" in text


# --- a follow-up: the resolve call's own schema (no extra call) ----------------------------------------------------

def _resolution(**kw) -> dict:
    return {"relation": "new_question", "scope": "entity", "standalone_question": "מה דמי השכירות בנכס?",
            "changed_fields": [], "metric_kind": "rent", "unit": "unknown", "scale": "unknown", "period": "unknown",
            "area_basis": "", "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


class _Recording(ScriptedProvider):
    def __init__(self):
        super().__init__()
        self.budgets: list = []

    def structured(self, purpose, instructions, input, schema, *, max_output_tokens=None, **kw):
        self.budgets.append(max_output_tokens)
        return super().structured(purpose, instructions, input, schema, max_output_tokens=max_output_tokens, **kw)


def _resolve(response, message: str = "ומה דמי השכירות?") -> tuple[resolve.Request | None, _Recording]:
    p = _Recording()
    p.on(Purpose.RESOLVE, response)
    usage: list = []
    out = resolve.resolve(p, None, [], message, [], lambda ids: set(ids), [], usage)
    assert [u["purpose"] for u in usage] == ["resolve"]  # one call: the components come with the resolution
    return out, p


def test_a_follow_ups_components_come_from_the_resolve_call():
    req, p = _resolve(_resolution(components=[component("דמי השכירות"), component("בטבלה", "instruction")]))
    assert req is not None and req.components_status == "ok"
    assert [(i["id"], i["kind"]) for i in req.components] == [("N1", "information"), ("N2", "instruction")]
    assert p.budgets[0] >= 4000  # the output budget fits the component list
    assert "components" in resolve.ResolvedRequest.model_json_schema()["properties"]


def test_a_follow_up_whose_component_part_does_not_validate_keeps_the_rest_of_its_resolution():
    req, _ = _resolve(_resolution(components=[{"id": "1", "text": "דמי השכירות", "kind": "wish"}]))
    assert req is not None and req.standalone_question == "מה דמי השכירות בנכס?" and req.metric_kind == "rent"
    assert req.components is None and req.components_status == "invalid"
    assert req.resolution["decisions"]["components"].startswith("invalid")


def test_a_follow_up_whose_components_are_broken_keeps_the_rest_of_its_resolution():
    req, _ = _resolve(_resolution(components=[component("דמי השכירות", id="1", parent="7")]))
    assert req is not None and req.standalone_question == "מה דמי השכירות בנכס?"
    assert req.components is None and req.components_status == "invalid"


def test_an_older_resolution_without_components_is_valid_and_has_none():
    req, _ = _resolve(_resolution())
    assert req is not None and req.components is None and req.components_status == "empty"
