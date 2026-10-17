"""Appraisal units inside one file, derived from its stored reading and kept in ``document_units`` (round 9 U7: KTD4,
KTD5; R6–R9).

A file may hold one appraisal or a compendium of many, followed by appendices of comparables. A *unit* is a range of
the file's blocks and pages with its own identity (a title, document-grounded aliases, identifying details), a kind
and a certainty marker. Units come only from the document (R7): a name, an alias or a boundary is recorded with the
blocks it was read from, and a comparable property named in a table or in the text never becomes a unit.

**Derivation** (``derive``): from the version's ``document_blocks`` and ``pages`` only — no re-reading of the file, no
model call (R9). A boundary opens before a page (or, for the identifier rule, before a title block) where independent
signals say a new part starts; each signal is recorded as evidence by name:

- ``label_restart`` — the printed page number drops below the previous numbered page's (``pages.printed_label``; when
  the reading kept no label because the numbering restarts, which ``_printed_labels`` takes for unreliable, the
  page-number block in the top or bottom band of the page, then the page-number line of the page's stored text). The
  boundary is the first page after the old sequence's last numbered page, so a new report's unnumbered cover pages
  belong to it; the evidence records the pages that show the drop;
- ``title_block_repeat`` — the page opens with a title block (a prominent single line followed by more lines before
  the first heading) whose layout repeats the file's first title block: same top, same line height (the stored
  ``bbox`` is pdfplumber's frame; positions are compared within the one document, never rendered). A weak signal:
  pages of one report share a layout, so it never cuts alone;
- ``toc_entry`` — a table-of-contents entry maps the title the page opens with to that page;
- ``identifier_numbering_restart`` — the restated-identifier rule of ``contexts.derive``, reused as is.

Two signals or more make the cut ``certain``, one strong signal makes it ``probable`` (R8: it stays marked, and
reading may extend across it), none, or the weak signal alone: no cut. A file with no cut is one unit of kind ``document`` and certainty
``unsplit``: it is not presented as an appraisal on the strength of a first heading (R8).

**Identity** is the unit's own title block, the lines at the top of its first page up to the first heading: the title
is the most prominent line; aliases are quoted names, named-project phrases (a word of ``PROJECT_WORDS`` and the
words after it) and addresses; identifiers are the labelled addresses and block/parcel pairs (``contexts.
identifiers``). A picture at the top that was read (``read``, ``read_uncertain``) is title evidence too, marked weak.
Lines that repeat as the letterhead of several units, running headers and footers (text on half the pages) and a
repeated picture (page furniture) never become a title, an alias or a boundary; neither does boilerplate that names
nothing (``is_boilerplate``: spaced single letters, an accessibility tag).

**Kind**: ``appendix`` (an appendix word opens the unit), ``comparables`` (mostly tables, or a comparables heading
without chapters of its own), ``front_matter`` (before the first appraisal, without chapters of its own, and short: at
most ``FRONT_MATTER_PAGES_MAX`` pages — a long part before the first appraisal is ``document``), ``appraisal`` (own
numbered chapters or a valuation heading), otherwise ``document``. The vocabulary below is the one place that names
words of a report's structure; it never holds question words.

**Evidence** holds block indexes and rule names only, never copied text beyond the title and aliases themselves.

**Store** (KTD4, round 15 KTD6): ``document_units``, keyed (version, reading, derivation key, unit number), so the
rows of a new derivation live beside the previous ones and nothing deletes the old (they are read only to resolve the
unit anchors a conversation stored before: ``entities.live_unit``; every reader filters by the current key), written by ``replace`` inside the
publish transaction and the reindex swap (so a ``ready`` version always has units) and by ``rebuild_if_stale`` when a
positions backfill changes the derivation key (``UNITS_VERSION`` plus the reading's positions marker); a version
published before this round gets its units on first use (``ensure``, single-flight per version and reading). Rows are
visible through the document-access policy only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import statistics
import threading
from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as dc_replace
from itertools import islice
from uuid import UUID

from sqlalchemy import Connection, text

from app.chat import contexts
from app.extraction import hebrew
from app.extraction.normalize_text import base_normalize
from app.platform.source_kinds import APPRAISALS_SQL
from app.professional.sections import flight, landed

logger = logging.getLogger(__name__)

UNITS_VERSION = "units-v4"
# the derivation whose stable ids and anchors carry no tag: an id or an anchor without a tag is a ``units-v4`` one
# (round 15 U3, KTD6); every later derivation's ids end in ``@<tag>``
UNTAGGED_VERSION = "units-v4"


def tag_of(units_version: str) -> str | None:
    """The short tag a derivation's stable unit ids and anchors carry (``units-v5`` -> ``v5``); None for the untagged
    ``units-v4``, so the ids already stored stay valid."""
    return None if units_version == UNTAGGED_VERSION else units_version.removeprefix("units-")


def version_of_tag(tag: str | None) -> str:
    """The ``units_version`` an id or an anchor with this tag was derived by (no tag: ``units-v4``)."""
    return f"units-{tag}" if tag else UNTAGGED_VERSION


def current_tag() -> str | None:
    """The tag of the derivation the code derives now."""
    return tag_of(UNITS_VERSION)


def current_key_sql(unit: str = "u", version: str = "v") -> str:
    """The SQL condition that a ``document_units`` row (alias ``unit``) is of the current derivation key of its version
    (alias ``version``, ``document_versions``): ``UNITS_VERSION`` plus the reading's positions marker, as
    ``derivation_key`` builds it. Every raw reader of the table uses it, because the table holds the rows of earlier
    derivations beside the current ones (round 15 U3, KTD6)."""
    return (f"{unit}.derivation_key = '{UNITS_VERSION}|' || COALESCE(NULLIF({version}.ingestion->>'positions', ''), '-')")

# --- kinds, certainty, rule names --------------------------------------------------------------------------------

APPRAISAL, APPENDIX, COMPARABLES, FRONT_MATTER, DOCUMENT = "appraisal", "appendix", "comparables", "front_matter", "document"
KINDS = (APPRAISAL, APPENDIX, COMPARABLES, FRONT_MATTER, DOCUMENT)
CERTAIN, PROBABLE, UNSPLIT = "certain", "probable", "unsplit"

LABEL_RESTART = "label_restart"
TITLE_BLOCK_REPEAT = "title_block_repeat"
TOC_ENTRY = "toc_entry"
IDENTIFIER_RESTART = "identifier_numbering_restart"
SIGNALS = (LABEL_RESTART, TITLE_BLOCK_REPEAT, TOC_ENTRY, IDENTIFIER_RESTART)
CERTAIN_SIGNALS = 2  # independent signals that make a cut certain
# a repeated layout is weak evidence: many pages of one report may share a title block's layout, so it never cuts
# alone; with a strong signal it makes the cut certain
WEAK_SIGNALS = frozenset({TITLE_BLOCK_REPEAT})

# --- the vocabulary of a report's structure (never question words) -------------------------------------------------

# a word that introduces a named project in a title block: "פרויקט X", "מתחם X", "מרכז X"
PROJECT_WORDS = ("פרויקט", "מתחם", "מרכז", "בית", "מגדל", "מגדלי", "קניון", "פארק", "קמפוס", "שכונת")
# a word that follows a project word without naming anything ("מרכז מסחרי"), which ends the name
NAME_STOP_WORDS = frozenset({"מסחרי", "מסחר", "מגורים", "משרדים", "תעשיה", "תעשייה", "לוגיסטי", "ציבורי", "אזורי",
                             "עירוני", "רפואי", "הנדון", "הבא", "זה", "זו"})
# a generic place word that opens a name ("גבעת הרקפות"); the name without it is an alias too
PLACE_WORDS = frozenset({"שדרות", "גבעת", "נחל", "רכס", "כפר", "עמק", "הר", "מעלה", "רמת", "קרית", "קריית", "נווה",
                         "גן", "שכונת", "רחוב"})
APPENDIX_WORDS = ("נספח", "נספחים", "תוספת")
COMPARABLES_WORDS = ("עסקאות השוואה", "עסקאות להשוואה", "נכסים להשוואה", "נתוני השוואה", "נכסי השוואה")
VALUATION_WORDS = ("הערכת שווי", "תחשיב", "חוות דעת", "שומה")
TOC_WORDS = ("תוכן", "תוכן עניינים", "תוכן העניינים", "תוכן הענינים")
# text that is no name: an accessibility tag of a PDF ("מסמך PDF נגיש"), the word "document" alone, and letters spaced
# apart by the typesetting ("ב ס ר": a run of single-letter tokens)
BOILERPLATE_ANYWHERE = ("pdf",)
BOILERPLATE_ALONE = frozenset({"מסמך"})
LETTER_RUN_MIN = 3  # single-letter tokens in a row that make a line spaced letters (all tokens, from two)

# --- thresholds ----------------------------------------------------------------------------------------------------

PROMINENT_RATIO = 1.25  # a title line is this much taller than the file's body lines
TITLE_WORDS_MAX = 14
LAYOUT_TOP_TOLERANCE = 6.0  # points: tops of two title blocks that "repeat" a layout
LAYOUT_HEIGHT_TOLERANCE = 1.5  # points: their line heights
RUN_MAX = 8  # blocks of a title run
FRONT_MATTER_PAGES_MAX = 4  # a part before the first appraisal longer than this is a document, not a cover
FURNITURE_MIN_PAGES = 3
FURNITURE_SHARE = 0.5  # a text on this share of the pages (and at least FURNITURE_MIN_PAGES) is a running header
TOC_PAGES = 4  # pages after the TOC heading read for entries
TOC_TABLE_PAGES = 15  # tables on these first pages bring their text (a TOC set as a table)
TABLE_DOMINANT = 0.5
CHAPTERS_MIN = 2
LABEL_RESTART_AT = 1
PAGE_NUMBER_ZONE = 0.15  # share of the page height, at its top and bottom, where a page-number block may sit
PAGE_NUMBER_CHARS = 90  # a page-number block is short (a file path may precede the number)
EDGE_LINES = (3, 8)  # lines read at the start and at the end of a page's stored text for a page-number line
TEXT_KEPT = 600  # characters of a block's text read for the derivation

_TEXT_KINDS = ("paragraph", "textbox")
_READ = ("read", "read_uncertain")
_PUNCT = contexts.NAME_PUNCT  # the one punctuation set names are compared with
_QUOTED = re.compile(r"(?<![\wא-ת])[\"“״]([^\"“”״\n]{2,60}?)[\"”״](?![א-ת\w])")
_TOC_ENTRY = re.compile(r"^(?P<title>.{3,}?)(?:\s*[.·…_\-–—]{2,}\s*|\s{3,}|\s*\|\s*)(?P<page>\d{1,4})\s*$")
_NUMBERING = re.compile(r"^\s*\d{1,3}(?:\.\d+)*[.)]?\s+")
_LABEL_NUMBER = re.compile(r"(\d{1,4})\s*$")
# a page-number line: a bare number ("12", "- 12 -"), or the page word and its number at the END of the line, after
# anything (a footer may open with a file path whose digits are not the page): "עמוד 3", "עמוד 3 מתוך 10", "עמ׳ 3",
# "Page 3", or the number before the word alone on the line ("3 עמוד")
_PAGE_WORD_THEN_NUMBER = re.compile(
    r"(?:עמוד|עמ['׳’]?|page)\s*[:.]?\s*(\d{1,4})(?:\s*(?:מתוך|מ-|of|/)\s*\d{1,4})?[\s.\-–—]*$", re.IGNORECASE)
_NUMBER_THEN_PAGE_WORD = re.compile(r"^[\s\-–—]*(\d{1,4})\s*(?:עמוד|עמ['׳’]?|page)[\s.\-–—]*$", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"^[\s\-–—]*(\d{1,4})[\s\-–—]*$")
_NAME_CUT = re.compile(r"[,:;()|—–]|\s-\s")

# --- the unit ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Unit:
    """One unit of a reading. ``certainty`` is that of the boundary that opens it (the first unit of a file starts at
    the file's start: ``certain``; a file with no cut is one ``unsplit`` unit); a unit's end is as certain as the
    next unit's start."""

    unit_no: int
    kind: str
    title: str | None
    aliases: tuple[str, ...]
    identifiers: dict
    page_first: int | None
    page_last: int | None
    block_first: int
    block_last: int
    certainty: str
    evidence: dict = field(default_factory=dict)

    def pages_label(self) -> str:
        if self.page_first is None:
            return "pages ?"
        return f"page {self.page_first}" if self.page_first == self.page_last else (
            f"pages {self.page_first}-{self.page_last}")


# --- helpers over rows ---------------------------------------------------------------------------------------------


def _text(r) -> str:
    return (getattr(r, "text", None) or "").strip()


def _norm(s: str) -> str:
    return " ".join(_PUNCT.sub(" ", base_normalize(s)).split())


def _words(s: str) -> int:
    return len(s.split())


def _line_height(r) -> float | None:
    """The height of a one-line paragraph, from its stored box; None for anything else."""
    if r.kind not in _TEXT_KINDS or getattr(r, "table_index", None) is not None or not getattr(r, "bbox", None):
        return None
    t = _text(r)
    if not t or "\n" in t or len(t) >= TEXT_KEPT:
        return None
    try:
        box = [float(x) for x in r.bbox]
    except (TypeError, ValueError):
        return None
    height = box[3] - box[1] if len(box) >= 4 else 0.0
    return height if 0 < height < 80 else None


def _top(r) -> float | None:
    try:
        return float(r.bbox[1])
    except (TypeError, ValueError, IndexError):
        return None


def _title_shaped(t: str) -> bool:
    return bool(t) and _words(t) <= TITLE_WORDS_MAX and not t.rstrip().endswith(".")


def _is_text(r) -> bool:
    return r.kind in (*_TEXT_KINDS, "heading") and getattr(r, "table_index", None) is None and bool(_text(r)) and (
        (getattr(r, "status", None) or "read") in _READ)


def _picture_title(r, furniture_images: frozenset) -> bool:
    """A picture whose reading is text, outside page furniture: weaker title evidence."""
    if r.kind != "image" or (getattr(r, "status", None) or "read") not in _READ or not _text(r):
        return False
    digest = (getattr(r, "content_hash", None) or "").removeprefix("ink:")
    return not any(digest.startswith(h.removeprefix("ink:")) for h in furniture_images if h)


# --- page numbers ---------------------------------------------------------------------------------------------------


def line_page_number(line: str) -> int | None:
    """The page number a line states: a bare number, or the number that ends the line after the page word, whatever
    stands before it (the digits of a file path in a footer are not the page). None for any other line."""
    t = hebrew.strip_bidi_controls(line).strip()
    if not t or len(t) > PAGE_NUMBER_CHARS:
        return None
    for pattern in (_BARE_NUMBER, _NUMBER_THEN_PAGE_WORD, _PAGE_WORD_THEN_NUMBER):
        m = pattern.search(t)
        if m:
            return int(m.group(1))
    return None


def footer_number(edge: str) -> int | None:
    """The number of the page-number line among the first and last lines of a page's stored text (``EDGE_LINES``:
    other lines may follow the number line)."""
    lines = [ln.strip() for ln in edge.splitlines() if ln.strip()]
    found = {n for ln in [*lines[:EDGE_LINES[0]], *lines[-EDGE_LINES[1]:]] if (n := line_page_number(ln)) is not None}
    return found.pop() if len(found) == 1 else None


def _block_numbers(blocks: Iterable) -> dict[int, int]:
    """The printed number of each page from its blocks, by position: a short one-line block in the top or bottom band
    of the page that is a page-number line ("עמוד 7", "- 7 -"), wherever it falls in the reading order. The page
    height is that of the lowest block of the file (the stored boxes are in one frame)."""
    rows = [r for r in blocks if r.page is not None and r.kind in _TEXT_KINDS and getattr(r, "bbox", None)
            and getattr(r, "table_index", None) is None]
    bottoms = []
    for r in rows:
        try:
            bottoms.append(float(r.bbox[3]))
        except (TypeError, ValueError, IndexError):
            pass
    if not bottoms:
        return {}
    height = max(bottoms)
    found: dict[int, set[int]] = defaultdict(set)
    for r in rows:
        top = _top(r)
        if top is None or not (top <= PAGE_NUMBER_ZONE * height or top >= (1 - PAGE_NUMBER_ZONE) * height):
            continue
        t = _text(r)
        if not t or "\n" in t or (n := line_page_number(t)) is None:
            continue
        found[r.page].add(n)
    return {page: next(iter(ns)) for page, ns in found.items() if len(ns) == 1}


def page_numbers(pages: Iterable, blocks: Iterable = ()) -> dict[int, int]:
    """The printed number of each page: its stored label when it ends in a number; else the page-number block in the
    top or bottom band of the page; else the page-number line among the first and last lines of its stored text."""
    by_block = _block_numbers(blocks)
    out: dict[int, int] = {}
    for p in pages:
        label = (getattr(p, "printed_label", None) or "").strip()
        m = _LABEL_NUMBER.search(label) if label else None
        if m:
            n = int(m.group(1))
        elif label:
            n = None  # a label that is not a number (roman, lettered) says nothing here
        else:
            n = by_block.get(p.page_no)
            if n is None:
                n = footer_number(getattr(p, "edge", None) or "")
        if n is not None:
            out[p.page_no] = n
    return out


def label_restarts(numbers: dict[int, int]) -> dict[int, dict]:
    """File pages where a new numbering starts, with the pages that show it. A numbered page whose number is below the
    previous numbered page's (any drop; a second page numbered 1 too) ends the old sequence: the boundary is the
    first page after the old sequence's last numbered page, which is the numbered page itself or, when unnumbered
    pages (a cover) lie between, the first of them — a new report's cover pages come before its first printed
    number."""
    out: dict[int, dict] = {}
    prev_page = prev_n = None
    for page in sorted(numbers):
        n = numbers[page]
        if prev_n is not None and (n < prev_n or (n == prev_n == LABEL_RESTART_AT)):
            out[prev_page + 1 if prev_page + 1 < page else page] = {
                "rule": "number_drop", "previous_page": prev_page, "numbered_page": page}
        prev_page, prev_n = page, n
    return out


# --- table of contents ----------------------------------------------------------------------------------------------


def _toc_entries(rows: Sequence) -> list[tuple[str, int]]:
    """(normalized title, page) of the entries of a table of contents: the lines after a "תוכן עניינים" line that end
    in a page number after dot leaders, spaces or a table cell boundary."""
    entries: list[tuple[str, int]] = []
    for i, r in enumerate(rows):
        if r.kind == "table" or not _is_text(r) or _norm(_text(r)) not in TOC_WORDS:
            continue
        misses, page0 = 0, r.page or 0
        for x in rows[i + 1:]:
            if x.page is not None and x.page > page0 + TOC_PAGES:
                break
            found = 0
            for line in _text(x).splitlines():
                m = _TOC_ENTRY.match(line.strip())
                if m:
                    title = _norm(_NUMBERING.sub("", m.group("title")))
                    if title:
                        entries.append((title, int(m.group("page"))))
                        found += 1
            misses = 0 if found else misses + 1
            if misses >= 3:
                break
    return entries


def is_boilerplate(name: str) -> bool:
    """Whether a title or an alias names nothing: spaced single letters, a PDF accessibility tag, "מסמך" alone."""
    tokens = _norm(name).split()
    if not tokens:
        return True
    if len(tokens) == 1 and tokens[0] in BOILERPLATE_ALONE:
        return True
    if any(w in t for t in tokens for w in BOILERPLATE_ANYWHERE):
        return True
    single = [len(t) == 1 and t.isalpha() for t in tokens]
    if len(tokens) >= 2 and all(single):
        return True
    run = 0
    for one in single:
        run = run + 1 if one else 0
        if run >= LETTER_RUN_MIN:
            return True
    return False


def _same_title(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)))


