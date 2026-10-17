"""The facet of a datum: the component, consideration or cash flow its source states (round 13 U3; R1, R2; KTD1-KTD3).

A coefficient or a rate means little without what it is for: two coefficients of one table, each for another
consideration, are parallel components of one appraisal, not two values of one datum. The facet is therefore a stored,
anchored record of the source's own words:

- ``written``: the words as the source wrote them, without a leading preposition (ל / עבור / בגין);
- ``part``: where they are (``row``, ``column``, ``caption``, ``sentence``, ``lead_in``, ``heading``; round 14 U2, KTD3: also
  ``preceding_sentence``, ``paragraph`` and ``table_note``; follow-up: ``label``, the label of a short list item or line);
- ``tier``: ``own`` (the value's own row label, its column header in a matrix, the purpose phrase of its own sentence)
  or ``context`` (the table caption, a table note, an earlier sentence or paragraph, the list lead-in, the nearest heading);
- ``status``: ``stated``, ``none`` (read, nothing stated), ``unparsed`` (read, ambiguous) or ``not_read`` (round 14 U2, KTD4);
  every state but ``stated`` carries a ``reason``, and a context facet its place: ``block`` and ``sentence`` (indexes) and
  ``at`` (the stub of the context sentence, stamped by ``tools``).

It is source text only: what the model wrote (``Value.label``, ``Value.subject``) never reaches this module. The
reading is a pure function of stored text (no model call, no database): ``tools`` hands it the cell's row, header and
caption, or the value's sentence with the candidates above it, once, when a datum enters the turn (KTD2).

Comparing two facets is ``relation`` (round 13 U4, KTD3): ``core`` is the facet's words normalized (quote marks unified,
prefix letters and plural endings folded, the family's generic and metric words dropped, digits kept), so two spellings
of one cash flow have one core. ``FACET_FAMILIES`` names the families whose datum needs a facet. ``canonical_amount``
(KTD5) is the one form in which two amounts are compared."""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from functools import lru_cache
from typing import NamedTuple

from app.measurements.values import parse_amount

STATUS_STATED, STATUS_NONE, STATUS_NOT_READ = "stated", "none", "not_read"
STATUS_UNPARSED = "unparsed"  # read, and the source's words cannot be attributed to the value (round 14 U2, KTD4)
TIER_OWN, TIER_CONTEXT = "own", "context"

# the reasons of a state that is not ``stated`` (round 14 U2, KTD4): what was checked, and why it settled nothing
REASON_NOT_IN_RANGE = "not_in_range"  # read the whole range (own sentence, earlier text of the unit and section): nothing names one
REASON_INTERVENING = "intervening_value"  # a component phrase, with a value of the same kind between it and this value
REASON_TWO_COMPONENTS = "two_components"  # the phrase names two components: which one is this value's is not said
REASON_PICTURE_CELL = "picture_cell"  # a cell of a picture: the text around it was not read
REASON_NOT_NEEDED = "not_needed"  # the family needs no component (the unit's headline datum)
REASON_CAP = "cap"  # the search reached its character cap before an accepted candidate
REASON_OWN_SENTENCE = "own_sentence"  # the value's own sentence describes it, and the reader could not tie the words to a component

# where a context facet was read from (``part``)
PART_PRECEDING_SENTENCE, PART_PARAGRAPH, PART_HEADING, PART_LEAD_IN = "preceding_sentence", "paragraph", "heading", "lead_in"
PART_TABLE_NOTE, PART_CAPTION, PART_LIST_ITEM = "table_note", "caption", "list_item"
# where an own facet is: the purpose phrase of the value's sentence, or the label of its short line or list item (round 14 U2 follow-up)
PART_SENTENCE, PART_LABEL = "sentence", "label"
# the parts read from the text above the value in its own paragraph flow: ``relation`` rule 3 counts them like the value's own words
# where the two data sit together, and across sections only beside a true own-tier facet
SENTENCE_PARTS = frozenset({PART_PRECEDING_SENTENCE, PART_PARAGRAPH})

# the families whose datum needs a facet (KTD3): a coefficient, and a proportion (a rate, a percentage, an
# adjustment). In any other family an empty or absent facet is the appraisal unit's headline datum
COEFFICIENT, PROPORTION = "coefficient", "proportion"
FACET_FAMILIES = frozenset({COEFFICIENT, PROPORTION})

LABEL_NOT_STATED = "לא צוין במקור"
LABEL_NOT_READ = "לא נקרא"
LABEL_FROM_CONTEXT = "מההקשר"
LABEL_FACET = "רכיב"
LABEL_UNPARSED = {
    REASON_INTERVENING: "נקרא ולא הוכרע: ערך אחר מאותו סוג עומד בין ההקשר לערך",
    REASON_TWO_COMPONENTS: "נקרא ולא הוכרע: ההקשר מונה שני רכיבים",
    REASON_OWN_SENTENCE: "נקרא ולא הוכרע: משפט הערך מתאר אותו, ולא ניתן לקשור את המילים לרכיב",
}
LABEL_NOT_READ_REASON = {
    REASON_PICTURE_CELL: "לא נקרא: תא בתמונה, הטקסט סביבו לא נקרא",
    REASON_CAP: "לא נקרא: החיפוש הגיע למגבלת הקריאה לפני שנמצא הקשר",
}


def facet(written: str, part: str, tier: str, **place) -> dict:
    """A stated facet. ``place``: where a context facet was read (``block``, ``sentence``, ``at``; round 14 U2, KTD3), kept
    apart from the value's own locator."""
    return {"status": STATUS_STATED, "written": written, "part": part, "tier": tier} | {
        k: v for k, v in place.items() if v is not None}


PUBLIC_FACET_KEYS = ("status", "reason", "written", "part", "tier")


def public_facet(f: dict | None) -> dict:
    """The facet as a client may read it (review, round 14; ``docs/api-contract.md``): ``status``, ``reason``, ``written``,
    ``part`` and ``tier`` only. The reader's internals — the block and sentence it read from, and ``at``, the offsets the context
    anchor is resolved from (``anchors.context_stub``) — stay on the workspace object and in the cache, never in a response, so a
    client cannot come to depend on a place format the server may change. A key the facet does not have is not added."""
    f = f or not_read()
    return {k: f[k] for k in PUBLIC_FACET_KEYS if k in f}


def none(reason: str = REASON_NOT_IN_RANGE) -> dict:
    """Read, and the source states no facet (the unit's headline datum, or a datum that cannot be placed): nothing in the range
    checked names one (``not_in_range``), or its family needs none (``not_needed``)."""
    return {"status": STATUS_NONE, "written": None, "part": None, "tier": None, "reason": reason}


def not_read(reason: str | None = None) -> dict:
    """Not read: the place the facet is read from was not at hand (no ``reason``), a picture cell, or the search reached its
    cap. It is never shown as "the source states none"."""
    return {"status": STATUS_NOT_READ, "written": None, "part": None, "tier": None, "reason": reason}


