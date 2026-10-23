"""The item ledger of an answer (round 17 U2; KTD2; R4, R5, R7).

When the request asks for the members of a kind a source lists (all the adjustments, every rate, each condition), the answering
agent declares, in its final structured answer and before the prose, one item per member it found for each frozen component
(``engine.LedgerItem``): its source, a short verbatim quote, its value and condition as the source writes them, and a state.
This module is what the server does with that list. It never takes the agent's word:

- ``validate`` keeps an item only when it passes the checks the round-16 support goes through (``verify.SupportScope``): the
  source was returned to the agent with text, the quote is of a sensible length and a verbatim part of the source, the source
  lies in an asked unit and in a document the answer is about. It also needs a component of the frozen request (a component given
  as text is mapped to its id; a turn with no components keeps its items without one), a known state, and a reason for a
  ``not_verifiable`` or ``not_applicable`` item. An item that fails is excluded and recorded with the first reason it failed, never
  silently dropped; "never up" — a check can only remove an item, nothing is added to the ledger from the agent's list.
- ``covered`` decides whether a surviving unit of the answer gives an item, by attribution (R5): the unit cites the item's source,
  or a value (V#) or calculation (C#) registered from it, or another passage of the same document and unit that holds the item's
  quote, in the item's own document and appraisal unit; and it states the item's value (for an item without a value, at least half
  of its label's content words), and the item's date when it has one. The item's condition words are asked only to tell apart
  items of one component that share a value (or a label), so a paraphrased condition still covers. Words or numbers somewhere
  else in the answer, or in a unit that cites another document, cover nothing.
- ``Ledger`` is the turn's ledger across answer versions, keyed by (component, evidence). Each version's valid items
  merge into it; an item the server added from the judge's support (``support_item``, state ``omitted``) stays until a surviving
  unit covers it or a later version declares it ``not_verifiable`` or ``not_applicable`` with a valid reason. The agent not
  listing an item again closes nothing.

Round 19 U3 (KTD1; R1-R3) separates the EVIDENCE from the REQUEST COMPONENT it fills. A piece of evidence is a span of a source,
identified by (document, appraisal unit, verbatim quote) — no S# handle, so a repair citing the same passage under another S# is the
same evidence, and the same quote in two appraisal units of one file is two. An ``Item`` is the FILL of one component by one
evidence: its own state, reason, origin, version and history. One evidence fills several components with one fill each; an agent's
fill replaces a server fill only of the SAME component (the same evidence, or a containing or contained quote of the same document
and unit) and never deletes the fill of another; the condition of an evidence is checked once, whatever the number of its fills. A
closed decision (``not_verifiable`` / ``not_applicable`` with a reason) is not reopened by the same item listed ``included`` again.

The wiring into the requirements call, the outcomes and the repair is round 17 U3's.

Round 18 U5 (KTD1; R1-R4) adds the item's CONDITION as a check of its own, separate from the ``shared`` words above that tell
similar items apart:

- ``condition_spans`` reads what limits an item from its own source context: a closed-class operator clause of its quote or of its
  row or sentence, its row's note, a paragraph attributed to its table (U4's ``TablePart.attributed``) and its heading when that
  names a method, a status, a component or a time. ``validate`` checks the agent's condition against that context: one that stands
  only in another row's context is recorded as misattributed (never silent, never a reason to drop the item).
- ``source_items`` builds an origin ``source`` item from a source span an answer sentence cites when the span carries such a
  condition and no item covers it, so the condition is checked on a turn whose agent declared nothing.
- ``Ledger.outcomes`` gives every covered item a ``condition_check`` against the sentence that covers it (``conditions.outcome``):
  ``kept``, ``missing`` (the clear case: the operator's class and its operand are both absent from the sentence, decided by the
  server alone and never sent to the requirements call), ``undecided`` or ``hint``, apart from the coverage above. A condition is
  read only from the source's own spans; what the server cannot decide goes to the requirements call (``verify._item_ledger_block``). A sentence that carries a condition of
  another item's span, and none of its own, is a ``hint`` from another item.

Round 18 U6 (KTD2; R5-R7) finds the items the agent dropped from BOTH its ledger and the answer, in the structure of the sources the
turn read. ``candidates`` offers, around each anchor (a ledger item's source, a read source the answer cites), the siblings of the
anchor that no sentence gives and no ledger item holds: the other data rows of its table, the other members of its enumerated list,
the other value-bearing sentences of a cited passage's paragraph run, and the notes of its table that state a value of their own.
A note, paragraph or heading that LIMITS a value is that value's condition span, never a candidate. The requirements call decides
which candidate a frozen component asks for (``verify.valid_candidates`` checks its words against the request and the candidate's
own text); ``candidate_item`` builds the ledger item of a kept one — origin ``source``, ``candidate`` True, its label, value and
condition built from the source — which stays open until a sentence gives it, is completed by the existing text repair, and gets
the "read at <place>, not used" line (built from the source's words) if it is still omitted.

Round 18 U7 (KTD6; R11) makes what coverage says, and what the lines built from it say, true. A value is stated when each of its numbers
has a form the sentence writes (``_number_forms``: "16.07 מיליון" and "16,070,000" are one number); an item's date may stand in the
sentence's own lead (``_leads``: the lead-in of its list, the sentence and header before its table, the headings above it) when the
sentence states no date of its own. ``source_name`` is how a server line names an item: the source's row label or quote, never the
agent's label. An item whose value an attributed sentence states but whose date it does not stays omitted for the repair round, and is
never "read and not used" (``coverage.item_gap_lines``).

Round 20 U1 (KTD1; R1-R4) makes the identity of an item the DATUM it states, not the overlap of its quote. Two fills of one component whose
quotes are the same or nested are one fill only when ``same_datum`` holds: the same headline values (an aggregate never absorbs its parts),
compatible dates, the same source block (``block_of``) and no condition of one with a word the other lacks. The datum fingerprint
(``Datum``) is taken once, at a fill's first insertion, and kept: a later declaration is matched against it and never rewrites it. A second
declaration of the same evidence with another datum is a fill of its own (its key adds the fingerprint, ``Item.variant``) and is no
duplicate; a closed decision moves only through a twin that passed the gate; a server item is dropped only beside a same-datum fill; and a
wide item defers its condition check only to a covered narrow item of its own component and datum (``_defers``). An item of an earlier version
that the latest version does not list, when the latest declares another datum on the same or a nested quote of the component, is closed as
superseded (``Ledger._supersede``): it is not scored and writes no gap line.

Round 19 U4 (KTD4; R13-R17) makes coverage ONE pure function over the model-written text of the answer as it stands, by BLOCK. A block is a
list item with all its sentences, a paragraph, a table row or a heading (``Unit.block``, ``blocks_of``); its text is the union of its
sentences' and its citations the union of theirs, so an item's value, its plan number and its condition may stand in different sentences
of one block, and the heading, lead-in or table header above it gives its date. What the server wrote inside or after the model's text (a
marker, a mark, a gap line, an item line, a note) covers nothing (``model_units``). Per fill (evidence x component) the function yields
one outcome: ``covered``; ``partial`` — the block gives some of the item's HEADLINE values (``headline``: the numbers of its value field
that its quote writes too, minus identifiers, plan and permit numbers, footnote marks and ordinals) and ``missing_detail`` names the others
as the source writes them; ``omitted`` — no headline value in a block that cites the item's source (an item without one is asked by half
the words of its value, label or quote, in the block); or ``unchecked`` — the check itself failed or an input is missing, which is its own
state and wording, never "omitted". A declared ``not_verifiable`` / ``not_applicable`` fill passes the same check: a block that states it
covers it, and no line is written. ``Ledger.outcomes`` is that function over the ledger; ``VerifyReport.decision`` is its value over the units
that stand, computed at every compose pass, and the body's caps, the gap paragraph and the item lines are all read from it."""

from __future__ import annotations

import functools
import hashlib
import logging
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from app.answering.numread import number_forms, number_spans, number_words, whole_number_word
from app.answering.verify import _QUANTITY_AFTER, not_an_amount, numbers_in
from app.chat import conditions, scopes
from app.chat.evidence import OTHER_ITEM, claim_terms
from app.chat.meaning import _row_cells, after_kind
from app.chat.verify import (
    _FINAL_LETTERS,
    ITEM_COVERED,
    ITEM_INCLUDED,
    ITEM_NOT_APPLICABLE,
    ITEM_NOT_VERIFIABLE,
    ITEM_OMITTED,
    ITEM_UNCHECKED,
    ORIGIN_AGENT,
    ORIGIN_JUDGE_SUPPORT,
    ORIGIN_SOURCE,
    SUPPORT_QUOTE_MAX,
    SUPPORT_QUOTE_MIN,
    SupportScope,
    Unit,
    _cited_sources,
    _flat_span,
    _is_tabular,
    _without_server,
    asked_components,
    context_of_source,
    evidence_key,
    is_server_item,
    request_word_forms,
    shared_request_words,
    shares_request_word,
    unit_referent,
)
from app.extraction.normalize_text import (
    absence_clause,
    clause_spans,
    holds_absence,
    negated_spans,
    negations_in,
)
from app.platform import dates as shared_dates

logger = logging.getLogger(__name__)

STATES = (ITEM_INCLUDED, ITEM_NOT_VERIFIABLE, ITEM_NOT_APPLICABLE)  # what the agent declares
OMITTED = ITEM_OMITTED  # what the server assigns: a member the judge's support adds, and an included item no surviving unit covers
COVERED = ITEM_COVERED
OUTCOME_PARTIAL = "partial"  # the block gives some of the item's headline values and leaves others out (``Ledger.outcomes``)
NEEDS_REASON = (ITEM_NOT_VERIFIABLE, ITEM_NOT_APPLICABLE)
SUPERSEDED = "superseded"  # round 20 U1: the history mark of an item a later version's item of another datum corrected (``Ledger._supersede``)
# the order of the coverage checks: a failure is reported by the furthest check a unit reached
_WHY_ORDER = ("not_attributed", "value_not_stated", "date_not_stated", "condition_not_stated")


Evidence = tuple[str, str, str]  # (document, appraisal unit, verbatim quote): ``verify.evidence_key``
ItemKey = tuple[str, Evidence, str]  # (component, evidence, variant): a fill; the variant is "" unless another datum rests on the same evidence


@dataclass
class Item:
    """One FILL of the ledger: one request component filled by one piece of evidence (round 19 U3). ``component``: the N# id ("" in a
    turn with no frozen components); ``source``: an S#, as a V# or T# the agent gave is resolved to the text it names; ``origin``:
    ``agent`` or ``judge_support``; ``id``: I# (the fill's id), assigned by the turn's ledger and kept for the fill's key across versions;
    ``document_id`` and ``unit_id`` (None in a file without unit marks): the place of its source, which with the quote is the evidence
    (``evidence``; ``evidence_id``: the ledger's E#). ``first_version`` / ``last_version`` / ``history``: the answer versions that declared
    it and what each declared, as the ledger merged it."""

    component: str
    label: str
    source: str
    quote: str
    value: str = ""
    condition: str = ""
    state: str = ITEM_INCLUDED
    reason: str = ""
    origin: str = ORIGIN_AGENT
    id: str = ""
    document_id: str = ""
    unit_id: str | None = None
    candidate: bool = False  # round 18 U6: an origin ``source`` item built from a candidate the requirements call assigned to a component
    evidence_id: str = ""
    first_version: int = 0
    last_version: int = 0
    history: list[dict] = field(default_factory=list)
    # round 20 U1 (KTD1): where the quote stands in its source (``block_of``: the table and the row or paragraph holding it), the datum
    # fingerprint frozen at the fill's first insertion (``datum_of``), the key suffix of a fill that shares its evidence with a fill of
    # another datum, and the I# of the later item that corrected this one
    block: tuple[int | None, str] = (None, "")
    datum: Datum | None = field(default=None, repr=False, compare=False)
    variant: str = ""
    superseded_by: str = ""

    @property
    def evidence(self) -> Evidence:
        """The piece of evidence the fill rests on: its document, its appraisal unit and its verbatim quote."""
        return evidence_key(self.document_id, self.unit_id, self.source, self.quote)

    @property
    def key(self) -> ItemKey:
        """The fill's identity across answer versions: its component, its evidence and, only for a fill that shares its evidence with a
        fill of another datum, that datum's fingerprint (round 20 U1, KTD1)."""
        return self.component, self.evidence, self.variant

    def public(self) -> dict:
        return {"id": self.id, "component": self.component, "label": self.label, "source": self.source,
                "quote": self.quote, "value": self.value, "condition": self.condition, "state": self.state,
                "reason": self.reason, "origin": self.origin, "document_id": self.document_id, "unit_id": self.unit_id,
                "candidate": self.candidate, "evidence_id": self.evidence_id, "fill_id": self.id}


class Validation(NamedTuple):
    kept: list[Item]
    excluded: list[dict]  # [{"component", "source", "quote", "state", "reason": why it was excluded}]
    # [{"source", "quote", "condition"}]: items kept whose agent-written condition stands only in another row's context (R3)
    misattributed: list[dict]


class Coverage(NamedTuple):
    ok: bool
    unit: int | None  # the index of the first sentence of the covering block (``Unit.index``)
    # when not covered: the furthest check a block passed to (``_WHY_ORDER``), ``value_partial`` — a block gives some of its headline
    # values (``missing`` names the others) —, or ``unchecked`` — the check could not be completed (``checked`` False)
    why: str
    missing: tuple[frozenset[str], ...] = ()
    checked: bool = True


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _fields(raw: object) -> dict[str, str]:
    """An item as the agent sent it (a model or a mapping), its strings normalised."""
    data = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)  # type: ignore[arg-type]
    return {k: _text(data.get(k)) for k in ("component", "label", "source", "quote", "value", "condition", "state", "reason")}


def _component(given: str, frozen: Mapping[str, str]) -> str | None:
    """The frozen id a component names: its N# id (in any case), else the one component whose text it is or contains (or is
    contained in); None when it names none, or more than one."""
    if not frozen:
        return ""
    if given in frozen:
        return given
    if given.upper() in frozen:
        return given.upper()
    wanted = given.casefold()
    if len(wanted) < 4:
        return None
    hits = [cid for cid, text in frozen.items() if (t := _text(text).casefold()) and (wanted == t or wanted in t or t in wanted)]
    return hits[0] if len(hits) == 1 else None


def _resolve_source(ws, handle: str, flat: str, scope: SupportScope) -> str:
    """The S# a handle of an item stands for: an S# itself; a value (V#) names the source it was registered from; a table (T#)
    names a source returned for it that holds the quote (else the first). Anything else is returned as given."""
    if handle in ws.values:
        return ws.values[handle].source_id
    if handle[:1] == "T" and handle not in ws.sources:
        sids = [sid for sid, s in ws.sources.items() if handle in (s.tags.get("table") or "").split()]
        for sid in sids:
            if scope.source_problem(sid, flat) is None and scope.verbatim(sid, ws.sources[sid], flat):
                return sid
        return sids[0] if sids else handle
    return handle


def validate(raw: Iterable[object], ws, units: Sequence[Unit], components: Sequence[Mapping],
             ledger: Ledger | None = None) -> Validation:
    """The agent's items that stand (see the module docstring), each with its place and an I# id (the turn ledger's when
    ``ledger`` is given, so an item keeps its id across versions), and the excluded ones with their first failing reason.
    ``units``: the answer's surviving units (they decide the asked units and the documents the answer is about);
    ``components``: the turn's frozen requirements ({"id", "text"})."""
    scope = SupportScope(ws, list(units))
    frozen = {c["id"]: c.get("text") or "" for c in components}
    # an instruction about the answer itself ("צרף מקור לכל נתון") is scored against the answer and never searched for: no item rests on it,
    # so no gap line is written for it (round 21 U7, R7)
    instructions = {c["id"] for c in components if c.get("kind") == "instruction"}
    kept: list[Item] = []
    excluded: list[dict] = []
    misattributed: list[dict] = []
    seen: dict[ItemKey, list[Item]] = {}
    numbers = iter(range(1, 10**6))
    for entry in raw:
        f = _fields(entry)
        flat = SupportScope.flat(f["quote"])
        component = _component(f["component"], frozen)
        if component in instructions:
            component = None
        sid = f["source"]
        if f["state"] not in STATES:
            why = "bad_state"
        elif f["state"] in NEEDS_REASON and not f["reason"]:
            why = "no_reason"
        elif component is None:
            why = "component_unassigned"
        else:
            sid = _resolve_source(ws, sid, flat, scope)
            src = ws.sources.get(sid)
            # the same evidence under one component is one item, whichever S# the agent cites it by (round 19 U3)
            key = (component, evidence_key(*_site_of(src), sid, flat) if src is not None else (sid, "", flat), "")
            why = scope.source_problem(sid, flat)
            if why is None:
                why = scope.place_problem(src) or (None if scope.verbatim(sid, src, flat) else "not_verbatim")
            if why is None:
                document_id, unit_id = _site_of(src)
                condition = f["condition"]
                foreign = bool(condition) and (_foreign_condition(condition, src, f["quote"])
                                               or _note_of_another_scope(ws, sid, src, condition, f["quote"], condition=True))
                item = Item(component, f["label"], sid, f["quote"], f["value"], "" if foreign else condition, f["state"], f["reason"],
                            document_id=document_id, unit_id=unit_id, block=block_of(src, f["quote"]))
                # a second declaration of the same evidence is a duplicate only when it states the same datum (round 20 U1, KTD1): the
                # same quote with another value, date or condition is another item of the same passage
                if any(same_datum(other, item) for other in seen.get(key, ())):
                    why = "duplicate"
                else:
                    seen.setdefault(key, []).append(item)
                    if foreign:
                        misattributed.append({"source": sid, "quote": f["quote"][:SUPPORT_QUOTE_MAX], "condition": condition})
                        # the item is checked against its own context (``condition_spans``), not the agent's words
                    item.id = ledger.id_of(item) if ledger is not None else f"I{next(numbers)}"
                    kept.append(item)
                    continue
        excluded.append({"component": f["component"], "source": f["source"], "quote": f["quote"][:SUPPORT_QUOTE_MAX],
                         "state": f["state"], "reason": why})
    return Validation(kept, excluded, misattributed)


