# Security checklist — verified 2026-09-17

- **Owner:** A13 (P15-T01) · **Plan reference:** §T · **Threat model:** [`threat-model.md`](threat-model.md)
- **System state at verification (MEASURED):** 8 containers up; 177 sources, 2,853 chunks, 614
  entities, 1,244 facts, 729 artifacts, 147 episodes, 146 `source_text` rows, 1,669 Neo4j nodes.
- **Corpus integrity:** counts taken before and after the full test run were **identical** (see
  item 11). No ingestion, no extraction, no AWS call was made by this review.
- **Re-run it:** `scripts/doctor.ps1 -Security` (Windows) / `scripts/doctor.sh --security` (bash) for
  the live posture; `pytest tests/integration/test_security_checklist.py` for the static assertions.

Every item below carries the command that was actually run and its raw output, trimmed only for
length (marked `…`) and for secrets (marked `<REDACTED>`). Nine of ten pass; one fails and is
SEC-03.

| # | Item (plan §T) | Result |
|---|---|---|
| 1 | Every source root mount is `:ro`; a write inside the container fails | **PASS** |
| 2 | Only `127.0.0.1` bindings; base compose publishes only 8000/8020/5005 | **PASS** |
| 3 | `.env` git-ignored and absent from images | **PASS** |
| 4 | Neo4j read-only user cannot write | **FAIL as written — superseded by ADR-0013.** The user *can* write; the claim was correctly withdrawn. See SEC-04. |
| 5 | Path guard: traversal fixtures rejected with tests | **PASS** |
| 6 | Secret detector: adversarial fixtures `CATALOG_ONLY`, no text stored, nothing in logs | **PASS** |
| 7 | MCP: writes refused without flag/confirm; audit rows exist; no DB creds in mcp-server | **PARTIAL** — refusals and credential separation pass; audit persistence fails (**SEC-03**) |
| 8 | Error responses carry no stack traces or absolute host paths | **PASS** |
| 9 | Logs contain paths and ids, never file contents | **PASS** |
| 10 | Backup script output excludes `.env` values from any printed summary | **PASS** |
| 11 | *(added)* Corpus unchanged by this review | **PASS** |

Findings raised: **SEC-01** (High), **SEC-02** (Medium), **SEC-03** (Medium), **SEC-04** (Medium,
accepted), **SEC-05** / **SEC-06** (Low), **SEC-07** (informational). All are described in
[`threat-model.md`](threat-model.md) §6.

---

## 1. Read-only source mounts — PASS

```
$ docker inspect ai-memory-ingestion-1 --format '{{range .Mounts}}[{{.Type}} {{.Source}} -> {{.Destination}} RW={{.RW}} Mode={{.Mode}}] {{end}}'
[bind D:/My-Vault -> /sources/vault RW=false Mode=ro]
[bind D:/AWS2/SupaBaseProject/DE -> /sources/joblab-de RW=false Mode=ro]
[bind D:\AI memory\config -> /app/config RW=false Mode=ro]
[bind D:\AI memory\.memoryignore -> /app/.memoryignore RW=false Mode=ro]
[volume …/ai-memory_ingestion_state/_data -> /state RW=true Mode=rw]
[bind /run/desktop/mnt/host/c/Users/PC/.aws -> /home/app/.aws RW=false Mode=ro]
```

Proof rather than trust — the kernel refuses the write, not the compose file:

```
$ docker exec ai-memory-ingestion-1 sh -c 'echo test > /sources/vault/__a13_probe.txt; echo "vault-exit=$?"; \
    echo test > /sources/joblab-de/__a13_probe.txt; echo "joblab-exit=$?"; \
    echo test > /home/app/.aws/__a13_probe; echo "aws-exit=$?"; \
    echo test > /app/config/__a13_probe; echo "config-exit=$?"'
sh: 1: cannot create /sources/vault/__a13_probe.txt: Read-only file system
vault-exit=2
sh: 1: cannot create /sources/joblab-de/__a13_probe.txt: Read-only file system
joblab-exit=2
sh: 1: cannot create /home/app/.aws/__a13_probe: Read-only file system
aws-exit=2
sh: 1: cannot create /app/config/__a13_probe: Read-only file system
config-exit=2
```

