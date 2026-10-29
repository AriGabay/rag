"""Font-map repair on the real evidence path: word crops rendered by pdfium, read by Tesseract ``heb+eng`` (U5, AE2).

The OCR tests need Tesseract with Hebrew data, so they skip on a host without it; run them in the backend image:

    docker compose run --rm --no-deps -v "$PWD/backend:/app" worker python -m pytest tests/integration/test_fontmap_repair.py

The ingestion test (``db``) runs against the test database with the clean-twin reader of ``tests/unit/test_fontmap.py``
standing in for OCR, so it runs where Tesseract has no Hebrew.
"""

from __future__ import annotations

import hashlib
import re
import time
from functools import cache
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_tx
from app.extraction import fontmap
from app.extraction.base import ExtractionResult
from app.extraction.fontmap import FontMapConfig, FontMapFix
from app.extraction.hebrew import QUALITY_THRESHOLD, fix_text_lines, quality_score
from app.extraction.pdf import extract_pdf

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fontmap"
CLEAN = FIXTURES / "F1_synthetic_fontmap_clean.pdf"
BROKEN = FIXTURES / "F1_synthetic_fontmap_broken.pdf"
INCONSISTENT = FIXTURES / "F2_synthetic_fontmap_inconsistent.pdf"
ENGLISH = FIXTURES / "F3_synthetic_fontmap_english.pdf"
BOOK = "MPDFAA+DejaVuSansBook"
BOLD = "MPDFAA+DejaVuSansBold"
ETH = "ð"


def _heb_ocr_available() -> bool:
    try:
        import pytesseract

        return "heb" in pytesseract.get_languages(config="")
    except Exception:  # noqa: BLE001
        return False


requires_ocr = pytest.mark.skipif(not _heb_ocr_available(), reason="tesseract with Hebrew data not installed")


@cache
def analyze(path: Path) -> FontMapFix:
    data = path.read_bytes()
    doc = pdfium.PdfDocument(data)
    try:
        with pdfplumber.open(path) as pdf:
            return fontmap.analyze(doc, pdf, FontMapConfig())
    finally:
        doc.close()


def page_lines(path: Path, fix: FontMapFix | None = None) -> list[tuple[str, str, list[dict]]]:
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0].dedupe_chars(tolerance=1.0)
        if fix is not None:
            page = fix.correct_page(page).page
        raw = page.extract_text_lines(return_chars=True)
        fixed = fix_text_lines([ln["text"] for ln in raw])
        return [(ln["text"], t, ln["chars"]) for ln, t in zip(raw, fixed, strict=True)]


@requires_ocr
def test_broken_map_is_repaired_per_font_from_the_glyphs():
    fix = analyze(BROKEN)
    assert {k: c.letter for k, c in fix.corrections.items()} == {(BOOK, ETH): "נ", (BOLD, ETH): "ר"}
    assert fix.unresolved == {}
    for record in fix.report()["corrections"]:
        assert record["samples"] >= FontMapConfig().min_samples
        assert record["agreement"] >= FontMapConfig().min_agreement


@requires_ocr
def test_repaired_page_reads_like_the_intact_document_and_numbers_are_byte_identical():
    fix = analyze(BROKEN)
    broken, repaired, clean = page_lines(BROKEN), page_lines(BROKEN, fix), page_lines(CLEAN)
    assert [t for _, t, _ in repaired] == [t for _, t, _ in clean]
    text = "\n".join(t for _, t, _ in repaired)
    assert ETH not in text
    assert "הנכס נמצא בשכונה שקטה" in text  # a search for a word with the broken letter finds the passage
    token = re.compile(r"[\d%₪/.,:]+|[A-Za-z]+")
    for (_, before, _), (raw, after, chars) in zip(broken, repaired, strict=True):
        assert token.findall(before) == token.findall(after)  # digits, %, ₪, '/', Latin words untouched
        assert fix.original_line(raw, after, chars) == (before if before != after else None)
        assert fix.uncertain_reason(chars) is None


@requires_ocr
def test_inconsistent_map_is_not_repaired_and_is_marked_uncertain():
    fix = analyze(INCONSISTENT)
    assert fix.corrections == {}
    bad = fix.unresolved[(BOOK, ETH)]
    assert bad.reason == fontmap.REASON_INCONSISTENT
    assert bad.agreement < FontMapConfig().min_agreement
    with pdfplumber.open(INCONSISTENT) as pdf:
        pc = fix.correct_page(pdf.pages[0].dedupe_chars(tolerance=1.0))
    assert pc.corrected_words == 0 and pc.uncertain_words > 0
    lines = page_lines(INCONSISTENT, fix)
    assert [t for _, t, _ in lines] == [t for _, t, _ in page_lines(INCONSISTENT)]
    assert {fix.uncertain_reason(c) for _, t, c in lines if ETH in t} == {fontmap.REASON_INCONSISTENT}
    text = "\n".join(t for _, t, _ in lines)
    assert quality_score(text, corruption=pc.corruption) < quality_score(text)


@requires_ocr
def test_english_text_with_eth_stays_unchanged():
    fix = analyze(ENGLISH)
    assert fix.corrections == {} and fix.unresolved == {} and fix.pages == set()
    text = "\n".join(t for _, t, _ in page_lines(ENGLISH, fix))
    assert "Garðabær" in text and "Reyðarfjörður" in text
    assert quality_score(text) >= QUALITY_THRESHOLD


