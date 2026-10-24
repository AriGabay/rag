# ruff: noqa: F811 - the tests take the imported ``offline`` fixture by name, as the round-13, round-14 and round-20 sequences do
"""Round 23 U1 (M1): the gap and coverage lines are decided by the datum, not by the form of a citation (R1, R2, R19, R20; KTD3-KTD6;
AE1, AE2). A table cited once on its lead-in states its rows; a twin's shared value stands in a lead-in; a date range is a range; a judge's
"missing part" is an item only when it is a stored structural span; a removal says what happened. Every text, name and value is invented."""

from __future__ import annotations

import re

import pytest

from app.chat import coverage, items, verify
from tests.unit.test_coverage import (
    SOURCE,
    _adj_item,
    _answer,
    _file,
    _finish,
    _item_lines,
    _ledger_check,
    _req,
)
from tests.unit.test_coverage import _ws as _coverage_ws
from tests.unit.test_final_check import offline  # noqa: F401 - the fixture, taken by name
from tests.unit.test_items import _item, _units, _ws

FIVE = [("מיקום", "5%"), ("קומה", "3%"), ("גיל הבניין", "2%"), ("מרפסת", "1%"), ("חניה", "4%")]
TABLE = "\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in FIVE)])


def _row_item(label: str, value: str, **kw) -> items.Item:
    return _item(label=f"ההתאמה ל{label}", quote=f"{label} | {value}", value=value, **kw)


def _ledger_of(*its: items.Item) -> items.Ledger:
    ledger = items.Ledger()
    ledger.merge(list(its))
    return ledger


def _states(ws, markdown: str, *its: items.Item) -> dict[str, dict]:
    """The state of each item (by its quote's label) against the answer, as ``Ledger.outcomes`` decides it."""
    ledger = _ledger_of(*its)
    return {o["quote"].split(" | ")[0]: o for o in ledger.outcomes(_units(markdown), ws)}


def _table(rows: list[tuple[str, str]], *, lead: str = "", head_cite: str = "", row_cites: dict[str, str] | None = None) -> str:
    """A Markdown table of ``rows`` (label, value); ``lead``: the line above it; ``row_cites``: a citation written at the end of a row."""
    body = [f"| {k} | {v}{(' ' + (row_cites or {}).get(k, '')).rstrip()} |" for k, v in rows]
    return (f"{lead}\n\n" if lead else "") + "\n".join([f"| סוג התאמה | שיעור{head_cite} |", "|---|---|", *body])


# --- sub-fix 2 (KTD5): a table cited once on its lead-in or header gives its rows ---------------------------------------------------

def test_a_table_cited_once_on_its_lead_in_gives_every_row_it_states():
    ws = _ws(TABLE)
    its = [_row_item(k, v) for k, v in FIVE]
    got = _states(ws, _table(FIVE, lead="ההתאמות שהשומה יישמה [S1]:"), *its)
    assert {k: o["state"] for k, o in got.items()} == {k: "covered" for k, _ in FIVE}


def test_a_table_cited_once_on_its_header_row_gives_its_rows_and_one_row_missing_leaves_exactly_that_item_open():
    ws = _ws(TABLE)
    its = [_row_item(k, v) for k, v in FIVE]
    got = _states(ws, _table(FIVE[:4], lead="ההתאמות שהשומה יישמה:", head_cite=" [S1]"), *its)
    assert {k: o["state"] for k, o in got.items()} == {**{k: "covered" for k, _ in FIVE[:4]}, "חניה": "omitted"}


def test_a_row_that_cites_another_source_keeps_that_attribution_whatever_the_lead_in_cites():
    other = "התאמות לנכס אחר\nסוג התאמה | שיעור\nמיקום | 5%\nקומה | 3%"
    ws = _ws(TABLE, other)
    item = _row_item("מיקום", "5%")
    both = _table([("מיקום", "5%"), ("קומה", "3%")], lead="ההתאמות [S1]:", row_cites={"מיקום": "[S2]"})
    assert _states(ws, both, item)["מיקום"]["state"] == "omitted"  # the row cites plan B (S2): plan A's item is not given by it
    own = _table([("מיקום", "5%")], lead="ההתאמות [S1]:")
    assert _states(ws, own, item)["מיקום"]["state"] == "covered"


def test_a_lead_in_that_cites_a_source_of_another_document_gives_no_row():
    other = "התאמות לנכס אחר\nסוג התאמה | שיעור\nמיקום | 5%"
    ws = _ws(TABLE, other)
    assert _states(ws, _table([("מיקום", "5%")], lead="ההתאמות [S2]:"), _row_item("מיקום", "5%"))["מיקום"]["state"] == "omitted"


# --- sub-fix 1 (KTD4): the datum audit inside the one coverage decision ---------------------------------------------------------------

def _adj(rows: list[tuple[str, str]]):
    """The adjustments table (source S2 of ``_file``) and the agent's items for ``rows``."""
    from tests.unit.test_coverage import ADJ_TEXT  # noqa: F401 - the table of the file is the module's own

    return [_adj_item(f"ההתאמה ל{k}", f"{k} | {v}", v) for k, v in rows]


ADJ_ROWS = [("מיקום", "5%"), ("קומה", "3%"), ("גיל הבניין", "2%")]


def _adj_report(markdown: str, rows=ADJ_ROWS, **kw):
    from tests.unit.test_coverage import _adj_file

    ws = _adj_file()
    r, turn, answer, ledger = _ledger_check(ws, markdown, _adj(rows), **kw)
    return ws, r, turn, answer


def test_a_bullet_that_states_the_datum_without_a_citation_is_no_read_and_not_used_line():
    ws, r, turn, answer = _adj_report(
        "- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].\n- ההתאמה לגיל הבניין היא 2%.")
    final, outcomes = _finish(ws, answer, r, turn)
    assert _item_lines(final) == [] and [s["state"] for s in r.item_states()] == ["covered"] * 3
    assert outcomes[0]["status"] == "full" and r.item_gaps == []


def test_the_audit_asks_the_rows_words_with_its_numbers_a_bare_number_in_an_uncited_sentence_gives_nothing():
    ws, r, turn, answer = _adj_report("- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].\n- שיעור כלשהו הוא 2%.")
    final, _ = _finish(ws, answer, r, turn)
    assert [ln.split("»")[0] for ln in _item_lines(final)] == ["«גיל הבניין"]


def test_the_audit_does_not_give_an_item_to_a_sentence_that_cites_something_else():
    ws, r, turn, answer = _adj_report(
        "- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].\n- ההתאמה לגיל הבניין היא 2% [S1].")
    final, _ = _finish(ws, answer, r, turn)
    assert [ln.split("»")[0] for ln in _item_lines(final)] == ["«גיל הבניין"]


def test_a_covered_item_the_audit_gives_keeps_its_condition_check():
    ws = _ws("\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור | הערה", "קומה | 3% | התאמה לפי קומה",
                        "גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה"]))
    age = _item(label="ההתאמה לגיל הבניין", quote="גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה", value="2%")
    bare = _ledger_of(age).outcomes(_units("- ההתאמה לגיל הבניין היא 2%."), ws, audit=True)[0]
    assert bare["state"] == "covered" and bare["condition_check"] in ("undecided", "missing")  # the condition is still asked
    stated = _ledger_of(age).outcomes(_units("- ההתאמה לגיל הבניין היא 2%, כאשר הבניין מעל גיל 30 שנה."), ws, audit=True)[0]
    assert stated["state"] == "covered" and stated["condition_check"] == "kept"


# --- sub-fix 3: twin items - the shared value and the words that tell them apart may stand in the block and its lead-in --------------

TWIN_TEXT = "\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בקומות עליונות | 7%", "חנויות בקומת קרקע | 7%"])


