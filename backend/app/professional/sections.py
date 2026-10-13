"""The structure of a standard or a professional document, derived from its stored blocks and kept in
``document_sections`` (round 10 U4: R3, R21–R24; KTD9, KTD11).

A standard is read by its structure: a provision is only as good as the conditions and exceptions that limit it. A
*section* is a range of the document's blocks with a label, a title, a parent and a role. Everything here comes from
the document itself (R21): a label is taken from the block's own numbering or its own text, a role from the heading's
own words, a reference from the sentence that makes it. Nothing is inferred from a title, a file name or a position,
and a document with no headings is one ``preamble`` section whose uncertainty is recorded in ``evidence``.

**Derivation** (``derive``, pure — no file, no model, no stored state; R3):

- *Openers*: a ``heading`` block opens a section; so does a short paragraph shaped like an appendix line ("נספח ב' –
  טבלאות"), because a PDF reader finds only numbered headings (``extraction.chunking``) and an appendix title is no
  numbered heading. A numeric label ("2.1") gives the depth ("2.1" is level 2); a heading without one takes its depth
  from its heading path (``section_path``, the length the DOCX and PDF readers record), else it is top level. An
  appendix is always top level and closes the part before it. A section runs to the next opener of its own depth or a
  higher one, so a chapter holds its subsections' blocks; the parent is the nearest open section above it.
- *Roles*: ``chapter`` (top level), ``section`` (deeper), ``definitions`` (a heading "הגדרות"), ``appendix`` (a heading
  or line "נספח" + a letter or number), ``note`` (a heading "הערה"/"הערות", or a paragraph "הערה: …"), ``preamble``
  (text before the first heading, depth 0). Under a definitions heading each term-dash-text paragraph becomes a
  ``definition`` child holding that one block; a note paragraph is a one-block child of the section it sits in.
- *Ownership*: a sentence belongs to the innermost section that holds its block, and its limit, reference and
  modality count are recorded there and nowhere else (a chapter does not repeat what its subsections say; the reader
  ``section_text`` gathers them when the whole section is read). Headings and pictures are labels, not provisions:
  they carry no limit, reference or count.
- ``limits`` are the sentences with a condition or exception marker (one entry per sentence, the first marker), each
  with its block, the sentence capped at ``LIMIT_SENTENCE_MAX`` and ``provision_block``, the block holding the
  provision it limits: the same block when the limit follows a sentence of it, else the previous text block of the
  section. The provision is never cut: a limit is a record about blocks that stay in their section.
- ``refs`` are the mentions "סעיף N(.N)", "סעיפים N ו-N", "נספח X" (resolved to the section they name when it exists,
  inside the asking part first; otherwise ``target`` is None) and "תקן N(.N)" (an external reference with no target:
  a document that may not be in the repository is reported as a gap, never filled in; R23).
- ``modality`` counts, per section, the markers of each class below.

The marker lists classify standard text — what a provision obliges, recommends, illustrates, offers instead or
excepts. They are not question words and are never matched against a user's question.

**Digest** (``text_digest``, KTD11, R24): the sha256 of the normalized words of all text blocks in order. It uses
``base_normalize`` — the comparison normalization (no search variants), so a change to the search morphology never
changes a digest — over every line that is not page furniture (a page-number line), with a leading list mark ("2.1",
"(1)", "א.") removed from each block, since a DOCX keeps a heading's number in ``label`` and a PDF in the text. Words
are joined with one space, so the result does not depend on where a reader cut the paragraphs. Pictures are left out:
their text is a model's or an OCR's reading, which differs from one reading to the next. The same text read from a PDF
and from a DOCX has the same digest; an edited text has another.

**Store** (KTD9): ``document_sections``, keyed (version, reading, section number), written by ``ensure`` — lazily on
first use and after ingestion, single-flight per version and reading — together with ``text_digest`` in the version's
``ingestion``. Rows of an older reading or an older ``SECTIONS_VERSION`` of the same version are replaced; nothing
else is ever deleted. Only professional kinds get sections: an appraisal's structure is its units (round 9).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.db import TenantContext, tenant_tx
from app.extraction.chunking import heading_label, is_footer
from app.extraction.hebrew import strip_bidi_controls
from app.extraction.normalize_text import base_normalize
from app.platform.source_kinds import APPRAISAL

logger = logging.getLogger(__name__)

SECTIONS_VERSION = "sections-v1"
NO_READING = ""  # the reading id of a version whose ingestion report names none

CHAPTER, SECTION, DEFINITIONS, DEFINITION, APPENDIX, NOTE, PREAMBLE = (
    "chapter", "section", "definitions", "definition", "appendix", "note", "preamble")
LIMIT_SENTENCE_MAX = 400  # characters of a limit sentence kept

# --- the marker lists: they classify standard text, not user questions --------------------------------------------
REQUIREMENT_MARKERS = ("חייב", "יש ל", "נדרש", "על השמאי ל", "חובה")
RECOMMENDATION_MARKERS = ("מומלץ", "רצוי", "ראוי")
EXAMPLE_MARKERS = ("לדוגמה", "למשל")
ALTERNATIVE_MARKERS = ("לחלופין", "רשאי")
EXCEPTION_MARKERS = ("למעט", "אלא אם", "בכפוף ל", "על אף האמור", "בתנאי ש", "חריג", "לא יחול")
MODALITY_CLASSES = ("requirement", "recommendation", "example", "alternative", "exception")
_MARKERS = {"requirement": REQUIREMENT_MARKERS, "recommendation": RECOMMENDATION_MARKERS,
            "example": EXAMPLE_MARKERS, "alternative": ALTERNATIVE_MARKERS, "exception": EXCEPTION_MARKERS}


def marker_pattern(marker: str) -> re.Pattern:
    """A marker as it occurs in running Hebrew: after at most two prefix letters (ו, ה, ב, כ, ל, מ, ש), words joined by
    any whitespace. A marker that ends in a one-letter word ("יש ל", "בתנאי ש") is a prefix of the next word and takes
    its letters; any other takes an inflection ending of up to three letters ("חריגים", "נדרשת") and no more."""
    body = r"\s+".join(re.escape(w) for w in marker.split())
    tail = r"[א-ת]*" if len(marker.split()[-1]) == 1 else r"[א-ת]{0,3}(?![א-ת])"
    return re.compile(r"(?<![א-ת])[ובהכלמש]{0,2}" + body + tail)


_MODALITY_RE = {cls: tuple(marker_pattern(m) for m in markers) for cls, markers in _MARKERS.items()}
_EXCEPTION_RE = tuple(zip(EXCEPTION_MARKERS, _MODALITY_RE["exception"], strict=True))

# --- shapes ---------------------------------------------------------------------------------------------------------
_SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
_NUMERIC = re.compile(r"\d{1,3}(?:\.\d{1,3})*")
_LETTER_LABEL = re.compile(r"[א-ת]|[A-Za-z]|[ivxIVX]{1,5}")
_APPENDIX = re.compile(r"נספח\s+(?:[א-ת](?![א-ת])[׳']?|\d{1,2}(?!\d)|[A-Za-z](?![A-Za-z]))")
_APPENDIX_KEY = re.compile(r"נספח\s+([א-ת]|\d{1,2}|[A-Za-z])")
_DEFINITIONS = re.compile(r"הגדרות(?:\s+ו?מונחים)?\s*:?")
_NOTE_HEADING = re.compile(r"הער(?:ה|ות)(?:\s*\d+)?\s*:?")
_NOTE_PARAGRAPH = re.compile(r"הערה(?:\s*\d+)?\s*[:.]\s*\S")
_DEFINITION = re.compile(r"(?P<term>[^–—:.]{1,40}?)(?:\s*[–—]\s*|\s+-\s+)(?P<body>\S.*)", re.DOTALL)
_TERM_WORDS_MAX = 6
_LINE_HEADING_WORDS_MAX = 9  # an appendix line is short (``chunking.is_heading``'s bound)
_LINE_HEADING_CHARS_MAX = 80
# a list mark opening a block: "2.1", "2.1.", "(1)", "1)", "א.", "(א)"; a bare number ("15 דירות") is text
LIST_MARK = re.compile(r"^(?:\(\d{1,3}\)|\d{1,3}\)|\d{1,3}(?:\.\d{1,3})*\.|\d{1,3}(?:\.\d{1,3})+|[א-ת]\.|\([א-ת]\))\s+")
_KEYWORD = re.compile(r"(?<![א-ת])[ובלמהשכ]{0,3}(?P<kw>סעיפים|סעיף|נספח|תקן)(?![א-ת])")
_NUM = r"\d+(?:\.\d+)*"
_SECTION_ARG = re.compile(rf"\s+(?P<args>{_NUM}(?:\s*(?:,|ו-?|או)\s*{_NUM})*)")
_APPENDIX_ARG = re.compile(r"\s+(?P<label>(?:[א-ת](?![א-ת])[׳']?|\d{1,2}(?!\d)|[A-Za-z](?![A-Za-z])))")
_STANDARD_ARG = re.compile(r"\s+(?:שמאי\s+)?(?:(?:מס['׳]|מספר)\s*)?(?P<num>\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class Section:
    """One stored section. ``limits``, ``refs``, ``modality`` and ``evidence`` are the JSON the table holds."""

    section_no: int
    label: str | None
    title: str | None
    parent_no: int | None
    depth: int
    role: str
    block_first: int
    block_last: int
    page_first: int | None
    page_last: int | None
    limits: tuple[dict, ...] = ()
    refs: tuple[dict, ...] = ()
    modality: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)


# --- derivation -----------------------------------------------------------------------------------------------------

@dataclass
class _Row:
    index: int
    kind: str
    text: str
    label: str | None
    path: list[str]
    page: int | None


def _clean(value: str | None) -> str:
    return strip_bidi_controls(value or "").strip()


def _rows(blocks: Iterable) -> list[_Row]:
    return [_Row(int(b.block_index), b.kind, _clean(b.text), getattr(b, "label", None),
                 list(getattr(b, "section_path", None) or []), getattr(b, "page", None)) for b in blocks]


def _norm_label(raw: str | None) -> str | None:
    """A numbering label as the document shows it ("2.1", "א"), without the trailing dot or the brackets a list
    numbering adds ("1.", "1)", "(א)"); None for anything that is no label (a bullet, a long string)."""
    s = _clean(raw).strip("()").rstrip(".)").strip()
    return s if s and (_NUMERIC.fullmatch(s) or _LETTER_LABEL.fullmatch(s)) else None


def _depth_of(label: str | None) -> int | None:
    return label.count(".") + 1 if label and _NUMERIC.fullmatch(label) else None


def _short_line(value: str) -> bool:
    return (len(value) <= _LINE_HEADING_CHARS_MAX and len(value.split()) <= _LINE_HEADING_WORDS_MAX
            and not value.endswith((".", ",", ";", ":")) and "\n" not in value)


@dataclass
class _Builder:
    role: str
    label: str | None
    title: str | None
    depth: int
    first: int
    parent: _Builder | None
    evidence: dict
    last: int | None = None
    limits: list[dict] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)
    modality: dict = field(default_factory=lambda: dict.fromkeys(MODALITY_CLASSES, 0))
    last_text_block: int | None = None  # the previous text block of this section (for ``provision_block``)
    no: int = 0

    @property
    def root(self) -> _Builder:
        b = self
        while b.parent is not None:
            b = b.parent
        return b


def _opener(row: _Row) -> _Builder | None:
    """The section a block opens, or None for a block that is content."""
    if row.kind == "heading":
        label = _norm_label(row.label) or heading_label(row.text)
        label_from = "block_label" if _norm_label(row.label) else ("text" if label else None)
        title = _strip_label(row.text, label) or None
        body = title or ""
        if _APPENDIX.match(body):
            key = _APPENDIX.match(body).group(0)
            return _Builder(APPENDIX, key, title, 1, row.index, None,
                            {"opened_by": "heading", "block": row.index, "label_from": "text"})
        depth, depth_from = _depth_of(label), "label"
        if depth is None:
            depth, depth_from = (len(row.path), "section_path") if row.path else (1, "default")
        role = (DEFINITIONS if _DEFINITIONS.fullmatch(body) else NOTE if _NOTE_HEADING.fullmatch(body)
                else CHAPTER if depth == 1 else SECTION)
        return _Builder(role, label, title, depth, row.index, None,
                        {"opened_by": "heading", "block": row.index, "label_from": label_from,
                         "depth_from": depth_from})
    if row.kind in ("paragraph", "textbox") and _short_line(row.text):
        m = _APPENDIX.match(row.text)
        if m:
            return _Builder(APPENDIX, m.group(0), row.text, 1, row.index, None,
                            {"opened_by": "paragraph_shape", "block": row.index, "label_from": "text"})
    return None


def _strip_label(title: str, label: str | None) -> str:
    """``title`` without its leading numbering ("2.1 מדידה" -> "מדידה"); a DOCX heading carries none."""
    if label:
        m = re.match(rf"\(?{re.escape(label)}[.)]?\s+", title)
        if m:
            return title[m.end():].strip()
    return title


def _definition(row: _Row) -> str | None:
    """The term of a term-dash-text paragraph, or None."""
    if row.kind not in ("paragraph", "textbox"):
        return None
    m = _DEFINITION.fullmatch(row.text)
    if not m:
        return None
    term = m.group("term").strip()
    return term if term and len(term.split()) <= _TERM_WORDS_MAX and not term.endswith((",", ";")) else None


def sentences(value: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(value) if s.strip()]


def _first_marker(sentence: str) -> str | None:
    found = [(m.start(), marker) for marker, pattern in _EXCEPTION_RE if (m := pattern.search(sentence))]
    return min(found)[1] if found else None


def _scan(owner: _Builder, row: _Row) -> None:
    """Record the limits, references and modality counts of one text block in its owner."""
    for cls, patterns in _MODALITY_RE.items():
        owner.modality[cls] += sum(len(p.findall(row.text)) for p in patterns)
    seen_here = False
    for sentence in sentences(row.text):
        marker = _first_marker(sentence)
        if marker is not None:
            provision = row.index if seen_here or owner.last_text_block is None else owner.last_text_block
            owner.limits.append({"block": row.index, "sentence": sentence[:LIMIT_SENTENCE_MAX], "marker": marker,
                                 "provision_block": provision})
        seen_here = True
    owner.last_text_block = row.index
    for m in _KEYWORD.finditer(row.text):
        owner.refs.extend(_refs_at(row, m))


def _refs_at(row: _Row, m: re.Match) -> list[dict]:
    kw, start, end = m.group("kw"), m.start("kw"), m.end()
    if kw in ("סעיף", "סעיפים"):
        arg = _SECTION_ARG.match(row.text, end)
        if not arg:
            return []
        labels = re.findall(_NUM, arg.group("args"))
        if kw == "סעיף":
            labels = labels[:1]
        return [{"type": "section", "label": label, "target": None, "block": row.index,
                 "text": row.text[start:arg.end()] if len(labels) == 1 else f"{kw} {label}"} for label in labels]
    if kw == "נספח":
        arg = _APPENDIX_ARG.match(row.text, end)
        return [{"type": "appendix", "label": f"נספח {arg.group('label')}", "target": None, "block": row.index,
                 "text": row.text[start:arg.end()]}] if arg else []
    arg = _STANDARD_ARG.match(row.text, end)
    return [{"type": "standard", "standard": arg.group("num"), "target": None, "block": row.index,
             "text": row.text[start:arg.end()]}] if arg else []


def _appendix_key(label: str) -> str | None:
    m = _APPENDIX_KEY.match(label)
    return m.group(1) if m else None


def derive(blocks: Iterable) -> tuple[Section, ...]:
    """The sections of one reading's blocks (see the module docstring); () when no block holds text. ``blocks`` are
    the stored ``document_blocks`` rows (or anything with the same attributes): ``block_index``, ``kind``, ``label``,
    ``section_path``, ``page`` and ``text``."""
    all_rows = _rows(blocks)
    rows = [r for r in all_rows if r.text and r.kind != "image"]
    if not rows:
        return ()
    pages = {r.index: r.page for r in all_rows}
    first_index, last_index = min(pages), max(pages)
    out: list[_Builder] = []
    stack: list[_Builder] = []
    preamble: _Builder | None = None
    for row in rows:
        opener = _opener(row)
        if opener is not None:
            if preamble is not None and preamble.last is None:
                preamble.last = row.index - 1
            while stack and stack[-1].depth >= opener.depth:
                stack.pop().last = row.index - 1
            opener.parent = stack[-1] if stack else None
            stack.append(opener)
            out.append(opener)
            continue
        owner = stack[-1] if stack else None
        if owner is None:
            if preamble is None:
                preamble = _Builder(PREAMBLE, None, None, 0, first_index, None,
                                    {"structure": {"rule": "before_first_heading", "certain": True}})
                out.append(preamble)
            owner = preamble
        owner = _child_of(owner, row, out) or owner
        _scan(owner, row)
    for b in stack:
        b.last = last_index
    if preamble is not None and preamble.last is None:
        preamble.last = last_index
        preamble.evidence = {"structure": {"rule": "no_headings", "certain": False}}
    for i, b in enumerate(out, start=1):
        b.no = i
    return _freeze(out, pages)


def _child_of(owner: _Builder, row: _Row, out: list[_Builder]) -> _Builder | None:
    """The one-block child a content block makes (a definition under a definitions section, a note paragraph), or
    None."""
    term = _definition(row) if owner.role == DEFINITIONS else None
    if term is not None:
        child = _Builder(DEFINITION, term, term, owner.depth + 1, row.index, owner,
                         {"opened_by": "definition_paragraph", "block": row.index})
    elif owner.role != NOTE and row.kind in ("paragraph", "textbox") and _NOTE_PARAGRAPH.match(row.text):
        child = _Builder(NOTE, None, None, owner.depth + 1, row.index, owner,
                         {"opened_by": "note_paragraph", "block": row.index})
    else:
        return None
    child.last = row.index
    out.append(child)
    return child


def _freeze(builders: list[_Builder], pages: dict[int, int | None]) -> tuple[Section, ...]:
    by_label: dict[str, list[_Builder]] = {}
    by_key: dict[str, list[_Builder]] = {}
    for b in builders:
        if b.role in (CHAPTER, SECTION) and b.label:
            by_label.setdefault(b.label, []).append(b)
        elif b.role == APPENDIX and b.label and (key := _appendix_key(b.label)):
            by_key.setdefault(key, []).append(b)

    def resolve(b: _Builder, ref: dict) -> dict:
        if ref["type"] == "standard":
            return ref
        candidates = (by_label.get(ref["label"], []) if ref["type"] == "section"
                      else by_key.get(_appendix_key(ref["label"]) or "", []))
        near = [c for c in candidates if c.root is b.root]
        target = (near or candidates or [None])[0]
        return ref | {"target": target.no if target is not None else None}

    frozen = []
    for b in builders:
        page_values = [p for i, p in pages.items() if b.first <= i <= (b.last or b.first) and p is not None]
        frozen.append(Section(
            b.no, b.label, b.title, b.parent.no if b.parent is not None else None, b.depth, b.role, b.first,
            b.last if b.last is not None else b.first, min(page_values, default=None), max(page_values, default=None),
            tuple(b.limits), tuple(resolve(b, r) for r in b.refs), dict(b.modality), b.evidence))
    return tuple(frozen)


# --- the text digest ------------------------------------------------------------------------------------------------

def _digest_words(blocks: Iterable) -> list[str]:
    words: list[str] = []
    for b in blocks:
        if b.kind == "image" or getattr(b, "status", "read") in ("decorative", "no_text"):
            continue
        lines = [ln for ln in strip_bidi_controls(b.text or "").splitlines()
                 if ln.strip() and (b.kind == "table" or not is_footer(ln))]
        if lines and b.kind != "table":
            lines[0] = LIST_MARK.sub("", lines[0].strip(), count=1)
        words += [w for ln in lines for w in base_normalize(ln).split() if w.strip("|")]
    return words


def text_digest(blocks: Iterable) -> str:
    """The sha256 of the normalized text of all blocks in order (see the module docstring)."""
    return hashlib.sha256(" ".join(_digest_words(blocks)).encode("utf-8")).hexdigest()


# --- the store ------------------------------------------------------------------------------------------------------

_BLOCKS_SQL = ("SELECT block_index, kind, label, section_path, paragraph_no, page, status, text FROM document_blocks"
               " WHERE version_id = :v ORDER BY block_index")
_COLUMNS = ("section_no, label, title, parent_no, depth, role, block_first, block_last, page_first, page_last,"
            " limits, refs, modality, evidence")
_INSERT = (
    "INSERT INTO document_sections (office_id, document_id, version_id, reading_id, sections_version, section_no,"
    " label, title, parent_no, depth, role, block_first, block_last, page_first, page_last, limits, refs, modality,"
    " evidence) VALUES (app_office(), :d, :v, :r, :sv, :n, :l, :t, :p, :dp, :ro, :bf, :bl, :pf, :pl,"
    " CAST(:li AS jsonb), CAST(:rf AS jsonb), CAST(:m AS jsonb), CAST(:e AS jsonb))"
    " ON CONFLICT (version_id, reading_id, section_no) DO NOTHING")


@dataclass(frozen=True)
class Version:
    """What the store needs to know of a version: its document, its reading and whether it takes sections."""

    document_id: UUID
    reading_id: str
    ingestion: dict


def version_of(conn: Connection, version_id: UUID | str) -> Version | None:
    """The version (as the caller sees it) when its document is a standard or a professional document; None for an
    appraisal, a version that does not exist or one the caller may not see."""
    row = conn.execute(text(
        "SELECT v.document_id, v.ingestion FROM document_versions v JOIN documents d ON d.id = v.document_id"
        " WHERE v.id = :v AND d.source_kind <> :a"), {"v": UUID(str(version_id)), "a": APPRAISAL}).first()
    if row is None:
        return None
    ing = row.ingestion or {}
    return Version(row.document_id, str(ing.get("reading_id") or NO_READING), ing)


def _section_of_row(r) -> Section:
    return Section(r.section_no, r.label, r.title, r.parent_no, r.depth, r.role, r.block_first, r.block_last,
                   r.page_first, r.page_last, tuple(r.limits or ()), tuple(r.refs or ()), dict(r.modality or {}),
                   dict(r.evidence or {}))


def stored(conn: Connection, version_id: UUID | str, version: Version | None = None) -> tuple[Section, ...] | None:
    """The version's stored sections of its current reading and ``SECTIONS_VERSION``; None when they were not derived
    yet. A reading that has no text derives no section, which its ``ingestion`` records so it is not derived again."""
    version = version or version_of(conn, version_id)
    if version is None:
        return None
    rows = conn.execute(text(
        f"SELECT {_COLUMNS} FROM document_sections WHERE version_id = :v AND reading_id = :r AND sections_version = :sv"
        " ORDER BY section_no"), {"v": UUID(str(version_id)), "r": version.reading_id, "sv": SECTIONS_VERSION}).all()
    if rows:
        return tuple(_section_of_row(r) for r in rows)
    done = version.ingestion.get("sections_derived") or {}
    if (done.get("reading_id"), done.get("version"), done.get("count")) == (version.reading_id, SECTIONS_VERSION, 0):
        return ()
    return None


def derive_version(conn: Connection, version_id: UUID | str) -> tuple[tuple[Section, ...], str]:
    """(sections, text digest) of the version's stored blocks: read-only, no file, no model (R3)."""
    rows = conn.execute(text(_BLOCKS_SQL), {"v": UUID(str(version_id))}).all()
    return derive(rows), text_digest(rows)


