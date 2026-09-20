"""P9-T01 (A09), integration: the candidate SQL against a real PostgreSQL + pgvector.

What only a real database can prove: that the HNSW cosine ordering is what the code thinks it is,
that ``tsvector`` finds an exact identifier an embedding would smear away, and - most importantly -
that every scoping rule is enforced *inside* the query. A filter applied after ``LIMIT 40`` looks
identical in unit tests and is wrong in production.

Every test seeds its own tiny corpus through the shared ``pg_session`` fixture, whose transaction is
rolled back at teardown, so nothing is left behind and no real source root is touched. The whole
module skips (never errors) when PostgreSQL is unreachable or unmigrated - see ``tests/conftest.py``.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import ObjectType, RetrieverKind
from aimemory.domain.retrieval import RetrievalConfig, SearchQuery
from aimemory.retrieval.candidates import keyword_candidates, semantic_candidates, vector_literal
from aimemory.retrieval.filters import ScopeFilters
from aimemory.retrieval.pipeline import HybridRetriever
from aimemory.retrieval.relevance import analyse_query, lexical_coverage
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_available")]

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
DEVICE_ID = "local-development-machine"
MODEL_ID = "minilm-l6-v2-384"
DIM = 384
CONFIG = RetrievalConfig()


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p9:{label}")


def _unit_vector(angle_index: int) -> list[float]:
    """A normalized 384-d vector in the first two dimensions only.

    Cosine distance between two of these is a pure function of their angle, so a test can say
    "this chunk is nearer than that one" and mean it, without depending on the real model.
    """
    angle = angle_index * math.pi / 180.0
    vector = [0.0] * DIM
    vector[0] = math.cos(angle)
    vector[1] = math.sin(angle)
    return vector


@dataclass
class Corpus:
    """Ids of everything one test seeded, so assertions can name rows without re-querying."""

    project_id: str
    other_project_id: str
    chunks: dict[str, UUID]
    artifacts: dict[str, UUID]
    sources: dict[str, UUID]


def _exec(session: Session, sql: str, params: dict[str, Any] | None = None) -> None:
    session.execute(sa.text(sql), params or {})


def _seed_source(
    session: Session,
    corpus_key: str,
    *,
    project_id: str | None,
    policy: str = "INDEX_CONTENT",
    status: str = "active",
    trust: str = "high",
    secret_suspected: bool = False,
    observed_at: datetime = NOW,
) -> tuple[UUID, UUID]:
    source_id, version_id = _id(f"source:{corpus_key}"), _id(f"version:{corpus_key}")
    _exec(
        session,
        """
        INSERT INTO sources (id, uri, root_id, relative_path, project_id, kind, media_type,
                             policy, status, trust, secret_suspected, origin)
        VALUES (:id, :uri, :root, :rel, :project, 'file', 'text/markdown',
                :policy, :status, :trust, :secret, 'internal')
        """,
        {
            "id": source_id,
            "uri": f"vault://p9/{corpus_key}.md",
            "root": "t-p9-root",
            "rel": f"p9/{corpus_key}.md",
            "project": project_id,
            "policy": policy,
            "status": status,
            "trust": trust,
            "secret": secret_suspected,
        },
    )
    _exec(
        session,
        """
        INSERT INTO source_versions (id, source_id, content_hash, size_bytes, observed_at,
                                     change_type)
        VALUES (:id, :source_id, :hash, 100, :observed_at, 'new')
        """,
        {
            "id": version_id,
            "source_id": source_id,
            "hash": f"sha256:{_id('content:' + corpus_key).hex * 2}",
            "observed_at": observed_at,
        },
    )
    _exec(
        session,
        "UPDATE sources SET current_version_id = :v WHERE id = :s",
        {"v": version_id, "s": source_id},
    )
    return source_id, version_id


def _seed_chunk(
    session: Session,
    key: str,
    *,
    source_id: UUID,
    version_id: UUID,
    project_id: str | None,
    text: str,
    angle: int | None = None,
    ordinal: int = 0,
) -> UUID:
    chunk_id = _id(f"chunk:{key}")
    _exec(
        session,
        """
        INSERT INTO chunks (id, version_id, source_id, project_id, ordinal, text, text_hash,
                            heading_path, char_start, char_end, token_count)
        VALUES (:id, :version_id, :source_id, :project, :ordinal, :text, :hash,
                ARRAY['Design'], 0, :end, :tokens)
        """,
        {
            "id": chunk_id,
            "version_id": version_id,
            "source_id": source_id,
            "project": project_id,
            "ordinal": ordinal,
            "text": text,
            "hash": f"sha256:{_id('hash:' + key).hex}",
            "end": len(text),
            "tokens": max(1, len(text) // 4),
        },
    )
    if angle is not None:
        _exec(
            session,
            """
            INSERT INTO embeddings (id, object_type, object_id, text_hash, model_id, dimension,
                                    vector)
            VALUES (:id, 'chunk', :object_id, :hash, :model, :dim, CAST(:vector AS vector))
            """,
            {
                "id": _id(f"embedding:{key}"),
                "object_id": chunk_id,
                "hash": f"sha256:{_id('hash:' + key).hex}",
                "model": MODEL_ID,
                "dim": DIM,
                "vector": vector_literal(_unit_vector(angle)),
            },
        )
    return chunk_id


def _seed_artifact(
    session: Session,
    key: str,
    *,
    project_id: str | None,
    title: str,
    body: str,
    source_id: UUID | None = None,
    status: str = "current",
    valid_from: datetime = NOW - timedelta(days=30),
    valid_to: datetime | None = None,
    observed_at: datetime = NOW - timedelta(days=30),
    artifact_type: str = "decision",
    angle: int | None = None,
) -> UUID:
    artifact_id = _id(f"artifact:{key}")
    _exec(
        session,
        """
        INSERT INTO knowledge_artifacts (id, type, title, body, project_id, current_status,
                                         valid_from, valid_to, engine, source_status, source_id,
                                         source_uri, device_id, observed_at)
        VALUES (:id, :type, :title, :body, :project, :status, :valid_from, :valid_to,
                'native', 'active', :source_id, :source_uri, :device, :observed_at)
        """,
        {
            "id": artifact_id,
            "type": artifact_type,
            "title": title,
            "body": body,
            "project": project_id,
            "status": status,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "source_id": source_id,
            "source_uri": f"vault://p9/{key}.md",
            "device": DEVICE_ID,
            "observed_at": observed_at,
        },
    )
    if angle is not None:
        _exec(
            session,
            """
            INSERT INTO embeddings (id, object_type, object_id, text_hash, model_id, dimension,
                                    vector)
            VALUES (:id, 'artifact', :object_id, :hash, :model, :dim, CAST(:vector AS vector))
            """,
            {
                "id": _id(f"embedding:artifact:{key}"),
                "object_id": artifact_id,
                "hash": f"sha256:{_id('hash:artifact:' + key).hex}",
                "model": MODEL_ID,
                "dim": DIM,
                "vector": vector_literal(_unit_vector(angle)),
            },
        )
    return artifact_id


@pytest.fixture()
def corpus(pg_session: Session) -> Corpus:
    """A deliberately awkward little corpus: two projects, a low-trust clipping, a deleted source,
    a CATALOG_ONLY source, a secret-flagged source, plus a current and a closed artifact."""
    session = pg_session
    project_id, other_project_id = "t-p9-proj", "t-p9-other"
    _exec(
        session,
        "INSERT INTO projects (id, name, track, status) VALUES "
        "(:a, 'P9 Primary', 'business', 'active'), (:b, 'P9 Other', 'research', 'active')",
        {"a": project_id, "b": other_project_id},
    )
    _exec(
        session,
        "INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, "
        "default_project_id) VALUES ('t-p9-root', 'vault', 't-p9-root', '/sources/vault', "
        ":device, :project)",
        {"device": DEVICE_ID, "project": project_id},
    )

    sources: dict[str, UUID] = {}
    chunks: dict[str, UUID] = {}

    # Semantic neighbourhood: 'near' (0 deg) is closest to a query at 0 deg, then 'mid', then 'far'.
    for key, angle, text in (
        ("near", 0, "The retrieval pipeline fuses pgvector HNSW candidates with tsvector hits."),
        ("mid", 30, "Embeddings are reused by text hash so a re-scan costs nothing."),
        ("far", 85, "Backups are taken nightly and restored into a scratch database."),
    ):
        source_id, version_id = _seed_source(session, key, project_id=project_id)
        sources[key] = source_id
        chunks[key] = _seed_chunk(
            session,
            key,
            source_id=source_id,
            version_id=version_id,
            project_id=project_id,
            text=text,
            angle=angle,
        )

    # An exact identifier that only keyword search can be relied on to find.
    src, ver = _seed_source(session, "adr", project_id=project_id)
    sources["adr"] = src
    chunks["adr"] = _seed_chunk(
        session,
        "adr",
        source_id=src,
        version_id=ver,
        project_id=project_id,
        text="ADR-0007 binds every published port to 127.0.0.1 loopback only.",
        angle=150,
    )

    # Same words, a different project - the scoping filter's target.
    src, ver = _seed_source(session, "other", project_id=other_project_id)
    sources["other"] = src
    chunks["other"] = _seed_chunk(
        session,
        "other",
        source_id=src,
        version_id=ver,
        project_id=other_project_id,
        text="The retrieval pipeline fuses pgvector HNSW candidates in another project.",
        angle=2,
    )

    # Excluded by policy / status / secret flag / trust.
    src, ver = _seed_source(session, "catalog", project_id=project_id, policy="CATALOG_ONLY")
    sources["catalog"] = src
    chunks["catalog"] = _seed_chunk(
        session, "catalog", source_id=src, version_id=ver, project_id=project_id,
        text="Catalog-only pgvector HNSW retrieval pipeline text.", angle=1,
    )
    src, ver = _seed_source(session, "deleted", project_id=project_id, status="deleted")
    sources["deleted"] = src
    chunks["deleted"] = _seed_chunk(
        session, "deleted", source_id=src, version_id=ver, project_id=project_id,
        text="Deleted-source pgvector HNSW retrieval pipeline text.", angle=1,
    )
    src, ver = _seed_source(session, "secret", project_id=project_id, secret_suspected=True)
    sources["secret"] = src
    chunks["secret"] = _seed_chunk(
        session, "secret", source_id=src, version_id=ver, project_id=project_id,
        text="Secret-flagged pgvector HNSW retrieval pipeline text.", angle=1,
    )
    src, ver = _seed_source(session, "clipping", project_id=project_id, trust="low")
    sources["clipping"] = src
    chunks["clipping"] = _seed_chunk(
        session, "clipping", source_id=src, version_id=ver, project_id=project_id,
        text="A clipped article about the retrieval pipeline and pgvector HNSW.", angle=3,
    )

    # An old source, for the `since` axis.
    src, ver = _seed_source(
        session, "old", project_id=project_id, observed_at=NOW - timedelta(days=400)
    )
    sources["old"] = src
    chunks["old"] = _seed_chunk(
        session, "old", source_id=src, version_id=ver, project_id=project_id,
        text="An old note about the retrieval pipeline and pgvector HNSW.", angle=4,
    )

    artifacts = {
        "current": _seed_artifact(
            session,
            "current",
            project_id=project_id,
            title="Use RRF for hybrid retrieval",
            body="Reciprocal Rank Fusion with k=60 combines pgvector HNSW and tsvector candidates.",
            source_id=sources["near"],
            angle=5,
        ),
        "closed": _seed_artifact(
            session,
            "closed",
            project_id=project_id,
            title="Use weighted score blending for hybrid retrieval",
            body="Blend normalized cosine and ts_rank_cd scores for the pgvector HNSW pipeline.",
            source_id=sources["near"],
            status="superseded",
            valid_from=NOW - timedelta(days=200),
            valid_to=NOW - timedelta(days=30),
            observed_at=NOW - timedelta(days=200),
            angle=6,
        ),
        "unconfirmed": _seed_artifact(
            session,
            "unconfirmed",
            project_id=project_id,
            title="Unconfirmed note on hybrid retrieval",
            body="An unconfirmed statement about the pgvector HNSW retrieval pipeline.",
            source_id=sources["near"],
            status="unconfirmed",
            angle=7,
        ),
    }
    session.flush()
    return Corpus(
        project_id=project_id,
        other_project_id=other_project_id,
        chunks=chunks,
        artifacts=artifacts,
        sources=sources,
    )


def _labels(corpus: Corpus, output: Any) -> list[str]:
    by_id = {v: f"chunk:{k}" for k, v in corpus.chunks.items()}
    by_id.update({v: f"artifact:{k}" for k, v in corpus.artifacts.items()})
    return [by_id.get(c.object_id, str(c.object_id)) for c in output.candidates]


# ------------------------------------------------------------------------------ semantic search


def test_semantic_candidates_are_ordered_by_cosine_distance(pg_session: Session, corpus: Corpus) -> None:
    scope = ScopeFilters(project_ids=[corpus.project_id])

    output = semantic_candidates(
        pg_session, _unit_vector(0), model_id=MODEL_ID, scope=scope, limit=40,
        object_types=[ObjectType.CHUNK],
    )

    labels = _labels(corpus, output)
    assert labels[0] == "chunk:near"
    assert labels.index("chunk:mid") < labels.index("chunk:far")
    assert output.candidates[0].rank == 1
    assert output.candidates[0].raw_score == pytest.approx(1.0, abs=1e-6)
    assert output.candidates[0].retriever is RetrieverKind.SEMANTIC


def test_semantic_candidates_carry_the_metadata_ranking_and_citation_need(
    pg_session: Session, corpus: Corpus
) -> None:
    output = semantic_candidates(
        pg_session, _unit_vector(0), model_id=MODEL_ID,
        scope=ScopeFilters(project_ids=[corpus.project_id]), limit=40,
        object_types=[ObjectType.CHUNK],
    )

    meta = output.meta_for((ObjectType.CHUNK, corpus.chunks["near"]))
    assert meta is not None
    assert meta.source_uri == "vault://p9/near.md"
    assert meta.heading_path == ["Design"]
    assert meta.observed_at == NOW
    assert meta.trust == "high"
    assert meta.project_id == corpus.project_id
    assert meta.text_hash is not None


def test_an_unknown_embedding_model_returns_nothing_rather_than_mixing_vector_spaces(
    pg_session: Session, corpus: Corpus
) -> None:
    output = semantic_candidates(
        pg_session, _unit_vector(0), model_id="some-other-model-768",
        scope=ScopeFilters(), limit=40,
    )

    assert output.candidates == []


def test_semantic_search_covers_artifacts_as_well_as_chunks(
    pg_session: Session, corpus: Corpus
) -> None:
    output = semantic_candidates(
        pg_session, _unit_vector(5), model_id=MODEL_ID,
        scope=ScopeFilters(project_ids=[corpus.project_id]), limit=40,
    )

    assert {c.object_type for c in output.candidates} == {ObjectType.CHUNK, ObjectType.ARTIFACT}
    assert corpus.artifacts["current"] in {c.object_id for c in output.candidates}


# ------------------------------------------------------------------------------- keyword search


def test_keyword_search_finds_an_exact_identifier(pg_session: Session, corpus: Corpus) -> None:
    output = keyword_candidates(
        pg_session, "ADR-0007", scope=ScopeFilters(project_ids=[corpus.project_id]), limit=40,
        object_types=[ObjectType.CHUNK],
    )

    assert _labels(corpus, output) == ["chunk:adr"]
    assert output.candidates[0].retriever is RetrieverKind.KEYWORD


def test_keyword_search_ranks_artifacts_by_title_and_body(
    pg_session: Session, corpus: Corpus
) -> None:
    output = keyword_candidates(
        pg_session, "Reciprocal Rank Fusion", scope=ScopeFilters(project_ids=[corpus.project_id]),
        limit=40, object_types=[ObjectType.ARTIFACT],
    )

    assert corpus.artifacts["current"] in {c.object_id for c in output.candidates}


def test_a_query_of_only_stop_words_returns_nothing_instead_of_failing(
    pg_session: Session, corpus: Corpus
) -> None:
    output = keyword_candidates(pg_session, "the of and", scope=ScopeFilters(), limit=40)

    assert output.candidates == []


# ------------------------------------------------------------------------------------- scoping


@pytest.mark.parametrize("retriever", ["semantic", "keyword"])
def test_project_scoping_is_enforced_inside_the_sql(
    pg_session: Session, corpus: Corpus, retriever: str
) -> None:
    scope = ScopeFilters(project_ids=[corpus.project_id])
    if retriever == "semantic":
        output = semantic_candidates(
            pg_session, _unit_vector(0), model_id=MODEL_ID, scope=scope, limit=40
        )
    else:
        output = keyword_candidates(pg_session, "retrieval pipeline pgvector", scope=scope, limit=40)

    returned = {c.object_id for c in output.candidates}
    assert corpus.chunks["other"] not in returned
    assert all(c.project_id == corpus.project_id for c in output.candidates)


@pytest.mark.parametrize("excluded", ["catalog", "secret"])
def test_catalog_only_and_secret_flagged_sources_are_never_returned(
    pg_session: Session, corpus: Corpus, excluded: str
) -> None:
    semantic = semantic_candidates(
        pg_session, _unit_vector(1), model_id=MODEL_ID, scope=ScopeFilters(), limit=40
    )
    keyword = keyword_candidates(
        pg_session, "retrieval pipeline pgvector", scope=ScopeFilters(), limit=40
    )

    for output in (semantic, keyword):
        assert corpus.chunks[excluded] not in {c.object_id for c in output.candidates}


def test_a_deleted_source_is_hidden_by_default_and_visible_on_request(
    pg_session: Session, corpus: Corpus
) -> None:
    default = keyword_candidates(
        pg_session, "retrieval pipeline pgvector", scope=ScopeFilters(), limit=40
    )
    widened = keyword_candidates(
        pg_session,
        "retrieval pipeline pgvector",
        scope=ScopeFilters(allowed_source_status=("active", "deleted", "moved")),
        limit=40,
    )

    assert corpus.chunks["deleted"] not in {c.object_id for c in default.candidates}
    assert corpus.chunks["deleted"] in {c.object_id for c in widened.candidates}


def test_since_filters_on_the_observation_axis(pg_session: Session, corpus: Corpus) -> None:
    recent_only = keyword_candidates(
        pg_session,
        "retrieval pipeline pgvector",
        scope=ScopeFilters(since=NOW - timedelta(days=90)),
        limit=40,
    )
    everything = keyword_candidates(
        pg_session, "retrieval pipeline pgvector", scope=ScopeFilters(), limit=40
    )

    assert corpus.chunks["old"] not in {c.object_id for c in recent_only.candidates}
    assert corpus.chunks["old"] in {c.object_id for c in everything.candidates}


def test_the_as_of_predicate_hides_an_artifact_closed_before_it(
    pg_session: Session, corpus: Corpus
) -> None:
    now_scope = ScopeFilters(project_ids=[corpus.project_id], as_of=NOW)
    back_then = ScopeFilters(project_ids=[corpus.project_id], as_of=NOW - timedelta(days=100))

    current = keyword_candidates(
        pg_session, "hybrid retrieval", scope=now_scope, limit=40,
        object_types=[ObjectType.ARTIFACT],
    )
    historic = keyword_candidates(
        pg_session, "hybrid retrieval", scope=back_then, limit=40,
        object_types=[ObjectType.ARTIFACT],
    )

    assert corpus.artifacts["closed"] not in {c.object_id for c in current.candidates}
    assert corpus.artifacts["closed"] in {c.object_id for c in historic.candidates}
    assert corpus.artifacts["current"] not in {c.object_id for c in historic.candidates}


def test_unconfirmed_artifacts_are_kept_by_default_and_droppable_on_request(
    pg_session: Session, corpus: Corpus
) -> None:
    kept = keyword_candidates(
        pg_session, "unconfirmed statement", scope=ScopeFilters(as_of=NOW), limit=40,
        object_types=[ObjectType.ARTIFACT],
    )
    dropped = keyword_candidates(
        pg_session, "unconfirmed statement",
        scope=ScopeFilters(as_of=NOW, include_unconfirmed=False), limit=40,
        object_types=[ObjectType.ARTIFACT],
    )

    assert corpus.artifacts["unconfirmed"] in {c.object_id for c in kept.candidates}
    assert corpus.artifacts["unconfirmed"] not in {c.object_id for c in dropped.candidates}


def test_artifact_type_scoping_applies(pg_session: Session, corpus: Corpus) -> None:
    output = keyword_candidates(
        pg_session, "hybrid retrieval",
        scope=ScopeFilters(as_of=NOW, artifact_types=["task"]), limit=40,
        object_types=[ObjectType.ARTIFACT],
    )

    assert output.candidates == []


# ------------------------------------------------------------------------------- end to end


class _StaticEmbedder:
    """Stands in for the embedding-service so the pipeline test needs only PostgreSQL."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    @property
    def dimensions(self) -> int:
        return DIM

    def embed(self, texts):  # pragma: no cover
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self._vector

    def model_identity(self):
        from aimemory.domain.models import EmbeddingModel

        return EmbeddingModel(
            id=MODEL_ID, name="sentence-transformers/all-MiniLM-L6-v2", dimension=DIM
        )

    def health(self) -> bool:
        return True


