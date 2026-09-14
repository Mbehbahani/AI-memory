"""Claude Haiku 4.5 on AWS Bedrock as an :class:`~aimemory.domain.ports.LLMProvider` (ADR-0012).

The second of two extraction providers. Neither this nor :class:`~.ollama_provider.OllamaProvider`
is the reference implementation: ``LLM_PROVIDER`` selects one, both are measured on the same frozen
benchmark set (ADR-0010), and provenance records which produced each row.

**Selecting this provider sends source text to AWS.** Assumption B16 ("no cloud calls at runtime")
does not hold when it is active. That is an owner decision recorded in ADR-0012, not a default.

Strict JSON
-----------
Bedrock has no equivalent of Ollama's ``format=<json schema>``. MEASURED 2026-09-14: a plain prompt
asking for JSON returns it wrapped in a markdown fence. So this provider does not prompt-and-parse -
it passes the frozen extraction schema as a **tool** ``input_schema`` and forces
``tool_choice={"type": "tool", "name": ...}``. The model must then emit a ``tool_use`` block whose
``input`` conforms to the schema, and we read the object straight out of it. The same
``schemas/extraction/*.json`` files are used verbatim by both providers.

Credentials
-----------
Never held in settings or code. ``boto3``'s standard chain resolves them - a read-only mounted
``~/.aws``, or ``AWS_*`` environment variables. Nothing AWS-related is logged; errors are sanitized
before they leave this module because a botocore exception can echo request metadata.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Self

from aimemory.common.config import LLMSettings
from aimemory.common.errors import LLMProviderError
from aimemory.domain.models import ExtractionModel
from aimemory.domain.ports import LLMResponse

from .validation import validate_json

#: Name given to the forced tool. Arbitrary, but stable so traces are comparable across runs.
EXTRACTION_TOOL_NAME = "emit_extraction"


@dataclass(slots=True)
class BedrockCallTrace:
    """Raw counters for one Bedrock ``InvokeModel`` round-trip (one attempt). All MEASURED.

    Deliberately mirrors :class:`~.ollama_provider.CallTrace` so the P4-T02 harness can tabulate both
    providers in one table. Bedrock reports token counts but no load or per-phase timings, so
    ``load_ms`` is always 0.0 and the eval/prompt split is derived from ``usage`` rather than from
    server-side timers - see :attr:`gen_tok_s`.
    """

    attempt: int
    wall_ms: float
    prompt_eval_count: int
    eval_count: int
    valid: bool
    json_decoded: bool
    load_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    content_chars: int = 0
    stop_reason: str | None = None

    @property
    def prompt_tok_s(self) -> float | None:
        """UNKNOWN for Bedrock: the API does not separate prompt-eval time from generation time."""
        return None

    @property
    def gen_tok_s(self) -> float | None:
        """Output tokens over total wall time - an *under*-estimate of raw generation speed.

        It includes network round trip and prompt processing, so it is not comparable to Ollama's
        ``eval_count / eval_duration``. Labelled accordingly wherever it is reported.
        """
        if self.wall_ms <= 0 or self.eval_count <= 0:
            return None
        return self.eval_count / (self.wall_ms / 1000.0)


@dataclass(slots=True)
class TracedBedrockResponse:
    """A :class:`LLMResponse` plus the per-attempt traces (P4-T02 harness input)."""

    response: LLMResponse
    traces: list[BedrockCallTrace]


class BedrockProvider:
    """Claude Haiku 4.5 via Bedrock. One instance per process; the boto3 client is thread-safe."""

    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self._settings = settings or LLMSettings()
        self._client = client
        self._resolved_model: str | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    def close(self) -> None:
        """boto3 clients hold a pooled connection; dropping the reference is enough."""
        self._client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def settings(self) -> LLMSettings:
        return self._settings

    @property
    def client(self) -> Any:
        """Lazily built so importing this module never requires boto3 or credentials."""
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415 - optional extra, imported on first use
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise LLMProviderError(
                    "boto3 is required for LLM_PROVIDER=bedrock. Install the 'bedrock' extra."
                ) from exc
            session_kwargs: dict[str, Any] = {}
            if self._settings.bedrock_profile:
                session_kwargs["profile_name"] = self._settings.bedrock_profile
            session = boto3.Session(**session_kwargs)
            self._client = session.client(
                "bedrock-runtime", region_name=self._settings.bedrock_region
            )
        return self._client

    # -- LLMProvider -----------------------------------------------------------------------------

    def complete_json(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> LLMResponse:
        return self.complete_json_traced(
            prompt, json_schema, system=system, max_retries=max_retries
        ).response

    def complete_text(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        """Unconstrained generation. Summaries only - never structured knowledge (plan section M)."""
        started = time.perf_counter()
        body = self._body(prompt, system=system)
        payload = self._invoke(body)
        text = "".join(
            block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
        )
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            parsed=None,
            model=self._settings.bedrock_model_id,
            model_digest=None,
            attempts=1,
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            valid=True,
        )

    def model_identity(self) -> ExtractionModel:
        """Stamped on every artifact this provider produces, so provenance stays honest."""
        return ExtractionModel(
            id=f"bedrock:{self._settings.bedrock_model_id}",
            provider="bedrock",
            name=self._settings.bedrock_model_id,
            digest=None,  # Bedrock exposes no content digest for a managed model.
            parameters={
                "region": self._settings.bedrock_region,
                "max_tokens": self._settings.bedrock_max_tokens,
                "temperature": self._settings.temperature,
                "structured_output": "forced_tool_use",
            },
        )

    def health(self) -> bool:
        """True when the model answers. Cheap but *not* free - it is a billed one-token call."""
        try:
            self._invoke(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "ping"}],
                }
            )
        except Exception:  # noqa: BLE001 - health must never raise
            return False
        return True

    # -- traced path (P4-T02) --------------------------------------------------------------------

    def complete_json_traced(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> TracedBedrockResponse:
        """Schema-constrained generation with per-attempt traces.

        Forced tool use makes a schema violation unlikely rather than impossible - the model can
        still omit an optional-but-required-by-us field in edge cases - so the same validate-and-retry
        loop as the Ollama path is kept, feeding the validation errors back as a user turn.
        """
        retries = self._settings.max_retries if max_retries is None else max_retries
        traces: list[BedrockCallTrace] = []
        errors: list[str] = []
        raw_text = ""
        parsed: dict[str, Any] | None = None
        started_all = time.perf_counter()

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        for attempt in range(1, retries + 2):
            body = self._body(
                messages=messages,
                system=system,
                tool_schema=json_schema,
            )
            t0 = time.perf_counter()
            payload = self._invoke(body)
            wall_ms = (time.perf_counter() - t0) * 1000

            usage = payload.get("usage") or {}
            candidate, raw_text = self._tool_input(payload)
            decoded = candidate is not None
            errors = (
                validate_json(candidate, json_schema) if decoded else ["no tool_use block returned"]
            )
            valid = decoded and not errors

            traces.append(
                BedrockCallTrace(
                    attempt=attempt,
                    wall_ms=wall_ms,
                    prompt_eval_count=int(usage.get("input_tokens") or 0),
                    eval_count=int(usage.get("output_tokens") or 0),
                    valid=valid,
                    json_decoded=decoded,
                    errors=list(errors),
                    content_chars=len(raw_text),
                    stop_reason=payload.get("stop_reason"),
                )
            )

            if valid:
                parsed = candidate
                break

            if attempt <= retries:
                # Feed the failure back, exactly as the Ollama path does, so the two providers are
                # retried on equal terms and the attempt counts stay comparable.
                messages = [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": raw_text or "(no output)"},
                    {
                        "role": "user",
                        "content": (
                            "That did not satisfy the schema: "
                            + "; ".join(errors[:10])
                            + ". Emit the tool call again, corrected."
                        ),
                    },
                ]

        total = traces[-1] if traces else None
        return TracedBedrockResponse(
            response=LLMResponse(
                text=raw_text,
                parsed=parsed,
                model=self._settings.bedrock_model_id,
                model_digest=None,
                attempts=len(traces),
                duration_ms=int((time.perf_counter() - started_all) * 1000),
                prompt_tokens=total.prompt_eval_count if total else None,
                completion_tokens=total.eval_count if total else None,
                valid=parsed is not None,
                errors=list(errors) if parsed is None else [],
            ),
            traces=traces,
        )

    # -- internals -------------------------------------------------------------------------------

    def _body(
        self,
        prompt: str | None = None,
        *,
        messages: list[dict[str, Any]] | None = None,
        system: str | None = None,
        tool_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self._settings.bedrock_max_tokens,
            "temperature": self._settings.temperature,
            "messages": messages or [{"role": "user", "content": prompt or ""}],
        }
        if system:
            body["system"] = system
        if tool_schema is not None:
            body["tools"] = [
                {
                    "name": EXTRACTION_TOOL_NAME,
                    "description": "Return the extraction result conforming to the schema.",
                    "input_schema": tool_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": EXTRACTION_TOOL_NAME}
        return body

    @staticmethod
    def _tool_input(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Pull the forced tool call's ``input`` out of the response.

        Returns ``(object_or_None, raw_text)``. ``raw_text`` is what gets stored on the failure
        record, so it is populated even when no tool block came back.
        """
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == EXTRACTION_TOOL_NAME:
                value = block.get("input")
                return (value if isinstance(value, dict) else None), json.dumps(value, default=str)
        text = "".join(
            block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
        )
        return None, text

    def _invoke(self, body: dict[str, Any]) -> dict[str, Any]:
        """One ``InvokeModel`` call, with errors sanitized before they escape this module."""
        try:
            response = self.client.invoke_model(
                modelId=self._settings.bedrock_model_id,
                body=json.dumps(body).encode(),
                contentType="application/json",
                accept="application/json",
            )
            return json.loads(response["body"].read())
        except LLMProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore raises a wide, unstable set
            # Deliberately does not interpolate the exception's full payload: botocore error strings
            # can carry request metadata, and this message reaches logs and the Ops page.
            raise LLMProviderError(
                f"bedrock invoke failed for {self._settings.bedrock_model_id} "
                f"in {self._settings.bedrock_region}: {type(exc).__name__}"
            ) from exc
