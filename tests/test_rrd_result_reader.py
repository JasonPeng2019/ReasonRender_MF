from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from contextmesh.scripts.rrd_result_reader import (
    ResultReaderError,
    _apply_absent,
    _apply_regular,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )


def test_regular_apply_recovers_an_exact_fsynced_temporary(tmp_path: Path) -> None:
    before = b"before"
    after = b"after"
    target = tmp_path / "solution.py"
    target.write_bytes(before)
    target.chmod(0o644)
    receipt = "a" * 64
    temporary = tmp_path / f".rrcv2-{receipt[:24]}.tmp"
    temporary.write_bytes(after)
    temporary.chmod(0o644)
    parent = _directory(tmp_path)
    try:
        _apply_regular(
            parent,
            target.name,
            source=after,
            receipt=receipt,
            expected_sha256=_sha(before),
            expected_bytes=len(before),
            expected_mode=0o644,
        )
    finally:
        os.close(parent)
    assert target.read_bytes() == after
    assert not temporary.exists()


def test_regular_apply_rejects_conflicting_temporary_without_overwrite(tmp_path: Path) -> None:
    before = b"before"
    target = tmp_path / "solution.py"
    target.write_bytes(before)
    target.chmod(0o644)
    receipt = "b" * 64
    temporary = tmp_path / f".rrcv2-{receipt[:24]}.tmp"
    temporary.write_bytes(b"foreign")
    temporary.chmod(0o644)
    parent = _directory(tmp_path)
    try:
        with pytest.raises(ResultReaderError, match="temporary path conflicts"):
            _apply_regular(
                parent,
                target.name,
                source=b"after",
                receipt=receipt,
                expected_sha256=_sha(before),
                expected_bytes=len(before),
                expected_mode=0o644,
            )
    finally:
        os.close(parent)
    assert target.read_bytes() == before


@pytest.mark.parametrize("mask", [0o000, 0o022, 0o077])
def test_absent_apply_forces_mode_independent_of_umask(tmp_path: Path, mask: int) -> None:
    parent = _directory(tmp_path)
    previous = os.umask(mask)
    try:
        _apply_absent(
            parent,
            "solution.py",
            source=b"accepted",
            receipt="c" * 64,
            source_sha256=_sha(b"accepted"),
            expected_mode=0o644,
        )
    finally:
        os.umask(previous)
        os.close(parent)
    target = tmp_path / "solution.py"
    assert target.read_bytes() == b"accepted"
    assert target.stat().st_mode & 0o777 == 0o644


def test_absent_apply_never_replaces_a_post_stage_racer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = _directory(tmp_path)
    target = tmp_path / "solution.py"
    real_link = os.link

    def race_then_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        target.write_bytes(b"foreign")
        target.chmod(0o644)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", race_then_link)
    try:
        with pytest.raises(ResultReaderError, match="raced or already differs"):
            _apply_absent(
                parent,
                target.name,
                source=b"accepted",
                receipt="d" * 64,
                source_sha256=_sha(b"accepted"),
                expected_mode=0o644,
            )
    finally:
        os.close(parent)
    assert target.read_bytes() == b"foreign"


def test_absent_apply_retries_exact_published_target_and_cleans_stage(tmp_path: Path) -> None:
    source = b"accepted"
    receipt = "e" * 64
    target = tmp_path / "solution.py"
    target.write_bytes(source)
    target.chmod(0o644)
    temporary = tmp_path / f".rrcv2-{receipt[:24]}.new"
    temporary.write_bytes(source)
    temporary.chmod(0o644)
    parent = _directory(tmp_path)
    try:
        _apply_absent(
            parent,
            target.name,
            source=source,
            receipt=receipt,
            source_sha256=_sha(source),
            expected_mode=0o644,
        )
    finally:
        os.close(parent)
    assert target.read_bytes() == source
    assert not temporary.exists()