def test_hybrid_pipeline_fuses_both_retrievers_and_ranks_deterministically(
    pg_session: Session, corpus: Corpus
) -> None:
    retriever = HybridRetriever(_StaticEmbedder(_unit_vector(0)), config=CONFIG)
    query = SearchQuery(query="retrieval pipeline pgvector", project_ids=[corpus.project_id])

    first = retriever.retrieve(pg_session, query, now=NOW)
    second = retriever.retrieve(pg_session, query, now=NOW)

    assert first.warnings == []
    assert first.candidate_counts["semantic"] > 0
    assert first.candidate_counts["keyword"] > 0
    assert first.hits, "a seeded corpus must produce hits"
    assert [h.object_id for h in first.hits] == [h.object_id for h in second.hits]
    assert [h.rank for h in first.hits] == list(range(1, len(first.hits) + 1))
    top = first.hits[0]
    assert top.object_id == corpus.chunks["near"]
    assert "project_match" in top.boosts
    assert top.score == pytest.approx(sum(top.boosts.values()))
    assert top.score == pytest.approx(top.relevance * (1 + sum(top.boost_fractions.values())))
    assert 0.0 <= top.relevance <= 1.0, "the base score is calibrated, not an RRF magnitude"


def test_a_low_trust_clipping_is_penalised_relative_to_an_identical_high_trust_hit(
    pg_session: Session, corpus: Corpus
) -> None:
    outcome = HybridRetriever(_StaticEmbedder(_unit_vector(3)), config=CONFIG).retrieve(
        pg_session,
        SearchQuery(query="retrieval pipeline pgvector", project_ids=[corpus.project_id]),
        now=NOW,
    )

    clipping = next(h for h in outcome.hits if h.object_id == corpus.chunks["clipping"])
    high_trust = next(h for h in outcome.hits if h.object_id == corpus.chunks["near"])

    assert clipping.boost_fractions["low_trust"] == pytest.approx(CONFIG.low_trust_penalty)
    assert "low_trust" not in high_trust.boost_fractions
    # AC-6 is a *relative* demotion since the P14 fix: the penalty is 15 % of the clipping's own
    # relevance, reported under its own key, and it can no longer drive a score negative or push a
    # strong match below an unrelated one.
    assert clipping.boosts["low_trust"] == pytest.approx(
        CONFIG.low_trust_penalty * clipping.relevance
    )
    assert clipping.score == pytest.approx(
        clipping.relevance * (1 + sum(clipping.boost_fractions.values()))
    )
    assert clipping.score > 0.0
    penalty_free = clipping.relevance * (
        1 + sum(v for k, v in clipping.boost_fractions.items() if k != "low_trust")
    )
    assert clipping.score < penalty_free, "the clipping must still rank lower than it otherwise would"


