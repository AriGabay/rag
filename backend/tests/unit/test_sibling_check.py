"""Round 25 U2 (KTD1-KTD4, KTD7, KTD8; R1-R5, R7; AE1-AE4): the sibling check in the completeness decision.

A component is `full` only when no asked item of the block the answer used is left unstated (siblings offered to the requirements call for a
component that asks for a SET, from the sources that returned the same block too), a value-bearing item has its own attribution, and an item
with its own condition has that condition. Each mechanism sits behind its own setting (all off by default): `chat_sibling_anchors`,
`chat_condition_check`, `chat_own_attribution`. The judge is scripted: these tests prove what the server does with what it returns, not the
model's judgement. Every name, number and sentence is invented."""

from __future__ import annotations

import re

import pytest

from app.chat import items, verify
from app.chat.tools import STEP_LIMIT, TIME_LIMIT
from app.chat.verify import verify_answer
from app.config import get_settings
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_coverage import SOURCE, _answer, _file, _finish, _item_lines, _req, _turn

HEAD = ["זכויות בנייה מאושרות", "רכיב | שטח (מ״ר)"]
ROWS = ["שטח עיקרי מעל הקרקע | 2,140", "שטח שירות מעל הקרקע | 610", "שטח עיקרי מתחת לקרקע | 380", "שטח שירות מתחת לקרקע | 270"]
TOTAL = "סך הכול זכויות בנייה | 3,400"
TABLE = "\n".join([*HEAD, TOTAL, *ROWS])
SET_ASK = "פירוט זכויות בנייה לפי רכיבים"
SINGLE_ASK = "סך זכויות הבנייה בנכס"
TOTAL_ONLY = "סך זכויות הבנייה הוא 3,400 מ״ר [S2]."
WORDS = "זכויות בנייה"

SETTINGS = ("chat_sibling_anchors", "chat_condition_check", "chat_own_attribution")


@pytest.fixture
def sibling(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_sibling_anchors", True)


def _switch(monkeypatch, *on: str) -> None:
    for name in SETTINGS:
        monkeypatch.setattr(get_settings(), name, name in on)


def _table_ws(*tables: str, units: tuple[int, ...] | None = None, declared: tuple[int, ...] = (1,)):
    """S1 a context passage, S2… the tables, all of one file (``_file``)."""
    ws = _file(SOURCE, *tables, units=units if units is not None else (1,) * (1 + len(tables)), declared=declared)
    for sid in list(ws.sources)[1:]:
        ws.sources[sid].kind = "table"
    return ws


def _blocks(ws, sid: str, start: int, end: int, reading: str = "r1") -> None:
    ws.sources[sid].block_start, ws.sources[sid].block_end, ws.sources[sid].reading_id = start, end, reading


def _judge(assign=lambda cid, attrs: WORDS, *, seen: list | None = None, instructions: list | None = None,
           votes=None) -> ScriptedProvider:
    """A judge that scores every component ``full`` over the units shown and, for each ``<candidate>`` the call renders, names the request
    words ``assign(id, attrs)`` returns (None: not asked) under every scored requirement. ``votes(ids, units, asks)``: a hand-made reply."""
    p = ScriptedProvider()

    def respond(prompt: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        if instructions is not None:
            instructions.append(prompt)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        asks = []
        for cid, attrs in re.findall(r'<candidate id="(K\d+)"([^>]*)/>', input):
            words = assign(cid, attrs)
            if words is not None:
                asks.append({"candidate": cid, "words": words})
        out = {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]}
        if "<requirements>" in input:
            ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
            out["requirements"] = votes(ids, units, asks) if votes else [
                _req("", "full", list(units), id=i) | {"candidates": asks} for i in ids]
        return out

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _check(markdown: str, *, ws=None, ask: str = SET_ASK, raw=(), judge=None, ledger=None, components=None):
    ws = ws or _table_ws(TABLE)
    turn = _turn(*(components or [{"id": "1", "text": ask}]))
    answer = _answer(markdown).model_copy(update={"items": list(raw)})
    ledger = ledger if ledger is not None else items.Ledger()
    report = verify_answer(judge or _judge(), answer, ws, "?", [], requirements=turn, ledger=ledger)
    return report, ws, answer, ledger, turn


def _open(report) -> list[str]:
    """The row labels of the items the answer leaves open (omitted), in the ledger's order."""
    return [s["quote"].split(" | ")[0] for s in report.item_states() if s["state"] == "omitted"]


def _status(report) -> str:
    return report.requirement_outcomes()[0]["status"]


# --- AE1 / AE2: a set asks for the rows, one total asks for none (KTD1, KTD2; R2, R5) -----------------------------------------------

def test_ae1_a_set_request_whose_answer_gives_only_the_total_has_the_four_rows_as_open_items(sibling):
    report, ws, *_ = _check(TOTAL_ONLY)
    assert _status(report) == "partial"
    assert _open(report) == ["שטח עיקרי מעל הקרקע", "שטח שירות מעל הקרקע", "שטח עיקרי מתחת לקרקע", "שטח שירות מתחת לקרקע"]
    assert report.requirement_outcomes()[0]["omitted_items"]  # the pointer to the items that capped it


def test_ae2_a_request_for_the_total_alone_is_full_and_no_row_is_offered_or_open_even_when_a_judge_would_assign_them(sibling):
    seen: list[str] = []
    report, ws, *_ = _check(TOTAL_ONLY, ask=SINGLE_ASK, judge=_judge(seen=seen))
    assert "<candidates>" not in seen[0]  # nothing is offered for a single datum
    assert _status(report) == "full" and _open(report) == [] and report.candidate_offer.get("offered") in (None, [])


def test_a_judge_that_assigns_a_sibling_to_a_single_datum_component_anyway_never_makes_it_an_item(sibling):
    # two components: the first asks for a set, the second for one total; the offer exists (the first needs it), the second may not take a row
    def votes(ids, units, asks):
        return [_req("", "full", list(units), id=i) | {"candidates": asks} for i in ids]

    components = [{"id": "1", "text": SET_ASK}, {"id": "2", "text": SINGLE_ASK}]
    report, ws, _, ledger, _ = _check(TOTAL_ONLY, judge=_judge(votes=votes), components=components)
    by_component = {}
    for i in ledger.items.values():
        by_component.setdefault(i.component, []).append(i.label)
    assert set(by_component) == {"N1"} and len(by_component["N1"]) == 4
    outcomes = {o["id"]: o["status"] for o in report.requirement_outcomes()}
    assert outcomes == {"N1": "partial", "N2": "full"}
    trace = {v["call"]: [c for c in v["candidates"]] for v in report.requirements_trace()[1]["votes"]}
    assert {c["why"] for cs in trace.values() for c in cs} == {"not_a_set"}


def test_with_the_setting_off_a_single_datum_request_is_offered_the_rows_as_before(monkeypatch):
    _switch(monkeypatch)
    seen: list[str] = []
    report, *_ = _check(TOTAL_ONLY, ask=SINGLE_ASK, judge=_judge(seen=seen))
    assert "<candidates>" in seen[0] and _status(report) == "partial" and len(_open(report)) == 4


# --- KTD2: the sources that returned the same block are anchors too -------------------------------------------------------------------

FRAGMENT = "\n".join([*HEAD, TOTAL])  # the window the answer cited: the total row alone
WINDOW = "\n".join([*HEAD, TOTAL, *ROWS[:3]])  # another window of the same table, returned under another S#, with three more rows
WIDE = "\n".join([*HEAD, TOTAL, *ROWS])  # a third, wider window


def test_a_source_that_returned_an_overlapping_block_of_the_same_table_anchors_and_its_rows_are_offered_once(sibling):
    ws = _table_ws(FRAGMENT, WINDOW, WIDE)
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 10, 14)
    _blocks(ws, "S4", 11, 16)
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    offered = [c.quote for c in report.candidate_pool.values()]
    assert sorted(offered) == sorted(ROWS)  # every row once, whichever S# returned it
    assert len(offered) == len(set(offered)) and report.candidate_offer["offered"] == sorted(report.candidate_pool, key=lambda k: int(k[1:]))
    assert _status(report) == "partial" and len(_open(report)) == 4


