"""The held-out scorer (eval/general.py) on synthetic answers: for every rule changed in
docs/evaluation/scorer-changes.md a correct answer passes and a negative control fails (wrong value, opposite
polarity, wrong document, a partial computation presented as complete, the value of another attribute)."""

from __future__ import annotations

import operator
from decimal import ROUND_HALF_UP, Decimal

import pytest

from eval.flows import Flow
from eval.general import (
    QUESTIONS,
    QUESTIONS_V2,
    SET_LABELS,
    Hooks,
    TurnRun,
    effective,
    fact_value_stated,
    facts,
    load_items,
    numbers_in,
    replaced_docs,
    rescore,
    score_turn,
)
from eval.truth import USERS

V1 = {i["id"]: i for i in load_items(QUESTIONS)}
V2 = {i["id"]: i for i in load_items(QUESTIONS_V2)}
HOOKS = Hooks(client=lambda user: None, doc_of=lambda s: s.get("doc"), plan_of=lambda q: None,
              forbidden=lambda user, rule: set())
F = facts()


# --- builders -------------------------------------------------------------------------------------------

def src(eid: str, doc: str, pages: list[int], snippet: str = "", **kw) -> dict:
    return {"evidence_id": eid, "document_id": doc, "version_id": doc, "doc": doc, "page_list": pages,
            "snippet": snippet, **kw}


def claim(text: str, *eids: str) -> dict:
    return {"text": text, "kind": "explicit", "evidence_ids": list(eids)}


def ans(kind: str = "content", text: str = "", claims=(), sources=(), **kw) -> dict:
    return {"kind": kind, "text": text, "claims": list(claims), "sources": list(sources), **kw}


def plan(task: str, relation: str = "new_question", tools=()) -> dict:
    return {"turn_plan": {"task_type": task, "turn_relation": relation, "steps": [{"tool": t} for t in tools]}}


def run(answer: dict, plan_: dict | None = None, steps=(), before=None, after=None, previous=None) -> TurnRun:
    flow = Flow(answer=answer, conversation_id="c", transcript=[answer], latencies_ms=[1.0], question_ids=["q"])
    return TurnRun(flow, plan_, [{"tool": t} for t in steps], None, before or {}, after or {}, previous=previous)


def score(item: dict, answer: dict, index: int = 0, **kw) -> dict:
    return score_turn(HOOKS, item, index, run(answer, **kw))


def facet_reasons(result: dict, facet: str) -> list[str]:
    return [r for r in result["reasons"] if r.startswith(facet + ":")]


def single(expect: dict, ask: str = "שאלה", terms=(), **item) -> dict:
    return {"id": "T", "category": "test", "terms": list(terms), "turns": [{"ask": ask, "expect": expect}], **item}


# --- token matching (#10, S2, S3, L4) ------------------------------------------------------------------

def test_numbers_are_whole_tokens_and_never_unit_exponents_or_ordinals():
    assert numbers_in("60 m2 main area") == {Decimal(60)}
    assert numbers_in("הגרסה השנייה, הבניין השני") == set()
    assert numbers_in("מחסן 120 מ״ר") == {Decimal(120)}  # 12 is not "found" in 120
    assert Decimal(12) not in numbers_in("שטח של 120 מ״ר")


@pytest.mark.parametrize(("text", "value"), [
    ("קומה אחת", 1), ("שתי מרפסות", 2), ("שני מקומות", 2), ("בשלושה חדרים", 3), ("ששת הבניינים", 6),
    ("עשר שנים", 10), ("אחת עשרה קומות", 11), ("שתים עשרה דירות", 12), ("שבעה עשר", 17), ("עשרים", 20),
])
def test_hebrew_number_words_both_genders_and_construct_forms(text, value):
    assert Decimal(value) in numbers_in(text)


def test_teen_number_words_are_not_read_as_their_parts():
    assert numbers_in("אחת עשרה") == {Decimal(11)}


