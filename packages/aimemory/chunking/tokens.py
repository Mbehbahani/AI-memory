"""Deterministic, documented token-count approximation (plan section L; MiniLM ``max_seq=256``).

No tokenizer ships at runtime in the ingestion image - MiniLM's WordPiece tokenizer lives only inside
the ``embedding-service`` container (``sentence-transformers/all-MiniLM-L6-v2``, baked offline for
network isolation). Carrying a second copy of that tokenizer (and its vocab file) into every
extractor/chunker call site would duplicate the dependency and could drift out of sync with whatever
model revision the embedding service actually runs.

Heuristic (calibrated, not guessed)::

    token_count = max(1, round(char_count / 3.4))

MEASURED calibration (2026-09-14): the real MiniLM WordPiece tokenizer
(``AutoTokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")``, ``HF_HUB_OFFLINE=1``)
was run inside ``aimemory/embedding-service:dev`` (the image with the model baked in) over 283
windows of ~800 characters sliced from ``docs/architecture/*.md``, ``docs/adr/*.md`` and
``packages/aimemory/domain/*.py`` - a mix of prose, tables and Python source representative of what
actually gets chunked. Candidate divisors compared by mean absolute percentage error (MAPE) against
the real token count:

.. code-block:: text

    char/4.0  -> MAPE 18.3 %      char/3.6 -> MAPE 12.1 %
    char/3.8  -> MAPE 14.8 %      char/3.4 -> MAPE 11.0 %  (median 8.4 %)  <- selected
    char/3.5  -> MAPE 11.3 %      char/3.2 -> MAPE 12.0 %

Word-count-based and hybrid (char+word) linear-regression formulas were all worse (23-47 % MAPE) on
this corpus, so the simple character-count divisor was kept.

This is accurate to roughly **+/-11 % mean absolute percentage error (MEASURED)** against the real
tokenizer - good enough to target ~200-token chunks with headroom under MiniLM's 256-token truncation
and to size a ~30-token overlap, both soft targets. It must never be reported as an exact token count
(e.g. for LLM context-window accounting, which should ask Ollama for its own count).
"""

from __future__ import annotations

__all__ = ["CHARS_PER_TOKEN", "chars_for_tokens", "count_tokens"]

CHARS_PER_TOKEN = 3.4


def count_tokens(text: str) -> int:
    """Approximate token count. See the module docstring for the calibration and its measured error."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def chars_for_tokens(tokens: int) -> int:
    """Inverse of :func:`count_tokens` - characters expected to produce ``tokens`` tokens.

    Used to size overlap windows in characters, since the chunkers only have string offsets to work
    with.
    """
    return max(0, round(tokens * CHARS_PER_TOKEN))
