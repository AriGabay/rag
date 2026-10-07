"""The structured eval checks fail answers that keyword checks pass: negative controls on synthetic answers.

Each control answer contains every expected keyword; it must still fail because it leaves out a document,
changes a unit, shows one value instead of the set, attributes a value to the wrong document, or claims full
coverage when the ledger says otherwise. The positive control passes every check."""

from __future__ import annotations

import re

from eval.scoring import check

DOC_A, DOC_B = "שומה הגפן", "שומה הזית"


TITLES = {"S1": f"{DOC_A} 12", "S2": f"{DOC_B} 7"}


def _answer(markdown: str, *, ledger=None) -> dict:
    # as the server stores it: the sources list holds the cited sources only
    cited = [i for i in TITLES if f"[{i}]" in markdown]
    return {"markdown": markdown, "sources": [{"id": i, "title": TITLES[i]} for i in cited], "measurements": [],
            "ledger": ledger}


def _keywords_present(md: str, keywords: list[str]) -> bool:
    return all(re.search(k, md) for k in keywords)


KEYWORDS = ["שווי", "₪"]


def test_omitting_a_required_document_fails():
    a = _answer("בהגפן השווי למ\"ר הוא 9,500 ₪ [S1].")
    assert _keywords_present(a["markdown"], KEYWORDS)
    problems = check(a, {"required_documents": [DOC_A, DOC_B]})
    assert problems == [f"אין ציטוט מהמסמך '{DOC_B}'"]


def test_changing_a_unit_fails():
    a = _answer("דמי השכירות הם 55 ₪ למ\"ר לשנה [S1].")
    expect = {"value_meaning": [{"value": "55", "must_near": ["חודש"], "must_not_near": ["לשנה"]}]}
    assert _keywords_present(a["markdown"], ["55", "₪"]) and check(a, expect)
    good = _answer("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    assert check(good, expect) == []


def test_one_value_instead_of_the_set_fails_unless_called_an_example_with_the_size():
    values = [str(51 + i) for i in range(9)]
    one = _answer("שכר הדירה למ\"ר בטבלה הוא 53 ₪ [S1].")
    assert check(one, {"value_set": {"values": values, "example_count": 9}})
    example = _answer("לדוגמה, ברחוב הדגמה 3 שכר הדירה הוא 53 ₪ למ\"ר; בטבלה 9 ערכים כאלה [S1].")
    assert check(example, {"value_set": {"values": values, "example_count": 9}}) == []
    full = _answer("שכר הדירה בטבלה: " + ", ".join(f"{v} ₪" for v in values) + " [S1].")
    assert check(full, {"value_set": {"values": values}}) == []


def test_a_value_attributed_to_the_wrong_document_fails():
    a = _answer("השווי למ\"ר הוא 11,000 ₪ [S1].")  # S1 is DOC_A; the value belongs to DOC_B
    assert check(a, {"attribution": [{"value": "11,000", "document": DOC_B}]})
    good = _answer("השווי למ\"ר הוא 11,000 ₪ [S2].")
    assert check(good, {"attribution": [{"value": "11,000", "document": DOC_B}]}) == []


def test_partial_coverage_without_the_note_fails():
    ledger = {"complete": False, "not_checked": [{"title": f"{DOC_B} 7"}]}
    silent = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", ledger=ledger)
    assert "הכיסוי חלקי אבל התשובה אינה אומרת זאת" in check(silent, {})
    noted = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\n\n> **כיסוי:** 2 מסמכים מתאימים; לא נבדקו: שומה הזית 7.",
                    ledger=ledger)
    assert check(noted, {"coverage": {"expect_complete": False, "must_list_unchecked": [DOC_B]}}) == []


def test_complete_coverage_expected_but_partial_fails():
    ledger = {"complete": False, "unused": [{"title": f"{DOC_B} 7"}]}
    a = _answer("השווי 9,500 ₪ [S1].\n\n> **כיסוי:** חלקי.", ledger=ledger)
    assert check(a, {"coverage": {"expect_complete": True}}) == ["כיסוי חלקי (צפוי מלא)"]


def test_positive_control_passes_everything():
    ledger = {"complete": True}
    a = _answer("בהגפן השווי למ\"ר הוא 9,500 ₪ לחודש [S1]; בהזית 11,000 ₪ [S2].", ledger=ledger)
    expect = {"required_documents": [DOC_A, DOC_B], "forbidden_documents": ["שומה הכרמל"],
              "value_set": {"values": ["9,500", "11,000"]},
              "value_meaning": [{"value": "9500", "must_near": ["למ\"ר"]}],
              "attribution": [{"value": "11000", "document": DOC_B}],
              "coverage": {"expect_complete": True}}
    assert check(a, expect) == []


# --- absence and reference corrections ---------------------------------------------------------------------

