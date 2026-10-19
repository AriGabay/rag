"""Kind-aware verification (round 10 U9: R31, R33, R34, R35; KD13, KTD14).

A standard supports a professional rule, never a property's datum: a sentence that states a datum of the property
(a number with a money or area unit, in a sentence that names the property) and cites only standards or professional
documents fails a deterministic check; a sentence that attributes its number to the standard ("לפי סעיף 4", "התקן
קובע", a worked example) is left to the judge. The judge's evidence header carries the source kind and, for a standard,
the limits (conditions and exceptions) and the wording of the section the passage is in, and its policy gains the rules
that need them. A professional turn creates no "property not located" gap and no wrong-context removal. The judge is
scripted: these tests prove what the server checks and what the judge is shown, not the judge's judgement. Synthetic
data only (every standard and property here is invented); no database."""

from __future__ import annotations

import re
import uuid

from app.chat import coverage, verify
from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import Unit, VerdictCache, split_units, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_coverage import _in_units

CHECK = "property_datum_from_standard"
LIMIT = "הוראה זו חלה על כל הנכסים למעט נכסים חריגים שאין להם נכסי השוואה."
EXAMPLE_TEXT = "דוגמת חישוב סינתטית: שווי נכס של 2,400,000 ₪ ושטח של 12 מ\"ר. שטח 100 מ\"ר מוכפל במקדם 0.85."
APPRAISAL_TEXT = "שווי הנכס 2,400,000 ₪. שטח הנכס 85 מ\"ר. הנכס פנוי."


def _std(number: str = "98.1", edition: str | None = "גרסה 2.0", section: str = "2.1") -> dict:
    return {"number": number, "title": "מדידת שטחים סינתטית", "edition": edition, "effective": "05.10.2025" if edition
            else None, "section": section, "section_title": "גישת ההשוואה"}


def _ws(intent: str = "professional") -> Workspace:
    ws = Workspace(ctx=None)
    ws.intent = intent
    return ws


def standard(ws: Workspace, text: str = EXAMPLE_TEXT, *, rules: dict | None = None, kind: str = "standard",
             std: dict | None = None) -> str:
    """A standard's passage as the tools return it (U8): its kind and identity, its title and location from them."""
    std = std or _std()
    return ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title=T.standard_title(std, "מסמך"),
                         section=T.section_string(std), location=T.standard_location(std, [3]), kind="text", text=text,
                         source_kind=kind, standard=std, rules=rules).sid


def appraisal(ws: Workspace, text: str = APPRAISAL_TEXT) -> str:
    return ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה הרקפות 5 (סינתטי)",
                         section="סיכום", location="סעיף \"סיכום\"", kind="context", text=text).sid


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _judge(respond=None, seen: list[str] | None = None) -> ScriptedProvider:
    p = ScriptedProvider()

    def answer(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)
        if respond is not None:
            return {"verdicts": [{"index": int(i), "verdict": respond(input, t), "reason": "בדיקה"} for i, t in units]}
        return {"verdicts": [{"index": int(i), "verdict": "supported", "reason": "בדיקה"} for i, _ in units]}

    p.on(Purpose.VERIFY, answer, repeat=True)
    return p


def _problems(markdown: str, ws: Workspace, question: str = "?") -> list[verify.Problem]:
    units = split_units(markdown)
    verify.assign_ids(units, ws.claim_salt)
    return verify.deterministic(units, ws, question)


def _checks(markdown: str, ws: Workspace) -> list[str]:
    return [p.check for p in _problems(markdown, ws) if p.check == CHECK]


# --- the deterministic check: a property datum cited only to a standard ------------------------------------------------

