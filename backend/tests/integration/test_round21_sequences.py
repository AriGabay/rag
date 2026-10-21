"""Reproductions of the round-21 failure shapes, recorded failing on the round-20 build (round 21 U1: R1-R11; AE1-AE8; KTD1-KTD9).

Each sequence is a turn over a synthetic world (office B, or the offline engine with no database): the model and the judge are scripted, so a
test proves what the server does with an answer that is CORRECT for its fixture, not the judgement of a model. The assertions are written
as the CORRECT behaviour of the acceptance example (the plan's AE text), on what the user sees: the final message and the server's lines in
it, or on what the agent and the verifier are shown. Each open case is marked ``xfail(strict=True, raises=AssertionError)`` with the unit
that owns the fix and what fails on the round-20 server; the owning unit removes the marker when its fix lands (a strict xfail that starts
passing fails the suite, so the marker cannot be forgotten). A protection sequence passes on the round-20 build and must keep passing.

1. AE1 (U2, fixed): two data with a source condition each, in one sentence — each condition lands in its own datum's clause.
2. AE2 (U2, fixed): a share fraction (n/m) in a list item — the condition binds to the line that holds the item's own fraction; with no
   such line nothing is written inline and one uncertainty line names the item.
3. Equal values in two components (U2, fixed): two equal numbers anchor nothing — no inline condition, one uncertainty line names both.
4. A footnote-marked note (U2, fixed): the note of the first contract is written in the clause of the first group only.
5. AE3 (U3, fixed): a monthly column header put "monthly" on a capitalised total.
6. AE4 (U4, fixed): a removal for another lot of the same property said "another property".
7. Several lots (U4, fixed): the same bullet under two lot headings lost its second copy to the tidy-up.
8. AE5 (U9): a judge "partial" on a supported number sends the repair round, which drops the number.
9. AE6 (U8, fixed): a table stored in reversed cell order is served unpaired and reversed to the agent and to the verifier.
10. AE7 (U7): a numbered item stated with its label inflected gets a false "read and not used" line.
11. AE8 (not reproduced; protection): a follow-up carries no condition of the previous answer.
12. The capture of U1: both drafts of a two-round turn and the locator of every cited source are in the diagnostics
    (``test_chat_engine.py``, ``test_chat_permissions.py``); a captured locator rebuilds the source text here.

The shapes with a document use the synthetic fixtures of ``tests/fixtures/round20`` (``scripts/generate_fixtures.py --only round20``);
the others are Workspace(ctx=None) worlds (``add_source``, ``verify_answer``, ``report.edit``) or the offline engine. Every name and figure
is invented."""

# ruff: noqa: F811 - the tests take the imported ``offline`` fixture by name, as the round-13 and round-14 sequences do

from __future__ import annotations

import json
import re

import pytest

from app.chat import engine
from app.chat import items as ledger_items
from app.chat.verify import verify_answer
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import _outputs
from tests.integration.test_round18_sequences import (
    DOCUMENT_OF,
    ingest_pdf,
    lenient_judge,
    read_pages,
    read_table,
)
from tests.integration.test_round20_sequences import (
    FOOTNOTE,
    GROUP_A_ROW,
    GROUP_B_ROW,
    LOTS,
    LOTS_ASK,
    LOTS_QUESTION,
    MANIFEST,
    lot_item,
)
from tests.integration.test_round20_sequences import ask as ask_over_document
from tests.support.replay import rebuild_source_text
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read
from tests.unit.test_coverage import (
    _adj_item,
    _answer,
    _condition_judge,
    _file,
    _finish,
    _item_lines,
    _ledger_check,
    _plan_check,
    _plan_file,
)
from tests.unit.test_coverage import _turn as requirements_of
from tests.unit.test_final_check import SEARCH, Judge, offline  # noqa: F401 - the fixture, taken by name
from tests.unit.test_final_check import _turn as engine_turn


