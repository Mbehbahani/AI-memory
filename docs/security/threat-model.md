# AI Memory V0.1 — threat model

- **Owner:** A13 (P15-T01)
- **Date:** 2026-09-17
- **Scope of this document:** the system as it actually runs on `D:\AI memory` today, with the corpus
  it actually holds (177 sources, 2,853 chunks, 614 entities, 1,244 facts, 729 artifacts, 1,669 Neo4j
  nodes — MEASURED 2026-09-17).
- **Companion:** [`checklist-2026-09-17.md`](checklist-2026-09-17.md) — every plan §T item with the
  command that verified it and the raw output. This document is the *why*; that one is the *evidence*.
- **Executable form:** `tests/integration/test_security_checklist.py` and
  `scripts/doctor.ps1 -Security` / `scripts/doctor.sh --security`.

---

## 0. Read this first: proportion

This is a **single-user, single-machine, loopback-only** system. There is no remote access, no
multi-tenancy, no untrusted operator, and no service exposed beyond `127.0.0.1`. Almost every control
below rests on that one fact, and the honest summary is:

> **If the host user account is compromised, every control in this system is already bypassed.** None
> of them are designed to survive that, and pretending otherwise would be the wrong kind of thorough.

What this model is actually for is narrower and real:

1. the system must not **damage the user's own data** (the source roots are read-only, always);
2. it must not **leak the user's data** to places the user did not choose (a cloud LLM, a log file, a
   git commit, a LAN neighbour);
3. an **AI client** speaking MCP must not be able to quietly rewrite the memory it reads from;
4. a **hostile document** inside an otherwise-trusted corpus must not be able to escape its lane.

Findings are rated against that frame, not against an internet-facing service.

---

## 1. Assets

| # | Asset | Where it lives | Why it matters |
|---|---|---|---|
| A1 | **The source roots** — `D:\My-Vault`, `D:\AWS2\SupaBaseProject\DE` | Host filesystem, bind-mounted `:ro` | The user's irreplaceable originals. The system is a *reader*. Corrupting these is the worst outcome available. |
| A2 | **Vault content at rest** — `source_text`, `chunks`, `episodes`, `mirror_blobs` | `pg_data` volume | Contains motivation letters naming real employers, job applications, a personal profile, a skill map. Sensitive personal data. |
| A3 | **Derived knowledge** — `entities`, `facts`, `knowledge_artifacts`, the Neo4j projection | `pg_data`, `neo4j_data` | ~35 minutes of paid extraction. Postgres is the system of record (ADR-0001); the graph is rebuildable and therefore *not* an asset of record. |
| A4 | **Host AWS credentials** | `C:\Users\PC\.aws`, bind-mounted `:ro` into `ingestion` and `tools` | `AdministratorAccess` + `IAMFullAccess` on account `780822965578`. See SEC-01 — the highest-value asset in this table, and it does not belong to this system. |
| A5 | **Stack credentials** — Postgres and Neo4j passwords | `.env` (git-ignored), container env | Guard A2/A3 from other *local* processes. Not a boundary against the host user. |
| A6 | **Query history** | `retrieval_logs.query_text` (178 rows), `mcp_audit_log.arguments` | What the user asked the system is itself personal data. |
| A7 | **Availability and cost** | The running stack; the AWS bill | With `LLM_PROVIDER=bedrock` an ingestion run costs real money (ESTIMATED ~$1.66 for a full vault). |

---

## 2. Actors

| Actor | Trusted? | Capability assumed |
|---|---|---|
| **The owner** (Mohammad, at the console) | Fully | Root of trust. Can do anything; no control targets this actor. |
| **An AI client over MCP** (Claude Code / Desktop) | Partially | Reads freely. May be steered by content it reads. Must not be able to write memory without a deliberate, out-of-band act by the owner. This is the actor ADR-0008 exists for. |
| **A hostile document inside a source root** | Not trusted | Arrives via `Clippings/` (`origin=external, trust=low`) or a cloned repo. Can attempt prompt injection, path traversal via filename, decompression/size abuse, and secret-bearing content. |
| **Another local process on the host** | Not trusted *by convention*, but unstoppable in practice | Can reach every loopback port unauthenticated. Mitigated only by the fact that such a process already has the user's privileges. Stated, not defended. |
| **A malicious web page the owner visits** | Not trusted | The one genuinely *remote* actor with a path to loopback: a browser can be made to issue cross-origin requests to `127.0.0.1`. See SEC-02. |
| **Anything on the LAN / the internet** | Not trusted | **No path in.** Every published port is `127.0.0.1` (verified live, see checklist item 2). |
| **AWS Bedrock** | Trusted with content, by an explicit decision | Receives episode text under ADR-0014. Not trusted with anything else. |

