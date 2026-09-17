"""Regression tests for the two chunker defects found by the first real `D:\\My-Vault` ingest (P7-T02).

Both were MEASURED on real files, but the fixtures here are synthetic and constructed to the same
*shape* - the vault is a read-only source root and is never a fixture source (CLAUDE.md, plan §T).

1. `chunking/markdown.py`: a fence-open transition did not flush the buffer, so prose sitting just
   under `max_tokens` absorbed an entire fenced block before any size check could fire. A real vault
   note produced a single 28,446-character (~8,366-token) chunk against a 200-token target.
2. `chunking/code.py`: the line window could never shrink below one whole physical line, so a
   one-line 12,623-character `.json` file became a single chunk. The embedding service rejects those
   outright (`422 texts[0] exceeds 8000 characters`).
"""

from __future__ import annotations

from aimemory.chunking.code import chunk_code_text
from aimemory.chunking.markdown import chunk_markdown_text
from aimemory.chunking.tokens import count_tokens

MAX_TOKENS = 200


def _assert_offsets_reconstruct(text: str, chunks: list) -> None:
    """The invariant both modules promise: text is sliced exactly, and the gaps are whitespace only."""
    for chunk in chunks:
        assert chunk.text == text[chunk.char_start : chunk.char_end], (
            "char offsets must slice the source exactly, not merely .strip()-equal"
        )
    cursor = 0
    for chunk in chunks:
        assert text[cursor : chunk.char_start].strip() == "", "dropped non-whitespace between chunks"
        cursor = chunk.char_end
    assert text[cursor:].strip() == "", "dropped non-whitespace after the last chunk"


def _fenced_note() -> str:
    """Prose just under the budget, then one large fenced block - the shape that failed."""
    prose = " ".join(f"word{i}" for i in range(180))
    body = "\n".join(f'    "key{i}": "value{i}",' for i in range(400))
    return f"# Workflow\n\n{prose}\n\n```json\n{body}\n```\n\nClosing prose after the block.\n"


def test_a_fence_does_not_absorb_the_prose_above_it() -> None:
    text = _fenced_note()
    chunks = chunk_markdown_text(text, max_tokens=MAX_TOKENS)

    # The prose must survive as its own chunk rather than being swallowed by the block below it.
    assert any("word0" in c.text and "```" not in c.text for c in chunks), (
        "the prose above the fence was merged into the fence chunk"
    )
    # The fence is one chunk of its own: an oversized *standalone* fence is the accepted outcome
    # (module rule 1), prose-plus-fence is not.
    fence_chunks = [c for c in chunks if "```json" in c.text]
    assert len(fence_chunks) == 1
    assert "word179" not in fence_chunks[0].text
    # Prose after the block starts a new chunk instead of being appended to it.
    assert any(c.text.startswith("Closing prose") for c in chunks)
    _assert_offsets_reconstruct(text, chunks)


def test_the_fence_rule_still_holds_never_split_inside_a_block() -> None:
    """The fix must not buy bounded chunks by breaking the module's first hard rule."""
    text = _fenced_note()
    chunks = chunk_markdown_text(text, max_tokens=MAX_TOKENS)
    fence_chunks = [c for c in chunks if "```" in c.text]
    assert len(fence_chunks) == 1, "the fenced block was cut into pieces"
    opened = fence_chunks[0].text.count("```")
    assert opened == 2, "a chunk holds either a whole fence or none of it"


def test_a_heading_inside_a_fence_is_still_not_a_heading() -> None:
    text = "# Real\n\ntext\n\n```\n# not a heading\nmore\n```\n\nafter\n"
    chunks = chunk_markdown_text(text, max_tokens=MAX_TOKENS)
    assert all(c.heading_path in ([], ["Real"]) for c in chunks), (
        f"a # inside a fence was treated as a heading: {[c.heading_path for c in chunks]}"
    )
    _assert_offsets_reconstruct(text, chunks)


def test_a_single_oversized_physical_line_is_split_by_the_code_chunker() -> None:
    """The minified-JSON shape: one line, no whitespace, far over budget."""
    line = "{" + ",".join(f'"key{i}":"value{i}"' for i in range(900)) + "}"
    text = line + "\n"
    assert count_tokens(line) > MAX_TOKENS, "fixture is not actually oversized"

    chunks = chunk_code_text(text, relative_path="data.json", max_tokens=MAX_TOKENS)

    assert len(chunks) > 1, "the oversized single line was not split"
    for chunk in chunks:
        assert count_tokens(chunk.text) <= MAX_TOKENS * 1.5, (
            f"chunk still over budget: {count_tokens(chunk.text)} tokens"
        )
    # Every piece cites the one line it came from.
    assert all(c.heading_path == ["data.json:1-1"] for c in chunks)
    _assert_offsets_reconstruct(text, chunks)


def test_an_oversized_line_with_whitespace_prefers_a_whitespace_cut() -> None:
    line = " ".join(f"token{i}" for i in range(900))
    text = line + "\n"
    chunks = chunk_code_text(text, relative_path="long.sql", max_tokens=MAX_TOKENS)

    assert len(chunks) > 1
    # A whitespace-preferring cut never severs a word, so every piece round-trips through a split.
    rejoined = " ".join(c.text for c in chunks)
    assert rejoined.split() == line.split(), "a token was cut in half despite available whitespace"
    _assert_offsets_reconstruct(text, chunks)


def test_ordinary_code_is_unaffected_by_the_oversized_line_path() -> None:
    text = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(40))
    chunks = chunk_code_text(text, relative_path="mod.py", max_tokens=MAX_TOKENS)
    assert chunks
    assert all(c.heading_path[0].startswith("mod.py:") for c in chunks)
    assert all("-" in c.heading_path[0] for c in chunks)
    _assert_offsets_reconstruct(text, chunks)
