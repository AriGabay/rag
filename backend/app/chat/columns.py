"""Page-text column binding from stored word positions (round 23 U4, M3, KTD8, R5).

A row of a column layout that the PDF text layer stored as page text (a paragraph block per row) says its numbers in the stored logical
order, which for a line of digits on a Hebrew page can be the reverse of the order they are drawn in; the column header is another
paragraph block above, sometimes on the previous page. Nothing stored says which header word a number is under. The stored word spans
(``document_blocks.spans``: ``[line, word, start, end, x0, top, x1, bottom]``, already in the rendered page's frame, so a rotated or
CropBox-offset page needs no conversion here) do: this module reads them, at serve time, and never rewrites anything.

``bind`` takes one row block's text and spans and the header candidates (the lines above it, nearest first, one attempt per page: the
row's own page, then the previous ones) and returns either a *bound* row (every number under exactly one column label, served as
"label: value" pairs in drawn order, each pair's value a verbatim copy of the stored word span, ``Binding.parts`` mapping it back) or an
*unknown* one (no positions, no header, a number under no label or under two): then the row is served as stored and nothing is removed for
it. A row that is no left-to-right numeric row (a Hebrew word in it, a sentence) is ``not_row``: never touched.

Pure functions over plain values; the reads of the blocks are ``reader.page_words`` and ``tools._served_rows``."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import median

HEBREW = re.compile(r"[֐-׿]")
_STRIP = "()[]₪$€£%-−–+*:;"
_NUMBER = re.compile(r"\d[\d.,]*\d|\d")
_YEAR = re.compile(r"(?:19|20)\d\d")
_TOKEN = re.compile(r"\S+")
_SUFFIX_SIGNS = {"%", "‰"}

MIN_CELLS = 2  # numbers a row needs for its columns to be told apart
NOTE_NUMBERS = 3  # numbers of a row that has no positions for the judge to be told its columns are unknown (``meaning.COLUMN_ROW_NUMBERS``)
MAX_ROW_LABEL_WORDS = 5  # words before the first number of a row; more is a sentence
MAX_LABEL_WORDS = 6  # words of one header label (a longer group of words is prose between the header and the rows)
GAP_FACTOR = 0.6  # a gap wider than this many word heights starts another column label
LINE_GAP = 1.0  # the most (in word heights) between the lines of a stacked header
MAX_HEADER_LINES = 6
OVERLAP = 0.5  # points of horizontal overlap that count as "under"
PAGES_BACK = 2  # previous pages a continuation looks at for its header


_DATE = re.compile(r"\d{1,2}[/.]\d{1,2}[/.](?:\d{4}|\d{2})")


def number_token(token: str) -> bool:
    """Whether ``token`` is a written number (digits with separators, a sign, a currency mark or a percent sign around them) or a written
    date ("31/12/2022", "1.1.26"): a date is a cell of a row of numbers, the value of a date column (round 23 U4 measurement: rows that end
    with their date were not rows, so a reversed table with a date column was never bound)."""
    core = token.strip(_STRIP)
    return bool(core) and (_NUMBER.fullmatch(core) is not None or _DATE.fullmatch(core) is not None)


def year_like(token: str) -> bool:
    return _YEAR.fullmatch(token.strip(_STRIP)) is not None


def symbol_token(token: str) -> bool:
    """A token of no letter and no digit (a currency or percent sign standing apart from its number)."""
    return not any(c.isalnum() for c in token)


@dataclass(frozen=True)
class Word:
    line: int
    start: int
    end: int
    x0: float
    top: float
    x1: float
    bottom: float
    text: str


def words_of(text: str, spans: Sequence | None) -> list[Word] | None:
    """The words a block's stored ``spans`` place, with their text read from ``text``; None when the spans are absent or do not index this
    text (an offset outside it, a word with a space, a box that is no box)."""
    if not spans:
        return None
    out: list[Word] = []
    try:
        for s in spans:
            line, _, start, end, x0, top, x1, bottom = s
            line, start, end = int(line), int(start), int(end)
            x0, top, x1, bottom = float(x0), float(top), float(x1), float(bottom)
            word = text[start:end]
            if not (0 <= start < end <= len(text)) or not word or any(c.isspace() for c in word) or x1 < x0 or bottom < top:
                return None
            out.append(Word(line, start, end, x0, top, x1, bottom, word))
    except (TypeError, ValueError):
        return None
    return sorted(out, key=lambda w: w.start)


# --- the row ------------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Shape:
    """A row read from its text alone: the tokens before the first number (the row's label) and its cells, each cell the token indexes
    of one number with the currency or percent sign beside it."""

    tokens: tuple[tuple[int, int], ...]
    prefix: tuple[int, ...]
    cells: tuple[tuple[int, ...], ...]


def row_shape(text: str) -> Shape | None:
    """The shape of a left-to-right row of numbers (``MIN_CELLS`` or more, after at most ``MAX_ROW_LABEL_WORDS`` label words, nothing between or
    after them), or None: a block of several lines, one with a Hebrew letter in it (a right-to-left row stays as stored), a sentence, a
    number between two words, a sign between two numbers (a range)."""
    if not text or "\n" in text.strip() or HEBREW.search(text):
        return None
    tokens = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
    kinds = []
    for a, b in tokens:
        w = text[a:b]
        kinds.append("N" if number_token(w) else "S" if symbol_token(w) else "W")
    cell_of: dict[int, int] = {}
    cells: list[list[int]] = []
    for i, k in enumerate(kinds):
        if k != "N":
            continue
        cell_of[i] = len(cells)
        cells.append([i])
    for i, k in enumerate(kinds):
        if k != "S":
            continue
        near = [j for j in (i - 1, i + 1) if j in cell_of]
        if len(near) == 2 and text[tokens[i][0]:tokens[i][1]] in _SUFFIX_SIGNS:  # "5 % 6 %": the percent sign follows its number
            near = near[:1]
        if len(near) == 2:  # a sign between two numbers: a range or a separator, not a unit
            return None
        if near:
            cell = cells[cell_of[near[0]]]
            cell.append(i)
            cell.sort()
    if len(cells) < MIN_CELLS:
        return None
    in_cell = {i for c in cells for i in c}
    first = min(in_cell)
    prefix = tuple(i for i in range(first) if i not in in_cell)
    if len(prefix) > MAX_ROW_LABEL_WORDS or any(i not in in_cell and i > first for i in range(len(tokens))):
        return None
    for cell in cells:  # the tokens of a cell are next to each other
        if cell[-1] - cell[0] != len(cell) - 1:
            return None
    return Shape(tuple(tokens), prefix, tuple(tuple(c) for c in sorted(cells)))


def unbound_note_due(text: str) -> bool:
    """Whether a row whose columns could not be told is one the judge is told about: a row of ``NOTE_NUMBERS`` numbers or more."""
    shape = row_shape(text)
    return shape is not None and len(shape.cells) >= NOTE_NUMBERS


# --- the header ---------------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Line:
    page: int
    block: int
    line: int
    words: tuple[Word, ...]

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def height(self) -> float:
        return median(w.bottom - w.top for w in self.words) or 1.0


def lines_of(page: int, block: int, text: str, spans: Sequence | None) -> list[Line]:
    """The lines of one block, with words that have positions; last line first."""
    words = words_of(text, spans)
    if not words:
        return []
    by_line: dict[int, list[Word]] = {}
    for w in words:
        by_line.setdefault(w.line, []).append(w)
    return [Line(page, block, n, tuple(ws)) for n, ws in sorted(by_line.items(), reverse=True)]


def page_lines(page: int, blocks: Iterable[tuple[int, str, Sequence | None]], before: int | None = None) -> list[Line]:
    """The lines of ``blocks`` (``(block_index, text, spans)``) of one page above block ``before`` (all of them when None), nearest first."""
    out: list[Line] = []
    for index, text, spans in sorted(blocks, key=lambda b: -b[0]):
        if before is None or index < before:
            out.extend(lines_of(page, index, text, spans))
    return out


@dataclass(frozen=True)
class Group:
    line: Line
    words: tuple[Word, ...]

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def label(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.start))


def _overlap(a0: float, a1: float, b0: float, b1: float) -> bool:
    return min(a1, b1) - max(a0, b0) > OVERLAP


def groups_of(line: Line) -> list[Group]:
    """The words of a header line grouped by horizontal gap: one group per column label (a label of several words stays one)."""
    ws = sorted(line.words, key=lambda w: w.x0)
    limit = GAP_FACTOR * line.height
    groups: list[list[Word]] = []
    for w in ws:
        if groups and w.x0 - max(x.x1 for x in groups[-1]) <= limit:
            groups[-1].append(w)
        else:
            groups.append([w])
    return [Group(line, tuple(g)) for g in groups]


def header_like(line: Line) -> bool:
    """Whether a line can be column labels: it has a word with a letter, or only years, and no other number (a row of values is no header)."""
    texts = [w.text for w in line.words]
    numbers = [t for t in texts if number_token(t)]
    if any(not year_like(t) for t in numbers):
        return False
    return any(any(c.isalpha() for c in t) for t in texts) or (bool(numbers) and len(numbers) == len(texts))


def pick_header(lines: Sequence[Line], cells: Sequence[tuple[float, float]]) -> list[Line]:
    """The lines of the header above the row, nearest first: the nearest header-like line with a label under one of the row's numbers, then the
    lines stacked directly above it. A line of prose (a long group of words) and a line of values are passed over."""
    accepted: list[Line] = []
    for line in lines:
        if not header_like(line):
            continue
        groups = groups_of(line)
        if any(len(g.words) > MAX_LABEL_WORDS for g in groups):
            continue
        if not accepted:
            if not any(_overlap(g.x0, g.x1, c0, c1) for g in groups for c0, c1 in cells):
                continue
        elif accepted[-1].page != line.page or accepted[-1].top - line.bottom > LINE_GAP * line.height:
            break
        accepted.append(line)
        if len(accepted) >= MAX_HEADER_LINES:
            break
    return accepted


@dataclass(frozen=True)
class Column:
    x0: float
    x1: float
    label: str
    ambiguous: bool = False


def columns_of(lines: Sequence[Line]) -> list[Column]:
    """One label per column from the stacked header lines (nearest first): the groups of the lines are clustered by horizontal overlap; a
    group that lies over two or more groups of another line is a label of all of them (a spanning label) and goes first in each. The label of
    a column is its spanning labels, then its own words from the top line to the bottom, a group's words in their stored order. A column that
    holds two groups of one line is ambiguous."""
    groups = [g for line in lines for g in groups_of(line)]

    def spans_two(g: Group) -> bool:
        for line in lines:
            if line is g.line:
                continue
            if sum(1 for o in groups_of(line) if _overlap(g.x0, g.x1, o.x0, o.x1)) >= 2:
                return True
        return False

    spanning = [g for g in groups if spans_two(g)]
    base = [g for g in groups if g not in spanning]
    parent = list(range(len(base)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(base):
        for j in range(i + 1, len(base)):
            b = base[j]
            if _overlap(a.x0, a.x1, b.x0, b.x1):
                parent[find(j)] = find(i)
    clusters: dict[int, list[Group]] = {}
    for i, g in enumerate(base):
        clusters.setdefault(find(i), []).append(g)
    out: list[Column] = []
    for members in clusters.values():
        x0, x1 = min(g.x0 for g in members), max(g.x1 for g in members)
        above = sorted((g for g in spanning if _overlap(g.x0, g.x1, x0, x1)), key=lambda g: g.line.top)
        own = sorted(members, key=lambda g: (g.line.top, g.x0))
        ambiguous = len({id(g.line) for g in members}) < len(members)
        label = " ".join(g.label for g in [*above, *own])
        out.append(Column(x0, x1, label, ambiguous))
    return sorted(out, key=lambda c: c.x0)


def column_of(cell: tuple[float, float], columns: Sequence[Column]) -> Column | None:
    """The one column label a number is under; None when it is under none or under two (or under an ambiguous one)."""
    under = [c for c in columns if _overlap(c.x0, c.x1, *cell)]
    return under[0] if len(under) == 1 and not under[0].ambiguous else None


# --- the binding --------------------------------------------------------------------------------------------------------------------

BOUND, UNKNOWN, NOT_ROW = "bound", "unknown", "not_row"


@dataclass(frozen=True)
class Part:
    """A piece of the served text that is a verbatim copy of stored text: ``served[s0:s1] == stored[t0:t1]``."""

    s0: int
    s1: int
    t0: int
    t1: int


@dataclass(frozen=True)
class Binding:
    status: str
    reason: str = ""
    served: str = ""
    parts: tuple[Part, ...] = ()
    labels: tuple[str, ...] = field(default=())  # the label of each cell, in drawn order


NOT_A_ROW = Binding(NOT_ROW)


def bind(text: str, spans: Sequence | None, attempts: Sequence[Sequence[Line]]) -> Binding:
    """The binding of one row block. ``attempts``: the lines above the row, nearest first, of the row's page and then of each previous page a
    continuation may take its header from; the first whose header labels every number of the row wins."""
    shape = row_shape(text)
    if shape is None:
        return NOT_A_ROW
    words = words_of(text, spans)
    if words is None:
        return Binding(UNKNOWN, "no word positions")
    by_start = {w.start: w for w in words}
    if len(words) != len(shape.tokens) or any(by_start.get(a) is None or by_start[a].end != b for a, b in shape.tokens):
        return Binding(UNKNOWN, "words without positions")
    boxes = []
    for cell in shape.cells:
        ws = [by_start[shape.tokens[i][0]] for i in cell]
        boxes.append((min(w.x0 for w in ws), max(w.x1 for w in ws)))
    if len({w.line for w in words}) != 1:
        return Binding(UNKNOWN, "several lines")
    if not attempts or not any(attempts):
        return Binding(UNKNOWN, "no header")
    reason = "no header"
    for lines in attempts:
        header = pick_header(lines, boxes)
        if not header:
            continue
        columns = columns_of(header)
        labelled = [column_of(box, columns) for box in boxes]
        if any(c is None or not c.label for c in labelled):
            reason = "a number under no label or two"
            continue
        if len({id(c) for c in labelled}) != len(labelled):
            reason = "two numbers under one label"
            continue
        return _serve(text, shape, boxes, [c.label for c in labelled])  # type: ignore[union-attr]
    return Binding(UNKNOWN, reason)


def _serve(text: str, shape: Shape, boxes: list[tuple[float, float]], labels: list[str]) -> Binding:
    parts: list[Part] = []
    out = ""
    prefix = [shape.tokens[i] for i in shape.prefix]
    for k, (a, b) in enumerate(prefix):
        out += " " if k else ""
        parts.append(Part(len(out), len(out) + b - a, a, b))
        out += text[a:b]
    if prefix:
        out += " — "
    order = sorted(range(len(shape.cells)), key=lambda i: (boxes[i][0], i))
    for n, i in enumerate(order):
        out += "; " if n else ""
        out += f"{labels[i]}: "
        a, b = shape.tokens[shape.cells[i][0]][0], shape.tokens[shape.cells[i][-1]][1]
        parts.append(Part(len(out), len(out) + b - a, a, b))
        out += text[a:b]
    return Binding(BOUND, "", out, tuple(parts), tuple(labels[i] for i in order))


def stored_segments(parts: Sequence[Part], start: int, end: int) -> list[tuple[int, int]]:
    """The stored ranges of the verbatim pieces of the served text that lie in ``[start, end)`` of it (served words that are no copy of the
    stored text, the column labels, are no place of the stored block); adjacent ranges are joined."""
    out: list[tuple[int, int]] = []
    for p in parts:
        lo, hi = max(start, p.s0), min(end, p.s1)
        if lo >= hi:
            continue
        a, b = p.t0 + (lo - p.s0), p.t0 + (hi - p.s0)
        if out and out[-1][1] == a:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out