def test_the_same_table_returned_under_a_second_s_number_adds_no_duplicate_candidate(sibling):
    ws = _table_ws(WIDE, WIDE + "\n")
    _blocks(ws, "S2", 10, 16)
    _blocks(ws, "S3", 10, 16)
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    quotes = [c.quote for c in report.candidate_pool.values()]
    assert sorted(quotes) == sorted(ROWS) and len(set(quotes)) == 4
    # the ids stay in the order of the offer and every one is rendered once
    assert len(report.candidate_offer["offered"]) == len(set(report.candidate_offer["offered"])) == 4


def test_a_read_source_of_another_block_of_the_same_document_is_no_anchor_under_the_first_attempt(sibling):
    ws = _table_ws(FRAGMENT, WINDOW)
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 40, 44)  # the same file, another part of it
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    assert report.candidate_pool == {} and _status(report) == "full"


@pytest.mark.parametrize("change", ["reading", "version", "unit", "document", "no_blocks"])
def test_a_source_that_differs_in_reading_version_unit_document_or_has_no_block_range_is_no_anchor(sibling, change):
    import uuid

    ws = _table_ws(FRAGMENT, WINDOW, units=(1, 1, 1) if change != "unit" else (1, 1, 2), declared=(1,))
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 10, 14, reading="r2" if change == "reading" else "r1")
    if change == "version":
        ws.sources["S3"].version_id = uuid.uuid4()
    if change == "document":
        ws.sources["S3"].document_id = uuid.uuid4()
    if change == "no_blocks":
        ws.sources["S3"].block_start = ws.sources["S3"].block_end = None
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    assert report.candidate_pool == {}, change


def test_a_second_appraisal_unit_with_the_same_row_labels_is_never_offered(sibling):
    other = "\n".join([*HEAD, "סך הכול זכויות בנייה | 1,900", "שטח עיקרי מעל הקרקע | 1,200", "שטח שירות מעל הקרקע | 330"])
    ws = _table_ws(FRAGMENT, other, units=(1, 1, 2), declared=(1,))
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 10, 14)  # the block range even overlaps: the unit tells them apart
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    assert report.candidate_pool == {}


def test_widened_anchors_are_not_offered_with_the_setting_off(monkeypatch):
    _switch(monkeypatch)
    ws = _table_ws(FRAGMENT, WINDOW)
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 10, 14)
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    assert report.candidate_pool == {}


# --- KTD2 caps: an assignment counts only from a vote that sees the component's surviving unit --------------------------------------

def _hand_report(setting: bool, monkeypatch):
    """A report whose component N1 was scored in two calls: the first saw its answer (unit 0), the second only another unit and still
    assigned a sibling."""
    from app.chat.verify import CandidateAssignment, JudgeRequirement, VerifyReport, split_units

    monkeypatch.setattr(get_settings(), "chat_sibling_anchors", setting)
    ws = _table_ws(TABLE)
    units = split_units("סך זכויות הבנייה הוא 3,400 מ״ר [S2].\n\nהערה כללית על הנכס.")
    ledger = items.Ledger()
    report = VerifyReport(units, request="?")
    report.item_ledger, report.item_ws = ledger, ws
    report.requirements = [{"id": "N1", "text": SET_ASK, "kind": "information", "parent": ""}]
    offer = items.candidates(units[:1], ws, ledger, report.requirements, "")
    report.candidate_pool = {c.id: c for c in offer.candidates}
    seen = CandidateAssignment(candidate=offer.candidates[0].id, words=WORDS)
    blind = JudgeRequirement(id="N1", status="partial", units=[], candidates=[seen])
    sighted = JudgeRequirement(id="N1", status="full", units=[0])
    return report, ledger, [sighted, blind], offer


