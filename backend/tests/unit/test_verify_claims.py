"""The judge contract of round 8 (U2, KTD1, KTD7; R4, R6, R7, R9, R14): a verdict may carry ``claims`` — exact spans of
its unit with their own verdicts — the server turns every verdict into validated claims (a span that is not in the unit,
or no claims, falls back to one whole-unit claim carrying the unit verdict), the judge is shown the heading chain and a
table row's header above a unit, and a changed heading or judge version is a new verdict key.

The judge is scripted: these tests prove what the server does with what the judge returns and what the judge is shown,
not the judge's judgement. Synthetic texts only; no database."""

from __future__ import annotations

import re
import uuid

from app.chat import verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import JudgeClaim, JudgeVerdict, Unit, VerdictCache, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider

SOURCE = "דוח: תכסית 40% מותרת. גובה הבניין 12 מטר. הנכס פנוי."
SENTENCE = "תכסית 40% מותרת, והנכס מושכר [S1]."
SPAN_OK, SPAN_BAD = "תכסית 40% מותרת", "והנכס מושכר"


def _ws(*texts: str) -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts or (SOURCE,):
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    return ws


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _judge(respond, seen: list[str] | None = None) -> ScriptedProvider:
    p = ScriptedProvider()

    def answer(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        return respond(input)

    p.on(Purpose.VERIFY, answer, repeat=True)
    return p


def _supported(input: str) -> dict:
    return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in _units(input)]}


def _unit(text: str = SENTENCE, **kw) -> Unit:
    return Unit(0, text, re.sub(r"\s*\[S1\]", "", text), ["S1"], **kw)


def _mixed(*claims: dict, verdict: str = "partial") -> JudgeVerdict:
    return JudgeVerdict.model_validate({"index": 0, "verdict": verdict, "reason": "x", "claims": list(claims)})


# --- the schema: additive, every field defaulted -----------------------------------------------------------------

def test_an_older_reply_without_claims_validates_and_yields_one_whole_unit_claim():
    v = JudgeVerdict.model_validate({"index": 0, "verdict": "supported", "reason": "x"})  # no claims, as before
    assert v.claims == []
    (claim,) = verify.unit_claims(_unit(), v)
    assert (claim.span, claim.verdict, claim.whole_unit, claim.fallback) == (_unit().text, "supported", True, "")
    # a claim's own fields are all defaulted: a reply naming only a span validates
    assert JudgeClaim.model_validate({"span": "x"}).needs_context == "none"


def test_the_judge_policy_and_schema_name_the_new_vocabulary():
    for word in ("claims", "span", "contradicts", "needs_context", "<server>"):
        assert word in verify.JUDGE_POLICY, word
    assert "contradicts" in JudgeVerdict.model_fields["verdict"].annotation.__args__
    for kind in ("heading", "neighbours", "continuation", "table_headers", "units_note", "footnotes", "next_page",
                 "region"):
        assert kind in verify.JUDGE_POLICY, kind
    # rule 9 (KTD3): an uncited factual claim is judged against every source shown, and a claim no shown source
    # supports is not linked to a read source
    assert verify.NOT_LINKED_REASON in verify.JUDGE_POLICY


# --- the server turns a verdict into validated claims ------------------------------------------------------------

def test_a_span_that_is_not_an_exact_substring_falls_back_to_the_whole_unit_and_records_it():
    v = _mixed({"span": SPAN_OK, "verdict": "supported"},
               {"span": "הנכס אינו פנוי", "verdict": "unsupported"})  # reworded: not in the unit
    claims = verify.unit_claims(_unit(), v)
    assert len(claims) == 1 and claims[0].whole_unit and claims[0].fallback == "span_not_in_unit", claims
    assert claims[0].verdict == "partial"  # it carries the unit's verdict, not either claim's


def test_an_empty_span_falls_back_too():
    (claim,) = verify.unit_claims(_unit(), _mixed({"span": "  ", "verdict": "unsupported"}))
    assert claim.whole_unit and claim.fallback == "empty_span"


def test_valid_spans_are_kept_with_their_own_verdicts_kinds_and_context_needs():
    v = _mixed({"span": SPAN_OK, "verdict": "supported"},
               {"span": SPAN_BAD, "verdict": "contradicts", "kind": "datum"},
               {"span": "מושכר", "verdict": "partial", "needs_context": "table_headers", "depends_on": [SPAN_OK, "3"]})
    claims = verify.unit_claims(_unit(), v)
    assert [(c.span, c.verdict, c.whole_unit, c.fallback) for c in claims] == [
        (SPAN_OK, "supported", False, ""), (SPAN_BAD, "contradicts", False, ""), ("מושכר", "partial", False, "")]
    assert claims[1].failure == "contradicts_source"  # a contradiction is that failure kind, with no help from the judge
    assert claims[2].needs_context == "table_headers" and claims[2].depends_on == (SPAN_OK, "3")


