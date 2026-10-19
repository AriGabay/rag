"""What an answer covered, stated by the server from what the turn's tools actually did.

The model declares only what kind of question it answered — about one document or property (``focused``) or
about a set (``set``, with the terms that define the set) — and which data it found but left out, and why. The
server knows the rest: the documents of the set (``find_documents``, or the whole repository from
``list_documents``), how deep the tools reached into each (``tools.LEVELS``: located, a passage retrieved, a
section or table read, its datum cited by the verified answer), and which were read only in part.

Reaching a document is not reading it: a search hit is one passage. The ledger keeps the levels apart, and the
note states document coverage (how many documents of the set the answer drew a verified datum from) apart from
data coverage (how many were read section by section, and from how many only passages were retrieved).

For a set, an answer that did not reach every document of the set, or that reached one and used nothing from it
without saying why, gets a coverage note naming them, and its status is at most ``partial``: a result is never
presented as covering the set when it covers part of it. An answer that covers every document from retrieved
passages only says so. For a focused answer to a question that names no document, when several documents share a
distinctive word of the question in their titles and the answer cites only some of them, the note names the
others. For a table it cites, the ledger records the rows it presented against the rows in the table.

What the request requires is frozen in ``verify.TurnRequirements``: the request's components before the answer
(round 7 KTD1), or, when the turn has none, the judge's derivation (KTD7). Each component's status in the verified
answer — full, partial, not answered, needs clarification, not relevant — is computed from the units that survived
verification, a parent's from its children (``VerifyReport.requirement_outcomes``, round 7 KTD3, R6, R7, R10).

One owner states each gap: the server (round 7 KTD4, R8, R9, R11). The model writes no absence sentence; its
``requested`` declarations are per-component status claims (``component``: the N# id), validated against what the
turn did (``validate_requested``) and only ever downgraded: they add evidence to the component's reason, never make a
component given. A component not given gets one reason from one vocabulary (``REASONS``), chosen from the turn's
evidence — the ids the judge named as related to it, the model's validated claim, the workspace — by the reason
table, first match wins (``reason_of``):

1. an instruction the shown answer does not meet — instruction not met;
2. filled only by units removed in verification — removed in verification, its sentence naming why when they share
   one failure kind (round 7 U4, KTD5: ``removal_kinds``, ``REMOVED_BECAUSE``; a unit not checked is never called
   wrong);
3. a tool or provider failure tied to it (``E#``) — tool failure;
4. a failed calculation tied to it (``F#``) — calculation not completed;
5. a calculation resting on a parameter the user did not give, which no user assumption (``A#``), no document rate a
   calculation of the turn applied and no computation of the component over document values and user assumptions
   only fills (round 7 KTD9; its status is then ``needs_clarification``), or a clarification component — a detail
   missing from the request;
6. a calculation whose inputs were found and never computed — calculation not completed (also a calculation component
   the judge scored full whose surviving units show no computation of the turn: it is partial, KTD3);
7. a value found with an uncertain reading, or whose kind or unit was asserted (round 13 U5) — found, not verifiable;
8. values of one datum that differ — sources conflict when their periods overlap or one has no date (``conflicting_pairs``,
   round 12 U11, R18, KTD13: a datum undated only because it is history or a comparable conflicts with nothing, and values of
   different non-overlapping dates are a history). One datum is one class of the timeline's decision (round 13 U4, R3, R4,
   KTD4; ``timeline.buckets``): the same property group (round 12 U6, KTD5), kind, unit, period, basis, scenario and status,
   and a facet the decision joins (``datum.relation``) — two components of one appraisal, or two values whose facets cannot be
   compared, never conflict;
9. the judge's "undeterminable" (an evidence state, not a status) — found, not verifiable (or sources conflict, 8);
10. data found that the verified answer does not present — found, not verifiable;
10-text. a part of a partly given or missing component that a passage the turn read states, with a verbatim quote the
    server validated (``verify.validated_support``, round 16 U2, KTD2) and the answer still omits — found, not verifiable,
    variant ``read_not_used``: the part was read at that place (the document, the section or pages, the S#) and not used.
    It never says "not found" (R6); a component whose quote was not valid keeps the wording below;
10-item. an item of the turn's item ledger (``items.Ledger``, round 17 U3, KTD3, R8) the answer does not give — listed by the agent
    and given by no surviving unit — or that the agent declared ``not_verifiable`` or ``not_applicable`` with a reason: its own
    line after the gap paragraph (``item_gap_lines``, the ``item_gap`` server span) naming the item, the place it was read at
    (document, location, S#) and, for a declared one, the reason. An omitted item makes its component partial
    (``VerifyReport.requirement_outcomes``) and, as the judge's validated support does, is a ``REQ_TEXT_FOUND`` completion for the
    repair round; it is never "not found". A declared item does not block ``full``;
10a. a professional question whose pool was empty — the office holds no standard or professional document (round 10 U8,
    KTD15, R29): the standard text is not in the repository — standard text absent (stated as the repository's gap, and
    the other permitted sources still answer);
11. a reading of the section, table or pages it concerns that was clipped or has an unread region, or a model claim
    validated as "read in part" — the relevant region was not fully read;
11a. the turn was stopped by a limit (time, reading budget, steps) before the datum was located — not located within
    the limit, saying which, and — for the time limit, when one unit was located — that the unit stays in focus (round 9
    R18, KTD10: ``limit_reached``); a file of several units where the turn located none — the appraisal was not located
    (``unit_not_located``). Both come before 12 and 14: a failure to look is never "not present";
12. a section, table or pages read to the end without it — not present in the part read (naming it, with its S#); a
    read of paragraphs or pages that returned every block of a section counts as reading that section (``_openings``);
13. a document it concerns read only in part — the relevant region was not fully read;
14. a search or reading covered it, nothing found — not located by the searches performed;
15. nothing covered it, after the repair bound — not located by the searches performed, saying no search covered it:
    never "not present", never "not found".

The server writes one compact gap paragraph after the answer (``state_components``): one line per reason (and place),
naming only the components not given — the leaves; a parent whose children are stated is not — so the request is
never restated, and no component gets two reasons. It is stored in the answer text (copy and history keep it); the
per-component detail (status, reason, evidence, place) is in the ledger's ``requirements`` for the UI, and the
completeness line names each gap with its status only. A unit the judge marked as stating the absence of a component
the server states, and an unmarked uncited absence sentence (the backstop), are removed first
(``VerifyReport.supersede``). A model claim with no component id is stated on its own only when the turn has no
requirements at all, so it can never double a component's statement. The answer is then tidied (``tidy``): no orphan
list marker and no repeated line is left by the removals and additions (R22).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Hashable, Sequence
from datetime import date
from typing import TYPE_CHECKING, NamedTuple

from app.chat import contexts
from app.chat.compose import line_cut
from app.chat.dates import COMPARABLE_ORIGIN, HISTORY_ORIGIN
from app.chat.intent import PROFESSIONAL
from app.chat.tools import (
    READ_TO_END,
    STEP_LIMIT,
    TIME_LIMIT,
    TOOL_BUDGET,
    dated,
    period_bounds,
    value_uncertain,
)
from app.db import tenant_tx

if TYPE_CHECKING:
    from app.chat.compose import Edited
    from app.chat.engine import FinalAnswer
    from app.chat.tools import Workspace
    from app.chat.verify import TurnRequirements

_CITE = re.compile(r"\[([SMCVA]\d+(?:\s*[,،;]\s*[SMCVA]\d+)*)\]")
NAMES_SHOWN = 8


def cited_ids(markdown: str) -> set[str]:
    """The S#, M#, V#, A# and C# ids the answer cites."""
    ids: set[str] = set()
    for m in _CITE.finditer(markdown or ""):
        ids |= set(re.findall(r"[SMCVA]\d+", m.group(1)))
    return ids


def cited_documents(ws: Workspace, markdown: str) -> dict[str, str]:
    """Document id -> title of every source, measurement, value and calculation input the answer cites."""
    out: dict[str, str] = {}
    for i in cited_ids(markdown):
        if i in ws.sources:
            if not ws.sources[i].is_listing:  # a listing names documents; it is not one of them
                out[str(ws.sources[i].document_id)] = ws.sources[i].title
        elif i in ws.measurements:
            out[str(ws.measurements[i].document_id)] = ws.measurements[i].title
        elif i in ws.values:
            out[str(ws.values[i].document_id)] = ws.values[i].title
        elif i in ws.computations:  # the documents of the values and measurements it rests on
            for leaf in ws.computations[i].leaves:
                if leaf in ws.measurements:
                    out[str(ws.measurements[leaf].document_id)] = ws.measurements[leaf].title
                elif leaf in ws.values:
                    out[str(ws.values[leaf].document_id)] = ws.values[leaf].title
    return out


LEDGER_DOCUMENT_KEYS = ("cited", "matching", "read", "retrieved_only", "with_data", "not_checked", "unused",
                        "partially_read", "omitted", "also_matching", "tables", "requirements", "gap_documents",
                        "unread_documents")


def ledger_documents(ledger: dict | None) -> set[str]:
    """Every document id a coverage ledger names, under any of its keys (the permission check reads this)."""
    ids: set[str] = set()
    for key in LEDGER_DOCUMENT_KEYS:
        ids |= {d.get("document_id") for d in (ledger or {}).get(key) or [] if isinstance(d, dict)}
    ids.discard(None)
    return ids


