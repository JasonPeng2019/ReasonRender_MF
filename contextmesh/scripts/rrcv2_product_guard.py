#!/usr/bin/env python3
"""One-shot credential-confined launcher for the RRCv2 product credibility smoke."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from rrc.contract import canonical_json_bytes
from rrc.product_runtime import product_authority_refs
from rrc.sandbox_capability import DOCKER_CONTEXT
from rrd_native_config import (
    SANDBOX_PROFILE,
    config_text,
    credential_profile_text,
    hooks_value,
    resolve_executable,
)
from rrd_native_config import (
    validate as validate_native_home,
)

FIXTURE_SHA256 = "483db5cdc34b2ab16d99dd578ca87981e4b82e3d6ea53550be841fa7eedaaf39"
EXPERIMENT_ID = "rrcv2-cli-smoke-v22"
PRODUCER_SHA256 = "7187db49a70bc28a5a5ebef777a439a21d0697d97418f26d5daf2a36b18768f3"
ROUND_TOKEN = "rrcv2-cli-smoke-7187db49a70bc28a5a5ebef777a439a2"
SESSION_PLAN_SHA256 = "595212576a81c72615795dd64f5d273e1121ec80622d3229fbe9cfa31d53ebfb"
SESSION_REVIEW_SHA256 = "1634bf2a3947851fa9238eaf63851e3377e0f666e658b7e3902e59b2f5e2cbe9"
SESSION_REVIEW_SUBJECT = "3af96ad6e8d265ea08ce16382ad25ee9d06a426783f5cb614491c0d24e3ba27f"
LAUNCH_MANIFEST_SHA256 = "4e26c75806aeba0a1864759b06a7bf391edbfff61caf3611d86ea7efabb14ac4"
GUARD_NORMALIZED_SHA256 = "404c15e3af50c00c46451c13cb111dc315349458a66a032254bb0c4d47108eea"
PREDECESSOR_MANIFEST_SHA256 = "29e756aeea869c781cfc42aaf2447c7485ddcbd9441ac48a6df4e9b055bb9550"
PRICE_AUTHORITY_SHA256 = "4a9e961131aee2a0c137c24fe35ddda31a12b9f8f6b394209bfa022a665b5cf6"
_MAX_AUTHORITY = 2_000_000
_ROUND = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_TIMEOUT = re.compile(r"(?:[1-9][0-9]{0,4}|[1-8][0-9]{5}|900000)\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_HOME_LOCK_SECONDS = 120.0
_STAGE_MATRIX = {
    "root": ["gpt-5.5", "medium", "priority"],
    "small": ["gpt-5.6-luna", "low", "priority"],
    "spec": ["gpt-5.5", "low", "priority"],
}
_TIMEOUT_MATRIX = {
    "bridge_seconds": 240,
    "finisher_start_seconds": 15,
    "model_adapter_seconds": 300,
    "outer_milliseconds": 900000,
    "planner_seconds": 90,
    "root_seconds": 720,
    "stable_home_lock_seconds": int(_STABLE_HOME_LOCK_SECONDS),
    "visibility_seconds": 10,
}
_ENVIRONMENT_PROJECTION = {
    "RRCV2_DOCKER_BIN": str(Path("/usr/local/bin/docker").resolve()),
    "RRD_CODEX_MODEL": "gpt-5.5",
    "RRD_CODEX_REASONING": "medium",
    "RRD_ENABLE_CONTEXTMESH": "1",
    "RRD_ENABLE_RRC": "1",
    "RRD_EXTERNAL_SANDBOX": "1",
    "RRD_MEMORY_BACKEND": "sqlite",
    "RRD_SUMMARY_MODE": "deterministic",
    "RRD_WORKER_MODEL": "gpt-5.6-luna",
    "RRD_WORKER_REASONING": "low",
}
_SELF_AUTHORITY_NAMES = (
    "PRODUCER_SHA256",
    "ROUND_TOKEN",
    "LAUNCH_MANIFEST_SHA256",
    "GUARD_NORMALIZED_SHA256",
    "PRICE_AUTHORITY_SHA256",
)
_MAIN_TRAMPOLINE = """\
import os,signal,sys
gate=int(sys.argv[1]); request=sys.argv[2]; argv=sys.argv[3:]
signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTERM,signal.SIGINT,signal.SIGHUP})
released=os.read(gate,1); os.close(gate)
if released != b'1' or os.path.lexists(request): raise SystemExit(125)
os.execvpe(argv[0],argv,os.environ)
"""
_SMOKE_BOOTSTRAP = """\
import os,runpy,sys
repo,script,mode,*args=sys.argv[1:]
if mode not in {"import","main"}: raise SystemExit(125)
expected=os.path.join(repo,"contextmesh","scripts","rrcv2_product_smoke.py")
if repo != os.path.realpath(repo) or script != expected: raise SystemExit(125)
if mode == "import" and args: raise SystemExit(125)
if mode == "main" and (len(args) != 4 or args[::2] != ["--fixture","--round-root"]): raise SystemExit(125)
sys.path[:0]=[repo,os.path.dirname(script)]
if mode == "import": runpy.run_path(script,run_name="rrcv2_import_probe")
else:
    sys.argv=[script,*args]
    runpy.run_path(script,run_name="__main__")
