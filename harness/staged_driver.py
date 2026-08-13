"""Detached, artifact-only supervisor for one paid staged Codex cohort.

The driver is deliberately outside the cohort root.  It can survive a short
terminal session without manufacturing or modifying measured evidence; it only
starts the retained staged controller and snapshots artifacts every 30 seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


POLL_SECONDS = 30


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\n")


def driver_root(metrics_root: str | Path, round_id: str) -> Path:
    """Return the external sibling directory used before a cohort exists."""

    return Path(metrics_root).resolve() / f"{round_id}-staged-driver"


def cohort_root(metrics_root: str | Path, round_id: str) -> Path:
    return Path(metrics_root).resolve() / round_id / "staged-codex"


def artifact_snapshot(metrics_root: str | Path, round_id: str) -> dict[str, object]:
    """Read retained artifacts only; never inspect or manipulate live processes."""

    root = cohort_root(metrics_root, round_id)
    arms: dict[str, object] = {}
    for arm in ("raw", "contextmesh", "full"):
        arm_root = root / arm
        stages = arm_root / "stages"
        stage_rows = []
        if stages.is_dir():
            for stage in sorted(path for path in stages.iterdir() if path.is_dir()):
                workers = stage / "workers"
                stage_rows.append(
                    {
                        "stage_id": stage.name,
                        "result": (stage / "result.json").is_file(),
                        "terra_stream": (stage / "terra" / "stream.jsonl").is_file(),
                        "worker_streams": len(list(workers.glob("*/stream.jsonl"))) if workers.is_dir() else 0,
                        "attempt_records": len(list(stage.glob("**/attempts/*.json"))),
                    }
                )
        arms[arm] = {
            "result": (arm_root / "result.json").is_file(),
            "broker_events": (arm_root / "broker" / "events.jsonl").is_file(),
            "stages": stage_rows,
        }
    return {
        "schema_version": 1,
        "observed_unix": time.time(),
        "round_id": round_id,
        "cohort_root": str(root),
        "prepared": (root / "run-plan.json").is_file(),
        "progress": (root / "progress.json").is_file(),
        "report": (root / "report.json").is_file() or any(root.glob("report-*-onward.json")),
        "arms": arms,
    }


def controller_command(
    repo_root: str | Path,
    round_id: str,
    metrics_root: str | Path,
    *,
    from_stage: str | None = None,
) -> tuple[str, ...]:
    command = (
        sys.executable,
        "-m",
        "harness.staged_codex",
        round_id,
        "--repo-root",
        str(Path(repo_root).resolve()),
        "--metrics-root",
        str(Path(metrics_root).resolve()),
        "--run",
    )
    return command if from_stage is None else (*command, "--from-stage", from_stage)


def run_controller(
    repo_root: str | Path,
    round_id: str,
    metrics_root: str | Path,
    *,
    from_stage: str | None = None,
    poll_seconds: float = POLL_SECONDS,
) -> int:
    """Run one retained controller and append artifact-only monitoring records."""

    root = driver_root(metrics_root, round_id)
    root.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = root / "controller.stdout.log", root / "controller.stderr.log"
    command = controller_command(repo_root, round_id, metrics_root, from_stage=from_stage)
    with stdout_path.open("a", encoding="utf-8") as stdout, stderr_path.open("a", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=Path(repo_root).resolve(), stdout=stdout, stderr=stderr, text=True)
        _write_json(
            root / "controller.json",
            {
                "schema_version": 1,
                "round_id": round_id,
                "command": list(command),
                "pid": process.pid,
                "started_unix": time.time(),
                "poll_seconds": poll_seconds,
                "monitoring": "artifact_only",
            },
        )
        while True:
            _append_jsonl(root / "monitor.jsonl", artifact_snapshot(metrics_root, round_id))
            exit_code = process.poll()
            if exit_code is not None:
                _append_jsonl(
                    root / "monitor.jsonl",
                    {**artifact_snapshot(metrics_root, round_id), "controller_exit_code": exit_code},
                )
                return exit_code
            time.sleep(poll_seconds)


def launch_detached(
    repo_root: str | Path,
    round_id: str,
    metrics_root: str | Path,
    *,
    from_stage: str | None = None,
) -> dict[str, object]:
    """Start the local supervisor without creating a partial cohort root."""

    root = driver_root(metrics_root, round_id)
    root.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "harness.staged_driver",
        "_controller",
        round_id,
        "--repo-root",
        str(Path(repo_root).resolve()),
        "--metrics-root",
        str(Path(metrics_root).resolve()),
    ]
    if from_stage is not None:
        command.extend(("--from-stage", from_stage))
    kwargs: dict[str, object] = {"cwd": str(Path(repo_root).resolve()), "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    payload = {
        "schema_version": 1,
        "round_id": round_id,
        "driver_root": str(root),
        "cohort_root": str(cohort_root(metrics_root, round_id)),
        "command": command,
        "pid": process.pid,
        "started_unix": time.time(),
        "monitoring": "artifact_only_every_30_seconds",
    }
    _write_json(root / "launch.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("launch", "_controller", "snapshot"):
        child = subparsers.add_parser(mode)
        child.add_argument("round_id")
        child.add_argument("--repo-root", default=".")
        child.add_argument("--metrics-root", default="metrics")
        child.add_argument("--from-stage")
    args = parser.parse_args(argv)
    if args.mode == "launch":
        print(json.dumps(launch_detached(args.repo_root, args.round_id, args.metrics_root, from_stage=args.from_stage), sort_keys=True))
        return 0
    if args.mode == "snapshot":
        print(json.dumps(artifact_snapshot(args.metrics_root, args.round_id), sort_keys=True))
        return 0
    return run_controller(args.repo_root, args.round_id, args.metrics_root, from_stage=args.from_stage)


if __name__ == "__main__":  # pragma: no cover - manual detached entry point.
    raise SystemExit(main())
