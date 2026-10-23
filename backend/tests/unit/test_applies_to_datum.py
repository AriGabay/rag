"""Round 22 U8 (KTD7; R12, R13, R14; AE3): a condition, a scope or a conditional tag is written next to a datum only when it applies to THAT
datum, and it is decided by structure first.

Item-to-sentence attribution is by ROW: a sentence gives an item only when it cites the item's source and its clause names the item's row - its label,
its designator, a number or date of the row, or a value (V#) of the row. The server's additions are then checked as a separate question from "the source
supports the phrase": does it apply to the datum the clause states? What structure leaves open is asked in the requirements call that already runs (one
question per addition), the verdict is stored in the report, and the audit and the composition read it as data. Synthetic text and values only."""

from __future__ import annotations

import re

import pytest

from app.chat import coverage, items, verify
from app.chat.compose import ADDITION_KINDS, Doc, ref_of
from app.chat.verify import APPLIES, NOT_APPLIES, Applies, verify_answer
from app.providers.llm import CallStatus
from tests.support.scripted_provider import ScriptedProvider
from tests.unit import test_coverage as C

NOTE = "הזכויות חלות רק על פי תכנית מאושרת"
HEADER = "סוג שטח | שטח (מ״ר) | הערה"
NORTH, SOUTH = "שטח קרקע צפונית", "שטח קרקע דרומית"
ROWS = [HEADER, f"{NORTH} | 300 | {NOTE}", f"{SOUTH} | 300 | ללא הערה"]
BOTH_NOTED = [HEADER, f"{NORTH} | 300 | {NOTE}", f"{SOUTH} | 300 | {NOTE}"]
ASK = "שטח הקרקע"


def _item(label: str = NORTH, value: str = "300", **kw):
    return C._adj_item(label, f"{label} | {value}", value, **kw)


def _compose(markdown: str, rows=ROWS, raw=None, *, judge=None, ws=None, ask: str = ASK):
    """The answer verified over the plan file (``rows``) and composed as the server finishes it: (ws, report, doc, answer, turn)."""
    ws = ws if ws is not None else C._plan_file(*rows)
    turn = C._turn({"id": "1", "text": ask})
    answer = C._answer(markdown).model_copy(update={"items": raw if raw is not None else [_item()]})
    report = verify_answer(judge or C._condition_judge(), answer, ws, "?", [], requirements=turn, ledger=items.Ledger())
    edited, _ = coverage.compose_components(ws, answer, report, turn)
    return ws, report, edited.doc, answer, turn


def _notes(doc: Doc) -> list:
    return [s for s in doc.spans() if s.kind == "qualifier"]


def _states(report) -> dict[str, dict]:
    return {x["id"]: x for x in report.item_states()}


# --- AE3: an equal value in another row gets no condition (R13, R14) -------------------------------------------------------------------------

def test_ae3_the_equal_value_of_another_row_gets_no_condition_and_the_item_is_not_given_by_that_sentence():
    ws, report, doc, *_ = _compose("שטח הקרקע הדרומית הוא 300 מ״ר [S2].")
    assert NOTE not in doc.text and _notes(doc) == [], doc.text
    assert _states(report)["I1"]["state"] == "omitted" and _states(report)["I1"]["covered_by"] is None
    assert "«שטח קרקע צפונית»" in doc.text  # the row that was read and not used is said by the server's own line
    # the control: the sentence about the row that carries the condition gets it
    _, _, doc, *_ = _compose("שטח הקרקע הצפונית הוא 300 מ״ר [S2].")
    assert doc.text.startswith(f"שטח הקרקע הצפונית הוא 300 מ״ר (התנאי לפי המקור: {NOTE}) [S2].") and len(_notes(doc)) == 1


