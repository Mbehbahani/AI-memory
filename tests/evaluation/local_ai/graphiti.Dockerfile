# Throwaway image for the ADR-0002 Graphiti gate (P4-T04, owner A05).
#
# graphiti-core is an optional extra in pyproject.toml ([graphiti]) and is deliberately NOT part of
# the runtime images: ADR-0002 says the dependency is only adopted if the gate passes. This image
# exists so the gate can be run without contaminating infra/docker/*.
#
#   docker build -f tests/evaluation/local_ai/graphiti.Dockerfile -t aimemory/graphiti-gate:dev .
#
# The resolved graphiti-core version is pinned in reports/graphiti-gate.md after the build, because
# "which version was measured" is part of the evidence.

FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

COPY pyproject.toml README.md /workspace/
COPY packages/aimemory /workspace/packages/aimemory

RUN pip install --no-cache-dir -e ".[dev]" "graphiti-core" \
    && pip freeze | grep -iE "^(graphiti-core|openai|neo4j|pydantic|httpx|tenacity)=" > /graphiti-versions.txt \
    && cat /graphiti-versions.txt

CMD ["python", "-m", "tests.evaluation.local_ai.run_graphiti_gate"]