"""


class GuardError(RuntimeError):
    """The product guard cannot establish the reviewed launch boundary."""


@dataclass(frozen=True)
class FileAuthority:
    device: int
    inode: int
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class DirectoryAuthority:
    device: int
    inode: int
    mode: int
    uid: int


@dataclass
class StableHomeLease:
    stream: BinaryIO
    authorities: dict[str, FileAuthority]
    environment: dict[str, str]

    def close(self) -> None:
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


@dataclass
class ExperimentLease:
    """Exclusive lifetime reservation for the single reviewed producer root."""

    stream: BinaryIO

    def close(self) -> None:
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


@dataclass
class PreparedLaunch:
    user_home: Path
    native_home: Path
    codex_bin: Path
    docker_bin: Path
    lease: StableHomeLease
    producer_directory: DirectoryAuthority
    round_directory: DirectoryAuthority
    environment: dict[str, str]
    profile: Path
    experiment_lease: ExperimentLease


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _normalized_guard_bytes(raw: bytes) -> bytes:
    """Remove the four deliberately cyclic launch constants from self identity."""

    normalized = raw
    for name in _SELF_AUTHORITY_NAMES:
        pattern = re.compile(rb"(?m)^" + name.encode() + rb' = "[^"\n]+"$')
        normalized, count = pattern.subn(name.encode() + b' = "<normalized>"', normalized)
        if count != 1:
            raise GuardError(f"product guard self-authority field differs: {name}")
    return normalized


def _validate_self() -> None:
    raw, _opened = _read_regular(Path(__file__).resolve(strict=True), mode=0o755)
    if _sha(_normalized_guard_bytes(raw)) != GUARD_NORMALIZED_SHA256:
        raise GuardError("product guard normalized source differs")


def _executable_version(name: str, path: Path) -> str:
    if name == "sandbox-exec":
        return "not-reported; executable-sha256-authoritative"
    suffix = ["--version"]
    completed = subprocess.run(
        [str(path), *suffix],
        env={
            "HOME": str(Path.home()),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
        },
        capture_output=True,
        timeout=10,
        check=False,
    )
    lines = (completed.stdout + completed.stderr).decode("utf-8", errors="strict").splitlines()
    output = next((line.strip() for line in lines if line.strip()), "")
    if completed.returncode != 0 or not output:
        raise GuardError(f"cannot establish {name} executable version")
    return output


def _read_regular(
    path: Path,
    *,
    mode: int,
    cap: int = _MAX_AUTHORITY,
    allowed_links: frozenset[int] = frozenset({1}),
) -> tuple[bytes, os.stat_result]:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_nlink not in allowed_links
        or before.st_uid != os.getuid()
        or before.st_size > cap
    ):
        raise GuardError(f"authority is not a bounded owned mode-{mode:04o} regular file: {path}")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, cap + 1)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or len(raw) != opened.st_size
        or len(raw) > cap
    ):
        raise GuardError(f"authority changed while opening: {path}")
    return raw, opened


def _authority(path: Path) -> FileAuthority:
    raw, opened = _read_regular(path, mode=0o600)
    return FileAuthority(
        opened.st_dev,
        opened.st_ino,
        stat.S_IMODE(opened.st_mode),
        len(raw),
        _sha(raw),
    )


def _owned_directory(path: Path, *, create: bool = False) -> DirectoryAuthority:
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise GuardError(f"product directory is not owned mode-0700: {path}")
    return DirectoryAuthority(
        metadata.st_dev, metadata.st_ino, stat.S_IMODE(metadata.st_mode), metadata.st_uid
    )


def _reopen_directory(path: Path, authority: DirectoryAuthority) -> None:
    if _owned_directory(path) != authority:
        raise GuardError(f"product directory identity changed: {path}")


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short authority write")
        view = view[written:]


def _create_exclusive(path: Path, raw: bytes, mode: int = 0o600) -> FileAuthority:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, raw)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return FileAuthority(
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        _sha(raw),
    )


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_expected_replace(directory: Path, name: str, expected: bytes) -> None:
    final = directory / name
    temporary = directory / f".rrcv2-product-{name}.next"
    old_identity: tuple[int, int] | None = None
    try:
        old, metadata = _read_regular(final, mode=0o600)
    except FileNotFoundError:
        old = None
    else:
        old_identity = (metadata.st_dev, metadata.st_ino)
        if old == expected and not temporary.exists():
            return
    if temporary.exists() or temporary.is_symlink():
        temporary_raw, _ = _read_regular(temporary, mode=0o600, cap=max(1, len(expected)))
        if temporary_raw != expected:
            raise GuardError(f"foreign stable-home transaction temporary: {temporary}")
    else:
        _create_exclusive(temporary, expected)
    if old_identity is None:
        try:
            os.lstat(final)
        except FileNotFoundError:
            pass
        else:
            raise GuardError(f"stable-home final appeared during reconciliation: {final}")
    else:
        current = os.lstat(final)
        if (current.st_dev, current.st_ino) != old_identity:
            raise GuardError(f"stable-home final changed during reconciliation: {final}")
    os.replace(temporary, final)
    _fsync_dir(directory)
    reopened, _ = _read_regular(final, mode=0o600, cap=max(1, len(expected)))
    if reopened != expected:
        raise GuardError(f"stable-home reconciliation differed: {final}")


def _validate_home(home: Path) -> None:
    metadata = os.lstat(home)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise GuardError("stable CODEX_HOME must be an owned mode-0700 directory")


def _acquire_stable_home_lock(descriptor: int) -> None:
    deadline = time.monotonic() + _STABLE_HOME_LOCK_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise GuardError("stable-home lock acquisition timed out") from None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _acquire_experiment_lease(parent: Path) -> ExperimentLease:
    """Fail fast when another V22 invocation owns the one-shot namespace."""

    _owned_directory(parent)
    path = parent / ".rrcv2-v22-one-shot.lock"
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
            raise GuardError("V22 experiment lock is not owned regular data")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise GuardError("the V22 one-shot namespace is already reserved") from None
        return ExperimentLease(cast(BinaryIO, os.fdopen(descriptor, "r+b", buffering=0)))
    except BaseException:
        os.close(descriptor)
        raise


def prepare_stable_home(*, repo: Path, home: Path, user_home: Path) -> StableHomeLease:
    """Reconcile and hold the exact stable authenticated Codex home."""

    _validate_home(home)
    lock_path = home / ".rrcv2-product-home.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid():
        os.close(descriptor)
        raise GuardError("stable-home lock is not owned regular data")
    stream = cast(BinaryIO, os.fdopen(descriptor, "r+b", buffering=0))
    try:
        _acquire_stable_home_lock(stream.fileno())
        python_bin = Path(sys.executable).resolve(strict=True)
        hook_path = (repo / "contextmesh/scripts/rrd_codex_hook.py").resolve(strict=True)
        for executable in (python_bin, hook_path):
            metadata = executable.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                raise GuardError(f"product executable is mutable: {executable}")
        expected = {
            "config.toml": config_text(
                model="gpt-5.5",
                reasoning="medium",
                worker_model="gpt-5.6-luna",
                worker_reasoning="low",
            ).encode(),
            "hooks.json": (
                json.dumps(
                    hooks_value(python_bin=python_bin, hook_path=hook_path),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            SANDBOX_PROFILE: credential_profile_text(
                user_home=user_home,
                repo_root=repo,
            ).encode(),
        }
        for name, raw in expected.items():
            _safe_expected_replace(home, name, raw)
        auth = home / "auth.json"
        if auth.exists() or auth.is_symlink():
            raise GuardError("stable product home must not contain auth.json")
        authorities = {name: _authority(home / name) for name in expected}
        return StableHomeLease(stream, authorities, {})
    except Exception:
        stream.close()
        raise


def verify_stable_home(home: Path, lease: StableHomeLease) -> None:
    auth = home / "auth.json"
    if auth.exists() or auth.is_symlink():
        raise GuardError("auth.json appeared during product execution")
    observed = {name: _authority(home / name) for name in lease.authorities}
    if observed != lease.authorities:
        raise GuardError("immutable stable-home bytes changed during product execution")


def load_fixture(path: Path) -> dict[str, object]:
    raw, _ = _read_regular(path, mode=0o644, cap=64 * 1024)
    if _sha(raw) != FIXTURE_SHA256:
        raise GuardError("credibility fixture manifest hash differs")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("credibility fixture is not JSON") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise GuardError("credibility fixture is not canonical JSON")
    if set(value) != {"cases", "kind", "v"} or value.get("v") != 1:
        raise GuardError("credibility fixture schema differs")
    cases = value.get("cases")
    if not isinstance(cases, list) or [
        row.get("kind") for row in cases if isinstance(row, dict)
    ] != [
        "miss",
        "hit",
        "near",
    ]:
        raise GuardError("credibility fixture case order differs")
    expected_paths = {"manifest.json", "miss", "hit", "near"}
    for row in cases:
        if not isinstance(row, dict):
            raise GuardError("credibility fixture case is not an object")
        files = row.get("files")
        if not isinstance(files, dict) or set(files) != {
            "starter.py",
            "public_tests.json",
            "oracle_tests.json",
        }:
            raise GuardError("credibility fixture file inventory differs")
        for item in files.values():
            if not isinstance(item, dict) or set(item) != {"bytes", "path", "sha256"}:
                raise GuardError("credibility fixture file row differs")
            relative = item["path"]
            if not isinstance(relative, str):
                raise GuardError("credibility fixture path differs")
            target = path.parent / relative
            nested, _ = _read_regular(target, mode=0o644, cap=64 * 1024)
            if len(nested) != item["bytes"] or _sha(nested) != item["sha256"]:
                raise GuardError(f"credibility fixture nested hash differs: {relative}")
            expected_paths.add(relative)
    observed = {item.relative_to(path.parent).as_posix() for item in path.parent.rglob("*")}
    if observed != expected_paths:
        raise GuardError("credibility fixture has an extra or missing entry")
    return cast(dict[str, object], value)


def _producer_preimage() -> bytes:
    return canonical_json_bytes(
        {
            "experiment_id": EXPERIMENT_ID,
            "fixture_manifest_sha256": FIXTURE_SHA256,
            "launch_manifest_sha256": LAUNCH_MANIFEST_SHA256,
            "predecessor_manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
            "price_authority_sha256": PRICE_AUTHORITY_SHA256,
            "session_plan_sha256": SESSION_PLAN_SHA256,
            "session_review_sha256": SESSION_REVIEW_SHA256,
            "v": 22,
        }
    )


def producer_value() -> bytes:
    if _sha(_producer_preimage()) != PRODUCER_SHA256:
        raise GuardError("producer preimage constant differs")
    return canonical_json_bytes(
        {
            "experiment_id": EXPERIMENT_ID,
            "fixture_manifest_sha256": FIXTURE_SHA256,
            "launch_manifest_sha256": LAUNCH_MANIFEST_SHA256,
            "predecessor_manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
            "price_authority_sha256": PRICE_AUTHORITY_SHA256,
            "session_plan_sha256": SESSION_PLAN_SHA256,
            "session_review_sha256": SESSION_REVIEW_SHA256,
            "producer_sha256": PRODUCER_SHA256,
            "round_token": ROUND_TOKEN,
            "v": 22,
        }
    )


def publish_producer(root: Path, *, round_authority: DirectoryAuthority) -> bool:
    """Publish once; return True only for the invocation eligible to launch."""

    if not root.parent.exists():
        root.parent.mkdir(parents=True, mode=0o700)
        os.chmod(root.parent, 0o700)
    _owned_directory(root.parent)
    root_authority = _owned_directory(root, create=True)
    final = root / "producer.json"
    temporary = root / ".producer.v22.tmp"
    preparation = root / "preparation.json"
    expected = producer_value()
    initial_inventory = {path.name for path in root.iterdir()}
    if "preparation.json" not in initial_inventory:
        raise GuardError("producer preparation authority is missing")
    fresh_inventory = {"preparation.json", "round"}
    temporary_inventory = fresh_inventory | {".producer.v22.tmp"}
    linked_inventory = fresh_inventory | {".producer.v22.tmp", "producer.json"}
    final_inventory = fresh_inventory | {"producer.json"}
    terminal_inventory = final_inventory | {"terminal.json"}
    if frozenset(initial_inventory) not in {
        frozenset(fresh_inventory),
        frozenset(temporary_inventory),
        frozenset(linked_inventory),
        frozenset(final_inventory),
        frozenset(terminal_inventory),
    }:
        raise GuardError("producer root inventory differs")
    _reopen_directory(root / "round", round_authority)
    preparation_exists = True
    preparation_metadata: os.stat_result | None = None
    temporary_authority: FileAuthority | None = None
    if preparation_exists:
        preparation_raw, preparation_metadata = _read_regular(
            preparation, mode=0o600, cap=len(_preparation_value())
        )
        if preparation_raw != _preparation_value():
            raise GuardError("producer preparation authority differs")

    def validate_preparation(expected_inventory: set[str]) -> None:
        _reopen_directory(root, root_authority)
        _reopen_directory(root / "round", round_authority)
        if {path.name for path in root.iterdir()} != expected_inventory:
            raise GuardError("producer publication inventory differs")
        if expected_inventory == linked_inventory:
            final_raw, final_metadata = _read_regular(
                final, mode=0o600, cap=len(expected), allowed_links=frozenset({2})
            )
            temporary_raw, temporary_metadata = _read_regular(
                temporary, mode=0o600, cap=len(expected), allowed_links=frozenset({2})
            )
            final_authority = FileAuthority(
                final_metadata.st_dev,
                final_metadata.st_ino,
                stat.S_IMODE(final_metadata.st_mode),
                final_metadata.st_size,
                _sha(final_raw),
            )
            current_temporary_authority = FileAuthority(
                temporary_metadata.st_dev,
                temporary_metadata.st_ino,
                stat.S_IMODE(temporary_metadata.st_mode),
                temporary_metadata.st_size,
                _sha(temporary_raw),
            )
            if (
                temporary_authority is None
                or final_raw != expected
                or temporary_raw != expected
                or final_authority != temporary_authority
                or current_temporary_authority != temporary_authority
            ):
                raise GuardError("producer linked publication authority differs")
        if preparation_exists:
            try:
                preparation_raw, final_metadata = _read_regular(
                    preparation, mode=0o600, cap=len(_preparation_value())
                )
            except OSError as exc:
                raise GuardError("producer preparation disappeared during publication") from exc
            if (
                preparation_metadata is None
                or preparation_raw != _preparation_value()
                or (final_metadata.st_dev, final_metadata.st_ino)
                != (preparation_metadata.st_dev, preparation_metadata.st_ino)
            ):
                raise GuardError("producer preparation changed during publication")

    final_exists = final.exists() or final.is_symlink()
    temp_exists = temporary.exists() or temporary.is_symlink()
    if temp_exists:
        temp_raw, temp_meta = _read_regular(
            temporary, mode=0o600, cap=len(expected), allowed_links=frozenset({1, 2})
        )
        if temp_raw != expected:
            raise GuardError("producer temporary differs")
        temporary_authority = FileAuthority(
            temp_meta.st_dev,
            temp_meta.st_ino,
            stat.S_IMODE(temp_meta.st_mode),
            temp_meta.st_size,
            _sha(temp_raw),
        )
        if not final_exists:
            os.link(temporary, final, follow_symlinks=False)
            _fsync_dir(root)
            final_exists = True
        else:
            final_raw, final_meta = _read_regular(
                final, mode=0o600, cap=len(expected), allowed_links=frozenset({2})
            )
            if final_raw != expected or (temp_meta.st_dev, temp_meta.st_ino) != (
                final_meta.st_dev,
                final_meta.st_ino,
            ):
                raise GuardError("producer final/temporary pair differs")
        validate_preparation(linked_inventory)
        return False
    if final_exists:
        final_raw, _ = _read_regular(final, mode=0o600, cap=len(expected))
        if final_raw != expected:
            raise GuardError("existing producer record differs")
        validate_preparation(initial_inventory)
        return False
    temporary_authority = _create_exclusive(temporary, expected)
    os.link(temporary, final, follow_symlinks=False)
    _fsync_dir(root)
    final_raw, final_meta = _read_regular(
        final, mode=0o600, cap=len(expected), allowed_links=frozenset({2})
    )
    temporary_raw, temporary_meta = _read_regular(
        temporary, mode=0o600, cap=len(expected), allowed_links=frozenset({2})
    )
    if (
        final_raw != expected
        or temporary_raw != expected
        or FileAuthority(
            final_meta.st_dev,
            final_meta.st_ino,
            stat.S_IMODE(final_meta.st_mode),
            final_meta.st_size,
            _sha(final_raw),
        )
        != temporary_authority
        or FileAuthority(
            temporary_meta.st_dev,
            temporary_meta.st_ino,
            stat.S_IMODE(temporary_meta.st_mode),
            temporary_meta.st_size,
            _sha(temporary_raw),
        )
        != temporary_authority
    ):
        raise GuardError("published producer record differs")
    validate_preparation(linked_inventory)
    return True


def _producer_is_published(root: Path) -> bool:
    final = root / "producer.json"
    if not final.exists() and not final.is_symlink():
        return False
    raw, _metadata = _read_regular(
        final,
        mode=0o600,
        cap=len(producer_value()),
        allowed_links=frozenset({1, 2}),
    )
    if raw != producer_value():
        raise GuardError("published producer authority differs")
    return True


def _preparation_value() -> bytes:
    return canonical_json_bytes(
        {"kind": "rrcv2_v22_preparation", "producer_sha256": PRODUCER_SHA256, "v": 1}
    )


def _reconcile_abandoned_preparation(root: Path) -> None:
    """Remove only a marker-bound, unpublished preparation from a dead owner."""

    if _producer_is_published(root):
        raise GuardError("the one reviewed producer attempt is already consumed")
    producer_directory = _owned_directory(root)
    marker, _ = _read_regular(root / "preparation.json", mode=0o600, cap=len(_preparation_value()))
    if marker != _preparation_value():
        raise GuardError("abandoned V22 preparation authority differs")
    _reopen_directory(root, producer_directory)
    round_root = root / "round"
    if round_root.exists() or round_root.is_symlink():
        round_directory = _owned_directory(round_root)
        _reopen_directory(round_root, round_directory)
    elif set(path.name for path in root.iterdir()) != {"preparation.json"}:
        raise GuardError("partial V22 preparation inventory differs")
    shutil.rmtree(root, ignore_errors=False)


def _reconcile_unpublished_root(
    root: Path,
    producer_directory: DirectoryAuthority,
    round_directory: DirectoryAuthority,
) -> bool:
    """Preserve a published one-shot, or erase an exact zero-provider preparation."""

    if _producer_is_published(root):
        return True
    _reopen_directory(root, producer_directory)
    _reopen_directory(root / "round", round_directory)
    shutil.rmtree(root, ignore_errors=False)
    return False


def _canonical_object(path: Path, *, expected_sha256: str) -> dict[str, object]:
    raw, _metadata = _read_regular(path, mode=0o600, cap=16 * 1024 * 1024)
    if _sha(raw) != expected_sha256:
        raise GuardError(f"sealed authority hash differs: {path}")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"sealed authority is not JSON: {path}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise GuardError(f"sealed authority is noncanonical: {path}")
    return cast(dict[str, object], value)


def _validate_predecessor_manifest(repo: Path) -> None:
    path = repo / ".generated/state/rrcv2-convergence/verify/v22-predecessor-tree-manifest.v1.json"
    value = _canonical_object(path, expected_sha256=PREDECESSOR_MANIFEST_SHA256)
    rows = value.get("entries")
    if (
        set(value) != {"entries", "kind", "v"}
        or value.get("kind") != "rrcv2_v22_predecessor_tree_manifest"
        or value.get("v") != 1
        or not isinstance(rows, list)
    ):
        raise GuardError("predecessor manifest schema differs")
    expected: set[tuple[str, str, str]] = set()
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            raise GuardError("predecessor manifest row is invalid")
        experiment = raw_row.get("experiment_id")
        producer = raw_row.get("producer")
        relative = raw_row.get("path")
        kind = raw_row.get("type")
        mode = raw_row.get("mode")
        if (
            experiment not in {"rrcv2-cli-smoke-v19", "rrcv2-cli-smoke-v20", "rrcv2-cli-smoke-v21"}
            or not isinstance(producer, str)
            or _HEX_64.fullmatch(producer) is None
            or not isinstance(relative, str)
            or not isinstance(kind, str)
            or isinstance(mode, bool)
            or not isinstance(mode, int)
        ):
            raise GuardError("predecessor manifest identity differs")
        root = repo / ".generated/state/rrcv2-convergence/verify/cli-smoke" / experiment / producer
        target = root if relative == "." else root / relative
        if target.absolute() != target or ".." in Path(relative).parts:
            raise GuardError("predecessor manifest path escapes")
        metadata = os.lstat(target)
        if stat.S_IMODE(metadata.st_mode) != mode:
            raise GuardError(f"predecessor mode differs: {target}")
        if kind == "directory":
            if not stat.S_ISDIR(metadata.st_mode) or set(raw_row) != {
                "experiment_id",
                "mode",
                "path",
                "producer",
                "type",
            }:
                raise GuardError(f"predecessor directory differs: {target}")
        elif kind == "regular":
            raw, opened = _read_regular(target, mode=mode, cap=16 * 1024 * 1024)
            if (
                set(raw_row)
                != {"bytes", "experiment_id", "mode", "path", "producer", "sha256", "type"}
                or raw_row.get("bytes") != opened.st_size
                or raw_row.get("sha256") != _sha(raw)
            ):
                raise GuardError(f"predecessor file differs: {target}")
        elif kind == "symlink":
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or set(raw_row) != {"experiment_id", "mode", "path", "producer", "target", "type"}
                or raw_row.get("target") != os.readlink(target)
            ):
                raise GuardError(f"predecessor symlink differs: {target}")
        else:
            raise GuardError("predecessor manifest type differs")
        expected.add((experiment, producer, relative))
    observed: set[tuple[str, str, str]] = set()
    for experiment in ("rrcv2-cli-smoke-v19", "rrcv2-cli-smoke-v20", "rrcv2-cli-smoke-v21"):
        parent = repo / ".generated/state/rrcv2-convergence/verify/cli-smoke" / experiment
        for root in sorted(parent.iterdir()):
            if not root.is_dir():
                raise GuardError("predecessor experiment contains a foreign entry")
            observed.add((experiment, root.name, "."))
            observed.update(
                (experiment, root.name, str(child.relative_to(root))) for child in root.rglob("*")
            )
    if observed != expected:
        raise GuardError("predecessor evidence topology differs")


def _current_diff_subject(repo: Path) -> str:
    """Reproduce the workspace worktree-review subject without importing it."""

    diff = subprocess.run(
        ["/usr/bin/git", "diff", "--binary", "HEAD", "--", "."],
        cwd=repo,
        capture_output=True,
        timeout=30,
        check=False,
    )
    others = subprocess.run(
        ["/usr/bin/git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if diff.returncode != 0 or others.returncode != 0:
        raise GuardError("cannot compute current worktree review subject")
    try:
        snapshot = diff.stdout.decode("utf-8", errors="strict")
        names = others.stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise GuardError("worktree review subject is not UTF-8 addressable") from exc
    runtime = {".agent-workspace", ".agents", ".claude", ".codex", ".generated", ".vscode"}
    for relative in names:
        path = repo / relative
        if any(part in runtime for part in Path(relative).parts) or not path.exists():
            continue
        if path.is_symlink():
            payload = path.readlink().as_posix()
        else:
            raw = path.read_bytes()
            try:
                payload = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                payload = raw.hex()
        snapshot += f"UNTRACKED {relative}\n{payload}\n"
    return _sha(snapshot.encode("utf-8"))


def _validate_fresh_diff_review(repo: Path) -> None:
    record_path = repo / ".generated/state/reviews/diff-worktree.toml"
    raw, _opened = _read_regular(record_path, mode=0o600)
    try:
        value = tomllib.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardError("V22 diff review record is malformed") from exc
    expected_keys = {
        "audience_entry_id",
        "kind",
        "mode",
        "origin",
        "persona",
        "recorded_at",
        "repo_root",
        "scope",
        "subject_hash",
        "transcript_hash",
        "verdict",
        "workspace_runtime_root",
        "workspace_session",
    }
    if (
        set(value) != expected_keys
        or value.get("kind") != "diff"
        or value.get("scope") != "worktree"
        or value.get("verdict") != "SHIP"
        or value.get("origin") != "forked"
        or value.get("mode") != "unleashed"
        or value.get("repo_root") != str(repo)
        or value.get("workspace_session") != ""
        or value.get("workspace_runtime_root") != ""
        or value.get("subject_hash") != _current_diff_subject(repo)
        or _HEX_64.fullmatch(str(value.get("transcript_hash", ""))) is None
    ):
        raise GuardError("V22 worktree diff review is absent, stale, or not SHIP")
    transcript_hash = cast(str, value["transcript_hash"])
    review_root = repo / ".generated/state/rrcv2-convergence/reviews"
    found = False
    for candidate in review_root.glob("*.txt"):
        try:
            transcript, _metadata = _read_regular(candidate, mode=0o600, cap=2_000_000)
        except (GuardError, OSError):
            continue
        if _sha(transcript) == transcript_hash and b"VERDICT: SHIP" in transcript:
            found = True
            break
    if not found:
        raise GuardError("V22 SHIP diff-review transcript is missing")


def _validate_launch_manifest(repo: Path) -> None:
    path = repo / ".generated/state/rrcv2-convergence/verify/v22-launch-manifest.v1.json"
    value = _canonical_object(path, expected_sha256=LAUNCH_MANIFEST_SHA256)
    files = value.get("files")
    executables = value.get("executables")
    if (
        set(value)
        != {
            "argv",
            "codex_path",
            "environment_projection",
            "executables",
            "files",
            "guard_normalized_sha256",
            "kind",
            "stage_matrix",
            "timeout_matrix",
            "v",
        }
        or value.get("kind") != "rrcv2_v22_launch_manifest"
        or value.get("v") != 1
        or value.get("guard_normalized_sha256") != GUARD_NORMALIZED_SHA256
        or value.get("argv")
        != [
            "bash",
            "contextmesh/scripts/rrd_demo_tui.sh",
            "smoke",
            "--fixture",
            "tests/fixtures/rrcv2_cli_smoke/manifest.json",
            "--round-id",
            "<ROUND_TOKEN>",
            "--timeout-ms",
            "900000",
        ]
        or value.get("stage_matrix") != _STAGE_MATRIX
        or value.get("timeout_matrix") != _TIMEOUT_MATRIX
        or value.get("environment_projection") != _ENVIRONMENT_PROJECTION
        or not isinstance(files, list)
        or not isinstance(executables, list)
    ):
        raise GuardError("V22 launch manifest schema differs")
    executable_paths = {
        "bash": Path("/bin/bash").resolve(strict=True),
        "codex": resolve_executable(str(Path.home() / ".local/bin/codex")),
        "docker": Path("/usr/local/bin/docker").resolve(strict=True),
        "git": Path("/usr/bin/git").resolve(strict=True),
        "python": Path(sys.executable).resolve(strict=True),
        "sandbox-exec": Path("/usr/bin/sandbox-exec").resolve(strict=True),
        "uv": Path("/usr/local/bin/uv").resolve(strict=True),
    }
    executable_seen: set[str] = set()
    for row in executables:
        if (
            not isinstance(row, dict)
            or set(row) != {"bytes", "mode", "name", "path", "sha256", "version"}
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("path"), str)
            or isinstance(row.get("bytes"), bool)
            or not isinstance(row.get("bytes"), int)
            or isinstance(row.get("mode"), bool)
            or not isinstance(row.get("mode"), int)
            or not isinstance(row.get("sha256"), str)
            or not isinstance(row.get("version"), str)
        ):
            raise GuardError("V22 launch executable row differs")
        name = cast(str, row["name"])
        expected_path = executable_paths.get(name)
        if name in executable_seen or expected_path is None or row["path"] != str(expected_path):
            raise GuardError("V22 launch executable identity differs")
        executable_seen.add(name)
        before = os.lstat(expected_path)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_mode & 0o022
            or before.st_size > 512 * 1024 * 1024
            or stat.S_IMODE(before.st_mode) != row["mode"]
            or before.st_size != row["bytes"]
        ):
            raise GuardError("V22 launch executable metadata differs")
        descriptor = os.open(
            expected_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            opened = os.fstat(descriptor)
            chunks: list[bytes] = []
            remaining = 512 * 1024 * 1024 + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(descriptor)
        raw = b"".join(chunks)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or len(raw) != opened.st_size
            or _sha(raw) != row["sha256"]
            or _executable_version(name, expected_path) != row["version"]
        ):
            raise GuardError("V22 launch executable bytes differ")
    if executable_seen != set(executable_paths):
        raise GuardError("V22 launch executable inventory differs")
    codex_path = value.get("codex_path")
    release = executable_paths["codex"].parent.parent / "codex-path"
    release_metadata = os.lstat(release)
    rg = release / "rg"
    rg_raw, rg_metadata = _read_regular(rg, mode=0o755, cap=16 * 1024 * 1024)
    expected_codex_path = {
        "directory": str(release),
        "directory_mode": stat.S_IMODE(release_metadata.st_mode),
        "entries": [
            {
                "bytes": len(rg_raw),
                "mode": stat.S_IMODE(rg_metadata.st_mode),
                "path": str(rg),
                "sha256": _sha(rg_raw),
            }
        ],
    }
    if (
        not stat.S_ISDIR(release_metadata.st_mode)
        or release_metadata.st_uid != os.getuid()
        or sorted(path.name for path in release.iterdir()) != ["rg"]
        or codex_path != expected_codex_path
    ):
        raise GuardError("V22 Codex PATH release authority differs")
    seen: set[str] = set()
    for row in files:
        if (
            not isinstance(row, dict)
            or set(row) != {"mode", "path", "sha256"}
            or not isinstance(row.get("path"), str)
            or not isinstance(row.get("mode"), int)
            or not isinstance(row.get("sha256"), str)
        ):
            raise GuardError("V22 launch manifest file row differs")
        relative = cast(str, row["path"])
        if relative in seen or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise GuardError("V22 launch manifest path differs")
        seen.add(relative)
        target = repo / relative
        raw, _opened = _read_regular(target, mode=cast(int, row["mode"]), cap=16 * 1024 * 1024)
        if _sha(raw) != row["sha256"]:
            raise GuardError(f"V22 launch source differs: {relative}")
    required = {
        "contextmesh/scripts/rrd_codex_hook.py",
        "contextmesh/scripts/rrd_demo_tui.sh",
        "contextmesh/scripts/rrd_native_config.py",
        "contextmesh/scripts/rrd_result_reader.py",
        "contextmesh/scripts/rrcv2_product_smoke.py",
        "contextmesh/scripts/rrcv2_product_cell.py",
        "contextmesh/scripts/rrc_finisher.py",
        "contextmesh/scripts/rrcv2_v22_analyzer.py",
        "tests/test_rrc_product_runtime.py",
        "contextmesh/.codex-rrd-native/config.toml",
        "contextmesh/.codex-rrd-native/credential-deny.sb",
        "contextmesh/.codex-rrd-native/hooks.json",
        "pyproject.toml",
        "uv.lock",
        ".generated/sessions/unleashed-4/task/PLAN.md",
        ".generated/sessions/unleashed-4/state/reviews/plan.toml",
        ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.v1.json",
        ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.md",
        ".generated/state/rrcv2-convergence/verify/v22-predecessor-tree-manifest.v1.json",
        ".generated/state/rrcv2-convergence/verify/v22-codex-hook-path-probe.v1.json",
    }
    required.update(
        path.relative_to(repo).as_posix() for path in (repo / "rrc").rglob("*.py") if path.is_file()
    )
    required.update(
        path.relative_to(repo).as_posix()
        for path in (repo / "tests/fixtures/rrcv2_cli_smoke").rglob("*")
        if path.is_file()
    )
    if seen != required:
        raise GuardError("V22 launch source inventory differs")
    _validate_path_probe(repo, executable_paths["codex"], expected_codex_path)


def _validate_path_probe(repo: Path, codex_bin: Path, codex_path: Mapping[str, object]) -> None:
    path = repo / ".generated/state/rrcv2-convergence/verify/v22-codex-hook-path-probe.v1.json"
    raw, _metadata = _read_regular(path, mode=0o600, cap=2_000_000)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("V22 Codex PATH probe is malformed") from exc
    codex_raw, codex_metadata = _read_regular(codex_bin, mode=0o755, cap=512 * 1024 * 1024)
    provider = value.get("provider") if isinstance(value, dict) else None
    events = value.get("events") if isinstance(value, dict) else None
    release_rows = codex_path.get("entries")
    expected_release = cast(list[dict[str, object]], release_rows)[0]
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) != raw
        or set(value) != {"base_path", "codex", "events", "kind", "provider", "release_tool", "v"}
        or value.get("kind") != "rrcv2_v22_codex_hook_path_probe"
        or value.get("v") != 1
        or value.get("base_path") != "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        or value.get("codex")
        != {
            "bytes": codex_metadata.st_size,
            "path": str(codex_bin),
            "sha256": _sha(codex_raw),
            "version": "codex-cli 0.147.0",
        }
        or value.get("release_tool")
        != {
            "bytes": expected_release["bytes"],
            "directory": codex_path["directory"],
            "mode": expected_release["mode"],
            "path": expected_release["path"],
            "sha256": expected_release["sha256"],
        }
        or not isinstance(provider, dict)
        or provider.get("all_requests_loopback") is not True
        or not isinstance(provider.get("endpoint"), str)
        or not cast(str, provider["endpoint"]).startswith("http://127.0.0.1:")
        or not isinstance(provider.get("request_count"), int)
        or cast(int, provider["request_count"]) <= 0
        or not isinstance(provider.get("request_body_sha256s"), list)
        or len(cast(list[object], provider["request_body_sha256s"])) != provider["request_count"]
        or not isinstance(events, list)
        or not events
    ):
        raise GuardError("V22 Codex PATH probe authority differs")
    for event in events:
        if (
            not isinstance(event, dict)
            or event.get("normalized_path") != value["base_path"]
            or not isinstance(event.get("observed_path"), str)
            or "/codex-path:" not in cast(str, event["observed_path"])
            or not isinstance(event.get("filesystem_metadata"), list)
            or [
                row.get("mode")
                for row in cast(list[dict[str, object]], event["filesystem_metadata"])
            ]
            != ["0o700", "0o755", "0o700", "0o755", "0o755", "0o755"]
            or any(
                row.get("is_symlink") is not False
                for row in cast(list[dict[str, object]], event["filesystem_metadata"])
            )
        ):
            raise GuardError("V22 Codex PATH probe event differs")


def _validate_prepublication_verification(repo: Path) -> None:
    """Reopen post-review zero-provider receipts immediately before publication."""

    path = (
        repo / ".generated/state/rrcv2-convergence/verify/v22-prepublication-verification.v1.json"
    )
    raw, _metadata = _read_regular(path, mode=0o600, cap=2_000_000)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("V22 prepublication verification is malformed") from exc
    review = repo / ".generated/state/reviews/diff-worktree.toml"
    review_raw, _ = _read_regular(review, mode=0o600)
    try:
        review_value = tomllib.loads(review_raw.decode("utf-8", errors="strict"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise GuardError("V22 prepublication diff review is malformed") from exc
    sources = value.get("sources") if isinstance(value, dict) else None
    commands = value.get("commands") if isinstance(value, dict) else None
    required_sources = {
        "contextmesh/scripts/rrcv2_product_guard.py": _sha(
            _read_regular(Path(__file__).resolve(strict=True), mode=0o755)[0]
        ),
        "contextmesh/scripts/rrd_codex_hook.py": _sha(
            _read_regular(repo / "contextmesh/scripts/rrd_codex_hook.py", mode=0o755)[0]
        ),
        "contextmesh/scripts/rrcv2_v22_analyzer.py": _sha(
            _read_regular(repo / "contextmesh/scripts/rrcv2_v22_analyzer.py", mode=0o755)[0]
        ),
        "pyproject.toml": _sha(_read_regular(repo / "pyproject.toml", mode=0o644)[0]),
    }
    command_specs = _prepublication_command_specs(repo)
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) != raw
        or set(value)
        != {
            "authorities",
            "commands",
            "diff_review",
            "kind",
            "producer_sha256",
            "sources",
            "v",
        }
        or value.get("kind") != "rrcv2_v22_prepublication_verification"
        or value.get("v") != 1
        or value.get("producer_sha256") != PRODUCER_SHA256
        or value.get("sources") != required_sources
        or value.get("authorities")
        != {
            "launch_manifest_sha256": LAUNCH_MANIFEST_SHA256,
            "path_probe_sha256": _sha(
                _read_regular(
                    repo
                    / ".generated/state/rrcv2-convergence/verify/v22-codex-hook-path-probe.v1.json",
                    mode=0o600,
                )[0]
            ),
            "plan_review_sha256": SESSION_REVIEW_SHA256,
            "plan_sha256": SESSION_PLAN_SHA256,
            "predecessor_manifest_sha256": PREDECESSOR_MANIFEST_SHA256,
        }
        or value.get("diff_review")
        != {
            "record_sha256": _sha(review_raw),
            "subject_hash": review_value.get("subject_hash"),
            "transcript_hash": review_value.get("transcript_hash"),
        }
        or review_value.get("subject_hash") != _current_diff_subject(repo)
        or not isinstance(sources, dict)
        or not isinstance(commands, list)
        or {row.get("name") for row in commands if isinstance(row, dict)} != set(command_specs)
    ):
        raise GuardError("V22 prepublication verification authority differs")
    _validate_prepublication_commands(repo, commands, command_specs=command_specs)


def _prepublication_command_specs(
    repo: Path,
) -> dict[str, tuple[list[str], dict[str, str], tuple[bytes, ...]]]:
    capability = {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "RRC_CAPABILITY_MODE": "validate-sealed",
        "RRD_REQUIRED_CODEX_VERSION": "codex-cli 0.147.0",
    }
    model_bearing = {
        "CODEX_HOME": str(repo / "contextmesh/.codex-rrd-native"),
        "RRD_VERIFY_MODEL_BEARING": "1",
    }
    verify_guard = "contextmesh/scripts/rrd_verify_guard.sh"
    guarded_pytest = [
        verify_guard,
        "--",
        "env",
        *(f"{key}={value}" for key, value in sorted(capability.items())),
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    pytest_prefix = ["uv", "run", "--locked", "pytest", "-q", "-p", "no:cacheprovider"]
    return {
        "authority": (
            [
                *pytest_prefix,
                "tests/test_rrcv2_product_guard.py::test_reviewed_fixture_and_producer_constants_reopen_exactly",
                "tests/test_rrcv2_product_guard.py::test_v22_predecessor_and_launch_authorities_reopen_exactly",
            ],
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            (b"2 passed",),
        ),
        "collection": (
            [
                "uv",
                "run",
                "--locked",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
            ],
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            (b"819 tests collected",),
        ),
        "installed_fake_provider": (
            [
                *guarded_pytest,
                "-m",
                "installed_fake_provider and not sandbox_real and not paid_live",
            ],
            model_bearing,
            (b"22 passed", b"797 deselected"),
        ),
        "ordinary": (
            [
                *pytest_prefix,
                "-m",
                "not sandbox_real and not installed_fake_provider and not paid_live",
            ],
            {
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "RRC_CAPABILITY_MODE": "validate-sealed",
            },
            (b"756 passed", b"63 deselected"),
        ),
        "ruff_check": (
            [
                "uv",
                "run",
                "--locked",
                "ruff",
                "check",
                "rrc",
                "contextmesh/scripts",
                "contextmesh/bench",
                "tests",
            ],
            {},
            (b"All checks passed!",),
        ),
        "ruff_format": (
            [
                "uv",
                "run",
                "--locked",
                "ruff",
                "format",
                "--check",
                "rrc",
                "contextmesh/scripts",
                "contextmesh/bench",
                "tests",
            ],
            {},
            (b"files already formatted",),
        ),
        "sandbox_real": (
            [
                *guarded_pytest,
                "-m",
                "sandbox_real and not installed_fake_provider and not paid_live",
            ],
            model_bearing,
            (b"40 passed", b"779 deselected"),
        ),
        "shellcheck": (
            [
                "/bin/bash",
                "-c",
                "shellcheck contextmesh/*.sh contextmesh/scripts/*.sh",
            ],
            {},
            (),
        ),
        "typecheck": (
            [
                "uv",
                "run",
                "--locked",
                "pyright",
                "rrc",
                "contextmesh/scripts",
                "contextmesh/bench",
                "tests",
            ],
            {},
            (b"0 errors, 0 warnings, 0 informations",),
        ),
    }


def _validate_prepublication_commands(
    repo: Path,
    commands: list[object],
    *,
    command_specs: Mapping[str, tuple[list[str], dict[str, str], tuple[bytes, ...]]] | None = None,
) -> None:
    specs = _prepublication_command_specs(repo) if command_specs is None else command_specs
    seen: set[str] = set()
    for row in commands:
        name = row.get("name") if isinstance(row, dict) else None
        spec = specs.get(cast(str, name)) if isinstance(name, str) else None
        output_path = row.get("output_path") if isinstance(row, dict) else None
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "argv",
                "environment",
                "exit_code",
                "name",
                "output_bytes",
                "output_path",
                "output_sha256",
            }
            or spec is None
            or name in seen
            or row.get("exit_code") != 0
            or row.get("argv") != spec[0]
            or row.get("environment") != spec[1]
            or not isinstance(output_path, str)
            or output_path
            != f".generated/state/rrcv2-convergence/verify/v22-command-receipts/{name}.log"
            or Path(output_path).is_absolute()
            or ".." in Path(output_path).parts
            or isinstance(row.get("output_bytes"), bool)
            or not isinstance(row.get("output_bytes"), int)
            or cast(int, row["output_bytes"]) < 0
            or cast(int, row["output_bytes"]) > 16 * 1024 * 1024
            or _HEX_64.fullmatch(str(row.get("output_sha256", ""))) is None
        ):
            raise GuardError("V22 prepublication command receipt differs")
        output, _ = _read_regular(repo / output_path, mode=0o600, cap=16 * 1024 * 1024)
        if (
            len(output) != row["output_bytes"]
            or _sha(output) != row["output_sha256"]
            or any(fragment not in output for fragment in spec[2])
            or (name == "shellcheck" and output)
        ):
            raise GuardError("V22 prepublication command output differs")
        seen.add(cast(str, name))
    if seen != set(specs):
        raise GuardError("V22 prepublication command inventory differs")


def product_environment(
    *,
    repo: Path,
    native_home: Path,
    user_home: Path,
    codex_bin: Path,
    uv_bin: Path,
    docker_bin: Path | None = None,
    round_root: Path,
    producer_root: Path,
    nonce: str,
) -> dict[str, str]:
    if docker_bin is None:
        docker_bin = next(
            (
                path.resolve(strict=True)
                for path in (Path("/usr/local/bin/docker"), Path("/usr/bin/docker"))
                if path.is_file()
            ),
            None,
        )
    if docker_bin is None:
        raise GuardError("Docker is unavailable for the product environment")
    cell = round_root / "controller"
    environment = {
        "HOME": str(user_home),
        "CODEX_HOME": str(native_home),
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TERM": "dumb",
        "TMPDIR": str(round_root / "tmp"),
        "RRD_CODEX_BIN": str(codex_bin),
        "RRD_CODEX_MODEL": "gpt-5.5",
        "RRD_CODEX_REASONING": "medium",
        "RRD_WORKER_MODEL": "gpt-5.6-luna",
        "RRD_WORKER_REASONING": "low",
        "RRD_ENABLE_RRC": "1",
        "RRD_ENABLE_CONTEXTMESH": "1",
        "RRD_EXTERNAL_SANDBOX": "1",
        "RRD_REPO_ROOT": str(repo),
        "RRD_MEMORY_BACKEND": "sqlite",
        "RRD_SUMMARY_MODE": "deterministic",
        "RRD_TARGET_ROOT": str(cell / "target"),
        "RRD_SEED_MANIFEST": str(cell / "seed-manifest.json"),
        "RRD_HOOK_EVENTS": str(cell / "hook-events.jsonl"),
        "RRD_RAW_RESULTS": str(cell / "raw-results"),
        "RRC_PLANNER_CODEX_HOME": str(native_home),
        "RRC_DEMO_UV_BIN": str(uv_bin),
        "RRC_DEMO_ROUND": "rrd-sqlite-" + ROUND_TOKEN,
        "RRC_DEMO_MODE": "warm",
        "RRC_DEMO_DATABASE": str(round_root / "rrcv2.sqlite3"),
        "RRC_DEMO_LOCK": str(cell / "plan-spec.lock"),
        "RRC_DEMO_EVENTS": str(cell / "rrc-events.jsonl"),
        "RRC_DEMO_MODEL_EVENTS": str(cell / "rrc-model-events.jsonl"),
        "RRC_STRONG_MODEL": "gpt-5.5",
        "RRC_REQUIRE_EFFECTIVE_MODEL": "1",
        "RRC_PLANNER_TIMEOUT": "90",
        "RRC_LOCK_TIMEOUT": "120",
        "RRC_VISIBILITY_TIMEOUT": "10",
        "RRC_BRIDGE_TIMEOUT": "240",
        "RRCV2_ATTEMPTS_ROOT": str(round_root / "rrcv2-attempts"),
        "RRCV2_DOCKER_BIN": str(docker_bin),
        "RRCV2_OWNER_SCOPE": "cm-" + _sha(ROUND_TOKEN.encode())[:32],
        "RRCV2_ROUTE_ID": "rrcv2-coding-v1",
        "RRCV2_CELL_ID": "rrcv2-" + ROUND_TOKEN + "-controller",
        "RRCV2_CELL_AUTHORITY_ROOT": str(round_root / "rrcv2-cell-authority"),
        "RRCV2_PRODUCT_TASK_ENVELOPE": str(cell / "task-envelope.v1.json"),
        "RRCV2_ROOT_PROMPT_SHA256": "0" * 64,
        "RRCV2_ROOT_SENTINEL": "rrcv2-root-" + nonce,
        "RRCV2_PARENT_HISTORY_SENTINEL": "rrcv2-parent-" + nonce,
        "RRCV2_PRODUCT_SMOKE": "1",
        "RRCV2_SMOKE_RUN_NONCE": nonce,
        "RRCV2_SMOKE_CANCELLATION_ROOT": str(round_root / "cancellation"),
        "RRCV2_SMOKE_CANCELLATION_REQUEST": str(round_root / "cancellation/request"),
        "RRCV2_SMOKE_CHILD_REGISTRY": str(round_root / "cancellation/children"),
        "RRCV2_SMOKE_PRODUCER_ROOT": str(producer_root),
    }
    return environment


def _credential_targets(user_home: Path, repo: Path) -> tuple[Path, ...]:
    return (
        user_home / ".ssh",
        user_home / ".aws",
        user_home / ".azure",
        user_home / ".config/gcloud",
        user_home / ".config/gh",
        user_home / ".gnupg",
        user_home / ".kube",
        user_home / ".codex/sessions",
        user_home / ".codex/logs",
        user_home / ".codex/auth.json",
        user_home / ".codex/config.toml",
        user_home / ".codex/history.jsonl",
        user_home / ".docker/config.json",
        user_home / ".git-credentials",
        user_home / ".netrc",
        user_home / ".npmrc",
        user_home / ".pypirc",
        repo / ".env",
        repo / ".env.local",
        repo / "contextmesh/.env",
        repo / "contextmesh/.env.local",
    )


_CREDENTIAL_PROBE = r"""
import json,os,sys
payload=json.loads(sys.argv[1]); observed=[]
for row in payload['targets']:
    if row['kind']=='absent':
        observed.append({'kind':'absent','path':row['path']}); continue
    try:
        if row['kind']=='regular':
            fd=os.open(row['path'],os.O_RDONLY); os.read(fd,1); os.close(fd)
        else:
            with os.scandir(row['path']) as entries: next(entries,None)
    except PermissionError:
        observed.append({'kind':'denied_'+row['kind'],'path':row['path']})
    else: raise SystemExit(41)
