"""The search measurement of round 22 U0 (R10, R25): each ``search_evidence`` call inside ``measure_searches`` records the
lexical candidates its stage returned, the candidates fused, the depth asked for and its seconds; the chat's
``memoized_search`` puts them on the next tool record of the turn's stage clock; and none of it changes the hits.
The stages are replaced by a recording fake (no database); synthetic text only."""

from __future__ import annotations

from uuid import uuid4

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
