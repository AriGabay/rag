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


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[])


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
