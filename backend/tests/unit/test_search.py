"""The search measurement of round 22 U0 (R10, R25): each ``search_evidence`` call inside ``measure_searches`` records the
lexical candidates its stage returned, the candidates fused, the depth asked for and its seconds; the chat's
``memoized_search`` puts them on the next tool record of the turn's stage clock; and none of it changes the hits.
The stages are replaced by a recording fake (no database); synthetic text only."""

from __future__ import annotations

import math
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.chat import tools as T
from app.chat.timing import StageClock
from app.platform import search as S
from app.platform.search import SearchScope, measure_searches


class FakeStages:
    """Stands in for the database stages of ``search_evidence``: records what each stage was asked for and returns scripted
    ranked chunk ids."""

    def __init__(self, monkeypatch, lexical: list[str], fuzzy: list[str], semantic: list[str]) -> None:
        self.asked: list[tuple[str, int]] = []
        self.lexical, self.fuzzy, self.semantic = lexical, fuzzy, semantic
        monkeypatch.setattr(S, "_prepare", lambda conn, scope, filters: (S._Sql(version_ids=[]), None, None))
        monkeypatch.setattr(S, "_lexical", self._stage("lexical", lambda: self.lexical))
        monkeypatch.setattr(S, "_trigram", self._stage("trigram", lambda: self.fuzzy))
        monkeypatch.setattr(S, "_semantic", self._stage("semantic", lambda: self.semantic))
        monkeypatch.setattr(S, "_supported", lambda conn, ranked, topic: set(ranked))
        monkeypatch.setattr(S, "_chunk_rows", lambda conn, sql, ids: {cid: {"id": cid} for cid in ids})
        monkeypatch.setattr(S, "_hit", lambda row, score, supported, tokens: {"chunk_id": row["id"], "score": score,
                                                                              "kind": "text", "version_id": "v"})

    def _stage(self, name: str, ids):
        def stage(conn, *args):
            self.asked.append((name, args[-1]))
            return list(ids())
        return stage


def test_a_search_records_its_lexical_and_fused_candidates_and_seconds(monkeypatch):
    fake = FakeStages(monkeypatch, lexical=["a", "b", "c"], fuzzy=["b", "d"], semantic=["e"])
    with measure_searches() as ran:
        out = S.search_evidence(None, ["שאילתה אחת"], 2)
    assert len(out.hits) == 2
    (rec,) = ran
    assert rec["lexical"] == 3 and rec["fused"] == 5 and rec["depth"] == S.CANDIDATES and rec["queries"] == 1
    assert isinstance(rec["seconds"], float) and rec["seconds"] >= 0.0
    assert ("lexical", S.CANDIDATES) in fake.asked  # the stage was asked for the candidate depth


def test_the_lexical_count_is_the_candidates_returned_over_all_queries(monkeypatch):
    FakeStages(monkeypatch, lexical=["a", "b"], fuzzy=[], semantic=[])
    with measure_searches() as ran:
        S.search_evidence(None, ["שאילתה אחת", "שאילתה שנייה"], 8)
    assert ran[0]["lexical"] == 4 and ran[0]["fused"] == 2 and ran[0]["queries"] == 2


def test_a_search_with_no_scope_to_search_records_zero_candidates(monkeypatch):
    monkeypatch.setattr(S, "_prepare", lambda conn, scope, filters: (None, None, None))
    with measure_searches() as ran:
        assert S.search_evidence(None, ["שאילתה"], 8).hits == []
    assert ran[0]["lexical"] == 0 and ran[0]["fused"] == 0


def test_hits_are_the_same_with_and_without_the_measurement(monkeypatch):
    FakeStages(monkeypatch, lexical=["a", "b", "c"], fuzzy=["c", "a"], semantic=["d", "a"])
    plain = S.search_evidence(None, ["שאילתה אחת"], 3).hits
    with measure_searches():
        measured = S.search_evidence(None, ["שאילתה אחת"], 3).hits
    assert plain == measured and plain


def test_a_search_outside_the_measurement_records_nothing(monkeypatch):
    FakeStages(monkeypatch, lexical=["a"], fuzzy=[], semantic=[])
    S.search_evidence(None, ["שאילתה"], 8)
    with measure_searches() as ran:
        pass
    assert ran == []


# --- the chat: the record reaches the tool call's record on the stage clock ----------------------------------------------

