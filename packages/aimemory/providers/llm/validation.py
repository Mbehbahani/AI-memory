"""JSON-Schema validation for LLM output (P4-T01, owner A05).

Ollama's ``format=<schema>`` performs grammar-constrained decoding, but the guarantee is not total:
the model can still stop early (truncated JSON at ``num_predict``), and older/partial grammar support
lets ``maxLength`` / ``minItems`` style keywords through. The provider therefore re-validates every
decoded object against the *same* schema it sent, and that verdict is what
:class:`~aimemory.domain.ports.LLMResponse.valid` reports and what the P4-T02 validity rate counts.

``jsonschema`` is used when importable (it is in ``infra/docker/requirements.lock`` via ``mcp``);
otherwise a small built-in checker covers the keyword subset actually used by
``schemas/extraction/*.json``: ``type``, ``required``, ``enum``, ``properties``,
``additionalProperties``, ``items``, ``minLength``/``maxLength``, ``minItems``/``maxItems``,
``minimum``/``maximum``. The fallback is deliberately conservative - it never reports an error a full
validator would not report - so a missing optional dependency cannot inflate the failure rate.
"""

from __future__ import annotations

from typing import Any

__all__ = ["VALIDATOR_BACKEND", "validate_json"]

try:  # pragma: no cover - import-path dependent
    from importlib.metadata import version as _version

    import jsonschema as _jsonschema

    VALIDATOR_BACKEND = f"jsonschema {_version('jsonschema')}"
except ImportError:  # pragma: no cover - fallback path
    _jsonschema = None  # type: ignore[assignment]
    VALIDATOR_BACKEND = "builtin-subset"

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def validate_json(instance: Any, schema: dict[str, Any], *, limit: int = 10) -> list[str]:
    """Return a list of human-readable validation errors; empty means valid.

    Messages are short and structural (``path: problem``) so they can be fed straight back to the
    model as retry feedback and stored in ``LLMResponse.errors`` without leaking content.
    """
    if _jsonschema is not None:
        validator_cls = _jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
        found: list[str] = []
        for err in validator.iter_errors(instance):
            where = "/".join(str(p) for p in err.absolute_path) or "<root>"
            found.append(f"{where}: {err.message}")
            if len(found) >= limit:
                break
        return found
    errors: list[str] = []
    _check(instance, schema, "<root>", errors, limit)
    return errors[:limit]


def _check(value: Any, schema: dict[str, Any], path: str, errors: list[str], limit: int) -> None:
    if len(errors) >= limit or not isinstance(schema, dict):
        return

    expected = schema.get("type")
    if expected is not None:
        names = [expected] if isinstance(expected, str) else list(expected)
        allowed = tuple(
            t
            for name in names
            for t in (_TYPES[name] if isinstance(_TYPES[name], tuple) else (_TYPES[name],))
            if name in _TYPES
        )
        if allowed:
            ok = isinstance(value, allowed) and not (
                isinstance(value, bool) and "boolean" not in names
            )
            if not ok:
                errors.append(f"{path}: expected type {'|'.join(names)}, got {type(value).__name__}")
                return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")

    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property '{name}'")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: additional property '{key}' is not allowed")
        for key, sub in properties.items():
            if key in value:
                _check(value[key], sub, f"{path}/{key}", errors, limit)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _check(item, item_schema, f"{path}/{i}", errors, limit)
