"""Round 25 U4 (R1, R7, R11; KTD7, KTD8): the completeness record in the turn diagnostics.

Per component decision (``VerifyReport.requirement_decisions``, served in ``requirements_trace``): the pointer (the surviving units that fill it
and the evidence ids they cite), the open items that capped it (id, label prefix, source id, kind) and the anchor sources offered to the
requirements call; per open item of the ledger diagnostics (``VerifyReport.item_diagnostics``) the gap cause code it got. Ids, counts, codes and
short label prefixes only - never a value, a quote or a sentence of the answer. Every list is bounded and its whole count is kept. With every
detection setting off the keys are absent and the records are those of round 24. The judge is scripted; names and numbers are invented."""

from __future__ import annotations

import json

import pytest

from app.chat import verify
from app.chat.verify import COMPLETENESS_ANCHORS_KEPT, COMPLETENESS_LABEL_CHARS, COMPLETENESS_OPEN_KEPT
from tests.unit.test_coverage import _adj_item, _finish
from tests.unit.test_sibling_check import (
    COND_ASK,
    COND_ROWS,
    ROWS,
    SINGLE_ASK,
    TOTAL_ONLY,
    _check,
    _cond_check,
    _switch,
    _table_ws,
)
from tests.unit.test_sibling_check import (
    HEAD as _HEAD,
)

NEW_KEYS = {"open_items", "anchors", "counts"}


def _decision(report, cid: str = "N1") -> dict:
    return report.requirement_decisions[cid]


def test_a_capped_component_records_its_four_open_items_with_ids_source_and_kind_sibling(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)
    d = _decision(report)
    assert d["status"] == "partial" and d["caps"] == ["omitted_items"]
    assert [i["label"] for i in d["open_items"]] == [r.split(" | ")[0] for r in ROWS]
    assert all(set(i) == {"id", "label", "source", "kind"} for i in d["open_items"])
    assert {i["source"] for i in d["open_items"]} == {"S2"} and {i["kind"] for i in d["open_items"]} == {"sibling"}
    assert [i["id"] for i in d["open_items"]] == report.requirement_outcomes()[0]["omitted_items"]  # the ids of the ledger's items
    assert d["counts"] == {"units": len(d["units"]), "cites": len(d["cites"]), "open_items": 4, "anchors": 1}
    assert d["anchors"] == {"sources": ["S2"], "count": 1, "sources_left": 0}


def test_the_record_carries_no_value_quote_or_sentence(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)
    dumped = json.dumps(_decision(report), ensure_ascii=False)
    for text in ("2,140", "610", "380", "270", "3,400", "סך הכול", "הוא 3,400"):
        assert text not in dumped, text


def test_the_requirements_trace_carries_the_same_decision(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)
    traced = report.requirements_trace()[0]["decision"]
    assert traced["open_items"] == _decision(report)["open_items"] and traced["anchors"]["sources"] == ["S2"]
    assert json.loads(json.dumps(traced, ensure_ascii=False)) == traced  # (it is stored as JSON)


def test_a_full_component_records_an_empty_open_list_and_the_pointer(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY, ask=SINGLE_ASK)
    d = _decision(report)
    assert d["status"] == "full" and d["open_items"] == [] and d["units"] and d["cites"] == ["S2"]
    assert d["counts"]["open_items"] == 0 and d["anchors"]["count"] == 0 and d["anchors"]["sources"] == []


@pytest.mark.parametrize("setting", ["chat_sibling_anchors", "chat_condition_check", "chat_own_attribution"])
def test_any_one_detection_setting_turns_the_record_on(monkeypatch, setting):
    _switch(monkeypatch, setting)
    report, *_ = _check(TOTAL_ONLY)
    assert NEW_KEYS <= set(_decision(report))


def test_with_every_detection_setting_off_no_key_is_added(monkeypatch):
    _switch(monkeypatch)
    report, *_ = _check(TOTAL_ONLY)
    d = _decision(report)
    assert not (NEW_KEYS & set(d)) and "units" not in d and "cites" not in d
    assert not NEW_KEYS & set(report.requirements_trace()[0]["decision"])
    # and the item records carry no cause
    assert report.item_diagnostics()["items"] and all("gap_cause" not in i for i in report.item_diagnostics()["items"])


# --- the kinds ---------------------------------------------------------------------------------------------------------------

def test_a_condition_item_is_recorded_with_kind_condition(monkeypatch):
    _switch(monkeypatch, "chat_condition_check")
    report, *_ = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    d = _decision(report)
    assert d["status"] == "partial"
    (item,) = d["open_items"]
    assert item["kind"] == "condition" and item["source"] == "S2" and item["label"] == "שווי הקרקע"
    assert "בכפוף" not in json.dumps(d, ensure_ascii=False)  # the condition's words are not in the record


def test_an_agent_item_the_answer_leaves_out_is_recorded_with_kind_agent(monkeypatch):
    _switch(monkeypatch, "chat_condition_check")
    report, *_ = _check("הנתון נקרא מהמסמך [S2].", ws=_table_ws("\n".join(COND_ROWS)), ask=COND_ASK,
                        raw=[_adj_item("שווי הקרקע", "שווי הקרקע | 4,820,000 | בכפוף לאישור התכנית המפורטת", "4,820,000")])
    kinds = {i["kind"] for i in _decision(report)["open_items"]}
    assert kinds == {"agent"}


