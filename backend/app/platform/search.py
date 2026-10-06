"""Hybrid retrieval over authorized chunks (KTD9, U9; generalized in KTD10).

Three ranked lists per query inside the user's RLS-scoped transaction — full text ('simple' config
over app-normalized Hebrew), trigram word similarity, and pgvector cosine for the active embedding
model — fused with reciprocal rank fusion over all queries (the question and its rewrites).

Scope: current versions of non-deleted documents, optionally narrowed to a document set or explicit
version ids. Older versions appear only when the caller names their ids and asks for them
(``include_noncurrent``, version comparison); RLS still decides what the user can see. Metadata
filters (city, neighborhood, year range) keep only versions whose stated value matches; versions that
state nothing are reported as unknown and never searched.

Lexical support (evidence admission) counts only topic terms: place names, years and stopwords are
excluded, so a passage that only shares "רמת גן" with the question is not evidence."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from app.answering.metadata import FilterReport, MetadataFilters, apply_filters, version_metadata
from app.answering.places import KNOWN_CITIES
from app.db import TenantContext, tenant_tx
from app.deps import get_ctx
from app.extraction.normalize_text import base_normalize, query_tokens
from app.providers.embeddings import get_embedding_provider, to_pgvector

router = APIRouter(prefix="/api/search", tags=["search"])

RRF_K = 60
# Exact terms (prices, block/parcel, names) matter most in appraisal documents: lexical counts double.
RRF_WEIGHTS = (2.0, 1.0, 1.0)  # lexical, trigram, semantic
LEXICAL_GUARANTEE = 2
CANDIDATES = 40
TRIGRAM_THRESHOLD = 0.35
MAX_QUERIES = 3
_YEAR = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True)
class SearchScope:
    version_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    include_noncurrent: bool = False  # honored only together with explicit version_ids


@dataclass
class SearchOutcome:
    hits: list[dict]
    topic_terms: list[str]
    filter_report: FilterReport | None = None


@dataclass
class _Sql:
    """FROM/JOIN clause and WHERE conditions of one search scope."""

    where: str = "v.is_current"
    params: dict = field(default_factory=dict)
    joins: str = (" FROM chunks c JOIN document_versions v ON v.id = c.version_id"
                  " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL")


def _scope_sql(scope: SearchScope | None, allowed_versions: list[UUID] | None = None) -> _Sql:
    sql = _Sql()
    conds: list[str] = []
    if scope and scope.version_ids:
        sql.params["scope_v"] = list(scope.version_ids)
        conds.append("c.version_id = ANY(:scope_v)")
        conds.append("(v.is_current OR v.id = ANY(:scope_v))" if scope.include_noncurrent else "v.is_current")
    else:
        conds.append("v.is_current")
    if scope and scope.document_ids:
        sql.params["scope_d"] = list(scope.document_ids)
        conds.append("c.document_id = ANY(:scope_d)")
    if allowed_versions is not None:
        sql.params["scope_allowed"] = allowed_versions
        conds.append("c.version_id = ANY(:scope_allowed)")
    sql.where = " AND ".join(conds)
    return sql


def _versions_in_scope(conn: Connection, scope: SearchScope | None) -> list[UUID]:
    conds, params = ["v.is_current"], {}
    if scope and scope.version_ids:
        params["v"] = list(scope.version_ids)
        conds = ["v.id = ANY(:v)", "(v.is_current OR v.id = ANY(:v))" if scope.include_noncurrent else "v.is_current"]
    if scope and scope.document_ids:
        params["d"] = list(scope.document_ids)
        conds.append("v.document_id = ANY(:d)")
    return list(conn.execute(
        text("SELECT v.id FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
             " WHERE " + " AND ".join(conds) + " ORDER BY v.created_at, v.id"),
        params,
    ).scalars())


def _lexical(conn: Connection, tokens: list[str], scope: _Sql | None = None) -> list:
    """OR over query terms, ranked by IDF-weighted matched terms (rare terms such as a price or a
    block/parcel outweigh generic words), then ts_rank_cd within ties. IDF comes from all current
    visible chunks, so a narrow scope does not flatten the weights."""
    if not tokens:
        return []
    scope = scope or _Sql()
    base = _Sql()
    params = {f"t{i}": t for i, t in enumerate(tokens)}
    match = [f"(c.tsv @@ plainto_tsquery('simple', :t{i}))" for i in range(len(tokens))]
    stats = conn.execute(
        text("SELECT count(*) AS n, " + ", ".join(f"count(*) FILTER (WHERE {m}) AS d{i}" for i, m in enumerate(match))
             + base.joins + " WHERE " + base.where),
        params,
    ).one()
    n = max(stats.n, 1)
    weights = {f"w{i}": math.log((n + 1) / (getattr(stats, f"d{i}") + 0.5)) for i in range(len(tokens))}
    parts = " || ".join(f"plainto_tsquery('simple', :t{i})" for i in range(len(tokens)))
    score = " + ".join(f"CAST(:w{i} AS float8) * {m}::int" for i, m in enumerate(match))
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} CROSS JOIN (SELECT {parts}) AS q(query)"
             f" WHERE {scope.where} AND c.tsv @@ q.query ORDER BY ({score}) DESC, ts_rank_cd(c.tsv, q.query) DESC"
             " LIMIT :n"),
        params | weights | scope.params | {"n": CANDIDATES},
    ).scalars())


def _trigram(conn: Connection, normalized_query: str, scope: _Sql) -> list:
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} WHERE {scope.where} AND word_similarity(:q, c.normalized_text) > :th"
             " ORDER BY word_similarity(:q, c.normalized_text) DESC LIMIT :n"),
        scope.params | {"q": normalized_query, "th": TRIGRAM_THRESHOLD, "n": CANDIDATES},
    ).scalars())


def _semantic(conn: Connection, query: str, scope: _Sql) -> list:
    provider = get_embedding_provider()
    vec = provider.embed_query(query)
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} WHERE {scope.where} AND c.embedding_model = :m AND c.embedding IS NOT NULL"
             " ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n"),
        scope.params | {"m": provider.model_id, "v": to_pgvector(vec), "n": CANDIDATES},
    ).scalars())


def _alpha(tokens: Iterable[str]) -> str:
    return " ".join(t for t in tokens if any(ch.isalpha() for ch in t))


def _strip_places(normalized: str, places: Iterable[str]) -> str:
    """Remove place phrases (with an optional one/two-letter Hebrew prefix) from normalized text."""
    for place in places:
        words = base_normalize(place).replace("-", " ").split()
        if not words:
            continue
        pattern = r"[\s\-]+".join(re.escape(w) for w in words)
        normalized = re.sub(rf"(?<![\w״׳])[והבלמשכ]{{0,2}}-?{pattern}(?![\w״׳])", " ", normalized)
    return normalized


def topic_tokens(query: str, place_terms: Iterable[str] = ()) -> list[str]:
    """Query tokens that carry the topic: no stopwords, years, or place names (the caller's place terms
    and the city gazetteer). A question that names only a place keeps the place as its topic."""
    norm = base_normalize(query)
    tokens = [t for t in query_tokens(_strip_places(norm, [*place_terms, *KNOWN_CITIES]))
              if not _YEAR.fullmatch(t)]
    if tokens:
        return tokens
    return [t for t in query_tokens(query) if not _YEAR.fullmatch(t)] or query_tokens(query)


def _supported(conn: Connection, ids: list, topic: list[str]) -> set:
    """Chunks among ``ids`` that match a topic term exactly or by trigram similarity."""
    if not ids or not topic:
        return set()
    params = {f"t{i}": t for i, t in enumerate(topic)} | {"ids": ids, "q": _alpha(topic), "th": TRIGRAM_THRESHOLD}
    parts = " || ".join(f"plainto_tsquery('simple', :t{i})" for i in range(len(topic)))
    fuzzy = " OR word_similarity(:q, c.normalized_text) > :th" if params["q"] else ""
    return set(conn.execute(
        text(f"SELECT c.id FROM chunks c WHERE c.id = ANY(:ids) AND (c.tsv @@ ({parts}){fuzzy})"), params,
    ).scalars())


def search_evidence(conn: Connection, queries: Sequence[str], limit: int = 8, *, scope: SearchScope | None = None,
                    filters: MetadataFilters | None = None, place_terms: Iterable[str] = ()) -> SearchOutcome:
    """Fused search over up to ``MAX_QUERIES`` queries (the first is the primary question)."""
    queries = [q for q in queries if q and q.strip()][:MAX_QUERIES]
    place_terms = list(place_terms)
    topic: list[str] = []
    for q in queries:
        topic.extend(t for t in topic_tokens(q, place_terms) if t not in topic)
    report = None
    allowed = None
    if filters is not None and filters.active:
        report = apply_filters(version_metadata(conn, _versions_in_scope(conn, scope)), filters)
        allowed = report.matched
        if not allowed:
            return SearchOutcome([], topic, report)
    sql = _scope_sql(scope, allowed)
    scores: dict = {}
    all_tokens: list[str] = []
    guaranteed: list = []
    for qi, q in enumerate(queries):
        tokens = query_tokens(q)
        all_tokens.extend(t for t in tokens if t not in all_tokens)
        lexical = _lexical(conn, tokens, sql)
        fuzzy = _trigram(conn, _alpha(tokens), sql) if tokens else []
        if qi == 0:
            guaranteed = lexical[:LEXICAL_GUARANTEE]
        for weight, ranking in zip(RRF_WEIGHTS, [lexical, fuzzy, _semantic(conn, q, sql)], strict=True):
            for rank, chunk_id in enumerate(ranking):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank + 1)
    top = sorted(scores, key=lambda cid: -scores[cid])[:limit]
    # The best exact-term matches of the question always get a slot: semantic neighbours must not crowd them out.
    for pos, chunk_id in enumerate(guaranteed):
        if chunk_id not in top:
            top.insert(min(pos, len(top)), chunk_id)
    top = top[:limit]
    if not top:
        return SearchOutcome([], topic, report)
    supported = _supported(conn, top, topic)
    rows = {r.id: r for r in conn.execute(
        text("SELECT c.id, c.document_id, c.version_id, c.page_list, c.section, c.text, c.kind, c.table_index,"
             f" c.row_index, d.title{sql.joins} WHERE {sql.where} AND c.id = ANY(:ids)"),
        sql.params | {"ids": top},
    ).all()}
    out = []
    for cid in top:
        r = rows.get(cid)
        if r is None:
            continue
        hit = {"chunk_id": r.id, "document_id": r.document_id, "version_id": r.version_id,
               "page_list": list(r.page_list) if r.page_list else [], "section": r.section, "text": r.text,
               "kind": r.kind, "title": r.title, "score": round(scores[cid], 5),
               "lexical_support": cid in supported, "snippet": snippet(r.text, all_tokens)}
        if r.table_index is not None:
            hit["table_index"] = r.table_index
        if r.row_index is not None:
            hit["row_index"] = r.row_index
        out.append(hit)
    return SearchOutcome(out, topic, report)


def hybrid_search(conn: Connection, query: str, limit: int = 8, *, scope: SearchScope | None = None,
                  filters: MetadataFilters | None = None, extra_queries: Sequence[str] = (),
                  place_terms: Iterable[str] = ()) -> list[dict]:
    """Hits only; ``search_evidence`` also returns the topic terms and the filter report."""
    return search_evidence(conn, [query, *extra_queries], limit, scope=scope, filters=filters,
                           place_terms=place_terms).hits


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
