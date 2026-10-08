"""Content a PDF page's text layer does not cover (KTD4, KTD5): found, deduplicated by content, and read.

Candidate regions on a page whose text layer passed come from two sources:

- image objects, with their region, a hash over their raw stream bytes (the same picture on fifty pages has one
  hash) and their effective resolution (their pixels over their printed size);
- ink drawn by the page's own vector content outside the text layer: the page is rendered with its text and image
  objects removed, the boxes of the text-layer characters are masked, long thin runs (rules, borders, underlines
  and solid fills) are erased, and what is left is grouped into regions. This finds text drawn as outlines and
  labelled drawings; a ruled table whose text is in the text layer leaves nothing.

A region with text-layer characters inside it is *covered* when OCR of the region agrees with those characters (a
searchable scan: a page image under an invisible text layer); without OCR, a region densely overlaid by text is
taken as covered. A covered region becomes no block of its own.

Everything else is read once per content (``images.read_raster`` with ``RegionHints``): OCR first, the vision
model on a crop at legible scale for tables, low-resolution pictures and uncertain OCR, and a photograph is
recorded without text. Within a document the first reading of a hash applies to every occurrence; across the
office's documents a ``ReadingCache`` (the office-scoped ``image_readings`` table) returns earlier readings by
content hash, reader version, model configuration and crop scale. Only successful and uncertain readings are
cached. The vision prompt carries no page context, so a reading depends on the content alone.

A picture repeated in a document is shown once (``mark_repeated``): content occurring ``REPEAT_MIN`` times or more
(a logo on every page, watermark tiles, a header stamp) is page furniture and becomes one block, at its first
occurrence the text layer does not cover, with its reading (repeated is not decorative: a logo with text keeps that
text). Two occurrences on two pages are kept (a table or a signature repeated in two sections); two on one page are
one block.

pdfium is not thread-safe: every render and image decode happens on the calling thread; the OCR and model calls
run in a small worker pool. A vision failure that is transient or a configuration error raises
``images.VisionUnavailable`` out of the pool and fails the job; the version keeps its earlier reading.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image, ImageChops, ImageDraw

from app.config import Settings
from app.extraction.base import check_deadline
from app.extraction.images import (
    OCR_MIN_CONFIDENCE,
    VISION_MAX_SIDE,
    PictureReading,
    RegionHints,
    VisionReader,
    flatten,
    long_runs,
    ocr_words,
    read_gray,
)

log = logging.getLogger(__name__)

# The reader of regions: a cached reading made by an older one is not reused.
REGION_READER_VERSION = "regions-v1"

INK_DPI = 100  # resolution of the paths-only render that finds ink outside the text layer
INK_THRESHOLD = 160
RULE_PT = 8.0  # a straight run of ink at least this long (about twice this to be sure) is a rule, border or fill
CELL_PT = 4.0  # ink is grouped on a grid of cells this size; a gap of one empty cell still joins
MIN_REGION_PT = 6.0  # an ink region smaller than this in either direction is a mark (a bullet), not content
MIN_INK_PX = 40
FILL_DENSITY = 0.9  # a region this solid is a filled shape
CHAR_PAD_PT = 1.0
REGION_PAD_PT = 2.0
READ_DPI = 300  # vector regions are rendered at this resolution for reading (the long side capped for the model)
LARGE_SHARE = 0.08  # a region covering this share of its page is large
COVER_MIN_CHARS = 20  # fewer text-layer characters inside a region than this never cover it
COVER_MIN_WORDS = 5
COVER_AGREEMENT = 0.5  # share of OCR's confident words the text layer must also hold
COVER_DENSITY = 0.04  # without OCR: share of the region the characters' boxes must fill ...
COVER_SPAN = 0.4  # ... with the characters spread over this share of it (not one caption line over a picture)
REPEAT_MIN = 3  # a content occurring this often in a document (on any pages) is page furniture
HASH_SHOWN = 12  # characters of a content hash shown in the ingestion report
REGION_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
IMAGE_CROP = f"native<={VISION_MAX_SIDE}"
NOTE_NO_PIXELS = "לא ניתן היה לחלץ את התמונה מהקובץ"


@dataclass
class Region:
    """A picture or a group of ink on one page. ``bbox``: x0, top, x1, bottom in points from the top-left corner.
    ``kind``: ``image`` (an image object) or ``ink`` (vector content outside the text layer). After reading,
    ``covered`` says the text layer already holds its content and ``reading`` what was read from it;
    ``furniture`` says the same content repeats through the document (``mark_repeated``) and ``duplicate`` that
    another occurrence of it already stands for it (it becomes no block)."""

    page: int
    bbox: list[float]
    kind: str = "image"
    content_hash: str | None = None
    srcsize: tuple[int, int] | None = None
    reading: PictureReading | None = None
    covered: bool = False
    furniture: bool = False
    duplicate: bool = False
    # filled before reading: what the text layer holds inside the region
    layer_text: str = ""
    layer_chars: int = 0
    layer_density: float = 0.0
    layer_span: float = 0.0


@dataclass
class PageLayer:
    """What one text-layer page holds, for finding and checking its regions."""

    index: int  # 0-based page index
    width: float
    height: float
    chars: list[tuple[float, float, float, float]]
    lines: list[tuple[list[float], str]]  # each text-layer line's box and logical text
    regions: list[Region] = field(default_factory=list)


@dataclass(frozen=True)
class ReadingKey:
    content_hash: str
    reader_version: str
    model_config: str
    crop_scale: str


class ReadingCache(Protocol):
    """Readings of earlier documents of the same office, by content (``image_readings``)."""

    def get(self, key: ReadingKey) -> PictureReading | None: ...

    def put(self, key: ReadingKey, reading: PictureReading) -> None: ...


CACHEABLE = ("read", "read_uncertain", "no_text")


def model_config(vision: VisionReader | None) -> str:
    """The reader configuration a reading depends on: the vision model and effort, or ``none`` (OCR only)."""
    if vision is None:
        return "none"
    return getattr(vision, "config", None) or type(vision).__name__


# --- ink outside the text layer --------------------------------------------------------------------------------

def _components(ink: Image.Image, cell: int) -> list[tuple[int, int, int, int, float]]:
    """Groups of ink cells (a gap of one empty cell joins): pixel box and ink pixel count of each."""
    w, h = ink.size
    gw, gh = math.ceil(w / cell), math.ceil(h / cell)
    padded = Image.new("L", (gw * cell, gh * cell), 0)
    padded.paste(ink, (0, 0))
    small = padded.resize((gw, gh), Image.Resampling.BOX)
    values = small.tobytes()
    occupied = {i for i, v in enumerate(values) if v >= 8}
    seen: set[int] = set()
    out = []
    for start in occupied:
        if start in seen:
            continue
        seen.add(start)
        queue = deque([start])
        xs, ys, ink_px = [], [], 0.0
        while queue:
            i = queue.popleft()
            y, x = divmod(i, gw)
            xs.append(x)
            ys.append(y)
            ink_px += values[i] / 255 * cell * cell
            for dy in (-2, -1, 0, 1, 2):
                for dx in (-2, -1, 0, 1, 2):
                    ny, nx = y + dy, x + dx
                    j = ny * gw + nx
                    if 0 <= ny < gh and 0 <= nx < gw and j in occupied and j not in seen:
                        seen.add(j)
                        queue.append(j)
        out.append((min(xs) * cell, min(ys) * cell, min(w, (max(xs) + 1) * cell), min(h, (max(ys) + 1) * cell),
                    ink_px))
    return out


def _merge_lines(boxes: list[list[float]]) -> list[list[float]]:
    """Join regions stacked like lines of one block: overlapping horizontally, the gap below one line's height."""
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                overlap = min(a[2], b[2]) - max(a[0], b[0])
                gap = max(a[1], b[1]) - min(a[3], b[3])
                height = max(a[3] - a[1], b[3] - b[1])
                if overlap > 0.5 * min(a[2] - a[0], b[2] - b[0]) and gap <= height:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def ink_regions(paths_page, layer: PageLayer, pictures: list[list[float]]) -> list[list[float]]:
    """Regions of ink on a page rendered from its vector content alone (the caller removed its text and image
    objects), outside the text layer's character boxes and the pictures, without rules, borders and fills."""
    scale = INK_DPI / 72
    gray = paths_page.render(scale=scale, grayscale=True).to_pil().convert("L")
    ink = gray.point(lambda p: 255 if p < INK_THRESHOLD else 0)
    draw = ImageDraw.Draw(ink)
    pad = CHAR_PAD_PT
    for x0, top, x1, bottom in [*layer.chars, *[tuple(b) for b in pictures]]:
        draw.rectangle(((x0 - pad) * scale, (top - pad) * scale, (x1 + pad) * scale, (bottom + pad) * scale), fill=0)
    k = max(2, round(RULE_PT * scale))
    ink = ImageChops.subtract(ink, long_runs(ink, k, k))
    boxes = []
    for px0, py0, px1, py1, ink_px in _components(ink, max(2, round(CELL_PT * scale))):
        x0, top, x1, bottom = px0 / scale, py0 / scale, px1 / scale, py1 / scale
        area_px = max(1, (px1 - px0) * (py1 - py0))
        if x1 - x0 < MIN_REGION_PT or bottom - top < MIN_REGION_PT or ink_px < MIN_INK_PX:
            continue
        if ink_px / area_px >= FILL_DENSITY:
            continue
        boxes.append([x0, top, x1, bottom])
    out = []
    for x0, top, x1, bottom in _merge_lines(boxes):
        out.append([round(max(0.0, x0 - REGION_PAD_PT), 1), round(max(0.0, top - REGION_PAD_PT), 1),
                    round(min(layer.width, x1 + REGION_PAD_PT), 1), round(min(layer.height, bottom + REGION_PAD_PT), 1)])
    return out


