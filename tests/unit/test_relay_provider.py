"""The relay provider: Claude Haiku 4.5 answered by a Claude Code subagent instead of by Bedrock.

Why it exists
-------------
The extraction engine runs inside a container and cannot call a subagent. So the call is turned
inside out: the prompt is written to a file, the operator (or a subagent) answers it, and the run is
repeated. The same prompt hashes to the same key, so the second run finds its answer and proceeds.

What these tests protect
------------------------
The value of doing this at the ``LLMProvider`` seam is that *nothing else changes* - entity
resolution, the temporal rules, the registry veto and provenance all still apply, because only the
model connection was swapped. Two properties make that true, and both are easy to lose:

* **Determinism.** Identical prompt, identical key. If keying drifted, a second run would re-ask
  everything and the two-pass cycle would never converge.
* **Failure, not guessing.** A missing or malformed answer must raise. A provider that returned an
  empty result on a missing file would report a successful extraction of a document no model ever
  read - the exact silent-partial-result failure this system is built to avoid, and the same class of
  bug as the 8000-character truncation that went unnoticed for two days.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aimemory.providers.llm.relay_provider import (
    MODEL_ID,
    RelayPending,
    RelayProvider,
)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["doc_kind"],
    "properties": {"doc_kind": {"type": "string"}},
}


@pytest.fixture()
def relay(tmp_path: Path) -> RelayProvider:
    return RelayProvider(root=tmp_path)


def _answer(root: Path, key: str, parsed: dict) -> None:
    (root / "responses").mkdir(parents=True, exist_ok=True)
    (root / "responses" / f"{key}.json").write_text(
        json.dumps({"parsed": parsed}), encoding="utf-8"
    )


# ------------------------------------------------------------------ the two-pass cycle


def test_an_unanswered_prompt_is_recorded_and_the_call_fails(relay, tmp_path) -> None:
    """Pass one: nothing to answer with, so the prompt is written down and the call does not pretend."""
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("extract this", SCHEMA)

    key = raised.value.key
    request = json.loads((tmp_path / "requests" / f"{key}.json").read_text(encoding="utf-8"))
    assert request["prompt"] == "extract this"
    assert request["json_schema"] == SCHEMA, "the answerer needs the schema, not just the prompt"


def test_the_same_prompt_is_answered_on_the_second_pass(relay, tmp_path) -> None:
    """Pass two: the whole point. The key must be reproduced exactly or nothing ever converges."""
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("extract this", SCHEMA)
    _answer(tmp_path, raised.value.key, {"doc_kind": "readme"})

    response = relay.complete_json("extract this", SCHEMA)

    assert response.parsed == {"doc_kind": "readme"}
    assert response.valid is True
    assert response.model == "claude-haiku-4-5"


def test_a_rerun_does_not_multiply_request_files(relay, tmp_path) -> None:
    """Draining a queue means re-running repeatedly; each rerun must leave one file per prompt."""
    for _ in range(3):
        with pytest.raises(RelayPending):
            relay.complete_json("extract this", SCHEMA)

    assert len(list((tmp_path / "requests").glob("*.json"))) == 1


# ------------------------------------------------------------------ keying


def test_different_prompts_get_different_keys(relay) -> None:
    keys = set()
    for prompt in ("document one", "document two"):
        with pytest.raises(RelayPending) as raised:
            relay.complete_json(prompt, SCHEMA)
        keys.add(raised.value.key)

    assert len(keys) == 2


def test_the_schema_and_system_prompt_are_part_of_the_key(relay) -> None:
    """Same document, different task, different answer - so they cannot share a cache entry."""
    with pytest.raises(RelayPending) as first:
        relay.complete_json("same text", SCHEMA)
    with pytest.raises(RelayPending) as second:
        relay.complete_json("same text", {"type": "object", "properties": {"other": {}}})
    with pytest.raises(RelayPending) as third:
        relay.complete_json("same text", SCHEMA, system="a different role")

    assert len({first.value.key, second.value.key, third.value.key}) == 3


def test_an_answer_does_not_leak_across_prompts(relay, tmp_path) -> None:
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("document one", SCHEMA)
    _answer(tmp_path, raised.value.key, {"doc_kind": "readme"})

    with pytest.raises(RelayPending):
        relay.complete_json("document two", SCHEMA)


# ------------------------------------------------------------------ it fails rather than guesses


def test_a_response_without_a_parsed_object_is_an_error(relay, tmp_path) -> None:
    """The wrapper shape is mandatory. A bare object would be ambiguous with a wrapped one."""
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("extract this", SCHEMA)
    (tmp_path / "responses").mkdir(parents=True, exist_ok=True)
    (tmp_path / "responses" / f"{raised.value.key}.json").write_text(
        json.dumps({"doc_kind": "readme"}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="parsed"):
        relay.complete_json("extract this", SCHEMA)


def test_an_unreadable_response_is_an_error_not_an_empty_result(relay, tmp_path) -> None:
    """Half-written JSON is the likeliest real failure: a file being answered while a run is going."""
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("extract this", SCHEMA)
    (tmp_path / "responses").mkdir(parents=True, exist_ok=True)
    (tmp_path / "responses" / f"{raised.value.key}.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="unreadable"):
        relay.complete_json("extract this", SCHEMA)


# ------------------------------------------------------------------ provenance


def test_the_model_identity_is_distinct_from_the_bedrock_row() -> None:
    """Same underlying Haiku 4.5, different route - and provenance records the route.

    ``extraction_models`` answers "how was this fact produced?". Reusing the Bedrock id would make a
    cost review or an incident review unable to tell which path a fact came down.
    """
    identity = RelayProvider(root=Path("/tmp/unused")).model_identity()

    assert identity.id == MODEL_ID == "claude-code:haiku-4-5"
    assert identity.id != "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert identity.provider == "claude-code"
    assert identity.parameters["route"] == "claude-code-subagent"


def test_the_factory_builds_it_from_the_setting() -> None:
    from aimemory.common.config import LLMSettings
    from aimemory.providers.llm import get_provider

    provider = get_provider(LLMSettings(LLM_PROVIDER="relay"))

    assert provider.model_identity().id == MODEL_ID


def test_health_reports_what_is_outstanding(relay, tmp_path) -> None:
    """Draining hundreds of prompts by hand needs a progress number that is not a manual count."""
    with pytest.raises(RelayPending) as raised:
        relay.complete_json("extract this", SCHEMA)
    assert relay.health()["requests"] == 1
    assert relay.health()["responses"] == 0

    _answer(tmp_path, raised.value.key, {"doc_kind": "readme"})

    assert relay.health()["responses"] == 1


# ------------------------------------------------------------------ schema checking on read


def test_an_answer_that_breaks_the_schema_is_rejected_and_re_asked(relay, tmp_path) -> None:
    """A real provider gets grammar-constrained decoding; a written answer does not.

    Without this check an answer using a value outside an enum travels three layers before Pydantic
    rejects it, and the operator sees `entities.8.type -> enum` against an episode id with no way
    back to the file that produced it. MEASURED 2026-09-19: 10 of 111 subagent answers broke their
    schema this way, all of them on an enum.
    """
    enum_schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"type": "string", "enum": ["decision", "requirement"]}},
    }
    with pytest.raises(RelayPending) as first:
        relay.complete_json("classify this", enum_schema)
    _answer(tmp_path, first.value.key, {"kind": "specification"})  # not in the enum

    with pytest.raises(RelayPending):
        relay.complete_json("classify this", enum_schema)


def test_the_rejection_reason_is_written_back_into_the_request(relay, tmp_path) -> None:
    """The correction loop. A rejected answer is only useful if the next attempt is told why."""
    enum_schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"type": "string", "enum": ["decision"]}},
    }
    with pytest.raises(RelayPending) as first:
        relay.complete_json("classify this", enum_schema)
    key = first.value.key
    _answer(tmp_path, key, {"kind": "nonsense"})
    with pytest.raises(RelayPending):
        relay.complete_json("classify this", enum_schema)

    request = json.loads((tmp_path / "requests" / f"{key}.json").read_text(encoding="utf-8"))
    assert "previous_answer_rejected" in request
    assert "nonsense" in request["previous_answer_rejected"]
    assert "REJECTED" in request["instructions"]


def test_a_conforming_answer_still_passes_straight_through(relay, tmp_path) -> None:
    """The guard must not become a second place for correct answers to die."""
    enum_schema = {
        "type": "object",
        "required": ["kind"],
        "properties": {"kind": {"type": "string", "enum": ["decision"]}},
    }
    with pytest.raises(RelayPending) as first:
        relay.complete_json("classify this", enum_schema)
    _answer(tmp_path, first.value.key, {"kind": "decision"})

    assert relay.complete_json("classify this", enum_schema).parsed == {"kind": "decision"}
