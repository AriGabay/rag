"""Text and tables from EMF pictures (Windows Enhanced Metafiles).

Spreadsheet ranges pasted into a Word report as a picture are usually EMF: the cells are not a Word table
and no OCR is needed, because the metafile keeps every text run as Unicode (``EMR_EXTTEXTOUTW``) together
with its position, and draws the cell borders as lines (``MOVETOEX``/``LINETO``) or one-pixel pattern blits
(``BITBLT``). This module reads those records and rebuilds the table:

- runs: text, x extent (reference point, text alignment and the character advances), y extent (font height);
- cells: runs on one line separated by less than a gap become one cell. Hebrew runs are stored in logical
  order, but the runs of one cell are emitted in visual (left to right) order, so a cell with Hebrew is read
  right to left and its punctuation joins by the measured gaps;
- grid: vertical border positions split columns, horizontal ones split rows (a cell that wraps over two
  lines stays one cell). Without borders, columns come from the x extents of the runs and rows from lines;
- header: the leading rows that carry no number; rows under the grid are notes ("(*) ...").

A metafile without text runs but with a bitmap (``STRETCHDIBITS``) hands the bitmap to the caller for OCR.
Nothing here raises on a malformed record: parsing stops and returns what was read.
"""

from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass, field

EMR_HEADER = 1
EMR_SETWINDOWORGEX = 10
EMR_EOF = 14
EMR_SETTEXTALIGN = 22
EMR_SAVEDC = 33
EMR_RESTOREDC = 34
EMR_SELECTOBJECT = 37
EMR_MOVETOEX = 27
EMR_LINETO = 54
EMR_BITBLT = 76
EMR_STRETCHDIBITS = 81
EMR_EXTCREATEFONTINDIRECTW = 82
EMR_EXTTEXTOUTW = 84

TA_RIGHT = 2
TA_CENTER = 6
TA_BOTTOM = 8
TA_BASELINE = 24
ETO_PDY = 0x2000

_HEB = re.compile(r"[֐-׿]")
_DIGIT = re.compile(r"\d")
_LINE_MIN_LENGTH = 6  # shorter strokes are glyph parts or ticks, not cell borders
_MAX_RECORDS = 200_000


@dataclass
class Run:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str

    @property
    def height(self) -> float:
        return max(1.0, self.y1 - self.y0)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Metafile:
    runs: list[Run] = field(default_factory=list)
    vertical: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y0, y1)
    horizontal: list[tuple[float, float, float]] = field(default_factory=list)  # (y, x0, x1)
    bitmaps: list[bytes] = field(default_factory=list)  # BMP files from STRETCHDIBITS, largest first


@dataclass
class EmfTable:
    headers: list[str]
    rows: list[list[str]]
    notes: list[str]
    title: list[str]

    @property
    def text(self) -> str:
        return render_table_text(self)


@dataclass
class EmfContent:
    tables: list[EmfTable]
    lines: list[str]  # text outside any table, logical order, top to bottom
    bitmaps: list[bytes]

    @property
    def has_text(self) -> bool:
        return bool(self.lines) or any(t.rows or t.headers for t in self.tables)


# --- record parsing ----------------------------------------------------------------------------------

def _bitmap(data: bytes, off: int, size: int) -> bytes | None:
    """BMP file bytes of one STRETCHDIBITS record (header + bits), or None."""
    try:
        off_bmi, cb_bmi, off_bits, cb_bits = struct.unpack_from("<IIII", data, off + 48)
        bmi = data[off + off_bmi: off + off_bmi + cb_bmi]
        bits = data[off + off_bits: off + off_bits + cb_bits]
        if len(bmi) < 40 or not bits:
            return None
        file_header = b"BM" + struct.pack("<IHHI", 14 + len(bmi) + len(bits), 0, 0, 14 + len(bmi))
        return file_header + bmi + bits
    except struct.error:
        return None


