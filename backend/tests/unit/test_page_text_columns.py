"""Page-text column binding from stored word positions (round 23 U4, M3, KTD8, R5, R16, R19; AE4).

The rows are paragraph blocks of the PDF text layer (a block per row) with the per-word spans the extraction stores
(``[line, word, start, end, x0, top, x1, bottom]``, display frame); the header is another block above, on the same page or on the previous
one. Every name, label and figure is invented. The pure binder is ``app.chat.columns``; the serve path (``tools._served_rows``), the exact
citation of a cell and the replay are in ``tests/integration/test_chat_page_text_columns.py``."""

from __future__ import annotations

import random

import pytest

from app.chat import columns as C

H = 10.0  # a word's height


def block(words: list[tuple[str, float, float]], top: float = 100.0, line: int = 0):
    """``(text, spans)`` of one line: ``words`` are ``(word, x0, x1)`` in the stored (logical) order."""
    text, spans = "", []
    for i, (w, x0, x1) in enumerate(words):
        text += (" " if text else "")
        spans.append([line, i, len(text), len(text) + len(w), x0, top, x1, top + H])
        text += w
    return text, spans


def stack(rows: list[list[tuple[str, float, float]]], top: float = 100.0):
    """A block of several lines (a stacked header): ``(text, spans)`` with the offsets of the joined text."""
    text, spans = "", []
    for li, words in enumerate(rows):
        t, s = block(words, top + li * (H + 2), li)
        off = len(text) + (1 if text else 0)
        text += ("\n" if text else "") + t
        spans += [[sp[0], sp[1], sp[2] + off, sp[3] + off, *sp[4:]] for sp in s]
    return text, spans


def attempt(page: int, *blocks: tuple[str, list], first: int = 0, before: int | None = None) -> list[C.Line]:
    return C.page_lines(page, [(first + i, t, s) for i, (t, s) in enumerate(blocks)], before)


QUARTERS = [("Q1", 100, 130), ("Q2", 200, 230), ("Q3", 300, 330)]


def quarter_header(page: int = 1, first: int = 0) -> list[C.Line]:
    return attempt(page, block(QUARTERS), first=first)


def reversed_row(label: str, values: tuple[str, str, str]):
    """A row whose stored order is the reverse of the drawn one: the third column first. Drawn: values[0] under Q1."""
    return block([(label, 20, 70), (values[2], 300, 340), (values[1], 200, 240), (values[0], 100, 140)], top=120)


def test_a_latin_labelled_numeric_row_stored_reversed_is_served_in_drawn_order_with_its_column_labels():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    assert text == "Revenue 3,300 2,200 1,100"
    b = C.bind(text, spans, [quarter_header()])
    assert b.status == C.BOUND, b
    assert b.served == "Revenue — Q1: 1,100; Q2: 2,200; Q3: 3,300"
    assert b.labels == ("Q1", "Q2", "Q3")


def test_a_row_stored_in_drawn_order_is_labelled_the_same_way():
    text, spans = block([("Cost", 20, 60), ("1,100", 100, 140), ("2,200", 200, 240), ("3,300", 300, 340)], top=120)
    b = C.bind(text, spans, [quarter_header()])
    assert b.served == "Cost — Q1: 1,100; Q2: 2,200; Q3: 3,300"


def test_a_digits_only_row_has_no_label_before_its_pairs():
    text, spans = block([("3,300", 300, 340), ("2,200", 200, 240), ("1,100", 100, 140)], top=120)
    assert C.bind(text, spans, [quarter_header()]).served == "Q1: 1,100; Q2: 2,200; Q3: 3,300"


def test_a_right_to_left_hebrew_row_is_not_touched():
    text, spans = block([("1,100", 100, 140), ("2,200", 200, 240), ("סה״כ", 300, 340)], top=120)
    b = C.bind(text, spans, [quarter_header()])
    assert b.status == C.NOT_ROW and b.served == ""
    assert not C.unbound_note_due(text)


