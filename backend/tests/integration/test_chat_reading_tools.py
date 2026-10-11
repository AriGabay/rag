"""The reading tools (U8): ``read`` by source, pages, section, table or continuation, and an openable ``outline``.

The model is scripted; these tests prove what the server returns and records: every result states whether it is
complete, clipped, or has unread regions, and how to read on; a section is never taken as fully read when only
part of it was returned; blocks already returned are not sent again while verification still sees the full
text; the agent's reads open the same blocks the source panel shows; and permission and the reading a handle was
issued for are checked on every access. Synthetic documents only; the street names are invented."""

from __future__ import annotations

import json
import re

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
