from app.extraction.normalize_text import base_normalize, normalize_for_search, query_tokens


def test_prefix_variant_makes_ramat_gan_searchable():
    assert "רמת" in normalize_for_search("ברמת גן").split()


def test_gershayim_variants_unify():
    assert base_normalize('מ"ר') == base_normalize("מ״ר") == base_normalize("מ“ר")


def test_thousands_separators_removed_but_decimals_kept():
    assert base_normalize("1,250,000 ₪ ו-3.5 חדרים") == "1250000 ₪ ו 3.5 חדרים"


def test_niqqud_stripped():
    assert base_normalize("שָׁמַאי") == "שמאי"


def test_query_tokens_include_prefix_variants_and_dedupe():
    toks = query_tokens("השמאי המכריע בחרוזים")
    assert "שמאי" in toks and "מכריע" in toks and "חרוזים" in toks
    assert len(toks) == len(set(toks))


def test_block_parcel_token_kept():
    assert "6158/42" in query_tokens("גוש 6158/42")
