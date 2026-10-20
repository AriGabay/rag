"""Letter-spaced text-layer lines: join, leave or mark uncertain, on synthetic glyph data (U5, KTD12, R4).

A line is built the way pdfplumber hands it over: its text with a space wherever the layout put one, and one glyph
box per character (None for those spaces)."""

from __future__ import annotations

from app.extraction import chunking, pdf
from app.extraction import letterspacing as ls
from app.extraction.hebrew import fix_text_lines, strip_bidi_controls
from app.extraction.letterspacing import JOINED, PLAIN, UNCERTAIN, Glyph

# A Hebrew line of the same page: a digits-only line follows the file's orientation only on a page that holds Hebrew (round 20, U4).
HEBREW_LINE = "תינכת"


def line(words: list[str], pitch: float, word_pitch: float, *, width: float = 6.0, size_pt: float = 10.5,
         seq_desc: bool = False, x0: float = 100.0) -> tuple[str, list[Glyph | None]]:
    """Words drawn glyph by glyph, ``pitch`` apart inside a word and ``word_pitch`` from a word's last glyph to the
    next word's first (centre to centre); glyph boxes ``width`` wide. ``seq_desc``: the content stream runs against
    the x order."""
    raw, glyphs, x = "", [], x0
    for w, word in enumerate(words):
        if w:
            x += word_pitch - pitch
        for ch in word:
            if raw:
                raw += " "
                glyphs.append(None)
            glyphs.append(Glyph(ch, x, x + width, 10.0, 10.0 + size_pt))
            raw += ch
            x += pitch
    count = sum(1 for g in glyphs if g)
    seqs = iter(range(count - 1, -1, -1) if seq_desc else range(count))
    return raw, [None if g is None else Glyph(g.ch, g.x0, g.x1, g.top, g.bottom, next(seqs)) for g in glyphs]


def text_of(raw: str, verdict: ls.Verdict, glyphs) -> str:
    return ls.join_line(raw, glyphs, verdict).raw


# --- the decision ------------------------------------------------------------------------------------------------

def test_a_fixed_step_with_wider_steps_between_words_is_joined_into_those_words():
    raw, glyphs = line(["תכנית", "מתאר"], pitch=11.3, word_pitch=28.0)
    verdict = ls.read_line(raw, glyphs, 10.5)
    assert verdict.state == JOINED and verdict.note is None
    assert text_of(raw, verdict, glyphs) == "תכנית מתאר"


def test_a_fixed_tracking_with_glyphs_of_different_widths_is_joined_too():
    """Edge to edge uniform (tracking), centre to centre not: the glyphs differ in width."""
    raw, glyphs, x = "", [], 50.0
    widths = {"ו": 3.0, "ל": 5.0, "ם": 8.0, "ב": 6.0, "י": 2.5, "ת": 7.0}
    for w, word in enumerate(["ולם", "בית"]):
        x += 14.0 if w else 0.0
        for ch in word:
            if raw:
                raw += " "
                glyphs.append(None)
            glyphs.append(Glyph(ch, x, x + widths[ch], 10, 20.5))
            raw += ch
            x += widths[ch] + 5.0
    assert ls.read_line(raw, glyphs, 10.5).state == JOINED


def test_gaps_that_split_into_no_two_groups_are_uncertain_and_carry_the_runs_box():
    xs = [100, 111, 140, 149, 210, 223, 290, 302, 370]
    raw = " ".join("ABCDEFGHI")
    glyphs, k = [], 0
    for ch in raw:
        if ch == " ":
            glyphs.append(None)
        else:
            glyphs.append(Glyph(ch, xs[k], xs[k] + 6, 10, 20.5, k))
            k += 1
    verdict = ls.read_line(raw, glyphs, 10.5)
    assert verdict.state == UNCERTAIN and verdict.note == ls.NOTE_UNEVEN and verdict.words == []
    assert verdict.box == (100, 10, 376, 20.5)


def test_a_uniform_run_with_no_larger_gap_has_no_words_to_find_and_is_uncertain():
    raw, glyphs = line(["ABCDEFGH"], pitch=14.0, word_pitch=0)
    verdict = ls.read_line(raw, glyphs, 10.5)
    assert verdict.state == UNCERTAIN and verdict.note == ls.NOTE_NO_WORDS


def test_single_letter_words_an_ordinary_space_apart_are_not_letter_spacing():
    """"ו ב ה" with ordinary word spaces: even a uniform run of single letters is left alone."""
    raw, glyphs = line(["ו", "ב", "ה", "ג", "ד", "ח"], pitch=0, word_pitch=9.0, width=6.0)
    assert ls.read_line(raw, glyphs, 10.5).state == PLAIN


def test_a_split_whose_small_gaps_are_ordinary_word_spaces_is_left_alone():
    """"1 2 3   4 5 6": single digits a word space apart, in two columns: columns, not letter-spaced numbers."""
    raw, glyphs = line(["123", "456"], pitch=9.0, word_pitch=30.0, width=6.5)
    assert ls.read_line(raw, glyphs, 10.5).state == PLAIN


def test_an_ordinary_line_is_never_checked():
    raw = "כתובת הנכס: רחוב החצב 6 ו א"
    assert not ls.is_candidate(raw)
    glyphs = [None if c == " " else Glyph(c, i * 5.0, i * 5.0 + 4, 0, 10) for i, c in enumerate(raw)]
    assert ls.read_line(raw, glyphs, 10.5).state == PLAIN