def _level(ws: Workspace, document_id: str, verified: Collection[str] = ()) -> str:
    """The deepest level the turn reached in a document; one in ``verified`` (the documents the final text cites) is
    ``verified`` whether or not the engine has recorded it yet (``touch_verified``)."""
    from app.chat.tools import NOT_REACHED

    return "verified" if document_id in verified else (ws.activity.get(document_id) or {}).get("level", NOT_REACHED)


def _read(ws: Workspace, document_id: str) -> bool:
    """A section or table of the document was opened, or its stored measurements listed."""
    return bool((ws.activity.get(document_id) or {}).get("read"))


def less_named(question: str, others: dict[str, str], cited: dict[str, str]) -> dict[str, str]:
    """``others`` (id -> title) without the documents whose titles carry fewer of the question's words than a cited
    document's title: a title sharing "אזור התעשייה" with a question that also names the city of the cited one
    matches the question less, however either title spells the shared words."""
    from app.platform.search import question_words_in_title

    words = {i: question_words_in_title(question, t) for i, t in {**others, **cited}.items()}
    best = max((len(words[c]) for c in cited), default=0)
    return {i: t for i, t in others.items() if len(words[i]) >= best}


def _names(docs: list[dict]) -> str:
    shown = "; ".join(d["title"] for d in docs[:NAMES_SHOWN])
    return shown + (f" ועוד {len(docs) - NAMES_SHOWN}" if len(docs) > NAMES_SHOWN else "")


# the reasons a component is not given (R8): one vocabulary, in the order of the reason table (KTD4)
REASONS = {
    "instruction_not_met": "הוראה שלא קוימה",
    "removed": "הוסר באימות",
    "tool_failure": "תקלה בכלי או בשירות המודל",
    "calculation_incomplete": "החישוב לא הושלם",
    "detail_missing": "חסר פרט בבקשה",
    "not_verifiable": "נמצא, אך לא ניתן לאמת או להכריע",
    "sources_conflict": "המקורות סותרים",
    "document_not_read": "המסמך שנשאל עליו לא נקרא",
    "region_not_read": "האזור הרלוונטי לא נקרא במלואו",
    "standard_absent": "נוסח התקן אינו במאגר",
    "limit_reached": "לא אותר במסגרת מגבלות השאלה",
    "unit_not_located": "השומה שנשאלה עליה לא אותרה",
    "not_in_part_read": "לא מופיע בחלק שנקרא ונבדק",
    "not_located": "לא אותר בחיפושים שבוצעו",
}
_ORDER = tuple(REASONS)
# why a component's units were removed (round 7 KTD5, R12): the variant of "removed" is their one failure kind
REMOVED_BECAUSE = {
    "": "כי לא נמצאה לו תמיכה במקורות.",
    "absent_from_source": "כי המקורות שנבדקו אינם מציינים אותו.",
    "wrong_subject": "כי הנתון שבו שייך לנכס או לצד אחר.",
    "wrong_unit": "כי היחידה, התקופה, בסיס השטח או המע\"מ שלו אינם כבמקור.",
    "uncertain_reading": "כי הוא נשען על קריאה לא ודאית של המקור.",
    "contradicts_source": "כי הוא סותר את המקור.",
    "context_not_read": "כי ההקשר הדרוש לבדיקתו לא נקרא (הוא לא נמצא שגוי).",
    "wrong_calculation": "כי החישוב או הקלטים שלו שגויים.",
    "invalid_citation": "כי הוא ציטט מקור שלא נבדק בתור הזה.",
    "not_checked": "כי לא ניתן היה לבדוק אותו מול המקורות (הוא לא נמצא שגוי).",
}
# the limits of a turn that stop it before the datum is located (R18), in the order the sentence prefers; INSPECT_LIMIT
# stops only the visual readings and is no reason by itself
LIMITS = (TIME_LIMIT, TOOL_BUDGET, STEP_LIMIT)
LIMIT_WHERE = {TIME_LIMIT: "מגבלת הזמן של השאלה", TOOL_BUDGET: "מגבלת היקף הקריאה של השאלה",
               STEP_LIMIT: "מספר הצעדים המרבי של השאלה"}
# what a gap line adds after a time limit when one unit was located: the conversation stays in it (design lens, R18)
UNIT_IN_FOCUS = "{unit} נשאר בפוקוס; אפשר לבקש להמשיך לקרוא."
# a component whose text is a clause of the request, with no noun phrase to name it: the datum asked for
GENERIC_LABEL = "הנתון המבוקש"
# the item lines after the answer (round 17 U3, KTD3, R8): at most this many lines (the rest is counted in one), an item's name
# and the agent's reason clipped to these lengths (the reason is the model's, in a server line: never more than a short clause)
ITEM_GAP_LINES = 8
ITEM_NAME_CHARS = 80
ITEM_REASON_CHARS = 160
# a model claim's status -> the reason it supports, once validated
_KIND_OF_STATUS = {"not_found_search": "not_located", "source_partial": "region_not_read",
                   "section_checked_absent": "not_in_part_read", "sources_conflict": "sources_conflict"}
# when one datum is claimed missing more than once: the claim the turn supports most specifically
_STRENGTH = ("section_checked_absent", "sources_conflict", "source_partial", "not_found_search")
UNMET = ("not_answered", "partial", "needs_clarification")


def _norm_label(label: str) -> str:
    return " ".join(re.sub(r"[*_`]+", " ", label or "").split()).strip(" :.").casefold()


class Comparable(NamedTuple):
    """One datum as the conflict rule reads it: ``key`` (everything that must be equal for two data to be the same datum —
    property group, family, unit, period, basis, scenario, stance class), its ``amount`` and its ``as_of``."""

    key: Hashable
    amount: object
    as_of: dict | None


def _span(as_of: dict | None) -> tuple[date, date] | None:
    """The first and the last day of a datum's period (a range: up to the last day of its end); None when it has no date."""
    when = dated(as_of)
    if when is None:
        return None
    iso, precision, end = when
    return period_bounds(iso, precision, end)


def _undated_by_origin(as_of: dict | None) -> bool:
    """Undated only because it is history or a comparable (round 12 U4, KTD12): such a datum conflicts with nothing."""
    return bool(as_of) and not as_of.get("date") and as_of.get("date_origin") in (HISTORY_ORIGIN, COMPARABLE_ORIGIN)


def _clash(a: Comparable, b: Comparable, first: tuple[date, date] | None, second: tuple[date, date] | None) -> bool:
    """The pair rule: different amounts, and periods that overlap or one of which is unknown (unless the one without a date is
    undated only because it is history or a comparable). ``first``, ``second``: the spans of ``a`` and ``b``."""
    if a.amount == b.amount or _undated_by_origin(a.as_of) or _undated_by_origin(b.as_of):
        return False
    return first is None or second is None or (first[0] <= second[1] and second[0] <= first[1])


def clashing(a: Comparable, b: Comparable) -> bool:
    """Whether two data have different amounts in overlapping periods — the condition of a conflict between two data of one
    datum, and of the ``undecided`` mark between two that could not be compared (round 13 U4, R6)."""
    return _clash(a, b, _span(a.as_of), _span(b.as_of))


def conflicting_pairs(items: Sequence[Comparable]) -> list[tuple[int, int]]:
    """The pairs (indexes into ``items``, ``i < j``) that conflict: the one conflict rule of the answer and its timeline
    (round 12 U11, R18, KTD13; ``conflicting`` and ``timeline.build`` both read it, and with ``timeline.adjustment_conflicts``, which
    decides the pairs across a coefficient and a proportion, nothing else decides a conflict).

    Two data of one ``key`` with different amounts conflict when their periods overlap (a year and a day inside it overlap;
    a range runs to its end) or when either has no date, unless the one without a date is undated only because it is history
    or a comparable (``date_origin``). Two dated data whose periods do not overlap are a history, not a conflict. Each pair
    is decided on its own: a third value that differs from only one of them conflicts with that one only.

    Round 13 U4 (KTD4): the ``key`` is the datum class (``timeline.buckets``): this is the pair rule within one datum, never a
    second way to decide which data are one."""
    by_key: dict[Hashable, list[int]] = {}
    for i, item in enumerate(items):
        by_key.setdefault(item.key, []).append(i)
    pairs = []
    spans: dict[int, tuple[date, date] | None] = {}

    def span_of(k: int) -> tuple[date, date] | None:
        if k not in spans:
            spans[k] = _span(items[k].as_of)
        return spans[k]

    for members in by_key.values():
        for n, i in enumerate(members):
            for j in members[n + 1:]:
                if _clash(items[i], items[j], span_of(i), span_of(j)):
                    pairs.append((i, j))
    return sorted(pairs)


