"""DOCX reading in blocks: sections, text boxes, Word tables, pictures (EMF and raster), chunks with locations.

Uses the synthetic fixture written by scripts/make_chat_fixture.py (invented content)."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.extraction import images
from app.extraction.docx import extract_docx
from app.extraction.images import VisionOut, VisionTableOut, read_raster

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "chat" / "CHAT_synthetic_mixed_report.docx"


@pytest.fixture(scope="module")
def result():
    return extract_docx(FIXTURE.read_bytes(), time.monotonic() + 60, get_settings())


def test_no_pages_are_invented(result):
    assert result.page_count is None and result.is_docx
    assert all(c.page_list is None for c in result.chunks)


def test_sections_come_from_numbered_headings(result):
    headings = [b.text for b in result.blocks if b.kind == "heading"]
    assert headings[:3] == ["פרטי הנכס", "סקר שוק", "נתוני היצע לדמי שכירות"]
    statement = next(b for b in result.blocks if "9,500" in b.text)
    assert statement.section == "סיכום סקר שוק" and statement.section_path[-1] == "סיכום סקר שוק"
    assert statement.paragraph_no is not None


def test_text_box_is_read_once(result):
    boxes = [b for b in result.blocks if b.kind == "textbox"]
    assert [b.text for b in boxes] == ["רחוב הגפן 12, עיר לדוגמה"]


def test_emf_picture_table_keeps_header_rows_and_caption(result):
    pic = next(b for b in result.blocks if b.kind == "image")
    assert (pic.status, pic.source, pic.media) == ("read", "emf", "image1.emf")
    table = next(t for t in result.tables if t.source == "emf")
    assert table.headers == ["כתובת", 'שטח במ"ר', 'שכ"ד חודשי', 'שכ"ד למ"ר']
    assert table.rows[1].cells == ["הזית 7", "80", "₪ 4,880", "₪ 61"]
    assert table.caption == "להלן נתוני היצע לדמי שכירות לחנויות מהסביבה הקרובה:"
    assert table.block_index == pic.index


def test_word_table_is_kept_with_its_header(result):
    table = next(t for t in result.tables if t.source == "word_table")
    assert table.headers == ["קומה", "שימוש", 'שטח במ"ר'] and len(table.rows) == 3


def test_chunks_point_at_blocks_and_rows_carry_their_table_context(result):
    kinds = {c.kind for c in result.chunks}
    assert {"text", "table", "table_row"} <= kinds
    row = next(c for c in result.chunks if c.kind == "table_row" and "הזית 7" in c.text)
    assert row.text.startswith("להלן נתוני היצע לדמי שכירות לחנויות מהסביבה הקרובה: ")
    assert 'שכ"ד למ"ר: ₪ 61' in row.text
    assert all(c.block_start is not None and c.block_end is not None for c in result.chunks)
    statement = next(c for c in result.chunks if "9,500" in c.text)
    assert result.blocks[statement.block_start].section == "סיכום סקר שוק"


def test_reading_report_counts_what_was_read(result):
    comp = result.components
    assert comp["images"] == {"read": 1} and comp["partial"] is False and comp["tables"] == 2


# --- raster pictures ------------------------------------------------------------------------------------------

def _png(w=400, h=200) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def _drawn_png(w=400, h=300) -> bytes:
    """A picture with content (not blank): a gradient with lines, as a scanned drawing."""
    from PIL import Image, ImageDraw

    im = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    draw = ImageDraw.Draw(im)
    for x in range(0, w, 37):
        draw.line([(x, 0), (w - x, h)], fill="black", width=2)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class _Vision:
    def __init__(self, out):
        self.out, self.calls = out, 0

    def read(self, png, context, careful=False):
        self.calls += 1
        return self.out


def _words(*texts):
    return [{"text": t, "conf": 95.0} for t in texts]


def test_without_the_model_a_picture_with_few_ocr_words_holds_no_text(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("x"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    assert read_raster(_drawn_png(), "", None, "heb+eng").status == "no_text"


def test_few_confident_ocr_words_are_no_proof_a_picture_holds_nothing(monkeypatch):
    # a scanned plan: OCR finds many words but trusts almost none; the model reads its labels
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("גוש") + [{"text": "3?49", "conf": 20.0}] * 60)
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    vision = _Vision(VisionOut("map", True, "גוש 3749 גוש 3853", [], "תשריט", []))
    reading = read_raster(_drawn_png(), "", vision, "heb+eng")
    assert vision.calls >= 1 and reading.method == "vision" and "3749" in reading.text


def test_the_model_decides_a_photo_holds_no_text(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("x"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    vision = _Vision(VisionOut("photo", True, "", [], "חזית הבניין", []))
    reading = read_raster(_drawn_png(), "", vision, "heb+eng")
    assert (reading.status, reading.method, reading.note) == ("no_text", "vision", "חזית הבניין") and vision.calls == 1


def test_vision_table_confirmed_by_ocr_numbers_is_read(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("אזור", "שטח", "120,500", "97.5%", "שיעור", "צפון"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    out = VisionOut("table", True, "", [VisionTableOut("", ["אזור", "שטח", "שיעור"], [["צפון", "120,500", "97.5%"]],
                                                      [])], "טבלה", [])
    reading = read_raster(_png(), "להלן סקר:", _Vision(out), "heb+eng")
    assert reading.status == "read" and reading.method == "vision"
    assert reading.tables[0].rows == [["צפון", "120,500", "97.5%"]]


def test_vision_reading_that_drops_numbers_ocr_saw_is_uncertain(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("אזור", "120,500", "241,817", "538,472", "שטח", "650"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    out = VisionOut("table", True, "", [VisionTableOut("", ["אזור", "שטח"], [["צפון", "120,500"]], [])], "טבלה", [])
    reading = read_raster(_png(), "", _Vision(out), "heb+eng")
    assert reading.status == "read_uncertain" and "OCR" in (reading.note or "")


def test_without_ocr_and_without_the_model_a_picture_is_unread(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: [])
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: False)
    reading = read_raster(_png(), "", None, "heb+eng")
    assert reading.status == "unread"


def test_only_pictures_too_thin_for_a_glyph_are_decorative():
    assert read_raster(_png(400, 4), "", None, "heb+eng").status == "decorative"
    assert read_raster(_png(30, 30), "", None, "heb+eng").status != "decorative"  # small is not decorative (U4)


def test_a_reading_that_drops_a_column_of_names_is_retried_carefully_and_kept_uncertain(monkeypatch):
    names = ["צפון", "דרום", "מרכז", "שרון", "גליל"]
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words(*names, "120,500", "130,600", "שטח"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    out = VisionOut("table", True, "", [VisionTableOut("", ["שטח"], [["120,500"], ["130,600"]], [])], "טבלה", [])
    vision = _Vision(out)
    reading = read_raster(_png(), "", vision, "heb+eng")
    assert vision.calls == 2  # the first reading failed the word check: one careful retry
    assert reading.status == "read_uncertain" and "מילים" in (reading.note or "")


def test_duplicate_headers_make_a_reading_uncertain(monkeypatch):
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("מחיר", "חציון", "שטח", "120,500", "97.5%"))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda lang: True)
    out = VisionOut("table", True, "", [VisionTableOut("", ["מחיר חציון", "מחיר חציון"], [["120,500", "97.5%"]], [])],
                    "טבלה", [])
    assert read_raster(_png(), "", _Vision(out), "heb+eng").status == "read_uncertain"


class _Readings:
    def __init__(self):
        self.rows: dict = {}

    def get(self, key):
        return self.rows.get(key)

    def put(self, key, reading):
        self.rows[key] = reading


def test_a_picture_read_once_is_taken_from_the_office_readings_on_the_next_ingestion(monkeypatch):
    from app.extraction import docx

    calls = []

    def read_picture(data, ext, context, vision, languages):
        calls.append(ext)
        return images.PictureReading("read", "vision", text="מקרא: גוש 1234")

    monkeypatch.setattr(docx, "read_picture", read_picture)
    readings = _Readings()
    first = extract_docx(FIXTURE.read_bytes(), time.monotonic() + 60, get_settings(), None, readings)
    again = extract_docx(FIXTURE.read_bytes(), time.monotonic() + 60, get_settings(), None, readings)
    assert len(calls) == 1 and len(readings.rows) == 1
    assert [b.picture_text for b in again.blocks if b.kind == "image"] == \
        [b.picture_text for b in first.blocks if b.kind == "image"] == ["מקרא: גוש 1234"]


def test_a_picture_reading_depends_on_its_context_and_the_reader():
    from app.extraction.docx import _reading_key

    vision = _Vision(None)
    vision.config = "gpt-6-luna:low"
    key = _reading_key(b"png", "להלן תשריט:", vision)
    assert key == _reading_key(b"png", "להלן תשריט:", vision)
    assert key != _reading_key(b"png", "להלן טבלה:", vision)
    assert key != _reading_key(b"png", "להלן תשריט:", None)  # an OCR-only reading is not the model's
