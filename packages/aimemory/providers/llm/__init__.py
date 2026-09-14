"""aimemory.providers.llm - schema-constrained generation (plan section M, ADR-0012, owner A05).

Two providers, neither privileged: :class:`OllamaProvider` (qwen3:4b, local/offline) and
:class:`BedrockProvider` (Claude Haiku 4.5). ``LLM_PROVIDER`` selects one; ``get_provider`` resolves
it from settings. See ADR-0012 for why both exist and what selecting bedrock costs.
"""

from .ollama_provider import (
    VALIDATOR_BACKEND,
    CallTrace,
    OllamaProvider,
    TracedResponse,
    default_model_slug,
)
from .bedrock_provider import (
    EXTRACTION_TOOL_NAME,
    BedrockCallTrace,
    BedrockProvider,
    TracedBedrockResponse,
)
from .factory import get_provider
from .validation import validate_json

__all__ = [
    "EXTRACTION_TOOL_NAME",
    "VALIDATOR_BACKEND",
    "BedrockCallTrace",
    "BedrockProvider",
    "TracedBedrockResponse",
    "get_provider",
    "CallTrace",
    "OllamaProvider",
    "TracedResponse",
    "default_model_slug",
    "validate_json",
]
