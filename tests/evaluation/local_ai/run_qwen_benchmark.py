"""P4-T02 measurement harness for qwen3:4b on this machine. Owner A05.

Run (from the repo root, stack up):

    docker compose --profile tools run --rm --no-deps \
        -e LLM_TIMEOUT_SECONDS=1800 \
        tools python -m tests.evaluation.local_ai.run_qwen_benchmark

What it measures, all MEASURED, all raw numbers written to
``tests/evaluation/local_ai/results/qwen-benchmark.json``:

1. **model load time** - the model is explicitly unloaded (``keep_alive=0``) first, then the next
   call's ``load_duration`` is the cold load;
2. **prompt-eval and generation tok/s at three prompt sizes** (~200 / ~800 / ~2000 tokens) -
   A00's 32.7 tok/s prompt-eval figure came from a 28-token prompt and does not generalise;
3. **JSON-schema validity over 20 runs** of ``schemas/extraction/episode_extraction.schema.json``
   against 20 distinct ~800-token episodes from the real vault, split into first-attempt valid /
   valid after retry / failed after 2 retries;
4. **seconds per episode end to end**, median and p90.

Nothing here writes to the vault, to Postgres or to Neo4j. Container RSS is sampled from the host
with ``docker stats`` while this is running (a container cannot see its siblings).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from aimemory.common.config import LLMSettings
from aimemory.providers.llm import VALIDATOR_BACKEND, OllamaProvider

from .episodes import Episode, load_benchmark_episodes
from .prompts import CONCISE_SUFFIX, EPISODE_SYSTEM_PROMPT, episode_prompt

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SCHEMA_PATH = Path("schemas/extraction/episode_extraction.schema.json")

# ~200 / ~800 / ~2000 tokens at the 4-chars-per-token rule of thumb. The MEASURED counts are
# Ollama's prompt_eval_count, which is what the report quotes.
SIZE_SWEEP_CHARS = (800, 3200, 8000)

_TINY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[index]


def _unload(settings: LLMSettings) -> None:
    """Ask Ollama to evict the model so the next call pays the cold load."""
    with httpx.Client(base_url=settings.ollama_url.rstrip("/"), timeout=60.0) as client:
        client.post("/api/generate", json={"model": settings.model, "keep_alive": 0})
    time.sleep(8)


def _long_text(episodes: list[Episode], chars: int) -> str:
    """Concatenate real episode text until ``chars`` is reached (still real vault prose)."""
    buffer: list[str] = []
    total = 0
    for episode in episodes:
        buffer.append(episode.text)
        total += len(episode.text)
        if total >= chars:
            break
    return "\n\n".join(buffer)[:chars]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="as-specified", help="name of this configuration")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--num-predict", type=int, default=None)
    parser.add_argument(
        "--concise",
        action="store_true",
        help="append the output-budget instruction to the system prompt (mitigation variant)",
    )
    parser.add_argument("--skip-cold-load", action="store_true")
    parser.add_argument("--skip-sweep", action="store_true")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    overrides: dict[str, Any] = {}
    if args.num_predict is not None:
        overrides["LLM_NUM_PREDICT"] = args.num_predict
    settings = LLMSettings(**overrides)
    provider = OllamaProvider(settings)
    episodes = load_benchmark_episodes()[: args.limit]
    identity = provider.model_identity()
    system_prompt = EPISODE_SYSTEM_PROMPT + (CONCISE_SUFFIX if args.concise else "")
    max_retries = settings.max_retries if args.max_retries is None else args.max_retries

    out: dict[str, Any] = {
        "tag": args.tag,
        "started_at": datetime.now(UTC).isoformat(),
        "validator_backend": VALIDATOR_BACKEND,
        "model": identity.model_dump(mode="json"),
        "settings": {
            "num_ctx": settings.num_ctx,
            "num_predict": settings.num_predict,
            "temperature": settings.temperature,
            "timeout_seconds": settings.timeout_seconds,
            "max_retries": max_retries,
            "keep_alive": settings.keep_alive,
            "concise_prompt": args.concise,
        },
        "schema": str(SCHEMA_PATH),
    }

    # ---------------------------------------------------------------- 1. cold model load time
    if not args.skip_cold_load:
        print("[1/3] unloading model, then measuring cold load ...", flush=True)
        _unload(settings)
        cold_start = time.perf_counter()
        cold = provider.complete_json_traced('Answer with {"ok": true}.', _TINY_SCHEMA, max_retries=0)
        out["cold_load"] = {
            "wall_s": round(time.perf_counter() - cold_start, 3),
            "traces": [asdict(t) for t in cold.traces],
            "load_ms": cold.traces[0].load_ms if cold.traces else None,
            "valid": cold.response.valid,
        }
        print(f"      cold load {out['cold_load']['load_ms']:.0f} ms", flush=True)

    # ------------------------------------------------------- 2. prompt-size throughput sweep
    sweep: list[dict[str, Any]] = []
    for chars in () if args.skip_sweep else SIZE_SWEEP_CHARS:
        print(f"[2/3] prompt-size sweep {chars} chars ...", flush=True)
        text = _long_text(episodes, chars)
        started = time.perf_counter()
        traced = provider.complete_json_traced(
            f"Document title: size-probe-{chars}\n---\n{text}\n---\nExtract the document as JSON.",
            schema,
            system=system_prompt,
            max_retries=0,
        )
        trace = traced.traces[0]
        record = {
            "target_chars": chars,
            "wall_s": round(time.perf_counter() - started, 3),
            "prompt_eval_count": trace.prompt_eval_count,
            "prompt_eval_ms": round(trace.prompt_eval_ms, 1),
            "prompt_tok_s": round(trace.prompt_tok_s or 0.0, 2),
            "eval_count": trace.eval_count,
            "eval_ms": round(trace.eval_ms, 1),
            "gen_tok_s": round(trace.gen_tok_s or 0.0, 2),
            "valid": trace.valid,
            "done_reason": trace.done_reason,
        }
        sweep.append(record)
        print(f"      {chars} chars -> {json.dumps(record)}", flush=True)
    out["size_sweep"] = sweep

    # ------------------------------------------------------------------ 3. N-run validity set
    print(f"[3/3] {len(episodes)} episode extractions ...", flush=True)
    runs: list[dict[str, Any]] = []
    for episode in episodes:
        started = time.perf_counter()
        traced = provider.complete_json_traced(
            episode_prompt(episode), schema, system=system_prompt, max_retries=max_retries
        )
        wall = time.perf_counter() - started
        first = traced.traces[0]
        parsed = traced.response.parsed or {}
        record = {
            "key": episode.key,
            "relative_path": episode.relative_path,
            "chars": len(episode.text),
            "wall_s": round(wall, 2),
            "attempts": traced.response.attempts,
            "first_attempt_valid": first.valid,
            "final_valid": traced.response.valid,
            "prompt_eval_count": first.prompt_eval_count,
            "prompt_tok_s": round(first.prompt_tok_s or 0.0, 2),
            "eval_count": first.eval_count,
            "gen_tok_s": round(first.gen_tok_s or 0.0, 2),
            "done_reason": first.done_reason,
            "n_entities": len(parsed.get("entities", []) or []),
            "n_artifacts": len(parsed.get("artifacts", []) or []),
            "doc_kind": parsed.get("doc_kind"),
            "errors": traced.response.errors,
            "traces": [asdict(t) for t in traced.traces],
        }
        record["truncated_at_num_predict"] = any(
            t.done_reason == "length" for t in traced.traces
        )
        runs.append(record)
        print(
            f"      {episode.key} {wall:6.1f}s attempts={record['attempts']} "
            f"first_valid={record['first_attempt_valid']} final_valid={record['final_valid']} "
            f"pe={record['prompt_eval_count']}tok@{record['prompt_tok_s']}t/s "
            f"gen={record['eval_count']}tok@{record['gen_tok_s']}t/s "
            f"ent={record['n_entities']} art={record['n_artifacts']}",
            flush=True,
        )
        with (RESULTS_DIR / f"qwen-{args.tag}.partial.json").open("w", encoding="utf-8") as fh:
            json.dump({**out, "runs": runs}, fh, indent=1)

    walls = [r["wall_s"] for r in runs]
    first_valid = sum(1 for r in runs if r["first_attempt_valid"])
    final_valid = sum(1 for r in runs if r["final_valid"])
    out["runs"] = runs
    out["summary"] = {
        "n": len(runs),
        "first_attempt_valid": first_valid,
        "first_attempt_validity_pct": round(100.0 * first_valid / max(1, len(runs)), 1),
        "valid_after_retries": final_valid,
        "final_validity_pct": round(100.0 * final_valid / max(1, len(runs)), 1),
        "failed_outright": len(runs) - final_valid,
        "truncated_at_num_predict": sum(1 for r in runs if r["truncated_at_num_predict"]),
        "median_s": round(statistics.median(walls), 2) if walls else 0.0,
        "p90_s": round(_percentile(walls, 90), 2),
        "min_s": round(min(walls), 2) if walls else 0.0,
        "max_s": round(max(walls), 2) if walls else 0.0,
        "median_prompt_tok_s": round(
            statistics.median([r["prompt_tok_s"] for r in runs if r["prompt_tok_s"]]), 2
        )
        if runs
        else 0.0,
        "median_gen_tok_s": round(
            statistics.median([r["gen_tok_s"] for r in runs if r["gen_tok_s"]]), 2
        )
        if runs
        else 0.0,
    }
    out["finished_at"] = datetime.now(UTC).isoformat()

    target = RESULTS_DIR / f"qwen-{args.tag}.json"
    target.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["summary"], indent=1), flush=True)
    print(f"raw results -> {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
