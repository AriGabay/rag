"""Visual -> logical Hebrew order, orientation check and text quality score (U5, KTD11)."""

from __future__ import annotations

import pytest

from app.extraction.hebrew import (
    QUALITY_THRESHOLD,
    fix_line,
    fix_text_lines,
    orientation_evidence,
    quality_score,
    visual_to_logical,
)

# Raw pdfplumber lines quoted in tests/fixtures/README.md (D1 page 1).
RAW_ADDRESS = "14 ןפגה :סכנה תבותכ"
RAW_ROW = "26,000 2,470,000 וטנ 95 4 הריד 12/02/2024 6158/42/3 20 ןפגה"
RAW_OFFICE_D1 = ")יטתניס( ׳א ומד תואמש :דרשמ"  # mirrored bracket glyphs
RAW_OFFICE_D10 = "(יטתניס) ׳א ומד תואמש :דרשמ"


def test_reversed_header_line_becomes_logical():
    assert fix_line(RAW_ADDRESS) == "כתובת הנכס: הגפן 14"


def test_row_line_keeps_numbers_dates_and_block_parcel_intact():
    out = fix_line(RAW_ROW)
    assert out == "הגפן 20 6158/42/3 12/02/2024 דירה 4 95 נטו 2,470,000 26,000"
    for token in ("6158/42/3", "12/02/2024", "2,470,000", "26,000"):
        assert token in out.split()


@pytest.mark.parametrize("raw", [RAW_OFFICE_D1, RAW_OFFICE_D10])
def test_brackets_come_out_balanced_whether_or_not_glyphs_were_mirrored(raw):
    assert fix_line(raw) == "משרד: שמאות דמו א׳ (סינתטי)"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ןג תמר ,8 תיזה — ןיעקרקמ תמוש", "שומת מקרקעין — הזית 8, רמת גן"),
        ("₪ 2,650,000 :סכנה יווש", "שווי הנכס: 2,650,000 ₪"),
        ("1,890,000 ₪ :סכנה יווש", "שווי הנכס: ₪ 1,890,000"),
        ("המושה תרטמ .1", "1. מטרת השומה"),
        ("₪ 27,895-כ לש יווש עבקנ", "נקבע שווי של כ-27,895 ₪"),
        (".2024-ל 2023 ןיב הנותמ היילע", "עלייה מתונה בין 2023 ל-2024."),
        ("1 דומע | ומדל יטתניס ךמסמ", "מסמך סינתטי לדמו | עמוד 1"),
        (".המוק לכל 2% לש המוק םדקמ", "מקדם קומה של 2% לכל קומה."),
        ("15/03/2024 :עבוקה דעומה", "המועד הקובע: 15/03/2024"),
        ("3.5 םירדח 1,250,000 ריחמ", "מחיר 1,250,000 חדרים 3.5"),
        ("Tel Aviv ריעה", "העיר Tel Aviv"),
    ],
)
def test_visual_to_logical_examples(raw, expected):
    assert visual_to_logical(raw) == expected


@pytest.mark.parametrize(
    "line",
    [
        "כתובת הנכס: הגפן 14",
        "עיר: רמת גן",
        "גוש: 6158 חלקה: 42 תת חלקה: 7",
        "1. מטרת השומה",
        "שווי הנכס: 2,650,000 ₪",
        "משרד: שמאות דמו א׳ (סינתטי)",
    ],
)
def test_already_logical_lines_are_left_unchanged(line):
    assert fix_line(line) == line


def test_mixed_hebrew_digits_line_keeps_token_order():
    line = "דירה 4 חד' 95 מ״ר נטו"
    assert fix_line(line) == line
    assert visual_to_logical("וטנ ר״מ 95 'דח 4 הריד") == line


def test_ambiguous_line_follows_the_page_orientation():
    # "הריד" alone has no orientation evidence; the page around it is clearly visual.
    raw = ["ןג תמר :ריע", "םיזורח :הנוכש", "וטנ ר״מ 95 'דח 4 הריד"]
    assert fix_text_lines(raw) == ["עיר: רמת גן", "שכונה: חרוזים", "דירה 4 חד' 95 מ״ר נטו"]
    logical = ["עיר: רמת גן", "שכונה: חרוזים", "דירה 4 חד' 95 מ״ר נטו"]
    assert fix_text_lines(logical) == logical


def test_final_letter_orientation_check():
    logical, visual = orientation_evidence("כתובת הנכס: הגפן 14 רמת גן חרוזים")
    assert logical > 0 and visual == 0
    logical, visual = orientation_evidence(RAW_ADDRESS + " ןג תמר םיזורח")
    assert visual > 0 and logical == 0


def test_good_text_scores_above_threshold():
    text = "\n".join(fix_text_lines([RAW_ADDRESS, RAW_ROW, RAW_OFFICE_D1]))
    assert quality_score(text) >= QUALITY_THRESHOLD
    assert quality_score("שומת מקרקעין — הגפן 14, רמת גן\nשווי הנכס: 2,650,000 ₪") >= QUALITY_THRESHOLD


@pytest.mark.parametrize(
    "garbled",
    [
        "���� ��� ��: 14 ���",
        "×©×•×ž×” ×ž×§×¨×§×¢×™×Ÿ ×”×’×¤×Ÿ 14",
        "",
        "   \n  ",
    ],
)
def test_garbled_text_scores_below_threshold(garbled):
    assert quality_score(garbled) < QUALITY_THRESHOLD


def test_text_still_in_visual_order_scores_below_threshold():
    raw = "\n".join([RAW_ADDRESS, "ןג תמר :ריע", "םיזורח :הנוכש", "המושה תרטמ .1"] * 3)
    assert quality_score(raw) < QUALITY_THRESHOLD