def test_a_property_value_cited_only_to_a_standard_is_unsupported_and_the_same_number_cited_to_the_appraisal_is_not():
    ws = _ws()
    s1, s2 = standard(ws), appraisal(ws)
    (problem,) = _problems(f"שווי הנכס 2,400,000 ₪ [{s1}].", ws)
    assert problem.check == CHECK and problem.failure_kind == "wrong_subject" and problem.severity == "error"
    assert problem.removes_unit and "2,400,000" in problem.reason
    assert not _problems(f"שווי הנכס 2,400,000 ₪ [{s2}].", ws)  # the number cited to the appraisal is untouched
    assert _checks(f"שווי הנכס 2,400,000 ₪ [{s1}][{s2}].", ws) == []  # one appraisal among the citations is enough here


def test_the_unit_fails_before_the_judge_is_asked_and_leaves_the_answer():
    ws = _ws()
    s1, s2 = standard(ws), appraisal(ws)
    seen: list[str] = []
    md = f"שווי הנכס 2,400,000 ₪ [{s1}].\n\nשטח הנכס 85 מ\"ר [{s2}]."
    report = verify_answer(_judge(seen=seen), _answer(md), ws, "?", [])
    assert report.removed_units() == {0} and [p.check for p in report.problems] == [CHECK]
    assert '<unit index="0"' not in "".join(seen) and '<unit index="1"' in "".join(seen)
    assert "2,400,000" not in report.apply(_answer(md)).answer_markdown


def test_a_number_the_sentence_attributes_to_the_standard_does_not_trigger_and_one_about_the_property_does():
    ws = _ws()
    s1 = standard(ws)
    # the sentence gives the number to a section, the standard or its worked example: the judge decides
    assert _checks(f"לפי סעיף 4, שטח מינימלי של 12 מ\"ר [{s1}].", ws) == []
    assert _checks(f"התקן קובע שטח של 12 מ\"ר בנכס [{s1}].", ws) == []
    assert _checks(f"על פי סעיף 2.1, שטח הנכס יהיה 12 מ\"ר [{s1}].", ws) == []
    assert _checks(f"בדוגמה שבתקן, שטח הנכס 12 מ\"ר ושוויו 2,400,000 ₪ [{s1}].", ws) == []
    # the property named by a pronoun bound to it, or by a unit label
    assert _checks(f"שטח הנכס 12 מ\"ר [{s1}].", ws) == [CHECK]
    assert _checks(f"בנכס יש שטח של 12 מ\"ר [{s1}].", ws) == [CHECK]
    assert _checks(f"שטח הדירה הוא 12 מ\"ר [{s1}].", ws) == [CHECK]
    assert _checks(f"חנות 12 בשטח של 12 מ\"ר [{s1}].", ws) == [CHECK]


def test_a_sentence_that_names_no_property_or_states_no_datum_does_not_trigger():
    ws = _ws()
    s1 = standard(ws)
    assert _checks(f"נכסים בשטח 12 מ\"ר חייבים בממ\"ד [{s1}].", ws) == []  # a class of properties, not the property
    assert _checks(f"שיטת ההשוואה נבחרת כשיש עסקאות השוואה בנכסים דומים [{s1}].", ws) == []  # no number
    assert _checks(f"הנכס נבדק לפי שלוש שיטות [{s1}].", ws) == []  # a number with no unit and no measurement kind


def test_the_property_the_request_or_the_conversation_names_counts_as_the_property():
    ws = _ws("mixed")
    s1 = standard(ws)
    ws.unit_focus = {"handle": "U1", "document_id": str(uuid.uuid4()), "display": "רחוב הרקפות 5, תל אביב"}
    assert _checks(f"בהרקפות 5 השטח הוא 12 מ\"ר [{s1}].", ws) == [CHECK]
    assert _checks(f"ברחוב אחר השטח הוא 12 מ\"ר [{s1}].", ws) == []


