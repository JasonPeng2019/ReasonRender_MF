"""Input normalization keeps policy modules independent from request transport."""
from __future__ import annotations

from collections.abc import Mapping

from .domain import NormalizedInput


def normalize_payload(payload: Mapping[str, object], subject_id: str, request_id: str) -> NormalizedInput:
    return NormalizedInput(values={key: normalize_value(value) for key, value in payload.items()}, subject_id=subject_id, request_id=request_id)


def normalize_value(value: object) -> object:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return tuple(normalize_value(item) for item in value)
    if isinstance(value, dict):
        return {key: normalize_value(item) for key, item in value.items()}
    return value


def normalize_text(value: str) -> str:
    return " ".join(value.strip().split())


def lower_text(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return normalize_text(value).lower()


def number(value: object | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def sequence(value: object | None) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return ()


def has_field(data: NormalizedInput, field: str) -> bool:
    return data.value(field) is not None


def required_text(data: NormalizedInput, field: str) -> str | None:
    return lower_text(data.value(field))


def required_number(data: NormalizedInput, field: str) -> float | None:
    return number(data.value(field))


def normalized_fields(data: NormalizedInput) -> tuple[str, ...]:
    return tuple(sorted(data.values))


def diagnostic_value(data: NormalizedInput, field: str) -> str:
    value = data.value(field)
    if value is None:
        return "<missing>"
    return str(value)
