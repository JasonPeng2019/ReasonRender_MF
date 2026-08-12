#!/usr/bin/env python3
"""Confined receipt consumer for accepted ContextMesh RRCv2 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from rrc.attempts import AttemptRepository
from rrc.cell_journal import RootToolEventV1, SQLiteCellJournal
from rrc.contextmesh import parse_coding_assignment
from rrc.contract import canonical_json_bytes
from rrc.journal import SQLiteRRCRepository


class ResultReaderError(RuntimeError):
    """A receipt cannot be safely applied to its sealed target."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ResultReaderError("accepted result write was incomplete")
        view = view[written:]


def _open_parent(root: Path, relative: str) -> tuple[list[int], int, str]:
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or relative.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or PurePosixPath(relative).as_posix() != relative
    ):
        raise ResultReaderError("accepted artifact path is not canonical")
    try:
        metadata = os.lstat(root)
    except OSError as exc:
        raise ResultReaderError("sealed target root is unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ResultReaderError("sealed target root is not a real directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        for component in parts[:-1]:
            current = os.open(component, flags, dir_fd=current)
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise ResultReaderError("accepted artifact parent is not a directory")
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise ResultReaderError("accepted artifact parent is unavailable") from exc
    return descriptors, descriptors[-1], parts[-1]


def _read_leaf_identity(parent: int, leaf: str, *, cap: int) -> tuple[bytes, int, int, int] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(leaf, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ResultReaderError("accepted artifact target cannot be opened") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_size > cap:
            raise ResultReaderError("accepted artifact target is not bounded regular data")
        chunks: list[bytes] = []
        remaining = cap + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > cap or len(raw) != observed.st_size:
            raise ResultReaderError("accepted artifact target changed or exceeded its cap")
        # Reopen-by-name is not enough for a security decision: the name may
        # have been swapped after ``open``.  Bind the bytes we read back to the
        # directory entry that still exists at the end of the read.
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_dev != observed.st_dev
            or named.st_ino != observed.st_ino
        ):
            raise ResultReaderError("accepted artifact target changed during validation")
        return raw, stat.S_IMODE(observed.st_mode), observed.st_dev, observed.st_ino
    finally:
        os.close(descriptor)


def _read_leaf(parent: int, leaf: str, *, cap: int) -> tuple[bytes, int] | None:
    observed = _read_leaf_identity(parent, leaf, cap=cap)
    return None if observed is None else observed[:2]


def _apply_regular(
    parent: int,
    leaf: str,
    *,
    source: bytes,
    receipt: str,
    expected_sha256: str,
    expected_bytes: int,
    expected_mode: int,
) -> None:
    current = _read_leaf(parent, leaf, cap=1024 * 1024)
    if current is None:
        raise ResultReaderError("regular target disappeared")
    raw, mode = current
    if raw == source and mode == expected_mode:
        return
    if _sha(raw) != expected_sha256 or len(raw) != expected_bytes or mode != expected_mode:
        raise ResultReaderError("regular target preimage differs")
    temporary = f".rrcv2-{receipt[:24]}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    try:
        descriptor = os.open(temporary, flags, expected_mode, dir_fd=parent)
        created = True
    except FileExistsError:
        stale = _read_leaf(parent, temporary, cap=1024 * 1024)
        if stale != (source, expected_mode):
            raise ResultReaderError("accepted result temporary path conflicts") from None
    else:
        try:
            os.fchmod(descriptor, expected_mode)
            _write_all(descriptor, source)
            os.fsync(descriptor)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)
    try:
        # Recheck the preimage after the potentially long temp write.  A same-user
        # race must never be overwritten merely because it matched before writing.
        latest = _read_leaf(parent, leaf, cap=1024 * 1024)
        if latest == (source, expected_mode):
            os.unlink(temporary, dir_fd=parent)
            return
        if latest is None:
            raise ResultReaderError("regular target disappeared before commit")
        latest_raw, latest_mode = latest
        if (
            _sha(latest_raw) != expected_sha256
            or len(latest_raw) != expected_bytes
            or latest_mode != expected_mode
        ):
            raise ResultReaderError("regular target changed before commit")
        os.rename(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except BaseException:
        if created:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        raise


def _apply_absent(
    parent: int,
    leaf: str,
    *,
    source: bytes,
    receipt: str,
    source_sha256: str,
    expected_mode: int,
) -> None:
    if _sha(source) != source_sha256:
        raise ResultReaderError("accepted source hash differs")
    temporary = f".rrcv2-{receipt[:24]}.new"
    current = _read_leaf(parent, leaf, cap=1024 * 1024)
    if current is not None:
        raw, mode = current
        if _sha(raw) == source_sha256 and raw == source and mode == expected_mode:
            stale = _read_leaf(parent, temporary, cap=1024 * 1024)
            if stale == (source, expected_mode):
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            return
        raise ResultReaderError("absent target was raced or already differs")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary, flags, expected_mode, dir_fd=parent)
    except FileExistsError:
        stale = _read_leaf_identity(parent, temporary, cap=1024 * 1024)
        if stale is None or stale[:2] != (source, expected_mode):
            raise ResultReaderError("accepted result temporary path conflicts") from None
    except OSError as exc:
        raise ResultReaderError("greenfield target staging failed") from exc
    else:
        try:
            os.fchmod(descriptor, expected_mode)
            _write_all(descriptor, source)
            os.fsync(descriptor)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
            raise
        finally:
            os.close(descriptor)

    staged = _read_leaf_identity(parent, temporary, cap=1024 * 1024)
    if staged is None or staged[:2] != (source, expected_mode):
        raise ResultReaderError("greenfield target staging differs")
    try:
        # A hard link is the portable same-directory no-replace primitive.  It
        # either publishes this exact inode or leaves an existing target alone.
        os.link(
            temporary,
            leaf,
            src_dir_fd=parent,
            dst_dir_fd=parent,
            follow_symlinks=False,
        )
    except FileExistsError:
        installed = _read_leaf_identity(parent, leaf, cap=1024 * 1024)
        if installed is not None and installed[:2] == (source, expected_mode):
            os.unlink(temporary, dir_fd=parent)
            os.fsync(parent)
            return
        raise ResultReaderError("absent target was raced or already differs") from None
    except OSError as exc:
        raise ResultReaderError("greenfield target could not be atomically installed") from exc

    installed = _read_leaf_identity(parent, leaf, cap=1024 * 1024)
    if installed is None or installed[:2] != (source, expected_mode) or installed[2:] != staged[2:]:
        # Never unlink the final name here: if it differs, another same-user
        # actor owns the observed directory entry and we must not destroy it.
        raise ResultReaderError("greenfield target changed after installation")
    os.fsync(parent)
    os.unlink(temporary, dir_fd=parent)
    os.fsync(parent)


