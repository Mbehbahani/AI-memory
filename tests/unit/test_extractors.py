"""Unit tests for :mod:`aimemory.extractors` (P6-T02)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import docx
import pytest
from aimemory.extractors import DEFAULT_EXTRACTORS, get_extractor
from aimemory.extractors.code import CodeExtractor, LANGUAGE_BY_EXTENSION
from aimemory.extractors.docx import DocxExtractor
from aimemory.extractors.markdown import MarkdownExtractor, extract_links, split_frontmatter
from aimemory.extractors.notebook import IpynbExtractor
from aimemory.extractors.pdf import PdfExtractor
from aimemory.extractors.plaintext import PlainTextExtractor
from aimemory.extractors.structured import StructuredExtractor
from aimemory.extractors.tex import TexExtractor, strip_line_comments

REPO_ROOT = Path(__file__).resolve().parents[2]
MINI_VAULT = REPO_ROOT / "tests" / "fixtures" / "mini-vault"
MINI_REPO = REPO_ROOT / "tests" / "fixtures" / "mini-repo"
ADVERSARIAL = REPO_ROOT / "tests" / "fixtures" / "adversarial"

# A minimal, hand-written single-page PDF with a real content stream. pypdf recovers from its
# deliberately terse xref table (logs a recoverable warning) and extracts "Hello Fixture".
MINIMAL_PDF_BYTES = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 200 200] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length 44 >>
stream
BT /F1 24 Tf 20 100 Td (Hello Fixture) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f
trailer
<< /Size 6 /Root 1 0 R >>
startxref
0
%%EOF
"""


class TestMarkdownExtractor:
    def test_frontmatter_and_wikilinks(self) -> None:
        extractor = MarkdownExtractor()
        data = (MINI_VAULT / "01 Projects" / "Fixture Project.md").read_bytes()
        result = extractor.extract(data, relative_path="01 Projects/Fixture Project.md")
        assert result.ok is True
        assert result.frontmatter["project"] == "fixture-project"
        assert result.frontmatter["status"] == "active"
        assert "architecture-decision-a" in result.links
        assert "architecture-decision-b" in result.links
        assert "PostgreSQL" in result.links
        assert "Neo4j" in result.links
        assert "requirement-fixture-storage" in result.links
        # the external http(s) markdown link target is captured too
        assert any(link.startswith("https://example.com") for link in result.links)
        # frontmatter block itself must not leak into the body text
        assert "---" not in result.text.split("\n", 1)[0]
        assert "aliases" not in result.text

    def test_split_frontmatter_missing_block(self) -> None:
        fm, body = split_frontmatter("# just a heading\n\nbody text")
        assert fm == {}
        assert body == "# just a heading\n\nbody text"

    def test_split_frontmatter_malformed_yaml_degrades_gracefully(self) -> None:
        text = "---\nthis: [is not: valid: yaml\n---\nbody\n"
        fm, body = split_frontmatter(text)
        assert fm == {}
        assert body == text  # whole document kept as body, nothing raised

    def test_split_frontmatter_non_mapping_document(self) -> None:
        text = "---\n- just\n- a\n- list\n---\nbody\n"
        fm, body = split_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_extract_links_wikilink_alias_and_heading(self) -> None:
        body = "[[Target A]] and [[Target B|Alias]] and [[Target C#Section]] and [[Target C#Section|X]]"
        links = extract_links(body)
        assert links == ["Target A", "Target B", "Target C"]

    def test_extract_links_ignores_image_embeds(self) -> None:
        body = "![alt text](image.png) but [a real link](note.md)"
        assert extract_links(body) == ["note.md"]

    def test_binary_disguised_as_markdown_is_rejected(self) -> None:
        extractor = MarkdownExtractor()
        data = (ADVERSARIAL / "binary-disguised-as-md.md").read_bytes()
        result = extractor.extract(data, relative_path="binary-disguised-as-md.md")
        assert result.ok is False
        assert "binary" in result.reason

    def test_empty_file_is_ok_with_empty_text(self) -> None:
        extractor = MarkdownExtractor()
        data = (ADVERSARIAL / "empty-file.md").read_bytes()
        assert data == b""
        result = extractor.extract(data, relative_path="empty-file.md")
        assert result.ok is True
        assert result.text == ""
        assert result.char_count == 0

    def test_bom_file_decodes_without_bom_character(self) -> None:
        extractor = MarkdownExtractor()
        data = (ADVERSARIAL / "bom-file.md").read_bytes()
        assert data.startswith(b"\xef\xbb\xbf")
        result = extractor.extract(data, relative_path="bom-file.md")
        assert result.ok is True
        assert "﻿" not in result.text
        assert result.text.startswith("# BOM file")

    def test_crlf_file_preserves_line_content(self) -> None:
        extractor = MarkdownExtractor()
        data = (ADVERSARIAL / "crlf-file.md").read_bytes()
        result = extractor.extract(data, relative_path="crlf-file.md")
        assert result.ok is True
        assert "item one" in result.text
        assert "item two" in result.text

    def test_unicode_filename_is_supported_by_extension_alone(self) -> None:
        extractor = MarkdownExtractor()
        candidates = list(ADVERSARIAL.glob("*report.md"))
        assert candidates, "unicode-named fixture not found"
        path = candidates[0]
        assert extractor.supports(path.name) is True
        result = extractor.extract(path.read_bytes(), relative_path=path.name)
        assert result.ok is True

    def test_max_bytes_truncates_and_sets_flag(self) -> None:
        extractor = MarkdownExtractor()
        data = ("# T\n\n" + ("word " * 5000)).encode("utf-8")
        result = extractor.extract(data, relative_path="big.md", max_bytes=100)
        assert result.truncated is True
        assert len(result.text.encode("utf-8")) <= 100

    def test_supports_only_markdown_extensions(self) -> None:
        extractor = MarkdownExtractor()
        assert extractor.supports("note.md") is True
        assert extractor.supports("note.markdown") is True
        assert extractor.supports("note.txt") is False


