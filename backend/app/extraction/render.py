"""A page of a stored PDF, or a region of it, as a PNG: for the source viewer's page and region images and for the
model's visual inspection (``app.chat.tools.inspect``).

Rendering is at a given scale with the long side capped, so no page or region size makes an image unbounded. The
source viewer shows a page at one of two tiers (``PAGE_SCALES``: the normal view and a zoom); each tier's cap
(``page_max_side``) is large enough for a whole page up to ``FULL_PAGE_POINTS`` on its long side to reach the tier's
nominal scale, so zooming really magnifies an ordinary page, and only a larger sheet is scaled down to the cap.
A rendered page is the page's display frame (its CropBox within its MediaBox, turned by ``/Rotate``): the frame the
stored word spans and cell boxes are in (``app.extraction.geometry``), so ``RenderedPage.width``/``height`` are the
size an overlay's fractions are taken of.

pdfium is not thread-safe and the API serves sync endpoints and chat turns on thread pools: one render at a time
per process, under ``RENDER_LOCK``.
"""

from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass

PAGE_SCALE = 1.5  # 108 dpi: a whole page to look at
ZOOM_SCALE = 3.0  # 216 dpi: a page's small print and table cells read without blurring
PAGE_SCALES = {"normal": PAGE_SCALE, "zoom": ZOOM_SCALE}  # the source viewer's two tiers
REGION_SCALE = 3.0  # 216 dpi: a region's cells and numbers stay legible
RENDER_MAX_SIDE = 2000  # pixels on the long side, whatever the size of the page or region
FULL_PAGE_POINTS = 1008.0  # the long side of a US Legal sheet (A4 842, Letter 792): a whole page reaches any tier
REGION_MARGIN = 4.0  # points shown around a region
RENDER_LOCK = threading.Lock()


class RenderError(Exception):
    """The file has no such page, or the region is empty: nothing to show."""


@dataclass(frozen=True)
class RenderedPage:
    png: bytes
    width: float  # the page's display frame in points (what an overlay's fractions are of)
    height: float
    scale: float  # the scale applied: the tier's, or less when the cap was reached


def page_max_side(scale: float) -> int:
    """The long-side cap for a page shown at ``scale``: a page of ``FULL_PAGE_POINTS`` reaches the scale, and the cap
    is never below ``RENDER_MAX_SIDE``."""
    return max(RENDER_MAX_SIDE, math.ceil(scale * FULL_PAGE_POINTS))


def applied_scale(long_side: float, scale: float, max_side: int) -> float:
    """``scale``, or less when the long side would exceed ``max_side`` pixels."""
    return min(scale, max_side / max(long_side, 1.0))


def _render(data: bytes, page_no: int, bbox: list[float] | None, scale: float | None, max_side: int):
    """The image, the page's display size in points and the scale applied."""
    import pypdfium2 as pdfium

    with RENDER_LOCK:
        try:
            doc = pdfium.PdfDocument(data)
        except Exception:  # noqa: BLE001 - a file pdfium cannot open has no page to show
            raise RenderError("unreadable file") from None
        try:
            if not 1 <= page_no <= len(doc):
                raise RenderError("no such page")
            page = doc[page_no - 1]
            try:
                width, height = page.get_size()
                if bbox is None:
                    crop, wanted, long_side = (0, 0, 0, 0), scale or PAGE_SCALE, max(width, height)
                else:
                    x0, top = max(0.0, bbox[0] - REGION_MARGIN), max(0.0, bbox[1] - REGION_MARGIN)
                    x1, bottom = min(width, bbox[2] + REGION_MARGIN), min(height, bbox[3] + REGION_MARGIN)
                    if x1 - x0 < 1 or bottom - top < 1:
                        raise RenderError("empty region")
                    # pdfium crops by the amount removed from each side: left, bottom, right, top
                    crop = (x0, height - bottom, width - x1, top)
                    wanted, long_side = scale or REGION_SCALE, max(x1 - x0, bottom - top)
                applied = applied_scale(long_side, wanted, max_side)
                try:
                    image = page.render(scale=applied, crop=crop).to_pil()
                except Exception:  # noqa: BLE001 - a page pdfium cannot draw has nothing to show
                    raise RenderError("render failed") from None
            finally:
                page.close()
        finally:
            doc.close()
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue(), float(width), float(height), applied


def render_png(data: bytes, page_no: int, bbox: list[float] | None, *, scale: float | None = None,
               max_side: int = RENDER_MAX_SIDE) -> bytes:
    """One page of a PDF (``bbox`` None) or a region of it (x0, top, x1, bottom in points in the page's display
    frame, from its top-left corner, with a small margin), rendered at ``scale`` (by default ``PAGE_SCALE`` for a
    page, ``REGION_SCALE`` for a region) with the long side capped at ``max_side`` pixels."""
    return _render(data, page_no, bbox, scale, max_side)[0]


def render_page(data: bytes, page_no: int, *, scale: float, max_side: int) -> RenderedPage:
    """One whole page at ``scale`` (capped at ``max_side`` pixels), with its display size and the scale applied."""
    png, width, height, applied = _render(data, page_no, None, scale, max_side)
    return RenderedPage(png, width, height, applied)
