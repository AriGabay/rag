"""Shared pieces of the answering tools (U8, U9; KTD11): the provider of the office's mode, evidence built
from authorized search hits, and the ``provider_usage`` log of every model call with its status.

The search, locate and compare tools themselves run in ``turn`` (and ``compare``)."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, text

from app.answering.compose import Usage
from app.platform.documents import source_file_url
from app.providers.llm import (
    CallStatus,
    LLMProvider,
    MockLLM,
    get_selected_provider,
    selected_provider_configured,
)
from app.providers.status import Mode, ProviderState, office_provider_state

EVIDENCE_LIMIT = 6  # admitted passages per content answer


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


def log_usages(conn: Connection, provider: LLMProvider, usage: Iterable[Usage]) -> None:
    """The ``provider_usage`` rows of a composed answer's calls (answer and judge)."""
    for u in usage:
        log_usage(conn, provider, u.purpose, u.result, u.ok, u.status)


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