def _twins():
    upper = _item(label="שיעור למשרדים", quote="משרדים בקומות עליונות | 7%", value="7%", condition="משרדים בקומות עליונות")
    ground = _item(label="שיעור לחנויות", quote="חנויות בקומת קרקע | 7%", value="7%", condition="חנויות בקומת קרקע")
    return upper, ground


def _twin_states(markdown: str) -> dict[str, str]:
    upper, ground = _twins()
    ws = _ws(TWIN_TEXT)
    got = _states(ws, markdown, upper, ground)
    return {"upper": got["משרדים בקומות עליונות"]["state"], "ground": got["חנויות בקומת קרקע"]["state"]}


def test_twins_a_shared_value_in_the_lead_in_sentence_and_the_distinguishing_words_in_the_blocks_below_are_both_covered():
    got = _twin_states("שיעור ההיוון הוא 7% בשני השימושים [S1]:\n- משרדים בקומות עליונות [S1]\n- חנויות בקומת קרקע [S1]")
    assert got == {"upper": "covered", "ground": "covered"}


def test_twins_a_block_below_the_lead_in_that_names_only_one_twin_gives_only_that_twin():
    got = _twin_states("שיעור ההיוון הוא 7% [S1]:\n- משרדים בקומות עליונות [S1]")
    assert got == {"upper": "covered", "ground": "omitted"}


def test_twins_the_distinguishing_words_in_the_lead_in_and_the_value_in_the_block_below_give_that_twin():
    got = _twin_states("לגבי משרדים בקומות עליונות:\n- שיעור ההיוון הוא 7% [S1].")
    assert got == {"upper": "covered", "ground": "omitted"}


def test_twins_a_lead_in_without_the_value_gives_a_block_that_names_a_twin_nothing():
    got = _twin_states("שיעורי ההיוון שנקבעו [S1]:\n- משרדים בקומות עליונות [S1]\n- חנויות בקומת קרקע [S1]")
    assert got == {"upper": "omitted", "ground": "omitted"}


def test_twins_a_value_in_a_lead_in_does_not_give_an_item_that_has_no_twin():
    ws = _ws("\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בקומות עליונות | 7%", "חנויות בקומת קרקע | 8%"]))
    upper = _item(label="שיעור למשרדים", quote="משרדים בקומות עליונות | 7%", value="7%", condition="משרדים בקומות עליונות")
    ground = _item(label="שיעור לחנויות", quote="חנויות בקומת קרקע | 8%", value="8%", condition="חנויות בקומת קרקע")
    got = _states(ws, "שיעור ההיוון הוא 7% [S1]:\n- חנויות בקומת קרקע [S1]", upper, ground)
    assert got["חנויות בקומת קרקע"]["state"] == "omitted"  # its own value, 8%, is nowhere


def test_a_lead_in_gives_its_date_to_every_item_of_its_list_not_only_to_the_first():
    dated = "שיעור היוון ליום 31.12.2023 – 7.5%"
    ws = _ws(dated, "מקדם התאמה ליום 31.12.2023 – 0.9")
    first = _item(label="שיעור ההיוון", quote=dated, value="7.5%")
    second = _item(label="מקדם ההתאמה", quote="מקדם התאמה ליום 31.12.2023 – 0.9", value="0.9", source="S2")
    markdown = "הנתונים נכון ליום 31.12.2023:\n- שיעור ההיוון הוא 7.5% [S1].\n- מקדם ההתאמה הוא 0.9 [S2]."
    got = _states(ws, markdown, first, second)
    assert {o["state"] for o in got.values()} == {"covered"}
    late = _states(ws, markdown.replace("31.12.2023", "30.06.2024"), first, second)
    assert {o["why"] for o in late.values()} == {"date_not_stated"}


# --- sub-fix 4: a date range is a range; the quote's words ask for a value the label's words do not -----------------------------------

LEASE = "\n".join(["תקופת השכירות", "נושא | תקופה", "תקופת שכירות | 1.1.2025 – 31.12.2026"])


def _lease() -> items.Item:
    return _item(label="תקופת השכירות", quote="תקופת שכירות | 1.1.2025 – 31.12.2026", value="1.1.2025 עד 31.12.2026")


@pytest.mark.parametrize("stated", [
    "תקופת השכירות היא מ-1.1.2025 ועד 31.12.2026 [S1].",
    "תקופת השכירות היא 1.1.2025 – 31.12.2026 [S1].",
    "תקופת השכירות מתחילה ב-1.1.2025 ומסתיימת ב-31.12.2026 [S1].",
    "תקופת השכירות היא מינואר 2025 עד דצמבר 2026 [S1].",
])
def test_a_date_range_value_is_covered_when_the_answer_states_the_range(stated):
    got = _states(_ws(LEASE), stated, _lease())["תקופת שכירות"]
    assert got["state"] == "covered", got["why"]


@pytest.mark.parametrize("stated", [
    "תקופת השכירות מתחילה ב-1.1.2025 [S1].",
    "תקופת השכירות מסתיימת ב-31.12.2026 [S1].",
])
def test_a_date_range_value_is_partial_when_the_answer_states_only_one_end(stated):
    got = _states(_ws(LEASE), stated, _lease())["תקופת שכירות"]
    assert (got["state"], got["outcome"]) == ("omitted", "partial") and got["missing"], got
    assert "31.12.2026" in got["missing"][0] or "1.1.2025" in got["missing"][0]


def test_a_date_range_value_is_not_given_by_another_range_or_another_year():
    ws = _ws(LEASE)
    for other in ("תקופת השכירות היא 1.1.2025 – 31.12.2027 [S1].", "תקופת השכירות היא 1.1.2024 – 31.12.2026 [S1]."):
        assert _states(ws, other, _lease())["תקופת שכירות"]["state"] == "omitted"


def test_a_single_date_value_is_still_stated_by_its_date():
    ws = _ws("\n".join(["תאריך", "נושא | מועד", "מועד האכלוס | 1.3.2025"]))
    item = _item(label="מועד האכלוס", quote="מועד האכלוס | 1.3.2025", value="1.3.2025")
    assert _states(ws, "האכלוס ב-1.3.2025 [S1].", item)["מועד האכלוס"]["state"] == "covered"
    assert _states(ws, "האכלוס ב-1.4.2025 [S1].", item)["מועד האכלוס"]["state"] == "omitted"


def test_a_value_with_digits_the_quote_does_not_write_is_stated_by_the_quotes_words_when_the_labels_are_not_in_the_block():
    ws = _ws("\n".join(["פרטי הפרויקט", "נושא | נתון", "יחידות דיור | שתים עשרה"]))
    item = _item(label="ההיקף הכולל", quote="יחידות דיור | שתים עשרה", value="12 יחידות")
    assert _states(ws, "בפרויקט יש שתים עשרה יחידות דיור [S1].", item)["יחידות דיור"]["state"] == "covered"
    assert _states(ws, "בפרויקט יש יחידות מסחר [S1].", item)["יחידות דיור"]["state"] == "omitted"


# --- sub-fix 5 (KTD6): the judge's "missing part" is an item only when it is a stored structural span ------------------------------------

CAPTION = "טבלה 4: שיעורי היוון בשנת 2023"
FOOTNOTE = "(1) השיעור לפני תיקון בגין הצמדה"
CAPTIONED = "\n".join([CAPTION, "שימוש | שיעור", "משרדים | 6.5%", "מסחר | 7.0%", "מחסנים | 19.5% לשנה", FOOTNOTE])
PARTS_ASK = "שיעורי ההיוון שנקבעו"
PARTS_MARKDOWN = "שיעור היוון למשרדים 6.5% [S2]. שיעור היוון למסחר 7.0% [S2]."


