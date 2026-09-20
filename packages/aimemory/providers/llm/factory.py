"""Provider selection (ADR-0012).

One function so every consumer - the ingestion worker, the P4-T02 benchmark harness, the knowledge
engine - resolves ``LLM_PROVIDER`` the same way and none of them imports a concrete provider class.
"""

from __future__ import annotations

from typing import Any

from aimemory.common.config import LLMSettings
from aimemory.common.errors import LLMProviderError
from aimemory.domain.ports import LLMProvider


def get_provider(settings: LLMSettings | None = None, *, provider: str | None = None) -> LLMProvider:
    """Return the configured :class:`~aimemory.domain.ports.LLMProvider`.

    ``provider`` overrides the setting, which is what the benchmark harness uses to run both engines
    over the same episodes in one process (ADR-0010's model comparison, ADR-0012's dual evaluation).
    """
    settings = settings or LLMSettings()
    name = (provider or settings.provider).lower()

    if name == "ollama":
        from .ollama_provider import OllamaProvider  # noqa: PLC0415 - avoid import cycles

        return OllamaProvider(settings)
    if name == "bedrock":
        from .bedrock_provider import BedrockProvider  # noqa: PLC0415

        return BedrockProvider(settings)
    if name == "relay":
        # Claude Haiku 4.5 answered by a Claude Code subagent instead of Bedrock: the same model by a
        # different route, for when the operator does not want the call billed to AWS. Prompts and
        # answers pass through files; see `relay_provider`.
        from .relay_provider import RelayProvider  # noqa: PLC0415

        return RelayProvider()

    raise LLMProviderError(
        f"unknown LLM_PROVIDER {name!r}; expected 'ollama', 'bedrock' or 'relay'"
    )


def both_providers(settings: LLMSettings | None = None) -> dict[str, Any]:
    """Both providers keyed by name, for the side-by-side benchmark required by ADR-0012.

    Construction is lazy in each provider, so this does not touch the network or require AWS
    credentials until a call is actually made.
    """
    settings = settings or LLMSettings()
    return {name: get_provider(settings, provider=name) for name in ("ollama", "bedrock")}