def support_item(component: str, source: str, quote: str, ledger: Ledger, *, label: str = "", value: str = "",
                 condition: str = "", ws=None) -> Item:
    """An item the server adds from the judge's validated support (round 16 U2): a member the answer omits, state ``omitted``,
    origin ``judge_support``. Its identity is the ledger's, so the agent listing the same part later is the same item."""
    item = Item(component, label, source, quote, value, condition, OMITTED, "", ORIGIN_JUDGE_SUPPORT)
    if ws is not None and (src := ws.sources.get(source)) is not None:
        item.document_id, item.unit_id = _site_of(src)
        item.block = block_of(src, quote)
    item.id = ledger.id_of(item)
    return item


# --- the condition of an item: its span in its own source context (round 18 U5, KTD1; R1-R4) ---------------------------------

ROWS_PER_UNIT = 5  # the cited rows of one source a sentence builds items from, at most
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# a comma joins the digits of one number only in groups of three ("1,250,000"): "2018,2019,2021" is three years (``answering.verify._NUMBER``)
_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+(?!\d)|\d+)(?:\.\d+)?%?")


class Span(NamedTuple):
    """One span of an item's own source context that states a condition: ``place`` — ``quote`` (a closed-class operator clause of the
    item's quote), ``row`` (such a clause of its row or sentence), ``note`` (its row's note cell), ``paragraph`` (a paragraph
    attributed to its table, or one of its notes) or ``heading`` (a heading that names a method, a status, a component or a time) —
    and ``text`` as the source writes it."""

    place: str
    text: str

    def public(self) -> dict:
        return {"place": self.place, "text": self.text}


def _row_of(body: Sequence[str], flat: str) -> str | None:
    """The line of a source's body (a table row, a paragraph) that holds the quote ``flat``."""
    return next((ln for ln in body if flat in _flat_span(ln)), None)


def source_name(src, quote: str) -> str:
    """How the source names the span an item quotes (round 18 U7, KTD6): the first cell of its table row, else the quote itself. A server
    line names an item by this, never by the agent's own label."""
    if src is not None:
        row = _row_of(context_of_source(src)[1], SupportScope.flat(quote))
        cells = _row_cells(row) if row is not None and "|" in row else []
        if len(cells) >= 2 and re.search(r"[^\W\d_]", cells[0]):
            return " ".join(cells[0].split())
    return " ".join(quote.split())


MISSING_DETAIL_CHARS = 60  # the source words that name a missing figure, at most
_WORDS_AROUND = 3  # a missing figure of a passage is named with this many words on each side of it


def _tokens(text: str) -> list[str]:
    """The blank-separated tokens of ``text``, a run of single letters ("W A L E": a word the source spaces out) being one token."""
    out: list[str] = []
    spaced = False
    for token in text.split():
        single = len(token) == 1 and token.isalpha()
        if single and spaced:
            out[-1] += " " + token
        else:
            out.append(token)
        spaced = single
    return out


def _whole_tokens(text: str, limit: int) -> str:
    """``text`` cut after the last whole token that keeps it within ``limit`` characters (``_tokens``): never a half word, and never the
    letters of a spaced word ("W A L E") cut in the middle (round 21 U7, KTD7); the first token alone when it is longer than the limit
    (clipped)."""
    tokens = _tokens(text)
    out: list[str] = []
    for token in tokens:
        if len(" ".join([*out, token])) > limit:
            break
        out.append(token)
    return " ".join(out) if out else (tokens[0][:limit] if tokens else "")


def missing_detail(src, quote: str, group: frozenset[str]) -> str:
    """How the source names the figure of the item's quote that the answer leaves out (round 19 U4, R15): for a table row the figure's cell with
    the header of its column, for a passage the figure with the words next to it. Every word is the source's own, and a whole token of it:
    the figure's token is taken to the blanks around it ("ריבית5.4%שנתית" is one), and the cut at ``MISSING_DETAIL_CHARS`` falls between tokens
    (round 21 U7, KTD7)."""
    cells = _row_cells(quote) if "|" in quote else []
    if len(cells) >= 2:
        at = next((i for i, c in enumerate(cells) if group & number_forms(c, source=True)), None)
        if at is not None:
            head = _header_cells(context_of_source(src)[1]) if src is not None else []
            named = f"{head[at]}: {cells[at]}" if len(head) == len(cells) and head[at] else cells[at]
            return _whole_tokens(named, MISSING_DETAIL_CHARS)
    for start, end, forms in number_spans(quote, source=True):
        if group & forms:
            while start > 0 and not quote[start - 1].isspace():
                start -= 1
            while end < len(quote) and not quote[end].isspace():
                end += 1
            left, right = _tokens(quote[:start])[-_WORDS_AROUND:], _tokens(quote[end:])[:_WORDS_AROUND]
            near = " ".join([*left, quote[start:end].strip(), *right])
            return _whole_tokens(re.sub(r"[\[\]]", " ", near), MISSING_DETAIL_CHARS)
    return _whole_tokens(quote, MISSING_DETAIL_CHARS)


def _header_cells(body: Sequence[str]) -> list[str]:
    """The cells of the table's header row: its first row, when it holds no digit."""
    first = next((ln for ln in body if "|" in ln), None)
    return _row_cells(first) if first is not None and not re.search(r"\d", first) else []


def _placeholder(cell: str, header: str) -> bool:
    """Whether a cell holds no more than the header's own words ("ללא הערה" under "הערה", or an operator alone): it states nothing."""
    need = conditions.operand_of(cell).groups
    have = conditions.operand_of(header).groups
    return not need or all(any(g & h for h in have) for g in need)


def _row_conditions(row: str, body: Sequence[str], flat: str) -> Iterable[tuple[str, str]]:
    """(place, text) of what a row or sentence says that limits its value: the note cell of a table row (its last cell, with three
    words or more that are not the header's own), an operator clause of its other cells, or an operator clause of the sentence
    that holds the quote."""
    cells = _row_cells(row) if "|" in row else []
    if len(cells) >= 2:
        head = _header_cells(body)
        for i, cell in enumerate(cells[1:], 1):
            last = i == len(cells) - 1 and len(cells) >= 3
            if last and _placeholder(cell, head[i] if len(head) == len(cells) else ""):
                continue
            if last and len(re.findall(r"[^\W\d_]+", cell)) >= 3:
                yield "note", cell
            else:
                yield from (("row", c) for c in conditions.material_clauses(cell))
        return
    sentence = next((x for x in _SENTENCE_END.split(row) if flat in _flat_span(x)), "")
    yield from (("row", c) for c in conditions.material_clauses(sentence))


def condition_spans(src, quote: str, elsewhere: Sequence[str] = (), value: str = "") -> list[Span]:
    """The condition spans of the item whose quote is ``quote`` in ``src``, from its own context only: an operator clause of the
    quote, the clauses and note of its row (or its sentence), the headings that name a method, a status, a component or a time, and
    the paragraphs and notes attributed to its table (``TablePart.attributed``: a paragraph that is not attributed is no condition
    of its values). Each span once, in that order. ``elsewhere``: the quotes of the other items of the turn: a note whose mark one of
    them carries and the item's own quote and row do not is the note of that item (round 20 U9, F2). ``value``: the item's value; a quote that
    holds it and another amount gives the item only the operator clauses of its own part (``_foreign_part``: round 21 full measurement)."""
    parts, body = context_of_source(src)
    flat = SupportScope.flat(quote)
    out: list[Span] = []
    seen: set[str] = set()

    def add(place: str, text: str) -> None:
        key = _flat_span(text)
        if key and key not in seen:
            seen.add(key)
            out.append(Span(place, " ".join(text.split())))

    row = _row_of(body, flat)
    if row is None or "|" not in row:  # a table row is read cell by cell below: its placeholder cells say nothing
        for clause in conditions.material_clauses(quote):
            if not _foreign_part(quote, clause, value):
                add("quote", clause)
    if row is not None:
        for place, text in _row_conditions(row, body, flat):
            add(place, text)
    for part in parts:
        if part.place == "heading":
            if conditions.material(part.text):
                add("heading", part.text)
        elif part.place in ("before", "after", "note") and not part.reason and _note_of_row(part.text, body, row, quote, elsewhere):
            for sentence in _SENTENCE_END.split(part.text):
                if conditions.material(sentence):
                    add("paragraph", sentence)
    return out


# the places a quote of several amounts breaks into the part of each: a comma that joins no digits, a semicolon, and the words that open another part
_PART_BREAK = re.compile(r"(?<!\d),(?!\d)|;|(?<![א-ת])(?:דהיינו|כלומר|ואילו)(?![א-ת])")


def _foreign_part(quote: str, clause: str, value: str) -> bool:
    """Whether the operator clause ``clause`` of a quote that holds the item's ``value`` and another amount limits only the other amount (round 21
    full measurement: «… 3.250 דונם בבעלות צד אחר והוא אינו מהווה חלק מנשוא חוות הדעת, דהיינו סה"כ … 40.100 דונם»: the exclusion is the 3.250 part's,
    not the total's). The quote is cut into parts at a comma, a semicolon and «דהיינו», «כלומר», «ואילו»; a part that holds an amount other than the
    value and not the value is foreign. The clause is foreign when it has an operator in some part and every part with an operator is foreign. No
    value, a quote with one amount or none of the value, and a clause with no part of its own are never foreign (the clause is the item's, as before)."""
    if not value or not (tokens := value_tokens(value, quote)) or not any(tokens):
        return False
    mine = [place for places in token_places(quote, *tokens) for place in places]
    if not mine:
        return False
    _, numbers = _text_scan(quote)
    others = [(a, b) for a, b, _ in numbers if not _not_headline(quote, a, b) and not any(x <= a and b <= y for x, y in mine)]
    start = quote.find(clause)
    if not others or start < 0:
        return False
    end, cuts = start + len(clause), [0, *(m.end() for m in _PART_BREAK.finditer(quote)), len(quote)]
    held = False
    for a, b in zip(cuts, cuts[1:], strict=False):
        if b <= start or a >= end:
            continue
        part = quote[max(a, start):min(b, end)]
        if not conditions.material_clauses(part):
            continue
        held = True
        if not any(a <= x and y <= b for x, y in others) or any(a <= x and y <= b for x, y in mine):
            return False  # an operator of the part that holds the value, or of a part with no other amount: the item's
    return held


def _note_of_row(note: str, body: Sequence[str], row: str | None, quote: str = "", elsewhere: Sequence[str] = ()) -> bool:
    """Whether a note beside a table limits the item whose row is ``row`` (round 20 U3, KTD3, R10, AE7; round 20 U9, F2): a note that opens
    with a mark («*», «**», «(1)», «(***)») belongs to the cells that carry the same mark, so it is no condition of a row that carries none
    of them; a mark in a header cell belongs to every row. A mark that the quote of another item (``elsewhere``) carries, and the item's own
    quote and row do not, is the mark of that item's row, even in another table: the note is no condition of this item. A note without a
    mark and a mark nobody carries keep the note for a table row. Round 21 full measurement: a marked note belongs to what carries its
    mark, never to a prose item whose own quote does not carry it, whether or not a row of this source text carries the mark."""
    mark = scopes.note_mark(note)
    if not mark:
        return True
    elsewhere_holds = any(mark in scopes.marks_of(q) for q in elsewhere)
    if row is None or "|" not in row:
        # round 21 full measurement: a marked note belongs to what carries its mark, so a prose item whose own quote does not carry it never
        # takes it (the mark may stand on a row of another chunk or source that this text does not hold: it is still not this item's)
        return mark in scopes.marks_of(quote)
    rows, held = _rows_marked(tuple(body), mark)
    if held is None:  # no cell of the table carries it: another item's note only when the table carries no mark of its own
        return mark in scopes.marks_of(quote) or not elsewhere_holds or _marked_table(tuple(body))
    return row in {rows[i] for i in held}


@functools.lru_cache(maxsize=256)
def _marked_table(body: tuple[str, ...]) -> bool:
    """Whether any cell of a table text carries a note mark."""
    return any(scopes.cell_marks(c) for ln in body if "|" in ln for c in scopes.row_cells(ln))


@functools.lru_cache(maxsize=256)
def _rows_marked(body: tuple[str, ...], mark: str) -> tuple[tuple[str, ...], frozenset[int] | None]:
    """The body rows of a table text (all but its header) and the indexes of those a note with ``mark`` belongs to (``scopes.note_rows``),
    kept per text and mark: every note, row and item of a turn asks the same few tables."""
    header = scopes.header_of(body)
    rows = tuple(ln for ln in body if "|" in ln and scopes.row_cells(ln) != header)
    return rows, scopes.note_rows(mark, header, rows)


def _own_context(src, quote: str, elsewhere: Sequence[str] = ()) -> str:
    """The text of the item's own context: its quote and row, the paragraphs attributed to the table and its headings."""
    parts, body = context_of_source(src)
    row = _row_of(body, SupportScope.flat(quote))
    return "\n".join([quote, row or "", *(p.text for p in parts if not p.reason and _note_of_row(p.text, body, row, quote, elsewhere))])


def _other_context(src, quote: str, elsewhere: Sequence[str] = ()) -> str:
    """The text of what stands beside the item's row in a table: the other rows and the paragraphs not attributed to the table ("" for
    a source that is no table). Only ``_foreign_condition`` reads it."""
    if not _is_tabular(src):
        return ""
    parts, body = context_of_source(src)
    row = _row_of(body, SupportScope.flat(quote))
    return "\n".join([ln for ln in body if ln != row] + [p.text for p in parts if p.reason or not _note_of_row(p.text, body, row, quote, elsewhere)])


def _scope_of_quote(ws, sid: str, src, quote: str) -> dict:
    """The scope the item's quote is written under in its source (``scopes.occurrences``: the openers above it, its column and row), {} when
    the source has no scope or the quote is not found. A quote in a table row is looked for in the cells of that row only (a cell of an
    earlier row that is empty or short is no place of the quote); a place states the quote when it holds it, or is a sentence of at least
    ``SUPPORT_QUOTE_MIN`` characters inside a longer quote, and an empty place never does."""
    parts, body = context_of_source(src)
    flat = SupportScope.flat(quote)
    if not flat:
        return {}
    row = _row_of(body, flat)
    row = row if row is not None and "|" in row else None

    def holds(text: str) -> bool:
        span = _flat_span(text)
        return bool(span) and (flat in span or (len(span) >= SUPPORT_QUOTE_MIN and span in flat))

    layout = ws.layouts.get(sid)
    place = [(p.place, p.text) for p in parts]
    found = scopes.first_occurrence(place, body, layout, holds, row)
    if found is None and row is not None:
        found = scopes.first_occurrence(place, body, layout, lambda text: True, row)  # a row of short cells: the scope the row sets
    return dict(found.scope) if found is not None else {}


def _note_of_another_scope(ws, sid: str, src, text: str, quote: str, *, condition: bool = False,
                           against: Mapping[str, frozenset[str]] | None = None) -> bool:
    """Whether ``text`` is a footnote of the source that its geometry binds to another scope than the item's (round 20 U3, KTD3, R10, AE7):
    ``condition`` — ``text`` is the words of a condition (stated in the note, not in the item's own context) and the note is bound to
    other identifiers (another contract, lot group or plan) than the quote stands under; otherwise ``text`` is a sentence of the source,
    and the note it belongs to is bound to another scope than ``against`` (what the answer sentence that cites it is about). A note whose
    mark could not be bound conflicts with nothing: it keeps its own server item and is never another item's condition."""
    layout = ws.layouts.get(sid)
    found = scopes.notes(layout)
    if not found:
        return False
    flat = SupportScope.flat(text)
    mine = item = own = None
    for note in found:
        if condition:
            if not conditions.stated_in(text, note.text):
                continue
            if own is None:
                own = _own_context(src, quote)  # the item's own context, read once for all the notes
            if conditions.stated_in(text, own):
                continue
        elif flat not in _flat_span(note.text):
            continue
        if note.scope is None:
            continue  # an unbound note is the note's own item, as before; nothing proves it another item's (the judge is told when a claim states it)
        if mine is None:
            mine = against if against is not None else _scope_of_quote(ws, sid, src, quote)
            item = scopes.item_of_referent(layout, mine) if against is not None else scopes.item_of_text(layout, quote)
        if scopes.note_conflicts(note, mine, item):
            return True
    return False


