# adversarial fixtures

Built by **A07b** as part of P6-T01 (the task assigned to A12 for this directory was interrupted
before any files were written - if you are A12 picking this up, this corpus already exists; extend it
rather than recreating it, and see "Ownership" below).

Every fixture here must resolve to a **safe** outcome through
`aimemory.sources.{ignore,policies,secrets}` - see `tests/unit/test_policies.py`,
`test_secrets.py`, `test_ignore.py` and `test_extractors.py` for the assertions. Summary:

| Fixture | Resolved policy | Why |
|---|---|---|
| `fake-secrets/.env` | `CATALOG_ONLY` (`secret_suspected=true`) | filename `.env` + content matches `aws_access_key`/`conn_string_pw`/`github_token` |
| `fake-secrets/id_rsa` | `CATALOG_ONLY` (`secret_suspected=true`) | filename `id_rsa*` + content matches `private_key` |
| `fake-secrets/jwt-in-note.md` | `CATALOG_ONLY` (`secret_suspected=true`) | innocuous filename, content matches `jwt` |
| `binary-disguised-as-md.md` | extractor returns `ok=False` | binary sniff (`looks_binary`) catches it before any text is stored |
| `empty-file.md` | `INDEX_CONTENT`, extractor returns empty text | zero bytes is not an error, just empty |
| `bom-file.md` | `INDEX_CONTENT`, decodes cleanly | BOM-aware decode strips the mark |
| `crlf-file.md` | `INDEX_CONTENT`, decodes cleanly | CRLF is preserved in extracted text; `text_hash` normalizes it |
| `Ünïcödé résumé — 测试 report.md` | `INDEX_CONTENT` | non-ASCII, spaces and an em dash in the filename do not break path handling |
| `nested-headings-with-fence.md` | `INDEX_CONTENT`, chunked correctly | H1-H6 nesting plus a fenced block containing `#`/`##`/`###` lines that must never be read as real headings |
| `oversized/generate.py` (helper) | file it generates -> `CATALOG_ONLY` | > `max_index_bytes` (2 MB); generated at test time, not committed |
| path traversal (see below) | rejected before any I/O | see "Path traversal" |
| Windows junction (see below) | rejected / not descended | see "Windows junction" |

## Path traversal

A real file whose *name* contains `..` cannot be created inside this directory without literally
writing outside the repository - git (and this filesystem) will not let a checked-in path escape its
parent directory. The traversal case is therefore tested as a **string**, not a fixture file:
`tests/unit/test_policies.py` calls `resolve_policy("../../escape.md", ...)` directly and asserts it
resolves to `IGNORE` with rule `builtin_deny:path_traversal` - `aimemory.sources.policies` refuses any
relative path containing a `..` segment before it looks at anything else. This is defence in depth
alongside A07a's own path guard (`aimemory.common.errors.PathGuardError`, P6-T03), which additionally
resolves the real filesystem path and confirms it stays under its declared root.

## Windows junction

A junction (`mklink /J`) is a real filesystem reparse point A07a's discovery walker must refuse to
follow (plan section T: no symlink/junction escape). It is not committed as a fixture (junctions are
not portable across checkouts and `git` cannot represent one), and creating one requires either
Developer Mode or an elevated shell on Windows, which cannot be assumed for every future test run - the
existing contract test `tests/unit/test_contracts_uri.py::test_*junction*` already documents this and
`pytest.skip`s when creation is not permitted. Any test written for A07b or A07a's own junction
handling should follow that same pattern: attempt `os.symlink`/`mklink /J` in a temp directory inside
`setup`, and `pytest.skip("junction-style reparse points need elevation/Developer Mode on this host")`
on `OSError`/`PermissionError` rather than failing the suite on a machine without the right privilege.

## Ownership

`tests/fixtures/adversarial/**` is owned by **A12** per the agent matrix, but A07b (this task, P6-T01)
was told to build it if A12's own attempt had not yet produced anything, which was the case. If you are
A12: this corpus satisfies P6-T01's acceptance criterion ("every adversarial fixture resolves safely");
feel free to extend it for P6-T04's memory-scenario suite, but please do not delete or restructure the
existing files without checking `tests/unit/test_policies.py`, `test_secrets.py`, `test_extractors.py`
and `test_chunking_markdown.py`, which all read from here by relative path.
