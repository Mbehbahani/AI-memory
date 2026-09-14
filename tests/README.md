# AI Memory V0.1 — test suite

Owner of structure/fixtures: A12 (see `docs/development/agent-matrix.md`). Feature owners add tests
in their own areas (`tests/unit/test_<area>.py`); this file documents how to run what's here.

## Why everything runs inside the `tools` container

The host runs Python 3.14; `pyproject.toml` pins `requires-python = ">=3.12,<3.13"`. Every test run
therefore goes through the `tools` profile image (`infra/docker/tools.Dockerfile`, Python 3.12,
`aimemory` installed editable with `dev,api,mcp` extras), which also sits on the `ai-memory-net`
Docker network so service hostnames (`postgres`, `neo4j`, `ollama`, `embedding-service`) resolve.
Running `pytest` directly on the host Windows Python is not supported and is not what CI/agents do.

```powershell
scripts/test.ps1                 # everything
scripts/test.ps1 -Unit           # -m "not integration and not memory and not evaluation and not slow"
scripts/test.ps1 -PytestArgs "-k","policies"
```
```bash
scripts/test.sh                  # everything
scripts/test.sh --unit
```

Equivalent raw compose commands:
```
docker compose --profile tools run --rm --entrypoint pytest tools tests -q
docker compose --profile tools run --rm --entrypoint pytest tools tests -q -m "not integration"
docker compose --profile tools run --rm --entrypoint pytest tools tests -q -m integration
docker compose --profile tools run --rm --entrypoint pytest tools tests -q -m memory
docker compose --profile tools run --rm --entrypoint pytest tools tests -q -m evaluation
docker compose --profile tools run --rm --entrypoint pytest tools tests --collect-only -q
```

`docker compose --profile tools run --rm tools` (no `--entrypoint pytest`) also works - the image's
`CMD` is already `["pytest", "-q"]`; extra args after the service name are appended to it.

## Markers (`pyproject.toml: [tool.pytest.ini_options].markers`)

| Marker | Meaning | Needs services up? |
|---|---|---|
| *(none)* | Pure unit tests: chunkers, policies, ignore rules, URI scheme, path guard, secret detector, contracts, temporal rules. | No |
| `integration` | Talks to a real Postgres/Neo4j/Ollama/embedding-service. | Yes (self-skips otherwise) |
| `memory` | Memory/change-detection scenarios (plan §L/§Y): `tests/memory/`. | Depends on the test; several are currently `pytest.skip()`d pending P6-T03/T04. |
| `evaluation` | Retrieval gold-set evaluation: `tests/evaluation/`. | Some need the full stack (P8-P10); `tests/evaluation/scorer.py` itself is a pure unit of the frozen `domain.retrieval` DTOs and needs nothing. |
| `slow` | Long-running (LLM) tests. Excluded from the default fast loop. | Usually yes |

Run one marker at a time with `-m <marker>`, combine with `and`/`or`/`not` (see `scripts/test.ps1 -Unit`
above for an example). `--collect-only -q` never touches a service and is the fastest sanity check
that the suite still imports cleanly.

## Nothing running? Still green.

Every fixture that needs a service is built on a **session-cached availability probe**
(`tests/conftest.py`: `postgres_available`, `neo4j_available`, `ollama_available`,
`embedding_available`, `memory_api_available`, `mcp_available`). Each does one ~2 s reachability
check, then either returns `True` once for the whole session or calls `pytest.skip(...)` with a
specific reason (never a bare connection-refused traceback). `db_engine`/`pg_session` additionally
skip - rather than error - when Postgres is reachable but the schema has not been migrated yet.

`memory_api_available` / `mcp_available` will always skip before P10/P11 (`apps/memory-api`,
`apps/mcp-server` do not exist yet) - that is expected, not a bug in the probe.

## Fixtures worth knowing about (`tests/conftest.py`)

* `tmp_source_root` — a disposable, mutable copy of `tests/fixtures/mini-vault` under pytest's own
  `tmp_path`. Use `.write/.modify/.touch/.delete/.move/.duplicate(relative_path, ...)` between two
  simulated ingestion runs to exercise change detection. **Never** touches `D:\My-Vault` or the pilot
  repo — it only ever copies the tiny fixture already committed to this repo.
* `fixed_now` — a fixed UTC `datetime` for temporal/provenance assertions. Pass it explicitly
  (`observed_at=fixed_now`); see the fixture's docstring for why this project does not monkeypatch an
  ambient clock.
* `uuid_factory("label")` — the same UUID every time for a given label, for readable fixed-id
  assertions instead of threading a random `uuid4()` through several fixtures.
* `pg_session` — a `Session` whose outer transaction is always rolled back at teardown; nothing a test
  writes survives it. `graph_store` — a live `Neo4jGraphStore`; tests clean up their own nodes by id.

## Directory map

```
tests/
  conftest.py            availability probes, connection fixtures, tmp_source_root, determinism (A12)
  fixtures/
    mini-vault/           10-note fixture vault incl. the architecture-decision-a/b supersession pair (A07b)
    mini-repo/             tiny code repo fixture for the code chunker/extractor (A07b)
    adversarial/           secrets, binary-disguised-as-md, oversized (generated at test time), BOM,
                            CRLF, unicode filename, nested-headings-with-fence (A07b, see its README.md)
  unit/                   no services needed
  integration/            @pytest.mark.integration - real Postgres/Neo4j
  memory/                 @pytest.mark.memory - change-detection scenarios (plan §L/§Y)
  evaluation/             @pytest.mark.evaluation - gold.yaml, scorer.py, run_eval.py, benchmark/,
                          local_ai/ (A05's Graphiti-gate/Qwen3 measurement harness)
```

## Evaluation and the ongoing benchmark (ADR-0010, ADR-0012)

* `tests/evaluation/gold.yaml` — ~25 retrieval questions. Every question is tagged `author: owner` or
  `author: agent`; **agent-authored items are a draft for the owner to correct**, not a finished gold
  set.
* `tests/evaluation/scorer.py` — hit@5, expected-entity presence, provenance completeness (must be
  100 % on returned evidence), temporal correctness, computed against the frozen DTOs in
  `packages/aimemory/domain/retrieval.py`. Pure functions; unit-testable today with synthetic
  `ScoredHit`/`SearchResult` objects, before retrieval (P9) exists.
* `tests/evaluation/benchmark/` — the frozen gate episodes + Architecture-A/B fixture with hand-listed
  expectations (ADR-0010). **Never edited after being frozen** (P14-T05); this is what
  `scripts/eval --benchmark --model <name>` compares every candidate model against.
* `tests/evaluation/local_ai/` (A05) — the Graphiti-gate and Qwen3 measurement harness; separate from
  the gold-set evaluation above.

## Adding a scenario test before its feature exists

See `tests/memory/test_change_detection.py` for the pattern: `pytest.skip("awaiting <task>")` as the
*first* line of the test body, followed by the full arrange/act/assert the test will run once the skip
is deleted. Do not import not-yet-existing modules at module scope (it breaks collection for every
other test in the file); a local `import` after the `pytest.skip()` call is fine, since Python never
executes past the skip.