# --- title runs ------------------------------------------------------------------------------------------------------


@dataclass
class _Ctx:
    """What the derivation knows about the whole file."""

    rows: list
    body: float | None  # the median line height of one-line paragraphs
    furniture: frozenset  # normalized texts of running headers
    furniture_images: frozenset


def _run(c: _Ctx, start: int) -> list:
    """The title run opened by the block at position ``start`` of ``c.rows``: the blocks from it to the first heading
    or table (at most ``RUN_MAX``), on its page; running headers, repeated pictures and pictures not read are
    skipped."""
    first = c.rows[start]
    out: list = []
    for r in islice(c.rows, start, None):
        if first.page is not None and r.page != first.page:
            break
        if r.kind in ("heading", "table") or getattr(r, "table_index", None) is not None:
            break
        if r.kind == "image":
            if _picture_title(r, c.furniture_images):
                out.append(r)
        elif _is_text(r) and _norm(_text(r)) not in c.furniture:
            out.append(r)
        if len(out) >= RUN_MAX:
            break
    return out


def _lead(c: _Ctx, run: list):
    """The line a title run opens with when it is a title: a prominent one-line paragraph, or a picture read as text.
    A run needs another line besides it to be a title block."""
    if len(run) < 2:
        return None
    r = run[0]
    if r.kind == "image":
        return r
    h = _line_height(r)
    if h is not None and c.body is not None and h >= PROMINENT_RATIO * c.body and _title_shaped(_text(r)):
        return r
    return None


