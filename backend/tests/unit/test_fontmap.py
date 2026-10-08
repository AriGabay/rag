"""Font-map corruption: detection per font, OCR-verified repair decisions, and per-font application (U5, KTD6).

No Tesseract here: the repair runs with a fake word reader that reads the intact twin of the broken fixture at the
same place (``F1_synthetic_fontmap_clean.pdf`` has the same glyphs and geometry). The real render-and-OCR path is
covered in ``tests/integration/test_fontmap_repair.py`` (needs Tesseract ``heb``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber
import pytest

from app.extraction import fontmap
from app.extraction.fontmap import Correction, FontMapConfig, FontMapFix, Unresolved, Vote, WordSample, decide
from app.extraction.hebrew import QUALITY_THRESHOLD, fix_text_lines, quality_score, suspect_positions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fontmap"
CLEAN = FIXTURES / "F1_synthetic_fontmap_clean.pdf"
BROKEN = FIXTURES / "F1_synthetic_fontmap_broken.pdf"
INCONSISTENT = FIXTURES / "F2_synthetic_fontmap_inconsistent.pdf"
ENGLISH = FIXTURES / "F3_synthetic_fontmap_english.pdf"
BOOK = "MPDFAA+DejaVuSansBook"
BOLD = "MPDFAA+DejaVuSansBold"
ETH = "ð"


# --- hebrew.py: quality score characterization and the in-word suspect test ------------------------------------

@pytest.mark.parametrize(
    ("text", "score"),
    [
        ("שומת מקרקעין — הגפן 14, רמת גן\nשווי הנכס: 2,650,000 ₪", 1.0),
        ("הðכס ðמצא בשכוðה שקטה בצפון העיר, בקרבת גן ציבורי ומוסדות חיðוך.", 0.926),
        ("The survey team visited Garðabær and the harbour of Hafnarfjörður in May.", 0.935),
        ("קצר", 0.2),
        ("", 0.0),
        ("ÐÐÐ ÃÃÃ ÂÂÂ ¿¿¿ ×××", 0.0),
        ("14 ןפגה :סכנה תבותכ ,ריעה זכרמב תנבנה הריד ןינבב", 0.0),
    ],
)
def test_quality_score_is_unchanged_without_a_corruption_signal(text, score):
    # Values pinned before the corruption signal was added (characterization).
    assert quality_score(text) == score
    assert quality_score(text, corruption=0.0) == score


def test_an_unrepaired_font_corruption_lowers_page_quality():
    text = "שומת מקרקעין — הגפן 14, רמת גן\nשווי הנכס: 2,650,000 ₪"
    assert quality_score(text, corruption=0.1) == 0.9
    assert quality_score(text, corruption=0.5) < QUALITY_THRESHOLD
    assert quality_score(text, corruption=2.0) == 0.0  # clamped


@pytest.mark.parametrize(
    ("word", "positions"),
    [
        ("הðכס", [1]),
        ("סכðה", [2]),
        ("מ״ð", [2]),  # an abbreviation is one Hebrew word
        ("חיðוך.", [2]),
        ("ðבðה", [0, 2]),
        ("שלום", []),
        ("Garðabær", []),  # a Latin word keeps its letters
        ("ðæt", []),
        ("ð", []),
        ("הPDF", []),  # ASCII letters glued to Hebrew are a mixed word, not a broken map
        ("2,450,000", []),
        ("שם", [1]),  # a private-use glyph inside a Hebrew word
    ],
)
def test_suspect_positions_are_foreign_letters_inside_hebrew_words(word, positions):
    assert suspect_positions(word) == positions


# --- fixtures ---------------------------------------------------------------------------------------------------

def _words(path: Path) -> list[WordSample]:
    with pdfplumber.open(path) as pdf:
        out = []
        for i, page in enumerate(pdf.pages):
            out.extend(fontmap.words_of_page(page.dedupe_chars(tolerance=1.0), i))
        return out


def _lines(path: Path, fix: FontMapFix | None = None) -> list[tuple[str, str, list[dict]]]:
    """(raw text, logical text, chars) per line, the way ``pdf.py`` reads a page."""
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0].dedupe_chars(tolerance=1.0)
        if fix is not None:
            page = fix.correct_page(page).page
        raw = page.extract_text_lines(return_chars=True)
        fixed = fix_text_lines([ln["text"] for ln in raw])
        return [(ln["text"], t, ln["chars"]) for ln, t in zip(raw, fixed, strict=True)]


class CleanTwinReader:
    """Reads a word the way OCR of its glyphs would: the intact twin's word at the same place, in logical order."""

    def __init__(self, twin: Path):
        self.words = _words(twin)
        self.calls = 0

    def read(self, sample: WordSample) -> str | None:
        self.calls += 1
        for w in self.words:
            if w.page == sample.page and all(abs(a - b) < 0.5 for a, b in zip(w.bbox, sample.bbox, strict=True)):
                return w.text[::-1]  # fpdf2 writes Hebrew in visual order; OCR reads it logically
        return None


# --- detection --------------------------------------------------------------------------------------------------

