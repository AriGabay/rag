"""Geometry-only backfill: positions for a version read before positions existed (U3, KTD3, R13).

A version read before KTD2 has its blocks, tables and ``reading_id``, but no page geometry, word spans or cell
boxes. Reading it again (``reindex_version``) would give it positions but also a new ``reading_id``, turning every
earlier citation of it stale, and would run OCR and the vision model over the corpus again. This pass instead reads
the stored PDF's text layer only, with the reader's own code (``app.extraction.pdf``: the same lines, glyph boxes,
orientation fix, cell boxes and conversion into the frame of the rendered page), and gives each *existing* block and
table the positions a fresh reading would have stored:

- a heading or paragraph read from the text layer is matched to a contiguous run of the re-read lines of its page by
  normalized text, in reading order (never by block index alone: an older reader may have numbered blocks
  differently); its word spans come from that run exactly as ``_Walker.add_text`` builds them, moved onto the stored
  text when the two differ only in whitespace or direction marks;
- a text-layer table is matched to a re-assembled table by its headers and every row's page and cells; its cell boxes
  come from that table;
- every page gets its geometry and printed page number.

Pages repaired for a broken font map are read again through the corrections recorded in the ingestion report
(``fontmap.corrections``), with no OCR: the report does not keep on which side of a word a positional mapping's final
form goes (``Correction.visual``), so such a report is read both ways and each page keeps the reading that aligns more
of its blocks. A block or table that does not align gets nothing; the counts of both go on the version. Pages read by
OCR and pictures read by OCR or the vision model get no positions, as in a fresh reading.

Nothing here touches the database: ``app.platform.pipeline.backfill_positions`` loads the stored reading, calls
``read_positions`` and writes the result in one transaction.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

import pdfplumber

from app.extraction import geometry
from app.extraction import pdf as reader
from app.extraction.base import PageGeometry, PageResult, check_deadline
from app.extraction.chunking import is_footer
from app.extraction.fontmap import Correction, FontMapFix
from app.extraction.hebrew import strip_bidi_controls
from app.extraction.tables import RawTable, assemble_tables, clean_cell

log = logging.getLogger(__name__)

TEXT_METHODS = ("text_layer", "mixed")  # pages and blocks whose text came from the text layer
_SPACE = re.compile(r"\s+")
_IGNORED = re.compile(r"[\s‎‏‪-‮⁦-⁩؜]")


@dataclass(frozen=True)
class StoredPage:
    page_no: int
    method: str
    ok: bool


@dataclass(frozen=True)
class StoredBlock:
    index: int
    kind: str
    page: int | None
    method: str | None
    text: str


@dataclass(frozen=True)
class StoredTable:
    index: int
    structure: dict


@dataclass
class PagePositions:
    page_no: int
    geometry: PageGeometry | None
    printed_label: str | None


def _counts() -> dict:
    return {"aligned": 0, "unaligned": 0, "no_positions": 0}


@dataclass
class Positions:
    """What the backfill writes: every page's geometry and printed number, the spans of each aligned block (by its
    stored index) and the new structure of each aligned table (by its stored index). ``counts``: per blocks and
    tables, how many aligned with positions, did not align, or aligned on a page whose positions cannot be
    converted (those keep their region only, as in a fresh reading)."""

    pages: list[PagePositions] = field(default_factory=list)
    spans: dict[int, list] = field(default_factory=dict)
    tables: dict[int, dict] = field(default_factory=dict)
    counts: dict = field(default_factory=lambda: {"blocks": _counts(), "tables": _counts()})


# --- the corrections recorded in the ingestion report ---------------------------------------------------------------

def fixes_from_report(report: dict | None) -> list[FontMapFix | None]:
    """The font-map repairs to read the document through, from the ingestion report's ``fontmap`` record: none when
    it records no correction; otherwise its corrections, once with every positional mapping's final form at the end
    of the word as the text layer stores it and, when a positional mapping exists, once at its start (a word stored
    in visual order). Unresolved pairs change no text, so they are not needed."""
    corrections = [c for c in ((report or {}).get("corrections") or []) if c.get("font") is not None
                   and c.get("from") and c.get("to")]
    if not corrections:
        return [None]
    orders = (False, True) if any(c.get("to_final") for c in corrections) else (False,)
    return [FontMapFix({(c["font"], c["from"]): Correction(c["font"], c["from"], c["to"], c.get("to_final"),
                                                         int(c.get("samples") or 0), float(c.get("agreement") or 0),
                                                         int(c.get("occurrences") or 0), visual)
                        for c in corrections}, {}, {}, set())
            for visual in orders]


def _needs_fix(page, fix: FontMapFix | None) -> bool:
    if fix is None:
        return False
    keys = set(fix.corrections)
    return any((c.get("fontname") or "", c.get("text")) in keys for c in page.chars)


# --- reading the text layer again -----------------------------------------------------------------------------------

def _read_page(doc, plumber, index: int, page: StoredPage | None, fix: FontMapFix | None) -> reader._PageOut:
    """One page the way ``pdf._read_page`` reads it, without the quality gate and its OCR fallback: a page the stored
    reading took from the text layer is read from it again; any other page only gets its geometry."""
    page_no = index + 1
    layer = None
    if plumber is not None and page is not None and page.ok and page.method in TEXT_METHODS:
        p = plumber.pages[index]
        try:
            layer = reader._text_layer(p, index, fix if _needs_fix(p, fix) else None)
        except Exception:  # noqa: BLE001 - a page the text layer cannot be read from gets no positions
            log.warning("positions: text layer failed on page %s", page_no, exc_info=True)
        finally:
            p.close()
    if layer is not None:
        raws = [RawTable(page_no, rows, ocr=False, top=bbox[1], bbox=bbox, uncertain=reason, boxes=boxes)
                for bbox, rows, reason, boxes in layer.tables]
        reader._mark_table_lines(layer.lines, raws)
        out = reader._PageOut(PageResult(page_no, "\n".join(ln.text for ln in layer.lines), "text_layer", 1.0, True),
                              layer.lines, raws, [], [], layer.raw_texts if layer.visual is None else None,
                              layer.raw_texts)
    else:
        out = reader._PageOut(PageResult(page_no, "", page.method if page else "failed", 0.0,
                                         bool(page and page.ok)), [], [])
    out.frame = reader._frame(plumber, index)
    pdf_page = doc[index]
    try:
        out.width, out.height = pdf_page.get_size()
        out.page.geometry = reader._page_geometry(pdf_page, out)
    finally:
        pdf_page.close()
    reader._place(out)
    return out


def _read_document(doc, plumber, pages: dict[int, StoredPage], fix: FontMapFix | None,
                   deadline: float) -> list[reader._PageOut]:
    outs = []
    for index in range(len(doc)):
        check_deadline(deadline)
        outs.append(_read_page(doc, plumber, index, pages.get(index + 1), fix))
    reader._orient_undecided_pages(outs)
    return outs


# --- alignment --------------------------------------------------------------------------------------------------

def _key(text: str) -> str:
    return _SPACE.sub(" ", strip_bidi_controls(text or "")).strip()


def block_lines(lines: list) -> list:
    """The lines of a page that headings and paragraphs are made of, in the order ``_Walker.page`` visits them."""
    return [ln for ln in sorted(lines, key=lambda ln: ln.top)
            if ln.text.strip() and not ln.in_table and not is_footer(ln.text)]


def align_blocks(blocks: list[StoredBlock], lines: list) -> dict[int, list]:
    """Each block's run of ``lines`` (``block_lines`` of its page): the next contiguous run, after the previous
    block's, whose lines read the block's lines by normalized text. Blocks are visited in their stored order; one
    that does not align is left out and does not move the cursor."""
    cand = block_lines(lines)
    keys = [_key(ln.text) for ln in cand]
    found: dict[int, list] = {}
    cursor = 0
    for b in sorted(blocks, key=lambda b: b.index):
        want = [_key(t) for t in b.text.split("\n")]
        if not any(want):
            continue
        n = len(want)
        for i in range(cursor, len(cand) - n + 1):
            if keys[i:i + n] == want:
                found[b.index] = cand[i:i + n]
                cursor = i + n
                break
    return found


def _units(text: str) -> list[int]:
    return [k for k, ch in enumerate(text) if not _IGNORED.match(ch)]


def remap_spans(spans: list[list], src: str, dst: str) -> list[list] | None:
    """Spans indexing ``src`` moved onto ``dst``, which holds the same characters apart from whitespace and direction
    marks; None when it does not."""
    a, b = _units(src), _units(dst)
    if len(a) != len(b) or any(src[i] != dst[j] for i, j in zip(a, b, strict=True)):
        return None
    where = {i: k for k, i in enumerate(a)}
    out = []
    for s in spans:
        inside = [where[i] for i in range(s[2], s[3]) if i in where]
        if not inside:
            return None
        out.append([s[0], s[1], b[inside[0]], b[inside[-1]] + 1, *s[4:]])
    return out


def _block_spans(block: StoredBlock, run: list) -> tuple[str, list | None]:
    """``("aligned", spans)``, ``("no_positions", None)`` (the page cannot be converted, or no glyph found) or
    ``("unaligned", None)`` when the run's text cannot be mapped onto the stored text."""
    spans = geometry.word_spans([(ln.text, ln.layer_text, ln.glyphs) for ln in run])
    if spans is None:
        return "no_positions", None
    text = "\n".join(ln.text.strip() for ln in run)
    if text != block.text:
        spans = remap_spans(spans, text, block.text)
        if spans is None:
            return "unaligned", None
    return "aligned", spans