def _same_layout(a, b) -> bool:
    if a.kind != b.kind:
        return False
    ta, tb = _top(a), _top(b)
    if ta is None or tb is None or abs(ta - tb) > LAYOUT_TOP_TOLERANCE:
        return False
    if a.kind == "image":
        return True
    ha, hb = _line_height(a), _line_height(b)
    return ha is not None and hb is not None and abs(ha - hb) <= LAYOUT_HEIGHT_TOLERANCE


# --- boundaries ------------------------------------------------------------------------------------------------------


@dataclass
class _Cut:
    block: int
    page: int | None
    signals: set = field(default_factory=set)
    detail: dict = field(default_factory=dict)


def _cuts(c: _Ctx, pages: Sequence, version_id: str) -> list[_Cut]:
    rows = c.rows
    first_block = rows[0].block_index
    position = {r.block_index: i for i, r in enumerate(rows)}
    lead_at: dict[int, int] = {}  # page -> position of its first block
    for i, r in enumerate(rows):
        if r.page is not None:
            lead_at.setdefault(r.page, i)
    numbers = page_numbers(pages, rows)
    restart_at: dict[int, dict] = {}  # page that has blocks -> the pages that show the restart
    for page, detail in label_restarts(numbers).items():
        resolved = next((q for q in sorted(lead_at) if q >= page), None)
        if resolved is not None:
            restart_at.setdefault(resolved, detail)
    toc = _toc_entries(rows)
    cuts: dict[int, _Cut] = {}  # by block

    def add(block: int, page: int | None, signal: str, detail: dict | None = None) -> None:
        cut = cuts.setdefault(block, _Cut(block, page))
        cut.signals.add(signal)
        if detail:
            cut.detail[signal] = detail

    ref = None  # the file's first title block (its lead line)
    for page in sorted(lead_at):
        pos = lead_at[page]
        if rows[pos].block_index == first_block:
            run = _run(c, pos)
            ref = ref or _lead(c, run)
            continue
        block = rows[pos].block_index
        run = _run(c, pos)
        lead = _lead(c, run)
        if page in restart_at:
            add(block, page, LABEL_RESTART, restart_at[page])
        if lead is not None and ref is not None and _same_layout(lead, ref):
            add(block, page, TITLE_BLOCK_REPEAT)
        if lead is not None and toc:
            title = _norm(_text(lead))
            if any(_same_title(title, t) and n in (page, numbers.get(page)) for t, n in toc):
                add(block, page, TOC_ENTRY)
        if ref is None and lead is not None:
            ref = lead
    # the restated-identifier rule of the appraisal contexts: a new context opens a unit
    for seg in contexts.derive(rows, version_id).segments[1:]:
        s = seg.first
        page = rows[position[s]].page if s in position else None
        lead_pos = lead_at.get(page) if page is not None else None
        if lead_pos is not None and all(r.kind == "image" for r in rows[lead_pos:position[s]]):
            s = rows[lead_pos].block_index  # the title block starts at the page's top (a picture may precede it)
        if s > first_block:
            add(s, page, IDENTIFIER_RESTART)
    # a repeated layout alone cuts nothing
    return [cuts[b] for b in sorted(cuts) if cuts[b].signals - WEAK_SIGNALS]