def test_detection_flags_the_suspect_char_per_font_and_the_absent_letter():
    found = fontmap.detect(_words(BROKEN), FontMapConfig())
    assert set(found.suspects) == {(BOOK, ETH), (BOLD, ETH)}
    assert found.suspects[(BOOK, ETH)] > 20
    book = found.fonts[BOOK]
    assert book.hebrew_letters >= FontMapConfig().min_hebrew_letters
    assert "נ" in book.absent  # the font emits Hebrew but never נ
    assert "נ" not in found.fonts[BOLD].absent  # the bold font's נ is intact


@pytest.mark.parametrize("path", [CLEAN, ENGLISH])
def test_clean_hebrew_and_english_with_eth_have_no_suspects(path):
    found = fontmap.detect(_words(path), FontMapConfig())
    assert found.suspects == {}
    assert all(not f.absent for f in found.fonts.values())


def test_absence_alone_is_reported_but_marks_nothing_uncertain():
    words = [WordSample(0, "שלום", (0, 0, 10, 10), (BOOK,) * 4)] * 200  # 800 Hebrew letters, never י, ה, נ...
    fix = fontmap.detect_and_repair(words, config=FontMapConfig(min_hebrew_letters=300))
    report = fix.report()
    assert report["fonts"][0]["absent"]  # reported
    assert report["corrections"] == [] and report["unresolved"] == []
    assert fix.pages == set()


# --- repair decisions --------------------------------------------------------------------------------------------

def _votes(*letters: str, final: bool = False) -> list[Vote]:
    return [Vote(letter, final) for letter in letters]


def test_decide_accepts_enough_agreeing_samples():
    d = decide(_votes("נ", "נ", "נ", "נ", "נ"), FontMapConfig())
    assert d.accepted and d.letter == "נ" and d.final_letter is None
    assert d.samples == 5 and d.agreement == 1.0


def test_decide_rejects_inconsistent_evidence():
    d = decide(_votes("נ", "נ", "נ", "ר", "ר"), FontMapConfig())
    assert not d.accepted and d.agreement == 0.6
    assert d.reason == fontmap.REASON_INCONSISTENT


def test_decide_rejects_too_few_samples():
    d = decide(_votes("נ", "נ"), FontMapConfig(min_samples=3))
    assert not d.accepted and d.reason == fontmap.REASON_FEW_SAMPLES


def test_decide_rejects_a_non_hebrew_reading():
    d = decide(_votes("o", "o", "o", "o"), FontMapConfig())
    assert not d.accepted


def test_decide_accepts_final_and_non_final_forms_of_one_letter_by_position():
    votes = _votes("נ", "נ", "נ", "נ") + _votes("ן", "ן", final=True)
    d = decide(votes, FontMapConfig())
    assert d.accepted and d.letter == "נ" and d.final_letter == "ן"
    mixed = _votes("נ", "נ", "נ") + _votes("ר", "ר", final=True)  # not the two forms of one letter
    assert not decide(mixed, FontMapConfig()).accepted


@pytest.mark.parametrize(
    ("extracted", "ocr", "expected"),
    [
        ("סכðה", "הנכס", [Vote("נ", False)]),  # visual order in the text layer, logical from OCR
        ("הðכס", "הנכס", [Vote("נ", False)]),  # logical in both
        ("הðבð", "נבנה", [Vote("נ", False), Vote("נ", False)]),  # two occurrences, two votes
        ("ðכש", "שכן", [Vote("ן", True)]),  # word-final is the logical end, wherever the text layer put it
        ("שכð", "שכן", [Vote("ן", True)]),
        ("סכðה:", "הנכס", [Vote("נ", False)]),  # punctuation does not count
        ("סכðה", "הנכסים", None),  # different length: no alignment
        ("סכðה", "שלום", None),  # the other letters disagree: not this word
    ],
)
def test_alignment_of_an_ocr_word_with_the_extracted_word(extracted, ocr, expected):
    sample = WordSample(0, extracted, (0, 0, 10, 10), (BOOK,) * len(extracted))
    assert fontmap.align(sample, BOOK, ETH, ocr, FontMapConfig()) == expected


# --- application ---------------------------------------------------------------------------------------------------

def _fix(corrections: dict, unresolved: dict | None = None) -> FontMapFix:
    return FontMapFix(corrections=corrections, unresolved=unresolved or {}, fonts={}, pages={0})


def test_a_mapping_found_in_one_font_is_not_applied_to_another_font():
    fix = _fix({(BOOK, ETH): Correction(BOOK, ETH, "נ", None, 12, 1.0, 30)},
               {(BOLD, ETH): Unresolved(BOLD, ETH, 5, 0, 0.0, {}, fontmap.REASON_NO_OCR)})
    lines = _lines(BROKEN, fix)
    clean = _lines(CLEAN)
    for (_, text, chars), (_, want, _) in zip(lines, clean, strict=True):
        fonts = {c["fontname"] for c in chars}
        if fonts == {BOOK}:
            assert text == want
        if BOLD in fonts and ETH in text:
            assert fix.uncertain_reason(chars) == fontmap.REASON_NO_OCR  # still broken, and marked
    assert any(ETH in t for _, t, _ in lines)  # the bold ð was left alone, not turned into נ