def parse(data: bytes) -> Metafile:
    mf = Metafile()
    if len(data) < 88 or struct.unpack_from("<I", data, 0)[0] != EMR_HEADER:
        return mf
    fonts: dict[int, float] = {}
    font_h = 12.0
    align = 0
    org = (0, 0)
    stack: list[tuple[float, int, tuple[int, int]]] = []
    cur: tuple[int, int] | None = None
    off = 0
    for _ in range(_MAX_RECORDS):
        if off + 8 > len(data):
            break
        kind, size = struct.unpack_from("<II", data, off)
        if size < 8 or off + size > len(data):
            break
        try:
            if kind == EMR_SETWINDOWORGEX:
                org = struct.unpack_from("<ii", data, off + 8)
            elif kind == EMR_SETTEXTALIGN:
                align = struct.unpack_from("<I", data, off + 8)[0]
            elif kind == EMR_SAVEDC:
                stack.append((font_h, align, org))
            elif kind == EMR_RESTOREDC and stack:
                font_h, align, org = stack.pop()
            elif kind == EMR_EXTCREATEFONTINDIRECTW:
                handle, height = struct.unpack_from("<Ii", data, off + 8)
                fonts[handle] = float(abs(height)) or 12.0
            elif kind == EMR_SELECTOBJECT:
                handle = struct.unpack_from("<I", data, off + 8)[0]
                if handle in fonts:
                    font_h = fonts[handle]
            elif kind == EMR_MOVETOEX:
                cur = struct.unpack_from("<ii", data, off + 8)
            elif kind == EMR_LINETO:
                p = struct.unpack_from("<ii", data, off + 8)
                if cur is not None:
                    _add_line(mf, cur[0] - org[0], cur[1] - org[1], p[0] - org[0], p[1] - org[1])
                cur = p
            elif kind == EMR_BITBLT:
                x, y, cx, cy = struct.unpack_from("<iiii", data, off + 24)
                x, y = x - org[0], y - org[1]
                if abs(cx) <= 2 and abs(cy) >= _LINE_MIN_LENGTH:
                    _add_line(mf, x, y, x, y + cy)
                elif abs(cy) <= 2 and abs(cx) >= _LINE_MIN_LENGTH:
                    _add_line(mf, x, y, x + cx, y)
            elif kind == EMR_STRETCHDIBITS:
                bmp = _bitmap(data, off, size)
                if bmp:
                    mf.bitmaps.append(bmp)
            elif kind == EMR_EXTTEXTOUTW:
                run = _text_run(data, off, size, font_h, align, org)
                if run is not None:
                    mf.runs.append(run)
            elif kind == EMR_EOF:
                break
        except struct.error:
            break
        off += size
    mf.bitmaps.sort(key=len, reverse=True)
    return mf


def _add_line(mf: Metafile, x0: float, y0: float, x1: float, y1: float) -> None:
    if x0 == x1 and abs(y1 - y0) >= 1:
        mf.vertical.append((x0, min(y0, y1), max(y0, y1)))
    elif y0 == y1 and abs(x1 - x0) >= _LINE_MIN_LENGTH:
        mf.horizontal.append((y0, min(x0, x1), max(x0, x1)))


def _text_run(data: bytes, off: int, size: int, font_h: float, align: int, org: tuple[int, int]) -> Run | None:
    base = off + 8 + 16 + 12  # type/size, bounds, graphics mode, x/y scale
    x, y, n, off_string, options = struct.unpack_from("<iiIII", data, base)
    if n == 0 or n > 4096 or off_string + 2 * n > size:
        return None
    text = data[off + off_string: off + off_string + 2 * n].decode("utf-16le", "replace")
    text = text.replace("\x00", "")
    if not text.strip():
        return None
    off_dx = struct.unpack_from("<I", data, base + 36)[0]
    width = 0.0
    if off_dx and off_dx + 4 * n <= size:
        step = 2 if options & ETO_PDY else 1
        if off_dx + 4 * n * step <= size:
            advances = struct.unpack_from(f"<{n * step}i", data, off + off_dx)
            width = float(sum(advances[::step]))
    if width <= 0:
        width = 0.55 * font_h * len(text)
    x -= org[0]
    y -= org[1]
    horizontal = align & TA_CENTER
    if horizontal == TA_CENTER:
        x0 = x - width / 2
    elif horizontal == TA_RIGHT:
        x0 = x - width
    else:
        x0 = x
    vertical = align & TA_BASELINE
    if vertical == TA_BASELINE:
        y0 = y - 0.8 * font_h
    elif vertical == TA_BOTTOM:
        y0 = y - font_h
    else:
        y0 = y
    return Run(x0, x0 + width, y0, y0 + font_h, text)


# --- logical text of a group of runs -------------------------------------------------------------------

_NO_SPACE_BEFORE = set(",.:;)%]״\"'׳")
_NO_SPACE_AFTER = set("([\"'״׳")


def _join(parts: list[tuple[str, bool]]) -> str:
    """Join run texts in logical order; ``True`` marks a measured gap before the part. A run's own leading or
    trailing blank also separates it from its neighbour."""
    out = ""
    trailing = False
    for raw, gap in parts:
        piece = raw.strip()
        if not piece:
            trailing = trailing or bool(raw)
            continue
        spaced = gap or trailing or raw[:1].isspace()
        if out and spaced and piece[0] not in _NO_SPACE_BEFORE and out[-1] not in _NO_SPACE_AFTER:
            out += " "
        out += piece
        trailing = raw[-1:].isspace()
    return re.sub(r"\s{2,}", " ", out).strip()


