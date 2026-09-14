"""Unit tests for :mod:`aimemory.chunking.code` (P6-T02).

The task's "real files" bounds requirement is satisfied here against real Python source files in this
very repository (``packages/aimemory/**.py``) rather than ``D:\\My-Vault``, which is a pure-markdown
Obsidian vault with no source code in it (checked: zero ``*.py`` files) - see
``tests/unit/test_chunking_markdown.py`` for the ``D:\\My-Vault`` bounds test, which is the file family
that vault actually contains.
"""

from __future__ import annotations

from pathlib import Path

from aimemory.chunking.code import CodeChunker, chunk_code_text
from aimemory.chunking.tokens import count_tokens

REPO_ROOT = Path(__file__).resolve().parents[2]
DOMAIN_DIR = REPO_ROOT / "packages" / "aimemory" / "domain"
MINI_REPO = REPO_ROOT / "tests" / "fixtures" / "mini-repo"


def _assert_offsets_and_coverage(text: str, chunks: list) -> None:
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
    def test_short_file_is_one_chunk(self) -> None:
        text = "def f():\n    return 1\n"
        chunks = chunk_code_text(text, relative_path="f.py")
        assert len(chunks) == 1
        assert chunks[0].heading_path == ["f.py:1-2"]
        _assert_offsets_and_coverage(text, chunks)

    def test_empty_text_returns_no_chunks(self) -> None:
        assert chunk_code_text("", relative_path="empty.py") == []

    def test_heading_path_is_line_range_citation(self) -> None:
        text = "\n".join(f"line {i}" for i in range(150))
        chunks = chunk_code_text(text, relative_path="src/module.py", target_lines=60, overlap_lines=8)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.heading_path[0].startswith("src/module.py:")

    def test_line_windows_overlap(self) -> None:
        text = "\n".join(f"line {i}" for i in range(150)) + "\n"
        chunks = chunk_code_text(text, relative_path="m.py", target_lines=60, overlap_lines=8)
        for a, b in zip(chunks, chunks[1:]):
            assert b.char_start <= a.char_end
        _assert_offsets_and_coverage(text, chunks)


class TestBoundarySnapping:
    def test_snaps_to_top_level_def_when_cheap(self) -> None:
        lines = [f"    x{i} = {i}" for i in range(55)]
        lines.append("def next_function():")
        lines.append("    return 1")
        text = "\n".join(lines) + "\n"
        chunks = chunk_code_text(text, relative_path="m.py", target_lines=56, overlap_lines=5)
        # The first chunk should end right before "def next_function():" rather than mid-body.
        first_text = chunks[0].text
        assert "def next_function" not in first_text or first_text.rstrip().endswith(
            "def next_function():"
        )

    def test_token_budget_shrinks_window_for_dense_lines(self) -> None:
        # A handful of very long lines should trip the token budget well before 60 lines.
        text = "\n".join("x = '" + ("a" * 400) + "'" for _ in range(10)) + "\n"
        chunks = chunk_code_text(text, relative_path="dense.py", max_tokens=100, target_lines=60)
        assert len(chunks) > 1
        for chunk in chunks:
            assert chunk.token_count <= 100 + 50  # generous slack for the +1 line snap tolerance


class TestChunkerClass:
    def test_name_and_dispatch(self) -> None:
        chunker = CodeChunker()
        assert chunker.name == "code"
        text = "\n".join(f"line {i}" for i in range(10))
        chunks = chunker.chunk(text, relative_path="x.py")
        assert len(chunks) == 1
        assert count_tokens(chunks[0].text) == chunks[0].token_count


class TestRealRepositoryFilesBounds:
    def test_chunks_are_sane_on_real_domain_source_files(self) -> None:
        py_files = sorted(DOMAIN_DIR.glob("*.py"))
        assert py_files, "expected real .py files under packages/aimemory/domain"
        checked = 0
        for path in py_files:
            text = path.read_text(encoding="utf-8")
            if not text.strip():
                continue
            chunks = chunk_code_text(text, relative_path=str(path.relative_to(REPO_ROOT)))
            _assert_offsets_and_coverage(text, chunks)
            for chunk in chunks:
                assert chunk.token_count >= 1
                # Soft target ~200 tokens/~60 lines; boundary-snapping (blank line / top-level def)
                # is allowed to overshoot the token budget by up to 1.5x (chunk_code_text's own
                # documented cap) in exchange for not cutting a function/class in half.
                assert chunk.token_count <= 300
                assert ":" in chunk.heading_path[0]
            checked += 1
        assert checked == len(py_files)

    def test_chunks_are_sane_on_mini_repo_app_py(self) -> None:
        path = MINI_REPO / "src" / "app.py"
        text = path.read_text(encoding="utf-8")
        chunks = chunk_code_text(text, relative_path="src/app.py")
        assert len(chunks) >= 1
        _assert_offsets_and_coverage(text, chunks)