def unparsed(reason: str, part: str | None = None, tier: str = TIER_CONTEXT, **place) -> dict:
    """Read, and ambiguous (round 14 U2, KTD3, KTD4): the nearest text that names a component cannot be attributed to this value
    (``intervening_value``, ``two_components``), or the value's own sentence describes it in words no rule ties to one
    (``own_sentence``, tier own, part ``sentence``: nothing was borrowed). No component is stated, so ``core_of`` is empty;
    ``part`` and the place say where the rejected text is."""
    return {"status": STATUS_UNPARSED, "written": None, "part": part, "tier": tier, "reason": reason} | {
        k: v for k, v in place.items() if v is not None}


# how much a component state says (round 14 U1, KTD2): "not read" is nothing, "read" (nothing stated, or ambiguous) is more, "stated"
# is most. A state only moves forward
_STATE_RANK = {STATUS_NOT_READ: 0, STATUS_NONE: 1, STATUS_UNPARSED: 1, STATUS_STATED: 2}


def forward(first: dict | None, second: dict | None) -> dict:
    """The facet a datum keeps once a second registration of the same source span read one (round 14 U1, KTD2, R6): the
    second replaces the first only when it states more — a stated facet over one that was not read, read without a component or
    ambiguous, and a read one over one not read — and never the reverse. The model often registers again with a wider quote to
    fix a context, and this keeps what that read found; an equal state keeps the first."""
    first = first or not_read()
    second = second or not_read()
    return dict(second if _STATE_RANK.get(second.get("status"), 0) > _STATE_RANK.get(first.get("status"), 0) else first)


def reread(fresh: dict | None, old: dict | None, *, older_reader: bool) -> dict:
    """The facet a cached value keeps once its context is read again (round 14 U3, KTD5, R5; review #7): ``forward`` (never lower;
    equal states take the fresh read, which carries its reason), except that a *stated context-tier* facet written by an older
    reader (``older_reader``) is replaced by a fresh read that found nothing (``none``) or an ambiguity (``unparsed``). The old
    reader had no intervening-value or two-component rule, so a heading it borrowed across another value is exactly what the
    careful read now rejects; keeping it would present the wrong component as the source's own words. A facet written by the
    current reader, an own-tier facet (the value's own sentence or label: nothing was borrowed) and a read that was not made
    (``not_read``: a cap, an unreachable place) keep the forward-only rule."""
    fresh = fresh or not_read()
    old = old or not_read()
    if (older_reader and old.get("status") == STATUS_STATED and old.get("tier") == TIER_CONTEXT
            and fresh.get("status") in (STATUS_NONE, STATUS_UNPARSED)):
        return dict(fresh)
    return forward(fresh, old)


def family_of(kind: str) -> str:
    """The datum family of a kind, as ``resolve.family`` has it (a ratio and a rate are one proportion; value and
    value per area are one family); ``resolve`` is imported where used: it reaches the tools, which read this module."""
    from app.chat.resolve import family

    return family(kind or "")


def needs_facet(family: str) -> bool:
    return family in FACET_FAMILIES


def line(f: dict | None) -> str:
    """The facet as the model is told it (short, Hebrew): its written words, or the state in words (round 14 U2, KTD4, KTD12): the
    source states none in the range checked, the read was ambiguous (and why), or it was not read (and why) — never one for
    another."""
    f = f or {}
    status, reason = f.get("status"), f.get("reason")
    if status == STATUS_STATED:
        where = f" ({LABEL_FROM_CONTEXT})" if f.get("tier") == TIER_CONTEXT else ""
        return f"{LABEL_FACET}{where}: «{f.get('written')}»"
    if status == STATUS_NONE:
        return f"{LABEL_FACET}: {LABEL_NOT_STATED}"
    if status == STATUS_UNPARSED:
        return f"{LABEL_FACET}: {LABEL_UNPARSED.get(reason, 'נקרא ולא הוכרע')}"
    return f"{LABEL_FACET}: {LABEL_NOT_READ_REASON.get(reason, LABEL_NOT_READ)}"


# --- normalization (KTD3) ---------------------------------------------------------------------------------------------

_QUOTE_MARKS = dict.fromkeys(map(ord, "\"'״׳`’‘“”„‚‎‏"))  # one spelling of an abbreviation: no quote at all
_FINAL = str.maketrans("ךםןףץ", "כמנפצ")
_TOKEN = re.compile(r"[א-ת]+|[A-Za-z]+|\d+(?:[.,]\d+)*")
_PREFIX = "הולבמש"  # the prefix letters of the facet words (the definite article, and, to, in, from, that)
_PREFIXES_MAX = 3  # a stack of prefixes (ו+ל+ה) is at most three letters


def _flat(text: str) -> str:
    return (text or "").translate(_QUOTE_MARKS)


