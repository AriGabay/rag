"""Page-text column binding at the serve path (round 23 U4, M3, KTD8, R5, R16, R19; AE4).

The stored reading is synthetic: a PDF whose page text is a block per row, each with the per-word spans the extraction stores (display frame),
the column header another block above (on the previous page for the continuation). These tests prove what the agent and the judge are shown,
that a quote of a bound row keeps its exact stored word span, and that the replay's rebuilt text is the served text. Every name and figure is
invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import anchors, rereader
from app.chat import tools as T
from app.db import tenant_tx
from tests.factories import make_document, make_office
from tests.integration.test_chat_calculation import meaning, quote, run
from tests.support.replay import rebuild_source_text

pytestmark = pytest.mark.db

SECTION = "5. Statement"
W, H = 600.0, 800.0
QUARTERS = [("Q1", 100, 130), ("Q2", 200, 230), ("Q3", 300, 330)]
STORED_REVERSED = "Revenue 3,300 2,200 1,100"
SERVED_REVERSED = "Revenue — Q1: 1,100; Q2: 2,200; Q3: 3,300"
HEBREW_ROW = "סה״כ 1,100 2,200 3,300"
NO_SPANS_ROW = "Memo 7 8 9"
CONTINUED = "Revenue 6,300 5,200 4,100"
SERVED_CONTINUED = "Revenue — Q1: 4,100; Q2: 5,200; Q3: 6,300"
QUESTION = "What was the revenue?"


def line(words: list[tuple[str, float, float]], top: float):
    out, t = [], ""
    for i, (w, x0, x1) in enumerate(words):
        t += " " if t else ""
        out.append([0, i, len(t), len(t) + len(w), x0, top, x1, top + 10])
        t += w
    return t, out


def row(words, top):
    return ("paragraph",) + line(words, top)


BLOCKS = [  # (page, kind, text, spans)
    (3, "heading", SECTION, None),
    (3, *row(QUARTERS, 100)),
    (3, *row([("Revenue", 20, 70), ("3,300", 300, 340), ("2,200", 200, 240), ("1,100", 100, 140)], 120)),
    (3, *row([("Cost", 20, 50), ("110", 100, 120), ("220", 200, 220), ("330", 300, 320)], 135)),
    (3, *row([("סה״כ", 20, 60), ("1,100", 100, 140), ("2,200", 200, 240), ("3,300", 300, 340)], 150)),
    (3, "paragraph", NO_SPANS_ROW, None),
    (4, *row([("Revenue", 20, 70), ("6,300", 300, 340), ("5,200", 200, 240), ("4,100", 100, 140)], 60)),
]


def add_document(office, *, blocks=BLOCKS) -> tuple[str, object]:
    doc, ver = make_document(office, office.default_group_id, "Synthetic statements", sha="c" * 64)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 4, mime_type = 'application/pdf', filename = 'f.pdf',"
                          " ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for page in (3, 4):
            conn.execute(text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox, cropbox, rotation,"
                " display_width, display_height, printed_label) VALUES (app_office(), :d, :v, :p, '', 'text_layer', 1, true,"
                " CAST(:mb AS jsonb), CAST(:mb AS jsonb), 0, :w, :h, :l)"),
                {"d": doc, "v": ver, "p": page, "mb": json.dumps([0, 0, W, H]), "w": W, "h": H, "l": str(page)})
        for i, (page, kind, t, spans) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section, section_path, page, text,"
                " status, method, bbox, spans) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :pg, :t, 'read', 'text_layer',"
                " CAST(:bb AS jsonb), CAST(:spans AS jsonb))"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": SECTION, "sp": [SECTION], "pg": page, "t": t,
                 "bb": json.dumps([0, 0, W, 20]), "spans": json.dumps(spans) if spans else None})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד ב", "admin-b@example.test")
    a.doc, a.ver = add_document(a)
    return a


def workspace(office) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx())
    ws.user_messages = [{"turn": 1, "text": QUESTION, "current": True}]
    return ws


def read_pages(ws, doc: str, first: int, last: int) -> T.Source:
    out = T.tool_read(ws, {"pages": {"document": doc, "from_page": first, "to_page": last}})
    return ws.sources[re.search(r'<source id="(S\d+)"', out).group(1)]


def test_the_agent_reads_a_reversed_row_in_drawn_order_with_its_column_labels(office):
    src = read_pages(workspace(office), office.doc, 3, 3)
    lines = src.text.split("\n")
    assert SERVED_REVERSED in lines and STORED_REVERSED not in src.text
    assert "Cost — Q1: 110; Q2: 220; Q3: 330" in lines  # a row stored in drawn order is labelled the same way
    assert HEBREW_ROW in lines  # a right-to-left row is served as stored (negative control)
    assert NO_SPANS_ROW in lines  # a row with no word positions is served as stored
    assert "Q1 Q2 Q3" in lines  # the header is served as stored


def test_the_judge_is_shown_the_same_text_the_agent_read(office):
    ws = workspace(office)
    src = read_pages(ws, office.doc, 3, 3)
    with tenant_tx(ws.ctx) as conn:
        head = T.reader.version(conn, src.version_id)
        near = rereader._expand_neighbours(ws, conn, head, src)
    assert near is not None and SERVED_REVERSED in near[0].split("\n") and STORED_REVERSED not in near[0]
    assert HEBREW_ROW in near[0].split("\n") and NO_SPANS_ROW in near[0].split("\n")


def test_a_row_with_no_word_positions_is_served_as_stored_and_the_judge_is_told_its_columns_are_unknown(office):
    ws = workspace(office)
    src = read_pages(ws, office.doc, 3, 3)
    assert T.column_binding_note(ws, src) == T.MSG_COLUMNS_UNKNOWN
    other = read_pages(ws, office.doc, 4, 4)  # a source with no such row carries no note
    assert T.column_binding_note(ws, other) == ""


def test_a_continuation_page_row_binds_to_the_header_of_the_previous_page(office):
    src = read_pages(workspace(office), office.doc, 4, 4)
    assert src.text.split("\n") == [SERVED_CONTINUED]


def test_a_continuation_row_on_a_page_of_another_width_is_served_as_stored(office):
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE pages SET display_width = 700 WHERE version_id = :v AND page_no = 4"), {"v": office.ver})
    ws = workspace(office)
    src = read_pages(ws, office.doc, 4, 4)
    assert src.text.split("\n") == [CONTINUED]
    assert T.column_binding_note(ws, src) == T.MSG_COLUMNS_UNKNOWN


def test_a_quote_of_a_bound_row_keeps_the_exact_stored_word_span_and_the_unbound_values_meaning(office):
    ws = workspace(office)
    src = read_pages(ws, office.doc, 3, 3)
    out = run(ws, "take_value", source=src.sid, locator=quote("Q2: 2,200", "2,200"), meaning=meaning("income", unit="ILS"), label="Q2")
    assert out.startswith("V1 נרשם"), out
    payload = {"values": [ws.values["V1"].public()]}
    anchors.attach(ws, payload)
    a = payload["values"][0]["anchor"]
    # the stored word "2,200" of block 2 (offset 14..19 of "Revenue 3,300 2,200 1,100"), on page 3
    assert a["precision"] == "span" and [p["page"] for p in a["pages"]] == [3], a
    box = a["pages"][0]["focus"][0]
    assert box == [round(200 / W, 4), round(120 / H, 4), round(240 / W, 4), round(130 / H, 4)]
    # the same figure taken from a source that serves the row as stored gives the same scale and unit
    plain = workspace(office)
    stored = read_pages(plain, office.doc, 3, 3)
    plain.sources[stored.sid].text = stored.text.replace(SERVED_REVERSED, "Revenue 3,300 2,200 1,100")
    again = run(plain, "take_value", source=stored.sid, locator=quote("2,200 1,100", "2,200"), meaning=meaning("income", unit="ILS"), label="x")
    assert again.startswith("V1 נרשם"), again
    a_bound, a_plain = ws.values["V1"], plain.values["V1"]
    assert (a_bound.unit, a_bound.scale, a_bound.value) == (a_plain.unit, a_plain.scale, a_plain.value)


def test_a_quote_across_two_cells_of_a_bound_row_anchors_both_stored_words(office):
    ws = workspace(office)
    src = read_pages(ws, office.doc, 3, 3)
    out = run(ws, "take_value", source=src.sid, locator=quote("Q1: 1,100; Q2: 2,200", "1,100"), meaning=meaning("income", unit="ILS"), label="Q1")
    assert out.startswith("V1 נרשם"), out
    payload = {"values": [ws.values["V1"].public()]}
    anchors.attach(ws, payload)
    a = payload["values"][0]["anchor"]
    assert a["precision"] == "span" and len(a["pages"][0]["rects"]) == 2 and len(a["pages"][0]["focus"]) == 1


def test_the_replays_rebuilt_text_of_a_block_range_is_the_served_text(office):
    ws = workspace(office)
    src = read_pages(ws, office.doc, 3, 3)
    locator = {"version_id": str(office.ver), "reading_id": "reading-1", "block_start": src.block_start, "block_end": src.block_end}
    rebuilt = rebuild_source_text(office.ctx(), locator)
    assert SERVED_REVERSED in rebuilt.split("\n") and rebuilt != "\n".join(
        b[2] for b in BLOCKS[src.block_start:src.block_end + 1] if b[2])
    assert rebuilt == src.text


def test_a_search_hit_of_page_text_serves_a_bound_row_the_same_way(office):
    from app.extraction.normalize_text import normalize_for_search
    from app.platform import pipeline

    passage = "\n".join([BLOCKS[1][2], BLOCKS[2][2]])
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section, text, normalized_text,"
            " block_start, block_end) VALUES (app_office(), :d, :v, 0, 'text', :p, :s, :t, :n, 1, 2)"),
            {"d": office.doc, "v": office.ver, "p": [3], "s": SECTION, "t": passage, "n": normalize_for_search(passage)})
    pipeline.embed_stage(office.system, pipeline.VersionInfo(office.ver, office.doc, "k", "application/pdf", None), 1e18)
    ws = workspace(office)
    T.tool_search(ws, "Revenue Q1 Q2 Q3", [office.doc], None)
    hit = next(s for s in ws.sources.values() if s.kind == "text")
    assert SERVED_REVERSED in hit.text.split("\n") and STORED_REVERSED not in hit.text