def open_case(unit: str, what: str):
    """The marker of an open case: it fails with an assertion on the round-20 build, for the reason ``what``, and ``unit`` owns the fix."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"round 21 {unit}: {what}")


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def condition_notes(markdown: str) -> list[str]:
    """The conditions the server wrote next to a value (its «התנאי לפי המקור: …» notes)."""
    return re.findall(r"התנאי לפי המקור(?: של [^:]+)?: ([^)]+)\)", markdown)


# --- 1. two data with a source condition each, in one sentence (AE1, U2) -----------------------------------------------------------

CLAUSE_A = "הזכויות חלות רק על פי תכנית מאושרת"
CLAUSE_B = "השטח חל רק לאחר פינוי המגרש"
TWO_CONDITIONS_ROWS = ("סוג שטח | שטח (מ״ר) | הערה", f"שטח עיקרי | 1,200 | {CLAUSE_A}", f"שטח שירות | 300 | {CLAUSE_B}")
TWO_CONDITIONS = "השטח העיקרי בנכס הוא 1,200 מ״ר והשטח השירות בנכס הוא 300 מ״ר [S2]."


def test_ae1_each_condition_of_two_data_in_one_sentence_lands_in_its_own_datums_clause():
    raw = [_adj_item("שטח עיקרי", "שטח עיקרי | 1,200", "1,200"), _adj_item("שטח שירות", "שטח שירות | 300", "300")]
    r, _, answer, _ = _plan_check(TWO_CONDITIONS, rows=TWO_CONDITIONS_ROWS, raw=raw, ask="השטח העיקרי והשירות בנכס")
    doc = r.edit(answer).doc
    text = doc.text
    first, second = text.split("והשטח השירות")
    assert CLAUSE_A in first and CLAUSE_A not in second, text
    assert CLAUSE_B in second and CLAUSE_B not in first, text
    assert "1,200" in first and "300" in second, text
    # each note is registered with the datum it belongs to (KTD1): the item ids of the two data, in the text's order
    spans = [sp for sp in doc.spans() if sp.kind == "qualifier"]
    assert all(sp.claim_ids for sp in spans), spans  # every addition names the claims it is about, the area basis note included
    notes = [sp for sp in spans if sp.datum]
    assert [sp.datum for sp in notes] == ["I1", "I2"], spans
    assert all(len(sp.claim_ids) == 1 and sp.as_dict()["datum"] == sp.datum for sp in notes), notes


# --- 2. a share fraction in a list item (AE2, U2) ------------------------------------------------------------------------------------

SHARE_CLAUSE = "החלק חל רק לאחר פירוק השיתוף"
SHARE_ROWS = ("בעל זכות | חלק בנכס | הערה", "בעל הקרקע | 1/3 | ", f"הדייר | 2/3 | {SHARE_CLAUSE}")
SHARES = "- החלק של בעל הקרקע הוא 1/3 [S2]. החלק של הדייר הוא 2/3 [S2]."


def test_ae2_the_condition_of_a_share_item_binds_only_to_the_line_that_holds_its_own_fraction():
    raw = [_adj_item("חלק בעל הקרקע", "בעל הקרקע | 1/3", "1/3"), _adj_item("חלק הדייר", "הדייר | 2/3", "2/3")]
    r, _, answer, _ = _plan_check(SHARES, rows=SHARE_ROWS, raw=raw, ask="החלקים בנכס")
    text = r.edit(answer).doc.text
    owner_line, tenant_line = text.split("החלק של הדייר")
    assert SHARE_CLAUSE not in owner_line, text  # the line of 1/3 is not the item's line
    assert SHARE_CLAUSE in tenant_line and tenant_line.index("2/3") < tenant_line.index(SHARE_CLAUSE), text


SHARES_WITHOUT_TENANT_FRACTION = "- החלק של בעל הקרקע הוא 1/3 [S2]. החלק של הדייר נקבע בהסכם [S2]."


def test_ae2_a_share_item_whose_fraction_no_line_states_gets_no_inline_note_and_one_uncertainty_line_that_names_it():
    """The other branch of AE2 (KTD2): the item is covered by its words, no line holds its tokens 2/3, so nothing is written beside any line,
    the item stays, and one line in the gap area names it and its condition; it counts as a missing caveat, not as coverage."""
    ws = _plan_file(*SHARE_ROWS)
    raw = [_adj_item("חלק בעל הקרקע", "בעל הקרקע | 1/3", "1/3"), _adj_item("חלק הדייר", "הדייר | 2/3", "2/3")]
    turn = requirements_of({"id": "1", "text": "החלקים בנכס"})
    answer = _answer(SHARES_WITHOUT_TENANT_FRACTION).model_copy(update={"items": raw})
    report = verify_answer(_condition_judge(), answer, ws, "?", [], requirements=turn, ledger=ledger_items.Ledger())
    final_answer, _ = _finish(ws, answer, report, turn)
    lines = final_answer.answer_markdown.splitlines()
    assert SHARE_CLAUSE not in lines[0] and "התנאי לפי המקור" not in lines[0], lines[0]  # no note beside any line
    (uncertain,) = [ln for ln in lines[1:] if SHARE_CLAUSE in ln]  # one line, in the gap area after the answer
    assert "חלק הדייר" in uncertain and "בעל הקרקע" not in uncertain, uncertain  # it names the item and not the one that is fine
    assert final_answer.status == "partial", final_answer.status
    assert [c["label"] for c in report.completeness["missing_caveats"]] == ["חלק הדייר"] and report.completeness["status"] == "partial"
    assert [r.kind for r in report.problems if r.check == "condition"] == []  # nothing was written for it
    assert not any(p.removes_unit for p in report.problems)  # unknown never removes: the datum stays


# --- 3. equal values in two components (U2) ---------------------------------------------------------------------------------------------

EQUAL_ROWS = ("סוג שטח | שטח (מ״ר) | הערה", f"שטח שירות | 300 | {CLAUSE_A}", f"שטח מרפסת | 300 | {CLAUSE_B}")
EQUAL = "שטח השירות בנכס הוא 300 מ״ר ושטח המרפסת בנכס הוא 300 מ״ר [S2]."


def test_two_equal_values_in_two_components_get_no_inline_condition_and_one_line_that_names_both():
    ws = _plan_file(*EQUAL_ROWS)
    raw = [_adj_item("שטח שירות", "שטח שירות | 300", "300", component="N1"),
           _adj_item("שטח מרפסת", "שטח מרפסת | 300", "300", component="N2")]
    turn = requirements_of({"id": "1", "text": "שטח השירות בנכס"}, {"id": "2", "text": "שטח המרפסת בנכס"})
    answer = _answer(EQUAL).model_copy(update={"items": raw})
    report = verify_answer(_condition_judge(), answer, ws, "?", [], requirements=turn, ledger=ledger_items.Ledger())
    final_answer, _ = _finish(ws, answer, report, turn)
    lines = final_answer.answer_markdown.splitlines()
    assert CLAUSE_A not in lines[0] and CLAUSE_B not in lines[0], lines[0]  # no equal number anchors a condition
    assert any("שירות" in ln and "מרפסת" in ln for ln in lines[1:]), final_answer.answer_markdown  # one focused line names both data
    (uncertain,) = [ln for ln in lines[1:] if "שירות" in ln]  # one line, in the gap area, naming each datum with the condition it could not place
    assert CLAUSE_A in uncertain and CLAUSE_B in uncertain and "300" in uncertain, uncertain
    assert not report.removals() and "300" in lines[0], lines[0]  # unknown never removes: both data stay
    # every unbound datum is a missing caveat in completeness, never coverage
    assert [c["label"] for c in report.completeness["missing_caveats"]] == ["שטח שירות", "שטח מרפסת"], report.completeness
    assert report.completeness["status"] == "partial" and final_answer.status == "partial"
    # the spans the composer registered: the uncertainty line sits in the gap area, after the answer, and no server span is inline
    assert [s.kind for s in spans_of(ws, answer, report, turn)] == ["uncertainty_line"]


def spans_of(ws, answer, report, turn):
    """The registry of the server spans of the finished answer (``compose_components``' own ``Doc``)."""
    from app.chat import coverage

    edited, _ = coverage.compose_components(ws, answer, report, turn)
    return edited.doc.spans()


# --- 4. a footnote-marked note (U2) -----------------------------------------------------------------------------------------------------

TWO_GROUPS = (f"שטח קבוצת מגרשים א׳ הוא {GROUP_A_ROW[2]} מ״ר לפי {GROUP_A_ROW[1]}, "
              f"ושטח קבוצת מגרשים ב׳ הוא {GROUP_B_ROW[2]} מ״ר לפי {GROUP_B_ROW[1]} [S1].")


@pytest.mark.db
def test_a_footnote_marked_on_the_first_groups_contract_is_written_in_the_first_groups_clause_only(client, office, monkeypatch):
    m = ask_over_document(client, office, monkeypatch, LOTS, read_table(LOTS["caption"]), TWO_GROUPS,
                          LOTS_QUESTION.replace("ב׳", "א׳ וב׳"), LOTS_ASK, items=[lot_item(GROUP_A_ROW, FOOTNOTE)])
    md = m["answer"]["markdown"]
    first, second = md.split("ושטח קבוצת מגרשים ב׳")
    assert "מינהל" in first, md  # the first group's contract keeps its condition
    assert "מינהל" not in second, md  # and the second group's clause does not carry it


# --- 5. a monthly header on a capitalised total (AE3, U3) -------------------------------------------------------------------------------

INCOME_TABLE = "\n".join(["תמהיל הכנסות", "רכיב | סכום חודשי (₪)", "שכר דירה בסיסי | 55,000", "שווי מהוון | 6,600,000"])
CAPITALISED = "השווי המהוון של הנכס הוא 6,600,000 ₪ [S2]."


def test_ae3_a_capitalised_total_under_a_monthly_column_header_gets_no_monthly_period():
    ws = _file("סיכום: השווי למ\"ר הוא 9,500 ₪.", INCOME_TABLE, units=(1, 1), declared=(1,))
    ws.sources["S2"].kind = "table"
    answer = _answer(CAPITALISED)
    report = verify_answer(_condition_judge(), answer, ws, "?", [], requirements=requirements_of({"id": "1", "text": "השווי המהוון של הנכס"}),
                           ledger=ledger_items.Ledger())
    text = report.edit(answer).doc.text
    assert "6,600,000" in text and "לחודש" not in text, text  # the total is not a monthly amount


# --- 6. a removal for another lot of the same property (AE4, U4) ------------------------------------------------------------------------

LOT_SOURCE = "מגרש 101 — שווי הקרקע 3,100,000 ₪.\nמגרש 102 — שווי הקרקע 4,200,000 ₪."
LOT_QUESTION = "מה שווי הקרקע במגרשים 101 ו-102?"
MISPLACED_LOT = "שווי הקרקע במגרש 101 הוא 4,200,000 ₪ [S1].\nשווי הקרקע במגרש 102 הוא 4,200,000 ₪ [S1]."


def test_ae4_the_note_of_a_claim_removed_for_another_lot_names_the_lot_scope_and_not_another_property(offline):
    offline.texts = [LOT_SOURCE]
    agent = ScriptedAgent([SEARCH, *[final(MISPLACED_LOT) for _ in range(3)]], judge=Judge(lambda t, i, n: "supported"))
    out = engine_turn(agent, LOT_QUESTION)
    md = out.answer.answer_markdown
    assert [(p.check, p.failure_kind) for p in out.report.removals()] == [("scope_mismatch", "wrong_subject")], md
    assert "4,200,000" in md and "במגרש 101" not in md, md  # the misplaced claim goes, the lot's own claim stays
    (note,) = [ln for ln in md.splitlines() if ln.startswith(">")]
    assert "מגרש" in note and "נכס" not in note, note


# --- 7. several lots (U4) ------------------------------------------------------------------------------------------------------------------

SAME_BULLET_SOURCE = "מגרש 101 — שטח 800 מ״ר.\nמגרש 102 — שטח 800 מ״ר."
SAME_BULLET = "**מגרש 101**\n- השטח הוא 800 מ״ר [S1].\n\n**מגרש 102**\n- השטח הוא 800 מ״ר [S1]."


def test_the_same_bullet_under_two_lot_headings_survives_the_tidy_up(offline):
    offline.texts = [SAME_BULLET_SOURCE]
    agent = ScriptedAgent([SEARCH, *[final(SAME_BULLET) for _ in range(3)]], judge=Judge(lambda t, i, n: "supported"))
    out = engine_turn(agent, "מה השטח של מגרשים 101 ו-102?")
    md = out.answer.answer_markdown
    assert md.count("השטח הוא 800 מ״ר") == 2, md
    assert md.index("מגרש 102") < md.rindex("השטח הוא 800 מ״ר"), md  # the second copy stands under lot 102


# --- 8. a judge "partial" on a supported number (AE5, U9) -----------------------------------------------------------------------------------

VERIFIED_NUMBER = "השווי למ\"ר הוא 9,500 ₪ [S1]."
OTHER_FACT = "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."


@open_case("U9", "the partial verdict sends a repair round whose rewrite omits the supported number, and nothing restores it")
def test_ae5_a_supported_number_the_judge_calls_partial_stays_in_the_answer_when_the_repair_round_omits_it(offline):
    offline.texts = ["השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש."]

    def rule(text, input, n):
        # the number is in the source; the judge doubts only its unit
        return {"verdict": "partial", "defect": "units"} if "9,500" in text and n == 1 else "supported"

    # the repair round's rewrite follows the repair prompt and omits what was not verified
    agent = ScriptedAgent([SEARCH, final(f"{VERIFIED_NUMBER}\n{OTHER_FACT}"), final(OTHER_FACT), final(OTHER_FACT)], judge=Judge(rule))
    out = engine_turn(agent, "מה השווי למ\"ר ודמי השכירות?")
    md = out.answer.answer_markdown
    line = next((ln for ln in md.splitlines() if "9,500" in ln), "")
    assert line, md  # the verified number stays
    assert "אומת" in line, md  # with what was not verified stated beside it
    assert "55" in md, md


# --- 9. a table stored in reversed cell order (AE6, U8) ---------------------------------------------------------------------------------------

QUARTERS = MANIFEST["hebrew_visual"]
REVENUE_QUESTION = "מה ההכנסות ברבעון הראשון של 2024?"
REVENUE_ANSWER = "ההכנסות ברבעון הראשון של 2024 הן 100 [S1]."


def reverse_stored_table(office, table_index: int) -> None:
    """The table as the round-19 extraction stored a left-to-right table (the generation the round-20 extraction left in the database
    without a reader version bump): each row's cells and boxes reversed, the boxes descending, and the table's block and chunks written in
    that order. Test data only: the throwaway database, a table of a synthetic fixture."""
    from sqlalchemy import text

    from app.db import tenant_tx

    def flip(line: str) -> str:
        return " | ".join(reversed(line.split(" | ")))

    with tenant_tx(office.system) as conn:
        stored = conn.execute(text("SELECT version_id, structure FROM extracted_tables WHERE table_index = :t"),
                              {"t": table_index}).one()
        structure = stored.structure
        for row in structure["rows"]:
            row["cells"] = list(reversed(row["cells"]))
            row["cell_boxes"] = list(reversed(row["cell_boxes"]))
        conn.execute(text("UPDATE extracted_tables SET structure = CAST(:s AS jsonb) WHERE table_index = :t AND version_id = :v"),
                     {"s": json.dumps(structure, ensure_ascii=False), "t": table_index, "v": stored.version_id})
        for block in conn.execute(text("SELECT version_id, block_index, text FROM document_blocks WHERE table_index = :t"),
                                  {"t": table_index}).all():
            conn.execute(text("UPDATE document_blocks SET text = :x WHERE version_id = :v AND block_index = :b"),
                         {"x": "\n".join(flip(ln) for ln in block.text.split("\n")), "v": block.version_id, "b": block.block_index})
        for chunk in conn.execute(text("SELECT id, kind, text FROM chunks WHERE table_index = :t"), {"t": table_index}).all():
            if chunk.kind == "table":
                head, _, rest = chunk.text.partition("\n")
                new = head + "\n" + "\n".join(flip(ln) for ln in rest.split("\n"))
            else:
                prefix, _, row = chunk.text.partition(": ")
                new = prefix + ": " + flip(row)
            conn.execute(text("UPDATE chunks SET text = :x WHERE id = :i"), {"x": new, "i": chunk.id})


def pairs_in_drawn_order(text: str, row_label: str, pairs: list[tuple[str, str]]) -> bool:
    """Whether the line of ``row_label`` in ``text`` gives each (column label, value) as «label: value» (the pairing of a stored row chunk
    of a Hebrew table: «שטח (מ״ר): 120»), the labels in drawn order."""
    lines = [ln for ln in text.splitlines() if row_label in ln]
    if len(lines) != 1:
        return False
    at = -1
    for label, value in pairs:
        found = re.search(re.escape(label) + r"\s*[:=]\s*" + re.escape(value) + r"(?![\d,.])", lines[0][at + 1:])
        if found is None:
            return False
        at += found.end()
    return True


QUARTER_PAIRS = [("Q1 2024", "100"), ("Q2 2024", "200"), ("Annual", "300")]


@pytest.mark.db
def test_ae6_a_reversed_stored_table_with_left_to_right_evidence_is_read_in_drawn_order_with_its_column_labels(client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, QUARTERS)
    DOCUMENT_OF[0] = doc
    reverse_stored_table(office, 0)
    seen: list[str] = []

    def judge(input: str) -> dict:
        seen.append(input)
        return lenient_judge(input)

    steps = [[call("outline", document=doc)], [read(pages={"document": doc, "from_page": 2, "to_page": 2})],
             *[final(REVENUE_ANSWER, documents=[doc]) for _ in range(5)]]
    agent = ScriptedAgent(steps, judge=judge, request=[component("ההכנסות ברבעון הראשון של 2024")])
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), REVENUE_QUESTION)
    assert m["status"] == "done", m
    served = "\n".join(o for items in agent.seen for o in _outputs(items))
    assert pairs_in_drawn_order(served, "Revenue", QUARTER_PAIRS), served  # the agent reads each cell with its column label
    assert pairs_in_drawn_order(seen[0], "Revenue", QUARTER_PAIRS), seen[0]  # and the verifier reads the same pairing


