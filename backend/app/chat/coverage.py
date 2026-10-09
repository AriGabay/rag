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

A datum that was not found gets exactly one limitation, of four kinds (R21), derived from what the turn did: not
found in the search performed (search only); the source was read only in part (a read clipped or with an unread
region, or a document read in part); the relevant source was read and does not show it (a section or table read
to its end); the sources conflict (values registered this turn give it different amounts). One sentence per datum:
requested data are deduplicated by label, and a part of the request the verified answer does not cover gets one
too (``state_parts``), unless it is already stated.

What the request requires is derived by the verification judge (``verify.TurnRequirements``, KTD7). A requirement the
verified answer does not give gets one sentence with its reason (R21), computed from what the turn found and did
(``limitation``): a tool or provider failure; a calculation that failed, or whose inputs were found and never
computed; the sources disagree, or the documents do not allow a conclusion (insufficient to conclude); a value found
with an uncertain reading or meaning, or a document it needs read only in part (found but uncertain); not found in
the search — only when a search or reading covered it; otherwise not searched, never "not found". The answer is then
tidied (``tidy``): no orphan list marker and no repeated line is left by the removals and additions (R22).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import TYPE_CHECKING

from app.chat.tools import READ_TO_END
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
                        "partially_read", "omitted", "also_matching", "tables", "requirements")


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


# the four kinds of limitation (R21), and the requested status each comes from
ABSENCE_KINDS = {"not_found_search": "לא נמצא בחיפוש שבוצע", "source_partial": "המקור נקרא רק בחלקו",
                 "read_absent": "המקורות הרלוונטיים נקראו ואינם מציגים את הנתון", "sources_conflict": "המקורות סותרים"}
_KIND_OF_STATUS = {"not_found_search": "not_found_search", "source_partial": "source_partial",
                   "section_checked_absent": "read_absent", "sources_conflict": "sources_conflict"}
# when one datum is claimed missing more than once: the limitation the turn supports most specifically
_STRENGTH = ("section_checked_absent", "sources_conflict", "source_partial", "not_found_search")


def _norm_label(label: str) -> str:
    return " ".join(re.sub(r"[*_`]+", " ", label or "").split()).strip(" :.").casefold()


def conflicting(ws: Workspace, document_ids: list[str] | None = None) -> bool:
    """Whether values the turn registered (V#) or measurements it listed (M#) give one datum different amounts:
    the same kind (by family), unit, period, area basis and named subject, and different numbers. Only documents
    in ``document_ids``, when given."""
    from app.answering.verify import numbers_in
    from app.chat.resolve import family

    seen: dict[tuple, set] = {}
    entries = [(str(v.document_id), v.kind, v.unit, v.period, v.area_basis, v.subject, str(v.value))
               for v in ws.values.values()]
    entries += [(str(m.document_id), m.row.metric_kind, m.row.unit, m.row.period, m.row.area_basis, m.row.subject,
                 frozenset(numbers_in(m.row.value_text or ""))) for m in ws.measurements.values()]
    for doc, kind, unit, period, basis, subject, amount in entries:
        if document_ids and doc not in document_ids:
            continue
        if not (subject or "").strip() or kind in (None, "unknown", "other"):
            continue  # a datum of no named subject cannot be said to conflict with another
        key = (family(kind), unit, period, _norm_label(basis or ""), _norm_label(subject))
        seen.setdefault(key, set()).add(amount)
    return any(len(amounts) > 1 for amounts in seen.values())


def validate_requested(ws: Workspace, requested) -> list[dict]:
    """Each requested datum with the status the turn's actions support. "The section was checked" needs a section,
    table or page range of one of its documents opened this turn and named in ``checked_where`` (a measurements
    listing is not a reading of the section), read to its end with no unread region in it (R12): one read only in
    part — clipped and not continued to the end, or with a region that was not read — supports only "the source was
    read in part" (``partial_reason``: ``clipped`` or ``unread``). "Read in part" otherwise needs one of its
    documents read in part. "The sources conflict" needs values of the turn that conflict (``conflicting``);
    without them the datum was found. An unsupported status falls to the strongest one the turn does support, down
    to "not found in the search". ``kind`` is the limitation's kind (``ABSENCE_KINDS``), None when found."""
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
        out.append({"label": r.label.strip(), "document_ids": docs, "status": status, "claimed": r.status,
                    "checked_where": where["sid"] if where else None, "section": where["name"] if where else None,
                    "scope": where["scope"] if where else None, "partial_document": partial,
                    "partial_reason": reason, "kind": _KIND_OF_STATUS.get(status)})
    return out


