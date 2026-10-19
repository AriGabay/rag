"""Condition completeness by meaning: the closed-class operator reader (round 18 U5; KTD1, KTD8; R1-R4).

An item's condition is what its source says limits the value: a negation, "only", an exception, a subjection, a threshold, and
the status, method, component or time a heading or a note names. The answer sentence that gives the value must carry it. This
module reads a condition and a sentence with a tokenizer that KEEPS the operators — ``normalize_text.STOPWORDS`` drops לא, רק, עד,
אם and אין, so ``evidence.claim_terms`` cannot tell "excluding" from "including" — and decides what it can:

- ``KEPT``: the sentence has the same operator with the same polarity over most of the condition's operand (the words and numbers
  that follow the operator in the source up to the next operator), or, for a condition with no operator (a heading, a row note), most
  of its content words;
- ``HINT``: the sentence has the opposite polarity over most of the same operand ("not including X" in the source, "including X" in
  the answer; a negation in the source and none over the same words in the answer). It is only a hint: the requirements call
  confirms it with a quote, and an unconfirmed hint leaves the value as it was;
- ``UNDECIDED``: everything else — another operator for the same idea ("excluding" and "without"), an operand match below most, a
  sentence that holds both polarities over the operand. The requirements call decides these.

The classes are closed (the lists below) and no other phrase list is read. A plain "כולל" (including) is an operator for the
sentence side — it is what "not including" is contradicted by — but never a condition of a source by itself: "שטח כולל" is a total."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NamedTuple

from app.answering.verify import numbers_in
from app.chat.evidence import claim_terms
from app.chat.verify import _MISSING_TOKEN, _QUALIFIER_PATTERN, _missing_forms, qualifier_words
from app.extraction.normalize_text import is_negation
from app.platform import dates as shared_dates

KEPT, HINT, UNDECIDED = "kept", "hint", "undecided"

# the operator classes (closed): the key of an operator is what two spellings of it share, its polarity the side it stands on
NEGATION, EXCLUSIVITY, EXCEPTION, SUBJECTION, THRESHOLD = "negation", "exclusivity", "exception", "subjection", "threshold"
_EXCLUSIVE = frozenset("רק בלבד אלא".split())
_EXCEPT_OUT = frozenset("למעט מלבד פרט להוציא בהוצאת".split())
_EXCEPT_IN = frozenset("כולל כוללת כוללים כוללות לרבות".split())
_SUBJECT = frozenset("בכפוף כפוף כפופה כפופים כפופות בתנאי מותנה מותנית מותנים מותנות אם".split())
_THRESHOLD = {"מעל": "מעל", "יותר": "מעל", "מתחת": "מתחת", "פחות": "מתחת", "עד": "עד", "היותר": "עד", "מקסימום": "עד",
              "לפחות": "לפחות", "מינימום": "לפחות"}
# a heading or a note that names a method or a component of the valuation limits the values under it
_METHOD_HEADS = frozenset("גישת שיטת רכיב מרכיב".split())
_JOINERS = ("ו", "ש", "וש")  # a conjunction or relative prefix an operator takes ("ושלא", "וכולל")
_CLAUSE_END = re.compile(r"[;|\n]|(?<!\d)[.:!?](?!\d)|\s[—–]\s")


class Op(NamedTuple):
    """One operator of a text: ``cls`` its class, ``key`` what its spellings share (``cls`` for negation, exclusivity and
    subjection; ``include`` for the exception class, whose polarity is the side: excluding or including; the word for a threshold),
    ``negative`` its polarity (a negation, an exclusion, "not more than"), ``at`` its token index in the clause and ``width`` the
    tokens it takes (two for a negation folded into the word after it)."""

    cls: str
    key: str
    negative: bool
    at: int
    width: int = 1

    @property
    def material(self) -> bool:
        """Whether a source's condition: a plain "including" is a total or a label, not a limit."""
        return not (self.cls == EXCEPTION and not self.negative)


