"""Markdown extractor (plan section P6-T02): YAML frontmatter -> dict, ``[[wikilink]]`` and
``[text](url)`` links -> list, remaining body -> text.

The vault is an Obsidian PARA vault (plan section C), so wikilinks are load-bearing: they are the
primary edge source for the Tier 0/1 structural graph (plan section L) before any LLM runs.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from aimemory.domain.ports import ExtractedText

from ._util import decode_text, finalize_text, looks_binary, truncate_to_bytes

__all__ = ["MarkdownExtractor", "extract_links", "split_frontmatter"]

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.DOTALL)
# [[Target]], [[Target|Alias]], [[Target#Heading]], [[Target#Heading|Alias]] - only the target survives.
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)(?:[#|][^\]]*)?\]\]")
# [text](url "title") - the leading (?<!!) excludes image embeds ![alt](url).
_MDLINK_RE = re.compile(r'(?<!!)\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_EXTENSIONS = frozenset({".md", ".markdown"})


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a leading ``---\\n...\\n---`` YAML block off ``text``.

    Malformed YAML (or a non-mapping document) is treated as "no frontmatter" rather than raised -
    the whole file is then kept as body text, per the ``ExtractedText`` contract of degrading rather
    than failing.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, text[match.end() :]


def extract_links(body: str) -> list[str]:
    """Wikilink targets followed by markdown-link hrefs, in document order, de-duplicated."""
    links: list[str] = []
    seen: set[str] = set()
    for pattern in (_WIKILINK_RE, _MDLINK_RE):
        for match in pattern.finditer(body):
            target = match.group(1).strip()
            if target and target not in seen:
                seen.add(target)
                links.append(target)
    return links


class MarkdownExtractor:
    """:class:`~aimemory.domain.ports.TextExtractor` for ``.md``/``.markdown``."""

    name = "markdown"
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
                reason="binary content detected in a file with a markdown extension",
            )
        text, _encoding, _lossy = decode_text(data)
        frontmatter, body = split_frontmatter(text)
        links = extract_links(body)
        truncated = False
        if max_bytes is not None:
            body, truncated = truncate_to_bytes(body, max_bytes)
        stored, char_count = finalize_text(body)
        return ExtractedText(
            text=stored,
            extractor=self.name,
            extractor_version=self.extractor_version,
            media_type="text/markdown",
            char_count=char_count,
            truncated=truncated,
            frontmatter=frontmatter,
            links=links,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
