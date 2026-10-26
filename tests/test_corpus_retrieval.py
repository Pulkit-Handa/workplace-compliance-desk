"""Corpus, chunking, embedding and hybrid-retrieval tests.

These tests pin the properties the legal verifier relies on: the repealed Act is
present but marked repealed, a proviso is its own chunk, and retrieval can
exclude or down-rank repealed material.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from compliance.config import settings
from compliance.corpus.chunker import chunk_provision
from compliance.corpus.embedder import EmbeddingCache, HashingEmbedder
from compliance.corpus.index import BM25, IndexStore, SearchHit, get_default_index
from compliance.corpus.models import load_corpus
from compliance.tools import ToolResult

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(REPO_ROOT / "corpus" / "provisions.jsonl")


def test_corpus_shape_and_required_documents(corpus):
    assert len(corpus.provisions) == 46
    documents = corpus.document_versions
    assert set(documents) == {"ssc2020", "posh2013", "gratuity1972", "commencement2025"}
    assert documents["ssc2020"]["status"] == "in_force"
    assert documents["gratuity1972"]["status"] == "repealed"
    assert documents["gratuity1972"]["effective_to"] == "2025-11-20"
    assert len(corpus.corpus_hash) == 64


def test_repealed_act_is_marked_and_has_date_bounds(corpus):
    repealed = [p for p in corpus.provisions if p.status == "repealed"]
    assert len(repealed) == 4
    for provision in repealed:
        assert provision.document_id == "gratuity1972"
        assert provision.repealed_by == "ssc2020"
        assert provision.effective_to == "2025-11-20"
        assert provision.effective_from is not None


def test_successor_commences_the_day_after_repeal(corpus):
    gratuitous = next(p for p in corpus.provisions if p.id == "ssc2020-s53-1")
    repeal = next(p for p in corpus.provisions if p.id == "ssc2020-s164-1")
    commencement = next(
        p for p in corpus.provisions if p.id == "commencement2025-ss-o-5319-e"
    )
    assert gratuitous.effective_from == "2025-11-21"
    assert repeal.effective_from == "2025-11-21"
    assert commencement.effective_from == "2025-11-21"


def test_proviso_is_a_separate_provision(corpus):
    ids = {p.id for p in corpus.provisions}
    assert "ssc2020-s53-1" in ids
    assert "ssc2020-s53-1-second-proviso" in ids
    assert "ssc2020-s53-2-third-proviso" in ids
    # The proviso text must not be swallowed by the main clause.
    main = next(p for p in corpus.provisions if p.id == "ssc2020-s53-1")
    proviso = next(p for p in corpus.provisions if p.id == "ssc2020-s53-1-second-proviso")
    assert "five years" in main.text.lower()
    assert "five years" in proviso.text.lower()
    assert main.text != proviso.text


def test_chunker_repeats_the_header_and_keeps_parts(corpus):
    provision = next(p for p in corpus.provisions if p.id == "ssc2020-s53-1")
    chunks = chunk_provision(provision, max_chars=320, overlap_chars=40)
    assert len(chunks) >= 2
    for index, chunk in enumerate(chunks, start=1):
        assert chunk.part == index
        assert chunk.parts == len(chunks)
        assert chunk.text.startswith("[Code on Social Security, 2020 | section 53")
        assert chunk.status == "in_force"
        assert chunk.id == f"{provision.id}::c{index}"


def test_chunker_respects_the_character_budget(corpus):
    provision = next(p for p in corpus.provisions if p.id == "ssc2020-s60")
    chunks = chunk_provision(provision, max_chars=360, overlap_chars=60)
    for chunk in chunks:
        assert len(chunk.text) <= 360 + 2


def test_tool_trace_records_the_retrieved_passages(corpus):
    provision = next(p for p in corpus.provisions if p.id == "ssc2020-s53-1")
    chunk = chunk_provision(provision, max_chars=320, overlap_chars=40)[0]
    hit = SearchHit(
        chunk=chunk,
        score=0.91,
        vector_score=0.82,
        lexical_score=0.64,
        rank=1,
        query="gratuity",
    )
    trace = ToolResult("search_provisions", True, [hit], 1.25).as_trace()
    assert trace["items"] == 1
    assert trace["passages"][0]["id"] == chunk.id
    assert trace["passages"][0]["citation"] == chunk.citation
    assert trace["passages"][0]["score"] == 0.91
    assert trace["passages"][0]["preview"]


def test_bm25_ranks_a_matching_document_first():
    bm25 = BM25([["gratuity", "five", "years"], ["posh", "committee"], ["maternity", "benefit"]])
    scores = bm25.scores(["gratuity", "years"])
    assert int(np.argmax(scores)) == 0
    assert scores[0] > scores[1]
    assert scores[0] > 0


def test_hashing_embedder_is_deterministic_and_normalised():
    embedder = HashingEmbedder(dim=64)
    first = embedder.embed_one("gratuity five years continuous service")
    second = embedder.embed_one("gratuity five years continuous service")
    assert first.shape == (64,)
    assert np.allclose(first, second)
    assert 0.0 < float(np.linalg.norm(first)) <= 1.0001


def test_embedding_cache_reuses_unchanged_text(tmp_path):
    cache = EmbeddingCache(tmp_path / "cache.sqlite")
    embedder = HashingEmbedder(dim=32)
    texts = ["gratuity", "maternity benefit", "posh committee"]
    matrix, hits, misses = cache.embed_with_cache(embedder, texts)
    assert matrix.shape == (3, 32)
    assert (hits, misses) == (0, 3)

    again, hits, misses = cache.embed_with_cache(embedder, texts)
    assert (hits, misses) == (3, 0)
    assert np.allclose(matrix, again)


def test_index_metadata_describes_the_build(index):
    metadata = index.metadata()
    assert metadata["index_version"]
    assert metadata["chunk_count"] == 82
    assert metadata["provision_count"] == 46
    assert metadata["embedding"]["provider"] == "hashing"
    assert metadata["chunking"]["max_chars"] == settings.chunk_max_chars
    assert metadata["freshness"]["built_at"]


def test_search_returns_the_governing_gratuity_section(index):
    hits = index.search("gratuity five years continuous service", top_k=6)
    assert hits
    assert hits[0].chunk.provision_id == "ssc2020-s53-1"
    assert hits[0].chunk.status == "in_force"
    assert hits[0].score > 0
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_repeal_query_finds_the_successor_repeal_section(index):
    hits = index.search("repeal Payment of Gratuity Act 1972 section 164", top_k=4)
    assert hits[0].chunk.provision_id == "ssc2020-s164-1"
    assert any(hit.chunk.status == "repealed" for hit in hits)


def test_in_force_only_excludes_the_repealed_act(index):
    hits = index.search("Payment of Gratuity Act 1972 gratuity", top_k=20, in_force_only=True)
    assert hits
    assert all(hit.chunk.status == "in_force" for hit in hits)

    hits = index.search("Payment of Gratuity Act 1972 gratuity", top_k=20, include_repealed=False)
    assert hits
    assert all(hit.chunk.status == "in_force" for hit in hits)


def test_repealed_act_is_available_for_historical_questions(index):
    hits = index.search("Payment of Gratuity Act 1972 gratuity five years", top_k=10)
    assert any(hit.chunk.provision_id.startswith("gratuity1972-") for hit in hits)


def test_lookup_by_provision_id(index):
    chunks = index.lookup_clause("ssc2020-s53-1-second-proviso")
    assert chunks
    assert all(chunk.provision_id == "ssc2020-s53-1-second-proviso" for chunk in chunks)


def test_lookup_by_citation_fragment(index):
    chunks = index.lookup_clause("POSH Act, 2013, s. 4")
    assert chunks
    assert {chunk.provision_id for chunk in chunks} == {"posh2013-s4"}


def test_lookup_by_section_number(index):
    chunks = index.lookup_clause("53(1)")
    provision_ids = {chunk.provision_id for chunk in chunks}
    assert "ssc2020-s53-1" in provision_ids
    assert "ssc2020-s53-1-second-proviso" in provision_ids


def test_lookup_unknown_clause_is_empty(index):
    assert index.lookup_clause("not-a-real-citation") == []
    assert index.lookup_clause("   ") == []


def test_related_chunks_expand_a_provision(index):
    chunks = index.related_chunks(["ssc2020-s53-1"])
    assert chunks
    assert all(chunk.provision_id == "ssc2020-s53-1" for chunk in chunks)


def test_search_respects_empty_query(index):
    assert index.search("") == []
    assert index.search("   ") == []


def test_search_returns_posh_material_for_a_committee_query(index):
    hits = index.search("committee complaint", top_k=5, domain="workplace_harassment")
    assert hits
    assert any(hit.chunk.document_id == "posh2013" for hit in hits)


def test_metadata_reports_the_current_index(index):
    assert index.index_version in index.metadata()["index_version"]
    assert index.loaded is not None
    assert index.loaded.vectors.dtype == np.float32
    assert index.loaded.vectors.shape[0] == len(index.loaded.chunks)


def test_fresh_store_loads_the_active_version():
    store = IndexStore()
    store.load()
    assert store.index_version
    assert store.get_chunk("ssc2020-s53-1::c1") is not None
    assert store.get_chunk("does-not-exist") is None


def test_toolbox_currency_marks_repealed_act_out_of_force_after_commencement():
    from compliance.tools import ToolBox

    toolbox = ToolBox(index=get_default_index(), as_of=date(2026, 1, 1))
    import asyncio

    result = asyncio.run(toolbox.currency(["gratuity1972-s4"]))
    assert result.ok
    assert result.data is not None
    status = result.data[0]
    assert status.status == "repealed"
    assert status.in_force_on is False
    assert status.successor == "Code on Social Security, 2020"


def test_toolbox_currency_marks_repealed_act_in_force_historically():
    from compliance.tools import ToolBox

    toolbox = ToolBox(index=get_default_index(), as_of=date(2024, 6, 1))
    import asyncio

    result = asyncio.run(toolbox.currency(["gratuity1972-s4"]))
    assert result.ok
    status = result.data[0]
    assert status.status == "repealed"
    assert status.in_force_on is True
