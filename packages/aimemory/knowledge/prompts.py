"""Production extraction prompts for the native engine (A08, P8-T02).

Two calls, two prompts, matching the two frozen schemas in ``schemas/extraction/``. The schema is
**never restated in the prompt**: it is enforced by the provider (Ollama ``format=<schema>``
grammar-constrained decoding, Bedrock forced tool use), which is the whole point of assumption B10.
Restating it would spend context on something already guaranteed and would create a second place for
the contract to drift.

These are deliberately close in shape to ``tests/evaluation/local_ai/prompts.py`` (A05's *measurement*
prompt), so the P4-T02 latency and token numbers transfer to production. Three differences, each
earned by a MEASURED failure:

* **An output budget.** The schema's own caps (40 entities, 30 artifacts, 1200-char statements) admit
  an object far larger than ``LLM_NUM_PREDICT=1024`` tokens; when the model used that headroom the
  reply was truncated mid-stream and did not parse. Capping the *budget* in the prompt fixes that
  without touching the frozen schema.
* **Explicit type guidance for the things both models got wrong.** P4-T02 measured both providers
  typing the vault's PARA folders as ``Repository``/``InfrastructureComponent`` and ``Personal
  Harness`` as ``Technology``. The prompt now says what those are. The deterministic seed guard
  (ADR-0014 rule 3) still has the last word - this only reduces how often it has to fire.
* **A provenance header.** The document title and source URI are given to the model so entity names
  can be grounded, and so a name that appears only in the path is still resolvable.

Neither prompt mentions a provider. ADR-0009's engine is provider-agnostic by construction.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..domain.ports import EpisodeContext

__all__ = [
    "EPISODE_SYSTEM_PROMPT",
    "OUTPUT_BUDGET",
    "RELATION_SYSTEM_PROMPT",
    "RELATION_RETRY_SUFFIX",
    "episode_prompt",
    "relation_prompt",
]

NEWLINE = chr(10)

OUTPUT_BUDGET = (
    "Output budget (important): at most 12 entities and at most 5 artifacts. "
    "Keep every description under 120 characters, every statement under 200 characters, "
    "every evidence_quote under 120 characters, and the summary under 300 characters. "
    "Omit aliases unless the text gives one explicitly. "
    "A short valid object is better than a long truncated one."
)

_TYPE_GUIDANCE = (
    "Typing rules that are often got wrong:" + NEWLINE
    + "- A folder or category of a note vault (for example '01 Projects', '03 Resources') is a "
    "Concept, never a Repository and never an InfrastructureComponent." + NEWLINE
    + "- A named personal initiative or system being built (for example 'Personal Harness') is a "
    "Project, not a Technology." + NEWLINE
    + "- A tool you use (an editor, an assistant, a library, a CLI) is a Technology, not a Project." + NEWLINE
    + "- Application means a deployed or deployable software product. A job vacancy, target role, "
    "interview, CV, or application letter is not an Application; use Concept for a role or "
    "interview topic and Document for a file." + NEWLINE
    + "- A human name is a Person. Never type a person as an Organization or an "
    "InfrastructureComponent." + NEWLINE
    + "- A company, university or community is an Organization."
)

EPISODE_SYSTEM_PROMPT = (
    "You are a knowledge extractor for a personal memory system. "
    "Read one document and return a single JSON object describing it." + NEWLINE
    + "Rules:" + NEWLINE
    + "- Use only information stated in the document. Never invent names, dates or statuses." + NEWLINE
    + "- entities: the people, projects, organizations, technologies, documents, repositories, "
    "concepts, decisions, requirements, tasks, experiments, datasets, research findings, "
    "applications and infrastructure components the document is actually about. Prefer the exact "
    "surface form used in the text." + NEWLINE
    + "- artifacts: decisions, requirements, tasks, findings, hypotheses and experiments that the "
    "document states. Give each a short title and a one-paragraph statement. "
    "Set date_if_stated and supersedes_if_stated only when the text says so, otherwise null." + NEWLINE
    + "- summary: at most 3 sentences." + NEWLINE
    + "- Output JSON only." + NEWLINE
    + _TYPE_GUIDANCE + NEWLINE
    + OUTPUT_BUDGET
)

RELATION_SYSTEM_PROMPT = (
    "You extract relationships between already-identified entities in one document." + NEWLINE
    + "Rules:" + NEWLINE
    + "- subject and object must both be taken verbatim from the provided entity list." + NEWLINE
    + "- Only state a relationship the document actually supports. Never invent one." + NEWLINE
    + "- predicate must be one of the allowed relationship types." + NEWLINE
    + "- Use HAS_STATUS, HAS_STAGE or SELECTED_OPTION only when the document states a single "
    "current value for that property of the subject, and give the value as plain text, not as "
    "an entity. A subject can hold only one of these at a time (ADR-0015)." + NEWLINE
    + "- HAS_OWNER, USES_ARCHITECTURE and DEPLOYED_ON are ordinary relationships: a subject "
    "may have several at once. Do not drop one because you are stating another." + NEWLINE
    + "- Direction matters and is not symmetric. Write the subject that *owns* the property "
    "first: a project HAS_OWNER a person (never a person HAS_OWNER a project); a project "
    "USES_ARCHITECTURE a technology; an application is DEPLOYED_ON infrastructure." + NEWLINE
    + "- statement: one sentence, grounded in the document." + NEWLINE
    + "- Set valid_from_if_stated only when the document gives a date for the relationship." + NEWLINE
    + "- At most 25 facts. Output JSON only."
)

#: Call 3 (optional): used once when call 2 returned no usable fact but the document clearly has
#: several entities. Keeps the engine inside plan section M's "2-3 calls per episode".
RELATION_RETRY_SUFFIX = (
    NEWLINE
    + "Your previous reply contained no usable relationship. State the most obvious relationships "
    "between the listed entities - at least the ones the document asserts directly - and nothing "
    "you cannot point at in the text."
)


def _header(context: EpisodeContext, title: str | None) -> str:
    parts = [f"Document title: {title or context.doc_title or '(untitled)'}"]
    if context.source_uri:
        parts.append(f"Source: {context.source_uri}")
    if context.heading_path:
        parts.append("Section: " + " > ".join(context.heading_path))
    if context.project_id:
        parts.append(f"Project: {context.project_id}")
    return NEWLINE.join(parts)


def episode_prompt(body: str, context: EpisodeContext, *, title: str | None = None) -> str:
    """Call 1: provenance header + episode text."""
    known = ""
    if context.known_entity_names:
        listing = ", ".join(context.known_entity_names[:40])
        known = (
            NEWLINE
            + "Entities the system already knows (prefer these exact spellings when they appear): "
            + listing
            + NEWLINE
        )
    return (
        _header(context, title) + NEWLINE
        + known
        + "---" + NEWLINE
        + body + NEWLINE
        + "---" + NEWLINE
        + "Extract the document as JSON."
    )


def relation_prompt(
    body: str,
    context: EpisodeContext,
    entity_names: Sequence[str],
    *,
    title: str | None = None,
    retry: bool = False,
) -> str:
    """Call 2 (and the single optional call 3): the call-1 entity list + the episode text."""
    listing = NEWLINE.join(f"- {name}" for name in entity_names) or "- (none)"
    prompt = (
        _header(context, title) + NEWLINE
        + "Entities found in this document:" + NEWLINE
        + listing + NEWLINE
        + "---" + NEWLINE
        + body + NEWLINE
        + "---" + NEWLINE
        + "Return the relationships between these entities as JSON."
    )
    return prompt + RELATION_RETRY_SUFFIX if retry else prompt
