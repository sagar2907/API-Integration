"""JSON Schema path resolution and type compatibility.

This is the component that answers "does the data coming out of step A actually
fit what step B requires?" — the question that separates a validated workflow
from a hopeful one.

**Scope is deliberately limited, and the limits are the design.** Handled:
objects, arrays, the primitive types, nested dot paths, required-ness, and a
small set of safe coercions. Not handled: `oneOf` / `anyOf` / `allOf`,
cross-document `$ref`, `format` (date-time, email, uri), numeric ranges,
patterns, and enum membership.

Where a schema cannot be understood, the result is `UNKNOWN` and the compiler
lets it pass. That direction is chosen on purpose: this is a gate against
provably wrong plans, not a proof of correctness. Rejecting everything we cannot
model would make the system useless against real specs, most of which use at
least one unsupported construct somewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

# Trailing [] or [0] in a path means "step into the array's elements".
_ARRAY_SUFFIX = re.compile(r"\[\d*\]$")

JSON_TYPES = ("string", "number", "integer", "boolean", "object", "array", "null")

# Conversions applied silently at execution time because they cannot lose
# information or fail. Anything absent here is a genuine incompatibility:
# string -> number is missing precisely because "abc" has no numeric value.
SAFE_COERCIONS: dict[str, set[str]] = {
    "integer": {"number", "string"},
    "number": {"string"},
    "boolean": {"string"},
}


class Compatibility(Enum):
    OK = "ok"
    COERCIBLE = "coercible"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True)
class MatchResult:
    compatibility: Compatibility
    source_type: str | None
    target_type: str | None
    detail: str = ""

    @property
    def acceptable(self) -> bool:
        return self.compatibility is not Compatibility.INCOMPATIBLE


def schema_type(schema: dict[str, Any] | None) -> str | None:
    """The declared type of a schema, if it declares one unambiguously."""
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if isinstance(declared, str) and declared in JSON_TYPES:
        return declared
    if isinstance(declared, list):
        # ["string", "null"] is the common nullable idiom; the non-null entry
        # is the type that matters for compatibility.
        concrete = [t for t in declared if t in JSON_TYPES and t != "null"]
        if len(concrete) == 1:
            return concrete[0]
        return None
    # A schema with properties but no declared type is an object in practice.
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def resolve_path(schema: dict[str, Any] | None, path: str) -> dict[str, Any] | None:
    """Walk a dot path into a JSON Schema and return the sub-schema.

    `user.login` steps through `properties.user.properties.login`.
    `labels[].name` steps into the array's item schema first.

    Returns None when the path does not exist — which is a real finding, not an
    error: it means the plan referenced a field the provider never returns.
    """
    if not isinstance(schema, dict):
        return None
    if not path:
        return schema

    current: dict[str, Any] | None = schema
    for raw_segment in path.split("."):
        if current is None:
            return None
        segment = raw_segment
        wants_element = bool(_ARRAY_SUFFIX.search(segment))
        if wants_element:
            segment = _ARRAY_SUFFIX.sub("", segment)

        if segment:
            # Step into an array automatically when the schema is a list but the
            # path did not say so; specs are inconsistent about this.
            if schema_type(current) == "array" and "properties" not in current:
                items = current.get("items")
                current = items if isinstance(items, dict) else None
                if current is None:
                    return None
            properties = current.get("properties")
            if not isinstance(properties, dict) or segment not in properties:
                return None
            nested = properties[segment]
            current = nested if isinstance(nested, dict) else None

        if wants_element and current is not None:
            items = current.get("items")
            current = items if isinstance(items, dict) else None

    return current


def required_fields(schema: dict[str, Any] | None) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    return [f for f in required if isinstance(f, str)] if isinstance(required, list) else []


def has_unsupported_constructs(schema: dict[str, Any] | None) -> bool:
    """True when a schema uses a construct this module cannot reason about."""
    if not isinstance(schema, dict):
        return False
    return any(key in schema for key in ("oneOf", "anyOf", "allOf", "not"))


def compare(
    source: dict[str, Any] | None,
    target: dict[str, Any] | None,
) -> MatchResult:
    """Can a value shaped like `source` be used where `target` is expected?"""
    source_type = schema_type(source)
    target_type = schema_type(target)

    if has_unsupported_constructs(source) or has_unsupported_constructs(target):
        return MatchResult(
            Compatibility.UNKNOWN,
            source_type,
            target_type,
            "schema uses oneOf/anyOf/allOf, which is outside the checker's scope",
        )

    if source_type is None or target_type is None:
        return MatchResult(
            Compatibility.UNKNOWN,
            source_type,
            target_type,
            "one side declares no type",
        )

    if source_type == target_type:
        if source_type == "object":
            return _compare_objects(source, target, source_type, target_type)
        if source_type == "array":
            return _compare_arrays(source, target)
        return MatchResult(Compatibility.OK, source_type, target_type)

    if target_type in SAFE_COERCIONS.get(source_type, set()):
        return MatchResult(
            Compatibility.COERCIBLE,
            source_type,
            target_type,
            f"{source_type} converts to {target_type} without loss",
        )

    return MatchResult(
        Compatibility.INCOMPATIBLE,
        source_type,
        target_type,
        f"cannot use a {source_type} where a {target_type} is required",
    )


def _compare_objects(
    source: dict[str, Any],
    target: dict[str, Any],
    source_type: str,
    target_type: str,
) -> MatchResult:
    """An object fits when it supplies every field the target requires."""
    target_required = required_fields(target)
    if not target_required:
        return MatchResult(Compatibility.OK, source_type, target_type)

    source_properties = source.get("properties")
    if not isinstance(source_properties, dict):
        return MatchResult(
            Compatibility.UNKNOWN,
            source_type,
            target_type,
            "source object declares no properties",
        )

    missing = [field for field in target_required if field not in source_properties]
    if missing:
        return MatchResult(
            Compatibility.INCOMPATIBLE,
            source_type,
            target_type,
            f"source object is missing required field(s): {', '.join(sorted(missing))}",
        )
    return MatchResult(Compatibility.OK, source_type, target_type)


def _compare_arrays(source: dict[str, Any], target: dict[str, Any]) -> MatchResult:
    source_items = source.get("items")
    target_items = target.get("items")
    if not isinstance(source_items, dict) or not isinstance(target_items, dict):
        return MatchResult(Compatibility.UNKNOWN, "array", "array", "element schema unknown")
    element = compare(source_items, target_items)
    if element.compatibility is Compatibility.INCOMPATIBLE:
        return MatchResult(
            Compatibility.INCOMPATIBLE,
            "array",
            "array",
            f"array elements incompatible: {element.detail}",
        )
    return MatchResult(element.compatibility, "array", "array", element.detail)


def parameter_schema(param_type: str | None) -> dict[str, Any]:
    """Turn a stored parameter type into a minimal schema for comparison."""
    if param_type in JSON_TYPES:
        return {"type": param_type}
    return {}
