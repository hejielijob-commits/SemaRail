"""Minimal independent row comparator for the preserved legacy regression."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
from typing import Any


def rows_equal(actual: list[Any], expected: list[Any], tolerance: float,
               ordered: bool, aliases: Mapping[str, Any] | None = None,
               allowed_extras: list[str] | None = None) -> bool:
    expected_keys = set(expected[0]) if expected else set()
    alias_map = aliases or {}
    extras = set(allowed_extras or [])
    # The preserved v1 corpus predates an explicit numericColumns field. Derive
    # the trusted numeric set from the frozen expected values alone, never from
    # result schema metadata or numeric-looking strings. A key is numeric only
    # when every expected row has a finite int/float value (bool is deliberately
    # excluded even though Python treats it as an int subclass).
    numeric_keys = {
        key for key in expected_keys
        if all(
            isinstance(row, Mapping)
            and type(row.get(key)) in (int, float)
            and (type(row.get(key)) is int or math.isfinite(row[key]))
            for row in expected
        )
    }

    def normalize(row: Any) -> Mapping[str, Any] | None:
        if not isinstance(row, Mapping):
            return None
        result: dict[str, Any] = {}
        consumed: set[str] = set()
        for key in expected_keys:
            raw = alias_map.get(key, [])
            candidates = [key, *(raw if isinstance(raw, list) else [raw])]
            matches = [candidate for candidate in candidates if candidate in row]
            if len(matches) != 1:
                return None
            consumed.add(matches[0])
            result[key] = row[matches[0]]
        if set(row) - consumed - extras:
            return None
        return result

    def equal(left: Any, right: Any) -> bool:
        normalized = normalize(left)
        if normalized is None or not isinstance(right, Mapping) or set(normalized) != set(right):
            return False
        for key, value in normalized.items():
            other = right[key]
            if key in numeric_keys:
                if not numeric_equal(value, other, tolerance):
                    return False
                continue
            if isinstance(value, bool) or isinstance(other, bool):
                if type(value) is not type(other) or value != other:
                    return False
            elif isinstance(value, (int, float)) and isinstance(other, (int, float)):
                if not math.isfinite(float(value)) or not math.isfinite(float(other)):
                    return False
                if abs(float(value) - float(other)) > tolerance:
                    return False
            elif type(value) is not type(other) or value != other:
                return False
        return True

    if len(actual) != len(expected):
        return False
    if ordered:
        return all(equal(left, right) for left, right in zip(actual, expected))
    remaining = list(actual)
    for row in expected:
        index = next((i for i, candidate in enumerate(remaining) if equal(candidate, row)), None)
        if index is None:
            return False
        remaining.pop(index)
    return True


def numeric_equal(left: Any, right: Any, tolerance: float) -> bool:
    """Compare only oracle-declared numeric values without float rounding."""
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, (str, int, float)) or not isinstance(right, (str, int, float)):
        return False
    try:
        left_number = Decimal(str(left))
        right_number = Decimal(str(right))
        tolerance_number = Decimal(str(tolerance))
        return (
            left_number.is_finite()
            and right_number.is_finite()
            and tolerance_number.is_finite()
            and tolerance_number >= 0
            and abs(left_number - right_number) <= tolerance_number
        )
    except (InvalidOperation, ValueError, TypeError):
        return False
