"""Sanitized error hierarchy.

Consumers: every package. A09 maps these to HTTP status codes in memory-api, A10 maps them to MCP
tool errors, A07a records them in ``ingestion_jobs.error`` / ``episodes.error``, A13 audits them.

Plan §T requires *sanitized errors*: nothing that leaves the process may contain a host path, a
connection string, a password, or the body of a file suspected to hold secrets. Each error therefore
carries two messages:

* ``public_message`` — safe to return over REST/MCP and to store in the database;
* ``detail`` — full text, only ever passed to the local structured logger, and redacted there by
  :mod:`aimemory.common.logging`.

``str(error)`` returns the *public* message, so an accidental ``f"{exc}"`` in a response body cannot
leak. Use :meth:`AiMemoryError.sanitized` to build an API/MCP payload.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AiMemoryError",
    "ConfigurationError",
    "ContractError",
    "EmbeddingProviderError",
    "ExtractionError",
    "GraphStoreError",
    "LLMProviderError",
    "NotFoundError",
    "OntologyError",
    "PathGuardError",
    "PersistenceError",
    "PolicyViolationError",
    "ProviderError",
    "RateLimitError",
    "RetrievalError",
    "SchemaValidationError",
    "SecretDetectedError",
    "SourceUriError",
    "WriteDisabledError",
]


class AiMemoryError(Exception):
    """Base class. ``code`` is a stable machine-readable token used by clients and tests."""

    code: str = "internal_error"
    public_message: str = "An internal error occurred."
    http_status: int = 500

    def __init__(
        self,
        public_message: str | None = None,
        *,
        detail: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.public_message = public_message or type(self).public_message
        self.detail = detail
        self.context = context or {}
        super().__init__(self.public_message)

    def __str__(self) -> str:  # never the detail — see module docstring
        return self.public_message

    def sanitized(self) -> dict[str, Any]:
        """Payload safe to return to a caller and to persist. Contains no detail and no secrets."""
        return {"error": self.code, "message": self.public_message, "context": self.context}


class ConfigurationError(AiMemoryError):
    """Missing/invalid setting or config file. Raised at startup, never mid-request."""

    code = "configuration_error"
    public_message = "Service configuration is invalid."


class ContractError(AiMemoryError):
    """A domain object violates a contract that the code itself guarantees (a bug, not user input)."""

    code = "contract_error"
    public_message = "Internal contract violation."


class OntologyError(ContractError):
    """``schemas/ontology.yaml`` is malformed, or a label/predicate is not in the ontology."""

    code = "ontology_error"
    public_message = "Ontology violation."


class SourceUriError(AiMemoryError):
    """A source URI could not be parsed or built (ADR-0004)."""

    code = "source_uri_error"
    public_message = "Invalid source URI."
    http_status = 400


class PathGuardError(AiMemoryError):
    """A path escaped its root, or is a symlink/junction/reparse point (plan §T).

    Security-relevant: the offending path goes to ``detail`` only. The public message never echoes
    the input, so a traversal probe learns nothing from the response.
    """

    code = "path_guard_error"
    public_message = "Path is outside the allowed source root."
    http_status = 400


class PolicyViolationError(AiMemoryError):
    """An operation was attempted on a source whose storage policy forbids it (plan §K)."""

    code = "policy_violation"
    public_message = "Storage policy forbids this operation."
    http_status = 403


class SecretDetectedError(AiMemoryError):
    """A secret pattern matched. The matching text is never attached — not even to ``detail``."""

    code = "secret_detected"
    public_message = "Content flagged by the secret detector; metadata only."
    http_status = 403


class ProviderError(AiMemoryError):
    """An external local provider (Ollama, embedding service, Neo4j) failed."""

    code = "provider_error"
    public_message = "A local model/service is unavailable."
    http_status = 503


class LLMProviderError(ProviderError):
    code = "llm_provider_error"
    public_message = "The extraction model is unavailable."


class EmbeddingProviderError(ProviderError):
    code = "embedding_provider_error"
    public_message = "The embedding service is unavailable."


class GraphStoreError(ProviderError):
    code = "graph_store_error"
    public_message = "The graph store is unavailable."


class SchemaValidationError(AiMemoryError):
    """Model output did not satisfy a ``schemas/extraction/*.json`` contract after retries."""

    code = "schema_validation_error"
    public_message = "Model output did not match the required schema."
    http_status = 422


class ExtractionError(AiMemoryError):
    """An episode could not be processed; the episode is marked ``failed`` and stays reprocessable."""

    code = "extraction_error"
    public_message = "Knowledge extraction failed for this episode."


class PersistenceError(AiMemoryError):
    code = "persistence_error"
    public_message = "A storage operation failed."


class RetrievalError(AiMemoryError):
    code = "retrieval_error"
    public_message = "The query could not be answered."


class NotFoundError(AiMemoryError):
    code = "not_found"
    public_message = "The requested object does not exist."
    http_status = 404


class WriteDisabledError(AiMemoryError):
    """ADR-0008: writes require ``GATEWAY_WRITE_ENABLED`` (and ``MCP_WRITE_ENABLED``) plus confirm."""

    code = "write_disabled"
    public_message = "Writes are disabled or not confirmed."
    http_status = 403


class RateLimitError(AiMemoryError):
    """ADR-0008: MCP write tools are limited to 10 calls/minute."""

    code = "rate_limited"
    public_message = "Rate limit exceeded."
    http_status = 429
