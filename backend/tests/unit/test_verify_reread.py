"""The bounded re-read of round 8 (U5, KTD5; R13-R15): a claim the judge could only call partial or unsupported for want
of context (``needs_context``) gets one expansion of its cited source and a re-judge before it is given up. The
expansion comes through an injected ``ContextFetcher`` (a fake here: no database), at most ``chat_reread_claims``
claims per turn and only within the verify time allowance. A technical failure of the re-read leaves the claim as it
was, never a contradiction. The judge is scripted: these tests prove what the server fetches, what the judge is shown,
what it does with the verdict, and the bounds, not the judge's judgement. Synthetic texts only."""

from __future__ import annotations

import re
import time
import uuid
from typing import get_args

import pytest

from app.chat import verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import Expansion, VerdictCache, verify_answer
from app.config import get_settings
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider

UNITS_NOTE = "הטבלה מוצגת באלפי ₪"


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _ws(*texts: str, kind: str = "context", **kw) -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind=kind, text=t, **kw)
    return ws


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _partial(index: int, text: str, needs: str, reason: str = "חסר הקשר") -> dict:
    """A partial verdict whose one claim (the whole unit) needs ``needs``."""
    return {"index": index, "verdict": "partial", "reason": reason,
            "claims": [{"span": text, "verdict": "partial", "needs_context": needs}]}


def _supported(index: int, reason: str = "בדיקה") -> dict:
    return {"index": index, "verdict": "supported", "reason": reason}


