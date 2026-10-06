"""Claim-level verification (KTD11, R23): layer 1 is deterministic per claim, layer 2 is one judge call."""

from app.answering.verify import JUDGE_POLICY, JudgeOutput, check_claim, judge_claims, judge_input, numbers_in
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider

EVIDENCE = {
    "E1": "העסקה בהגפן 20 נמכרה ב-2,470,000 ₪ בשטח 95 מ״ר.",
    "E2": "השמאי המכריע קבע הפחתה של 10%.",
    "E3": "שיעור ההתאמה לגודל הוא 7%.",
}


def check(text, ids, kind="explicit", computed=frozenset(), declared=()):
    return check_claim(text, ids, list(declared), kind, evidence=EVIDENCE, computed_numbers=set(computed))


def test_supported_claims_pass():
    assert check("העסקה נמכרה ב-2,470,000 ₪", ["E1"]) == []
    assert check("הוחלה הפחתה של 10%", ["E2"]) == []


def test_number_from_another_evidence_fails():
    """A claim citing E1 whose number appears only in E3 fails, though E3 is authorized."""
    assert "unsupported_number" in check("שיעור ההתאמה הוא 7%", ["E1"])
    assert check("שיעור ההתאמה הוא 7%", ["E3"]) == []


def test_computed_numbers_only_in_computed_claims():
    assert check("הממוצע הוא 26,000 ₪ למ״ר", [], kind="computed", computed={"26000"}) == []
    assert "unsupported_number" in check("הממוצע הוא 26,000 ₪ למ״ר [E1]", ["E1"], computed={"26000"})


def test_unknown_or_missing_citation_fails():
    assert "unknown_citation" in check("ראו את הקטע", ["E9"])
    assert "no_citations" in check("זו טענה בלי מקור", [])
    # an inline citation must be one of the claim's own evidence ids
    assert "unknown_citation" in check("הוחלה הפחתה של 10% [E1]", ["E2"])


def test_invented_and_single_digit_numbers_fail():
    assert "unsupported_number" in check("המחיר היה 3,100,000 ₪", ["E1"])
    assert "unsupported_number" in check("ההפחתה הייתה 9%", ["E2"])


def test_declared_numbers_must_come_from_the_cited_evidence():
    assert "unsupported_number" in check("הוחלה הפחתה", ["E2"], declared=["15"])
    assert check("הוחלה הפחתה של 10%", ["E2"], declared=["10"]) == []


def test_links_and_markup_fail():
    assert "links_or_markup" in check("ראו https://evil.example/?q=secret", ["E1"])
    assert "links_or_markup" in check("![x](http://a)", ["E1"])
    assert "links_or_markup" in check("<b>הפחתה</b> של 10%", ["E2"])


def test_list_ordinals_are_not_numbers():
    found = numbers_in("1. העסקה נמכרה ב-2,470,000 ₪")
    assert "2470000" in found and "1" not in found


def _judge_items():
    return [(0, "הוחלה הפחתה של 10%", [("E2", EVIDENCE["E2"])]),
            (1, "ההפחתה נבעה מתוכנית מתאר חדשה", [("E2", EVIDENCE["E2"])])]


def test_judge_gets_only_the_cited_spans_and_returns_verdicts():
    p = ScriptedProvider().on(Purpose.VERIFY, {"verdicts": [{"claim": 0, "verdict": "supported"},
                                                            {"claim": 1, "verdict": "unsupported"}]})
    verdicts, result = judge_claims(p, _judge_items())
    assert result.status == CallStatus.OK and verdicts == {0: "supported", 1: "unsupported"}
    call = p.calls[0]
    assert call.purpose == Purpose.VERIFY and call.schema is JudgeOutput
    assert EVIDENCE["E2"] in call.input and EVIDENCE["E1"] not in call.input and EVIDENCE["E3"] not in call.input


