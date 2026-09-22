"""Close demonstrably misleading Senior AI ENG extraction edges, preserving history.

The source notes remain unchanged. A manual episode records the review, and the
original facts keep their provenance with a closed validity window. Run without
``--apply`` to inspect the exact candidate list first.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from sqlalchemy import text

from aimemory.common.ids import new_id
from aimemory.common.time import utc_now
from aimemory.domain.enums import EngineKind, EpisodeStatus, EpisodeType, Origin, Tier
from aimemory.domain.models import Episode
from aimemory.persistence.db import Database
from aimemory.persistence.repositories import EpisodeRepo

ROOT_ID = "senior-ai-eng"
PROJECT_ID = "senior-ai-eng"
DEFAULT_REPORT_PATH = Path("reports/2026-09-21-senior-ai-eng-edge-review.md")

QUERY = text(
    """
    SELECT f.id, f.predicate, subject.type AS subject_type,
           subject.canonical_name AS subject, object.type AS object_type,
           object.canonical_name AS object, source.relative_path AS path
      FROM facts f
      JOIN sources source ON source.id = f.source_id
      JOIN entities subject ON subject.id = f.subject_entity_id
      JOIN entities object ON object.id = f.object_entity_id
     WHERE source.root_id = :root_id
       AND f.project_id = :project_id
       AND f.valid_to IS NULL
       AND f.status IN ('current', 'unconfirmed')
     ORDER BY f.predicate, subject.canonical_name, object.canonical_name, f.id
    """
)


def reason(row: object) -> str | None:
    """Conservative, exact rules based on the cited source notes reviewed on 2026-09-21."""
    predicate, subject, object_ = row.predicate, row.subject, row.object

    if predicate == "PART_OF" and row.subject_type == row.object_type == "Project":
        return "Independent portfolio projects are referenced as evidence, not subprojects."

    false_attribution = {
        ("Harness engineering", "Anthropic"),
        ("Harness engineering", "OpenAI"),
        ("Managed Agents", "Michael Cohen"),
        ("Managed Agents", "Lance Martin"),
        ("Managed Agents", "Gabe Cemaj"),
        ("Senior AI ENG", "Chip Huyen"),
        ("Senior AI ENG", "Hamel"),
        ("Senior AI ENG", "sae-offer-architect"),
        ("Claude Sonnet 5", "Anthropic"),
    }
    if predicate == "CREATED_BY" and (subject, object_) in false_attribution:
        if subject == "Claude Sonnet 5":
            return "The cited note says Claude Sonnet 4.5; the extracted model name is wrong."
        return "The source names a reference, article author, or document agent, not this entity's creator."

    if predicate == "HAS_OWNER" and (subject, object_) == (
        "Senior AI ENG", "sae-offer-architect"
    ):
        return "A document-writing agent is not the project owner."

    false_dependencies = {
        "Digital Omnibus agreement", "Offer 0", "Offer 1", "Offer 2", "Offer 3",
        "Anthropic", "Chip Huyen", "Hamel Husain", "AI Act",
        "CBS (Centraal Bureau voor de Statistiek)", "Exact MKB Barometer",
    }
    if predicate == "DEPENDS_ON" and subject == "Senior AI ENG" and object_ in false_dependencies:
        return "The note cites context, a reading source, or an offer; it does not establish a project dependency."

    harness_options = {
        "DuckDB", "SQLite", "Kuzu", "LanceDB", "Amazon DynamoDB", "pgvector", "Neo4j"
    }
    sae_topics_and_options = {
        "RAG", "GraphRAG", "LLM", "AIOS", "Recursive Language Models",
        "Vector and Graph Databases", "KVK Handelsregister", "LinkedIn company search",
        "Exact Online", "AFAS", "Twinfield", "Azure OpenAI", "Claude on AWS Bedrock",
    }
    if predicate == "USES":
        if (subject, object_) == ("Personal Harness", "Amazon Bedrock"):
            return "Bedrock calls belong to the JobLab backend; Harness source code has no Bedrock integration."
        if (subject, object_) == ("Personal Harness", "Quality Loop"):
            return "The note compares Quality Loop ideas and says a key rule should still be adopted."
        if subject == r"D:\Harness" and object_ in harness_options:
            return "The source compares database options; it does not say Harness uses this one."
        if subject == "Senior AI ENG" and object_ in sae_topics_and_options:
            return "The workspace studies or proposes this topic; actual use is not established."
        if subject == "Order-to-Plan":
            return "The curriculum specifies a future build; it is not implementation evidence."
        if (subject, object_) in {
            ("AI Harness", "GraphRAG"),
            ("AI Harness", "Vector and Graph Databases"),
            ("Personal Harness", "GraphRAG"),
            ("JobPilot", "Snowflake"),
            ("JobLab", "FastMCP"),
        }:
            return "An article idea, learning goal, or different project was mistaken for current use."

    if predicate == "USES_ARCHITECTURE" and (subject, object_) == (
        "Personal Harness", "Managed Agents"
    ):
        return "The note compares architectural principles and future design influence, not an implemented pattern."

    false_deployments = {
        ("JobLab Data Pipeline", "Terraform and CloudFormation"),
        ("JobPilot", "Amazon Bedrock"),
        ("n8n", "Hetzner"),
        ("Postgres 17 with pgvector", "Hetzner"),
    }
    if predicate == "DEPLOYED_ON" and (subject, object_) in false_deployments:
        return "The source describes IaC, a different system, or a proposed hosting option."

    if predicate == "BELONGS_TO" and subject == "cwc-long-running-agents":
        return "This is an external reference repository, not part of Senior AI ENG."
    if predicate == "IMPLEMENTS" and (subject, object_) == (r"D:\Harness", "RAG"):
        return "The note discusses retrieval design; it does not verify a working RAG implementation."
    if predicate == "PRODUCES" and (subject, object_) in {
        ("JobLab", "Career Evidence Base"), ("Order-to-Plan", "Measured Pilot")
    }:
        return "Evidence feeds the career record, or a planned flagship feeds an offer; neither is a produced artifact here."
    if predicate == "RELATED_TO" and (
        (subject, object_) == ("Senior AI ENG", "Ralph Wiggum")
        or subject == "sae-offer-architect"
    ):
        return "A technique title or document agent was mistaken for a meaningful person relationship."
    if predicate == "USES_ARCHITECTURE" and (subject, object_) == (
        "Terraform", "Build and Handover"
    ):
        return "The offer may use Terraform; Terraform does not use the offer's architecture."
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Close reviewed facts and write audit report")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH,
                        help="Audit report path for this review pass")
    args = parser.parse_args()

    database = Database()
    with database.session() as session:
        selected = [(row, why) for row in session.execute(
            QUERY, {"root_id": ROOT_ID, "project_id": PROJECT_ID}
        ) if (why := reason(row)) is not None]
        print(f"reviewed candidates: {len(selected)}")
        for row, why in selected:
            print(f"{row.id} | {row.subject} -[{row.predicate}]-> {row.object} | {row.path} | {why}")
        if not args.apply or not selected:
            return

        now = utc_now()
        episode = EpisodeRepo(session).create(Episode(
            id=new_id(), type=EpisodeType.MANUAL, project_id=PROJECT_ID,
            title="Senior AI ENG semantic-edge evidence review, 2026-09-21",
            body=f"Closed {len(selected)} unsupported current facts. Audit: {args.report.as_posix()}",
            observed_at=now, status=EpisodeStatus.EXTRACTED,
            engine=EngineKind.DETERMINISTIC, origin=Origin.INTERNAL, tier=Tier.KNOWLEDGE,
        ))
        for row, _why in selected:
            session.execute(text(
                "UPDATE facts SET valid_to=:at, status='historical', invalidated_at=:at, "
                "invalidated_by_episode_id=:episode WHERE id=:id AND valid_to IS NULL"
            ), {"at": now, "episode": episode.id, "id": row.id})

    lines = [
        "# Senior AI ENG semantic-edge review — 2026-09-21", "",
        f"Reviewed root: `{ROOT_ID}`. Closed {len(selected)} unsupported current facts.",
        "Original facts, source provenance, and validity history remain in PostgreSQL. "
        "Neo4j must be rebuilt after this review.", "",
        "The review closes only relationships whose predicate makes a stronger claim than "
        "the cited note supports. A source mention, reading reference, design option, or "
        "planned build is not evidence of a current dependency, implementation, deployment, "
        "ownership, or project containment.", "",
        "| Fact ID | Edge | Source | Reason |", "|---|---|---|---|",
    ]
    for row, why in selected:
        edge = f"{row.subject} → {row.predicate} → {row.object}".replace("|", "\\|")
        lines.append(f"| `{row.id}` | {edge} | `{row.path}` | {why} |")
    args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"closed: {len(selected)}; review episode: {episode.id}; report: {args.report}")


if __name__ == "__main__":
    main()
