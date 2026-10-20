"""Reproductions of the round-20 failure shapes, recorded failing on the round-19 build (round 20 U8: R1-R22; AE1-AE10;
KTD1-KTD5).

Each sequence is a ScriptedAgent turn over a synthetic document (office B): the model and the judge are scripted, so a test
proves what the server does with an answer that is CORRECT for its fixture, not the judgement of a model. Each open case is
marked ``xfail(strict=True, raises=AssertionError)`` with the unit that owns the fix and what fails, and the owning unit removes
the marker when its fix lands (a strict xfail that starts passing fails the suite, so the marker cannot be forgotten). A
protection sequence (a wrong datum that must still be removed or flagged) is no xfail: it passes on the round-19 build and
must keep passing. Every assertion is on what the user sees (the final message and the server's lines in it), on the
diagnostics the message stores, or on what the judge is shown, never on a call inside the server.

The shapes that need no document (the ledger's merge, the final check of a table row, the ingestion order, the gap lines) are
in ``tests/unit/test_round20_shapes.py``. Here:

1. Building rights of two plans in one table (AE6, U3): the answer puts plan B's service area under plan A's column.
2. Two lot groups with a footnote marked on the first group's contract (AE7, U3): the footnote is not the condition of the
   second group's item, and the server writes none.
3. Two parcels with a value each (U3): parcel 2's value written under parcel 1 is flagged, in a bullet whose lead names the
   parcel and in a plain sentence that names it.
4. Two appraisals compared in a sentence of their own, the first with a condition (U3): the server writes no condition into the
   shared sentence when a sibling sentence states it.
5. A unit titled by name and labelled by its address (AE8, U3): the judge is shown the stored link between the two.

The documents are the synthetic fixtures of ``tests/fixtures/round20`` (``scripts/generate_fixtures.py --only round20``);
every name and figure is invented."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_round18_sequences import (
    DOCUMENT_OF,
    ask,
    ingest_pdf,
    lenient_judge,
    open_section,
    read_pages,
    read_table,
)
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read

pytestmark = pytest.mark.db

REPAIRS = 4  # the same answer again at each step a verification round may ask the agent for
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round20" / "manifest.json").read_text(encoding="utf-8"))["documents"]


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד ב", "admin-b@example.test")


def removals(client, m: dict) -> list[tuple[str, str]]:
    """(check, failure kind) of each claim the turn removed, from the diagnostics the message stores."""
    removed = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["removed"]
    return [(r["check"], r["failure_kind"]) for r in removed]


def strict_xfail(unit: str, what: str):
    """The marker of an open case: it fails with an assertion on the round-19 build, for the reason ``what``, and ``unit`` owns the fix."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{unit}: {what}")


# --- 1. building rights of two plans in one table (AE6, U3) -----------------------------------------------------------------

PLANS = MANIFEST["two_plans"]
PLAN_A, PLAN_B = PLANS["plans"]["a"], PLANS["plans"]["b"]
PLANS_QUESTION = "מהן זכויות הבנייה בתכנית ג/11111 ובתכנית ג/22222?"
PLANS_ASK = "זכויות הבנייה בכל אחת מהתכניות"
PLANS_HEAD = f"| רכיב | {PLAN_A} | {PLAN_B} |\n|---|---|---|\n"
MAIN_ROW = "| שטח עיקרי (מ״ר) | 1,200 [S1] | 1,800 [S1] |\n"


def test_each_figure_of_a_rights_table_stays_under_the_plan_whose_column_it_is_written_in(client, office, monkeypatch):
    answer = "זכויות הבנייה לפי כל תכנית:\n\n" + PLANS_HEAD + MAIN_ROW + "| שטח שירות (מ״ר) | 300 [S1] | 450 [S1] |\n"
    m = ask(client, office, monkeypatch, PLANS, read_table(PLANS["caption"]), answer, PLANS_QUESTION, PLANS_ASK)
    md = m["answer"]["markdown"]
    for figure in ("1,200", "1,800", "300", "450"):
        assert figure in md, (figure, md)
    assert removals(client, m) == [], md


