#!/usr/bin/env python3
"""Resumable zero-provider post-v20 reviewed-reference refresh."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from rrc.contract import canonical_json_bytes

PLAN_SHA256 = "d50b81b37183ad9805147e4215792a43d801f424340464e3698a75d51e537c73"
TRANSCRIPT_SHA256 = "d1685d4d706e89589c38f4e7306ffb23a160de05cc6bce67c6b617c0b5a595b8"
SEAL_SHA256 = "ad87bbc6086e333a38cc9b7c77f31e472d4770c5600227a3b755b47079cbb0e2"
RECORD_SHA256 = "a53ec32217d88852c69c4e001b2e4a1b853b0798a7b306a74d73990f0ed192ad"
V20_REFRESH_SHA256 = "5930921629bbfd4f80d770eb8cef51fab3d4eafe4df2e5a4670fdaa2cc66335a"
OLD_REVIEW = b"plan-m6-cli-smoke-v20"
NEW_REVIEW = b"plan-m6-post-v20-hook-bootstrap-v2"
OLD_PLAN = b"d9e02378d8539894d966b3a687abcf1bb4441df7ad1d773a9afee505ed7ee00c"
NEW_PLAN = PLAN_SHA256.encode()
_MAX = 4_000_000


class RefreshError(RuntimeError):
    """The closed reference refresh could not converge safely."""


@dataclass(frozen=True)
class Row:
    relative: str
    pre_sha256: str
    post_sha256: str
    old: bytes
    new: bytes
    count: int


ROWS = (
    Row(
        "rrc/product_runtime.py",
        "b839b07ef521148ae82b3fecfe5f94c56d26fbe524fc4a7de01cf89ef769535c",
        "9d878f2178aced7305fb76039b20931b8b8aa65dc917e33dfb39c4f9d6937f4c",
        OLD_REVIEW,
        NEW_REVIEW,
        2,
    ),
    Row(
        "contextmesh/bench/run_rrcv2_bench.py",
        "28743227c72290bf84c2743c0e3ce3052319b7f4ad2bf931042918b01ee6153e",
        "9cbdec5867d3bc7a4db996deb0ae1a1886664dd7e64a3101d9906fc8d7ec3e3f",
        OLD_REVIEW,
        NEW_REVIEW,
        2,
    ),
    Row(
        "docs/decisions/0002-rrcv2-full-contextmesh-profile.md",
        "2a53a9f948785b61ad6ae2ee6f45935b41289cd6983461d5dc56f0ffad9ac149",
        "c74a832edfbb442bb0ba469255e1c15cd79f574824a08fcd58a59724f9f6d598",
        OLD_PLAN,
        NEW_PLAN,
        1,
    ),
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path, *, mode: int, cap: int = _MAX) -> tuple[bytes, os.stat_result]:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_nlink != 1
        or before.st_uid != os.getuid()
        or before.st_size > cap
    ):
        raise RefreshError(f"reference authority is invalid: {path}")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or len(raw) != opened.st_size
        or len(raw) > cap
    ):
        raise RefreshError(f"reference authority changed while opening: {path}")
    return raw, opened


def _write(path: Path, raw: bytes, mode: int) -> None:
    fd = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise RefreshError("reference authority write was incomplete")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _recover_publish(path: Path, raw: bytes) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    target_exists = path.exists() or path.is_symlink()
    temp_exists = temporary.exists() or temporary.is_symlink()
    if temp_exists:
        temp_raw, temp_meta = _read(temporary, mode=0o600, cap=len(raw))
        if temp_raw != raw:
            raise RefreshError(f"foreign publication temporary: {temporary}")
        if not target_exists:
            os.link(temporary, path, follow_symlinks=False)
            _fsync(path.parent)
            target_exists = True
        target_raw, target_meta = _read_linked(path, len(raw))
        if target_raw != raw or (target_meta.st_dev, target_meta.st_ino) != (
            temp_meta.st_dev,
            temp_meta.st_ino,
        ):
            raise RefreshError("publication final/temporary pair differs")
        temporary.unlink()
        _fsync(path.parent)
        return
    if target_exists:
        existing, _ = _read(path, mode=0o600, cap=len(raw))
        if existing != raw:
            raise RefreshError(f"published authority differs: {path}")
        return
    _write(temporary, raw, 0o600)
    os.link(temporary, path, follow_symlinks=False)
    _fsync(path.parent)
    temporary.unlink()
    _fsync(path.parent)


def _read_linked(path: Path, cap: int) -> tuple[bytes, os.stat_result]:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != 2
        or before.st_uid != os.getuid()
        or before.st_size > cap
    ):
        raise RefreshError("linked publication authority differs")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        raw = os.read(fd, cap + 1)
    finally:
        os.close(fd)
    if len(raw) != opened.st_size or (opened.st_dev, opened.st_ino) != (
        before.st_dev,
        before.st_ino,
    ):
        raise RefreshError("linked publication authority changed")
    return raw, opened


def _projection(state: str) -> dict[str, object]:
    return {
        "kind": "RRCV2ReferenceRefreshPostV20HookBootstrapV2",
        "plan_review_record_sha256": RECORD_SHA256,
        "plan_review_seal_sha256": SEAL_SHA256,
        "plan_review_transcript_sha256": TRANSCRIPT_SHA256,
        "plan_sha256": PLAN_SHA256,
        "predecessor_refresh_sha256": V20_REFRESH_SHA256,
        "rows": [
            {
                "mode": 0o644,
                "path": row.relative,
                "post_sha256": row.post_sha256,
                "pre_sha256": row.pre_sha256,
            }
            for row in ROWS
        ],
        "state": state,
        "v": 1,
    }


def _authority(repo: Path, plan: Path, transcript: Path, seal: Path, record: Path) -> None:
    expected = {
        plan: PLAN_SHA256,
        transcript: TRANSCRIPT_SHA256,
        seal: SEAL_SHA256,
        record: RECORD_SHA256,
    }
    exact = {
        repo / "PLAN.md",
        repo / ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.txt",
        repo
        / ".generated/state/rrcv2-convergence/reviews/plan-m6-post-v20-hook-bootstrap-v2.seal.json",
        repo / ".generated/state/reviews/plan.toml",
    }
    if set(expected) != exact:
        raise RefreshError("review authority paths differ")
    for path, digest in expected.items():
        raw, _ = _read(path, mode=0o600 if path not in {plan, record} else 0o644)
        if _sha(raw) != digest:
            raise RefreshError(f"review authority hash differs: {path.name}")
    seal_raw, _ = _read(seal, mode=0o600, cap=64 * 1024)
    record_raw, _ = _read(record, mode=0o644, cap=64 * 1024)
    try:
        seal_value = json.loads(seal_raw)
        record_value = tomllib.loads(record_raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RefreshError("review authority is invalid") from exc
    if (
        not isinstance(seal_value, dict)
        or seal_raw
        not in {canonical_json_bytes(seal_value), canonical_json_bytes(seal_value) + b"\n"}
        or seal_value.get("raw_plan_sha256") != PLAN_SHA256
        or seal_value.get("transcript_sha256") != TRANSCRIPT_SHA256
        or seal_value.get("record_sha256") != RECORD_SHA256
        or seal_value.get("repo_root") != str(repo)
        or seal_value.get("verdict") != "SHIP"
        or record_value.get("verdict") != "SHIP"
        or record_value.get("transcript_hash") != TRANSCRIPT_SHA256
        or record_value.get("subject_hash") != seal_value.get("record_subject_hash")
        or record_value.get("repo_root") != str(repo)
        or record_value.get("workspace_session") != ""
        or record_value.get("workspace_runtime_root") != ""
    ):
        raise RefreshError("review authority relation differs")


def _parse_record(path: Path, expected: bytes) -> None:
    raw, _ = _read(path, mode=0o600, cap=64 * 1024)
    if raw != expected:
        raise RefreshError(f"reference transaction record differs: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshError("reference transaction record is invalid") from exc
    if canonical_json_bytes(value) != raw:
        raise RefreshError("reference transaction record is noncanonical")


def _validate_v20_history(repo: Path) -> None:
    path = repo / ".generated/state/rrcv2-convergence/verify/reference-refresh.v20.json"
    raw, _ = _read(path, mode=0o600, cap=64 * 1024)
    if _sha(raw) != V20_REFRESH_SHA256:
        raise RefreshError("historical v20 reference authority differs")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefreshError("historical v19 reference authority is invalid") from exc
    if (
        canonical_json_bytes(value) != raw
        or value.get("state") != "complete"
        or value.get("v") != 20
    ):
        raise RefreshError("historical v20 reference authority is noncanonical")


def _target_state(repo: Path, row: Row) -> str:
    raw, metadata = _read(repo / row.relative, mode=0o644)
    digest = _sha(raw)
    if digest == row.pre_sha256:
        if raw.count(row.old) != row.count or row.new in raw:
            raise RefreshError(f"reference preimage literal differs: {row.relative}")
        return "pre"
    if digest == row.post_sha256:
        if raw.count(row.new) != row.count or row.old in raw:
            raise RefreshError(f"reference postimage literal differs: {row.relative}")
        return "post"
    del metadata
    raise RefreshError(f"reference target is neither preimage nor postimage: {row.relative}")


def _replace_target(repo: Path, row: Row) -> None:
    path = repo / row.relative
    raw, metadata = _read(path, mode=0o644)
    transformed = raw.replace(row.old, row.new)
    if raw.count(row.old) != row.count or _sha(transformed) != row.post_sha256:
        raise RefreshError(f"reference transformation differs: {row.relative}")
    temporary = path.with_name(".rrcv2-reference-refresh-post-v20-hook-bootstrap-v2.stage")
    if temporary.exists() or temporary.is_symlink():
        staged, _ = _read(temporary, mode=0o644, cap=len(transformed))
        if staged != transformed:
            raise RefreshError(f"foreign reference staging file: {temporary}")
    else:
        _write(temporary, transformed, stat.S_IMODE(metadata.st_mode))
    current = os.lstat(path)
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise RefreshError(f"reference target changed before replace: {path}")
    os.replace(temporary, path)
    _fsync(path.parent)
    if _target_state(repo, row) != "post":
        raise RefreshError(f"reference postimage did not reopen: {row.relative}")


def _print_inventory(terminal: str) -> None:
    print(f"PLAN.md {PLAN_SHA256}")
    print(f"plan-m6-post-v20-hook-bootstrap-v2.txt {TRANSCRIPT_SHA256}")
    print(f"plan-m6-post-v20-hook-bootstrap-v2.seal.json {SEAL_SHA256}")
    print(f"plan.toml {RECORD_SHA256}")
    for row in ROWS:
        print(f"{row.relative} {row.post_sha256}")
    print(terminal)


def apply(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    _validate_v20_history(repo)
    _authority(
        repo,
        args.plan.resolve(),
        args.transcript.resolve(),
        args.seal.resolve(),
        args.record.resolve(),
    )
    pending = args.pending.resolve()
    output = args.output.resolve()
    expected_parent = (repo / ".generated/state/rrcv2-convergence/verify").resolve()
    if pending.parent != expected_parent or output.parent != expected_parent:
        raise RefreshError("reference transaction paths differ")
    pending_raw = canonical_json_bytes(_projection("pending"))
    complete_raw = canonical_json_bytes(_projection("complete"))
    pending_exists = pending.exists() or pending.is_symlink()
    output_exists = output.exists() or output.is_symlink()
    if not pending_exists and not output_exists:
        _recover_publish(pending, pending_raw)
        pending_exists = True
    if pending_exists:
        _recover_publish(pending, pending_raw)
        _parse_record(pending, pending_raw)
    if output_exists:
        _recover_publish(output, complete_raw)
        _parse_record(output, complete_raw)
        if any(_target_state(repo, row) != "post" for row in ROWS):
            raise RefreshError("complete refresh record has a non-postimage target")
        if pending_exists:
            pending.unlink()
            _fsync(pending.parent)
        _print_inventory("reference-refresh-post-v20-hook-bootstrap-v2: PASS")
        return 0
    for row in ROWS:
        if _target_state(repo, row) == "pre":
            _replace_target(repo, row)
    _recover_publish(output, complete_raw)
    _parse_record(output, complete_raw)
    pending.unlink()
    _fsync(pending.parent)
    _print_inventory("reference-refresh-post-v20-hook-bootstrap-v2: PASS")
    return 0


def validate(args: argparse.Namespace) -> int:
    repo = Path(__file__).resolve().parents[2]
    _validate_v20_history(repo)
    _authority(
        repo,
        args.plan.resolve(),
        args.transcript.resolve(),
        args.seal.resolve(),
        args.record.resolve(),
    )
    output = args.output.resolve()
    expected = (
        repo
        / ".generated/state/rrcv2-convergence/verify/reference-refresh.post-v20-hook-bootstrap-v2.json"
    ).resolve()
    if output != expected:
        raise RefreshError("reference complete path differs")
    _parse_record(output, canonical_json_bytes(_projection("complete")))
    if any(_target_state(repo, row) != "post" for row in ROWS):
        raise RefreshError("reference validation found a non-postimage target")
    _print_inventory("reference-refresh-post-v20-hook-bootstrap-v2: VALID")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, function in (("apply", apply), ("validate", validate)):
        command = commands.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--transcript", type=Path, required=True)
        command.add_argument("--seal", type=Path, required=True)
        command.add_argument("--record", type=Path, required=True)
        if name == "apply":
            command.add_argument("--pending", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
        command.set_defaults(function=function)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    function: Callable[[argparse.Namespace], int] = args.function
    return function(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RefreshError, OSError, ValueError) as exc:
        print(
            f"reference-refresh-post-v20-hook-bootstrap-v2: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
