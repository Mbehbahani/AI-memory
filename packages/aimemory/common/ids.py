"""Identifier and slug helpers.

Consumers: A04 (primary keys, seeds), A07a (source/version/chunk ids, project slugs),
A07b (chunk ids), A08 (entity/fact/artifact ids, deterministic ids for structural nodes),
A09 (retrieval log ids), A10 (audit ids), A12 (tests).

Two kinds of identifier exist in V0.1:

* **Random, time-ordered** — :func:`new_id` returns a UUIDv7-shaped ``uuid.UUID`` (48-bit big-endian
  Unix-ms prefix, version nibble 7). Time ordering keeps B-tree inserts local in Postgres. The
  standard library on Python 3.12 has no ``uuid7``, so the layout is produced here; the value is a
  perfectly ordinary RFC-4122 UUID for every consumer.
* **Deterministic** — :func:`deterministic_id` (UUIDv5 in a fixed namespace) so that re-running a
  deterministic step (registry seed, structural graph, chunk identity) produces the same ids and the
  writes stay idempotent, which the resumable state machine of plan §L depends on.

Registry keys that a human types (``projects.id``, ``source_roots.root_id``) are *slugs*, not UUIDs.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
from datetime import datetime

from .time import utc_now

__all__ = [
    "AIMEMORY_NAMESPACE",
    "deterministic_id",
    "is_slug",
    "new_id",
    "normalize_name",
    "slugify",
]

# uuid5 namespace for every deterministic id in the system. Frozen: changing it re-keys the database.
AIMEMORY_NAMESPACE = uuid.UUID("6f0f5c1e-6b3a-5f6d-9c2b-1a0d2e3f4a5b")

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_WS_RE = re.compile(r"\s+")


def new_id(at: datetime | None = None) -> uuid.UUID:
    """Return a time-ordered UUID (version 7 layout): 48-bit ms timestamp + 74 random bits."""
    moment = at or utc_now()
    unix_ms = int(moment.timestamp() * 1000)
    raw = bytearray(os.urandom(16))
    raw[0:6] = unix_ms.to_bytes(6, "big")
    raw[6] = (raw[6] & 0x0F) | 0x70  # version 7
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC 4122 variant
    return uuid.UUID(bytes=bytes(raw))


def deterministic_id(kind: str, *parts: str | int | uuid.UUID | None) -> uuid.UUID:
    """Stable UUIDv5 for ``kind`` + ``parts``.

    ``kind`` names the row family (``"source"``, ``"chunk"``, ``"entity"``, …) so that two families
    never collide on the same natural key. ``None`` parts are rendered as an empty segment, which
    keeps ``(a, None)`` distinct from ``(a,)``.
    """
    key = "|".join([kind, *["" if p is None else str(p) for p in parts]])
    return uuid.uuid5(AIMEMORY_NAMESPACE, key)


def slugify(value: str, *, max_length: int = 64) -> str:
    """Lowercase ASCII slug (``JobLab Lakehouse (DE)`` → ``joblab-lakehouse-de``).

    Used for ``projects.id``, ``source_roots.root_id`` and alias normalization. Raises
    :class:`ValueError` if nothing slug-like remains, because an empty registry key is a bug.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM_RE.sub("-", folded.lower()).strip("-")
    slug = slug[:max_length].strip("-")
    if not slug:
        raise ValueError(f"cannot slugify {value!r}")
    return slug


def is_slug(value: str) -> bool:
    """True if ``value`` is already a canonical slug."""
    return bool(_SLUG_RE.match(value))


def normalize_name(value: str) -> str:
    """Normalized entity name used for deterministic entity resolution and duplicate detection.

    Case-folded, accent-stripped, whitespace-collapsed, trailing sentence punctuation removed.
    Brackets are kept, so ``JobLab Lakehouse (DE)`` keeps its qualifier. This is the
    value stored in ``entities.normalized_name`` and compared by A08's deterministic-first resolver.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    collapsed = _WS_RE.sub(" ", folded).strip().lower()
    return collapsed.strip(" \t\r\n.,;:!?'\"")
