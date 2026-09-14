# AI Memory V0.1 — ingestion service (worker + CLI). Owner: A03 (infra); app code owner: A07a.
# Used by BOTH the `migrate` one-shot (`aimemory-ingest migrate`) and the always-on `ingestion`
# worker (`aimemory-ingest worker`), and for ad-hoc CLI use via `docker compose exec ingestion ...`.
#
# Build context is the repo root:
#   docker build -f apps/ingestion/Dockerfile -t aimemory/ingestion:dev .
#
# Dependencies are resolved from infra/docker/requirements.lock (pinned on Python 3.12, see
# infra/docker/README.md). aimemory is installed editable, base extras only (no [api]/[mcp]/[embedding]
# extra is needed for ingestion; sqlalchemy/psycopg/neo4j/httpx/pypdf/python-docx are core deps).

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal runtime deps: pypdf/python-docx are pure Python; no build toolchain required.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY infra/docker/requirements.lock /tmp/requirements.lock
COPY pyproject.toml README.md /app/
COPY packages/aimemory /app/packages/aimemory

# pip -c constraints files cannot carry extras markers (e.g. uvicorn[standard]); strip them to
# name==version before using the lock file as a version-pinning constraints file.
RUN sed -E 's/^([A-Za-z0-9_.-]+)\[[^]]*\]==/\1==/' /tmp/requirements.lock > /tmp/constraints.txt \
    && pip install --no-cache-dir -c /tmp/constraints.txt -e . \
    && rm -f /tmp/requirements.lock /tmp/constraints.txt

COPY apps/ingestion /app/apps/ingestion
COPY config /app/config
COPY schemas /app/schemas
COPY .memoryignore /app/.memoryignore

RUN chown -R app:app /app
USER app

# No fixed ENTRYPOINT: docker-compose sets the full `command:` (e.g. ["aimemory-ingest", "migrate"] /
# ["aimemory-ingest", "worker"]); an ENTRYPOINT here would be prepended to that and duplicate the binary.
CMD ["aimemory-ingest", "--help"]
