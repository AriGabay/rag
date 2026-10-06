"""Conversations and ``/api/ask``: turn reservation, the final transaction, stale marking (U9; KTD13, KTD14).

``ask`` follows KTD13:

1. A short transaction takes a per-turn advisory lock and reserves ``turn_id`` by inserting a ``pending``
   question row (unique per conversation; a ``turn_id`` is looked up in the request's conversation only).
   A turn that is already done returns its stored result; one still pending answers 409; a failed one runs
   again on the same row, and so does a pending one older than ``abandoned_after_seconds()`` (its process
   died). The reservation's ``created_at`` is its stamp: only the run holding the current stamp may
   complete or fail the row, so a run whose reservation was taken over never completes it. The same
   transaction loads the state and cache inputs (``turn.load``).
2. Interpretation and tools run outside it (``turn.interpret_turn`` / ``turn.execute_turn``); each tool
   opens its own short transaction under the user's context, and no model call runs inside one.
3. A final transaction checks ``state_version`` and completes the row, its sources, the state and the
   cache together. On a conflict the same plan is re-applied once to the fresh state (its relative years
   were resolved against the starting state, so they never apply twice); a second conflict answers 409.
   A turn that answers a clarification is not re-applied when the fresh state's pending clarification is
   another one or gone: it answers 409 instead. A failed turn marks its reservation ``failed`` so a retry
   can run.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from app.answering import turn
from app.answering.conditions import QueryConditions
from app.answering.state import ConversationState
from app.answering.turn import TurnError, TurnRequest, TurnResult, scope_hash
from app.config import get_settings
from app.db import TenantContext, current_data_version, tenant_tx
from app.deps import NOT_FOUND, get_ctx, parse_uuid
from app.providers.llm import Purpose, timeout_for

router = APIRouter(prefix="/api", tags=["chat"])
NEW_CONVERSATION_TITLE = "שיחה חדשה"
IN_PROGRESS = "השאלה עדיין בעיבוד"
CLARIFY_RELATIONS = ("answer_to_clarification", "change_clarification")
# A turn starts no model call after its deadline, but a call started just before it runs to its own timeout,
# and the SDK clients retry a timed-out call (Anthropic: up to 2 retries, OpenAI: 1), so one call can take up
# to three attempts of the slowest per-purpose timeout.
MODEL_CALL_ATTEMPTS = 3


def abandoned_after_seconds() -> float:
    """Age after which a ``pending`` reservation can no longer belong to a live run (its process crashed or
    was killed): the turn deadline plus the longest a model call started just before it may still take
    (135 s with the default settings)."""
    slowest = max(timeout_for(p) for p in Purpose)
    return get_settings().turn_deadline_seconds + MODEL_CALL_ATTEMPTS * slowest


class _Superseded(Exception):
    """This run's reservation was taken over by a re-run after it outlived ``abandoned_after_seconds``."""


class AskBody(BaseModel):
    conversation_id: str | None = None
    question: str | None = Field(default=None, max_length=1000)
    filters: dict | None = None  # explicit condition edits, applied as a delta to the conversation state
    remove: list[str] | None = None  # context chips to clear (``context[].key``)
    clarification: dict | None = None
    turn_id: str | None = None  # client-generated; the same id never applies twice


def _conversation(conn: Connection, ctx: TenantContext, conversation_id: str | UUID | None, title: str | None):
    if conversation_id:
        cid = conversation_id if isinstance(conversation_id, UUID) else parse_uuid(conversation_id)
        row = conn.execute(text("SELECT * FROM conversations WHERE id = :c AND user_id = :u"),
                           {"c": cid, "u": ctx.user_id}).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return row
    return conn.execute(
        text("INSERT INTO conversations (office_id, user_id, title) VALUES (app_office(), :u, :t) RETURNING *"),
        {"u": ctx.user_id, "t": (title or NEW_CONVERSATION_TITLE)[:80]},
    ).one()


def _turn_id(value: str | None) -> UUID:
    if not value:
        return uuid.uuid4()
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "מזהה פנייה לא תקין") from None


