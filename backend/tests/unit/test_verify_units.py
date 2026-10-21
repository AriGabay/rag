"""Verification fails closed: a claim the judge did not check is never kept as checked, and the judge reads the
evidence that covers each claim, wherever it is in the source. The judge here is scripted; these tests prove the
server's handling of its verdicts and failures, not the model's judgement. Synthetic text only."""

from __future__ import annotations

import json
import re
import uuid

import pytest

from app.chat import meaning, verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import Unit, VerificationUnavailable, split_units, structural_kind, verify_answer
from app.providers.llm import CallStatus, Purpose, StructuredResult
from tests.support.scripted_provider import ScriptedProvider


def _ws(*texts: str, kind: str = "context") -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind=kind, text=t)
    return ws


def _answer(markdown: str, parts: list | None = None) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=parts or [])


def _tag(i: int) -> str:
    """A distinct Hebrew label per line (digits would be checked against the sources)."""
    return chr(0x05D0 + i % 22) + chr(0x05D0 + i // 22 % 22)


def _indexes(input: str) -> list[int]:
    return [int(i) for i in re.findall(r'<unit index="(\d+)"', input)]


def _judge(rule) -> ScriptedProvider:
    """A judge answering each unit by ``rule(unit_text) -> verdict``, or None to leave the unit out."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
            v = rule(m.group(2))
            if v is not None:
                out.append({"index": int(m.group(1)), "verdict": v, "reason": "בדיקה"})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. הנכס פנוי."


# --- structural kinds -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,kind", [
    ("## סיכום", "heading"), ("**הנתונים:**", "label"), ("להלן הנתונים:", "label"),
    ("האם תרצה פירוט נוסף?", "question"), ("בנוסף,", "connective"), ("כמו כן —", "connective"),
])
def test_structural_units(raw, kind):
    (u,) = split_units(raw)
    assert structural_kind(u) == kind


@pytest.mark.parametrize("raw", [
    "השמאי קבע כי הנכס פנוי.", "הנכס פנוי.", "להלן השווי: 12 ₪", "**השווי: 9,500 ₪**",
    "אלה הם השיקולים העיקריים שהובילו את השמאי לקבוע את השווי בהתאם לגישה:",
    "הנכס פנוי [S1]?",
])
def test_claims_are_not_structural(raw):
    (u,) = split_units(raw)
    assert structural_kind(u) is None


# --- a lead-in before list items is structure, not a claim (round 13 U10, KTD12, R15) ------------------------------------

LEAD_IN = "הנתונים לחלקה 7 הם:"  # invented: a lot number in a short line ending with a colon
LEAD_SOURCE = "חלקה 7: מקדם א 0.8, מקדם ב 0.9."


@pytest.mark.parametrize("tail", ["- מקדם א\n- מקדם ב", "* מקדם א\n* מקדם ב", "1. מקדם א\n2. מקדם ב", "## פירוט"])
def test_a_short_colon_line_before_list_items_or_a_heading_is_a_lead_in_even_with_a_number(tail):
    lead = split_units(f"{LEAD_IN}\n{tail}")[0]
    assert structural_kind(lead) == "lead_in" and verify.exempt_without_verdict(lead)


def test_the_same_line_alone_or_before_prose_is_still_a_claim():
    for markdown in (LEAD_IN, f"{LEAD_IN}\nמקדם א נקבע בשומה."):
        lead = split_units(markdown)[0]
        assert structural_kind(lead) is None and not verify.exempt_without_verdict(lead)


def test_a_colon_line_of_more_than_eight_words_before_items_is_still_judged():
    long_line = "הנתונים שנקבעו לחלקה 7 בשומה זו לפי השמאי המכריע הם:"
    assert len(long_line.split()) > 8
    lead = split_units(f"{long_line}\n- מקדם א\n- מקדם ב")[0]
    assert structural_kind(lead) is None and not verify.exempt_without_verdict(lead)


def test_a_colon_line_that_states_an_amount_before_items_is_still_judged():
    lead = split_units("דמי השכירות 55 ₪ למ\"ר הם:\n- א\n- ב")[0]
    assert structural_kind(lead) is None


def test_a_lead_in_is_not_sent_to_the_judge_and_is_not_removed_as_an_unverified_claim():
    """The real defect: the judge called the lot-number lead-in ``not_factual`` and it was withheld as not classified; or
    it called it unsupported. Neither reaches the answer now: the judge is not asked, and the numbers still pass the checks."""
    seen: list[str] = []
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": "supported", "reason": "בדיקה"}
                             for i in re.findall(r'<unit index="(\d+)"', input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    a = _answer(f"{LEAD_IN}\n- מקדם א הוא 0.8 [S1]\n- מקדם ב הוא 0.9 [S1]")
    r = verify_answer(p, a, _ws(LEAD_SOURCE), "?", [])
    assert r.ok and r.removed_units() == set() and r.problems == []
    assert all(LEAD_IN not in i for i in seen)
    assert r.verdicts[0] == "navigation" and r.claims[0][0].action == "kept"
    assert r.apply(a).answer_markdown == a.answer_markdown


def test_a_wrong_number_in_a_lead_in_is_still_caught_by_the_deterministic_checks():
    a = _answer("הנתונים לחלקה 9 הם:\n- מקדם א הוא 0.8 [S1]\n- מקדם ב הוא 0.9 [S1]")
    r = verify_answer(_judge(lambda t: "supported"), a, _ws(LEAD_SOURCE), "?", [])
    (p,) = [x for x in r.problems if x.unit.index == 0]
    assert p.check == "unstated_number" and p.removes_unit and "9" in p.unstated


def test_a_lead_in_whose_items_were_all_removed_goes_with_them_as_a_dependency():
    a = _answer(f"{LEAD_IN}\n- מקדם א הוא 0.8 [S1]\n- מקדם ב הוא 0.9 [S1]")
    r = verify_answer(_judge(lambda t: "unsupported" if "מקדם" in t else "supported"), a, _ws(LEAD_SOURCE), "?", [])
    assert r.removed_units() == {0, 1, 2}
    lead = next(x for x in r.removals() if x.unit.index == 0)
    assert lead.check == "dependency" and lead.failure_kind == "absent_from_source" and lead.basis
    assert LEAD_IN not in r.apply(a).answer_markdown.split("\n\n> ")[0]


def test_a_lead_in_stays_while_one_of_its_items_stands():
    a = _answer(f"{LEAD_IN}\n- מקדם א הוא 0.8 [S1]\n- מקדם ב הוא 0.9 [S1]")
    r = verify_answer(_judge(lambda t: "unsupported" if "מקדם ב" in t else "supported"), a, _ws(LEAD_SOURCE), "?", [])
    assert r.removed_units() == {2}
    assert r.apply(a).answer_markdown.startswith(f"{LEAD_IN}\n- מקדם א הוא 0.8 [S1]")


# A shape is not evidence: only one-word, digit-free navigation text passes without a verdict.
@pytest.mark.parametrize("raw", ["## מקורות", "**סיכום**", "### פירוט:", "האם תרצה פירוט נוסף?", "בנוסף,"])
def test_neutral_navigation_is_exempt(raw):
    (u,) = split_units(raw)
    assert verify.exempt_without_verdict(u)


@pytest.mark.parametrize("raw", [
    "# הנכס פנוי", "## השווי נקבע לפי גישת ההשוואה", "**הנכס מושכר:**", "**השווי נקבע ל-9,800 ₪**",
    "### הדקל 14", "להלן הנתונים:",
])
def test_claims_styled_as_structure_are_not_exempt(raw):
    (u,) = split_units(raw)
    assert not verify.exempt_without_verdict(u)


def test_a_claim_table_header_is_not_exempt():
    header, _row = split_units("| הנכס פנוי | כן |\n|---|---|\n| הגפן 3 | 4 חדרים [S1] |")
    assert header.table_header and not verify.exempt_without_verdict(header)
    (single,) = split_units("| נכס |\n|---|")[:1]
    assert verify.exempt_without_verdict(single)


# --- fail closed --------------------------------------------------------------------------------------------

def test_judge_failing_twice_raises_and_makes_exactly_two_attempts():
    p = ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    usage: list = []
    with pytest.raises(VerificationUnavailable):
        verify_answer(p, _answer("הנכס פנוי."), _ws(SOURCE), "?", usage)
    assert [u["purpose"] for u in usage] == ["verify", "verify"]


def test_judge_failing_once_is_retried():
    p = ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT)
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [{"index": n, "verdict": "supported", "reason": "ok"}
                                                       for n in _indexes(input)]}, repeat=True)
    r = verify_answer(p, _answer("הנכס פנוי [S1]."), _ws(SOURCE), "?", [])
    assert r.ok and r.judged


def test_a_verbal_claim_without_a_verdict_fails():
    # no number and no citation, and the judge left it out twice: it is a claim, so it fails
    p = _judge(lambda t: None if "פנוי" in t else "supported")
    r = verify_answer(p, _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["הנכס פנוי."]
    assert "לא נבדקה" in r.problems[0].reason


def test_a_structural_unit_without_a_verdict_passes():
    p = _judge(lambda t: None if t.startswith("##") or t.startswith("**") else "supported")
    r = verify_answer(p, _answer("## סיכום\n**הנתונים**\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(SOURCE), "?", [])
    assert r.ok


def test_claims_styled_as_headings_labels_or_table_headers_fail_when_the_judge_skips_them():
    # (a colon line right before a heading is a lead-in since round 13 U10, KTD12: the claim styled so stands before prose)
    md = ("# הנכס פנוי\n**השווי נקבע ל-9,800 ₪**\n## מקורות\n**הנכס מושכר:**\n"
          "השווי למ\"ר הוא 9,500 ₪ [S1].\n\n| הנכס פנוי | כן |\n|---|---|\n| הגפן | 9,500 ₪ [S1] |")
    p = _judge(lambda t: "supported" if t.startswith("השווי למ") or t.startswith("| הגפן") else None)
    a = _answer(md)
    r = verify_answer(p, a, _ws(SOURCE + " 9,800"), "?", [])
    failed = sorted(x.unit.text for x in r.problems)
    assert failed == sorted(["# הנכס פנוי", "**השווי נקבע ל-9,800 ₪**", "**הנכס מושכר:**", "| הנכס פנוי | כן |"])
    out = r.apply(a).answer_markdown
    assert "## מקורות" in out and "השווי למ\"ר הוא 9,500" in out
    # the failed header takes its whole table with it: no separator or orphan row is left behind
    assert "|" not in out and "הגפן" not in out


def test_navigation_verdict_is_accepted_only_for_headings_and_labels():
    def rule(t):
        if t.startswith("##"):
            return "navigation"
        return "navigation" if "פנוי" in t else "supported"
    md = "## פירוט לפי מסמך\nהנכס פנוי [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1]."
    r = verify_answer(_judge(rule), _answer(md), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["הנכס פנוי ."]


def test_not_factual_on_a_multi_word_heading_is_not_accepted():
    p = _judge(lambda t: "not_factual" if t.startswith("##") else "supported")
    r = verify_answer(p, _answer("## הנכס פנוי\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["## הנכס פנוי"]


def test_navigation_heading_with_an_amount_is_not_accepted():
    p = _judge(lambda t: "navigation" if t.startswith("##") else "supported")
    r = verify_answer(p, _answer("## השווי 9,500 ₪\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["## השווי 9,500 ₪"]


def test_not_factual_on_a_number_is_not_accepted():
    p = _judge(lambda t: "not_factual")
    r = verify_answer(p, _answer("השווי הוא 9,500 ₪ [S1].\nאשמח לעזור בשאלות נוספות."), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["השווי הוא 9,500 ₪ ."]


def test_wrong_verbal_claim_is_removed_beside_correct_numbers():
    p = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס מושכר לטווח ארוך [S1].")
    r = verify_answer(p, a, _ws(SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert "מושכר" not in out and "9,500" in out and "55" in out


def test_a_wrong_claim_at_the_end_of_a_long_answer_is_judged_and_removed():
    lines = [f"פרט {_tag(i)}: השווי למ\"ר הוא 9,500 ₪ [S1]." for i in range(60)] + ["הנכס מושכר לטווח ארוך [S1]."]
    p = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    a = _answer("\n".join(lines))
    usage: list = []
    r = verify_answer(p, a, _ws(SOURCE), "?", usage)
    assert len(usage) >= 2  # more than one judge call
    assert [x.unit.text for x in r.problems] == ["הנכס מושכר לטווח ארוך ."]
    assert "מושכר" not in r.apply(a).answer_markdown


def test_an_incomplete_reply_splits_the_batch():
    calls: list[int] = []

    def respond(instructions: str, input: str):
        idx = _indexes(input)
        calls.append(len(idx))
        if len(idx) > 10:
            return StructuredResult(CallStatus.INCOMPLETE, detail="max_output_tokens")
        return {"verdicts": [{"index": n, "verdict": "supported", "reason": "ok"} for n in idx]}

    p = ScriptedProvider().on(Purpose.VERIFY, respond, repeat=True)
    a = _answer("\n".join(f"פרט {_tag(i)}: השווי הוא 9,500 ₪ [S1]." for i in range(20)))
    r = verify_answer(p, a, _ws(SOURCE), "?", [])
    assert r.ok and calls == [20, 10, 10]


# --- evidence in the judge's input ------------------------------------------------------------------------

def _long_source(late: str, early: str = "") -> str:
    filler = [f"פסקה {i}: תיאור כללי של הסביבה, התשתיות והנגישות בשכונה." for i in range(80)]
    return "\n".join([early, *filler[:60], late, *filler[60:]] if early else [*filler[:60], late, *filler[60:]])


def test_the_judge_reads_evidence_past_the_first_1800_characters():
    late = "בהתאם לסקר, שכר הדירה החודשי הראוי הוא 55 ₪ למ\"ר לחודש."
    src = _long_source(late, early="במגרש קיימות 55 חניות תת-קרקעיות.")
    assert src.index(late) > 1800
    p = _judge(lambda t: "supported")
    verify_answer(p, _answer("שכר הדירה החודשי הוא 55 ₪ למ\"ר [S1]."), _ws(src), "?", [])
    seen = p.calls[0].input
    assert late in seen and "55 חניות" in seen and 'excerpt="true"' in seen


def test_each_source_is_sent_once_per_call():
    p = _judge(lambda t: "supported")
    a = _answer("\n".join(f"פרט {_tag(i)}: השווי למ\"ר הוא 9,500 ₪ [S1]." for i in range(10)))
    verify_answer(p, a, _ws(SOURCE), "?", [])
    assert p.calls[0].input.count('<source id="S1"') == 1


def test_judge_calls_are_packed_by_evidence_size_without_cutting():
    big = [_long_source(f"בבניין {n} השווי למ\"ר הוא {9000 + n} ₪.") for n in range(6)]
    ws = _ws(*[b.replace("פסקה", f"פסקה-{n}") for n, b in enumerate(big)])
    lines = [f"בבניין {n} השווי למ\"ר הוא {9000 + n} ₪ [S{n + 1}]." for n in range(6)]
    p = _judge(lambda t: "supported")
    monkey = verify.JUDGE_CALL_CHARS
    try:
        verify.JUDGE_CALL_CHARS = 3000
        r = verify_answer(p, _answer("\n".join(lines)), ws, "?", [])
    finally:
        verify.JUDGE_CALL_CHARS = monkey
    assert r.ok and len(p.calls) >= 2
    for n in range(6):  # every claim's own evidence reached the judge
        assert any(f"בבניין {n} השווי למ\"ר הוא {9000 + n} ₪." in c.input for c in p.calls)


def test_unit_dataclass_still_exposes_raw_and_ids():
    (u,) = split_units("השווי הוא 9,500 ₪ [S1][M2].")
    assert isinstance(u, Unit) and u.ids == ["S1", "M2"]


# --- VAT stays with the value it was written for ---------------------------------------------------------------

PAIR = ("סיכום: השווי למ\"ר בנוי למסחר נקבע ל-9,800 ₪, ללא מע\"מ, ודמי השכירות למ\"ר נקבעו ל-58 ₪ "
        "לחודש.")


def _deterministic(markdown: str, *sources: str):
    return verify.deterministic(split_units(markdown), _ws(*sources), "?")


def test_vat_written_for_one_value_is_not_given_to_the_other():
    problems = _deterministic("דמי השכירות הם 58 ₪ למ\"ר לחודש, ללא מע\"מ [S1].", PAIR)
    assert problems and "מע\"מ" in problems[0].reason and "58" in problems[0].reason


def test_vat_of_the_value_itself_passes():
    assert _deterministic("השווי למ\"ר בנוי למסחר הוא 9,800 ₪, ללא מע\"מ [S1].", PAIR) == []
    assert _deterministic("דמי השכירות הם 58 ₪ למ\"ר לחודש [S1].", PAIR) == []


def test_vat_with_the_other_polarity_fails():
    problems = _deterministic("השווי למ\"ר הוא 9,800 ₪ כולל מע\"מ [S1].", PAIR)
    assert problems and "9,800" in problems[0].reason


def test_a_general_vat_note_covers_the_table():
    table = "שכירות מבוקשת:\nהדקל 2 | ₪ 55\nהדקל 9 | ₪ 58\n(*) המחירים המבוקשים אינם כוללים מע\"מ."
    assert _deterministic("ברחוב הדקל 9 מבוקשים 58 ₪ למ\"ר, ללא מע\"מ [S1].", table) == []


@pytest.mark.parametrize("written", ["לא כולל מ.ע.מ.", "אינו כולל מ.ע.מ", "לא כולל מע״מ", "אינו כולל מעמ",
                                     "לא כולל מס ערך מוסף", "לא כולל המס ערך מוסף", "אינו כולל מע''מ"])
@pytest.mark.parametrize("claim", ["ללא מע\"מ", "ללא מע״מ", "לא כולל מס ערך מוסף", "לפני מ.ע.מ."])
def test_every_written_form_of_the_vat_is_read_in_the_source_and_the_claim(written, claim):
    """Round 13 W5/V2/W10: a source writing the VAT as "מ.ע.מ.", "מעמ" or "מס ערך מוסף" backs a claim writing it as
    "מע״מ" and the reverse (synthetic sentences)."""
    source = f"שווי הנכס לעיל הוא 2,450,000 ₪, {written}. מועד הביקור 01.02.2020."
    assert _deterministic(f"שווי הנכס הוא 2,450,000 ₪, {claim} [S1].", source) == []


def test_a_dotted_vat_abbreviation_does_not_split_the_sentence_and_the_other_polarity_still_fails():
    source = "השווי 2,450,000 ₪ אינו כולל מ.ע.מ. והוא נקבע ליום 01.02.2020"
    pairs, general = verify.vat_attachments(source)
    assert pairs == {(f, "excluded") for f in verify.numbers_in("2,450,000")} and not general
    assert _deterministic("השווי הוא 2,450,000 ₪, כולל מע\"מ [S1].", source)


def test_vat_words_and_attested_read_the_written_forms():
    assert meaning.vat_words("המחיר אינו כולל מ.ע.מ.") == {"excluded": "אינו כולל מ.ע.מ"}
    assert meaning.vat_words("המחיר כולל מס ערך מוסף").get("included", "").endswith("מס ערך מוסף")
    assert meaning.vat_attested("שווי 2,450,000 ₪ אינו כולל מעמ", frozenset(verify.numbers_in("2,450,000"))) == {"excluded"}


def test_a_word_that_only_looks_like_a_vat_abbreviation_is_not_one():
    assert verify.vat_attachments("המעמד של 5,000 ₪ לא כולל מעמד נוסף")[0] == set()


@pytest.mark.parametrize("wrapped, polarity", [
    ('בסך של 2,450,000 ₪ לא\nכולל מע"מ.', "excluded"),
    ('בסך של 2,450,000 ₪ ללא\nמע"מ.', "excluded"),
    ('בסך של 2,450,000 ₪ אינו\nכולל מע״מ.', "excluded"),
    ('בסך של 2,450,000 ₪ לא \n כולל מ.ע.מ.', "excluded"),
    ('בסך של 2,450,000 ₪ (שני מיליון ארבע מאות חמישים אלף שקלים חדשים) לא\nכולל מע"מ.', "excluded"),
    ('בסך של 2,450,000 ₪ כולל\nמע"מ.', "included"),
])
def test_a_line_break_inside_a_vat_phrase_does_not_reverse_its_status(wrapped, polarity):
    """Round 13: PDF text wraps a line between the negation and the rest of the VAT phrase (synthetic sentences)."""
    pairs, general = verify.vat_attachments(wrapped)
    assert pairs == {(f, polarity) for f in verify.numbers_in("2,450,000")} and not general


def test_a_claim_is_backed_by_a_source_that_wraps_the_vat_phrase_across_lines():
    source = 'שווי הנכס נקבע בסך של 2,450,000 ₪ (שני מיליון ארבע מאות חמישים אלף שקלים חדשים) לא\nכולל מע"מ.'
    assert _deterministic('השווי הוא 2,450,000 ₪, לא כולל מע״מ [S1].', source) == []
    assert _deterministic('השווי הוא 2,450,000 ₪, כולל מע"מ [S1].', source)
    unit = split_units('השווי הוא 2,450,000 ₪, לא כולל מע״מ [S1].')[0]
    assert verify._vat_problems(unit, _ws(source)) == []


def test_a_line_break_between_table_rows_still_ends_the_clause():
    # the next row starts with the VAT phrase and does not continue a negation: it stays a general note, as before
    pairs, general = verify.vat_attachments('שווי 1,000 ₪\nכולל מע"מ: לא צוין')
    assert pairs == set() and general == {"included"}
    pairs, general = verify.vat_attachments('הדקל 9 | ₪ 58\nלא\nכולל מע"מ')
    assert pairs == set() and general == {"excluded"}


def test_vat_spans_offsets_point_at_the_phrase_in_the_original_text():
    text = 'בסך של 2,450,000 ₪ לא\nכולל מע"מ.'
    spans = list(verify._vat_spans(text))
    assert len(spans) == 1
    forms, polarity, a, b = spans[0]
    assert polarity == "excluded" and forms == verify.numbers_in("2,450,000")
    assert text[a:b] == 'לא\nכולל מע"מ'


def test_vat_claim_backed_by_a_measurement_record():
    ws = _ws(PAIR)
    from types import SimpleNamespace

    from app.chat.tools import Measurement

    row = SimpleNamespace(value_text="58 ₪", quote=PAIR, vat="excluded", metric="דמ\"ש", metric_kind="rent_per_area",
                          unit="ILS_per_sqm", period="month", area_basis=None, subject=None,
                          value_role="appraiser_determination", status="auto_validated", section=None,
                          block_index=1, table_index=None)
    ws.measurements["M1"] = Measurement("M1", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "דוח", row)
    assert verify.deterministic(split_units("דמי השכירות הם 58 ₪ ללא מע\"מ [M1]."), ws, "?") == []


def _registered(written: str, quote: str, *, vat: str = "not_stated", area_basis: str = "", unit: str = "ILS",
                period: str = "none", provenance: dict | None = None) -> Workspace:
    """A workspace whose V1 is a registered value of the invented property: ``vat``, ``area_basis`` and ``period``
    as the record holds them, each with the ``provenance`` entry given (default: all from the source)."""
    from decimal import Decimal

    from app.chat import calc

    ws = Workspace(ctx=None)
    prov = {"unit": "source", "vat": "source", "area_basis": "source", "period": "source"} | (provenance or {})
    ws.values["V1"] = calc.Value("V1", Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(),
                                 None, "שומת הדגמה", "סעיף 3", "שווי הנכס", "value", unit, period, vat, area_basis,
                                 "נכס הדגמה", "appraiser_determination", prov, {"quote": quote}, quote)
    return ws


# The quote carries no VAT words (the VAT basis is on the next source line); the record's own VAT is source-given.
_VALUE_QUOTE = "שווי הנכס: 2,450,000 ₪"


def test_vat_claim_backed_by_a_source_given_value_record():
    """Round 11 U2, KTD10 (AE6): a cited V# backs the VAT its record holds when the source gave it."""
    ws = _registered("2,450,000", _VALUE_QUOTE, vat="included")
    assert verify.deterministic(split_units("שווי הנכס הוא 2,450,000 ₪, כולל מע\"מ [V1]."), ws, "?") == []


def test_a_different_vat_than_the_source_given_one_is_still_removed():
    ws = _registered("2,450,000", _VALUE_QUOTE, vat="included")
    (problem,) = verify.deterministic(split_units("שווי הנכס הוא 2,450,000 ₪, לא כולל מע\"מ [V1]."), ws, "?")
    assert problem.check == "vat" and problem.failure_kind == "wrong_unit"


@pytest.mark.parametrize("vat,provenance", [
    ("included", {"vat": "model_asserted"}),  # the model said so; the source did not
    ("excluded", {"vat": "model_asserted"}),
    ("not_stated", {"vat": "not_stated"}),
    ("not_stated", {"vat": "source"}),
])
def test_a_value_record_without_a_source_given_vat_backs_no_vat_claim(vat, provenance):
    ws = _registered("2,450,000", _VALUE_QUOTE, vat=vat, provenance=provenance)
    for claim in ("כולל מע\"מ", "לא כולל מע\"מ"):
        problems = verify.deterministic(split_units(f"שווי הנכס הוא 2,450,000 ₪, {claim} [V1]."), ws, "?")
        assert [(p.check, p.failure_kind) for p in problems] == [("vat", "wrong_unit")], claim


# --- the date a cited value or measurement states backs the same date in a claim (round 11 U15 B) --------------------

_PASSAGE = "סיכום השומה: השווי נקבע לפי גישת ההשוואה בסך 1,950,000 ₪ כולל מע\"מ."   # the passage holds no date


def _dated(as_of, *, passage: str = _PASSAGE, provenance: str = "unit_valuation"):
    """A workspace with S1 (a passage without the valuation-date line) and V1, the registered value in it, whose
    server-resolved ``as_of`` is ``as_of`` (a dict) or None."""
    ws = _registered("1,950,000", "השווי נקבע בסך 1,950,000 ₪ כולל מע\"מ", vat="included")
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section="סיכום",
                  location="סעיף 3", kind="context", text=passage)
    if as_of is not None:
        ws.values["V1"].as_of = {"end": None, "forecast": False, "locator": "block 1", "provenance": provenance} | as_of
    return ws