def _foreign_condition(condition: str, src, quote: str, elsewhere: Sequence[str] = ()) -> bool:
    """Whether an agent-written condition stands only in another item's context: most of its words are not in the item's own
    context and are in a row beside it (R3)."""
    return (not conditions.stated_in(condition, _own_context(src, quote, elsewhere))
            and conditions.stated_in(condition, _other_context(src, quote, elsewhere)))


def _clip_row(line: str) -> str:
    """A row as an item's quote: the whole row, else its cells up to ``SUPPORT_QUOTE_MAX`` characters."""
    if len(line) <= SUPPORT_QUOTE_MAX:
        return line
    kept: list[str] = []
    for cell in _row_cells(line):
        if len(" | ".join([*kept, cell])) > SUPPORT_QUOTE_MAX:
            break
        kept.append(cell)
    return " | ".join(kept)


class _SpanRow(NamedTuple):
    """A table row of a source for ``_cited_spans``: the line, its first cell, its first value cell, the numbers its value cells state and the
    class of each (``_number_class``), then a plain class for each number that no matched number holds."""

    line: str
    label: str
    value: str
    numbers: frozenset[str]
    classes: tuple[tuple[frozenset[str], str], ...] = ()


class _SpanSentence(NamedTuple):
    """A sentence of a passage line for ``_cited_spans``: its text and, for each number it writes outside its dates, (as written, its numbers,
    its class)."""

    text: str
    written: tuple[tuple[str, frozenset[str], str], ...]
    names: tuple[tuple[str, frozenset[str], str], ...] = ()  # the numbers it writes as names (``_not_headline``: a plot, a block, a permit), same shape


_PERCENT_WORD = re.compile(r"\s?(?:%|אחוז(?:ים)?(?![א-ת]))")
WRAP_MIN = 45  # a line at least this long, with no sentence end, goes on in the line after it (``_unwrapped``)
_SENTENCE_CLOSE = re.compile(r"[.!?;:]['\")\]״׳]*\s*$")


def _number_class(text: str, start: int, end: int) -> str:
    """What the number at ``text[start:end]`` counts: "percent", "count" (a noun of things follows: "9 קומות"), "duration" (a time word follows)
    or "" (a plain number or an amount). A percent is no count and no duration, and neither is the other (round 21 packet A, Q4)."""
    if text[start:end].endswith("%") or _PERCENT_WORD.match(text, end):
        return "percent"
    return after_kind(text, end)


def _compatible(a: str, b: str) -> bool:
    """Whether two number classes can be one datum: the same, or either plain."""
    return a == b or not a or not b


def _states(pairs: Iterable[tuple[frozenset[str], str]], wanted: Mapping[str, set[str]]) -> bool:
    """Whether a span's numbers (forms, class) state a number of the unit (form -> classes) in a compatible class."""
    return any(_compatible(cls, w) for forms, cls in pairs for f in forms if f in wanted for w in wanted[f])


def _unwrapped(lines: Sequence[str]) -> list[str]:
    """The lines of a passage with each wrapped sentence on one line (round 21 packet A, Q4): a line of at least ``WRAP_MIN`` characters that
    does not end a sentence and is no table row, heading or list member goes on in the next line when that is no list member, heading, table
    row or note (a footnote mark opens it) either. A sentence cut per source line leaves a headless fragment that no item may be built from."""
    out: list[str] = []
    for line in lines:
        prev = out[-1] if out else ""
        if (prev and len(prev) >= WRAP_MIN and "|" not in prev and "|" not in line and not _SENTENCE_CLOSE.search(prev)
                and not _HEADING_LINE.match(prev) and not _HEADING_LINE.match(line) and not _MEMBER_LINE.match(line)
                and line.strip() and not scopes.note_mark(line)):
            out[-1] = prev.rstrip() + " " + line.strip()
        else:
            out.append(line)
    return out


def _span_index(src) -> list[_SpanRow | list[_SpanSentence]]:
    """The body of a source parsed once for ``_cited_spans``, line by line (a wrapped sentence is one line, ``_unwrapped``): a table row (two
    cells or more) with a value cell that holds a digit, else the sentences of a passage line. A row without a digit in its value cells has no entry."""
    out: list[_SpanRow | list[_SpanSentence]] = []
    for line in _unwrapped(context_of_source(src)[1]):
        cells = _row_cells(line) if "|" in line else []
        if len(cells) >= 2:
            values = [c for c in cells[1:] if re.search(r"\d", c)]
            if values:
                blank = _outside_dates(" ".join(values))
                classes = tuple((frozenset(numbers_in(m.group(0))), _number_class(blank, m.start(), m.end())) for m in _NUMBER.finditer(blank))
                numbers = frozenset(numbers_in(blank))
                covered = {f for forms, _ in classes for f in forms}
                # a number the row states that no matched number holds (a form only the whole text reads) is a plain number: class ""
                classes += tuple((frozenset({f}), "") for f in numbers - covered)
                out.append(_SpanRow(line, cells[0], values[0], numbers, classes))
            continue
        sentences = []
        for sentence in _SENTENCE_END.split(line):
            blank = _outside_dates(sentence)
            # a number the sentence writes as a name (a plot, a plan, a permit, a footnote mark: ``_not_headline``) is kept apart: a figure matches a
            # unit by itself, a name only when the unit states every name of the sentence (``_cited_spans``; round 21 third pass, H3)
            figures, names = [], []
            for m in _NUMBER.finditer(blank):
                (names if _not_headline(blank, m.start(), m.end()) else figures).append(
                    (m.group(0), frozenset(numbers_in(m.group(0))), _number_class(blank, m.start(), m.end())))
            sentences.append(_SpanSentence(sentence, tuple(figures), tuple(names)))
        out.append(sentences)
    return out


def _cited_spans(index: Sequence[_SpanRow | list[_SpanSentence]], wanted: Mapping[str, set[str]]) -> Iterable[tuple[str, str, str, tuple[str, ...]]]:
    """(quote, label, value, hits) of each table row of the source (parsed by ``_span_index``) whose value cells state a number of ``wanted``,
    or each sentence of a passage that does: the spans an answer sentence stating those numbers draws on. ``wanted`` maps each form of the
    unit's numbers to their classes: a percent never states a count, nor a count a percent (round 21 packet A, Q4). ``hits``: every number of a
    sentence that the unit states, in the sentence's order (``value`` is the first); empty for a row."""
    for entry in index:
        if isinstance(entry, _SpanRow):
            if _states(entry.classes, wanted):
                yield _clip_row(entry.line), entry.label, entry.value, ()
            continue
        for sentence in entry:
            hits = [written for written, forms, cls in sentence.written if _states([(forms, cls)], wanted)]
            if not hits and sentence.names and all(forms & wanted.keys() for _, forms, _ in sentence.names):
                # matched by a name alone ("block 181"): the sentence is about the unit's thing only when the unit states every name the
                # sentence writes — "plot 200" beside "plot 301" of a neighbouring sentence is about the neighbour (round 21 third pass, H3)
                hits = [written for written, forms, cls in sentence.names if _states([(forms, cls)], wanted)]
            if hits:
                yield sentence.text.strip(), _first_words(sentence.text, trim=False), hits[0], tuple(hits)


def _component_of(asked: Sequence[Mapping], text: str) -> str:
    """The component a sentence gives: the one asked component, else the one whose words the sentence shares the most (the first
    on a tie); "" in a turn with none."""
    if not asked:
        return ""
    words = claim_terms(text)[1]
    return max(asked, key=lambda c: len(claim_terms(c.get("text") or "")[1] & words))["id"]


def _same_source(ws, a: str, b: str) -> bool:
    """Whether the sources ``a`` and ``b`` are one text: the same S#, or one is the re-read of the other (``Source.same_as``)."""
    first, second = ws.sources.get(a), ws.sources.get(b)
    return a == b or (first is not None and first.same_as == b) or (second is not None and second.same_as == a)


def _holds(item: Item, sid: str, ws, flat: str, component: str | None = None) -> bool:
    """Whether the ledger item already covers the span ``flat`` of source ``sid``: a quote that holds or is held by the span, in the same
    place — the same document and appraisal unit (the same source, or its re-read, for an item with no place of its own). Round 19 U3:
    with ``component``, only an item of that component holds it: one span serves several components, one fill each."""
    if component is not None and item.component != component:
        return False
    mine = _flat_span(item.quote)
    if not (mine in flat or flat in mine):
        return False
    place = _place(ws, sid)
    return place == (item.document_id, item.unit_id) if place is not None and item.document_id else _same_source(ws, item.source, sid)


def _related(sentence: str, text: str) -> bool:
    """Whether a source sentence and an answer sentence share a content word (one word's stem opening the other, three letters at least)."""
    # a stem keeps its letters when a suffix follows ("דגם" / "דגמים")
    mine, theirs = {w.translate(_FINAL_LETTERS) for w in claim_terms(sentence)[1]}, {w.translate(_FINAL_LETTERS) for w in claim_terms(text)[1]}
    return any(a.startswith(b) or b.startswith(a) for a in mine for b in theirs if min(len(a), len(b)) >= 3)


_OWN_PLACES = ("quote", "row")  # the places of a condition span that are the item's own quote or its table row, not the context around them


def _only_itself(spans: Sequence[Span], flat: str) -> bool:
    """Whether every condition span of an item is the item's own quote: the sentence limits itself and nothing else limits it."""
    def bare(text: str) -> str:
        return SupportScope.flat(text).strip(" .,;:")

    mine = bare(flat)
    return all(sp.place in _OWN_PLACES and mine in bare(sp.text) for sp in spans)


def source_items(units: Sequence[Unit], ws, components: Sequence[Mapping], ledger: Ledger) -> list[Item]:
    """The origin ``source`` items for the spans the answer cites (round 18 U5, KTD1): for each surviving unit and each source it
    cites, each table row or sentence stating a number of the unit whose own context carries a condition (``condition_spans``) and
    that no item of the ledger covers becomes an item whose label, value and condition the server builds from the source. A cited
    span with no condition makes none. The source must pass the checks the agent's items pass (``SupportScope``); at most
    ``ROWS_PER_UNIT`` rows of one source per unit."""
    scope = SupportScope(ws, list(units))
    asked = asked_components(components)
    held = list(ledger.items.values())
    built: list[Item] = []
    indexes: dict[str, list] = {}  # each cited source parsed once, whatever the number of units that cite it
    for unit in units:
        blank = _outside_dates(unit.text)
        wanted: dict[str, set[str]] = {}
        for m in _NUMBER.finditer(blank):
            for form in numbers_in(m.group(0)):
                wanted.setdefault(form, set()).add(_number_class(blank, m.start(), m.end()))
        if not wanted:
            continue
        sources = _cited_sources(unit, ws)
        referent = unit_referent(unit) if sources else {}  # what the unit is about, read once for all the spans it is compared with
        for sid, src in sources:
            made = 0
            if sid not in indexes:
                indexes[sid] = _span_index(src)
            for quote, label, value, hits in _cited_spans(indexes[sid], wanted):
                flat = SupportScope.flat(quote)
                component = _component_of(asked, f"{unit.text} {label}")
                # a fill of ANOTHER component on the span holds nothing for this one: the span serves each component (round 19 U3)
                # (a footnote of another contract, lot group or plan, or one whose mark is not found, is nobody's condition here: round 20 U3)
                if (made >= ROWS_PER_UNIT or scope.source_problem(sid, flat) or scope.place_problem(src)
                        or any(_holds(i, sid, ws, flat, component) for i in [*held, *built])):
                    continue
                spans = condition_spans(src, quote, [i.quote for i in held], value)
                own = [sp for sp in spans if sp.place in _OWN_PLACES] if hits else []
                if own:
                    # a clause of the item's own sentence limits the figures it holds: the clause of another figure of the sentence (the exclusion
                    # of the commerce area, beside the lot's area) is that figure's. The item's value is the first figure the unit states that
                    # such a clause holds; when none, the clause is not the item's and only its other spans are (round 21 third pass, H3)
                    inside = frozenset().union(*(numbers_in(sp.text) for sp in own))
                    figures = [h for h in hits if numbers_in(h) & inside]
                    if figures:
                        value = figures[0]
                    else:
                        spans = [sp for sp in spans if sp not in own]
                if not spans or _note_of_another_scope(ws, sid, src, quote, quote, against=referent):
                    continue
                if _only_itself(spans, flat) and not _related(quote, unit.text):
                    continue  # the figure coincides: the sentence is about another thing and its only condition is itself (round 21 packet A, Q4)
                made += 1
                document_id, unit_id = _site_of(src)
                item = Item(component, label, sid, quote, value,
                            " ; ".join(sp.text for sp in spans), ITEM_INCLUDED, "", ORIGIN_SOURCE,
                            document_id=document_id, unit_id=unit_id, block=block_of(src, quote))
                item.id = ledger.id_of(item)
                built.append(item)
    return built


# --- candidate items: the siblings of what the answer draws on (round 18 U6, KTD2; R5-R7) --------------------------------------

CANDIDATES_PER_SOURCE = 20  # the candidates of one anchor source one call offers, at most (the count left out is recorded)
CANDIDATE_SOURCES = 4  # the anchor sources one call offers candidates from, at most (the count left out is recorded)
FIRST_WORDS = 6  # the words of a sentence that name a candidate with a value
_MEMBER_LINE = re.compile(r"^\s*(?:[-*+•·–—]|\d{1,3}[.)]|[א-ת][.)])\s+")  # a list member's opening: a bullet, a dash, a number or a letter
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s")  # a heading line ends the paragraph run of a section's text


@dataclass
class Candidate:
    """One sibling of an anchor that the answer does not give: ``kind`` — ``row`` (a data row of a table), ``member`` (a member of an
    enumerated list), ``sentence`` (a value-bearing sentence of a passage) or ``note`` (a note of a table that states a value of its own).
    ``label``, ``value`` and ``condition`` are built from the source (a member without a number is its own value); ``own`` is its own
    text — the row, the header, the caption, the heading or the list's lead-in — which an assignment's request words must stand in."""

    id: str
    source: str
    quote: str
    label: str
    value: str
    condition: str
    own: str
    kind: str
    document_id: str = ""
    unit_id: str | None = None

    def listing(self) -> dict:
        """The candidate as the requirements call renders it (``id``, ``source``, ``label``, ``value``) and checks an assignment
        against it (``quote``, ``own``)."""
        return {"id": self.id, "source": self.source, "label": self.label, "value": self.value, "quote": self.quote,
                "own": self.own, "kind": self.kind}


class Offer(NamedTuple):
    """What one call offers: the candidates, the number left out of each anchor source by ``CANDIDATES_PER_SOURCE`` and the number of
    anchor sources left out by ``CANDIDATE_SOURCES``."""

    candidates: list[Candidate]
    left_out: dict[str, int]
    sources_left: int

    def record(self) -> dict:
        return {"offered": [c.id for c in self.candidates], "left_out": dict(self.left_out), "sources_left": self.sources_left}


class _Raw(NamedTuple):
    quote: str
    label: str
    value: str
    kind: str
    own: str
    group: str  # the table, the list or the paragraph run the span stands in: an anchor reaches the spans of its group


def _first_value(text: str) -> str:
    """The first number of ``text`` as it is written, outside dates and never an identifier, a footnote mark or an ordinal
    (``_not_headline``); "" when it has none."""
    blank = _outside_dates(text)
    return next((m.group(0) for m in _NUMBER.finditer(blank)
                 if numbers_in(m.group(0)) and not _not_headline(blank, m.start(), m.end())), "")


# a column header that names an identifier (a number, a plot, a file, a code): its cells state no amount
_IDENTIFIER_HEADER = re.compile(r"מס(?:פר|[׳'.])|מס[\"״]ד|גוש|חלקה|מגרש|תיק|תכנית|תוכנית|היתר|מזהה|קוד|סידורי|\bID\b|\bNo\b|#")


def _row_value(cells: Sequence[str], header: Sequence[str], known: Collection[str] = frozenset()) -> str:
    """The value cell of a table row: the cell after the label whose column header shares the most request words (``known``:
    ``request_word_forms`` of the asked components and the request) when the header lines up with the row, and holds a figure (round 20
    U5, KTD5, R17; an identifier column is never it); else the first cell after the label that holds a figure and is neither under an
    identifier header (when the header lines up with the row) nor an identifier, a footnote mark or an ordinal itself (round 19 U4); the
    first cell with a digit when no cell does."""
    aligned = len(header) == len(cells)
    values = [(i, c) for i, c in enumerate(cells) if i and re.search(r"\d", c)]
    if aligned and known:
        shared = [(shared_request_words(header[i], known), c) for i, c in values if not _IDENTIFIER_HEADER.search(header[i]) and _first_value(c)]
        best = max((n for n, _ in shared), default=0)
        if best and sum(1 for n, _ in shared if n == best) == 1:  # one column names the request most; a tie decides nothing
            return next(c for n, c in shared if n == best)
    for i, cell in values:
        if not (aligned and _IDENTIFIER_HEADER.search(header[i])) and _first_value(cell):
            return cell
    return values[0][1] if values else ""


