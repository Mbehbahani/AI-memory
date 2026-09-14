"""Jupyter notebook extractor: ``.ipynb`` -> markdown + code cells, outputs dropped (plan section K).

Outputs are dropped unconditionally: they can be large, can contain rendered images as base64, and add
no information a re-run of the notebook wouldn't reproduce - keeping them would bloat ``source_text``
for no retrieval benefit. Raw cells are skipped (rare, and not renderable as either language).
"""

from __future__ import annotations

import json

from aimemory.domain.ports import ExtractedText

from ._util import finalize_text, truncate_to_bytes

__all__ = ["IpynbExtractor"]

_EXTENSIONS = frozenset({".ipynb"})


def _cell_source(cell: dict) -> str:
    source = cell.get("source", "")
    if isinstance(source, list):
        return "".join(source)
    return str(source)


def _notebook_to_text(notebook: dict) -> tuple[str, str]:
    """Returns ``(body, language)``."""
    language = (
        notebook.get("metadata", {}).get("kernelspec", {}).get("language")
        or notebook.get("metadata", {}).get("language_info", {}).get("name")
        or "python"
    )
    parts: list[str] = []
    for cell in notebook.get("cells", ()):
        cell_type = cell.get("cell_type")
        source = _cell_source(cell).strip("\n")
        if not source.strip():
            continue
        if cell_type == "markdown":
            parts.append(source)
        elif cell_type == "code":
            parts.append(f"```{language}\n{source}\n```")
        # "raw" and anything else: skipped, matches "markdown + code cells" in the spec.
    return "\n\n".join(parts), language


class IpynbExtractor:
    name = "ipynb"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in _EXTENSIONS

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        try:
            notebook = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                ok=False,
                reason=f"invalid notebook JSON ({type(exc).__name__})",
            )
        if not isinstance(notebook, dict) or "cells" not in notebook:
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                ok=False,
                reason="notebook JSON has no 'cells' array",
            )
        body, language = _notebook_to_text(notebook)
        truncated = False
        if max_bytes is not None:
            body, truncated = truncate_to_bytes(body, max_bytes)
        stored, char_count = finalize_text(body)
        return ExtractedText(
            text=stored,
            extractor=self.name,
            extractor_version=self.extractor_version,
            media_type=f"application/x-ipynb+{language}",
            char_count=char_count,
            truncated=truncated,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
