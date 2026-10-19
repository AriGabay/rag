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

The wiring into the requirements call, the outcomes and the repair is round 17 U3's."""

from __future__ import annotations

import functools
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from app.answering.verify import numbers_in
from app.chat.evidence import claim_terms
from app.chat.verify import (
    ITEM_COVERED,
    ITEM_INCLUDED,
    ITEM_NOT_APPLICABLE,
    ITEM_NOT_VERIFIABLE,
    ITEM_OMITTED,
    ORIGIN_AGENT,
    ORIGIN_JUDGE_SUPPORT,
    SUPPORT_QUOTE_MAX,
    SupportScope,
    Unit,
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

    @property
    def key(self) -> tuple[str, str, str]:
        """The item's identity across answer versions: its component, its source and its verbatim quote."""
        return self.component, self.source, SupportScope.flat(self.quote)

    def public(self) -> dict:
        return {"id": self.id, "component": self.component, "label": self.label, "source": self.source,
                "quote": self.quote, "value": self.value, "condition": self.condition, "state": self.state,
                "reason": self.reason, "origin": self.origin, "document_id": self.document_id, "unit_id": self.unit_id}


class Validation(NamedTuple):
    kept: list[Item]
    excluded: list[dict]  # [{"component", "source", "quote", "state", "reason": why it was excluded}]


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
                item = Item(component, f["label"], sid, f["quote"], f["value"], f["condition"], f["state"], f["reason"],
                            document_id=document_id, unit_id=unit_id)
                item.id = ledger.id_for(item.key) if ledger is not None else f"I{next(numbers)}"
                kept.append(item)
                continue
        excluded.append({"component": f["component"], "source": f["source"], "quote": f["quote"][:SUPPORT_QUOTE_MAX],
                         "state": f["state"], "reason": why})
    return Validation(kept, excluded)


def support_item(component: str, source: str, quote: str, ledger: Ledger, *, label: str = "", value: str = "",
                 condition: str = "", ws=None) -> Item:
    """An item the server adds from the judge's validated support (round 16 U2): a member the answer omits, state ``omitted``,
    origin ``judge_support``. Its identity is the ledger's, so the agent listing the same part later is the same item."""
    item = Item(component, label, source, quote, value, condition, OMITTED, "", ORIGIN_JUDGE_SUPPORT)
    if ws is not None and (src := ws.sources.get(source)) is not None:
        item.document_id, item.unit_id = _site_of(src)
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
            if cid == item.source or src.same_as == item.source or ws.sources[item.source].same_as == cid \
                    or flat in _flat_text(src.text or ""):
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


def _states_value(item: Item, text: str, words: frozenset[str]) -> bool:
    if parsed := shared_dates.parse(item.value):
        return any(p.iso.startswith(parsed.iso) for p in shared_dates.find_all(text))
    wanted = numbers_in(_outside_dates(item.value))
    if wanted:
        return wanted <= numbers_in(_outside_dates(text))
    return _enough(item.value or item.label or item.quote, words)


def _states_date(item: Item, text: str) -> bool:
    stated = [p.iso for p in shared_dates.find_all(text)]
    return all(any(s.startswith(iso) for s in stated) for iso in _item_dates(item))


def _unit_covers(item: Item, unit: Unit, ws, shared: bool) -> str:
    """"" when the unit gives the item, else the first check it fails."""
    if not _attributed(item, unit, ws):
        return "not_attributed"
    words = claim_terms(unit.text)[1]
    if not _states_value(item, unit.text, words):
        return "value_not_stated"
    if not _states_date(item, unit.text):
        return "date_not_stated"
    if shared and not _enough(item.condition or item.label, words):
        return "condition_not_stated"
    return ""


def _coverage(item: Item, units: Sequence[Unit], ws, shared: bool) -> Coverage:
    """Whether a surviving unit of the answer gives the item (R5; see the module docstring). ``shared``: another item of the item's
    component has its signature (its value, or its label), which makes its condition words (else its label's) needed."""
    furthest = -1
    for unit in units:
        why = _unit_covers(item, unit, ws, shared)
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
        self._ids: dict[tuple[str, str, str], str] = {}

    def id_for(self, key: tuple[str, str, str]) -> str:
        """The I# of a key: the same for the same key in every version."""
        return self._ids.setdefault(key, f"I{len(self._ids) + 1}")

    def merge(self, kept: Iterable[Item], *, excluded: Iterable[dict] = (), version: int = 0) -> list[Item]:
        """Merge one answer version's valid items (and record what it excluded); the ledger's items, in the order first seen.
        An item already in the ledger takes its new declaration from the agent (state, reason, label, value, condition) — a
        later version may close it with a valid ``not_verifiable`` or ``not_applicable``, or state it ``included`` again; the
        judge's support never changes an item the ledger already holds. An item not listed again stays."""
        for item in kept:
            item.id = item.id or self.id_for(item.key)
            old = self.items.get(item.key)
            if old is None:
                self.items[item.key] = item
            elif item.origin == ORIGIN_AGENT:
                old.label, old.value, old.condition = item.label or old.label, item.value or old.value, item.condition or old.condition
                old.state, old.reason = item.state, item.reason
        self.excluded += [{"version": version, **e} for e in excluded]
        return list(self.items.values())

    def outcomes(self, units: Sequence[Unit], ws) -> list[dict]:
        """Every item with its state against these surviving units: ``covered`` (a unit gives it), ``omitted`` (an included or
        judge-added item no unit gives), or the ``not_verifiable`` / ``not_applicable`` the agent declared with a reason."""
        out = []
        # each item's signature is computed once, and the items of a component that share one are counted: an item's condition is
        # asked when another item of its component has its signature (``_coverage``)
        signatures = {id(i): _signature(i) for i in self.items.values()}
        alike = Counter((i.component, signatures[id(i)]) for i in self.items.values())
        for item in self.items.values():
            sig = signatures[id(item)]
            got = (_coverage(item, units, ws, bool(sig) and alike[(item.component, sig)] > 1)
                   if item.state in (ITEM_INCLUDED, OMITTED) else None)
            state = item.state if got is None else (COVERED if got.ok else OMITTED)
            out.append({**item.public(), "state": state, "declared": item.state, "covered_by": got.unit if got else None,
                        "why": got.why if got else ""})
        return out
