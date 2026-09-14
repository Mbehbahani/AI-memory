"""The extraction prompt used for the P4-T02 validity measurement (owner A05).

This is deliberately a *measurement* prompt, not the production one: A08 owns the native engine's
prompts (P8-T01). It has the shape the production call will have - a short system message fixing the
role and the ontology vocabulary, and a user message carrying the episode's provenance header plus
its text - so the measured token counts and latencies transfer.

The JSON Schema itself is not restated in the prompt: it is passed to Ollama as ``format=`` and
enforced by grammar-constrained decoding, which is the whole point of assumption B10.
"""

from __future__ import annotations

from .episodes import Episode

__all__ = ["CONCISE_SUFFIX", "EPISODE_SYSTEM_PROMPT", "episode_prompt"]

# Mitigation variant measured in P4-T02 run B. The schema's own caps (40 entities / 30 artifacts /
# 1200-char statements) allow an object far larger than ``LLM_NUM_PREDICT=1024`` tokens; when the
# model uses that headroom the reply is cut mid-token-stream and no longer parses. This suffix caps
# the *output budget* in the prompt without touching the frozen schema.
CONCISE_SUFFIX = (
    "\nOutput budget (important): at most 12 entities and at most 5 artifacts. "
    "Keep every description under 120 characters, every statement under 200 characters, "
    "every evidence_quote under 120 characters, and the summary under 300 characters. "
    "Omit aliases unless the text gives one explicitly. "
    "A short valid object is better than a long truncated one."
)

EPISODE_SYSTEM_PROMPT = (
    "You are a knowledge extractor for a personal memory system. "
    "Read one document and return a single JSON object describing it.\n"
    "Rules:\n"
    "- Use only information stated in the document. Never invent names, dates or statuses.\n"
    "- entities: the people, projects, organizations, technologies, documents, repositories, "
    "concepts, decisions, requirements, tasks, experiments, datasets, research findings, "
    "applications and infrastructure components the document is actually about. Prefer the exact "
    "surface form used in the text. At most 40.\n"
    "- artifacts: decisions, requirements, tasks, findings, hypotheses and experiments that the "
    "document states. Give each a short title and a one-paragraph statement. "
    "Set date_if_stated and supersedes_if_stated only when the text says so, otherwise null.\n"
    "- summary: at most 3 sentences.\n"
    "- Output JSON only."
)


def episode_prompt(episode: Episode) -> str:
    """Provenance header + episode text, exactly as the ingestion path will assemble it."""
    return (
        f"Document title: {episode.title}\n"
        f"Source: {episode.source_uri}\n"
        "---\n"
        f"{episode.text}\n"
        "---\n"
        "Extract the document as JSON."
    )
