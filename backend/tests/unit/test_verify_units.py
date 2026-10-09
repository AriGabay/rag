"""Verification fails closed: a claim the judge did not check is never kept as checked, and the judge reads the
evidence that covers each claim, wherever it is in the source. The judge here is scripted; these tests prove the
server's handling of its verdicts and failures, not the model's judgement. Synthetic text only."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import verify
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
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
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
    md = ("# הנכס פנוי\n**השווי נקבע ל-9,800 ₪**\n**הנכס מושכר:**\n## מקורות\n"
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
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ג נקבע סף של 63 נקודות."
    a = _answer(md)  # S2 states the 63, but no unit cites it, so the judge was never shown it
    r = verify_answer(_supported_by(_named("S2")), a, _ws(TABLE, OTHER), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]
    assert "63" not in r.apply(a).answer_markdown


@pytest.mark.parametrize("named", ["S7", "P1", "A1"])
def test_supported_by_an_id_that_is_no_evidence_of_the_turn_is_removed(named):
    md = "שלב א עבר עם סף 40 נקודות [S1].\nבשלב ב הסף היה 55 נקודות."
    a = _answer(md)
    r = verify_answer(_supported_by(_named("S1", also=named)), a, _ws(TABLE), "?", [])
    assert [p.unit.index for p in r.problems if p.removes_unit] == [1]


def test_an_uncited_claim_with_no_evidence_in_the_batch_stays_unsupported():
    md = "שלב א עבר עם סף 40 נקודות.\nהנכס פנוי."
    a = _answer(md)  # nothing is cited, so no source is shown; a source the judge names anyway is not accepted
    p = _supported_by(lambda t, c: ("supported", ["S1"]))
    r = verify_answer(p, a, _ws(TABLE), "?", [])
    assert "<source id=" not in p.calls[0].input
    assert sorted(x.unit.index for x in r.problems if x.removes_unit) == [0, 1]


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
