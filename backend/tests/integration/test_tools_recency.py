"""Server ranking in search, values and measurements (round 11 U8: R4, R6, R7, R8, R12, R15; KTD6, KTD7; AE1).

The turn's time mode (``Workspace.time``, set by the engine) decides how ``search`` orders the documents of its fused
window and how ``find_measurements`` orders a page; ``take_value`` records the date of a value; and the standards
listing names the edition in force at the property's valuation date. The tools are run directly under a workspace of a
given intent and time mode. Every document is synthetic, its dates and amounts invented."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat.timeframe import Requested, TimeMode
from app.db import tenant_tx
from app.platform.search import SearchScope, search_passages
from tests.factories import make_office
from tests.integration.test_tools_pools import run, sources_of, standard, workspace
from tests.support.unit_files import READING, unit_file

pytestmark = pytest.mark.db

ANCHOR = date(2026, 10, 14)
HEADER = "מסמך סינתטי — נתונים בדויים"
QUERY = "שווי הנכס ברחוב הרקפות"
BODY = "שווי הנכס ברחוב הרקפות 5 הוא {amount} ₪ (סינתטי)."
WEAK = "הערכת שווי {amount} ₪ (סינתטי)."
RECENT = TimeMode("recent", "switch", ANCHOR, None, True)
NONE = TimeMode("none", "switch", ANCHOR, None, False)


def explicit(start: str, end: str | None, precision: str, kind: str = "point") -> TimeMode:
    return TimeMode("explicit", "question", ANCHOR, Requested(kind, start, end, precision, "בשאלה"), True)


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def sha_of(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def dated(office, key: str, valuation: str | None, body: str = BODY, amount: str = "2,450,000", passages: int = 1) -> str:
    """A one-unit appraisal whose unit states ``valuation`` (None: no valuation date), with ``passages`` blocks that
    match the question. Returns its document id."""
    blocks = [HEADER, *([f"המועד הקובע: {valuation}"] if valuation else []),
              *[body.format(amount=amount) + " " * n for n in range(passages)]]
    doc, _ = unit_file(office, office.default_group_id, f"מסמך {key}", [{"title": f"מסמך {key}", "blocks": blocks}],
                       sha=sha_of(key))
    return doc


def ws_for(office, time: TimeMode, intent: str = "property") -> T.Workspace:
    ws = workspace(office, intent)
    ws.time = time
    return ws


def order(out: str) -> list[str]:
    """The documents of a search output in order, once each."""
    seen: list[str] = []
    for s in sources_of(out):
        if s["document_id"] not in seen:
            seen.append(s["document_id"])
    return seen


def lines(out: str) -> dict[str, str]:
    """document id -> the date line of its first hit."""
    found: dict[str, str] = {}
    for s in sources_of(out):
        found.setdefault(s["document_id"], s["date"])
    return found


def search(ws, limit: int = 6, query: str = QUERY, **kw) -> str:
    return run(ws, "search", query=query, limit=limit, **kw)


# --- search: the order, the window, the date lines ---------------------------------------------------------------------

def test_ae1_three_dated_appraisals_among_nine_others_the_newest_first_and_the_oldest_kept(office):
    d22 = dated(office, "a22", "20/05/2022", amount="2,100,000")
    d24 = dated(office, "a24", "15/03/2024", amount="2,300,000")
    d25 = dated(office, "a25", "15/04/2025", amount="2,450,000")
    others = [dated(office, f"u{n}", None, amount=f"1,{n:03d},000") for n in range(9)]
    ws = ws_for(office, RECENT)
    out = search(ws, limit=6)
    got = order(out)
    assert {s.public()["date_line"] for s in ws.sources.values()} >= {T.MSG_DATE_VALUATION.format(date="15/04/2025")}
    assert got[:3] == [d25, d24, d22], "newest first; the oldest is not cut by the limit"
    assert set(got[3:]) <= set(others) and len(got) == 12, "a small limit never cuts a relevant dated document"
    assert lines(out)[d25] == T.MSG_DATE_VALUATION.format(date="15/04/2025")
    assert all(lines(out)[d] == T.MSG_DATE_UNKNOWN for d in got[3:])


def window_documents(office) -> tuple[list[str], list[str]]:
    """(the documents of the fused window in the order of their best passage, the best passages' documents in order)."""
    with tenant_tx(office.ctx()) as conn:
        hits = search_passages(conn, QUERY, 12, scope=SearchScope(source_kinds=("appraisal",)), window=True)
    passages = [str(h["document_id"]) for h in hits]
    return list(dict.fromkeys(passages)), passages


def test_a_relevant_documents_best_passage_ranked_14th_by_passage_still_competes_and_is_first(office):
    for n in range(6):  # six documents with three passages each: eighteen passages that match every word, more relevant
        dated(office, f"hot{n}", "01/01/2021", amount=f"1,{n:03d},000", passages=3)
    newest = dated(office, "cold", "15/04/2025", body=WEAK, amount="2,450,000")
    docs, passages = window_documents(office)
    assert passages.index(newest) >= 13 and docs.index(newest) < 12, (passages.index(newest), docs.index(newest))
    got = order(search(ws_for(office, RECENT), limit=6))
    assert got[0] == newest
    # without the preference it is where relevance put it: not among the six shown
    assert newest not in order(search(ws_for(office, NONE), limit=6))


def test_a_newer_document_outside_the_first_twelve_documents_is_not_lifted_above_the_relevant_ones(office):
    hot = [dated(office, f"hot{n}", "01/01/2021", amount=f"1,{n:03d},000") for n in range(13)]
    newest = dated(office, "cold", "15/04/2025", body=WEAK, amount="2,450,000")
    docs, _ = window_documents(office)
    assert docs.index(newest) >= 12, docs.index(newest)
    out = search(ws_for(office, RECENT), limit=6)
    got = order(out)
    assert newest not in got and set(got) <= set(hot) and len(got) == 12
    # the twelve keep the order their dates give: equal dates, so relevance
    assert got == [d for d in docs if d in set(got)]


def test_limit_six_with_nine_relevant_dated_documents_returns_all_nine_best_hits_in_time_order(office):
    years = list(range(2016, 2025))
    docs = {y: dated(office, f"y{y}", f"15/04/{y}", amount=f"{y},000") for y in years}
    out = search(ws_for(office, RECENT), limit=6)
    assert order(out) == [docs[y] for y in reversed(years)]
    assert len(sources_of(out)) == 9, "one hit per document, however small the limit"
    # none keeps the limit
    assert len(sources_of(search(ws_for(office, NONE), limit=6))) <= 6


def test_a_value_dated_after_the_anchor_ranks_after_every_dated_value_and_before_the_unknown(office):
    future = dated(office, "future", "01/01/2028", amount="3,900,000")
    past = dated(office, "past", "15/03/2024", amount="2,300,000")
    unknown = dated(office, "unknown", None, amount="2,000,000")
    got = order(search(ws_for(office, RECENT), limit=6))
    assert got == [past, future, unknown]


def test_mode_none_keeps_relevance_order_and_adds_the_date_lines(office):
    d25 = dated(office, "a25", "15/04/2025", amount="2,450,000")
    d22 = dated(office, "a22", "20/05/2022", amount="2,100,000")
    unknown = dated(office, "u", None, amount="2,000,000")
    out = search(ws_for(office, NONE), limit=6)
    with tenant_tx(office.ctx()) as conn:
        plain = search_passages(conn, QUERY, 6, scope=SearchScope(source_kinds=("appraisal",)))
    assert order(out) == list(dict.fromkeys(str(h["document_id"]) for h in plain))
    assert lines(out) == {d25: T.MSG_DATE_VALUATION.format(date="15/04/2025"),
                          d22: T.MSG_DATE_VALUATION.format(date="20/05/2022"), unknown: T.MSG_DATE_UNKNOWN}


def test_an_explicit_year_puts_that_years_document_first_then_the_nearest(office):
    d22 = dated(office, "a22", "20/05/2022", amount="2,100,000")
    d24 = dated(office, "a24", "15/03/2024", amount="2,300,000")
    d25 = dated(office, "a25", "15/04/2025", amount="2,450,000")
    d19 = dated(office, "a19", "01/02/2019", amount="1,800,000")
    unknown = dated(office, "u", None, amount="2,000,000")
    got = order(search(ws_for(office, explicit("2022", "2022", "year")), limit=6))
    assert got == [d22, d24, d25, d19, unknown], "inside, then by distance (2024 is 440 days away, 2025 836, 2019 1065), then unknown"


def test_an_explicit_range_is_inclusive_by_its_precision(office):
    inside = dated(office, "in", "30/06/2023", amount="2,100,000")
    edge = dated(office, "edge", "01/01/2024", amount="2,200,000")
    after = dated(office, "after", "01/02/2024", amount="2,300,000")
    got = order(search(ws_for(office, explicit("2023-06", "2024-01", "month", "range")), limit=6))
    assert set(got[:2]) == {inside, edge}, "the whole of January 2024 is inside a range that ends in 2024-01"
    assert got[2] == after


def test_when_no_date_is_known_the_order_is_relevance_and_every_line_says_so(office):
    for n in range(4):
        dated(office, f"u{n}", None, amount=f"1,{n:03d},000")
    on = search(ws_for(office, RECENT), limit=6)
    off = search(ws_for(office, NONE), limit=6)
    assert order(on) == order(off)
    assert set(lines(on).values()) == {T.MSG_DATE_UNKNOWN}


def test_equal_dates_are_ordered_by_relevance(office):
    a = dated(office, "tie-a", "15/04/2025", amount="2,450,000")
    b = dated(office, "tie-b", "15/04/2025", amount="2,460,000")
    c = dated(office, "newer", "01/06/2025", amount="2,500,000")
    on = order(search(ws_for(office, RECENT), limit=6))
    assert on[0] == c
    off = order(search(ws_for(office, NONE), limit=6))
    assert on[1:] == [d for d in off if d in (a, b)]


def test_a_unit_that_states_two_dates_ranks_as_unknown_and_says_so(office):
    doc, _ = unit_file(office, office.default_group_id, "מסמך סתירה",
                       [{"title": "מסמך סתירה", "blocks": [HEADER, "המועד הקובע: 15/04/2024", "המועד הקובע: 01/06/2024",
                                                           BODY.format(amount="2,000,000")]}], sha=sha_of("conflict"))
    known = dated(office, "known", "15/04/2023", amount="2,100,000")
    out = search(ws_for(office, RECENT), limit=6)
    assert order(out) == [known, doc]
    assert T.MSG_DATE_UNKNOWN in lines(out)[doc]


def test_the_memo_key_holds_the_mode_and_the_requested_time(office):
    dated(office, "a", "15/04/2025")
    ws = ws_for(office, RECENT)
    search(ws)
    search(ws)
    assert len(ws.search_memo) == 1
    ws.time = explicit("2022", "2022", "year")
    search(ws)
    ws.time = explicit("2023", "2023", "year")
    search(ws)
    ws.time = NONE
    search(ws)
    assert len(ws.search_memo) == 4


def test_a_professional_question_gives_the_same_output_with_the_switch_on_and_off(office):
    std, _ = standard(office)
    dated(office, "a", "15/04/2025", body="הנכס ברחוב הרקפות 5. נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים.")
    dated(office, "b", "15/04/2022", body="הנכס ברחוב הרקפות 7. נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים.")
    q = "עסקאות השוואה בנכסים דומים"
    on = run(ws_for(office, RECENT, "professional"), "search", query=q)
    off = run(ws_for(office, NONE, "professional"), "search", query=q)
    assert on == off and std in on


def test_the_professional_hits_of_a_mixed_question_keep_their_order(office):
    standard(office)
    dated(office, "a", "15/04/2025", body="הנכס ברחוב הרקפות 5. נבחרה גישת ההשוואה לפי עסקאות השוואה בנכסים דומים.")
    q = "עסקאות השוואה בנכסים דומים"
    on = [s["document_id"] for s in sources_of(run(ws_for(office, RECENT, "mixed"), "search", query=q))
          if s.get("source_kind", "appraisal") != "appraisal"]
    off = [s["document_id"] for s in sources_of(run(ws_for(office, NONE, "mixed"), "search", query=q))
           if s.get("source_kind", "appraisal") != "appraisal"]
    assert on == off and on


# --- take_value: the date of a value ------------------------------------------------------------------------------------

ADOPTED = {"kind": "value", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "", "subject": "הנכס",
           "role": "other", "stance": "adopted"}


def take(ws, sid: str, quote: str, number: str, meaning: dict | None = None) -> str:
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": quote,
               "number": number}
    return run(ws, "take_value", source=sid, locator=locator, meaning=meaning or ADOPTED, label="שווי")