def _stem(word: str) -> str:
    """One spelling of a Hebrew word for comparison: a plural or construct ending folded (ים, ות; else a final י),
    then the prefix letters stripped while more than three letters remain. The suffix goes first, so a short stem
    (שטחי -> שטח) keeps its first letter, and the prefixes go greedily, so ל+מסחר and מסחר (and ה+מ+קורות and קורות)
    meet. Two words written the same way meet whatever they mean; the fold is a heuristic for two spellings of one
    phrase, never a lexicon."""
    if not word or not "א" <= word[0] <= "ת":
        return word
    if len(word) > 4 and word.endswith(("ים", "ות")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("י"):
        word = word[:-1]
    n = 0
    while n < _PREFIXES_MAX and len(word) > 3 and word[0] in _PREFIX:
        word, n = word[1:], n + 1
    return word.translate(_FINAL)


def _stems(words: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in _TOKEN.findall(_flat(words)))


# words that name no component in any family: a label, a pointer, an adoption, a structure (a heading's generic nouns),
# the preposition-like words of a purpose clause
_BASE_WORDS = _stems(
    "ערך ערכים נקבע שנקבע שנקבעו נקבעו נתון נתונים מדד רכיב רכיבים פרמטר טבלה טבלת סהכ סך כולל בשל עבור בגין בעבור לפי "
    "כללי תיאור פרט סעיף שווי נכס הנכס נכסים להלן לעיל בהתאם כמפורט מפורט בנפרד הבא הבאים הבאה כדלקמן אומץ אומצה "
    "שאומץ שומה בשומה סיכום הערות מבוא")
_COEFFICIENT_WORDS = _stems("מקדם מקדמי מקדמים הפחתה הפחתות הפחתת התאמה התאמות בסיסי בסיס")
_PROPORTION_WORDS = _stems("שיעור שיעורי שיעורים היוון תשואה ריבית אחוז אחוזים תזרים תזרימי תזרימים")
# the generic and metric words dropped per family; a coefficient is often written as a percent, and a rate as a
# ratio, so both facet families drop both families' metric words. Any other family drops only the base words
FAMILY_WORDS: dict[str, frozenset[str]] = {
    COEFFICIENT: _BASE_WORDS | _COEFFICIENT_WORDS | _PROPORTION_WORDS,
    PROPORTION: _BASE_WORDS | _COEFFICIENT_WORDS | _PROPORTION_WORDS,
}
# the words that introduce a component in a caption, a heading or a lead-in (a context text states a facet only when
# it holds one of them): a metric word is the cue that the text is about the coefficient or rate, and its other words
# then name the component
_BASIS_WORDS = _stems("בסיס בסיסי")  # "בסיס" is a word of a basis label, not a cue
_CUE_WORDS = (_COEFFICIENT_WORDS | _PROPORTION_WORDS) - _BASIS_WORDS
# "בהתאם" ("in accordance with") has the stem of the plural "התאמות" (an adjustment): it is a connective of nearly every sentence
# that cites an earlier one, never a cue (round 14 U2: a candidate needs a cue word, and "בהתאם לאמור" must not open a phrase)
_NOT_A_CUE = frozenset({"בהתאם", "בהתאמה"})


def _has_cue(text: str) -> bool:
    """Whether a text holds a cue word of a metric (a coefficient or a rate)."""
    return any(_stem(w) in _CUE_WORDS for w in _TOKEN.findall(_flat(text)) if w not in _NOT_A_CUE)


def generic_words(family: str) -> frozenset[str]:
    """The stems dropped from a facet of a family: its metric and generic words, and the base words of every family."""
    return FAMILY_WORDS.get(family, _BASE_WORDS)


@lru_cache(maxsize=4096)
def core(written: str | None, family: str = "") -> frozenset[str]:
    """The words of a facet, normalized, for comparing two spellings of one component (KTD3): quote marks unified,
    prefix letters and plural endings folded, the family's generic and metric words and every word of two letters or
    fewer dropped, digits kept. Empty: the text names no component."""
    generic = generic_words(family)
    out: set[str] = set()
    for token in _TOKEN.findall(_flat(written or "")):
        if token[0].isdigit():
            out.add(token.replace(",", ""))
            continue
        stem = _stem(token)
        if len(stem) > 2 and stem not in generic:
            out.add(stem)
    return frozenset(out)


def names_a_thing(words: frozenset[str]) -> bool:
    """A core with a word that is not a number: a year or a count alone is a period or an ordinal, never a component."""
    return any(not w[0].isdigit() for w in words)


def _states(text: str, family: str) -> bool:
    return names_a_thing(core(text, family))


def facet_family(kind: str, *texts: str) -> str:
    """The family a datum reads its facet as: its kind's, and for a kind that names none (``other``, unknown) the one
    a metric word of its own row, sentence or caption names."""
    family = family_of(kind)
    if family in FACET_FAMILIES or kind not in ("other", "unknown", ""):
        return family
    stems = _stems(" ".join(texts))
    if stems & (_COEFFICIENT_WORDS - _BASIS_WORDS):
        return COEFFICIENT
    if stems & _PROPORTION_WORDS:
        return PROPORTION
    return family


# --- table cells ---------------------------------------------------------------------------------------------------------

def read_cell(row_label: str | None, header: str | None, caption: str | None = None, title: Sequence[str] = (),
              *, family: str = "", matrix: bool = False, notes: Sequence[str] = ()) -> dict:
    """The facet of a table cell (R1): its row label is the own facet; the column header names it when the row label
    states nothing (a generic label such as "הערך שנקבע"); a (row label, column header) pair when both name things in a
    matrix (more than one value column). The caption and the table's title are the context facet, read only in a family
    that needs a facet and only when they hold a metric word and name a component beside it. Last, in the same family, the
    table's notes (round 14 U2, KTD3): a note that names the row label and states a purpose phrase for it
    (``table_note``). A totals or aggregate column header (``_totals``) is no component at all: a row that holds only the metric word
    reads the caption and the title as round 13 does. Nothing stated: ``none``."""
    row, col = (row_label or "").strip(), (header or "").strip()
    if col and _totals(col):
        col = ""  # a totals or aggregate column is never a component: not the column facet, not a qualifier of the row (round 14, KTD3)
    if row and _states(row, family):
        if matrix and col and _states(col, family):
            return facet(f"{row} — {col}", "row", TIER_OWN)
        return facet(row, "row", TIER_OWN)
    if col and _states(col, family):
        return facet(col, "column", TIER_OWN)
    if not needs_facet(family):
        return none(REASON_NOT_NEEDED)
    for text in (caption or "", *title):
        if (found := _context(text, family, PART_CAPTION)) is not None:
            return found
    for index, note in enumerate(notes):
        if (found := _note(note, row, family, index)) is not None:
            return found
    return none()


# the words of a totals or aggregate column, with the usual abbreviation forms (סה"כ, סה״כ, ס"ה, סך הכל, ממוצע, ממוצ', מצטבר ...)
_INCLUDING = _stem("כולל")
_TOTAL_WORDS = _stems("סהכ סה סכ סך כולל סיכום ממוצע ממוצעת ממוצעים ממוצ מצטבר מצטברת מצטברים מצטב")


def _totals(header: str) -> bool:
    """Whether a column header is a total or an average column (round 14 U2 follow-up, KTD3): any word of it is a totals word
    ("סה"כ מר"א" is one). Such a header names no component in any case: it is neither the column facet nor a qualifier of a row label
    ("שיעור היוון (בתחשיב …)" is the component by itself; the metric word alone reads the caption)."""
    words = [_stem(w) for w in _TOKEN.findall(_flat(header)) if not w[0].isdigit()]
    if any(w in _TOTAL_WORDS and w != _INCLUDING for w in words):
        return True
    return _INCLUDING in words and not _has_cue(header)  # "כולל" also means "including": "מקדם התאמה כולל" is a name, not a total


def _note(note: str, row_label: str, family: str, index: int) -> dict | None:
    """A table note as the context of a cell (round 14 U2, KTD3, R1): the note must name the cell's row label (the label
    as written, so a footnote about another row never reaches this one) and hold a cue word with a purpose phrase for it. A note
    that names two components is ambiguous, not attributed."""
    label = " ".join(_flat(row_label).split())
    text = " ".join(_flat(note).split())
    if len(label) < 2 or label not in text:
        return None
    phrase, two = _candidate_phrase(note, family)
    if phrase is None:
        return None
    if two:
        return unparsed(REASON_TWO_COMPONENTS, PART_TABLE_NOTE, sentence=index)
    return facet(phrase, PART_TABLE_NOTE, TIER_CONTEXT, sentence=index)


def _context(text: str, family: str, part: str, **place) -> dict | None:
    """A caption, a lead-in or a heading as a context facet, or None: it must hold a metric word of the family (the cue
    that it speaks of the coefficient or rate) and name a component beside it."""
    cleaned = _unnumbered(text).rstrip(" :").strip()
    if not cleaned or not _has_cue(cleaned) or not _states(cleaned, family):
        return None
    return facet(tidy(cleaned), part, TIER_CONTEXT, **place)


def _unnumbered(text: str) -> str:
    """A text without the section number or list letter that opens it ("3.2 …", "א. …")."""
    return re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[א-ת][.)])\s+", "", (text or "").strip())


# --- quote values ----------------------------------------------------------------------------------------------------------

_CONNECTORS = {"עבור", "בגין", "בעבור"}
# the head words of a phrase after ל that are a period, a unit, a date, a basis or a pointer, never a component
_NOT_A_COMPONENT = _stems(
    "שנה שנים שנתי שנתית חודש חודשים חודשי רבעון יום ימים תקופה מר מטר דונם יחידה תאריך מועד פי כל ללא כולל עד אחר "
    "עומת מעט רבות הלן עיל משך פני מעמ בסיס בשנה צורך")
_CLAUSE_END = ",;:.)"
_STOP_WORDS = frozenset({"הוא", "היא", "הינו", "הינה", "עומד", "עומדת", "נקבע", "נקבעה", "אומץ", "אומצה"})
_PHRASE_MAX = 8


