# AI Memory V0.1 — `tools` profile image (test runner). Owner: A03 (infra).
# Used by docker-compose.override.yml's `tools` service (profile "tools") to run pytest/ruff/mypy
# against the live compose stack from inside a real Python 3.12 environment (the Windows host runs
# Python 3.14, which aimemory does not target — pyproject.toml pins requires-python ">=3.12,<3.13").
#
# Build context is the repo root:
#   docker build -f infra/docker/tools.Dockerfile -t aimemory/tools:dev .
#
# Dependencies pinned in infra/docker/requirements.lock (same lock as ingestion/memory-api/mcp-server).
# aimemory is installed editable with the `dev` extra (pytest, pytest-asyncio, ruff, mypy, hypothesis)
# plus `api`+`mcp` so tests can import the gateway/MCP app code too.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /workspace

COPY infra/docker/requirements.lock /tmp/requirements.lock
COPY pyproject.toml README.md /workspace/
COPY packages/aimemory /workspace/packages/aimemory

# pip -c constraints files cannot carry extras markers (e.g. uvicorn[standard]); strip them to
# name==version before using the lock file as a version-pinning constraints file.
RUN sed -E 's/^([A-Za-z0-9_.-]+)\[[^]]*\]==/\1==/' /tmp/requirements.lock > /tmp/constraints.txt \
    && pip install --no-cache-dir -c /tmp/constraints.txt -e ".[dev,api,mcp]" \
    && rm -f /tmp/requirements.lock /tmp/constraints.txt

# The rest of the repo (tests/, apps/, config/, schemas/) is bind-mounted at /workspace by
# docker-compose.override.yml so test edits on the host are picked up without a rebuild.

RUN chown -R app:app /workspace
USER app

CMD ["pytest", "-q"]
