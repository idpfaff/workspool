"""Compact argument diagnostics using the MCP SDK's actual validation model.

Call after authorization and before the handler. This does not execute tools,
change arguments, or replace SDK validation; it preserves SDK JSON pre-parsing
and coercions while rejecting silently ignored object fields. Open dictionaries
remain open. Only schema-derived types and field paths enter a diagnostic, never
Pydantic messages, input values, or validator context.
"""

import json
import re
from typing import Any

from mcp.server.fastmcp.tools.base import Tool
from pydantic import ValidationError


def _resolve(schema: dict, definitions: dict) -> dict:
    ref = schema.get("$ref", "")
    return definitions.get(ref.removeprefix("#/$defs/"), schema)


def _branches(schema: dict, definitions: dict) -> list[dict]:
    schema = _resolve(schema, definitions)
    variants = schema.get("anyOf", schema.get("oneOf"))
    if variants:
        return [part for item in variants for part in _branches(item, definitions)]
    return [schema]


def _type(schema: dict, definitions: dict) -> str:
    schema = _resolve(schema, definitions)
    variants = schema.get("anyOf", schema.get("oneOf"))
    if variants:
        return " | ".join(dict.fromkeys(_type(item, definitions) for item in variants))
    if "enum" in schema or "const" in schema:
        values = schema.get("enum", [schema.get("const")])
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in values)
    kind = schema.get("type", "any")
    if kind == "array":
        item = _type(schema.get("items", {}), definitions)
        return f"({item})[]" if " | " in item else f"{item}[]"
    if kind == "object" and isinstance(schema.get("additionalProperties"), dict):
        return f"object<string, {_type(schema['additionalProperties'], definitions)}>"
    return kind


def _expected(schema: dict, definitions: dict) -> str:
    """Bounds come from the schema, never from Pydantic's input-bearing message."""
    schema = _resolve(schema, definitions)
    variants = schema.get("anyOf", schema.get("oneOf"))
    if variants:
        return " | ".join(
            dict.fromkeys(_expected(item, definitions) for item in variants)
        )
    result = _type(schema, definitions)
    constraints = []
    for key, label in (
        ("minimum", ">="),
        ("maximum", "<="),
        ("exclusiveMinimum", ">"),
        ("exclusiveMaximum", "<"),
        ("minLength", "length >="),
        ("maxLength", "length <="),
        ("minItems", "items >="),
        ("maxItems", "items <="),
    ):
        if key in schema:
            constraints.append(f"{label} {schema[key]}")
    return result + (" (" + ", ".join(constraints) + ")" if constraints else "")


def _field(key: Any) -> str:
    # Arbitrary dictionary keys can themselves contain private data. Only short
    # identifier-shaped unknown *field names* are useful in an error message.
    return (
        key
        if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][\w]{0,63}", key)
        else "<unknown>"
    )


def _child(path: str, field: str) -> str:
    return f"{path}.{field}" if path and field else path or field


def _unknown(value: Any, schema: dict, definitions: dict, path=""):
    branches = _branches(schema, definitions)
    shape = (
        "object"
        if isinstance(value, dict)
        else "array"
        if isinstance(value, list)
        else None
    )
    candidates = [item for item in branches if item == {} or item.get("type") == shape]
    if len(candidates) > 1:
        # Preserve each union alternative's nested fields. Merging properties
        # would overwrite equally named fields with incompatible submodels.
        failures = [_unknown(value, item, definitions, path) for item in candidates]
        return failures[0] if all(failures) else None
    if isinstance(value, dict):
        objects = [item for item in branches if item.get("type") == "object"]
        if not objects or any(item == {} for item in branches):
            return None
        properties = {
            key: item
            for branch in objects
            for key, item in branch.get("properties", {}).items()
        }
        open_maps = [
            branch
            for branch in objects
            if "properties" not in branch
            or branch.get("additionalProperties") is True
            or isinstance(branch.get("additionalProperties"), dict)
        ]
        for key, item in value.items():
            if key not in properties:
                if not open_maps:
                    return _child(path, _field(key)), schema
                # Keys of a typed map are data, but its values can be models.
                mapped = open_maps[0].get("additionalProperties", {})
                if isinstance(mapped, dict):
                    found = _unknown(item, mapped, definitions, path + "[*]")
                    if found:
                        return found
            else:
                found = _unknown(item, properties[key], definitions, _child(path, key))
                if found:
                    return found
    elif isinstance(value, list):
        arrays = [item for item in branches if item.get("type") == "array"]
        if arrays:
            for index, item in enumerate(value):
                found = _unknown(
                    item, arrays[0].get("items", {}), definitions, f"{path}[{index}]"
                )
                if found:
                    return found
    return None


