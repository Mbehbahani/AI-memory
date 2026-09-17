"""Export the OpenAPI document to ``schemas/api/openapi.json`` (P10-T02 acceptance). Owner: A09.

Run it from anywhere::

    docker compose --profile tools run --rm tools python apps/memory-api/openapi_export.py

The file is **generated, not hand-written**: it is regenerated whenever a route or a response model
changes, and the check mode (``--check``) fails when the committed copy is stale, so the contract
published to A10 (MCP) and to any future client can never silently drift from the code.

Importing the app must not need a live database: :func:`app.create_app` builds no connection - the
lifespan does, and the lifespan does not run here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "schemas" / "api" / "openapi.json"

if str(APP_DIR) not in sys.path:  # allow `python apps/memory-api/openapi_export.py` from the root
    sys.path.insert(0, str(APP_DIR))


def build_document() -> dict[str, object]:
    from app import create_app

    document = create_app().openapi()
    return dict(document)


def render(document: dict[str, object]) -> str:
    """Stable, diff-friendly JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the memory-api OpenAPI document.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the file on disk differs from what the code would generate.",
    )
    args = parser.parse_args(argv)

    rendered = render(build_document())
    if args.check:
        if not args.output.exists():
            print(f"missing: {args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(f"stale: {args.output} (re-run without --check)", file=sys.stderr)
            return 1
        print(f"up to date: {args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
