"""The shared history predicate (round 12 U3, KTD12): when a sentence, with the section it sits in, is history, so that
`derive` withholds its labelled date from the unit (U4 applies the same predicate in `resolve_as_of`). Synthetic
sentences only."""

from __future__ import annotations

import pytest

from app.chat.dates import is_history


@pytest.mark.parametrize("sentence, path", [
    ("הוערך הנכס בשווי 1,800,000 ₪", ("3. שומות קודמות",)),
    ("הוערך הנכס בשווי 1,800,000 ₪", ("שומות מקרקעין קודמות",)),
    ("שווי 1,800,000 ₪", ("2. היסטוריה", "2.1 פירוט")),
    ("נרכש בסכום 1,500,000 ₪", ("רכישות קודמות",)),
    ("סכום 1,500,000 ₪", ("עסקאות קודמות",)),
    ("הנכס נרכש ביום 15/05/2017 תמורת 1,500,000 ₪.", ()),
    ("הנכס נמכר בשנת 2019 ב-1,700,000 ₪.", ()),
    ("בשומה קודמת, לתאריך הקובע 31.12.21, הוערך הנכס ב-2,000,000 ₪.", ()),
    ("על פי שומות קודמות של הנכס, השווי היה 1,900,000 ₪.", ()),
    ("בעבר הוערך הנכס ב-2,100,000 ₪.", ()),
    ("קודם לכן הוערך הנכס ב-2,100,000 ₪.", ()),
    ("הוערך הנכס בשווי 2,000,000 ₪ בשומה קודמת.", ()),
    ("‏הנכס נרכש‏ ב-1,500,000 ₪", ()),   # bidi marks do not hide the cue
])
def test_what_is_history(sentence, path):
    assert is_history(sentence, path) is True


@pytest.mark.parametrize("sentence, path", [
    ("שווי הזכויות בנכס הוערך ב-2,640,000 ₪ נכון למועד הקובע.", ()),     # a bare "הוערך" is a current conclusion
    ("המועד הקובע: 31/12/2025", ("1. מטרת השומה",)),
    ("שווי הנכס: 2,410,000 ₪", ("3. ריכוז הערכות לנכס",)),
    ("הנכס הוערך לפי נתוני שוק עדכניים", ("2. שיקולי השמאי",)),
    ("הקודמים בתור לא נכנסו", ()),                                          # "קודם" alone is no cue
    ("", ()),
])
def test_what_is_not_history(sentence, path):
    assert is_history(sentence, path) is False


def test_the_past_cues_of_a_bare_valued_sentence_need_the_word_valued():
    assert is_history("בעבר שולם דמי חכירה של 20,000 ₪", ()) is False
    assert is_history("הוערך", ()) is False
