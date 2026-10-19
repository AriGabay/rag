"""The reading tools (U8): ``read`` by source, pages, section, table or continuation, and an openable ``outline``.

The model is scripted; these tests prove what the server returns and records: every result states whether it is
complete, clipped, or has unread regions, and how to read on; a section is never taken as fully read when only
part of it was returned; blocks already returned are not sent again while verification still sees the full
text; the agent's reads open the same blocks the source panel shows; and permission and the reading a handle was
issued for are checked on every access. Synthetic documents only; the street names are invented."""

from __future__ import annotations

import json
import re
from uuid import UUID

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, final, read

pytestmark = pytest.mark.db

TITLE = "שומה רחוב הערבה 14"
FILLER = "תיאור מפורט של המבנה ושל הסביבה הקרובה אליו. " * 18  # ~830 characters
ROWS = [[f"רחוב הערבה {i}", f"{80 + i}", f"{9000 + 10 * i}"] for i in range(1, 46)]  # 45 rows: two parts

DESCRIPTION = "2. תיאור הנכס"
BUILDING = "2.1 הבניין"


def _blocks() -> list[tuple]:
    """(kind, section path, page, text, status, note, table index) in reading order."""
    out = [("heading", ["1. מבוא"], 1, "1. מבוא", "read", None, None),
           ("paragraph", ["1. מבוא"], 1, "השומה נערכה לבקשת הבעלים.", "read", None, None),
           ("heading", [DESCRIPTION], 2, DESCRIPTION, "read", None, None),
           ("paragraph", [DESCRIPTION], 2, "פסקה 1: שטח המגרש 812 מ\"ר. " + FILLER, "read", None, None)]
    out += [("paragraph", [DESCRIPTION], 2 if n < 4 else 3, f"פסקה {n}: " + FILLER, "read", None, None)
            for n in range(2, 7)]
    out += [("heading", [DESCRIPTION, BUILDING], 3, BUILDING, "read", None, None),
            ("paragraph", [DESCRIPTION, BUILDING], 3, "הבניין בן חמש קומות.", "read", None, None),
            ("heading", ["3. התחשיב"], 4, "3. התחשיב", "read", None, None),
            ("table", ["3. התחשיב"], 4, "טבלת עסקאות השוואה", "read", None, 0),
            ("image", ["3. התחשיב"], 4, "", "unread", "טבלה בתמונה שלא נקראה", None),
            ("paragraph", ["3. התחשיב"], 4, "שווי הנכס 4,350,000 ₪.", "read", None, None)]
    return out


def add_document(office, group=None, sha: str = "a" * 64, reading: str = "reading-1") -> tuple[str, object]:
    doc, ver = make_document(office, group or office.default_group_id, TITLE, sha=sha)
    structure = {"headers": ["כתובת", "שטח במ\"ר", "מחיר למ\"ר"], "caption": "טבלת עסקאות השוואה", "title": [],
                 "notes": ["המחירים ללא מע\"מ."], "section": "3. התחשיב", "block_index": 12,
                 "rows": [{"cells": r} for r in ROWS]}
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 4, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": reading, "partial": True})})
        for i, (kind, path, page, t, status, note, table) in enumerate(_blocks()):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, note, table_index) VALUES (app_office(), :d, :v, :b, :k, :s,"
                " :sp, :pg, :t, :st, :n, :ti)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1], "sp": path, "pg": page, "t": t, "st": status,
                 "n": note, "ti": table})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 4, 4, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.outsider = make_user(a, "outsider@example.test", [a.default_group_id])
    a.doc, a.ver = add_document(a)
    return a


def handle_of(output: str, name: str) -> str:
    """The handle the outline gives an entry (``§3 «2. תיאור הנכס»``)."""
    m = re.search(r"([§T]\d+) «" + re.escape(name) + "»", output)
    assert m, output
    return m.group(1)


def tag(output: str, attr: str) -> str | None:
    m = re.search(rf'<source [^>]*\b{attr}="([^"]*)"', output)
    return m.group(1) if m else None


def section_handle(ws, doc: str, name: str = DESCRIPTION) -> str:
    return handle_of(T.tool_outline(ws, doc), name)


def revoke(office) -> None:
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u AND group_id = :g"),
                     {"u": office.emp, "g": office.private})


# --- outline and completeness ----------------------------------------------------------------------------------

def test_outline_returns_openable_sections_and_tables_with_sizes_and_unread_counts(office):
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_outline(ws, office.doc)
    assert re.search(r"§\d+ «2\. תיאור הנכס» — עמודים 2–3, [\d,]+ תווים", out)
    assert re.search(r"  - §\d+ «2\.1 הבניין»", out)  # a sub-section is its own entry
    assert re.search(r"«3\. התחשיב» — עמוד 4, [\d,]+ תווים, 1 אזורים שלא נקראו", out)
    assert re.search(r"T\d+ «טבלת עסקאות השוואה» — עמוד 4, 45 שורות", out)
    d = re.search(r"\((D\d+);", out).group(1)
    assert ws.handles[d]["document_id"] == office.doc
    assert ws.activity[office.doc]["level"] == "located"


def test_a_long_section_is_read_in_parts_and_the_last_part_says_it_is_complete(office):
    ws = T.Workspace(ctx=office.ctx())
    first = T.tool_read(ws, {"section": section_handle(ws, office.doc)})
    assert tag(first, "status") == "clipped" and re.fullmatch(r"K\d+", tag(first, "more"))
    assert tag(first, "reading_id") == "reading-1" and tag(first, "version_id") == str(office.ver)
    assert "פסקה 1:" in first and "הבניין בן חמש קומות" not in first
    s1 = ws.sources["S1"]
    assert s1.clipped and s1.status == "clipped" and s1.more == tag(first, "more")
    second = T.tool_read(ws, {"cursor": tag(first, "more")})
    assert tag(second, "status") == "complete" and tag(second, "more") is None
    assert "הבניין בן חמש קומות" in second and "פסקה 1:" not in second
    # the parts follow each other: nothing skipped, nothing repeated
    assert ws.sources["S2"].block_start == s1.block_end + 1 and ws.sources["S2"].block_end == 10
    activity = ws.activity[office.doc]
    assert activity["read_complete"] is True


# --- AE3: an absence needs a complete reading --------------------------------------------------------------------

def _absent_after(items_reader):
    """A final answer claiming the plot's frontage is absent from the section read last."""
    def step(items):
        outputs = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]
        sid = re.findall(r'<source id="(S\d+)"', outputs[-1])[-1]
        return final("הבניין בן חמש קומות [" + sid + "].", documents=[items_reader],
                     requested=[{"label": "חזית המגרש", "document_ids": [items_reader],
                                 "status": "section_checked_absent", "checked_where": sid}])
    return step


def _outline_then(doc):
    return lambda items: [read(section=handle_of(_last_output(items), DESCRIPTION))]


def _last_output(items) -> str:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]


def test_ae3_an_absence_after_part_of_a_section_is_read_in_part_and_after_the_end_it_is_accepted(
        client, office, monkeypatch):
    login(client, "admin-a@example.test")
    agent = ScriptedAgent([[call("outline", document=office.doc)], _outline_then(office.doc),
                           _absent_after(office.doc)])
    cloud(monkeypatch, office, agent)
    a = send(client, new_conversation(client), "מה חזית המגרש בערבה 14?")["answer"]
    (r,) = a["requested"]
    assert r["status"] == "source_partial" and r["partial_reason"] == "clipped"
    # stated once by the server, after the answer (round 7 KTD4)
    assert f"\n\n**חזית המגרש** לא נמצא בחלק שנקרא מהסעיף \"{DESCRIPTION}\"" in a["markdown"]
    assert a["status"] == "partial"

    agent = ScriptedAgent([[call("outline", document=office.doc)], _outline_then(office.doc),
                           lambda items: [read(cursor=tag(_last_output(items), "more"))],
                           _absent_after(office.doc)])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    a = send(client, new_conversation(client), "מה חזית המגרש בערבה 14?")["answer"]
    (r,) = a["requested"]
    assert r["status"] == "section_checked_absent" and r["section"] == DESCRIPTION
    assert a["markdown"].endswith(f"\n\n**חזית המגרש** לא מופיע בסעיף \"{DESCRIPTION}\" שנבדק [S2].")
    assert a["searches"] == []  # read to the end without any search