def test_corrected_lines_keep_every_digit_symbol_and_latin_word_and_restore_the_original():
    fix = _fix({(BOOK, ETH): Correction(BOOK, ETH, "נ", None, 12, 1.0, 30),
                (BOLD, ETH): Correction(BOLD, ETH, "ר", None, 4, 1.0, 5)})
    broken = _lines(BROKEN)
    repaired = _lines(BROKEN, fix)
    clean = _lines(CLEAN)
    assert [t for _, t, _ in repaired] == [t for _, t, _ in clean]
    token = re.compile(r"[\d%₪/.,:]+|[A-Za-z]+")
    for (_, before, _), (raw, after, chars) in zip(broken, repaired, strict=True):
        assert token.findall(before) == token.findall(after)
        assert len(before) == len(after)
        assert all(a == b or (a == ETH and b in "נר") for a, b in zip(before, after, strict=True))
        assert fix.original_line(raw, after, chars) == (before if before != after else None)
    text = "\n".join(t for _, t, _ in repaired)
    assert "הנכס נמצא בשכונה שקטה" in text and "רמת נוף" in text


def test_page_correction_counts_and_corruption_share():
    fix = _fix({(BOOK, ETH): Correction(BOOK, ETH, "נ", None, 12, 1.0, 30)},
               {(BOLD, ETH): Unresolved(BOLD, ETH, 5, 0, 0.0, {}, fontmap.REASON_NO_OCR)})
    with pdfplumber.open(BROKEN) as pdf:
        pc = fix.correct_page(pdf.pages[0].dedupe_chars(tolerance=1.0))
    assert pc.corrected_words > 20
    assert pc.uncertain_words == 6  # רמת, תיאור, גורמים, הערכת, ריכוז, מקורות
    assert 0 < pc.corruption == pc.uncertain_words / pc.hebrew_words < 0.1


def test_english_text_with_eth_is_unchanged_even_with_a_mapping_for_its_font():
    fix = _fix({(BOOK, ETH): Correction(BOOK, ETH, "נ", None, 12, 1.0, 30)})
    assert [t for _, t, _ in _lines(ENGLISH, fix)] == [t for _, t, _ in _lines(ENGLISH)]


# --- the whole repair with a fake reader -------------------------------------------------------------------------

def test_repair_with_agreeing_evidence_produces_per_font_corrections_and_a_record():
    reader = CleanTwinReader(CLEAN)
    fix = fontmap.detect_and_repair(_words(BROKEN), config=FontMapConfig(), reader=reader)
    assert set(fix.corrections) == {(BOOK, ETH), (BOLD, ETH)}
    assert fix.corrections[(BOOK, ETH)].letter == "נ"
    assert fix.corrections[(BOLD, ETH)].letter == "ר"
    assert fix.unresolved == {}
    assert fix.pages == {0}
    assert reader.calls <= 2 * FontMapConfig().max_samples
    record = fix.report()["corrections"]
    assert {(r["font"], r["from"], r["to"]) for r in record} == {(BOOK, ETH, "נ"), (BOLD, ETH, "ר")}
    for r in record:
        assert r["samples"] >= FontMapConfig().min_samples and r["agreement"] == 1.0 and r["occurrences"] >= r["samples"]
    assert [t for _, t, _ in _lines(BROKEN, fix)] == [t for _, t, _ in _lines(CLEAN)]


def test_repair_with_inconsistent_evidence_leaves_the_text_and_marks_it_uncertain():
    fix = fontmap.detect_and_repair(_words(INCONSISTENT), config=FontMapConfig(), reader=CleanTwinReader(CLEAN))
    assert fix.corrections == {}
    bad = fix.unresolved[(BOOK, ETH)]
    assert bad.reason == fontmap.REASON_INCONSISTENT
    assert set(bad.votes) == {"נ", "ה"} and bad.agreement < FontMapConfig().min_agreement
    lines = _lines(INCONSISTENT, fix)
    assert [t for _, t, _ in lines] == [t for _, t, _ in _lines(INCONSISTENT)]  # nothing changed
    reasons = {fix.uncertain_reason(chars) for _, t, chars in lines if ETH in t}
    assert reasons == {fontmap.REASON_INCONSISTENT}
    assert fix.report()["unresolved"][0]["reason"] == fontmap.REASON_INCONSISTENT


def test_without_a_reader_suspects_stay_unresolved():
    fix = fontmap.detect_and_repair(_words(BROKEN), config=FontMapConfig())
    assert fix.corrections == {}
    assert {u.reason for u in fix.unresolved.values()} == {fontmap.REASON_NO_OCR}


def test_english_document_needs_no_reader_and_changes_nothing():
    reader = CleanTwinReader(CLEAN)
    fix = fontmap.detect_and_repair(_words(ENGLISH), config=FontMapConfig(), reader=reader)
    assert reader.calls == 0 and not fix.corrections and not fix.unresolved and fix.pages == set()