def _level_cells(line: str, header: Sequence[str]) -> list[str]:
    """The cells of a header line of a table text, lined up with the level before it (``header``) when that is possible. The text of a
    table renders a row as its cells joined by " | ", so a line that starts or ends with a pipe has an EMPTY edge cell ("| חודשית | שנתית":
    the first column has no second-level header) - ``_row_cells`` reads that pipe as the outer pipe of a Markdown row and drops the cell,
    which shifts every later cell one column left. A line with a pipe at both edges is read both ways, and the reading that has as many cells
    as the earlier level wins; with no earlier level to line up with, a pipe at both edges is Markdown's, and a pipe at one edge is an empty
    cell (round 20 U5, KTD5, R17)."""
    flat = line.strip()
    full = [c.strip() for c in flat.split("|")]
    outer = _row_cells(line)
    if header:
        return next((cells for cells in (full, outer) if len(cells) == len(header)), outer)
    return outer if flat.startswith("|") and flat.endswith("|") else full


def _merge_header(header: list[str], cells: Sequence[str]) -> list[str]:
    """The header lines of a table as one row of column headers: a second level is merged column by column ("הכנסה" over "חודשית" is "הכנסה
    חודשית"), each column once, when it lines up with the level before it; else the cells follow the earlier ones (round 20 U5, KTD5; R17)."""
    if not header:
        return list(cells)
    if len(cells) != len(header):
        return [*header, *cells]
    return [" ".join(dict.fromkeys(part for part in (a, b) if part)) for a, b in zip(header, cells, strict=True)]


def _first_words(text: str, *, trim: bool = True) -> str:
    """The first ``FIRST_WORDS`` words of a sentence or list member, the label of a span with a value; ``trim``: without the punctuation
    that closes them."""
    words = " ".join(text.split()[:FIRST_WORDS])
    return words.rstrip(".:;,") if trim else words


def _named(text: str) -> tuple[str, str]:
    """(label, value) of a sentence or list member: its first words and its number; a span without a number is its own value."""
    number = _first_value(text)
    return (_first_words(text), number) if number else ("", _MEMBER_LINE.sub("", text).strip())


def _context_words(src, parts) -> str:
    """The text of the headings and captions of a source (and its section): what every span of it is about."""
    return " ".join([*(p.text for p in parts if p.place in ("heading", "caption")), _text(src.section)])


def _raws(src, known: Collection[str] = frozenset()) -> tuple[list[_Raw], dict[str, list[str]]]:
    """The spans of a source that can be an item, and the lines of each group (``_Raw.group``). A table: the data rows (a first cell,
    and a value cell with a number) and the sentences of its attributed notes that state a number and limit nothing. A passage: the
    members of each enumerated list (two lines or more in a row that open with a list mark) and the sentences with a number of the
    other lines, a heading line ending the paragraph run. Round 20 U5 (KTD5, R17): the header lines of a table are merged column by column,
    and a row's value is the cell of the column whose header shares the most request words (``known``)."""
    parts, body = context_of_source(src)
    about = _context_words(src, parts)
    raws: list[_Raw] = []
    groups: dict[str, list[str]] = {}
    if _is_tabular(src):
        header: list[str] = []
        for line in body:
            cells = _row_cells(line) if "|" in line else []
            if len(cells) < 2:
                continue
            values = [c for c in cells[1:] if re.search(r"\d", c)]
            if not values:
                if not raws:
                    header = _merge_header(header, _level_cells(line, header))
                continue
            if numbers_in(_outside_dates(" ".join(values))):
                quote = _clip_row(line)
                raws.append(_Raw(quote, cells[0], _row_value(cells, header, known), "row", f"{quote} {' '.join(header)} {about}", "table"))
        for part in parts:
            if part.place in ("before", "after", "note") and not part.reason and not part.completed:  # a completed note was never shown: no item of its own
                for sentence in _SENTENCE_END.split(part.text):
                    number = _first_value(sentence)
                    if number and not conditions.material(sentence):  # a note that limits a value is that value's condition span
                        raws.append(_Raw(sentence.strip(), _first_words(sentence), number, "note",
                                         f"{sentence} {about}", "table"))
        groups["table"] = list(body)
        return raws, groups
    run, listing, lead = 0, 0, ""
    for at, line in enumerate(body):
        if _HEADING_LINE.match(line):
            run += 1
            continue
        group = f"run{run}"
        groups.setdefault(group, []).append(line)
        if _MEMBER_LINE.match(line) and ((at + 1 < len(body) and _MEMBER_LINE.match(body[at + 1])) or (at > 0 and _MEMBER_LINE.match(body[at - 1]))):
            if not (at > 0 and _MEMBER_LINE.match(body[at - 1])):
                listing += 1
            name = f"list{run}.{listing}"
            groups.setdefault(name, []).append(line)
            label, value = _named(line)
            raws.append(_Raw(line.strip(), label, value, "member", f"{line} {lead} {about}", name))
            continue
        lead = line
        for sentence in _SENTENCE_END.split(line):
            number = _first_value(sentence)
            if number:
                raws.append(_Raw(sentence.strip(), _first_words(sentence), number, "sentence",
                                 f"{sentence} {about}", group))
    return raws, groups


def candidates(units: Sequence[Unit], ws, ledger: Ledger, components: Sequence[Mapping], request: str = "") -> Offer:
    """The candidates one requirements call is offered (round 18 U6, KTD2): for each anchor source — the source of a ledger item, and a
    source the surviving units cite — the spans of its structure (``_raws``) that no unit gives (``_coverage``: attribution, value and
    date) and no ledger item holds. A table is an anchor whole; the list or paragraph run of a passage only when one of its spans is
    given or held (the siblings of what the answer draws on). The source must pass the checks the agent's items pass (``SupportScope``:
    a returned source, a document the answer is about, an asked appraisal unit — a span of another unit of the file is never offered),
    and a span is offered only when a content word of its own text stands in an asked component or the request (an assignment
    could never be kept otherwise). At most ``CANDIDATES_PER_SOURCE`` per source in the source's order, ``CANDIDATE_SOURCES`` sources;
    the counts left out are in the offer. No component is needed before the call."""
    scope = SupportScope(ws, list(units))
    asked = asked_components(components)
    leads = _leads(units)
    blocks = blocks_of(units)
    known = request_word_forms(" ".join([*(c.get("text") or "" for c in asked), request]))
    held = list(ledger.items.values())
    anchors: dict[str, None] = {}
    for item in held:
        if item.source in ws.sources:
            anchors.setdefault(item.source)
    for unit in units:
        for sid, _ in _cited_sources(unit, ws):
            anchors.setdefault(sid)
    offered: list[Candidate] = []
    left_out: dict[str, int] = {}
    used = 0
    sources_left = 0
    for sid in anchors:
        src = ws.sources[sid]
        if src.is_listing or sid not in scope.readable or scope.place_problem(src):
            continue
        raws, groups = _raws(src, known)
        if not raws:
            continue
        if used >= CANDIDATE_SOURCES:
            sources_left += 1
            continue
        flats = [SupportScope.flat(r.quote) for r in raws]
        spans = [Item("", r.label, sid, r.quote, r.value) for r in raws]
        signatures = Counter(_signature(i) for i in spans)
        touched: set[str] = set()
        free: list[int] = []
        for n, (raw, flat, span) in enumerate(zip(raws, flats, spans, strict=True)):
            sig = _signature(span)
            shared = bool(sig) and signatures[sig] > 1
            if any(_holds(h, sid, ws, flat) for h in held) or _coverage(span, units, ws, shared, leads, blocks).ok:
                touched.add(raw.group)
            else:
                free.append(n)
        mine = [SupportScope.flat(h.quote) for h in held if _same_source(ws, h.source, sid)]
        for name, lines in groups.items():
            if any(q in _flat_span(line) for q in mine for line in lines):
                touched.add(name)
        found = [n for n in free if (raws[n].group == "table" or raws[n].group in touched)
                 and not scope.source_problem(sid, flats[n]) and scope.verbatim(sid, src, flats[n])
                 and shares_request_word(raws[n].own, known)]
        if not found:
            continue
        used += 1
        document_id, unit_id = _site_of(src)
        for n in found[:CANDIDATES_PER_SOURCE]:
            raw = raws[n]
            offered.append(Candidate(ledger.candidate_id(evidence_key(document_id, unit_id, sid, flats[n])), sid, raw.quote, raw.label, raw.value,
                                     " ; ".join(sp.text for sp in condition_spans(src, raw.quote, value=raw.value)), " ".join(raw.own.split()),
                                     raw.kind, document_id, unit_id))
        if len(found) > CANDIDATES_PER_SOURCE:
            left_out[sid] = len(found) - CANDIDATES_PER_SOURCE
    return Offer(offered, left_out, sources_left)


def candidate_item(c: Candidate, component: str, ledger: Ledger) -> Item:
    """The ledger item of a candidate a frozen component asks for: origin ``source`` and ``candidate``, its label, value and condition
    as the server built them from the source (nothing the model wrote), state ``included`` so that ``Ledger.outcomes`` decides whether a
    surviving unit gives it."""
    item = Item(component, c.label, c.source, c.quote, c.value, c.condition, ITEM_INCLUDED, "", ORIGIN_SOURCE,
                document_id=c.document_id, unit_id=c.unit_id, candidate=True)
    item.id = ledger.id_of(item)
    return item


# --- coverage by attribution (R5) --------------------------------------------------------------------------------------------

def _site_of(src) -> tuple[str, str | None]:
    """(document, appraisal unit id) of a source as the item records them; the unit is None in a file without unit marks."""
    return str(src.document_id), (src.unit or {}).get("id")


def _place(ws, sid: str) -> tuple[str, str | None] | None:
    """``_site_of`` the source ``sid``; None for a source the turn does not hold or one without a document."""
    src = ws.sources.get(sid)
    return None if src is None or src.document_id is None else _site_of(src)


def _value_place(v) -> tuple[str, str | None]:
    return str(v.document_id), (v.context or {}).get("key")


def _same_place(item_place: tuple[str, str | None], other: tuple[str, str | None]) -> bool:
    """The cited text is in the item's document and, when the item lies in an appraisal unit of a file of several, in that unit."""
    return item_place[0] == other[0] and (item_place[1] is None or item_place[1] == other[1])


def _leaves(ws, cid: str) -> list[str]:
    c = ws.computations.get(cid)
    return list(getattr(c, "leaves", None) or []) if c is not None else []


_SUM_DIGITS = 4  # a number of this many digits (a sum, an area, a price) is the item's own datum in another passage of its unit


def _holds_value(item: Item, src) -> bool:
    """Whether the passage ``src`` of the item's document and unit holds the item's value: every headline value of the item, each written with
    four digits or more (round 21 U7, KTD7, R8). One datum can stand under two quotes (a row of the main table and the sum of a notes table),
    and the sentence that cites the one gives the item; a short figure ("12", "6.5%") is common to many data and attributes nothing."""
    groups = headline(item.value, item.quote)
    if not groups or not all(any(f.isdigit() and len(f) >= _SUM_DIGITS for f in g) for g in groups):
        return False
    have = _have(src.text or "")
    return all(g & have for g in groups)


def _attributed(item: Item, unit: Unit, ws) -> bool:
    """Whether the unit cites the item's source, or a value or calculation registered from it, or another passage of the item's
    document and unit that holds its quote or its value (``_holds_value``) — and nothing of another document or appraisal unit."""
    home = _place(ws, item.source)
    if home is None:
        return False
    flat = SupportScope.flat(item.quote)
    for cid in unit.ids:
        if cid in ws.sources:
            src = ws.sources[cid]
            place = _place(ws, cid)
            if place is None or not _same_place(home, place):
                continue
            if _same_source(ws, cid, item.source) or flat in _flat_span(src.text or "") or _holds_value(item, src):
                return True
        elif cid in ws.values:
            v = ws.values[cid]
            if v.source_id == item.source and _same_place(home, _value_place(v)):
                return True
        elif cid in ws.computations:
            for leaf in _leaves(ws, cid):
                v = ws.values.get(leaf)
                if v is not None and v.source_id == item.source and _same_place(home, _value_place(v)):
                    return True
    return False


def _cites_source(item: Item, unit: Unit, ws) -> bool:
    """Whether the unit cites the item's own source itself (round 21 full measurement): the S# (or its re-read), a value V# registered from it, or a
    calculation C# one of whose leaves is such a value. Narrower than ``_attributed``, which also takes another passage of the item's document and
    unit that holds its quote or value: a sentence that cites only that passage gives the same figure from another source."""
    for cid in unit.ids:
        if cid in ws.sources:
            if _same_source(ws, cid, item.source):
                return True
        elif cid in ws.values:
            if _same_source(ws, ws.values[cid].source_id, item.source):
                return True
        elif cid in ws.computations:
            if any((v := ws.values.get(leaf)) is not None and _same_source(ws, v.source_id, item.source) for leaf in _leaves(ws, cid)):
                return True
    return False


def _outside_dates(text: str) -> str:
    """``text`` with its dates blanked: a day or month number of a date states no value."""
    for p in sorted(shared_dates.find_all(text), key=lambda x: x.span[0], reverse=True):
        text = text[:p.span[0]] + " " + text[p.span[1]:]
    return text


_ASIDE = re.compile(r"\([^()]*\)")
_DATING_ASIDE = re.compile(r"\(\s*(?:נכון|כמצב|לתאריך|ליום|בתאריך|ביום|מיום)")  # a parenthesis that opens with a word of dating: "(נכון ליום …)"


def _dating_dates(quote: str):
    """The dates of a quote that date what it says: those outside a parenthetical aside made of words and dates alone ("(אשר נפתח … 2021)" tells
    when a building opened, not when the figure beside it holds), unless the aside opens with a word of dating or holds a number of its own (round
    21 third pass, H3: a source sentence wrapped over two lines is one sentence now, and its aside is in the quote)."""
    asides = [(m.start(), m.end()) for m in _ASIDE.finditer(quote) if not _DATING_ASIDE.match(m.group())
              and not numbers_in(_outside_dates(m.group()))]
    return [p for p in shared_dates.find_all(quote) if not any(a <= p.span[0] and p.span[1] <= b for a, b in asides)]


@functools.lru_cache(maxsize=2048)
def _dates_of(is_source: bool, label: str, value: str, condition: str, quote: str) -> tuple[str, ...]:
    """``_item_dates`` as a function of the strings it reads (an item's date words are read many times: the ledger asks them of the same
    item for every sentence)."""
    if is_source:  # the server's label and condition are clips of the quote: they carry its asides too
        own = [p for text in (label, value, condition) for p in _dating_dates(text)]
    else:
        own = shared_dates.find_all(f"{label} ; {value} ; {condition}")
    if own:
        return tuple(dict.fromkeys(p.iso for p in own))
    quoted = _dating_dates(quote)
    return (quoted[0].iso,) if len(quoted) == 1 else ()


def _item_dates(item: Item) -> list[str]:
    """The dates (ISO) the item has: those in its own words (label, value, condition); when none, the one date of its quote."""
    return list(_dates_of(item.origin == ORIGIN_SOURCE, item.label, item.value, item.condition, item.quote))


def _signature(item: Item) -> frozenset[str]:
    """What an item is told apart by from its value: its numbers, else its value's words, else its label's."""
    for text in (item.value, item.label):
        nums = numbers_in(_outside_dates(text))
        if nums:
            return frozenset(nums)
        words = claim_terms(text)[1]
        if words:
            return frozenset(words)
    return frozenset()


# a word, with the abbreviations that carry a quote mark inside (בע"מ, בע״מ, סה"כ, מס'): one token, the way the block's side reads it (round 21 U7)
_TOKEN = re.compile(r"[^\W\d_]+(?:[\"״'׳][^\W\d_]+)*")
# a plan, permit or file identifier writes letters and digits around a slash ("ג/4521", "בי/1/483", "483/2"): it is no content word
_CODE = re.compile(r"[א-תA-Za-z]{1,4}\s?[/\\]\s?\d[\d/\\.\-]*|\d+(?:\s?[/\\]\s?\d+)+")


# the maqaf (U+05BE) and the Unicode hyphens (U+2010, U+2011) that join a Hebrew compound: ``claim_terms`` reads the maqaf as a mark of
# the text (the search normaliser drops it) and so joins the compound into one word, while ``_TOKEN`` splits at it, so the two sides of a
# comparison read the compound differently (round 20 U5, KTD5, R17)
_MAQAF = re.compile("[־‐‑]")


def _unjoined(text: str) -> str:
    """``text`` with the maqaf and the Unicode hyphens read as a space, the way the ASCII hyphen is read: a compound is its words on BOTH sides
    of a comparison (``_word_groups`` of an item's condition, ``_words`` of a block of the answer), never one word on one side and two on the
    other (round 20 U5, KTD5; R17)."""
    return _MAQAF.sub(" ", text)


@functools.lru_cache(maxsize=2048)
def _word_groups(wanted: str) -> tuple[frozenset[str], ...]:
    """The content words of ``wanted`` (an identifier written with a slash left out), one group of variants per word. A compound joined by a
    maqaf is its words (``_unjoined``)."""
    return tuple(g for t in _TOKEN.findall(_CODE.sub(" ", _unjoined(wanted))) if (g := claim_terms(t)[1]))


