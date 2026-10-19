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
from app.providers.llm import CallStatus, Purpose
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
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
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


# =====================================================================================================================
# U3: claim records with stable ids, the three planes, the dependency cascade and the causes of "not checked"
# =====================================================================================================================

def _salted(*texts: str) -> Workspace:
    """A workspace with a fixed per-message salt, so ids are the same in two verifications (R18, KTD2)."""
    ws = _ws(*texts)
    ws.claim_salt = "salt-a"
    return ws


def _mixed_judge(input: str) -> dict:
    """Judges every unit holding a rented-claim as mixed (the area supported, the lease not), the rest as supported."""
    return {"verdicts": [
        {"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
            {"span": SPAN_OK, "verdict": "supported"}, {"span": SPAN_BAD, "verdict": "unsupported"}]}
        if "מושכר" in t else {"index": i, "verdict": "supported", "reason": "x"} for i, t in _units(input).items()]}


def test_a_report_gives_every_claim_a_record_with_its_position_kind_citation_and_action():
    answer = _answer("# נתונים\n" + SENTENCE + "\nהנכס פנוי [S1].")
    report = verify_answer(_judge(_mixed_judge), answer, _salted(), "?", [])
    first, second = report.claims[1], report.claims[2]
    ok, bad = first
    assert (ok.text, ok.verdict, ok.action, ok.citation) == (SPAN_OK, "supported", "kept", "cited")
    # U4: the failing claim is the ladder's to cut (its span is replaced by a marker), no longer left pending
    assert (bad.text, bad.verdict, bad.action, bad.kind) == (SPAN_BAD, "unsupported", "cut", "datum")
    md = answer.answer_markdown
    assert md[ok.start:ok.end] == SPAN_OK and md[bad.start:bad.end] == SPAN_BAD  # offsets in the answer's markdown
    assert ok.unit_id == bad.unit_id != second[0].unit_id and ok.unit == bad.unit == 1
    assert len({ok.id, bad.id, second[0].id, ok.unit_id}) == 4
    assert ok.sources_checked == ["S1"] and not ok.depends_on and ok.fallback == ""
    # a whole-unit claim: the span is the unit's text, offsets include the citation the text drops
    whole = second[0]
    assert whole.whole_unit and md[whole.start:whole.end] == "הנכס פנוי [S1]."


AREA_SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. הנכס מושכר. הנכס פנוי."


def test_an_id_is_content_not_position_so_reordering_keeps_every_verdict_on_its_claim_id():
    def respond(input: str) -> dict:
        verdict = lambda t: "unsupported" if "מושכר" in t else "supported"  # noqa: E731
        return {"verdicts": [{"index": i, "verdict": verdict(t), "reason": "x"} for i, t in _units(input).items()]}

    one = "השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס מושכר [S1].\nהנכס פנוי [S1]."
    two = "הנכס פנוי [S1].\nהנכס מושכר [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1]."
    by_text = []
    for markdown in (one, two):
        report = verify_answer(_judge(respond), _answer(markdown), _salted(AREA_SOURCE), "?", [])
        by_text.append({r.text: (r.id, r.verdict) for rs in report.claims.values() for r in rs})
    assert by_text[0] == by_text[1] and by_text[0]["הנכס מושכר ."][1] == "unsupported"
    # the failing claim's problem carries the same id in both orders
    ids = []
    for markdown in (one, two):
        report = verify_answer(_judge(respond), _answer(markdown), _salted(AREA_SOURCE), "?", [])
        (removal,) = report.removals()
        ids.append(removal.claim_id)
    assert ids[0] == ids[1] == by_text[0]["הנכס מושכר ."][0]


def test_a_repeated_sentence_gets_an_id_per_occurrence_and_the_salt_changes_every_id():
    answer = _answer("הנכס פנוי [S1].\nהנכס פנוי [S1].")
    report = verify_answer(_judge(_supported), answer, _salted(), "?", [])
    a, b = report.claims[0][0], report.claims[1][0]
    assert a.id != b.id and a.unit_id != b.unit_id
    other = _ws()
    other.claim_salt = "salt-b"
    again = verify_answer(_judge(_supported), answer, other, "?", [])
    assert {a.id, b.id}.isdisjoint({again.claims[0][0].id, again.claims[1][0].id})  # ids do not reveal text
    # no random per-turn default is shared between workspaces
    assert _ws().claim_salt != _ws().claim_salt


