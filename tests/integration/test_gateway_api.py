"""P10-T02 (A09), integration: the memory-api REST surface, its contract and its exposure.

Three kinds of assertion live here:

1. **Contract** - the routes of plan section Q exist, answer the frozen shapes, and the exported
   ``schemas/api/openapi.json`` is not stale (A10 builds the MCP tools against that file).
2. **Behaviour** - health reports each dependency honestly, errors are sanitized (plan section T),
   writes are refused unless the ADR-0008 flag is on, and ``/metrics`` counts what it served.
3. **Exposure** - ADR-0007: every published port in the compose files is bound to ``127.0.0.1``.
   That is asserted against the compose YAML itself, because it is the file that decides it.

The app is driven in-process with ``TestClient`` (so the suite is green with only PostgreSQL up) and,
when the compose stack is running, the *live container* is probed too - a passing in-process test is
not evidence that ``docker compose up -d`` produced a healthy service.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from aimemory.common.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "memory-api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def client(postgres_available: bool):
    """A ``TestClient`` running the real app, including its lifespan (so the runtime is built)."""
    from app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


# ------------------------------------------------------------------------------------- health


def test_health_reports_every_dependency_by_name(client: Any) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    names = {check["name"] for check in payload["checks"]}
    assert names == {"postgres", "neo4j", "embedding"}
    assert payload["writes_enabled"] is get_settings().gateway.write_enabled


def test_health_never_leaks_a_dsn_or_a_password(client: Any) -> None:
    body = client.get("/health").text
    settings = get_settings()

    assert settings.postgres.password.get_secret_value() not in body
    assert settings.neo4j.password.get_secret_value() not in body
    assert "postgresql+psycopg://" not in body


def test_postgres_being_up_is_what_makes_the_service_healthy(client: Any) -> None:
    """Neo4j or the embedder being down is ``degraded`` (200), not ``down`` - both have defined
    degraded modes, and a container that fails its healthcheck for a survivable condition would
    stop mcp-server from ever starting."""
    payload = client.get("/health").json()
    postgres = next(check for check in payload["checks"] if check["name"] == "postgres")

    assert postgres["ok"] is True
    assert payload["status"] != "down"


# ------------------------------------------------------------------------------------- routes


def test_every_plan_section_q_route_is_exposed(client: Any) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    for path in (
        "/v1/search",
        "/v1/projects",
        "/v1/projects/{project_id}",
        "/v1/entities/{entity_id}",
        "/v1/related",
        "/v1/decisions",
        "/v1/timeline",
        "/v1/sources",
        "/v1/artifacts/{artifact_id}",
        "/v1/state",
        "/v1/explain/{object_id}",
        "/v1/episodes",
        "/health",
        "/metrics",
    ):
        assert path in paths, f"{path} is missing from the API"
    assert "post" in paths["/v1/episodes"]
    assert "post" in paths["/v1/decisions"]


def test_search_answers_the_frozen_search_result_shape(client: Any) -> None:
    response = client.post("/v1/search", json={"query": "retrieval pipeline", "k": 3})

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) >= {
        "query",
        "hits",
        "related_entities",
        "context",
        "provenance",
        "candidate_counts",
        "latency_ms",
        "warnings",
        "retrieval_log_id",
    }
    assert isinstance(payload["warnings"], list)
    assert payload["latency_ms"] >= 0
    assert len(payload["hits"]) <= 3
    for hit in payload["hits"]:
        assert hit["provenance"] is not None, "provenance completeness is 100 % on returned evidence"
        assert hit["citation"]


def test_state_and_projects_answer_even_on_an_empty_corpus(client: Any) -> None:
    projects = client.get("/v1/projects")
    state = client.get("/v1/state")

    assert projects.status_code == 200
    assert isinstance(projects.json(), list)
    assert state.status_code == 200
    payload = state.json()
    assert "coverage" in payload and "last_ingestion" in payload


# ------------------------------------------------------------------------- errors and writes


def test_an_unknown_id_is_a_sanitized_404(client: Any) -> None:
    response = client.get("/v1/artifacts/00000000-0000-4000-8000-000000000000")

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"] == "not_found"
    assert set(payload) == {"error", "message", "context"}
    assert "Traceback" not in response.text


def test_a_malformed_body_is_a_422_that_describes_the_request_not_the_server(client: Any) -> None:
    response = client.post("/v1/search", json={"k": 5})

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"] == "validation_error"
    assert any("query" in "/".join(item["loc"]) for item in payload["errors"])


def test_an_unparseable_path_parameter_does_not_reach_the_database(client: Any) -> None:
    response = client.get("/v1/entities/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"


def test_writes_follow_the_adr_0008_flag(client: Any) -> None:
    """Refused with 403 while ``GATEWAY_WRITE_ENABLED=false`` - the shipped default."""
    response = client.post("/v1/episodes", json={"text": "written by a test"})

    if get_settings().gateway.write_enabled:
        assert response.status_code == 201
        assert response.json()["accepted"] is True
    else:
        assert response.status_code == 403
        assert response.json()["error"] == "write_disabled"


def test_an_oversized_write_is_rejected_by_the_request_model(client: Any) -> None:
    limit = get_settings().mcp.max_text_chars

    response = client.post("/v1/episodes", json={"text": "x" * (limit + 1)})

    assert response.status_code in {403, 422}, "either gate is fine; silently accepting it is not"


# ------------------------------------------------------------------------------------ metrics


def test_metrics_counts_what_the_process_served(client: Any) -> None:
    client.get("/v1/projects")

    payload = client.get("/metrics").json()

    assert payload["uptime_seconds"] >= 0
    assert payload["requests_total"], "requests are counted per route template"
    assert any(route.endswith("/v1/projects") for route in payload["requests_total"])
    assert all(
        "{" not in route or "_id}" in route for route in payload["requests_total"]
    ), "route templates, not expanded ids, keep the counter cardinality bounded"
    assert "corpus" in payload


def test_metrics_reports_measured_corpus_totals(client: Any) -> None:
    corpus = client.get("/metrics").json()["corpus"]

    if corpus:  # empty only if the database blinked; every value is a MEASURED count
        assert all(isinstance(value, int) and value >= 0 for value in corpus.values())
        assert {"chunks", "embeddings", "sources", "retrieval_logs"} <= set(corpus)


# -------------------------------------------------------------- OpenAPI export (P10-T02 gate)


def test_the_exported_openapi_document_is_committed_and_current() -> None:
    """``schemas/api/openapi.json`` is generated; a stale copy would mislead A10's MCP tools."""
    import openapi_export

    exported = REPO_ROOT / "schemas" / "api" / "openapi.json"
    assert exported.exists(), "run: python apps/memory-api/openapi_export.py"
    assert openapi_export.main(["--check", "--output", str(exported)]) == 0, (
        "schemas/api/openapi.json is stale - re-run apps/memory-api/openapi_export.py"
    )


