"""What an answer covered, stated by the server from what the turn's tools actually did.

The model declares only what kind of question it answered — about one document or property (``focused``) or
about a set (``set``, with the terms that define the set) — and which data it found but left out, and why. The
server knows the rest: the documents of the set (``find_documents``, or the whole repository from
``list_documents``), which of them the tools touched, which the answer cites, and which were read only in part.

For a set, an answer that did not check every document of the set, or that checked one and used nothing from it
without saying why, gets a coverage note naming them, and its status is at most ``partial``: a result is never
presented as covering the set when it covers part of it. For a focused answer to a question that names no
document, when several documents share a distinctive word of the question in their titles and the answer cites
only some of them, the note names the others.
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
            out[str(ws.sources[i].document_id)] = ws.sources[i].title
        elif i in ws.measurements:
            out[str(ws.measurements[i].document_id)] = ws.measurements[i].title
        elif i in ws.computations:
            for mid in ws.computations[i].measurement_ids:
                if mid in ws.measurements:
                    out[str(ws.measurements[mid].document_id)] = ws.measurements[mid].title
    return out


LEDGER_DOCUMENT_KEYS = ("cited", "matching", "checked", "with_data", "not_checked", "unused", "partially_read",
                        "omitted", "also_matching")


def ledger_documents(ledger: dict | None) -> set[str]:
    """Every document id a coverage ledger names, under any of its keys (the permission check reads this)."""
    ids: set[str] = set()
    for key in LEDGER_DOCUMENT_KEYS:
        ids |= {d.get("document_id") for d in (ledger or {}).get(key) or [] if isinstance(d, dict)}
    ids.discard(None)
    return ids


def _names(docs: list[dict]) -> str:
    shown = "; ".join(d["title"] for d in docs[:NAMES_SHOWN])
    return shown + (f" ועוד {len(docs) - NAMES_SHOWN}" if len(docs) > NAMES_SHOWN else "")


def build(ws: Workspace, answer: FinalAnswer, question: str) -> tuple[dict, FinalAnswer]:
    """The coverage ledger of the answer, and the answer with the coverage note and status cap when needed."""
    from app.chat.tools import new_scope
    from app.platform.search import documents_matching, documents_named, documents_sharing_title_words

    cited = cited_documents(ws, answer.answer_markdown)
    scope_kind = answer.scope_kind
    ledger: dict = {"scope_kind": scope_kind, "scope_query": answer.scope_query or "",
                    "cited": [{"document_id": d, "title": t} for d, t in cited.items()]}
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
        checked = [m for m in matching if m["document_id"] in ws.activity]
        with_data = [m for m in matching if m["document_id"] in cited]
        not_checked = [m for m in matching if m["document_id"] not in ws.activity]
        unused = [m for m in checked if m["document_id"] not in cited and m["document_id"] not in explained]
        partially = [m for m in checked if ws.activity[m["document_id"]]["partial"]]
        complete = bool(matching) and not not_checked and not unused and not partially
        ledger |= {"scope_query": scope["query"], "matching": matching, "checked": checked, "with_data": with_data,
                   "not_checked": not_checked, "unused": unused, "partially_read": partially, "omitted": omitted,
                   "pages": scope.get("pages"), "pages_read": len(scope.get("pages_read") or ()),
                   "complete": complete}
        if not matching:
            note = f"**כיסוי:** לא נמצאו מסמכים שמכילים את כל המונחים של \"{scope['query']}\"."
        elif not complete:
            parts = [f"**כיסוי:** {len(matching)} מסמכים מתאימים לתחום \"{scope['query']}\"; התשובה מבוססת על "
                     f"{len(with_data)} מהם, ולכן אינה מכסה את כל התחום."]
            if not_checked:
                parts.append(f"לא נבדקו: {_names(not_checked)}.")
            if unused:
                parts.append(f"נבדקו, לא נמצא בהם נתון שנכלל בתשובה: {_names(unused)}.")
            if partially:
                parts.append(f"נקראו חלקית (חלק מהתמונות לא נקראו): {_names(partially)}.")
            note = " ".join(parts)
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
        status = "partial" if answer.status == "answered" else answer.status
        answer = answer.model_copy(update={"answer_markdown": (answer.answer_markdown.rstrip() + "\n\n> "
                                                               + note).strip(), "status": status})
    return ledger, answer
