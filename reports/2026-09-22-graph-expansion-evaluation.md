# Graph-expansion evaluation — paired live comparison — 2026-09-22

## Method

Two runs of the existing `tests/evaluation/run_eval.py` harness queried the live Memory Gateway with
the unchanged `tests/evaluation/gold.yaml` v0.2.0 (25 questions, 24 scored, one fixture skip).
Neo4j was healthy and populated for **both** runs. The no-expansion run is therefore the only
baseline in this report; `evaluation-20260918T043955Z.md` is not used as a before-graph comparison,
because it had `expand=True` while Neo4j was empty.

| Run | Command | Report |
|---|---|---|
| No expansion | `python tests/evaluation/run_eval.py --no-expand` | `evaluation-20260922T155622Z.md` |
| Graph expansion | `python tests/evaluation/run_eval.py` | `evaluation-20260922T155633Z.md` |

## Measured result

| Metric | No expansion | Graph expansion | Change |
|---|---:|---:|---:|
| hit@5 on expected sources (n=15) | 20.0% | 26.7% | +6.7 pp |
| Expected-entity presence (n=11) | 84.8% | 93.9% | +9.1 pp |
| Provenance completeness (n=24) | 100.0% | 100.0% | 0.0 pp |
| Questions with complete provenance | 24/24 | 24/24 | unchanged |
| Retrieval HTTP failures | 0 | 0 | unchanged |
| Temporal correctness (n=1) | 100.0% | 100.0% | unchanged |
| Run duration | 3.1 s | 2.8 s | not a controlled latency benchmark |

Graph expansion produced one additional expected-source hit (4/15 rather than 3/15) and improved
expected-entity presence. The result supports retaining the existing graph-expansion path; it does
not justify an embedding replacement or reranker change.

## Important limitations

- The gold set is only 25 questions and source hit@5 is measured on 15 questions, so this is
  directional evidence rather than a final retrieval-quality certification.
- Both generated evaluation reports retrieve AI Memory's historical evaluation reports prominently
  for several unrelated questions. This is corpus contamination/noise that suppresses source hit@5
  and should be investigated separately.
- The generated reports repeat an outdated limitation that JobLab DE is not ingested. Live state at
  this evaluation includes JobLab DE; that prose is stale and was not used to interpret the metrics.
- Deliberately absent questions still return five hits on average in both runs, with mean top-1
  scores of 0.810. Expansion does not resolve abstention/discriminative-quality behaviour.
