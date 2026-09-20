"""P8-T06 (A08), integration: ``entity_mentions.chunk_id`` is what seeds graph expansion.

Stage 4 of retrieval resolves a fused **chunk** hit to entity ids through one column
(``retrieval.md`` section 4 step 1, :func:`aimemory.retrieval.expansion.seed_entity_ids`). Tier 2
wrote that column as NULL for every row, so no chunk hit could ever seed the graph and
``related_entities`` was always empty. What only a database can prove, and what this module
therefore asserts against a real PostgreSQL:

* the reconcile in :mod:`aimemory.knowledge.mentions` attaches a mention to **every** chunk that
  quotes it, so a hit on any of them seeds the same entity - overlapping chunks included;
* an entity the text never spells out keeps **exactly one** row with ``chunk_id IS NULL``: the
  episode's assertion survives, and no chunk is invented for it;
* ``seed_entity_ids`` - A09's own function, imported unchanged - then returns those entities for the
  chunks, which is the end-to-end link between the write path and graph expansion;
* running the reconcile twice changes nothing (idempotent), because the corpus is re-reconciled
  after every ingestion run.

The fixture is seeded inside ``pg_session``'s transaction and rolled back at teardown; no source root
is touched and the live corpus is never modified.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import ObjectType
from aimemory.knowledge.mentions import backfill_mention_chunks
from aimemory.retrieval.expansion import seed_entity_ids
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_available")]

NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
DEVICE_ID = "local-development-machine"
ROOT_ID = "t-p8t06-root"
PROJECT_ID = "t-p8t06"

#: One document, two subjects. "Databricks" is quoted twice, at offsets 44 and 118, and each
#: occurrence falls inside several of the overlapping 60/40 chunks; "Snowflake" is never written -
#: the extractor inferred it from context, which is the case that must stay a NULL.
DOCUMENT = (
    "# JobLab DE Lakehouse\n"
    "The lakehouse runs on Databricks and stores Delta tables.\n"
    "Ingestion is PySpark; orchestration is Databricks Workflows.\n"
    "Costs are tracked per DBU and reviewed monthly by the owner.\n"
)
CHUNK_SIZE, CHUNK_OVERLAP = 60, 40


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p8t06:{label}")


def _exec(session: Session, sql: str, params: dict[str, Any] | None = None) -> None:
    session.execute(sa.text(sql), params or {})


def _seed_chunks(session: Session, source_id: UUID, version_id: UUID) -> list[UUID]:
    """Overlapping chunks whose ``text`` is exactly ``DOCUMENT[char_start:char_end]``."""
    ids: list[UUID] = []
    start, ordinal = 0, 0
    while start < len(DOCUMENT):
        end = min(start + CHUNK_SIZE, len(DOCUMENT))
        chunk_id = _id(f"chunk:{ordinal}")
        _exec(
            session,
            """
            INSERT INTO chunks (id, version_id, source_id, ordinal, text, text_hash, heading_path,
                                char_start, char_end, token_count)
            VALUES (:id, :version, :source, :ordinal, :text, :hash, ARRAY['JobLab DE Lakehouse'],
                    :start, :end, :tokens)
            """,
            {
                "id": chunk_id,
                "version": version_id,
                "source": source_id,
                "ordinal": ordinal,
                "text": DOCUMENT[start:end],
                "hash": f"sha256:{_id(f'chunk-hash:{ordinal}').hex}",
                "start": start,
                "end": end,
                "tokens": max(1, (end - start) // 4),
            },
        )
        ids.append(chunk_id)
        if end == len(DOCUMENT):
            break
        start, ordinal = end - CHUNK_OVERLAP, ordinal + 1
    return ids


@pytest.fixture()
def fixture(pg_session: Session) -> dict[str, Any]:
    session = pg_session
    source_id, version_id, episode_id = _id("source"), _id("version"), _id("episode")
    # Own project slug: the live corpus already owns a project-less ``Technology/databricks`` row,
    # and ``uq_entities_type_normalized_project`` is the ADR-0010 duplicate guard.
    _exec(
        session,
        "INSERT INTO projects (id, name, track, status) "
        "VALUES (:p, 'P8-T06 fixture', 'business', 'active')",
        {"p": PROJECT_ID},
    )
    _exec(
        session,
        "INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, enabled) "
        "VALUES (:root, 'vault', :root, '/sources/vault', :device, true)",
        {"root": ROOT_ID, "device": DEVICE_ID},
    )
    _exec(
        session,
        """
        INSERT INTO sources (id, uri, root_id, relative_path, kind, media_type, policy, status,
                             trust, secret_suspected, origin)
        VALUES (:id, 'vault://p8t06/lakehouse.md', :root, 'p8t06/lakehouse.md', 'file',
                'text/markdown', 'INDEX_CONTENT', 'active', 'high', false, 'internal')
        """,
        {"id": source_id, "root": ROOT_ID},
    )
    _exec(
        session,
        """
        INSERT INTO source_versions (id, source_id, content_hash, size_bytes, observed_at,
                                     change_type)
        VALUES (:id, :source, :hash, :size, :observed_at, 'new')
        """,
        {
            "id": version_id,
            "source": source_id,
            "hash": f"sha256:{_id('content').hex * 2}",
            "size": len(DOCUMENT),
            "observed_at": NOW,
        },
    )
    chunk_ids = _seed_chunks(session, source_id, version_id)
    _exec(
        session,
        """
        INSERT INTO episodes (id, type, source_id, version_id, title, body, observed_at, status,
                              tier)
        VALUES (:id, 'document', :source, :version, 'lakehouse.md', :body, :observed_at,
                'extracted', 2)
        """,
        {
            "id": episode_id,
            "source": source_id,
            "version": version_id,
            "body": DOCUMENT,
            "observed_at": NOW,
        },
    )

    entities = {"databricks": _id("entity:databricks"), "snowflake": _id("entity:snowflake")}
    _exec(
        session,
        "INSERT INTO entities (id, type, canonical_name, normalized_name, project_id, engine) "
        "VALUES (:a, 'Technology', 'Databricks', 'databricks', :p, 'native'), "
        "(:b, 'Technology', 'Snowflake', 'snowflake', :p, 'native')",
        {"a": entities["databricks"], "b": entities["snowflake"], "p": PROJECT_ID},
    )
    for key, surface in (("databricks", "Databricks"), ("snowflake", "Snowflake")):
        _exec(
            session,
            """
            INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form,
                                         source_id, source_uri, source_hash, source_version,
                                         device_id, observed_at, valid_from, extraction_model_id)
            VALUES (:id, :entity, :episode, NULL, :surface, :source, 'vault://p8t06/lakehouse.md',
                    :hash, :version, :device, :observed_at, :observed_at,
                    'deterministic:registry-v1')
            """,
            {
                "id": _id(f"mention:{key}"),
                "entity": entities[key],
                "episode": episode_id,
                "surface": surface,
                "source": source_id,
                "hash": f"sha256:{_id('content').hex * 2}",
                "version": version_id,
                "device": DEVICE_ID,
                "observed_at": NOW,
            },
        )
    session.flush()
    return {"chunks": chunk_ids, "entities": entities, "episode_id": episode_id}


def _rows(session: Session, entity_id: UUID) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in session.execute(
            sa.text(
                "SELECT id, chunk_id, char_start, char_end, heading_path FROM entity_mentions "
                "WHERE entity_id = :e ORDER BY char_start NULLS LAST"
            ),
            {"e": entity_id},
        ).mappings()
    ]


def test_a_quoted_entity_is_attached_to_every_chunk_that_contains_it(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """Chunks overlap; a hit on any of them has to seed the same entity, so all of them get a row."""
    backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])

    rows = _rows(pg_session, fixture["entities"]["databricks"])

    assert len(rows) >= 4, "two occurrences, each inside several overlapping chunks"
    assert all(row["chunk_id"] is not None for row in rows)
    for row in rows:
        quoted = DOCUMENT[row["char_start"] : row["char_end"]]
        assert quoted == "Databricks", f"offsets must point at the surface form, got {quoted!r}"
        assert row["heading_path"] == ["JobLab DE Lakehouse"]


def test_an_inferred_entity_keeps_one_null_row_instead_of_a_guessed_chunk(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """The episode did name Snowflake; the text never did. Both facts survive, neither is invented."""
    report = backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])

    rows = _rows(pg_session, fixture["entities"]["snowflake"])

    assert len(rows) == 1
    assert rows[0]["chunk_id"] is None
    assert report.by_reason.get("not_quoted", 0) >= 1


def test_the_seed_query_used_by_graph_expansion_now_returns_those_entities(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """The point of the whole exercise: A09's own seeding function, unchanged, resolving a hit."""
    backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])

    keys = [(ObjectType.CHUNK, chunk_id) for chunk_id in fixture["chunks"]]
    seeds = seed_entity_ids(pg_session, keys)

    seeded = {key: ids for key, ids in seeds.items() if ids}
    assert seeded, "a chunk hit must seed at least one entity, or expansion can never start"
    assert all(
        fixture["entities"]["databricks"] in ids for ids in seeded.values()
    ), "every seeded chunk here quotes Databricks"
    assert not any(fixture["entities"]["snowflake"] in ids for ids in seeds.values())


