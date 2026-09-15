"""P9-T01 (A09): :class:`HybridRetriever` orchestration and its degraded modes.

Plan section Y requires "embedding down -> keyword-only with warning" to be a *tested* behaviour,
not a hope. These tests drive the pipeline with a fake SQLAlchemy session and a fake embedding
provider so the degradation paths are exercised deterministically and offline; the SQL itself is
tested against a real PostgreSQL in ``tests/integration/test_retrieval_candidates.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from aimemory.domain.models import EmbeddingModel
from aimemory.domain.ports import EmbeddingResult
from aimemory.domain.retrieval import RetrievalConfig, SearchQuery
from aimemory.retrieval.pipeline import (
    EMBEDDING_DEGRADED_WARNING,
    EMBEDDING_MODEL_UNKNOWN_WARNING,
    HybridRetriever,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
MODEL_ID = "minilm-l6-v2-384"
CONFIG = RetrievalConfig(semantic_top_k=40, keyword_top_k=40, fused_top_k=15, final_k=10)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:pipeline:{label}")


def _chunk_row(label: str, raw_score: float) -> dict[str, Any]:
    return {
        "object_id": _id(label),
        "project_id": "joblab-de",
        "source_id": _id(f"src-{label}"),
        "version_id": _id(f"ver-{label}"),
        "text": f"text of {label}",
        "text_hash": "sha256:" + "0" * 64,
        "heading_path": ["Design"],
        "source_uri": f"vault://notes/{label}.md",
        "source_status": "active",
        "trust": "high",
        "policy": "INDEX_CONTENT",
        "observed_at": NOW,
        "raw_score": raw_score,
    }


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def mappings(self) -> _FakeResult:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeSession:
    """Returns canned rows for whichever of A09's statements it recognises, and records every SQL
    string it was given, so a test can assert that a retriever did (or did not) run."""

    def __init__(self, routes: dict[str, list[Any]] | None = None) -> None:
        self.routes = routes or {}
        self.executed: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _FakeResult:
        sql = str(statement)
        self.executed.append(sql)
        for marker, rows in self.routes.items():
            if marker in sql:
                return _FakeResult(rows)
        return _FakeResult([])

    def ran(self, marker: str) -> bool:
        return any(marker in sql for sql in self.executed)


class FakeEmbedder:
    """Minimal :class:`~aimemory.domain.ports.EmbeddingProvider`. ``fail_on`` makes exactly one of
    the two calls the Gateway makes (``model_identity`` / ``embed_query``) raise."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on

    @property
    def dimensions(self) -> int:
        return 384

    def embed(self, texts):  # pragma: no cover - the Gateway only embeds the query
        return EmbeddingResult(vectors=[], model_id=MODEL_ID, dimension=384)

    def embed_query(self, text: str) -> list[float]:
        if self.fail_on == "embed_query":
            raise RuntimeError("embedding-service unreachable")
        return [0.1] * 384

    def model_identity(self) -> EmbeddingModel:
        if self.fail_on == "model_identity":
            raise RuntimeError("embedding-service unreachable")
        return EmbeddingModel(
            id=MODEL_ID, name="sentence-transformers/all-MiniLM-L6-v2", dimension=384
        )

    def health(self) -> bool:
        return self.fail_on is None


MODEL_ROW = [(MODEL_ID,)]
SEMANTIC_MARKER = "e.object_type = 'chunk'"
KEYWORD_MARKER = "ts_rank_cd(c.tsv"


def _session(*, semantic: list[Any] | None = None, keyword: list[Any] | None = None,
             model_registered: bool = True) -> FakeSession:
    return FakeSession(
        {
            "FROM embedding_models": MODEL_ROW if model_registered else [],
            SEMANTIC_MARKER: semantic or [],
            KEYWORD_MARKER: keyword or [],
        }
    )


def test_both_retrievers_run_and_their_counts_are_measured_per_query() -> None:
    session = _session(
        semantic=[_chunk_row("a", 0.91), _chunk_row("b", 0.80)],
        keyword=[_chunk_row("b", 0.42)],
    )
    retriever = HybridRetriever(FakeEmbedder(), config=CONFIG)

    outcome = retriever.retrieve(session, SearchQuery(query="pgvector hnsw"), now=NOW)

    assert outcome.candidate_counts == {"semantic": 2, "keyword": 1, "fused": 2, "returned": 2}
    assert outcome.warnings == []
    assert outcome.degraded is False
    assert outcome.embedding_model_id == MODEL_ID
    # 'b' was found by both retrievers; RRF rewards the agreement.
    assert outcome.hits[0].object_id == _id("b")
    assert [r.value for r in outcome.hits[0].retrievers] == ["semantic", "keyword"]
    assert [hit.rank for hit in outcome.hits] == [1, 2]