def first_source(ws, doc: str) -> str:
    return next(s.sid for s in ws.sources.values() if str(s.document_id) == doc)


def test_a_prose_conclusion_of_a_dated_unit_takes_the_units_valuation_date(office):
    doc = dated(office, "a", "15/04/2025", body="שווי הנכס נקבע ל-2,450,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    out = take(ws, first_source(ws, doc), "שווי הנכס נקבע ל-2,450,000 ₪", "2,450,000")
    assert out.startswith("V1 נרשם"), out
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["precision"], as_of["provenance"], as_of["forecast"]) == (
        "2025-04-15", "day", "unit_valuation", False)
    assert ws.values["V1"].public()["as_of"] == as_of
    assert "15/04/2025" in out


def test_a_value_whose_sentence_states_its_own_date_takes_that_one(office):
    doc = dated(office, "a", "15/04/2025", body="נכון ליום 01/03/2024 שווי הנכס נקבע ל-2,450,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    take(ws, first_source(ws, doc), "שווי הנכס נקבע ל-2,450,000 ₪", "2,450,000")
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["provenance"]) == ("2024-03-01", "source")


def test_a_value_that_is_not_the_units_conclusion_stays_undated(office):
    doc = dated(office, "a", "15/04/2025", body="לטענת המשיבה שווי הנכס 2,000,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    out = take(ws, first_source(ws, doc), "שווי הנכס 2,000,000 ₪", "2,000,000",
               ADOPTED | {"stance": "claim", "stated_by": "המשיבה"})
    assert ws.values["V1"].as_of is None and ws.values["V1"].public()["as_of"] is None
    assert T.MSG_DATE_UNKNOWN in out


