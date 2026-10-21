"""What the scope reader's preload reads from the stored document (``ContextReader.preload``, round 20 U3, KTD3): the layout of each cited
source (the blocks of its appraisal unit above it, with their word geometry) and the title block of each unit of a file holding several.
It reads stored text only, under the user's own permissions: a reader that fails, a source read again since, an allowance that is spent
and a document the user may not see all leave ``None`` (a layout) or nothing (a title) and never raise; the blocks that hold no text are
left out. Synthetic documents in one office (office B); no model call."""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from app.chat import scopes
from app.chat import tools as T
from app.chat.rereader import ContextReader
from app.db import tenant_tx
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

READING = "reading-1"
RESPONDENT = "4. טענות המשיבה"
DECISION = "6. דיון והכרעה"
WATERMARK = "סימן מים של דף"
UNREAD = "טקסט שלא נקרא בעמוד"
ADOPT = "לאחר שבחנתי את טענות הצדדים, אני קובע כי שווי הקרקע הוא 190 ₪ למ\"ר."

# two appraisals in one file (the second's numbering starts again below the first's last chapter); the second holds blocks with no text to read
APPRAISAL_A = [("paragraph", "שומה נגדית — רחוב הערבה 3\nגוש: 40100 חלקה: 12", [], 1, "read"),
               ("heading", "8. השומה", ["8. השומה"], 1, "read"),
               ("paragraph", "שווי הקרקע הוא 180 ₪ למ\"ר.", ["8. השומה"], 1, "read")]
APPRAISAL_B = [("paragraph", "שומה מכרעת — רחוב השיטה 5\nגוש: 40102 חלקה: 4", ["8. השומה"], 2, "read"),
               ("paragraph", WATERMARK, ["8. השומה"], 2, "decorative"),
               ("heading", RESPONDENT, [RESPONDENT], 2, "read"),
               ("paragraph", "המשיבה טוענת כי שווי הקרקע הוא 200 ₪ למ\"ר.", [RESPONDENT], 2, "read"),
               ("paragraph", UNREAD, [RESPONDENT], 2, "unread"),
               ("paragraph", "", [RESPONDENT], 2, "no_text"),
               ("heading", DECISION, [DECISION], 2, "read"),
               ("paragraph", ADOPT, [DECISION], 2, "read")]
SKIPPED = {4, 7, 8}  # the blocks of the second appraisal with a status of no text
CITED = 10  # the last block: the sentence the answer cites


@pytest.fixture
def office(db):
    return make_office(db, "משרד ב", "admin-b@example.test")


def _two_appraisals(office, group, sha: str, marker: str = "") -> tuple:
    """A document of two appraisals in one file, its blocks stored in reading order; ``marker`` is written in the cited block."""
    doc, ver = make_document(office, group, "שתי שומות סינתטיות", sha=sha)
    rows = APPRAISAL_A + APPRAISAL_B
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 2, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": READING})})
        for i, (kind, t, path, page, status) in enumerate(rows):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, bbox, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :p,"
                " CAST(:bb AS jsonb), :t, :st)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1] if path else None, "sp": path, "p": page,
                 "bb": json.dumps([50, 40 + 20 * i, 500, 52 + 20 * i]), "t": t + (f" {marker}" if marker and i == CITED else ""),
                 "st": status})
    return doc, ver


def _add(ws: T.Workspace, doc, ver, **kw) -> T.Source:
    base = {"title": "שתי שומות סינתטיות", "section": DECISION, "location": f"סעיף \"{DECISION}\"", "kind": "text", "text": ADOPT,
            "block_start": CITED, "block_end": CITED, "contexts": [2]}
    return ws.add_source(document_id=doc, version_id=ver, **(base | kw))


def _indexes(layout) -> list[int]:
    return [p.index for p in layout.pieces]


def _employee(office, group):
    user = make_user(office, "emp-b@example.test", [group])
    return office.ctx("employee", user)


def test_the_layout_of_a_cited_source_is_the_blocks_of_its_unit_above_it_and_leaves_out_the_blocks_with_no_text(office):
    doc, ver = _two_appraisals(office, office.default_group_id, "a" * 64)
    ws = T.Workspace(ctx=office.ctx())
    _add(ws, doc, ver)
    ContextReader(ws).preload(["S1"])
    layout = ws.layouts["S1"]
    assert isinstance(layout, scopes.Layout) and layout.first == CITED
    # the second appraisal only (the first one's blocks are another unit), without the decorative, unread and no_text blocks
    assert _indexes(layout) == [3, 5, 6, 9, 10] and not SKIPPED & set(_indexes(layout))
    assert [p.text for p in layout.pieces][-1] == ADOPT and all(WATERMARK not in p.text and UNREAD not in p.text for p in layout.pieces)
    # the title blocks of both units of the file, read once, and not the blocks with no text
    assert {k for k in ws.unit_titles} == {(str(ver), 1), (str(ver), 2)}
    assert any("גוש: 40102" in ln for ln in ws.unit_titles[(str(ver), 2)]) and any("גוש: 40100" in ln for ln in ws.unit_titles[(str(ver), 1)])
    assert all(WATERMARK not in ln for lines in ws.unit_titles.values() for ln in lines)
    # a second preload reads nothing again
    before = dict(ws.layouts), dict(ws.unit_titles)
    ContextReader(ws).preload(["S1"])
    assert (dict(ws.layouts), dict(ws.unit_titles)) == before