def test_a_line_with_few_single_characters_is_not_a_candidate():
    assert not ls.is_candidate("2 4 2 מתאר תכנית אישור")  # three singles
    assert ls.is_candidate("2 4 2 4")
    assert not ls.is_candidate("2 4 2 4 תכנית מתאר תכנית אישור מספר תאריך")  # singles are a minority


# --- reading direction ------------------------------------------------------------------------------------------

def test_a_digit_word_drawn_right_to_left_is_flagged_and_one_drawn_left_to_right_is_not():
    for desc, flagged in ((True, True), (False, False)):
        raw, glyphs = line(["2424", "תכנית"], pitch=11.3, word_pitch=28.0, seq_desc=desc)
        verdict = ls.read_line(raw, glyphs, 10.5)
        joined = ls.join_line(raw, glyphs, verdict)
        assert joined.raw == "2424 תכנית"
        # the Hebrew word is never flagged: its order is the orientation fix's
        assert joined.flips == ([(0, 4)] if flagged else [])


def test_the_fixture_shapes_come_out_as_the_numbers_the_page_shows():
    """The R9e producer: glyphs drawn from the right, the first glyph of "4242" the rightmost. The orientation fix
    reverses the line; the flagged digit words end up in the order they were drawn."""
    raw, glyphs = line(["1202/60/12", "2424"], pitch=11.3, word_pitch=28.0, seq_desc=True)
    verdict = ls.read_line(raw, glyphs, 10.5)
    joined = ls.join_line(raw, glyphs, verdict)
    fixed = fix_text_lines([HEBREW_LINE, joined.raw], default_visual=True)[1]
    assert fixed == "2424 1202/60/12"  # the fix alone leaves each digit run in x order
    final = ls.apply_flips(joined.raw, fixed, joined.flips)
    assert final == "4242 21/06/2021"


def test_words_drawn_left_to_right_keep_their_order_through_the_fix():
    raw, glyphs = line(["2021/60/12", "4242"], pitch=11.3, word_pitch=28.0)
    joined = ls.join_line(raw, glyphs, ls.read_line(raw, glyphs, 10.5))
    fixed = fix_text_lines([HEBREW_LINE, joined.raw], default_visual=True)[1]
    assert ls.apply_flips(joined.raw, fixed, joined.flips) == "4242 2021/60/12"


def test_apply_flips_gives_up_when_the_fix_cannot_be_traced():
    assert ls.apply_flips("abc", "xyz", [(0, 3)]) is None
    assert ls.apply_flips("abc", "xyz", []) == "xyz"


def test_line_words_carry_the_boxes_of_the_words_in_final_order():
    raw, glyphs = line(["1202/60/12", "תינכת"], pitch=11.3, word_pitch=28.0, seq_desc=True)  # x order: visual
    joined = ls.join_line(raw, glyphs, ls.read_line(raw, glyphs, 10.5))
    kept = [glyphs[i] for i in joined.keep]
    fixed = fix_text_lines([joined.raw], default_visual=True)[0]
    final = ls.apply_flips(joined.raw, fixed, joined.flips)
    words = ls.line_words(joined.raw, fixed, final, kept, joined.ranges)
    assert [w.text for w in words] == ["תכנית", "21/06/2021"]
    assert all(w.joined for w in words)
    by_text = {w.text: w for w in words}
    assert by_text["תכנית"].x0 > by_text["21/06/2021"].x1  # the Hebrew word is to the right of the date


def test_a_page_without_orientation_evidence_reads_flagged_words_after_the_documents_orientation():
    """``orient_pages`` fixes a title page's lines again with the document's orientation; a flagged word is reversed
    back each time, and a fix that cannot be traced leaves the line uncertain."""
    raw, glyphs = line(["1202/60/12", "2424"], pitch=11.3, word_pitch=28.0, seq_desc=True)
    joined = ls.join_line(raw, glyphs, ls.read_line(raw, glyphs, 10.5))
    ln = pdf._Line(10.0, "", layer_text=joined.raw, flips=[list(f) for f in joined.flips])
    pdf._read_flips(ln, fix_text_lines([HEBREW_LINE, joined.raw], default_visual=True)[1])
    assert ln.text == "4242 21/06/2021" and ln.uncertain is None
    broken = pdf._Line(10.0, "", layer_text="abc", flips=[[0, 3]])
    pdf._read_flips(broken, "xyz")
    assert broken.text == "xyz" and broken.uncertain == ls.NOTE_ORDER


# --- bidi controls ---------------------------------------------------------------------------------------------------

def test_bidi_controls_are_stripped_from_every_listed_code_point():
    marks = "".join(chr(c) for c in (0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)))
    assert strip_bidi_controls(f"{marks}2. מצב{marks}") == "2. מצב"


def test_a_numbered_heading_preceded_by_a_mark_is_a_heading_with_its_label():
    for mark in ("\u200f", "\u200e", "\u202b", "\u2067"):
        assert chunking.is_heading(f"{mark}2. מצב תכנוני") and chunking.heading_label(f"{mark}2. מצב תכנוני") == "2"
        assert chunking.heading_label(f"{mark}9.1 תכנית מאושרת") == "9.1"


def test_the_raw_line_loses_its_marks_with_the_glyph_behind_each():
    slots = [{"text": "א"}, {"text": "\u200f"}, None, {"text": "ב"}]
    out = pdf._without_bidi({"text": "א\u200f ב", "slots": slots, "top": 1})
    assert out["text"] == "א ב" and out["slots"] == [slots[0], None, slots[3]]
    clean = {"text": "א ב", "slots": slots[:1]}
    assert pdf._without_bidi(clean) is clean
