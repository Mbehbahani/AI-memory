# Shared Docker bits

Created by A03 in P2-T02: `base.Dockerfile` (python:3.12-slim, non-root `app` user, `packages/aimemory`
installed editable from the lock file), `tools.Dockerfile` (base + dev/test extras, used by the
`tools` compose profile to run pytest against the live stack), `healthcheck.py`, and entrypoint scripts.