def test_a_figure_of_plan_b_written_under_plan_a_is_cut_and_plan_as_own_figures_stay(client, office, monkeypatch):
    misplaced = "| שטח שירות (מ״ר) | 450 [S1] | — |\n"
    m = ask(client, office, monkeypatch, PLANS, read_table(PLANS["caption"]),
            "זכויות הבנייה לפי כל תכנית:\n\n" + PLANS_HEAD + MAIN_ROW + misplaced, PLANS_QUESTION, PLANS_ASK)
    md = m["answer"]["markdown"]
    assert "1,200" in md and "1,800" in md, md  # the cells that stand under their own plan
    assert "450" not in md, md  # plan B's figure is not stated for plan A


def test_the_figure_of_plan_b_under_plan_a_is_cut_alone_and_the_cell_of_plan_b_beside_it_stays(client, office, monkeypatch):
    """The cell-level outcome (round 20 U3, KTD3, with U2's path): the row keeps the cell that stands, and the cell of the wrong plan goes."""
    both = "| שטח שירות (מ״ר) | 450 [S1] | 450 [S1] |\n"
    m = ask(client, office, monkeypatch, PLANS, read_table(PLANS["caption"]),
            "זכויות הבנייה לפי כל תכנית:\n\n" + PLANS_HEAD + MAIN_ROW + both, PLANS_QUESTION, PLANS_ASK)
    md = m["answer"]["markdown"]
    row = next(ln for ln in md.splitlines() if "שטח שירות" in ln)
    assert row.count("450") == 1 and "חלק שלא אומת הושמט" in row and row.index("הושמט") < row.index("450"), md  # plan A's cell is the cut one
    assert "1,200" in md and "1,800" in md, md
    assert ("scope_mismatch", "wrong_subject") in removals(client, m), removals(client, m)


# --- 2. a footnote marked on the first lot group's contract (AE7, U3) -------------------------------------------------------

LOTS = MANIFEST["lot_groups"]
FOOTNOTE = LOTS["footnote"]
LOTS_QUESTION = "מהו השטח של קבוצת מגרשים ב׳ ומהו חוזה החכירה שלה?"
LOTS_ASK = "השטח של קבוצת מגרשים ב׳ וחוזה החכירה שלה"
GROUP_A_ROW, GROUP_B_ROW = LOTS["rows"]
GROUP_B_ANSWER = f"שטח קבוצת מגרשים ב׳ הוא {GROUP_B_ROW[2]} מ״ר, לפי {GROUP_B_ROW[1]} [S1]."
GROUP_A_ANSWER = f"שטח קבוצת מגרשים א׳ הוא {GROUP_A_ROW[2]} מ״ר, לפי {GROUP_A_ROW[1]} [S1]."


def lot_item(row: list[str], condition: str) -> dict:
    return {"component": "N1", "label": row[0], "source": "S1", "quote": " | ".join(row), "value": f"{row[2]} מ״ר",
            "condition": condition, "state": "included", "reason": ""}


def test_a_footnote_marked_on_one_lot_groups_contract_is_not_the_condition_of_the_other_groups_items(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, LOTS, read_table(LOTS["caption"]), GROUP_B_ANSWER, LOTS_QUESTION, LOTS_ASK,
            items=[lot_item(GROUP_B_ROW, FOOTNOTE)])
    md = m["answer"]["markdown"]
    assert GROUP_B_ROW[2] in md, md
    assert "מינהל" not in md and "אישור" not in md, md  # group A's footnote does not limit group B


# --- 3. two parcels with a value each (U3) ----------------------------------------------------------------------------------

PARCELS = MANIFEST["parcels"]
PARCEL_1, PARCEL_2 = PARCELS["parcels"]
PARCELS_QUESTION = "מהו שווי הקרקע בחלקה 12 ובחלקה 14?"
PARCELS_ASK = "שווי הקרקע בכל אחת מהחלקות"
ONE_PARCEL_QUESTION = "מהו שווי הקרקע בחלקה 12?"
ONE_PARCEL_ASK = "שווי הקרקע בחלקה 12"


