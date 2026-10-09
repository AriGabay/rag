"""Value and measurement statuses without approval (U12, R25, R28): database-free.

A stored measurement (M#) is shown as checked by a person only when a person's review is recorded; one whose place
was lost on reprocessing, or that extraction flagged, is uncertain; anything else was checked automatically. A
calculation states its inputs' statuses and never implies a review is needed; an uncertain input makes the result
conditional. A value whose region was read uncertainly gets a bounded focused re-read through the inspect path.
A search hit is marked partly read only when the page or section it cites has an unread region. Synthetic values
only; the project is invented."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chat import calc, reader
from app.chat import tools as T
from app.extraction.images import PictureReading

SUBJECT = "פרויקט הדגמה"
MSG_UNCERTAIN = T.MSG_UNCERTAIN_INPUTS


# --- labels ----------------------------------------------------------------------------------------------------------

def test_the_four_labels_match_the_frontend_statuses():
    assert T.STATUS_AUTO == "✓ נבדק אוטומטית"
    assert T.STATUS_HUMAN == "✎ אומת או תוקן על ידי אדם"
    assert T.STATUS_UNCERTAIN == "? לא ודאי"
    assert T.STATUS_UNREAD == "∅ לא נקרא"
    assert len({T.STATUS_AUTO, T.STATUS_HUMAN, T.STATUS_UNCERTAIN, T.STATUS_UNREAD}) == 4


def _row(status: str, *, anchor_lost=None, reviewed_by="u1", **kw) -> SimpleNamespace:
    base = {"id": uuid.uuid4(), "document_id": uuid.uuid4(), "version_id": uuid.uuid4(), "title": "שומה סינתטית",
            "metric": "שווי למ\"ר", "metric_kind": "value_per_area", "value": Decimal("9500"), "value_text": "9,500 ₪",
            "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "", "subject": SUBJECT,
            "value_role": "other", "value_form": "exact", "table_index": None, "quote": "השווי 9,500 ₪",
            "section": None, "block_index": 1, "issues": [], "reading_id": None}
    return SimpleNamespace(**(base | {"status": status, "anchor_lost": anchor_lost, "reviewed_by": reviewed_by} | kw))


@pytest.mark.parametrize("status, lost, reviewer, label", [
    ("verified", None, "u1", T.STATUS_HUMAN),
    ("corrected", None, "u1", T.STATUS_HUMAN),
    ("auto_validated", None, None, T.STATUS_AUTO),
    ("needs_review", None, None, T.STATUS_UNCERTAIN),
    ("verified", {"reason": "not found"}, "u1", T.STATUS_UNCERTAIN),  # a person's value, not checked against now
    ("verified", None, None, T.STATUS_AUTO),  # no review recorded: never shown as a person's check
])
def test_a_measurement_is_labelled_by_a_person_only_when_a_review_is_recorded(status, lost, reviewer, label):
    assert T.measurement_status(_row(status, anchor_lost=lost, reviewed_by=reviewer)) == label


# --- calculation note and conditional ----------------------------------------------------------------------------------

def _ws() -> T.Workspace:
    return T.Workspace(ctx=None)


def _measure(ws: T.Workspace, status: str, value: str, *, anchor_lost=None) -> str:
    return ws.add_measurement(_row(status, anchor_lost=anchor_lost, value=Decimal(value), value_text=f"{value} ₪",
                                   reviewed_by="u1" if status in ("verified", "corrected") else None)).mid


def _source(ws: T.Workspace, status: str = "complete") -> str:
    return ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="בדיקת כדאיות", section=None,
                         location="עמוד 1", kind="context", text="הכנסות 5,200,000 עלויות 4,300,000",
                         status=status).sid


def _value(ws: T.Workspace, written: str, sid: str, *, asserted: bool = False, role: str = "other",
           vat: str = "excluded") -> str:
    vid = f"V{len(ws.values) + 1}"
    prov = {"unit": "source", "vat": "model_asserted" if asserted else "source"}
    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, sid, uuid.uuid4(), uuid.uuid4(),
                                None, "בדיקת כדאיות", "עמוד 1", f"ערך {vid}", "income", "ILS", "none", vat, "",
                                SUBJECT, role, prov, {"quote": written}, written)
    return vid


@pytest.fixture
def calc_ws(monkeypatch) -> T.Workspace:
    monkeypatch.setattr(T, "_check_available", lambda ws, leaves: None)  # visibility is the database's (integration)
    return _ws()


def test_the_calculation_note_states_each_inputs_status_and_never_asks_for_a_review(calc_ws):
    a = _measure(calc_ws, "auto_validated", "9500")
    b = _measure(calc_ws, "verified", "9700")
    out = json.loads(T.tool_calculate(calc_ws, f"{a} + {b}", "סכום"))
    assert "טרם אומתו" not in out["note"] and "נתון ראשוני" not in out["note"]
    assert f"{a} {T.STATUS_AUTO}" in out["note"] and f"{b} {T.STATUS_HUMAN}" in out["note"]
    assert out["conditional"] is False


def test_an_uncertain_measurement_makes_the_result_conditional_not_certain(calc_ws):
    a = _measure(calc_ws, "auto_validated", "9500")
    b = _measure(calc_ws, "needs_review", "9700")
    out = json.loads(T.tool_calculate(calc_ws, f"{a} + {b}", "סכום"))
    assert out["conditional"] is True and f"{b} {T.STATUS_UNCERTAIN}" in out["note"]
    assert "מותנה" in out["note"] and b in out["note"] and "לפי ההצדקה" not in out["note"]
    c = calc_ws.computations[out["id"]]
    assert c.conditional and any(b in x for x in c.outcome.conditional) and c.justification is None


def test_a_measurement_whose_place_was_lost_is_uncertain_in_a_calculation(calc_ws):
    a = _measure(calc_ws, "verified", "9500", anchor_lost={"reason": "not found"})
    b = _measure(calc_ws, "verified", "9700")
    out = json.loads(T.tool_calculate(calc_ws, f"{a} + {b}", "סכום"))
    assert out["conditional"] is True and f"{a} {T.STATUS_UNCERTAIN}" in out["note"]
    assert f"{b} {T.STATUS_HUMAN}" in out["note"]


@pytest.mark.parametrize("case", ["asserted", "uncertain_source", "unclear_after_reread"])
def test_an_uncertain_value_makes_the_result_conditional(calc_ws, case):
    s_ok = _source(calc_ws)
    s = _source(calc_ws, "uncertain_reading" if case == "uncertain_source" else "complete")
    v1 = _value(calc_ws, "5,200,000", s_ok, role="income")
    v2 = _value(calc_ws, "4,300,000", s, asserted=case == "asserted", role="other")
    if case == "unclear_after_reread":
        calc_ws.uncertain_values[v2] = "קריאה לא ודאית"
    assert T.uncertain_inputs(calc_ws, [v1, v2]) == [v2]
    out = json.loads(T.tool_calculate(calc_ws, f"{v1} - {v2}", "הפרש"))
    assert out["conditional"] is True and v2 in "; ".join(calc_ws.computations[out["id"]].outcome.conditional)
    assert f"{v1} {T.STATUS_AUTO}" in out["note"] and f"{v2} {T.STATUS_UNCERTAIN}" in out["note"]


def test_certain_values_give_a_certain_result(calc_ws):
    s = _source(calc_ws)
    v1, v2 = _value(calc_ws, "5,200,000", s), _value(calc_ws, "4,300,000", s)
    out = json.loads(T.tool_calculate(calc_ws, f"{v1} - {v2}", "הפרש"))
    assert out["conditional"] is False and "מותנה" not in out["note"]
    assert T.value_status(calc_ws, v1) == T.STATUS_AUTO


def test_a_justified_mix_keeps_its_justification_next_to_the_uncertainty(calc_ws):
    s, s_ok = _source(calc_ws, "uncertain_reading"), _source(calc_ws)
    v1 = _value(calc_ws, "5,200,000", s)
    v2 = _value(calc_ws, "4,300,000", s_ok, vat="included")
    out = json.loads(T.tool_calculate(calc_ws, f"{v1} + {v2}", "סכום", "המשתמש ביקש לחבר"))
    assert out["conditional"] is True and "לפי ההצדקה: המשתמש ביקש לחבר" in out["note"]
    assert MSG_UNCERTAIN.format(ids=v1) in out["note"]
    assert calc_ws.computations[out["id"]].justification == "המשתמש ביקש לחבר"


# --- a focused re-read of an unclear value -------------------------------------------------------------------------------

PDF = reader.Version(uuid.uuid4(), uuid.uuid4(), "שומה סינתטית", "reading-1", "application/pdf", True, 2, False,
                     "k", "f.pdf")
DOCX = reader.Version(uuid.uuid4(), uuid.uuid4(), "שומה סינתטית", "reading-1",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True, None, False,
                      "k", "f.docx")


def _block(page=1, bbox=(10.0, 20.0, 200.0, 60.0)) -> SimpleNamespace:
    return SimpleNamespace(block_index=4, page=page, bbox=list(bbox) if bbox else None, status=reader.UNCERTAIN,
                           section="5. תחשיב", media=None, table_index=0, text="48,600", method="vision", note="")


@pytest.fixture
def vision(monkeypatch):
    """The inspect path's visual reading, scripted: each call returns the next reading and is counted."""
    calls: list = []
    readings: list[PictureReading] = []

    def fake(ws, spot):
        calls.append(spot)
        return readings.pop(0), False

    monkeypatch.setattr(T, "_vision_read", fake)
    # OCR of the crop is what confirms a re-read (checked inside _vision_read); without it no re-read is made
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    return SimpleNamespace(calls=calls, readings=readings)


