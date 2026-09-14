-- Runs once on first cluster initialization (docker-entrypoint-initdb.d). Schema itself is managed by Alembic.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
