"""Letter-spaced text-layer lines (KTD12, R4).

Some producers draw a table or a heading glyph by glyph, far apart. The text layer then holds one character per
token (``'2 1 / 0 6 / 2 0 2 1'`` for a date, ``'מ ס פ ר'`` for a word) and word grouping fails: the plan number or
its date cannot be found by search. Joining such a line by its spaces would also join text that merely has single
letters in it, so a line is checked against its glyph spacing:

- a line is checked when at least ``MIN_SINGLES`` of its tokens are single characters and they are most of its
  tokens (ordinary Hebrew has a one-letter word now and then, never most of a line);
- inside the runs of single-character tokens, the distances between neighbouring glyphs are taken four ways: left
  edge to left edge and right edge to right edge (a producer placing glyphs at a fixed step from one side), centre
  to centre, and edge to edge (a producer adding a fixed tracking);
- the run is letter-spaced when, in one of them, the small distances are uniform and the larger ones are all
  clearly larger (``GROUP_RATIO``): those larger ones are the breaks between words, and every word is joined. A run
  whose distances show no such split is ``uncertain``, and so is a uniform one, where nothing tells a word from
  several words;
- spacing no wider than an ordinary word space (``SPACE_EM``) is no letter spacing: the line is left as read.

Glyphs written one at a time need a reading direction. The text-layer order is the glyphs' left to right order;
for Hebrew the logical order is its reverse, like any visual-order line. A word of digits, Latin letters and
separators is read in the order the producer drew it: drawn left to right it stays, drawn right to left (the
content stream runs against the glyphs' x order) it is reversed. That is a fact of the file, not of what the text
says (R29).

Everything here works on a line's text and its glyph geometry, so it can be tested on synthetic lines.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from app.extraction.geometry import logical_order

MIN_SINGLES = 4  # single-character tokens a line needs before its spacing is checked
SINGLE_SHARE = 0.5  # ... and they must be more than this share of its tokens
LETTER_TOLERANCE = 1.3  # distances within this factor of the smallest are the letter spacing
GROUP_RATIO = 1.6  # the distances between words are at least this much larger than the largest letter spacing
MIN_LETTER_SHARE = 0.4  # at least this share of the distances inside runs must be letter spacing
SPACE_EM = 0.35  # an edge gap up to this share of the type size is an ordinary word space

PLAIN, JOINED, UNCERTAIN = "plain", "joined", "uncertain"
NOTE_UNEVEN = "ריווח אותיות לא סדיר בשכבת הטקסט; לא ניתן היה לשחזר את המילים"
NOTE_NO_WORDS = "אותיות בודדות במרווח אחיד ללא הפרדה בין מילים בשכבת הטקסט; לא ניתן היה לשחזר את המילים"
NOTE_ORDER = "ריווח אותיות בשכבת הטקסט; לא ניתן היה לאמת את סדר הקריאה של המילים"
NOTES = (NOTE_UNEVEN, NOTE_NO_WORDS, NOTE_ORDER)

_TOKEN = re.compile(r"\S+")
_HEBREW = re.compile(r"[א-ת]")


@dataclass(frozen=True)
class Glyph:
    """One character of a text-layer line: its box on the page and its place in the content stream (``seq``, -1
    when unknown)."""

    ch: str
    x0: float
    x1: float
    top: float = 0.0
    bottom: float = 0.0
    seq: int = -1


@dataclass
class Verdict:
    state: str = PLAIN
    note: str | None = None
    words: list[tuple[int, int]] = field(default_factory=list)  # [start, end) of each word to join, in the raw line
    box: tuple[float, float, float, float] | None = None  # the runs checked, when the verdict is ``uncertain``


@dataclass
class Joined:
    """A line with its letter-spaced words joined: the new raw text, the indices of the old raw text that were kept
    (to carry glyphs along), the joined words' ranges in the new text, and those of them to read in reverse."""

    raw: str
    keep: list[int]
    ranges: list[tuple[int, int]]
    flips: list[tuple[int, int]]


def _runs(raw: str) -> tuple[list[list[tuple[int, int]]], int, int]:
    """Runs of at least two neighbouring single-character tokens, the single-token count and the token count."""
    tokens = [(m.start(), m.end()) for m in _TOKEN.finditer(raw)]
    runs: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    for tok in tokens:
        if tok[1] - tok[0] == 1:
            current.append(tok)
            continue
        if len(current) >= 2:
            runs.append(current)
        current = []
    if len(current) >= 2:
        runs.append(current)
    singles = sum(1 for s, e in tokens if e - s == 1)
    return runs, singles, len(tokens)


def is_candidate(raw: str) -> bool:
    runs, singles, total = _runs(raw)
    return bool(runs) and singles >= MIN_SINGLES and singles > SINGLE_SHARE * total


def _split(values: list[float]) -> tuple[float, float | None] | None:
    """(largest letter spacing, smallest word gap or None) when ``values`` split cleanly into the two, else None."""
    low = min(values)
    if low <= 0:
        return None
    letters = [v for v in values if v <= LETTER_TOLERANCE * low]
    rest = [v for v in values if v > LETTER_TOLERANCE * low]
    if len(letters) < MIN_LETTER_SHARE * len(values):
        return None
    if not rest:
        return max(letters), None
    if min(rest) < GROUP_RATIO * max(letters):
        return None
    return max(letters), min(rest)


