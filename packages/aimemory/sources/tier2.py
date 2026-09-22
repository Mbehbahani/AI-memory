"""Tier 2: the background LLM extraction queue (A07a, ADR-0006 / ADR-0012).

Tier 1 leaves ``episodes`` rows in ``queued`` state ordered by ``priority`` (AIOS, Projects, Areas,
decision docs and READMEs first; ``Clippings/`` last, ``origin=external, trust=low`` per AC-6). This
module drains that queue.

Three constraints are wired in rather than left to convention:

1. **Strictly serial.** ``INGEST_LLM_CONCURRENCY=1`` is a contract, not a preference: Qwen3 4B on a
   4-core CPU has no headroom, and the Ollama container is already limited to ``OLLAMA_CPUS``. The
   runner refuses to start with a higher setting instead of silently ignoring it.
2. **No provider is privileged (ADR-0012/ADR-0014).** The engine is supplied by A08 and the model is
   resolved through ``LLM_PROVIDER`` -> :func:`aimemory.providers.llm.get_provider`; nothing here
   names ``ollama`` or ``bedrock``. ADR-0014 made ``bedrock`` the default, which changes the value of
   that setting and nothing in this module.
3. **One extraction model per corpus (ADR-0014 rule 2).** Before any episode is claimed,
   :func:`assert_single_extraction_model` compares the model this run would use against the models
   that produced the scope's existing *current* facts. A second model is refused with
   :class:`ExtractionModelMismatch`, which names both models and the remedy
   (``aimemory-ingest reprocess --re-extract --model <id>``). ``allow_model_mix=True`` is the only way
   past it, exists for benchmarking, and is recorded on the run.

4. **A suspected secret is never sent.** :func:`aimemory.persistence.ingest_repo.claim_episode_for_extraction`
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
from uuid import UUID

from sqlalchemy.orm import Session

from ..common.config import Settings, get_settings
from ..common.errors import AiMemoryError, ConfigurationError
from ..common.logging import get_logger
from ..domain.models import Episode
from ..domain.ports import EpisodeContext, KnowledgeEngine
from ..persistence import ingest_repo
from ..persistence.repositories import EpisodeRepo
from .pipeline import SessionScope

__all__ = [
    "ExtractionModelMismatch",
    "Tier2Report",
    "assert_single_extraction_model",
    "get_knowledge_engine",
    "resolve_extraction_model_id",
    "run_tier2",
]

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
    #: ``extraction_models.id`` every row this run wrote is stamped with (ADR-0014 rule 2).
    model_id: str | None = None
    allow_model_mix: bool = False
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


def get_knowledge_writer(scope: SessionScope) -> ResultWriter | None:
    """A08's persistence writer, if it has been built yet. Never raises - see
    :func:`get_knowledge_engine` for why a half-built system degrades instead of crashing.
    """
    try:
        from ..knowledge import get_writer  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - not built yet, or an optional dependency is missing
        return None
    try:
        return get_writer(scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tier2.writer_unavailable", error=f"{type(exc).__name__}: {exc}")
        return None


# --------------------------------------------------------------------------------------------------
# ADR-0014 rule 2: one extraction model per corpus
# --------------------------------------------------------------------------------------------------


class ExtractionModelMismatch(AiMemoryError):
    """The scope already holds current facts produced by a different extraction model.

    P4-T02 MEASURED the two providers disagreeing *systematically* on entity types (the vault's PARA
    folders: ``Repository`` from ``qwen3:4b``, ``InfrastructureComponent`` from Claude Haiku 4.5), so a
    graph built half by each contains two incompatible typings of the same thing. This is a hard stop,
    not a warning. ``AiMemoryError.__str__`` prints ``public_message`` only, so both model ids and the
    remedy are in it.
    """

    code = "extraction_model_mismatch"
    http_status = 409


def resolve_extraction_model_id(
    settings: Settings | None = None, *, engine: Any = None, provider: Any = None
) -> str:
    """The ``extraction_models.id`` this run will stamp on everything it writes.

    Resolution order: an explicit provider/engine identity, then ``LLM_PROVIDER`` through
    :func:`aimemory.providers.llm.get_provider`. Neither branch names a provider: ADR-0014 changed the
    default from ``ollama`` to ``bedrock`` without this module changing a line, and that is the point.
    Constructing a provider here is cheap and offline - ``model_identity()`` reads settings only, so a
    missing AWS credential surfaces on the first real call, not on a status check.
    """
    settings = settings or get_settings()
    for candidate in (engine, provider):
        identity = getattr(candidate, "model_identity", None)
        if callable(identity):
            try:
                return str(identity().id)
            except Exception:  # noqa: BLE001 - fall through to the configured provider
                pass
        explicit = getattr(candidate, "extraction_model_id", None)
        if explicit:
            return str(explicit)
    from ..providers.llm import get_provider

    return str(get_provider(settings.llm).model_identity().id)


def assert_single_extraction_model(
    session: Session,
    model_id: str,
    *,
    project_id: str | None = None,
    root_id: str | None = None,
    allow_model_mix: bool = False,
) -> dict[str, int]:
    """Refuse to extract into a scope whose current facts came from another model.

    Returns the ``{model_id: fact count}`` map it checked, so the caller can log it. Deterministic
    ids are not models in this sense and are excluded by
    :func:`aimemory.persistence.ingest_repo.extraction_models_in_use`.
    """
    in_use = ingest_repo.extraction_models_in_use(session, project_id=project_id, root_id=root_id)
    # Every id naming the same model, not just the one being written. ADR-0014 rule 2 refuses a
    # *second model*, and two routes to Claude Haiku 4.5 are one model (ADR-0017); refusing between
    # them would demand a full re-extraction to change nothing about the corpus.
    equivalent = set(ingest_repo.equivalent_model_ids(model_id))
    others = {mid: count for mid, count in in_use.items() if mid not in equivalent}
    if not others:
        return in_use
    if allow_model_mix:
        logger.warning(
            "tier2.model_mix_allowed",
            model=model_id,
            existing=sorted(others),
            scope=project_id or root_id or "corpus",
        )
        return in_use
    scope = f"project {project_id!r}" if project_id else (f"root {root_id!r}" if root_id else "this corpus")
    existing = ", ".join(f"{mid} ({count} current facts)" for mid, count in sorted(others.items()))
    raise ExtractionModelMismatch(
        f"refusing to extract into {scope} with {model_id!r}: it already holds facts from "
        f"{existing}. ADR-0014 allows one extraction model per corpus. Remedy: re-extract the scope "
        f"under one model with `aimemory-ingest reprocess --re-extract --model {model_id}"
        + (f" --project {project_id}" if project_id else "")
        + "`, which supersedes the old facts through the normal temporal path (they become "
        "historical, never deleted). `--allow-model-mix` overrides this for benchmarking only.",
        detail=f"requested={model_id} existing={sorted(others)}",
        context={"requested_model": model_id, "existing_models": sorted(others)},
    )



def _register_model(
    session: Session, source: Any, model_id: str, run_id: UUID | None, allow_model_mix: bool
) -> None:
    """Make the model row exist (FK target) and record it on the run (ADR-0014 rule 2)."""
    identity = getattr(source, "model_identity", None)
    if callable(identity):
        try:
            ingest_repo.ensure_extraction_model(session, identity())
        except Exception as exc:  # noqa: BLE001 - a missing identity must not stop extraction
            logger.warning("tier2.model_register_failed", error=f"{type(exc).__name__}: {exc}")
    if run_id is not None:
        ingest_repo.record_run_extraction_model(
            session, run_id, model_id=model_id, allow_model_mix=allow_model_mix
        )


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
    project_id: str | None = None,
    root_id: str | None = None,
    allow_model_mix: bool = False,
    run_id: UUID | None = None,
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
    report.model_id = resolve_extraction_model_id(settings, provider=provider)
    report.allow_model_mix = allow_model_mix
    schema = _episode_schema(settings)
    with scope.session() as session:
        assert_single_extraction_model(
            session,
            report.model_id,
            project_id=project_id,
            root_id=root_id,
            allow_model_mix=allow_model_mix,
        )
        _register_model(session, provider, report.model_id, run_id, allow_model_mix)
    processed = 0
    started = time.perf_counter()
    while limit is None or processed < limit:
        with scope.session() as session:
            row = ingest_repo.claim_episode_for_extraction(
                session, project_id=project_id, root_id=root_id
            )
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
    project_id: str | None = None,
    root_id: str | None = None,
    allow_model_mix: bool = False,
    model_id: str | None = None,
    run_id: UUID | None = None,
) -> Tier2Report:
    """Drain up to ``limit`` episodes from the Tier 2 queue, one at a time.

    ``allow_model_mix`` is the ADR-0014 escape hatch: never the default, recorded on the run, and
    intended for the benchmark harness that compares two models over the same episodes.
    """
    settings = settings or get_settings()
    if settings.ingest.llm_concurrency != 1:
        raise ConfigurationError(
            "Tier 2 extraction must run serially.",
            detail=f"INGEST_LLM_CONCURRENCY={settings.ingest.llm_concurrency} (ADR-0006 fixes it at 1)",
        )
    report = Tier2Report()
    engine = engine or get_knowledge_engine(settings)
    if engine is None and provider is not None:
        return run_tier2_with_provider(
            scope,
            provider,
            limit=limit,
            settings=settings,
            project_id=project_id,
            root_id=root_id,
            allow_model_mix=allow_model_mix,
            run_id=run_id,
        )
    if engine is None:
        report.notes.append(
            "no KnowledgeEngine available (A08/P8-T02 not built yet) - queue left untouched"
        )
        return report
    # Without this, an engine with no writer would call the LLM for every episode and then throw
    # every result away (the persist call below is guarded by `writer is not None`) - paying for the
    # extraction, and the egress, to produce nothing.
    writer = writer or get_knowledge_writer(scope)
    if writer is None:
        report.notes.append(
            "no ResultWriter available - refusing to extract, since nothing could be persisted"
        )
        return report
    report.engine = str(getattr(engine, "kind", "unknown"))
    report.model_id = model_id or resolve_extraction_model_id(settings, engine=engine)
    report.allow_model_mix = allow_model_mix

    # ADR-0014 rule 2: the guard runs *before* the first episode is claimed, so a mismatched corpus
    # costs nothing (no LLM call, no egress) instead of being discovered halfway through a run.
    with scope.session() as session:
        assert_single_extraction_model(
            session,
            report.model_id,
            project_id=project_id,
            root_id=root_id,
            allow_model_mix=allow_model_mix,
        )
        _register_model(session, engine, report.model_id, run_id, allow_model_mix)

    started = time.perf_counter()
    processed = 0
    checked_projects: set[str] = set()
    while limit is None or processed < limit:
        with scope.session() as session:
            row = ingest_repo.claim_episode_for_extraction(
                session, project_id=project_id, root_id=root_id
            )
        if row is None:
            break
        episode = Episode(**row)
        # The scope guard above covers the requested scope; this covers each project an unscoped run
        # actually reaches, so "extracting into a project" is checked for every project touched.
        if episode.project_id and episode.project_id not in checked_projects:
            try:
                with scope.session() as session:
                    assert_single_extraction_model(
                        session,
                        report.model_id,
                        project_id=episode.project_id,
                        allow_model_mix=allow_model_mix,
                    )
            except ExtractionModelMismatch:
                # The episode was already claimed. Leave it available for the explicit
                # re-extraction remedy instead of stranding it in `running`.
                with scope.session() as session:
                    ingest_repo.requeue_episode(session, episode.id)
                raise
            checked_projects.add(episode.project_id)
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
            # Guarded for the same reason `process_episode` above is: one episode must not be able
            # to end the run. The guard used to cover only extraction, so a *persistence* failure -
            # a constraint violation on one entity, say - propagated out of the loop and abandoned
            # the remaining queue, leaving the episode stuck in `running`. MEASURED: a single
            # entities.project_id foreign-key violation stopped a 129-episode run after 4, with the
            # LLM cost for those episodes already paid.
            try:
                with scope.session() as session:
                    writer(result, episode)
            except Exception as exc:  # noqa: BLE001 - the queue outlives any one bad result
                with scope.session() as session:
                    EpisodeRepo(session).mark_failed(
                        episode.id, f"persist: {type(exc).__name__}: {exc}"[:500]
                    )
                report.failed += 1
                logger.warning(
                    "tier2.persist_failed",
                    episode_id=str(episode.id),
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
                continue
        with scope.session() as session:
            EpisodeRepo(session).mark_extracted(episode.id, report.engine or "native")
        report.extracted += 1
    report.seconds = time.perf_counter() - started
    return report