def test_the_same_text_under_another_heading_or_table_header_is_another_claim_but_a_server_span_is_not_part_of_it():
    def ids_of(markdown: str, server: bool = False) -> list[str]:
        units = verify.split_units(markdown)
        if server:
            for u in units:
                u.server = ["[חלק שלא אומת הושמט]"]
        verify.assign_ids(units, "s")
        return [u.id for u in units]

    base = ids_of("## מצב מוצע\nתכסית 40% מותרת [S1].")[1]
    assert ids_of("## מצב מאושר\nתכסית 40% מותרת [S1].")[1] != base
    assert ids_of("## מצב מוצע\nתכסית 40% מותרת [S1].")[1] == base
    # a registered server span inside the unit's text is left out of the id: an edit never renames the model's claim
    unit = verify.split_units("## מצב מוצע\nתכסית 40% מותרת [חלק שלא אומת הושמט] [S1].")
    unit[1].server = ["[חלק שלא אומת הושמט]"]
    verify.assign_ids(unit, "s")
    assert unit[1].id == base
    tables = ["| נכס | שטח |\n| --- | --- |\n| א | 40 |", "| נכס | תכסית |\n| --- | --- |\n| א | 40 |"]
    assert ids_of(tables[0])[1] != ids_of(tables[1])[1]


def test_a_cut_only_report_counts_no_partial_unit_though_the_answer_is_partial():
    """A cut resolves its partial problem (the text carries a marker, no partial mark): ``counts`` leaves it out of
    ``partial`` exactly as ``edit`` does, while the answer is still ``partial`` (a cut is a loss)."""
    report = verify_answer(_judge(_mixed_judge), _answer("תכסית 40% מותרת, והנכס מושכר [S1]."), _salted(), "?", [])
    assert [p.resolution for p in report.problems if p.severity == "partial"] == ["cut"]
    counts = report.counts()
    assert counts["actions"]["cut"] == 1 and counts["removed"] == 0
    assert counts["partial"] == 0 and counts["correctness"] == "partial"


def test_a_claim_changed_by_a_repair_gets_a_new_id_with_the_parent_it_replaced():
    cache, ws = VerdictCache(), _salted()
    judge = _judge(_mixed_judge)
    first = verify_answer(judge, _answer("תכסית 40% מותרת, והנכס מושכר [S1]."), ws, "?", [], cache=cache)
    old = first.claims[0][1]
    # the repair rewrote the failing clause; the supported clause is unchanged (same id, no parent)
    second = verify_answer(_judge(_supported), _answer("תכסית 40% מותרת, והנכס מושכר ללא חוזה [S1]."), ws, "?", [],
                           cache=cache)
    (new,) = second.claims[0]
    assert new.id != old.id and new.parent_id in {c.id for c in first.claims[0]}
    kept = verify_answer(judge, _answer("תכסית 40% מותרת, והנכס מושכר [S1]."), ws, "?", [], cache=cache)
    assert [c.id for c in kept.claims[0]] == [c.id for c in first.claims[0]]  # a claim that stands keeps its id
    assert kept.claims[0][0].parent_id == ""


def test_a_rewritten_claim_keeps_its_draft_and_is_logged_as_rewritten_in_the_texts_order():
    """Round 8 U7 (R22, KTD8): the log's original text for a claim a repair replaced is the earliest version, and the
    counts say how many claims each action touched."""
    cache, ws = VerdictCache(), _salted()
    first = verify_answer(_judge(_mixed_judge), _answer("תכסית 40% מותרת, והנכס מושכר [S1]."), ws, "?", [], cache=cache)
    assert first.counts()["actions"] == {"removed": 0, "cut": 1, "rewritten": 0, "withheld": 0, "qualified": 0,
                                         "cited": 0}
    second = verify_answer(_judge(_supported), _answer("תכסית 40% מותרת, והנכס מושכר ללא חוזה [S1]."), ws, "?", [],
                           cache=cache)
    (row,) = second.logged()
    assert row.action == "rewritten" and row.claim_id == second.claims[0][0].id
    assert row.record.draft == SPAN_BAD and row.record.text != SPAN_BAD
    counts = second.counts()
    assert counts["actions"]["rewritten"] == 1 and counts["removals"] == []  # a rewrite takes nothing out of the text