def test_judge_missing_verdict_counts_as_unsupported_and_failure_returns_none():
    p = ScriptedProvider().on(Purpose.VERIFY, {"verdicts": [{"claim": 0, "verdict": "partial"},
                                                            {"claim": 7, "verdict": "supported"}]})
    verdicts, _ = judge_claims(p, _judge_items())
    assert verdicts == {0: "partial", 1: "unsupported"}
    verdicts, result = judge_claims(ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT), _judge_items())
    assert verdicts is None and result.status == CallStatus.TIMEOUT


# --- Restating a cited span: number formats and the question's own subject (real-model sample, (d)) -------


def test_number_formats_of_the_same_value_match():
    ev = {"E1": "גובה תקרה | 2.80 מ׳. השטח 1,250 מ״ר. המועד הקובע: 14/02/2023. הותקנה ב2019 מעלית."}
    for text in ("גובה התקרה 2.8 מ׳", "השטח 1250 מ״ר", "המועד הקובע הוא 14.2.2023", "המעלית הותקנה ב-2019"):
        assert check_claim(text, ["E1"], [], "explicit", evidence=ev, computed_numbers=set()) == [], text


def test_a_count_written_as_a_word_in_the_evidence_supports_its_digit():
    ev = {"E1": "לדירה צמודות שתי חניות תת-קרקעיות."}
    assert check_claim("לדירה 2 חניות צמודות", ["E1"], ["2"], "explicit", evidence=ev, computed_numbers=set()) == []
    assert "unsupported_number" in check_claim("לדירה 3 חניות", ["E1"], ["3"], "explicit", evidence=ev,
                                               computed_numbers=set())


def test_the_question_subject_restated_in_a_claim_is_not_an_unsupported_number():
    """GQ10: the claim names the address the user asked about; the cited table row holds only the year."""
    ev = {"E1": "שנת בנייה | 1968"}
    q = "מה שנת הבנייה של הבניין ברחוב המאבק 25 לפי טבלת מאפייני הנכס?"
    text = "שנת הבנייה של הבניין ברחוב המאבק 25 היא 1968."
    assert "unsupported_number" in check_claim(text, ["E1"], ["1968"], "explicit", evidence=ev,
                                               computed_numbers=set())
    assert check_claim(text, ["E1"], ["1968"], "explicit", evidence=ev, computed_numbers=set(), question=q) == []
    # "בבן יהודה 140" restates "ברחוב בן יהודה 140" (a prefix letter differs)
    assert check_claim("בדירה בבן יהודה 140 גובה התקרה 2.80 מ׳", ["E2"], ["2.80"], "explicit",
                       evidence={"E2": "גובה תקרה 2.80 מ׳"}, computed_numbers=set(),
                       question="מה גובה התקרה בדירה ברחוב בן יהודה 140?") == []


def test_a_question_number_stated_as_the_claims_value_still_needs_evidence():
    ev = {"E1": "הבניין נבנה בשנת 1962."}
    q = "האם הבניין נבנה בשנת 1958?"
    # the model declares 1958 as the claim's value: it is a fact, not the question's subject
    assert "unsupported_number" in check_claim("הבניין נבנה בשנת 1958", ["E1"], ["1958"], "explicit",
                                               evidence=ev, computed_numbers=set(), question=q)
    # a question number under another word is not a restatement either
    assert "unsupported_number" in check_claim("בבניין 1958 דירות", ["E1"], [], "explicit", evidence=ev,
                                               computed_numbers=set(), question=q)


def test_judge_input_names_each_spans_document():
    items = [(0, "גובה התקרה 3.05 מ׳", [("E1", "גובה התקרה בדירה 3.05 מ׳")])]
    out = judge_input(items, {"E1": "שומה בן יהודה 140, עמ׳ 1, 3. תיאור הנכס"})
    assert 'source="שומה בן יהודה 140, עמ׳ 1, 3. תיאור הנכס"' in out and "3.05" in out
    assert "partial" in JUDGE_POLICY and "2.80" in JUDGE_POLICY  # format equivalence and subject rules
