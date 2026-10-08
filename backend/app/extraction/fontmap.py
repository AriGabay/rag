"""Broken font maps: detection per font and repair from the glyphs' own rendering (KTD6, R7, R8).

A PDF font carries a ToUnicode map from its glyph codes to characters. When a producer writes that map wrong, the
page shows the right Hebrew letters but the text layer reads another character for one of them (``'הðכס'`` for
``'הנכס'``). The page still looks like good text to the quality score, so the corruption is found per font:

- a letter of another script (or a private-use glyph) inside otherwise Hebrew words (``hebrew.suspect_positions``);
- a frequent Hebrew letter that a font emitting plenty of Hebrew never produces. This alone is only reported: a
  font used for a few fixed words may well lack a letter, and nothing tells which character stands for it.

A suspect ``font × character`` pair is repaired only on evidence independent of the broken map. pdfium draws the
glyph outlines whatever the ToUnicode map says, so a word crop rendered with pypdfium2 and read by Tesseract
(``heb+eng``) shows which letter the glyph is. (pdfium's own text extraction uses the same broken map and proves
nothing.) Each OCR word is aligned with the extracted word of the same length, in either order, and counts only
when the word's other letters agree; the letters OCR reads at the suspect's places are the votes. A mapping is
accepted with enough agreeing votes and a high agreement share. Two forms of one letter split by word position
(נ inside a word, ן at its end) are accepted as one positional mapping.

An accepted mapping applies to that font's suspect character inside Hebrew words of this document only: other
fonts, Latin words, digits, numbers and symbols are never touched. A pair without an accepted mapping leaves the
text as extracted and marks it uncertain with the reason. The caller keeps the original text next to the
corrected one and stores the correction record (``FontMapFix.report``).

How ``pdf.py`` uses it, per document:

1. while reading each page's text layer, collect ``words_of_page(page, index)`` (the deduplicated page);
2. ``fix = detect_and_repair(words, pdf_doc, FontMapConfig.from_settings(settings))``;
3. re-read the pages in ``fix.pages`` with ``fix.correct_page(page)`` right after ``dedupe_chars``: its ``page``
   reads corrected text everywhere (lines, words, tables), each corrected char keeps ``fontmap_from`` and each
   unrepaired suspect char carries ``fontmap_uncertain`` (the reason);
4. per line: ``fix.original_line(raw_text, logical_text, chars)`` gives the original logical text (``None`` when
   the line was not corrected) and ``fix.uncertain_reason(chars)`` the reason its block is ``read_uncertain``;
5. the page quality takes ``quality_score(text, corruption=correction.corruption)``.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol

from PIL import Image

from app.extraction import ocr
from app.extraction.hebrew import (
    is_hebrew_letter,
    letter_like,
    strip_bidi_controls,
    suspect_positions,
    visual_to_logical,
)

log = logging.getLogger(__name__)

REASON_NO_OCR = "בשכבת הטקסט יש תווים זרים בתוך מילים בעברית (מיפוי גופן פגום), וזיהוי טקסט לאימות תיקון אינו זמין"
REASON_FEW_SAMPLES = "בשכבת הטקסט יש תווים זרים בתוך מילים בעברית (מיפוי גופן פגום), ואין די דוגמאות קריאות לאימות תיקון"
REASON_INCONSISTENT = ("בשכבת הטקסט יש תווים זרים בתוך מילים בעברית (מיפוי גופן פגום), והקריאה החזותית של האותיות "
                       "אינה עקבית, ולכן הטקסט לא תוקן")

_FINAL_OF = {"כ": "ך", "מ": "ם", "נ": "ן", "פ": "ף", "צ": "ץ"}
_FOLD = str.maketrans({v: k for k, v in _FINAL_OF.items()})

# Approximate share of each letter in running Hebrew text. Only letters frequent enough to be expected in any text
# of some length are checked for absence (``FontMapConfig.min_expected_absent``).
HEBREW_FREQUENCY = {
    "י": 0.105, "ו": 0.100, "ה": 0.090, "ל": 0.068, "א": 0.062, "ת": 0.058, "ר": 0.057, "מ": 0.055, "ב": 0.047,
    "ש": 0.040, "נ": 0.033, "ע": 0.030, "ד": 0.028, "כ": 0.027, "ם": 0.024, "ח": 0.022, "ק": 0.020, "פ": 0.016,
    "ס": 0.013, "ג": 0.013, "ן": 0.012, "ט": 0.011, "צ": 0.009, "ז": 0.008, "ך": 0.004, "ף": 0.002, "ץ": 0.001,
}


@dataclass(frozen=True)
class FontMapConfig:
    min_samples: int = 3  # agreeing OCR votes needed to accept a mapping
    min_agreement: float = 0.8  # share of the aligned votes that must agree
    max_samples: int = 24  # word crops read per suspect pair
    max_pairs: int = 12  # suspect pairs verified per document (the most frequent first)
    min_context_match: float = 0.75  # share of a sample's other letters OCR must read the same for it to count
    min_hebrew_letters: int = 300  # a font emits at least this many Hebrew letters before an absence is reported
    min_expected_absent: float = 8.0  # ... and the absent letter's expected count is at least this
    ocr_dpi: int = 300
    ocr_languages: str = "heb+eng"
    ocr_timeout_seconds: int = 60
    max_render_pixels: int = 40_000_000

    @classmethod
    def from_settings(cls, settings) -> FontMapConfig:
        """OCR settings from ``Settings``; ``fontmap_*`` settings, when configured, override the thresholds."""
        values = {}
        for name in cls.__dataclass_fields__:
            for attr in (f"fontmap_{name}", name):
                if hasattr(settings, attr):
                    values[name] = getattr(settings, attr)
                    break
        return cls(**values)


@dataclass(frozen=True)
class WordSample:
    """One word of the text layer as extracted (in the text layer's order, often visual for Hebrew)."""

    page: int  # 0-based page index
    text: str
    bbox: tuple[float, float, float, float]  # x0, top, x1, bottom in points from the page's top-left corner
    fonts: tuple[str, ...]  # the font of each character of ``text``


class Vote(NamedTuple):
    letter: str  # what OCR reads at the suspect's place
    final: bool  # the place is the word's last letter in logical order


@dataclass
class Decision:
    accepted: bool
    letter: str | None
    final_letter: str | None  # set for a positional mapping (its word-final form)
    samples: int
    agreement: float
    votes: dict[str, int]
    reason: str | None = None


@dataclass
class Correction:
    font: str
    char: str
    letter: str
    final_letter: str | None  # a positional mapping: this letter at the end of a word
    samples: int
    agreement: float
    occurrences: int
    visual: bool = False  # the font's words are in visual order in the text layer (where their end is)

    def letter_for(self, final: bool) -> str:
        return self.final_letter if final and self.final_letter else self.letter


@dataclass
class Unresolved:
    font: str
    char: str
    occurrences: int
    samples: int
    agreement: float
    votes: dict[str, int]
    reason: str


@dataclass
class FontStats:
    font: str
    hebrew_letters: int = 0
    letters: Counter = field(default_factory=Counter)
    suspects: Counter = field(default_factory=Counter)  # character -> occurrences inside Hebrew words
    absent: list[str] = field(default_factory=list)


@dataclass
class Detection:
    fonts: dict[str, FontStats]
    suspects: dict[tuple[str, str], int]  # (font, character) -> occurrences inside Hebrew words
    words: dict[tuple[str, str], list[WordSample]]  # the words holding each pair, in document order


@dataclass
class PageCorrection:
    page: object  # a pdfplumber page whose chars read the corrected text
    hebrew_words: int
    corrected_words: int
    uncertain_words: int

    @property
    def corruption(self) -> float:
        """Share of the page's Hebrew words that keep an unrepaired corruption (``quality_score``'s signal)."""
        return self.uncertain_words / self.hebrew_words if self.hebrew_words else 0.0


class WordReader(Protocol):
    def read(self, sample: WordSample) -> str | None:
        """The word as read from its glyphs (logical order), or None when it could not be read."""


def _char_units(chars: list[dict]) -> tuple[str, list[dict]]:
    """A word's text from its chars and, per character of it, the char it came from."""
    text: list[str] = []
    owners: list[dict] = []
    for c in chars:
        for ch in c.get("text") or "":
            text.append(ch)
            owners.append(c)
    return "".join(text), owners


# --- detection -------------------------------------------------------------------------------------------------

def words_of_page(page, page_index: int) -> list[WordSample]:
    """The words of a pdfplumber page with the font of every character (pass the deduplicated page)."""
    x0, top = float(page.bbox[0]), float(page.bbox[1])
    out = []
    for w in page.extract_words(return_chars=True):
        text, owners = _char_units(w.get("chars") or [])
        if not text:
            continue
        out.append(WordSample(page_index, text,
                              (float(w["x0"]) - x0, float(w["top"]) - top, float(w["x1"]) - x0, float(w["bottom"]) - top),
                              tuple(c.get("fontname") or "" for c in owners)))
    return out


def detect(words: list[WordSample], config: FontMapConfig) -> Detection:
    fonts: dict[str, FontStats] = {}
    suspects: Counter = Counter()
    holding: dict[tuple[str, str], list[WordSample]] = {}
    for w in words:
        for ch, font in zip(w.text, w.fonts, strict=True):
            if is_hebrew_letter(ch):
                stats = fonts.setdefault(font, FontStats(font))
                stats.hebrew_letters += 1
                stats.letters[ch] += 1
        for i in suspect_positions(w.text):
            key = (w.fonts[i], w.text[i])
            suspects[key] += 1
            fonts.setdefault(key[0], FontStats(key[0])).suspects[key[1]] += 1
            seen = holding.setdefault(key, [])
            if not seen or seen[-1] is not w:
                seen.append(w)
    for stats in fonts.values():
        n = stats.hebrew_letters
        if n >= config.min_hebrew_letters:
            stats.absent = [ch for ch, share in HEBREW_FREQUENCY.items()
                            if stats.letters[ch] == 0 and share * n >= config.min_expected_absent]
    return Detection(fonts, dict(suspects), holding)


# --- repair ----------------------------------------------------------------------------------------------------

def _align(sample: WordSample, font: str, char: str, ocr_text: str,
           config: FontMapConfig) -> tuple[list[Vote], bool] | None:
    letters = [(i, ch) for i, ch in enumerate(sample.text) if letter_like(ch)]
    read = [ch for ch in strip_bidi_controls(ocr_text) if letter_like(ch)]
    if not letters or len(read) != len(letters):
        return None
    suspects = set(suspect_positions(sample.text))
    targets = {i for i in suspects if sample.text[i] == char and sample.fonts[i] == font}
    if not targets:
        return None
    options = []
    for reversed_ in (False, True):
        order = letters[::-1] if reversed_ else letters
        context = [(a, b) for (i, a), b in zip(order, read, strict=True) if i not in suspects]
        if len(context) < 2:
            continue
        match = sum(a.translate(_FOLD) == b.translate(_FOLD) for a, b in context) / len(context)
        last = len(order) - 1
        votes = [Vote(read[k], k == last) for k, (i, _) in enumerate(order) if i in targets]
        options.append((match, votes, reversed_))
    if not options:
        return None
    options.sort(key=lambda o: -o[0])
    best = options[0]
    if best[0] < config.min_context_match:
        return None
    if len(options) == 2 and options[1][0] == best[0] and options[1][1] != best[1]:
        return None  # both orders fit equally and read differently
    return best[1], best[2]


def align(sample: WordSample, font: str, char: str, ocr_text: str, config: FontMapConfig) -> list[Vote] | None:
    """The OCR votes for ``font``'s ``char`` in this word, in logical order; None when the OCR word does not align
    with the extracted word (another length, or too few of the other letters read the same)."""
    found = _align(sample, font, char, ocr_text, config)
    return found[0] if found else None


def _top(votes: list[Vote]) -> tuple[str | None, int]:
    if not votes:
        return None, 0
    return Counter(v.letter for v in votes).most_common(1)[0]


def decide(votes: list[Vote], config: FontMapConfig) -> Decision:
    """Accept a mapping when enough votes agree on one Hebrew letter; or, split by word position, on the two forms
    of one letter (נ inside words, ן at their end)."""
    counts = dict(Counter(v.letter for v in votes))
    total = len(votes)
    letter, n = _top(votes)
    agreement = round(n / total, 3) if total else 0.0
    if total < config.min_samples:
        return Decision(False, None, None, total, agreement, counts, REASON_FEW_SAMPLES)
    if letter and is_hebrew_letter(letter) and n >= config.min_samples and n / total >= config.min_agreement:
        return Decision(True, letter, None, total, agreement, counts)
    inner, last = [v for v in votes if not v.final], [v for v in votes if v.final]
    (li, ni), (lf, nf) = _top(inner), _top(last)
    if (li and lf and _FINAL_OF.get(li) == lf and ni + nf >= config.min_samples
            and ni / len(inner) >= config.min_agreement and nf / len(last) >= config.min_agreement):
        return Decision(True, li, lf, total, round((ni + nf) / total, 3), counts)
    return Decision(False, None, None, total, agreement, counts, REASON_INCONSISTENT)


def _pick(words: list[WordSample], n: int) -> list[WordSample]:
    """Up to ``n`` sample words spread over the document, preferring words with at least three letters."""
    longer = [w for w in words if sum(1 for ch in w.text if letter_like(ch)) >= 3]
    pool = longer if len(longer) >= min(n, 3) else words
    if len(pool) <= n:
        return pool
    return [pool[round(k * (len(pool) - 1) / (n - 1))] for k in range(n)] if n > 1 else pool[:1]


class OcrWordReader:
    """Renders a word's glyphs with pdfium (independent of the font's ToUnicode map) and reads them with Tesseract."""

    def __init__(self, pdf_doc, config: FontMapConfig):
        self.doc = pdf_doc
        self.config = config
        self._page: tuple[int, Image.Image, float] | None = None

    def _render(self, index: int) -> tuple[Image.Image, float]:
        if self._page is None or self._page[0] != index:
            page = self.doc[index]
            try:
                width = page.get_size()[0]
            finally:
                page.close()
            img = ocr.render_page(self.doc, index, self.config.ocr_dpi, self.config.max_render_pixels)
            self._page = (index, img, img.size[0] / width)
        return self._page[1], self._page[2]

    def crop(self, sample: WordSample) -> Image.Image:
        img, scale = self._render(sample.page)
        x0, top, x1, bottom = sample.bbox
        h = max(bottom - top, 1.0)
        box = (max(0, math.floor((x0 - 0.08 * h) * scale)), max(0, math.floor((top - 0.25 * h) * scale)),
               min(img.size[0], math.ceil((x1 + 0.08 * h) * scale)),
               min(img.size[1], math.ceil((bottom + 0.25 * h) * scale)))
        return img.crop(box)

    def read(self, sample: WordSample) -> str | None:
        import pytesseract

        crop = self.crop(sample)
        if crop.size[0] < 2 or crop.size[1] < 2:
            return None
        pad = Image.new("L", (crop.size[0] + 40, crop.size[1] + 40), 255)
        pad.paste(crop, (20, 20))
        try:
            text = pytesseract.image_to_string(pad, lang=self.config.ocr_languages, config="--psm 7",
                                               timeout=self.config.ocr_timeout_seconds)
        except RuntimeError:  # pytesseract's per-call timeout
            log.warning("font map: OCR timed out on a word crop of page %s", sample.page + 1)
            return None
        return strip_bidi_controls(text).strip() or None


@dataclass
class FontMapFix:
    corrections: dict[tuple[str, str], Correction]
    unresolved: dict[tuple[str, str], Unresolved]
    fonts: dict[str, FontStats]
    pages: set[int]  # 0-based pages holding a corrected or unresolved character

    def correct_page(self, page) -> PageCorrection:
        """A derived pdfplumber page whose chars read the corrected text. A corrected char keeps its extracted
        character in ``fontmap_from``; an unrepaired suspect char gets ``fontmap_uncertain`` (the reason)."""
        from pdfplumber.page import FilteredPage

        replace: dict[int, dict] = {}
        hebrew = corrected = uncertain = 0
        for w in page.extract_words(return_chars=True):
            text, owners = _char_units(w.get("chars") or [])
            if not any(is_hebrew_letter(ch) for ch in text):
                continue
            hebrew += 1
            positions = suspect_positions(text)
            if not positions:
                continue
            letters = [k for k, ch in enumerate(text) if letter_like(ch)]
            fixed = unsure = False
            for k in positions:
                c = owners[k]
                if len(c.get("text") or "") != 1:
                    continue
                key = (c.get("fontname") or "", c["text"])
                if key in self.corrections:
                    corr = self.corrections[key]
                    final = k == (letters[0] if corr.visual else letters[-1])
                    replace[id(c)] = c | {"text": corr.letter_for(final), "fontmap_from": c["text"]}
                    fixed = True
                elif key in self.unresolved:
                    replace[id(c)] = c | {"fontmap_uncertain": self.unresolved[key].reason}
                    unsure = True
            corrected += fixed
            uncertain += unsure
        derived = FilteredPage(page, lambda _obj: True)
        derived._objects = dict(page.objects) | {"char": [replace.get(id(c), c) for c in page.chars]}
        return PageCorrection(derived, hebrew, corrected, uncertain)

    @staticmethod
    def uncertain_reason(chars: list[dict]) -> str | None:
        """Why a line (its chars from the corrected page) is uncertain; None when nothing in it is."""
        return next((c["fontmap_uncertain"] for c in chars if c.get("fontmap_uncertain")), None)

    @staticmethod
    def original_line(raw_text: str, logical_text: str, chars: list[dict]) -> str | None:
        """The line as extracted before correction, in the same order as ``logical_text`` (the orientation fix of
        ``raw_text``); None when no character of the line was corrected."""
        if not any("fontmap_from" in c for c in chars):
            return None
        out = list(raw_text)
        pos = 0
        for c in chars:
            t = c.get("text") or ""
            idx = raw_text.find(t, pos) if t else -1
            if idx < 0:
                continue
            if "fontmap_from" in c:
                out[idx] = c["fontmap_from"]
            pos = idx + len(t)
        raw_original = "".join(out)
        if logical_text == raw_text:
            return raw_original
        # a corrected letter and its original are both outside every left-to-right run, so the orientation fix
        # moves them alike
        return visual_to_logical(raw_original)

    def report(self) -> dict:
        """The record kept in the ingestion report: suspect fonts, accepted corrections, unresolved pairs."""
        return {
            "fonts": [{"font": f.font, "hebrew_letters": f.hebrew_letters, "suspects": dict(f.suspects),
                       "absent": list(f.absent)}
                      for f in self.fonts.values() if f.suspects or f.absent],
            "corrections": [{"font": c.font, "from": c.char, "to": c.letter, "to_final": c.final_letter,
                             "samples": c.samples, "agreement": c.agreement, "occurrences": c.occurrences}
                            for c in self.corrections.values()],
            "unresolved": [{"font": u.font, "char": u.char, "occurrences": u.occurrences, "samples": u.samples,
                            "agreement": u.agreement, "votes": dict(u.votes), "reason": u.reason}
                           for u in self.unresolved.values()],
        }


def detect_and_repair(words: list[WordSample], pdf_doc=None, config: FontMapConfig | None = None,
                      reader: WordReader | None = None) -> FontMapFix:
    """Detect suspect ``font × character`` pairs in a document's words and verify each against its glyphs.
    ``reader`` reads a word from its glyphs; by default pdfium + Tesseract on ``pdf_doc`` when Tesseract has the
    configured languages. Without a reader every suspect pair stays unresolved."""
    config = config or FontMapConfig()
    found = detect(list(words), config)
    if found.suspects and reader is None and pdf_doc is not None and ocr.ocr_available(config.ocr_languages):
        reader = OcrWordReader(pdf_doc, config)
    corrections: dict[tuple[str, str], Correction] = {}
    unresolved: dict[tuple[str, str], Unresolved] = {}
    ranked = sorted(found.suspects, key=lambda k: (-found.suspects[k], k))
    for rank, key in enumerate(ranked):
        font, char = key
        occurrences = found.suspects[key]
        if reader is None:
            unresolved[key] = Unresolved(font, char, occurrences, 0, 0.0, {}, REASON_NO_OCR)
            continue
        if rank >= config.max_pairs:
            unresolved[key] = Unresolved(font, char, occurrences, 0, 0.0, {}, REASON_FEW_SAMPLES)
            continue
        votes: list[Vote] = []
        visual = 0
        for sample in _pick(found.words[key], config.max_samples):
            text = reader.read(sample)
            got = _align(sample, font, char, text, config) if text else None
            if got:
                votes.extend(got[0])
                visual += 1 if got[1] else -1
        d = decide(votes, config)
        if d.accepted:
            corrections[key] = Correction(font, char, d.letter, d.final_letter, d.samples, d.agreement, occurrences,
                                          visual > 0)
        else:
            unresolved[key] = Unresolved(font, char, occurrences, d.samples, d.agreement, d.votes, d.reason)
    keys = set(corrections) | set(unresolved)
    pages = {w.page for key in keys for w in found.words[key]}
    return FontMapFix(corrections, unresolved, found.fonts, pages)


def analyze(pdf_doc, plumber_pdf, config: FontMapConfig | None = None, dedupe_tolerance: float = 1.0,
            reader: WordReader | None = None) -> FontMapFix:
    """``detect_and_repair`` over a whole document opened with pypdfium2 and pdfplumber (one extra pass over the
    text layer; ``pdf.py`` collects the words while it reads the pages instead)."""
    words: list[WordSample] = []
    for index, page in enumerate(plumber_pdf.pages):
        try:
            words.extend(words_of_page(page.dedupe_chars(tolerance=dedupe_tolerance), index))
        finally:
            page.close()
    return detect_and_repair(words, pdf_doc, config, reader)
