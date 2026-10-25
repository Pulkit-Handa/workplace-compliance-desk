from __future__ import annotations

import json
import math
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from compliance.config import settings
from compliance.corpus.chunker import chunk_provision
from compliance.corpus.embedder import Embedder, EmbeddingCache, make_embedder
from compliance.corpus.models import Chunk, load_corpus, load_source_manifest
from compliance.logging_setup import get_logger
from compliance.observability import metrics
from compliance.util import iso_now, json_loads, tokenize

logger = get_logger(__name__)


@dataclass
class SearchHit:
    chunk: Chunk
    score: float
    vector_score: float
    lexical_score: float
    rank: int
    query: str = ""


@dataclass
class BuildResult:
    index_version: str
    manifest: dict[str, Any]
    reused: bool
    chunk_count: int
    embedded: int
    cache_hits: int
    duration_s: float


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _read_current(index_dir: Path) -> dict[str, Any] | None:
    pointer = index_dir / "current.json"
    if not pointer.exists():
        return None
    return json_loads(pointer.read_text(encoding="utf-8"))


def _load_previous(index_dir: Path) -> tuple[dict[str, Any] | None, list[Chunk], np.ndarray | None]:
    current = _read_current(index_dir)
    if not current:
        return None, [], None
    version_dir = index_dir / "indexes" / current["index_version"]
    manifest_path = version_dir / "manifest.json"
    chunks_path = version_dir / "chunks.jsonl"
    vectors_path = version_dir / "vectors.npy"
    if not (manifest_path.exists() and chunks_path.exists() and vectors_path.exists()):
        return None, [], None
    manifest = json_loads(manifest_path.read_text(encoding="utf-8"))
    chunks = [Chunk.from_dict(json.loads(line)) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line]
    vectors = np.load(vectors_path)
    return manifest, chunks, vectors


