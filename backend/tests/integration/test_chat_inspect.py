"""The visual inspection tool (U9): ``inspect`` reads a region (``R#``) or a page that ingestion did not read.

The vision model is scripted; these tests prove what the server does: a region or page read at ingestion returns
that reading without a model call; an unread one is rendered and read once, stored per version, reading and
region, and served from storage to a later turn without another call; permission is checked on every call, so a
user outside the document's group gets "not available" even when a stored reading exists; the per-turn cap ends
in a visible limitation; an office without cloud reading refuses with the reason; and a scripted turn cites the
transcription. Synthetic documents only."""

from __future__ import annotations

import io
import json
import re

import pytest
from PIL import Image
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction.images import VisionOut, VisionTableOut
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, final, read
from tests.unit.test_regions import HEADERS, R1, ROWS

pytestmark = pytest.mark.db

TITLE = "שומה סינתטית עם טבלה סרוקה"
OFFICE_READER = T.inspect_reader  # the office's own vision reader (None unless the office allows cloud reading)


class ScriptedVision:
    """A vision reader that transcribes the synthetic table and counts its calls."""

    config = "scripted-vision:low"

    def __init__(self, uncertain: list[str] | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.uncertain = uncertain or []
        self.usage = None
        self.deadlines: list[float | None] = []

    def read(self, png: bytes, context: str, careful: bool = False, deadline: float | None = None) -> VisionOut:
        self.calls.append(Image.open(io.BytesIO(png)).size)
        self.deadlines.append(deadline)
        return VisionOut("table", True, "", [VisionTableOut("", HEADERS, [list(r) for r in ROWS], [])], "טבלה",
                         list(self.uncertain))


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.outsider = make_user(a, "outsider@example.test", [a.default_group_id])
    a.vision = ScriptedVision()
    monkeypatch.setattr(T, "inspect_reader", lambda ctx: a.vision)
    return a


def ingest_r1(office, monkeypatch, at_ingestion=None) -> str:
    """The synthetic raster-table PDF, ingested without OCR (and without vision unless ``at_ingestion``), in the
    closed group."""
    from app.platform import pipeline
    from tests.integration.test_documents_api import ingest

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: at_ingestion)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    doc, _ = ingest(office, R1, TITLE)
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE documents SET group_id = :g WHERE id = :d"), {"g": office.private, "d": doc})
    return doc


def region_of(ws, doc: str, marker: str = "אזור שלא נקרא") -> str:
    out = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    m = re.search(r"\[" + marker + r" (R\d+)", out)
    assert m, out
    return m.group(1)


def tag(output: str, attr: str) -> str | None:
    m = re.search(rf'<source [^>]*\b{attr}="([^"]*)"', output)
    return m.group(1) if m else None


def stored(office) -> int:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT count(*) FROM region_readings")).scalar_one()


def inspect_views(office) -> int:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'source_view'"
                                 " AND details->>'tool' = 'inspect'")).scalar_one()


def emp(office) -> T.Workspace:
    return T.Workspace(ctx=office.ctx("employee", office.emp))


# --- a reading made at ingestion -----------------------------------------------------------------------------------

def test_a_region_read_at_ingestion_returns_its_stored_reading_without_a_model_call(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch, at_ingestion=ScriptedVision(uncertain=["תפוסה"]))
    ws = emp(office)
    region = region_of(ws, doc, marker="קריאה לא ודאית")
    out = T.tool_inspect(ws, {"region": region})
    assert office.vision.calls == [] and stored(office) == 0
    assert tag(out, "status") == "uncertain_reading" and "48,600" in out and "נקרא בעיבוד המסמך" in out
    s = ws.sources[tag(out, "id")]
    assert s.status == "uncertain_reading" and s.page_list == [1] and s.more is None and "48,600" in s.text
    # the page was read at ingestion too: its stored reading, through the same reader as ``read``
    page = T.tool_inspect(emp(office), {"document": doc, "page": 1})
    assert office.vision.calls == [] and "48,600" in page and "נקרא בעיבוד המסמך" in page


# --- an unread region: one model call, stored for later turns ------------------------------------------------------