class TestPlainTextExtractor:
    def test_extracts_plain_text(self) -> None:
        extractor = PlainTextExtractor()
        result = extractor.extract(b"hello world\n", relative_path="notes.txt")
        assert result.ok is True
        # ExtractedText (frozen DomainModel) sets str_strip_whitespace=True, so the trailing newline
        # is stripped from the stored field even though the extractor itself does not trim anything.
        assert result.text == "hello world"
        assert result.extractor == "plaintext"


class TestCodeExtractor:
    @pytest.mark.parametrize("ext,language", LANGUAGE_BY_EXTENSION.items())
    def test_language_detection(self, ext: str, language: str) -> None:
        extractor = CodeExtractor()
        result = extractor.extract(b"print(1)\n", relative_path=f"file{ext}")
        assert result.ok is True
        assert result.media_type == f"text/x-{language}"

    def test_real_mini_repo_file(self) -> None:
        extractor = CodeExtractor()
        data = (MINI_REPO / "src" / "app.py").read_bytes()
        result = extractor.extract(data, relative_path="src/app.py")
        assert result.ok is True
        assert "def main" in result.text

    def test_binary_code_file_rejected(self) -> None:
        extractor = CodeExtractor()
        data = bytes(range(256)) * 4
        result = extractor.extract(data, relative_path="weird.py")
        assert result.ok is False


class TestStructuredExtractor:
    def test_toml_file(self) -> None:
        extractor = StructuredExtractor()
        data = (MINI_REPO / "config" / "settings.toml").read_bytes()
        result = extractor.extract(data, relative_path="config/settings.toml")
        assert result.ok is True
        assert result.media_type == "application/toml"
        assert "mini-repo" in result.text

    def test_json_file(self) -> None:
        extractor = StructuredExtractor()
        result = extractor.extract(b'{"a": 1}', relative_path="x.json")
        assert result.ok is True
        assert result.media_type == "application/json"


class TestTexExtractor:
    def test_strips_unescaped_comments_only(self) -> None:
        text = r"100\% done % this is a comment" + "\nkeep this line\n"
        stripped = strip_line_comments(text)
        assert r"100\% done" in stripped
        assert "this is a comment" not in stripped
        assert "keep this line" in stripped

    def test_extract_bib(self) -> None:
        extractor = TexExtractor()
        data = b"@article{fixture2026, title={Fixture}, year=2026}\n"
        result = extractor.extract(data, relative_path="refs.bib")
        assert result.ok is True
        assert result.media_type == "text/x-bibtex"


