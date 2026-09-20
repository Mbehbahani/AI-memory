"""What happens to an episode when the *second* extraction call fails.

The bug this records
--------------------
Extraction is two calls: entities/artifacts, then relationships. When call 2 raised, the engine
caught it, appended an error string, returned an empty fact list - and then finished the episode with
``valid=True``. The episode was marked ``extracted``, kept its entities, left the queue, and was never
looked at again. It had **no relationships, permanently**, and nothing on screen said so.

"The model found no relationships here" and "nobody asked the model" produce the same empty list.
Only the first is a result. Treating the second as one means a single Bedrock timeout silently costs
a document its entire contribution to the graph - and the corpus-wide symptom is a graph that looks
thin for no discoverable reason.

Found 2026-09-19 while wiring the relay provider, where call 2 fails *by design* on the first pass:
4 joblab-de episodes were marked ``extracted`` with entities, artifacts and zero facts.

The rule
--------
Entities and artifacts from call 1 are real knowledge and are kept either way. But an episode whose
relationship call never completed is **not valid**, so it stays queued and gets another attempt.
"""

from __future__ import annotations

from typing import Any

import pytest

from aimemory.domain.enums import EpisodeType
from aimemory.domain.models import Episode, ExtractionModel
from aimemory.domain.ports import LLMResponse
from aimemory.common.ids import new_id
from aimemory.common.time import utc_now

ENTITY_ANSWER = {
    "doc_kind": "readme",
    "summary": "A repository that builds a lakehouse from job postings.",
    "entities": [
        {"name": "JobLab Lakehouse", "type": "Project"},
        {"name": "Delta Lake", "type": "Technology"},
    ],
    "artifacts": [],
}


class _Provider:
    """Answers call 1, then does whatever ``on_relationship`` says for call 2."""

    def __init__(self, on_relationship: Any) -> None:
        self._on_relationship = on_relationship
        self.calls = 0

    def complete_json(self, prompt: str, json_schema: dict, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        title = (json_schema or {}).get("title", "")
        if title == "EpisodeExtraction" or self.calls == 1:
            return LLMResponse(text="{}", parsed=ENTITY_ANSWER, model="test-model")
        return self._on_relationship()

    def complete_text(self, prompt: str, **_kwargs: Any) -> LLMResponse:  # pragma: no cover
        return LLMResponse(text="", model="test-model")

    def model_identity(self) -> ExtractionModel:
        return ExtractionModel(id="test-model", provider="test", name="test-model")


@pytest.fixture()
def episode() -> Episode:
    return Episode(
        id=new_id(),
        type=EpisodeType.DOCUMENT,
        body="JobLab Lakehouse uses Delta Lake for its silver tables.",
        observed_at=utc_now(),
        title="README.md",
    )


def _run(provider: _Provider, episode: Episode):
    from aimemory.knowledge.native_engine.engine import NativeTemporalEngine
    from aimemory.retrieval.types import HitKey  # noqa: F401 - import parity with the engine module

    engine = NativeTemporalEngine(provider=provider, allow_relationship_retry=False)
    context = _context()
    return engine.process_episode(episode, context)


def _context():
    from aimemory.domain.ports import EpisodeContext

    return EpisodeContext(project_id=None, doc_title="README.md", known_entity_names=[])


# ------------------------------------------------------------------ the bug


def test_a_failed_relationship_call_does_not_count_as_a_successful_extraction(episode) -> None:
    """The regression. ``valid=True`` here is what retired episodes with no edges."""

    def boom() -> LLMResponse:
        raise TimeoutError("relationship call timed out")

    result = _run(_Provider(boom), episode)

    assert result.valid is False, (
        "an episode whose relationship call never completed must stay queued; marking it valid "
        "retires it permanently with entities and no edges"
    )
    assert any("relationship call failed" in e for e in result.errors)


def test_the_entities_from_call_one_are_still_kept(episode) -> None:
    """Failing the episode must not throw away knowledge that was genuinely produced."""

    def boom() -> LLMResponse:
        raise TimeoutError("relationship call timed out")

    result = _run(_Provider(boom), episode)

    assert [e.name for e in result.entities] == ["JobLab Lakehouse", "Delta Lake"]
    assert result.summary


# ------------------------------------------------------------------ the case that is NOT a failure


def test_a_model_that_genuinely_finds_no_relationships_is_a_valid_result(episode) -> None:
    """The distinction the fix turns on.

    A document that states no relationships is correctly extracted with zero facts. If this were
    treated as a failure the episode would be retried forever and the queue would never drain.
    """

    def empty() -> LLMResponse:
        return LLMResponse(text="{}", parsed={"facts": []}, model="test-model")

    result = _run(_Provider(empty), episode)

    assert result.valid is True
    assert list(result.facts) == []
    assert not any("relationship call failed" in e for e in result.errors)


def test_the_failure_flag_does_not_leak_between_episodes(episode) -> None:
    """Engine state is reused across a queue drain; a stuck flag would fail every later episode."""

    def boom() -> LLMResponse:
        raise TimeoutError("relationship call timed out")

    from aimemory.knowledge.native_engine.engine import NativeTemporalEngine

    engine = NativeTemporalEngine(provider=_Provider(boom), allow_relationship_retry=False)
    first = engine.process_episode(episode, _context())
    assert first.valid is False

    engine._provider = _Provider(
        lambda: LLMResponse(text="{}", parsed={"facts": []}, model="test-model")
    )
    second = engine.process_episode(episode, _context())

    assert second.valid is True, "the previous episode's failure must not carry over"
