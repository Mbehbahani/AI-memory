"""Ollama adapter for the frozen :class:`aimemory.domain.ports.LLMProvider` port (P4-T01, A05).

Plan section M, verbatim: ``POST /api/chat`` with ``format=<JSON schema>``, ``think=false``,
``temperature=0``, ``num_ctx`` / ``num_predict`` from :class:`~aimemory.common.config.LLMSettings`,
a request timeout, and at most ``max_retries`` (default 2) retries that feed the *validation error*
back to the model as a new turn. There is no cloud fallback: if qwen3:4b cannot produce a
schema-valid object after the retries, the call returns ``valid=False`` with sanitized errors and the
caller marks the episode ``failed`` (the Tier 1 vectors survive either way).

``schemas/extraction/*.json`` are passed **verbatim** as ``format``; the provider never rewrites a
schema, because the same document is the contract A02 froze and the thing A08's engine validates
against.

Every response carries ``attempts`` and ``duration_ms`` - ADR-0010's extraction metrics are computed
from them, so they are populated on failures too. :meth:`OllamaProvider.complete_json_traced` exposes
the per-attempt Ollama counters (``load_duration``, ``prompt_eval_count``/``prompt_eval_duration``,
``eval_count``/``eval_duration``) that ``tests/evaluation/local_ai`` turns into the P4-T02 table.

Consumers: A08 (native engine), A07a (ingestion), A16 (ops health).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Self

import httpx

from aimemory.common.config import LLMSettings
from aimemory.common.errors import LLMProviderError
from aimemory.domain.models import ExtractionModel
from aimemory.domain.ports import LLMResponse

from .validation import VALIDATOR_BACKEND, validate_json

__all__ = [
    "VALIDATOR_BACKEND",
    "CallTrace",
    "OllamaProvider",
    "TracedResponse",
    "default_model_slug",
]

_RETRY_HINT = (
    "Your previous reply did not satisfy the required JSON Schema. "
    "Reply again with a single JSON object that satisfies the schema. "
    "Do not apologise and do not explain; output JSON only."
)


def default_model_slug(model_name: str) -> str:
    """``qwen3:4b`` -> ``qwen3-4b`` - the ``extraction_models.id`` A04 persists."""
    return model_name.replace(":", "-").replace("/", "-").replace(".", "-").lower()


@dataclass(slots=True)
class CallTrace:
    """Raw counters for one HTTP round-trip to ``/api/chat`` (one attempt). All MEASURED."""

    attempt: int
    wall_ms: float
    load_ms: float
    prompt_eval_count: int
    prompt_eval_ms: float
    eval_count: int
    eval_ms: float
    total_ms: float
    valid: bool
    json_decoded: bool
    errors: list[str] = field(default_factory=list)
    content_chars: int = 0
    done_reason: str | None = None

    @property
    def prompt_tok_s(self) -> float | None:
        if self.prompt_eval_ms <= 0 or self.prompt_eval_count <= 0:
            return None
        return self.prompt_eval_count / (self.prompt_eval_ms / 1000.0)

    @property
    def gen_tok_s(self) -> float | None:
        if self.eval_ms <= 0 or self.eval_count <= 0:
            return None
        return self.eval_count / (self.eval_ms / 1000.0)


@dataclass(slots=True)
class TracedResponse:
    """A :class:`LLMResponse` plus the per-attempt traces (P4-T02 harness input)."""

    response: LLMResponse
    traces: list[CallTrace]


class OllamaProvider:
    """The V0.1 :class:`~aimemory.domain.ports.LLMProvider`. One instance per process."""

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or LLMSettings()
        self._client = client or httpx.Client(
            base_url=self._settings.ollama_url.rstrip("/"),
            timeout=httpx.Timeout(float(self._settings.timeout_seconds), connect=10.0),
        )
        self._owns_client = client is None
        self._identity: ExtractionModel | None = None

    # ------------------------------------------------------------------------------- lifecycle

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def settings(self) -> LLMSettings:
        return self._settings

    # ------------------------------------------------------------------------------------ port

    def complete_json(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> LLMResponse:
        """Generate a JSON object constrained by ``json_schema``; retry with error feedback."""
        return self.complete_json_traced(
            prompt, json_schema, system=system, max_retries=max_retries
        ).response

    def complete_text(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        """Unconstrained generation. Summaries only - never structured knowledge."""
        messages = self._messages(prompt, system)
        started = time.perf_counter()
        payload = self._chat(messages, schema=None)
        content = self._content(payload)
        return LLMResponse(
            text=content,
            parsed=None,
            model=self._settings.model,
            model_digest=self._digest(),
            attempts=1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=payload.get("prompt_eval_count"),
            completion_tokens=payload.get("eval_count"),
            valid=True,
        )

    def model_identity(self) -> ExtractionModel:
        """``qwen3:4b`` + Ollama digest + the effective decoding parameters (plan section M)."""
        if self._identity is not None:
            return self._identity
        try:
            show = self._request("POST", "/api/show", json={"model": self._settings.model})
            details = show.get("details") or {}
        except LLMProviderError:
            details = {}
        parameters: dict[str, str | int | float | bool] = {
            "temperature": self._settings.temperature,
            "num_ctx": self._settings.num_ctx,
            "num_predict": self._settings.num_predict,
            "think": False,
            "format": "json_schema",
            "keep_alive": self._settings.keep_alive,
        }
        for key in ("quantization_level", "parameter_size", "family", "format"):
            value = details.get(key)
            if isinstance(value, (str, int, float, bool)):
                parameters[f"model_{key}"] = value
        self._identity = ExtractionModel(
            id=default_model_slug(self._settings.model),
            provider="ollama",
            name=self._settings.model,
            digest=self._digest(),
            parameters=parameters,
        )
        return self._identity

    def health(self) -> bool:
        """True when Ollama answers and the configured model is listed by ``/api/tags``."""
        try:
            tags = self._request("GET", "/api/tags")
        except LLMProviderError:
            return False
        names = {m.get("model") or m.get("name") for m in tags.get("models", [])}
        return self._settings.model in names

    # -------------------------------------------------------------------------------- extended

    def complete_json_traced(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> TracedResponse:
        """:meth:`complete_json` plus the raw per-attempt counters used by the P4-T02 harness."""
        retries = self._settings.max_retries if max_retries is None else max_retries
        messages = self._messages(prompt, system)
        traces: list[CallTrace] = []
        started = time.perf_counter()
        content = ""
        parsed: dict[str, Any] | None = None
        errors: list[str] = ["no attempt was made"]
        payload: dict[str, Any] = {}

        for attempt in range(1, retries + 2):
            call_start = time.perf_counter()
            payload = self._chat(messages, schema=json_schema)
            wall_ms = (time.perf_counter() - call_start) * 1000
            content = self._content(payload)
            parsed, errors = self._decode_and_validate(content, json_schema)
            ok = parsed is not None and not errors
            traces.append(
                CallTrace(
                    attempt=attempt,
                    wall_ms=wall_ms,
                    load_ms=float(payload.get("load_duration") or 0) / 1e6,
                    prompt_eval_count=int(payload.get("prompt_eval_count") or 0),
                    prompt_eval_ms=float(payload.get("prompt_eval_duration") or 0) / 1e6,
                    eval_count=int(payload.get("eval_count") or 0),
                    eval_ms=float(payload.get("eval_duration") or 0) / 1e6,
                    total_ms=float(payload.get("total_duration") or 0) / 1e6,
                    valid=ok,
                    json_decoded=parsed is not None,
                    errors=list(errors),
                    content_chars=len(content),
                    done_reason=payload.get("done_reason"),
                )
            )
            if ok:
                break
            if attempt <= retries:
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": _RETRY_HINT
                        + "\nValidation errors:\n- "
                        + "\n- ".join(errors[:5]),
                    },
                ]

        valid = parsed is not None and not errors
        response = LLMResponse(
            text=content,
            parsed=parsed if valid else None,
            model=self._settings.model,
            model_digest=self._digest(),
            attempts=max(1, len(traces)),
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=payload.get("prompt_eval_count"),
            completion_tokens=payload.get("eval_count"),
            valid=valid,
            errors=[] if valid else errors[:5],
        )
        return TracedResponse(response=response, traces=traces)

    # ------------------------------------------------------------------------------- internals

    @staticmethod
    def _messages(prompt: str, system: str | None) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return messages

    @staticmethod
    def _decode_and_validate(
        content: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        text = content.strip()
        if not text:
            return None, ["empty response"]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, [f"invalid JSON: {exc.msg} at position {exc.pos}"]
        if not isinstance(parsed, dict):
            return None, [f"expected a JSON object, got {type(parsed).__name__}"]
        return parsed, validate_json(parsed, schema)

    @staticmethod
    def _content(payload: dict[str, Any]) -> str:
        message = payload.get("message") or {}
        return str(message.get("content") or "")

    def _chat(
        self, messages: list[dict[str, str]], *, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": self._settings.keep_alive,
            "options": {
                "temperature": self._settings.temperature,
                "num_ctx": self._settings.num_ctx,
                "num_predict": self._settings.num_predict,
            },
        }
        if schema is not None:
            body["format"] = schema
        return self._request("POST", "/api/chat", json=body)

    def _digest(self) -> str | None:
        if self._identity is not None:
            return self._identity.digest
        try:
            tags = self._request("GET", "/api/tags")
        except LLMProviderError:
            return None
        for model in tags.get("models", []):
            if (model.get("model") or model.get("name")) == self._settings.model:
                digest = model.get("digest")
                return str(digest) if digest else None
        return None

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=json)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as exc:
            raise LLMProviderError(
                "The local model did not answer in time.",
                detail=f"ollama {method} {path} timed out after "
                f"{self._settings.timeout_seconds}s: {exc}",
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(
                "The local model is unavailable.",
                detail=f"ollama {method} {path} failed: {exc}",
            ) from exc
        except ValueError as exc:
            raise LLMProviderError(
                "The local model returned an unreadable response.",
                detail=f"ollama {method} {path} returned non-JSON: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise LLMProviderError(
                "The local model returned an unreadable response.",
                detail=f"ollama {method} {path} returned {type(data).__name__}, expected object",
            )
        return data
