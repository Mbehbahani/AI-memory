"""Source-code extractor: text + language, for every ``index_content`` code extension except TeX/BibTeX
(handled by :mod:`aimemory.extractors.tex`) and structured-config formats (handled by
:mod:`aimemory.extractors.structured`).

One class covers the whole family (``extractor_version``/``name`` are the same for every language);
``media_type`` records which language was detected so downstream consumers (A08's ``doc_kind``
classifier, citations) can distinguish a Python file from a shell script without re-deriving it from
the path.
"""

from __future__ import annotations

from aimemory.domain.ports import ExtractedText

from ._util import decode_text, finalize_text, looks_binary, truncate_to_bytes

__all__ = ["CodeExtractor", "LANGUAGE_BY_EXTENSION"]

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".r": "r",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".mjs": "javascript",
    ".svelte": "svelte",
    ".sh": "shell",
    ".ps1": "powershell",
    ".cypher": "cypher",
}


class CodeExtractor:
    name = "code"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in LANGUAGE_BY_EXTENSION

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        language = LANGUAGE_BY_EXTENSION.get(_suffix(relative_path), "text")
        if looks_binary(data):
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                media_type=f"text/x-{language}",
                ok=False,
                reason=f"binary content detected in a '{language}' source file",
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
            media_type=f"text/x-{language}",
            char_count=char_count,
            truncated=truncated,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    # ``.R`` is a distinct convention from ``.r`` on case-sensitive filesystems but plan section K
    # lists both; normalize once here so the language map only needs the lowercase key.
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