_NUMERIC_RUN = re.compile(r"[\d₪$€%.,/+\-\s]*")


def _numeric(text: str) -> bool:
    """A run that is part of a number written left to right: digits with separators and a currency sign."""
    return bool(_NUMERIC_RUN.fullmatch(text)) and bool(re.search(r"[\d₪$€]", text))


def cell_text(runs: list[Run]) -> str:
    """Logical text of the runs of one cell. A cell with Hebrew is read right to left (runs by descending x);
    within it, a sequence of runs without Hebrew (a number and its currency sign) keeps its left-to-right
    order. A cell without Hebrew is read left to right."""
    if not runs:
        return ""
    gap_min = 0.2 * max(r.height for r in runs)
    if not any(_HEB.search(r.text) for r in runs):
        ordered = sorted(runs, key=lambda r: r.x0)
        return _join([(r.text, i > 0 and r.x0 - ordered[i - 1].x1 > gap_min) for i, r in enumerate(ordered)])
    rtl = sorted(runs, key=lambda r: -r.x1)
    groups: list[list[Run]] = []
    for r in rtl:
        if groups and _numeric(r.text) and _numeric(groups[-1][-1].text):
            groups[-1].append(r)  # neutral/number runs: collected, then emitted left to right
        else:
            groups.append([r])
    parts: list[tuple[str, bool]] = []
    prev: Run | None = None
    for g in groups:
        ltr = sorted(g, key=lambda r: r.x0)
        right_edge = max(r.x1 for r in g)
        gap = prev is not None and prev.x0 - right_edge > gap_min
        for i, r in enumerate(ltr):
            inner = i > 0 and r.x0 - ltr[i - 1].x1 > gap_min
            parts.append((r.text, gap if i == 0 else inner))
        prev = min(g, key=lambda r: r.x0)
    return _join(parts)


# --- table reconstruction --------------------------------------------------------------------------------

def _cluster(values: list[float], tolerance: float) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if out and v - out[-1] <= tolerance:
            continue
        out.append(v)
    return out


def _lines(runs: list[Run]) -> list[list[Run]]:
    """Runs grouped into text lines by vertical overlap, top to bottom."""
    lines: list[list[Run]] = []
    for r in sorted(runs, key=lambda r: (r.cy, -r.x1)):
        for line in lines:
            ref = line[0]
            if abs(ref.cy - r.cy) <= 0.45 * min(ref.height, r.height):
                line.append(r)
                break
        else:
            lines.append([r])
    lines.sort(key=lambda line: min(r.cy for r in line))
    return lines


def _segments(line: list[Run]) -> list[list[Run]]:
    """Runs of one line split where the gap is wider than a space (a column change)."""
    ordered = sorted(line, key=lambda r: r.x0)
    segs: list[list[Run]] = [[ordered[0]]]
    for r in ordered[1:]:
        h = max(r.height, segs[-1][-1].height)
        if r.x0 - max(x.x1 for x in segs[-1]) > 0.9 * h:
            segs.append([r])
        else:
            segs[-1].append(r)
    return segs


def _has_number(text: str) -> bool:
    return bool(_DIGIT.search(text))