def _numbers(markdown: str, ws: Workspace) -> list:
    return [p for p in verify.deterministic(split_units(markdown), ws, "?") if p.check == "unstated_number"]


CLAIM_DAY = "השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, למועד הקובע 10/03/2022 [V1][S1]."


@pytest.mark.parametrize("written", ["10/03/2022", "10.3.2022", "10.03.2022", "10/3/2022"])
def test_a_date_the_cited_values_as_of_states_is_backed_in_any_written_form(written):
    ws = _dated({"date": "2022-03-10", "precision": "day"})
    assert _numbers(CLAIM_DAY.replace("10/03/2022", written), ws) == []


@pytest.mark.parametrize("provenance", ["source", "source_table", "unit_valuation"])
def test_every_server_provenance_of_the_as_of_backs_its_date(provenance):
    ws = _dated({"date": "2022-03-10", "precision": "day"}, provenance=provenance)
    assert _numbers(CLAIM_DAY, ws) == []


def test_without_the_as_of_the_same_claim_is_cut_as_unstated():
    """The red of the real-model run: S7 holds no valuation-date line, V2's as_of was never read."""
    for ws in (_dated(None), _dated({"date": None, "precision": None, "forecast": True, "provenance": None})):
        (problem,) = _numbers(CLAIM_DAY, ws)
        assert "10/03/2022" in problem.reason or set(problem.unstated) >= {"2022"}


def test_a_different_date_than_the_as_of_is_still_flagged():
    ws = _dated({"date": "2022-03-10", "precision": "day"})
    for other in ("11/03/2022", "10/04/2022", "10/03/2021", "03/10/2022"):
        (problem,) = _numbers(CLAIM_DAY.replace("10/03/2022", other), ws)
        assert problem.unstated, other


def test_a_number_that_only_looks_like_a_part_of_the_date_is_not_backed():
    ws = _dated({"date": "2022-03-10", "precision": "day"})
    (problem,) = _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, בעבור 10 יחידות [V1][S1].", ws)
    assert "10" in problem.unstated


def test_a_month_as_of_backs_the_month_and_not_a_day_of_it():
    ws = _dated({"date": "2022-03", "precision": "month"})
    assert _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, נכון ל-03/2022 [V1][S1].", ws) == []
    assert _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, נכון ל-10/03/2022 [V1][S1].", ws)


def test_a_year_as_of_backs_the_year():
    ws = _dated({"date": "2022", "precision": "year"})
    assert _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, נכון לשנת 2022 [V1][S1].", ws) == []
    assert _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, נכון לשנת 2023 [V1][S1].", ws)


def test_a_range_as_of_backs_both_ends():
    ws = _dated({"date": "2021", "precision": "year", "end": "2022"}, provenance="source_table")
    assert _numbers("מחירי העסקאות בין 2021 ל-2022 הגיעו ל-1,950,000 ₪ כולל מע\"מ [V1][S1].", ws) == []
    assert _numbers("מחירי העסקאות בין 2021 ל-2023 הגיעו ל-1,950,000 ₪ כולל מע\"מ [V1][S1].", ws)


def test_the_as_of_of_a_value_the_unit_does_not_cite_backs_nothing():
    ws = _dated({"date": "2022-03-10", "precision": "day"})
    assert _numbers("השווי נקבע ל-1,950,000 ₪ כולל מע\"מ, למועד הקובע 10/03/2022 [S1].", ws)


def test_a_measurements_as_of_backs_its_date_too():
    from types import SimpleNamespace

    from app.chat.tools import Measurement

    ws = _ws("דמי השכירות הם 55 ₪ למ\"ר לחודש.")
    row = SimpleNamespace(value_text="55 ₪", quote="דמי השכירות הם 55 ₪ למ\"ר לחודש", vat="unknown", metric="דמי שכירות",
                          metric_kind="rent_per_area", unit="ILS_per_sqm", period="month", area_basis=None, subject=None,
                          value_role="appraiser_determination", status="auto_validated", section=None, block_index=1,
                          table_index=None)
    m = Measurement("M1", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "דוח", row)
    m.as_of = {"date": "2023-06-15", "precision": "day", "end": None, "provenance": "source", "forecast": False,
               "locator": "quote"}
    ws.measurements["M1"] = m
    assert _numbers("דמי השכירות הם 55 ₪ למ\"ר לחודש נכון ל-15/06/2023 [M1][S1].", ws) == []
    assert _numbers("דמי השכירות הם 55 ₪ למ\"ר לחודש נכון ל-16/06/2023 [M1][S1].", ws)


def test_the_judge_reads_the_date_and_where_it_came_from_in_the_value_record():
    ws = _dated({"date": "2022-03-10", "precision": "day"})
    heading, body, kind = verify._source_parts(ws, "V1")
    assert kind == "value" and "10/03/2022" in body and "המועד הקובע של היחידה" in body
    unknown = _dated(None)
    assert "10/03/2022" not in verify._source_parts(unknown, "V1")[1]


def test_a_source_given_area_basis_on_a_value_record_backs_the_matching_claim_only():
    ws = _registered("9,500", "דמי השכירות: 9,500 ₪", unit="ILS_per_sqm", area_basis="נטו", period="month")
    assert verify.deterministic(split_units("דמי השכירות הם 9,500 ₪ למ\"ר נטו לחודש [V1]."), ws, "?") == []
    problems = verify.deterministic(split_units("דמי השכירות הם 9,500 ₪ למ\"ר ברוטו לחודש [V1]."), ws, "?")
    assert any(p.check == "meaning" for p in problems), [(p.check, p.failure_kind) for p in problems]


def test_a_numbered_heading_or_table_header_judged_navigation_or_not_factual_is_kept():
    def rule(t):
        if t.startswith("###"):
            return "navigation"
        return "not_factual" if "שווי 2024" in t else "supported"
    md = "### הדקל 14\n| נכס | שווי 2024 |\n|---|---|\n| הדקל 14 | 9,500 ₪ [S1] |"
    r = verify_answer(_judge(rule), _answer(md), _ws(SOURCE + " הדקל 14 2024"), "?", [])
    assert r.ok, [x.reason for x in r.problems]
    (head, header, row) = split_units(md)
    assert structural_kind(header) == "table_header" and structural_kind(row) is None


def test_a_table_header_asserting_a_value_judged_not_factual_fails():
    p = _judge(lambda t: "not_factual" if "פנוי" in t else "supported")
    md = "| הנכס פנוי | 9,500 ₪ |\n|---|---|\n| הגפן | 9,500 ₪ [S1] |"
    r = verify_answer(p, _answer(md), _ws(SOURCE), "?", [])
    assert [x.unit.text for x in r.problems] == ["| הנכס פנוי | 9,500 ₪ |"]


@pytest.mark.parametrize("claim", [
    "השווי הוא 9,500 ₪ למ\"ר (שטח 120 מ\"ר), לא כולל מע\"מ [S1].",
    "השווי בשנת 2024 הוא 9,500 ₪ למ\"ר, לא כולל מע\"מ [S1].",
])
def test_vat_skips_a_year_an_area_or_a_parenthesized_number(claim):
    source = "השווי למ\"ר נקבע ל-9,500 ₪, לא כולל מע\"מ. שטח הנכס 120 מ\"ר. השומה נכונה לשנת 2024."
    assert _deterministic(claim, source) == []


PERMIT_FEE = "אגרת היתר מס׳ 638 מיום 01.11.1966: 70 ₪, לא כולל מע\"מ."


def test_vat_is_not_given_to_a_permit_number_or_a_date():
    # the nearest "number" before the VAT phrase is the date's day and month, not the fee
    claim = "האגרה, 70 ₪, נקבעה בהיתר מס׳ 638 מיום 01.11.1966, ללא מע\"מ [S1]."
    assert _deterministic(claim, PERMIT_FEE) == []


def test_an_absence_sentence_gets_no_vat_check():
    for claim in ("לא צוין אם הסכום של 70 ₪ כולל מע\"מ [S1].", "לא צוין אם 70 ₪ לא כולל מע\"מ [S1].",
                  "לא נמצא במסמך אם 70 ₪ כולל מע\"מ [S1]."):
        assert _deterministic(claim, "הסכום הוא 70 ₪ לחודש.") == [], claim


def test_a_claimed_vat_after_an_absence_clause_is_still_checked():
    problems = _deterministic("לא צוין היטל נוסף; הסכום 70 ₪ כולל מע\"מ [S1].", "הסכום הוא 70 ₪ לחודש.")
    assert problems and problems[0].check == "vat"


def test_a_date_with_a_closing_period_is_stated_by_a_source_with_a_comma():
    # AE7: the source writes "01.11.1966," and the answer "01.11.1966." — same date, the claim stays
    source = "היתר הבנייה הוא מס׳ 638, מיום 01.11.1966, ממנו נגזרות הזכויות."
    for claim in ("היתר הבנייה הוא מס׳ 638, מיום 01.11.1966. [S1]", "היתר הבנייה בתוקף מיום 01.11.1966, [S1]"):
        assert _deterministic(claim, source) == [], claim
    assert _deterministic("ההיתר בתוקף עד 28.12.2022. [S1]", "ההיתר בתוקף עד 28.12.2022, ללא הארכה.") == []