def test_a_number_next_to_another_attribute_is_not_the_value():
    safe_room = F["H1-F01"]  # 12 m2
    assert fact_value_stated(safe_room, "בדירה ממ״ד בשטח 12 מ״ר ומחסן בשטח 5 מ״ר")
    assert not fact_value_stated(safe_room, "בדירה ממ״ד בשטח 9.5 מ״ר ומחסן בשטח 12 מ״ר")
    assert not fact_value_stated(safe_room, "מחסן 12 מ״ר")


def test_hebrew_values_match_whole_tokens():
    zoning = F["H5-F12"]  # מגורים א׳
    assert fact_value_stated(zoning, "ייעוד המגרש הוא מגורים א'")
    assert not fact_value_stated(zoning, "ייעוד המגרש הוא מגורים אחרים")
    assert not fact_value_stated(zoning, "ייעוד המגרש הוא מגורים ב׳")


def test_descriptive_value_needs_its_own_tokens_not_any_number():
    status = F["H4v2-F09"]  # approved (May 2023)
    assert fact_value_stated(status, "התכנית אושרה למתן תוקף במאי 2023")
    assert not fact_value_stated(status, "התכנית הופקדה בשנת 2023")  # any 2023 used to pass (L4)
    assert not fact_value_stated(status, "התכנית הופקדה במאי 2023")  # month + year, but another status
    assert not fact_value_stated(status, "התכנית טרם אושרה במאי 2023")  # the opposite


def test_descriptive_value_with_a_unit_exponent_and_number_words():
    rights = F["H2-F11"]  # "60 m2 main area": the 2 of m2 is not a number of the value (S2)
    assert fact_value_stated(rights, "יתרה של כ-60 מ״ר שטח עיקרי שטרם מומשה")
    assert not fact_value_stated(rights, "יתרה של כ-61 מ״ר שטח עיקרי שטרם מומשה")
    floor = F["H4v2-F10"]  # "1 additional floor": a number word counts (S3)
    assert fact_value_stated(floor, "התכנית מאפשרת תוספת קומה אחת לבניין")
    assert not fact_value_stated(floor, "התכנית מאפשרת תוספת שתי קומות לבניין")


def test_descriptive_negative_value_needs_a_negation():
    old_status = {"document": "H4", "page": 2, "attribute": "planning_status",
                  "value": "deposited, not yet approved", "quote": "נמצאת בשלב הפקדה וטרם אושרה"}
    assert fact_value_stated(old_status, "התכנית נמצאת בשלב הפקדה וטרם אושרה")
    assert not fact_value_stated(old_status, "התכנית אושרה")


# --- L1: boolean and "none" values need the right polarity ---------------------------------------------

def test_absence_needs_a_negation():
    no_elevator = F["H8-F03"]  # False
    assert fact_value_stated(no_elevator, "הבניין אינו כולל מעלית")
    assert fact_value_stated(no_elevator, "בבניין בן ארבע קומות ללא מעלית")
    assert not fact_value_stated(no_elevator, "בבניין יש מעלית")
    no_storage = F["H5-F11"]  # False, table cell "מחסן | אין"
    assert fact_value_stated(no_storage, "מחסן: אין")
    assert not fact_value_stated(no_storage, "לדירה צמוד מחסן בקומת המרתף")


def test_presence_must_not_be_negated():
    easement = F["K7-F11"]  # True
    assert fact_value_stated(easement, "על המגרש רשומה זיקת הנאה למעבר הציבור", ("זיקת הנאה",))
    assert not fact_value_stated(easement, "לא רשומה על המגרש זיקת הנאה", ("זיקת הנאה",))


def test_none_value_needs_the_stated_absence():
    no_renovation = F["H1-F07"]  # none
    assert fact_value_stated(no_renovation, "הדירה במצב חדש ולא בוצעו בה שינויים מאז האכלוס", ("שופצה",))
    assert not fact_value_stated(no_renovation, "הדירה שופצה בשנת 2021", ("שופצה",))


def test_zero_is_stated_by_a_stated_absence():
    no_allowance = F["K1-F11"]  # 0
    assert fact_value_stated(no_allowance, "לא הובא בחשבון ניכוי בגין אובדן הכנסות")
    assert not fact_value_stated(no_allowance, "הובא בחשבון ניכוי של 5% בגין אובדן הכנסות")