def test_an_absence_in_a_section_with_an_unread_region_is_read_in_part(office):
    from app.chat import coverage
    from app.chat.engine import Requested

    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_read(ws, {"section": section_handle(ws, office.doc, "3. התחשיב")})
    assert tag(out, "status") == "has_unread_regions" and tag(out, "more") is None
    (r,) = coverage.validate_requested(ws, [Requested(component="", label="שווי הקרקע", document_ids=[office.doc],
                                                      status="section_checked_absent", checked_where="S1")])
    assert r["status"] == "source_partial" and r["partial_reason"] == "unread"
    assert "אזורים שלא נקראו" in coverage.absence_sentence(r)


# --- pages and region markers ------------------------------------------------------------------------------------

def test_read_pages_returns_their_blocks_with_the_unread_region_marked_in_place(office):
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_read(ws, {"pages": {"document": office.doc, "from_page": 4, "to_page": 4}})
    assert tag(out, "status") == "has_unread_regions" and tag(out, "location") == "עמוד 4"
    marker = re.search(r"\[אזור שלא נקרא (R\d+)[^\]]*טבלה בתמונה שלא נקראה\]", out)
    assert marker, out
    # in reading order: after the table, before the paragraph that follows the picture
    assert out.index("טבלת עסקאות השוואה") < marker.start() < out.index("שווי הנכס 4,350,000")
    region = ws.handles[marker.group(1)]
    assert region["block_index"] == 13 and region["page"] == 4 and region["status"] == "unread"
    source = ws.sources["S1"]
    assert source.unread_regions == [marker.group(1)] and source.page_list == [4]
    assert "[אזור" not in source.text  # the source's text is the document's, not the marker


def test_read_pages_on_a_partly_read_pdf_marks_its_unread_region(office, monkeypatch):
    from app.platform import pipeline
    from tests.integration.test_documents_api import ingest
    from tests.unit.test_regions import R1

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    doc, _ = ingest(office, R1)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    assert tag(out, "status") in ("has_unread_regions", "clipped")
    assert re.search(r"\[אזור שלא נקרא R\d+", out)
    assert ws.sources["S1"].unread_regions


def test_pages_out_of_range_or_reversed_are_refused(office):
    ws = T.Workspace(ctx=office.ctx())
    with pytest.raises(T.ToolError, match="עמודים"):
        T.tool_read(ws, {"pages": {"document": office.doc, "from_page": 3, "to_page": 2}})
    with pytest.raises(T.ToolError, match="4 עמודים"):
        T.tool_read(ws, {"pages": {"document": office.doc, "from_page": 5, "to_page": 6}})
    with pytest.raises(T.ToolError, match="אחד בדיוק"):
        T.tool_read(ws, {"section": "§1", "cursor": "K1"})


# --- parity with the source panel --------------------------------------------------------------------------------

def test_outline_handles_open_the_blocks_the_source_panel_shows(client, office):
    login(client, "admin-a@example.test")
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_outline(ws, office.doc)
    panel = client.get(f"/api/documents/{office.doc}/versions/{office.ver}/blocks").json()["blocks"]
    for name in ("1. מבוא", DESCRIPTION, BUILDING, "3. התחשיב"):
        before = set(ws.sources)
        result = T.tool_read(ws, {"section": handle_of(out, name)})
        while tag(result, "more"):
            result = T.tool_read(ws, {"cursor": tag(result, "more")})
        parts = [ws.sources[s] for s in ws.sources if s not in before]
        read_blocks = {i for p in parts for i in range(p.block_start, p.block_end + 1)}
        start, end = min(read_blocks), max(read_blocks)
        window = client.get(f"/api/documents/{office.doc}/versions/{office.ver}/blocks",
                            params={"start": start, "end": end}).json()["blocks"]
        in_section = {b["index"] for b in panel if b["section_path"][:len(name.split(" > "))] == [name]
                      or name in b["section_path"]}
        assert read_blocks == {b["index"] for b in window} == in_section
        joined = "\n".join(p.text for p in parts)
        for b in window:
            assert b["text"] in joined
    table = T.tool_read(ws, {"table": handle_of(out, "טבלת עסקאות השוואה")})
    t = ws.sources[re.search(r'<source id="(S\d+)"', table).group(1)]
    (block,) = client.get(f"/api/documents/{office.doc}/versions/{office.ver}/blocks",
                          params={"start": t.block_start, "end": t.block_end}).json()["blocks"]
    assert block["table"]["caption"] == "טבלת עסקאות השוואה" and len(block["table"]["rows"]) == 45


def test_a_table_is_read_forty_rows_at_a_time_with_its_headers_and_notes(office):
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_outline(ws, office.doc)
    first = T.tool_read(ws, {"table": handle_of(out, "טבלת עסקאות השוואה")})
    assert tag(first, "status") == "clipped" and "רחוב הערבה 40 |" in first and "רחוב הערבה 41 |" not in first
    assert "כתובת | שטח במ\"ר | מחיר למ\"ר" in first and "המחירים ללא מע\"מ." in first
    assert "הטבלה: 45 שורות" in first
    rest = T.tool_read(ws, {"cursor": tag(first, "more")})
    assert tag(rest, "status") == "complete" and "רחוב הערבה 45 |" in rest and "רחוב הערבה 40 |" not in rest
    assert "כתובת | שטח במ\"ר | מחיר למ\"ר" in rest


def test_a_continued_table_part_keeps_the_caption_the_headers_the_size_and_the_notes_that_carry_the_units(office):
    """Every part of a table, the continuation included, is read with what gives its numbers their meaning (R15)."""
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_outline(ws, office.doc)
    first = T.tool_read(ws, {"table": handle_of(out, "טבלת עסקאות השוואה")})
    rest = T.tool_read(ws, {"cursor": tag(first, "more")})
    body = rest.split("\n", 2)[2]
    lines = body.splitlines()
    caption, size = lines.index("טבלת עסקאות השוואה"), next(i for i, x in enumerate(lines) if "45 שורות" in x)
    header = lines.index("כתובת | שטח במ\"ר | מחיר למ\"ר")
    assert caption < size < header < lines.index("רחוב הערבה 41 | 121 | 9410")
    assert lines[-3] == "המחירים ללא מע\"מ."  # the notes close the table of every part (then what follows, and the tag)
    assert lines[-2].startswith("[אחרי הטבלה: ")
    assert "שורות 41–45 מתוך 45" in rest


# --- the turn's memo of identical reads (round 9 R16, KTD9) ---------------------------------------------------------

def _pages(office, first: int, last: int) -> dict:
    return {"pages": {"document": office.doc, "from_page": first, "to_page": last}}


def test_an_identical_read_in_one_turn_returns_the_window_it_already_read_without_reading_again(office, monkeypatch):
    fetched = []
    real = T.reader.blocks_on_pages
    monkeypatch.setattr(T.reader, "blocks_on_pages", lambda *a, **kw: (fetched.append(a[2:4]), real(*a, **kw))[1])
    ws = T.Workspace(ctx=office.ctx())
    first = T.tool_read(ws, _pages(office, 2, 3))
    again = T.tool_read(ws, _pages(office, 2, 3))
    assert fetched == [(2, 3)] and "פסקה 1: שטח המגרש 812" in first
    # marked as already read: a reference to the first source, not its text again; the new id is citable all the same
    assert 'same_as="S1"' in again and "(אותו טקסט כמו S1, שכבר הוחזר בתור הזה)" in again and "פסקה 1" not in again
    assert ws.sources["S2"].text == ws.sources["S1"].text and ws.sources["S2"].page_list == ws.sources["S1"].page_list
    assert ws.sources["S2"].block_start == ws.sources["S1"].block_start and ws.sources["S2"].more == ws.sources["S1"].more
    # another window is another read
    T.tool_read(ws, _pages(office, 3, 3))
    assert fetched == [(2, 3), (3, 3)]


