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
5. a calculation resting on a parameter the user did not give, which no user assumption (``A#``) and no document
   rate a calculation of the turn applied fills (round 7 KTD9; its status is then ``needs_clarification``), or a
   clarification component — a detail missing from the request;
6. a calculation whose inputs were found and never computed — calculation not completed;
7. a value found with an uncertain reading or meaning — found, not verifiable;
8. values for the same property, kind, unit, period, basis, scenario and status that differ — sources conflict (in
   a file holding several appraisals, with ``chat_appraisal_context_enforced``, the property is the value's appraisal
   context: two appraisals' values never conflict — round 7 U6, KTD7);
9. the judge's "undeterminable" (an evidence state, not a status) — found, not verifiable (or sources conflict, 8);
10. data found that the verified answer does not present — found, not verifiable;
11. a reading of the section, table or pages it concerns that was clipped or has an unread region, or a model claim
    validated as "read in part" — the relevant region was not fully read;
12. a section, table or pages read to the end without it — not present in the part read (naming it, with its S#);
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
from collections.abc import Collection
from typing import TYPE_CHECKING

from app.chat.tools import READ_TO_END, value_uncertain
from app.db import tenant_tx

if TYPE_CHECKING:
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
                        "partially_read", "omitted", "also_matching", "tables", "requirements", "gap_documents")


def ledger_documents(ledger: dict | None) -> set[str]:
    """Every document id a coverage ledger names, under any of its keys (the permission check reads this)."""
    ids: set[str] = set()
    for key in LEDGER_DOCUMENT_KEYS:
        ids |= {d.get("document_id") for d in (ledger or {}).get(key) or [] if isinstance(d, dict)}
    ids.discard(None)
    return ids


def _level(ws: Workspace, document_id: str) -> str:
    from app.chat.tools import NOT_REACHED

    return (ws.activity.get(document_id) or {}).get("level", NOT_REACHED)


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
    "region_not_read": "האזור הרלוונטי לא נקרא במלואו",
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
    "wrong_calculation": "כי החישוב או הקלטים שלו שגויים.",
    "invalid_citation": "כי הוא ציטט מקור שלא נבדק בתור הזה.",
    "not_checked": "כי לא ניתן היה לבדוק אותו מול המקורות (הוא לא נמצא שגוי).",
}
# a model claim's status -> the reason it supports, once validated
_KIND_OF_STATUS = {"not_found_search": "not_located", "source_partial": "region_not_read",
                   "section_checked_absent": "not_in_part_read", "sources_conflict": "sources_conflict"}
# when one datum is claimed missing more than once: the claim the turn supports most specifically
_STRENGTH = ("section_checked_absent", "sources_conflict", "source_partial", "not_found_search")
UNMET = ("not_answered", "partial", "needs_clarification")


def _norm_label(label: str) -> str:
    return " ".join(re.sub(r"[*_`]+", " ", label or "").split()).strip(" :.").casefold()


def conflicting(ws: Workspace, document_ids: list[str] | None = None, ids: Collection[str] | None = None) -> bool:
    """Whether values the turn registered (V#) or measurements it listed (M#) give one datum different amounts: the
    same kind (by family), unit, period, area basis, named subject (the property), scenario and stance (status), and
    different numbers (R8: a conflict only after that check — two properties, two scenarios or a party's claim and the
    decision are not one datum). Only documents in ``document_ids``, and only the ids in ``ids``, when given.

    In a file holding several appraisals, when the context checks are enforced (round 7 U6, KTD7), the property is the
    value's appraisal context, never the subject's words: two appraisals' values are two properties' however the model
    named their subject."""
    from app.answering.verify import numbers_in
    from app.chat.contexts import enforced
    from app.chat.resolve import family

    by_context = enforced()
    seen: dict[tuple, set] = {}
    entries = [(i, str(v.document_id), v.kind, v.unit, v.period, v.area_basis, v.subject, v.scenario, v.stance,
                str(v.value), (getattr(v, "context", None) or {}).get("key")) for i, v in ws.values.items()]
    entries += [(i, str(m.document_id), m.row.metric_kind, m.row.unit, m.row.period, m.row.area_basis, m.row.subject,
                 getattr(m.row, "scenario", "") or "", getattr(m.row, "stance", "") or "",
                 frozenset(numbers_in(m.row.value_text or "")), (getattr(m, "context", None) or {}).get("key"))
                for i, m in ws.measurements.items()]
    for i, doc, kind, unit, period, basis, subject, scenario, stance, amount, context in entries:
        if (document_ids and doc not in document_ids) or (ids is not None and i not in ids):
            continue
        if not (subject or "").strip() or kind in (None, "unknown", "other"):
            continue  # a datum of no named subject cannot be said to conflict with another
        prop = ("context", context) if by_context and context else _norm_label(subject)
        key = (family(kind), unit, period, _norm_label(basis or ""), prop, _norm_label(scenario),
               stance or "unknown")
        seen.setdefault(key, set()).add(amount)
    return any(len(amounts) > 1 for amounts in seen.values())


def validate_requested(ws: Workspace, requested) -> list[dict]:
    """Each requested datum (a model claim, linked to its component by ``component``) with the status the turn's
    actions support. "The section was checked" needs a section, table or page range of one of its documents opened
    this turn and named in ``checked_where`` (a measurements listing is not a reading of the section), read to its
    end with no unread region in it (R12): one read only in part — clipped and not continued to the end, or with a
    region that was not read — supports only "the source was read in part" (``partial_reason``: ``clipped`` or
    ``unread``). "Read in part" otherwise needs one of its documents read in part. "The sources conflict" needs values
    of the turn that conflict (``conflicting``); without them the datum was found. An unsupported status falls to the
    strongest one the turn does support, down to "not found in the search" — never up. ``kind`` is the reason it
    supports (``REASONS``), None when found; ``searched``: the turn made a search or opened one of its documents."""
    out = []
    for r in requested or []:
        docs = [d for d in r.document_ids if d in ws.activity]
        openings = {o["sid"]: o for d in docs for o in ws.activity[d]["openings"]}
        status, where, reason = r.status, None, None
        if status == "section_checked_absent":
            where = openings.get((r.checked_where or "").strip())
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
        out.append({"component": (getattr(r, "component", "") or "").strip(), "label": r.label.strip(),
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
    return f"בסעיף \"{name}\"", f"מהסעיף \"{name}\"", "הסעיף"


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
    if reason == "not_verifiable" and variant == "undeterminable":
        return f"{who}: המסמכים אינם מספיקים כדי להכריע בכך."
    if reason == "not_verifiable":
        return f"{who}: נמצאו נתונים, אבל הם לא הוצגו בתשובה כנתון מאומת."
    if reason == "sources_conflict":
        return f"{who}: המקורות סותרים — הם נותנים {'להם' if many else 'לנתון'} ערכים שונים, ולכן אין " \
               f"{'להם' if many else 'לו'} ערך אחד."
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
    if reason == "not_in_part_read":
        return f"{who} לא {'מופיעים' if many else 'מופיע'} {in_it} שנבדק" + (f" [{sid}]" if sid else "") + "."
    if variant == "searched":  # not_located
        return f"{who} לא {'נמצאו' if many else 'נמצא'} בחיפוש במסמכים שנבדקו."
    return (f"{who} לא {'אותרו' if many else 'אותר'} בחיפושים שבוצעו: אף חיפוש או קריאה לא כיסו "
            f"{'אותם' if many else 'אותו'}, ולכן לא ידוע אם {'הם מופיעים' if many else 'הוא מופיע'} במסמכים.")


def absence_sentence(r: dict) -> str | None:
    """The server's sentence for one validated model claim (``validate_requested``), None when it was found."""
    gap = _claim_gap(r)
    return gap_sentence(gap["reason"], gap["variant"], [r["label"]], gap["place"], gap["document"]) if gap else None


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
    ``checks`` — searches (H#) and readings (S#); ``documents`` — the documents of its data and readings."""
    incidents = {x["id"]: x for x in (turn.incidents if turn is not None else [])}
    data, failures, checks, documents = [], [], [], []
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
    return {"data": data, "failures": failures, "checks": checks, "documents": list(dict.fromkeys(documents))}


def _uncertain_value(ws: Workspace, ids: list[str]) -> bool:
    """A value among ``ids`` whose meaning was asserted rather than found in its source, whose own region stayed
    unclear after its focused re-reads (``Value.reading``), or whose source was read uncertainly while its own
    region was not found clear."""
    return any(value_uncertain(ws, i) for i in ids if i in ws.values)


def _openings(ws: Workspace) -> dict[str, tuple[str, dict]]:
    """S# of each section, table or page range the turn opened -> (its document, the opening)."""
    return {o["sid"]: (doc, o) for doc, a in ws.activity.items() for o in a.get("openings") or []}


def _missing_parameters(ws: Workspace, o: dict, ev: dict) -> list[str]:
    """The parameters of a calculation the user did not give that the workspace fills with neither a user assumption
    (A#) nor a document value applied as a rate (round 7 KTD9: ``verify.unfilled_parameters``; a rate the report
    states in a sensitivity section fills it): the detail its result waits for."""
    from app.chat.verify import unfilled_parameters

    return list(o.get("pending_parameters") or unfilled_parameters(ws, o))


def reason_of(ws: Workspace, o: dict, turn: TurnRequirements | None, claim: dict | None = None) -> dict:
    """Why a component is not given, chosen from the turn's evidence by the reason table (first match wins; the
    module docstring lists it): {"reason", "variant", "place", "document", "searched", "detail"}. ``claim``: the
    model's validated claim about it, which adds evidence and never upgrades."""
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
    if ev["data"]:
        return out | {"reason": "not_verifiable", "variant": "not_presented"}
    opened = _openings(ws)
    readings = [(sid, *opened[sid]) for sid in ev["checks"] if sid in opened]
    for sid, doc, o_ in readings:
        st = ws.reads.get(o_.get("target")) if o_.get("target") is not None else None
        if ws.read_complete(o_.get("target")) is False:
            variant = "unread" if st["to"] == READ_TO_END else "clipped"
            return out | {"reason": "region_not_read", "variant": variant, "searched": True,
                          "place": {"sid": sid, "scope": o_.get("scope"), "name": o_.get("name")},
                          "document_id": doc}
    gap = _claim_gap(claim) if claim is not None else None
    if gap is not None and gap["reason"] == "region_not_read":
        return out | gap | {"searched": True}
    for sid, doc, o_ in readings:
        if ws.read_complete(o_.get("target")) is True:
            return out | {"reason": "not_in_part_read", "searched": True,
                          "place": {"sid": sid, "scope": o_.get("scope"), "name": o_.get("name")},
                          "document_id": doc}
    if gap is not None and gap["reason"] == "not_in_part_read":
        return out | gap | {"searched": True}
    documents = list(dict.fromkeys(ev["documents"] + ((claim or {}).get("document_ids") or [])))
    for doc in documents:
        a = ws.activity.get(doc) or {}
        if a.get("partial") or a.get("read_partial"):
            return out | {"reason": "region_not_read", "searched": True,
                          "variant": "document_read_in_part" if a.get("read_partial") else "document_partial",
                          "document": a.get("title"), "document_id": doc}
    if out["searched"]:
        return out | {"reason": "not_located", "variant": "searched"}
    return out | {"reason": "not_located"}


def _claims_by_component(ws: Workspace, requested) -> tuple[dict[str, dict], list[dict]]:
    """The model's validated claims: the strongest per component id, and those linked to none (strongest per label)."""
    linked: dict[str, list[dict]] = {}
    unlinked: dict[str, list[dict]] = {}
    for r in validate_requested(ws, requested):
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
    gap paragraph (a leaf not answered or needing clarification, and an instruction not met; a partly given leaf is
    listed in completeness only)."""
    claims, _ = _claims_by_component(ws, requested)
    outcomes = report.requirement_outcomes(applied)
    for o in outcomes:
        o |= {"limitation": None, "limitation_text": None, "place": None, "document": None, "document_id": None,
              "document_title": None,
              "searched": None, "claimed": (claims.get(o["id"]) or {}).get("claimed"), "detail": "", "variant": "",
              "gap": None}
        if o["children"] or o["status"] not in UNMET:
            continue
        why = reason_of(ws, o, turn, claims.get(o["id"]))
        o |= {"limitation": why["reason"], "limitation_text": REASONS[why["reason"]], "place": why.get("place"),
              "document": why.get("document"), "document_id": why.get("document_id"), "searched": why["searched"],
              "detail": why.get("detail") or "", "variant": why.get("variant") or ""}
        if o["document_id"]:
            o["document_title"] = (ws.activity.get(o["document_id"]) or {}).get("title")
        o["stated"] = o["status"] != "partial" or o["kind"] == "instruction"
        o["gap"] = gap_sentence(why["reason"], o["variant"], [o["text"]], o["place"], o["document"], o["detail"])
    return outcomes


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
        texts = list(dict.fromkeys(m["text"] for m in members))
        line = gap_sentence(key[0], key[1], texts, members[0].get("place"), key[4], key[5])
        if any(g["text"] == line for g in out):
            continue
        out.append({"reason": key[0], "reason_text": REASONS[key[0]],
                    "components": [m["id"] for m in members if m.get("id")], "texts": texts, "text": line})
    return out


def gap_lines(items: list[dict]) -> list[str]:
    """The gap paragraph's lines (``gap_groups``)."""
    return [g["text"] for g in gap_groups(items)]


# what the payload keeps of each component (the UI's per-component detail)
PUBLIC_FIELDS = ("id", "text", "kind", "aspect", "parent", "conditional", "subject", "status", "evidence_state",
                 "limitation", "limitation_text", "stated", "gap", "units", "removed_units", "removal_kinds",
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
        out.append({"text": r["label"], "limitation": gap["reason"], "variant": gap["variant"],
                    "place": gap["place"], "document": gap["document"], "detail": ""})
    return out


def _append(answer: FinalAnswer, lines: list[str]) -> FinalAnswer:
    body = answer.answer_markdown.strip()
    status = "partial" if answer.status == "answered" else answer.status
    return answer.model_copy(update={"answer_markdown": (body + "\n\n" if body else "") + "\n".join(lines),
                                     "status": status})


def state_components(ws: Workspace, answer: FinalAnswer, report,
                     turn: TurnRequirements | None = None) -> tuple[FinalAnswer, list[dict]]:
    """The verified answer as the server finishes it (round 7 U3, ``engine.finish``), in order: the units that failed
    removed (``VerifyReport.apply``); each component's outcome recomputed from the surviving units with its reason
    (``component_outcomes``); the units stating an absence of a component the server states removed
    (``VerifyReport.supersede``, then applied again); and one gap paragraph after the answer (``gap_lines``). A
    component not given makes the answer at most ``partial``. Returns the answer, tidied, and the outcomes (none
    for a clarification, which asks and states nothing)."""
    final = report.apply(answer)
    if answer.status == "clarification":
        return final, []
    outcomes = component_outcomes(ws, report, final, turn, answer.requested) if report.requirements else []
    if outcomes and report.supersede(outcomes):
        final = report.apply(answer)
        outcomes = component_outcomes(ws, report, final, turn, answer.requested)
    items = [o for o in outcomes if o["stated"]] + ([] if report.requirements else _unlinked_gaps(ws, answer.requested))
    groups = gap_groups(items)
    report.gaps = groups
    lines = [g["text"] for g in groups]
    if lines:
        final = _append(final, lines)
    elif any(not o["children"] and o["status"] in UNMET for o in outcomes) and final.status == "answered":
        final = final.model_copy(update={"status": "partial"})
    return final.model_copy(update={"answer_markdown": tidy(final.answer_markdown)}), outcomes


def gap_documents(outcomes: list[dict]) -> list[dict]:
    """The documents the gap statements name (a section read, a document read in part): permission checks cover
    them (``LEDGER_DOCUMENT_KEYS``)."""
    out: dict[str, dict] = {}
    for o in outcomes:
        if o.get("stated") and o.get("document_id"):
            out[o["document_id"]] = {"document_id": o["document_id"], "title": o.get("document_title")}
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
                         "parent": o.get("parent") or "", "conditional": bool(o.get("conditional"))}
                        for o in leaves if o["status"] != "full"]}


# a line with nothing left but markup: a list marker, a list item's number, a quote mark, bold marks
_ORPHAN_LINE = re.compile(r"(?:[-*+]|\d{1,3}[.)]|>)?\s*(?:\*\*|__)?\s*(?:\*\*|__)?")


def tidy(markdown: str) -> str:
    """The answer without the remnants of removals and additions (R22): a line left with nothing but markup, and a
    prose line repeated (an absence sentence said twice); tables are kept as they are."""
    out: list[str] = []
    seen: set[str] = set()
    for line in markdown.split("\n"):
        s = line.strip()
        if s and not s.startswith("|"):
            if _ORPHAN_LINE.fullmatch(s):
                continue
            if re.search(r"[א-תA-Za-z0-9]", s):
                if s in seen:
                    continue
                seen.add(s)
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


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


def build(ws: Workspace, answer: FinalAnswer, question: str) -> tuple[dict, FinalAnswer]:
    """The coverage ledger of the answer, and the answer with the coverage note and status cap when needed."""
    from app.chat.tools import LEVELS, new_scope
    from app.platform.search import (
        documents_matching,
        documents_named,
        documents_sharing_title_words,
    )

    cited = cited_documents(ws, answer.answer_markdown)
    for doc, title in cited.items():  # its datum passed verification (what failed was removed before this)
        ws.touch(doc, title, "verified")
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
            with tenant_tx(ws.ctx) as conn:
                found = documents_matching(conn, query)
            scope = new_scope(query, [(d["document_id"], d["title"]) for d in found], 1)
        matching = scope["matching"]
        omitted = []
        for o in answer.omitted:
            doc = str(o.document_id or "")
            title = next((m["title"] for m in matching if m["document_id"] == doc),
                         (ws.activity.get(doc) or {}).get("title"))
            omitted.append({"document_id": doc if title else None, "title": title, "what": o.what, "why": o.why})
        explained = {o["document_id"] for o in omitted if o["document_id"]}
        levels = {m["document_id"]: _level(ws, m["document_id"]) for m in matching}
        reached = [m for m in matching if levels[m["document_id"]] in LEVELS[1:]]
        reached_ids = {m["document_id"] for m in reached}
        with_data = [m for m in matching if m["document_id"] in cited]
        read = [m for m in matching if _read(ws, m["document_id"])]
        retrieved_only = [m for m in reached if not _read(ws, m["document_id"])]
        not_checked = [m for m in matching if m["document_id"] not in reached_ids]
        unused = [m for m in reached if m["document_id"] not in cited and m["document_id"] not in explained]
        partially = [m for m in reached if ws.activity[m["document_id"]]["partial"]]
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
        with tenant_tx(ws.ctx) as conn:
            named = documents_named(conn, question)
            shared = {} if named else documents_sharing_title_words(conn, question)
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
