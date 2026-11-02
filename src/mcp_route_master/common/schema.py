"""Optional tool fields that accept JSON null but publish a plain JSON-schema type.

Pydantic turns `str | None` into `anyOf: [string, null]`, which some function-calling
formats (older Gemini) reject. Plain `str` with a None default publishes cleanly but
rejects an explicit null, which models often send for fields they don't know. `opt()`
gives both: the schema says "string" and validation still accepts None.

Used for the optional parameters of the MCP tools in `server.py`. Each helper
returns an `Annotated` type: the real Python type (`X | None`) for validation,
plus `WithJsonSchema` to replace the published schema with the plain one. Only
the constraints listed in `_CONSTRAINT_KEYS` (and `description`) are copied to
that schema, so add a key there before relying on a new Field constraint.
"""

from __future__ import annotations

from typing import Annotated, Any, get_args

from pydantic import Field, WithJsonSchema

_JSON_TYPES = {str: "string", float: "number", int: "integer", bool: "boolean"}
# Pydantic Field keyword -> JSON-schema keyword, for the constraints we publish.
_CONSTRAINT_KEYS = {"max_length": "maxLength", "min_length": "minLength", "ge": "minimum", "le": "maximum"}


def _schema(json_type: str, **field_kwargs: Any) -> dict[str, Any]:
    """Plain JSON schema for a scalar type, carrying over description and constraints."""
    schema: dict[str, Any] = {"type": json_type}
    if "description" in field_kwargs:
        schema["description"] = field_kwargs["description"]
    for key, json_key in _CONSTRAINT_KEYS.items():
        if key in field_kwargs:
            schema[json_key] = field_kwargs[key]
    return schema


def _drop_default(schema: dict[str, Any]) -> None:
    # "default": null next to "type": "string" trips strict validators; omitting the field already means None.
    schema.pop("default", None)


def opt(tp: type, **field_kwargs: Any) -> Any:
    """`opt(str, description=..., max_length=80)` -> optional string field.

    `tp` must be one of str, float, int or bool.
    """
    return Annotated[
        tp | None,
        Field(json_schema_extra=_drop_default, **field_kwargs),
        WithJsonSchema(_schema(_JSON_TYPES[tp], **field_kwargs)),
    ]


def opt_list(item_tp: Any, items_schema: dict[str, Any], **field_kwargs: Any) -> Any:
    """Optional list whose items follow `items_schema` (e.g. {"type": "string", "enum": [...]})."""
    schema: dict[str, Any] = {"type": "array", "items": items_schema}
    if "description" in field_kwargs:
        schema["description"] = field_kwargs["description"]
    if "max_length" in field_kwargs:
        schema["maxItems"] = field_kwargs["max_length"]
    return Annotated[
        list[item_tp] | None, Field(json_schema_extra=_drop_default, **field_kwargs), WithJsonSchema(schema)
    ]


def opt_enum(literal_tp: Any, **field_kwargs: Any) -> Any:
    """Optional string restricted to the values of a Literal type."""
    schema: dict[str, Any] = {"type": "string", "enum": list(get_args(literal_tp))}
    if "description" in field_kwargs:
        schema["description"] = field_kwargs["description"]
    return Annotated[
        literal_tp | None, Field(json_schema_extra=_drop_default, **field_kwargs), WithJsonSchema(schema)
    ]