def test_an_identical_cursor_or_table_read_is_answered_from_the_memo_too(office, monkeypatch):
    structures = []
    real = T.reader.table_structure
    monkeypatch.setattr(T.reader, "table_structure", lambda *a, **kw: (structures.append(a[2]), real(*a, **kw))[1])
    ws = T.Workspace(ctx=office.ctx())
    handle = handle_of(T.tool_outline(ws, office.doc), "טבלת עסקאות השוואה")
    first = T.tool_read(ws, {"table": handle})
    structures.clear()
    cursor = tag(first, "more")
    T.tool_read(ws, {"cursor": cursor})
    T.tool_read(ws, {"cursor": cursor})
    assert structures == [0]  # the second identical continuation reads no table
    T.tool_read(ws, {"table": handle})  # the table from its beginning was read before
    assert structures == [0]


def test_a_read_after_the_offices_data_changed_reads_again(office, monkeypatch):
    from app.db import bump_data_version

    fetched = []
    real = T.reader.blocks_on_pages
    monkeypatch.setattr(T.reader, "blocks_on_pages", lambda *a, **kw: (fetched.append(a[2:4]), real(*a, **kw))[1])
    ws = T.Workspace(ctx=office.ctx())
    T.tool_read(ws, _pages(office, 2, 3))
    with tenant_tx(office.ctx()) as conn:
        bump_data_version(conn)
    T.tool_read(ws, _pages(office, 2, 3))
    assert fetched == [(2, 3), (2, 3)]


# --- block-level dedupe ------------------------------------------------------------------------------------------

def test_a_section_read_after_its_neighbors_sends_only_the_new_blocks(office):
    ws = T.Workspace(ctx=office.ctx())
    ws.add_source(document_id=office.doc, version_id=office.ver, title=TITLE, section=DESCRIPTION, location="עמוד 2",
                  kind="text", text="פסקה 2", block_start=4, block_end=4, reading_id="reading-1")
    near = T.tool_read(ws, {"source": "S1"})
    assert "פסקה 2:" in near and re.search(r"§\d+ «2\. תיאור הנכס»", near)
    assert ws.activity[office.doc]["read"] is False  # paragraphs around a passage are not a section read
    sent = {i for i, sid in ws.sent[str(office.ver)].items() if sid == "S2"}
    assert 4 in sent
    section = T.tool_read(ws, {"section": re.search(r"(§\d+) «2\. תיאור הנכס»", near).group(1)})
    assert "S2" in section and "כבר הוחזרו" in section
    assert section.count("פסקה 2:") == 0  # already sent with the neighbors
    full = ws.sources["S3"]
    assert "פסקה 2:" in full.text and "פסקה 1: שטח המגרש 812" in full.text  # the source keeps the full text
    # the same part read again is a reference to the first
    again = T.tool_read(ws, {"section": re.search(r"(§\d+) «2\. תיאור הנכס»", near).group(1)})
    assert 'same_as="S3"' in again


@pytest.mark.parametrize("opened", ["section", "pages"])
def test_a_continued_read_returns_the_parts_of_the_whole_window_from_its_cursor(office, monkeypatch, opened):
    """Every part after the first, a block cut in the middle included, holds the blocks and text the whole
    window's parts hold from the same position."""
    from app.chat import reader
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_read_chars", 500)  # paragraphs of ~830 characters are cut
    ws = T.Workspace(ctx=office.ctx())
    with tenant_tx(office.ctx()) as conn:
        if opened == "section":
            rows = conn.execute(text(f"SELECT {reader.BLOCK_COLS} FROM document_blocks WHERE version_id = :v"
                                     " AND section_path[1] = :p ORDER BY block_index"),
                                {"v": office.ver, "p": DESCRIPTION}).all()
            result = T.tool_read(ws, {"section": section_handle(ws, office.doc)})
        else:
            rows = conn.execute(text(f"SELECT {reader.BLOCK_COLS} FROM document_blocks WHERE version_id = :v"
                                     " AND page BETWEEN 2 AND 3 ORDER BY block_index"), {"v": office.ver}).all()
            result = T.tool_read(ws, {"pages": {"document": office.doc, "from_page": 2, "to_page": 3}})
    expected, pos, cuts = [], None, 0
    while True:
        part = reader.take(rows, pos, 500)
        expected.append(([r.block_index for r, _, _ in part.items], "\n".join(p for _, p, _ in part.items if p)))
        if part.next is None:
            break
        pos = part.next
        cuts += pos[1] > 0
    got = []
    while True:
        s = ws.sources[re.search(r'<source id="(S\d+)"', result).group(1)]
        got.append((list(range(s.block_start, s.block_end + 1)), s.text))
        if not tag(result, "more"):
            break
        result = T.tool_read(ws, {"cursor": tag(result, "more")})
    assert len(expected) > 3 and cuts  # parts that start inside a block, and parts that start at one
    assert got == expected


def test_the_verifier_receives_the_full_text_of_a_part_sent_as_a_reference(client, office, monkeypatch):
    seen: list[str] = []

    def judge(input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": "supported", "reason": "בדיקה"}
                             for i in re.findall(r'<unit index="(\d+)"', input)]}

    agent = ScriptedAgent([
        [read(pages={"document": office.doc, "from_page": 2, "to_page": 2})],
        lambda items: [read(section=re.search(r"(§\d+) «2\. תיאור הנכס»", _last_output(items)).group(1))],
        final("שטח המגרש 812 מ\"ר [S2].", documents=[office.doc])], judge=judge)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה שטח המגרש בערבה 14?")
    second = agent.tool_outputs(2)[-1]
    assert "פסקה 1: שטח המגרש 812" not in second and "S1" in second  # sent once, with the pages
    # the judge reads S2's own text, where the block sent with S1 is
    assert seen and re.search(r'<source id="S2"[^>]*>[^<]*שטח המגרש 812', seen[0])
    assert m["answer"]["status"] == "answered" and "812" in m["answer"]["markdown"]


# --- permissions and stale handles -------------------------------------------------------------------------------

def test_a_document_the_user_cannot_see_is_not_available_without_its_title_or_size(office):
    private, _ = add_document(office, office.private, sha="b" * 64)
    ws = T.Workspace(ctx=office.ctx("employee", office.outsider))
    for call_ in (lambda: T.tool_read(ws, {"pages": {"document": private, "from_page": 1, "to_page": 1}}),
                  lambda: T.tool_outline(ws, private)):
        with pytest.raises(T.ToolError) as e:
            call_()
        assert "אינו זמין" in str(e.value) and TITLE not in str(e.value) and "עמודים" not in str(e.value)
    assert ws.sources == {} and ws.activity == {}


def test_handles_and_cursors_stop_working_when_access_is_revoked(office):
    private, _ = add_document(office, office.private, sha="c" * 64)
    ws = T.Workspace(ctx=office.ctx("employee", office.emp))
    out = T.tool_outline(ws, private)
    d = re.search(r"\((D\d+);", out).group(1)
    first = T.tool_read(ws, {"section": handle_of(out, DESCRIPTION)})
    cursor = tag(first, "more")
    revoke(office)
    for locator in ({"cursor": cursor}, {"section": handle_of(out, DESCRIPTION)},
                    {"table": handle_of(out, "טבלת עסקאות השוואה")},
                    {"pages": {"document": d, "from_page": 1, "to_page": 1}}, {"source": "S1"}):
        with pytest.raises(T.ToolError) as e:
            T.tool_read(ws, locator)
        assert "אינו זמין" in str(e.value) and TITLE not in str(e.value)


