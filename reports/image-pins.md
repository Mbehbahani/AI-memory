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
| `aimemory/ingestion:dev` | `apps/ingestion/Dockerfile` | 335 MB |
| `aimemory/memory-api:dev` | `apps/memory-api/Dockerfile` | 368 MB |
| `aimemory/mcp-server:dev` | `apps/mcp-server/Dockerfile` | 370 MB |
| `aimemory/tools:dev` | `infra/docker/tools.Dockerfile` | 527 MB |
| `aimemory/embedding-service:dev` | `apps/embedding-service/Dockerfile` | 2.45 GB (torch CPU wheel dominates; expected per plan §Z "Images ... 4-5 GB" budget across all built images) |

Total of the five built images: 335 + 368 + 370 + 527 + 2450 ≈ 4.05 GB (MEASURED), inside the plan's
4-5 GB ESTIMATED budget for "Images" (plan §Z), before the pulled `postgres`/`neo4j`/`ollama`/`neodash`
images above (which are separate infra images, not built).