# --- the PDF reader on the real evidence path ----------------------------------------------------------------------

_TOKEN = re.compile(r"[\d%₪/.,:]+|[A-Za-z]+")


@cache
def read(path: Path) -> ExtractionResult:
    return extract_pdf(path.read_bytes(), time.monotonic() + 600, get_settings())


def _text_blocks(result: ExtractionResult):
    return [b for b in result.blocks if b.kind in ("heading", "paragraph")]


@requires_ocr
def test_extract_pdf_repairs_the_broken_document_into_its_intact_twin():
    result, clean = read(BROKEN), read(CLEAN)
    assert [(b.kind, b.text, b.page) for b in result.blocks] == [(b.kind, b.text, b.page) for b in clean.blocks]
    assert [c.text for c in result.chunks] == [c.text for c in clean.chunks]
    assert result.pages[0].method == "text_layer" and result.pages[0].quality == clean.pages[0].quality
    changed = [b for b in _text_blocks(result) if b.original_text is not None]
    assert changed and all(ETH in b.original_text and ETH not in b.text for b in changed)
    for b in _text_blocks(result):
        original = b.original_text or b.text
        assert _TOKEN.findall(original) == _TOKEN.findall(b.text)  # digits, %, ₪, '/', Latin byte-identical
        assert b.status == "read"
    comp = result.components
    assert comp["partial"] is False
    assert {(c["font"], c["from"], c["to"]) for c in comp["fontmap"]["corrections"]} == {
        (BOOK, ETH, "נ"), (BOLD, ETH, "ר")}


@requires_ocr
def test_extract_pdf_leaves_an_inconsistent_map_uncertain_and_the_document_partly_read():
    result = read(INCONSISTENT)
    assert result.pages[0].ok and result.pages[0].method == "text_layer"
    uncertain = [b for b in result.blocks if b.status == "read_uncertain"]
    assert uncertain and all(ETH in b.text and b.note == fontmap.REASON_INCONSISTENT for b in uncertain)
    assert all(b.original_text is None for b in result.blocks)
    comp = result.components
    assert comp["partial"] is True
    assert comp["uncertain"] == [{"page": 1, "blocks": len(uncertain), "reason": fontmap.REASON_INCONSISTENT}]
    assert comp["fontmap"]["corrections"] == [] and comp["fontmap"]["unresolved"][0]["char"] == ETH


@requires_ocr
def test_extract_pdf_leaves_english_with_eth_unchanged():
    result = read(ENGLISH)
    assert all(b.status == "read" and b.original_text is None for b in result.blocks)
    assert "fontmap" not in result.components and result.components["partial"] is False
    assert "Garðabær" in "\n".join(b.text for b in result.blocks)


# --- ingestion: the corrected text is what search finds (AE2) ------------------------------------------------------

@pytest.mark.db
def test_an_ingested_broken_map_is_found_by_search_with_its_original_and_record_kept(db, monkeypatch):
    from app.platform import pipeline
    from app.platform.search import hybrid_search
    from app.platform.storage import get_storage, storage_key
    from tests.factories import make_office
    from tests.unit.test_fontmap import CleanTwinReader

    office = make_office(db, "משרד ב", "admin-b@example.test")
    real = fontmap.detect_and_repair
    monkeypatch.setattr(fontmap, "detect_and_repair",
                        lambda words, pdf_doc=None, config=None, reader=None:
                        real(words, pdf_doc, config, CleanTwinReader(CLEAN)))
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    data = BROKEN.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(office.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(office.ctx()) as conn:
        doc = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g, :t)"
                                " RETURNING id"), {"g": office.default_group_id, "t": "מגדל הנחל"}).scalar_one()
        ver = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, :f, 'application/pdf', :b, :k,"
            " :u) RETURNING id"),
            {"d": doc, "s": sha, "f": BROKEN.name, "b": len(data), "k": key, "u": office.admin_id}).scalar_one()
    pipeline.process_version(office.office_id, ver)

    with tenant_tx(office.ctx()) as conn:
        hits = hybrid_search(conn, "שכונה שקטה", 5)
        assert hits and "הנכס נמצא בשכונה שקטה" in hits[0]["text"] and hits[0]["lexical_support"]
        assert not conn.execute(text("SELECT 1 FROM chunks WHERE version_id = :v AND text LIKE :e"),
                                {"v": ver, "e": f"%{ETH}%"}).first()
        block = conn.execute(text("SELECT text, original_text, status, reader_version FROM document_blocks"
                                  " WHERE version_id = :v AND text LIKE :t"),
                             {"v": ver, "t": "%הנכס נמצא בשכונה שקטה%"}).one()
        ing = conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"), {"v": ver}).scalar_one()
    assert block.status == "read" and block.reader_version == pipeline.PDF_INGESTION_VERSION
    assert "הðכס ðמצא בשכוðה שקטה" in block.original_text
    assert _TOKEN.findall(block.original_text) == _TOKEN.findall(block.text)
    assert ing["partial"] is False and ing["ingestion_version"] == pipeline.PDF_INGESTION_VERSION
    assert {(c["font"], c["from"], c["to"]) for c in ing["fontmap"]["corrections"]} == {
        (BOOK, ETH, "נ"), (BOLD, ETH, "ר")}