def apply_receipt(*, database: Path, attempt_id: str, receipt: str) -> dict[str, object]:
    with SQLiteRRCRepository(database) as repository:
        attempts = AttemptRepository(repository)
        record, payload = repository.load_receipt(attempt_id, receipt)
        registered = attempts.load_registered_input(attempt_id)
        assignment = parse_coding_assignment(registered.assignment)
        target = registered.task_envelope.target_preimage
        if (
            record.attempt_id != attempt_id
            or payload.attempt_id != attempt_id
            or payload.artifact_path != assignment.task.artifact_path
            or target.path != assignment.task.artifact_path
            or not isinstance(target.path, str)
            or not isinstance(target.mode, int)
            or isinstance(target.mode, bool)
        ):
            raise ResultReaderError("receipt, assignment, and target identity differ")
        source_text = repository.load_accepted_source(attempt_id, record.artifact_record_sha256)
        source = source_text.encode("utf-8", errors="strict")
        if _sha(source) != payload.source_sha256 or len(source) != payload.source_bytes:
            raise ResultReaderError("accepted source differs from its receipt")
        target_path = target.path
        target_mode = target.mode
        descriptors, parent, leaf = _open_parent(registered.target_root, target_path)
        try:
            if target.kind == "regular":
                if target.sha256 is None or target.bytes is None:
                    raise ResultReaderError("regular target preimage is incomplete")
                _apply_regular(
                    parent,
                    leaf,
                    source=source,
                    receipt=receipt,
                    expected_sha256=target.sha256,
                    expected_bytes=target.bytes,
                    expected_mode=target_mode,
                )
            elif target.kind == "absent":
                _apply_absent(
                    parent,
                    leaf,
                    source=source,
                    receipt=receipt,
                    source_sha256=payload.source_sha256,
                    expected_mode=target_mode,
                )
            else:
                raise ResultReaderError("target preimage kind cannot be applied")
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        result = {
            "artifact_path": payload.artifact_path,
            "attempt_id": attempt_id,
            "bytes": payload.source_bytes,
            "receipt": receipt,
            "sha256": payload.source_sha256,
            "v": 1,
        }
        cell_id = os.environ.get("RRCV2_CELL_ID")
        if cell_id:
            cells = SQLiteCellJournal(
                repository,
                authority_root=(
                    Path(value) if (value := os.environ.get("RRCV2_CELL_AUTHORITY_ROOT")) else None
                ),
            )
            root = cells.load_root_started(cell_id)
            input_raw = canonical_json_bytes({"attempt_id": attempt_id, "receipt": receipt, "v": 1})
            output_raw = canonical_json_bytes(result)
            cells.record_root_tool_event(
                cell_id=cell_id,
                event=RootToolEventV1(
                    "apply",
                    "apply-" + attempt_id,
                    _sha(input_raw),
                    _sha(output_raw),
                    output_raw,
                ),
                expected_generation=root.cell.generation,
            )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--attempt-id", required=True)
    apply.add_argument("--receipt", required=True)
    apply.add_argument(
        "--database",
        type=Path,
        default=(Path(value) if (value := os.environ.get("RRC_DEMO_DATABASE")) else None),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "apply" or args.database is None:
        raise ResultReaderError("RRC_DEMO_DATABASE is not configured")
    result = apply_receipt(
        database=args.database.absolute(), attempt_id=args.attempt_id, receipt=args.receipt
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"rrcv2 result reader: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
