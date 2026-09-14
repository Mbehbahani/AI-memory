"""P4 local-AI validation harness (owner A05).

``test_ollama_provider.py``  - P4-T01 unit tests, HTTP fully mocked.
``test_ollama_live.py``      - P4-T01 live test against the running Ollama container.
``episodes.py``              - builds ~800-token episodes from the read-only my-vault mount.
``prompts.py``               - the extraction prompt used for the P4-T02 validity measurement.
``run_qwen_benchmark.py``    - P4-T02 measurement run -> reports/local-ai-validation.md.
``run_graphiti_gate.py``     - P4-T04 ADR-0002 gate -> reports/graphiti-gate.md.
``gate_expectations.py``     - hand-listed C3 entities/relationships, written before the gate ran.
"""
