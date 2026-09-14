#!/bin/sh
# One-shot init: ensure the extraction model is present. Idempotent. Runs inside the ollama image.
set -eu
MODEL="${LLM_MODEL:-qwen3:4b}"
echo "[ollama-init] checking for model: $MODEL"
if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
  echo "[ollama-init] model present: $MODEL"
else
  echo "[ollama-init] pulling $MODEL (one-time download)"
  ollama pull "$MODEL"
fi
ollama show "$MODEL" --modelfile | head -n 5 || true
echo "[ollama-init] done"
