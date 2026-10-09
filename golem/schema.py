"""A small JSON Schema subset: enough to check tool interfaces without a dependency.

Supported keywords: type, properties, required, additionalProperties (bool or a
schema for map values), items, enum, minimum, maximum, minLength, maxLength,
minItems, maxItems, plus description, default, and title as annotations.
Anything else is rejected when a manifest is declared, so the interface a tool
promises is one Golem can actually check.
"""

from __future__ import annotations

ALLOWED_KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "description",
    "default",
    "title",
}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def check_schema(schema: object, where: str = "schema", depth: int = 0) -> list[str]:
    """Problems with a schema itself (unsupported keywords, bad types)."""
    if depth > 6:
        return [f"{where}: nested deeper than 6 levels"]
    if not isinstance(schema, dict):
        return [
            f"{where}: must be a JSON Schema object, got {type(schema).__name__} {str(schema)[:60]!r}"
        ]
    problems = [
        f"{where}: unsupported keyword {key!r}"
        for key in schema
        if key not in ALLOWED_KEYWORDS
    ]
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if kind is None or any(item not in TYPES for item in kinds):
        problems.append(f"{where}: type must be one of {sorted(TYPES)}")
    props = schema.get("properties")
    if props is None:
        props = {}
    elif not isinstance(props, dict):
        problems.append(f"{where}: properties must be an object")
        props = {}
    for name, sub in props.items():
        problems += check_schema(sub, f"{where}.{name}", depth + 1)
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
    ):
        problems.append(f"{where}: required must be a list of strings")
    if isinstance(schema.get("items"), dict):
        problems += check_schema(schema["items"], f"{where}[]", depth + 1)
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        problems += check_schema(extra, f"{where}.*", depth + 1)
    elif extra is not None and not isinstance(extra, bool):
        problems.append(
            f"{where}: additionalProperties must be true, false, or a schema"
        )
    return problems


def validate(value: object, schema: dict, where: str = "$") -> list[str]:
    """Ways a value breaks a schema. Empty means valid."""
    errors: list[str] = []
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    type_names = [item for item in kinds if isinstance(item, str)]
    if kind is not None and not any(_is_type(value, item) for item in type_names):
        return [f"{where}: expected {kind}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        props = schema.get("properties")
        if not isinstance(props, dict):
            props = {}
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    errors.append(f"{where}: missing required {name!r}")
        extra = schema.get("additionalProperties")
        for name, item in value.items():
            if name in props:
                errors += validate(item, props[name], f"{where}.{name}")
            elif extra is False:
                errors.append(f"{where}: unexpected property {name!r}")
            elif isinstance(extra, dict):
                errors += validate(item, extra, f"{where}.{name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{where}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                errors += validate(item, schema["items"], f"{where}[{index}]")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{where}: above {schema['maximum']}")
    return errors[:20]


def outline(schema: object, depth: int = 0) -> str:
    """A one-line signature of a schema, e.g. {jobs: [{job: string, most?: [string]}]}. It is
    what an installed tool returns, short enough to list beside every tool, so a session can see
    which tool's output fields fit another tool's arguments without calling either."""
    if not isinstance(schema, dict):
        return "any"
    kind = schema.get("type")
    kinds = [item for item in (kind if isinstance(kind, list) else [kind]) if isinstance(item, str)]
    parts = []
    for item in kinds or ["any"]:
        if item == "object" and depth < 5:
            props = schema.get("properties")
            props = props if isinstance(props, dict) else {}
            required = schema.get("required")
            required = {name for name in required if isinstance(name, str)} if isinstance(required, list) else set()
            fields = [
                f"{name}{'' if name in required else '?'}: {outline(sub, depth + 1)}"
                for name, sub in props.items()
            ]
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                fields.append(f"*: {outline(extra, depth + 1)}")
            parts.append("{" + ", ".join(fields) + "}" if fields else "object")
        elif item == "array" and depth < 5 and isinstance(schema.get("items"), dict):
            parts.append(f"[{outline(schema['items'], depth + 1)}]")
        else:
            parts.append(item)
    return " | ".join(parts)


def _is_type(value: object, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    return False
