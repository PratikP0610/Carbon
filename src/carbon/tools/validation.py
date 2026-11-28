"""Strict runtime JSON-schema validator for tool arguments and model outputs."""

import math
from typing import Any


class ToolArgumentValidationError(ValueError):
    """Raised when tool arguments violate the registered JSON schema."""


def validate_against_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "arguments",
) -> dict[str, Any]:
    """Validate and return a sanitized copy of object `value` against `schema`.

    Rejects missing required fields, type mismatches (including Python `bool` where
    `integer`/`number` is declared), disallowed extra properties, numeric/string bound
    violations, enum mismatches, and nested constraint violations.
    """
    if not isinstance(value, dict):
        raise ToolArgumentValidationError(
            f"Invalid {path}: expected object/dict, got {type(value).__name__}."
        )
    _validate_node(value, schema, path=path)
    return _copy_clean(value)


def _copy_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _copy_clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_clean(item) for item in value]
    return value


def _validate_node(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not schema:
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        _check_type(value, expected_type, path=path)

    if "enum" in schema and value not in schema["enum"]:
        raise ToolArgumentValidationError(
            f"Invalid value at '{path}': must be one of {schema['enum']}."
        )

    if expected_type == "object" or isinstance(value, dict):
        if not isinstance(value, dict):
            return
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        additional_allowed = schema.get("additionalProperties", True)

        for req_key in required:
            if req_key not in value:
                raise ToolArgumentValidationError(
                    f"Missing required field '{req_key}' at '{path}'."
                )

        for key, item_val in value.items():
            if not isinstance(key, str):
                raise ToolArgumentValidationError(f"Invalid non-string property key at '{path}'.")
            if key in properties:
                _validate_node(item_val, properties[key], path=f"{path}.{key}")
            elif additional_allowed is False:
                raise ToolArgumentValidationError(
                    f"Unexpected parameter '{key}' is not permitted at '{path}'."
                )
            elif isinstance(additional_allowed, dict):
                _validate_node(item_val, additional_allowed, path=f"{path}.{key}")

    elif expected_type == "string" and isinstance(value, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(
            value.strip() if schema.get("stripCheck", True) else value
        ) < int(min_len):
            raise ToolArgumentValidationError(
                f"String at '{path}' must be at least {min_len} non-whitespace character(s)."
            )
        if max_len is not None and len(value) > int(max_len):
            raise ToolArgumentValidationError(
                f"String at '{path}' exceeds maximum length {max_len}."
            )

    elif expected_type in ("integer", "number") and isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ToolArgumentValidationError(f"Numeric value at '{path}' must be finite.")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ToolArgumentValidationError(
                f"Value {value} at '{path}' is below minimum {minimum}."
            )
        if maximum is not None and value > maximum:
            raise ToolArgumentValidationError(
                f"Value {value} at '{path}' exceeds maximum {maximum}."
            )

    elif expected_type == "array" and isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, elem in enumerate(value):
                _validate_node(elem, item_schema, path=f"{path}[{idx}]")


def _check_type(value: Any, expected_type: str, *, path: str) -> None:
    if expected_type == "object":
        ok = isinstance(value, dict)
    elif expected_type == "string":
        ok = isinstance(value, str)
    elif expected_type == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif expected_type == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected_type == "boolean":
        ok = isinstance(value, bool)
    elif expected_type == "array":
        ok = isinstance(value, list)
    elif expected_type == "null":
        ok = value is None
    else:
        raise ToolArgumentValidationError(f"Unsupported schema type '{expected_type}' at '{path}'.")

    if not ok:
        raise ToolArgumentValidationError(
            f"Invalid type at '{path}': expected '{expected_type}', got '{type(value).__name__}'."
        )
