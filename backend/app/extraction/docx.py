"""DOCX extraction: every part of the body in reading order, located by section and paragraph (R5).

A DOCX has no physical pages, so nothing here invents one: a block is located by its section (the path of
headings above it), its numbering label ("13.2") and its running paragraph number, and a picture also by its
media name. ``page_count`` stays None and the one ``PageResult`` (``page_no`` 1, method ``docx``) only carries
the plain text for the earlier text-based stages.

What is read:

- paragraphs (runs, hyperlinks, fields' results, content controls), with their numbering labels computed from
  ``numbering.xml`` the way Word counts them (per abstract list, levels reset by their parent);
- headings: built-in heading styles and outline levels, and short numbered paragraphs of the first levels
  (reports number their sections with custom list styles, not Word's heading styles);
- text boxes (``w:txbxContent``), once each — Word stores a Choice and a Fallback copy of every shape;
- Word tables, including the pictures inside their cells;
- pictures (``a:blip``, VML ``v:imagedata``): EMF text records, SVG text, and raster pictures through OCR and,
  when the caller passes a ``VisionReader``, the vision model (``app.extraction.images``). Every picture gets a
  status, and a picture that could not be read makes the document partial.

Tables (Word's own and those read from pictures) keep the paragraph that introduces them as ``caption``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import posixpath
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from lxml import etree

from app.config import Settings
from app.extraction.base import (
    Block,
    ExtractionError,
    ExtractionResult,
    PageResult,
    TableResult,
    TableRow,
    check_deadline,
)
from app.extraction.chunking import chunk_blocks, is_heading
from app.extraction.images import CONTEXT_CHARS, PictureReading, VisionReader, read_picture
from app.extraction.tables import clean_cell, units_for

logger = logging.getLogger(__name__)

MSG_CORRUPT = "הקובץ פגום או שאינו DOCX תקין"
MSG_TOO_BIG = "קובץ ה-DOCX חורג ממגבלת הגודל המותרת לאחר פריסה"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {
    "w": W, "r": R,
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "v": "urn:schemas-microsoft-com:vml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
_W = f"{{{W}}}"
_SKIP = {f"{_W}delText", f"{_W}instrText", f"{_W}txbxContent", f"{{{NS['mc']}}}Fallback"}
HEADING_MAX_WORDS = 12
HEADING_MAX_CHARS = 90
NUMBERED_HEADING_LEVELS = 2  # numbered short paragraphs on list levels 0..1 are section headings
PICTURE_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
PICTURE_READER_VERSION = "docx-pictures-v2"  # a picture's reading, cached by content (v2: the model decides no-text)
_HEB_LETTERS = "אבגדהוזחטיכלמנסעפצקרשת"


def _check_zip(data: bytes, max_mb: int) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
            total = sum(i.file_size for i in zf.infolist())
    except (zipfile.BadZipFile, ValueError, OSError):
        raise ExtractionError(MSG_CORRUPT, True) from None
    if "word/document.xml" not in names:
        raise ExtractionError(MSG_CORRUPT, True)
    if total > max_mb * 1024 * 1024:
        raise ExtractionError(MSG_TOO_BIG, True)


def _xml(zf: zipfile.ZipFile, name: str):
    try:
        return etree.fromstring(zf.read(name), parser=etree.XMLParser(resolve_entities=False, no_network=True,
                                                                     huge_tree=True))
    except KeyError:
        return None


# --- styles and numbering ------------------------------------------------------------------------------

@dataclass
class _Style:
    name: str
    based_on: str | None
    outline: int | None
    num_id: str | None
    ilvl: int | None


def _styles(root) -> dict[str, _Style]:
    out: dict[str, _Style] = {}
    if root is None:
        return out
    for s in root.iter(f"{_W}style"):
        sid = s.get(f"{_W}styleId")
        if not sid:
            continue
        name = s.find(f"{_W}name")
        based = s.find(f"{_W}basedOn")
        ol = s.find(f"{_W}pPr/{_W}outlineLvl")
        num = s.find(f"{_W}pPr/{_W}numPr")
        num_id = ilvl = None
        if num is not None:
            n = num.find(f"{_W}numId")
            lvl = num.find(f"{_W}ilvl")
            num_id = n.get(f"{_W}val") if n is not None else None
            ilvl = int(lvl.get(f"{_W}val")) if lvl is not None else None
        out[sid] = _Style(name.get(f"{_W}val") if name is not None else sid,
                          based.get(f"{_W}val") if based is not None else None,
                          int(ol.get(f"{_W}val")) if ol is not None else None, num_id, ilvl)
    return out


def _style_attr(styles: dict[str, _Style], sid: str | None, attr: str):
    seen = set()
    while sid and sid in styles and sid not in seen:
        seen.add(sid)
        value = getattr(styles[sid], attr)
        if value is not None:
            return value
        sid = styles[sid].based_on
    return None


@dataclass
class _Level:
    fmt: str = "decimal"
    text: str = "%1."
    start: int = 1


@dataclass
class _Numbering:
    abstract_of: dict[str, str] = field(default_factory=dict)
    levels: dict[str, dict[int, _Level]] = field(default_factory=dict)
    overrides: dict[str, dict[int, int]] = field(default_factory=dict)  # numId -> {ilvl: start}
    counters: dict[str, list[int]] = field(default_factory=dict)
    started: set = field(default_factory=set)

    def label(self, num_id: str, ilvl: int) -> str | None:
        abstract = self.abstract_of.get(num_id)
        if abstract is None or num_id == "0":
            return None
        levels = self.levels.get(abstract, {})
        lvl = levels.get(ilvl, _Level())
        if lvl.fmt in ("bullet", "none"):
            return None
        counts = self.counters.setdefault(abstract, [0] * 9)
        override = self.overrides.get(num_id, {})
        if (num_id, ilvl) not in self.started and ilvl in override:
            counts[ilvl] = override[ilvl] - 1
        self.started.add((num_id, ilvl))
        if counts[ilvl] == 0:
            counts[ilvl] = lvl.start - 1
        counts[ilvl] += 1
        for deeper in range(ilvl + 1, 9):
            counts[deeper] = 0

        def fmt(k: int) -> str:
            level = levels.get(k, _Level())
            value = counts[k] or level.start
            if level.fmt.startswith("hebrew"):
                return _HEB_LETTERS[(value - 1) % len(_HEB_LETTERS)]
            if level.fmt == "lowerLetter":
                return chr(ord("a") + (value - 1) % 26)
            if level.fmt == "upperLetter":
                return chr(ord("A") + (value - 1) % 26)
            return str(value)

        text = re.sub(r"%(\d)", lambda m: fmt(int(m.group(1)) - 1), lvl.text)
        return text.strip().rstrip(".").strip() or None


def _numbering(root) -> _Numbering:
    nb = _Numbering()
    if root is None:
        return nb
    for a in root.iter(f"{_W}abstractNum"):
        aid = a.get(f"{_W}abstractNumId")
        levels = {}
        for lvl in a.iter(f"{_W}lvl"):
            i = int(lvl.get(f"{_W}ilvl", "0"))
            fmt = lvl.find(f"{_W}numFmt")
            txt = lvl.find(f"{_W}lvlText")
            start = lvl.find(f"{_W}start")
            levels[i] = _Level(fmt.get(f"{_W}val") if fmt is not None else "decimal",
                               txt.get(f"{_W}val") if txt is not None else f"%{i + 1}.",
                               int(start.get(f"{_W}val")) if start is not None else 1)
        nb.levels[aid] = levels
    for n in root.iter(f"{_W}num"):
        nid = n.get(f"{_W}numId")
        a = n.find(f"{_W}abstractNumId")
        if a is not None:
            nb.abstract_of[nid] = a.get(f"{_W}val")
        for o in n.iter(f"{_W}lvlOverride"):
            so = o.find(f"{_W}startOverride")
            if so is not None:
                nb.overrides.setdefault(nid, {})[int(o.get(f"{_W}ilvl", "0"))] = int(so.get(f"{_W}val"))
    return nb


# --- text ---------------------------------------------------------------------------------------------

def _text(el) -> str:
    """The visible text of a paragraph or cell, without text boxes, deleted text or field codes."""
    parts: list[str] = []

    def walk(node) -> None:
        for child in node:
            tag = child.tag
            if not isinstance(tag, str) or tag in _SKIP:
                continue
            if tag == f"{_W}t":
                parts.append(child.text or "")
            elif tag in (f"{_W}tab", f"{_W}ptab"):
                parts.append(" ")
            elif tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
            elif tag == f"{_W}noBreakHyphen":
                parts.append("-")
            else:
                walk(child)

    walk(el)
    return "".join(parts)


def _clean(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace(" ", " ").split("\n")]
    return "\n".join(line for line in lines if line)


def _pictures(el) -> list[str]:
    """Relationship ids of the pictures under ``el`` (outside its text boxes), in document order, once each."""
    ids: list[str] = []

    def walk(node) -> None:
        for child in node:
            tag = child.tag
            if not isinstance(tag, str) or tag in (f"{_W}txbxContent", f"{{{NS['mc']}}}Fallback"):
                continue
            if tag == f"{{{NS['a']}}}blip":
                rid = child.get(f"{{{R}}}embed")
                if rid and rid not in ids:
                    ids.append(rid)
            elif tag == f"{{{NS['v']}}}imagedata":
                rid = child.get(f"{{{R}}}id")
                if rid and rid not in ids:
                    ids.append(rid)
            walk(child)

    walk(el)
    return ids


def _textboxes(el) -> list:
    """Text box contents under ``el`` outside a Fallback copy."""
    out = []
    for tb in el.iter(f"{_W}txbxContent"):
        node, fallback = tb.getparent(), False
        while node is not None and node is not el:
            if node.tag == f"{{{NS['mc']}}}Fallback":
                fallback = True
                break
            node = node.getparent()
        if not fallback:
            out.append(tb)
    return out


# --- the walk -----------------------------------------------------------------------------------------

@dataclass
class _Picture:
    block: Block
    rid: str
    context: str


@dataclass
class _Walker:
    zf: zipfile.ZipFile
    styles: dict[str, _Style]
    numbering: _Numbering
    rels: dict[str, str]
    deadline: float
    blocks: list[Block] = field(default_factory=list)
    tables: list[TableResult] = field(default_factory=list)
    pictures: list[_Picture] = field(default_factory=list)
    path: list[str] = field(default_factory=list)
    paragraph_no: int = 0
    seen_textboxes: set = field(default_factory=set)
    last_text: str = ""

    @property
    def section(self) -> str | None:
        return self.path[-1] if self.path else None

    def add(self, kind: str, text: str, **kw) -> Block:
        b = Block(index=len(self.blocks), kind=kind, text=text, section=self.section, section_path=list(self.path),
                  **kw)
        self.blocks.append(b)
        return b

    # paragraphs

    def paragraph(self, p) -> None:
        ppr = p.find(f"{_W}pPr")
        sid = None
        num_id = ilvl = outline = None
        if ppr is not None:
            ps = ppr.find(f"{_W}pStyle")
            sid = ps.get(f"{_W}val") if ps is not None else None
            num = ppr.find(f"{_W}numPr")
            if num is not None:
                n, lvl = num.find(f"{_W}numId"), num.find(f"{_W}ilvl")
                num_id = n.get(f"{_W}val") if n is not None else None
                ilvl = int(lvl.get(f"{_W}val")) if lvl is not None else None
            ol = ppr.find(f"{_W}outlineLvl")
            outline = int(ol.get(f"{_W}val")) if ol is not None else None
        style_name = (_style_attr(self.styles, sid, "name") or "").lower()
        if style_name.startswith("toc"):
            return  # table of contents lines repeat the headings with page numbers
        num_id = num_id or _style_attr(self.styles, sid, "num_id")
        if ilvl is None:
            ilvl = _style_attr(self.styles, sid, "ilvl") or 0
        if outline is None:
            outline = _style_attr(self.styles, sid, "outline")
        text = _clean(_text(p))
        label = self.numbering.label(num_id, ilvl) if (num_id and text) else None

        if text:
            level = self._heading_level(text, style_name, outline, label, ilvl)
            if level is not None:
                # The computed list label is kept as data only: Word's own count can differ (restarts, fields),
                # and a wrong section number in a citation misleads more than the heading's text alone.
                self.path = self.path[: level - 1] + [text]
                self.add("heading", text, label=label)
            else:
                self.paragraph_no += 1
                self.add("paragraph", text, label=label, paragraph_no=self.paragraph_no)
            self.last_text = text
        for tb in _textboxes(p):
            self.textbox(tb)
        for rid in _pictures(p):
            self.picture(rid, context=text or self.last_text)

    def _heading_level(self, text: str, style_name: str, outline: int | None, label: str | None,
                       ilvl: int) -> int | None:
        short = (len(text) <= HEADING_MAX_CHARS and len(text.split()) <= HEADING_MAX_WORDS
                 and not text.rstrip().endswith((".", ",", ";")) and "\n" not in text)
        if outline is not None and outline < 9 and short:
            return outline + 1
        m = re.match(r"heading (\d)", style_name)
        if m and short:
            return int(m.group(1))
        if label and short and ilvl < NUMBERED_HEADING_LEVELS and re.match(r"^\d", label):
            return ilvl + 1
        if is_heading(text):
            return 1
        return None

    def textbox(self, tb) -> None:
        text = _clean("\n".join(_text(p) for p in tb.iter(f"{_W}p")))
        if not text or text in self.seen_textboxes:
            return
        self.seen_textboxes.add(text)
        self.add("textbox", text)
        for rid in _pictures(tb):
            self.picture(rid, context=text)

    # tables

    def table(self, tbl) -> None:
        rows: list[list[str]] = []
        for tr in tbl.findall(f"{_W}tr"):
            cells = []
            for tc in tr.findall(f"{_W}tc"):
                span = tc.find(f"{_W}tcPr/{_W}gridSpan")
                cells.append(clean_cell(_text(tc).replace("\n", " ")))
                if span is not None:
                    cells.extend([""] * (int(span.get(f"{_W}val", "1")) - 1))
            rows.append(cells)
        rows = [r for r in rows if any(r)]
        for tb in _textboxes(tbl):
            self.textbox(tb)
        pictures = _pictures(tbl)
        if rows:
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            if width < 2 or len(rows) == 1 and width <= 2:
                # a one-column or one-cell table is layout: its text is a paragraph
                self.paragraph_no += 1
                self.add("paragraph", "\n".join(" ".join(c for c in r if c) for r in rows),
                         paragraph_no=self.paragraph_no)
            else:
                headers, body = (rows[0], rows[1:]) if _header_like(rows[0]) and len(rows) > 1 else ([], rows)
                self._table(headers, body, source="word_table", caption=self.last_text, title=[], notes=[])
        for rid in pictures:
            self.picture(rid, context=self.last_text)

    def _table(self, headers: list[str], body: list[list[str]], *, source: str, caption: str | None,
               title: list[str], notes: list[str], media: str | None = None, block: Block | None = None) -> TableResult:
        index = len(self.tables)
        t = TableResult(index=index, headers=headers, units=units_for(headers),
                        rows=[TableRow(page=None, cells=r) for r in body], page_start=None, page_end=None,
                        ocr=source in ("ocr", "vision"), section=self.section if block is None else block.section,
                        source=source, media=media, caption=caption or None, title=title, notes=notes)
        if block is None:
            block = self.add("table", "", source=source, table_index=index)
        else:
            block.table_index = index if block.table_index is None else block.table_index
        t.block_index = block.index
        block.text = "\n".join(x for x in [*title, render_table(t)] if x)
        self.tables.append(t)
        return t

    # pictures

    def picture(self, rid: str, context: str) -> None:
        target = self.rels.get(rid)
        if not target:
            return
        block = self.add("image", "", media=posixpath.basename(target), status="unread")
        self.pictures.append(_Picture(block, target, context))


def _header_like(cells: list[str]) -> bool:
    filled = [c for c in cells if c]
    return bool(filled) and sum(1 for c in filled if re.search(r"\d", c)) * 3 <= len(filled) and any(
        re.search(r"[א-ת]", c) for c in filled)


def render_table(t: TableResult) -> str:
    """Plain-text table: header row, data rows (" | " between cells), then notes."""
    lines = []
    if any(t.headers):
        lines.append(" | ".join(t.headers))
    lines.extend(" | ".join(r.cells) for r in t.rows)
    lines.extend(t.notes)
    return "\n".join(line for line in lines if line.strip(" |"))


def _rels(zf: zipfile.ZipFile) -> dict[str, str]:
    root = _xml(zf, "word/_rels/document.xml.rels")
    out = {}
    if root is None:
        return out
    for rel in root:
        if rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        out[rel.get("Id")] = posixpath.normpath(posixpath.join("word", target)) if not target.startswith("/") \
            else target.lstrip("/")
    return out


def _reading_key(data: bytes, context: str, vision: VisionReader | None, ocr_languages: str | None = None):
    """The cache key of a picture's reading: its bytes and the context the model is shown with it, and the reader
    configuration with the OCR languages (``regions.model_config``, KTD11); ``ocr_languages`` defaults to the
    configured ones."""
    from app.config import get_settings
    from app.extraction.regions import IMAGE_CROP, ReadingKey, model_config

    languages = ocr_languages if ocr_languages is not None else get_settings().ocr_languages
    digest = hashlib.sha256(data + b"\0" + context[:CONTEXT_CHARS].encode()).hexdigest()
    return ReadingKey("docx:" + digest, PICTURE_READER_VERSION, model_config(vision, languages), IMAGE_CROP)


def _read_pictures(w: _Walker, vision: VisionReader | None, settings: Settings, cache=None) -> None:
    """Read every picture once (a media part used twice is read once) in a small thread pool, then fill the
    picture blocks in document order: text into the block, tables into ``w.tables``. ``cache``: the office's
    readings by content (``app.extraction.regions.ReadingCache``): a picture read before, in this or another
    document, with the same context and reader configuration is not read again."""
    from app.extraction.regions import CACHEABLE

    unique: dict[str, _Picture] = {}
    for pic in w.pictures:
        unique.setdefault(pic.rid, pic)

    def read(pic: _Picture) -> tuple[str, PictureReading]:
        check_deadline(w.deadline)
        try:
            data = w.zf.read(pic.rid)
        except KeyError:
            return pic.rid, PictureReading("unread", "none", note="קובץ התמונה חסר במסמך")
        key = _reading_key(data, pic.context, vision, settings.ocr_languages) if cache is not None else None
        if key is not None:
            try:
                cached = cache.get(key)
            except Exception:  # noqa: BLE001 - a cache that cannot be read only costs a fresh reading
                logger.warning("reading cache lookup failed")
                cached = None
            if cached is not None:
                return pic.rid, cached
        ext = posixpath.splitext(pic.rid)[1]
        reading = read_picture(data, ext, pic.context, vision, settings.ocr_languages)
        if key is not None and reading.cacheable and reading.status in CACHEABLE:
            try:
                cache.put(key, reading)
            except Exception:  # noqa: BLE001
                logger.warning("reading cache store failed")
        return pic.rid, reading

    with ThreadPoolExecutor(max_workers=PICTURE_WORKERS) as pool:
        readings = dict(pool.map(read, unique.values()))
    for pic in w.pictures:
        reading = readings[pic.rid]
        b = pic.block
        b.status, b.source, b.note = reading.status, reading.method, reading.note
        b.picture_text = reading.text
        texts = [reading.text] if reading.text else []
        for pt in reading.tables:
            t = w._table(pt.headers, pt.rows, source=reading.method, caption=pic.context, title=pt.title,
                         notes=pt.notes, media=b.media, block=b)
            texts.append("\n".join(x for x in [*t.title, render_table(t)] if x))
        b.text = "\n".join(x for x in texts if x)


def extract_docx(data: bytes, deadline: float, settings: Settings, vision: VisionReader | None = None,
                 readings=None) -> ExtractionResult:
    _check_zip(data, settings.max_docx_uncompressed_mb)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        doc = _xml(zf, "word/document.xml")
        body = doc.find(f"{_W}body") if doc is not None else None
    except Exception:  # noqa: BLE001
        raise ExtractionError(MSG_CORRUPT, True) from None
    if body is None:
        raise ExtractionError(MSG_CORRUPT, True)
    w = _Walker(zf, _styles(_xml(zf, "word/styles.xml")), _numbering(_xml(zf, "word/numbering.xml")), _rels(zf),
                deadline)

    def walk(container) -> None:
        for el in container.iterchildren():
            check_deadline(deadline)
            tag = el.tag if isinstance(el.tag, str) else ""
            if tag == f"{_W}p":
                w.paragraph(el)
            elif tag == f"{_W}tbl":
                w.table(el)
            elif tag in (f"{_W}sdt", f"{_W}customXml"):
                content = el.find(f"{_W}sdtContent") if tag == f"{_W}sdt" else el
                if content is not None:
                    walk(content)

    walk(body)
    _read_pictures(w, vision, settings, readings)
    blocks = w.blocks
    text = "\n".join(b.text for b in blocks if b.text)
    page = PageResult(page_no=1, text=text, method="docx", quality=1.0, ok=True)
    chunks = chunk_blocks(blocks, w.tables)
    result = ExtractionResult(page_count=None, pages=[page], tables=w.tables, chunks=chunks, is_docx=True,
                              blocks=blocks)
    unread = result.components["unread"]
    if unread:
        result.warnings.append(f"{len(unread)} תמונות לא נקראו")
    return result