def test_a_sentence_with_two_numbers_is_no_row():
    text, spans = block([(w, 20 + 40 * i, 55 + 40 * i) for i, w in enumerate("The area was 450 on 12 plots".split())], top=120)
    assert C.bind(text, spans, [quarter_header()]).status == C.NOT_ROW


def test_a_row_with_no_spans_is_unknown_and_the_judge_is_told():
    text = "Revenue 3,300 2,200 1,100"
    b = C.bind(text, None, [quarter_header()])
    assert b.status == C.UNKNOWN and b.served == ""
    assert C.unbound_note_due(text)
    assert not C.unbound_note_due("Revenue 2,200 1,100")  # two numbers: no note, though the binder may bind it


def test_spans_that_miss_a_word_of_the_row_leave_it_unknown():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    assert C.bind(text, spans[:-1], [quarter_header()]).status == C.UNKNOWN


def test_a_row_with_no_header_found_is_unknown():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    assert C.bind(text, spans, [[]]).status == C.UNKNOWN
    assert C.bind(text, spans, []).status == C.UNKNOWN


def test_a_continuation_row_binds_to_the_header_of_the_previous_page():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    on_this_page = attempt(2, block([("Page", 20, 50), ("title", 55, 90)], top=40), before=7)
    b = C.bind(text, spans, [on_this_page, quarter_header(page=1)])
    assert b.status == C.BOUND and b.served == "Revenue — Q1: 1,100; Q2: 2,200; Q3: 3,300"


def test_a_header_of_the_previous_page_in_other_columns_binds_nothing():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    elsewhere = attempt(1, block([("Q1", 500, 530), ("Q2", 600, 630), ("Q3", 700, 730)]))
    assert C.bind(text, spans, [[], elsewhere]).status == C.UNKNOWN


STACKED = [
    [("Gross", 100, 135), ("Gross", 200, 235), ("Net", 300, 320)],
    [("rent", 100, 125), ("rent", 200, 225), ("area", 300, 325)],
    [("annual", 100, 140), ("monthly", 200, 250), ("(sqm)", 300, 335)],
]


def test_a_header_stacked_over_three_lines_with_two_similar_labels_binds_each_number_to_its_own():
    text, spans = block([("Unit", 20, 60), ("300", 300, 320), ("20,000", 200, 250), ("240,000", 100, 160)], top=150)
    header = attempt(1, stack(STACKED))
    b = C.bind(text, spans, [header])
    assert b.status == C.BOUND, b
    assert b.served == "Unit — Gross rent annual: 240,000; Gross rent monthly: 20,000; Net area (sqm): 300"


def test_the_stacked_header_may_be_three_blocks_one_line_each():
    text, spans = block([("Unit", 20, 60), ("240,000", 100, 160), ("20,000", 200, 250), ("300", 300, 320)], top=150)
    header = attempt(1, *[block(r, top=100 + i * (H + 2)) for i, r in enumerate(STACKED)])
    assert C.bind(text, spans, [header]).served == "Unit — Gross rent annual: 240,000; Gross rent monthly: 20,000; Net area (sqm): 300"


def test_a_label_of_several_words_stays_one_label():
    header = attempt(1, block([("Net", 100, 120), ("operating", 124, 170), ("income", 174, 210), ("Area", 300, 330)]))
    text, spans = block([("Lot", 20, 50), ("55,000", 140, 190), ("1,200", 300, 330)], top=130)
    assert C.bind(text, spans, [header]).served == "Lot — Net operating income: 55,000; Area: 1,200"


def test_a_label_over_two_columns_is_in_the_label_of_both():
    header = attempt(1, block([("2024", 120, 260)], top=100),
                     block([("Q1", 100, 130), ("Q2", 200, 230)], top=112), first=0)
    text, spans = block([("Cost", 20, 60), ("2,200", 200, 240), ("1,100", 100, 140)], top=130)
    assert C.bind(text, spans, [header]).served == "Cost — 2024 Q1: 1,100; 2024 Q2: 2,200"


