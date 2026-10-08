"""Content the text layer does not cover (U4, KTD4, KTD5): found on every text-layer page, deduplicated by content,
read by OCR or a scripted vision reader, or reported unread with its reason.

The fixtures under ``tests/fixtures/regions/`` are synthetic (``scripts/generate_fixtures.py --only regions``): a
raster table under a valid text layer with a logo and a watermark on ten pages, a low-resolution raster table, a
searchable scan, a ruled vector table with a dark header, vector text drawn as outlines, and a stamp next to a
photograph. Every value in them is invented. The vision reader is scripted by the size of the picture it is
shown; no model is called. OCR is switched off unless a test scripts it (``ocr`` tests use Tesseract ``heb``)."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image

from app.config import get_settings
from app.extraction import images
from app.extraction.base import ExtractionError
from app.extraction.images import (
    PictureReading,
    RegionHints,
    VisionCallFailed,
    VisionOut,
    VisionTableOut,
    VisionUnavailable,
    read_gray,
    read_raster,
)
from app.extraction.ocr import ocr_available
from app.extraction.pdf import extract_pdf
from app.extraction.regions import REGION_READER_VERSION, ReadingKey

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REGIONS = FIXTURES / "regions"
R1 = REGIONS / "R1_synthetic_raster_table.pdf"
R2 = REGIONS / "R2_synthetic_lowres_table.pdf"
R3 = REGIONS / "R3_synthetic_searchable_scan.pdf"
R4 = REGIONS / "R4_synthetic_vector_table.pdf"
R5 = REGIONS / "R5_synthetic_vector_text.pdf"
R6 = REGIONS / "R6_synthetic_stamp_and_photo.pdf"

# picture sizes in the fixtures (pixels): what the scripted reader recognises a picture by
TABLE, LOWRES, LOGO, MARK, STAMP = (1400, 420), (600, 300), (440, 66), (200, 250), (240, 110)
HEADERS = ["אזור", "שטח (מ״ר)", "דמי שכירות (₪)", "תפוסה"]
ROWS = [["צפון", "1,250", "48,600", "92.5%"], ["מרכז", "2,340", "97,350", "88.0%"],
        ["דרום", "1,880", "61,420", "95.5%"], ["מערב", "960", "33,780", "79.5%"]]
heb_ocr = pytest.mark.skipif(not ocr_available("heb+eng"), reason="tesseract with Hebrew data not installed")


def table_out() -> VisionOut:
    return VisionOut("table", True, "", [VisionTableOut("", HEADERS, [list(r) for r in ROWS], [])], "טבלה", [])


SCRIPT = {
    TABLE: table_out,
    LOWRES: table_out,
    LOGO: lambda: VisionOut("text", True, "משרד שמאות לדוגמה", [], "", []),
    MARK: lambda: VisionOut("diagram", True, "", [], "סמל עגול עם כוכב", []),
    STAMP: lambda: VisionOut("text", True, "שמאי מקרקעין\nרישיון 4821", [], "", []),
}


class ScriptedVision:
    """Answers by the size of the picture it is shown; ``fail`` maps a size to the status its calls fail with."""

    config = "scripted:low"

    def __init__(self, fail: dict | None = None, script: dict | None = None):
        self.calls: list[tuple[int, int]] = []
        self.contexts: list[str] = []
        self.fail = fail or {}
        self.script = script or SCRIPT

    def read(self, png, context, careful=False):
        size = Image.open(io.BytesIO(png)).size
        self.calls.append(size)
        self.contexts.append(context)
        if size in self.fail:
            raise VisionCallFailed(self.fail[size])
        answer = self.script.get(size)
        return answer() if answer else VisionOut("other", True, "", [], "ציור", [])

    def count(self, size) -> int:
        return self.calls.count(size)


class MemoryCache:
    def __init__(self):
        self.rows: dict[ReadingKey, PictureReading] = {}
        self.gets = 0

    def get(self, key):
        self.gets += 1
        return self.rows.get(key)

    def put(self, key, reading):
        self.rows[key] = reading


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch, request):
    if request.node.get_closest_marker("ocr") is None:
        monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)


def scripted_ocr(monkeypatch, by_size: dict):
    """OCR switched on with scripted words per picture size (pictures not listed: no words)."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words",
                        lambda gray, lang: [{"text": t, "conf": 95.0} for t in by_size.get(gray.size, [])])


