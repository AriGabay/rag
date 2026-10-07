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