---

## 3. Trust boundaries

```
                      ┌─────────────────────── the host user account ────────────────────────┐
                      │  (root of trust — every boundary below is inside it)                 │
                      │                                                                      │
  D:\My-Vault ──:ro──►│  ingestion ──────────────────────────────────► AWS Bedrock  ◄── B3   │
  (A1, never written) │     │  ▲                                        (egress: A2 leaves)  │
                      │     │  └── ~/.aws :ro (A4)  ◄── B4                                   │
                      │     ▼                                                                │
                      │  postgres (A2, A3)  ◄──── B2 ────  memory-api ──127.0.0.1:8000──┐    │
                      │     ▲                                  ▲                        │    │
                      │  neo4j (projection) ───────────────────┘                        │    │
                      │                                                                 │    │
                      │  mcp-server ──127.0.0.1:8020──  no DB credentials  ◄── B1       │    │
                      └─────────────────────────────────────────────────────────────────┼────┘
                                                                                        │
                                              browser / MCP client / other local process┘
```

- **B1 — MCP ↔ Gateway (ADR-0008).** The mcp-server has *no* database credentials (verified: its
  environment contains no `DATABASE_URL`, `NEO4J_*` or `POSTGRES_*`). Everything it does goes through
  memory-api's HTTP contract, so there is exactly one place where write policy is enforced and one
  place to audit. Writes need `MCP_WRITE_ENABLED` **and** `GATEWAY_WRITE_ENABLED` — two flags on two
  processes — plus `confirm=true`, under a 10/min rate limit and an 8,000-character cap.
- **B2 — Gateway ↔ stores.** memory-api is the only process holding both the Postgres DSN and a Neo4j
  credential. It never returns raw file bytes (no source root is mounted into it at all — verified:
  its `Mounts` array is empty).
