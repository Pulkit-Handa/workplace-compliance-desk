from __future__ import annotations

import re

from compliance.corpus.models import Chunk, Provision
from compliance.util import normalise_text, sha256_text

_SENTENCE_SPLIT = re.compile(r"(?<=[.;:])\s+(?=[A-Z(“\"0-9])")


def _header(provision: Provision) -> str:
    clause = f" {provision.clause}" if provision.clause else ""
    return (
        f"[{provision.short_title} | section {provision.section}{clause} | "
        f"{provision.status.upper()} | {provision.effective_from or 'unknown'} to "
        f"{provision.effective_to or 'present'}]"
    )


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    return parts or [text.strip()]


def chunk_provision(
    provision: Provision, *, max_chars: int = 1100, overlap_chars: int = 120
) -> list[Chunk]:
    """Chunk one provision.

    Chunking is provision-aware: a proviso is never merged into the main clause
    it qualifies.  Long sections are split on sentence boundaries and every
    chunk repeats the statute/section/status header.  The header is what makes a
    qualifying proviso retrievable on its own and keeps the citation attached
    when only part of a section is returned.
    """
    header = _header(provision)
    body = normalise_text(provision.text)
    budget = max(240, max_chars - len(header) - 12)
    if len(body) <= budget:
        segments = [body]
    else:
        sentences = _split_sentences(body)
        segments: list[str] = []
        current = ""
        for sentence in sentences:
            if not current:
                current = sentence
            elif len(current) + 1 + len(sentence) <= budget:
                current += " " + sentence
            else:
                segments.append(current)
                tail = current[-overlap_chars:] if overlap_chars else ""
                if tail:
                    cut = tail.find(" ")
                    tail = tail[cut + 1 :] if cut >= 0 else tail
                current = (tail + " " + sentence).strip()
        if current:
            segments.append(current)
        # A single sentence may exceed the budget; hard-split it.
        hard: list[str] = []
        for segment in segments:
            while len(segment) > budget:
                hard.append(segment[:budget].rstrip())
                segment = segment[max(0, budget - overlap_chars) :].lstrip()
            if segment:
                hard.append(segment)
        segments = hard
    total = len(segments)
    chunks: list[Chunk] = []
    config = f"max={max_chars};overlap={overlap_chars};v=2"
    for index, segment in enumerate(segments, start=1):
        text = f"{header}\n{segment}"
        chunks.append(
            Chunk(
                id=f"{provision.id}::c{index}",
                provision_id=provision.id,
                document_id=provision.document_id,
                citation=provision.citation,
                statute=provision.statute,
                short_title=provision.short_title,
                section=provision.section,
                clause=provision.clause,
                heading=provision.heading,
                status=provision.status,
                effective_from=provision.effective_from,
                effective_to=provision.effective_to,
                repealed_by=provision.repealed_by,
                source_url=provision.source_url,
                topics=list(provision.topics),
                related_ids=list(provision.related_ids),
                text=text,
                body=segment,
                part=index,
                parts=total,
                content_hash=sha256_text(text),
                chunking_config=config,
            )
        )
    return chunks
