"""The served MCP surface must equal ``schemas/mcp/tools.json``. Owner: A10 (P11-T01).

This module needs no compose stack: it builds the FastMCP server in-process and asks it what it
serves. That is the point - the equality being asserted is a property of the code, not of a running
container, so it fails in CI the moment a handler and the frozen contract disagree.

P11-T02 (A12) owns the *client* suite that asserts the same equality over a real session; the two are
complementary, and the assertion is stated here as well because a drift should fail fast and cheaply.

The other half of the file covers the ADR-0008 pieces that are pure logic - the write gate's check
order, the rate limiter and the frozen-schema argument validator - for the same reason.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "mcp-server"
TOOLS_JSON = REPO_ROOT / "schemas" / "mcp" / "tools.json"

# apps/mcp-server has a hyphen, so it is not importable as a package; the container runs its modules
# with that directory as the working directory (see apps/mcp-server/main.py) and so does this test.
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytest.importorskip("mcp", reason="the mcp SDK is only installed in the api/mcp/tools images")

from safeguards import Denial, RateLimiter, WriteGate, redact_arguments
from server import build_server
from spec import get_spec
from validation import SchemaViolation, assert_supported, validate_arguments


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def served_tools() -> list:
    return asyncio.run(build_server().list_tools())


# ====================================================================================================
# The contract
# ====================================================================================================


def test_tool_names_equal_the_contract(contract: dict, served_tools: list) -> None:
    assert [t.name for t in served_tools] == [t["name"] for t in contract["tools"]]


def test_tool_counts_match_the_declared_split(contract: dict, served_tools: list) -> None:
    kinds = {t["name"]: t["kind"] for t in contract["tools"]}
    served = {t.name for t in served_tools}
    assert sum(1 for n in served if kinds[n] == "read") == contract["counts"]["read"]
    assert sum(1 for n in served if kinds[n] == "write") == contract["counts"]["write"]


def test_every_input_schema_is_the_contract_object(contract: dict, served_tools: list) -> None:
    """Advertised ``inputSchema`` is the file's object, not one re-derived from a signature."""
    expected = {t["name"]: t["inputSchema"] for t in contract["tools"]}
    for tool in served_tools:
        assert tool.inputSchema == expected[tool.name], tool.name


def test_titles_and_descriptions_come_from_the_contract(contract: dict, served_tools: list) -> None:
    expected = {t["name"]: (t["title"], t["description"]) for t in contract["tools"]}
    for tool in served_tools:
        assert (tool.title, tool.description) == expected[tool.name], tool.name


def test_no_extra_fields_are_advertised(served_tools: list) -> None:
    """tools.json specifies no outputSchema and no annotations; the server must not invent either."""
    for tool in served_tools:
        assert tool.outputSchema is None, tool.name
        assert tool.annotations is None, tool.name


def test_resources_equal_the_contract(contract: dict) -> None:
    server = build_server()
    concrete = {str(r.uri) for r in asyncio.run(server.list_resources())}
    templated = {r.uriTemplate for r in asyncio.run(server.list_resource_templates())}
    assert concrete | templated == {r["uri"] for r in contract["resources"]}


def test_safeguard_numbers_are_read_from_the_contract(contract: dict) -> None:
    spec = get_spec()
    safeguards = contract["safeguards"]
    assert spec.rate_limit_per_minute == safeguards["rate_limit_per_minute"] == 10
    assert spec.max_text_chars == safeguards["max_text_chars"] == 8000
    assert spec.confirm_required is safeguards["confirm_required"] is True
    assert spec.audit_table == safeguards["audit_table"] == "mcp_audit_log"


def test_every_contract_schema_keyword_is_enforced(contract: dict) -> None:
    """A keyword the validator cannot enforce must fail loudly, never be ignored."""
    for tool in contract["tools"]:
        assert_supported(tool["inputSchema"], where=tool["name"])


def test_the_server_holds_no_database_credentials() -> None:
    """ADR-0008: the mcp-server talks only to memory-api.

    Checked over the parsed AST, not the text - the modules *discuss* the rule in their docstrings,
    and a grep that cannot tell an import from a sentence is a test that punishes documentation.
    """
    import ast

    banned_roots = {"sqlalchemy", "psycopg", "psycopg2", "neo4j", "asyncpg", "pgvector"}
    sources = sorted(APP_DIR.glob("*.py"))
    assert sources, "apps/mcp-server has no Python modules"
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                root = module.split(".")[0]
                assert root not in banned_roots, f"{path.name} imports {module}"
                assert not module.startswith("aimemory.persistence"), f"{path.name}: {module}"
        # No credential is read from the environment either.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {"DATABASE_URL", "POSTGRES_PASSWORD", "NEO4J_PASSWORD"}


# ====================================================================================================
# ADR-0008 safeguards (pure logic)
# ====================================================================================================


def _gate(*, mcp_write_enabled: bool = True, limit: int = 10, max_chars: int = 8000) -> WriteGate:
    return WriteGate(
        mcp_write_enabled=mcp_write_enabled,
        rate_limit_per_minute=limit,
        max_text_chars=max_chars,
    )


