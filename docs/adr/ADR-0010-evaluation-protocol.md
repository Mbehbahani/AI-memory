# ADR-0010 — Ongoing evaluation protocol and model-upgrade rule

Status: accepted · Date: 2026-09-14 · Deciders: owner (Mohammad), A00

## Context
Qwen3 4B on CPU is the extraction model by requirement, but its adequacy is unknown and may change as
sources grow. The build-time checks (P4 measurements, Graphiti gate, P14 gold set) are one-time. The
owner needs a way to see, during day-to-day use, whether extraction quality is acceptable and when a
better model would be justified — without any silent model change.

## Decision
1. **Continuous metrics**: after every ingestion run and every `scripts/eval` run, a row is written to
   `metrics_snapshots` (validity rate, failed-episode share, seconds/episode, duplicate-entity rate,
   unconfirmed share, coverage per project, gold-set scores). Shown in `aimemory-ingest status` and a
   NeoDash "Health" page.
2. **Human review**: `aimemory-ingest review --sample N` presents random recent artifacts/facts with
   evidence quote and citation; verdicts (`accept | wrong | partial`) go to `extraction_reviews`.
   Acceptance rate is the primary extraction-quality number.
3. **Permanent benchmark set**: the gate episodes plus the A/B fixture, with hand-listed expectations,
   frozen in `tests/evaluation/benchmark/`; `scripts/eval --benchmark --model <name>` produces a
   comparable report for any model.
4. **Upgrade rule**: acceptance < 70 % over two consecutive reviews, or validity < 85 % over two runs,
   triggers a notice to the owner and a benchmark run of the candidate model only. Switching
   `LLM_MODEL` requires owner approval after comparing the two benchmark reports. Both models remain in
   `extraction_models`; every fact records which model produced it.

## Consequences
+ Quality is visible, comparable, and decided by the owner. + A future model change is a config change
with an audit trail. − Adds two tables, a CLI command, a NeoDash page, and ~15 minutes/week of review.
