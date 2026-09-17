"""P10-T01 (A09): stage 8, context assembly - order, budget, truncation policy and track labels.

``retrieval.md`` §8 is an algorithm, so it is tested as one: blocks appear in ``context.block_order``,
only ``evidence_chunks`` may be cut mid-way, everything else is dropped whole and named in
``dropped_blocks``, and ``AssembledContext`` itself refuses to validate if the budget is exceeded.

The other thing proved here is plan section Q's rule that **business and research results are
labelled and never merged silently**.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from uuid import UUID

from aimemory.domain.enums import EntityType, ObjectType, RetrieverKind
from aimemory.domain.provenance import Provenance
from aimemory.domain.retrieval import RelatedEntity, RetrievalConfig, ScoredHit
from aimemory.retrieval.context import (
    TRACK_MIX_WARNING,
    ContextInputs,
    DecisionRow,
    ProjectSummary,
    assemble_context,
    estimate_tokens,
    track_mix_warning,
)
from aimemory.retrieval.temporal import FactRow

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
CONFIG = RetrievalConfig(
    token_budget=6000,
    block_order=[
        "project_summary",
        "current_facts",
        "decisions",
        "evidence_chunks",
        "related_entities",
    ],
)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p10-context:{label}")


def _provenance(label: str, uri: str = "vault://notes/a.md") -> Provenance:
    return Provenance(
        source_id=_id(f"src-{label}"),
        source_uri=uri,
        source_hash="sha256:" + "ab" * 32,
        source_version=_id(f"ver-{label}"),
        device_id="local-development-machine",
        observed_at=NOW,
        heading_path=["Design", "Retrieval"],
    )


def _hit(label: str, *, project_id: str, rank: int, text: str = "evidence text", **extra) -> ScoredHit:
    provenance = _provenance(label)
    return ScoredHit(
        object_type=ObjectType.CHUNK,
        object_id=_id(label),
        text=text,
        score=1.0 - rank / 100,
        rrf_score=0.5,
        retrievers=[RetrieverKind.SEMANTIC],
        rank=rank,
        project_id=project_id,
        provenance=provenance,
        citation=provenance.citation(),
        **extra,
    )


def _fact(label: str, *, project_id: str, statement: str, status: str = "current") -> FactRow:
    return FactRow(
        id=_id(label),
        statement=statement,
        predicate="USES",
        status=status,
        confidence=1.0,
        project_id=project_id,
        subject_name="JobLab DE",
        object_name="Postgres",
        valid_from=NOW,
        valid_to=None,
        observed_at=NOW,
        provenance=_provenance(label),
    )


def _decision(label: str, *, project_id: str, title: str, supersedes: str | None = None) -> DecisionRow:
    return DecisionRow(
        id=_id(label),
        title=title,
        body="Because the alternative was worse.",
        status="current",
        project_id=project_id,
        valid_from=NOW,
        supersedes_title=supersedes,
        citation=_provenance(label).citation(),
        provenance=_provenance(label),
    )


TRACKS = {"biz": "business", "res": "research"}


def _inputs(**overrides) -> ContextInputs:
    base = ContextInputs(
        projects=[
            ProjectSummary(
                project_id="biz",
                name="JobLab DE",
                track="business",
                status="active",
                summary="A data engineering pilot.",
                coverage_note="12/12 indexable sources embedded",
            )
        ],
        facts=[_fact("f1", project_id="biz", statement="JobLab DE uses Postgres")],
        decisions=[_decision("d1", project_id="biz", title="Use RRF")],
        hits=[_hit("h1", project_id="biz", rank=1)],
        related=[
            RelatedEntity(
                entity_id=_id("e1"),
                name="pgvector",
                type=EntityType.TECHNOLOGY,
                predicate="USES",
                project_id="biz",
            )
        ],
        tracks=TRACKS,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_token_estimate_is_the_documented_four_characters_per_token() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil(5/4)


def test_blocks_are_emitted_in_config_block_order() -> None:
    context = assemble_context(_inputs(), config=CONFIG)

    assert [block.kind for block in context.blocks] == CONFIG.block_order
    assert context.truncated is False
    assert context.dropped_blocks == []
    assert context.tokens_used == sum(block.tokens for block in context.blocks)
    assert context.tokens_used <= context.token_budget


def test_every_block_carries_its_own_citations_and_the_context_collects_them() -> None:
    context = assemble_context(_inputs(), config=CONFIG)

    evidence = next(b for b in context.blocks if b.kind == "evidence_chunks")
    assert evidence.citations, "evidence must be cited"
    assert all(citation in context.citations for citation in evidence.citations)
    assert context.citations, "the assembled context exposes the union of its citations"


def test_an_empty_input_produces_no_blocks_rather_than_empty_ones() -> None:
    context = assemble_context(ContextInputs(tracks=TRACKS), config=CONFIG)

    assert context.blocks == []
    assert context.tokens_used == 0


def test_only_evidence_chunks_may_be_truncated_and_it_says_so() -> None:
    inputs = _inputs(
        hits=[_hit(f"h{i}", project_id="biz", rank=i, text="x" * 400) for i in range(1, 30)]
    )

    context = assemble_context(inputs, config=CONFIG, token_budget=200)

    kinds = [block.kind for block in context.blocks]
    evidence = [block for block in context.blocks if block.kind == "evidence_chunks"]
    assert context.truncated is True
    assert context.tokens_used <= 200
    if evidence:
        assert "truncated" in evidence[0].flags
        assert "[truncated: token budget reached]" in evidence[0].text
    # Whatever else did not fit was dropped whole, never half-emitted.
    for kind in ("current_facts", "decisions", "project_summary"):
        assert kind in kinds or kind in context.dropped_blocks


def test_a_block_that_cannot_fit_is_dropped_whole_and_named() -> None:
    inputs = _inputs(
        decisions=[
            _decision("d-big", project_id="biz", title="A very long decision " + "y" * 4000)
        ],
        hits=[],
        related=[],
    )

    context = assemble_context(inputs, config=CONFIG, token_budget=120)

    assert "decisions" in context.dropped_blocks
    assert all(block.kind != "decisions" for block in context.blocks)
    assert context.truncated is True


def test_an_unknown_block_name_degrades_the_context_instead_of_raising() -> None:
    context = assemble_context(_inputs(), config=CONFIG, block_order=["project_summary", "nope"])

    assert [block.kind for block in context.blocks] == ["project_summary"]
    assert context.dropped_blocks == ["nope"]


# ----------------------------------------------------------------- track labelling (plan §Q)


def test_business_and_research_hits_are_labelled_and_never_merged_silently() -> None:
    inputs = _inputs(
        hits=[
            _hit("h-biz", project_id="biz", rank=1, text="a business chunk"),
            _hit("h-res", project_id="res", rank=2, text="a research chunk"),
        ],
        facts=[
            _fact("f-biz", project_id="biz", statement="JobLab DE uses Postgres"),
            _fact("f-res", project_id="res", statement="Study uses MiniLM"),
        ],
    )

    context = assemble_context(inputs, config=CONFIG)
    evidence = next(block for block in context.blocks if block.kind == "evidence_chunks")

    assert "[business track]" in evidence.text
    assert "[research track]" in evidence.text
    assert {"track:business", "track:research"} <= set(evidence.flags)
    assert track_mix_warning(inputs) == TRACK_MIX_WARNING


def test_a_single_track_answer_is_not_flagged_as_mixed() -> None:
    assert track_mix_warning(_inputs()) is None


def test_a_project_with_no_registry_track_is_labelled_unknown_not_guessed() -> None:
    inputs = _inputs(hits=[_hit("h-x", project_id="not-in-registry", rank=1)])

    context = assemble_context(inputs, config=CONFIG)
    evidence = next(block for block in context.blocks if block.kind == "evidence_chunks")

    assert "track:unknown" in evidence.flags
    # One known track + one unknown is not a "mix" - there is nothing to confuse it with.
    assert track_mix_warning(inputs) is None


# ------------------------------------------------------------------------- inline caveat flags


def test_unconfirmed_and_low_trust_hits_are_flagged_next_to_the_claim() -> None:
    unconfirmed = _hit("h-unconf", project_id="biz", rank=1, status="unconfirmed")
    low_trust = _hit("h-clip", project_id="biz", rank=2, boosts={"low_trust": -0.15})
    inputs = _inputs(hits=[unconfirmed, low_trust])

    context = assemble_context(inputs, config=CONFIG)
    evidence = next(block for block in context.blocks if block.kind == "evidence_chunks")

    assert "unconfirmed" in evidence.text
    assert "low-trust" in evidence.text
    assert {"unconfirmed", "low-trust"} <= set(evidence.flags)


def test_a_source_deleted_hit_is_flagged_from_the_gateway_supplied_map() -> None:
    hit = _hit("h-del", project_id="biz", rank=1)
    inputs = _inputs(hits=[hit], hit_flags={hit.object_id: ["source-deleted"]})

    context = assemble_context(inputs, config=CONFIG)
    evidence = next(block for block in context.blocks if block.kind == "evidence_chunks")

    assert "source-deleted" in evidence.text
    assert "source-deleted" in evidence.flags


def test_an_unconfirmed_fact_is_marked_in_the_current_facts_block() -> None:
    inputs = _inputs(
        facts=[_fact("f-u", project_id="biz", statement="Maybe true", status="unconfirmed")]
    )

    context = assemble_context(inputs, config=CONFIG)
    facts_block = next(block for block in context.blocks if block.kind == "current_facts")

    assert "[unconfirmed]" in facts_block.text
    assert "unconfirmed" in facts_block.flags


def test_a_superseding_decision_states_what_it_replaces() -> None:
    inputs = _inputs(
        decisions=[_decision("d2", project_id="biz", title="Use RRF v2", supersedes="Use RRF")]
    )

    context = assemble_context(inputs, config=CONFIG)
    decisions = next(block for block in context.blocks if block.kind == "decisions")

    assert "supersedes: Use RRF" in decisions.text


def test_the_project_block_carries_the_honest_coverage_note() -> None:
    context = assemble_context(_inputs(), config=CONFIG)
    summary = next(block for block in context.blocks if block.kind == "project_summary")

    assert "coverage: 12/12 indexable sources embedded" in summary.text
    assert "track: business" in summary.text
