"""Round 22 U4 (KTD4, R7, AE2): one table identity across the paths that show a cell.

A Latin table stored in reversed cell order (the round-19 reading) is stored with its Hebrew context: the stored text of its page.
The same cell must give the same row, column and box on the search hit, the table read and take_value (by cell and by quote), the
citation (``anchors.load`` / ``snapshot``) and the source viewer's block view (``get_blocks``); a table whose page holds Hebrew was
stored for right-to-left reading and is served as stored on every path. Synthetic documents only; the labels and figures are invented."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.chat import anchors, reader
from app.chat import tools as T
from app.db import tenant_tx
from app.extraction import tables as stored_tables
from tests.conftest import login
from tests.factories import make_document, make_office
from tests.integration.test_chat_reading_tools import handle_of

pytestmark = pytest.mark.db

CAPTION = "Quarterly revenue"
W, H = 600.0, 800.0
# stored in reversed cell order: drawn Item | Q1 2023 | Q2 2023, boxes from right to left
STORED = [["Q2 2023", "Q1 2023", "Item"], ["1,500", "1,200", "Rent"], ["310", "300", "Fees"]]
XS = [400.0, 250.0, 100.0]


def _box(row: int, x0: float) -> list[float]:
    return [x0, 100.0 + 20 * row, x0 + 140.0, 120.0 + 20 * row]


def add_table_document(office, page_text: str, sha: str):
    doc, ver = make_document(office, office.default_group_id, "Synthetic revenue report", sha=sha)
    rows = [{"page": 1, "cells": cells, "cell_boxes": [_box(i, x) for x in XS]} for i, cells in enumerate(STORED)]
    structure = {"headers": [], "units": [], "caption": CAPTION, "title": [], "notes": [], "section": "1. Revenue", "block_index": 1,
                 "source": None, "rows": rows}
    block_text = "\n".join(" | ".join(cells) for cells in STORED)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        conn.execute(text(
            "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox, cropbox, rotation,"
            " display_width, display_height) VALUES (app_office(), :d, :v, 1, :t, 'text_layer', 1, true, CAST(:mb AS jsonb),"
            " CAST(:mb AS jsonb), 0, :w, :h)"),
            {"d": doc, "v": ver, "t": page_text, "mb": json.dumps([0, 0, W, H]), "w": W, "h": H})
        for i, (kind, t, table, bbox) in enumerate([("heading", "1. Revenue", None, [50, 10, 560, 30]),
                                                    ("table", block_text, 0, [100, 100, 540, 160])]):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section, section_path, page,"
                " text, status, table_index, bbox) VALUES (app_office(), :d, :v, :b, :k, '1. Revenue', :sp, 1, :t, 'read', :ti,"
                " CAST(:bb AS jsonb))"),
                {"d": doc, "v": ver, "b": i, "k": kind, "sp": ["1. Revenue"], "t": t, "ti": table, "bb": json.dumps(bbox)})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start, page_end,"
                          " structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure)})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.latin_doc, a.latin_ver = add_table_document(a, "Quarterly report. Rent and fees by quarter.", "a" * 64)
    a.hebrew_doc, a.hebrew_ver = add_table_document(a, "דוח רבעוני. שכירות ודמי ניהול לפי רבעון.", "b" * 64)
    return a


def _meaning() -> dict:
    return {"kind": "coefficient", "unit": "ratio", "period": "unknown", "vat": "unknown", "area_basis": "", "subject": "הנכס",
            "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""}


def _frac(box) -> list[float]:
    return [round(box[0] / W, 4), round(box[1] / H, 4), round(box[2] / W, 4), round(box[3] / H, 4)]


def _read_table(office, doc: str) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx())
    ws.user_messages = [{"turn": 1, "text": "How much is the rent?", "current": True}]
    handle = handle_of(T.tool_outline(ws, doc), CAPTION)
    T.tool_read(ws, {"table": handle})
    return ws


def _snapshot(office, doc: str, ver, anchor: dict) -> dict:
    stub = {"document_id": doc, "version_id": str(ver), "reading_id": "reading-1", "kind": "cell",
            "block_start": 1, "block_end": 1, "pages": [1]} | anchor
    with tenant_tx(office.ctx()) as conn:
        readings = anchors.load(conn, [stub])
    return anchors.snapshot(stub, readings[str(ver)], "reading-1")


def test_the_page_context_is_read_from_the_stored_page_text_once_per_table(office):
    with tenant_tx(office.ctx()) as conn:
        latin = reader.table_structure(conn, office.latin_ver, 0)
        hebrew = reader.table_structure(conn, office.hebrew_ver, 0)
        everything = reader.tables_of(conn, office.hebrew_ver)
    assert latin[stored_tables.PAGE_HEBREW] is False and hebrew[stored_tables.PAGE_HEBREW] is True
    assert everything[0][stored_tables.PAGE_HEBREW] is True
    assert stored_tables.serve_direction(latin) == stored_tables.Direction(True, "boxes", True)
    assert stored_tables.serve_direction(hebrew) == stored_tables.Direction(False, "page", False)


def test_the_same_cell_gives_the_same_row_column_and_box_on_the_search_take_value_citation_and_viewer_paths(office, client):
    stored_cell = STORED[1][0]  # «1,500»: Rent in Q2 2023, stored column 0
    box = _box(1, XS[0])
    # 1. a search hit on the row's chunk (the stored chunk text is the stored cells), served in the order the table is drawn
    with tenant_tx(office.ctx()) as conn:
        ws = T.Workspace(ctx=office.ctx())
        hit = {"version_id": office.latin_ver, "kind": "table_row", "table_index": 0, "row_index": 1}
        served = T._served_hit_text(ws, conn, hit, f"{CAPTION}: 1,500 | 1,200 | Rent")
    assert served.endswith("Item: Rent | Q1 2023: 1,200 | Q2 2023: 1,500")
    # 2. take_value by row and column, and by a quote of the served text: one datum with one anchor
    ws = _read_table(office, office.latin_doc)
    T.tool_take_value(ws, "S1", {"row": "Rent", "column": "Q2 2023"}, _meaning(), "Rent Q2")
    by_cell = ws.values["V1"]
    assert (by_cell.locator["row"], by_cell.locator["column"]) == ("Rent", "Q2 2023")
    assert (by_cell.locator["row_number"], by_cell.locator["column_number"], by_cell.locator["column_shown"]) == (2, 1, 3)
    anchor = ws.anchors["V1"]
    assert (anchor["table_index"], anchor["row"], anchor["column"]) == (0, 1, 0)
    by_quote_ws = _read_table(office, office.latin_doc)
    T.tool_take_value(by_quote_ws, "S1", {"quote": "Q2 2023: 1,500", "number": "1,500"}, _meaning(), "Rent Q2")
    assert by_quote_ws.values["V1"].locator == by_cell.locator and by_quote_ws.anchors["V1"] == anchor
    # 3. the citation: the labels the agent was shown, and the stored box of the cell
    snap = _snapshot(office, office.latin_doc, office.latin_ver, {k: anchor[k] for k in ("table_index", "row", "column")})
    assert (snap["table"]["row_label"], snap["table"]["column_header"]) == ("Rent", "Q2 2023")
    assert snap["table"]["row_number"] == by_cell.locator["row_number"] and snap["table"]["column_number"] == by_cell.locator["column_shown"]
    assert snap["precision"] == "cell" and snap["pages"][0]["rects"] == [_frac(box)]
    # 4. the source viewer's block view: the table in the order it is drawn, with the stored column of each shown one
    login(client, "admin-a@example.test")
    shown = client.get(f"/api/documents/{office.latin_doc}/versions/{office.latin_ver}/blocks").json()["blocks"][1]
    table = shown["table"]
    assert table["columns"] == [2, 1, 0] and table["rows"][1] == ["Rent", "1,200", "1,500"]
    position = table["columns"].index(anchor["column"])  # where the cited stored column is shown
    assert (table["rows"][anchor["row"]][0], table["rows"][0][position], table["rows"][anchor["row"]][position]) == \
        (snap["table"]["row_label"], snap["table"]["column_header"], stored_cell)
    assert "Item | Q1 2023 | Q2 2023" in shown["text"] and "Item: Rent | Q1 2023: 1,200 | Q2 2023: 1,500" in shown["text"]


def test_a_table_on_a_hebrew_page_is_served_as_stored_on_every_path(office, client):
    ws = _read_table(office, office.hebrew_doc)
    T.tool_take_value(ws, "S1", {"row_number": 2, "column_number": 1}, _meaning(), "Q2")
    value = ws.values["V1"]
    assert value.locator["column_number"] == 1 and "column_shown" not in value.locator and value.locator["row"] == "1,500"
    anchor = ws.anchors["V1"]
    assert (anchor["row"], anchor["column"]) == (1, 0)
    snap = _snapshot(office, office.hebrew_doc, office.hebrew_ver, {k: anchor[k] for k in ("table_index", "row", "column")})
    assert snap["table"]["row_label"] == value.locator["row"] and "direction" not in snap["table"]
    assert snap["pages"][0]["rects"] == [_frac(_box(1, XS[0]))]
    login(client, "admin-a@example.test")
    table = client.get(f"/api/documents/{office.hebrew_doc}/versions/{office.hebrew_ver}/blocks").json()["blocks"][1]["table"]
    assert table["columns"] is None and table["rows"][1] == STORED[1]
