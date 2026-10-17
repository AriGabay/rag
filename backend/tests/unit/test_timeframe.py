"""The question's timeframe and the turn's time mode (round 11 U7: R3, R11, R16; KTD5).

``timeframe.resolve`` reads the explicit time a question gives (a point, a range, an open range, a relative period or a
comparison of two points) and decides the turn's mode: ``explicit`` when the question gives time, otherwise ``recent``
or ``none`` from the recorded switch. What is proven here is the parse and the mode; the engine's use of it (the time
block, the diagnostics, the unchanged intent) is in ``tests/integration/test_chat_time_mode.py``. All texts are
synthetic."""

from __future__ import annotations

from datetime import date

import pytest

from app.chat import timeframe as TF

ANCHOR = "2026-10-14"


def pref(on: bool, anchor: str = ANCHOR) -> dict:
    return {"version": 1, "prefer_recent": on, "anchor_date": anchor}


def req(question: str, on: bool = True, anchor: str = ANCHOR) -> TF.Requested:
    t = TF.resolve(question, pref(on, anchor))
    assert t.mode == "explicit" and t.source == "question", t
    assert t.requested is not None
    return t.requested


def shape(r: TF.Requested) -> tuple:
    return r.kind, r.start, r.end, r.precision


# --- the kinds KTD5 lists --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("on", [True, False])
def test_a_year_in_the_question_is_a_point_with_the_switch_on_or_off(on):
    r = req("מה היה השווי ב-2022?", on)
    assert shape(r) == ("point", "2022", "2022", "year")
    assert "2022" in r.text


@pytest.mark.parametrize("question, expected", [
    ("מה השווי נכון ל-15/04/2024?", ("point", "2024-04-15", "2024-04-15", "day")),
    ("מה השווי ביוני 2023?", ("point", "2023-06", "2023-06", "month")),
    ("השומה מיום 15/04/2024 - מה נקבע בה?", ("point", "2024-04-15", "2024-04-15", "day")),
])
def test_a_date_is_a_point_at_the_precision_the_question_gives(question, expected):
    assert shape(req(question)) == expected


@pytest.mark.parametrize("question, expected", [
    ("מה היו המחירים בין 2020 ל-2023?", ("range", "2020", "2023", "year")),
    ("מה היו המחירים מ-2020 עד 2023?", ("range", "2020", "2023", "year")),
    ("כמה עלו הדירות בין מרץ 2021 לבין מרץ 2024?", ("range", "2021-03", "2024-03", "month")),
])
def test_a_range_has_both_ends(question, expected):
    assert shape(req(question)) == expected


@pytest.mark.parametrize("question, expected", [
    ("מה השתנה מאז 2021?", ("open", "2021", None, "year")),
    ("מה השווי מ-2020 והלאה?", ("open", "2020", None, "year")),
    ("מה היה השווי עד 2023?", ("open", None, "2023", "year")),
    # "before" and "after" are exclusive: the open range starts or ends at the neighbouring period, never at the date
    ("מה היה השווי לפני 2023?", ("open", None, "2022", "year")),
    ("מה השתנה אחרי 2021?", ("open", "2022", None, "year")),
    ("מה השווי לפני 15/04/2024?", ("open", None, "2024-04-14", "day")),
    ("מה השווי אחרי מרץ 2024?", ("open", "2024-04", None, "month")),
])
def test_an_open_range_has_one_end(question, expected):
    assert shape(req(question)) == expected


def test_two_points_are_a_comparison_in_date_order():
    r = req("השווה בין השווי ב-2024 לבין השווי ב-2021")
    assert shape(r) == ("compare", "2021", "2024", "year")
    assert shape(req("מה השווי בשנת 2022 ובשנת 2024?")) == ("compare", "2022", "2024", "year")
    assert shape(req("השווה את השווי ב-2021 לשווי ב-03/2024")) == ("compare", "2021", "2024", "year")


def test_the_same_point_twice_is_one_point():
    assert shape(req("מה השווי ב-2022, כלומר בשנת 2022?")) == ("point", "2022", "2022", "year")


# --- relative periods, against the stored anchor (R14) -----------------------------------------------------------------

@pytest.mark.parametrize("question, expected", [
    ("מה היה השווי בשנה שעברה?", ("relative", "2025", "2025", "year")),
    ("מה השווי אשתקד?", ("relative", "2025", "2025", "year")),
    ("מה השווי השנה?", ("relative", "2026", "2026", "year")),
    ("מה השתנה בשנתיים האחרונות?", ("relative", "2024-10-14", "2026-10-14", "day")),
    ("מה השתנה ב-3 השנים האחרונות?", ("relative", "2023-10-14", "2026-10-14", "day")),
    ("מה השתנה בחמש השנים האחרונות?", ("relative", "2021-10-14", "2026-10-14", "day")),
    ("מה השתנה בשנה האחרונה?", ("relative", "2025-10-14", "2026-10-14", "day")),
])
def test_a_relative_period_is_resolved_against_the_anchor(question, expected):
    assert shape(req(question)) == expected