def _place(r: dict) -> tuple[str, str, str]:
    """Where a datum was looked for: (in it, of it, what it is) — "בסעיף "X"", "מהסעיף "X"", "הסעיף"."""
    scope, name = r.get("scope") or "section", r.get("section") or ""
    if scope == "pages":
        return f"ב{name}", f"מ{name}", "הטווח"
    if scope == "table":
        return f"בטבלה \"{name}\"", f"מהטבלה \"{name}\"", "הטבלה"
    return f"בסעיף \"{name}\"", f"מהסעיף \"{name}\"", "הסעיף"


def absence_sentence(r: dict) -> str | None:
    """The opening sentence for a datum that was not found, at the level the turn checked."""
    label = " ".join(re.sub(r"\*+", " ", r["label"]).split())
    in_it, of_it, what = _place(r)
    if r["status"] == "section_checked_absent":
        return f"**{label}** לא מופיע {in_it} שנבדק [{r['checked_where']}]."
    if r["status"] == "source_partial" and r.get("partial_reason") == "clipped":
        return (f"**{label}** לא נמצא בחלק שנקרא {of_it}: {what} נקרא רק בחלקו, ולכן ייתכן שהנתון מופיע בחלק שלא "
                "נקרא.")
    if r["status"] == "source_partial" and r.get("partial_reason") == "unread":
        return (f"**{label}** לא נמצא {in_it}, אבל יש בו אזורים שלא נקראו (תמונה או טבלה), ולכן ייתכן שהנתון "
                "מופיע בהם.")
    if r["status"] == "source_partial" and r.get("partial_reason") == "read_in_part":
        return (f"**{label}** לא נמצא. המסמך \"{r['partial_document']}\" נקרא רק בחלקו, ולכן ייתכן שהנתון מופיע "
                "בחלק שלא נקרא.")
    if r["status"] == "source_partial":
        return (f"**{label}** לא נמצא. המסמך \"{r['partial_document']}\" נקרא חלקית (חלק מהתמונות או העמודים לא "
                "נקראו), ולכן ייתכן שהנתון מופיע בחלק שלא נקרא.")
    if r["status"] == "sources_conflict":
        return f"**{label}**: המקורות סותרים — הם נותנים לנתון ערכים שונים, ולכן אין לו ערך אחד."
    if r["status"] == "not_found_search":
        return f"**{label}** לא נמצא בחיפוש במסמכים שנבדקו."
    return None


def _per_label(rows: list[dict]) -> dict[str, list[dict]]:
    """The validated limitations of each datum (by its label, normalized), strongest first; found data left out."""
    out: dict[str, list[dict]] = {}
    for r in rows:
        if r["status"] in _STRENGTH:
            out.setdefault(_norm_label(r["label"]), []).append(r)
    for rs in out.values():
        rs.sort(key=lambda r: _STRENGTH.index(r["status"]))
    return out


def _absence_sentences(ws: Workspace, answer: FinalAnswer, *, cited: bool,
                       withdrawn: Collection[str] = ()) -> list[str]:
    """One sentence per datum not found. ``cited=True``: the data whose section or table was read and does not
    show them. ``cited=False``: every other datum — and one whose section sentence is not in the answer (removed
    in verification) falls to its next limitation. A sentence verification ``withdrawn`` (its datum was found) is
    not given."""
    out: list[str] = []
    for rows in _per_label(validate_requested(ws, answer.requested)).values():
        if cited:
            chosen = rows[0] if rows[0]["status"] == "section_checked_absent" else None
        else:
            stated = [r for r in rows if r["status"] == "section_checked_absent"
                      and absence_sentence(r) in answer.answer_markdown]
            chosen = None if stated else next((r for r in rows if r["status"] != "section_checked_absent"), None)
        sentence = absence_sentence(chosen) if chosen else None
        if sentence and sentence not in answer.answer_markdown and sentence not in out and sentence not in withdrawn:
            out.append(sentence)
    return out


