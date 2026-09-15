"""P9-T01 (A09): ``config/retrieval.yaml`` -> :class:`RetrievalConfig`.

``config/retrieval.yaml`` is the only tuning surface of the pipeline, so a renamed or dropped key
must be a loud startup failure, never a silently-defaulted weight. These tests pin that, plus the
one documented optional key (``boosts.low_trust_penalty``, ``retrieval.md`` deviation 2).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from aimemory.common.errors import ConfigurationError
from aimemory.retrieval.config import (
    load_retrieval_config,
    retrieval_config_from_mapping,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG = REPO_ROOT / "config" / "retrieval.yaml"


def _raw() -> dict:
    return yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))


def test_loads_the_real_config_file_with_the_documented_v01_values() -> None:
    config = load_retrieval_config(REAL_CONFIG)

    assert (config.semantic_top_k, config.keyword_top_k) == (40, 40)
    assert (config.fused_top_k, config.final_k) == (15, 10)
    assert config.fusion_method == "rrf"
    assert config.rrf_k == 60
    assert config.boost_project_match == pytest.approx(0.10)
    assert config.boost_entity_linked == pytest.approx(0.10)
    assert config.unconfirmed_penalty == pytest.approx(-0.15)
    assert config.recency_half_life_days == pytest.approx(180.0)
    assert config.token_budget == 6000


def test_low_trust_penalty_defaults_to_the_unconfirmed_magnitude_when_the_key_is_absent() -> None:
    raw = _raw()
    assert "low_trust_penalty" not in raw["boosts"], "config now sets it explicitly; update this test"

    config = retrieval_config_from_mapping(raw)

    assert config.low_trust_penalty == config.unconfirmed_penalty == pytest.approx(-0.15)


def test_explicit_low_trust_penalty_is_honoured() -> None:
    raw = _raw()
    raw["boosts"]["low_trust_penalty"] = -0.25

    assert retrieval_config_from_mapping(raw).low_trust_penalty == pytest.approx(-0.25)


@pytest.mark.parametrize(
    ("section", "key"),
    [
        ("candidates", "semantic_top_k"),
        ("candidates", "final_k"),
        ("fusion", "rrf_k"),
        ("boosts", "unconfirmed_penalty"),
        ("boosts", "recency_half_life_days"),
        ("graph_expansion", "max_nodes"),
        ("context", "token_budget"),
        ("context", "block_order"),
    ],
)
def test_a_missing_key_is_a_configuration_error_not_a_default(section: str, key: str) -> None:
    raw = _raw()
    del raw[section][key]

    with pytest.raises(ConfigurationError) as excinfo:
        retrieval_config_from_mapping(raw)

    assert f"{section}.{key}" in (excinfo.value.detail or "")
    assert "retrieval.yaml" in str(excinfo.value)


def test_a_missing_section_is_a_configuration_error() -> None:
    raw = _raw()
    del raw["fusion"]

    with pytest.raises(ConfigurationError):
        retrieval_config_from_mapping(raw)


def test_an_out_of_range_value_fails_validation_instead_of_being_clamped() -> None:
    raw = _raw()
    raw["candidates"]["final_k"] = 0  # RetrievalConfig declares ge=1

    with pytest.raises(ConfigurationError):
        retrieval_config_from_mapping(raw)


def test_a_missing_file_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_retrieval_config(tmp_path / "does-not-exist.yaml")


def test_the_cache_is_invalidated_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "retrieval.yaml"
    raw = _raw()
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert load_retrieval_config(path).rrf_k == 60

    raw["fusion"]["rrf_k"] = 30
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    # The cache key is (path, st_mtime_ns). Bump the mtime explicitly rather than relying on the
    # filesystem timestamp granularity being finer than the two writes above.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert load_retrieval_config(path).rrf_k == 30
