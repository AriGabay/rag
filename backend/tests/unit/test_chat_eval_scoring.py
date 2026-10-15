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


def test_a_count_from_a_cited_listing_covers_the_documents_it_lists():
    from eval.scoring import check

    docs = [{"document_id": "d1", "title": "שומה - הדקל 14"}, {"document_id": "d2", "title": "שומה - התמר 3"}]
    answer = {"status": "answered", "markdown": "יש לנו 2 שומות [S1].\n\n> **כיסוי:** רשימת 2 המסמכים נבדקה; תוכן "
              "המסמכים לא נקרא.", "sources": [{"id": "S1", "kind": "listing", "title": "מסמכים בתחום",
                                               "listed_document_ids": ["d1", "d2"]}],
              "ledger": {"scope_kind": "set", "membership": True, "matching": docs, "complete": True,
                         "note": "x", "tables": []}}
    assert check(answer, {"required_documents": ["הדקל 14", "התמר 3"]}) == []
    assert check(answer, {"required_documents": ["הנחל 8"]}) == ["אין ציטוט מהמסמך 'הנחל 8'"]


def test_usage_report_prices_each_call_by_its_model_and_purpose_with_cache_buckets():
    from eval.chat_eval import Result, usage_summary

    luna = {"model": "gpt-6-luna", "input_tokens": 1000, "cached_input_tokens": 400, "cache_write_tokens": 100,
            "output_tokens": 50}
    turns = [{"seconds": 3.0, "usage": [
        luna | {"purpose": "agent", "cost_usd": 0.5},  # priced at log time: taken as recorded
        luna | {"purpose": "verify"},  # stored before costs were recorded: priced from its model and buckets
        {"purpose": "resolve", "model": "unpriced-model", "input_tokens": 10, "output_tokens": 1, "cost_usd": None},
    ]}, {"seconds": 5.0, "status": "failed", "usage": [luna | {"purpose": "agent", "cost_usd": 0.25}]}]
    text = "\n".join(usage_summary([Result("answers", "A", True, [], {"turns": turns})]))
    assert "| agent | gpt-6-luna | 2 | 2,000 | 40% | 10% | 100 | 0.7500$ |" in text
    assert "| verify | gpt-6-luna | 1 | 1,000 | 40% | 10% | 50 | 0.0001$ |" in text  # 91.5e-6
    assert "| resolve | unpriced-model | 1 |" in text and "בלי 1 קריאות שעלותן לא ידועה" in text
    assert "סה\"כ 0.750$" in text and "0.37" in text  # mean per turn over both turns, the failed one included


# --- `source_docs_mode: if_needed` and `--sha` (round 13 U1, KTD18) ---------------------------------------------

GLOSSARY = "מילון מונחים"
APPRAISAL_TITLE = "שומה הדגמה 5"
STANDARD_TITLE = "תקן הדגמה 99"


def _src(sid: str, title: str, kind: str = "appraisal") -> dict:
    return {"id": sid, "title": title, "source_kind": kind, "kind": "passage"}


def _mixed_turn(markdown: str, sources: list[dict], *, removed: list[dict] | None = None, judge_status: str = "ok",
                judged: bool = True) -> dict:
    """A turn as the runner stores it: the answer payload, with the diagnostics the runner fetched."""
    cited = [s for s in sources if f"[{s['id']}]" in markdown]
    removals = [{"claim_id": r["claim_id"], "action": r["action"]} for r in removed or []]
    return {"status": "done",
            "answer": {"markdown": markdown, "status": "answered", "sources": cited, "measurements": [],
                       "verification": {"judged": judged, "judge_status": judge_status, "removed": len(removals),
                                        "removals": removals}},
            "_diagnostics": {"rounds": [], "removed": removed or []}}


def _removal(action: str, text: str, kind: str) -> dict:
    return {"claim_id": "c1", "action": action, "text": text, "verdict": "unsupported",
            "sources": [{"id": "S9", "type": "sources", "source_kind": kind}]}


IF_NEEDED = {"source_docs": [GLOSSARY], "source_docs_mode": "if_needed"}
APPRAISAL_LINE = "השווי למ\"ר בשומה הוא 9,500 ₪ [S1]."
STANDARD_LINE = "לפי התקן, שווי שוק הוא המחיר שהיה משולם בעסקה בין קונה מרצון למוכר מרצון [S2]."
SOURCES = [_src("S1", APPRAISAL_TITLE), _src("S2", STANDARD_TITLE, "standard"),
           _src("S3", GLOSSARY, "professional")]


