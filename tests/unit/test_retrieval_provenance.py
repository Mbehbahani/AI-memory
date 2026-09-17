"""P10-T01 (A09): stage 7, provenance attachment and citation rendering (``retrieval.md`` §7).

The acceptance criterion is absolute - **provenance completeness 100 % on returned evidence** - so
the behaviour that matters most is the *negative* one: a hit whose stamp cannot be resolved is
dropped and the caller is told, rather than cited without a traceable origin.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from aimemory.common.hashing import short_hash
from aimemory.domain.enums import ObjectType
from aimemory.domain.provenance import Provenance
from aimemory.retrieval.provenance import (
    DEFAULT_CITATION_FORMAT,
    PROVENANCE_MISSING_WARNING,
    attach_provenance,
    render_citation,
)
from aimemory.retrieval.types import HitMetadata, RankedCandidate

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
HASH = "sha256:" + "ab" * 32


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p10-prov:{label}")


def _stamp(label: str, **extra: Any) -> dict[str, Any]:
    row = {
        "object_id": _id(label),
        "source_id": _id(f"src-{label}"),
        "source_uri": f"vault://notes/{label}.md",
        "source_hash": HASH,
        "source_version": _id(f"ver-{label}"),
        "project_id": "joblab-de",
        "device_id": "local-development-machine",
        "observed_at": NOW,
        "valid_from": None,
        "valid_to": None,
        "confidence": 1.0,
        "extraction_model_id": None,
        "embedding_model_id": "minilm-l6-v2-384",
        "ingestion_run_id": None,
        "episode_id": None,
        "heading_path": ["Design", "Retrieval"],
        "char_start": 0,
        "char_end": 120,
    }
    row.update(extra)
    return row


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)


class FakeSession:
    def __init__(self, chunk_rows: list[dict[str, Any]], artifact_rows: list[dict[str, Any]]) -> None:
        self.routes = {"FROM chunks c": chunk_rows, "FROM provenance_v p": artifact_rows}
        self.executed: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.executed.append(sql)
        for marker, rows in self.routes.items():
            if marker in sql:
                return _Result(rows)
        return _Result([])


def _hit(label: str, object_type: ObjectType = ObjectType.CHUNK, **meta: Any) -> RankedCandidate:
    metadata = HitMetadata(
        object_type=object_type,
        object_id=_id(label),
        text=f"text of {label}",
        project_id="joblab-de",
        observed_at=NOW,
        **meta,
    )
    return RankedCandidate(
        object_type=object_type,
        object_id=_id(label),
        rrf_score=0.5,
        score=0.6,
        boosts={"project_match": 0.1},
        rank=1,
        metadata=metadata,
    )


def test_citation_renders_the_configured_format() -> None:
    provenance = Provenance(**{k: v for k, v in _stamp("a").items() if k != "object_id"})

    citation = render_citation(provenance, DEFAULT_CITATION_FORMAT)

    assert citation == f"[vault://notes/a.md#Design > Retrieval @{short_hash(HASH)}]"


def test_a_malformed_citation_format_falls_back_instead_of_failing_the_query() -> None:
    provenance = Provenance(**{k: v for k, v in _stamp("a").items() if k != "object_id"})

    citation = render_citation(provenance, "[{not_a_placeholder}]")

    assert citation.startswith("[vault://notes/a.md#")


def test_a_stamp_without_a_source_still_renders_something_honest() -> None:
    manual = Provenance(device_id="local-development-machine", observed_at=NOW)

    assert render_citation(manual) == "[unknown# @unknown]"


def test_every_returned_hit_has_provenance_and_a_citation() -> None:
    session = FakeSession([_stamp("a")], [])

    hits, warnings = attach_provenance(session, [_hit("a")])

    assert warnings == []
    assert len(hits) == 1
    assert hits[0].provenance.is_complete is True
    assert hits[0].citation.startswith("[vault://notes/a.md#")
    assert hits[0].rank == 1


def test_a_hit_whose_provenance_cannot_be_resolved_is_dropped_with_a_warning() -> None:
    session = FakeSession([_stamp("a")], [])  # nothing for 'b'

    hits, warnings = attach_provenance(session, [_hit("a"), _hit("b")])

    assert [hit.object_id for hit in hits] == [_id("a")]
    assert warnings == [PROVENANCE_MISSING_WARNING]


def test_ranks_are_renumbered_after_a_drop() -> None:
    session = FakeSession([_stamp("a"), _stamp("c")], [])
    hits = [_hit("a"), _hit("b"), _hit("c")]
    for position, hit in enumerate(hits, start=1):
        hit.rank = position

    scored, _ = attach_provenance(session, hits)

    assert [hit.rank for hit in scored] == [1, 2]


def test_entity_ids_from_the_expansion_reach_the_returned_hit() -> None:
    session = FakeSession([_stamp("a")], [])
    entity_id = _id("entity-1")

    hits, _ = attach_provenance(
        session, [_hit("a")], entity_ids_by_hit={(ObjectType.CHUNK, _id("a")): [entity_id]}
    )

    assert hits[0].entity_ids == [entity_id]


def test_artifacts_are_resolved_from_provenance_v_and_chunks_from_the_chunk_chain() -> None:
    session = FakeSession([_stamp("chunk-a")], [_stamp("art-a")])

    hits, warnings = attach_provenance(
        session, [_hit("chunk-a"), _hit("art-a", ObjectType.ARTIFACT)]
    )

    assert warnings == []
    assert {hit.object_type for hit in hits} == {ObjectType.CHUNK, ObjectType.ARTIFACT}
    assert any("provenance_v" in sql for sql in session.executed)
    assert any("FROM chunks c" in sql for sql in session.executed)


def test_scores_and_boosts_survive_the_conversion_unchanged() -> None:
    session = FakeSession([_stamp("a")], [])

    hits, _ = attach_provenance(session, [_hit("a")])

    assert hits[0].score == 0.6
    assert hits[0].rrf_score == 0.5
    assert hits[0].boosts == {"project_match": 0.1}