@functools.lru_cache(maxsize=2048)
def _words(text: str) -> frozenset[str]:
    """The content words of ``text``, with their variants; a compound joined by a maqaf is its words (``_unjoined``)."""
    return claim_terms(_unjoined(text))[1]


def _enough(wanted: str, have: frozenset[str]) -> bool:
    """At least half (rounded up) of the content words of ``wanted`` are stated in ``have`` (the content words of the block,
    with their variants: ``claim_terms``). A word counts once, whichever of its variants the block writes, so a spelling
    variant does not count twice; a text with no content word asks for nothing."""
    groups = _word_groups(wanted)
    return not groups or sum(1 for g in groups if g & have) >= (len(groups) + 1) // 2


def _condition_dates(item: Item) -> frozenset[str]:
    """The dates (ISO) the item states: what tells it apart from a twin that states other dates (round 19 U4). Its own words — condition,
    label and, since round 21 G2c, its value — and, when it writes none, the one date of its quote (``_item_dates``): "33 years, 2012 to
    2045" is told from "33 years" of the extension by its value's date, and "9.75% on 3.8.2031" from "9.75% on 3.8.2030" by its quote's."""
    return frozenset(_item_dates(item))


def _tells_apart(item: Item, have: frozenset[str], rivals: frozenset[str], rival_dates: frozenset[str] = frozenset()) -> bool:
    """Whether the block's words tell the item apart from the items that share its value: at least half of the words of its condition (else
    its label) that none of those items writes (``rivals``: their words). A condition that every twin writes tells nothing apart, so the
    label's words are asked, and when no word of either differs nothing is asked (round 19 U4). A date is no word (``_TOKEN`` reads letters),
    so twins that differ only by their date are told apart by it: when the item states a date that none of its twins states
    (``rival_dates``), the date tells it apart, and ``_states_date`` has already asked the block for it (KTD4; R13-R16)."""
    if (own := _condition_dates(item)) and not own <= rival_dates:
        return True
    for text in (item.condition, item.label):
        if not text:
            continue
        if not (named := _word_groups(text)):
            return True  # a text with no content word asks nothing
        if groups := [g for g in named if not g & rivals]:
            return sum(1 for g in groups if g & have) >= (len(groups) + 1) // 2
    return True


def _numbers_with_scale(text: str, *, source: bool = False) -> list[tuple[int, int, frozenset[str]]]:
    """Every number of ``text`` as (start, end, forms), the way a document writes it (``numread``: a comma by its context, a sign), and,
    written with a scale word, with the forms of its full value too ("16.07 מיליון" and "16,070,000" are one number: round 18 U7). The
    full value is scaled from the forms the reader already produced, never from the written slice: "13,2 מיליון" is 13.2 x a million,
    not the digits "132" x a million. A signed form keeps its unsigned scaled value only (the number's forms already hold its sign)."""
    from app.chat.calc import scale_after

    out = []
    for start, end, forms in number_spans(text, source=source):
        scale = scale_after(text, start, end)[0]
        if scale != 1:
            scaled: set[str] = set()
            for form in forms:
                if form.startswith("-"):
                    continue
                try:
                    scaled |= numbers_in(format(Decimal(form) * scale, "f"))
                except InvalidOperation:
                    pass
            forms = forms | scaled
        out.append((start, end, forms))
    return out


@functools.lru_cache(maxsize=2048)
def _number_forms(text: str) -> tuple[frozenset[str], ...]:
    """The forms of each number ``text`` writes, outside its dates. A function of the text alone, kept per text: the ledger reads the
    same sentence against every item."""
    return tuple(forms for _, _, forms in _numbers_with_scale(_outside_dates(text)))


@functools.lru_cache(maxsize=2048)
def _have(text: str) -> frozenset[str]:
    """Every form of every number ``text`` writes, outside its dates."""
    return frozenset().union(*_number_forms(text))


# a range of lots, floors or units ("61-65", "1 עד 3"): the numbers inside it are stated too (round 21 G1)
_RANGE = re.compile(r"(?<![\d.,/:\-–־—])(\d{1,4})\s*(?:[-–־—]|\s+עד\s+)\s*(\d{1,4})(?![\d.,/:\-–־—%])")
RANGE_MAX = 60  # a range wider than this is a span of years or an amount, not an enumeration


@functools.lru_cache(maxsize=2048)
def _ranged(text: str) -> frozenset[str]:
    """The numbers inside the ranges ``text`` writes ("61-65" states 62, 63 and 64 too), outside its dates: a quote that enumerates
    "61, 62, 63, 64, 65" is stated by the answer's "61-65" (round 21 G1)."""
    out: set[str] = set()
    for m in _RANGE.finditer(_outside_dates(text)):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b <= a + RANGE_MAX and a < 1900:
            out.update(str(n) for n in range(a, b + 1))
    return frozenset(out)


def _stray(group: frozenset[str]) -> bool:
    """Whether a headline value is a bare number of one or two digits ("2", "12"): a count that many rows of a table share."""
    return any(f.isdigit() and len(f) <= 2 for f in group)


def _stated_dates(dates: Sequence[str], text: str) -> bool:
    """Whether ``text`` states every date of a quote (``dates``: their ISO forms)."""
    stated = [p.iso for p in shared_dates.find_all(text)]
    return all(any(s.startswith(d) for s in stated) for d in dates)


@functools.lru_cache(maxsize=2048)
def _figures_plan(quote: str) -> tuple[tuple[frozenset[str], ...], tuple[str, ...]] | None:
    """What ``figures_stated`` asks of a quote, a function of the quote alone (kept per quote: the same quote is checked against every
    sentence): its headline values and the ISO forms of its dates; None when its figures are too few to tell it from another passage
    (no date, fewer than three values, fewer than two that are no bare small number)."""
    groups = headline(quote, quote)
    dates = tuple(p.iso for p in shared_dates.find_all(quote))
    if not (dates or len(groups) >= 3 or sum(1 for g in groups if not _stray(g)) >= 2):
        return None
    return groups, dates


def figures_stated(quote: str, text: str) -> bool | None:
    """Whether ``text`` states a quote by its figures and dates alone (round 21 G1): None when the quote's figures are too few to tell it
    from another passage of the answer (no date, fewer than two amounts, fewer than three numbers) — its words then decide; else whether
    every figure of the quote (ranges and enumerations expanded to the same set) and every date of it stands in ``text``. A judge's
    quote ("9.75% ... 3.8.2031", "lots 61, 62, 63, 64, 65 ...") is written in the source's words and the answer in its own, so the
    words half of a quote that is its figures says nothing about it."""
    plan = _figures_plan(quote)
    if plan is None:
        return None
    groups, dates = plan
    text = positive_text(text, designators(quote), False, *_own_negation((quote,)))  # round 22 U6: only a clause that states them positively, for the quote's own designators
    if dates and not _stated_dates(dates, text):
        return False
    have = _have(text) | _ranged(text)
    return all(g & have for g in groups)


# --- the positive statement of a datum (round 22 U6, KTD6; R16, R17) -------------------------------------------------------------------
#
# A block states an item's datum when a CLAUSE of it holds the value or the words with no negation or absence on them, and the clause is not the
# row of another item. The clause is the unit (``normalize_text.clause_spans``): «דמי הניהול בבניין א לא צוינו; בבניין ב הם 1,200 ₪» is two
# clauses, the first says A's fee is absent, the second states B's. What the block states positively is its text with the clauses that state
# an absence, the words a negation governs («אינם 1,200 ₪») and the clauses of another designator's row blanked out (``positive_text``); every
# check of coverage reads that text, never the block's raw words. A single letter or number next to a noun that names a plot, a building or a lot
# is a DESIGNATOR («בניין א», «תא שטח 2»; the nouns of places only - never a document, a plan or a section, whose code is its name): it is coverage-local, so the search stopwords stay as they are; an item whose label writes
# one is stated only by a clause that writes it, or by one that writes no designator of that noun at all (a clause that says «הם 1,200 ₪»
# under the heading «בניין א»), never by a clause that writes another.

_DESIGNATOR_NOUNS = (r"תא[\s-]+שטח|תת[\s-]*חלקה|מגרש|בניין|מבנה|חלקה|גוש|יחידה|דירה|קומה|אגף|כניסה|שלב|מתחם|אתר|נכס|חנות|משרד|מחסן|מגדל|בית|"
                     r"חדר|קבוצה|אשכול|פרויקט")
_DESIGNATOR = re.compile(
    rf"(?<![א-ת])[ובהלמש]{{0,2}}(?P<noun>{_DESIGNATOR_NOUNS})[׳']?\s*(?:מס(?:פר|[׳'.])?\s*)?[:\-–]?\s*"
    r"(?P<token>[A-Za-z](?![A-Za-z])|\d{1,4}(?![\d]|[.,]\d)(?!\s*(?:₪|ש[\"״]ח|מ[\"״][רק]|דונם|%|אלף|מיליון|מטר))|[א-ת][א-ת׳']*)"
    r"(?!\s?[/\\]\s?\d)")  # (a plan or file code - «ג/4521», «12/3» - names a document or a sub-parcel, not a row)


def _designator(m: re.Match) -> tuple[str, str] | None:
    token = m.group("token").rstrip("׳'")
    if token.isdigit():
        token = str(int(token))
    elif len(token) > 1:  # a number written in words («שתיים»); any other word is no designator
        if (value := whole_number_word(token)) is None:
            return None
        token = str(value)
    return re.sub(r"[\s-]", "", m.group("noun")), token.casefold()


def _designator_matches(text: str):
    """(match, designator) of each designator phrase of ``text``. A noun followed by an ordinary word is no designator, and the word is not
    swallowed with it: «משטח החלקה ובחלקה 57» holds the designator of plot 57 (the second noun is not consumed as the first one's token)."""
    pos = 0
    while (m := _DESIGNATOR.search(text, pos)) is not None:
        d = _designator(m)
        if d is not None:
            yield m, d
            pos = m.end()
        else:
            pos = m.end("noun")


@functools.lru_cache(maxsize=4096)
def designators(text: str) -> frozenset[tuple[str, str]]:
    """The designators ``text`` writes: a single letter, a number of up to four digits or a number written in words, right after a noun
    that names a plot, a building, a unit or a lot, as (noun without its prefix letters, token). A letter in brackets, a list number and a
    number that has a unit after it are none."""
    return frozenset(d for _, d in _designator_matches(text))


def without_designators(text: str) -> str:
    """``text`` without its designator phrases (a noun, its prefix and its letter or number): the words that are left are the text's own."""
    out, last = [], 0
    for m, _ in _designator_matches(text):
        out += [text[last:m.start()], " "]
        last = m.end()
    return "".join(out) + text[last:]


def _foreign(own: frozenset[tuple[str, str]], found: frozenset[tuple[str, str]]) -> bool:
    """Whether a clause that writes the designators ``found`` speaks of another row than the item whose designators are ``own``: it writes
    a designator of a noun the item also writes, and none of the item's tokens of that noun."""
    for noun in {n for n, _ in own}:
        theirs = {t for n, t in found if n == noun}
        if theirs and not theirs & {t for n, t in own if n == noun}:
            return True
    return False


@functools.lru_cache(maxsize=4096)
def positive_text(text: str, own: frozenset[tuple[str, str]] = frozenset(), whole_clause: bool = False,
                  own_negations: frozenset[str] = frozenset(), own_absence: bool = False) -> str:
    """``text`` as far as it STATES positively (KTD6): the clauses that state an absence, the words a negation governs and, for an item that writes
    designators (``own``), the clauses of another designator's row are replaced by blanks. The rest of the text, its figures and its dates,
    is where it was. ``whole_clause``: a clause with a negation is blanked whole, not only the words the negation governs - for a part of a
    request that names no value of its own («מועד סיום למגרש א»), the clause that negates anything says nothing positive of it. A negation is a negation of the datum unless
    the datum itself carries it: ``own_negations`` are the negations the item's own quote writes («לא הוגשו הצעות», «שאינם מושכרים»), and
    ``own_absence`` says its quote is an absence statement itself; an answer that says the same is stating it, not negating it."""
    chars = list(text)

    def blank(a: int, b: int) -> None:
        chars[a:b] = " " * (b - a)

    for a, b in clause_spans(text):
        clause = text[a:b]
        if (absence_clause(clause) and not own_absence) or (own and _foreign(own, designators(clause))):
            blank(a, b)
            continue
        spans = negated_spans(clause, own_negations)
        if whole_clause and spans:
            blank(a, b)
            continue
        for x, y in spans:
            blank(a + x, a + y)
    return "".join(chars)


def _own_designators(item: Item) -> frozenset[tuple[str, str]]:
    """The designators of the row an item is: its label's, else its quote's. Never its condition's: that is the agent's wording about what limits
    the datum («שיוכו לחלקה 56 לא מאומת»), where a designator may stand under a negation."""
    return designators(item.label) or designators(item.quote)


@functools.lru_cache(maxsize=2048)
def _own_negation(texts: tuple[str, ...]) -> tuple[frozenset[str], bool]:
    """The negations the item's own words write and whether they state an absence: the datum's own (round 22 U6 fix)."""
    joined = "\n".join(texts)
    return negations_in(joined), holds_absence(joined)


def _positive(item: Item, text: str) -> str:
    """The text of a block as it states the ``item`` positively (``positive_text``), without its list number. The negations and the absence the
    item's own quote, value, label or condition write are its datum's («לא הוגשו הצעות במכרז» is what the item says), never counted against it."""
    negations, absence = _own_negation((item.label, item.condition, item.quote, item.value))
    return positive_text(_without_list_number(text), _own_designators(item), False, negations, absence)


_HEADING_MARK = re.compile(r"\A\s*(?:#{1,6}\s|(?:[א-ת]|\d{1,2})[.)]\s|\d{1,2}(?:\.\d{1,3})+\.?\s)")
HEADING_WORDS = 10  # a heading line has few words


def heading_only(quote: str) -> bool:
    """Whether a quoted part of a source is only a heading line (round 21 G1): one line of at most ``HEADING_WORDS`` words that is marked
    as a heading — a markdown mark, a section number or letter ("ד.", "3.2"), or a closing colon — that is no sentence (it holds no negation or absence and does not end like a sentence: round 22 U6) and states no amount and no date
    (the digits of a lot list "4-6, 9" or of the heading's number are not one). A heading names what follows it; the part that follows
    is the item, never the line, so a heading is no part of the asked kind that the answer omits."""
    text = quote.strip()
    if not text or "\n" in text or len(_TOKEN.findall(text)) > HEADING_WORDS:
        return False
    if not (_HEADING_MARK.match(text) or text.endswith(":")):
        return False
    # round 22 U6 (KTD6): a line that says something is a sentence, not a heading - it negates or states an absence («3.2 מועד סיום לא נקבע»),
    # or it is closed like a sentence («ד. מועד הסיום הוסכם בין הצדדים.»)
    if absence_clause(text) or negated_spans(text) or (not text.startswith("#") and re.search(r"[.!?]\s*$", text)):
        return False
    return not shared_dates.find_all(text) and all(_stray(g) for g in headline(text, text))


# a count written as a word ("שתי קומות", two floors) is the digit the source writes ("2 קומות"), when it stands directly before the noun the
# source writes after the digit (round 20 U5, KTD5; R17): "one", "two" and the like are ordinary words elsewhere. The words are read by
# ``numread.number_words`` (round 22 U6/U10: "שנים עשר תשלומים" is 12, not a 2 and a 10), a claim's reading: no "one", no definite count
_NOUN_AFTER_WORD = re.compile(r"[ \t]+([^\W\d_]+)")


