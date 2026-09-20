# Security

| Document | What it is |
|---|---|
| [`threat-model.md`](threat-model.md) | Assets, actors, trust boundaries, what is explicitly out of scope, the egress story, and every finding with a severity and the assumption it rests on. |
| [`checklist-2026-09-17.md`](checklist-2026-09-17.md) | Plan §T, item by item, with the command that verified it and its raw output. Nine of ten pass; the one failure is SEC-03. |
| [`../../reports/known-limitations.md`](../../reports/known-limitations.md) | The accepted limitations (SEC-L1 … SEC-L7) and the four open findings with owners. |

**Re-run the checks:**

```
scripts/doctor.ps1 -Security        # Windows — live posture of the running stack
scripts/doctor.sh  --security       # bash equivalent
scripts/test.ps1 -- tests/integration/test_security_checklist.py   # the static assertions
```

`scripts/doctor.*` exits non-zero when a security check fails. Note that the *live* half of
`test_security_checklist.py` skips inside the `tools` container, which has no docker CLI and no git —
and must not be given a docker socket, since that is a root-equivalent handle. The doctor scripts are
what exercise those checks in practice.

## Standing rules (in force since the scaffold)

Read-only source mounts; explicit roots only; canonicalised paths inside a root with symlinks and
junctions refused; loopback-only published ports (ADR-0007); `.env` never committed; built-in deny
list + `.memoryignore` + secret detector (`config/policies.yaml`); flagged files never stored or
logged; sanitised errors; MCP writes off by default behind two flags, `confirm=true`, a 10/min rate
limit, an 8,000-character cap and an append-only audit table (ADR-0008); no file bytes through the
Gateway.

## Two things the documentation used to promise and must not

1. **There is no enforced read-only Neo4j user.** Neo4j Community has no RBAC (ADR-0013, re-verified
   2026-09-17). `memory_reader` is an identity, not a privilege boundary. What holds instead is in
   the threat model, SEC-04.
2. **Content does leave the machine in the default configuration.** `LLM_PROVIDER=bedrock` is the
   default (ADR-0014), so episode text is sent to AWS. B16 and AC-8 hold only under
   `LLM_PROVIDER=ollama`. See the threat model, §5.

## The AWS credential mount — resolved from "flagged" to a finding

A03 flagged the `${HOST_AWS_DIR}` → `/home/app/.aws:ro` mount on `ingestion` and `tools` for P15
review. A13's verdict, with evidence, is **SEC-01 (High)** in the threat model. In short: read-only is
adequate for what read-only can do (verified — the container cannot write the file), the optional
empty-directory default is a genuine mitigation, and no code path lets a malicious document cause an
unintended AWS call (no `eval`/`exec`/`subprocess`/`pickle`/unsafe `yaml.load` anywhere in `packages/`
or `apps/`; the boto3 client is hard-coded to `bedrock-runtime`). What is *not* adequate is the
credential's scope: the profile is administrator-equivalent on the whole AWS account where the task
needs one permission on one model. The fix is a dedicated `bedrock:InvokeModel`-only IAM principal —
an AWS-console action for the account owner, outside this repository.
