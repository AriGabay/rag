"""Reading pictures embedded in documents.

Three readers, chosen per picture:

- vector metafiles (EMF): their own text records (``app.extraction.emf``) — exact, no OCR; a metafile that only
  wraps a bitmap hands the bitmap on to the raster path;
- raster pictures (PNG, JPEG, BMP, GIF, TIFF): Tesseract OCR first decides whether the picture carries text at
  all (a photo of a building does not). A picture with text is then read by the vision model when the office
  allows cloud use (``VisionReader``), and the OCR words check that reading: numbers the model wrote that OCR
  did not see make the reading ``read_uncertain``. Without the vision model, the OCR reading itself is kept as
  ``read_uncertain``;
- anything else is ``unread`` with the reason, so the document is reported as partly indexed.

A reading never invents structure: a table keeps its title, header, rows and notes as read; a drawing or map
keeps a one-line description and only the labels that were legible.
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

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
MIN_SIDE = 40
MIN_PIXELS = 120 * 60
OCR_MIN_WORDS = 5  # confident words below which a picture is taken as carrying no text
OCR_MIN_CONFIDENCE = 60.0
OCR_TARGET_WIDTH = 1600
AGREEMENT_OK = 0.8
OCR_RELIABLE_WORDS = 8  # without the vision model, OCR text is kept only when this many words were confident
OCR_RELIABLE_SHARE = 0.6  # ... and they are this share of all the words Tesseract found
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
    def read(self, png: bytes, context: str, careful: bool = False) -> VisionOut | None:
        """The model's reading of one picture, or None when the call failed (the caller falls back to OCR).
        ``careful``: a second, slower reading after the first one failed the OCR cross-checks."""


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


def _grayscale(data: bytes):
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("L")


def _png(gray) -> bytes:
    buf = io.BytesIO()
    gray.save(buf, "PNG")
    return buf.getvalue()


def _ocr_words(gray, languages: str) -> list[dict] | None:
    """Tesseract's words, [] without OCR, None when OCR failed on this picture."""
    from PIL import Image

    from app.extraction.ocr import _words, ocr_available

    if not ocr_available(languages):
        return []
    img = gray
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


def read_raster(data: bytes, context: str, vision: VisionReader | None, languages: str) -> PictureReading:
    try:
        gray = _grayscale(data)
    except Exception:  # noqa: BLE001
        return PictureReading("unread", "none", note="לא ניתן לפענח את קובץ התמונה")
    w, h = gray.size
    if min(w, h) < MIN_SIDE or w * h < MIN_PIXELS:
        return PictureReading("decorative", "none", note="תמונה קטנה (סמל או קישוט)")
    words = _ocr_words(gray, languages)
    from app.extraction.ocr import ocr_available

    if words is None:
        if vision is None:
            return PictureReading("unread", "none", note="OCR נכשל על התמונה")
        words = []  # the vision model reads it; nothing to check its numbers against
        ocr_failed = True
    else:
        ocr_failed = False
    confident = _confident(words)

    if not ocr_available(languages) and vision is None:
        return PictureReading("unread", "none", note="אין OCR זמין ואין קריאה חזותית")
    if ocr_available(languages) and not ocr_failed and len(confident) < OCR_MIN_WORDS:
        return PictureReading("no_text", "ocr", note="תמונה ללא טקסט קריא", kind="photo")
    if vision is not None:
        png = _png(gray) if gray.size[0] * gray.size[1] < 4_000_000 else data
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
    from app.extraction.ocr import ocr_page_image

    try:
        page = ocr_page_image(gray.convert("L"), languages)
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