def test_default_mode_still_requires_the_cited_document():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES)
    assert grade_turn(m, {"source_docs": [GLOSSARY]}, {}) == [f"אין מקור מהמסמך '{GLOSSARY}'"]
    assert grade_turn(m, {"source_docs": [GLOSSARY], "source_docs_mode": "cited"}, {}) == [
        f"אין מקור מהמסמך '{GLOSSARY}'"]


def test_if_needed_passes_when_the_glossary_is_cited():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(f"{APPRAISAL_LINE}\n\nהמונח מוגדר במילון [S3].", SOURCES)
    assert grade_turn(m, IF_NEEDED, {}) == []


def test_if_needed_passes_when_every_professional_statement_is_verified_by_another_cited_source():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES)
    assert grade_turn(m, IF_NEEDED, {}) == []


def test_if_needed_fails_when_a_professional_statement_was_removed_and_the_glossary_is_not_cited():
    from eval.chat_eval import grade_turn

    gone = _removal("removed", "שווי שוק הוא המחיר בעסקה בין קונה מרצון למוכר מרצון.", "professional")
    m = _mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES, removed=[gone])
    problems = grade_turn(m, IF_NEEDED, {})
    assert len(problems) == 1 and GLOSSARY in problems[0] and "הוסרה" in problems[0]
    only_gone = _mixed_turn(APPRAISAL_LINE, SOURCES, removed=[gone])  # nothing professional is left in the text
    assert grade_turn(only_gone, IF_NEEDED, {})
    # a removed property statement is none of this check's business
    property_gone = _removal("removed", "השטח הבנוי 120 מ\"ר.", "appraisal")
    assert grade_turn(_mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES, removed=[property_gone]),
                      IF_NEEDED, {}) == []


def test_if_needed_fails_an_answer_without_a_professional_statement_unless_it_was_not_asked():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(APPRAISAL_LINE, SOURCES)
    problems = grade_turn(m, IF_NEEDED, {})
    assert len(problems) == 1 and "אין בתשובה טענה מקצועית" in problems[0]
    assert grade_turn(m, IF_NEEDED | {"professional_not_asked": True}, {}) == []
    # not asked waives the missing part only: a professional statement that did not hold still fails
    gone = _removal("cut", "הגדרה.", "standard")
    assert grade_turn(_mixed_turn(APPRAISAL_LINE, SOURCES, removed=[gone]),
                      IF_NEEDED | {"professional_not_asked": True}, {})


def test_if_needed_fails_a_professional_statement_that_cannot_be_called_verified():
    from eval.chat_eval import grade_turn

    md = f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}"
    assert grade_turn(_mixed_turn(md, SOURCES, judge_status="unavailable"), IF_NEEDED, {})
    assert grade_turn(_mixed_turn(md, SOURCES, judged=False), IF_NEEDED, {})
    no_diagnostics = _mixed_turn(md, SOURCES) | {"_diagnostics": None}
    assert grade_turn(no_diagnostics, IF_NEEDED, {}) == []  # nothing was removed, so there is nothing to inspect
    no_diagnostics["answer"]["verification"]["removed"] = 1
    assert grade_turn(no_diagnostics, IF_NEEDED, {})  # something was removed and what it was cannot be seen


def test_if_needed_names_professional_statements_by_pattern_and_needs_a_cited_support_for_each():
    from eval.chat_eval import grade_turn

    expect = IF_NEEDED | {"professional_patterns": ["שווי שוק הוא"]}
    # supported by the appraisal's own cited text: another cited source than the glossary
    own = _mixed_turn("שווי שוק הוא המחיר שהיה משולם בעסקה, כפי שהשומה מגדירה [S1].", SOURCES)
    assert grade_turn(own, expect, {}) == []
    uncited = _mixed_turn(f"{APPRAISAL_LINE}\n\nשווי שוק הוא המחיר שהיה משולם בעסקה.", SOURCES)
    problems = grade_turn(uncited, expect, {})
    assert len(problems) == 1 and "אינה נתמכת" in problems[0]


