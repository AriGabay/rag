"""Positions in the frame of the rendered page (KTD2, R3, R4).

A citation is shown over an image of the page that pypdfium2 renders: the page's CropBox (intersected with its
MediaBox), turned by its ``/Rotate``, with the origin at the top-left corner and y growing downwards, in points.
That is the *display frame*. Every stored position that a viewer draws (word spans, table cell boxes) is in it.

pdfplumber reports its coordinates already turned by ``/Rotate`` (pdfminer applies the rotation while it reads the
page), but measured from the rotated MediaBox in its own way, not from the CropBox. ``to_display`` converts a
pdfplumber box into the display frame: it removes the MediaBox origin pdfplumber adds, then subtracts the CropBox
offset computed from the raw ``/MediaBox``, ``/CropBox`` and ``/Rotate`` with the same matrix pdfminer uses. The
conversion is a translation only, so a union of boxes can be converted after it is taken. Stored
``document_blocks.bbox`` keeps its pdfplumber frame; whoever needs it on the rendered page converts it with the same
function and the page's stored geometry.

A page whose positions cannot be converted reliably (a rotation that is not a multiple of 90, a CropBox outside the
MediaBox, a rendered size that does not match the computed one, or boxes the two readers disagree on) records why in
``PageGeometry.issue``; its blocks then get no word spans and its tables no cell boxes, and a viewer falls back to
page precision.

A block's word spans index its final logical text (the text after the visual-to-logical reordering, font-map repair
and the line strip/join). ``visual_to_logical`` only reorders characters, so each logical character comes from one
character of the line as the text layer read it, and so from one glyph; ``logical_order`` recovers that
permutation. A span is ``[line, word, start, end, x0, top, x1, bottom]``: the line within the block, the word
within the line, the character range ``text[start:end]`` of the block text, and the word's box in the display
frame (``Span`` reads one).
"""

from __future__ import annotations

import re
from typing import NamedTuple

from app.extraction.base import PageGeometry
from app.extraction.hebrew import _LATIN, _LTR_TOKEN, _MIRROR

# Marker in a version's ingestion report: its reading stores positions under this scheme (KTD3). A version without
# it is one the geometry-only backfill still has to visit.
POSITIONS_VERSION = "v1"

ISSUE_ROTATION = "rotation_unsupported"
ISSUE_CROP = "cropbox_outside_mediabox"
ISSUE_BOX = "invalid_box"
ISSUE_SIZE = "display_size_mismatch"
ISSUE_FRAME = "text_layer_frame_differs"

SIZE_TOLERANCE = 0.5  # points: the rendered page size may differ from the computed one by rounding only
EDGE_TOLERANCE = 2.0  # points: a box may overhang the rendered page by this much before it is clipped
_ROTATIONS = (0, 90, 180, 270)
_WORD = re.compile(r"\S+")

Box = tuple[float, float, float, float]


class Span(NamedTuple):
    line: int
    word: int
    start: int
    end: int
    box: list[float]

    @classmethod
    def from_json(cls, row: list) -> Span:
        return cls(int(row[0]), int(row[1]), int(row[2]), int(row[3]), [float(v) for v in row[4:8]])


def normalize_box(box) -> Box | None:
    """``[x0, y0, x1, y1]`` with its corners sorted, or None when it is not four finite numbers."""
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x0, y0, x1, y1)):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def effective_crop(mediabox, cropbox) -> Box | None:
    """The rendered region: the CropBox intersected with the MediaBox (a missing CropBox is the MediaBox)."""
    m = normalize_box(mediabox)
    c = normalize_box(cropbox) if cropbox is not None else m
    if m is None or c is None:
        return None
    out = max(m[0], c[0]), max(m[1], c[1]), min(m[2], c[2]), min(m[3], c[3])
    return out if out[2] > out[0] and out[3] > out[1] else None


