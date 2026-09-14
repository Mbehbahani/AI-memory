"""aimemory.providers.embedding — HTTP client adapter for apps/embedding-service (P3-T03, A06)."""

from __future__ import annotations

from .http import EmbeddingServiceError, HttpEmbeddingProvider

__all__ = ["EmbeddingServiceError", "HttpEmbeddingProvider"]

