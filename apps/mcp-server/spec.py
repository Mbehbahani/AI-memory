"""The frozen MCP contract, loaded from ``schemas/mcp/tools.json``. Owner: A10.

The contract file is owned by A02 and frozen after P1 (ADR process to change it). This module is the
*only* place the server learns what it serves: tool names, titles, descriptions, input schemas,
resources and the ADR-0008 safeguard numbers all come from that file, and the advertised
``inputSchema`` is the file's object byte-for-byte rather than something re-derived from a Python
signature. That is what makes P11-T02's "served tool list == tools.json" an identity rather than a
coincidence - and it means a contract change lands in the server without an edit here.

Nothing in this module touches a database or the network.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from aimemory.common.config import get_settings

__all__ = [
    "Spec",
    "ToolSpec",
    "get_spec",
]


class ToolSpec:
    """One entry of ``tools[]``. Read-only view; the dicts handed out are deep copies."""

    __slots__ = ("_raw",)

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw

    @property
    def name(self) -> str:
        return str(self._raw["name"])

    @property
    def kind(self) -> str:
        return str(self._raw["kind"])

    @property
    def title(self) -> str:
        return str(self._raw["title"])

    @property
    def description(self) -> str:
        return str(self._raw["description"])

    @property
    def gateway(self) -> str:
        return str(self._raw.get("gateway", ""))

    @property
    def is_write(self) -> bool:
        return self.kind == "write"

    @property
    def input_schema(self) -> dict[str, Any]:
        """A fresh copy, so a caller (or FastMCP) can never mutate the loaded contract."""
        return json.loads(json.dumps(self._raw["inputSchema"]))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ToolSpec({self.name!r}, kind={self.kind!r})"


class Spec:
    """The whole contract: tools, resources, safeguards."""

    def __init__(self, raw: dict[str, Any], *, path: Path) -> None:
        self.path = path
        self.raw = raw
        self.version = str(raw.get("version", "0"))
        self.tools: list[ToolSpec] = [ToolSpec(t) for t in raw["tools"]]
        self._by_name = {t.name: t for t in self.tools}
        self.resources: list[dict[str, Any]] = list(raw.get("resources", []))
        self.safeguards: dict[str, Any] = dict(raw.get("safeguards", {}))
        self._verify_counts()

    def _verify_counts(self) -> None:
        """Fail at import, not at the first call, if the file contradicts its own ``counts`` block."""
        declared = self.raw.get("counts", {})
        actual = {
            "read": sum(1 for t in self.tools if t.kind == "read"),
            "write": sum(1 for t in self.tools if t.kind == "write"),
        }
        for kind, expected in declared.items():
            if actual.get(kind) != expected:
                raise ValueError(
                    f"tools.json declares counts.{kind}={expected} but contains {actual.get(kind)}"
                )

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._by_name[name]
        except KeyError:  # pragma: no cover - a typo in the server, caught by the startup check
            raise KeyError(f"tools.json has no tool named {name!r}") from None

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.tools]

    @property
    def write_names(self) -> list[str]:
        return [t.name for t in self.tools if t.is_write]

    # ---- ADR-0008 safeguard numbers, read from the contract rather than hard-coded -------------
    @property
    def rate_limit_per_minute(self) -> int:
        return int(self.safeguards.get("rate_limit_per_minute", 10))

    @property
    def max_text_chars(self) -> int:
        return int(self.safeguards.get("max_text_chars", 8000))

    @property
    def confirm_required(self) -> bool:
        return bool(self.safeguards.get("confirm_required", True))

    @property
    def audit_table(self) -> str:
        return str(self.safeguards.get("audit_table", "mcp_audit_log"))


@lru_cache(maxsize=1)
def get_spec() -> Spec:
    """Load and cache the contract. ``MEMORY_SCHEMA_DIR`` decides where it is read from."""
    path = get_settings().paths.mcp_tools_file
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Spec(raw, path=Path(path))