FORMS = frozenset({"48600"})


def test_an_unclear_value_is_re_read_at_most_twice_and_then_reported_uncertain(vision):
    ws = _ws()
    vision.readings += [PictureReading("read_uncertain", "vision", "48,600"),
                        PictureReading("read_uncertain", "vision", "48,600")]
    key = ("v", "cell", 1)
    results = [T._reread_value(ws, PDF, _block(), FORMS, key) for _ in range(3)]
    assert [ok for ok, _ in results] == [False, False, False]
    assert len(vision.calls) == 2 and ws.rereads[key] == 2
    assert vision.calls[0].region == "block:4" and vision.calls[0].bbox == [10.0, 20.0, 200.0, 60.0]


def test_a_clear_re_read_that_holds_the_number_settles_the_value(vision):
    ws = _ws()
    vision.readings.append(PictureReading("read", "vision", "תפוסה 48,600"))
    ok, _ = T._reread_value(ws, PDF, _block(), FORMS, ("v", 1))
    assert ok and len(vision.calls) == 1


def test_a_clear_re_read_without_the_number_does_not_settle_it(vision):
    ws = _ws()
    vision.readings.append(PictureReading("read", "vision", "תפוסה 46,800"))
    ok, _ = T._reread_value(ws, PDF, _block(), FORMS, ("v", 1))
    assert not ok