@functools.lru_cache(maxsize=2048)
def _counts_before_nouns(text: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """For each count word (a phrase: «שנים עשר») of ``text`` that is directly followed by a word (only blanks between them), the count (as a
    digit string) and the words of the noun it stands before."""
    joined = _unjoined(text)
    return tuple((str(w.value), _words(m.group(1))) for w in number_words(joined, claim=True)
                 if (m := _NOUN_AFTER_WORD.match(joined, w.end)))


@functools.lru_cache(maxsize=2048)
def _noun_after(count: str, value: str, quote: str) -> frozenset[str]:
    """The words of the noun the item's value (else its quote) writes right after the digit ``count`` ("2 קומות"); none when it writes none."""
    pattern = re.compile(rf"(?<![\d.,]){count}(?![\d.,%])[ \t]+([^\W\d_]+)")
    for text in (value, quote):
        if m := pattern.search(_unjoined(text)):
            return _words(m.group(1))
    return frozenset()


def _stated(item: Item, text: str, have: frozenset[str], group: frozenset[str]) -> bool:
    """Whether ``text`` states the headline value ``group`` of the item: a form of it among the numbers ``text`` writes (``have``), or, for a
    count, the count written as a word right before the noun the item's source writes after the digit (round 20 U5, KTD5)."""
    if group & have:
        return True
    counted = [d for d in group if d.isdigit() and int(d) >= 2]
    return bool(counted) and any(n == d and (nouns := _noun_after(d, item.value, item.quote)) and nouns & after
                                 for d in counted for n, after in _counts_before_nouns(text))


# --- the headline values of an item (round 19 U4, KTD4; R14) ------------------------------------------------------------------

# a plan, a permit or a file named before a number: the number is its identifier, unless a quantity follows it
_PLAN_BEFORE = re.compile(r"(?<![א-ת])[בלמהו]?(?:תכנית|תוכנית|תב[\"״]?ע|תמ[\"״][אמל]|היתר|הקלה|תיק|חוזה|רישיון|תעודה|מכרז)[׳']?\s*"
                          r"(?:מס(?:פר|[׳'.])?\s*)?[:\-–]?\s*$")
_CODE_BEFORE = re.compile(r"(?:[א-תA-Za-z]{1,4}|\d)\s?[/\\]\s?$")  # "ג/" or "בי/" or "1/" right before the number
_CODE_AFTER = re.compile(r"\s?[/\\]\s?\d")
_MARK_AFTER = re.compile(r"\s*[)\]]")
_MARK_BEFORE = re.compile(r"[(\[]\s*$")
_ORDINAL_AT_START = re.compile(r"\s*(?:[-*+•]\s+)?\d{1,3}[.)]\s")
_LIST_NUMBER = re.compile(r"\A\s*(?:[-*+•]\s+)?\d{1,3}[.)]\s")  # the number a numbered list item opens with: its place in the list


# a heading's numbering at the start of a text ("4.2 תנאי תשלום", "## 4.2.1 …"): dotted groups, then a word. The number names the section and states
# no amount - unless a unit, a currency or a duration follows it ("3.5 שנים", "4.5 מ״ר"; round 21 U7, KTD7)
_SECTION_NUMBER = re.compile(r"\s*(?:#{1,6}\s+|[-*+•]\s+)?(\d{1,3}(?:\.\d{1,3})+)\.?(?=\s+[^\W\d_])")
_UNIT_NOUN_AFTER = re.compile(r"\s*(?:שנה|שנים|שנות|חודש|חודשים|ימים|יום|שבוע|שבועות|קומות|חדרים|מ[\"״]ר)")


def _without_list_number(text: str) -> str:
    """``text`` without the number a numbered list item opens with ("2. …"): the number is the item's place in the list, not a figure it
    states, so an item is matched on its text and never on its list number (round 20 U5, KTD5; R17)."""
    return _LIST_NUMBER.sub(" ", text, count=1)


def _not_headline(text: str, start: int, end: int) -> bool:
    """Whether the number at ``text[start:end]`` names a thing and states no amount of the item: an identifier (a plot, a unit, a section,
    after מס׳; a plan, a permit or a file; letters or digits and a slash around it), a footnote mark ("(1)"), an ordinal opening the
    text ("2. …") or the numbering of a section heading opening it ("4.2 …"; round 21 U7, KTD7)."""
    if not_an_amount(text, start, end, years=False):
        return True
    before = text[max(0, start - 28):start]
    if _CODE_BEFORE.search(before) or _CODE_AFTER.match(text, end):
        return True
    if _PLAN_BEFORE.search(before) and not (_QUANTITY_AFTER.match(text, end) or text[start:end].endswith("%")):
        return True
    if (end - start <= 2 and _MARK_BEFORE.search(before) and _MARK_AFTER.match(text, end)
            and text[start:end].isdigit()):
        return True
    ordinal = _ORDINAL_AT_START.match(text)
    if ordinal and start < ordinal.end():
        return True
    section = _SECTION_NUMBER.match(text)
    return bool(section and section.start(1) <= start and end <= section.end(1)
                and not (_QUANTITY_AFTER.match(text, section.end(1)) or _UNIT_NOUN_AFTER.match(text, section.end(1))))


@functools.lru_cache(maxsize=2048)
def headline(value: str, quote: str) -> tuple[frozenset[str], ...]:
    """The headline values of an item: the numbers of its value field that its quote writes too (a number only the agent wrote is no
    figure of the source), without the identifiers, the footnote marks and the ordinals (``_not_headline``) — by the value's own
    context and by the quote's. One set of forms per number, once each; empty for an item whose value states no such figure (it is then
    asked by its words)."""
    blank, source = _outside_dates(value), _outside_dates(quote)
    written = _numbers_with_scale(source, source=True)
    quoted = frozenset().union(*(f for _, _, f in written)) if written else frozenset()
    out: list[frozenset[str]] = []
    for start, end, forms in _numbers_with_scale(blank):
        if _not_headline(blank, start, end) or not forms & quoted or forms in out:
            continue
        hits = [(a, b) for a, b, f in written if f & forms]
        if hits and all(_not_headline(source, a, b) for a, b in hits):
            continue  # the source writes this number only as an identifier
        out.append(forms)
    return tuple(out)


# a share written as a fraction ("2/3"): the item's own value token though no headline number (``_not_headline`` reads n and m as identifiers)
_FRACTION = re.compile(r"(?<![\d/.,])(\d{1,4})\s*/\s*(\d{1,4})(?![\d]|\s*/\s*\d)")


def fractions_in(text: str) -> tuple[str, ...]:
    """The share fractions ``text`` writes ("1/3"), as n/m with no blanks, once each, in order."""
    return tuple(dict.fromkeys(f"{m.group(1)}/{m.group(2)}" for m in _FRACTION.finditer(text or "")))


@functools.lru_cache(maxsize=2048)
def value_tokens(value: str, quote: str) -> tuple[tuple[frozenset[str], ...], tuple[str, ...]]:
    """The datum's own value tokens (round 21 U2, KTD1): its headline forms (``headline``) and the share fractions n/m of its value that
    its quote writes too. What the server looks for inside a sentence to bind a note to the item, never a number that merely stands near it."""
    written = set(fractions_in(quote))
    return headline(value, quote), tuple(f for f in fractions_in(value) if f in written)


@functools.lru_cache(maxsize=2048)
def _text_scan(text: str) -> tuple[tuple[tuple[int, int, str], ...], tuple[tuple[int, int, frozenset[str]], ...]]:
    """What ``token_places`` reads of a text alone: its stated fractions as (start, end, n/m) and the numbers of the text with its dates
    blanked in place (so the offsets stay the text's own), as ``_numbers_with_scale`` reads them. A function of the text alone, kept per
    text: the same sentence is placed against every item (round 21 simplification; same values, once per text)."""
    frac = tuple((m.start(), m.end(), f"{m.group(1)}/{m.group(2)}") for m in _FRACTION.finditer(text))
    blank = text  # the dates blanked in place, so the offsets stay the text's own
    for p in sorted(shared_dates.find_all(text), key=lambda x: x.span[0], reverse=True):
        blank = blank[:p.span[0]] + " " * (p.span[1] - p.span[0]) + blank[p.span[1]:]
    return frac, tuple(_numbers_with_scale(blank))


def token_places(text: str, groups: Sequence[frozenset[str]], fractions: Sequence[str]) -> list[list[tuple[int, int]]]:
    """For each value token of a datum (a headline group, then each fraction), where ``text`` states it, as (start, end) offsets of
    ``text``, in the text's order; an empty list for a token the text does not state. A number inside a stated fraction counts for the
    fraction only."""
    frac, numbers = _text_scan(text)

    def inside(a: int, b: int) -> bool:
        return any(fa <= a and b <= fb for fa, fb, _ in frac)

    out: list[list[tuple[int, int]]] = []
    for group in groups:
        out.append([(a, b) for a, b, forms in numbers if forms & group and not inside(a, b)])
    for f in fractions:
        out.append([(a, b) for a, b, g in frac if g == f])
    return out


# --- the datum of an item: what two overlapping fills must share to be one (round 20 U1, KTD1; R1-R4) -----------------------------------

class Datum(NamedTuple):
    """The fingerprint of what an item states, taken once at its first insertion into the ledger and never recomputed (``Item.datum``): its
    headline values (``values``; ``figures``: the numbers of its quote when it has none), its dates, its condition and label words, the
    quote and the place of the quote in its source (``block``). ``declared``: the agent wrote the item, so its condition words are the
    agent's reading of the datum; a server item's condition is built from the source and tells no datum apart."""

    values: frozenset[frozenset[str]]
    figures: frozenset[frozenset[str]]
    valued: bool
    dates: frozenset[str]
    condition: str
    label: str
    quote: str
    declared: bool
    block: tuple[int | None, str]

    def token(self) -> str:
        """A short stable name of the datum, the suffix of the key of a fill that shares its evidence with another datum's."""
        text = "|".join([";".join(sorted(",".join(sorted(v)) for v in self.values or self.figures)), ",".join(sorted(self.dates)),
                         ";".join(sorted(",".join(sorted(g)) for g in _word_groups(self.condition))), str(self.block)])
        return hashlib.blake2s(text.encode("utf-8"), digest_size=4).hexdigest()


def block_of(src, quote: str) -> tuple[int | None, str]:
    """Where the quote stands in its source (round 20 U1, KTD1: the datum's position): the source's table and the flat line (a table row,
    a paragraph) that holds the whole quote; ("" for the line) when no single line does, or the source is unknown — a position that is
    not known is compatible with every other (``same_datum``)."""
    if src is None:
        return None, ""
    row = _row_of(context_of_source(src)[1], SupportScope.flat(quote))
    return src.table_index, _flat_span(row) if row is not None else ""


def datum_of(item: Item) -> Datum:
    """The datum fingerprint of an item from its own fields (``Datum``)."""
    values = frozenset(headline(item.value, item.quote))
    return Datum(values, values or frozenset(_number_forms(item.quote)), bool(item.value), frozenset(_item_dates(item)), item.condition,
                 item.label, _flat_span(item.quote), item.origin == ORIGIN_AGENT, item.block)


def fingerprint(item: Item) -> Datum:
    """The item's frozen datum: computed the first time it is asked for (an item's first comparison or insertion) and kept (KTD1)."""
    if item.datum is None:
        item.datum = datum_of(item)
    return item.datum


def _lacks(wanted: str, have: str) -> bool:
    """Whether ``wanted`` writes a content word that ``have`` does not."""
    return any(not g & _words(have) for g in _word_groups(wanted))


def same_datum(a: Item, b: Item) -> bool:
    """Whether two fills of one component whose quotes overlap state ONE datum (round 20 U1, KTD1; R1-R3). Overlapping or nested quotes
    alone never make them one; all of these must hold, each decided against the items' frozen fingerprints:

    - the same headline values — equal sets, so an aggregate over several figures never absorbs one of its parts (an item with no value
      is asked by the figures of its quote, which must not contradict: one holds the other's);
    - compatible dates — equal where both give one (one may add a second date, never contradict the first);
    - the same source block — the same table and the same row or paragraph, where both are known;
    - no condition of one with a content word the other lacks (the agent's conditions: a server item's, built from the source, is no
      reading of a datum). An item with no value and no condition is told apart by its label the same way when the quotes differ."""
    x, y = fingerprint(a), fingerprint(b)
    if x.valued and y.valued:
        if x.values != y.values:
            return False
    elif x.figures and y.figures and not (x.figures <= y.figures or y.figures <= x.figures):
        return False
    if x.dates and y.dates and not (x.dates <= y.dates or y.dates <= x.dates):
        return False
    (table_x, line_x), (table_y, line_y) = x.block, y.block
    if (table_x is not None and table_y is not None and table_x != table_y) or (line_x and line_y and line_x != line_y):
        return False
    if x.declared and y.declared:
        if _lacks(x.condition, y.condition) or _lacks(y.condition, x.condition):
            return False
        if (not x.values and not y.values and not (x.condition or y.condition) and x.quote != y.quote
                and _lacks(x.label, y.label) and _lacks(y.label, x.label)):
            return False
    return True


# --- blocks: the model-written text the answer gives, as the unit of coverage (round 19 U4, KTD4; R13, R14) ----------------------------

class Block(NamedTuple):
    """One block of the answer — a list item with all its sentences, a paragraph, a table row, a heading — as one unit: its text and the
    citations of its sentences joined. ``unit.index`` is the first sentence's."""

    unit: Unit
    parts: tuple[Unit, ...]  # the sentences of the block, as the model wrote them


def model_units(units: Iterable[Unit]) -> list[Unit]:
    """The units as the model wrote them: the spans the server wrote inside a unit (a marker, a mark, a citation it inserted) are left out
    of its text, and a unit made only of server spans is no unit. A gap line, an item line or a note covers nothing (KTD4)."""
    out = []
    for u in units:
        if not u.server:
            out.append(u)
            continue
        text = _without_server(u.text, u.server)
        if re.search(r"\w", text):
            out.append(replace(u, text=text, server=[]))
    return out


def blocks_of(units: Sequence[Unit]) -> list[Block]:
    """The blocks of ``units`` (``Unit.block``): consecutive units of one block are joined — text, and the union of the citations; a unit
    built by hand (block -1) is a block of its own. The units are the answer's own, in order."""
    out: list[Block] = []
    run: list[Unit] = []

    def close() -> None:
        if not run:
            return
        first = run[0]
        joined = first if len(run) == 1 else replace(
            first, raw=" ".join(u.raw for u in run), text=" ".join(u.text for u in run),
            ids=list(dict.fromkeys(i for u in run for i in u.ids)), end=run[-1].end, leads_list=run[-1].leads_list)
        out.append(Block(joined, tuple(run)))
        run.clear()

    for u in units:
        if run and (u.block < 0 or u.block != run[-1].block):
            close()
        run.append(u)
    close()
    return out


_PREFIX_LETTERS = "ובהלמכש"  # the one-letter prefixes of a Hebrew word: and, in, the, to, from, as, that


def _unprefixed(word: str) -> str:
    """``word`` without its one leading prefix letter when a stem of three letters or more is left ("לחלקות" is "חלקות"); else ``word``."""
    return word[1:] if len(word) >= 4 and word[0] in _PREFIX_LETTERS else word


def quote_in_blocks(quote: str, units: Sequence[Unit], component: str = "", *, blocks: Sequence[Block] | None = None) -> bool:
    """Whether a quoted part of a source is in the answer (round 16 KTD2, by block since round 19 U4): one block holds all the quote's headline
    values (``headline`` of the quote itself: no identifier, footnote mark or ordinal) with at least half of its content words, the words
    that name the item (its label) — not those of the component's own text, which every item of a list shares — and its value. Another
    item with the same value is no presence. A quote with neither numbers nor words counts as present, so it supports nothing. ``blocks``:
    the blocks of ``units`` when the caller has them already (``blocks_of(model_units(units))``), for a caller that tests many quotes
    against the same answer.

    Round 21 U7 (KTD7, R8): a word is the component's when it or its stem without a prefix letter is one of the component's words ("לחלקות"
    under "חלקות", "בגוש" under "גוש"), so the prefixed forms of a component's word are not left among the words the answer must write; and
    the label may be inflected for a quote with no number as well as for one with numbers."""
    if blocks is None:
        blocks = blocks_of(model_units(units))
    words = _words(quote)
    asked = _words(component)
    words = frozenset(w for w in words if w not in asked and _unprefixed(w) not in asked) or words
    need = (len(words) + 1) // 2
    groups = headline(quote, quote)

    own, negation = designators(quote), _own_negation((quote,))

    def stated(block: Block) -> bool:
        # the block states the quote only by the clauses that state it positively (round 22 U6, KTD6): not a "not found" sentence that repeats
        # its figures, a negation of them, or the row of another designator
        text = positive_text(_without_list_number(block.unit.text), own, False, *negation)
        # a quote that is its figures (a date, several amounts, an enumeration of lots) is asked by them, a range and an enumeration read as
        # one set; its words are the source's and the answer's own (round 21 G1)
        if (by_figures := figures_stated(quote, text)) is not None:
            return by_figures
        if not all(g & _have(text) for g in groups):
            return False
        have = _words(text)
        if len(words & have) >= need:
            return True
        # every headline value stands in the block (the quote has none, or each of them does): the label may be inflected ("שווי" / "שוויו"), a
        # word of three letters or more that a block word starts with is the same word (round 20 U9, F8); a block that holds the value under
        # another noun still needs the words
        return sum(1 for w in words if w in have or (len(w) >= 3 and any(h.startswith(w) for h in have))) >= need

    return any(stated(b) for b in blocks)


class _Check(NamedTuple):
    why: str  # "" the block gives the item; "partial" it gives part of its headline values; else the furthest check it failed
    missing: tuple[frozenset[str], ...] = ()


class CoverageUnavailable(Exception):
    """The coverage of an item cannot be decided: an input is missing (its source is not in the workspace). It is the outcome
    ``unchecked`` of the fill, never "omitted"."""


_DURATION = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:שנה|שנים|שנות|חודש|חודשים|ימים|יום|שבוע|שבועות)(?![א-ת])")


def _whole_duration(item: Item, text: str, group: frozenset[str]) -> bool:
    """Whether the item's value writes the number of ``group`` as a duration ("5 שנים") and ``text`` writes the same duration."""
    def durations(source: str) -> set[str]:
        return {f for m in _DURATION.finditer(source) for f in number_forms(m.group(1))}

    return bool(group & durations(item.value) & durations(text))


