"""Reading pictures embedded in documents.

Three readers, chosen per picture:

- vector metafiles (EMF): their own text records (``app.extraction.emf``) — exact, no OCR; a metafile that only
  wraps a bitmap hands the bitmap on to the raster path;
- raster pictures (PNG, JPEG, BMP, GIF, TIFF) and PDF page regions: Tesseract OCR and a look at the pixels first
  decide whether the picture carries text at all (a photo of a building does not). A picture with text is then
  read by the vision model when the office allows cloud use (``VisionReader``), and the OCR words check that
  reading: numbers the model wrote that OCR did not see make the reading ``read_uncertain``. Without the vision
  model, the OCR reading itself is kept as ``read_uncertain``;
- anything else is ``unread`` with the reason, so the document is reported as partly indexed.

No picture is dismissed for being small: only a picture too thin to hold a glyph is ``decorative``, and a blank
one has no text. Few OCR words alone never prove a picture has no text: a table drawn as a picture (ruled, or
mostly numbers), a low-resolution picture (PDF) and a large region (PDF) go to the vision model instead.

A PDF region (``RegionHints``) is strict about the model: a transient failure (timeout, rate limit, network) or a
configuration error (key, quota, model) raises ``VisionUnavailable``, which fails the ingestion job instead of
leaving the region silently unread; a refusal, invalid or truncated output gets one careful retry and then leaves
the region ``unread`` (``read_uncertain`` from OCR when OCR read text) with the status as its reason. A DOCX
picture keeps its OCR fallback on any model failure.

A reading never invents structure: a table keeps its title, header, rows and notes as read; a drawing or map
keeps a one-line description and only the labels that were legible.

A reading made on demand during a chat turn (``inspect``) also keeps what OCR of the same crop and the region's
text layer confirm of it (``ocr_evidence``, ``PictureReading.ocr``): per number, how often OCR saw it and where; per
table cell, whether its number was seen once in the cell's place — inside its row's and its column's bands, located
by their own words or by the grid of the numbers themselves (``cell_evidence``, KTD6, R18).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Protocol

from app.extraction.base import ExtractionError
from app.extraction.emf import EmfTable, bitmap_to_png, read_emf

logger = logging.getLogger(__name__)


def _limit_tesseract_threads() -> None:
    """Pictures are read in a small thread pool and Tesseract is itself multi-threaded; one thread per Tesseract
    call keeps the CPUs from being oversubscribed (calls timed out under load). The limit goes to the Tesseract
    child processes only: in this process it would also throttle the embedding model."""
    try:
        import pytesseract.pytesseract as pt

        pt.environ = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    except Exception:  # noqa: BLE001 - without pytesseract there is no OCR to limit
        pass


_limit_tesseract_threads()

RASTER_EXTENSIONS = {"png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff"}
THIN_SIDE = 6  # pixels: a picture thinner than this is a line or a bar, it cannot hold a glyph
BLANK_STDDEV = 2.0  # gray-level spread under which a picture is blank
PHOTO_LEVELS = 10  # of 32 gray-level bands, this many each holding 1% of the pixels: a continuous-tone photo
RULE_SHARE = 0.6  # a row or column of ink this long relative to the picture is a table rule
LOW_DPI = 150  # a PDF picture printed at less than this many pixels per inch is read by the model
NUMERIC_WORDS = 4  # confident OCR words with digits that make a picture table-like (with NUMERIC_SHARE)
NUMERIC_SHARE = 0.3
VISION_MAX_SIDE = 2048  # long side of a picture sent to the model: the bound of a high-detail reading
CONTEXT_CHARS = 400  # characters of the document text before a DOCX picture shown to the model with it
OCR_MIN_WORDS = 5  # without the vision model, confident words below which a picture is taken as carrying no text
OCR_MIN_CONFIDENCE = 60.0
OCR_TARGET_WIDTH = 1600
AGREEMENT_OK = 0.8
OCR_RELIABLE_WORDS = 8  # without the vision model, OCR text is kept only when this many words were confident
OCR_RELIABLE_SHARE = 0.6  # ... and they are this share of all the words Tesseract found
OCR_REGION_SHARE = 0.8  # a PDF region is read by OCR alone when this share of its words was read with confidence
_NUM = re.compile(r"\d[\d,.]*")


@dataclass
class PictureTable:
    headers: list[str]
    rows: list[list[str]]
    title: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class PictureReading:
    status: str  # read | read_uncertain | no_text | decorative | unread
    method: str  # emf | ocr | vision | none
    text: str = ""  # lines of text outside tables, logical order
    tables: list[PictureTable] = field(default_factory=list)
    note: str | None = None  # the reason for unread/uncertain, or what a non-text picture shows
    kind: str | None = None  # table | text | diagram | map | photo | chart | signature | other
    cacheable: bool = True  # False for a fallback after a failed model call: the next ingestion tries again
    # an on-demand (``inspect``) reading only: what OCR of the same crop and the region's text layer confirm of it
    # (``ocr_evidence``) and the frame the crop was rendered in (``app.chat.tools``), KTD6
    ocr: dict | None = None

    @classmethod
    def from_json(cls, data: dict) -> PictureReading:
        """A reading as stored (``to_json``) in a readings cache."""
        data = dict(data)
        data["tables"] = [PictureTable(**t) for t in data.get("tables") or []]
        return cls(**data)

    def to_json(self) -> str:
        out = asdict(self)
        if out["ocr"] is None:  # a reading without on-demand evidence is stored as before it existed
            del out["ocr"]
        return json.dumps(out, ensure_ascii=False)


@dataclass
class VisionTableOut:
    title: str
    headers: list[str]
    rows: list[list[str]]
    notes: list[str]


@dataclass
class VisionOut:
    kind: str
    legible: bool
    text: str
    tables: list[VisionTableOut]
    description: str
    uncertain: list[str]


class VisionReader(Protocol):
    def read(self, png: bytes, context: str, careful: bool = False,
             deadline: float | None = None) -> VisionOut | None:
        """The model's reading of one picture. A failed call raises ``VisionCallFailed`` with the provider's status
        (None: no reading, cause unknown). ``careful``: a second, slower reading after the first one failed the OCR
        cross-checks or the call failed deterministically. ``deadline`` (``time.monotonic()``; a chat turn's
        reading deadline, None for ingestion): the call is cut to it, without SDK retries. A reader may carry
        ``config`` (model and effort), part of the key its readings are cached by."""


TRANSIENT_STATUSES = frozenset({"timeout", "rate_limited", "error"})
CONFIG_STATUSES = frozenset({"auth", "quota", "model_unavailable"})
MSG_VISION_TRANSIENT = "קריאת התמונות במודל נכשלה זמנית ({status}); הקריאה הקודמת נשמרת"
MSG_VISION_CONFIG = "קריאת התמונות במודל אינה זמינה בשל הגדרות הספק ({status}); יש לבדוק מפתח, מכסה ומודל"
NOTE_VISION_FAILED = "הקריאה החזותית נכשלה ({status})"


class VisionCallFailed(Exception):  # noqa: N818 - a call outcome, raised by readers
    """A vision call that produced no reading. ``status``: the provider's ``CallStatus`` value."""

    def __init__(self, status: str, detail: str | None = None):
        super().__init__(f"vision call failed: {status}")
        self.status, self.detail = str(status), detail


