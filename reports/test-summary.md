# Test Summary

Development started 2026-09-14. Test runs are recorded here verbatim as they happen.

| Run | Phase | Command | Passed | Failed | Skipped | Failures (verbatim) | Fix task |
|---|---|---|---|---|---|---|---|
| 1 | P1-T01 | `python -m pytest tests/unit -q` (agent A02, host `.venv-contracts`, Python 3.13.14) | 157 | 0 | 1 | none — raw tail quoted verbatim: `SKIPPED [1] tests\unit\test_contracts_uri.py:225: POSIX symlink creation / 157 passed, 1 skipped in 0.60s` | n/a |
| 2 | P5/P6 (partial) | not available — reported in the `606d8a3` commit message only, not independently re-run or logged by A01 at the time | 342 | 1 | 1 | Verbatim from the commit message: "the failure is A07b's own real-vault bounds test — a LinkedIn tracking-URL blob yields a 264-token chunk against a 250-token assertion." (test not individually re-quoted here because A01 does not have the raw pytest output, only the commit's paraphrase — flagged as a warning, not fabricated as a literal pytest line) | fixed in `8e495ca` |
| 3 | ADR-0012 (n=1 smoke) | not available — commit `96b751a` states "Test state unchanged: 342 passed, 1 failed (A07b's in-flight bounds test), 1 skipped" | 342 | 1 | 1 | same failure as Run 2, still open at this commit | fixed in `8e495ca` |
| 4 | 2026-09-15, full suite (**MEASURED, authoritative**) | `docker compose --profile tools run --rm tools pytest -q` (run by the orchestrator, in the `tools` image) | **428** | **0** | **1** | The one skip is `tests/unit/test_contracts_uri.py:206` — Windows-specific junction reparse points, a platform skip, not a failure. No failures. | n/a |

## Fix log (from commit messages and diffs, quoted where the raw command output is not available)

- **P6-T02 chunking bounds bug** (Run 2/3 failure → resolved by Run 4). Root cause per `8e495ca`'s diff comment: the hard-split slice size for long unbroken runs (e.g. a LinkedIn tracking URL with no whitespace) was computed once from the *full* `max_tokens` budget (`chars_for_tokens(self.max_tokens)`), rather than from the buffer's *remaining* headroom — so a hard split could land on top of already-buffered content (a heading, a short sentence) and add up to another full `max_tokens` worth of tokens before the size check in `maybe_split` fired. This is exactly how a 200-token target chunk grew to 264 tokens against the test's 250-token assertion. Fix (`packages/aimemory/chunking/markdown.py`): the slice size is now recomputed before every cut as `max(1, chars_for_tokens(max(1, self.max_tokens - count_tokens(self.buf))))`, bounding overshoot to token/char rounding error. Fixed in commit `8e495ca` (bundled with the P5-R01 review and ADR-0013 in the same commit; not its own commit).
- **`test_modified_file_creates_new_version_and_unconfirms_old_facts`** (tests/memory, P6-T04). Failure mode per `2c87f86`'s commit message: after the extractor strips frontmatter, the fixture body became a single chunk, so appending a bare sentence rewrote the only chunk there was and no `text_hash` could survive — the embedding reuse the scenario exists to prove was unobservable. Fix: the test edit now appends a new `## Status update` section, which places a chunk boundary, so the original section survives byte-identical. Fixed in `2c87f86`.
- **`test_allow_model_mix_is_the_only_way_past_the_guard`** (tests/memory, P6-T04). Failure mode per `2c87f86`'s commit message: `record_run_extraction_model` writes one `metrics_snapshots` row per `(run, model)`, so the run holds two rows; the test's unordered `.first()` picked generation 1's row. Fix: the test now asserts both rows and their `allow_model_mix` flags, and — this is the substantive part — **the closing assertion was corrected to match what the system actually does, not loosened to hide it**: generation 2 proposed facts identical to the open ones, so ADR-0005 re-confirms them via `FactRepo.touch` without restamping provenance, meaning `extraction_models_in_use` reports **one** model for a corpus **two** models actually ran over. This is recorded in the test itself as a known limitation, not papered over. Fixed (test corrected) in `2c87f86`.

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

## Notes

- The single skip (`test_contracts_uri.py`) has moved line numbers across commits (225 in the P1-T01
  run → 206 at HEAD) as the file grew; both are the same POSIX-only symlink-creation test, skipped on
  Windows — not a failure.
- `ruff check --fix` → all checks passed at P1-T01. `mypy` → "Success: no issues found in 18 source files"
  at P1-T01. Neither has been independently re-run by A01 since; not re-asserted here.
- Run 4 (428 passed, 1 skipped) is the only run in this table that A01 treats as independently MEASURED
  rather than reported secondhand from a commit message — it was run by the orchestrator directly and
  relayed to A01 verbatim. Runs 2 and 3 are recorded because the commit messages state them, but A01 has
  not seen their raw pytest output and flags this as a warning rather than presenting them as
  independently verified.
- P7 (Tier 0/1 vault ingestion, structural graph) and P8-T04 (Tier 2 run on my-vault) have supporting
  code and pass their fixture-based unit/scenario tests, but **no evaluation, integration-against-real-vault,
  or gold-set test has run yet** — those are P13/P14 scope and remain outstanding.