def _judge(needs: str, shows: str, seen: list[str] | None = None) -> ScriptedProvider:
    """A judge that finds every unit partial (needing ``needs``) until its input shows ``shows``, then supported."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        if shows in input:
            return {"verdicts": [_supported(i) for i in _units(input)]}
        return {"verdicts": [_partial(i, t, needs) for i, t in _units(input).items()]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


class FakeFetcher:
    """What the DB fetcher does, offline: expansions by (source id, kind), or a failure."""

    def __init__(self, expansions: dict | None = None, fail: bool = False, ws: Workspace | None = None):
        self.expansions = expansions or {}
        self.fail = fail
        self.ws = ws  # with it, ``adopt`` registers the expansion as a turn source, as the DB fetcher does
        self.calls: list[tuple[str, str]] = []
        self.adopted: list[tuple[str, str]] = []

    def expand(self, sid: str, kind: str):
        self.calls.append((sid, kind))
        if self.fail:
            raise RuntimeError("db down")
        return self.expansions.get((sid, kind))

    def adopt(self, sid: str, kind: str) -> str | None:
        self.adopted.append((sid, kind))
        if self.ws is None:
            return "S9"
        got = self.expansions[(sid, kind)]
        src = self.ws.sources[sid]
        return self.ws.add_source(document_id=src.document_id, version_id=src.version_id, title=src.title,
                                  section=src.section, location=src.location + " — המשך", kind=src.kind,
                                  text=got.text if isinstance(got, Expansion) else got,
                                  block_start=src.block_start, block_end=src.block_end).sid


def _purposes(usage: list[dict]) -> list[str]:
    return [u["purpose"] for u in usage]


def _rereads(report) -> list[dict]:
    return [x for recs in report.claims.values() for r in recs for x in r.rereads]


# --- the three contexts the plan names ----------------------------------------------------------------------------

def test_a_units_note_in_the_heading_above_makes_a_partial_claim_supported():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    seen: list[str] = []
    fetcher = FakeFetcher({("S1", "units_note"): Expansion(f"כותרת: ## נכסים\n{UNITS_NOTE}", "כותרות")})
    usage: list = []
    md = "סך כל הנכסים הוא 25,742 [S1]."
    report = verify_answer(_judge("units_note", UNITS_NOTE, seen), _answer(md), ws, "?", usage, reread=fetcher)
    assert report.ok and not report.problems
    assert fetcher.calls == [("S1", "units_note")]
    (rec,) = report.claims[0]
    assert rec.verdict == "supported" and rec.action == "kept"
    (log,) = rec.rereads
    assert (log["source"], log["scope"], log["outcome"]) == ("S1", "units_note", "supported")
    # the first call did not show the note; the re-judge showed it, inside the very source it expands
    assert len(seen) == 2 and UNITS_NOTE not in seen[0] and UNITS_NOTE in seen[1]
    assert re.search(r'<source id="S1"[^>]*>.*<expanded_context kind="units_note">', seen[1], re.S)
    assert report.verdicts == {0: "supported"}
    assert _purposes(usage) == ["verify", "verify_reread"]


def test_a_plain_string_expansion_is_taken_as_its_text():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", [],
                           reread=fetcher)
    assert report.ok and report.claims[0][0].verdict == "supported"


def test_a_total_on_the_next_page_is_supported_after_the_continuation():
    ws = _ws("עמוד 1: שורה א | 100\nשורה ב | 200\nסיכום ביניים | 300 (המשך בעמוד הבא; סה\"כ 600)", kind="table",
             table_index=0)
    fetcher = FakeFetcher({("S1", "next_page"): Expansion("עמוד 2: שורה ג | 300\nסה\"כ | 600", "המשך הטבלה")})
    usage: list = []
    md = "הסה\"כ בטבלה, בכל עמודיה, הוא 600 [S1]."
    report = verify_answer(_judge("next_page", "סה\"כ | 600"), _answer(md), ws, "?", usage, reread=fetcher)
    assert report.ok and report.claims[0][0].verdict == "supported"
    assert report.claims[0][0].rereads[0]["scope"] == "next_page"
    assert _purposes(usage).count("verify_reread") == 1


def test_a_vision_table_cell_is_supported_from_its_stored_reading():
    ws = _ws("אזור בעמוד 4 (קריאה חזותית)\nשכירות 55", kind="image", method="vision")
    reading = "שורה | ערך\nשכירות | 55"
    fetcher = FakeFetcher({("S1", "region"): Expansion(reading, "קריאה שמורה של האזור")})
    md = "השכירות בטבלה היא 55 [S1]."
    report = verify_answer(_judge("region", reading), _answer(md), ws, "?", [], reread=fetcher)
    assert report.ok and report.claims[0][0].verdict == "supported"
    assert fetcher.calls == [("S1", "region")]


def test_a_supported_claims_expansion_is_registered_as_a_turn_source():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", [],
                           reread=fetcher)
    assert fetcher.adopted == [("S1", "units_note")]
    assert report.claims[0][0].rereads[0]["expanded_source"] == "S9"


def test_a_claim_that_stays_partial_after_the_re_read_is_not_registered():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): "טקסט לא קשור"})
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", [],
                           reread=fetcher)
    assert fetcher.adopted == []
    (rec,) = report.claims[0]
    assert rec.verdict == "partial" and rec.rereads[0]["outcome"] == "partial"
    assert [p.severity for p in report.problems] == ["partial"]  # given up as before, after the one re-read


# --- which claims are re-read --------------------------------------------------------------------------------------

def test_nothing_is_re_read_without_a_fetcher():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    usage: list = []
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage)
    assert report.claims[0][0].verdict == "partial" and _purposes(usage) == ["verify"]


def test_a_claim_that_needs_no_context_is_not_re_read():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    p = ScriptedProvider().on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": n, "verdict": "unsupported", "reason": "אין", "failure": "absent_from_source"}
        for n in _units(input)]}, repeat=True)
    fetcher, usage = FakeFetcher(), []
    verify_answer(p, _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage, reread=fetcher)
    assert fetcher.calls == [] and _purposes(usage) == ["verify"]


def test_a_contradiction_or_a_supported_claim_is_not_re_read():
    ws = _ws("סיכום: סך כל הנכסים 25,742. השכירות 55.")
    p = ScriptedProvider().on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": 0, "verdict": "contradicts", "reason": "אחר", "claims": [
            {"span": "סך כל הנכסים הוא 25,742", "verdict": "contradicts", "needs_context": "units_note"}]},
        {"index": 1, "verdict": "supported", "reason": "ok", "claims": [
            {"span": "השכירות היא 55", "verdict": "supported", "needs_context": "heading"}]}]}, repeat=True)
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE, ("S1", "heading"): "כותרת"})
    verify_answer(p, _answer("סך כל הנכסים הוא 25,742 [S1].\n\nהשכירות היא 55 [S1]."), ws, "?", [], reread=fetcher)
    assert fetcher.calls == []


def test_an_uncited_claim_has_no_source_to_re_read():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742."), ws, "?", [],
                           reread=fetcher)
    assert fetcher.calls == []
    assert not any(r.rereads for recs in report.claims.values() for r in recs)


def test_a_claim_the_check_could_not_complete_is_never_re_read():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    p = ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    fetcher, usage = FakeFetcher({("S1", "units_note"): UNITS_NOTE}), []
    with pytest.raises(verify.VerificationUnavailable):
        verify_answer(p, _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage, reread=fetcher)
    assert fetcher.calls == [] and "verify_reread" not in _purposes(usage)


def test_a_table_kind_picks_the_cited_table_not_the_first_source():
    ws = _ws("פסקה: הערה כללית.")
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח", section="נספח", location="טבלה",
                  kind="table", text="שורה | 100", table_index=2)
    fetcher = FakeFetcher({("S2", "table_headers"): "כותרות: ערך באלפי ₪"})
    md = "השורה היא 100 [S1][S2]."
    verify_answer(_judge("table_headers", "כותרות: ערך"), _answer(md), ws, "?", [], reread=fetcher)
    assert fetcher.calls == [("S2", "table_headers")]


# --- the bounds ----------------------------------------------------------------------------------------------------

def _five() -> tuple[Workspace, str, FakeFetcher]:
    ws = _ws("סיכום: א 11. ב 22. ג 33. ד 44. ה 55.")
    names = "אבגדה"
    md = "\n\n".join(f"הערך {n} הוא {11 * (i + 1)} [S1]." for i, n in enumerate(names))
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    return ws, md, fetcher


def test_a_fifth_eligible_claim_is_not_re_read_and_keeps_its_verdict_with_its_reason():
    assert get_settings().chat_reread_claims == 4
    ws, md, fetcher = _five()
    seen: list[str] = []
    usage: list = []

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        shown = UNITS_NOTE in input
        return {"verdicts": [_supported(i) if shown else _partial(i, t, "units_note") for i, t in _units(input).items()]}

    p = ScriptedProvider().on(Purpose.VERIFY, respond, repeat=True)
    report = verify_answer(p, _answer(md), ws, "?", usage, reread=fetcher)
    assert len(fetcher.calls) == 4 and _purposes(usage) == ["verify", "verify_reread"]
    assert len(_units(seen[1])) == 4 and 4 not in _units(seen[1])  # one call, the first four claims' units only
    verdicts = [recs[0].verdict for _, recs in sorted(report.claims.items())]
    assert verdicts == ["supported"] * 4 + ["partial"]
    last = report.claims[4][0]
    assert last.rereads[0]["outcome"] == "bound" and last.rereads[0]["reason"] == "source_read_in_part"
    (problem,) = report.problems  # the fifth is given up as it was: partial, saying the source was read in part
    assert problem.unit.index == 4 and verify.READ_IN_PART in problem.reason


def test_the_bound_is_per_turn_not_per_verification():
    ws, md, fetcher = _five()
    judge = _judge("units_note", UNITS_NOTE)
    units = md.split("\n\n")
    verify_answer(judge, _answer("\n\n".join(units[:3])), ws, "?", [], reread=fetcher)
    assert len(fetcher.calls) == 3
    # a repair round of the same turn: the third claim again (expanded already) and two new ones, with one expansion
    # left of the four
    report = verify_answer(judge, _answer("\n\n".join(units[2:])), ws, "?", [], reread=fetcher)
    assert len(fetcher.calls) == 4
    assert [recs[0].verdict for _, recs in sorted(report.claims.items())] == ["partial", "supported", "partial"]
    assert report.claims[2][0].rereads[0]["outcome"] == "bound"


def test_a_claim_is_expanded_once_in_the_turn():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): "טקסט לא קשור"})
    cache = VerdictCache()
    judge = _judge("units_note", UNITS_NOTE)
    md = _answer("סך כל הנכסים הוא 25,742 [S1].")
    verify_answer(judge, md, ws, "?", [], reread=fetcher, cache=cache)
    report = verify_answer(judge, md, ws, "?", [], reread=fetcher, cache=cache)  # the next round, same claim
    assert fetcher.calls == [("S1", "units_note")]
    assert report.claims[0][0].rereads  # the log follows the claim into the next round


def test_a_supported_re_read_verdict_is_reused_by_the_next_round():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    cache, usage = VerdictCache(), []
    judge = _judge("units_note", UNITS_NOTE)
    md = _answer("סך כל הנכסים הוא 25,742 [S1].")
    verify_answer(judge, md, ws, "?", usage, reread=fetcher, cache=cache)
    report = verify_answer(judge, md, ws, "?", usage, reread=fetcher, cache=cache)
    assert report.ok and report.claims[0][0].verdict == "supported" and report.reused == 1
    assert _purposes(usage) == ["verify", "verify_reread"]  # the second round costs no call


def test_no_re_read_starts_without_time():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    usage: list = []
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage,
                           deadline=time.monotonic() + verify.REREAD_MIN_SECONDS / 2, reread=fetcher)
    assert fetcher.calls == [] and _purposes(usage) == ["verify"]
    (rec,) = report.claims[0]
    assert rec.verdict == "partial" and rec.rereads[0]["outcome"] == "no_time"


# --- a failure of the re-read is never a contradiction -------------------------------------------------------------

def test_a_failing_fetch_leaves_the_claim_as_it_was():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    usage: list = []
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage,
                           reread=FakeFetcher(fail=True))
    (rec,) = report.claims[0]
    assert rec.verdict == "partial" and rec.rereads[0]["outcome"] == "check_failed"
    assert _purposes(usage) == ["verify"]  # nothing to show the judge
    assert all(p.failure_kind != "contradicts_source" for p in report.problems)


def test_nothing_more_to_read_is_recorded_and_costs_no_call():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    usage: list = []
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage,
                           reread=FakeFetcher())
    assert report.claims[0][0].rereads[0]["outcome"] == "no_context" and _purposes(usage) == ["verify"]


@pytest.mark.parametrize("status", [CallStatus.TIMEOUT, CallStatus.INVALID, CallStatus.INCOMPLETE])
def test_a_failing_re_judge_leaves_the_claim_as_it_was_never_a_contradiction(status):
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [_partial(n, t, "units_note") for n, t in _units(input).items()]})
    p.on(Purpose.VERIFY, status, repeat=True)  # the second call, and its retry
    usage: list = []
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    report = verify_answer(p, _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", usage, reread=fetcher)
    (rec,) = report.claims[0]
    assert rec.verdict == "partial" and rec.rereads[0]["outcome"] == "check_failed"
    assert report.verdicts == {0: "partial"}
    assert all(p_.failure_kind != "contradicts_source" for p_ in report.problems)
    assert fetcher.adopted == []
    # a retryable failure is retried once, as every judge call is; a truncated reply is not resent
    assert _purposes(usage).count("verify_reread") == (1 if status is CallStatus.INCOMPLETE else 2)


def test_a_re_judge_that_leaves_the_unit_out_changes_nothing():
    ws = _ws("סיכום: סך כל הנכסים 25,742.")
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [_partial(n, t, "units_note") for n, t in _units(input).items()]})
    p.on(Purpose.VERIFY, {"verdicts": []})
    report = verify_answer(p, _answer("סך כל הנכסים הוא 25,742 [S1]."), ws, "?", [],
                           reread=FakeFetcher({("S1", "units_note"): UNITS_NOTE}))
    assert report.claims[0][0].verdict == "partial" and report.claims[0][0].rereads[0]["outcome"] == "check_failed"


def test_the_re_judge_is_not_asked_for_more_context_by_policy():
    for word in ("expanded_context", "reread", "needs_context"):
        assert word in verify.JUDGE_REREAD_POLICY, word


# --- a number only the next part of the source states (the deterministic check) ------------------------------------

PAGE_1 = "עמוד 1: שורה א | 100\nשורה ב | 200"
PAGE_2 = "עמוד 2: שורה ג | 300\nסה\"כ | 600"


def _table_ws() -> Workspace:
    return _ws(PAGE_1, kind="table", table_index=0, block_start=2, block_end=2)


def _all_supported() -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [_supported(n) for n in _units(input)]}, repeat=True)
    return p


def test_a_total_stated_only_on_the_next_page_is_kept_and_cited_to_the_expansion():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): Expansion(PAGE_2, "המשך הטבלה")}, ws=ws)
    usage: list = []
    md = "הסה\"כ בטבלה הוא 600 [S1]."
    report = verify_answer(_all_supported(), _answer(md), ws, "?", usage, reread=fetcher)
    assert report.ok and not report.removed_units()
    assert fetcher.calls == [("S1", "next_page")] and fetcher.adopted == [("S1", "next_page")]
    assert [p.cite for p in report.problems if p.kind == "needs_citation"] == ["S2"]
    assert "[S1][S2]" in report.apply(_answer(md)).answer_markdown
    assert PAGE_2 in ws.sources["S2"].text
    (rec,) = report.claims[0]
    assert rec.verdict == "supported" and rec.citation == "repaired"
    (log,) = rec.rereads
    assert (log["source"], log["scope"], log["outcome"], log["expanded_source"]) == ("S1", "next_page", "found", "S2")
    assert _purposes(usage) == ["verify"]  # no re-judge: the unit was only missing a source


def test_the_continuation_is_tried_when_the_next_page_has_nothing():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "continuation"): PAGE_2}, ws=ws)
    md = "הסה\"כ בטבלה הוא 600 [S1]."
    report = verify_answer(_all_supported(), _answer(md), ws, "?", [], reread=fetcher)
    assert report.ok and fetcher.calls == [("S1", "next_page"), ("S1", "continuation")]
    assert [x["outcome"] for x in report.claims[0][0].rereads] == ["not_found", "found"]


def test_an_expansion_that_does_not_state_the_number_leaves_the_removal():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): "עמוד 2: שורה ג | 300", ("S1", "continuation"): "פסקה אחרת 700"},
                          ws=ws)
    md = "הסה\"כ בטבלה הוא 600 [S1]."
    report = verify_answer(_all_supported(), _answer(md), ws, "?", [], reread=fetcher)
    (p,) = report.problems
    assert p.removes_unit and p.check == "unstated_number" and not report.ok
    assert fetcher.adopted == [] and list(ws.sources) == ["S1"]
    assert [x["outcome"] for x in report.claims[0][0].rereads] == ["not_found"] * 3  # next_page, continuation, preceding


def test_a_number_in_a_different_form_is_matched_as_the_check_matches_it():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): "סה\"כ | 1,200,000"}, ws=ws)
    report = verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 1200000 [S1]."), ws, "?", [], reread=fetcher)
    assert report.ok and fetcher.adopted == [("S1", "next_page")]
    # a number the expansion states with another scale is no match
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): "סה\"כ | 1,200,000"}, ws=ws)
    report = verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 1,200 [S1]."), ws, "?", [], reread=fetcher)
    assert report.removed_units() and fetcher.adopted == []


def test_a_unit_that_holds_more_goes_to_the_judge_once_the_expansion_states_its_number():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): PAGE_2}, ws=ws)
    md = "שורה א היא 100 והסה\"כ בטבלה הוא 600 [S1]."
    seen: list[str] = []
    p = ScriptedProvider().on(Purpose.VERIFY, lambda i, input: (seen.append(input), {"verdicts": [
        _supported(n) for n in _units(input)]})[1], repeat=True)
    report = verify_answer(p, _answer(md), ws, "?", [], reread=fetcher)
    assert report.ok and len(seen) == 1 and "<unstated" not in seen[0]  # judged as a unit whose numbers are stated
    assert 'cites="S1,S2"' in seen[0]


def test_these_expansions_count_against_the_turns_bound(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_reread_claims", 2)
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): "סה\"כ | 600 | 700 | 800"}, ws=ws)
    md = "\n\n".join(f"{name} הוא {n} [S1]." for name, n in (("הראשון", 600), ("השני", 700), ("השלישי", 800)))
    report = verify_answer(_all_supported(), _answer(md), ws, "?", [], reread=fetcher)
    assert len(fetcher.calls) == 2  # two units were expanded, not three
    outcomes = {i: recs[0].rereads[0]["outcome"] for i, recs in report.claims.items()}
    assert outcomes == {0: "found", 1: "found", 2: "bound"}
    assert report.removed_units() == {2}


def test_an_unstated_number_expansion_and_a_claim_re_read_share_one_bound(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_reread_claims", 1)
    ws = _ws(PAGE_1 + "\nסיכום: סך כל הנכסים 25,742.", kind="table", table_index=0, block_start=2, block_end=2)
    fetcher = FakeFetcher({("S1", "next_page"): PAGE_2, ("S1", "units_note"): UNITS_NOTE}, ws=ws)

    def respond(instructions: str, input: str) -> dict:
        return {"verdicts": [_partial(i, t, "units_note") if "25,742" in t else _supported(i)
                             for i, t in _units(input).items()]}

    p = ScriptedProvider().on(Purpose.VERIFY, respond, repeat=True)
    md = "הסה\"כ בטבלה הוא 600 [S1].\n\nסך כל הנכסים הוא 25,742 [S1]."
    report = verify_answer(p, _answer(md), ws, "?", [], reread=fetcher)
    assert ("S1", "units_note") not in fetcher.calls
    assert report.claims[1][0].rereads[0]["outcome"] == "bound"


def test_no_expansion_for_a_missing_number_starts_without_time():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): PAGE_2}, ws=ws)
    report = verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 600 [S1]."), ws, "?", [],
                           deadline=time.monotonic() + verify.REREAD_MIN_SECONDS / 2, reread=fetcher)
    assert fetcher.calls == [] and report.removed_units() == {0}
    assert report.claims[0][0].rereads[0]["outcome"] == "no_time"


def test_a_failing_fetch_for_a_missing_number_leaves_the_removal():
    ws = _table_ws()
    report = verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 600 [S1]."), ws, "?", [],
                           reread=FakeFetcher(fail=True, ws=ws))
    assert report.removed_units() == {0} and report.claims[0][0].rereads[0]["outcome"] == "check_failed"


def test_an_uncited_unit_or_one_failing_for_another_reason_is_not_expanded():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): PAGE_2}, ws=ws)
    verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 600."), ws, "?", [], reread=fetcher)
    verify_answer(_all_supported(), _answer("הסה\"כ בטבלה הוא 600 [S7]."), ws, "?", [], reread=fetcher)
    assert fetcher.calls == []


# --- a number the part of the section before the passage states (round 13) ---------------------------------------------

PASSAGE = "שיעור ההפחתה לשטח הבנוי הוא 15%."
EARLIER = "מגרש 101 הוא מגרש פינתי בשטח רגיל."
ELSEWHERE = "מגרש 101 מופיע גם בנספח התכניות."  # another source of the turn states the number: a unit citing nothing may use it
NAMED_MD = "במגרש 101 חל שיעור הפחתה של 15%."  # cites nothing; the judge names S1


def _named_ws() -> Workspace:
    return _ws(PASSAGE, ELSEWHERE, block_start=9, block_end=9)


def _names_s1() -> ScriptedProvider:
    """A judge that finds every unit supported by S1, which the unit does not cite."""
    return ScriptedProvider().on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": n, "verdict": "supported", "reason": "בדיקה", "supported_by": ["S1"]} for n in _units(input)]},
        repeat=True)


def test_the_expansions_of_a_missing_number_read_the_part_before_the_passage_last():
    assert verify.UNSTATED_EXPANSIONS == ("next_page", "continuation", "preceding")
    assert "preceding" not in get_args(verify.NeedsContext)  # the judge cannot ask for it


def test_a_cited_unit_finds_its_number_in_the_part_of_the_section_before_the_passage():
    ws = _ws(PASSAGE, block_start=9, block_end=9)
    fetcher = FakeFetcher({("S1", "preceding"): Expansion(EARLIER, "תחילת הסעיף שלפני המקור")}, ws=ws)
    report = verify_answer(_all_supported(), _answer(f"{NAMED_MD} [S1]"), ws, "?", [], reread=fetcher)
    assert report.ok and not report.removed_units()
    assert [k for _, k in fetcher.calls] == ["next_page", "continuation", "preceding"]
    assert [p.cite for p in report.problems if p.kind == "needs_citation"] == ["S2"]
    assert [x["outcome"] for x in report.claims[0][0].rereads] == ["not_found", "not_found", "found"]
    assert report.claims[0][0].rereads[-1]["scope"] == "preceding"


def test_a_unit_the_judge_found_supported_by_a_named_source_keeps_a_number_the_section_before_it_states():
    ws = _named_ws()
    fetcher = FakeFetcher({("S1", "preceding"): Expansion(EARLIER, "תחילת הסעיף שלפני המקור")}, ws=ws)
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [], reread=fetcher)
    assert report.ok and not report.removed_units()
    # the named source and the adopted expansion are both cited by the server
    assert sorted(p.cite for p in report.problems if p.kind == "needs_citation") == ["S1", "S3"]
    assert EARLIER in ws.sources["S3"].text
    (log,) = ws.reread_log.values()
    assert [(x["source"], x["scope"], x["outcome"]) for x in log] == [
        ("S1", "next_page", "not_found"), ("S1", "continuation", "not_found"), ("S1", "preceding", "found")]
    assert log[-1]["expanded_source"] == "S3"
    assert fetcher.adopted == [("S1", "preceding")]


def test_a_number_nowhere_in_the_source_removes_a_named_support_unit_as_before():
    ws = _named_ws()
    fetcher = FakeFetcher({("S1", "preceding"): "פסקה אחרת, מגרש 202."}, ws=ws)
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [], reread=fetcher)
    (p,) = report.problems
    assert p.removes_unit and p.check == "unstated_number" and p.failure_kind == "absent_from_source"
    assert fetcher.adopted == [] and list(ws.sources) == ["S1", "S2"]
    assert [x["outcome"] for x in next(iter(ws.reread_log.values()))] == ["not_found"] * 3


def test_a_named_support_unit_the_bound_kept_from_the_read_is_context_not_read(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_reread_claims", 0)
    ws = _named_ws()
    fetcher = FakeFetcher({("S1", "preceding"): EARLIER}, ws=ws)
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [], reread=fetcher)
    (p,) = report.problems
    assert p.removes_unit and p.failure_kind == "context_not_read" and p.check == "unstated_number"
    assert fetcher.calls == [] and next(iter(ws.reread_log.values()))[-1]["outcome"] == "bound"


def test_a_named_support_unit_with_no_time_for_the_read_is_context_not_read():
    ws = _named_ws()
    fetcher = FakeFetcher({("S1", "preceding"): EARLIER}, ws=ws)
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [],
                           deadline=time.monotonic() + verify.REREAD_MIN_SECONDS / 2, reread=fetcher)
    (p,) = report.problems
    assert p.failure_kind == "context_not_read" and fetcher.calls == []


def test_a_failing_read_of_a_named_support_unit_stays_the_technical_failure():
    ws = _named_ws()
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [], reread=FakeFetcher(fail=True, ws=ws))
    (p,) = report.problems
    assert p.removes_unit and p.failure_kind != "context_not_read"
    assert next(iter(ws.reread_log.values()))[-1]["outcome"] == "check_failed"


def test_a_named_support_unit_is_removed_as_before_without_a_fetcher():
    ws = _named_ws()
    report = verify_answer(_names_s1(), _answer(NAMED_MD), ws, "?", [])
    (p,) = report.problems
    assert p.removes_unit and p.check == "unstated_number" and ws.reread_log == {}


# --- a removal for context not read is its own kind (round 13 U10, KTD11, R14) --------------------------------------------

CLAIM = "סך כל הנכסים הוא 25,742"
OTHER = "השכירות היא 55"
SRC = "סיכום: סך כל הנכסים 25,742. השכירות 55."


def _judge_needing(needs: str, verdict: str = "unsupported", also: bool = False, seen: list[str] | None = None
                   ) -> ScriptedProvider:
    """A judge whose claim about the total needs ``needs`` and is ``verdict`` for want of it; with ``also``, the unit's other
    claim (the rent) is supported. Shown ``UNITS_NOTE`` it supports everything."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        out = []
        for i in _units(input):
            if UNITS_NOTE in input:
                out.append(_supported(i))
                continue
            claims = [{"span": CLAIM, "verdict": verdict, "needs_context": needs}]
            if also:
                claims.append({"span": OTHER, "verdict": "supported"})
            out.append({"index": i, "verdict": verdict if not also else "partial", "reason": "חסר הקשר",
                        "defect": "part" if also else "none", "claims": claims})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def test_the_new_kind_has_its_sentence_label_and_hint_and_the_judge_cannot_emit_it():
    kind = "context_not_read"
    assert kind in verify.FAILURE_KINDS
    assert verify.REMOVAL_SENTENCES[kind] and verify.FAILURE_LABELS[kind] and verify.REPAIR_HINTS[kind]
    assert verify.removal_summary(kind)["text"] == verify.REMOVAL_SENTENCES[kind]
    assert kind not in get_args(verify.JudgeFailure)  # the server sets it; the judge's field stays as it was
    assert verify.JUDGE_VERSION == 18  # the policy text moved to 9 with round 13 U11 (rule 31); 10: round 15 U7; 11: round 16 U1 (read passages, `support`); 12: every returned passage; 13: round 17 U5 (defect `scope`); 14: round 18 U3 (the source header of a multi-report file, a bold label over a statement; rule 33); 15: round 18 U4 (the marks of a table part's attribution, rule 32); 16: round 18 U5 (the condition outcome of an item in the requirements call, `conditions`); 17: round 18 U6 (the candidates of the requirements call, `candidates`); 18: round 19 U2 (the `<ownership>` fact line of a unit whose evidence's owner is unknown, rule 34)


