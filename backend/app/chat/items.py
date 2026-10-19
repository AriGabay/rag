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
- ``Ledger`` is the turn's ledger across answer versions, keyed by (component, source, verbatim quote). Each version's valid items
  merge into it; an item the server added from the judge's support (``support_item``, state ``omitted``) stays until a surviving
  unit covers it or a later version declares it ``not_verifiable`` or ``not_applicable`` with a valid reason. The agent not
  listing an item again closes nothing.

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
never "read and not used" (``coverage.item_gap_lines``)."""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from app.answering.verify import numbers_in
from app.chat import conditions
from app.chat.evidence import claim_terms
from app.chat.meaning import _row_cells
from app.chat.verify import (
    _NUM_AT,
    ITEM_COVERED,
    ITEM_INCLUDED,
    ITEM_NOT_APPLICABLE,
    ITEM_NOT_VERIFIABLE,
    ITEM_OMITTED,
    ORIGIN_AGENT,
    ORIGIN_JUDGE_SUPPORT,
    ORIGIN_SOURCE,
    SUPPORT_QUOTE_MAX,
    SupportScope,
    Unit,
    _cited_sources,
    _is_tabular,
    _value_forms,
    asked_components,
    context_of_source,
    is_server_item,
    request_word_forms,
    shares_request_word,
)
from app.platform import dates as shared_dates

STATES = (ITEM_INCLUDED, ITEM_NOT_VERIFIABLE, ITEM_NOT_APPLICABLE)  # what the agent declares
OMITTED = ITEM_OMITTED  # what the server assigns: a member the judge's support adds, and an included item no surviving unit covers
COVERED = ITEM_COVERED
NEEDS_REASON = (ITEM_NOT_VERIFIABLE, ITEM_NOT_APPLICABLE)
# the order of the coverage checks: a failure is reported by the furthest check a unit reached
_WHY_ORDER = ("not_attributed", "value_not_stated", "date_not_stated", "condition_not_stated")


@dataclass
class Item:
    """One item of the ledger. ``component``: the N# id ("" in a turn with no frozen components); ``source``: an S#, as a V# or
    T# the agent gave is resolved to the text it names; ``origin``: ``agent`` or ``judge_support``; ``id``: I#, assigned by the
    turn's ledger and kept for the item's key across versions; ``document_id`` and ``unit_id`` (None in a file without unit
    marks): the place of its source, for diagnostics (coverage reads them from the workspace)."""

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

    @property
    def key(self) -> tuple[str, str, str]:
        """The item's identity across answer versions: its component, its source and its verbatim quote."""
        return self.component, self.source, SupportScope.flat(self.quote)

    def public(self) -> dict:
        return {"id": self.id, "component": self.component, "label": self.label, "source": self.source,
                "quote": self.quote, "value": self.value, "condition": self.condition, "state": self.state,
                "reason": self.reason, "origin": self.origin, "document_id": self.document_id, "unit_id": self.unit_id,
                "candidate": self.candidate}


class Validation(NamedTuple):
    kept: list[Item]
    excluded: list[dict]  # [{"component", "source", "quote", "state", "reason": why it was excluded}]
    # [{"source", "quote", "condition"}]: items kept whose agent-written condition stands only in another row's context (R3)
    misattributed: list[dict]


class Coverage(NamedTuple):
    ok: bool
    unit: int | None  # the index of the covering unit (``Unit.index``)
    why: str  # when not covered: the furthest check a unit passed to (``_WHY_ORDER``)


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
    kept: list[Item] = []
    excluded: list[dict] = []
    misattributed: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    numbers = iter(range(1, 10**6))
    for entry in raw:
        f = _fields(entry)
        flat = SupportScope.flat(f["quote"])
        component = _component(f["component"], frozen)
        sid = f["source"]
        if f["state"] not in STATES:
            why = "bad_state"
        elif f["state"] in NEEDS_REASON and not f["reason"]:
            why = "no_reason"
        elif component is None:
            why = "component_unassigned"
        else:
            sid = _resolve_source(ws, sid, flat, scope)
            key = (component, sid, flat)
            why = "duplicate" if key in seen else scope.source_problem(sid, flat)
            if why is None:
                src = ws.sources[sid]
                why = scope.place_problem(src) or (None if scope.verbatim(sid, src, flat) else "not_verbatim")
            if why is None:
                seen.add(key)
                document_id, unit_id = _site_of(src)
                condition = f["condition"]
                if condition and _foreign_condition(condition, src, f["quote"]):
                    misattributed.append({"source": sid, "quote": f["quote"][:SUPPORT_QUOTE_MAX], "condition": condition})
                    condition = ""  # the item is checked against its own context (``condition_spans``), not the agent's words
                item = Item(component, f["label"], sid, f["quote"], f["value"], condition, f["state"], f["reason"],
                            document_id=document_id, unit_id=unit_id)
                item.id = ledger.id_for(item.key) if ledger is not None else f"I{next(numbers)}"
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
    item.id = ledger.id_for(item.key)
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
    return next((ln for ln in body if flat in _flat_text(ln)), None)


