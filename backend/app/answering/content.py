"""The search-and-answer tool (U10, U8; KTD11): content and combined answers composed from verified claims.

Evidence comes from authorized search only; the answer is composed by ``compose`` (claims, two verification
layers, extractive fallback) through the provider of the office's mode, and every model call is logged
to ``provider_usage`` with its status."""

from __future__ import annotations

import logging

from sqlalchemy import Connection, text

from app.answering.compose import (
    ABSTENTION_TEXT,
    answer_fields,
    compose_answer,
    computed_from_numeric,
    no_evidence_kind,
    source_json,
)
from app.answering.coverage import coverage
from app.db import TenantContext
from app.platform.documents import source_file_url
from app.platform.search import hybrid_search
from app.providers.llm import (
    CallStatus,
    LLMProvider,
    MockLLM,
    get_selected_provider,
    selected_provider_configured,
)
from app.providers.status import Mode, ProviderState, office_provider_state

logger = logging.getLogger(__name__)
EVIDENCE_LIMIT = 6


def select_provider(conn: Connection) -> tuple[LLMProvider | None, ProviderState]:
    """The provider for this office's mode (``providers.status``): the selected cloud provider in ``cloud``
    mode, the labeled demo mock in ``demo`` mode, and none (sources only) in ``limited`` and ``error``."""
    state = office_provider_state(conn, key_present=selected_provider_configured())
    if state.mode == Mode.CLOUD:
        return get_selected_provider(), state
    if state.mode == Mode.DEMO:
        return MockLLM(), state
    return None, state


def log_usage(conn: Connection, provider: LLMProvider, purpose: str, result, ok: bool,
              status: CallStatus | str | None = None) -> None:
    """One ``provider_usage`` row; ``status`` defaults to the result's own call status when it has one."""
    status = status if status is not None else getattr(result, "status", None)
    conn.execute(
        text("INSERT INTO provider_usage (office_id, provider, model, purpose, input_tokens, output_tokens,"
             " latency_ms, ok, status) VALUES (app_office(), :p, :m, :pu, :i, :o, :l, :ok, :s)"),
        {"p": provider.name, "m": provider.model, "pu": str(purpose), "i": getattr(result, "input_tokens", None),
         "o": getattr(result, "output_tokens", None), "l": getattr(result, "latency_ms", None), "ok": ok,
         "s": str(status) if status is not None else None},
    )


def evidence_from_hits(hits: list[dict], start: int) -> list[dict]:
    out = []
    for i, h in enumerate(hits, start=start):
        page = h["page_list"][0] if h["page_list"] else None
        out.append({
            "evidence_id": f"E{i}", "document_id": str(h["document_id"]), "version_id": str(h["version_id"]),
            "title": h["title"], "page_list": h["page_list"], "section": h["section"], "row": None,
            "snippet": h["snippet"], "text": h["text"], "chunk_id": str(h["chunk_id"]),
            "url": source_file_url(h["document_id"], h["version_id"], page),
        })
    return out


def answer_content(conn: Connection, ctx: TenantContext, question: str, c, route: str, numeric=None):
    from app.answering.service import Outcome

    query = question
    if c is not None and (c.neighborhood or c.city) and numeric is not None:
        query = f"{question} {c.neighborhood or ''} {c.city or ''}"
    # Evidence needs at least one lexical or fuzzy term match; a purely semantic neighbour is not a basis.
    hits = [h for h in hybrid_search(conn, query, EVIDENCE_LIMIT * 2) if h["lexical_support"]][:EVIDENCE_LIMIT]
    base = numeric.answer if numeric is not None else None
    base_sources = base["sources"] if base else []
    evidence = evidence_from_hits(hits, len(base_sources) + 1)
    cov = base["coverage"] if base else coverage(conn, None)
    limitations = list(base["limitations"]) if base else []
    provider, state = select_provider(conn)

    if not evidence:
        if base:
            base["kind"] = "combined"
            base["limitations"] = limitations + ["לא נמצאו קטעי הסבר רלוונטיים במסמכים המורשים."]
            return numeric
        kind = no_evidence_kind(ctx)
        answer = {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov,
                  "text": ABSTENTION_TEXT[kind], "numeric": None,
                  "limitations": ["החיפוש בוצע רק במסמכי המשרד שעובדו ושאתם מורשים לראות."],
                  "claims": [], "abstention_kind": kind, "dropped_claims": 0, "mode": state.mode.value}
        return Outcome(answer, c, c.intent if c else "explanation", route)

    computed = computed_from_numeric(base["numeric"], [s["evidence_id"] for s in base_sources]) if base else []
    comp = compose_answer(provider, question, evidence, computed=computed,
                          cited_extra={s["evidence_id"]: s.get("snippet") or "" for s in base_sources})
    for u in comp.usage:
        log_usage(conn, provider, u.purpose, u.result, u.ok, u.status)
    limitations += comp.limitations
    if mode_note := state.limitation():
        limitations.append(mode_note)

    sources = base_sources + source_json(evidence)
    fields = answer_fields(comp, state.mode.value)
    if base:
        answer = dict(base)
        answer.update({"kind": "combined", "text": base["text"] + "\n\n" + comp.text, "provider": comp.provider
                       if comp.provider != "extractive" else "template", "demo": comp.demo, "sources": sources,
                       "limitations": limitations, **fields})
    else:
        answer = {"kind": "content", "text": comp.text, "provider": comp.provider, "demo": comp.demo,
                  "sources": sources, "coverage": cov, "limitations": limitations, "numeric": None, **fields}
    rows = (numeric.source_rows if numeric else []) + [
        {"document_id": e["document_id"], "version_id": e["version_id"], "chunk_id": e["chunk_id"],
         "page_list": e["page_list"]} for e in evidence]
    return Outcome(answer, c, c.intent if c else "explanation", route, source_rows=rows,
                   cacheable=comp.cacheable and state.mode != Mode.ERROR)