def test_an_assignment_from_a_batch_that_cannot_see_the_components_answer_does_not_become_an_item(monkeypatch):
    report, ledger, votes, _ = _hand_report(True, monkeypatch)
    report.requirement_votes = {"N1": votes}
    report.add_candidate_items()
    assert ledger.items == {}
    report.requirement_votes = {"N1": [votes[0], votes[1].model_copy(update={"units": [0]})]}
    report.add_candidate_items()
    assert len(ledger.items) == 1  # the same assignment from a vote that sees unit 0


def test_with_the_setting_off_the_same_blind_assignment_becomes_an_item_as_before(monkeypatch):
    report, ledger, votes, _ = _hand_report(False, monkeypatch)
    report.requirement_votes = {"N1": votes}
    report.add_candidate_items()
    assert len(ledger.items) == 1


def test_a_vote_whose_only_unit_was_removed_does_not_see_a_surviving_unit(monkeypatch):
    from app.chat.verify import Problem

    report, ledger, votes, _ = _hand_report(True, monkeypatch)
    votes[1] = votes[1].model_copy(update={"units": [1]})
    report.requirement_votes = {"N1": votes}
    report.problems.append(Problem(report.units[1], "הוסרה", kind="claim"))
    report.add_candidate_items()
    assert ledger.items == {}


# --- KTD1: the policy text and the cache key move with the setting --------------------------------------------------------------------

def test_the_requirements_policy_tells_a_full_vote_to_assign_candidates_only_with_the_setting_on(monkeypatch):
    prompts_on: list[str] = []
    prompts_off: list[str] = []
    _switch(monkeypatch, "chat_sibling_anchors")
    _check(TOTAL_ONLY, judge=_judge(instructions=prompts_on))
    _switch(monkeypatch)
    _check(TOTAL_ONLY, judge=_judge(instructions=prompts_off))
    assert verify.JUDGE_REQUIREMENTS_POLICY in prompts_off[0] and verify.JUDGE_REQUIREMENTS_POLICY in prompts_on[0]
    assert prompts_on[0] != prompts_off[0] and prompts_on[0].startswith(prompts_off[0].split(verify.JUDGE_REQUIREMENTS_POLICY)[0])
    assert verify.JUDGE_SIBLING_POLICY in prompts_on[0] and verify.JUDGE_SIBLING_POLICY not in prompts_off[0]
    assert "full" in verify.JUDGE_SIBLING_POLICY and "candidates" in verify.JUDGE_SIBLING_POLICY


def test_the_judge_version_is_the_old_one_with_the_setting_off_and_the_next_with_it_on(monkeypatch):
    _switch(monkeypatch)
    assert verify.judge_version() == verify.JUDGE_VERSION == 23
    _switch(monkeypatch, "chat_condition_check", "chat_own_attribution")
    assert verify.judge_version() == 23  # the prompt does not change for the other two mechanisms
    _switch(monkeypatch, "chat_sibling_anchors")
    assert verify.judge_version() == 24


# --- KTD3 / AE4: an item whose own source states a condition is given in full only with that condition ---------------------------------

COND_ROWS = ["שווי הקרקע לפי השומה", "פריט | סכום (₪) | הערה", "שווי הקרקע | 4,820,000 | בכפוף לאישור התכנית המפורטת",
             "שווי המבנה | 1,150,000 | "]
COND_QUOTE = "שווי הקרקע | 4,820,000 | בכפוף לאישור התכנית המפורטת"
COND_ASK = "שווי הקרקע"


def _cond_item(**kw):
    from tests.unit.test_coverage import _adj_item

    return _adj_item("שווי הקרקע", COND_QUOTE, "4,820,000", **kw)


def _cond_check(markdown: str, *, quote: str = COND_QUOTE, rows=COND_ROWS, label: str = "שווי הקרקע", value: str = "4,820,000"):
    from tests.unit.test_coverage import _adj_item

    ws = _table_ws("\n".join(rows))
    return _check(markdown, ws=ws, ask=COND_ASK, raw=[_adj_item(label, quote, value)])


@pytest.fixture
def cond(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_condition_check", True)


def test_ae4_a_value_stated_without_the_condition_its_row_states_is_partial_with_the_condition_missing_and_the_value_stays(cond):
    report, ws, answer, ledger, turn = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    (state,) = report.item_states()
    assert (state["state"], state["outcome"]) == ("omitted", "partial")
    assert state["missing"] == ["בכפוף לאישור התכנית המפורטת"]  # the source's words, never the answer's
    assert _status(report) == "partial"
    # the existing condition route asks for the condition next to the value, without tools; nothing is removed
    (problem,) = [p for p in report.problems if p.kind == "requirement"]
    assert problem.check == verify.CONDITION_FOUND_CHECK and "בכפוף לאישור התכנית המפורטת" in problem.reason
    assert report.removed_units() == set() and report.text_completion_only() is True


def test_the_same_value_with_the_condition_in_any_equivalent_wording_is_covered_and_full(cond):
    for stated in ("שווי הקרקע הוא 4,820,000 ₪, בכפוף לאישור התכנית המפורטת [S2].",
                   "שווי הקרקע הוא 4,820,000 ₪ והוא מותנה באישור התכנית המפורטת [S2].",
                   "שווי הקרקע הוא 4,820,000 ₪ [S2]. הערך כפוף לאישורה של התכנית המפורטת."):
        report, *_ = _cond_check(stated)
        assert [s["state"] for s in report.item_states()] == ["covered"], stated
        assert _status(report) == "full" and not [p for p in report.problems if p.kind == "requirement"], stated


def test_a_negation_the_datum_itself_carries_and_the_answer_repeats_counts_as_the_condition_stated(cond):
    rows = ["מחיר המכירה בשומה", "פריט | סכום (₪)", "מחיר המכירה | 3,950,000 ₪ לא כולל מע״מ"]
    quote = "מחיר המכירה | 3,950,000 ₪ לא כולל מע״מ"
    kept, *_ = _cond_check("מחיר המכירה הוא 3,950,000 ₪ לא כולל מע״מ [S2].", quote=quote, rows=rows, label="מחיר המכירה", value="3,950,000")
    assert [s["state"] for s in kept.item_states()] == ["covered"] and _status(kept) == "full"
    lost, *_ = _cond_check("מחיר המכירה הוא 3,950,000 ₪ [S2].", quote=quote, rows=rows, label="מחיר המכירה", value="3,950,000")
    (state,) = lost.item_states()
    assert state["outcome"] == "partial" and state["missing"] == ["לא כולל מע״מ"]


def test_a_condition_a_heading_states_in_words_the_server_cannot_class_is_not_decided_by_it(cond):
    rows = ["שווי הקרקע בשיטת ההשוואה", "פריט | סכום (₪)", "שווי הקרקע | 4,820,000"]
    report, *_ = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].", quote="שווי הקרקע | 4,820,000", rows=rows)
    assert [s["state"] for s in report.item_states()] == ["covered"] and _status(report) == "full"


