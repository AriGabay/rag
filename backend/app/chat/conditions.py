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
- ``MISSING``: the clear case the server decides without a judge — a condition operator whose CLASS (negation, exclusivity,
  exception, subjection, threshold; any polarity, any wording of the class: "רק" and "בלבד" are both exclusivity) the sentence does
  not carry anywhere, over an operand none of whose words, numbers or dates the sentence states. The value is given without its
  limit; the server completes it (never removes anything);
- ``UNDECIDED``: everything else — another operator for the same idea ("excluding" and "without"), an operand match below most, a
  sentence that holds both polarities over the operand. The requirements call decides these.

The classes are closed (the lists below) and no other phrase list is read. A plain "כולל" (including) is an operator for the
sentence side — it is what "not including" is contradicted by — but never a condition of a source by itself: "שטח כולל" is a total."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import NamedTuple

from app.answering.verify import numbers_in
from app.chat.evidence import HINT, KEPT, MISSING, UNDECIDED, claim_terms
from app.chat.verify import _MISSING_TOKEN, _QUALIFIER_PATTERN, _missing_forms, qualifier_words
from app.extraction.normalize_text import is_negation
from app.platform import dates as shared_dates

# the operator classes (closed): the key of an operator is what two spellings of it share, its polarity the side it stands on
NEGATION, EXCLUSIVITY, EXCEPTION, SUBJECTION, THRESHOLD = "negation", "exclusivity", "exception", "subjection", "threshold"
_EXCLUSIVE = frozenset("רק בלבד אלא".split())
_EXCEPT_OUT = frozenset("למעט מלבד פרט להוציא בהוצאת".split())
_EXCEPT_IN = frozenset("כולל כוללת כוללים כוללות לרבות".split())
_SUBJECT = frozenset("בכפוף כפוף כפופה כפופים כפופות בתנאי מותנה מותנית מותנים מותנות אם".split())
# "above"/"below" before a floor or ground-level word is a position, not a threshold: "קומות מעל קומת הקרקע", "מתחת לקרקע", "מעל פני הים"
# (round 21 packet A, Q4). A named place ("שמעל החניון") stays an operator: the wing above the lot is a limit of the datum
_SPATIAL_NEXT = frozenset("קומה קומת קומות קרקע פני גג מפלס כביש ים".split())
_THRESHOLD = {"מעל": "מעל", "יותר": "מעל", "מתחת": "מתחת", "פחות": "מתחת", "עד": "עד", "היותר": "עד", "מקסימום": "עד",
              "לפחות": "לפחות", "מינימום": "לפחות"}
# a heading or a note that names a method or a component of the valuation limits the values under it
_METHOD_HEADS = frozenset("גישת שיטת רכיב מרכיב".split())
_JOINERS = ("ו", "ש", "וש")  # a conjunction or relative prefix an operator takes ("ושלא", "וכולל")
# a dash after a one-letter preposition ("לא יפחתו מ – 31%") joins it to its number: it is no clause end (round 21 third pass, H4)
_CLAUSE_END = re.compile(r"[;|\n]|(?<!\d)[.:!?](?!\d)|(?<!\s[מלבכ])\s[—–]\s")


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


class OperatorSpan(NamedTuple):
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


# The readers below are pure functions of their strings, and the item ledger reads the same spans and sentences again for every item,
# unit and other item of a turn: each is kept per text. What is kept is immutable (tuples, frozensets, named tuples), so a cached result
# cannot be changed by its caller; the public list-returning functions hand out a copy.
@lru_cache(maxsize=4096)
def _tokens(clause: str) -> tuple[str, ...]:
    return tuple(_MISSING_TOKEN.findall(clause))


def _spatial(tokens: Sequence[str], at: int) -> bool:
    """Whether the "above"/"below" at ``tokens[at]`` says where something is (a place word follows it, with or without a "the" or "to" prefix),
    not how much (round 21 packet A, Q4)."""
    nxt = _bare(tokens[at + 1]) if at + 1 < len(tokens) else ""
    return nxt in _SPATIAL_NEXT or re.sub(r"^[הלבו]+", "", nxt) in _SPATIAL_NEXT


def _ops(tokens: Sequence[str]) -> list[Op]:
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
        if cls == THRESHOLD and key in ("מעל", "מתחת") and _spatial(tokens, at):
            at += 1
            continue
        follows = _word_op(tokens[at + 1]) if at + 1 < len(tokens) else None
        if cls == NEGATION and follows is not None and follows[0] in (EXCEPTION, THRESHOLD):
            cls, key, negative = follows[0], follows[1], not follows[2]
            out.append(Op(cls, key, negative, at, 2))
            at += 2
            continue
        out.append(Op(cls, key, negative, at))
        at += 1
    return out


