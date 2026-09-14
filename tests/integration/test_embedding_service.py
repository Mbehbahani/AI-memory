"""Integration tests against the real, live ``embedding-service`` (P3-T03; owner A06; tests owed by
A12, P2-T04 - see the task brief).

Marked ``integration``: needs the compose network / the loopback-published port. Auto-skips (with a
reason) when the service is not reachable, via ``embedding_available`` in ``tests/conftest.py``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import httpx
import pytest
from aimemory.common.config import get_settings
from aimemory.providers.embedding.http import HttpEmbeddingProvider

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("embedding_available")]


@pytest.fixture()
def provider() -> Iterator[HttpEmbeddingProvider]:
    with HttpEmbeddingProvider(get_settings().embedding) as p:
        yield p


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def test_health_reports_384_dims_and_loaded_model(provider: HttpEmbeddingProvider) -> None:
    assert provider.health() is True
    identity = provider.model_identity()
    assert identity.dimension == 384
    assert identity.normalized is True


def test_embed_returns_384_dim_unit_norm_vectors(provider: HttpEmbeddingProvider) -> None:
    result = provider.embed(["PostgreSQL is the system of record.", "Neo4j is a rebuildable projection."])
    assert result.dimension == 384
    assert len(result.vectors) == 2
    for vector in result.vectors:
        assert len(vector) == 384
        assert _norm(vector) == pytest.approx(1.0, abs=1e-3)  # MEASURED: MiniLM output is L2-normalized


def test_embed_is_deterministic_for_the_same_text(provider: HttpEmbeddingProvider) -> None:
    text = "Determinism check: the same input must produce the same embedding."
    first = provider.embed([text]).vectors[0]
    second = provider.embed([text]).vectors[0]
    assert first == pytest.approx(second, abs=1e-6)


def test_embed_query_matches_embed_dimension(provider: HttpEmbeddingProvider) -> None:
    vector = provider.embed_query("what uses Databricks?")
    assert len(vector) == 384
    assert _norm(vector) == pytest.approx(1.0, abs=1e-3)


def test_openai_compatible_endpoint_returns_expected_shape(embedding_available: bool) -> None:
    settings = get_settings().embedding
    response = httpx.post(
        f"{settings.url}/v1/embeddings",
        json={"input": ["OpenAI-shaped request"], "model": settings.model_id},
        timeout=settings.timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()

    assert body["object"] == "list"
    assert len(body["data"]) == 1
    entry = body["data"][0]
    assert entry["object"] == "embedding"
    assert entry["index"] == 0
    assert isinstance(entry["embedding"], list)
    assert len(entry["embedding"]) == 384
    assert body["usage"]["prompt_tokens"] > 0
    assert body["usage"]["total_tokens"] >= body["usage"]["prompt_tokens"]


def test_openai_compatible_endpoint_supports_base64_encoding(embedding_available: bool) -> None:
    import base64

    settings = get_settings().embedding
    response = httpx.post(
        f"{settings.url}/v1/embeddings",
        json={"input": "base64 please", "encoding_format": "base64"},
        timeout=settings.timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    raw = base64.b64decode(body["data"][0]["embedding"])
    assert len(raw) == 384 * 4  # 384 little-endian float32s