def test_a_header_line_value_with_an_unknown_stance_is_dated_by_its_unit(office):
    """Round 11 U15 A: on the real model the header line "שווי הנכס: 2,410,000 ₪" is registered with the stance unknown
    (the source does not say who stated it); the unit's valuation date still dates it."""
    doc = dated(office, "h", "15/04/2025", body="שווי הנכס: 2,410,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    out = take(ws, first_source(ws, doc), "שווי הנכס: 2,410,000 ₪", "2,410,000", ADOPTED | {"stance": "unknown"})
    assert out.startswith("V1 נרשם"), out
    assert ws.values["V1"].stance == "unknown"
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["provenance"]) == ("2025-04-15", "unit_valuation")
    assert "15/04/2025" in out


def test_a_value_whose_role_is_comparison_in_prose_stays_undated(office):
    doc = dated(office, "c", "15/04/2025", body="שווי הנכס ברחוב הרקפות 7 לפי נכס דומה שנמכר ב-1,900,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    out = take(ws, first_source(ws, doc), "נכס דומה שנמכר ב-1,900,000 ₪", "1,900,000",
               ADOPTED | {"stance": "unknown", "role": "comparison"})
    assert ws.values["V1"].as_of is None and T.MSG_DATE_UNKNOWN in out


def test_a_value_attributed_to_another_party_stays_undated_whatever_its_stance(office):
    doc = dated(office, "p", "15/04/2025", body="שמאי המשיבה: שווי הנכס 2,000,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    take(ws, first_source(ws, doc), "שווי הנכס 2,000,000 ₪", "2,000,000",
         ADOPTED | {"stance": "unknown", "stated_by": "שמאי המשיבה"})
    assert ws.values["V1"].stated_by == "שמאי המשיבה"
    assert ws.values["V1"].provenance.get("stated_by") == "source"
    assert ws.values["V1"].as_of is None


