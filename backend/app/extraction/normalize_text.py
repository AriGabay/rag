"""Hebrew text normalization for lexical search (KTD9).

PostgreSQL has no Hebrew dictionary; the 'simple' configuration splits on punctuation and keeps
one-letter prefixes glued to words, so 'ברמת גן' never matches 'רמת גן' and '45,000' splits in
two. We normalize in the application, identically for indexed text and queries:

- strip niqqud and cantillation marks
- unify geresh/gershayim and ASCII/typographic quotes (מ"ר, מ״ר -> מ״ר)
- drop thousands separators inside numbers (1,250,000 -> 1250000)
- for Hebrew words starting with a one-letter prefix (ו ה ב ל מ ש כ) add the stripped form as
  an extra token, so both 'ברמת' and 'רמת' are searchable
- light inflection (KTD10): plural and construct suffixes add singular-form tokens on both sides
  ('דירות' -> 'דירה', 'חדרים' -> 'חדר', construct 'שיקולי' -> 'שיקולים', 'שיקול'). A morphological rule,
  not topic vocabulary. Changing these rules changes indexed text: run ``scripts/reindex_text.py``.
"""

from __future__ import annotations

import re
import unicodedata

_NIQQUD = re.compile(r"[֑-ׇ]")
_GERSHAYIM = re.compile(r"(?<=[א-ת])[\"“”״](?=[א-ת])")
_GERESH = re.compile(r"(?<=[\u05D0-\u05EA])['`’‘]")
_THOUSANDS = re.compile(r"(?<=\d)[,٬](?=\d{3}(?!\d))")
_HEB_WORD = re.compile(r"[א-ת][א-ת״׳]*")
_PREFIXES = "והבלמשכ"
_TWO_LETTER_PREFIXES = ("וה", "וב", "ול", "ומ", "וש", "שה", "מה", "בה", "לה", "כש", "שב", "של", "שמ")
_SPACE = re.compile(r"\s+")
_HYPHEN_NUM = re.compile(r"(?<=[\u05D0-\u05EA])-(?=\d)")
# Question words and particles that carry no retrieval signal.
STOPWORDS = frozenset(
    "מה מי איך למה מדוע האם על של את עם זה זו זאת אלה כל או גם אם כי לא יש אין הוא היא הם הן היה היו "
    "בין עד כמה איזה איזו אילו לגבי נכתב נאמר כתוב אני אנחנו יותר פחות כמו רק עוד אותו אותה שם פה "
    "ומה ועל ושל ואת וגם ואם במה בזה לזה מזה כך כן בכ כ- "
    # meta words about the repository itself ("what is written in the documents about ...")
    "מסמך מסמכים במסמך במסמכים בשומות שומות המסמכים במאגר מאגר נכתבו נכתבה הוזכר הוזכרו מופיע".split()
)


def base_normalize(text: str) -> str:
    """Normalization shared by display-independent comparisons (no extra tokens)."""
    text = unicodedata.normalize("NFKC", text)
    text = _NIQQUD.sub("", text)
    text = _GERSHAYIM.sub("״", text)
    text = _GERESH.sub("׳", text)
    text = text.replace("₪", " ₪ ")
    # "ב-2024", "בכ-1.25": a Hebrew prefix glued to a number by a hyphen would index as "-1.25"
    text = _HYPHEN_NUM.sub(" ", text)
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


def inflection_variants(word: str) -> list[str]:
    """Singular forms for plural/construct suffixes: -ות -> -ת/-ה, -ים -> stem, construct -ת -> -ה, and
    construct plural -י -> -ים and stem ('שיקולי' -> 'שיקולים', 'שיקול', the same stem '-ים' yields).
    Only when at least three stem letters remain, so short words are never truncated."""
    variants: list[str] = []
    if len(word) >= 5 and word.endswith("ות"):
        variants += [word[:-2] + "ת", word[:-2] + "ה"]
    elif len(word) >= 5 and word.endswith("ים"):
        variants.append(word[:-2])
    elif len(word) >= 4 and word.endswith("ת"):
        variants.append(word[:-1] + "ה")
    elif len(word) >= 4 and word.endswith("י"):
        variants += [word[:-1] + "ים", word[:-1]]
    return variants


def _word_variants(word: str) -> list[str]:
    """Prefix-stripped forms, and inflection variants of the word and of each stripped form."""
    out = prefix_variants(word)
    for w in [word, *out]:
        out = out + inflection_variants(w)
    return out


def normalize_for_search(text: str) -> str:
    """Normalized text plus prefix-stripped and inflection variants, for tsvector and trigram indexing."""
    base = base_normalize(text)
    extra: list[str] = []
    for m in _HEB_WORD.finditer(base):
        extra.extend(_word_variants(m.group(0)))
    return base if not extra else f"{base} {' '.join(extra)}"


def query_tokens(query: str) -> list[str]:
    """Tokens for an OR-style tsquery: base tokens plus prefix-stripped and inflection variants."""
    base = base_normalize(query)
    tokens: list[str] = []
    for raw in re.findall(r"[\w״׳./]+", base):
        tok = raw.strip("./-׳״")
        letters = sum(ch.isalpha() for ch in tok)
        if tok in STOPWORDS or (letters < 2 and not any(ch.isdigit() for ch in tok)):
            continue
        tokens.append(tok)
        if _HEB_WORD.fullmatch(tok):
            tokens.extend(_word_variants(tok))
    seen: set[str] = set()
    return [t for t in tokens if not (t in seen or seen.add(t))]
