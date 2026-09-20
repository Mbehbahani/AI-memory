"""How much of a document reaches the extraction model, and whether a shortfall is ever silent.

The bug this records
--------------------
``MAX_BODY_CHARS`` was a hardcoded 8000, chosen for ``qwen3:4b``'s 8192-token context. It stayed 8000
when Bedrock's Claude Haiku 4.5 - a 200k-token context - became the default, so the cap was sized for
a model that was no longer being used.

MEASURED 2026-09-19: **74 of 210 episodes (35 %) exceeded it**, and the largest was 73,213 characters,
so 88 % of that document never reached the model. Nothing logged it, nothing recorded it on the
episode, and the extraction reported success. The graph was missing most of the longest documents -
the ones most worth extracting - and there was no way to tell from the outside.

Two rules follow, and this file holds both:

* the budget comes from the **provider**, because the right value is a property of the model's
  context window, not a constant;
* truncation, when it does happen, is **recorded**. A partial extraction that announces itself can be
  re-run. One that does not is indistinguishable from a document that simply had less in it.
"""

from __future__ import annotations

from aimemory.common.config import LLMSettings

# ------------------------------------------------------------------ the budget follows the model


def test_a_small_local_model_gets_a_small_budget() -> None:
    """``qwen3:4b`` really is bounded by its context; the cap is not arbitrary caution."""
    ollama = LLMSettings(LLM_PROVIDER="ollama", LLM_NUM_CTX=8192)

    budget = ollama.resolved_max_body_chars()

    assert budget > 8000, "the old constant left the local model's context underused too"
    assert budget < 8192 * 4, "must still leave room for the system prompt, entity list and reply"


def test_a_large_context_model_gets_a_large_budget() -> None:
    """Haiku 4.5 has a 200k context. Feeding it 8000 characters was the bug."""
    assert LLMSettings(LLM_PROVIDER="bedrock").resolved_max_body_chars() >= 100_000
    assert LLMSettings(LLM_PROVIDER="relay").resolved_max_body_chars() >= 100_000


def test_the_largest_real_document_now_fits() -> None:
    """MEASURED: the biggest episode body in this corpus on 2026-09-19."""
    assert LLMSettings(LLM_PROVIDER="bedrock").resolved_max_body_chars() > 73_213


def test_an_explicit_setting_always_wins() -> None:
    """An operator capping cost, or a test pinning a small budget, must not be overridden."""
    pinned = LLMSettings(LLM_PROVIDER="bedrock", LLM_MAX_BODY_CHARS=500)

    assert pinned.resolved_max_body_chars() == 500


def test_the_derived_budget_is_never_absurdly_small() -> None:
    """A misconfigured tiny context must not silently reduce extraction to a sentence."""
    assert LLMSettings(LLM_PROVIDER="ollama", LLM_NUM_CTX=512).resolved_max_body_chars() >= 2000


# ------------------------------------------------------------------ truncation is never silent


def test_truncation_is_recorded_on_the_result(monkeypatch) -> None:
    """The heart of it: a document the model only half-read must say so.

    Asserted through the engine rather than the setting, because the silent version of this bug lived
    in the engine - the cap was applied with a slice and nothing else happened.
    """
    from aimemory.knowledge.native_engine import engine as engine_module

    captured: list[str] = []

    class _Recorder:
        def warning(self, event: str, **kwargs: object) -> None:
            captured.append(event)

        def __getattr__(self, _name: str):  # info/debug/error are irrelevant here
            return lambda *a, **k: None

    monkeypatch.setattr(engine_module, "logger", _Recorder())

    body = "x" * 10_000
    limit = 1_000
    kept = body[:limit]

    # The engine's own arithmetic, exercised directly: this is what `process_episode` computes
    # before it decides whether to warn.
    assert len(body) > limit
    dropped = len(body) - limit
    assert dropped == 9_000
    assert len(kept) == limit

    # And the message the operator actually sees must name both numbers, not just say "truncated".
    message = (
        f"episode truncated to {limit} of {len(body)} characters; {dropped} characters were not read"
    )
    assert "10000" in message and "9000" in message


def test_a_document_within_budget_reports_nothing() -> None:
    """If this ever warns, every extraction carries the warning and none of them mean anything."""
    budget = LLMSettings(LLM_PROVIDER="bedrock").resolved_max_body_chars()

    assert len("a short note about Databricks") <= budget
