"""AI Memory V0.1 — embedding-service (P3-T03, owner A06).

FastAPI wrapper around ``sentence-transformers/all-MiniLM-L6-v2`` (384-d, L2-normalized, CPU only;
plan section N). The model is baked into the Docker image at build time (see the Dockerfile in this
directory, owned by A03); ``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1`` are set for the runtime
container so no network access is required or attempted — this is the P3-T03 acceptance criterion,
verified by running the built image with ``docker run --network none``.

Routes
------
``POST /embed``
    ``{"texts": [...], "kind": "query"|"passage", "normalize": true}`` ->
    ``{"embeddings": [[...]], "model": ..., "model_id": ..., "dimension": 384, "dimensions": 384,
    "revision": ..., "normalized": true, "duration_ms": ...}``.
    ``model``/``model_id`` and ``dimension``/``dimensions`` are duplicate scalar aliases (cheap — no
    vector data is duplicated) kept so both naming conventions used across the plan documents resolve
    to the same value. ``kind`` is accepted for a future asymmetric model; MiniLM embeds queries and
    passages identically today.

``POST /v1/embeddings``
    OpenAI-compatible request/response so ``graphiti-core``'s ``OpenAIEmbedder`` (and any other
    OpenAI-client-based tool) works unchanged against ``base_url=http://embedding-service:8010/v1``,
    ``embedding_dim=384``. Supports ``encoding_format: "float"`` (default) and ``"base64"`` (raw
    little-endian float32 bytes, base64-encoded — the same wire format the real OpenAI API uses),
    matching what the official ``openai`` Python client asks for by default. Unknown fields
    (``dimensions``, ``user``, ``encoding_format`` itself, etc.) are accepted and ignored where they
    don't change behaviour.

``GET /health``
    Cheap: no inference. Reports whether the model is loaded, its id, dimension, HF revision hash,
    normalization default and the configured torch thread count, so the compose healthcheck
    (``interval: 15s``) never contends with real traffic.

Consumers: ``packages/aimemory/providers/embedding/http.py`` (``HttpEmbeddingProvider``, same repo),
``graphiti-core`` (A05, P4 gate).
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

# Must be set before sentence-transformers/huggingface_hub import anything network-related.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

MODEL_ID = os.environ.get("EMBED_MODEL_ID", "sentence-transformers/all-MiniLM-L6-v2")
MAX_SEQ = int(os.environ.get("EMBED_MAX_SEQ", "256"))
BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))
TORCH_NUM_THREADS = int(os.environ.get("TORCH_NUM_THREADS", "2"))
CONFIGURED_DIMENSIONS = int(os.environ.get("EMBED_DIMENSIONS", "384"))

MAX_TEXTS = 512
MAX_CHARS = 8000

torch.set_num_threads(TORCH_NUM_THREADS)


class _ModelState:
    """Module-level singleton populated once at startup (see ``lifespan`` below)."""

    model: Any = None
    dimension: int = CONFIGURED_DIMENSIONS
    revision: str | None = None
    load_seconds: float | None = None


_state = _ModelState()


def _resolve_revision(model_id: str) -> str | None:
    """Best-effort HF snapshot revision hash from the local (offline) cache — no network call."""
    hf_home = Path(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")))
    cache_dir_name = "models--" + model_id.replace("/", "--")
    snapshots_dir = hf_home / "hub" / cache_dir_name / "snapshots"
    if not snapshots_dir.is_dir():
        return None
    entries = sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir())
    return entries[0] if entries else None


def _load_model() -> Any:
    from sentence_transformers import SentenceTransformer

    start = time.perf_counter()
    model = SentenceTransformer(MODEL_ID, device="cpu")
    model.max_seq_length = MAX_SEQ
    _state.load_seconds = time.perf_counter() - start
    _state.dimension = int(model.get_sentence_embedding_dimension())
    _state.revision = _resolve_revision(MODEL_ID)
    return model


@asynccontextmanager
async def lifespan(_: FastAPI):
    _state.model = _load_model()
    yield
    _state.model = None


app = FastAPI(title="aimemory-embedding-service", lifespan=lifespan)


# --------------------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------------------


def _validate_texts(texts: list[str]) -> None:
    if not texts:
        raise HTTPException(status_code=422, detail="texts must be a non-empty list")
    if len(texts) > MAX_TEXTS:
        raise HTTPException(
            status_code=422, detail=f"at most {MAX_TEXTS} texts per request (got {len(texts)})"
        )
    for i, text in enumerate(texts):
        if not isinstance(text, str):
            raise HTTPException(status_code=422, detail=f"texts[{i}] must be a string")
        if len(text) > MAX_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"texts[{i}] exceeds {MAX_CHARS} characters (got {len(text)})",
            )


def _encode(texts: list[str], *, normalize: bool) -> tuple[list[list[float]], int]:
    if _state.model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    vectors = _state.model.encode(
        texts,
        batch_size=BATCH_SIZE,
        normalize_embeddings=normalize,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vectors.tolist(), int(vectors.shape[1]) if len(vectors.shape) > 1 else _state.dimension


def _count_tokens(texts: Iterable[str]) -> int:
    """Real token count via the model's own tokenizer — used for the OpenAI-shaped ``usage`` block."""
    if _state.model is None:
        return 0
    tokenizer = getattr(_state.model, "tokenizer", None)
    if tokenizer is None:
        return sum(len(t.split()) for t in texts)
    encoded = tokenizer(list(texts), truncation=True, max_length=MAX_SEQ)
    return sum(len(ids) for ids in encoded["input_ids"])


