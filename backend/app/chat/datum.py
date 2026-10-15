"""The facet of a datum: the component, consideration or cash flow its source states (round 13 U3; R1, R2; KTD1-KTD3).

A coefficient or a rate means little without what it is for: two coefficients of one table, each for another
consideration, are parallel components of one appraisal, not two values of one datum. The facet is therefore a stored,
anchored record of the source's own words:

- ``written``: the words as the source wrote them, without a leading preposition (ל / עבור / בגין);
- ``part``: where they are (``row``, ``column``, ``caption``, ``sentence``, ``lead_in`` or ``heading``);
- ``tier``: ``own`` (the value's own row label, its column header in a matrix, the purpose phrase of its own sentence)
  or ``context`` (the table caption, the list lead-in, the nearest heading);
- ``status``: ``stated``, ``none`` (read, nothing stated) or ``not_read``.

It is source text only: what the model wrote (``Value.label``, ``Value.subject``) never reaches this module. The
reading is a pure function of stored text (no model call, no database): ``tools`` hands it the cell's row, header and
caption, or the value's sentence with the blocks above it, once, when a datum enters the turn (KTD2).

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
TIER_OWN, TIER_CONTEXT = "own", "context"
PARTS = ("row", "column", "caption", "sentence", "lead_in", "heading")
CONTEXT_BLOCKS = 3  # the blocks above a quote value that the bounded context read looks at (KTD2)

# the families whose datum needs a facet (KTD3): a coefficient, and a proportion (a rate, a percentage, an
# adjustment). In any other family an empty or absent facet is the appraisal unit's headline datum
FACET_FAMILIES = frozenset({"coefficient", "proportion"})

LABEL_NOT_STATED = "לא צוין במקור"
LABEL_NOT_READ = "לא נקרא"
LABEL_FROM_CONTEXT = "מההקשר"
LABEL_FACET = "רכיב"


def facet(written: str, part: str, tier: str) -> dict:
    """A stated facet."""
    return {"status": STATUS_STATED, "written": written, "part": part, "tier": tier}


def none() -> dict:
    """Read, and the source states no facet (the unit's headline datum, or a datum that cannot be placed)."""
    return {"status": STATUS_NONE, "written": None, "part": None, "tier": None}


def not_read() -> dict:
    """Not read: the place the facet is read from was not at hand."""
    return {"status": STATUS_NOT_READ, "written": None, "part": None, "tier": None}


def family_of(kind: str) -> str:
    """The datum family of a kind, as ``resolve.family`` has it (a ratio and a rate are one proportion; value and
    value per area are one family), without importing ``resolve`` (which imports the tools)."""
    base = (kind or "")[: -len("_per_area")] if (kind or "").endswith("_per_area") else (kind or "")
    return {"ratio": "proportion", "rate": "proportion"}.get(base, base)


def needs_facet(family: str) -> bool:
    return family in FACET_FAMILIES


def line(f: dict | None) -> str:
    """The facet as the model is told it (short, Hebrew): its written words, or that the source states none."""
    f = f or {}
    if f.get("status") == STATUS_STATED:
        where = f" ({LABEL_FROM_CONTEXT})" if f.get("tier") == TIER_CONTEXT else ""
        return f"{LABEL_FACET}{where}: «{f.get('written')}»"
    if f.get("status") == STATUS_NONE:
        return f"{LABEL_FACET}: {LABEL_NOT_STATED}"
    return f"{LABEL_FACET}: {LABEL_NOT_READ}"


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
# a row or column label that is no component by itself: the generic words of a label column
_LABEL_WORDS = _BASE_WORDS


def generic_words(family: str) -> frozenset[str]:
    """The stems dropped from a facet of a family: its metric and generic words, and the base words of every family."""
    return FAMILY_WORDS.get(family, _BASE_WORDS)


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
              *, family: str = "", matrix: bool = False) -> dict:
    """The facet of a table cell (R1): its row label is the own facet; the column header names it when the row label
    states nothing (a generic label such as "הערך שנקבע"); a (row label, column header) pair when both name things in a
    matrix (more than one value column). The caption and the table's title are the context facet, read only in a family
    that needs a facet and only when they hold a metric word and name a component beside it. Nothing stated: ``none``."""
    row, col = (row_label or "").strip(), (header or "").strip()
    if row and _states(row, family):
        if matrix and col and _states(col, family):
            return facet(f"{row} — {col}", "row", TIER_OWN)
        return facet(row, "row", TIER_OWN)
    if col and _states(col, family):
        return facet(col, "column", TIER_OWN)
    if needs_facet(family):
        for text in (caption or "", *title):
            if (found := _context(text, family, "caption")) is not None:
                return found
    return none()


