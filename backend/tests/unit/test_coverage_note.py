"""The coverage note the server writes from a coverage record, and the coverage summary the model reads (round 12 U8: R12,
R13, R14; KTD7, KTD8; AE4).

Records are built by hand in the shape ``tools.coverage_record`` makes and assessed by ``tools.assess_coverage``, so the
completeness the note words is the real rule's. What is proven: the state table of KTD8 (partial recent / explicit, not
identified, complete, nothing without a record), the reason groups (at most three named units, newest first, then "ועוד N"),
that a complete note never claims the latest in the repository, and that the note is a function of the record alone
(independent of timeline rows). Every title and date is invented."""

from __future__ import annotations

from datetime import date

import pytest

from app.chat import timeframe as TF
from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode

ANCHOR = date(2026, 10, 14)
RECENT = TimeMode("recent", "switch", ANCHOR, None, True)
EXPLICIT = TimeMode("explicit", "question", ANCHOR, Requested("point", "2022", "2022", "year", "ב-2022"), True)
LATEST_PHRASE = "הנתון המאוחר ביותר במקורות שנבדקו"


def unit(n: int, iso: str | None, status: str = T.HIT, title: str | None = None, precision: str = "day") -> dict:
    return {"document_id": f"d{n}", "version_id": f"v{n}", "unit_no": 1, "title": title or f"שומה סינתטית {n}",
            "date": iso, "precision": precision if iso else None, "date_status": "stated" if iso else "none",
            "undated": iso is None, "status": status, "link": "parcel 7101/15", "focus": n == 1, "blocks": False}


def record(time: TimeMode, units: list[dict], *, candidates: dict | None = None, undetermined: int = 0,
           scope: str = T.PROPERTY, identified: bool = True) -> dict:
    rec = {"v": 1, "mode": time.mode, "requested": time.requested.as_dict() if time.requested else None, "scope": scope,
           "via": "focus", "identified": identified,
           "property": {"label": "הצאלון 7", "reasons": ["parcel 7101/15"], "units": len(units)},
           "units": units, "candidates": candidates or {"named": [], "count": 0}, "undetermined": undetermined}
    return T.assess_coverage(time, rec)


def test_ae4_one_undated_unit_not_searched_makes_recent_partial_and_the_note_names_it():
    rec = record(RECENT, [unit(1, "2025-03-31"), unit(2, "2024-06-15"), unit(3, None, T.NOT_SEARCHED_CAP, "שומה ללא תאריך סינתטית")])
    assert not rec["complete"]
    note = TF.coverage_note(rec)
    assert note.splitlines()[0] == TF.MSG_NOTE_RECENT and LATEST_PHRASE in note
    assert "שומה ללא תאריך סינתטית" in note


def test_the_recent_phrase_is_the_qualified_one_never_the_latest_in_the_repository():
    rec = record(RECENT, [unit(1, "2025-03-31", T.NO_MATCH), unit(2, "2024-06-15")])
    note = TF.coverage_note(rec)
    assert LATEST_PHRASE in note
    assert "במאגר" not in note and "העדכני ביותר" not in note.replace(LATEST_PHRASE, "")


def test_a_newer_unit_searched_without_a_match_is_named_with_its_date():
    rec = record(RECENT, [unit(1, "2025-03-31", T.NO_MATCH, "שומה חדשה סינתטית"), unit(2, "2024-06-15")])
    note = TF.coverage_note(rec)
    assert "שומה חדשה סינתטית" in note and "31/03/2025" in note
    assert "שומה סינתטית 2" not in note, "an older unit that was matched is no part of the gap"


def test_older_units_left_unsearched_are_complete_and_the_complete_note_states_the_count():
    units = [unit(1, "2026-01-10")] + [unit(n, f"20{26 - n:02d}-01-10", T.NOT_SEARCHED_CAP) for n in range(2, 11)]
    rec = record(RECENT, units)
    assert rec["complete"]
    note = TF.coverage_note(rec)
    assert note == TF.MSG_NOTE_COMPLETE.format(n=10)
    assert "10" in note and "במאגר" not in note and LATEST_PHRASE not in note and "העדכני" not in note