def test_a_header_re_read_that_states_the_datum_makes_the_claim_supported():
    ws = _ws("שורה | 25,742", kind="table", table_index=0)
    fetcher = FakeFetcher({("S1", "table_headers"): Expansion(f"כותרות הטבלה: {UNITS_NOTE}", "כותרות")}, ws=ws)
    report = verify_answer(_judge_needing("table_headers"), _answer(f"{CLAIM} [S1]."), ws, "?", [], reread=fetcher)
    assert report.ok and report.claims[0][0].verdict == "supported"
    assert report.claims[0][0].rereads[0]["outcome"] == "supported"


@pytest.mark.parametrize("fetcher_kw,outcome", [
    ({}, "no_context"),  # the source has nothing more to give
])
def test_a_re_read_that_finds_nothing_removes_the_claim_as_context_not_read(fetcher_kw, outcome):
    ws = _ws(SRC)
    report = verify_answer(_judge_needing("units_note"), _answer(f"{CLAIM} [S1]."), ws, "?", [],
                           reread=FakeFetcher(**fetcher_kw))
    assert report.claims[0][0].rereads[0]["outcome"] == outcome
    (p,) = report.problems
    assert p.removes_unit and p.failure_kind == "context_not_read"
    assert p.summary()["text"] == verify.REMOVAL_SENTENCES["context_not_read"]
    assert p.summary()["text"] != verify.REMOVAL_SENTENCES["absent_from_source"]
    (row,) = report.counts()["removals"]
    assert row["failure_kind"] == "context_not_read"