def test_a_forecast_is_marked_even_with_no_date(office):
    doc = dated(office, "a", None, body="שווי הנכס צפוי להגיע ל-3,000,000 ₪ (סינתטי).")
    ws = ws_for(office, RECENT)
    search(ws)
    out = take(ws, first_source(ws, doc), "שווי הנכס צפוי להגיע ל-3,000,000 ₪", "3,000,000")
    as_of = ws.values["V1"].as_of
    assert as_of["date"] is None and as_of["forecast"] is True
    assert T.MSG_FORECAST in out


TABLE_CAPTION = "עסקאות השוואה בנכסים דומים (סינתטי)"
SECTION = "6. עסקאות השוואה"


def table_doc(office, caption: str, rows: list[list[str]], valuation: str = "15/04/2025", sha: str = "t" * 64,
              headers: list[str] | None = None) -> str:
    """An appraisal of one unit: a heading, its valuation date and one table of comparable transactions (stored as the
    reader stores a table: its blocks, its structure)."""
    from tests.factories import make_document

    doc, ver = make_document(office, office.default_group_id, "שומה עם טבלה (סינתטי)", sha=sha)
    structure = {"headers": headers or ["נכס", "מועד", "מחיר (₪)"], "caption": caption, "title": [], "notes": [], "section": SECTION,
                 "block_index": 2, "rows": [{"cells": r} for r in rows]}
    blocks = [("heading", SECTION, None), ("paragraph", f"המועד הקובע: {valuation}", None), ("table", caption, 0)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, table) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', :ti)"), {"d": doc, "v": ver, "b": i, "k": kind, "s": SECTION, "sp": [SECTION], "t": t,
                                       "ti": table})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc)