def test_explicit_mode_partial_uses_its_own_sentence_and_names_the_unsearched_later_unit():
    rec = record(EXPLICIT, [unit(1, "2024-05-01", T.NOT_SEARCHED_CAP, "שומת 2024 סינתטית"), unit(2, "2022-03-01")])
    assert not rec["complete"]
    note = TF.coverage_note(rec)
    assert note.splitlines()[0] == TF.MSG_NOTE_EXPLICIT
    assert "שומת 2024 סינתטית" in note and LATEST_PHRASE not in note


def test_not_identified_says_only_the_focus_appraisal_was_checked():
    rec = record(RECENT, [unit(1, "2025-03-31")], scope=T.UNIT, identified=False)
    rec["property"] = None
    assert TF.coverage_note(T.assess_coverage(RECENT, rec)) == TF.MSG_NOTE_NOT_IDENTIFIED
    rec = record(EXPLICIT, [unit(1, "2022-03-31")], scope=T.PROPERTY, identified=False)
    assert TF.coverage_note(rec) == TF.MSG_NOTE_NOT_IDENTIFIED


def test_a_search_window_is_partial_and_says_no_inventory_was_made():
    rec = record(RECENT, [unit(1, "2025-03-31"), unit(2, "2024-06-15")], scope=T.WINDOW, identified=False)
    note = TF.coverage_note(rec)
    assert note.splitlines()[0] == TF.MSG_NOTE_RECENT and TF.MSG_GROUP_WINDOW in note


def test_five_units_in_a_reason_group_are_three_named_newest_first_then_and_2_more():
    capped = [unit(n, f"20{20 + n:02d}-06-01", T.NOT_SEARCHED_CAP, f"שומת {2020 + n} סינתטית") for n in range(2, 7)]
    rec = record(RECENT, [unit(1, None, T.NO_MATCH, "שומה ללא תאריך")] + capped)
    # the five dated, unsearched units all precede nothing found: the record is partial and they are the gap
    assert not rec["complete"]
    (line,) = [ln for ln in TF.coverage_note(rec).splitlines() if "שומת 2026 סינתטית" in ln]
    assert {y for y in range(2022, 2027) if f"שומת {y} סינתטית" in line} == {2026, 2025, 2024}, line
    assert line.index("2026") < line.index("2025") < line.index("2024"), "newest first"
    assert "שומת 2023 סינתטית" not in line and "שומת 2022 סינתטית" not in line
    assert line.rstrip(".").endswith("ועוד 2")


def test_candidates_are_named_up_to_three_from_the_record_and_the_rest_counted():
    named = [{"document_id": f"c{n}", "version_id": f"cv{n}", "unit_no": 1, "title": f"מועמדת {n} סינתטית",
              "date": f"202{n}-01-01", "precision": "day", "date_status": "stated", "reason": "city missing"}
             for n in (1, 2, 3)]
    rec = record(RECENT, [unit(1, "2025-03-31")], candidates={"named": named, "count": 5})
    note = TF.coverage_note(rec)
    assert not rec["complete"]
    assert all(f"מועמדת {n} סינתטית" in note for n in (1, 2, 3)) and "ועוד 2" in note
    assert note.index("מועמדת 3") < note.index("מועמדת 2") < note.index("מועמדת 1"), "newest first"


def test_undetermined_units_are_counted_never_named():
    rec = record(RECENT, [unit(1, "2025-03-31")], undetermined=4)
    note = TF.coverage_note(rec)
    assert not rec["complete"] and "4" in note