def test_each_parcels_value_stays_under_the_parcel_its_bullet_names(client, office, monkeypatch):
    answer = (f"- חלקה {PARCEL_1['parcel']}: שווי הקרקע {PARCEL_1['value']} ₪ [S1]\n"
              f"- חלקה {PARCEL_2['parcel']}: שווי הקרקע {PARCEL_2['value']} ₪ [S1]")
    m = ask(client, office, monkeypatch, PARCELS, read_pages(1, 1), answer, PARCELS_QUESTION, PARCELS_ASK)
    md = m["answer"]["markdown"]
    assert PARCEL_1["value"] in md and PARCEL_2["value"] in md, md
    assert removals(client, m) == [], md


def test_the_value_of_parcel_2_written_in_the_bullet_of_parcel_1_is_flagged(client, office, monkeypatch):
    answer = (f"- חלקה {PARCEL_1['parcel']}: שווי הקרקע {PARCEL_2['value']} ₪ [S1]\n"
              f"- חלקה {PARCEL_2['parcel']}: שווי הקרקע {PARCEL_2['value']} ₪ [S1]")
    m = ask(client, office, monkeypatch, PARCELS, read_pages(1, 1), answer, PARCELS_QUESTION, PARCELS_ASK)
    md = m["answer"]["markdown"]
    bullet_of_1 = next((ln for ln in md.splitlines() if f"חלקה {PARCEL_1['parcel']}" in ln), "")
    assert PARCEL_2["value"] not in bullet_of_1, md  # parcel 12 is not valued at parcel 14's value


def test_the_value_of_another_parcel_stated_under_the_asked_parcel_in_a_plain_sentence_is_flagged(client, office, monkeypatch):
    """Not a protection test: the round-19 build does not flag this either, so U3 owns it with the bullet case above."""
    m = ask(client, office, monkeypatch, PARCELS, read_pages(1, 1),
            f"שווי הקרקע בחלקה {PARCEL_1['parcel']} הוא {PARCEL_2['value']} ₪ [S1].", ONE_PARCEL_QUESTION, ONE_PARCEL_ASK)
    md = m["answer"]["markdown"]
    assert PARCEL_2["value"] not in md, md


# --- 4. two appraisals compared in a sentence of their own, the first with a condition (U3) --------------------------------------

SHARED = MANIFEST["shared_sentence"]
SHARED_A, SHARED_B = SHARED["reports"]
SHARED_QUESTION = "השווה בין שווי הנכס ברחוב האלה 5 לבין השומה בעמוד 2"
SHARED_SUBJECT = "הנכס ברחוב האלה 5 והשומה בעמוד 2"
SHARED_ASK = "השוואת שווי הנכס בין הנכס ברחוב האלה 5 לבין השומה בעמוד 2"
SHARED_SENTENCE = (f"- שווי הנכס בגישת ההשוואה הוא {SHARED_A['value']} ₪ בנכס ברחוב האלה 5 וגם {SHARED_B['value']} ₪ "
                   "בשומה בעמוד 2 [S1][S2].")
SIBLING_SENTENCE = f"- הערך של הנכס ברחוב האלה 5 הוא {SHARED_A['condition']} [S1]."
SERVER_QUALIFIER = "התנאי לפי המקור"


def compare_sides(client, office, monkeypatch, answer: str) -> dict:
    """A turn that compares the two appraisals of the file: the agent reads page 1 (S1) and page 2 (S2) and answers ``answer``."""
    doc = ingest_pdf(office, monkeypatch, SHARED)
    DOCUMENT_OF[0] = doc
    steps = [[call("outline", document=doc)],
             [read(pages={"document": doc, "from_page": 1, "to_page": 1}), read(pages={"document": doc, "from_page": 2, "to_page": 2})],
             *[final(answer, documents=[doc]) for _ in range(1 + REPAIRS)]]
    cloud(monkeypatch, office, ScriptedAgent(steps, judge=lenient_judge, request=[component(SHARED_ASK, subject=SHARED_SUBJECT)]))
    login(client, "admin-b@example.test")
    m = send(client, new_conversation(client), SHARED_QUESTION)
    assert m["status"] == "done", m
    return m