def conflicting(ws: Workspace, document_ids: list[str] | None = None, ids: Collection[str] | None = None) -> bool:
    """Whether values the turn registered (V#) or measurements it listed (M#) give one datum different amounts: true only for
    a conflict pair inside a datum class (round 13 U4, R3, R4, KTD4) — the classes of ``timeline.buckets``, the same the
    timeline shows — or a coefficient and a proportion of one consideration that are not one adjustment
    (``timeline.adjustment_conflicts``, round 14 review #3), so this is true exactly when the timeline shows a conflict mark
    (round 12 U11, R18, KTD13: R8).
    Two components of one appraisal (decided different), two data whose facets could not be compared (undecided), two
    properties, two scenarios, a party's claim and the decision are not one datum and never conflict; a datum undated only
    because it is history or a comparable conflicts with nothing. Only documents in ``document_ids``, and only the ids in
    ``ids``, when given — the classes are still the whole turn's, so a filter never changes which values are one datum.

    The property is the value's group in the turn's property graph (``timeline.identity_of``); a value the graph holds in no
    group keeps its own identity (its document and unit) and matches no other, as in the timeline. A field the source does not
    state (the kind, the unit, the VAT of an amount of money, a recurring period) makes the value match no other."""
    from app.chat import timeline

    def kept(d: timeline.Datum) -> bool:
        return not ((document_ids and d.document_id not in document_ids) or (ids is not None and d.ref not in ids))

    everything = timeline.datums(ws)
    # only a base key's fields that hold two different amounts among the data asked about can conflict: the property of a
    # datum (the office's property graph) is read only there
    amounts: dict[tuple, set] = {}
    for d in everything:
        if d.fields is not None and kept(d):
            amounts.setdefault(d.fields, set()).add(d.comparable)
    open_fields = {f for f, seen in amounts.items() if len(seen) > 1}
    asked = [d.ref for d in everything if d.fields in open_fields and kept(d)]
    if asked and any(bucket.conflicts(kept) for bucket in timeline.buckets(ws, asked, everything=everything)):
        return True
    # the other half of the one rule (round 14 review #3): a coefficient and a proportion that are not one adjustment are in two
    # families, hence two buckets, and the table marks them ``conflict``
    return bool(timeline.adjustment_conflicts(ws, kept, everything=everything))


def validate_requested(ws: Workspace, requested, opened: dict[str, tuple[str, dict]] | None = None) -> list[dict]:
    """Each requested datum (a model claim, linked to its component by ``component``) with the status the turn's
    actions support. "The section was checked" needs a section, table or page range of one of its documents opened
    this turn and named in ``checked_where`` (a measurements listing is not a reading of the section), read to its
    end with no unread region in it (R12): one read only in part — clipped and not continued to the end, or with a
    region that was not read — supports only "the source was read in part" (``partial_reason``: ``clipped`` or
    ``unread``). "Read in part" otherwise needs one of its documents read in part. "The sources conflict" needs values
    of the turn that conflict (``conflicting``); without them the datum was found. An unsupported status falls to the
    strongest one the turn does support, down to "not found in the search" — never up. ``kind`` is the reason it
    supports (``REASONS``), None when found; ``searched``: the turn made a search or opened one of its documents.
    ``opened``: the turn's openings (``_openings``), when the caller already has them."""
    out = []
    for r in requested or []:
        docs = [d for d in r.document_ids if d in ws.activity]
        status, where, reason = r.status, None, None
        if status == "section_checked_absent":
            # an opening of one of its documents (an S# is one document's)
            found = (_openings(ws, docs) if opened is None else opened).get((r.checked_where or "").strip())
            where = found[1] if found is not None and found[0] in docs else None
            if where is None:
                status = "source_partial"
            elif ws.read_complete(where.get("target")) is False:
                st = ws.reads[where["target"]]
                status, reason = "source_partial", "unread" if st["to"] == READ_TO_END else "clipped"
        if status == "source_partial" and reason is None and not any(ws.activity[d]["partial"] for d in docs):
            status = "not_found_search"
        if status == "sources_conflict" and not conflicting(ws, docs or None):
            status = "found"
        partial = next((ws.activity[d]["title"] for d in docs if ws.activity[d]["partial"]), None)
        out.append({"component": (r.component or "").strip(), "label": r.label.strip(),
                    "document_ids": docs, "status": status, "claimed": r.status,
                    "checked_where": where["sid"] if where else None, "section": where["name"] if where else None,
                    "scope": where["scope"] if where else None, "partial_document": partial,
                    "partial_reason": reason, "kind": _KIND_OF_STATUS.get(status),
                    "searched": bool(ws.searches) or any(ws.activity[d].get("read") for d in docs)})
    return out


def _place(scope: str | None, name: str | None) -> tuple[str, str, str]:
    """Where a datum was looked for: (in it, of it, what it is) — "בסעיף "X"", "מהסעיף "X"", "הסעיף"."""
    scope, name = scope or "section", name or ""
    if scope == "pages":
        return f"ב{name}", f"מ{name}", "הטווח"
    if scope == "table":
        return f"בטבלה \"{name}\"", f"מהטבלה \"{name}\"", "הטבלה"
    if scope == "sections":  # several sections a read returned whole: their names, quoted
        return f"בסעיפים {name}", f"מהסעיפים {name}", "הסעיפים"
    return f"בסעיף \"{name}\"", f"מהסעיף \"{name}\"", "הסעיף"


def _read_at(document: str | None, detail: str, sid: str | None) -> str:
    """Where a part was read, as the lines about a part read and not used word it: ב"document" (location) [S#]."""
    return f'ב"{document}"' + (f" ({detail})" if detail else "") + (f" [{sid}]" if sid else "")


def _label(text: str) -> str:
    return "**" + " ".join(re.sub(r"\*+", " ", text or "").split()) + "**"


def _labels(texts: list[str]) -> str:
    """"**א**", "**א** ו**ב**", "**א**, **ב** ו**ג**"."""
    marked = [_label(t) for t in texts]
    return marked[0] if len(marked) == 1 else ", ".join(marked[:-1]) + " ו" + marked[-1]


def gap_sentence(reason: str, variant: str, texts: list[str], place: dict | None = None,
                 document: str | None = None, detail: str = "") -> str:
    """The server's one sentence for the components ``texts`` that share a reason (``REASONS``), a variant of it and a
    place; each reason says what the turn did, never more ("not present" only for a part read to its end)."""
    many = len(texts) > 1
    who = _labels(texts)
    in_it, of_it, what = _place((place or {}).get("scope"), (place or {}).get("name"))
    sid = (place or {}).get("sid")
    if reason == "instruction_not_met":
        lead = "הוראות שלא קוימו בתשובה" if many else "הוראה שלא קוימה בתשובה"
        return f"{lead}: {who}" + (f" (נתון בלי מראה מקום: «{detail}»)" if detail else "") + "."
    if reason == "removed":
        return f"{who}: מה שנכתב על כך בתשובה הוסר באימות, " + REMOVED_BECAUSE.get(variant, REMOVED_BECAUSE[""])
    if reason == "tool_failure":
        return f"{who} לא {'הושלמו' if many else 'הושלם'} בגלל תקלה בכלי או בשירות המודל בזמן הבדיקה."
    if reason == "calculation_incomplete" and variant == "failed":
        return f"{who}: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."
    if reason == "calculation_incomplete":
        return f"{who}: הנתונים לחישוב נמצאו, אבל החישוב לא הושלם."
    if reason == "detail_missing":
        return f"{who}: חסר פרט שהמשתמש צריך לתת" + (f" ({detail})" if detail else "") + ", ולכן לא נקבעה תוצאה."
    if reason == "not_verifiable" and variant == "uncertain":
        return f"{who}: נמצא ערך, אבל קריאתו או משמעותו אינן ודאיות, ולכן הוא לא הוצג כנתון מאומת."
    if reason == "not_verifiable" and variant == "read_not_used":
        # round 16 U2, R6: the part was read at a place and the answer does not give it - what the turn did, never "not found"
        return f"{who}: {'החלקים נקראו' if many else 'החלק נקרא'} {_read_at(document, detail, sid)}, ולא נעשה בו שימוש בתשובה."
    if reason == "not_verifiable" and variant == "undeterminable":
        return f"{who}: המסמכים אינם מספיקים כדי להכריע בכך."
    if reason == "not_verifiable":
        return f"{who}: נמצאו נתונים, אבל הם לא הוצגו בתשובה כנתון מאומת."
    if reason == "sources_conflict":
        return f"{who}: המקורות סותרים — הם נותנים {'להם' if many else 'לנתון'} ערכים שונים, ולכן אין " \
               f"{'להם' if many else 'לו'} ערך אחד."
    if reason == "document_not_read":
        # a document the user may see whose latest version was not read (KTD11): what the turn could not do, never
        # that the datum is not in it (R18)
        if variant == "many":
            return f"{who}: המסמכים \"{document}\" לא נקראו ({detail}), ולכן לא נבדק אם הנתון מופיע בהם."
        return f"{who}: המסמך \"{document}\" לא נקרא ({detail}), ולכן לא נבדק אם הנתון מופיע בו."
    if reason == "standard_absent":
        return f"{who}: נוסח התקן אינו במאגר, ולכן לא נבדק מה התקן עצמו קובע בעניין הזה."
    if reason == "region_not_read" and variant == "clipped":
        return (f"{who} לא {'נמצאו' if many else 'נמצא'} בחלק שנקרא {of_it}: {what} נקרא רק בחלקו, ולכן ייתכן "
                "שהנתון מופיע בחלק שלא נקרא.")
    if reason == "region_not_read" and variant == "unread":
        return (f"{who} לא {'נמצאו' if many else 'נמצא'} {in_it}, אבל יש בו אזורים שלא נקראו (תמונה או טבלה), ולכן "
                "ייתכן שהנתון מופיע בהם.")
    if reason == "region_not_read" and variant == "document_partial":
        return (f"{who}: המסמך \"{document}\" נקרא חלקית (חלק מהתמונות או העמודים לא נקראו), ולכן ייתכן שהנתון "
                "מופיע בחלק שלא נקרא.")
    if reason == "region_not_read":
        return f"{who}: המסמך \"{document}\" נקרא רק בחלקו, ולכן ייתכן שהנתון מופיע בחלק שלא נקרא."
    if reason == "limit_reached":
        # the turn was stopped before it located the datum: what it could not do, never that the datum is absent (R18)
        where = LIMIT_WHERE.get(variant, LIMIT_WHERE[TIME_LIMIT])
        return (f"{who} לא {'אותרו' if many else 'אותר'} במסגרת {where}."
                + (" " + UNIT_IN_FOCUS.format(unit=detail) if detail and variant == TIME_LIMIT else ""))
    if reason == "unit_not_located":
        return (f"{who}: השומה (היחידה בקובץ) שהשאלה מתייחסת אליה לא אותרה, ולכן לא נבדק אם "
                f"{'הם מופיעים' if many else 'הנתון מופיע'} בה.")
    if reason == "not_in_part_read":
        return f"{who} לא {'מופיעים' if many else 'מופיע'} {in_it} שנבדק" + (f" [{sid}]" if sid else "") + "."
    if variant == "searched":  # not_located
        return f"{who} לא {'נמצאו' if many else 'נמצא'} בחיפוש במסמכים שנבדקו."
    return (f"{who} לא {'אותרו' if many else 'אותר'} בחיפושים שבוצעו: אף חיפוש או קריאה לא כיסו "
            f"{'אותם' if many else 'אותו'}, ולכן לא ידוע אם {'הם מופיעים' if many else 'הוא מופיע'} במסמכים.")


