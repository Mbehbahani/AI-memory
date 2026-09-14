"""PLACEHOLDER — created by A03 (infra) only to prove the Docker build/healthcheck in P2-T02/P3.

A06 owns the real application (P3-T03): `/embed`, `/v1/embeddings`, `/health`, model identity,
batching, normalization (plan section N). REPLACE THIS FILE. It intentionally does nothing beyond
loading the baked MiniLM model once at import time (proving the offline bake works) and exposing a
`/health` endpoint so docker-compose healthchecks and `scripts/doctor.ps1` have something to probe
before A06's implementation lands.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

app = FastAPI(title="aimemory-embedding-service (PLACEHOLDER)")

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        model_id = os.environ.get("EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")
        _model = SentenceTransformer(model_id)
    return _model


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "placeholder": True, "owner": "A06 replaces this in P3-T03"}


@app.post("/embed")
def embed(payload: dict) -> dict:
    texts = payload.get("input") or payload.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    vectors = _get_model().encode(texts, normalize_embeddings=True).tolist()
    return {"vectors": vectors, "dimensions": len(vectors[0]) if vectors else 0}
