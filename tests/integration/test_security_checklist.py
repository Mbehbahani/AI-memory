"""Executable form of the P15 security checklist. Owner: A13.

``docs/security/checklist-2026-09-17.md`` records the *manual* evidence (raw command output) for
every item in plan section T. This module is the part that must keep holding after A13 is gone: the
static half runs with nothing started, and the live half asserts against the *running* containers and
skips honestly when Docker is not up.

Why it duplicates a little of ``test_gateway_api.py``
----------------------------------------------------
``test_gateway_api.py::test_every_published_port_is_bound_to_loopback`` already checks that every
published port string starts with ``127.0.0.1:``. That is necessary but not sufficient for section T,
which makes two further promises the suite did not check:

* the **base** compose file publishes *only* 8000/8020/5005 (the override may add databases, and only
  it may) - so a new service silently published in the base file would have passed before;
* the loopback guarantee is about the **host** side of a *running* container, which no amount of YAML
  reading can prove. :func:`test_live_published_ports_are_loopback_only` reads
  ``NetworkSettings.Ports`` from the daemon.

Nothing here writes. The live tests are read-only ``docker inspect`` / ``docker exec`` calls and a
single write *attempt* into a read-only mount, which must fail - that is the assertion.

Where the live half actually runs (read this before trusting a green run)
------------------------------------------------------------------------
The suite's normal home is the ``tools`` container, which has **no docker CLI and no git** - and it
must not be given a docker socket, because that is a root-equivalent handle and granting one to run a
security test would be worse than the test is worth. So in ``scripts/test.ps1`` the five live checks
**skip**, and a green run of this file proves the static half only.

The live half therefore has two homes, and both were exercised for P15-T01:

* ``scripts/doctor.ps1 -Security`` / ``scripts/doctor.sh --security`` - the same assertions against
  the running stack, on the host, where docker and git exist. This is the one that runs in practice;
* this file, when pytest is run from a host interpreter that has ``aimemory`` installed.

``docs/security/checklist-2026-09-17.md`` records the raw output of each live check, run by hand.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from aimemory.common.errors import AiMemoryError, PathGuardError
from aimemory.domain.source_uri import path_guard
from aimemory.sources.policies import load_policies_config, resolve_policy
from aimemory.sources.secrets import scan

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERRIDE_COMPOSE = REPO_ROOT / "docker-compose.override.yml"

#: Plan section T: "base compose publishes only 8000/8020/5005".
BASE_ALLOWED_PUBLISHED_PORTS = {"8000", "8020", "5005"}

#: Container paths that are mounted from a source root and must never be writable (CLAUDE.md).
READ_ONLY_CONTAINER_PATHS = ("/sources/vault", "/sources/joblab-de", "/home/app/.aws")


# ----------------------------------------------------------------------------- helpers


def _compose(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _published_specs(document: dict[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for service, definition in (document.get("services") or {}).items():
        for entry in (definition or {}).get("ports") or []:
            out.append((service, entry))
    return out


#: ``${MEMORY_API_HOST_PORT:-8000}`` / ``${NEODASH_HOST_PORT}``. The default is what a checkout runs.
_INTERPOLATION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(text: str) -> str:
    """Replace ``${VAR:-default}`` with ``default`` (and a bare ``${VAR}`` with an empty string).

    Without this, splitting ``"127.0.0.1:${MEMORY_API_HOST_PORT:-8000}:8000"`` on ``:`` yields four
    parts and the host IP is silently lost - which is exactly the kind of quiet pass a security test
    must not produce.
    """
    return _INTERPOLATION_RE.sub(lambda m: m.group(2) or "", text)


def _host_ip_and_port(entry: Any) -> tuple[str | None, str]:
    """Normalise both compose port syntaxes to ``(host_ip, host_port)``.

    Short syntax is ``"127.0.0.1:8000:8000"``; long syntax is a mapping with ``host_ip``/
    ``published``. An entry that is neither is returned with ``host_ip=None`` so the caller fails it.
    """
    if isinstance(entry, dict):
        return entry.get("host_ip"), _expand(str(entry.get("published", "")))
    parts = _expand(str(entry)).split(":")
    if len(parts) == 3:
        return parts[0], parts[1]
    return None, parts[0] if parts else ""


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, check=False
    )


@pytest.fixture(scope="session")
def docker_available() -> bool:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH")
    if _docker("version", "--format", "{{.Server.Version}}").returncode != 0:
        pytest.skip("Docker daemon not reachable")
    return True


@pytest.fixture(scope="session")
def running_containers(docker_available: bool) -> list[str]:
    result = _docker("ps", "--filter", "label=com.docker.compose.project", "--format", "{{.Names}}")
    names = [line for line in result.stdout.splitlines() if line.strip()]
    if not names:
        pytest.skip("no compose containers running - `docker compose up -d` first")
    return names


def _inspect(name: str) -> dict[str, Any]:
    result = _docker("inspect", name)
    assert result.returncode == 0, f"docker inspect {name} failed: {result.stderr}"
    return json.loads(result.stdout)[0]


# ---------------------------------------------------------------- item 2: loopback exposure


def test_base_compose_publishes_only_the_three_documented_ports() -> None:
    """Plan section T / ADR-0007. The base file is what a non-dev checkout runs."""
    published = _published_specs(_compose(BASE_COMPOSE))

    normalised = {port for _service, entry in published for _ip, port in [_host_ip_and_port(entry)]}

    assert normalised == BASE_ALLOWED_PUBLISHED_PORTS, (
        f"base docker-compose.yml publishes {sorted(normalised)}; section T allows only "
        f"{sorted(BASE_ALLOWED_PUBLISHED_PORTS)} (databases belong in the dev override)"
    )


def test_every_published_port_declares_the_loopback_host_ip() -> None:
    """ADR-0007, in both compose syntaxes. The *container* may bind 0.0.0.0; the host side may not."""
    for path in (BASE_COMPOSE, OVERRIDE_COMPOSE):
        if not path.exists():
            continue
        for service, entry in _published_specs(_compose(path)):
            host_ip, _port = _host_ip_and_port(entry)
            assert host_ip == "127.0.0.1", (
                f"{path.name}:{service} publishes {entry!r} with host_ip={host_ip!r}; "
                "ADR-0007 requires an explicit 127.0.0.1"
            )


def test_live_published_ports_are_loopback_only(running_containers: list[str]) -> None:
    """The guarantee ADR-0007 actually makes, read from the daemon rather than from YAML."""
    offenders: list[str] = []
    for name in running_containers:
        ports = _inspect(name)["NetworkSettings"]["Ports"] or {}
        for container_port, bindings in ports.items():
            for binding in bindings or []:
                if binding.get("HostIp") not in ("127.0.0.1", "::1"):
                    offenders.append(f"{name} {container_port} -> {binding.get('HostIp')}")

    assert not offenders, "containers publishing off loopback: " + ", ".join(offenders)


# ------------------------------------------------------- item 1: read-only source mounts


def test_compose_declares_source_roots_read_only() -> None:
    """Static half: every ``${HOST_*_ROOT}`` / ``HOST_AWS_DIR`` bind carries ``:ro``."""
    for path in (BASE_COMPOSE, OVERRIDE_COMPOSE):
        if not path.exists():
            continue
        document = _compose(path)
        for service, definition in (document.get("services") or {}).items():
            for volume in (definition or {}).get("volumes") or []:
                if not isinstance(volume, str):
                    continue
                if "HOST_VAULT_ROOT" in volume or "HOST_PILOT_ROOT" in volume:
                    assert volume.endswith(":ro"), f"{path.name}:{service} mounts {volume!r} writable"
                if "HOST_AWS_DIR" in volume:
                    assert volume.endswith(":ro"), (
                        f"{path.name}:{service} mounts host AWS credentials writable: {volume!r}"
                    )


def test_live_source_mounts_are_not_writable(running_containers: list[str]) -> None:
    """Live half: ``Mounts[].RW`` is false for every source-root and credential bind."""
    checked = 0
    for name in running_containers:
        for mount in _inspect(name).get("Mounts") or []:
            if mount.get("Destination") in READ_ONLY_CONTAINER_PATHS:
                checked += 1
                assert mount.get("RW") is False, (
                    f"{name} mounts {mount['Destination']} read-write (source: {mount.get('Source')})"
                )
    if checked == 0:
        pytest.skip("no source-root mounts on the running containers (ingestion not up)")


def test_a_write_into_a_source_root_actually_fails(running_containers: list[str]) -> None:
    """Proof rather than trust: the kernel, not the compose file, refuses the write."""
    target = next((n for n in running_containers if "ingestion" in n), None)
    if target is None:
        pytest.skip("ingestion container not running")

    result = _docker(
        "exec", target, "sh", "-c", "echo probe > /sources/vault/.a13-write-probe 2>&1; echo rc=$?"
    )

    assert "rc=0" not in result.stdout, f"write into /sources/vault succeeded: {result.stdout!r}"
    assert "Read-only file system" in result.stdout


# ----------------------------------------------------- items 3 / 7: secrets and credentials


def test_env_is_git_ignored_and_untracked() -> None:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("git not available (this runs inside a container without the repo's .git)")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=REPO_ROOT, capture_output=True, check=False
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )

    assert ignored.returncode == 0, ".env is not covered by .gitignore"
    assert tracked.returncode != 0, ".env is TRACKED - rotate every credential in it"


def test_mcp_server_compose_environment_holds_no_database_credentials() -> None:
    """ADR-0008: "the mcp-server holds no database credentials; it talks only to memory-api"."""
    environment = (_compose(BASE_COMPOSE)["services"]["mcp-server"].get("environment")) or {}
    keys = set(environment) if isinstance(environment, dict) else {
        str(item).split("=")[0] for item in environment
    }
    # The YAML merge key `<<: *common-env` is resolved by the safe loader, so `keys` is the full set.
    forbidden = {"DATABASE_URL", "POSTGRES_PASSWORD", "NEO4J_PASSWORD", "NEO4J_URI", "NEO4J_USER"}

    assert not (keys & forbidden), f"mcp-server is given {sorted(keys & forbidden)}"


def test_live_mcp_server_has_no_database_credentials(running_containers: list[str]) -> None:
    target = next((n for n in running_containers if "mcp-server" in n), None)
    if target is None:
        pytest.skip("mcp-server container not running")

    env_keys = {
        line.split("=", 1)[0] for line in _docker("exec", target, "env").stdout.splitlines() if line
    }
    forbidden = {"DATABASE_URL", "POSTGRES_PASSWORD", "NEO4J_PASSWORD", "NEO4J_URI"}

    assert not (env_keys & forbidden), f"mcp-server has {sorted(env_keys & forbidden)} in its env"


def test_aws_credential_mount_is_optional_and_defaults_to_an_empty_directory() -> None:
    """A developer who never sets ``HOST_AWS_DIR`` must get zero AWS exposure (A03's mitigation)."""
    volumes = _compose(BASE_COMPOSE)["services"]["ingestion"]["volumes"]
    aws_mount = next(v for v in volumes if isinstance(v, str) and "HOST_AWS_DIR" in v)

    assert ":-./infra/docker/aws-empty}" in aws_mount, (
        f"HOST_AWS_DIR has no empty-directory default: {aws_mount!r}"
    )
    default_dir = REPO_ROOT / "infra" / "docker" / "aws-empty"
    assert default_dir.is_dir()
    assert not [p for p in default_dir.iterdir() if p.name != ".gitkeep"], (
        "infra/docker/aws-empty is not empty - it is mounted at ~/.aws by default"
    )


# ------------------------------------------------- item 5: path traversal / junction escape


@pytest.mark.parametrize(
    "candidate",
    [
        "../../escape.md",
        "..",
        "a/../../../etc/passwd",
        "~/secrets.md",
        "sub/\x00null.md",
    ],
)
def test_path_guard_rejects_traversal(tmp_path: Path, candidate: str) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)

    with pytest.raises(PathGuardError):
        path_guard(root, candidate)


