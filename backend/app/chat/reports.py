"""Report a problematic answer (round 25 U5: R11, KTD9).

A user flags an answer of their own. The report row keeps the ids (message, conversation, cited sources, the build's commit sha and
the model ids) and a STRUCTURAL snapshot of the run's diagnostics, so a later retry (which deletes the message and, by cascade, its
diagnostics) or a deleted conversation does not lose the evidence, and the row holds no document or answer text.

The snapshot is built by an allowlist (``structural``): a key is kept only when it is in ``ALLOWED_KEYS`` (the children of a
``MAP_KEYS`` map are kept when their key is a code), a string is kept only when it is a code (``SAFE_CODE``: ASCII, no spaces, at most
64 characters - an id, a status, a check's name, a timestamp), numbers and booleans are kept, a list is cut at ``LIST_KEPT`` and the depth
at ``MAX_DEPTH``. So a draft, an answer, a quote, a label, a cell or a failure's free text never gets in, even under a key that is
allowed, and a new field a later round adds to the diagnostics stays out until someone adds its name here. The reporter's note is the
only free text in the row; the admin's detail view shows it, the export does not.

Access: the office comes from the session (``get_ctx``); the message is read under the reporter's own row security (messages are
owner-only), so another user's message is a 404. The report takes the chat's per-conversation advisory lock (the one retry takes) and a
row lock on the message, so a racing retry or conversation delete gives 404 or 409, never a foreign-key error. The office admin lists,
reads and exports the office's reports (row security: office isolation plus owner-or-admin); the detail and the export show a document
id only while the admin sees that document (``api._visible_ids``), and both reads are audited like ``chat.diagnostics.read``."""

from __future__ import annotations

import json
import math
import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.audit import audit
from app.chat import api as chat_api
from app.config import get_settings
from app.db import TenantContext, tenant_tx
from app.deps import NOT_FOUND, get_ctx, parse_uuid, require_admin

router = APIRouter(tags=["answer-reports"])

CATEGORIES = ("wrong", "missing", "wrong_source", "wrong_property", "other")
NOTE_MAX = 1000
SNAPSHOT_VERSION = 1
SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:#/\-]{1,64}$")
LIST_KEPT = 100   # the most items of any list the snapshot keeps
MAX_DEPTH = 8
LISTED = 200   # the most reports one list call returns
REDACTED = {"redacted": "document_not_visible"}

# the maps whose children are named by a code of their own (an id, a stage, a tool, a cause): their keys are kept when they are codes
MAP_KEYS = frozenset({"sources", "stages", "reads", "not_checked", "waits", "decisions", "repairable_kinds", "skipped"})
# the names the snapshot may hold. Each is an id, a locator part, a status or code, a count or a number of seconds - never text.
ALLOWED_KEYS = MAP_KEYS | frozenset({
    # the snapshot's own shape
    "version", "answer", "counts", "cited_sources", "cited_measurements", "cited_values", "document_ids", "limits_hit", "rounds",
    "removed", "resolution", "redacted",
    "kind", "status", "judged", "partial", "annotated", "completed", "drafts", "requirements", "problems", "measurements", "values",
    # ids and structural locators (document, version, reading, block, table and row indexes, page)
    "id", "type", "document_id", "version_id", "reading_id", "chunk_id", "source_id", "source", "document", "claim_id", "component",
    "checked_ids", "sources_checked", "expanded_source", "leaves", "block_start", "block_end", "block_index", "block_first",
    "block_last", "table_index", "row_index", "table_part", "page", "page_list", "pos", "left", "cursor", "listed_document_ids",
    # verification problems and decisions: codes
    "severity", "failure_kind", "check", "repair_attempted", "cause", "basis", "claim", "action", "verdict", "reason", "outcome",
    "post_check", "ordinal", "rereads", "scope", "why", "supported", "not_verified",
    # the stage clock, the repair gate and the reads
    "timing", "total_seconds", "unattributed_seconds", "tools", "tool", "stage", "seconds", "chars", "pages", "search", "run",
    "memoized", "lexical", "fused", "depth", "attempted", "failed_with", "budget_skipped", "calls", "tables_served",
    "multi_page_tables", "repair_gate", "unfollowed_cursors", "attempt", "decision", "steps", "refused_by", "estimate", "window",
    "offered", "kept", "dropped", "ok", "settled", "answer_seconds", "turn_seconds", "kinds", "request_analysis", "round",
    "confirm", "claims", "flips", "max_steps", "judge_status", "repairs_allowed", "seconds_to_gate", "seconds_to_deadline",
    "gate_window_seconds", "reused_on_timeout", "evidence_calls", "returned", "outcomes",
    # the turn's time decision
    "time", "mode", "anchor", "requested", "prefer_recent",
    # the requirements trace and the item ledger
    "requirements_trace", "item_ledger", "final_status", "votes", "reused", "passages", "rendered", "ids", "support", "support_omitted",
    "origin", "call", "call_units", "history", "history_omitted", "rests_on", "rests_omitted", "caps", "evidence", "digest", "items",
    "excluded", "misattributed", "candidates", "unbound", "state", "declared", "covered_by", "condition_check", "removed_in_verification",
    "removal_kind", "units", "from_children",
    # a failed turn's cause, as codes
    "failure",
})