def test_the_handled_claims_are_logged_in_the_texts_order_whatever_their_action():
    def respond(input: str) -> dict:
        out = []
        for i, t in _units(input).items():
            if "מושכר" in t:
                out.append({"index": i, "verdict": "partial", "reason": "x", "defect": "part", "claims": [
                    {"span": SPAN_OK, "verdict": "supported"}, {"span": SPAN_BAD, "verdict": "unsupported"}]})
            else:
                out.append({"index": i, "verdict": "unsupported" if "פנוי" in t else "supported", "reason": "y"})
        return {"verdicts": out}

    md = "הנכס פנוי [S1].\n" + SENTENCE
    report = verify_answer(_judge(respond), _answer(md), _salted(), "?", [])
    assert [(r.action, r.position) for r in report.logged()] == [("removed", 0), ("cut", md.index(SPAN_BAD))]
    assert [r["action"] for r in report.counts()["removals"]] == ["removed", "cut"]


def test_a_problem_carries_the_id_of_the_failing_claim_and_reports_it_in_diagnostics():
    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "unsupported", "reason": "x"} for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer(SENTENCE), _salted(), "?", [])
    (p,) = report.problems
    assert p.claim_id == report.claims[0][0].id and p.as_dict()["claim_id"] == p.claim_id
    # a deterministic failure (a number the source never states) is a claim record too, with the same link
    report = verify_answer(_judge(_supported), _answer("גובה הבניין 99 מטר [S1]."), _salted(), "?", [])
    (p,) = report.removals()
    (record,) = report.claims[0]
    assert (p.claim_id, record.id, record.verdict) == (record.id, p.claim_id, "unsupported")
    # U4: its unit goes whole (the ladder's last resort), and the record says so
    assert record.action == "removed" and p.as_dict()["claim_id"] == record.id


def test_each_failing_claim_of_a_mixed_unit_is_its_own_problem_with_its_claim_id_and_the_unit_is_kept():
    report = verify_answer(_judge(_mixed_judge), _answer(SENTENCE), _salted(), "?", [])
    ok, bad = report.claims[0]
    (p,) = report.problems
    assert p.claim_id == bad.id and p.severity == "partial" and not report.removed_units()
    assert SPAN_BAD in p.reason


# --- citation repair and the instruction plane (R2, KTD3) ----------------------------------------------------------

VALUE = "השווי למ\"ר הוא 9,500 ₪."
RENT = "נספח השכרה: דמי השכירות הם 55 ₪ למ\"ר לחודש."
UNRELATED = "סעיף אחר: החניה בנכס מקורה ויש בו מעלית."


def _shown(input: str) -> dict[str, str]:
    return {sid: body for sid, body in re.findall(r'<source id="([^"]+)"[^>]*>\n(.*?)\n</source>', input, re.S)}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*\d|\d", text))