# --- identity ------------------------------------------------------------------------------------------------------


def _project_names(line: str) -> list[str]:
    """The names a line gives to a project: the words after a project word, up to a separator, a number or a word
    that names nothing."""
    out: list[str] = []
    for word in PROJECT_WORDS:
        for m in re.finditer(rf"(?<![א-ת])[והבלמשכ]?{word}\s+(\S.*)", line):
            tail = _NAME_CUT.split(m.group(1), maxsplit=1)[0].split()
            name: list[str] = []
            for tok in tail:
                if tok in NAME_STOP_WORDS or (re.search(r"\d", tok) and len(tok) > 1) or len(name) >= 4:
                    break
                name.append(tok)
            if name and len(" ".join(name)) >= 3:
                out.append(" ".join(name))
    return out


def _opens_with_project_word(line: str) -> bool:
    return any(re.match(rf"[והבלמשכ]?{w}\s", line.lstrip()) for w in PROJECT_WORDS)


def _alias_candidates(line: str, naming: bool) -> list[tuple[str, str]]:
    """(alias, rule) of a title-block line: quoted names, addresses and, for a line that names the unit (its title, or
    a line that opens with a project word: ``naming``), project phrases (with and without the project word, and
    without a leading place word). A neighbourhood line ("שכונה: מרכז העיר") names nothing."""
    found: list[tuple[str, str]] = []
    for m in _QUOTED.finditer(line):
        found.append((m.group(1), "quoted_name"))
    for name in _project_names(line) if naming else ():
        word = next((w for w in PROJECT_WORDS if re.search(rf"(?<![א-ת])[והבלמשכ]?{w}\s+{re.escape(name)}", line)), "")
        found.append((name, "project_phrase"))
        if word:
            found.append((f"{word} {name}", "project_phrase"))
        head, _, rest = name.partition(" ")
        if head in PLACE_WORDS and len(rest) >= 3:
            found.append((rest, "project_phrase"))
    ident = contexts.identifiers(line)
    for shown in ident.shown:
        if not shown.startswith("גוש"):
            found.append((shown, "address"))
    for words, number, letter in ident.streets:
        found.append((f"{' '.join(words)} {number}{letter}", "address"))
    return found