def test_a_part_only_the_judge_named_is_recorded_with_kind_judge_support(monkeypatch):
    _switch(monkeypatch, "chat_condition_check")
    report, *_ = _check("הנתון נקרא מהמסמך [S2].", ws=_table_ws("\n".join(COND_ROWS)), ask=COND_ASK)
    report.add_support_items({"N1": [{"id": "S2", "quote": "שווי המבנה | 1,150,000 |"}]})
    kinds = {i["kind"] for i in _decision(report)["open_items"]}
    assert kinds == {"judge_support"}


def test_an_open_items_label_never_holds_its_value(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)
    report.candidate_pool = {}
    note = {"voters": [], "caps": [], "open": [{"id": "I1", "label": "שיעור היוון למחסנים 9.1%", "value": "9.1%", "quote": "שיעור היוון למחסנים 9.1%",
                                               "source": "S3", "origin": "source", "candidate": True}]}
    d = report._decisions([{"id": "N1", "status": "partial", "units": [], "children": [], "support": []}], {"N1": note})["N1"]
    (item,) = d["open_items"]
    assert item["label"] == "שיעור היוון למחסנים" and item["kind"] == "sibling" and item["source"] == "S3"


# --- bounds ------------------------------------------------------------------------------------------------------------------

def test_a_long_label_is_clipped_to_the_prefix_and_the_open_list_is_bounded_with_its_whole_count(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    long_label = "רכיב בנייה ארוך מאוד שתיאורו ממשיך " * 4
    rows = [f"{long_label}{n} | {1000 + n}" for n in range(COMPLETENESS_OPEN_KEPT + 3)]
    report, *_ = _check(TOTAL_ONLY, ws=_table_ws("\n".join([*_HEAD, "סך הכול זכויות בנייה | 3,400", *rows])))
    d = _decision(report)
    assert len(d["open_items"]) == min(COMPLETENESS_OPEN_KEPT, d["counts"]["open_items"])
    assert d["counts"]["open_items"] > 0 and all(len(i["label"]) <= COMPLETENESS_LABEL_CHARS for i in d["open_items"])


def test_the_lists_of_units_cites_and_anchors_are_bounded_and_their_counts_stay_whole(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)
    outcome = {"id": "N1", "status": "partial", "units": list(range(100)), "children": [], "support": []}
    report.units = list(report.units) * 0 + [type("U", (), {"index": n, "ids": [f"S{n}"]})() for n in range(100)]
    note = {"voters": [], "caps": [], "open": []}
    d = report._decisions([outcome], {"N1": note})["N1"]
    kept = verify.CONFIRM_RECORD_KEPT
    assert len(d["units"]) == kept and len(d["cites"]) == kept
    assert d["counts"]["units"] == 100 and d["counts"]["cites"] == 100
    pool = {f"K{n}": type("C", (), {"source": f"S{n + 2}"})() for n in range(COMPLETENESS_ANCHORS_KEPT + 5)}
    report.candidate_pool = pool
    d = report._decisions([outcome], {"N1": note})["N1"]
    assert len(d["anchors"]["sources"]) == COMPLETENESS_ANCHORS_KEPT and d["anchors"]["count"] == COMPLETENESS_ANCHORS_KEPT + 5


# --- the cause code of each open item in the ledger diagnostics ---------------------------------------------------------------------

def test_each_open_item_of_the_ledger_diagnostics_has_the_gap_cause_code_it_got(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, ws, answer, ledger, turn = _check(TOTAL_ONLY)
    _finish(ws, answer, report, turn)
    items = report.item_diagnostics()["items"]
    open_ = [i for i in items if i["state"] == "omitted"]
    assert len(open_) == 4 and {i["gap_cause"] for i in open_} == {"not_completed_time"}
    assert [i["gap_cause"] for i in open_] == [g["cause"] for g in report.item_gaps]  # the code the line was written with


def test_the_recorded_cause_of_an_item_is_the_one_its_line_got_and_a_covered_item_has_none(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, ws, answer, ledger, turn = _check(TOTAL_ONLY, raw=[_adj_item("סך הכול זכויות בנייה", "סך הכול זכויות בנייה | 3,400", "3,400")])
    _finish(ws, answer, report, turn)
    first = next(s["id"] for s in report.item_states() if s["state"] == "omitted")
    report.gap_causes[first] = "not_completed_steps"
    _finish(ws, answer, report, turn)
    items = report.item_diagnostics()["items"]
    by_state = {s: [i for i in items if i["state"] == s] for s in ("omitted", "covered")}
    assert by_state["covered"] and all("gap_cause" not in i for i in by_state["covered"])
    assert sorted(i["gap_cause"] for i in by_state["omitted"]).count("not_completed_steps") == 1
    assert {i["gap_cause"] for i in by_state["omitted"]} <= {"not_completed_steps", "not_completed_time"}


def test_the_cause_code_is_a_code_never_prose(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, ws, answer, ledger, turn = _check(TOTAL_ONLY)
    _finish(ws, answer, report, turn)
    for i in report.item_diagnostics()["items"]:
        if "gap_cause" in i:
            assert i["gap_cause"].isascii() and " " not in i["gap_cause"]


def test_a_cause_that_cannot_be_read_leaves_the_item_record_as_it_was_and_never_fails_the_diagnostics(monkeypatch):
    from app.chat import coverage

    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY)  # (no item line was written yet: the cause is read from the attempt record)
    monkeypatch.setattr(coverage, "gap_cause", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    items = report.item_diagnostics()["items"]
    assert items and all("gap_cause" not in i for i in items)