def test_a_number_under_two_labels_or_under_none_leaves_the_row_unknown():
    wide = block([("Cost", 20, 60), ("12,345,678,901", 110, 290), ("5", 300, 305)], top=130)
    assert C.bind(*wide, [quarter_header()]).status == C.UNKNOWN  # the first number lies over Q1 and Q2
    off = block([("Cost", 20, 60), ("1,100", 100, 140), ("7", 450, 460)], top=130)
    assert C.bind(*off, [quarter_header()]).status == C.UNKNOWN  # the second is under no label


def test_two_numbers_under_one_label_leave_the_row_unknown():
    text, spans = block([("Cost", 20, 60), ("1,100", 100, 140), ("1,200", 102, 142)], top=130)
    assert C.bind(text, spans, [quarter_header()]).status == C.UNKNOWN


def test_a_sign_beside_its_number_stays_with_it_and_a_sign_between_numbers_is_a_range():
    text, spans = block([("Rate", 20, 60), ("5", 100, 105), ("%", 106, 112), ("6", 200, 205), ("%", 206, 212), ("7", 300, 305), ("%", 306, 312)], top=130)
    assert C.bind(text, spans, [quarter_header()]).served == "Rate — Q1: 5 %; Q2: 6 %; Q3: 7 %"
    assert C.row_shape("Range 5 - 6 7") is None


def test_each_served_value_maps_back_to_its_stored_word_span():
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    b = C.bind(text, spans, [quarter_header()])
    at = b.served.index("2,200")
    (seg,) = C.stored_segments(b.parts, at, at + 5)
    assert text[seg[0]:seg[1]] == "2,200"
    # a partly quoted number maps to the same characters of the stored word
    (seg,) = C.stored_segments(b.parts, at + 1, at + 4)
    assert text[seg[0]:seg[1]] == ",20"
    # the words of a label are no stored place; the row's own label is
    assert C.stored_segments(b.parts, b.served.index("Q2"), b.served.index("Q2") + 2) == []
    assert [text[a:b_] for a, b_ in C.stored_segments(b.parts, 0, len("Revenue"))] == ["Revenue"]


@pytest.mark.parametrize("seed", range(6))
def test_adding_an_unrelated_paragraph_shuffling_the_rows_and_changing_the_numbers_keep_the_binding(seed):
    rnd = random.Random(seed)
    prose = block([(w, 20 + 40 * i, 55 + 40 * i) for i, w in enumerate("The figures below were prepared for the review of the year".split())], top=110)
    header_block = block(QUARTERS)
    labels_seen = set()
    for variant in range(3):
        rows = []
        for r in range(5):
            vals = tuple(f"{rnd.randint(1, 9)},{rnd.randint(100, 999)}" for _ in range(3))
            drawn = [("Row" + str(r), 20, 60), (vals[0], 100, 140), (vals[1], 200, 240), (vals[2], 300, 340)]
            stored = drawn if variant % 2 else [drawn[0], *reversed(drawn[1:])]
            rows.append((vals, block(stored, top=140 + 12 * r)))
        rnd.shuffle(rows)
        lines = attempt(1, header_block, prose, before=None) if variant == 0 else attempt(1, header_block, first=0)
        for vals, (text, spans) in rows:
            b = C.bind(text, spans, [lines])
            assert b.status == C.BOUND, (variant, text)
            assert b.served == f"{text.split()[0]} — Q1: {vals[0]}; Q2: {vals[1]}; Q3: {vals[2]}"
            labels_seen.add(b.labels)
    assert labels_seen == {("Q1", "Q2", "Q3")}


def test_a_short_unrelated_line_beside_the_header_does_not_change_a_column_it_does_not_touch():
    header = attempt(1, block(QUARTERS), block([("Note", 20, 50)], top=60))
    text, spans = reversed_row("Revenue", ("1,100", "2,200", "3,300"))
    assert C.bind(text, spans, [header]).served == "Revenue — Q1: 1,100; Q2: 2,200; Q3: 3,300"


