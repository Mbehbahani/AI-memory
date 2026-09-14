"""Line-window code chunker (plan section L / P6-T02).

Targets ``TARGET_LINES`` (~60) lines per chunk with ``OVERLAP_LINES`` lines of overlap between
consecutive chunks, additionally bounded by ``max_tokens`` for very dense/long lines (minified JS,
long SQL). The chunk boundary is snapped, when cheap, to a nearby blank line or a top-level
``def``/``class``/``function``/``export`` line (no leading whitespace) so a chunk does not open or
close mid-function whenever a nearby better cut point exists within ``SNAP_WINDOW`` lines - this is a
heuristic, not a parser, and deliberately stays cheap (no AST).

``heading_path`` carries the line range as ``["<relative_path>:<start>-<end>"]`` (1-indexed, inclusive)
so a citation points a reader straight at the lines in an editor, e.g. ``src/module.py:120-180``.
"""

from __future__ import annotations

import re

from aimemory.common.hashing import text_hash
from aimemory.domain.ports import ChunkDraft

from .tokens import count_tokens

__all__ = ["CodeChunker", "chunk_code_text"]

TARGET_LINES = 60
OVERLAP_LINES = 8
SNAP_WINDOW = 10
DEFAULT_MAX_TOKENS = 200

_TOP_LEVEL_DEF_RE = re.compile(
    r"^(def |class |async def |function |export function |export default |export class |"
    r"export const |export async function |CREATE TABLE|CREATE OR REPLACE|--\s*$)",
)


def _is_boundary_line(line: str) -> bool:
    """A blank line, or a line starting a top-level definition (no leading whitespace)."""
    if line.strip() == "":
        return True
    if line[:1].isspace():
        return False
    return bool(_TOP_LEVEL_DEF_RE.match(line))


def _snap(end: int, n: int, boundaries: list[int], min_end: int) -> int:
    """Move ``end`` to the nearest boundary line index within ``SNAP_WINDOW``, if one exists.

    Prefers a boundary at or before ``end`` (so the chunk does not grow past its token budget just to
    reach a boundary); falls back to one shortly after. Never moves below ``min_end`` (the chunk must
    contain at least one line) or above ``n``.
    """
    if end >= n:
        return n
    lo = max(min_end, end - SNAP_WINDOW)
    candidates_before = [b for b in boundaries if lo <= b <= end]
    if candidates_before:
        return candidates_before[-1] if candidates_before[-1] > min_end else end
    hi = min(n, end + SNAP_WINDOW)
    candidates_after = [b for b in boundaries if end < b <= hi]
    if candidates_after:
        return candidates_after[0]
    return end


def chunk_code_text(
    text: str,
    *,
    relative_path: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    target_lines: int = TARGET_LINES,
    overlap_lines: int = OVERLAP_LINES,
) -> list[ChunkDraft]:
    """Chunk source code by line windows. See module docstring."""
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    n = len(lines)
    offsets = [0] * (n + 1)
    for i, line in enumerate(lines):
        offsets[i + 1] = offsets[i] + len(line)

    boundaries = [i for i in range(n) if _is_boundary_line(lines[i])]

    chunks: list[ChunkDraft] = []
    start = 0
    while start < n:
        end = min(start + target_lines, n)
        # Token budget can shrink the window further before we try to snap to a boundary.
        while end > start + 1 and count_tokens("".join(lines[start:end])) > max_tokens:
            end -= 1
        pre_snap_end = end
        end = _snap(end, n, boundaries, start + 1)
        # Boundary-snapping is a soft preference, not allowed to reintroduce a large token-budget
        # overshoot: if the snapped window blows well past the budget, keep the token-fitted window.
        if count_tokens("".join(lines[start:end])) > max_tokens * 1.5:
            end = pre_snap_end
        end = max(end, start + 1)
        chunk_lines = lines[start:end]
        piece = "".join(chunk_lines)
        stripped = piece.strip()
        if stripped:
            # ChunkDraft (DomainModel, frozen) sets str_strip_whitespace=True, so ``text`` can never
            # itself carry leading/trailing whitespace. Shrink the offsets by exactly the trimmed
            # amount so ``chunk.text == text[chunk.char_start:chunk.char_end]`` holds exactly, and any
            # gap between chunks' offsets is always whitespace-only (see chunking/markdown.py for the
            # same reasoning). ``heading_path`` still reports the raw ``start+1``-``end`` line range,
            # since those line numbers are what a citation should point an editor at.
            lead = len(piece) - len(piece.lstrip())
            trail = len(piece) - len(piece.rstrip())
            chunks.append(
                ChunkDraft(
                    ordinal=len(chunks),
                    text=stripped,
                    text_hash=text_hash(stripped),
                    heading_path=[f"{relative_path}:{start + 1}-{end}"],
                    char_start=offsets[start] + lead,
                    char_end=offsets[end] - trail,
                    token_count=count_tokens(stripped),
                )
            )
        if end >= n:
            break
        start = max(end - overlap_lines, start + 1)
    return chunks


class CodeChunker:
    """:class:`~aimemory.domain.ports.Chunker` implementation for source code."""

    name = "code"

    def chunk(
        self, text: str, *, relative_path: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> list[ChunkDraft]:
        return chunk_code_text(text, relative_path=relative_path, max_tokens=max_tokens)
