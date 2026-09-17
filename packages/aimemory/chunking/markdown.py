"""Heading-aware markdown chunker (plan section L / P6-T02).

Targets ~200 tokens per chunk (``max_tokens`` default, headroom under MiniLM's 256-token truncation,
plan section N) with a ~30-token overlap between consecutive chunks *within the same section*. Every
chunk carries the full ``heading_path`` trail (``["H1", "H2", "H3"]``) of the section it came from and
exact character offsets into the input string.

Two hard rules, enforced structurally rather than by post-hoc checking:

1. **Never split inside a fenced code block.** Interior fence lines never trigger the size check at
   all - so a single fenced block that is itself larger than ``max_tokens`` still becomes exactly one
   (accepted) oversized chunk rather than being cut in half. Fence *open* and *close* lines are
   additionally treated as hard chunk boundaries - like an ATX heading, whatever is currently buffered
   is flushed unconditionally (not size-gated) the moment a fence starts, and again the moment it
   ends. Without the open-side flush, prose accumulated just under ``max_tokens`` would silently absorb
   the entire fence on top of it before any size check ever fires, producing a chunk that is prose
   *plus* fence - which is not an accepted oversized shape (only a standalone oversized fence is;
   MEASURED regression: a 28,446-character single chunk in a real vault note came from exactly this -
   intro prose merged with one large fenced block because the fence-open transition never flushed the
   prose first).
2. **A ``#`` inside a fence is never a heading.** The heading regex is only tried when not inside a
   fence, for the same reason.

Chunk boundaries are placed at (a) every ATX heading line (level 1-6, outside a fence), (b) every fence
open/close transition (see rule 1), and (c) the first opportunity after the running token estimate
reaches ``max_tokens`` while not inside a fence.

Offsets and ``ChunkDraft.text``: :class:`~aimemory.domain.base.DomainModel` (frozen, A02) sets
``str_strip_whitespace=True`` on every model, so ``ChunkDraft.text`` can never itself carry leading or
trailing whitespace. Rather than let the model silently strip text out from under offsets computed
against the *unstripped* buffer, this chunker trims each buffer itself before building the
``ChunkDraft`` and shrinks ``char_start``/``char_end`` by exactly the trimmed amount. The guaranteed
invariant is therefore ``chunk.text == text[chunk.char_start:chunk.char_end]`` exactly (not merely
``.strip()``-equal) for every chunk, and any content between two chunks' offsets (or before the first
/ after the last) is always whitespace-only - never a dropped non-whitespace character - so the
original text can be losslessly reconstructed from the chunks plus the (whitespace) gaps between them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aimemory.common.hashing import text_hash
from aimemory.domain.ports import ChunkDraft

from .tokens import chars_for_tokens, count_tokens

__all__ = ["MarkdownChunker", "chunk_markdown_text"]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_WORD_SPLIT_RE = re.compile(r"(\s+)")

DEFAULT_MAX_TOKENS = 200
DEFAULT_OVERLAP_TOKENS = 30


def _fence_delim(stripped: str) -> tuple[str, str] | None:
    """``(run, trailing)`` when ``stripped`` is a fence delimiter line, else ``None``."""
    match = _FENCE_RE.match(stripped)
    if not match:
        return None
    return match.group(1), match.group(2)


def _closes_fence(trailing_and_run: tuple[str, str], opening_run: str) -> bool:
    run, trailing = trailing_and_run
    return run[0] == opening_run[0] and len(run) >= len(opening_run) and trailing.strip() == ""


@dataclass
class _Builder:
    max_tokens: int
    overlap_tokens: int
    chunks: list[ChunkDraft] = field(default_factory=list)
    heading_stack: list[str] = field(default_factory=list)
    buf: str = ""
    buf_start: int = 0

    def _append_chunk(self) -> bool:
        stripped = self.buf.strip()
        if not stripped:
            return False
        lead = len(self.buf) - len(self.buf.lstrip())
        trail = len(self.buf) - len(self.buf.rstrip())
        start = self.buf_start + lead
        end = self.buf_start + len(self.buf) - trail
        self.chunks.append(
            ChunkDraft(
                ordinal=len(self.chunks),
                text=stripped,
                text_hash=text_hash(stripped),
                heading_path=list(self.heading_stack),
                char_start=start,
                char_end=end,
                token_count=count_tokens(stripped),
            )
        )
        return True

    def _emit(self) -> bool:
        """Emit the buffer as a chunk if it has non-whitespace content. Returns whether it did."""
        return self._append_chunk()

    def on_fence_line(self, line: str) -> None:
        self.buf += line

    def on_fence_open(self, line: str, pos_before: int) -> None:
        """Flush whatever is buffered, then start the fence in a chunk of its own.

        Unconditional, exactly like :meth:`on_heading` - deliberately *not* size-gated. A size gate
        here would be no gate at all: prose sitting just under ``max_tokens`` passes it, and then the
        whole fenced block is appended on top before any further check can fire (interior fence lines
        never trigger one, by rule 1).
        """
        if self._emit():
            self.buf = ""
            self.buf_start = pos_before
        self.buf += line

    def on_fence_close(self, pos_after: int) -> None:
        """Close the fence and flush it, so following prose starts a new chunk rather than being
        appended to the block."""
        if self._emit():
            self.buf = ""
            self.buf_start = pos_after

    def maybe_split(self, pos: int) -> None:
        if not self.buf.strip():
            return
        if count_tokens(self.buf) < self.max_tokens:
            return
        emitted = self._emit()
        if not emitted:
            return
        overlap_chars = min(len(self.buf), chars_for_tokens(self.overlap_tokens))
        overlap_text = self.buf[len(self.buf) - overlap_chars :] if overlap_chars else ""
        self.buf = overlap_text
        self.buf_start = pos - len(overlap_text)

    def on_heading(self, line: str, level: int, title: str, pos_before: int) -> None:
        emitted = self._emit()
        if emitted:
            self.buf = ""
            self.buf_start = pos_before
        self.heading_stack = self.heading_stack[: level - 1] + [title]
        self.buf += line

    def on_text_line(self, line: str, pos_before: int) -> None:
        # Split into whitespace-preserving word pieces so a very long *unwrapped* paragraph line
        # (common in Obsidian notes that are not hard-wrapped) can still be cut close to the token
        # target, instead of only ever being able to split at physical line boundaries. A "word" that
        # is itself pathologically long (MEASURED in the real vault: URL-encoded tracking query
        # strings pasted into `Clippings/` job postings, hundreds of characters with no whitespace at
        # all) is additionally hard-split by character count - the only place this chunker ever cuts
        # mid-token, and only because there is no smaller natural unit left to cut on.
        #
        # The slice size is recomputed before *every* cut from the buffer's *remaining* budget
        # (``max_tokens - count_tokens(self.buf)``), not fixed at ``chars_for_tokens(max_tokens)``. A
        # fixed full-budget slice size would let a hard split land on top of whatever partial content
        # (a heading, a short preceding sentence) already sits in ``buf``, adding up to another full
        # ``max_tokens`` worth of tokens on top of it before the size check in ``maybe_split`` ever
        # fires - which is exactly how a 200-token target previously produced a 264-token chunk. Sizing
        # each slice to the remaining headroom instead bounds the overshoot to the token<->char
        # rounding error (a token or two), regardless of how much was already buffered.
        cursor = pos_before
        for piece in _WORD_SPLIT_RE.split(line):
            if not piece:
                continue
            i = 0
            while i < len(piece):
                remaining_tokens = max(1, self.max_tokens - count_tokens(self.buf))
                limit = max(1, chars_for_tokens(remaining_tokens))
                sub = piece[i : i + limit]
                self.buf += sub
                cursor += len(sub)
                i += len(sub)
                self.maybe_split(cursor)

    def finish(self) -> list[ChunkDraft]:
        self._emit()
        return self.chunks


def chunk_markdown_text(
    text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[ChunkDraft]:
    """Chunk ``text`` (already extracted markdown body, frontmatter stripped). See module docstring."""
    if not text:
        return []
    builder = _Builder(max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    fence_run: str | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        pos_after = pos + len(line)
        delim = _fence_delim(stripped)
        if fence_run is not None:
            builder.on_fence_line(line)
            if delim is not None and _closes_fence(delim, fence_run):
                fence_run = None
                builder.on_fence_close(pos_after)
            pos = pos_after
            continue
        if delim is not None:
            fence_run = delim[0]
            builder.on_fence_open(line, pos)
            pos = pos_after
            continue
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            builder.on_heading(line, level, title, pos)
            pos = pos_after
            continue
        builder.on_text_line(line, pos)
        pos = pos_after
    return builder.finish()


class MarkdownChunker:
    """:class:`~aimemory.domain.ports.Chunker` implementation for markdown."""

    name = "markdown"

    def chunk(
        self, text: str, *, relative_path: str, max_tokens: int = DEFAULT_MAX_TOKENS
    ) -> list[ChunkDraft]:
        overlap = min(DEFAULT_OVERLAP_TOKENS, max(1, max_tokens // 4))
        return chunk_markdown_text(text, max_tokens=max_tokens, overlap_tokens=overlap)
