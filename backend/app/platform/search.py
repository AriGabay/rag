"""Hybrid retrieval over authorized chunks (KTD9, U9; generalized in KTD10).

Three ranked lists per query inside the user's RLS-scoped transaction — full text ('simple' config
over app-normalized Hebrew), trigram word similarity, and pgvector cosine for the active embedding
model — fused with reciprocal rank fusion over all queries (the question and its rewrites).

Scope: current versions of non-deleted documents, optionally narrowed to a document set or explicit
version ids. Older versions appear only when the caller names their ids and asks for them
(``include_noncurrent``, version comparison); RLS still decides what the user can see. Metadata
filters (city, neighborhood, year range) keep only versions whose stated value matches; versions that
state nothing are reported as unknown and never searched.

A scope may also name the pool it reads (``source_kinds``: round 10 U8, KTD12) and the versions it leaves out
(``exclude_versions``: the later copies of one text): both apply to every stage — the version resolution that feeds the
trigram stage, the lexical weights and the semantic stage — and to the document naming a search does by title.

Lexical support (evidence admission) counts only topic terms: place names, years and stopwords are
excluded, so a passage that only shares "רמת גן" with the question is not evidence.

A small scope (at most ``SMALL_SCOPE_DOCUMENTS`` documents, e.g. the documents an address resolved to)
ranks by topic terms only (the place terms name every passage in it) and returns the best supported
passage of every page and the best few of every version first, so a page-2 passage is never cut by a
page 1 full of address words.

``locate_documents`` ranks documents rather than passages for "which documents mention X" questions:
every distinctive topic term of the question, in one passage, beats some of them; a negation in the
question ("אין", "ללא") is a term the passage must carry governing the topic ("ללא X", not "יש X ואין Y");
words present in every document weigh almost nothing. Every fully supported document is kept; partial
matches are kept only close to the best, and never when a question word occurs in no document in scope
unless they name a word that tells documents apart (``LocateOutcome.absent_terms`` lists such words)."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from app.answering.metadata import FilterReport, MetadataFilters, filter_versions
from app.answering.places import KNOWN_CITIES
from app.db import TenantContext, tenant_tx
from app.deps import get_ctx
from app.extraction.normalize_text import (
    NEGATION_WORDS,
    STOPWORDS,
    base_normalize,
    inflection_variants,
    is_negation,
    prefix_variants,
    query_tokens,
)
from app.platform.source_kinds import APPRAISALS_SQL
from app.providers.embeddings import get_embedding_provider, to_pgvector

router = APIRouter(prefix="/api/search", tags=["search"])

RRF_K = 60
# Exact terms (prices, block/parcel, names) matter most in appraisal documents: lexical counts double.
RRF_WEIGHTS = (2.0, 1.0, 1.0)  # lexical, trigram, semantic
LEXICAL_GUARANTEE = 2
CANDIDATES = 40
TRIGRAM_THRESHOLD = 0.35
MAX_QUERIES = 3
SMALL_SCOPE_DOCUMENTS = 3
SMALL_SCOPE_CANDIDATES = 200
PER_VERSION_GUARANTEE = 2
UNIT_TRIGRAM_CANDIDATES = 1000
_YEAR = re.compile(r"(?:19|20)\d{2}")


@dataclass(frozen=True)
class SearchScope:
    version_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    include_noncurrent: bool = False  # honored only together with explicit version_ids
    # a unit scope (round 9 U9, KTD7): only chunks that begin inside the blocks (first, last) of the one version in
    # ``version_ids`` — an appraisal unit of a file holding several. A chunk is a section of a chapter, so the one that
    # closes a unit may run on into the next unit's title block; it belongs to the unit it begins in (the next unit's
    # own scope never holds the previous unit's closing chapter, whose figures are not its)
    blocks: tuple[int, int] | None = None
    # the pool (round 10 U8, KTD12): only documents of these source kinds (``app.platform.source_kinds``); empty: every
    # kind, as the legacy answering path and the search screen search. The chat tools always name one pool
    source_kinds: tuple[str, ...] = ()
    # versions left out of the scope: the second and later copies of one text (``professional.catalog``, KTD11), so that
    # identical text is counted once. Not applied to a scope that names its versions or documents
    exclude_versions: tuple[UUID, ...] = ()


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
    # the visible versions the scope resolves to (under row security, with the metadata filters applied), once the
    # search has resolved them: the trigram stage passes them to ``trigram_candidates``
    version_ids: list[UUID] | None = None
    # the pool's source kinds: the lexical stage weighs a term by its frequency in the pool, not in the whole office
    source_kinds: tuple[str, ...] = ()
    joins: str = (" FROM chunks c JOIN document_versions v ON v.id = c.version_id"
                  " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL")


def _scope_conditions(scope: SearchScope | None, version_col: str, document_col: str) -> tuple[list[str], dict]:
    """WHERE conditions and parameters of ``scope`` for a query that joins ``document_versions v``: current
    versions only, unless the caller names version ids and asks for older ones (``include_noncurrent``)."""
    conds: list[str] = []
    params: dict = {}
    if scope and scope.version_ids:
        params["scope_v"] = list(scope.version_ids)
        conds.append(f"{version_col} = ANY(:scope_v)")
        conds.append("(v.is_current OR v.id = ANY(:scope_v))" if scope.include_noncurrent else "v.is_current")
    else:
        conds.append("v.is_current")
    if scope and scope.document_ids:
        params["scope_d"] = list(scope.document_ids)
        conds.append(f"{document_col} = ANY(:scope_d)")
    if scope and scope.source_kinds:
        params["scope_k"] = list(scope.source_kinds)
        conds.append("d.source_kind = ANY(:scope_k)")
    if scope and scope.exclude_versions:
        params["scope_x"] = list(scope.exclude_versions)
        conds.append(f"NOT ({version_col} = ANY(:scope_x))")
    return conds, params


def _scope_sql(scope: SearchScope | None, allowed_versions: list[UUID] | None = None) -> _Sql:
    conds, params = _scope_conditions(scope, "c.version_id", "c.document_id")
    if scope and scope.blocks:
        params |= {"scope_blk_lo": scope.blocks[0], "scope_blk_hi": scope.blocks[1]}
        conds.append("c.block_start BETWEEN :scope_blk_lo AND :scope_blk_hi")
    if allowed_versions is not None:
        params["scope_allowed"] = allowed_versions
        conds.append("c.version_id = ANY(:scope_allowed)")
    return _Sql(where=" AND ".join(conds), params=params, source_kinds=scope.source_kinds if scope else ())


def _scoped_versions(conn: Connection, scope: SearchScope | None) -> list[tuple[UUID, UUID]]:
    """(version id, document id) of the visible versions in ``scope``."""
    conds, params = _scope_conditions(scope, "v.id", "v.document_id")
    return [(r.id, r.document_id) for r in conn.execute(
        text("SELECT v.id, v.document_id FROM document_versions v"
             " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
             " WHERE " + " AND ".join(conds) + " ORDER BY v.created_at, v.id"),
        params,
    ).all()]


def _lexical(conn: Connection, tokens: list[str], scope: _Sql | None = None, n: int = CANDIDATES) -> list:
    """OR over query terms, ranked by IDF-weighted matched terms (rare terms such as a price or a
    block/parcel outweigh generic words), then ts_rank_cd within ties. IDF comes from all current
    visible chunks of the scope's pool (every kind for an open scope), so a narrow scope does not flatten the weights
    and a term common in appraisals weighs nothing less in a pool of standards."""
    if not tokens:
        return []
    scope = scope or _Sql()
    base = _Sql(source_kinds=scope.source_kinds)
    if base.source_kinds:
        base = replace(base, where="v.is_current AND d.source_kind = ANY(:idf_k)",
                       params={"idf_k": list(base.source_kinds)})
    params = {f"t{i}": t for i, t in enumerate(tokens)}
    match = [f"(c.tsv @@ plainto_tsquery('simple', :t{i}))" for i in range(len(tokens))]
    stats = conn.execute(
        text("SELECT count(*) AS n, " + ", ".join(f"count(*) FILTER (WHERE {m}) AS d{i}" for i, m in enumerate(match))
             + base.joins + " WHERE " + base.where),
        params | base.params,
    ).one()
    n = max(stats.n, 1)
    weights = {f"w{i}": math.log((n + 1) / (getattr(stats, f"d{i}") + 0.5)) for i in range(len(tokens))}
    parts = " || ".join(f"plainto_tsquery('simple', :t{i})" for i in range(len(tokens)))
    score = " + ".join(f"CAST(:w{i} AS float8) * {m}::int" for i, m in enumerate(match))
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} CROSS JOIN (SELECT {parts}) AS q(query)"
             f" WHERE {scope.where} AND c.tsv @@ q.query ORDER BY ({score}) DESC, ts_rank_cd(c.tsv, q.query) DESC,"
             " c.chunk_index, c.id LIMIT :n"),
        params | weights | scope.params | {"n": n},
    ).scalars())


def _trigram_statement(scope: _Sql, n: int = CANDIDATES) -> tuple[str, dict]:
    """The trigram stage's SQL and its parameters (add ``q``). The candidates come from ``trigram_candidates`` (0017),
    which scans the GIN trigram index inside the visible versions the search resolved under row security and returns
    ids and similarity only; the ids are joined back to ``chunks``, ``document_versions`` and ``documents`` under row
    security for scope and order, so no chunk text is read outside it. Row-level security keeps the planner from using
    the index for ``<%`` on ``chunks`` itself, and made a plain join scan ``chunks`` once per version x document,
    evaluating the predicate on each scan (10.8 s on the office; R16, KTD3). Equal similarities order by chunk order,
    then id, so tied passages come back the same on every run (round 17, KTD8)."""
    return (
        "WITH cand AS MATERIALIZED (SELECT t.chunk_id AS id, t.sim"
        " FROM trigram_candidates(:q, CAST(:tv AS uuid[]), CAST(:th AS real), :n_in) t)"
        " SELECT c.id FROM cand JOIN chunks c ON c.id = cand.id JOIN document_versions v ON v.id = c.version_id"
        " JOIN documents d ON d.id = c.document_id AND d.deleted_at IS NULL"
        f" WHERE {scope.where} AND cand.sim > :th ORDER BY cand.sim DESC, c.chunk_index, c.id LIMIT :n",
        scope.params | {"tv": list(scope.version_ids or []), "th": TRIGRAM_THRESHOLD, "n": n,
                        # the candidates are the best of the whole version, a unit's block range is cut afterwards:
                        # a unit scope asks for more of them
                        "n_in": max(n, UNIT_TRIGRAM_CANDIDATES) if "scope_blk_lo" in scope.params else n},
    )


def _trigram(conn: Connection, normalized_query: str, scope: _Sql, n: int = CANDIDATES) -> list:
    if scope.version_ids is None:
        raise ValueError("the trigram stage needs the scope's visible versions (_prepare resolves them)")
    statement, params = _trigram_statement(scope, n)
    return list(conn.execute(text(statement), params | {"q": normalized_query}).scalars())


def _semantic(conn: Connection, query: str, scope: _Sql, n: int = CANDIDATES) -> list:
    provider = get_embedding_provider()
    vec = provider.embed_query(query)
    conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} WHERE {scope.where} AND c.embedding_model = :m AND c.embedding IS NOT NULL"
             " ORDER BY c.embedding <=> CAST(:v AS vector) LIMIT :n"),
        scope.params | {"m": provider.model_id, "v": to_pgvector(vec), "n": n},
    ).scalars())


def _alpha(tokens: Iterable[str]) -> str:
    return " ".join(t for t in tokens if any(ch.isalpha() for ch in t))


@lru_cache(maxsize=256)
def _place_pattern(places: tuple[str, ...]) -> re.Pattern | None:
    alternatives = []
    for place in sorted(set(places), key=len, reverse=True):  # longest first: "תל אביב-יפו" before "תל אביב"
        words = base_normalize(place).replace("-", " ").split()
        if words:
            alternatives.append(r"[\s\-]+".join(re.escape(w) for w in words))
    if not alternatives:
        return None
    return re.compile(rf"(?<![\w״׳])[והבלמשכ]{{0,2}}-?(?:{'|'.join(alternatives)})(?![\w״׳])")


def _strip_places(normalized: str, places: Iterable[str]) -> str:
    """Remove place phrases (with an optional one/two-letter Hebrew prefix) from normalized text."""
    pattern = _place_pattern(tuple(places))
    return pattern.sub(" ", normalized) if pattern else normalized


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


def _prepare(conn: Connection, scope: SearchScope | None, filters: MetadataFilters | None
             ) -> tuple[_Sql | None, FilterReport | None, int | None]:
    """The SQL scope after metadata filters, the filter report, and the number of documents when the
    caller narrowed the scope (``None`` for an open scope). ``None`` SQL means nothing can match."""
    narrowed = bool(scope and (scope.version_ids or scope.document_ids))
    if not narrowed and (filters is None or not filters.active):
        return _scope_sql(scope), None, None  # an open scope's versions are resolved by the search that needs them
    versions = _scoped_versions(conn, scope)
    report, allowed = None, None
    if filters is not None and filters.active:
        report = filter_versions(conn, [v for v, _ in versions], filters)
        allowed = report.matched
        if not allowed:
            return None, report, 0
        versions = [(v, d) for v, d in versions if v in set(allowed)]
    documents = len({d for _, d in versions}) if narrowed else None
    return replace(_scope_sql(scope, allowed), version_ids=[v for v, _ in versions]), report, documents


def _hit(r, score: float, supported: bool, tokens: list[str]) -> dict:
    hit = {"chunk_id": r.id, "document_id": r.document_id, "version_id": r.version_id,
           "page_list": list(r.page_list) if r.page_list else [], "section": r.section, "text": r.text,
           "kind": r.kind, "title": r.title, "score": round(score, 5), "lexical_support": supported,
           "snippet": snippet(r.text, tokens)}
    if r.table_index is not None:
        hit["table_index"] = r.table_index
    if r.row_index is not None:
        hit["row_index"] = r.row_index
    return hit


def _chunk_rows(conn: Connection, sql: _Sql, ids: list) -> dict:
    return {r.id: r for r in conn.execute(
        text("SELECT c.id, c.document_id, c.version_id, c.page_list, c.section, c.text, c.kind, c.table_index,"
             f" c.row_index, c.chunk_index, d.title{sql.joins} WHERE {sql.where} AND c.id = ANY(:ids)"),
        sql.params | {"ids": ids},
    ).all()}


def _small_scope_order(ranked: list, rows: dict, supported: set) -> tuple[list, int]:
    """Within a small scope: each page's best supported passage and each version's best few first (by
    score), then the rest in score order. Also returns how many lead the list."""
    first: list = []
    pages: set = set()
    per_version: dict = {}
    for cid in ranked:
        r = rows.get(cid)
        if r is None or cid not in supported:
            continue
        page = (r.version_id, r.page_list[0] if r.page_list else None)
        count = per_version.get(r.version_id, 0)
        if page not in pages or count < PER_VERSION_GUARANTEE:
            first.append(cid)
            pages.add(page)
            per_version[r.version_id] = count + 1
    chosen = set(first)
    return first + [cid for cid in ranked if cid not in chosen], len(first)


def search_evidence(conn: Connection, queries: Sequence[str], limit: int = 8, *, scope: SearchScope | None = None,
                    filters: MetadataFilters | None = None, place_terms: Iterable[str] = (),
                    window: bool = False) -> SearchOutcome:
    """Fused search over up to ``MAX_QUERIES`` queries (the first is the primary question). In a small
    scope the result may hold more than ``limit`` hits (at most twice), so that every page's best
    supported passage is in it; when the pages need more than that, the best ``limit`` lead. ``window`` (opt-in, round 11
    U8, KTD6): the fused ranking is read down to the stage's candidate depth (``CANDIDATES``; ``SMALL_SCOPE_CANDIDATES``
    in a small scope) whatever ``limit`` is, for a caller that re-orders documents itself before it cuts."""
    queries = [q for q in queries if q and q.strip()][:MAX_QUERIES]
    place_terms = list(place_terms)
    topic: list[str] = []
    for q in queries:
        topic.extend(t for t in topic_tokens(q, place_terms) if t not in topic)
    sql, report, documents = _prepare(conn, scope, filters)
    if sql is None:
        return SearchOutcome([], topic, report)
    if sql.version_ids is None:
        sql = replace(sql, version_ids=[v for v, _ in _scoped_versions(conn, scope)])
    small = documents is not None and documents <= SMALL_SCOPE_DOCUMENTS
    n = SMALL_SCOPE_CANDIDATES if small else CANDIDATES
    if window:
        limit = max(limit, n)
    scores: dict = {}
    all_tokens: list[str] = []
    guaranteed: list = []
    fuzzy_of: dict[str, list] = {}
    for qi, q in enumerate(queries):
        tokens = query_tokens(q)
        all_tokens.extend(t for t in tokens if t not in all_tokens)
        # Every passage of a small scope shares its place words: rank by the topic alone.
        ranking = (query_tokens(_strip_places(base_normalize(q), [*place_terms, *KNOWN_CITIES])) if small
                   else tokens) or tokens
        lexical = _lexical(conn, ranking, sql, n)
        alpha = _alpha(ranking) if ranking else ""
        if alpha and alpha not in fuzzy_of:  # variants that spell the same letters read the chunks once
            fuzzy_of[alpha] = _trigram(conn, alpha, sql, n)
        fuzzy = fuzzy_of.get(alpha, [])
        if qi == 0:
            guaranteed = lexical[:LEXICAL_GUARANTEE]
        for weight, ranked in zip(RRF_WEIGHTS, [lexical, fuzzy, _semantic(conn, q, sql, n)], strict=True):
            for rank, chunk_id in enumerate(ranked):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (RRF_K + rank + 1)
    ranked = sorted(scores, key=lambda cid: -scores[cid])
    # The best exact-term matches of the question always get a slot: semantic neighbours must not crowd them out.
    for pos, chunk_id in enumerate(guaranteed):
        if chunk_id in ranked[limit:]:
            ranked.remove(chunk_id)
            ranked.insert(min(pos, limit), chunk_id)
    if small:
        rows = _chunk_rows(conn, sql, ranked)
        supported = _supported(conn, ranked, topic)
        ordered, leading = _small_scope_order(ranked, rows, supported)
        # Up to twice the limit so a short document keeps every page; a long one keeps score order.
        top = ordered[:max(limit, leading) if leading <= 2 * limit else limit]
    else:
        top = ranked[:limit]
        if not top:
            return SearchOutcome([], topic, report)
        supported = _supported(conn, top, topic)
        rows = _chunk_rows(conn, sql, top)
    out = [_hit(rows[cid], scores[cid], cid in supported, all_tokens) for cid in top if cid in rows]
    return SearchOutcome(out, topic, report)


ROWS_PER_TABLE = 2


def diversify_rows(hits: list[dict], limit: int) -> list[dict]:
    """Hits in rank order with at most ``ROWS_PER_TABLE`` row passages per table, so the rows of one long table do
    not crowd out every other passage. A table passage, which holds its rows, is always kept, and the first row past
    the cap brings it forward to its own place. The first hit of a table whose matching rows the cap left out
    carries their number (``rows_not_shown``), so the caller can point at the whole table."""
    def table(h: dict) -> tuple:
        return h["version_id"], h.get("table_index")

    out: list[dict] = []
    per_table: dict[tuple, int] = {}
    taken: set[int] = set()
    for i, h in enumerate(hits):
        if i in taken:
            continue
        if h["kind"] == "table_row":
            if per_table.get(table(h), 0) >= ROWS_PER_TABLE:
                # the table's own passage stands in for the rows past the cap
                j = next((j for j in range(i + 1, len(hits)) if j not in taken and hits[j]["kind"] == "table"
                          and table(hits[j]) == table(h)), None)
                if j is None:
                    continue
                taken.add(j)
                h = hits[j]
            else:
                per_table[table(h)] = per_table.get(table(h), 0) + 1
        taken.add(i)
        out.append(h)
        if len(out) >= limit:
            break
    shown = {id(h) for h in out}
    hidden: dict[tuple, int] = {}
    for h in hits:
        if h["kind"] == "table_row" and id(h) not in shown and per_table.get(table(h), 0) >= ROWS_PER_TABLE:
            hidden[table(h)] = hidden.get(table(h), 0) + 1
    for k, h in enumerate(out):
        if hidden.get(table(h)) and h["kind"] in ("table", "table_row"):
            out[k] = h | {"rows_not_shown": hidden.pop(table(h))}
    return out


_TITLE_WORD = re.compile(r"[\w״׳\"'-]+")


def _title_words(title: str) -> set[str]:
    words = set()
    for raw in _TITLE_WORD.findall(base_normalize(title)):
        w = raw.strip("-'״׳\"")
        if len(w) >= 2 and w not in STOPWORDS:
            words.add(w)
            words.update(v for v in prefix_variants(w) if len(v) >= 2)
    return words


# A word in more than this share of the visible documents' text, or of their titles (and in more than two titles),
# is a word of the trade ("שווי", "שומה"), not a name: it names no document, whatever title holds it.
NAMING_DF_SHARE = 0.5


def _numbers_of(title: str) -> set[str]:
    return {w for w in (raw.strip("-'״׳\"") for raw in _TITLE_WORD.findall(base_normalize(title))) if w.isdigit()}


def _text_document_frequency(conn: Connection, forms: dict[str, set[str]], kinds: tuple[str, ...] = ()
                             ) -> tuple[int, dict[str, int]]:
    """The number of visible current documents (of ``kinds`` when given), and for each word the number whose current
    text holds it in any of its forms."""
    words = [w for w in forms if forms[w]]
    params: dict = {}
    cols = ["count(DISTINCT c.document_id) AS n"]
    for i, w in enumerate(words):
        cols.append(f"count(DISTINCT c.document_id) FILTER (WHERE {_any_form(forms[w], f'f{i}', params)}) AS f{i}")
    base = _Sql()
    where = base.where + (" AND d.source_kind = ANY(:df_k)" if kinds else "")
    row = conn.execute(text("SELECT " + ", ".join(cols) + base.joins + " WHERE " + where),
                       params | ({"df_k": list(kinds)} if kinds else {})).one()
    return row.n, {w: getattr(row, f"f{i}") for i, w in enumerate(words)}


def documents_named(conn: Connection, query: str, scope: SearchScope | None = None) -> list[tuple[UUID, set[str]]]:
    """Visible current documents the query names by their title: at least two query words in the title (a word and
    its prefix-stripped form are one word), one of them a number ("הגפן 12", "הזית 7") or both distinctive (in no
    more than two titles). A word of the trade never counts, however few titles hold it: one in more than
    ``NAMING_DF_SHARE`` of the visible documents' text or titles ("שווי" in a title like "שומה שווי שוק ..."). Returns
    (document id, the query words that named it), best first; empty when the query names none or too many. Word
    frequencies are over every visible document of the pool; ``scope`` only limits which of them may be named. The pool is
    the scope's source kinds — a search of the professional pool names a standard by its title as a search of the
    appraisals names an appraisal (round 10 U8) — and the appraisals when the scope names none."""
    kinds = list(scope.source_kinds) if scope and scope.source_kinds else None
    docs = [(r.id, r.title) for r in conn.execute(text(
        "SELECT d.id, d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        " WHERE d.deleted_at IS NULL AND " + ("d.source_kind = ANY(:k)" if kinds else APPRAISALS_SQL)),
        {"k": kinds} if kinds else {})]
    # a title's words, and its numbers however short (the house number of "התמר 5")
    words_of = {i: _title_words(t) | _numbers_of(t) for i, t in docs}
    df: dict[str, int] = {}
    for ws in words_of.values():
        for w in ws:
            df[w] = df.get(w, 0) + 1
    # each query word with its forms, as titles hold them
    forms: dict[str, set[str]] = {}
    for raw in _TITLE_WORD.findall(base_normalize(query)):
        w = raw.strip("-'״׳\"")
        if len(w) >= 2 or w.isdigit():
            forms.setdefault(w, set()).update({w, *prefix_variants(w)} & set(df))
    forms = {w: f for w, f in forms.items() if f}
    if not forms:
        return []
    many_titles = max(2, NAMING_DF_SHARE * len(docs))
    allowed = set(scope.document_ids) if scope and scope.document_ids else None

    def naming(trade: set[str], ids) -> list[tuple]:
        out = []
        for i in ids:
            ws = words_of[i]
            hit = {w for w, f in forms.items() if w not in trade and f & ws}
            distinctive = {w for w in hit if min(df[x] for x in forms[w] & ws) <= 2}
            if len(hit) >= 2 and any(not w.isdigit() for w in hit) and (
                    any(w.isdigit() for w in hit) or len(distinctive) >= 2):
                out.append((len(distinctive), len(hit), i, hit))
        return out

    # the documents the titles alone name; a word of the trade by the text can only remove words, so the text's
    # frequencies are read only for the words of these candidates (and not at all when there is none)
    trade = {w for w, f in forms.items() if max(df[x] for x in f) > many_titles}
    candidates = naming(trade, [i for i in words_of if allowed is None or i in allowed])
    if not candidates:
        return []
    alpha = {w: forms[w] for _, _, _, hit in candidates for w in hit if not w.isdigit()}
    n, text_df = _text_document_frequency(conn, alpha, tuple(kinds or ()))
    trade |= {w for w in alpha if text_df.get(w, 0) > NAMING_DF_SHARE * n}
    named = naming(trade, [i for _, _, i, _ in candidates])
    named.sort(key=lambda x: (-x[0], -x[1]))
    if not named or len(named) > 2:
        return []
    return [(i, hit) for _, _, i, hit in named]


def _scope_terms(query: str) -> list[list[str]]:
    """The query's terms, each with its forms: the word, its prefix-stripped and inflected variants, and the
    other spelling of an abbreviation ("דמ״ש" / "דמי שכירות" become one term)."""
    from app.extraction.abbreviations import variants
    from app.extraction.normalize_text import _word_variants

    terms: list[list[str]] = []
    base = base_normalize(query)
    alternates = [base_normalize(v) for v in variants(query, limit=4)]
    for raw in _WORD_RE.findall(base):
        tok = raw.strip("./-׳״")
        if tok in STOPWORDS or (sum(ch.isalpha() for ch in tok) < 2 and not any(ch.isdigit() for ch in tok)):
            continue
        forms = [tok, *(_word_variants(tok) if _HEB.fullmatch(tok) else [])]
        if "״" in tok:  # an abbreviation: its spelled-out form counts as the same term
            for alt in alternates:
                if tok not in alt:
                    forms += [w for w in alt.split() if w not in base.split() and w not in STOPWORDS]
        terms.append(list(dict.fromkeys(f for f in forms if len(f) >= 2)))
    return [t for t in terms if t]


def documents_matching(conn: Connection, query: str) -> list[dict]:
    """Every visible current document that contains **every** term of ``query`` — in its title or in its current
    text, each term in any of its forms. An AND per document, so a generic word of the query cannot pull in
    documents that lack its defining terms. Returns [{document_id, title, in_title, in_text, hits}], title
    matches first, then by hits. No ranking cut-off: the whole matching set."""
    terms = _scope_terms(query)
    if not terms:
        return []
    params: dict = {}
    filters = []
    for i, forms in enumerate(terms):
        q = " || ".join(f"plainto_tsquery('simple', :t{i}_{j})" for j in range(len(forms)))
        params |= {f"t{i}_{j}": f for j, f in enumerate(forms)}
        filters.append(f"count(c.id) FILTER (WHERE c.tsv @@ ({q})) AS h{i}")
    rows = conn.execute(text(
        "SELECT d.id, d.title, " + ", ".join(filters) + " FROM documents d JOIN document_versions v"
        " ON v.document_id = d.id AND v.is_current LEFT JOIN chunks c ON c.version_id = v.id"
        f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL} GROUP BY d.id, d.title"), params).all()
    out = []
    term_sets = [set(t) for t in terms]
    for r in rows:
        title = _title_words(r.title)
        in_title = [t[0] for t, ts in zip(terms, term_sets, strict=True) if title & ts]
        in_text = [t[0] for i, t in enumerate(terms) if getattr(r, f"h{i}")]
        if all(title & ts or getattr(r, f"h{i}") for i, ts in enumerate(term_sets)):
            out.append({"document_id": r.id, "title": r.title, "in_title": in_title, "in_text": in_text,
                        "hits": sum(getattr(r, f"h{i}") for i in range(len(terms)))})
    out.sort(key=lambda d: (-len(d["in_title"]), -d["hits"], d["title"], str(d["document_id"])))
    return out


# --- units: the second level of retrieval (round 9 U9: KTD7; R10–R13) ------------------------------------------

@dataclass
class UnitMatches:
    """What ``units_matching`` found. ``documents``: ``documents_matching`` rows of the files that hold the terms — for
    a file of several units, the units that hold **all** of them (``units``, subject candidates only); ``comparables``:
    units where the terms appear only as a comparable (an appendix, a comparables part, or a property that only the
    comparison tables of an appraisal name); ``split``: files holding every term, but in different units, so that no
    single unit is a match (R11)."""

    documents: list[dict] = field(default_factory=list)
    comparables: list[dict] = field(default_factory=list)
    split: list[dict] = field(default_factory=list)

    @property
    def units(self) -> list[dict]:
        return [u for d in self.documents for u in d.get("units") or ()]


_FILE_SQL = (
    "SELECT d.id, d.title, v.id AS version_id, COALESCE(v.ingestion->>'reading_id', '') AS reading_id, {counts}"
    " FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
    f" LEFT JOIN chunks c ON c.version_id = v.id WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}"
    " GROUP BY d.id, d.title, v.id")


def _tsquery(forms: Sequence[str], prefix: str, params: dict) -> str:
    params |= {f"{prefix}_{j}": f for j, f in enumerate(forms)}
    return " || ".join(f"plainto_tsquery('simple', :{prefix}_{j})" for j in range(len(forms)))


def _term_regexes(terms: list[list[str]]) -> list[re.Pattern]:
    """One pattern per term over normalized text: any of its forms as a whole word."""
    return [re.compile(r"(?<![\w])(?:" + "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
                       + r")(?![\w])") for forms in terms]


def _name_fit(names: Sequence[str], term_sets: list[set[str]]) -> tuple[set[int], int]:
    """The terms a unit's title and aliases hold (indexes), and how many words of the best-fitting name the question
    does not use (0: the question says the whole name). The name that holds the most terms, then the shortest, is
    the one that counts."""
    held, _, extra = _best_name_fit(names, term_sets)
    return held, extra


def _best_name_fit(names: Sequence[str], term_sets: list[set[str]]) -> tuple[set[int], int, int]:
    """``_name_fit`` with the number of terms the best-fitting name itself holds, before the words it leaves unsaid."""
    best: tuple[int, int] = (0, 0)
    held: set[int] = set()
    for name in names:
        words = _title_words(name) | _numbers_of(name)
        got = {i for i, ts in enumerate(term_sets) if words & ts}
        held |= got
        size = len(_TITLE_WORD.findall(base_normalize(name)))
        extra = max(0, size - len(got))
        if got and (len(got), -extra) > (best[0], -best[1]):
            best = (len(got), extra)
    return held, best[0], best[1]


def units_matching(conn: Connection, query: str) -> UnitMatches:
    """The visible current files, and the units within them, that hold **every** term of ``query`` (the terms of
    ``documents_matching``: a title word, or a term in any of its forms). For a file with one unit the file is its
    unit and the match is the file-level AND, as before. For a file of several units the AND is per unit (R11): a
    term is satisfied in a unit by its title or an alias (a name the document itself states), by a chunk whose
    blocks lie inside the unit, or by a block of the unit inside a chunk that runs across the unit's start (the last
    chapter of one report runs on into the next one's title block, whose words are the next unit's); the file's
    title satisfies every unit. Terms split between two units match neither, whatever the file holds. An appendix or
    comparables unit, and a unit whose only mentions of the terms are cells of its comparison tables, is reported
    as a comparable and is never a candidate for the asked subject. Under row security: a unit of a file the user
    cannot see is not here."""
    from app.chat import contexts as unit_contexts
    from app.chat import units as unit_store

    terms = _scope_terms(query)
    if not terms:
        return UnitMatches()
    params: dict = {}
    counts = []
    queries = []
    for i, forms in enumerate(terms):
        q = _tsquery(forms, f"t{i}", params)
        queries.append(q)
        counts.append(f"count(c.id) FILTER (WHERE c.tsv @@ ({q})) AS h{i}")
    term_sets = [set(t) for t in terms]
    out = UnitMatches()
    tag = unit_store.current_tag()
    passing = []
    for r in conn.execute(text(_FILE_SQL.format(counts=", ".join(counts))), params).all():
        title = _title_words(r.title)
        if all(title & ts or getattr(r, f"h{i}") for i, ts in enumerate(term_sets)):
            passing.append(r)
    stored: dict[UUID, tuple] = unit_store.stored_many(conn, [r.version_id for r in passing])
    for r in passing:
        if r.version_id in stored:
            continue
        try:  # a version published before units were stored: built on first use
            stored[r.version_id] = tuple(unit_store.ensure(conn, r.version_id))
        except Exception:  # noqa: BLE001 - units that cannot be had: the file is its own unit
            stored[r.version_id] = ()
    multi = [r for r in passing if len(stored[r.version_id]) > 1]
    multi_ids = {r.version_id for r in multi}
    per_unit = _unit_passages(conn, multi, stored, terms, queries, params) if multi else {}
    for r in passing:
        title = _title_words(r.title)
        in_title = [t[0] for t, ts in zip(terms, term_sets, strict=True) if title & ts]
        in_text = [t[0] for i, t in enumerate(terms) if getattr(r, f"h{i}")]
        total = sum(getattr(r, f"h{i}") for i in range(len(terms)))
        row = {"document_id": r.id, "title": r.title, "in_title": in_title, "in_text": in_text, "hits": total,
               "version_id": r.version_id, "reading_id": r.reading_id}
        if r.version_id not in multi_ids:
            out.documents.append(row | {"units": [], "multi": False})
            continue
        row |= {"multi": True, "units": []}
        in_file_title = {i for i, ts in enumerate(term_sets) if title & ts}
        matched = False
        for u in stored[r.version_id]:
            names = ([u.title] if u.title else []) + list(u.aliases)
            named, extra = _name_fit(names, term_sets)
            prose, tabular = per_unit[(r.version_id, u.unit_no)]
            by_name = named | in_file_title
            if len(by_name | prose | tabular) < len(terms):
                continue
            matched = True
            unit = unit_contexts.unit_row(
                u, r.id, r.version_id, r.reading_id, tag, file_title=r.title,
                matched_by=("alias" if len(by_name) == len(terms) and named else
                            "file_title" if len(by_name) == len(terms) else "passage"),
                name_terms=len(named), name_extra=extra, hits=len(prose) + len(tabular),
                in_name=[terms[i][0] for i in sorted(named | in_file_title)])
            # a unit is the asked subject when its name and its prose hold every term; one that needs cells of its
            # comparison tables to hold them has the property as a comparable, not as its own subject
            comparable = u.kind in unit_contexts.NON_SUBJECT_KINDS or len(by_name | prose) < len(terms)
            if comparable:
                out.comparables.append(unit)
            else:
                row["units"].append(unit)
        if row["units"]:
            out.documents.append(row)
        elif not matched:
            out.split.append({"document_id": r.id, "title": r.title, "units": len(stored[r.version_id])})
    printed_labels(conn, [*out.units, *out.comparables])
    out.documents.sort(key=lambda d: (-len(d["in_title"]), -d["hits"], d["title"], str(d["document_id"])))
    return out


def _unit_passages(conn: Connection, files: list, stored: dict, terms: list[list[str]], queries: list[str],
                   params: dict) -> dict[tuple, tuple[set[int], set[int]]]:
    """For each unit of the given multi-unit files: (terms found in a prose passage of it, terms found only in a
    table passage of it), by the chunks inside it and the blocks of the chunks that run across its start."""
    vs, ns, los, his = [], [], [], []
    for r in files:
        for u in stored[r.version_id]:
            vs.append(r.version_id)
            ns.append(u.unit_no)
            los.append(u.block_first)
            his.append(u.block_last)
    ranges = {"vs": vs, "ns": ns, "los": los, "his": his}
    ranges_sql = ("unnest(CAST(:vs AS uuid[]), CAST(:ns AS int[]), CAST(:los AS int[]), CAST(:his AS int[]))"
                  " AS u(version_id, unit_no, lo, hi)")
    cols = ", ".join(f"count(c.id) FILTER (WHERE c.tsv @@ ({q})) AS h{i}, count(c.id) FILTER (WHERE c.tsv @@ ({q})"
                     f" AND c.kind NOT IN ('table', 'table_row')) AS p{i}" for i, q in enumerate(queries))
    found: dict[tuple, tuple[set[int], set[int]]] = {(v, n): (set(), set()) for v, n in zip(vs, ns, strict=True)}
    for r in conn.execute(text(
            f"SELECT u.version_id, u.unit_no, {cols} FROM {ranges_sql} JOIN chunks c ON c.version_id = u.version_id"
            " AND c.block_start >= u.lo AND c.block_end <= u.hi GROUP BY u.version_id, u.unit_no"),
            params | ranges):
        prose = {i for i in range(len(terms)) if getattr(r, f"p{i}")}
        every = {i for i in range(len(terms)) if getattr(r, f"h{i}")}
        found[(r.version_id, r.unit_no)] = (prose, every - prose)
    # chunks that run across the start of a unit: the blocks of the unit they hold decide
    any_term = " OR ".join(f"c.tsv @@ ({q})" for q in queries)
    across = conn.execute(text(
        f"SELECT DISTINCT c.id, c.version_id, c.block_start, c.block_end FROM {ranges_sql} JOIN chunks c"
        " ON c.version_id = u.version_id AND c.block_start < u.lo AND c.block_end >= u.lo"
        f" WHERE {any_term}"), params | ranges).all()
    if not across:
        return found
    patterns = _term_regexes(terms)
    by_version: dict[UUID, list] = {}
    for r in across:
        by_version.setdefault(r.version_id, []).append(r)
    units_of = {r.version_id: stored[r.version_id] for r in files}
    for version_id, chunks in by_version.items():
        # only the blocks of the chunks that run across a unit start (a few blocks each), not the span between them
        for b in conn.execute(text(
                "SELECT DISTINCT b.block_index, b.kind, b.text FROM document_blocks b JOIN unnest(CAST(:los AS int[]),"
                " CAST(:his AS int[])) AS r(lo, hi) ON b.block_index BETWEEN r.lo AND r.hi WHERE b.version_id = :v"
                " AND b.text IS NOT NULL"),
                {"v": version_id, "los": [c.block_start for c in chunks], "his": [c.block_end for c in chunks]}):
            unit = next((u for u in units_of[version_id] if u.block_first <= b.block_index <= u.block_last), None)
            if unit is None:
                continue
            norm = base_normalize(b.text)
            held = {i for i, p in enumerate(patterns) if p.search(norm)}
            if held:
                prose, tabular = found[(version_id, unit.unit_no)]
                (tabular if b.kind == "table" else prose).update(held)
                found[(version_id, unit.unit_no)] = (prose, tabular - prose)
    return found


def unit_term_holders(conn: Connection, terms: list[list[str]], units: Sequence) -> dict[tuple, set[int]]:
    """For each unit row (``version_id``, ``unit_no``, ``block_first``, ``block_last``, ``title``, ``aliases``): the
    terms (their indexes) it holds as a subject does in ``units_matching`` — in its title or an alias, or in a prose
    passage of its blocks (a table cell alone does not make a unit the subject). One passage query for all the units
    of all the given files; keyed by (version id, unit number)."""
    from types import SimpleNamespace

    params: dict = {}
    queries = [_tsquery(forms, f"t{i}", params) for i, forms in enumerate(terms)]
    stored: dict[UUID, list] = {}
    for u in units:
        stored.setdefault(u.version_id, []).append(u)
    per_unit = _unit_passages(conn, [SimpleNamespace(version_id=v) for v in stored], stored, terms, queries, params)
    term_sets = [set(t) for t in terms]
    out: dict[tuple, set[int]] = {}
    for u in units:
        named, _ = _name_fit(([u.title] if u.title else []) + list(u.aliases or ()), term_sets)
        out[(u.version_id, u.unit_no)] = named | per_unit[(u.version_id, u.unit_no)][0]
    return out


def document_term_holders(conn: Connection, terms: list[list[str]]) -> dict[UUID, set[int]]:
    """For each visible current document that is not split into several units (one unit, or none): the terms (their
    indexes) it holds as ``documents_matching`` reads them — in its title or in any chunk of its current text, each in
    any of its forms. A version that holds only the units of an earlier derivation gets the current ones first
    (``entities.ensure_current_units``, additive: the old rows stay), so the "not split" test below reads the current
    derivation and a document the new derivation splits is never counted whole (round 16 U5, R10). One query once every
    version is current; a document that holds none is not in the result."""
    from app.chat import units as unit_store
    from app.chat.entities import ensure_current_units

    ensure_current_units(conn)
    params: dict = {}
    counts = ", ".join(f"count(c.id) FILTER (WHERE c.tsv @@ ({_tsquery(forms, f't{i}', params)})) AS h{i}"
                       for i, forms in enumerate(terms))
    term_sets = [set(t) for t in terms]
    out: dict[UUID, set[int]] = {}
    for r in conn.execute(text(
            f"SELECT d.id, d.title, {counts} FROM documents d JOIN document_versions v ON v.document_id = d.id"
            f" AND v.is_current LEFT JOIN chunks c ON c.version_id = v.id WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}"
            " AND NOT EXISTS"
            " (SELECT 1 FROM document_units u WHERE u.version_id = v.id AND u.reading_id = COALESCE("
            f"v.ingestion->>'reading_id', '') AND {unit_store.current_key_sql()} OFFSET 1) GROUP BY d.id, d.title"), params):
        title = _title_words(r.title)
        held = {i for i, ts in enumerate(term_sets) if title & ts or getattr(r, f"h{i}")}
        if held:
            out[r.id] = held
    return out


def unit_opening_text(conn: Connection, version_id: UUID, block_first: int, block_last: int) -> str:
    """The normalized text of a unit's opening: its blocks on the page of its first block (where an appraisal states
    what it is about). One query."""
    rows = conn.execute(text(
        "SELECT b.text FROM document_blocks b WHERE b.version_id = :v AND b.block_index BETWEEN :a AND :z"
        " AND b.text IS NOT NULL AND b.page IS NOT DISTINCT FROM (SELECT f.page FROM document_blocks f"
        " WHERE f.version_id = :v AND f.block_index = :a) ORDER BY b.block_index"),
        {"v": version_id, "a": block_first, "z": block_last}).all()
    return "\n".join(base_normalize(r.text) for r in rows)


def printed_labels(conn: Connection, units: list[dict]) -> None:
    """Sets ``first_label`` on each unit row (``version_id``, ``page_first``): its first printed page label, the part of
    its anchor that survives a new reading: the page's stored label, else — a report whose numbering restarts keeps
    none — the number its page-number line states."""
    from app.chat import units as unit_store

    firsts = [(u["version_id"], u["page_first"]) for u in units if u["page_first"] is not None]
    labels: dict[tuple, str | None] = {}
    if firsts:
        for r in conn.execute(text(
                "SELECT p.version_id, p.page_no, p.printed_label, left(p.text, 600) || E'\\n' || right(p.text, 600)"
                " AS edge FROM pages p JOIN unnest(CAST(:v AS uuid[]), CAST(:n AS int[])) AS w(version_id, page_no)"
                " ON p.version_id = w.version_id AND p.page_no = w.page_no"),
                {"v": [v for v, _ in firsts], "n": [n for _, n in firsts]}):
            number = unit_store.footer_number(r.edge or "") if not r.printed_label else None
            labels[(r.version_id, r.page_no)] = r.printed_label or (str(number) if number is not None else None)
    for u in units:
        u["first_label"] = labels.get((u["version_id"], u["page_first"]))


def documents_sharing_title_words(conn: Connection, question: str) -> dict[str, list[tuple[UUID, str]]]:
    """Question words that appear in the titles of several visible documents, but not in most titles: word ->
    [(document id, title)]. Between 2 and min(8, 40% of the titles) documents, so a word every title carries
    ("תקן", "שומה") never counts."""
    docs = [(r.id, r.title) for r in conn.execute(text(
        "SELECT d.id, d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL} ORDER BY d.title, d.id"))]
    if len(docs) < 2:
        return {}
    ceiling = min(8, max(2, int(0.4 * len(docs))))
    words_of = {i: _title_words(t) for i, t in docs}
    out: dict[str, list[tuple[UUID, str]]] = {}
    for raw in _TITLE_WORD.findall(base_normalize(question)):
        w = raw.strip("-'״׳\"")
        if len(w) < 2 or w in STOPWORDS or not any(ch.isalpha() for ch in w):
            continue
        forms = {w, *prefix_variants(w)}
        hit = [(i, t) for i, t in docs if words_of[i] & forms]
        if 2 <= len(hit) <= ceiling:
            out[w] = hit
    return out


# --- documents whose latest version was not read (round 9 U6, KTD11) ------------------------------------

# A version in one of these states holds nothing the search can reach. Only a document with no current version at
# all is "not read" here: one whose older version is current is answered from that version, as before.
UNREAD_STATUSES = ("failed", "pending", "processing")
_UNREAD_FROM = (
    " FROM documents d JOIN LATERAL (SELECT v.id, v.version_no, v.status, v.status_reason, v.ingestion, v.created_at"
    " FROM document_versions v WHERE v.document_id = d.id ORDER BY v.version_no DESC LIMIT 1) lv ON true"
    f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL} AND lv.status = ANY(:unread)"
    " AND NOT EXISTS (SELECT 1 FROM document_versions c WHERE c.document_id = d.id AND c.is_current)")


def unread_documents(conn: Connection) -> list[dict]:
    """The visible documents whose latest version (by ``version_no``, as the documents screen shows it) is failed,
    waiting or being processed and that have no current version, whatever the reason: [{document_id, title, status,
    reason, progress, uploaded_at}], most recently uploaded first. The same visibility as the current-version lookups
    (RLS, deleted documents excluded): a file of a group the user cannot see is not here. ``progress``: the pages
    read and in the file of a version being processed (None otherwise)."""
    from types import SimpleNamespace

    from app.platform.documents import _progress

    rows = conn.execute(text(
        "SELECT d.id, d.title, lv.status, lv.status_reason, lv.ingestion, lv.created_at" + _UNREAD_FROM
        + " ORDER BY lv.created_at DESC, d.title, d.id"), {"unread": list(UNREAD_STATUSES)}).all()
    return [{"document_id": r.id, "title": r.title, "status": r.status, "reason": r.status_reason,
             "progress": _progress(SimpleNamespace(status=r.status, ingestion=r.ingestion)),
             "uploaded_at": r.created_at} for r in rows]


def unread_matching(conn: Connection | None, query: str, files: list[dict] | None = None) -> list[dict]:
    """The unread documents (``unread_documents``) whose **title** holds every term of ``query``, each term in any of
    its forms: a document that was not read has no text to match. Rows as ``documents_matching`` gives them (no
    passages: ``hits`` 0), with the state of the version. ``files``: the unread documents, when the caller has them
    already (then ``conn`` is not used and may be None)."""
    terms = [set(t) for t in _scope_terms(query)]
    if not terms:
        return []
    out = []
    for d in (unread_documents(conn) if files is None else files):
        title = _title_words(d["title"]) | _numbers_of(d["title"])
        if all(title & ts for ts in terms):
            out.append(d | {"in_title": [sorted(title & ts)[0] for ts in terms], "in_text": [], "hits": 0})
    return out


def unread_named(conn: Connection, query: str, files: list[dict] | None = None) -> list[dict]:
    """The unread documents a search query names by their title, as ``documents_named`` names current ones: at least
    two query words in the title, one of them a number or both distinctive (in no more than two visible titles,
    current ones and unread ones together). ``files``: the unread documents, when the caller has them already."""
    unread = unread_documents(conn) if files is None else files
    if not unread:
        return []
    titles = [r.title for r in conn.execute(text(
        "SELECT d.title FROM documents d JOIN document_versions v ON v.document_id = d.id AND v.is_current"
        f" WHERE d.deleted_at IS NULL AND {APPRAISALS_SQL}"))] + [d["title"] for d in unread]
    df: dict[str, int] = {}
    for t in titles:
        for w in _title_words(t) | _numbers_of(t):
            df[w] = df.get(w, 0) + 1
    forms: dict[str, set[str]] = {}
    for raw in _TITLE_WORD.findall(base_normalize(query)):
        w = raw.strip("-'״׳\"")
        if len(w) >= 2 or w.isdigit():
            forms.setdefault(w, set()).update({w, *prefix_variants(w)} & set(df))
    forms = {w: f for w, f in forms.items() if f}
    many = max(2, NAMING_DF_SHARE * len(titles))
    out = []
    for d in unread:
        words = _title_words(d["title"]) | _numbers_of(d["title"])
        hit = {w for w, f in forms.items() if f & words and max(df[x] for x in f) <= many}
        distinctive = {w for w in hit if min(df[x] for x in forms[w] & words) <= 2}
        if len(hit) >= 2 and any(not w.isdigit() for w in hit) and (
                any(w.isdigit() for w in hit) or len(distinctive) >= 2):
            out.append(d)
    return out


def question_words_in_title(question: str, title: str) -> frozenset[str]:
    """The words of ``question`` that ``title`` carries (in any prefixed form), as the question writes them."""
    in_title = _title_words(title)
    out = set()
    for raw in _TITLE_WORD.findall(base_normalize(question)):
        w = raw.strip("-'״׳\"")
        if len(w) >= 2 and w not in STOPWORDS and in_title & {w, *prefix_variants(w)}:
            out.add(w)
    return frozenset(out)


def search_passages(conn: Connection, query: str, limit: int = 8, *, scope: SearchScope | None = None,
                    window: bool = False) -> list[dict]:
    """The search the answering tools and the search screen share: the query and its abbreviation variants over
    the scope, and — when the query names a document by its title — the same query inside that document, whose
    passages lead (a document's name is in its title, not in its passages, so a global search ranks other
    documents' passages that share the name's words above it). Rows of one table never crowd out the rest.
    ``window`` (opt-in; the chat's ``search`` in a time mode, round 11 U8, KTD6): the fused ranking is returned down to
    the candidate depth (``search_evidence``) instead of its best ``limit``, so the caller can order the documents of the
    whole window by their dates before it cuts; every other caller is unchanged."""
    from app.extraction.abbreviations import variants

    queries = [query, *variants(query)]
    hits = search_evidence(conn, queries, limit * 2, scope=scope, window=window).hits
    if scope is not None and scope.blocks:  # inside a unit already: a document's title names nothing further
        return diversify_rows(hits, max(limit, len(hits)) if window else limit)
    named = documents_named(conn, query, scope)
    if named:
        inside = SearchScope(document_ids=tuple(i for i, _ in named), source_kinds=scope.source_kinds if scope else (),
                             exclude_versions=scope.exclude_versions if scope else ())
        focused = search_evidence(conn, queries, limit * 2, scope=inside, window=window).hits
        seen = {h["chunk_id"] for h in focused}
        hits = focused[: max(3, limit // 2) * 2] + [h for h in hits if h["chunk_id"] not in seen] + focused[
            max(3, limit // 2) * 2:]
    return diversify_rows(hits, max(limit, len(hits)) if window else limit)


def hybrid_search(conn: Connection, query: str, limit: int = 8, *, scope: SearchScope | None = None,
                  filters: MetadataFilters | None = None, extra_queries: Sequence[str] = (),
                  place_terms: Iterable[str] = ()) -> list[dict]:
    """Hits only; ``search_evidence`` also returns the topic terms and the filter report."""
    return search_evidence(conn, [query, *extra_queries], limit, scope=scope, filters=filters,
                           place_terms=place_terms).hits


# --- locate: rank documents, not passages ---------------------------------------------------------

LOCATE_LIMIT = 12
LOCATE_CANDIDATES = 600
PASSAGES_PER_DOCUMENT = 3
RELATIVE_THRESHOLD = 0.9  # partial documents: keep those scoring at least this share of the best
COMPLETE_SHARE = 0.9  # a passage carrying this share of the topic weight supports the whole topic
PHRASE_WEIGHT = 0.25  # bonus for adjacent question words found next to each other (ranks, never excludes)
PHRASE_WINDOW = 2
# Words between a negation and the term it governs: at most this many, and at most one content word
# ("אינו כולל X", "אין בו X").
NEGATION_GAP = 2
# Words that say a thing is there (grammar, not topic): the only content word a negation may reach across.
EXISTENCE_WORDS = frozenset({"כולל", "כוללת", "כוללים", "כוללות", "קיים", "קיימת", "קיימים", "קיימות", "מצויד",
                             "מצוידת", "מצוידים", "נמצא", "נמצאת", "נמצאה", "נמצאו", "נמצאים", "נמצאות",
                             "בנמצא", "מצוי", "מצויה", "מצויים", "מותקן", "מותקנת", "הותקן", "הותקנה"})
# A word found in at most this share of the documents in scope tells them apart; one in more of them
# ("בניין", or an address every passage of a one-document scope names) does not.
DISTINCTIVE_DF_SHARE = 0.5
# Question-form words of "which documents mention ..." questions: never topic terms.
LOCATE_META = frozenset(
    "מוזכר מוזכרת מוזכרים מוזכרות מזכיר מזכירה מזכירים מזכירות מצוין מצוינת מצוינים מצוינות צוין צוינה "
    "כתובה כתובים מופיע מופיעה מופיעים מופיעות איפה היכן באיזו באיזה באילו אלו שומה השומה בשומה שומת "
    # emphasis and hedging words a question adds ("...בכלל?")
    "בכלל כלשהו כלשהי כלשהם כלשהן בדיוק ממש אכן בפרט למשל".split()
)
# Words that end the reach of a negation: a coordinator opens a new clause, "יש" affirms.
COORDINATORS = frozenset("אך אבל או אולם ואילו אלא".split())
AFFIRMATIVES = frozenset({"יש", "שיש", "ויש"})
# Negations that are prepositions or nouns ("without", "absence of"): they negate only what follows them.
PREPOSED_NEGATIONS = frozenset("ללא בלי מבלי היעדר העדר בהיעדר בהעדר".split())
# Words that ask about table structure; a table-row passage satisfies them.
TABLE_WORDS = frozenset("טבלה טבלת טבלאות עמודה עמודת עמודות טור טורים".split())
_WORD_RE = re.compile(r"[\w״׳./]+")
_HEB = re.compile(r"[א-ת][א-ת״׳]*")
# Between two words: a comma, semicolon, dash, parenthesis or table cell border closes a clause.
_CLAUSE_BREAK = re.compile(r"[,;|()\[\]—–]")  # " - " joins a label and its value ("X - לא"); it is no break


@dataclass
class _Group:
    """One question word: the forms it matches, its document frequency and weight."""

    word: str
    position: int
    forms: frozenset[str] = frozenset()
    df: int = 0
    weight: float = 0.0
    kind: str = "term"  # term | negation | table
    governs: tuple[str, ...] = ()  # negation: the question terms it governs ("בלי X" -> "X")


@dataclass
class RankedDocument:
    document_id: UUID
    version_id: UUID
    title: str
    score: float
    full_support: bool  # one passage carries every topic term of the question
    matched_terms: list[str]
    missing_terms: list[str]
    pages: list[int]
    passages: list[dict]  # hit dicts, as ``search_evidence`` returns them (lexical_support is True)


@dataclass
class LocateOutcome:
    documents: list[RankedDocument]
    topic_terms: list[str]
    filter_report: FilterReport | None = None
    # Question words that occur in no document in scope. When set, no document is fully supported: the
    # caller can say these words were not found (and abstain when ``documents`` is empty).
    absent_terms: list[str] = field(default_factory=list)


def _negation(word: str) -> bool:
    # "אל" is also the preposition "to": in running text it is never read as a negation.
    return is_negation(word, ambiguous=False)


@lru_cache(maxsize=8192)
def _word_forms(word: str) -> frozenset[str]:
    """The forms the index holds for a passage word: itself, prefix-stripped and singular forms."""
    if not _HEB.fullmatch(word):
        return frozenset({word})
    out = [word, *prefix_variants(word)]
    return frozenset(out + [v for w in out for v in inflection_variants(w)])


def _words(text_: str, places: Sequence[str] = ()) -> list[tuple[str, int, int]]:
    """(word, sentence number, clause number) of normalized text without its place names; a word ending
    with a period closes its sentence, and a sentence end or ``_CLAUSE_BREAK`` closes a clause."""
    out: list[tuple[str, int, int]] = []
    sentence = clause = prev_end = 0
    norm = _strip_places(base_normalize(text_), places) if places else base_normalize(text_)
    for m in _WORD_RE.finditer(norm):
        if out and _CLAUSE_BREAK.search(norm, prev_end, m.start()):
            clause += 1
        raw = m.group(0)
        word = raw.strip("./-׳״")
        if word:
            out.append((word, sentence, clause))
        prev_end = m.end()
        follows = norm[m.end():m.end() + 1]
        if (raw.endswith(".") and not re.fullmatch(r"[\d.]+", raw)) or (follows and follows in "!?;"):
            sentence += 1
            clause += 1
    return out


def _light(word: str) -> bool:
    """A function word: a stopword, or a preposition/pronoun of at most two letters ("בו", "את")."""
    return word in STOPWORDS or sum(ch.isalpha() for ch in word) <= 2


def _blocks(word: str) -> bool:
    """A word a negation does not reach across: a coordinator, a ו-prefixed word opening a new conjunct
    ("ואין", "וגם"), or the affirmative "יש"."""
    return word in COORDINATORS or word in AFFIRMATIVES or (word.startswith("ו") and len(word) >= 3)


def _bare(word: str) -> str:
    """The word without a one-letter ו/ש prefix ("וכולל" -> "כולל")."""
    return word[1:] if len(word) > 3 and word[0] in "וש" and word[1:] in EXISTENCE_WORDS else word


def _governs(words: list[tuple[str, int, int]], i: int, j: int) -> bool:
    """Whether the negation at ``i`` governs the word at ``j``, in the same clause:

    - before it, with at most ``NEGATION_GAP`` words between, at most one of them a content word, and no
      coordinator or affirmative ("ללא X", "אינו כולל X", "אין בו X"; not "אין Y ויש X");
    - or directly after it, bare (no ו/ש prefix) and not a preposition ("ללא", "בלי"), as a label and its
      value ("X: אין", "X לא קיימת"), when no "יש" affirms the word earlier in the clause (not "יש X ואין
      Y", not "X שלא הוחלפה", not "X ללא Y")."""
    clause = words[j][2]
    if words[i][2] != clause:
        return False
    if i < j:
        gap = [w for w, *_ in words[i + 1:j]]
        # the one content word allowed between them must say "has / includes" ("אינו כולל X"); any other noun is
        # what is negated ("אין צורך ב-X", "אין בעיה עם X" state that X exists)
        return (len(gap) <= NEGATION_GAP and all(_light(w) or _bare(w) in EXISTENCE_WORDS for w in gap)
                and not any(_blocks(w) for w in gap))
    after = [w for w, _, c in words[i + 1:] if c == clause]
    # "X: אין", "X לא קיימת": the negation ends the clause or denies existence; "X לא תקינה" says X exists
    return (i == j + 1 and words[i][0] in NEGATION_WORDS and words[i][0] not in PREPOSED_NEGATIONS
            and (not after or _bare(after[0]) in EXISTENCE_WORDS)
            and not any(w in AFFIRMATIVES for w, _, c in words[:j] if c == clause))


def _negated_words(words: list[tuple[str, int, int]], i: int) -> tuple[str, ...]:
    """The words a question's negation at ``i`` negates: the first content word it governs after it ("בלי
    X"), and the content word it directly follows as a predicate ("X לא קיים")."""
    def content(j: int) -> bool:
        return not _light(words[j][0]) and words[j][0] not in LOCATE_META and _governs(words, i, j)

    after = next((words[j][0] for j in range(i + 1, len(words)) if content(j)), None)
    before = words[i - 1][0] if i and content(i - 1) else None
    return tuple(w for w in (after, before) if w)


def _question_groups(query: str, place_terms: Iterable[str]) -> list[_Group]:
    groups: list[_Group] = []
    located = _words(query, (*place_terms, *KNOWN_CITIES))
    words = [w for w, *_ in located]
    for pos, word in enumerate(words):
        if _negation(word):
            if not any(g.kind == "negation" for g in groups):
                groups.append(_Group(word, pos, kind="negation", governs=_negated_words(located, pos)))
        elif word in TABLE_WORDS or any(v in TABLE_WORDS for v in prefix_variants(word)):
            if not any(g.kind == "table" for g in groups):
                groups.append(_Group(word, pos, kind="table"))
        elif (word not in STOPWORDS and word not in LOCATE_META
              and not any(v in LOCATE_META for v in prefix_variants(word)) and not _YEAR.fullmatch(word)
              and (sum(ch.isalpha() for ch in word) >= 2 or any(ch.isdigit() for ch in word))
              and not any(g.word == word for g in groups)):
            groups.append(_Group(word, pos))
    return groups


def _term_forms(word: str) -> frozenset[str]:
    """Forms a question word matches: the word as asked with its singular forms, and each prefix-stripped
    form with only its plural-to-singular forms (a construct rule on a stripped form would turn a root
    letter taken for a prefix into an unrelated common word)."""
    if not _HEB.fullmatch(word):
        return frozenset({word})
    forms = {word, *inflection_variants(word)}
    for stem in prefix_variants(word):
        forms.add(stem)
        if len(stem) >= 5 and stem.endswith("ות"):
            forms.update({stem[:-2] + "ת", stem[:-2] + "ה"})
        elif len(stem) >= 5 and stem.endswith("ים"):
            forms.add(stem[:-2])
    return frozenset(forms)


def _any_form(forms: Iterable[str], prefix: str, params: dict) -> str:
    parts = []
    for i, f in enumerate(sorted(forms)):
        params[f"{prefix}_{i}"] = f
        parts.append(f"plainto_tsquery('simple', :{prefix}_{i})")
    return "c.tsv @@ (" + " || ".join(parts) + ")"


def _weigh(conn: Connection, groups: list[_Group], sql: _Sql) -> int:
    """Weigh every group by its inverse document frequency in the scope (a word in every document weighs
    almost nothing; a negation weighs as much as an average topic term). Returns the number of documents."""
    params: dict = {}
    cols = ["count(DISTINCT c.document_id) AS n",
            "count(DISTINCT c.document_id) FILTER (WHERE c.kind = 'table_row') AS tables"]
    for gi, g in enumerate(groups):
        if g.kind == "term":
            g.forms = _term_forms(g.word)
            cols.append(f"count(DISTINCT c.document_id) FILTER (WHERE {_any_form(g.forms, f'g{gi}', params)})"
                        f" AS g{gi}")
    row = conn.execute(text("SELECT " + ", ".join(cols) + sql.joins + " WHERE " + sql.where),
                       params | sql.params).one()
    n = row.n
    for gi, g in enumerate(groups):
        if g.kind == "term":
            g.df = getattr(row, f"g{gi}")
        elif g.kind == "table":
            g.forms, g.df = TABLE_WORDS, row.tables
        if g.kind != "negation" and g.df:
            g.weight = math.log((n + 1) / (g.df + 0.5))
    terms = [g.weight for g in groups if g.kind == "term" and g.df]
    for g in groups:
        if g.kind == "negation":
            g.weight = sum(terms) / len(terms) if terms else 0.0
    return n


@dataclass
class _Passage:
    row: object
    supported: list[_Group]
    score: float
    complete: bool  # carries the whole topic
    cohesive: bool = False  # carries the whole topic within one sentence


def _score_passage(row, present: list[_Group], pairs: list[tuple[_Group, _Group]], total: float,
                   places: tuple[str, ...], completable: bool = True) -> _Passage:
    words = _words(row.text, places)  # a word inside a place name ("גן" of "רמת גן") is not the topic
    forms = [_word_forms(w) for w, *_ in words]
    at = {id(g): [i for i, f in enumerate(forms) if f & g.forms] for g in present if g.kind == "term"}
    supported = [g for g in present if g.kind == "term" and at[id(g)]]
    # The sentences each supported group occurs in (a table group holds for the whole passage).
    where = {id(g): {words[i][1] for i in at[id(g)]} for g in supported}
    table = next((g for g in present if g.kind == "table"), None)
    if table is not None and (row.kind == "table_row" or any(f & TABLE_WORDS for f in forms)):
        supported.append(table)
        where[id(table)] = {s for _, s, _ in words}
    negation = next((g for g in present if g.kind == "negation"), None)
    if negation is not None:
        # The negation must govern the term the question negates ("בלי X": X), or, when the
        # question's negation governs no term, a distinctive one (weight at least the mean), never a common
        # word or a neighbouring clause's subject ("יש X ואין Y" does not negate X).
        if any(g.kind == "term" and g.word in negation.governs for g in present):
            targets = [g for g in supported if g.kind == "term" and g.word in negation.governs]
        else:
            mean = sum(g.weight for g in present if g.kind == "term") / max(1, sum(g.kind == "term" for g in present))
            targets = [g for g in supported if g.kind == "term" and g.weight >= mean - 1e-9]
        topic_at = [i for g in targets for i in at[id(g)]]
        governed = {words[j][1] for i, (w, *_) in enumerate(words) if _negation(w)
                    for j in topic_at if _governs(words, i, j)}
        if governed:
            supported.append(negation)
            where[id(negation)] = governed
    share = sum(g.weight for g in supported) / total if total else 0.0
    near = sum(1 for a, b in pairs if any(abs(i - j) <= PHRASE_WINDOW for i in at.get(id(a), [])
                                          for j in at.get(id(b), [])))
    score = share + (PHRASE_WEIGHT * near / len(pairs) if pairs else 0.0)
    complete = completable and share >= COMPLETE_SHARE - 1e-9
    cohesive = complete and any(
        sum(g.weight for g in supported if s in where[id(g)]) / total >= COMPLETE_SHARE - 1e-9
        for s in {s for _, s, _ in words})
    return _Passage(row, supported, score, complete, cohesive)


def locate_evidence(conn: Connection, queries: Sequence[str], *, scope: SearchScope | None = None,
                    filters: MetadataFilters | None = None, place_terms: Iterable[str] = (),
                    limit: int = LOCATE_LIMIT, question: str | None = None) -> LocateOutcome:
    """The documents that mention the question's topic, best first, with their supporting passages.

    The user's own question (``question``) decides the polarity: a question about what is absent keeps only its
    negated variants (an un-negated rephrasing would list every document stating the opposite), a question
    about what is present drops negated ones. Quotation marks around a phrase are dropped first (a quoted
    variant kept its negation word glued to the quote). Query variants are alternatives: a document fully
    supported by any one variant qualifies, so a word only one rephrasing adds ("...לא קיימת") cannot push out
    documents another variant names exactly.
    Each variant is judged alone (terms are never pooled across fully supported variants); a document
    keeps the passages of every variant that fully supports it. When no variant is fully supported
    anywhere, the variants are pooled as one question."""
    queries = [_unquoted(q) for q in queries if q and q.strip()][:MAX_QUERIES]
    place_terms = list(place_terms)
    negated = [q for q in queries if any(g.kind == "negation" for g in _question_groups(q, place_terms))]
    # The user's own question decides the polarity: a question about what is absent keeps only the negated
    # variants (an un-negated one would admit every document stating the opposite: "X" next to "אין X"), and a
    # question about what is present drops a negated variant the rephrasing added ("ללא X" for "X").
    asked_negated = (any(g.kind == "negation" for g in _question_groups(question, place_terms))
                     if question else bool(negated))
    if asked_negated:
        queries = negated or ([question] if question else queries)
    elif negated:
        queries = [q for q in queries if q not in negated] or ([question] if question else queries)
    if len(queries) > 1:
        outcomes = [_locate(conn, [q], scope=scope, filters=filters, place_terms=place_terms, limit=limit)
                    for q in queries]
        full = [o for o in outcomes if any(d.full_support for d in o.documents)]
        if full:
            names = list(dict.fromkeys(w for o in full for w in o.topic_terms))
            return LocateOutcome(_merge_variants(full)[:limit], names, full[0].filter_report)
    return _locate(conn, queries, scope=scope, filters=filters, place_terms=place_terms, limit=limit)


_PHRASE_QUOTES = re.compile(r'(^|\s)["“”„]+|["“”„]+(?=\s|$)')  # never ' : a geresh ends "ג'"


def _unquoted(query: str) -> str:
    """The query without quotation marks around its phrases (gershayim inside a word stay)."""
    return " ".join(_PHRASE_QUOTES.sub(r"\1", query).split())


def _merge_variants(outcomes: list[LocateOutcome]) -> list[RankedDocument]:
    """Every version some variant fully supports, scored by its best variant, with the union of the
    passages of all variants that fully support it (deduplicated, best first)."""
    found: dict[UUID, list[RankedDocument]] = {}
    for o in outcomes:
        for d in o.documents:
            if d.full_support:
                found.setdefault(d.version_id, []).append(d)
    docs: list[RankedDocument] = []
    for variants in found.values():
        best = max(variants, key=lambda d: d.score)  # the earliest variant on a tie
        variants = [best, *(d for d in variants if d is not best)]
        passages: dict = {}
        for d in variants:
            for p in d.passages:
                if p["chunk_id"] not in passages or p["score"] > passages[p["chunk_id"]]["score"]:
                    passages[p["chunk_id"]] = p
        ranked = sorted(passages.values(), key=lambda p: -p["score"])
        matched = list(dict.fromkeys(w for d in variants for w in d.matched_terms))
        docs.append(replace(best, matched_terms=matched,
                            missing_terms=[w for w in best.missing_terms if w not in matched],
                            pages=sorted({pg for p in ranked for pg in p["page_list"]}), passages=ranked))
    return sorted(docs, key=lambda d: (-d.score, str(d.title), str(d.version_id)))


def _locate(conn: Connection, queries: Sequence[str], *, scope: SearchScope | None,
            filters: MetadataFilters | None, place_terms: Iterable[str], limit: int) -> LocateOutcome:
    place_terms = list(place_terms)
    sql, report, _ = _prepare(conn, scope, filters)
    groups: list[_Group] = []
    for q in queries:
        for g in _question_groups(q, place_terms):
            same = next((o for o in groups if o.kind == g.kind and (g.kind != "term" or o.word == g.word)), None)
            if same is None:
                groups.append(g)
            elif g.kind == "negation":  # pooled variants: the negation governs what any of them negates
                same.governs = tuple(dict.fromkeys((*same.governs, *g.governs)))
    names = [g.word for g in groups]
    if sql is None or not any(g.kind == "term" for g in groups):
        return LocateOutcome([], names, report)
    n = _weigh(conn, groups, sql)
    # A question word no document in scope contains: no passage can carry the whole question.
    unfound = [g.word for g in groups if g.kind == "term" and not g.df]
    present = [g for g in groups if g.df or (g.kind == "negation" and g.weight)]
    terms = [g for g in present if g.kind == "term"]
    if not terms:
        return LocateOutcome([], names, report, unfound)
    total = sum(g.weight for g in present)
    first = queries[0]
    order = [g for g in _question_groups(first, place_terms) if g.kind == "term"]
    pairs = [(a, b) for a, b in zip(order, order[1:], strict=False) if b.position - a.position == 1]
    pairs = [(next(g for g in terms if g.word == a.word), next(g for g in terms if g.word == b.word))
             for a, b in pairs if any(g.word == a.word for g in terms) and any(g.word == b.word for g in terms)]
    params: dict = {}
    match = [_any_form(g.forms, f"m{i}", params) for i, g in enumerate(terms)]
    rows = conn.execute(
        text("SELECT c.id, c.document_id, c.version_id, c.page_list, c.section, c.text, c.kind, c.table_index,"
             f" c.row_index, c.chunk_index, d.title{sql.joins} WHERE {sql.where} AND ({' OR '.join(match)})"
             f" ORDER BY ({' + '.join(f'({m})::int' for m in match)}) DESC, c.version_id, c.chunk_index LIMIT :lim"),
        params | sql.params | {"lim": LOCATE_CANDIDATES},
    ).all()
    places = (*place_terms, *KNOWN_CITIES)
    by_version: dict = {}
    for r in rows:
        p = _score_passage(r, present, pairs, total, places, completable=not unfound)
        if p.supported:
            by_version.setdefault(r.version_id, []).append(p)
    if unfound:
        # Only passages naming a word that tells documents apart can be about the question; one that
        # shares only words most documents in scope carry ("בניין", the scope's own address) is not.
        by_version = {v: kept for v, ps in by_version.items()
                      if (kept := [p for p in ps if any(g.kind == "term" and g.df <= DISTINCTIVE_DF_SHARE * n
                                                        for g in p.supported)])}
    if not by_version:
        return LocateOutcome([], names, report, unfound)
    best = {v: max(ps, key=lambda p: p.score) for v, ps in by_version.items()}
    # Tiers, best first: the whole topic in one sentence, in one passage, part of it. Only documents of
    # the best tier present count (the phrase bonus ranks within a tier, never across).
    for tier in (lambda p: p.cohesive, lambda p: p.complete):
        if any(tier(p) for ps in by_version.values() for p in ps):
            best = {v: max((p for p in ps if tier(p)), key=lambda p: p.score)
                    for v, ps in by_version.items() if any(tier(p) for p in ps)}
            break
    top = max(b.score for b in best.values())
    absent = [g.word for g in groups if g not in present]
    tokens = sorted({f for g in terms for f in g.forms})
    out: list[RankedDocument] = []
    for v, b in sorted(best.items(), key=lambda kv: (-kv[1].score, str(kv[1].row.title), str(kv[0]))):
        # The threshold trims partial matches only: a fully supported document is never dropped because
        # another one's words happen to stand next to each other (the phrase bonus ranks, never excludes).
        if not b.complete and b.score < RELATIVE_THRESHOLD * top:
            continue
        # Complete passages first (within one sentence first), then partial ones close to the document's best.
        chosen = sorted((p for p in by_version[v] if p.complete or p.score >= RELATIVE_THRESHOLD * b.score),
                        key=lambda p: (not p.cohesive, not p.complete, -p.score, p.row.chunk_index)
                        )[:PASSAGES_PER_DOCUMENT]
        pages = sorted({pg for p in chosen for pg in (p.row.page_list or [])})
        matched = [g.word for g in present if g in b.supported]
        out.append(RankedDocument(
            document_id=b.row.document_id, version_id=v, title=b.row.title, score=round(b.score, 4),
            full_support=b.complete and not absent, matched_terms=matched,
            missing_terms=[g.word for g in groups if g.word not in matched],
            pages=pages, passages=[_hit(p.row, p.score, True, tokens) for p in chosen]))
    return LocateOutcome(out[:limit], names, report, unfound)


def locate_documents(conn: Connection, queries: Sequence[str], *, scope: SearchScope | None = None,
                     filters: MetadataFilters | None = None, place_terms: Iterable[str] = (),
                     limit: int = LOCATE_LIMIT) -> list[RankedDocument]:
    """Documents only; ``locate_evidence`` also returns the question terms and the filter report."""
    return locate_evidence(conn, queries, scope=scope, filters=filters, place_terms=place_terms,
                           limit=limit).documents


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
        hits = search_passages(conn, q, limit)
    return {"results": [{"chunk_id": str(h["chunk_id"]), "document_id": str(h["document_id"]),
                         "version_id": str(h["version_id"]), "title": h["title"], "page_list": h["page_list"],
                         "section": h["section"], "snippet": h["snippet"], "text": h["text"], "kind": h["kind"],
                         "score": h["score"]} for h in hits]}
