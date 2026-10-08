"""The no-regression gate counts the numbers of page furniture (a letterhead or footer read once for the whole
document) as present on every page, so moving its single block to the first occurrence is not a lost number;
a number missing from a page's own text still is (KTD7). Synthetic values only."""

from app.extraction.base import Block, ExtractionResult, PageResult
from app.platform import pipeline

FOOTER = "a1" * 32


def _result(footer_text: str, body: str) -> ExtractionResult:
    blocks = [Block(0, "image", footer_text, page=1, content_hash=FOOTER, status="read", source="vision"),
              Block(1, "paragraph", body, page=7)]
    return ExtractionResult(page_count=7, pages=[PageResult(7, body, "text_layer", 1.0, True)], tables=[],
                            chunks=[], blocks=blocks,
                            repeated=[{"hash": FOOTER[:pipeline._hash_shown(FOOTER).__len__()], "occurrences": 9,
                                       "pages": 9, "first_page": 1, "status": "read"}])


def test_footer_numbers_count_on_every_page():
    r = _result("רחוב הדוגמה 12 | טלפון 03-5550101", "השווי נקבע ל-1,480,000 ש\"ח")
    seen = pipeline._numbers(pipeline._page_texts(r).get(7, "")) | pipeline._furniture_numbers(r)
    old_page_7 = "השווי נקבע ל-1,480,000 ש\"ח\nרחוב הדוגמה 12 | טלפון 03-5550101"
    assert pipeline._missing_numbers(old_page_7, seen) == []


def test_a_number_missing_from_the_page_itself_still_counts():
    r = _result("רחוב הדוגמה 12 | טלפון 03-5550101", "השווי נקבע ל-1,480,000 ש\"ח")
    seen = pipeline._numbers(pipeline._page_texts(r).get(7, "")) | pipeline._furniture_numbers(r)
    assert pipeline._missing_numbers("שטח 245 מ\"ר; השווי 1,480,000", seen) == ["245"]


def test_a_block_that_is_not_furniture_does_not_count_elsewhere():
    r = _result("טלפון 03-5550101", "גוף")
    r.repeated = []
    assert pipeline._furniture_numbers(r) == set()