def _judged(*quotes: str, text: str = CAPTIONED):
    """The answer verified with a judge that scores the one component partial and names these quotes of S2 (a table) as its missing parts."""
    from tests.unit.test_coverage import _judge, _turn

    ws = _file(SOURCE, text, units=(1, 1), declared=(1,))
    ws.sources["S2"].kind = "table"
    ws.sources["S2"].section = "טבלת שיעורים"
    turn = _turn({"id": "1", "text": PARTS_ASK})

    def score(frozen, units):
        return [_req("", "partial", list(units), id=frozen[0][0]) | {"missing": "שיעורי ההיוון",
                                                                     "support": [{"id": "S2", "quote": q} for q in quotes]}]

    r = verify.verify_answer(_judge(score), _answer(PARTS_MARKDOWN), ws, "?", [], requirements=turn, ledger=items.Ledger())
    return ws, r


def _outcomes(report) -> list[str]:
    (vote,) = report.requirements_trace()[0]["votes"]
    return [x["outcome"] for x in vote["support"]]


def test_a_source_row_the_judge_names_as_a_missing_part_is_accepted_and_becomes_an_item():
    ws, r = _judged("מחסנים | 19.5% לשנה")
    assert _outcomes(r) == ["accepted"] and r.text_support["N1"][0]["quote"] == "מחסנים | 19.5% לשנה"
    assert [i.origin for i in r.item_ledger.items.values() if i.quote == "מחסנים | 19.5% לשנה"] == ["judge_support"]


def test_a_reader_written_caption_the_judge_names_as_a_missing_part_is_no_part_and_no_item():
    ws, r = _judged(CAPTION)
    assert _outcomes(r) == ["not_a_source_span"] and r.text_support == {}
    assert not [i for i in r.item_ledger.items.values() if i.origin == "judge_support"]


def test_a_footnote_note_with_words_is_a_source_clause_and_a_bare_mark_is_no_required_value():
    """(follow-up 1: was «a footnote line is no part») the note under a table is the source's own sentence; only its mark is no value."""
    ws, r = _judged(FOOTNOTE)
    assert _outcomes(r) == ["accepted"]
    assert items.source_span_problem(ws.sources["S2"], "(1)") == "not_a_source_span"


def test_a_quote_that_begins_in_the_middle_of_a_number_is_no_value_of_the_source():
    ws, r = _judged("9.5% לשנה")
    assert _outcomes(r) == ["not_a_source_span"] and r.text_support == {}


def test_two_rows_of_the_table_in_one_quote_are_both_source_spans():
    ws, r = _judged("מסחר | 7.0%\nמחסנים | 19.5% לשנה")
    assert _outcomes(r) == ["accepted"]


def test_a_quote_made_of_a_caption_and_the_rows_after_it_is_not_a_source_span():
    ws, r = _judged(CAPTION + "\nשימוש | שיעור\nמחסנים | 19.5% לשנה", text=CAPTIONED.replace("משרדים | 6.5%\nמסחר | 7.0%\n", ""))
    assert _outcomes(r) == ["not_a_source_span"]


def test_a_cell_of_a_source_row_is_a_source_span():
    ws, r = _judged("מחסנים | 19.5% לשנה | הערה", text=CAPTIONED.replace("מחסנים | 19.5% לשנה", "מחסנים | 19.5% לשנה | הערה"))
    assert _outcomes(r) == ["accepted"]


# --- sub-fix 6: the wording says what happened (R2; AE2) ------------------------------------------------------------------------------

def _two_sentence_report(rule=lambda t: {"verdict": "supported"}):
    """The answer's two sentences, each the one unit of its component; ``rule`` judges them (a dict of the verdict's fields)."""
    from tests.unit.test_coverage import _by_words, _structured_judge, _turn
    from tests.unit.test_coverage import _ws as _coverage_ws

    ws = _coverage_ws()
    ws.searches.append("דמי ניהול")
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "דמי הניהול"})
    answer = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול הם 9,500 ₪ [S1].")

    judge = _structured_judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {}}), rule)
    return ws, turn, answer, verify.verify_answer(judge, answer, ws, "?", [], requirements=turn)


def test_a_sentence_removed_by_a_check_that_names_no_failure_kind_is_said_removed_in_verification_and_not_unsupported():
    ws, turn, answer, r = _two_sentence_report()
    unit = next(u for u in r.units if "ניהול" in u.text)
    r.problems.append(verify.Problem(unit, "בסיס המע\"מ של המספר אינו כבמקור", severity="error", kind="claim", check="vat"))
    final, outcomes = _finish(ws, answer, r, turn)
    (gap,) = [g for g in r.gaps if g["components"] == ["N2"]]
    assert gap["reason"] == "removed" and "הוסר באימות" in gap["text"]
    assert "תמיכה" not in gap["text"] and "המקורות" not in gap["text"] and "נמצאה" not in gap["text"]
    assert gap["text"] in final.answer_markdown and outcomes[1]["removal_kinds"] == []


def test_a_sentence_the_judge_removed_keeps_the_reason_of_its_failure_kind():
    ws, turn, answer, r = _two_sentence_report(
        lambda t: {"verdict": "unsupported", "failure": "wrong_unit"} if "ניהול" in t else {"verdict": "supported"})
    final, outcomes = _finish(ws, answer, r, turn)
    (gap,) = [g for g in r.gaps if g["components"] == ["N2"]]
    assert outcomes[1]["removal_kinds"] == ["wrong_unit"] and "הוסר באימות" in gap["text"]
    assert gap["text"].endswith(coverage.REMOVED_BECAUSE["wrong_unit"])


def test_the_default_removal_wording_is_neutral_and_every_kind_keeps_its_own_tail():
    plain = coverage.gap_sentence("removed", "", ["דמי הניהול"])
    assert plain.endswith("הוסר באימות.") and "תמיכה" not in plain
    for kind, tail in coverage.REMOVED_BECAUSE.items():
        sentence = coverage.gap_sentence("removed", kind, ["דמי הניהול"])
        assert sentence.startswith("**דמי הניהול**: מה שנכתב על כך בתשובה הוסר באימות") and (not tail or sentence.endswith(tail))


def _removed_item_report(kind: str):
    """The adjustments answer whose third sentence the judge removes (``kind``: its failure kind, or None for a check that names none)."""
    from tests.unit.test_coverage import ADJ_ASK, _adj_file, _structured_judge, _turn

    ws = _adj_file()
    turn = _turn({"id": "1", "text": ADJ_ASK})
    markdown = "ההתאמה למיקום היא 5% [S2].\nההתאמה לקומה היא 3% [S2].\nההתאמה לגיל הבניין היא 2% [S2]."
    answer = _answer(markdown).model_copy(update={"items": _adj(ADJ_ROWS)})

    def score(frozen, units):
        return [_req("", "full", [i for i, t in units.items() if "גיל" not in t], id=frozen[0][0])]

    rule = (lambda t: {"verdict": "unsupported", "failure": kind} if "גיל" in t else {"verdict": "supported"}) if kind else (
        lambda t: {"verdict": "supported"})
    r = verify.verify_answer(_structured_judge(score, rule), answer, ws, "?", [], requirements=turn, ledger=items.Ledger())
    if not kind:
        unit = next(u for u in r.units if "גיל" in u.text)
        r.problems.append(verify.Problem(unit, "בסיס המע\"מ אינו כבמקור", severity="error", kind="claim", check="vat"))
    return ws, turn, answer, r


@pytest.mark.parametrize("kind", ["wrong_unit", None])
def test_an_item_whose_only_giving_sentence_was_removed_in_verification_is_not_read_and_not_used(kind):
    ws, turn, answer, r = _removed_item_report(kind)
    final, outcomes = _finish(ws, answer, r, turn)
    (line,) = _item_lines(final)
    assert line.startswith("«גיל הבניין»") and "הוסר באימות" in line and "לא נעשה בו שימוש" not in line, line
    (st,) = [s for s in r.item_states() if s["state"] == "omitted"]
    assert st["removed_in_verification"] is True and st["removal_kind"] == (kind or "")
    assert (coverage.REMOVED_BECAUSE[kind] in line) if kind else ("כי" not in line)


