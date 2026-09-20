"""An :class:`~aimemory.domain.ports.LLMProvider` whose model is not reachable from this process.

Why this exists
---------------
The extraction engine runs inside the ``ingestion`` container and calls a model over the network -
Ollama on this machine, or Claude Haiku 4.5 through AWS Bedrock. The owner asked for a third route:
the *same* Haiku 4.5, answered by a Claude Code subagent in the operator's session rather than billed
through Bedrock. Same model, same quality; a different delivery path and a different bill.

A subagent cannot be called from inside a container, so the call is turned inside out. Instead of
"send a prompt, block, receive a reply", this provider does:

1. hash the prompt (plus its schema) into a stable key;
2. return the answer if ``<relay>/responses/<key>.json`` already exists;
3. otherwise write ``<relay>/requests/<key>.json`` and raise :class:`RelayPending`.

The operator fills the responses between runs. Extraction is then run again and the same prompts hash
to the same keys, so every answered call now succeeds. The engine, entity resolution, the temporal
rules, the registry veto and provenance are all unchanged - this swaps the model connection and
nothing else, which is the whole point of doing it at this seam rather than by writing facts directly.

Why hashing the prompt is safe
------------------------------
The prompt is fully determined by the episode body, the ontology and the entity list, and generation
is ``temperature=0``. Identical prompt means identical intended answer, so a key collision is not a
correctness risk - it is a cache hit. Change the prompt text and every key changes, which is the
behaviour you want: old answers stop applying instead of being silently reused.

What this is not
----------------
Not a cache in front of a working provider, and not a fallback. If a response is missing the call
*fails*; it never guesses, returns empty, or quietly degrades. A half-extracted episode that reports
success is exactly the failure this system is built to avoid.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ...common.config import Settings, get_settings
from ...common.logging import get_logger
from ...domain.models import ExtractionModel
from ...domain.ports import LLMResponse

__all__ = ["RelayPending", "RelayProvider", "MODEL_ID", "relay_dir"]

logger = get_logger(__name__)

#: Registered in ``extraction_models`` before this provider writes anything. Distinct from the
#: Bedrock row even though the underlying model is the same Haiku 4.5: provenance records *how* a
#: fact was produced, and "which route answered this" is the question a cost or incident review asks.
MODEL_ID = "claude-code:haiku-4-5"
MODEL_NAME = "claude-haiku-4-5"
PROVIDER_NAME = "claude-code"

#: Default relay location. A bind mount, not the ``ingestion_state`` volume, so the operator can read
#: and write these files directly instead of through ``docker compose exec`` for every one of them.
DEFAULT_RELAY_DIR = "/relay"


class RelayPending(RuntimeError):
    """No answer on disk yet. Carries the key so the caller can report what is outstanding."""

    def __init__(self, key: str, path: Path) -> None:
        super().__init__(f"relay response missing for {key}")
        self.key = key
        self.path = path


def relay_dir(settings: Settings | None = None) -> Path:
    return Path(os.environ.get("LLM_RELAY_DIR") or DEFAULT_RELAY_DIR)


def _key(prompt: str, schema: dict[str, Any] | None, system: str | None) -> str:
    """A stable id for one call. Schema and system prompt are included because both change the task."""
    digest = hashlib.sha256()
    for part in (system or "", prompt, json.dumps(schema or {}, sort_keys=True)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


class RelayProvider:
    """Answers from ``<relay>/responses/``; records unanswered prompts in ``<relay>/requests/``."""

    def __init__(self, settings: Settings | None = None, *, root: Path | None = None) -> None:
        self._settings = settings or get_settings()
        self._root = root or relay_dir(self._settings)
        self._requests = self._root / "requests"
        self._responses = self._root / "responses"

    # ------------------------------------------------------------------ LLMProvider

    def complete_json(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        key = _key(prompt, json_schema, system)
        answer = self._read_response(key)
        if answer is None:
            self._write_request(key, prompt, json_schema, system)
            raise RelayPending(key, self._requests / f"{key}.json")

        parsed = answer.get("parsed")
        if not isinstance(parsed, dict):
            # A malformed answer is a failure, never an empty success. The engine records the error
            # and leaves the episode unextracted, which is recoverable; a silent empty result is not.
            raise ValueError(f"relay response {key} has no JSON object under 'parsed'")

        # A real provider gets grammar-constrained decoding; an answer written by hand or by a
        # subagent does not, so the schema is checked here. Without this, an answer using an entity
        # type outside the ontology travels three layers before Pydantic rejects it, and the operator
        # sees "entities.8.type -> enum" against an episode id with no way back to the file to fix.
        violation = _schema_violation(parsed, json_schema)
        if violation is not None:
            # Re-record the request with the failure attached and report it as unanswered. That turns
            # a dead end into a correction loop: the next answering pass sees exactly what was wrong.
            self._write_request(key, prompt, json_schema, system, previous_error=violation)
            raise RelayPending(key, self._requests / f"{key}.json")

        return LLMResponse(
            text=answer.get("text") or json.dumps(parsed),
            parsed=parsed,
            model=MODEL_NAME,
            model_digest=None,
            attempts=int(answer.get("attempts") or 1),
            duration_ms=int((time.perf_counter() - started) * 1000),
            prompt_tokens=answer.get("prompt_tokens"),
            completion_tokens=answer.get("completion_tokens"),
            valid=True,
            errors=[],
        )

    def complete_text(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        started = time.perf_counter()
        key = _key(prompt, None, system)
        answer = self._read_response(key)
        if answer is None:
            self._write_request(key, prompt, None, system)
            raise RelayPending(key, self._requests / f"{key}.json")
        return LLMResponse(
            text=str(answer.get("text") or ""),
            parsed=None,
            model=MODEL_NAME,
            duration_ms=int((time.perf_counter() - started) * 1000),
            valid=True,
        )

    def model_identity(self) -> ExtractionModel:
        return ExtractionModel(
            id=MODEL_ID,
            provider=PROVIDER_NAME,
            name=MODEL_NAME,
            digest=None,
            parameters={
                "route": "claude-code-subagent",
                "temperature": 0.0,
                "structured_output": "relay_file",
            },
        )

    def health(self) -> dict[str, Any]:
        """Outstanding vs answered, so the operator can see how much work is left without counting."""
        return {
            "provider": PROVIDER_NAME,
            "model": MODEL_NAME,
            "relay_dir": str(self._root),
            "requests": len(list(self._requests.glob("*.json"))) if self._requests.is_dir() else 0,
            "responses": len(list(self._responses.glob("*.json"))) if self._responses.is_dir() else 0,
        }

    # ------------------------------------------------------------------ files

    def _read_response(self, key: str) -> dict[str, Any] | None:
        path = self._responses / f"{key}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"relay response {key} is unreadable: {type(exc).__name__}") from exc
        return data if isinstance(data, dict) else None

    def _write_request(
        self,
        key: str,
        prompt: str,
        schema: dict[str, Any] | None,
        system: str | None,
        *,
        previous_error: str | None = None,
    ) -> None:
        """Record the prompt for the operator to answer. Idempotent - reruns must not multiply files.

        ``previous_error`` overwrites an existing request on purpose: a rejected answer must replace
        the old prompt so the next pass carries the correction, which is the one case where leaving
        the file untouched would lose information.
        """
        self._requests.mkdir(parents=True, exist_ok=True)
        path = self._requests / f"{key}.json"
        if path.is_file() and previous_error is None:
            return
        payload: dict[str, Any] = {
            "key": key,
            "system": system,
            "prompt": prompt,
            "json_schema": schema,
            "model": MODEL_NAME,
            "instructions": (
                "Answer as claude-haiku-4-5 at temperature 0. Reply with a JSON object conforming to "
                "json_schema and write it to responses/<key>.json as {\"parsed\": <object>}."
            ),
        }
        if previous_error is not None:
            payload["previous_answer_rejected"] = previous_error
            payload["instructions"] += (
                " A previous answer to this prompt was REJECTED for the reason in "
                "'previous_answer_rejected'. Fix exactly that and keep the rest."
            )
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(
            "relay.request_recorded", key=key, chars=len(prompt), retry=previous_error is not None
        )


def _schema_violation(parsed: dict[str, Any], schema: dict[str, Any] | None) -> str | None:
    """First schema error as a short sentence, or ``None`` if the answer conforms.

    A missing ``jsonschema`` is not treated as "valid": it is reported, so the check can never be
    silently skipped. Skipping it would restore the behaviour this function exists to remove.
    """
    if not schema:
        return None
    try:
        import jsonschema  # noqa: PLC0415 - optional at import time, required at call time
    except ImportError:  # pragma: no cover
        return "jsonschema is not installed, so the relay cannot validate this answer"
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        return f"{location}: {exc.message}"[:400]
    except jsonschema.SchemaError as exc:  # pragma: no cover - our own schemas are tested
        return f"schema itself is invalid: {exc.message}"[:200]
    return None
