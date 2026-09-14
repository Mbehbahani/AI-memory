# AI Memory V0.1 — mcp-server (FastMCP over memory-api HTTP). Owner: A03 (infra); app code owner: A10.
# Build context is the repo root:
#   docker build -f apps/mcp-server/Dockerfile -t aimemory/mcp-server:dev .
#
# Dependencies pinned in infra/docker/requirements.lock (same lock as memory-api/ingestion/tools).
# aimemory is installed editable with the `mcp` extra (mcp SDK).
#
# NOTE (A03 -> A10): apps/mcp-server/ has no application code yet (P11-T01). This Dockerfile's CMD
# assumes an entrypoint `apps.mcp_server.main:app`-style ASGI app or a `python -m` launcher exposing
# streamable-HTTP on 8020 with `/health` (matches docker-compose.yml healthcheck + plan section R).
# Adjust the CMD below once the real entrypoint exists; the build itself does not require it.

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
    && pip install --no-cache-dir -c /tmp/constraints.txt -e ".[mcp]" \
    && rm -f /tmp/requirements.lock /tmp/constraints.txt

COPY apps/mcp-server /app/apps/mcp-server
COPY config /app/config
COPY schemas /app/schemas

RUN chown -R app:app /app
USER app

EXPOSE 8020
CMD ["python", "-m", "apps.mcp_server.main"]
