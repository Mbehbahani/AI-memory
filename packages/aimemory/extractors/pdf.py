"""PDF extractor via ``pypdf`` (plan section K).

A broken/encrypted/corrupt PDF must never crash a scan of a thousand files (plan section P6-T02): any
failure - from ``pypdf`` itself, from a single unreadable page, or an unexpected structure - is caught
and reported as ``ExtractedText(ok=False, reason=...)`` so the caller downgrades the source to
``CATALOG_ONLY`` with that reason recorded, instead of raising.
"""

from __future__ import annotations

import io

import pypdf

from aimemory.domain.ports import ExtractedText

from ._util import finalize_text, truncate_to_bytes

__all__ = ["PdfExtractor"]

_EXTENSIONS = frozenset({".pdf"})


class PdfExtractor:
    name = "pdf"
    extractor_version = "0.1.0"

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        return _suffix(relative_path) in _EXTENSIONS

    def extract(
        self, data: bytes, *, relative_path: str, max_bytes: int | None = None
    ) -> ExtractedText:
        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                try:
                    # Many "encrypted" PDFs use an empty owner password and are still readable.
                    reader.decrypt("")
                except Exception:  # noqa: BLE001 - any decrypt failure means we cannot read it
                    return ExtractedText(
                        extractor=self.name,
                        extractor_version=self.extractor_version,
                        media_type="application/pdf",
                        ok=False,
                        reason="pdf is encrypted and could not be decrypted with an empty password",
                    )
            page_texts: list[str] = []
            for page in reader.pages:
                try:
                    page_texts.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - one bad page must not fail the whole document
                    page_texts.append("")
            text = "\n\n".join(t for t in page_texts if t.strip())
        except Exception as exc:  # noqa: BLE001 - pypdf raises many distinct exception types
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                media_type="application/pdf",
                ok=False,
                reason=f"pdf extraction failed: {type(exc).__name__}",
            )
        if not text.strip():
            return ExtractedText(
                extractor=self.name,
                extractor_version=self.extractor_version,
                media_type="application/pdf",
                ok=False,
                reason="pdf produced no extractable text (scanned image or empty document)",
            )
        truncated = False
        if max_bytes is not None:
            text, truncated = truncate_to_bytes(text, max_bytes)
        stored, char_count = finalize_text(text)
        return ExtractedText(
            text=stored,
            extractor=self.name,
            extractor_version=self.extractor_version,
            media_type="application/pdf",
            char_count=char_count,
            truncated=truncated,
            ok=True,
        )


def _suffix(relative_path: str) -> str:
    name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""