def test_a_prior_reference_opens_while_its_reading_is_current_and_is_stale_after(office):
    resume = {"target": ["section", str(office.ver), [DESCRIPTION]], "pos": [6, 0]}
    prior = {"version_id": str(office.ver), "block_start": 4, "block_end": 4, "table_index": None, "chunk_id": None,
             "reading_id": "reading-1", "resume": resume}
    ws = T.Workspace(ctx=office.ctx(), prior={"P1": prior})
    out = T.tool_read(ws, {"source": "P1"})
    assert "פסקה 2:" in out
    cursor = re.search(r"read\(cursor=(K\d+)\)", out).group(1)  # where the earlier turn stopped
    rest = T.tool_read(ws, {"cursor": cursor})
    assert "הבניין בן חמש קומות" in rest and "פסקה 5:" in rest

    with tenant_tx(office.system) as conn:  # the document is read again
        conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion, '{reading_id}',"
                          " '\"reading-2\"') WHERE id = :v"), {"v": office.ver})
    with pytest.raises(T.ToolError, match="קריאה קודמת"):
        T.tool_read(T.Workspace(ctx=office.ctx(), prior={"P1": prior}), {"source": "P1"})
    with pytest.raises(T.ToolError, match="קריאה קודמת"):
        T.tool_read(ws, {"cursor": cursor})


# --- round 17 U4: a table read carries its context and registers what was sent (KTD4, KTD6 items 1-2) -------------------

PATH4 = ["4. שיטות"]
PATH5 = ["5. סיכום"]
SENSITIVITY = "טבלת רגישות לשיעור ההיוון"
SCOPE_NOTE = "הערה: הנתונים בטבלה שלעיל מתייחסים לגישת ההיוון בלבד, ולא לשווי הנכס כולו."
LAID_OUT_TITLE = "שומה רחוב הצפצפה 3"


def _sensitivity(caption: str = SENSITIVITY, block_index: int = 1, rows: int = 3) -> dict:
    """A stored table with no title and no notes (the shape of a DOCX or PDF table whose note lies outside it)."""
    return {"headers": ["תרחיש", "שווי"], "caption": caption, "title": [], "notes": [], "section": PATH4[0],
            "block_index": block_index, "rows": [{"cells": [f"תרחיש {n}", f"{4_000_000 + 10_000 * n:,}"]}
                                                 for n in range(1, rows + 1)]}


def add_laid_out(office, blocks: list[tuple], tables: dict[int, dict] | None = None, *, sha: str,
                 pages: bool = True) -> tuple[UUID, UUID]:
    """A document of the given blocks, (kind, section path, text, table index) in reading order, all read; ``tables``
    their stored structures by table index. ``pages=False``: a Word file (blocks without a page)."""
    doc, ver = make_document(office, office.default_group_id, LAID_OUT_TITLE, sha=sha)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = :n, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "n": 4 if pages else None, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, path, body, table, *at) in enumerate(blocks):
            page = at[0] if at else 1  # a fifth element is the block's page
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :pg,"
                " :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1], "sp": path, "pg": page if pages else None, "t": body,
                 "ti": table})
        for index, structure in (tables or {}).items():
            conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index,"
                              " page_start, page_end, structure) VALUES (app_office(), :d, :v, :t, :p, :p,"
                              " CAST(:s AS jsonb))"),
                         {"d": doc, "v": ver, "t": index, "p": 1 if pages else None,
                          "s": json.dumps(structure, ensure_ascii=False)})
    return doc, ver


def read_table(ws, doc, name: str = SENSITIVITY) -> str:
    return T.tool_read(ws, {"table": handle_of(T.tool_outline(ws, str(doc)), name)})


def the_source(ws, out: str):
    return ws.sources[re.search(r'<source id="(S\d+)"', out).group(1)]


def test_ae5_a_note_after_the_table_reaches_its_read_context_with_its_block_reference_and_stops_at_the_heading(office):
    doc, _ = add_laid_out(office, [("heading", PATH4, PATH4[0], None), ("table", PATH4, SENSITIVITY, 0),
                                   ("paragraph", PATH4, SCOPE_NOTE, None), ("heading", PATH5, PATH5[0], None),
                                   ("paragraph", PATH5, "ההמלצה סופית ואינה תלויה בטבלה.", None)],
                          {0: _sensitivity()}, sha="1" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, doc)
    s = the_source(ws, out)
    note = f"[אחרי הטבלה · פסקה · בלוק 2] {SCOPE_NOTE}"
    assert note in s.text and note in out  # what the verifier checks and what the model reads
    assert f"כותרות מעל הטבלה: {PATH4[0]}" in s.text
    assert PATH5[0] not in s.text and "ההמלצה" not in s.text  # it stops before the heading
    # the status and the pointer speak of the table and name what follows, with a handle that reads on
    assert "מצב הטבלה: הטבלה נקראה במלואה" in out and "מצב: נקרא במלואו" not in out
    pointer = re.search(r"\[אחרי הטבלה: כותרת \(בלוק 3\), פסקה \(בלוק 4\) — .*read\(cursor=(K\d+)\)\]", out)
    assert pointer and pointer.group(0) in out.split(note)[1]
    assert "ההמלצה סופית" in T.tool_read(ws, {"cursor": pointer.group(1)})


def test_a_table_followed_by_long_paragraphs_takes_only_those_within_the_cap_and_the_pointer_names_the_rest(office,
                                                                                                            monkeypatch):
    monkeypatch.setattr(T, "TABLE_CONTEXT_AFTER_CHARS", 300)
    paragraphs = [f"פסקה {n}: " + "מילה " * 28 for n in (1, 2, 3)]  # ~150 characters each
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), *[("paragraph", PATH4, p, None) for p in paragraphs]],
                          {0: _sensitivity(block_index=0)}, sha="2" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, doc)
    s = the_source(ws, out)
    assert re.search(r"\[אחרי הטבלה[^\]]*בלוק 1\] פסקה 1:", s.text) and re.search(r"\[אחרי הטבלה[^\]]*בלוק 2\] פסקה 2:", s.text)
    assert "פסקה 3:" not in s.text and not re.search(r"\[אחרי הטבלה[^\]]*בלוק 3\]", s.text)
    pointer = re.search(r"\[אחרי הטבלה: פסקה \(בלוק 3\) — .*read\(cursor=(K\d+)\)\]", out)
    assert pointer
    assert "פסקה 3:" in T.tool_read(ws, {"cursor": pointer.group(1)})


def test_a_table_followed_directly_by_another_table_takes_no_trailing_part_and_the_pointer_names_it(office):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("table", PATH4, "טבלת מדדים", 1),
                                   ("paragraph", PATH4, SCOPE_NOTE, None)],
                          {0: _sensitivity(block_index=0), 1: _sensitivity("טבלת מדדים", 1)}, sha="3" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, doc)
    s = the_source(ws, out)
    assert "[אחרי הטבלה ·" not in s.text and SCOPE_NOTE not in s.text and "[לפני הטבלה ·" not in s.text
    assert re.search(r"\[אחרי הטבלה: טבלה \(בלוק 1\), פסקה \(בלוק 2\) — .*read\(cursor=K\d+\)\]", out)


def test_the_paragraph_before_a_table_is_a_part_unless_it_is_the_tables_caption(office):
    intro = "הטבלה שלהלן מציגה את שווי הנכס בשלושה תרחישים."
    doc, _ = add_laid_out(office, [("paragraph", PATH4, intro, None), ("table", PATH4, SENSITIVITY, 0),
                                   ("paragraph", PATH4, "טבלת מדדים", None), ("table", PATH4, "טבלת מדדים", 1)],
                          {0: _sensitivity(), 1: _sensitivity("טבלת מדדים", 3)}, sha="4" * 64)
    ws = T.Workspace(ctx=office.ctx())
    first = the_source(ws, read_table(ws, doc))
    assert f"[לפני הטבלה · פסקה · בלוק 0] {intro}" in first.text
    second = the_source(ws, read_table(ws, doc, "טבלת מדדים"))
    # the block above the second table is its caption, written as a paragraph here: not a part, not repeated
    assert "[לפני הטבלה ·" not in second.text
    # and the first table's trailing paragraph is that caption: between the two tables
    assert "[אחרי הטבלה · בין שתי טבלאות · בלי הפניה לטבלה · פסקה · בלוק 2] טבלת מדדים" in first.text