def _write(conn: Connection, version_id: UUID | str, version: Version, sections: tuple[Section, ...],
           digest: str) -> None:
    v = UUID(str(version_id))
    conn.execute(text("DELETE FROM document_sections WHERE version_id = :v AND (reading_id <> :r OR"
                      " sections_version <> :sv)"), {"v": v, "r": version.reading_id, "sv": SECTIONS_VERSION})
    for s in sections:
        conn.execute(text(_INSERT), {
            "d": version.document_id, "v": v, "r": version.reading_id, "sv": SECTIONS_VERSION, "n": s.section_no,
            "l": s.label, "t": s.title, "p": s.parent_no, "dp": s.depth, "ro": s.role, "bf": s.block_first,
            "bl": s.block_last, "pf": s.page_first, "pl": s.page_last,
            "li": json.dumps(s.limits, ensure_ascii=False), "rf": json.dumps(s.refs, ensure_ascii=False),
            "m": json.dumps(s.modality, ensure_ascii=False), "e": json.dumps(s.evidence, ensure_ascii=False)})
    marker = {"reading_id": version.reading_id, "version": SECTIONS_VERSION, "count": len(sections)}
    conn.execute(text("UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb) ||"
                      " jsonb_build_object('text_digest', CAST(:d AS text), 'sections_derived', CAST(:m AS jsonb))"
                      " WHERE id = :v"), {"d": digest, "m": json.dumps(marker), "v": v})