# --- 10. a numbered item stated with its label inflected (AE7, U7) ----------------------------------------------------------------------------

CONDITION_ROWS = ["תנאי העסקה", "תנאי | פירוט", "אישור הוועדה | נדרש אישור ועדת התכנון", "מימון | הלוואה בנקאית",
                  "פינוי | פינוי המגרש לפני המסירה"]
CONDITION_ITEMS = [("אישור הוועדה", "נדרש אישור ועדת התכנון"), ("מימון", "הלוואה בנקאית"), ("פינוי", "פינוי המגרש לפני המסירה")]


def condition_list_lines(markdown: str):
    """The answer's items (a word-valued ledger of three rows, none with a figure) and the server's item lines for it."""
    ws = _file("סיכום: השווי למ\"ר הוא 9,500 ₪.", "\n".join(CONDITION_ROWS), units=(1, 1), declared=(1,))
    ws.sources["S2"].kind = "table"
    raw = [_adj_item(label, f"{label} | {detail}", "") for label, detail in CONDITION_ITEMS]
    report, turn, answer, _ = _ledger_check(ws, markdown, raw)
    final_answer, _ = _finish(ws, answer, report, turn)
    return _item_lines(final_answer), final_answer.answer_markdown


NUMBERED = ("1. אישור הוועדה: נדרש אישור ועדת התכנון [S2].\n"
            "3. המגרש יפונה לפני המסירה [S2].")  # the second item is omitted; the third is stated with its label inflected


