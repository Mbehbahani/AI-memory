#!/usr/bin/env bash
# Health check for the AI Memory stack: Docker daemon, compose config, per-service health, port
# bindings (loopback-only), model presence, disk space. Read-only.
# Usage: scripts/doctor.sh [--security]
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
fail=0

step() { echo "== $1 =="; }

step "Docker daemon"
if ! docker version --format '{{.Server.Version}}' >/tmp/doctor_docker_version 2>&1; then
    echo "FAIL: Docker daemon not reachable: $(cat /tmp/doctor_docker_version)"
    fail=1
else
    echo "Docker Engine $(cat /tmp/doctor_docker_version) reachable."
fi

step ".env present"
if [[ ! -f .env ]]; then
    echo "FAIL: .env missing -- run scripts/init-env.sh"
    fail=1
else
    echo ".env present."
fi

step "docker compose config"
if ! docker compose config --quiet; then
    echo "FAIL: docker compose config failed"
    fail=1
else
    echo "Compose files (base + override) are valid."
fi

step "Service status / health"
docker compose ps || true

step "Published ports are loopback-only (127.0.0.1)"
bad=0
for id in $(docker compose ps -q 2>/dev/null); do
    ports="$(docker inspect "$id" --format '{{json .NetworkSettings.Ports}}')"
    if echo "$ports" | grep -q '"HostIp":"0.0.0.0"'; then
        echo "FAIL: container $id publishes a port on 0.0.0.0"
        bad=1
    fi
done
if [[ "$bad" -eq 1 ]]; then fail=1; else echo "All published container ports are bound to 127.0.0.1."; fi

step "Ollama model presence (qwen3:4b)"
if docker compose ps -q ollama >/dev/null 2>&1 && [[ -n "$(docker compose ps -q ollama)" ]]; then
    if docker compose exec -T ollama ollama list 2>&1 | grep -q 'qwen3:4b'; then
        echo "qwen3:4b present."
    else
        echo "FAIL: qwen3:4b not present."
        fail=1
    fi
else
    echo "ollama service not running -- skip."
fi

step "Disk space"
docker system df || true
df -h "$repo_root" || true

if [[ "${1:-}" == "--security" ]]; then
    step "Security checklist (--security)"
    echo "STUB: full checklist implemented by A13 in P15-T01 (docs/security/*)."
fi

echo ""
if [[ "$fail" -eq 0 ]]; then
    echo "doctor: ALL CHECKS PASSED"
    exit 0
else
    echo "doctor: FAILED"
    exit 1
fi