def test_ae3_a_designator_names_the_row_the_same_way_a_single_letter_plot_a_condition_stays_off_plot_b():
    rows = [HEADER, f"שטח מגרש א | 300 | {NOTE}", "שטח מגרש ב | 300 | ללא הערה"]
    item = _item("שטח מגרש א")
    _, report, doc, *_ = _compose("שטח מגרש ב הוא 300 מ״ר [S2].", rows, [item])
    assert NOTE not in doc.text and _states(report)["I1"]["state"] == "omitted"
    _, _, doc, *_ = _compose("שטח מגרש א הוא 300 מ״ר [S2].", rows, [item])
    assert NOTE in doc.text and len(_notes(doc)) == 1


def test_a_sentence_that_names_no_row_while_two_rows_hold_the_value_gives_neither_row_and_writes_no_condition():
    ws, report, doc, *_ = _compose("השטח הוא 300 מ״ר [S2].")
    assert NOTE not in doc.text and _states(report)["I1"]["state"] == "omitted"
    # a value only one row holds names its row by itself
    rows = [HEADER, f"{NORTH} | 300 | {NOTE}", f"{SOUTH} | 400 | ללא הערה"]
    _, _, doc, *_ = _compose("השטח הוא 300 מ״ר [S2].", rows)
    assert NOTE in doc.text


def test_a_value_cited_from_the_other_row_does_not_give_the_item_and_one_from_its_own_row_does():
    import uuid
    from decimal import Decimal

    from app.chat import calc

    ws = C._plan_file(*ROWS)

    def add(vid: str, row: str) -> None:
        ws.values[vid] = calc.Value(vid, Decimal(300), "300", "S2", ws.sources["S2"].document_id, uuid.uuid4(), None, "דוח", "סעיף", row.split(" | ")[0], "area", "sqm",
                                    "none", "unknown", "", "נכס הדגמה", "other", {"unit": "source"}, {"quote": row}, row,
                                    context={"key": ws.sources["S2"].unit["id"], "number": 1, "label": "שומה א", "described": "שומה א"})

    add("V1", f"{NORTH} | 300 | {NOTE}")
    add("V2", f"{SOUTH} | 300 | ללא הערה")
    _, _, doc, *_ = _compose("השטח הוא 300 מ״ר [V2].", ws=ws)
    assert NOTE not in doc.text
    _, _, doc, *_ = _compose("השטח הוא 300 מ״ר [V1].", ws=ws)
    assert NOTE in doc.text


# --- two rows with the same condition both get it; one sentence that merges them gets it once, with its scope ---------------------------------

def test_two_rows_that_carry_the_same_condition_both_get_it_each_on_its_own_sentence():
    raw = [_item(NORTH), _item(SOUTH)]
    _, report, doc, *_ = _compose("שטח הקרקע הצפונית הוא 300 מ״ר [S2]. שטח הקרקע הדרומית הוא 300 מ״ר [S2].", BOTH_NOTED, raw)
    states = _states(report)
    assert (states["I1"]["covered_by"], states["I2"]["covered_by"]) == (0, 1), states  # each row is given by its own sentence
    assert doc.text.count(NOTE) == 2 and [s.datum for s in _notes(doc)] == ["I1", "I2"], doc.text


def test_one_sentence_that_merges_two_rows_with_the_same_condition_gets_it_once_for_both():
    raw = [_item(NORTH), _item(SOUTH)]
    _, _, doc, *_ = _compose("שטח הקרקע הצפונית והדרומית הוא 300 מ״ר [S2].", BOTH_NOTED, raw)
    assert doc.text.count(NOTE) == 1, doc.text
    (note,) = _notes(doc)
    assert set(note.datum.split(",")) == {"I1", "I2"}


def test_one_sentence_that_merges_two_rows_writes_the_condition_of_one_of_them_once_and_names_the_row_it_is_about():
    _, _, doc, *_ = _compose("שטח הקרקע הצפונית והדרומית הוא 300 מ״ר [S2].")
    assert doc.text.count(NOTE) == 1, doc.text
    assert f"(התנאי לפי המקור של {NORTH}: {NOTE}) [S2]" in doc.text  # its scope: the row the condition belongs to
    (note,) = _notes(doc)
    assert note.datum == "I1" and note.addition == "condition"


