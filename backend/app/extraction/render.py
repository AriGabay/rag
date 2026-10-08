"""A page of a stored PDF, or a region of it, as a PNG: for the source panel's page and region views and for the
model's visual inspection (``app.chat.tools.inspect``).

Rendering is at a fixed scale with the long side capped, so no page or region size makes an image unbounded.
pdfium is not thread-safe and the API serves sync endpoints and chat turns on thread pools: one render at a time
per process, under ``RENDER_LOCK``.
"""

from __future__ import annotations

import io
import threading

PAGE_SCALE = 1.5  # 108 dpi: a whole page to look at
REGION_SCALE = 3.0  # 216 dpi: a region's cells and numbers stay legible
RENDER_MAX_SIDE = 2000  # pixels on the long side, whatever the size of the page or region
REGION_MARGIN = 4.0  # points shown around a region
RENDER_LOCK = threading.Lock()


class RenderError(Exception):
    """The file has no such page, or the region is empty: nothing to show."""


def render_png(data: bytes, page_no: int, bbox: list[float] | None, *, scale: float | None = None,
               max_side: int = RENDER_MAX_SIDE) -> bytes:
    """One page of a PDF (``bbox`` None) or a region of it (x0, top, x1, bottom in points from the page's top-left
    corner, with a small margin), rendered at ``scale`` (by default ``PAGE_SCALE`` for a page, ``REGION_SCALE``
    for a region) with the long side capped at ``max_side`` pixels."""
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
                image = page.render(scale=min(wanted, max_side / max(long_side, 1.0)), crop=crop).to_pil()
            finally:
                page.close()
        finally:
            doc.close()
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
