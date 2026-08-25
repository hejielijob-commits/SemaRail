"""Structured business-model projection over Wren's file-based project format.

The visual editor uses a small, stable projection of Wren metadata.  The
projection deliberately keeps the Wren files as the source of truth: writes
merge only fields owned by the editor and retain unknown Wren keys so newer
Wren releases (or hand-authored project extensions) are not silently damaged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml

try:
    from .project import ProjectError, ProjectStore
except ImportError:  # pragma: no cover - direct smoke loading
    from project import ProjectError, ProjectStore  # type: ignore[no-redef]


LOCALES_PATH = "semantic-console/locales.yml"
RELATIONSHIPS_PATH = "relationships.yml"
_MODEL_PATH = re.compile(r"^models/([^/]+)/metadata\.ya?ml$", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")
_QUALIFIED_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9_$-])([A-Za-z_][A-Za-z0-9_$-]*)\.([A-Za-z_][A-Za-z0-9_$-]*)"
)
_LOCALES = ("zh-CN", "en-US")
_JOIN_TYPES = {"ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, *, maximum: int = 8_000, code: str = "INVALID_MODEL") -> str:
    """Return a bounded string, rejecting rather than silently truncating it."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProjectError(code, "text values must be strings")
    result = value.strip()
    if len(result) > maximum:
        raise ProjectError(code, "text value exceeds the permitted length")
    return result


def _boolean(value: Any, *, default: bool, code: str = "INVALID_MODEL") -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProjectError(code, "boolean values must be true or false")
    return value


def _localized(value: Any, fallback: str = "", *, code: str = "INVALID_MODEL") -> dict[str, str]:
    """Normalize the two supported locale values.

    ``None`` means that the caller did not provide a localized value and uses
    the supplied Wren description/name fallback for English.  A scalar is
    rejected instead of being silently interpreted as an empty translation.
    Unknown locale keys are intentionally ignored by the projection but are
    preserved in the source locale document by the merge logic below.
    """

    if value is None:
        source: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        source = value
    else:
        raise ProjectError(code, "localized values must be an object")
    result: dict[str, str] = {}
    for locale in _LOCALES:
        raw = source.get(locale, fallback if locale == "en-US" else "")
        result[locale] = _text(raw, code=code)
    return result


