"""Embedding providers (KTD10). Local by default: a cloud embedding API would send document
text out of the office, which is off by default.

- ``LocalEmbedding``: sentence-transformers on CPU, default intfloat/multilingual-e5-small (MIT,
  384 dims). e5 models expect "query: " / "passage: " prefixes.
- ``HashEmbedding``: deterministic hashed bag-of-tokens. NOT semantic; for tests and fast CI only.

A query is embedded once per process (``QueryCache``, round 9 R16, KTD3), and the local model is loaded and run once at
start-up (``warm_up_embeddings``) so the first search does not pay for it. Passages are never cached.
"""

from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache
from typing import Protocol

from app.config import get_settings
from app.extraction.normalize_text import normalize_for_search

QUERY_CACHE_SIZE = 256
WARMUP_QUERY = "warm up"


class QueryCache:
    """Least-recently-used vectors of queries, keyed by the query with its whitespace collapsed (the text the model
    reads is otherwise the same). Callers get copies, so a vector altered downstream never changes the cache."""

    def __init__(self) -> None:
        self._vectors: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, query: str, embed: Callable[[str], list[float]]) -> list[float]:
        key = " ".join(query.split())
        with self._lock:
            vec = self._vectors.get(key)
            if vec is not None:
                self._vectors.move_to_end(key)
                return list(vec)
        vec = embed(query)
        with self._lock:
            self._vectors[key] = list(vec)
            self._vectors.move_to_end(key)
            while len(self._vectors) > QUERY_CACHE_SIZE:
                self._vectors.popitem(last=False)
        return list(vec)


def to_pgvector(vec: list[float]) -> str:
    """pgvector text literal; used for both indexing and querying so the two never disagree."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


class EmbeddingProvider(Protocol):
    model_id: str
    dim: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...
    def warmup(self) -> None: ...


class HashEmbedding:
    """Signed feature hashing over normalized tokens and character trigrams."""

    def __init__(self, dim: int):
        self.dim = dim
        self.model_id = f"hash-v1-{dim}"
        self._queries = QueryCache()

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = normalize_for_search(text).split()
        feats = list(words)
        for word in words:
            padded = f" {word} "
            feats.extend(padded[i:i + 3] for i in range(len(padded) - 2))
        for f in feats:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            vec[idx] += 1.0 if h[4] & 1 else -1.0
        length = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / length for v in vec]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._queries.get(text, self._vec)

    def warmup(self) -> None:
        return None


class LocalEmbedding:
    def __init__(self, model_name: str, dim: int):
        self.model_name = model_name
        self.dim = dim
        self.model_id = model_name
        self._model = None
        self._lock = threading.Lock()
        self._queries = QueryCache()

    def _load(self):
        with self._lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.model_name, device="cpu")
                got = self._model.get_sentence_embedding_dimension()
                if got != self.dim:
                    raise RuntimeError(f"embedding model {self.model_name} has dim {got}, expected {self.dim}")
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._load().encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode([f"passage: {t}" for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._queries.get(text, lambda q: self._encode([f"query: {q}"])[0])

    def warmup(self) -> None:
        """Load the model and run one encode, so the first search pays neither the load nor the first-call setup."""
        self._load()
        self.embed_query(WARMUP_QUERY)


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_provider == "hash":
        return HashEmbedding(settings.embedding_dim)
    if settings.embedding_provider == "local":
        return LocalEmbedding(settings.embedding_model, settings.embedding_dim)
    raise RuntimeError(f"unknown EMBEDDING_PROVIDER {settings.embedding_provider!r}")


def warm_up_embeddings() -> bool:
    """Warm the local model at start-up, so the first search pays neither the model load nor the first encode. The
    hash provider has nothing to load and is skipped. A model that cannot load still stops the start-up, as before.
    True when it was warmed."""
    if get_settings().embedding_provider != "local":
        return False
    get_embedding_provider().warmup()
    return True
