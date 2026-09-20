#!/usr/bin/env bash
# Run the P14-T03 gold-set evaluation (tests/evaluation/run_eval.py) in the `tools` profile image,
# against the live Memory Gateway (`memory-api`, reached over the compose network as
# `http://memory-api:8000` from inside `tools`). Writes reports/evaluation-<ts>.md.
#
# Usage:
#   scripts/eval.sh                        # gold.yaml, k=5, expansion on
#   scripts/eval.sh --no-expand            # vector+keyword only, for comparison
#   scripts/eval.sh --gold path/to.yaml --k 10 --out reports/custom.md
#
# --benchmark/--model (ADR-0010 §3, P14-T05's frozen tests/evaluation/benchmark/ set) is not
# implemented by this wrapper or by run_eval.py yet - that is separate, not-yet-built work; running
# this script always runs the gold set, never the frozen benchmark.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

echo "docker compose --profile tools run --rm tools python tests/evaluation/run_eval.py $*"
docker compose --profile tools run --rm tools python tests/evaluation/run_eval.py "$@"
