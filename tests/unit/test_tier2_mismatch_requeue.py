"""A project model guard must not strand an already-claimed episode."""

from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aimemory.sources import tier2


def test_project_model_mismatch_requeues_claimed_episode(monkeypatch):
    episode_id = uuid4()
    requeued = []

    class Scope:
        @contextmanager
        def session(self):
            yield object()

    def guard(_session, _model_id, *, project_id=None, **_kwargs):
        if project_id == "oploy-website":
            raise tier2.ExtractionModelMismatch("existing project facts use another model")

    monkeypatch.setattr(tier2, "assert_single_extraction_model", guard)
    monkeypatch.setattr(tier2, "_register_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        tier2.ingest_repo,
        "claim_episode_for_extraction",
        lambda *_args, **_kwargs: {"id": episode_id},
    )
    monkeypatch.setattr(
        tier2.ingest_repo, "requeue_episode", lambda _session, value: requeued.append(value)
    )
    monkeypatch.setattr(
        tier2, "Episode", lambda **_row: SimpleNamespace(id=episode_id, project_id="oploy-website")
    )

    with pytest.raises(tier2.ExtractionModelMismatch):
        tier2.run_tier2(
            Scope(),
            engine=SimpleNamespace(kind="native"),
            writer=lambda *_args: None,
            root_id="wagtail",
            model_id="codex:gpt-5.6-luna",
        )

    assert requeued == [episode_id]