def _citing_judge(seen: list[str] | None = None) -> ScriptedProvider:
    """What the real judge does with rule 9: a cited unit is supported; an uncited one by a shown source that states
    its numbers (named in ``supported_by``), else it is unsupported as not linked to a read source."""
    def respond(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        shown, out = _shown(input), []
        for i, text in _units(input).items():
            cites = re.search(rf'<unit index="{i}" cites="([^"]*)"', input).group(1)
            by = [sid for sid, body in shown.items() if _numbers(text) and _numbers(text) <= _numbers(body)]
            if cites:
                out.append({"index": i, "verdict": "supported", "reason": "x"})
            elif by:
                out.append({"index": i, "verdict": "supported", "reason": "x", "supported_by": by})
            else:
                out.append({"index": i, "verdict": "unsupported", "reason": verify.NOT_LINKED_REASON,
                            "failure": "absent_from_source"})
        return {"verdicts": out}

    return _judge(respond)


def test_ae2_an_uncited_datum_a_read_source_supports_gets_its_citation_and_nothing_is_removed():
    seen: list[str] = []
    answer = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש.")
    report = verify_answer(_citing_judge(seen), answer, _salted(VALUE, RENT, UNRELATED), "?", [])
    assert not [p for p in report.problems if p.removes_unit] and report.counts()["removed"] == 0
    (record,) = report.claims[1]
    # U4: the server attaches the citation, and the record's action says it was cited
    assert (record.verdict, record.citation, record.action) == ("supported", "repaired", "cited")
    assert "[S2]" in report.apply(answer).answer_markdown
    # the judge was shown the read source that holds the unit's number, and not the one that holds nothing of it
    assert "S2" in _shown(seen[0]) and "S3" not in _shown(seen[0])
    # the instruction plane records the repair, per claim
    cites = {c["claim_id"]: c["state"] for c in report.instructions["citations"]}
    assert cites[record.id] == "repaired" and cites[report.claims[0][0].id] == "cited"


def test_an_uncited_datum_no_read_source_supports_is_not_linked_and_never_attributed_outside():
    answer = _answer("החניה בנכס מקורה.")
    report = verify_answer(_citing_judge(), answer, _salted(VALUE, RENT), "?", [])
    (removal,) = report.removals()
    assert removal.failure_kind == "absent_from_source" and verify.NOT_LINKED_REASON in removal.reason
    (record,) = report.claims[0]
    assert (record.verdict, record.citation, record.failure) == ("unsupported", "missing", "absent_from_source")
    assert "מקור חיצוני" not in removal.reason and report.instructions["citations"][0]["state"] == "missing"


def test_an_uncited_number_no_read_source_states_is_not_linked_either_before_the_judge_is_asked():
    report = verify_answer(_citing_judge(), _answer("דמי החניה הם 777 ₪ לחודש."), _salted(VALUE, RENT), "?", [])
    (removal,) = report.removals()
    assert (removal.failure_kind, removal.check) == ("absent_from_source", "unstated_number")
    assert verify.NOT_LINKED_REASON in removal.reason and "חיצוני" not in removal.reason
    assert report.claims[0][0].citation == "missing" and report.claims[0][0].verdict == "unsupported"


def test_the_reason_of_an_uncited_unsupported_claim_always_says_it_is_not_linked_to_a_read_source():
    def respond(input: str) -> dict:  # the judge forgot the phrase
        return {"verdicts": [{"index": i, "verdict": "unsupported", "reason": "אין תמיכה", "failure": "absent_from_source"}
                             for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer("החניה בנכס מקורה."), _salted(), "?", [])
    assert verify.NOT_LINKED_REASON in report.removals()[0].reason


def test_candidate_sources_for_an_uncited_unit_are_bounded_in_count_and_size(monkeypatch):
    big = "נספח: דמי השכירות הם 55 ₪ למ\"ר לחודש. " + "מילה " * 3000
    ws = _salted(*[big + str(n) for n in range(8)])
    unit = verify.split_units("דמי השכירות הם 55 ₪ למ\"ר לחודש.")
    rendered = verify._render_batch(verify._Batch(unit), ws)
    shown = _shown(rendered)
    assert 0 < len(shown) <= verify.CANDIDATE_SOURCES
    assert len(rendered) <= verify.JUDGE_CALL_CHARS
    assert sum(len(b) for b in shown.values()) <= verify.CANDIDATE_CHARS
    # a source that holds neither a number nor a content word of the unit is not a candidate
    assert verify._candidate_ids(verify._Batch(verify.split_units("דמי הניהול הם 12 ₪.")), _salted(UNRELATED)) == {}
    # a cited unit gets no candidates: it is judged against what it cites
    cited = verify.split_units("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    assert verify._candidate_ids(verify._Batch(cited), ws) == {}


def test_a_source_named_but_not_shown_is_still_refused_and_a_candidate_is_accepted():
    unit_batch = verify._Batch(verify.split_units("דמי השכירות הם 55 ₪ למ\"ר לחודש."))
    ws = _salted(VALUE, RENT)
    v = JudgeVerdict.model_validate({"index": 0, "verdict": "supported", "reason": "x", "supported_by": ["S2"]})
    assert verify._shown_support({0: v}, unit_batch, ws)[0].verdict == "supported"  # S2 holds 55: a shown candidate
    v = JudgeVerdict.model_validate({"index": 0, "verdict": "supported", "reason": "x", "supported_by": ["S1"]})
    assert verify._shown_support({0: v}, unit_batch, ws)[0].verdict == "unsupported"  # S1 was not shown for it


def test_a_cited_number_the_named_candidate_does_not_hold_is_not_attached():
    def respond(input: str) -> dict:  # names S1 for a number it does not hold
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "x", "supported_by": ["S1"]}
                             for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer("השווי למ\"ר הוא 9,500 ₪."), _salted(VALUE, "השווי הוא 9,500 ₪ והחניה 4."),
                           "?", [])
    assert [p.kind for p in report.problems] in ([], ["needs_citation"])


# --- identical numbers in different contexts (R7) --------------------------------------------------------------------

CONTEXT_SOURCE = "מצב מוצע: תכסית 500 מ\"ר, טרם אושרה. מצב מאושר: תכסית 300 מ\"ר."


def _heading_aware(input: str) -> dict:
    """The scripted judge reads the rendered heading: a datum under a "proposed" heading stated as approved contradicts."""
    out = []
    for i, text in _units(input).items():
        context = re.search(rf'<unit_context unit="{i}">(.*?)</unit_context>', input, re.S)
        proposed = bool(context and "מוצע" in context.group(1))
        claims_approved = "מאושר" in text
        verdict = "contradicts" if proposed and claims_approved else "supported"
        out.append({"index": i, "verdict": verdict, "reason": "x"})
    return {"verdicts": out}


def test_the_same_number_under_a_proposed_heading_stated_as_approved_contradicts_and_in_its_context_is_supported():
    wrong = "## מצב מוצע\nהתכסית המאושרת היא 500 מ\"ר [S1]."
    right = "## מצב מוצע\nהתכסית המוצעת היא 500 מ\"ר [S1].\n## מצב מאושר\nהתכסית המאושרת היא 300 מ\"ר [S1]."
    bad = verify_answer(_judge(_heading_aware), _answer(wrong), _salted(CONTEXT_SOURCE), "?", [])
    (record,) = bad.claims[1]
    assert (record.verdict, record.failure) == ("contradicts", "contradicts_source")
    assert bad.removals()[0].failure_kind == "contradicts_source" and bad.removals()[0].claim_id == record.id
    good = verify_answer(_judge(_heading_aware), _answer(right), _salted(CONTEXT_SOURCE), "?", [])
    assert [r.verdict for u in (1, 3) for r in good.claims[u]] == ["supported", "supported"] and good.ok


# --- the dependency cascade at claim level (R11, AE5) ---------------------------------------------------------------

def test_ae5_a_removed_input_claim_cascades_to_the_inference_inside_its_unit_and_a_computation_over_workspace_values_stays():
    from tests.unit.test_coverage import _computed, _value

    ws = _salted("סיכום: שטח הנכס 1,200 מ\"ר. הסף הוא 1,000 מ\"ר. סה\"כ הכנסות 9,500 ₪, סה\"כ עלויות 8,000 ₪.")  # area 1,200
    v1, v2 = _value(ws, "9,500", "סה\"כ הכנסות", "income"), _value(ws, "8,000", "סה\"כ עלויות", "cost")
    cid = _computed(ws, f"{v1} - {v2}", "הרווח")
    mixed = "שטח הנכס הוא 1,000 מ\"ר, ולכן הוא חורג מהסף [S1]."
    answer = _answer(f"{mixed}\nהרווח הוא 1,500 ₪ [{cid}].")

    def respond(input: str) -> dict:
        out = []
        for i, text in _units(input).items():
            if "ולכן" in text:
                out.append({"index": i, "verdict": "partial", "reason": "x", "defect": "part", "claims": [
                    {"span": "שטח הנכס הוא 1,000 מ\"ר", "verdict": "contradicts"},
                    {"span": "ולכן הוא חורג מהסף", "verdict": "supported", "kind": "inference",
                     "depends_on": ["שטח הנכס הוא 1,000 מ\"ר"]}]})
            else:
                out.append({"index": i, "verdict": "supported", "reason": "x"})
        return {"verdicts": out}

    report = verify_answer(_judge(respond), answer, ws, "?", [])
    datum, inference = report.claims[0]
    assert (datum.verdict, inference.verdict) == ("contradicts", "unsupported")  # the inference lost its basis
    assert inference.basis == datum.id and inference.depends_on == [datum.id]
    # both claims of the unit fail: the unit goes, and the computation (it cites the C#, not the removed claim) stays
    assert report.removed_units() == {0}
    (removal,) = report.removals()
    assert removal.failure_kind == "contradicts_source"
    computed = report.claims[1][0]
    assert computed.kind == "computed" and computed.verdict == "supported" and computed.action == "kept"
    dependent = [p for p in report.problems if p.check == "dependency"]
    assert dependent and dependent[0].claim_id == inference.id


def test_a_dependent_claim_in_a_unit_with_a_supported_claim_marks_the_unit_and_keeps_it():
    sentence = "שטח הנכס הוא 1,000 מ\"ר, ולכן חורג מהסף, והנכס פנוי [S1]."

    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "x", "defect": "part", "claims": [
            {"span": "שטח הנכס הוא 1,000 מ\"ר", "verdict": "contradicts"},
            {"span": "ולכן חורג מהסף", "verdict": "supported", "kind": "inference",
             "depends_on": ["שטח הנכס הוא 1,000 מ\"ר"]},
            {"span": "והנכס פנוי", "verdict": "supported"}]} for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer(sentence), _salted("שטח הנכס 1,200 מ\"ר. הסף 1,000 מ\"ר. הנכס פנוי."),
                           "?", [])
    states = [(r.text, r.verdict) for r in report.claims[0]]
    assert states == [("שטח הנכס הוא 1,000 מ\"ר", "contradicts"), ("ולכן חורג מהסף", "unsupported"),
                      ("והנכס פנוי", "supported")]
    assert not report.removed_units() and {p.claim_id for p in report.problems} == {
        report.claims[0][0].id, report.claims[0][1].id}