def _borders(mf: Metafile) -> tuple[list[float], list[float], tuple[float, float, float, float] | None]:
    """Column borders (x, right to left), row borders (y, top to bottom) and the grid's box, from the lines that
    cross at least one text line; None box when the metafile has no usable grid."""
    if not mf.runs:
        return [], [], None
    h = sorted(r.height for r in mf.runs)[len(mf.runs) // 2]
    xs = _cluster([x for x, _, _ in mf.vertical], 0.3 * h)
    ys = _cluster([y for y, _, _ in mf.horizontal], 0.3 * h)
    if len(xs) < 2 and len(ys) < 2:
        return [], [], None
    x_lo = min([*xs, *[x0 for _, x0, _ in mf.horizontal]], default=min(r.x0 for r in mf.runs))
    x_hi = max([*xs, *[x1 for _, _, x1 in mf.horizontal]], default=max(r.x1 for r in mf.runs))
    y_lo = min(ys, default=min(r.y0 for r in mf.runs))
    y_hi = max(ys, default=max(r.y1 for r in mf.runs))
    return sorted(xs, reverse=True), ys, (x_lo, y_lo, x_hi, y_hi)


def _columns_from_runs(segments: list[list[Run]], h: float) -> list[tuple[float, float]]:
    """Column bands (x0, x1), right to left, from the union of segment extents across lines."""
    spans = sorted(((min(r.x0 for r in s), max(r.x1 for r in s)) for s in segments), key=lambda s: s[0])
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 <= bands[-1][1] - 0.2 * h:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    return [(b[0], b[1]) for b in sorted(bands, key=lambda b: -b[1])]


def _band_index(bands: list[tuple[float, float]], x0: float, x1: float) -> int:
    """The band that overlaps the extent most (a point extent: the band holding it, else the nearest)."""
    best, best_overlap = 0, float("-inf")
    for i, (b0, b1) in enumerate(bands):
        overlap = min(b1, x1) - max(b0, x0)
        if overlap > best_overlap:
            best, best_overlap = i, overlap
    return best


def _cell(runs: list[Run]) -> str:
    """A cell's text: its lines top to bottom (a wrapped cell), each read in its own direction."""
    return " ".join(cell_text(line) for line in _lines(runs)) if runs else ""


def _merge_spans(cells: list[list[Run]], bands: list[tuple[float, float]],
                 vertical: list[tuple[float, float, float]], row_runs: list[Run], tol: float) -> None:
    """Horizontally merged cells: where no border is drawn between two columns at this row's height, their runs
    are one cell, kept in the first (rightmost) column of the span."""
    y = sum(r.cy for r in row_runs) / len(row_runs)
    for i in range(len(bands) - 1, 0, -1):
        border = bands[i][1]  # the border between band i-1 (right) and band i (left)
        drawn = any(abs(x - border) <= tol and y0 - tol <= y <= y1 + tol for x, y0, y1 in vertical)
        if not drawn and cells[i]:
            cells[i - 1].extend(cells[i])
            cells[i] = []


def _split_lines(cells: list[list[Run]]) -> list[list[list[Run]]]:
    """A grid row whose cells hold several aligned text lines in two or more columns is several rows the
    author drew without borders between them; one wrapped cell alone stays one row."""
    lines = _lines([r for c in cells for r in c])
    if len(lines) < 2:
        return [cells]
    def line_of(r: Run) -> int:
        return next(i for i, line in enumerate(lines) if r in line)
    multi = sum(1 for c in cells if len({line_of(r) for r in c}) >= 2)
    if multi < 2:
        return [cells]
    rows: list[list[list[Run]]] = [[[] for _ in cells] for _ in lines]
    for j, c in enumerate(cells):
        for r in c:
            rows[line_of(r)][j].append(r)
    return rows


def _merged_regions(grid: list[list[list[Run]]], ys: list[float], bands: list[tuple[float, float]],
                    horizontal: list[tuple[float, float, float]], tol: float) -> dict[tuple[int, int], str]:
    """Vertically merged cells: a row border that does not cross a column joins the cells above and below it in
    that column. The joined text (its lines in order) is given to every row of the merged region, so each data
    row keeps the shared value (a block and parcel, an address). Returns {(grid row, column): text}."""
    out: dict[tuple[int, int], str] = {}
    for j, (b0, b1) in enumerate(bands):
        center = (b0 + b1) / 2
        start = 1  # grid row 0 lies above the first border
        while start < len(grid) - 1:
            end = start
            while end + 1 < len(grid) - 1 and not any(
                    abs(y - ys[end]) <= tol and x0 <= center <= x1 for y, x0, x1 in horizontal):
                end += 1
            if end > start:
                joined = _cell([r for k in range(start, end + 1) for r in grid[k][j]])
                for k in range(start, end + 1):
                    out[(k, j)] = joined
            start = end + 1
    return out


def build_tables(mf: Metafile) -> EmfContent:
    runs = mf.runs
    if not runs:
        return EmfContent([], [], mf.bitmaps)
    h = sorted(r.height for r in runs)[len(runs) // 2]
    xs, ys, box = _borders(mf)
    inside = runs
    outside: list[Run] = []
    if box is not None and len(ys) >= 2:
        x_lo, y_lo, x_hi, y_hi = box
        inside = [r for r in runs if y_lo - 0.3 * h <= r.cy <= y_hi + 0.3 * h]
        outside = [r for r in runs if r not in inside]

    if len(xs) >= 2:
        bands = [(xs[i + 1], xs[i]) for i in range(len(xs) - 1)]
        # text right of the first border or left of the last one is its own column
        if any(r.x0 >= xs[0] for r in inside):
            bands.insert(0, (xs[0], max(r.x1 for r in inside)))
        if any(r.x1 <= xs[-1] for r in inside):
            bands.append((min(r.x0 for r in inside), xs[-1]))
    else:
        bands = _columns_from_runs([s for line in _lines(inside) for s in _segments(line)], h)

    grid_rows: list[list[Run]]
    if len(ys) >= 2:
        grid_rows = [[] for _ in range(len(ys) + 1)]
        for r in inside:
            grid_rows[sum(1 for y in ys if y < r.cy)].append(r)
    else:
        grid_rows = _lines(inside)

    grid_cells: list[list[list[Run]]] = []
    for row_runs in grid_rows:
        cells: list[list[Run]] = [[] for _ in bands]
        if len(xs) >= 2:  # borders decide: each run goes to the column holding its center
            for r in row_runs:
                cx = (r.x0 + r.x1) / 2
                cells[_band_index(bands, cx, cx)].append(r)
            if len(ys) >= 2 and row_runs:
                _merge_spans(cells, bands, mf.vertical, row_runs, 0.3 * h)
        else:
            for seg in [s for line in _lines(row_runs) for s in _segments(line)]:
                x0, x1 = min(r.x0 for r in seg), max(r.x1 for r in seg)
                cells[_band_index(bands, x0, x1)].extend(seg)
        grid_cells.append(cells)

    merged = _merged_regions(grid_cells, ys, bands, mf.horizontal, 0.3 * h) if len(ys) >= 2 else {}
    texts_matrix: list[list[str]] = []
    for k, cells in enumerate(grid_cells):
        subrows = _split_lines(cells) if len(xs) >= 2 else [cells]
        for sub in subrows:
            texts_matrix.append([merged.get((k, j)) or _cell(c) for j, c in enumerate(sub)])
    table_rows = [t for t in texts_matrix if any(t)]

    # drop columns that are empty in every row
    keep = [i for i in range(len(bands)) if any(i < len(r) and r[i] for r in table_rows)]
    table_rows = [[r[i] for i in keep] for r in table_rows]

    title: list[str] = []
    while table_rows and sum(1 for c in table_rows[0] if c) == 1 and len(keep) > 2 \
            and not _has_number(" ".join(table_rows[0])):
        title.append(next(c for c in table_rows[0] if c))
        table_rows.pop(0)
    header_rows = 0
    for r in table_rows:
        if not _header_like(r):
            break
        header_rows += 1
    if header_rows == len(table_rows):
        header_rows = min(1, len(table_rows))
    headers = [" ".join(dict.fromkeys(r[i] for r in table_rows[:header_rows] if r[i])).strip()
               for i in range(len(keep))]
    rows = table_rows[header_rows:]

    above = [r for r in outside if box is not None and r.cy < box[1]]
    below = [r for r in outside if box is not None and r.cy > box[3]]
    title = [*_line_texts(above), *title]
    notes = _line_texts(below)
    if len(table_rows) <= 1 and not any(headers):
        return EmfContent([], _line_texts(runs), mf.bitmaps)
    table = EmfTable(headers=headers, rows=rows, notes=notes, title=title)
    return EmfContent([table], [], mf.bitmaps)


def _header_like(row: list[str]) -> bool:
    """A header row: its filled cells are words; at most a third of them carry a digit ("3%", a year)."""
    filled = [c for c in row if c]
    if not filled:
        return True
    numeric = sum(1 for c in filled if _has_number(c))
    return any(_HEB.search(c) for c in filled) and numeric * 3 <= len(filled)


def _line_texts(runs: list[Run]) -> list[str]:
    out = []
    for line in _lines(runs):
        segs = sorted(_segments(line), key=lambda s: -max(r.x1 for r in s))
        text = " ".join(cell_text(s) for s in segs).strip()
        if text:
            out.append(text)
    return out


def read_emf(data: bytes) -> EmfContent:
    try:
        return build_tables(parse(data))
    except Exception as exc:  # noqa: BLE001 - a broken picture must not fail the document (it is reported unread)
        import logging

        logging.getLogger(__name__).warning("emf reading failed: %s", type(exc).__name__)
        return EmfContent([], [], [])


def render_table_text(t: EmfTable) -> str:
    """A readable plain-text rendering: title, header row, data rows, notes (cells separated by " | ")."""
    out = [*t.title]
    if any(t.headers):
        out.append(" | ".join(t.headers))
    out.extend(" | ".join(c for c in r) for r in t.rows)
    out.extend(t.notes)
    return "\n".join(x for x in out if x.strip())


def bitmap_to_png(bmp: bytes) -> bytes | None:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(bmp))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return None