- **B3 — the machine ↔ AWS (ADR-0014).** The *only* deliberate egress. Episode text is sent to
  `us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`. Assumption B16 ("no cloud calls at
  runtime") **no longer holds in the default configuration**; see §5.
- **B4 — container ↔ host credentials.** `~/.aws` is bind-mounted read-only. Read-only stops the
  container mutating the file; it does not stop it *using* the key. See SEC-01.
- **B5 — host ↔ network.** Loopback only. This is the boundary that holds up almost everything else,
  and it is the one verified most directly (docker inspect + `netstat -ano`).

---

## 4. What is explicitly **out of scope** for V0.1

Named here so nothing later mistakes an absence for an oversight.

| Out of scope | Why | What would change it |
|---|---|---|
| **Authentication / authorisation on any endpoint** | One user, one machine, loopback only (ADR-0007, ADR-0011). memory-api `/v1/**` and `/ops`, the MCP server, Neo4j Browser and NeoDash are all unauthenticated to anything that can already reach loopback. | Any remote access, or a second user on the machine. |
| **A database-enforced read-only Neo4j user** | Neo4j Community has no RBAC (ADR-0013 — re-verified today: `SHOW ROLES` → *Unsupported administration command*; `memory_reader` successfully ran a `CREATE`). | Neo4j Enterprise, or a proxy in front of Bolt. |
| **Encryption at rest** | Docker volumes on an unencrypted host volume. The vault originals are not encrypted either, so encrypting the derivative would be theatre. | BitLocker on `D:` would cover both, and costs nothing here. |
| **Defending against a compromised host user** | See §0. | Nothing within this project's reach. |
| **Supply-chain verification of Python/Docker dependencies** | No SBOM, no pinned hashes beyond image digests in `reports/image-pins.md`. This is the realistic path by which SEC-01 turns into an incident. | `pip-audit` / hash-pinned lockfiles in CI. |
| **Multi-device sync** | Plan §AG names it out of scope, and ADR-0013 says it must not be crossed while the graph has no privilege model. | A new ADR, after ADR-0013 is revisited. |
| **Prompt-injection-proof extraction** | A hostile document *can* steer what the LLM extracts. The mitigation is structural, not semantic: output is schema-validated, entity types cannot contradict deterministic seeds (ADR-0014 §3), and provenance stamps every fact. A poisoned fact is visible and supersedable, not silent. | A dedicated injection-detection pass; not V0.1. |

---

## 5. Egress: what leaves this machine, plainly

**It is true that vault content is sent to AWS.** ADR-0014 made `LLM_PROVIDER=bedrock` the default
because the local model failed the plan's own relationship-recall bar (40.2 % vs ≥ 50 %). The owner
was told this before the decision. The honest accounting:

**What leaves:** the `body` of each Tier-2 episode — chunk-level text from `INDEX_CONTENT` sources.
For this corpus that includes motivation letters naming real employers, job applications, a personal
profile and a skill map. 146 of 147 episodes have been extracted this way.

**What does not leave:**

| | Protected by | Verified |
|---|---|---|
| Sources flagged `secret_suspected` | Two independent gates: the pipeline refuses to create an episode for one, **and** `claim_episode_for_extraction` excludes them in SQL (`coalesce(s.secret_suspected, false) = false`) | 1 flagged source in the corpus; it has **0** versions with text, **0** chunks, **0** episodes, **0** mirror blobs. Its one episode row is `skipped`. |
| `CATALOG_ONLY` / `IGNORE` sources | No text is stored, so there is nothing to send | 31 `CATALOG_ONLY` sources; `source_text` holds 146 rows against 144 `INDEX_CONTENT` + 2 `MIRROR` sources |
| Embeddings | MiniLM runs locally; the container is built with `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` | image env inspected |
| Retrieval, the graph, MCP, the Ops page | All local. An outage degrades *extraction* only | ADR-0014 |
| Credentials | `bedrock_provider.py` sanitizes botocore exceptions before they escape the module; no AWS value appears in 37,250 log lines | grep over full `docker compose logs` |

**How to turn egress off entirely:** set `LLM_PROVIDER=ollama` and unset `HOST_AWS_DIR`. Per ADR-0014
rule 2 this must be a *separate corpus scope or an explicit re-extraction* — the pipeline refuses to
mix extraction models, so this is not a flag you flip mid-corpus.

The secret detector is therefore **a privacy control on egress**, not hygiene. It is also
conservative: the single flagged source in this corpus is a false positive (a university job advert
containing an `sk-`-prefixed string). Losing one job advert's text to protect against sending a real
key is the right trade, and it is the direction the failure should point.

---

## 6. Findings

Severity is calibrated to §0: **High** = plausible real harm on this machine today; **Medium** =
real, bounded, or needs an unlikely precondition; **Low** = hygiene or defence-in-depth.

### SEC-01 — The mounted AWS credential is administrator-equivalent, not Bedrock-scoped — **HIGH**

**What.** `${HOST_AWS_DIR}` → `/home/app/.aws:ro` on `ingestion` and `tools`. On this machine that
resolves to `C:\Users\PC\.aws`, whose single profile `mohabehb` has, per
`D:\Account Center\AWS\README.md`, `AdministratorAccess` + `IAMFullAccess` on account `780822965578`
— *"effectively root-equivalent… can delete every resource in the account, create new admin users,
and modify billing."*

**Verified.** `docker inspect` shows the mount as `RW=false`; a write attempt inside the container
returns `Read-only file system`. The directory contains `credentials` (816 B, one profile
`[mohabehb]`), `config`, and **also** `cli/cache/session.db` and `login/cache/*.json` — mounting the
whole directory exposes every credential artefact in it, present and future, not just the one profile
the provider uses.

**Why it is High.** The asset is the most valuable thing in §1 and it does not belong to this system.
The task needs exactly one permission (`bedrock:InvokeModel` on one inference profile in one region);
it is given all of them. Read-only prevents the container rewriting the key file — it does nothing
about the key's own IAM permissions, which are exercised over the network.

**Why it is not Critical.** Exploiting it requires code execution inside `ingestion` or `tools`. A13
looked for the obvious paths and did not find them: there is **no** `eval`, `exec`, `os.system`,
`subprocess`, `pickle.loads` or unsafe `yaml.load` anywhere in `packages/` or `apps/` (all six YAML
call sites use `safe_load`); the boto3 client is constructed once, hard-coded to `bedrock-runtime`,
with no path by which document content selects a service or an operation; LLM output is parsed as
JSON against frozen schemas. **A malicious document cannot cause an unintended AWS call** — the
realistic route is a dependency supply-chain compromise, which §4 lists as out of scope.

**Recommendation (owner action, outside this repo).** Create a dedicated IAM user or role whose only
policy is `bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` on
`arn:aws:bedrock:us-east-1::inference-profile/us.anthropic.claude-haiku-4-5-*`, put it in its own
profile, and point `HOST_AWS_DIR` at a directory containing *only* that profile. That removes the
finding entirely at a cost of about ten minutes in the AWS console. A13 did not and cannot make this
change: it is an AWS-console action, not a repository one.

**Interim mitigation already in place and confirmed:** the mount is optional and defaults to
`infra/docker/aws-empty` (present, empty), so a developer who never sets `HOST_AWS_DIR` has zero
exposure; `LLM_PROVIDER=ollama` removes the need for it altogether.

**Assumption this rests on:** that the account registry's record of `mohabehb`'s permissions is
current (it is dated and the file is the designated source of truth; A13 did not call AWS to confirm
— doing so would itself be an AWS call with those keys).

---

### SEC-02 — `/ops` actions are unauthenticated, cross-origin-reachable form POSTs — **MEDIUM**

**What.** `POST /ops/runs` and `POST /ops/review` on `127.0.0.1:8000` accept
`application/x-www-form-urlencoded` bodies with no authentication, no CSRF token, no `Origin` check
and no `TrustedHostMiddleware`. A simple cross-origin form POST is not blocked by the same-origin
policy — the *response* is hidden from the attacker, but the *request executes*.

**Reproduction (MEASURED 2026-09-17, deliberately with an invalid action so nothing was enqueued):**

```
$ curl -s -o - -w "\nHTTP %{http_code}\n" -X POST http://127.0.0.1:8000/ops/runs \
    -H "Origin: https://evil.example" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "action=__a13_probe_invalid__&tier=2"
<p class='error'>unrecognised action or tier</p>
HTTP 422
```

The 422 comes from `RunAction(action)` failing — i.e. the request reached the handler and was
processed on its merits. With `action=scan&tier=2` it would have inserted a `run_requests` row, which
the always-on worker executes. An arbitrary `Host:` header is also accepted (`Host: evil.example` →
`HTTP 200` on `/health`), which is what makes DNS rebinding viable.

**Impact.** Not data theft — no CORS headers are emitted, so the attacker cannot read anything back.
It is a **cost and availability** vector: a web page the owner visits while the stack is up can
trigger a paid Bedrock extraction run (A7) or fill `extraction_reviews` with junk verdicts. Bounded
by `RunAction` having only four values, none of which control Docker or delete anything.

**Note on what is *not* affected.** `POST /v1/mcp/audit` — deliberately not behind
`GATEWAY_WRITE_ENABLED`, and the item this review was asked to satisfy itself about — is **not**
reachable this way. It requires a real JSON body, which makes the request non-simple; the preflight
gets `405` with no `Access-Control-Allow-Origin`, so a browser blocks it (MEASURED: form-encoded and
`text/plain` variants both 422 with *"Input should be a valid dictionary"*). A local process can post
to it, but a local process can already reach everything. The route appends only to an append-only
observability table and cannot touch an entity, fact or artifact. **A13's judgement: the exemption is
correctly reasoned and is not a write vector worth worrying about.** Its one rough edge is SEC-06.

**Recommendation (A09/A16 — NEEDS_HANDOFF, not fixed here).** Either add
`TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost", "127.0.0.1:8000", ...])`, or reject
state-changing requests carrying an `Origin` header that is not `http://127.0.0.1:8000`. Both are a
few lines in `apps/memory-api/app.py` and neither needs a session or a token.

**Assumption:** that the owner browses the web on the same machine while the stack is running — true
here.

---

### SEC-03 — The MCP audit sink latches to "local" on one transient error and never recovers — **MEDIUM**

**What.** ADR-0008 requires every write call, *especially* a refusal, to be recorded in
`mcp_audit_log`. `apps/mcp-server/audit.py::AuditTrail.record` sets `self.sink = "local"` on **any**
`ApiError` — including a transient `api_unreachable` — and nothing ever sets it back. `_warned`
latches too, so the single warning is emitted once and every subsequent drop is a quiet
`"persisted": false` line.

**Verified (MEASURED 2026-09-17):**

```
mcp-server-1 | {"code": "api_unreachable", "event": "mcp.audit_sink_failed", ...,
                "status": null, "timestamp": "2026-09-17T18:14:52.505246Z"}
```

That is memory-api being recreated. From then on:

```
$ curl -s http://127.0.0.1:8020/health | jq .audit
{"sink": "local", "degraded": true, "submitted": 32, "persisted": 7, "dropped": 25}
```

**25 of 32 audit records — 78 %, including every write refusal since 18:14 — never reached the
table.** Four refusals A13 generated at 19:19 (client `a13-security-probe`) appear in the container
log and in `/health`'s ring buffer, but `SELECT count(*) FROM mcp_audit_log` stayed at 7.

**Proof it is the latch and not a broken route:** a second mcp-server started from the same image
against the same memory-api reported `sink: "gateway"` and persisted **19 of 19** refusals, including
`confirm_required`, `size_limit_exceeded` and `write_disabled_gateway` rows.

**Impact.** The durable audit trail silently stops after any memory-api restart — which is every
`docker compose up -d` that recreates the service. The records are not lost (container log + a
200-entry ring), but the auditable artefact ADR-0008 names is incomplete and the only signal is a
boolean on `/health`. Not High: nothing was *written* while unaudited (writes are off), and the
degradation is honest rather than silently claiming persistence.

**Repro for A10:** `docker compose restart memory-api`, wait for healthy, call `memory.add_episode`
over MCP, then compare `/health`'s `audit.dropped` against `SELECT count(*) FROM mcp_audit_log`.

**Recommendation (A10 — NEEDS_HANDOFF).** Distinguish *"the route does not exist"* (404/405 →
latch, which is what the latch was written for) from *"the route was briefly unreachable"* (connection
error / 5xx → retry, or re-probe on a timer). A `sink` that can return to `gateway` is enough; a small
retry queue is not needed.

**Interim:** both doctor scripts now **fail** on `audit.degraded`, so the condition is visible
instead of buried. Restarting mcp-server restores the sink.

---

### SEC-04 — Any process on loopback with a valid Neo4j credential can write the graph — **MEDIUM (accepted, ADR-0013)**

**What.** Re-verified today, not taken on trust: `SHOW ROLES` → *"Unsupported administration
command"*, and `memory_reader` successfully executed a write (`CREATE … DELETE …` in one transaction;
node count before and after: **1,669 → 1,669**, zero probe nodes left behind). Neo4j Community has no
RBAC. `memory_reader` is an **identity**, not a privilege boundary.