class Operand(NamedTuple):
    """What an operator applies to: the content words (each token with its variants, ``claim_terms``), the numbers and the dates
    (ISO) of the tokens that follow it, up to the next operator."""

    groups: tuple[frozenset[str], ...] = ()
    numbers: frozenset[str] = frozenset()
    dates: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.groups or self.numbers or self.dates)


class Segment(NamedTuple):
    op: Op
    operand: Operand
    clause: int


def _bare(token: str) -> str:
    return re.sub(r"[\"'״׳]", "", token)


def _word_op(token: str) -> tuple[str, str, bool] | None:
    """(class, key, negative) of one operator word, with a conjunction or relative prefix taken off; None for any other word."""
    word = _bare(token)
    for prefix in ("", *_JOINERS):
        if prefix and not word.startswith(prefix):
            continue
        w = word[len(prefix):]
        if w in _EXCLUSIVE:
            return EXCLUSIVITY, "only", False
        if w in _EXCEPT_OUT:
            return EXCEPTION, "include", True
        if w in _EXCEPT_IN:
            return EXCEPTION, "include", False
        if w in _SUBJECT:
            return SUBJECTION, "subject", False
        if w in _THRESHOLD:
            return THRESHOLD, _THRESHOLD[w], False
    return (NEGATION, "negation", True) if is_negation(word) else None


def _tokens(clause: str) -> list[str]:
    return _MISSING_TOKEN.findall(clause)


def _ops(tokens: list[str]) -> list[Op]:
    """The operators of a clause's tokens. A negation right before an including word or a threshold folds into it ("לא כולל" is
    an exclusion, "לא יותר" the negative side of a threshold) and is not an operator of its own."""
    out: list[Op] = []
    at = 0
    while at < len(tokens):
        found = _word_op(tokens[at])
        if found is None:
            at += 1
            continue
        cls, key, negative = found
        follows = _word_op(tokens[at + 1]) if at + 1 < len(tokens) else None
        if cls == NEGATION and follows is not None and follows[0] in (EXCEPTION, THRESHOLD):
            cls, key, negative = follows[0], follows[1], not follows[2]
            out.append(Op(cls, key, negative, at, 2))
            at += 2
            continue
        out.append(Op(cls, key, negative, at))
        at += 1
    return out


def clauses(text: str) -> list[str]:
    """The clauses of a text: split at a sentence end, a semicolon, a colon, a cell edge or a line, never at a comma (a list of
    exceptions is one clause) or inside a number."""
    return [c.strip() for c in _CLAUSE_END.split(text or "") if c and c.strip()]


def operators(text: str) -> list[Op]:
    """Every operator of a text, clause by clause (``Op.at`` indexes its clause's tokens)."""
    return [op for c in clauses(text) for op in _ops(_tokens(c))]


def _operand(tokens: Iterable[str]) -> Operand:
    kept = [t for t in tokens if _word_op(t) is None]
    text = " ".join(kept)
    groups = tuple(g for t in kept if not t[0].isdigit() and (g := claim_terms(t)[1]))
    dates = tuple(p.iso for p in shared_dates.find_all(text))
    return Operand(groups, frozenset(numbers_in(text)), dates)


def segments(text: str) -> list[Segment]:
    """The operators of a text with their operands: the tokens after an operator up to the next one, else (an operator that ends
    its clause, "בלבד") the tokens before it back to the previous one."""
    out: list[Segment] = []
    for n, clause in enumerate(clauses(text)):
        tokens = _tokens(clause)
        marks = _ops(tokens)
        for i, op in enumerate(marks):
            after_end = marks[i + 1].at if i + 1 < len(marks) else len(tokens)
            operand = _operand(tokens[op.at + op.width:after_end])
            if not operand:
                before_start = marks[i - 1].at + 1 if i else 0
                operand = _operand(tokens[before_start:op.at])
            out.append(Segment(op, operand, n))
    return out


def _words_of(text: str) -> tuple[frozenset[str], frozenset[str], tuple[str, ...]]:
    nums, words = claim_terms(text)
    return words, frozenset(nums | numbers_in(text)), tuple(p.iso for p in shared_dates.find_all(text))