def find_ink_regions(data: bytes, layers: list[PageLayer]) -> None:
    """Add each page's ink regions to ``layer.regions``. Uses its own copy of the document, whose pages lose their
    text and image objects. Rotated pages and pages whose crop box differs from their media box are skipped (their
    text-layer coordinates do not match the render)."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    if not layers:
        return
    doc = pdfium.PdfDocument(data)
    try:
        for layer in layers:
            page = doc[layer.index]
            try:
                if page.get_rotation() or tuple(page.get_cropbox()) != tuple(page.get_mediabox()):
                    continue
                removed = list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT, pdfium_c.FPDF_PAGEOBJ_IMAGE],
                                                max_depth=1))
                for obj in removed:
                    page.remove_obj(obj)
                    obj.close()
                pictures = [r.bbox for r in layer.regions if r.kind == "image"]
                for bbox in ink_regions(page, layer, pictures):
                    layer.regions.append(Region(page=layer.index + 1, bbox=bbox, kind="ink"))
            except Exception:  # noqa: BLE001 - a page that cannot be analysed keeps its text-layer reading
                log.warning("ink analysis failed on page %s", layer.index + 1, exc_info=True)
            finally:
                page.close()
    finally:
        doc.close()


# --- what the text layer holds inside a region -----------------------------------------------------------------

def _inside(box: tuple[float, float, float, float] | list[float], region: list[float]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def _fill_layer(layer: PageLayer) -> None:
    for r in layer.regions:
        chars = [c for c in layer.chars if _inside(c, r.bbox)]
        area = max(1.0, (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1]))
        r.layer_chars = len(chars)
        r.layer_density = sum((c[2] - c[0]) * (c[3] - c[1]) for c in chars) / area
        if chars:
            r.layer_span = ((max(c[2] for c in chars) - min(c[0] for c in chars))
                            * (max(c[3] for c in chars) - min(c[1] for c in chars))) / area
        r.layer_text = "\n".join(t for box, t in layer.lines if box and _inside(box, r.bbox))


_TOKEN = re.compile(r"[א-תA-Za-z0-9][א-תA-Za-z0-9,.%/\-״׳\"']*[א-תA-Za-z0-9%]|"
                    r"[א-תA-Za-z0-9]")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text) if len(t) >= 2}


def is_covered(region: Region, words: list[dict] | None, ocr_on: bool) -> bool:
    """Whether the text layer inside ``region`` already holds what the region shows."""
    if region.layer_chars < COVER_MIN_CHARS:
        return False
    if not ocr_on:
        return region.layer_density >= COVER_DENSITY and region.layer_span >= COVER_SPAN
    if not words:
        return False
    seen = {t for w in words if w["conf"] >= OCR_MIN_CONFIDENCE for t in _tokens(w["text"])}
    if len(seen) < COVER_MIN_WORDS:
        return False
    layer = _tokens(region.layer_text)
    return sum(1 for t in seen if t in layer) / len(seen) >= COVER_AGREEMENT


# --- pixels ----------------------------------------------------------------------------------------------------

def _image_pixels(doc, layers: list[PageLayer], wanted: set[str]) -> dict[str, Image.Image]:
    """Grayscale pixels of each wanted image hash, decoded once from the first page that holds it."""
    import pypdfium2.raw as pdfium_c

    found: dict[str, Image.Image] = {}
    for layer in layers:
        need = {r.content_hash for r in layer.regions if r.kind == "image"} & (wanted - found.keys())
        if not need:
            continue
        page = doc[layer.index]
        objects = list(page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]))
        try:
            for obj in objects:
                try:
                    digest = hashlib.sha256(bytes(obj.get_data(decode_simple=False))).hexdigest()
                    if digest not in need or digest in found:
                        continue
                    native = tuple(obj.get_px_size())
                    try:  # with its mask applied (transparent parts white, not black)
                        img = obj.get_bitmap(render=True).to_pil()
                        if img.size != native and all(abs(a - b) <= 2 for a, b in zip(img.size, native, strict=True)):
                            img = img.resize(native, Image.Resampling.LANCZOS)  # rounding of the image matrix
                    except Exception:  # noqa: BLE001 - without its mask the picture still reads
                        img = obj.get_bitmap(render=False).to_pil()
                    found[digest] = flatten(img)
                except Exception:  # noqa: BLE001 - one undecodable picture is reported unread, not fatal
                    log.warning("image decode failed on page %s", layer.index + 1)
        finally:
            for obj in objects:
                obj.close()
            page.close()
    return found


def _render_crop(doc, layer: PageLayer, bbox: list[float]) -> tuple[Image.Image, int]:
    """A region of the page rendered at reading resolution, the long side capped for the model."""
    long_side = max(bbox[2] - bbox[0], bbox[3] - bbox[1], 1.0)
    dpi = min(READ_DPI, int(VISION_MAX_SIDE * 72 / long_side))
    page = doc[layer.index]
    try:
        crop = (bbox[0], layer.height - bbox[3], layer.width - bbox[2], bbox[1])
        img = page.render(scale=dpi / 72, crop=crop, grayscale=True).to_pil().convert("L")
    finally:
        page.close()
    return img, dpi


# --- reading ---------------------------------------------------------------------------------------------------

@dataclass
class _Content:
    """One distinct content (an image hash or a rendered ink region) and every place it occurs."""

    content_hash: str
    gray: Image.Image | None
    crop_scale: str
    occurrences: list[Region] = field(default_factory=list)
    dpi: float | None = None
    large: bool = False


def _effective_dpi(r: Region) -> float | None:
    if not r.srcsize:
        return None
    w_pt, h_pt = r.bbox[2] - r.bbox[0], r.bbox[3] - r.bbox[1]
    if w_pt <= 0 or h_pt <= 0:
        return None
    return min(r.srcsize[0] / (w_pt / 72), r.srcsize[1] / (h_pt / 72))


def _contents(doc, layers: list[PageLayer]) -> list[_Content]:
    contents: dict[str, _Content] = {}
    wanted = {r.content_hash for layer in layers for r in layer.regions if r.kind == "image" and r.content_hash}
    pixels = _image_pixels(doc, layers, wanted)
    for layer in layers:
        page_area = max(1.0, layer.width * layer.height)
        for r in layer.regions:
            if r.kind == "ink":
                try:
                    gray, dpi = _render_crop(doc, layer, r.bbox)
                except Exception:  # noqa: BLE001
                    log.warning("region render failed on page %s", r.page)
                    r.reading = PictureReading("unread", "none", note=NOTE_NO_PIXELS)
                    continue
                r.content_hash = "ink:" + hashlib.sha256(f"{gray.size}".encode() + gray.tobytes()).hexdigest()
                c = contents.setdefault(r.content_hash, _Content(r.content_hash, gray, f"ink@{dpi}dpi"))
                c.dpi = float(dpi)
            else:
                gray = pixels.get(r.content_hash or "")
                if gray is None:
                    r.reading = PictureReading("unread", "none", note=NOTE_NO_PIXELS)
                    continue
                c = contents.setdefault(r.content_hash, _Content(r.content_hash, gray, IMAGE_CROP))
                dpi = _effective_dpi(r)
                if dpi is not None:
                    c.dpi = dpi if c.dpi is None else min(c.dpi, dpi)
            area = (r.bbox[2] - r.bbox[0]) * (r.bbox[3] - r.bbox[1])
            c.large = c.large or area / page_area >= LARGE_SHARE
            c.occurrences.append(r)
    return list(contents.values())


def _read_content(c: _Content, settings: Settings, vision: VisionReader | None, cache: ReadingCache | None,
                  deadline: float) -> tuple[dict[int, bool], PictureReading | None]:
    """Coverage of each occurrence and the content's reading (None when every occurrence is covered)."""
    from app.extraction.ocr import ocr_available

    check_deadline(deadline)
    languages = settings.ocr_languages
    key = ReadingKey(c.content_hash, REGION_READER_VERSION, model_config(vision), c.crop_scale)
    cached = None
    if cache is not None:
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001 - a cache that cannot be read only costs a fresh reading
            log.warning("reading cache lookup failed")
    ocr_on = ocr_available(languages)
    checks = [r for r in c.occurrences if r.layer_chars >= COVER_MIN_CHARS]
    words: list[dict] | None = None
    taken = False
    if ocr_on and (checks or cached is None):
        words, taken = ocr_words(c.gray, languages), True
    covered = {id(r): is_covered(r, words, ocr_on) for r in c.occurrences}
    if all(covered.values()):
        return covered, None
    if cached is not None:
        return covered, cached
    check_deadline(deadline)
    hints = RegionHints(dpi=c.dpi, large=c.large, words=words, words_taken=taken)
    reading = read_gray(c.gray, "", vision, languages, hints)
    if cache is not None and reading.cacheable and reading.status in CACHEABLE:
        try:
            cache.put(key, reading)
        except Exception:  # noqa: BLE001
            log.warning("reading cache store failed")
    return covered, reading


