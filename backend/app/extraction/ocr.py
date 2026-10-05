"""OCR for pages whose text layer fails the quality check (KTD11, R3): pypdfium2 rendering +
Tesseract ``heb+eng``.

Tables on OCR pages are rebuilt from Tesseract word boxes (``image_to_data``):

- ruled tables: horizontal and vertical rules are found with PIL projection profiles (no numpy), the
  rules are erased, the table region is OCR'd once, and each word goes to the grid cell holding its
  center. Header cells (and digit-free data cells Tesseract read as Latin junk) are re-read one by one
  with Hebrew-only OCR, and header text is snapped to the known header vocabulary.
- borderless tables: the header line (known Hebrew header vocabulary) gives the column positions;
  following lines are split into columns by the words' x positions.

Every table from an OCR page is marked ``ocr`` so its records go to review (U6). Page text comes from
the same word boxes in Tesseract's reading order (logical order).
"""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter

from app.extraction.chunking import is_heading
from app.extraction.hebrew import strip_bidi_controls
from app.extraction.tables import is_header_row, snap_header

_LATIN = re.compile(r"[A-Za-z]")
_HEB = re.compile(r"[א-ת]")
_DIGIT = re.compile(r"\d")
_INK_THRESHOLD = 160


@dataclass
class OcrLine:
    top: float
    text: str


@dataclass
class OcrTable:
    top: float
    rows: list[list[str]]  # logical column order, header row first when present


@dataclass
class OcrPage:
    lines: list[OcrLine]
    tables: list[OcrTable] = field(default_factory=list)
    angle: float = 0.0

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


