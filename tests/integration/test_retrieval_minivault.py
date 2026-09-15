"""P9-T01 (A09), integration: hybrid retrieval over *real* MiniLM vectors of real fixture text.

``tests/integration/test_retrieval_candidates.py`` pins the SQL with synthetic unit vectors, where
"nearer" is arithmetic and the assertions cannot be wrong. This module answers the other half of
P9-T01's acceptance - "top-k sane on smoke queries" - by building a miniature Tier-1 corpus the same
way ingestion does (real markdown chunker, real ``embedding-service`` vectors, real pgvector index)
out of ``tests/fixtures/mini-vault``, and asking it questions a human can check.

It never touches ``D:\\My-Vault`` or the pilot repo: the ``tmp_source_root`` fixture copies the
committed fixture into pytest's ``tmp_path`` and nothing else is read. The whole corpus lives inside
``pg_session``'s transaction and is rolled back at teardown.

Skips cleanly (never errors) when PostgreSQL or the embedding-service is unavailable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from aimemory.chunking import get_chunker
from aimemory.domain.enums import ObjectType
from aimemory.domain.retrieval import RetrievalConfig, SearchQuery
from aimemory.providers.embedding.http import HttpEmbeddingProvider
from aimemory.retrieval.candidates import vector_literal
from aimemory.retrieval.pipeline import HybridRetriever
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_available")]

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
DEVICE_ID = "local-development-machine"
MODEL_ID = "minilm-l6-v2-384"
PROJECT_ID = "t-p9-minivault"
CONFIG = RetrievalConfig()

#: The fixture files that carry distinguishable topics. ``README.md`` and the maps are left out:
#: they mention every topic at once and would make "which note answers this?" meaningless.
FIXTURE_FILES = (
    "architecture-decision-a.md",
    "architecture-decision-b.md",
    "01 Projects/requirement-fixture-storage.md",
    "01 Projects/Fixture Project.md",
    "03 Resources/research-finding-fixture.md",
    "02 Areas/Fixture Research Track.md",
    "00 Inbox/daily-note-2026-09-05.md",
)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p9-minivault:{label}")


def _exec(session: Session, sql: str, params: dict[str, Any]) -> None:
    session.execute(sa.text(sql), params)


@pytest.fixture(scope="module")
def embedder(embedding_available: bool):
    with HttpEmbeddingProvider() as provider:
        yield provider


@pytest.fixture()
def minivault_corpus(pg_session: Session, tmp_source_root, embedder) -> dict[UUID, str]:
    """Ingest the fixture vault the Tier-1 way: chunk -> embed -> ``chunks`` + ``embeddings``.

    Returns ``{chunk_id: relative_path}`` so an assertion can name the note a hit came from.
    """
    session = pg_session
    _exec(
        session,
        "INSERT INTO projects (id, name, track, status) "
        "VALUES (:id, 'P9 Mini Vault', 'research', 'active')",
        {"id": PROJECT_ID},
    )
    _exec(
        session,
        "INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, "
        "default_project_id) VALUES ('t-p9-mv-root', 'vault', 't-p9-mv-root', '/sources/vault', "
        ":device, :project)",
        {"device": DEVICE_ID, "project": PROJECT_ID},
    )

    chunker = get_chunker("markdown")
    origin: dict[UUID, str] = {}
    texts: list[str] = []
    rows: list[dict[str, Any]] = []

    for relative in FIXTURE_FILES:
        body = tmp_source_root.read(relative)
        source_id, version_id = _id(f"source:{relative}"), _id(f"version:{relative}")
        _exec(
            session,
            """
            INSERT INTO sources (id, uri, root_id, relative_path, project_id, kind, media_type,
                                 policy, status, trust, secret_suspected, origin)
            VALUES (:id, :uri, 't-p9-mv-root', :rel, :project, 'file', 'text/markdown',
                    'INDEX_CONTENT', 'active', 'high', false, 'internal')
            """,
            {
                "id": source_id,
                "uri": f"vault://t-p9-mv/{relative}",
                "rel": relative,
                "project": PROJECT_ID,
            },
        )
        _exec(
            session,
            """
            INSERT INTO source_versions (id, source_id, content_hash, size_bytes, observed_at,
                                         change_type)
            VALUES (:id, :source_id, :hash, :size, :observed_at, 'new')
            """,
            {
                "id": version_id,
                "source_id": source_id,
                "hash": f"sha256:{_id('content:' + relative).hex * 2}",
                "size": len(body.encode("utf-8")),
                "observed_at": NOW,
            },
        )
        for draft in chunker.chunk(body, relative_path=relative):
            chunk_id = _id(f"chunk:{relative}:{draft.ordinal}")
            _exec(
                session,
                """
                INSERT INTO chunks (id, version_id, source_id, project_id, ordinal, text,
                                    text_hash, heading_path, char_start, char_end, token_count)
                VALUES (:id, :version_id, :source_id, :project, :ordinal, :text, :hash,
                        CAST(:heading AS text[]), :start, :end, :tokens)
                """,
                {
                    "id": chunk_id,
                    "version_id": version_id,
                    "source_id": source_id,
                    "project": PROJECT_ID,
                    "ordinal": draft.ordinal,
                    "text": draft.text,
                    "hash": draft.text_hash,
                    "heading": draft.heading_path,
                    "start": draft.char_start,
                    "end": draft.char_end,
                    "tokens": draft.token_count,
                },
            )
            origin[chunk_id] = relative
            texts.append(draft.text)
            rows.append({"chunk_id": chunk_id, "text_hash": draft.text_hash})

    # One batched call to the real embedding-service, exactly as the Tier-1 embed step does.
    result = embedder.embed(texts)
    assert result.dimension == 384
    seen: set[str] = set()
    for row, vector in zip(rows, result.vectors, strict=True):
        if row["text_hash"] in seen:  # the (text_hash, model_id) reuse key is unique
            continue
        seen.add(row["text_hash"])
        _exec(
            session,
            """
            INSERT INTO embeddings (id, object_type, object_id, text_hash, model_id, dimension,
                                    vector)
            VALUES (:id, 'chunk', :object_id, :hash, :model, 384, CAST(:vector AS vector))
            ON CONFLICT (text_hash, model_id) DO NOTHING
            """,
            {
                "id": _id(f"embedding:{row['chunk_id']}"),
                "object_id": row["chunk_id"],
                "hash": row["text_hash"],
                "model": MODEL_ID,
                "vector": vector_literal(vector),
            },
        )
    session.flush()
    return origin


def _hit_sources(outcome, origin: dict[UUID, str]) -> list[str]:
    return [origin.get(hit.object_id, str(hit.object_id)) for hit in outcome.hits]


@pytest.mark.parametrize(
    ("question", "expected_note"),
    [
        ("Where does the system of record live?", "01 Projects/requirement-fixture-storage.md"),
        (
            "does hybrid search beat vector-only retrieval?",
            "03 Resources/research-finding-fixture.md",
        ),
    ],
)
def test_a_natural_language_question_puts_the_right_note_on_top(
    pg_session: Session, minivault_corpus: dict[UUID, str], embedder, question: str,
    expected_note: str,
) -> None:
    """A smoke check a human can verify by reading the two notes - not a tuned metric.

    It is deliberately weak (the right note must be in the top 3, not exactly first): P9-T01 owns
    "top-k sane", and the measured quality numbers belong to the P13 gold set, where they are
    reported with their method rather than asserted here.
    """
    outcome = HybridRetriever(embedder, config=CONFIG).retrieve(
        pg_session, SearchQuery(query=question, project_ids=[PROJECT_ID]), now=NOW
    )

    assert outcome.warnings == []
    assert expected_note in _hit_sources(outcome, minivault_corpus)[:3]


def test_an_exact_identifier_is_found_by_the_keyword_side(
    pg_session: Session, minivault_corpus: dict[UUID, str], embedder
) -> None:
    outcome = HybridRetriever(embedder, config=CONFIG).retrieve(
        pg_session, SearchQuery(query="ADR-0001", project_ids=[PROJECT_ID]), now=NOW
    )

    top = outcome.hits[0]
    assert "ADR-0001" in top.metadata.text
    assert "keyword" in [r.value for r in top.retrievers]


def test_the_top_k_is_sane_bounded_ranked_and_every_hit_has_text(
    pg_session: Session, minivault_corpus: dict[UUID, str], embedder
) -> None:
    outcome = HybridRetriever(embedder, config=CONFIG).retrieve(
        pg_session,
        SearchQuery(query="architecture decision for the ingestion platform"),
        now=NOW,
    )

    assert 0 < len(outcome.hits) <= CONFIG.final_k
    assert [hit.rank for hit in outcome.hits] == list(range(1, len(outcome.hits) + 1))
    assert all(hit.metadata.text.strip() for hit in outcome.hits)
    assert all(hit.metadata.source_uri for hit in outcome.hits)
    assert all(hit.object_type is ObjectType.CHUNK for hit in outcome.hits)
    scores = [hit.score for hit in outcome.hits]
    assert scores == sorted(scores, reverse=True)
    assert outcome.candidate_counts["semantic"] > 0
    assert outcome.candidate_counts["fused"] <= CONFIG.fused_top_k


def test_the_same_query_twice_returns_the_identical_ranking(
    pg_session: Session, minivault_corpus: dict[UUID, str], embedder
) -> None:
    retriever = HybridRetriever(embedder, config=CONFIG)
    query = SearchQuery(query="reciprocal rank fusion", project_ids=[PROJECT_ID])

    first = retriever.retrieve(pg_session, query, now=NOW)
    second = retriever.retrieve(pg_session, query, now=NOW)

    assert [h.object_id for h in first.hits] == [h.object_id for h in second.hits]
    assert [h.score for h in first.hits] == [h.score for h in second.hits]