# --- the source around the table, and the order the search returned it in, change nothing ---------------------------------------------------

def test_an_unrelated_paragraph_before_the_table_changes_nothing():
    before = _compose("שטח הקרקע הצפונית הוא 300 מ״ר [S2].", ROWS)
    rows = ["בדיקה קודמת של הנכס: שטח של 300 מ״ר נמדד בסיור, והזכויות בו נבדקו בנפרד.", *ROWS]
    after = _compose("שטח הקרקע הצפונית הוא 300 מ״ר [S2].", rows)
    assert after[2].text == before[2].text
    assert [(s.kind, s.datum) for s in after[2].spans()] == [(s.kind, s.datum) for s in before[2].spans()]
    south_before = _compose("שטח הקרקע הדרומית הוא 300 מ״ר [S2].", ROWS)
    south_after = _compose("שטח הקרקע הדרומית הוא 300 מ״ר [S2].", rows)
    assert south_after[2].text == south_before[2].text and NOTE not in south_after[2].text


NORTH_TABLE = "\n".join([HEADER, f"{NORTH} | 1,200 | {NOTE}"])
SOUTH_TABLE = "\n".join([HEADER, f"{SOUTH} | 1,200 | ללא הערה"])


def _two_tables(first: str, second: str):
    ws = C._file(C.SOURCE, first, second, units=(1, 1, 1), declared=(1,))
    ws.sources["S2"].kind = ws.sources["S3"].kind = "table"
    return ws


@pytest.mark.parametrize("order", [(NORTH_TABLE, SOUTH_TABLE), (SOUTH_TABLE, NORTH_TABLE)])
def test_reordering_the_search_results_does_not_move_a_condition_to_another_property(order):
    """The same two tables, once in each order: S2 and S3 swap their texts, and the answer cites each by the handle it has. The row that carries the
    condition keeps it, and the other table's equal value (four digits: another passage of the same document holds it) never receives it."""
    ws = _two_tables(*order)
    north_sid = "S2" if order[0] == NORTH_TABLE else "S3"
    south_sid = "S3" if north_sid == "S2" else "S2"
    item = C._adj_item(NORTH, f"{NORTH} | 1,200", "1,200", source=north_sid)
    _, report, doc, *_ = _compose(f"שטח הקרקע הדרומית הוא 1,200 מ״ר [{south_sid}].", raw=[item], ws=ws)
    assert NOTE not in doc.text and _states(report)["I1"]["state"] == "omitted", doc.text
    _, report, doc, *_ = _compose(f"שטח הקרקע הצפונית הוא 1,200 מ״ר [{north_sid}].", raw=[item], ws=_two_tables(*order))
    assert doc.text.startswith(f"שטח הקרקע הצפונית הוא 1,200 מ״ר (התנאי לפי המקור: {NOTE}) [{north_sid}].") and len(_notes(doc)) == 1


def test_r12_another_appraisal_unit_of_the_same_file_with_more_passages_does_not_lock_the_property_of_this_one():
    """One physical file, two appraisal units: both tables are in the file of the same title, and the second unit has two passages that hold the
    value. A sentence that cites the second unit's passages never gives the first unit's row, and takes none of its conditions."""
    ws = C._file(C.SOURCE, NORTH_TABLE, SOUTH_TABLE, SOUTH_TABLE + "\nשטח נוסף | 1,200 | ללא הערה", units=(1, 1, 2, 2), declared=(1,))
    for sid in ("S2", "S3", "S4"):
        ws.sources[sid].kind = "table"
    item = C._adj_item(NORTH, f"{NORTH} | 1,200", "1,200", source="S2")
    _, report, doc, *_ = _compose("שטח הקרקע הוא 1,200 מ״ר [S3][S4].", raw=[item], ws=ws)
    assert NOTE not in doc.text and _states(report)["I1"]["state"] == "omitted", doc.text
    # and the same unit's own passage still gives it, with its condition
    _, _, doc, *_ = _compose("שטח הקרקע הצפונית הוא 1,200 מ״ר [S2].", raw=[item], ws=ws)
    assert NOTE in doc.text