@lru_cache(maxsize=4096)
def _clauses(text: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in _CLAUSE_END.split(text or "") if c and c.strip())


def clauses(text: str) -> list[str]:
    """The clauses of a text: split at a sentence end, a semicolon, a colon, a cell edge or a line, never at a comma (a list of
    exceptions is one clause) or inside a number."""
    return list(_clauses(text))


def clause_at(text: str, at: int) -> str:
    """The clause of ``text`` that holds the offset ``at`` (a clause ends as ``clauses`` reads it: a sentence end, a semicolon, a cell edge or a
    line, never a comma; round 21 second pass). The whole text when no boundary lies before the offset and after it."""
    start = 0
    for m in _CLAUSE_END.finditer(text or ""):
        if m.end() <= at:
            start = m.end()
        else:
            return text[start:m.start()].strip()
    return (text or "")[start:].strip()


_SEGMENT_END = re.compile(r",\s+")  # a comma between words, not the one inside a number ("1,500")


def without_echo(phrase: str, clause: str) -> str:
    """``phrase`` (the words of a source condition a note would carry) without the comma-separated segments the ``clause`` of the datum already
    keeps (``outcome`` is ``KEPT``: its words are written there and so is every operator that limits, so «לא כולל חניון» is no echo of
    a clause that writes the area but not the exclusion): a note says what the sentence leaves out, it does not repeat the datum's own words
    (round 21 second pass, harm 4). "" when the clause states all of it — then there is no note to write."""
    kept = [seg for seg in _SEGMENT_END.split(phrase or "") if seg and outcome(seg, clause) != KEPT]
    return ", ".join(kept)


@lru_cache(maxsize=4096)
def _operators(text: str) -> tuple[Op, ...]:
    return tuple(op for c in _clauses(text) for op in _ops(_tokens(c)))


def operators(text: str) -> list[Op]:
    """Every operator of a text, clause by clause (``Op.at`` indexes its clause's tokens)."""
    return list(_operators(text))


def _operand(tokens: Iterable[str]) -> Operand:
    kept = [t for t in tokens if _word_op(t) is None]
    text = " ".join(kept)
    groups = tuple(g for t in kept if not t[0].isdigit() and (g := claim_terms(t)[1]))
    dates = tuple(p.iso for p in shared_dates.find_all(text))
    return Operand(groups, frozenset(numbers_in(text)), dates)


@lru_cache(maxsize=4096)
def _segments(text: str) -> tuple[OperatorSpan, ...]:
    out: list[OperatorSpan] = []
    for n, clause in enumerate(_clauses(text)):
        tokens = _tokens(clause)
        marks = _ops(tokens)
        for i, op in enumerate(marks):
            after_end = marks[i + 1].at if i + 1 < len(marks) else len(tokens)
            operand = _operand(tokens[op.at + op.width:after_end])
            if not operand:
                before_start = marks[i - 1].at + 1 if i else 0
                operand = _operand(tokens[before_start:op.at])
            out.append(OperatorSpan(op, operand, n))
    return tuple(out)


def segments(text: str) -> list[OperatorSpan]:
    """The operators of a text with their operands: the tokens after an operator up to the next one, else (an operator that ends
    its clause, "בלבד") the tokens before it back to the previous one."""
    return list(_segments(text))


@lru_cache(maxsize=4096)
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


def _absent(operand: Operand, words: frozenset[str], numbers: frozenset[str], dates: Iterable[str]) -> bool:
    """Whether the sentence states nothing of the operand: none of its words (in any variant), numbers or dates. An empty operand is
    never absent: nothing is known to be left out."""
    if not operand:
        return False
    have = tuple(dates)
    return (not any(g & words for g in operand.groups) and not operand.numbers & numbers
            and not any(have_d.startswith(d) for d in operand.dates for have_d in have))


def _segment_outcome(seg: OperatorSpan, sentence: Sequence[OperatorSpan], sentence_clauses: Sequence[_Said], classes: frozenset[str],
                     stated: tuple[frozenset[str], frozenset[str], tuple[str, ...]]) -> str:
    """The outcome of one source operator against the sentence's segments and clauses (see the module docstring)."""
    if seg.op.cls not in classes and _absent(seg.operand, *stated):
        return MISSING  # the class is nowhere in the sentence and neither is its operand
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


