"""Retrieval gold-set scoring (plan section Y, ADR-0010; P14-T03, owner A12/A09).

Pure functions over the **frozen** DTOs in ``packages/aimemory/domain/retrieval.py``
(:class:`~aimemory.domain.retrieval.SearchResult`, :class:`~aimemory.domain.retrieval.ScoredHit`).
Nothing here talks to a database, the Gateway, or an LLM - that is deliberate: this module can be
written and unit-tested with synthetic ``SearchResult`` objects today, before P9 (retrieval) exists,
and P14-T03's ``scripts/eval`` / ``run_eval.py`` will call it unchanged once real results are available.

Every score is one of:

* ``bool | None`` - ``None`` means "the gold question does not make a claim this metric can check"
  (e.g. a question with no ``expected_sources_any`` has no hit@k verdict), never "failed".
* ``float | None`` in ``[0.0, 1.0]`` - a presence/coverage fraction, same "None = not applicable" rule.

:func:`aggregate_scores` rolls a list of :class:`QuestionScore` into summary numbers for a report;
callers must label every number they print MEASURED (CLAUDE.md) since it comes from a real run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from aimemory.domain.retrieval import ScoredHit, SearchResult

__all__ = [
    "GoldQuestion",
    "QuestionScore",
    "aggregate_scores",
    "load_gold_set",
    "score_entity_presence",
    "score_expected_types",
    "score_facts_contain",
    "score_hit_at_k",
    "score_provenance_completeness",
    "score_question",
    "score_temporal_order",
    "timeline_order_correct",
]


# ====================================================================================================
# gold.yaml
# ====================================================================================================


@dataclass(frozen=True)
class GoldQuestion:
    """One row of ``tests/evaluation/gold.yaml``. Every field beyond ``id``/``question``/``author``
    is optional - a question makes only the claims it lists, and unmade claims score ``None`` rather
    than failing (see the module docstring)."""

    id: str
    question: str
    author: str = "owner"
    expected_sources_any: tuple[str, ...] = ()
    expected_entities: tuple[str, ...] = ()
    expected_types: tuple[str, ...] = ()
    expected_facts_contain: tuple[str, ...] = ()
    expected_timeline_order: tuple[str, ...] = ()
    provenance_required: bool = False
    fixture: str | None = None
    expect_absent: bool = False
    """True for a deliberately unanswerable question (plan section Y / the P14-T03 instruction to
    measure "should NOT answer" cases): nothing in the corpus supports it, so no ``expected_*`` field
    applies. Scored by :func:`score_question` via ``hit_count``/``top_score`` (no pass/fail threshold
    is invented - see the module docstring's MEASURED-not-guessed rule); the report groups these
    separately from answerable questions so a reader can compare, unassisted by any fabricated cutoff.
    """

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldQuestion:
        missing = {"id", "question"} - data.keys()
        if missing:
            raise ValueError(f"gold.yaml question missing required key(s): {sorted(missing)}")
        return cls(
            id=str(data["id"]),
            question=str(data["question"]),
            author=str(data.get("author", "owner")),
            expected_sources_any=tuple(data.get("expected_sources_any", ())),
            expected_entities=tuple(data.get("expected_entities", ())),
            expected_types=tuple(data.get("expected_types", ())),
            expected_facts_contain=tuple(data.get("expected_facts_contain", ())),
            expected_timeline_order=tuple(data.get("expected_timeline_order", ())),
            provenance_required=bool(data.get("provenance_required", False)),
            fixture=data.get("fixture"),
            expect_absent=bool(data.get("expect_absent", False)),
        )

    @property
    def makes_no_checkable_claim(self) -> bool:
        """True for a question with nothing this scorer can verify - a shape bug, not a valid row."""
        return not (
            self.expected_sources_any
            or self.expected_entities
            or self.expected_types
            or self.expected_facts_contain
            or self.expected_timeline_order
            or self.provenance_required
            or self.expect_absent
        )


def load_gold_set(path: Path) -> tuple[str, list[GoldQuestion]]:
    """Parse ``gold.yaml``. Returns ``(version, questions)``. Raises :class:`ValueError` (not a bare
    KeyError/TypeError) on a shape problem, with a message that names the offending question id."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "questions" not in raw:
        raise ValueError(f"{path}: expected a top-level mapping with a 'questions' list")
    version = str(raw.get("version", "0.0.0"))
    questions: list[GoldQuestion] = []
    seen_ids: set[str] = set()
    for entry in raw["questions"]:
        question = GoldQuestion.from_dict(entry)
        if question.id in seen_ids:
            raise ValueError(f"{path}: duplicate question id {question.id!r}")
        seen_ids.add(question.id)
        questions.append(question)
    return version, questions


# ====================================================================================================
# Per-metric scoring
# ====================================================================================================


def score_hit_at_k(result: SearchResult, expected_sources_any: Sequence[str], k: int = 5) -> bool | None:
    """True if any of ``expected_sources_any`` is the source of one of the top-``k`` hits."""
    if not expected_sources_any:
        return None
    top_k = result.hits[:k]
    got_sources = {hit.provenance.source_uri for hit in top_k if hit.provenance.source_uri}
    return any(uri in got_sources for uri in expected_sources_any)


def _hit_haystack(hit: ScoredHit) -> str:
    parts = [hit.text or "", hit.title or "", hit.citation or ""]
    return " \n".join(parts)


def score_entity_presence(result: SearchResult, expected_entities: Sequence[str]) -> float | None:
    """Fraction of ``expected_entities`` that appear (case-insensitive substring) in the hit text/
    titles or in the graph-expansion ``related_entities`` names."""
    if not expected_entities:
        return None
    blob = " \n".join(_hit_haystack(hit) for hit in result.hits)
    blob += " \n" + " \n".join(entity.name for entity in result.related_entities)
    blob = blob.lower()
    found = sum(1 for entity in expected_entities if entity.lower() in blob)
    return found / len(expected_entities)


def score_expected_types(result: SearchResult, expected_types: Sequence[str]) -> float | None:
    """Fraction of ``expected_types`` (artifact/entity type names) present among the hits' own
    ``object_type`` or the artifact-type hinted at in their citation/title text."""
    if not expected_types:
        return None
    observed = {str(hit.object_type).lower() for hit in result.hits}
    blob = " \n".join(_hit_haystack(hit) for hit in result.hits).lower()
    found = sum(
        1 for t in expected_types if t.lower() in observed or t.lower() in blob
    )
    return found / len(expected_types)


def score_facts_contain(result: SearchResult, expected_facts_contain: Sequence[str]) -> float | None:
    """Fraction of expected substrings (e.g. ``"parked"``) present somewhere in the returned text."""
    if not expected_facts_contain:
        return None
    blob = " \n".join(hit.text for hit in result.hits).lower()
    found = sum(1 for phrase in expected_facts_contain if phrase.lower() in blob)
    return found / len(expected_facts_contain)


def score_provenance_completeness(result: SearchResult) -> float:
    """Plan section Y's acceptance criterion: the fraction of returned hits whose provenance can be
    walked back to a source version (:attr:`Provenance.is_complete`). ``1.0`` (vacuously complete)
    when there are no hits - a question that returns nothing is a recall problem, not a provenance one."""
    if not result.hits:
        return 1.0
    complete = sum(1 for hit in result.hits if hit.provenance.is_complete)
    return complete / len(result.hits)


def timeline_order_correct(events: Sequence[tuple[str, datetime | None]], expected_order: Sequence[str]) -> bool:
    """Given ``(label, valid_from)`` pairs (as extracted from a result), True iff every label in
    ``expected_order`` that is present appears in that relative order once sorted by ``valid_from``
    (events with no timestamp sort last and cannot satisfy an ordering claim)."""
    if not expected_order:
        return True
    dated = sorted(events, key=lambda pair: (pair[1] is None, pair[1]))
    order_seen = [label for label, _ in dated if label in expected_order]
    expected_present = [label for label in expected_order if label in order_seen]
    # The subsequence of expected labels that were actually observed must appear in the same
    # relative order they were observed in `dated`.
    positions = [order_seen.index(label) for label in expected_present]
    return positions == sorted(positions)


def score_temporal_order(result: SearchResult, expected_timeline_order: Sequence[str]) -> bool | None:
    """:func:`timeline_order_correct` applied to a real :class:`SearchResult`: a hit "is" one of the
    expected labels if its title or text contains that label verbatim (case-insensitive)."""
    if not expected_timeline_order:
        return None
    events: list[tuple[str, datetime | None]] = []
    for hit in result.hits:
        haystack = _hit_haystack(hit).lower()
        for label in expected_timeline_order:
            if label.lower() in haystack:
                events.append((label, hit.valid_from))
    return timeline_order_correct(events, expected_timeline_order)


# ====================================================================================================
# Aggregate per-question / whole-run
# ====================================================================================================


@dataclass
class QuestionScore:
    """Every field is ``None`` when the question made no claim that metric checks (see module
    docstring) - callers computing an average must filter ``None`` out themselves, which is why
    :func:`aggregate_scores` exists rather than a single blended number."""

    question_id: str
    author: str
    hit_at_5: bool | None = None
    entity_presence: float | None = None
    type_presence: float | None = None
    facts_presence: float | None = None
    provenance_completeness: float | None = None
    temporal_correct: bool | None = None
    expect_absent: bool = False
    hit_count: int = 0
    top_score: float | None = None
    notes: list[str] = field(default_factory=list)


def score_question(question: GoldQuestion, result: SearchResult) -> QuestionScore:
    score = QuestionScore(
        question_id=question.id,
        author=question.author,
        hit_at_5=score_hit_at_k(result, question.expected_sources_any, k=5),
        entity_presence=score_entity_presence(result, question.expected_entities),
        type_presence=score_expected_types(result, question.expected_types),
        facts_presence=score_facts_contain(result, question.expected_facts_contain),
        temporal_correct=score_temporal_order(result, question.expected_timeline_order),
        expect_absent=question.expect_absent,
        hit_count=len(result.hits),
        top_score=result.hits[0].score if result.hits else None,
    )
    if question.provenance_required or result.hits:
        score.provenance_completeness = score_provenance_completeness(result)
    if question.provenance_required and score.provenance_completeness is not None and score.provenance_completeness < 1.0:
        score.notes.append("provenance_required but completeness < 100%")
    return score


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def aggregate_scores(scores: Sequence[QuestionScore]) -> dict[str, Any]:
    """Summary numbers for a report. Every value here is MEASURED once computed from a real run -
    label it that way at the call site (CLAUDE.md); this function does not know whether its inputs
    came from a live retrieval run or a synthetic test fixture."""
    hit_values = [s.hit_at_5 for s in scores if s.hit_at_5 is not None]
    entity_values = [s.entity_presence for s in scores if s.entity_presence is not None]
    provenance_values = [s.provenance_completeness for s in scores if s.provenance_completeness is not None]
    temporal_values = [s.temporal_correct for s in scores if s.temporal_correct is not None]
    answerable = [s for s in scores if not s.expect_absent]
    absent = [s for s in scores if s.expect_absent]
    return {
        "n_questions": len(scores),
        "hit_at_5_rate": _mean(1.0 if v else 0.0 for v in hit_values),
        "hit_at_5_n": len(hit_values),
        "entity_presence_mean": _mean(entity_values),
        "entity_presence_n": len(entity_values),
        "provenance_completeness_mean": _mean(provenance_values),
        "provenance_completeness_n": len(provenance_values),
        "provenance_completeness_is_100pct": all(v >= 1.0 for v in provenance_values) if provenance_values else None,
        "temporal_correctness_rate": _mean(1.0 if v else 0.0 for v in temporal_values),
        "temporal_correctness_n": len(temporal_values),
        # Answerable- vs absent-question contrast (plan section Y / P14-T03's "should NOT answer"
        # requirement). No pass/fail threshold is invented here - raw MEASURED numbers only; the
        # report shows both groups side by side and lets the reader judge discriminative power.
        "answerable_n": len(answerable),
        "answerable_hit_count_mean": _mean(float(s.hit_count) for s in answerable) if answerable else None,
        "answerable_top_score_mean": _mean(s.top_score for s in answerable if s.top_score is not None) or None,
        "absent_n": len(absent),
        "absent_hit_count_mean": _mean(float(s.hit_count) for s in absent) if absent else None,
        "absent_top_score_mean": _mean(s.top_score for s in absent if s.top_score is not None) or None,
    }
