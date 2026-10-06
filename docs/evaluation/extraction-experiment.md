# Extraction experiment (U5, KTD11)

Synthetic fixtures only (`backend/tests/fixtures`, labeled "מסמך סינתטי לדמו"). These numbers show that the
pipeline handles the layouts we generated. They do **not** measure quality on real appraisal reports, which
remains with the user (see "Limits" below).

Reproduce (the OCR part needs Tesseract with Hebrew data, so run it in the backend image):

```bash
docker run --rm -v "$PWD/backend:/app" -w /app appraisal-rag-backend python scripts/extraction_experiment.py
# raw numbers: ... python scripts/extraction_experiment.py --json
```

Run on 2026-10-05 in the `appraisal-rag-backend` image (Python 3.12, pdfplumber 0.11.10, pypdf, pypdfium2,
Tesseract 5.3.0 with `heb` + `eng`).

## What was compared

| extractor | what it is |
|---|---|
| `default` | The shipped extractor: pdfplumber text lines + per-line visual→logical fix-up (`app/extraction/hebrew.py`), pdfplumber `find_tables()` with column reversal and header detection (`app/extraction/tables.py`) |
| `pdfplumber_raw` | pdfplumber `extract_text()` with no fix-up (baseline) |
| `pypdf` | `PdfReader.pages[i].extract_text()` (content-stream order) |
| `pypdfium2` | `PdfTextPage.get_text_range()` |
| Docling | **not run**: Docling not installed. The torch-based install (~2–3 GB) was skipped because of disk constraints on the dev machine. It also has open RTL ordering bugs (#1938, #3462). |
| Tesseract `heb+eng` | OCR path (`app/extraction/ocr.py`) on the image-only D2 |

Corpus: the 12 digital PDFs (`pdf_digital` + `pdf_visual`, 16 pages, 740 non-empty table cells) and the
scanned D2 (1 page, 3 table rows).

Metrics:

- **Line similarity / lines exact**: for each line the layout contract guarantees (city, neighborhood,
  address, block/parcel, both dates, area, value, VAT basis, and the five numbered section headings), we find
  the most similar extracted line on the expected physical page. We report the mean `difflib` character
  similarity and the share of lines reproduced exactly.
- **Table cells**: `default` returns structured tables. A cell counts only if it is exactly right, in logical
  column order, with the row's correct physical page. The text-only extractors have no tables, so they get a
  more lenient score: the share of ground-truth cells that appear somewhere on the row's page as an exact
  token sequence. That measures recall only, with no structure. The two numbers are not directly comparable,
  and the comparison favors the text-only extractors.
- **ms / page**: wall time, including opening the file, on the host Docker VM. Indicative only.

## Results: digital PDFs

| extractor | docs / pages | line similarity | lines exact | table cells | ms / page |
|---|---|---|---|---|---|
| `default` | 12 / 16 | 1.000 | 100.0% | 740/740 (100.0%, exact structured) | 71.7 |
| `pdfplumber_raw` | 12 / 16 | 0.359 | 0.0% | 489/740 (66.1%, token recall) | 65.8 |
| `pypdf` | 12 / 16 | 0.646 | 20.9% | 81/740 (10.9%, token recall) | 31.4 |
| `pypdfium2` | 12 / 16 | 0.977 | 64.1% | 740/740 (100.0%, token recall) | 2.4 |

Per document, all extractors behave the same on every digital file. D7 scores slightly lower for the
baselines because it has fewer header lines. D10 (visual order) is extracted exactly like D1 by `default`.

What the baselines get wrong:

- `pdfplumber_raw`: every Hebrew line is reversed (`14 ןפגה :סכנה תבותכ`). Digit-only tokens survive, which
  is why it recalls 66% of cells, but no Hebrew cell comes out right.
- `pypdf`: the text is in logical order, but digit runs are dropped. fpdf2 writes them as separate text
  operations that pypdf places elsewhere (`כתובת הנכס: הגפן`, no `14`), so most numbers are lost. Unusable
  for this data.
- `pypdfium2`: nearly logical, and very fast. Numbers survive, but punctuation at the RTL/LTR boundary lands on
  the wrong side (`.1 מטרת השומה`, `הגפן ,14`), and the mirrored bracket glyphs come out swapped
  (`)סינתטי(`). It has no table structure. It is the best fallback for text if pdfminer cannot parse a file.

## Results: OCR on the scanned D2 (Tesseract `heb+eng`, 300 dpi)

| variant | rows | headers | data cells | page lines exact | seconds |
|---|---|---|---|---|---|
| with header snapping (shipped) | 3/3 | 9/9 | 27/27 | 86% (12/14) | 2.1 |
| raw OCR headers (no snapping) | 3/3 | 6/9 | 27/27 | 86% (12/14) | 2.1 |

- Data-cell accuracy is **27/27 (100%)** on this fixture: every address, block/parcel, date, room count,
  area, area type, price and price per m² is right. The table is ruled. The rules are found with PIL
  projection profiles and erased, the region is binarized and despeckled, and the cells are rebuilt from
  `image_to_data` word boxes assigned to grid cells.
- Raw OCR headers: `שטח (מר)`, `טוג שטח`, `מחיר למ'ר (₪)`. Tesseract loses the gershayim and confuses ס/ט.
  Header cells are re-read with Hebrew-only OCR, then snapped to the known header vocabulary
  (`tables.CANONICAL_HEADERS`; letters must be ≥ 80% similar and the ₪ unit must match). That gives 9/9.
- Page text: 12 of 14 contract lines are exact. The two misses are `שטח הנכס: 88 מ״ר נטו`, read as
  `Now הנכס: 88 מר נטו`, and `בסיס מע״מ: לא כולל מע״מ`, read as `בסיס מע"ימ: לא כולל n’yn`. With `heb+eng`,
  Tesseract sometimes reads short Hebrew words as Latin. With `heb` only, it destroys digits (`25/01/2024` →
  `2004`), so `heb+eng` stays the default. A word-level Hebrew-only retry was tried and rejected: it produced
  low-confidence and often wrong output.
- Every table from an OCR page is marked `ocr=True`, so U6 routes its records to `needs_review`.

This is one clean synthetic scan (200 dpi source, light speckle, 0.4° rotation, which deskew fixes).
Real scans (skewed photocopies, stamps, handwriting, borderless tables) will do worse. The borderless-table
path (columns from the header line's word positions) is covered only by a unit test with synthetic word
boxes, with no real scan behind it.

## Recommendation

1. Keep the `default` extractor (KTD11): pdfplumber + the per-line fix-up. It was the only one that got 100%
   of contract lines and 100% of structured table cells on the digital fixtures, including the visual-order D10
   and both bracket-glyph variants. It costs about 70 ms per page.
2. Keep Tesseract `heb+eng` for pages that fail the quality gate, and keep OCR records under review. The
   known weakness is short Hebrew words read as Latin in body text.
3. Use pypdfium2 text (plus the same fix-up) as the next fallback if pdfminer cannot parse a real file. Today
   such a file goes straight to OCR.
4. Evaluate Docling again when there is disk space for a separate image, and once its RTL issues are closed.
   It plugs in behind `Extractor` (`app/extraction/pipeline.py`).

## Limits

- All inputs are synthetic and come from one producer (fpdf2 + DejaVu Sans). Other producers (Word "Save as
  PDF", report generators, scanners) put Hebrew in different orders. The orientation check (final letters,
  label colons, sentence periods) and the quality gate are built for that, but they are untested on real files.
- No real appraisal report was used, so real-world quality is still unmeasured (it stays with the user).
- Docling was not run.
