"""Loader for ``config/retrieval.yaml`` -> :class:`~aimemory.domain.retrieval.RetrievalConfig`.

``config/retrieval.yaml`` is the only tuning surface of the retrieval pipeline
(``docs/architecture/retrieval.md`` §0). This module is the only place that reads its raw nested
keys; everything downstream takes the typed, flat :class:`RetrievalConfig` view that A02 froze in
``packages/aimemory/domain/retrieval.py``, so a renamed YAML key becomes a loud
:class:`~aimemory.common.errors.ConfigurationError` instead of a silently-defaulted weight.

The file is cached per ``(path, mtime_ns)`` - the same pattern
:mod:`aimemory.sources.policies` uses - so a test that writes a temporary config file is picked up
without a manual cache clear, and the steady-state Gateway never re-reads the file per query.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..common.config import get_settings
from ..common.errors import ConfigurationError
from ..domain.retrieval import RetrievalConfig

__all__ = ["default_retrieval_config_path", "load_retrieval_config", "retrieval_config_from_mapping"]


def default_retrieval_config_path() -> Path:
    """``<MEMORY_CONFIG_DIR>/retrieval.yaml`` (``PathSettings.retrieval_file``)."""
    return get_settings().paths.retrieval_file


def _require(data: dict[str, Any], section: str, key: str) -> Any:
    try:
        block = data[section]
    except (KeyError, TypeError) as exc:
        raise ConfigurationError(
            "config/retrieval.yaml is missing a required section.", detail=f"missing {section!r}"
        ) from exc
    if not isinstance(block, dict) or key not in block:
        raise ConfigurationError(
            "config/retrieval.yaml is missing a required key.", detail=f"missing {section}.{key}"
        )
    return block[key]


def retrieval_config_from_mapping(data: dict[str, Any]) -> RetrievalConfig:
    """Build the typed view from an already-parsed mapping (the YAML file's shape).

    ``boosts.low_trust_penalty`` is optional: AC-6 only says clippings must rank lower, and
    ``retrieval.md`` §6 fixes its magnitude at the unconfirmed penalty when the key is absent.
    """
    boosts = data.get("boosts") or {}
    unconfirmed = float(_require(data, "boosts", "unconfirmed_penalty"))
    low_trust = boosts.get("low_trust_penalty", unconfirmed)
    try:
        return RetrievalConfig(
            version=str(data.get("version", "0.1.0")),
            semantic_top_k=int(_require(data, "candidates", "semantic_top_k")),
            keyword_top_k=int(_require(data, "candidates", "keyword_top_k")),
            fused_top_k=int(_require(data, "candidates", "fused_top_k")),
            final_k=int(_require(data, "candidates", "final_k")),
            fusion_method=str(_require(data, "fusion", "method")),
            rrf_k=int(_require(data, "fusion", "rrf_k")),
            boost_project_match=float(_require(data, "boosts", "project_match")),
            boost_entity_linked=float(_require(data, "boosts", "entity_linked")),
            unconfirmed_penalty=unconfirmed,
            low_trust_penalty=float(low_trust),
            recency_half_life_days=float(_require(data, "boosts", "recency_half_life_days")),
            graph_expansion_enabled=bool(_require(data, "graph_expansion", "enabled")),
            graph_max_depth=int(_require(data, "graph_expansion", "max_depth")),
            graph_max_nodes=int(_require(data, "graph_expansion", "max_nodes")),
            graph_relationship_types=list(_require(data, "graph_expansion", "relationship_types")),
            token_budget=int(_require(data, "context", "token_budget")),
            block_order=list(_require(data, "context", "block_order")),
            citation_format=str(_require(data, "context", "citation_format")),
        )
    except ConfigurationError:
        raise
    except Exception as exc:  # pydantic validation / type coercion
        raise ConfigurationError(
            "config/retrieval.yaml failed validation.", detail=str(exc)
        ) from exc


@lru_cache(maxsize=8)
def _load_cached(path_str: str, _mtime_ns: int) -> RetrievalConfig:
    with open(path_str, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigurationError(
            "config/retrieval.yaml must be a mapping.", detail=f"got {type(data).__name__}"
        )
    return retrieval_config_from_mapping(data)


def load_retrieval_config(path: Path | None = None) -> RetrievalConfig:
    """Load (and cache) the retrieval configuration. Re-reads when the file's mtime changes."""
    resolved = Path(path) if path is not None else default_retrieval_config_path()
    try:
        mtime_ns = resolved.stat().st_mtime_ns
    except OSError as exc:
        raise ConfigurationError(
            "config/retrieval.yaml could not be read.", detail=f"{type(exc).__name__} at {resolved}"
        ) from exc
    return _load_cached(str(resolved), mtime_ns)
