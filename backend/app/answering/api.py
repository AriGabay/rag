"""Conversations, /api/ask, exact answer cache, stale-answer marking (U8, U11, R27, R28, R39)."""

from __future__ import annotations

import hashlib
import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from app.answering.conditions import QueryConditions
from app.answering.coverage import coverage
from app.answering.service import AskInput, Outcome, run_question
from app.answering.templates import TEMPLATE_VERSION
from app.db import TenantContext, current_data_version, tenant_tx
from app.deps import NOT_FOUND, get_ctx, parse_uuid
from app.providers.embeddings import get_embedding_provider

router = APIRouter(prefix="/api", tags=["chat"])
CACHEABLE = ("numeric", "content", "combined", "abstain")


class AskBody(BaseModel):
    conversation_id: str | None = None
    question: str | None = Field(default=None, max_length=1000)
    filters: dict | None = None
    clarification: dict | None = None


def scope_hash(ctx: TenantContext) -> str:
    scope = "admin" if ctx.is_admin else "employee:" + ",".join(sorted(str(g) for g in ctx.group_ids))
    return hashlib.sha256(scope.encode()).hexdigest()[:32]


def _settings_version(conn: Connection) -> int:
    return conn.execute(text("SELECT settings_version FROM office_settings")).scalar_one()