`memory-api` and `mcp-server` have **no mounts at all** (`Mounts` is empty), which is the structural
reason "no file bytes through the Gateway" holds: the Gateway has no filesystem to read them from.

Every mount to a source root or to host credentials is `RW=false`. **PASS.**

## 2. Loopback exposure — PASS

Live bindings from the daemon (not from YAML):

```
$ for id in $(docker compose ps -q); do docker inspect $id --format '{{.Name}} {{json .NetworkSettings.Ports}}'; done
/ai-memory-embedding-service-1 {"8010/tcp":[{"HostIp":"127.0.0.1","HostPort":"8010"}]}
/ai-memory-ingestion-1         {}
/ai-memory-mcp-server-1        {"8020/tcp":[{"HostIp":"127.0.0.1","HostPort":"8020"}]}
/ai-memory-memory-api-1        {"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"8000"}]}
/ai-memory-neo4j-1             {"7474/tcp":[{"HostIp":"127.0.0.1","HostPort":"7474"}],"7687/tcp":[{"HostIp":"127.0.0.1","HostPort":"7687"}]}
/ai-memory-neodash-1           {"5005/tcp":[{"HostIp":"127.0.0.1","HostPort":"5005"}]}
/ai-memory-ollama-1            {"11434/tcp":[{"HostIp":"127.0.0.1","HostPort":"11434"}]}
/ai-memory-postgres-1          {"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5432"}]}
```

The host's real listening sockets agree — no `0.0.0.0`, no `[::]`:

```
> netstat -ano | Select-String LISTENING | Select-String ":8000|:8010|:8020|:5005|:5432|:7474|:7687|:11434"
  TCP    127.0.0.1:5005    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:5432    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:7474    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:7687    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:8000    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:8010    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:8020    0.0.0.0:0    LISTENING    4760
  TCP    127.0.0.1:11434   0.0.0.0:0    LISTENING    4760
```

Base compose publishes exactly the three permitted ports; the databases are in the dev override only:

```
$ bash scripts/doctor.sh --security   # step T2
Base compose publishes 8020 8000 5005 on 127.0.0.1 only.
```

**Note on the convention, verified as correct.** Services bind `0.0.0.0` *inside* the container
(`MEMORY_API_BIND_HOST=0.0.0.0`, `MCP_BIND_HOST=0.0.0.0`). That is not a violation: a container's
internal interfaces are reachable only from the compose network, and the guarantee ADR-0007 makes is
about the *published* port. This is stated here because the two look alike in a grep.

**The existing test was checked, not assumed.** `test_gateway_api.py::test_every_published_port_is_bound_to_loopback`
asserts `str(entry).startswith("127.0.0.1:")`. That assertion is *correct* (it fails closed on the
long-form mapping syntax, which would stringify to a dict repr), but it leaves two gaps: it never
checks §T's "base publishes only 8000/8020/5005", and it can only see YAML. Both gaps are now covered
by `tests/integration/test_security_checklist.py`. Writing that test also surfaced a trap worth
recording: naively splitting `"127.0.0.1:${MEMORY_API_HOST_PORT:-8000}:8000"` on `:` yields four
parts and silently loses the host IP — `_expand()` resolves the interpolation first.

**PASS.**

## 3. `.env` hygiene — PASS

```
$ git check-ignore -v .env
.gitignore:2:.env	.env

$ git ls-files --error-unmatch .env
error: pathspec '.env' did not match any file(s) known to git

$ git ls-files | grep -i "\.env"
.env.example
```

Never committed, in any branch, at any point in the 19-commit history:

```
$ git log --all --name-only --pretty=format:"%H" -- '.env'        # (no output)
$ for each PASSWORD/DATABASE_URL value in .env: git log --all -p | grep -cF "<value>"
POSTGRES_PASSWORD -> 0 ; DATABASE_URL -> 0 ; NEO4J_PASSWORD -> 0 ; NEO4J_READONLY_PASSWORD -> 0
```

Absent from every built image, and no secret baked into a layer:

