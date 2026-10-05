"""Hybrid retrieval over authorized chunks (KTD9, U9).

Three ranked lists inside the user's RLS-scoped transaction — full text ('simple' config over
app-normalized Hebrew), trigram word similarity, and pgvector cosine for the active embedding
model — fused with reciprocal rank fusion. Only current versions of non-deleted documents."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from app.db import TenantContext, tenant_tx
from app.deps import get_ctx
from app.extraction.normalize_text import base_normalize, query_tokens
from app.providers.embeddings import get_embedding_provider

router = APIRouter(prefix="/api/search", tags=["search"])

RRF_K = 60
CANDIDATES = 40
_SCOPE = (
    " FROM chunks c JOIN document_versions v ON v.id = c.version_id AND v.is_current"
    " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL"
)


def _lexical(conn: Connection, tokens: list[str]) -> list:
    if not tokens:
        return []
    parts = " || ".join(f"plainto_tsquery('simple', :t{i})" for i in range(len(tokens)))
    params = {f"t{i}": t for i, t in enumerate(tokens)}
    return list(conn.execute(
        text(f"SELECT c.id{_SCOPE} CROSS JOIN (SELECT {parts}) AS q(query)"
             " WHERE c.tsv @@ q.query ORDER BY ts_rank_cd(c.tsv, q.query) DESC LIMIT :n"),
        params | {"n": CANDIDATES},
    ).scalars())


def _trigram(conn: Connection, normalized_query: str) -> list:
    return list(conn.execute(
        text(f"SELECT c.id{_SCOPE} WHERE word_similarity(:q, c.normalized_text) > 0.35"
             " ORDER BY word_similarity(:q, c.normalized_text) DESC LIMIT :n"),
        {"q": normalized_query, "n": CANDIDATES},
    ).scalars())


def _semantic(conn: Connection, query: str) -> list:
    provider = get_embedding_provider()
    vec = provider.embed_query(query)
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    return list(conn.execute(
        text(f"SELECT c.id{_SCOPE} WHERE c.embedding_model = :m AND c.embedding IS NOT NULL"
             " ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n"),
        {"m": provider.model_id, "v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "n": CANDIDATES},
    ).scalars())


def hybrid_search(conn: Connection, query: str, limit: int = 8) -> list[dict]:
    tokens = query_tokens(query)
    ranked = [_lexical(conn, tokens), _trigram(conn, base_normalize(query)), _semantic(conn, query)]
    scores: dict = {}
    for ranking in ranked:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    top = sorted(scores, key=lambda cid: -scores[cid])[:limit]
    if not top:
        return []
    rows = {r.id: r for r in conn.execute(
        text("SELECT c.id, c.document_id, c.version_id, c.page_list, c.section, c.text, c.kind, d.title"
             f"{_SCOPE} WHERE c.id = ANY(:ids)"),
        {"ids": top},
    ).all()}
    out = []
    for cid in top:
        r = rows.get(cid)
        if r is None:
            continue
        out.append({"chunk_id": r.id, "document_id": r.document_id, "version_id": r.version_id,
                    "page_list": list(r.page_list) if r.page_list else [], "section": r.section, "text": r.text,
                    "kind": r.kind, "title": r.title, "score": round(scores[cid], 5),
                    "snippet": snippet(r.text, tokens)})
    return out


def snippet(chunk_text: str, tokens: list[str], width: int = 280) -> str:
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", chunk_text) if s.strip()]
    if not sentences:
        return chunk_text[:width]
    best = max(sentences, key=lambda s: sum(1 for t in tokens if t in base_normalize(s)))
    return best if len(best) <= width else best[:width] + "…"


@router.get("")
def search(q: str = Query(min_length=2, max_length=300), limit: int = Query(10, ge=1, le=30),
           ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        hits = hybrid_search(conn, q, limit)
    return {"results": [{"chunk_id": str(h["chunk_id"]), "document_id": str(h["document_id"]),
                         "version_id": str(h["version_id"]), "title": h["title"], "page_list": h["page_list"],
                         "section": h["section"], "snippet": h["snippet"], "score": h["score"]} for h in hits]}
