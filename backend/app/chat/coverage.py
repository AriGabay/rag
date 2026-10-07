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
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.db import tenant_tx

if TYPE_CHECKING:
    from app.chat.engine import FinalAnswer
    from app.chat.tools import Workspace

_CITE = re.compile(r"\[([SMC]\d+(?:\s*[,،;]\s*[SMC]\d+)*)\]")
NAMES_SHOWN = 8


def cited_ids(markdown: str) -> set[str]:
    """The S#, M# and C# ids the answer cites."""
    ids: set[str] = set()
    for m in _CITE.finditer(markdown or ""):
        ids |= set(re.findall(r"[SMC]\d+", m.group(1)))
    return ids


def cited_documents(ws: Workspace, markdown: str) -> dict[str, str]:
    """Document id -> title of every source, measurement and computation input the answer cites."""
    out: dict[str, str] = {}
    for i in cited_ids(markdown):
        if i in ws.sources:
            if not ws.sources[i].is_listing:  # a listing names documents; it is not one of them
                out[str(ws.sources[i].document_id)] = ws.sources[i].title
        elif i in ws.measurements:
            out[str(ws.measurements[i].document_id)] = ws.measurements[i].title
        elif i in ws.computations:
            for mid in ws.computations[i].measurement_ids:
                if mid in ws.measurements:
                    out[str(ws.measurements[mid].document_id)] = ws.measurements[mid].title
    return out


LEDGER_DOCUMENT_KEYS = ("cited", "matching", "read", "retrieved_only", "with_data", "not_checked", "unused",
                        "partially_read", "omitted", "also_matching", "tables")


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


def _names(docs: list[dict]) -> str:
    shown = "; ".join(d["title"] for d in docs[:NAMES_SHOWN])
    return shown + (f" ועוד {len(docs) - NAMES_SHOWN}" if len(docs) > NAMES_SHOWN else "")


def validate_requested(ws: Workspace, requested) -> list[dict]:
    """Each requested datum with the status the turn's actions support. "The section was checked" needs a section
    or table of one of its documents opened this turn and named in ``checked_where`` (a measurements listing is
    not a reading of the section); "read in part" needs one of its documents read in part. An unsupported status
    falls to the strongest one the turn does support, down to "not found in the search"."""
    out = []
    for r in requested or []:
        docs = [d for d in r.document_ids if d in ws.activity]
        openings = {o["sid"]: o for d in docs for o in ws.activity[d]["openings"]}
        status, where = r.status, None
        if status == "section_checked_absent":
            where = openings.get((r.checked_where or "").strip())
            if where is None:
                status = "source_partial"
        if status == "source_partial" and not any(ws.activity[d]["partial"] for d in docs):
            status = "not_found_search"
        partial = next((ws.activity[d]["title"] for d in docs if ws.activity[d]["partial"]), None)
        out.append({"label": r.label.strip(), "document_ids": docs, "status": status, "claimed": r.status,
                    "checked_where": where["sid"] if where else None, "section": where["name"] if where else None,
                    "scope": where["scope"] if where else None, "partial_document": partial})
    return out


def absence_sentence(r: dict) -> str | None:
    """The opening sentence for a datum that was not found, at the level the turn checked."""
    label = r["label"]
    if r["status"] == "section_checked_absent":
        place = "בטבלה" if r["scope"] == "table" else "בסעיף"
        return f"**{label}** לא מופיע {place} \"{r['section']}\" שנבדק [{r['checked_where']}]."
    if r["status"] == "source_partial":
        return (f"**{label}** לא נמצא. המסמך \"{r['partial_document']}\" נקרא חלקית (חלק מהתמונות או העמודים לא "
                "נקראו), ולכן ייתכן שהנתון מופיע בחלק שלא נקרא.")
    if r["status"] == "not_found_search":
        return f"**{label}** לא נמצא בחיפוש במסמכים שנבדקו."
    return None


def state_absence(ws: Workspace, answer: FinalAnswer, *, cited: bool) -> FinalAnswer:
    """The answer opened by a sentence for each requested datum that was not found, before any near datum the
    answer gives; its status is then at most ``partial``.

    Two kinds, added at two points. ``cited=True`` (before verification): "not in the section that was checked",
    which cites the opened section it rests on and is judged like any claim. ``cited=False`` (after
    verification): "not found in the search" and "the document was read in part" — statements about what the
    turn did, validated against it (``validate_requested``), with no source a judge could read."""
    if answer.status == "clarification":
        return answer
    checked = [r for r in validate_requested(ws, answer.requested)
               if (r["status"] == "section_checked_absent") == cited]
    sentences = [x for x in (absence_sentence(r) for r in checked) if x]
    if not sentences:
        return answer
    body = answer.answer_markdown.strip()
    status = "partial" if answer.status == "answered" else answer.status
    return answer.model_copy(update={"answer_markdown": "\n".join(sentences) + ("\n\n" + body if body else ""),
                                     "status": status})


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
    from app.platform.search import documents_matching, documents_named, documents_sharing_title_words

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
            parts = [f"**כיסוי:** {len(matching)} מסמכים מתאימים לתחום \"{scope['query']}\"; התשובה מבוססת על "
                     f"{len(with_data)} מהם, ולכן אינה מכסה את כל התחום ({counts})."]
            if not_checked:
                parts.append(f"לא נבדקו: {_names(not_checked)}.")
            if unused:
                parts.append(f"נבדקו, לא נמצא בהם נתון שנכלל בתשובה: {_names(unused)}.")
            if partially:
                parts.append(f"נקראו חלקית (חלק מהתמונות לא נקראו): {_names(partially)}.")
            note = " ".join(parts)
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
        for docs in shared.values():
            ids = {str(i): t for i, t in docs}
            if set(ids) & set(cited) and set(ids) - set(cited):
                others |= {i: t for i, t in ids.items() if i not in cited}
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
