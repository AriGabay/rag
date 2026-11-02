"""Round 9 evaluation measures (R29, KTD13), each separate: unit identification, source location, reading
completeness from the pages an answer cites, and per-stage time from the turn's diagnostics (median and p90 per
stage; a stored result without timing shows "—"). Synthetic answers only; no database, no stack, no model call."""

from __future__ import annotations

import json

import yaml

from eval.scoring import (
    percentile,
    reading_completeness,
    source_location,
    stage_time_stats,
    turn_measures,
    unit_identification,
)

DOC = "שומה הגפן"  # the document the turn is about
OTHER_DOC = "שומה הזית"


def _source(sid: str, title: str, pages: list[int]) -> dict:
    return {"id": sid, "document_id": f"doc-{title}", "title": title, "page_list": pages,
            "location": f"עמוד {pages[0]}"}


def _answer(*sources: dict) -> dict:
    return {"status": "answered", "markdown": "תשובה " + " ".join(f"[{s['id']}]" for s in sources),
            "sources": list(sources)}


def _timing(**stages: float) -> dict:
    return {"total_seconds": sum(stages.values()) + 1.0,
            "stages": {k: {"seconds": v, "calls": 1} for k, v in stages.items()},
            "unattributed_seconds": 1.0, "tools": []}


# --- unit identification -----------------------------------------------------------------------------------------

def test_pages_cited_inside_the_expected_range_pass_unit_identification():
    a = _answer(_source("S1", f"{DOC} 12", [41, 42]), _source("S2", f"{DOC} 12", [44]))
    r = unit_identification(a, {"expect_unit_pages": [40, 50], "expect_document": DOC})
    assert r["passed"] and r["outside"] == [] and r["cited"] == [41, 42, 44]


def test_another_units_pages_fail_unit_identification_and_are_named():
    a = _answer(_source("S1", f"{DOC} 12", [41]), _source("S2", f"{DOC} 12", [212, 213]))
    r = unit_identification(a, {"expect_unit_pages": [40, 50], "expect_document": DOC})
    assert not r["passed"] and r["outside"] == [212, 213]
    assert "212" in r["problems"][0] and "213" in r["problems"][0]


def test_only_the_expected_documents_pages_count():
    a = _answer(_source("S1", f"{DOC} 12", [41]), _source("S2", f"{OTHER_DOC} 7", [300]))
    assert unit_identification(a, {"expect_unit_pages": [40, 50], "expect_document": DOC})["passed"]


def test_no_cited_source_of_the_document_fails_unit_identification():
    a = _answer(_source("S1", f"{OTHER_DOC} 7", [45]))
    r = unit_identification(a, {"expect_unit_pages": [40, 50], "expect_document": DOC})
    assert not r["passed"] and r["cited"] == [] and r["problems"]


def test_a_measure_is_reported_only_when_the_turn_names_its_expectation():
    a = _answer(_source("S1", f"{DOC} 12", [41]))
    assert unit_identification(a, {}) is None
    assert source_location(a, {"expect_unit_pages": [40, 50]}) is None
    assert reading_completeness(a, {}) is None
    assert turn_measures(a, {}) == {}


# --- source location and reading completeness ---------------------------------------------------------------------

def test_source_location_is_the_fraction_of_expected_pages_cited():
    a = _answer(_source("S1", f"{DOC} 12", [41, 42]))
    r = source_location(a, {"expect_source_pages": [41, 42, 43, 44], "expect_document": DOC})
    assert (r["found"], r["of"], r["fraction"]) == (2, 4, 0.5) and r["missing"] == [43, 44]


def test_reading_completeness_uses_the_cited_pages_and_says_so():
    a = _answer(_source("S1", f"{DOC} 12", [41]), _source("S2", f"{DOC} 12", [43]))
    r = reading_completeness(a, {"expect_read_pages": [41, 42, 43]}, timing=_timing(read=2.0))
    assert (r["found"], r["of"]) == (2, 3) and r["missing"] == [42]
    assert r["signal"] == "sources"  # the read records of the timing hold no pages


def test_a_source_without_page_list_is_read_from_its_location_text():
    a = {"sources": [{"id": "S1", "title": f"{DOC} 12", "location": "עמודים 41–43"},
                     {"id": "S2", "title": f"{DOC} 12", "page": 50}]}
    r = source_location(a, {"expect_source_pages": [41, 42, 43, 50]})
    assert r["found"] == 4


