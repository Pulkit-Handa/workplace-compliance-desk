from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from compliance.config import settings
from compliance.observability.metrics import CACHE_OPS
from compliance.util import tokenize


def _stable_hash(feature: str, dim: int) -> tuple[int, int]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


class HashingEmbedder:
    """Deterministic local feature hashing.

    No download, no API, identical vectors in every container.  It is a lexical
    embedder (words, bigrams and character n-grams) rather than a neural one;
    the trade-off is documented in docs/retrieval.md.  The interface is the same
    as the optional MiniLM embedder, so swapping is a configuration change.
    """

    name = "hashing"
    dim = settings.embedding_dim

    def __init__(self, dim: int | None = None):
        self.dim = dim or settings.embedding_dim

    @property
    def fingerprint(self) -> str:
        return f"hashing:v1:{self.dim}"

    def _features(self, text: str) -> dict[str, float]:
        tokens = tokenize(text)
        counts: dict[str, float] = {}
        for token in tokens:
            counts[f"w:{token}"] = counts.get(f"w:{token}", 0.0) + 1.0
        for left, right in zip(tokens, tokens[1:], strict=False):
            feature = f"b:{left}_{right}"
            counts[feature] = counts.get(feature, 0.0) + 0.7
        for token in tokens:
            if len(token) >= 5:
                padded = f"<{token}>"
                for i in range(len(padded) - 3):
                    feature = f"c:{padded[i:i + 4]}"
                    counts[feature] = counts.get(feature, 0.0) + 0.25
        return counts

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for feature, count in self._features(text).items():
                index, sign = _stable_hash(feature, self.dim)
                matrix[row, index] += sign * (1.0 + math.log(count))
            norm = float(np.linalg.norm(matrix[row]))
            if norm > 0:
                matrix[row] /= norm
        return matrix

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class SentenceTransformerEmbedder:
    """Optional all-MiniLM-L6-v2 (or any sentence-transformers model)."""

    name = "sentence_transformers"

    def __init__(self, model_name: str, dim: int = 384):
        if not model_name:
            raise ValueError("EMBEDDING_MODEL must be set for sentence_transformers")
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model_name = model_name
        self.model = SentenceTransformer(model_name)
        self.dim = int(self.model.get_sentence_embedding_dimension() or dim)

    @property
    def fingerprint(self) -> str:
        return f"sentence_transformers:{self.model_name}:{self.dim}"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        vectors = self.model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


Embedder = HashingEmbedder | SentenceTransformerEmbedder


def make_embedder(provider: str | None = None) -> Embedder:
    provider = provider or settings.embedding_provider
    if provider == "hashing":
        return HashingEmbedder()
    if provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dim)
    raise ValueError(f"unknown embedding provider {provider!r}")


class EmbeddingCache:
    """SQLite-backed vector cache, keyed by provider+content hash.

    This is what makes re-embedding incremental: unchanged chunks never reach
    the encoder a second time.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, dim INTEGER NOT NULL, vector BLOB NOT NULL)"
        )
        self._conn.commit()

    @staticmethod
    def key(fingerprint: str, text: str) -> str:
        return hashlib.sha256(f"{fingerprint}|{text}".encode()).hexdigest()

    def get(self, key: str) -> np.ndarray | None:
        row = self._conn.execute("SELECT dim, vector FROM embeddings WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return np.frombuffer(row[1], dtype=np.float32).copy()

    def put(self, key: str, vector: np.ndarray) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO embeddings(key, dim, vector) VALUES (?, ?, ?)",
                (key, int(vector.shape[0]), vector.astype(np.float32).tobytes()),
            )
            self._conn.commit()

    def embed_with_cache(
        self, embedder: Embedder, texts: Sequence[str], *, kind: str = "embedding"
    ) -> tuple[np.ndarray, int, int]:
        if not texts:
            return np.zeros((0, embedder.dim), dtype=np.float32), 0, 0
        keys = [self.key(embedder.fingerprint, text) for text in texts]
        vectors: list[np.ndarray | None] = [self.get(key) for key in keys]
        hits = sum(1 for vector in vectors if vector is not None)
        missing = [(i, text) for i, (vector, text) in enumerate(zip(vectors, texts, strict=False)) if vector is None]
        if missing:
            fresh = embedder.embed([text for _, text in missing])
            for (index, _), vector in zip(missing, fresh, strict=False):
                vectors[index] = vector
                self.put(keys[index], vector)
        CACHE_OPS.labels(kind=kind, result="hit").inc(hits)
        CACHE_OPS.labels(kind=kind, result="miss").inc(len(missing))
        matrix = np.vstack([v for v in vectors if v is not None]).astype(np.float32)
        if matrix.shape[0] != len(texts):
            raise RuntimeError("embedding cache returned an incomplete matrix")
        return matrix, hits, len(missing)