# the lazy build: one derivation per (version, reading) at a time in a process, its result kept briefly so callers
# that arrive while the first one's transaction is still open derive nothing again; across processes the advisory lock
# in ``ensure_in`` makes a second writer wait for the first and find its rows
_MEMO_MAX = 64
_MEMO: OrderedDict[tuple[str, str, str], tuple[tuple[Section, ...], str]] = OrderedDict()
_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_GUARD = threading.Lock()


def _flight(version_id: str, reading_id: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault((version_id, reading_id), threading.Lock())


def _landed(version_id: str, reading_id: str, lock: threading.Lock) -> None:
    """Forget a build's lock once it is done, so ``_LOCKS`` holds only the builds in flight."""
    with _GUARD:
        if _LOCKS.get((version_id, reading_id)) is lock:
            del _LOCKS[(version_id, reading_id)]


def ensure_in(conn: Connection, version_id: UUID | str) -> tuple[Section, ...]:
    """The version's sections for use inside the caller's transaction (the caller resolved the version under the
    user's permissions): the stored ones when current; otherwise derived here, once per version and reading, and
    stored (in a savepoint, so a transaction that cannot write still gets its sections). () for an appraisal, a
    version that does not exist and one the caller may not see."""
    version = version_of(conn, version_id)
    if version is None:
        return ()
    found = stored(conn, version_id, version)
    if found is not None:
        return found
    vid = str(version_id)
    lock = _flight(vid, version.reading_id)
    try:
        with lock:
            found = stored(conn, version_id, version_of(conn, version_id))
            if found is not None:
                return found
            memo_key = (vid, version.reading_id, SECTIONS_VERSION)
            with _GUARD:
                memoed = _MEMO.get(memo_key)
            if memoed is None:
                memoed = derive_version(conn, version_id)
                with _GUARD:
                    _MEMO[memo_key] = memoed
                    while len(_MEMO) > _MEMO_MAX:
                        _MEMO.popitem(last=False)
            sections, digest = memoed
            try:
                with conn.begin_nested():
                    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                                 {"k": f"sections:{vid}:{version.reading_id}"})
                    found = stored(conn, version_id, version_of(conn, version_id))
                    if found is not None:  # another process wrote them while this one waited for the lock
                        return found
                    _write(conn, version_id, version, sections, digest)
            except Exception:  # noqa: BLE001 - a read-only transaction or a vanished version: the sections still serve
                logger.info("sections of version %s not stored", vid, exc_info=True)
            return sections
    finally:
        _landed(vid, version.reading_id, lock)