def test_a_retry_a_year_later_gives_the_same_range_because_the_anchor_is_the_stored_one():
    question = "מה היה השווי בשנה שעברה?"
    first = TF.resolve(question, pref(True, "2026-10-14"))
    retry = TF.resolve(question, pref(True, "2026-10-14"))  # the retry reads the message's recorded anchor, not today
    assert shape(first.requested) == shape(retry.requested) == ("relative", "2025", "2025", "year")
    assert first.anchor == date(2026, 10, 14)
    later = TF.resolve(question, pref(True, "2027-10-14"))  # a new message a year later is another matter
    assert shape(later.requested) == ("relative", "2026", "2026", "year")


def test_a_leap_day_anchor_has_a_valid_start():
    r = req("מה השתנה בשנתיים האחרונות?", anchor="2028-02-29")
    assert (r.start, r.end) == ("2026-02-28", "2028-02-29")


# --- numbers that are not time ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "מה השווי בגוש 7001 חלקה 2024?",
    "מה אומר תקן 19?",
    "מה קובע סעיף 2.1?",
    "תקן 19 סעיף 2.1 ו-2.3",
    "האם 2,450,000 ₪ הוא השווי?",
    "האם 80 מ״ר הוא השטח?",
    "מה השווי של הנכס ברחוב הרצל 2024?",
    "מה אומר תקן ישראלי 2024 על שווי?",
    "הנכס בקומה 2022",
    "מה השווי של הנכס שנבנה ב-1985?",
    "מה שווי הנכס שנבנה בשנת 1985 ברחוב הרקפות?",
    "מה שווי הנכס, בנייה משנת 1985?",
    "הנכס נרכש בשנת 2010 - מה שוויו?",
    "מה אומר חוק התכנון והבנייה 1965 על היתר?",
    "מה השווי לפי תקנות התכנון והבנייה 2016?",
    "האם היתר משנת 2008 קיים לנכס?",
    "מה השווי של נכס שנת הקמה 1990?",
])
@pytest.mark.parametrize("on, mode", [(True, "recent"), (False, "none")])
def test_identifiers_prices_and_areas_are_not_time(question, on, mode):
    t = TF.resolve(question, pref(on))
    assert t.mode == mode and t.source == "switch" and t.requested is None, t


@pytest.mark.parametrize("question, expected", [
    ("מה שווי הנכס שנבנה ב-1985 נכון ל-2022?", ("point", "2022", "2022", "year")),
    ("הנכס נרכש בשנת 2010; מה היה השווי ב-2022?", ("point", "2022", "2022", "year")),
    ("נרכש לפני שנים, מה השווי ב-2022?", ("point", "2022", "2022", "year")),
    ("מה השווי בשנת 2022?", ("point", "2022", "2022", "year")),
    ("מה השווי ב-2022 לפי חוק התכנון והבנייה?", ("point", "2022", "2022", "year")),
])
def test_a_year_with_a_valuation_context_still_makes_the_turn_explicit_beside_a_build_year(question, expected):
    assert shape(req(question)) == expected


def test_a_standard_number_does_not_hide_a_real_year_beside_it():
    assert shape(req("מה קבע תקן 19 בשנת 2022?")) == ("point", "2022", "2022", "year")
    assert shape(req("גוש 7001 חלקה 12 נכון ל-15/04/2024")) == ("point", "2024-04-15", "2024-04-15", "day")


# --- the mode -------------------------------------------------------------------------------------------------------

def test_with_no_time_words_the_switch_decides_the_mode():
    on = TF.resolve("מה השווי של הנכס?", pref(True))
    off = TF.resolve("מה השווי של הנכס?", pref(False))
    assert (on.mode, on.source, on.requested, on.prefer_recent) == ("recent", "switch", None, True)
    assert (off.mode, off.source, off.requested, off.prefer_recent) == ("none", "switch", None, False)


def test_explicit_time_overrides_the_switch_either_way():
    for on in (True, False):
        t = TF.resolve("מה השווי ב-2022?", pref(on))
        assert (t.mode, t.source, t.prefer_recent) == ("explicit", "question", on)


def test_the_result_is_a_pure_function_of_its_inputs():
    a = TF.resolve("מה השווי בשנה שעברה?", pref(True))
    b = TF.resolve("מה השווי בשנה שעברה?", pref(True))
    assert a == b


def test_a_caller_that_gives_no_preference_gets_the_switch_off_and_a_calendar_anchor():
    t = TF.resolve("מה השווי?", None)
    assert t.mode == "none" and t.prefer_recent is False and isinstance(t.anchor, date)
    assert TF.resolve("מה השווי ב-2022?", None).mode == "explicit"
    bad = TF.resolve("מה השווי?", {"prefer_recent": True, "anchor_date": "not a date"})
    assert bad.mode == "recent" and isinstance(bad.anchor, date)


def test_the_default_mode_is_none():
    t = TF.TimeMode()
    assert t.mode == "none" and t.requested is None and t.source == "switch"


def test_the_diagnostics_record_carries_mode_source_switch_anchor_and_requested():
    t = TF.resolve("מה היה השווי ב-2022?", pref(False))
    d = t.as_dict()
    assert set(d) == {"mode", "source", "prefer_recent", "anchor", "requested"}
    assert d["mode"] == "explicit" and d["source"] == "question" and d["prefer_recent"] is False
    assert d["anchor"] == ANCHOR
    assert d["requested"]["kind"] == "point" and d["requested"]["start"] == "2022"
    assert set(d["requested"]) == {"kind", "start", "end", "precision", "text"}
    none = TF.resolve("מה השווי?", pref(True)).as_dict()
    assert none["requested"] is None and none["mode"] == "recent"
