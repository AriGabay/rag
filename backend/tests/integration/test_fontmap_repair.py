"""Font-map repair on the real evidence path: word crops rendered by pdfium, read by Tesseract ``heb+eng`` (U5, AE2).

Needs Tesseract with Hebrew data, so it skips on a host without it; run it in the backend image:

    docker compose run --rm --no-deps -v "$PWD/backend:/app" worker python -m pytest tests/integration/test_fontmap_repair.py
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pytest

from app.extraction import fontmap
from app.extraction.fontmap import FontMapConfig, FontMapFix
from app.extraction.hebrew import QUALITY_THRESHOLD, fix_text_lines, quality_score

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
