"""Tier 2: the background LLM extraction queue (A07a, ADR-0006 / ADR-0012).

Tier 1 leaves ``episodes`` rows in ``queued`` state ordered by ``priority`` (AIOS, Projects, Areas,
decision docs and READMEs first; ``Clippings/`` last, ``origin=external, trust=low`` per AC-6). This
module drains that queue.

Three constraints are wired in rather than left to convention:

1. **Strictly serial.** ``INGEST_LLM_CONCURRENCY=1`` is a contract, not a preference: Qwen3 4B on a
   4-core CPU has no headroom, and the Ollama container is already limited to ``OLLAMA_CPUS``. The
   runner refuses to start with a higher setting instead of silently ignoring it.
2. **No provider is privileged (ADR-0012).** The engine is supplied by A08 and is selected through
   ``LLM_PROVIDER``; nothing here names ``ollama`` or ``bedrock``.
3. **A suspected secret is never sent.** :func:`aimemory.persistence.ingest_repo.claim_episode_for_extraction`
   excludes sources flagged ``secret_suspected`` in SQL, and the pipeline never creates an episode for
   one in the first place. With Bedrock selected, episode text leaves the machine, so this is a
   privacy control (asserted in ``tests/memory/test_change_detection.py``).

The knowledge engine itself (``KnowledgeEngine``, ADR-0009) is A08's, P8-T02. Until it exists,
:func:`run_tier2` reports honestly that no engine is available and leaves the queue untouched - the
Tier 1 vectors are already usable, which is the entire point of the tiering.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..common.config import Settings, get_settings
from ..common.errors import ConfigurationError
from ..common.logging import get_logger
from ..domain.models import Episode
from ..domain.ports import EpisodeContext, KnowledgeEngine
from ..persistence import ingest_repo
from ..persistence.repositories import EpisodeRepo
from .pipeline import SessionScope

__all__ = ["Tier2Report", "get_knowledge_engine", "run_tier2"]

logger = get_logger(__name__)

#: Signature A08 (P8-T02) provides to persist one :class:`ExtractionResult`. Kept as a callable so
#: this module has no import-time dependency on ``aimemory.knowledge``.
ResultWriter = Callable[[Any, Episode], None]


@dataclass
class Tier2Report:
    processed: int = 0
    extracted: int = 0
    failed: int = 0
    skipped: int = 0
    seconds: float = 0.0
    engine: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_counters(self) -> dict[str, int]:
        return {
            "tier2_processed": self.processed,
            "tier2_extracted": self.extracted,
            "tier2_failed": self.failed,
            "tier2_skipped": self.skipped,
        }


def get_knowledge_engine(settings: Settings | None = None) -> KnowledgeEngine | None:
    """A08's engine, if it has been built yet (ADR-0009 decides which implementation).

    Returns ``None`` - never raises - when ``aimemory.knowledge`` has no factory yet, so a Tier 2 run
    on a half-built system degrades to "nothing extracted" instead of a crash.
    """
    try:
        from ..knowledge import get_engine  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not built yet (P8-T02) or optional dependency missing
        return None
    try:
        return get_engine(settings or get_settings())
    except Exception as exc:  # noqa: BLE001
        logger.warning("tier2.engine_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None


def _episode_schema(settings: Settings) -> dict[str, Any]:
    """The frozen ``schemas/extraction/episode_extraction.schema.json`` (A02), used verbatim."""
    path = settings.paths.extraction_schema_dir / "episode_extraction.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run_tier2_with_provider(
    scope: SessionScope,
    provider: Any,
    *,
    limit: int | None = None,
    settings: Settings | None = None,
) -> Tier2Report:
    """Drain the queue with a bare :class:`~aimemory.domain.ports.LLMProvider` and no engine.

    This exists for exactly one reason: plan section L's *extraction failure* row - "invalid JSON
    after retries -> episode ``failed``, error kept, vectors intact" - is a property of the ingestion
    state machine, not of the knowledge engine, and must be testable before A08 lands (P8-T02).

    It deliberately does **not** mark a successful call ``extracted``: nothing is persisted from the
    response, so claiming extraction would be a lie in the coverage numbers. A valid response leaves
    the episode ``queued`` for the real engine and is counted as ``skipped``.
    """
    settings = settings or get_settings()
    report = Tier2Report(engine="provider-validation-only")
    schema = _episode_schema(settings)
    processed = 0
    started = time.perf_counter()
    while limit is None or processed < limit:
        with scope.session() as session:
            row = ingest_repo.claim_episode_for_extraction(session)
        if row is None:
            break
        episode = Episode(**row)
        processed += 1
        report.processed += 1
        prompt = (
            "Extract entities, artifacts and relationships from the document below.\n\n"
            f"# {episode.title or ''}\n{(episode.body or '')[:8000]}"
        )
        try:
            response = provider.complete_json(prompt, schema)
        except Exception as exc:  # noqa: BLE001
            with scope.session() as session:
                EpisodeRepo(session).mark_failed(episode.id, f"{type(exc).__name__}: {exc}"[:500])
            report.failed += 1
            continue
        if not getattr(response, "valid", False) or getattr(response, "parsed", None) is None:
            errors = "; ".join(getattr(response, "errors", []) or ["invalid JSON response"])
            with scope.session() as session:
                EpisodeRepo(session).mark_failed(episode.id, errors[:500])
            report.failed += 1
            continue
        with scope.session() as session:
            ingest_repo.requeue_episode(session, episode.id)
        report.skipped += 1
    report.seconds = time.perf_counter() - started
    report.notes.append(
        "no KnowledgeEngine: responses were validated only; nothing was persisted as knowledge"
    )
    return report


def run_tier2(
    scope: SessionScope,
    *,
    limit: int | None = None,
    settings: Settings | None = None,
    engine: KnowledgeEngine | None = None,
    writer: ResultWriter | None = None,
    provider: Any = None,
    progress: Callable[[int, str], None] | None = None,
) -> Tier2Report:
    """Drain up to ``limit`` episodes from the Tier 2 queue, one at a time."""
    settings = settings or get_settings()
    if settings.ingest.llm_concurrency != 1:
        raise ConfigurationError(
            "Tier 2 extraction must run serially.",
            detail=f"INGEST_LLM_CONCURRENCY={settings.ingest.llm_concurrency} (ADR-0006 fixes it at 1)",
        )
    report = Tier2Report()
    engine = engine or get_knowledge_engine(settings)
    if engine is None and provider is not None:
        return run_tier2_with_provider(scope, provider, limit=limit, settings=settings)
    if engine is None:
        report.notes.append(
            "no KnowledgeEngine available (A08/P8-T02 not built yet) - queue left untouched"
        )
        return report
    report.engine = str(getattr(engine, "kind", "unknown"))

    started = time.perf_counter()
    processed = 0
    while limit is None or processed < limit:
        with scope.session() as session:
            row = ingest_repo.claim_episode_for_extraction(session)
        if row is None:
            break
        episode = Episode(**row)
        processed += 1
        report.processed += 1
        if progress is not None:
            progress(processed, str(episode.title or episode.id))
        context = EpisodeContext(
            project_id=episode.project_id,
            doc_title=episode.title,
            heading_path=list(episode.section_path),
            observed_at=episode.observed_at,
        )
        try:
            result = engine.process_episode(episode, context)
        except Exception as exc:  # noqa: BLE001 - never lose the queue to one bad episode
            with scope.session() as session:
                EpisodeRepo(session).mark_failed(episode.id, f"{type(exc).__name__}: {exc}"[:500])
            report.failed += 1
            continue
        if not getattr(result, "valid", False):
            errors = "; ".join(getattr(result, "errors", []) or ["invalid extraction result"])
            with scope.session() as session:
                EpisodeRepo(session).mark_failed(episode.id, errors[:500])
            report.failed += 1
            continue
        if writer is not None:
            with scope.session() as session:
                writer(result, episode)
        with scope.session() as session:
            EpisodeRepo(session).mark_extracted(episode.id, report.engine or "native")
        report.extracted += 1
    report.seconds = time.perf_counter() - started
    return report