@lru_cache(maxsize=4096)
def _sentence_clauses(sentence: str) -> tuple[_Said, ...]:
    out = []
    for c in _clauses(sentence):
        words, numbers, dates = _words_of(c)
        out.append((words, numbers, dates, any(op.negative for op in _ops(_tokens(c)))))
    return tuple(out)


@lru_cache(maxsize=4096)
def read(condition: str, sentence: str) -> tuple[str, str]:
    """(outcome, quote) of one condition against a sentence: the quote is the clause of the condition that holds the first missing
    operator, empty for any other outcome."""
    material = [s for s in _segments(condition) if s.op.material]
    if not material:
        need = _operand(_tokens(condition))
        words, numbers, dates = _words_of(sentence)
        return (KEPT if _matches(need, words, numbers, dates) else UNDECIDED), ""
    said, said_clauses = _segments(sentence), _sentence_clauses(sentence)
    classes, stated = frozenset(s.op.cls for s in said), _words_of(sentence)
    found = [_segment_outcome(s, said, said_clauses, classes, stated) for s in material]
    if HINT in found:
        return HINT, ""
    if UNDECIDED in found:
        return UNDECIDED, ""
    if MISSING in found:
        return MISSING, _clauses(condition)[material[found.index(MISSING)].clause]
    return KEPT, ""


def outcome(condition: str, sentence: str) -> str:
    """What the sentence does with one source condition: ``KEPT``, ``HINT``, ``MISSING`` or ``UNDECIDED`` (the module docstring).
    Several operators give the worst of their outcomes (a hint over an undecided over a missing over a kept: a condition the
    server cannot fully read is the judge's, not completed on a part of it); a condition with no operator is kept when the sentence
    holds most of its content words, else undecided."""
    return read(condition, sentence)[0]


def missing_quote(condition: str, sentence: str) -> str:
    """The clause of ``condition`` the sentence leaves out when ``outcome`` is ``MISSING`` (verbatim from the condition), else ''."""
    return read(condition, sentence)[1]


@lru_cache(maxsize=4096)
def operand_of(text: str) -> Operand:
    """The content words, numbers and dates of a text, operators left out."""
    return _operand(_tokens(text))


@lru_cache(maxsize=4096)
def stated_in(condition: str, text: str) -> bool:
    """Whether most of the condition's operand (its words without the operators, every number and date) is written in ``text``,
    whatever the operator there: the test of where a condition stands, not of what it says."""
    words, numbers, dates = _words_of(text)
    return _matches(operand_of(condition), words, numbers, dates)


@lru_cache(maxsize=4096)
def material(text: str) -> bool:
    """Whether a heading, a paragraph or a note limits the values it stands over: a closed-class operator, a status or a time
    (``verify.QUALIFIER_LEXICON``, a date), or a word for a method or a component. A plain "including" does not."""
    if any(op.material for op in _operators(text)):
        return True
    words = [w for w in qualifier_words(text) if not _missing_forms(w) & _EXCEPT_IN]
    return bool(words or _QUALIFIER_PATTERN.search(text or "")
                or any(_missing_forms(t) & _METHOD_HEADS for t in _tokens(text or "")))


@lru_cache(maxsize=4096)
def _material_clauses(text: str) -> tuple[str, ...]:
    return tuple(c for c in _clauses(text) if any(op.material for op in _ops(_tokens(c))))


def material_clauses(text: str) -> list[str]:
    """The clauses of a text that hold a closed-class operator that limits (the source side), in order."""
    return list(_material_clauses(text))


NEAR_TOKENS = 12  # a limiting operator this many words from the figure, at most, limits that figure


def limits_figure(text: str, forms: frozenset[str] = frozenset()) -> bool:
    """Whether the closed-class limit that ``text`` holds can be about the figure ``forms`` (round 21 third pass, H2): True when some limiting
    operator stands within ``NEAR_TOKENS`` words of one of the figure's numbers (any number of its clause when the figure is not written there), or
    when the text holds no such operator at all (a footnote or a heading: a limit by its place, not by a word). False when every limiting
    operator is farther from the figure — "if there is such a part" at the end of a long description of a list limits that part, not the "22" at its
    start, and the server does not hand it over as the figure's missing condition."""
    held = False
    for clause in _clauses(text):
        tokens = _tokens(clause)
        marks = [op for op in _ops(tokens) if op.material]
        if not marks:
            continue
        held = True
        digits = [i for i, t in enumerate(tokens) if t[:1].isdigit()]
        mine = [i for i in digits if not forms or numbers_in(tokens[i]) & forms] or digits
        if not mine or any(abs(op.at - i) <= NEAR_TOKENS for op in marks for i in mine):
            return True
    return not held

