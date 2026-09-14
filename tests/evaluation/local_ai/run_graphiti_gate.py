"""P4-T04 — the ADR-0002 Graphiti compatibility gate, run exactly as plan section O specifies.

    docker build -f tests/evaluation/local_ai/graphiti.Dockerfile -t aimemory/graphiti-gate:dev .
    docker run --rm --network ai-memory_ai-memory-net --env-file .env \
        -v "D:/AI memory:/workspace" -v "D:/My-Vault:/sources/vault:ro" -w /workspace \
        aimemory/graphiti-gate:dev python -m tests.evaluation.local_ai.run_graphiti_gate

Wiring (plan section O): ``OpenAIGenericClient(base_url=<ollama>/v1, model=qwen3:4b)`` +
``OpenAIEmbedder(base_url=<embedding-service>/v1, embedding_dim=384)`` + Neo4j. No cloud key is used;
``api_key`` is a placeholder both local servers ignore.

Workload: the ten real my-vault episodes of ``episodes.py``, the Architecture-A/B supersession
fixture, and one pair run with 2 concurrent episodes.

Criteria and thresholds are copied from the plan; the decision rule (all pass -> Graphiti, any fail
-> native) is applied automatically and written to ``docs/adr/ADR-0009-knowledge-engine-verdict.md``
by hand from this output.

**Neo4j hygiene**: everything is written under ``group_id`` ``p4-graphiti-gate`` and every node
created also gets ``gate=true``; :func:`cleanup` deletes exactly those nodes and nothing else. The
gate never touches nodes it did not create and never drops a database or a volume.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aimemory.common.config import EmbeddingSettings, LLMSettings, Neo4jSettings

from .episodes import Episode, load_episodes
from .gate_expectations import EXPECTATIONS, normalize

GROUP_ID_BASE = "p4-graphiti-gate"
#: Set per run (``--llm``) so the ollama and bedrock gate runs never share nodes and each can be
#: cleaned up independently. Every node written also carries ``gate=true``.
GROUP_ID = GROUP_ID_BASE
RESULTS_DIR = Path(__file__).resolve().parent / "results"
FIXTURE_DIR = Path("tests/fixtures/mini-vault")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("gate")


# --------------------------------------------------------------------------------------------------
# instrumentation for C1
# --------------------------------------------------------------------------------------------------


@dataclass
class LLMStats:
    """Counts Graphiti's own LLM traffic. C1 is measured here, not guessed."""

    logical_calls: int = 0
    logical_failures: int = 0
    physical_attempts: int = 0
    physical_failures: int = 0
    failure_kinds: dict[str, int] = field(default_factory=dict)
    durations_s: list[float] = field(default_factory=list)

    def note_failure(self, exc: BaseException) -> None:
        key = type(exc).__name__
        self.failure_kinds[key] = self.failure_kinds.get(key, 0) + 1

    @property
    def parse_rate_pct(self) -> float:
        if self.logical_calls == 0:
            return 0.0
        return 100.0 * (self.logical_calls - self.logical_failures) / self.logical_calls


def instrument(client: Any, stats: LLMStats) -> None:
    """Wrap ``generate_response`` (logical) and ``_generate_response`` (physical attempt)."""
    outer = client.generate_response
    inner = client._generate_response

    async def wrapped_outer(*args: Any, **kwargs: Any) -> Any:
        stats.logical_calls += 1
        started = time.perf_counter()
        try:
            return await outer(*args, **kwargs)
        except BaseException as exc:
            stats.logical_failures += 1
            stats.note_failure(exc)
            raise
        finally:
            stats.durations_s.append(time.perf_counter() - started)

    async def wrapped_inner(*args: Any, **kwargs: Any) -> Any:
        stats.physical_attempts += 1
        try:
            return await inner(*args, **kwargs)
        except BaseException as exc:
            stats.physical_failures += 1
            stats.note_failure(exc)
            raise

    client.generate_response = wrapped_outer  # type: ignore[method-assign]
    client._generate_response = wrapped_inner  # type: ignore[method-assign]


# --------------------------------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------------------------------


def _safe_version(name: str) -> str | None:
    import importlib.metadata as _md

    try:
        return _md.version(name)
    except Exception:  # noqa: BLE001 - an absent optional dependency is not an error
        return None