def read_regions(doc, data: bytes, layers: list[PageLayer], settings: Settings, vision: VisionReader | None,
                 cache: ReadingCache | None, deadline: float) -> None:
    """Find, check and read every region of ``layers`` (their ``regions`` hold the image objects already). Fills
    each region's ``covered`` and ``reading``."""
    check_deadline(deadline)
    find_ink_regions(data, layers)
    for layer in layers:
        _fill_layer(layer)
    contents = _contents(doc, layers)
    if not contents:
        return
    pool = ThreadPoolExecutor(max_workers=REGION_WORKERS)
    try:
        futures = [(c, pool.submit(_read_content, c, settings, vision, cache, deadline)) for c in contents]
        for c, future in futures:
            covered, reading = future.result()
            for r in c.occurrences:
                r.covered = covered[id(r)]
                r.reading = None if r.covered else reading
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


# --- repeated pictures -----------------------------------------------------------------------------------------

def mark_repeated(layers: list[PageLayer]) -> list[dict]:
    """Mark the regions of ``layers`` (read already) that repeat one content: page furniture (``REPEAT_MIN``
    occurrences or more, counting the ones the text layer covers) keeps a block at its first uncovered occurrence
    only, and two occurrences on one page are one block. Returns the ingestion report of the furniture shown:
    ``{"hash", "occurrences", "pages", "first_page", "status"}`` per content."""
    by_hash: dict[str, list[Region]] = {}
    for layer in layers:
        for r in sorted(layer.regions, key=lambda r: (r.bbox[1], r.bbox[0])):
            if r.content_hash:
                by_hash.setdefault(r.content_hash, []).append(r)
    report = []
    for digest, regions in by_hash.items():
        if len(regions) < 2:
            continue
        shown = [r for r in regions if not r.covered]
        pages = {r.page for r in regions}
        furniture = len(regions) >= REPEAT_MIN
        if not furniture and len(pages) > 1:
            continue  # twice, on two pages: plausibly content, each place keeps its block
        for r in shown[1:]:
            r.duplicate = True
        if not furniture:
            continue
        for r in regions:
            r.furniture = True
        if shown:
            first = shown[0]
            status = first.reading.status if first.reading is not None else "unread"
            prefix = "ink:" if digest.startswith("ink:") else ""
            report.append({"hash": prefix + digest.removeprefix(prefix)[:HASH_SHOWN], "occurrences": len(regions),
                           "pages": len(pages), "first_page": first.page, "status": status})
    return report