def _reservation(conn: Connection, ctx: TenantContext, turn_id: UUID, conversation_id: str | None):
    """The question row reserved for ``turn_id``, with ``abandoned`` (pending past the bound). A ``turn_id`` is
    unique per conversation, so it is looked up in the request's conversation only; a request without one
    (the first turn of a new conversation, or its retry) matches only the caller's turn that opened a
    conversation, never a turn of an existing one."""
    params = {"t": turn_id, "u": ctx.user_id, "age": abandoned_after_seconds()}
    select = ("SELECT q.*, (q.status = 'pending' AND q.created_at < now() - make_interval(secs => :age))"
              " AS abandoned FROM questions q")
    if conversation_id:
        return conn.execute(text(select + " WHERE q.conversation_id = :c AND q.turn_id = :t"),
                            params | {"c": parse_uuid(conversation_id)}).first()
    return conn.execute(text(
        select + " WHERE q.turn_id = :t AND q.user_id = :u AND NOT EXISTS (SELECT 1 FROM questions o"
        " WHERE o.conversation_id = q.conversation_id AND o.created_at < q.created_at)"
        " ORDER BY q.created_at DESC LIMIT 1"), params).first()


def _stored(row) -> dict:
    return {"conversation_id": str(row.conversation_id), "question_id": str(row.id), "turn_id": str(row.turn_id),
            "answer": row.answer}


def _complete(conn: Connection, ctx: TenantContext, qid: UUID, reserved_at: datetime, r: TurnResult,
              L: turn.Loaded, question: str, latency: int) -> bool:
    """The final transaction: state (optimistic on ``state_version``), the reserved row, sources, cache.
    The row is locked first and must still carry this run's reservation stamp (else ``_Superseded``)."""
    held = conn.execute(text("SELECT 1 FROM questions WHERE id = :q AND status = 'pending' AND created_at = :r"
                             " FOR UPDATE"), {"q": qid, "r": reserved_at}).first()
    if held is None:
        raise _Superseded
    conv_id = L.conversation_id
    conflict = json.dumps(r.conflict_pending, ensure_ascii=False) if r.conflict_pending else None
    if r.state is not None:
        updated = conn.execute(
            text("UPDATE conversations SET state = CAST(:s AS jsonb), state_version = state_version + 1,"
                 " pending_clarification = CAST(:p AS jsonb), updated_at = now()"
                 " WHERE id = :c AND state_version = :v"),
            {"s": r.state.model_dump_json(), "p": conflict, "c": conv_id, "v": L.state_version},
        ).rowcount
        if not updated:
            return False
    else:
        conn.execute(text("UPDATE conversations SET pending_clarification = CAST(:p AS jsonb), updated_at = now()"
                          " WHERE id = :c"), {"p": conflict, "c": conv_id})
    answer = r.answer
    conn.execute(
        text("UPDATE questions SET status = 'done', question_text = :q, intent = :i, parse_route = :r,"
             " conditions = CAST(:cond AS jsonb), answer = CAST(:a AS jsonb), answer_kind = :k, data_version = :dv,"
             " scope_hash = :sh, latency_ms = :l, plan = CAST(:plan AS jsonb), steps = CAST(:steps AS jsonb),"
             " facts_versions = CAST(:fv AS jsonb) WHERE id = :id"),
        {"id": qid, "q": question, "i": r.intent, "r": r.route, "cond": json.dumps(r.conditions),
         "a": json.dumps(answer, ensure_ascii=False, default=str), "k": answer["kind"], "dv": L.data_version,
         "sh": scope_hash(ctx), "l": latency, "plan": json.dumps(r.plan_record, ensure_ascii=False, default=str),
         "steps": json.dumps(r.steps, ensure_ascii=False, default=str), "fv": json.dumps(r.facts_versions)},
    )
    for s in answer.get("sources", []):
        conn.execute(
            text("INSERT INTO answer_sources (office_id, question_id, document_id, version_id, page_list)"
                 " VALUES (app_office(), :q, :d, :v, :p)"),
            {"q": qid, "d": s["document_id"], "v": s["version_id"], "p": s.get("page_list") or None},
        )
    if question.strip():
        conn.execute(text("UPDATE conversations SET title = :t WHERE id = :c AND title = :placeholder"),
                     {"t": question.strip()[:80], "c": conv_id, "placeholder": NEW_CONVERSATION_TITLE})
    if r.cache_key:
        conn.execute(
            text("INSERT INTO answer_cache (office_id, cache_key, payload) VALUES (app_office(), :k,"
                 " CAST(:p AS jsonb)) ON CONFLICT (office_id, cache_key) DO UPDATE SET payload = EXCLUDED.payload,"
                 " created_at = now()"),
            {"k": r.cache_key, "p": json.dumps(r.cache_payload, ensure_ascii=False, default=str)},
        )
    return True