def test_path_guard_rejects_an_absolute_path_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside" / "loot.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    with pytest.raises(PathGuardError):
        path_guard(root, str(outside))


def test_path_guard_refuses_to_follow_a_link_out_of_the_root(tmp_path: Path) -> None:
    """Symlink/junction escape (plan section T). Skips where the OS will not let us build one."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.md").write_text("secret", encoding="utf-8")
    link = root / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("reparse points need Developer Mode or elevation on this host")

    with pytest.raises(PathGuardError):
        path_guard(root, "link/loot.md")


def test_path_guard_accepts_a_legitimate_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text("ok", encoding="utf-8")

    assert path_guard(root, "notes/a.md", must_exist=True).name == "a.md"


def test_policy_resolution_denies_traversal_before_touching_the_filesystem() -> None:
    config = load_policies_config()
    decision = resolve_policy("../../escape.md", size_bytes=10, config=config)

    assert decision.policy.value == "IGNORE"
    assert "path_traversal" in decision.rule


# ------------------------------------------------------------------- item 6: secret detector


@pytest.mark.parametrize(
    "name",
    [".env", "id_rsa", "jwt-in-note.md"],
)
def test_adversarial_secret_fixtures_are_flagged(name: str) -> None:
    """Every fixture in ``tests/fixtures/adversarial/fake-secrets`` must downgrade to no-text."""
    path = REPO_ROOT / "tests" / "fixtures" / "adversarial" / "fake-secrets" / name
    config = load_policies_config()

    result = scan(path.name, path.read_bytes(), config.secret_detector)

    assert result.suspected, f"{name} was not flagged by the secret detector"
    assert result.matched_rules, f"{name} was flagged with no rule recorded"


def test_a_clean_file_is_not_flagged() -> None:
    config = load_policies_config()

    result = scan("notes.md", b"# A normal note\n\nNothing secret here.\n", config.secret_detector)

    assert not result.suspected


# ------------------------------------------------------------------- item 8: sanitized errors


def test_domain_errors_never_put_detail_in_the_public_payload() -> None:
    """Section T: nothing leaving the process carries a host path or a connection string."""
    error = PathGuardError(detail=r"D:\My-Vault\private\cv.md escapes root D:\My-Vault")

    payload = json.dumps(error.sanitized())

    assert "My-Vault" not in payload
    assert str(error) == error.public_message


def test_str_of_a_domain_error_is_the_public_message() -> None:
    """``f"{exc}"`` in a response body must not leak - the base class guarantees this."""
    error = AiMemoryError("safe", detail="postgresql://aimemory:hunter2@postgres:5432/aimemory")

    assert "hunter2" not in str(error)
    assert "hunter2" not in json.dumps(error.sanitized())


# --------------------------------------------- ADR-0013: no false claim of a read-only Neo4j


def test_security_docs_do_not_claim_an_enforced_neo4j_read_only_user() -> None:
    """ADR-0013 requires the documentation to state the limitation, not promise the boundary."""
    docs = list((REPO_ROOT / "docs" / "security").glob("*.md"))

    assert docs, "docs/security is empty"
    threat_model = REPO_ROOT / "docs" / "security" / "threat-model.md"
    assert threat_model.exists(), "docs/security/threat-model.md is missing (P15-T01 deliverable)"
    text = threat_model.read_text(encoding="utf-8")
    assert "ADR-0013" in text, "the threat model must reference ADR-0013"
    assert "not a privilege boundary" in text or "not an enforcement" in text, (
        "the threat model must state plainly that memory_reader is not enforced"
    )
