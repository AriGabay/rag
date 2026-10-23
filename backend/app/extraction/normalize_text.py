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


# --- negation and absence (one vocabulary for search, interpretation, composition and coverage) ---------------------------------------

# Negation and absence words. "אל" is also the preposition "to" ("פונה אל הרחוב"): it is listed (the
# prohibitive "don't") but marked ambiguous, so callers that read running text can leave it out. "טרם" ("not yet"), "בלא" and "לבלתי"
# are the clause negations the claim checks compare (round 22 U6, KTD11: the list that lived in ``chat.verify`` is this one).
NEGATION_WORDS = frozenset(
    "לא אין אינו אינה אינם אינן איננו איננה ללא בלי מבלי אל היעדר העדר בהיעדר בהעדר טרם בלא לבלתי".split()
)
AMBIGUOUS_NEGATIONS = frozenset({"אל"})
# the negations that are prepositions ("ללא מע״מ", "בלי פירוט"): they negate the noun that follows them, never a datum the clause states
# ("1,200 ₪ ללא מע״מ" states 1,200 ₪)
PREPOSITIONAL_NEGATIONS = frozenset("ללא בלי מבלי היעדר העדר בהיעדר בהעדר בלא לבלתי".split())
# Prefixes a negation takes ("ואין", "שלא", "כשאין", "וללא"). Not ב/ל/מ/כ alone: "מלא" (full) and
# "כלא" (prison) are not negations.
_NEGATION_PREFIXES = ("וכש", "וש", "כש", "ו", "ש")


def negation_form(word: str, *, ambiguous: bool = True, prepositional: bool = True) -> str | None:
    """The negation word a normalized word is, without its conjunction/relative prefix ("ואין" -> "אין", "שלא" -> "לא"), else None. With
    ``ambiguous=False`` words that are also prepositions ("אל") never count; an ambiguous word counts only bare ("שאל" is "asked", "ואל"
    is "and to"). With ``prepositional=False`` the negations that are prepositions ("ללא", "בלי") do not count either: what is left negates
    a clause."""
    def listed(w: str, bare: bool) -> bool:
        return (w in NEGATION_WORDS and (w not in AMBIGUOUS_NEGATIONS or (ambiguous and bare))
                and (prepositional or w not in PREPOSITIONAL_NEGATIONS))

    if listed(word, True):
        return word
    return next((word[len(p):] for p in _NEGATION_PREFIXES if word.startswith(p) and listed(word[len(p):], False)), None)


def is_negation(word: str, *, ambiguous: bool = True, prepositional: bool = True) -> bool:
    """Whether a normalized word is a negation, bare or after a conjunction/relative prefix (``negation_form``)."""
    return negation_form(word, ambiguous=ambiguous, prepositional=prepositional) is not None


# --- absence: a clause that says a datum is not stated, found, computed or determined (round 22 U6, KTD6, KTD11) --------------------------
#
# The six readers that did this shared no word (``verify.NOT_FOUND`` / ``ABSENCE`` / ``_NOT_DETERMINED``, ``meaning.ABSENCE_SENTENCE``,
# ``compose.is_absence_claim``, ``verify._NEGATIONS``); this is the one that replaced them. A claim that says the datum is missing, as opposed to
# a fact the document states about an absence ("אין בדירה גינה", "השמאי מציין שאין גינה"): a negation that governs a word of stating, finding,
# computing, determining or knowing, or an information noun ("לא נמסר", "אין בהם נתון", "לא ניתן לקבוע", "ללא פירוט").
_FINALS = str.maketrans("ךםןףץ", "כמנפצ")


def _medial(words: str) -> frozenset[str]:
    return frozenset(w.translate(_FINALS) for w in words.split())


# verbs that, right after a negation, say the datum is missing: not stated / mentioned / written / detailed / known / found / shown /
# computed / determined / defined / checked
_STATED_VERBS = _medial(
    "צוין צוינה צוינו מצוין מצוינת מצוינים מצוינות ציין ציינה ציינו נכתב נכתבה נכתבו נאמר נאמרה פורט פורטה פורטו מפורט מפורטת "
    "הוזכר הוזכרה הוזכרו מוזכר מוזכרת נמסר נמסרה ידוע ידועה ברור ברורה נמצא נמצאה נמצאו נמצאים אותר אותרה אותרו מופיע מופיעה "
    "מופיעים מופיעות חושב חושבה חושבו חושבים נחשב נחשבה נקבע נקבעה נקבעו נקבעים הוגדר הוגדרה הוגדרו נבדק נבדקה נבדקו")