def test_a_dependency_on_a_unit_that_is_kept_removes_nothing_at_claim_level_either():
    sentence = "שטח הנכס הוא 1,200 מ\"ר, ולכן חורג מהסף [S1]."

    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "x", "claims": [
            {"span": "שטח הנכס הוא 1,200 מ\"ר", "verdict": "supported"},
            {"span": "ולכן חורג מהסף", "verdict": "supported", "kind": "inference",
             "depends_on": ["שטח הנכס הוא 1,200 מ\"ר"]}]} for i in _units(input)]}

    report = verify_answer(_judge(respond), _answer(sentence), _salted("שטח הנכס 1,200 מ\"ר."), "?", [])
    assert report.ok and [r.verdict for r in report.claims[0]] == ["supported", "supported"]


# --- "not checked" has three causes here (and a fourth, the final check's time limit), none a contradiction (R6, R14, AE3) ---------------------------------------------

def test_the_three_causes_of_not_checked_are_recorded_and_each_has_its_own_sentence(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)

    def respond(input: str):
        texts = _units(input)
        if any("דמי השכירות הם 55" in t for t in texts.values()):
            return CallStatus.TIMEOUT
        out = []
        for i, t in texts.items():
            if t.startswith("הנכס פנוי"):
                continue
            out.append({"index": i, "verdict": "not_factual" if "כך נראה" in t else "supported", "reason": "x"})
        return {"verdicts": out}

    markdown = ("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס פנוי [S1].\n"
                "דמי השכירות 55 ₪ הם, כך נראה, נתון חשוב [S1].")
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: respond(input), repeat=True)
    report = verify_answer(p, _answer(markdown), _salted("סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש."),
                           "?", [])
    removals = {r.unit.text.rstrip(" ."): r for r in report.removals()}
    causes = {t: r.cause for t, r in removals.items()}
    assert sorted(causes.values()) == ["check_failed", "not_classified", "omitted"], causes
    assert all(r.failure_kind == "not_checked" for r in removals.values())
    sentences = {r.summary()["text"] for r in removals.values()}
    assert len(sentences) == 3 and all(s in verify.CHECK_CAUSE_SENTENCES.values() for s in sentences)
    # ``time_limit`` is the final check's own cause (round 9 R19): the verification has the other three
    assert {r.summary()["cause"] for r in removals.values()} == set(verify.NOT_CHECKED_CAUSES) - {"time_limit"}
    # their records say "unchecked", never contradicts or unsupported
    verdicts = {r.text: r.verdict for rs in report.claims.values() for r in rs if r.cause}
    assert set(verdicts.values()) == {"unchecked"} and len(verdicts) == 3


def test_a_claim_resting_on_a_removed_unit_by_its_index_is_removed_with_that_units_failure_kind():
    answer = _answer("שטח הנכס הוא 1,000 מ\"ר [S1].\nהסף נחצה, כלומר הנכס גדול [S1].")

    def respond(input: str) -> dict:
        return {"verdicts": [
            {"index": 0, "verdict": "contradicts", "reason": "x"},
            {"index": 1, "verdict": "supported", "reason": "x", "claims": [
                {"span": "הסף נחצה, כלומר הנכס גדול", "verdict": "supported", "kind": "inference",
                 "depends_on": ["0"]}]}]}

    report = verify_answer(_judge(respond), answer, _salted("שטח הנכס 1,200 מ\"ר. הסף 1,000 מ\"ר. הנכס גדול, הסף נחצה."),
                           "?", [])
    assert report.removed_units() == {0, 1}
    dependent = next(r for r in report.removals() if r.unit.index == 1)
    assert (dependent.check, dependent.failure_kind) == ("dependency", "contradicts_source")
    inference = report.claims[1][0]
    assert inference.verdict == "unsupported" and inference.basis == report.claims[0][0].id
    assert inference.depends_on == [report.claims[0][0].id]