def cache_key(conn: Connection, ctx: TenantContext, outcome_conditions: QueryConditions | None, question: str,
              intent: str | None) -> str:
    """Office, permission scope, intent, normalized conditions, data version, calculation settings (R28)."""
    payload = {
        "office": str(ctx.office_id), "scope": scope_hash(ctx), "intent": intent,
        "conditions": outcome_conditions.normalized_key() if outcome_conditions else None,
        "question": None if intent == "calculation" else " ".join(question.split()),
        "data_version": current_data_version(conn), "settings_version": _settings_version(conn),
        "embedding": get_embedding_provider().model_id, "template": TEMPLATE_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sources_still_authorized(conn: Connection, answer: dict) -> bool:
    for s in answer.get("sources", []):
        ok = conn.execute(
            text("SELECT 1 FROM document_versions v JOIN documents d ON d.id = v.document_id"
                 " WHERE v.id = :v AND d.id = :d AND d.deleted_at IS NULL AND v.is_current"),
            {"v": s["version_id"], "d": s["document_id"]},
        ).first()
        if ok is None:
            return False
    return True


def _conversation(conn: Connection, ctx: TenantContext, conversation_id: str | None, title: str | None):
    if conversation_id:
        row = conn.execute(text("SELECT * FROM conversations WHERE id = :c AND user_id = :u"),
                           {"c": parse_uuid(conversation_id), "u": ctx.user_id}).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return row
    return conn.execute(
        text("INSERT INTO conversations (office_id, user_id, title) VALUES (app_office(), :u, :t) RETURNING *"),
        {"u": ctx.user_id, "t": (title or "שיחה חדשה")[:80]},
    ).one()


def _save(conn: Connection, ctx: TenantContext, conv_id: UUID, question: str, outcome: Outcome, latency: int) -> UUID:
    answer = outcome.answer
    qid = conn.execute(
        text("INSERT INTO questions (office_id, conversation_id, user_id, question_text, intent, parse_route,"
             " conditions, answer, answer_kind, data_version, scope_hash, latency_ms) VALUES (app_office(), :c, :u,"
             " :q, :i, :r, CAST(:cond AS jsonb), CAST(:a AS jsonb), :k, :dv, :sh, :l) RETURNING id"),
        {"c": conv_id, "u": ctx.user_id, "q": question, "i": outcome.intent, "r": outcome.parse_route,
         "cond": json.dumps(outcome.conditions.model_dump() if outcome.conditions else None),
         "a": json.dumps(answer, ensure_ascii=False, default=str), "k": answer["kind"],
         "dv": current_data_version(conn), "sh": scope_hash(ctx), "l": latency},
    ).scalar_one()
    for s in answer.get("sources", []):
        conn.execute(
            text("INSERT INTO answer_sources (office_id, question_id, document_id, version_id, page_list)"
                 " VALUES (app_office(), :q, :d, :v, :p)"),
            {"q": qid, "d": s["document_id"], "v": s["version_id"], "p": s.get("page_list") or None},
        )
    confirmed = outcome.conditions.model_dump() if (
        outcome.conditions and answer["kind"] in ("numeric", "combined", "abstain") and outcome.conditions.data_kind
    ) else None
    if question.strip():
        conn.execute(text("UPDATE conversations SET title = :t WHERE id = :c AND title = 'שיחה חדשה'"),
                     {"t": question.strip()[:80], "c": conv_id})
    conn.execute(
        text("UPDATE conversations SET pending_clarification = CAST(:p AS jsonb),"
             " confirmed_conditions = COALESCE(CAST(:cc AS jsonb), confirmed_conditions), updated_at = now()"
             " WHERE id = :c"),
        {"p": json.dumps(outcome.pending, ensure_ascii=False) if outcome.pending else None,
         "cc": json.dumps(confirmed) if confirmed else None, "c": conv_id},
    )
    return qid


@router.post("/ask")
def ask(body: AskBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    if not (body.question and body.question.strip()) and not body.clarification:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "יש לכתוב שאלה")
    started = time.perf_counter()
    with tenant_tx(ctx) as conn:
        conv = _conversation(conn, ctx, body.conversation_id, body.question)
        previous = QueryConditions.model_validate(conv.confirmed_conditions) if conv.confirmed_conditions else None
        pending = conv.pending_clarification if body.clarification else None
        def lookup(conds: QueryConditions, question: str):
            cached = conn.execute(text("SELECT payload FROM answer_cache WHERE cache_key = :k"),
                                  {"k": cache_key(conn, ctx, conds, question, conds.intent)}).scalar()
            if cached and cached.get("kind") != "clarification" and _sources_still_authorized(conn, cached):
                return cached | {"coverage": coverage(conn, conds), "cached": True}
            return None

        outcome = run_question(conn, ctx, AskInput(body.question, body.filters, body.clarification), previous,
                               pending, cache=lookup)
        question_text = body.question or (pending or {}).get("original_question", "")
        has_mixed_clarification = outcome.answer["kind"] == "clarification"
        if (outcome.answer["kind"] in CACHEABLE and outcome.conditions is not None and not outcome.answer.get("cached")
                and not has_mixed_clarification):
            key = cache_key(conn, ctx, outcome.conditions, question_text, outcome.conditions.intent)
            conn.execute(
                text("INSERT INTO answer_cache (office_id, cache_key, payload) VALUES (app_office(), :k,"
                     " CAST(:p AS jsonb)) ON CONFLICT (office_id, cache_key) DO UPDATE SET payload = EXCLUDED.payload,"
                     " created_at = now()"),
                {"k": key, "p": json.dumps(outcome.answer, ensure_ascii=False, default=str)},
            )
        latency = int((time.perf_counter() - started) * 1000)
        qid = _save(conn, ctx, conv.id, question_text, outcome, latency)
    return {"conversation_id": str(conv.id), "question_id": str(qid), "answer": outcome.answer}


@router.get("/conversations")
def conversations(ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        rows = conn.execute(text("SELECT id, title, updated_at FROM conversations WHERE user_id = :u"
                                 " ORDER BY updated_at DESC LIMIT 50"), {"u": ctx.user_id}).all()
    return {"conversations": [{"id": str(r.id), "title": r.title, "updated_at": r.updated_at.isoformat()} for r in rows]}


@router.post("/conversations")
def new_conversation(ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        conv = _conversation(conn, ctx, None, None)
    return {"id": str(conv.id)}


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        conv = _conversation(conn, ctx, conversation_id, None)
        dv, sh = current_data_version(conn), scope_hash(ctx)
        rows = conn.execute(text("SELECT * FROM questions WHERE conversation_id = :c ORDER BY created_at"),
                            {"c": conv.id}).all()
        messages = []
        for q in rows:
            answer = q.answer or {}
            expected = len(answer.get("sources", []))
            visible = conn.execute(
                text("SELECT count(*) FROM answer_sources s JOIN documents d ON d.id = s.document_id"
                     " WHERE s.question_id = :q AND d.deleted_at IS NULL"), {"q": q.id}).scalar_one()
            hidden = visible < expected
            messages.append({
                "question_id": str(q.id), "question": q.question_text,
                "answer": None if hidden else answer,
                "stale": (q.data_version != dv or q.scope_hash != sh) and q.answer_kind != "clarification",
                "hidden": hidden, "created_at": q.created_at.isoformat(),
            })
        confirmed = QueryConditions.model_validate(conv.confirmed_conditions).describe() if conv.confirmed_conditions else []
        pending = conv.pending_clarification
    return {"id": str(conv.id), "title": conv.title, "confirmed_conditions": confirmed,
            "pending_clarification": {k: pending[k] for k in ("key", "question", "options")} if pending else None,
            "messages": messages}