def test_a_claim_the_bound_kept_from_a_re_read_is_context_not_read_and_says_the_source_was_read_in_part():
    ws, md, fetcher = _five()
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        _supported(n) if UNITS_NOTE in input else
        {"index": n, "verdict": "unsupported", "reason": "חסר הקשר",
         "claims": [{"span": t, "verdict": "unsupported", "needs_context": "units_note"}]}
        for n, t in _units(input).items()]}, repeat=True)
    report = verify_answer(p, _answer(md), ws, "?", [], reread=fetcher)
    (problem,) = report.problems  # the first four were supported by the note; the fifth was never read
    assert problem.unit.index == 4 and problem.failure_kind == "context_not_read"
    assert verify.READ_IN_PART in problem.reason
    assert report.claims[4][0].failure_kind() == "context_not_read"


def test_a_claim_with_no_time_left_for_the_re_read_is_context_not_read():
    ws = _ws(SRC)
    report = verify_answer(_judge_needing("units_note"), _answer(f"{CLAIM} [S1]."), ws, "?", [],
                           deadline=time.monotonic() + verify.REREAD_MIN_SECONDS / 2,
                           reread=FakeFetcher({("S1", "units_note"): UNITS_NOTE}))
    assert report.claims[0][0].rereads[0]["outcome"] == "no_time"
    assert [p.failure_kind for p in report.problems] == ["context_not_read"]