def test_with_the_setting_off_the_value_without_its_condition_is_covered_and_the_component_full_as_before(monkeypatch):
    _switch(monkeypatch)
    report, *_ = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    assert [s["state"] for s in report.item_states()] == ["covered"] and _status(report) == "full"


def test_the_item_line_of_a_missing_condition_names_the_condition_and_never_the_value(cond):
    report, ws, answer, _, turn = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    final, outcomes = _finish(ws, answer, report, turn)
    (line,) = _item_lines(final)
    assert line.startswith("«שווי הקרקע»") and "בכפוף לאישור התכנית המפורטת" in line and "בחלקו" in line
    assert "4,820,000" not in line
    assert final.answer_markdown.startswith(answer.answer_markdown)  # the model's sentence and its value are untouched


# --- KTD4: a value-bearing item needs attribution to its own source and its own row -------------------------------------------------------

AREA_TABLE = "\n".join(["זכויות בנייה מאושרות", "רכיב | שטח (מ״ר)", "שטח עיקרי מעל הקרקע | 2,140", "שטח שירות מעל הקרקע | 610"])
AREA_NOTE = "\n".join(["סיכום זכויות הבנייה", "בסך הכול נקבעו שטחים מעל הקרקע בהיקף 2,140 מ״ר בנוסף לשטחי שירות."])