def test_the_first_inspect_makes_one_call_and_a_later_turn_none(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    region = region_of(ws, doc)
    out = T.tool_inspect(ws, {"region": region})
    x0, _, x1, _ = ws.handles[region]["bbox"]
    assert len(office.vision.calls) == 1 and office.vision.calls[0][0] >= (x1 - x0) * 4  # about 300 dpi: legible
    assert tag(out, "status") == "uncertain_reading" and tag(out, "more") is None and "48,600" in out
    s = ws.sources[tag(out, "id")]
    assert (s.status, s.method, s.more, s.clipped, s.page_list) == ("uncertain_reading", "vision", None, False, [1])
    assert s.public()["method"] == "vision" and "עמוד 1" in s.location
    assert "48,600" in s.text and "דמי שכירות" in s.text
    assert stored(office) == 1 and inspect_views(office) == 1
    assert ws.inspections == 1

    again = T.tool_inspect(ws, {"region": region})  # the same region in the same turn: no call, no second audit
    assert len(office.vision.calls) == 1 and tag(again, "same_as") == s.sid and inspect_views(office) == 1

    later = emp(office)  # a later turn: the stored reading, no model call
    out2 = T.tool_inspect(later, {"region": region_of(later, doc)})
    assert len(office.vision.calls) == 1 and "48,600" in out2 and tag(out2, "status") == "uncertain_reading"
    assert later.inspections == 0 and stored(office) == 1 and inspect_views(office) == 2


def test_a_page_with_an_unread_region_is_read_whole_once(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    out = T.tool_inspect(ws, {"document": doc, "page": 1})
    assert len(office.vision.calls) == 1 and "48,600" in out and tag(out, "status") == "uncertain_reading"
    assert "העמוד כולו" in ws.sources[tag(out, "id")].location
    T.tool_inspect(emp(office), {"document": doc, "page": 1})
    assert len(office.vision.calls) == 1 and stored(office) == 1


# --- permissions ---------------------------------------------------------------------------------------------------

def test_a_user_outside_the_documents_group_gets_not_available_even_when_a_reading_is_stored(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    region = region_of(ws, doc)
    T.tool_inspect(ws, {"region": region})
    assert stored(office) == 1 and len(office.vision.calls) == 1

    outsider = T.Workspace(ctx=office.ctx("employee", office.outsider))
    with pytest.raises(T.ToolError) as e:
        T.tool_inspect(outsider, {"document": doc, "page": 1})
    assert "אינו זמין" in str(e.value) and TITLE not in str(e.value)
    assert outsider.sources == {} and len(office.vision.calls) == 1

    # access revoked between two calls: the handle the turn holds opens nothing, stored reading or not
    later = emp(office)
    handle = region_of(later, doc)
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u AND group_id = :g"),
                     {"u": office.emp, "g": office.private})
    with pytest.raises(T.ToolError) as e:
        T.tool_inspect(later, {"region": handle})
    assert "אינו זמין" in str(e.value) and TITLE not in str(e.value) and len(office.vision.calls) == 1


# --- limits and refusals -------------------------------------------------------------------------------------------

def test_the_cap_stops_further_inspections_with_a_visible_limitation(office, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_max_inspections", 1)
    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    region = region_of(ws, doc)
    T.tool_inspect(ws, {"region": region})
    before = set(ws.sources)
    out = T.run_tool(ws, "inspect", json.dumps({"target": {"region": None, "document": doc, "page": 1}}))
    assert "מגבלה" in out and "1" in out and "לא נקרא" in out
    assert len(office.vision.calls) == 1 and set(ws.sources) == before and ws.limits_hit == ["inspect_cap"]
    # a stored reading costs no model call: the cap does not stop it
    T.tool_inspect(ws, {"region": region})
    assert len(office.vision.calls) == 1


def test_an_office_without_cloud_reading_refuses_with_the_reason(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    monkeypatch.setattr(T, "inspect_reader", OFFICE_READER)
    ws = emp(office)
    region = region_of(ws, doc)
    with pytest.raises(T.ToolError) as e:
        T.tool_inspect(ws, {"region": region})
    assert "ענן" in str(e.value) and "לא נקרא" in str(e.value)
    assert stored(office) == 0 and office.vision.calls == []


def test_a_target_names_a_region_or_a_page_of_a_pdf(office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    for target in ({"region": None, "document": None, "page": None}, {"region": "R1", "document": doc, "page": 1},
                   {"region": "R9", "document": None, "page": None}, {"document": doc, "page": 11},
                   {"document": doc, "page": 0}):
        with pytest.raises(T.ToolError):
            T.tool_inspect(ws, target)
    assert office.vision.calls == []


# --- a scripted turn -----------------------------------------------------------------------------------------------

def _last_output(items) -> str:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]


def _turn(doc: str) -> ScriptedAgent:
    def inspect_region(items):
        region = re.search(r"\[אזור שלא נקרא (R\d+)", _last_output(items)).group(1)
        return [call("inspect", target={"region": region, "document": None, "page": None})]

    def answer(items):
        sid = re.findall(r'<source id="(S\d+)"', _last_output(items))[-1]
        return final(f"דמי השכירות באזור צפון הם 48,600 ₪ [{sid}].", documents=[doc])

    return ScriptedAgent([[read(pages={"document": doc, "from_page": 1, "to_page": 1})], inspect_region, answer])


def test_a_scripted_turn_reads_an_unread_table_region_through_inspect_and_cites_it(client, office, monkeypatch):
    doc = ingest_r1(office, monkeypatch)
    agent = _turn(doc)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות באזור צפון?")
    a = m["answer"]
    assert m["status"] == "done" and a["status"] == "answered" and "48,600" in a["markdown"], m
    cited = next(s for s in a["sources"] if s["id"] == "S2")
    assert cited["status"] == "uncertain_reading" and cited["method"] == "vision" and cited["document_id"] == doc
    assert len(office.vision.calls) == 1

    agent = _turn(doc)  # a later turn: the same region, no model call
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, new_conversation(client), "מה דמי השכירות באזור צפון?")
    assert m["answer"]["status"] == "answered" and "48,600" in m["answer"]["markdown"]
    assert len(office.vision.calls) == 1


# --- the turn's reading deadline -------------------------------------------------------------------------------------

def test_an_inspection_is_bounded_by_the_turns_reading_deadline(office, monkeypatch):
    import time

    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    ws.read_until = time.monotonic() + 600
    T.tool_inspect(ws, {"region": region_of(ws, doc)})
    assert office.vision.deadlines == [ws.read_until]  # the call is cut to the reading window, without retries


class Clock:
    """``time.monotonic`` moved forward by hand: a slow model call is a jump of the clock."""

    def __init__(self, monkeypatch):
        import time

        self.offset, real = 0.0, time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + self.offset)


def test_an_inspection_near_the_reading_deadline_makes_no_call_and_the_turn_ends_partial(client, office, monkeypatch):
    """At 80 of the turn's 110 reading seconds, an inspection would need longer than is left. Made, a slow vision
    call (120 s) would leave no time to verify the answer (verify_no_time); refused, the turn ends as a verified
    partial answer that names the time limit."""
    from app.config import get_settings

    for k, v in {"chat_turn_seconds": 150, "chat_verify_reserve_seconds": 40, "chat_verify_min_seconds": 15}.items():
        monkeypatch.setattr(get_settings(), k, v)
    monkeypatch.setattr(get_settings(), "chat_inspect_reserve_seconds", 45, raising=False)
    doc = ingest_r1(office, monkeypatch)
    clock = Clock(monkeypatch)
    slow_read = office.vision.read

    def slow(*args, **kwargs):
        clock.offset += 120
        return slow_read(*args, **kwargs)

    monkeypatch.setattr(office.vision, "read", slow)

    def inspect_region(items):
        clock.offset += 80  # the turn has read for 80 seconds
        region = re.search(r"\[אזור שלא נקרא (R\d+)", _last_output(items)).group(1)
        return [call("inspect", target={"region": region, "document": None, "page": None})]

    answer = final("דמי השכירות באזור צפון לא נקראו: הטבלה בעמוד הראשון לא נקראה.", "not_found")
    agent = ScriptedAgent([[read(pages={"document": doc, "from_page": 1, "to_page": 1})], inspect_region,
                           answer, answer, answer])  # the same answer to a repair round, if verification asks
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה דמי השכירות באזור צפון?")
    assert m["status"] == "done", m.get("error")
    assert office.vision.calls == []
    refused = _last_output(agent.seen[2])
    assert "מגבלה" in refused and "לא נקרא" in refused
    a = m["answer"]
    assert a["status"] == "partial" and "time_limit" in a["limits_hit"] and "מגבלת הזמן לשאלה אחת" in a["markdown"]


def test_a_region_is_cropped_in_the_rendered_pages_frame_like_the_region_view(office, monkeypatch):
    """The stored box is in the text reader's frame; the crop sent to the model is converted with the page's stored
    geometry (a rotated page or one with a CropBox offset would otherwise be cut in the wrong place)."""
    from app.extraction import render

    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    region = region_of(ws, doc)
    converted: list = []
    cropped: list = []

    def display_box_of(conn, version_id, page_no, bbox):
        converted.append(list(bbox))
        return [1.0, 2.0, 3.0, 4.0]

    real = render.render_png
    monkeypatch.setattr(T, "display_box_of", display_box_of)
    monkeypatch.setattr(render, "render_png", lambda data, page, bbox, **kw: cropped.append(bbox) or real(
        data, page, None, **kw))
    T.tool_inspect(ws, {"region": region})
    assert converted and cropped == [[1.0, 2.0, 3.0, 4.0]]


def test_a_reading_stored_by_an_older_inspect_reader_is_read_again(office, monkeypatch):
    """A region read on demand before its crop was cut in the rendered page's frame (or checked against OCR) may
    show another place on a rotated or cropped page: the newer reader reads it again rather than reuse it."""
    from sqlalchemy import text

    from app.extraction.vision import INSPECT_READER_VERSION

    doc = ingest_r1(office, monkeypatch)
    ws = emp(office)
    region = region_of(ws, doc)
    T.tool_inspect(ws, {"region": region})
    assert len(office.vision.calls) == 1
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE region_readings SET reader_version = 'inspect-v1'"))
    assert INSPECT_READER_VERSION != "inspect-v1"
    later = emp(office)
    T.tool_inspect(later, {"region": region_of(later, doc)})
    assert len(office.vision.calls) == 2
