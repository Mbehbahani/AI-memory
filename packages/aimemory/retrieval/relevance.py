"""Stage 5b: a *calibrated* relevance score for the fused candidates (P9 quality fix).

Why this module exists
----------------------
The P14 gold-set evaluation measured two defects that share one root cause (see
``reports/evaluation-20260918*.md``):

1. Reciprocal Rank Fusion is **rank-based and therefore scale-free**: the best hit of every query
   scores ``1 / (rrf_k + 1)`` whether its cosine similarity was 0.74 or 0.32. A query the corpus
   cannot answer produced exactly the same top score as one it answers perfectly, so the Gateway was
   incapable of expressing "I do not know this" - not because of the corpus, but by construction.
2. The whole dynamic range of an RRF score over a 40-candidate list is ``1/61 - 1/100 = 0.0064``,
   while the boosts of ``config/retrieval.yaml`` are ``±0.10 … ±0.15``. Every boost was therefore
   15-25x larger than the entire relevance signal it was supposed to *nudge*: the final order was a
   sort by boost group, with relevance acting only as a tie-break inside a group. MEASURED example
   (2026-09-18, live corpus): the Apache Tika clipping was the **rank-1 semantic candidate**
   (cosine 0.654) for the Tika question and came back **last** with ``score = -0.034``, because
   ``low_trust = -0.15`` swamped its ``rrf_score = 0.0164``. AC-6 asks for "clippings rank lower",
   not "clippings are unreachable".

The fix is to give the ranking stage a signal that has an absolute meaning and a [0, 1] scale, so
boosts can be applied *proportionally* (see :mod:`aimemory.retrieval.boosts`). This module computes
that signal from evidence the pipeline already has:

``semantic``
    The pgvector cosine similarity of the query embedding, clamped to [0, 1]. Candidates that the
    semantic retriever never returned (keyword-only hits) are imputed the *lowest* cosine that
    retriever did return - an upper bound on their true similarity, since they ranked below its
    cut-off - rather than 0.0, which would falsely bury them.

``lexical``
    The share of the query's **IDF-weighted** content lexemes that the candidate's text actually
    contains. This is deliberately a *coverage* measure and not a term-frequency one
    (``ts_rank_cd`` is TF-based): repeating "data" forty times must not beat mentioning "Portainer"
    once. It is what restores the notion of term rarity that a MiniLM embedding does not have -
    the direct answer to A12's finding that "generic, vocabulary-heavy documents outrank specific,
    on-topic ones", and to the fact that a question about Mars colonies scored as confidently as one
    about the user's own projects.

``relevance = W_SEMANTIC * semantic + W_LEXICAL * lexical`` with the weights renormalised over
whichever components are available, so keyword-only degraded mode still yields a [0, 1] score.

Weight choice, honestly
-----------------------
:data:`W_SEMANTIC` and :data:`W_LEXICAL` are both 0.5: two independent, equally-trusted channels,
each already normalised to [0, 1], and there is no labelled calibration set that would justify an
asymmetry. MEASURED sensitivity on the 24-question gold set (2026-09-18): 0.7/0.3, 0.5/0.5 and
0.3/0.7 all score the same hit@5 (10/15); the answerable-vs-absent score gap grows with the lexical
weight (+0.13 / +0.16 / +0.21). Picking the weight that maximises that gap on 24 questions would be
fitting noise, so the structural default stands and the sensitivity is reported instead.

Failure behaviour: every SQL here is best-effort. If the lexical statements fail, the caller keeps
the semantic component and adds a warning; relevance never raises and never blocks a search.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger
from ..domain.enums import ObjectType
from ..domain.retrieval import Candidate
from .types import HitKey

__all__ = [
    "DF_CAP",
    "LEXICAL_DEGRADED_WARNING",
    "W_LEXICAL",
    "W_SEMANTIC",
    "LexicalUnavailable",
    "QueryLexicon",
    "RelevanceScore",
    "analyse_query",
    "corpus_chunk_count",
    "lexical_coverage",
    "score_relevance",
    "semantic_scores_from",
]

logger = get_logger(__name__)

#: Blend weights of the two evidence channels (see the module docstring for why they are equal).
W_SEMANTIC = 0.5
W_LEXICAL = 0.5

#: Document frequencies are counted up to this many chunks per lexeme. Above the cap every term is
#: "common" anyway and the IDF difference is immaterial; the cap bounds the cost of a stop-word-ish
#: lexeme on a corpus far larger than V0.1's (MEASURED here: 4,757 chunks, 2-20 ms per query).
DF_CAP = 20_000

LEXICAL_DEGRADED_WARNING = "lexical relevance unavailable; semantic-only ranking"

_QUERY_LEXEMES_SQL = "SELECT lexeme FROM unnest(to_tsvector('english', CAST(:query_text AS text)))"

# ``format('%L', t.lexeme)`` quotes a *bound parameter* server-side; no SQL string is ever built
# from user input in this module.
_DOCUMENT_FREQUENCY_SQL = """
SELECT t.lexeme,
       (
           SELECT count(*)
             FROM (
                   SELECT 1
                     FROM chunks c
                    WHERE c.tsv @@ CAST(format('%L', t.lexeme) AS tsquery)
                    LIMIT :df_cap
                  ) AS capped
       ) AS df
  FROM unnest(CAST(:lexemes AS text[])) AS t(lexeme)
