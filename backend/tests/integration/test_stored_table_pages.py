"""Round 22 U3 (KTD3, R9): a stored table whose rows span pages records no parts. Whether the caption's scale and notes reach its later
pages is read from the stored blocks above the later rows, minus page furniture: with no separating block they apply to every page, as
before; with one, the later page's cells carry the scale as unconfirmed and a value read from them is conditional. Synthetic documents."""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.chat import reader
from app.chat import tools as T
from app.db import tenant_tx
from tests.factories import make_document, make_office
from tests.integration.test_chat_calculation import cell, handle_of, meaning, run, workspace

pytestmark = pytest.mark.db

TITLE = "דוח הוצאות סינתטי שדרות הגפן 5"
CAPTION = "להלן טבלת הוצאות (באלפי ₪)"
SECTION = "3. הוצאות"
RUNNING = "דוח הוצאות סינתטי - נספח"
W, H = 600, 800


def add_document(office, *, heading_above_later_rows: bool, sha: str) -> tuple[str, object]:
    doc, ver = make_document(office, office.default_group_id, TITLE, sha=sha)
    rows = [{"page": 1, "cells": ["שכירות", "100"], "cell_boxes": [[400, 100, 500, 120], [250, 100, 400, 120]]},
            {"page": 2, "cells": ["אחזקה", "500"], "cell_boxes": [[400, 100, 500, 120], [250, 100, 400, 120]]}]
    structure = {"headers": ["סעיף", "סכום"], "units": [None, None], "caption": CAPTION, "title": [], "notes": [],
                 "section": SECTION, "block_index": 2, "source": "text", "rows": rows}
    blocks = [("heading", SECTION, None, 1, 10), ("paragraph", CAPTION, None, 1, 40), ("table", CAPTION, 0, 1, 100)]
    if heading_above_later_rows:
        blocks.append(("paragraph", "4. פירוט עלויות נוספות", None, 2, 60))
    blocks += [("paragraph", RUNNING, None, page, 5) for page in (2, 3, 4)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 4, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for page in range(1, 5):
            conn.execute(text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox,"
                " cropbox, rotation, display_width, display_height) VALUES (app_office(), :d, :v, :p, :t, 'text_layer', 1,"
                " true, CAST(:mb AS jsonb), CAST(:mb AS jsonb), 0, :w, :h)"),
                {"d": doc, "v": ver, "p": page, "t": f"עמוד {page}", "mb": json.dumps([0, 0, W, H]), "w": W, "h": H})
        for i, (kind, t, table, page, top) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section, section_path,"
                " page, text, status, table_index, bbox) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :pg, :t, 'read',"
                " :ti, CAST(:bb AS jsonb))"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": SECTION, "sp": [SECTION], "pg": page, "t": t, "ti": table,
                 "bb": json.dumps([50, top, 560, top + 20])})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 2, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.plain, a.plain_ver = add_document(a, heading_above_later_rows=False, sha="a" * 64)
    a.split, a.split_ver = add_document(a, heading_above_later_rows=True, sha="b" * 64)
    return a


def _lookup(office, doc_ver):
    with tenant_tx(office.system) as conn:
        st = reader.table_structure(conn, doc_ver, 0)
        block = reader.table_block(conn, doc_ver, 0)
        ws = T.Workspace(ctx=office.ctx())
        return T._later_page_unconfirmed(ws, conn, doc_ver, 0, st, block)


def test_running_header_is_furniture_and_no_block_above_the_later_rows_leaves_every_page_confirmed(office):
    assert _lookup(office, office.plain_ver) == {}


def test_a_paragraph_above_the_later_rows_marks_the_later_page_unconfirmed_and_not_the_running_header(office):
    got = _lookup(office, office.split_ver)
    assert list(got) == [2] and got[2] == "4. פירוט עלויות נוספות"


def _cells(office, doc):
    ws = workspace(office)
    t = handle_of(T.tool_outline(ws, doc), CAPTION)
    s = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"table": t})).group(1)
    for row in ("שכירות", "אחזקה"):
        out = run(ws, "take_value", source=s, locator=cell(row, "סכום"), meaning=meaning("cost", role="other"), label=row)
        assert out.startswith("V"), out
    return ws


def test_with_no_separation_the_caption_scale_applies_to_both_pages(office):
    ws = _cells(office, office.plain)
    assert ws.values["V1"].scale == ws.values["V2"].scale == 1000 and not ws.uncertain_values
    assert ws.values["V2"].value == Decimal("500")


def test_with_a_separation_the_later_cell_is_scaled_unconfirmed_and_a_calculation_with_it_is_conditional(office):
    ws = _cells(office, office.split)
    assert ws.values["V1"].scale == ws.values["V2"].scale == 1000  # never silently scale 1
    assert "V1" not in ws.uncertain_values and "4. פירוט עלויות נוספות" in ws.uncertain_values["V2"]
    out = json.loads(T.tool_calculate(ws, "V1 + V2", "סך הוצאות"))
    assert out["conditional"] is True
