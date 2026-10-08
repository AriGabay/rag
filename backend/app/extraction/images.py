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

    @classmethod
    def from_json(cls, data: dict) -> PictureReading:
        """A reading as stored (``to_json``) in a readings cache."""
        data = dict(data)
        data["tables"] = [PictureTable(**t) for t in data.get("tables") or []]
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


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
MSG_VISION_TRANSIENT = "קריאת התמונות במודל נכשלה זמנית ({status}); העיבוד ינוסה שוב והקריאה הקודמת נשמרת"
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


def _ocr_words(gray, languages: str) -> list[dict] | None:
    from PIL import Image

    from app.extraction.ocr import _words, ocr_available

    if not ocr_available(languages):
        return []
    img = without_rules(gray)
    if img.size[0] < OCR_TARGET_WIDTH:
        scale = OCR_TARGET_WIDTH / img.size[0]
        img = img.resize((OCR_TARGET_WIDTH, max(1, int(img.size[1] * scale))), Image.Resampling.LANCZOS)
    try:
        return _words(img, languages)
    except Exception as exc:  # noqa: BLE001 - a Tesseract failure leaves the picture unread, not the document
        logger.warning("ocr failed on a picture: %s", type(exc).__name__)
        return None


def _confident(words: list[dict]) -> list[str]:
    return [w["text"] for w in words if w["conf"] >= OCR_MIN_CONFIDENCE and len(w["text"]) >= 2
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
    return _from_ocr(gray, languages, note="נקרא ב-OCR בלבד; ייתכנו שגיאות זיהוי")


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
