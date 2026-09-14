#!/usr/bin/env bash
# Back up PostgreSQL (pg_dump -Fc), .env, config/, .memoryignore, and the NeoDash dashboard if present.
# Neo4j/embeddings/Ollama models are rebuildable and are NOT backed up here (plan section AA).
# Usage: scripts/backup.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }
[[ -n "$(docker compose ps -q postgres 2>/dev/null)" ]] || { echo "postgres service is not running -- run scripts/up.sh first" >&2; exit 1; }

timestamp="$(date +%Y%m%d-%H%M%S)"
pg_dir="backups/postgres"
cfg_dir="backups/config/$timestamp"
mkdir -p "$pg_dir" "$cfg_dir"
dump_path="$pg_dir/$timestamp.dump"

echo "Running pg_dump -Fc inside the postgres container -> $dump_path"
# Uses the postgres container's OWN env vars -- the password is never read/printed on the host side.
docker compose exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$dump_path"

if [[ ! -s "$dump_path" ]]; then
    echo "pg_dump produced an empty file -- check container logs" >&2
    rm -f "$dump_path"
    exit 1
fi
echo "Postgres dump: $dump_path ($(stat -c%s "$dump_path" 2>/dev/null || stat -f%z "$dump_path") bytes)"

cp .env "$cfg_dir/.env"
[[ -f .memoryignore ]] && cp .memoryignore "$cfg_dir/.memoryignore" || true
[[ -d config ]] && cp -r config "$cfg_dir/config" || true

if [[ -f infra/neodash/dashboard.json ]]; then
    cp infra/neodash/dashboard.json "$cfg_dir/dashboard.json"
    echo "NeoDash dashboard included."
else
    echo "NeoDash dashboard not present yet (infra/neodash/dashboard.json) -- skipped."
fi

echo "Config snapshot: $cfg_dir"
echo "Backup complete."
