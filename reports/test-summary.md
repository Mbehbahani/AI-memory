# Test Summary

Development started 2026-09-14. Test runs are recorded here verbatim as they happen.

| Run | Phase | Command | Passed | Failed | Skipped | Failures (verbatim) | Fix task |
|---|---|---|---|---|---|---|---|
| 1 | P1-T01 | `python -m pytest tests/unit -q` (agent A02, host `.venv-contracts`, Python 3.13.14) | 157 | 0 | 1 | none — raw tail quoted verbatim: `SKIPPED [1] tests\unit\test_contracts_uri.py:225: POSIX symlink creation / 157 passed, 1 skipped in 0.60s` | n/a |

## Notes

- The single skip (`test_contracts_uri.py:225`) is a POSIX-only symlink-creation test skipped on Windows; not a failure.
- `ruff check --fix` → all checks passed. `mypy` → "Success: no issues found in 18 source files." (not pytest, recorded here for completeness since it was part of A02's P1-T01 verification.)
- **Warning**: this run happened in a host-side virtualenv (`.venv-contracts`, Python 3.13.14) because the host's default Python (3.14/3.13) does not satisfy `pyproject.toml`'s `>=3.12,<3.13` pin, so `pip install -e ".[dev]"` could not run directly against the pinned toolchain. A02 used `--ignore-requires-python`. This has **not** yet been re-run inside the `tools` container image (that image does not exist until P2-T02). A03/A12 must re-run `pytest tests/unit` in-container and this table will be updated with that result.
- No integration, MCP, memory-scenario, or evaluation tests have run yet (none of those phases have started).