def source_name(src, quote: str) -> str:
    """How the source names the span an item quotes (round 18 U7, KTD6): the first cell of its table row, else the quote itself. A server
    line names an item by this, never by the agent's own label."""
    if src is not None:
        row = _row_of(context_of_source(src)[1], SupportScope.flat(quote))
        cells = _row_cells(row) if row is not None and "|" in row else []
        if len(cells) >= 2 and re.search(r"[^\W\d_]", cells[0]):
            return " ".join(cells[0].split())
    return " ".join(quote.split())


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
    sentence = next((x for x in _SENTENCE_END.split(row) if flat in _flat_text(x)), "")
    yield from (("row", c) for c in conditions.material_clauses(sentence))


def condition_spans(src, quote: str) -> list[Span]:
    """The condition spans of the item whose quote is ``quote`` in ``src``, from its own context only: an operator clause of the
    quote, the clauses and note of its row (or its sentence), the headings that name a method, a status, a component or a time, and
    the paragraphs and notes attributed to its table (``TablePart.attributed``: a paragraph that is not attributed is no condition
    of its values). Each span once, in that order."""
    parts, body = context_of_source(src)
    flat = SupportScope.flat(quote)
    out: list[Span] = []
    seen: set[str] = set()

    def add(place: str, text: str) -> None:
        key = _flat_text(text)
        if key and key not in seen:
            seen.add(key)
            out.append(Span(place, " ".join(text.split())))

    row = _row_of(body, flat)
    if row is None or "|" not in row:  # a table row is read cell by cell below: its placeholder cells say nothing
        for clause in conditions.material_clauses(quote):
            add("quote", clause)
    if row is not None:
        for place, text in _row_conditions(row, body, flat):
            add(place, text)
    for part in parts:
        if part.place == "heading":
            if conditions.material(part.text):
                add("heading", part.text)
        elif part.place in ("before", "after", "note") and not part.reason:
            for sentence in _SENTENCE_END.split(part.text):
                if conditions.material(sentence):
                    add("paragraph", sentence)
    return out


def _own_context(src, quote: str) -> str:
    """The text of the item's own context: its quote and row, the paragraphs attributed to the table and its headings."""
    parts, body = context_of_source(src)
    row = _row_of(body, SupportScope.flat(quote))
    return "\n".join([quote, row or "", *(p.text for p in parts if not p.reason)])


def _other_context(src, quote: str) -> str:
    """The text of what stands beside the item's row in a table: the other rows and the paragraphs not attributed to the table ("" for
    a source that is no table). Only ``_foreign_condition`` reads it."""
    if not _is_tabular(src):
        return ""
    parts, body = context_of_source(src)
    row = _row_of(body, SupportScope.flat(quote))
    return "\n".join([ln for ln in body if ln != row] + [p.text for p in parts if p.reason])


def _foreign_condition(condition: str, src, quote: str) -> bool:
    """Whether an agent-written condition stands only in another item's context: most of its words are not in the item's own
    context and are in a row beside it (R3)."""
    return not conditions.stated_in(condition, _own_context(src, quote)) and conditions.stated_in(condition, _other_context(src, quote))


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
    """A table row of a source for ``_cited_spans``: the line, its first cell, its first value cell and the numbers its value cells state."""

    line: str
    label: str
    value: str
    numbers: frozenset[str]


class _SpanSentence(NamedTuple):
    """A sentence of a passage line for ``_cited_spans``: its text and, for each number it writes outside its dates, (as written, its numbers)."""

    text: str
    written: tuple[tuple[str, frozenset[str]], ...]


