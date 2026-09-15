# Security

Written by A13 in P15: `threat-model.md`, `checklist-<date>.md` (10 verified items from plan §T with
commands and results), findings with severity, and entries for `reports/known-limitations.md`.

Standing rules (already in force from the scaffold): read-only source mounts, explicit roots only,
loopback-only published ports (ADR-0007), `.env` never committed, built-in deny list + `.memoryignore`
+ secret detector (`config/policies.yaml`), Neo4j read-only user for readers, MCP writes off by default
(ADR-0008), no file bytes through the Gateway, sanitized errors, append-only audit tables.

## Flagged for A13's P15 review: host AWS credentials mounted into the ingestion/tools containers

Added by A03, 2026-09-15, fixing a post-restart gap where `LLM_PROVIDER=bedrock` (the default,
ADR-0012/ADR-0014) had no way to authenticate in Docker at all. See
`docs/operations/bedrock-extraction.md` for the full description; summary of the new attack surface:

- `docker-compose.yml` (`ingestion`) and `docker-compose.override.yml` (`tools`) bind-mount a host
  directory named by `HOST_AWS_DIR` **read-only** at the container user's `/home/app/.aws`. When set to
  a real `~/.aws`, this exposes the host's plaintext, long-lived IAM access keys
  (`~/.aws/credentials`) to whatever runs inside those two containers.
- Per `D:\Account Center\AWS\README.md`, the `mohabehb` IAM user these keys belong to has
  `AdministratorAccess` + `IAMFullAccess` on account `780822965578` — i.e. the mounted credential is
  root-equivalent, not scoped to Bedrock. A compromise of the `ingestion` or `tools` image (a
  dependency supply-chain issue, a malicious file in an ingested source root that reaches a code path
  with shell/eval, etc.) would have the same blast radius as a compromise of the host user's own
  shell with those credentials exported.
- Mitigations already in place: read-only mount (container cannot write back or rotate/delete keys
  through the mount itself — though the credential's own IAM permissions could still do so via the
  network); the mount is optional and defaults to an empty directory
  (`infra/docker/aws-empty`) so a developer who never sets `HOST_AWS_DIR` has zero exposure; nothing
  AWS-related is logged (`bedrock_provider.py` sanitizes botocore exceptions before they leave the
  module); `.env` (which carries `HOST_AWS_DIR`/`AWS_PROFILE`, both non-secret paths/names — the
  actual keys never enter `.env` or any compose file) is git-ignored.
- **Not mitigated, and worth a P15 judgment call**: the mounted IAM user is administrator-equivalent
  on the whole AWS account, far broader than "call one Bedrock model." A15/A13 or the account owner
  may want to scope a dedicated `bedrock:InvokeModel`-only IAM user/role for this container instead of
  reusing `mohabehb`, but that is an AWS-console change outside A03's owned files and outside this
  fix's scope (recorded here, not actioned).
