"""Hebrew visual -> logical order fix-up and text-layer quality score (KTD11, R3, R4).

Many PDF producers (fpdf2 here, also several report generators) write shaped Hebrew glyphs in visual
(left-to-right display) order. pdfplumber then returns every Hebrew line reversed while digit runs
stay left to right: ``'14 ןפגה :סכנה תבותכ'`` for ``'כתובת הנכס: הגפן 14'``. Forcing pdfplumber's
``char_dir="rtl"`` reverses the digits too, so the fix works per line: reverse the line, then restore
every left-to-right run (numbers, dates, block/parcel, Latin words) and balance brackets.

Orientation is decided with a cheap check: Hebrew final letters (ך ם ן ף ץ) end words in logical
text, so reversed text puts them at word starts (and leaves non-final כ מ נ פ צ at word ends). Label
colons and sentence periods give the same signal (``'עיר:'`` vs ``':ריע'``).

Text is content only: nothing here interprets what the text says (R29).
"""

from __future__ import annotations

import re

QUALITY_THRESHOLD = 0.8

_FINALS = "ךםןףץ"
_NON_FINALS = "כמנפצ"
_HEB_LETTER = re.compile(r"[א-ת]")
_HEB_CORE = re.compile(r"[^א-ת]*([א-ת]{2,})[^א-ת]*")
_PUNCT_START = re.compile(r"^[:.,;!?]+[א-ת]")
_PUNCT_END = re.compile(r"[א-ת][:.,;!?]+$")
_NUM_DOT_START = re.compile(r"^\.\d{1,2}$")  # ".1" = reversed heading number "1."
_NUM_DOT_END = re.compile(r"^\d{1,2}\.$")
# A left-to-right token inside the reversed line: digits/Latin with inner separators.
_LTR_TOKEN = re.compile(r"[A-Za-z0-9%](?:[A-Za-z0-9.,/:%+\-]*[A-Za-z0-9%])?")
_LATIN = re.compile(r"[A-Za-z]")
_BIDI_CONTROLS = re.compile("[‎‏‪-‮⁦-⁩؜]")
_MIRROR = str.maketrans("()[]{}<>", ")(][}{><")
_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


def has_hebrew(text: str) -> bool:
    return bool(_HEB_LETTER.search(text))


def strip_bidi_controls(text: str) -> str:
    return _BIDI_CONTROLS.sub("", text)


def orientation_evidence(text: str) -> tuple[int, int]:
    """Return (logical, visual) evidence counts for the words in ``text``."""
    logical = visual = 0
    for tok in text.split():
        if _PUNCT_START.match(tok) or _NUM_DOT_START.match(tok):
            visual += 1
        elif _PUNCT_END.search(tok) or _NUM_DOT_END.match(tok):
            logical += 1
        m = _HEB_CORE.fullmatch(tok)
        if not m:  # no Hebrew word, or an abbreviation with ״/׳ inside (מ״ר, מע״מ)
            continue
        core = m.group(1)
        if core[0] in _FINALS:
            visual += 1
        if core[-1] in _FINALS:
            logical += 1
        elif core[-1] in _NON_FINALS:
            visual += 1
    return logical, visual


def looks_visual(text: str, margin: int = 1) -> bool | None:
    """True = visual order, False = logical, None = no decision at this margin."""
    logical, visual = orientation_evidence(text)
    if visual - logical >= margin:
        return True
    if logical - visual >= margin:
        return False
    return None


def _balanced(s: str) -> bool:
    for open_c, close_c in _PAIRS:
        depth = 0
        for ch in s:
            if ch == open_c:
                depth += 1
            elif ch == close_c:
                depth -= 1
                if depth < 0:
                    return False
        if depth != 0:
            return False
    return True


def _restore_ltr_runs(s: str) -> str:
    tokens = list(_LTR_TOKEN.finditer(s))
    if not tokens:
        return s
    # Group consecutive Latin-bearing tokens separated only by spaces ("Tel Aviv").
    groups: list[list] = []  # [start, end, last token has Latin]
    for m in tokens:
        latin = bool(_LATIN.search(m.group(0)))
        gap = s[groups[-1][1]:m.start()] if groups else ""
        if groups and latin and groups[-1][2] and gap and set(gap) == {" "}:
            groups[-1][1] = m.end()
        else:
            groups.append([m.start(), m.end(), latin])
    out = []
    pos = 0
    for start, end, _ in groups:
        out.append(s[pos:start])
        out.append(s[start:end][::-1])
        pos = end
    out.append(s[pos:])
    return "".join(out)


def visual_to_logical(line: str) -> str:
    """Convert one visual-order line to logical order (digit/Latin runs kept left to right)."""
    s = _restore_ltr_runs(line[::-1])
    if not _balanced(s):
        mirrored = s.translate(_MIRROR)
        if _balanced(mirrored):
            s = mirrored
    return s


def fix_line(line: str, default_visual: bool = False) -> str:
    """Fix one line; the line's own evidence wins, ``default_visual`` decides ambiguous lines."""
    if not has_hebrew(line):
        # Digits/symbols only (e.g. "1,480,000 ₪") follow the surrounding RTL paragraph; Latin text
        # without Hebrew is an LTR paragraph and stays as is.
        return visual_to_logical(line) if default_visual and not _LATIN.search(line) else line
    v = looks_visual(line)
    if v is None:
        v = default_visual
    return visual_to_logical(line) if v else line


def page_is_visual(lines: list[str]) -> bool | None:
    return looks_visual("\n".join(lines))


def fix_text_lines(lines: list[str], default_visual: bool | None = None) -> list[str]:
    """Fix all lines of one page. The page majority decides lines with weak evidence; a line
    overrides the page only with a clear margin (mixed-producer documents)."""
    page = page_is_visual(lines) if default_visual is None else default_visual
    out = []
    for line in lines:
        if not has_hebrew(line):
            out.append(fix_line(line, bool(page)))
            continue
        own = looks_visual(line, margin=2)
        if own is None:
            own = page if page is not None else bool(looks_visual(line))
        out.append(visual_to_logical(line) if own else line)
    return out


def _is_good_char(ch: str) -> bool:
    o = ord(ch)
    if 0x21 <= o <= 0x7E:
        return True
    if 0x05D0 <= o <= 0x05EA or 0x0591 <= o <= 0x05C7 or o in (0x05F3, 0x05F4, 0x05BE):
        return True
    return ch in "₪—–…•°’‘“”€$·"


def quality_score(text: str) -> float:
    """Score in [0, 1]: share of expected characters (Hebrew, Latin, digits, punctuation), penalized
    for replacement characters / mojibake, too little alphanumeric content, too little text, and text
    whose orientation check says it is still in visual order."""
    chars = [c for c in strip_bidi_controls(text) if not c.isspace()]
    total = len(chars)
    if total == 0:
        return 0.0
    good = sum(1 for c in chars if _is_good_char(c))
    score = good / total
    alnum = sum(1 for c in chars if c.isalnum() and _is_good_char(c))
    alnum_share = alnum / total
    if alnum_share < 0.5:
        score *= alnum_share / 0.5
    logical, visual = orientation_evidence(text)
    if visual > logical and visual >= 3:
        score *= logical / (logical + visual)
    if total < 15:
        score *= total / 15
    return round(max(0.0, min(1.0, score)), 3)