class _Drop:
    pass


_DROP = _Drop()


def structural(value, parent: str | None = None, depth: int = 0):
    """``value`` reduced to its structure: allowed keys, codes, numbers and booleans (see the module's docstring). Anything else is
    dropped, a dict or list that ends up empty stays empty."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP
    if isinstance(value, str):
        return value if SAFE_CODE.match(value) else _DROP
    if depth >= MAX_DEPTH:
        return _DROP
    if isinstance(value, dict):
        out: dict = {}
        in_map = parent in MAP_KEYS
        for k, v in value.items():
            if not isinstance(k, str):
                continue
            if in_map:   # a map's child is named by a code, and its own value is read as any other (its keys must be allowed)
                if not SAFE_CODE.match(k):
                    continue
                kept = structural(v, None, depth + 1)
            elif k in ALLOWED_KEYS:
                kept = structural(v, k, depth + 1)
            else:
                continue
            if kept is not _DROP:
                out[k] = kept
        return out
    if isinstance(value, (list, tuple)):
        kept_items = (structural(v, None, depth + 1) for v in list(value)[:LIST_KEPT])
        return [v for v in kept_items if v is not _DROP]
    return _DROP


def _pick(item, keys: tuple[str, ...]) -> dict:
    return {k: item.get(k) for k in keys if isinstance(item, dict) and item.get(k) is not None}


_LOCATOR_KEYS = ("id", "document_id", "version_id", "reading_id", "kind", "block_start", "block_end", "block_index", "table_index",
                 "row_index", "page_list", "status")


def _failure(failure) -> dict:
    """A failed turn's cause as codes: its status and the stage the detail starts with (``final_text``), never the detail."""
    if not isinstance(failure, dict):
        return {}
    out = structural({k: v for k, v in failure.items() if k != "detail"}, "failure", 1)
    out = out if isinstance(out, dict) else {}
    head = str(failure.get("detail") or "").split(":", 1)[0].strip()
    if re.fullmatch(r"[a-z_]{1,32}", head):
        out["stage"] = head
    return out


def build_snapshot(message, answer: dict | None, diagnostics) -> dict:
    """The structural snapshot of one message: its answer's kind and status, the cited ids and locators, and its diagnostics row
    (None: the message has none) through the allowlist."""
    answer = answer if isinstance(answer, dict) else {}
    rounds = list(diagnostics.rounds or []) if diagnostics is not None else []
    removed = list(diagnostics.removed or []) if diagnostics is not None else []
    resolution = dict(diagnostics.resolution or {}) if diagnostics is not None and diagnostics.resolution else {}
    sources = [s for s in answer.get("sources") or [] if isinstance(s, dict)]
    measurements = [s for s in answer.get("measurements") or [] if isinstance(s, dict)]
    values = [s for s in answer.get("values") or [] if isinstance(s, dict)]
    kept = {k: resolution[k] for k in ("timing", "requirements_trace", "item_ledger", "drafts", "decisions", "time") if k in resolution}
    snap = {
        "version": SNAPSHOT_VERSION,
        "answer": structural(_pick(answer, ("kind", "status")) | (
            {"verification": answer["verification"]} if isinstance(answer.get("verification"), dict) else {}), "answer", 1),
        "cited_sources": [structural(_pick(s, _LOCATOR_KEYS)) for s in sources[:LIST_KEPT]],
        "cited_measurements": [structural(_pick(s, _LOCATOR_KEYS)) for s in measurements[:LIST_KEPT]],
        "cited_values": [structural(_pick(s, _LOCATOR_KEYS)) for s in values[:LIST_KEPT]],
        "document_ids": sorted({str(d) for d in (diagnostics.document_ids if diagnostics is not None else None) or []
                                if SAFE_CODE.match(str(d))}),
        "limits_hit": structural(list(diagnostics.limits_hit or []) if diagnostics is not None else []),
        "rounds": structural(rounds),
        "removed": structural(removed),
        "resolution": structural(kept) | ({"failure": _failure(resolution["failure"])} if "failure" in resolution else {}),
        "counts": {
            "rounds": len(rounds), "problems": sum(len(r) for r in rounds if isinstance(r, list)), "removed": len(removed),
            "drafts": len(resolution.get("drafts") or []), "requirements": len(resolution.get("requirements_trace") or []),
            "sources": len(sources), "measurements": len(measurements), "values": len(values),
            "document_ids": len(diagnostics.document_ids or []) if diagnostics is not None else 0,
            "limits_hit": len(diagnostics.limits_hit or []) if diagnostics is not None else 0},
    }
    return snap


