"""Plain-text extractor: ``.txt``, ``.rst`` (plan section K index_content list)."""

from __future__ import annotations

from aimemory.domain.ports import ExtractedText

from ._util import decode_text, finalize_text, looks_binary, truncate_to_bytes

__all__ = ["PlainTextExtractor"]

_EXTENSIONS = frozenset({".txt", ".rst"})


class PlainTextExtractor:
    name = "plaintext"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in _EXTENSIONS

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        if looks_binary(data):
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                ok=False,
                reason="binary content detected in a plain-text file",
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
            media_type="text/plain",
            char_count=char_count,
            truncated=truncated,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
