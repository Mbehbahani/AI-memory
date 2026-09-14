# AI Memory V0.1 — embedding-service. Owner: A03 (infra, this Dockerfile); app code owner: A06 (P3-T03).
# Build context is the repo root:
#   docker build -f apps/embedding-service/Dockerfile -t aimemory/embedding-service:dev .
#
# Bakes all-MiniLM-L6-v2 into the image at build time (plan section N) so the runtime container never
# touches the network: HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 are set for both the bake step and the
# runtime. CPU-only torch wheels come from https://download.pytorch.org/whl/cpu (kept out of the default
# PyPI index to avoid pulling CUDA wheels) via infra/docker/requirements-embedding.lock, which pins both
# index URLs and every resolved version (sentence-transformers, torch==...+cpu, numpy, fastapi, uvicorn).
#
# apps/embedding-service/app.py in this build is A03's PLACEHOLDER (see the file's own docstring) —
# it exists only to prove the image builds, bakes the model, and serves /health. A06 REPLACES app.py
# in P3-T03 with the real /embed, /v1/embeddings implementation; this Dockerfile does not need to
# change when that happens (same COPY, same CMD) unless the entrypoint module name changes.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY infra/docker/requirements-embedding.lock /tmp/constraints.txt
COPY pyproject.toml README.md /app/
COPY packages/aimemory /app/packages/aimemory

# -r (not -c): this lock file carries --index-url/--extra-index-url directives that -c would ignore.
RUN pip install --no-cache-dir -r /tmp/constraints.txt \
    && pip install --no-cache-dir --no-deps -e ".[embedding,api]" \
    && rm -f /tmp/constraints.txt

ARG EMBED_MODEL_ID=sentence-transformers/all-MiniLM-L6-v2
ENV EMBED_MODEL_ID=${EMBED_MODEL_ID} \
    HF_HOME=/app/.cache/huggingface

# Bake the model into the image so runtime never needs network access.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBED_MODEL_ID}')"

COPY apps/embedding-service/app.py /app/apps/embedding-service/app.py
COPY config /app/config

RUN chown -R app:app /app
USER app

# Offline at runtime — the model is already on disk from the RUN step above.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

EXPOSE 8010
WORKDIR /app/apps/embedding-service
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]
