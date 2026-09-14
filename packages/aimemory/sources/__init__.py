"""``aimemory.sources`` - policy resolution, ignore rules, and the secret detector (A07b, plan
section K). A07a's discovery walker (P6-T03) is the intended caller of everything exported here.
"""

from __future__ import annotations

from .ignore import BUILTIN_DENY_DIRS, BUILTIN_DENY_FILES, IgnoreRules, load_ignore_rules
from .policies import (
    Limits,
    PoliciesConfig,
    PolicyDecision,
    RootOverride,
    default_policies_path,
    load_policies_config,
    resolve_policy,
)
from .secrets import (
    ContentPattern,
    SecretDetectorConfig,
    SecretScanResult,
    scan,
    scan_content,
    scan_filename,
)

__all__ = [
    "BUILTIN_DENY_DIRS",
    "BUILTIN_DENY_FILES",
    "ContentPattern",
    "IgnoreRules",
    "Limits",
    "PoliciesConfig",
    "PolicyDecision",
    "RootOverride",
    "SecretDetectorConfig",
    "SecretScanResult",
    "default_policies_path",
    "load_ignore_rules",
    "load_policies_config",
    "resolve_policy",
    "scan",
    "scan_content",
    "scan_filename",
]