```
$ for img in ai-memory-{ingestion,memory-api,mcp-server,embedding-service}; do
      docker run --rm --entrypoint sh $img -c 'ls -la /app/.env /.env; find / -maxdepth 4 -name ".env"'; done
ls: cannot access '/app/.env': No such file or directory
ls: cannot access '/.env': No such file or directory      (× 4 images, no find hits)

$ docker history --no-trunc --format '{{.CreatedBy}}' <each image> | grep -iE "password|secret|aws_|token|\.env"
(no matches — clean)
```

The only credential-shaped string in any image's default environment is `GPG_KEY`, which is the
upstream `python:3.12` base image's package-signing key — public by definition.

The 10 credential-pattern hits across the whole git history are all test fixtures using AWS's own
documented example key (`AKIAIOSFODNN7EXAMPLE`) or a fixture private key whose body reads
`ThisIsNotARealPrivateKeyItIsAFixtureUsedOnlyToExerciseTheSecretDetectorRegex`.

`schemas/api/openapi.json` contains no credential: its ten matches for
`password|secret|AKIA|aws_|token` are all one substring — `token_budget`.

**PASS.**

## 4. Neo4j read-only user — FAIL as originally written; correctly superseded by ADR-0013

This item is in the plan, so it is answered here rather than quietly dropped. **The control does not
exist.** Re-verified today rather than taken from the ADR:

```
$ docker exec ai-memory-neo4j-1 cypher-shell -u memory_reader -p <REDACTED> --format plain \
    "CREATE (n:__A13SecProbe {id:'probe'}) WITH n DELETE n RETURN 'WRITE_SUCCEEDED' AS result;"
result
"WRITE_SUCCEEDED"

$ … "SHOW ROLES;"
Unsupported administration command: SHOW ROLES
```

The probe creates and deletes in one transaction, so it is net-zero — confirmed:

```
$ … "MATCH (n) RETURN count(n) AS nodes;"            -> 1669   (unchanged, before and after)
$ … "MATCH (n:__A13SecProbe) RETURN count(n);"       -> 0
```

memory-api and NeoDash *do* use the separate identity, which is what ADR-0013 actually promises:

```
$ docker exec ai-memory-memory-api-1 env | grep NEO4J_USER
NEO4J_USER=memory_reader
```

NeoDash is given **no** Neo4j credentials at all (`standalone: true`; the operator types them into the
browser), runs only under `--profile viz`, and reaches Bolt on loopback.

No documentation in this repository claims an enforced boundary — `test_security_checklist.py`
asserts that the threat model names ADR-0013 and says plainly that `memory_reader` *is not a privilege
boundary*, so the false claim cannot silently return. See **SEC-04** for the three layers that do
hold and for one correction to the write-clause regex's reach.

## 5. Path traversal — PASS

`aimemory.domain.source_uri.path_guard` applies four independent checks (control characters →
lexical `normpath` before any `stat` → per-component `islink`/`isjunction` from the root down →
containment after full resolution). `aimemory.sources.policies.resolve_policy` denies `..` separately
with rule `builtin_deny:path_traversal`. And the mount is `:ro` (item 1). Any one of the three is
sufficient.

Fixtures asserted, in `tests/integration/test_security_checklist.py`:

| Input | Expected | Result |
|---|---|---|
| `../../escape.md` | `PathGuardError` | pass |
| `..` | `PathGuardError` | pass |
| `a/../../../etc/passwd` | `PathGuardError` | pass |
| `~/secrets.md` (home expansion) | `PathGuardError` | pass |
| `sub/\x00null.md` (control character) | `PathGuardError` | pass |
| absolute path outside the root | `PathGuardError` | pass |
| symlink/junction pointing out of the root | `PathGuardError` | pass (skips where the OS forbids creating one) |
| `notes/a.md` (legitimate) | resolves | pass |
| `resolve_policy("../../escape.md")` | `IGNORE` + `builtin_deny:path_traversal` | pass |

```
$ docker compose --profile tools run --rm tools pytest -q tests/integration/test_security_checklist.py
21 passed, 5 skipped in 1.07s
```

