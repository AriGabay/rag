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


def unit(n: int, iso: str | None, status: str = T.LOCATED, title: str | None = None, precision: str = "day") -> dict:
    return {"document_id": f"d{n}", "version_id": f"v{n}", "unit_no": 1, "title": title or f"שומה סינתטית {n}",
            "date": iso, "precision": precision if iso else None, "date_status": "stated" if iso else "none",
            "undated": iso is None, "status": status, "link": "parcel 7101/15", "focus": n == 1, "blocks": False}


def record(time: TimeMode, units: list[dict], *, candidates: dict | None = None, undetermined: int | None = None,
           scope: str = T.PROPERTY, identified: bool = True, possible: list[dict] | None = None, unlinkable: int = 0,
           excluded: int = 0, later_undecided: list[dict] | None = None) -> dict:
    """A record as ``coverage_record`` makes it (v2); ``undetermined`` builds the v1 shape instead (its single count)."""
    rec = {"v": 1 if undetermined is not None else 2, "mode": time.mode,
           "requested": time.requested.as_dict() if time.requested else None, "scope": scope,
           "via": "focus", "identified": identified,
           "property": {"label": "הצאלון 7", "reasons": ["parcel 7101/15"], "units": len(units)},
           "units": units, "candidates": candidates or {"named": [], "count": 0}}
    if undetermined is not None:
        rec["undetermined"] = undetermined
    else:
        rec |= {"possible": {"named": [], "count": 0, "read": 0, "items": possible or []}, "unlinkable": unlinkable,
                "excluded": excluded}
    if later_undecided:
        rec["later_undecided"] = later_undecided
    return T.assess_coverage(time, rec)


def possible_unit(n: int, iso: str | None, title: str | None = None, *, elsewhere: bool = False, read: bool = False) -> dict:
    """A possible candidate: an unidentified unit that mentions the property or was read in part (round 13 U8, R18)."""
    return {"document_id": f"p{n}", "version_id": f"pv{n}", "unit_no": 1, "title": title or f"מועמדת בלי זיהוי {n}",
            "date": iso, "precision": "day" if iso else None, "date_status": "stated" if iso else "none",
            "reason": "mentioned", "elsewhere": elsewhere, "read": read}


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


def test_a_v1_record_with_an_undetermined_count_renders_as_it_did():
    """Round 13 U8: an answer stored under coverage v1 (the single ``undetermined`` count, the status ``hit``) still renders."""
    stored = {"v": 1, "mode": "recent", "complete": False, "blocking": ["undetermined"], "undetermined": 4,
              "units": [unit(1, "2025-03-31", "hit")], "candidates": {"named": [], "count": 0}}
    note = TF.coverage_note(stored)
    assert note.splitlines() == [TF.MSG_NOTE_RECENT, f"{TF.MSG_GROUP_UNDETERMINED}: 4."], "the stored record is read as it is"
    old = record(RECENT, [unit(1, "2025-03-31")], undetermined=4)  # a v1-shaped record assessed today still blocks on it
    assert not old["complete"] and old["blocking"] == [TF.B_UNDETERMINED]


def test_ae9_possible_candidates_are_named_up_to_three_and_the_unlinkable_ones_are_counted_apart():
    """Two possible candidates are named (newest first) and block; the unlinkable units are one count line, and a unit proven
    to be another property is no part of the note."""
    rec = record(RECENT, [unit(1, "2025-03-31")], unlinkable=18, excluded=1,
                 possible=[possible_unit(7, "2022-08-28", "מועדאת ישנה"), possible_unit(15, "2020-04-28", "מועדאת ישנה מאוד")])
    assert not rec["complete"] and rec["blocking"] == [T.B_POSSIBLE] and rec["possible"]["count"] == 2
    lines = TF.coverage_note(rec).splitlines()
    (named,) = [ln for ln in lines if "מועדאת ישנה" in ln]
    assert named.index("מועדאת ישנה (") < named.index("מועדאת ישנה מאוד"), "newest first"
    assert f"{TF.MSG_GROUP_UNLINKABLE}: 18." in lines
    assert not any("1" == ln.strip(". ") for ln in lines) and "אחר" not in " ".join(lines)


def test_five_possible_candidates_name_three_newest_first_and_count_the_rest():
    rec = record(RECENT, [unit(1, "2025-03-31")],
                 possible=[possible_unit(n, f"20{20 + n:02d}-01-01", f"מועמדת {n} סינתטית") for n in range(1, 6)])
    assert rec["possible"]["count"] == 5 and len(rec["possible"]["named"]) == 3
    (line,) = [ln for ln in TF.coverage_note(rec).splitlines() if "מועמדת 5 סינתטית" in ln]
    assert line.index("מועמדת 5") < line.index("מועמדת 4") < line.index("מועמדת 3") and "מועמדת 2" not in line
    assert line.rstrip(".").endswith("ועוד 2")