def _span_index(src) -> list[_SpanRow | list[_SpanSentence]]:
    """The body of a source parsed once for ``_cited_spans``, line by line: a table row (two cells or more) with a value cell that holds a
    digit, else the sentences of a passage line. A row without a digit in its value cells has no entry."""
    out: list[_SpanRow | list[_SpanSentence]] = []
    for line in context_of_source(src)[1]:
        cells = _row_cells(line) if "|" in line else []
        if len(cells) >= 2:
            values = [c for c in cells[1:] if re.search(r"\d", c)]
            if values:
                out.append(_SpanRow(line, cells[0], values[0], frozenset(numbers_in(_outside_dates(" ".join(values))))))
            continue
        out.append([_SpanSentence(s, tuple((m.group(0), frozenset(numbers_in(m.group(0))))
                                           for m in _NUMBER.finditer(_outside_dates(s)))) for s in _SENTENCE_END.split(line)])
    return out


def _cited_spans(index: Sequence[_SpanRow | list[_SpanSentence]], wanted: set[str]) -> Iterable[tuple[str, str, str]]:
    """(quote, label, value) of each table row of the source (parsed by ``_span_index``) whose value cells state a number of ``wanted``,
    or each sentence of a passage that does: the spans an answer sentence stating those numbers draws on."""
    for entry in index:
        if isinstance(entry, _SpanRow):
            if wanted & entry.numbers:
                yield _clip_row(entry.line), entry.label, entry.value
            continue
        for sentence in entry:
            hits = [written for written, numbers in sentence.written if numbers & wanted]
            if hits:
                yield sentence.text.strip(), _first_words(sentence.text, trim=False), hits[0]


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


def _holds(item: Item, sid: str, ws, flat: str) -> bool:
    """Whether the ledger item already covers the span ``flat`` of source ``sid``: the same source (or its re-read) and a quote
    that holds or is held by the span."""
    mine = _flat_text(item.quote)
    return _same_source(ws, item.source, sid) and (mine in flat or flat in mine)


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
        wanted = numbers_in(_outside_dates(unit.text))
        if not wanted:
            continue
        for sid, src in _cited_sources(unit, ws):
            made = 0
            if sid not in indexes:
                indexes[sid] = _span_index(src)
            for quote, label, value in _cited_spans(indexes[sid], wanted):
                flat = SupportScope.flat(quote)
                if (made >= ROWS_PER_UNIT or scope.source_problem(sid, flat) or scope.place_problem(src)
                        or any(_holds(i, sid, ws, flat) for i in [*held, *built])):
                    continue
                spans = condition_spans(src, quote)
                if not spans:
                    continue
                made += 1
                document_id, unit_id = _site_of(src)
                item = Item(_component_of(asked, f"{unit.text} {label}"), label, sid, quote, value,
                            " ; ".join(sp.text for sp in spans), ITEM_INCLUDED, "", ORIGIN_SOURCE,
                            document_id=document_id, unit_id=unit_id)
                item.id = ledger.id_for(item.key)
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
    """The first number of ``text`` as it is written, outside dates; "" when it has none."""
    return next((m.group(0) for m in _NUMBER.finditer(_outside_dates(text)) if numbers_in(m.group(0))), "")


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