def test_a_unit_citing_anything_but_professional_sources_is_not_this_checks():
    ws = _ws()
    s1, s2 = standard(ws), appraisal(ws)
    other = standard(ws, EXAMPLE_TEXT, kind="professional")
    assert _checks(f"שטח הנכס 12 מ\"ר [{s1}][{other}].", ws) == [CHECK]  # a standard and a professional document
    assert _checks(f"שטח הנכס 12 מ\"ר [{s1}][{s2}].", ws) == []
    assert _checks("שטח הנכס 12 מ\"ר.", ws) == []  # no citation: the existing uncited path


def test_an_appraisal_only_turn_never_meets_the_check():
    ws = _ws("property")
    s2 = appraisal(ws)
    assert _problems(f"שווי הנכס 2,400,000 ₪ [{s2}].", ws) == []


# --- the inputs of a calculation are verified against the appraisal only ---------------------------------------------

def test_an_input_of_the_property_found_only_in_the_standards_example_is_not_stated_by_the_appraisal_beside_it():
    ws = _ws()
    s1, s2 = standard(ws), appraisal(ws)
    # 85 מ"ר is the appraisal's; 100 מ"ר is the standard's worked example
    assert _problems(f"שטח הנכס 85 מ\"ר [{s1}][{s2}].", ws) == []
    (problem,) = _problems(f"שטח הנכס 100 מ\"ר [{s1}][{s2}].", ws)
    assert problem.check == "unstated_number" and "100" in problem.unstated
    # the coefficient is the standard's method (no money or area unit): the standard may state it
    assert _problems(f"המקדם 0.85 [{s1}][{s2}].", ws) == []
    assert _problems(f"לפי סעיף 2.1 מכפילים שטח של 100 מ\"ר במקדם 0.85 [{s1}][{s2}].", ws) == []


# --- the evidence the judge sees ------------------------------------------------------------------------------------------

RULES = {"limits": [LIMIT], "modality": {"requirement": 1, "recommendation": 0, "example": 0, "alternative": 0,
                                           "exception": 1}}
RECOMMENDATION_ONLY = {"limits": [], "modality": {"requirement": 0, "recommendation": 2, "example": 0, "alternative": 0,
                                                   "exception": 0}}


def _rendered(ws: Workspace, markdown: str) -> str:
    return verify._render_batch(verify._Batch(split_units(markdown)), ws)


def test_the_judges_evidence_for_a_standard_holds_its_kind_edition_section_limits_and_wording():
    ws = _ws()
    s1 = standard(ws, "על השמאי לבסס את הערכתו על עסקאות השוואה.", rules=RULES)
    head, body, _ = verify._source_parts(ws, s1)
    assert "98.1" in head and "גרסה 2.0" in head and "סעיף 2.1" in head
    assert "תקן" in head and "אינו שומה" in head  # the kind, said in words
    assert LIMIT in head and "ניסוח חובה" in head and "חריג" in head
    assert LIMIT not in body and "חובה" not in body  # the text the number check reads is the passage alone
    rendered = _rendered(ws, f"יש לבסס את ההערכה על עסקאות השוואה [{s1}].")
    assert LIMIT in rendered and "ניסוח חובה" in rendered


def test_a_section_with_no_requirement_wording_is_shown_as_such():
    ws = _ws()
    s1 = standard(ws, "מומלץ להוון את ההכנסה לפי שיעור שוק.", rules=RECOMMENDATION_ONLY)
    head, _, _ = verify._source_parts(ws, s1)
    assert "המלצה" in head and "אין בו ניסוח חובה" in head


def test_a_standard_without_stated_edition_says_so_and_a_professional_document_is_named_as_one():
    ws = _ws()
    s1 = standard(ws, std=_std(edition=None))
    p1 = standard(ws, kind="professional", std={"number": None, "title": None, "edition": None, "effective": None,
                                                 "section": None, "section_title": None})
    assert "מהדורה: לא צוינה" in verify._source_parts(ws, s1)[0]
    head = verify._source_parts(ws, p1)[0]
    assert "מסמך מקצועי" in head and "אינו שומה" in head


