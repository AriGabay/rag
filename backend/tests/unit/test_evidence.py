"""The judge's evidence for a claim is the part of the source that covers it, wherever it is in the source.

Synthetic text only."""

from __future__ import annotations

from app.chat import evidence


def _section(filler_lines: int, *, late: str, early: str = "") -> str:
    lines = [early] if early else []
    lines += [f"פסקה {i}: תיאור כללי של הסביבה, התשתיות והנגישות בשכונה, ללא נתונים כספיים." for i in range(filler_lines)]
    lines.append(late)
    lines += [f"פסקת סיום {i}: הערות כלליות על מצב השוק באזור." for i in range(5)]
    return "\n".join(lines)


CLAIM = "שכר הדירה החודשי הוא 55 ₪ למ\"ר"
LATE = "בהתאם לסקר, שכר הדירה החודשי הראוי הוא 55 ₪ למ\"ר לחודש."


def test_evidence_after_character_1800_is_selected():
    text = _section(60, late=LATE)
    assert text.index(LATE) > 1800 and len(text) > evidence.WHOLE_SOURCE_CHARS
    got, excerpt = evidence.select([CLAIM], text)
    assert LATE in got and excerpt
    assert evidence.OMITTED in got
    assert len(got) < len(text) / 2


def test_the_same_number_with_another_meaning_is_shown_too():
    early = "במגרש קיימות 55 חניות תת-קרקעיות."
    text = _section(60, late=LATE, early=early)
    got, _ = evidence.select([CLAIM], text)
    assert early in got and LATE in got  # the judge sees both and decides by meaning


def test_neighbours_give_context():
    before, after = "פסקת הקשר: הסקר נערך בשנת 2023 בבניינים חדשים.", "הנתונים אינם כוללים דמי ניהול."
    lines = [f"פסקה {i}: תיאור כללי של הסביבה והנגישות בשכונה." for i in range(80)]
    lines[40:40] = [before, LATE, after]
    got, _ = evidence.select([CLAIM], "\n".join(lines))
    assert before in got and after in got


def test_a_table_row_comes_with_caption_header_size_and_notes():
    rows = [f"רחוב הדגמה {i} | {100 + i} | ₪ {40 + i} | משרדים בקומה גבוהה, מצב טוב" for i in range(1, 120)]
    table = "\n".join(["להלן נתוני היצע למשרדים:", "הטבלה: 119 שורות", "כתובת | שטח במ\"ר | שכ\"ד למ\"ר",
                       *rows, "(*) המחירים אינם כוללים מע\"מ"])
    assert len(table) > evidence.WHOLE_SOURCE_CHARS
    got, excerpt = evidence.select(["ברחוב הדגמה 9 שכר הדירה הוא 49 ₪ למ\"ר"], table, "table")
    assert excerpt
    for needed in ("להלן נתוני היצע למשרדים:", "הטבלה: 119 שורות", "כתובת | שטח במ\"ר | שכ\"ד למ\"ר",
                   "רחוב הדגמה 9 | 109 | ₪ 49 |", "(*) המחירים אינם כוללים מע\"מ"):
        assert needed in got, needed
    assert "רחוב הדגמה 60 |" not in got


def test_a_short_source_is_given_whole():
    text = "סיכום: השווי למ\"ר בנוי הוא 9,500 ₪.\nדמי השכירות הם 55 ₪ למ\"ר לחודש."
    assert evidence.select([CLAIM], text) == (text, False)


def test_a_claim_without_numbers_or_shared_words_never_gets_a_prefix():
    text = _section(60, late="המבנה נבנה בשיטת בנייה קונבנציונלית ונמצא במצב תחזוקה טוב.")
    got, excerpt = evidence.select(["מצב התחזוקה של המבנה טוב"], text)
    assert "מצב תחזוקה טוב" in got and excerpt
    assert not got.startswith(text[:300])


def test_abbreviations_match_their_spelled_out_form():
    text = _section(60, late="דמ\"ש ראויים למשרדים בגבולות של 64 ₪ למ\"ר.")
    got, _ = evidence.select(["דמי השכירות הראויים למשרדים בגבולות של 64 ₪ למ\"ר"], text)
    assert "דמ\"ש ראויים למשרדים" in got


def test_union_for_several_claims_of_one_call():
    text = _section(60, late=LATE, early="השווי למ\"ר בנוי ברוטו הוא 10,400 ₪ ללא מע\"מ.")
    got, _ = evidence.select([CLAIM, "השווי למ\"ר בנוי ברוטו הוא 10,400 ₪"], text)
    assert LATE in got and "10,400" in got


def test_narrow_keeps_only_number_matches_and_headers():
    text = _section(60, late=LATE)
    got, _ = evidence.select([CLAIM], text, narrow=True)
    assert LATE in got and "פסקת סיום 0" not in got