for path in payload['write_targets']:
    try:
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600); os.close(fd)
    except PermissionError: pass
    else: raise SystemExit(42)
fd=os.open(payload['scratch'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
os.write(fd,b'control'); os.close(fd); os.unlink(payload['scratch'])
print(json.dumps({'targets':observed,'v':1},sort_keys=True,separators=(',',':')),end='')
"""


def _probe_credential_profile(
    *, profile: Path, user_home: Path, repo: Path, native_home: Path, round_root: Path
) -> None:
    rows: list[dict[str, str]] = []
    canary = user_home / ".codex/config.toml"
    for path in _credential_targets(user_home, repo):
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            rows.append({"kind": "absent", "path": str(path)})
            continue
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise GuardError(f"credential probe target is aliased or foreign: {path}")
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_size > _MAX_AUTHORITY:
                raise GuardError(f"credential probe target is oversized: {path}")
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                os.read(descriptor, 1)
            finally:
                os.close(descriptor)
            kind = "regular"
        elif stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                for index, _entry in enumerate(entries):
                    if index >= 4096:
                        raise GuardError(f"credential directory inventory exceeded its cap: {path}")
            kind = "directory"
        else:
            raise GuardError(f"credential probe target is special: {path}")
        rows.append({"kind": kind, "path": str(path)})
    canary_rows = [row for row in rows if row["path"] == str(canary)]
    if canary_rows != [{"kind": "regular", "path": str(canary)}]:
        raise GuardError("canonical readable credential canary is unavailable")
    payload = {
        "scratch": str(round_root / "tmp/credential-write-control"),
        "targets": rows,
        "write_targets": [
            str(native_home / name)
            for name in ("auth.json", "config.toml", "hooks.json", SANDBOX_PROFILE)
        ],
    }
    completed = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-f",
            str(profile),
            str(Path(sys.executable).resolve(strict=True)),
            "-I",
            "-c",
            _CREDENTIAL_PROBE,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        env={
            "HOME": str(user_home),
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TMPDIR": str(round_root / "tmp"),
        },
        check=False,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > 64 * 1024
        or len(completed.stderr) > 4096
    ):
        raise GuardError("credential sandbox kernel probe failed")
    try:
        observed = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("credential sandbox kernel probe output is invalid") from exc
    expected = {
        "targets": [
            {
                "kind": "absent" if row["kind"] == "absent" else "denied_" + row["kind"],
                "path": row["path"],
            }
            for row in rows
        ],
        "v": 1,
    }
    if observed != expected:
        raise GuardError("credential sandbox kernel probe inventory differs")
    _create_exclusive(round_root / "credential-probe.json", canonical_json_bytes(observed))


def _merged_profile(native_home: Path, round_root: Path) -> Path:
    raw, _ = _read_regular(native_home / SANDBOX_PROFILE, mode=0o600)
    denied = tuple(
        native_home / name for name in ("auth.json", "config.toml", "hooks.json", SANDBOX_PROFILE)
    )
    merged = raw + b"".join(
        f"(deny file-write* (literal {json.dumps(str(path))}))\n".encode() for path in denied
    )
    output = round_root / "product.sb"
    _create_exclusive(output, merged)
    return output


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 5.0
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise GuardError("product process group survived KILL") from exc


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise GuardError("registered product group ownership is not provable") from exc
    return True


def _registry_row(path: Path, *, nonce: str) -> dict[str, object]:
    raw, _ = _read_regular(path, mode=0o600, cap=4096)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError("registered product child row is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"argv_sha256", "nonce", "pgid", "pid", "returncode", "state", "v"}
        or value.get("v") != 1
        or value.get("nonce") != nonce
        or value.get("state") not in {"live", "terminal"}
        or _HEX_64.fullmatch(str(value.get("argv_sha256"))) is None
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pid"), int)
        or value.get("pid") != value.get("pgid")
        or int(value["pid"]) <= 1
    ):
        raise GuardError("registered product child row differs")
    return cast(dict[str, object], value)


def _create_cancellation_request(path: Path) -> None:
    try:
        _create_exclusive(path, canonical_json_bytes({"cancelled": True, "v": 1}))
        _fsync_dir(path.parent)
    except FileExistsError:
        raw, _ = _read_regular(path, mode=0o600, cap=128)
        if raw != canonical_json_bytes({"cancelled": True, "v": 1}):
            raise GuardError("cancellation request differs") from None


def _docker_cleanup(environment: Mapping[str, str], docker_bin: Path) -> None:
    nonce = environment["RRCV2_SMOKE_RUN_NONCE"]
    if environment.get("RRCV2_DOCKER_BIN") != str(docker_bin):
        raise GuardError("Docker cleanup authority differs")
    base = [str(docker_bin), "--context", DOCKER_CONTEXT]
    docker_env = {
        "HOME": environment["HOME"],
        "PATH": environment["PATH"],
        "LANG": environment["LANG"],
        "LC_ALL": environment["LC_ALL"],
    }

    def command(*parts: str) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                [*base, *parts],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                env=docker_env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GuardError("Docker smoke cleanup command failed") from exc
        if len(result.stdout) > 65_536 or len(result.stderr) > 65_536:
            raise GuardError("Docker smoke cleanup output exceeded its cap")
        return result

    label = f"label=org.contextmesh.rrcv2-smoke={nonce}"
    for kind, list_parts, remove_prefix in (
        ("container", ("ps", "-aq", "--filter", label), ("rm", "-f")),
        ("volume", ("volume", "ls", "-q", "--filter", label), ("volume", "rm", "-f")),
    ):
        listed = command(*list_parts)
        if listed.returncode != 0:
            raise GuardError(f"Docker {kind} inventory failed")
        names = tuple(
            line for line in listed.stdout.decode("ascii", errors="strict").splitlines() if line
        )
        if any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", name) is None for name in names):
            raise GuardError(f"Docker {kind} inventory differs")
        for name in names:
            removed = command(*remove_prefix, name)
            if removed.returncode != 0:
                raise GuardError(f"Docker {kind} cleanup failed")
        proved = command(*list_parts)
        if proved.returncode != 0 or proved.stdout.strip():
            raise GuardError(f"Docker {kind} cleanup could not be proven")


def _cleanup_product(
    *,
    process: subprocess.Popen[bytes] | None,
    environment: Mapping[str, str] | None,
    cancellation_root: Path,
    docker_bin: Path | None = None,
) -> None:
    request = cancellation_root / "request"
    _create_cancellation_request(request)
    groups: set[int] = set()
    if process is not None:
        groups.add(process.pid)
    children = cancellation_root / "children"
    if environment is not None:
        nonce = environment["RRCV2_SMOKE_RUN_NONCE"]
        for path in children.iterdir():
            if path.is_symlink() or not path.name.startswith(f"child-{nonce}-"):
                raise GuardError("child registry contains a foreign entry")
            pgid = _registry_row(path, nonce=nonce)["pgid"]
            assert isinstance(pgid, int) and not isinstance(pgid, bool)
            groups.add(pgid)
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while any(_group_alive(pgid) for pgid in groups) and time.monotonic() < deadline:
        if process is not None:
            process.poll()
        time.sleep(0.05)
    for pgid in groups:
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if process is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise GuardError("main product group leader survived cleanup") from exc
    deadline = time.monotonic() + 5
    while any(_group_alive(pgid) for pgid in groups) and time.monotonic() < deadline:
        time.sleep(0.05)
    if any(_group_alive(pgid) for pgid in groups):
        raise GuardError("a registered product process group survived cleanup")
    if environment is not None:
        if docker_bin is None:
            raise GuardError("Docker cleanup executable authority is missing")
        _docker_cleanup(environment, docker_bin)


def _terminal_receipt(root: Path, *, status: str, returncode: int) -> None:
    raw = canonical_json_bytes(
        {"producer_sha256": PRODUCER_SHA256, "returncode": returncode, "status": status, "v": 1}
    )
    _create_exclusive(root / "terminal.json", raw)
    _fsync_dir(root)


def _launch_main(
    command: Sequence[str], *, environment: Mapping[str, str], cwd: Path
) -> subprocess.Popen[bytes]:
    blocked = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    gate_read, gate_write = os.pipe()
    os.set_inheritable(gate_read, True)
    process: subprocess.Popen[bytes] | None = None
    try:
        launched = [
            str(Path(sys.executable).resolve(strict=True)),
            "-c",
            _MAIN_TRAMPOLINE,
            str(gate_read),
            environment["RRCV2_SMOKE_CANCELLATION_REQUEST"],
            *command,
        ]
        process = subprocess.Popen(
            launched,
            env=dict(environment),
            cwd=cwd,
            start_new_session=True,
            pass_fds=(gate_read,),
        )
        os.close(gate_read)
        gate_read = -1
        registry = Path(environment["RRCV2_SMOKE_CHILD_REGISTRY"])
        path = registry / f"child-{environment['RRCV2_SMOKE_RUN_NONCE']}-{process.pid}-main.json"
        _create_exclusive(
            path,
            canonical_json_bytes(
                {
                    "argv_sha256": _sha(canonical_json_bytes(list(command))),
                    "nonce": environment["RRCV2_SMOKE_RUN_NONCE"],
                    "pgid": process.pid,
                    "pid": process.pid,
                    "returncode": None,
                    "state": "live",
                    "v": 1,
                }
            ),
        )
        _fsync_dir(registry)
        os.write(gate_write, b"1")
        os.close(gate_write)
        gate_write = -1
        return process
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        raise
    finally:
        for descriptor in (gate_read, gate_write):
            if descriptor >= 0:
                os.close(descriptor)
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)


def _mark_main_terminal(process: subprocess.Popen[bytes], environment: Mapping[str, str]) -> None:
    registry = Path(environment["RRCV2_SMOKE_CHILD_REGISTRY"])
    path = registry / (f"child-{environment['RRCV2_SMOKE_RUN_NONCE']}-{process.pid}-main.json")
    value = _registry_row(path, nonce=environment["RRCV2_SMOKE_RUN_NONCE"])
    if value["state"] != "live" or process.returncode is None:
        raise GuardError("main product registry row cannot become terminal")
    value["state"] = "terminal"
    value["returncode"] = process.returncode
    temporary = path.with_name("." + path.name + ".terminal")
    _create_exclusive(temporary, canonical_json_bytes(value))
    os.replace(temporary, path)
    _fsync_dir(registry)


def _smoke_inner_command(*, repo: Path, script: Path, mode: str, args: Sequence[str]) -> list[str]:
    """Build the sole isolated Python entrypoint for the reviewed smoke runner."""

    return [
        str(Path(sys.executable).resolve(strict=True)),
        "-I",
        "-c",
        _SMOKE_BOOTSTRAP,
        str(repo),
        str(script),
        mode,
        *args,
    ]


def _smoke_command(
    *,
    profile: Path,
    repo: Path,
    script: Path,
    fixture: Path,
    round_root: Path,
) -> list[str]:
    """Build the single outer-Seatbelt product argv."""

    return [
        "/usr/bin/sandbox-exec",
        "-f",
        str(profile),
        *_smoke_inner_command(
            repo=repo,
            script=script,
            mode="main",
            args=("--fixture", str(fixture), "--round-root", str(round_root)),
        ),
    ]


def _preflight_before_publish(*, repo: Path, fixture: Path, producer_root: Path) -> PreparedLaunch:
    """Prepare the exact launch tree and prove host boundaries before publication."""

    producer_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(producer_root.parent, 0o700)
    experiment_lease = _acquire_experiment_lease(producer_root.parent)
    producer_directory: DirectoryAuthority | None = None
    round_directory: DirectoryAuthority | None = None
    staging_root: Path | None = None
    staging_directory: DirectoryAuthority | None = None
    try:
        if producer_root.exists() or producer_root.is_symlink():
            _reconcile_abandoned_preparation(producer_root)
        staging_root = producer_root.with_name(
            f".{producer_root.name}.preparing.{os.getpid()}.{os.urandom(8).hex()}"
        )
        staging_root.mkdir(mode=0o700)
        staging_directory = _owned_directory(staging_root)
        _create_exclusive(staging_root / "preparation.json", _preparation_value())
        _fsync_dir(staging_root)
        if producer_root.exists() or producer_root.is_symlink():
            raise GuardError("the reviewed producer root appeared during preparation")
        os.rename(staging_root, producer_root)
        _fsync_dir(producer_root.parent)
        staging_root = None
        staging_directory = None
        producer_directory = _owned_directory(producer_root)
        round_root = producer_root / "round"
        round_root.mkdir(mode=0o700)
        round_directory = _owned_directory(round_root)
        for directory in (
            round_root / "tmp",
            round_root / "cancellation",
            round_root / "cancellation/children",
        ):
            directory.mkdir(parents=True, mode=0o700)
    except BaseException:
        try:
            if producer_directory is not None:
                _reopen_directory(producer_root, producer_directory)
                if round_directory is not None:
                    _reopen_directory(producer_root / "round", round_directory)
                shutil.rmtree(producer_root, ignore_errors=False)
            elif staging_root is not None and staging_directory is not None:
                _reopen_directory(staging_root, staging_directory)
                shutil.rmtree(staging_root, ignore_errors=False)
        finally:
            experiment_lease.close()
        raise

    lease: StableHomeLease | None = None
    try:
        user_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        native_home = (repo / "contextmesh/.codex-rrd-native").resolve(strict=True)
        codex_bin = resolve_executable(str(user_home / ".local/bin/codex"))
        uv_bin = Path("/usr/local/bin/uv").resolve(strict=True)
        docker_bin = next(
            (
                path.resolve(strict=True)
                for path in (Path("/usr/local/bin/docker"), Path("/usr/bin/docker"))
                if path.is_file()
            ),
            None,
        )
        if docker_bin is None:
            raise GuardError("Docker is unavailable for the V22 verifier preflight")
        lease = prepare_stable_home(repo=repo, home=native_home, user_home=user_home)
        validate_native_home(codex_bin=codex_bin, home=native_home, require_login=True)
        nonce = os.urandom(16).hex()
        environment = product_environment(
            repo=repo,
            native_home=native_home,
            user_home=user_home,
            codex_bin=codex_bin,
            uv_bin=uv_bin,
            docker_bin=docker_bin,
            round_root=round_root,
            producer_root=producer_root,
            nonce=nonce,
        )
        prefix_map = {
            name: value
            for name, value in environment.items()
            if name.startswith(("RRD_", "RRC_", "RRCV2_"))
        }
        observed_projection = {name: environment.get(name) for name in _ENVIRONMENT_PROJECTION}
        if observed_projection != _ENVIRONMENT_PROJECTION:
            raise GuardError("V22 runtime environment differs from the sealed projection")
        prefix_raw = canonical_json_bytes(prefix_map)
        _create_exclusive(
            round_root / "product-environment.json",
            canonical_json_bytes(
                {"prefix_map": prefix_map, "prefix_map_sha256": _sha(prefix_raw), "v": 1}
            ),
        )
        profile = _merged_profile(native_home, round_root)
        _probe_credential_profile(
            profile=profile,
            user_home=user_home,
            repo=repo,
            native_home=native_home,
            round_root=round_root,
        )
        smoke_script = (repo / "contextmesh/scripts/rrcv2_product_smoke.py").resolve(strict=True)
        hook_script = (repo / "contextmesh/scripts/rrd_codex_hook.py").resolve(strict=True)
        _read_regular(smoke_script, mode=0o755)
        _read_regular(hook_script, mode=0o755)
        imported = subprocess.run(
            [
                "/usr/bin/sandbox-exec",
                "-f",
                str(profile),
                *_smoke_inner_command(repo=repo, script=smoke_script, mode="import", args=()),
            ],
            cwd=repo,
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if imported.returncode != 0:
            raise GuardError("V22 isolated smoke import preflight failed")
        foreign = subprocess.run(
            [str(Path(sys.executable).resolve(strict=True)), str(hook_script), "--help"],
            cwd=round_root / "tmp",
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if foreign.returncode != 0 or b"ModuleNotFoundError" in foreign.stderr:
            raise GuardError("V22 foreign-cwd hook import preflight failed")
        product_authority_refs(repo)
        permit_probe = subprocess.run(
            [
                str(Path(sys.executable).resolve(strict=True)),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_rrc_product_runtime.py::test_real_root_binding_authorizes_controller_and_native_worker_before_launch",
            ],
            cwd=repo,
            env={
                "HOME": str(user_home),
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            },
            capture_output=True,
            timeout=60,
            check=False,
        )
        if permit_probe.returncode != 0 or b"1 passed" not in permit_probe.stdout:
            raise GuardError("V22 product-permit authorization preflight failed")
        docker_health = subprocess.run(
            [
                str(docker_bin),
                "--context",
                DOCKER_CONTEXT,
                "info",
                "--format",
                "{{.ServerVersion}}",
            ],
            env={
                "HOME": str(user_home),
                "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
            },
            capture_output=True,
            timeout=30,
            check=False,
        )
        if docker_health.returncode != 0 or not docker_health.stdout.strip():
            raise GuardError("Docker verifier health preflight failed")
        load_fixture(fixture)
        verify_stable_home(native_home, lease)
        _reopen_directory(producer_root, producer_directory)
        _reopen_directory(round_root, round_directory)
        return PreparedLaunch(
            user_home,
            native_home,
            codex_bin,
            docker_bin,
            lease,
            producer_directory,
            round_directory,
            environment,
            profile,
            experiment_lease,
        )
    except BaseException:
        try:
            if lease is not None:
                lease.close()
            _reconcile_unpublished_root(producer_root, producer_directory, round_directory)
        finally:
            experiment_lease.close()
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--timeout-ms", required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise GuardError("product credibility smoke requires macOS sandbox-exec")
    if args.round_id != ROUND_TOKEN or _ROUND.fullmatch(args.round_id) is None:
        raise GuardError("round ID is not the reviewed producer token")
    timeout_text = str(args.timeout_ms)
    if timeout_text != "900000":
        raise GuardError("timeout differs from the reviewed V22 launch")
    repo = Path(__file__).resolve().parents[2]
    fixture = args.fixture.resolve(strict=True)
    expected_fixture = (repo / "tests/fixtures/rrcv2_cli_smoke/manifest.json").resolve(strict=True)
    if fixture != expected_fixture:
        raise GuardError("fixture path is not the reviewed authority")
    load_fixture(fixture)
    _validate_self()
    session = repo / ".generated/sessions/unleashed-4"
    session_plan = session / "task/PLAN.md"
    session_review = session / "state/reviews/plan.toml"
    if _sha(session_plan.read_bytes()) != SESSION_PLAN_SHA256:
        raise GuardError("V22 session PLAN bytes differ")
    if _sha(session_review.read_bytes()) != SESSION_REVIEW_SHA256:
        raise GuardError("V22 plan-review record bytes differ")
    review_text = session_review.read_text(encoding="utf-8")
    if (
        'verdict = "SHIP"' not in review_text
        or f'subject_hash = "{SESSION_REVIEW_SUBJECT}"' not in review_text
        or 'workspace_session = "unleashed-4"' not in review_text
    ):
        raise GuardError("V22 plan-review record semantics differ")
    _validate_launch_manifest(repo)
    _validate_predecessor_manifest(repo)
    _validate_fresh_diff_review(repo)
    price = _canonical_object(
        repo / ".generated/state/rrcv2-convergence/economic/openai-pricing-20260812.v1.json",
        expected_sha256=PRICE_AUTHORITY_SHA256,
    )
    if price.get("kind") != "rrcv2_v21_official_price_authority":
        raise GuardError("V22 price authority differs")
    producer_root = (
        repo
        / ".generated/state/rrcv2-convergence/verify/cli-smoke"
        / EXPERIMENT_ID
        / PRODUCER_SHA256
    )
    round_root = producer_root / "round"
    producer_directory: DirectoryAuthority | None = None
    round_directory: DirectoryAuthority | None = None
    process: subprocess.Popen[bytes] | None = None
    environment: dict[str, str] | None = None
    lease: StableHomeLease | None = None
    native_home: Path | None = None
    docker_bin: Path | None = None
    experiment_lease: ExperimentLease | None = None
    published = False
    result = 1
    signal_returncode = 1
    old_handlers: dict[int, Any] = {}

    def interrupted(signum: int, _frame: object) -> None:
        nonlocal signal_returncode
        signal_returncode = 128 + signum
        raise GuardError(f"product credibility smoke interrupted by signal {signum}")

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        old_handlers[signum] = signal.signal(signum, interrupted)
    try:
        blocked_signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}
        prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked_signals)
        try:
            prepared = _preflight_before_publish(
                repo=repo,
                fixture=fixture,
                producer_root=producer_root,
            )
            lease = prepared.lease
            native_home = prepared.native_home
            docker_bin = prepared.docker_bin
            environment = prepared.environment
            experiment_lease = prepared.experiment_lease
            producer_directory = prepared.producer_directory
            round_directory = prepared.round_directory
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)
        smoke_script = (repo / "contextmesh/scripts/rrcv2_product_smoke.py").resolve(strict=True)
        command = _smoke_command(
            profile=prepared.profile,
            repo=repo,
            script=smoke_script,
            fixture=fixture,
            round_root=round_root,
        )
        _validate_prepublication_verification(repo)
        if not publish_producer(
            producer_root,
            round_authority=prepared.round_directory,
        ):
            raise GuardError("the one reviewed producer attempt is already consumed")
        published = True
        process = _launch_main(command, environment=environment, cwd=repo)
        try:
            result = process.wait(timeout=int(timeout_text) / 1000)
        except subprocess.TimeoutExpired:
            result = 124
        if process.poll() is not None:
            _mark_main_terminal(process, environment)
        _cleanup_product(
            process=process,
            environment=environment,
            cancellation_root=round_root / "cancellation",
            docker_bin=prepared.docker_bin,
        )
        verify_stable_home(prepared.native_home, lease)
        _validate_predecessor_manifest(repo)
        _reopen_directory(producer_root, prepared.producer_directory)
        _reopen_directory(round_root, prepared.round_directory)
        _terminal_receipt(
            producer_root,
            status="success" if result == 0 else "failure",
            returncode=result,
        )
        return result
    except BaseException as primary:
        cleanup_error: BaseException | None = None
        if not published and producer_root.exists() and not producer_root.is_symlink():
            try:
                published = _producer_is_published(producer_root)
                if not published:
                    if producer_directory is None or round_directory is None:
                        raise GuardError("unpublished product directory authority is missing")
                    published = _reconcile_unpublished_root(
                        producer_root, producer_directory, round_directory
                    )
            except BaseException as exc:
                cleanup_error = exc
        if published:
            try:
                _cleanup_product(
                    process=process,
                    environment=environment,
                    cancellation_root=round_root / "cancellation",
                    docker_bin=docker_bin,
                )
            except BaseException as exc:
                cleanup_error = exc
        try:
            if lease is not None and native_home is not None:
                verify_stable_home(native_home, lease)
            _validate_predecessor_manifest(repo)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        if published:
            try:
                if producer_directory is None or round_directory is None:
                    raise GuardError("published product directory authority is missing")
                _reopen_directory(producer_root, producer_directory)
                _reopen_directory(round_root, round_directory)
                _terminal_receipt(producer_root, status="failure", returncode=signal_returncode)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise GuardError(
                "product failure cleanup or terminal publication failed"
            ) from cleanup_error
        raise primary
    finally:
        if lease is not None:
            lease.close()
        if experiment_lease is not None:
            experiment_lease.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GuardError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"rrcv2 product guard: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