def test_the_pipeline_degrades_to_keyword_only_when_the_embedder_fails(
    pg_session: Session, corpus: Corpus
) -> None:
    class _BrokenEmbedder(_StaticEmbedder):
        def embed_query(self, text: str) -> list[float]:
            raise RuntimeError("embedding-service unreachable")

    outcome = HybridRetriever(_BrokenEmbedder(_unit_vector(0)), config=CONFIG).retrieve(
        pg_session,
        SearchQuery(query="retrieval pipeline pgvector", project_ids=[corpus.project_id]),
        now=NOW,
    )

    assert outcome.degraded is True
    assert outcome.candidate_counts["semantic"] == 0
    assert outcome.hits, "keyword-only still answers the query"


# ------------------------------------------------------ smoke test against whatever is ingested


def test_query_lexemes_and_idf_come_from_the_same_english_configuration_as_the_index(
    pg_session: Session, corpus: Corpus
) -> None:
    """The lexical channel must stem the query exactly like ``chunks.tsv`` was stemmed, or coverage
    silently measures nothing. Only a real Postgres with the ``english`` dictionary can show that."""
    lexicon = analyse_query(pg_session, "Which ADR binds published ports to loopback?")

    assert "adr" in lexicon.lexemes
    assert "loopback" in lexicon.lexemes
    assert "which" not in lexicon.lexemes, "stop words carry no IDF mass"
    # A rarer term must weigh more than a common one; both are positive (Robertson form).
    assert lexicon.idf["loopback"] > 0.0
    assert lexicon.corpus_size > 0


