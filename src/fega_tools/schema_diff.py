"""Conservative JSON Schema compatibility analysis for FEGA releases.

The analyser intentionally reports ``unknown`` for constructs where a textual
comparison cannot prove accepted-input compatibility.  Release tooling must not
turn an ambiguous result into an automatic SemVer decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterable

from .github_uri import transform_raw_github_uris


class Severity(IntEnum):
    SAME = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3
    UNKNOWN = 4

    @classmethod
    def parse(cls, value: str) -> "Severity":
        try:
            return cls[value.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported change severity: {value}") from exc

    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Change:
    path: str
    severity: Severity
    message: str
    old: Any = None
    new: Any = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "path": self.path or "/",
            "severity": self.severity.label(),
            "message": self.message,
        }
        if self.old is not None:
            result["old"] = self.old
        if self.new is not None:
            result["new"] = self.new
        return result


@dataclass
class SchemaDiff:
    changes: list[Change] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        return max((change.severity for change in self.changes), default=Severity.SAME)

    def add(
        self,
        path: str,
        severity: Severity,
        message: str,
        old: Any = None,
        new: Any = None,
    ) -> None:
        self.changes.append(Change(path, severity, message, old, new))

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.label(),
            "changes": [change.as_dict() for change in self.changes],
        }


ANNOTATION_KEYS = {
    "$comment",
    "default",
    "deprecated",
    "description",
    "examples",
    "meta:enum",
    "meta:version",
    "readOnly",
    "title",
    "writeOnly",
}
SET_ARRAY_KEYS = {"enum", "required", "type"}
COMPOSITION_KEYS = {
    "allOf",
    "anyOf",
    "dependentSchemas",
    "else",
    "if",
    "not",
    "oneOf",
    "then",
}
LOWER_BOUND_KEYS = {
    "minContains",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "exclusiveMinimum",
}
UPPER_BOUND_KEYS = {
    "maxContains",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "exclusiveMaximum",
}


def _path(parent: str, key: str | int) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}" if parent else f"/{escaped}"


def _normalise_uri(value: str) -> str:
    normalised, _ = transform_raw_github_uris(
        value,
        lambda uri: uri.render(ref="{release-ref}", preserve_prefix=False),
    )
    return normalised


def normalise_release_metadata(value: Any, *, key: str | None = None) -> Any:
    """Remove mechanical release metadata before semantic comparison."""
    if key == "meta:version":
        return "{component-version}"
    if isinstance(value, dict):
        return {
            child_key: normalise_release_metadata(child, key=child_key)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [normalise_release_metadata(child) for child in value]
    if isinstance(value, str):
        return _normalise_uri(value)
    return value


def _as_set(value: Any) -> set[Any] | None:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(
        isinstance(item, (str, int, float, bool, type(None))) for item in value
    ):
        return set(value)
    return None


def _compare_set_keyword(
    key: str, old: Any, new: Any, path: str, result: SchemaDiff
) -> None:
    old_set = _as_set(old)
    new_set = _as_set(new)
    if old_set is None or new_set is None:
        result.add(path, Severity.UNKNOWN, f"cannot compare {key} values safely", old, new)
        return
    removed = sorted(old_set - new_set, key=str)
    added = sorted(new_set - old_set, key=str)
    if key == "required":
        if added:
            result.add(path, Severity.MAJOR, "required properties added", old=removed, new=added)
        if removed:
            result.add(path, Severity.MINOR, "required properties removed", old=removed)
        return
    if removed:
        result.add(path, Severity.MAJOR, f"accepted {key} values removed", old=removed)
    if added:
        result.add(path, Severity.MINOR, f"accepted {key} values added", new=added)


def _compare_bound(
    key: str, old: Any, new: Any, path: str, result: SchemaDiff
) -> None:
    if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
        result.add(path, Severity.UNKNOWN, f"cannot compare {key} safely", old, new)
        return
    if old == new:
        return
    tighter = new > old if key in LOWER_BOUND_KEYS else new < old
    result.add(
        path,
        Severity.MAJOR if tighter else Severity.MINOR,
        f"{key} constraint {'tightened' if tighter else 'relaxed'}",
        old,
        new,
    )


def _compare_properties(
    old: dict[str, Any], new: dict[str, Any], path: str, result: SchemaDiff
) -> None:
    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    old_properties = old.get("properties", {})
    new_properties = new.get("properties", {})

    def old_object_closure() -> str:
        """Return ``closed``, ``open`` or ``unknown`` for this object.

        ``additionalProperties`` applies directly to the object and therefore
        gives an unambiguous answer when it is a boolean.  In contrast,
        ``unevaluatedProperties`` depends on annotations produced by sibling
        schemas.  We only treat the simple, non-composed form as provably
        closed; composed or schema-valued forms remain conservative.
        """
        if isinstance(old.get("additionalProperties", True), dict):
            return "unknown"
        if old.get("additionalProperties", True) is False:
            return "closed"
        if old.get("additionalProperties", True) is not True:
            return "unknown"
        if old.get("unevaluatedProperties") is False:
            if any(key in old for key in COMPOSITION_KEYS):
                return "unknown"
            return "closed"
        if isinstance(old.get("unevaluatedProperties"), dict):
            return "unknown"
        return "open"

    def is_true_schema(schema: Any) -> bool:
        return schema is True

    for name in sorted(set(old_properties) | set(new_properties)):
        child_path = _path(_path(path, "properties"), name)
        if name not in old_properties:
            required = name in new_required
            if required:
                severity = Severity.MAJOR
            elif is_true_schema(new_properties[name]):
                # Declaring an unconstrained property on an open object does
                # not change accepted inputs.  On a closed object it is an
                # ordinary additive expansion.
                severity = Severity.MINOR if old_object_closure() == "closed" else Severity.SAME
            elif old_object_closure() == "closed":
                severity = Severity.MINOR
            else:
                # A previously arbitrary value under this name may now fail
                # the newly introduced constraint.  Without evaluating every
                # possible instance, remain conservative.
                severity = Severity.UNKNOWN
            if severity == Severity.SAME:
                continue
            result.add(
                child_path,
                severity,
                "property added" + (" as required" if required else " as optional"),
                new=new_properties[name],
            )
        elif name not in new_properties:
            closure = old_object_closure()
            severity = {
                "closed": Severity.MAJOR,
                "open": Severity.MINOR,
                "unknown": Severity.UNKNOWN,
            }[closure]
            result.add(child_path, severity, "declared property removed", old=old_properties[name])
        else:
            _compare(old_properties[name], new_properties[name], child_path, result)


def _compare(old: Any, new: Any, path: str, result: SchemaDiff) -> None:
    if old == new:
        return
    if isinstance(old, bool) and isinstance(new, bool):
        result.add(
            path,
            Severity.MINOR if not old and new else Severity.MAJOR,
            "boolean schema relaxed" if not old and new else "boolean schema tightened",
            old,
            new,
        )
        return
    if not isinstance(old, dict) or not isinstance(new, dict):
        result.add(path, Severity.UNKNOWN, "schema value changed", old, new)
        return

    _compare_properties(old, new, path, result)
    keys = (set(old) | set(new)) - {"properties"}
    for key in sorted(keys):
        child_path = _path(path, key)
        if key not in old:
            if key in ANNOTATION_KEYS:
                result.add(child_path, Severity.PATCH, "annotation added", new=new[key])
            elif key == "required":
                _compare_set_keyword(key, [], new[key], child_path, result)
            elif key in {"additionalProperties", "unevaluatedProperties", "additionalItems", "unevaluatedItems"}:
                severity = Severity.MAJOR if new[key] is False else Severity.UNKNOWN
                result.add(child_path, severity, f"{key} constraint added", new=new[key])
            else:
                result.add(child_path, Severity.UNKNOWN, f"validation keyword '{key}' added", new=new[key])
            continue
        if key not in new:
            if key in ANNOTATION_KEYS:
                result.add(child_path, Severity.PATCH, "annotation removed", old=old[key])
            elif key == "required":
                _compare_set_keyword(key, old[key], [], child_path, result)
            else:
                result.add(child_path, Severity.MINOR, f"constraint or keyword '{key}' removed", old=old[key])
            continue

        old_value = old[key]
        new_value = new[key]
        if old_value == new_value:
            continue
        if key in ANNOTATION_KEYS:
            result.add(child_path, Severity.PATCH, "annotation changed", old_value, new_value)
        elif key in SET_ARRAY_KEYS:
            _compare_set_keyword(key, old_value, new_value, child_path, result)
        elif key in LOWER_BOUND_KEYS or key in UPPER_BOUND_KEYS:
            _compare_bound(key, old_value, new_value, child_path, result)
        elif key in {"additionalProperties", "unevaluatedProperties", "additionalItems", "unevaluatedItems"}:
            if old_value is False and new_value is True:
                result.add(child_path, Severity.MINOR, f"{key} relaxed", old_value, new_value)
            elif old_value is True and new_value is False:
                result.add(child_path, Severity.MAJOR, f"{key} tightened", old_value, new_value)
            else:
                result.add(child_path, Severity.UNKNOWN, f"{key} schema changed", old_value, new_value)
        elif key == "const":
            result.add(child_path, Severity.MAJOR, "const value changed", old_value, new_value)
        elif key in COMPOSITION_KEYS or key in {"pattern", "format", "$ref", "$dynamicRef"}:
            result.add(child_path, Severity.UNKNOWN, f"{key} changed", old_value, new_value)
        elif isinstance(old_value, dict) and isinstance(new_value, dict):
            _compare(old_value, new_value, child_path, result)
        else:
            result.add(child_path, Severity.UNKNOWN, f"keyword '{key}' changed", old_value, new_value)


def compare_schemas(old: Any, new: Any) -> SchemaDiff:
    result = SchemaDiff()
    _compare(
        normalise_release_metadata(old),
        normalise_release_metadata(new),
        "",
        result,
    )
    return result


def validate_meta_enums(document: Any) -> list[str]:
    """Return JSON-pointer-like errors for inconsistent ``meta:enum`` maps."""
    errors: list[str] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "meta:enum" in value:
                mapping = value["meta:enum"]
                enum = value.get("enum")
                if not isinstance(enum, list):
                    errors.append(f"{path or '/'}: meta:enum requires a sibling enum array")
                elif not isinstance(mapping, dict):
                    errors.append(f"{path or '/'}: meta:enum must be an object")
                else:
                    enum_keys = {str(item) for item in enum}
                    mapping_keys = set(mapping)
                    if enum_keys != mapping_keys:
                        missing = sorted(enum_keys - mapping_keys)
                        extra = sorted(mapping_keys - enum_keys)
                        errors.append(
                            f"{path or '/'}: meta:enum keys differ from enum; "
                            f"missing={missing}, extra={extra}"
                        )
            for key, child in value.items():
                visit(child, _path(path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, _path(path, index))

    visit(document, "")
    return errors


def overall_severity(diffs: Iterable[SchemaDiff]) -> Severity:
    return max((diff.severity for diff in diffs), default=Severity.SAME)
