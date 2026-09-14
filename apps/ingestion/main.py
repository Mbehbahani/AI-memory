"""Entry point of the ``ingestion`` container (A07a, P6-T03).

The service is a thin shell on purpose. Everything importable lives in the installed
``aimemory`` package (``packages/aimemory/sources/*`` for the engine, ``packages/aimemory/cli/ingest.py``
for the command line), because ``apps/`` is copied into the image but is **not** on ``sys.path`` -
the console script ``aimemory-ingest`` declared in ``pyproject.toml`` is what compose actually runs:

* ``aimemory-ingest migrate``  - the one-shot ``migrate`` service
* ``aimemory-ingest worker``   - the always-on ``ingestion`` service (ADR-0011)
* ``docker compose exec ingestion aimemory-ingest run --root vault --tier 1`` - ad-hoc scans

``python apps/ingestion/main.py`` does the same thing for a host-side run during development.
"""

from __future__ import annotations

from aimemory.cli.ingest import main

if __name__ == "__main__":  # pragma: no cover - container/dev entry point
    main()