def test_lexical_coverage_is_term_coverage_not_term_frequency(
    pg_session: Session, corpus: Corpus
) -> None:
    """A12's finding 1: a long, vocabulary-heavy chunk must not beat the specific one by repetition.

    ``chunk:adr`` is the only chunk that contains ``ADR-0007`` and ``loopback``; the others share
    the query's common words only.
    """
    lexicon = analyse_query(pg_session, "ADR-0007 loopback published ports")
    keys = [
        (ObjectType.CHUNK, corpus.chunks["adr"]),
        (ObjectType.CHUNK, corpus.chunks["near"]),
        (ObjectType.CHUNK, corpus.chunks["far"]),
    ]

    coverage = lexical_coverage(pg_session, lexicon, keys)

    assert coverage[keys[0]] > coverage[keys[1]]
    assert coverage[keys[0]] > coverage[keys[2]]
    assert all(0.0 <= value <= 1.0 for value in coverage.values())
    # Candidates that match nothing are reported as 0.0, never omitted: "covers nothing" and "was
    # not scored" must not look the same to the ranking stage.
    assert set(coverage) == set(keys)


def test_a_question_the_corpus_cannot_answer_scores_lower_than_one_it_can(
    pg_session: Session, corpus: Corpus
) -> None:
    """A12's finding 2, at the level where it is decided: the lexical channel is what makes an
    absent topic look absent. The corpus contains nothing about Mars colonies."""
    answerable = analyse_query(pg_session, "ADR-0007 loopback published ports")
    absent = analyse_query(pg_session, "Mars colony logistics manifest")
    keys = [(ObjectType.CHUNK, corpus.chunks["adr"])]

    covered = lexical_coverage(pg_session, answerable, keys)[keys[0]]
    uncovered = lexical_coverage(pg_session, absent, keys)[keys[0]]

    assert covered > uncovered
    assert uncovered < 0.5, "no chunk can cover the IDF mass of terms the corpus has never seen"