def test_only_unlinkable_units_outside_the_property_leave_the_record_complete_and_the_note_counts_them():
    rec = record(RECENT, [unit(1, "2025-03-31")], unlinkable=6, excluded=3)
    assert rec["complete"] and not rec["blocking"], "an unlinkable unit blocks nothing"
    assert TF.coverage_note(rec).splitlines() == [TF.MSG_NOTE_COMPLETE.format(n=1), f"{TF.MSG_GROUP_UNLINKABLE}: 6."]
    assert TF.coverage_note(record(RECENT, [unit(1, "2025-03-31")])) == TF.MSG_NOTE_COMPLETE.format(n=1), "none: no line"


def test_a_possible_candidate_whose_mention_was_read_under_another_subject_stops_blocking_and_is_counted_apart():
    stays = possible_unit(1, "2023-01-01", "נשארת", read=True)               # read, but about the property: still a candidate
    left = possible_unit(2, "2022-01-01", "עזבה", elsewhere=True, read=True)  # read inside a comparison part
    unread = possible_unit(3, "2021-01-01", "לא נקראה", elsewhere=True)       # elsewhere, but the turn did not read it
    rec = record(RECENT, [unit(1, "2025-03-31")], possible=[stays, left, unread])
    assert rec["possible"]["count"] == 2 and rec["possible"]["read"] == 1 and rec["blocking"] == [T.B_POSSIBLE]
    assert [i["title"] for i in rec["possible"]["named"]] == ["נשארת", "לא נקראה"]
    done = record(RECENT, [unit(1, "2025-03-31")], possible=[left])
    assert done["complete"] and done["possible"]["count"] == 0 and done["possible"]["read"] == 1


def test_a_newer_unit_that_was_only_located_blocks_once_a_unit_was_read_and_the_note_names_its_date():
    newer, older = unit(1, "2025-03-31", T.LOCATED), unit(2, "2023-06-15", T.READ)
    rec = record(RECENT, [newer, older])
    assert not rec["complete"] and rec["blocking"] == [T.B_LOCATED] and rec["units"][0]["blocks"]
    note = TF.coverage_note(rec)
    assert TF.MSG_LOCATED.format(date="31/03/2025") in note.splitlines()
    assert note.splitlines()[0] == TF.MSG_NOTE_RECENT and LATEST_PHRASE in note
    # round 12 stands where nothing was read: a located newest unit is checked
    assert record(RECENT, [unit(1, "2025-03-31", T.LOCATED), unit(2, "2023-06-15", T.LOCATED)])["complete"]
    # the datum was read from the newest unit: an older located one does not matter
    assert record(RECENT, [unit(1, "2025-03-31", T.READ), unit(2, "2023-06-15", T.LOCATED)])["complete"]
    # explicit mode keeps its own rule: located is searched
    assert record(EXPLICIT, [unit(1, "2024-05-01", T.LOCATED), unit(2, "2022-03-01", T.READ)])["complete"]


def test_in_a_window_scope_a_newer_unit_that_was_only_located_is_not_listed_and_does_not_block():
    """Round 13 V5: with no identified property the units of a window are whatever the search found, other properties among
    them: one only located is no newer unit of the property, so it neither blocks (``B_LOCATED``) nor is named. The window
    itself still keeps the record partial."""
    units = [unit(1, "2025-03-31", T.LOCATED), unit(2, "2023-06-15", T.READ)]
    rec = record(RECENT, units, scope=T.WINDOW, identified=False)
    assert T.B_LOCATED not in rec["blocking"] and not any(u["blocks"] for u in rec["units"]) and not rec["complete"]
    assert TF.MSG_LOCATED.format(date="31/03/2025") not in (TF.coverage_note(rec) or "")
    # the same units of a property's own scope (gathered members) or a focused unit still block
    for scope in (T.PROPERTY, T.UNIT):
        again = record(RECENT, [dict(u) for u in units], scope=scope)
        assert T.B_LOCATED in again["blocking"] and again["units"][0]["blocks"]


def test_three_located_newer_units_are_named_newest_first_and_the_rest_counted():
    units = [unit(n, f"20{20 + n:02d}-06-01", T.LOCATED) for n in range(2, 7)] + [unit(1, "2019-06-01", T.READ)]
    rec = record(RECENT, units)
    lines = [ln for ln in TF.coverage_note(rec).splitlines() if "אך הנתון בה לא נקרא" in ln]
    assert [ln for ln in lines] == [TF.MSG_LOCATED.format(date=d) for d in ("01/06/2026", "01/06/2025", "01/06/2024")]
    assert TF.MSG_MORE.format(n=2) + "." in TF.coverage_note(rec).splitlines()


def test_a_newer_datum_of_an_undecided_class_makes_the_record_partial_and_the_note_names_it():
    later = [{"older": "V1", "newer": "V2", "title": "שומת 2026 סינתטית", "when": "03/2026", "document_id": "d9"}]
    rec = record(RECENT, [unit(1, "2025-03-31", T.READ)], later_undecided=later)
    assert not rec["complete"] and rec["blocking"] == [T.B_LATER_UNDECIDED]
    note = TF.coverage_note(rec)
    assert LATEST_PHRASE in note and "שומת 2026 סינתטית" in note and "03/2026" in note


