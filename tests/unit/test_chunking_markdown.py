"""Unit tests for :mod:`aimemory.chunking.markdown` (P6-T02)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aimemory.chunking.markdown import MarkdownChunker, chunk_markdown_text
from aimemory.chunking.tokens import count_tokens
from aimemory.extractors.markdown import MarkdownExtractor

REPO_ROOT = Path(__file__).resolve().parents[2]
ADVERSARIAL = REPO_ROOT / "tests" / "fixtures" / "adversarial"
MINI_VAULT = REPO_ROOT / "tests" / "fixtures" / "mini-vault"

# Compose's tools service mounts the read-only vault at this container path (docker-compose.override
# .yml). Skipped when not mounted so the suite still collects/runs without the real vault attached.
REAL_VAULT = Path("/sources/vault")


def _assert_offsets_and_coverage(text: str, chunks: list) -> None:
    """Shared invariant checks: exact substrings, monotonic order, and whitespace-only gaps."""
    assert chunks, "expected at least one chunk"
    prev_end = 0
    for chunk in chunks:
        assert 0 <= chunk.char_start <= chunk.char_end <= len(text)
        assert text[chunk.char_start : chunk.char_end] == chunk.text
        gap = text[prev_end : chunk.char_start]
        assert gap.strip() == "", f"non-whitespace content skipped between chunks: {gap!r}"
        prev_end = max(prev_end, chunk.char_end)
    trailing = text[prev_end:]
    assert trailing.strip() == "", f"non-whitespace trailing content dropped: {trailing!r}"


class TestBasicChunking:
    def test_single_section_single_chunk(self) -> None:
        text = "# Title\n\nA short paragraph that easily fits in one chunk.\n"
        chunks = chunk_markdown_text(text, max_tokens=200, overlap_tokens=30)
        assert len(chunks) == 1
        assert chunks[0].heading_path == ["Title"]
        _assert_offsets_and_coverage(text, chunks)

    def test_heading_path_trail(self) -> None:
        text = (
            "# H1\n\nintro\n\n"
            "## H2\n\nunder h2\n\n"
            "### H3\n\nunder h3\n"
        )
        chunks = chunk_markdown_text(text, max_tokens=200, overlap_tokens=30)
        paths = [c.heading_path for c in chunks]
        assert ["H1"] in paths
        assert ["H1", "H2"] in paths
        assert ["H1", "H2", "H3"] in paths
        _assert_offsets_and_coverage(text, chunks)

    def test_heading_level_jump_back_up(self) -> None:
        text = "# H1\n\n## H2a\n\ntext\n\n## H2b\n\nmore text\n"
        chunks = chunk_markdown_text(text)
        paths = [c.heading_path for c in chunks]
        assert ["H1", "H2a"] in paths
        assert ["H1", "H2b"] in paths

    def test_empty_text_returns_no_chunks(self) -> None:
        assert chunk_markdown_text("") == []

    def test_no_heading_document_gets_empty_heading_path(self) -> None:
        text = "Just a paragraph with no heading at all.\n"
        chunks = chunk_markdown_text(text)
        assert chunks[0].heading_path == []
        _assert_offsets_and_coverage(text, chunks)


class TestFenceSafety:
    def test_never_splits_inside_a_fence_even_when_oversized(self) -> None:
        fence_body = "\n".join(f"line {i} of a long fenced block" for i in range(200))
        text = f"# Title\n\n```python\n{fence_body}\n```\n\nafter the fence\n"
        chunks = chunk_markdown_text(text, max_tokens=50, overlap_tokens=10)
        # The fence must appear whole inside exactly one chunk.
        fence_chunks = [c for c in chunks if "```python" in c.text]
        assert len(fence_chunks) == 1
        assert fence_chunks[0].text.count("```") == 2
        _assert_offsets_and_coverage(text, chunks)

    def test_hash_inside_fence_is_not_a_heading(self) -> None:
        data = (ADVERSARIAL / "nested-headings-with-fence.md").read_bytes()
        extracted = MarkdownExtractor().extract(data, relative_path="nested-headings-with-fence.md")
        assert extracted.ok is True
        chunks = chunk_markdown_text(extracted.text, max_tokens=60, overlap_tokens=10)
        heading_titles = {title for c in chunks for title in c.heading_path}
        # None of the fenced '#'-lines should have been picked up as real headings.
        assert "So does this." not in heading_titles
        assert not any(t.startswith("And this") for t in heading_titles)
        # But the real headings, including the one that follows the fence, are present.
        assert "H1 top" in heading_titles
        assert "H6 sixth level" in heading_titles
        assert "Back to H2" in heading_titles
        _assert_offsets_and_coverage(extracted.text, chunks)

    def test_deeply_nested_heading_path_is_six_long(self) -> None:
        data = (ADVERSARIAL / "nested-headings-with-fence.md").read_bytes()
        extracted = MarkdownExtractor().extract(data, relative_path="nested-headings-with-fence.md")
        chunks = chunk_markdown_text(extracted.text, max_tokens=60, overlap_tokens=10)
        deepest = max(chunks, key=lambda c: len(c.heading_path))
        assert deepest.heading_path == [
            "H1 top",
            "H2 second level",
            "H3 third level",
            "H4 fourth level",
            "H5 fifth level",
            "H6 sixth level",
        ]


class TestTokenTargetAndOverlap:
    def test_long_section_is_split_with_overlap(self) -> None:
        paragraph = "This is one sentence of filler prose used to pad a section. " * 40
        text = f"# Long section\n\n{paragraph}\n"
        chunks = chunk_markdown_text(text, max_tokens=100, overlap_tokens=20)
        assert len(chunks) > 1
        for chunk in chunks:
            # Word-level granularity means the overshoot past the token target is small (well under
            # one extra "sentence" worth of tokens), not a multiple of it.
            assert chunk.token_count <= 100 + 15
        # Overlap: consecutive chunks' character spans should intersect (shared tail/head text).
        for a, b in zip(chunks, chunks[1:]):
            assert b.char_start < a.char_end
        _assert_offsets_and_coverage(text, chunks)

    def test_chunker_class_uses_default_target(self) -> None:
        chunker = MarkdownChunker()
        assert chunker.name == "markdown"
        paragraph = "Word " * 2000
        chunks = chunker.chunk(f"# T\n\n{paragraph}", relative_path="t.md")
        assert len(chunks) > 1
        for c in chunks:
            assert count_tokens(c.text) == c.token_count


class TestRealVaultBounds:
    """Bounds test on real files from D:\\My-Vault (read-only), per P6-T02 acceptance."""

    def _vault_markdown_files(self) -> list[Path]:
        if not REAL_VAULT.is_dir():
            return []
        return list(REAL_VAULT.rglob("*.md"))[:200]

    def test_chunk_sizes_and_offsets_are_sane_on_real_notes(self) -> None:
        files = self._vault_markdown_files()
        if not files:
            pytest.skip(f"real vault not mounted at {REAL_VAULT} (see docker-compose.override.yml)")
        extractor = MarkdownExtractor()
        checked = 0
        for path in files:
            data = path.read_bytes()
            extracted = extractor.extract(data, relative_path=str(path))
            if not extracted.ok or not extracted.text.strip():
                continue
            chunks = chunk_markdown_text(extracted.text, max_tokens=200, overlap_tokens=30)
            _assert_offsets_and_coverage(extracted.text, chunks)
            for chunk in chunks:
                assert chunk.token_count >= 1
                if chunk.token_count > 250:
                    # MEASURED on this real vault: a few notes (e.g. a "copyable master prompt")
                    # contain one huge fenced code block. The hard rule "never split inside a fenced
                    # code block" (plan section P6-T02) means that fence must stay in one chunk even
                    # when it is far larger than the ~200-token target - so an oversized chunk is only
                    # acceptable here if it is in fact a fence, never plain prose overflowing (the
                    # character-level hard-split for pathological whitespace-free runs, e.g. tracking
                    # query strings in clipped job postings, keeps ordinary prose near the target).
                    assert "```" in chunk.text or "~~~" in chunk.text, (
                        f"oversized non-fenced chunk ({chunk.token_count} tokens) in {path}"
                    )
                # Backstop against a genuine runaway/accumulation bug, independent of fences.
                assert chunk.token_count <= 20_000
            checked += 1
        assert checked > 0, "no non-empty real vault markdown files were found to check"