def build_graphiti(stats: LLMStats, max_tokens: int, llm_provider: str, mode: str) -> Any:
    """Wire graphiti-core to the local stack (plan section O), or to Bedrock (ADR-0012).

    ``llm_provider="ollama"`` is the wiring plan section O specifies and the one the gate verdict is
    based on. ``llm_provider="bedrock"`` exists only to answer the question ADR-0012 raises: Graphiti
    issues 6-10 LLM calls per episode, so the C2 latency criterion is dominated by per-call latency,
    and the two providers differ by an order of magnitude there. Everything else - embedder, Neo4j,
    episodes, scoring - is identical between the two runs.
    """
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    llm = LLMSettings()
    emb = EmbeddingSettings()
    neo = Neo4jSettings()

    if llm_provider == "bedrock":
        from anthropic import AsyncAnthropicBedrock
        from graphiti_core.llm_client.anthropic_client import AnthropicClient

        bedrock_config = LLMConfig(
            api_key="bedrock-sigv4",  # unused: SigV4 comes from the boto3 credential chain
            model=llm.bedrock_model_id,
            small_model=llm.bedrock_model_id,
            temperature=0.0,
            max_tokens=max_tokens,
        )
        anthropic_kwargs: dict[str, Any] = {"aws_region": llm.bedrock_region}
        if llm.bedrock_profile:
            anthropic_kwargs["aws_profile"] = llm.bedrock_profile
        llm_client = AnthropicClient(
            config=bedrock_config,
            client=AsyncAnthropicBedrock(**anthropic_kwargs),
            max_tokens=max_tokens,
        )
        reranker_config = bedrock_config
    else:
        ollama_config = LLMConfig(
            api_key="ollama-local",
            model=llm.model,
            small_model=llm.model,
            base_url=llm.ollama_url.rstrip("/") + "/v1",
            temperature=0.0,
            max_tokens=max_tokens,
        )
        llm_client = OpenAIGenericClient(
            config=ollama_config, max_tokens=max_tokens, structured_output_mode=mode
        )
        reranker_config = ollama_config

    instrument(llm_client, stats)

    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key="embedding-service-local",
            embedding_model=emb.model_id,
            embedding_dim=emb.dimensions,
            base_url=emb.url.rstrip("/") + "/v1",
        )
    )
    # The cross-encoder is only used by search(); add_episode() does not call it. It is wired to the
    # local generic client in both runs so that nothing silently reaches a second backend.
    reranker = OpenAIRerankerClient(
        config=reranker_config
        if llm_provider == "ollama"
        else LLMConfig(
            api_key="ollama-local",
            model=llm.model,
            base_url=llm.ollama_url.rstrip("/") + "/v1",
            temperature=0.0,
            max_tokens=max_tokens,
        )
    )

    return Graphiti(
        neo.uri,
        neo.user,
        neo.password.get_secret_value(),
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=reranker,
    )


# --------------------------------------------------------------------------------------------------
# Neo4j helpers (raw driver - the projection layer is A04/A08's, not the gate's)
# --------------------------------------------------------------------------------------------------


def neo4j_driver() -> Any:
    from neo4j import GraphDatabase

    neo = Neo4jSettings()
    return GraphDatabase.driver(neo.uri, auth=(neo.user, neo.password.get_secret_value()))


def run_cypher(query: str, **params: Any) -> list[dict[str, Any]]:
    driver = neo4j_driver()
    try:
        with driver.session(database=Neo4jSettings().database) as session:
            return [record.data() for record in session.run(query, **params)]
    finally:
        driver.close()


def tag_gate_nodes() -> int:
    """Mark everything Graphiti wrote for this gate, so cleanup can be exact."""
    rows = run_cypher(
        "MATCH (n) WHERE n.group_id = $g SET n.gate = true RETURN count(n) AS c", g=GROUP_ID
    )
    return int(rows[0]["c"]) if rows else 0


def schema_snapshot() -> dict[str, list[str]]:
    """Index and constraint names currently in the database (A04 created 18 constraints)."""
    return {
        "indexes": sorted(r["name"] for r in run_cypher("SHOW INDEXES YIELD name RETURN name")),
        "constraints": sorted(
            r["name"] for r in run_cypher("SHOW CONSTRAINTS YIELD name RETURN name")
        ),
    }


