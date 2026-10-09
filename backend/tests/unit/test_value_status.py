"""Value and measurement statuses without approval (U12, R25, R28): database-free.

A stored measurement (M#) is shown as checked by a person only when a person's review is recorded; one whose place
was lost on reprocessing, or that extraction flagged, is uncertain; anything else was checked automatically. A
calculation states its inputs' statuses and never implies a review is needed; an uncertain input makes the result
conditional. A value whose region was read uncertainly gets a bounded focused re-read through the inspect path.
A search hit is marked partly read only when the page or section it cites has an unread region. A cell of a table
read by inspect is confirmed only when OCR (or the region's text layer) sees its number once in its row's and
column's bands; its OCR box maps into the rendered page, and its anchor is cell-precise only with that box (U5,
KTD6, R18, R19). Synthetic values only; the project is invented."""

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


# --- a table read by inspect: each cell confirmed by OCR in its place (U5, KTD6, R18, R19) -----------------------------
#
# The OCR words are scripted at the OCR boundary (the host's Tesseract has no Hebrew data): a small table laid out on a
# grid in the crop's OCR pixels, the columns right to left. The placement is the server's: the number's OCR box in the
# row band of its row label and in the column band of its header, both located by OCR word boxes.

from app.chat import anchors  # noqa: E402
from app.extraction import images as I  # noqa: E402

HEAD = ["רכיב", "עלות למ״ר (₪)", "עלות (₪)"]
BODY = [["בנייה עילית", "6,500", "15,600,000"], ["חניון תת-קרקעי", "4,200", "4,620,000"]]
COLUMNS = [(820, 1000), (450, 700), (60, 360)]  # x bands of the columns, right to left (OCR pixels)


def _word(text: str, x0: float, y0: float, x1: float, y1: float, conf: float = 95.0) -> dict:
    return {"text": text, "conf": conf, "left": x0, "top": y0, "width": x1 - x0, "height": y1 - y0}


def _cell_words(text: str, column: int, line: int) -> list[dict]:
    """A cell's words, right-aligned in its column, on its line (40 px high, 60 px apart)."""
    out, right = [], COLUMNS[column][1] - 10
    for token in text.split():
        width = 18 * len(token)
        out.append(_word(token, right - width, 20 + 60 * line, right, 60 + 60 * line))
        right -= width + 10
    return out


def _layout(rows=BODY, drop=(), extra=()) -> list[dict]:
    """The header on line 0 and each row below it; ``drop``: numbers OCR does not see; ``extra``: more words."""
    words = [w for j, h in enumerate(HEAD) for w in _cell_words(h, j, 0)]
    for i, row in enumerate(rows, 1):
        words += [w for j, c in enumerate(row) for w in _cell_words(c, j, i) if w["text"] not in drop]
    return words + list(extra)


def _table(rows=BODY) -> list[I.PictureTable]:
    return [I.PictureTable(headers=list(HEAD), rows=[list(r) for r in rows])]


def test_a_number_ocr_sees_once_in_its_rows_band_and_its_columns_band_is_confirmed_with_its_box():
    cells = I.cell_evidence(_table(), _layout())
    first = cells[0][0][2]
    assert first["status"] == I.CELL_CONFIRMED and first["by"] == "ocr"
    expected = next(w for w in _layout() if w["text"] == "15,600,000")
    assert first["box"] == [expected["left"], expected["top"], expected["left"] + expected["width"],
                            expected["top"] + expected["height"]]
    assert cells[0][1][2]["status"] == I.CELL_CONFIRMED and cells[0][1][1]["status"] == I.CELL_CONFIRMED
    assert cells[0][0][0] is None  # a label holds no number: nothing to confirm


def test_headers_sharing_a_word_are_told_apart_by_all_their_words():
    cells = I.cell_evidence(_table(), _layout())
    assert cells[0][0][1]["status"] == I.CELL_CONFIRMED  # under «עלות למ״ר (₪)», not «עלות (₪)»


def test_a_number_ocr_does_not_see_is_not_confirmed():
    cells = I.cell_evidence(_table(), _layout(drop=("4,620,000",)))
    assert cells[0][1][2] == {"status": I.CELL_NOT_SEEN, "box": None, "by": None}
    assert cells[0][0][2]["status"] == I.CELL_CONFIRMED


def test_a_number_ocr_sees_twice_in_the_crop_is_not_confirmed_and_has_no_box():
    twice = _word("15,600,000", 60, 400, 240, 440)  # also in a line under the table
    cells = I.cell_evidence(_table(), _layout(extra=[twice]))
    assert cells[0][0][2] == {"status": I.CELL_REPEATED, "box": None, "by": None}


def test_values_the_transcription_swapped_between_rows_are_seen_by_ocr_but_not_placed():
    swapped = [["בנייה עילית", "6,500", "4,620,000"], ["חניון תת-קרקעי", "4,200", "15,600,000"]]
    cells = I.cell_evidence(_table(swapped), _layout())  # OCR sees the page as it is
    assert cells[0][0][2]["status"] == I.CELL_NOT_PLACED and cells[0][1][2]["status"] == I.CELL_NOT_PLACED
    assert cells[0][0][2]["box"] is None