def _ctm_point(mediabox: Box, rotation: int, x: float, y: float) -> tuple[float, float]:
    """pdfminer's page matrix (``PDFPageInterpreter.process_page``) applied to a point of user space."""
    x0, y0, x1, y1 = mediabox
    if rotation == 90:
        return y - y0, x1 - x
    if rotation == 180:
        return x1 - x, y1 - y
    if rotation == 270:
        return y1 - y, x - x0
    return x - x0, y - y0


def display_size(mediabox, cropbox, rotation: int) -> tuple[float, float] | None:
    crop = effective_crop(mediabox, cropbox)
    if crop is None or rotation not in _ROTATIONS:
        return None
    w, h = crop[2] - crop[0], crop[3] - crop[1]
    return (h, w) if rotation in (90, 270) else (w, h)


def display_offset(mediabox, cropbox, rotation: int) -> tuple[float, float] | None:
    """What to subtract from a pdfplumber ``(x, top)`` to get the display frame, or None when the page cannot be
    converted.

    pdfplumber's ``x`` is pdfminer's rotated ``x'`` plus the rotated MediaBox's left edge, and its ``top`` is the
    rotated page height minus pdfminer's ``y'`` minus the rotated MediaBox's bottom edge (``pdfplumber.page``). The
    rendered page starts at the CropBox corner, which the same matrix places at ``(ox, oy)`` from the rotated
    MediaBox's top-left corner."""
    m = normalize_box(mediabox)
    crop = effective_crop(mediabox, cropbox)
    if m is None or crop is None or rotation not in _ROTATIONS:
        return None
    turned = rotation in (90, 270)
    origin_x, origin_y = (m[1], m[0]) if turned else (m[0], m[1])
    height = (m[2] - m[0]) if turned else (m[3] - m[1])
    corners = [_ctm_point(m, rotation, x, y) for x in (crop[0], crop[2]) for y in (crop[1], crop[3])]
    ox = min(px for px, _ in corners)
    oy = min(height - py for _, py in corners)
    return origin_x + ox, oy - origin_y


def to_display(box, mediabox, cropbox, rotation: int) -> list[float] | None:
    """A pdfplumber box ``[x0, top, x1, bottom]`` in the display frame, clipped to the rendered page; None when the
    page cannot be converted or the box lies outside the rendered page."""
    offset = display_offset(mediabox, cropbox, rotation)
    size = display_size(mediabox, cropbox, rotation)
    if offset is None or size is None or box is None:
        return None
    try:
        x0, top, x1, bottom = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    dx, dy = offset
    w, h = size
    x0, x1 = sorted((x0 - dx, x1 - dx))
    top, bottom = sorted((top - dy, bottom - dy))
    if x1 < -EDGE_TOLERANCE or top > h + EDGE_TOLERANCE or x0 > w + EDGE_TOLERANCE or bottom < -EDGE_TOLERANCE:
        return None
    out = [max(0.0, x0), max(0.0, top), min(w, x1), min(h, bottom)]
    if out[2] < out[0] or out[3] < out[1]:
        return None
    return [round(v, 1) for v in out]


def page_geometry(mediabox, cropbox, rotation, rendered_size: tuple[float, float] | None = None,
                  rendered_crop=None, rendered_rotation: int | None = None) -> PageGeometry:
    """The page's geometry record from the boxes and rotation the text layer was read with (pdfminer, which resolves
    inherited ``/MediaBox``, ``/CropBox`` and ``/Rotate``). ``rendered_*``: what the renderer reports for the same
    page (pypdfium2 ``get_size``, ``get_bbox``, ``get_rotation``); a disagreement means text-layer boxes would not land
    on the rendered page, so the page records an issue instead."""
    m = normalize_box(mediabox)
    c = normalize_box(cropbox) if cropbox is not None else m
    try:
        rotation = int(rotation) % 360
    except (TypeError, ValueError):
        rotation = None
    geom = PageGeometry(mediabox=[round(v, 2) for v in m] if m else None,
                        cropbox=[round(v, 2) for v in c] if c else None,
                        rotation=rotation, width=None, height=None)
    if m is None or c is None:
        geom.issue = ISSUE_BOX
        return geom
    if rotation not in _ROTATIONS:
        geom.issue = ISSUE_ROTATION
        return geom
    size = display_size(m, c, rotation)
    if size is None:
        geom.issue = ISSUE_CROP
        return geom
    geom.width, geom.height = round(size[0], 2), round(size[1], 2)
    if rendered_size is not None and (abs(rendered_size[0] - size[0]) > SIZE_TOLERANCE
                                      or abs(rendered_size[1] - size[1]) > SIZE_TOLERANCE):
        geom.issue = ISSUE_SIZE
        return geom
    crop = effective_crop(m, c)
    shown = normalize_box(rendered_crop) if rendered_crop is not None else crop
    if (rendered_rotation is not None and int(rendered_rotation) % 360 != rotation) or shown is None or any(
            abs(a - b) > SIZE_TOLERANCE for a, b in zip(shown, crop, strict=True)):
        geom.issue = ISSUE_FRAME
    return geom


