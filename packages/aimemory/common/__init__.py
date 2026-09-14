"""``aimemory.common`` - cross-cutting utilities owned by A02 (Architecture & Contracts).

Consumers: every other package and every service. Contents:

* :mod:`aimemory.common.config` - pydantic-settings groups mirroring ``.env.example``
* :mod:`aimemory.common.logging` - structlog JSON logging with secret redaction
* :mod:`aimemory.common.errors` - sanitized error hierarchy (plan section T)
* :mod:`aimemory.common.time` - UTC helpers for ``observed_at`` / ``valid_from`` (ADR-0005)
* :mod:`aimemory.common.ids` - UUIDv7-shaped and deterministic ids, slugs, name normalization
* :mod:`aimemory.common.hashing` - SHA-256 content/text hashes and the 8-char citation hash

Frozen after P1: changes require an ADR (see ``docs/development/agent-protocol.md``).
"""

from .config import Settings, get_settings
from .errors import AiMemoryError
from .hashing import content_hash_bytes, content_hash_file, short_hash, text_hash
from .ids import deterministic_id, new_id, normalize_name, slugify
from .logging import configure_logging, get_logger
from .time import ensure_utc, is_valid_at, observed_at_for, utc_now, valid_from_for

__all__ = [
    "AiMemoryError",
    "Settings",
    "configure_logging",
    "content_hash_bytes",
    "content_hash_file",
    "deterministic_id",
    "ensure_utc",
    "get_logger",
    "get_settings",
    "is_valid_at",
    "new_id",
    "normalize_name",
    "observed_at_for",
    "short_hash",
    "slugify",
    "text_hash",
    "utc_now",
    "valid_from_for",
]