def test_an_item_the_answer_never_wrote_is_still_read_and_not_used():
    ws, r, turn, answer = _adj_report("- ההתאמה למיקום היא 5% [S2].\n- ההתאמה לקומה היא 3% [S2].")
    final, _ = _finish(ws, answer, r, turn)
    (line,) = _item_lines(final)
    assert line.startswith("«גיל הבניין»") and "לא נעשה בו שימוש" in line and "הוסר" not in line
    assert not any(s.get("removed_in_verification") for s in r.item_states())


def test_a_judge_part_made_only_of_the_components_own_words_gets_no_absence_sentence():
    from tests.unit.test_coverage import _gap, _partial_case, _partial_turn

    wide = _partial_turn("שטחי שירות עיקריים ומשניים")
    _, a, r, final, outcomes = _partial_case("שטחי שירות", turn=wide)
    (o,) = outcomes
    assert o["status"] == "partial" and o["missing_part"] == "שטחי שירות"  # (the part is validated: it names nothing the request did not ask)
    assert _gap(final, a) == "" and r.gaps == []
    assert not re.search(r"לא נמצא|לא מופיע|לא אותר", final.answer_markdown)
    assert r.completeness["status"] == "partial" and final.status == "partial"  # the component is still not complete: only the sentence about the source is gone


def test_a_judge_part_with_words_of_its_own_still_gets_its_sentence():
    from tests.unit.test_coverage import PLOT1_SERVICE, _gap, _partial_case

    _, a, r, final, outcomes = _partial_case(PLOT1_SERVICE)
    assert _gap(final, a) == f"**{PLOT1_SERVICE}** לא נמצא בחיפוש במסמכים שנבדקו."


# --- property: the outcome does not depend on names, order, an unrelated paragraph or where a sentence ends (R19) ---------------------------------------

VOCABS = [
    FIVE,
    [("נוף לים", "6.5%"), ("מעלית", "2.5%"), ("שיפוץ אחרון", "7%"), ("מחסן", "1.25%"), ("גישה לנכים", "3.5%")],
]
UNRELATED = "הנכס נמצא באזור שקט, ובסביבתו שני גנים ציבוריים ובית ספר [S9]."


def _answer_of(rows, *, lead: str, extra_before: str = "", extra_after: str = "", row_cites=None) -> str:
    return "\n\n".join(x for x in (extra_before, _table(rows, lead=lead, row_cites=row_cites), extra_after) if x)