def extract(path: Path, vision=None, cache=None):
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings(), vision, cache)


def images_of(result, page=None):
    return [b for b in result.blocks if b.kind == "image" and (page is None or b.page == page)]


# --- a raster table under a valid text layer (AE1) ----------------------------------------------------------------

TABLE_WORDS = ["אזור", "צפון", "מרכז", "דרום", "מערב", "1,250", "48,600", "92.5%", "2,340", "97,350", "88.0%",
               "1,880", "61,420", "95.5%", "960", "33,780", "79.5%"]


def test_a_raster_table_on_a_text_page_becomes_a_table_at_its_place(monkeypatch):
    scripted_ocr(monkeypatch, {TABLE: TABLE_WORDS})
    vision = ScriptedVision()
    result = extract(R1, vision)
    page1 = [(b.kind, b.text.splitlines()[0] if b.text else "") for b in result.blocks if b.page == 1]
    assert page1[:6] == [("image", "משרד שמאות לדוגמה"), ("heading", "1. סקר דמי שכירות"),
                         ("paragraph", "להלן נתוני דמי השכירות שנאספו באזורי העיר:"),
                         ("image", "אזור | שטח (מ״ר) | דמי שכירות (₪) | תפוסה"),
                         ("paragraph", "הנתונים מלמדים על ביקוש יציב לשטחי מסחר בכל אזורי העיר."), ("image", "")]
    block = images_of(result, 1)[1]
    assert (block.status, block.method, block.source) == ("read", "vision", "vision")
    assert block.bbox[1] > 100 and block.content_hash and not block.content_hash.startswith("ink:")
    table = result.tables[block.table_index]
    assert (table.source, table.headers, table.block_index, table.page_start) == ("vision", HEADERS, block.index, 1)
    assert [r.cells for r in table.rows] == ROWS and all(r.page == 1 for r in table.rows)
    assert table.caption == "להלן נתוני דמי השכירות שנאספו באזורי העיר:"
    assert table.notes == ["(*) הנתונים בדויים ולצורך הדגמה בלבד."]  # the note line under the picture
    assert result.pages[0].method == "mixed" and "48,600" in result.pages[0].text
    rows = [c for c in result.chunks if c.kind == "table_row" and c.table_index == table.index]
    assert len(rows) == 4 and all(c.page_list == [1] for c in rows)
    assert result.components["partial"] is False
    assert vision.count(TABLE) == 1 and set(vision.contexts) == {""}  # the reading depends on the content alone


def test_a_logo_and_a_watermark_on_ten_pages_are_read_once():
    vision = ScriptedVision()
    result = extract(R1, vision)
    assert vision.count(LOGO) == 1 and vision.count(MARK) == 1
    logos = [b for b in images_of(result) if b.bbox[1] < 60]
    assert [b.page for b in logos] == list(range(1, 11))
    assert {(b.status, b.picture_text, b.content_hash) for b in logos} == {
        ("read", "משרד שמאות לדוגמה", logos[0].content_hash)}
    # page 1's two watermark tiles: one block, recorded without text; on pages 2-10 the body text covers them
    marks = [b for b in images_of(result) if b.bbox[1] > 300]
    assert [(b.page, b.status) for b in marks] == [(1, "no_text")] and marks[0].note == "סמל עגול עם כוכב"


