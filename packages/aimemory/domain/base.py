"""Shared pydantic base classes for the domain layer.

Consumers: every model in :mod:`aimemory.domain`. A04 maps these models to SQLAlchemy rows,
A09 serializes them over REST, A10 over MCP.

Two bases exist:

* :class:`DomainModel` - ``extra="forbid"``. Used for everything the system itself writes, so a typo
  in a field name fails loudly instead of being dropped on the floor.
* :class:`OpenModel` - ``extra="allow"``. Used only for payloads that come from outside (LLM output
  wrappers, engine-specific metadata) where forward compatibility matters more than strictness.

Both serialize datetimes as ISO-8601 with an explicit offset and validate on assignment, so a model
mutated in place stays legal.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["DomainModel", "OpenModel"]


class DomainModel(BaseModel):
    """Strict base: unknown fields are an error; assignment is validated."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class OpenModel(BaseModel):
    """Lenient base for externally produced payloads (extra keys are kept, not rejected)."""

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        use_enum_values=False,
    )
