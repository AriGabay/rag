"""Embedding providers (KTD10). Local by default: a cloud embedding API would send document
text out of the office, which is off by default.

- ``LocalEmbedding``: sentence-transformers on CPU, default intfloat/multilingual-e5-small (MIT,
  384 dims). e5 models expect "query: " / "passage: " prefixes.
- ``HashEmbedding``: deterministic hashed bag-of-tokens. NOT semantic; for tests and fast CI only.
"""

from __future__ import annotations

import hashlib
import math
import threading
from functools import lru_cache
from typing import Protocol

from app.config import get_settings
from app.extraction.normalize_text import normalize_for_search


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

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = normalize_for_search(text)
        feats = norm.split()
        for word in norm.split():
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
        return self._vec(text)

    def warmup(self) -> None:
        return None


class LocalEmbedding:
    def __init__(self, model_name: str, dim: int):
        self.model_name = model_name
        self.dim = dim
        self.model_id = model_name
        self._model = None
        self._lock = threading.Lock()

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
        return self._encode([f"query: {text}"])[0]

    def warmup(self) -> None:
        self._load()


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_provider == "hash":
        return HashEmbedding(settings.embedding_dim)
    if settings.embedding_provider == "local":
        return LocalEmbedding(settings.embedding_model, settings.embedding_dim)
    raise RuntimeError(f"unknown EMBEDDING_PROVIDER {settings.embedding_provider!r}")
