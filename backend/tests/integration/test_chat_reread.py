"""The database fetcher of the bounded re-read (round 8 U5, KTD5; R13-R15, R24): the context a claim lacked — the heading
chain, the paragraphs around the cited blocks, the rest of the section, a table's headers, units and notes, the notes
of the page, a table continued on the next page, the stored reading of a picture's region — is read from the stored
document through the reader, under the user's own permissions, registered as a turn source (with the cited anchor) when
the verification uses it, and never reaches a document the user cannot see. It reads stored text only: no model call is
made. Synthetic documents in one office; the judge is scripted."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.rereader import ContextReader
from app.chat.verify import verify_answer
from app.db import tenant_tx
from app.extraction.images import PictureReading, PictureTable
from app.providers.llm import Purpose
from tests.factories import make_document, make_group, make_office, make_user
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

TITLE = "שומה - הדקל 5 גבעת הארז"
NOTE = "כל הסכומים בטבלאות הסעיף באלפי ₪"


@pytest.fixture
def office(db):
    return make_office(db, "משרד ב", "admin-b@example.test")


def _blocks(office, doc, ver, rows: list[dict]) -> None:
    """Blocks in reading order: {"kind", "path": [section path], "text", optional "page", "table_index"}."""
    with tenant_tx(office.system) as conn:
        for i, r in enumerate(rows):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, paragraph_no, page, table_index, text) VALUES (app_office(), :d, :v, :b, :k, :s,"
                " :sp, :p, :pg, :ti, :t)"),
                {"d": doc, "v": ver, "b": i, "k": r.get("kind", "paragraph"), "s": (r["path"] or [None])[-1],
                 "sp": r["path"], "p": i + 1, "pg": r.get("page"), "ti": r.get("table_index"), "t": r["text"]})


def _table(office, doc, ver, index: int, structure: dict) -> None:
    pages = [r["page"] for r in structure.get("rows", []) if r.get("page")]
    with tenant_tx(office.system) as conn:
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, :i, :a, :b, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "i": index, "a": min(pages) if pages else None,
                      "b": max(pages) if pages else None, "s": json.dumps(structure, ensure_ascii=False)})


def _ws(ctx, doc, ver, **kw) -> T.Workspace:
    ws = T.Workspace(ctx=ctx)
    kw = {"title": TITLE, "section": "נכסים", "location": "סעיף \"נכסים\"", "kind": "text", "text": "x", **kw}
    ws.add_source(document_id=doc, version_id=ver, **kw)
    return ws


def _count(office, table: str) -> int:
    with tenant_tx(office.system) as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


@pytest.fixture
def report(office):
    """A document with a section whose heading carries the scale of the table below it, and a table that continues on
    the next page."""
    doc, ver = make_document(office, office.default_group_id, TITLE, sha="a" * 64)
    path = [f"טבלת נכסים ({NOTE})"]
    _blocks(office, doc, ver, [
        {"kind": "heading", "path": path, "text": path[0], "page": 3},
        {"path": path, "text": "הטבלה שלהלן מפרטת את הנכסים בקבוצה.", "page": 3},
        {"path": path, "text": "סך כל הנכסים בקבוצה: 25,742", "page": 3},
        {"path": path, "text": "הערה: הנתונים נכונים למועד הקובע.", "page": 3},
        {"path": path, "text": "(1) הסכומים אינם כוללים מע\"מ.", "page": 3},
        {"kind": "heading", "path": ["סעיף אחר"], "text": "סעיף אחר", "page": 4},
        {"path": ["סעיף אחר"], "text": "טקסט מסעיף אחר לגמרי.", "page": 4}])
    _table(office, doc, ver, 0, {
        "caption": "להלן נתוני הנכסים", "title": ["נכסי הקבוצה"], "headers": ["נכס", "שווי"], "units": [None, "אלפי ₪"],
        "source": "text", "block_index": 2,
        "rows": [{"page": 3, "cells": ["נכס א", "10,000"]}, {"page": 3, "cells": ["נכס ב", "7,000"]},
                 {"page": 4, "cells": ["נכס ג", "8,742"]}, {"page": 4, "cells": ["סה\"כ", "25,742"]}],
        "notes": ["(*) לא כולל מע\"מ."]})
    return doc, ver


# --- what each kind reads --------------------------------------------------------------------------------------------

def test_heading_gives_the_chain_of_the_cited_block_and_is_registered_with_the_cited_anchor(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    fetcher = ContextReader(ws)
    got = fetcher.expand("S1", "heading")
    assert got is not None and NOTE in got.text and "מעל המקור" in got.text
    assert len(ws.sources) == 1  # reading is not registering
    sid = fetcher.adopt("S1", "heading")
    assert sid == "S2" and fetcher.adopt("S1", "heading") == "S2"  # once
    s = ws.sources[sid]
    assert (s.block_start, s.block_end, s.document_id, s.version_id) == (2, 2, doc, ver)  # the cited place
    assert ws.anchors[sid]["block_start"] == 2 and NOTE in s.text


def test_neighbours_are_the_blocks_around_the_cited_range(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    got = ContextReader(ws).expand("S1", "neighbours")
    assert got is not None and "הטבלה שלהלן מפרטת" in got.text and "הנתונים נכונים למועד הקובע" in got.text


def test_continuation_is_the_rest_of_the_section_not_another_section(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="הטבלה שלהלן מפרטת את הנכסים בקבוצה.", block_start=1, block_end=1)
    fetcher = ContextReader(ws)
    got = fetcher.expand("S1", "continuation")
    assert got is not None and "(1) הסכומים אינם כוללים" in got.text and "סעיף אחר" not in got.text
    s = ws.sources[fetcher.adopt("S1", "continuation")]
    assert s.block_start == 1 and s.block_end == 4  # from the cited block to the last one read


def test_preceding_is_the_blocks_before_the_source_in_its_section_in_order(office, report):
    """Round 13: where a section states what a passage cited from further down only uses."""
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="(1) הסכומים אינם כוללים מע\"מ.", block_start=4, block_end=4)
    fetcher = ContextReader(ws)
    got = fetcher.expand("S1", "preceding")
    assert got is not None and "סעיף אחר" not in got.text and "הסכומים אינם כוללים" not in got.text
    lines = got.text.splitlines()
    assert lines[0].startswith("טבלת נכסים") and lines[-1] == "הערה: הנתונים נכונים למועד הקובע."  # document order
    assert len(lines) == 4  # the section's blocks 0-3, none of the source or after it
    s = ws.sources[fetcher.adopt("S1", "preceding")]
    assert (s.block_start, s.block_end) == (0, 3) and "תחילת הסעיף שלפני המקור" in s.location


def test_preceding_keeps_the_nearest_blocks_within_the_bound(office, report, monkeypatch):
    from app.chat import rereader

    doc, ver = report
    monkeypatch.setattr(rereader, "REREAD_CHARS", 90)
    ws = _ws(office.ctx(), doc, ver, text="(1) הסכומים אינם כוללים מע\"מ.", block_start=4, block_end=4)
    fetcher = ContextReader(ws)
    got = fetcher.expand("S1", "preceding")
    assert got is not None and "הערה: הנתונים נכונים למועד הקובע." in got.text  # the nearest block stays
    assert "טבלת נכסים" not in got.text and len(got.text) <= 90 + 20  # the farthest are left out
    s = ws.sources[fetcher.adopt("S1", "preceding")]
    assert (s.block_start, s.block_end) == (2, 3)


def test_a_source_at_the_start_of_its_section_has_nothing_preceding(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סעיף אחר", block_start=5, block_end=5)
    assert ContextReader(ws).expand("S1", "preceding") is None  # the first block of the second section: not the first's
    ws = _ws(office.ctx(), doc, ver, text="טבלת נכסים", block_start=0, block_end=0)
    assert ContextReader(ws).expand("S1", "preceding") is None
    ws = _ws(office.ctx(), doc, ver, text="x")  # a search hit's chunk: no place in the document
    assert ContextReader(ws).expand("S1", "preceding") is None


def test_the_end_of_a_section_has_no_continuation(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="(1) הסכומים אינם כוללים מע\"מ.", block_start=4, block_end=4)
    assert ContextReader(ws).expand("S1", "continuation") is None


def test_a_tables_headers_units_and_notes(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס ב | 7,000", table_index=0, block_start=2, block_end=2)
    fetcher = ContextReader(ws)
    for kind in ("table_headers", "units_note"):
        got = fetcher.expand("S1", kind)
        assert got is not None, kind
        for part in ("להלן נתוני הנכסים", "כותרות העמודות: נכס | שווי", "שווי: אלפי ₪", "(*) לא כולל מע\"מ", NOTE):
            assert part in got.text, (kind, part)
        assert "נכס ג" not in got.text  # the headers and notes, not the rows


def test_a_passages_units_note_is_the_heading_and_the_paragraphs_around_it(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    got = ContextReader(ws).expand("S1", "units_note")
    assert got is not None and NOTE in got.text and "הטבלה שלהלן מפרטת" in got.text


def test_footnotes_are_the_notes_of_the_table_and_of_the_page(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס ב | 7,000", table_index=0, block_start=2, block_end=2,
             page_list=[3])
    got = ContextReader(ws).expand("S1", "footnotes")
    assert got is not None and "(*) לא כולל מע\"מ" in got.text and "(1) הסכומים אינם כוללים מע\"מ" in got.text
    assert "הנתונים נכונים" not in got.text  # a note's start marks it, not any paragraph of the page


def test_the_section_lead_is_its_first_paragraphs_not_a_sub_section_or_another_section(office, report):
    """Round 13 U10 (KTD11, R14): what a section opens with, as stored text."""
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    got = ContextReader(ws).expand("S1", "section_lead")
    assert got is not None and "הטבלה שלהלן מפרטת" in got.text and "טקסט מסעיף אחר" not in got.text
    assert len(ws.sources) == 1  # reading is not registering
    # the judge's "heading" reads the chain and the lead-in under it
    heading = ContextReader(ws).expand("S1", "heading")
    assert heading is not None and NOTE in heading.text and "הטבלה שלהלן מפרטת" in heading.text


def test_the_section_notes_are_the_lines_that_start_as_notes_wherever_in_the_section(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="הטבלה שלהלן מפרטת את הנכסים בקבוצה.", block_start=1, block_end=1)
    got = ContextReader(ws).expand("S1", "section_notes")
    assert got is not None
    assert "הערה: הנתונים נכונים למועד הקובע" in got.text and "(1) הסכומים אינם כוללים מע\"מ" in got.text
    assert "סך כל הנכסים" not in got.text and "טקסט מסעיף אחר" not in got.text


def test_a_source_with_no_place_in_the_document_has_no_section_to_read(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742")  # a search hit's chunk: no block range
    reader = ContextReader(ws)
    assert reader.expand("S1", "section_lead") is None and reader.expand("S1", "section_notes") is None


def test_a_vat_status_the_stored_tables_notes_state_is_kept_with_its_anchor_instead_of_being_edited_out(office, report):
    """The real defect (round 13 U10, AE6): the table's notes state the VAT basis, and the figure's qualifier was edited away
    as unstated. The notes are read from the stored table before the edit, and cited with the table's anchor."""
    from decimal import Decimal

    from app.chat import calc

    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס ב | 7,000", table_index=0, block_start=2, block_end=2,
             page_list=[3])
    ws.values["V1"] = calc.Value("V1", Decimal(7000), "7,000", "S1", doc, ver, None, "שומת הדגמה", "טבלה", "שווי נכס ב",
                                 "value", "ILS", "none", "excluded", "", "נכס הדגמה", "appraiser_determination",
                                 {"unit": "source", "vat": "model_asserted", "area_basis": "source", "period": "source"},
                                 {"quote": "נכס ב | 7,000"}, "נכס ב | 7,000")
    md = "שווי נכס ב הוא 7,000, לא כולל מע\"מ [V1]."
    usage: list = []
    rep = verify_answer(_judge_supported(), _answer(md), ws, "?", usage, reread=ContextReader(ws))
    assert rep.qualifier_edits == {} and rep.removed_units() == set() and rep.counts()["edited"] == 0
    (cite,) = [p.cite for p in rep.problems if p.kind == "needs_citation"]
    s = ws.sources[cite]
    assert "(*) לא כולל מע\"מ" in s.text and (s.table_index, s.block_start, s.document_id) == (0, 2, doc)
    assert ws.anchors[cite]["table_index"] == 0
    assert "לא כולל מע\"מ" in rep.apply(_answer(md)).answer_markdown
    assert [u["purpose"] for u in usage] == ["verify"]  # no model call for the read