def _picture_line(r) -> str | None:
    """The first line of a picture's reading when it can be a title: a short line, not a table row."""
    first = (_text(r).splitlines() or [""])[0].strip()
    return first if first and "|" not in first and _title_shaped(first) and _words(first) <= 8 else None


def _identifiers_json(ident: contexts.Identity) -> dict:
    return {"addresses": sorted([list(a) for a in ident.addresses]), "parcels": sorted([list(p) for p in ident.parcels]),
            "shown": list(ident.shown)}


def _identity(c: _Ctx, run: list, letterhead: set) -> tuple[str | None, list[str], list[dict], dict, dict]:
    """(title, aliases, alias evidence, identifiers, title evidence) of a unit from its title run."""
    lines = [r for r in run if r.kind != "image" and _norm(_text(r)) not in letterhead]
    pictures = [r for r in run if r.kind == "image"]
    title, title_ev = None, {"rule": "none"}
    sized = [(h, i) for i, r in enumerate(lines) if (h := _line_height(r)) is not None and c.body is not None
             and h >= PROMINENT_RATIO * c.body and _title_shaped(_text(r))]
    if sized:
        _, i = max(sized, key=lambda x: (x[0], -x[1]))
        title, title_ev = _text(lines[i]), {"rule": "prominent_title_line", "block": lines[i].block_index}
    elif (pic := next((r for r in pictures if _picture_line(r) and _norm(_picture_line(r)) not in letterhead),
                      None)) is not None:
        title, title_ev = _picture_line(pic), {"rule": "image_title", "block": pic.block_index, "weak": True}
    elif c.body is None and lines and _title_shaped(_text(lines[0])) and "\n" not in _text(lines[0]):
        title, title_ev = _text(lines[0]), {"rule": "first_short_line", "block": lines[0].block_index}
    if title is not None and is_boilerplate(title):
        title, title_ev = None, {"rule": "none"}
    aliases: list[str] = []
    evidence: list[dict] = []
    ident = contexts.Identity()

    def keep(raw: str, rule: str, block: int, weak: bool) -> None:
        alias = _norm(raw)
        if len(alias) >= 3 and not alias.replace(" ", "").isdigit() and alias not in aliases and not is_boilerplate(alias):
            aliases.append(alias)
            evidence.append({"block": block, "rule": rule} | ({"weak": True} if weak else {}))

    for r in lines:
        ident = ident | contexts.identifiers(_text(r))
        naming = title_ev.get("block") == r.block_index or _opens_with_project_word(_text(r))
        for raw, rule in _alias_candidates(_text(r), naming):
            keep(raw, rule, r.block_index, False)
    for r in pictures:
        first = _picture_line(r)
        if first is None or _norm(first) in letterhead:
            continue
        for raw, rule in _alias_candidates(first, True):
            keep(raw, "image_title" if rule == "project_phrase" else rule, r.block_index, True)
        if _title_shaped(first):
            keep(first, "image_title", r.block_index, True)
    return title, aliases, evidence, _identifiers_json(ident), title_ev


# --- kind --------------------------------------------------------------------------------------------------------------


def _numbered_chapters(rows: Sequence) -> list[int]:
    return [r.block_index for r in rows if contexts._top_number(r) is not None]


def _kind(rows: Sequence, run: list, title: str | None, letterhead: set) -> tuple[str, dict]:
    """The kind of a unit from its structure: (kind, evidence). ``rows`` are the unit's blocks."""
    lead_heading = next((r for r in rows if r.kind == "heading"), None)
    marks = [(r.block_index, _norm(_text(r))) for r in run if r.kind != "image" and _norm(_text(r)) not in letterhead]
    if title:
        marks.insert(0, (-1, _norm(title)))
    if lead_heading is not None:
        marks.append((lead_heading.block_index, _norm(_text(lead_heading))))
    for block, t in marks:
        if any(t == w or t.startswith(f"{w} ") or t.startswith(f"{w}:") for w in APPENDIX_WORDS):
            return APPENDIX, {"rule": "appendix_marker"} | ({"block": block} if block >= 0 else {})
    chapters = _numbered_chapters(rows)
    valuation = next((r.block_index for r in rows if r.kind == "heading"
                      and any(w in _norm(_text(r)) for w in VALUATION_WORDS)), None)
    tables = sum(1 for r in rows if r.kind == "table")
    if rows and tables and tables / len(rows) >= TABLE_DOMINANT and len(chapters) < CHAPTERS_MIN:
        return COMPARABLES, {"rule": "table_dominant", "tables": tables, "blocks": len(rows)}
    if len(chapters) < CHAPTERS_MIN and any(w in t for _, t in marks for w in COMPARABLES_WORDS):
        return COMPARABLES, {"rule": "comparables_heading"}
    if len(chapters) >= CHAPTERS_MIN:
        return APPRAISAL, {"rule": "numbered_chapters", "blocks": chapters[:3]}
    if valuation is not None:
        return APPRAISAL, {"rule": "valuation_heading", "block": valuation}
    return DOCUMENT, {"rule": "no_kind_evidence"}