def test_the_prompt_look_alike_quotes_are_read_back_into_the_units_own_characters():
    text = "ההיתר מתיר <בקשה> לתוספת"
    unit = Unit(0, text, text, [])
    (claim,) = verify.unit_claims(unit, _mixed({"span": "<בקשה> לתוספת", "verdict": "partial"}))
    assert not claim.whole_unit
    (claim,) = verify.unit_claims(unit, _mixed({"span": "‹בקשה› לתוספת", "verdict": "partial"}))  # as the judge saw it
    assert not claim.whole_unit and claim.span == "<בקשה> לתוספת"


def test_a_verdicts_failure_kind_for_the_whole_unit_claim():
    unit = _unit()
    cases = [("unsupported", "none", "absent_from_source"), ("unsupported", "wrong_unit", "wrong_unit"),
             ("contradicts", "none", "contradicts_source"), ("supported", "none", "none"), ("partial", "none", "none")]
    for verdict, failure, expected in cases:
        v = JudgeVerdict.model_validate({"index": 0, "verdict": verdict, "reason": "x", "failure": failure})
        (claim,) = verify.unit_claims(unit, v, _ws())
        assert claim.failure == expected, (verdict, failure, claim.failure)


def test_a_unit_with_one_claim_gets_no_claims_list_and_the_server_builds_the_whole_unit_claim():
    seen: list[str] = []
    report = verify_answer(_judge(_supported, seen), _answer("תכסית 40% מותרת [S1]."), _ws(), "?", [])
    # the policy tells the judge to leave `claims` empty here; the server builds the unit's single claim itself
    assert report.claims.keys() == {0}
    (claim,) = report.claims[0]
    assert claim.whole_unit and claim.verdict == "supported" and claim.span == report.units[0].text


def test_a_contradicts_verdict_removes_the_unit_as_a_contradiction():
    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "contradicts", "reason": "המקור אומר 12"} for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer(SENTENCE), _ws(), "?", [])
    (removal,) = report.removals()
    assert removal.failure_kind == "contradicts_source" and removal.removes_unit
    assert report.claims[0][0].verdict == "contradicts"


def test_a_report_carries_the_validated_claims_of_a_mixed_unit_by_unit_index():
    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
            {"span": SPAN_OK, "verdict": "supported"}, {"span": SPAN_BAD, "verdict": "unsupported"}]}
            for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer(SENTENCE), _ws(), "?", [])
    assert [(c.span, c.verdict) for c in report.claims[0]] == [(SPAN_OK, "supported"), (SPAN_BAD, "unsupported")]
    assert report.verdicts == {0: "partial"}  # the unit verdict is unchanged: removal is not this unit's concern


def test_a_cached_verdict_keeps_its_claims_at_the_units_new_index():
    cache, seen = VerdictCache(), []

    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
            {"span": SPAN_OK, "verdict": "supported"}, {"span": SPAN_BAD, "verdict": "unsupported"}]}
            if "מושכר" in t else {"index": i, "verdict": "supported", "reason": "x"} for i, t in _units(input).items()]}

    judge, ws = _judge(respond, seen), _ws()
    verify_answer(judge, _answer(SENTENCE), ws, "?", [], cache=cache)
    again = verify_answer(judge, _answer("הנכס פנוי [S1].\n" + SENTENCE), ws, "?", [], cache=cache)
    assert again.reused == 1 and [c.span for c in again.claims[1]] == [SPAN_OK, SPAN_BAD], again.claims


# --- what the judge is shown: the heading chain, a table row's header, a server element ---------------------------

def _rendered(markdown: str) -> str:
    """What the judge is shown for every unit of an answer, in one call."""
    return verify._render_batch(verify._Batch(verify.split_units(markdown)), _ws())


def _context_of(out: str, index: int) -> str:
    """The ``<unit_context>`` element just above unit ``index`` ("" when it has none)."""
    m = re.search(rf'(<unit_context unit="{index}">.*?</unit_context>)\n<unit index="{index}"', out, re.S)
    return m.group(1) if m else ""


def test_the_rendered_batch_contains_the_headings_above_a_unit():
    out = _rendered("# מצב קיים\n## מצב מוצע\nתכסית 40% מותרת [S1].\n## מצב מאושר\nהנכס פנוי [S1].")
    units = _units(out)
    sentence, other = [i for i, t in units.items() if t.startswith("תכסית")][0], [i for i, t in units.items()
                                                                                  if t.startswith("הנכס")][0]
    assert "<heading>מצב קיים › מצב מוצע</heading>" in _context_of(out, sentence), out
    assert "<heading>מצב קיים › מצב מאושר</heading>" in _context_of(out, other), out
    # the heading is context beside the unit, never inside its text; a heading's own context is the headings above it
    assert "מצב מוצע" not in units[sentence]
    assert "<heading>מצב קיים</heading>" in _context_of(out, 1) and "מצב מוצע" not in _context_of(out, 3), out