def test_a_chat_search_puts_its_candidates_and_seconds_on_the_tool_record(monkeypatch):
    FakeStages(monkeypatch, lexical=["a", "b", "c"], fuzzy=["a"], semantic=["d"])
    monkeypatch.setattr(T, "current_data_version", lambda conn: 1)
    scope = SearchScope(version_ids=(uuid4(),), blocks=(1, 9))  # inside a unit: no document is named by its title (no database)
    ws = T.Workspace(ctx=None)
    ws.clock = StageClock()

    def search(ws_, args):
        T.memoized_search(ws_, None, "שאילתה", 8, scope)
        T.memoized_search(ws_, None, "שאילתה", 8, scope)  # the turn's memo answers it: no search
        return "תוצאה"

    monkeypatch.setattr(T, "HANDLERS", {"search": search})
    T.run_tool(ws, "search", '{"query": "שאילתה"}')
    (record,) = ws.clock.tools
    got = record["search"]
    assert got["run"] == 1 and got["memoized"] == 1 and len(got["calls"]) == 1
    call = got["calls"][0]
    assert call["lexical"] == 3 and call["fused"] == 4 and call["depth"] == S.CANDIDATES and call["evidence_calls"] == 1
    assert call["seconds"] >= 0.0
    # the next tool call starts a record of its own
    monkeypatch.setattr(T, "HANDLERS", {"search": lambda ws_, args: "ריק", "read": lambda ws_, args: "ריק"})
    T.run_tool(ws, "read", "{}")
    assert "search" not in ws.clock.tools[-1]
    assert ws.clock.summary()["tools"][0]["search"]["calls"][0]["lexical"] == 3


# --- round 22 U2 (R10): the lexical stage honours its candidate limit ----------------------------------------------------

class _Rows:
    def __init__(self, one=None, ids=()) -> None:
        self._one, self._ids = one, list(ids)

    def one(self):
        return self._one

    def scalars(self):
        return iter(self._ids)


class RecordingConn:
    """A connection that answers the two statements of ``_lexical``: the IDF statistics (``pool`` passages, every term in
    ``df`` of them) and the candidate statement, which returns the first ``:n`` of ``matches`` as ``LIMIT :n`` does. It
    records every statement with its parameters."""

    def __init__(self, pool: int, matches: int | None = None, df: int = 3) -> None:
        self.pool, self.df = pool, df
        self.matching = [f"c{i}" for i in range(pool if matches is None else matches)]
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        sql, params = str(stmt), dict(params or {})
        self.calls.append((sql, params))
        if "count(*) AS n" in sql:
            cols = {"n": self.pool} | {f"d{i}": self.df for i in range(len(params))}
            return _Rows(one=SimpleNamespace(**cols))
        return _Rows(ids=self.matching[:params["n"]] if "LIMIT :n" in sql else self.matching)

    def limits(self) -> list[int]:
        return [p["n"] for sql, p in self.calls if "LIMIT :n" in sql]


TOKENS = ["אחד", "שניים"]


def test_a_limit_of_40_over_a_pool_of_10000_binds_40():
    conn = RecordingConn(pool=10_000)
    got = S._lexical(conn, TOKENS, S._Sql(), 40)
    assert conn.limits() == [40]  # was 10000: the IDF passage count overwrote the limit
    assert len(got) == 40 and got == [f"c{i}" for i in range(40)]


def test_the_idf_weights_still_come_from_the_pool_size():
    conn = RecordingConn(pool=10_000, df=500)
    S._lexical(conn, TOKENS, S._Sql(), 40)
    params = next(p for sql, p in conn.calls if "LIMIT :n" in sql)
    assert params["w0"] == pytest.approx(math.log(10_001 / 500.5))
    assert params["w1"] == pytest.approx(params["w0"])


@pytest.mark.parametrize(("pool", "limit", "returned"), [(40, 40, 40), (25, 40, 25), (1, 40, 1), (1000, 1, 1)])
def test_a_limit_equal_to_or_above_the_pool_returns_the_matches_only(pool, limit, returned):
    conn = RecordingConn(pool=pool)
    assert len(S._lexical(conn, TOKENS, S._Sql(), limit)) == returned
    assert conn.limits() == [limit]  # the limit is the request, not the pool


def test_an_empty_pool_keeps_the_limit_and_returns_nothing():
    conn = RecordingConn(pool=0, df=0)
    assert S._lexical(conn, TOKENS, S._Sql(), 40) == []
    assert conn.limits() == [40]
    params = next(p for sql, p in conn.calls if "LIMIT :n" in sql)
    assert params["w0"] == pytest.approx(math.log(2 / 0.5))  # an empty pool weighs as a pool of one