**What actually holds, and was checked:**

1. `Neo4jGraphStore.query()` refuses any Cypher matching
   `CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL apoc.*.(create|merge|remove)`. More importantly,
   **no user-controlled Cypher reaches it**: all five call sites (`knowledge/rebuild.py`,
   `providers/graph/writer.py`, `retrieval/expansion.py`) pass module-level constant strings with bind
   parameters, and memory-api exposes no Cypher endpoint. So the regex is defence in depth, not the
   thing standing between an attacker and the graph.
2. The graph is a rebuildable projection (ADR-0001). The blast radius of an unauthorised write is
   `scripts/rebuild-graph.ps1`, never a lost fact.
3. Bolt is on `127.0.0.1:7687` and only under the dev override.

**One correction to the regex's reach, for the record:** APOC *is* installed (190 procedures
available to `memory_reader`), so a write procedure outside the `create|merge|remove` naming
convention — e.g. `apoc.cypher.doIt` — would not match. This is only reachable by someone who can
already choose the Cypher, which no code path allows. Recorded so a future endpoint that *does* take
Cypher does not inherit a false sense of safety. **Do not build one without revisiting this.**

**NeoDash is unconstrained** — it runs arbitrary dashboard Cypher and does not go through
`GraphStore`. Its mitigations are the whole story: `profile viz`, loopback only, and no credentials in
its environment (it is `standalone` and the operator types them into the browser).