def test_ef_search_is_set_for_the_semantic_statement() -> None:
    session = _session(semantic=[_chunk_row("a", 0.9)])

    HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(session, SearchQuery(query="q"), now=NOW)

    assert session.ran("SET LOCAL hnsw.ef_search = 80")


def test_embedding_service_down_degrades_to_keyword_only_with_a_warning() -> None:
    session = _session(keyword=[_chunk_row("k", 0.5)])
    retriever = HybridRetriever(FakeEmbedder(fail_on="embed_query"), config=CONFIG)

    outcome = retriever.retrieve(session, SearchQuery(query="ADR-0007"), now=NOW)

    assert EMBEDDING_DEGRADED_WARNING in outcome.warnings
    assert outcome.degraded is True
    assert outcome.candidate_counts["semantic"] == 0
    assert outcome.candidate_counts["keyword"] == 1
    assert [hit.object_id for hit in outcome.hits] == [_id("k")]
    assert not session.ran(SEMANTIC_MARKER), "no vector query may run without a query embedding"


def test_an_unreachable_provider_identity_also_degrades_rather_than_failing() -> None:
    session = _session(keyword=[_chunk_row("k", 0.5)])
    retriever = HybridRetriever(FakeEmbedder(fail_on="model_identity"), config=CONFIG)

    outcome = retriever.retrieve(session, SearchQuery(query="q"), now=NOW)

    assert EMBEDDING_DEGRADED_WARNING in outcome.warnings
    assert len(outcome.hits) == 1


def test_a_corpus_with_no_registered_embedding_model_degrades_with_its_own_warning() -> None:
    session = _session(keyword=[_chunk_row("k", 0.5)], model_registered=False)

    outcome = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(
        session, SearchQuery(query="q"), now=NOW
    )

    assert EMBEDDING_MODEL_UNKNOWN_WARNING in outcome.warnings
    assert not session.ran(SEMANTIC_MARKER)
    assert len(outcome.hits) == 1


def test_no_embedder_configured_is_keyword_only_and_says_so() -> None:
    session = _session(keyword=[_chunk_row("k", 0.5)])

    outcome = HybridRetriever(None, config=CONFIG).retrieve(session, SearchQuery(query="q"), now=NOW)

    assert outcome.warnings == [EMBEDDING_DEGRADED_WARNING]
    assert len(outcome.hits) == 1


def test_keyword_search_is_never_skipped_even_when_the_embedding_path_is_healthy() -> None:
    session = _session(semantic=[_chunk_row("a", 0.99)])

    HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(session, SearchQuery(query="q"), now=NOW)

    assert session.ran(KEYWORD_MARKER)


def test_the_caller_limit_never_exceeds_final_k() -> None:
    rows = [_chunk_row(f"c{i}", 1.0 - i / 100) for i in range(20)]
    session = _session(semantic=rows)

    generous = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(
        session, SearchQuery(query="q", k=100), now=NOW
    )
    modest = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(
        _session(semantic=rows), SearchQuery(query="q", k=3), now=NOW
    )

    assert len(generous.hits) == CONFIG.final_k
    assert len(modest.hits) == 3


def test_fused_top_k_bounds_what_reaches_the_boost_stage() -> None:
    rows = [_chunk_row(f"c{i}", 1.0 - i / 100) for i in range(40)]
    session = _session(semantic=rows)

    outcome = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(
        session, SearchQuery(query="q"), now=NOW
    )

    assert outcome.candidate_counts["semantic"] == 40
    assert outcome.candidate_counts["fused"] == CONFIG.fused_top_k == 15
    assert outcome.candidate_counts["returned"] == CONFIG.final_k == 10


def test_latency_and_config_version_are_reported_for_the_retrieval_log() -> None:
    outcome = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(
        _session(semantic=[_chunk_row("a", 0.9)]), SearchQuery(query="q"), now=NOW
    )

    assert outcome.latency_ms >= 0
    assert outcome.config_version == CONFIG.version
    assert set(outcome.candidate_counts) == {"semantic", "keyword", "fused", "returned"}


@pytest.mark.parametrize("scoped", [True, False])
def test_project_scoping_reaches_the_boost_stage(scoped: bool) -> None:
    session = _session(semantic=[_chunk_row("a", 0.9)])
    query = SearchQuery(query="q", project_ids=["joblab-de"] if scoped else [])

    outcome = HybridRetriever(FakeEmbedder(), config=CONFIG).retrieve(session, query, now=NOW)

    assert ("project_match" in outcome.hits[0].boosts) is scoped