def _yaml(content: str, path: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(content) or {}
    except yaml.YAMLError as exc:
        raise ProjectError("INVALID_YAML", f"{path} is not valid YAML") from exc
    if not isinstance(parsed, Mapping):
        raise ProjectError("INVALID_MODEL", f"{path} must contain an object")
    return dict(parsed)


def _primary_key(value: Any) -> str | list[str]:
    """Keep Wren's scalar and composite primary-key wire forms intact."""

    if value is None:
        return ""
    if isinstance(value, str):
        return _text(value, maximum=255)
    if isinstance(value, list):
        if not value or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ProjectError("INVALID_MODEL", "primaryKey must be a non-empty string or list of strings")
        return [_text(item, maximum=255) for item in value]
    raise ProjectError("INVALID_MODEL", "primaryKey must be a string or list of strings")


def _description(properties: Mapping[str, Any]) -> str:
    value = properties.get("description", "")
    if value is None:
        return ""
    return _text(value, code="INVALID_MODEL")


def _properties(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectError("INVALID_MODEL", f"{path} properties must be an object")
    return dict(value)


def _set_description(target: dict[str, Any], value: dict[str, str]) -> None:
    """Update only Wren's default description while retaining other props."""

    default = value["en-US"] or value["zh-CN"]
    if default:
        target["description"] = default
    else:
        target.pop("description", None)


def _locale_document(document: dict[str, Any]) -> dict[str, Any]:
    """Validate the known locale containers without rejecting extensions."""

    for key in ("models", "relationships"):
        value = document.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            raise ProjectError("INVALID_LOCALES", f"locales.yml {key} must be an object")
        for name, record in value.items():
            if not isinstance(name, str) or not isinstance(record, Mapping):
                raise ProjectError("INVALID_LOCALES", f"locales.yml {key} entries must be objects")
            if key == "models" and "columns" in record:
                columns = record.get("columns")
                if not isinstance(columns, Mapping):
                    raise ProjectError("INVALID_LOCALES", "locales.yml model columns must be an object")
                for column_name, column_record in columns.items():
                    if not isinstance(column_name, str) or not isinstance(column_record, Mapping):
                        raise ProjectError("INVALID_LOCALES", "locales.yml column entries must be objects")
    return document


def _model_columns(raw: Mapping[str, Any], path: str) -> list[dict[str, Any]]:
    value = raw.get("columns", [])
    if not isinstance(value, list):
        raise ProjectError("INVALID_MODEL", f"{path} columns must be a list")
    columns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ProjectError("INVALID_MODEL", f"{path} column entries must be objects")
        column = dict(item)
        name = column.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ProjectError("INVALID_MODEL", f"{path} column name is required")
        name = name.strip()
        if name in seen:
            raise ProjectError("INVALID_MODEL", f"{path} contains duplicate column '{name}'")
        _properties(column.get("properties"), f"{path} column '{name}'")
        seen.add(name)
        columns.append(column)
    return columns


class SemanticModelStore:
    """Read and update business models without changing Wren's file layout."""

    def __init__(self, project: ProjectStore) -> None:
        self.project = project

    def snapshot(self) -> dict[str, Any]:
        files = self.project.files()
        locales = _locale_document(self._read_optional_yaml(LOCALES_PATH))
        locale_models = _mapping(locales.get("models"))
        locale_relationships = _mapping(locales.get("relationships"))
        models: list[dict[str, Any]] = []
        model_names: set[str] = set()

        for item in files:
            path = str(item.get("path", ""))
            match = _MODEL_PATH.fullmatch(path)
            if not match:
                continue
            raw = _yaml(self.project.read_file(path)["content"], path)
            raw_name = raw.get("name")
            if raw_name is None:
                model_name = match.group(1)
            elif isinstance(raw_name, str) and raw_name.strip():
                model_name = raw_name.strip()
            else:
                raise ProjectError("INVALID_MODEL", f"{path} model name must be a string")
            if model_name in model_names:
                raise ProjectError("INVALID_MODEL", f"duplicate model name '{model_name}'")
            model_names.add(model_name)

            model_locale = _mapping(locale_models.get(model_name))
            model_properties = _properties(raw.get("properties"), path)
            raw_columns = _model_columns(raw, path)
            locale_columns = _mapping(model_locale.get("columns"))
            columns: list[dict[str, Any]] = []
            for column in raw_columns:
                column_name = str(column["name"])
                column_properties = _properties(column.get("properties"), f"{path} column '{column_name}'")
                column_locale = _mapping(locale_columns.get(column_name))
                semantic_role = column_locale.get("semantic_role")
                role = self._infer_role(column) if semantic_role is None else _text(semantic_role, maximum=64)
                column_type = column.get("type", "UNKNOWN")
                if column_type is None:
                    column_type = "UNKNOWN"
                if not isinstance(column_type, str):
                    raise ProjectError("INVALID_MODEL", f"{path} column type must be a string")
                calculated = bool(column.get("is_calculated")) or "expression" in column
                expression = column.get("expression", "")
                if expression is None:
                    expression = ""
                if not isinstance(expression, str):
                    raise ProjectError("INVALID_MODEL", f"{path} column expression must be a string")
                columns.append({
                    "name": column_name,
                    "type": column_type,
                    "primaryKey": bool(column.get("is_primary_key")),
                    "notNull": bool(column.get("not_null")),
                    "relationship": column.get("relationship"),
                    "calculated": calculated,
                    "expression": expression,
                    "displayName": _localized(column_locale.get("display_name"), column_name),
                    "description": _localized(column_locale.get("description"), _description(column_properties)),
                    "semanticRole": role,
                    "format": _text(column_locale.get("format", "auto"), maximum=64),
                    "visible": _boolean(column_locale.get("visible"), default=True),
                })
            table_reference = raw.get("table_reference")
            if table_reference is None:
                table_reference_map: dict[str, Any] = {}
            elif isinstance(table_reference, Mapping):
                table_reference_map = dict(table_reference)
            else:
                raise ProjectError("INVALID_MODEL", f"{path} table_reference must be an object")
            for key in ("schema", "table"):
                if table_reference_map.get(key) is not None and not isinstance(table_reference_map.get(key), str):
                    raise ProjectError("INVALID_MODEL", f"{path} table_reference.{key} must be a string")
            models.append({
                "name": model_name,
                "sourcePath": path,
                "tableReference": {
                    "schema": table_reference_map.get("schema", "") or "",
                    "table": table_reference_map.get("table", "") or "",
                },
                "primaryKey": _primary_key(raw.get("primary_key")),
                "displayName": _localized(model_locale.get("display_name"), model_name),
                "description": _localized(model_locale.get("description"), _description(model_properties)),
                "businessDomain": _text(model_locale.get("business_domain", ""), maximum=255),
                "visible": _boolean(model_locale.get("visible"), default=True),
                "columns": columns,
                "draft": bool(item.get("draft")),
            })

        relationships_raw = self._read_optional_yaml(RELATIONSHIPS_PATH)
        raw_relationships = relationships_raw.get("relationships", [])
        if raw_relationships is None:
            raw_relationships = []
        if not isinstance(raw_relationships, list):
            raise ProjectError("INVALID_RELATIONSHIP", "relationships.yml relationships must be a list")
        relationships: list[dict[str, Any]] = []
        relationship_errors: list[dict[str, str]] = []
        for raw_relationship in raw_relationships:
            if not isinstance(raw_relationship, Mapping):
                raise ProjectError("INVALID_RELATIONSHIP", "relationships.yml entries must be objects")
            relationship = dict(raw_relationship)
            name = relationship.get("name")
            model_names_value = relationship.get("models", [])
            if not isinstance(name, str) or not name.strip() or not isinstance(model_names_value, list) or len(model_names_value) != 2 or any(not isinstance(model, str) for model in model_names_value):
                raise ProjectError("INVALID_RELATIONSHIP", "relationship name and two model names are required")
            relation_models = [str(model_names_value[0]), str(model_names_value[1])]
            unknown_models = [model for model in relation_models if model not in model_names]
            if unknown_models:
                relationship_errors.append({
                    "name": name,
                    "message": f"relationship references unknown model '{unknown_models[0]}'",
                })
                continue
            condition = relationship.get("condition", "")
            if condition is None:
                condition = ""
            if not isinstance(condition, str):
                raise ProjectError("INVALID_RELATIONSHIP", f"relationship '{name}' condition must be a string")
            locale = _mapping(locale_relationships.get(name))
            relationships.append({
                "name": name,
                "models": relation_models,
                "joinType": str(relationship.get("join_type", "MANY_TO_ONE")),
                "condition": condition,
                "displayName": _localized(locale.get("display_name"), name),
                "description": _localized(locale.get("description")),
            })
        overview = self.project.overview()
        result: dict[str, Any] = {
            "revision": overview["revision"],
            "draftCount": overview["draftCount"],
            "models": sorted(models, key=lambda model: model["name"].lower()),
            "relationships": sorted(relationships, key=lambda relation: relation["name"].lower()),
            "sourceFiles": [
                item
                for item in files
                if _MODEL_PATH.fullmatch(str(item.get("path", "")))
                or item.get("path") in {RELATIONSHIPS_PATH, LOCALES_PATH}
            ],
        }
        if relationship_errors:
            result["relationshipErrors"] = relationship_errors
        return result

    def save_model(self, model_name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not _IDENTIFIER.fullmatch(model_name):
            raise ProjectError("INVALID_MODEL", "model name is not valid")
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_MODEL", "model payload must be an object")
        snapshot = self.snapshot()
        current = next((item for item in snapshot["models"] if item["name"] == model_name), None)
        if current is None:
            raise ProjectError("MODEL_NOT_FOUND", "business model was not found")
        expected = self._check_revision(payload, snapshot["revision"], "model")
        incoming_columns = payload.get("columns")
        if not isinstance(incoming_columns, list):
            raise ProjectError("INVALID_MODEL", "columns must be a list")

        path = str(current["sourcePath"])
        raw = _yaml(self.project.read_file(path)["content"], path)
        raw_columns = _model_columns(raw, path)
        existing_by_name = {str(column["name"]): column for column in raw_columns}
        incoming_by_name: dict[str, Mapping[str, Any]] = {}
        for item in incoming_columns:
            if not isinstance(item, Mapping):
                raise ProjectError("INVALID_MODEL", "column entries must be objects")
            name = _text(item.get("name"), maximum=255)
            if not name:
                raise ProjectError("INVALID_MODEL", "column name is required")
            if name in incoming_by_name:
                raise ProjectError("INVALID_MODEL", f"duplicate column '{name}'")
            incoming_by_name[name] = item

        def apply_column(column: dict[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
            if "type" in incoming:
                column["type"] = _text(incoming.get("type"), maximum=255)
            if "primaryKey" in incoming:
                self._toggle(column, "is_primary_key", _boolean(incoming.get("primaryKey"), default=False))
            if "notNull" in incoming:
                self._toggle(column, "not_null", _boolean(incoming.get("notNull"), default=False))
            if "expression" in incoming or "calculated" in incoming:
                current_expression = column.get("expression", "")
                expression = _text(incoming.get("expression", current_expression), maximum=8_000)
                calculated = _boolean(
                    incoming.get("calculated"),
                    default=bool(column.get("is_calculated")) or "expression" in column,
                )
                if calculated and not expression:
                    raise ProjectError("INVALID_MODEL", f"calculated column '{column['name']}' requires an expression")
                if expression:
                    column["expression"] = expression
                    column["is_calculated"] = True
                else:
                    column.pop("expression", None)
                    if "is_calculated" in column:
                        column["is_calculated"] = False
            if "description" in incoming:
                description = _localized(incoming.get("description"))
                properties = _properties(column.get("properties"), f"{path} column '{column['name']}'")
                _set_description(properties, description)
                if properties:
                    column["properties"] = properties
                else:
                    column.pop("properties", None)
            return column

        updated_columns: list[dict[str, Any]] = []
        for existing in raw_columns:
            name = str(existing["name"])
            incoming = incoming_by_name.get(name)
            updated_columns.append(apply_column(dict(existing), incoming) if incoming is not None else dict(existing))
        for name, incoming in incoming_by_name.items():
            if name in existing_by_name:
                continue
            expression = _text(incoming.get("expression", ""), maximum=8_000)
            calculated = _boolean(incoming.get("calculated"), default=bool(expression))
            if not calculated or not expression:
                raise ProjectError("INVALID_MODEL", f"unknown column '{name}' must be a calculated column")
            created: dict[str, Any] = {
                "name": name,
                "type": _text(incoming.get("type", "DECIMAL"), maximum=255),
                "is_calculated": True,
                "expression": expression,
            }
            updated_columns.append(apply_column(created, incoming))
        raw["columns"] = updated_columns

        if "primaryKey" in payload:
            primary_key = _primary_key(payload.get("primaryKey"))
            if primary_key == "":
                raw.pop("primary_key", None)
            else:
                raw["primary_key"] = primary_key
        if "tableReference" in payload:
            table_reference = payload.get("tableReference")
            if not isinstance(table_reference, Mapping):
                raise ProjectError("INVALID_MODEL", "tableReference must be an object")
            current_reference = _properties(raw.get("table_reference"), path)
            for key in ("schema", "table"):
                if key in table_reference:
                    value = _text(table_reference.get(key), maximum=255)
                    if current_reference or value:
                        current_reference[key] = value
            # A ref_sql model legitimately has no table_reference.  The
            # snapshot still exposes an empty tableReference object for a
            # stable UI shape; do not manufacture an empty table_reference
            # and make Wren reject the model on a no-op save.
            if current_reference or any(table_reference.get(key) for key in ("schema", "table")):
                raw["table_reference"] = current_reference
        if "description" in payload:
            description = _localized(payload.get("description"))
            properties = _properties(raw.get("properties"), path)
            _set_description(properties, description)
            if properties:
                raw["properties"] = properties
            else:
                raw.pop("properties", None)

        locales = _locale_document(self._read_optional_yaml(LOCALES_PATH))
        locale_models = _mapping(locales.get("models"))
        model_locale = _mapping(locale_models.get(model_name))
        if "displayName" in payload:
            model_locale["display_name"] = _localized(payload.get("displayName"), model_name)
        if "description" in payload:
            model_locale["description"] = _localized(payload.get("description"))
        if "businessDomain" in payload:
            model_locale["business_domain"] = _text(payload.get("businessDomain"), maximum=255)
        if "visible" in payload:
            model_locale["visible"] = _boolean(payload.get("visible"), default=True)
        locale_columns = _mapping(model_locale.get("columns"))
        for name, incoming in incoming_by_name.items():
            locale_column = _mapping(locale_columns.get(name))
            if "displayName" in incoming:
                locale_column["display_name"] = _localized(incoming.get("displayName"), name)
            if "description" in incoming:
                locale_column["description"] = _localized(incoming.get("description"))
            if "semanticRole" in incoming:
                locale_column["semantic_role"] = _text(incoming.get("semanticRole"), maximum=64)
            if "format" in incoming:
                locale_column["format"] = _text(incoming.get("format"), maximum=64)
            if "visible" in incoming:
                locale_column["visible"] = _boolean(incoming.get("visible"), default=True)
            locale_columns[name] = locale_column
        if locale_columns:
            model_locale["columns"] = locale_columns
        elif "columns" in model_locale:
            model_locale.pop("columns", None)
        locale_models[model_name] = model_locale
        locales["models"] = locale_models

        self.project.put_files(
            {
                path: yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                LOCALES_PATH: yaml.safe_dump(locales, allow_unicode=True, sort_keys=False),
            },
            expected_revision=expected,
        )
        return self.snapshot()

    def save_relationships(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ProjectError("INVALID_RELATIONSHIP", "relationship payload must be an object")
        snapshot = self.snapshot()
        expected = self._check_revision(payload, snapshot["revision"], "relationships")
        incoming = payload.get("relationships")
        if not isinstance(incoming, list):
            raise ProjectError("INVALID_RELATIONSHIP", "relationships must be a list")
        model_name_set = {str(item["name"]) for item in snapshot["models"]}
        normalized: list[dict[str, Any]] = []
        locale_records: dict[str, Any] = {}
        seen: set[str] = set()
        locales = _locale_document(self._read_optional_yaml(LOCALES_PATH))
        existing_locale_relationships = _mapping(locales.get("relationships"))
        relationships_document = self._read_optional_yaml(RELATIONSHIPS_PATH)
        existing_relationships = relationships_document.get("relationships", [])
        if existing_relationships is None:
            existing_relationships = []
        if not isinstance(existing_relationships, list):
            raise ProjectError("INVALID_RELATIONSHIP", "relationships.yml relationships must be a list")
        existing_by_name = {
            str(item.get("name")): dict(item)
            for item in existing_relationships
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        for item in incoming:
            if not isinstance(item, Mapping):
                raise ProjectError("INVALID_RELATIONSHIP", "relationship must be an object")
            name = _text(item.get("name"), maximum=255, code="INVALID_RELATIONSHIP")
            models = item.get("models")
            if not _IDENTIFIER.fullmatch(name) or name in seen:
                raise ProjectError("INVALID_RELATIONSHIP", "relationship name must be unique and valid")
            if not isinstance(models, list) or len(models) != 2 or any(not isinstance(model, str) for model in models):
                raise ProjectError("INVALID_RELATIONSHIP", "relationship must reference two model names")
            relation_models = [str(models[0]), str(models[1])]
            if any(model not in model_name_set for model in relation_models):
                raise ProjectError("INVALID_RELATIONSHIP", "relationship must reference two existing models")
            condition = _text(item.get("condition"), maximum=8_000, code="INVALID_RELATIONSHIP")
            self._validate_condition(condition, relation_models, model_name_set)
            join_type = _text(item.get("joinType", "MANY_TO_ONE"), maximum=32, code="INVALID_RELATIONSHIP")
            if join_type not in _JOIN_TYPES:
                raise ProjectError("INVALID_RELATIONSHIP", "relationship join type is not supported")
            seen.add(name)
            relationship = dict(existing_by_name.get(name, {}))
            relationship.update({
                "name": name,
                "models": relation_models,
                "join_type": join_type,
                "condition": condition,
            })
            normalized.append(relationship)

            locale_record = _mapping(existing_locale_relationships.get(name))
            if "displayName" in item:
                locale_record["display_name"] = _localized(item.get("displayName"), name)
            if "description" in item:
                locale_record["description"] = _localized(item.get("description"))
            locale_records[name] = locale_record
        relationships_document["relationships"] = normalized
        locales["relationships"] = locale_records
        self.project.put_files(
            {
                RELATIONSHIPS_PATH: yaml.safe_dump(relationships_document, allow_unicode=True, sort_keys=False),
                LOCALES_PATH: yaml.safe_dump(locales, allow_unicode=True, sort_keys=False),
            },
            expected_revision=expected,
        )
        return self.snapshot()

    def _read_optional_yaml(self, path: str) -> dict[str, Any]:
        try:
            content = self.project.read_file(path)["content"]
        except ProjectError as exc:
            if exc.code == "FILE_NOT_FOUND":
                return {}
            raise
        return _yaml(content, path)

    @staticmethod
    def _check_revision(payload: Mapping[str, Any], revision: str, resource: str) -> str | None:
        expected = payload.get("expectedRevision")
        if expected is not None and (not isinstance(expected, str) or not expected):
            raise ProjectError("INVALID_REVISION", "expectedRevision must be a non-empty string")
        if expected is not None and expected != revision:
            raise ProjectError(
                "REVISION_CONFLICT",
                f"project changed since {resource} were read",
                {"revision": revision},
            )
        return expected

    @staticmethod
    def _validate_condition(condition: str, models: list[str], model_names: set[str]) -> None:
        if not condition:
            raise ProjectError("INVALID_RELATIONSHIP", "relationship condition is required")
        references = _QUALIFIED_REFERENCE.findall(condition)
        if not references:
            raise ProjectError("INVALID_RELATIONSHIP", "relationship condition must reference its models")
        qualifiers = {model for model, _column in references}
        unknown = sorted(qualifiers - model_names)
        if unknown:
            raise ProjectError("INVALID_RELATIONSHIP", f"relationship condition references unknown model '{unknown[0]}'")
        required = set(models)
        unexpected = sorted(qualifiers - required)
        if unexpected:
            raise ProjectError("INVALID_RELATIONSHIP", f"relationship condition references unrelated model '{unexpected[0]}'")
        if not required.issubset(qualifiers):
            missing = sorted(required - qualifiers)[0]
            raise ProjectError("INVALID_RELATIONSHIP", f"relationship condition must reference model '{missing}'")

    @staticmethod
    def _infer_role(column: Mapping[str, Any]) -> str:
        if column.get("is_primary_key"):
            return "key"
        name = str(column.get("name", "")).strip().lower()
        if name == "id" or name.endswith("_id"):
            return "key"
        kind = str(column.get("type", "")).upper()
        if any(token in kind for token in ("DATE", "TIME")):
            return "time"
        if any(token in kind for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE")):
            return "measure"
        return "dimension"

    @staticmethod
    def _toggle(target: dict[str, Any], key: str, enabled: bool) -> None:
        if enabled:
            target[key] = True
        else:
            target.pop(key, None)


__all__ = ["LOCALES_PATH", "RELATIONSHIPS_PATH", "SemanticModelStore"]
