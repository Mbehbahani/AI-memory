# Enabling Bedrock extraction in Docker

Written by A03 (infra), 2026-09-15, as part of a post-restart recovery fix. `LLM_PROVIDER=bedrock`
(Claude Haiku 4.5, ADR-0012/ADR-0014) is the **default** extraction provider, but no compose service
passed AWS credentials, region, or even `boto3` itself into the containers that run extraction — so
the default was silently non-functional under Docker. This note documents the fix and how to use it.
See `docs/security/README.md` for the security-relevant part of this (flagged for A13's P15 review).

## What changed

- `infra/docker/requirements.lock` now includes the `bedrock` extra (`boto3`, `botocore`,
  `s3transfer`, `jmespath`, `python-dateutil`, `six`, `urllib3`).
- `apps/ingestion/Dockerfile` and `infra/docker/tools.Dockerfile` install `aimemory[...,bedrock]`, so
  `import boto3` succeeds inside the `ingestion` and `tools` images. `memory-api` and `mcp-server`
  never run extraction and were left untouched.
- `docker-compose.yml` (`ingestion`) and `docker-compose.override.yml` (`tools`) now:
  - mount an **optional, read-only** host directory at the container user's `~/.aws`
    (`/home/app/.aws` — both Dockerfiles create uid 1000 with `--create-home`), and
  - pass `AWS_PROFILE`, `AWS_REGION`, `BEDROCK_REGION`, `BEDROCK_MODEL_ID`, `BEDROCK_MAX_TOKENS`
    through from `.env`.

Nothing is copied, baked into an image, or logged. `boto3` is imported lazily by
`packages/aimemory/providers/llm/bedrock_provider.py` and only constructs a client when
`LLM_PROVIDER=bedrock` is actually selected, so none of this affects `LLM_PROVIDER=ollama` at runtime.

## How to enable it

1. Have a working AWS CLI profile on the host (see `D:\Account Center\AWS\README.md` for this
   project's account; the working profile is `mohabehb`, region `us-east-1` — the `default` profile in
   that account is a separate, currently-expired console session and is **not** interchangeable).
2. In your local `.env` (never committed), set:
   ```
   HOST_AWS_DIR=C:/Users/<you>/.aws
   AWS_PROFILE=mohabehb          # only if your profile is not named "mohabehb"
   LLM_PROVIDER=bedrock          # already the default
   ```
3. `docker compose up -d` (or `docker compose --profile tools run --rm tools ...`). The `ingestion`
   worker and the `tools` runner will resolve credentials via `boto3.Session(profile_name=AWS_PROFILE)`
   reading the mounted, read-only `~/.aws/credentials` and `~/.aws/config`.

`BEDROCK_REGION` (default `us-east-1`, matching the account registry) is passed explicitly to the
`bedrock-runtime` client constructor — it does not depend on the profile carrying a region. This
matters here: the account registry notes the `mohabehb` profile has **no region set** in
`~/.aws/config`, so a call without an explicit region fails with `NoRegion`.

## How to turn it off

Either:
- set `LLM_PROVIDER=ollama` in `.env` (the credential mount stays present but unused — `boto3` is only
  imported/invoked when the provider is `bedrock`), or
- leave `HOST_AWS_DIR` unset. It defaults to `infra/docker/aws-empty`, a directory checked into this
  repo that is always empty, so `docker compose up -d` and the `tools` profile work with zero AWS
  setup and `LLM_PROVIDER=bedrock` will simply fail authentication cleanly (`NoCredentialsError`)
  rather than silently reading a stray host `~/.aws`.

## What gets mounted

Read-only bind mount, host directory named by `HOST_AWS_DIR` (or the empty fallback) -> the
container's `/home/app/.aws`. This is a directory mount, not individual files, so it also picks up
`~/.aws/config` (region/output settings) and any `~/.aws/cli/cache` the AWS CLI itself might write —
none of that is written back (`:ro`), and neither `ingestion` nor `tools` runs the AWS CLI itself
today (only `boto3`).

## Verification performed (2026-09-15)

- `docker compose config` — mount and env resolve, no secret value printed (only the profile name and
  region, both non-secret identifiers).
- Removing `HOST_AWS_DIR`/`AWS_PROFILE` from the env file and re-running `docker compose config` /
  `docker compose --profile tools config` — both exit 0, mount source falls back to
  `infra/docker/aws-empty`, proving the mount is optional.
- `docker compose --profile tools run --rm tools python -c "import boto3, boto3.session as s; ..."` —
  resolved `sts:GetCallerIdentity` to account `780822965578`, ARN
  `arn:aws:iam::780822965578:user/mohabehb`, inside the container, using only the mounted read-only
  `~/.aws` and the `AWS_PROFILE`/`BEDROCK_REGION` env vars (no AWS CLI installed in the image, so a
  tiny boto3 script was used instead of `aws sts get-caller-identity`).
- `docker compose --profile tools run --rm tools pytest -q` — full suite still passes with the
  `bedrock` extra installed and the (optional) credential mount present.

Raw output for all of the above is in the A03 task result for this fix.
