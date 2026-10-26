from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from compliance.api import create_app
from compliance.config import settings
from compliance.contracts import ComplianceRequest
from compliance.corpus.index import get_default_index
from compliance.service.ratelimit import TokenBucketLimiter

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def index():
    return get_default_index()


def _first_golden_request() -> dict:
    line = (REPO_ROOT / "evals" / "golden.jsonl").read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)["request"]


@pytest.fixture(scope="session")
def fixed_expiry_request() -> dict:
    return _first_golden_request()


@pytest.fixture(scope="session")
def fixed_expiry_model(fixed_expiry_request) -> ComplianceRequest:
    return ComplianceRequest.model_validate(fixed_expiry_request)


@pytest.fixture()
def quarantine_dir(tmp_path, monkeypatch):
    target = tmp_path / "quarantine"
    target.mkdir()
    monkeypatch.setattr(settings, "quarantine_dir", target)
    return target


@pytest.fixture()
def client(quarantine_dir):
    app = create_app(
        backend="inline",
        limiter=TokenBucketLimiter(per_minute=100_000, burst=10_000),
    )
    with TestClient(app) as test_client:
        yield test_client
