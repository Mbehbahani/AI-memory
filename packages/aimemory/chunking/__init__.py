"""``aimemory.chunking`` - :class:`~aimemory.domain.ports.Chunker` implementations (A07b).

:class:`~aimemory.chunking.markdown.MarkdownChunker` for prose, :class:`~aimemory.chunking.code.
CodeChunker` for source code; :func:`get_chunker` picks by ``extractor`` name (the value written to
``source_text.extractor``) so A07a does not need to know the extension-to-chunker mapping itself.
"""

from __future__ import annotations

from aimemory.domain.ports import Chunker

from .code import CodeChunker
from .markdown import MarkdownChunker
from .tokens import count_tokens

__all__ = ["CodeChunker", "MarkdownChunker", "count_tokens", "get_chunker"]

_MARKDOWN_CHUNKER = MarkdownChunker()
_CODE_CHUNKER = CodeChunker()

# Keyed by SourceText.extractor value (see aimemory.domain.models.SourceText docstring).
_BY_EXTRACTOR: dict[str, Chunker] = {
    "markdown": _MARKDOWN_CHUNKER,
    "code": _CODE_CHUNKER,
    "ipynb": _CODE_CHUNKER,
    "plaintext": _MARKDOWN_CHUNKER,
    "pdf": _MARKDOWN_CHUNKER,
    "docx": _MARKDOWN_CHUNKER,
}


def get_chunker(extractor_name: str) -> Chunker:
    """Chunker for a given ``source_text.extractor`` value. Falls back to the markdown chunker (plain
    prose chunking, no fence/heading assumptions needed since prose without headings just becomes one
    section) for anything unrecognized, so a new extractor added later never crashes the pipeline for
    lack of a chunker.
    """
    return _BY_EXTRACTOR.get(extractor_name, _MARKDOWN_CHUNKER)
