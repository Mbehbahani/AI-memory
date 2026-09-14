"""Structured-config extractor: YAML/TOML/JSON/INI (plan section K: "yaml/toml/json < 200 KB").

Content is stored verbatim (no reformatting) so citations line up with the file the owner actually
wrote. The < 200 KB bound is enforced upstream by :mod:`aimemory.sources.policies` (which downgrades
an oversized structured file to ``CATALOG_ONLY`` before any extractor is invoked); this extractor also
respects ``max_bytes`` defensively so it never depends on the caller having done that.

Reported under ``extractor="code"`` (see ``SourceText.extractor`` docstring: markdown | plaintext |
pdf | docx | ipynb | code) with ``media_type`` distinguishing the format - the same convention
:mod:`aimemory.extractors.code` uses for programming languages.
"""

from __future__ import annotations

from aimemory.domain.ports import ExtractedText

from ._util import decode_text, finalize_text, looks_binary, truncate_to_bytes

__all__ = ["StructuredExtractor"]

_MEDIA_TYPE_BY_EXTENSION: dict[str, str] = {
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".json": "application/json",
    ".ini": "text/x-ini",
    ".cfg": "text/x-ini",
}


class StructuredExtractor:
    name = "code"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in _MEDIA_TYPE_BY_EXTENSION

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        media_type = _MEDIA_TYPE_BY_EXTENSION.get(_suffix(relative_path), "text/plain")
        if looks_binary(data):
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                media_type=media_type,
                ok=False,
                reason="binary content detected in a structured-config file",
            )
        text, _encoding, _lossy = decode_text(data)
        truncated = False
        if max_bytes is not None:
            text, truncated = truncate_to_bytes(text, max_bytes)
        stored, char_count = finalize_text(text)
        return ExtractedText(
            text=stored,
            extractor=self.name,
            extractor_version=self.extractor_version,
            media_type=media_type,
            char_count=char_count,
            truncated=truncated,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