def take_cell(ws, doc: str, row: str, role: str = "comparison", caption: str = TABLE_CAPTION) -> str:
    handle = re.search(r"(T\d+) «" + re.escape(caption[:10]), T.tool_outline(ws, doc))
    assert handle, "the table is outlined"
    read = T.tool_read(ws, {"table": handle.group(1)})
    sid = re.search(r'<source id="(S\d+)"', read).group(1)
    locator = {"table": None, "row": row, "row_number": None, "column": "מחיר (₪)", "column_number": None, "quote": None,
               "number": None}
    meaning = {"kind": "price", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "", "subject": "עסקה",
               "role": role}
    return run(ws, "take_value", source=sid, locator=locator, meaning=meaning, label=row)


def test_a_table_row_that_states_its_own_date_is_dated_by_it_not_by_the_units_valuation(office):
    doc = table_doc(office, TABLE_CAPTION, [["רחוב הדקל 3", "01/03/2024", "1,900,000"], ["רחוב הברוש 5", "", "2,000,000"]])
    ws = ws_for(office, RECENT)
    out = take_cell(ws, doc, "רחוב הדקל 3")
    assert out.startswith("V1 נרשם"), out
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["provenance"]) == ("2024-03-01", "source") and "01/03/2024" in out


def test_a_comparable_row_with_no_date_of_its_own_stays_unknown_whatever_the_unit_states(office):
    doc = table_doc(office, TABLE_CAPTION, [["רחוב הדקל 3", "01/03/2024", "1,900,000"], ["רחוב הברוש 5", "", "2,000,000"]])
    ws = ws_for(office, RECENT)
    out = take_cell(ws, doc, "רחוב הברוש 5")
    assert out.startswith("V1 נרשם"), out
    assert ws.values["V1"].as_of is None and T.MSG_DATE_UNKNOWN in out


def test_a_row_of_a_comparables_table_with_a_neutral_role_and_no_date_stays_unknown(office):
    """Round 11 U15 A(c): the table's caption and its date column mark it as comparables; the role does not matter."""
    doc = table_doc(office, TABLE_CAPTION, [["רחוב הברוש 5", "", "2,000,000"]])
    ws = ws_for(office, RECENT)
    out = take_cell(ws, doc, "רחוב הברוש 5", role="other")
    assert out.startswith("V1 נרשם"), out
    assert ws.values["V1"].role == "other" and ws.values["V1"].as_of is None and T.MSG_DATE_UNKNOWN in out


def test_a_summary_table_cell_with_no_date_column_takes_the_units_valuation_date(office):
    caption = "סיכום השומה (סינתטי)"
    doc = table_doc(office, caption, [["שווי הנכס", "2,410,000"]], headers=["פרט", "מחיר (₪)"])
    ws = ws_for(office, RECENT)
    out = take_cell(ws, doc, "שווי הנכס", role="other", caption=caption)
    assert out.startswith("V1 נרשם"), out
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["provenance"]) == ("2025-04-15", "unit_valuation")


def test_a_row_takes_the_range_its_tables_caption_states(office):
    doc = table_doc(office, "מחירי עסקאות בין 2022 ל-2023 (סינתטי)", [["רחוב הדקל 3", "", "1,900,000"]])
    ws = ws_for(office, RECENT)
    handle = re.search(r"(T\d+) «", T.tool_outline(ws, doc)).group(1)
    sid = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"table": handle})).group(1)
    locator = {"table": None, "row": "רחוב הדקל 3", "row_number": None, "column": "מחיר (₪)", "column_number": None,
               "quote": None, "number": None}
    run(ws, "take_value", source=sid, locator=locator, label="x", meaning={
        "kind": "price", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "", "subject": "עסקה",
        "role": "comparison"})
    as_of = ws.values["V1"].as_of
    assert (as_of["date"], as_of["end"], as_of["precision"], as_of["provenance"]) == ("2022", "2023", "year", "source_table")


