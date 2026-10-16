"""The facet of a datum: the component, consideration or cash flow its source states (round 13 U3; R1, R2; KTD1-KTD3).

A coefficient or a rate means little without what it is for: two coefficients of one table, each for another
consideration, are parallel components of one appraisal, not two values of one datum. The facet is therefore a stored,
anchored record of the source's own words:

- ``written``: the words as the source wrote them, without a leading preposition (ל / עבור / בגין);
- ``part``: where they are (``row``, ``column``, ``caption``, ``sentence``, ``lead_in``, ``heading``; round 14 U2, KTD3: also
  ``preceding_sentence``, ``paragraph`` and ``table_note``);
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

# where a context facet was read from (``part``)
PART_PRECEDING_SENTENCE, PART_PARAGRAPH, PART_HEADING, PART_LEAD_IN = "preceding_sentence", "paragraph", "heading", "lead_in"
PART_TABLE_NOTE, PART_CAPTION, PART_LIST_ITEM = "table_note", "caption", "list_item"
# the parts that are sentence-level evidence in the value's own paragraph flow: ``relation`` rule 3 counts them like an own facet
SENTENCE_PARTS = frozenset({PART_PRECEDING_SENTENCE, PART_PARAGRAPH})

# the families whose datum needs a facet (KTD3): a coefficient, and a proportion (a rate, a percentage, an
# adjustment). In any other family an empty or absent facet is the appraisal unit's headline datum
FACET_FAMILIES = frozenset({"coefficient", "proportion"})

LABEL_NOT_STATED = "לא צוין במקור"
LABEL_NOT_READ = "לא נקרא"
LABEL_FROM_CONTEXT = "מההקשר"
LABEL_FACET = "רכיב"
LABEL_UNPARSED = {
    REASON_INTERVENING: "נקרא ולא הוכרע: ערך אחר מאותו סוג עומד בין ההקשר לערך",
    REASON_TWO_COMPONENTS: "נקרא ולא הוכרע: ההקשר מונה שני רכיבים",
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


def none(reason: str = REASON_NOT_IN_RANGE) -> dict:
    """Read, and the source states no facet (the unit's headline datum, or a datum that cannot be placed): nothing in the range
    checked names one (``not_in_range``), or its family needs none (``not_needed``)."""
    return {"status": STATUS_NONE, "written": None, "part": None, "tier": None, "reason": reason}


def not_read(reason: str | None = None) -> dict:
    """Not read: the place the facet is read from was not at hand (no ``reason``), a picture cell, or the search reached its
    cap. It is never shown as "the source states none"."""
    return {"status": STATUS_NOT_READ, "written": None, "part": None, "tier": None, "reason": reason}


def unparsed(reason: str, part: str | None = None, **place) -> dict:
    """Read, and ambiguous (round 14 U2, KTD3, KTD4): the nearest text that names a component cannot be attributed to this value
    (``intervening_value``, ``two_components``). No component is stated, so ``core_of`` is empty; ``part`` and the place say
    where the rejected text is."""
    return {"status": STATUS_UNPARSED, "written": None, "part": part, "tier": TIER_CONTEXT, "reason": reason} | {
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
    "coefficient": _BASE_WORDS | _COEFFICIENT_WORDS | _PROPORTION_WORDS,
    "proportion": _BASE_WORDS | _COEFFICIENT_WORDS | _PROPORTION_WORDS,
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
        return "coefficient"
    if stems & _PROPORTION_WORDS:
        return "proportion"
    return family


# --- table cells ---------------------------------------------------------------------------------------------------------

def read_cell(row_label: str | None, header: str | None, caption: str | None = None, title: Sequence[str] = (),
              *, family: str = "", matrix: bool = False, notes: Sequence[str] = ()) -> dict:
    """The facet of a table cell (R1): its row label is the own facet; the column header names it when the row label
    states nothing (a generic label such as "הערך שנקבע"); a (row label, column header) pair when both name things in a
    matrix (more than one value column). The caption and the table's title are the context facet, read only in a family
    that needs a facet and only when they hold a metric word and name a component beside it. Last, in the same family, the
    table's notes (round 14 U2, KTD3): a note that names the row label and states a purpose phrase for it
    (``table_note``). Nothing stated: ``none``."""
    row, col = (row_label or "").strip(), (header or "").strip()
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
    ``read_sentence`` for the rules."""
    tokens = sentence.split()
    bare = [_bare(t) for t in tokens]
    at = next((i for i, t in enumerate(tokens) if _number_token(t, written)), None)
    metric = next((i for i, t in enumerate(bare) if _flat(t) not in _NOT_A_CUE and _stem(_flat(t)) in _CUE_WORDS
                   and (at is None or i < at)), None)
    marks = [i for i in (at, metric) if i is not None]
    j = min(marks) + 1 if marks else 0
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
                return phrase, first, tokens
        if tokens[j][-1:] in ",;:" and (at is None or j < at):
            return None  # the clause ended before the number: a phrase after it is another clause's
        j += 1
    return None


def read_sentence(sentence: str, written: str, family: str = "") -> dict | None:
    """The purpose phrase of the value's own sentence as an own facet, or None: the words after ל / עבור / בגין / בעבור
    that follow the number or the metric word, to the clause end (a comma, a semicolon, a colon, a full stop, another
    number or a conjoined "ול…" purpose). A phrase whose head word is a period, a unit, a date or a basis term (לשנה,
    למ״ר, למועד, לפי …) is not a facet, and a phrase of only generic words names no component."""
    found = _find_phrase(sentence, written, family)
    return facet(found[0], "sentence", TIER_OWN) if found is not None else None


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
    ``sentence`` its index among the block's sentences (``professional.sections.sentences``)."""

    text: str
    part: str
    block: int | None = None
    sentence: int | None = None


class Reach(NamedTuple):
    """What a bounded read of the text above a value covered, nearest first (``reach``): the candidates, whether the character
    cap cut the search short (a search that ends so with no accepted candidate is not read, never "none"), and the text of the
    blocks the candidates are in (their places are located in them)."""

    candidates: list
    capped: bool = False
    blocks: dict = {}  # noqa: RUF012 - never mutated: an immutable NamedTuple default


def _as_candidate(item) -> Candidate:
    """A ``Candidate``, or a legacy ``(kind, text)`` pair of a block above the value (round 13)."""
    if isinstance(item, Candidate):
        return item
    kind, text = item
    if kind == "heading":
        return Candidate(text, PART_HEADING)
    return Candidate(text, PART_LEAD_IN if (text or "").rstrip().endswith(":") else PART_PARAGRAPH)


def _accepted(c: Candidate, family: str) -> tuple[dict | None, bool]:
    """What a candidate states: ``(a stated facet, whether it names two components)``; ``(None, False)``: it states none."""
    if c.part == PART_LIST_ITEM:
        return None, False  # a sibling of the list: it holds numbers that stand between, never the component
    place = {"block": c.block, "sentence": c.sentence}
    if c.part in (PART_HEADING, PART_LEAD_IN):
        found = _context(c.text, family, c.part, **place)
        if found is None:
            return None, False
        phrase = _find_phrase(_unnumbered(c.text), "", family)
        return found, bool(phrase is not None and _second_purpose(phrase[2], phrase[1]))
    phrase, two = _candidate_phrase(c.text, family)
    return (facet(phrase, c.part, TIER_CONTEXT, **place) if phrase is not None else None), two


def read_context(preceding: Reach | Sequence, family: str = "") -> dict:
    """The bounded context read of a quote value whose sentence states no facet (round 14 U2, KTD3, KTD4; R1, R2): the
    candidates above it, nearest first (a ``Reach``, or a plain sequence of ``Candidate`` / legacy ``(kind, text)`` pairs).

    The first candidate that holds a component phrase decides: it is the value's facet (``stated``, tier context) unless
    - it names two components: ``unparsed``, reason ``two_components``;
    - a number of the same kind stands between it and the value, or the candidate states one itself (the phrase is that
      value's): ``unparsed``, reason ``intervening_value``. A list lead-in is not blocked by the numbers of its own list items:
      the items are peers, the lead-in is theirs all.
    A candidate with no phrase is passed over (its numbers count as standing between). Nothing accepted: ``none`` (reason
    ``not_in_range``), or ``not_read`` (reason ``cap``) when the character cap ended the search before its range did."""
    reach_ = preceding if isinstance(preceding, Reach) else Reach(list(preceding))
    seen_text = seen_item = False
    for item in reach_.candidates:
        c = _as_candidate(item)
        found, two = _accepted(c, family)
        has_number = family_number(c.text)
        if found is not None:
            place = {"block": c.block, "sentence": c.sentence}
            if two:
                return unparsed(REASON_TWO_COMPONENTS, c.part, **place)
            if seen_text or has_number or (seen_item and c.part != PART_LEAD_IN):
                return unparsed(REASON_INTERVENING, c.part, **place)
            return found
        if has_number:
            seen_item = seen_item or c.part == PART_LIST_ITEM
            seen_text = seen_text or c.part != PART_LIST_ITEM
    return not_read(REASON_CAP) if reach_.capped else none()


def read_quote(sentence: str, written: str, family: str = "", preceding: Reach | Sequence | None = None) -> dict:
    """The facet of a value quoted from a text: the purpose phrase of its sentence (own); else, in a family that needs a
    facet, what the candidates above it state (``read_context``: context); else ``none``. ``preceding`` None: the text above was
    not at hand, so a family that needs a facet is ``not_read`` rather than ``none``."""
    if (own := read_sentence(sentence, written, family)) is not None:
        return own
    if not needs_facet(family):
        return none(REASON_NOT_NEEDED)
    if preceding is None:
        return not_read()
    return read_context(preceding, family)


def _own_range(sentences: Sequence[str], text: str, quote: str) -> int:
    """The index of the first sentence of a block that the value's quote overlaps; -1 when the quote is not found in the block
    (then no sentence of the block is offered as a candidate)."""
    flat = " ".join(_flat(text).split())
    wanted = " ".join(_flat(quote).split())
    start = flat.find(wanted) if wanted else -1
    if start < 0:
        return -1
    end, pos = start + len(wanted), 0
    for i, sentence in enumerate(sentences):
        one = " ".join(_flat(sentence).split())
        at = flat.find(one, pos)
        if at < 0:
            continue
        pos = at + len(one)
        if at < end and pos > start:
            return i
    return -1


def reach(rows: Sequence, at: int, quote: str = "", *, path: Sequence[str] | None = None, lo: int = 0,
          chars: int = 6000, truncated: bool = False) -> Reach:
    """The candidates above a quote value, nearest first (round 14 U2, KTD3; R1, R2), from ``rows``: blocks with
    ``block_index``, ``kind``, ``text`` and ``section_path``, holding the block ``at`` that has the value and the blocks above it.

    1. the earlier sentences of the holder block (the value's own sentence, and what follows it, excluded);
    2. the earlier paragraph blocks of the same section, a sentence at a time, nearest first; a paragraph that ends in a colon is
       a ``lead_in`` as a whole, and a list item is kept only for the numbers it holds;
    3. the heading of the section, which ends the read.
    The read stops at a table block, at a block of another section (its heading or its text) and at ``lo``, the first block of
    the appraisal unit. ``chars`` caps the text of the candidates: the one that crosses it is dropped, the search ends there and
    ``capped`` is set. ``truncated``: ``rows`` were cut by the reader's row limit, so a block of the range that is missing from
    them means the search did not reach its edge (``capped``); otherwise a missing block simply ends the range."""
    from app.professional.sections import sentences as split

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
    """The sentence of a text that holds a number as written (the whole text when none does)."""
    for part in re.split(r"(?<=[.!?])\s+|\n", text or ""):
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
    is the only value of its base key in its unit (``in_unit``: how many values of that key the unit holds)."""
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
    just above it (round 14 U2, KTD3) — sentence-level evidence, which a section edge between two values does not weaken."""
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
    3. each core keeps a content word the other lacks, and both are in one unit's same table, list or section: different;
       or both facets are the value's own words — ``own`` tier (the value's own row label, column header or purpose phrase), or
       read from an earlier sentence or paragraph of the value's own paragraph flow (``SENTENCE_PARTS``, round 14 U2) — and both
       data are in the same appraisal unit, in any section: different (round 13 follow-up: an own facet is the value's own words,
       so two disjoint ones inside one appraisal are two components even where the sections differ);
    4. everything else (a subset, disjoint across units, context-tier cores that are not side by side, equal context-tier
       cores, an unresolved facet, a stated facet against a headline datum in a facet family or in one unit): undecided.

    "Different" needs structural evidence: two wordings of one datum in two documents can share no words. The rule is not
    transitive, so it is no key field: ``timeline`` joins "same" pairs into classes. The two data are of one base key
    (``timeline`` buckets them): ``in_unit`` is the number of values of that key in each one's appraisal unit."""
    if (span := getattr(a, "span", None)) and span == getattr(b, "span", None):
        return SAME
    ca, cb = core_of(a), core_of(b)
    own = (a.facet or {}).get("tier") == TIER_OWN and (b.facet or {}).get("tier") == TIER_OWN
    flow = _own_words(a.facet) and _own_words(b.facet)
    if own and ca and ca == cb:
        return SAME
    if headline(a, a_in_unit) and headline(b, b_in_unit):
        return SAME
    if not needs_facet(a.family) and not needs_facet(b.family) and a.appraisal != b.appraisal and not (ca and cb):
        return SAME
    if ca - cb and cb - ca and (_together(a, b) or (flow and a.appraisal is not None and a.appraisal == b.appraisal)):
        return DIFFERENT
    return UNDECIDED