def test_a_reader_that_fails_leaves_no_layout_and_no_titles_and_raises_nothing(office, monkeypatch):
    doc, ver = _two_appraisals(office, office.default_group_id, "b" * 64)

    def broken(conn, version_id, spans):
        raise RuntimeError("the blocks could not be read")

    monkeypatch.setattr(ContextReader, "_block_rows", staticmethod(broken))
    ws = T.Workspace(ctx=office.ctx())
    _add(ws, doc, ver)
    ContextReader(ws).preload(["S1"])  # does not raise
    assert ws.layouts == {"S1": None}
    assert ws.unit_titles and all(lines == () for lines in ws.unit_titles.values())


def test_a_layout_or_title_range_that_fails_is_none_or_empty_for_that_source_only(office, monkeypatch):
    doc, ver = _two_appraisals(office, office.default_group_id, "c" * 64)
    real = ContextReader._layout_range

    def layout_range(self, conn, head, src):
        if src.sid == "S1":
            raise RuntimeError("no layout for the first source")
        return real(self, conn, head, src)

    monkeypatch.setattr(ContextReader, "_layout_range", layout_range)
    ws = T.Workspace(ctx=office.ctx())
    _add(ws, doc, ver)
    _add(ws, doc, ver, block_start=CITED - 1, block_end=CITED - 1, text=DECISION)
    ContextReader(ws).preload(["S1", "S2"])
    assert ws.layouts["S1"] is None and isinstance(ws.layouts["S2"], scopes.Layout)

    def title_range(self, conn, head, src, number):
        raise RuntimeError("no title block")

    monkeypatch.undo()
    monkeypatch.setattr(ContextReader, "_title_range", title_range)
    again = T.Workspace(ctx=office.ctx())
    _add(again, doc, ver)
    ContextReader(again).preload(["S1"])
    assert isinstance(again.layouts["S1"], scopes.Layout)  # the layout does not depend on the titles
    assert again.unit_titles and all(lines == () for lines in again.unit_titles.values())


def test_a_source_whose_reading_differs_from_the_stored_one_gets_no_layout_and_no_title(office):
    doc, ver = _two_appraisals(office, office.default_group_id, "d" * 64)
    ws = T.Workspace(ctx=office.ctx())
    _add(ws, doc, ver, reading_id=READING)
    _add(ws, doc, ver, reading_id="an-earlier-reading", block_start=CITED - 1, block_end=CITED - 1, text=DECISION)
    ContextReader(ws).preload(["S1", "S2"])
    assert isinstance(ws.layouts["S1"], scopes.Layout) and ws.layouts["S2"] is None  # its block numbers would mean other text
    stale = T.Workspace(ctx=office.ctx())
    _add(stale, doc, ver, reading_id="an-earlier-reading")
    ContextReader(stale).preload(["S1"])
    assert stale.layouts == {"S1": None} and stale.contexts == {}  # nothing of the stored version was read for it
    assert stale.unit_titles and all(lines == () for lines in stale.unit_titles.values())


def test_a_spent_allowance_reads_nothing(office):
    doc, ver = _two_appraisals(office, office.default_group_id, "e" * 64)
    ws = T.Workspace(ctx=office.ctx())
    _add(ws, doc, ver)
    ContextReader(ws).preload(["S1"], deadline=time.monotonic() - 1)  # expired
    assert ws.layouts == {} and ws.unit_titles == {} and ws.contexts == {}
    ContextReader(ws).preload(["S1"], deadline=time.monotonic() + 1)  # less than the least a read needs
    assert ws.layouts == {} and ws.unit_titles == {}
    ContextReader(ws).preload(["S1"], deadline=time.monotonic() + 600)
    assert isinstance(ws.layouts["S1"], scopes.Layout)


def test_a_source_of_a_document_in_a_group_the_user_cannot_read_is_not_preloaded(office):
    seen_group, hidden_group = make_group(office, "קבוצה גלויה"), make_group(office, "קבוצה חסויה")
    ctx = _employee(office, seen_group)
    secret_doc, secret_ver = _two_appraisals(office, hidden_group, "f" * 64, marker="סוד-האלון-8812")
    open_doc, open_ver = _two_appraisals(office, seen_group, "1" * 64, marker="גלוי-הדקל-1")
    ws = T.Workspace(ctx=ctx)
    _add(ws, secret_doc, secret_ver)  # a source the turn held before the document moved out of the user's groups
    _add(ws, open_doc, open_ver)
    ContextReader(ws).preload(["S1", "S2"])
    assert ws.layouts["S1"] is None
    assert all(lines == () for (version, _), lines in ws.unit_titles.items() if version == str(secret_ver))
    # the same reader does read the document the user may see
    assert isinstance(ws.layouts["S2"], scopes.Layout) and any("גלוי-הדקל-1" in p.text for p in ws.layouts["S2"].pieces)
    assert any(lines for (version, _), lines in ws.unit_titles.items() if version == str(open_ver))
    assert str(secret_ver) not in ws.contexts  # the contexts of the hidden version were never derived
    assert "סוד-האלון" not in repr(ws.layouts) + repr(ws.unit_titles)
    # the user is no admin of the hidden group: the hidden source alone, in a turn of its own, reads nothing at all
    alone = T.Workspace(ctx=ctx)
    _add(alone, secret_doc, secret_ver)
    ContextReader(alone).preload(["S1"])
    assert alone.layouts == {"S1": None} and alone.contexts == {}
    assert all(lines == () for lines in alone.unit_titles.values())