def test_without_the_vision_model_a_table_region_is_unread_and_its_page_named(monkeypatch):
    scripted_ocr(monkeypatch, {TABLE: ["אזור", "צפון", "1,250", "48,600", "2,340", "97,350"]})
    result = extract(R1, None)
    block = images_of(result, 1)[1]
    assert (block.status, block.method) == ("unread", "ocr") and "קריאה חזותית" in block.note
    comp = result.components
    assert comp["partial"] is True and [u["page"] for u in comp["unread"]] == [1]
    assert not result.tables and result.warnings


def test_vision_numbers_ocr_did_not_see_make_the_region_uncertain(monkeypatch):
    scripted_ocr(monkeypatch, {TABLE: ["אזור", "צפון", "1,250", "48,600", "2,340", "97,350", "1,880", "61,420",
                                       "960", "33,780"]})
    out = table_out()
    out.tables[0].rows[0][2] = "84,600"  # a digit swap OCR does not confirm
    vision = ScriptedVision(script={**SCRIPT, TABLE: lambda: out})
    block = images_of(extract(R1, vision), 1)[1]
    assert block.status == "read_uncertain" and "OCR" in block.note
    assert vision.count(TABLE) == 2  # an unconfirmed reading gets one careful retry


# --- what is not uncovered ------------------------------------------------------------------------------------

def test_a_searchable_scan_is_covered_by_its_invisible_text():
    vision = ScriptedVision()
    result = extract(R3, vision)
    assert not images_of(result) and not vision.calls and result.components["partial"] is False
    assert "הנכס ממוקם ברחוב שקט" in result.pages[0].text and result.pages[0].method == "text_layer"


@pytest.mark.parametrize("path", [R4, FIXTURES / "blocks" / "B1_synthetic_reading_order.pdf",
                                  FIXTURES / "D1_synthetic_harozim_digital.pdf",
                                  FIXTURES / "D9_synthetic_harozim_30_comparables.pdf",
                                  FIXTURES / "general" / "H1_synthetic_ramatgan_irusim.pdf"])
def test_ruled_vector_tables_rules_fills_and_bullets_are_not_uncovered(path):
    vision = ScriptedVision()
    result = extract(path, vision)
    assert not images_of(result) and not vision.calls and result.components["partial"] is False


# --- vector text, low resolution, small pictures ------------------------------------------------------------------

def test_vector_text_without_a_text_layer_is_found_as_one_region():
    result = extract(R5, None)
    found = images_of(result)
    assert len(found) == 1 and found[0].content_hash.startswith("ink:")
    x0, top, x1, bottom = found[0].bbox
    assert x1 > 500 and 80 < top < bottom < 160  # both outlined lines, under the paragraph
    assert found[0].status == "unread" and result.components["partial"] is True
    kinds = [b.kind for b in result.blocks]
    assert kinds == ["heading", "paragraph", "image", "paragraph"]


def test_vector_text_goes_to_the_model_when_ocr_cannot_read_it():
    vision = ScriptedVision(script={})
    extract(R5, vision)
    assert len(vision.calls) == 1 and max(vision.calls[0]) <= images.VISION_MAX_SIDE


@pytest.mark.ocr
@heb_ocr
def test_vector_text_is_read_by_ocr():
    vision = ScriptedVision()
    result = extract(R5, vision)
    (block,) = images_of(result)
    assert (block.status, block.method) == ("read_uncertain", "ocr") and not vision.calls
    assert "1,450" in block.text and "3,780,000" in block.text


@pytest.mark.ocr
@heb_ocr
def test_a_searchable_scan_is_covered_when_ocr_agrees():
    vision = ScriptedVision()
    result = extract(R3, vision)
    assert not images_of(result) and not vision.calls and result.components["partial"] is False


@pytest.mark.ocr
@heb_ocr
def test_a_raster_table_read_by_tesseract_cross_checks_the_model():
    vision = ScriptedVision()
    result = extract(R1, vision)
    block = images_of(result, 1)[1]
    assert block.status == "read" and vision.count(TABLE) == 1