def test_next_page_is_the_stored_table_with_its_continuation(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס ב | 7,000", table_index=0, block_start=2, block_end=2)
    fetcher = ContextReader(ws)
    got = fetcher.expand("S1", "next_page")
    assert got is not None
    assert "[עמוד 3]" in got.text and "[עמוד 4]" in got.text and "סה\"כ | 25,742" in got.text
    s = ws.sources[fetcher.adopt("S1", "next_page")]
    assert s.kind == "table" and s.table_index == 0  # the same table, not another


def test_a_table_on_one_page_has_no_next_page(office):
    doc, ver = make_document(office, office.default_group_id, TITLE, sha="b" * 64)
    _blocks(office, doc, ver, [{"path": ["נכסים"], "text": "x", "table_index": 0, "page": 3}])
    _table(office, doc, ver, 0, {"headers": ["נכס"], "source": "text", "block_index": 0,
                                 "rows": [{"page": 3, "cells": ["נכס א"]}]})
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס א", table_index=0, block_start=0,
             block_end=0)
    assert ContextReader(ws).expand("S1", "next_page") is None


def test_a_passages_next_page_is_the_start_of_the_next_page(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="(1) הסכומים אינם כוללים מע\"מ.", block_start=4, block_end=4, page_list=[3])
    got = ContextReader(ws).expand("S1", "next_page")
    assert got is not None and "טקסט מסעיף אחר" in got.text