# a datum the text calls missing with no negation
_MISSING_WORDS = _medial("חסר חסרה חסרים חסרות")
_INFLECTION = r"(?:ה|ו|ת|ימ|ות|נו)?"
# verbs of stating, mentioning or reporting (a negation right before them, at most one word between)
_STATING = re.compile(r"(?:מ?צוי{1,2}נ|מציי?נ|ציי?נ|מ?פורט|מפרט|פירט|הוזכר|מוזכר|מזכיר|הזכיר|נזכר"
                      r"|מ?תייחס|התייחס|נאמר|נמסר|מסר|מוסר|מ?דווח|דיווח)" + _INFLECTION)
# verbs of appearing in a text: absence when a document word or an information noun follows
_APPEARING = re.compile(r"(?:מופיע|הופיע|נמצא|נכתב|כתוב|מובא|הובא|מוצג|הוצג)" + _INFLECTION)
# verbs of existing or being given: absence only when an information noun follows ("לא קיים מידע")
_GIVEN = re.compile(r"(?:קיימ|ניתנ)" + _INFLECTION)
_INFO_NOUN = re.compile(r"מידע|נתונ|נתונימ|פירוט|פרטימ|התייחסות|אזכור|ציונ|תיעוד|אינדיקציה")
_DOC_NOUN = re.compile(r"(?:מסמכ|קטע|מקור|טקסט|דוח)\w*|ראיות|ראיה|ראייה|שומה|שומות|שומת")
_KNOWING = re.compile(r"לקבוע|לדעת|לזהות|למצוא|להסיק|לאתר|לחשב|לברר")
_POSSIBLE = re.compile(r"ניתנ|אפשר|אפשרות|דרכ")
_QUANTIFIERS = frozenset({"כל", "שום", "כלל"})


def _forms(word: str) -> set[str]:
    """A word with final letters unified, and without one or two leading prefix letters (three letters stay)."""
    word = word.translate(_FINALS)
    out = {word}
    for k in (1, 2):
        if len(word) - k >= 3 and all(ch in _PREFIXES for ch in word[:k]):
            out.add(word[k:])
    return out


def _is(pattern: re.Pattern, word: str) -> bool:
    return any(pattern.fullmatch(f) for f in _forms(word))


def _first_content_word(words: list[str]) -> str | None:
    """The first word that is not a short function word ("בהם", "את"), a quantifier ("כל") or a document word ("במסמכים"): what a negation
    governs in "אין בהם נתון" or "אין במסמכי הראיות כל מידע"."""
    return next((w for w in words if not (len(w) <= 3 or w in _QUANTIFIERS or _is(_DOC_NOUN, w))), None)


def _absence_words(words: list[str]) -> bool:
    for i, w in enumerate(words):
        if w.translate(_FINALS) in _MISSING_WORDS:
            return True
        if w == "אי" and words[i + 1:i + 2] == ["אפשר"] and len(words) > i + 2 and _is(_KNOWING, words[i + 2]):
            return True
        if not is_negation(w, ambiguous=False):
            continue
        after = words[i + 1:i + 6]
        if after and after[0].translate(_FINALS) in _STATED_VERBS:  # "לא נמצא", "אינו מופיע", "לא נקבע"
            return True
        if any(_is(_STATING, x) for x in after[:2]):  # "לא נמסר", "אינם מפרטים"
            return True
        if len(after) >= 2 and _is(_POSSIBLE, after[0]) and _is(_KNOWING, after[1]):  # "לא ניתן לקבוע", "אין אפשרות לחשב"
            return True
        governed = _first_content_word(after)
        if governed is not None and _is(_INFO_NOUN, governed):  # "אין בהם נתון", "ללא פירוט"
            return True
        for j, x in enumerate(after[:2]):  # "אינו מופיע בשומה", "לא קיים מידע"
            rest = after[j + 1:j + 5]
            if _is(_APPEARING, x) and any(_is(_DOC_NOUN, y) or _is(_INFO_NOUN, y) for y in rest):
                return True
            if _is(_GIVEN, x) and any(_is(_INFO_NOUN, y) for y in rest):
                return True
    return False


def _tokens(text: str) -> list[str]:
    return _HEB_WORD.findall(base_normalize(text))


# A clause: the text between two clause ends. A clause end is a full stop (not inside a number, a date or the abbreviation "מ.ע.מ."), a
# semicolon, a line end, a question or exclamation mark, a bracket, a spaced dash, a comma that is no thousands separator, and the start of a contrast ("אלא", "אך",
# "אבל", "ואילו") or of a negated coordinate clause ("ולא", "ואין", "ואינו": "1,200 ₪ ולא 1,500 ₪" is two clauses).
_CLAUSE_END = re.compile(
    r"(?<!מ\.ע)\.(?!\d)(?!ע\.?מ(?![א-ת]))|[;!?\n()]|(?<!\d),|,(?!\d)|\s[—–]\s"
    r"|(?<![א-ת])(?=(?:אלא|אך|אבל|ואילו|ו(?:לא|אין|אינו|אינה|אינם|אינן))(?![א-ת]))")