@pytest.mark.parametrize("pages", [True, False], ids=["pdf-shaped", "docx-shaped"])
def test_pdf_and_docx_shaped_tables_with_no_notes_and_no_title_still_get_their_trailing_part(office, pages):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                          {0: _sensitivity(block_index=0)}, sha="5" * 64, pages=pages)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    assert f"[אחרי הטבלה · פסקה · בלוק 1] {SCOPE_NOTE}" in s.text


def test_a_paragraph_between_two_tables_is_a_part_of_both_tagged_as_between_tables(office):
    between = "הנתונים שלהלן מבוססים על סקר שוק שנערך השנה."
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, between, None),
                                   ("table", PATH4, "טבלת מדדים", 1)],
                          {0: _sensitivity(block_index=0), 1: _sensitivity("טבלת מדדים", 2)}, sha="6" * 64)
    ws = T.Workspace(ctx=office.ctx())
    a = the_source(ws, read_table(ws, doc))
    b = the_source(ws, read_table(ws, doc, "טבלת מדדים"))
    # it names no table, so it is attributed to neither (round 18 U4): the tag also says so
    assert f"[אחרי הטבלה · בין שתי טבלאות · בלי הפניה לטבלה · פסקה · בלוק 1] {between}" in a.text
    assert f"[לפני הטבלה · בין שתי טבלאות · בלי הפניה לטבלה · פסקה · בלוק 1] {between}" in b.text


# --- round 18 U4: a paragraph beside a table is attributed to it by adjacency, page and wording (KTD5, R9; AE7, AE8) -----------

NOTE_BACK = "הטבלה שלעיל מתייחסת לגישת היוון ההכנסות בלבד, ואינה משקפת את שווי הנכס בגישת ההשוואה."
UNRELATED = ["הנתונים נאספו מדוחות השוק האחרונים.", "הבדיקה נערכה לפני מועד השומה.", "לא נמצאו חריגות בנתוני הקלט."]
OTHER = "טבלת רגישות לדמי השכירות"


def two_tables(office, middle: list[tuple], *, sha: str):
    """The sensitivity table (block 0, page 1), the ``middle`` blocks, then another table: a document of the AE7 shape."""
    last = len(middle) + 1
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0, 1), *middle, ("table", PATH4, OTHER, 1, 2)],
                          {0: _sensitivity(block_index=0), 1: _sensitivity(OTHER, last)}, sha=sha)
    ws = T.Workspace(ctx=office.ctx())
    return ws, the_source(ws, read_table(ws, doc)), the_source(ws, read_table(ws, doc, OTHER))


def test_ae7_a_note_that_points_back_and_follows_the_table_directly_is_attributed_though_another_table_comes_later(office):
    middle = [("paragraph", PATH4, NOTE_BACK, None, 1), *[("paragraph", PATH4, u, None, 2) for u in UNRELATED]]
    ws, first, second = two_tables(office, middle, sha="b1" * 32)
    assert f"[אחרי הטבלה · פסקה · בלוק 1] {NOTE_BACK}" in first.text  # no mark: attributed, though a table follows
    assert all(f"[אחרי הטבלה · בין שתי טבלאות · לא צמוד לטבלה · בלי הפניה לטבלה · פסקה · בלוק {n}] {u}" in first.text
               for n, u in zip((2, 3, 4), UNRELATED, strict=True))  # the unrelated paragraphs of the next page are not
    assert [p.reasons for p in first.table_parts if p.side == "after"] == [(), ("between", "not_adjacent", "no_reference"),
                                                                           ("between", "not_adjacent", "no_reference"),
                                                                           ("between", "not_adjacent", "no_reference")]
    assert first.table_parts[0].reference == "back" and first.table_parts[0].page == 1 and first.table_parts[0].gap == 0


def test_ae7_the_same_note_without_the_reference_word_is_not_attributed(office):
    plain = NOTE_BACK.replace(" שלעיל", "")
    ws, first, _ = two_tables(office, [("paragraph", PATH4, plain, None, 1)], sha="b2" * 32)
    assert f"[אחרי הטבלה · בין שתי טבלאות · בלי הפניה לטבלה · פסקה · בלוק 1] {plain}" in first.text
    assert first.table_parts[0].reasons == ("between", "no_reference") and not first.table_parts[0].attributed


def test_ae7_the_same_note_with_an_unrelated_block_between_it_and_the_table_is_not_attributed(office):
    middle = [("paragraph", PATH4, UNRELATED[0], None, 1), ("paragraph", PATH4, NOTE_BACK, None, 1)]
    ws, first, _ = two_tables(office, middle, sha="b3" * 32)
    assert f"[אחרי הטבלה · בין שתי טבלאות · לא צמוד לטבלה · פסקה · בלוק 2] {NOTE_BACK}" in first.text
    assert first.table_parts[1].reasons == ("between", "not_adjacent") and first.table_parts[1].gap == 1


@pytest.mark.parametrize("page, attributed", [(1, True), (2, True), (3, False)])
def test_a_note_on_the_tables_page_or_the_top_of_the_next_is_adjacent_and_one_two_pages_on_is_not(office, page, attributed):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0, 1), ("paragraph", PATH4, NOTE_BACK, None, page)],
                          {0: _sensitivity(block_index=0)}, sha=f"b4{page}" * 21 + "b")
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    (part,) = s.table_parts
    assert part.attributed is attributed and part.page == page and part.page_gap == page - 1
    assert (f"[אחרי הטבלה · פסקה · בלוק 1] {NOTE_BACK}" in s.text) is attributed


def test_a_paragraph_with_a_forward_reference_scopes_the_table_it_precedes_and_not_the_table_above(office):
    forward = "הטבלה הבאה מתייחסת לגישת היוון ההכנסות בלבד."
    ws, first, second = two_tables(office, [("paragraph", PATH4, forward, None, 1)], sha="b5" * 32)
    assert f"[לפני הטבלה · פסקה · בלוק 1] {forward}" in second.text  # no mark: attributed to the table below
    assert f"[אחרי הטבלה · בין שתי טבלאות · בלי הפניה לטבלה · פסקה · בלוק 1] {forward}" in first.text  # and not to the one above
    assert second.table_parts[0].attributed and not first.table_parts[0].attributed


def test_a_paragraph_before_a_table_that_points_back_to_the_table_above_is_not_attributed_to_the_one_below(office):
    ws, first, second = two_tables(office, [("paragraph", PATH4, NOTE_BACK, None, 1)], sha="b6" * 32)
    assert first.table_parts[0].attributed  # the table above it
    assert second.table_parts[0].reasons == ("between", "no_reference")


def test_ae8_a_numbered_label_stored_as_a_paragraph_is_part_of_the_tables_heading_scope(office):
    method = "4. גישת היוון ההכנסות"
    lead = "הטבלה שלהלן מציגה את תוצאות החישוב."
    doc, _ = add_laid_out(office, [("heading", PATH4, PATH4[0], None), ("paragraph", PATH4, method, None),
                                   ("paragraph", PATH4, lead, None), ("table", PATH4, SENSITIVITY, 0)],
                          {0: _sensitivity(block_index=3)}, sha="b7" * 32)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    assert f"[לפני הטבלה · כותרת בסעיף · פסקה · בלוק 1] {method}" in s.text
    assert f"[לפני הטבלה · פסקה · בלוק 2] {lead}" in s.text
    assert [(p.heading_like, p.reasons) for p in s.table_parts] == [(True, ()), (False, ())]
    from app.chat import verify

    assert [(p.place, p.text) for p in verify.context_of_source(s)[0] if p.place == "heading"][-1] == ("heading", method)


