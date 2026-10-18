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


# --- round 16 U1: a long passage fitted to a limit, chosen by the claims and never by its prefix ----------------------------

def test_fit_keeps_the_segment_the_claim_needs_within_the_limit_and_not_the_first_lines():
    from app.chat.evidence import fit

    filler = [f"שורה מספר {n} של תיאור כללי בלי נתון מיוחד" for n in range(80)]
    text = "\n".join([*filler[:60], "תנאי ההיוון: השיעור 7.25% בכפוף לאישור", *filler[60:]])
    out, excerpt = fit(["השיעור 7.25%"], text, limit=500)
    assert excerpt and len(out) <= 500 and "7.25%" in out and filler[0] not in out
    assert fit(["x"], "קצר", limit=500) == ("קצר", False)


def test_fit_with_a_claim_nothing_covers_spreads_over_the_source():
    from app.chat.evidence import fit

    text = "\n".join(f"שורה מספר {n} של תיאור כללי בלי נתון מיוחד" for n in range(80))
    out, _ = fit(["מילים אחרות לגמרי"], text, limit=600)
    assert len(out) <= 600 and "שורה מספר 70 " in out and "שורה מספר 40 " in out


# --- the budget loop of ``fit_segments`` keeps a running length: its output is the one the render-per-trial loop gave --------


def _fit_segments_by_rendering(claims: list[str], segs: tuple, limit: int) -> tuple[str, bool]:
    """``fit_segments`` as it was before the running length: every candidate is rendered whole and measured. Kept here as the
    reference the property test compares the linear loop with."""
    if not segs:
        return "", False
    nums: set[str] = set()
    words: set[str] = set()
    for c in claims:
        n, w = evidence.claim_terms(c)
        nums |= n
        words |= w
    score = [3 * len(s.numbers & nums) + len(s.tokens & words) for s in segs]
    order = sorted((i for i, v in enumerate(score) if v), key=lambda i: (-score[i], i))
    if not order:
        step = max(1, len(segs) // 8)
        order = list(range(0, len(segs), step))
    headers = [i for i, s in enumerate(segs) if s.header]
    if sum(len(segs[i].text) + 1 for i in headers) > limit // 3:
        headers = []
    ranked = [*headers, *order]
    ranked += [j for i in order for j in (i - 1, i + 1) if 0 <= j < len(segs)]
    picked: set[int] = set()
    best = ("", True)
    for i in ranked:
        if i in picked:
            continue
        trial = evidence.render(segs, picked | {i})
        if len(trial[0]) <= limit:
            picked.add(i)
            best = trial
            if len(trial[0]) >= limit - 30:
                break
    return best if picked else ("", True)


def _synthetic_sources(rng) -> list[tuple[str, ...]]:
    """Segment sets of every shape the budget loop sees: prose of lines both short and long (a long line splits into
    sentences), tables with a size line, header rows and notes, one-character segments (a segment shorter than a marker
    shortens the rendering when it closes a gap), and very long sources."""
    vocab = ["שכר", "דירה", "חודשי", "שיעור", "היוון", "7.25%", "55", "1,200", "מ\"ר", "קומה", "חניה", "מחסן", "גג", "x", "ab"]

    def line(n_words: int) -> str:
        return " ".join(rng.choice(vocab) for _ in range(n_words))

    out: list[tuple[str, ...]] = []
    for _ in range(40):
        kind = rng.choice(["prose", "table", "tiny", "long", "huge"])
        if kind == "prose":
            lines = [line(rng.randint(1, 30)) for _ in range(rng.randint(1, 60))]
        elif kind == "tiny":
            lines = [line(1) if rng.random() < 0.5 else rng.choice("abc") for _ in range(rng.randint(1, 40))]
        elif kind == "long":
            lines = [". ".join(line(rng.randint(3, 12)) for _ in range(rng.randint(10, 80))) for _ in range(rng.randint(1, 5))]
        elif kind == "huge":
            lines = [line(rng.randint(1, 20)) for _ in range(rng.randint(300, 900))]
        else:
            lines = [f"{evidence.TABLE_SIZE_PREFIX} {rng.randint(2, 40)} שורות", "כותרת הטבלה"]
            lines += [" | ".join(line(rng.randint(1, 3)) for _ in range(rng.randint(2, 5))) for _ in range(rng.randint(2, 50))]
            lines += ["* הערה: " + line(rng.randint(2, 8))] * rng.randint(0, 2)
        out.append(tuple(lines))
    return out


def test_fit_segments_returns_what_the_render_per_trial_loop_returned_for_every_shape_of_source():
    import random

    rng = random.Random(1604)
    claims_pool = ["שכר דירה חודשי 55", "שיעור היוון 7.25%", "קומה חניה", "מילים שאין להן כיסוי", "1,200 מ\"ר", ""]
    compared = 0
    for lines in _synthetic_sources(rng):
        for kind in ("text", "table"):
            segs = evidence.segments_of("\n".join(lines), kind)
            for limit in (0, 1, 9, 10, 40, 120, 300, 700, 1500, 4000, 20000):
                claims = rng.sample(claims_pool, rng.randint(0, 3))
                got = evidence.fit_segments(claims, segs, limit)
                assert got == _fit_segments_by_rendering(claims, segs, limit), (len(segs), kind, limit, claims)
                compared += 1
    assert compared > 800


def test_fit_segments_agrees_with_the_reference_on_segments_of_any_length_and_any_headers():
    import random

    rng = random.Random(77)
    for _ in range(600):
        count = rng.randint(1, 25)
        segs = tuple(evidence.Segment(f"{rng.choice(['a', 'שכר 55', 'x' * rng.randint(1, 40)])} {n}" if rng.random() < 0.7
                                      else rng.choice("abc"), rng.random() < 0.2) for n in range(count))
        limit = rng.randint(0, 400)
        claims = rng.sample(["שכר 55", "a", "x 7", "b"], rng.randint(0, 3))
        assert evidence.fit_segments(claims, segs, limit) == _fit_segments_by_rendering(claims, segs, limit)
