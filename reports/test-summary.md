# Test Summary

Development started 2026-09-14. Test runs are recorded here verbatim as they happen.

| Run | Phase | Command | Passed | Failed | Skipped | Failures (verbatim) | Fix task |
|---|---|---|---|---|---|---|---|
| 1 | P1-T01 | `python -m pytest tests/unit -q` (agent A02, host `.venv-contracts`, Python 3.13.14) | 157 | 0 | 1 | none — raw tail quoted verbatim: `SKIPPED [1] tests\unit\test_contracts_uri.py:225: POSIX symlink creation / 157 passed, 1 skipped in 0.60s` | n/a |
| 2 | P5/P6 (partial) | not available — reported in the `606d8a3` commit message only, not independently re-run or logged by A01 at the time | 342 | 1 | 1 | Verbatim from the commit message: "the failure is A07b's own real-vault bounds test — a LinkedIn tracking-URL blob yields a 264-token chunk against a 250-token assertion." (test not individually re-quoted here because A01 does not have the raw pytest output, only the commit's paraphrase — flagged as a warning, not fabricated as a literal pytest line) | fixed in `8e495ca` |
| 3 | ADR-0012 (n=1 smoke) | not available — commit `96b751a` states "Test state unchanged: 342 passed, 1 failed (A07b's in-flight bounds test), 1 skipped" | 342 | 1 | 1 | same failure as Run 2, still open at this commit | fixed in `8e495ca` |
| 4 | 2026-09-15, full suite (MEASURED, authoritative at the time) | `docker compose --profile tools run --rm tools pytest -q` (run by the orchestrator, in the `tools` image) | **428** | **0** | **1** | The one skip is `tests/unit/test_contracts_uri.py:206` — Windows-specific junction reparse points, a platform skip, not a failure. No failures. | n/a |
| 5 | P9-T01 | commit `ef30037` message only, not independently re-run or logged live by A01 | 518 | 0 | 2 | Not quoted verbatim — no raw output seen by A01, only the commit's own summary line: "518 passed, 2 skipped (+90 tests)". A dedicated e2e smoke test skips honestly (no Tier-1 corpus yet at this commit). | n/a |
| 6 | P10-T01/T02 | commit `43d8a2b` message only, not independently re-run or logged live by A01 | 107 | 0 | not stated | Not quoted verbatim — commit's own summary line: "107 passed in the retrieval/gateway subset, run without the destructive migration test." This is a subset run, not the full suite. | n/a |
| 7 | P2-T04 corpus-safety fix | commit `670f516` message only, not independently re-run or logged live by A01 | 627 | 0 | 1 | Not quoted verbatim — commit's own summary line: "MEASURED: full suite 627 passed, 1 skipped." Corpus counts stated as identical before/after (176/2,838/2,779/27/144). | n/a (this commit is itself the fix — see fix log) |
| 8 | P8-T02 wiring fix | commit `c462170` message only, not independently re-run or logged live by A01 | 627 | 0 | 1 | Not quoted verbatim — commit's own summary line: "MEASURED: 627 passed, 1 skipped, corpus unchanged across the run." | n/a |
| 9 | 2026-09-17, full suite (**MEASURED, authoritative — supersedes Run 4**) | `docker compose --profile tools run --rm tools pytest -q` (run by the orchestrator, in the `tools` image) | **627** | **0** | **1** | The one skip is the Windows-junction test (`tests/unit/test_contracts_uri.py`), same platform skip as Run 4. The former "no Tier-1 corpus" skip present at Run 4/5 is **gone** — a real Tier-1 corpus now exists (P7-T02, 2,838 chunks). No failures. | n/a |

## Fix log (from commit messages and diffs, quoted where the raw command output is not available)

- **P6-T02 chunking bounds bug** (Run 2/3 failure → resolved by Run 4). Root cause per `8e495ca`'s diff comment: the hard-split slice size for long unbroken runs (e.g. a LinkedIn tracking URL with no whitespace) was computed once from the *full* `max_tokens` budget (`chars_for_tokens(self.max_tokens)`), rather than from the buffer's *remaining* headroom — so a hard split could land on top of already-buffered content (a heading, a short sentence) and add up to another full `max_tokens` worth of tokens before the size check in `maybe_split` fired. This is exactly how a 200-token target chunk grew to 264 tokens against the test's 250-token assertion. Fix (`packages/aimemory/chunking/markdown.py`): the slice size is now recomputed before every cut as `max(1, chars_for_tokens(max(1, self.max_tokens - count_tokens(self.buf))))`, bounding overshoot to token/char rounding error. Fixed in commit `8e495ca` (bundled with the P5-R01 review and ADR-0013 in the same commit; not its own commit).
- **`test_modified_file_creates_new_version_and_unconfirms_old_facts`** (tests/memory, P6-T04). Failure mode per `2c87f86`'s commit message: after the extractor strips frontmatter, the fixture body became a single chunk, so appending a bare sentence rewrote the only chunk there was and no `text_hash` could survive — the embedding reuse the scenario exists to prove was unobservable. Fix: the test edit now appends a new `## Status update` section, which places a chunk boundary, so the original section survives byte-identical. Fixed in `2c87f86`.
- **`test_allow_model_mix_is_the_only_way_past_the_guard`** (tests/memory, P6-T04). Failure mode per `2c87f86`'s commit message: `record_run_extraction_model` writes one `metrics_snapshots` row per `(run, model)`, so the run holds two rows; the test's unordered `.first()` picked generation 1's row. Fix: the test now asserts both rows and their `allow_model_mix` flags, and — this is the substantive part — **the closing assertion was corrected to match what the system actually does, not loosened to hide it**: generation 2 proposed facts identical to the open ones, so ADR-0005 re-confirms them via `FactRepo.touch` without restamping provenance, meaning `extraction_models_in_use` reports **one** model for a corpus **two** models actually ran over. This is recorded in the test itself as a known limitation, not papered over. Fixed (test corrected) in `2c87f86`.

- **Test suite destroyed the ingested vault corpus** (found while reaching Run 7/9). `test_migrations.py` ran `alembic downgrade base` against the shared dev database — documented as "Destructive by design", and CLAUDE.md carves out an exception for exactly that command, so the test was never wrong; but once a real corpus existed (after P7-T02), a plain `pytest -q` dropped it. **Two agents independently lost the vault corpus to this**; one captured the evidence — the `sources` table's OID changing across a run while `pg_stat_user_tables.n_tup_del` stayed 0, i.e. a DROP + CREATE, not a DELETE. Fix (`packages/aimemory/tests/integration/test_migrations.py`, commit `670f516`): the whole module now runs against a throwaway database created per module and dropped at teardown, so the P5-T01 acceptance still runs by default. A second bug surfaced while verifying: the scratch-database fixture yielded `str(url)`, and SQLAlchemy's `URL.__str__()` masks the password as `***`, so every test using that DSN failed authentication — fixed by rendering with `hide_password=False`.
- **`test_claim_next_and_mark_failed`** (tests/unit/test_persistence_repositories.py, found alongside the fix above). Assumed `EpisodeRepo.claim_next()` would return the episode it created, but the claim is global over the queue — real pending vault episodes outranked the fixture, so the test failed against real data while passing in isolation, exactly backwards. Fix: the test now moves competing episodes aside inside its own transaction, which `pg_session` rolls back; production behaviour untouched. Fixed in `670f516`.
- **Markdown chunker fence-swallowing bug** (MEASURED during the first real vault ingest, `37b90da`; no test was failing in CI, since no regression fixture existed for this shape until it did). A fence-open transition appended to the buffer without flushing it, so prose sitting just under `max_tokens` absorbed an entire fenced block before any size check could fire — a real note produced a single 28,446-character (~8,366-token) chunk against a 200-token target. Fix (`packages/aimemory/chunking/markdown.py`, commit `78561f3`): fence open/close are now hard boundaries like an ATX heading, unconditional and deliberately not size-gated. New regression test: `tests/unit/test_chunking_oversized.py`.
- **Code chunker single-line-over-budget bug** (MEASURED during the same real vault ingest). The line window could never shrink below one whole physical line, so a one-line 12,623-character `.json` file became a single chunk, rejected by the embedding service with `422 texts[0] exceeds 8000 characters`. Fix (`packages/aimemory/chunking/code.py`, commit `78561f3`): a single line over budget is now split within the line, preferring the last whitespace inside the budget and cutting mid-token only when there is none. MEASURED after the fix: the one-line JSON shape goes from 1 chunk to 27 (max 200 tokens/680 chars). **Remaining limitation, by design**: a standalone fence larger than the budget is still one oversized chunk (module rule 1) — measured at 10,191 chars, still above the embedder's 8,000-char limit, so the pipeline's hard-split backstop still carries it.
- **`aimemory-ingest tier2` reported "not built yet" though the engine was built** (found while trying to run P8-T04). `packages/aimemory/knowledge/__init__.py` was a bare docstring, so `from ..knowledge import get_engine` raised `ImportError`, swallowed by a broad `except` into a misleading message. Fix (commit `c462170`): added `get_engine`/`get_writer` factories with lazy imports (avoids an import cycle through `providers.llm` → config). Second gap found while fixing the first: `run_tier2` persisted only `if writer is not None` and every CLI path passed `None` — an engine with no writer would have called the LLM for every episode and discarded every result. `run_tier2` now refuses to run without a writer. Also rescoped both `tests/memory` scenarios asserting the ADR-0014 one-model-per-corpus guard to the fixture root, since with real vault facts present they were asserting over the whole (now non-empty) corpus — same class of bug as `test_claim_next_and_mark_failed` above.
- **`memory-api` restart loop, root cause found** (P10-T02, commit `43d8a2b`). Placeholder `CMD uvicorn apps.memory_api.main:app` could never import — the directory is `apps/memory-api` and a hyphen cannot appear in a Python module path. Fixed the way `embedding-service` already does it. `memory-api` reaches healthy for the first time.
- **`docker compose up -d` image build-context bug** (P3-T01, commit `3b80de9`). `apps/ingestion/Dockerfile` never `COPY`ed `infra/postgres/alembic` or `infra/neo4j/schema`, so `cli/migrate.py` — whose path logic was already correct — could never find `alembic.ini`, failing every `docker compose up -d` with `"No 'script_location' key found in configuration"`. Fixed by copying both trees into the image.
- **`docs/architecture/retrieval.md` §2 contained invalid SQL** (found verifying P9-T01, fixed in `ff19689`). `FROM chunks c, plainto_tsquery(...) q JOIN sources s ON s.id = c.source_id` binds the JOIN to the tsquery relation, putting `c` out of scope in its `ON` clause — confirmed against the live database: `"invalid reference to FROM-clause entry for table c"`. The shipped code was correct; only the doc was wrong (also fixed: §1's `since` filter, which used ingestion time instead of the observation axis, contradicting §5 and `temporal.md`).

## Known limitation (open — not a test failure, recorded here so it is not lost)

**`extraction_models_in_use` under-reports after `--allow-model-mix`.** When a Tier 2 run with
`--allow-model-mix` proposes facts identical to already-open ones from a prior run under a different
model, ADR-0005 re-confirmation (`FactRepo.touch`) re-stamps `valid_to`/confirmation bookkeeping but
**does not restamp `extraction_model_id`**. Consequently `extraction_models_in_use` — and the
`aimemory-ingest status` warning built on top of it — reports a single model for a corpus that two
models have actually run over. The per-`(run, model)` `metrics_snapshots` rows are the one place that
does correctly show both models were involved. This is a real gap between what the pipeline was
supposed to guarantee (ADR-0014 rule 2, the mismatch guard) and what it currently reports; closing it
is an ADR decision for **A04/A08** (whether `FactRepo.touch` should restamp `extraction_model_id`, and
what that means for provenance of a re-confirmed-not-re-extracted fact). Surfaced and documented,
not fixed, in `2c87f86`.

## Known limitations found during the 2026-09-17 reconciliation pass (open, not test failures)

- **Neo4j holds 0 nodes.** All knowledge is Postgres-only at HEAD. Graph expansion (P10-T01, built
  and tested with a simulated-outage degraded mode) has nothing to expand on real data, and the
  `entity_linked` boost cannot fire. `aimemory-ingest rebuild-graph` fails outright: the CLI imports
  `knowledge.structural.rebuild_graph`, which does not exist — `structural.py` exports
  `StructuralIndex`, `structural_plan`, and `document_node_id` but no driver function to run the
  projection and write to Neo4j. This is P7-T03, still open.
- **0 of 81 P8-T04-pilot artifacts carry an evidence quote.**
- **`extraction_models_in_use` under-reports after `--allow-model-mix`** — carried over from the
  previous reconciliation note above; still an open ADR decision for A04/A08.
- **`embeddings` has `UNIQUE(text_hash, model_id)`** while the semantic retrieval SQL joins
  `chunks c ON c.id = e.object_id` — a chunk whose text duplicates another is invisible to *semantic*
  retrieval (keyword search still finds it, since it queries `chunks` directly). Raised by A09 during
  P9-T01; needs an explicit accept-or-fix decision.
- **P8-T04 is 15/144** episodes (deliberately scoped pilot; see `reports/build-timeline.md`). P13
  (`joblab-de` pilot ingest) not started. P11, P12, P14–P17 not started.

## Notes

- The single skip (`test_contracts_uri.py`) has moved line numbers across commits (225 in the P1-T01
  run → 206 at HEAD) as the file grew; both are the same POSIX-only symlink-creation test, skipped on
  Windows — not a failure.
- `ruff check --fix` → all checks passed at P1-T01. `mypy` → "Success: no issues found in 18 source files"
  at P1-T01. Neither has been independently re-run by A01 since; not re-asserted here.
- Runs 4 and 9 (428 passed/1 skipped, then 627 passed/1 skipped) are the only runs in this table that
  A01 treats as independently MEASURED rather than reported secondhand from a commit message — both
  were run by the orchestrator directly and relayed to A01 verbatim. Run 9 supersedes Run 4 as the
  authoritative current test state. Runs 2, 3, 5, 6, 7, 8 are recorded because commit messages state
  them, but A01 has not seen their raw pytest output and flags this as a warning rather than presenting
  them as independently verified.
- P7-T01/T02 (Tier 0/1 vault ingestion) are now MEASURED complete against the real vault (see
  `reports/build-timeline.md`, commit `37b90da`) — a genuine Tier-1 corpus exists (176 sources, 2,838
  chunks, 2,779 embeddings), which is why the smoke-test skip present at Run 4/5 is gone by Run 9.
  P7-T03 (structural graph projection) and P8-T04 (Tier 2 extraction, 15/144 done) remain incomplete —
  see the known limitations above. No gold-set evaluation has run yet — that is P13/P14 scope.