@pytest.mark.parametrize("label, path, why", [
    ("4. הערכת השווי נעשתה לפי גישת ההכנסות.", PATH4, "ends with a period: a sentence"),
    ("הערה:", PATH4, "ends with a colon"),
    ("4. גישת ההשוואה", PATH5, "another section"),
    ("הנתונים בטבלה שלהלן נאספו מדוחות השוק האחרונים בלי שנעשתה בהם כל התאמה נוספת בשום שלב", PATH4, "too long"),
])
def test_a_paragraph_that_is_not_a_short_label_of_the_same_section_is_not_a_heading_part(office, label, path, why):
    doc, _ = add_laid_out(office, [("heading", PATH4, PATH4[0], None), ("paragraph", path, label, None),
                                   ("table", PATH4, SENSITIVITY, 0)], {0: _sensitivity(block_index=2)}, sha="b8" * 32)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    assert "כותרת בסעיף" not in s.text, why
    assert not any(p.heading_like for p in s.table_parts), why


def test_a_label_above_another_table_is_not_the_heading_scope_of_the_table_below_it(office):
    label = "4. גישת היוון ההכנסות"
    doc, _ = add_laid_out(office, [("paragraph", PATH4, label, None), ("table", PATH4, SENSITIVITY, 0),
                                   ("table", PATH4, OTHER, 1)], {0: _sensitivity(block_index=1), 1: _sensitivity(OTHER, 2)},
                          sha="b9" * 32)
    ws = T.Workspace(ctx=office.ctx())
    first = the_source(ws, read_table(ws, doc))
    second = the_source(ws, read_table(ws, doc, OTHER))
    assert any(p.heading_like for p in first.table_parts) and not any(p.heading_like for p in second.table_parts)


def test_a_later_sections_note_is_never_a_part_of_the_table_above_its_heading(office):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("heading", PATH5, PATH5[0], None),
                                   ("paragraph", PATH5, "הערה: הטבלה שלעיל מתייחסת לגישת ההשוואה בלבד.", None)],
                          {0: _sensitivity(block_index=0)}, sha="c1" * 32)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    assert s.table_parts == [] and "גישת ההשוואה" not in s.text


def test_the_memoized_copy_of_a_table_read_carries_the_same_structured_parts(office):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                          {0: _sensitivity(block_index=0)}, sha="c2" * 32)
    ws = T.Workspace(ctx=office.ctx())
    handle = handle_of(T.tool_outline(ws, str(doc)), SENSITIVITY)
    T.tool_read(ws, {"table": handle})
    T.tool_read(ws, {"table": handle})
    assert ws.sources["S2"].table_parts == ws.sources["S1"].table_parts != []


def test_a_table_at_the_end_of_a_unit_takes_no_trailing_part_from_the_next_units_untitled_paragraph(office):
    from app.chat.contexts import Contexts, Identity, Segment

    doc, ver = add_laid_out(office, [("heading", PATH4, PATH4[0], None), ("table", PATH4, SENSITIVITY, 0),
                                     ("paragraph", PATH4, "פסקת פתיחה ללא כותרת של הנכס הבא.", None),
                                     ("heading", PATH5, PATH5[0], None)], {0: _sensitivity()}, sha="7" * 64)
    ws = T.Workspace(ctx=office.ctx())
    ws.contexts[str(ver)] = Contexts(str(ver), (
        Segment(1, Identity(), 0, 1, 0, None, 1, 1, "appraisal", "certain", "נכס ראשון"),
        Segment(2, Identity(), 2, 3, 3, None, 1, 1, "appraisal", "certain", "נכס שני")), "reading-1")
    out = read_table(ws, doc)
    s = the_source(ws, out)
    assert "פסקת פתיחה" not in s.text and "[אחרי הטבלה ·" not in s.text
    assert "פסקת פתיחה" not in out and "אין עוד קטעים ביחידה" in out and "read(cursor=" not in out.split("מצב הטבלה")[1]


def test_nothing_follows_is_said_only_when_the_scan_reached_the_end_of_the_unit(office):
    """Round 17 review #9: a table whose own blocks fill the scan window leaves its tail unread, which is not the same as nothing
    following it; a table at the end of the document does have nothing after it."""
    blocks = [("table", PATH4, SENSITIVITY, 0)] + [("table", PATH4, f"שורה {n}", 0) for n in range(1, 13)]
    doc, _ = add_laid_out(office, blocks + [("paragraph", PATH4, SCOPE_NOTE, None)], {0: _sensitivity(block_index=0)},
                          sha="a" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, doc)
    assert "אין עוד קטעים ביחידה" not in out and T.MSG_TABLE_AFTER_NOT_READ in out
    end, _ = add_laid_out(office, blocks, {0: _sensitivity(block_index=0)}, sha="b" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, end)
    assert "אין עוד קטעים ביחידה" in out and T.MSG_TABLE_AFTER_NOT_READ not in out


def test_a_hebrew_note_with_quotation_marks_and_numbers_reaches_the_read_context_verbatim(office):
    note = "* לפי תקן 17 ״הערכת שווי״: השיעורים הם 7.5% ו-8.25% והסכומים באלפי ₪."
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, note, None)],
                          {0: _sensitivity(block_index=0)}, sha="8" * 64)
    ws = T.Workspace(ctx=office.ctx())
    out = read_table(ws, doc)
    assert f"[אחרי הטבלה · בלי הפניה לטבלה · פסקה · בלוק 1] {note}" in the_source(ws, out).text and note in out


def test_a_table_read_twice_returns_the_memoized_copy_with_the_same_context_parts(office):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                          {0: _sensitivity(block_index=0)}, sha="9" * 64)
    ws = T.Workspace(ctx=office.ctx())
    handle = handle_of(T.tool_outline(ws, str(doc)), SENSITIVITY)
    first = T.tool_read(ws, {"table": handle})
    again = T.tool_read(ws, {"table": handle})
    s1, s2 = ws.sources["S1"], ws.sources["S2"]
    assert 'same_as="S1"' in again and SCOPE_NOTE not in again
    assert s2.text == s1.text and f"[אחרי הטבלה · פסקה · בלוק 1] {SCOPE_NOTE}" in s2.text and s2.body == s1.body
    assert SCOPE_NOTE in first


def test_a_complete_table_read_registers_its_block_and_its_parts_so_a_later_section_read_sends_them_once(office):
    sub = [*PATH4, "4.1 תת-סעיף"]
    doc, ver = add_laid_out(office, [("heading", PATH4, PATH4[0], None), ("table", PATH4, SENSITIVITY, 0),
                                     ("paragraph", PATH4, SCOPE_NOTE, None), ("heading", sub, sub[1], None),
                                     ("paragraph", sub, "פסקה חדשה בסעיף שטרם נקראה.", None)],
                          {0: _sensitivity()}, sha="a1" * 32)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_outline(ws, str(doc))
    T.tool_read(ws, {"table": handle_of(out, SENSITIVITY)})
    assert ws.sent[str(ver)] == {1: "S1", 2: "S1"}
    section = T.tool_read(ws, {"section": handle_of(out, PATH4[0])})
    assert SCOPE_NOTE not in section and "S1" in section and "כבר הוחזרו" in section
    assert "פסקה חדשה בסעיף" in section
    assert SCOPE_NOTE in ws.sources["S2"].text  # the verifier still reads the full text


def test_a_clipped_table_part_registers_its_context_parts_but_not_its_block_and_the_next_part_points_to_them(office):
    doc, ver = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                            {0: _sensitivity(block_index=0, rows=45)}, sha="a2" * 32)
    ws = T.Workspace(ctx=office.ctx())
    first = read_table(ws, doc)
    assert tag(first, "status") == "clipped" and SCOPE_NOTE in first
    assert ws.sent[str(ver)] == {1: "S1"}  # the table was not read whole
    rest = T.tool_read(ws, {"cursor": tag(first, "more")})
    assert SCOPE_NOTE not in rest and "כבר הוחזר ב-S1" in rest
    s2 = the_source(ws, rest)
    assert f"[אחרי הטבלה · פסקה · בלוק 1] {SCOPE_NOTE}" in s2.text  # the verifier's text of every part keeps it
    assert 0 not in ws.sent[str(ver)]  # the last part alone is not the whole table either