def _context(text: str, family: str, part: str) -> dict | None:
    """A caption, a lead-in or a heading as a context facet, or None: it must hold a metric word of the family (the cue
    that it speaks of the coefficient or rate) and name a component beside it."""
    cleaned = re.sub(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[א-ת][.)])\s+", "", (text or "").strip()).rstrip(" :").strip()
    if not cleaned or not (_stems(cleaned) & _CUE_WORDS) or not _states(cleaned, family):
        return None
    return facet(cleaned, part, TIER_CONTEXT)


# --- quote values ----------------------------------------------------------------------------------------------------------

_CONNECTORS = {"עבור", "בגין", "בעבור"}
# the head words of a phrase after ל that are a period, a unit, a date, a basis or a pointer, never a component
_NOT_A_COMPONENT = _stems(
    "שנה שנים שנתי שנתית חודש חודשים חודשי רבעון יום ימים תקופה מר מטר דונם יחידה תאריך מועד פי כל ללא כולל עד אחר "
    "עומת מעט רבות הלן עיל משך פני מעמ בסיס בשנה צורך")
_CLAUSE_END = ",;:.)"
_STOP_WORDS = frozenset({"הוא", "היא", "הינו", "הינה", "עומד", "עומדת", "נקבע", "נקבעה", "אומץ", "אומצה"})
_PHRASE_MAX = 8


def _bare(token: str) -> str:
    return token.strip(" ()[]\"'״׳,;:.!?")


def _number_token(token: str, written: str) -> bool:
    return bool(written) and re.search(rf"(?<![\d.,]){re.escape(written)}(?!\d|[.,]\d)", token) is not None


def _excluded_head(word: str) -> bool:
    """Whether a word that opens a purpose phrase is a period, unit, date, basis or pointer term (or a number)."""
    flat = _flat(word)
    return not flat or flat[0].isdigit() or _stem(flat) in _NOT_A_COMPONENT or flat in _NOT_A_COMPONENT


def read_sentence(sentence: str, written: str, family: str = "") -> dict | None:
    """The purpose phrase of the value's own sentence as an own facet, or None: the words after ל / עבור / בגין / בעבור
    that follow the number or the metric word, to the clause end (a comma, a semicolon, a colon, a full stop, another
    number or a conjoined "ול…" purpose). A phrase whose head word is a period, a unit, a date or a basis term (לשנה,
    למ״ר, למועד, לפי …) is not a facet, and a phrase of only generic words names no component."""
    tokens = sentence.split()
    bare = [_bare(t) for t in tokens]
    at = next((i for i, t in enumerate(tokens) if _number_token(t, written)), None)
    metric = next((i for i, t in enumerate(bare) if _stem(_flat(t)) in _CUE_WORDS and (at is None or i < at)), None)
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
            phrase = " ".join(w for w in words if w)
            if _states(phrase, family):
                return facet(phrase, "sentence", TIER_OWN)
        if tokens[j][-1:] in ",;:" and (at is None or j < at):
            return None  # the clause ended before the number: a phrase after it is another clause's
        j += 1
    return None


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


def read_context(preceding: Sequence[tuple[str, str]], family: str = "") -> dict | None:
    """The bounded context read of a quote value whose sentence states no facet (KTD2): the nearest list lead-in (a
    paragraph ending in a colon) or heading among the blocks above it, ``(kind, text)`` nearest first, as a context
    facet. The nearest heading ends the read; a lead-in that names no component is passed over."""
    for kind, text in list(preceding)[:CONTEXT_BLOCKS]:
        if kind == "heading":
            return _context(text, family, "heading")
        if (text or "").rstrip().endswith(":") and (found := _context(text, family, "lead_in")) is not None:
            return found
    return None


