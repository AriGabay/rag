from app.answering.verify import allowed_numbers, verify_answer

EVIDENCE = ["העסקה בהגפן 20 נמכרה ב-2,470,000 ₪ בשטח 95 מ״ר.", "השמאי המכריע קבע הפחתה של 10%."]
NUMS = allowed_numbers(EVIDENCE, {"mean_price_per_sqm": "26000.00", "record_count": 4})


def test_supported_answer_passes():
    assert verify_answer("העסקה נמכרה ב-2,470,000 ₪ [E1] והוחלה הפחתה של 10% [E2].", ["E1", "E2"], {"E1", "E2"}, NUMS) == []


def test_calculation_numbers_are_allowed():
    assert verify_answer("הממוצע הוא 26,000 ₪ למ״ר מתוך 4 עסקאות [E1].", ["E1"], {"E1"}, NUMS) == []


def test_unknown_citation_fails():
    assert "unknown_citation" in verify_answer("ראו [E9].", ["E9"], {"E1"}, NUMS)


def test_invented_number_fails():
    assert "unsupported_number" in verify_answer("המחיר היה 3,100,000 ₪ [E1].", ["E1"], {"E1"}, NUMS)


def test_links_and_markup_fail():
    assert "links_or_markup" in verify_answer("ראו https://evil.example/?q=secret [E1]", ["E1"], {"E1"}, NUMS)
    assert "links_or_markup" in verify_answer("![x](http://a) [E1]", ["E1"], {"E1"}, NUMS)


def test_answer_without_citations_fails():
    assert "no_citations" in verify_answer("זו תשובה בלי מקור.", [], {"E1"}, NUMS)


def test_single_digit_invented_number_fails():
    assert "unsupported_number" in verify_answer("ההפחתה הייתה 9% [E1].", ["E1"], {"E1"}, NUMS)


def test_list_ordinals_are_not_numbers():
    assert verify_answer("1. העסקה נמכרה ב-2,470,000 ₪ [E1].\n2. הוחלה הפחתה של 10% [E2].", ["E1", "E2"],
                         {"E1", "E2"}, NUMS) == []