@pytest.mark.parametrize("vocab", range(len(VOCABS)))
@pytest.mark.parametrize("shape", ["plain", "reversed", "paragraph_before", "paragraph_after", "split_lead_in", "rotated"])
@pytest.mark.parametrize("missing", [None, 0, 3])
def test_property_a_table_cited_on_its_lead_in_covers_the_rows_it_states_whatever_the_names_the_order_or_the_text_around(vocab, shape, missing):
    rows = VOCABS[vocab]
    ws = _ws("\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in rows)]), UNRELATED)
    its = [_row_item(k, v) for k, v in rows]
    stated = [r for n, r in enumerate(rows) if n != missing]
    lead = "ההתאמות שהשומה יישמה [S1]:"
    kw = {}
    if shape == "reversed":
        stated = stated[::-1]
    elif shape == "rotated":
        stated = stated[2:] + stated[:2]
    elif shape == "paragraph_before":
        kw["extra_before"] = UNRELATED
    elif shape == "paragraph_after":
        kw["extra_after"] = UNRELATED
    elif shape == "split_lead_in":
        lead = "להלן ההתאמות. הן נלקחו מהשומה [S1]:"
    got = _states(ws, _answer_of(stated, lead=lead, **kw), *its)
    expect = {k: ("omitted" if n == missing else "covered") for n, (k, _) in enumerate(rows)}
    assert {k: o["state"] for k, o in got.items()} == expect


@pytest.mark.parametrize("vocab", range(len(VOCABS)))
@pytest.mark.parametrize("shape", ["plain", "reversed", "paragraph_before"])
def test_property_a_row_that_cites_another_source_is_never_given_to_the_item_of_this_one(vocab, shape):
    rows = VOCABS[vocab]
    other = "\n".join(["התאמות לנכס אחר", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in rows)])
    ws = _ws("\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in rows)]), other)
    its = [_row_item(k, v) for k, v in rows]
    stated = list(rows)[::-1] if shape == "reversed" else list(rows)
    kw = {"extra_before": UNRELATED} if shape == "paragraph_before" else {}
    got = _states(ws, _answer_of(stated, lead="ההתאמות [S1]:", row_cites={rows[1][0]: "[S2]"}, **kw), *its)
    expect = {k: ("omitted" if n == 1 else "covered") for n, (k, _) in enumerate(rows)}
    assert {k: o["state"] for k, o in got.items()} == expect


@pytest.mark.parametrize("vocab", range(len(VOCABS)))
@pytest.mark.parametrize("shape", ["plain", "reversed", "paragraph_before", "split"])
def test_property_the_datum_audit_gives_an_uncited_bullet_that_names_the_row_whatever_the_order_or_the_text_around(vocab, shape):
    rows = VOCABS[vocab]
    ws = _ws("\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in rows)]), UNRELATED)
    its = [_row_item(k, v) for k, v in rows]
    bullets = [f"- ההתאמה ל{k} היא {v}" + (" [S1]." if n else ".") for n, (k, v) in enumerate(rows)]  # the first bullet is the uncited one
    if shape == "reversed":
        bullets = bullets[::-1]
    elif shape == "paragraph_before":
        bullets = [UNRELATED, "", *bullets]
    elif shape == "split":
        first = rows[0]
        bullets[0] = f"- ההתאמה ל{first[0]} נקבעה בשומה.\n  היא {first[1]}."
    ledger = _ledger_of(*its)
    got = {o["quote"].split(" | ")[0]: o["state"] for o in ledger.outcomes(_units("\n".join(bullets)), ws, audit=True)}
    assert got == {k: "covered" for k, _ in rows}
    plain = {o["quote"].split(" | ")[0]: o["state"] for o in ledger.outcomes(_units("\n".join(bullets)), ws)}
    assert plain[rows[0][0]] == "omitted"  # without the audit the uncited bullet gives nothing (round 17: attribution by citation)


# --- integration: the closing recomposition reproduces a text whose item lines the datum audit decided (final_check.assert_fixed_point) ----------------

def _uncited_bullet_turn():
    from tests.support.scripted_agent import ScriptedAgent, component, final
    from tests.unit.test_final_check import RATES_ASK, SEARCH, Judge, _rate_item
    from tests.unit.test_final_check import _turn as run_turn

    # the third bullet states its rate and cites nothing: by citation form its item is open, by its datum it is given
    answer = "- שיעור ההיוון למשרדים הוא 6.5% [S1].\n- שיעור ההיוון למסחר הוא 7.0% [S1].\n- שיעור ההיוון לחניה הוא 8.25%."
    its = [_rate_item("שיעור למשרדים", "משרדים | 6.5%", "6.5%"), _rate_item("שיעור למסחר", "מסחר | 7.0%", "7.0%"),
           _rate_item("שיעור לחניה", "חניה | 8.25%", "8.25%")]
    judge = Judge(lambda t, i, n: "supported", asks={RATES_ASK: "6.5%"})
    return run_turn(ScriptedAgent([SEARCH, final(answer, items=its), final(answer, items=its), final(answer, items=its)], judge=judge,
                                  request=[component(RATES_ASK)]), "מהם שיעורי ההיוון שנקבעו?")


def test_the_fixed_point_check_holds_on_a_scripted_turn_where_the_datum_audit_covers_an_uncited_bullet(offline):
    from app.chat.compose import text_digest
    from tests.unit.test_final_check import RATES_TEXT, _kinds, _purposes

    offline.texts = [RATES_TEXT]
    out = _uncited_bullet_turn()
    md = out.answer.answer_markdown
    assert "item_gap" not in _kinds(out) and out.report.item_gaps == [] and out.answer.status == "answered", md
    assert {i["label"]: i["state"] for i in out.item_ledger["items"]} == {
        "שיעור למשרדים": "covered", "שיעור למסחר": "covered", "שיעור לחניה": "covered"}
    assert out.final_digest == text_digest(md) and out.final_check["judge_calls"] == 0 and "verify_final" not in _purposes(out)


def test_the_same_turn_without_the_audit_writes_the_line_the_audit_removes(offline, monkeypatch):
    from app.chat.compose import text_digest
    from tests.unit.test_final_check import RATES_TEXT, _kinds

    plain = items.Ledger.outcomes
    monkeypatch.setattr(items.Ledger, "outcomes", lambda self, units, ws, *, audit=False, removed=(), asked=None: plain(self, units, ws, removed=removed, asked=asked))
    offline.texts = [RATES_TEXT]
    out = _uncited_bullet_turn()
    md = out.answer.answer_markdown
    assert _kinds(out) == ["item_gap"] and out.answer.status == "partial" and "«חניה»" in md  # (and that text is a fixed point too)
    assert out.final_digest == text_digest(md)


# --- AE1 end to end: the answer is composed, and the lines the user reads are the ones the datum decides -------------------------------------------

def _table_report(stated, *, row_cites=None, extra_source: bool = False):
    from tests.unit.test_coverage import _file as coverage_file

    text = "\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in FIVE)])
    other = "\n".join(["התאמות לנכס אחר", "סוג התאמה | שיעור", *(f"{k} | {v}" for k, v in FIVE)])
    ws = coverage_file(SOURCE, text, *([other] if extra_source else []), units=(1, 1, 2)[: 3 if extra_source else 2], declared=(1,))
    raw = [_adj_item(f"ההתאמה ל{k}", f"{k} | {v}", v) for k, v in FIVE]
    markdown = _table(stated, lead="ההתאמות שהשומה יישמה [S2]:", row_cites=row_cites)
    r, turn, answer, _ = _ledger_check(ws, markdown, raw)
    final, outcomes = _finish(ws, answer, r, turn)
    return final, outcomes, r


def test_ae1_a_table_cited_once_on_its_lead_in_that_states_the_five_rows_has_no_item_line():
    final, outcomes, r = _table_report(FIVE)
    assert _item_lines(final) == [] and outcomes[0]["status"] == "full" and r.item_gaps == [] and final.status == "answered"


def test_ae1_the_same_table_with_one_row_missing_has_exactly_one_line_for_it():
    final, outcomes, r = _table_report(FIVE[:4])
    (line,) = _item_lines(final)
    assert line.startswith("«חניה»") and "לא נעשה בו שימוש" in line and outcomes[0]["omitted_items"] == ["I5"]


def test_ae1_a_row_that_cites_plan_b_does_not_close_the_item_of_plan_a_with_the_same_value():
    final, outcomes, r = _table_report(FIVE, row_cites={"קומה": "[S3]"}, extra_source=True)
    (line,) = _item_lines(final)
    assert line.startswith("«קומה»") and outcomes[0]["status"] == "partial"


# --- sub-fix 5 (cont.): a mark or an ordinal the extraction flattened onto a row is no value the answer must state ------------------------------------

def test_a_footnote_digit_flattened_onto_its_figure_and_a_row_ordinal_in_the_middle_of_a_line_are_no_headline_values():
    assert items.headline("9.1%1", "מחסנים 9.1%1") == (frozenset({"9.1"}),)
    assert items.headline("6.5%", "1) משרדים 6.5% 2) מסחר 7.0%") == (frozenset({"6.5"}),)
    assert items.headline("7.0%", "1) משרדים 6.5% 2) מסחר 7.0%") == (frozenset({"7"}),)
    # negative controls: a count and an amount keep their figures
    assert items.headline("2 קומות", "בניין של 2 קומות") == (frozenset({"2"}),)
    assert items.headline("19.5%", "מחסנים 19.5%") == (frozenset({"19.5"}),)
    assert items.headline("12", "מחסנים 12 יחידות") == (frozenset({"12"}),)


def test_a_judge_quote_whose_row_the_extraction_marked_is_present_when_the_answer_states_the_row_without_the_mark():
    quote = "מחסנים | 9.1%1"
    assert items.quote_in_blocks(quote, _units("שיעור ההיוון למחסנים הוא 9.1% [S1]."))
    assert not items.quote_in_blocks(quote, _units("שיעור ההיוון למחסנים הוא 9.5% [S1]."))


# --- sub-fix 5 (cont.): what is a source span in a passage ---------------------------------------------------------------------------------------

PASSAGE = "\n".join(["שיעורי ההיוון שנקבעו בשומה:", "שיעור היוון למשרדים הוא 6.5% לפי הדוח.", "הערכת השמאי נעשתה בגישת ההשוואה בלבד.",
                    "- משרדים בקומות עליונות", "- חנויות בקומת קרקע", "הוחלט כי שיעור ההיוון", "למחסנים הוא", "9.1% לפי השמאי"])


@pytest.mark.parametrize("quote, problem", [
    ("שיעור היוון למשרדים הוא 6.5% לפי הדוח.", ""),                      # a sentence with a figure
    ("חנויות בקומת קרקע", ""),                                              # a member of an enumerated list
    ("שיעור ההיוון למחסנים הוא 9.1% לפי השמאי", ""),                          # a sentence the source wrapped over lines
    ("הערכת השמאי נעשתה בגישת ההשוואה בלבד.", ""),                          # a sentence with no figure (follow-up 1: a datum too)
    ("שיעורי ההיוון שנקבעו בשומה:", "not_a_source_span"),                    # the line that opens the passage
    ("היוון למשרדים הוא 6.", "not_a_source_span"),                          # begins and ends in a figure's neighbourhood: a clipped number
])
def test_a_quote_of_a_passage_is_a_source_span_when_it_is_a_sentence_with_a_figure_or_a_list_member(quote, problem):
    from tests.unit.test_coverage import _file

    ws = _file(SOURCE, PASSAGE, units=(1, 1), declared=(1,))
    assert items.source_span_problem(ws.sources["S2"], quote) == problem


# --- follow-up 1: a judge part is a datum of the source when it is any source sentence or clause, with or without a figure -------------------------------

PROSE = "\n".join(["הסכם השכירות", "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש, ולא תהיה לו טענה בגינן.",
                   "הערכת השמאי נעשתה בגישת ההשוואה בלבד.", "לא התקבלה הודעת העברה מהבעלים.", "שיעור היוון למשרדים הוא 6.5% לפי הדוח."])


@pytest.mark.parametrize("quote", [
    "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש, ולא תהיה לו טענה בגינן.",   # a condition sentence with no figure
    "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש",                              # a clause of it
    "הערכת השמאי נעשתה בגישת ההשוואה בלבד.",                                   # a method sentence
    "לא התקבלה הודעת העברה מהבעלים.",                                           # a statement of absence in the source
    "הסכם השכירות\nהשוכר מודע לתכנית א ולתכנית ב החלות על המגרש",                # the title above a sentence does not spoil the sentence
])
def test_follow_up_a_sentence_or_clause_of_the_source_with_no_figure_is_a_source_span(quote):
    from tests.unit.test_coverage import _file

    ws = _file(SOURCE, PROSE, units=(1, 1), declared=(1,))
    assert items.source_span_problem(ws.sources["S2"], quote) == ""


@pytest.mark.parametrize("quote", ["הסכם השכירות", "(1)", "2.", "[3] (4)", "היוון למשרדים הוא 6."])
def test_follow_up_a_title_a_mark_or_a_clipped_number_is_still_no_source_span(quote):
    from tests.unit.test_coverage import _file

    ws = _file(SOURCE, PROSE, units=(1, 1), declared=(1,))
    assert items.source_span_problem(ws.sources["S2"], quote) == "not_a_source_span"


def test_follow_up_a_table_caption_is_no_span_and_a_table_note_with_words_is():
    from tests.unit.test_coverage import _file

    ws = _file(SOURCE, CAPTIONED, units=(1, 1), declared=(1,))
    ws.sources["S2"].kind = "table"
    assert items.source_span_problem(ws.sources["S2"], CAPTION) == "not_a_source_span"
    assert items.source_span_problem(ws.sources["S2"], FOOTNOTE) == ""
    assert items.source_span_problem(ws.sources["S2"], "(1)") == "not_a_source_span"


def test_follow_up_a_judge_part_with_a_figureless_clause_keeps_the_gap_the_answer_leaves():
    from tests.unit.test_coverage import _judge, _turn

    ws = _file(SOURCE, PROSE, units=(1, 1), declared=(1,))
    turn = _turn({"id": "1", "text": PARTS_ASK})
    clause = "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש"

    def score(frozen, units):
        return [_req("", "partial", list(units), id=frozen[0][0]) | {"missing": "תנאי השכירות", "support": [{"id": "S2", "quote": clause}]}]

    r = verify.verify_answer(_judge(score), _answer("שיעור ההיוון למשרדים 6.5% [S2]."), ws, "?", [], requirements=turn, ledger=items.Ledger())
    assert r.text_support == {"N1": [{"id": "S2", "quote": clause}]}


def test_follow_up_a_part_with_content_words_of_its_own_is_not_own_less():
    assert coverage._part_ownless({"missing_part": "שיעורי ההיוון", "text": "שיעורי ההיוון שנקבעו"})
    assert not coverage._part_ownless({"missing_part": "שיעורי ההיוון המוכרים לשוכר", "text": "שיעורי ההיוון שנקבעו"})
    assert not coverage._part_ownless({"missing_part": "השוכר מודע לתכנית אחרת", "text": "תנאי השכירות"})


# --- follow-up 2: a bare list of identifiers is stated by any surviving block that holds all its numbers and ranges ------------------------------------

LOTS_SOURCE = "פרק הפינוי\nמגרשים 9-12, 4, 41-45 מיועדים לפינוי בתוך שנה מהמסירה."
LOTS_TABLE = "טבלת מגרשים\nנושא | מגרשים\nפינוי | 9, 10, 11, 12, 4, 41, 42, 43, 44, 45"
LOTS_ITEM = dict(label="המגרשים לפינוי", quote="מגרשים 9-12, 4, 41-45 מיועדים לפינוי בתוך שנה מהמסירה.", value="9-12, 4, 41-45")


def _lots_ws(units=(1, 1)):
    from tests.unit.test_items import _in_units

    ws = _ws(LOTS_SOURCE, LOTS_TABLE, same_file=True, kind="context")
    ws.sources["S2"].kind = "table"
    _in_units(ws, *units)
    return ws


def _lots(markdown: str, **kw) -> dict:
    """The one item's state in the decision (the datum audit on)."""
    (got,) = _ledger_of(_item(**LOTS_ITEM)).outcomes(_units(markdown), _lots_ws(**kw), audit=True)
    return got


@pytest.mark.parametrize("cell", ["9, 10, 11, 12, 4, 41, 42, 43, 44, 45", "9-12, 4, 41-45", "4, 9-12 ו-41 עד 45"])
def test_follow_up_a_list_of_identifiers_is_stated_by_a_row_that_cites_another_source_of_the_same_place(cell):
    got = _lots(f"| נושא | מגרשים |\n|---|---|\n| פינוי | {cell} [S2] |")
    assert got["state"] == "covered", got["why"]


def test_follow_up_the_list_is_stated_by_an_uncited_row_too():
    assert _lots("| נושא | מגרשים |\n|---|---|\n| פינוי | 9-12, 4, 41-45 |")["state"] == "covered"


def test_follow_up_a_strict_subset_of_the_list_is_given_in_part_and_no_more():
    got = _lots("| נושא | מגרשים |\n|---|---|\n| פינוי | 9-12, 41-45 [S2] |")
    assert got["state"] == "omitted" and got["outcome"] in ("partial", "omitted")
    other = _lots("| נושא | מגרשים |\n|---|---|\n| פינוי | 9-12, 4, 51-55 [S2] |")
    assert other["state"] == "omitted"


def test_follow_up_a_list_stated_by_a_block_that_cites_another_appraisal_unit_is_not_the_items():
    got = _lots("| נושא | מגרשים |\n|---|---|\n| פינוי | 9-12, 4, 41-45 [S2] |", units=(1, 2))
    assert got["state"] == "omitted"


def test_follow_up_the_removed_sentence_is_not_the_reason_when_a_surviving_block_states_the_list():
    ws = _lots_ws()
    ledger = _ledger_of(_item(**LOTS_ITEM))
    gone = _units("מגרשים 9-12, 4, 41-45 מיועדים לפינוי בתוך שנה מהמסירה [S1].")
    stays = _units("| נושא | מגרשים |\n|---|---|\n| פינוי | 9, 10, 11, 12, 4, 41, 42, 43, 44, 45 [S2] |")
    (got,) = ledger.outcomes(stays, ws, audit=True, removed=gone)
    assert got["state"] == "covered" and got["removed_unit"] is None
    (without,) = ledger.outcomes(_units("הנכס מושכר [S1]."), ws, audit=True, removed=gone)
    assert without["state"] == "omitted" and without["removed_unit"] is not None


# --- follow-up 3: a component the answer gives is not reported as not located or not used ------------------------------------------------------------

def _multi_unit_partial(answer: str, missing: str, component: str = "שטחי שירות"):
    from tests.unit.test_coverage import SERVICE_QUESTION, _in_units, _judge, _partial_score, _partial_turn

    ws = _coverage_ws("תא שטח 1: שטחי השירות הם 120 מ״ר. תא שטח 2: שטחי השירות הם 300 מ״ר.")
    ws.searches.append("שטחי שירות")
    _in_units(ws)  # a file of two units, the turn located neither
    turn = _partial_turn(component)
    a = _answer(answer)
    r = verify.verify_answer(_judge(_partial_score(missing)), a, ws, SERVICE_QUESTION, [], requirements=turn)
    return a, *_finish(ws, a, r, turn), r


def test_follow_up_a_partial_component_whose_part_the_units_state_is_not_reported_as_not_located():
    a, final, outcomes, r = _multi_unit_partial("שטחי השירות בתא שטח 1 הם 120 מ״ר [S1].", "שטחי השירות בתא שטח 1")
    gap = final.answer_markdown[len(a.answer_markdown.strip()):]
    assert "לא אותרה" not in gap and "לא נמצא" not in gap and "לא מופיע" not in gap, gap
    assert r.gaps == []


def test_follow_up_a_partial_component_whose_part_is_not_stated_still_says_the_unit_was_not_located():
    a, final, outcomes, r = _multi_unit_partial("שטחי השירות בתא שטח 2 הם 300 מ״ר [S1].", "שטחי השירות בתא שטח 1")
    gap = final.answer_markdown[len(a.answer_markdown.strip()):]
    assert "לא אותרה" in gap or "לא נמצא" in gap or "לא אותר" in gap, gap


def test_follow_up_a_part_the_judge_named_in_an_earlier_version_is_closed_by_a_block_of_its_place_that_states_it():
    from tests.unit.test_coverage import ADJ_TEXT, TWO_ADJ

    ws = _file(SOURCE, ADJ_TEXT, "התאמות לפי דוח משלים\nההתאמה לגיל הבניין: 2.0%", units=(1, 1, 1), declared=(1,))
    ledger = items.Ledger()
    raw = _adj(ADJ_ROWS[:2])
    quote = "גיל הבניין | 2%"
    r1, _, _, _ = _ledger_check(ws, TWO_ADJ, raw, status="partial", support=[{"id": "S2", "quote": quote}], ledger=ledger)
    assert [s["state"] for s in r1.item_states() if s["origin"] == "judge_support"] == ["omitted"]  # the first draft omits it: a true gap
    # the next draft states it in a block that cites a sibling source of the same place, in the sibling's own figures
    r2, turn, answer, _ = _ledger_check(ws, TWO_ADJ + "\n- ההתאמה לגיל הבניין היא 2.0% [S3].", raw, status="full", ledger=ledger)
    final, outcomes = _finish(ws, answer, r2, turn)
    assert [s["state"] for s in r2.item_states() if s["origin"] == "judge_support"] == ["covered"]
    assert "לא נעשה בו שימוש" not in final.answer_markdown and outcomes[0]["status"] == "full"
    # a block that cites another appraisal unit's source is not the part's
    from app.chat import tools as T

    ws.sources["S3"].contexts, ws.sources["S3"].unit = [2], T.unit_info(ws.contexts[str(ws.sources["S1"].version_id)], 2)
    r3, _, _, _ = _ledger_check(ws, TWO_ADJ + "\n- ההתאמה לגיל הבניין היא 2.0% [S3].", raw, status="full", ledger=ledger)
    assert [s["state"] for s in r3.item_states() if s["origin"] == "judge_support"] == ["omitted"]


def test_follow_up_a_judge_part_that_is_a_bare_list_of_identifiers_is_stated_by_a_block_holding_all_its_numbers():
    ws = _lots_ws()
    part = _item(origin="judge_support", label="", value="", quote="חלקות 9-12, 4, 41-45", source="S1")
    ledger = _ledger_of(part)
    for cell in ("9, 10, 11, 12, 4, 41, 42, 43, 44, 45", "4, 9-12, 41-45"):
        (got,) = ledger.outcomes(_units(f"| נושא | מגרשים |\n|---|---|\n| פינוי | {cell} [S2] |"), ws, audit=True)
        assert got["state"] == "covered", cell
    (sub,) = ledger.outcomes(_units("| נושא | מגרשים |\n|---|---|\n| פינוי | 9, 10, 11, 12, 4 [S2] |"), ws, audit=True)
    assert sub["state"] == "omitted"


# --- follow-up 2A: a part with no figure of its own is stated by its distinctive words, not by the generic words a block shares ---------------------------

BASIS = "הערך הוא ממוצע שלוש השנים האחרונות (2018-2021 למעט 2020, שנת המגיפה)."
METHOD_S = "השווי נגזר משווי למ״ר של ייעוד חלופי ולאחר מכן מקדמי התאמה."
WORDS_SRC = "\n".join(["אופן קביעת הערך", BASIS, METHOD_S])
WORDS_ASK = "אופן קביעת הערך"
WORDS_CASES = {
    "basis": dict(
        quote=BASIS,
        generic="בשנים האחרונות נבנו כאן שנים רבות ללא היתר, והשנה האחרונה עברה למעט שינוי [S1].",
        restated="הערך נקבע לפי ממוצע של שלוש השנים האחרונות, למעט שנת המגיפה [S1].",
        reordered="למעט שנת המגיפה, נקבע הערך כממוצע שלוש השנים האחרונות [S1].",
        negated="הערך לא נקבע כממוצע של שלוש השנים האחרונות, למעט שנת המגיפה [S1]."),
    "method": dict(
        quote=METHOD_S,
        generic="השווי נקבע לפי מכירות באזור, ולאחר מכן נעשתה התאמה קלה [S1].",
        restated="השווי נגזר משווי למ״ר של ייעוד חלופי, ובהמשך הוחלו מקדמי התאמה [S1].",
        reordered="מקדמי התאמה הוחלו לאחר שהשווי נגזר משווי למ״ר של ייעוד חלופי [S1].",
        negated="השווי לא נגזר משווי למ״ר של ייעוד חלופי ולאחר מכן מקדמי התאמה [S1]."),
}
NOISE = "הנכס נמצא בסביבה שקטה וקרוב לתחבורה ציבורית ולמוסדות חינוך [S1]."


def _word_blocks(*paragraphs: str):
    return items.blocks_of(items.model_units(_units("\n\n".join(paragraphs))))


def _giving(case: str, *paragraphs: str):
    ws = _ws(WORDS_SRC, kind="context")
    return items.giving_block_in_place(WORDS_CASES[case]["quote"], "S1", _word_blocks(*paragraphs), ws, WORDS_ASK)


@pytest.mark.parametrize("case", WORDS_CASES)
def test_follow_up_a_block_that_shares_only_generic_words_with_a_wordy_part_does_not_give_it(case):
    assert items._wordy(WORDS_CASES[case]["quote"], WORDS_CASES[case]["quote"])
    assert _giving(case, WORDS_CASES[case]["generic"]) is None
    assert _giving(case, NOISE, WORDS_CASES[case]["generic"], NOISE) is None


@pytest.mark.parametrize("case", WORDS_CASES)
@pytest.mark.parametrize("form", ["restated", "reordered"])
def test_follow_up_the_part_restated_with_its_distinctive_terms_is_given_wherever_the_block_stands(case, form):
    mine = WORDS_CASES[case][form]
    assert _giving(case, mine) == 0
    assert _giving(case, NOISE, mine) == 1  # an unrelated sentence before it
    assert _giving(case, mine, NOISE) == 0


@pytest.mark.parametrize("case", WORDS_CASES)
def test_follow_up_a_block_that_negates_the_part_does_not_give_it_and_noise_does_not_change_that(case):
    assert _giving(case, WORDS_CASES[case]["negated"]) is None
    assert _giving(case, NOISE, WORDS_CASES[case]["negated"]) is None


def test_follow_up_a_part_with_a_figure_of_its_own_keeps_the_existing_rule():
    assert not items._wordy("5%", "מיקום | 5%") and not items._wordy("12 יחידות", "בבניין 12 יחידות דיור")
    assert items._wordy("", BASIS) and items._wordy("", METHOD_S)
    assert not items._wordy("", "מועד הסיום 3.8.2031")  # a date is a figure; a range of years is not


def test_follow_up_the_audit_asks_an_uncited_block_the_same_distinctive_words_for_an_item_with_no_figure():
    ws = _ws(WORDS_SRC, kind="context")
    item = _item(label="אופן קביעת השווי", quote=METHOD_S, value="", source="S1")
    ledger = _ledger_of(item)
    states = {}
    for name in ("generic", "restated", "negated"):
        (got,) = ledger.outcomes(_units("- " + WORDS_CASES["method"][name].replace(" [S1]", "")), ws, audit=True, asked={"N1": WORDS_ASK})
        states[name] = got["state"]
    assert states == {"generic": "omitted", "restated": "covered", "negated": "omitted"}


def test_follow_up_end_to_end_a_wordy_part_the_judge_named_stays_a_gap_when_a_later_block_shares_only_generic_words():
    from tests.unit.test_coverage import ADJ_TEXT, TWO_ADJ

    ws = _file(SOURCE, ADJ_TEXT + "\n" + BASIS, units=(1, 1), declared=(1,))
    ledger = items.Ledger()
    raw = _adj(ADJ_ROWS[:2])
    r1, *_ = _ledger_check(ws, TWO_ADJ, raw, status="partial", support=[{"id": "S2", "quote": BASIS}], ledger=ledger)
    assert [s["state"] for s in r1.item_states() if s["origin"] == "judge_support"] == ["omitted"]
    generic = TWO_ADJ + "\n\n" + WORDS_CASES["basis"]["generic"].replace("[S1]", "[S2]")
    r2, turn, answer, _ = _ledger_check(ws, generic, raw, status="full", ledger=ledger)
    final, _ = _finish(ws, answer, r2, turn)
    assert [s["state"] for s in r2.item_states() if s["origin"] == "judge_support"] == ["omitted"]
    assert "לא נעשה בו שימוש" in final.answer_markdown
    restated = TWO_ADJ + "\n\n" + WORDS_CASES["basis"]["restated"].replace("[S1]", "[S2]")
    r3, turn, answer, _ = _ledger_check(ws, restated, raw, status="full", ledger=ledger)
    final, _ = _finish(ws, answer, r3, turn)
    assert [s["state"] for s in r3.item_states() if s["origin"] == "judge_support"] == ["covered"]
    assert "לא נעשה בו שימוש" not in final.answer_markdown


# --- follow-up 2B: a bare list of identifiers stated in a heading, a bold lead line or the bold label of a list item ---------------------------------

def _lots_of(value: str, markdown: str, quote: str = "חלקות 9 - 12 במלואן מיועדות לפינוי בתוך שנה.") -> dict:
    ws = _lots_ws()
    (got,) = _ledger_of(_item(label="החלקות לפינוי", quote=quote, value=value)).outcomes(_units(markdown), ws, audit=True)
    return got


HEADED = [
    "### חוזה לחלקות {lots}\n\n| נושא | פירוט |\n|---|---|\n| פינוי | בתוך שנה [S2] |",
    "**חוזה לחלקות {lots}:**\n\n| נושא | פירוט |\n|---|---|\n| פינוי | בתוך שנה [S2] |",
    "**חוזה לחלקות {lots}**\n- פינוי בתוך שנה [S2]",
    "- **חלקות {lots}**: פינוי בתוך שנה [S2]",
    "טבלה 2 - חלקות {lots}\n\n| נושא | פירוט |\n|---|---|\n| פינוי | בתוך שנה [S2] |",
]


@pytest.mark.parametrize("shape", range(len(HEADED)))
@pytest.mark.parametrize("lots", ["9 - 12", "9-12", "9 עד 12", "9, 10, 11, 12", "9 ‒ 12 במלואן"])
def test_follow_up_a_range_of_lots_in_a_heading_or_a_bold_label_is_stated_whatever_way_the_range_is_written(shape, lots):
    got = _lots_of("9 - 12 במלואן", HEADED[shape].format(lots=lots))
    assert got["state"] == "covered", got["why"]


@pytest.mark.parametrize("shape", range(len(HEADED)))
def test_follow_up_a_different_list_of_lots_in_the_heading_is_not_the_items(shape):
    for lots in ("13 - 16", "9, 10, 11", "10 - 12"):
        assert _lots_of("9 - 12 במלואן", HEADED[shape].format(lots=lots))["state"] == "omitted", lots


@pytest.mark.parametrize("lots", ["41, 42, 43, 44, 45", "41-45", "41 עד 45", "41 - 45"])
def test_follow_up_a_full_list_of_lots_is_stated_by_a_heading_in_any_notation(lots):
    got = _lots_of("41, 42, 43, 44, 45", HEADED[0].format(lots=lots), quote="חלקות 41, 42, 43, 44, 45 מיועדות להפקעה.")
    assert got["state"] == "covered"
    other = _lots_of("41, 42, 43, 44, 45", HEADED[0].format(lots="41, 42, 43, 44, 46"), quote="חלקות 41, 42, 43, 44, 45 מיועדות להפקעה.")
    assert other["state"] == "omitted"


@pytest.mark.parametrize("value, markdown", [
    ("2018 - 2021", "### נתוני השנים 2018 - 2021 [S2]"),                  # a range of years is a period, not a list of identifiers
    ("3 - 5 קומות", "### בניין בן 3, 4, 5 [S2]"),                           # a range of a count is no list of identifiers
])
def test_follow_up_a_period_or_a_count_is_not_a_list_of_identifiers(value, markdown):
    assert items._identifier_list(value, value) is None
    assert _lots_of(value, markdown, quote=value)["state"] == "omitted"


# --- follow-up 3: a quote that is no datum of the source makes no item - and the part is still repaired, with tools (round 21 U9) ----------------------

@pytest.mark.parametrize("quote", [CAPTION, "שימוש | שיעור", "שיעורי ההיוון שנקבעו בשומה"])
def test_follow_up_a_caption_a_header_row_or_a_title_the_judge_quotes_makes_no_item_but_the_repair_round_keeps_its_tools(quote):
    text = CAPTIONED if quote != "שיעורי ההיוון שנקבעו בשומה" else "שיעורי ההיוון שנקבעו בשומה\n" + CAPTIONED
    ws, r = _judged(quote, text=text)
    assert _outcomes(r) == ["not_a_source_span"] and r.text_support == {} and r.quoted_support == {"N1": [{"id": "S2", "quote": quote}]}
    assert not [i for i in r.item_ledger.items.values() if i.origin == "judge_support"]  # no item, so no item line
    (problem,) = [p for p in r.problems if p.kind == "requirement"]
    assert problem.check is None and problem.repairable and quote in problem.reason and "אין צורך בחיפוש חדש" not in problem.reason
    assert r.text_completion_only() is False  # the round may read and search
    turn = _turn_of_parts()
    final, outcomes = _finish(ws, _answer(PARTS_MARKDOWN), r, turn)
    assert _item_lines(final) == [] and "לא נעשה בו שימוש" not in final.answer_markdown and outcomes[0]["status"] == "partial"


def test_follow_up_a_quote_that_is_a_source_row_is_still_a_completion_from_read_text():
    ws, r = _judged("מחסנים | 19.5% לשנה")
    (problem,) = [p for p in r.problems if p.kind == "requirement"]
    assert problem.check == verify.TEXT_FOUND_CHECK and r.quoted_support == {} and r.text_completion_only() is True


def _turn_of_parts():
    from tests.unit.test_coverage import _turn

    return _turn({"id": "1", "text": PARTS_ASK})


# --- follow-up 3 (cont.): the last wrapped line of a lead-in that ends in a colon is no title ------------------------------------------------------------

WRAPPED_LEAD = ["הסכם חכירה", "מרכיב השווי ייגזר משווי למ״ר קרקע מבונה בייעוד חלופי", "(שאינו בהכרח הייעוד הנפוץ) שעליו יופעלו מקדמי ההתאמה",
                "הבאים:", "- מקדם ראשון לפי מיקום", "- מקדם שני לפי גודל"]


def _span_of(lines: list[str], quote: str) -> str:
    from tests.unit.test_coverage import _file

    ws = _file(SOURCE, "\n".join(lines), units=(1, 1), declared=(1,))
    return items.source_span_problem(ws.sources["S2"], quote)


def test_follow_up_a_lead_in_sentence_wrapped_over_lines_that_ends_in_a_colon_is_a_source_span():
    quote = " ".join(WRAPPED_LEAD[1:4])
    assert _span_of(WRAPPED_LEAD, quote) == ""
    assert _span_of(WRAPPED_LEAD, "\n".join(WRAPPED_LEAD[1:4])) == ""
    assert _span_of(WRAPPED_LEAD, "הבאים:\n- מקדם ראשון לפי מיקום") == ""  # (the lead-in's last line and its first member)


def test_follow_up_the_same_one_word_line_after_a_closed_sentence_and_a_real_short_heading_are_still_titles():
    closed = ["הנחיות כלליות לשומה בנכס.", "הבאים:", "- מקדם ראשון לפי מיקום", "- מקדם שני לפי גודל"]
    assert _span_of(closed, "הבאים:") == "not_a_source_span"
    short = ["הנחיות כלליות לשומה בנכס.", "הסכם חכירה", "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש."]
    assert _span_of(short, "הסכם חכירה") == "not_a_source_span"
    assert _span_of(short, "השוכר מודע לתכנית א ולתכנית ב החלות על המגרש.") == ""
