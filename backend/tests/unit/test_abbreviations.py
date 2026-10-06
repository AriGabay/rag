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
    assert [h["n"] for h in out] == [0, 1, 9, 10]
