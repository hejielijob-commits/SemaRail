"""Structured CRUD for Wren v5 cube metadata.

The semantic console deliberately treats the YAML file as the source of truth.
This module projects the fields that the visual cube workbench owns while
merging edits back into the original mapping.  Unknown root and nested keys
are therefore retained when a user saves a cube through the UI.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml

try:
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover - direct module loading
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


CUBES_PATH_PREFIX = "cubes/"
_CUBE_PATH = re.compile(r"^cubes/([^/]+)/metadata\.ya?ml$", re.IGNORECASE)
_VIEW_PATH = re.compile(r"^views/([^/]+)/metadata\.ya?ml$", re.IGNORECASE)
_MODEL_PATH = re.compile(r"^models/([^/]+)/metadata\.ya?ml$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")
_MAX_NAME = 255
_MAX_TEXT = 8_000
_FIELD_KEYS = ("name", "expression", "type")


def _text(value: Any, field: str, *, maximum: int = _MAX_TEXT, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ProjectError("INVALID_CUBE", f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise ProjectError("INVALID_CUBE", f"{field} is required")
    if len(result) > maximum:
        raise ProjectError("INVALID_CUBE", f"{field} exceeds the permitted length")
    return result


def _cube_name(value: Any) -> str:
    name = _text(value, "cube name", maximum=_MAX_NAME, required=True)
    if not _IDENTIFIER.fullmatch(name):
        raise ProjectError("INVALID_CUBE", "cube name is not a valid identifier")
    return name


def _yaml(content: str, path: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ProjectError("INVALID_YAML", f"{path} is not valid YAML") from exc
    if not isinstance(parsed, Mapping):
        raise ProjectError("INVALID_CUBE", f"{path} must contain an object")
    return dict(parsed)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_CUBE", f"{field} must be an object")
    return dict(value)


def _source_path(name: str) -> str:
    return f"cubes/{name}/metadata.yml"


def _read_cube(project: ProjectStore, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        result = project.read_file(path)
    except ProjectError:
        raise
    content = result.get("content")
    if not isinstance(content, str):
        raise ProjectError("INVALID_CUBE", f"{path} does not contain text")
    return _yaml(content, path), result


def _project_entry(value: Any, path: str, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_CUBE", f"{path} entry {index + 1} must be an object")
    name = _text(value.get("name"), f"{path} entry {index + 1} name", maximum=_MAX_NAME, required=True)
    expression = _text(value.get("expression"), f"{path} entry {index + 1} expression", required=True)
    type_name = _text(value.get("type"), f"{path} entry {index + 1} type", maximum=255, required=True)
    # Only expose the keys the workbench edits.  The original mapping is
    # retained independently by save_cube, so this projection cannot erase
    # Wren extensions that are not understood by this client.
    return {"name": name, "expression": expression, "type": type_name}


def _project_entries(value: Any, path: str, *, optional: bool = True) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProjectError("INVALID_CUBE", f"{path} must be a list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        entry = _project_entry(item, path, index)
        if entry["name"] in seen:
            raise ProjectError("INVALID_CUBE", f"{path} contains duplicate name '{entry['name']}'")
        seen.add(entry["name"])
        entries.append(entry)
    if not optional and not entries:
        raise ProjectError("INVALID_CUBE", f"{path} must contain at least one entry")
    return entries


def _project_hierarchies(value: Any, path: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_CUBE", f"{path} must be an object")
    result: dict[str, list[str]] = {}
    for raw_name, levels in value.items():
        name = _text(raw_name, f"{path} name", maximum=_MAX_NAME, required=True)
        if not isinstance(levels, list) or any(not isinstance(level, str) or not level.strip() for level in levels):
            raise ProjectError("INVALID_CUBE", f"{path}.{name} levels must be a list of strings")
        result[name] = [_text(level, f"{path}.{name} level", maximum=_MAX_NAME, required=True) for level in levels]
    return result


def _available_base_objects(project: ProjectStore) -> list[str]:
    names: set[str] = set()
    for item in project.files():
        path = str(item.get("path", ""))
        match = _MODEL_PATH.fullmatch(path) or _VIEW_PATH.fullmatch(path)
        if match:
            names.add(match.group(1))
    return sorted(names, key=str.lower)


def _cube_projection(raw: Mapping[str, Any], *, path: str, draft: bool) -> dict[str, Any]:
    name = _cube_name(raw.get("name"))
    base_object = _text(raw.get("base_object"), f"{path} base_object", maximum=_MAX_NAME, required=True)
    measures = _project_entries(raw.get("measures"), f"{path} measures", optional=True)
    dimensions = _project_entries(raw.get("dimensions"), f"{path} dimensions", optional=True)
    time_dimensions = _project_entries(raw.get("time_dimensions"), f"{path} time_dimensions", optional=True)
    hierarchies = _project_hierarchies(raw.get("hierarchies"), f"{path} hierarchies")
    properties = raw.get("properties")
    projected: dict[str, Any] = {
        "name": name,
        "sourcePath": path,
        "baseObject": base_object,
        "measures": measures,
        "dimensions": dimensions,
        "timeDimensions": time_dimensions,
        "hierarchies": hierarchies,
        "draft": bool(draft),
    }
    if "refresh_time" in raw:
        projected["refreshTime"] = _text(raw.get("refresh_time"), f"{path} refresh_time", maximum=255)
    if isinstance(properties, Mapping):
        # Only expose the scalar description over JSON.  Other properties can
        # contain arbitrary YAML values (dates, tagged objects, and future
        # Wren extensions), but remain intact in the source mapping during a
        # save.
        description = properties.get("description")
        if isinstance(description, str):
            projected["properties"] = {"description": description}
    return projected


def _validate_entries(value: Any, field: str, *, optional: bool = True) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if value is None:
        return errors
    if not isinstance(value, list):
        return [{"path": field, "message": f"{field} must be a list", "severity": "error"}]
    seen: set[str] = set()
    for index, item in enumerate(value):
        entry_path = f"{field}[{index + 1}]"
        if not isinstance(item, Mapping):
            errors.append({"path": entry_path, "message": "entry must be an object", "severity": "error"})
            continue
        name = item.get("name")
        expression = item.get("expression")
        type_name = item.get("type")
        if not isinstance(name, str) or not name.strip():
            errors.append({"path": f"{entry_path}.name", "message": "name is required", "severity": "error"})
        elif len(name.strip()) > _MAX_NAME:
            errors.append({"path": f"{entry_path}.name", "message": "name is too long", "severity": "error"})
        elif name.strip() in seen:
            errors.append({"path": f"{entry_path}.name", "message": f"duplicate name '{name.strip()}'", "severity": "error"})
        else:
            seen.add(name.strip())
        if not isinstance(expression, str) or not expression.strip():
            errors.append({"path": f"{entry_path}.expression", "message": "expression is required", "severity": "error"})
        elif len(expression) > _MAX_TEXT:
            errors.append({"path": f"{entry_path}.expression", "message": "expression is too long", "severity": "error"})
        if not isinstance(type_name, str) or not type_name.strip():
            errors.append({"path": f"{entry_path}.type", "message": "type is required", "severity": "error"})
    if not optional and isinstance(value, list) and not value:
        errors.append({"path": field, "message": "at least one entry is required", "severity": "error"})
    return errors


def validate_cube_payload(
    payload: Mapping[str, Any],
    *,
    cube_name: str | None = None,
    available_base_objects: list[str] | None = None,
) -> dict[str, Any]:
    """Validate a projected cube without writing it to disk.

    The result intentionally mirrors Wren's validation shape and is safe to
    render directly in the web workbench.
    """

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    name_value = payload.get("name", cube_name)
    if not isinstance(name_value, str) or not name_value.strip() or not _IDENTIFIER.fullmatch(name_value.strip()):
        errors.append({"path": "name", "message": "cube name must be a valid identifier", "severity": "error"})
    elif cube_name is not None and name_value.strip() != cube_name:
        errors.append({"path": "name", "message": "cube name cannot be changed from its source path", "severity": "error"})
    base = payload.get("baseObject", payload.get("base_object"))
    if not isinstance(base, str) or not base.strip():
        errors.append({"path": "base_object", "message": "base_object is required", "severity": "error"})
    elif available_base_objects is not None and base.strip() not in set(available_base_objects):
        errors.append({"path": "base_object", "message": f"base_object '{base.strip()}' is not a defined model or view", "severity": "error"})
    errors.extend(_validate_entries(payload.get("measures"), "measures", optional=True))
    errors.extend(_validate_entries(payload.get("dimensions"), "dimensions", optional=True))
    errors.extend(_validate_entries(payload.get("timeDimensions", payload.get("time_dimensions")), "time_dimensions", optional=True))
    measures_value = payload.get("measures") or []
    dimensions_value = payload.get("dimensions") or []
    time_dimensions_value = payload.get("timeDimensions", payload.get("time_dimensions")) or []
    measure_names = {str(item.get("name")).strip() for item in measures_value if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    dimension_names = {str(item.get("name")).strip() for item in dimensions_value if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    time_names = {str(item.get("name")).strip() for item in time_dimensions_value if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    if not measure_names:
        warnings.append({"path": "measures", "message": "cube has no measures", "severity": "warning"})
    all_dimension_names = dimension_names | time_names
    hierarchies = payload.get("hierarchies", {})
    if hierarchies is not None and not isinstance(hierarchies, Mapping):
        errors.append({"path": "hierarchies", "message": "hierarchies must be an object", "severity": "error"})
    elif isinstance(hierarchies, Mapping):
        for hierarchy_name, levels in hierarchies.items():
            path = f"hierarchies.{hierarchy_name}"
            if not isinstance(levels, list) or any(not isinstance(level, str) or not level.strip() for level in levels):
                errors.append({"path": path, "message": "levels must be a list of strings", "severity": "error"})
                continue
            for level in levels:
                if level.strip() not in all_dimension_names:
                    errors.append({"path": path, "message": f"references unknown dimension '{level.strip()}'", "severity": "error"})
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "errorCount": len(errors),
        "warningCount": len(warnings),
    }


def _incoming_entries(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProjectError("INVALID_CUBE", f"{field} must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ProjectError("INVALID_CUBE", f"{field} entry {index + 1} must be an object")
        name = _text(item.get("name"), f"{field} entry {index + 1} name", maximum=_MAX_NAME, required=True)
        if name in seen:
            raise ProjectError("INVALID_CUBE", f"{field} contains duplicate name '{name}'")
        seen.add(name)
        expression = _text(item.get("expression"), f"{field} entry {index + 1} expression", required=True)
        type_name = _text(item.get("type"), f"{field} entry {index + 1} type", maximum=255, required=True)
        result.append({"name": name, "expression": expression, "type": type_name})
    return result


def _merge_entries(existing: Any, incoming: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    current = existing if isinstance(existing, list) else []
    by_name = {
        str(item.get("name")): dict(item)
        for item in current
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    merged: list[dict[str, Any]] = []
    for item in incoming:
        previous = by_name.get(item["name"])
        if previous is None:
            previous = {}
        # Start from the old mapping so extension keys nested in a measure,
        # dimension, or time dimension survive a normal visual save.
        next_item = dict(previous)
        next_item.update(item)
        merged.append(next_item)
    return merged


def _incoming_hierarchies(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_CUBE", "hierarchies must be an object")
    result: dict[str, list[str]] = {}
    for raw_name, raw_levels in value.items():
        name = _text(raw_name, "hierarchy name", maximum=_MAX_NAME, required=True)
        if not isinstance(raw_levels, list):
            raise ProjectError("INVALID_CUBE", f"hierarchies.{name} levels must be a list")
        result[name] = [_text(level, f"hierarchies.{name} level", maximum=_MAX_NAME, required=True) for level in raw_levels]
    return result


class CubeStore:
    """Read and update Wren v5 ``cubes/<name>/metadata.yml`` files."""

    def __init__(self, project: ProjectStore) -> None:
        self.project = project

    def snapshot(self) -> dict[str, Any]:
        cubes: list[dict[str, Any]] = []
        source_files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in self.project.files():
            path = str(item.get("path", ""))
            if not _CUBE_PATH.fullmatch(path):
                continue
            raw, file_info = _read_cube(self.project, path)
            cube = _cube_projection(raw, path=path, draft=bool(file_info.get("draft")))
            if cube["name"] in seen:
                raise ProjectError("INVALID_CUBE", f"duplicate cube name '{cube['name']}'")
            seen.add(cube["name"])
            cubes.append(cube)
            source_files.append(dict(file_info))
        overview = self.project.overview()
        cubes.sort(key=lambda item: str(item["name"]).lower())
        source_files.sort(key=lambda item: str(item.get("path", "")).lower())
        return {
            "revision": overview["revision"],
            "draftCount": overview["draftCount"],
            "cubes": cubes,
            "sourceFiles": source_files,
            "availableBaseObjects": _available_base_objects(self.project),
        }

    def validate(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        cube_name = _cube_name(name)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_CUBE", "cube payload must be an object")
        return validate_cube_payload(payload, cube_name=cube_name, available_base_objects=_available_base_objects(self.project))

    def get_cube(self, name: str) -> dict[str, Any]:
        cube_name = _cube_name(name)
        snapshot = self.snapshot()
        cube = next((item for item in snapshot["cubes"] if item.get("name") == cube_name), None)
        if cube is None:
            raise ProjectError("CUBE_NOT_FOUND", "cube was not found")
        return cube

    def create_cube(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_CUBE", "cube payload must be an object")
        cube_name = _cube_name(payload.get("name"))
        if any(item.get("name") == cube_name for item in self.snapshot()["cubes"]):
            raise ProjectError("FILE_EXISTS", "cube already exists", {"path": _source_path(cube_name)})
        return self.save_cube(cube_name, payload)

    def save_cube(self, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        cube_name = _cube_name(name)
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_CUBE", "cube payload must be an object")
        snapshot = self.snapshot()
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        if expected is not None and expected != snapshot["revision"]:
            raise ProjectError("REVISION_CONFLICT", "project changed since cubes were read", {"revision": snapshot["revision"]})
        path = self._path_for_name(cube_name, snapshot)
        try:
            raw, _ = _read_cube(self.project, path)
            exists = True
        except ProjectError as exc:
            if exc.code != "FILE_NOT_FOUND":
                raise
            raw = {}
            exists = False
        if not exists and "name" not in payload:
            payload = {**payload, "name": cube_name}
        validation = validate_cube_payload(payload, cube_name=cube_name, available_base_objects=_available_base_objects(self.project))
        if validation["errors"]:
            raise ProjectError("INVALID_CUBE", "cube contains invalid fields", {"errors": validation["errors"], "warnings": validation["warnings"]})
        raw["name"] = cube_name
        base = payload.get("baseObject", payload.get("base_object"))
        if base is not None:
            raw["base_object"] = _text(base, "baseObject", maximum=_MAX_NAME, required=True)
        for public_key, raw_key, required in (
            ("measures", "measures", True),
            ("dimensions", "dimensions", False),
            ("timeDimensions", "time_dimensions", False),
        ):
            if public_key in payload or raw_key in payload:
                incoming = _incoming_entries(payload.get(public_key, payload.get(raw_key)), public_key)
                if required and not incoming:
                    # Wren currently reports no measures as a warning, so an
                    # empty collection remains a valid draft rather than a
                    # destructive hard stop in the visual editor.
                    raw[raw_key] = []
                elif not incoming:
                    # Optional Wren collections are cleaner when absent than
                    # when persisted as an empty YAML array.  The projection
                    # still exposes an empty list to keep the UI shape stable.
                    raw.pop(raw_key, None)
                else:
                    raw[raw_key] = _merge_entries(raw.get(raw_key), incoming, raw_key)
        if "hierarchies" in payload:
            hierarchies = _incoming_hierarchies(payload.get("hierarchies"))
            if hierarchies:
                raw["hierarchies"] = hierarchies
            else:
                raw.pop("hierarchies", None)
        if "refreshTime" in payload or "refresh_time" in payload:
            refresh = payload.get("refreshTime", payload.get("refresh_time"))
            refresh_text = _text(refresh, "refreshTime", maximum=255)
            if refresh_text:
                raw["refresh_time"] = refresh_text
            else:
                raw.pop("refresh_time", None)
        if "properties" in payload:
            properties = payload.get("properties")
            if properties is None:
                raw.pop("properties", None)
            elif isinstance(properties, Mapping):
                current_properties = _mapping(raw.get("properties"), "properties")
                current_properties.update(dict(properties))
                raw["properties"] = current_properties
            else:
                raise ProjectError("INVALID_CUBE", "properties must be an object")
        content = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        self.project.put_file(path, content, expected_revision=expected)
        return self.snapshot()

    def delete_cube(self, name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        cube_name = _cube_name(name)
        payload = payload if isinstance(payload, Mapping) else {}
        snapshot = self.snapshot()
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        if expected is not None and expected != snapshot["revision"]:
            raise ProjectError("REVISION_CONFLICT", "project changed since cubes were read", {"revision": snapshot["revision"]})
        path = self._path_for_name(cube_name, snapshot)
        if not any(str(item.get("path")) == path for item in self.project.files()):
            raise ProjectError("CUBE_NOT_FOUND", "cube was not found")
        self.project.put_file(path, None, delete=True, expected_revision=expected)
        return self.snapshot()

    @staticmethod
    def _path_for_name(name: str, snapshot: Mapping[str, Any]) -> str:
        """Keep a legacy ``metadata.yaml`` spelling when it already exists."""

        cubes = snapshot.get("cubes")
        if isinstance(cubes, list):
            for cube in cubes:
                if isinstance(cube, Mapping) and cube.get("name") == name and isinstance(cube.get("sourcePath"), str):
                    return str(cube["sourcePath"])
        return _source_path(name)


__all__ = ["CUBES_PATH_PREFIX", "CubeStore", "validate_cube_payload"]