def read_line(raw: str, glyphs: list[Glyph | None], size: float) -> Verdict:
    """The verdict on one text-layer line: ``raw`` is the line as the text layer read it (left to right), ``glyphs``
    one glyph per character (None for a space the layout inserted), ``size`` its type size."""
    if len(glyphs) != len(raw) or not is_candidate(raw):
        return Verdict()
    runs, _, _ = _runs(raw)
    pairs = [(a, b) for run in runs for a, b in zip(run, run[1:], strict=False)]
    if any(glyphs[a[0]] is None or glyphs[b[0]] is None for a, b in pairs):
        return _uncertain(runs, glyphs, NOTE_UNEVEN)
    edge = [glyphs[b[0]].x0 - glyphs[a[0]].x1 for a, b in pairs]
    series = [[glyphs[b[0]].x0 - glyphs[a[0]].x0 for a, b in pairs], [glyphs[b[0]].x1 - glyphs[a[0]].x1 for a, b in pairs],
              [(glyphs[b[0]].x0 + glyphs[b[0]].x1 - glyphs[a[0]].x0 - glyphs[a[0]].x1) / 2 for a, b in pairs], edge]
    found = []
    for values in series:
        split = _split(values)
        if split is not None:
            ratio = 0.0 if split[1] is None else split[1] / split[0]
            found.append((ratio, values, split))
    if not found:
        return Verdict() if statistics.median(edge) <= SPACE_EM * size else _uncertain(runs, glyphs, NOTE_UNEVEN)
    _, values, (letter_max, group_min) = max(found, key=lambda f: f[0])  # a clean split beats a uniform run
    letter_edges = [e for e, v in zip(edge, values, strict=True) if v <= letter_max]
    if statistics.median(letter_edges) <= SPACE_EM * size:
        return Verdict()  # the "letters" are an ordinary word apart: single-letter words, not letter spacing
    if group_min is None:
        return _uncertain(runs, glyphs, NOTE_NO_WORDS)
    words = []
    k = 0
    for run in runs:
        start = run[0][0]
        for i in range(1, len(run)):
            if values[k + i - 1] >= group_min:
                if run[i - 1][1] - start > 1:
                    words.append((start, run[i - 1][1]))
                start = run[i][0]
        if run[-1][1] - start > 1:
            words.append((start, run[-1][1]))
        k += len(run) - 1
    return Verdict(JOINED, None, words)


def _uncertain(runs, glyphs, note: str) -> Verdict:
    boxes = [glyphs[s] for run in runs for s, _ in run if glyphs[s] is not None]
    box = (min(g.x0 for g in boxes), min(g.top for g in boxes), max(g.x1 for g in boxes),
           max(g.bottom for g in boxes)) if boxes else None
    return Verdict(UNCERTAIN, note, [], box)


def _right_to_left_drawn(word: list[Glyph]) -> bool:
    """Whether the producer drew the word's glyphs against their x order (the content stream runs right to left)."""
    seqs = [g.seq for g in word]
    if len(seqs) < 2 or any(s < 0 for s in seqs):
        return False
    steps = [b - a for a, b in zip(seqs, seqs[1:], strict=False)]
    return sum(1 for s in steps if s < 0) > sum(1 for s in steps if s > 0)


def join_line(raw: str, glyphs: list[Glyph | None], verdict: Verdict) -> Joined:
    """``raw`` without the spaces inside the words ``verdict`` found. A word with no Hebrew letter that was drawn right
    to left is marked for reversal (``Joined.flips``): the orientation fix leaves such a run in x order."""
    inside: set[int] = set()
    for s, e in verdict.words:
        inside.update(i for i in range(s, e) if raw[i].isspace())
    keep = [i for i in range(len(raw)) if i not in inside]
    new_index = {old: new for new, old in enumerate(keep)}
    ranges, flips = [], []
    for s, e in verdict.words:
        span = (new_index[s], new_index[e - 1] + 1)
        ranges.append(span)
        word = [g for g in glyphs[s:e] if g is not None]
        if not _HEBREW.search(raw[s:e]) and _right_to_left_drawn(word):
            flips.append(span)
    return Joined("".join(raw[i] for i in keep), keep, ranges, flips)


def apply_flips(raw: str, text: str, flips: list[tuple[int, int]]) -> str | None:
    """``text`` (``raw`` after the orientation fix) with the flagged words reversed back to the order they were drawn
    in. None when the fix cannot be traced to ``raw`` (the words' places in ``text`` are then unknown)."""
    if not flips:
        return text
    order = logical_order(raw, text)
    if order is None:
        return None
    where = {raw_i: j for j, raw_i in enumerate(order)}
    chars = list(text)
    for s, e in flips:
        places = sorted(where[i] for i in range(s, e))
        if "".join(chars[p] for p in places) == raw[s:e]:  # the fix left the run in x order
            for p, ch in zip(places, raw[s:e][::-1], strict=True):
                chars[p] = ch
    return "".join(chars)


@dataclass(frozen=True)
class Word:
    """A word of a final line: its text, the box of its glyphs, and whether it was joined from spaced letters."""

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    joined: bool


def line_words(raw: str, fixed: str, final: str, glyphs: list[Glyph | None], ranges: list[tuple[int, int]]
               ) -> list[Word] | None:
    """The words of the final line ``final`` (in its order) with the glyph boxes behind them. ``fixed`` is ``raw``
    after the orientation fix, ``final`` the same with flagged words reversed: the permutation is traced on ``fixed``.
    None when the fix cannot be traced."""
    order = logical_order(raw, fixed)
    if order is None:
        return None
    joined = set()
    for s, e in ranges:
        joined.update(range(s, e))
    out = []
    for m in _TOKEN.finditer(final):
        idx = [order[j] for j in range(m.start(), m.end())]
        boxes = [glyphs[i] for i in idx if glyphs[i] is not None]
        if not boxes:
            continue
        out.append(Word(m.group(0), min(g.x0 for g in boxes), min(g.top for g in boxes), max(g.x1 for g in boxes),
                        max(g.bottom for g in boxes), any(i in joined for i in idx)))
    return out