def cleanup(baseline: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Delete only what this gate created. Never a volume, never a database, never someone else's node.

    Two things are removed:

    * every node carrying this run's ``group_id`` (re-tagged ``gate=true`` first, so the delete
      predicate is doubly specific), together with its relationships via ``DETACH DELETE``;
    * every index/constraint that did **not** exist in ``baseline`` - i.e. exactly the schema
      ``Graphiti.build_indices_and_constraints()`` added. A04's 18 constraints and 24 indexes were in
      the baseline and are therefore never touched.
    """
    tagged = tag_gate_nodes()
    rows = run_cypher(
        "MATCH (n) WHERE n.group_id = $g AND n.gate = true "
        "WITH n LIMIT 100000 DETACH DELETE n RETURN count(n) AS c",
        g=GROUP_ID,
    )
    deleted = int(rows[0]["c"]) if rows else 0
    left = run_cypher("MATCH (n) WHERE n.group_id = $g RETURN count(n) AS c", g=GROUP_ID)

    dropped: list[str] = []
    failed: list[str] = []
    if baseline:
        after = schema_snapshot()
        for name in sorted(set(after["constraints"]) - set(baseline["constraints"])):
            try:
                run_cypher(f"DROP CONSTRAINT {name}")
                dropped.append(f"constraint:{name}")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                failed.append(f"constraint:{name}: {type(exc).__name__}")
        for name in sorted(set(after["indexes"]) - set(baseline["indexes"])):
            try:
                run_cypher(f"DROP INDEX {name}")
                dropped.append(f"index:{name}")
            except Exception as exc:  # noqa: BLE001
                failed.append(f"index:{name}: {type(exc).__name__}")

    total = run_cypher("MATCH (n) RETURN count(n) AS c")
    return {
        "tagged": tagged,
        "deleted": deleted,
        "remaining_with_group_id": int(left[0]["c"]) if left else -1,
        "dropped_schema": dropped,
        "failed_to_drop": failed,
        "nodes_left_in_database": int(total[0]["c"]) if total else -1,
    }


# --------------------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------------------


def score_c3(produced_nodes: list[str], produced_edges: list[tuple[str, str]], key: str) -> dict:
    """Score one episode against the hand-listed expectations of ``gate_expectations.py``."""
    expectation = EXPECTATIONS[key]
    normalized_nodes = [normalize(n) for n in produced_nodes]

    found_entities: list[str] = []
    for expected in expectation.entities:
        hit = any(
            any(name == node or name in node or node in name for name in expected.names)
            for node in normalized_nodes
            if node
        )
        if hit:
            found_entities.append(expected.canonical)

    def _match(label: str, node: str) -> bool:
        target = normalize(label)
        return bool(node) and (target == node or target in node or node in target)

    normalized_edges = [(normalize(a), normalize(b)) for a, b in produced_edges]
    found_relationships: list[tuple[str, str]] = []
    for subject, obj in expectation.relationships:
        hit = any(
            (_match(subject, a) and _match(obj, b)) or (_match(subject, b) and _match(obj, a))
            for a, b in normalized_edges
        )
        if hit:
            found_relationships.append((subject, obj))

    n_entities = len(expectation.entities)
    n_relationships = len(expectation.relationships)
    return {
        "expected_entities": n_entities,
        "found_entities": len(found_entities),
        "entity_recall_pct": round(100.0 * len(found_entities) / max(1, n_entities), 1),
        "entities_found": found_entities,
        "entities_missed": [
            e.canonical for e in expectation.entities if e.canonical not in found_entities
        ],
        "expected_relationships": n_relationships,
        "found_relationships": len(found_relationships),
        "relationship_recall_pct": round(
            100.0 * len(found_relationships) / max(1, n_relationships), 1
        ),
        "relationships_found": [list(r) for r in found_relationships],
        "relationships_missed": [
            list(r) for r in expectation.relationships if r not in found_relationships
        ],
    }


def episode_graph(episode_uuid: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Entity node names and (source_name, target_name) edges attached to one episode."""
    nodes = run_cypher(
        "MATCH (ep:Episodic {uuid: $u})-[:MENTIONS]->(n:Entity) RETURN n.name AS name", u=episode_uuid
    )
    names = [row["name"] for row in nodes if row.get("name")]
    edges = run_cypher(
        "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
        "WHERE $u IN r.episodes RETURN a.name AS a, b.name AS b, r.name AS rel",
        u=episode_uuid,
    )
    return names, [(row["a"], row["b"]) for row in edges if row.get("a") and row.get("b")]