def test_one_unsupported_claim_of_a_unit_is_cut_with_the_new_kind_and_the_other_stays():
    ws = _ws(SRC)
    md = f"{CLAIM} [S1] ו{OTHER} [S1]."
    report = verify_answer(_judge_needing("units_note", also=True), _answer(md), ws, "?", [], reread=FakeFetcher())
    assert report.cuts and report.removed_units() == set()
    (row,) = report.logged()
    assert row.action == "cut" and row.summary()["failure_kind"] == "context_not_read"


def test_a_failed_read_or_re_judge_stays_the_technical_failure_never_this_kind():
    ws = _ws(SRC)
    report = verify_answer(_judge_needing("units_note"), _answer(f"{CLAIM} [S1]."), ws, "?", [],
                           reread=FakeFetcher(fail=True))
    assert report.claims[0][0].rereads[0]["outcome"] == "check_failed"
    assert [p.failure_kind for p in report.problems] == ["absent_from_source"]


def test_a_claim_still_unsupported_after_the_expansion_was_shown_is_absent_from_the_source():
    ws = _ws(SRC)
    fetcher = FakeFetcher({("S1", "units_note"): "טקסט שלא מציין את הסכום"})
    report = verify_answer(_judge_needing("units_note"), _answer(f"{CLAIM} [S1]."), ws, "?", [], reread=fetcher)
    assert report.claims[0][0].rereads[0]["outcome"] == "unsupported"
    assert [p.failure_kind for p in report.problems] == ["absent_from_source"]