def test_the_status_of_a_clipped_table_speaks_of_the_table(office):
    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0)], {0: _sensitivity(block_index=0, rows=45)},
                          sha="a3" * 32)
    out = read_table(ws := T.Workspace(ctx=office.ctx()), doc)
    assert "מצב הטבלה: נקרא חלק מהטבלה; יש המשך" in out and ws.sources["S1"].status == "clipped"


def test_the_rereads_table_expansion_carries_the_same_context_parts(office):
    from app.chat import reader
    from app.chat.rereader import _expand_table_headers

    note = SCOPE_NOTE
    intro = "הטבלה שלהלן מציגה את שווי הנכס בשלושה תרחישים."
    doc, ver = add_laid_out(office, [("paragraph", PATH4, intro, None), ("table", PATH4, SENSITIVITY, 0),
                                     ("paragraph", PATH4, note, None)], {0: _sensitivity()}, sha="a4" * 32)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    with tenant_tx(office.ctx()) as conn:
        text_, _, _ = _expand_table_headers(ws, conn, reader.version(conn, ver), s)
    assert f"[לפני הטבלה · פסקה · בלוק 0] {intro}" in text_ and f"[אחרי הטבלה · פסקה · בלוק 2] {note}" in text_
    assert text_.index("בלוק 0") < text_.index("תרחיש") < text_.index("בלוק 2")


def test_the_verifiers_view_of_a_clipped_long_table_keeps_the_trailing_part(office):
    from app.chat import evidence

    doc, _ = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                          {0: _sensitivity(block_index=0, rows=40)}, sha="a5" * 32)
    ws = T.Workspace(ctx=office.ctx())
    s = the_source(ws, read_table(ws, doc))
    assert len(s.text) > 300
    kept, excerpt = evidence.fit(["שווי של 4,010,000 בתרחיש 1"], s.text, "table", limit=300)
    assert excerpt and f"[אחרי הטבלה · פסקה · בלוק 1] {SCOPE_NOTE}" in kept and "4,010,000" in kept


# --- search hits register the blocks they showed whole ---------------------------------------------------------

def _hit_of(office, doc, ver, *, kind: str, body: str, first: int, last: int, table_index: int | None = None,
            row_index: int | None = None) -> dict:
    with tenant_tx(office.system) as conn:
        cid = conn.execute(text(
            "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section, text,"
            " normalized_text, table_index, row_index, block_start, block_end) VALUES (app_office(), :d, :v, :i, :k,"
            " '{1}', :s, :t, :t, :ti, :ri, :a, :b) RETURNING id"),
            {"d": doc, "v": ver, "i": first, "k": kind, "s": PATH4[0], "t": body, "ti": table_index, "ri": row_index,
             "a": first, "b": last}).scalar_one()
    hit = {"chunk_id": cid, "document_id": doc, "version_id": ver, "page_list": [1], "section": PATH4[0], "text": body,
           "kind": kind, "title": LAID_OUT_TITLE, "score": 1.0, "lexical_support": True, "snippet": body}
    if table_index is not None:
        hit["table_index"] = table_index
    if row_index is not None:
        hit["row_index"] = row_index
    return hit


def _searching(monkeypatch, *hits: dict) -> None:
    monkeypatch.setattr(T, "memoized_search", lambda ws, conn, query, limit, scope, window=False, version=None:
                        [dict(h) for h in hits])


def test_a_paragraph_hit_shown_whole_is_not_sent_again_by_a_later_read_and_the_output_says_so(office, monkeypatch):
    target = "שטח הדירה לפי המדידה הוא 94 מ\"ר, לא כולל מרפסת."
    doc, ver = add_laid_out(office, [("paragraph", PATH4, "פסקה לפני.", None), ("paragraph", PATH4, target, None),
                                     ("paragraph", PATH4, "פסקה אחרי.", None)], sha="b1" * 32)
    _searching(monkeypatch, _hit_of(office, doc, ver, kind="text", body=target, first=1, last=1))
    ws = T.Workspace(ctx=office.ctx())
    hit = T.tool_search(ws, "שטח הדירה", [str(doc)])
    assert target in hit and ws.sent[str(ver)] == {1: "S1"}
    nearby = T.tool_read(ws, {"source": "S1"})
    assert target not in nearby and "כבר הוחזרו" in nearby and "S1" in nearby
    assert "פסקה לפני." in nearby and "פסקה אחרי." in nearby
    assert target in ws.sources["S2"].text  # the verifier keeps the full text


def test_a_hit_clipped_by_the_passage_cap_registers_nothing_and_a_later_read_sends_it_in_full(office, monkeypatch):
    from app.config import get_settings

    long_body = "תיאור מפורט של הבניין. " * 30 + "סוף-הפסקה"
    doc, ver = add_laid_out(office, [("paragraph", PATH4, long_body, None)], sha="b2" * 32)
    monkeypatch.setattr(get_settings(), "chat_passage_chars", 120)
    _searching(monkeypatch, _hit_of(office, doc, ver, kind="text", body=long_body, first=0, last=0))
    ws = T.Workspace(ctx=office.ctx())
    hit = T.tool_search(ws, "תיאור הבניין", [str(doc)])
    assert "סוף-הפסקה" not in hit and not ws.sent.get(str(ver))
    assert "סוף-הפסקה" in T.tool_read(ws, {"source": "S1"})


def test_a_scoped_hit_cut_around_the_query_terms_registers_nothing_and_a_later_read_sends_the_block_in_full(office):
    from tests.integration.test_chat_units import RENT
    from tests.support.unit_files import READING, unit_file

    filler = "מילה " * 200
    body = f"ראשית-המקטע {filler}{RENT} 5,400 ₪. {filler}סוף-המקטע"
    doc, ver = unit_file(office, office.default_group_id, "קובץ יחידות", [
        {"title": "פרויקט הארז", "aliases": ["הארז"], "blocks": ["פרויקט הארז", "שורה קצרה."]},
        {"title": "מתחם האשל", "aliases": ["האשל"], "blocks": ["מתחם האשל", body]}], sha="b3" * 32)
    ws = T.Workspace(ctx=office.ctx())
    handle = T.unit_handle_of(ws, doc, ver, READING, 2)
    out = T.tool_search(ws, RENT, None, None, handle)
    cut = next(s for s in ws.sources.values() if "5,400" in s.text)
    assert "סוף-המקטע" not in cut.text and f"read(source={cut.sid})" in out
    assert cut.block_start not in ws.sent.get(str(ver), {})  # a cut passage is not a block returned whole
    assert "סוף-המקטע" in T.tool_read(ws, {"source": cut.sid})


def test_a_table_row_hit_registers_nothing_and_read_source_still_returns_the_table_and_its_handle(office, monkeypatch):
    doc, ver = add_laid_out(office, [("table", PATH4, SENSITIVITY, 0), ("paragraph", PATH4, SCOPE_NOTE, None)],
                            {0: _sensitivity(block_index=0)}, sha="b4" * 32)
    row = "טבלת רגישות לשיעור ההיוון: תרחיש: תרחיש 2 | שווי: 4,020,000"
    _searching(monkeypatch, _hit_of(office, doc, ver, kind="table_row", body=row, first=0, last=0, table_index=0,
                                    row_index=1))
    ws = T.Workspace(ctx=office.ctx())
    hit = T.tool_search(ws, "תרחיש 2", [str(doc)])
    assert re.search(r'table="T\d+"', hit) and not ws.sent.get(str(ver))
    table = T.tool_read(ws, {"source": "S1"})
    assert 'kind="table"' in table and "תרחיש 3 | 4,030,000" in table and re.search(r'table="T\d+"', table)
    assert f"[אחרי הטבלה · פסקה · בלוק 1] {SCOPE_NOTE}" in table


# --- round 17 U6: the search envelope prints a document's attributes once; a section read lists its unread siblings ------

THREE = ["שטח הדירה לפי המדידה הוא 94 מ\"ר.", "מחיר למ\"ר בדירה הוא 12,000 ₪.", "הדירה נמצאת בקומה השלישית."]