# --------------------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------------------


async def add(graphiti: Any, name: str, body: str, description: str, when: datetime) -> dict:
    from graphiti_core.nodes import EpisodeType

    started = time.perf_counter()
    record: dict[str, Any] = {"name": name, "chars": len(body)}
    try:
        result = await graphiti.add_episode(
            name=name,
            episode_body=body,
            source_description=description,
            reference_time=when,
            source=EpisodeType.text,
            group_id=GROUP_ID,
        )
        record["seconds"] = round(time.perf_counter() - started, 2)
        record["ok"] = True
        record["episode_uuid"] = getattr(result.episode, "uuid", None)
        record["n_nodes"] = len(getattr(result, "nodes", []) or [])
        record["n_edges"] = len(getattr(result, "edges", []) or [])
    except Exception as exc:  # noqa: BLE001 - unhandled exceptions ARE the C1 measurement
        record["seconds"] = round(time.perf_counter() - started, 2)
        record["ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()[-2000:]
        log.error("episode %s failed after %ss: %s", name, record["seconds"], exc)
    return record


async def run_gate(
    limit: int, max_tokens: int, llm_provider: str, mode: str, budget_s: float
) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stats = LLMStats()
    out: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "group_id": GROUP_ID,
        "max_tokens": max_tokens,
        "llm_provider": llm_provider,
        "structured_output_mode": mode if llm_provider == "ollama" else "anthropic_tool_use",
        "unhandled_exceptions": 0,
    }

    import importlib.metadata as _md

    out["graphiti_version"] = _md.version("graphiti-core")
    out["dependency_versions"] = {
        name: _safe_version(name)
        for name in ("openai", "neo4j", "pydantic", "httpx", "tenacity", "anthropic")
    }

    graphiti = build_graphiti(stats, max_tokens, llm_provider, mode)
    try:
        # ---------------------------------------------------------------- C6 part 1: indices
        out["schema_baseline"] = schema_snapshot()
        out["nodes_before"] = run_cypher("MATCH (n) RETURN count(n) AS c")[0]["c"]
        index_started = time.perf_counter()
        await graphiti.build_indices_and_constraints()
        out["build_indices_s"] = round(time.perf_counter() - index_started, 2)

        episodes: list[Episode] = load_episodes()[:limit]

        # ------------------------------------------------------- sequential episodes (C1/C2/C3)
        sequential: list[dict[str, Any]] = []
        phase_started = time.perf_counter()
        planned = episodes[:-2] if len(episodes) > 2 else episodes
        out["sequential_planned"] = [e.key for e in planned]
        for episode in planned:
            if budget_s and (time.perf_counter() - phase_started) > budget_s:
                # A wall-clock cap on the *sequential* phase only. C2 is a median/p90 over the
                # episodes that ran; stopping early can only ever make the measured latency look
                # better, never worse, so a C2 failure under the cap is still a real failure.
                out["sequential_truncated_after_s"] = round(time.perf_counter() - phase_started, 1)
                out["sequential_skipped"] = [
                    e.key for e in planned if e.key not in {r["key"] for r in sequential}
                ]
                print(
                    f"  [budget] stopping sequential phase after {len(sequential)} episodes",
                    flush=True,
                )
                break
            record = await add(
                graphiti,
                episode.key,
                episode.text,
                f"my-vault markdown {episode.relative_path}",
                datetime.now(UTC),
            )
            record["key"] = episode.key
            record["relative_path"] = episode.relative_path
            sequential.append(record)
            print(f"  {episode.key} {record['seconds']}s ok={record['ok']}", flush=True)
            (RESULTS_DIR / f"graphiti-gate-{llm_provider}.partial.json").write_text(
                json.dumps({**out, "sequential": sequential}, indent=1), encoding="utf-8"
            )
        out["sequential"] = sequential

        # ----------------------------------------------------------------- C5: 2 concurrent
        concurrent: list[dict[str, Any]] = []
        if len(episodes) > 2:
            pair = episodes[-2:]
            started = time.perf_counter()
            results = await asyncio.gather(
                *[
                    add(
                        graphiti,
                        e.key,
                        e.text,
                        f"my-vault markdown {e.relative_path}",
                        datetime.now(UTC),
                    )
                    for e in pair
                ],
                return_exceptions=True,
            )
            out["concurrent_wall_s"] = round(time.perf_counter() - started, 2)
            for episode, result in zip(pair, results, strict=True):
                if isinstance(result, BaseException):
                    concurrent.append(
                        {"key": episode.key, "ok": False, "error": repr(result), "seconds": None}
                    )
                else:
                    result["key"] = episode.key
                    result["relative_path"] = episode.relative_path
                    concurrent.append(result)
            print(f"  concurrent pair {out['concurrent_wall_s']}s", flush=True)
        out["concurrent"] = concurrent

        # ------------------------------------------------------------ C4: supersession fixture
        fixture: list[dict[str, Any]] = []
        for name, when in (
            ("architecture-decision-a", datetime(2026, 9, 1, tzinfo=UTC)),
            ("architecture-decision-b", datetime(2026, 9, 11, tzinfo=UTC)),
        ):
            path = FIXTURE_DIR / f"{name}.md"
            record = await add(
                graphiti, name, path.read_text(encoding="utf-8"), f"fixture {name}.md", when
            )
            fixture.append(record)
            print(f"  fixture {name} {record['seconds']}s ok={record['ok']}", flush=True)
        out["fixture"] = fixture

        # -------------------------------------------------------------------------- scoring
        scores: dict[str, Any] = {}
        for record in [*sequential, *concurrent]:
            key = record.get("key")
            if not key or not record.get("ok") or not record.get("episode_uuid"):
                continue
            names, edges = episode_graph(record["episode_uuid"])
            scores[key] = {**score_c3(names, edges, key), "produced_entities": names}
        out["c3_scores"] = scores

        # C4 evidence: temporal fields on the fixture's edges
        out["c4_edges"] = run_cypher(
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) WHERE r.group_id = $g "
            "AND (toLower(a.name) CONTAINS 'architecture' OR toLower(b.name) CONTAINS 'architecture') "
            "RETURN a.name AS a, r.name AS rel, b.name AS b, r.fact AS fact, "
            "toString(r.valid_at) AS valid_at, toString(r.invalid_at) AS invalid_at, "
            "toString(r.expired_at) AS expired_at LIMIT 100",
            g=GROUP_ID,
        )
        out["c4_all_expired"] = run_cypher(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.group_id = $g AND "
            "(r.invalid_at IS NOT NULL OR r.expired_at IS NOT NULL) "
            "RETURN count(r) AS c",
            g=GROUP_ID,
        )

        # C6 evidence: vector width actually stored + indices/constraints present
        out["c6_vector_dims"] = run_cypher(
            "MATCH (n:Entity) WHERE n.group_id = $g AND n.name_embedding IS NOT NULL "
            "RETURN size(n.name_embedding) AS dim, count(*) AS c ORDER BY dim",
            g=GROUP_ID,
        )
        out["c6_edge_vector_dims"] = run_cypher(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.group_id = $g AND r.fact_embedding IS NOT NULL "
            "RETURN size(r.fact_embedding) AS dim, count(*) AS c ORDER BY dim",
            g=GROUP_ID,
        )
        out["c6_indexes"] = run_cypher("SHOW INDEXES YIELD name, type, entityType, properties")
        out["c6_constraints"] = run_cypher("SHOW CONSTRAINTS YIELD name, type, labelsOrTypes")

        out["node_counts"] = run_cypher(
            "MATCH (n) WHERE n.group_id = $g RETURN labels(n) AS labels, count(*) AS c", g=GROUP_ID
        )
    finally:
        out["llm_stats"] = {
            "logical_calls": stats.logical_calls,
            "logical_failures": stats.logical_failures,
            "parse_rate_pct": round(stats.parse_rate_pct, 1),
            "physical_attempts": stats.physical_attempts,
            "physical_failures": stats.physical_failures,
            "failure_kinds": stats.failure_kinds,
            "median_call_s": round(statistics.median(stats.durations_s), 2)
            if stats.durations_s
            else None,
        }
        try:
            await graphiti.close()
        except Exception as exc:  # noqa: BLE001 - closing must never mask the gate result
            log.warning("graphiti.close() failed: %s", exc)

    out["finished_at"] = datetime.now(UTC).isoformat()
    return out