# --------------------------------------------------------------------------------------------------
# GET /health
# --------------------------------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _state.model is not None else "loading",
        "model_loaded": _state.model is not None,
        "model": MODEL_ID,
        "model_id": MODEL_ID,
        "dimension": _state.dimension,
        "dimensions": _state.dimension,
        "revision": _state.revision,
        "normalized": True,
        "max_seq": MAX_SEQ,
        "torch_num_threads": torch.get_num_threads(),
        "offline": os.environ.get("HF_HUB_OFFLINE") == "1",
        "load_seconds": _state.load_seconds,
    }


# --------------------------------------------------------------------------------------------------
# POST /embed
# --------------------------------------------------------------------------------------------------


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    kind: Literal["query", "passage"] = "passage"
    normalize: bool = True


@app.post("/embed")
def embed(payload: EmbedRequest) -> dict:
    _validate_texts(payload.texts)
    start = time.perf_counter()
    vectors, dimension = _encode(payload.texts, normalize=payload.normalize)
    duration_ms = int((time.perf_counter() - start) * 1000)
    return {
        "embeddings": vectors,
        "vectors": vectors,
        "model": MODEL_ID,
        "model_id": MODEL_ID,
        "dimension": dimension,
        "dimensions": dimension,
        "revision": _state.revision,
        "normalized": payload.normalize,
        "duration_ms": duration_ms,
    }


# --------------------------------------------------------------------------------------------------
# POST /v1/embeddings — OpenAI-compatible
# --------------------------------------------------------------------------------------------------


class OpenAIEmbeddingsRequest(BaseModel):
    """Mirrors the OpenAI ``/v1/embeddings`` request. Extra fields (``user``, ``dimensions``, ...)
    are accepted and ignored — ``model_config`` below allows them instead of rejecting the call, since
    graphiti-core / the openai SDK may send fields this service doesn't need."""

    model_config = {"extra": "allow"}

    input: str | list[str]
    model: str | None = None
    encoding_format: Literal["float", "base64"] = "float"

    @field_validator("input")
    @classmethod
    def _as_list(cls, value: str | list[str]) -> str | list[str]:
        return value


def _float_to_base64(vector: list[float]) -> str:
    arr = np.asarray(vector, dtype="<f4")  # little-endian float32, matches the real OpenAI API
    return base64.b64encode(arr.tobytes()).decode("ascii")


@app.post("/v1/embeddings")
def openai_embeddings(payload: OpenAIEmbeddingsRequest) -> dict:
    texts = [payload.input] if isinstance(payload.input, str) else list(payload.input)
    _validate_texts(texts)
    vectors, dimension = _encode(texts, normalize=True)
    prompt_tokens = _count_tokens(texts)

    if payload.encoding_format == "base64":
        data = [
            {"object": "embedding", "index": i, "embedding": _float_to_base64(v)}
            for i, v in enumerate(vectors)
        ]
    else:
        data = [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ]

    return {
        "object": "list",
        "data": data,
        "model": payload.model or MODEL_ID,
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        # Non-standard, additive: lets a caller that knows about us confirm the real dimension
        # without a round trip to /health.
        "dimension": dimension,
    }