# --- find_measurements --------------------------------------------------------------------------------------------------

def add_measurements(office, doc: str, rows: list[tuple[str, str, str | None]]) -> None:
    """(value text, quote, stored effective date) rows of one document, in the order given."""
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": doc}).scalar_one()
        for i, (value_text, quote, effective) in enumerate(rows):
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
                " metric_kind, value, value_form, value_text, unit, period, area_basis, vat, subject, subject_role,"
                " value_role, quote, section, extraction_version, model, status, issues, effective_date) VALUES"
                " (app_office(), :d, :v, :b, :k, 'דמי שכירות', 'rent_per_area', :n, 'exact', :t, 'ILS_per_sqm', 'month',"
                " '', 'unknown', 'הנכס', 'appraised_property', 'appraiser_determination', :q, 'סיכום', 'test', 'test',"
                " 'auto_validated', '[]'::jsonb, :e)"),
                {"d": doc, "v": ver, "b": 2, "k": f"k{i}", "n": int(value_text.split()[0]), "t": value_text, "q": quote,
                 "e": effective})


RENTS = [("51 ₪", "דמי השכירות הם 51 ₪ למ\"ר לחודש נכון ל-01/03/2023", "2023-03-01"),
         ("52 ₪", "דמי השכירות הם 52 ₪ למ\"ר לחודש נכון ל-15/06/2025", "2025-06-15"),
         ("53 ₪", "דמי השכירות הם 53 ₪ למ\"ר לחודש", None),
         ("54 ₪", "דמי השכירות הם 54 ₪ למ\"ר לחודש נכון ל-01/01/2024", "2024-01-01"),
         ("55 ₪", "דמי השכירות הם 55 ₪ למ\"ר לחודש נכון ל-10/10/2022", "2022-10-10")]


def listed(out: str) -> list[str]:
    return re.findall(r"^\s+M\d+: [^=]*= (\d+) ₪", out, re.M)


def test_measurements_are_listed_newest_first_across_pages_with_no_row_skipped(office, monkeypatch):
    doc = dated(office, "m", "15/04/2025")
    add_measurements(office, doc, RENTS)
    monkeypatch.setattr(T, "MEASUREMENTS_MAX", 2)
    pages = []
    for page in (1, 2, 3):
        pages += listed(run(ws_for(office, RECENT), "find_measurements", query="", metric_kinds=["rent_per_area"],
                            page=page))
    assert pages == ["52", "54", "51", "55", "53"], "2025, 2024, 2023, 2022, then the undated one"


def test_measurements_show_their_date_and_mode_none_keeps_the_sql_order(office, monkeypatch):
    doc = dated(office, "m", "15/04/2025")
    add_measurements(office, doc, RENTS)
    out = run(ws_for(office, NONE), "find_measurements", query="", metric_kinds=["rent_per_area"])
    with tenant_tx(office.ctx()) as conn:
        sql_order = [str(int(r.value)) for r in conn.execute(text(
            "SELECT m.value FROM measurements m JOIN documents d ON d.id = m.document_id ORDER BY d.title, d.id,"
            " m.block_index NULLS FIRST, m.row_index NULLS FIRST, m.id"))]
    assert listed(out) == sql_order
    assert "01/03/2023" in out and out.count(T.MSG_DATE_UNKNOWN) == 1


def test_a_stored_date_the_quote_does_not_hold_is_not_a_date(office):
    doc = dated(office, "m", "15/04/2025")
    add_measurements(office, doc, [("51 ₪", "דמי השכירות הם 51 ₪ למ\"ר לחודש", "2023-03-01")])
    out = run(ws_for(office, RECENT), "find_measurements", query="", metric_kinds=["rent_per_area"])
    assert "01/03/2023" not in out and T.MSG_DATE_UNKNOWN in out


def test_explicit_time_orders_the_measurements_by_the_requested_year(office):
    doc = dated(office, "m", "15/04/2025")
    add_measurements(office, doc, RENTS)
    out = run(ws_for(office, explicit("2022", "2022", "year")), "find_measurements", query="",
              metric_kinds=["rent_per_area"])
    assert listed(out) == ["55", "51", "54", "52", "53"]


# --- standards: the edition in force at the valuation date ---------------------------------------------------------------

