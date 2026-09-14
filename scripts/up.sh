#!/usr/bin/env bash
# Bring up the AI Memory stack (base compose + dev override, loaded automatically).
# Usage: scripts/up.sh [--viz]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ ! -f ".env" ]]; then
    echo ".env not found. Run scripts/init-env.sh first." >&2
    exit 1
fi

if [[ "${1:-}" == "--viz" ]]; then
    set -x
    docker compose --profile viz up -d
else
    set -x
    docker compose up -d
fi
{ set +x; } 2>/dev/null

echo "Stack starting. Check status with 'docker compose ps' or scripts/doctor.sh."
