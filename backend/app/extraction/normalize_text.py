"""Hebrew text normalization for lexical search (KTD9).

PostgreSQL has no Hebrew dictionary; the 'simple' configuration splits on punctuation and keeps
one-letter prefixes glued to words, so 'ברמת גן' never matches 'רמת גן' and '45,000' splits in
two. We normalize in the application, identically for indexed text and queries:

- strip niqqud and cantillation marks
- unify geresh/gershayim and ASCII/typographic quotes (מ"ר, מ״ר -> מ״ר)
- drop thousands separators inside numbers (1,250,000 -> 1250000)
- for Hebrew words starting with a one-letter prefix (ו ה ב ל מ ש כ) add the stripped form as
  an extra token, so both 'ברמת' and 'רמת' are searchable
"""

from __future__ import annotations

import re
import unicodedata

_NIQQUD = re.compile(r"[֑-ׇ]")
_GERSHAYIM = re.compile(r"(?<=[א-ת])[\"“”״](?=[א-ת])")
_GERESH = re.compile(r"(?<=[א-ת])['`’‘׳](?=\s|$|[^א-ת])")
_THOUSANDS = re.compile(r"(?<=\d)[,٬](?=\d{3}(?!\d))")
_HEB_WORD = re.compile(r"[א-ת][א-ת״׳]*")
_PREFIXES = "והבלמשכ"
_TWO_LETTER_PREFIXES = ("וה", "וב", "ול", "ומ", "וש", "שה", "מה", "בה", "לה", "כש", "שב", "של", "שמ")
_SPACE = re.compile(r"\s+")


def base_normalize(text: str) -> str:
    """Normalization shared by display-independent comparisons (no extra tokens)."""
    text = unicodedata.normalize("NFKC", text)
    text = _NIQQUD.sub("", text)
    text = _GERSHAYIM.sub("״", text)
    text = _GERESH.sub("׳", text)
    text = text.replace("₪", " ₪ ")
    prev = None
    while prev != text:
        prev = text
        text = _THOUSANDS.sub("", text)
    return _SPACE.sub(" ", text).strip().lower()


def prefix_variants(word: str) -> list[str]:
    variants: list[str] = []
    for p in _TWO_LETTER_PREFIXES:
        if word.startswith(p) and len(word) - len(p) >= 2:
            variants.append(word[len(p):])
    if word[0] in _PREFIXES and len(word) >= 4:
        variants.append(word[1:])
    return variants


def normalize_for_search(text: str) -> str:
    """Normalized text plus prefix-stripped variants, for tsvector and trigram indexing."""
    base = base_normalize(text)
    extra: list[str] = []
    for m in _HEB_WORD.finditer(base):
        extra.extend(prefix_variants(m.group(0)))
    return base if not extra else f"{base} {' '.join(extra)}"


def query_tokens(query: str) -> list[str]:
    """Tokens for an OR-style tsquery: base tokens plus prefix-stripped variants."""
    base = base_normalize(query)
    tokens: list[str] = []
    for raw in re.findall(r"[\w״׳./-]+", base):
        tok = raw.strip("./-")
        if len(tok) < 2 and not tok.isdigit():
            continue
        tokens.append(tok)
        if _HEB_WORD.fullmatch(tok):
            tokens.extend(prefix_variants(tok))
    seen: set[str] = set()
    return [t for t in tokens if not (t in seen or seen.add(t))]
