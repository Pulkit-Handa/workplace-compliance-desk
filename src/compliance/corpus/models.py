from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from compliance.util import json_loads, normalise_text, sha256_text


@dataclass
class Provision:
    id: str
    document_id: str
    statute: str
    short_title: str
    instrument_type: str
    section: str
    clause: str
    heading: str
    text: str
    citation: str
    status: str
    effective_from: str | None
    effective_to: str | None
    repealed_by: str | None
    source_url: str
    source_doc_sha256: str
    page: int | None
    topics: list[str] = field(default_factory=list)
    related_ids: list[str] = field(default_factory=list)
    order: int = 0

    @property
    def is_in_force(self) -> bool:
        return self.status == "in_force"

    @property
    def content_hash(self) -> str:
        return sha256_text(
            "|".join(
                [
                    self.id,
                    self.short_title,
                    self.section,
                    self.clause,
                    normalise_text(self.text),
                    self.status,
                ]
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Chunk:
    id: str
    provision_id: str
    document_id: str
    citation: str
    statute: str
    short_title: str
    section: str
    clause: str
    heading: str
    status: str
    effective_from: str | None
    effective_to: str | None
    repealed_by: str | None
    source_url: str
    topics: list[str]
    related_ids: list[str]
    text: str
    body: str
    part: int
    parts: int
    content_hash: str
    chunking_config: str

    @property
    def is_in_force(self) -> bool:
        return self.status == "in_force"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Chunk:
        return cls(**value)


@dataclass
class Corpus:
    provisions: list[Provision]
    corpus_hash: str
    document_versions: dict[str, dict[str, Any]]


def load_corpus(path: Path) -> Corpus:
    if not path.exists():
        raise FileNotFoundError(f"corpus not found: {path}")
    provisions: list[Provision] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json_loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"invalid provision at {path}:{line_no}")
            provision = Provision(**raw)
            if provision.id in seen:
                raise ValueError(f"duplicate provision id {provision.id!r} at line {line_no}")
            seen.add(provision.id)
            if not provision.text.strip():
                raise ValueError(f"empty provision text for {provision.id}")
            provisions.append(provision)
    provisions.sort(key=lambda p: (p.order, p.id))
    corpus_hash = sha256_text("\n".join(p.content_hash for p in provisions))
    document_versions: dict[str, dict[str, Any]] = {}
    for provision in provisions:
        entry = document_versions.setdefault(
            provision.document_id,
            {
                "document_id": provision.document_id,
                "title": provision.statute,
                "status": provision.status,
                "effective_from": provision.effective_from,
                "effective_to": provision.effective_to,
                "source_url": provision.source_url,
                "source_doc_sha256": provision.source_doc_sha256,
                "provisions": 0,
            },
        )
        entry["provisions"] += 1
    return Corpus(provisions=provisions, corpus_hash=corpus_hash, document_versions=document_versions)


def load_source_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