class TestIpynbExtractor:
    def _notebook_bytes(self) -> bytes:
        notebook = {
            "metadata": {"kernelspec": {"language": "python"}},
            "cells": [
                {"cell_type": "markdown", "source": ["# Title\n", "\n", "Some prose.\n"]},
                {"cell_type": "code", "source": ["x = 1\n", "print(x)\n"], "outputs": [{"data": "should be dropped"}]},
                {"cell_type": "raw", "source": ["should be skipped entirely"]},
            ],
        }
        return json.dumps(notebook).encode("utf-8")

    def test_markdown_and_code_cells_kept_outputs_dropped(self) -> None:
        extractor = IpynbExtractor()
        result = extractor.extract(self._notebook_bytes(), relative_path="analysis.ipynb")
        assert result.ok is True
        assert "Some prose." in result.text
        assert "print(x)" in result.text
        assert "should be dropped" not in result.text
        assert "should be skipped entirely" not in result.text
        assert "```python" in result.text

    def test_invalid_json_degrades_gracefully(self) -> None:
        extractor = IpynbExtractor()
        result = extractor.extract(b"{not valid json", relative_path="broken.ipynb")
        assert result.ok is False
        assert result.reason


class TestPdfExtractor:
    def test_extracts_real_text(self) -> None:
        extractor = PdfExtractor()
        result = extractor.extract(MINIMAL_PDF_BYTES, relative_path="doc.pdf")
        assert result.ok is True
        assert "Hello Fixture" in result.text

    def test_corrupt_pdf_degrades_to_ok_false(self) -> None:
        extractor = PdfExtractor()
        result = extractor.extract(b"not a pdf at all", relative_path="broken.pdf")
        assert result.ok is False
        assert "pdf" in result.reason.lower()


class TestDocxExtractor:
    def _docx_bytes(self, text: str) -> bytes:
        document = docx.Document()
        document.add_paragraph(text)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    def test_extracts_real_text(self) -> None:
        extractor = DocxExtractor()
        result = extractor.extract(self._docx_bytes("Hello Fixture Docx"), relative_path="doc.docx")
        assert result.ok is True
        assert "Hello Fixture Docx" in result.text

    def test_corrupt_docx_degrades_to_ok_false(self) -> None:
        extractor = DocxExtractor()
        result = extractor.extract(b"not a docx", relative_path="broken.docx")
        assert result.ok is False


class TestRegistry:
    def test_get_extractor_dispatches_by_extension(self) -> None:
        assert isinstance(get_extractor("note.md"), MarkdownExtractor)
        assert isinstance(get_extractor("src/app.py"), CodeExtractor)
        assert isinstance(get_extractor("doc.pdf"), PdfExtractor)
        assert isinstance(get_extractor("doc.docx"), DocxExtractor)
        assert isinstance(get_extractor("nb.ipynb"), IpynbExtractor)
        assert isinstance(get_extractor("cfg.toml"), StructuredExtractor)
        assert isinstance(get_extractor("paper.tex"), TexExtractor)
        assert isinstance(get_extractor("notes.txt"), PlainTextExtractor)

    def test_unrecognized_extension_returns_none(self) -> None:
        assert get_extractor("archive.zip") is None

    def test_every_default_extractor_claims_a_disjoint_extension_set(self) -> None:
        seen: dict[str, str] = {}
        sample_extensions = [
            ".md", ".markdown", ".txt", ".rst", ".py", ".r", ".sql", ".ts", ".tsx", ".js", ".mjs",
            ".svelte", ".sh", ".ps1", ".cypher", ".tex", ".bib", ".yaml", ".yml", ".toml", ".json",
            ".ini", ".cfg", ".ipynb", ".pdf", ".docx",
        ]
        for ext in sample_extensions:
            claimants = [e for e in DEFAULT_EXTRACTORS if e.supports(f"file{ext}")]
            assert len(claimants) == 1, f"{ext} claimed by {claimants}"
            seen[ext] = type(claimants[0]).__name__
