"""State-changing requests from a foreign origin are refused (SEC-02, A13's P15 review).

The `/ops` page posts plain HTML forms and has no CSRF token, and the same-origin policy does not
stop a cross-origin form POST. A13 reproduced the consequence against the running service: a page on
another site could post `action=scan&tier=2` to `/ops/runs`, enqueue a `run_requests` row, and have
the always-on worker execute it — a paid Bedrock run triggered by a page the owner merely visited.
Nothing could be read back (no CORS headers are sent), so it is a trigger rather than a data leak.

The guard keys on `Origin` because browsers attach it to every cross-origin state-changing request
and cannot be talked out of it. Requests with **no** `Origin` are deliberately allowed: that is
server-to-server traffic — the MCP server calling this API over the compose network, `curl`, the
test client — which a browser cannot forge. Blocking those would break the MCP audit sink, whose
whole purpose is recording write refusals (ADR-0008).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "apps" / "memory-api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

pytest.importorskip("fastapi", reason="the API deps are only installed in the api/tools images")

from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), base_url="http://127.0.0.1:8000")


FOREIGN = "https://evil.example"
OWN = "http://127.0.0.1:8000"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_state_changing_method_is_refused_cross_origin(client: TestClient, method: str) -> None:
    response = client.request(method, "/v1/mcp/audit", headers={"Origin": FOREIGN}, json={})
    assert response.status_code == 403
    assert response.json()["error"] == "cross_origin_write_rejected"


def test_the_ops_action_a13_reproduced_is_now_refused(client: TestClient) -> None:
    """The exact request from the review: a cross-origin form POST that would enqueue a paid run."""
    response = client.post(
        "/ops/runs",
        headers={"Origin": FOREIGN, "Content-Type": "application/x-www-form-urlencoded"},
        content="action=scan&tier=2",
    )
    assert response.status_code == 403, "a foreign page could still enqueue an ingestion run"


def test_a_same_origin_write_is_not_blocked(client: TestClient) -> None:
    """The /ops page must keep working. It is enough that the request reaches the handler - whatever
    the handler then decides is not this middleware's business."""
    response = client.post(
        "/ops/runs",
        headers={"Origin": OWN, "Content-Type": "application/x-www-form-urlencoded"},
        content="action=__not_a_real_action__&tier=2",
    )
    assert response.status_code != 403


def test_a_request_without_an_origin_is_allowed(client: TestClient) -> None:
    """Server-to-server. The MCP audit sink posts here with no Origin header, and ADR-0008's audit
    trail depends on it being accepted."""
    response = client.post("/v1/mcp/audit", json={"tool": "test.probe", "kind": "read"})
    assert response.status_code != 403


def test_reads_are_unaffected(client: TestClient) -> None:
    """A cross-origin GET is not a state change, and blocking it would buy nothing: without CORS
    headers the caller cannot read the response anyway.

    Uses `/openapi.json` rather than `/health` deliberately - `create_app()` here has no runtime
    injected, so `/health` would 500 for reasons that have nothing to do with this middleware and
    the test would be measuring the fixture instead of the guard.
    """
    assert client.get("/openapi.json", headers={"Origin": FOREIGN}).status_code == 200