def ensure(ctx: TenantContext, version_id: UUID | str) -> tuple[Section, ...]:
    """``ensure_in`` in a transaction of its own under ``ctx`` (the worker after ingestion, a tool outside a
    transaction)."""
    with tenant_tx(ctx) as conn:
        return ensure_in(conn, version_id)


# --- readers --------------------------------------------------------------------------------------------------------

def sections_of(conn: Connection, version_id: UUID | str) -> tuple[Section, ...]:
    """All sections of the version in document order (derived on first use)."""
    return ensure_in(conn, version_id)


def section_of(conn: Connection, version_id: UUID | str, block_index: int) -> Section | None:
    """The innermost section holding a block (the one a search hit in that block belongs to), or None."""
    holding = [s for s in ensure_in(conn, version_id) if s.block_first <= block_index <= s.block_last]
    return max(holding, key=lambda s: (s.depth, s.section_no), default=None)


@dataclass(frozen=True)
class SectionText:
    """A section read whole: its text with the subsections', every limit in it and every reference it makes, as the
    answer tools cite them (R22, R23). A reference carries ``status``: ``resolved`` (it names a section of this
    document, with ``target_label``), ``unresolved`` (it names a section that is not there) or ``external`` (another
    document: a gap unless the caller finds it in the repository)."""

    section: Section
    text: str
    blocks: tuple[tuple[int, str, int | None, str], ...]  # (block index, kind, page, text)
    subsections: tuple[Section, ...]
    limits: tuple[dict, ...]
    refs: tuple[dict, ...]