# --- derivation ----------------------------------------------------------------------------------------------------------


def derive(blocks: Iterable, pages: Iterable = (), version_id: str = "", furniture_images: Iterable[str] = ()
           ) -> tuple[Unit, ...]:
    """The units of a version's blocks (``block_index``, ``kind``, ``page``, ``bbox``, ``text``, ``status``,
    ``table_index``, ``label``, ``content_hash`` and the page geometry ``contexts.derive`` reads) and pages
    (``page_no``, ``printed_label``, ``edge``: the start and end of the page's text), in reading order. A file
    without a boundary is one ``document`` unit."""
    rows = sorted(blocks, key=lambda r: r.block_index)
    pages = list(pages)
    if not rows:
        return (Unit(1, DOCUMENT, None, (), {}, None, None, 0, 0, UNSPLIT, {"boundary": {"rule": "file_start"}}),)
    heights = [h for r in rows if (h := _line_height(r)) is not None]
    body = statistics.median(heights) if len(heights) >= 3 else None
    per_text: dict[str, set] = defaultdict(set)
    for r in rows:
        if r.page is not None and r.kind in _TEXT_KINDS and _text(r) and getattr(r, "table_index", None) is None:
            per_text[_norm(_text(r))].add(r.page)
    pages_seen = len({r.page for r in rows if r.page is not None})
    need = max(FURNITURE_MIN_PAGES, math.ceil(FURNITURE_SHARE * pages_seen))
    furniture = frozenset(t for t, ps in per_text.items() if t and len(ps) >= need)
    c = _Ctx(rows, body, furniture, frozenset(furniture_images))
    cuts = _cuts(c, pages, version_id)
    starts = [(rows[0].block_index, None, None, {})] + [(x.block, x.page, x.signals, x.detail) for x in cuts]
    ranges = []
    for n, (first, page, signals, detail) in enumerate(starts):
        last = starts[n + 1][0] - 1 if n + 1 < len(starts) else rows[-1].block_index
        ranges.append((first, last, page, signals, detail))
    indexes = [r.block_index for r in rows]
    position = {b: i for i, b in enumerate(indexes)}
    runs = [_run(c, position[first]) for first, _, _, _, _ in ranges]
    # lines that open several units alike are a letterhead, never a title or an alias
    seen: dict[str, int] = defaultdict(int)
    for run in runs:
        for t in {_norm(_picture_line(r) or "") if r.kind == "image" else _norm(_text(r)) for r in run} - {""}:
            seen[t] += 1
    letterhead = {t for t, k in seen.items() if k >= 2} if len(runs) >= 2 else set()
    units: list[Unit] = []
    for n, ((first, last, page, signals, detail), run) in enumerate(zip(ranges, runs, strict=True), start=1):
        own = rows[bisect_left(indexes, first):bisect_right(indexes, last)]
        title, aliases, alias_ev, identifiers, title_ev = _identity(c, run, letterhead)
        kind, kind_ev = _kind(own, run, title, letterhead)
        if len(ranges) == 1:
            kind, certainty = DOCUMENT, UNSPLIT
            boundary = {"rule": "file_start", "signals": []}
        elif n == 1:
            certainty, boundary = CERTAIN, {"rule": "file_start", "signals": []}
        else:
            certainty = CERTAIN if len(signals) >= CERTAIN_SIGNALS else PROBABLE
            boundary = {"block": first, "signals": [s for s in SIGNALS if s in signals]} | (
                {"page": page} if page is not None else {}) | ({"detail": detail} if detail else {})
        units.append(Unit(n, kind, title, tuple(aliases), identifiers, None, None, first, last, certainty,
                          {"boundary": boundary, "title": title_ev, "aliases": alias_ev, "kind": kind_ev}))
    # a short part before the first appraisal that holds no chapters of its own is front matter; a long one stays a
    # document (it may hold anything)
    first_appraisal = next((n for n, u in enumerate(units) if u.kind == APPRAISAL), None)
    for n in range(first_appraisal or 0):
        u = units[n]
        pgs = [r.page for r in rows[bisect_left(indexes, u.block_first):bisect_right(indexes, u.block_last)]
               if r.page is not None]
        if u.kind == DOCUMENT and (not pgs or max(pgs) - min(pgs) + 1 <= FRONT_MATTER_PAGES_MAX):
            units[n] = dc_replace(u, kind=FRONT_MATTER,
                                  evidence=u.evidence | {"kind": {"rule": "before_first_appraisal"}})
    first_on_page: dict[int, int] = {}
    for r in rows:
        if r.page is not None:
            first_on_page.setdefault(r.page, r.block_index)
    out = []
    for n, u in enumerate(units):
        pgs = [p for r in rows[bisect_left(indexes, u.block_first):bisect_right(indexes, u.block_last)]
               for p in (r.page, getattr(r, "page_end", None)) if p is not None]  # a table may run on to a later page
        first, last = (min(pgs), max(pgs)) if pgs else (None, None)
        if last is not None and n + 1 < len(units):
            # a table merged across two units stays on the page before the next unit when that unit starts at the
            # top of its page
            nxt = rows[position[units[n + 1].block_first]]
            if nxt.page is not None:
                top_of_page = first_on_page.get(nxt.page) == nxt.block_index
                last = max(first or 0, min(last, nxt.page - 1 if top_of_page else nxt.page))
        out.append(dc_replace(u, page_first=first, page_last=last))
    return tuple(out)


# --- the stored reading ---------------------------------------------------------------------------------------------------

