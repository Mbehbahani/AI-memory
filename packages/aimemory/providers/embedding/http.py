"""HTTP client adapter for ``apps/embedding-service`` (P3-T03, owner A06).

Implements the frozen :class:`aimemory.domain.ports.EmbeddingProvider` port (do not edit that
contract here — ``packages/aimemory/domain`` is A02's). Talks to the FastAPI service over plain HTTP
inside the compose network (``EmbeddingSettings.url``, default ``http://embedding-service:8010``),
batches large requests at ``EmbeddingSettings.batch_size``, retries transient failures with
exponential backoff, and returns the same :class:`~aimemory.domain.models.EmbeddingModel` /
:class:`~aimemory.domain.ports.EmbeddingResult` payloads A04 persists and A09/A07a query by the
``(text_hash, model_id)`` reuse key — see :func:`aimemory.common.hashing.text_hash`, re-exported here
so callers don't reach into ``common`` themselves.

Consumers: A07a (ingestion embed step, ``INGEST_EMBED_CONCURRENCY``), A09 (query embedding at
retrieval time).
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

import httpx

from aimemory.common.config import EmbeddingSettings
from aimemory.common.hashing import text_hash
from aimemory.domain.models import EmbeddingModel
from aimemory.domain.ports import EmbeddingResult

__all__ = ["EmbeddingServiceError", "HttpEmbeddingProvider", "text_hash"]

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class EmbeddingServiceError(RuntimeError):
    """Raised when the embedding-service is unreachable or returns a malformed/invalid payload."""


def _slugify(value: str) -> str:
    slug = _NON_ALNUM_RE.sub("-", value.lower()).strip("-")
    return slug or "model"


def _default_model_slug(model_name: str, dimension: int) -> str:
    """``sentence-transformers/all-MiniLM-L6-v2`` -> ``all-minilm-l6-v2-384`` (plan section N)."""
    tail = model_name.rsplit("/", 1)[-1]
    return f"{_slugify(tail)}-{dimension}"


class HttpEmbeddingProvider:
    """Default :class:`~aimemory.domain.ports.EmbeddingProvider`: HTTP client for the MiniLM service."""

    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        *,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._settings = settings or EmbeddingSettings()
        self._client = client or httpx.Client(
            base_url=self._settings.url, timeout=self._settings.timeout_seconds
        )
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._identity: EmbeddingModel | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpEmbeddingProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------------------------------- port

    @property
    def dimensions(self) -> int:
        return self._settings.dimensions

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        texts = list(texts)
        if not texts:
            return EmbeddingResult(
                vectors=[], model_id=self._settings.model_id, dimension=self._settings.dimensions
            )

        vectors: list[list[float]] = []
        model_id = self._settings.model_id
        start = time.perf_counter()
        batch_size = max(1, self._settings.batch_size)
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset : offset + batch_size]
            payload = self._post_with_retries(
                "/embed", {"texts": batch, "kind": "passage", "normalize": True}
            )
            batch_vectors = payload.get("embeddings", payload.get("vectors"))
            if not isinstance(batch_vectors, list):
                raise EmbeddingServiceError(
                    "embedding-service /embed response missing 'embeddings'/'vectors'"
                )
            self._check_dimension(batch_vectors)
            vectors.extend(batch_vectors)
            model_id = payload.get("model_id", payload.get("model", model_id))
        duration_ms = int((time.perf_counter() - start) * 1000)
        return EmbeddingResult(
            vectors=vectors,
            model_id=model_id,
            dimension=self._settings.dimensions,
            duration_ms=duration_ms,
        )

    def embed_query(self, text: str) -> list[float]:
        payload = self._post_with_retries(
            "/embed", {"texts": [text], "kind": "query", "normalize": True}
        )
        vectors = payload.get("embeddings", payload.get("vectors"))
        if not isinstance(vectors, list) or not vectors:
            raise EmbeddingServiceError("embedding-service /embed returned no vector for query")
        self._check_dimension(vectors)
        return vectors[0]

    def model_identity(self) -> EmbeddingModel:
        if self._identity is not None:
            return self._identity
        data = self._get_with_retries("/health")
        name = data.get("model_id", data.get("model", self._settings.model_id))
        dimension = int(data.get("dimension", data.get("dimensions", self._settings.dimensions)))
        self._identity = EmbeddingModel(
            id=_default_model_slug(name, dimension),
            name=name,
            dimension=dimension,
            revision=data.get("revision"),
            normalized=bool(data.get("normalized", True)),
            max_seq=int(data.get("max_seq", self._settings.max_seq)),
        )
        return self._identity

    def health(self) -> bool:
        try:
            data = self._get_with_retries("/health", max_retries=0)
        except EmbeddingServiceError:
            return False
        return bool(data.get("model_loaded", True))

    # ------------------------------------------------------------------------------------ internals

    def _check_dimension(self, vectors: list[list[float]]) -> None:
        expected = self._settings.dimensions
        for i, vector in enumerate(vectors):
            if len(vector) != expected:
                raise EmbeddingServiceError(
                    f"embedding-service returned a {len(vector)}-d vector at index {i}, "
                    f"expected {expected}-d"
                )

    def _post_with_retries(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_with_retries("POST", path, json=body)

    def _get_with_retries(self, path: str, *, max_retries: int | None = None) -> dict[str, Any]:
        return self._request_with_retries("GET", path, max_retries=max_retries)

    def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        retries = self._max_retries if max_retries is None else max_retries
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self._client.request(method, path, json=json)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(self._backoff_seconds * (2**attempt))
                    continue
        raise EmbeddingServiceError(
            f"embedding-service {method} {path} failed after {retries + 1} attempt(s): {last_exc}"
        ) from last_exc