def planned_statements(ws: Workspace, answer: FinalAnswer) -> list[str]:
    """The sentences ``state_absence(cited=False)`` will add to this answer after verification: the judge reads
    them beside the answer, so a part of the request they state missing is not stated again."""
    if answer.status == "clarification":
        return []
    return _absence_sentences(ws, answer, cited=False)


def _prepend(answer: FinalAnswer, sentences: list[str]) -> FinalAnswer:
    body = answer.answer_markdown.strip()
    status = "partial" if answer.status == "answered" else answer.status
    return answer.model_copy(update={"answer_markdown": "\n".join(sentences) + ("\n\n" + body if body else ""),
                                     "status": status})


def state_absence(ws: Workspace, answer: FinalAnswer, *, cited: bool, withdrawn: Collection[str] = ()) -> FinalAnswer:
    """The answer opened by a sentence for each requested datum that was not found, before any near datum the
    answer gives; its status is then at most ``partial``.

    Two kinds, added at two points. ``cited=True`` (before verification): "not in the section that was checked",
    which cites the opened section it rests on and is judged like any claim. ``cited=False`` (after
    verification): "not found in the search", "the document was read in part" and "the sources conflict" —
    statements about what the turn did, validated against it (``validate_requested``), with no source a judge
    could read. A datum gets one sentence, however many times it is listed; none when verification ``withdrew`` it
    (the turn holds the datum's values)."""
    if answer.status == "clarification":
        return answer
    sentences = _absence_sentences(ws, answer, cited=cited, withdrawn=withdrawn)
    return _prepend(answer, sentences) if sentences else answer


# the reasons a requirement is not given (R21), as the completeness indicator names them
LIMITATIONS = {
    "not_found": "לא נמצא בחיפוש שבוצע",
    "uncertain": "נמצא, אך לא בוודאות",
    "tool_failure": "תקלה בכלי או בשירות המודל",
    "calculation_incomplete": "החישוב לא הושלם",
    "insufficient": "אין די במקורות כדי להכריע",
    "not_searched": "לא נבדק",
}
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
    for i in ids:
        v = ws.values.get(i)
        if v is None:
            continue
        source = ws.sources.get(v.source_id)
        if v.certainty != "verified" or i in ws.uncertain_values:
            return True
        if (source is not None and source.status == "uncertain_reading" and v.reading != "clear"
                and i not in ws.settled_values):
            return True
    return False


def limitation(ws: Workspace, outcome: dict, turn: TurnRequirements | None = None) -> dict:
    """Why a requirement is not given, computed from what the turn found and did (R21): ``kind`` (a
    ``LIMITATIONS`` key) and the sentence that says so. "Not found in the search" only when a search or reading
    covered it and none of its data was found; with no search behind it, "not searched"."""
    label = " ".join(re.sub(r"\*+", " ", outcome["text"]).split())
    ev = related_evidence(ws, outcome, turn)
    kinds = {f["kind"] for f in ev["failures"]}
    partly = next((ws.activity[d]["title"] for d in ev["documents"] if d in ws.activity
                   and (ws.activity[d].get("partial") or ws.activity[d].get("read_partial"))), None)
    if "tool" in kinds:
        return {"kind": "tool_failure",
                "sentence": f"**{label}** לא הושלם בגלל תקלה בכלי או בשירות המודל בזמן הבדיקה."}
    if "calculation" in kinds:
        return {"kind": "calculation_incomplete",
                "sentence": f"**{label}**: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."}
    if ev["data"] and len(ev["documents"]) and conflicting(ws, ev["documents"]):
        return {"kind": "insufficient",
                "sentence": f"**{label}**: המקורות סותרים — הם נותנים ערכים שונים, ולכן אין די כדי להכריע."}
    if outcome["status"] == "undeterminable":
        return {"kind": "insufficient", "sentence": f"**{label}**: המסמכים אינם מספיקים כדי להכריע בכך."}
    if _uncertain_value(ws, ev["data"]):
        return {"kind": "uncertain", "sentence": (f"**{label}**: נמצא ערך, אבל קריאתו או משמעותו אינן ודאיות, ולכן "
                                                  "הוא לא הוצג כנתון מאומת.")}
    if partly:
        return {"kind": "uncertain",
                "sentence": f"**{label}**: המסמך \"{partly}\" נקרא רק בחלקו, ולכן אין ודאות לגבי הנתון."}
    if ev["data"] and outcome.get("calculation"):
        return {"kind": "calculation_incomplete",
                "sentence": f"**{label}**: הנתונים לחישוב נמצאו, אבל החישוב לא הושלם."}
    if ev["data"]:
        return {"kind": "insufficient", "sentence": f"**{label}**: נמצאו נתונים, אבל אין בהם די כדי להכריע בכך."}
    if ev["checks"]:
        return {"kind": "not_found", "sentence": f"**{label}** לא נמצא בחיפוש במסמכים שנבדקו."}
    return {"kind": "not_searched", "sentence": (f"**{label}** לא נבדק: לא בוצע חיפוש או קריאה שמכסים אותו, ולכן "
                                                 "לא ידוע אם הוא מופיע במסמכים.")}


