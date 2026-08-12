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
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from rrc.contract import canonical_json_bytes
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
EXPERIMENT_ID = "rrcv2-cli-smoke-v20"
PRODUCER_SHA256 = "7a66a30bbd2e7004724ba4aee1de7a879d87eb9212653e6a36d52d2cf1514744"
ROUND_TOKEN = "rrcv2-cli-smoke-7a66a30bbd2e7004724ba4aee1de7a87"
PLAN_SHA256 = "d9e02378d8539894d966b3a687abcf1bb4441df7ad1d773a9afee505ed7ee00c"
PLAN_TRANSCRIPT_SHA256 = "b2d9f4bae4dcea176bfde22c3245ac5b03a34a2d84b71da97f864786b2d8edb9"
PLAN_SEAL_SHA256 = "76e4cd5b6551a207bc4ca5cf812a245d5079560cbd873e92b6cc66f481e71137"
_MAX_AUTHORITY = 2_000_000
_ROUND = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_TIMEOUT = re.compile(r"(?:[1-9][0-9]{0,4}|[1-8][0-9]{5}|900000)\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
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


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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


def _create_exclusive(path: Path, raw: bytes, mode: int = 0o600) -> None:
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
    finally:
        os.close(descriptor)


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
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
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
        {"experiment_id": EXPERIMENT_ID, "fixture_manifest_sha256": FIXTURE_SHA256, "v": 20}
    )


def producer_value() -> bytes:
    if len(_producer_preimage()) != 139 or _sha(_producer_preimage()) != PRODUCER_SHA256:
        raise GuardError("producer preimage constant differs")
    return canonical_json_bytes(
        {
            "experiment_id": EXPERIMENT_ID,
            "fixture_manifest_sha256": FIXTURE_SHA256,
            "plan_review_seal_sha256": PLAN_SEAL_SHA256,
            "plan_review_transcript_sha256": PLAN_TRANSCRIPT_SHA256,
            "plan_sha256": PLAN_SHA256,
            "producer_sha256": PRODUCER_SHA256,
            "round_token": ROUND_TOKEN,
            "v": 20,
        }
    )


def publish_producer(root: Path) -> bool:
    """Publish once; return True only for the invocation eligible to launch."""

    if not root.parent.exists():
        root.parent.mkdir(parents=True, mode=0o700)
        os.chmod(root.parent, 0o700)
    _owned_directory(root.parent)
    _owned_directory(root, create=True)
    final = root / "producer.json"
    temporary = root / ".producer.v20.tmp"
    expected = producer_value()
    admitted = {"producer.json", ".producer.v20.tmp", "round", "terminal.json"}
    if any(path.name not in admitted for path in root.iterdir()):
        raise GuardError("producer root contains a foreign entry")
    final_exists = final.exists() or final.is_symlink()
    temp_exists = temporary.exists() or temporary.is_symlink()
    if temp_exists:
        temp_raw, temp_meta = _read_regular(
            temporary, mode=0o600, cap=len(expected), allowed_links=frozenset({1, 2})
        )
        if temp_raw != expected:
            raise GuardError("producer temporary differs")
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
        temporary.unlink()
        _fsync_dir(root)
        return False
    if final_exists:
        final_raw, _ = _read_regular(final, mode=0o600, cap=len(expected))
        if final_raw != expected:
            raise GuardError("existing producer record differs")
        return False
    _create_exclusive(temporary, expected)
    os.link(temporary, final, follow_symlinks=False)
    _fsync_dir(root)
    temporary.unlink()
    _fsync_dir(root)
    final_raw, _ = _read_regular(final, mode=0o600, cap=len(expected))
    if final_raw != expected:
        raise GuardError("published producer record differs")
    return True


def product_environment(
    *,
    repo: Path,
    native_home: Path,
    user_home: Path,
    codex_bin: Path,
    uv_bin: Path,
    round_root: Path,
    producer_root: Path,
    nonce: str,
) -> dict[str, str]:
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