def _cells(cells) -> list[str]:
    return [_key(clean_cell(c)) for c in cells or []]


def _table_matches(structure: dict, table) -> bool:
    rows = structure.get("rows") or []
    return (_cells(structure.get("headers")) == _cells(table.headers) and len(rows) == len(table.rows)
            and all(r.get("page") == t.page and _cells(r.get("cells")) == _cells(t.cells)
                    for r, t in zip(rows, table.rows, strict=True)))


def _with_boxes(structure: dict, table) -> dict | None:
    """The stored structure with the re-read table's cell boxes, stored the way ``_insert_outputs`` stores them;
    None when the table has none (its pages cannot be converted)."""
    if not table.header_boxes and not any(r.cell_boxes for r in table.rows):
        return None
    out = dict(structure)
    out["rows"] = [{k: v for k, v in r.items() if k != "cell_boxes"} | ({"cell_boxes": t.cell_boxes}
                                                                         if t.cell_boxes else {})
                   for r, t in zip(structure.get("rows") or [], table.rows, strict=True)]
    out.pop("header_boxes", None)
    if table.header_boxes:
        out["header_boxes"] = table.header_boxes
    return out


def _text_tables(outs: list[reader._PageOut]):
    """The text-layer tables the reader assembles from these pages (pieces in page order, top to bottom)."""
    raws: list[RawTable] = []
    for out in outs:
        for k, raw in enumerate(sorted(out.tables, key=lambda t: t.top)):
            raw.first_on_page = k == 0
            raws.append(raw)
    return assemble_tables(raws)