def absence_sentence(r: dict) -> str | None:
    """The server's sentence for one validated model claim (``validate_requested``), None when it was found."""
    gap = _claim_gap(r)
    return gap_sentence(gap["reason"], gap["variant"], [gap_label({"text": r["label"]})], gap["place"],
                        gap["document"]) if gap else None


def _claim_gap(r: dict, ws: Workspace | None = None) -> dict | None:
    """The reason a validated claim supports, with its variant and place."""
    place = {"sid": r["checked_where"], "scope": r["scope"], "name": r["section"]} if r.get("checked_where") else None
    if r["status"] == "section_checked_absent":
        return {"reason": "not_in_part_read", "variant": "", "place": place, "document": None}
    if r["status"] == "source_partial":
        variant = r.get("partial_reason") or "document_partial"
        return {"reason": "region_not_read", "variant": variant, "place": place, "document": r.get("partial_document")}
    if r["status"] == "sources_conflict":
        return {"reason": "sources_conflict", "variant": "", "place": None, "document": None}
    if r["status"] == "not_found_search":
        return {"reason": "not_located", "variant": "searched" if r.get("searched") else "", "place": None,
                "document": None}
    return None


_H = re.compile(r"H(\d+)")


def related_evidence(ws: Workspace, outcome: dict, turn: TurnRequirements | None) -> dict:
    """What the turn holds for a requirement, from the ids the judge named as related (each checked against the
    workspace): ``data`` — values, measurements and calculations; ``failures`` — failed calculations and tools;
    ``checks`` — searches (H#) and readings (S#); ``text`` — the passages the turn read that state a part the answer omits,
    as ``[{"id": S#, "quote"}]`` (the judge's support the server validated, ``verify.validated_support``, carried by the
    outcome's ``support``; round 16 U2); ``documents`` — the documents of its data, readings and text."""
    incidents = {x["id"]: x for x in (turn.incidents if turn is not None else [])}
    data, failures, checks, documents = [], [], [], []
    text = [dict(x) for x in outcome.get("support") or [] if x.get("id") in ws.sources]
    documents += [str(ws.sources[x["id"]].document_id) for x in text]
    for i in outcome.get("related") or []:
        if i in ws.values:
            data.append(i)
            documents.append(str(ws.values[i].document_id))
        elif i in ws.measurements:
            data.append(i)
            documents.append(str(ws.measurements[i].document_id))
        elif i in ws.computations:
            data.append(i)
            # a calculation's leaves are values, measurements and the user's assumptions (which have no document)
            documents += [str(src.document_id) for leaf in ws.computations[i].leaves
                          if (src := ws.values.get(leaf) or ws.measurements.get(leaf)) is not None]
        elif i in incidents:
            failures.append(incidents[i])
        elif i in ws.sources:
            checks.append(i)
            documents.append(str(ws.sources[i].document_id))
        elif (m := _H.fullmatch(i)) and 1 <= int(m.group(1)) <= len(ws.searches):
            checks.append(i)
    return {"data": data, "failures": failures, "checks": checks, "text": text,
            "documents": list(dict.fromkeys(documents))}


def _uncertain_value(ws: Workspace, ids: list[str]) -> bool:
    """A value among ``ids`` whose kind or unit was asserted rather than found in its source (``Value.certainty``: an
    asserted stance, speaker, scenario, period, area basis or VAT counts as not stated and does not limit the component,
    round 13 U5, KTD6), whose own region stayed unclear after its focused re-reads (``Value.reading``), or whose source
    was read uncertainly while its own region was not found clear."""
    return any(value_uncertain(ws, i) for i in ids if i in ws.values)


def _openings(ws: Workspace, documents: Collection[str] | None = None) -> dict[str, tuple[str, dict]]:
    """S# of each section, table or page range the turn opened -> (its document, the opening it counts as). A read of
    paragraphs or pages that returned whole sections (``tools._whole_sections``, ``covers``) is an opening of those
    sections (round 7 KTD4, R10), counted as its opening unless the read's own one is a section or a table, or a page
    range read to its end (the broader place, as before). ``documents``: only those."""
    def counted(own: dict, covered: dict) -> dict:
        broader = own.get("scope") in ("section", "table") or ws.read_complete(own.get("target")) is True
        return own if broader else covered

    out: dict[str, tuple[str, dict]] = {}
    for doc, a in ws.activity.items():
        if documents is not None and doc not in documents:
            continue
        for o in a.get("openings") or []:
            have = out.get(o["sid"])
            if have is None:
                out[o["sid"]] = (doc, o)
            elif bool(have[1].get("covers")) != bool(o.get("covers")):  # the read's own opening, and what it covered
                own, covered = (o, have[1]) if have[1].get("covers") else (have[1], o)
                out[o["sid"]] = (doc, counted(own, covered))
    return out


def _unit_key(data: dict) -> str:
    """The stable id of the unit a handle's data names (``Contexts.key``: version, reading, number and, for a unit of
    a derivation after ``units-v4``, its tag: ``contexts.anchor_key``, round 15 U4)."""
    return contexts.anchor_key(data["version_id"], data)


def _unit_text(ws: Workspace, key: str, fallback: str = "") -> str:
    """How a gap line names a unit: «title» and its pages. The titles come from the contexts the turn read; a unit the
    turn only held in focus has the text the engine gave it (``Workspace.unit_focus``)."""
    from app.chat.tools import unit_info

    version, _, number = contexts.split_unit_key(key)
    cx = ws.contexts.get(version)
    if cx is None or number not in cx.numbers:
        return fallback
    info = unit_info(cx, number)
    pages = contexts.pages_text(*(info["pages"] or (None, None)))
    name = info["title"] or info["label"]
    return f"«{name}»" + (f" ({pages})" if pages else "")


def _offered_only(ws: Workspace) -> set[str]:
    """The handles of the candidate units the turn's first message only OFFERED the model (``Workspace.offered_units``,
    round 15 U6): not read (a passage of theirs returned), not worked in through a handle (``unit_scopes``), not the
    conversation's unit, not declared in the answer. A candidate shown and ignored is no unit the turn was in, nor a file it
    knew to hold several units (round 15 U7)."""
    from app.chat.tools import read_unit_handles

    offered = set(getattr(ws, "offered_units", ()) or ())
    if not offered:
        return set()
    used = read_unit_handles(ws)
    if ws.unit_focus and ws.unit_focus.get("handle"):
        used.add(ws.unit_focus["handle"])
    return offered - used - set(getattr(ws, "declared_handles", ()) or ())


def unit_signal(ws: Workspace) -> dict:
    """What the turn knows of the appraisal unit (round 9 U9, U10): ``multi`` — the documents it saw to hold several
    units (a source or a unit handle of theirs); ``located`` — the units it was in, as {unit id: how a line names it}:
    the conversation's unit (``Workspace.unit_focus``), a unit its tools worked in through a handle
    (``unit_scopes``), the units the answer declared (``declared_units``, round 15 U7), the one unit a request that names
    a property matches in a file, and the one unit ``find_documents`` listed when it listed no other. A turn that touched
    a file of several units and was in none of them has located nothing (R18). A candidate the first message only offered
    the model is neither (``_offered_only``): it counts once the turn read, scoped or declared it."""
    from app.chat import entities
    from app.chat.tools import asked_contexts

    ignored = _offered_only(ws)
    several = {v for v, cx in ws.contexts.items() if cx.multi}
    multi = {str(s.document_id) for s in ws.sources.values()
             if s.document_id is not None and (s.unit or str(s.version_id) in several)}
    handles = {h: d for h, d in ws.handles.items() if d["kind"] == "U" and h not in ignored}
    multi |= {d["document_id"] for d in handles.values()}
    multi |= {x["document_id"] for x in ws.unit_scopes.values()}
    found: dict[str, str] = {}
    focus = ws.unit_focus
    if focus and focus.get("handle") in ws.handles:
        multi.add(str(focus["document_id"]))
        key = _unit_key(ws.handles[focus["handle"]])
        found[key] = _unit_text(ws, key, focus.get("display") or "")
    for key in ws.unit_scopes:
        found.setdefault(key, _unit_text(ws, key))
    for h, a in (getattr(ws, "declared_handles", None) or {}).items():  # the answer's own declaration, validated against what was offered or read
        multi.add(str(a["document_id"]))
        key = _unit_key(ws.handles[h])
        found.setdefault(key, _unit_text(ws, key, entities.focus_text(a)))
    for cx in ws.contexts.values():
        if cx.multi:
            named = [n for n in asked_contexts(ws, cx) if cx.is_subject(n)]
            if len(named) == 1:
                found.setdefault(cx.key(named[0]), _unit_text(ws, cx.key(named[0])))
    listed = list(handles.values())
    if not found and len(listed) == 1:
        key = _unit_key(listed[0])
        found[key] = _unit_text(ws, key)
    return {"multi": multi, "located": found}


