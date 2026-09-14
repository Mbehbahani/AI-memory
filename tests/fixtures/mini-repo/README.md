# mini-repo fixture

A tiny source-code repository used by unit tests for the code extractor/chunker and the ignore/policy
resolver's directory-pruning behaviour. Layout:

- `README.md` - this file (INDEX_CONTENT via `policies.yaml: index_content.filenames`).
- `src/app.py`, `src/utils.py`, `src/models.py` - three Python modules (INDEX_CONTENT, `.py`).
- `config/settings.toml` - one bounded structured-config file (INDEX_CONTENT, `< 200KB`).
- `node_modules/left-pad/index.js` - a dependency-tree decoy; must be pruned/IGNORE'd by the built-in
  deny list and `.memoryignore`, never walked or indexed.
- `data/events.csv` - one CSV (CATALOG_ONLY per `policies.yaml: catalog_only.extensions`), large enough
  to also exercise the same code path a real dataset export would.