def _matches(need: Operand, words: frozenset[str], numbers: frozenset[str], dates: Iterable[str]) -> bool:
    """Whether what is stated (its words, numbers, dates) holds most of the operand: every number and date, and more than half of
    the content words — an operand that is a date is the date ("נכון ליום 31.12.2023" is stated by "נכון ל-31/12/2023"). An empty
    operand matches nothing."""
    if not need:
        return False
    if not need.numbers <= numbers:
        return False
    if need.dates:
        return all(any(have.startswith(d) for have in dates) for d in need.dates)
    return not need.groups or 2 * sum(1 for g in need.groups if g & words) > len(need.groups)


_Said = tuple[frozenset[str], frozenset[str], tuple[str, ...], bool]  # a clause: words, numbers, dates, holds a negative operator


def _segment_outcome(seg: Segment, sentence: list[Segment], sentence_clauses: list[_Said]) -> str:
    """The outcome of one source operator against the sentence's segments and clauses (see the module docstring)."""
    same = opposite = False
    for other in sentence:
        if other.op.key != seg.op.key:
            continue
        words = frozenset().union(*other.operand.groups) if other.operand.groups else frozenset()
        if not _matches(seg.operand, words, other.operand.numbers, other.operand.dates):
            continue
        if other.op.negative == seg.op.negative:
            same = True
        elif seg.op.cls in (EXCEPTION, THRESHOLD):
            opposite = True
    if seg.op.cls == NEGATION:
        # a negation is lost when a clause states most of its operand and holds no negation at all
        opposite = any(not negated and _matches(seg.operand, words, numbers, dates)
                       for words, numbers, dates, negated in sentence_clauses)
    if same and opposite:
        return UNDECIDED  # both polarities over the operand: the sentence is not read either way
    return KEPT if same else HINT if opposite else UNDECIDED


def _sentence_clauses(sentence: str) -> list[_Said]:
    out = []
    for c in clauses(sentence):
        words, numbers, dates = _words_of(c)
        out.append((words, numbers, dates, any(op.negative for op in _ops(_tokens(c)))))
    return out


def outcome(condition: str, sentence: str) -> str:
    """What the sentence does with one source condition: ``KEPT``, ``HINT`` or ``UNDECIDED`` (the module docstring). Several
    operators give the worst of their outcomes (a hint over an undecided over a kept); a condition with no operator is kept when
    the sentence holds most of its content words."""
    material = [s for s in segments(condition) if s.op.material]
    if not material:
        need = _operand(_tokens(condition))
        words, numbers, dates = _words_of(sentence)
        return KEPT if _matches(need, words, numbers, dates) else UNDECIDED
    said, said_clauses = segments(sentence), _sentence_clauses(sentence)
    found = [_segment_outcome(s, said, said_clauses) for s in material]
    return HINT if HINT in found else UNDECIDED if UNDECIDED in found else KEPT


def operand_of(text: str) -> Operand:
    """The content words, numbers and dates of a text, operators left out."""
    return _operand(_tokens(text))


def stated_in(condition: str, text: str) -> bool:
    """Whether most of the condition's operand (its words without the operators, every number and date) is written in ``text``,
    whatever the operator there: the test of where a condition stands, not of what it says."""
    words, numbers, dates = _words_of(text)
    return _matches(_operand(_tokens(condition)), words, numbers, dates)


def material(text: str) -> bool:
    """Whether a heading, a paragraph or a note limits the values it stands over: a closed-class operator, a status or a time
    (``verify.QUALIFIER_LEXICON``, a date), or a word for a method or a component. A plain "including" does not."""
    if any(op.material for op in operators(text)):
        return True
    words = [w for w in qualifier_words(text) if not _missing_forms(w) & _EXCEPT_IN]
    return bool(words or _QUALIFIER_PATTERN.search(text or "")
                or any(_missing_forms(t) & _METHOD_HEADS for t in _tokens(text or "")))


def material_clauses(text: str) -> list[str]:
    """The clauses of a text that hold a closed-class operator that limits (the source side), in order."""
    return [c for c in clauses(text) if any(op.material for op in _ops(_tokens(c)))]
