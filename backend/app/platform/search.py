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

from app.answering.metadata import FilterReport, MetadataFilters, apply_filters, version_metadata
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
    return conds, params


def _scope_sql(scope: SearchScope | None, allowed_versions: list[UUID] | None = None) -> _Sql:
    conds, params = _scope_conditions(scope, "c.version_id", "c.document_id")
    if allowed_versions is not None:
        params["scope_allowed"] = allowed_versions
        conds.append("c.version_id = ANY(:scope_allowed)")
    return _Sql(where=" AND ".join(conds), params=params)


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
        params | weights | scope.params | {"n": n},
    ).scalars())


def _trigram(conn: Connection, normalized_query: str, scope: _Sql, n: int = CANDIDATES) -> list:
    return list(conn.execute(
        text(f"SELECT c.id{scope.joins} WHERE {scope.where} AND word_similarity(:q, c.normalized_text) > :th"
             " ORDER BY word_similarity(:q, c.normalized_text) DESC LIMIT :n"),
        scope.params | {"q": normalized_query, "th": TRIGRAM_THRESHOLD, "n": n},
    ).scalars())


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
        return _scope_sql(scope), None, None
    versions = _scoped_versions(conn, scope)
    report, allowed = None, None
    if filters is not None and filters.active:
        report = apply_filters(version_metadata(conn, [v for v, _ in versions]), filters)
        allowed = report.matched
        if not allowed:
            return None, report, 0
        versions = [(v, d) for v, d in versions if v in set(allowed)]
    documents = len({d for _, d in versions}) if narrowed else None
    return _scope_sql(scope, allowed), report, documents


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
                    filters: MetadataFilters | None = None, place_terms: Iterable[str] = ()) -> SearchOutcome:
    """Fused search over up to ``MAX_QUERIES`` queries (the first is the primary question). In a small
    scope the result may hold more than ``limit`` hits (at most twice), so that every page's best
    supported passage is in it; when the pages need more than that, the best ``limit`` lead."""
    queries = [q for q in queries if q and q.strip()][:MAX_QUERIES]
    place_terms = list(place_terms)
    topic: list[str] = []
    for q in queries:
        topic.extend(t for t in topic_tokens(q, place_terms) if t not in topic)
    sql, report, documents = _prepare(conn, scope, filters)
    if sql is None:
        return SearchOutcome([], topic, report)
    small = documents is not None and documents <= SMALL_SCOPE_DOCUMENTS
    n = SMALL_SCOPE_CANDIDATES if small else CANDIDATES
    scores: dict = {}
    all_tokens: list[str] = []
    guaranteed: list = []
    for qi, q in enumerate(queries):
        tokens = query_tokens(q)
        all_tokens.extend(t for t in tokens if t not in all_tokens)
        # Every passage of a small scope shares its place words: rank by the topic alone.
        ranking = (query_tokens(_strip_places(base_normalize(q), [*place_terms, *KNOWN_CITIES])) if small
                   else tokens) or tokens
        lexical = _lexical(conn, ranking, sql, n)
        fuzzy = _trigram(conn, _alpha(ranking), sql, n) if ranking else []
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
_CLAUSE_BREAK = re.compile(r"[,;|()\[\]—–]|\s-\s")


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
        return (len(gap) <= NEGATION_GAP and sum(not _light(w) for w in gap) <= 1
                and not any(_blocks(w) for w in gap))
    return (i == j + 1 and words[i][0] in NEGATION_WORDS and words[i][0] not in PREPOSED_NEGATIONS
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
                    limit: int = LOCATE_LIMIT) -> LocateOutcome:
    """The documents that mention the question's topic, best first, with their supporting passages.

    A question about what is absent keeps only its negated variants: an un-negated rephrasing would list every
    document stating the opposite. Quotation marks around a phrase are dropped first (a quoted variant kept its
    negation word glued to the quote). Query variants are alternatives: a document fully supported by any one
    variant qualifies, so a word
    only one rephrasing adds ("...לא קיימת") cannot push out documents another variant names exactly.
    Each variant is judged alone (terms are never pooled across fully supported variants); a document
    keeps the passages of every variant that fully supports it. When no variant is fully supported
    anywhere, the variants are pooled as one question."""
    queries = [_unquoted(q) for q in queries if q and q.strip()][:MAX_QUERIES]
    place_terms = list(place_terms)
    negated = [q for q in queries if any(g.kind == "negation" for g in _question_groups(q, place_terms))]
    if negated:
        # A question about what is absent: a variant without the negation asks about what is present and would
        # admit every document that states it ("X" next to "אין X"). Only the negated variants count.
        queries = negated
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
        hits = hybrid_search(conn, q, limit)
    return {"results": [{"chunk_id": str(h["chunk_id"]), "document_id": str(h["document_id"]),
                         "version_id": str(h["version_id"]), "title": h["title"], "page_list": h["page_list"],
                         "section": h["section"], "snippet": h["snippet"], "score": h["score"]} for h in hits]}