"""

_COVERAGE_SQL = """
WITH terms AS (
    SELECT lexeme, idf
      FROM unnest(CAST(:lexemes AS text[]), CAST(:idfs AS float8[])) AS t(lexeme, idf)
),
candidates AS (
    SELECT 'chunk' AS object_type, c.id AS object_id, c.tsv AS tsv
      FROM chunks c
     WHERE c.id = ANY(CAST(:chunk_ids AS uuid[]))
    UNION ALL
    SELECT 'artifact', a.id, to_tsvector('english', a.title || ' ' || a.body)
      FROM knowledge_artifacts a
     WHERE a.id = ANY(CAST(:artifact_ids AS uuid[]))
)
SELECT candidates.object_type,
       candidates.object_id,
       sum(terms.idf) / CAST(:total_idf AS float8) AS coverage
  FROM candidates
  JOIN terms ON candidates.tsv @@ CAST(format('%L', terms.lexeme) AS tsquery)
 GROUP BY 1, 2
"""

_CHUNK_COUNT_SQL = "SELECT count(*) FROM chunks"


class LexicalUnavailable(RuntimeError):
    """The lexical component could not be computed; the caller degrades with a warning."""


@dataclass(frozen=True, slots=True)
class QueryLexicon:
    """The query's content lexemes with their corpus IDF.

    ``idf[lexeme] = ln(1 + N / (1 + df))`` (the always-positive Robertson form). A lexeme the corpus
    has never seen keeps its full weight in :attr:`total_idf`: that is precisely what makes a
    question about something absent ("Mars colony") score low - nothing can cover the terms that
    matter most in it.
    """

    lexemes: tuple[str, ...] = ()
    idf: Mapping[str, float] = field(default_factory=dict)
    corpus_size: int = 0

    @property
    def total_idf(self) -> float:
        return sum(self.idf.values())

    def __bool__(self) -> bool:
        return bool(self.lexemes) and self.total_idf > 0.0


@dataclass(frozen=True, slots=True)
class RelevanceScore:
    """One candidate's calibrated relevance and the parts it was made of."""

    relevance: float
    parts: dict[str, float]


def corpus_chunk_count(session: Session) -> int:
    """``count(*)`` over ``chunks`` - the ``N`` of the IDF. Best-effort; 0 disables IDF weighting."""
    try:
        return int(session.execute(text(_CHUNK_COUNT_SQL)).scalar_one())
    except Exception as exc:  # noqa: BLE001 - relevance degrades, it never fails a search
        logger.warning("retrieval.corpus_size_failed", error=type(exc).__name__)
        return 0


def analyse_query(session: Session, query_text: str, *, corpus_size: int | None = None) -> QueryLexicon:
    """Stem the query with the same ``english`` configuration the index uses and weight it by IDF.

    Returns an empty lexicon (falsy) when the query reduces to stop words or punctuation, or when
    the statements fail - the caller then ranks on the semantic component alone.
    """
    try:
        rows = session.execute(text(_QUERY_LEXEMES_SQL), {"query_text": query_text}).all()
    except Exception as exc:  # noqa: BLE001 - a stop-word-only query is not an error
        logger.warning("retrieval.query_lexemes_failed", error=type(exc).__name__)
        return QueryLexicon()
    lexemes = tuple(str(row[0]) for row in rows)
    if not lexemes:
        return QueryLexicon()

    size = corpus_chunk_count(session) if corpus_size is None else corpus_size
    if size <= 0:
        return QueryLexicon()

    try:
        df_rows = session.execute(
            text(_DOCUMENT_FREQUENCY_SQL), {"lexemes": list(lexemes), "df_cap": DF_CAP}
        ).all()
    except Exception as exc:  # noqa: BLE001 - IDF is best-effort; the caller ranks without it
        logger.warning("retrieval.document_frequency_failed", error=type(exc).__name__)
        return QueryLexicon()

    frequencies = {str(row[0]): int(row[1]) for row in df_rows}
    idf = {
        lexeme: math.log(1.0 + size / (1.0 + frequencies.get(lexeme, 0))) for lexeme in lexemes
    }
    return QueryLexicon(lexemes=lexemes, idf=idf, corpus_size=size)