def test_an_empty_lexicon_skips_the_coverage_statement_instead_of_dividing_by_zero(
    pg_session: Session, corpus: Corpus
) -> None:
    lexicon = analyse_query(pg_session, "the and of a")
    keys = [(ObjectType.CHUNK, corpus.chunks["adr"])]

    assert not lexicon
    assert lexical_coverage(pg_session, lexicon, keys) == {keys[0]: 0.0}


def test_the_ranking_reflects_relevance_rather_than_boost_groups(
    pg_session: Session, corpus: Corpus
) -> None:
    """The P14 regression test: the pipeline's top hit is the best match, not the best-boosted one.

    ``chunk:clipping`` carries the AC-6 ``low_trust`` penalty; before the fix that -0.15 (50x the
    RRF range) pushed an exactly-matching clipping to the bottom of the list with a negative score.
    """
    outcome = HybridRetriever(_StaticEmbedder(_unit_vector(0)), config=CONFIG).retrieve(
        pg_session,
        SearchQuery(query="retrieval pipeline pgvector HNSW tsvector", project_ids=[corpus.project_id]),
        now=NOW,
    )

    assert outcome.hits[0].object_id == corpus.chunks["near"]
    assert all(hit.score >= 0.0 for hit in outcome.hits), "no boost may drive a score negative"
    clipping = next(hit for hit in outcome.hits if hit.object_id == corpus.chunks["clipping"])
    assert clipping.score >= 0.0
    assert all(0.0 <= hit.relevance <= 1.0 for hit in outcome.hits)
    assert "lexical" in outcome.hits[0].boosts, "the lexical channel ran against the real index"


def test_smoke_query_against_the_ingested_corpus_if_there_is_one(
    pg_session: Session, embedding_available: bool
) -> None:
    """Plan acceptance for P9-T01 is "top-k sane on smoke queries" against Tier-1 data.

    The ingested corpus is not this test's to create (that is A07a's pipeline, and it writes outside
    a rollback boundary), so this skips honestly when the database holds no embedded chunks yet.
    """
    from aimemory.providers.embedding.http import HttpEmbeddingProvider

    total = pg_session.execute(
        sa.text("SELECT count(*) FROM embeddings WHERE object_type = 'chunk'")
    ).scalar_one()
    if not total:
        pytest.skip("no Tier-1 corpus is ingested yet; run `aimemory-ingest` first")

    with HttpEmbeddingProvider() as embedder:
        outcome = HybridRetriever(embedder, config=CONFIG).retrieve(
            pg_session, SearchQuery(query="retrieval pipeline"), now=NOW
        )

    assert outcome.warnings == []
    assert 0 < len(outcome.hits) <= CONFIG.final_k
    assert all(hit.metadata.text for hit in outcome.hits)
    assert [hit.rank for hit in outcome.hits] == list(range(1, len(outcome.hits) + 1))