# function words and connectors that open or close a phrase without belonging to the component it names ("…מועטה של"
# before a number, "בשיעור …" after one)
_EDGE_WORDS = frozenset({"של", "על", "עם", "את", "ב", "ל", "ו", "בגובה", "בשיעור", "בסך", "הוא", "היא", "-", "–", "—"})


def tidy(written: str) -> str:
    """A facet's written words without the function words, connectors, dashes and colons at either edge; the words
    inside are the source's as they are. The core of the text is the same before and after."""
    words = [w for w in (written or "").split() if w.strip(":,;")]
    while words and (words[-1].strip(":,;") in _EDGE_WORDS or not _bare(words[-1])):
        words.pop()
    while words and (words[0].strip(":,;") in _EDGE_WORDS or not _bare(words[0])):
        words.pop(0)
    return " ".join(words).strip(" :,;-–—")


def _bare(token: str) -> str:
    return token.strip(" ()[]\"'״׳,;:.!?")


def _number_token(token: str, written: str) -> bool:
    return bool(written) and re.search(rf"(?<![\d.,]){re.escape(written)}(?!\d|[.,]\d)", token) is not None


def _excluded_head(word: str) -> bool:
    """Whether a word that opens a purpose phrase is a period, unit, date, basis or pointer term (or a number)."""
    flat = _flat(word)
    return not flat or flat[0].isdigit() or _stem(flat) in _NOT_A_COMPONENT or flat in _NOT_A_COMPONENT


def _find_phrase(sentence: str, written: str, family: str) -> tuple[str, int, list[str]] | None:
    """The purpose phrase of a sentence, ``(phrase, index of the token that opens it, tokens)``, or None: the words after
    ל / עבור / בגין / בעבור that follow the number (``written``; none when empty) or the metric word, to the clause end. See
    ``read_sentence`` for the rules. With a number, the phrase right after it wins over one before it (round 14 U2 follow-up): the
    one before is kept only when none follows, and only when its clause did not end before the number."""
    tokens = sentence.split()
    bare = [_bare(t) for t in tokens]
    at = next((i for i, t in enumerate(tokens) if _number_token(t, written)), None)
    metric = next((i for i, t in enumerate(bare) if _flat(t) not in _NOT_A_CUE and _stem(_flat(t)) in _CUE_WORDS
                   and (at is None or i < at)), None)
    marks = [i for i in (at, metric) if i is not None]
    j = min(marks) + 1 if marks else 0
    before: tuple[str, int, list[str]] | None = None
    closed = False  # a clause ended before the number: a phrase between that end and the number is another clause's
    while j < len(tokens):
        word = _flat(bare[j])
        first = None
        if word in _CONNECTORS:
            first, head = j + 1, bare[j + 1] if j + 1 < len(tokens) else ""
        elif bare[j].startswith("ל") and len(word) >= 3:
            first, head = j, bare[j][1:]
        if first is not None and head and not _excluded_head(head):
            words = [head] + [_bare(t) for t in _after(tokens, first)]
            phrase = tidy(" ".join(w for w in words if w))
            if _states(phrase, family):
                if at is None or j > at:
                    return phrase, first, tokens
                if before is None and not closed:
                    before = (phrase, first, tokens)
        if tokens[j][-1:] in ",;:" and (at is None or j < at):
            if at is None:
                return None  # the clause ended before the number: a phrase after it is another clause's
            closed = True
        j += 1
    return before


def read_sentence(sentence: str, written: str, family: str = "") -> dict | None:
    """The purpose phrase of the value's own sentence as an own facet, or None: the words after ל / עבור / בגין / בעבור
    that follow the number or the metric word, to the clause end (a comma, a semicolon, a colon, a full stop, another
    number or a conjoined "ול…" purpose). A phrase whose head word is a period, a unit, a date or a basis term (לשנה,
    למ״ר, למועד, לפי …) is not a facet, and a phrase of only generic words names no component."""
    found = _find_phrase(sentence, written, family)
    return facet(found[0], PART_SENTENCE, TIER_OWN) if found is not None else None


def _after(tokens: list[str], first: int) -> list[str]:
    """The tokens after the one that opens a phrase, up to the clause end."""
    out: list[str] = []
    if tokens[first][-1:] in _CLAUSE_END:
        return out
    for k in range(first + 1, min(len(tokens), first + _PHRASE_MAX)):
        t = tokens[k]
        word = _bare(t)
        if word in _STOP_WORDS or re.match(r"^ו?-?\d", word) or (word.startswith("ול") and len(word) > 3):
            break
        out.append(t)
        if t[-1:] in _CLAUSE_END:
            break
    return out


# --- the value's own sentence decides what may be borrowed (round 14 U2 follow-up, R2) --------------------------------------
#
# A value's context is borrowed only when its own sentence leaves the question open: it is bare (the number, its unit, the metric's
# cue words and the words of taking a value into the appraisal) or it points back at the text above. A sentence that describes
# the value in other words is the value's own description: when no rule ties those words to a component the facet is
# ``unparsed`` (``own_sentence``) and nothing is borrowed — a neighbour's component on a value that describes itself is the one
# error the round's precision criterion rules out. A short line "<label> – <number>" is its own label.

# the words of taking a value into the appraisal, the document's own words and connectives: they describe no component
_OWN_FUNCTION = _stems(
    "הובא הובאה הובאו הבאתי נלקח נלקחה נלקחו נקבע נקבעה נקבעו בחשבון חשבון חוות בחוות דעת זו זה זאת שומה הערכה בשיעור בגובה של "
    "הוא היא הינו הינה עומד עומדת אומץ אומצה גם אך כי אשר כאשר לכן ולכן זאת כן שנת")
# the markers of a sentence that points back at the text above (נוסף, כאמור, האמור, לעיל, הנ"ל), in every prefixed spelling
_BACK_REFERENCE = _stems("נוסף נוספת נוספים נוספות האמור כאמור לאמור לעיל דלעיל הנל")
_THIS = frozenset({"זה", "זו", "זאת"})
LINE_WORDS_MAX = 15  # a "<label> – <number>" line is short; a longer sentence is a description


def _unit_phrase_end(tokens: list[str], i: int) -> int:
    """The index of the last token of a period, unit, date, basis or pointer phrase that opens at token ``i`` ("לשנה", "למ״ר",
    "עבור שנה", "לפי המקור" ...), or ``i - 1`` when none opens there: the words of such a phrase describe no component."""
    word = _flat(_bare(tokens[i]))
    if word in _CONNECTORS:
        if i + 1 < len(tokens) and _excluded_head(_bare(tokens[i + 1])):
            return i + len(_after(tokens, i + 1)) + 1
    elif word.startswith("ל") and len(word) >= 3 and _excluded_head(word[1:]):
        return i + len(_after(tokens, i))
    return i - 1