def test_the_header_of_an_appraisal_source_is_byte_identical():
    ws = _ws("property")
    s2 = appraisal(ws)
    assert verify._source_parts(ws, s2) == ("שומה הרקפות 5 (סינתטי) — סעיף \"סיכום\"", APPRAISAL_TEXT, "context")
    plain = Workspace(ctx=None)
    plain.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח", section="סיכום",
                     location="עמוד 2", kind="context", text="טקסט", context="שומה ב", subject="הנכס")
    assert verify._source_parts(plain, "S1") == ("דוח — עמוד 2 — שומה ב", "טקסט", "context")


def test_a_changed_limit_or_wording_is_a_new_verdict_key_and_an_appraisals_key_is_unchanged_by_it():
    unit = Unit(0, "[S1]", "יש להתאים", ["S1"])
    digests: dict = {}
    keys = []
    for rules in (RULES, RECOMMENDATION_ONLY, None):
        ws = _ws()
        standard(ws, "טקסט", rules=rules)
        keys.append(verify.unit_key(unit, ws, "?", "", {}))
    assert len(set(keys)) == 3
    ws = _ws("property")
    appraisal(ws)
    assert verify.unit_key(unit, ws, "?", "", digests) == verify.unit_key(unit, ws, "?", "", {})


def test_the_judge_version_is_bumped_and_the_cache_does_not_reuse_an_older_contract(monkeypatch):
    assert verify.JUDGE_VERSION == 12  # round 13 U9: server-held unit facts and derived computations; U11: the preceding text of a back-reference; round 15 U7: the declared unit in a source header; round 16 U1: the read passages and `support`; 12: every returned passage, items of a kind
    ws = _ws()
    s1 = standard(ws, "על השמאי לבסס את הערכתו על עסקאות השוואה.", rules=RULES)
    seen: list[str] = []
    cache, judge = VerdictCache(), _judge(seen=seen)
    md = f"יש לבסס את ההערכה על עסקאות השוואה [{s1}]."
    verify_answer(judge, _answer(md), ws, "?", [], cache=cache)
    assert verify_answer(judge, _answer(md), ws, "?", [], cache=cache).reused == 1
    monkeypatch.setattr(verify, "JUDGE_VERSION", verify.JUDGE_VERSION + 1)
    assert verify_answer(judge, _answer(md), ws, "?", [], cache=cache).reused == 0


def test_the_policy_carries_the_kind_aware_rules():
    policy = verify.JUDGE_POLICY
    for words in ("רחב מגבולותיו", "מחייבת", "כלל מקצועי כללי", "מהדורת התקן", "אינו מאמת"):
        assert words in policy, words


def test_a_requirement_worded_claim_against_a_recommendation_only_section_goes_to_the_judge_with_the_wording_shown():
    ws = _ws()
    s1 = standard(ws, "מומלץ להוון את ההכנסה לפי שיעור שוק.", rules=RECOMMENDATION_ONLY)
    seen: list[str] = []

    def judge(input: str, unit: str) -> str:
        return "unsupported" if "אין בו ניסוח חובה" in input and "חובה" in unit else "supported"

    md = f"חובה להוון את ההכנסה לפי שיעור שוק [{s1}].\n\nמומלץ להוון את ההכנסה לפי שיעור שוק [{s1}]."
    report = verify_answer(_judge(judge, seen), _answer(md), ws, "?", [])
    assert "אין בו ניסוח חובה" in seen[0] and "המלצה" in seen[0]
    assert report.removed_units() == {0} or any(p.repairable and p.unit.index == 0 for p in report.problems)
    assert report.verdicts[1] == "supported"


