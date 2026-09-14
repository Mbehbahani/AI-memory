"""Single-episode probe used to size the P4-T02 run. Not a test; run it by hand.

    docker compose --profile tools run --rm --no-deps -e LLM_TIMEOUT_SECONDS=1800 \
        tools python -m tests.evaluation.local_ai.probe_once
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from aimemory.providers.llm import VALIDATOR_BACKEND, OllamaProvider

from .episodes import load_episodes
from .prompts import EPISODE_SYSTEM_PROMPT, episode_prompt


def main() -> int:
    schema = json.loads(
        Path("schemas/extraction/episode_extraction.schema.json").read_text(encoding="utf-8")
    )
    provider = OllamaProvider()
    print("validator backend:", VALIDATOR_BACKEND, flush=True)
    print("health:", provider.health(), flush=True)
    print("identity:", provider.model_identity().model_dump_json(), flush=True)

    episode = load_episodes()[int(sys.argv[1]) - 1 if len(sys.argv) > 1 else 0]
    print(f"episode {episode.key} {episode.relative_path} chars={len(episode.text)}", flush=True)

    started = time.perf_counter()
    traced = provider.complete_json_traced(
        episode_prompt(episode), schema, system=EPISODE_SYSTEM_PROMPT
    )
    wall = time.perf_counter() - started
    print(f"wall: {wall:.1f}s  attempts={traced.response.attempts}", flush=True)
    for trace in traced.traces:
        print(
            f"  attempt {trace.attempt}: valid={trace.valid} load={trace.load_ms:.0f}ms "
            f"prompt_eval={trace.prompt_eval_count}tok/{trace.prompt_eval_ms:.0f}ms "
            f"({trace.prompt_tok_s or 0:.1f} tok/s) "
            f"gen={trace.eval_count}tok/{trace.eval_ms:.0f}ms ({trace.gen_tok_s or 0:.1f} tok/s) "
            f"done={trace.done_reason} errors={trace.errors[:2]}",
            flush=True,
        )
    print("valid:", traced.response.valid, flush=True)
    if traced.response.parsed:
        print(json.dumps(traced.response.parsed, indent=1)[:2000], flush=True)
    else:
        print("raw (first 1000 chars):", traced.response.text[:1000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