# --- the whole version ------------------------------------------------------------------------------------------

def read_positions(data: bytes, pages: list[StoredPage], blocks: list[StoredBlock], tables: list[StoredTable],
                   fontmap_report: dict | None, settings, deadline: float) -> Positions:
    """The positions of a stored reading, from the PDF's text layer alone (KTD3). Raises ``ExtractionError`` for a
    file the reader cannot open (permanent: reading it again would fail the same way)."""
    stored_pages = {p.page_no: p for p in pages}
    doc = reader.open_pdf(data, settings.max_pages)
    try:
        try:
            plumber = pdfplumber.open(io.BytesIO(data))
        except Exception:  # noqa: BLE001 - pdfium opened it; pdfminer did not: geometry only
            plumber = None
        try:
            variants = [_read_document(doc, plumber, stored_pages, fix, deadline)
                        for fix in fixes_from_report(fontmap_report)]
        finally:
            if plumber is not None:
                plumber.close()
        reader._printed_labels(doc, variants[0])
    finally:
        doc.close()

    out = Positions()
    # a block of a reader older than block provenance has no method: its page's method says where it came from
    eligible = [b for b in blocks if b.kind in ("heading", "paragraph") and (b.method is None or b.method in TEXT_METHODS)
                and b.page is not None and stored_pages.get(b.page) is not None
                and stored_pages[b.page].method in TEXT_METHODS]
    by_page: dict[int, list[StoredBlock]] = {}
    for b in eligible:
        by_page.setdefault(b.page, []).append(b)
    chosen = list(variants[0])
    for index in range(len(chosen)):
        page_blocks = by_page.get(index + 1, [])
        best: tuple[int, int, dict] | None = None
        for k, outs in enumerate(variants):
            runs = align_blocks(page_blocks, outs[index].lines)
            if best is None or len(runs) > best[1]:
                best = (k, len(runs), runs)
        k, _, runs = best
        chosen[index] = variants[k][index]
        for b in page_blocks:
            run = runs.get(b.index)
            state, spans = _block_spans(b, run) if run is not None else ("unaligned", None)
            out.counts["blocks"][state] += 1
            if spans:
                out.spans[b.index] = spans

    for o, first in zip(chosen, variants[0], strict=True):
        g = first.page.geometry
        out.pages.append(PagePositions(o.page.page_no, g, first.page.printed_label))

    rebuilt = _text_tables(chosen)
    cursor = 0
    for t in sorted(tables, key=lambda t: t.index):
        s = t.structure or {}
        if s.get("ocr") or (s.get("source") or "text") != "text":
            continue  # OCR and vision tables keep no cell boxes (KTD2)
        hit = next((j for j in range(cursor, len(rebuilt)) if _table_matches(s, rebuilt[j])), None)
        if hit is None:
            out.counts["tables"]["unaligned"] += 1
            continue
        cursor = hit + 1
        new = _with_boxes(s, rebuilt[hit])
        if new is None:
            out.counts["tables"]["no_positions"] += 1
            continue
        out.counts["tables"]["aligned"] += 1
        out.tables[t.index] = new
    return out