def test_bool_value_in_a_content_answer_is_scored_by_polarity():
    item = V1["GQ11"]  # H5-F11 False, cited at H5 p2
    good = ans(text="אין מחסן", claims=[claim("לדירה אין מחסן", "E1")], sources=[src("E1", "H5", [2], "מחסן | אין")])
    bad = ans(text="יש מחסן", claims=[claim("לדירה יש מחסן", "E1")], sources=[src("E1", "H5", [2], "מחסן | אין")])
    assert not facet_reasons(score(item, good, plan_=plan("answer")), "values")
    assert facet_reasons(score(item, bad, plan_=plan("answer")), "values")


# --- S1: a locate answer states values in its cited snippets -------------------------------------------

def _locate(snippet: str, doc: str = "H8", **kw) -> dict:
    return ans(text=f"• {doc} — עמ׳ 1 [E1]", sources=[src("E1", doc, [1], snippet)], **kw)


def test_locate_template_states_the_value_in_its_snippet():
    item = V1["GQ14"]
    ok = score(item, _locate("נבנה בשנת 1972 ואינו כולל מעלית."), plan_=plan("locate"))
    assert ok["ok"], ok["reasons"]


def test_locate_template_with_the_opposite_snippet_or_another_document_fails():
    item = V1["GQ14"]
    assert facet_reasons(score(item, _locate("בבניין פועלת מעלית"), plan_=plan("locate")), "values")
    other = score(item, _locate("בבניין בן ארבע קומות ללא מעלית", doc="H6"), plan_=plan("locate"))
    assert facet_reasons(other, "sources") and facet_reasons(other, "values")


def test_a_fallback_answer_with_dropped_claims_is_not_a_template():
    item = V1["GQ14"]
    r = score(item, _locate("נבנה בשנת 1972 ואינו כולל מעלית.", dropped_claims=2), plan_=plan("locate"))
    assert facet_reasons(r, "values")


# --- L5: a page-less source meets a page only for a page-less document --------------------------------

def test_pageless_source_does_not_meet_a_page_of_a_paged_document():
    item = single({"outcome": "answer", "sources": {"all_of": [{"doc": "H1", "page": 1}]}})
    assert facet_reasons(score(item, ans(text="x", sources=[src("E1", "H1", [])])), "sources")
    assert not facet_reasons(score(item, ans(text="x", sources=[src("E1", "H1", [1])])), "sources")


def test_pageless_source_meets_a_docx_document():
    item = single({"outcome": "answer", "sources": {"all_of": [{"doc": "H7", "page": None}]}})
    assert not facet_reasons(score(item, ans(text="x", sources=[src("E1", "H7", [])])), "sources")


# --- S4: a clarification raised by the computation tool ----------------------------------------------

def _clarification(key: str) -> dict:
    return ans("clarification", text="לאיזה נתון הכוונה?", clarification={"key": key, "options": []})


def test_tool_raised_clarification_counts_as_the_expected_clarification():
    r = score(V1["GQ51"], _clarification("data_kind"), plan_=plan("compute", tools=["compute_records"]))
    assert not facet_reasons(r, "task_type") and not facet_reasons(r, "outcome"), r["reasons"]


def test_tool_clarification_with_another_key_or_without_a_compute_tool_fails():
    wrong_key = score(V1["GQ51"], _clarification("area_type"), plan_=plan("compute", tools=["compute_records"]))
    assert facet_reasons(wrong_key, "task_type") and facet_reasons(wrong_key, "outcome")
    no_tool = score(V1["GQ51"], _clarification("data_kind"), plan_=plan("compute", tools=["search"]))
    assert facet_reasons(no_tool, "task_type")
    answer_plan = score(V1["GQ51"], _clarification("data_kind"), plan_=plan("answer", tools=["compute_records"]))
    assert facet_reasons(answer_plan, "task_type")


# --- S5: new_question and topic_change are equivalent only with checked state --------------------------

