#!/usr/bin/env bash
# Run an ingestion pass via the `ingestion` service's CLI (`aimemory-ingest run`).
# Usage: scripts/ingest.sh --root <root> [--tier N] [-- extra args]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -n "$(docker compose ps -q ingestion 2>/dev/null)" ]]; then
    echo "docker compose exec ingestion aimemory-ingest run $*"
    docker compose exec ingestion aimemory-ingest run "$@"
else
    echo "ingestion worker not running -- using 'docker compose run --rm'"
    echo "docker compose run --rm ingestion aimemory-ingest run $*"
    docker compose run --rm ingestion aimemory-ingest run "$@"
fi