def editions(office):
    old, _ = standard(office, edition="גרסה 1.0", effective="01.01.2024", title="תקן 98.1 גרסה 1", sha="a" * 64)
    new, _ = standard(office, edition="גרסה 2.0", effective="01.06.2025", title="תקן 98.1 גרסה 2", sha="b" * 64)
    return old, new


def test_the_edition_in_force_at_the_valuation_date_is_named_with_the_switch_on_or_off(office):
    editions(office)
    dated(office, "p", "01/03/2025")
    for mode in (RECENT, NONE):
        ws = ws_for(office, mode)
        search(ws)  # the property's unit enters the turn
        out = run(ws, "find_standards")
        applicable = [ln for ln in out.splitlines() if ln.startswith(T.MSG_EDITION_IN_FORCE.split("{")[0])]
        assert len(applicable) == 1, out
        assert "גרסה 1.0" in applicable[0] and "גרסה 2.0" not in applicable[0] and "01/03/2025" in applicable[0]


def test_an_unknown_valuation_date_calls_no_edition_applicable(office):
    editions(office)
    dated(office, "p", None)
    ws = ws_for(office, RECENT)
    search(ws)
    out = run(ws, "find_standards")
    assert T.MSG_EDITION_DATE_UNKNOWN in out and T.MSG_EDITION_IN_FORCE.split("{")[0] not in out


def test_a_valuation_date_before_every_edition_names_none(office):
    editions(office)
    dated(office, "p", "01/03/2020")
    ws = ws_for(office, NONE)
    search(ws)
    out = run(ws, "find_standards")
    assert T.MSG_EDITION_NONE_YET in out


def test_a_professional_question_lists_the_editions_the_same_with_the_switch_on_and_off(office):
    editions(office)
    dated(office, "p", "01/03/2025")
    on = run(ws_for(office, RECENT, "professional"), "find_standards")
    off = run(ws_for(office, NONE, "professional"), "find_standards")
    assert on == off and T.MSG_EDITION_IN_FORCE.split("{")[0] not in on
    assert T.MSG_EDITION_DATE_UNKNOWN not in on


def test_a_hit_of_an_edition_says_whether_it_is_the_one_in_force_and_one_search_is_enough(office):
    old, new = editions(office)
    dated(office, "p", "01/03/2025")
    ws = ws_for(office, NONE, "mixed")
    out = run(ws, "search", query="על השמאי לבסס את הערכתו על עסקאות השוואה בנכסים דומים")
    hits = {s["document_id"]: s["body"] for s in sources_of(out) if s.get("source_kind") == "standard"}
    assert T.MSG_EDITION_THIS_IN_FORCE.format(date="01/03/2025") in hits[old]
    assert "01/03/2025" in hits[new] and T.MSG_EDITION_THIS_IN_FORCE.format(date="01/03/2025") not in hits[new]


def test_a_purely_professional_search_carries_no_edition_note(office):
    old, new = editions(office)
    dated(office, "p", "01/03/2025")
    out = run(ws_for(office, RECENT, "professional"), "search", query="על השמאי לבסס את הערכתו על עסקאות השוואה")
    assert all(T.MSG_EDITION_THIS_IN_FORCE.split("{")[0] not in s["body"] for s in sources_of(out))


def test_the_located_unit_of_a_file_of_several_decides_the_edition_not_its_neighbour(office):
    editions(office)
    doc, ver = unit_file(office, office.default_group_id, "קובץ מתחמים (סינתטי)", [
        {"title": "מתחם הגפן א", "blocks": [HEADER, "המועד הקובע: 01/03/2020", BODY.format(amount="1,500,000")]},
        {"title": "מתחם הברוש ב", "blocks": [HEADER, "המועד הקובע: 01/03/2025", BODY.format(amount="2,500,000")]}],
        sha=sha_of("multi"))
    ws = ws_for(office, NONE)
    assert T.locate_unit(ws, {"document_id": doc, "reading_id": READING, "unit_no": 2, "display": "מתחם הברוש ב"}, ver)
    out = run(ws, "find_standards")
    applicable = [ln for ln in out.splitlines() if ln.startswith(T.MSG_EDITION_IN_FORCE.split("{")[0])]
    assert len(applicable) == 1 and "גרסה 1.0" in applicable[0] and "01/03/2025" in applicable[0]
    assert "01/03/2020" not in out