def geometry_box(box, geom: PageGeometry | None) -> list[float] | None:
    """``to_display`` with a stored page geometry; None when the page records an issue."""
    if geom is None or geom.issue is not None or geom.mediabox is None or geom.rotation is None:
        return None
    return to_display(box, geom.mediabox, geom.cropbox, geom.rotation)


# --- word spans --------------------------------------------------------------------------------------------------

def _ltr_groups(s: str) -> list[tuple[int, int]]:
    """The left-to-right runs ``hebrew._restore_ltr_runs`` turns back, as (start, end) in ``s``."""
    groups: list[list] = []
    for m in _LTR_TOKEN.finditer(s):
        latin = bool(_LATIN.search(m.group(0)))
        gap = s[groups[-1][1]:m.start()] if groups else ""
        if groups and latin and groups[-1][2] and gap and set(gap) == {" "}:
            groups[-1][1] = m.end()
        else:
            groups.append([m.start(), m.end(), latin])
    return [(g[0], g[1]) for g in groups]


def logical_order(raw: str, text: str) -> list[int] | None:
    """For each character of ``text`` (a line after the orientation fix), the index of the character of ``raw``
    (the line as the text layer read it) it came from; None when ``text`` is neither ``raw`` nor its
    ``visual_to_logical`` form (the positions of such a line are not used)."""
    if len(raw) != len(text):
        return None
    if raw == text:
        return list(range(len(raw)))
    reversed_raw = raw[::-1]
    order = list(range(len(raw)))[::-1]
    for start, end in _ltr_groups(reversed_raw):
        order[start:end] = order[start:end][::-1]
    got = "".join(raw[k] for k in order)
    return order if got == text or got.translate(_MIRROR) == text else None


def _union(boxes: list[list[float]]) -> list[float]:
    return [round(min(b[0] for b in boxes), 1), round(min(b[1] for b in boxes), 1),
            round(max(b[2] for b in boxes), 1), round(max(b[3] for b in boxes), 1)]


def word_spans(lines: list[tuple[str, str | None, list | None]]) -> list[list] | None:
    """The word spans of a block made of ``lines``: each ``(text, raw, glyphs)`` is a line's final text, the line as
    the text layer read it and one display-frame box (or None) per character of ``raw``. The block text is
    ``"\\n".join(text.strip() ...)``. A line without glyphs, or whose order cannot be recovered, contributes no span;
    word numbers still count every word of the line. None when no span was found."""
    spans: list[list] = []
    offset = 0
    for li, (text, raw, glyphs) in enumerate(lines):
        stripped = text.strip()
        lead = len(text) - len(text.lstrip())
        order = logical_order(raw, text) if raw is not None and glyphs is not None and len(glyphs) == len(raw) \
            else None
        if order is not None:
            for wi, m in enumerate(_WORD.finditer(stripped)):
                boxes = [glyphs[order[lead + k]] for k in range(m.start(), m.end())]
                boxes = [b for b in boxes if b]
                if boxes:
                    spans.append([li, wi, offset + m.start(), offset + m.end(), *_union(boxes)])
        offset += len(stripped) + 1
    return spans or None