ABSENT = {"absence": {"near_values": ["184"]}}


def test_absence_said_first_with_the_near_datum_labelled_passes():
    md = "**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S1].\n\nהשטח הבנוי (נתון אחר) הוא 184 מ\"ר [S1]."
    assert check(_answer(md), ABSENT) == []


def test_a_near_datum_given_as_the_answer_fails():
    md = "שטח המגרש הוא 184 מ\"ר [S1]."
    problems = check(_answer(md), ABSENT)
    assert any("אינה פותחת" in p for p in problems) and any("184" in p for p in problems)


def test_absence_said_only_at_the_end_fails():
    md = "השטח הבנוי הוא 184 מ\"ר (נתון אחר) [S1].\nשטח המגרש לא מופיע במסמך [S1]."
    assert any("אינה פותחת" in p for p in check(_answer(md), ABSENT))


def test_a_corrected_reference_is_graded_both_ways():
    from eval.chat_eval import original_grade, rescore

    turn = {"ask": "באיזו שנה נבנה הבניין?", "expect": {"must": ["2016"]},
            "reference_corrected": {"date": "2026-10-07", "evidence": "שורת ההשוואה באותה חלקה",
                                    "was": {"must": ["לא נמצא"]}}}
    m = {"status": "done", "answer": {"markdown": "הבניין נבנה בשנת 2016 [S1].", "status": "answered", "sources": []}}
    assert original_grade(m, turn, {}) is False
    stored = [{"kind": "answers", "id": "X1", "ok": False, "detail": [],
               "data": {"turns": [{"ask": turn["ask"], "status": "done", "markdown": m["answer"]["markdown"],
                                   "answer": m["answer"]}]}}]
    (r,) = rescore({"answers": [{"id": "X1", "turns": [turn]}]}, stored)
    assert r.ok and r.data["turns"][0]["passed_original_reference"] is False
    assert r.data["corrections"] == [{"turn": 1, "date": "2026-10-07", "evidence": "שורת ההשוואה באותה חלקה"}]


# --- data coverage and the report numbers ---------------------------------------------------------------------

def test_a_complete_ledger_from_retrieved_passages_may_carry_its_note():
    note = "השווי 9,500 ₪ [S1].\n\n> **כיסוי:** נמצא נתון מאומת בכל המסמכים."
    ledger = {"scope_kind": "set", "complete": True, "retrieved_only": [{"document_id": "d", "title": "t"}]}
    assert check(_answer(note, ledger=ledger), {}) == []
    assert any("מסויגת" in p for p in check(_answer(note, ledger=ledger | {"retrieved_only": []}), {}))


def test_expect_all_rows_fails_a_partly_presented_table():
    ledger = {"scope_kind": "focused", "complete": True, "tables": [{"title": "סקר", "rows": 9, "presented": 2}]}
    expect = {"coverage": {"expect_all_rows": True}}
    assert any("2 מתוך 9" in p for p in check(_answer("שכ\"ד 55 ₪ [S1].", ledger=ledger), expect))
    full = ledger | {"tables": [{"title": "סקר", "rows": 9, "presented": 9}]}
    assert check(_answer("שכ\"ד 55 ₪ [S1].", ledger=full), expect) == []


def test_original_score_and_cost_per_pass():
    from eval.chat_eval import Result, cost_per_pass, original_score

    passed = Result("answers", "A", True, [], {"turns": [{"problems": [], "passed_original_reference": False}]})
    failed = Result("answers", "B", False, [], {"turns": [{"problems": ["x"]}]})
    assert original_score([passed, failed]) == 0
    assert cost_per_pass([passed, failed], 0.5).startswith("0.5000$ (1")
    assert cost_per_pass([failed], 0.5) == "אין שיחות שעברו"


def test_a_failed_turn_shows_how_its_follow_up_was_resolved():
    from eval.chat_eval import resolution_lines

    turn = {"diagnostics": {"resolution": {
        "parse": {"relation": "scale_change", "scope": "entity", "metric_kind": "value", "scale": "total",
                  "changed_fields": [{"field": "scale", "user_words": "לכל השארית"}]},
        "decisions": {"scale": "rejected: not_in_message"},
        "lookup": {"kind": "ambiguous", "query": "הנרקיס 4", "documents": [{"title": "שומה א"}, {"title": "שומה ב"}]}}},
        "answer": {"request": {"metric_kind": "value_per_area", "unit": "ILS_per_sqm", "approved": ["metric"],
                               "document_ids": ["d"]}}}
    text = "\n".join(resolution_lines(turn))
    assert "scale_change" in text and "«לכל השארית»" in text and "scale: rejected: not_in_message" in text
    assert "ambiguous" in text and "שומה א" in text and "מוחזק ל-metric" in text
    assert resolution_lines({"answer": {}}) == []