def _fail(ctx: TenantContext, qid: UUID, reserved_at: datetime) -> None:
    with tenant_tx(ctx) as conn:
        conn.execute(text("UPDATE questions SET status = 'failed' WHERE id = :q AND status = 'pending'"
                          " AND created_at = :r"), {"q": qid, "r": reserved_at})


def _answers_clarification(it: turn.Interpreted) -> bool:
    return it.mode == "button" or (it.plan is not None and it.plan.turn_relation in CLARIFY_RELATIONS)


def _pending_identity(L: turn.Loaded) -> tuple:
    """The open clarifications a turn may answer: the state's pending one and a filter conflict."""
    p, c = L.state.pending, L.conflict_pending
    return ((p.key, p.question, p.original_question) if p is not None else None,
            (c.get("key"), c.get("question"), c.get("question_text")) if c else None)


def _run(ctx: TenantContext, req: TurnRequest, L: turn.Loaded, qid: UUID, reserved_at: datetime, started: float,
         perf: float) -> TurnResult:
    it = turn.interpret_turn(ctx, req, L)
    answers = _answers_clarification(it)  # read before execution, which may normalize the plan
    for attempt in (1, 2):
        result = turn.execute_turn(ctx, it, L, qid, started)
        answers = answers or _answers_clarification(it)
        try:
            with tenant_tx(ctx) as conn:
                done = _complete(conn, ctx, qid, reserved_at, result, L, it.question,
                                 int((time.perf_counter() - perf) * 1000))
        except _Superseded:
            raise TurnError(status.HTTP_409_CONFLICT, IN_PROGRESS) from None
        if done:
            return result
        if attempt == 2:
            break
        with tenant_tx(ctx) as conn:  # another turn changed the conversation: restart from the state load
            fresh = turn.load(conn, conn.execute(text("SELECT * FROM conversations WHERE id = :c"),
                                                 {"c": L.conversation_id}).one())
        if answers and _pending_identity(fresh) != _pending_identity(L):
            break  # the clarification this turn answered was answered, replaced or cleared meanwhile
        L = fresh
    raise TurnError(status.HTTP_409_CONFLICT, turn.STATE_CONFLICT)


