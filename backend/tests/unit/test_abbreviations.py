"""Query variants with Hebrew abbreviations spelled out or abbreviated, and row diversity in search results."""

from app.extraction.abbreviations import variants
from app.platform.search import diversify_rows


def test_spelled_out_term_is_also_searched_abbreviated():
    assert variants('דמי שכירות ראויים למ"ר')[0] == 'דמ"ש ראויים למ"ר'


def test_abbreviation_with_a_prefix_and_any_quote_mark_is_spelled_out():
    assert variants("מה הדמ״ש באותה שומה") == ["מה הדמי שכירות באותה שומה"]
    assert variants("כמה יח''ד יש") == [] or variants("כמה יח\"ד יש") == ["כמה יחידות דיור יש"]


def test_no_variant_without_a_known_form():
    assert variants("מה השווי שנקבע") == []


def _hit(kind, table=None, n=0):
    return {"kind": kind, "version_id": "v", "table_index": table, "n": n}


def test_rows_of_one_table_do_not_crowd_out_other_passages():
    hits = [_hit("table_row", 1, i) for i in range(6)] + [_hit("text", None, 9), _hit("table", 1, 10)]
    out = diversify_rows(hits, 5)
    # the table passage stands in for the rows past the cap, at the place of the first of them
    assert [h["n"] for h in out] == [0, 1, 10, 9]
    assert [h.get("rows_not_shown") for h in out] == [4, None, None, None]


def test_rows_past_the_cap_are_counted_when_the_table_passage_is_not_among_the_hits():
    hits = [_hit("table_row", 1, i) for i in range(3)] + [_hit("table_row", 2, 5), _hit("text", None, 9)]
    out = diversify_rows(hits, 8)
    assert [h["n"] for h in out] == [0, 1, 5, 9]
    assert [h.get("rows_not_shown") for h in out] == [1, None, None, None]
    assert all("rows_not_shown" not in h for h in hits)  # the caller's hits are not changed


def test_no_count_when_every_matching_row_is_shown():
    hits = [_hit("table_row", 1, 0), _hit("table_row", 1, 1), _hit("text", None, 9)]
    assert all("rows_not_shown" not in h for h in diversify_rows(hits, 8))


def test_every_spelling_of_the_square_metre_is_one_term():
    assert variants("שווי מטר מרובע מבונה") == ['שווי מ"ר מבונה']
    assert variants('שווי מ"ר מבונה')[0] == "שווי מטר רבוע מבונה"


def test_open_space_and_planning_abbreviations_are_spelled_out():
    assert variants('מה שטח השפ"פ') == ["מה שטח השטח פרטי פתוח"]
    assert variants("גובל בשטח ציבורי פתוח") == ['גובל בשצ"פ']
    assert variants("לפי תכנית בניין עיר") == ['לפי תב"ע']
    assert variants('לפי התב"ע') == ["לפי התוכנית בניין עיר"]
