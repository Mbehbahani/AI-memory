"""Typed settings for every AI Memory process (pydantic-settings v2).

Consumers: A03 (compose passes the variables), A04 (``postgres.dsn``, ``neo4j``), A05 (``llm``),
A06 (``embedding``), A07a/A07b (``ingest``, ``paths``), A09 (``gateway``, ``paths.retrieval_file``),
A10 (``mcp``), A16 (``gateway``), A12 (tests override via environment).

Every variable name here appears in ``.env.example`` and in ``docker-compose.yml``. Rules:

* **Defaults are container defaults.** Hostnames default to Compose service names
  (``postgres``, ``neo4j``, ``ollama``, ``embedding-service``) - never ``localhost``/``127.0.0.1``,
  which would work on the host and silently fail inside a container (ADR-0007 publishes ports on
  loopback only for *human* access, not for service-to-service traffic).
* **Secrets are ``SecretStr``.** ``repr``/logs show a mask; call ``.get_secret_value()`` at the point
  of use. :attr:`PostgresSettings.safe_dsn` is the form that may be logged or returned.
* **Groups are nested but the environment stays flat.** Each group is its own ``BaseSettings`` and
  reads the flat names; no ``__`` delimiter is used, so the ``.env`` file keeps the shape the owner
  edits by hand.
* ``get_settings()`` is cached; tests call ``get_settings.cache_clear()`` after patching the
  environment.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "EmbeddingSettings",
    "GatewaySettings",
    "IngestSettings",
    "LLMSettings",
    "LoggingSettings",
    "McpSettings",
    "Neo4jSettings",
    "PathSettings",
    "PostgresSettings",
    "Settings",
    "default_config_dir",
    "default_schema_dir",
    "get_settings",
]

_CONTAINER_ROOT = Path("/app")


def _resolve_dir(name: str) -> Path:
    """Container path first (``/app/<name>``), then the repository checkout, then a relative path."""
    container = _CONTAINER_ROOT / name
    if container.is_dir():
        return container
    repo = Path(__file__).resolve().parents[3] / name  # packages/aimemory/common/config.py
    if repo.is_dir():
        return repo
    return Path(name)


def default_config_dir() -> Path:
    """Directory holding ``policies.yaml`` / ``retrieval.yaml`` / ``source-roots.yaml``."""
    return _resolve_dir("config")


def default_schema_dir() -> Path:
    """Directory holding ``ontology.yaml``, ``extraction/*.json``, ``mcp/tools.json``."""
    return _resolve_dir("schemas")


_BASE = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


class PostgresSettings(BaseSettings):
    """System of record (ADR-0001). ``DATABASE_URL`` is authoritative; the parts are for tooling."""

    model_config = _BASE

    database_url: SecretStr = Field(
        default=SecretStr("postgresql+psycopg://aimemory:change-me@postgres:5432/aimemory"),
        validation_alias="DATABASE_URL",
    )
    db: str = Field(default="aimemory", validation_alias="POSTGRES_DB")
    user: str = Field(default="aimemory", validation_alias="POSTGRES_USER")
    password: SecretStr = Field(default=SecretStr("change-me"), validation_alias="POSTGRES_PASSWORD")
    host_port: int = Field(default=5432, ge=1, le=65535, validation_alias="POSTGRES_HOST_PORT")

    @property
    def dsn(self) -> str:
        """The real connection string. Never log this; use :attr:`safe_dsn`."""
        return self.database_url.get_secret_value()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def safe_dsn(self) -> str:
        """DSN with the password replaced - safe for logs, ``/health`` and the Ops page."""
        return re.sub(r"://([^:/\s]+):[^@/\s]+@", r"://\1:***@", self.dsn)


class Neo4jSettings(BaseSettings):
    """Rebuildable projection (ADR-0001). memory-api/NeoDash get the read-only user (plan section T)."""

    model_config = _BASE

    uri: str = Field(default="bolt://neo4j:7687", validation_alias="NEO4J_URI")
    user: str = Field(default="neo4j", validation_alias="NEO4J_USER")
    password: SecretStr = Field(default=SecretStr("change-me"), validation_alias="NEO4J_PASSWORD")
    readonly_user: str = Field(default="memory_reader", validation_alias="NEO4J_READONLY_USER")
    readonly_password: SecretStr = Field(
        default=SecretStr("change-me"), validation_alias="NEO4J_READONLY_PASSWORD"
    )
    database: str = Field(default="neo4j", validation_alias="NEO4J_DATABASE")
    heap_max: str = Field(default="512M", validation_alias="NEO4J_HEAP_MAX")
    pagecache: str = Field(default="256M", validation_alias="NEO4J_PAGECACHE")


class LLMSettings(BaseSettings):
    """Extraction model (plan section M; ADR-0012 built both providers, ADR-0014 chose the default).

    ``bedrock`` (Claude Haiku 4.5, US inference profile) is the default. MEASURED in P4-T02 over 20
    identical real episodes: relationship recall 61.2 % against ``qwen3:4b``'s 40.2 %, where plan
    section O criterion C3 requires >= 50 % - so the local model fails the plan's own quality bar and
    Bedrock clears it. Schema validity (95 %) and entity recall (68.9 %) were ties.

    ``ollama`` (``qwen3:4b``, local) is retained and tested as the offline / zero-cost mode, not
    deprecated. **The default sends source text to AWS**; AC-8 ("nothing leaves the machine") holds
    only under ``LLM_PROVIDER=ollama``. See ADR-0014.

    The two models disagree *systematically* on entity types, so a corpus must be extracted by one
    model only - the pipeline enforces this (ADR-0014 rule 2). Never mix without a re-extraction.
    """

    model_config = _BASE

    provider: Literal["ollama", "bedrock"] = Field(
        default="bedrock", validation_alias="LLM_PROVIDER"
    )
    model: str = Field(default="qwen3:4b", validation_alias="LLM_MODEL")
    ollama_url: str = Field(default="http://ollama:11434", validation_alias="OLLAMA_URL")
    num_ctx: int = Field(default=8192, ge=512, validation_alias="LLM_NUM_CTX")
    num_predict: int = Field(default=1024, ge=64, validation_alias="LLM_NUM_PREDICT")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, validation_alias="LLM_TEMPERATURE")
    timeout_seconds: int = Field(default=300, ge=1, validation_alias="LLM_TIMEOUT_SECONDS")
    max_retries: int = Field(default=2, ge=0, le=5, validation_alias="LLM_MAX_RETRIES")
    keep_alive: str = Field(default="5m", validation_alias="OLLAMA_KEEP_ALIVE")

    # --- Bedrock (ADR-0012). Unused when provider == "ollama". -------------------------------
    # Credentials are never held here: boto3's standard chain resolves them (mounted ~/.aws, or
    # AWS_* environment). Nothing AWS-related is ever logged or written to an image.
    bedrock_model_id: str = Field(
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        validation_alias="BEDROCK_MODEL_ID",
        description=(
            "Inference-profile id, not the bare model id. MEASURED 2026-09-14: the bare id "
            "'anthropic.claude-haiku-4-5-20251001-v1:0' returns ValidationException "
            "(on-demand throughput unsupported)."
        ),
    )
    bedrock_region: str = Field(default="us-east-1", validation_alias="BEDROCK_REGION")
    bedrock_profile: str | None = Field(default=None, validation_alias="AWS_PROFILE")
    bedrock_max_tokens: int = Field(
        default=4096, ge=256, validation_alias="BEDROCK_MAX_TOKENS"
    )


class EmbeddingSettings(BaseSettings):
    """MiniLM-L6-v2, 384-d, normalized (plan section N). ``dimensions`` matches the pgvector column."""

    model_config = _BASE

    url: str = Field(default="http://embedding-service:8010", validation_alias="EMBED_URL")
    model_id: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2", validation_alias="EMBED_MODEL_ID"
    )
    dimensions: int = Field(default=384, ge=1, validation_alias="EMBED_DIMENSIONS")
    batch_size: int = Field(default=32, ge=1, validation_alias="EMBED_BATCH_SIZE")
    max_seq: int = Field(default=256, ge=16, validation_alias="EMBED_MAX_SEQ")
    torch_num_threads: int = Field(default=2, ge=1, validation_alias="TORCH_NUM_THREADS")
    timeout_seconds: int = Field(default=120, ge=1, validation_alias="EMBED_TIMEOUT_SECONDS")


class IngestSettings(BaseSettings):
    """Tiered ingestion knobs (ADR-0006). LLM concurrency is 1 by contract, not by preference."""

    model_config = _BASE

    llm_concurrency: int = Field(default=1, ge=1, le=2, validation_alias="INGEST_LLM_CONCURRENCY")
    embed_concurrency: int = Field(default=2, ge=1, validation_alias="INGEST_EMBED_CONCURRENCY")
    max_text_bytes: int = Field(default=2_097_152, ge=1, validation_alias="INGEST_MAX_TEXT_BYTES")
    worker_poll_seconds: float = Field(
        default=5.0, gt=0, validation_alias="INGEST_WORKER_POLL_SECONDS"
    )
    state_dir: Path = Field(default=Path("/state"), validation_alias="INGEST_STATE_DIR")


class GatewaySettings(BaseSettings):
    """memory-api (plan section Q). ``write_enabled`` is half of the ADR-0008 write gate."""

    model_config = _BASE

    url: str = Field(default="http://memory-api:8000", validation_alias="MEMORY_API_URL")
    host_port: int = Field(default=8000, ge=1, le=65535, validation_alias="MEMORY_API_HOST_PORT")
    write_enabled: bool = Field(default=False, validation_alias="GATEWAY_WRITE_ENABLED")
    ops_enabled: bool = Field(default=True, validation_alias="GATEWAY_OPS_ENABLED")


class McpSettings(BaseSettings):
    """mcp-server (ADR-0008). Holds no database credentials - only the Gateway URL."""

    model_config = _BASE

    host_port: int = Field(default=8020, ge=1, le=65535, validation_alias="MCP_HOST_PORT")
    transport: Literal["streamable-http", "stdio"] = Field(
        default="streamable-http", validation_alias="MCP_TRANSPORT"
    )
    write_enabled: bool = Field(default=False, validation_alias="MCP_WRITE_ENABLED")
    rate_limit_per_minute: int = Field(
        default=10, ge=1, validation_alias="MCP_RATE_LIMIT_PER_MINUTE"
    )
    max_text_chars: int = Field(default=8000, ge=1, validation_alias="MCP_MAX_TEXT_CHARS")


class LoggingSettings(BaseSettings):
    """structlog configuration, consumed by :func:`aimemory.common.logging.configure_logging`."""

    model_config = _BASE

    level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    format: Literal["json", "console"] = Field(default="json", validation_alias="LOG_FORMAT")

    @field_validator("level")
    @classmethod
    def _upper(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return upper


class PathSettings(BaseSettings):
    """Config/contract file locations. Host paths never appear here (ADR-0004)."""

    model_config = _BASE

    config_dir: Path = Field(
        default_factory=default_config_dir, validation_alias="MEMORY_CONFIG_DIR"
    )
    schema_dir: Path = Field(
        default_factory=default_schema_dir, validation_alias="MEMORY_SCHEMA_DIR"
    )
    source_roots_file: Path | None = Field(default=None, validation_alias="MEMORY_SOURCE_ROOTS_FILE")
    memoryignore_file: Path | None = Field(default=None, validation_alias="MEMORY_IGNORE_FILE")

    @property
    def policies_file(self) -> Path:
        return self.config_dir / "policies.yaml"

    @property
    def retrieval_file(self) -> Path:
        return self.config_dir / "retrieval.yaml"

    @property
    def technology_aliases_file(self) -> Path:
        return self.config_dir / "technology-aliases.yaml"

    @property
    def roots_file(self) -> Path:
        return self.source_roots_file or (self.config_dir / "source-roots.yaml")

    @property
    def ontology_file(self) -> Path:
        return self.schema_dir / "ontology.yaml"

    @property
    def extraction_schema_dir(self) -> Path:
        return self.schema_dir / "extraction"

    @property
    def mcp_tools_file(self) -> Path:
        return self.schema_dir / "mcp" / "tools.json"


class Settings(BaseSettings):
    """Root settings object. Build once per process with :func:`get_settings`."""

    model_config = _BASE

    device_id: str = Field(default="local-development-machine", validation_alias="DEVICE_ID")
    project_name: str = Field(default="ai-memory", validation_alias="COMPOSE_PROJECT_NAME")

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    paths: PathSettings = Field(default_factory=PathSettings)

    @property
    def writes_allowed(self) -> bool:
        """ADR-0008: an MCP write needs *both* flags. Confirmation is checked per call."""
        return self.gateway.write_enabled and self.mcp.write_enabled


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process settings. Tests call ``get_settings.cache_clear()`` after editing os.environ."""
    return Settings()