# --- what structure leaves open is asked inside the existing requirements call; the verdict is stored; the audit reads it as data -----------------

@pytest.fixture
def undecided(monkeypatch):
    """Two rows that nothing in their labels, designators, numbers or dates tells apart (structure cannot decide which one a clause states)."""
    real = items.row_status
    monkeypatch.setattr(items, "row_status", lambda *a, **k: items.Row(items.ROW_INDISTINCT))
    return real


def _addition_judge(answer, seen: list | None = None):
    """The condition judge that also answers the addition questions of the call: ``answer(addition_id, input)`` is applies, not_applies, unknown or None."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        out = {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]}
        if "<requirements>" in input:
            ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
            asked = re.findall(r'<addition id="(A\d+)"', input)
            replies = [{"addition": a, "outcome": got} for a in asked if (got := answer(a, input)) is not None]
            out["requirements"] = [C._req("", "full", list(units), id=i) | ({"applies": replies} if n == 0 else {}) for n, i in enumerate(ids)]
        return out

    p.on(verify.Purpose.VERIFY, respond, repeat=True)
    return p


SENTENCE = "שטח הקרקע הצפונית הוא 300 מ״ר [S2]."


def test_an_undecided_addition_is_one_question_in_the_existing_call_and_an_applies_answer_writes_it_inline(undecided):
    seen: list[str] = []
    _, report, doc, *_ = _compose(SENTENCE, judge=_addition_judge(lambda a, i: "applies", seen))
    assert len(seen) == 1 and seen[0].count("<addition ") == 1, seen  # one call, one question: never a call per cell
    assert f"<other_rows>{SOUTH} | 300 | ללא הערה</other_rows>" in seen[0] and f"<row>{NORTH} | 300 | {NOTE}</row>" in seen[0]
    assert f"(התנאי לפי המקור: {NOTE})" in doc.text and report.unbound_data() == []
    ((key, got),) = [(k, v) for k, v in report.applies.items() if k[0] == "condition"]
    assert key[:2] == ("condition", "I1") and (got.verdict, got.by) == (APPLIES, "judge")


def test_a_not_applies_answer_writes_nothing_inline_and_the_uncertainty_line_names_the_datum(undecided):
    _, report, doc, *_ = _compose(SENTENCE, judge=_addition_judge(lambda a, i: "not_applies"))
    assert NOTE not in doc.text.split("\n\n")[0] and _notes(doc) == []
    assert report.applies[("condition", "I1", "")].verdict == NOT_APPLIES
    line = report.uncertainty_line()
    assert line and NORTH in line and NOTE in line and line in doc.text


@pytest.mark.parametrize("reply", [None, "unknown"])
def test_an_undecided_addition_the_judge_does_not_decide_gets_the_uncertainty_line_and_nothing_inline(undecided, reply):
    _, report, doc, *_ = _compose(SENTENCE, judge=_addition_judge(lambda a, i: reply))
    assert _notes(doc) == [] and NOTE in report.uncertainty_line() and report.uncertainty_line() in doc.text
    assert report.applies[("condition", "I1", "")].verdict == ""  # nothing was decided: no verdict that it applies


def test_a_judge_that_times_out_leaves_no_addition_inline_the_verification_is_unavailable(undecided):
    """The question travels in the requirements call: when that call cannot be answered (after its retry) there is no verification at all - the
    answer is never composed with a note that nothing decided."""
    p = ScriptedProvider()
    p.on(verify.Purpose.VERIFY, CallStatus.TIMEOUT, repeat=True)
    with pytest.raises(verify.VerificationUnavailable):
        _compose(SENTENCE, judge=p)


def test_an_answer_for_a_question_the_call_did_not_ask_is_dropped():
    cov = verify._Coverage(C._turn({"id": "1", "text": ASK}), [], "?", additions=[{"id": "A1", "unit": 2}])
    vote = verify.JudgeRequirement(status="full", applies=[verify.AppliesVerdict(addition="A1", outcome="applies"),
                                                          verify.AppliesVerdict(addition="A9", outcome="applies")])
    assert cov.valid_applies(vote, {0, 1}) == []  # the unit of the addition was not in this call
    assert [a.addition for a in cov.valid_applies(vote, {2})] == ["A1"]  # an id the call never asked is dropped
    assert verify.JudgeCoverageOutput.model_validate({"verdicts": [], "requirements": [{"id": "N1", "status": "full"}]}).requirements[0].applies == []


# --- round 22 review #1: an addition answer belongs to the call that asked it (A# is renumbered every verification round) ---------------------

NORTH_SENTENCE, SOUTH_SENTENCE = "שטח הקרקע הצפונית הוא 300 מ״ר [S2].", "שטח הקרקע הדרומית הוא 300 מ״ר [S2]."
NOTE_SOUTH = "השטח כפוף להיתר בנייה בתוקף"
TWO_NOTES = [HEADER, f"{NORTH} | 300 | {NOTE}", f"{SOUTH} | 300 | {NOTE_SOUTH}"]
NORTH_STATED = f"שטח הקרקע הצפונית הוא 300 מ״ר, {NOTE} [S2]."  # the repair writes the north row's condition itself: no addition is asked for it


def _two_row_round(markdown: str, cache, ledger, turn, ws, seen: list, first_answers: dict[str, str]):
    """One verification round of the two-row answer (the south sentence is cited by the FIRST requirement, so the addition answers travel on the vote
    that a later round carries when only the north sentence changed). ``first_answers``: the answers of the call, by A#."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        south = [i for i, t in units.items() if "הדרומית" in t]
        north = [i for i, t in units.items() if "הצפונית" in t]
        out = {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]}
        if "<requirements>" in input:
            asked = re.findall(r'<addition id="(A\d+)"', input)
            replies = [{"addition": a, "outcome": first_answers[a]} for a in asked if a in first_answers]
            ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
            out["requirements"] = [C._req("", "full", south if i == "N1" else north, id=i) | ({"applies": replies} if n == 0 else {})
                                   for n, i in enumerate(ids)]
        return out

    p.on(verify.Purpose.VERIFY, respond, repeat=True)
    answer = C._answer(markdown).model_copy(update={"items": [_item(), _item(SOUTH, component="N2")]})
    return verify_answer(p, answer, ws, "?", [], requirements=turn, ledger=ledger, cache=cache), answer