def _gq55_turn2(relation: str, attribute_after: str | None) -> dict:
    a = ans(text="מגורים ב׳", claims=[claim("ייעוד המגרש הוא מגורים ב׳", "E1")], sources=[src("E1", "H3", [2])])
    return score(V1["GQ55"], a, index=1, plan_=plan("answer", relation),
                 before={"context": {"attribute": "שטח ממ״ד"}}, after={"context": {"attribute": attribute_after}})


def test_new_question_is_accepted_for_topic_change_when_the_cleared_state_is_checked():
    r = _gq55_turn2("new_question", None)
    assert r["ok"], r["reasons"]


def test_equivalent_relation_still_fails_when_the_state_is_not_cleared_or_the_relation_differs():
    assert facet_reasons(_gq55_turn2("new_question", "שטח ממ״ד"), "state")
    assert facet_reasons(_gq55_turn2("follow_up", None), "relation")
    no_state = single({"relation": "topic_change", "outcome": "answer"})
    assert facet_reasons(score(no_state, ans(text="x"), plan_=plan("answer", "new_question")), "relation")


# --- S6 (answer key) and S7: accepted pages ----------------------------------------------------------

def test_header_page_of_the_same_document_is_valid_but_another_document_is_not():
    base = [src("E1", "H3", [2])]
    claims = [claim("ייעוד המגרש הוא מגורים ב׳", "E1"), claim("הנכס בארלוזורוב 88", "E2")]
    header = score(V1["GQ55"], ans(text="x", claims=claims, sources=base + [src("E2", "H3", [1])]), index=1)
    assert not facet_reasons(header, "source_precision")
    other = score(V1["GQ55"], ans(text="x", claims=claims, sources=base + [src("E2", "H2", [1])]), index=1)
    assert facet_reasons(other, "source_precision")


def test_also_valid_page_binds_a_value_but_another_document_does_not():
    item = V1["GQ57"]
    p2 = score(item, _locate("סגירת המרפסת ללא היתר עלולה לחייב הריסה", doc="H5") | {
        "sources": [src("E1", "H5", [2], "סגירת המרפסת ללא היתר עלולה לחייב הריסה")]})
    assert not facet_reasons(p2, "values")
    other = score(item, ans(text="x", sources=[src("E1", "H8", [1], "היתר בנייה לתוספת חדר")]))
    assert facet_reasons(other, "values")


# --- S10: a meta turn re-shows the previous turn's sources ------------------------------------------

def test_show_sources_is_scored_on_the_previous_turn_sources():
    item = V1["GQ50"]
    previous = ans(text="• D1v2 — עמ׳ 1 [E1]", sources=[src("E1", "D1v2", [1], "היעדר מעלית")])
    same = ans(text="המקורות: [E1]", sources=[src("E1", "D1v2", [1], "היעדר מעלית")])
    r = score(item, same, index=1, plan_=plan("answer", "meta_sources", ["show_sources"]), steps=["show_sources"],
              previous=previous)
    assert not facet_reasons(r, "sources"), r["reasons"]
    added = ans(text="x", sources=[src("E1", "D1v2", [1]), src("E2", "H6", [1])])
    r2 = score(item, added, index=1, plan_=plan("answer", "meta_sources", ["show_sources"]), previous=previous)
    assert facet_reasons(r2, "sources")
    dropped = ans(text="x", sources=[src("E1", "H8", [1])])
    r3 = score(item, dropped, index=1, plan_=plan("answer", "meta_sources", ["show_sources"]), previous=previous)
    assert facet_reasons(r3, "sources")


def test_rescore_supplies_the_previous_turn_of_stored_runs():
    item = V1["GQ50"]
    first = run(ans(text="x", sources=[src("E1", "D1v2", [1])]), plan("locate")).raw()
    second = run(ans(text="x", sources=[src("E1", "D1v2", [1])]), plan("answer", "meta_sources")).raw()
    del first["previous"], second["previous"]
    stored = [{"id": "GQ50", "turn": 1, "raw": first}, {"id": "GQ50", "turn": 2, "raw": second}]
    out = rescore(HOOKS, [item], stored)
    assert not facet_reasons(out[1], "sources"), out[1]["reasons"]