_ROWS_SQL = (
    "SELECT b.block_index, b.kind, b.page, b.bbox, b.status, b.table_index, b.label, b.content_hash,"
    " t.page_end,"
    " CASE WHEN b.kind <> 'table' AND b.table_index IS NULL THEN left(b.text, :keep)"
    " WHEN b.kind = 'table' AND b.page <= :toc THEN left(b.text, :keep) END AS text,"
    " p.mediabox, p.cropbox, p.rotation, p.display_width, p.display_height, p.geometry_issue"
    " FROM document_blocks b LEFT JOIN pages p ON p.version_id = b.version_id AND p.page_no = b.page"
    " LEFT JOIN extracted_tables t ON t.version_id = b.version_id AND t.table_index = b.table_index"
    " WHERE b.version_id = :v ORDER BY b.block_index")
_PAGES_SQL = ("SELECT page_no, printed_label, left(text, 600) || E'\\n' || right(text, 600) AS edge FROM pages"
              " WHERE version_id = :v ORDER BY page_no")
NO_READING = ""  # the reading id of a version whose report names none


@dataclass(frozen=True)
class Reading:
    """What a version's units are derived from: its reading and its derivation key."""

    document_id: UUID
    reading_id: str
    key: str
    furniture_images: tuple[str, ...]

    @property
    def tag(self) -> str | None:
        """The tag of the derivation ``key`` names (``tag_of``)."""
        return tag_of(self.key.split("|", 1)[0])


def derivation_key(ingestion: dict | None) -> str:
    """``UNITS_VERSION`` plus the reading's positions marker: a positions backfill changes the pages' labels, so it
    changes the key and the units are derived again."""
    return f"{UNITS_VERSION}|{(ingestion or {}).get('positions') or '-'}"


def reading_of(conn: Connection, version_id: UUID | str) -> Reading | None:
    """The version's reading, or None for a version that does not exist (to the caller) or whose document is not an
    appraisal: units are an appraisal notion, so a standard or a professional document gets none, built or stored
    (round 10 U3, KTD9). Every builder (``ensure``, ``replace``, ``rebuild_if_stale``) starts here."""
    row = conn.execute(text(
        "SELECT v.document_id, v.ingestion FROM document_versions v JOIN documents d ON d.id = v.document_id"
        f" WHERE v.id = :v AND {APPRAISALS_SQL}"), {"v": UUID(str(version_id))}).first()
    if row is None:
        return None
    ing = row.ingestion or {}
    hashes = tuple(str(e["hash"]) for e in (ing.get("repeated_images") or [])
                   if isinstance(e, dict) and e.get("hash"))
    return Reading(row.document_id, str(ing.get("reading_id") or NO_READING), derivation_key(ing), hashes)


def derive_version(conn: Connection, version_id: UUID | str, reading: Reading | None = None) -> tuple[Unit, ...]:
    """Derive a version's units from its stored blocks and pages (read-only, no model, no file)."""
    reading = reading or reading_of(conn, version_id)
    v = UUID(str(version_id))
    rows = conn.execute(text(_ROWS_SQL), {"v": v, "keep": TEXT_KEPT, "toc": TOC_TABLE_PAGES}).all()
    pages = conn.execute(text(_PAGES_SQL), {"v": v}).all()
    return derive(rows, pages, str(version_id), reading.furniture_images if reading else ())


def _derived_or_whole(conn: Connection, version_id: UUID | str, reading: Reading) -> tuple[Unit, ...]:
    """The version's derived units, or its one undetermined unit when the derivation fails (logged): the same
    fallback everywhere units are built, so neither publication, a chat turn nor the count script stops on it."""
    try:
        with conn.begin_nested():
            return derive_version(conn, version_id, reading)
    except Exception:  # noqa: BLE001
        logger.exception("units of version %s could not be derived", version_id)
        return _whole_file(conn, version_id, "derivation_failed")


def _unit_of(r) -> Unit:
    return Unit(r.unit_no, r.kind, r.title, tuple(r.aliases or ()), r.identifiers or {}, r.page_first, r.page_last,
                r.block_first, r.block_last, r.certainty, r.evidence or {})


def stored(conn: Connection, version_id: UUID | str, reading: Reading | None = None) -> tuple[Unit, ...] | None:
    """The version's stored units when they are of its current reading and derivation key; None otherwise."""
    reading = reading or reading_of(conn, version_id)
    if reading is None:
        return None
    rows = conn.execute(text(
        "SELECT unit_no, kind, title, aliases, identifiers, page_first, page_last, block_first, block_last,"
        " certainty, evidence FROM document_units WHERE version_id = :v AND reading_id = :r AND derivation_key = :k"
        " ORDER BY unit_no"), {"v": UUID(str(version_id)), "r": reading.reading_id, "k": reading.key}).all()
    return tuple(_unit_of(r) for r in rows) or None


def stored_many(conn: Connection, version_ids: Sequence[UUID | str]) -> dict[UUID, tuple[Unit, ...]]:
    """The stored units of many versions in one query (those of their current reading and derivation key, as
    ``stored`` has them); a version without any is not in the result (``ensure`` builds those)."""
    if not version_ids:
        return {}
    out: dict[UUID, list[Unit]] = defaultdict(list)
    for r in conn.execute(text(
            "SELECT u.version_id, u.unit_no, u.kind, u.title, u.aliases, u.identifiers, u.page_first, u.page_last,"
            " u.block_first, u.block_last, u.certainty, u.evidence FROM document_units u"
            " JOIN document_versions v ON v.id = u.version_id WHERE u.version_id = ANY(:v)"
            " AND u.reading_id = COALESCE(v.ingestion->>'reading_id', :none)"
            " AND u.derivation_key = :uv || '|' || COALESCE(NULLIF(v.ingestion->>'positions', ''), '-')"
            " ORDER BY u.version_id, u.unit_no"),
            {"v": [UUID(str(v)) for v in version_ids], "none": NO_READING, "uv": UNITS_VERSION}):
        out[r.version_id].append(_unit_of(r))
    return {v: tuple(units) for v, units in out.items()}