def test_region_is_the_stored_reading_of_a_pictures_table(office):
    doc, ver = make_document(office, office.default_group_id, TITLE, sha="c" * 64)
    _blocks(office, doc, ver, [{"kind": "image", "path": ["נספח"], "text": "", "table_index": 0, "page": 6}])
    _table(office, doc, ver, 0, {"headers": ["פריט", "שכירות"], "source": "vision", "block_index": 0,
                                 "caption": "טבלה מתוך תמונה", "notes": ["הערה: השכירות לחודש"],
                                 "rows": [{"page": 6, "cells": ["חנות", "55"]}]})
    ws = _ws(office.ctx(), doc, ver, kind="table", text="חנות | 55", table_index=0, block_start=0, block_end=0)
    got = ContextReader(ws).expand("S1", "region")
    assert got is not None and "קריאה חזותית" in got.text and "השכירות לחודש" in got.text and "חנות | 55" in got.text


def test_region_of_an_inspected_picture_is_its_stored_reading_and_makes_no_model_call(office):
    doc, ver = make_document(office, office.default_group_id, TITLE, sha="d" * 64)
    reading = PictureReading("read_uncertain", "vision", tables=[PictureTable(
        ["פריט", "שכירות"], [["מחסן", "31"]], title=["שכירות באלפי ₪"], notes=["הערה: ללא מע\"מ"])])
    with tenant_tx(office.system) as conn:
        rid = conn.execute(text("SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v"),
                           {"v": ver}).scalar_one()
        conn.execute(text(
            "INSERT INTO region_readings (office_id, document_id, version_id, reading_id, region, reader_version,"
            " model_config, page, status, reading) VALUES (app_office(), :d, :v, :r, 'page:6', 'rv', 'cfg', 6,"
            " 'read_uncertain', CAST(:x AS jsonb))"),
            {"d": doc, "v": ver, "r": rid or "", "x": reading.to_json()})
    ws = _ws(office.ctx(), doc, ver, kind="image", text="מחסן 31", method="vision",
             vision={"region": "page:6", "page": 6, "reading": PictureReading("read_uncertain", "vision"), "tables": []})
    before = _count(office, "region_readings")
    got = ContextReader(ws).expand("S1", "region")
    assert got is not None and "שכירות באלפי ₪" in got.text and "ללא מע\"מ" in got.text  # the stored one
    assert _count(office, "region_readings") == before and ws.inspections == 0