def test_a_renumbered_plan_never_reads_the_answer_of_a_different_addition_from_a_carried_vote(undecided):
    ws = C._plan_file(*TWO_NOTES)
    turn = C._turn({"id": "1", "text": "שטח הקרקע הצפונית"}, {"id": "2", "text": "שטח הקרקע הדרומית"})
    cache, ledger, seen = verify.VerdictCache(), items.Ledger(), []
    # round 1: A1 = the north row's note (applies), A2 = the south row's (not_applies)
    first, _ = _two_row_round(f"{NORTH_SENTENCE}\n\n{SOUTH_SENTENCE}", cache, ledger, turn, ws, seen,
                              {"A1": "applies", "A2": "not_applies"})
    assert [(a.id, a.datum) for a in first.additions] == [("A1", "I1"), ("A2", "I2")], first.additions
    # round 2: the repair wrote the north row's condition itself, so the plan is A1 = the south row's note; the call shows only the changed
    # sentence and does not answer; the vote of the south-cited requirement is carried from round 1 with its answers
    markdown = f"{NORTH_STATED}\n\n{SOUTH_SENTENCE}"
    second, answer = _two_row_round(markdown, cache, ledger, turn, ws, seen, {})
    assert [(a.id, a.datum) for a in second.additions] == [("A1", "I2")], second.additions
    assert second.reused >= 1  # the south sentence's verdict and the vote resting on it were carried
    edited, _ = coverage.compose_components(ws, answer, second, turn)
    south_sentence = [p for p in edited.doc.text.split("\n\n") if "הדרומית" in p][0]
    assert NOTE_SOUTH not in south_sentence, edited.doc.text  # the judge ruled the south note out in round 1: never written beside the south row now
    assert [s.datum for s in _notes(edited.doc)] != ["I2"]


