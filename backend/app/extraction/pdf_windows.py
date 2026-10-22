"""Windowed, resumable reading of a PDF (KTD2, R1, R9).

A long file is read in page windows. Each window's output (the lines with their glyph boxes, tables, pictures,
geometry, orientation evidence, the text layer's character boxes and the words with their fonts) is kept in a
``Stage`` before the next window starts, so the pages of a window are in memory only while it is read. Everything
a later step reads stays in the stage (nothing the steps need is dropped); the steps run over it after the last
window, each in resumable units:

1. pass 1 per window (``pdf.read_window``);
2. font-map detection over all words, then the re-read of the pages holding a suspect character, per window;
3. orientation of the pages whose words decided nothing, and printed page labels;
4. region reading: ink regions and picture hashes per window, each distinct content read in batches, then every
   region given its reading and the repeated pictures marked;
5. the block walker over the pages in order (tables continuing over a window border are assembled there, by
   ``Walker.finish`` over all the pieces), chunking and the reading report.

A step's unit finishes before its progress is recorded in the stage's state (``Stage.commit``), and re-running a
unit gives the same stored result, so a crash resumes at the last finished unit. When the time is spent
(``soft``), the run stops cleanly before the next unit (``ExtractionPaused``); a deadline inside a unit drops that
unit only. The result of a file under one window is the single pass's, and a file read in any window size gives the
same blocks, tables and chunks (``tests/integration/test_extraction_pipeline.py``).

``MemoryStage`` keeps the objects themselves (a document read in one call); ``StorageStage`` keeps them in file
storage as compressed JSON under an office-scoped key (``app.platform.storage.staged_key``), never pickled.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import time
import zlib
from collections.abc import Callable
from dataclasses import asdict
from uuid import UUID

from app.config import Settings
from app.extraction import fontmap, pdf
from app.extraction.base import (
    MSG_DEADLINE,
    ExtractionError,
    ExtractionPaused,
    ExtractionResult,
    PageGeometry,
    PageResult,
    check_deadline,
)
from app.extraction.fontmap import FontMapConfig, FontMapFix, WordSample
from app.extraction.images import PictureReading, VisionReader
from app.extraction.pdf import WARN_PLUMBER, _layers, _Line, _PageOut
from app.extraction.regions import (
    REGION_BATCH,
    ContentInfo,
    ContentResult,
    Occurrence,
    ReadingCache,
    Region,
    apply_results,
    index_regions,
    mark_repeated,
    occurrence_places,
    read_contents,
)
from app.extraction.tables import RawTable

log = logging.getLogger(__name__)

STATE_VERSION = 1

# What the ingestion report shows as the stage of a reading in progress (the documents screen names them).
STAGE_READING = "reading_pages"
STAGE_FONTS = "checking_fonts"
STAGE_LAYOUT = "layout"
STAGE_REGIONS = "reading_regions"
STAGE_ASSEMBLING = "assembling"

Progress = Callable[[int, int, str], None]


class StageLost(Exception):
    """Staged outputs the state counts on cannot be read (a file is gone or damaged): the reading starts again."""


def _now() -> float:
    return time.monotonic()


# --- the stage ---------------------------------------------------------------------------------------------------

class MemoryStage:
    """A staged reading kept as objects in memory: what a document read in one call needs, with no serialisation."""

    def __init__(self) -> None:
        self.state: dict = {}
        self._windows: dict[int, list[_PageOut]] = {}
        self._blobs: dict[str, object] = {}

    def reset(self) -> None:
        self.state, self._windows, self._blobs = {}, {}, {}

    def commit(self) -> None:
        """The state is the object itself."""

    def put_window(self, w: int, outs: list[_PageOut]) -> None:
        self._windows[w] = outs

    def get_window(self, w: int) -> list[_PageOut]:
        return self._windows[w]

    def update_window(self, w: int, outs: list[_PageOut]) -> None:
        self._windows[w] = outs

    def chars(self, w: int) -> list[list]:
        return [o.chars for o in self._windows[w]]

    def set_chars(self, w: int, chars: list[list]) -> None:
        for o, c in zip(self._windows[w], chars, strict=True):
            o.chars = c

    def drop_chars(self, windows: int) -> None:
        for outs in self._windows.values():
            for o in outs:
                o.chars = []

    def words(self, w: int) -> list[list[WordSample]]:
        return [o.words for o in self._windows[w]]

    def drop_words(self, windows: int) -> None:
        for outs in self._windows.values():
            for o in outs:
                o.words = []

    def put_blob(self, name: str, obj: object) -> None:
        self._blobs[name] = obj

    def get_blob(self, name: str) -> object:
        return self._blobs[name]


class StorageStage:
    """A staged reading in file storage: ``<office>/staging/<version>/<attempt>/<name>``, one file per window
    (``w000000``), its character boxes (``c000000``) and words (``x000000``), the batches of region readings
    (``r000000``) and the state (``state.json``). Compressed JSON, never pickle: what is read back is data only.
    A stage whose state belongs to another file or window size (an attempt key hashes both) starts clean."""

    def __init__(self, storage, office_id: UUID, version_id: UUID, attempt: str):
        self.storage = storage
        self.office_id, self.version_id, self.attempt = office_id, version_id, attempt
        self.state: dict = {}
        try:
            self.state = json.loads(self.storage.get(self._key("state.json")))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):  # damaged: the reading starts again
            log.warning("staged state of version %s is unreadable", version_id)

    def _key(self, name: str) -> str:
        from app.platform.storage import staged_key

        return staged_key(self.office_id, self.version_id, self.attempt, name)

    def _put(self, name: str, obj: object) -> None:
        self.storage.replace(self._key(name), gzip.compress(json.dumps(obj).encode(), compresslevel=3))

    def _get(self, name: str) -> object:
        try:
            return json.loads(gzip.decompress(self.storage.get(self._key(name))))
        except (OSError, ValueError, EOFError, zlib.error) as exc:  # gone, truncated, corrupt or not JSON
            raise StageLost(name) from exc

    def reset(self) -> None:
        from app.platform.storage import staging_prefix

        self.storage.delete_prefix(staging_prefix(self.office_id, self.version_id, self.attempt))
        self.state = {}

    def commit(self) -> None:
        self.storage.replace(self._key("state.json"), json.dumps(self.state).encode())

    def put_window(self, w: int, outs: list[_PageOut]) -> None:
        self._put(f"c{w:06d}.json.gz", [_enc_chars(o.chars) for o in outs])
        self._put(f"x{w:06d}.json.gz", _enc_words([o.words for o in outs]))
        self.update_window(w, outs)

    def get_window(self, w: int) -> list[_PageOut]:
        return [_dec_page_out(p) for p in self._get(f"w{w:06d}.json.gz")]

    def update_window(self, w: int, outs: list[_PageOut]) -> None:
        self._put(f"w{w:06d}.json.gz", [_enc_page_out(o) for o in outs])

    def chars(self, w: int) -> list[list]:
        return [_dec_chars(c) for c in self._get(f"c{w:06d}.json.gz")]

    def set_chars(self, w: int, chars: list[list]) -> None:
        self._put(f"c{w:06d}.json.gz", [_enc_chars(c) for c in chars])

    def drop_chars(self, windows: int) -> None:
        for w in range(windows):
            self.storage.delete_prefix(self._key("c") + f"{w:06d}.")

    def words(self, w: int) -> list[list[WordSample]]:
        return _dec_words(self._get(f"x{w:06d}.json.gz"))

    def drop_words(self, windows: int) -> None:
        for w in range(windows):
            self.storage.delete_prefix(self._key("x") + f"{w:06d}.")

    def put_blob(self, name: str, obj: object) -> None:
        self._put(f"{name}.json.gz", obj)

    def get_blob(self, name: str) -> object:
        return self._get(f"{name}.json.gz")


# --- encoding (data only) ----------------------------------------------------------------------------------------

def _enc_chars(chars: list[tuple[float, float, float, float]]) -> list[float]:
    return [v for c in chars for v in c]


def _dec_chars(flat: list[float]) -> list[tuple[float, float, float, float]]:
    return [(flat[i], flat[i + 1], flat[i + 2], flat[i + 3]) for i in range(0, len(flat), 4)]


def _enc_words(pages: list[list[WordSample]]) -> dict:
    fonts: dict[str, int] = {}
    out = []
    for words in pages:
        enc = []
        for w in words:
            runs: list[list[int]] = []
            for f in w.fonts:
                k = fonts.setdefault(f, len(fonts))
                if runs and runs[-1][0] == k:
                    runs[-1][1] += 1
                else:
                    runs.append([k, 1])
            enc.append([w.page, w.text, list(w.bbox), runs])
        out.append(enc)
    return {"fonts": list(fonts), "pages": out}


def _dec_words(data: dict) -> list[list[WordSample]]:
    fonts = data["fonts"]
    return [[WordSample(page, text, tuple(bbox), tuple(fonts[k] for k, n in runs for _ in range(n)))
             for page, text, bbox, runs in words] for words in data["pages"]]


def _enc_geometry(g: PageGeometry | None) -> list | None:
    return None if g is None else [g.mediabox, g.cropbox, g.rotation, g.width, g.height, g.issue]


def _enc_line(ln: _Line) -> list:
    # a corrected line keeps only what ``FontMapFix.original_line`` reads of its chars: the text and its origin
    fm = None if ln.fm_chars is None else [
        {"text": c.get("text")} | ({"fontmap_from": c["fontmap_from"]} if "fontmap_from" in c else {})
        for c in ln.fm_chars]
    return [ln.top, ln.text, ln.bottom, ln.x0, ln.x1, ln.size, ln.bold, ln.in_table, ln.original, ln.uncertain,
            ln.raw, fm, ln.layer_text, ln.glyphs, ln.flips]


def _enc_table(t: RawTable) -> list:
    return [t.page, t.rows, t.ocr, t.section, t.break_before, t.top, t.bbox, t.table_index, t.continues,
            t.uncertain, t.boxes]


def _dec_table(d: list) -> RawTable:
    # a window staged before round 22 U3 holds ``first_on_page`` (a bool) where ``break_before`` stands now
    return RawTable(*[None if k == 4 and isinstance(v, bool) else v for k, v in enumerate(d)])


def _enc_region(r: Region) -> list:
    return [r.page, r.bbox, r.kind, r.content_hash, list(r.srcsize) if r.srcsize else None,
            asdict(r.reading) if r.reading is not None else None, r.covered, r.furniture, r.duplicate, r.layer_text,
            r.layer_chars, r.layer_density, r.layer_span]


def _dec_region(d: list) -> Region:
    page, bbox, kind, digest, srcsize, reading, covered, furniture, duplicate, text, chars, density, span = d
    return Region(page, bbox, kind, digest, tuple(srcsize) if srcsize else None,
                  PictureReading.from_json(reading) if reading is not None else None, covered, furniture, duplicate,
                  text, chars, density, span)


def _enc_page_out(o: _PageOut) -> list:
    p = o.page
    return [[p.page_no, p.text, p.method, p.quality, p.ok, _enc_geometry(p.geometry), p.printed_label],
            [_enc_line(ln) for ln in o.lines], [_enc_table(t) for t in o.tables],
            [_enc_region(r) for r in o.pictures], o.raw_undecided is not None, o.raw_texts, o.width, o.height,
            o.corruption, o.warnings, o.frame, o.spaced]


def _dec_page_out(d: list) -> _PageOut:
    page, lines, tables, pictures, undecided, raw_texts, width, height, corruption, warnings, frame, spaced = d
    geometry = None if page[5] is None else PageGeometry(*page[5])
    return _PageOut(
        PageResult(page[0], page[1], page[2], page[3], page[4], geometry, page[6]),
        [_Line(*ln) for ln in lines], [_dec_table(t) for t in tables], [_dec_region(r) for r in pictures], [],
        raw_texts if undecided else None, raw_texts, width, height, [], corruption, warnings,
        None if frame is None else tuple(frame), spaced)


def _enc_content(c: ContentInfo) -> dict:
    return asdict(c)


def _dec_content(d: dict) -> ContentInfo:
    return ContentInfo(d["content_hash"], d["kind"], d["crop_scale"], d["dpi"], d["large"],
                       [Occurrence(**o) for o in d["occurrences"]])


def _enc_result(r: ContentResult) -> dict:
    return {"covered": r.covered, "reading": asdict(r.reading) if r.reading is not None else None}


def _dec_result(d: dict) -> ContentResult:
    return ContentResult(d["covered"], PictureReading.from_json(d["reading"]) if d["reading"] is not None else None)


# --- a run -------------------------------------------------------------------------------------------------------

class _Run:
    """One call's pacing: before each unit of work, stop (soft) or fail (hard) when the time is spent. A run that
    stops after finishing at least one unit has made progress."""

    def __init__(self, deadline: float, soft: bool, progress: Progress | None):
        self.deadline, self.soft, self.progress = deadline, soft, progress
        self.units = 0
        self._longest: dict[str, float] = {}
        self._kind = ""
        self._t0 = 0.0

    def begin(self, kind: str) -> None:
        """Called before a unit. Soft: stop when the time is spent, or when the longest unit of this kind this run
        would not fit in what is left. Hard: a spent deadline is a permanent error."""
        if self.soft:
            left = self.deadline - _now()
            if left <= 0 or (self.units > 0 and left < self._longest.get(kind, 0.0)):
                raise ExtractionPaused(self.units > 0)
        else:
            check_deadline(self.deadline)
        self._kind, self._t0 = kind, _now()

    def end(self) -> None:
        self.units += 1
        self._longest[self._kind] = max(self._longest.get(self._kind, 0.0), _now() - self._t0)

    def report(self, done: int, total: int, stage: str) -> None:
        if self.progress is not None:
            self.progress(done, total, stage)


def read_pdf(data: bytes, deadline: float, settings: Settings, vision: VisionReader | None = None,
             readings: ReadingCache | None = None, stage: MemoryStage | StorageStage | None = None,
             progress: Progress | None = None, soft: bool = False) -> ExtractionResult:
    """Read a PDF in page windows through ``stage`` (None: in memory, in this call). ``soft``: the time running out
    stops the run at a unit boundary (``ExtractionPaused``) instead of failing it permanently; ``progress`` gets
    (pages read, pages in the file, stage) as the reading advances."""
    stage = stage if stage is not None else MemoryStage()
    run = _Run(deadline, soft, progress)
    doc = pdf.open_pdf(data, settings.max_pages)
    try:
        try:
            return _read(doc, data, settings, vision, readings, stage, run)
        except StageLost:
            log.warning("staged outputs are unreadable; reading the file again from its first page")
            stage.reset()
            return _read(doc, data, settings, vision, readings, stage, run)
    except ExtractionError as exc:
        if soft and exc.reason == MSG_DEADLINE:  # inside a unit: that unit is dropped, the finished ones stay
            raise ExtractionPaused(run.units > 0) from None
        raise
    finally:
        doc.close()


class _Reading:
    """One call's walk through the steps over a file: the open document, the stage with its state, the pacing."""

    def __init__(self, doc, data: bytes, settings: Settings, vision, cache, stage, run: _Run):
        self.doc, self.data, self.settings, self.vision, self.cache = doc, data, settings, vision, cache
        self.stage, self.run = stage, run
        self.n = len(doc)
        self.width = max(1, settings.ingest_window_pages)
        self.windows = math.ceil(self.n / self.width)
        if stage.state.get("version") != STATE_VERSION or stage.state.get("pages") != self.n \
                or stage.state.get("window") != self.width:
            stage.reset()  # nothing staged, or staged for another file or window size
            stage.state.update(
                version=STATE_VERSION, pages=self.n, window=self.width, windows_done=0, plumber_failed=False,
                reread_next=0, orient_next=0, numbers=[], labels_next=0, find_next=0, contents={}, read_done=[],
                read_batches=0, apply_next=0)
        self.st = stage.state
        stage.commit()

    def unit(self, kind: str, stage_name: str | None = None) -> None:
        self.run.begin(kind)
        if stage_name:
            self.run.report(self.n, self.n, stage_name)

    def done(self) -> None:
        self.stage.commit()
        self.run.end()

    # 1. pass 1, one window at a time
    def read_pages(self) -> None:
        st, w_pages = self.st, self.width
        for w in range(st["windows_done"], self.windows):
            self.run.begin("window")
            self.run.report(w * w_pages, self.n, STAGE_READING)
            outs, no_text_layer = pdf.read_window(self.doc, self.data, w * w_pages, min(self.n, (w + 1) * w_pages),
                                                  self.settings, self.run.deadline)
            self.stage.put_window(w, outs)
            st["windows_done"] = w + 1
            st["plumber_failed"] = st["plumber_failed"] or no_text_layer
            self.done()
            self.run.report(min(self.n, (w + 1) * w_pages), self.n, STAGE_READING)

    # 2. broken font maps: detected over every page's words, then only the pages holding a suspect character are
    # read again, from a corrected text layer
    def repair_fonts(self) -> FontMapFix:
        st, stage = self.st, self.stage
        if "fix" not in st:
            self.unit("fonts", STAGE_FONTS)
            words = (w for k in range(self.windows) for page in stage.words(k) for w in page)
            st["fix"] = fontmap.detect_and_repair(words, self.doc, FontMapConfig.from_settings(self.settings)).to_json()
            self.done()
        fix = FontMapFix.from_json(st["fix"])
        for w in range(st["reread_next"], self.windows):
            again = sorted(i for i in fix.pages if w * self.width <= i < (w + 1) * self.width)
            if again:
                self.unit("reread", STAGE_FONTS)
                fresh, _ = pdf.read_window(self.doc, self.data, 0, 0, self.settings, self.run.deadline, fix, only=again)
                outs, chars = stage.get_window(w), stage.chars(w)
                for index, out in zip(again, fresh, strict=True):
                    k = index - w * self.width
                    chars[k], out.chars = out.chars, []
                    outs[k] = out
                stage.set_chars(w, chars)
                stage.update_window(w, outs)
                st["reread_next"] = w + 1
                self.done()
            else:
                st["reread_next"] = w + 1
        stage.drop_words(self.windows)
        return fix

    # 3. orientation (a page whose words decided nothing takes the document's) and printed page labels
    def orient_and_label(self) -> None:
        st, stage = self.st, self.stage
        if "visual" not in st:
            self.unit("layout", STAGE_LAYOUT)
            logical = visual = 0
            for w in range(self.windows):
                lg, vs = pdf.decided_evidence(stage.get_window(w))
                logical, visual = logical + lg, visual + vs
            st["visual"] = pdf.visual_from_evidence(logical, visual)
            st["labels"] = pdf.document_labels(self.doc, self.n)
            self.done()
        labels = st["labels"]
        for w in range(st["orient_next"], self.windows):
            self.run.begin("layout")
            outs = stage.get_window(w)
            pdf.orient_pages(outs, st["visual"])
            if any(labels):
                for out in outs:
                    out.page.printed_label = labels[out.page.page_no - 1] or None
            else:
                st["numbers"] += [[p, v] for p, v in pdf.page_numbers(outs).items()]
            stage.update_window(w, outs)
            st["orient_next"] = w + 1
            self.done()
        numbers = {p: v for p, v in st["numbers"]}
        if not any(labels) and pdf.consistent_numbers(numbers):
            for w in range(st["labels_next"], self.windows):
                self.run.begin("layout")
                outs = stage.get_window(w)
                for out in outs:
                    if out.page.page_no in numbers:
                        out.page.printed_label = str(numbers[out.page.page_no])
                stage.update_window(w, outs)
                st["labels_next"] = w + 1
                self.done()

    # 4. regions the text layer does not cover: found per window, each distinct content read in batches, then every
    # region given its reading
    def read_regions(self) -> None:
        st, stage = self.st, self.stage
        contents = {h: _dec_content(c) for h, c in st["contents"].items()}
        for w in range(st["find_next"], self.windows):
            self.unit("regions", STAGE_REGIONS)
            outs = stage.get_window(w)
            for o, c in zip(outs, stage.chars(w), strict=True):
                o.chars = c
                o.pictures[:] = [r for r in o.pictures if r.kind != "ink"]  # a window found again starts clean
                for r in o.pictures:
                    r.reading = None
            index_regions(self.doc, self.data, _layers(outs, w * self.width), contents)
            stage.update_window(w, outs)
            st["contents"] = {h: _enc_content(c) for h, c in contents.items()}
            st["find_next"] = w + 1
            self.done()
        stage.drop_chars(self.windows)
        done = set(st["read_done"])
        pending = [c for h, c in contents.items() if h not in done]
        transient = None
        for k in range(0, len(pending), REGION_BATCH):
            self.unit("read", STAGE_REGIONS)
            results, failed = read_contents(self.doc, pending[k:k + REGION_BATCH], self.settings, self.vision,
                                            self.cache, self.run.deadline)
            stage.put_blob(f"r{st['read_batches']:06d}", {h: _enc_result(r) for h, r in results.items()})
            st["read_batches"] += 1
            st["read_done"] += list(results)
            transient = transient or failed
            self.stage.commit()
            if results:  # a batch that read nothing changed no durable reading: it is no progress (``requeue_paused``)
                self.run.end()
        if transient is not None:
            raise transient  # the contents that failed are read again by the next attempt, the rest are kept
        if "repeated" not in st:
            self.give_readings(contents)

    def give_readings(self, contents: dict[str, ContentInfo]) -> None:
        """Every region gets its content's reading, then the pictures repeated through the document are marked."""
        st, stage, width = self.st, self.stage, self.width
        results: dict[str, ContentResult] = {}
        for k in range(st["read_batches"]):
            results |= {h: _dec_result(r) for h, r in stage.get_blob(f"r{k:06d}").items()}
        places = occurrence_places(contents)
        for w in range(st["apply_next"], self.windows):
            self.run.begin("regions")
            outs = stage.get_window(w)
            apply_results(_layers(outs, w * width), places, results)
            stage.update_window(w, outs)
            st["apply_next"] = w + 1
            self.done()
        self.run.begin("regions")
        layers = []
        for w in range(self.windows):  # the regions only: the pages themselves are dropped again
            layers += [layer for layer in _layers(stage.get_window(w), w * width) if layer.regions]
        repeated = mark_repeated(layers)
        flags = {(layer.index, ri): (r.furniture, r.duplicate)
                 for layer in layers for ri, r in enumerate(layer.regions) if r.furniture or r.duplicate}
        for w in range(self.windows):
            if not any(w * width <= p < (w + 1) * width for p, _ in flags):
                continue
            outs = stage.get_window(w)
            for layer in _layers(outs, w * width):
                for ri, r in enumerate(layer.regions):
                    r.furniture, r.duplicate = flags.get((layer.index, ri), (False, False))
            stage.update_window(w, outs)
        st["repeated"] = repeated
        self.done()

    # 5. the blocks, tables and chunks of the whole document
    def assemble(self, fix: FontMapFix) -> ExtractionResult:
        self.unit("assemble", STAGE_ASSEMBLING)
        warnings = [WARN_PLUMBER] if self.st["plumber_failed"] else []
        result = pdf.assemble_result((self.stage.get_window(w) for w in range(self.windows)), self.n, warnings, fix,
                                     self.st["repeated"])
        self.run.end()
        return result


def _read(doc, data: bytes, settings: Settings, vision, cache, stage, run: _Run) -> ExtractionResult:
    reading = _Reading(doc, data, settings, vision, cache, stage, run)
    reading.read_pages()
    fix = reading.repair_fonts()
    reading.orient_and_label()
    reading.read_regions()
    return reading.assemble(fix)