class VisionUnavailable(ExtractionError):  # noqa: N818 - an ExtractionError the worker records on the job
    """The vision model could not be reached for a PDF region: transient (retried, ``permanent`` False) or a
    configuration error (``permanent``). Either fails the job; the version keeps its earlier reading."""

    def __init__(self, status: str):
        self.status = status
        transient = status not in CONFIG_STATUSES
        super().__init__((MSG_VISION_TRANSIENT if transient else MSG_VISION_CONFIG).format(status=status),
                         permanent=not transient)


@dataclass
class RegionHints:
    """What is known about a PDF region beyond its pixels. Its presence makes the reading strict about the model
    (see the module docstring). ``words``: OCR words already taken (``words_taken``), so they are not taken twice."""

    dpi: float | None = None  # effective resolution: the picture's pixels over its printed size
    large: bool = False  # covers a large share of its page
    words: list[dict] | None = None
    words_taken: bool = False


def _from_emf_table(t: EmfTable) -> PictureTable:
    return PictureTable(headers=t.headers, rows=t.rows, title=t.title, notes=t.notes)


def read_picture(data: bytes, ext: str, context: str, vision: VisionReader | None,
                 ocr_languages: str) -> PictureReading:
    ext = ext.lower().lstrip(".")
    if ext in ("emf", "wmf"):
        if ext == "wmf":
            return PictureReading("unread", "none", note="תמונה וקטורית בפורמט WMF אינה נקראת")
        content = read_emf(data)
        if content.has_text:
            return PictureReading("read", "emf", text="\n".join(content.lines),
                                  tables=[_from_emf_table(t) for t in content.tables], kind="table"
                                  if content.tables else "text")
        if content.bitmaps:
            png = bitmap_to_png(content.bitmaps[0])
            if png:
                return read_raster(png, context, vision, ocr_languages)
        return PictureReading("no_text", "emf", note="תמונה וקטורית ללא טקסט")
    if ext == "svg":
        return _read_svg(data)
    if ext in RASTER_EXTENSIONS:
        return read_raster(data, context, vision, ocr_languages)
    return PictureReading("unread", "none", note=f"פורמט תמונה שאינו נתמך ({ext})")


def _read_svg(data: bytes) -> PictureReading:
    try:
        from lxml import etree

        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))
        texts = [" ".join("".join(t.itertext()).split()) for t in root.iter("{*}text")]
        texts = [t for t in texts if t]
    except Exception:  # noqa: BLE001
        return PictureReading("unread", "none", note="קובץ SVG פגום")
    if not texts:
        return PictureReading("no_text", "none", note="תמונה וקטורית ללא טקסט")
    return PictureReading("read", "svg", text="\n".join(texts), kind="text")


def flatten(im):
    """A picture as 8-bit grayscale over white (transparent parts become white, not black)."""
    from PIL import Image

    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("L")


def _grayscale(data: bytes):
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    return flatten(im)


def _png(gray) -> bytes:
    buf = io.BytesIO()
    gray.save(buf, "PNG")
    return buf.getvalue()


def _vision_png(gray) -> bytes:
    """The picture for the model: its own pixels, the long side capped to the high-detail bound."""
    from PIL import Image

    w, h = gray.size
    if max(w, h) > VISION_MAX_SIDE:
        scale = VISION_MAX_SIDE / max(w, h)
        gray = gray.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
    return _png(gray)


def _shift(img, dx: int, dy: int):
    from PIL import Image

    out = Image.new("L", img.size, 0)
    out.paste(img, (dx, dy))
    return out


def long_runs(ink, k_h: int, k_v: int):
    """Mask of the long horizontal and vertical runs in ``ink`` (ink = 255): rules, borders and solid fills. A run
    is found where a whole cell of ``k`` pixels along it is ink (guaranteed for a run of ``2k``); the cell's
    neighbours along the run are marked too, so a rule's ends go with it."""
    from PIL import Image, ImageChops

    w, h = ink.size
    mask = Image.new("L", (w, h), 0)
    for horizontal, k in ((True, k_h), (False, k_v)):
        cells = w // k if horizontal else h // k
        if cells < 1:
            continue
        span = (cells * k, h) if horizontal else (w, cells * k)
        small = ink.crop((0, 0, *span)).resize((cells, h) if horizontal else (w, cells), Image.Resampling.BOX)
        full = small.point(lambda v: 255 if v >= 250 else 0)
        step = (1, 0) if horizontal else (0, 1)
        grown = ImageChops.lighter(full, ImageChops.lighter(_shift(full, *step), _shift(full, -step[0], -step[1])))
        layer = Image.new("L", (w, h), 0)
        layer.paste(grown.resize(span, Image.Resampling.NEAREST), (0, 0))
        mask = ImageChops.lighter(mask, layer)
    return mask