@lru_cache(maxsize=8)
def ocr_available(languages: str) -> bool:
    if shutil.which("tesseract") is None:
        return False
    try:
        import pytesseract

        have = set(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001
        return False
    return all(lang in have for lang in languages.split("+"))


def render_page(pdf_doc, index: int, dpi: int, max_pixels: int) -> Image.Image:
    """Render one page to grayscale at ``dpi``, downscaled so width*height <= ``max_pixels``."""
    page = pdf_doc[index]
    try:
        w, h = page.get_size()
        scale = dpi / 72.0
        pixels = (w * scale) * (h * scale)
        if pixels > max_pixels:
            scale *= math.sqrt(max_pixels / pixels)
        return page.render(scale=scale).to_pil().convert("L")
    finally:
        page.close()


# --- image helpers ---------------------------------------------------------------------------------

def _ink(img: Image.Image, threshold: int = _INK_THRESHOLD) -> Image.Image:
    """Binary image where ink = 255 (so averages measure darkness)."""
    return img.point(lambda p: 255 if p < threshold else 0)


def _row_profile(ink: Image.Image) -> bytes:
    return ink.resize((1, ink.size[1]), Image.Resampling.BOX).tobytes()


def _col_profile(ink: Image.Image) -> bytes:
    return ink.resize((ink.size[0], 1), Image.Resampling.BOX).tobytes()


def deskew(img: Image.Image, max_angle: float = 1.5, step: float = 0.05) -> tuple[Image.Image, float]:
    """Small-angle deskew by maximizing the sharpness of the row profile on a downscaled copy."""
    w, h = img.size
    small = _ink(img.resize((max(1, w // 4), max(1, h // 4)), Image.Resampling.BILINEAR))
    best_score, best_angle = -1.0, 0.0
    n = int(round(max_angle / step))
    for k in range(-n, n + 1):
        angle = k * step
        prof = _row_profile(small.rotate(angle, resample=Image.Resampling.NEAREST, fillcolor=0))
        score = float(sum((prof[i + 1] - prof[i]) ** 2 for i in range(len(prof) - 1)))
        if score > best_score:
            best_score, best_angle = score, angle
    if abs(best_angle) < step / 2:
        return img, 0.0
    return img.rotate(best_angle, resample=Image.Resampling.BICUBIC, fillcolor=255), best_angle


def _runs(indices: list[int], max_gap: int = 2) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for v in indices:
        if out and v - out[-1][1] <= max_gap:
            out[-1][1] = v
        else:
            out.append([v, v])
    return [(a, b) for a, b in out]


@dataclass
class Grid:
    ys: list[tuple[int, int]]  # horizontal rules, top -> bottom
    xs: list[tuple[int, int]]  # vertical rules, left -> right

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.xs[0][0], self.ys[0][0], self.xs[-1][1], self.ys[-1][1]


def find_grids(ink: Image.Image) -> list[Grid]:
    """Ruled tables: long solid horizontal rules grouped by spacing, then vertical rules inside."""
    w, h = ink.size
    prof = _row_profile(ink)
    candidates = _runs([y for y, v in enumerate(prof) if v > 0.45 * 255])
    rules: list[tuple[int, int]] = []
    for a, b in candidates:
        if b - a > max(12, h // 250):
            continue  # a thick dark band (image, shading), not a rule
        solid = False
        for y in range(a, b + 1):
            strip = ink.crop((0, y, w, y + 1))
            box = strip.getbbox()
            if not box:
                continue
            seg = strip.crop((box[0], 0, box[2], 1)).tobytes()
            if box[2] - box[0] > 0.4 * w and sum(1 for c in seg if c) >= 0.9 * len(seg):
                solid = True
                break
        if solid:
            rules.append((a, b))
    groups: list[list[tuple[int, int]]] = []
    max_row = max(40, int(h * 0.08))
    for r in rules:
        if groups and r[0] - groups[-1][-1][1] <= max_row:
            groups[-1].append(r)
        else:
            groups.append([r])
    grids: list[Grid] = []
    for ys in groups:
        if len(ys) < 2:
            continue
        top, bottom = ys[0][1] + 1, ys[-1][0]
        if bottom - top < 10:
            continue
        cols = _col_profile(ink.crop((0, top, w, bottom)))
        xs = _runs([x for x, v in enumerate(cols) if v > 0.7 * 255])
        if len(xs) >= 3:
            grids.append(Grid(ys=ys, xs=xs))
    return grids


def _erase_rules(img: Image.Image, grids: list[Grid], pad: int = 3) -> Image.Image:
    out = img.copy()
    draw = ImageDraw.Draw(out)
    for g in grids:
        x0, y0, x1, y1 = g.bbox
        for a, b in g.ys:
            draw.rectangle((x0 - pad, a - pad, x1 + pad, b + pad), fill=255)
        for a, b in g.xs:
            draw.rectangle((a - pad, y0 - pad, b + pad, y1 + pad), fill=255)
    return out


def _clean_word(text: str) -> str:
    return strip_bidi_controls(text).strip()


def _words(img: Image.Image, languages: str, psm: int = 6) -> list[dict]:
    import pytesseract

    d = pytesseract.image_to_data(img, lang=languages, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    out = []
    for i, raw in enumerate(d["text"]):
        text = _clean_word(raw or "")
        if not text:
            continue
        out.append({
            "text": text, "left": d["left"][i], "top": d["top"][i], "width": d["width"][i],
            "height": d["height"][i], "conf": float(d["conf"][i]), "order": i,
            "line": (d["block_num"][i], d["par_num"][i], d["line_num"][i]),
        })
    return out


def _single_line(img: Image.Image, languages: str) -> str:
    import pytesseract

    pad = Image.new("L", (img.size[0] + 60, img.size[1] + 60), 255)
    pad.paste(img, (30, 30))
    return strip_bidi_controls(pytesseract.image_to_string(pad, lang=languages, config="--psm 7")).strip()


def _lines_from_words(words: list[dict]) -> list[OcrLine]:
    lines: dict[tuple, list[dict]] = {}
    for w in words:
        lines.setdefault(w["line"], []).append(w)
    out = []
    for ws in lines.values():
        ws.sort(key=lambda w: w["order"])  # Tesseract reading order = logical order
        out.append(OcrLine(top=min(w["top"] for w in ws), text=" ".join(w["text"] for w in ws)))
    out.sort(key=lambda line: line.top)
    return out


def _hebrew_only(languages: str) -> str | None:
    return "heb" if "heb" in languages.split("+") else None


def _better_hebrew(a: str, b: str) -> str:
    """Pick the reading with Hebrew and without Latin junk."""
    def score(s: str) -> tuple[int, int]:
        return (0 if _LATIN.search(s) or "?" in s else 1, len(_HEB.findall(s)))

    return b if score(b) > score(a) else a


def _grid_table(clean: Image.Image, grid: Grid, languages: str) -> OcrTable:
    x0, y0, x1, y1 = grid.bbox
    m = 10
    ox, oy = max(0, x0 - m), max(0, y0 - m)
    # Binarize + despeckle the region only: removes header shading and scan noise.
    box = (ox, oy, min(clean.size[0], x1 + m), min(clean.size[1], y1 + m))
    region_img = clean.crop(box).point(lambda p: 0 if p < 170 else 255)
    region_img = region_img.filter(ImageFilter.MedianFilter(3))
    words = _words(region_img, languages)
    nrows, ncols = len(grid.ys) - 1, len(grid.xs) - 1
    cells: list[list[list[dict]]] = [[[] for _ in range(ncols)] for _ in range(nrows)]
    for w in words:
        cx = w["left"] + w["width"] / 2 + ox
        cy = w["top"] + w["height"] / 2 + oy
        r = next((i for i in range(nrows) if grid.ys[i][1] <= cy <= grid.ys[i + 1][0]), None)
        c = next((j for j in range(ncols) if grid.xs[j][1] <= cx <= grid.xs[j + 1][0]), None)
        if r is not None and c is not None:
            cells[r][c].append(w)

    def cell_box(r: int, c: int) -> tuple[int, int, int, int]:
        return (grid.xs[c][1] + 4 - ox, grid.ys[r][1] + 4 - oy, grid.xs[c + 1][0] - 4 - ox,
                grid.ys[r + 1][0] - 4 - oy)

    heb = _hebrew_only(languages)
    rows: list[list[str]] = []
    for r in range(nrows):
        texts = []
        for c in range(ncols):
            ws = sorted(cells[r][c], key=lambda w: (w["line"], w["order"]))
            texts.append(" ".join(w["text"] for w in ws))
        header = r == 0 and _looks_like_header(texts)
        for c, t in enumerate(texts):
            needs_retry = header or (t and _LATIN.search(t) and not _DIGIT.search(t))
            bx0, by0, bx1, by1 = cell_box(r, c)
            if heb and needs_retry and bx1 - bx0 > 4 and by1 - by0 > 4:
                texts[c] = _better_hebrew(t, _single_line(region_img.crop(cell_box(r, c)), heb))
        if header:
            texts = [snap_header(t) for t in texts]
        rows.append(texts[::-1])  # columns right -> left = logical order
    return OcrTable(top=float(y0), rows=rows)


def _looks_like_header(cells: list[str]) -> bool:
    from app.extraction.tables import HEADER_VOCAB

    hits = sum(1 for c in cells if any(v in c for v in HEADER_VOCAB))
    return hits >= 2


def _borderless_tables(words: list[dict]) -> tuple[list[OcrTable], set[tuple]]:
    """Header line by vocabulary; columns from the header words' x-gaps; rows = following lines."""
    by_line: dict[tuple, list[dict]] = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    lines = sorted(by_line.items(), key=lambda kv: min(w["top"] for w in kv[1]))
    tables: list[OcrTable] = []
    used: set[tuple] = set()
    i = 0
    while i < len(lines):
        key, ws = lines[i]
        ws_sorted = sorted(ws, key=lambda w: w["left"])
        if not is_header_row([w["text"] for w in ws_sorted]):
            i += 1
            continue
        heights = sorted(w["height"] for w in ws_sorted)
        gap_min = 1.5 * heights[len(heights) // 2]
        cols: list[list[dict]] = [[ws_sorted[0]]]
        for w in ws_sorted[1:]:
            prev = cols[-1][-1]
            if w["left"] - (prev["left"] + prev["width"]) > gap_min:
                cols.append([w])
            else:
                cols[-1].append(w)
        if len(cols) < 2:
            i += 1
            continue
        spans = [(min(w["left"] for w in c), max(w["left"] + w["width"] for w in c)) for c in cols]
        bounds = [(spans[j][0] + spans[j - 1][1]) / 2 for j in range(1, len(spans))]

        def col_of(w: dict, bounds: list[float] = bounds) -> int:
            cx = w["left"] + w["width"] / 2
            return sum(1 for b in bounds if cx > b)

        header = [" ".join(w["text"] for w in sorted(c, key=lambda w: w["order"])) for c in cols]
        rows = [[snap_header(h) for h in header][::-1]]
        used.add(key)
        j = i + 1
        while j < len(lines) and any(_DIGIT.search(w["text"]) for w in lines[j][1]):
            line_text = " ".join(w["text"] for w in sorted(lines[j][1], key=lambda w: w["order"]))
            row = [[] for _ in cols]
            for w in lines[j][1]:
                row[col_of(w)].append(w)
            if is_heading(line_text) or sum(1 for c in row if c) < 2:
                break  # the next section starts; the table ended
            rows.append([" ".join(w["text"] for w in sorted(c, key=lambda w: w["order"])) for c in row][::-1])
            used.add(lines[j][0])
            j += 1
        if len(rows) > 1:
            tables.append(OcrTable(top=float(min(w["top"] for w in ws)), rows=rows))
        i = j
    return tables, used


def ocr_page_image(img: Image.Image, languages: str) -> OcrPage:
    """OCR a rendered page: page text lines (logical order) and tables rebuilt from word boxes."""
    gray, angle = deskew(img)
    grids = find_grids(_ink(gray))
    clean = _erase_rules(gray, grids)
    tables = [_grid_table(clean, g, languages) for g in grids]

    page_img = clean.copy()
    draw = ImageDraw.Draw(page_img)
    for g in grids:
        x0, y0, x1, y1 = g.bbox
        draw.rectangle((x0 - 5, y0 - 5, x1 + 5, y1 + 5), fill=255)
    words = _words(page_img, languages)
    if not grids:
        extra, used = _borderless_tables(words)
        tables.extend(extra)
        words = [w for w in words if w["line"] not in used]
    lines = _lines_from_words(words)
    # Table rows also go into the page text (searchable), at the table's position.
    for t in tables:
        for k, row in enumerate(t.rows):
            lines.append(OcrLine(top=t.top + k * 0.01, text=" ".join(c for c in row if c)))
    lines.sort(key=lambda line: line.top)
    return OcrPage(lines=lines, tables=sorted(tables, key=lambda t: t.top), angle=angle)
