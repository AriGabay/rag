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
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
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


def test_a_monthly_rent_table_does_not_attest_a_period_for_a_project_total():
    source = 'שווי הפרויקט הוא 2,730,000 ₪.\nנכס | שכ"ד לחודש (₪ למ"ר)\nהדולב 6 | 64'
    q = meaning.number_qualifiers(source, frozenset({"2730000", "2,730,000"}), 'שווי הפרויקט הוא 2,730,000 ₪.')
    assert not q.keys("period")
    assert _problems('שווי הפרויקט הוא 2,730,000 ₪ [S1].', _ws(source)) == []


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
    # round 12 U9: the stored basis counts as evidence only when its own words are in the measurement's quote
    row = SimpleNamespace(value_text="8,350", quote="שווי למ\"ר ברוטו 8,350", vat="unknown", metric="שווי למ\"ר",
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
    assert r.ok and len(p.calls) == 1  # still judged; the server writes the qualifier in itself, so no repair round
    out = r.apply(a).answer_markdown
    assert out.startswith("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1].")
    assert "הוסרו" not in out


def test_a_missing_basis_on_an_unsupported_unit_is_removed_not_annotated():
    ws = _ws(VALUE)
    a = _answer("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].\nהנכס בצאלון 7 מושכר [S1].")
    r = verify_answer(_judge("unsupported"), a, ws, "?", [])
    out = r.apply(a).answer_markdown
    assert "כפי שנכתב במקור" not in out and "14,250" not in out


def test_an_added_period_is_removed_and_the_number_stays_without_judging_it():
    # round 12 U10 (R7, KTD10): the source is silent on the period, so only the added "לחודש" goes; the number it
    # states stays
    ws = _ws("שכ\"ד 63 ₪ למ\"ר לפי הסקר בשכונת רמת האלה.")
    a = _answer("דמי השכירות הם 63 ₪ למ\"ר לחודש [S1].")
    p = _judge("supported")
    r = verify_answer(p, a, ws, "?", [])
    assert [x.kind for x in r.problems] == ["claim"] and not p.calls
    out = r.apply(a).answer_markdown
    assert "63" in out and "לחודש" not in out


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


def test_a_table_note_qualifies_the_rent_rate_only_when_a_total_is_taken():
    total = meaning.number_qualifiers(CALC, frozenset({"20630000", "20,630,000"}), 'שווי מעוגל | ₪ 20,630,000')
    rate = meaning.number_qualifiers(CALC, frozenset({"52"}), 'דמ"ש למ"ר | ₪ 52')
    assert not total.keys("period")
    assert rate.keys("period") == {"month"}


def test_a_headed_income_table_keeps_the_period_of_its_caption():
    source = 'ההכנסות בטבלה הן לשנה.\nרכיב | סכום (₪)\nשכירות | 100,000\nחניה | 20,000\nסה״כ | 120,000'
    for number, row in [('100,000', 'שכירות'), ('20,000', 'חניה'), ('120,000', 'סה״כ')]:
        assert _periods(source, number, f'{row} | {number}') == {"year"}


def test_a_headerless_rent_and_capital_table_keeps_the_period_off_capital():
    source = CALC.replace('\nרכיב | ערך', '')
    assert _periods(source, '20,630,000', 'שווי מעוגל | ₪ 20,630,000') == set()
    assert _periods(source, '52', 'דמ"ש למ"ר | ₪ 52') == {"month"}
    assert _periods(source, '2,480', 'סה"כ מ"ר אקווי\' | 2,480') == set()


def test_a_rent_caption_does_not_give_a_month_to_a_capital_rate():
    source = CALC + '\nשווי למ"ר | ₪ 8,320'
    assert _periods(source, '8,320', 'שווי למ"ר | ₪ 8,320') == set()


def test_a_rent_caption_does_not_give_a_month_to_a_purchase_price():
    source = CALC + '\nמחיר רכישה | ₪ 2,000,000'
    assert _periods(source, '2,000,000', 'מחיר רכישה | ₪ 2,000,000') == set()


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


def test_a_currency_stated_with_a_scale_attests_the_currency():
    """Round 7 R14: the scale is carried apart (``calc.stated_scale``), so "באלפי ₪" attests ₪ like a plain ₪."""
    from app.chat.meaning import units_attested
    assert units_attested("סך ההכנסות (באלפי ₪) 412,300") == {"ILS"}
    assert units_attested("עלות 5,000,000 ₪") == {"ILS"}
    assert units_attested("שיעור 17%") == {"percent"}
    assert units_attested("12 קומות") == set()


# --- a period word attests a number of its own line, sentence or governing line only (final evaluation, round 7) ----
#
# A feasibility section in thousands: a heading-like line, one figure per paragraph, and a later paragraph on the
# financing whose "ריבית שנתית" wraps onto the next line — the interest rate's period, in another paragraph.

FEASIBILITY = ("ממצאי בדיקת הכדאיות לפרויקט באלפי ₪ לא כולל מע״מ\nסה״כ הכנסות היזם 117,860\n"
               "סה״כ הוצאות (כולל מימון ועקיפות) 104,610-\nרווח שוטף (הפסד) 13,250\nשיעור רווח לעלות (הפסד) 12.7%\n"
               "הבדיקה נערכה על בסיס הנחות היזם, לרבות מימון בנקאי בריבית שנתית\nשל 6% לכל תקופת ההקמה.")


def _periods(text: str, number: str, within: str) -> set[str]:
    return meaning.number_qualifiers(text, frozenset(meaning.numbers_in(number)), within).keys("period")


@pytest.mark.parametrize("number, within", [
    ("104,610", "סה״כ הוצאות (כולל מימון ועקיפות) 104,610-"),
    ("13,250", "רווח שוטף (הפסד) 13,250"),
    ("12.7", "שיעור רווח לעלות (הפסד) 12.7%"),
])
def test_an_interest_rates_year_in_another_paragraph_is_not_the_totals_period(number, within):
    assert _periods(FEASIBILITY, number, within) == set()


def test_an_interest_rates_period_is_the_rates_own():
    assert _periods("המימון הבנקאי בריבית שנתית של 6% לכל תקופת ההקמה.", "6", "בריבית שנתית של 6%") == {"year"}
    # a line on the financing above the figures states the rate's period, not theirs
    above = "המימון חושב בריבית שנתית.\nסה״כ הוצאות 104,610"
    assert _periods(above, "104,610", "סה״כ הוצאות 104,610") == set()


@pytest.mark.parametrize("text, number, within", [
    ("סה״כ ההכנסות מהחניון 310,000 ₪ לשנה.", "310,000", "ההכנסות מהחניון 310,000"),  # its own line
    ("ההכנסות המוצגות להלן הן לשנה\nהכנסות מהחניון 310,000\nהכנסות מהמחסנים 42,500", "42,500", "המחסנים 42,500"),
    ("דמי השכירות לנכס נקבעו ל-310,000 ₪\nלשנה, בהתאם להסכם.", "310,000", "נקבעו ל-310,000 ₪"),  # wrapped
])
def test_a_genuine_period_on_the_values_line_its_sentence_or_its_heading_line_still_attests_it(text, number, within):
    assert _periods(text, number, within) == {"year"}


def test_a_period_in_a_later_paragraph_is_not_the_figures():
    text = "סה״כ הכנסות היזם 117,860\nרווח שוטף 13,250\nההצמדה למדד מחושבת לשנה."
    assert _periods(text, "13,250", "רווח שוטף 13,250") == set()
    # nor does it attest a period an answer gives the figure
    (p,) = _problems("הרווח השוטף הוא 13,250 ₪ לשנה [S1].", _ws(text))
    assert p.blocking and p.kind == "period"


@pytest.mark.parametrize("source", [
    "ההכנסות המוצגות להלן הן לשנה\nהכנסות מהחניון 310,000",
    "הכנסות מהחניון 310,000 ₪\nלשנה, בהתאם להסכם.",
])
def test_a_governing_period_is_required_when_the_answer_omits_it(source):
    (p,) = _problems("הכנסות מהחניון הן 310,000 ₪ [S1].", _ws(source))
    assert not p.blocking and p.kind == "period" and p.annotation == "לשנה"


def test_a_period_heading_does_not_cross_a_numbered_new_section():
    source = "ההכנסות לשנה\nהכנסות 100\n8. סיכום עלויות הפרויקט\nעלות כוללת 450"
    assert _periods(source, "450", "עלות כוללת 450") == set()


def test_a_wrapped_interest_rate_keeps_its_own_period():
    source = "מימון בנקאי בריבית שנתית\nשל 6% לכל תקופת ההקמה."
    assert _periods(source, "6", "של 6% לכל תקופת ההקמה") == {"year"}


def test_an_expanded_source_can_attest_its_governing_period():
    ws = _ws("הכנסות מהחניון 310,000")
    original = ws.sources["S1"]
    expanded = ws.add_source(document_id=original.document_id, version_id=original.version_id,
                             title=original.title, section=original.section, location="", kind="context",
                             text="ההכנסות לשנה\nהכנסות מהחניון 310,000")
    (unit,) = split_units("הכנסות מהחניון הן 310,000 ₪ לשנה [S1].")
    assert meaning.Fetcher(ws).attesting(unit, frozenset(meaning.numbers_in("310,000")),
                                        "period", "year", set()) == expanded.sid


@pytest.mark.parametrize("context, units", [
    ("מספר יח\"ד בפרויקט", {"units"}),
    ("מקדם התאמה", {"ratio"}),
    ("מספר השנים שנותרו בחכירה", set()),
    ("מספר חודשי הגרייס", set()),
])
def test_a_count_is_units_but_a_number_of_years_is_not(context, units):
    from app.chat.meaning import units_attested

    assert units_attested(context) == units


def test_a_line_above_on_another_kind_gives_no_period_to_a_capital_value():
    source = 'השוכר משלם בנוסף דמי ניהול לחודש.\nשווי השוק של הנכס הינו 3,200,000 ₪.'
    assert _periods(source, '3,200,000', 'שווי השוק של הנכס הינו 3,200,000 ₪') == set()
    noted = 'ההכנסות להלן הן לשנה\nהכנסות משכירות 480,000 ₪'
    assert _periods(noted, '480,000', 'הכנסות משכירות 480,000 ₪') == {"year"}


# --- identifiers, years and absence sentences are not amounts (round 9 U12, KTD10, R22) -----------------------------

PERMIT = "היתר בנייה מס׳ 638 מיום 01.11.1966, בתוקף לשנה. דמי האגרה 70 ₪ לחודש."


@pytest.mark.parametrize("answer", [
    "היתר הבנייה הוא מס׳ 638, מיום 01.11.1966 [S1].",
    "היתר הבנייה הוא מס׳ 638, מיום 01.11.1966. [S1]",
    "היתר מספר 638 מיום 01.11.1966 [S1].",
    "ההיתר הוא No 638 [S1].",
    "ההיתר הוא #638 [S1].",
])
def test_a_permit_number_and_the_year_of_its_date_are_not_amounts(answer):
    # the source puts "לשנה" near them: neither the number nor the year is an amount that needs (or lacks) a period
    assert _problems(answer, _ws(PERMIT)) == []


def test_a_year_alone_is_not_an_amount_but_a_year_with_a_currency_is():
    ws = _ws("השומה נכונה לשנת 2019, לחודש. דמי השכירות 2019 ₪ לחודש.")
    assert _problems("השומה נכונה לשנת 2019 [S1].", ws) == []
    (p,) = _problems("דמי השכירות הם 2019 ₪ [S1].", ws)
    assert p.kind == "period" and p.annotation == "לחודש"


def test_the_amount_next_to_an_identifier_is_still_checked():
    (p,) = _problems("דמי האגרה לפי היתר מס׳ 638 הם 70 ₪ [S1].", _ws(PERMIT))
    assert p.kind == "period" and p.annotation == "לחודש" and p.number == "70"


@pytest.mark.parametrize("answer", [
    "לא צוין אם הסכום של 70 ₪ כולל מע\"מ [S1].",
    "לא צוינה תקופה לסכום של 70 ₪ [S1].",
    "הסכום 70 ₪ אינו מופיע במסמך [S1].",
])
def test_an_absence_sentence_gets_no_number_meaning_check(answer):
    assert _problems(answer, _ws(PERMIT)) == []


def test_only_the_absence_clause_is_exempt():
    (p,) = _problems("דמי האגרה 70 ₪; לא צוין אם יש היטל נוסף [S1].", _ws(PERMIT))
    assert p.kind == "period" and p.annotation == "לחודש"


# --- per-attribute provenance (round 12 U9, R6, R8, KTD9, AE5) --------------------------------------------------
#
# A registered value's period and area basis back a claim only when its source states them: the record of a field
# the model asserted is no evidence, and only the words of the value's own quote can attest it. A stored measurement's
# period or basis counts only when its own words are in the measurement's quote (or caption, header). Synthetic text.

from decimal import Decimal  # noqa: E402

from app.chat import calc  # noqa: E402
from app.chat import tools as T  # noqa: E402

RENT_QUOTE = "שכר דירה 5,000 ₪"
RENT_QUOTE_MONTHLY = "שכר דירה 5,000 ₪ לחודש"
SOURCE_ALL = {"unit": "source", "vat": "source", "area_basis": "source", "period": "source"}


def _registered(written: str, quote: str, *, period: str = "none", area_basis: str = "", unit: str = "ILS",
                provenance: dict | None = None, kind: str = "rent") -> Workspace:
    """A workspace whose V1 is a registered value of an invented property: ``period`` and ``area_basis`` as its record
    holds them, each with the ``provenance`` entry given (default: all from the source)."""
    ws = Workspace(ctx=None)
    ws.values["V1"] = calc.Value("V1", Decimal(written.replace(",", "")), written, "S1", uuid.uuid4(), uuid.uuid4(),
                                 None, "שומת הדגמה", "סעיף 3", "דמי שכירות", kind, unit, period, "unknown",
                                 area_basis, "נכס הדגמה", "other", SOURCE_ALL | (provenance or {}),
                                 {"quote": quote}, quote)
    return ws


def test_a_model_asserted_period_is_no_evidence_for_the_same_period_in_the_answer():
    """AE5: V1 registered from a quote that states no period, the model having asserted "month": the number is the
    source's, the period is not."""
    ws = _registered("5,000", RENT_QUOTE, period="month", provenance={"period": "model_asserted"})
    (p,) = _problems("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws)
    assert p.blocking and p.kind == "period"
    assert _problems("שכר הדירה הוא 5,000 ₪ [V1].", ws) == []  # nothing the source lacks is asked for either


def test_a_model_asserted_period_is_attested_when_the_values_own_quote_states_it():
    ws = _registered("5,000", RENT_QUOTE_MONTHLY, period="month", provenance={"period": "model_asserted"})
    assert _problems("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws) == []
    (p,) = _problems("שכר הדירה הוא 5,000 ₪ לשנה [V1].", ws)  # the quote's own word still decides
    assert p.blocking and p.kind == "period"


def test_a_source_given_period_of_the_record_is_attested():
    ws = _registered("5,000", RENT_QUOTE, period="month")
    assert _problems("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws) == []


def test_a_model_asserted_basis_is_no_evidence_and_is_not_demanded():
    quote = "שטח הדירה 96 מ\"ר"
    ws = _registered("96", quote, unit="sqm", area_basis="פלדלת", provenance={"area_basis": "model_asserted"},
                     kind="area")
    (p,) = _problems("שטח הדירה הוא 96 מ\"ר פלדלת [V1].", ws)
    assert p.blocking and p.kind == "basis"
    assert _problems("שטח הדירה הוא 96 מ\"ר [V1].", ws) == []
    stated = _registered("96", "שטח הדירה 96 מ\"ר פלדלת", unit="sqm", area_basis="פלדלת",
                         provenance={"area_basis": "model_asserted"}, kind="area")
    assert _problems("שטח הדירה הוא 96 מ\"ר פלדלת [V1].", stated) == []


# --- a stored measurement's period and basis need their own words in what it quotes -----------------------------

def _measured(quote: str, **fields) -> Workspace:
    ws = _ws("טבלה")
    row = SimpleNamespace(value_text="5,000", quote=quote, vat="unknown", metric="שכר דירה", metric_kind="rent",
                          unit="ILS", period="none", area_basis=None, subject="הדולב 4",
                          value_role="appraiser_determination", value_form="exact")
    for k, v in fields.items():
        setattr(row, k, v)
    ws.measurements["M1"] = Measurement("M1", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "שומה", row)
    return ws


def test_a_stored_period_that_neither_the_quote_nor_the_header_states_is_not_attested():
    ws = _measured(RENT_QUOTE, period="year")
    (p,) = _problems("שכר הדירה הוא 5,000 ₪ לשנה [M1].", ws)
    assert p.blocking and p.kind == "period"
    assert _problems("שכר הדירה הוא 5,000 ₪ [M1].", ws) == []  # and no period is demanded of the answer


def test_a_stored_period_its_quote_states_is_attested_and_a_header_in_the_quote_counts():
    assert _problems("שכר הדירה הוא 5,000 ₪ לשנה [M1].", _measured("שכר דירה 5,000 ₪ לשנה", period="year")) == []
    by_header = _measured("שכר דירה שנתי: 5,000", period="year")
    assert _problems("שכר הדירה הוא 5,000 ₪ לשנה [M1].", by_header) == []


def test_a_stored_basis_the_quote_does_not_write_is_not_attested():
    ws = _measured("שווי למ\"ר 8,350", value_text="8,350", metric_kind="value_per_area", unit="ILS_per_sqm",
                   area_basis="ברוטו")
    assert _problems("השווי למ\"ר הוא 8,350 ₪ [M1].", ws) == []
    (p,) = _problems("השווי למ\"ר ברוטו הוא 8,350 ₪ [M1].", ws)
    assert p.blocking and p.kind == "basis"


def test_measurement_states_follows_the_extraction_checks_for_each_field():
    row = SimpleNamespace(quote="דמי ניהול 18 ₪ למ\"ר לחודש, ללא מע\"מ", period="month", vat="excluded",
                          area_basis="")
    assert meaning.measurement_states(row, "period") and meaning.measurement_states(row, "vat")
    assert not meaning.measurement_states(SimpleNamespace(**{**vars(row), "period": "year"}), "period")
    assert not meaning.measurement_states(SimpleNamespace(**{**vars(row), "vat": "included"}), "vat")
    assert not meaning.measurement_states(SimpleNamespace(**{**vars(row), "period": "unknown"}), "period")
    assert not meaning.measurement_states(SimpleNamespace(**{**vars(row), "quote": "דמי ניהול 18 ₪ למ\"ר"}),
                                          "period")
    # the caption or header of the table, when the row carries them, states it too
    assert meaning.measurement_states(SimpleNamespace(quote="הדולב 4 | 71", period="month", header="שכ\"ד לחודש"),
                                      "period")


# --- the anchor of each source attribute, recorded when the value is taken ----------------------------------------

DOC, VER = uuid.UUID(int=11), uuid.UUID(int=12)


def _source(text: str, kind: str = "context") -> T.Source:
    return T.Source(sid="S1", document_id=DOC, version_id=VER, title="שומה סינתטית", section="5. תחשיב",
                    location="עמוד 3", kind=kind, text=text, block_start=0, block_end=0, reading_id="r1",
                    status="complete")


def _given(**kw) -> dict:
    return {"kind": "rent", "unit": "ILS", "period": "unknown", "vat": "unknown", "area_basis": "",
            "subject": "הנכס", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def _register(ws: Workspace, taken: dict, given: dict, src: T.Source) -> calc.Value:
    settled = T._settle(taken, given, ())
    return T._new_value(ws, src, "S1", taken, settled, given, "תווית", (), "r1")


def test_a_period_vat_and_kind_in_the_quoted_sentence_are_anchored_to_the_sentence():
    text = "דמי הניהול בבניין הם 18 ₪ למ\"ר לחודש, ללא מע\"מ."
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": "18"}, [(0, text, 1)])
    v = _register(Workspace(ctx=None), taken, _given(kind="management_fee", unit="ILS_per_sqm"), src)
    assert (v.period, v.vat) == ("month", "excluded")
    assert v.meaning_from["period"] == {"part": "sentence", "written": "לחודש"}
    assert v.meaning_from["vat"]["part"] == "sentence" and "מע" in v.meaning_from["vat"]["written"]
    assert v.meaning_from["kind"] == {"part": "sentence", "written": "ניהול"}
    assert v.meaning_from["unit"] == "quote"  # the unit keeps its earlier shape


def test_what_the_model_asserted_has_no_anchor():
    text = "דמי הניהול בבניין הם 18 ₪."
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": "18"}, [(0, text, 1)])
    v = _register(Workspace(ctx=None), taken, _given(kind="management_fee", period="month", vat="included",
                                                     area_basis="פלדלת"), src)
    assert {k: v.provenance[k] for k in ("period", "vat", "area_basis")} == {
        "period": "model_asserted", "vat": "model_asserted", "area_basis": "model_asserted"}
    assert not {"period", "vat", "area_basis"} & set(v.meaning_from)
    assert v.meaning_from["kind"]["part"] == "sentence"  # the sentence names the kind: that one is the source's


TABLE = {"headers": ["נכס", "שכר דירה חודשי (₪)", "שטח (מ\"ר פלדלת)"], "caption": "שכירות בבניין הדולב (נתונים סינתטיים)",
         "title": [], "notes": ["הסכומים ללא מע\"מ"], "rows": [{"cells": ["הדולב 4", "5,000", "96"], "page": 1}]}


def _cell_value(column: str, **given) -> calc.Value:
    full = T._table_text(TABLE, TABLE["rows"])
    src = _source(full, "table")
    taken = T._cell_of(src, full, {"row": "הדולב 4", "column": column}, TABLE, 0)
    return _register(Workspace(ctx=None), taken, _given(**given), src)


def test_a_period_in_the_column_header_is_source_provenanced_with_the_header_anchor():
    v = _cell_value("שכר דירה חודשי (₪)")
    assert v.period == "month" and v.provenance["period"] == "source"
    assert v.meaning_from["period"] == {"part": "header", "written": "חודשי"}
    assert v.meaning_from["vat"] == {"part": "note", "written": "ללא מע\"מ"}
    ws = Workspace(ctx=None)
    ws.values["V1"] = v
    assert _problems("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws) == []  # attested from the record, not the quote


def test_an_area_basis_in_the_column_header_is_anchored_to_the_header():
    v = _cell_value("שטח (מ\"ר פלדלת)", kind="area", unit="sqm")
    assert v.provenance["area_basis"] == "source" and v.meaning_from["area_basis"]["part"] == "header"
    assert "פלדלת" in v.meaning_from["area_basis"]["written"]


def test_the_qualifiers_a_source_gives_carry_the_part_that_states_them():
    table = "שכירות בבניין הדולב\nנכס | שכ\"ד לחודש (₪ למ\"ר)\nהדולב 4 | 71"
    q = meaning.number_qualifiers(table, frozenset({"71"}), "הדולב 4 | 71")
    assert q.parts["period"]["month"] == "header"
    q = meaning.number_qualifiers("דמי הניהול הם 18 ₪ לחודש.", frozenset({"18"}), "דמי הניהול הם 18 ₪ לחודש.")
    assert q.parts["period"]["month"] == "sentence"


# --- a calculation reads only the periods and bases a source states ---------------------------------------------

def test_the_operand_of_a_value_whose_period_the_model_asserted_has_an_unknown_period():
    stated = _registered("5,000", RENT_QUOTE_MONTHLY, period="month").values["V1"]
    asserted = _registered("5,000", RENT_QUOTE, period="month", provenance={"period": "model_asserted"}).values["V1"]
    assert stated.operand().period == "month" and asserted.operand().period == "unknown"
    plain = _registered("5,000", RENT_QUOTE, period="none", provenance={"period": "not_stated"}).values["V1"]
    assert plain.operand().period is None  # a capital or one-time amount keeps having no period


def test_a_basis_the_model_asserted_is_not_the_operands_basis():
    stated = _registered("96", "שטח הדירה 96 מ\"ר פלדלת", unit="sqm", area_basis="פלדלת", kind="area").values["V1"]
    asserted = _registered("96", "שטח הדירה 96 מ\"ר", unit="sqm", area_basis="פלדלת", kind="area",
                           provenance={"area_basis": "model_asserted"}).values["V1"]
    assert stated.operand().basis == "פלדלת" and asserted.operand().basis == ""


def test_a_sum_of_a_stated_and_an_asserted_month_goes_through_the_unknown_period_assumption():
    stated = _registered("5,000", RENT_QUOTE_MONTHLY, period="month").values["V1"]
    asserted = _registered("3,000", RENT_QUOTE, period="month", provenance={"period": "model_asserted"}).values["V1"]
    asserted.vid = "V2"
    operands = {"V1": stated.operand(), "V2": asserted.operand()}
    with pytest.raises(calc.CalcError, match="תקופה"):  # the existing mixed-period guard asks for a justification
        calc.evaluate(calc.parse("V1 + V2"), operands, None)
    out = calc.evaluate(calc.parse("V1 + V2"), operands, "שני הסכומים לחודש לפי הדוח")
    assert any("תקופה" in c for c in out.conditional)  # and the result is conditional on the period of one
    other = _registered("3,000", RENT_QUOTE_MONTHLY, period="month").values["V1"]
    other.vid = "V2"
    out = calc.evaluate(calc.parse("V1 + V2"), {"V1": stated.operand(), "V2": other.operand()}, None)
    assert not out.conditional and out.period == "month"


# --- an adopted measurement carries its date (round 12 U4, KTD14, R17) ----------------------------------------------------

def _stored(quote: str, **kw) -> SimpleNamespace:
    """A stored measurement row of the subject property as the fetcher reads it (synthetic), with its table's caption and
    column header as the fetcher's query adds them."""
    base = dict(id=uuid.uuid4(), document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת בדיקה", metric="דמי שכירות",
                metric_kind="rent_per_area", value_text="55 ₪", quote=quote, effective_date=None, caption=None,
                header=None, block_index=3, table_index=0, row_index=1, period="month", area_basis="", vat="unknown",
                value_form="exact", section="תחשיב", status="auto_validated", anchor_lost=None, subject_role="appraised_property",
                value_role="appraiser_determination", value=55)
    return SimpleNamespace(**(base | kw))


def test_a_measurement_adopted_from_a_table_whose_caption_states_its_year_is_dated_by_it():
    ws = Workspace(ctx=None)
    row = _stored("דמי שכירות: 55 ₪", caption="שכר דירה לשנת 2023")
    mid = meaning.Fetcher(ws).adopt(row)
    as_of = ws.measurements[mid].as_of
    assert (as_of["date"], as_of["precision"], as_of["provenance"]) == ("2023", "year", "source_table")


def test_a_measurement_adopted_with_a_date_in_its_quote_is_dated_by_the_quote():
    ws = Workspace(ctx=None)
    mid = meaning.Fetcher(ws).adopt(_stored("דמי השכירות הם 55 ₪ למ\"ר לחודש נכון ל-01/03/2023"))
    as_of = ws.measurements[mid].as_of
    assert (as_of["date"], as_of["provenance"]) == ("2023-03-01", "source")


def test_a_measurement_adopted_with_no_date_anywhere_stays_unknown_and_is_shown_as_such():
    from app.chat import timeline

    ws = Workspace(ctx=None)
    mid = meaning.Fetcher(ws).adopt(_stored("דמי שכירות: 55 ₪", caption="שכר דירה"))
    assert ws.measurements[mid].as_of is None
    assert timeline.shown(ws.measurements[mid].as_of) == timeline.MSG_UNKNOWN_DATE


def test_a_measurement_found_again_keeps_the_date_it_was_stamped_with():
    ws = Workspace(ctx=None)
    row = _stored("דמי שכירות: 55 ₪", caption="שכר דירה לשנת 2023")
    fetcher = meaning.Fetcher(ws)
    mid = fetcher.adopt(row)
    assert fetcher.adopt(row) == mid and ws.measurements[mid].as_of["date"] == "2023"


def test_a_period_given_by_the_column_header_is_stated_for_the_measurement():
    """The fetcher's query adds the column header, so a period the header states backs a claim (U9, `measurement_states`)."""
    row = _stored("55 ₪", header="דמי שכירות לחודש")
    assert meaning.measurement_states(row, "period")
    assert not meaning.measurement_states(_stored("55 ₪", header="דמי שכירות"), "period")


def test_the_fetcher_query_reads_each_rows_table_caption_and_column_header_with_the_row():
    """The caption and the header come in the one subject-measurement query (no second read): the fragments the query is
    built from name both columns."""
    assert "AS caption" in meaning.TABLE_TEXT_COLUMNS and "AS header" in meaning.TABLE_TEXT_COLUMNS
    assert "extracted_tables" in meaning.TABLE_TEXT_JOIN


# --- quantity kinds and period reading (round 18 U1, KTD3, KTD4, R8, R10, AE9) ------------------------------------
#
# Synthetic wording only. A period, a duration and a count are told apart in the source and in the answer: a split word
# of the text layer is one word again, a duration or a count never owns or claims a period, a header period reaches only
# the cells of a rate, and each table of a source has its own header.

def _no_period(source: str, number: str, within: str) -> None:
    assert _periods(source, number, within) == set()


@pytest.mark.parametrize("source", [
    "שכר דירה חודש י 5,000 ₪.",          # "<prefix+stem> <final letter>": the monthly word split by the text layer
    "שכר הדירה ה חודש י 5,000 ₪.",
    "שכר דירה חודשי 5,000 ₪.",
    "דמי שכירות חודשיים 5,000 ₪.",       # the unprefixed plural adjective after a plural noun
])
def test_a_monthly_word_split_by_the_text_layer_still_attests_the_period(source):
    assert _periods(source, "5,000", source) == {"month"}
    assert _problems("שכר הדירה הוא 5,000 ₪ לחודש [S1].", _ws(source)) == []
    (p,) = _problems("שכר הדירה הוא 5,000 ₪ [S1].", _ws(source))
    assert p.kind == "period" and not p.blocking and p.annotation == "לחודש"


def test_a_split_prefix_letter_and_a_split_year_word_are_joined_too():
    assert _periods("דמי ניהול 800 ₪ ל חודש.", "800", "דמי ניהול 800 ₪ ל חודש") == {"month"}
    assert _periods("הכנסות שנת י 120,000 ₪.", "120,000", "הכנסות שנת י 120,000 ₪") == {"year"}


def test_a_word_is_joined_only_when_the_joined_token_is_a_time_word():
    # "מ" + "ר" ... none of these joins make a period or a duration word, so the text is read as written
    for text in ("שכר דירה שכירות י 5,000 ₪.", "ביצוע ו 5,000 ₪ בלבד.", "תקופה ב 5,000 ₪ סה\"כ."):
        assert meaning._read(text) == meaning._norm(text)
    assert meaning._read("36 חודשי ם") == "36 חודשים "
    assert len(meaning._read("שכר חודש י 5")) == len("שכר חודש י 5")  # positions stay valid


@pytest.mark.parametrize("source, answer", [
    ("תקופת השכירות 36 חודשים, ודמי השכירות 4,700 ₪ לחודש.", "תקופת השכירות היא 36 חודשים [S1]."),
    ("תקופת השכירות 36 חודשי ם, ודמי השכירות 4,700 ₪ לחודש.", "תקופת השכירות היא 36 חודשים [S1]."),
    ("תקופת השכירות 36 חודשי שכירות, ודמי השכירות 4,700 ₪ לחודש.", "תקופת השכירות היא 36 חודשי שכירות [S1]."),
    ("תקופת הגרייס 2 חודשיים, ודמי השכירות 4,700 ₪ לחודש.", "תקופת הגרייס היא 2 חודשיים [S1]."),
])
def test_the_number_before_a_months_noun_is_a_duration_with_no_period_attested_or_claimed(source, answer):
    assert _problems(answer, _ws(source)) == []


def test_a_months_count_takes_no_period_from_the_line_above_or_from_its_sentence():
    source = "דמי השכירות במתחם הם לחודש.\nתקופת השכירות 36 חודשים."
    _no_period(source, "36", "תקופת השכירות 36 חודשים")
    assert _problems("תקופת השכירות היא 36 חודשים [S1].", _ws(source)) == []


def test_a_period_word_after_a_duration_belongs_to_the_amount_not_to_the_duration():
    source = "תקופת השכירות 36 חודשים.\nדמי השכירות 5,000 ₪ לחודש."
    # the answer writes the period before an amount that has no currency sign: the 36 is not its owner
    assert _problems("תקופת השכירות 36 חודשים, ודמי השכירות החודשיים 5,000 [S1].", _ws(source)) == []


def test_a_dual_months_word_after_a_rent_noun_is_a_monthly_rate_and_after_a_number_a_duration():
    assert _periods("דמי שכירות חודשיים 5,000 ₪.", "5,000", "דמי שכירות חודשיים 5,000 ₪") == {"month"}
    assert _periods("תקופת פטור 2 חודשיים, שכר דירה 5,000 ₪.", "5,000", "שכר דירה 5,000 ₪") == set()
    assert meaning._all("תקופת פטור של חודשיים").keys("period") == set()
    assert meaning._all("תשלומים שנתיים וגם 3 שנתיים").keys("period") == {"year"}


@pytest.mark.parametrize("source", [
    "החוזה נחתם לשנה אחת, ודמי השכירות 4,700 ₪.",
    "החוזה נחתם לשנה אחת.\nדמי השכירות 4,700 ₪.",
])
def test_for_one_year_in_a_lease_sentence_is_a_duration_not_a_yearly_period(source):
    assert _problems("דמי השכירות הם 4,700 ₪ [S1].", _ws(source)) == []
    assert _problems("דמי השכירות הם 4,700 ₪ לשנה [S1].", _ws(source))[0].blocking


@pytest.mark.parametrize("text", [
    "דמי השכירות לחודש ינואר היו 5,000 ₪.",
    "התשלום בחודש אפריל היה 5,000 ₪.",
    "הסכום 5,000 ₪ אינו מוצג כסכום חודשי.",
    "הסכום 5,000 ₪ לא נקבע לשנה.",
])
def test_a_month_name_after_the_month_of_and_a_negated_period_are_no_period(text):
    assert meaning._all(text).keys("period") == set()
    assert _problems("הסכום הוא 5,000 ₪ [S1].", _ws(text)) == []


def test_a_period_in_a_comparison_is_still_a_period():
    assert meaning._all("לא פחות מ-5,000 ₪ לחודש").keys("period") == {"month"}
    assert meaning._all("הסכום 5,000 ₪, לא כולל מע\"מ, לחודש").keys("period") == {"month"}


def test_an_answer_that_negates_a_period_claims_none():
    assert _problems("הסכום הוא 5,000 ₪ ואינו חודשי [S1].", _ws("הסכום הוא 5,000 ₪.")) == []


def test_a_period_the_source_contradicts_is_still_blocking():
    (p,) = _problems("דמי השכירות הם 760 ₪ למ\"ר לחודש [S1].", _ws("דמי השכירות לנכס נקבעו ל-760 ₪ למ\"ר לשנה."))
    assert p.blocking and p.kind == "period"
    (p,) = _problems("דמי השכירות הם 760 ₪ למ\"ר לשנה [S1].", _ws("דמי שכירות חודשיים 760 ₪ למ\"ר."))
    assert p.blocking and p.kind == "period"


# column headers ----------------------------------------------------------------------------------------------------

TWO_ROW_HEADER = ("| | לחודש | לחודש | לחודש\nנכס | שכ\"ד (₪ למ\"ר) | תפוסה (%) | מספר חוזים\n"
                  "הדולב 4 | 71 | 92 | 3\nהדולב 6 | 64 | 88 | 5")


def test_a_period_in_the_upper_header_row_reaches_the_amounts_and_not_a_percent_or_a_count_column():
    assert _periods(TWO_ROW_HEADER, "71", "הדולב 4 | 71 | 92 | 3") == {"month"}
    _no_period(TWO_ROW_HEADER, "92", "הדולב 4 | 71 | 92 | 3")
    _no_period(TWO_ROW_HEADER, "3", "הדולב 4 | 71 | 92 | 3")
    ws = _ws(TWO_ROW_HEADER, kind="table")
    (p,) = _problems("בדולב 6 דמי השכירות הם 64 ₪ למ\"ר [S1].", ws)
    assert p.kind == "period" and p.annotation == "לחודש"
    assert _problems("בדולב 6 שיעור התפוסה הוא 88% [S1].", ws) == []


def test_a_spanned_header_cell_reaches_the_empty_header_cells_after_it():
    table = ("| | לחודש | | מדדים\nנכס | שכ\"ד (₪ למ\"ר) | דמי ניהול (₪) | מספר חוזים\n"
             "הדולב 4 | 71 | 12 | 3")
    assert _periods(table, "71", "הדולב 4 | 71 | 12 | 3") == {"month"}
    assert _periods(table, "12", "הדולב 4 | 71 | 12 | 3") == {"month"}
    _no_period(table, "3", "הדולב 4 | 71 | 12 | 3")


def test_a_source_holding_two_tables_gives_each_cell_its_own_headers_period():
    source = ("שכירות בבניין הדולב\nנכס | שכ\"ד לחודש (₪ למ\"ר)\nהדולב 4 | 71\nהדולב 6 | 64\n"
              "הכנסות בבניין האורן\nנכס | הכנסה שנתית (₪)\nהאורן 3 | 480,000")
    assert _periods(source, "71", "הדולב 4 | 71") == {"month"}
    assert _periods(source, "480,000", "האורן 3 | 480,000") == {"year"}


def test_a_header_row_joined_with_empty_edge_cells_keeps_them_so_the_period_reaches_the_first_amount_column():
    # the rendered table text joins cells with " | ": an empty first or last cell leaves " | x | " (no outer pipes)
    table = ("טבלה 4.1 — פירוט\n | הכנסה שנתית (₪) | \nרכיב | משרדים | מסחר\n"
             "דמי שכירות | 1,240,000 | 860,000\nדמי ניהול | 96,000 | 64,000")
    assert meaning._row_cells(" | הכנסה שנתית (₪) | ") == ["", "הכנסה שנתית (₪)", ""]
    assert meaning._row_cells("| a | b |") == ["a", "b"]
    assert _periods(table, "1,240,000", "דמי שכירות | 1,240,000 | 860,000") == {"year"}
    assert _periods(table, "860,000", "דמי שכירות | 1,240,000 | 860,000") == {"year"}
    assert _periods(table, "96,000", "דמי ניהול | 96,000 | 64,000") == {"year"}


def test_a_header_row_with_one_cell_fewer_than_its_data_rows_aligns_by_its_non_empty_cells():
    table = "שכ\"ד לחודש (₪ למ\"ר) | שטח (מ\"ר)\nהדולב 4 | 71 | 100\nהדולב 6 | 64 | 90"
    assert _periods(table, "71", "הדולב 4 | 71 | 100") == {"month"}
    _no_period(table, "100", "הדולב 4 | 71 | 100")


def test_leading_digit_free_rows_form_the_header_and_a_later_digit_free_row_does_not():
    table = "נכס | שכ\"ד חודשי (₪)\nהדולב 4 | 7,000\nסה\"כ שטחים | \nהדולב 6 | 6,000"
    assert _periods(table, "6,000", "הדולב 6 | 6,000") == {"month"}
    undated = "הדולב 4 | 7,000\nהערות בלבד | חודשי\nהדולב 6 | 6,000"
    _no_period(undated, "6,000", "הדולב 6 | 6,000")


def test_a_months_count_column_takes_no_header_period():
    table = "| | לחודש | לחודש\nנכס | תקופת שכירות (חודשים) | שכ\"ד (₪)\nהדולב 4 | 36 | 7,000"
    _no_period(table, "36", "הדולב 4 | 36 | 7,000")
    assert _periods(table, "7,000", "הדולב 4 | 36 | 7,000") == {"month"}
    ws = _ws(table, kind="table")
    assert _problems("תקופת השכירות בדולב 4 היא 36 חודשים [S1].", ws) == []


def test_a_rates_own_header_period_still_reaches_a_percent_column():
    table = "נכס | תשואה שנתית (%)\nהדולב 4 | 6.5"
    assert _periods(table, "6.5", "הדולב 4 | 6.5") == {"year"}


# the area basis -------------------------------------------------------------------------------------------------------

def test_a_basis_adjective_needs_its_area_word_next_to_it():
    assert meaning._all("הסכום הוא 5,000 ₪ ברוטו").keys("basis") == set()
    assert meaning._all("הרכיב העיקרי בתחשיב הוא 5,000 ₪").keys("basis") == set()
    assert meaning._all("שטח המבנה 412 מ\"ר ברוטו").keys("basis") == {"ברוטו"}
    assert meaning._all("שטח ברוטו 412").keys("basis") == {"ברוטו"}
    assert meaning._all("שטח עיקרי 96").keys("basis") == {"עיקרי"}
    assert meaning._all("96 מ\"ר בנוי ברוטו").keys("basis") == {"בנוי", "ברוטו"}


def test_a_basis_word_in_a_sentence_with_no_number_gives_no_basis_to_other_numbers():
    ws = _ws("הערכים בדוח מוצגים במ\"ר ברוטו.\nשטח הנכס 120 מ\"ר.")
    (p,) = _problems("שטח הנכס הוא 120 מ\"ר ברוטו [S1].", ws)
    assert p.blocking and p.kind == "basis"


def test_a_table_captions_basis_is_still_a_general_basis_of_its_numbers():
    ws = _ws("כל השטחים בטבלה הם מ\"ר ברוטו.\nנכס | שטח\nהדולב 4 | 120", kind="table")
    assert [p for p in _problems("שטח הדולב 4 הוא 120 מ\"ר ברוטו [S1].", ws) if p.blocking] == []


# trailing punctuation -------------------------------------------------------------------------------------------------

def test_a_year_written_with_a_trailing_comma_is_a_year_not_an_amount():
    source = "שכר הדירה לחודש בשנת 2021, עמד על 5,000."
    assert _problems("בשנת 2021, שכר הדירה היה 5,000 [S1].", _ws(source)) == []
    assert meaning.written_at("בשנת 2021, שכר הדירה", len("בשנת ")) == "2021"
    assert meaning.written_at("שכר 5,000, עד", len("שכר ")) == "5,000"


# the problem names its own occurrence -------------------------------------------------------------------------------------

def test_a_problem_names_the_occurrence_it_is_about():
    answer = "בהיתר מס' 120 נקבע שטח של 120 מ\"ר [S1]."
    (p,) = _problems(answer, _ws("שטח 120 מ\"ר פלדלת."))
    assert p.kind == "basis" and p.number == "120"
    assert answer.index("120 מ\"ר") == p.at


# --- review fixes: a negation governs only the period word near it; a large number is no duration; windowed helpers ---------------

def test_a_negation_far_before_a_period_word_does_not_blank_it_but_one_next_to_it_still_does():
    text = "הנכס לא הושכר בפועל ולכן הוערך לפי 5,000 ₪ לחודש"
    assert meaning._all(text).keys("period") == {"month"}
    ((_, _, q),), _ = meaning.attached(text)
    assert q.found == {"period": {"month": "לחודש"}}
    assert meaning._all("הנכס לא הושכר בפועל ולכן הוערך לפי 5,000 ₪ בשנה").keys("period") == {"year"}
    # the negation governs the period word right after it, in any written form
    assert meaning._all("הסכום 5,000 ₪ אינו מוצג כסכום חודשי").keys("period") == set()
    assert meaning._all("הסכום 5,000 ₪ לא נקבע לשנה").keys("period") == set()
    assert meaning._all("לא פחות מ-5,000 ₪ לחודש").keys("period") == {"month"}


def test_a_negation_closed_by_a_comma_or_a_conjunction_clause_does_not_reach_a_later_period_word():
    assert meaning._all("הנכס לא הושכר, 5,000 ₪ לחודש").keys("period") == {"month"}
    assert meaning._all("הנכס לא הושכר אלא הוערך ב-5,000 ₪ לחודש").keys("period") == {"month"}


@pytest.mark.parametrize("text", [
    "דמי שכירות 5,000 חודשי",
    "דמי שכירות 5,000.50 חודשיים",
    "שכר דירה 1500 שנתי",
])
def test_a_large_number_before_a_period_adjective_is_an_amount_with_that_period_not_a_duration(text):
    ((_, _, q),), _ = meaning.attached(text)
    assert q.found.get("period"), text


@pytest.mark.parametrize("text, kind", [
    ("תקופת השכירות 36 חודשים", "duration"),
    ("תקופת השכירות 36 חודשי שכירות", "duration"),
    ("תקופת הגרייס 12 חודשים", "duration"),
    ("תקופה של 2.5 שנים", "duration"),
    ("תקופת השכירות 5,000 חודשים", ""),   # not a count of months
    ("דמי שכירות 5,000 חודשי", ""),
])
def test_after_kind_reads_a_duration_only_after_a_small_number(text, kind):
    end = len(re.match(r".*?\d[\d,.]*", text).group(0))
    assert meaning.after_kind(text, end) == kind


@pytest.mark.parametrize("filler", list(range(0, 40, 3)) + list(range(110, 140, 3)) + list(range(250, 270, 3)) + list(range(500, 530, 4)))
def test_the_windowed_helpers_give_the_result_of_a_scan_of_the_whole_text_at_the_edges_of_their_windows(filler):
    pad = "ב" * filler
    cases = [
        # (text before the word, the helper, the whole-text answer)
        (f"{pad}, לא נקבע ", meaning._negated, lambda t: bool(
            meaning._NEGATION.search(t, _negation_start(t), len(t)))),
        (f"לא {pad} נקבע ", meaning._negated, lambda t: bool(meaning._NEGATION.search(t, _negation_start(t), len(t)))),
        (f"{pad} 5,000 ₪ סכום 12 ", meaning._number_before, _number_written_before),
        (f"{pad} {'1' * filler}", meaning._number_before, _number_written_before),
        (f"{pad} 5,000-", meaning._number_before, _number_written_before),
        (f"{pad}תקופה של ", meaning._after_duration_preposition, lambda t: bool(meaning._DURATION_PREPOSITION.search(t))),
        (f"{pad} {' ' * filler}של ", meaning._after_duration_preposition, lambda t: bool(meaning._DURATION_PREPOSITION.search(t))),
        (f"{pad}כ", meaning._after_duration_preposition, lambda t: bool(meaning._DURATION_PREPOSITION.search(t))),
    ]
    for head, helper, whole in cases:
        text = head + "חודשי"
        assert helper(text, len(head)) == whole(head), (helper.__name__, filler, head[-30:])


def _number_written_before(head: str) -> str:
    m = meaning._NUMBER_BEFORE.search(head)
    return m.group(1) if m else ""


def _negation_start(head: str) -> int:
    """Where the negation of ``head`` may start, read on the whole text: the first of the last ``NEGATION_TOKENS`` blank-separated
    words, and not before the last mark that closes a part of the clause."""
    words = list(re.finditer(r"\S+", head))[-meaning.NEGATION_TOKENS:]
    start = words[0].start() if words else len(head)
    closings = list(meaning._NEGATION_END.finditer(head, start, len(head)))
    return closings[-1].end() if closings else start


# --- numbers keep their header, row, unit, scale, period and sign (round 19 U1, KTD3, R9-R12, AE5, AE6) ----------------------------
#
# Synthetic wording only. A Hebrew abbreviation ends with a quote mark and a letter ("סה"כ", "מע"מ", "בע"מ", "דמ"ש"): that letter is
# never the split piece of the next word, so "סה"כ לשנה" stays an annual header; an abbreviated construct plural ("דמ"ש") is a
# plural noun before the plural monthly adjective; a decimal comma is read by context, a sign is part of the number.

@pytest.mark.parametrize("text", [
    'סה"כ לשנה', 'סה״כ לשנה', 'מע"מ לחודש', 'דמי שכירות בע"מ לשנה', 'דמ"ש לחודש', 'דמ״ש חודשיים', "סה'כ לחודש",
])
def test_the_last_letter_of_an_abbreviation_is_never_joined_to_the_next_word(text):
    assert meaning._read(text) == meaning._norm(text)
    assert len(meaning._read(text)) == len(text)


def test_an_annual_total_header_with_an_abbreviation_keeps_the_yearly_period():
    table = 'נכס | סה"כ לשנה (₪)\nהדולב 4 | 120,000\nהדולב 6 | 96,000'
    assert _periods(table, "120,000", "הדולב 4 | 120,000") == {"year"}
    assert meaning._all('סה"כ לשנה').keys("period") == {"year"}
    assert meaning._all('מע"מ לחודש').keys("period") == {"month"}


def test_a_table_of_two_columns_with_different_periods_keeps_each_columns_period():
    table = 'נכס | שכ"ד לחודש (₪) | סה"כ לשנה (₪)\nהדולב 4 | 10,000 | 120,000\nהדולב 6 | 8,000 | 96,000'
    row = "הדולב 4 | 10,000 | 120,000"
    assert _periods(table, "10,000", row) == {"month"}
    assert _periods(table, "120,000", row) == {"year"}
    (p,) = _problems("הדולב 4: סה\"כ ההכנסה הוא 120,000 ₪ [S1].", _ws(table, kind="table"))
    assert p.kind == "period" and p.annotation == "לשנה"
    assert _problems("הדולב 4: סה\"כ ההכנסה לשנה הוא 120,000 ₪ [S1].", _ws(table, kind="table")) == []


def test_a_duration_column_next_to_an_annual_column_has_no_period():
    table = 'נכס | תקופה (חודשים) | סה"כ לשנה (₪)\nהדולב 4 | 36 | 120,000'
    row = "הדולב 4 | 36 | 120,000"
    _no_period(table, "36", row)
    assert _periods(table, "120,000", row) == {"year"}


@pytest.mark.parametrize("text", ['דמ"ש חודשיים 5,000 ₪', "דמ״ש חודשיים 5,000 ₪", 'הדמ"נ חודשיים 800 ₪'])
def test_an_abbreviated_construct_plural_before_the_plural_monthly_adjective_is_a_monthly_period(text):
    assert meaning._all(text).keys("period") == {"month"}


@pytest.mark.parametrize("text", [
    "תקופה של חודשיים", "שכירות לתקופה של חודשיים", "חודשיים לאחר מכן", 'סה"כ חודשיים', 'בע"מ חודשיים',
])
def test_a_period_of_two_months_is_no_period(text):
    assert meaning._all(text).keys("period") == set()


def test_ae6_a_decimal_comma_cell_reads_as_its_decimal_and_a_thousands_number_as_its_thousands():
    source = "שטח | שווי\n13,2 | 3,596,696"
    assert meaning.source_occurrences(source, {"13.2"})[0]
    assert not meaning.source_occurrences(source, {"13"})[0] and not meaning.source_occurrences(source, {"132"})[0]
    assert meaning.source_occurrences(source, {"3596696"})[0]


def test_a_list_is_no_decimal_and_an_ambiguous_pair_keeps_both_readings_in_a_source():
    assert not meaning.source_occurrences("קומות 6,7,8 בבניין", {"6.7"})[0]
    assert meaning.source_occurrences("קומות 6,7,8 בבניין", {"7"})[0]
    for form in ("3.4", "3", "4"):
        assert meaning.source_occurrences("בקומות 3,4 יש חדרים", {form})[0]


def test_a_decimal_comma_in_the_answer_meets_the_same_number_in_the_source():
    source = "דמי הניהול 13,2 ₪ למ\"ר לחודש."
    assert _problems("דמי הניהול הם 13,2 ₪ למ\"ר לחודש [S1].", _ws(source)) == []
    (p,) = _problems("דמי הניהול הם 13,2 ₪ למ\"ר [S1].", _ws(source))
    assert p.kind == "period" and p.annotation == "לחודש"


def test_a_negative_source_number_has_its_signed_forms():
    for source in ("השינוי (3.5)% בשנה.", "השינוי -3.5% בשנה.", "השינוי 3.5- בשנה."):
        assert meaning.source_occurrences(source, {"-3.5"})[0], source
        assert meaning.source_occurrences(source, {"3.5"})[0], source
    assert not meaning.source_occurrences("השינוי 3.5% בשנה.", {"-3.5"})[0]


def test_a_plot_or_unit_identifier_carries_no_area_basis_or_header_qualifiers():
    for source in ("מ\"ר אקוו' בתא שטח 101", "מ\"ר אקוו' בגוש 6004", "מ\"ר אקוו' בתת חלקה 12", "מ\"ר אקוו' בנספח 3"):
        (occ,), _ = meaning.source_occurrences(source, set(meaning.numbers_in(source[-4:].strip())))
        assert occ.identifier and not occ.qualifiers.found, source
    table = "תא שטח | שטח (מ\"ר אקוו') | מקדם\nתא שטח 101 | 250 | 0.8"
    ((ident,), _) = meaning.source_occurrences(table, {"101"})
    assert ident.identifier and not ident.qualifiers.keys("basis")
    (area,), _ = meaning.source_occurrences(table, {"250"})
    assert not area.identifier and area.qualifiers.keys("basis") == {"אקוו"}


def test_a_number_the_evidence_states_only_as_an_identifier_is_not_checked_for_a_basis():
    ws = _ws("מ\"ר אקוו' בתא שטח 101")
    assert _problems("הערך הוא 101 [S1].", ws) == []


def test_a_plot_label_before_a_quantity_is_still_a_quantity():
    source = "שטח מגרש 450 מ\"ר אקוו' ושווי מגרש 1,250,000 ₪."
    (occ,), _ = meaning.source_occurrences(source, {"450"})
    assert not occ.identifier and occ.qualifiers.keys("basis") == {"אקוו"}
    (occ,), _ = meaning.source_occurrences(source, {"1250000"})
    assert not occ.identifier
