from app.extraction.normalize_text import (
    base_normalize,
    inflection_variants,
    normalize_for_search,
    prefix_variants,
    query_tokens,
)


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


def test_prefix_variants_and_base_normalize_unchanged():
    # The turn parser depends on these exact behaviors; inflection variants live elsewhere.
    assert prefix_variants("ברמת") == ["רמת"]
    assert prefix_variants("מרפסות") == ["רפסות"]
    assert base_normalize("מרפסות") == "מרפסות"


def test_plural_query_includes_singular_forms():
    assert "מרפסת" in query_tokens("מרפסות")
    assert "ממ״ד" in query_tokens("ממ״דים")
    assert "חדר" in query_tokens("חדרים")


def test_index_side_gets_the_same_inflection_variants():
    # Symmetric with the query side: a singular question finds a plural passage.
    indexed = set(normalize_for_search("לדירה שתי מרפסות").split())
    assert {"מרפסות", "מרפסת"} <= indexed
    assert set(inflection_variants("מרפסת")) & indexed


def test_inflection_rule_skips_short_words():
    assert inflection_variants("בית") == []
    assert inflection_variants("שים") == []


def test_topic_tokens_exclude_places_years_and_stopwords():
    from app.platform.search import topic_tokens

    toks = topic_tokens("מה נכתב על מרפסות ברמת גן בשנת 2024?", ["רמת גן"])
    assert "מרפסת" in toks and "מרפסות" in toks
    assert not {"רמת", "ברמת", "גן", "2024", "מה", "על"} & set(toks)


def test_topic_tokens_use_gazetteer_without_caller_places():
    from app.platform.search import topic_tokens

    assert not {"רמת", "ברמת", "גן"} & set(topic_tokens("מרפסות ברמת גן"))


def test_place_only_question_keeps_place_as_topic():
    from app.platform.search import topic_tokens

    assert "רמת" in topic_tokens("מה נכתב על רמת גן?")


def test_block_parcel_and_prices_are_topic_terms():
    from app.platform.search import topic_tokens

    toks = topic_tokens("עסקה בגוש 6158/42 ב-2,470,000 ₪ בשנת 2021")
    assert "6158/42" in toks and "2470000" in toks and "2021" not in toks
