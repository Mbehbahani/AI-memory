"""Generates an oversized text file at test time - deliberately **not** committed as a multi-MB blob.

Used by ``tests/unit/test_policies.py`` to prove the > 2 MB -> ``CATALOG_ONLY`` rule (plan section K)
without bloating the git repository with a static fixture file.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_SIZE_BYTES = 3 * 1024 * 1024  # 3 MB - safely over the 2 MB `max_index_bytes` threshold.


def write_oversized_markdown(path: Path, size_bytes: int = DEFAULT_SIZE_BYTES) -> Path:
    """Write a syntactically boring but valid markdown file of at least ``size_bytes``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = "This line exists only to pad the fixture file past the size threshold.\n"
    repeats = size_bytes // len(line) + 1
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Oversized fixture\n\n")
        for _ in range(repeats):
            handle.write(line)
    return path
