# Image Pins — P2-T02 / P3

All values MEASURED on 2026-09-14 by A03 with `docker pull <image>` + `docker inspect --format
'{{.RepoDigests}}'` + `docker images`. Host: Windows 11 Pro, Docker Desktop 4.87.0, engine 29.7.2.
Sizes are `docker images` "Size" (on-disk, after decompression/dedup against the local layer store —
not the registry-compressed download size).

| Image | Tag (pinned) | Digest (sha256) | Pull date (UTC) | Size (MEASURED) | Used by |
|---|---|---|---|---|---|
| `pgvector/pgvector` | `pg17` | `cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f` | 2026-09-14 | 627 MB | `postgres` service |
| `neo4j` | `5.26-community` | `22ec5cd05a8cbb372fc4bed5e384c30bc75fd92504c72be4462039761b105f61` | 2026-09-14 | 991 MB | `neo4j` service |
| `ollama/ollama` | `0.34.0` (was floating `latest`) | `684d8674b4315fa18f4f0e973a118ec2652ed96f67563277839985175858e0ba` | 2026-09-14 | 9.18 GB | `ollama`, `ollama-init` |
| `neo4jlabs/neodash` | `2.4.11` (was floating `latest`) | `04ead957355d11b3c3687465a7ae4622df24a0c3f8266bbd4f0fdfaab5be09ad` | 2026-09-14 | 229 MB | `neodash` (profile `viz`) |
| `python` | `3.12-slim` (pinned by digest in every app Dockerfile, not by a compose var) | `78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea` | 2026-09-14 | 190 MB | base layer for `ingestion`, `memory-api`, `mcp-server`, `embedding-service`, `tools` |

At pull time, `ollama/ollama:latest` and `ollama/ollama:0.34.0` resolved to the same manifest-list
digest (`684d8674...`) and `neo4jlabs/neodash:latest` / `:2.4.11` likewise resolved to the same digest
(`04ead957...`) — confirmed via the Docker Hub API (`GET /v2/repositories/<repo>/tags/<tag>`) before
pulling. Pinning the numeric tag freezes today's `latest` so a future unrelated `docker pull` cannot
silently change the running model runtime or dashboard image; `latest` will keep moving.

`docker-compose.yml` and `.env.example` now default to these pinned tags
(`OLLAMA_IMAGE_TAG=0.34.0`, `NEODASH_IMAGE_TAG=2.4.11`); `pgvector/pgvector:pg17` and
`neo4j:5.26-community` were already non-floating tags (only their digests were unrecorded before this
report).

## Built application images (this repo's Dockerfiles)

Not registry pins (nothing is pushed anywhere in V0.1) but recorded here for completeness — see
`MEASUREMENTS` in the P2-T02 task result for build durations.

| Image | Dockerfile | Size (MEASURED, `docker images`) |
|---|---|---|
| `aimemory/ingestion:dev` | `apps/ingestion/Dockerfile` | 335 MB (2026-09-14); **390 MB (MEASURED 2026-09-15)** after adding the `bedrock` extra (boto3/botocore/s3transfer/jmespath/python-dateutil/six/urllib3) — see below |
| `aimemory/memory-api:dev` | `apps/memory-api/Dockerfile` | 368 MB |
| `aimemory/mcp-server:dev` | `apps/mcp-server/Dockerfile` | 370 MB |
| `aimemory/tools:dev` | `infra/docker/tools.Dockerfile` | 527 MB (2026-09-14); **582 MB (MEASURED 2026-09-15)** after adding the `bedrock` extra |
| `aimemory/embedding-service:dev` | `apps/embedding-service/Dockerfile` | 2.45 GB (torch CPU wheel dominates; expected per plan §Z "Images ... 4-5 GB" budget across all built images) |

Total of the five built images (2026-09-14 baseline): 335 + 368 + 370 + 527 + 2450 ≈ 4.05 GB (MEASURED),
inside the plan's 4-5 GB ESTIMATED budget for "Images" (plan §Z), before the pulled
`postgres`/`neo4j`/`ollama`/`neodash` images above (which are separate infra images, not built). With
the 2026-09-15 `bedrock` extra added to `ingestion` and `tools`, the total rises by ~110 MB to ≈ 4.16 GB
(MEASURED), still inside budget.

### 2026-09-15 — `bedrock` extra added to `ingestion` and `tools` (recovery fix, A03)

`LLM_PROVIDER=bedrock` is the default (ADR-0012/ADR-0014), but `infra/docker/requirements.lock` was
compiled without `--extra bedrock` and `apps/ingestion/Dockerfile` installed the base extras only, so
`import boto3` failed inside every container that runs extraction — the default provider was silently
non-functional in Docker regardless of credentials. Fixed by:
- Regenerating `infra/docker/requirements.lock` with `--extra bedrock` added (boto3 1.43.94, botocore
  1.43.94, s3transfer 0.19.2, jmespath 1.1.0, python-dateutil 2.9.0.post0, six 1.17.0, urllib3 2.7.0 —
  all MEASURED via `pip-compile` resolving against live PyPI on 2026-09-15).
- `apps/ingestion/Dockerfile`: `pip install -e .` → `pip install -e ".[bedrock]"`.
- `infra/docker/tools.Dockerfile`: `pip install -e ".[dev,api,mcp]"` → `pip install -e ".[dev,api,mcp,bedrock]"`.
- Rebuilt both images (`docker compose build ingestion`, `docker compose --profile tools build tools`);
  build times MEASURED 22.4 s and 36.4 s respectively (includes downloading the ~7 new wheels; no base
  image was re-pulled, `python:3.12-slim` pin unchanged).
See `docs/operations/bedrock-extraction.md` and `docs/security/README.md` for the credential-mount
side of this fix (host `~/.aws` mounted read-only, `AWS_PROFILE`/`BEDROCK_REGION` wired through).

## Models (added by A00, P3-T02 verification, 2026-09-14)

| Model | Served by | Size | Id / digest | Pulled |
|---|---|---|---|---|
| `qwen3:4b` | `ollama/ollama:0.34.0` | 2.5 GB (MEASURED, `ollama list`) | `359d7dd4bcda` (MEASURED) | 2026-09-14 |
| `sentence-transformers/all-MiniLM-L6-v2` | `ai-memory-embedding-service` (baked into the image at build) | 384-d | HF revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41` (MEASURED, reported by `/health`) | baked 2026-09-14 |

The MiniLM revision hash is the model identity that must be written to `embedding_models` (A04/A06)
so retrieval never mixes vectors from two revisions.