def test_default_posture_refuses_a_write() -> None:
    denial = _gate(mcp_write_enabled=False).evaluate(
        "memory.add_episode",
        {"text": "hello", "confirm": True},
        client_id="test",
        gateway_writes_enabled=True,
    )
    assert isinstance(denial, Denial)
    assert denial.reason == "write_disabled_mcp"


def test_gateway_flag_alone_also_refuses() -> None:
    denial = _gate().evaluate(
        "memory.add_episode",
        {"text": "hello", "confirm": True},
        client_id="test",
        gateway_writes_enabled=False,
    )
    assert denial is not None and denial.reason == "write_disabled_gateway"


@pytest.mark.parametrize("confirm", [None, False, "true", 1])
def test_confirm_must_be_literally_true(confirm: object) -> None:
    args = {"text": "hello"}
    if confirm is not None:
        args["confirm"] = confirm  # type: ignore[assignment]
    denial = _gate().evaluate(
        "memory.add_episode", args, client_id="test", gateway_writes_enabled=True
    )
    assert denial is not None and denial.reason == "confirm_required"


def test_size_limit_applies_to_every_text_field() -> None:
    gate = _gate()
    long = "x" * 8001
    assert gate.evaluate(
        "memory.add_episode",
        {"text": long, "confirm": True},
        client_id="a",
        gateway_writes_enabled=True,
    ).reason == "size_limit_exceeded"
    assert gate.evaluate(
        "memory.record_decision",
        {"title": "ok", "statement": long, "project_id": "p", "confirm": True},
        client_id="b",
        gateway_writes_enabled=True,
    ).reason == "size_limit_exceeded"


def test_a_confirmed_in_range_write_passes_the_gate() -> None:
    assert (
        _gate().evaluate(
            "memory.add_episode",
            {"text": "hello", "confirm": True},
            client_id="test",
            gateway_writes_enabled=True,
        )
        is None
    )


def test_rate_limit_is_ten_per_minute_per_client() -> None:
    limiter = RateLimiter(limit=10, window_seconds=60.0)
    assert all(limiter.check_and_record("a", now=100.0 + i) for i in range(10))
    assert limiter.check_and_record("a", now=109.0) is False
    assert limiter.check_and_record("b", now=109.0) is True  # a different client is unaffected
    assert limiter.check_and_record("a", now=100.0 + 61) is True  # the window slides


def test_rate_limit_counts_refused_attempts_too() -> None:
    """A disabled endpoint must not be free to hammer."""
    gate = _gate(mcp_write_enabled=False, limit=3)
    reasons = [
        gate.evaluate(
            "memory.add_episode", {"text": "x"}, client_id="c", gateway_writes_enabled=False
        ).reason
        for _ in range(4)
    ]
    assert reasons == ["write_disabled_mcp"] * 3 + ["rate_limited"]


def test_audit_arguments_are_redacted_but_keep_their_shape() -> None:
    redacted = redact_arguments({"text": "y" * 5000, "project_id": "joblab", "confirm": True})
    assert redacted == {"text": "<5000 chars>", "project_id": "joblab", "confirm": True}


# ====================================================================================================
# Frozen-schema argument validation
# ====================================================================================================


def _schema(name: str) -> dict:
    return get_spec().tool(name).input_schema


def test_unknown_arguments_are_refused() -> None:
    with pytest.raises(SchemaViolation, match="unknown argument"):
        validate_arguments({"query": "x", "limit": 5}, _schema("memory.search"))


def test_required_arguments_are_enforced() -> None:
    with pytest.raises(SchemaViolation, match="required"):
        validate_arguments({}, _schema("memory.search"))


def test_defaults_from_the_contract_are_applied() -> None:
    resolved = validate_arguments({"query": "x"}, _schema("memory.search"))
    assert resolved["k"] == 10 and resolved["expand"] is True


@pytest.mark.parametrize(
    "arguments, expected",
    [
        ({"query": "x", "k": 0}, "minimum"),
        ({"query": "x", "k": 51}, "maximum"),
        ({"query": ""}, "minimum 1 chars"),
        ({"query": "x", "types": ["nope"]}, "must be one of"),
        ({"query": "x", "project_ids": "joblab"}, "expected array"),
        ({"query": "x", "expand": "yes"}, "expected boolean"),
    ],
)
def test_contract_constraints_are_enforced(arguments: dict, expected: str) -> None:
    with pytest.raises(SchemaViolation, match=expected):
        validate_arguments(arguments, _schema("memory.search"))


def test_confirm_const_true_is_enforced_by_the_schema_as_well() -> None:
    with pytest.raises(SchemaViolation, match="must be True"):
        validate_arguments(
            {"text": "hi", "confirm": False}, _schema("memory.add_episode")
        )


def test_eight_thousand_char_cap_is_in_the_schema_too() -> None:
    with pytest.raises(SchemaViolation, match="8000"):
        validate_arguments(
            {"text": "x" * 8001, "confirm": True}, _schema("memory.add_episode")
        )
