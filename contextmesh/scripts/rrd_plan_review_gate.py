#!/usr/bin/env python3
"""Bind a forked plan review to the exact raw PLAN bytes.

The workspace review recorder intentionally hashes a normalized plan projection.  RRCv2 convergence
also needs a small repository-owned seal over the raw file, the exact reviewer transcript, and the
root-routed recorder row.  This tool is deliberately independent of product runtime code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

PLAN_CAP = 4 * 1024 * 1024
TRANSCRIPT_CAP = 1024 * 1024
RECORD_CAP = 64 * 1024
EVIDENCE_CAP = 64 * 1024


class PlanReviewGateError(RuntimeError):
    """The review cannot be proven to cover the current raw plan."""


def _bounded_regular(path: Path, cap: int, *, require_mode: int | None = None) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise PlanReviewGateError(f"cannot stat {path}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PlanReviewGateError(f"not a regular file: {path}")
    if require_mode is not None and stat.S_IMODE(before.st_mode) != require_mode:
        raise PlanReviewGateError(f"wrong mode for {path}: expected {require_mode:o}")
    if before.st_size > cap:
        raise PlanReviewGateError(f"file exceeds cap: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PlanReviewGateError(f"cannot open {path}") from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise PlanReviewGateError(f"file changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65_536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                raise PlanReviewGateError(f"file exceeds cap: {path}")
        after = os.fstat(fd)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise PlanReviewGateError(f"file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex64(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise PlanReviewGateError(f"invalid {field}")
    return value


def _decode_utf8(raw: bytes, field: str) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PlanReviewGateError(f"{field} is not UTF-8") from exc


def _check_unrouted_environment() -> None:
    for name in ("AGENT_WORKSPACE_SESSION", "AGENT_WORKSPACE_RUNTIME_ROOT"):
        if name in os.environ:
            raise PlanReviewGateError(f"{name} must be unset")


def _parse_record(raw: bytes) -> dict[str, Any]:
    try:
        row = tomllib.loads(_decode_utf8(raw, "review record"))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise PlanReviewGateError("invalid review record") from exc
    required = {
        "kind": "plan",
        "scope": "plan",
        "verdict": "SHIP",
        "origin": "forked",
        "mode": "unleashed",
        "workspace_session": "",
        "workspace_runtime_root": "",
    }
    for field, expected in required.items():
        if row.get(field) != expected:
            raise PlanReviewGateError(f"review record {field} must equal {expected!r}")
    _hex64(row.get("subject_hash"), "record subject_hash")
    _hex64(row.get("transcript_hash"), "record transcript_hash")
    repo_root = row.get("repo_root")
    if not isinstance(repo_root, str) or not Path(repo_root).is_absolute():
        raise PlanReviewGateError("review record has invalid repo_root")
    return row


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _atomic_write_0600(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def seal(*, plan: Path, transcript: Path, record: Path, output: Path) -> dict[str, Any]:
    _check_unrouted_environment()
    plan = plan.absolute()
    transcript = transcript.absolute()
    record = record.absolute()
    output = output.absolute()
    repo_root = plan.parent
    expected_record = repo_root / ".generated/state/reviews/plan.toml"
    expected_transcript_root = repo_root / ".generated/state/rrcv2-convergence/reviews"
    if record != expected_record:
        raise PlanReviewGateError("record must be the repository-root plan record")
    if (
        transcript.parent != expected_transcript_root
        or not transcript.name.startswith("plan-")
        or not transcript.name.endswith(".txt")
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-." for char in transcript.name)
    ):
        raise PlanReviewGateError("transcript must use the confined root-review path")
    plan_raw = _bounded_regular(plan, PLAN_CAP)
    transcript_raw = _bounded_regular(transcript, TRANSCRIPT_CAP, require_mode=0o600)
    record_raw = _bounded_regular(record, RECORD_CAP)
    row = _parse_record(record_raw)
    if Path(row["repo_root"]).absolute() != repo_root:
        raise PlanReviewGateError("review record belongs to another repository")
    raw_plan_sha = _sha(plan_raw)
    transcript_sha = _sha(transcript_raw)
    if row["transcript_hash"] != transcript_sha:
        raise PlanReviewGateError("transcript does not match review record")
    transcript_text = _decode_utf8(transcript_raw, "review transcript")
    marker = transcript_text.find("CHECKS RUN")
    if marker < 0 or raw_plan_sha not in transcript_text[marker:]:
        raise PlanReviewGateError("review CHECKS RUN does not name the raw PLAN SHA-256")
    evidence: dict[str, Any] = {
        "kind": "rrcv2_raw_plan_review_seal",
        "mode": row["mode"],
        "origin": row["origin"],
        "plan_path": str(plan),
        "raw_plan_sha256": raw_plan_sha,
        "record_path": str(record),
        "record_sha256": _sha(record_raw),
        "record_subject_hash": row["subject_hash"],
        "repo_root": str(repo_root),
        "transcript_path": str(transcript),
        "transcript_sha256": transcript_sha,
        "v": 1,
        "verdict": row["verdict"],
    }
    _atomic_write_0600(output, _canonical_json(evidence))
    return evidence


def check(evidence_path: Path) -> dict[str, Any]:
    _check_unrouted_environment()
    evidence_path = evidence_path.absolute()
    evidence_raw = _bounded_regular(evidence_path, EVIDENCE_CAP, require_mode=0o600)
    try:
        evidence = json.loads(_decode_utf8(evidence_raw, "seal evidence"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise PlanReviewGateError("invalid seal evidence") from exc
    if not isinstance(evidence, dict) or _canonical_json(evidence) != evidence_raw:
        raise PlanReviewGateError("seal evidence is not canonical JSON")
    if evidence.get("v") != 1 or evidence.get("kind") != "rrcv2_raw_plan_review_seal":
        raise PlanReviewGateError("unsupported seal evidence")
    plan = Path(str(evidence.get("plan_path", ""))).absolute()
    transcript = Path(str(evidence.get("transcript_path", ""))).absolute()
    record = Path(str(evidence.get("record_path", ""))).absolute()
    repo_root = Path(str(evidence.get("repo_root", ""))).absolute()
    if plan.parent != repo_root or record != repo_root / ".generated/state/reviews/plan.toml":
        raise PlanReviewGateError("seal paths do not describe one repository")
    transcript_root = repo_root / ".generated/state/rrcv2-convergence/reviews"
    if transcript.parent != transcript_root or not transcript.name.startswith("plan-"):
        raise PlanReviewGateError("seal transcript path is not root-confined")
    plan_raw = _bounded_regular(plan, PLAN_CAP)
    transcript_raw = _bounded_regular(transcript, TRANSCRIPT_CAP, require_mode=0o600)
    record_raw = _bounded_regular(record, RECORD_CAP)
    row = _parse_record(record_raw)
    expected = {
        "raw_plan_sha256": _sha(plan_raw),
        "transcript_sha256": _sha(transcript_raw),
        "record_sha256": _sha(record_raw),
        "record_subject_hash": row["subject_hash"],
        "repo_root": row["repo_root"],
        "origin": row["origin"],
        "verdict": row["verdict"],
        "mode": row["mode"],
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise PlanReviewGateError(f"stale seal field: {field}")
    if row["transcript_hash"] != expected["transcript_sha256"]:
        raise PlanReviewGateError("current transcript and record disagree")
    transcript_text = _decode_utf8(transcript_raw, "review transcript")
    marker = transcript_text.find("CHECKS RUN")
    if marker < 0 or expected["raw_plan_sha256"] not in transcript_text[marker:]:
        raise PlanReviewGateError("current review does not name current raw plan")
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("--plan", type=Path, required=True)
    seal_parser.add_argument("--transcript", type=Path, required=True)
    seal_parser.add_argument("--record", type=Path, required=True)
    seal_parser.add_argument("--output", type=Path, required=True)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "seal":
            result = seal(
                plan=args.plan, transcript=args.transcript, record=args.record, output=args.output
            )
        else:
            result = check(args.evidence)
    except PlanReviewGateError as exc:
        print(f"rrcv2 plan review gate: FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
