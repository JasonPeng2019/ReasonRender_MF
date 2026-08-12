#!/usr/bin/env python3
"""Materialize one canonical coding assignment and render the product demo prompt."""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import stat
import tempfile
from pathlib import Path

from rrc.contextmesh import CodingAssignmentV1
from rrc.contract import (
    ArtifactRefV1,
    ReferencedTaskInputV1,
    TargetPreimageV1,
    Task,
    canonical_test_artifact_bytes,
    seal_task_input,
    task_envelope_bytes,
)

_TASK_ID = "rrcv2-general-demo-001"
_ARTIFACT_PATH = "rrcv2_demo/reading.py"
_PUBLIC_PATH = "rrcv2_demo/reading.public.v1.json"
_ORACLE_PATH = "rrcv2_demo/reading.oracle.v1.json"
_STARTER = """from dataclasses import dataclass

@dataclass(frozen=True)
class Reading:
    value: int

def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))

def normalize_reading(value: int) -> Reading:
    raise NotImplementedError
"""
_PUBLIC_TESTS = (
    "def test_normalize_reading():\n    assert normalize_reading(7) == Reading(7)",
    "def test_clamp_helper():\n    assert clamp(12, 0, 10) == 10",
)
_ORACLE_TESTS = ("def test_normalize_negative():\n    assert normalize_reading(-3).value == -3",)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bounded_regular(path: Path, *, cap: int, mode: int) -> bytes | None:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_size > cap
    ):
        raise ValueError(f"demo authority is not bounded mode-{mode:04o} regular data: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        after = os.fstat(descriptor)
        raw = os.read(descriptor, cap + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(after.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or len(raw) != after.st_size
        or len(raw) > cap
    ):
        raise ValueError(f"demo authority changed while it was read: {path}")
    return raw


def _create(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("demo authority write was incomplete")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exact_authority(path: Path, raw: bytes, mode: int) -> None:
    existing = _bounded_regular(path, cap=max(1, len(raw)), mode=mode)
    if existing is None:
        _create(path, raw, mode)
    elif existing != raw:
        raise ValueError(f"existing demo authority differs: {path}")


def _target_source(path: Path, starter: bytes) -> bytes:
    existing = _bounded_regular(path, cap=1024 * 1024, mode=0o644)
    if existing is None:
        _create(path, starter, 0o644)
        return starter
    return existing


def render(
    *,
    repository: Path,
    target: Path,
    mode: str,
    root_sentinel: str,
    parent_history_sentinel: str,
    uv_bin: str = "uv",
    task_envelope_output: Path | None = None,
) -> str:
    starter = _STARTER.encode("utf-8", errors="strict")
    public = canonical_test_artifact_bytes(_PUBLIC_TESTS)
    oracle = canonical_test_artifact_bytes(_ORACLE_TESTS)
    source = _target_source(target / _ARTIFACT_PATH, starter)
    _exact_authority(target / _PUBLIC_PATH, public, 0o600)
    _exact_authority(target / _ORACLE_PATH, oracle, 0o600)
    task = Task(
        task_id=_TASK_ID,
        text=(
            "Implement normalize_reading(value) so it returns a Reading containing the exact "
            "integer. Preserve the dataclass, import, clamp helper, annotations, and public API."
        ),
        family="reading_normalization",
        artifact_path=_ARTIFACT_PATH,
        searchable_public=False,
        verification_profile="rrcv2_general_v1",
        primary="normalize_reading",
        shape=None,
        slot_values=None,
    )
    assignment = CodingAssignmentV1(
        mode=mode,  # type: ignore[arg-type]
        task=task,
        source_path=_ARTIFACT_PATH,
        public_test_path=_PUBLIC_PATH,
        oracle_test_path=_ORACLE_PATH,
        owned_paths=(_ARTIFACT_PATH,),
        target_preimage=TargetPreimageV1.regular(_ARTIFACT_PATH, _sha(source), len(source), 0o644),
    )
    if task_envelope_output is not None:
        task_envelope_output = task_envelope_output.absolute()
        task_envelope_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(
            tempfile.mkdtemp(prefix="rrcv2-root-input-", dir=task_envelope_output.parent)
        )
        try:
            incoming = temporary / "incoming"
            _create(incoming / _ARTIFACT_PATH, source, 0o644)
            _create(incoming / ".rrcv2/public-tests.v1.json", public, 0o600)
            _create(incoming / ".rrcv2/oracle-tests.v1.json", oracle, 0o600)
            envelope = seal_task_input(
                ReferencedTaskInputV1(
                    task=task,
                    sealed_root=incoming,
                    source_ref=ArtifactRefV1(_sha(source), len(source), _ARTIFACT_PATH),
                    public_test_ref=ArtifactRefV1(
                        _sha(public), len(public), ".rrcv2/public-tests.v1.json"
                    ),
                    oracle_ref=ArtifactRefV1(
                        _sha(oracle), len(oracle), ".rrcv2/oracle-tests.v1.json"
                    ),
                    target_preimage=assignment.target_preimage,
                ),
                input_root=temporary / "sealed",
            )
            _exact_authority(task_envelope_output, task_envelope_bytes(envelope), 0o600)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    reader_command = shlex.join(
        (
            uv_bin,
            "run",
            "--locked",
            "--project",
            str(repository / "pyproject.toml"),
            "python",
            str(repository / "contextmesh/scripts/rrd_result_reader.py"),
            "apply",
            "--attempt-id",
            "ID",
            "--receipt",
            "RECEIPT",
        )
    )
    lines = [
        "Run the canonical ReasonRenderCoding + ContextMesh coding demo.",
        f"Root-only source sentinel: {root_sentinel}",
        f"Root-only parent-history sentinel: {parent_history_sentinel}",
        "Do not inspect or read the task source, public tests, oracle tests, or result files directly.",
        ("Spawn exactly one native worker with agent_type=worker and fork_context=false."),
        "Give the worker exactly the complete RRCV2_CODING_ASSIGNMENT_V1 marker below; do not alter it.",
        "The hook will replace each assignment with a source-blind canonical IMPLEMENT prompt.",
        "Wait for every worker. If a returned rrc_pending row remains, wait again only for that agent.",
        "For each rrc_accepted row, run exactly: " + reader_command,
        "Do not use any alternate command or path. Report task IDs and the hash-only apply responses; never reproduce source.",
        "",
    ]
    lines.extend(("Assignment 1:", assignment.marker(), ""))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--mode", choices=("cold", "warm"), required=True)
    parser.add_argument("--root-sentinel", required=True)
    parser.add_argument("--parent-history-sentinel", required=True)
    parser.add_argument("--uv-bin", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-envelope-output", type=Path, required=True)
    args = parser.parse_args()
    text = render(
        repository=args.repository.resolve(strict=True),
        target=args.target.resolve(strict=True),
        mode=args.mode,
        root_sentinel=args.root_sentinel,
        parent_history_sentinel=args.parent_history_sentinel,
        uv_bin=args.uv_bin,
        task_envelope_output=args.task_envelope_output,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    os.chmod(args.output, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