def lexical_coverage(
    session: Session,
    lexicon: QueryLexicon,
    keys: Iterable[HitKey],
) -> dict[HitKey, float]:
    """Fraction of the query's IDF mass that each candidate's own text contains, in [0, 1].

    One statement for the whole candidate pool (``candidates.fused_top_k`` rows), evaluated against
    the chunk's stored ``tsv`` (so it agrees with the keyword retriever) and the artifact's
    ``title || ' ' || body`` (the expression ``ix_artifacts_tsv`` indexes). Candidates with no
    matching lexeme are reported as 0.0 rather than omitted, so a caller can tell "covers nothing"
    from "was not scored".
    """
    key_list = list(keys)
    zeroed = {key: 0.0 for key in key_list}
    if not key_list or not lexicon:
        return zeroed

    chunk_ids = [str(key[1]) for key in key_list if key[0] is ObjectType.CHUNK]
    artifact_ids = [str(key[1]) for key in key_list if key[0] is ObjectType.ARTIFACT]
    params = {
        "lexemes": list(lexicon.lexemes),
        "idfs": [lexicon.idf[lexeme] for lexeme in lexicon.lexemes],
        "total_idf": lexicon.total_idf,
        "chunk_ids": chunk_ids,
        "artifact_ids": artifact_ids,
    }
    try:
        rows = session.execute(text(_COVERAGE_SQL), params).all()
    except Exception as exc:  # re-raised below; the caller degrades with a warning
        logger.warning("retrieval.lexical_coverage_failed", error=type(exc).__name__)
        raise LexicalUnavailable(str(exc)) from exc

    by_id = {(str(row[0]), str(row[1])): float(row[2]) for row in rows}
    for key in key_list:
        value = by_id.get((key[0].value, str(key[1])))
        if value is not None:
            zeroed[key] = max(0.0, min(1.0, value))
    return zeroed


def score_relevance(
    keys: Sequence[HitKey],
    *,
    semantic_scores: Mapping[HitKey, float] | None = None,
    lexical_scores: Mapping[HitKey, float] | None = None,
    rrf_scores: Mapping[HitKey, float] | None = None,
) -> dict[HitKey, RelevanceScore]:
    """Blend the available components into one [0, 1] relevance per candidate.

    * ``semantic_scores`` are raw cosine similarities from the semantic retriever's own list.
      Candidates absent from it are imputed that list's **minimum** similarity (they ranked below
      its cut-off, so the minimum is an upper bound), which keeps keyword-only hits comparable
      without pretending they scored zero.
    * ``lexical_scores`` come from :func:`lexical_coverage`; ``None`` means the channel is
      unavailable (degraded, or a stop-word-only query) and the weights renormalise to semantic.
    * ``rrf_scores`` is the last-resort ordering when *neither* channel exists (both retrievers
      degraded): the RRF value normalised by its own maximum, so the result is still in [0, 1] and
      still ordered like the fusion stage, just not calibrated.
    """
    semantic = dict(semantic_scores or {})
    imputed = min(semantic.values()) if semantic else 0.0
    max_rrf = max(rrf_scores.values()) if rrf_scores else 0.0

    scores: dict[HitKey, RelevanceScore] = {}
    for key in keys:
        parts: dict[str, float] = {}
        weighted = 0.0
        weight_total = 0.0
        if semantic:
            value = max(0.0, min(1.0, semantic.get(key, imputed)))
            parts["semantic"] = W_SEMANTIC * value
            weighted += W_SEMANTIC * value
            weight_total += W_SEMANTIC
        if lexical_scores is not None:
            value = max(0.0, min(1.0, lexical_scores.get(key, 0.0)))
            parts["lexical"] = W_LEXICAL * value
            weighted += W_LEXICAL * value
            weight_total += W_LEXICAL
        if weight_total <= 0.0:
            fallback = 0.0
            if rrf_scores and max_rrf > 0.0:
                fallback = max(0.0, min(1.0, rrf_scores.get(key, 0.0) / max_rrf))
            scores[key] = RelevanceScore(relevance=fallback, parts={"rrf_normalised": fallback})
            continue
        # Renormalise so a missing channel rescales the survivors instead of halving every score.
        relevance = weighted / weight_total
        parts = {name: value / weight_total for name, value in parts.items()}
        scores[key] = RelevanceScore(relevance=relevance, parts=parts)
    return scores


def semantic_scores_from(candidates: Iterable[Candidate]) -> dict[HitKey, float]:
    """``{(object_type, object_id): cosine}`` from a retriever output's candidate list."""
    return {
        (candidate.object_type, candidate.object_id): float(candidate.raw_score)
        for candidate in candidates
    }