def without_rules(gray):
    """The picture with its table rules and borders whitened: Tesseract reads a ruled table's cells as text
    instead of the lines as glyphs."""
    from PIL import Image

    w, h = gray.size
    # a rule spans much of the picture; a glyph's stroke, even in a large title, does not
    mask = long_runs(gray.point(lambda p: 255 if p < 160 else 0), max(40, w // 8), max(40, h // 4))
    if mask.getbbox() is None:
        return gray
    return Image.composite(Image.new("L", gray.size, 255), gray, mask)


def ocr_words(gray, languages: str) -> list[dict] | None:
    """Tesseract's words, [] without OCR, None when OCR failed on this picture."""
    return _ocr_words(gray, languages)


def ocr_upscale(width: int) -> float:
    """How much ``_ocr_words`` enlarges a picture ``width`` pixels wide before OCR: its words' boxes are in the
    enlarged picture's pixels."""
    return OCR_TARGET_WIDTH / width if 0 < width < OCR_TARGET_WIDTH else 1.0


def _ocr_words(gray, languages: str) -> list[dict] | None:
    from PIL import Image

    from app.extraction.ocr import _words, ocr_available

    if not ocr_available(languages):
        return []
    img = without_rules(gray)
    if img.size[0] < OCR_TARGET_WIDTH:
        scale = ocr_upscale(img.size[0])
        img = img.resize((OCR_TARGET_WIDTH, max(1, int(img.size[1] * scale))), Image.Resampling.LANCZOS)
    try:
        return _words(img, languages)
    except Exception as exc:  # noqa: BLE001 - a Tesseract failure leaves the picture unread, not the document
        logger.warning("ocr failed on a picture: %s", type(exc).__name__)
        return None


def _confident(words: list[dict]) -> list[str]:
    return [w["text"] for w in confident_words(words) if len(w["text"]) >= 2]


def confident_words(words: list[dict]) -> list[dict]:
    """``_confident`` keeping each word with its box; a single digit (a cell writing "3") is kept too."""
    return [w for w in words if w["conf"] >= OCR_MIN_CONFIDENCE and (len(w["text"]) >= 2 or w["text"].isdigit())
            and re.search(r"[א-ת\dA-Za-z]", w["text"])]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def agreement(vision_numbers: list[str], ocr_words: list[str]) -> float | None:
    """Share of the multi-digit numbers in the model's reading that OCR also saw (digits only, so "1,251,300"
    and "1251300" agree). None when the reading has no such number."""
    nums = [_digits(n) for n in vision_numbers if len(_digits(n)) >= 2]
    if not nums:
        return None
    seen = {_digits(w) for w in ocr_words}
    seen |= {_digits(m) for w in ocr_words for m in _NUM.findall(w)}
    return sum(1 for n in nums if n in seen) / len(nums)


@dataclass
class _Look:
    """What the pixels alone say about a picture."""

    thin: bool
    blank: bool
    photo: bool  # continuous tone: many gray levels, as in a photograph
    ruled: bool  # several long horizontal rules and vertical rules: a ruled table


def _look(gray) -> _Look:
    from PIL import Image, ImageStat

    w, h = gray.size
    if min(w, h) < THIN_SIDE:
        return _Look(True, False, False, False)
    if ImageStat.Stat(gray).stddev[0] < BLANK_STDDEV:
        return _Look(False, True, False, False)
    hist = gray.histogram()
    total = w * h
    levels = sum(1 for k in range(32) if sum(hist[k * 8:(k + 1) * 8]) >= 0.01 * total)
    ink = gray.point(lambda p: 255 if p < 160 else 0)

    def rules(profile: bytes) -> int:
        """Thin runs of long ink standing out from what is around them (a photograph's dark band is no rule)."""
        n, thin = len(profile), max(3, len(profile) // 30)
        runs, start = 0, None
        for i, v in enumerate([*profile, 0]):
            if v >= RULE_SHARE * 255:
                start = i if start is None else start
                continue
            if start is not None and i - start <= thin:
                before, after = profile[max(0, start - 2)], profile[min(n - 1, i + 1)]
                if (start < 2 or before < 0.35 * 255) and (i + 1 >= n or after < 0.35 * 255):
                    runs += 1
            start = None
        return runs

    rows = rules(ink.resize((1, h), Image.Resampling.BOX).tobytes())
    cols = rules(ink.resize((w, 1), Image.Resampling.BOX).tobytes())
    return _Look(False, False, levels >= PHOTO_LEVELS, rows >= 3 and cols >= 2)


def _confident_share(words: list[dict], confident: list[str]) -> float:
    """Share of the word-like things OCR found (letters or digits) that it read with confidence."""
    found = [w for w in words if re.search(r"[א-ת\dA-Za-z]", w["text"])]
    return len(confident) / len(found) if found else 0.0


def _any_text(words: list[dict]) -> bool:
    """Whether OCR found anything word-like at all, even with low confidence."""
    return any(w["conf"] >= 30 and len(w["text"]) >= 2 and re.search(r"[א-ת\dA-Za-z]", w["text"]) for w in words)


def _numeric(confident: list[str]) -> bool:
    numbers = [w for w in confident if len(_digits(w)) >= 2]
    return len(numbers) >= NUMERIC_WORDS and len(numbers) >= NUMERIC_SHARE * len(confident)


def read_raster(data: bytes, context: str, vision: VisionReader | None, languages: str,
                hints: RegionHints | None = None) -> PictureReading:
    try:
        gray = _grayscale(data)
    except Exception:  # noqa: BLE001
        return PictureReading("unread", "none", note="לא ניתן לפענח את קובץ התמונה")
    return read_gray(gray, context, vision, languages, hints)


def read_gray(gray, context: str, vision: VisionReader | None, languages: str,
              hints: RegionHints | None = None) -> PictureReading:
    """Read a grayscale picture: ``hints`` given, as a PDF region (strict about the model); otherwise as a DOCX
    picture."""
    from app.extraction.ocr import ocr_available

    look = _look(gray)
    if look.thin:
        return PictureReading("decorative", "none", note="קו או פס דק")
    if hints is not None and hints.words_taken:
        words = hints.words
    else:
        words = _ocr_words(gray, languages)
    ocr_on = ocr_available(languages)
    ocr_failed = words is None
    if ocr_failed:
        if vision is None:
            return PictureReading("unread", "none", note="OCR נכשל על התמונה")
        words = []  # the vision model reads it; nothing to check its numbers against
    if not ocr_on and vision is None:
        return PictureReading("unread", "none", note="אין OCR זמין ואין קריאה חזותית")
    confident = _confident(words)
    few = len(confident) < OCR_MIN_WORDS
    if look.blank and few:
        return PictureReading("no_text", "none", note="תמונה ריקה", kind="other")
    table_like = look.ruled or _numeric(confident)
    if hints is None:
        return _read_docx_picture(gray, context, vision, languages, words, confident, ocr_on and not ocr_failed,
                                  table_like)

    low_res = hints.dpi is not None and hints.dpi < LOW_DPI
    ocr_ok = ocr_on and not ocr_failed
    share = _confident_share(words, confident)
    legible = ocr_ok and bool(confident) and share >= OCR_RELIABLE_SHARE  # what OCR found, it read with confidence
    reliable = ocr_ok and not few and share >= OCR_REGION_SHARE
    # few OCR words are no proof a picture holds nothing: with the model, it decides (once per content, cached)
    if vision is None and look.photo and not table_like and (few or share < 0.5):
        return PictureReading("no_text", "ocr" if ocr_on else "none", note="צילום ללא טקסט קריא", kind="photo")
    if reliable and not table_like and not low_res:
        return _from_ocr(gray, languages, note="נקרא ב-OCR בלבד; ייתכנו שגיאות זיהוי")
    if vision is not None:
        return _read_strict(gray, vision, languages, confident, ocr_ok)
    if not ocr_ok:
        return PictureReading("unread", "none", note="OCR נכשל על התמונה" if ocr_failed
                              else "אין OCR זמין ואין קריאה חזותית")
    if table_like:
        return PictureReading("unread", "ocr", note="טבלה בתמונה: נדרשת קריאה חזותית, שאינה מופעלת במשרד",
                              kind="table")
    if legible and not low_res:  # a stamp or a logo whose few words OCR read with confidence
        return _from_ocr(gray, languages, note="נקרא ב-OCR בלבד; ייתכנו שגיאות זיהוי")
    if few and not hints.large and (not low_res or not _any_text(words)):
        return PictureReading("no_text", "ocr", note="תמונה ללא טקסט קריא", kind="other")
    if legible:  # low resolution, but OCR read it with confidence
        return _from_ocr(gray, languages, note="נקרא ב-OCR בלבד ברזולוציה נמוכה; ייתכנו שגיאות זיהוי")
    return PictureReading("unread", "ocr", note="בתמונה יש טקסט שלא ניתן היה לקרוא ב-OCR באיכות מספקת")


def _read_strict(gray, vision: VisionReader, languages: str, confident: list[str], ocr_ok: bool) -> PictureReading:
    """A PDF region through the model: no page context (the reading depends on the content alone), one careful
    retry, and failures split by cause."""
    png = _vision_png(gray)
    best: PictureReading | None = None
    failure: str | None = None
    for careful in (False, True):
        try:
            out = vision.read(png, "", careful=careful)
        except VisionCallFailed as exc:
            if exc.status in TRANSIENT_STATUSES or exc.status in CONFIG_STATUSES:
                raise VisionUnavailable(exc.status) from None
            failure = exc.status
            continue
        if out is None:
            failure = failure or "unsupported"
            continue
        reading = _from_vision(out, confident)
        if best is None or _score(reading) > _score(best):
            best = reading
        if best.status in ("read", "no_text"):
            break
    if best is not None:
        return best
    note = NOTE_VISION_FAILED.format(status=failure)
    if ocr_ok and len(confident) >= OCR_MIN_WORDS:
        fallback = _from_ocr(gray, languages, note=note + "; נקרא ב-OCR בלבד")
        if fallback.status == "read_uncertain":
            fallback.cacheable = False
            return fallback
    return PictureReading("unread", "none", note=note, cacheable=False)


def _read_docx_picture(gray, context: str, vision: VisionReader | None, languages: str, words: list[dict],
                       confident: list[str], ocr_ok: bool, table_like: bool) -> PictureReading:
    """A DOCX picture: with the model, the model reads it (few confident OCR words are no proof it holds nothing:
    a scanned plan's labels are words OCR finds but cannot trust); without it, few confident OCR words mean no text
    unless it looks like a table. The model's failures fall back to OCR."""
    if vision is None and ocr_ok and len(confident) < OCR_MIN_WORDS and not table_like:
        return PictureReading("no_text", "ocr", note="תמונה ללא טקסט קריא", kind="photo")
    if vision is not None:
        png = _vision_png(gray)
        best: PictureReading | None = None
        for careful in (False, True):
            out = None
            try:
                out = vision.read(png, context, careful=careful)
            except Exception as exc:  # noqa: BLE001
                logger.warning("vision reading failed: %s", type(exc).__name__)
            if out is None:
                continue
            reading = _from_vision(out, confident)
            if best is None or _score(reading) > _score(best):
                best = reading
            if best.status == "read" or best.status == "no_text":
                break
        if best is not None:
            return best
    if len(confident) < OCR_RELIABLE_WORDS or len(confident) < OCR_RELIABLE_SHARE * len(words):
        # mostly unreadable words (a map, a scanned stamp): OCR text would only add noise to the index
        return PictureReading("unread", "ocr", note="בתמונה יש טקסט שלא ניתן היה לקרוא ב-OCR באיכות מספקת")
    reading = _from_ocr(gray, languages, note="נקרא ב-OCR בלבד; ייתכנו שגיאות זיהוי")
    if vision is not None:
        reading.cacheable = False  # the model was not reached: the next ingestion asks it again
    return reading


_HEB_WORD = re.compile(r"^[\u05D0-\u05EA]{3,}$")
WORD_RECALL_OK = 0.6


def word_recall(ocr_words: list[str], reading_text: str) -> float | None:
    """Share of OCR's confident Hebrew words (three letters or more) that the reading also contains: a reading
    that dropped a column of names (regions, tenants) keeps every number and still loses these."""
    words = {w for w in ocr_words if _HEB_WORD.match(w)}
    if len(words) < 4:
        return None
    seen = set(re.findall(r"[\u05D0-\u05EA]{3,}", reading_text))
    return sum(1 for w in words if w in seen or any(w in s for s in seen if len(s) > len(w))) / len(words)


def _score(r: PictureReading) -> tuple[int, int]:
    return (1 if r.status == "read" else 0, len(r.text) + sum(len(" ".join(c for row in t.rows for c in row))
                                                             for t in r.tables))


def _from_vision(out: VisionOut, ocr_words: list[str]) -> PictureReading:
    tables = [PictureTable(headers=[c.strip() for c in t.headers], rows=[[c.strip() for c in r] for r in t.rows],
                           title=[t.title] if t.title.strip() else [], notes=[n for n in t.notes if n.strip()])
              for t in out.tables if t.rows or t.headers]
    text = out.text.strip()
    if out.kind in ("photo", "signature") or (not text and not tables):
        return PictureReading("no_text", "vision", note=out.description.strip() or None, kind=out.kind)
    numbers = _NUM.findall(text) + [n for t in tables for r in [t.headers, *t.rows] for c in r
                                    for n in _NUM.findall(c)]
    share = agreement(numbers, ocr_words)
    # the other direction: confident OCR numbers the reading left out (a dropped column or row)
    recall = agreement([w for w in ocr_words if len(_digits(w)) >= 3], numbers)
    if recall is not None and (share is None or recall < share):
        share = recall
    all_text = " ".join([text, *[" ".join([*t.title, *t.headers, *[c for r in t.rows for c in r], *t.notes])
                                 for t in tables]])
    words = word_recall(ocr_words, all_text)
    duplicated = any(len([h for h in t.headers if h]) != len({h for h in t.headers if h}) for t in tables)
    uncertain = out.uncertain or not out.legible
    status = "read" if ((share is None or share >= AGREEMENT_OK) and (words is None or words >= WORD_RECALL_OK)
                        and not duplicated and not uncertain) else "read_uncertain"
    note = None
    if status == "read_uncertain":
        parts = []
        if share is not None and share < AGREEMENT_OK:
            parts.append(f"רק {round(share * 100)}% מהמספרים אומתו מול OCR")
        if words is not None and words < WORD_RECALL_OK:
            parts.append(f"חסרות בקריאה מילים ש-OCR זיהה ({round(words * 100)}% נמצאו)")
        if duplicated:
            parts.append("כותרות עמודה כפולות")
        if out.uncertain:
            parts.append("קטעים שקריאתם לא ודאית: " + "; ".join(out.uncertain[:5]))
        note = "; ".join(parts) or "קריאה חלקית"
    if out.kind not in ("table", "text") and out.description.strip():
        text = (out.description.strip() + ("\n" + text if text else "")).strip()
    return PictureReading(status, "vision", text=text, tables=tables, note=note, kind=out.kind)


def _from_ocr(gray, languages: str, note: str) -> PictureReading:
    from PIL import Image

    from app.extraction.ocr import ocr_page_image

    img = gray.convert("L")
    if img.size[0] < OCR_TARGET_WIDTH // 2:  # a small picture: OCR reads it better enlarged, as for its words
        scale = OCR_TARGET_WIDTH / img.size[0]
        img = img.resize((OCR_TARGET_WIDTH, max(1, int(img.size[1] * scale))), Image.Resampling.LANCZOS)
    try:
        page = ocr_page_image(img, languages)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr failed on a picture: %s", type(exc).__name__)
        return PictureReading("unread", "none", note="OCR נכשל")
    tables = []
    for t in page.tables:
        rows = [r for r in t.rows if any(c for c in r)]
        if not rows:
            continue
        tables.append(PictureTable(headers=rows[0], rows=rows[1:]))
    table_lines = {" ".join(c for c in r if c) for t in page.tables for r in t.rows}
    text = "\n".join(line.text for line in page.lines if line.text not in table_lines)
    if not text.strip() and not tables:
        return PictureReading("no_text", "ocr", note="תמונה ללא טקסט קריא")
    return PictureReading("read_uncertain", "ocr", text=text, tables=tables, note=note,
                          kind="table" if tables else "text")


# --- an on-demand reading's numbers, confirmed in their place (KTD6, R18, R19) ----------------------------------------
#
# ``inspect`` reads a region during a chat turn: a model's transcription, which by itself verifies nothing. A cell of
# a table it transcribed is confirmed only when an independent reading of the same pixels puts the cell's number in
# the cell's place: OCR of the same crop (or the region's own text layer) sees the number exactly once, and that
# word's box is where the transcription puts it. Two placements establish that, either one enough:
#
# - by the labels: the box lies in the row band of the row's label words and the column band of its column header,
#   both located by their own word boxes in the crop;
# - by the grid of the numbers (``_grid``), which needs no header or label text (Tesseract garbles Hebrew header
#   text, and the model's label may differ slightly from the drawn one, while the numbers are read once each): the
#   numbers OCR saw once are grouped into OCR lines (overlapping heights) and x bands (overlapping widths). A row is
#   placed on the line where it has more numbers than on any other, which holds more of its numbers than of any other
#   row's, and at least two (the cell and one more: a number alone has no band but its own box, so its place would
#   prove nothing); a column likewise on its x band. The rows' lines must run top to bottom in the transcription's
#   row order, and the columns' bands in the table's reading direction (right to left for a Hebrew table, as the
#   transcription lists them), or the rows or columns out of order stay unplaced. A cell is placed when its number is
#   on its row's line and in its column's band; a number the transcription writes in two cells places neither. A label
#   or a header the words do locate must agree: a row label read on another line, or a header over another band,
#   leaves that row or column unplaced.
#
# A number seen twice, not seen, or seen outside its row or column stays unconfirmed: a transcription that swapped
# two rows' values keeps every number OCR saw and still fails here (each swapped number lies on the other row's
# line). Boxes are in the OCR picture's pixels (the crop as ``_ocr_words`` enlarged it, ``ocr_upscale``);
# ``app.chat.anchors.page_box`` maps them into the rendered page. A reading stored before the grid placement
# (``GRID_PLACEMENT`` absent from its evidence) is placed again from its stored per-number boxes when a cell is taken
# (``placed_cells``), so no stored reading has to be read again.

CELL_CONFIRMED = "confirmed"  # seen once, in the row band of its label and the column band of its header
CELL_NOT_SEEN = "not_seen"  # OCR did not see the number
CELL_REPEATED = "repeated"  # OCR saw the number more than once in the crop: which one is the cell is not known
CELL_NOT_PLACED = "not_placed"  # seen once, but not in its row and column, or they could not be located
CELL_NO_OCR = "no_ocr"  # no OCR of the crop (and no text layer confirmed it)
BY_OCR = "ocr"  # a confirmed cell's ``by``: OCR of the crop confirmed it
BY_TEXT_LAYER = "text_layer"  # a confirmed cell's ``by``: the region's text layer confirmed it
PHRASE_GAP = 1.0  # a gap wider than this many word heights separates two cells on a line
ROW_OVERLAP = 0.5  # the share of the lower of two heights a number and its row label must overlap by
GRID_ANCHORS = 2  # numbers of a row on its line (of a column in its band), the cell's included, to place it by the grid
# the evidence's placement: its cells were placed by the labels and by the numbers' grid (a reading stored without it
# is placed again from its stored number boxes, ``placed_cells``)
GRID_PLACEMENT = "labels+grid"


def _number_key(text: str) -> str | None:
    """The digits of the one number a cell writes, None when it writes none or several."""
    found = _NUM.findall(text or "")
    return _digits(found[0]) or None if len(found) == 1 else None


def _box(w: dict) -> list[float]:
    x0, y0 = float(w["left"]), float(w["top"])
    return [x0, y0, x0 + float(w["width"]), y0 + float(w["height"])]


def _placed(words: list[dict] | None) -> list[dict]:
    """The words that carry a box (Tesseract's always do): only they can place a number in a cell."""
    return [w for w in words or [] if all(w.get(k) is not None for k in ("left", "top", "width", "height"))]


def _label_tokens(text: str) -> tuple[frozenset, frozenset]:
    """A label's words as compared with OCR: (words with a letter, words of digits), punctuation, quote marks and
    currency signs dropped."""
    tokens = [re.sub(r"[^\w]|_", "", t) for t in (text or "").split()]
    tokens = [t for t in tokens if t]
    return (frozenset(t for t in tokens if re.search(r"[^\W\d_]", t)),
            frozenset(t for t in tokens if t.isdigit()))


def _phrases(words: list[dict]) -> list[tuple[frozenset, frozenset, list[float]]]:
    """Runs of words on one line with no wide gap (one cell each, as drawn): their tokens and their joint box."""
    lines: list[list[dict]] = []
    extents: list[list[float]] = []  # each line's running [top, bottom], over its members
    for w in sorted(words, key=lambda w: float(w["top"])):
        b = _box(w)
        for line, extent in zip(lines, extents, strict=True):
            if _same_line(b, [0.0, extent[0], 0.0, extent[1]]):
                line.append(w)
                extent[0], extent[1] = min(extent[0], b[1]), max(extent[1], b[3])
                break
        else:
            lines.append([w])
            extents.append([b[1], b[3]])
    out = []
    for line in lines:
        line.sort(key=lambda w: float(w["left"]))
        heights = sorted(_box(w)[3] - _box(w)[1] for w in line)
        gap = PHRASE_GAP * heights[len(heights) // 2]
        run = [line[0]]
        for w in line[1:]:
            if _box(w)[0] - _box(run[-1])[2] > gap:
                out.append(run)
                run = []
            run.append(w)
        out.append(run)
    phrases = []
    for run in out:
        letters, numbers = _label_tokens(" ".join(w["text"] for w in run))
        boxes = [_box(w) for w in run]
        phrases.append((letters, numbers, [min(b[0] for b in boxes), min(b[1] for b in boxes),
                                           max(b[2] for b in boxes), max(b[3] for b in boxes)]))
    return phrases


def _locate(label: str, phrases) -> list[float] | None:
    """The box of the one phrase that is ``label`` (the same words with a letter, its numbers among them); None when
    no phrase or several are."""
    letters, numbers = _label_tokens(label)
    if not letters:
        return None
    hits = [box for ls, ns, box in phrases if ls == letters and numbers <= ns]
    return hits[0] if len(hits) == 1 else None


def _column_bands(headers: list[str], phrases) -> list[tuple[float, float] | None]:
    """Each header's column band: when every header is located, from the midpoint to its neighbour on each side
    (a number right- or left-aligned under a short header is still in its column); otherwise the header's own box."""
    boxes = [_locate(h, phrases) if h.strip() else None for h in headers]
    named = [i for i, h in enumerate(headers) if h.strip()]
    if not named or any(boxes[i] is None for i in named):
        return [(b[0], b[2]) if b is not None else None for b in boxes]
    order = sorted(named, key=lambda i: (boxes[i][0] + boxes[i][2]) / 2)
    bands: list[tuple[float, float] | None] = [None] * len(headers)
    for k, i in enumerate(order):
        left = (boxes[order[k - 1]][2] + boxes[i][0]) / 2 if k > 0 else float("-inf")
        right = (boxes[i][2] + boxes[order[k + 1]][0]) / 2 if k + 1 < len(order) else float("inf")
        bands[i] = (left, right)
    return bands


def _row_label(row: list[str]) -> str:
    """The cell a row is named by: its first non-empty cell that is not a number."""
    return next((c for c in row if c.strip() and _number_key(c) is None), row[0] if row else "")


def _rtl(table: PictureTable) -> bool:
    """Whether a table reads right to left: its words are mostly Hebrew (a table of numbers alone reads as the
    documents do)."""
    words = " ".join([*table.title, *table.headers, *(c for r in table.rows for c in r)])
    return len(re.findall(r"[א-ת]", words)) >= len(re.findall(r"[A-Za-z]", words))


def _same_line(a: list[float], b: list[float]) -> bool:
    return min(a[3], b[3]) - max(a[1], b[1]) >= ROW_OVERLAP * min(a[3] - a[1], b[3] - b[1])


def _same_band(a: list[float], b: list[float]) -> bool:
    return min(a[2], b[2]) - max(a[0], b[0]) > 0


def _groups(boxes: dict[tuple[int, int], list[float]], same) -> dict[tuple[int, int], int]:
    """Each cell's group: cells joined, through one another, by ``same`` on their boxes."""
    keys = list(boxes)
    parent = {k: k for k in keys}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for x, a in enumerate(keys):
        for b in keys[x + 1:]:
            if same(boxes[a], boxes[b]):
                parent[root(b)] = root(a)
    roots: dict = {}
    return {k: roots.setdefault(root(k), len(roots)) for k in keys}


def _owned(group_of: dict[tuple[int, int], int], axis: int) -> dict[int, int]:
    """Each row (``axis`` 0) or column (1) placed on a group: the group where it has the most of its numbers, more
    than in any other group and at least ``GRID_ANCHORS``, and where no other row (column) has as many."""
    count: dict[tuple[int, int], int] = {}
    for cell, g in group_of.items():
        count[(cell[axis], g)] = count.get((cell[axis], g), 0) + 1
    out = {}
    for who in {cell[axis] for cell in group_of}:
        mine = sorted(((n, g) for (w, g), n in count.items() if w == who), reverse=True)
        n, g = mine[0]
        if n < GRID_ANCHORS or (len(mine) > 1 and mine[1][0] == n):
            continue
        if any(m >= n for (w, h), m in count.items() if h == g and w != who):
            continue
        out[who] = g
    return out


def _out_of_order(placed: dict[int, float], descending: bool) -> set[int]:
    """The rows (columns) whose group's position contradicts their order in the transcription."""
    bad = set()
    for a in placed:
        for b in placed:
            if a < b and (placed[a] <= placed[b] if descending else placed[a] >= placed[b]):
                bad |= {a, b}
    return bad


def _grid(table: PictureTable, hits: dict[str, list]) -> tuple[list[list[list[float] | None]], dict, dict]:
    """The cells the numbers' own grid places (module comment): per row and cell, the confirming box or None; and
    the line (y0, y1) of each placed row and the band (x0, x1) of each placed column. ``hits``: per number (its
    digits), the boxes of the words OCR saw it in (None for a box not kept)."""
    rows = table.rows
    keys: dict[tuple[int, int], str] = {}
    for i, row in enumerate(rows):
        label = _row_label(row)
        for j, cell in enumerate(row):
            key = _number_key(cell)
            if key is not None and cell != label:
                keys[(i, j)] = key
    written: dict[str, int] = {}
    for key in keys.values():
        written[key] = written.get(key, 0) + 1
    boxes = {cell: hits[key][0] for cell, key in keys.items()
             if written[key] == 1 and len(hits.get(key) or []) == 1 and hits[key][0] is not None}
    lines, bands = _groups(boxes, _same_line), _groups(boxes, _same_band)
    row_line, column_band = _owned(lines, 0), _owned(bands, 1)

    def extent(group_of: dict, g: int, lo: int, hi: int) -> tuple[float, float]:
        members = [boxes[c] for c, h in group_of.items() if h == g]
        return min(b[lo] for b in members), max(b[hi] for b in members)

    line_of = {i: extent(lines, g, 1, 3) for i, g in row_line.items()}
    band_of = {j: extent(bands, g, 0, 2) for j, g in column_band.items()}
    bad_rows = _out_of_order({i: (y0 + y1) / 2 for i, (y0, y1) in line_of.items()}, descending=False)
    bad_columns = _out_of_order({j: (x0 + x1) / 2 for j, (x0, x1) in band_of.items()}, descending=_rtl(table))
    out = []
    for i, row in enumerate(rows):
        out.append([boxes[(i, j)] if (i, j) in boxes and i not in bad_rows and j not in bad_columns
                    and lines[(i, j)] == row_line.get(i) and bands[(i, j)] == column_band.get(j) else None
                    for j in range(len(row))])
    return out, {i: line_of[i] for i in line_of if i not in bad_rows}, \
        {j: band_of[j] for j in band_of if j not in bad_columns}


def _number_hits(words: list[dict]) -> dict[str, list[list[float]]]:
    seen: dict[str, list[list[float]]] = {}
    for w in words:
        for key in {_digits(m) for m in _NUM.findall(w["text"])} - {""}:
            seen.setdefault(key, []).append(_box(w))
    return seen


def _placements(table: PictureTable, words: list[dict]) -> list[list[dict | None]]:
    """Each cell's evidence from one set of words (OCR of the crop, or the region's text layer): placed by its row
    label and column header, or by the numbers' grid where no located label or header contradicts it."""
    words = _placed(words)
    phrases = _phrases(words)
    seen = _number_hits(words)
    bands = _column_bands(table.headers, phrases)
    grid, grid_lines, grid_bands = _grid(table, seen)
    # a label or header the words locate where the grid does not put its row or column: the grid is not trusted there
    vetoed_rows = {i for i, (y0, y1) in grid_lines.items()
                   if (b := _locate(_row_label(table.rows[i]), phrases)) is not None
                   and not _same_line(b, [0.0, y0, 0.0, y1])}
    vetoed_columns = {j for j, (x0, x1) in grid_bands.items()
                      if j < len(table.headers) and table.headers[j].strip()
                      and (b := _locate(table.headers[j], phrases)) is not None and not _same_band(b, [x0, 0.0, x1, 0.0])}
    out = []
    for i, row in enumerate(table.rows):
        label = _row_label(row)
        row_box = _locate(label, phrases)
        cells: list[dict | None] = []
        for j, cell in enumerate(row):
            key = _number_key(cell)
            if key is None or cell == label:
                cells.append(None)
                continue
            hits = seen.get(key, [])
            if not hits:
                cells.append({"status": CELL_NOT_SEEN, "box": None, "by": None})
                continue
            if len(hits) > 1:
                cells.append({"status": CELL_REPEATED, "box": None, "by": None})
                continue
            box = hits[0]
            band = bands[j] if j < len(bands) else None
            in_row = row_box is not None and _same_line(box, row_box)
            in_column = band is not None and band[0] <= (box[0] + box[2]) / 2 <= band[1]
            by_grid = grid[i][j] is not None and i not in vetoed_rows and j not in vetoed_columns
            cells.append({"status": CELL_CONFIRMED, "box": box, "by": None} if (in_row and in_column) or by_grid
                         else {"status": CELL_NOT_PLACED, "box": None, "by": None})
        out.append(cells)
    return out


def cell_evidence(tables: list[PictureTable], words: list[dict] | None,
                  layer: list[dict] | None = None) -> list[list[list[dict | None]]]:
    """For each table, row and cell of a transcription: None for a cell without exactly one number; otherwise
    ``{"status", "box", "by"}`` — ``confirmed`` with the confirming word's box (OCR pixels) and ``by`` (``ocr`` or
    ``text_layer``), or why not (``CELL_*``). ``words``: the confident OCR words of the crop with their boxes (None:
    no OCR); ``layer``: the region's text-layer words in the same pixels, accepted under the same placement test."""
    out = []
    for table in tables:
        by_ocr = _placements(table, words or [])
        by_layer = None  # the text layer's placements, computed at the first cell OCR did not confirm
        rows = []
        for i, row in enumerate(by_ocr):
            cells = []
            for j, cell in enumerate(row):
                if cell is None:
                    cells.append(None)
                    continue
                if cell["status"] == CELL_CONFIRMED:
                    cell = cell | {"by": BY_OCR}
                else:
                    if by_layer is None and layer:
                        by_layer = _placements(table, layer)
                    other = by_layer[i][j] if by_layer is not None else None
                    if other is not None and other["status"] == CELL_CONFIRMED:
                        cell = other | {"by": BY_TEXT_LAYER}
                    elif other is not None and other["status"] != CELL_NOT_SEEN and (
                            words is None or cell["status"] == CELL_NOT_SEEN):
                        cell = other  # the text layer saw it, where OCR did not: its reason is the one to give
                    elif words is None:
                        cell = {"status": CELL_NO_OCR, "box": None, "by": None}
                cells.append(cell)
            rows.append(cells)
        out.append(rows)
    return out


def ocr_evidence(reading: PictureReading, words: list[dict] | None, layer: list[dict] | None = None) -> dict:
    """What OCR of the crop (``words``: confident words with boxes; None: no OCR) and the region's text layer confirm
    of an on-demand reading: per transcribed number, how many times OCR saw it and, when once, where; per table
    cell, ``cell_evidence``, placed by the labels and the numbers' grid (``placement``). Kept in the stored reading
    (``PictureReading.ocr``)."""
    texts = [reading.text, *[c for t in reading.tables for r in [t.headers, *t.rows] for c in r]]
    found = _number_hits(_placed(words))
    numbers, keys = [], set()
    for t in texts:
        for written in _NUM.findall(t or ""):
            key = _digits(written)
            if len(key) < 2 or key in keys:
                continue
            keys.add(key)
            hits = found.get(key, [])
            numbers.append({"number": key, "text": written, "seen": len(hits),
                            "box": hits[0] if len(hits) == 1 else None})
    return {"available": words is not None, "numbers": numbers,
            "cells": cell_evidence(reading.tables, words, layer), "placement": GRID_PLACEMENT}


def placed_cells(reading: PictureReading) -> list:
    """The per-cell evidence of a stored on-demand reading (``ocr_evidence``'s ``cells``). A reading stored before the
    numbers' grid placed cells (no ``placement``) is placed again from its stored per-number OCR boxes: a cell its
    labels left unplaced is confirmed when the grid places it (``_grid``; the labels' words were not stored, so only
    the grid's own checks apply). Nothing else changes: a number not seen, seen twice or without OCR stays so."""
    ocr = reading.ocr or {}
    cells = ocr.get("cells") or []
    if ocr.get("placement") or not ocr.get("available"):
        return cells
    hits = {n["number"]: [n.get("box")] if n.get("seen") == 1 else [None] * int(n.get("seen") or 0)
            for n in ocr.get("numbers") or []}
    out = []
    for ti, stored in enumerate(cells):
        if ti >= len(reading.tables):
            out.append(stored)
            continue
        grid, _, _ = _grid(reading.tables[ti], hits)
        rows = []
        for i, row in enumerate(stored):
            rows.append([{"status": CELL_CONFIRMED, "box": grid[i][j], "by": BY_OCR}
                         if cell is not None and cell.get("status") == CELL_NOT_PLACED and i < len(grid)
                         and j < len(grid[i]) and grid[i][j] is not None else cell
                         for j, cell in enumerate(row)])
        out.append(rows)
    return out
