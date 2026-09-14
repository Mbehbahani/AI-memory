#!/usr/bin/env bash
# Create .env from .env.example with cryptographically random passwords for
# POSTGRES_PASSWORD, NEO4J_PASSWORD, NEO4J_READONLY_PASSWORD. Never prints the generated values.
#
# Usage: scripts/init-env.sh [--force]
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
example_path="$repo_root/.env.example"
env_path="$repo_root/.env"
force=0
[[ "${1:-}" == "--force" ]] && force=1

if [[ ! -f "$example_path" ]]; then
    echo ".env.example not found at $example_path" >&2
    exit 1
fi

if [[ -f "$env_path" && "$force" -ne 1 ]]; then
    echo ".env already exists at $env_path -- not overwriting. Re-run with --force to regenerate." >&2
    exit 0
fi

random_secret() {
    # 32 alphanumeric chars (no URI-reserved characters; DATABASE_URL embeds this value).
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32
}

postgres_user="$(grep -E '^POSTGRES_USER=' "$example_path" | head -1 | cut -d= -f2-)"
postgres_db="$(grep -E '^POSTGRES_DB=' "$example_path" | head -1 | cut -d= -f2-)"
postgres_user="${postgres_user:-aimemory}"
postgres_db="${postgres_db:-aimemory}"

postgres_password="$(random_secret)"
neo4j_password="$(random_secret)"
neo4j_readonly_password="$(random_secret)"

awk -v pw="$postgres_password" -v npw="$neo4j_password" -v nrpw="$neo4j_readonly_password" \
    -v user="$postgres_user" -v db="$postgres_db" '
    /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" pw; next }
    /^NEO4J_PASSWORD=/ { print "NEO4J_PASSWORD=" npw; next }
    /^NEO4J_READONLY_PASSWORD=/ { print "NEO4J_READONLY_PASSWORD=" nrpw; next }
    /^DATABASE_URL=/ { print "DATABASE_URL=postgresql+psycopg://" user ":" pw "@postgres:5432/" db; next }
    { print }
' "$example_path" > "$env_path"

echo "Wrote $env_path"
echo "Generated random POSTGRES_PASSWORD / NEO4J_PASSWORD / NEO4J_READONLY_PASSWORD (values not printed)."
echo "Before 'scripts/up.sh', review HOST_VAULT_ROOT / HOST_PILOT_ROOT and other non-secret values in .env."
