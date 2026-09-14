#!/usr/bin/env bash
# Restore a PostgreSQL backup produced by scripts/backup.sh.
# Usage: scripts/restore.sh <dump-file-or-timestamp>
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

[[ -f .env ]] || { echo ".env not found" >&2; exit 1; }
arg="${1:?Usage: scripts/restore.sh <dump-file-or-timestamp>}"

path="$arg"
if [[ ! -f "$path" && -f "backups/postgres/$arg.dump" ]]; then
    path="backups/postgres/$arg.dump"
fi
[[ -f "$path" ]] || { echo "Dump file not found: $arg" >&2; exit 1; }

[[ -n "$(docker compose ps -q postgres 2>/dev/null)" ]] || { echo "postgres service is not running -- run scripts/up.sh first" >&2; exit 1; }

echo "About to run pg_restore --clean --if-exists from $path"
echo "This will DROP and recreate objects in the current database. Ctrl+C within 5s to abort."
sleep 5

docker compose exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$path"

echo "Restore complete from $path."
echo "Neo4j was not touched -- run scripts/rebuild-graph.sh to re-project it from Postgres."