def test_running_the_reconcile_twice_changes_nothing(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """It runs after every ingestion run; a second pass must be a no-op, not a duplicate row."""
    first = backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])
    before = pg_session.execute(sa.text("SELECT count(*) FROM entity_mentions")).scalar()

    second = backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])
    after = pg_session.execute(sa.text("SELECT count(*) FROM entity_mentions")).scalar()

    assert first.rows_updated + first.rows_inserted > 0
    assert (second.rows_updated, second.rows_inserted, second.rows_deleted) == (0, 0, 0)
    assert second.unchanged == second.groups
    assert after == before


def test_a_mention_that_should_not_be_there_is_removed_by_the_reconcile(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """A stale link (older matcher, changed alias table, re-chunked source) must be repairable."""
    stale_chunk = fixture["chunks"][-1]
    _exec(
        pg_session,
        "UPDATE entity_mentions SET chunk_id = :c, char_start = 0, char_end = 9 "
        "WHERE entity_id = :e",
        {"c": stale_chunk, "e": fixture["entities"]["snowflake"]},
    )

    report = backfill_mention_chunks(pg_session, episode_ids=[fixture["episode_id"]])

    rows = _rows(pg_session, fixture["entities"]["snowflake"])
    assert rows[0]["chunk_id"] is None
    assert report.rows_updated >= 1


def test_the_writer_attaches_chunks_at_extraction_time_not_only_in_a_repair_run(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """The forward path: a *new* episode must never write a NULL chunk for a quoted entity again.

    The backfill exists for history. What keeps the column populated is
    :class:`~aimemory.knowledge.persist.KnowledgeWriter`, so it is exercised here through its real
    entry point - entity resolution, temporal rules and provenance included, graph projection off.
    """
    from aimemory.domain.enums import EngineKind, EntityType, EpisodeType
    from aimemory.domain.extraction import ExtractedEntity, ExtractionResult
    from aimemory.domain.models import Episode
    from aimemory.knowledge.persist import KnowledgeWriter

    episode = Episode(
        id=_id("episode:forward"),
        type=EpisodeType.DOCUMENT,
        source_id=_id("source"),
        version_id=_id("version"),
        title="lakehouse.md",
        # A section-level episode: distinct from the fixture's document-level row under
        # ``uq_episodes_version_section``, and it exercises the heading-path narrowing too.
        section_path=["JobLab DE Lakehouse"],
        body=DOCUMENT,
        observed_at=NOW,
    )
    _exec(
        pg_session,
        """
        INSERT INTO episodes (id, type, source_id, version_id, title, section_path, body,
                              observed_at, status, tier)
        VALUES (:id, 'document', :source, :version, :title, CAST(:section AS text[]), :body,
                :observed_at, 'extracted', 2)
        """,
        {
            "id": episode.id,
            "section": list(episode.section_path),
            "source": episode.source_id,
            "version": episode.version_id,
            "title": episode.title,
            "body": DOCUMENT,
            "observed_at": NOW,
        },
    )
    result = ExtractionResult(
        episode_id=episode.id,
        engine=EngineKind.NATIVE,
        extraction_model_id="deterministic:p8t06-test",
        entities=[
            ExtractedEntity(name="Databricks", type=EntityType.TECHNOLOGY),
            ExtractedEntity(name="Snowflake", type=EntityType.TECHNOLOGY),
        ],
        valid=True,
    )

    report = KnowledgeWriter(
        pg_session, device_id=DEVICE_ID, graph=None, write_summary_artifact=False
    ).write(result, episode)

    rows = [
        dict(row)
        for row in pg_session.execute(
            sa.text(
                "SELECT surface_form, chunk_id, char_start, heading_path FROM entity_mentions "
                "WHERE episode_id = :e"
            ),
            {"e": episode.id},
        ).mappings()
    ]
    quoted = [r for r in rows if r["surface_form"] == "Databricks"]
    inferred = [r for r in rows if r["surface_form"] == "Snowflake"]

    assert report.mentions_located == 1 and report.mentions_unlocated == 1
    assert report.provenance_incomplete == 0
    assert len(quoted) >= 4 and all(r["chunk_id"] is not None for r in quoted)
    assert all(DOCUMENT[r["char_start"] : r["char_start"] + 10] == "Databricks" for r in quoted)
    assert len(inferred) == 1 and inferred[0]["chunk_id"] is None


def test_a_document_change_episode_is_not_starved_by_its_sentinel_section_path(
    pg_session: Session, fixture: dict[str, Any]
) -> None:
    """MEASURED regression: ``section_path = ['__change__']`` made a changed document unlocatable.

    ``aimemory.sources.pipeline`` gives the ``document_change`` episode that sentinel so it can
    coexist with the document episode under ``uq_episodes_version_section`` - its body is still the
    whole file. Narrowing the chunk search on it matched nothing, so every entity of every modified
    document was reported "not quoted" and seeded nothing.
    """
    change_id = _id("episode:change")
    _exec(
        pg_session,
        """
        INSERT INTO episodes (id, type, source_id, version_id, title, section_path, body,
                              observed_at, status, tier)
        VALUES (:id, 'document_change', :source, :version, 'change: lakehouse.md',
                ARRAY['__change__'], :body, :observed_at, 'extracted', 2)
        """,
        {
            "id": change_id,
            "source": _id("source"),
            "version": _id("version"),
            "body": DOCUMENT,
            "observed_at": NOW,
        },
    )
    _exec(
        pg_session,
        """
        INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form, source_id,
                                     source_uri, source_hash, source_version, device_id,
                                     observed_at, valid_from, extraction_model_id)
        VALUES (:id, :entity, :episode, NULL, 'Databricks', :source,
                'vault://p8t06/lakehouse.md', :hash, :version, :device, :observed_at,
                :observed_at, 'deterministic:registry-v1')
        """,
        {
            "id": _id("mention:change"),
            "entity": fixture["entities"]["databricks"],
            "episode": change_id,
            "source": _id("source"),
            "hash": f"sha256:{_id('content').hex * 2}",
            "version": _id("version"),
            "device": DEVICE_ID,
            "observed_at": NOW,
        },
    )

    report = backfill_mention_chunks(pg_session, episode_ids=[change_id])

    rows = [
        dict(row)
        for row in pg_session.execute(
            sa.text("SELECT chunk_id FROM entity_mentions WHERE episode_id = :e"),
            {"e": change_id},
        ).mappings()
    ]
    assert report.by_reason.get("no_chunks", 0) == 0
    assert len(rows) >= 4 and all(row["chunk_id"] is not None for row in rows)