def test_ae7_a_numbered_item_stated_with_its_label_inflected_gets_no_read_and_not_used_line_and_an_omitted_one_still_does():
    lines, md = condition_list_lines(NUMBERED)
    assert [ln.split("»")[0] for ln in lines] == ["«מימון"], md  # exactly the omitted item


# --- 11. a follow-up carries no condition of the previous answer (AE8) ---------------------------------------------------------------------


@pytest.mark.db
def test_protection_ae8_a_follow_up_about_another_group_carries_no_condition_of_the_previous_answer(client, office, monkeypatch):
    """Not reproduced on the round-20 build: the previous answer reaches the follow-up only as its history without the spans the server wrote
    (the registry saved with the message), and the item ledger is the turn's own, so no condition of the first answer is written, kept or
    shown in the second. Kept as a protection: the binding record of U2 must not introduce a carrier between turns."""
    doc = ingest_pdf(office, monkeypatch, LOTS)
    DOCUMENT_OF[0] = doc
    first = f"שטח קבוצת מגרשים א׳ הוא {GROUP_A_ROW[2]} מ״ר, לפי {GROUP_A_ROW[1]} [S1]."
    second = f"שטח קבוצת מגרשים ב׳ הוא {GROUP_B_ROW[2]} מ״ר, לפי {GROUP_B_ROW[1]} [S1]."
    outline = [call("outline", document=doc)]
    items = [lot_item(GROUP_A_ROW, FOOTNOTE)]
    agent = ScriptedAgent([outline, read_table(LOTS["caption"]), *[final(first, documents=[doc], items=items) for _ in range(5)]],
                          judge=lenient_judge, request=[component("השטח וחוזה החכירה של קבוצת מגרשים א׳")])
    cloud(monkeypatch, office, agent)
    login(client, "admin-b@example.test")
    cid = new_conversation(client)
    m1 = send(client, cid, "מהו השטח של קבוצת מגרשים א׳ ומהו חוזה החכירה שלה?")
    assert m1["status"] == "done" and "מינהל" in m1["answer"]["markdown"], m1  # the first answer carries the condition of group A
    agent.steps = [outline, read_table(LOTS["caption"]), *[final(second, documents=[doc]) for _ in range(5)]]
    shown = len(agent.seen)
    m2 = send(client, cid, "ומה השטח של קבוצת מגרשים ב׳?")
    md = m2["answer"]["markdown"]
    assert m2["status"] == "done" and GROUP_B_ROW[2] in md, m2
    assert "מינהל" not in md and "התנאי לפי המקור" not in md, md
    history = agent.seen[shown][0]["content"]
    assert "מינהל" not in history and "התנאי לפי המקור" not in history, history  # the earlier answer arrives without the server's words