def test_a_row_whose_label_ocr_cannot_locate_leaves_its_numbers_unplaced():
    cells = I.cell_evidence(_table(), _layout(drop=("חניון", "תת-קרקעי")))
    assert cells[0][1][2]["status"] == I.CELL_NOT_PLACED and cells[0][0][2]["status"] == I.CELL_CONFIRMED


def test_the_regions_text_layer_is_accepted_under_the_same_placement_test():
    layer = _layout()
    cells = I.cell_evidence(_table(), [], layer)
    assert cells[0][0][2]["status"] == I.CELL_CONFIRMED and cells[0][0][2]["by"] == "text_layer"
    swapped = [["בנייה עילית", "6,500", "4,620,000"], ["חניון תת-קרקעי", "4,200", "15,600,000"]]
    assert I.cell_evidence(_table(swapped), [], layer)[0][0][2]["status"] == I.CELL_NOT_PLACED


def test_the_reading_keeps_per_number_ocr_evidence_and_without_ocr_says_so():
    reading = I.PictureReading("read", "vision", tables=_table(), kind="table")
    ev = I.ocr_evidence(reading, _layout(drop=("4,620,000",)))
    assert ev["available"] is True
    seen = {n["number"]: n["seen"] for n in ev["numbers"]}
    assert seen["15600000"] == 1 and seen["4620000"] == 0
    assert ev["cells"][0][1][2]["status"] == I.CELL_NOT_SEEN
    none = I.ocr_evidence(reading, None)
    assert none["available"] is False and none["cells"][0][0][2]["status"] == I.CELL_NO_OCR
    # stored with the reading and read back; a reading without it keeps its old JSON
    back = I.PictureReading.from_json(json.loads(I.PictureReading("read", "vision", ocr=ev).to_json()))
    assert back.ocr == ev
    assert "ocr" not in json.loads(I.PictureReading("read", "vision").to_json())


def test_an_ocr_box_is_mapped_into_the_rendered_page_by_the_upscale_the_render_scale_and_the_crop_origin():
    frame = {"origin": [100.0, 200.0], "scale": 4.0, "upscale": 1.25}
    # 50 points right and 10 down of the crop's corner, 20 x 5 points: in OCR pixels x 4 (render) x 1.25 (OCR)
    assert anchors.page_box([250, 50, 350, 75], frame) == [150.0, 210.0, 170.0, 215.0]
    assert anchors.page_box(None, frame) is None


def _vision_reading(block_bbox=(99.2, 249.4, 552.8, 417.9)) -> anchors.Reading:
    vid = str(uuid.uuid4())
    page = anchors.Page(1, 595.28, 841.89, [0, 0, 595.28, 841.89], [0, 0, 595.28, 841.89], 0)
    block = anchors.Block(8, "image", 1, "", list(block_bbox), None, ("2. עלויות הבנייה",))
    return anchors.Reading(str(uuid.uuid4()), vid, "שומה סינתטית", "f.pdf", True, "r1", {1: page}, {8: block})


def _vision_stub(reading: anchors.Reading, box) -> dict:
    return {"kind": "cell", "document_id": reading.document_id, "version_id": reading.version_id, "reading_id": "r1",
            "block_start": 8, "block_end": 8, "pages": [1], "section": "2. עלויות הבנייה",
            "vision": {"region": "block:8", "table": 0, "row": 0, "column": 2, "page": 1, "cell_box": box,
                       "title": None, "row_label": "בנייה עילית", "row_number": 1, "column_header": "עלות (₪)",
                       "column_number": 3, "unit_note": "(*) הסכומים בש״ח", "notes": ["(*) הסכומים בש״ח"]}}


def test_a_vision_cell_with_its_box_is_highlighted_at_cell_precision_with_its_table_context():
    reading = _vision_reading()
    snap = anchors.snapshot(_vision_stub(reading, [400.0, 300.0, 450.0, 310.0]), reading, "r1")
    assert snap["precision"] == "cell" and snap["degraded"] is None
    assert snap["pages"][0]["rects"] == [[round(400 / 595.28, 4), round(300 / 841.89, 4), round(450 / 595.28, 4),
                                          round(310 / 841.89, 4)]]
    t = snap["table"]
    assert (t["row_label"], t["column_header"], t["source"], t["table_index"]) == ("בנייה עילית", "עלות (₪)",
                                                                                     "vision", None)
    assert "שורה «בנייה עילית»" in snap["location"]["label"]


def test_a_vision_cell_without_a_box_is_highlighted_as_its_whole_table_with_context():
    reading = _vision_reading()
    snap = anchors.snapshot(_vision_stub(reading, None), reading, "r1")
    assert (snap["precision"], snap["region"], snap["degraded"]) == ("region", "table", anchors.NO_CELL_BOX)
    assert snap["pages"][0]["rects"] == [[round(99.2 / 595.28, 4), round(249.4 / 841.89, 4),
                                          round(552.8 / 595.28, 4), round(417.9 / 841.89, 4)]]
    assert snap["table"]["row_label"] == "בנייה עילית" and snap["table"]["unit_note"] == "(*) הסכומים בש״ח"