def test_a_unit_is_named_by_its_title_and_date_and_an_undated_one_by_its_title_alone():
    rec = record(RECENT, [unit(1, "2025-03-31", T.NO_MATCH, "שומה א"), unit(2, None, T.NOT_SEARCHED_CAP, "שומה ב")])
    lines = TF.coverage_note(rec).splitlines()
    assert any("שומה א" in ln and "31/03/2025" in ln for ln in lines)
    assert any("שומה ב" in ln and "/" not in ln for ln in lines)


@pytest.mark.parametrize("units", [[unit(1, "2025-03-31"), unit(2, None, T.HIT)]])
def test_the_note_depends_on_the_record_alone(units):
    rec = record(RECENT, units)
    assert TF.coverage_note(rec) == TF.coverage_note(dict(rec)), "same record, same words"
    assert TF.coverage_note(None) is None, "a message without a record reads as not recorded, never complete"


def test_a_record_of_a_turn_with_no_time_mode_has_no_note():
    rec = record(RECENT, [unit(1, "2025-03-31")])
    rec["mode"] = "none"
    assert TF.coverage_note(rec) is None


def test_the_summary_for_the_model_carries_the_note_and_the_phrase_rule_only_when_partial():
    partial = record(RECENT, [unit(1, "2025-03-31", T.NO_MATCH), unit(2, "2024-06-15")])
    complete = record(RECENT, [unit(1, "2025-03-31"), unit(2, "2024-06-15")])
    assert LATEST_PHRASE in TF.coverage_summary(partial) and TF.coverage_note(partial) in TF.coverage_summary(partial)
    assert LATEST_PHRASE not in TF.coverage_summary(complete)
    assert TF.coverage_summary(None) is None


def test_the_time_block_states_the_phrase_rule_in_both_modes():
    for mode in (RECENT, EXPLICIT):
        block = TF.block(mode)
        assert LATEST_PHRASE in block
    assert TF.block(TimeMode("none", "switch", ANCHOR, None, False)) is None


# --- the payload (``timeline.payload``): the note is independent of the timeline's rows ---------------------------------------

def _ws(time: TimeMode = RECENT):
    from app.chat.tools import Workspace

    ws = Workspace(ctx=None)
    ws.time = time
    return ws


def test_the_payload_carries_the_record_and_the_note_with_no_timeline_rows_and_the_copy_text_is_the_note():
    from app.chat import timeline

    rec = record(RECENT, [unit(1, "2025-03-31"), unit(2, None, T.NOT_SEARCHED_CAP, "שומה ללא תאריך")])
    out = timeline.payload(_ws(), set(), coverage=rec)
    assert out["timeline"] == [] and out["coverage"]["v"] == 1 and out["coverage"]["note"] == TF.coverage_note(rec)
    assert out["text"] == out["coverage"]["note"]
    assert out["coverage"]["units"] == rec["units"], "the record itself is kept, with the note added"
    assert "note" not in rec, "the caller's record is not changed"


def test_the_payload_without_a_record_has_no_coverage_key_and_no_text():
    from app.chat import timeline

    out = timeline.payload(_ws(TimeMode("none", "switch", ANCHOR, None, False)), set())
    assert "coverage" not in out and out["text"] is None


def test_the_explicit_note_and_the_not_identified_note_reach_the_payload():
    from app.chat import timeline

    explicit = record(EXPLICIT, [unit(1, "2024-05-01", T.NOT_SEARCHED_CAP, "שומת 2024 סינתטית"), unit(2, "2022-03-01")])
    out = timeline.payload(_ws(EXPLICIT), set(), coverage=explicit)
    assert out["coverage"]["note"].splitlines()[0] == TF.MSG_NOTE_EXPLICIT and "שומת 2024 סינתטית" in out["text"]
    lone = record(RECENT, [unit(1, "2025-03-31")], scope=T.UNIT, identified=False)
    assert timeline.payload(_ws(), set(), coverage=lone)["coverage"]["note"] == TF.MSG_NOTE_NOT_IDENTIFIED
