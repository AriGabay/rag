---
title: Stored PDF block boxes are not in the frame the page is rendered in
date: 2026-10-09
category: logic-errors
module: backend PDF regions and chat visual reading (app/extraction/geometry.py, render.py, vision.py; app/platform/documents.py; app/chat/tools.py)
problem_type: logic_error
component: background_job
symptoms:
  - "On a rotated page, or one whose CropBox is offset from the MediaBox, the model reading a region on demand is shown a different place than the cited block"
  - "A region read on demand keeps returning the same wrong reading after the crop is fixed, because the stored reading is reused"
  - "A focused visual re-read of an unclear value never settles it: every on-demand reading comes back read_uncertain"
root_cause: logic_error
resolution_type: code_fix
severity: high
related_components: [api_layer, database]
tags: [pdf, bbox, coordinates, cropbox, mediabox, rotation, render, inspect, region-readings, cache-key, ocr, vision, re-read]
---

# Stored PDF block boxes are not in the frame the page is rendered in

## Problem

`document_blocks.bbox` holds pdfplumber's coordinates: points relative to the top-left of the page as pdfplumber
sees it (the rotated MediaBox). `render_png` crops the rendered page, which is the CropBox in display orientation.
On an upright page whose CropBox equals its MediaBox the two frames coincide, so passing the stored box straight to
the renderer looks right in every ordinary test. On a page rotated 90/180/270 degrees, or with a CropBox inset from
the MediaBox, it cuts the wrong place.

During round 6 the region view and the source viewer converted the box, but the chat's on-demand visual reading
(`inspect`, and the focused re-read of an unclear value) still passed the raw box for the whole round. Nothing
failed: the model simply read another part of the page.

## Root cause

Two frames with the same shape (x0, top, x1, bottom in points, origin top-left) and no type to tell them apart.
The conversion needs the page's stored geometry (`pages.mediabox`, `cropbox`, `rotation`, recorded at extraction
or by the positions backfill), which only exists after migration 0016, so the raw box was a natural default.

Two follow-on traps:

- **The cache kept the wrong reading.** `region_readings` is keyed by version, reading id, region id, reader version
  and model config, not by the crop rendered. Fixing the crop did not invalidate readings already stored from the
  old crop; only bumping `INSPECT_READER_VERSION` (to `inspect-v2`) did.
- **A re-read could not settle anything.** `transcribe` checks the model's numbers against OCR words of the image
  (`images.agreement`), and the on-demand path passed none. `agreement` returns 0.0 against an empty word list, so
  every on-demand reading was `read_uncertain` by design. That is the right default, since a model's own reading
  is not verification, but it means a "focused re-read" spends a model call and can never confirm a value unless
  OCR of the same crop is available.

## Solution

- `app/platform/documents.py::display_box_of(conn, version_id, page_no, bbox)` loads the page's stored geometry and
  returns the box in the rendered frame (`geometry.geometry_box`); a page without usable geometry keeps the raw box,
  as before positions existed. The region route and the chat's `_vision_read` both crop through it.
- `INSPECT_READER_VERSION` moved to `inspect-v2`, so readings cut in the old frame are read again once, when next
  inspected.
- `vision.transcribe(..., ocr_words=...)` takes the confident OCR words of the crop (`tools._crop_ocr_words`); a
  focused re-read is made only when OCR is available and settles a value only when the reading is `read` and holds
  its number. Without OCR no re-read is spent and the value stays uncertain.

Tests: `test_a_region_is_cropped_in_the_rendered_pages_frame_like_the_region_view`,
`test_a_reading_stored_by_an_older_inspect_reader_is_read_again` (tests/integration/test_chat_inspect.py), and the
OCR cases in tests/unit/test_value_status.py and tests/integration/test_chat_value_status.py. The end-to-end fixture
`C4_synthetic_citations_rotated_90_cropped.pdf` (tests/fixtures/citations) checks the viewer's highlight on such a page.

## Prevention

Any new code that renders, crops or highlights a stored region must go through `display_box_of` (server) or the
anchor's display-frame fractions (client), never `document_blocks.bbox` directly. When the crop of a cached visual
reading changes meaning, bump `INSPECT_READER_VERSION` (or the region reader version) in the same change, and test
on a rotated, CropBox-offset fixture, because an upright fixture cannot tell the two frames apart.