def _names_row(item: Item, words: frozenset[str]) -> bool:
    """Whether the block's words state every content word of the item's label: the block speaks of the item's own row."""
    named = _word_groups(item.label)
    return bool(named) and all(g & words for g in named)


def _value_check(item: Item, text: str, words: frozenset[str]) -> _Check:
    """Whether the block's ``text`` states the item's value: a date value by its date; else every headline value (``headline``), and, for an item
    with none, half of the words of its value, its label or its quote (the first that has content words)."""
    text = _positive(item, text)  # only the clauses that state it positively, for its own designators (round 22 U6, KTD6)
    if parsed := shared_dates.parse(item.value):
        return _Check("" if any(p.iso.startswith(parsed.iso) for p in shared_dates.find_all(text)) else "value_not_stated")
    groups = headline(item.value, item.quote)
    if groups:
        have = _have(text)
        missing = tuple(g for g in groups if not _stated(item, text, have, g))
        if not missing:
            return _Check("")
        if len(missing) == len(groups):
            return _Check("value_not_stated")
        given = [g for g in groups if g not in missing]
        # a block gives an item in part by a figure of it; the digits one or two of an unrelated row also write ("2" floors, "6" rooms) are
        # no part of this item unless they are its whole duration ("5 שנים") or the block states the item's whole row label (round 21 U7, KTD7)
        if all(_stray(g) for g in given) and not any(_whole_duration(item, text, g) or _names_row(item, words) for g in given):
            return _Check("value_not_stated")
        return _Check("partial", missing)
    # an item with NO value is asked by the figures and dates of its quote first, a range and an enumeration of lots read as one set (round 21
    # G1): a judge's quote is the source's wording, and half of its words are not the answer's
    if not item.value.strip() and figures_stated(item.quote, text):
        return _Check("")
    # the words that ask for it: its value's, else its label's, else its quote's; a value with figures the quote does not write is the agent's
    # own, and its words are not the source's: the label and the quote come first. A value with no number is asked by its own words, and then
    # by the quote's — the source's words — before the agent's label, which the answer need not write ("פינוי" for a row the answer states
    # as "המגרש יפונה …"; round 21 U7, KTD7, R8)
    order = (item.label, item.quote, item.value) if re.search(r"\d", item.value) else (item.value, item.quote, item.label)
    wanted = next((t for t in order if _word_groups(t)), "")
    if _enough(wanted, words):
        return _Check("")
    # an item with NO value is asked by the quote's words first, and then by its label's: the label is the agent's own description of the claim,
    # and the answer states the claim in its own words as often as in the source's (the label's words for a quote that names the same thing otherwise). A label of one content word is no evidence (it stands in unrelated blocks), so only a label of two or more counts (round 21
    # second pass, harm 1; the pre-U7 order asked the label for an empty value)
    if not item.value.strip() and wanted != item.label and len(_word_groups(item.label)) >= 2 and _enough(item.label, words):
        return _Check("")
    return _Check("value_not_stated")


def _states_date(item: Item, text: str, lead: str = "") -> bool:
    """The item's date is stated by the block, or, when the block states no date of its own, by its lead: the lead-in sentence of
    its list, the sentence and header before its table, or the heading above it (``_leads``; round 18 U7)."""
    stated = [p.iso for p in shared_dates.find_all(text)] or [p.iso for p in shared_dates.find_all(lead)]
    return all(any(s.startswith(iso) for s in stated) for iso in _item_dates(item))


def _leads(units: Sequence[Unit]) -> dict[int, str]:
    """For each unit (by index), the text around it that dates it: its table's header row and the line before the table
    (``Unit.context``), the lead-in line of the list it is an item of — the nearest earlier unit that leads a list, through the items
    directly before it — and the headings above it (``Unit.heading``). A lead-in the answer no longer holds dates nothing."""
    by_index = {u.index: u for u in units}
    out = {}
    for u in units:
        parts = [u.heading, u.context]
        if u.list_item:
            prev = by_index.get(u.index - 1)
            while prev is not None and prev.list_item and not prev.leads_list:
                prev = by_index.get(prev.index - 1)
            if prev is not None and prev.leads_list:
                parts.append(prev.text)
        out[u.index] = " ; ".join(x for x in parts if x)
    return out


def _check_block(item: Item, block: Block, ws, shared: bool, rivals: frozenset[str], lead: str = "",
                 rival_dates: frozenset[str] = frozenset()) -> _Check:
    """What one block of the answer gives of the item: its failure ``why`` (the first check it fails), ``partial`` with the headline values it
    leaves out, or "" when it gives it. Attribution is the union of the citations of the block's sentences (R5); the value is every headline
    value in the block's text; the date may stand in the block's lead; the condition words are asked only to tell apart items that
    share a value, over the block's words, and a date the item states that its twins do not state tells it apart (round 19 U4)."""
    unit = block.unit
    if not _attributed(item, unit, ws):
        return _Check("not_attributed")
    text = _positive(item, unit.text)  # what the block states positively for this item's row (round 22 U6, KTD6)
    words = _words(text)
    verdict = _value_check(item, text, words | _words(unit.context))
    if verdict.why:
        return verdict
    if not _states_date(item, text, lead):
        return _Check("date_not_stated")
    if shared and not _tells_apart(item, words, rivals, rival_dates):
        return _Check("condition_not_stated")
    return verdict


def _giving_parts(item: Item, block: Block, ws=None) -> list[Unit]:
    """The sentences of a block that give the item: those that state one of its headline values (its date, for an item whose value is a date);
    all of them for an item asked by its words, or when no sentence states a value of its own. Round 20 U3 (KTD3, R10): among several that
    state it, the ones whose OWN citations include the item's source (``_attributed``) — a sentence that only shares the figure with the item
    through the citations of the block it stands in (it speaks of two appraisals, and cites the other's value) is not the one the item's
    condition is read against or written into; when none does, all of them, as before. Round 21 full measurement: among those, the ones that cite
    the item's own source itself (``_cites_source``) when some do."""
    if len(block.parts) == 1:
        return list(block.parts)
    if parsed := shared_dates.parse(item.value):
        mine = [u for u in block.parts if any(p.iso.startswith(parsed.iso) for p in shared_dates.find_all(_positive(item, u.text)))]
    elif groups := headline(item.value, item.quote):
        mine = [u for u in block.parts if any(_stated(item, (t := _positive(item, u.text)), _have(t), g) for g in groups)]
    elif fractions := value_tokens(item.value, item.quote)[1]:
        # a share ("2/3") is no headline number, so every sibling sentence of the block gave the item: the sentence that holds the item's own
        # fraction is the one that gives it (round 21 U2, KTD1, R1, R6); none holds it: all of them, and nothing is written next to any
        mine = [u for u in block.parts if set(fractions) & set(fractions_in(u.text))]
    else:
        mine = []
    chosen = mine or list(block.parts)
    if ws is not None and len(chosen) > 1:
        own = [u for u in chosen if _attributed(item, u, ws)] or chosen
        return [u for u in own if _cites_source(item, u, ws)] or own  # round 21 full measurement: the sentence that cites the item's own source
    return chosen


class Giving(NamedTuple):
    """The units that give a covered item: its sentences (``units``, the indexes the condition check is read against) and the block of each
    of them when it has several sentences (``blocks``: the text a condition split over them is read in)."""

    units: frozenset[int]
    blocks: tuple[Unit, ...]


_NO_GIVING = Giving(frozenset(), ())


def _coverage_all(item: Item, blocks: Sequence[Block], ws, shared: bool, leads: Mapping[int, str],
                  rivals: frozenset[str] = frozenset(), rival_dates: frozenset[str] = frozenset()) -> tuple[Coverage, Giving]:
    """``_coverage`` and, from the same pass over the blocks, every sentence that gives the item (``Ledger.outcomes``): the coverage names
    the first of them. Raises ``CoverageUnavailable`` when the item's source is not in the workspace."""
    if ws is None or _place(ws, item.source) is None:
        raise CoverageUnavailable(item.id)
    checks = [(b, _check_block(item, b, ws, shared, rivals, leads.get(b.unit.index, ""), rival_dates))
              for b in blocks]
    giving = [b for b, c in checks if not c.why]
    if giving:
        parts = [(b, _giving_parts(item, b, ws)) for b in giving]
        # round 21 full measurement: when a block that gives the item cites its own source, the blocks that give it only by another source's citation
        # (the same figure in another passage) are not the sentence the item and its condition note go to; no block cites it: the first one, as before
        parts = [(b, mine) for b, mine in parts if any(_cites_source(item, u, ws) for u in mine)] or parts
        indexes = frozenset(u.index for _, mine in parts for u in mine)
        return (Coverage(True, parts[0][1][0].index, ""),
                Giving(indexes, tuple(b.unit for b, _ in parts if len(b.parts) > 1)))
    partial = [c for _, c in checks if c.why == "partial"]
    if partial:  # the block that gives the most of the item
        return Coverage(False, None, "value_partial", min(partial, key=lambda c: len(c.missing)).missing), _NO_GIVING
    furthest = max((_WHY_ORDER.index(c.why) for _, c in checks), default=-1)
    return Coverage(False, None, _WHY_ORDER[furthest] if furthest >= 0 else "not_attributed"), _NO_GIVING


def _coverage(item: Item, units: Sequence[Unit], ws, shared: bool, leads: Mapping[int, str] | None = None,
              blocks: Sequence[Block] | None = None) -> Coverage:
    """Whether the answer gives the item, block by block (R5, R13, R14; see the module docstring). ``shared``: another item of the item's
    component has its signature (its value, or its label), which makes its condition words (else its label's) needed. ``leads``: the
    text around each unit that dates it (``_leads``), and ``blocks``: ``blocks_of(units)``, when the caller already has them."""
    leads = _leads(units) if leads is None else leads
    blocks = blocks_of(units) if blocks is None else blocks
    try:
        return _coverage_all(item, blocks, ws, shared, leads)[0]
    except CoverageUnavailable:
        return Coverage(False, None, "not_attributed")


# --- the turn's ledger ---------------------------------------------------------------------------------------------------------

def _overlap(a: Evidence, b: Evidence) -> bool:
    """Whether two pieces of evidence are one passage: the same document and appraisal unit, and a quote that holds or is held by the other."""
    return a[:2] == b[:2] and (a[2] in b[2] or b[2] in a[2])


_ROW_MARKER = re.compile(r"\A\s*(?:[-*+•·–—]|\d{1,3}[.)])\s+")  # a list marker a quote opens with
_ROW_BRACKETS = re.compile(r"[()\[\]{}<>]")  # a bracket in either direction (a right-to-left line draws "(" for ")" and back)
_ROW_END = re.compile(r"[\s.,;:!?…]+\Z")
_ROW_KEY_MIN = 6  # a normalised quote shorter than this names no row


@functools.lru_cache(maxsize=2048)
def _row_key(quote: str) -> str:
    """The row a quote names, whichever way the source or the agent spelled its edges (round 21 U7, KTD7): without a leading list marker
    ("- ", "* ", "2. "), without the brackets (their direction is a drawing matter) and without the marks that close it."""
    flat = _ROW_BRACKETS.sub(" ", _ROW_MARKER.sub("", quote, count=1))
    return _ROW_END.sub("", " ".join(flat.split())).casefold()


def _same_row(a: Evidence, b: Evidence) -> bool:
    """Whether two pieces of evidence are one row (round 21 U7, KTD7; R8): the same document and appraisal unit, and quotes that differ only
    by a leading list marker, the end punctuation, the direction of a bracket or a truncated tail — one normalised quote holds the other.
    A server-built twin of an agent's item is the same row, not another item that shares its value, and asks no words of it."""
    if a[:2] != b[:2]:
        return False
    x, y = _row_key(a[2]), _row_key(b[2])
    short, long = sorted((x, y), key=len)
    return len(short) >= _ROW_KEY_MIN and short in long


def _inside(narrow: Evidence, wide: Evidence) -> bool:
    """Whether ``narrow`` is a strictly smaller quote of the same place held by ``wide``."""
    return narrow != wide and narrow[:2] == wide[:2] and narrow[2] in wide[2]


