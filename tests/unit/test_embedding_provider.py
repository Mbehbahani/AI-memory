"""Unit tests for :mod:`aimemory.providers.embedding.http` (P3-T03; owner A06; tests owed by A12,
P2-T04 - see the task brief: this file had zero coverage despite the service being verified working).

Every test here mocks HTTP with :class:`httpx.MockTransport` - no network, no compose stack. The
real, live embedding-service is covered separately by ``tests/integration/test_embedding_service.py``
(``@pytest.mark.integration``).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from aimemory.common.config import EmbeddingSettings
from aimemory.providers.embedding.http import EmbeddingServiceError, HttpEmbeddingProvider

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler, **kwargs: object) -> HttpEmbeddingProvider:
    settings = EmbeddingSettings(EMBED_BATCH_SIZE=kwargs.pop("batch_size", 32))  # type: ignore[call-arg]
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.url)
    return HttpEmbeddingProvider(settings, client=client, backoff_seconds=0.001, **kwargs)  # type: ignore[arg-type]


def _vector(dim: int = 384, fill: float = 0.1) -> list[float]:
    return [fill] * dim


# ---- embed(): batching ------------------------------------------------------------------------


def test_embed_batches_requests_at_settings_batch_size() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["texts"])
        assert payload["kind"] == "passage"
        assert payload["normalize"] is True
        return httpx.Response(
            200,
            json={
                "embeddings": [_vector() for _ in payload["texts"]],
                "model_id": "all-minilm-l6-v2-384",
                "dimension": 384,
            },
        )

    provider = _provider(handler, batch_size=2)
    texts = [f"chunk {i}" for i in range(5)]
    result = provider.embed(texts)

    assert [len(batch) for batch in calls] == [2, 2, 1]
    assert len(result.vectors) == 5
    assert result.dimension == 384
    assert result.model_id == "all-minilm-l6-v2-384"
    assert result.duration_ms >= 0


def test_embed_empty_list_makes_no_request_and_returns_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never be called
        raise AssertionError("no HTTP call expected for an empty batch")

    provider = _provider(handler)
    result = provider.embed([])
    assert result.vectors == []
    assert result.dimension == 384


# ---- embed_query(): kind=query, single vector --------------------------------------------------


def test_embed_query_sends_kind_query_and_returns_one_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {"texts": ["find X"], "kind": "query", "normalize": True}
        return httpx.Response(200, json={"embeddings": [_vector()], "model_id": "m", "dimension": 384})

    provider = _provider(handler)
    vector = provider.embed_query("find X")
    assert len(vector) == 384


def test_embed_query_raises_when_service_returns_no_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [], "model_id": "m", "dimension": 384})

    provider = _provider(handler)
    with pytest.raises(EmbeddingServiceError, match="no vector"):
        provider.embed_query("find X")


# ---- dimension validation -----------------------------------------------------------------------


def test_embed_raises_on_wrong_dimension() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [_vector(dim=123)], "model_id": "m", "dimension": 123})

    provider = _provider(handler)
    with pytest.raises(EmbeddingServiceError, match="123-d"):
        provider.embed(["one text"])


def test_embed_raises_when_response_missing_embeddings_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_id": "m", "dimension": 384})

    provider = _provider(handler)
    with pytest.raises(EmbeddingServiceError, match="missing"):
        provider.embed(["one text"])


# ---- retry / backoff ------------------------------------------------------------------------


def test_embed_retries_transient_failures_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503, json={"detail": "loading"})
        return httpx.Response(200, json={"embeddings": [_vector()], "model_id": "m", "dimension": 384})

    provider = _provider(handler, max_retries=5)
    result = provider.embed(["text"])
    assert attempts["n"] == 3
    assert len(result.vectors) == 1


def test_embed_gives_up_after_max_retries_and_raises() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"detail": "loading"})

    provider = _provider(handler, max_retries=2)
    with pytest.raises(EmbeddingServiceError, match="failed after 3 attempt"):
        provider.embed(["text"])
    assert attempts["n"] == 3  # initial attempt + 2 retries


def test_embed_retries_on_connect_error() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"embeddings": [_vector()], "model_id": "m", "dimension": 384})

    provider = _provider(handler, max_retries=3)
    result = provider.embed(["text"])
    assert len(result.vectors) == 1
    assert attempts["n"] == 2


# ---- model_identity() -------------------------------------------------------------------------


def test_model_identity_parses_health_response_and_slugifies_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(
            200,
            json={
                "model_id": "sentence-transformers/all-MiniLM-L6-v2",
                "dimension": 384,
                "revision": "abc123",
                "normalized": True,
                "max_seq": 256,
            },
        )

    provider = _provider(handler)
    identity = provider.model_identity()
    assert identity.id == "all-minilm-l6-v2-384"
    assert identity.name == "sentence-transformers/all-MiniLM-L6-v2"
    assert identity.dimension == 384
    assert identity.revision == "abc123"
    assert identity.normalized is True
    assert identity.max_seq == 256


def test_model_identity_is_cached_after_first_call() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"model_id": "m", "dimension": 384})

    provider = _provider(handler)
    first = provider.model_identity()
    second = provider.model_identity()
    assert first is second
    assert calls["n"] == 1


# ---- health() -----------------------------------------------------------------------------------


def test_health_true_when_model_loaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_loaded": True})

    assert _provider(handler).health() is True


def test_health_false_when_service_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert _provider(handler).health() is False


def test_health_false_when_model_not_loaded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_loaded": False})

    assert _provider(handler).health() is False


# ---- context manager / close -------------------------------------------------------------------


def test_context_manager_closes_an_internally_created_client() -> None:
    """No ``client=`` is passed here (unlike ``_provider()``, which always supplies a mock one), so
    the provider builds - and, on ``__exit__``, must close - its own ``httpx.Client``. No request is
    ever made, so this needs no mock transport and no live service."""
    provider = HttpEmbeddingProvider(EmbeddingSettings())
    with provider:
        assert provider._client.is_closed is False
    assert provider._client.is_closed is True


def test_close_does_not_close_a_caller_supplied_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model_loaded": True})

    settings = EmbeddingSettings()
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.url)
    provider = HttpEmbeddingProvider(settings, client=client)
    provider.close()
    assert client.is_closed is False
    client.close()