def test_the_heading_chain_pops_a_deeper_level_and_keeps_a_label_under_it():
    markdown = "# א\n## ב\nשורה [S1].\n# ג\nשורה אחרת [S1].\n**סיכום:**\nשורה שלישית [S1]."
    chains = {u.text: u.heading for u in verify.split_units(markdown)}
    assert chains["שורה ."] == "א › ב" and chains["שורה אחרת ."] == "ג" and chains["שורה שלישית ."] == "ג › סיכום:"
    assert chains["## ב"] == "א" and chains["# ג"] == ""  # a heading's chain is the headings it sits under


def test_a_table_rows_header_context_is_rendered():
    out = _rendered("## נתוני השוואה\n| נכס | שווי |\n| --- | --- |\n| א | 9,500 |")
    (row,) = [i for i, t in _units(out).items() if t == "| א | 9,500 |"]
    context = _context_of(out, row)
    assert "<table_header>| נכס | שווי |</table_header>" in context, out
    assert "<table_intro>## נתוני השוואה</table_intro>" in context and "<heading>נתוני השוואה</heading>" in context


def test_a_unit_with_no_heading_and_no_table_context_renders_no_context_element():
    assert "<unit_context" not in _rendered("תכסית 40% מותרת [S1].\nהנכס פנוי [S1].")


def test_a_server_span_inside_a_unit_renders_as_a_server_element_in_place():
    unit = Unit(0, "תכסית 40% מותרת [חלק שלא אומת הושמט].", "תכסית 40% מותרת [חלק שלא אומת הושמט].", [],
                server=["[חלק שלא אומת הושמט]"])
    out = verify._render_batch(verify._Batch([unit]), _ws())
    assert "תכסית 40% מותרת <server>[חלק שלא אומת הושמט]</server>." in out, out
    # one that is not in the unit's text (an inserted citation) follows it inside the unit block
    unit = Unit(0, "תכסית 40% מותרת.", "תכסית 40% מותרת.", [], server=["[S1]"])
    assert "תכסית 40% מותרת.\n<server>[S1]</server>\n</unit>" in verify._render_batch(verify._Batch([unit]), _ws())
    # none by default: the unit renders exactly as before
    assert "<server>" not in verify._render_batch(verify._Batch([_unit()]), _ws())


def test_a_server_marker_inside_a_supported_sentence_leaves_the_unit_supported():
    """The marker is shown as a server element, not as text to judge; the verdict path is the ordinary one."""
    seen: list[str] = []
    markdown = "תכסית 40% מותרת, [חלק שלא אומת הושמט] [S1]."
    monkey = verify.split_units(markdown)
    for u in monkey:
        u.server = ["[חלק שלא אומת הושמט]"]
    rendered = verify._render_batch(verify._Batch(monkey), _ws())
    assert "<server>[חלק שלא אומת הושמט]</server>" in rendered
    report = verify_answer(_judge(_supported, seen), _answer(markdown), _ws(), "?", [])
    assert report.ok and report.claims[0][0].verdict == "supported"


# --- the verdict key: heading chain and judge version --------------------------------------------------------------

def test_a_changed_heading_changes_the_unit_key_and_so_does_the_judge_version(monkeypatch):
    ws, digests = _ws(), {}
    unit = _unit()
    base = verify.unit_key(unit, ws, "?", "מצב מוצע", digests)
    assert verify.unit_key(unit, ws, "?", "מצב מאושר", digests) != base
    assert verify.unit_key(unit, ws, "?", "מצב מוצע", digests) == base
    monkeypatch.setattr(verify, "JUDGE_VERSION", verify.JUDGE_VERSION + 1)
    assert verify.unit_key(unit, ws, "?", "מצב מוצע", digests) != base


def test_a_deeper_heading_chain_is_a_new_verdict_for_the_same_sentence():
    seen: list[str] = []
    cache, ws = VerdictCache(), _ws()
    judge = _judge(_supported, seen)
    verify_answer(judge, _answer("# א\n## מצב מוצע\nתכסית 40% מותרת [S1]."), ws, "?", [], cache=cache)
    n = len(seen)
    again = verify_answer(judge, _answer("# ב\n## מצב מוצע\nתכסית 40% מותרת [S1]."), ws, "?", [], cache=cache)
    assert again.reused == 0 and len(seen) == n + 1  # the same nearest heading, another chain above it