def clause_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the clauses of ``text`` that hold something (a letter or a digit), in order; the clause ends are not inside them."""
    out, start = [], 0
    for m in [*_CLAUSE_END.finditer(text), None]:
        end = m.start() if m else len(text)
        if m is None or m.start() >= start:
            if re.search(r"\w", text[start:end]):
                out.append((start, end))
            start = m.end() if m else end
    return out


def absence_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the clauses of ``text`` (``clause_spans``) that say a datum is not stated, found, computed or determined. A caller that
    ignores what an absence clause names (a VAT phrase, a basis) ignores that clause, not the whole sentence beside it."""
    return [(a, b) for a, b in clause_spans(text) if absence_clause(text[a:b])]


def absence_clause(clause: str) -> bool:
    """Whether one clause says a datum is not stated, found, computed or determined: the reader of absence, over its words."""
    return _absence_words(_tokens(clause))


def states_absence(text: str) -> bool:
    """Whether ``text`` says that a datum is not stated, not found, not computed or not determined, and says nothing else: it has an absence
    clause and every clause that holds a figure is an absence clause. «לא נמצאו חריגות; מועד הסיום 2026» states a date: it is no absence, and a
    sentence that holds one cannot be dropped as an absence without taking its datum with it."""
    spans = clause_spans(text)
    absent = {span for span in spans if absence_clause(text[span[0]:span[1]])}
    return bool(absent) and all(span in absent or not re.search(r"\d", text[span[0]:span[1]]) for span in spans)


# a negation does not negate a datum when what follows it only qualifies the datum: "לא כולל מע״מ", "לא פחות מ-5%", "אינה כלולה בשטח"
_QUALIFYING = frozenset(
    "כולל כוללת כוללים כוללות כלול כלולה כלולים כלולות פחות יותר נמוך נמוכה גבוה גבוהה עולה עולים מעל מתחת יעלה יעלו יפחת יפחתו יפחות "
    "יאוחר יאוחרו יקדים יקדם רק".split())  # (the limit expressions: "לא יאוחר מ", "לא יעלה על", "לא פחות מ", "לא יותר מ")
_NEGATION_REACH = 2  # a negation governs the words right after it: «אינם 1,200 ₪», not the next clause's «ושטחו 300»
_WORD_OR_NUMBER = re.compile(r"[א-ת][א-ת״׳]*|\d[\d.,]*")


def _negation_reach(text: str) -> list[tuple[int, int, str]]:
    """(start, end, form) of every clause negation of ``text`` that governs a datum: from the negation word to the ``_NEGATION_REACH`` words
    after it. Not a preposition («ללא מע״מ» negates the VAT, not the datum) and not a limit expression, where the word after it only qualifies
    («לא כולל», «לא יאוחר מ», «לא יעלה על»)."""
    out = []
    for a, b in clause_spans(text):
        found = [(m.start() + a, m.group(0)) for m in _WORD_OR_NUMBER.finditer(text[a:b])]
        for k, (at, word) in enumerate(found):
            form = negation_form(base_normalize(word), ambiguous=False, prepositional=False)
            if form is None or (k + 1 < len(found) and base_normalize(found[k + 1][1]) in _QUALIFYING):
                continue
            last = found[min(k + _NEGATION_REACH, len(found) - 1)]
            out.append((at, last[0] + len(last[1]), form))
    return out


def negated_spans(text: str, ignore: frozenset[str] = frozenset()) -> list[tuple[int, int]]:
    """The spans of ``text`` that a negation governs (``_negation_reach``): «אינם 1,200 ₪», «לא 1,500»; the words before the negation are not
    governed by it. ``ignore``: negation forms that are not counted (the negations of an item's own quote: they are its datum's)."""
    return [(a, b) for a, b, form in _negation_reach(text) if form not in ignore]


def negations_in(text: str) -> frozenset[str]:
    """The clause negations ``text`` writes, each without its prefix («שאינם» is «אינם»). What an item's own quote negates is its datum: the
    same negation in the answer's clause is the datum's, not a negation of the item."""
    return frozenset(form for _, _, form in _negation_reach(text))


def holds_absence(text: str) -> bool:
    """Whether any clause of ``text`` says a datum is not stated, found, computed or determined."""
    return any(absence_clause(text[a:b]) for a, b in clause_spans(text))


def undouble_word(word: str) -> str:
    """A word whose every character appears twice in a row (``'ההננככסס'``, ``'2200,,550000'``) read once: what an
    older reader extracted from a bold font drawn twice, before char dedupe. Any other word is returned as is."""
    if len(word) >= 2 and len(word) % 2 == 0 and word[0::2] == word[1::2]:
        return word[0::2]
    return word