def _raws(src) -> tuple[list[_Raw], dict[str, list[str]]]:
    """The spans of a source that can be an item, and the lines of each group (``_Raw.group``). A table: the data rows (a first cell,
    and a value cell with a number) and the sentences of its attributed notes that state a number and limit nothing. A passage: the
    members of each enumerated list (two lines or more in a row that open with a list mark) and the sentences with a number of the
    other lines, a heading line ending the paragraph run."""
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
                    header += cells
                continue
            if numbers_in(_outside_dates(" ".join(values))):
                quote = _clip_row(line)
                raws.append(_Raw(quote, cells[0], values[0], "row", f"{quote} {' '.join(header)} {about}", "table"))
        for part in parts:
            if part.place in ("before", "after", "note") and not part.reason:
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
        raws, groups = _raws(src)
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
            if any(_holds(h, sid, ws, flat) for h in held) or _coverage(span, units, ws, shared, leads).ok:
                touched.add(raw.group)
            else:
                free.append(n)
        mine = [SupportScope.flat(h.quote) for h in held if _same_source(ws, h.source, sid)]
        for name, lines in groups.items():
            if any(q in _flat_text(line) for q in mine for line in lines):
                touched.add(name)
        found = [n for n in free if (raws[n].group == "table" or raws[n].group in touched)
                 and not scope.source_problem(sid, flats[n]) and scope.verbatim(sid, src, flats[n])
                 and shares_request_word(raws[n].own, known)]
        if not found:
            continue
        used += 1
        for n in found[:CANDIDATES_PER_SOURCE]:
            raw = raws[n]
            document_id, unit_id = _site_of(src)
            offered.append(Candidate(ledger.candidate_id(sid, flats[n]), sid, raw.quote, raw.label, raw.value,
                                     " ; ".join(sp.text for sp in condition_spans(src, raw.quote)), " ".join(raw.own.split()),
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
    item.id = ledger.id_for(item.key)
    return item


# --- coverage by attribution (R5) --------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=512)
def _flat_text(text: str) -> str:
    return SupportScope.flat(text)


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


def _attributed(item: Item, unit: Unit, ws) -> bool:
    """Whether the unit cites the item's source, or a value or calculation registered from it, or another passage of the item's
    document and unit that holds its quote — and nothing of another document or appraisal unit."""
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
            if _same_source(ws, cid, item.source) or flat in _flat_text(src.text or ""):
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


def _outside_dates(text: str) -> str:
    """``text`` with its dates blanked: a day or month number of a date states no value."""
    for p in sorted(shared_dates.find_all(text), key=lambda x: x.span[0], reverse=True):
        text = text[:p.span[0]] + " " + text[p.span[1]:]
    return text


def _item_dates(item: Item) -> list[str]:
    """The dates (ISO) the item has: those in its own words (label, value, condition); when none, the one date of its quote."""
    own = shared_dates.find_all(f"{item.label} ; {item.value} ; {item.condition}")
    if own:
        return list(dict.fromkeys(p.iso for p in own))
    quoted = shared_dates.find_all(item.quote)
    return [quoted[0].iso] if len(quoted) == 1 else []


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


_TOKEN = re.compile(r"[^\W\d_]+")


def _enough(wanted: str, have: frozenset[str]) -> bool:
    """At least half (rounded up) of the content words of ``wanted`` are stated in ``have`` (the content words of the unit,
    with their variants: ``claim_terms``). A word counts once, whichever of its variants the unit writes, so a spelling
    variant does not count twice; a text with no content word asks for nothing."""
    groups = [g for t in _TOKEN.findall(wanted) if (g := claim_terms(t)[1])]
    return not groups or sum(1 for g in groups if g & have) >= (len(groups) + 1) // 2


@functools.lru_cache(maxsize=1024)
def _number_forms(text: str) -> tuple[frozenset[str], ...]:
    """The forms of each number ``text`` writes, outside its dates (``verify._value_forms``): a number written with a scale word has the
    forms of its full value too, so "16.07 מיליון" and "16,070,000" are one number (round 18 U7, KTD6). A function of the text alone, kept
    per text: the ledger reads the same sentence against every item."""
    blank = _outside_dates(text)
    return tuple(frozenset(forms) for m in _NUM_AT.finditer(blank) if (forms := _value_forms(blank, m)))


def _states_value(item: Item, text: str, words: frozenset[str]) -> bool:
    if parsed := shared_dates.parse(item.value):
        return any(p.iso.startswith(parsed.iso) for p in shared_dates.find_all(text))
    wanted = _number_forms(item.value)
    if wanted:
        have = set().union(*_number_forms(text))
        return all(forms & have for forms in wanted)
    return _enough(item.value or item.label or item.quote, words)


def _states_date(item: Item, text: str, lead: str = "") -> bool:
    """The item's date is stated by the sentence, or, when the sentence states no date of its own, by its lead: the lead-in sentence of
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


def _unit_covers(item: Item, unit: Unit, ws, shared: bool, lead: str = "") -> str:
    """"" when the unit gives the item, else the first check it fails. ``lead``: the text around the unit that dates it (``_leads``)."""
    if not _attributed(item, unit, ws):
        return "not_attributed"
    words = claim_terms(unit.text)[1]
    if not _states_value(item, unit.text, words):
        return "value_not_stated"
    if not _states_date(item, unit.text, lead):
        return "date_not_stated"
    if shared and not _enough(item.condition or item.label, words):
        return "condition_not_stated"
    return ""


def _coverage_all(item: Item, units: Sequence[Unit], ws, shared: bool, leads: Mapping[int, str]) -> tuple[Coverage, frozenset[int]]:
    """``_coverage`` and, from the same pass over the units, every unit that gives the item (``Ledger.outcomes``): the coverage names
    the first of them."""
    checks = [(unit.index, _unit_covers(item, unit, ws, shared, leads.get(unit.index, ""))) for unit in units]
    giving = [index for index, why in checks if not why]
    if giving:
        return Coverage(True, giving[0], ""), frozenset(giving)
    furthest = max((_WHY_ORDER.index(why) for _, why in checks), default=-1)
    return Coverage(False, None, _WHY_ORDER[furthest] if furthest >= 0 else "not_attributed"), frozenset()


def _coverage(item: Item, units: Sequence[Unit], ws, shared: bool, leads: Mapping[int, str] | None = None) -> Coverage:
    """Whether a surviving unit of the answer gives the item (R5; see the module docstring). ``shared``: another item of the item's
    component has its signature (its value, or its label), which makes its condition words (else its label's) needed. ``leads``: the
    text around each unit that dates it (``_leads``), when the caller already has it."""
    furthest = -1
    leads = _leads(units) if leads is None else leads
    for unit in units:
        why = _unit_covers(item, unit, ws, shared, leads.get(unit.index, ""))
        if not why:
            return Coverage(True, unit.index, "")
        furthest = max(furthest, _WHY_ORDER.index(why))
    return Coverage(False, None, _WHY_ORDER[furthest] if furthest >= 0 else "not_attributed")


# --- the turn's ledger ---------------------------------------------------------------------------------------------------------

class Ledger:
    """The turn's items across answer versions, keyed by (component, source, verbatim quote), with the items excluded so far."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str], Item] = {}
        self.excluded: list[dict] = []
        self.misattributed: list[dict] = []
        self._ids: dict[tuple[str, str, str], str] = {}
        self._candidate_ids: dict[tuple[str, str], str] = {}

    def candidate_id(self, source: str, flat: str) -> str:
        """The K# of a candidate (its source and verbatim quote): the same in every call of the turn."""
        return self._candidate_ids.setdefault((source, flat), f"K{len(self._candidate_ids) + 1}")

    def id_for(self, key: tuple[str, str, str]) -> str:
        """The I# of a key: the same for the same key in every version."""
        return self._ids.setdefault(key, f"I{len(self._ids) + 1}")

    def merge(self, kept: Iterable[Item], *, excluded: Iterable[dict] = (), version: int = 0,
              misattributed: Iterable[dict] = ()) -> list[Item]:
        """Merge one answer version's valid items (and record what it excluded and the conditions it misattributed); the ledger's
        items, in the order first seen. An item already in the ledger takes its new declaration from the agent (state, reason,
        label, value, condition) — a later version may close it with a valid ``not_verifiable`` or ``not_applicable``, or state it
        ``included`` again; the judge's support and the server's source spans never change an item the ledger already holds. An item
        not listed again stays. An item the agent declares replaces the origin ``source`` items that hold the same span (round 18
        U5): the span's condition is then checked once, as the agent's item."""
        for item in kept:
            item.id = item.id or self.id_for(item.key)
            old = self.items.get(item.key)
            if item.origin == ORIGIN_AGENT:
                for key in [k for k, i in self.items.items()
                            if i.origin == ORIGIN_SOURCE and k != item.key and k[1] == item.key[1]
                            and (k[2] in item.key[2] or item.key[2] in k[2])]:
                    del self.items[key]
            if old is None:
                self.items[item.key] = item
            elif item.origin == ORIGIN_AGENT:
                old.label, old.value, old.condition = item.label or old.label, item.value or old.value, item.condition or old.condition
                old.state, old.reason = item.state, item.reason
                if old.origin == ORIGIN_SOURCE:
                    old.origin, old.candidate = ORIGIN_AGENT, False
        self.excluded += [{"version": version, **e} for e in excluded]
        self.misattributed += [{"version": version, **m} for m in misattributed]
        return list(self.items.values())

    def outcomes(self, units: Sequence[Unit], ws) -> list[dict]:
        """Every item with its state against these surviving units: ``covered`` (a unit gives it), ``omitted`` (an included or
        judge-added item no unit gives), or the ``not_verifiable`` / ``not_applicable`` the agent declared with a reason. Round 18
        U5: a covered item also has its condition check against the unit that gives it (``condition_check``: "" without a
        material condition, else ``kept``, ``missing``, ``undecided`` or ``hint``; ``condition_quote``: for ``missing``, the clause of the span the unit leaves
        out; ``condition_hint``: ``contradicted`` and/or ``other_item``;
        ``condition_spans``: its spans; ``condition_foreign``: the items whose condition the sentence states; ``condition_unit``:
        the unit checked). An origin ``source`` item no surviving unit gives is not an open item: the sentence that cited its span
        is gone, so it is left out; a ``candidate`` item (round 18 U6) is the exception: a component asks for it, so it stays
        open (``omitted``) until a sentence gives it."""
        out = []
        # each item's signature is computed once, and the items of a component that share one are counted: an item's condition is
        # asked when another item of its component has its signature (``_coverage``)
        signatures = {id(i): _signature(i) for i in self.items.values()}
        alike = Counter((i.component, signatures[id(i)]) for i in self.items.values())
        by_index = {u.index: u for u in units}
        leads = _leads(units)
        spans = {i.key: _spans_of(ws, i) for i in self.items.values()}
        rows: list[tuple[Item, Coverage | None]] = []
        gave: dict[tuple[str, str, str], frozenset[int]] = {}  # the units that give each covered item (the first is the covering unit)
        for item in self.items.values():
            sig = signatures[id(item)]
            shared = bool(sig) and alike[(item.component, sig)] > 1
            got = None
            if item.state in (ITEM_INCLUDED, OMITTED):
                got, covering = _coverage_all(item, units, ws, shared, leads)
                if got.ok:
                    gave[item.key] = covering
            if is_server_item(item) and (got is None or not got.ok):
                continue  # (a candidate the call assigned to a component is an open item whatever sentence cited what: round 18 U6)
            rows.append((item, got))
        owns = {i.key: _own_text(ws, i) for i, _ in rows}  # each item's own context text
        others = [i for i, _ in rows]
        for item, got in rows:
            state = item.state if got is None else (COVERED if got.ok else OMITTED)
            check = _condition_check(item, got, spans, gave, owns, by_index, others)
            out.append({**item.public(), "state": state, "declared": item.state, "covered_by": got.unit if got else None,
                        "why": got.why if got else "", **check})
        return out


# --- the condition outcome of a covered item (round 18 U5) ---------------------------------------------------------------------

_NO_CHECK = {"condition_check": "", "condition_hint": [], "condition_spans": [], "condition_foreign": [], "condition_unit": None,
             "condition_quote": ""}
_RANK = {conditions.KEPT: 0, conditions.UNDECIDED: 1, conditions.MISSING: 2, conditions.HINT: 3}


def _spans_of(ws, item: Item) -> list[Span]:
    src = ws.sources.get(item.source)
    return condition_spans(src, item.quote) if src is not None else []


def _own_text(ws, item: Item) -> str:
    src = ws.sources.get(item.source)
    return _own_context(src, item.quote) if src is not None else item.quote


def _condition_check(item: Item, got: Coverage | None, spans: dict, gave: dict, owns: dict, by_index: dict[int, Unit],
                     others: list[Item]) -> dict:
    """The condition check of a covered item (``Ledger.outcomes``): each of its spans against the sentence that gives it
    (``conditions.outcome``) — the worst of them, a hint over an undecided over a kept; a sentence that states an operator clause
    of another item's span that the item's own context does not hold is a hint from that item. Among several units that give the
    item the one that keeps its condition wins, else the first."""
    mine = spans[item.key]
    if got is None or not got.ok:
        return dict(_NO_CHECK)
    best = None
    for index in sorted(gave.get(item.key, {got.unit})):
        unit = by_index[index]
        read = [conditions.read(sp.text, unit.text) for sp in mine]  # (outcome, quote) of each span
        results = [outcome for outcome, _ in read]
        hints = ["contradicted"] if conditions.HINT in results else []
        foreign = _foreign_items(item, unit, spans, gave, owns, others)
        if foreign:
            hints.append("other_item")
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
    return best or dict(_NO_CHECK)


@functools.lru_cache(maxsize=4096)
def _span_kept(text: str, sentence: str) -> bool:
    """Whether a span states an operator clause that limits and the sentence keeps it: a function of the span and the sentence alone, so
    it is decided once per (span, sentence), whichever item asks."""
    return bool(conditions.material_clauses(text)) and conditions.outcome(text, sentence) == conditions.KEPT


def _foreign_items(item: Item, unit: Unit, spans: dict, gave: dict, owns: dict, others: list[Item]) -> list[str]:
    """The items whose operator clauses the sentence states and the item's own context does not hold: the sentence gives this item
    a condition of another item. Another item the same sentence gives itself is no source of one."""
    found = []
    for other in others:
        if other.key == item.key or unit.index in gave.get(other.key, ()):
            continue
        for sp in spans[other.key]:
            if _span_kept(sp.text, unit.text) and not conditions.stated_in(sp.text, owns[item.key]):
                found.append(other.id)
                break
    return found