def test_a_row_that_ends_with_its_date_is_a_row_and_a_date_inside_prose_is_not():
    """Round 23 U4 (measured on stored drafts): a row of numbers whose last cell is its date ("31/12/2026") is a row; the date column is a column."""
    assert C.row_shape("8.5% 8.1% 4,300,000 0 50,000- 4,350,000 31/12/2026") is not None
    assert C.row_shape("1.25% 7,000 01.02.27") is not None
    assert C.number_token("31/12/2026") and C.number_token("1.2.27")
    assert C.row_shape("the deal closed on 31/12/2026 at 4,000") is None  # a sentence stays no row


# --- header pick and ambiguity heuristics (round 23 review #2) ---------------------------------------------------------------------------
# Tests only: the heuristics of ``pick_header`` (the stack, the prose line, the years-only line) and the two ways a row stays unknown on purpose
# (an ambiguous column, a row of several lines). Invented labels and figures.

ROW_AT = 200.0  # the row's top in these tests; a header line directly above it sits at ROW_AT - 2 * (H + 2) or so


def _row(top: float = ROW_AT):
    return block([("Cost", 20, 60), ("1,100", 100, 140), ("2,200", 200, 240), ("3,300", 300, 340)], top=top)


TOTALS = [("Total", 100, 130), ("Total", 200, 230), ("Total", 300, 330)]


def test_a_label_line_beyond_the_stack_gap_above_the_header_does_not_contribute():
    far = attempt(1, block(TOTALS, top=100), block(QUARTERS, top=150))  # 40 above the Q line's top (gap 40 - H = 30 > LINE_GAP * H)
    near = attempt(1, block(TOTALS, top=138), block(QUARTERS, top=150))  # directly above it: part of the stack
    b = C.bind(*_row(), [far])
    assert b.status == C.BOUND and b.labels == ("Q1", "Q2", "Q3")
    assert b.served == "Cost — Q1: 1,100; Q2: 2,200; Q3: 3,300"
    assert C.bind(*_row(), [near]).labels == ("Total Q1", "Total Q2", "Total Q3")  # negative control: the same line, stacked
    assert [ln.top for ln in C.pick_header(far, [(100, 140)])] == [150.0]
    # the gap is measured per line: a third line just above the far one is cut with it, whatever it says
    cut = attempt(1, block([("Report", 100, 130), ("Report", 200, 230), ("Report", 300, 330)], top=88), block(TOTALS, top=100), block(QUARTERS, top=150))
    assert C.bind(*_row(), [cut]).labels == ("Q1", "Q2", "Q3")


PROSE = "The figures below were prepared for the annual review".split()  # nine words


def _prose(n: int, top: float):
    return block([(w, 20 + 34 * i, 50 + 34 * i) for i, w in enumerate(PROSE[:n])], top=top)


def test_a_prose_line_of_seven_words_between_the_header_and_the_row_is_skipped_and_the_header_below_still_binds():
    header = block(QUARTERS, top=100)
    for n in (7, 9):
        lines = attempt(1, header, _prose(n, top=130))  # the prose is nearer the row than the header, and over its columns
        b = C.bind(*_row(), [lines])
        assert b.status == C.BOUND and b.served == "Cost — Q1: 1,100; Q2: 2,200; Q3: 3,300", (n, b)
        assert [ln.top for ln in C.pick_header(lines, [(100, 140)])] == [100.0]
    # negative control: a line of six words is still a label group, so it is the header the row is read under (here, over two of its columns)
    six = attempt(1, header, _prose(6, top=130))
    assert [ln.top for ln in C.pick_header(six, [(100, 140)])][0] == 130.0
    assert C.bind(*_row(), [six]).labels != ("Q1", "Q2", "Q3")


