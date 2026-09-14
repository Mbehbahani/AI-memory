"""P4-T01 unit tests for :class:`aimemory.providers.llm.OllamaProvider`. HTTP is fully mocked.

No container is needed: every request is answered by an ``httpx.MockTransport`` handler, so these run
in CI and in the ``tools`` image without Ollama. The live counterpart is ``test_ollama_live.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from aimemory.common.config import LLMSettings
from aimemory.common.errors import LLMProviderError
from aimemory.domain.ports import LLMProvider
from aimemory.providers.llm import OllamaProvider

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["doc_kind", "entities"],
    "properties": {
        "doc_kind": {"type": "string", "enum": ["notes", "decision_record"]},
        "entities": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string", "maxLength": 10},
        },
    },
}

VALID = {"doc_kind": "notes", "entities": ["Neo4j"]}

TAGS = {
    "models": [
        {"model": "qwen3:4b", "name": "qwen3:4b", "digest": "359d7dd4bcda" + "0" * 52},
        {"model": "other:1b", "name": "other:1b", "digest": "ff" * 32},
    ]
}

SHOW = {"details": {"quantization_level": "Q4_K_M", "parameter_size": "4.0B", "family": "qwen3"}}


def _settings(**overrides: Any) -> LLMSettings:
    base = {
        "LLM_MODEL": "qwen3:4b",
        "OLLAMA_URL": "http://ollama:11434",
        "LLM_NUM_CTX": 8192,
        "LLM_NUM_PREDICT": 1024,
        "LLM_TEMPERATURE": 0.0,
        "LLM_TIMEOUT_SECONDS": 300,
        "LLM_MAX_RETRIES": 2,
        "OLLAMA_KEEP_ALIVE": "5m",
    }
    base.update(overrides)
    return LLMSettings(**base)  # type: ignore[arg-type]


def _chat_reply(content: str, **counters: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "qwen3:4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
        "total_duration": 2_000_000_000,
        "load_duration": 100_000_000,
        "prompt_eval_count": 812,
        "prompt_eval_duration": 20_000_000_000,
        "eval_count": 240,
        "eval_duration": 26_000_000_000,
    }
    payload.update(counters)
    return payload


def _provider(handler: Any, **overrides: Any) -> tuple[OllamaProvider, list[dict[str, Any]]]:
    """Build a provider whose transport is ``handler``; returns it plus the captured request bodies."""
    captured: list[dict[str, Any]] = []

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        captured.append({"path": request.url.path, "method": request.method, "body": body})
        return handler(request, body)

    client = httpx.Client(
        transport=httpx.MockTransport(_transport_handler), base_url="http://ollama:11434"
    )
    return OllamaProvider(_settings(**overrides), client=client), captured


def _scripted(contents: list[str]) -> Any:
    """Answer ``/api/chat`` with ``contents`` in order; ``/api/tags`` and ``/api/show`` statically."""
    remaining = list(contents)

    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=TAGS)
        if request.url.path == "/api/show":
            return httpx.Response(200, json=SHOW)
        if request.url.path == "/api/chat":
            content = remaining.pop(0) if remaining else contents[-1]
            return httpx.Response(200, json=_chat_reply(content))
        return httpx.Response(404, json={"error": "not found"})

    return handler


# --------------------------------------------------------------------------------------------------
# port conformance and request shape (plan section M)
# --------------------------------------------------------------------------------------------------


def test_provider_satisfies_the_frozen_llm_provider_port() -> None:
    provider, _ = _provider(_scripted([json.dumps(VALID)]))
    assert isinstance(provider, LLMProvider)


def test_chat_request_matches_plan_section_m() -> None:
    provider, captured = _provider(_scripted([json.dumps(VALID)]))
    provider.complete_json("extract this", SCHEMA, system="you are an extractor")

    chat = next(c for c in captured if c["path"] == "/api/chat")
    body = chat["body"]
    assert body["model"] == "qwen3:4b"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["keep_alive"] == "5m"
    assert body["options"] == {"temperature": 0.0, "num_ctx": 8192, "num_predict": 1024}
    # the frozen schema is passed verbatim, not rewritten
    assert body["format"] == SCHEMA
    assert body["messages"] == [
        {"role": "system", "content": "you are an extractor"},
        {"role": "user", "content": "extract this"},
    ]


def test_complete_text_sends_no_format_key() -> None:
    provider, captured = _provider(_scripted(["a plain summary"]))
    response = provider.complete_text("summarize")
    chat = next(c for c in captured if c["path"] == "/api/chat")
    assert "format" not in chat["body"]
    assert response.text == "a plain summary"
    assert response.parsed is None
    assert response.attempts == 1


# --------------------------------------------------------------------------------------------------
# happy path and metrics (ADR-0010 depends on attempts / duration_ms)
# --------------------------------------------------------------------------------------------------


def test_valid_first_attempt_records_metrics() -> None:
    provider, _ = _provider(_scripted([json.dumps(VALID)]))
    response = provider.complete_json("extract", SCHEMA)

    assert response.valid is True
    assert response.parsed == VALID
    assert response.attempts == 1
    assert response.duration_ms >= 0
    assert response.prompt_tokens == 812
    assert response.completion_tokens == 240
    assert response.model == "qwen3:4b"
    assert response.model_digest is not None and response.model_digest.startswith("359d7dd4bcda")
    assert response.errors == []


def test_traces_expose_the_raw_ollama_counters() -> None:
    provider, _ = _provider(_scripted([json.dumps(VALID)]))
    traced = provider.complete_json_traced("extract", SCHEMA)

    assert len(traced.traces) == 1
    trace = traced.traces[0]
    assert trace.prompt_eval_count == 812
    assert trace.eval_count == 240
    assert trace.prompt_tok_s == pytest.approx(812 / 20.0, rel=1e-6)
    assert trace.gen_tok_s == pytest.approx(240 / 26.0, rel=1e-6)
    assert trace.load_ms == pytest.approx(100.0)
    assert trace.done_reason == "stop"


# --------------------------------------------------------------------------------------------------
# retry with error feedback (max 2)
# --------------------------------------------------------------------------------------------------


def test_unparseable_json_is_retried_with_the_error_fed_back() -> None:
    provider, captured = _provider(_scripted(['{"doc_kind": "notes", ', json.dumps(VALID)]))
    response = provider.complete_json("extract", SCHEMA)

    assert response.valid is True
    assert response.attempts == 2
    chats = [c["body"] for c in captured if c["path"] == "/api/chat"]
    assert len(chats) == 2
    retry_messages = chats[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["user", "assistant", "user"]
    assert "invalid JSON" in retry_messages[-1]["content"]


def test_schema_violation_is_detected_even_though_the_json_parses() -> None:
    bad = json.dumps({"doc_kind": "manifesto", "entities": ["Neo4j"]})
    provider, captured = _provider(_scripted([bad, json.dumps(VALID)]))
    response = provider.complete_json("extract", SCHEMA)

    assert response.attempts == 2
    assert response.valid is True
    retry = [c["body"] for c in captured if c["path"] == "/api/chat"][1]
    assert "doc_kind" in retry["messages"][-1]["content"]


def test_persistent_failure_returns_invalid_after_two_retries_without_raising() -> None:
    bad = json.dumps({"doc_kind": "notes", "unexpected": 1, "entities": []})
    provider, captured = _provider(_scripted([bad, bad, bad, bad]))
    response = provider.complete_json("extract", SCHEMA)

    assert response.valid is False
    assert response.parsed is None
    assert response.attempts == 3  # 1 + LLM_MAX_RETRIES
    assert len([c for c in captured if c["path"] == "/api/chat"]) == 3
    assert response.errors and all(isinstance(e, str) for e in response.errors)


def test_max_retries_override_is_honoured() -> None:
    bad = json.dumps({"doc_kind": "notes"})
    provider, captured = _provider(_scripted([bad, bad, bad]))
    response = provider.complete_json("extract", SCHEMA, max_retries=0)

    assert response.attempts == 1
    assert response.valid is False
    assert len([c for c in captured if c["path"] == "/api/chat"]) == 1


def test_truncated_array_item_violating_maxlength_is_invalid() -> None:
    long_item = json.dumps({"doc_kind": "notes", "entities": ["x" * 40]})
    provider, _ = _provider(_scripted([long_item, long_item, long_item]))
    response = provider.complete_json("extract", SCHEMA)
    assert response.valid is False


# --------------------------------------------------------------------------------------------------
# identity, health, failures
# --------------------------------------------------------------------------------------------------


def test_model_identity_carries_the_digest_and_effective_parameters() -> None:
    provider, _ = _provider(_scripted([json.dumps(VALID)]))
    identity = provider.model_identity()

    assert identity.id == "qwen3-4b"
    assert identity.provider == "ollama"
    assert identity.name == "qwen3:4b"
    assert identity.digest == "359d7dd4bcda" + "0" * 52
    assert identity.parameters["num_ctx"] == 8192
    assert identity.parameters["think"] is False
    assert identity.parameters["model_quantization_level"] == "Q4_K_M"


def test_health_is_true_only_when_the_configured_model_is_present() -> None:
    provider, _ = _provider(_scripted([json.dumps(VALID)]))
    assert provider.health() is True

    missing, _ = _provider(_scripted([json.dumps(VALID)]), LLM_MODEL="qwen3:14b")
    assert missing.health() is False


def test_unreachable_ollama_makes_health_false_and_calls_raise_sanitized() -> None:
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider, _ = _provider(handler)
    assert provider.health() is False
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete_json("extract", SCHEMA)
    assert "connection refused" not in str(excinfo.value)  # public message is sanitized


def test_timeout_raises_llm_provider_error() -> None:
    def handler(request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if request.url.path == "/api/chat":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=TAGS)

    provider, _ = _provider(handler)
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete_json("extract", SCHEMA)
    assert "in time" in str(excinfo.value)


def test_empty_content_counts_as_invalid_and_is_retried() -> None:
    provider, _captured = _provider(_scripted(["", "", json.dumps(VALID)]))
    response = provider.complete_json("extract", SCHEMA)
    assert response.attempts == 3
    assert response.valid is True