def test_a_failed_header_and_a_failed_row_remove_the_whole_table():
    md = "השווי למ\"ר הוא 9,500 ₪ [S1].\n\n| הנכס פנוי | כן |\n|---|---|\n| הגפן | 9,500 ₪ [S1] |\n| הזית | 7 [S1] |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "supported" if t.startswith("השווי") or "הגפן" in t else None), a, _ws(SOURCE),
                      "?", [])
    out = r.apply(a).answer_markdown
    assert "|" not in out and "הגפן" not in out and out.startswith("השווי למ\"ר הוא 9,500")


# --- numbered headings stay whole, and removal leaves no fragment (R22) ------------------------------------------

@pytest.mark.parametrize("md", ["9. השומה\nהשווי למ\"ר הוא 9,500 ₪ [S1].", "## 9. השומה\nהשווי למ\"ר הוא 9,500 ₪ [S1].",
                                "9.1 שיטת השומה\nהשווי למ\"ר הוא 9,500 ₪ [S1].", "**9.2. השומה**\nהנכס פנוי [S1]."])
def test_a_numbered_heading_is_one_unit(md):
    head = split_units(md)[0]
    assert head.raw == md.split("\n")[0] and len(split_units(md)) == 2


def test_a_numbered_heading_followed_by_text_on_its_line_is_never_split():
    (u,) = split_units("9. השומה: השווי למ\"ר הוא 9,500 ₪ [S1].")
    assert u.raw.startswith("9. השומה") and u.ids == ["S1"]


def test_a_numbered_heading_passes_as_navigation_and_its_number_is_no_claim():
    p = _judge(lambda t: "supported" if "9,500" in t else None)
    r = verify_answer(p, _answer("9. השומה\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(SOURCE), "?", [])
    assert r.ok, [x.reason for x in r.problems]


def test_a_numbered_list_item_keeps_its_number_with_its_claim():
    md = "1. השווי למ\"ר הוא 9,500 ₪ [S1].\n2. הנכס מושכר לטווח ארוך [S1]."
    assert [u.raw for u in split_units(md)] == md.split("\n")
    p = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    a = _answer(md)
    out = verify_answer(p, a, _ws(SOURCE), "?", []).apply(a).answer_markdown
    assert out.startswith("1. השווי למ\"ר הוא 9,500 ₪ [S1].") and "2." not in out and "מושכר" not in out


def test_removing_a_claim_in_the_middle_of_a_sentence_removes_the_whole_sentence():
    md = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
          "התשלום כולל שני רכיבים: 1. מקדמה של 41 ₪ [S1] ו-2. יתרה של 55 ₪ [S1], שתיהן לפני החתימה.\n"
          "הנכס פנוי [S1].")
    p = _judge(lambda t: "unsupported" if "מקדמה" in t else "supported")
    a = _answer(md)
    r = verify_answer(p, a, _ws(SOURCE + " 41"), "?", [])
    out = r.apply(a).answer_markdown
    for fragment in ("התשלום", "רכיבים", "יתרה", "החתימה", ": 1."):
        assert fragment not in out, out
    assert "השווי למ\"ר הוא 9,500 ₪ [S1]." in out and "הנכס פנוי [S1]." in out


def test_a_bullet_whose_content_went_leaves_no_empty_bullet():
    md = "השווי למ\"ר הוא 9,500 ₪ [S1].\n- הנכס מושכר לטווח ארוך [S1].\n- הנכס פנוי [S1]."
    p = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    a = _answer(md)
    out = verify_answer(p, a, _ws(SOURCE), "?", []).apply(a).answer_markdown
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert all(re.search(r"[א-ת]", ln) for ln in lines), out
    assert "- הנכס פנוי [S1]." in out and "מושכר" not in out


def test_the_first_sentence_of_a_bullet_goes_and_the_bullet_keeps_its_marker():
    md = "- הנכס מושכר לטווח ארוך [S1]. הנכס פנוי [S1]."
    p = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    a = _answer(md)
    out = verify_answer(p, a, _ws(SOURCE), "?", []).apply(a).answer_markdown
    assert out.startswith("- הנכס פנוי [S1].")


def test_a_heading_whose_whole_content_went_goes_with_it():
    md = ("## שווי\nהשווי למ\"ר הוא 9,500 ₪ [S1].\n\n## שכירות\n- הנכס מושכר לטווח ארוך [S1].\n"
          "- השוכר הוא חברת בדיקה [S1].")
    p = _judge(lambda t: "supported" if "9,500" in t or t.startswith("##") else "unsupported")
    a = _answer(md)
    out = verify_answer(p, a, _ws(SOURCE), "?", []).apply(a).answer_markdown
    assert "## שכירות" not in out and "## שווי" in out and "9,500" in out


# --- the coverage plane: every part of the request is answered or said to be missing (R19) ------------------------

# --- a correct claim that cites nothing, next to a claim citing its source: the judge names the source -------------

TABLE = ("טבלת שלבים: שלב א — סף 40 נקודות, יחס 0.62, עבר. שלב ב — סף 55 נקודות, יחס 0.71, עבר. "
         "סה\"כ הפרויקט: 95 נקודות, עבר.")
OTHER = "נספח: בשלב ג נקבע סף של 63 נקודות."


def _supported_by(rule) -> ScriptedProvider:
    """A judge answering each unit by ``rule(unit_text, cites) -> (verdict, supported_by)``."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="([^"]*)">\n(.*?)\n</unit>', input, re.S):
            verdict, by = rule(m.group(3), m.group(2))
            out.append({"index": int(m.group(1)), "verdict": verdict, "reason": "בדיקה", "supported_by": by})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _named(source: str, also: str | None = None):
    """Every unit supported; one citing nothing is said to be supported by ``source``."""
    return lambda text, cites: ("supported", [] if cites else [source, *([also] if also else [])])


def test_the_judge_schema_and_policy_name_the_supporting_source():
    assert "supported_by" in verify.JudgeVerdict.model_fields and "supported_by" in verify.JUDGE_POLICY
    v = verify.JudgeVerdict(index=0, verdict="supported", reason="x")  # optional: older replies stay valid
    assert v.supported_by == []


def test_an_uncited_correct_sentence_next_to_a_cited_one_is_kept_with_the_citation_appended():
    md = "שלב א עבר עם סף 40 נקודות [S1]. בשלב ב הסף היה 55 נקודות והיחס 0.71, ולכן גם הוא עבר."
    a = _answer(md)
    r = verify_answer(_supported_by(_named("S1")), a, _ws(TABLE), "?", [])
    assert r.ok and not r.removed_units()
    out = r.apply(a).answer_markdown
    assert out == "שלב א עבר עם סף 40 נקודות [S1]. בשלב ב הסף היה 55 נקודות והיחס 0.71, ולכן גם הוא עבר [S1]."


def test_an_uncited_sentence_whose_number_is_not_in_the_named_source_is_removed():
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ג נקבע סף של 63 נקודות."
    a = _answer(md)  # 63 is a number of the turn (S2), so it passes the uncited number check, but not of S1
    r = verify_answer(_supported_by(_named("S1")), a, _ws(TABLE, OTHER), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]
    out = r.apply(a).answer_markdown
    assert "63" not in out and "40 נקודות [S1]" in out


def test_supported_by_a_source_not_shown_in_the_batch_is_ignored_and_the_unit_removed():
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ג נקבע סף של 40 נקודות."
    a = _answer(md)  # S2 holds none of the unit's numbers: no unit cites it and it is no candidate, so it was never shown
    r = verify_answer(_supported_by(_named("S2")), a, _ws(TABLE, OTHER), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]
    assert "בשלב ג" not in r.apply(a).answer_markdown


def test_a_source_the_turn_read_that_holds_the_number_is_shown_for_an_uncited_unit_and_its_named_support_is_accepted():
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ג נקבע סף של 63 נקודות."
    a = _answer(md)  # S2 states the 63 and no unit cites it: the turn read it, so it is shown beside the uncited unit
    p = _supported_by(_named("S2"))
    r = verify_answer(p, a, _ws(TABLE, OTHER), "?", [])
    assert '<source id="S2" excerpt="false" candidate="true"' in p.calls[0].input
    assert not [x for x in r.problems if x.removes_unit]
    assert r.apply(a).answer_markdown == "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ג נקבע סף של 63 נקודות [S2]."


@pytest.mark.parametrize("named", ["S7", "P1", "A1"])
def test_supported_by_an_id_that_is_no_evidence_of_the_turn_is_removed(named):
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ב הסף היה 55 נקודות."
    a = _answer(md)
    r = verify_answer(_supported_by(_named("S1", also=named)), a, _ws(TABLE), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]


def test_an_uncited_claim_no_shown_source_holds_stays_unsupported_though_the_judge_names_one():
    md = "שלב א עבר עם סף 40 נקודות.\nהנכס פנוי."
    a = _answer(md)  # S1 holds the first unit's 40, so it is shown for it; for "הנכס פנוי" nothing is, and naming S1 fails
    p = _supported_by(lambda t, c: ("supported", ["S1"]))
    r = verify_answer(p, a, _ws(TABLE), "?", [])
    assert '<source id="S1" excerpt="false" candidate="true"' in p.calls[0].input
    assert sorted(x.unit.index for x in r.problems if x.removes_unit) == [1]


def test_a_cited_unit_naming_its_own_source_is_unchanged():
    md = "שלב א עבר עם סף 40 נקודות [S1]."
    a = _answer(md)
    r = verify_answer(_supported_by(lambda t, c: ("supported", ["S1"])), a, _ws(TABLE), "?", [])
    assert r.ok and not r.problems and r.apply(a).answer_markdown == md


def test_supported_by_is_ignored_for_a_verdict_other_than_supported():
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ב הסף היה 55 נקודות."
    a = _answer(md)
    r = verify_answer(_supported_by(lambda t, c: ("supported", []) if c else ("unsupported", ["S1"])), a,
                      _ws(TABLE), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]


def test_an_uncited_table_row_gets_the_citation_inside_its_last_cell():
    md = "שלב א עבר עם סף 40 נקודות [S1].\n\n| שלב | סף | יחס |\n|---|---|---|\n| ב | 55 | 0.71 |"
    a = _answer(md)
    p = _supported_by(lambda t, c: ("navigation" if "שלב" in t and "|" in t else "supported",
                                    [] if c or "שלב" in t else ["S1"]))
    r = verify_answer(p, a, _ws(TABLE), "?", [])
    assert r.ok and not r.removed_units()
    assert r.apply(a).answer_markdown.endswith("| ב | 55 | 0.71 [S1] |")


# --- a part not covered whose only limitation is a document the turn read in part ---------------------------------


# --- instruction components are checked against the shown answer, never searched (round 7 KTD2, R4, AE1) ----------

STYLE, CITE = "כתיבה בסגנון מקצועי", "מראה מקום לכל נתון"


def _instructed(*extra: dict) -> verify.TurnRequirements:
    turn = verify.TurnRequirements()
    turn.adopt([{"id": "N1", "text": "השווי למ\"ר"}, {"id": "N2", "text": STYLE, "kind": "instruction", "aspect": "style"},
                {"id": "N3", "text": CITE, "kind": "instruction", "aspect": "citation"}, *extra], "analysis")
    return turn


def _scoring(table: dict[str, str], seen: list | None = None, rule=lambda text: "supported") -> ScriptedProvider:
    """Every unit by ``rule``; each frozen requirement ``table[id]`` (default ``missing``), given by every unit when
    it is ``full``."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        return {"verdicts": [{"index": i, "verdict": rule(t), "reason": "בדיקה"} for i, t in units.items()],
                "requirements": [{"id": i, "status": table.get(i, "missing"),
                                  "units": list(units) if table.get(i) == "full" else [], "related": [],
                                  "reason": "בדיקה"} for i in ids]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def test_the_judge_scores_instructions_as_a_separate_list_against_the_answer():
    seen: list[str] = []
    verify_answer(_scoring({"N1": "full", "N2": "full"}, seen), _answer("השווי למ\"ר הוא 9,500 ₪ [S1]."),
                  _ws(SOURCE), "?", [], requirements=_instructed())
    requirements = re.search(r"<requirements>(.*?)</requirements>", seen[0], re.S).group(1)
    instructions = re.search(r"<instructions>(.*?)</instructions>", seen[0], re.S).group(1)
    assert 'id="N1"' in requirements and "N2" not in requirements and "N3" not in requirements
    assert 'id="N2"' in instructions and 'aspect="style"' in instructions and 'id="N3"' in instructions


def test_a_citation_instruction_is_met_when_every_datum_of_the_shown_answer_is_cited_whatever_the_judge_says():
    r = verify_answer(_scoring({"N1": "full", "N2": "full", "N3": "missing"}),
                      _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1]."), _ws(SOURCE), "?", [],
                      requirements=_instructed())
    assert r.ok and not r.problems
    (cite,) = [o for o in r.requirement_outcomes() if o["id"] == "N3"]
    assert cite["status"] == "full" and cite["check"] == "citation"


def test_a_datum_without_a_citation_leaves_the_citation_instruction_unmet_naming_it_and_asks_for_an_answer_change():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש.")
    r = verify_answer(_scoring({"N1": "full", "N2": "full", "N3": "full"}), a, _ws(SOURCE), "?", [],
                      requirements=_instructed())
    (cite,) = [o for o in r.requirement_outcomes() if o["id"] == "N3"]
    assert cite["status"] == "not_answered" and cite["uncited"] == ["דמי השכירות הם 55 ₪ למ\"ר לחודש."]
    (problem,) = [p for p in r.problems if p.kind == "instruction"]
    assert not r.ok and problem.repairable and not problem.removes_unit and not r.removed_units()
    assert CITE in problem.reason and "דמי השכירות הם 55" in problem.reason
    assert "לחפש" not in r.problems_text()  # an answer change, never a search


def test_an_unmet_style_instruction_is_a_repair_problem_never_a_search_or_a_removal():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_scoring({"N1": "full", "N3": "full"}), a, _ws(SOURCE), "?", [], requirements=_instructed())
    (problem,) = r.problems
    assert problem.kind == "instruction" and STYLE in problem.reason and problem.repairable
    assert "לחפש" not in r.problems_text() and not r.removed_units()
    assert "לחפש" not in verify.REQ_INSTRUCTION
    # a rewrite from verified content can still meet an instruction: it is among the rewrite's problems
    assert STYLE in r.problems_text(claims_only=True)


def test_an_instruction_still_unmet_is_a_gap_of_its_own_reason_never_missing_from_the_documents():
    from app.chat import coverage

    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    turn = _instructed()
    r = verify_answer(_scoring({"N1": "full", "N3": "full"}), a, _ws(SOURCE), "?", [], requirements=turn)
    final, outcomes = coverage.state_components(_ws(SOURCE), a, r, turn)
    (style,) = [o for o in outcomes if o["id"] == "N2"]
    assert style["limitation"] == "instruction_not_met"
    gap = final.answer_markdown[len(a.answer_markdown):].strip()
    assert gap == f"הוראה שלא קוימה בתשובה: **{STYLE}**."
    assert not re.search(r"לא\s+(?:נמצא|נבדק|מופיע|אותר)", gap)