def _location(schema: dict, definitions: dict, location: tuple) -> tuple[str, dict]:
    """Resolve real schema fields, omitting Pydantic's synthetic union labels."""
    path = ""
    for part in location:
        for branch in _branches(schema, definitions):
            if isinstance(part, int) and branch.get("type") == "array":
                path += f"[{part}]"
                schema = branch.get("items", {})
                break
            if part in branch.get("properties", {}):
                path = _child(path, part)
                schema = branch["properties"][part]
                break
            mapped = branch.get("additionalProperties")
            if isinstance(mapped, dict):
                path += "[*]"
                schema = mapped
                break
    return path, schema


def _accepts(schema: dict, definitions: dict, path: str) -> dict[str, str]:
    result = {
        key: _type(value, definitions)
        for key, value in schema.get("properties", {}).items()
    }
    # Include nested model fields only along the failing path. Map keys are not
    # listed: those are caller data, not accepted parameter names.
    current = schema
    prefix = ""
    for part in re.findall(r"[^.\[\]]+|\[\d+\]|\[\*\]", path):
        for branch in _branches(current, definitions):
            if part.startswith("[") and branch.get("type") == "array":
                prefix += "[]"
                current = branch.get("items", {})
                break
            if part in branch.get("properties", {}):
                prefix = _child(prefix, part)
                current = branch["properties"][part]
                break
            if part == "[*]" and isinstance(branch.get("additionalProperties"), dict):
                prefix += "[*]"
                current = branch["additionalProperties"]
                break
        else:
            break
        for branch in _branches(current, definitions):
            for key, value in branch.get("properties", {}).items():
                result[_child(prefix, key)] = _type(value, definitions)
    return result


def argument_error(tool: Tool, arguments: object) -> dict[str, Any] | None:
    """Return a safe actionable error payload, or None for accepted arguments.

    Unknown fields take priority over missing fields so a misspelled parameter
    can be corrected in one call. Only the first failure is rendered; ``accepts``
    retains every top-level argument and the relevant nested model's fields.
    Authorization must run first: this function does not authorize a caller.
    """
    schema = tool.parameters
    definitions = schema.get("$defs", {})
    path = ""
    if not isinstance(arguments, dict):
        message = f"{tool.name} arguments must be object"
    else:
        parsed = tool.fn_metadata.pre_parse_json(arguments)
        unknown = _unknown(parsed, schema, definitions)
        if unknown:
            path, _ = unknown
            parent, _, field = path.rpartition(".")
            message = f"{_child(tool.name, parent)} has no field {field}"
        else:
            try:
                tool.fn_metadata.arg_model.model_validate(parsed)
                return None
            except ValidationError as error:
                issue = error.errors(
                    include_url=False, include_context=False, include_input=False
                )[0]
                path, expected = _location(schema, definitions, issue["loc"])
                if issue["type"] == "missing":
                    parent, _, field = path.rpartition(".")
                    message = f"{_child(tool.name, parent)} requires {field}"
                else:
                    message = f"{_child(tool.name, path)} must be {_expected(expected, definitions)}"
    return {"error": message, "accepts": _accepts(schema, definitions, path)}