---

### SEC-05 — Backups contain a plaintext `.env`; nothing is encrypted at rest — **LOW (accepted)**

`scripts/backup.{sh,ps1}` copies `.env` into `backups/config/<timestamp>/.env` so `restore` can work.
Neither script ever *prints* a secret: `pg_dump` runs as
`sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump …'` inside the container, so the password is expanded
by the container's own shell and never crosses to the host side, and the printed summary is paths and
byte counts only (checklist item 10 — PASS). `backups/*` is git-ignored except `.gitkeep`.

The residual is that `backups/`, `pg_data` and `neo4j_data` are all unencrypted on `D:`. So is
`D:\My-Vault`. Enabling BitLocker on `D:` would close all of them at once and is the single
highest-value thing on this list that costs nothing.

---

### SEC-06 — `mcp_audit_log.arguments` is size-unbounded — **LOW**

`McpAuditRecord` in `apps/memory-api/routes/write.py` caps `tool` (200), `client_id` (200),
`denied_reason` (500) and `result_ref` (500), but `arguments: dict[str, Any]` has no cap, and
`POST /v1/mcp/audit` has no rate limit. The MCP server itself redacts oversized text (A13 observed
`{"text": "<8001 chars>"}` in a real refusal record), so the *intended* client is well-behaved; a
buggy or hostile local client is not bounded by anything. Worst case is local disk growth in an
append-only observability table. Reachable only from a local process (see SEC-02 on why a browser
cannot). **A09 may want a `max_length` on the serialised `arguments`.**

