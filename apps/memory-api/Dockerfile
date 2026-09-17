# AI Memory V0.1 — memory-api (Memory Gateway, FastAPI/uvicorn). Owner: A03 (infra); app code owner: A09.
# Build context is the repo root:
#   docker build -f apps/memory-api/Dockerfile -t aimemory/memory-api:dev .
#
# Dependencies pinned in infra/docker/requirements.lock (same lock as mcp-server/ingestion/tools;
# resolved together on Python 3.12 so versions never drift between images). aimemory is installed
# editable with the `api` extra (fastapi + uvicorn[standard]).
#
# ENTRYPOINT (A09, P10-T02): the application lives at apps/memory-api/app.py and is served as
# `app:app` with WORKDIR /app/apps/memory-api - the same shape as apps/embedding-service. The
# placeholder CMD `apps.memory_api.main:app` could never have imported: the directory on disk is
# `apps/memory-api`, and a hyphen cannot appear in a Python module path (that is exactly the
# `ModuleNotFoundError: No module named 'apps.memory_api'` the service restart-looped on).
#
# BINDING (ADR-0007): the loopback guarantee is the *published* port - docker-compose.yml publishes
# `127.0.0.1:${MEMORY_API_HOST_PORT}:8000`, and tests/integration/test_gateway_api.py asserts that no
# service publishes on any other interface. Inside the container uvicorn binds MEMORY_API_BIND_HOST,
# set below to the container's own interfaces, because a process bound to the *container's* loopback
# is unreachable from both the published port (docker-proxy dials the container IP) and from
# mcp-server on the compose network. Outside a container the app defaults to 127.0.0.1
# (apps/memory-api/app.py:DEFAULT_BIND_HOST). Same pattern as apps/embedding-service.

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
ENV MEMORY_API_BIND_HOST=0.0.0.0
ENV MEMORY_API_PORT=8000

WORKDIR /app/apps/memory-api
CMD ["sh", "-c", "exec uvicorn app:app --host \"$MEMORY_API_BIND_HOST\" --port \"$MEMORY_API_PORT\""]
