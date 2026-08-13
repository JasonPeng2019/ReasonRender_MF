"""Provider-free completion-gate semantics for future SubagentStop wiring."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

# Claude executes this hook by absolute script path with the isolated target as
# its cwd, so the repository package is otherwise absent from sys.path.
if __package__ in {None, ""}:  # pragma: no cover - exercised through the CLI.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from ..gate_manifest import GateTask, load_manifest_tasks
    from ._gate_state import GateState, load_state, save_state
except ImportError:  # pragma: no cover - direct hook invocation.
    from harness.gate_manifest import GateTask, load_manifest_tasks
    from harness.hooks._gate_state import GateState, load_state, save_state


GATE_MAX_CYCLES = 3
GATE_MAX_WALL_S = 900
COMMAND_TIMEOUT_S = 180
Runner = Callable[[str, str], tuple[int, str]]
Clock = Callable[[], float]


def _setting(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def resolve_task_id(payload: Mapping[str, object], known_task_ids: Mapping[str, GateTask]) -> str | None:
    """Resolve one task id from the child transcript, then the parent fallback."""

    for path_value in (payload.get("agent_transcript_path"), payload.get("transcript_path")):
        if not isinstance(path_value, str) or not path_value:
            continue
        try:
            transcript = Path(path_value).read_text(encoding="utf-8")
        except OSError:
            continue
        matches = [task_id for task_id in known_task_ids if task_id in transcript]
        if len(matches) == 1:
            return matches[0]
    return None


def _default_runner(command: str, cwd: str) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return 124, f"command timed out after {COMMAND_TIMEOUT_S}s\n{output}"
    return result.returncode, result.stdout + result.stderr


def _tail(value: str) -> str:
    return value[-2000:]


def _record(
    log_path: str | Path,
    *,
    session_id: str,
    task_id: str | None,
    cycle: int,
    verdict: str,
    cmd: str | None,
    exit_code: int | None,
    duration_ms: int,
) -> None:
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "task_id": task_id,
        "cycle": cycle,
        "verdict": verdict,
        "cmd": cmd,
        "exit_code": exit_code,
        "duration_ms": duration_ms,
    }
    destination = Path(log_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")


def _allow(verdict: str, **fields: Any) -> dict[str, Any]:
    return {"decision": "approve", "verdict": verdict, **fields}


def evaluate(
    payload: Mapping[str, object],
    tasks: Mapping[str, GateTask],
    *,
    state_dir: str | Path,
    log_path: str | Path,
    runner: Runner = _default_runner,
    clock: Clock = time.time,
    max_cycles: int | None = None,
    max_wall_s: int | None = None,
) -> dict[str, Any]:
    """Evaluate one hook payload and return a stdout-JSON transport decision."""

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    task_id = resolve_task_id(payload, tasks)
    now = clock()
    if task_id is None:
        _record(log_path, session_id=session_id, task_id=None, cycle=0, verdict="unresolved", cmd=None, exit_code=None, duration_ms=0)
        return _allow("unresolved")
    task = tasks[task_id]
    state = load_state(state_dir, session_id, now)
    cycle_limit = _setting("GATE_MAX_CYCLES", GATE_MAX_CYCLES) if max_cycles is None else max_cycles
    wall_limit = _setting("GATE_MAX_WALL_S", GATE_MAX_WALL_S) if max_wall_s is None else max_wall_s
    if now - state.started_at > wall_limit:
        _record(log_path, session_id=session_id, task_id=task_id, cycle=state.cycles, verdict="fail_timeout", cmd=task.acceptance_cmd, exit_code=None, duration_ms=0)
        return _allow("fail_timeout")
    if state.cycles >= cycle_limit:
        _record(log_path, session_id=session_id, task_id=task_id, cycle=state.cycles, verdict="fail", cmd=task.acceptance_cmd, exit_code=None, duration_ms=0)
        return _allow("fail")

    cwd = payload.get("cwd")
    run_cwd = cwd if isinstance(cwd, str) and cwd else "."
    started = clock()
    exit_code, output = runner(task.acceptance_cmd, run_cwd)
    duration_ms = max(0, int((clock() - started) * 1000))
    if exit_code == 0:
        _record(log_path, session_id=session_id, task_id=task_id, cycle=state.cycles, verdict="pass", cmd=task.acceptance_cmd, exit_code=0, duration_ms=duration_ms)
        return _allow("pass")

    updated = GateState(started_at=state.started_at, cycles=state.cycles + 1)
    save_state(state_dir, session_id, updated)
    reason = (
        f"Acceptance gate failed for {task_id}.\n$ {task.acceptance_cmd}\n"
        f"exit={exit_code}\n{_tail(output)}\n"
        f"Fix the cause and re-run. Attempt {updated.cycles}/{cycle_limit}."
    )
    _record(log_path, session_id=session_id, task_id=task_id, cycle=updated.cycles, verdict="block", cmd=task.acceptance_cmd, exit_code=exit_code, duration_ms=duration_ms)
    return {"decision": "block", "verdict": "block", "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="rrc/manifest.json")
    parser.add_argument("--state-dir", default=".rrc-gate/state")
    parser.add_argument("--log-path", default="gate.jsonl")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        decision = evaluate(
            payload,
            load_manifest_tasks(args.manifest),
            state_dir=args.state_dir,
            log_path=args.log_path,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"completion gate error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: value for key, value in decision.items() if key != "verdict"}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI exercised in tests.
    raise SystemExit(main())
