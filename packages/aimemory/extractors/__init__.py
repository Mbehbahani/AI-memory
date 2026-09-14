"""``aimemory.extractors`` - :class:`~aimemory.domain.ports.TextExtractor` implementations (A07b).

Import :data:`DEFAULT_EXTRACTORS` or call :func:`get_extractor` to pick the right implementation for a
path; A07a's ingestion pipeline is the intended caller. Every extractor returns
:class:`~aimemory.domain.ports.ExtractedText` and never raises for a malformed *content* file (a
programming bug in the extractor itself is not "malformed content" and is allowed to raise).
"""

from __future__ import annotations

from aimemory.domain.ports import TextExtractor

from .code import CodeExtractor
from .docx import DocxExtractor
from .markdown import MarkdownExtractor
from .notebook import IpynbExtractor
from .pdf import PdfExtractor
from .plaintext import PlainTextExtractor
from .structured import StructuredExtractor
from .tex import TexExtractor

__all__ = [
    "CodeExtractor",
    "DEFAULT_EXTRACTORS",
    "DocxExtractor",
    "IpynbExtractor",
    "MarkdownExtractor",
    "PdfExtractor",
    "PlainTextExtractor",
    "StructuredExtractor",
    "TexExtractor",
    "get_extractor",
]

# Order matters only in that every extractor here claims a disjoint set of extensions (checked by
# tests/unit/test_extractors.py), so the first match is also the only match.
DEFAULT_EXTRACTORS: tuple[TextExtractor, ...] = (
    MarkdownExtractor(),
    PlainTextExtractor(),
    CodeExtractor(),
    StructuredExtractor(),
    TexExtractor(),
    IpynbExtractor(),
    PdfExtractor(),
    DocxExtractor(),
)


def get_extractor(
    relative_path: str,
    media_type: str | None = None,
    *,
    extractors: tuple[TextExtractor, ...] = DEFAULT_EXTRACTORS,
) -> TextExtractor | None:
    """Return the extractor that claims ``relative_path``, or ``None`` if nothing does (the caller
    should then leave the source at whatever policy :mod:`aimemory.sources.policies` already gave it,
    typically ``CATALOG_ONLY``)."""
    for extractor in extractors:
        if extractor.supports(relative_path, media_type):
            return extractor
    return None