# --- 12. a captured source rebuilds its text from its locator (U1, KTD9) -------------------------------------------------------------------


@pytest.mark.db
def test_a_captured_locator_rebuilds_the_text_of_its_source_through_the_serve_path(client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, QUARTERS)
    DOCUMENT_OF[0] = doc
    steps = [[call("outline", document=doc)], read_pages(2, 2), *[final(REVENUE_ANSWER, documents=[doc]) for _ in range(5)]]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=lenient_judge, request=[component("ההכנסות ברבעון הראשון של 2024")]))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), REVENUE_QUESTION)
    assert m["status"] == "done", m
    (draft,) = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["drafts"]
    assert draft["markdown"] == REVENUE_ANSWER
    locator = draft["sources"]["S1"]
    assert set(engine.LOCATOR_FIELDS) <= set(locator) and locator["block_start"] is not None, locator
    (source,) = [s for s in m["answer"]["sources"] if s["id"] == "S1"]
    assert rebuild_source_text(office.ctx(), locator) == source["text"]


@pytest.mark.db
def test_a_captured_table_locator_rebuilds_the_text_of_a_table_source(client, office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, LOTS)
    DOCUMENT_OF[0] = doc
    answer = f"שטח קבוצת מגרשים ב׳ הוא {GROUP_B_ROW[2]} מ״ר, לפי {GROUP_B_ROW[1]} [S1]."
    steps = [[call("outline", document=doc)], read_table(LOTS["caption"]), *[final(answer, documents=[doc]) for _ in range(5)]]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=lenient_judge, request=[component(LOTS_ASK)]))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), LOTS_QUESTION)
    assert m["status"] == "done", m
    draft = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["drafts"][0]
    locator = draft["sources"]["S1"]
    assert locator["kind"] == "table" and locator["table_index"] is not None, locator
    (source,) = [s for s in m["answer"]["sources"] if s["id"] == "S1"]
    assert rebuild_source_text(office.ctx(), locator) == source["text"]