@pytest.fixture
def attributed(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_own_attribution", True)


def _area_state(markdown: str, *, texts=(AREA_TABLE, AREA_NOTE), label="שטח עיקרי מעל הקרקע", quote="שטח עיקרי מעל הקרקע | 2,140",
                value="2,140", audit: bool = True) -> dict:
    from tests.unit.test_items import _item, _units, _ws

    ws = _ws(*texts, same_file=True)
    ledger = items.Ledger()
    ledger.merge([_item(label=label, quote=quote, value=value)])
    (state,) = ledger.outcomes(_units(markdown), ws, audit=audit)
    return state


def test_a_number_held_by_another_passage_of_the_unit_gives_the_item_only_with_its_setting_off(monkeypatch):
    _switch(monkeypatch)
    assert _area_state("השטחים הכוללים הם 2,140 מ״ר [S2].")["state"] == "covered"  # the loose rule: any passage of the unit holding the number
    _switch(monkeypatch, "chat_own_attribution")
    loose = _area_state("השטחים הכוללים הם 2,140 מ״ר [S2].")
    assert loose["state"] == "omitted" and loose["why"] in ("not_attributed", "value_not_stated")


def test_the_same_passage_gives_the_item_when_the_block_states_the_items_own_row(attributed):
    assert _area_state("השטח העיקרי מעל הקרקע הוא 2,140 מ״ר [S2].")["state"] == "covered"
    assert _area_state("הזכויות: שטח עיקרי מעל הקרקע - 2,140 מ״ר [S1] [S2].")["state"] == "covered"  # cited with its own source too


def test_a_block_that_cites_the_items_own_source_is_attributed_whatever_the_setting(attributed):
    assert _area_state("השטחים הם 2,140 מ״ר [S1].")["state"] == "covered"


def test_an_uncited_paragraph_that_states_the_number_and_the_label_gives_a_value_bearing_item_only_with_its_setting_off(monkeypatch):
    _switch(monkeypatch)
    assert _area_state("שטח עיקרי מעל הקרקע הוא 2,140 מ״ר.")["state"] == "covered"  # the datum audit's fallback
    _switch(monkeypatch, "chat_own_attribution")
    uncited = _area_state("שטח עיקרי מעל הקרקע הוא 2,140 מ״ר.")
    assert uncited["state"] == "omitted" and uncited["why"] == "not_attributed"
    assert _area_state("שטח עיקרי מעל הקרקע הוא 2,140 מ״ר.", audit=False)["state"] == "omitted"  # (the plain check never gave it)


def test_a_table_body_row_without_its_own_citation_is_still_given_through_the_tables_citation(attributed):
    markdown = "הזכויות שנקבעו [S1]:\n\n| רכיב | שטח |\n|---|---|\n| שטח עיקרי מעל הקרקע | 2,140 |"
    assert _area_state(markdown)["state"] == "covered"


def test_an_item_without_a_figure_is_still_given_by_the_uncited_fallback_as_before(attributed):
    reasons = "\n".join(["הנימוקים להתאמה:", "- מיקום מרכזי בעיר", "- קרבה לתחבורה ציבורית"])
    state = _area_state("ההתאמה נובעת ממיקום מרכזי בעיר.", texts=(reasons,), label="מיקום", quote="- מיקום מרכזי בעיר", value="מיקום מרכזי בעיר")
    assert state["state"] == "covered"


def test_the_uncited_bullet_leaves_a_read_and_not_used_line_and_caps_the_component_with_the_setting_on(monkeypatch):
    from tests.unit.test_coverage import _adj_file, _adj_items, _ledger_check

    bullets = "- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].\n- ההתאמה לגיל הבניין היא 2%."
    for on, status, lines in ((False, "full", []), (True, "partial", ["«גיל הבניין"])):
        _switch(monkeypatch, *(["chat_own_attribution"] if on else []))
        ws = _adj_file()
        r, turn, answer, _ = _ledger_check(ws, bullets, _adj_items())
        final, outcomes = _finish(ws, answer, r, turn)
        assert outcomes[0]["status"] == status and [ln.split("»")[0] for ln in _item_lines(final)] == lines


# --- KTD7: a capped component reaches the gap lines, and each item line names its cause and never the value ---------------------------

def _capped(monkeypatch, *on: str, markdown: str = TOTAL_ONLY, ws=None):
    _switch(monkeypatch, *(on or ("chat_sibling_anchors",)))
    ws = ws or _table_ws(TABLE)
    report, ws, answer, ledger, turn = _check(markdown, ws=ws)
    final, outcomes = _finish(ws, answer, report, turn)
    return report, ws, final, outcomes


def test_a_capped_component_reaches_the_component_outcomes_and_each_open_row_gets_a_line_naming_its_label_place_and_cause(monkeypatch):
    report, ws, final, outcomes = _capped(monkeypatch)
    (o,) = outcomes
    assert o["status"] == "partial" and o["stated"] is False and len(o["omitted_items"]) == 4  # stated one by one, in the item lines
    lines = _item_lines(final)
    assert [ln.split("»")[0] for ln in lines] == ["«" + r.split(" | ")[0] for r in ROWS]
    for line in lines:
        assert "דוח בדיקה" in line and "S2" in line and "נקרא" in line and "לא נעשה בו שימוש" in line
        assert "לא הושלם מחוסר זמן" in line  # the cause when no completion ran and nothing else is known
        assert not re.search(r"\d{3}", line.split("S2")[0].split("»")[1])  # the line never states the row's value
    assert not re.search(r"2,140|\b610\b|\b380\b|\b270\b", "\n".join(lines))
    assert [g["cause"] for g in report.item_gaps] == ["not_completed_time"] * 4
    assert not re.search(r"לא נמצא|לא אותר|לא מופיע", "\n".join(lines))


def test_with_every_detection_setting_off_the_item_lines_carry_no_cause(monkeypatch):
    _switch(monkeypatch)
    report, ws, answer, ledger, turn = _check(TOTAL_ONLY, ws=_table_ws(TABLE))  # the offer is the old one: the rows are offered, the judge assigns
    final, _ = _finish(ws, answer, report, turn)
    assert _item_lines(final) and all("לא הושלם" not in ln for ln in _item_lines(final))
    assert all("cause" not in g for g in report.item_gaps)


def test_a_condition_gap_line_names_the_condition_and_the_cause_when_only_that_setting_is_on(monkeypatch):
    _switch(monkeypatch, "chat_condition_check")
    report, ws, answer, _, turn = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    final, _ = _finish(ws, answer, report, turn)
    (line,) = _item_lines(final)
    assert "בכפוף לאישור התכנית המפורטת" in line and line.rstrip(".").endswith("לא הושלם מחוסר זמן") and "4,820,000" not in line


@pytest.mark.parametrize("how,cause,words", [
    ("steps", "not_completed_steps", "הצעדים"),
    ("tool_budget", "not_completed_steps", "הצעדים"),
    ("time", "not_completed_time", "מחוסר זמן"),
    ("settled", "not_completed_settled", "סבב"),
    ("rounds", "not_completed_settled", "סבב"),
    ("unread", "context_not_read", "לא נקרא"),
])
def test_the_cause_follows_the_attempt_record_the_turns_limits_and_the_source(monkeypatch, how, cause, words):
    _switch(monkeypatch, "chat_sibling_anchors")
    ws = _table_ws(TABLE)
    if how == "steps":
        ws.limits_hit.append(STEP_LIMIT)
    elif how == "tool_budget":
        ws.limits_hit.append("tool_budget")
    elif how == "time":
        ws.limits_hit.append(TIME_LIMIT)
    elif how in ("settled", "rounds"):
        ws.clock.note_repair_gate({"decision": "refused", "refused_by": ["settled" if how == "settled" else "rounds_exhausted"]})
    else:
        ws.sources["S2"].unread_regions = ["R1"]
    report, ws, answer, _, turn = _check(TOTAL_ONLY, ws=ws)
    final, _ = _finish(ws, answer, report, turn)
    assert {g["cause"] for g in report.item_gaps} == {cause}
    assert all(words in ln for ln in _item_lines(final))


def test_the_gate_record_outranks_a_limit_the_turn_reached_and_an_explicit_record_outranks_both(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    ws = _table_ws(TABLE)
    ws.limits_hit.append(TIME_LIMIT)
    ws.clock.note_repair_gate({"decision": "refused", "refused_by": ["settled"]})
    report, ws, answer, _, turn = _check(TOTAL_ONLY, ws=ws)
    assert {g["cause"] for g in (_finish(ws, answer, report, turn), report.item_gaps)[1]} == {"not_completed_settled"}
    first = next(s["id"] for s in report.item_states() if s["state"] == "omitted")
    report.gap_causes[first] = "not_completed_steps"
    _finish(ws, answer, report, turn)
    assert sorted(g["cause"] for g in report.item_gaps).count("not_completed_steps") == 1


def test_a_declared_not_verifiable_item_keeps_its_reason_and_gets_no_cause(monkeypatch):
    from tests.unit.test_coverage import _adj_file, _adj_items, _ledger_check

    _switch(monkeypatch, "chat_sibling_anchors")
    ws = _adj_file()
    r, turn, answer, _ = _ledger_check(ws, "ההתאמה למיקום היא 5% [S2]. ההתאמה לקומה היא 3% [S2].",
                                       _adj_items(state="not_verifiable", reason="גיל הבניין בין ההתאמות שהשומה יישמה"))
    final, _ = _finish(ws, answer, r, turn)
    (line,) = _item_lines(final)
    assert "לא ניתן לאמת" in line and "לא הושלם" not in line and all("cause" not in g for g in r.item_gaps)


# --- the pointer: the surviving units that fill a component and the evidence they cite (R1; U4 completes the record) -----------------

def test_the_decision_points_at_the_surviving_units_that_fill_the_component_and_the_sources_they_cite(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors")
    report, *_ = _check(TOTAL_ONLY + "\n\nהנתון נקרא מהטבלה [S1].")
    decision = report.requirement_decisions["N1"]
    assert decision["status"] == "partial" and decision["caps"] == ["omitted_items"]
    assert decision["units"] == [0, 1] and decision["cites"] == ["S1", "S2"]
    assert [row["units"] for row in decision["rests_on"]] == [[0, 1]]
    _switch(monkeypatch)
    old, *_ = _check(TOTAL_ONLY + "\n\nהנתון נקרא מהטבלה [S1].", ask=SINGLE_ASK)
    assert "units" not in old.requirement_decisions["N1"] and "cites" not in old.requirement_decisions["N1"]


# --- the plan's other scenarios, each with its negative control ---------------------------------------------------------------------------

NORTH = "\n".join([*HEAD, "סך הכול זכויות בנייה | 3,400", "שטח עיקרי מעל הקרקע | 2,140", "שטח שירות מעל הקרקע | 610", "שטח עיקרי מתחת לקרקע | 650"])
SOUTH = "\n".join([*HEAD, "סך הכול זכויות בנייה | 2,950", "שטח עיקרי מעל הקרקע | 1,870", "שטח שירות מעל הקרקע | 480", "שטח עיקרי מתחת לקרקע | 600"])


def test_a_comparison_checks_each_sides_siblings_against_that_sides_block_only(sibling):
    ws = _table_ws(NORTH, SOUTH, units=(1, 1, 2), declared=(1, 2))
    _blocks(ws, "S2", 10, 14)
    _blocks(ws, "S3", 10, 14)  # the same range in the two units: only the unit tells the blocks apart
    markdown = "בנכס הצפוני סך זכויות הבנייה הוא 3,400 מ״ר [S2], ובנכס הדרומי 2,950 מ״ר [S3]."
    report, ws, _, ledger, _ = _check(markdown, ws=ws, ask="השוואת זכויות בנייה לפי רכיבים בין שני הנכסים")
    pool = list(report.candidate_pool.values())
    assert len(pool) == 6 and {c.source for c in pool} == {"S2", "S3"}
    own = {"S2": set(NORTH.splitlines()), "S3": set(SOUTH.splitlines())}
    assert all(c.quote in own[c.source] for c in pool)  # no row of property A is offered from property B's source, nor the reverse
    units = {sid: ws.sources[sid].unit["id"] for sid in own}
    assert units["S2"] != units["S3"]
    assert all(i.unit_id == units[i.source] for i in ledger.items.values() if i.candidate)
    assert _status(report) == "partial" and len(_open(report)) == 6


def test_a_follow_up_that_narrows_to_one_member_asks_for_no_other_member(sibling):
    seen: list[str] = []
    report, *_ = _check("שטח השירות מעל הקרקע הוא 610 מ״ר [S2].", ask="שטח השירות מעל הקרקע", judge=_judge(seen=seen))
    assert "<candidates>" not in seen[0] and report.candidate_pool == {} and _status(report) == "full" and _open(report) == []


def test_an_instruction_component_never_gets_candidates_and_is_decided_by_the_form_of_the_answer(sibling):
    seen: list[str] = []
    components = [{"id": "1", "text": SET_ASK}, {"id": "2", "text": "צטט מראה מקום לכל נתון", "kind": "instruction", "aspect": "citation"}]
    report, ws, _, ledger, _ = _check(TOTAL_ONLY, components=components, judge=_judge(seen=seen))
    assert all(i.component == "N1" for i in ledger.items.values())
    outcomes = {o["id"]: o for o in report.requirement_outcomes()}
    assert outcomes["N1"]["status"] == "partial" and outcomes["N2"]["status"] == "full" and outcomes["N2"]["check"] == "citation"
    assert not [p for p in report.problems if p.kind == "instruction"]  # a gap of information never reaches the instruction


def test_a_source_item_the_server_built_from_a_cited_span_and_the_judge_did_not_assign_never_caps(sibling):
    report, *_ = _check("שווי הקרקע הוא 4,820,000 ₪ [S2].", ws=_table_ws("\n".join(COND_ROWS)), ask="פירוט שווי הקרקע לפי רכיבים",
                        judge=_judge(lambda cid, attrs: None))
    assert _status(report) == "full" and _open(report) == []
    assert any(i.origin == "source" and not i.candidate for i in report.item_ledger.items.values())  # the span's condition is checked, it caps nothing


def test_the_values_own_condition_can_still_open_a_condition_item_of_a_single_datum_component(monkeypatch):
    _switch(monkeypatch, "chat_sibling_anchors", "chat_condition_check")
    seen: list[str] = []
    ws = _table_ws("\n".join(COND_ROWS))
    from tests.unit.test_coverage import _adj_item

    report, *_ = _check("שווי הקרקע הוא 4,820,000 ₪ [S2].", ws=ws, ask=COND_ASK, raw=[_adj_item("שווי הקרקע", COND_QUOTE, "4,820,000")],
                        judge=_judge(seen=seen))
    assert "<candidates>" not in seen[0]  # the sibling row (the building's value) is not asked for a single datum
    assert _status(report) == "partial" and len(_open(report)) == 1  # the one open item is the land's own condition


AMOUNT_ROWS = ["רכיבי זכויות", "רכיב | שטח", "שטח עיקרי | 2,674,200", "שטח שירות | 2,674,200"]


@pytest.mark.parametrize("on", [(), SETTINGS])
def test_ae3_an_equivalent_number_form_with_the_rows_label_is_stated_and_the_same_value_under_the_neighbouring_label_is_not(monkeypatch, on):
    from tests.unit.test_items import _item, _units, _ws

    _switch(monkeypatch, *on)
    ws = _ws("\n".join(AMOUNT_ROWS))
    main = _item(label="שטח עיקרי", quote="שטח עיקרי | 2,674,200", value="2,674,200")
    side = _item(label="שטח שירות", quote="שטח שירות | 2,674,200", value="2,674,200")
    for markdown, expected in (("השטח העיקרי הוא 2.6742 מיליון מ״ר [S1].", {"שטח עיקרי": "covered", "שטח שירות": "omitted"}),
                               ("שטח השירות הוא 2,674,200 מ״ר [S1].", {"שטח עיקרי": "omitted", "שטח שירות": "covered"})):
        ledger = items.Ledger()
        ledger.merge([main, side])
        got = {o["quote"].split(" | ")[0]: o["state"] for o in ledger.outcomes(_units(markdown), ws, audit=True)}
        assert got == expected, markdown


# --- each setting alone changes only its own mechanism (KTD8) -----------------------------------------------------------------------

def _ae1_widened(monkeypatch, on):
    _switch(monkeypatch, *on)
    ws = _table_ws(FRAGMENT, WIDE)
    _blocks(ws, "S2", 10, 11)
    _blocks(ws, "S3", 10, 16)
    report, *_ = _check(TOTAL_ONLY, ws=ws)
    return _status(report), len(_open(report))


def _ae4(monkeypatch, on):
    _switch(monkeypatch, *on)
    report, *_ = _cond_check("שווי הקרקע הוא 4,820,000 ₪ [S2].")
    return _status(report), len([s for s in report.item_states() if s["state"] == "omitted"])


def _attr(monkeypatch, on):
    from tests.unit.test_coverage import _adj_file, _adj_items, _ledger_check

    _switch(monkeypatch, *on)
    r, *_ = _ledger_check(_adj_file(), "- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].\n- ההתאמה לגיל הבניין היא 2%.", _adj_items())
    return r.requirement_outcomes()[0]["status"], len([s for s in r.item_states() if s["state"] == "omitted"])


@pytest.mark.parametrize("on,ae1,ae4,attr", [
    ((), ("full", 0), ("full", 0), ("full", 0)),
    (("chat_sibling_anchors",), ("partial", 4), ("full", 0), ("full", 0)),
    (("chat_condition_check",), ("full", 0), ("partial", 1), ("full", 0)),
    (("chat_own_attribution",), ("full", 0), ("full", 0), ("partial", 1)),
    (SETTINGS, ("partial", 4), ("partial", 1), ("partial", 1)),
])
def test_each_setting_alone_changes_only_its_own_mechanisms_decisions(monkeypatch, on, ae1, ae4, attr):
    assert _ae1_widened(monkeypatch, on) == ae1
    assert _ae4(monkeypatch, on) == ae4
    assert _attr(monkeypatch, on) == attr


def test_a_source_item_the_server_built_from_a_cited_span_keeps_the_existing_condition_route_with_the_setting_on(cond):
    # no agent item: the server builds one from the cited span so that its condition is checked (round 18 U5); it is covered by the sentence that
    # cites it and the condition is completed by the existing note, not held against a component as a partial item
    report, *_ = _check("שווי הקרקע הוא 4,820,000 ₪ [S2].", ws=_table_ws("\n".join(COND_ROWS)), ask=COND_ASK)
    (state,) = [s for s in report.item_states() if s["origin"] == "source"]
    assert state["state"] == "covered" and state["condition_check"] == "missing" and "condition_gap" not in state
    assert _status(report) == "full" and [p.check for p in report.problems if p.kind == "requirement"] == [verify.CONDITION_FOUND_CHECK]


def test_after_the_repair_states_the_condition_the_same_ledger_makes_the_item_covered_and_the_component_full(cond):
    from tests.unit.test_coverage import _adj_item

    ws = _table_ws("\n".join(COND_ROWS))
    raw = [_adj_item("שווי הקרקע", COND_QUOTE, "4,820,000")]
    first, _, _, ledger, _ = _check("שווי הקרקע הוא 4,820,000 ₪ [S2].", ws=ws, ask=COND_ASK, raw=raw)
    assert _status(first) == "partial"
    second, *_ = _check("שווי הקרקע הוא 4,820,000 ₪, בכפוף לאישור התכנית המפורטת [S2].", ws=ws, ask=COND_ASK, raw=raw, ledger=ledger)
    assert _status(second) == "full" and [s["state"] for s in second.item_states()] == ["covered"]
    assert not [p for p in second.problems if p.kind == "requirement"]


# --- property: whatever the names, the numbers, the order of the rows and the shape of the answer ------------------------------------------

VOCABS = [
    ("זכויות בנייה מאושרות", "רכיב | שטח (מ״ר)", ["חלק עליון | 912", "חלק תחתון | 407", "חלק צדדי | 1,118"], "סך הכול זכויות בנייה | 2,437",
     "פירוט זכויות בנייה לפי רכיבים", "זכויות בנייה", "סך זכויות הבנייה הוא 2,437 מ״ר"),
    ("מרכיבי שטח מסחרי", "מרכיב | שטח (מ״ר)", ["חזית ראשית | 412", "חזית צדדית | 133", "מרתף מסחרי | 290", "גלריה פנימית | 175"],
     "סך הכול שטח מסחרי | 1,010", "פירוט שטח מסחרי לפי מרכיבים", "שטח מסחרי", "השטח המסחרי הכולל הוא 1,010 מ״ר"),
    ("יחידות דיור בתכנית", "סוג | מספר", ["דירות קטנות | 136", "דירות בינוניות | 254", "דירות גדולות | 118"], "סך הכול יחידות דיור | 508",
     "פירוט יחידות דיור לפי סוג", "יחידות דיור", "מספר יחידות הדיור הכולל הוא 508"),
]


@pytest.mark.parametrize("vocab", VOCABS)
@pytest.mark.parametrize("total_at", ["first", "middle", "last"])
@pytest.mark.parametrize("shape", ["sentence", "bullets", "row_stated"])
def test_property_the_rows_the_answer_does_not_state_are_the_open_items_whatever_the_names_numbers_order_and_shape(
        sibling, vocab, total_at, shape):
    caption, header, rows, total, ask, words, total_sentence = vocab
    ordered = {"first": [total, *rows], "last": [*rows, total], "middle": [rows[0], total, *rows[1:]]}[total_at]
    ws = _table_ws("\n".join([caption, header, *ordered]))
    stated: list[str] = []
    if shape == "sentence":
        markdown = f"{total_sentence} [S2]."
    elif shape == "bullets":
        markdown = f"הנתונים לפי הטבלה:\n\n- {total_sentence} [S2]."
    else:
        label, value = rows[1].split(" | ")
        stated = [rows[1]]
        markdown = f"{total_sentence} [S2]. {label} - {value} [S2]."
    report, *_ = _check(markdown, ws=ws, ask=ask, judge=_judge(lambda cid, attrs: words))
    expected = [r.split(" | ")[0] for r in ordered if r != total and r not in stated]
    assert _open(report) == expected
    assert _status(report) == "partial"
    # the nearest negative control: the same answer with every row stated is full
    every = f"{total_sentence} [S2]. " + " ".join(f"{r.split(' | ')[0]} - {r.split(' | ')[1]} [S2]." for r in rows)
    full, *_ = _check(every, ws=ws, ask=ask, judge=_judge(lambda cid, attrs: words))
    assert _status(full) == "full" and _open(full) == []


@pytest.mark.parametrize("operator,note,stated", [
    ("בכפוף", "בכפוף לאישור הוועדה המקומית", "בכפוף לאישור הוועדה המקומית"),
    ("רק", "רק למימוש בתוך שנתיים", "המימוש מותר רק בתוך שנתיים"),
    ("לא כולל", "לא כולל היטל השבחה", "לא כולל היטל השבחה"),
    ("למעט", "למעט שטחי חניה תת קרקעית", "למעט שטחי חניה תת קרקעית"),
])
def test_property_a_condition_in_the_row_note_must_be_stated_in_its_class_whatever_the_wording(cond, operator, note, stated):
    rows = ["שווי הנכס לפי השומה", "פריט | סכום (₪) | הערה", f"שווי המגרש | 3,310,000 | {note}", "שווי המבנה | 905,000 | "]
    quote = f"שווי המגרש | 3,310,000 | {note}"
    bare, *_ = _cond_check("שווי המגרש הוא 3,310,000 ₪ [S2].", quote=quote, rows=rows, label="שווי המגרש", value="3,310,000")
    assert [s["outcome"] for s in bare.item_states()] == ["partial"] and _status(bare) == "partial"
    given, *_ = _cond_check(f"שווי המגרש הוא 3,310,000 ₪, {stated} [S2].", quote=quote, rows=rows, label="שווי המגרש", value="3,310,000")
    assert [s["state"] for s in given.item_states()] == ["covered"] and _status(given) == "full"


# --- a sibling that repeats the value of a stated row is judged by its own label (KTD2; the duplicate-value hole of the first E1 design) ----------------

DUP_PASSAGE = "\n".join(["שיעורי ההיוון שנקבעו בשומה:", "שיעור היוון למשרדים 6.5%", "שיעור היוון למסחר 7.0%", "שיעור היוון למחסנים 7.0%"])
DUP_TABLE = "\n".join(["שיעורי היוון לפי שימוש", "שימוש | שיעור היוון", "משרדים | 6.5%", "מסחר | 7.0%", "מחסנים | 7.0%"])
DUP_ASK = [{"id": "N1", "text": "פירוט שיעורי ההיוון לפי שימוש"}]
DUP_STATED = "שיעור היוון למשרדים 6.5% [S1]. שיעור היוון למסחר 7.0% [S1]."


def _dup_offer(text: str, markdown: str, kind: str):
    from tests.unit.test_items import _units, _ws

    ws = _ws(text, kind=kind)
    return items.candidates(_units(markdown), ws, items.Ledger(), DUP_ASK, "")


@pytest.mark.parametrize("text,kind,stated,labels", [
    (DUP_PASSAGE, "context", DUP_STATED, ["שיעור היוון למחסנים 7.0%"]),
    (DUP_TABLE, "table", "השיעור למשרדים הוא 6.5% [S1]. השיעור למסחר הוא 7.0% [S1].", ["מחסנים"]),
])
def test_a_sibling_that_repeats_a_stated_rows_value_is_offered_when_its_own_label_is_not_stated(monkeypatch, text, kind, stated, labels):
    _switch(monkeypatch, "chat_sibling_anchors")
    assert [c.label for c in _dup_offer(text, stated, kind).candidates] == labels


@pytest.mark.parametrize("text,kind,stated", [
    (DUP_PASSAGE, "context", DUP_STATED + " שיעור היוון למחסנים 7.0% [S1]."),
    (DUP_TABLE, "table", "השיעור למשרדים הוא 6.5% [S1]. השיעור למסחר הוא 7.0% [S1]. השיעור למחסנים הוא 7.0% [S1]."),
])
def test_a_sibling_that_repeats_a_value_and_whose_own_label_is_stated_is_not_offered(monkeypatch, text, kind, stated):
    _switch(monkeypatch, "chat_sibling_anchors")
    assert _dup_offer(text, stated, kind).candidates == []


def test_with_the_setting_off_or_only_the_other_two_on_the_offer_for_duplicate_values_is_the_round_24_one(monkeypatch):
    _switch(monkeypatch)
    assert _dup_offer(DUP_PASSAGE, DUP_STATED, "context").candidates == []  # the sibling that repeats a stated value counts as stated, as before
    _switch(monkeypatch, "chat_condition_check", "chat_own_attribution")
    assert _dup_offer(DUP_PASSAGE, DUP_STATED, "context").candidates == []
