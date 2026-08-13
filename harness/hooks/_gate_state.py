"""Durable, session-keyed completion-gate state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateState:
    started_at: float
    cycles: int = 0


def _path(root: str | Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return Path(root) / f"{digest}.json"


def load_state(root: str | Path, session_id: str, now: float) -> GateState:
    path = _path(root, session_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return GateState(started_at=now)
    if not isinstance(payload, dict):
        raise ValueError(f"invalid gate state for session {session_id!r}")
    started_at = payload.get("started_at")
    cycles = payload.get("cycles")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise ValueError(f"invalid gate state start for session {session_id!r}")
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles < 0:
        raise ValueError(f"invalid gate state cycles for session {session_id!r}")
    return GateState(started_at=float(started_at), cycles=cycles)


def save_state(root: str | Path, session_id: str, state: GateState) -> None:
    path = _path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), sort_keys=True) + "\n", encoding="utf-8")