def state_parts(ws: Workspace, answer: FinalAnswer, report,
                turn: TurnRequirements | None = None) -> tuple[FinalAnswer, list[dict]]:
    """Each requirement of the request with its status in the verified answer (``VerifyReport.requirement_outcomes``)
    and, when not given in full, its limitation (``limitation``); the answer opened by a sentence for each
    requirement it neither gives nor says is missing or undeterminable, once per label. A requirement not given in
    full makes the answer at most ``partial``. The answer is tidied last (``tidy``)."""
    if answer.status == "clarification" or not report.requirements:
        return answer, []
    outcomes = report.requirement_outcomes(answer)
    # the data whose limitation the answer already states (a requested datum's sentence that is in the answer)
    stated = {_norm_label(r["label"]) for r in validate_requested(ws, answer.requested)
              if r["kind"] and absence_sentence(r) in answer.answer_markdown}
    sentences: list[str] = []
    for o in outcomes:
        lim = limitation(ws, o, turn) if o["status"] != "full" else None
        o["limitation"] = lim["kind"] if lim else None
        o["limitation_text"] = LIMITATIONS[lim["kind"]] if lim else None
        if lim is None or o["status"] == "partial" or o["stated"]:
            continue
        sentence = lim["sentence"]
        if _norm_label(o["text"]) not in stated and sentence not in sentences and sentence not in answer.answer_markdown:
            stated.add(_norm_label(o["text"]))
            sentences.append(sentence)
    if sentences:
        answer = _prepend(answer, sentences)
    elif any(o["status"] != "full" for o in outcomes) and answer.status == "answered":
        answer = answer.model_copy(update={"status": "partial"})
    return answer.model_copy(update={"answer_markdown": tidy(answer.answer_markdown)}), outcomes


def completeness(outcomes: list[dict]) -> dict | None:
    """The answer's completeness, apart from its correctness (R19): ``full`` (every requirement given), ``partial``
    (some given, in full or in part), ``missing`` (none given) or ``undeterminable`` (none can be concluded from the
    documents); and each requirement not given in full with its reason, for the line under the answer. None when no
    requirements were judged."""
    if not outcomes:
        return None
    statuses = [o["status"] for o in outcomes]
    if all(s == "full" for s in statuses):
        status = "full"
    elif any(s in ("full", "partial") for s in statuses):
        status = "partial"
    elif all(s == "undeterminable" for s in statuses):
        status = "undeterminable"
    else:
        status = "missing"
    return {"status": status, "requirements": len(outcomes),
            "missing": [{"id": o["id"], "text": o["text"], "status": o["status"], "reason": o["limitation"],
                         "reason_text": LIMITATIONS.get(o["limitation"])} for o in outcomes if o["status"] != "full"]}


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