def _content_words(sentence: str, written: str) -> list[str]:
    """The words of a value's own sentence that describe something: not the number and its unit, the family's cue words, the
    function words and taking verbs (``_OWN_FUNCTION``), the base and period words, the words of a period, unit or basis phrase,
    nor a word of two letters or fewer. A word in another script counts."""
    tokens = sentence.split()
    out: list[str] = []
    skip = -1
    for i, token in enumerate(tokens):
        if i <= skip:
            continue
        if _number_token(token, written):
            continue
        if (end := _unit_phrase_end(tokens, i)) >= i:
            skip = end
            continue
        for word in _TOKEN.findall(_flat(token)):
            if word[0].isdigit():
                continue
            stem = _stem(word)
            if len(stem) > 2 and stem not in _CUE_WORDS and stem not in _BASIS_WORDS and stem not in _BASE_WORDS \
                    and stem not in _OWN_FUNCTION and stem not in _NOT_A_COMPONENT:
                out.append(word)
    return out


def _points_back(sentence: str) -> bool:
    """Whether a sentence carries a back-reference marker (נוסף, כאמור, האמור, לעיל, דלעיל, הנ"ל), or a "this" right after the
    metric word ("מקדם הפחתה זה", "שיעור זה")."""
    words = _TOKEN.findall(_flat(sentence))
    if any(_stem(w) in _BACK_REFERENCE for w in words if not w[0].isdigit()):
        return True
    return any(b in _THIS and _stem(a) in _CUE_WORDS for a, b in zip(words, words[1:], strict=False))


def _label_of(sentence: str, written: str, family: str) -> dict | None:
    """The label of a short line or list item "<label> – <number>" (also "-", "—", ":"), as an own facet, or None: at most
    ``LINE_WORDS_MAX`` words, the number the last thing in the line (a unit sign or a percent word may follow), a separator right
    before it, and a label of content words with no cue word of the metric (a line that holds one is a description) and no value of
    its own. The label is the source's words, tidied of a leading connector and the edge words."""
    tokens = sentence.split()
    if not written or not 2 <= len(tokens) <= LINE_WORDS_MAX:
        return None
    at = max((i for i, t in enumerate(tokens) if _number_token(t, written)), default=None)
    if at is None or at == 0:
        return None
    if any(_TOKEN.search(_flat(t)) and _stem(_flat(_bare(t))) not in _PROPORTION_WORDS for t in tokens[at + 1:]):
        return None  # a word follows the number: the line is a sentence
    before = tokens[at - 1]
    if before in ("-", "–", "—"):
        words = tokens[:at - 1]
    elif before[-1:] in (":", "-", "–", "—"):
        words = tokens[:at]
    else:
        return None
    while words and _flat(_bare(words[0])) in _CONNECTORS:
        words = words[1:]
    label = tidy(" ".join(words))
    if not label or _has_cue(label) or family_number(label) or not _states(label, family):
        return None
    return facet(label, PART_LABEL, TIER_OWN)


def own_facet(sentence: str, written: str, family: str = "") -> dict | None:
    """What a value's own sentence settles of its facet (round 14 U2 follow-up, R2), or None when it settles nothing and the text
    above may be read: the purpose phrase of the sentence (``read_sentence``) in any family; then, in a family that needs a facet,
    the label of a short line (``_label_of``); then ``unparsed`` (reason ``own_sentence``, part ``sentence``, tier own) when the
    sentence holds a content word and points back at nothing. Bare (no content word) or back-referring: None."""
    if (own := read_sentence(sentence, written, family)) is not None:
        return own
    if not needs_facet(family):
        return None
    if (label := _label_of(sentence, written, family)) is not None:
        return label
    if _points_back(sentence) or not _content_words(sentence, written):
        return None
    return unparsed(REASON_OWN_SENTENCE, PART_SENTENCE, TIER_OWN)


# --- the context candidates (round 14 U2, KTD3) -----------------------------------------------------------------------------

# a ו+ל word that opens no second purpose ("ולכן", "ולאחר" ...)
_NOT_A_PURPOSE = frozenset({"ולכן", "ולפיכך", "ולאחר", "ולפי", "ולמרות", "ולעומת", "ולצורך", "ולאור", "ולמעשה", "ולהלן", "ולעיל"})
_VAV_CONNECTORS = frozenset({"ובגין", "ועבור", "ובעבור"})
_PERCENT = re.compile(r"\d[\d.,]*\s*(?:%|אחוז)")
_COEFFICIENT_NUMBER = re.compile(r"(?<![\d.,])[01][.,]\d+(?!\d|[.,]\d)")


def family_number(text: str) -> bool:
    """Whether a text states a number of the kind a facet is read for — a percent, or a coefficient (a decimal 0.x or 1.x) — the
    other value that would stand between a context phrase and the value it might name (R2). A section number that opens the
    text is no value."""
    body = _unnumbered(text)
    return bool(_PERCENT.search(body) or _COEFFICIENT_NUMBER.search(body))


def _second_purpose(tokens: list[str], first: int) -> bool:
    """Whether the sentence names a second purpose after the one that opens at token ``first``: another connector or a
    conjoined "ול…" / "ובגין" in the same clause, or an explicit "ובגין / ועבור" after the clause end."""
    open_clause = tokens[first][-1:] not in _CLAUSE_END
    for k in range(first + 1, len(tokens)):
        word = _flat(_bare(tokens[k]))
        if word in _VAV_CONNECTORS:
            return True
        if open_clause and (word in _CONNECTORS or (word.startswith("ול") and len(word) > 3 and word not in _NOT_A_PURPOSE)):
            return True
        if tokens[k][-1:] in _CLAUSE_END:
            open_clause = False
    return False


def _candidate_phrase(text: str, family: str) -> tuple[str | None, bool]:
    """The component an earlier sentence, paragraph or note states: ``(phrase, names two)``. The text must hold a cue word of
    the family and a purpose phrase (``read_sentence``'s phrase rules, here without a number to anchor on); None: it states none."""
    body = _unnumbered(text)
    if not _has_cue(body):
        return None, False
    found = _find_phrase(body, "", family)
    if found is None:
        return None, False
    return found[0], _second_purpose(found[2], found[1])


class Candidate(NamedTuple):
    """A text above a value that may state its component: ``part`` is where it is (``PART_*``), ``block`` its block index and
    ``sentence`` its index among the block's sentences (``sentences_of``)."""

    text: str
    part: str
    block: int | None = None
    sentence: int | None = None

    def place(self) -> dict:
        """Where the candidate is, as the keywords of ``facet`` / ``unparsed`` take it."""
        return {"block": self.block, "sentence": self.sentence}


class Reach(NamedTuple):
    """What a bounded read of the text above a value covered, nearest first (``reach``): the candidates, whether the character
    cap cut the search short (a search that ends so with no accepted candidate is not read, never "none"), and the text of the
    blocks the candidates are in (their places are located in them)."""

    candidates: list
    capped: bool = False
    blocks: dict = {}  # noqa: RUF012 - never mutated: an immutable NamedTuple default


def _accepted(c: Candidate, family: str) -> tuple[dict | None, bool]:
    """What a candidate states: ``(a stated facet, whether it names two components)``; ``(None, False)``: it states none."""
    if c.part == PART_LIST_ITEM:
        return None, False  # a sibling of the list: it holds numbers that stand between, never the component
    place = c.place()
    if c.part in (PART_HEADING, PART_LEAD_IN):
        found = _context(c.text, family, c.part, **place)
        if found is None:
            return None, False
        phrase = _find_phrase(_unnumbered(c.text), "", family)
        return found, bool(phrase is not None and _second_purpose(phrase[2], phrase[1]))
    phrase, two = _candidate_phrase(c.text, family)
    return (facet(phrase, c.part, TIER_CONTEXT, **place) if phrase is not None else None), two


