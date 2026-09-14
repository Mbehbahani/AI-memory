"""Unit tests for :class:`aimemory.providers.llm.BedrockProvider` (ADR-0012). Owner A05.

boto3 is never imported and no AWS call is ever made: a stub client object stands in for
``bedrock-runtime`` and returns canned ``InvokeModel`` payloads. These run in the ``tools`` image,
which deliberately has no AWS credentials.

The live counterpart of these tests is the P4-T02 campaign itself
(``run_benchmark.py --provider bedrock``), whose raw output is in
``tests/evaluation/local_ai/results/bench-bedrock-p4t02.json``.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from aimemory.common.config import LLMSettings
from aimemory.common.errors import LLMProviderError
from aimemory.domain.ports import LLMProvider
from aimemory.providers.llm import EXTRACTION_TOOL_NAME, BedrockProvider

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["doc_kind", "entities"],
    "properties": {
        "doc_kind": {"type": "string", "enum": ["notes", "decision_record"]},
        "entities": {"type": "array", "maxItems": 2, "items": {"type": "string", "maxLength": 10}},
    },
}

VALID = {"doc_kind": "notes", "entities": ["Neo4j"]}
INVALID = {"doc_kind": "spreadsheet", "entities": ["Neo4j"]}


def _payload(
    tool_input: dict[str, Any] | None,
    *,
    text: str = "",
    stop_reason: str = "tool_use",
    tokens_in: int = 1200,
    tokens_out: int = 300,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if tool_input is not None:
        content.append(
            {"type": "tool_use", "name": EXTRACTION_TOOL_NAME, "id": "t1", "input": tool_input}
        )
    if text:
        content.append({"type": "text", "text": text})
    return {
        "content": content,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out},
    }


class StubClient:
    """Minimal stand-in for a ``bedrock-runtime`` client."""

    def __init__(self, payloads: list[dict[str, Any]] | Exception) -> None:
        self._payloads = payloads
        self.bodies: list[dict[str, Any]] = []
        self.model_ids: list[str] = []

    def invoke_model(self, **kwargs: Any) -> dict[str, Any]:
        self.bodies.append(json.loads(kwargs["body"]))
        self.model_ids.append(kwargs["modelId"])
        if isinstance(self._payloads, Exception):
            raise self._payloads
        payload = self._payloads[min(len(self.bodies) - 1, len(self._payloads) - 1)]
        return {"body": io.BytesIO(json.dumps(payload).encode())}


def _provider(payloads: list[dict[str, Any]] | Exception, **overrides: Any) -> BedrockProvider:
    settings = LLMSettings(**overrides)
    return BedrockProvider(settings, client=StubClient(payloads))


def test_satisfies_the_frozen_port() -> None:
    assert isinstance(_provider([_payload(VALID)]), LLMProvider)


def test_forced_tool_use_is_what_carries_the_schema() -> None:
    """ADR-0012 point 5: the schema is a tool ``input_schema``, not prose in the prompt."""
    provider = _provider([_payload(VALID)])
    provider.complete_json("extract this", SCHEMA, system="be terse")
    body = provider.client.bodies[0]

    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": EXTRACTION_TOOL_NAME}
    assert body["system"] == "be terse"
    assert body["temperature"] == 0.0
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert json.dumps(SCHEMA) not in json.dumps(body["messages"])


def test_inference_profile_id_is_used_verbatim() -> None:
    provider = _provider([_payload(VALID)])
    provider.complete_json("x", SCHEMA)
    assert provider.client.model_ids == ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]


def test_valid_first_attempt_populates_metrics() -> None:
    traced = _provider([_payload(VALID)]).complete_json_traced("x", SCHEMA)
    response = traced.response

    assert response.valid is True
    assert response.parsed == VALID
    assert response.attempts == 1
    assert response.prompt_tokens == 1200
    assert response.completion_tokens == 300
    assert len(traced.traces) == 1
    assert traced.traces[0].stop_reason == "tool_use"
    assert traced.traces[0].valid is True
    assert traced.traces[0].prompt_tok_s is None  # documented as unavailable on Bedrock
    assert traced.traces[0].gen_tok_s is not None


def test_schema_violation_in_the_tool_input_is_retried_with_error_feedback() -> None:
    provider = _provider([_payload(INVALID), _payload(VALID)])
    traced = provider.complete_json_traced("x", SCHEMA, max_retries=2)

    assert traced.response.valid is True
    assert traced.response.attempts == 2
    assert traced.traces[0].valid is False
    assert traced.traces[1].valid is True

    retry_messages = provider.client.bodies[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["user", "assistant", "user"]
    assert "did not satisfy the schema" in retry_messages[-1]["content"]


def test_retries_are_capped_and_failure_is_reported_not_raised() -> None:
    provider = _provider([_payload(INVALID)])
    traced = provider.complete_json_traced("x", SCHEMA, max_retries=2)

    assert traced.response.valid is False
    assert traced.response.parsed is None
    assert traced.response.attempts == 3  # 1 + max_retries
    assert len(provider.client.bodies) == 3
    assert traced.response.errors


def test_missing_tool_use_block_is_a_failure_not_a_parse_attempt() -> None:
    provider = _provider([_payload(None, text="```json\n{}\n```", stop_reason="end_turn")])
    traced = provider.complete_json_traced("x", SCHEMA, max_retries=0)

    assert traced.response.valid is False
    assert traced.traces[0].json_decoded is False
    assert "no tool_use block returned" in traced.traces[0].errors


def test_model_identity_is_what_provenance_stores() -> None:
    identity = _provider([_payload(VALID)]).model_identity()

    assert identity.provider == "bedrock"
    assert identity.name == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert identity.id == "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert identity.digest is None
    assert identity.parameters["structured_output"] == "forced_tool_use"
    assert identity.parameters["region"] == "us-east-1"


def test_transport_failure_is_wrapped_and_carries_no_aws_payload() -> None:
    provider = _provider(RuntimeError("AccessDenied: arn:aws:sts::123456789012:assumed-role/x"))
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete_json("x", SCHEMA)

    message = str(excinfo.value)
    assert "bedrock invoke failed" in message
    assert "arn:aws" not in message
    assert "123456789012" not in message


def test_health_is_false_when_the_endpoint_errors() -> None:
    assert _provider(RuntimeError("boom")).health() is False
    assert _provider([_payload(None, text="pong", stop_reason="end_turn")]).health() is True