def build_index(
    *,
    corpus_file: Path | None = None,
    index_dir: Path | None = None,
    embedder: Embedder | None = None,
    max_chars: int | None = None,
    overlap_chars: int | None = None,
    force: bool = False,
) -> BuildResult:
    corpus_file = corpus_file or settings.corpus_file
    index_dir = index_dir or settings.index_dir
    embedder = embedder or make_embedder()
    max_chars = max_chars or settings.chunk_max_chars
    overlap_chars = overlap_chars if overlap_chars is not None else settings.chunk_overlap_chars
    start = time.perf_counter()
    corpus = load_corpus(corpus_file)
    chunks: list[Chunk] = []
    for provision in corpus.provisions:
        chunks.extend(chunk_provision(provision, max_chars=max_chars, overlap_chars=overlap_chars))
    chunking = f"max={max_chars};overlap={overlap_chars};v=2"
    previous_manifest, previous_chunks, previous_vectors = _load_previous(index_dir)
    same_config = bool(
        previous_manifest
        and previous_manifest.get("corpus_hash") == corpus.corpus_hash
        and previous_manifest.get("chunking", {}).get("config") == chunking
        and previous_manifest.get("embedding", {}).get("fingerprint") == embedder.fingerprint
    )
    if same_config and not force:
        version_dir = index_dir / "indexes" / previous_manifest["index_version"]
        if (version_dir / "vectors.npy").exists():
            logger.info(
                "index_up_to_date",
                extra={
                    "event": "index_up_to_date",
                    "fields": {"index_version": previous_manifest["index_version"], "chunks": len(previous_chunks)},
                },
            )
            metrics.INDEX_CHUNKS.set(len(previous_chunks))
            return BuildResult(
                index_version=previous_manifest["index_version"],
                manifest=previous_manifest,
                reused=True,
                chunk_count=len(previous_chunks),
                embedded=0,
                cache_hits=0,
                duration_s=time.perf_counter() - start,
            )

    stored_vectors: dict[str, np.ndarray] = {}
    if (
        previous_manifest
        and previous_vectors is not None
        and previous_manifest.get("embedding", {}).get("fingerprint") == embedder.fingerprint
        and previous_manifest.get("chunking", {}).get("config") == chunking
        and len(previous_chunks) == len(previous_vectors)
    ):
        for chunk, vector in zip(previous_chunks, previous_vectors, strict=False):
            stored_vectors[chunk.content_hash] = vector

    texts: list[str] = []
    reused = 0
    for chunk in chunks:
        vector = stored_vectors.get(chunk.content_hash)
        if vector is not None:
            reused += 1
        texts.append(chunk.text)

    cache = EmbeddingCache(index_dir / "embedding_cache.sqlite")
    matrix, hits, misses = cache.embed_with_cache(embedder, texts)
    # Replace cache-produced rows with the previous index vectors where possible.
    for i, chunk in enumerate(chunks):
        vector = stored_vectors.get(chunk.content_hash)
        if vector is not None:
            matrix[i] = vector
    if matrix.shape[0] != len(chunks):
        raise RuntimeError("embedding matrix length does not match chunks")

    version = f"idx-{int(time.time())}-{corpus.corpus_hash[:8]}"
    version_dir = index_dir / "indexes" / version
    version_dir.mkdir(parents=True, exist_ok=False)
    with (version_dir / "chunks.jsonl").open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(json.dumps(chunk.as_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    np.save(version_dir / "vectors.npy", matrix.astype(np.float32))
    duration = time.perf_counter() - start
    manifest = {
        "index_version": version,
        "built_at": iso_now(),
        "corpus_file": str(corpus_file),
        "corpus_hash": corpus.corpus_hash,
        "chunk_count": len(chunks),
        "provision_count": len(corpus.provisions),
        "document_count": len(corpus.document_versions),
        "documents": list(corpus.document_versions.values()),
        "source_manifest": load_source_manifest(settings.index_manifest),
        "embedding": {
            "provider": embedder.name,
            "fingerprint": embedder.fingerprint,
            "dim": int(matrix.shape[1]),
        },
        "chunking": {
            "strategy": "provision-aware; provisos kept separate; header repeated per chunk",
            "max_chars": max_chars,
            "overlap_chars": overlap_chars,
            "config": chunking,
        },
        "build": {
            "duration_s": round(duration, 3),
            "chunks_per_second": round(len(chunks) / duration, 2) if duration else 0,
            "embedded": misses,
            "reused_from_previous_index": reused,
            "embedding_cache_hits": hits,
            "force": force,
        },
    }
    _write_json(version_dir / "manifest.json", manifest)
    _write_json(
        index_dir / "current.json",
        {
            "index_version": version,
            "built_at": manifest["built_at"],
            "chunk_count": len(chunks),
            "corpus_hash": corpus.corpus_hash,
            "embedding_fingerprint": embedder.fingerprint,
        },
    )
    # Keep the previous two versions for rollback; remove older ones.
    all_versions = sorted(
        [p for p in (index_dir / "indexes").iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
    )
    for old in all_versions[:-3]:
        shutil.rmtree(old, ignore_errors=True)

    metrics.INDEX_CHUNKS.set(len(chunks))
    metrics.INDEX_BUILD_TIMESTAMP.set(time.time())
    metrics.INDEX_BUILD_DURATION.observe(duration)
    metrics.INDEX_EMBEDDED_CHUNKS.inc(misses)
    metrics.INDEX_REUSED_CHUNKS.inc(reused)
    logger.info(
        "index_built",
        extra={
            "event": "index_built",
            "fields": {
                "index_version": version,
                "chunks": len(chunks),
                "embedded": misses,
                "reused": reused,
                "duration_s": round(duration, 3),
                "embedding": embedder.fingerprint,
            },
        },
    )
    return BuildResult(
        index_version=version,
        manifest=manifest,
        reused=False,
        chunk_count=len(chunks),
        embedded=misses,
        cache_hits=hits,
        duration_s=duration,
    )


class BM25:
    def __init__(self, documents: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents = documents
        self.doc_len = np.array([len(doc) for doc in documents], dtype=np.float32)
        self.avgdl = float(self.doc_len.mean()) if len(documents) else 0.0
        self.term_freq: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in documents:
            frequencies: dict[str, int] = {}
            for term in doc:
                frequencies[term] = frequencies.get(term, 0) + 1
            self.term_freq.append(frequencies)
            for term in frequencies:
                df[term] = df.get(term, 0) + 1
        total = max(1, len(documents))
        self.idf = {
            term: math.log(1 + (total - count + 0.5) / (count + 0.5)) for term, count in df.items()
        }

    def scores(self, query_tokens: list[str]) -> np.ndarray:
        if not self.documents:
            return np.zeros(0, dtype=np.float32)
        scores = np.zeros(len(self.documents), dtype=np.float32)
        for i, frequencies in enumerate(self.term_freq):
            norm = 1 - self.b + self.b * (self.doc_len[i] / self.avgdl if self.avgdl else 0)
            for term in query_tokens:
                tf = frequencies.get(term)
                if not tf:
                    continue
                scores[i] += self.idf.get(term, 0.0) * (tf * (self.k1 + 1)) / (
                    tf + self.k1 * norm
                )
        return scores


@dataclass
class LoadedIndex:
    manifest: dict[str, Any]
    chunks: list[Chunk]
    vectors: np.ndarray
    bm25: BM25
    faiss_index: Any
    by_id: dict[str, int]
    by_provision: dict[str, list[int]]


class IndexStore:
    def __init__(self, index_dir: Path | None = None):
        self.index_dir = index_dir or settings.index_dir
        self.embedder: Embedder = make_embedder()
        self.loaded: LoadedIndex | None = None

    @property
    def index_version(self) -> str | None:
        return self.loaded.manifest.get("index_version") if self.loaded else None

    def load(self) -> IndexStore:
        import faiss

        current = _read_current(self.index_dir)
        if not current:
            raise FileNotFoundError(
                f"no active index under {self.index_dir}; run `python -m compliance.cli index build`"
            )
        version_dir = self.index_dir / "indexes" / current["index_version"]
        manifest = json_loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
        chunks = [
            Chunk.from_dict(json.loads(line))
            for line in (version_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line
        ]
        vectors = np.load(version_dir / "vectors.npy").astype(np.float32)
        if vectors.shape[0] != len(chunks):
            raise ValueError("index is corrupt: vectors/chunks length mismatch")
        if manifest.get("embedding", {}).get("fingerprint") != self.embedder.fingerprint:
            raise ValueError(
                "active index embedding fingerprint does not match configuration; rebuild the index"
            )
        faiss_index = faiss.IndexFlatIP(int(vectors.shape[1]))
        faiss_index.add(vectors)
        bm25 = BM25([tokenize(chunk.text) for chunk in chunks])
        by_id = {chunk.id: i for i, chunk in enumerate(chunks)}
        by_provision: dict[str, list[int]] = {}
        for i, chunk in enumerate(chunks):
            by_provision.setdefault(chunk.provision_id, []).append(i)
        self.loaded = LoadedIndex(
            manifest=manifest,
            chunks=chunks,
            vectors=vectors,
            bm25=bm25,
            faiss_index=faiss_index,
            by_id=by_id,
            by_provision=by_provision,
        )
        metrics.INDEX_CHUNKS.set(len(chunks))
        logger.info(
            "index_loaded",
            extra={
                "event": "index_loaded",
                "fields": {"index_version": manifest["index_version"], "chunks": len(chunks)},
            },
        )
        return self

    def ensure_loaded(self) -> LoadedIndex:
        if self.loaded is None:
            self.load()
        assert self.loaded is not None
        return self.loaded

    @staticmethod
    def _normalise(values: np.ndarray) -> np.ndarray:
        if values.size == 0:
            return values
        maximum = float(np.max(values))
        minimum = float(np.min(values))
        if maximum <= 0:
            return np.zeros_like(values)
        if maximum == minimum:
            return np.ones_like(values)
        return (values - minimum) / (maximum - minimum)

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        include_repealed: bool = True,
        domain: str | None = None,
        in_force_only: bool = False,
    ) -> list[SearchHit]:
        loaded = self.ensure_loaded()
        top_k = top_k or settings.retrieval_top_k
        if not loaded.chunks:
            return []
        query = query.strip()
        if not query:
            return []
        query_vector = self.embedder.embed_one(query).reshape(1, -1).astype(np.float32)
        candidate_count = min(len(loaded.chunks), max(top_k * 8, 40))
        vector_scores = np.zeros(len(loaded.chunks), dtype=np.float32)
        distances, indices = loaded.faiss_index.search(query_vector, candidate_count)
        for score, index in zip(distances[0], indices[0], strict=False):
            if index >= 0:
                vector_scores[index] = max(0.0, float(score))
        lexical_scores = loaded.bm25.scores(tokenize(query))
        lexical_norm = self._normalise(lexical_scores)
        vector_norm = self._normalise(vector_scores)
        combined = (
            settings.retrieval_vector_weight * vector_norm
            + settings.retrieval_lexical_weight * lexical_norm
        )
        for i, chunk in enumerate(loaded.chunks):
            if chunk.status == "in_force":
                combined[i] += 0.04
            else:
                combined[i] -= 0.08
            if domain and domain in chunk.topics:
                combined[i] += 0.07
        order = np.argsort(-combined)
        hits: list[SearchHit] = []
        rank = 0
        for index in order:
            chunk = loaded.chunks[int(index)]
            if in_force_only and chunk.status != "in_force":
                continue
            if not include_repealed and chunk.status != "in_force":
                continue
            if combined[index] <= 0 and hits:
                continue
            rank += 1
            hits.append(
                SearchHit(
                    chunk=chunk,
                    score=round(float(combined[index]), 6),
                    vector_score=round(float(vector_scores[index]), 6),
                    lexical_score=round(float(lexical_norm[index]), 6),
                    rank=rank,
                    query=query,
                )
            )
            if len(hits) >= top_k:
                break
        metrics.PASSAGES_RETURNED.observe(len(hits))
        return hits

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        loaded = self.ensure_loaded()
        index = loaded.by_id.get(chunk_id)
        return loaded.chunks[index] if index is not None else None

    def get_provision_chunks(self, provision_id: str) -> list[Chunk]:
        loaded = self.ensure_loaded()
        return [loaded.chunks[i] for i in loaded.by_provision.get(provision_id, [])]

    def lookup_clause(self, clause: str) -> list[Chunk]:
        """Look up by provision id, citation fragment, or section number.

        Accepted examples: ``ssc2020-s53-1``, ``Code on Social Security, 2020 s. 53(1)``,
        ``53(1)`` and ``POSH s. 4``.  The result is resolved against the loaded
        index, never against the network.
        """
        loaded = self.ensure_loaded()
        needle = clause.strip()
        if not needle:
            return []
        if needle in loaded.by_provision:
            return [loaded.chunks[i] for i in loaded.by_provision[needle]]
        lowered = needle.lower()
        candidates: list[Chunk] = []
        for chunk in loaded.chunks:
            haystack = " ".join(
                [chunk.provision_id, chunk.citation, chunk.short_title, chunk.section, chunk.clause]
            ).lower()
            if lowered in haystack:
                candidates.append(chunk)
        if candidates:
            return candidates
        # Last resort: normalise "s. 53(1)" to "53(1)" and match section+clause.
        import re

        match = re.search(r"(?:s\.?\s*)?(\d+[A-Z]?)(?:\s*\(([^)]+)\))?", lowered)
        if match:
            section, sub = match.group(1), match.group(2)
            for chunk in loaded.chunks:
                if chunk.section.lower() == section and (not sub or sub in chunk.clause.lower()):
                    candidates.append(chunk)
        return candidates

    def related_chunks(self, provision_ids: list[str]) -> list[Chunk]:
        self.ensure_loaded()
        seen: set[str] = set()
        result: list[Chunk] = []
        for provision_id in provision_ids:
            for chunk in self.get_provision_chunks(provision_id):
                if chunk.id not in seen:
                    seen.add(chunk.id)
                    result.append(chunk)
        return result

    def metadata(self) -> dict[str, Any]:
        loaded = self.ensure_loaded()
        manifest = loaded.manifest
        return {
            "index_version": manifest.get("index_version"),
            "built_at": manifest.get("built_at"),
            "chunk_count": manifest.get("chunk_count"),
            "provision_count": manifest.get("provision_count"),
            "corpus_hash": manifest.get("corpus_hash"),
            "embedding": manifest.get("embedding"),
            "chunking": manifest.get("chunking"),
            "documents": manifest.get("documents"),
            "build": manifest.get("build"),
            "freshness": _freshness(manifest.get("built_at")),
        }


def _freshness(built_at: str | None) -> dict[str, Any]:
    if not built_at:
        return {"built_at": None, "age_hours": None}
    try:
        from datetime import datetime

        then = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
        age = (datetime.now(UTC) - then).total_seconds() / 3600
        return {"built_at": built_at, "age_hours": round(age, 2)}
    except ValueError:
        return {"built_at": built_at, "age_hours": None}


_default_store: IndexStore | None = None


def get_default_index() -> IndexStore:
    global _default_store
    if _default_store is None:
        _default_store = IndexStore().load()
    return _default_store


def reset_default_index() -> None:
    global _default_store
    _default_store = None