def test_a_noncompliance_claim_shows_the_judge_both_sources_and_editions_and_a_not_supported_verdict_drops_it():
    ws = _ws()
    std = standard(ws, "על השמאי לבסס את הערכתו על חמש עסקאות השוואה.")
    s2 = appraisal(ws, "השומה נשענת על שלוש עסקאות השוואה. הנכס פנוי.")
    seen: list[str] = []

    def judge(input: str, unit: str) -> str:
        return "unsupported" if "אינה עומדת בתקן" in unit else "supported"

    md = f"השומה אינה עומדת בתקן 98.1 [{std}][{s2}].\n\nהשומה נשענת על שלוש עסקאות השוואה [{s2}]."
    report = verify_answer(_judge(judge, seen), _answer(md), ws, "?", [])
    assert "גרסה 2.0" in seen[0] and "תקן 98.1" in seen[0] and "שומה הרקפות 5" in seen[0]
    assert report.removed_units() == {0} or any(p.repairable and p.unit.index == 0 for p in report.problems)
    assert report.verdicts[1] == "supported"


# --- a professional turn: no property gap and no wrong-context removal (R35) --------------------------------------------

def test_a_professional_turns_supported_sentences_are_all_kept_and_no_gap_line_is_written():
    ws = _ws()
    s1 = standard(ws, "על השמאי לבסס את הערכתו על עסקאות השוואה. " + LIMIT, rules=RULES)
    md = f"יש לבסס את ההערכה על עסקאות השוואה [{s1}].\n\nההוראה חלה על כל הנכסים למעט נכסים חריגים [{s1}]."
    report = verify_answer(_judge(), _answer(md), ws, "?", [])
    assert report.ok and not report.removed_units() and not report.problems
    edited, outcomes = coverage.compose_components(ws, _answer(md), report)
    assert edited.answer(_answer(md)).answer_markdown == md and outcomes == [] and report.gaps == []


def test_an_appraisal_example_read_in_a_file_of_several_units_makes_no_unit_not_located_gap_under_a_professional_question():
    from tests.unit.test_coverage import _only_missing
    from tests.unit.test_coverage import _ws as _coverage_ws

    for intent, reason in (("property", "unit_not_located"), ("professional", None)):
        ws = _coverage_ws()
        ws.searches.append("דמי ניהול")
        ws.intent = intent
        _in_units(ws)
        outcome, gap = _only_missing(ws)
        if reason is not None:
            assert outcome["limitation"] == reason
        else:
            assert outcome["limitation"] != "unit_not_located" and "לא אותרה" not in gap, (outcome, gap)


def test_a_professional_question_never_removes_a_sentence_as_another_propertys_figure(monkeypatch):
    from app.config import get_settings
    from tests.unit.test_appraisal_context import _claim_ws, _units_contexts

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _units_contexts()
    asked = "מה שווי שדרות האלון?"
    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    other = _claim_ws(cx, {"V1": 3}, asked)  # appraisal 3's value under a question about appraisal 1
    other.intent = "property"
    assert verify._wrong_context(unit, other, asked)
    other.intent = "professional"
    assert verify._wrong_context(unit, other, asked) is None


def _listing(ws: Workspace, kind: str, text: str = "מספר: 98.1 | שם: מדידת שטחים סינתטית") -> str:
    """A listing source as ``_listing`` registers it (round 11 U4): it names no document, and has the kind of the
    documents it lists."""
    return ws.add_source(document_id=None, version_id=None, title="תקנים ומסמכים מקצועיים", section=None,
                         location="רשימת מסמכים", kind="listing", text=text, source_kind=kind).sid


def test_a_property_datum_citing_only_a_listing_of_standards_is_flagged_and_one_citing_a_listing_of_appraisals_is_not():
    ws = _ws()
    standards, appraisals = _listing(ws, "professional"), _listing(ws, "appraisal")
    assert _checks(f"שטח הנכס 98.1 מ\"ר [{standards}].", ws) == [CHECK]
    assert _checks(f"שטח הנכס 98.1 מ\"ר [{appraisals}].", ws) == []
    assert _checks(f"שטח הנכס 98.1 מ\"ר [{standards}][{appraisals}].", ws) == []  # an appraisal listing beside it
