"""Round 22 U5 (KTD5, R8; finding g): a table's notes are bound to their scope and completed at the consumer from the stored structure.

``chunking._table_chunks`` writes the notes only into the last table chunk, so a row chunk or an earlier group chunk reaches the search hit
without them. A note that opens with a mark («(*)», «(1)») belongs to the rows that carry the mark (``scopes.note_rows``); an unmarked note to
its own table. The search hit gets a short flag with the marks (no text); take_value (the cell path and the quote path), the condition spans
and the verification read the APPLICABLE notes of the table of the source, never the notes of another table, a candidate-linked part or a
paragraph. Synthetic tables only; the stored structure is the per-turn cache (``Workspace.tables``), so no database is read.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.chat import items, verify
from app.chat import tools as T
from app.extraction.base import TableResult, TableRow
from app.extraction.chunking import _table_chunks
from tests.integration.test_chat_reading_tools import _hit_of, _searching, add_laid_out
from tests.integration.test_chat_reading_tools import (
    office as office,  # noqa: F401 - the fixture, taken by name
)

STAR = "(*) הסכומים מוצגים באלפי ₪"
ONE = "(1) התשואה אינה כוללת מע\"מ"
PLAIN = "הערה: הנתונים נכונים למועד הקובע"
HEADERS = ["תיאור", "מחיר (₪)"]
MARKED_ROW = 20
OTHER_MARKED_ROW = 85
VID = uuid.uuid4()


def _table(notes: list[str], rows: int = 90, marks: dict[int, str] | None = None, index: int = 0, candidate_of: int | None = None) -> TableResult:
    marks = marks if marks is not None else {MARKED_ROW: "(*)", OTHER_MARKED_ROW: "(1)"}
    body = [TableRow(page=1 if i < 45 else 2, cells=[f"דירה {i}", f"{1000 + i}" + (f" {marks[i]}" if i in marks else "")]) for i in range(rows)]
    return TableResult(index=index, headers=list(HEADERS), units=[None, "₪"], rows=body, page_start=1, page_end=2,
                       caption="להלן טבלת שווי", notes=notes, candidate_of=candidate_of)


def _structure(t: TableResult) -> dict:
    return {"headers": t.headers, "units": t.units, "caption": t.caption, "title": [], "notes": t.notes, "candidate_of": t.candidate_of,
            "rows": [{"page": r.page, "cells": r.cells} for r in t.rows]}


def _ws(*tables: TableResult) -> T.Workspace:
    ws = T.Workspace(ctx=None)
    for t in tables:
        ws.tables[(str(VID), t.index)] = (_structure(t), None)
    return ws


def _row_hit(t: TableResult, row: int) -> tuple[dict, str]:
    chunk = next(c for c in _table_chunks(t, "3. שווי", 1200) if c[0] == "table_row" and c[2] == row)
    return {"version_id": VID, "kind": "table_row", "table_index": t.index, "row_index": row}, chunk[1]


def _group_hit(t: TableResult, row: int) -> tuple[dict, str]:
    chunk = next(c for c in _table_chunks(t, "3. שווי", 1200) if c[0] == "table" and f"דירה {row} |" in c[1])
    return {"version_id": VID, "kind": "table", "table_index": t.index}, chunk[1]


# --- the search hit: the notes flag, marks only ----------------------------------------------------------------------------------------

def test_a_row_chunk_hit_of_a_long_table_shows_the_notes_flag_with_the_marks_and_not_the_text():
    t = _table([STAR, ONE])
    ws = _ws(t)
    hit, passage = _row_hit(t, MARKED_ROW)
    assert "(*)" not in "".join(c[1] for c in _table_chunks(t, "3. שווי", 1200) if c[0] == "table_row" and c[2] != MARKED_ROW)
    assert STAR.split(") ")[1] not in passage  # the row chunk has no note: that is the gap
    notes, flag = T._hit_notes(ws, None, hit, passage, "T1")
    assert notes == [STAR]  # the (1) note is another row's
    assert "(*)" in flag and "(1)" not in flag and "באלפי" not in flag and "T1" in flag


def test_a_marked_note_does_not_reach_the_rows_without_the_mark():
    t = _table([STAR, ONE])
    ws = _ws(t)
    hit, passage = _row_hit(t, 30)  # carries no mark
    assert T._hit_notes(ws, None, hit, passage, "T1") == ([], "")
    hit, passage = _row_hit(t, OTHER_MARKED_ROW)
    notes, flag = T._hit_notes(ws, None, hit, passage, "T1")
    assert notes == [ONE] and "(1)" in flag and "(*)" not in flag


def test_an_unmarked_note_belongs_to_every_row_of_its_table():
    t = _table([STAR, PLAIN])
    ws = _ws(t)
    hit, passage = _row_hit(t, 30)
    notes, flag = T._hit_notes(ws, None, hit, passage, "T1")
    assert notes == [PLAIN] and flag and "(*)" not in flag
    hit, passage = _row_hit(t, MARKED_ROW)
    assert T._hit_notes(ws, None, hit, passage, "T1")[0] == [STAR, PLAIN]


def test_a_mark_in_a_header_cell_belongs_to_every_row():
    t = _table([STAR], marks={})
    t.headers = ["תיאור", "מחיר (₪) (*)"]
    ws = _ws(t)
    hit, passage = _row_hit(t, 7)
    assert T._hit_notes(ws, None, hit, passage, "T1")[0] == [STAR]


def test_a_mark_nobody_carries_is_the_note_of_the_table():
    t = _table([STAR], marks={})
    ws = _ws(t)
    hit, passage = _row_hit(t, 7)
    assert T._hit_notes(ws, None, hit, passage, "T1")[0] == [STAR]  # as items._note_of_row keeps a mark nobody carries


def test_a_table_chunk_hit_gets_the_notes_of_the_rows_it_holds_and_not_those_it_already_has():
    t = _table([STAR, ONE])
    ws = _ws(t)
    chunks = [c for c in _table_chunks(t, "3. שווי", 1200) if c[0] == "table"]
    assert len(chunks) >= 2
    hit = {"version_id": VID, "kind": "table", "table_index": 0}
    first_notes, first_flag = T._hit_notes(ws, None, hit, chunks[0][1], "T1")
    assert first_notes == [STAR] and "(*)" in first_flag and "(1)" not in first_flag  # the rows 0-… hold the (*) row only
    last_notes, last_flag = T._hit_notes(ws, None, hit, chunks[-1][1], "T1")
    assert STAR not in last_notes and ONE not in last_notes and last_flag == ""  # the last chunk holds the notes in its text


def test_a_hit_outside_a_table_gets_no_table_notes_and_reads_no_structure():
    ws = _ws(_table([STAR]))
    paragraph = {"version_id": VID, "kind": "text", "table_index": None}
    assert T._hit_notes(ws, None, paragraph, "שווי הנכס נקבע לפי גישת ההשוואה (*)", "") == ([], "")
    assert T._hit_notes(ws, None, {"version_id": VID, "kind": "text", "table_index": 0}, "טקסט", "") == ([], "")


def test_a_note_of_another_table_never_applies():
    first, second = _table([STAR], index=0), _table([], index=1, marks={5: "(*)"})
    ws = _ws(first, second)
    hit, passage = _row_hit(second, 5)  # the second table carries the mark; its note is the first table's
    assert T._hit_notes(ws, None, hit, passage, "T2") == ([], "")
    assert T._notes_of_rows(ws.tables[(str(VID), 1)][0], [5]) == []


def test_a_candidate_linked_part_does_not_take_the_notes_of_the_other_part():
    first = _table([PLAIN, STAR], index=0)
    later = _table([], index=1, candidate_of=0, marks={})
    ws = _ws(first, later)
    hit, passage = _row_hit(later, 3)
    assert T._hit_notes(ws, None, hit, passage, "T2") == ([], "")
    st = ws.tables[(str(VID), 1)][0]
    cell = T._cell_of(SimpleNamespace(sid="S1", version_id=VID, table_index=1), passage, {"row_number": 4, "column_number": 2, "number": "1,003"}, st, 1)
    assert cell["scale"] == 1


# --- take_value: the cell path and the quote path read the same notes ------------------------------------------------------------------

def _thousands_table() -> TableResult:
    t = _table([STAR], rows=90, marks={MARKED_ROW: "(*)"})
    t.page_end = 1
    for r in t.rows:
        r.page = 1
    return t


def test_the_quote_path_and_the_cell_path_give_the_same_scale_for_the_same_cell():
    t = _thousands_table()
    st = _structure(t)
    hit, passage = _row_hit(t, MARKED_ROW)
    src = SimpleNamespace(sid="S1", version_id=VID, table_index=0, row_index=MARKED_ROW, kind="table_row")
    cell = T._cell_of(src, passage, {"row_number": MARKED_ROW + 1, "column_number": 2, "number": "1020"}, st, 0)
    assert cell["scale"] == 1000
    quote = T._take_quote(src, passage, {"quote": "מחיר (₪): 1020 (*)", "number": "1020"}, None, None,
                          table_notes=T._notes_of_rows(st, [MARKED_ROW]))
    assert quote["scale"] == cell["scale"] == 1000 and quote["units"] == cell["units"] == {"ILS"}


def test_the_cell_path_reads_the_notes_of_its_row_only():
    t = _thousands_table()
    st = _structure(t)
    hit, passage = _row_hit(t, MARKED_ROW)
    src = SimpleNamespace(sid="S1", version_id=VID, table_index=0)
    marked = T._cell_of(src, passage, {"row_number": MARKED_ROW + 1, "column_number": 2, "number": "1020"}, st, 0)
    other_hit, other_passage = _row_hit(t, 30)
    other = T._cell_of(src, other_passage, {"row_number": 31, "column_number": 2, "number": "1030"}, st, 0)
    assert marked["scale"] == 1000 and other["scale"] == 1  # the thousands note is the marked row's


def test_a_paragraph_quote_takes_no_table_notes():
    ws = _ws(_thousands_table())
    paragraph = SimpleNamespace(sid="S1", version_id=VID, table_index=None, row_index=None, kind="text", text="העלות הכוללת היא 1,020 ₪.")
    assert T._quote_table_notes(ws, None, paragraph, {"quote": "העלות הכוללת היא 1,020 ₪"}) == []
    got = T._take_quote(paragraph, paragraph.text, {"quote": "העלות הכוללת היא 1,020 ₪", "number": "1,020"}, None, None,
                        table_notes=T._quote_table_notes(ws, None, paragraph, {"quote": "העלות הכוללת היא 1,020 ₪"}))
    assert got["scale"] == 1


def test_a_table_quote_reads_the_notes_of_the_rows_that_hold_it_from_the_stored_structure():
    t = _thousands_table()
    ws = _ws(t)
    hit, passage = _row_hit(t, MARKED_ROW)
    src = SimpleNamespace(sid="S1", version_id=VID, table_index=0, row_index=MARKED_ROW, kind="table_row")
    assert T._quote_table_notes(ws, None, src, {"quote": "מחיר (₪): 1020 (*)"}) == [STAR]
    other_hit, other_passage = _row_hit(t, 30)
    other = SimpleNamespace(sid="S2", version_id=VID, table_index=0, row_index=30, kind="table_row")
    assert T._quote_table_notes(ws, None, other, {"quote": "מחיר (₪): 1030"}) == []
    # a table chunk: the rows the quote is in decide, and a quote that is in no unique row takes only what it carries itself
    group_hit, group = _group_hit(t, MARKED_ROW)
    chunk = SimpleNamespace(sid="S3", version_id=VID, table_index=0, row_index=None, kind="table")
    assert T._quote_table_notes(ws, None, chunk, {"quote": "דירה 20 | 1020 (*)"}) == [STAR]
    assert T._quote_table_notes(ws, None, chunk, {"quote": "דירה 21 | 1021"}) == []


# --- the consumers without the database: condition spans and the scope reading ----------------------------------------------------------

def _source(kind: str, text: str, notes: list[str], table_index: int | None = 0) -> T.Source:
    return T.Source(sid="S1", document_id=uuid.uuid4(), version_id=VID, title="", section="3. שווי", location="", kind=kind, text=text,
                    table_index=table_index, table_notes=list(notes))


def test_the_context_of_a_row_chunk_source_holds_its_completed_notes_as_notes_of_its_table():
    t = _table([STAR])
    hit, passage = _row_hit(t, MARKED_ROW)
    parts, body = verify.context_of_source(_source("table_row", passage, [STAR]))
    assert [(p.place, p.text, p.completed) for p in parts if p.place == "note"] == [("note", STAR, True)]
    assert list(body) == [passage]


def test_the_context_of_a_paragraph_source_never_takes_table_notes():
    parts, _ = verify.context_of_source(_source("text", "שווי הנכס הוא 1,020 (*)", [STAR], table_index=None))
    assert not any(p.text == STAR for p in parts)


def test_a_completed_note_that_the_text_already_holds_is_not_added_twice():
    t = _table([STAR])
    chunk = [c for c in _table_chunks(t, "3. שווי", 1200) if c[0] == "table"][-1][1]
    parts, _ = verify.context_of_source(_source("table", chunk, [STAR]))
    assert [p.text for p in parts if STAR.split(") ")[1] in p.text] == [STAR]


def test_the_scope_reading_finds_a_phrase_a_completed_note_states():
    src = _source("table_row", "להלן טבלת שווי: תיאור: דירה 20 | מחיר (₪): 1020 (*)", [STAR])
    assert verify._scope_standing("באלפי ₪", src) == "ok"
    assert verify._scope_standing("באלפי ₪", _source("table_row", src.text, [])) == "absent"


def test_a_completed_note_is_no_item_candidate():
    src = _source("table", "תיאור | מחיר (₪)\nדירה 20 | 1020 (*)", ["(1) התשואה 7% בשנה"])
    raws, _ = items._raws(src)
    assert all("התשואה" not in r.quote for r in raws)


# --- the search hit as the agent reads it (database) ----------------------------------------------------------------------------------

PATH = ["3. שווי"]


def _stored(t: TableResult) -> dict:
    return _structure(t) | {"section": PATH[0], "block_index": 1}


@pytest.mark.db
def test_a_search_hit_on_a_row_chunk_shows_the_notes_flag_to_the_agent_and_keeps_the_text_the_verifier_reads(office, monkeypatch):  # noqa: F811
    t = _table([STAR, ONE])
    lines = [" | ".join(HEADERS), *(" | ".join(r.cells) for r in t.rows)]
    doc, ver = add_laid_out(office, [("heading", PATH, PATH[0], None), ("table", PATH, "\n".join(lines), 0)], {0: _stored(t)}, sha="f1" * 32)
    row_chunk = next(c[1] for c in _table_chunks(t, PATH[0], 1200) if c[0] == "table_row" and c[2] == MARKED_ROW)
    _searching(monkeypatch, _hit_of(office, doc, ver, kind="table_row", body=row_chunk, first=1, last=1, table_index=0, row_index=MARKED_ROW))
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_search(ws, "דירה 20", [str(doc)])
    src = ws.sources["S1"]
    assert "לטבלה יש הערות: (*)" in out and "(1)" not in out.split("לטבלה יש הערות")[1].split("]")[0] and "באלפי" not in out
    assert "לטבלה יש הערות" not in src.text and src.table_notes == [STAR]  # the flag is for the agent; the text is what the claims are checked against
    _searching(monkeypatch, _hit_of(office, doc, ver, kind="table_row", body=row_chunk.replace("20", "30"), first=2, last=2, table_index=0, row_index=30))
    plain = T.tool_search(T.Workspace(ctx=office.ctx()), "דירה 30", [str(doc)])
    assert "לטבלה יש הערות" not in plain