def _limit_of(ws: Workspace) -> str | None:
    """The limit that stopped the turn before it finished looking, when it reached one (``LIMITS``)."""
    return next((k for k in LIMITS if k in ws.limits_hit), None)


def _inside_unit(ws: Workspace, sid: str, signal: dict) -> bool:
    """Whether the source ``sid`` lies in a unit the turn located, or in a file that is one unit: only a section read
    inside the appraisal asked about says "not present in the part read"."""
    source = ws.sources.get(sid)
    if source is None or source.document_id is None or str(source.document_id) not in signal["multi"]:
        return True
    return bool(source.unit) and source.unit["id"] in signal["located"]


def _missing_parameters(ws: Workspace, o: dict, ev: dict) -> list[str]:
    """The parameters of a calculation the user did not give that the workspace does not fill — with a user
    assumption (A#), a document value applied as a rate (a rate the report states in a sensitivity section), or a
    computation of the component over document values and user assumptions only (round 7 KTD9:
    ``verify.unfilled_parameters``): the detail its result waits for. A verified component's own
    ``pending_parameters`` (settled with the judge's links, ``VerifyReport.settle_parameters``) decide when present."""
    from app.chat.verify import unfilled_parameters

    if "pending_parameters" in o:
        return list(o["pending_parameters"] or [])
    return unfilled_parameters(ws, o)


def reason_of(ws: Workspace, o: dict, turn: TurnRequirements | None, claim: dict | None = None,
              opened: dict[str, tuple[str, dict]] | None = None, signal: dict | None = None) -> dict:
    """Why a component is not given, chosen from the turn's evidence by the reason table (first match wins; the
    module docstring lists it): {"reason", "variant", "place", "document", "searched", "detail"}. ``claim``: the
    model's validated claim about it, which adds evidence and never upgrades. ``opened``: the turn's openings
    (``_openings``), and ``signal``: the turn's unit signal (``unit_signal``), when the caller already has them."""
    ev = related_evidence(ws, o, turn)
    kinds = {f["kind"] for f in ev["failures"]}
    # covered only by a search or reading the judge tied to it: a model's "not found in the search" proves none
    out = {"variant": "", "place": None, "document": None, "searched": bool(ev["checks"]), "detail": ""}
    if o["kind"] == "instruction":
        snippet = (o.get("uncited") or [""])[0]
        return out | {"reason": "instruction_not_met", "variant": o.get("check") or "",
                      "detail": snippet[:80] + ("…" if len(snippet) > 80 else "")}
    if o.get("removed_units") and not o.get("units"):
        kinds = o.get("removal_kinds") or []
        return out | {"reason": "removed", "variant": kinds[0] if len(kinds) == 1 else ""}
    if "tool" in kinds:
        return out | {"reason": "tool_failure"}
    if "calculation" in kinds:
        return out | {"reason": "calculation_incomplete", "variant": "failed"}
    if o["kind"] == "clarification":
        return out | {"reason": "detail_missing"}
    missing = _missing_parameters(ws, o, ev)
    if missing:
        return out | {"reason": "detail_missing", "detail": "; ".join(missing)}
    if o["kind"] == "calculation" and ev["data"]:
        return out | {"reason": "calculation_incomplete", "variant": "not_computed"}
    if _uncertain_value(ws, ev["data"]):
        return out | {"reason": "not_verifiable", "variant": "uncertain"}
    if (ev["data"] and conflicting(ws, ev["documents"] or None, ev["data"])) or (
            claim is not None and claim["status"] == "sources_conflict"):
        return out | {"reason": "sources_conflict"}
    if o.get("evidence_state") == "undeterminable":
        return out | {"reason": "not_verifiable", "variant": "undeterminable"}
    if ev["text"]:  # a passage the turn read states the part and the answer does not give it (round 16 U2, R6): never "not found"
        return out | _read_not_used(ws, ev["text"][0]["id"])  # also when related data was found: it is the omitted part's place
    if ev["data"]:
        return out | {"reason": "not_verifiable", "variant": "not_presented"}
    # a professional question whose pool was empty: the repository holds no standard or professional document (U8, KTD15,
    # R29). It says what the repository lacks — never that a search found nothing in a standard
    if ws.intent == PROFESSIONAL and ws.standards_absent:
        return out | {"reason": "standard_absent", "searched": True}
    if opened is None:
        opened = _openings(ws)
    readings = [(sid, *opened[sid]) for sid in ev["checks"] if sid in opened]
    gap = _claim_gap(claim) if claim is not None else None
    if signal is None:
        signal = unit_signal(ws)
    # a turn stopped by a limit says so (R18; AE8) — unless a section read to its end inside the located unit (or in a
    # file that is one unit) ties the component: the limit did not cause that absence, and "not present in the section
    # checked" is the accurate statement. A file named but not read is the better statement of what was not done
    proven = any(ws.read_complete(o_.get("target")) is True and _inside_unit(ws, sid, signal)
                 for sid, _, o_ in readings) or (
        gap is not None and gap["reason"] == "not_in_part_read" and _inside_unit(ws, gap["place"]["sid"], signal))
    if (limit := _limit_of(ws)) is not None and not proven and not (ws.unread_named and not readings):
        located = [t for t in signal["located"].values() if t]
        return out | {"reason": "limit_reached", "variant": limit,
                      "detail": located[0] if limit == TIME_LIMIT and len(signal["located"]) == 1 and located else ""}
    for sid, doc, o_ in readings:
        st = ws.reads.get(o_.get("target")) if o_.get("target") is not None else None
        if ws.read_complete(o_.get("target")) is False:
            variant = "unread" if st["to"] == READ_TO_END else "clipped"
            return out | {"reason": "region_not_read", "variant": variant, "searched": True,
                          "place": {"sid": sid, "scope": o_.get("scope"), "name": o_.get("name")},
                          "document_id": doc}
    if gap is not None and gap["reason"] == "region_not_read":
        return out | gap | {"searched": True}
    # a turn that never located the appraisal asked about did not look in the right place: it says so, before any "not
    # present"
    # (a professional question asks about no property: an appraisal it read is an example, so no property is "not located")
    if not (ws.unread_named and not readings) and ws.intent != PROFESSIONAL:
        docs = set(ev["documents"]) | set((claim or {}).get("document_ids") or [])
        if signal["multi"] and (not docs or docs & signal["multi"]) and not signal["located"]:
            return out | {"reason": "unit_not_located"}
    for sid, doc, o_ in readings:
        if ws.read_complete(o_.get("target")) is True and _inside_unit(ws, sid, signal):
            return out | {"reason": "not_in_part_read", "searched": True,
                          "place": {"sid": sid, "scope": o_.get("scope"), "name": o_.get("name")},
                          "document_id": doc}
    if gap is not None and gap["reason"] == "not_in_part_read" and _inside_unit(ws, gap["place"]["sid"], signal):
        return out | gap | {"searched": True}
    if ws.unread_named:  # the documents the turn's lookups matched by title were not read: nothing was searched in them
        return out | _not_read_gap(ws)
    documents = list(dict.fromkeys(ev["documents"] + ((claim or {}).get("document_ids") or [])))
    for doc in documents:
        a = ws.activity.get(doc) or {}
        if a.get("partial") or a.get("read_partial"):
            return out | {"reason": "region_not_read", "searched": True,
                          "variant": "document_read_in_part" if a.get("read_partial") else "document_partial",
                          "document": a.get("title"), "document_id": doc}
    # a reading outside the located unit searched nothing of the appraisal asked about
    outside = [sid for sid in ev["checks"] if sid in ws.sources and not _inside_unit(ws, sid, signal)]
    if out["searched"] and len(outside) < len(ev["checks"]):
        return out | {"reason": "not_located", "variant": "searched"}
    return out | {"reason": "not_located", "searched": False}


def _read_not_used(ws: Workspace, sid: str) -> dict:
    """The gap of a part the turn read at ``sid`` (validated support) and the answer does not give: where it was read — the
    document, the section or pages, the S# — and that it was not used. The place is that passage's."""
    src = ws.sources[sid]
    place = {"sid": sid, "scope": "section" if src.section else None, "name": src.section}
    return {"reason": "not_verifiable", "variant": "read_not_used", "searched": True, "place": place,
            "document": src.title, "document_id": str(src.document_id), "detail": src.location or ""}


_BRACKETS = re.compile(r"[\[\]\r\n]+")


