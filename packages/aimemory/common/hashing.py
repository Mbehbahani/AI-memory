"""Content and text hashing.

Consumers: A07a (source fingerprinting, change detection), A07b (chunk text hashes),
A06/A09 (embedding reuse by ``(text_hash, model_id)``), A08 (provenance stamps),
A09 (citation format ``[{source_uri}#{heading} @{hash8}]`` of ``config/retrieval.yaml``).

All hashes are lowercase hex SHA-256 with an algorithm prefix (``sha256:...``) so a future algorithm
change is visible in the data instead of silently comparing apples to pears.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

__all__ = [
    "HASH_PREFIX",
    "content_hash_bytes",
    "content_hash_file",
    "short_hash",
    "text_hash",
]

HASH_PREFIX = "sha256:"
_CHUNK_BYTES = 1024 * 1024


def _prefixed(digest: str) -> str:
    return f"{HASH_PREFIX}{digest}"


def content_hash_bytes(data: bytes | Iterable[bytes]) -> str:
    """Hash raw bytes (or a stream of byte blocks). Used for ``source_versions.content_hash``."""
    digest = hashlib.sha256()
    if isinstance(data, (bytes, bytearray, memoryview)):
        digest.update(data)
    else:
        for block in data:
            digest.update(block)
    return _prefixed(digest.hexdigest())


def content_hash_file(path: Path | str) -> str:
    """Stream a file and hash it. Never reads the whole file into memory."""

    def _blocks() -> Iterator[bytes]:
        with open(path, "rb") as handle:
            while block := handle.read(_CHUNK_BYTES):
                yield block

    return content_hash_bytes(_blocks())


def text_hash(text: str) -> str:
    """Hash extracted/chunk text after newline and trailing-whitespace normalization.

    Normalization (CRLF → LF, strip trailing whitespace per line, strip the document ends) makes the
    hash stable across Windows/Linux checkouts so that embeddings are reused rather than recomputed.
    """
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return _prefixed(hashlib.sha256(normalized.encode("utf-8")).hexdigest())


def short_hash(value: str, length: int = 8) -> str:
    """First ``length`` hex characters of a prefixed or bare hash — the ``@{hash8}`` in citations."""
    bare = value.removeprefix(HASH_PREFIX)
    return bare[:length]
