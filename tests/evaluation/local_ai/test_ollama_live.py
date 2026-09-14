"""P4-T01 live test: one real call against the running Ollama container.

    docker compose --profile tools run --rm --no-deps tools \
        pytest tests/evaluation/local_ai/test_ollama_live.py -m integration -q

Marked ``integration`` **and** ``slow``: on this CPU-only host one schema-constrained call against
qwen3:4b takes minutes (see ``reports/local-ai-validation.md``), so it is never part of the default
unit run. It uses a deliberately *small* schema and a short prompt - the purpose is to prove the
provider talks to the real server and returns a schema-valid parsed object with populated metrics,
not to re-measure throughput.
"""

from __future__ import annotations

from typing import Any

import pytest
from aimemory.common.config import LLMSettings
from aimemory.domain.ports import LLMProvider
from aimemory.providers.llm import OllamaProvider

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SMALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project", "status"],
    "properties": {
        "project": {"type": "string", "maxLength": 60},
        "status": {"type": "string", "enum": ["active", "paused", "done"]},
    },
}


@pytest.fixture(scope="module")
def provider() -> OllamaProvider:
    llm = OllamaProvider(LLMSettings())
    if not llm.health():
        pytest.skip("ollama is not reachable or qwen3:4b is not pulled")
    return llm


def test_live_provider_is_healthy_and_identifies_itself(provider: OllamaProvider) -> None:
    assert isinstance(provider, LLMProvider)
    identity = provider.model_identity()
    assert identity.name == "qwen3:4b"
    assert identity.provider == "ollama"
    assert identity.digest and len(identity.digest) == 64
    assert identity.parameters["num_ctx"] == provider.settings.num_ctx
    assert identity.parameters["think"] is False


def test_live_schema_constrained_call_returns_valid_json(provider: OllamaProvider) -> None:
    traced = provider.complete_json_traced(
        "The JobLab Lakehouse project is currently active. "
        "Return its name as 'project' and its status as 'status'.",
        SMALL_SCHEMA,
        system="Return JSON only.",
    )
    response = traced.response
    assert response.valid is True, response.errors
    assert response.parsed is not None
    assert response.parsed["status"] in {"active", "paused", "done"}
    assert "joblab" in response.parsed["project"].lower()

    # the metrics ADR-0010 reads must be populated on a real call
    assert response.attempts >= 1
    assert response.duration_ms > 0
    assert response.prompt_tokens and response.prompt_tokens > 0
    assert response.completion_tokens and response.completion_tokens > 0
    assert traced.traces[0].prompt_tok_s and traces_positive(traced.traces[0].gen_tok_s)


def traces_positive(value: float | None) -> bool:
    return value is not None and value > 0