def kept(conn: Connection, version_id: UUID | str, reading_id: str, unit_no: int, tag: str | None) -> Unit | None:
    """The kept row of an earlier derivation (``tag``: its ``tag_of``; None is ``units-v4``) for a unit number of a
    reading, or None when none was kept: what an anchor stored before a re-derivation stood on, read only to find the
    unit it stands for now (``entities.live_unit``, KTD6). Of several keys of that derivation (a positions backfill
    changed it) the newest. Never the current rows: those are ``stored``."""
    row = conn.execute(text(
        "SELECT unit_no, kind, title, aliases, identifiers, page_first, page_last, block_first, block_last,"
        " certainty, evidence FROM document_units WHERE version_id = :v AND reading_id = :r AND unit_no = :n"
        " AND units_version = :uv ORDER BY created_at DESC, derivation_key DESC LIMIT 1"),
        {"v": UUID(str(version_id)), "r": reading_id, "n": unit_no, "uv": version_of_tag(tag)}).first()
    return _unit_of(row) if row is not None else None


def read_or_derive(conn: Connection, version_id: UUID | str) -> tuple[Unit, ...]:
    """Stored units when they are current, else derived in memory; writes nothing (the count script)."""
    reading = reading_of(conn, version_id)
    if reading is None:
        return ()
    return stored(conn, version_id, reading) or _derived_or_whole(conn, version_id, reading)


_INSERT = (
    "INSERT INTO document_units (office_id, document_id, version_id, reading_id, unit_no, kind, title, aliases,"
    " identifiers, page_first, page_last, block_first, block_last, certainty, evidence, units_version, derivation_key)"
    " VALUES (app_office(), :d, :v, :r, :n, :k, :t, CAST(:a AS text[]), CAST(:i AS jsonb), :pf, :pl, :bf, :bl, :c,"
    " CAST(:e AS jsonb), :uv, :key) ON CONFLICT (version_id, reading_id, derivation_key, unit_no) DO NOTHING")


def _insert(conn: Connection, version_id: UUID | str, reading: Reading, units: Sequence[Unit]) -> None:
    for u in units:
        conn.execute(text(_INSERT), {
            "d": reading.document_id, "v": UUID(str(version_id)), "r": reading.reading_id, "n": u.unit_no,
            "k": u.kind, "t": u.title, "a": list(u.aliases), "i": json.dumps(u.identifiers, ensure_ascii=False),
            "pf": u.page_first, "pl": u.page_last, "bf": u.block_first, "bl": u.block_last, "c": u.certainty,
            "e": json.dumps(u.evidence, ensure_ascii=False), "uv": UNITS_VERSION, "key": reading.key})


def _whole_file(conn: Connection, version_id: UUID | str, rule: str) -> tuple[Unit, ...]:
    """The one unit a version has when its units cannot be derived: all of its blocks, undetermined, unsplit."""
    row = conn.execute(text(
        "SELECT COALESCE(min(block_index), 0) AS first, COALESCE(max(block_index), 0) AS last, min(page) AS pf,"
        " max(page) AS pl FROM document_blocks WHERE version_id = :v"), {"v": UUID(str(version_id))}).one()
    return (Unit(1, DOCUMENT, None, (), {}, row.pf, row.pl, row.first, row.last, UNSPLIT,
                 {"boundary": {"rule": "file_start", "signals": []}, "kind": {"rule": rule}}),)


def replace(conn: Connection, version_id: UUID | str) -> tuple[Unit, ...]:
    """Derive the version's units from its stored reading and write them: the rows of the same reading and derivation
    key are replaced by the new derivation, and the rows of any other key stay where they are (round 15 U3, KTD6, KD8:
    derived data is added beside the old, which is kept to resolve the anchors a conversation stored before). Called
    inside the publish transaction and the reindex swap, so a ready version always has units. A derivation that fails
    (it should not: it reads stored blocks only) leaves the version one undetermined unit rather than blocking its
    publication; the failure is logged."""
    reading = reading_of(conn, version_id)
    if reading is None:
        return ()
    conn.execute(text("DELETE FROM document_units WHERE version_id = :v AND reading_id = :r AND derivation_key = :k"),
                 {"v": UUID(str(version_id)), "r": reading.reading_id, "k": reading.key})
    units = _derived_or_whole(conn, version_id, reading)
    _insert(conn, version_id, reading, units)
    return units


def rebuild_if_stale(conn: Connection, version_id: UUID | str) -> bool:
    """Derive the units again when the stored ones are not of the version's current reading and derivation key (a
    positions backfill changed the key). Returns whether it did."""
    reading = reading_of(conn, version_id)
    if reading is None or stored(conn, version_id, reading) is not None:
        return False
    replace(conn, version_id)
    return True


# the lazy build: one derivation per (version, reading) at a time in a process, and its result kept briefly, so
# callers that arrive while the first one's transaction is open derive nothing again; across processes the primary
# key and ``ON CONFLICT DO NOTHING`` make a second write harmless
_MEMO_MAX = 64
_MEMO: OrderedDict[tuple[str, str, str], tuple[Unit, ...]] = OrderedDict()
_GUARD = threading.Lock()


def ensure(conn: Connection, version_id: UUID | str) -> tuple[Unit, ...]:
    """The version's units for use in a chat turn (the caller resolved the version under the user's permissions): the
    stored ones when current; otherwise built here, once per version and reading in the process, and stored in the
    caller's transaction (a savepoint, so a transaction that cannot write still gets its units), beside the rows of
    earlier derivations, which it leaves alone."""
    reading = reading_of(conn, version_id)
    if reading is None:
        return ()
    found = stored(conn, version_id, reading)
    if found is not None:
        return found
    vid = str(version_id)
    lock = flight("units", vid, reading.reading_id)
    try:
        with lock:
            found = stored(conn, version_id, reading)
            if found is not None:
                return found
            memo_key = (vid, reading.reading_id, reading.key)
            with _GUARD:
                units = _MEMO.get(memo_key)
            if units is None:
                units = _derived_or_whole(conn, version_id, reading)
                with _GUARD:
                    _MEMO[memo_key] = units
                    while len(_MEMO) > _MEMO_MAX:
                        _MEMO.popitem(last=False)
            try:
                with conn.begin_nested():
                    # beside the rows of any other key or reading, which stay (KTD6): none is read by mistake, since
                    # every reader filters by the version's current reading and derivation key
                    _insert(conn, version_id, reading, units)
            except Exception:  # noqa: BLE001 - a read-only transaction or a vanished version: the units still serve
                logger.info("units of version %s not stored", vid, exc_info=True)
        return units
    finally:
        landed("units", vid, reading.reading_id, lock)