def test_the_judges_verdicts_are_kept_on_the_report():
    r = verify_answer(_judge(lambda t: "not_factual" if "פנוי" in t else "supported"),
                      _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."), _ws(SOURCE), "?", [])
    assert r.verdicts == {0: "supported", 1: "not_factual"}


# --- structured removal decisions (round 7 U4: KTD5, KTD10; R12, R14, R15) -----------------------------------------

BIG = "סיכום: שווי הנכס הוא 1,530,000 ₪. השווי למ\"ר הוא 14,250 ₪. הנכס פנוי."


def _decided(rule) -> ScriptedProvider:
    """A judge answering each unit by ``rule(text, input) -> dict`` (the verdict's fields; ``verdict`` at least), or
    None to leave the unit out."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
            v = rule(m.group(2), input)
            if v is not None:
                out.append({"index": int(m.group(1)), "reason": "השווי שייך לנכס אחר"} | v)
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def test_the_judge_verdict_carries_a_failure_kind_and_dependencies_with_defaults_for_older_replies():
    v = verify.JudgeVerdict(index=0, verdict="unsupported", reason="x")
    assert v.failure == "none" and v.depends_on == []
    fields = verify.JudgeVerdict.model_fields
    assert {"failure", "depends_on"} <= set(fields)
    assert set(verify.FAILURE_KINDS) == {"absent_from_source", "wrong_subject", "wrong_unit", "uncertain_reading",
                                         "contradicts_source", "wrong_calculation", "invalid_citation", "not_checked",
                                         "context_not_read"}
    # the judge never says "not checked": that is the server's when a check did not finish
    assert "not_checked" not in verify.JudgeVerdict.model_json_schema()["properties"]["failure"]["enum"]
    assert "failure" in verify.JUDGE_POLICY and "depends_on" in verify.JUDGE_POLICY


def test_a_judges_wrong_property_verdict_is_removed_as_wrong_subject_with_the_sources_checked():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].")
    p = _decided(lambda t, _: {"verdict": "unsupported", "failure": "wrong_subject"} if "שכירות" in t
                 else {"verdict": "supported"})
    r = verify_answer(p, a, _ws(SOURCE), "?", [])
    (decision,) = r.removals()
    assert (decision.unit.index, decision.failure_kind, decision.check) == (1, "wrong_subject", "judge")
    assert decision.checked_ids == ["S1"] and decision.repair_attempted is False
    stored = decision.as_dict()
    assert {"failure_kind", "check", "component", "checked_ids", "repair_attempted"} <= set(stored)


def test_an_unsupported_verdict_without_a_failure_kind_is_absent_from_the_source():
    a = _answer("הנכס מושכר לטווח ארוך [S1].")
    r = verify_answer(_judge(lambda t: "unsupported"), a, _ws(SOURCE), "?", [])
    assert [(d.failure_kind, d.check) for d in r.removals()] == [("absent_from_source", "judge")]


@pytest.mark.parametrize("markdown, sources, kind, check", [
    ("השווי למ\"ר הוא 9,500 ₪ [S7].", (SOURCE,), "invalid_citation", "unknown_id"),
    ("השווי למ\"ר הוא 1,234 ₪ [S1].", (SOURCE,), "absent_from_source", "unstated_number"),
    ("השווי למ\"ר הוא 9,500 ₪ כולל מע\"מ [S1].", ("השווי למ\"ר הוא 9,500 ₪ ללא מע\"מ. דמי הניהול 12 ₪ כולל מע\"מ.",),
     "wrong_unit", "vat"),
])
def test_each_deterministic_check_sets_its_failure_kind(markdown, sources, kind, check):
    r = verify_answer(_judge(lambda t: "supported"), _answer(markdown), _ws(*sources), "?", [])
    (decision,) = r.removals()
    assert (decision.failure_kind, decision.check) == (kind, check)
    assert decision.checked_ids == verify.split_units(markdown)[0].ids


@pytest.mark.parametrize("claim", [
    "שווי הנכס הוא 1.53 מיליון ₪ [S1].",  # an equivalent representation of 1,530,000
    "השווי למ\"ר הוא כ-14 אלף ₪ (בעיגול) [S1].",  # 14,250 rounded, and marked as rounded
    "השווי למ\"ר הוא כ-14 אלף ₪ (מעוגל מ-14,250 ₪) [S1].",
    "השווי למ\"ר הוא 14,250 ש\"ח, [S1].",  # punctuation and another spelling of the currency
])
def test_a_valid_claim_is_never_removed_for_its_representation_or_a_marked_correct_rounding(claim):
    r = verify_answer(_judge(lambda t: "supported"), _answer(claim), _ws(BIG), "?", [])
    assert not r.removed_units(), r.problems_text()


@pytest.mark.parametrize("claim", [
    "שווי הנכס הוא 1.6 מיליון ₪ [S1].",  # not 1,530,000 at the precision shown
    "השווי למ\"ר הוא כ-15 אלף ₪ [S1].",  # 14,250 does not round to 15 thousand
    "שווי הנכס הוא 1.53 אלף ₪ [S1].",  # another scale is another number
])
def test_a_wrong_number_in_another_representation_is_still_removed(claim):
    r = verify_answer(_judge(lambda t: "supported"), _answer(claim), _ws(BIG), "?", [])
    assert [d.failure_kind for d in r.removals()] == ["absent_from_source"]


def test_a_judge_timeout_on_one_batch_records_its_units_as_not_checked_never_as_wrong(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)
    p = ScriptedProvider()

    def respond(instructions: str, input: str):
        if 1 in _indexes(input):  # the batch of the second unit
            return StructuredResult(CallStatus.TIMEOUT, detail="timeout")
        return {"verdicts": [{"index": n, "verdict": "supported", "reason": "ok"} for n in _indexes(input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1].")
    usage: list = []
    r = verify_answer(p, a, _ws(SOURCE), "?", usage)
    (decision,) = r.removals()
    assert (decision.unit.index, decision.failure_kind) == (1, "not_checked")
    assert len(usage) == 3  # the batch that timed out was tried twice, never a third time
    (summary,) = r.counts()["removals"]
    assert summary == {"failure_kind": "not_checked", "component": None, "cause": "check_failed",
                       "text": verify.CHECK_CAUSE_SENTENCES["check_failed"], "claim_id": decision.claim_id,
                       "action": "withheld"}
    assert "שגוי" not in summary["text"].replace("לא נמצאה שגויה", "") and "תמיכה" not in summary["text"]
    out = r.apply(a).answer_markdown
    assert "פנוי" not in out and "תמיכה" not in out and out.endswith("> " + verify.CHECK_CAUSE_SENTENCES["check_failed"])  # its own cause (round 18 U7)


def test_a_judge_that_fails_on_every_batch_still_fails_the_verification(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)
    p = ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    with pytest.raises(VerificationUnavailable):
        verify_answer(p, _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1]."), _ws(SOURCE), "?", [])


def test_a_unit_the_judge_left_out_is_not_checked():
    p = _judge(lambda t: None if "פנוי" in t else "supported")
    r = verify_answer(p, _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."), _ws(SOURCE), "?", [])
    assert [(d.failure_kind, d.check) for d in r.removals()] == [("not_checked", "judge")]


def test_a_conclusion_resting_on_a_removed_claim_is_removed_too_and_no_broken_list_item_remains():
    md = ("- השווי למ\"ר הוא 9,500 ₪ [S1].\n"
          "- דמי השכירות הם 1,234 ₪ למ\"ר לחודש [S1].\n"
          "- מכאן עולה שהנכס מניב תשואה גבוהה.")
    seen: list[str] = []

    def rule(text, input):
        seen.append(input)
        if "מכאן" in text:  # its conclusion rests on the removed rent (unit 1), shown to the judge as removed
            return {"verdict": "supported", "depends_on": [1]}
        return {"verdict": "supported"}

    a = _answer(md)
    r = verify_answer(_decided(rule), a, _ws(SOURCE), "?", [])
    assert '<removed_unit index="1">' in seen[0] and "1,234" in seen[0]
    assert r.removed_units() == {1, 2}
    dependent = next(d for d in r.removals() if d.unit.index == 2)
    assert dependent.check == "dependency" and dependent.failure_kind == "absent_from_source"
    out = r.apply(a).answer_markdown
    assert out.split("\n\n")[0] == "- השווי למ\"ר הוא 9,500 ₪ [S1]."


def test_a_dependency_on_a_kept_claim_removes_nothing():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nמכאן עולה שהנכס יקר יחסית.")
    p = _decided(lambda t, _: {"verdict": "supported", "depends_on": [0]} if "מכאן" in t else {"verdict": "supported"})
    assert verify_answer(p, a, _ws(SOURCE), "?", []).ok


def test_the_public_removal_summary_has_the_kind_and_a_fixed_sentence_never_the_claim_or_the_judges_reason():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].\nהנכס פנוי [S1].")
    p = _decided(lambda t, _: {"verdict": "unsupported", "failure": "wrong_subject"} if "שכירות" in t
                 else {"verdict": "partial", "defect": "none"} if "פנוי" in t else {"verdict": "supported"})
    r = verify_answer(p, a, _ws(SOURCE), "?", [])
    counts = r.counts()
    assert counts["removed"] == len(counts["removals"]) == 1
    (summary,) = counts["removals"]
    (decision,) = r.removals()
    assert summary == {"failure_kind": "wrong_subject", "component": None,
                       "text": verify.REMOVAL_SENTENCES["wrong_subject"], "claim_id": decision.claim_id,
                       "action": "removed"}
    shown = json.dumps(counts, ensure_ascii=False)
    assert "שכירות" not in shown and "השווי שייך לנכס אחר" not in shown
    assert set(verify.REMOVAL_SENTENCES) == set(verify.FAILURE_KINDS)


def _note_lines(edited) -> list[str]:
    """The lines of the removal note the answer ends with (round 18 U7)."""
    return [ln for ln in edited.doc.text.split("\n\n")[-1].split("\n>\n")]


def test_a_wrong_subject_removal_note_says_the_passage_belongs_to_another_subject_and_not_that_no_support_was_found():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].")
    p = _decided(lambda t, _: {"verdict": "unsupported", "failure": "wrong_subject"} if "שכירות" in t
                 else {"verdict": "supported"})
    edited = verify_answer(p, a, _ws(SOURCE), "?", []).edit(a)
    assert _note_lines(edited) == ["> " + verify.REMOVAL_SENTENCES["wrong_subject"]]
    assert "לא נמצאה להן תמיכה" not in edited.doc.text and "לא נמצאה תמיכה" not in edited.doc.text


def test_each_cause_of_removal_gets_its_own_note_line_in_the_answers_order_and_a_repeated_cause_is_counted():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].\n"
                "הנכס מנוהל בידי חברת ניהול [S1].\nהנכס מושכר לטווח ארוך [S1].\nהנכס מרוהט [S1].")
    kinds = {"שכירות": "wrong_subject", "ניהול": "wrong_subject", "לטווח": "absent_from_source", "מרוהט": "contradicts_source"}
    p = _decided(lambda t, _: {"verdict": "unsupported", "failure": next(k for w, k in kinds.items() if w in t)}
                 if any(w in t for w in kinds) else {"verdict": "supported"})
    edited = verify_answer(p, a, _ws(SOURCE), "?", []).edit(a)
    assert _note_lines(edited) == [
        "> " + verify.REMOVAL_SENTENCES["wrong_subject"].rstrip(".") + " (2 פעמים).",
        "> " + verify.REMOVAL_SENTENCES["absent_from_source"],
        "> " + verify.REMOVAL_SENTENCES["contradicts_source"]]


def test_a_claim_that_could_not_be_checked_is_noted_with_its_own_cause_sentence(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)
    p = ScriptedProvider()

    def respond(instructions: str, input: str):
        if 1 in _indexes(input):
            return StructuredResult(CallStatus.TIMEOUT, detail="timeout")
        return {"verdicts": [{"index": n, "verdict": "supported", "reason": "ok"} for n in _indexes(input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1].")
    edited = verify_answer(p, a, _ws(SOURCE), "?", []).edit(a)
    assert _note_lines(edited) == ["> " + verify.CHECK_CAUSE_SENTENCES["check_failed"]]


def test_the_repair_prompt_names_each_problems_failure_kind_and_the_ids_checked():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].")
    p = _decided(lambda t, _: {"verdict": "unsupported", "failure": "wrong_subject"} if "שכירות" in t
                 else {"verdict": "supported"})
    text = verify_answer(p, a, _ws(SOURCE), "?", []).problems_text()
    assert verify.FAILURE_LABELS["wrong_subject"] in text and "S1" in text
    assert verify.REPAIR_HINTS["wrong_subject"] in text


# --- a bare answer is verified with the sentence that supports it (round 7 R15) --------------------------------------

THRESHOLD = "שיעור הרווח לעלות בפרויקט הוא 12.4%, והסף שנקבע הוא 15%."


@pytest.mark.parametrize("markdown, units", [
    ("לא. שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1].",
     ["לא. שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1]."]),
    ("**כן.** הסף של 15% נקבע בשומה [S1].", ["**כן.** הסף של 15% נקבע בשומה [S1]."]),
    ("- לא נכון. שיעור הרווח לעלות הוא 12.4% [S1].", ["- לא נכון. שיעור הרווח לעלות הוא 12.4% [S1]."]),
    # last in its line: joined to the sentence before it
    ("שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1]. לא.", ["שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1]. לא."]),
    # alone on its line: joined to the paragraph that explains it
    ("**לא.**\n\nשיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1].",
     ["**לא.**\n\nשיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1]."]),
    ("שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1].\n\nחלקית.",
     ["שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1].\n\nחלקית."]),
])
def test_a_bare_answer_is_one_unit_with_the_sentence_that_supports_it(markdown, units):
    assert [u.raw for u in split_units(markdown)] == units
    assert all(u.ids == ["S1"] for u in split_units(markdown))


@pytest.mark.parametrize("markdown, n", [
    ("לא נמצא בשומה שיעור רווח. הסף הוא 15% [S1].", 2),  # a claim, not a bare answer
    ("לא 15%. הסף הוא 15% [S1].", 2),  # a number: a claim of its own
    ("לא [S1]. הסף הוא 15% [S1].", 2),  # cited: a claim of its own
    ("## לא\n\n| שנה | שיעור |\n|---|---|\n| 2025 | 12.4% [S1] |", 3),  # never joined to a table row
])
def test_what_is_not_a_bare_answer_stays_a_unit_of_its_own(markdown, n):
    assert len(split_units(markdown)) == n


def test_a_bare_answer_with_its_supported_sentence_is_kept_and_an_uncited_claim_is_still_removed():
    ws = _ws(THRESHOLD)
    a = _answer("לא. שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1]. הנכס נמכר אשתקד.")
    judged: list[str] = []

    def rule(text: str) -> str:
        judged.append(text)
        return "supported" if "[S1]" in text or "12.4%" in text else "unsupported"

    r = verify_answer(_judge(rule), a, ws, "האם שיעור הרווח עומד בסף?", [])
    assert judged[0].startswith("לא. שיעור הרווח")
    applied = r.apply(a)
    assert applied.answer_markdown.startswith("לא. שיעור הרווח לעלות 12.4% נמוך מהסף של 15% [S1].")
    assert "הנכס נמכר אשתקד" not in applied.answer_markdown
    assert [(d.failure_kind, d.unit.text) for d in r.removals()] == [("absent_from_source", "הנכס נמכר אשתקד.")]


def test_a_bare_answer_goes_with_its_supporting_sentence_when_that_sentence_fails():
    ws = _ws(THRESHOLD)
    a = _answer("כן. שיעור הרווח לעלות 19% עובר את הסף [S1].\n\nהסף שנקבע הוא 15% [S1].")
    r = verify_answer(_judge(lambda text: "supported"), a, ws, "האם שיעור הרווח עומד בסף?", [])
    applied = r.apply(a)
    assert applied.answer_markdown.startswith("הסף שנקבע הוא 15% [S1].")
    assert "כן" not in applied.answer_markdown.split("\n")[0]


# --- the edit ladder (round 8 U4: KTD4; R8, R9, R10, R12) ------------------------------------------------------------
# A sentence is never removed whole when only part of it fails: a missing citation is attached, a failing claim's span
# is cut (a server marker in its place), a unit that cannot be cut is rewritten (the repair rounds), and only a unit
# nothing of which stands is removed — as the logged last resort.

AREA_SOURCE = "תא שטח 1: השטח העיקרי הוא 1,200 מ״ר. הנכס פנוי."
AREA = "השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר"
MARKER_1 = "«[1: חלק שלא אומת הושמט]»"
MARKER_2 = "«[2: חלק שלא אומת הושמט]»"


def _claims_judge(verdicts) -> ScriptedProvider:
    """A judge that answers each unit by ``verdicts(text)``: None (supported), or the claims of a mixed unit as
    [(span, verdict, extra)], the unit's verdict ``partial`` with defect ``part`` (rule 16)."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        out = []
        for m in re.finditer(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
            claims = verdicts(m.group(2))
            if claims is None:
                out.append({"index": int(m.group(1)), "verdict": "supported", "reason": "בדיקה"})
            else:
                out.append({"index": int(m.group(1)), "verdict": "partial", "reason": "חלקי", "defect": "part",
                            "claims": [{"span": s, "verdict": v} | extra for s, v, *rest in claims
                                       for extra in [rest[0] if rest else {}]]})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _mixed_area(sentence: str, bad: str):
    return lambda text: [(AREA, "supported"), (bad, "unsupported")] if bad in text else None


def test_ae1_a_failing_clause_is_cut_to_a_marker_and_the_supported_claim_stays_with_its_plot_and_unit():
    bad = "שאושר בוועדה המחוזית"
    a = _answer(f"{AREA}, {bad} [S1].")
    r = verify_answer(_claims_judge(_mixed_area(a.answer_markdown, bad)), a, _ws(AREA_SOURCE), "?", [])
    # the cut is the server's: it neither fails the report nor starts a repair round
    assert r.ok and not r.removed_units() and not any(p.repairable for p in r.problems)
    assert r.repair_keys() == set() and r.problems_text() == ""  # a repair prompt would have nothing to say of it
    out = r.apply(a).answer_markdown
    assert out == f"{AREA}, {MARKER_1} [S1]."
    assert [(c.text, c.action) for c in r.claims[0]] == [(AREA, "kept"), (bad, "cut")]
    assert r.claims[0][1].marker == 1 and r.claims[0][0].marker == 0
    # the marker is a registered server span, and nothing else is
    edited = r.edit(a)
    assert [(s.kind, edited.doc.text[s.start:s.end]) for s in edited.doc.spans()] == [("marker", MARKER_1)]
    assert r.correctness() == "partial" and "אומת חלקית" not in out  # claims were cut, the rest is whole


def test_a_failing_span_that_holds_a_negation_is_cut_whole_and_the_supported_claim_is_intact():
    a = _answer("הנכס פנוי, והוא לא מושכר לטווח ארוך [S1].")
    judge = _claims_judge(lambda t: [("הנכס פנוי", "supported"), ("והוא לא מושכר לטווח ארוך", "unsupported")])
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert out == f"הנכס פנוי, {MARKER_1} [S1]."
    assert "מושכר" not in out  # no negated remainder is left to read as a different claim


def test_a_unit_with_only_failing_claims_is_removed_whole_as_the_last_resort_and_the_actions_say_so():
    a = _answer("הנכס פנוי [S1].\nהנכס מושכר, והשכירות גבוהה [S1].")
    judge = _claims_judge(lambda t: [("הנכס מושכר", "unsupported"), ("והשכירות גבוהה", "unsupported")]
                          if "מושכר" in t else None)
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    assert r.removed_units() == {1} and not r.ok  # a rewrite is still tried first (repairable), then it goes
    out = r.apply(a).answer_markdown
    assert "מושכר" not in out and "«" not in out
    assert out == "הנכס פנוי [S1].\n\n> " + verify.REMOVAL_SENTENCES["absent_from_source"]
    assert [c.action for c in r.claims[1]] == ["removed", "removed"] and r.claims[0][0].action == "kept"


def test_a_failing_claim_that_cannot_be_cut_alone_takes_the_unit_with_it():
    # the supported claim contains the failing one: a cut of the failing span would cut into the supported claim
    a = _answer("הנכס פנוי ומושכר לטווח ארוך [S1].")
    judge = _claims_judge(lambda t: [("הנכס פנוי ומושכר לטווח ארוך", "supported"), ("מושכר", "unsupported")])
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    assert r.removed_units() == {0} and not r.ok
    assert r.apply(a).answer_markdown == "לא הצלחתי לבסס תשובה על המקורות."
    # …and the rewrite round is told which claims to keep verbatim and which to fix
    text = r.problems_text()
    assert "לשמור כלשונם: «הנכס פנוי ומושכר לטווח ארוך»" in text and "לתקן: «מושכר»" in text


def test_a_claim_resting_on_the_cut_claim_goes_with_it_in_one_marker():
    sentence = "שטח הנכס הוא 1,000 מ\"ר, ולכן חורג מהסף, והנכס פנוי [S1]."

    def respond(text):
        return [("שטח הנכס הוא 1,000 מ\"ר", "contradicts"), ("ולכן חורג מהסף", "supported", {"depends_on": [
            "שטח הנכס הוא 1,000 מ\"ר"]}), ("והנכס פנוי", "supported")]

    a = _answer(sentence)
    r = verify_answer(_claims_judge(respond), a, _ws("שטח הנכס 1,200 מ\"ר. הסף 1,000 מ\"ר. הנכס פנוי."), "?", [])
    assert r.ok and not r.removed_units()
    assert r.apply(a).answer_markdown == f"{MARKER_1}, והנכס פנוי [S1]."
    assert [c.marker for c in r.claims[0]] == [1, 1, 0]


def test_markers_are_numbered_in_the_order_of_their_place_in_the_final_text():
    a = _answer("הנכס פנוי, והוא מושכר [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול הם 12 ₪ למ\"ר, והם כוללים מע\"מ [S1].")
    judge = _claims_judge(lambda t: [("הנכס פנוי", "supported"), ("והוא מושכר", "unsupported")] if "מושכר" in t
                          else [("דמי הניהול הם 12 ₪ למ\"ר", "supported"), ("והם כוללים מע\"מ", "unsupported")]
                          if "כוללים" in t else None)
    r = verify_answer(judge, a, _ws(SOURCE + " דמי הניהול הם 12 ₪ למ\"ר."), "?", [])
    out = r.apply(a).answer_markdown
    assert out.index(MARKER_1) < out.index(MARKER_2) and out.count("חלק שלא אומת הושמט") == 2
    assert [c.marker for u in (0, 2) for c in r.claims[u] if c.action == "cut"] == [1, 2]


def test_a_cut_does_not_remove_a_citation_inside_the_sentence():
    a = _answer("הנכס פנוי [S1], והוא מושכר [S2].")
    judge = _claims_judge(lambda t: [("הנכס פנוי", "supported"), ("והוא מושכר", "unsupported")])
    r = verify_answer(judge, a, _ws(SOURCE, SOURCE), "?", [])
    assert r.apply(a).answer_markdown == f"הנכס פנוי [S1], {MARKER_1} [S2]."


def test_a_span_inside_a_table_row_is_cut_only_within_one_cell():
    md = "| נכס | הערה |\n|---|---|\n| הגפן | פנוי ומושכר [S1] |"
    a = _answer(md)
    r = verify_answer(_claims_judge(lambda t: [("פנוי", "supported"), ("ומושכר", "unsupported")] if "הגפן" in t
                                    else None), a, _ws(SOURCE), "?", [])
    assert r.apply(a).answer_markdown == f"| נכס | הערה |\n|---|---|\n| הגפן | פנוי {MARKER_1} [S1] |"
    across = _answer("| נכס | הערה |\n|---|---|\n| הגפן | פנוי | מושכר [S1] |")
    r = verify_answer(_claims_judge(lambda t: [("הגפן", "supported"), ("פנוי | מושכר", "unsupported")] if "הגפן" in t
                                    else None), across, _ws(SOURCE), "?", [])
    assert r.removed_units() == {1}  # a span across cells would break the table: the row is rewritten, then removed


# --- tables and headings after edits (R12) --------------------------------------------------------------------------

PARTS_SOURCE = "טבלת רכיבים: רכיב א 100 ₪, רכיב ב 200 ₪, רכיב ג 50 ₪, סה״כ 350 ₪."
PARTS = ("| רכיב | סכום |\n|---|---|\n| רכיב א | 100 ₪ [S1] |\n| רכיב ב | 200 ₪ [S1] |\n| רכיב ג | 50 ₪ [S1] |\n"
         "| סה״כ | 350 ₪ [S1] |")


def test_a_removed_row_under_a_total_row_qualifies_the_total():
    a = _answer(PARTS)
    r = verify_answer(_judge(lambda t: "unsupported" if "רכיב ב" in t else "supported"), a, _ws(PARTS_SOURCE), "?", [])
    edited = r.edit(a)
    assert edited.doc.text == (
        "| רכיב | סכום |\n|---|---|\n| רכיב א | 100 ₪ [S1] |\n| רכיב ג | 50 ₪ [S1] |\n"
        "| סה״כ (הסכום כולל שורה שהוסרה) | 350 ₪ [S1] |\n\n> " + verify.REMOVAL_SENTENCES["absent_from_source"])
    assert [s.kind for s in edited.doc.spans()] == ["qualifier", "removal_note"]


PARTS_NOTED = ("| רכיב | סכום | הערה |\n|---|---|---|\n| רכיב א | 100 ₪ [S1] | — |\n| רכיב ב | 200 ₪ [S1] | הנכס מושכר |\n"
               "| רכיב ג | 50 ₪ [S1] | — |\n| סה״כ | 350 ₪ [S1] | — |")


def test_a_cut_inside_a_component_row_qualifies_the_total_too():
    """Round 20 U2 (KTD2): the cell that fails is the row's note, and the amount beside it stays; a cut of the amount, the row's only data
    cell, would leave the label alone, and the row goes whole (the next test)."""
    a = _answer(PARTS_NOTED)
    r = verify_answer(_claims_judge(lambda t: [("200 ₪", "supported"), ("הנכס מושכר", "unsupported")] if "רכיב ב" in t
                                    else None), a, _ws(PARTS_SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert f"| רכיב ב | 200 ₪ [S1] | {MARKER_1} |" in out and "| סה״כ (הסכום כולל שורה שחלק ממנה הושמט) | 350 ₪" in out


def test_a_component_row_whose_only_data_cell_fails_is_removed_whole_and_qualifies_the_total():
    """Round 20 U2 (KTD2, R6): with the amount cut, the row would be a label over a marker: no safe partial keep, so the row goes."""
    a = _answer(PARTS)
    r = verify_answer(_claims_judge(lambda t: [("רכיב ב", "supported"), ("200 ₪", "unsupported")] if "רכיב ב" in t
                                    else None), a, _ws(PARTS_SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert "רכיב ב" not in out and "| סה״כ (הסכום כולל שורה שהוסרה) | 350 ₪" in out and r.removed_units() == {2}


def test_a_total_row_is_left_alone_when_every_component_row_stands():
    a = _answer(PARTS)
    r = verify_answer(_judge(lambda t: "supported"), a, _ws(PARTS_SOURCE), "?", [])
    assert r.apply(a).answer_markdown == PARTS


def test_a_table_left_with_its_header_only_is_removed_with_its_header_and_the_heading_over_it():
    md = "הנכס פנוי [S1].\n\n## הרכיבים\n\n| רכיב | סכום |\n|---|---|\n| רכיב א | 100 ₪ [S1] |\n| רכיב ב | 200 ₪ [S1] |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "unsupported" if "רכיב" in t and "|" in t and "סכום" not in t else "supported"),
                      a, _ws(PARTS_SOURCE, AREA_SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert "|" not in out and "הרכיבים" not in out
    assert out == "הנכס פנוי [S1].\n\n> " + verify.REMOVAL_SENTENCES["absent_from_source"].rstrip(".") + " (2 פעמים)."


# --- a unit failed only for numbers no cited source states is split by the judge, not removed up front -------------------

DATE_CLAUSE = "שאושר ביום 1.1.2020"
AREA_AND_DATE = f"{AREA}, {DATE_CLAUSE} [S1]."


def _recording_judge(claims) -> tuple[ScriptedProvider, list[str]]:
    seen: list[str] = []
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": "partial", "reason": "חלקי", "defect": "part",
                              "claims": claims} for i in re.findall(r'<unit index="(\d+)"', input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p, seen


def test_an_unstated_number_is_named_to_the_judge_and_only_its_claim_is_cut():
    a = _answer(AREA_AND_DATE)
    judge, seen = _recording_judge([{"span": AREA, "verdict": "supported"}, {"span": DATE_CLAUSE, "verdict": "unsupported"}])
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    assert '<unstated unit="0">1.1.2020</unstated>' in seen[0] and "<unstated" in verify.JUDGE_POLICY
    assert r.ok and not r.removed_units()
    assert r.apply(a).answer_markdown == f"{AREA}, {MARKER_1} [S1]."


def test_the_claim_holding_an_unstated_number_fails_even_when_the_judge_calls_it_supported():
    a = _answer(AREA_AND_DATE)
    judge, _ = _recording_judge([{"span": AREA, "verdict": "supported"}, {"span": DATE_CLAUSE, "verdict": "supported"}])
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    ok, bad = r.claims[0]
    assert (ok.verdict, bad.verdict, bad.failure_kind(), bad.action) == ("supported", "unsupported", "absent_from_source", "cut")
    assert "1.1.2020" in r.problems[0].reason and "מספרים שאינם מופיעים במקורות המצוטטים" in r.problems[0].reason
    assert r.apply(a).answer_markdown == f"{AREA}, {MARKER_1} [S1]."


@pytest.mark.parametrize("claims", [
    [],  # the judge did not split the unit
    [{"span": AREA_AND_DATE.removesuffix(" [S1]."), "verdict": "supported"}],  # its only claim holds the number
    [{"span": AREA, "verdict": "supported"}, {"span": "שאושר", "verdict": "unsupported"}],  # no claim holds it
], ids=["no_claims", "only_claim", "none_holds_it"])
def test_a_unit_the_judge_does_not_split_around_the_unstated_number_fails_whole(claims):
    a = _answer(AREA_AND_DATE)
    judge, _ = _recording_judge(claims)
    r = verify_answer(judge, a, _ws(AREA_SOURCE), "?", [])
    (removal,) = r.removals()
    assert removal.check == "unstated_number" and removal.failure_kind == "absent_from_source"
    assert r.removed_units() == {0} and [c.action for c in r.claims[0]] == ["removed"]


def test_a_unit_that_is_only_the_unstated_number_is_removed_whole_without_a_judge_call():
    usage: list = []
    a = _answer("גובה הבניין 99 מטר [S1].")
    r = verify_answer(_judge(lambda t: "supported"), a, _ws(AREA_SOURCE), "?", usage)
    assert usage == [] and r.removed_units() == {0} and r.removals()[0].check == "unstated_number"


# --- a model-written period or basis of a registered value backs no claim (round 12 U9, AE5, R6) ------------------

_RENT_QUOTE = "שכר דירה 5,000 ₪"


@pytest.mark.parametrize("provenance,blocking", [({"period": "model_asserted"}, True), ({"period": "source"}, False)])
def test_a_period_claim_on_a_registered_value_is_blocking_only_when_the_model_alone_gave_the_period(provenance, blocking):
    ws = _registered("5,000", _RENT_QUOTE, period="month", provenance=provenance)
    problems = verify.deterministic(split_units("שכר הדירה הוא 5,000 ₪ לחודש [V1]."), ws, "?")
    assert [(p.check, p.failure_kind) for p in problems if p.check == "meaning"] == (
        [("meaning", "wrong_unit")] if blocking else [])


def test_a_model_written_period_costs_the_claim_its_qualifier_and_not_its_number():
    """Round 12 U10 (R7, AE5): the blocking meaning problem above stays the trigger; the sources back 5,000 ₪, so the
    ladder removes the unsupported "לחודש" instead of the claim."""
    ws = _registered("5,000", _RENT_QUOTE, period="month", provenance={"period": "model_asserted"})
    a = _answer("שכר הדירה הוא 5,000 ₪ לחודש [V1].")
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    assert r.removed_units() == set() and r.counts()["edited"] == 1
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000 ₪ [V1].")


# --- a model-asserted VAT backs nothing, in a calculation or in a measurement (round 12 U10 follow-ups, R6, R8) ------

@pytest.mark.parametrize("provenance,expected", [({"vat": "model_asserted"}, "unknown"), ({"vat": "not_stated"}, "unknown"),
                                                 ({"vat": "source"}, "included")])
def test_the_operand_of_a_value_carries_its_vat_only_when_the_source_gave_it(provenance, expected):
    v = _registered("2,450,000", _VALUE_QUOTE, vat="included", provenance=provenance).values["V1"]
    assert v.operand().vat == expected


def _two_values(provenance: str):
    from tests.unit.test_verify_computed import _compute, _value

    ws = Workspace(ctx=None)
    _value(ws, "12,450,000", "סה״כ הכנסות", "income", "income")
    _value(ws, "10,400,000", "סה״כ עלויות", "cost", "cost")
    for v in ws.values.values():
        v.provenance["vat"] = provenance
    return ws, _compute(ws, "V1 - V2", "הרווח", "בדיקה")


def test_a_calculation_over_values_with_a_model_asserted_vat_has_no_vat_basis():
    ws, c = _two_values("model_asserted")
    assert c.vat is None
    assert verify._computation_vat(split_units("הרווח 2,050,000 ₪ ללא מע\"מ [C1].")[0], ws, c) == set()
    (problem,) = [p for p in verify.deterministic(split_units("הרווח הוא 2,050,000 ₪ ללא מע\"מ [C1]."), ws, "?")
                  if p.check == "vat"]
    assert problem.failure_kind == "wrong_unit"


def test_a_calculation_over_values_with_a_source_vat_keeps_it():
    ws, c = _two_values("source")
    assert c.vat == "excluded"
    assert [p for p in verify.deterministic(split_units("הרווח הוא 2,050,000 ₪ ללא מע\"מ [C1].")
                                            , ws, "?") if p.check == "vat"] == []


def test_the_vat_of_an_input_value_is_read_only_when_the_source_gave_it():
    ws, c = _two_values("source")
    unit = split_units("ההכנסות היו 12,450,000 ₪ ללא מע\"מ [C1].")[0]
    assert ("12450000", "excluded") in verify._computation_vat(unit, ws, c)
    ws.values["V1"].provenance["vat"] = "model_asserted"
    assert ("12450000", "excluded") not in verify._computation_vat(unit, ws, c)


def _measured(ws: Workspace, quote: str, vat: str = "excluded", **extra):
    from types import SimpleNamespace

    from app.chat.tools import Measurement

    row = SimpleNamespace(value_text="58 ₪", quote=quote, vat=vat, metric="דמ\"ש", metric_kind="rent_per_area",
                          unit="ILS_per_sqm", period="month", area_basis=None, subject=None,
                          value_role="appraiser_determination", status="auto_validated", section=None,
                          block_index=1, table_index=None, **extra)
    ws.measurements["M1"] = Measurement("M1", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "דוח", row)


def test_a_measurements_stored_vat_backs_a_claim_only_when_its_own_words_state_it():
    ws = _ws(PAIR)
    _measured(ws, "דמי השכירות למ\"ר נקבעו ל-58 ₪ לחודש.")  # the stored VAT is the model's: no words state it
    (problem,) = verify.deterministic(split_units("דמי השכירות הם 58 ₪ ללא מע\"מ [M1]."), ws, "?")
    assert problem.check == "vat"
    ws = _ws(PAIR)
    _measured(ws, "דמי השכירות למ\"ר נקבעו ל-58 ₪ לחודש.", caption="דמי שכירות, ללא מע\"מ", header="")
    assert verify.deterministic(split_units("דמי השכירות הם 58 ₪ ללא מע\"מ [M1]."), ws, "?") == []


# --- the server's own facts about a cited source's unit reach the judge and the date check (round 13 U9, KTD10, R13, AE6) ------

VALUATION_LINE = "למועד הקובע 01/03/2023"
_UNIT_PASSAGE = "סיכום השומה: גישת ההשוואה העניקה שווי של 1,950,000 ₪ לנכס."  # the passage states no valuation date


def _valued(date_line: str | None = VALUATION_LINE, link: str | None = None) -> Workspace:
    """A workspace whose S1 is a passage of an appraisal unit the server dated (``Source.date_line``), without the date in
    its text."""
    ws = _ws(_UNIT_PASSAGE)
    ws.sources["S1"].date_line = date_line
    ws.sources["S1"].link = link
    return ws


def _recording_supported() -> tuple[ScriptedProvider, list[str]]:
    seen: list[str] = []
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": "supported", "reason": "בדיקה"}
                             for i in re.findall(r'<unit index="(\d+)"', input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p, seen


@pytest.mark.parametrize("written", ["01/03/2023", "1.3.2023", "01.03.2023"])
def test_a_date_equal_to_the_cited_sources_stated_unit_valuation_date_is_backed(written):
    ws = _valued()
    assert _numbers(f"הערכת השווי בשומה נעשתה למועד הקובע {written} [S1].", ws) == []


@pytest.mark.parametrize("other", ["02/03/2023", "01/04/2023", "01/03/2022", "03/01/2023"])
def test_another_date_than_the_units_valuation_date_is_still_unstated(other):
    ws = _valued()
    (problem,) = _numbers(f"הערכת השווי בשומה נעשתה למועד הקובע {other} [S1].", ws)
    assert problem.unstated, other


@pytest.mark.parametrize("line", [None, "מועד לא ידוע",
                                  "מועד לא ידוע (בחלק הזה של המסמך כמה מועדים קובעים שונים: 01/03/2023, 05/06/2024)"])
def test_a_unit_with_no_stated_valuation_date_backs_no_date(line):
    ws = _valued(line)
    assert _numbers("הערכת השווי בשומה נעשתה למועד הקובע 01/03/2023 [S1].", ws)


def test_the_valuation_date_of_a_source_the_unit_does_not_cite_backs_nothing():
    ws = _valued()
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="מסמך אחר", section="סיכום",
                  location="סעיף 2", kind="context", text="גישת ההשוואה העניקה שווי של 1,950,000 ₪.")
    assert _numbers("הערכת השווי בשומה נעשתה למועד הקובע 01/03/2023 [S2].", ws)


def test_the_date_lines_digits_never_back_another_number_of_the_unit():
    ws = _valued()
    problems = _numbers("הנכס בן 3 חדרים ו-1 חניות, והשומה למועד הקובע 01/03/2023 [S1].", ws)
    assert problems and {"3", "1"} <= set(problems[0].unstated)
    assert "01/03/2023" not in verify._source_parts(ws, "S1", dated=False)[0]  # the number check reads the record without it


def test_the_judge_reads_the_units_valuation_date_as_a_fact_the_server_holds():
    ws = _valued()
    head, body, kind = verify._source_parts(ws, "S1")
    assert VALUATION_LINE in head and verify.SERVER_HELD in head and kind == "context"
    assert VALUATION_LINE not in body  # the passage's own text is untouched


def test_a_sentence_naming_the_units_valuation_date_is_kept_and_the_judge_input_holds_the_server_date_line():
    ws = _valued()
    judge, seen = _recording_supported()
    a = _answer("הערכת השווי בשומה נעשתה למועד הקובע 01.03.2023 [S1].")
    r = verify_answer(judge, a, ws, "?", [])
    assert r.ok and not r.removed_units(), [p.reason for p in r.problems]
    assert VALUATION_LINE in seen[0] and verify.SERVER_HELD in seen[0]
    assert r.apply(a).answer_markdown == a.answer_markdown


def test_the_link_phrase_is_rendered_as_server_held_text_too():
    ws = _valued(link="שומה של אותו נכס: חלקה 8401/40 משותפת")
    head = verify._source_parts(ws, "S1")[0]
    assert "שומה של אותו נכס: חלקה 8401/40 משותפת" in head and head.count(verify.SERVER_HELD) == 2
    assert "שומה של אותו נכס" not in verify._source_parts(ws, "S1", dated=False)[0]


def test_the_judge_policy_names_the_server_held_line_and_the_derived_computation():
    assert verify.SERVER_HELD in verify.JUDGE_POLICY and "derived" in verify.JUDGE_POLICY


# --- the judge contract is version 14: a verdict of an older contract is not reused (round 13 U9, U11, KTD19; round 15 U7; round 16 U1; round 18 U3) --

def test_a_verdict_cached_under_judge_contract_13_misses_under_14(monkeypatch):
    assert verify.JUDGE_VERSION == 21  # 15: round 18 U4 (the marks of a table part's attribution, rule 32); 16: round 18 U5 (the condition outcome of an item in the requirements call, `conditions`); 17: round 18 U6 (the candidates of the requirements call, `candidates`); 18: round 19 U2 (the `<ownership>` fact line of a unit whose evidence's owner is unknown, rule 34); 19: round 19 U2 (that line also tells of a source of another context cited beside the asked one's); 20: round 20 U2 (rule 35: the claims of a table row that holds a defect are its cells); 21: round 20 U3 (the source header names a unit's other names; the ownership line carries the scope and same-property facts)
    ws, a, cache = _valued(), _answer("הנכס פנוי [S1]."), verify.VerdictCache()
    judge, seen = _recording_supported()
    monkeypatch.setattr(verify, "JUDGE_VERSION", 13)
    verify_answer(judge, a, ws, "?", [], cache=cache)
    assert verify_answer(judge, a, ws, "?", [], cache=cache).reused == 1
    monkeypatch.setattr(verify, "JUDGE_VERSION", 14)
    assert verify_answer(judge, a, ws, "?", [], cache=cache).reused == 0
    assert verify_answer(judge, a, ws, "?", [], cache=cache).reused == 1


# --- back-references and the final check's rule 31 (round 13 U11, KTD13 rule 3) -------------------------------------------

@pytest.mark.parametrize("text", [
    "שני המקדמים לעיל חלים על כל החלקה.", "כאמור, השטח אינו משתנה.", "וכאמור השטח אינו משתנה.", "הנתון האמור נכון.",
    "לפי האמור בסעיף 3.", "שלושת השיעורים נקבעו כך.", "המקדמים האלה נקבעו כך.", "הערכים הנ\"ל נקבעו כך.",
    "הערכים הנ״ל נקבעו כך.", "השיעורים הללו נקבעו כך.", "זכויות הבנייה לעיל.",
    # the indefinite demonstrative form (round 13)
    "שיעורים אלה נכללו בחישוב.", "ומקדמים אלו נקבעו כך.", "ערכים הללו נקבעו כך.", "ההפחתות אלה נקבעו כך.",
    "התאמות אלה חלות על החלקה.", "נתונים אלה נלקחו מהטבלה.",
])
def test_a_sentence_that_refers_back_is_recognised(text):
    assert verify.has_back_reference(text)


@pytest.mark.parametrize("text", [
    "מקדם א הוא 0.8.", "השטח העיקרי הוא 1,200 מ״ר.", "המקדמים נקבעו בשומה.", "שניים מהמקדמים נקבעו בשומה.",
    "הוא אמר שיעלה.", "תיאורטית השווי גבוה.", "", "שיעורים שונים נקבעו בשומה.", "הפחתות נקבעו בשומה.",
])
def test_a_sentence_that_refers_to_nothing_before_it_is_not(text):
    assert not verify.has_back_reference(text)


def test_the_judge_policy_names_the_preceding_text_and_the_removed_predecessors():
    assert "<preceding" in verify.JUDGE_POLICY and "removed_before" in verify.JUDGE_POLICY


@pytest.mark.parametrize("line_date, claimed", [
    ("15/07/2024", "15.7.2024"), ("15/07/2024", "15.07.2024"), ("15.07.2024", "15/7/2024"), ("5/7/2024", "05.07.2024"),
    ("05/07/2024", "5.7.2024"),
])
def test_a_claim_date_matches_the_units_valuation_date_in_any_digit_form(line_date, claimed):
    """One-digit and two-digit days and months are the same date, on either side (round 13 U9, KTD10)."""
    ws = _valued(f"למועד הקובע {line_date}")
    assert _numbers(f"הערכת השווי בשומה נעשתה למועד הקובע {claimed} [S1].", ws) == []
    assert _numbers("הערכת השווי בשומה נעשתה למועד הקובע 16.7.2024 [S1].", ws)


def test_a_professional_source_or_one_with_no_place_is_never_stamped_with_a_valuation_date():
    from app.chat.tools import stamp_valuation

    ws = _ws("תקן סינתטי: על השמאי לציין את המועד הקובע.")
    src = ws.sources["S1"]
    src.source_kind, src.block_start = "standard", 3
    assert stamp_valuation(ws, None, src).date_line is None  # no connection is needed to decide it is not an appraisal
    src.source_kind, src.block_start = "appraisal", None
    assert stamp_valuation(ws, None, src).date_line is None


# --- a percentage of a fraction the source states (round 13) -------------------------------------------------

WEIGHTS = "גישה א | 300,000 | 0.80 | 240,000\nגישה ב | 200,000 | 0.20 | 40,000"


@pytest.mark.parametrize("shown,fraction", [("80%", "0.80"), ("7.25%", "0.0725"), ("100%", "1.00"), ("20%", "0.20")])
def test_a_percentage_of_a_fraction_the_cited_source_states_is_not_an_unstated_number(shown, fraction):
    ws = _ws(f"גישה א | {fraction}")
    assert _numbers(f"משקל של {shown} [S1].", ws) == []


def test_the_weights_of_a_table_written_as_percentages_are_not_unstated():
    ws = _ws(WEIGHTS)
    assert _numbers("גישה א קיבלה משקל של **80%** וגישה ב משקל של **20%** [S1].", ws) == []


def test_a_percentage_whose_fraction_the_source_does_not_state_is_still_unstated():
    (problem,) = _numbers("משקל של 80% [S1].", _ws("גישה א | 0.75"))
    assert "80" in problem.unstated


def test_a_number_without_a_percent_sign_is_not_converted_to_a_fraction():
    (problem,) = _numbers("משקל של 80 [S1].", _ws("גישה א | 0.80"))
    assert "80" in problem.unstated


def test_a_fraction_in_the_answer_is_not_read_as_a_percentage_of_the_source():
    (problem,) = _numbers("משקל של 0.8 [S1].", _ws("גישה א | 80%"))
    assert "0.8" in problem.unstated


def test_the_same_digits_written_without_a_percent_sign_stay_unstated():
    (problem,) = _numbers("משקל של 80% ושטח של 80 מ\"ר [S1].", _ws("גישה א | 0.80"))
    assert "80" in problem.unstated


def test_a_percentage_of_a_fraction_any_source_of_the_turn_states_backs_a_unit_that_cites_nothing():
    ws = _ws("גישה א | 0.8")
    assert _numbers("משקל של 80% בשומה.", ws) == []
    assert _numbers("משקל של 75% בשומה.", ws)


# --- number forms, VAT attachment and calculation dates (round 18 U2, KTD3, R8) ----------------------------------------------

def test_a_source_listing_averaging_years_joined_by_commas_backs_each_year():
    source = "הממוצע חושב על השנים 2018,2019,2021 בלבד."
    assert _deterministic("הממוצע נלקח לפי השנים 2018, 2019 ו-2021 [S1].", source) == []
    (problem,) = _deterministic("הממוצע נלקח לפי השנים 2018, 2019 ו-2020 [S1].", source)
    assert problem.check == "unstated_number" and "2020" in problem.unstated and "2019" not in problem.unstated


@pytest.mark.parametrize("source, claimed", [
    ("הערך הנקוב הוא 850 אלף ₪.", "850,000"), ("הערך הנקוב הוא 1.5 מיליון ₪.", "1,500,000"),
    ("הערך הנקוב הוא 122.7K ₪.", "122,700"), ("הערך הנקוב הוא 850 א׳ ₪.", "850,000"),
    ("הערך הנקוב הוא ₪ 2.4M.", "2,400,000"),
])
def test_a_full_digit_amount_whose_source_writes_it_with_a_scale_word_is_not_an_unstated_number(source, claimed):
    assert _deterministic(f"הערך הנקוב הוא {claimed} ₪ [S1].", source) == []


def test_a_full_digit_amount_that_is_not_the_scaled_source_amount_is_still_unstated():
    (problem,) = _deterministic("הערך הנקוב הוא 850,001 ₪ [S1].", "הערך הנקוב הוא 850 אלף ₪.")
    assert "850001" in problem.unstated


@pytest.mark.parametrize("source, claim", [
    ("שווי הנכס 122,700,000 ₪ כולל מע\"מ.", "שווי הנכס 122.7 מיליון ₪ כולל מע\"מ"),
    ("שווי הנכס 122.7 מיליון ₪ כולל מע\"מ.", "שווי הנכס 122,700,000 ₪ כולל מע\"מ"),
    ("שווי הנכס 850 אלף ₪ לא כולל מע\"מ.", "שווי הנכס 850,000 ₪, ללא מע\"מ"),
])
def test_vat_is_compared_by_value_across_the_written_forms_of_an_amount(source, claim):
    unit = split_units(f"{claim} [S1].")[0]
    assert verify._wrong_vat(unit, _ws(source)) == []
    assert verify._vat_problems(unit, _ws(source)) == []
    assert _deterministic(f"{claim} [S1].", source) == []


def test_vat_of_the_other_status_on_the_same_amount_in_another_form_is_still_wrong():
    # protection: the source excludes the VAT of 122,700,000; "122.7 מיליון ... כולל מע״מ" is the opposite status
    source = "שווי הנכס 122,700,000 ₪ לא כולל מע\"מ."
    unit = split_units("שווי הנכס 122.7 מיליון ₪ כולל מע\"מ [S1].")[0]
    assert verify._wrong_vat(unit, _ws(source))
    (problem,) = [p for p in _deterministic("שווי הנכס 122.7 מיליון ₪ כולל מע\"מ [S1].", source) if p.check == "vat"]
    assert "122.7" in problem.reason


def test_a_vat_phrase_after_a_property_name_that_holds_a_number_is_not_attached_to_that_number():
    pairs, general = verify.vat_attachments("דמי השכירות 58 ₪ לחודש בבניין מגדל הים 7 ללא מע\"מ")
    assert pairs == {(f, "excluded") for f in verify.numbers_in("58")} and not general
    source = "דמי השכירות 58 ₪ לחודש, ללא מע\"מ. הנכס נמצא בבניין מגדל הים 7."
    assert _deterministic("דמי השכירות 58 ₪ לחודש בבניין מגדל הים 7 ללא מע\"מ [S1].", source) == []
    # a plain number after a word that is no identifier is still an amount that carries the VAT
    pairs, _ = verify.vat_attachments("דמי השכירות 58 ללא מע\"מ")
    assert pairs == {("58", "excluded")}


@pytest.mark.parametrize("text", [
    'שכר דירה 4,500 ש"ח לא כולל מע"מ',
    'שכר דירה 4,500 ש״ח לא כולל מע"מ',
    "שכר דירה 4500 שקלים ללא מע\"מ",
    "שכר בית 3000 שקל ללא מע\"מ",
    "דמי שימוש במבנה 800 לחודש ללא מע\"מ",
    "שכר קומה 1200 בשנה ללא מע\"מ",
    'שכר דירה בסך ש"ח 4,500 ללא מע"מ',
    "שכר דירה 4,500 ללא מע\"מ",
])
def test_a_short_amount_after_a_generic_property_noun_still_carries_its_vat_status(text):
    ((forms, polarity, _, _),) = list(verify._vat_phrases(text, True))
    assert forms is not None and polarity in ("excluded", "included"), text
    pairs, general = verify.vat_attachments(text)
    assert pairs and not general


@pytest.mark.parametrize("text", [
    "דמי השכירות 58 ₪ לחודש בבניין מגדל הים 7 ללא מע\"מ",
    "הנכס בגוש 1234 חלקה 56 ללא מע\"מ",
    "הנכס ברחוב הצאלון 7 ללא מע\"מ",
    "הנכס דירה 12 בקומה 3 ללא מע\"מ",
])
def test_a_number_in_the_name_of_a_property_or_a_plot_is_still_not_vat_bearing(text):
    pairs, _ = verify.vat_attachments(text)
    assert not any(f in ("7", "1234", "56", "12", "3") for f, _ in pairs), text


@pytest.mark.parametrize("text, house", [
    ("השכירות 4,500 ₪ בהרצל 12 ללא מע\"מ", "12"),
    ("השכירות 4,500 ₪ ליד בניין 7 לא כולל מע\"מ", "7"),
    ("השכירות 4,500 ₪ בדירה 305 ללא מע\"מ", "305"),
])
def test_a_bare_whole_number_of_up_to_three_digits_is_not_vat_bearing(text, house):
    """Round 19 U1 (KTD3): a house number has no currency, scale, percent, separator or unit near it, so the VAT phrase after it
    belongs to the amount before it."""
    pairs, _ = verify.vat_attachments(text)
    assert pairs == {(f, "excluded") for f in verify.numbers_in("4,500")}, text
    assert not any(f == house for f, _ in pairs)


@pytest.mark.parametrize("text, amount", [
    ("שכר דירה 800 ₪ ללא מע\"מ", "800"),
    ("דמי שימוש במבנה 800 לחודש ללא מע\"מ", "800"),
    ("עלות הפיתוח 120 אלף ללא מע\"מ", "120"),
    ("שכר דירה ₪ 950 ללא מע\"מ", "950"),
    ("תשלום 100 שקלים לא כולל מע\"מ", "100"),
    ("דמי ניהול 4,500 ללא מע\"מ", "4500"),
])
def test_a_small_amount_with_a_currency_scale_period_or_separator_is_still_vat_bearing(text, amount):
    pairs, _ = verify.vat_attachments(text)
    assert any(f == amount and p == "excluded" for f, p in pairs), text


def test_the_vat_of_a_rent_written_with_a_shekel_word_after_a_property_noun_is_checked_against_the_source():
    source = 'שכר דירה 4,500 ש"ח, לא כולל מע"מ.'
    assert _deterministic('שכר דירה 4,500 ש"ח לא כולל מע"מ [S1].', source) == []
    (problem,) = [p for p in _deterministic('שכר דירה 4,500 ש"ח כולל מע"מ [S1].', source) if p.check == "vat"]
    assert "4,500" in problem.reason or "4500" in problem.reason


def test_a_vat_phrase_inside_the_parentheses_of_an_amount_belongs_to_that_amount():
    pairs, general = verify.vat_attachments("השווי 3,100,000 ₪ (מתוכם 2,450,000 ₪ ללא מע\"מ)")
    assert pairs == {(f, "excluded") for f in verify.numbers_in("2,450,000")} and not general
    source = "השווי 3,100,000 ₪; מתוכם 2,450,000 ₪ ללא מע\"מ."
    unit = split_units("השווי 3,100,000 ₪ (מתוכם 2,450,000 ₪ ללא מע\"מ) [S1].")[0]
    assert verify._wrong_vat(unit, _ws(source)) == []
    # a VAT phrase after the parentheses still belongs to the amount before them
    pairs, _ = verify.vat_attachments("9,500 ₪ (שטח 120 מ\"ר), לא כולל מע\"מ")
    assert pairs == {(f, "excluded") for f in verify.numbers_in("9,500")}


def test_an_unstated_number_repair_names_the_number_and_asks_to_add_a_citation_not_to_replace_one():
    (problem,) = _deterministic("הערך הנקוב הוא 850,001 ₪ [S1].", "הערך הנקוב הוא 850 אלף ₪.")
    note = problem.repair_note()
    assert "850,001" in note and "הוסף לטענה ציטוט" in note
    assert "החלף" not in note and "במקום" not in note


def test_vat_of_an_amount_shown_in_millions_is_backed_by_the_full_amount_rounded_to_the_shown_precision():
    source = "שווי הנכס 122,749,000 ₪ כולל מע\"מ."
    unit = split_units("שווי הנכס 122.7 מיליון ₪ כולל מע\"מ [S1].")[0]
    assert verify._wrong_vat(unit, _ws(source)) == []
    other = split_units("שווי הנכס 122.8 מיליון ₪ כולל מע\"מ [S1].")[0]
    assert verify._wrong_vat(other, _ws(source))


# --- a bold label followed by a statement is judged as the statement (round 18 U3, KTD3, R8) -----------------------------------

LOCATION_SOURCE = "דוח: סעיף 5, פסקה 3 עוסק בזכויות הבנייה."


@pytest.mark.parametrize("raw", ["**מיקום:** סעיף 5, פסקה 3 [S1]", "**מיקום**: סעיף 5, פסקה 3 [S1]",
                                 "- **מיקום:** סעיף 5, פסקה 3 [S1]"])
def test_a_bold_label_followed_by_a_statement_is_shown_to_the_judge_as_the_statement_and_is_not_removed_as_unclassified(raw):
    seen: list[str] = []

    def rule(text: str) -> str:
        seen.append(text)
        return "navigation" if text.lstrip("- ").startswith("**") else "supported"  # a bold label alone reads as navigation

    report = verify_answer(_judge(rule), _answer(raw), _ws(LOCATION_SOURCE), "?", [])
    assert report.ok and report.removals() == [], [(p.check, p.reason) for p in report.problems]
    assert [t.lstrip("- ") for t in seen] == ["מיקום: סעיף 5, פסקה 3"]  # the label is a word of the statement, not markup


def test_a_bare_bold_label_and_a_wholly_bold_statement_are_shown_as_they_were_written():
    for raw in ("**הנתונים:**", "**השווי: 9,500 ₪**"):
        seen: list[str] = []
        verify_answer(_judge(lambda text, seen=seen: seen.append(text) or "supported"), _answer(raw), _ws(SOURCE), "?", [])
        assert seen == [raw], raw


def test_a_claim_span_the_judge_took_from_a_bold_labelled_statement_stays_a_substring_of_the_units_own_text():
    (u,) = split_units("**שווי:** שטח 1,200 מ\"ר ודמי שכירות 55 ₪ [S1]")
    shown = verify.label_as_statement(u.text)
    assert shown == "שווי: שטח 1,200 מ\"ר ודמי שכירות 55 ₪"
    for span, want in ((shown, u.text), ("שווי: שטח 1,200 מ\"ר", "**שווי:** שטח 1,200 מ\"ר"),
                       ("שטח 1,200 מ\"ר", "שטח 1,200 מ\"ר"), ("ודמי שכירות 55 ₪", "ודמי שכירות 55 ₪")):
        assert verify._claim_span(u, span) == want and verify._claim_span(u, span) in u.text, span
    assert verify._claim_span(u, "שווי: ") is None  # a span that ends inside the label is no statement
    assert verify._claim_span(u, "שטח אחר") is None


# --- three-state ownership: an unknown owner is never removed deterministically, the judge reads a neutral fact line (round 19 U2) ----

def _owner_file(version: str):
    """An appraisal of the invented «מתחם גבעת האלה» (unit 1: street and parcel), calculation pages that a restarted numbering and an
    opening subject line cut and that state nothing (unit 2), and another invented property with its own parcel (unit 3)."""
    from types import SimpleNamespace

    from app.chat import contexts, units

    def ident(written: str) -> dict:
        found = contexts.identifiers(written)
        return {"addresses": [list(a) for a in found.addresses], "parcels": [list(p) for p in found.parcels],
                "shown": list(found.shown)}

    def unit(no, kind, title, identifiers, first, last, pages, signals=("subject_line",)):
        return units.Unit(no, kind, title, (), identifiers, pages[0], pages[1], first, last, "certain",
                          {"boundary": {"block": first, "signals": list(signals)}})

    parts = [unit(1, "appraisal", "מתחם גבעת האלה", ident("רחוב הדס 5\nגוש 40100 חלקה 12"), 0, 7, (1, 2), ()),
             unit(2, "document", None, {}, 8, 12, (3, 4)),
             unit(3, "appraisal", "פרויקט נחל הלוטם", ident("רחוב הלוטם 9\nגוש 40100 חלקה 31"), 13, 20, (5, 6))]
    first = {n: SimpleNamespace(block_index=n, page=p, bbox=[50.0, 40.0, 500.0, 52.0]) for n, p in ((8, 3), (13, 5))}
    return contexts.from_units(parts, first, [4, 9, 14], version, "r1")


def _owner_ws(text: str, number: int, question: str = "מה ההכנסה נטו השנתית של מתחם גבעת האלה?"):
    version = uuid.uuid4()
    ws = _ws()
    ws.contexts[str(version)] = _owner_file(str(version))
    ws.user_messages = [{"text": question, "current": True}]
    src = ws.add_source(document_id=uuid.uuid4(), version_id=version, title="שומה", section=None, location="עמוד 3",
                        kind="context", text=text)
    src.contexts = [number]
    return ws, src.sid


OWNER_ANSWER = "ההכנסה נטו השנתית של מתחם גבעת האלה היא 610,000 ₪ [S1]."


@pytest.fixture
def owner_enforced(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)


def test_an_unknown_owner_is_not_removed_and_the_judge_reads_a_neutral_fact_line_in_the_call_that_judges_the_unit(owner_enforced):
    ws, sid = _owner_ws("הכנסה נטו שנתית 610,000 ₪.", 2)
    judge, seen = _recording_supported()
    report = verify_answer(judge, _answer(OWNER_ANSWER), ws, ws.user_messages[0]["text"], [])
    assert sid == "S1" and not [p for p in report.problems if p.check == "context"]  # nothing is removed deterministically
    assert report.verdicts == {0: "supported"} and len(seen) == 1  # the one call that judges the unit; no call was added
    (rendered,) = seen
    assert re.search(r'<ownership unit="0">[^<]*הקשר 2[^<]*הקשר 1[^<]*</ownership>', rendered), rendered
    assert rendered.index("<ownership") < rendered.index('<unit index="0"')  # beside the unit, never inside its text
    assert "ראיה שמכריעה" in rendered


def test_an_owner_the_text_settles_adds_no_fact_line_and_a_different_owner_is_removed_before_the_judge(owner_enforced):
    same, _ = _owner_ws("חישוב ההיוון לגוש 40100 חלקה 12: הכנסה נטו שנתית 610,000 ₪.", 2)
    judge, seen = _recording_supported()
    verify_answer(judge, _answer(OWNER_ANSWER), same, same.user_messages[0]["text"], [])
    assert len(seen) == 1 and "<ownership" not in seen[0]
    other, _ = _owner_ws("הכנסה נטו שנתית 610,000 ₪.", 3)  # unit 3 states a parcel and a name of its own
    judge, seen = _recording_supported()
    report = verify_answer(judge, _answer(OWNER_ANSWER), other, other.user_messages[0]["text"], [])
    assert [p.check for p in report.problems] == ["context"] and report.problems[0].failure_kind == "wrong_subject"
    assert not seen or "<ownership" not in seen[0]


def test_the_verdict_cache_key_changes_with_the_ownership_fact_and_the_fact_is_recomputed_each_round(owner_enforced):
    ws, _ = _owner_ws("הכנסה נטו שנתית 610,000 ₪.", 2)
    (unit,) = split_units(OWNER_ANSWER)
    q = ws.user_messages[0]["text"]
    plain = verify.unit_key(unit, ws, q, "", {})
    verify.deterministic([unit], ws, q)
    assert ws.ownership_facts[verify._fact_key(unit)]
    assert verify.unit_key(unit, ws, q, "", {}) != plain  # a verdict given without the line is not reused with it
    ws.sources["S1"].text = "חישוב ההיוון לגוש 40100 חלקה 12: הכנסה נטו שנתית 610,000 ₪."  # the text now settles it
    verify.deterministic([unit], ws, q)
    assert verify._fact_key(unit) not in ws.ownership_facts


def test_permissions_are_not_touched_a_citation_of_a_source_the_turn_does_not_hold_is_still_an_invalid_citation(owner_enforced):
    """The context check reads only the evidence the turn holds (the workspace is built from the documents the user may see): a citation
    of anything else fails as before, whatever the ownership of the sources that are held."""
    ws, _ = _owner_ws("חישוב ההיוון לגוש 40100 חלקה 12: הכנסה נטו שנתית 610,000 ₪.", 2)
    (unit,) = split_units("ההכנסה נטו השנתית של מתחם גבעת האלה היא 610,000 ₪ [S1] [S9].")
    (problem,) = verify.deterministic([unit], ws, ws.user_messages[0]["text"])
    assert problem.check == "unknown_id" and problem.failure_kind == "invalid_citation"
    assert not ws.ownership_facts  # no ownership was decided for a unit that cites what is not held


# --- round 20 U2: cells of a table row, the reuse context, the mark inside the last cell (KTD2; R5-R7) ---------------------------

@pytest.mark.parametrize("row, cells", [
    ("| a | b | c |", ["a", "b", "c"]),
    ("| a |  | c |", ["a", "", "c"]),
    ("| a | b", ["a", "b"]),
    ("a | b |", ["a", "b"]),
    ("| a [S1] | 9,500 ₪ |", ["a [S1]", "9,500 ₪"]),
])
def test_the_cells_of_a_table_row_are_its_segments_between_the_bars(row, cells):
    assert [row[a:b].strip() for a, b in verify.cell_bounds(row)] == cells


@pytest.mark.parametrize("line, names", [
    ("הטבלה מציגה את הנתונים העיקריים של הנכס [S1].", False),
    ("תאריך השומה הוא 1.1.2020 [S1].", False),
    ("זכויות הבנייה לפי תכנית ב׳ [S1].", True),
    ("הנתונים בתכנית 4/ב", True),
    ("הנתונים של חלקה 12", True),
    ("השוואה בין נכס א׳ לנכס ב׳", True),
    ("קבוצת מגרשים ב'", True),
    ("חלופה 2 לפי ההסכם", True),
    ("הנתונים במצב המוצע", True),
    ("הנתונים המאושרים", True),
    ("תכנית כללית ללא מספר", False),
])
def test_a_line_names_a_scope_when_it_gives_a_plan_parcel_contract_alternative_stage_side_or_state(line, names):
    assert verify.names_scope(line) is names


def _row_key(lead: str, header: str = "| נתון | ערך |", heading: str = "") -> str:
    md = f"{heading}{lead}\n{header}\n|---|---|\n| השווי למ\"ר | 9,500 ₪ [S1] |"
    row = next(u for u in split_units(md) if "9,500" in u.text)
    ws = _ws(SOURCE)
    return verify.unit_key(row, ws, "?", row.heading, {})


def test_the_reuse_key_of_a_table_row_holds_its_header_and_the_scope_its_lead_line_names_and_no_other_line():
    """R7: a verdict is reused when the row, its sources, the table header, the headings and the scope the lead names are the same."""
    plain = _row_key("הטבלה מציגה את הנתונים העיקריים של הנכס [S1].")
    assert plain == _row_key("") == _row_key("שורה אחרת לגמרי, בלי תכנית ובלי חלופה.")  # a line that names no scope is left out
    plan_a, plan_b = _row_key("הנתונים לפי תכנית א׳."), _row_key("הנתונים לפי תכנית ב׳.")
    assert len({plain, plan_a, plan_b}) == 3  # a lead line that names a plan is part of the key, and so is which plan
    assert _row_key("הנתונים לפי תכנית א׳.") == _row_key("הנתונים לפי תכנית א׳.")
    assert _row_key("הטבלה מציגה.", header="| נתון | שווי |") != plain  # the header row is
    assert _row_key("הטבלה מציגה.", heading="## מצב קיים\n") != _row_key("הטבלה מציגה.", heading="## מצב מוצע\n")  # so is the heading chain


def test_the_partial_mark_of_a_table_row_joins_its_last_cell_before_the_closing_bar():
    md = "| נתון | ערך | הערה |\n|---|---|---|\n| השווי למ\"ר | 9,500 ₪ [S1] | פנוי |\n| דמי השכירות | 55 ₪ למ\"ר לחודש [S1] | — |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "partial" if "9,500" in t else "supported"), a, _ws(SOURCE), "?", [])
    edited = r.edit(a).doc.text
    row = next(ln for ln in edited.splitlines() if "9,500" in ln)
    assert row == "| השווי למ\"ר | 9,500 ₪ [S1] | פנוי *(אומת חלקית)* |"
    assert len(verify.cell_bounds(row)) == 3
    assert [s.kind for s in r.edit(a).doc.spans()] == ["partial_mark"]


def test_the_partial_mark_of_a_sentence_still_joins_its_end():
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(lambda t: "partial"), a, _ws(SOURCE), "?", [])
    assert r.edit(a).doc.text == "השווי למ\"ר הוא 9,500 ₪ [S1]. *(אומת חלקית)*"


COUNT_SOURCE = "בפרויקט כ-59 דירות. השווי למ\"ר הוא 9,500 ₪."


def test_an_approximate_count_is_written_as_approximate_and_the_row_is_not_removed():
    """Z08: the source writes a count with an approximation sign and the answer omits it; the server writes the qualifier beside the
    count (it never wrote it for a count), so the judge reads "about 59" and does not find "59" overstated."""
    md = "| נתון | ערך |\n|---|---|\n| מספר הדירות | 59 דירות [S1] |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "supported"), a, _ws(COUNT_SOURCE), "?", [])
    row = next(ln for ln in r.edit(a).doc.text.splitlines() if "59" in ln)
    assert row == "| מספר הדירות | 59 דירות (מקורב, כפי שנכתב במקור) [S1] |", row
    assert r.removed_units() == set()


def test_a_period_is_still_never_written_after_a_count():
    md = "| נתון | ערך |\n|---|---|\n| מספר הדירות | 59 דירות [S1] |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "supported"), a, _ws("בפרויקט 59 דירות. הכנסה חודשית של 100 ₪."), "?", [])
    assert "לחודש" not in r.edit(a).doc.text


PART_SOURCE = "טבלת רכיבים: רכיב א 100 ₪, רכיב ב 200 ₪."


def test_protection_an_unsupported_number_in_a_cell_of_a_row_that_stands_is_still_cut():
    """A number no cited source states is cut with only its own cell's span; the supported cells stay."""
    a = _answer("| רכיב | סכום | הערה |\n|---|---|---|\n| רכיב א | 100 ₪ [S1] | 777 ₪ [S1] |")
    r = verify_answer(_claims_judge(lambda t: [("100 ₪", "supported"), ("777 ₪", "unsupported")] if "רכיב א" in t else None),
                      a, _ws(PART_SOURCE), "?", [])
    out = r.apply(a).answer_markdown
    assert "777" not in out and f"| רכיב א | 100 ₪ [S1] | {MARKER_1} [S1] |" in out and r.removed_units() == set()


def test_a_cut_of_the_key_cell_or_with_a_qualifier_word_or_a_defect_that_changes_the_row_removes_the_row_instead():
    head = "| רכיב | סכום | הערה |\n|---|---|---|\n"
    key = _answer(f"{head}| רכיב ז | 100 ₪ [S1] | תקין |")
    r = verify_answer(_claims_judge(lambda t: [("רכיב ז", "unsupported"), ("100 ₪", "supported")] if "רכיב ז" in t else None),
                      key, _ws(PART_SOURCE), "?", [])
    assert r.removed_units() == {1} and "100" not in r.apply(key).answer_markdown
    qual = _answer(f"{head}| רכיב א | 100 ₪ [S1] | בכפוף לאישור [S1] |")
    r = verify_answer(_claims_judge(lambda t: [("100 ₪", "supported"), ("בכפוף לאישור", "unsupported")] if "רכיב א" in t else None),
                      qual, _ws(PART_SOURCE), "?", [])
    assert r.removed_units() == {1}


# --- scopes below the property: a datum under another plan, parcel, contract or alternative (round 20 U3, KTD3; R9-R13; AE6-AE8) ----------
#
# Every plan number, parcel, name and figure is invented. The sources are read as the verifier holds them: a table read (rows with a
# header), a passage (lines), and the layout of the blocks above a source that the fetcher preloads (``Workspace.layouts``).

from types import SimpleNamespace  # noqa: E402

from app.chat import scopes  # noqa: E402

PLANS_TABLE = ("טבלה 3.1 — זכויות הבנייה לפי תכנית\nהטבלה: 3 שורות\nרכיב | תכנית ג/11111 | תכנית ג/22222\n"
               "שטח עיקרי | 1,200 | 1,800\nשטח שירות | 300 | 450")
PLANS_HEAD = "| רכיב | תכנית ג/11111 | תכנית ג/22222 |\n|---|---|---|\n"


def _row(markdown: str, needle: str) -> Unit:
    return next(u for u in split_units(markdown) if needle in u.text and u.table_span is not None)


def _scope_ws(*texts: str, kind: str = "table") -> Workspace:
    ws = _ws(*texts, kind=kind)
    return ws


def test_a_cell_under_another_plans_column_is_found_by_its_index_and_the_cell_of_its_own_plan_is_not():
    ws = _scope_ws(PLANS_TABLE)
    row = _row(PLANS_HEAD + "| שטח שירות | 450 [S1] | 450 [S1] |", "שטח שירות")
    found = verify._scope_check(row, ws)
    assert list(found.cells) == [1] and found.reason is None and found.facts == []
    assert "תכנית 22222" in found.cells[1] and "תכנית 11111" in found.cells[1]
    assert verify._scope_check(_row(PLANS_HEAD + "| שטח שירות | 300 [S1] | 450 [S1] |", "שטח שירות"), ws).cells == {}


def test_ae6_the_cell_of_another_plan_is_cut_to_the_marker_and_the_rest_of_the_row_stays():
    ws = _scope_ws(PLANS_TABLE)
    md = PLANS_HEAD + "| שטח עיקרי | 1,800 [S1] | 1,800 [S1] |\n| שטח שירות | 300 [S1] | 450 [S1] |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    assert r.removed_units() == set() and r.cuts
    text = r.edit(a).doc.text
    assert "| שטח עיקרי | «[1: חלק שלא אומת הושמט]» [S1] | 1,800 [S1] |" in text  # plan A's cell is cut, plan B's own cell stays
    assert "| שטח שירות | 300 [S1] | 450 [S1] |" in text and text.count("חלק שלא אומת") == 1
    (problem,) = [p for p in r.problems if p.check == verify.SCOPE_CHECK_NAME]
    assert problem.failure_kind == "wrong_subject" and problem.resolution == "cut" and problem.defect == "part"


def test_protection_a_cited_cell_naming_another_plan_is_still_removed_when_it_is_the_rows_only_data_cell():
    ws = _scope_ws(PLANS_TABLE)
    a = _answer(PLANS_HEAD + "| שטח שירות | 450 [S1] | — |")
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    assert 2 in r.removed_units() or any(p.removes_unit and p.check == verify.SCOPE_CHECK_NAME for p in r.problems)
    assert "450" not in r.apply(a).answer_markdown


def test_protection_a_row_whose_key_cell_names_the_wrong_parcel_is_removed_whole():
    ws = _scope_ws("חלקה 12 — שווי 3,100,000 ₪.\nחלקה 14 — שווי 4,200,000 ₪.", kind="context")
    a = _answer("| נושא | שווי |\n|---|---|\n| שווי חלקה 12: 4,200,000 [S1] | 3,100,000 [S1] |")
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    assert any(p.removes_unit and p.check == verify.SCOPE_CHECK_NAME for p in r.problems) and not r.cuts  # the other cell would describe the wrong thing
    assert "4,200,000" not in r.apply(a).answer_markdown and "3,100,000" not in r.apply(a).answer_markdown


def test_a_unit_with_no_scope_opener_and_a_claim_that_names_no_scope_get_no_scope_decision():
    ws = _scope_ws("שווי הנכס בגישת ההשוואה לחלקה 12 הוא 6,800,000 ₪.", kind="context")
    plain = Unit(0, "שווי הנכס הוא 6,800,000 ₪ [S1].", "שווי הנכס הוא 6,800,000 ₪", ["S1"])
    named = Unit(0, "שווי חלקה 12 הוא 6,800,000 ₪ [S1].", "שווי חלקה 12 הוא 6,800,000 ₪", ["S1"])
    for unit in (plain, named):
        found = verify._scope_check(unit, ws)
        assert found.reason is None and found.cells == {} and found.facts == []
    a = _answer("שווי חלקה 12 הוא 6,800,000 ₪ [S1].")
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    assert r.problems == [] and r.removed_units() == set()


PARCELS = "3. שווי הקרקע לפי חלקה\nחלקה 56 — שיעור 3.32% עד 30.10.2025.\nחלקה 57 — שיעור 4.15% עד 4.11.2025."


def test_a_value_of_one_parcel_written_under_another_is_found_in_a_sentence_that_names_the_parcel():
    ws = _scope_ws(PARCELS, kind="context")
    found = verify._scope_check(Unit(0, "בחלקה 56 השיעור הוא 4.15% [S1].", "בחלקה 56 השיעור הוא 4.15%", ["S1"]), ws)
    assert found.reason and "חלקה 57" in found.reason and "חלקה 56" in found.reason
    assert verify._scope_check(Unit(0, "בחלקה 57 השיעור הוא 4.15% [S1].", "בחלקה 57 השיעור הוא 4.15%", ["S1"]), ws).reason is None


def test_the_parcel_the_bullets_lead_names_is_the_referent_of_the_sentence_after_it():
    ws = _scope_ws(PARCELS, kind="context")
    md = "- חלקה 56: מבנה פינתי. השיעור הוא 4.15% [S1]."
    units = split_units(md)
    problems = verify.deterministic(units, ws, "?")
    (p,) = problems
    assert p.check == verify.SCOPE_CHECK_NAME and p.failure_kind == "wrong_subject" and p.unit.text.startswith("השיעור") and p.removes_unit
    right = verify.deterministic(split_units("- חלקה 57: מבנה פינתי. השיעור הוא 4.15% [S1]."), ws, "?")
    assert right == []


def test_the_stored_subject_of_a_measurement_is_the_scope_of_its_datum():
    ws = _scope_ws(PARCELS, kind="context")
    row = SimpleNamespace(value_text="3.32%", subject="המבנה בחלקה 56", quote="", section="", metric="שיעור")
    ws.measurements["M1"] = SimpleNamespace(row=row, context=None, version_id=uuid.uuid4())
    wrong = verify._scope_check(Unit(0, "בחלקה 57 השיעור הוא 3.32% [M1].", "בחלקה 57 השיעור הוא 3.32%", ["M1"]), ws)
    assert wrong.reason and "חלקה 56" in wrong.reason
    assert verify._scope_check(Unit(0, "בחלקה 56 השיעור הוא 3.32% [M1].", "בחלקה 56 השיעור הוא 3.32%", ["M1"]), ws).reason is None


def test_a_claim_that_cites_a_source_under_its_own_parcel_beside_one_under_another_is_kept_with_a_fact():
    ws = _scope_ws(PARCELS, "חלקה 56 — שיעור 4.15% לפי הסכם הארכה.", kind="context")
    found = verify._scope_check(Unit(0, "בחלקה 56 השיעור הוא 4.15% [S1][S2].", "בחלקה 56 השיעור הוא 4.15%", ["S1", "S2"]), ws)
    assert found.reason is None and len(found.facts) == 1 and "S1" in found.facts[0] and "חלקה 57" in found.facts[0]


def test_a_datum_written_under_its_own_parcel_and_under_none_is_told_to_the_judge_and_never_removes():
    ws = _scope_ws("שיעור 4.15% בנכס כולו, לרבות חלקה 56.\nחלקה 57 — שיעור 4.15% עד 4.11.2025.", kind="context")
    found = verify._scope_check(Unit(0, "בחלקה 56 השיעור הוא 4.15% [S1].", "בחלקה 56 השיעור הוא 4.15%", ["S1"]), ws)
    assert found.reason is None and len(found.facts) == 1 and "חלקה 57" in found.facts[0]
    units = split_units("בחלקה 56 השיעור הוא 4.15% [S1].")
    assert verify.deterministic(units, ws, "?") == [] and "חלקה 57" in ws.ownership_facts[verify._fact_key(units[0])]


def test_the_scope_a_fetcher_preloaded_above_a_table_is_its_scope_and_the_claims_plan_is_compared_with_it():
    layout = scopes.Layout((scopes.Piece(1, "ג. בהתאם לתכנית בניין עיר מספר ג/20039 אשר פורסמה", "paragraph", 1, ("7. סיכום",)),
                            scopes.Piece(2, "טבלת זכויות והוראות הבניה:", "paragraph", 1, ("7. סיכום",)),
                            scopes.Piece(3, "שימוש | שטח", "table", 1, ("7. סיכום",))), first=3)
    ws = _scope_ws("הטבלה: 2 שורות\nשימוש | שטח\nמסחר | 11,900", kind="table")
    ws.layouts["S1"] = layout
    claim = Unit(0, "שטח המסחר לפי תכנית ג/25083 הוא 11,900 מ״ר [S1].", "שטח המסחר לפי תכנית ג/25083 הוא 11,900 מ״ר", ["S1"])
    assert "תכנית 20039" in verify._scope_check(claim, ws).reason
    own = Unit(0, "שטח המסחר לפי תכנית ג/20039 הוא 11,900 מ״ר [S1].", "שטח המסחר לפי תכנית ג/20039 הוא 11,900 מ״ר", ["S1"])
    assert verify._scope_check(own, ws).reason is None
    del ws.layouts["S1"]  # without the blocks above it the source states no scope: nothing is decided
    assert verify._scope_check(claim, ws).reason is None


FOOTNOTE_LAYOUT = scopes.Layout((
    scopes.Piece(10, "ג. חוזה לחלקות 41-45 נחתם בשנת 1994", "paragraph", 1, ("4. חוזים",), ("1",)),
    scopes.Piece(11, "ד. חוזה למגרשים 1-3, 8", "paragraph", 1, ("4. חוזים",)),
    scopes.Piece(12, "1 הוצג חוזה אחד בלבד שמייצג את כל המגרשים פרט לתקופת החכירה", "paragraph", 1, ("4. חוזים",), (), "1"),
    scopes.Piece(13, "2 תנאי נוסף שאין לו סימן במקום אחר בעמוד", "paragraph", 1, ("4. חוזים",), (), "2")), first=11)
NOTE_PASSAGE = ("ד. חוזה למגרשים 1-3, 8\n1 הוצג חוזה אחד בלבד שמייצג את כל המגרשים פרט לתקופת החכירה\n"
                "2 תנאי נוסף שאין לו סימן במקום אחר בעמוד")


def test_a_claim_that_states_a_footnote_for_another_scope_than_the_one_its_mark_is_in_is_found():
    ws = _scope_ws(NOTE_PASSAGE, kind="context")
    ws.layouts["S1"] = FOOTNOTE_LAYOUT
    text = "לגבי חלקות 1-3 ו-8 הוצג חוזה אחד בלבד שמייצג את כל המגרשים פרט לתקופת החכירה"
    found = verify._scope_check(Unit(0, text + " [S1].", text, ["S1"]), ws)
    assert found.reason and "חלקה 41" in found.reason and "חלקה 1, 2, 3, 8" in found.reason
    own = "לגבי חלקות 41-45 הוצג חוזה אחד בלבד שמייצג את כל המגרשים פרט לתקופת החכירה"
    assert verify._scope_check(Unit(0, own + " [S1].", own, ["S1"]), ws).reason is None


def test_a_footnote_whose_mark_cannot_be_bound_is_told_to_the_judge_and_attached_to_nothing():
    ws = _scope_ws(NOTE_PASSAGE, kind="context")
    ws.layouts["S1"] = FOOTNOTE_LAYOUT
    text = "קיים תנאי נוסף שאין לו סימן במקום אחר בעמוד לגבי חלקות 1-3"
    found = verify._scope_check(Unit(0, text + " [S1].", text, ["S1"]), ws)
    assert found.reason is None and len(found.facts) == 1 and "סימן 2" in found.facts[0]


# --- the unit's names in the judge's header, and the same-property fact (AE8, R12) ----------------------------------------------------

def _identity_json(*shown: str) -> dict:
    """The stored identifiers of a unit that shows ``shown`` (as the units derivation writes them)."""
    from app.chat import contexts, units

    return units._identifiers_json(contexts.identifiers("\n".join(shown)))


def _named_file(titled: bool = False):
    from app.chat import contexts
    from tests.unit.test_appraisal_context import _unit

    parts = [_unit(1, "appraisal", None, ("מתחם גבעת הרימון", "גבעת הרימון") if titled else (),
                   ("רחוב גפן 7",), first=0, last=3, pages=(1, 1), identifiers=_identity_json("רחוב גפן 7")),
             _unit(2, "appraisal", None, (), ("דרך צבר 3",), first=4, last=7, pages=(2, 2), identifiers=_identity_json("דרך צבר 3"))]
    first = {0: SimpleNamespace(block_index=0, page=1, bbox=[50.0, 40.0, 500.0, 52.0]),
             4: SimpleNamespace(block_index=4, page=2, bbox=[50.0, 40.0, 500.0, 52.0])}
    return contexts.from_units(parts, first, [], "v", "r1")


TITLES = {("v", 1): ("שמאות דמו (סינתטי)", "לכבוד", "מר נועם הדמיון, דרך צבר 3, כפר הדמה",
                     "הנדון: שומת מקרקעין — מתחם גבעת הרימון, רחוב גפן 7", "משרד: שמאות הגבעה, רחוב גפן 7"),
          ("v", 2): ("הנדון: שומת מקרקעין — פרויקט נחל הצבר, דרך צבר 3",)}


def _named_ws(titles=TITLES) -> SimpleNamespace:
    cx = _named_file()
    src = SimpleNamespace(version_id="v", contexts=[1], text="1. הערכת שווי\nשווי הנכס הוא 6,800,000 ₪.", context=cx.describe(1))
    return SimpleNamespace(contexts={"v": cx}, unit_titles=dict(titles), sources={"S1": src}, layouts={})


def test_the_name_a_title_block_gives_its_unit_beside_its_address_is_a_fact_for_the_judge():
    ws = _named_ws()
    question = "מהו השווי של מתחם גבעת הרימון?"
    fact = verify._name_link(ws.contexts["v"], {1}, [question], ws)
    assert "מתחם גבעת הרימון" in fact and "רחוב גפן 7" in fact and "wrong_subject" in fact
    assert verify._name_link(ws.contexts["v"], {2}, [question], ws) == ""  # another unit's block does not write that name


def test_protection_the_addressees_or_the_appraisers_address_never_links_a_name_to_a_unit():
    ws = _named_ws()
    cx = ws.contexts["v"]
    # the addressee's street is unit 2's, in unit 1's block: it is no evidence for unit 1, and a name there is no unit 1's
    assert verify._name_link(cx, {1}, ["מה השווי של מר נועם הדמיון בדרך צבר 3?"], ws) == ""
    # the office line is the only line with the name, and it is not the unit's
    assert verify._name_link(cx, {1}, ["מה השווי של שמאות הגבעה?"], ws) == ""
    # a name that two units' blocks write is no unit's alone
    both = dict(TITLES) | {("v", 2): ("הנדון: שומת מקרקעין — מתחם גבעת הרימון, דרך צבר 3",)}
    assert verify._name_link(cx, {1}, ["מהו השווי של מתחם גבעת הרימון?"], _named_ws(both)) == ""
    # a unit whose neighbours' blocks were not read cannot show the name is its alone
    assert verify._name_link(cx, {1}, ["מהו השווי של מתחם גבעת הרימון?"], _named_ws({("v", 1): TITLES[("v", 1)]})) == ""


def test_the_judges_source_header_also_shows_the_names_the_document_gives_the_unit():
    cx = _named_file(titled=True)
    ws = SimpleNamespace(contexts={"v": cx})
    src = SimpleNamespace(version_id="v", contexts=[1])
    head = verify._names_head(ws, src)
    assert head.startswith(" (נקרא במסמך גם: ") and "«מתחם גבעת הרימון»" in head and "«גבעת הרימון»" in head
    assert verify._names_head(ws, SimpleNamespace(version_id="v", contexts=[2])) == ""  # a unit with no name of its own
    assert verify._names_head(SimpleNamespace(contexts={}), src) == "" and verify._names_head(ws, SimpleNamespace(version_id="v", contexts=[])) == ""
    # the passage's header carries them beside the unit's label and before the file's title
    source = SimpleNamespace(context=cx.describe(1), contexts=[1], version_id="v", location="עמוד 1", title="קובץ", subject="", source_kind="appraisal",
                             text="x", kind="context", date_line=None, link=None, unit=None)
    heading = verify._source_parts(SimpleNamespace(sources={"S1": source}, contexts={"v": cx}, declared_handles={}), "S1")[0]
    assert heading.index("מתחם גבעת הרימון") < heading.index("קובץ: קובץ") and cx.describe(1) in heading


# --- round 20 U5: a missing part is a specific part, never the whole component (KTD5; R16, R18) -------------------------------------

def test_a_missing_text_equal_to_the_whole_component_is_rejected_and_so_is_its_shortened_copy():
    component = "שווי הנכס בגישת ההשוואה"
    request = "מהו שווי הנכס בגישת ההשוואה?"
    assert verify.valid_missing(component, "partial", component, request) == ""
    assert verify.valid_missing("שווי הנכס גישת ההשוואה", "partial", component, request) == ""  # a stop-word-free copy
    long = "מי השוכר שנשארה לו התקופה הארוכה ביותר בכל אחד משני הנכסים, וכמה זמן התקופה?"
    assert verify.valid_missing("מי השוכר שנשארה לו התקופה הארוכה ביותר בכל אחד משני הנכסים", "partial", long, long) == ""  # the closing clause dropped


def test_a_part_of_a_component_is_kept_when_it_is_a_piece_of_its_words_or_is_narrowed_by_a_word_of_the_request():
    component = "השטח והשווי של הנכס"
    request = "מהם השטח והשווי של הנכס בתא שטח 2?"
    assert verify.valid_missing("השטח", "partial", component, request) == "השטח"
    assert verify.valid_missing("השטח והשווי", "partial", component, request) == "השטח והשווי"
    assert verify.valid_missing("השווי בתא שטח 2", "partial", component, request) == "השווי בתא שטח 2"  # narrowed by the request
    assert verify.valid_missing("השטח והשווי של הנכס בתא שטח 2", "partial", component, request) == "השטח והשווי של הנכס בתא שטח 2"
    # a number the component writes that the part leaves out narrows it to the piece the number names
    plots = "שטחי השירות בתא שטח 1 ובתא שטח 2"
    assert verify.valid_missing("שטחי השירות בתא שטח 2", "partial", plots, plots) == "שטחי השירות בתא שטח 2"
    assert verify.covers_component("שטחי השירות בתא שטח 2", plots) is False
    assert verify.covers_component(plots, plots) is True and verify.covers_component("", plots) is False
    assert verify.covers_component("מרפסות", plots) is False  # a word the component never wrote is no copy of it


def test_the_components_subject_and_the_users_question_are_words_of_the_request_for_a_missing_part():
    component = "השוכר הארוך ביותר בכל אחד משני הנכסים"
    request = "מי השוכר הארוך ביותר בכל נכס?"
    part = "השוכר הארוך ביותר באינדג'יה"
    assert verify.valid_missing(part, "partial", component, request) == ""  # a word of neither text
    assert verify.valid_missing(part, "partial", component, request, "נכס אינדג'יה") == part
    assert verify.valid_missing(part, "partial", component, request, "", "מי השוכר באינדג'יה ובקרגוייבץ?") == part
    assert verify.valid_missing("השוכר באילת", "partial", component, request, "נכס אינדג'יה") == ""  # still never a word nobody wrote
