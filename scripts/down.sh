#!/usr/bin/env bash
# Stop the AI Memory stack WITHOUT deleting volumes. Never passes -v/--volumes to docker compose.
# Usage: scripts/down.sh
set -euo pipefail

for arg in "$@"; do
    if [[ "$arg" == "-v" || "$arg" == "--volumes" ]]; then
        echo "REFUSED: '$arg' would delete named volumes (pg_data, neo4j_data, ollama_models, ingestion_state). Never run 'docker compose down -v' in this project." >&2
        exit 1
    fi
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

set -x
docker compose down --remove-orphans
{ set +x; } 2>/dev/null

echo "Stack stopped. Volumes preserved (pg_data, neo4j_data, neo4j_logs, neo4j_import, ollama_models, ingestion_state)."