def read_context(preceding: Reach, family: str = "") -> dict:
    """The bounded context read of a quote value whose sentence states no facet (round 14 U2, KTD3, KTD4; R1, R2): the
    candidates above it, nearest first (a ``Reach``).

    The first candidate that holds a component phrase decides: it is the value's facet (``stated``, tier context) unless
    - it names two components: ``unparsed``, reason ``two_components``;
    - a number of the same kind stands between it and the value, or the candidate states one itself (the phrase is that
      value's): ``unparsed``, reason ``intervening_value``. A list lead-in is not blocked by the numbers of its own list items:
      the items are peers, the lead-in is theirs all.
    A candidate with no phrase is passed over (its numbers count as standing between). Nothing accepted: ``none`` (reason
    ``not_in_range``), or ``not_read`` (reason ``cap``) when the character cap ended the search before its range did."""
    seen_text = seen_item = False
    for c in preceding.candidates:
        found, two = _accepted(c, family)
        has_number = family_number(c.text)
        if found is not None:
            place = c.place()
            if two:
                return unparsed(REASON_TWO_COMPONENTS, c.part, **place)
            if seen_text or has_number or (seen_item and c.part != PART_LEAD_IN):
                return unparsed(REASON_INTERVENING, c.part, **place)
            return found
        if has_number:
            seen_item = seen_item or c.part == PART_LIST_ITEM
            seen_text = seen_text or c.part != PART_LIST_ITEM
    return not_read(REASON_CAP) if preceding.capped else none()


def read_quote(sentence: str, written: str, family: str = "", preceding: Reach | None = None) -> dict:
    """The facet of a value quoted from a text: what its own sentence settles (``own_facet``: its purpose phrase, the label of a
    short line, or an own description that names no component); else, in a family that needs a facet, what the candidates above it
    state (``read_context``: context) — only for a bare or back-referring sentence; else ``none``. ``preceding`` None: the text
    above was not at hand, so a family that needs a facet is ``not_read`` rather than ``none``."""
    if (own := own_facet(sentence, written, family)) is not None:
        return own
    if not needs_facet(family):
        return none(REASON_NOT_NEEDED)
    if preceding is None:
        return not_read()
    return read_context(preceding, family)


# --- line wraps (round 14 follow-up) -------------------------------------------------------------------------------------
# PDF extraction stores a paragraph block as lines joined by "\n": the line wraps mid-sentence. The context reader reads a wrap as
# a space (``unwrap``); the sentence splitter of ``professional.sections`` (standards parsing, limits) is not changed.

_LINE_BREAK = re.compile(r"(?<!\n)\n(?!\n)")  # one "\n" alone; a run of them is a paragraph break
_BREAK_PUNCT = (".", "!", "?", "…", ":")  # a line ending so ends its sentence (":" opens a list)
_CLOSERS = "\"'”’״׳)]"  # closing marks that may follow the punctuation
# a list item's opening: a bullet, a dash or a number or Hebrew letter with "." or ")" and a space ("2.5 מ"ר" or "-5%" are not)
_LIST_MARKER = re.compile(r"[•*·]|(?:[-–—]|\d{1,3}[.)]|[א-ת][.)])(?=\s)")


def unwrap(text: str | None) -> str:
    """A paragraph block's text with each line wrap read as a space (round 14 follow-up, KTD3): a "\n" ends no sentence unless
    the line before it ends with sentence punctuation (. ! ? … :) or the next line opens a list item (a bullet, a dash, a number or a
    Hebrew letter with "." or ")"); a run of "\n" is a paragraph break and stays. The replacement has the same length, so every
    offset computed on the result (an anchor, a span, a sentence's place) is valid against the stored text."""
    text = text or ""

    def one(m: re.Match) -> str:
        start = text.rfind("\n", 0, m.start()) + 1
        before = text[start:m.start()].strip().rstrip(_CLOSERS)
        after = text[m.end():m.end() + 24].lstrip(" \t")
        if not before or not after or after[0] == "\n" or before.endswith(_BREAK_PUNCT):
            return "\n"
        return "\n" if _LIST_MARKER.match(after) else " "

    return _LINE_BREAK.sub(one, text)


def sentences_of(text: str | None) -> list[str]:
    """The sentences of a paragraph block, a line wrap not ending one (``unwrap``): ``professional.sections.sentences`` of the
    unwrapped text. The index of a sentence in this list is the one ``Candidate.sentence`` and a facet's ``sentence`` carry."""
    from app.professional.sections import sentences

    return sentences(unwrap(text))


def _overlapping(sentences: Sequence[str], text: str, quote: str) -> list[int]:
    """The indexes of the sentences of a block that the value's quote overlaps, in order; empty when the quote is not found in the
    block (then no sentence of the block is offered as a candidate)."""
    flat = " ".join(_flat(text).split())
    wanted = " ".join(_flat(quote).split())
    start = flat.find(wanted) if wanted else -1
    if start < 0:
        return []
    end, pos, out = start + len(wanted), 0, []
    for i, sentence in enumerate(sentences):
        one = " ".join(_flat(sentence).split())
        at = flat.find(one, pos)
        if at < 0:
            continue
        pos = at + len(one)
        if at < end and pos > start:
            out.append(i)
    return out


def _own_range(sentences: Sequence[str], text: str, quote: str) -> int:
    """The index of the first sentence of a block that the value's quote overlaps; -1 when the quote is not found in the block."""
    found = _overlapping(sentences, text, quote)
    return found[0] if found else -1


def own_sentence(block_text: str | None, quote: str, written: str = "") -> str | None:
    """The whole sentence of a block that a value's quote is in, a line wrap not ending it (``unwrap``): the quote may be a line, or a
    stretch of the sentence, of a PDF paragraph. ``written`` (the number as written): of the sentences the quote overlaps, the
    first that holds the number; without it, the one sentence that holds the whole quote. None: the quote is not in the block (or
    spans sentences without the number), so the caller keeps the sentence it had."""
    sentences = sentences_of(block_text)
    found = _overlapping(sentences, block_text or "", quote)
    if written:
        return next((sentences[i] for i in found if _number_token(sentences[i], written)), None)
    flat = " ".join(_flat(quote).split())
    return next((sentences[i] for i in found if flat in " ".join(_flat(sentences[i]).split())), None)