def summarize(out: dict[str, Any]) -> dict[str, Any]:
    """Apply the plan section O thresholds. No judgement calls, only arithmetic."""
    episodes = [r for r in [*out.get("sequential", []), *out.get("concurrent", [])]]
    ok = [r for r in episodes if r.get("ok")]
    times = [r["seconds"] for r in ok if r.get("seconds")]
    ordered = sorted(times)

    def _p(pct: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))]

    stats = out.get("llm_stats", {})
    scores = out.get("c3_scores", {})
    entity_pcts = [s["entity_recall_pct"] for s in scores.values()]
    rel_pcts = [s["relationship_recall_pct"] for s in scores.values()]

    c1 = stats.get("parse_rate_pct", 0.0) >= 90.0 and len(ok) == len(episodes)
    c2 = bool(times) and statistics.median(times) <= 240 and _p(90) <= 480
    c3 = bool(entity_pcts) and (
        statistics.mean(entity_pcts) >= 60.0 and statistics.mean(rel_pcts) >= 50.0
    )
    fixture_ok = all(r.get("ok") for r in out.get("fixture", [])) and bool(out.get("fixture"))
    invalidated = (out.get("c4_all_expired") or [{"c": 0}])[0].get("c", 0)
    c4 = fixture_ok and invalidated > 0
    c5 = bool(out.get("concurrent")) and all(r.get("ok") for r in out.get("concurrent", []))
    dims = {row["dim"] for row in out.get("c6_vector_dims", [])}
    c6 = dims == {384} and bool(out.get("c6_indexes"))

    return {
        "C1_schema_reliability": {
            "pass": c1,
            "parse_rate_pct": stats.get("parse_rate_pct"),
            "threshold": ">= 90 % and 0 unhandled exceptions",
            "episodes_ok": f"{len(ok)}/{len(episodes)}",
        },
        "C2_latency": {
            "pass": c2,
            "median_s": round(statistics.median(times), 1) if times else None,
            "p90_s": round(_p(90), 1) if times else None,
            "threshold": "median <= 240 s, p90 <= 480 s",
        },
        "C3_quality": {
            "pass": c3,
            "mean_entity_recall_pct": round(statistics.mean(entity_pcts), 1)
            if entity_pcts
            else None,
            "mean_relationship_recall_pct": round(statistics.mean(rel_pcts), 1)
            if rel_pcts
            else None,
            "threshold": ">= 60 % entities, >= 50 % relationships",
        },
        "C4_temporal": {
            "pass": c4,
            "fixture_episodes_ok": fixture_ok,
            "invalidated_or_expired_edges": invalidated,
            "threshold": "old edge invalidated, new edge current",
        },
        "C5_concurrency": {
            "pass": c5,
            "threshold": "2 parallel episodes complete without deadlock/corruption",
        },
        "C6_storage": {
            "pass": c6,
            "vector_dims": sorted(dims),
            "n_indexes": len(out.get("c6_indexes", [])),
            "n_constraints": len(out.get("c6_constraints", [])),
            "threshold": "384-d vectors stored; indices/constraints created",
        },
        "VERDICT": "graphiti" if all((c1, c2, c3, c4, c5, c6)) else "native",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10, help="how many vault episodes to run")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--llm", choices=("ollama", "bedrock"), default="ollama")
    parser.add_argument(
        "--budget-s",
        type=float,
        default=0.0,
        help="wall-clock cap on the sequential phase; 0 = run all episodes",
    )
    parser.add_argument(
        "--structured-output-mode",
        choices=("json_schema", "json_object"),
        default="json_schema",
        help="graphiti-core OpenAIGenericClient mode; ollama only",
    )
    args = parser.parse_args()

    global GROUP_ID
    GROUP_ID = f"{GROUP_ID_BASE}-{args.llm}"

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.cleanup_only:
        print(json.dumps(cleanup(), indent=1))
        return 0

    out = asyncio.run(
        run_gate(
            args.limit, args.max_tokens, args.llm, args.structured_output_mode, args.budget_s
        )
    )
    out["criteria"] = summarize(out)
    out["cleanup"] = cleanup(out.get("schema_baseline"))
    target = RESULTS_DIR / f"graphiti-gate-{args.llm}.json"
    target.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["criteria"], indent=1), flush=True)
    print(json.dumps(out["cleanup"], indent=1), flush=True)
    print(f"raw results -> {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