class Ledger:
    """The turn's fills across answer versions, keyed by (component, evidence), with the items excluded so far."""

    def __init__(self) -> None:
        self.items: dict[ItemKey, Item] = {}
        self.excluded: list[dict] = []
        self.misattributed: list[dict] = []
        self.version = 0  # the latest answer version an agent declared items in (0: versions are not told)
        self._ids: dict[ItemKey, str] = {}
        self._evidence_ids: dict[Evidence, str] = {}
        self._candidate_ids: dict[Evidence, str] = {}

    def candidate_id(self, evidence: Evidence) -> str:
        """The K# of a candidate (its evidence): the same in every call of the turn, whichever S# names the passage."""
        return self._candidate_ids.setdefault(evidence, f"K{len(self._candidate_ids) + 1}")

    def evidence_id(self, evidence: Evidence) -> str:
        """The E# of a piece of evidence: the same in every version, for every component it fills."""
        return self._evidence_ids.setdefault(evidence, f"E{len(self._evidence_ids) + 1}")

    def id_for(self, key: ItemKey) -> str:
        """The I# of a key: the same for the same key in every version."""
        return self._ids.setdefault(key, f"I{len(self._ids) + 1}")

    def _near(self, item: Item) -> list[Item]:
        """The fills of the item's component on a quote that holds or is held by its quote, in its document and appraisal unit."""
        ev = item.evidence
        return [i for i in self.items.values() if i.component == item.component and i.evidence != ev and _overlap(i.evidence, ev)]

    def twin_of(self, item: Item) -> Item | None:
        """The fill of the ledger that IS the item: the fill of the same component and evidence that states its datum, else the only fill of
        its component on an overlapping quote of the same place that states it too (another spelling of the same passage). Round 20 U1
        (KTD1; R1-R3): a structural match is never enough — ``same_datum`` must hold, against the fill's frozen fingerprint — so two
        different data of one passage are two fills, and a fill is never another datum's twin. Several overlapping fills are separate
        evidence — each keeps its own condition check — so none is the item's twin."""
        held = [i for i in self.items.values() if i.component == item.component and i.evidence == item.evidence]
        for fill in held:
            if fill is item or same_datum(fill, item):
                return fill
        # an item built by hand has no place of its own (no document): it is the fill of the same source and quote, placed or not
        unplaced = [i for i in self.items.values() if i.component == item.component and i.source == item.source
                    and i.evidence[2] == item.evidence[2] and not (i.document_id and item.document_id) and same_datum(i, item)]
        if unplaced:
            return unplaced[0]
        near = self._near(item)
        return near[0] if len(near) == 1 and same_datum(near[0], item) else None

    def id_of(self, item: Item) -> str:
        """The I# of the item: its twin's when the ledger holds one, else the one of its key."""
        twin = self.twin_of(item)
        if twin is None:
            return self.id_for(item.key)
        return twin.id or self.id_for(twin.key)

    def _place(self, item: Item) -> None:
        """Give an item that has no twin its own key and its own I#: when its evidence is already filled by another datum, its key adds the
        datum's fingerprint (round 20 U1, KTD1: it is not dropped as a duplicate), and an I# another fill holds (the id of a same-evidence
        item of the same batch, taken before either was merged) is replaced."""
        if item.key in self.items:
            item.variant = fingerprint(item).token()
            while item.key in self.items:
                item.variant += "'"
        if not item.id or any(i.id == item.id for i in self.items.values()):
            item.id = self.id_for(item.key)

    def alive(self, item: Item) -> bool:
        """Whether the fill stands in the latest version: an agent's fill the latest version declared, or one the server derives again at
        every verification (a candidate, the judge's support, a cited span). An agent's fill an earlier version declared and the latest one
        did not list is still open, but is no twin of the items that do (``Ledger.outcomes``: ``shared``)."""
        return item.origin != ORIGIN_AGENT or item.last_version >= self.version

    def merge(self, kept: Iterable[Item], *, excluded: Iterable[dict] = (), version: int = 0,
              misattributed: Iterable[dict] = ()) -> list[Item]:
        """Merge one answer version's valid items (and record what it excluded and the conditions it misattributed); the ledger's
        items, in the order first seen. A fill already in the ledger takes its new declaration from the agent (label, value, condition,
        and state and reason) — a later version may close it with a valid ``not_verifiable`` or ``not_applicable``, but a closed
        decision is not reopened by the same item listed ``included`` again (round 19 U3, R3); the judge's support and the server's
        source spans never change a fill the ledger already holds. A fill not listed again stays. The same passage of the same component
        (another spelling of the quote, or another S#) is the same fill (``twin_of``) only when it states the same datum (round 20 U1,
        KTD1: ``same_datum``); another datum is a fill of its own, and a version's item that a later version's item of another datum
        corrects is closed as superseded (``_supersede``). An agent's fill replaces the origin ``source``
        fill of its own component that holds the same span (round 18 U5): the span's condition is then checked once, as the agent's
        fill — and never touches the fill of another component, which the same evidence also serves (round 19 U3, R2). ``version``:
        recorded in each fill's history."""
        self.version = max(self.version, version)
        for item in kept:
            item.evidence_id = self.evidence_id(item.evidence)
            twin = self.twin_of(item)
            if twin is None:
                # a server fill adds nothing only beside a fill of the same datum (round 20 U1, KTD1): another datum of the passage is kept
                if item.origin != ORIGIN_AGENT and any(same_datum(n, item) for n in self._near(item)):
                    continue
                self._place(item)
                item.datum = fingerprint(item)  # frozen here, at the first insertion
                item.first_version = item.last_version = version
                self._record(item, version)
                self.items[item.key] = item
            elif item.origin == ORIGIN_AGENT:
                if twin.key != item.key and twin.origin == ORIGIN_SOURCE:
                    self._replace(twin, item, version)
                else:
                    self._declare(twin, item, version)
        self._supersede()
        self.excluded += [{"version": version, **e} for e in excluded]
        self.misattributed += [{"version": version, **m} for m in misattributed]
        return list(self.items.values())

    def _supersede(self) -> None:
        """Close as superseded an agent item of an earlier version that the latest version does not list, when the latest version declares,
        in the same component, an item of another datum on the same or a nested quote that no earlier version declared (round 20 U1, KTD1;
        R3): a value corrected, or a condition added, is a new item, and the old one is neither scored nor a gap line. Only an item still
        ``included`` is closed this way; a decision the agent closed with a reason stays its own. Nothing happens in a turn whose
        versions are not told."""
        if self.version <= 0:
            return
        latest = [g for g in self.items.values() if g.origin == ORIGIN_AGENT and g.first_version == self.version]
        for fill in self.items.values():
            if (fill.origin != ORIGIN_AGENT or fill.last_version >= self.version or fill.superseded_by
                    or fill.state != ITEM_INCLUDED):
                continue
            by = next((g for g in latest if g.component == fill.component and _overlap(g.evidence, fill.evidence)
                       and g.first_version > fill.last_version), None)
            if by is not None:
                fill.superseded_by = by.id
                fill.history.append({"version": self.version, "state": SUPERSEDED, "reason": "", "origin": fill.origin, "by": by.id})

    @staticmethod
    def _record(fill: Item, version: int, declared: str = "") -> None:
        """Add the fill's present state to its history, once per version and state; ``declared``: what the agent said when the fill kept a
        closed decision instead."""
        rec = {"version": version, "state": fill.state, "reason": fill.reason, "origin": fill.origin}
        if declared and declared != fill.state:
            rec["declared"] = declared
        if not fill.history or fill.history[-1] != rec:
            fill.history.append(rec)

    def _declare(self, old: Item, item: Item, version: int) -> None:
        """The agent's declaration of the fill ``old``: its words (never emptied), its state and reason — except that a closed decision
        stays when the agent lists the item ``included`` again — and its origin, an origin ``source`` fill becoming the agent's."""
        old.label, old.value, old.condition = item.label or old.label, item.value or old.value, item.condition or old.condition
        if not (old.state in NEEDS_REASON and item.state == ITEM_INCLUDED):
            old.state, old.reason = item.state, item.reason
        if old.origin == ORIGIN_SOURCE:
            old.origin, old.candidate = ORIGIN_AGENT, False
        old.last_version = max(old.last_version, version)
        old.superseded_by = ""  # a version that lists the item again stands it up again
        self._record(old, version, item.state)

    def _replace(self, old: Item, item: Item, version: int) -> None:
        """The agent's fill takes the place of the origin ``source`` fill of its component on an overlapping quote: its id and its position
        in the ledger, its history and its first version."""
        item.id, item.first_version, item.last_version = old.id, old.first_version, max(old.last_version, version)
        item.history = list(old.history)
        if item.key != old.key and item.key in self.items:  # another datum already rests on the agent's evidence (round 20 U1)
            item.variant = fingerprint(item).token()
            while item.key in self.items:
                item.variant += "'"
        item.datum = fingerprint(item)
        self._ids.setdefault(item.key, old.id)
        self.items = {(item.key if k == old.key else k): (item if k == old.key else v) for k, v in self.items.items()}
        self._record(item, version)

    def outcomes(self, units: Sequence[Unit], ws) -> list[dict]:
        """Every fill with its state against these surviving units: ``covered`` (a block of them gives it), ``omitted`` (an included or
        judge-added fill no block gives, wholly or in part: ``outcome`` says which, and ``missing`` names what a partly given one is
        missing), ``unchecked`` (the check could not be completed), or the ``not_verifiable`` / ``not_applicable`` the agent declared
        with a reason — which a block that gives the fill covers as well (round 19 U4). Round 18
        U5: a covered fill also has its condition check against the unit that gives it (``condition_check``: "" without a
        material condition, else ``kept``, ``missing``, ``undecided`` or ``hint``; ``condition_quote``: for ``missing``, the clause of the span the unit leaves
        out; ``condition_hint``: ``contradicted`` and/or ``other_item``;
        ``condition_spans``: its spans; ``condition_foreign``: the items whose condition the sentence states; ``condition_unit``:
        the unit checked). Round 19 U3: the condition is a property of the EVIDENCE, so it is checked once for the fills of one
        evidence — on the first of them a unit gives, against the units that give any of them — and the others carry no check.
        An origin ``source`` fill no surviving unit gives is not an open item: the sentence that cited its span
        is gone, so it is left out; a ``candidate`` fill (round 18 U6) is the exception: a component asks for it, so it stays
        open (``omitted``) until a sentence gives it. ``history``: the answer versions' declarations of the fill."""
        out = []
        # each item's signature is computed once, and the items of a component that share one are counted: an item's condition is
        # asked when another item of its component has its signature (``_coverage``). Only the fills alive in the latest version count
        # (``alive``): an earlier version's item the latest did not list is no twin of the ones that stand, and neither is a fill of the
        # same passage in another quote (``_overlap``)
        # an item a later version's item of another datum corrected is closed as superseded: it is not scored, and no line is written for it
        # (round 20 U1, KTD1)
        scored = [i for i in self.items.values() if not i.superseded_by]
        signatures = {id(i): _signature(i) for i in scored}
        standing: dict[tuple[str, frozenset[str]], list[Item]] = {}
        for i in scored:
            if self.alive(i) and signatures[id(i)]:
                standing.setdefault((i.component, signatures[id(i)]), []).append(i)
        units = model_units(units)  # a server-written span covers nothing (KTD4)
        blocks = blocks_of(units)
        by_index = {u.index: u for u in units}
        leads = _leads(units)
        # per evidence, once: of the LAST fill that rests on it (the fills of one evidence may name it by another S# or write its quote
        # with other blanks, and the later fill's reading is the one that stood)
        quotes = [i.quote for i in scored]  # a note whose mark another item's quote carries is that item's (round 20 U9, F2)
        spans = {ev: _spans_of(ws, i, quotes) for ev, i in {i.evidence: i for i in scored}.items()}
        rows: list[tuple[Item, Coverage | None]] = []
        gave: dict[Evidence, frozenset[int]] = {}  # the sentences that give each covered evidence (the first is the covering sentence)
        wide: dict[Evidence, tuple[Unit, ...]] = {}  # the multi-sentence blocks that give it, each as one unit
        for item in scored:
            sig = signatures[id(item)]
            # an item that states the same row (a server twin of the agent's item that the source spells with another marker, a truncated tail,
            # another closing mark or the other bracket direction) is no rival that shares the value (round 21 U7, KTD7)
            # nor is a row the server built from a cited span (round 21 G2b): the sentence that cites it gives it and it is never an open
            # item, so it cannot be mistaken for the agent's own item and must not force that item to show its label words
            rivals = [j for j in standing.get((item.component, sig), ()) if j is not item and not _overlap(j.evidence, item.evidence)
                      and not _same_row(j.evidence, item.evidence) and not (item.origin == ORIGIN_AGENT and is_server_item(j))]
            shared = bool(sig) and bool(rivals)
            got = None
            if item.state in (ITEM_INCLUDED, OMITTED, *NEEDS_REASON):  # a declared item passes the same check (R16)
                try:
                    got, covering = _coverage_all(item, blocks, ws, shared, leads,
                                                  frozenset().union(*(_words(j.condition or j.label) for j in rivals)),
                                                  frozenset().union(*(_condition_dates(j) for j in rivals)))
                except Exception as exc:  # noqa: BLE001 - any failure of the check is "could not be completed", never "not used" (R17)
                    logger.warning("item coverage check could not be completed: %s", type(exc).__name__)
                    got, covering = Coverage(False, None, ITEM_UNCHECKED, checked=False), _NO_GIVING
                if got.ok:
                    gave[item.evidence] = gave.get(item.evidence, frozenset()) | covering.units
                    wide[item.evidence] = wide.get(item.evidence, ()) + tuple(x for x in covering.blocks if x not in wide.get(item.evidence, ()))
            if is_server_item(item) and (got is None or not got.ok):
                continue  # (a candidate the call assigned to a component is an open item whatever sentence cited what: round 18 U6)
            rows.append((item, got))
        owns = {ev: _own_text(ws, i, quotes) for ev, i in {i.evidence: i for i, _ in rows}.items()}  # each evidence's own context text (last fill's)
        others = [i for i, _ in rows]
        checked: set[Evidence] = set()
        covered = [i for i, g in rows if g is not None and g.ok]
        for item, got in rows:
            # the fill's outcome of the one coverage function: covered, partial (with what the block leaves out), omitted, or unchecked
            outcome = _outcome(got)
            if got is None:
                state = item.state
            elif got.ok:
                state = COVERED
            elif not got.checked:
                state = ITEM_UNCHECKED
            else:  # a declared fill the answer does not give stays declared
                state = item.state if item.state in NEEDS_REASON else OMITTED
            missing = [missing_detail(ws.sources.get(item.source), item.quote, g) for g in got.missing] if got is not None else []
            # a quote that holds a smaller covered quote of the same place leaves the check to that one only when the narrow item is of the
            # same component and states the same datum, reads the conditions of the wide one and is given by the same sentences (round 20
            # U1, KTD1): each narrower quote keeps the condition of its own row, and a narrow item of another component or datum defers nothing
            if got is not None and got.ok and (item.evidence in checked or any(_defers(item, n, spans, gave) for n in covered)):
                check = dict(_NO_CHECK)
            else:
                check = _condition_check(item, got, spans, gave, owns, by_index, others, wide)
                if got is not None and got.ok:
                    checked.add(item.evidence)
            out.append({**item.public(), "state": state, "declared": item.state, "covered_by": got.unit if got else None,
                        "why": got.why if got else "", "outcome": outcome, "missing": missing,
                        "history": [dict(h) for h in item.history], **check})
        return out


def _defers(wide: Item, narrow: Item, spans: Mapping[Evidence, Sequence[Span]], gave: Mapping[Evidence, frozenset[int]]) -> bool:
    """Whether the covered item ``narrow`` carries the condition check of ``wide`` (round 20 U1, KTD1): it is of the same component, its
    quote lies inside the wide one, it states the same datum, its condition spans contain every span of the wide one, and every sentence
    that gives the wide item gives it too."""
    if narrow.component != wide.component or not _inside(narrow.evidence, wide.evidence) or not same_datum(narrow, wide):
        return False

    def texts(ev: Evidence) -> set[str]:
        return {SupportScope.flat(sp.text) for sp in spans[ev]}

    return texts(wide.evidence) <= texts(narrow.evidence) and gave.get(wide.evidence, frozenset()) <= gave.get(narrow.evidence, frozenset())


def _outcome(got: Coverage | None) -> str | None:
    """The outcome of one fill (``Ledger.outcomes``): None for a fill that was not checked at all, ``covered``, ``unchecked`` (the check could
    not be completed), ``partial`` (the block states some headline values and misses others) or ``omitted``."""
    if got is None:
        return None
    if got.ok:
        return COVERED
    if not got.checked:
        return ITEM_UNCHECKED
    return OUTCOME_PARTIAL if got.missing else OMITTED


# --- the condition outcome of a covered item (round 18 U5) ---------------------------------------------------------------------

_NO_CHECK = {"condition_check": "", "condition_hint": [], "condition_spans": [], "condition_foreign": [], "condition_unit": None,
             "condition_quote": ""}
_RANK = {conditions.KEPT: 0, conditions.UNDECIDED: 1, conditions.MISSING: 2, conditions.HINT: 3}


def _spans_of(ws, item: Item, elsewhere: Sequence[str] = ()) -> list[Span]:
    src = ws.sources.get(item.source)
    return condition_spans(src, item.quote, elsewhere, item.value) if src is not None else []


def _own_text(ws, item: Item, elsewhere: Sequence[str] = ()) -> str:
    src = ws.sources.get(item.source)
    return _own_context(src, item.quote, elsewhere) if src is not None else item.quote


def _condition_check(item: Item, got: Coverage | None, spans: dict, gave: dict, owns: dict, by_index: dict[int, Unit],
                     others: list[Item], wide: Mapping[Evidence, Sequence[Unit]] = {}) -> dict:
    """The condition check of a covered item (``Ledger.outcomes``): each of its spans against the sentence that gives it
    (``conditions.outcome``) — the worst of them, a hint over an undecided over a kept; a sentence that states an operator clause
    of another item's span that the item's own context does not hold is a hint from that item. Among several units that give the
    item the one that keeps its condition wins, else the first. Round 19 U4: a condition the item's sentence leaves out but the other
    sentences of its block state (a list item over two sentences) is kept: the block read as one unit rescues a ``missing`` or an
    ``undecided`` check, and never hides a hint."""
    mine = spans[item.evidence]
    if got is None or not got.ok:
        return dict(_NO_CHECK)
    best = None
    for index in sorted(gave.get(item.evidence, {got.unit})):
        unit = by_index[index]
        read = [conditions.read(sp.text, unit.text) for sp in mine]  # (outcome, quote) of each span
        results = [outcome for outcome, _ in read]
        hints = ["contradicted"] if conditions.HINT in results else []
        foreign = _foreign_items(item, unit, spans, gave, owns, others)
        if foreign:
            hints.append(OTHER_ITEM)
        check = (conditions.HINT if hints else "" if not mine else
                 conditions.UNDECIDED if conditions.UNDECIDED in results else
                 conditions.MISSING if conditions.MISSING in results else conditions.KEPT)
        quote = read[results.index(conditions.MISSING)][1] if check == conditions.MISSING else ""
        row = {"condition_check": check, "condition_hint": hints, "condition_foreign": foreign,
               "condition_spans": [sp.public() for sp in mine], "condition_unit": index, "condition_quote": quote}
        if best is None or _RANK.get(check, 0) < _RANK.get(best["condition_check"], 0):
            best = row
        if check in ("", conditions.KEPT):
            break
    if best is not None and best["condition_check"] in (conditions.MISSING, conditions.UNDECIDED):
        for block in wide.get(item.evidence, ()):
            if all(conditions.read(sp.text, block.text)[0] == conditions.KEPT for sp in mine):
                return best | {"condition_check": conditions.KEPT, "condition_quote": "", "condition_unit": best["condition_unit"]}
        # a lead-in line that states a condition for every item of its list is part of the item's own reading (round 21 U2, R3): each span is
        # kept by the item's sentence or by the lead of the list the sentence is an item of, and the item then carries the condition
        lead = _list_lead(by_index[best["condition_unit"]], by_index)
        if lead is not None and all(conditions.read(sp.text, by_index[best["condition_unit"]].text)[0] == conditions.KEPT
                                    or conditions.read(sp.text, lead.text)[0] == conditions.KEPT for sp in mine):
            return best | {"condition_check": conditions.KEPT, "condition_quote": "", "condition_unit": best["condition_unit"]}
    return best or dict(_NO_CHECK)


def _list_lead(unit: Unit, by_index: Mapping[int, Unit]) -> Unit | None:
    """The lead-in line of the list ``unit`` is an item of (round 21 U2): the nearest earlier unit that is no list item itself, through the
    items directly before it, when it leads the list (``Unit.leads_list``: the next line is a list item or a heading). None for a unit
    that is no list item or whose list has no lead-in line. (A list item followed by another also has ``leads_list``; it is no lead.)"""
    if not unit.list_item:
        return None
    prev = by_index.get(unit.index - 1)
    while prev is not None and prev.list_item:
        prev = by_index.get(prev.index - 1)
    return prev if prev is not None and prev.leads_list else None


@functools.lru_cache(maxsize=4096)
def _span_kept(text: str, sentence: str) -> bool:
    """Whether a span states an operator clause that limits and the sentence keeps it: a function of the span and the sentence alone, so
    it is decided once per (span, sentence), whichever item asks."""
    return bool(conditions.material_clauses(text)) and conditions.outcome(text, sentence) == conditions.KEPT


def _foreign_items(item: Item, unit: Unit, spans: dict, gave: dict, owns: dict, others: list[Item]) -> list[str]:
    """The items whose operator clauses the sentence states and the item's own context does not hold: the sentence gives this item
    a condition of another item. Another item the same sentence gives itself is no source of one."""
    found = []
    seen = {item.evidence}  # the fills of the item's own evidence are no other item's; another evidence is named once
    for other in others:
        if other.evidence in seen or unit.index in gave.get(other.evidence, ()):
            continue
        seen.add(other.evidence)
        for sp in spans[other.evidence]:
            if _span_kept(sp.text, unit.text) and not conditions.stated_in(sp.text, owns[item.evidence]):
                found.append(other.id)
                break
    return found