def _run_ids(message) -> tuple[str, dict]:
    """The build's commit sha (set at image build; ``unknown`` without it) and the model ids the run used."""
    sha = (os.environ.get("APP_BUILD_SHA") or "").strip()
    s = get_settings()
    models = {"provider": s.llm_provider} | {p: s.model_for(p)[0] for p in ("agent", "resolve", "verify")}
    if message.model:
        models["message"] = message.model
    return (sha if SAFE_CODE.match(sha) else "unknown"), {k: v for k, v in models.items() if SAFE_CODE.match(str(v))}


class ReportBody(BaseModel):
    category: Literal["wrong", "missing", "wrong_source", "wrong_property", "other"]
    note: str = Field("", max_length=NOTE_MAX)


@router.post("/api/chat/messages/{message_id}/report")
def report_message(message_id: str, body: ReportBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    mid = parse_uuid(message_id)
    select = chat_api._SELECT + " WHERE m.id = :m AND m.role = 'assistant'"
    with tenant_tx(ctx) as conn:
        r = conn.execute(text(select), {"m": mid}).first()   # row security: the owner's messages only
        if r is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        chat_api.lock_conversation(conn, r.conversation_id)   # the lock a retry holds: what it deletes is gone when this resumes
        # the row lock keeps a conversation delete (which takes no chat lock) from removing the message between this read and the insert
        r = conn.execute(text(select + " FOR SHARE OF m"), {"m": mid}).first()
        if r is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        stale = r.status in ("running", "cancelling") and r.age > chat_api._stale_after()
        if r.status in ("running", "cancelling") and not stale:
            raise HTTPException(status.HTTP_409_CONFLICT, "התשובה עדיין בעיבוד")
        message_status = "failed" if stale else r.status
        diagnostics = conn.execute(text("SELECT * FROM message_diagnostics WHERE message_id = :m"), {"m": mid}).first()
        sha, models = _run_ids(r)
        values = {"u": ctx.user_id, "c": r.conversation_id, "m": mid, "cat": body.category, "n": body.note.strip(),
                  "s": message_status, "snap": json.dumps(build_snapshot(r, r.answer, diagnostics), ensure_ascii=False),
                  "sha": sha, "models": json.dumps(models)}
        try:
            with conn.begin_nested():
                row = conn.execute(text(
                    "INSERT INTO answer_reports (office_id, user_id, conversation_id, message_id, category, note, message_status,"
                    " snapshot, build_sha, model_ids) VALUES (app_office(), :u, :c, :m, :cat, :n, :s, CAST(:snap AS jsonb), :sha,"
                    " CAST(:models AS jsonb)) ON CONFLICT (user_id, message_id) WHERE message_id IS NOT NULL DO UPDATE SET"
                    " category = EXCLUDED.category, note = EXCLUDED.note, message_status = EXCLUDED.message_status,"
                    " snapshot = EXCLUDED.snapshot, build_sha = EXCLUDED.build_sha, model_ids = EXCLUDED.model_ids,"
                    " updated_at = now() RETURNING id, category, note, created_at, (xmax = 0) AS inserted"), values).one()
        except IntegrityError:   # the message or conversation went in the meantime: nothing to report on
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
        audit(conn, "answer_report.create" if row.inserted else "answer_report.update", ctx.user_id, "answer_report", row.id,
              category=body.category)
    return {"id": str(row.id), "category": row.category, "note": row.note, "message_id": str(mid), "updated": not row.inserted,
            "created_at": row.created_at.isoformat()}


@router.get("/api/chat/conversations/{conversation_id}/reports")
def conversation_reports(conversation_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    """The reporter's own reports of the messages still in this conversation (the marker on an answer reopens its report)."""
    with tenant_tx(ctx) as conn:
        conv = chat_api._conversation(conn, ctx, conversation_id)
        found = conn.execute(text(
            "SELECT id, message_id, category, note FROM answer_reports WHERE conversation_id = :c AND user_id = :u"
            " AND message_id IS NOT NULL ORDER BY created_at, id"), {"c": conv.id, "u": ctx.user_id})
        return {"reports": [{"id": str(x.id), "message_id": str(x.message_id), "category": x.category, "note": x.note}
                            for x in found]}


# --- the admin's views ---------------------------------------------------------------------------------------------------

def _document_ids(node) -> set[str]:
    """Every document id a snapshot names: ``document_id``, ``document_ids`` and ``listed_document_ids`` at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "document_id" and isinstance(v, str):
                found.add(v)
            elif k in ("document_ids", "listed_document_ids") and isinstance(v, list):
                found |= {x for x in v if isinstance(x, str)}
            else:
                found |= _document_ids(v)
    elif isinstance(node, list):
        for v in node:
            found |= _document_ids(v)
    return found


def hide_documents(node, hidden: set[str]):
    """``node`` without the documents in ``hidden``: a record naming one (its ``document_id``) is replaced by a marker, and the id goes
    out of the id lists. Nothing is changed when ``hidden`` is empty."""
    if not hidden:
        return node
    if isinstance(node, dict):
        if node.get("document_id") in hidden:
            return dict(REDACTED)
        return {k: ([x for x in v if x not in hidden] if k in ("document_ids", "listed_document_ids") and isinstance(v, list)
                    else hide_documents(v, hidden)) for k, v in node.items()}
    if isinstance(node, list):
        return [hide_documents(v, hidden) for v in node]
    return node


def _visible_snapshot(conn, snapshot: dict) -> dict:
    snapshot = snapshot or {}
    named = _document_ids(snapshot)
    return hide_documents(snapshot, named - chat_api._visible_ids(conn, named))


def _report_row(conn, report_id: str):
    row = conn.execute(text(
        "SELECT r.*, (r.message_id IS NOT NULL) AS message_exists, (r.conversation_id IS NOT NULL) AS conversation_exists"
        " FROM answer_reports r WHERE r.id = :i"), {"i": parse_uuid(report_id)}).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return row


def _head(row) -> dict:
    return {"message_id": str(row.message_id) if row.message_id else None,
            "conversation_id": str(row.conversation_id) if row.conversation_id else None,
            "message_exists": row.message_exists, "category": row.category, "message_status": row.message_status,
            "build_sha": row.build_sha, "model_ids": row.model_ids, "created_at": row.created_at.isoformat()}


@router.get("/api/admin/answer-reports")
def list_reports(limit: int = Query(100, ge=1, le=LISTED), ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        found = conn.execute(text(
            "SELECT id, category, message_status, created_at, (message_id IS NOT NULL) AS message_exists FROM answer_reports"
            " ORDER BY created_at DESC, id DESC LIMIT :n"), {"n": limit})
        return {"reports": [{"id": str(x.id), "category": x.category, "message_status": x.message_status,
                             "message_exists": x.message_exists, "created_at": x.created_at.isoformat()} for x in found]}


@router.get("/api/admin/answer-reports/{report_id}")
def report_detail(report_id: str, ctx: TenantContext = Depends(require_admin)) -> dict:
    """The report with its note and its structural snapshot, shown only with the documents the admin sees; the read is audited."""
    with tenant_tx(ctx) as conn:
        row = _report_row(conn, report_id)
        snapshot = _visible_snapshot(conn, row.snapshot)
        if row.user_id != ctx.user_id:
            audit(conn, "answer_report.read", ctx.user_id, "answer_report", row.id, owner=str(row.user_id))
    return {"id": str(row.id), "reporter_id": str(row.user_id), "note": row.note, "updated_at": row.updated_at.isoformat(),
            "snapshot": snapshot} | _head(row)


@router.get("/api/admin/answer-reports/{report_id}/export")
def report_export(report_id: str, ctx: TenantContext = Depends(require_admin)) -> JSONResponse:
    """What an office sends to the developer: the structural snapshot with the ids, without the note, shown only with the documents
    the admin sees; the export is audited."""
    with tenant_tx(ctx) as conn:
        row = _report_row(conn, report_id)
        snapshot = _visible_snapshot(conn, row.snapshot)
        if row.user_id != ctx.user_id:
            audit(conn, "answer_report.export", ctx.user_id, "answer_report", row.id, owner=str(row.user_id))
    body = {"report_id": str(row.id)} | _head(row) | {"snapshot": snapshot}
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="answer-report-{row.id}.json"',
                                       "Cache-Control": "no-store"})
