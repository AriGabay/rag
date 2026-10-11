"""The evidence the verification judge reads for a claim: the parts of each cited source that cover it.

A cited source can be long (a whole section, a whole table). Cutting it to its first characters shows the
judge text that may not hold the claim's evidence at all, and hides the evidence that does. Instead, the source
is split into segments (lines; long lines into sentences; each table row on its own) and, for each claim, the
segments that cover it are kept:

- every segment that states one of the claim's numbers — all of them, so when the same number appears with
  another meaning elsewhere in the source the judge sees both and decides by meaning;
- the segments sharing the most words with the claim (prefix, inflection and abbreviation variants count);
- one neighbouring segment on each side of what was kept, for context;
- for a table: its caption, title, size line, header row and notes, so a row is read with its column meanings,
  units and qualifications.

A source short enough is kept whole. A cut source is rendered with the omitted spans marked, and the judge is
told it is an excerpt (an omission is not evidence that something is absent).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property, lru_cache

from app.answering.verify import numbers_in
from app.extraction.abbreviations import variants
from app.extraction.normalize_text import query_tokens

WHOLE_SOURCE_CHARS = 4000  # a source this short is given whole
LONG_LINE = 400  # a longer line is split into sentences
TOP_WORD_SEGMENTS = 3
OMITTED = "…[הושמט]…"
TABLE_SIZE_PREFIX = "הטבלה:"  # the size line a table source starts with (tools.table_size_line)

_SENTENCE = re.compile(r"(?<=[.!?:;])\s+(?=\S)")
_NOTE = re.compile(r"^\s*(?:\(?\*+\)?|הערה|הערות|הע׳|הע')")


def _content_words(text: str) -> set[str]:
    """Search tokens of the text (with their variants), numbers left out."""
    return {t for t in query_tokens(text) if not any(c.isdigit() for c in t)}


@dataclass
class Segment:
    text: str
    header: bool = False

    @cached_property
    def tokens(self) -> frozenset[str]:
        return frozenset(_content_words(self.text))

    @cached_property
    def numbers(self) -> frozenset[str]:
        return frozenset(numbers_in(self.text, words=True))


@lru_cache(maxsize=256)
def segments_of(text: str, kind: str = "text") -> tuple[Segment, ...]:
    """The source split into segments, table header lines and notes marked as headers."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    tabular = kind in ("table", "table_row") or sum(" | " in ln for ln in lines) >= 2
    rows = [i for i, ln in enumerate(lines) if " | " in ln]
    first_row, last_row = (rows[0], rows[-1]) if rows else (None, None)
    out: list[Segment] = []
    for i, ln in enumerate(lines):
        if tabular and first_row is not None:
            header = (i <= first_row or i > last_row or bool(_NOTE.match(ln))
                      or ln.startswith(TABLE_SIZE_PREFIX))
            out.append(Segment(ln, header))
            continue
        if tabular and _NOTE.match(ln):
            out.append(Segment(ln, True))
            continue
        if len(ln) <= LONG_LINE:
            out.append(Segment(ln))
            continue
        out.extend(Segment(part) for part in _SENTENCE.split(ln) if part.strip())
    return tuple(out)


@lru_cache(maxsize=1024)
def claim_terms(claim: str) -> tuple[frozenset[str], frozenset[str]]:
    """The claim's numbers, and its content words with the abbreviation variants of the claim."""
    words: set[str] = set()
    for form in [claim, *variants(claim, limit=4)]:
        words |= _content_words(form)
    return frozenset(numbers_in(claim)), frozenset(words)


def choose(claim: str, segments: tuple[Segment, ...], *, narrow: bool = False) -> set[int]:
    """Indexes of the segments that cover the claim. ``narrow`` keeps only number matches and table headers
    (for a claim whose evidence would not fit otherwise)."""
    nums, words = claim_terms(claim)
    picked = {i for i, s in enumerate(segments) if nums and s.numbers & nums}
    if not narrow:
        scored = sorted(((len(s.tokens & words), i) for i, s in enumerate(segments) if not s.header),
                        reverse=True)
        floor = 2 if len(words) >= 4 else 1
        picked |= {i for score, i in scored[:TOP_WORD_SEGMENTS] if score >= floor}
        picked |= {j for i in list(picked) for j in (i - 1, i + 1) if 0 <= j < len(segments)}
    if picked:
        picked |= {i for i, s in enumerate(segments) if s.header}
    return picked


def render(segments: tuple[Segment, ...], picked: set[int]) -> tuple[str, bool]:
    """The kept segments in source order, each omitted run marked; and whether anything was omitted."""
    if not segments:
        return "", False
    parts: list[str] = []
    gap = False
    for i, s in enumerate(segments):
        if i in picked:
            if gap:
                parts.append(OMITTED)
                gap = False
            parts.append(s.text)
        else:
            gap = True
    if gap:
        parts.append(OMITTED)
    excerpt = len(picked) < len(segments)
    return "\n".join(parts), excerpt


def select(claims: list[str], text: str, kind: str = "text", *, narrow: bool = False) -> tuple[str, bool]:
    """The evidence of one source for the given claims: the whole source when it is short, otherwise the union
    of the segments each claim needs. A claim with nothing in common with a long source gets the segments that
    share the most words with it; never an arbitrary prefix."""
    if len(text or "") <= WHOLE_SOURCE_CHARS:
        return text or "", False
    segs = segments_of(text, kind)
    picked: set[int] = set()
    for c in claims:
        got = choose(c, segs, narrow=narrow)
        if not got and not narrow:
            # nothing matched by number or by enough words: the best-sharing segments, still not a prefix
            _, words = claim_terms(c)
            best = sorted(((len(s.tokens & words), i) for i, s in enumerate(segs)), reverse=True)[:TOP_WORD_SEGMENTS]
            got = {i for _, i in best} | {i for i, s in enumerate(segs) if s.header}
        picked |= got
    return render(segs, picked)