def test_the_server_writes_no_condition_into_a_sentence_about_two_appraisals_when_a_sibling_sentence_states_it(
        client, office, monkeypatch):
    m = compare_sides(client, office, monkeypatch, f"{SHARED_SENTENCE}\n{SIBLING_SENTENCE}")
    md = m["answer"]["markdown"]
    assert SHARED_A["value"] in md and SHARED_B["value"] in md, md
    assert SHARED_A["condition"] in md, md  # the sibling sentence states it
    assert SERVER_QUALIFIER not in md, md


def test_protection_a_sentence_about_two_appraisals_that_no_sentence_qualifies_still_carries_the_first_appraisals_condition(
        client, office, monkeypatch):
    """The condition of the first appraisal is not lost when nothing states it: the server writes it, or a labelled note of it."""
    m = compare_sides(client, office, monkeypatch, SHARED_SENTENCE)
    md = m["answer"]["markdown"]
    assert SHARED_A["value"] in md and SHARED_B["value"] in md, md
    assert SHARED_A["condition"] in md, md


# --- 5. a unit titled by name and labelled by its address (AE8, U3) ---------------------------------------------------------

NAMES = MANIFEST["name_address"]["reports"][0]
NAME_QUESTION = f"מהו שווי הנכס בגישת ההשוואה של {NAMES['name']}?"
NAME_ASK = f"שווי הנכס בגישת ההשוואה של {NAMES['name']}"
NAME_ANSWER = f"שווי הנכס בגישת ההשוואה של {NAMES['name']} הוא {NAMES['value']} ₪ [S1]."


def judge_that_records(seen: list[str]):
    def judge(input: str) -> dict:
        seen.append(input)
        return lenient_judge(input)

    return judge


def outside_the_passages(input: str) -> list[str]:
    """The lines of a judge call without the text of the sources it shows: the headers of the sources (their attributes) and everything
    the server wrote beside the units."""
    bare = re.sub(r"(<source [^>]*>).*?</source>", r"\1", input, flags=re.S)
    return [ln for ln in bare.splitlines() if "<unit" not in ln]


def test_the_value_of_a_unit_is_kept_when_the_question_names_it_by_name_and_the_unit_is_labelled_by_its_address(
        client, office, monkeypatch):
    seen: list[str] = []
    m = ask(client, office, monkeypatch, MANIFEST["name_address"], open_section("1. הערכת שווי"), NAME_ANSWER, NAME_QUESTION,
            NAME_ASK, judge=judge_that_records(seen))
    assert NAMES["value"] in m["answer"]["markdown"] and removals(client, m) == []
    # the unit the judge is shown is labelled by its address, the name stays in the title block the section read does not hold
    assert any(NAMES["street"] in ln for ln in outside_the_passages(seen[0])), seen[0]


def test_the_judge_is_given_the_stored_link_between_the_name_in_the_question_and_the_address_that_labels_the_unit(
        client, office, monkeypatch):
    seen: list[str] = []
    ask(client, office, monkeypatch, MANIFEST["name_address"], open_section("1. הערכת שווי"), NAME_ANSWER, NAME_QUESTION,
        NAME_ASK, judge=judge_that_records(seen))
    linked = [ln for ln in outside_the_passages(seen[0]) if NAMES["name"] in ln and NAMES["street"] in ln]
    assert linked, [ln for ln in outside_the_passages(seen[0]) if ln.strip()]


def test_protection_a_question_that_names_the_addressee_gets_no_fact_that_links_the_addressee_to_the_unit(client, office, monkeypatch):
    """The addressee's name and street stand in the title block of both reports: they are no name of the unit, and the judge is told no link."""
    seen: list[str] = []
    question = "מהו שווי הנכס של מר דוד דמו מרחוב הדמיון 3?"
    ask(client, office, monkeypatch, MANIFEST["name_address"], open_section("1. הערכת שווי"), f"שווי הנכס בגישת ההשוואה הוא {NAMES['value']} ₪ [S1].",
        question, "שווי הנכס של מר דוד דמו", judge=judge_that_records(seen))
    outside = "\n".join(outside_the_passages(seen[0]))
    assert "<ownership" not in outside or "דוד דמו" not in outside.split("<ownership", 1)[1].split("</ownership>", 1)[0], outside

