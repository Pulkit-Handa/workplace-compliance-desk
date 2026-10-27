# Retrieval

## Design constraints

Retrieval has to satisfy two unusually strict requirements:

1. **A proviso must be retrievable on its own.** The main rule and the exception that changes the answer frequently sit in the same section. If the chunker merges them, a top-k result can contain the rule while silently losing the exception.
2. **The retrieval result must carry its legal status.** A passage is not just text; it has a document, a provision, an effective window, a repeal flag, and a source URL. Verification needs all of that.

The design is therefore provision-aware rather than a generic document splitter, and hybrid rather than a single embedding search.

## Corpus to index

```
corpus/provisions.jsonl        one normalised provision per line
        │
        ▼
provision-aware chunker        one or more chunks per provision
        │
        ├── deterministic hashing embedder (default)
        │      or optional sentence-transformers
        │
        ├── SQLite embedding cache (incremental)
        │
        ▼
data/index/indexes/<version>/
  chunks.jsonl
  vectors.npy
  manifest.json
  current.json -> newest version
```

A new index version is created when the corpus hash, chunking configuration, embedding fingerprint, or manifest changes. Unchanged vectors are reused from the previous index and the SQLite cache, so a rebuild of this corpus does not re-embed 82 chunks.

## Provision-aware chunking

`src/compliance/corpus/chunker.py`:

- Chunks **one provision at a time**. A section and its provisos are separate provisions in the corpus, so a proviso is never merged into the main clause.
- Keeps every chunk under `CHUNK_MAX_CHARS` (default 1,100).
- Splits long provisions on sentence boundaries (`_split_sentences`) with `CHUNK_OVERLAP_CHARS` (default 120) of overlap.
- Repeats a header in every chunk:

  ```
  [Code on Social Security, 2020 | section 53(1) | IN_FORCE | 2025-11-21 to present]
  ```

  The header lets a standalone proviso chunk be ranked and cited without needing the rest of the section.
- Hard-splits a single over-long sentence rather than dropping text.
- Records `chunking_config = "max=1100;overlap=120;v=2"` in every chunk and in the index manifest.

At the current corpus size this produces **82 chunks from 46 provisions**. The chunk count is asserted in the API test (`test_operational_metadata`), so an accidental corpus or chunker change fails the suite.

## Embeddings

The default is `HashingEmbedder` (`src/compliance/corpus/embedder.py`):

- Token unigrams (`w:`), weighted bigrams (`b:`, 0.7), and 4-character n-grams for tokens of length ≥ 5 (`c:`, 0.25).
- Sublinear term weighting: `1 + log(count)`.
- Signed feature hashing with BLAKE2b into `EMBEDDING_DIM` (default 384).
- L2-normalised rows, so FAISS inner product is cosine similarity.
- Fingerprint: `hashing:v1:<dim>`.

This is deliberately **lexical**, not semantic. It is deterministic across machines and containers, needs no download, and is adequate for a corpus where legal terms of art are stable. Its weakness is synonym and paraphrase recall; the optional `sentence_transformers` provider is the upgrade path:

```bash
pip install -e '.[ml]'
EMBEDDING_PROVIDER=sentence_transformers \
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2 \
python3 -m compliance.cli index build --force
```

The fingerprint includes the model name and dimension, so changing providers forces a rebuild instead of silently mixing vector spaces.

The SQLite cache (`EmbeddingCache`) is keyed by `sha256(fingerprint + "|" + text)`. The index build reports `cache_hits` and `embedded`; unchanged chunks do not reach the encoder again.

## Hybrid scoring

`IndexStore.search` combines:

```
vector_norm = min-max(FAISS top candidates)
lexical_norm = min-max(BM25 scores over all chunks)
combined = 0.62 * vector_norm + 0.38 * lexical_norm
         + 0.04 if chunk.status == "in_force"
         - 0.08 if chunk.status == "repealed"
         + 0.07 if requested domain is in chunk.topics
```

Properties:

- The vector side retrieves semantically nearby passages; the lexical side guarantees exact statutory phrases such as “continuous service”, “fixed-term”, or “three months” are not lost.
- `candidate_count = max(top_k * 8, 40)`, so a FAISS miss can still be recovered by BM25.
- The status and domain adjustments are **ranking preferences**, not safety controls. A repealed provision can still be retrieved for a historical date, and verification is what prevents it from supporting a current operative claim.
- `in_force_only` and `include_repealed` are available for callers that need a hard filter (for example the currency tool).
- `PASSAGES_RETURNED` observes the number of hits per search.

