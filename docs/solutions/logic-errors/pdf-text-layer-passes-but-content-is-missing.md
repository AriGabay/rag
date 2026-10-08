---
title: A PDF page whose text layer looks fine can still hide its most important content
date: 2026-10-08
category: logic-errors
module: backend PDF ingestion (app/extraction/pdf.py, regions.py, fontmap.py, images.py; app/platform/pipeline.py)
problem_type: logic_error
component: background_job
symptoms:
  - "A table drawn as an image on a page with a normal heading and body text never reaches search; the page is stored as read, quality 1.0, no warning, document not partial"
  - "Hebrew words lose one letter everywhere in a document (nun read as 'ð') and search for those words fails, while every page passes the quality score"
  - "A logo and dozens of identical background images per page would be sent to vision on every page"
  - "Bold text comes out with doubled letters; document titles come out in reversed visual order"
  - "After a correct reprocess, the no-regression gate flags a page of property photos or a footer as having lost numbers"
root_cause: missing_validation
resolution_type: code_fix
severity: high
tags: [pdf, ingestion, ocr, vision, image-table, vector-content, page-furniture, content-hash, tounicode, cid-font, hebrew, reprocess-gate]
---

# A PDF page whose text layer looks fine can still hide its most important content

## Problem

The PDF reader accepted a page as soon as its text layer passed a character-share quality score. Real appraisal
reports mix a valid text layer with content the text layer does not carry, so the most important figures of a
report (an income / cost / profit table embedded as a picture) were silently missing from search, and the
document was not marked partial. A second document had a broken font map that the same score accepted.

## Symptoms

- A page with a heading and paragraphs plus a table pasted as a picture (about a third of the page) scored 1.0;
  no chunk held any number from the table.
- A CID font with a broken `ToUnicode` map emitted a plausible Latin glyph (`ð`) for one Hebrew letter, about 600
  times in one document. Every page scored 0.95+ and passed.
- Each page carried a logo and about 25 identical small images (a watermark or background).

## What Didn't Work

- **A text-quality score as the gate.** It measures how much of the extracted text looks like language, not how
  much of the page the text covers. A page with good text and an image table scores perfectly.
- **A size threshold for pictures.** Too high, and a small but meaningful table or stamp with numbers is dropped.
  Too low, and the logo and watermark are read on every page (25 × pages vision calls).
- **"Few OCR words" as proof a picture is empty.** OCR on a low-resolution table image returns a handful of
  words; the table still holds every number the question needs.
- **A blind replacement of `ð` with nun.** `ð` is a real letter in other languages and documents; a global
  replacement corrupts them and can never be checked.

## Solution

Look for content per page, regardless of how good the text is, and read each distinct content once.

1. **Find what the text layer does not explain** (`backend/app/extraction/regions.py`). Candidate regions are the page's
   image objects plus *ink* found by rendering the page's vector paths only (`find_ink_regions`, `INK_DPI`) and
   grouping it into regions. A region counts as covered only if the text layer actually holds its words
   (`is_covered`: enough characters, agreement with OCR's confident words, or dense and spread characters
   without OCR). Small marks are skipped by an absolute floor (`MIN_REGION_PT`), never by a share of the page.
2. **Recognise page furniture by content, not size** (`mark_repeated`). A content hash that occurs
   `REPEAT_MIN` (3) times or more in a document is furniture: stored as one block and read once. Readings are
   cached content-addressed per office (`image_readings`, system-only RLS), so a logo shared by many reports is
   read once per office.
3. **Escalate by evidence, not word count.** OCR first; table-like or low-effective-DPI regions go to vision at
   a resolution that keeps cells legible (`READ_DPI`, `render.py`). Vision failures are classified
   (`backend/app/extraction/images.py`, `backend/app/providers/llm.py`): transient or configuration failures raise and fail the job,
   so the previous good reading stays; a deterministic failure (BadRequest / 422 → `INVALID`, refusal) leaves the
   region unread and the document is marked partial with the reason, visible to the model and the documents
   screen.
4. **Repair a broken font map only from verified evidence** (`backend/app/extraction/fontmap.py`). Detect per font: a
   foreign glyph inside Hebrew words, or a frequent Hebrew letter a large font never emits (`min_hebrew_letters`,
   `min_expected_absent`). Verify each suspect glyph by OCR on single-word crops (`--psm 7`) aligned with the
   rest of the word (`min_context_match`), and accept a mapping only with enough agreeing votes
   (`min_samples`, `min_agreement`). Keep `original_text` beside the corrected text, apply the mapping only to
   that font's letters, never to digits, symbols or units, and record the reason when it cannot be verified
   (`REASON_NO_OCR`, `REASON_FEW_SAMPLES`, `REASON_INCONSISTENT`).
5. **Keep the reprocess gate honest about furniture** (`backend/app/platform/pipeline.py`, `_furniture_numbers`). The
   new reading reads furniture once for the whole document, so its numbers must count as present on every page;
   otherwise a footer page, or a page of photos whose old reading was OCR noise, looks like a regression.

## Why This Works

The failure was a coverage question answered with a quality metric. Measuring coverage (what on the page is not
explained by the text layer) catches image tables, scans and vector tables on pages that otherwise look
perfect. Content hashing separates "repeated" from "small", which a size threshold cannot do. Verified per-font
mapping fixes the corruption where it comes from, keeps an audit trail, and cannot spread to documents where
the glyph is legitimate.

## Prevention

- Treat "page read" as a coverage claim: every meaningful region is read, or the document is partial with a
  reason. Never infer coverage from a text-quality score.
- Structural ingestion checks in the private evaluation look for specific table cells (row, column, unit, page),
  not only for a number appearing somewhere; synthetic fixtures for image tables and broken font maps live in
  `backend/tests/fixtures/fontmap` and `backend/tests/fixtures/regions` (`backend/scripts/make_fontmap_fixtures.py`).
- Two more PDF text-layer quirks seen on the same reports: bold text drawn twice duplicates glyphs, and titles
  stored in visual order come out reversed. Check both when a new report source is added.

## Related Issues

- `docs/operations/reprocessing.md` — the swap, the no-regression gate and `accept_regression`.
- `docs/solutions/database-issues/force-rls-security-definer-lookups-need-bypassrls-owner.md` — the RLS model the
  office-scoped reading cache follows.
- PR AriGabay/rag#2 (pending).