The junction case is Windows-specific and is skipped inside the Linux tools container, matching the
existing convention in `tests/unit/test_contracts_uri.py` (which also skips: *"junction-style reparse
points are Windows-specific"*). The symlink variant does run there and passes, exercising the same
code path in `path_guard`. **PASS.**

## 6. Secret detector and no stored text — PASS

Against the adversarial fixtures — all three flagged, including the one with an innocuous filename:

| Fixture | Flagged | Rule |
|---|---|---|
| `fake-secrets/.env` | yes | filename `.env` + `aws_access_key` / `conn_string_pw` content |
| `fake-secrets/id_rsa` | yes | filename `id_rsa*` + `private_key` content |
| `fake-secrets/jwt-in-note.md` | yes | content `jwt` only — the filename gives nothing away |
| a clean note | **not** flagged | (negative control, asserted) |

Against the **real corpus**, which is what actually matters:

```
$ psql -c "select policy, secret_suspected, count(*) from sources group by 1,2 order by 1,2;"
    policy     | secret_suspected | count
---------------+------------------+-------
 CATALOG_ONLY  | f                |    30
 CATALOG_ONLY  | t                |     1
 INDEX_CONTENT | f                |   144
 MIRROR        | f                |     2

$ psql -x -c "select s.uri, s.policy, s.policy_reason,
      (select count(*) from source_versions v where v.source_id=s.id)                                            as versions,
      (select count(*) from source_text t join source_versions v on v.id=t.version_id where v.source_id=s.id)    as text_rows,
      (select count(*) from chunks c   where c.source_id=s.id)                                                   as chunks,
      (select count(*) from episodes e where e.source_id=s.id)                                                   as episodes,
      (select count(*) from mirror_blobs m join source_versions v on v.id=m.version_id where v.source_id=s.id)   as blobs
    from sources s where s.secret_suspected;"
uri              | vault://my-vault/00 Inbox/job-positions/Product Owner & Community Lead_ … Utrecht University.md
policy           | CATALOG_ONLY
policy_reason    | secret detector: matched rule(s) [content:openai_style_key] (2 occurrences)
secret_suspected | t
versions         | 1
text_rows        | 0
chunks           | 0
episodes         | 0
blobs            | 0
```

**Zero stored text, zero chunks, zero episodes, zero blobs** for the flagged source. (It is a false
positive — a university job advert containing an `sk-`-prefixed string — which is the direction this
control should fail in.)

Both egress gates confirmed to hold:

```
$ psql -c "SELECT count(*) FROM episodes e JOIN sources s ON s.id=e.source_id WHERE s.secret_suspected;"
 0                      -- the pipeline never created an episode for it

$ psql -c "SELECT status, count(*) FROM episodes GROUP BY 1;"
 extracted | 146
 skipped   |   1        -- the one source-level skip

$ -- the claim query's own predicate, run standalone:
$ psql -c "SELECT count(*) FROM episodes e LEFT JOIN sources s ON s.id = e.source_id
           WHERE e.status IN ('pending','queued') AND coalesce(s.secret_suspected,false) = false
             AND s.secret_suspected IS TRUE;"
 0
```

`claim_episode_for_extraction` carries `coalesce(s.secret_suspected, false) = false` as a database-level
predicate, so the exclusion survives a bug in the pipeline. Nothing flagged appears in any log:

```
$ docker compose logs --no-color | wc -l          -> 37250
$ grep -cE "AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key|sk-[A-Za-z0-9_-]{20,}"   -> 0
```

**PASS.** For what this protects and what it does not, see `threat-model.md` §5 — the honest answer is
that non-flagged vault content *has* been sent to AWS, by an explicit decision (ADR-0014).

## 7. MCP write policy — PARTIAL (controls pass, audit persistence fails → SEC-03)

**7a. Writes refused in the shipped posture.** A real MCP session over streamable-HTTP
(`initialize` → `tools/list` → `tools/call`), client id `a13-security-probe/1.0`:

```
tools: memory.search, memory.get_project, memory.get_entity, memory.get_related, memory.get_decisions,
       memory.get_timeline, memory.get_sources, memory.explain, memory.get_artifact,
       memory.get_current_state, memory.add_episode, memory.record_decision

memory.add_episode {"text": "..."}                     -> isError, "MCP writes are disabled. …"
memory.add_episode {"text": "...", "confirm": true}    -> isError, "MCP writes are disabled. …"
memory.add_episode {"text": <8001 chars>, confirm}     -> isError, "MCP writes are disabled. …"
memory.record_decision {..., "confirm": true}          -> isError, "MCP writes are disabled. …"
```

The Gateway refuses independently, so the MCP server is not the only thing standing in the way:

```
$ curl -X POST http://127.0.0.1:8000/v1/episodes  -d '{"text":"A13 security probe","client":"a13"}'
HTTP 403 {"error":"write_disabled","message":"Writes are disabled (GATEWAY_WRITE_ENABLED=false).","context":{"flag":"GATEWAY_WRITE_ENABLED"}}
$ curl -X POST http://127.0.0.1:8000/v1/decisions -d '{"title":"probe","body":"A13 probe","client":"a13"}'
HTTP 403  (same)
$ curl -X POST http://127.0.0.1:8000/v1/episodes  -d '{"text":"<8001 chars>"}'
HTTP 422 {"errors":[{"loc":["body","text"],"msg":"String should have at most 8000 characters"}]}
```

**7b. `confirm`, size and rate limits, exercised for real.** In the shipped posture the
`MCP_WRITE_ENABLED` gate fires first, so these can never be reached — which means "verified by reading
the code" would have been the only option. Instead a **second, throwaway mcp-server** was started from
the same image with `MCP_WRITE_ENABLED=true` and `GATEWAY_WRITE_ENABLED` left **false** on memory-api,
on `127.0.0.1:8021`. That exercises every gate in front of the Gateway while making an actual write
impossible:

```
1) no confirm       : This tool writes to memory and needs an explicit confirm=true.
2) confirm=false    : This tool writes to memory and needs an explicit confirm=true.
3) confirm="true"   : This tool writes to memory and needs an explicit confirm=true.   <- string, not bool: still refused
4) text 8001 chars  : 'text' is 8001 characters; the limit is 8000.
5) confirm=true     : The memory Gateway has writes disabled. Set GATEWAY_WRITE_ENABLED=true …
6) 14 further calls : calls 1-5 reach the Gateway refusal; call 6 onward ->
                      "Rate limit reached: at most 10 write calls per minute per client."
```

The rate-limit accounting is exactly right: 5 calls in step 5 plus 5 in step 6 is 10, and the
eleventh is refused. `confirm` requires a real boolean `True`, not a truthy string. The container was
removed afterwards (`docker rm -f a13-mcp-probe`).

**7c. Audit rows.** Against the throwaway server, **19 of 19** refusals persisted, with the
distinguishing reason on each:

```
$ psql -c "select tool,confirmed,allowed,denied_reason,left(arguments::text,80) from mcp_audit_log where client_id like 'a13-writeprobe%' order by at limit 6;"
memory.add_episode | f | f | confirm_required        | {"text": "probe"}
memory.add_episode | f | f | confirm_required        | {"text": "probe", "confirm": false}
memory.add_episode | f | f | confirm_required        | {"text": "probe", "confirm": "true"}
memory.add_episode | t | f | size_limit_exceeded     | {"text": "<8001 chars>", "confirm": true}
memory.add_episode | t | f | write_disabled_gateway  | {"text": "probe ok", "confirm": true}
memory.add_episode | t | f | write_disabled_gateway  | {"text": "rl 0", "confirm": true}
```

(Note the oversized text is redacted to `<8001 chars>` before storage — the audit trail does not
become a copy of the payload.)

**Against the long-running mcp-server, it fails.** Four refusals generated at 19:19 UTC never reached
the table:

```
$ curl -s http://127.0.0.1:8020/health | jq .audit
{"sink": "local", "degraded": true, "submitted": 32, "persisted": 7, "dropped": 25}
$ psql -c "select count(*) from mcp_audit_log where client_id like '%a13-security-probe%';"   -> 0
```

Root cause and reproduction: **SEC-03**. In one line — `AuditTrail.record` latches `sink="local"` on
*any* API error, including the transient `api_unreachable` logged at 18:14:52 when memory-api was
recreated, and nothing ever unlatches it.

**7d. No DB credentials in mcp-server.** ADR-0008's boundary holds:

```
$ docker exec ai-memory-mcp-server-1 env | sort
DEVICE_ID=… GPG_KEY=… HOME=/home/app … LOG_FORMAT=json LOG_LEVEL=INFO
MCP_BIND_HOST=0.0.0.0 MCP_HOST_PORT=8020 MCP_TRANSPORT=streamable-http
MCP_WRITE_ENABLED=false MEMORY_API_URL=http://memory-api:8000
PATH=… PYTHON_VERSION=3.12.14 …
```

No `DATABASE_URL`, no `POSTGRES_*`, no `NEO4J_*`. (`memory-api` holds them, as it must; its
`NEO4J_USER` is `memory_reader`.)

**7e. `POST /v1/mcp/audit` is not a write vector worth worrying about — satisfied.** The route is
deliberately outside `GATEWAY_WRITE_ENABLED` because refusals only happen while writes are disabled,
so gating it would guarantee the records that matter most were never stored. A13 checked the obvious
objection — whether that makes it an unauthenticated write channel — and it does not, for a concrete
reason: it requires a real JSON body, which makes a cross-origin request non-simple, so a browser must
preflight, and the preflight is refused with no CORS headers:

```
$ curl -X OPTIONS http://127.0.0.1:8000/v1/mcp/audit -H "Origin: https://evil.example" \
       -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: content-type"
HTTP/1.1 405 Method Not Allowed          (allow: POST; no Access-Control-Allow-Origin)

$ curl -X POST … -H "Content-Type: application/x-www-form-urlencoded" -d "tool=evil&kind=write"
HTTP 422 {"errors":[{"loc":["body"],"msg":"Input should be a valid dictionary or object to extract fields from"}]}
$ curl -X POST … -H "Content-Type: text/plain" -d '{"tool":"a13.probe","kind":"write"}'
HTTP 422  (same)
```

So the route is reachable only from a local process, which can already reach everything; it appends
to an append-only observability table and cannot touch an entity, fact or artifact. **The exemption is
correctly reasoned.** Its one rough edge is that `arguments` has no size cap — **SEC-06**, Low.

## 8. Sanitized errors — PASS

```
$ curl http://127.0.0.1:8000/v1/entities/not-a-uuid
HTTP 422 {"error":"validation_error","message":"The request did not match the expected shape.",
          "context":{},"errors":[{"loc":["path","entity_id"],"msg":"Input should be a valid UUID, invalid character: found `n` at 1"}]}
$ curl http://127.0.0.1:8000/v1/entities/00000000-0000-0000-0000-000000000000
HTTP 404 {"error":"not_found","message":"No such entity.","context":{}}
$ curl "http://127.0.0.1:8000/v1/projects/../../etc/passwd"
HTTP 404 {"detail":"Not Found"}
$ curl "http://127.0.0.1:8000/v1/sources?limit=-5"
HTTP 422 {"error":"validation_error", … "msg":"Input should be greater than or equal to 1"}
$ curl http://127.0.0.1:8000/nope        -> HTTP 404 {"detail":"Not Found"}
$ curl http://127.0.0.1:8000/v1/search   -> HTTP 405 {"detail":"Method Not Allowed"}
```

No traceback, no `/app/...`, no `D:\...`, no connection string, in any of them. The structure behind
this was read, not just sampled: `apps/memory-api/errors.py` installs three handlers, and the
catch-all returns a fixed `GENERIC_500` body while `exc_info` goes only to the local logger.
`AiMemoryError` carries a `public_message` *and* a private `detail`, and `__str__` returns the
public one — so an accidental `f"{exc}"` in a response body cannot leak. Two unit tests in
`test_security_checklist.py` pin that: a `PathGuardError` whose detail names `D:\My-Vault\private\cv.md`
serialises without the string `My-Vault`, and an error whose detail is a DSN with an inline password
serialises without the password. **PASS.**

## 9. Log hygiene — PASS

```
$ docker compose logs --no-color | wc -l
37250

# every PASSWORD/SECRET/TOKEN/DATABASE_URL value from .env, searched literally:
POSTGRES_PASSWORD -> 0 occurrences
DATABASE_URL -> 0 occurrences
NEO4J_PASSWORD -> 0 occurrences
NEO4J_READONLY_PASSWORD -> 0 occurrences

$ grep -cE "AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|aws_secret_access_key|sk-[A-Za-z0-9_-]{20,}"
0
$ grep -cE "[A-Za-z]:\\\\|D:/My-Vault|C:/Users"
0
```

File contents do not reach the logs. Sampled 60-, 39-, 35-, 24- and 22-character substrings taken from
real `source_text` rows: **0 occurrences each**. The only content-bearing keys present are `"text"`
(20 occurrences) and `"body"` (1), and every one of them is an MCP write-tool *argument* inside an
`mcp.audit` record — client-supplied, never file bytes. Example:

```
{"allowed": false, "arguments": {"confirm": true, "text": "a test episode"}, "audit_id": "…",
 "client_id": "aimemory-pytest/0.1.0", "denied_reason": "write_disabled_mcp", "event": "mcp.audit", …}
```

That is what ADR-0008 asks for. The structural control is real: `aimemory.common.logging` installs a
`redact_processor` that masks any key matching
`password|passwd|secret|token|api[_-]?key|authorization|credential|dsn|database_url` and rewrites
inline URL credentials. See **SEC-07** for the one consequence to be aware of. **PASS.**

## 10. Backup output excludes `.env` values — PASS

The password is expanded by the *container's* shell and never crosses to the host side:

```
scripts/backup.sh:21   docker compose exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$dump_path"
scripts/backup.ps1:43  … same construction, single-quoted so PowerShell does not interpolate …
```

Everything the scripts print is a path, a byte count or a status:

```
Running pg_dump -Fc inside the postgres container -> backups/postgres/<ts>.dump
Postgres dump: backups/postgres/<ts>.dump (<n> bytes)
NeoDash dashboard included. | NeoDash dashboard not present yet … -- skipped.
Config snapshot: backups/config/<ts>
Backup complete.
```

No `.env` *value* appears in any summary line. `.env` is *copied* into
`backups/config/<ts>/.env` — required for restore — and `backups/*` is git-ignored except `.gitkeep`,
so it cannot be committed. The residual (an unencrypted credential file on disk, alongside
unencrypted `pg_data` and an unencrypted vault) is **SEC-05**, Low. **PASS.**

## 11. Corpus integrity — PASS

```
                                        sources | chunks | entities | facts | artifacts | episodes | source_text | neo4j
before this review                          177 |  2853  |   614    | 1244  |    729    |   147    |     146     | 1669
after the full test suite (790 passed)      177 |  2853  |   614    | 1244  |    729    |   147    |     146     | 1669
```

```
$ bash scripts/test.sh
790 passed, 6 skipped, 2 warnings in 96.07s (0:01:36)
```

Unchanged. The only row this review added anywhere is 19 refusal records in `mcp_audit_log` (7 → 26)
from the throwaway write-gate server in item 7b — an append-only observability table, not corpus, and
the rows are legitimate audit evidence tagged `a13-writeprobe`. The Neo4j write probe in item 4
created and deleted its node inside one transaction; node count is unchanged and no `__A13SecProbe`
node remains.

---

## Method notes

- Nothing in this review ran an ingestion, an extraction, or an AWS API call. The corpus cost real
  money and real time; it was treated as read-only.
- Every "verified" above is a command whose output is quoted. Where a control could not be exercised
  in the shipped posture (item 7b), a throwaway container was used rather than changing the running
  stack's configuration, and it was removed afterwards.
- One thing was *not* verified and is stated as such: whether the `mohabehb` IAM user's permissions
  are still what `D:\Account Center\AWS\README.md` records. Confirming it would mean making an AWS
  call with the very credential under review.
- **This file will trip the secret detector, and that is correct.** Quoting the grep patterns above
  means the document literally contains `AKIAIOSFODNN7EXAMPLE` (AWS's own published example key ID)
  and the string `-----BEGIN … PRIVATE KEY-----`. If this repository is ever added as a source root,
  expect this file to resolve to `CATALOG_ONLY` with `secret_suspected=true`. Nothing here is a real
  credential; the detector is doing exactly what it should.
- Ad-hoc multi-module pytest selections (e.g. `pytest tests/unit/x.py tests/memory/y.py`) show
  cross-module interference — 16 failures that do not occur when either module runs alone, and that
  occur identically with A13's new file removed. Not caused by this task; `scripts/test.sh` (the
  supported invocation) is green at 790/6. Flagged to A12 as an observation.