def test_a_low_resolution_table_with_two_ocr_words_goes_to_the_model(monkeypatch):
    scripted_ocr(monkeypatch, {LOWRES: ["1,250", "צפון"]})
    vision = ScriptedVision()
    result = extract(R2, vision)
    block = images_of(result)[1]
    assert block.method == "vision" and block.status in ("read", "read_uncertain") and block.table_index is not None
    assert vision.count(LOWRES) >= 1


STAMP_WORDS = ["שמאי", "מקרקעין", "רישיון", "4821"]


def test_a_small_stamp_with_text_is_read_and_a_photo_is_not_sent(monkeypatch):
    scripted_ocr(monkeypatch, {STAMP: STAMP_WORDS})
    vision = ScriptedVision()
    result = extract(R6, vision)
    stamp, photo = images_of(result)
    assert (stamp.status, stamp.picture_text) == ("read", "שמאי מקרקעין\nרישיון 4821")
    assert photo.status == "no_text" and photo.note
    assert vision.calls == [STAMP]


# --- vision failures (KTD4) -----------------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["rate_limited", "timeout", "error"])
def test_a_transient_vision_failure_fails_the_job_for_retry(status):
    with pytest.raises(VisionUnavailable) as exc:
        extract(R1, ScriptedVision(fail={TABLE: status}))
    assert exc.value.permanent is False and status in exc.value.reason


@pytest.mark.parametrize("status", ["auth", "quota", "model_unavailable"])
def test_a_vision_configuration_error_fails_the_job(status):
    with pytest.raises(ExtractionError) as exc:
        extract(R1, ScriptedVision(fail={TABLE: status}))
    assert exc.value.permanent is True


def test_invalid_output_twice_leaves_the_region_unread_with_the_status():
    vision = ScriptedVision(fail={TABLE: "invalid"})
    cache = MemoryCache()
    result = extract(R1, vision, cache)
    block = images_of(result, 1)[1]
    assert (block.status, block.note) == ("unread", "הקריאה החזותית נכשלה (invalid)")
    assert vision.count(TABLE) == 2  # one careful retry
    assert result.components["partial"] is True
    assert not any(k for k in cache.rows if k.content_hash == block.content_hash)  # a failure is never cached


def test_invalid_output_with_ocr_text_keeps_the_ocr_reading_uncertain(monkeypatch):
    scripted_ocr(monkeypatch, {TABLE: ["אזור", "צפון", "1,250", "48,600", "2,340", "97,350"]})
    monkeypatch.setattr(images, "_from_ocr", lambda gray, lang, note: PictureReading(
        "read_uncertain", "ocr", text="צפון 1,250 48,600", note=note))
    block = images_of(extract(R1, ScriptedVision(fail={TABLE: "refusal"})), 1)[1]
    assert (block.status, block.method) == ("read_uncertain", "ocr") and "refusal" in block.note


# --- reuse across documents (KTD5) ----------------------------------------------------------------------------

def test_readings_are_reused_by_content_across_documents_and_re_ingestion():
    office_a, office_b = MemoryCache(), MemoryCache()
    first = ScriptedVision()
    extract(R1, first, office_a)
    assert first.count(LOGO) == 1
    key = next(k for k in office_a.rows if office_a.rows[k].text == "משרד שמאות לדוגמה")
    assert (key.reader_version, key.model_config) == (REGION_READER_VERSION, "scripted:low")
    again = ScriptedVision()
    extract(R1, again, office_a)
    assert again.calls == []  # re-ingestion: no new vision call for content already read
    second_doc = ScriptedVision()
    extract(R2, second_doc, office_a)
    assert set(second_doc.calls) == {LOWRES}  # the logo came from the office's readings
    other_office = ScriptedVision()
    extract(R2, other_office, office_b)
    assert set(other_office.calls) == {LOWRES, LOGO} and other_office.count(LOGO) == 1


