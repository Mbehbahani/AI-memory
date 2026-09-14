"""P4-T02 - the dual-provider extraction measurement campaign (ADR-0012). Owner A05.

Runs the *same* 20 real my-vault episodes, through the *same* ``complete_json_traced`` interface,
against the *same* frozen schemas, for either provider, so the two result files are directly
comparable:

    # local, from the host (OLLAMA_URL is 127.0.0.1 in .env)
    VAULT_ROOT=D:/My-Vault python -m tests.evaluation.local_ai.run_benchmark --provider ollama
    # cloud (ADR-0012): needs AWS_PROFILE; the tools container has no credentials
    AWS_PROFILE=mohabehb python -m tests.evaluation.local_ai.run_benchmark --provider bedrock

Measured per provider, all raw rows written to ``results/bench-<provider>-<tag>.json``:

* first-pass JSON-schema validity, validity after retries, outright failures;
* entity recall, **entity-type correctness** and relationship recall against the hand-listed
  ``gate_expectations`` / ``benchmark_expectations`` (written before the run);
* seconds per episode - median and p90;
* Ollama only: cold model-load time, and prompt-eval / generation tok/s at three prompt sizes
  *and* at the real ~800-token episode length;
* Bedrock only: input/output tokens and the resulting cost per episode.

Container RSS is sampled separately from the host (``scripts`` in the report) because a process
cannot see a sibling container's memory.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from aimemory.common.config import LLMSettings
from aimemory.providers.llm import VALIDATOR_BACKEND, get_provider

from .benchmark_expectations import BENCHMARK_EXPECTATIONS, acceptable_types
from .episodes import Episode, load_benchmark_episodes
from .gate_expectations import normalize
from .prompts import (
    CONCISE_SUFFIX,
    EPISODE_SYSTEM_PROMPT,
    RELATION_SYSTEM_PROMPT,
    episode_prompt,
    relation_prompt,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"
EPISODE_SCHEMA_PATH = Path("schemas/extraction/episode_extraction.schema.json")
RELATION_SCHEMA_PATH = Path("schemas/extraction/relationship_extraction.schema.json")

#: ~200 / ~800 / ~2000 tokens at the 4-chars-per-token rule of thumb. The MEASURED counts are
#: Ollama's ``prompt_eval_count``, which is what the report quotes.
SIZE_SWEEP_CHARS = (800, 3200, 8000)

#: DOCUMENTED list price for Claude Haiku 4.5 (USD per million tokens), used to turn the MEASURED
#: token counts into a MEASURED-inputs / DOCUMENTED-rate cost. Override with --price-in/--price-out.
HAIKU_PRICE_IN_PER_MTOK = 1.00
HAIKU_PRICE_OUT_PER_MTOK = 5.00

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


def _trace_dict(trace: Any) -> dict[str, Any]:
    return asdict(trace) if is_dataclass(trace) else dict(trace)


def _unload(settings: LLMSettings) -> None:
    """Ask Ollama to evict the model so the next call pays the cold load."""
    with httpx.Client(base_url=settings.ollama_url.rstrip("/"), timeout=60.0) as client:
        client.post("/api/generate", json={"model": settings.model, "keep_alive": 0})
    time.sleep(8)


def _long_text(episodes: list[Episode], chars: int) -> str:
    buffer: list[str] = []
    total = 0
    for episode in episodes:
        buffer.append(episode.text)
        total += len(episode.text)
        if total >= chars:
            break
    return "\n\n".join(buffer)[:chars]


# ----------------------------------------------------------------------------------- quality


def _matches(expected_names: tuple[str, ...], produced: str) -> bool:
    node = normalize(produced)
    if not node:
        return False
    return any(name == node or name in node or node in name for name in expected_names)


def score_entities(key: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    """Entity recall + type correctness for one episode against the hand-listed expectation."""
    expectation = BENCHMARK_EXPECTATIONS[key]
    produced = [(e.get("name") or "", e.get("type") or "") for e in entities if isinstance(e, dict)]

    found: list[str] = []
    typed_ok: list[str] = []
    typed_wrong: list[dict[str, str]] = []
    for expected in expectation.entities:
        hits = [(name, etype) for name, etype in produced if _matches(expected.names, name)]
        if not hits:
            continue
        found.append(expected.canonical)
        allowed = acceptable_types(expected.canonical)
        if allowed is None:
            continue
        if any(etype in allowed for _, etype in hits):
            typed_ok.append(expected.canonical)
        else:
            typed_wrong.append(
                {
                    "expected": expected.canonical,
                    "got_type": hits[0][1],
                    "acceptable": "|".join(allowed),
                    "as_name": hits[0][0],
                }
            )

    n_expected = len(expectation.entities)
    scored_for_type = len(typed_ok) + len(typed_wrong)
    return {
        "expected_entities": n_expected,
        "found_entities": len(found),
        "entity_recall_pct": round(100.0 * len(found) / max(1, n_expected), 1),
        "entities_found": found,
        "entities_missed": [
            e.canonical for e in expectation.entities if e.canonical not in found
        ],
        "type_scored": scored_for_type,
        "type_correct": len(typed_ok),
        "type_correct_pct": round(100.0 * len(typed_ok) / max(1, scored_for_type), 1)
        if scored_for_type
        else None,
        "type_errors": typed_wrong,
        "n_produced_entities": len(produced),
    }


def score_relationships(key: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Relationship recall for one episode; direction-insensitive, predicate-insensitive."""
    expectation = BENCHMARK_EXPECTATIONS[key]
    pairs = [
        (normalize(f.get("subject") or ""), normalize(f.get("object") or ""))
        for f in facts
        if isinstance(f, dict)
    ]

    def _hit(label: str, node: str) -> bool:
        target = normalize(label)
        return bool(node) and (target == node or target in node or node in target)

    found = [
        (subject, obj)
        for subject, obj in expectation.relationships
        if any(
            (_hit(subject, a) and _hit(obj, b)) or (_hit(subject, b) and _hit(obj, a))
            for a, b in pairs
        )
    ]
    n_expected = len(expectation.relationships)
    return {
        "expected_relationships": n_expected,
        "found_relationships": len(found),
        "relationship_recall_pct": round(100.0 * len(found) / max(1, n_expected), 1),
        "relationships_found": [list(r) for r in found],
        "relationships_missed": [
            list(r) for r in expectation.relationships if r not in found
        ],
        "n_produced_facts": len(pairs),
    }


