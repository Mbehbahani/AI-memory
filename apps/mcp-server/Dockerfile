# AI Memory V0.1 — mcp-server (FastMCP over memory-api HTTP). Owner: A03 (infra); app code owner: A10.
# Build context is the repo root:
#   docker build -f apps/mcp-server/Dockerfile -t aimemory/mcp-server:dev .
#
# Dependencies pinned in infra/docker/requirements.lock (same lock as memory-api/ingestion/tools).
# aimemory is installed editable with the `mcp` extra (mcp SDK).
#
# ENTRYPOINT (A10, P11-T01): `apps/mcp-server` contains a hyphen, so it can never be a Python
# package - `python -m apps.mcp_server.main` (the P2 placeholder) could not import and left this
# container in a restart loop. Same fix A09 applied to memory-api: make the app directory the
# WORKDIR and run a top-level module from it. `main:app` is the Starlette app FastMCP serves,
# streamable-HTTP at /mcp plus GET /health (docker-compose.yml healthcheck, plan section R).
#
# The bind host is 0.0.0.0 *inside* the container; docker-compose.yml publishes 127.0.0.1:8020:8020,
# which is what carries the plan section T loopback guarantee. Binding the container's own loopback
# would make the service unreachable through docker-proxy.

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
ENV MCP_BIND_HOST=0.0.0.0
ENV MCP_HOST_PORT=8020

WORKDIR /app/apps/mcp-server
CMD ["sh", "-c", "exec uvicorn main:app --host \"$MCP_BIND_HOST\" --port \"$MCP_HOST_PORT\" --no-access-log"]