def test_a_judge_contradiction_stays_contradicts_source_and_is_not_re_read():
    ws = _ws(SRC)
    fetcher = FakeFetcher({("S1", "units_note"): UNITS_NOTE})
    p = ScriptedProvider().on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": n, "verdict": "contradicts", "reason": "אחר",
         "claims": [{"span": CLAIM, "verdict": "contradicts", "needs_context": "units_note"}]}
        for n in _units(input)]}, repeat=True)
    report = verify_answer(p, _answer(f"{CLAIM} [S1]."), ws, "?", [], reread=fetcher)
    assert fetcher.calls == [] and [x.failure_kind for x in report.problems] == ["contradicts_source"]


def test_a_failing_judge_call_stays_not_checked_check_failed():
    ws = _ws(SRC)
    p = ScriptedProvider().on(Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    with pytest.raises(verify.VerificationUnavailable):
        verify_answer(p, _answer(f"{CLAIM} [S1]."), ws, "?", [], reread=FakeFetcher())
    # a unit the judge left out is unchecked, never context not read
    p = ScriptedProvider().on(Purpose.VERIFY, {"verdicts": []}, repeat=True)
    report = verify_answer(p, _answer(f"{CLAIM} [S1]."), ws, "?", [], reread=FakeFetcher())
    assert [(x.failure_kind, x.cause) for x in report.problems] == [("not_checked", "omitted")]


def test_the_next_round_of_the_turn_keeps_the_kind_of_a_claim_already_re_read():
    ws = _ws(SRC)
    fetcher, cache = FakeFetcher(), VerdictCache()
    judge = _judge_needing("units_note")
    md = _answer(f"{CLAIM} [S1].")
    verify_answer(judge, md, ws, "?", [], reread=fetcher, cache=cache)
    report = verify_answer(judge, md, ws, "?", [], reread=fetcher, cache=cache)
    assert [p.failure_kind for p in report.problems] == ["context_not_read"]


def test_a_partial_claim_whose_context_is_unread_is_marked_with_the_kind_and_stays_a_partial_problem():
    ws = _ws(SRC)
    report = verify_answer(_judge("units_note", UNITS_NOTE), _answer(f"{CLAIM} [S1]."), ws, "?", [],
                           reread=FakeFetcher())
    (p,) = report.problems
    assert p.severity == "partial" and p.failure_kind == "context_not_read"
    assert verify.FAILURE_LABELS["context_not_read"] in p.repair_note()


def test_public_removals_passes_the_new_kind_with_its_fixed_sentence():
    from app.chat import api

    out = api.public_removals([{"failure_kind": "context_not_read", "component": "N1", "text": "must not leak"}])
    assert out == [{"failure_kind": "context_not_read", "component": "N1",
                    "text": verify.REMOVAL_SENTENCES["context_not_read"]}]


# --- an expansion is not adopted on a row ordinal of another table (round 18 U2, KTD3, R8) ------------------------------

ORDINAL_TABLE = "1 | דירה א | 100\n2 | דירה ב | 200\n3 | דירה ג | 300"


def test_an_expansion_whose_only_match_is_a_row_ordinal_of_another_table_is_not_adopted():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): ORDINAL_TABLE}, ws=ws)
    report = verify_answer(_all_supported(), _answer("מספר החדרים בדירה הוא 3 [S1]."), ws, "?", [], reread=fetcher)
    (p,) = report.problems
    assert p.removes_unit and p.check == "unstated_number" and p.unstated == ["3"]
    assert fetcher.adopted == [] and list(ws.sources) == ["S1"]
    assert "next_page" in [x["scope"] for x in report.claims[0][0].rereads]
    assert {x["outcome"] for x in report.claims[0][0].rereads} == {"not_found"}


def test_the_same_number_written_as_a_value_in_the_expansion_is_still_adopted():
    ws = _table_ws()
    fetcher = FakeFetcher({("S1", "next_page"): "מספר חדרים | 3\n" + ORDINAL_TABLE}, ws=ws)
    report = verify_answer(_all_supported(), _answer("מספר החדרים בדירה הוא 3 [S1]."), ws, "?", [], reread=fetcher)
    assert report.ok and fetcher.adopted == [("S1", "next_page")]


def test_only_consecutive_rows_that_count_are_row_ordinals():
    blanked = verify._without_row_ordinals(ORDINAL_TABLE)
    assert not verify.numbers_in(blanked) & {"1", "2", "3"}
    assert verify.numbers_in(blanked) >= {"100", "200", "300"}
    # a lone first cell, or one that does not count on, is a value
    assert verify.numbers_in(verify._without_row_ordinals("3 | דירה ג | 300")) >= {"3"}
    assert verify.numbers_in(verify._without_row_ordinals("3 | א | 1\n7 | ב | 2")) >= {"3", "7"}