def test_a_region_without_a_box_is_re_read_as_its_page(vision):
    ws = _ws()
    vision.readings.append(PictureReading("read", "vision", "48,600"))
    T._reread_value(ws, PDF, _block(bbox=None), FORMS, ("v", 1))
    assert vision.calls[0].region == "page:1" and vision.calls[0].bbox is None


def test_a_value_that_cannot_be_re_read_is_uncertain_without_a_call(vision, monkeypatch):
    ws = _ws()
    ok, _ = T._reread_value(ws, DOCX, _block(page=None), FORMS, ("v", 1))
    assert not ok and vision.calls == []

    def refused(ws, spot):
        raise T.ToolError("קריאה חזותית אינה זמינה במשרד הזה")

    monkeypatch.setattr(T, "_vision_read", refused)
    ok, note = T._reread_value(ws, PDF, _block(), FORMS, ("v", 2))
    assert not ok and note
    monkeypatch.setattr(T, "_vision_read", lambda ws, spot: "מגבלה: בתור הזה כבר נעשו 3 קריאות חזותיות")
    ok, note = T._reread_value(ws, PDF, _block(), FORMS, ("v", 3))
    assert not ok and "מגבלה" in note


def test_without_ocr_no_re_read_is_made_since_nothing_would_confirm_it(vision, monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    ok, note = T._reread_value(_ws(), PDF, _block(), FORMS, ("v", 1))
    assert not ok and vision.calls == [] and "אין OCR" in note


def test_a_re_read_is_checked_against_the_ocr_of_its_crop(monkeypatch):
    from app.extraction import vision as V
    from app.extraction.images import VisionOut, VisionTableOut

    class Reader:
        def read(self, png, context, careful=False, deadline=None):
            return VisionOut("table", True, "", [VisionTableOut("", ["אזור", "דמי שכירות"], [["צפון", "48,600"]], [])],
                             "טבלה", [])

    assert V.transcribe(Reader(), b"", ocr_words=["צפון", "48,600"]).status == "read"
    assert V.transcribe(Reader(), b"", ocr_words=["צפון", "48,800"]).status == "read_uncertain"
    assert V.transcribe(Reader(), b"").status == "read_uncertain"  # nothing independent confirmed it


def test_the_blocks_of_a_value_come_from_its_anchor():
    assert T._value_blocks({"segments": [[3, 0, 5], [4, 0, 9]], "number": [4, 2, 8]}) == [4]
    assert T._value_blocks({"blocks": [6, 7]}) == [6, 7]
    assert T._value_blocks({}) is None
    assert T._value_blocks({"table_index": 0, "row": 1, "column": 2}) is None


# --- partial labels on a hit ------------------------------------------------------------------------------------------

def _coverage(*entries) -> dict:
    return {"partial": True, "coverage": list(entries)}


def _entry(page, *regions, ok=True) -> dict:
    return {"page": page, "ok": ok, "method": None, "corrected": 0,
            "regions": [{"block": 1, "kind": "image", "status": s, "reason": None, "bbox": None, "section": sec,
                         "media": None} for s, sec in regions]}


def test_a_hit_on_a_page_without_unread_regions_is_not_partial():
    gaps = T.ReadingGaps.of(_coverage(_entry(7, ("unread", "נספחים"))), [], True)
    assert gaps.partial  # the document is partly read (for absence) ...
    assert not gaps.cites([3], "2. תיאור הנכס")  # ... but the cited page is whole
    assert gaps.cites([6, 7], None)


def test_an_uncertain_region_alone_does_not_make_a_hit_partial():
    gaps = T.ReadingGaps.of(_coverage(_entry(3, ("read_uncertain", None))), [], True)
    assert not gaps.cites([3], None)


def test_a_page_whose_reading_failed_makes_a_hit_on_it_partial():
    gaps = T.ReadingGaps.of({"partial": True}, [4], True)
    assert gaps.cites([4], None) and not gaps.cites([5], None)
    gaps = T.ReadingGaps.of(_coverage(_entry(5, ok=False)), [], True)
    assert gaps.cites([5], None)


def test_a_document_without_pages_is_partial_only_in_the_section_with_the_unread_region():
    gaps = T.ReadingGaps.of(_coverage(_entry(None, ("unread", "3. נספחים"))), [], True)
    assert gaps.cites(None, "3. נספחים") and not gaps.cites(None, "2. תיאור הנכס")
    unplaced = T.ReadingGaps.of(_coverage(_entry(None, ("unread", None))), [], True)
    assert unplaced.cites(None, "2. תיאור הנכס")  # where it is is unknown: it may be the cited part


def test_an_older_report_without_coverage_uses_its_unread_pictures():
    ing = {"partial": True, "unread": [{"media": "img1.png", "section": None, "reason": "x", "page": 2}]}
    gaps = T.ReadingGaps.of(ing, [], True)
    assert gaps.cites([2], None) and not gaps.cites([1], None)


def test_a_fully_read_document_is_never_partial():
    gaps = T.ReadingGaps.of({"partial": False}, [], False)
    assert not gaps.partial and not gaps.cites([1], None) and not gaps.cites(None, None)