@router.post("/ask")
def ask(body: AskBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    question = (body.question or "").strip()
    if not question and not body.clarification and not body.filters and not body.remove:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, turn.NEED_QUESTION)
    turn_id = _turn_id(body.turn_id)
    started, perf = time.monotonic(), time.perf_counter()
    req = TurnRequest(question or None, body.clarification, body.filters, list(body.remove or []))
    with tenant_tx(ctx) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"turn:{ctx.user_id}:{turn_id}"})
        existing = _reservation(conn, ctx, turn_id, body.conversation_id)
        if existing is not None and existing.status == "done":
            return _stored(existing)
        if existing is not None and existing.status == "pending" and not existing.abandoned:
            raise HTTPException(status.HTTP_409_CONFLICT, IN_PROGRESS)
        conv = _conversation(conn, ctx, existing.conversation_id if existing is not None else body.conversation_id,
                             question)
        L = turn.load(conn, conv)
        try:
            turn.check_request(req, L)
        except TurnError as e:
            raise HTTPException(e.status_code, e.detail) from None
        if existing is not None:  # a failed or abandoned turn runs again on its reservation
            qid = existing.id
            # only if the row is still as read: an abandoned run that completes meanwhile holds the row lock,
            # and this update then finds it done (409; the next poll returns the stored result)
            reserved_at = conn.execute(
                text("UPDATE questions SET status = 'pending', created_at = now() WHERE id = :q AND status = :s"
                     " AND created_at = :r RETURNING created_at"),
                {"q": qid, "s": existing.status, "r": existing.created_at},
            ).scalar_one_or_none()
            if reserved_at is None:
                raise HTTPException(status.HTTP_409_CONFLICT, IN_PROGRESS)
        else:
            qid, reserved_at = conn.execute(
                text("INSERT INTO questions (office_id, conversation_id, user_id, question_text, turn_id, status)"
                     " VALUES (app_office(), :c, :u, :q, :t, 'pending') RETURNING id, created_at"),
                {"c": conv.id, "u": ctx.user_id, "q": question, "t": turn_id},
            ).one()
    try:
        result = _run(ctx, req, L, qid, reserved_at, started, perf)
    except TurnError as e:
        _fail(ctx, qid, reserved_at)
        raise HTTPException(e.status_code, e.detail) from None
    except BaseException:
        _fail(ctx, qid, reserved_at)
        raise
    return {"conversation_id": str(conv.id), "question_id": str(qid), "turn_id": str(turn_id),
            "answer": result.answer}


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


def _stale(q, dv: int, sh: str, facts_now: dict[str, int]) -> bool:
    """Data or permission scope changed, or a fact of an attribute this answer used was reviewed or added
    (only that attribute's ``facts_version`` counts, KTD9)."""
    if q.answer_kind == "clarification":
        return False
    used = q.facts_versions or {}
    return q.data_version != dv or q.scope_hash != sh or any(facts_now.get(a) != v for a, v in used.items())


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        conv = _conversation(conn, ctx, conversation_id, None)
        dv, sh = current_data_version(conn), scope_hash(ctx)
        rows = conn.execute(text("SELECT * FROM questions WHERE conversation_id = :c AND status = 'done'"
                                 " ORDER BY created_at"), {"c": conv.id}).all()
        visible_counts = dict(conn.execute(
            text("SELECT s.question_id, count(*) FROM answer_sources s JOIN documents d ON d.id = s.document_id"
                 " WHERE s.question_id = ANY(:ids) AND d.deleted_at IS NULL GROUP BY s.question_id"),
            {"ids": [q.id for q in rows]},
        ).all()) if rows else {}
        facts_now = {str(r.id): r.facts_version for r in conn.execute(
            text("SELECT id, facts_version FROM attribute_definitions")).all()} if any(
            q.facts_versions for q in rows) else {}
        messages = []
        for q in rows:
            answer = q.answer or {}
            hidden = visible_counts.get(q.id, 0) < len(answer.get("sources", []))
            messages.append({
                "question_id": str(q.id), "question": q.question_text, "answer": None if hidden else answer,
                "stale": _stale(q, dv, sh, facts_now), "hidden": hidden, "created_at": q.created_at.isoformat(),
            })
    try:
        state = ConversationState.model_validate(dict(conv.state or {}))
    except ValueError:
        state = ConversationState()
    legacy = conv.pending_clarification or {}
    if legacy.get("key") == turn.FILTER_CONFLICT:
        pending = {k: legacy[k] for k in ("key", "question", "options")}
    elif state.pending is not None:
        pending = {"key": state.pending.key, "question": state.pending.question,
                   "options": [o.model_dump() for o in state.pending.options]}
    else:
        pending = None
    c = state.conditions
    context = {
        "attribute": state.attribute.description if state.attribute else None, "metric": state.metric,
        "city": c.city, "neighborhood": c.neighborhood,
        "years": {"from": c.year_from, "to": c.year_to} if c.year_from is not None else None,
        "data_kind": c.data_kind, "date_field": c.date_field,
        "chips": turn.context_items(state),
    }
    return {"id": str(conv.id), "title": conv.title,
            "confirmed_conditions": QueryConditions.model_validate(c.model_dump()).describe(),
            "pending_clarification": pending, "context": context, "messages": messages}