def _find(sections: tuple[Section, ...], ref: int | str) -> Section | None:
    if isinstance(ref, int):
        return next((s for s in sections if s.section_no == ref), None)
    wanted = _clean(str(ref))
    if wanted.startswith("נספח"):
        key = _appendix_key(wanted)
        return next((s for s in sections if s.role == APPENDIX and s.label and _appendix_key(s.label) == key), None)
    label = _norm_label(wanted) or wanted
    return next((s for s in sections if s.label == label), None)


def section_text(conn: Connection, version_id: UUID | str, ref: int | str) -> SectionText | None:
    """The section named by its number or label ("2.1", "נספח א'") with its subsections, limits and references; None
    when the version has no such section."""
    sections = ensure_in(conn, version_id)
    section = _find(sections, ref)
    if section is None:
        return None
    inside = tuple(s for s in sections if s.section_no != section.section_no
                   and section.block_first <= s.block_first and s.block_last <= section.block_last
                   and s.depth > section.depth)
    blocks = tuple((r.block_index, r.kind, r.page, r.text) for r in conn.execute(text(
        "SELECT block_index, kind, page, text FROM document_blocks WHERE version_id = :v AND block_index BETWEEN :a"
        " AND :b ORDER BY block_index"), {"v": UUID(str(version_id)), "a": section.block_first,
                                          "b": section.block_last}) if (r.text or "").strip())
    by_no = {s.section_no: s for s in sections}
    owned = (section, *inside)
    refs = []
    for s in owned:
        for r in s.refs:
            target = by_no.get(r["target"]) if r.get("target") is not None else None
            status = ("external" if r["type"] == "standard" else "resolved" if target is not None else "unresolved")
            refs.append(r | {"status": status} | ({"target_label": target.label or target.title}
                                                  if target is not None else {}))
    limits = tuple(sorted((lim for s in owned for lim in s.limits), key=lambda lim: lim["block"]))
    return SectionText(section, "\n".join(b[3] for b in blocks), blocks, inside, limits,
                       tuple(sorted(refs, key=lambda r: r["block"])))
