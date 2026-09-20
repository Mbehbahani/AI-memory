"""Argument validation against the frozen ``inputSchema`` from ``tools.json``. Owner: A10.

FastMCP validates a call against a pydantic model derived from the handler's Python signature. That
is good, but it is not the same object as the contract we advertise: pydantic ignores unknown keys by
default, so ``additionalProperties: false`` and the ADR-0008 numbers (``maxLength: 8000``,
``confirm: {"const": true}``) would be documentation rather than enforcement.

So every call is checked against the contract *first*, by this module. It implements exactly the
JSON-Schema subset ``tools.json`` uses - object/required/additionalProperties, type (incl. the
``["string", "null"]`` union form), minLength/maxLength, minimum/maximum, enum, const, array items
and maxItems - and refuses anything else with a message safe to hand back to a client. A keyword
appearing in the contract that this module does not know about raises at startup (see
:func:`assert_supported`), so the contract can never silently out-run the enforcement.

No dependency beyond the standard library: adding ``jsonschema`` for ~120 lines of well-understood
checking would have been a new pin for no gain.
"""

from __future__ import annotations

from typing import Any

__all__ = ["SchemaViolation", "assert_supported", "validate_arguments"]

#: Every keyword this module understands. Anything else in tools.json is a hard startup error.
SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "maxItems",
        "minItems",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "default",
        "description",
        "title",
        "$comment",
    }
)

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


class SchemaViolation(ValueError):
    """A client sent arguments the frozen contract does not allow.

    The message names the offending field and the rule; it never carries the value, because the value
    can be arbitrary user text (plan section T: nothing unbounded leaves the process in an error).
    """


def assert_supported(schema: dict[str, Any], *, where: str) -> None:
    """Raise if the contract uses a keyword :func:`validate_arguments` would silently ignore."""
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise NotImplementedError(
            f"{where}: unsupported JSON-Schema keyword(s) {sorted(unknown)} in tools.json"
        )
    for name, sub in (schema.get("properties") or {}).items():
        assert_supported(sub, where=f"{where}.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        assert_supported(items, where=f"{where}[]")


def _types_of(schema: dict[str, Any]) -> tuple[str, ...]:
    declared = schema.get("type")
    if declared is None:
        return ()
    if isinstance(declared, str):
        return (declared,)
    return tuple(str(t) for t in declared)


def _check_type(value: Any, schema: dict[str, Any], path: str) -> None:
    names = _types_of(schema)
    if not names:
        return
    allowed: tuple[type, ...] = tuple(t for name in names for t in _TYPE_MAP.get(name, ()))
    # bool is a subclass of int in Python; JSON Schema treats them as different types.
    if isinstance(value, bool) and "boolean" not in names:
        raise SchemaViolation(f"{path}: expected {' or '.join(names)}, got boolean")
    if not isinstance(value, allowed):
        got = "null" if value is None else type(value).__name__
        raise SchemaViolation(f"{path}: expected {' or '.join(names)}, got {got}")


def _check_value(value: Any, schema: dict[str, Any], path: str) -> None:
    _check_type(value, schema, path)

    if "const" in schema and value != schema["const"]:
        raise SchemaViolation(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaViolation(f"{path}: must be one of {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise SchemaViolation(f"{path}: shorter than the minimum {schema['minLength']} chars")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise SchemaViolation(
                f"{path}: {len(value)} chars exceeds the maximum of {schema['maxLength']}"
            )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaViolation(f"{path}: below the minimum of {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaViolation(f"{path}: above the maximum of {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise SchemaViolation(f"{path}: more than {schema['maxItems']} items")
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise SchemaViolation(f"{path}: fewer than {schema['minItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _check_value(item, item_schema, f"{path}[{index}]")
    if isinstance(value, dict) and schema.get("properties"):
        validate_arguments(value, schema, path=path)


def validate_arguments(
    arguments: dict[str, Any], schema: dict[str, Any], *, path: str = "arguments"
) -> dict[str, Any]:
    """Validate ``arguments`` against an object schema and return them with defaults applied.

    Raises :class:`SchemaViolation` on the first problem - one clear reason beats a list the client
    has to parse.
    """
    if not isinstance(arguments, dict):
        raise SchemaViolation(f"{path}: expected an object")

    for name in schema.get("required", []):
        if name not in arguments:
            raise SchemaViolation(f"{path}.{name}: required")

    properties: dict[str, Any] = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        extra = sorted(set(arguments) - set(properties))
        if extra:
            raise SchemaViolation(f"{path}: unknown argument(s) {extra}")

    for name, value in arguments.items():
        sub = properties.get(name)
        if isinstance(sub, dict):
            _check_value(value, sub, f"{path}.{name}")

    resolved = dict(arguments)
    for name, sub in properties.items():
        if name not in resolved and isinstance(sub, dict) and "default" in sub:
            resolved[name] = sub["default"]
    return resolved