def _three_hits(office, monkeypatch, sha: str = "c1" * 32) -> tuple[UUID, UUID]:
    doc, ver = add_laid_out(office, [("paragraph", PATH4, b, None) for b in THREE], sha=sha)
    _searching(monkeypatch, *[_hit_of(office, doc, ver, kind="text", body=b, first=i, last=i)
                              for i, b in enumerate(THREE)])
    return doc, ver


def test_three_hits_of_one_document_print_its_title_and_id_once_and_each_hit_keeps_its_id_location_and_kind(
        office, monkeypatch):
    doc, ver = _three_hits(office, monkeypatch)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_search(ws, "הדירה", [str(doc)])
    assert out.count(LAID_OUT_TITLE) == 1 and out.count("<document ") == 1  # the title and the document tag, once
    assert out.count(f'<source id="S1" document_id="{doc}"') == 1  # a hit opens as it always did
    tags = re.findall(r'<source id="(S\d+)"([^>]*)>', out)
    assert [sid for sid, _ in tags] == ["S1", "S2", "S3"]
    for _sid, attrs in tags:
        assert re.search(r'\blocation="[^"]+"', attrs) and re.search(r'\bkind="text"', attrs), attrs
        assert re.search(r'\bdocument="D\d+"', out)  # the document's handle is still given (once, with the document)
    assert all(b in out for b in THREE)


def test_the_sources_the_verifier_checks_against_are_the_same_whether_the_search_output_is_grouped_or_not(
        office, monkeypatch):
    doc, ver = _three_hits(office, monkeypatch)
    ws = T.Workspace(ctx=office.ctx())
    T.tool_search(ws, "הדירה", [str(doc)])
    for src, body in zip(ws.sources.values(), THREE, strict=True):
        assert src.text == body and src.title == LAID_OUT_TITLE and str(src.document_id) == str(doc)
        assert src.body is None and src.location and src.kind == "text"


def test_grouping_saves_the_envelope_of_every_hit_after_the_first(office, monkeypatch):
    doc, ver = _three_hits(office, monkeypatch)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_search(ws, "הדירה", [str(doc)])
    flat = "\n\n".join(T._render_source(s) for s in ws.sources.values())
    saved = len(flat) - len(out)
    # the title and the document handle are printed once instead of three times (the group tag itself costs a little)
    assert saved >= 2 * len(LAID_OUT_TITLE) - 40, (len(flat), len(out))
    print(f"\nENVELOPE 3 same-document hits: before={len(flat)} after={len(out)} saved={saved}")


def test_hits_of_two_documents_form_two_groups_in_order_and_a_lone_hit_keeps_its_own_tag(office, monkeypatch):
    doc1, ver1 = add_laid_out(office, [("paragraph", PATH4, THREE[0], None), ("paragraph", PATH4, THREE[1], None)],
                              sha="c2" * 32)
    doc2, ver2 = add_laid_out(office, [("paragraph", PATH4, THREE[2], None)], sha="c3" * 32)
    _searching(monkeypatch,
               _hit_of(office, doc1, ver1, kind="text", body=THREE[0], first=0, last=0),
               _hit_of(office, doc2, ver2, kind="text", body=THREE[2], first=0, last=0),
               _hit_of(office, doc1, ver1, kind="text", body=THREE[1], first=1, last=1))
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_search(ws, "הדירה", None)
    assert out.count("<document ") == 1 and out.count(LAID_OUT_TITLE) == 2  # a group of two: the title once; the lone hit as before
    assert re.search(r'<document document="D\d+" title="[^"]+"', out) and f'<source id="S2" document_id="{doc2}"' in out
    assert 'title="' + LAID_OUT_TITLE + '"' in out.split("</document>")[1]  # the lone hit keeps its own title
    assert out.index(THREE[0]) < out.index(THREE[1]) < out.index(THREE[2])  # the group follows its first hit
    assert [s.document_id for s in ws.sources.values()] == [doc1, doc2, doc1]  # source ids are still in rank order


def test_the_progress_label_of_a_grouped_search_counts_its_hits_and_documents(office, monkeypatch):
    from app.chat import timing

    doc, ver = add_laid_out(office, [("paragraph", PATH4, b, None) for b in THREE[:2]], sha="c4" * 32)
    doc2, ver2 = add_laid_out(office, [("paragraph", PATH4, THREE[2], None)], sha="c5" * 32)
    mine = [_hit_of(office, doc, ver, kind="text", body=b, first=i, last=i) for i, b in enumerate(THREE[:2])]
    other = _hit_of(office, doc2, ver2, kind="text", body=THREE[2], first=0, last=0)
    _searching(monkeypatch, *mine)
    out = T.tool_search(T.Workspace(ctx=office.ctx()), "הדירה", None)
    assert timing.done_label("search", {}, out) == "נמצאו 2 קטעים"
    _searching(monkeypatch, *mine, other)
    out = T.tool_search(T.Workspace(ctx=office.ctx()), "הדירה", None)
    assert timing.done_label("search", {}, out) == "נמצאו 3 קטעים ב-2 מסמכים"


SIX = "6. התחשיב"
S61, S62, S63 = [SIX, "6.1 עסקאות"], [SIX, "6.2 שכירויות"], [SIX, "6.3 סיכום"]


def _six(office, sha: str = "d1" * 32):
    return add_laid_out(office, [
        ("heading", [SIX], SIX, None),
        ("heading", S61, "6.1 עסקאות", None), ("paragraph", S61, "עסקה אחת במתחם: 3,000,000 ₪.", None),
        ("heading", S62, "6.2 שכירויות", None), ("paragraph", S62, "דמי שכירות חודשיים: 5,400 ₪.", None),
        ("heading", S63, "6.3 סיכום", None), ("paragraph", S63, "השווי הסופי נקבע לפי העסקאות.", None)], sha=sha)


def test_a_sub_section_read_lists_its_unread_siblings_with_handles_and_drops_one_once_it_is_read(office):
    doc, _ = _six(office)
    ws = T.Workspace(ctx=office.ctx())
    outline = T.tool_outline(ws, str(doc))
    h61, h62, h63 = (handle_of(outline, n) for n in ("6.1 עסקאות", "6.2 שכירויות", "6.3 סיכום"))
    first = T.tool_read(ws, {"section": h61})
    assert "6.2 שכירויות" in first and "6.3 סיכום" in first and h62 in first and h63 in first
    assert "6.1 עסקאות»" not in first.split("סעיפים אחים")[-1]  # the section being read is not its own sibling
    second = T.tool_read(ws, {"section": h62})
    tail = second.split("סעיפים אחים")[-1]
    assert h63 in tail and "6.3 סיכום" in tail and "6.1 עסקאות" not in tail  # 6.1 was read: no longer listed
    third = T.tool_read(ws, {"section": h63})
    assert "סעיפים אחים" not in third  # nothing unread is left


def test_a_top_level_section_read_lists_no_siblings(office):
    doc, _ = _six(office, sha="d2" * 32)
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_read(ws, {"section": handle_of(T.tool_outline(ws, str(doc)), SIX)})
    assert "סעיפים אחים" not in out


def test_the_next_part_of_a_section_does_not_repeat_the_sibling_list(office):
    doc, _ = add_laid_out(office, [
        ("heading", [SIX], SIX, None), ("heading", S61, "6.1 עסקאות", None),
        *[("paragraph", S61, f"פסקה {n}: " + FILLER, None) for n in range(1, 9)],
        ("heading", S62, "6.2 שכירויות", None), ("paragraph", S62, "דמי שכירות חודשיים: 5,400 ₪.", None)], sha="d3" * 32)
    ws = T.Workspace(ctx=office.ctx())
    first = T.tool_read(ws, {"section": handle_of(T.tool_outline(ws, str(doc)), "6.1 עסקאות")})
    assert tag(first, "more") and "6.2 שכירויות" in first
    second = T.tool_read(ws, {"cursor": tag(first, "more")})
    assert "סעיפים אחים" not in second