def reach(rows: Sequence, at: int, quote: str = "", *, path: Sequence[str] | None = None, lo: int = 0,
          chars: int = 6000, truncated: bool = False) -> Reach:
    """The candidates above a quote value, nearest first (round 14 U2, KTD3; R1, R2), from ``rows``: blocks with
    ``block_index``, ``kind``, ``text`` and ``section_path``, holding the block ``at`` that has the value and the blocks above it.

    1. the earlier sentences of the holder block (the value's own sentence, and what follows it, excluded);
    2. the earlier paragraph blocks of the same section, a sentence at a time, nearest first; a paragraph that ends in a colon is
       a ``lead_in`` as a whole, and a list item is kept only for the numbers it holds;
    3. the heading of the section, which ends the read.
    A paragraph's sentences are those of ``sentences_of``: a PDF line wrap inside a sentence is no sentence end.
    The read stops at a table block, at a block of another section (its heading or its text) and at ``lo``, the first block of
    the appraisal unit. ``chars`` caps the text of the candidates: the one that crosses it is dropped, the search ends there and
    ``capped`` is set. ``truncated``: ``rows`` were cut by the reader's row limit, so a block of the range that is missing from
    them means the search did not reach its edge (``capped``); otherwise a missing block simply ends the range."""
    split = sentences_of  # a line wrap is no sentence end (``unwrap``)

    by = {r.block_index: r for r in rows}
    holder = by.get(at)
    here = tuple(path) if path is not None else tuple(getattr(holder, "section_path", None) or ())
    out: list[Candidate] = []
    texts: dict[int, str] = {}
    used = 0
    capped = False

    def add(c: Candidate) -> bool:
        nonlocal used, capped
        used += len(c.text)
        if used > chars:
            capped = True
            return False
        out.append(c)
        return True

    if holder is not None:
        sents = split(holder.text or "")
        texts[at] = holder.text or ""
        for i in range(_own_range(sents, holder.text or "", quote) - 1, -1, -1):
            if not add(Candidate(sents[i], PART_PRECEDING_SENTENCE, at, i)):
                return Reach(out, capped, texts)
    for k in range(at - 1, lo - 1, -1):
        r = by.get(k)
        if r is None:
            return Reach(out, truncated, texts)
        if tuple(r.section_path or ()) != here or r.kind == "table":
            break
        text = (r.text or "").strip()
        if r.kind == "heading":
            if text:
                texts[k] = r.text
                add(Candidate(text, PART_HEADING, k, 0))
            break
        if not text or r.kind not in ("paragraph", "li"):
            continue
        texts[k] = r.text
        if r.kind == "li":
            if not add(Candidate(text, PART_LIST_ITEM, k, 0)):
                break
        elif text.endswith(":"):
            if not add(Candidate(text, PART_LEAD_IN, k, 0)):
                break
        else:
            sents = split(text)
            if not all(add(Candidate(sents[i], PART_PARAGRAPH, k, i)) for i in range(len(sents) - 1, -1, -1)):
                break
        if capped:
            break
    return Reach(out, capped, texts)


def sentence_with(text: str, written: str) -> str:
    """The sentence of a text that holds a number as written (the whole text when none does). A line wrap is no sentence end
    (``unwrap``)."""
    for part in re.split(r"(?<=[.!?])\s+|\n", unwrap(text)):
        if _number_token(part, written):
            return part.strip()
    return (text or "").strip()


# --- the canonical amount (round 13 U4, KTD5) ---------------------------------------------------------------------------

# a datum's amount as two data are compared: a Decimal, or (low, high) for a range. Never a string
Amount = Decimal | tuple[Decimal, Decimal]


def _fraction(value: Decimal, unit: str, family: str) -> Decimal:
    """A coefficient or a proportion in the form of a fraction: a percent is divided by 100, so the unit ``percent`` ("80%" is
    stored as 80) and the unit ``ratio`` (0.8) meet. Any other family keeps its number."""
    return value / 100 if unit == "percent" and needs_facet(family) else value


def canonical_amount(value: Decimal | None, *, scale: int = 1, unit: str = "", family: str = "") -> Decimal | None:
    """The amount of a registered value (V#) as it is compared (KTD5): the number normalized (``8.0`` is ``8``) and multiplied by
    the scale its source states ("באלפי ₪"), as a fraction in a coefficient or proportion family. None: no amount."""
    if value is None:
        return None
    return _fraction(value * (scale or 1), unit, family).normalize()


def canonical_measurement(value: object, value_text: str, *, unit: str = "", family: str = "") -> Amount | None:
    """The amount of a stored measurement (M#) as it is compared (KTD5): a range by its bounds (``parse_amount`` of the
    written text), else the extraction's own number (``value``), else the number ``parse_amount`` reads in the text; in the same
    form as ``canonical_amount``. None: the text holds no number (a datum of words: it is compared as the words)."""
    parsed = parse_amount(value_text or "")
    if parsed is not None and parsed.form == "range" and parsed.low is not None and parsed.high is not None:
        return (_fraction(parsed.low, unit, family).normalize(), _fraction(parsed.high, unit, family).normalize())
    number = Decimal(str(value)) if value is not None else (parsed.value if parsed is not None else None)
    return _fraction(number, unit, family).normalize() if number is not None else None


# --- the source span (round 14 U1, KTD1) ------------------------------------------------------------------------------------

def quote_span(version_id, reading_id: str | None, anchor: dict | None) -> tuple | None:
    """Where a quote value's number is in its source: the version, the reading, the block that holds the number and the
    number's character offset in that block's text — from the anchor ``anchors.locate_quote`` gave (``number``: [block, start,
    end]). One quote can hold one number twice and two quotes can cover one number, so neither a quote nor a quote index is a
    place. None when the quote was located to block precision only (found more than once in the read range) or not at all."""
    number = (anchor or {}).get("number")
    if not number or len(number) < 2:
        return None
    return ("quote", str(version_id), reading_id, number[0], number[1])


def cell_span(version_id, reading_id: str | None, table, row: int, column: int) -> tuple:
    """Where a table cell's number is: the version, the reading, the table (its index, or the region and index of a table a
    visual read transcribed), the row and the column (``row_number`` and ``column_number``: counted from 1)."""
    return ("cell", str(version_id), reading_id, table, row, column)


# --- the decision (round 13 U4, KTD3) -----------------------------------------------------------------------------------

SAME, DIFFERENT, UNDECIDED = "same", "different", "undecided"


class Site(NamedTuple):
    """What ``relation`` reads of a datum: ``timeline.Datum`` has the same attributes. ``appraisal``: the appraisal unit it is in
    ((version, unit number)); ``table``: (version, table index) of its cell, or None; ``section``: the section path of its
    block; ``span``: where the number is in its source (``quote_span``, ``cell_span``; round 14 U1, KTD1), or None when it
    cannot be placed to a number."""

    family: str
    facet: dict | None
    appraisal: tuple | None = None
    table: tuple | None = None
    section: str = ""
    span: tuple | None = None


def core_of(site) -> frozenset[str]:
    """The core of a datum's stated facet; empty when it states none (a facet of the status ``none`` or ``not_read``, or one
    that reduces to generic words only)."""
    f = site.facet or {}
    if f.get("status") != STATUS_STATED:
        return frozenset()
    return core(f.get("written") or "", site.family)


def headline(site, in_unit: int = 1) -> bool:
    """Whether a datum is its appraisal unit's headline datum (KTD3): it states no facet, and either its family needs none or it
    is the only datum of its base key in its unit (``in_unit``: how many data of that key the unit holds, counted by source span: see
    ``timeline._classify``)."""
    return not core_of(site) and (not needs_facet(site.family) or in_unit <= 1)


def _together(a, b) -> bool:
    """Whether two data of one appraisal unit are structurally side by side: the same table, or the same section path (a list
    and its lead-in always sit in one section, so a list needs no signal of its own: values do not store their block)."""
    if a.appraisal is None or a.appraisal != b.appraisal:
        return False
    if a.table is not None and a.table == b.table:
        return True
    return bool(a.section) and a.section == b.section