def test_turn_measures_keeps_each_measure_apart():
    a = _answer(_source("S1", f"{DOC} 12", [41]))
    m = turn_measures(a, {"expect_unit_pages": [40, 50], "expect_source_pages": [41, 42], "expect_document": DOC})
    assert set(m) == {"unit", "location"} and m["unit"]["passed"] and m["location"]["fraction"] == 0.5


# --- per-stage time -----------------------------------------------------------------------------------------------

def test_percentile_matches_the_reports_nearest_rank():
    xs = [float(i) for i in range(1, 11)]
    assert percentile(xs, 0.5) == 6.0 and percentile(xs, 0.9) == 10.0 and percentile([3.0], 0.9) == 3.0


def test_stage_timings_aggregate_into_median_and_p90_per_stage():
    turns = [{"diagnostics": {"timing": _timing(locate=float(i), read=2.0 * i)}} for i in range(1, 11)]
    stats = stage_time_stats(turns)
    assert stats["locate"] == {"n": 10, "median": 6.0, "p90": 10.0, "max": 10.0, "calls": 1.0}
    assert stats["read"]["median"] == 12.0 and stats["read"]["p90"] == 20.0
    assert stats["compose"]["n"] == 0 and stats["compose"]["median"] is None  # no turn reached it
    assert stats["unattributed"]["median"] == 1.0


def test_turns_without_timing_are_left_out_of_the_stage_statistics():
    turns = [{"diagnostics": None}, {}, {"diagnostics": {"timing": _timing(locate=3.0)}}]
    assert stage_time_stats(turns)["locate"]["n"] == 1


# --- the report and the rescore -----------------------------------------------------------------------------------

def _stored(with_timing: bool) -> list[dict]:
    turn = {"ask": "מה השווי?", "status": "done", "seconds": 9.0,
            "usage": [{"purpose": "agent", "model": "gpt-6-luna", "input_tokens": 100, "output_tokens": 10,
                       "cost_usd": 0.02}],
            "diagnostics": {"timing": _timing(locate=2.0, read=4.0, compose=1.5)} if with_timing else None,
            "answer": _answer(_source("S1", f"{DOC} 12", [41, 42]), _source("S2", f"{DOC} 12", [212]))}
    return [{"kind": "answers", "id": "C1", "ok": True, "detail": [], "data": {"turns": [turn]}}]


SPEC = {"title": "סט סינתטי", "answers": [{"id": "C1", "turns": [{"ask": "מה השווי?", "expect": {
    "expect_document": DOC, "expect_unit_pages": [40, 50], "expect_source_pages": [41, 42],
    "expect_read_pages": [41, 42, 43]}}]}]}


def test_rescore_of_a_stored_result_produces_the_new_columns(tmp_path):
    from eval.chat_eval import report, rescore

    (c1,) = rescore(SPEC, _stored(with_timing=True))
    m = c1.data["turns"][0]["measures"]
    assert not m["unit"]["passed"] and m["unit"]["outside"] == [212]
    assert m["location"]["fraction"] == 1.0 and m["reading"]["found"] == 2
    text = report([c1], tmp_path / "r.md", "t")
    for column in ("זיהוי יחידה", "מיקום מקורות", "שלמות קריאה", "חישוב", "שלמות אחרי אימות", "עלות"):
        assert column in text
    assert "212" in text  # the pages outside the unit are named
    assert "| locate |" in text and "| read |" in text and "| compose |" in text
    assert "0.0200$" in text  # the turn's cost


def test_a_result_without_timing_renders_a_dash(tmp_path):
    from eval.chat_eval import report, rescore

    text = report(rescore(SPEC, _stored(with_timing=False)), tmp_path / "r.md", "t")
    assert "| locate | 0 | — | — | — | — |" in text


def test_rescore_from_the_command_line_writes_the_measures_without_the_stack(tmp_path):
    from eval import chat_eval

    (tmp_path / "set.yaml").write_text(yaml.safe_dump(SPEC, allow_unicode=True), encoding="utf-8")
    (tmp_path / "res.json").write_text(json.dumps(_stored(with_timing=True), ensure_ascii=False), encoding="utf-8")
    assert chat_eval.main(["--set", str(tmp_path / "set.yaml"), "--out", str(tmp_path / "out"),
                           "--rescore", str(tmp_path / "res.json")]) == 0
    (rescored,) = (tmp_path / "out").glob("rescored-*.json")
    (c1,) = json.loads(rescored.read_text(encoding="utf-8"))
    assert c1["data"]["turns"][0]["measures"]["unit"]["outside"] == [212]