The weights, top-k, and chunk size are environment settings, not hard-coded constants:

```
RETRIEVAL_TOP_K=6
RETRIEVAL_VECTOR_WEIGHT=0.62
RETRIEVAL_LEXICAL_WEIGHT=0.38
RETRIEVAL_CACHE_TTL_S=900
CHUNK_MAX_CHARS=1100
CHUNK_OVERLAP_CHARS=120
```

## Retrieval plans: v1 vs v2

The variant changes how research is planned, not the index.

**v1 — single-pass baseline**

- One query: the normalised question (plus notes if present).
- `top_k=6`.
- No curated provision fetch, no related expansion, no retry.
- This is the control. It is expected to miss provisos and qualifiers, and the evaluation shows that.

**v2 — qualifier-aware**

- A base question query (`top_k=4`).
- Domain-specific queries from `policy.query_plan`, for example:
  - fixed-term threshold and the second proviso;
  - pro-rata rate under s. 53(2);
  - 240-day continuous service;
  - 10-employee applicability;
  - POSH committee and limitation queries.
- A curated fetch of the **required provisions** for the domain and facts.
- Expansion through `related_ids` (for example s. 53(1) ↔ s. 54, or First Schedule Chapter V).
- On verification failure, up to `MAX_VERIFY_LOOPS=2` re-queries built from the verifier's issue suggestions (`reason="verification_gap"`).

v2 is also what produces the qualifier coverage improvement in the golden set; see `docs/evals.md`.

## The tools

Agents never touch the index directly; they use bounded tools in `src/compliance/tools/__init__.py`:

| Tool | Purpose |
|---|---|
| `search_corpus` | Hybrid search with domain/status filters; returns evidence with citations |
| `lookup_provision` | Exact provision id, citation fragment, or section lookup |
| `related_provisions` | Expand through `related_ids` |
| `check_currency` | Status and effective window for a set of provision ids on `as_of` |
| `coverage_check` | Whether the corpus can answer the domain at all |

Each call is timed, cached within the run where appropriate, and counted against `MAX_TOOL_CALLS`. A tool failure is visible in the trace and degrades the run; it does not crash the process.

## Request-time retrieval and `as_of_date`

- `as_of_date` defaults to today.
- For gratuity on or after **2025-11-21**, the plan retrieves the Code on Social Security, 2020 plus the commencement notification.
- For gratuity before that date, it retrieves the Payment of Gratuity Act, 1972.
- `check_currency` returns a status record per provision: in force, repealed, not yet in force, or unknown, with the reason and dates.
- Verification independently checks `effective_from <= as_of <= effective_to` for every operative citation. The query plan is an optimisation; the verifier is the guarantee.

## Index provenance

`GET /v1/index` exposes:

- `index_version`
- `built_at` and `age_hours`
- `chunk_count` and `provision_count`
- `corpus_hash`
- embedding provider, fingerprint, and dimension
- chunking configuration
- per-document status, effective window, source URL, source hash, and provision count
- declared SLO

The cache key for a completed run includes the index version, prompt hashes, and a fingerprint of the deterministic policy/composition modules, so an index rebuild, prompt change, or code-only legal-policy change cannot serve an answer produced from stale logic.

## Retrieval failure modes

| Failure | Effect |
|---|---|
| Empty query / no hits | Draft has no evidence; verification fails; route escalates |
| Cached embedding mismatch | Index load fails with a rebuild instruction (fingerprint guard) |
| Corrupt vectors/chunks | `vectors.shape[0] != len(chunks)` raises at load |
| Missing index | Startup/readiness fails; `compliance index build` fixes it |
| Top-k dominated by repealed Act | Status adjustment reduces rank; verification rejects it for current dates |
| Qualifier missed | v2 retry fetches the required provision; if still missing, escalation |
| Semantic paraphrase | Lexical hash embedder may miss it; BM25 and curated fetch mitigate; sentence-transformers is the upgrade |

## Why not a vector database?

At 82 chunks, FAISS `IndexFlatIP` plus an in-process BM25 is faster and simpler than a managed vector database, and it keeps the index fully versioned and inspectable in a directory. The interface (`IndexStore`) is deliberately narrow, so a larger deployment can replace the storage layer without touching agents or verification.