def test_if_needed_applies_to_a_stored_result_on_rescore():
    from eval.chat_eval import rescore

    turn = {"ask": "מהו שווי שוק ומה שווי הנכס?", "expect": IF_NEEDED}
    m = _mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES)
    stored = [{"kind": "answers", "id": "X1", "ok": False, "detail": [],
               "data": {"turns": [{"ask": turn["ask"], "status": "done", "markdown": m["answer"]["markdown"],
                                   "answer": m["answer"], "diagnostics": m["_diagnostics"]}]}}]
    (r,) = rescore({"answers": [{"id": "X1", "turns": [turn]}]}, stored)
    assert r.ok
    stored[0]["data"]["turns"][0]["diagnostics"] = {"removed": [_removal("removed", "טענה.", "standard")]}
    (r,) = rescore({"answers": [{"id": "X1", "turns": [turn]}]}, stored)
    assert not r.ok


def test_an_unknown_source_docs_mode_is_a_problem_not_a_pass():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(APPRAISAL_LINE, SOURCES)
    assert grade_turn(m, {"source_docs": [GLOSSARY], "source_docs_mode": "sometimes"}, {})


def _sha_of(path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_sha_check_accepts_a_matching_file_and_refuses_a_changed_one(tmp_path):
    import pytest

    from eval.chat_eval import SetHashMismatch, check_set_sha

    q = tmp_path / "set.yaml"
    q.write_text("title: synthetic\nanswers: []\n", encoding="utf-8")
    sha = tmp_path / "set.yaml.sha256"
    sha.write_text(f"{_sha_of(q)}  set.yaml\n", encoding="utf-8")
    assert check_set_sha(q, sha) == _sha_of(q)
    q.write_text("title: synthetic\nanswers: []\n# edited\n", encoding="utf-8")
    with pytest.raises(SetHashMismatch) as e:
        check_set_sha(q, sha)
    assert _sha_of(q) in str(e.value) and sha.read_text().split()[0] in str(e.value)
    sha.write_text("not a hash\n", encoding="utf-8")
    with pytest.raises(SetHashMismatch):
        check_set_sha(q, sha)


def test_main_refuses_a_set_that_differs_from_its_sha_before_touching_the_stack(tmp_path, capsys):
    from eval.chat_eval import main

    q = tmp_path / "set.yaml"
    q.write_text("title: synthetic\nanswers: []\n", encoding="utf-8")
    sha = tmp_path / "set.sha256"
    sha.write_text("0" * 64 + "  set.yaml\n", encoding="utf-8")
    # an unreachable base: a run that got past the check would fail on the connection, not return 2
    code = main(["--set", str(q), "--out", str(tmp_path / "out"), "--sha", str(sha), "--base", "http://127.0.0.1:9"])
    err = capsys.readouterr().err
    assert code == 2 and "0" * 64 in err and _sha_of(q) in err
    assert not (tmp_path / "out").exists()


# --- a `source_docs` entry may list alternative title fragments (round 13 U14) ------------------------------------

STORED_TWICE = ["מילון", "מים, |"]  # the same glossary stored under its own title and under one the extraction garbled


def test_a_source_docs_entry_may_be_a_list_of_alternative_title_fragments():
    from eval.chat_eval import grade_turn

    garbled = _src("S3", "מים, |", "professional")
    m = _mixed_turn(f"{APPRAISAL_LINE}\n\nהמונח מוגדר במילון [S3].", [*SOURCES[:2], garbled])
    assert grade_turn(m, {"source_docs": [STORED_TWICE]}, {}) == []
    # the string form still names one fragment only
    assert grade_turn(m, {"source_docs": ["מילון"]}, {}) == ["אין מקור מהמסמך 'מילון'"]
    # none of the alternatives is cited: the problem names them all
    absent = _mixed_turn(APPRAISAL_LINE, SOURCES[:1])
    assert grade_turn(absent, {"source_docs": [STORED_TWICE]}, {}) == ["אין מקור מהמסמך 'מילון / מים, |'"]


def test_if_needed_with_alternatives_judges_support_by_a_source_none_of_them_names():
    from eval.chat_eval import grade_turn

    m = _mixed_turn(f"{APPRAISAL_LINE}\n\n{STANDARD_LINE}", SOURCES[:2])
    assert grade_turn(m, {"source_docs": [STORED_TWICE], "source_docs_mode": "if_needed"}, {}) == []
    only_glossary_copy = _mixed_turn(f"{APPRAISAL_LINE}\n\nהמונח במילון [S3].", [SOURCES[0], _src("S3", "מים, |", "standard")])
    # a statement cited to one of the alternatives is the glossary itself: it is cited, so nothing more is asked
    assert grade_turn(only_glossary_copy, {"source_docs": [STORED_TWICE], "source_docs_mode": "if_needed"}, {}) == []
