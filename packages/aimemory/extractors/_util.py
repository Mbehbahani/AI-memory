"""Shared helpers for the extractor family (plan section P6-T02). Not part of the public API surface
of :mod:`aimemory.extractors` - imported by the individual extractor modules only.
"""

from __future__ import annotations

__all__ = ["decode_text", "finalize_text", "looks_binary", "truncate_to_bytes"]

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF32_LE_BOM = b"\xff\xfe\x00\x00"
_UTF32_BE_BOM = b"\x00\x00\xfe\xff"


def decode_text(data: bytes) -> tuple[str, str, bool]:
    """BOM-aware decode to ``str``.

    Order: a UTF-32 BOM (checked first - its LE form is a strict prefix of the UTF-16 LE BOM) then a
    UTF-16 BOM, then UTF-8 (with or without a BOM), then a lossy UTF-8 fallback.

    Returns ``(text, encoding_name, lossy)``. ``lossy=True`` means undecodable bytes were replaced
    with U+FFFD (``errors="replace"``) - acceptable for cataloging/embedding purposes, but the text is
    then not guaranteed to round-trip back to the original bytes.
    """
    if data.startswith(_UTF32_LE_BOM):
        return data.decode("utf-32-le"), "utf-32-le", False
    if data.startswith(_UTF32_BE_BOM):
        return data.decode("utf-32-be"), "utf-32-be", False
    if data.startswith(_UTF16_LE_BOM):
        return data.decode("utf-16-le"), "utf-16-le", False
    if data.startswith(_UTF16_BE_BOM):
        return data.decode("utf-16-be"), "utf-16-be", False
    if data.startswith(_UTF8_BOM):
        return data.decode("utf-8-sig"), "utf-8-sig", False
    try:
        return data.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8-replace", True


def looks_binary(data: bytes, *, sample_size: int = 8192) -> bool:
    """Cheap binary sniff: a NUL byte in the first ``sample_size`` bytes, or a high ratio of
    non-printable/non-whitespace bytes. Good enough to catch an image/executable saved with a ``.md``
    extension without needing a MIME-sniffing dependency.
    """
    sample = data[:sample_size]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    text_bytes = bytes(range(0x20, 0x7F)) + b"\t\n\r\x0b\x0c"
    # Bytes >= 0x80 are allowed (UTF-8 multi-byte sequences); only control bytes below 0x20 (minus
    # the whitespace ones above) count against the file.
    non_text = sum(1 for b in sample if b < 0x20 and bytes([b]) not in b"\t\n\r\x0b\x0c")
    return (non_text / len(sample)) > 0.30


def finalize_text(text: str) -> tuple[str, int]:
    """Strip and measure text the same way :class:`~aimemory.domain.ports.ExtractedText` will.

    ``ExtractedText`` (a frozen ``DomainModel``, A02) sets ``str_strip_whitespace=True``, so its
    ``text`` field is stripped on construction regardless of what an extractor passes in. Every
    extractor calls this *before* building the model so ``char_count`` matches what is actually
    stored, instead of the length of a string pydantic is about to trim out from under it.
    """
    stripped = text.strip()
    return stripped, len(stripped)


def truncate_to_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` so its UTF-8 encoding fits in ``max_bytes``. Never cuts a multi-byte
    character in half (uses ``errors="ignore"`` on the boundary byte slice, which only ever drops the
    partial trailing sequence, not any earlier byte).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True