def test_the_summary_for_the_model_names_the_located_unit_so_it_can_read_it():
    rec = record(RECENT, [unit(1, "2025-03-31", T.LOCATED), unit(2, "2023-06-15", T.READ)])
    summary = TF.coverage_summary(rec)
    assert TF.MSG_LOCATED.format(date="31/03/2025") in summary and TF.PHRASE_RULE in summary


def test_a_unit_is_named_by_its_title_and_date_and_an_undated_one_by_its_title_alone():
    rec = record(RECENT, [unit(1, "2025-03-31", T.NO_MATCH, "שומה א"), unit(2, None, T.NOT_SEARCHED_CAP, "שומה ב")])
    lines = TF.coverage_note(rec).splitlines()
    assert any("שומה א" in ln and "31/03/2025" in ln for ln in lines)
    assert any("שומה ב" in ln and "/" not in ln for ln in lines)


@pytest.mark.parametrize("units", [[unit(1, "2025-03-31"), unit(2, None, T.LOCATED)]])
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
    assert out["timeline"] == [] and out["coverage"]["v"] == 2 and out["coverage"]["note"] == TF.coverage_note(rec)
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


# --- a turn that read sources and ran no time-ranked search (round 12 U12b): only what was read counts ---------------------

def test_a_record_of_reads_only_is_partial_and_the_note_names_the_units_that_were_not_checked():
    rec = record(RECENT, [unit(1, "2025-03-31", T.READ), unit(2, "2024-06-15", T.NOT_SEARCHED, "שומה שנייה סינתטית"),
                          unit(3, "2023-06-15", T.NOT_SEARCHED, "שומה שלישית סינתטית")])
    assert not rec["complete"] and rec["blocking"] == [T.B_NOT_SEARCHED]
    assert [u["blocks"] for u in rec["units"]] == [False, True, True]
    lines = TF.coverage_note(rec).splitlines()
    assert lines[0] == TF.MSG_NOTE_RECENT and LATEST_PHRASE in lines[0]
    group = next(ln for ln in lines if ln.startswith(TF.MSG_GROUP_READ_ONLY))
    assert "שומה שנייה סינתטית" in group and "שומה שלישית סינתטית" in group and "שומה סינתטית 1" not in group


def test_reads_only_in_explicit_mode_use_the_explicit_sentence_and_still_name_the_unchecked():
    rec = record(EXPLICIT, [unit(1, "2024-05-01", T.NOT_SEARCHED, "שומת 2024 סינתטית"), unit(2, "2022-03-01", T.READ)])
    assert not rec["complete"] and rec["blocking"] == [T.B_NOT_SEARCHED]
    note = TF.coverage_note(rec)
    assert note.splitlines()[0] == TF.MSG_NOTE_EXPLICIT and "שומת 2024 סינתטית" in note


def test_every_unit_of_the_property_read_is_complete_and_a_read_unit_counts_as_checked_for_the_newest_datum():
    rec = record(RECENT, [unit(1, "2025-03-31", T.READ), unit(2, "2024-06-15", T.READ)])
    assert rec["complete"] and rec["blocking"] == []
    assert TF.coverage_note(rec) == TF.MSG_NOTE_COMPLETE.format(n=2)


def test_a_dated_unit_not_searched_after_a_hit_blocks_only_for_reads_never_for_the_cap():
    """The cap leaves older units out without harm (AE4); a unit nothing searched or read is a gap whatever its age."""
    cap = record(RECENT, [unit(1, "2025-03-31"), unit(2, "2020-06-15", T.NOT_SEARCHED_CAP)])
    assert cap["complete"]
    reads = record(RECENT, [unit(1, "2025-03-31", T.READ), unit(2, "2020-06-15", T.NOT_SEARCHED)])
    assert not reads["complete"] and reads["blocking"] == [T.B_NOT_SEARCHED]


def test_an_undated_unit_not_searched_stays_in_the_undated_group_of_a_reads_only_record():
    rec = record(RECENT, [unit(1, "2025-03-31", T.READ), unit(2, None, T.NOT_SEARCHED, "שומה ללא תאריך סינתטית")])
    assert rec["blocking"] == [T.B_UNDATED]
    note = TF.coverage_note(rec)
    assert "שומה ללא תאריך סינתטית" in note and TF.MSG_GROUP_READ_ONLY not in note


def test_every_document_a_v2_record_names_is_in_the_visibility_check_of_the_answer():
    """Round 13 U8 (C10): the possible and unlinked candidates and the newer undecided datum the note names carry document
    ids, so a revoked one hides the answer."""
    from app.chat import api

    cand = {"named": [], "count": 1, "read": 0, "items": [possible_unit(1, "2023-01-01") | {"document_id": "dc"}]}
    rec = record(RECENT, [unit(1, "2025-03-31", T.READ)], possible=[possible_unit(2, "2022-01-01") | {"document_id": "dp"}],
                 later_undecided=[{"older": "V1", "newer": "V2", "title": "t", "when": "2026", "document_id": "dl"}])
    rec["candidates"] = cand
    assert {"d1", "dc", "dp", "dl"} <= api._answer_documents({"time": {"coverage": rec}})
