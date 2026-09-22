# Senior AI ENG semantic-edge review — 2026-09-21

Reviewed root: `senior-ai-eng`. Closed 3 unsupported current facts.
Original facts, source provenance, and validity history remain in PostgreSQL. Neo4j must be rebuilt after this review.

The review closes only relationships whose predicate makes a stronger claim than the cited note supports. A source mention, reading reference, design option, or planned build is not evidence of a current dependency, implementation, deployment, ownership, or project containment.

| Fact ID | Edge | Source | Reason |
|---|---|---|---|
| `01a0c357-92b0-783b-8e1c-e46b98a8ae9e` | Personal Harness → USES → Amazon Bedrock | `06 Program/research/05-evidence-baseline.md` | Bedrock calls belong to the JobLab backend; Harness source code has no Bedrock integration. |
| `01a0c356-8f5e-70a6-9f64-40dcd5bddaf9` | Personal Harness → USES → Quality Loop | `01 Agents and Harness Engineering/Harness Engineering - Key Concepts and Sources.md` | The note compares Quality Loop ideas and says a key rule should still be adopted. |
| `01a0c356-8f5f-7a18-8807-a52e2d1a535f` | Personal Harness → USES_ARCHITECTURE → Managed Agents | `01 Agents and Harness Engineering/Harness Engineering - Key Concepts and Sources.md` | The note compares architectural principles and future design influence, not an implemented pattern. |
