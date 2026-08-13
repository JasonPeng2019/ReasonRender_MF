"""Validation for JSON module packets."""

from __future__ import annotations

import json
import math
from pathlib import PurePosixPath, PureWindowsPath
from collections.abc import Iterable
from typing import TypeAlias, cast

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
NormalizedPacket: TypeAlias = dict[str, JSONValue]


class PacketValidationError(ValueError):
    """Raised when a module packet does not satisfy the shared contract."""


PacketError = PacketValidationError


def _require_json(value: object, location: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise PacketValidationError(f"{location} contains a non-finite number")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PacketValidationError(f"{location} has a non-string object key")
            _require_json(item, f"{location}.{key}")
        return
    raise PacketValidationError(f"{location} is not JSON-compatible")


def _relative_path(value: object, location: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PacketValidationError(f"{location} must be a non-empty relative path")

    portable = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    if portable.startswith("/") or windows.drive:
        raise PacketValidationError(f"{location} must be relative")

    parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PacketValidationError(f"{location} must remain under the target root")
    if PurePosixPath(portable).is_absolute():
        raise PacketValidationError(f"{location} must be relative")
    return "/".join(parts)


def validate_packet(
    payload: object,
    expected_packet_id: str,
    allowed_paths: Iterable[str],
) -> NormalizedPacket:
    """Validate and detach a JSON module packet from its caller-owned data."""

    if not isinstance(expected_packet_id, str) or not expected_packet_id:
        raise PacketValidationError("expected_packet_id must be a non-empty string")
    if isinstance(allowed_paths, (str, bytes)):
        raise PacketValidationError("allowed_paths must be an iterable of paths")
    try:
        allowed = frozenset(_relative_path(path, "allowed path") for path in allowed_paths)
    except TypeError as error:
        raise PacketValidationError("allowed_paths must be an iterable of paths") from error

    _require_json(payload)
    if not isinstance(payload, dict):
        raise PacketValidationError("packet must be a JSON object")

    required_fields = ("packet_id", "required_slots", "slot_values", "owned_paths")
    missing_fields = [field for field in required_fields if field not in payload]
    if missing_fields:
        raise PacketValidationError(f"packet is missing required fields: {', '.join(missing_fields)}")

    packet_id = payload["packet_id"]
    if not isinstance(packet_id, str):
        raise PacketValidationError("packet_id must be a string")
    if packet_id != expected_packet_id:
        raise PacketValidationError("packet_id does not match the expected packet id")

    required_slots = payload["required_slots"]
    if not isinstance(required_slots, list) or not all(
        isinstance(slot, str) and bool(slot) for slot in required_slots
    ):
        raise PacketValidationError("required_slots must be a list of non-empty names")
    if len(set(required_slots)) != len(required_slots):
        raise PacketValidationError("required_slots must not contain duplicates")

    slot_values = payload["slot_values"]
    if not isinstance(slot_values, dict):
        raise PacketValidationError("slot_values must be a JSON object")
    missing_slots = [slot for slot in required_slots if slot not in slot_values]
    if missing_slots:
        raise PacketValidationError(f"missing required slot: {', '.join(missing_slots)}")

    declared_paths = payload["owned_paths"]
    if not isinstance(declared_paths, list):
        raise PacketValidationError("owned_paths must be a list")
    normalized_paths = [_relative_path(path, "owned path") for path in declared_paths]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise PacketValidationError("owned_paths must not contain duplicates")
    unauthorized = [path for path in normalized_paths if path not in allowed]
    if unauthorized:
        raise PacketValidationError(f"owned path is not allowed: {unauthorized[0]}")

    normalized = cast(NormalizedPacket, json.loads(json.dumps(payload, sort_keys=True, allow_nan=False)))
    normalized["owned_paths"] = normalized_paths
    return normalized


__all__ = ["JSONValue", "NormalizedPacket", "PacketError", "PacketValidationError", "validate_packet"]
