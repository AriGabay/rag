"""Extraction experiment (KTD11, spec section 3): compare text extractors on the synthetic fixtures.

Extractors compared on the digital PDFs (``pdf_digital`` + ``pdf_visual``):

- ``default``: pdfplumber + per-line visual->logical fix-up + pdfplumber tables (the shipped extractor)
- ``pdfplumber_raw``: pdfplumber ``extract_text()`` without the fix-up (baseline)
- ``pypdf``: ``PdfReader.page.extract_text()`` (content-stream order)
- ``pypdfium2``: ``PdfTextPage.get_text_range()``
- ``docling``: only if importable (not installed by default; see the report)

Metrics per extractor:

- header-line accuracy: for each known line (report header labels and the five section headings) the best
  matching extracted line on its page, scored with ``difflib`` character similarity; plus the share of lines
  reproduced exactly
- table cells: ``default`` returns structured tables, scored as exact cell matches (logical column order,
  correct row page). Text-only extractors are scored by cell recall: the share of non-empty ground-truth
  cells that appear as an exact whitespace-delimited token sequence on the row's page
- runtime per page

OCR (Tesseract ``heb+eng``) on the scanned D2: structured cell accuracy and header accuracy, with and without
snapping OCR'd header cells to the known header vocabulary. Needs Tesseract with Hebrew data; run in the
backend image::

    docker run --rm -v "$PWD/backend:/app" -w /app appraisal-rag-backend python scripts/extraction_experiment.py

Prints Markdown result tables to stdout (``--json`` for raw numbers).
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
TRUTH = yaml.safe_load((FIXTURES / "ground_truth.yaml").read_text(encoding="utf-8"))
PDF = "application/pdf"
AREA_TYPES = {"net": "נטו", "gross": "ברוטו", "registered": "רשום", "equivalent": "אקוויוולנטי"}
VAT = {"included": "כולל מע״מ", "excluded": "לא כולל מע״מ"}


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def expected_lines(doc: dict) -> list[tuple[int, str]]:
    """(page, line) pairs the layout contract guarantees, rebuilt from the ground truth."""
    out: list[tuple[int, str]] = []
    av = next((r for r in doc["records"] if r["data_kind"] == "appraised_value"), None)
    if av:
        p = av["page"]
        out.append((p, f"עיר: {av['city']}"))
        if av["neighborhood"]:
            out.append((p, f"שכונה: {av['neighborhood']}"))
        out.append((p, f"כתובת הנכס: {av['address']}"))
        bp = f"גוש: {av['block']} חלקה: {av['parcel']}"
        if av["sub_parcel"]:
            bp += f" תת חלקה: {av['sub_parcel']}"
        out.append((p, bp))
        for key, label in (("valuation_date", "המועד הקובע"), ("report_date", "תאריך עריכת השומה")):
            y, m, d = av[key].split("-")
            out.append((p, f"{label}: {d}/{m}/{y}"))
        out.append((p, f"שטח הנכס: {av['area']} מ״ר {AREA_TYPES[av['area_type']]}"))
        price = f"{int(av['price']):,}"
        # Layout contract: D5 writes the shekel sign first.
        out.append((p, f"שווי הנכס: ₪ {price}" if doc["id"] == "D5" else f"שווי הנכס: {price} ₪"))
        out.append((p, f"בסיס מע״מ: {VAT[av['vat_basis']]}"))
    for s in doc["sections"]:
        out.append((s["page"], s["title"]))
    return out


# --- extractors: return (pages_text: dict[page, str], tables or None, seconds) -------------------------

def run_default(data: bytes):
    from app.extraction.default import DefaultExtractor

    t = time.perf_counter()
    r = DefaultExtractor().extract(data, PDF, time.monotonic() + 600)
    return {p.page_no: p.text for p in r.pages}, r.tables, time.perf_counter() - t


def run_pdfplumber_raw(data: bytes):
    import pdfplumber

    t = time.perf_counter()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = {i + 1: (p.extract_text() or "") for i, p in enumerate(pdf.pages)}
    return pages, None, time.perf_counter() - t


def run_pypdf(data: bytes):
    from pypdf import PdfReader

    t = time.perf_counter()
    reader = PdfReader(io.BytesIO(data))
    pages = {i + 1: (p.extract_text() or "") for i, p in enumerate(reader.pages)}
    return pages, None, time.perf_counter() - t


def run_pypdfium2(data: bytes):
    import pypdfium2 as pdfium

    t = time.perf_counter()
    doc = pdfium.PdfDocument(data)
    pages = {}
    for i in range(len(doc)):
        page = doc[i]
        tp = page.get_textpage()
        pages[i + 1] = tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
        tp.close()
        page.close()
    doc.close()
    return pages, None, time.perf_counter() - t


def run_docling(data: bytes):  # pragma: no cover - only when Docling is installed
    import tempfile

    from docling.document_converter import DocumentConverter

    t = time.perf_counter()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        res = DocumentConverter().convert(f.name)
    text = res.document.export_to_markdown()
    # Docling has no stable per-page text in markdown export; all text is attributed to every page.
    n = len(res.document.pages)
    return {i + 1: text for i in range(n)}, None, time.perf_counter() - t


def docling_available() -> bool:
    try:
        import docling  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


# --- metrics ------------------------------------------------------------------------------------------------

def line_scores(pages: dict[int, str], doc: dict) -> tuple[float, float]:
    sims, exact = [], 0
    for page, line in expected_lines(doc):
        cands = [norm(x) for x in pages.get(page, "").splitlines() if x.strip()]
        best = max((difflib.SequenceMatcher(None, line, c).ratio() for c in cands), default=0.0)
        sims.append(best)
        exact += line in cands
    n = len(sims) or 1
    return sum(sims) / n, exact / n


def table_scores(pages: dict[int, str], tables, doc: dict) -> tuple[int, int]:
    """(matched cells, total non-empty cells)."""
    ok = total = 0
    for t_index, exp in enumerate(doc["tables"]):
        got_rows = tables[t_index].rows if tables is not None and t_index < len(tables) else []
        for r_index, row in enumerate(exp["rows"]):
            got = got_rows[r_index] if r_index < len(got_rows) else None
            page_tokens = norm(pages.get(row["page"], "")).split(" ")
            for c_index, cell in enumerate(row["cells"]):
                if not norm(cell):
                    continue
                total += 1
                if tables is not None:
                    ok += bool(got and got.page == row["page"] and c_index < len(got.cells)
                               and norm(got.cells[c_index]) == norm(cell))
                else:
                    ok += _contains_tokens(page_tokens, norm(cell).split(" "))
    return ok, total


def _contains_tokens(hay: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return any(hay[i:i + n] == needle for i in range(len(hay) - n + 1))


def run_digital(extractors: dict) -> dict:
    docs = [d for d in TRUTH["documents"] if d["kind"] in ("pdf_digital", "pdf_visual")]
    results: dict = {}
    for name, fn in extractors.items():
        sims, exacts, cells_ok, cells_total, secs, n_pages = [], [], 0, 0, 0.0, 0
        per_doc = {}
        for doc in docs:
            data = (FIXTURES / doc["filename"]).read_bytes()
            pages, tables, sec = fn(data)
            sim, exact = line_scores(pages, doc)
            ok, total = table_scores(pages, tables, doc)
            sims.append(sim)
            exacts.append(exact)
            cells_ok += ok
            cells_total += total
            secs += sec
            n_pages += doc["page_count"]
            per_doc[doc["id"]] = {"line_similarity": round(sim, 4), "lines_exact": round(exact, 4),
                                  "cells": f"{ok}/{total}"}
        results[name] = {
            "documents": len(docs),
            "pages": n_pages,
            "line_similarity": round(sum(sims) / len(sims), 4),
            "lines_exact": round(sum(exacts) / len(exacts), 4),
            "cells_ok": cells_ok,
            "cells_total": cells_total,
            "ms_per_page": round(1000 * secs / max(1, n_pages), 1),
            "per_doc": per_doc,
        }
    return results


def run_ocr() -> dict:
    from app.extraction import ocr

    langs = "heb+eng"
    if not ocr.ocr_available(langs):
        return {"skipped": "tesseract with Hebrew data not installed here (run in the backend image)"}
    doc = next(d for d in TRUTH["documents"] if d["id"] == "D2")
    data = (FIXTURES / doc["filename"]).read_bytes()
    out: dict = {}
    for label, snap in (("with_header_snapping", True), ("raw_ocr_headers", False)):
        original = ocr.snap_header
        if not snap:
            ocr.snap_header = lambda s: norm(s)  # type: ignore[assignment]
        try:
            pages, tables, sec = run_default(data)
        finally:
            ocr.snap_header = original
        exp_t = doc["tables"][0]
        got_t = tables[0] if tables else None
        headers_ok = sum(1 for a, b in zip(got_t.headers if got_t else [], exp_t["headers"], strict=False)
                         if norm(a) == b)
        ok, total = table_scores(pages, tables, doc)
        sim, exact = line_scores(pages, doc)
        out[label] = {
            "seconds": round(sec, 2),
            "tables": len(tables),
            "rows": len(got_t.rows) if got_t else 0,
            "rows_expected": len(exp_t["rows"]),
            "headers_ok": f"{headers_ok}/{len(exp_t['headers'])}",
            "cells_ok": f"{ok}/{total}",
            "line_similarity": round(sim, 4),
            "lines_exact": round(exact, 4),
            "mismatched_cells": [
                (r_i, c_i, got_t.rows[r_i].cells[c_i], cell)
                for r_i, row in enumerate(exp_t["rows"]) if got_t and r_i < len(got_t.rows)
                for c_i, cell in enumerate(row["cells"]) if norm(got_t.rows[r_i].cells[c_i]) != norm(cell)
            ],
            "page_lines_not_exact": [
                line for p, line in expected_lines(doc)
                if line not in [norm(x) for x in pages.get(p, "").splitlines()]
            ],
        }
    return out


def to_markdown(digital: dict, ocr_res: dict, docling_note: str) -> str:
    lines = ["### Digital PDFs", "",
             "| extractor | docs / pages | header+heading line similarity | lines exact | table cells | ms / page |",
             "|---|---|---|---|---|---|"]
    for name, r in digital.items():
        kind = "exact structured" if name == "default" else "token recall in text"
        lines.append(
            f"| `{name}` | {r['documents']} / {r['pages']} | {r['line_similarity']:.3f} | {r['lines_exact']:.1%} | "
            f"{r['cells_ok']}/{r['cells_total']} ({r['cells_ok'] / max(1, r['cells_total']):.1%}, {kind}) | "
            f"{r['ms_per_page']} |"
        )
    lines += ["", f"Docling: {docling_note}", "", "### Per document (lines exact / cells)", "",
              "| doc | " + " | ".join(digital) + " |", "|---|" + "---|" * len(digital)]
    doc_ids = list(next(iter(digital.values()))["per_doc"])
    for did in doc_ids:
        lines.append(f"| {did} | " + " | ".join(
            f"{digital[n]['per_doc'][did]['lines_exact']:.0%} / {digital[n]['per_doc'][did]['cells']}" for n in digital
        ) + " |")
    lines += ["", "### OCR on the scanned D2 (Tesseract heb+eng)", ""]
    if "skipped" in ocr_res:
        lines.append(f"Skipped: {ocr_res['skipped']}")
    else:
        lines += ["| variant | rows | headers | data cells | page lines exact | seconds |", "|---|---|---|---|---|---|"]
        for label, r in ocr_res.items():
            lines.append(f"| {label} | {r['rows']}/{r['rows_expected']} | {r['headers_ok']} | {r['cells_ok']} | "
                         f"{r['lines_exact']:.0%} | {r['seconds']} |")
        r = ocr_res["with_header_snapping"]
        lines += ["", f"Mismatched data cells: {r['mismatched_cells'] or 'none'}",
                  f"Expected page lines not reproduced exactly: {r['page_lines_not_exact'] or 'none'}"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of Markdown")
    args = ap.parse_args()
    import logging

    logging.disable(logging.WARNING)
    extractors = {
        "default": run_default,
        "pdfplumber_raw": run_pdfplumber_raw,
        "pypdf": run_pypdf,
        "pypdfium2": run_pypdfium2,
    }
    if docling_available():
        extractors["docling"] = run_docling
        docling_note = "run (see table)."
    else:
        docling_note = ("not run: Docling not installed — torch-based install (~2–3 GB) skipped due to disk "
                        "constraints on the dev machine; open RTL bugs #1938/#3462.")
    run_default(next((FIXTURES / d["filename"]).read_bytes() for d in TRUTH["documents"] if d["id"] == "D1"))
    digital = run_digital(extractors)
    ocr_res = run_ocr()
    if args.json:
        print(json.dumps({"digital": digital, "ocr": ocr_res, "docling": docling_note}, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(digital, ocr_res, docling_note))


if __name__ == "__main__":
    main()