# --- L3: the changed assumptions of a version diff ---------------------------------------------------

def _version_diff(rate_old: str, rate_old_doc: str = "H4") -> dict:
    sources = [src("E1", "H4", [2]), src("E2", "H4v2", [2]), src("E3", rate_old_doc, [2])]
    claims = [claim("בגרסה 1 התכנית נמצאת בשלב הפקדה וטרם אושרה", "E1"),
              claim("בגרסה 2 התכנית אושרה למתן תוקף במאי 2023", "E2"),
              claim(f"בגרסה 1 הובאה בחשבון התאמה בשיעור {rate_old}%", "E3"),
              claim("בגרסה 2 הובאה בחשבון התאמה בשיעור 10%", "E2")]
    return ans(text="x", claims=claims, sources=sources,
               compare={"sides": [{"version_id": "a", "label": "1"}, {"version_id": "b", "label": "2"}]})


def test_every_changed_assumption_must_be_stated_for_both_versions():
    item = V1["GQ22"]
    r = score(item, _version_diff("6"), index=1, plan_=plan("compare", "follow_up", ["compare"]), steps=["compare"])
    assert not facet_reasons(r, "changed"), r["reasons"]
    missing = _version_diff("6")
    missing["claims"] = missing["claims"][:2]
    assert facet_reasons(score(item, missing, index=1), "changed")
    assert facet_reasons(score(item, _version_diff("8"), index=1), "changed")  # wrong old rate
    assert facet_reasons(score(item, _version_diff("6", "H4v2"), index=1), "changed")  # cited to the wrong version


# --- L6: conflicting statements are shown from cited sources ----------------------------------------

def test_conflict_needs_both_values_in_claims_citing_their_documents():
    item = V1["GQ26"]
    sources = [src("E1", "H6", [1]), src("E2", "H7", [])]
    good = ans(text="x", claims=[claim("לפי השומה מ-2022 הבניין נבנה בשנת 1958", "E1"),
                                 claim("לפי השומה מ-2023 הבניין נבנה בשנת 1962", "E2")], sources=sources)
    assert not facet_reasons(score(item, good), "conflict")
    one_side = ans(text="הבניין נבנה בשנת 1958, ויש מקור שמציין 1962",
                   claims=[claim("הבניין נבנה בשנת 1958", "E1")], sources=sources)
    assert facet_reasons(score(item, one_side), "conflict")  # 1962 only in the text, not in a cited claim


# --- computations: the audit block (L2, S8, S9, L7) ------------------------------------------------------

def _entry(doc: str, state: str, value: str | None = None) -> dict:
    return {"document_id": doc, "version_id": doc, "doc": doc, "title": doc, "state": state, "value": value,
            "tier": "preliminary" if value else None, "entity_key": None}


def _gq28(audit_docs: list[dict], completeness: str = "subset", found: int = 2, awaiting: int = 1) -> dict:
    sources = [src("E1", "H1", [1], value="12", tier="preliminary"), src("E2", "H2", [1], value="9.5",
                                                                           tier="preliminary")]
    return ans("numeric", text="10.75", sources=sources,
               numeric={"operation": "mean", "unit": "sqm", "value": None, "record_count": 0},
               preliminary={"value": "10.75", "record_count": 2, "values": ["12", "9.5"]},
               coverage={"facts": {"in_scope": 15, "found": found, "not_stated": 12, "awaiting_review": awaiting}},
               audit={"attribute": "גודל ממ״ד", "operation": "mean", "unit": "sqm", "completeness": completeness,
                      "in_scope": 15, "unknown_metadata": 0, "observations": 2, "duplicates_merged": 0,
                      "documents": audit_docs})


GOOD_GQ28 = [_entry("H1", "used", "12"), _entry("H2", "used", "9.5"), _entry("H3", "awaiting_review"),
             _entry("H8", "not_stated"), _entry("D7", "not_stated")]