def _docker_cleanup(environment: Mapping[str, str]) -> None:
    nonce = environment["RRCV2_SMOKE_RUN_NONCE"]
    docker = "/usr/local/bin/docker"
    if not Path(docker).is_file():
        docker = "/usr/bin/docker"
    if not Path(docker).is_file():
        raise GuardError("Docker is unavailable for exact smoke cleanup")
    base = [docker, "--context", DOCKER_CONTEXT]
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
        _docker_cleanup(environment)


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
    if _TIMEOUT.fullmatch(timeout_text) is None or str(int(timeout_text)) != timeout_text:
        raise GuardError("timeout is outside the reviewed grammar")
    repo = Path(__file__).resolve().parents[2]
    fixture = args.fixture.resolve(strict=True)
    expected_fixture = (repo / "tests/fixtures/rrcv2_cli_smoke/manifest.json").resolve(strict=True)
    if fixture != expected_fixture:
        raise GuardError("fixture path is not the reviewed authority")
    load_fixture(fixture)
    if _sha((repo / "PLAN.md").read_bytes()) != PLAN_SHA256:
        raise GuardError("settled PLAN bytes differ")
    review = repo / ".generated/state/rrcv2-convergence/reviews"
    if _sha((review / "plan-m6-cli-smoke-v20.txt").read_bytes()) != PLAN_TRANSCRIPT_SHA256:
        raise GuardError("plan transcript bytes differ")
    if _sha((review / "plan-m6-cli-smoke-v20.seal.json").read_bytes()) != PLAN_SEAL_SHA256:
        raise GuardError("plan seal bytes differ")
    producer_root = (
        repo
        / ".generated/state/rrcv2-convergence/verify/cli-smoke"
        / EXPERIMENT_ID
        / PRODUCER_SHA256
    )
    if not publish_producer(producer_root):
        raise GuardError("the one reviewed producer attempt is already consumed")
    producer_directory = _owned_directory(producer_root)
    round_root = producer_root / "round"
    round_directory: DirectoryAuthority | None = None
    process: subprocess.Popen[bytes] | None = None
    environment: dict[str, str] | None = None
    lease: StableHomeLease | None = None
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
        round_root.mkdir(mode=0o700)
        round_directory = _owned_directory(round_root)
        for directory in (
            round_root / "tmp",
            round_root / "cancellation",
            round_root / "cancellation/children",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        user_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        native_home = (repo / "contextmesh/.codex-rrd-native").resolve(strict=True)
        codex_bin = resolve_executable(str(user_home / ".local/bin/codex"))
        uv_bin = Path("/usr/local/bin/uv").resolve(strict=True)
        lease = prepare_stable_home(repo=repo, home=native_home, user_home=user_home)
        validate_native_home(codex_bin=codex_bin, home=native_home, require_login=True)
        nonce = os.urandom(16).hex()
        environment = product_environment(
            repo=repo,
            native_home=native_home,
            user_home=user_home,
            codex_bin=codex_bin,
            uv_bin=uv_bin,
            round_root=round_root,
            producer_root=producer_root,
            nonce=nonce,
        )
        prefix_map = {
            name: value
            for name, value in environment.items()
            if name.startswith(("RRD_", "RRC_", "RRCV2_"))
        }
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
        _read_regular(smoke_script, mode=0o755)
        command = _smoke_command(
            profile=profile,
            repo=repo,
            script=smoke_script,
            fixture=fixture,
            round_root=round_root,
        )
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
        )
        verify_stable_home(native_home, lease)
        assert round_directory is not None
        _reopen_directory(producer_root, producer_directory)
        _reopen_directory(round_root, round_directory)
        _terminal_receipt(
            producer_root,
            status="success" if result == 0 else "failure",
            returncode=result,
        )
        return result
    except BaseException as primary:
        cleanup_error: BaseException | None = None
        try:
            cancellation = round_root / "cancellation"
            if cancellation.is_dir():
                _cleanup_product(
                    process=process,
                    environment=environment,
                    cancellation_root=cancellation,
                )
        except BaseException as exc:
            cleanup_error = exc
        try:
            if lease is not None:
                verify_stable_home(
                    (repo / "contextmesh/.codex-rrd-native").resolve(strict=True), lease
                )
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
            _reopen_directory(producer_root, producer_directory)
            if round_directory is not None:
                _reopen_directory(round_root, round_directory)
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        try:
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