def test_an_addition_answer_is_read_by_what_it_was_asked_about_not_by_its_position():
    stale = verify.JudgeRequirement(status="full", applies=[verify.AppliesVerdict(addition="A1", outcome="applies"),
                                                            verify.AppliesVerdict(addition="A2", outcome="not_applies")])
    stale._asked = {"A1": ("condition", "I1", 5), "A2": ("condition", "I2", 8)}
    fresh = verify.JudgeRequirement(status="full", applies=[verify.AppliesVerdict(addition="A1", outcome="applies")])
    fresh._asked = {"A1": ("condition", "I2", 8)}  # this round's A1 is the old A2's addition
    unknown = verify.JudgeRequirement(status="full", applies=[verify.AppliesVerdict(addition="A1", outcome="applies")])  # a vote that knows nothing of what it asked
    assert verify._addition_answers([stale]) == {("condition", "I1", 5): "applies", ("condition", "I2", 8): "not_applies"}
    assert verify._addition_answers([fresh]) == {("condition", "I2", 8): "applies"}  # the positive control: a fresh answer is read for its own addition
    assert verify._addition_answers([unknown]) == {}
    assert verify._addition_answers([stale, fresh])[("condition", "I2", 8)] == "not_applies"  # two answers to one addition: the careful one


def test_a_vote_the_cache_reuses_for_moved_units_keeps_no_addition_answer():
    cache = verify.VerdictCache()
    vote = verify.JudgeRequirement(id="N1", status="full", units=[1], applies=[verify.AppliesVerdict(addition="A1", outcome="applies")])
    vote._asked = {"A1": ("condition", "I1", 1)}
    cache.verdicts["k1"] = verify.JudgeVerdict(index=0, verdict="supported", reason="x")
    cache.last = verify._Round(["k0", "k1"], [vote], ())
    unit = type("U", (), {"index": 0})()
    _, carried, _ = cache.reuse([unit], [unit], {0: "k1"})
    assert [v.units for v in carried] == [[0]] and carried[0].applies == [] and carried[0]._asked is None


def test_a_decided_addition_asks_nothing_so_the_requirements_call_is_what_it_was():
    seen: list[str] = []
    _compose(SENTENCE, judge=_addition_judge(lambda a, i: "applies", seen))
    assert "<additions>" not in seen[0]


def test_the_audit_with_a_stored_not_applies_verdict_removes_the_inline_addition_and_names_it():
    ws, report, doc, *_ = _compose(SENTENCE)
    (note,) = _notes(doc)
    assert note.datum == "I1" and note.addition == "condition" and NOTE in doc.text
    claim = note.claim_ids[0]
    assert report.applies[("condition", "I1", claim)].verdict == APPLIES  # decided by structure: the clause names the row
    report.applies[("condition", "I1", claim)] = Applies(NOT_APPLIES, "judge", label=NORTH, value="300", detail=NOTE)
    assert coverage.audit_server_spans(ws, report, doc) == 1
    assert _notes(doc) == [] and NOTE not in doc.text
    assert report.unbound_data()[0].datum == "I1" and NORTH in report.uncertainty_line()


def test_an_addition_with_no_recorded_verdict_is_not_written_inline_by_the_composition():
    ws, report, _, answer, turn = _compose(SENTENCE)
    report.applies.clear()
    edited, _ = coverage.compose_components(ws, answer, report, turn)
    assert NOTE not in edited.doc.text.split("\n\n")[0] and _notes(edited.doc) == []
    assert NOTE in report.uncertainty_line() and report.uncertainty_line() in edited.doc.text