def _gq28_score(answer: dict) -> dict:
    return score(V1["GQ28"], answer, plan_=plan("compute", tools=["extract_and_compute"]),
                 steps=["extract_and_compute"])


def test_settled_computation_with_a_per_document_audit_passes():
    r = _gq28_score(_gq28(GOOD_GQ28))
    assert r["ok"], r["reasons"]


@pytest.mark.parametrize(("change", "why"), [
    ({"H3": _entry("H3", "not_stated")}, "H3 reported as not stated"),
    ({"H3": _entry("H3", "used", "10.5")}, "H3 used"),
    ({"H8": _entry("H8", "used", "12")}, "a document that states nothing used as a value"),
    ({"D7": _entry("D7", "used", "90")}, "junk value from another attribute used"),
    ({"H1": _entry("H1", "used", "11")}, "wrong value"),
])
def test_audit_negative_controls_fail(change, why):
    docs_ = [change.get(e["doc"], e) for e in GOOD_GQ28]
    assert facet_reasons(_gq28_score(_gq28(docs_)), "coverage"), why


def test_partial_computation_presented_as_complete_fails():
    assert facet_reasons(_gq28_score(_gq28(GOOD_GQ28, completeness="complete")), "coverage")


def test_superseded_version_used_fails():
    docs_ = GOOD_GQ28 + [_entry("H4", "used", "11")]
    assert facet_reasons(_gq28_score(_gq28(docs_)), "coverage")


def test_count_fallback_without_audit_still_requires_the_awaiting_review_count():
    a = _gq28(GOOD_GQ28)
    del a["audit"]
    assert _gq28_score(a)["ok"]
    a["coverage"]["facts"]["awaiting_review"] = 0
    assert facet_reasons(_gq28_score(a), "coverage")


def test_old_key_figure_is_no_longer_accepted():
    a = _gq28(GOOD_GQ28)  # the old key's 10.67 over 3 values, H3 converted by assumption
    a["preliminary"] = {"value": "10.67", "record_count": 3, "values": ["12", "9.5", "10.5"]}
    assert facet_reasons(_gq28_score(a), "result")


def _gq49(h4v2_state: str) -> dict:
    sources = [src("E1", "H5", [1], value="7", tier="preliminary"), src("E2", "H4v2", [1])]
    return ans("combined", text="7", sources=sources,
               claims=[claim("בדירה בשינקין מרפסת סלון בשטח 12 מ״ר ומרפסת חדר שינה בשטח 6 מ״ר", "E2")],
               numeric={"operation": "values", "unit": "sqm", "value": None, "record_count": 0},
               preliminary={"value": None, "record_count": 1, "values": ["7"]},
               coverage={"facts": {"in_scope": 3, "found": 1, "not_stated": 1, "awaiting_review": 1}},
               audit={"completeness": "subset", "unknown_metadata": 0, "documents": [
                   _entry("H5", "used", "7"), _entry("H4v2", h4v2_state), _entry("D6", "not_stated")]})


def test_combined_answer_contradicting_its_coverage_fails():
    plan_ = plan("compute", tools=["extract_and_compute"])
    ok = score(V1["GQ49"], _gq49("awaiting_review"), plan_=plan_)
    assert not facet_reasons(ok, "coverage"), ok["reasons"]
    bad = score(V1["GQ49"], _gq49("not_stated"), plan_=plan_)
    assert any("content part" in r for r in facet_reasons(bad, "coverage"))


def test_filtered_observation_counts_toward_n():
    item = V1["GQ34"]  # settled: 1 of H1 2017, H3 2004, H8 1972 is > 2010; H2 awaiting review
    docs_ = [_entry("H1", "used", "2017"), _entry("H3", "filtered_out", "2004"), _entry("H8", "filtered_out", "1972"),
             _entry("H2", "awaiting_review")]
    a = ans("numeric", text="1", sources=[src("E1", "H1", [2], value="2017", tier="preliminary")],
            numeric={"operation": "count", "unit": None, "value": None, "record_count": 0},
            preliminary={"value": "1", "record_count": 3},
            coverage={"facts": {"in_scope": 15, "found": 3, "not_stated": 11, "awaiting_review": 1}},
            audit={"completeness": "subset", "unknown_metadata": 0, "documents": docs_})
    assert score(item, a, plan_=plan("compute", tools=["extract_and_compute"]))["ok"]
    wrong = [_entry("H1", "used", "2017"), _entry("H3", "used", "2020"), _entry("H8", "filtered_out", "1972"),
             _entry("H2", "awaiting_review")]  # the renovation year taken as the build year
    assert facet_reasons(score(item, a | {"audit": {"documents": wrong}}), "coverage")


