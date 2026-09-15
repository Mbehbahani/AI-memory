"""``NativeTemporalEngine`` - the V0.1 :class:`~aimemory.domain.ports.KnowledgeEngine` (ADR-0009).

**2-3 LLM calls per episode** (plan section M), against Graphiti's 6-10:

1. ``episode_extraction`` - classification, entities, artifacts, summary;
2. ``relationship_extraction`` - facts between the entities call 1 found;
3. *optional* - one retry of call 2, and only when call 2 produced nothing usable while the document
   clearly has several entities. Never a third distinct prompt, so the call budget stays inside §M.

Provider-agnostic by construction, which is reason 1 of ADR-0009: the engine holds an
:class:`~aimemory.domain.ports.LLMProvider` resolved through
:func:`aimemory.providers.llm.get_provider`, so it runs on Bedrock (the ADR-0014 default) and on
Ollama (the offline mode) with no code path of its own for either. The word "bedrock" and the word
"ollama" do not appear below.

What it does **not** do: write to PostgreSQL or Neo4j. The port contract is explicit
("write nothing to Postgres themselves - the caller persists"), and ADR-0001 keeps PostgreSQL the
system of record. Persistence, entity resolution, the temporal rules and the graph projection are
:mod:`aimemory.knowledge.persist`. The one exception is :meth:`invalidate`, which is a *write* by
definition; it needs a session factory and says so loudly when it does not have one.

Failure is a value, not an exception (plan section L): invalid JSON after the provider's retries
returns ``ExtractionResult(valid=False, errors=[...])`` with **sanitized** errors - never an echo of
the document, which may be a CV or a private note - so the episode is marked ``failed``, the Tier 1
vectors stay untouched, and ``reprocess --failed`` can pick it up later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from ...common.config import Settings, get_settings
from ...common.errors import ConfigurationError
from ...common.logging import get_logger
from ...common.time import ensure_utc, utc_now
from ...domain.enums import EngineKind
from ...domain.extraction import (
    EpisodeExtraction,
    ExtractedFact,
    ExtractionResult,
    RelationshipExtraction,
)
from ...domain.models import Episode, ExtractionModel
from ...domain.ports import EpisodeContext, LLMProvider, LLMResponse
from ...common.ids import normalize_name
from ..prompts import (
    EPISODE_SYSTEM_PROMPT,
    RELATION_SYSTEM_PROMPT,
    episode_prompt,
    relation_prompt,
)
from .schemas import episode_schema, relationship_schema

__all__ = ["MAX_BODY_CHARS", "NativeTemporalEngine"]

logger = get_logger(__name__)

#: Episode text handed to the model. ``LLM_NUM_CTX`` is 8192 tokens; ~8000 characters leaves room for
#: the system prompt, the entity list and the reply without silently truncating the tail of a note.
MAX_BODY_CHARS = 8000


def _sanitize(message: str, *, limit: int = 300) -> str:
    """An error safe to persist: no document text, no values, no secrets - shape only."""
    first_line = str(message).strip().splitlines()[0] if str(message).strip() else "error"
    return first_line[:limit]


def _validation_errors(exc: ValidationError, *, prefix: str) -> list[str]:
    """Pydantic errors reduced to location + type. The *input* is deliberately dropped."""
    out: list[str] = []
    for error in exc.errors()[:10]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        out.append(f"{prefix}: {location or '<root>'} -> {error.get('type', 'invalid')}")
    return out


class NativeTemporalEngine:
    """The ADR-0009 engine. One instance per process; safe to call one episode at a time."""

    kind = EngineKind.NATIVE

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        settings: Settings | None = None,
        session_factory: Callable[[], AbstractContextManager[Any]] | None = None,
        graph: Any = None,
        max_body_chars: int = MAX_BODY_CHARS,
        allow_relationship_retry: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        if provider is None:
            from ...providers.llm import get_provider  # noqa: PLC0415 - avoid an import cycle

            provider = get_provider(self._settings.llm)
        self._provider = provider
        self._session_factory = session_factory
        self._graph = graph
        self._max_body_chars = max_body_chars
        self._allow_retry = allow_relationship_retry
        self._identity: ExtractionModel | None = None

    # ---- identity ---------------------------------------------------------------------------

    def model_identity(self) -> ExtractionModel:
        """The ``extraction_models`` row every fact this engine produces is stamped with (ADR-0014)."""
        if self._identity is None:
            self._identity = self._provider.model_identity()
        return self._identity

    @property
    def extraction_model_id(self) -> str:
        return str(self.model_identity().id)

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    # ---- the port ---------------------------------------------------------------------------

    def process_episode(self, episode: Episode, context: EpisodeContext) -> ExtractionResult:
        """Two (rarely three) schema-constrained calls; returns a pre-persistence result."""
        started = utc_now()
        clock = time.perf_counter()
        model_id = self._safe_model_id()
        body = (episode.body or "")[: self._max_body_chars]
        errors: list[str] = []
        attempts = 0

        def finish(
            *,
            valid: bool,
            extraction: EpisodeExtraction | None = None,
            facts: Sequence[ExtractedFact] = (),
        ) -> ExtractionResult:
            return ExtractionResult(
                episode_id=episode.id,
                engine=EngineKind.NATIVE,
                extraction_model_id=model_id,
                doc_kind=extraction.doc_kind if extraction else None,
                summary=extraction.summary if extraction else None,
                entities=list(extraction.entities) if extraction else [],
                artifacts=list(extraction.artifacts) if extraction else [],
                facts=list(facts),
                valid=valid,
                attempts=max(attempts, 1),
                latency_ms=int((time.perf_counter() - clock) * 1000),
                errors=errors,
                started_at=started,
                finished_at=utc_now(),
            )

        if not body.strip():
            errors.append("episode has no body text")
            return finish(valid=False)

        # ---- call 1 -------------------------------------------------------------------------
        try:
            response = self._provider.complete_json(
                episode_prompt(body, context, title=episode.title),
                episode_schema(),
                system=EPISODE_SYSTEM_PROMPT,
            )
        except Exception as exc:  # noqa: BLE001 - a provider failure is a failed episode, not a crash
            errors.append(_sanitize(f"episode call failed: {type(exc).__name__}: {exc}"))
            return finish(valid=False)
        attempts += getattr(response, "attempts", 1)
        extraction = self._parse(response, EpisodeExtraction, "episode_extraction", errors)
        if extraction is None:
            return finish(valid=False)

        # ---- call 2 (+ one optional retry) --------------------------------------------------
        names = [entity.name for entity in extraction.entities]
        facts: list[ExtractedFact] = []
        if len(names) >= 2:
            facts, used = self._extract_relationships(body, context, episode, names, errors)
            attempts += used

        dropped = 0
        known = {normalize_name(name) for name in names}
        kept: list[ExtractedFact] = []
        for fact in facts:
            if normalize_name(fact.subject) in known and normalize_name(fact.object) in known:
                kept.append(fact)
            else:
                dropped += 1
        if dropped:
            errors.append(
                f"relationship_extraction: dropped {dropped} fact(s) whose endpoints were not in "
                "the call-1 entity list"
            )

        logger.info(
            "native_engine.episode_extracted",
            episode_id=str(episode.id),
            model=model_id,
            entities=len(extraction.entities),
            artifacts=len(extraction.artifacts),
            facts=len(kept),
            dropped_facts=dropped,
            attempts=attempts,
        )
        # Dropped endpoints are a data-quality note, not a failure: the episode's entities, artifacts
        # and remaining facts are all still valid knowledge and must not be thrown away.
        return finish(valid=True, extraction=extraction, facts=kept)

    def invalidate(self, fact_id: UUID, at: datetime, by_episode: UUID | None = None) -> None:
        """ADR-0005 ``close()``: set ``valid_to``, mark ``historical``, record who. Idempotent.

        This is the one method that writes. It needs a session factory; constructing the engine
        without one and then calling it is a wiring bug, and it is reported as one rather than
        silently doing nothing.
        """
        if self._session_factory is None:
            raise ConfigurationError(
                "NativeTemporalEngine.invalidate needs a session factory.",
                detail="construct the engine with session_factory=Database().session",
            )
        from ..temporal import SqlFactStore, close  # noqa: PLC0415 - avoid an import cycle

        with self._session_factory() as session:
            close(
                fact_id,
                at=ensure_utc(at),
                by_episode=by_episode,
                store=SqlFactStore(session),
                reason="invalidate",
                graph=self._graph,
            )

    def health(self) -> bool:
        try:
            return bool(self._provider.health())
        except Exception:  # noqa: BLE001 - a health check never raises
            return False

    # ---- internals --------------------------------------------------------------------------

    def _safe_model_id(self) -> str:
        try:
            return self.extraction_model_id
        except Exception as exc:  # noqa: BLE001 - identity lookup must not fail an episode
            logger.warning("native_engine.model_identity_failed", error=f"{type(exc).__name__}")
            return str(self._settings.llm.model)

    def _extract_relationships(
        self,
        body: str,
        context: EpisodeContext,
        episode: Episode,
        names: Sequence[str],
        errors: list[str],
    ) -> tuple[list[ExtractedFact], int]:
        attempts = 0
        for retry in (False, True):
            if retry and not self._allow_retry:
                break
            try:
                response = self._provider.complete_json(
                    relation_prompt(body, context, names, title=episode.title, retry=retry),
                    relationship_schema(),
                    system=RELATION_SYSTEM_PROMPT,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    _sanitize(f"relationship call failed: {type(exc).__name__}: {exc}")
                )
                return [], attempts + 1
            attempts += getattr(response, "attempts", 1)
            parsed = self._parse(
                response, RelationshipExtraction, "relationship_extraction", errors
            )
            if parsed is not None and parsed.facts:
                return list(parsed.facts), attempts
            if parsed is None:
                return [], attempts
        return [], attempts

    def _parse(
        self,
        response: LLMResponse,
        model: type[EpisodeExtraction] | type[RelationshipExtraction],
        label: str,
        errors: list[str],
    ) -> Any:
        """Second validation layer: the schema constrained the JSON, this constrains the *meaning*.

        ``EpisodeExtraction`` / ``RelationshipExtraction`` use ``extra='forbid'`` and reject the
        entity types and predicates the model is not allowed to emit (``Source``, ``Device``,
        ``Episode``, and the six structural predicates), which the JSON Schema alone cannot express
        for every field.
        """
        if not getattr(response, "valid", False) or response.parsed is None:
            provider_errors = [_sanitize(e) for e in (response.errors or [])] or [
                "provider returned no schema-valid object"
            ]
            errors.extend(f"{label}: {message}" for message in provider_errors)
            return None
        try:
            return model.model_validate(response.parsed)
        except ValidationError as exc:
            errors.extend(_validation_errors(exc, prefix=label))
            return None
