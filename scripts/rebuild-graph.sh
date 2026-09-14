#!/usr/bin/env bash
# Recreate the Neo4j projection from PostgreSQL (the system of record).
# Usage: scripts/rebuild-graph.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ -n "$(docker compose ps -q ingestion 2>/dev/null)" ]]; then
    docker compose exec ingestion aimemory-ingest rebuild-graph
else
    docker compose run --rm ingestion aimemory-ingest rebuild-graph
fi
echo "Graph rebuild complete."
