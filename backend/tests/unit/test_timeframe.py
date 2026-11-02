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


# --- an explicit period carries to a follow-up that names no time (round 22 U12, KTD14, R21) ------------------------------------------------


def stated(question: str, message_id: str = "m-1", on: bool = False) -> dict:
    """The period a property turn leaves for the next one (``period_of``), from the question that stated it."""
    period = TF.period_of(TF.resolve(question, pref(on)), message_id)
    assert period is not None, question
    return period


def test_the_period_a_question_stated_is_what_the_turn_leaves_and_a_turn_on_the_switch_leaves_none():
    period = stated("מה היה השווי ב-2022?")
    assert period["source_message_id"] == "m-1" and period["requested"]["start"] == "2022" and period["anchor"] == ANCHOR
    assert TF.period_of(TF.resolve("ומה לגבי השוכר?", pref(True)), "m-2") is None
    assert TF.period_of(TF.resolve("ומה לגבי השוכר?", pref(False)), "m-2") is None


@pytest.mark.parametrize("on", [False, True])
def test_a_follow_up_that_names_no_time_carries_the_previous_questions_explicit_period_with_the_switch_either_way(on):
    period = stated("מה היה השווי ב-2022?")
    follow = TF.carried(TF.resolve("ומה לגבי השוכר?", pref(on)), period)
    assert follow.mode == "explicit" and follow.source == "carried" and follow.carried_from == "m-1"
    assert shape(follow.requested) == ("point", "2022", "2022", "year") and follow.prefer_recent is on
    assert follow.as_dict()["carried_from"] == "m-1" and follow.as_dict()["source"] == "carried"
    assert "בשאלה קודמת" in TF.block(follow) and "2022" in TF.block(follow)


def test_a_follow_up_that_states_its_own_time_keeps_it_and_never_inherits_the_old_one():
    period = stated("מה היה השווי ב-2022?")
    own = TF.resolve("ומה היה ב-2024?", pref(True))
    assert TF.carried(own, period) is own and shape(own.requested) == ("point", "2024", "2024", "year")
    assert own.source == "question" and own.carried_from is None


def test_with_the_latest_switch_on_an_explicit_period_still_wins_and_without_a_period_the_switch_decides():
    period = stated("מה היה השווי ב-2022?", on=True)
    on = TF.carried(TF.resolve("ומה לגבי השוכר?", pref(True)), period)
    assert on.mode == "explicit" and on.requested.start == "2022"  # not "recent": the period wins over the switch
    assert TF.carried(TF.resolve("ומה לגבי השוכר?", pref(True)), None).mode == "recent"
    assert TF.carried(TF.resolve("ומה לגבי השוכר?", pref(False)), None).mode == "none"


def test_the_period_is_carried_on_from_turn_to_turn_with_its_first_source_and_a_relative_period_keeps_its_days():
    period = stated("מה היה השווי בשנה שעברה?")
    assert period["requested"]["kind"] == "relative"
    second = TF.carried(TF.resolve("ומה לגבי השוכר?", pref(True, "2027-03-01")), period)  # a year later: the same days
    assert second.requested.kind == "point" and (second.requested.start, second.requested.end) == ("2025", "2025")
    again = TF.period_of(second, "m-2")
    assert again["source_message_id"] == "m-1"  # the question that stated it, not the message that carried it
    third = TF.carried(TF.resolve("ומה לגבי המשכיר?", pref(False)), again)
    assert third.carried_from == "m-1" and third.requested.start == "2025"


@pytest.mark.parametrize("period", [None, {}, {"requested": None}, {"requested": {"kind": "point"}},
                                    {"requested": {"kind": "open", "start": None, "end": None, "precision": "year"}}])
def test_a_period_that_does_not_read_as_one_carries_nothing(period):
    t = TF.resolve("ומה לגבי השוכר?", pref(True))
    assert TF.carried(t, period) is t
