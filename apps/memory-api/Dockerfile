# AI Memory V0.1 — memory-api (Memory Gateway, FastAPI/uvicorn). Owner: A03 (infra); app code owner: A09.
# Build context is the repo root:
#   docker build -f apps/memory-api/Dockerfile -t aimemory/memory-api:dev .
#
# Dependencies pinned in infra/docker/requirements.lock (same lock as mcp-server/ingestion/tools;
# resolved together on Python 3.12 so versions never drift between images). aimemory is installed
# editable with the `api` extra (fastapi + uvicorn[standard]).
#
# NOTE (A03 -> A09): apps/memory-api/ has no application code yet (P10-T02). This Dockerfile's CMD
# assumes the entrypoint module will be `apps.memory_api.main:app` exposing an ASGI `app` object and
# a `/health` route on port 8000 (matches docker-compose.yml healthcheck + plan section D/R). Adjust
# the CMD below if the module path differs; the build itself does not require that module to exist.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY infra/docker/requirements.lock /tmp/requirements.lock
COPY pyproject.toml README.md /app/
COPY packages/aimemory /app/packages/aimemory

# pip -c constraints files cannot carry extras markers (e.g. uvicorn[standard]); strip them to
# name==version before using the lock file as a version-pinning constraints file.
RUN sed -E 's/^([A-Za-z0-9_.-]+)\[[^]]*\]==/\1==/' /tmp/requirements.lock > /tmp/constraints.txt \
    && pip install --no-cache-dir -c /tmp/constraints.txt -e ".[api]" \
    && rm -f /tmp/requirements.lock /tmp/constraints.txt

COPY apps/memory-api /app/apps/memory-api
COPY config /app/config
COPY schemas /app/schemas

RUN chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "apps.memory_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
