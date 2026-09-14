"""Secret detector driven by ``config/policies.yaml: secret_detector`` (plan section K).

Contract this module upholds everywhere, including on the unhappy path: **no byte of scanned content
is ever returned, logged, or embedded in an exception message.** Every public function here returns
only :class:`SecretScanResult` (a boolean, a tuple of *rule names*, and a count) or raises nothing.
Callers (:mod:`aimemory.sources.policies`) may log the result freely - it contains no file content by
construction, not by discipline, so there is nothing for a careless ``logger.info(result)`` to leak.

Two kinds of rule, both from ``config/policies.yaml``:

* ``filename_patterns`` - glob patterns (``fnmatch``) matched against the file's basename only, never
  its full path (so a `.env` living deep in an ignored tree is still caught if it is ever considered).
* ``content_patterns`` - regexes matched against decoded text. A match's *span* is discarded
  immediately; only the rule ``name`` and a count of matches survive.

No entropy-based detection is implemented. An entropy scanner would need a calibrated threshold and a
measured false-positive rate on ordinary prose before it could be trusted (plan section K asks for
exactly that if one is added); doing that measurement honestly needs a representative text corpus and
is out of scope for P6-T01. Filename + regex patterns cover every adversarial case in
``tests/fixtures/adversarial`` (AWS-style keys, private-key headers, JWTs, connection strings with an
inline password, `.env`/`.pem`/`id_rsa*` filenames) - see ``tests/unit/test_secrets.py``.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

__all__ = [
    "ContentPattern",
    "SecretDetectorConfig",
    "SecretScanResult",
    "scan",
    "scan_content",
    "scan_filename",
]


@dataclass(frozen=True)
class ContentPattern:
    name: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class SecretDetectorConfig:
    """Compiled form of ``config/policies.yaml: secret_detector``."""

    filename_patterns: tuple[str, ...] = ()
    content_patterns: tuple[ContentPattern, ...] = ()
    action: str = "CATALOG_ONLY_NO_TEXT"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SecretDetectorConfig":
        filename_patterns = tuple(data.get("filename_patterns", ()))
        content_patterns = tuple(
            ContentPattern(name=entry["name"], regex=re.compile(entry["regex"]))
            for entry in data.get("content_patterns", ())
        )
        action = data.get("action", "CATALOG_ONLY_NO_TEXT")
        return cls(
            filename_patterns=filename_patterns, content_patterns=content_patterns, action=action
        )


@dataclass(frozen=True)
class SecretScanResult:
    """The *only* thing this module ever hands back. Never carries content."""

    suspected: bool
    matched_rules: tuple[str, ...] = field(default_factory=tuple)
    match_count: int = 0

    @property
    def reason(self) -> str:
        if not self.suspected:
            return "no secret pattern matched"
        rules = ", ".join(self.matched_rules)
        plural = "s" if self.match_count != 1 else ""
        return f"matched rule(s) [{rules}] ({self.match_count} occurrence{plural})"

    def __or__(self, other: "SecretScanResult") -> "SecretScanResult":
        """Merge two results (filename scan + content scan) without ever touching content."""
        rules = tuple(dict.fromkeys((*self.matched_rules, *other.matched_rules)))
        return SecretScanResult(
            suspected=self.suspected or other.suspected,
            matched_rules=rules,
            match_count=self.match_count + other.match_count,
        )


_NO_MATCH = SecretScanResult(suspected=False)


def scan_filename(relative_path: str, config: SecretDetectorConfig) -> SecretScanResult:
    """Match the file's basename against ``filename_patterns``. Never inspects file content."""
    name = PurePosixPath(relative_path.replace("\\", "/")).name
    hits = tuple(
        f"filename:{pattern}"
        for pattern in config.filename_patterns
        if fnmatch.fnmatch(name.lower(), pattern.lower())
    )
    if not hits:
        return _NO_MATCH
    return SecretScanResult(suspected=True, matched_rules=hits, match_count=len(hits))


def scan_content(text: str, config: SecretDetectorConfig) -> SecretScanResult:
    """Match decoded text against ``content_patterns``.

    Only counts and rule names leave this function - ``text`` itself, and every matched substring, are
    discarded the instant ``regex.findall`` returns.
    """
    matched_rules: list[str] = []
    total = 0
    for pattern in config.content_patterns:
        count = sum(1 for _ in pattern.regex.finditer(text))
        if count:
            matched_rules.append(f"content:{pattern.name}")
            total += count
    if not matched_rules:
        return _NO_MATCH
    return SecretScanResult(suspected=True, matched_rules=tuple(matched_rules), match_count=total)


def scan(
    relative_path: str,
    content: bytes | str | None,
    config: SecretDetectorConfig,
) -> SecretScanResult:
    """Filename scan (always) plus a content scan (when ``content`` is given).

    ``content`` may be ``None`` for paths where content will never be read (e.g. already ``IGNORE`` or
    already over the size limit before any bytes are loaded) - the filename check alone still runs,
    because a match on ``*.pem``/``.env``/`id_rsa*` needs no content at all.
    """
    result = scan_filename(relative_path, config)
    if content is None:
        return result
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes | bytearray) else content
    return result | scan_content(text, config)