def test_a_small_scope_keeps_its_200_limit():
    conn = RecordingConn(pool=10_000)
    assert len(S._lexical(conn, TOKENS, S._Sql(), S.SMALL_SCOPE_CANDIDATES)) == 200
    assert conn.limits() == [200]
    small = RecordingConn(pool=50)  # a pool of fewer than 200 passages returns them all, not a pool-sized bind of 50
    assert len(S._lexical(small, TOKENS, S._Sql(), S.SMALL_SCOPE_CANDIDATES)) == 50
    assert small.limits() == [200]


def test_a_search_in_a_small_scope_asks_the_stage_for_200_and_otherwise_40(monkeypatch):
    fake = FakeStages(monkeypatch, lexical=["a", "b"], fuzzy=[], semantic=[])
    monkeypatch.setattr(S, "_small_scope_order", lambda ranked, rows, supported: (ranked, 0))
    for documents, depth in [(2, S.SMALL_SCOPE_CANDIDATES), (S.SMALL_SCOPE_DOCUMENTS + 1, S.CANDIDATES), (None, S.CANDIDATES)]:
        monkeypatch.setattr(S, "_prepare", lambda conn, scope, filters, d=documents: (S._Sql(version_ids=[]), None, d))
        fake.asked.clear()
        with measure_searches() as ran:
            S.search_evidence(None, ["שאילתה אחת"], 8)
        assert ("lexical", depth) in fake.asked and ran[0]["depth"] == depth


def test_an_all_stopword_query_has_no_tokens_and_the_stage_runs_no_statement():
    conn = RecordingConn(pool=10_000)
    assert S._lexical(conn, S.query_tokens("של את על"), S._Sql(), 40) == [] and conn.calls == []


def test_an_all_stopword_search_records_zero_lexical_candidates(monkeypatch):
    stopwords = "של את על"
    assert S.query_tokens(stopwords) == []
    conn = RecordingConn(pool=10_000)
    monkeypatch.setattr(S, "_prepare", lambda c, scope, filters: (S._Sql(version_ids=[]), None, None))
    monkeypatch.setattr(S, "_trigram", lambda c, q, sql, n: [])
    monkeypatch.setattr(S, "_semantic", lambda c, q, sql, n: [])
    with measure_searches() as ran:
        assert S.search_evidence(conn, [stopwords], 8).hits == []
    assert ran[0]["lexical"] == 0 and conn.limits() == []


def test_the_search_records_the_candidates_the_limit_allowed_not_the_pool(monkeypatch):
    conn = RecordingConn(pool=10_000)
    monkeypatch.setattr(S, "_prepare", lambda c, scope, filters: (S._Sql(version_ids=[]), None, None))
    monkeypatch.setattr(S, "_trigram", lambda c, q, sql, n: [])
    monkeypatch.setattr(S, "_semantic", lambda c, q, sql, n: [])
    monkeypatch.setattr(S, "_supported", lambda c, ranked, topic: set(ranked))
    monkeypatch.setattr(S, "_chunk_rows", lambda c, sql, ids: {cid: {"id": cid} for cid in ids})
    monkeypatch.setattr(S, "_hit", lambda row, score, supported, tokens: {"chunk_id": row["id"]})
    with measure_searches() as ran:
        out = S.search_evidence(conn, ["מחיר דירה"], 8)
    assert ran[0]["lexical"] == S.CANDIDATES and ran[0]["fused"] == S.CANDIDATES and ran[0]["depth"] == S.CANDIDATES
    assert len(out.hits) == 8 and conn.limits() == [S.CANDIDATES]


def test_the_lexical_statement_breaks_ties_by_chunk_index_then_id():
    conn = RecordingConn(pool=10)
    S._lexical(conn, TOKENS, S._Sql(), 5)
    sql = next(s for s, _ in conn.calls if "LIMIT :n" in s)
    assert sql.endswith("c.chunk_index, c.id LIMIT :n")


def test_the_fused_ranking_is_deterministic_and_ties_keep_the_stage_order(monkeypatch):
    # two chunks tie on every stage's score: the stage order (SQL: chunk index, id) is what orders them, the same on every run
    FakeStages(monkeypatch, lexical=["a", "b", "c"], fuzzy=["b", "a"], semantic=["c", "b", "a"])
    runs = [[h["chunk_id"] for h in S.search_evidence(None, ["שאילתה אחת"], 3).hits] for _ in range(5)]
    assert all(r == runs[0] for r in runs) and sorted(runs[0]) == ["a", "b", "c"]
    FakeStages(monkeypatch, lexical=["x", "y"], fuzzy=["y", "x"], semantic=[])  # equal fused scores for x and y
    tied = [h["chunk_id"] for h in S.search_evidence(None, ["שאילתה אחת"], 2).hits]
    assert tied == ["x", "y"] and [h["chunk_id"] for h in S.search_evidence(None, ["שאילתה אחת"], 2).hits] == tied