---

### SEC-07 — Write payloads are stored and logged verbatim — **INFORMATIONAL**

Every MCP write attempt, including refused ones, stores its `arguments` in `mcp_audit_log` and emits
them as a structured log line. That is exactly what ADR-0008 asks for, and the content is
client-supplied rather than file bytes — but if a future client attempts to write a sensitive
episode, the refusal record keeps a copy of it. Correct as designed; noted so it is not a surprise.
The same applies to `retrieval_logs.query_text`, which retains every search string indefinitely (178
rows today).

---

## 7. Attack walkthroughs

**"A malicious note in `Clippings/` tries to make the system write outside the vault."** The
filename is normalised before any filesystem call (`os.path.normpath` collapses `..` first), rejected
if it contains a control character, rejected if any component from the root down is a symlink or a
junction, and rejected again after full resolution if it is not under the root. Independently,
`resolve_policy` denies any relative path containing a `..` segment with rule
`builtin_deny:path_traversal`. And independently of *that*, the mount is `:ro`, so even a successful
escape has nothing to write with — proven, not assumed: `echo test > /sources/vault/x` inside the
container returns `Read-only file system`. **Three layers, each sufficient alone.**

**"A note contains an API key."** The filename patterns (`.env`, `*.pem`, `id_rsa*`, `credentials*`,
…) and seven content regexes downgrade the source to `CATALOG_ONLY` with `secret_suspected=true`. No
text row, no chunk, no episode — so nothing to embed, nothing to send to Bedrock, nothing to return
through retrieval. The database is the second gate: `claim_episode_for_extraction` filters these out
in SQL even if an episode somehow existed. Verified against the real corpus and against the three
adversarial fixtures.

**"An AI client decides to rewrite a fact it disagrees with."** It calls `memory.add_episode`. With
the shipped posture it gets *"MCP writes are disabled"* and a `write_disabled_mcp` audit row. With
`MCP_WRITE_ENABLED=true` it gets *"needs an explicit confirm=true"*; with `confirm=true` it gets
*"The memory Gateway has writes disabled"*; past that, 10 calls per minute and 8,000 characters. And
even then writes only **append** — new episodes and artifacts, supersession links — never update or
delete. All four refusal types were exercised for real (see checklist item 7).

**"A web page the owner visits attacks the stack."** It cannot read anything (no CORS headers). It
cannot reach `/v1/**` JSON endpoints (preflight blocked). It *can* POST a form to `/ops/runs` —
SEC-02.

**"Something reads the logs."** 37,250 log lines were searched for every password in `.env`, for
`AKIA`/private-key/`aws_secret_access_key`/`sk-` patterns, and for Windows absolute paths: **zero
hits**. The only `text`/`body` fields present are MCP write-tool arguments (SEC-07). Sampled 60- and
39-character substrings of real `source_text` rows appear **0** times. File contents do not reach the
logs; paths and ids do, which is the intent.

---

## 8. What would change this model

- **Remote access of any kind.** Revisit ADR-0013 *first* — the graph has no privilege model, and §4
  says that boundary must not be crossed until it does.
- **A second user on the machine**, or running the stack as a service account.
- **Enabling MCP writes.** The controls are real and were tested; but the audit trail must be fixed
  (SEC-03) before writes are turned on, or enabled writes will go partly unrecorded.
- **Adding an endpoint that accepts Cypher, SQL, or a filesystem path from a caller.** Each would
  invalidate a distinct assumption above; SEC-04's note applies to the first.
- **Scoping the AWS credential (SEC-01).** This would downgrade the single highest finding to nothing
  and is the recommended next action.
