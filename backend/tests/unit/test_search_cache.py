"""Search latency reuse (round 9 U2, R16, KTD3): a turn does not repeat an identical search, a process does not
re-embed a query it has embedded, and the embedding model is loaded once at startup without failing it.
Scripted providers and a scripted search; synthetic text only."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.chat import tools as T
from app.platform.search import SearchScope
from app.providers import embeddings as E


class CountingProvider:
    """Embeds by length; counts the calls."""

    model_id = "counting"
    dim = 2

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text)), 1.0]


class Searches:
    """A scripted ``search_passages``: counts its calls and, like the real one, embeds its query."""

    def __init__(self, monkeypatch, provider: CountingProvider) -> None:
        self.calls: list[tuple] = []
        self.provider = provider
        self.version = 1
        monkeypatch.setattr(T, "search_passages", self)
        monkeypatch.setattr(T, "current_data_version", lambda conn: self.version)

    def __call__(self, conn, query, limit, *, scope=None):
        self.calls.append((query, limit, scope))
        self.provider.embed_query(query)
        return [{"chunk_id": f"c{len(self.calls)}", "text": query}]


def _scripted(monkeypatch):
    provider = CountingProvider()
    return provider, Searches(monkeypatch, provider)


# --- the per-turn memo of identical searches -----------------------------------------------------------------

def test_an_identical_second_search_runs_no_search_and_no_embedding(monkeypatch):
    provider, searches = _scripted(monkeypatch)
    ws = T.Workspace(ctx=None)
    scope = SearchScope(document_ids=(uuid4(),))
    first = T.memoized_search(ws, None, "שאילתה", 8, scope)
    second = T.memoized_search(ws, None, "שאילתה", 8, scope)
    assert first == second
    assert len(searches.calls) == 1 and provider.calls == ["שאילתה"]


def test_the_memo_hands_out_copies(monkeypatch):
    _, searches = _scripted(monkeypatch)
    ws = T.Workspace(ctx=None)
    first = T.memoized_search(ws, None, "שאילתה", 8, None)
    first[0]["text"] = "changed"
    first.append({"chunk_id": "extra"})
    again = T.memoized_search(ws, None, "שאילתה", 8, None)
    assert again == [{"chunk_id": "c1", "text": "שאילתה"}] and len(searches.calls) == 1


def test_another_scope_query_limit_or_data_version_misses(monkeypatch):
    _, searches = _scripted(monkeypatch)
    ws = T.Workspace(ctx=None)
    d1, d2 = uuid4(), uuid4()
    T.memoized_search(ws, None, "שאילתה", 8, SearchScope(document_ids=(d1,)))
    T.memoized_search(ws, None, "שאילתה", 8, SearchScope(document_ids=(d2,)))  # another scope
    T.memoized_search(ws, None, "שאילתה", 8, None)  # open scope
    T.memoized_search(ws, None, "שאילתה אחרת", 8, None)  # another query
    T.memoized_search(ws, None, "שאילתה", 12, None)  # another limit
    assert len(searches.calls) == 5
    searches.version = 2  # the office's data changed mid-turn: nothing from before is reused
    T.memoized_search(ws, None, "שאילתה", 8, SearchScope(document_ids=(d1,)))
    assert len(searches.calls) == 6


def test_the_order_of_the_scope_documents_does_not_matter(monkeypatch):
    _, searches = _scripted(monkeypatch)
    ws = T.Workspace(ctx=None)
    d1, d2 = uuid4(), uuid4()
    T.memoized_search(ws, None, "שאילתה", 8, SearchScope(document_ids=(d1, d2)))
    T.memoized_search(ws, None, "שאילתה", 8, SearchScope(document_ids=(d2, d1)))
    assert len(searches.calls) == 1


def test_a_call_in_a_time_mode_and_a_call_without_one_do_not_share_an_entry_and_a_unit_scope_is_its_own(monkeypatch):
    """Round 15 U2, KTD10: the key carries the turn's time mode and the unit the call names (its block range), so a search
    of the same words and version answers each as asked."""
    from app.chat.timeframe import TimeMode

    _, searches = _scripted(monkeypatch)
    ws = T.Workspace(ctx=None)
    version = uuid4()
    whole, unit = SearchScope(version_ids=(version,)), SearchScope(version_ids=(version,), blocks=(2, 5))
    T.memoized_search(ws, None, "שאילתה", 8, unit)
    T.memoized_search(ws, None, "שאילתה", 8, unit)
    assert len(searches.calls) == 1
    ws.time = TimeMode("recent", "switch", ws.time.anchor, None, True)  # the same query, now in a time mode
    T.memoized_search(ws, None, "שאילתה", 8, unit)
    assert len(searches.calls) == 2
    T.memoized_search(ws, None, "שאילתה", 8, whole)  # the whole file is not the unit
    assert len(searches.calls) == 3


def test_each_turn_has_its_own_memo(monkeypatch):
    _, searches = _scripted(monkeypatch)
    T.memoized_search(T.Workspace(ctx=None), None, "שאילתה", 8, None)
    T.memoized_search(T.Workspace(ctx=None), None, "שאילתה", 8, None)
    assert len(searches.calls) == 2


# --- the per-process query-embedding cache -------------------------------------------------------------------

class FakeModel:
    def __init__(self) -> None:
        self.encoded: list[list[str]] = []

    def encode(self, texts, **kw):
        self.encoded.append(list(texts))

        class Vec(list):
            def tolist(self):
                return list(self)

        return [Vec([float(len(t)), 0.5]) for t in texts]


def _local() -> tuple[E.LocalEmbedding, FakeModel]:
    provider = E.LocalEmbedding("fake-model", 2)
    provider._model = FakeModel()
    return provider, provider._model


def test_a_query_is_embedded_once_per_process_whatever_its_spacing():
    provider, model = _local()
    a = provider.embed_query("מה השווי של הנכס")
    b = provider.embed_query("  מה  השווי של\tהנכס ")
    assert a == b and model.encoded == [["query: מה השווי של הנכס"]]
    provider.embed_query("שאלה אחרת")
    assert len(model.encoded) == 2


def test_the_query_cache_hands_out_copies():
    provider, _ = _local()
    first = provider.embed_query("שאלה")
    first[0] = -1.0
    assert provider.embed_query("שאלה")[0] == float(len("query: שאלה"))


def test_the_query_cache_drops_the_least_recently_used(monkeypatch):
    monkeypatch.setattr(E, "QUERY_CACHE_SIZE", 2)
    provider, model = _local()
    provider.embed_query("אחת")
    provider.embed_query("שתיים")
    provider.embed_query("אחת")  # touched: "שתיים" is now the oldest
    provider.embed_query("שלוש")  # evicts "שתיים"
    n = len(model.encoded)
    provider.embed_query("אחת")
    assert len(model.encoded) == n
    provider.embed_query("שתיים")
    assert len(model.encoded) == n + 1


def test_passages_are_never_cached():
    provider, model = _local()
    provider.embed_passages(["קטע"])
    provider.embed_passages(["קטע"])
    assert len(model.encoded) == 2


def test_the_hash_provider_caches_its_queries_too():
    provider = E.HashEmbedding(8)
    calls = []
    original = provider._vec
    provider._vec = lambda t: (calls.append(t), original(t))[1]
    assert provider.embed_query("שאלה  אחת") == provider.embed_query("שאלה אחת")
    assert len(calls) == 1


# --- the start-up warm-up -------------------------------------------------------------------------------------

def test_warmup_loads_the_model_and_runs_one_encode():
    provider, model = _local()
    provider.warmup()
    assert len(model.encoded) == 1
    provider.embed_query(E.WARMUP_QUERY)  # the warm-up query is cached: the first real search does not repeat it
    assert len(model.encoded) == 1


def test_start_up_skips_the_hash_provider(monkeypatch):
    class Boom(E.HashEmbedding):
        def warmup(self):
            raise AssertionError("the hash provider has nothing to load")

    monkeypatch.setattr(E, "get_embedding_provider", lambda: Boom(8))
    monkeypatch.setattr(E, "get_settings", lambda: SimpleNamespace(embedding_provider="hash"))
    assert E.warm_up_embeddings() is False


def test_start_up_warms_the_local_provider(monkeypatch):
    provider, model = _local()
    monkeypatch.setattr(E, "get_embedding_provider", lambda: provider)
    monkeypatch.setattr(E, "get_settings", lambda: SimpleNamespace(embedding_provider="local"))
    assert E.warm_up_embeddings() is True and len(model.encoded) == 1
