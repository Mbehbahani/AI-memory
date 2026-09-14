"""The ten real my-vault episodes used by P4-T02 and by the ADR-0002 gate (P4-T04). Owner A05.

The vault is **read-only** (`D:\\My-Vault`, mounted at ``/sources/vault`` by the ``tools`` service).
Nothing here writes to it.

The file list is *fixed and committed* rather than sampled, for two reasons: the C3 quality criterion
needs hand-listed expected entities per episode (``gate_expectations.py``), which is only auditable
if the text is stable; and P4-T02's per-run numbers must be reproducible by re-running the harness.

Each episode is the frontmatter-stripped body truncated at a paragraph boundary near
``TARGET_CHARS`` (~800 tokens at the ~4 chars/token rule of thumb for English markdown). The *actual*
token count is whatever Ollama reports as ``prompt_eval_count``; the report quotes that, not the
estimate.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BENCHMARK_EXTRA_FILES",
    "EPISODE_FILES",
    "TARGET_CHARS",
    "Episode",
    "load_benchmark_episodes",
    "load_episodes",
    "vault_root",
]

TARGET_CHARS = 3200

# Ten notes spanning projects, areas, resources, workflows and the AIOS maps, so the measurement is
# not dominated by a single writing style.
EPISODE_FILES: tuple[str, ...] = (
    "01 Projects/JobLab Product/JobLab DE Lakehouse.md",
    "01 Projects/Oploy Website/Oploy Landing Page.md",
    "02 Areas/Career Development/Career Target - AI Engineering.md",
    "02 Areas/Home Infrastructure/Ubuntu Server - Admin Dashboards.md",
    "02 Areas/Oploy Business/Oploy Website - System Map.md",
    "03 Resources/GitHub/Repository Standard.md",
    "03 Resources/Ubuntu Server/Ubuntu Server Setup and Usage Guide.md",
    "07 Workflows/Context - JobLab Product.md",
    "07 Workflows/Operating Manual.md",
    "AIOS/Maps/project-graph.md",
)

# P4-T02 needs 20 *distinct* prompts (temperature 0 makes a repeat of the same prompt a repeat of
# the same answer, which would measure nothing). These ten add to the gate's ten.
BENCHMARK_EXTRA_FILES: tuple[str, ...] = (
    "00 Inbox/web-extention/Monitor AI coding agents with Grafana.md",
    "03 Resources/GitHub/About and Topics Cheatsheet.md",
    "03 Resources/GitHub/Profile README Draft.md",
    "03 Resources/Tableau/Tableau Public Profile.md",
    "06 Outputs/Career Docs/2026-09-11 - Career Update - Harness and Research MLOps.md",
    "06 Outputs/Career Docs/CV - Current.md",
    "06 Outputs/YouTube/YouTube Channel - Oploy.md",
    "07 Workflows/Context Router.md",
    "AIOS/Maps/vault-map.md",
    "AIOS/me.md",
)

_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Episode:
    """One ~800-token episode with the provenance needed for the report and for the gate."""

    key: str
    relative_path: str
    title: str
    text: str

    @property
    def source_uri(self) -> str:
        return f"vault://my-vault/{self.relative_path}"

    @property
    def approx_tokens(self) -> int:
        """ESTIMATED only - the MEASURED count is Ollama's ``prompt_eval_count``."""
        return round(len(self.text) / 4)


def vault_root() -> Path:
    """``/sources/vault`` inside the tools container; ``VAULT_ROOT`` overrides for a host run."""
    override = os.environ.get("VAULT_ROOT")
    if override:
        return Path(override)
    return Path("/sources/vault")


def _truncate(body: str, target: int = TARGET_CHARS) -> str:
    if len(body) <= target:
        return body.strip()
    window = body[: target + 400]
    cut = window.rfind("\n\n", target - 600)
    if cut == -1:
        cut = window.rfind("\n", target - 300)
    if cut == -1:
        cut = target
    return body[:cut].strip()


def _load(base: Path, files: tuple[str, ...], start: int) -> list[Episode]:
    episodes: list[Episode] = []
    for index, relative in enumerate(files, start=start):
        raw = (base / relative).read_text(encoding="utf-8", errors="replace")
        text = _truncate(_FRONTMATTER.sub("", raw))
        episodes.append(
            Episode(
                key=f"E{index:02d}",
                relative_path=relative,
                title=Path(relative).stem,
                text=text,
            )
        )
    return episodes


def load_episodes(root: Path | None = None) -> list[Episode]:
    """The ten gate episodes (E01-E10). Raises ``FileNotFoundError`` if the vault is not mounted."""
    return _load(root or vault_root(), EPISODE_FILES, 1)


def load_benchmark_episodes(root: Path | None = None) -> list[Episode]:
    """The twenty P4-T02 episodes: the gate's ten (E01-E10) plus ten more (E11-E20)."""
    base = root or vault_root()
    return _load(base, EPISODE_FILES, 1) + _load(base, BENCHMARK_EXTRA_FILES, 11)