def _plain(text: str, limit: int) -> str:
    """A model-written phrase for a server line: one line, no square brackets (a citation is the server's to write), clipped."""
    flat = " ".join(_BRACKETS.sub(" ", text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def item_line(ws: Workspace, state: dict) -> dict:
    """The server's line for one item of the ledger the answer does not give (round 17 U3, KTD3, R8): it names the item, the place
    in the source it was read at (the document, the location, the S#) and the reason — still omitted: read there and not used,
    never "not found"; ``not_verifiable`` / ``not_applicable``: the reason the agent gave. {"id", "state", "text", "document_id",
    "title"}."""
    from app.chat.verify import ITEM_NOT_APPLICABLE, ITEM_NOT_VERIFIABLE

    src = ws.sources.get(state["source"])
    title = (src.title if src is not None else "") or ""
    place = _read_at(title, src.location if src is not None else "", state["source"])
    name = "«" + _plain(state["label"] or state["quote"], ITEM_NAME_CHARS) + "»"
    reason = _plain(state["reason"], ITEM_REASON_CHARS).rstrip(".")
    if state["state"] == ITEM_NOT_VERIFIABLE:
        text = f"{name}: נקרא {place}, אך לא ניתן לאמת אותו: {reason}."
    elif state["state"] == ITEM_NOT_APPLICABLE:
        text = f"{name}: נקרא {place}, ולא נכלל בתשובה כי הוא אינו חל על הבקשה: {reason}."
    else:
        text = f"{name}: נקרא {place}, ולא נעשה בו שימוש בתשובה."
    return {"id": state["id"], "state": state["state"], "text": text,
            "document_id": str(src.document_id) if src is not None and src.document_id is not None else None,
            "title": title}


def item_gap_lines(ws: Workspace, report, outcomes: Sequence[dict]) -> list[dict]:
    """The lines for the ledger's items that stand open in the saved answer: those the agent listed and no surviving unit gives
    (still omitted) and those declared ``not_verifiable`` or ``not_applicable`` with a reason (``item_line``), in the ledger's order, at most
    ``ITEM_GAP_LINES`` (one more line counts the rest). An item of a component the request made not relevant is not stated."""
    from app.chat.verify import (
        ITEM_NOT_APPLICABLE,
        ITEM_NOT_VERIFIABLE,
        ITEM_OMITTED,
        ORIGIN_JUDGE_SUPPORT,
        ORIGIN_SOURCE,
    )

    skip = {o["id"] for o in outcomes if o.get("status") == "not_relevant"}
    # a part only the judge named is stated by its component's own line (round 16: ``read_not_used``), not twice; an item the server
    # built from a span a sentence cites (round 18 U5) is covered by that sentence and is never "read and not used"
    lines = [item_line(ws, st) for st in report.item_states()
             if st["component"] not in skip and st["origin"] != ORIGIN_SOURCE
             and (st["state"] in (ITEM_NOT_VERIFIABLE, ITEM_NOT_APPLICABLE)
                  or (st["state"] == ITEM_OMITTED and st["origin"] != ORIGIN_JUDGE_SUPPORT))]
    if len(lines) <= ITEM_GAP_LINES:
        return lines
    rest = len(lines) - ITEM_GAP_LINES
    return lines[:ITEM_GAP_LINES] + [{"id": "", "state": "more", "text": f"ועוד {rest} פריטים נוספים שנקראו ולא נכללו בתשובה.",
                                      "document_id": None, "title": ""}]


def _not_read_gap(ws: Workspace) -> dict:
    """The gap of a component whose named document was not read (``document_not_read``): the documents the turn's
    lookups matched by title, with their state and reason (``unread.gap_detail``)."""
    from app.chat import unread

    files = list(ws.unread_named.values())
    one = len(files) == 1
    return {"reason": "document_not_read", "variant": "" if one else "many", "searched": True,
            "document": "; ".join(unread.title_of(d) for d in files), "detail": unread.gap_detail(files),
            "document_id": files[0]["document_id"] if one else None}


def _claims_by_component(ws: Workspace, requested, opened: dict[str, tuple[str, dict]] | None = None
                         ) -> tuple[dict[str, dict], list[dict]]:
    """The model's validated claims: the strongest per component id, and those linked to none (strongest per label)."""
    linked: dict[str, list[dict]] = {}
    unlinked: dict[str, list[dict]] = {}
    for r in validate_requested(ws, requested, opened):
        if r["status"] not in _STRENGTH:
            continue
        if r["component"]:
            linked.setdefault(r["component"], []).append(r)
        else:
            unlinked.setdefault(_norm_label(r["label"]), []).append(r)
    def strongest(rows: list[dict]) -> dict:
        return min(rows, key=lambda r: _STRENGTH.index(r["status"]))

    return ({c: strongest(rs) for c, rs in linked.items()}, [strongest(rs) for rs in unlinked.values()])


def component_outcomes(ws: Workspace, report, applied: FinalAnswer, turn: TurnRequirements | None,
                       requested=()) -> list[dict]:
    """Each component with its status in the shown answer (``VerifyReport.requirement_outcomes``) and, when it is a
    leaf not given (not answered, partial, needs clarification), its reason (``reason_of``): ``limitation`` (a
    ``REASONS`` key) and ``limitation_text``; the evidence it rests on — ``place`` (the section, table or pages read,
    with its S#), ``document``/``document_id``/``document_title``, ``searched`` (a search or reading the judge tied
    to it covered it), ``claimed`` (the model's own claim, before validation), ``detail`` (the parameters missing, the
    first unit without a citation); ``gap`` (its sentence alone) and ``stated`` — whether the server states it in the
    gap paragraph (a leaf not answered or needing clarification, and an instruction not met; a partly given leaf only
    for its ``missing_part`` — the part the judge named as not given, validated against the request: its line names
    that part with the component's reason (R20), the status staying partial, and a conditional requirement is never
    stated (R21); a partly given leaf with none is listed in completeness only)."""
    opened = _openings(ws)  # the turn's openings, for every claim and component
    claims, _ = _claims_by_component(ws, requested, opened)
    outcomes = report.requirement_outcomes(applied)
    signal = None  # the turn's unit signal, computed once for the first component that needs a reason
    for o in outcomes:
        o |= {"limitation": None, "limitation_text": None, "place": None, "document": None, "document_id": None,
              "document_title": None,
              "searched": None, "claimed": (claims.get(o["id"]) or {}).get("claimed"), "detail": "", "variant": "",
              "gap": None}
        if o["children"] or o["status"] not in UNMET:
            continue
        if signal is None:
            signal = unit_signal(ws)
        why = reason_of(ws, o, turn, claims.get(o["id"]), opened, signal)
        o |= {"limitation": why["reason"], "limitation_text": REASONS[why["reason"]], "place": why.get("place"),
              "document": why.get("document"), "document_id": why.get("document_id"), "searched": why["searched"],
              "detail": why.get("detail") or "", "variant": why.get("variant") or ""}
        if o["document_id"]:
            o["document_title"] = ((ws.activity.get(o["document_id"]) or ws.unread_named.get(o["document_id"]) or {})
                                   .get("title"))
        # a partly given leaf is stated only for the part the judge named missing (validated against the request,
        # ``verify.valid_missing``): a conditional requirement (R21) or one that is no datum of the documents never is
        datum = not o["conditional"] and o["kind"] in ("information", "calculation")
        if o["status"] != "partial" or not datum:
            o["missing_part"] = ""
        # a part a read passage states and the answer omits is stated too (round 16 U2, R6), never a conditional component
        o["stated"] = (o["status"] != "partial" or o["kind"] == "instruction" or bool(o["missing_part"])
                       or (o["variant"] == "read_not_used" and datum))
        if o["status"] == "partial" and o.get("omitted_items") and not o.get("judge_items") and not o["missing_part"]:
            o["stated"] = False  # a partly given component's omitted agent items are stated one by one, in the item lines (``item_gap_lines``)
        o["gap"] = gap_sentence(why["reason"], o["variant"], [gap_label(o)], o["place"], o["document"], o["detail"])
    return outcomes


_LEADING_QUESTION = re.compile(r"^\s*(?:מהו|מהי|מהם|מהן|מה|איזה|איזו|אילו)\s+(?:הוא\s+|היא\s+|הם\s+|הן\s+)?")


SHORT_COMPONENT_WORDS = 8  # a component of at most this many words, in one clause, is its own label


def gap_label(o: dict) -> str:
    """What a component's gap line names (R24): never the whole request or a long or multi-clause question. In order:
    the part of a partly given component the answer does not give (R20; validated, so a noun phrase); the component's
    own words when they are a noun phrase; a short component (``SHORT_COMPONENT_WORDS`` words, one clause, no "?") as
    the user wrote it, a yes/no "האם …" one included; the words without a leading interrogative pronoun ("מהו תאריך
    הטופס?" -> "תאריך הטופס") when what remains is a noun phrase; the datum asked for, with the component's subject;
    "הנתון המבוקש" alone as the last resort."""
    from app.chat.verify import _QUESTION_WORD, MISSING_PART_CHARS, is_request_clause

    if o.get("status") == "partial" and o.get("missing_part"):
        return o["missing_part"]
    text = " ".join((o.get("text") or "").split())
    if text and not is_request_clause(text) and len(text) <= MISSING_PART_CHARS:
        return text
    asked = len(_QUESTION_WORD.findall(text))
    if (text and "?" not in text and "," not in text and len(text.split()) <= SHORT_COMPONENT_WORDS
            and asked == 1 and text.startswith("האם ")):
        return text
    rest = _LEADING_QUESTION.sub("", text).strip(" ?")
    if rest and rest != text and not is_request_clause(rest) and len(rest) <= MISSING_PART_CHARS:
        return rest
    subject = " ".join((o.get("subject") or "").split())
    if subject and not is_request_clause(subject) and len(subject) <= MISSING_PART_CHARS:
        return f"{GENERIC_LABEL} ({subject})"
    return GENERIC_LABEL


def gap_groups(items: list[dict]) -> list[dict]:
    """The gap paragraph, grouped: one group per reason, variant and place, in the reason table's order, each with
    its line and the components it names once, in the request's order — {"reason", "reason_text", "components" (ids;
    none for a model claim the turn had no component for), "texts", "text"}."""
    groups: dict[tuple, list[dict]] = {}
    for o in items:
        place = o.get("place") or {}
        key = (o["limitation"], o.get("variant") or "", place.get("sid"), place.get("name"), o.get("document"),
               o.get("detail") or "")
        groups.setdefault(key, []).append(o)
    out: list[dict] = []
    for key in sorted(groups, key=lambda k: _ORDER.index(k[0])):
        members = groups[key]
        texts = list(dict.fromkeys(gap_label(m) for m in members))
        line = gap_sentence(key[0], key[1], texts, members[0].get("place"), key[4], key[5])
        if any(g["text"] == line for g in out):
            continue
        out.append({"reason": key[0], "reason_text": REASONS[key[0]],
                    "components": [m["id"] for m in members if m.get("id")], "texts": texts, "text": line})
    return out


# what the payload keeps of each component (the UI's per-component detail)
PUBLIC_FIELDS = ("id", "text", "kind", "aspect", "parent", "conditional", "subject", "status", "evidence_state",
                 "limitation", "limitation_text", "variant", "stated", "gap", "missing_part", "units", "removed_units", "removal_kinds",
                 "absence_units",
                 "related", "place", "document", "document_id", "document_title", "searched", "claimed", "detail",
                 "uncited", "check", "pending_parameters")


def public_components(outcomes: list[dict]) -> list[dict]:
    """Each component's outcome for the answer payload: its status, reason and evidence, without the judge's words."""
    return [{k: o.get(k) for k in PUBLIC_FIELDS if k in o} for o in outcomes]


def _unlinked_gaps(ws: Workspace, requested) -> list[dict]:
    """A turn with no requirements at all: each datum the model claimed missing, as its own gap item."""
    _, unlinked = _claims_by_component(ws, requested)
    out = []
    for r in unlinked:
        gap = _claim_gap(r)
        if gap["reason"] == "not_located" and ws.unread_named:  # the named document was not read: not "not found"
            gap = _not_read_gap(ws) | {"place": None}
        out.append({"text": gap_label({"text": r["label"]}), "limitation": gap["reason"], "variant": gap["variant"],
                    "place": gap["place"], "document": gap["document"], "detail": gap.get("detail") or ""})
    return out


def compose_components(ws: Workspace, answer: FinalAnswer, report,
                       turn: TurnRequirements | None = None) -> tuple[Edited, list[dict]]:
    """The verified answer as the server finishes it (round 7 U3, ``engine.finish``), in order, through one ``Doc``
    that records what the server writes (round 8 U4): the units that failed cut or removed (``VerifyReport.edit``);
    each component's outcome recomputed from the surviving units with its reason (``component_outcomes``); the units
    stating an absence of a component the server states removed (``VerifyReport.supersede``, then edited again from the
    original); and one gap paragraph after the answer (``gap_groups``). A component not given makes the answer at most
    ``partial``. Returns the edits, tidied, and the outcomes (none for a clarification, which asks and states
    nothing)."""
    edited = report.edit(answer)
    if answer.status == "clarification":
        return edited, []
    outcomes = component_outcomes(ws, report, edited.answer(answer), turn, answer.requested) if report.requirements else []
    if outcomes and report.supersede(outcomes):
        edited = report.edit(answer)
        outcomes = component_outcomes(ws, report, edited.answer(answer), turn, answer.requested)
    if outcomes:  # the instruction plane from the components as stated, beside the content and completeness planes
        report.instructions = report.instruction_plane(outcomes)
    items = [o for o in outcomes if o["stated"]] + ([] if report.requirements else _unlinked_gaps(ws, answer.requested))
    groups = gap_groups(items)
    report.gaps = groups
    lines = [g["text"] for g in groups]
    doc = edited.doc
    if note := report.conditional_note():  # one caveat per calculation, after the body and before the gaps (R23)
        doc.tidy_blank_lines()
        doc.append(("\n\n" if doc.text else "") + note, "conditional_note")
    if lines:
        doc.tidy_blank_lines()
        doc.append(("\n\n" if doc.text else "") + "\n".join(lines), "gap_paragraph")
        edited.status = "partial" if edited.status == "answered" else edited.status
    # the ledger's items the answer does not give, or that were declared not verifiable / not applicable, one line each (round 17
    # U3, R8): server text, a pure function of the final state like the gap paragraph. Whether the answer is partial follows the
    # components (an omitted item makes its component partial; an item declared with a reason does not)
    item_lines = item_gap_lines(ws, report, outcomes) if outcomes else []
    report.item_gaps = item_lines
    if item_lines:
        doc.tidy_blank_lines()
        doc.append(("\n\n" if doc.text else "") + "\n".join(x["text"] for x in item_lines), "item_gap")
    if any(not o["children"] and o["status"] in UNMET for o in outcomes) and edited.status == "answered":
        edited.status = "partial"
    doc.delete(tidy_cuts(doc.text))
    doc.tidy_blank_lines()
    return edited, outcomes


def state_components(ws: Workspace, answer: FinalAnswer, report,
                     turn: TurnRequirements | None = None) -> tuple[FinalAnswer, list[dict]]:
    """``compose_components`` as the answer and the outcomes, without the registry of the server's spans."""
    edited, outcomes = compose_components(ws, answer, report, turn)
    return edited.answer(answer), outcomes


def gap_documents(outcomes: list[dict], item_lines: Sequence[dict] = ()) -> list[dict]:
    """The documents the gap statements name (a section read, a document read in part, the place of an item line,
    ``item_gap_lines``): permission checks cover them (``LEDGER_DOCUMENT_KEYS``)."""
    out: dict[str, dict] = {}
    for o in outcomes:
        if o.get("stated") and o.get("document_id"):
            out[o["document_id"]] = {"document_id": o["document_id"], "title": o.get("document_title")}
    for line in item_lines:
        if line.get("document_id"):
            out.setdefault(line["document_id"], {"document_id": line["document_id"], "title": line.get("title")})
    return list(out.values())


def completeness(outcomes: list[dict]) -> dict | None:
    """The answer's completeness, apart from its correctness (R19), over the components that are leaves (a parent
    takes its status from its children) and relevant: ``full`` (every one given), ``partial`` (some given, in full or
    in part) or ``missing`` (none given); and each one not given with its status and reason key, for the line under
    the answer — the reason itself is said once, in the answer (KTD4). None when no requirements were judged."""
    if not outcomes:
        return None
    leaves = [o for o in outcomes if not o.get("children") and o["status"] != "not_relevant"]
    statuses = [o["status"] for o in leaves]
    if all(s == "full" for s in statuses):
        status = "full"
    elif any(s in ("full", "partial") for s in statuses):
        status = "partial"
    else:
        status = "missing"
    return {"status": status, "requirements": len(leaves),
            "missing": [{"id": o["id"], "text": o["text"], "status": o["status"], "reason": o["limitation"],
                         "parent": o.get("parent") or "", "conditional": bool(o.get("conditional")),
                         "missing_part": o.get("missing_part") or ""}
                        for o in leaves if o["status"] != "full"]}


# a line with nothing left but markup: a list marker, a list item's number, a quote mark, bold marks
_ORPHAN_LINE = re.compile(r"(?:[-*+]|\d{1,3}[.)]|>)?\s*(?:\*\*|__)?\s*(?:\*\*|__)?")


def tidy_cuts(markdown: str) -> list[tuple[int, int]]:
    """The lines ``tidy`` drops, as ranges (each with its newline): a line left with nothing but markup, and a prose
    line repeated; tables are kept as they are."""
    out: list[tuple[int, int]] = []
    seen: set[str] = set()
    lines = markdown.split("\n")
    pos = 0
    for n, line in enumerate(lines):
        start, end = pos, pos + len(line)
        pos = end + 1
        s = line.strip()
        if s and not s.startswith("|"):
            drop = bool(_ORPHAN_LINE.fullmatch(s))
            if not drop and re.search(r"[א-תA-Za-z0-9]", s):
                drop = s in seen
                seen.add(s)
            if drop:
                out.append(line_cut(start, end, n + 1 == len(lines)))
    return out


def tidy(markdown: str) -> str:
    """The answer without the remnants of removals and additions (R22): a line left with nothing but markup, and a
    prose line repeated (an absence sentence said twice); tables are kept as they are."""
    from app.chat.compose import Doc

    doc = Doc(markdown)
    doc.delete(tidy_cuts(markdown))
    doc.tidy_blank_lines()
    return doc.text


def tables_presented(ws: Workspace, markdown: str) -> list[dict]:
    """For each table the answer cites: its rows, and how many of them the answer presents a value of."""
    from app.answering.verify import numbers_in
    from app.chat.tools import table_row_count

    shown = numbers_in(markdown)
    tables: dict[tuple, dict] = {}
    for sid in cited_ids(markdown):
        s = ws.sources.get(sid)
        if s is None or s.kind not in ("table", "table_row") or s.table_index is None:
            continue
        size = table_row_count(s.text)
        t = tables.setdefault((s.version_id, s.table_index), {"document_id": str(s.document_id), "title": s.title,
                                                              "location": s.location, "rows": None,
                                                              "presented": set()})
        if size:
            t["rows"] = size
        rows = [ln for ln in s.text.split("\n") if " | " in ln and re.search(r"\d", ln)]
        for n, row in enumerate(rows if s.kind == "table" else rows[-1:]):
            cells = row.split(" | ")[1:] or [row]  # a row's label (an address, a floor) is not its value
            if numbers_in(" ".join(cells)) & shown:
                t["presented"].add(row if s.kind == "table_row" else n)
    return [t | {"presented": len(t["presented"])} for t in tables.values() if t["rows"]]


def short_tables(tables: list[dict]) -> list[dict]:
    """The cited tables the answer presented only part of."""
    return [t for t in tables if t["presented"] < t["rows"]]


def _membership(ws: Workspace, answer: FinalAnswer, ids: set[str]) -> tuple[dict, FinalAnswer]:
    """An answer about which documents are in a set (a count, a list) that cites only listings. Each cited
    listing is a page of one set (a tool and its query); the set is covered when every page of it was listed in
    the turn. The documents' content was not read, and the note says so."""
    sets: dict[tuple, dict] = {}
    for s in ws.sources.values():
        if s.is_listing and s.listing:
            entry = sets.setdefault(s.listing["key"], {"listing": s.listing, "pages_read": set(), "documents": {},
                                                       "cited": False})
            entry["pages_read"].add(s.listing["page"])
            entry["documents"] |= {d["document_id"]: d for d in s.listing["documents"]}
            entry["cited"] = entry["cited"] or s.sid in ids
    cited = [e for e in sets.values() if e["cited"]]
    notes, matching, pages, read = [], {}, 0, 0
    for e in cited:
        lst = e["listing"]
        e_pages, e_read = lst["pages"] or 1, len(e["pages_read"])
        pages, read = pages + e_pages, read + min(e_read, e_pages)
        matching |= e["documents"]
        note = f"רשימת {lst['total']} {lst['criterion']} נבדקה לפי התאמת המונחים; תוכן המסמכים לא נקרא."
        if e_read < e_pages:
            note += f" נקראו {e_read} מתוך {e_pages} עמודי הרשימה, ולכן הספירה חלקית."
        notes.append(note)
    complete = bool(cited) and read >= pages
    note = "**כיסוי:** " + " ".join(notes)
    ledger = {"scope_kind": "set", "scope_query": "; ".join(e["listing"]["criterion"] for e in cited),
              "membership": True, "cited": [], "matching": list(matching.values()), "pages": pages,
              "pages_read": read, "complete": complete, "note": note, "tables": []}
    status = "partial" if not complete and answer.status == "answered" else answer.status
    return ledger, answer.model_copy(update={
        "answer_markdown": (answer.answer_markdown.rstrip() + "\n\n> " + note).strip(), "status": status})


def touch_verified(ws: Workspace, markdown: str) -> None:
    """A document whose datum the final text cites is ``verified`` (its datum passed verification: what failed was
    removed before this). The one side effect the coverage ledger had: made once, by the engine, after the answer is
    composed (``build`` only reads), so the final check (round 8 U6) can run first."""
    for doc, title in cited_documents(ws, markdown).items():
        ws.touch(doc, title, "verified")


def build(ws: Workspace, answer: FinalAnswer, question: str) -> tuple[dict, FinalAnswer]:
    """The coverage ledger of the answer, and the answer with the coverage note and status cap when needed. It reads
    the workspace and changes nothing in it: the documents the answer cites count as ``verified`` in the ledger
    (``_level``) before the engine records it (``touch_verified``)."""
    from app.chat.tools import LEVELS, new_scope
    from app.platform.search import (
        documents_named,
        documents_sharing_title_words,
        units_matching,
    )

    cited = cited_documents(ws, answer.answer_markdown)
    verified = set(cited)  # the documents whose datum the final text cites; ``touch_verified`` records them, once
    ids = cited_ids(answer.answer_markdown)
    if ids and all(i in ws.sources and ws.sources[i].is_listing for i in ids):
        return _membership(ws, answer, ids)
    scope_kind = answer.scope_kind
    ledger: dict = {"scope_kind": scope_kind, "scope_query": answer.scope_query or "",
                    "cited": [{"document_id": d, "title": t} for d, t in cited.items()],
                    "tables": tables_presented(ws, answer.answer_markdown)}
    note: str | None = None
    if scope_kind == "set":
        scope = ws.scope
        if scope is None:  # the model did not look the set up: the server does, with the terms it declared
            query = ledger["scope_query"] or question
            if ("matching", query) not in ws.lookups:  # the same query reads the same documents all turn
                with tenant_tx(ws.ctx) as conn:
                    ws.lookups["matching", query] = units_matching(conn, query).documents  # by unit in a file of several (R11)
            found = ws.lookups["matching", query]
            scope = new_scope(query, [(d["document_id"], d["title"]) for d in found], 1)
        matching = scope["matching"]
        omitted = []
        for o in answer.omitted:
            doc = str(o.document_id or "")
            title = next((m["title"] for m in matching if m["document_id"] == doc),
                         (ws.activity.get(doc) or {}).get("title"))
            omitted.append({"document_id": doc if title else None, "title": title, "what": o.what, "why": o.why})
        explained = {o["document_id"] for o in omitted if o["document_id"]}
        levels = {m["document_id"]: _level(ws, m["document_id"], verified) for m in matching}
        reached = [m for m in matching if levels[m["document_id"]] in LEVELS[1:]]
        reached_ids = {m["document_id"] for m in reached}
        with_data = [m for m in matching if m["document_id"] in cited]
        read = [m for m in matching if _read(ws, m["document_id"])]
        retrieved_only = [m for m in reached if not _read(ws, m["document_id"])]
        not_checked = [m for m in matching if m["document_id"] not in reached_ids]
        unused = [m for m in reached if m["document_id"] not in cited and m["document_id"] not in explained]
        partially = [m for m in reached if (ws.activity.get(m["document_id"]) or {}).get("partial")]
        # document coverage: a verified datum (or an explained omission) from every document of the set
        complete = bool(matching) and not not_checked and not unused and not partially
        ledger |= {"scope_query": scope["query"], "matching": matching, "levels": levels, "read": read,
                   "retrieved_only": retrieved_only, "with_data": with_data, "not_checked": not_checked,
                   "unused": unused, "partially_read": partially, "omitted": omitted,
                   "pages": scope.get("pages"), "pages_read": len(scope.get("pages_read") or ()),
                   "complete": complete}
        counts = (f"{len(matching)} מסמכים מתאימים · נקראו (סעיף/טבלה) {len(read)} · נשלפו קטעים בלבד מ-"
                  f"{len(retrieved_only)} · נתון מאומת מ-{len(with_data)}")
        if not matching:
            note = f"**כיסוי:** לא נמצאו מסמכים שמכילים את כל המונחים של \"{scope['query']}\"."
        elif not complete:
            # one sentence in the answer; which documents were not checked, unused or partly read is listed in the
            # sources panel (the ledger), not in the answer text
            note = (f"**כיסוי:** {len(matching)} מסמכים מתאימים לתחום \"{scope['query']}\"; התשובה מבוססת על "
                    f"{len(with_data)} מהם, ולכן אינה מכסה את כל התחום ({counts}). הפירוט בחלונית המקורות.")
        elif retrieved_only:
            # every document gave a verified datum, but some only through retrieved passages: not a full reading
            note = (f"**כיסוי:** נמצא נתון מאומת בכל {len(matching)} המסמכים המתאימים ({counts}). מ-"
                    f"{len(retrieved_only)} מהם נשלפו קטעים בלבד, בלי לקרוא את הסעיף או הטבלה במלואם, ולכן ייתכנו "
                    "בהם נתונים נוספים.")
        short = short_tables(ledger["tables"])
        if short and note is None:
            note = "**כיסוי:** " + " ".join(f"מהטבלה ב\"{t['title']}\" הוצגו ערכים מ-{t['presented']} מתוך "
                                             f"{t['rows']} שורות." for t in short)
    elif cited:  # a focused answer that cites nothing has no documents to compare titles with
        # the documents the question names, and when none, those sharing title words with it: read once per turn
        look = ws.lookups
        if ("named", question) not in look or (not look["named", question] and ("shared", question) not in look):
            with tenant_tx(ws.ctx) as conn:
                if ("named", question) not in look:
                    look["named", question] = documents_named(conn, question)
                if not look["named", question] and ("shared", question) not in look:
                    look["shared", question] = documents_sharing_title_words(conn, question)
        named = look["named", question]
        shared = {} if named else look["shared", question]
        others: dict[str, str] = {}
        cited_titles: dict[str, str] = {}
        for docs in shared.values():
            ids = {str(i): t for i, t in docs}
            if set(ids) & set(cited) and set(ids) - set(cited):
                others |= {i: t for i, t in ids.items() if i not in cited}
                cited_titles |= {i: t for i, t in ids.items() if i in cited}
        others = less_named(question, others, cited_titles)
        also = [{"document_id": i, "title": t} for i, t in others.items()]
        ledger |= {"also_matching": also, "complete": not also}
        if also:
            note = (f"**שימו לב:** התשובה מבוססת על {len(cited)} מסמכים; מסמכים נוספים שכותרתם מתאימה לשאלה "
                    f"לא נבדקו: {_names(also)}.")
    else:
        ledger |= {"also_matching": [], "complete": True}
    if note:
        ledger["note"] = note
        # an answer that does not cover its documents is at most partial; one that covers them from passages, or
        # shows part of a table, says so without the cap
        capped = ledger.get("complete") is False and answer.status == "answered"
        status = "partial" if capped else answer.status
        answer = answer.model_copy(update={"answer_markdown": (answer.answer_markdown.rstrip() + "\n\n> "
                                                               + note).strip(), "status": status})
    return ledger, answer
