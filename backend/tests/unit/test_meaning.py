"""A presented number keeps the meaning its evidence gives it: its area basis as written, its period and its
approximation are carried, and nothing is added that the evidence lacks. Synthetic names, amounts and
phrasings only; the judge is scripted."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

import pytest

from app.chat import meaning
from app.chat.engine import FinalAnswer
from app.chat.tools import Measurement, Workspace
from app.chat.verify import split_units, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider


def _ws(*texts: str, kind: str = "context") -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת בדיקה", section="תחשיב",
                      location="סעיף \"תחשיב\"", kind=kind, text=t)
    return ws


def _problems(markdown: str, ws: Workspace) -> list[meaning.MeaningProblem]:
    (u,) = split_units(markdown)
    return meaning.check(u, ws)


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _judge(verdict: str = "supported") -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": int(n), "verdict": verdict, "reason": "בדיקה"}
        for n in re.findall(r'<unit index="(\d+)"', input)]}, repeat=True)
    return p


VALUE = "בפרק התחשיב: השווי למ\"ר אקוו' לנכס ברחוב הצאלון 7 נקבע ל-14,250 ₪, ללא מע\"מ."


# --- missing qualifiers -------------------------------------------------------------------------------------

def test_a_dropped_area_basis_is_a_missing_qualifier_with_one_annotation():
    (p,) = _problems("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].", _ws(VALUE))
    assert not p.blocking and p.kind == "basis" and p.annotation == "מ״ר אקוו׳"


def test_the_basis_written_in_any_spelling_passes():
    for said in ("למ\"ר אקוויוולנטי", "למ״ר אקו׳", "למ\"ר אקוו'"):
        assert _problems(f"השווי {said} בצאלון 7 הוא 14,250 ₪ [S1].", _ws(VALUE)) == []


@pytest.mark.parametrize("source,answer,annotation", [
    ("שטח הדירה בקומה 3 הוא 96 מ\"ר פלדלת.", "שטח הדירה הוא 96 מ\"ר [S1].", "מ״ר פלדלת"),
    ("לפי היתר הבנייה, שטח המבנה 412 מ\"ר ברוטו.", "שטח המבנה הוא 412 מ\"ר [S1].", "מ״ר ברוטו"),
    ("דמי הניהול בבניין הם 18 ₪ למ\"ר לחודש.", "דמי הניהול הם 18 ₪ למ\"ר [S1].", "לחודש"),
    ("התקבולים השנתיים מהחניון נאמדו בכ-310,000 ₪.", "התקבולים השנתיים מהחניון הם 310,000 ₪ [S1].", "מקורב"),
])
def test_needed_qualifiers_are_required(source, answer, annotation):
    (p,) = _problems(answer, _ws(source))
    assert not p.blocking and p.annotation == annotation


def test_a_period_in_the_column_header_is_needed_and_written_there_passes():
    table = "שכירות בבניין הדולב\nנכס | שכ\"ד לחודש (₪ למ\"ר)\nהדולב 4 | 71\nהדולב 6 | 64"
    ws = _ws(table, kind="table")
    (p,) = _problems("בדולב 6 דמי השכירות הם 64 ₪ למ\"ר [S1].", ws)
    assert p.kind == "period" and p.annotation == "לחודש"
    assert _problems("בדולב 6 דמי השכירות החודשיים הם 64 ₪ למ\"ר [S1].", ws) == []


def test_a_row_label_carries_the_basis():
    table = "סיכום שווי\nרכיב | ערך\nשווי למ\"ר פלדלת | 11,900\nשווי כולל | 1,130,500"
    (p,) = _problems("השווי למ\"ר הוא 11,900 ₪ [S1].", _ws(table, kind="table"))
    assert p.kind == "basis" and p.annotation == "מ״ר פלדלת"


def test_an_answer_table_header_counts_for_its_rows():
    table = "נכס | שכ\"ד לחודש (₪ למ\"ר)\nהדולב 4 | 71"
    units = split_units("| נכס | שכ\"ד לחודש |\n|---|---|\n| הדולב 4 | 71 ₪ [S1] |")
    assert meaning.check(units[1], _ws(table, kind="table")) == []


def test_two_bases_attested_ask_for_a_repair_without_annotation():
    src = ("שווי הדירה ברחוב הערבה 2 הוא 12,600 ₪ למ\"ר פלדלת.\n"
           "בטבלת ההשוואה, הדירה ברחוב הערבה 2 נמכרה ב-12,600 ₪ למ\"ר ברוטו.")
    (p,) = _problems("בערבה 2 הערך הוא 12,600 ₪ למ\"ר [S1].", _ws(src))
    assert not p.blocking and p.annotation is None and "פלדלת" in p.reason and "ברוטו" in p.reason


def test_the_same_number_with_another_meaning_elsewhere_is_not_required():
    src = "במגרש 64 חניות תת-קרקעיות.\nדמי השכירות לחניה הם 64 ₪ לחודש."
    assert _problems("במגרש יש 64 חניות תת-קרקעיות [S1].", _ws(src)) == []


def test_a_number_without_qualifiers_in_the_evidence_has_no_problem():
    assert _problems("מספר הקומות הוא 9 [S1].", _ws("הבניין בן 9 קומות מעל קומת קרקע.")) == []


def test_a_cited_measurement_gives_the_qualifiers():
    ws = _ws("טבלה")
    row = SimpleNamespace(value_text="8,350", quote="שווי למ\"ר 8,350", vat="unknown", metric="שווי למ\"ר",
                          metric_kind="value_per_area", unit="ILS_per_sqm", period="none", area_basis="ברוטו",
                          subject="השקמה 11", value_role="appraiser_determination", value_form="exact")
    ws.measurements["M1"] = Measurement("M1", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "שומה", row)
    (p,) = _problems("השווי למ\"ר בשקמה 11 הוא 8,350 ₪ [M1].", ws)
    assert p.kind == "basis" and p.annotation == "ברוטו"


# --- qualifiers the evidence does not give ---------------------------------------------------------------

def test_an_added_period_is_blocking():
    ws = _ws("שכ\"ד 63 ₪ למ\"ר לפי הסקר בשכונת רמת האלה.")
    (p,) = _problems("דמי השכירות הם 63 ₪ למ\"ר לחודש [S1].", ws)
    assert p.blocking and p.kind == "period"
    assert _problems("דמי השכירות הם 63 ₪ למ\"ר [S1].", ws) == []


def test_a_contradicting_period_is_blocking():
    ws = _ws("דמי השכירות לנכס נקבעו ל-760 ₪ למ\"ר לשנה.")
    problems = _problems("דמי השכירות הם 760 ₪ למ\"ר לחודש [S1].", ws)
    assert any(p.blocking and p.kind == "period" for p in problems)


def test_an_added_area_basis_is_blocking():
    (p,) = _problems("שטח המשרד 240 מ\"ר ברוטו [S1].", _ws("שטח המשרד בקומה הרביעית 240 מ\"ר."))
    assert p.blocking and p.kind == "basis"


def test_a_period_stated_once_for_the_whole_source_attests_it():
    src = "כל דמי השכירות בטבלה הם לחודש.\nהאורן 3 | 58\nהאורן 5 | 61"
    assert _problems("באורן 5 דמי השכירות הם 61 ₪ למ\"ר לחודש [S1].", _ws(src, kind="table")) == []


def test_qualifiers_belong_to_their_own_number():
    src = "דמי השכירות 63 ₪ למ\"ר לחודש, והשווי 14,250 ₪ למ\"ר אקוו'."
    answer = "דמי השכירות 63 ₪ למ\"ר לחודש והשווי 14,250 ₪ למ\"ר אקוו' [S1]."
    assert _problems(answer, _ws(src)) == []


# --- through verification -------------------------------------------------------------------------------------

def test_a_missing_basis_is_judged_and_finally_annotated():
    ws = _ws(VALUE)
    a = _answer("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].")
    p = _judge("supported")
    r = verify_answer(p, a, ws, "?", [])
    assert not r.ok and len(p.calls) == 1  # still judged, and still a problem for the repair round
    out = r.apply(a).answer_markdown
    assert out.startswith("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1].")
    assert "הוסרו" not in out


def test_a_missing_basis_on_an_unsupported_unit_is_removed_not_annotated():
    ws = _ws(VALUE)
    a = _answer("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].\nהנכס בצאלון 7 מושכר [S1].")
    r = verify_answer(_judge("unsupported"), a, ws, "?", [])
    out = r.apply(a).answer_markdown
    assert "כפי שנכתב במקור" not in out and "14,250" not in out


def test_an_added_period_is_removed_without_judging_it():
    ws = _ws("שכ\"ד 63 ₪ למ\"ר לפי הסקר בשכונת רמת האלה.")
    a = _answer("דמי השכירות הם 63 ₪ למ\"ר לחודש [S1].")
    p = _judge("supported")
    r = verify_answer(p, a, ws, "?", [])
    assert [x.kind for x in r.problems] == ["claim"] and not p.calls
    assert "63" not in r.apply(a).answer_markdown


@pytest.mark.parametrize("source,answer", [
    ("החוזה נחתם לפני שנה, והשווי למ\"ר נקבע ל-9,150 ₪.", "השווי למ\"ר הוא 9,150 ₪ [S1]."),
    ("תקופת השכירות 12 חודשים, ודמי השכירות 4,700 ₪.", "דמי השכירות הם 4,700 ₪ [S1]."),
])
def test_a_duration_or_a_date_is_not_a_period(source, answer):
    assert _problems(answer, _ws(source)) == []


def test_a_slash_period_is_a_period():
    ws = _ws("דמ\"ש ראויים בגבולות של 47 ₪ למ\"ר/חודש.")
    assert _problems("דמי השכירות הראויים הם 47 ₪ למ\"ר לחודש [S1].", ws) == []
    (p,) = _problems("דמי השכירות הראויים הם 47 ₪ למ\"ר [S1].", ws)
    assert p.kind == "period" and p.annotation == "לחודש"


def test_a_basis_of_two_words_is_one_basis():
    ws = _ws("ראוי לקבוע את השווי למ\"ר בנוי ברוטו למסחר בגבולות של 8,700 ₪.")
    (p,) = _problems("השווי נקבע 8,700 ₪ [S1].", ws)
    assert p.annotation == "מ״ר בנוי ברוטו"


# --- review fixes -------------------------------------------------------------------------------------------

def test_a_total_abbreviation_is_not_an_approximation():
    ws = _ws("שווי הבניין: סה״כ 96,250,000 ₪, כולל מע״מ.")
    assert _problems("שווי הבניין הוא 96,250,000 ₪, כולל מע״מ [S1].", ws) == []


def test_a_period_written_before_its_amount_belongs_to_it():
    ws = _ws("הנכס בשטח 120 מ\"ר, דמי השכירות לחודש 7,560 ₪ ללא מע\"מ.")
    assert _problems("דמי השכירות הם 7,560 ₪ לחודש [S1].", ws) == []
    assert _problems("דמי השכירות החודשיים הם 7,560 ₪ [S1].", ws) == []
    assert _problems("שטח הנכס 120 מ\"ר [S1].", ws) == []  # the period is not the area's
    (p,) = _problems("דמי השכירות הם 7,560 ₪ [S1].", ws)
    assert p.kind == "period" and not p.blocking


def test_an_approximate_number_written_as_approximate_passes():
    ws = _ws("התקבולים השנתיים מהחניון נאמדו בכ-310,000 ₪.")
    assert _problems("התקבולים השנתיים הם כ-310,000 ₪ [S1].", ws) == []


# --- meaning stated elsewhere in the same calculation (plan 2026-10-07-1643, KTD5) ---------------------------

CALC = ("תחשיב שווי בגישת היוון ההכנסות:\nרכיב | ערך\nסה\"כ מ\"ר אקווי' | 2,480\nדמ\"ש למ\"ר | ₪ 52\n"
        "הכנסה שנתית | ₪ 1,547,520\nשווי מעוגל | ₪ 20,630,000\n(*) דמי השכירות בטבלה הם לחודש, ללא מע\"מ.")


def test_a_rate_gets_the_basis_of_its_tables_area_row_and_the_period_of_its_notes():
    found = {p.kind: p for p in _problems("דמי השכירות בתחשיב הם 52 ₪ למ\"ר [S1].", _ws(CALC, kind="table"))}
    assert found["basis"].annotation == "מ״ר אקווי׳" and not found["basis"].blocking
    assert found["period"].annotation == "לחודש"
    # the total of the same table gets neither
    assert _problems("השווי המעוגל הוא 20,630,000 ₪ [S1].", _ws(CALC, kind="table")) == []


def test_a_table_with_two_bases_binds_none():
    two = CALC.replace("דמ\"ש למ\"ר | ₪ 52", "שטח פלדלת במ\"ר | 2,100\nדמ\"ש למ\"ר | ₪ 52")
    assert not [p for p in _problems("דמי השכירות הם 52 ₪ למ\"ר [S1].", _ws(two, kind="table")) if p.kind == "basis"]


def test_text_around_a_table_in_a_long_passage_is_not_the_tables_definition():
    passage = "\n".join([f"פסקה {i}: תיאור כללי של הנכס." for i in range(5)]
                        + ["שטח הבניין נמדד במ\"ר אקוו'.", "רכיב | ערך", "דמ\"ש למ\"ר | ₪ 52"])
    assert not [p for p in _problems("דמי השכירות הם 52 ₪ למ\"ר [S1].", _ws(passage)) if p.kind == "basis"]


def test_spellings_of_the_equivalent_basis_are_one_basis_shown_as_written():
    src = "השווי למ\"ר אקוו' לנכס נקבע ל-14,250 ₪."
    for said in ("אקווי׳", "אקוי׳", "אקו׳"):
        assert _problems(f"השווי למ״ר {said} הוא 14,250 ₪ [S1].", _ws(src)) == [], said
    (p,) = _problems("השווי למ\"ר הוא 52 ₪ [S1].", _ws("השווי למ\"ר אקווי' נקבע ל-52 ₪."))
    assert p.annotation == "מ״ר אקווי׳"


def _same_document(*sections: tuple[str, str]) -> Workspace:
    ws = Workspace(ctx=None)
    doc, ver = uuid.uuid4(), uuid.uuid4()
    for section, t in sections:
        ws.add_source(document_id=doc, version_id=ver, title="שומת בדיקה", section=section,
                      location=f"סעיף \"{section}\"", kind="context", text=t)
    return ws


def test_a_correct_period_written_elsewhere_in_the_calculation_is_cited_not_removed():
    ws = _same_document(("תחשיב", "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר."),
                        ("תחשיב", "לאחר התאמות, דמי השכירות הראויים נקבעו ל-70 ₪ למ\"ר לחודש."))
    (u,) = split_units("דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1].")
    (p,) = meaning.check(u, ws, meaning.Fetcher(ws))
    assert p.needs_citation and p.cite == "S2" and not p.blocking and "S2" in u.ids
    out = verify_answer(_judge(), _answer("דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1]."), ws, "?", [])
    final = out.apply(_answer("דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1]."))
    assert out.ok and "לחודש" in final.answer_markdown and "[S1][S2]" in final.answer_markdown


def test_the_same_number_with_a_period_in_a_comparables_section_attests_nothing():
    ws = _same_document(("תחשיב", "דמי השכירות הראויים לנכס הם 70 ₪ למ\"ר."),
                        ("סקר שוק", "עסקת השוואה ברחוב הערבה: 70 ₪ למ\"ר לחודש."))
    (u,) = split_units("דמי השכירות הראויים הם 70 ₪ למ\"ר לחודש [S1].")
    (p,) = meaning.check(u, ws, meaning.Fetcher(ws))
    assert p.blocking and p.kind == "period" and u.ids == ["S1"]


# --- an area basis qualifies an area or a per-area amount, never a total in ₪ or a factor --------------------------

AREA_CALC = ("תחשיב מצב חדש\nמהות | שטח | מקדם אקוו' | סה\"כ מ\"ר אקוו'\nשטח עיקרי | 145 | 1 | 145\n"
        "שטח חצר | 100 | 0.25 | 25\nמקדם דחיה | | | 0.8712\nשווי מצב חדש | | | 28,940,300")


@pytest.mark.parametrize("answer", [
    "שווי המצב החדש שנקבע הוא 28,940,300 ₪ [S1].",
    "הובא בחשבון מקדם דחיה של 0.8712 [S1].",
])
def test_a_total_or_a_factor_in_an_area_table_is_not_given_the_tables_area_basis(answer):
    assert _problems(answer, _ws(AREA_CALC, kind="table")) == []


def test_an_area_in_the_same_table_still_needs_its_basis():
    (p,) = _problems("שטח החצר המשוקלל הוא 25 מ\"ר [S1].", _ws(AREA_CALC, kind="table"))
    assert p.kind == "basis" and "אקוו" in p.annotation


def test_the_equivalent_basis_written_without_a_geresh_is_that_basis():
    src = "שווי הנכס: 8,640 ₪ X 68.40 מ\"ר אקווי'."
    for said in ("אקווי", "אקוי"):
        assert _problems(f"בתחשיב השווי הוכפל ב-68.40 מ״ר {said} [S1].", _ws(src)) == [], said


def test_a_per_area_amount_named_only_by_its_column_header_still_needs_its_basis():
    table = "דירות בבניין\nדירה | שטח | שווי למ\"ר\nדירה 3 | 80 | 8,700\nסה\"כ מ\"ר אקוו' | 2,480"
    (p,) = _problems("השווי של דירה 3 הוא 8,700 ₪ [S1].", _ws(table, kind="table"))
    assert p.kind == "basis" and "אקוו" in p.annotation


def test_a_per_area_word_belongs_to_the_figure_it_follows_in_a_list():
    answer = ("למצב החדש נרשמו 145 ₪ למ\"ר מבונה, שווי מצב חדש של 28,940,300 ₪, ומקדם דחיה 0.8712 [S1].")
    numbers = {p.number for p in _problems(answer, _ws(AREA_CALC, kind="table"))}
    assert not numbers & {"28,940,300", "0.8712"}