def test_a_reading_without_the_model_is_not_reused_once_the_model_is_allowed(monkeypatch):
    scripted_ocr(monkeypatch, {STAMP: STAMP_WORDS})
    monkeypatch.setattr(images, "_from_ocr", lambda gray, lang, note: PictureReading(
        "read_uncertain", "ocr", text=" ".join(STAMP_WORDS), note=note))
    cache = MemoryCache()
    stamp = images_of(extract(R6, None, cache))[0]  # the few words OCR read with confidence: an OCR reading
    assert (stamp.status, stamp.method) == ("read_uncertain", "ocr")
    assert {k.model_config for k in cache.rows} == {"none"}
    vision = ScriptedVision()
    stamp = images_of(extract(R6, vision, cache))[0]
    assert vision.calls == [STAMP] and stamp.status == "read"


# --- pictures (images.py) -------------------------------------------------------------------------------------

def _png(w, h, color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _words(*texts):
    return [{"text": t, "conf": 95.0} for t in texts]


def test_a_small_picture_is_not_decorative_by_size(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("שמאי", "רישיון"))
    vision = ScriptedVision(script={(30, 30): lambda: VisionOut("other", True, "שמאי רישיון", [], "", [])})
    stamp = Image.new("L", (30, 30), 255)
    stamp.paste(0, (4, 10, 26, 14))
    stamp.paste(0, (4, 18, 20, 22))
    reading = read_gray(stamp, "", vision, "heb+eng", RegionHints())
    assert reading.status == "read" and vision.calls
    assert read_raster(_png(400, 4), "", None, "heb+eng").status == "decorative"  # a line holds no glyph


def test_a_ruled_table_picture_with_few_ocr_words_goes_to_the_model(monkeypatch):
    from PIL import ImageDraw

    img = Image.new("L", (400, 200), 255)
    draw = ImageDraw.Draw(img)
    for y in (0, 50, 100, 150, 199):
        draw.line((0, y, 399, y), fill=0, width=2)
    for x in (0, 200, 399):
        draw.line((x, 0, x, 199), fill=0, width=2)
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("1,250"))
    vision = ScriptedVision(script={(400, 200): table_out})
    for hints in (None, RegionHints()):  # a DOCX picture and a PDF region alike
        assert read_gray(img, "", vision, "heb+eng", hints).method == "vision"


def test_docx_pictures_keep_the_ocr_fallback_when_the_model_fails(monkeypatch):
    """Characterization: a DOCX picture whose model call fails (any cause) is read by OCR, as before U4."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words",
                        lambda gray, lang: _words("אזור", "שטח", "צפון", "דרום", "מרכז", "120,500", "130,600", "שיעור"))
    monkeypatch.setattr(images, "_from_ocr", lambda gray, lang, note: PictureReading(
        "read_uncertain", "ocr", text="צפון 120,500", note=note))
    for status in ("rate_limited", "auth", "invalid"):
        vision = ScriptedVision(fail={(400, 200): status})
        reading = read_raster(_png(400, 200, "gray"), "להלן סקר:", vision, "heb+eng")
        assert (reading.status, reading.method) == ("read_uncertain", "ocr") and len(vision.calls) == 2
        assert vision.contexts == ["להלן סקר:"] * 2  # a DOCX picture keeps its context


def test_a_long_picture_is_sent_capped_to_the_high_detail_bound(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: _words("1,250", "2,340"))
    vision = ScriptedVision()
    img = Image.new("L", (4000, 1000), 255)
    img.paste(0, (100, 100, 3900, 120))
    img.paste(0, (100, 500, 3900, 520))
    read_gray(img, "", vision, "heb+eng", RegionHints(dpi=400))
    assert vision.calls and max(vision.calls[0]) == images.VISION_MAX_SIDE
