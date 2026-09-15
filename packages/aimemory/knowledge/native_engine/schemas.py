"""The frozen extraction schemas, loaded **verbatim** (A08, P8-T02).

``schemas/extraction/episode_extraction.schema.json`` and
``schemas/extraction/relationship_extraction.schema.json`` are A02's contract, frozen after P1. They
are read from disk and handed to the provider unchanged: Ollama takes them as ``format=`` for
grammar-constrained decoding, Bedrock as a forced tool-use input schema. Nothing here rewrites,
trims, subsets or "fixes" a schema - if one needs to change, that is an ADR and A02's edit, not a
runtime mutation.

The ``$id`` of each file is asserted against the domain constants on load, so a schema swapped
underneath the engine fails loudly at the first episode instead of producing quietly different
knowledge.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...common.config import Settings, get_settings
from ...common.errors import ContractError
from ...domain.extraction import (
    EPISODE_EXTRACTION_SCHEMA_ID,
    RELATIONSHIP_EXTRACTION_SCHEMA_ID,
)

__all__ = [
    "EPISODE_SCHEMA_FILE",
    "RELATIONSHIP_SCHEMA_FILE",
    "episode_schema",
    "load_schema",
    "relationship_schema",
]

EPISODE_SCHEMA_FILE = "episode_extraction.schema.json"
RELATIONSHIP_SCHEMA_FILE = "relationship_extraction.schema.json"


def load_schema(filename: str, expected_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """Read one frozen schema and verify its ``$id``."""
    directory: Path = (settings or get_settings()).paths.extraction_schema_dir
    path = directory / filename
    if not path.exists():
        raise ContractError(
            "Extraction schema file is missing.",
            detail=f"expected {filename} under {directory}",
        )
    schema = json.loads(path.read_text(encoding="utf-8"))
    actual = schema.get("$id")
    if actual != expected_id:
        raise ContractError(
            "Extraction schema identity does not match the frozen contract.",
            detail=f"{filename}: $id={actual!r} expected {expected_id!r}",
        )
    return schema


@lru_cache(maxsize=1)
def episode_schema() -> dict[str, Any]:
    """Call 1's schema, cached per process (the file never changes at runtime)."""
    return load_schema(EPISODE_SCHEMA_FILE, EPISODE_EXTRACTION_SCHEMA_ID)


@lru_cache(maxsize=1)
def relationship_schema() -> dict[str, Any]:
    """Call 2's schema, cached per process."""
    return load_schema(RELATIONSHIP_SCHEMA_FILE, RELATIONSHIP_EXTRACTION_SCHEMA_ID)
