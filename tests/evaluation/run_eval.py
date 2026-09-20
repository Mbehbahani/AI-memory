"""P14-T03 gold-set evaluation runner (ADR-0010, plan section Y; owner A12).

Usage — run inside the ``tools`` container (shares the compose network with ``memory-api``, and has
``httpx``/``yaml``/``aimemory`` installed; the host Python is not targeted, see ``pyproject.toml``)::

    docker compose --profile tools run --rm tools python tests/evaluation/run_eval.py
    docker compose --profile tools run --rm tools python tests/evaluation/run_eval.py \\
        --gold tests/evaluation/gold.yaml --api-url http://memory-api:8000 --k 5 --out reports/evaluation-custom.md

What it does
------------
For every row in ``gold.yaml`` (see :mod:`scorer` for the schema): builds a ``SearchQuery``-shaped
JSON body, ``POST``\\s it to the live Memory Gateway's ``/v1/search``, parses the response back into a
:class:`~aimemory.domain.retrieval.SearchResult`, and scores it with :mod:`scorer`. Every number that
ends up in the written report comes from that real run against whatever corpus ``memory-api`` is
currently serving — this script never fabricates or estimates a metric (CLAUDE.md).

What it deliberately does **not** do
-------------------------------------
Rows marked ``fixture: mini-vault`` are **skipped**, not scored as a pass or a fail, and are listed
in the report under "Skipped (fixture questions)" with the reason. Running those would require
standing up an ephemeral corpus (ingest ``tests/fixtures/mini-vault`` into a throwaway
Postgres+Neo4j, query it, tear it down) that does not exist yet — that harness is P14-T05's frozen
``tests/evaluation/benchmark/`` work, not this script. Silently mapping a fixture question onto the
live corpus (which does not contain the fixture) would either always fail or, worse, coincidentally
pass for the wrong reason; skipping with a reason is the honest choice.

Exit code is always 0 (this is a measurement, not a gate); the report is the deliverable. A
connection failure to the Gateway is not swallowed — it aborts the run with a clear message, because
a report with every question silently scored "no hits" would look like a real (catastrophic) recall
number instead of what it actually is (the service was unreachable).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

# ``tests/evaluation`` has no ``__init__.py`` (see tests/README.md and test_scorer.py's own note), so
# this sibling import mirrors how test_scorer.py imports scorer.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scorer import (  # noqa: E402
    GoldQuestion,
    QuestionScore,
    aggregate_scores,
    load_gold_set,
    score_question,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = REPO_ROOT / "tests" / "evaluation" / "gold.yaml"
DEFAULT_REPORTS_DIR = REPO_ROOT / "reports"


def _default_api_url() -> str:
    """``GatewaySettings.url`` (env ``MEMORY_API_URL``, default ``http://memory-api:8000`` — the
    compose service DNS name, reachable from the ``tools`` container). Falls back to the host loopback
    mapping if ``aimemory`` is not importable (e.g. this script run outside any container)."""
    try:
        from aimemory.common.config import get_settings

        return get_settings().gateway.url
    except Exception:  # noqa: BLE001 - genuinely best-effort; --api-url overrides it either way
        return "http://127.0.0.1:8000"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="Path to gold.yaml")
    parser.add_argument("--api-url", type=str, default=None, help="Memory Gateway base URL")
    parser.add_argument("--k", type=int, default=5, help="SearchQuery.k (top hits requested)")
    parser.add_argument(
        "--expand",
        dest="expand",
        action="store_true",
        default=True,
        help="Enable graph expansion (default: on)",
    )
    parser.add_argument("--no-expand", dest="expand", action="store_false", help="Vector+keyword only")
    parser.add_argument("--out", type=Path, default=None, help="Report path (default: reports/evaluation-<ts>.md)")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout (s)")
    return parser.parse_args(argv)


# ====================================================================================================
# Querying the live Gateway
# ====================================================================================================


def _check_health(client: httpx.Client) -> dict[str, Any]:
    resp = client.get("/health", timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _search(client: httpx.Client, question: GoldQuestion, *, k: int, expand: bool) -> dict[str, Any]:
    body = {"query": question.question, "k": k, "expand": expand}
    resp = client.post("/v1/search", json=body)
    resp.raise_for_status()
    return resp.json()


# ====================================================================================================
# Run + report
# ====================================================================================================


def run(args: argparse.Namespace) -> Path:
    from aimemory.domain.retrieval import SearchResult  # deferred: only needed once we actually run

    api_url = args.api_url or _default_api_url()
    version, questions = load_gold_set(args.gold)

    started_at = datetime.now(UTC)
    scores: list[QuestionScore] = []
    skipped: list[tuple[GoldQuestion, str]] = []
    errors: list[tuple[GoldQuestion, str]] = []
    raw_by_id: dict[str, dict[str, Any]] = {}

    with httpx.Client(base_url=api_url, timeout=args.timeout) as client:
        try:
            health = _check_health(client)
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"BLOCKER: memory-api unreachable at {api_url!r} ({type(exc).__name__}: {exc}). "
                "Is the compose stack up (`docker compose ps`)? This run cannot produce a MEASURED "
                "report against an unreachable service."
            ) from exc

        for question in questions:
            if question.fixture:
                skipped.append((question, f"fixture={question.fixture!r} — no ephemeral-corpus harness in this script (see module docstring)"))
                continue
            try:
                raw = _search(client, question, k=args.k, expand=args.expand)
            except httpx.HTTPError as exc:
                errors.append((question, f"{type(exc).__name__}: {exc}"))
                continue
            raw_by_id[question.id] = raw
            result = SearchResult.model_validate(raw)
            scores.append(score_question(question, result))

    finished_at = datetime.now(UTC)
    summary = aggregate_scores(scores)

    out_path = args.out or (DEFAULT_REPORTS_DIR / f"evaluation-{started_at.strftime('%Y%m%dT%H%M%SZ')}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = _render_report(
        gold_version=version,
        api_url=api_url,
        health=health,
        started_at=started_at,
        finished_at=finished_at,
        args=args,
        questions=questions,
        scores=scores,
        skipped=skipped,
        errors=errors,
        summary=summary,
        raw_by_id=raw_by_id,
    )
    out_path.write_text(report, encoding="utf-8")
    return out_path


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_float(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _fmt_bool_or_none(value: bool | float | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:.2f}"


def _render_report(
    *,
    gold_version: str,
    api_url: str,
    health: dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
    args: argparse.Namespace,
    questions: list[GoldQuestion],
    scores: list[QuestionScore],
    skipped: list[tuple[GoldQuestion, str]],
    errors: list[tuple[GoldQuestion, str]],
    summary: dict[str, Any],
    raw_by_id: dict[str, dict[str, Any]],
) -> str:
    duration_s = (finished_at - started_at).total_seconds()
    lines: list[str] = []
    a = lines.append

    a(f"# Gold-set evaluation — {started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    a("")
    a(
        "All numbers below are MEASURED by querying the live Memory Gateway "
        f"(`{api_url}`) with `tests/evaluation/gold.yaml` v{gold_version} and scoring the real "
        "responses with `tests/evaluation/scorer.py`. Nothing here is estimated."
    )
    a("")
    a(f"- Run duration: **{duration_s:.1f} s** MEASURED (`{started_at.isoformat()}` → `{finished_at.isoformat()}`)")
    a(f"- Query params: `k={args.k}`, `expand={args.expand}`")
    a(
        "- Gateway `/health` at run start: "
        + ", ".join(f"{c['name']}={'ok' if c['ok'] else 'DOWN (' + str(c.get('detail')) + ')'}" for c in health.get("checks", []))
        + f" — DOCUMENTED (reported by the service, not independently verified by this script)"
    )
    a(f"- Questions in gold.yaml: **{len(questions)}** (owner-authored: {sum(1 for q in questions if q.author == 'owner')}, agent-authored: {sum(1 for q in questions if q.author == 'agent')})")
    a(f"- Scored: **{len(scores)}** · Skipped (fixture): **{len(skipped)}** · Errored (HTTP failure): **{len(errors)}**")
    a("")

    a("## Aggregate metrics (MEASURED, n as shown)")
    a("")
    a("| Metric | Value | n |")
    a("|---|---|---|")
    a(f"| hit@{args.k} on `expected_sources_any` | {_fmt_pct(summary['hit_at_5_rate'])} | {summary['hit_at_5_n']} |")
    a(f"| expected-entity presence (mean fraction found) | {_fmt_pct(summary['entity_presence_mean'])} | {summary['entity_presence_n']} |")
    a(f"| provenance completeness (mean fraction of hits fully traceable) | {_fmt_pct(summary['provenance_completeness_mean'])} | {summary['provenance_completeness_n']} |")
    a(f"| provenance completeness == 100% on every scored question | {_fmt_bool_or_none(summary['provenance_completeness_is_100pct'])} | {summary['provenance_completeness_n']} |")
    a(f"| temporal correctness (order matches `expected_timeline_order`) | {_fmt_pct(summary['temporal_correctness_rate'])} | {summary['temporal_correctness_n']} |")
    a("")

    a("## Answerable vs. deliberately-unanswerable questions (MEASURED, no invented threshold)")
    a("")
    a(
        "Plan section Y / the P14-T03 brief: a system that always returns something confident is worse "
        "than one that returns nothing for a question the corpus cannot answer. No pass/fail cutoff is "
        "invented here (CLAUDE.md forbids fabricating a metric) — the raw hit-count and top-hit-score "
        "means for the two groups are reported side by side; the reader judges discriminative power."
    )
    a("")
    a("| Group | n | mean hit count | mean top-1 score |")
    a("|---|---|---|---|")
    a(f"| Answerable (`expect_absent: false`) | {summary['answerable_n']} | {_fmt_float(summary['answerable_hit_count_mean'], 2)} | {_fmt_float(summary['answerable_top_score_mean'])} |")
    a(f"| Deliberately absent (`expect_absent: true`) | {summary['absent_n']} | {_fmt_float(summary['absent_hit_count_mean'], 2)} | {_fmt_float(summary['absent_top_score_mean'])} |")
    a("")

    a("## Per-question results")
    a("")
    a("| ID | Author | Question | hit@k | entity | type | facts | temporal | prov. | hits | top score |")
    a("|---|---|---|---|---|---|---|---|---|---|---|")
    score_by_id = {s.question_id: s for s in scores}
    for q in questions:
        if q.id in {sq.id for sq, _ in skipped}:
            continue
        s = score_by_id.get(q.id)
        if s is None:
            continue  # errored — reported separately below
        a(
            f"| {q.id} | {q.author} | {q.question[:70]}{'…' if len(q.question) > 70 else ''} | "
            f"{_fmt_bool_or_none(s.hit_at_5)} | {_fmt_bool_or_none(s.entity_presence)} | "
            f"{_fmt_bool_or_none(s.type_presence)} | {_fmt_bool_or_none(s.facts_presence)} | "
            f"{_fmt_bool_or_none(s.temporal_correct)} | {_fmt_bool_or_none(s.provenance_completeness)} | "
            f"{s.hit_count} | {_fmt_float(s.top_score, 3)} |"
        )
    a("")

    failing = [
        (q, s)
        for q in questions
        for s in [score_by_id.get(q.id)]
        if s is not None
        and not s.expect_absent
        and (s.hit_at_5 is False or (s.entity_presence is not None and s.entity_presence < 1.0))
    ]
    if failing:
        a("## Questions with a measured gap (examples, honest detail)")
        a("")
        for q, s in failing:
            a(f"### {q.id} — {q.question}")
            a("")
            a(f"- Expected sources: `{list(q.expected_sources_any) or 'n/a'}`")
            a(f"- Expected entities: `{list(q.expected_entities) or 'n/a'}`")
            a(f"- hit@{args.k}: {_fmt_bool_or_none(s.hit_at_5)} · entity presence: {_fmt_bool_or_none(s.entity_presence)}")
            raw = raw_by_id.get(q.id, {})
            got_sources = [h.get("provenance", {}).get("source_uri") for h in raw.get("hits", [])[:5]]
            a(f"- Top-{args.k} sources actually returned: `{got_sources}`")
            a("")

    if skipped:
        a("## Skipped (fixture questions)")
        a("")
        for q, reason in skipped:
            a(f"- **{q.id}** ({q.question!r}): {reason}")
        a("")

    if errors:
        a("## Errors (HTTP failures — excluded from aggregate metrics, not silently scored as failing hits)")
        a("")
        for q, reason in errors:
            a(f"- **{q.id}** ({q.question!r}): {reason}")
        a("")

    a("## Limitations of this run (honest, not swept under the rug)")
    a("")
    a(
        "- `expected_timeline_order` questions (e.g. Q08) can fail even when the underlying "
        "`facts.supersedes_fact_id` chain is correct, because several real facts share one "
        "`valid_from` timestamp (extraction time was used as a fallback, not a date stated in the "
        "text) — `score_temporal_order`'s stable sort then cannot recover the intended order. This is "
        "a real, measured property of the current corpus, not a scorer bug."
    )
    a(
        "- Q04 and Q06 (owner-authored, left unmodified) reference a JobLab DE `localfs://` source and "
        "the `mini-vault` fixture respectively; the corpus this run queried is vault-only (the JobLab "
        "DE repo has not been ingested) and this script does not stand up a fixture corpus (see module "
        "docstring), so Q04 is expected to score poorly and Q06 is skipped rather than silently passed."
    )
    a(
        "- `expected_types` matching is a best-effort substring check against hit text/title (see "
        "`scorer.score_expected_types`), not a guarantee the returned object is literally that "
        "artifact type — a look at the per-question table plus the raw response is the ground truth."
    )
    a("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    out_path = run(args)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