def _own_words(f: dict | None) -> bool:
    """Whether a facet is the value's own words in its own paragraph flow: its own tier, or read from the sentence or paragraph
    just above it (round 14 U2, KTD3)."""
    f = f or {}
    return f.get("tier") == TIER_OWN or f.get("part") in SENTENCE_PARTS


def relation(a, b, a_in_unit: int = 1, b_in_unit: int = 1) -> str:
    """The one pairwise decision (R3, KTD3): are ``a`` and ``b`` one datum (``same``: compared, and a conflict when their amounts
    differ and their periods overlap), parallel components (``different``: never a conflict) or not to be established
    (``undecided``)? First match wins:

    0. both have the same non-empty source span (round 14 U1, KTD1): one number in one place of one reading, however many
       times it was registered, so it is one datum whatever the two facets read (a wider quote can read a facet the first
       registration missed) — before every facet rule, and so for the coverage, recency and timeline readers that call this;
    1. both facets are ``own`` tier and their cores are equal and not empty: same;
    2. both are the unit's headline datum (``headline``): same;
    2b. a family that needs no facet (rent, value, area, income ...), the two data in different appraisal units, one stating a
       facet and the other none: same. A stated facet there is a wording of the source, not a component, so the value that states
       none (the headline datum of its unit) is compared with the other unit's value of the key; two own facets with disjoint
       cores across units stay undecided (rule 4). Inside one unit this rule does not apply;
    3. each core keeps a content word the other lacks, and both are in one unit's same table, list or section: different, whatever
       the tier (a facet read from the sentence or paragraph above the value counts there like the value's own words); or both
       facets are the value's own words — ``own`` tier (its own row label, column header or purpose phrase), or read from an earlier
       sentence or paragraph of its paragraph flow (``SENTENCE_PARTS``, round 14 U2) — at least one of them a true ``own`` facet, and
       both data are in the same appraisal unit, in any section: different (round 13 follow-up: an own facet is the value's own
       words, so two disjoint ones inside one appraisal are two components even where the sections differ; a reduction that
       takes its consideration from the sentence above, beside one that names its own, is the reported shape). Across sections two
       facets that are *both* borrowed from the text above decide nothing (review, round 14): a restated adjustment worded
       differently in two sections must not lose its conflict, so the pair is undecided;
    4. everything else (a subset, disjoint across units, context-tier cores that are not side by side, equal context-tier
       cores, an unresolved facet, a stated facet against a headline datum in a facet family or in one unit): undecided.

    "Different" needs structural evidence: two wordings of one datum in two documents can share no words. The rule is not
    transitive, so it is no key field: ``timeline`` joins "same" pairs into classes. The two data are of one base key
    (``timeline`` buckets them): ``in_unit`` is the number of data of that key in each one's appraisal unit."""
    if (span := getattr(a, "span", None)) and span == getattr(b, "span", None):
        return SAME
    ca, cb = core_of(a), core_of(b)
    a_own, b_own = (a.facet or {}).get("tier") == TIER_OWN, (b.facet or {}).get("tier") == TIER_OWN
    own = a_own and b_own
    flow = _own_words(a.facet) and _own_words(b.facet)
    if own and ca and ca == cb:
        return SAME
    if headline(a, a_in_unit) and headline(b, b_in_unit):
        return SAME
    if not needs_facet(a.family) and not needs_facet(b.family) and a.appraisal != b.appraisal and not (ca and cb):
        return SAME
    across = flow and (a_own or b_own) and a.appraisal is not None and a.appraisal == b.appraisal
    if ca - cb and cb - ca and (_together(a, b) or across):
        return DIFFERENT
    return UNDECIDED


# --- a coefficient and its reduction (round 14 U4, KTD6) -------------------------------------------------------------------

RECONCILED, CONFLICT, UNRELATED = "reconciled", "conflict", "unrelated"

# the words of a reduction, with the prefix letters Hebrew writes before a noun and the construct and plural forms: it is the
# source's own cue that a proportion is the complement of a coefficient (a rate or a share is not)
_REDUCTION_CUE = re.compile(r"(?<![א-ת])[ובלהשכמ]{0,2}(?:הפחת(?:ה|ת|ות)|הנח(?:ה|ת|ות)|ניכוי|ניכויי)(?![א-ת])")
_NUMBER_WRITTEN = re.compile(r"\d[\d,]*(?:\.(\d+))?")
_REDUCTION_STEMS = _stems("הפחתה הפחתת הפחתות הנחה הנחת הנחות ניכוי ניכויי")  # the cue words, which name no consideration


def reduction_cue(text: str | None) -> bool:
    """Whether a text holds a word of a reduction (הפחתה, הנחה, ניכוי)."""
    return bool(_REDUCTION_CUE.search(_flat(text or "")))


def _half_unit(site) -> Decimal:
    """Half a unit of the last digit a datum's number is written with, as a fraction: "15" of a percent is 0.005, "0.15" of a ratio
    0.005, "8.5" of a percent 0.0005."""
    found = _NUMBER_WRITTEN.search(getattr(site, "written", "") or "")
    places = len(found.group(1) or "") if found else 0
    half = Decimal(5) / (Decimal(10) ** (places + 1))
    return half / 100 if getattr(site, "unit", "") == "percent" else half


def reconciles(coefficient, proportion) -> str:
    """Whether a coefficient and a proportion are one adjustment, written twice (round 14 U4, KTD6, R8, AE6): the coefficient is one
    minus the reduction ("0.85" and "15%", "0.83" and "17%"). Pure, over two data of different buckets (the metric family leads
    the base key, so ``relation`` never compares them). The data read as ``relation`` reads them, with ``amount`` (the canonical
    amount), ``written`` and ``unit`` (the number as the source writes it) and ``words`` (the source's words around the
    proportion's number):

    - ``reconciled``: the same appraisal unit, equal stated facet cores, a reduction cue (``reduction_cue``) in the proportion's own
      words or facet, and the coefficient equal to 1 − the proportion to within half a unit of the last digit the proportion is
      written with (the way ``calc.display_matches`` checks a derived number against what the answer shows), the half unit itself
      excluded: 0.9 is not 1 − 15%, 0.85 only rounds up to it;
    - ``conflict``: the same unit, equal cores and the cue, with amounts that do not reconcile;
    - ``unrelated``: anything else — two appraisal units (whatever the amounts), different or unstated cores, no cue, a range or a
      missing amount, or the families in any other order. A proportion with no reduction cue is a rate of its own, never the
      complement of a coefficient."""
    if getattr(coefficient, "family", "") != COEFFICIENT or getattr(proportion, "family", "") != PROPORTION:
        return UNRELATED
    if coefficient.appraisal is None or coefficient.appraisal != proportion.appraisal:
        return UNRELATED
    cores = core_of(coefficient) - _REDUCTION_STEMS
    if not cores or cores != core_of(proportion) - _REDUCTION_STEMS:
        return UNRELATED
    if not (reduction_cue(getattr(proportion, "words", "")) or reduction_cue((proportion.facet or {}).get("written"))):
        return UNRELATED
    c, p = getattr(coefficient, "amount", None), getattr(proportion, "amount", None)
    if not isinstance(c, Decimal) or not isinstance(p, Decimal):
        return UNRELATED
    return RECONCILED if abs(c - (1 - p)) < _half_unit(proportion) else CONFLICT