def read_quote(sentence: str, written: str, family: str = "", preceding: Sequence[tuple[str, str]] | None = None) -> dict:
    """The facet of a value quoted from a text: the purpose phrase of its sentence (own); else, in a family that needs a
    facet, the lead-in or heading above it (context); else ``none``. ``preceding`` None: the blocks above were not at
    hand, so a family that needs a facet is ``not_read`` rather than ``none``."""
    if (own := read_sentence(sentence, written, family)) is not None:
        return own
    if needs_facet(family):
        if preceding is None:
            return not_read()
        if (found := read_context(preceding, family)) is not None:
            return found
    return none()


def preceding(rows: Sequence, at: int, path: Sequence[str] | None = None, limit: int = CONTEXT_BLOCKS) -> list[tuple[str, str]]:
    """The ``(kind, text)`` of the heading and paragraph blocks among the ``limit`` blocks above block ``at``, nearest
    first, inside the section of that block (the same section path: the read stops at the section's edge).
    ``rows``: blocks with ``block_index``, ``kind``, ``text`` and ``section_path``."""
    by = {r.block_index: r for r in rows}
    here = tuple(path) if path is not None else tuple(getattr(by.get(at), "section_path", None) or ())
    out: list[tuple[str, str]] = []
    for k in range(at - 1, at - limit - 1, -1):
        r = by.get(k)
        if r is None or tuple(r.section_path or ()) != here:
            break
        if r.kind in ("heading", "paragraph") and (r.text or "").strip():
            out.append((r.kind, r.text.strip()))
    return out


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


# --- the decision (round 13 U4, KTD3) -----------------------------------------------------------------------------------

SAME, DIFFERENT, UNDECIDED = "same", "different", "undecided"


class Site(NamedTuple):
    """What ``relation`` reads of a datum: ``timeline.Datum`` has the same attributes. ``appraisal``: the appraisal unit it is in
    ((version, unit number)); ``table``: (version, table index) of its cell, or None; ``section``: the section path of its
    block."""

    family: str
    facet: dict | None
    appraisal: tuple | None = None
    table: tuple | None = None
    section: str = ""


@lru_cache(maxsize=4096)
def _core(written: str, family: str) -> frozenset[str]:
    return core(written, family)


def core_of(site) -> frozenset[str]:
    """The core of a datum's stated facet; empty when it states none (a facet of the status ``none`` or ``not_read``, or one
    that reduces to generic words only)."""
    f = site.facet or {}
    if f.get("status") != STATUS_STATED:
        return frozenset()
    return _core(f.get("written") or "", site.family)


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


def relation(a, b, a_in_unit: int = 1, b_in_unit: int = 1) -> str:
    """The one pairwise decision (R3, KTD3): are ``a`` and ``b`` one datum (``same``: compared, and a conflict when their amounts
    differ and their periods overlap), parallel components (``different``: never a conflict) or not to be established
    (``undecided``)? First match wins:

    1. both facets are ``own`` tier and their cores are equal and not empty: same;
    2. both are the unit's headline datum (``headline``): same;
    3. each core keeps a content word the other lacks, and both are in one unit's same table, list or section: different;
    4. everything else (a subset, disjoint across units, equal context-tier cores, an unresolved facet, a stated facet against
       a headline datum): undecided.

    "Different" needs structural evidence: two wordings of one datum in two documents can share no words. The rule is not
    transitive, so it is no key field: ``timeline`` joins "same" pairs into classes. The two data are of one base key
    (``timeline`` buckets them): ``in_unit`` is the number of values of that key in each one's appraisal unit."""
    ca, cb = core_of(a), core_of(b)
    own = (a.facet or {}).get("tier") == TIER_OWN and (b.facet or {}).get("tier") == TIER_OWN
    if own and ca and ca == cb:
        return SAME
    if headline(a, a_in_unit) and headline(b, b_in_unit):
        return SAME
    if ca - cb and cb - ca and _together(a, b):
        return DIFFERENT
    return UNDECIDED