def test_doc_audit_reads_the_verdict_as_data_and_leaves_every_other_span_alone():
    doc = Doc("שטח 300 מ״ר [S2].")
    doc.edit([(9, 9, " (תנאי)", "qualifier", ref_of(["k1"], "I1", "condition")), (9, 9, " (עוד)", "qualifier", ref_of(["k1"], "I1")),
              (9, 9, " (שלישי)", "scope_note", ref_of(["k2"], "V1", "scope"))])
    kept = {"scope"}
    asked: list[str] = []

    def applies(span) -> bool:
        asked.append(span.addition)
        return span.addition in kept

    assert doc.audit(lambda span: None, applies=applies) == 1
    assert sorted(s.addition for s in doc.spans()) == ["", "scope"] and "תנאי" not in doc.text and "עוד" in doc.text
    assert set(asked) == {"condition", "scope"}  # the span with no addition kind is never asked
    assert doc.audit(lambda span: None, applies=applies) == 0  # a second run changes nothing
    assert ADDITION_KINDS == ("condition", "scope", "conditional")


def test_the_span_registry_carries_the_kind_of_addition():
    doc = Doc("שטח 300 מ״ר [S2].")
    doc.edit([(9, 9, " (תנאי)", "qualifier", ref_of(["k1"], "I1", "condition"))])
    (span,) = doc.spans()
    assert (span.claim_ids, span.datum, span.addition) == (("k1",), "I1", "condition")
    assert span.as_dict()["addition"] == "condition"
    plain = Doc("שטח 300 מ״ר [S2].")
    plain.edit([(9, 9, " (תנאי)", "qualifier", ref_of(["k1"], "I1"))])
    assert "addition" not in plain.spans()[0].as_dict()  # a stored span without the field reads as no addition


def test_the_conditional_tag_is_decided_by_structure_and_dropped_when_the_stored_verdict_says_it_does_not_apply():
    from tests.unit import test_verify_computed as VC

    ws = VC._conditional_sum()
    answer = VC._answer("עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].")
    report = verify_answer(VC._judge(lambda t: "supported"), answer, ws, VC.COST_QUESTION, [])
    edited, _ = coverage.compose_components(ws, answer, report)
    (tag,) = [s for s in edited.doc.spans() if s.addition == "conditional"]
    assert tag.datum == "C1" and "(תוצאה מותנית)" in edited.doc.text
    assert report.applies[("conditional", "C1", "")].verdict == APPLIES  # the sentence states the calculation's result
    report.applies[("conditional", "C1", "")] = Applies(NOT_APPLIES, "judge")
    again, _ = coverage.compose_components(ws, answer, report)
    assert "(תוצאה מותנית)" not in again.doc.text and [s for s in again.doc.spans() if s.addition == "conditional"] == []


def test_the_scope_note_is_decided_by_structure_and_dropped_when_the_stored_verdict_says_it_does_not_apply():
    from tests.unit import test_verify_edit_ladder as L

    ws = L._tables(L._table())
    answer, report = L._scoped(L.SCOPE_UNIT, ws, L._scope_judge())
    edited, _ = coverage.compose_components(ws, answer, report)
    (note,) = [s for s in edited.doc.spans() if s.addition == "scope"]
    claim = note.claim_ids[0] if note.claim_ids else ""
    assert report.applies_for("scope", note.datum, claim).verdict == APPLIES and L.SCOPE_ADDED in edited.doc.text
    report.applies.clear()  # no verdict recorded: the scope is not written inline either
    again, _ = coverage.compose_components(ws, answer, report)
    assert L.SCOPE_ADDED not in again.doc.text and [s for s in again.doc.spans() if s.addition == "scope"] == []
    assert report.unbound_data() and report.unbound_data()[0].kind == "scope"
