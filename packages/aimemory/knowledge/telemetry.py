"""Records what each LLM call consumed: tokens, latency, model, and the price applied.

The provider already measures all of this (`bedrock_provider.py` fills `prompt_tokens`,
`completion_tokens` and `duration_ms` on every completion) and, before this module existed, threw
it away. The result was a system that could not answer "what did that cost" about its own work.

Two deliberate choices:

* **The rate is copied onto the row, not looked up later.** AWS changes Bedrock prices whenever it
  likes. If cost were computed at read time from the current rate table, every historical number
  would silently re-price itself on the next edit, and a cost report would stop meaning anything.
* **Recording never breaks extraction.** :func:`record_call` swallows its own failures. Telemetry
  that can take down the thing it is measuring is worse than no telemetry - and an extraction call
  has already been *paid for* by the time we get here, so losing the work to a bookkeeping error is
  the most expensive possible failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from ..common.ids import new_id
from ..common.logging import get_logger

__all__ = ["CallRecord", "load_rates", "rate_for", "record_call"]

logger = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
RATES_PATH = REPO_ROOT / "config" / "model-rates.yaml"


@dataclass(slots=True)
class CallRecord:
    """One call to a language model. Field names match the ``llm_calls`` columns exactly."""

    provider: str
    model_id: str
    purpose: str = "other"
    episode_id: UUID | None = None
    run_id: UUID | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    duration_ms: int | None = None
    attempts: int = 1
    ok: bool = True
    error: str | None = None


@lru_cache(maxsize=1)
def load_rates(path: str | None = None) -> dict[str, dict[str, dict[str, float]]]:
    """Read ``config/model-rates.yaml``. A missing or unreadable file means "no rates known".

    Returning an empty mapping is the correct degradation: calls are still recorded in full and
    their cost is simply left empty, which the ops page reports as unpriced. The alternative -
    falling back to a hardcoded price - would produce numbers that look authoritative and are not.
    """
    import yaml  # noqa: PLC0415 - optional at import time

    target = Path(path) if path else RATES_PATH
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("telemetry.rates_unreadable", path=str(target), error=str(exc)[:200])
        return {}
    providers = loaded.get("providers")
    return providers if isinstance(providers, dict) else {}


def rate_for(provider: str, model_id: str) -> tuple[Decimal | None, Decimal | None]:
    """``(input_rate, output_rate)`` per million tokens, or ``(None, None)`` when unknown.

    Unknown is a first-class answer. A model absent from the rate file records its tokens and
    abstains on cost; it does not borrow another model's price.
    """
    rates = load_rates().get(provider) or {}
    # Model ids arrive in two shapes and both must resolve. The engine reports its identity as
    # "bedrock:us.anthropic.claude-haiku-..." (provider-qualified, so a fact can say which model
    # produced it), while the rate file is keyed by the bare model name a person would copy off the
    # AWS pricing page. MEASURED: the first real extraction recorded every token correctly and left
    # cost empty for exactly this reason - the abstain-on-unknown rule worked, but the rate was
    # there all along. Try the id as given, then without a leading "<provider>:".
    entry = rates.get(model_id)
    if entry is None and model_id.startswith(f"{provider}:"):
        entry = rates.get(model_id.split(":", 1)[1])
    if not isinstance(entry, dict):
        return None, None
    try:
        return (
            Decimal(str(entry["input_per_mtok"])),
            Decimal(str(entry["output_per_mtok"])),
        )
    except (KeyError, ArithmeticError, TypeError, ValueError):
        logger.warning("telemetry.rate_malformed", provider=provider, model_id=model_id)
        return None, None


def record_call(session: Any, record: CallRecord) -> None:
    """Write one row to ``llm_calls``. Never raises.

    The caller is mid-extraction and the model has already been paid for; a bookkeeping failure
    must not cost that work. Failures are logged and dropped.
    """
    from sqlalchemy import text as sql_text  # noqa: PLC0415

    rate_in, rate_out = rate_for(record.provider, record.model_id)
    try:
        session.execute(
            sql_text(
                """
                INSERT INTO llm_calls
                    (id, provider, model_id, purpose, episode_id, run_id, prompt_tokens,
                     completion_tokens, duration_ms, attempts, ok, error,
                     rate_input_per_mtok, rate_output_per_mtok)
                VALUES
                    (:id, :provider, :model_id, :purpose, :episode_id, :run_id, :prompt_tokens,
                     :completion_tokens, :duration_ms, :attempts, :ok, :error,
                     :rate_in, :rate_out)
                """
            ),
            {
                "id": new_id(),
                "provider": record.provider,
                "model_id": record.model_id,
                "purpose": record.purpose,
                "episode_id": record.episode_id,
                "run_id": record.run_id,
                "prompt_tokens": record.prompt_tokens,
                "completion_tokens": record.completion_tokens,
                "duration_ms": record.duration_ms,
                "attempts": max(1, int(record.attempts or 1)),
                "ok": bool(record.ok),
                "error": (record.error or None) if not record.ok else None,
                "rate_in": rate_in,
                "rate_out": rate_out,
            },
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the work it measures
        logger.warning(
            "telemetry.record_failed",
            model_id=record.model_id,
            purpose=record.purpose,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