def test_a_text_passage_has_no_region_and_nothing_unknown_is_read(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    fetcher = ContextReader(ws)
    assert fetcher.expand("S1", "region") is None and fetcher.expand("S1", "nonsense") is None
    assert fetcher.expand("S9", "heading") is None and fetcher.adopt("S1", "region") is None


def test_a_listing_has_nothing_to_read(office, report):
    ws = T.Workspace(ctx=office.ctx())
    ws.add_source(document_id=None, version_id=None, title="רשימה", section=None, location="", kind="listing", text="x")
    assert ContextReader(ws).expand("S1", "heading") is None


# --- permissions (R24) ----------------------------------------------------------------------------------------------

def _employee(office, group):
    user = make_user(office, "emp-b@example.test", [group])
    return office.ctx("employee", user)


def test_it_never_reads_a_document_the_user_cannot_see(office):
    seen_group, hidden_group = make_group(office, "קבוצה גלויה"), make_group(office, "קבוצה חסויה")
    ctx = _employee(office, seen_group)
    secret_doc, secret_ver = make_document(office, hidden_group, "מסמך חסוי", sha="e" * 64)
    open_doc, open_ver = make_document(office, seen_group, "מסמך פתוח", sha="f" * 64)
    for doc, ver, marker in ((secret_doc, secret_ver, "סוד-האלון-8812"), (open_doc, open_ver, "גלוי-הדקל-1")):
        _blocks(office, doc, ver, [{"kind": "heading", "path": [f"{marker} (באלפי ₪)"], "text": marker, "page": 1},
                                   {"path": [f"{marker} (באלפי ₪)"], "text": "פסקה", "page": 1}])
        _table(office, doc, ver, 0, {"caption": marker, "headers": ["א"], "source": "text", "block_index": 1,
                                     "rows": [{"page": 1, "cells": ["1"]}, {"page": 2, "cells": ["2"]}]})
    for kind in ("heading", "neighbours", "continuation", "table_headers", "units_note", "footnotes", "next_page"):
        # the revoked one: a source the turn held before the document moved out of the user's groups
        ws = _ws(ctx, secret_doc, secret_ver, kind="table", text="1", table_index=0, block_start=1, block_end=1,
                 page_list=[1])
        fetcher = ContextReader(ws)
        assert fetcher.expand("S1", kind) is None, kind
        assert fetcher.adopt("S1", kind) is None and len(ws.sources) == 1
        # the same reader does read the visible one
        ok = _ws(ctx, open_doc, open_ver, kind="table", text="1", table_index=0, block_start=1, block_end=1,
                 page_list=[1])
        got = ContextReader(ok).expand("S1", kind)
        assert got is None or "סוד-האלון" not in got.text, kind


def test_a_document_of_another_office_or_a_deleted_one_is_never_read(office, db):
    other = make_office(db, "משרד אחר", "admin-other@example.test")
    doc, ver = make_document(other, other.default_group_id, "מסמך של משרד אחר", sha="1" * 64)
    _blocks(other, doc, ver, [{"kind": "heading", "path": ["כותרת-זרה (באלפי ₪)"], "text": "כותרת", "page": 1},
                              {"path": ["כותרת-זרה (באלפי ₪)"], "text": "פסקה", "page": 1}])
    ws = _ws(office.ctx(), doc, ver, text="פסקה", block_start=1, block_end=1)
    assert ContextReader(ws).expand("S1", "heading") is None  # row-level security: another office's version is absent
    mine, mine_ver = make_document(office, office.default_group_id, TITLE, sha="2" * 64)
    _blocks(office, mine, mine_ver, [{"kind": "heading", "path": ["כותרת (באלפי ₪)"], "text": "כותרת"},
                                     {"path": ["כותרת (באלפי ₪)"], "text": "פסקה"}])
    ws = _ws(office.ctx(), mine, mine_ver, text="פסקה", block_start=1, block_end=1)
    assert ContextReader(ws).expand("S1", "heading") is not None
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": mine})
    assert ContextReader(ws).expand("S1", "heading") is None


def test_a_document_read_again_since_is_not_expanded(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    ws.sources["S1"].reading_id = "an-earlier-reading"  # the block numbers of that reading mean other text now
    assert ContextReader(ws).expand("S1", "heading") is None


# --- in a verification -----------------------------------------------------------------------------------------------

def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def test_a_claim_whose_units_are_in_the_heading_is_supported_after_a_database_re_read(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, text="סך כל הנכסים בקבוצה: 25,742", block_start=2, block_end=2)
    seen: list[str] = []

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        out = []
        for n, span in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
            if NOTE in input:
                out.append({"index": int(n), "verdict": "supported", "reason": "בדיקה"})
            else:
                out.append({"index": int(n), "verdict": "partial", "reason": "יחידות לא ידועות", "claims": [
                    {"span": span, "verdict": "partial", "needs_context": "units_note"}]})
        return {"verdicts": out}

    judge = ScriptedProvider().on(Purpose.VERIFY, respond, repeat=True)
    usage: list = []
    md = "סך כל הנכסים בקבוצה הוא 25,742 [S1]."
    rep = verify_answer(judge, _answer(md), ws, "?", usage, reread=ContextReader(ws))
    assert rep.ok and rep.claims[0][0].verdict == "supported"
    assert [u["purpose"] for u in usage] == ["verify", "verify_reread"]
    (log,) = rep.claims[0][0].rereads
    assert (log["source"], log["scope"], log["outcome"], log["expanded_source"]) == ("S1", "units_note", "supported", "S2")
    assert NOTE in ws.sources["S2"].text and NOTE not in seen[0] and NOTE in seen[1]


def _judge_supported() -> ScriptedProvider:
    return ScriptedProvider().on(Purpose.VERIFY, lambda i, input: {"verdicts": [
        {"index": int(n), "verdict": "supported", "reason": "בדיקה"}
        for n in re.findall(r'<unit index="(\d+)"', input)]}, repeat=True)


def test_a_total_only_the_tables_second_page_states_is_kept_and_cited_to_the_expansion(office, report):
    doc, ver = report
    # a search hit on the table: the first page's rows, with the table's index and no place of its own
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס א | 10,000\nנכס ב | 7,000", table_index=0,
             block_start=2, block_end=2, page_list=[3])
    md = "סך הנכסים בטבלה הוא 25,742 [S1]."
    usage: list = []
    rep = verify_answer(_judge_supported(), _answer(md), ws, "?", usage, reread=ContextReader(ws))
    assert rep.ok and not rep.removed_units()
    final = rep.apply(_answer(md)).answer_markdown
    assert "25,742" in final and "[S1][S2]" in final
    s = ws.sources["S2"]
    assert "סה\"כ | 25,742" in s.text and "[עמוד 4]" in s.text
    assert (s.table_index, s.block_start, s.document_id, s.version_id) == (0, 2, doc, ver)
    assert ws.anchors["S2"]["table_index"] == 0 and ws.anchors["S2"]["block_start"] == 2  # the table, where it is read
    (log,) = rep.claims[0][0].rereads
    assert (log["scope"], log["outcome"], log["expanded_source"]) == ("next_page", "found", "S2")
    assert [u["purpose"] for u in usage] == ["verify"]


def test_a_total_no_page_of_the_table_states_is_still_removed(office, report):
    doc, ver = report
    ws = _ws(office.ctx(), doc, ver, kind="table", text="נכס א | 10,000\nנכס ב | 7,000", table_index=0,
             block_start=2, block_end=2, page_list=[3])
    md = "סך הנכסים בטבלה הוא 99,999 [S1]."
    rep = verify_answer(_judge_supported(), _answer(md), ws, "?", [], reread=ContextReader(ws))
    assert rep.removed_units() == {0} and list(ws.sources) == ["S1"]
    assert [x["outcome"] for x in rep.claims[0][0].rereads] == ["not_found"] * 3  # next_page, continuation, preceding