def test_a_header_of_years_only_binds_and_a_line_with_another_number_is_no_header():
    years = attempt(1, block([("2022", 100, 130), ("2023", 200, 230), ("2024", 300, 330)], top=150))
    b = C.bind(*_row(), [years])
    assert b.status == C.BOUND and b.served == "Cost — 2022: 1,100; 2023: 2,200; 2024: 3,300"
    assert C.bind(*_row(), [years]).labels == ("2022", "2023", "2024")
    # negative controls: a line of values (a number that is no year) is no header, a year with a letter-bearing label stays one
    values = attempt(1, block([("2022", 100, 130), ("5,400", 200, 230), ("2024", 300, 330)], top=150))
    assert C.bind(*_row(), [values]).status == C.UNKNOWN
    mixed = attempt(1, block([("FY2022", 100, 140), ("2023", 200, 230), ("2024", 300, 330)], top=150))
    assert C.bind(*_row(), [mixed]).status == C.BOUND


AMBIGUOUS_STACK = [
    [("Gross", 100, 130), ("Net", 150, 180), ("Zone", 300, 330)],  # two groups of one line ...
    [("rent", 100, 140)],  # ... a label over the first only ...
    [("annual", 135, 160), ("area", 300, 330)],  # ... and one that overlaps that label and the second group: one column holds both groups
]


def _two_cells(top: float = 190.0):
    return block([("Lot", 20, 50), ("5", 110, 138), ("7", 300, 320)], top=top)


def test_a_header_whose_two_groups_of_one_line_fall_in_one_column_leaves_the_number_unknown():
    text, spans = _two_cells()
    ambiguous = attempt(1, stack(AMBIGUOUS_STACK, top=140))
    cols = C.columns_of(C.pick_header(ambiguous, [(110, 138), (300, 320)]))
    assert [c.ambiguous for c in cols] == [True, False]
    b = C.bind(text, spans, [ambiguous])
    assert b.status == C.UNKNOWN and b.reason == "a number under no label or two"
    # negative control: without the label that joins the two groups the same row is bound
    clear = attempt(1, stack([AMBIGUOUS_STACK[0], AMBIGUOUS_STACK[1], [("area", 300, 330)]], top=140))
    assert C.bind(text, spans, [clear]).status == C.BOUND
    assert C.bind(text, spans, [clear]).labels == ("Gross rent", "Zone area")


def test_a_row_whose_words_carry_two_line_numbers_is_unknown_several_lines():
    text, spans = block([("Cost", 20, 60), ("1,100", 100, 140), ("2,200", 200, 240), ("3,300", 300, 340)], top=ROW_AT)
    assert C.bind(text, spans, [quarter_header()]).status == C.BOUND
    for k in (1, 2, 3):  # whichever word is on another line, and the number of words that are
        moved = [list(s) for s in spans]
        for s in moved[k:]:
            s[0] = 1
        b = C.bind(text, moved, [quarter_header()])
        assert b.status == C.UNKNOWN and b.reason == "several lines", (k, b)
        assert not b.served


def test_a_header_stack_longer_than_the_most_lines_is_cut_at_that_many():
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
    assert len(words) > C.MAX_HEADER_LINES
    rows = [[(w, 100, 130), (w.upper(), 200, 230)] for w in words]  # eight lines, one under another, each overlapping the next
    header = attempt(1, stack(rows, top=100))
    picked = C.pick_header(header, [(100, 140)])
    assert len(picked) == C.MAX_HEADER_LINES
    text, spans = block([("Lot", 20, 50), ("1,100", 100, 140), ("2,200", 200, 240)], top=100 + len(words) * (H + 2) + 4)
    b = C.bind(text, spans, [header])
    assert b.status == C.BOUND
    assert b.labels[0] == " ".join(words[-C.MAX_HEADER_LINES:])  # the nearest six, top to bottom: the two lines above them do not contribute
    assert "alpha" not in b.labels[0] and "beta" not in b.labels[0]
    # negative control: a stack of exactly the most lines is whole
    few = attempt(1, stack(rows[:C.MAX_HEADER_LINES], top=100))
    assert len(C.pick_header(few, [(100, 140)])) == C.MAX_HEADER_LINES
