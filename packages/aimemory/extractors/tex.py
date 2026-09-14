"""TeX/BibTeX extractor: ``.tex``, ``.bib`` (plan section K index_content list).

Strips unescaped LaTeX line comments (``%`` not preceded by a backslash) before storing the text, so
retrieval and chunking are not full of noise from build metadata comments; nothing else about the
markup is interpreted (macros, math mode, etc. are left as-is - this is a text extractor, not a LaTeX
parser).
"""

from __future__ import annotations

import re

from aimemory.domain.ports import ExtractedText

from ._util import decode_text, finalize_text, looks_binary, truncate_to_bytes

__all__ = ["TexExtractor", "strip_line_comments"]

_EXTENSIONS = frozenset({".tex", ".bib"})
# An unescaped '%' starts a comment; '\%' is a literal percent sign in LaTeX.
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)


def strip_line_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


class TexExtractor:
    name = "code"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in _EXTENSIONS

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        ext = _suffix(relative_path)
        media_type = "text/x-tex" if ext == ".tex" else "text/x-bibtex"
        if looks_binary(data):
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                media_type=media_type,
                ok=False,
                reason="binary content detected in a TeX/BibTeX file",
            )
        text, _encoding, _lossy = decode_text(data)
        text = strip_line_comments(text)
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