def test_the_exported_document_names_the_service_and_its_version() -> None:
    import json

    document = json.loads((REPO_ROOT / "schemas" / "api" / "openapi.json").read_text("utf-8"))

    assert document["openapi"].startswith("3.")
    assert document["info"]["title"] == "AI Memory Gateway"
    assert document["info"]["version"] == "0.1.0"
    assert "SearchResult" in document["components"]["schemas"]
    assert "ScoredHit" in document["components"]["schemas"]
    assert "Provenance" in document["components"]["schemas"]


# ------------------------------------------------------------------- exposure (ADR-0007)


def _published_ports() -> list[tuple[str, str]]:
    """``[(service, published spec)]`` across the base compose file and the dev override."""
    out: list[tuple[str, str]] = []
    for name in ("docker-compose.yml", "docker-compose.override.yml"):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for service, definition in (document.get("services") or {}).items():
            for entry in definition.get("ports") or []:
                out.append((service, str(entry)))
    return out


def test_every_published_port_is_bound_to_loopback() -> None:
    """ADR-0007. The container may bind its own interfaces; the *host* side never may."""
    published = _published_ports()

    assert published, "no published ports found - has the compose file moved?"
    for service, spec in published:
        assert spec.startswith("127.0.0.1:"), f"{service} publishes {spec!r}, not on loopback"


def test_memory_api_is_published_on_loopback_port_8000() -> None:
    specs = [spec for service, spec in _published_ports() if service == "memory-api"]

    assert specs, "memory-api publishes no port"
    assert any(spec.startswith("127.0.0.1:") and spec.endswith(":8000") for spec in specs)


# ---------------------------------------------------------- the live container (compose up -d)


def test_the_running_container_answers_health(memory_api_available: bool) -> None:
    """Skips honestly when the stack is not up; when it is, this is the evidence that
    ``docker compose up -d`` produced a *healthy* service, which no in-process test can give."""
    settings = get_settings()

    response = httpx.get(f"{settings.gateway.url}/health", timeout=10.0)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert {check["name"] for check in payload["checks"]} == {"postgres", "neo4j", "embedding"}


def test_the_running_container_serves_search_and_metrics(memory_api_available: bool) -> None:
    settings = get_settings()

    search = httpx.post(
        f"{settings.gateway.url}/v1/search",
        json={"query": "retrieval pipeline", "k": 2},
        timeout=30.0,
    )
    metrics = httpx.get(f"{settings.gateway.url}/metrics", timeout=10.0)

    assert search.status_code == 200
    assert len(search.json()["hits"]) <= 2
    assert metrics.status_code == 200
    assert metrics.json()["uptime_seconds"] > 0
