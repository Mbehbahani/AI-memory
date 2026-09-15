# Shared Docker bits

Created by A03 in P2-T02:

- `requirements.lock` — pinned dependency set for `aimemory` core + `api` + `mcp` + `dev` + `bedrock`
  extras, resolved with `pip-compile` (pip-tools 7.4.1) inside `python:3.12-slim` (not the Windows
  host, which runs Python 3.14). Used as a `pip install -c` constraints file by
  `apps/ingestion/Dockerfile`, `apps/memory-api/Dockerfile`, `apps/mcp-server/Dockerfile`, and
  `tools.Dockerfile`, so all images get identical versions of shared packages (sqlalchemy, neo4j,
  pydantic, httpx, etc.) even though each only installs the extras it needs. `bedrock` (boto3 +
  botocore + s3transfer + jmespath + python-dateutil + six + urllib3) was added 2026-09-15 so the
  `ingestion` worker and `tools` runner can actually import boto3 for `LLM_PROVIDER=bedrock`
  (ADR-0012/ADR-0014); see `docs/operations/bedrock-extraction.md`.
- `requirements-embedding.lock` — separate lock for the `embedding` + `api` extras, resolved against
  the CPU-only PyTorch index (`https://download.pytorch.org/whl/cpu`) so `torch` never pulls CUDA
  wheels. Used with `pip install -r` (not `-c`) by `apps/embedding-service/Dockerfile` because it
  embeds `--index-url`/`--extra-index-url` directives that a constraints file would ignore.
- `tools.Dockerfile` — the `tools` compose profile image (`docker-compose.override.yml`): base image +
  `dev`/`api`/`mcp` extras (pytest, ruff, mypy, hypothesis + the app code they test), non-root `app`
  user. Repo content besides `packages/aimemory` is bind-mounted at `/workspace`, not baked in, so
  edits on the host are picked up without a rebuild.

There is no shared `base.Dockerfile`: each of the four `apps/*/Dockerfile` images and `tools.Dockerfile`
is self-contained (`FROM python:3.12-slim@sha256:...` pinned digest, `COPY` + `pip install -c
requirements.lock`), because Compose builds each service from its own Dockerfile independently and
chaining local `FROM` stages across services would require a separate pre-build step. Regenerate the
lock files the same way if `pyproject.toml` dependency ranges change:

```powershell
docker run --rm -v "${PWD}:/workspace" -w /workspace python:3.12-slim bash -c "
  pip install --quiet pip-tools==7.4.1 &&
  pip-compile --resolver=backtracking --extra api --extra mcp --extra dev --extra bedrock \
    -o infra/docker/requirements.lock pyproject.toml &&
  pip-compile --resolver=backtracking --extra embedding --extra api \
    --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
    -o infra/docker/requirements-embedding.lock pyproject.toml
"
```
