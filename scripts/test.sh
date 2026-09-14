#!/usr/bin/env bash
# Run the test suite in the `tools` profile image (Python 3.12, matches the runtime containers).
# Usage: scripts/test.sh [--unit] [-- pytest args...]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

cmd=(pytest -q)
if [[ "${1:-}" == "--unit" ]]; then
    cmd+=(-m "not integration and not memory and not evaluation and not slow")
    shift
fi
cmd+=("$@")

echo "docker compose --profile tools run --rm tools ${cmd[*]}"
docker compose --profile tools run --rm tools "${cmd[@]}"
