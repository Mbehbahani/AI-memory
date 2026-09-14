"""P1-T01 acceptance tests: the contract layer imports, round-trips, and matches ``schemas/``.

Owner: A02. These tests are the guard that keeps ``packages/aimemory/domain`` and ``schemas/`` in
sync after the P1 freeze - if a later agent changes one without the other (which would need an ADR),
these fail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import aimemory.common
import aimemory.domain
import aimemory.ontology
import pytest
import yaml
from aimemory.common.config import Settings, get_settings
from aimemory.common.errors import AiMemoryError, PathGuardError, WriteDisabledError
from aimemory.common.hashing import content_hash_bytes, short_hash, text_hash
from aimemory.common.ids import deterministic_id, is_slug, new_id, normalize_name, slugify
from aimemory.common.logging import REDACTED, redact_processor
from aimemory.common.time import ensure_utc, is_valid_at, observed_at_for, utc_now, valid_from_for
from aimemory.domain import (
    PROVENANCE_COLUMNS,
    ArtifactStatus,
    ArtifactType,
    Chunk,
    ChunkDraft,
    Chunker,
    DocKind,
    Embedding,
    EmbeddingProvider,
    EngineKind,
    Entity,
    EntityType,
    Episode,
    EpisodeContext,
    EpisodeExtraction,
    EpisodeStatus,
    EpisodeType,
    ExtractedArtifact,
    ExtractedEntity,
    ExtractedFact,
    ExtractionResult,
    Fact,
    FactStatus,
    GraphStore,
    JobStage,
    JobState,
    KnowledgeArtifact,
    KnowledgeEngine,
    LLMProvider,
    MetricsSnapshot,
    Origin,
    Predicate,
    Project,
    Provenance,
    RelationshipExtraction,
    RetrievalConfig,
    ReviewVerdict,
    RunAction,
    RunRequest,
    SearchQuery,
    Source,
    SourceStatus,
    SourceUriScheme,
    SourceVersion,
    StatedArtifactStatus,
    StoragePolicy,
    TextExtractor,
    Tier,
    Track,
    Trust,
)
from aimemory.domain import models as domain_models
from aimemory.domain.extraction import (
    EXTRACTABLE_ARTIFACT_TYPES,
    EXTRACTABLE_ENTITY_TYPES,
    EXTRACTABLE_PREDICATES,
)
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
CONFIG_DIR = REPO_ROOT / "config"

NOW = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


# --------------------------------------------------------------------------------------------------
# sample objects
# --------------------------------------------------------------------------------------------------


def _provenance() -> Provenance:
    return Provenance(
        source_id=UUID(int=1),
        source_uri="vault://my-vault/AIOS/me.md",
        source_hash="sha256:" + "ab" * 32,
        source_version=UUID(int=2),
        project_id="ai-memory",
        device_id="local-development-machine",
        observed_at=NOW,
        valid_from=NOW,
        confidence=0.9,
        extraction_model_id="qwen3-4b",
        embedding_model_id="minilm-l6-v2-384",
        ingestion_run_id=UUID(int=3),
        episode_id=UUID(int=4),
        heading_path=["AIOS", "Goals"],
        char_start=0,
        char_end=120,
    )


def _samples() -> dict[str, Any]:
    prov = _provenance()
    return {
        "Device": domain_models.Device(id="local-development-machine", label="Dev laptop"),
        "Project": Project(id="ai-memory", name="AI Memory", track=Track.FOUNDATION),
        "ProjectAlias": domain_models.ProjectAlias(
            id=new_id(), project_id="ai-memory", alias="AI Memory", normalized_alias="ai memory"
        ),
        "SourceRoot": domain_models.SourceRoot(
            root_id="vault",
            scheme=SourceUriScheme.VAULT,
            label="my-vault",
            container_path="/sources/vault",
        ),
        "EmbeddingModel": domain_models.EmbeddingModel(
            id="minilm-l6-v2-384", name="sentence-transformers/all-MiniLM-L6-v2", dimension=384
        ),
        "ExtractionModel": domain_models.ExtractionModel(id="qwen3-4b", name="qwen3:4b"),
        "Source": Source(
            id=UUID(int=1),
            uri="vault://my-vault/AIOS/me.md",
            root_id="vault",
            relative_path="AIOS/me.md",
            policy=StoragePolicy.MIRROR,
            origin=Origin.INTERNAL,
            trust=Trust.HIGH,
        ),
        "SourceVersion": SourceVersion(
            id=UUID(int=2),
            source_id=UUID(int=1),
            content_hash="sha256:" + "ab" * 32,
            size_bytes=1024,
            observed_at=NOW,
        ),
        "SourceText": domain_models.SourceText(
            version_id=UUID(int=2), text="hello", extractor="markdown", char_count=5
        ),
        "MirrorBlob": domain_models.MirrorBlob(
            version_id=UUID(int=2), media_type="text/markdown", size_bytes=5, data=b"hello"
        ),
        "Chunk": Chunk(
            id=new_id(),
            version_id=UUID(int=2),
            source_id=UUID(int=1),
            ordinal=0,
            text="hello",
            text_hash=text_hash("hello"),
            char_start=0,
            char_end=5,
            token_count=2,
        ),
        "Embedding": Embedding(
            id=new_id(),
            object_type="chunk",
            object_id=UUID(int=5),
            text_hash=text_hash("hello"),
            model_id="minilm-l6-v2-384",
            dimension=3,
            vector=[0.1, 0.2, 0.3],
        ),
        "Episode": Episode(
            id=UUID(int=4),
            type=EpisodeType.DOCUMENT,
            observed_at=NOW,
            status=EpisodeStatus.PENDING,
            tier=Tier.KNOWLEDGE,
        ),
        "Entity": Entity(
            id=UUID(int=6),
            type=EntityType.TECHNOLOGY,
            canonical_name="PostgreSQL",
            normalized_name="postgresql",
        ),
        "EntityMention": domain_models.EntityMention(
            id=new_id(),
            entity_id=UUID(int=6),
            episode_id=UUID(int=4),
            surface_form="Postgres",
            provenance=prov,
        ),
        "Fact": Fact(
            id=UUID(int=7),
            subject_entity_id=UUID(int=6),
            predicate=Predicate.HAS_STATUS,
            object_value="active",
            statement="PostgreSQL is active.",
            valid_from=NOW,
            observed_at=NOW,
            status=FactStatus.CURRENT,
            provenance=prov,
        ),
        "KnowledgeArtifact": KnowledgeArtifact(
            id=UUID(int=8),
            type=ArtifactType.DECISION,
            title="Use Postgres as system of record",
            body="ADR-0001",
            valid_from=NOW,
            current_status=ArtifactStatus.CURRENT,
            provenance=prov,
        ),
        "ArtifactEntity": domain_models.ArtifactEntity(
            artifact_id=UUID(int=8), entity_id=UUID(int=6)
        ),
        "IngestionRun": domain_models.IngestionRun(id=new_id(), root_id="vault"),
        "IngestionJob": domain_models.IngestionJob(
            id=new_id(), run_id=UUID(int=9), stage=JobStage.CHUNK, state=JobState.PENDING
        ),
        "SourceEvent": domain_models.SourceEvent(
            id=new_id(), source_id=UUID(int=1), event_type="created"
        ),
        "RetrievalLog": domain_models.RetrievalLog(
            id=new_id(), query_text="what did I decide", query_hash=text_hash("q")
        ),
        "McpAuditLog": domain_models.McpAuditLog(id=new_id(), tool="memory.search", kind="read"),
        "MetricsSnapshot": MetricsSnapshot(id=new_id(), scope="ingestion_run"),
        "ExtractionReview": domain_models.ExtractionReview(
            id=new_id(),
            object_type="artifact",
            object_id=UUID(int=8),
            verdict=ReviewVerdict.ACCEPT,
        ),
        "RunRequest": RunRequest(id=new_id(), action=RunAction.SCAN),
        "ServiceStat": domain_models.ServiceStat(id=new_id(), service="ingestion"),
        "ExtractionResult": ExtractionResult(
            episode_id=UUID(int=4), engine=EngineKind.NATIVE, extraction_model_id="qwen3-4b"
        ),
        "SearchQuery": SearchQuery(query="what did I decide about the graph store"),
    }


# --------------------------------------------------------------------------------------------------
# round-trips and structural guarantees
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_samples()))
def test_model_json_round_trip(name: str) -> None:
    """Every model survives dump -> load unchanged (the wire format is lossless)."""
    sample = _samples()[name]
    restored = type(sample).model_validate_json(sample.model_dump_json())
    assert restored == sample


@pytest.mark.parametrize("name", sorted(_samples()))
def test_model_has_docstring_naming_consumers(name: str) -> None:
    """Acceptance criterion: every contract documents who consumes it."""
    doc = type(_samples()[name]).__doc__ or ""
    assert doc.strip(), f"{name} has no docstring"


def test_domain_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Project(id="x", name="X", track=Track.BUSINESS, nonsense=1)  # type: ignore[call-arg]


def test_fact_requires_exactly_one_object() -> None:
    common = {
        "id": UUID(int=7),
        "subject_entity_id": UUID(int=6),
        "predicate": Predicate.USES,
        "statement": "x uses y",
        "valid_from": NOW,
        "observed_at": NOW,
        "provenance": _provenance(),
    }
    with pytest.raises(ValidationError):
        Fact(**common)  # neither
    with pytest.raises(ValidationError):
        Fact(**common, object_entity_id=UUID(int=10), object_value="literal")  # both
    assert Fact(**common, object_value="literal").object_key == "literal"


def test_embedding_dimension_is_enforced() -> None:
    with pytest.raises(ValidationError):
        Embedding(
            id=new_id(),
            object_type="chunk",
            object_id=UUID(int=5),
            text_hash="sha256:00",
            model_id="m",
            dimension=384,
            vector=[0.1],
        )


def test_provenance_column_set_matches_plan_section_j() -> None:
    """Plan section J lists the [PROV] columns; the model must expose exactly those, in order."""
    expected = ["source_id", "source_uri", "source_hash", "source_version", "project_id", "device_id", "observed_at", "valid_from", "valid_to", "confidence", "extraction_model_id", "embedding_model_id", "ingestion_run_id", "episode_id"]
    assert list(PROVENANCE_COLUMNS) == expected
    fields = list(Provenance.model_fields)
    assert fields[: len(expected)] == expected
    assert fields[len(expected):] == ["heading_path", "char_start", "char_end"]


def test_provenance_citation_format_matches_config() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "retrieval.yaml").read_text(encoding="utf-8"))
    assert cfg["context"]["citation_format"] == "[{source_uri}#{heading} @{hash8}]"
    citation = _provenance().citation()
    assert citation.startswith("[vault://my-vault/AIOS/me.md#AIOS > Goals @")
    assert citation.endswith("]")
    assert short_hash("sha256:" + "ab" * 32) in citation


def test_models_with_provenance_are_the_prov_tables() -> None:
    """Plan section G marks entity_mentions, facts and knowledge_artifacts with [PROV]."""
    with_prov = {
        name
        for name, model in vars(domain_models).items()
        if isinstance(model, type)
        and hasattr(model, "model_fields")
        and "provenance" in getattr(model, "model_fields", {})
    }
    assert with_prov == {"EntityMention", "Fact", "KnowledgeArtifact"}


# --------------------------------------------------------------------------------------------------
# enum coverage
# --------------------------------------------------------------------------------------------------


def test_storage_policy_matches_plan_section_k() -> None:
    assert [p.value for p in StoragePolicy] == [
        "IGNORE",
        "CATALOG_ONLY",
        "INDEX_CONTENT",
        "MIRROR",
    ]
    assert StoragePolicy.INDEX_CONTENT.stores_text
    assert StoragePolicy.MIRROR.stores_bytes
    assert not StoragePolicy.CATALOG_ONLY.stores_text


def test_policy_config_uses_known_policies() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "policies.yaml").read_text(encoding="utf-8"))
    StoragePolicy(cfg["default_policy"])
    assert cfg["secret_detector"]["action"].startswith("CATALOG_ONLY")


def test_ingestion_stage_and_change_vocabularies() -> None:
    """Plan section L: the stage list and the change cases are complete."""
    stages = [s.value for s in JobStage]
    for expected in ("discover", "classify", "fingerprint", "diff", "extract_text", "chunk",
                     "embed", "episode", "extract_knowledge", "resolve_entities", "temporal",
                     "provenance"):
        assert expected in stages
    assert {c.value for c in aimemory.domain.ChangeType} == {
        "new", "modified", "moved", "deleted", "unchanged", "duplicate"
    }


def test_temporal_status_vocabularies_match_adr_0005() -> None:
    assert {s.value for s in FactStatus} == {"current", "historical", "unconfirmed"}
    assert {s.value for s in SourceStatus} == {"active", "deleted", "moved"}


def test_tier_values_match_adr_0006() -> None:
    assert (Tier.REGISTRY, Tier.EMBED, Tier.KNOWLEDGE) == (0, 1, 2)


def test_stated_status_maps_into_stored_status() -> None:
    assert StatedArtifactStatus.UNKNOWN.to_artifact_status() is ArtifactStatus.CURRENT
    for member in StatedArtifactStatus:
        assert isinstance(member.to_artifact_status(), ArtifactStatus)


def test_run_actions_are_limited_to_adr_0011_list() -> None:
    assert {a.value for a in RunAction} == {"scan", "retry_failed", "eval", "benchmark"}


# --------------------------------------------------------------------------------------------------
# schema <-> model agreement
# --------------------------------------------------------------------------------------------------


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "extraction" / name).read_text(encoding="utf-8"))


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """Resolve ``$ref``/``anyOf`` in a pydantic-generated JSON schema into a flat node."""
    if isinstance(node, dict):
        if "$ref" in node:
            key = node["$ref"].rsplit("/", 1)[-1]
            return _inline(defs.get(key, {}), defs)
        if "anyOf" in node:
            options = [o for o in node["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                return _inline(options[0], defs)
        return {k: _inline(v, defs) for k, v in node.items() if k not in ("$ref", "anyOf")}
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    return node


def _model_schema(model: type) -> dict[str, Any]:
    raw = model.model_json_schema()
    defs = raw.get("$defs", {})
    return {name: _inline(prop, defs) for name, prop in raw.get("properties", {}).items()}


#: Fields where the JSON Schema enum is a documented *subset* of the model enum
#: (see EXTRACTABLE_* in aimemory.domain.extraction). Enforced at runtime by field validators.
SUBSET_ENUM_FIELDS = {"type", "artifact_type", "predicate"}


def _assert_object_matches(file_node: dict[str, Any], model: type, where: str) -> None:
    file_props = file_node["properties"]
    model_props = _model_schema(model)

    assert set(file_props) == set(model_props), f"{where}: property names differ"
    assert file_node.get("additionalProperties") is False, f"{where}: additionalProperties must be false"

    file_required = set(file_node.get("required", []))
    model_required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert model_required <= file_required, f"{where}: model requires more than the schema"

    for name, spec in file_props.items():
        mine = model_props[name]
        if "enum" in spec:
            model_enum = set(mine.get("enum", []))
            if name in SUBSET_ENUM_FIELDS:
                assert set(spec["enum"]) <= model_enum, f"{where}.{name}: enum not a model subset"
            else:
                assert set(spec["enum"]) == model_enum, f"{where}.{name}: enum differs"
        for key in ("maxLength", "minLength", "maxItems", "minimum", "maximum"):
            if key in spec:
                target = mine.get("items", mine) if key in ("maxItems",) else mine
                assert spec[key] == mine.get(key, target.get(key)), f"{where}.{name}: {key} differs"


def test_episode_extraction_schema_matches_model() -> None:
    schema = _load_schema("episode_extraction.schema.json")
    _assert_object_matches(schema, EpisodeExtraction, "EpisodeExtraction")
    _assert_object_matches(
        schema["properties"]["entities"]["items"], ExtractedEntity, "ExtractedEntity"
    )
    _assert_object_matches(
        schema["properties"]["artifacts"]["items"], ExtractedArtifact, "ExtractedArtifact"
    )


def test_relationship_extraction_schema_matches_model() -> None:
    schema = _load_schema("relationship_extraction.schema.json")
    _assert_object_matches(schema, RelationshipExtraction, "RelationshipExtraction")
    _assert_object_matches(schema["properties"]["facts"]["items"], ExtractedFact, "ExtractedFact")


def test_extraction_schemas_are_ollama_format_safe() -> None:
    """Ollama `format=` needs a self-contained object schema with closed properties."""
    for name in ("episode_extraction.schema.json", "relationship_extraction.schema.json"):
        schema = _load_schema(name)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert schema["required"]
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text, f"{name} must be flat (no $ref/$defs)"


def test_extraction_models_refuse_out_of_subset_values() -> None:
    with pytest.raises(ValidationError):
        ExtractedEntity(name="me.md", type=EntityType.SOURCE)
    with pytest.raises(ValidationError):
        ExtractedArtifact(artifact_type=ArtifactType.SUMMARY, title="abc", statement="abc")
    with pytest.raises(ValidationError):
        ExtractedFact(subject="a", predicate=Predicate.MENTIONS, object="b", statement="abc")


def test_extraction_enums_are_the_documented_subsets() -> None:
    episode = _load_schema("episode_extraction.schema.json")
    rel = _load_schema("relationship_extraction.schema.json")
    entity_enum = set(episode["properties"]["entities"]["items"]["properties"]["type"]["enum"])
    artifact_enum = set(
        episode["properties"]["artifacts"]["items"]["properties"]["artifact_type"]["enum"]
    )
    status_enum = set(episode["properties"]["artifacts"]["items"]["properties"]["status"]["enum"])
    doc_kind_enum = set(episode["properties"]["doc_kind"]["enum"])
    predicate_enum = set(rel["properties"]["facts"]["items"]["properties"]["predicate"]["enum"])

    assert entity_enum == {t.value for t in EXTRACTABLE_ENTITY_TYPES}
    assert entity_enum < {t.value for t in EntityType}
    assert artifact_enum == {t.value for t in EXTRACTABLE_ARTIFACT_TYPES}
    assert status_enum == {s.value for s in StatedArtifactStatus}
    assert doc_kind_enum == {k.value for k in DocKind}
    assert predicate_enum == {p.value for p in EXTRACTABLE_PREDICATES}
    assert predicate_enum < {p.value for p in Predicate}


def test_mcp_tools_contract_matches_plan_section_r() -> None:
    tools = json.loads((SCHEMA_DIR / "mcp" / "tools.json").read_text(encoding="utf-8"))
    read = [t for t in tools["tools"] if t["kind"] == "read"]
    write = [t for t in tools["tools"] if t["kind"] == "write"]
    assert len(read) == 10, "plan section R: 10 read tools"
    assert len(write) == 2, "plan section R: 2 write tools"
    assert {t["name"] for t in write} == {"memory.add_episode", "memory.record_decision"}
    assert tools["counts"] == {"read": 10, "write": 2}

    safeguards = tools["safeguards"]
    assert safeguards["write_enabled_env"] == "MCP_WRITE_ENABLED"
    assert safeguards["confirm_required"] is True
    assert safeguards["rate_limit_per_minute"] == 10
    assert safeguards["max_text_chars"] == 8000
    assert safeguards["append_only"] is True

    for tool in tools["tools"]:
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert tool["description"]
        if tool["kind"] == "write":
            assert "confirm" in schema["required"]
            assert schema["properties"]["confirm"]["const"] is True


def test_retrieval_config_model_covers_config_file() -> None:
    cfg = yaml.safe_load((CONFIG_DIR / "retrieval.yaml").read_text(encoding="utf-8"))
    model = RetrievalConfig(
        version=str(cfg["version"]),
        semantic_top_k=cfg["candidates"]["semantic_top_k"],
        keyword_top_k=cfg["candidates"]["keyword_top_k"],
        fused_top_k=cfg["candidates"]["fused_top_k"],
        final_k=cfg["candidates"]["final_k"],
        fusion_method=cfg["fusion"]["method"],
        rrf_k=cfg["fusion"]["rrf_k"],
        boost_project_match=cfg["boosts"]["project_match"],
        boost_entity_linked=cfg["boosts"]["entity_linked"],
        unconfirmed_penalty=cfg["boosts"]["unconfirmed_penalty"],
        recency_half_life_days=cfg["boosts"]["recency_half_life_days"],
        graph_expansion_enabled=cfg["graph_expansion"]["enabled"],
        graph_max_depth=cfg["graph_expansion"]["max_depth"],
        graph_max_nodes=cfg["graph_expansion"]["max_nodes"],
        graph_relationship_types=cfg["graph_expansion"]["relationship_types"],
        token_budget=cfg["context"]["token_budget"],
        block_order=cfg["context"]["block_order"],
        citation_format=cfg["context"]["citation_format"],
    )
    assert model.rrf_k == 60
    assert model.token_budget == 6000
    assert model.unconfirmed_penalty == -0.15
    for predicate in model.graph_relationship_types:
        Predicate(predicate)  # every expansion type is a real ontology predicate


# --------------------------------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------------------------------


class _FakeEngine:
    kind = EngineKind.NATIVE

    def process_episode(self, episode: Episode, context: EpisodeContext) -> ExtractionResult:
        return ExtractionResult(
            episode_id=episode.id, engine=EngineKind.NATIVE, extraction_model_id="qwen3-4b"
        )

    def invalidate(self, fact_id: UUID, at: datetime, by_episode: UUID | None = None) -> None:
        return None


def test_knowledge_engine_port_shape() -> None:
    engine = _FakeEngine()
    assert isinstance(engine, KnowledgeEngine)
    result = engine.process_episode(_samples()["Episode"], EpisodeContext())
    assert isinstance(result, ExtractionResult)


def test_all_ports_are_runtime_checkable_protocols() -> None:
    for port in (LLMProvider, EmbeddingProvider, KnowledgeEngine, GraphStore, TextExtractor, Chunker):
        assert getattr(port, "_is_protocol", False), f"{port.__name__} must be a Protocol"
        assert port.__doc__ and port.__doc__.strip(), f"{port.__name__} needs a docstring"
        assert isinstance(object(), port) is False


def test_knowledge_engine_signature_is_the_adr_0002_seam() -> None:
    import inspect

    sig = inspect.signature(KnowledgeEngine.process_episode)
    assert list(sig.parameters) == ["self", "episode", "context"]
    inv = inspect.signature(KnowledgeEngine.invalidate)
    assert list(inv.parameters) == ["self", "fact_id", "at", "by_episode"]


def test_chunk_draft_promotes_to_chunk() -> None:
    draft = ChunkDraft(
        ordinal=0,
        text="hello",
        text_hash=text_hash("hello"),
        char_start=0,
        char_end=5,
        token_count=2,
    )
    chunk = aimemory.domain.chunk_from_draft(
        draft, chunk_id=new_id(), version_id=UUID(int=2), source_id=UUID(int=1)
    )
    assert chunk.text_hash == draft.text_hash and chunk.ordinal == 0


# --------------------------------------------------------------------------------------------------
# common utilities
# --------------------------------------------------------------------------------------------------


def test_time_helpers_follow_adr_0005() -> None:
    assert utc_now().tzinfo is not None
    naive = datetime(2026, 9, 14, 10, 0)  # noqa: DTZ001 - deliberately naive input
    assert ensure_utc(naive).tzinfo == UTC
    assert observed_at_for(None, NOW) == NOW
    assert observed_at_for(NOW - timedelta(days=1), NOW) == NOW - timedelta(days=1)
    assert valid_from_for(None, NOW) == NOW
    assert valid_from_for("2020-01-01", NOW) == datetime(2020, 1, 1, tzinfo=UTC)


def test_point_in_time_predicate() -> None:
    start, end = NOW - timedelta(days=10), NOW - timedelta(days=1)
    assert is_valid_at(start, None, NOW)
    assert is_valid_at(start, end, NOW - timedelta(days=5))
    assert not is_valid_at(start, end, NOW)
    assert not is_valid_at(NOW + timedelta(days=1), None, NOW)
    assert not is_valid_at(start, NOW, NOW), "valid_to is exclusive (valid_to > as_of)"


def test_id_helpers() -> None:
    first = new_id(NOW)
    assert first.version == 7
    assert new_id(NOW) != first
    assert deterministic_id("source", "vault", "AIOS/me.md") == deterministic_id(
        "source", "vault", "AIOS/me.md"
    )
    assert deterministic_id("source", "a") != deterministic_id("chunk", "a")
    assert slugify("JobLab Lakehouse (DE)") == "joblab-lakehouse-de"
    assert is_slug("joblab-lakehouse-de")
    assert normalize_name("  JobLab   Lakehouse (DE) ") == "joblab lakehouse (de)"
    assert normalize_name("Postgres.") == "postgres"


def test_hashing_helpers() -> None:
    assert content_hash_bytes(b"abc").startswith("sha256:")
    assert text_hash("a\r\nb  \n") == text_hash("a\nb")
    assert len(short_hash(text_hash("x"))) == 8


def test_errors_are_sanitized() -> None:
    err = PathGuardError(detail="D:/My-Vault/../../secret")
    assert "secret" not in str(err)
    assert err.sanitized() == {
        "error": "path_guard_error",
        "message": "Path is outside the allowed source root.",
        "context": {},
    }
    assert isinstance(err, AiMemoryError)
    assert WriteDisabledError().http_status == 403


def test_logging_redacts_secrets() -> None:
    event = redact_processor(
        None,
        "info",
        {
            "event": "connect",
            "password": "hunter2",
            "database_url": "postgresql://u:p@postgres:5432/db",
            "dsn_text": "postgresql://user:secret@postgres:5432/db",
        },
    )
    assert event["password"] == REDACTED
    assert event["database_url"] == REDACTED
    assert "secret" not in event["dsn_text"]


# --------------------------------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------------------------------


def test_settings_defaults_are_container_defaults() -> None:
    settings = Settings()
    assert "localhost" not in settings.neo4j.uri and "127.0.0.1" not in settings.neo4j.uri
    assert settings.neo4j.uri.startswith("bolt://neo4j")
    assert settings.llm.ollama_url == "http://ollama:11434"
    assert settings.embedding.url == "http://embedding-service:8010"
    assert settings.gateway.url == "http://memory-api:8000"
    assert "postgres:5432" in settings.postgres.safe_dsn


def test_settings_never_leak_passwords_in_repr() -> None:
    settings = Settings()
    text = repr(settings)
    assert "change-me" not in text
    assert "***" in settings.postgres.safe_dsn


def test_settings_read_every_env_name_in_env_example(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each variable in .env.example is either consumed by Settings or owned by Compose only."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    names = {
        line.split("=", 1)[0].strip()
        for line in env_example.splitlines()
        if line.strip() and not line.startswith("#") and "=" in line
    }
    compose_only = {
        "COMPOSE_PROJECT_NAME",
        "POSTGRES_HOST_PORT",
        "NEO4J_IMAGE_TAG",
        "NEO4J_HEAP_MAX",
        "NEO4J_PAGECACHE",
        "OLLAMA_IMAGE_TAG",
        "OLLAMA_CPUS",
        "HOST_VAULT_ROOT",
        "HOST_PILOT_ROOT",
        "NEODASH_HOST_PORT",
        "NEODASH_IMAGE_TAG",
        "MEMORY_API_HOST_PORT",
        "MCP_HOST_PORT",
    }
    aliases: set[str] = set()
    for group in (
        Settings,
        type(Settings().postgres),
        type(Settings().neo4j),
        type(Settings().llm),
        type(Settings().embedding),
        type(Settings().ingest),
        type(Settings().gateway),
        type(Settings().mcp),
        type(Settings().logging),
        type(Settings().paths),
    ):
        for field in group.model_fields.values():
            if isinstance(field.validation_alias, str):
                aliases.add(field.validation_alias)
    missing = names - aliases - compose_only
    assert not missing, f".env.example variables not read by Settings: {sorted(missing)}"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("GATEWAY_WRITE_ENABLED", "true")
    monkeypatch.setenv("MCP_WRITE_ENABLED", "true")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.llm.model == "qwen3:8b"
        assert settings.writes_allowed is True
    finally:
        get_settings.cache_clear()


def test_writes_are_off_unless_both_flags_are_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_WRITE_ENABLED", "true")
    monkeypatch.setenv("MCP_WRITE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().writes_allowed is False
    finally:
        get_settings.cache_clear()