# --------------------------------------------------------------------------------------- run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("ollama", "bedrock"), default="ollama")
    parser.add_argument("--tag", default="p4t02")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument(
        "--relations",
        type=int,
        default=10,
        help="run call 2 (relationship extraction) on the first N episodes (hand-listed pairs)",
    )
    parser.add_argument("--max-retries", type=int, default=None)
    parser.add_argument("--num-predict", type=int, default=None)
    parser.add_argument("--concise", action="store_true", default=True)
    parser.add_argument("--no-concise", dest="concise", action="store_false")
    parser.add_argument("--skip-cold-load", action="store_true")
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument("--price-in", type=float, default=HAIKU_PRICE_IN_PER_MTOK)
    parser.add_argument("--price-out", type=float, default=HAIKU_PRICE_OUT_PER_MTOK)
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    episode_schema = json.loads(EPISODE_SCHEMA_PATH.read_text(encoding="utf-8"))
    relation_schema = json.loads(RELATION_SCHEMA_PATH.read_text(encoding="utf-8"))

    overrides: dict[str, Any] = {}
    if args.num_predict is not None:
        overrides["LLM_NUM_PREDICT"] = args.num_predict
    settings = LLMSettings(**overrides)
    provider = get_provider(settings, provider=args.provider)
    is_ollama = args.provider == "ollama"

    episodes = load_benchmark_episodes()[: args.limit]
    identity = provider.model_identity()
    system_prompt = EPISODE_SYSTEM_PROMPT + (CONCISE_SUFFIX if args.concise else "")
    max_retries = settings.max_retries if args.max_retries is None else args.max_retries

    out: dict[str, Any] = {
        "provider": args.provider,
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
            "bedrock_model_id": settings.bedrock_model_id if not is_ollama else None,
            "bedrock_region": settings.bedrock_region if not is_ollama else None,
            "bedrock_max_tokens": settings.bedrock_max_tokens if not is_ollama else None,
        },
        "schemas": {"episode": str(EPISODE_SCHEMA_PATH), "relation": str(RELATION_SCHEMA_PATH)},
        "episodes": [
            {"key": e.key, "relative_path": e.relative_path, "chars": len(e.text)} for e in episodes
        ],
    }

    # ---------------------------------------------------------------- 1. cold model load time
    if is_ollama and not args.skip_cold_load:
        print("[1/4] unloading model, then measuring cold load ...", flush=True)
        _unload(settings)
        cold_start = time.perf_counter()
        cold = provider.complete_json_traced(
            'Answer with {"ok": true}.', _TINY_SCHEMA, max_retries=0
        )
        out["cold_load"] = {
            "wall_s": round(time.perf_counter() - cold_start, 3),
            "load_ms": cold.traces[0].load_ms if cold.traces else None,
            "valid": cold.response.valid,
            "traces": [_trace_dict(t) for t in cold.traces],
        }
        print(f"      cold load {out['cold_load']['load_ms']:.0f} ms", flush=True)

    # ------------------------------------------------------- 2. prompt-size throughput sweep
    sweep: list[dict[str, Any]] = []
    if is_ollama and not args.skip_sweep:
        for chars in SIZE_SWEEP_CHARS:
            print(f"[2/4] prompt-size sweep {chars} chars ...", flush=True)
            text = _long_text(episodes, chars)
            started = time.perf_counter()
            traced = provider.complete_json_traced(
                f"Document title: size-probe-{chars}\n---\n{text}\n---\n"
                "Extract the document as JSON.",
                episode_schema,
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
            print(f"      {json.dumps(record)}", flush=True)
    out["size_sweep"] = sweep

    # ------------------------------------------------------------ 3. call 1 over N episodes
    print(f"[3/4] {len(episodes)} episode extractions ({args.provider}) ...", flush=True)
    runs: list[dict[str, Any]] = []
    parsed_by_key: dict[str, dict[str, Any]] = {}
    for episode in episodes:
        started = time.perf_counter()
        error: str | None = None
        try:
            traced = provider.complete_json_traced(
                episode_prompt(episode),
                episode_schema,
                system=system_prompt,
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001 - a transport failure IS a measurement
            runs.append(
                {
                    "key": episode.key,
                    "relative_path": episode.relative_path,
                    "chars": len(episode.text),
                    "wall_s": round(time.perf_counter() - started, 2),
                    "attempts": 0,
                    "first_attempt_valid": False,
                    "final_valid": False,
                    "transport_error": f"{type(exc).__name__}: {exc}",
                }
            )
            print(f"      {episode.key} TRANSPORT FAILURE {type(exc).__name__}", flush=True)
            continue
        wall = time.perf_counter() - started
        first = traced.traces[0]
        parsed = traced.response.parsed or {}
        if parsed:
            parsed_by_key[episode.key] = parsed
        entities = parsed.get("entities", []) or []
        quality = score_entities(episode.key, entities) if parsed else None
        record: dict[str, Any] = {
            "key": episode.key,
            "relative_path": episode.relative_path,
            "chars": len(episode.text),
            "wall_s": round(wall, 2),
            "attempts": traced.response.attempts,
            "first_attempt_valid": first.valid,
            "final_valid": traced.response.valid,
            "prompt_eval_count": first.prompt_eval_count,
            "prompt_tok_s": round(first.prompt_tok_s or 0.0, 2) if is_ollama else None,
            "eval_count": first.eval_count,
            "gen_tok_s": round(first.gen_tok_s or 0.0, 2) if is_ollama else None,
            "stop": getattr(first, "done_reason", None) or getattr(first, "stop_reason", None),
            "n_entities": len(entities),
            "n_artifacts": len(parsed.get("artifacts", []) or []),
            "doc_kind": parsed.get("doc_kind"),
            "errors": traced.response.errors,
            "transport_error": error,
            "quality": quality,
            "traces": [_trace_dict(t) for t in traced.traces],
        }
        record["truncated_at_num_predict"] = any(
            _trace_dict(t).get("done_reason") == "length" for t in traced.traces
        )
        if not is_ollama:
            tokens_in = sum(_trace_dict(t)["prompt_eval_count"] for t in traced.traces)
            tokens_out = sum(_trace_dict(t)["eval_count"] for t in traced.traces)
            record["tokens_in_total"] = tokens_in
            record["tokens_out_total"] = tokens_out
            record["cost_usd"] = round(
                tokens_in / 1e6 * args.price_in + tokens_out / 1e6 * args.price_out, 6
            )
        runs.append(record)
        qual = (
            f"ent_recall={quality['entity_recall_pct']}% type_ok={quality['type_correct_pct']}%"
            if quality
            else "quality=n/a"
        )
        print(
            f"      {episode.key} {wall:6.1f}s attempts={record['attempts']} "
            f"first_valid={record['first_attempt_valid']} final_valid={record['final_valid']} "
            f"in={record['prompt_eval_count']} out={record['eval_count']} "
            f"ent={record['n_entities']} art={record['n_artifacts']} {qual}",
            flush=True,
        )
        (RESULTS_DIR / f"bench-{args.provider}-{args.tag}.partial.json").write_text(
            json.dumps({**out, "runs": runs}, indent=1), encoding="utf-8"
        )

    out["runs"] = runs

    # ------------------------------------------------- 4. call 2 (relationships) on first N
    print(f"[4/4] {args.relations} relationship extractions ...", flush=True)
    relation_runs: list[dict[str, Any]] = []
    for episode in episodes[: args.relations]:
        parsed = parsed_by_key.get(episode.key)
        if not parsed:
            relation_runs.append({"key": episode.key, "skipped": "call 1 produced no valid object"})
            continue
        names = [e.get("name", "") for e in parsed.get("entities", []) or [] if isinstance(e, dict)]
        started = time.perf_counter()
        try:
            traced = provider.complete_json_traced(
                relation_prompt(episode, names),
                relation_schema,
                system=RELATION_SYSTEM_PROMPT,
                max_retries=max_retries,
            )
        except Exception as exc:  # noqa: BLE001
            relation_runs.append(
                {"key": episode.key, "transport_error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        wall = time.perf_counter() - started
        first = traced.traces[0]
        facts = (traced.response.parsed or {}).get("facts", []) or []
        record = {
            "key": episode.key,
            "wall_s": round(wall, 2),
            "attempts": traced.response.attempts,
            "first_attempt_valid": first.valid,
            "final_valid": traced.response.valid,
            "prompt_eval_count": first.prompt_eval_count,
            "eval_count": first.eval_count,
            "n_facts": len(facts),
            "entity_names_given": len(names),
            "quality": score_relationships(episode.key, facts),
            "errors": traced.response.errors,
            "traces": [_trace_dict(t) for t in traced.traces],
        }
        if not is_ollama:
            tokens_in = sum(_trace_dict(t)["prompt_eval_count"] for t in traced.traces)
            tokens_out = sum(_trace_dict(t)["eval_count"] for t in traced.traces)
            record["tokens_in_total"] = tokens_in
            record["tokens_out_total"] = tokens_out
            record["cost_usd"] = round(
                tokens_in / 1e6 * args.price_in + tokens_out / 1e6 * args.price_out, 6
            )
        relation_runs.append(record)
        print(
            f"      {episode.key} {wall:6.1f}s valid={record['final_valid']} "
            f"facts={record['n_facts']} "
            f"rel_recall={record['quality']['relationship_recall_pct']}%",
            flush=True,
        )
        (RESULTS_DIR / f"bench-{args.provider}-{args.tag}.partial.json").write_text(
            json.dumps({**out, "relation_runs": relation_runs}, indent=1), encoding="utf-8"
        )
    out["relation_runs"] = relation_runs

    # ----------------------------------------------------------------------------- summary
    completed = [r for r in runs if not r.get("transport_error")]
    walls = [r["wall_s"] for r in completed]
    first_valid = sum(1 for r in completed if r.get("first_attempt_valid"))
    final_valid = sum(1 for r in completed if r.get("final_valid"))
    qualities = [r["quality"] for r in completed if r.get("quality")]
    ent_recall = [q["entity_recall_pct"] for q in qualities]
    type_pcts = [q["type_correct_pct"] for q in qualities if q["type_correct_pct"] is not None]
    rel_qualities = [r["quality"] for r in relation_runs if r.get("quality")]
    rel_recall = [q["relationship_recall_pct"] for q in rel_qualities]

    summary: dict[str, Any] = {
        "provider": args.provider,
        "n_episodes": len(runs),
        "transport_failures": len(runs) - len(completed),
        "first_attempt_valid": first_valid,
        "first_attempt_validity_pct": round(100.0 * first_valid / max(1, len(runs)), 1),
        "valid_after_retries": final_valid,
        "final_validity_pct": round(100.0 * final_valid / max(1, len(runs)), 1),
        "failed_outright": len(runs) - final_valid,
        "truncated_at_num_predict": sum(1 for r in completed if r.get("truncated_at_num_predict")),
        "median_s": round(statistics.median(walls), 2) if walls else None,
        "p90_s": round(_percentile(walls, 90), 2) if walls else None,
        "min_s": round(min(walls), 2) if walls else None,
        "max_s": round(max(walls), 2) if walls else None,
        "mean_entity_recall_pct": round(statistics.mean(ent_recall), 1) if ent_recall else None,
        "median_entity_recall_pct": round(statistics.median(ent_recall), 1) if ent_recall else None,
        "mean_type_correct_pct": round(statistics.mean(type_pcts), 1) if type_pcts else None,
        "n_type_errors": sum(len(q["type_errors"]) for q in qualities),
        "type_errors": [err for q in qualities for err in q["type_errors"]],
        "relation_episodes": len(rel_recall),
        "relation_first_attempt_valid": sum(
            1 for r in relation_runs if r.get("first_attempt_valid")
        ),
        "relation_final_valid": sum(1 for r in relation_runs if r.get("final_valid")),
        "mean_relationship_recall_pct": round(statistics.mean(rel_recall), 1)
        if rel_recall
        else None,
        "median_relation_s": round(
            statistics.median([r["wall_s"] for r in relation_runs if r.get("wall_s")]), 2
        )
        if any(r.get("wall_s") for r in relation_runs)
        else None,
    }
    if is_ollama:
        pe = [r["prompt_tok_s"] for r in completed if r.get("prompt_tok_s")]
        gen = [r["gen_tok_s"] for r in completed if r.get("gen_tok_s")]
        summary["median_prompt_tok_s_at_episode_length"] = (
            round(statistics.median(pe), 2) if pe else None
        )
        summary["median_gen_tok_s_at_episode_length"] = (
            round(statistics.median(gen), 2) if gen else None
        )
        summary["median_prompt_eval_count"] = (
            round(statistics.median([r["prompt_eval_count"] for r in completed]))
            if completed
            else None
        )
    else:
        cost = sum(r.get("cost_usd", 0.0) for r in runs) + sum(
            r.get("cost_usd", 0.0) for r in relation_runs
        )
        summary["tokens_in_total"] = sum(r.get("tokens_in_total", 0) for r in runs)
        summary["tokens_out_total"] = sum(r.get("tokens_out_total", 0) for r in runs)
        summary["cost_usd_call1_total"] = round(sum(r.get("cost_usd", 0.0) for r in runs), 5)
        summary["cost_usd_all_calls"] = round(cost, 5)
        summary["cost_usd_per_episode_call1"] = (
            round(sum(r.get("cost_usd", 0.0) for r in runs) / max(1, len(runs)), 6) if runs else None
        )
        summary["price_basis_usd_per_mtok"] = {"in": args.price_in, "out": args.price_out}

    out["summary"] = summary
    out["finished_at"] = datetime.now(UTC).isoformat()

    target = RESULTS_DIR / f"bench-{args.provider}-{args.tag}.json"
    target.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1), flush=True)
    print(f"raw results -> {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