# --- S9: the settled answer-key blocks are consistent -----------------------------------------------

def _number(fact: dict) -> Decimal:
    return Decimal(str((fact.get("normalized") or {}).get("value", fact["value"])))


SETTLED = [(i["id"], t) for i in V1.values() for t in i["turns"] if t["expect"].get("settled")]


def test_settled_blocks_exist_for_the_decisions_listed_in_scorer_changes():
    assert sorted({i for i, _ in SETTLED}) == ["GQ28", "GQ29", "GQ34", "GQ35", "GQ36", "GQ49"]


@pytest.mark.parametrize(("item_id", "turn"), SETTLED, ids=[i for i, _ in SETTLED])
def test_settled_block_is_derived_from_the_document_facts(item_id, turn):
    expect, settled = turn["expect"], turn["expect"]["settled"]
    eff = effective(expect)
    res = eff["result"]
    used = res["facts"]
    assert settled["basis"] and set(used) < set(expect["result"]["facts"])
    assert res["n"] == len(used) == eff["coverage"]["values_found"]
    left_out = {F[f]["document"] for f in expect["result"]["facts"]} - {F[f]["document"] for f in used}
    assert left_out == set(eff["coverage"].get("awaiting_review") or []) | (
        set(eff["coverage"].get("not_stated_includes") or []) & left_out)
    assert {v["fact"] for v in eff["values"]} <= set(used)
    numbers = [_number(F[f]) for f in used]
    if res["metric"] == "mean":
        assert (sum(numbers) / len(numbers)).quantize(Decimal("0.01"), ROUND_HALF_UP) == Decimal(res["value"])
    elif res["metric"] == "count":
        op, bound = res["where"].split()[:2]
        test = {">": operator.gt, "<": operator.lt}[op]
        assert sum(1 for n in numbers if test(n, Decimal(bound))) == int(res["value"])


# --- harness: both sets load and every turn can be scored -----------------------------------------------

@pytest.mark.parametrize("items", [V1, V2], ids=["regression", "holdout_v2"])
def test_every_turn_of_both_sets_scores_and_an_empty_answer_fails(items):
    for item in items.values():
        for i, turn in enumerate(item["turns"]):
            for v in (turn["expect"].get("values") or []) + (effective(turn["expect"]).get("values") or []):
                assert v["fact"] in F, (item["id"], v)
            r = score(item, {"kind": "error", "status": 500, "detail": "x"}, index=i)
            assert not r["ok"], (item["id"], i)


def test_sets_users_and_superseded_versions():
    assert SET_LABELS == {"general": "regression", "holdout_v2": "held-out v2"}
    assert sum(1 for i in V1.values() if i.get("held_out") and i.get("cloud", "on") == "on") == 54
    assert "G4" in USERS["dana@demo.test"][1] and "G4" not in (USERS["yossi@demo.test"][1] or set())
    assert {"H4", "K3", "D1"} <= replaced_docs()


def test_v2_values_are_scored_with_the_v2_answer_key():
    item = V2["HV01"]  # the lease term written only in words ("עשר שנים")
    a = ans(text="x", claims=[claim("החנות מושכרת לתקופה של עשר שנים", "E1")], sources=[src("E1", "K3v2", [1])])
    assert not facet_reasons(score(item, a, plan_=plan("answer")), "values")
    wrong = ans(text="x", claims=[claim("החנות מושכרת לתקופה של חמש שנים", "E1")], sources=[src("E1", "K3v2", [1])])
    assert facet_reasons(score(item, wrong, plan_=plan("answer")), "values")
