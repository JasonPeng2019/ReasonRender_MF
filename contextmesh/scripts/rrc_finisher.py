#!/usr/bin/env python3
"""Durable ContextMesh finisher for submitted canonical RRCv2 attempts."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import socket
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from rrc.attempts import AttemptRepository
from rrc.contextmesh import parse_coding_assignment
from rrc.contextmesh_runtime import load_memory_runtime
from rrc.contract import ArmMode, Config, ModelPort
from rrc.journal import JournalStateError, SQLiteRRCRepository
from rrc.model import CodexModel
from rrc.pipeline.solve import finish, hydrate_prepared, reject_contextmesh_attempt


class FinisherError(RuntimeError):
    """The finisher cannot safely advance a submitted attempt."""


def _host_token() -> str:
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]


def _default_owner_id() -> str:
    return f"finisher.{_host_token()}.{os.getpid()}"


def _owner_is_alive(owner_id: str) -> bool:
    match = re.fullmatch(r"finisher\.([0-9a-f]{16})\.([1-9][0-9]*)", owner_id)
    if match is None or match.group(1) != _host_token():
        # A foreign or legacy identity is not proven dead by this host.
        return True
    try:
        os.kill(int(match.group(2)), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


def reconcile_lifecycle(
    attempts: AttemptRepository,
    *,
    config: Config,
    now_ms: int | None = None,
) -> tuple[str, ...]:
    """Recover dead leases and terminalize expired stop-before-bind attempts."""

    recovered = attempts.reconcile_queue(
        owner_scope=config.owner_scope,
        owner_is_alive=_owner_is_alive,
        now_ms=now_ms,
    )
    for expired in attempts.expired_stop_pending(
        owner_scope=config.owner_scope,
        now_ms=now_ms,
    ):
        registered = attempts.load_registered_input(expired.attempt_id)
        assignment = parse_coding_assignment(registered.assignment)
        prepared_raw = attempts.repository.load_prepared(expired.attempt_id)
        if prepared_raw is None:
            raise JournalStateError("expired stop-pending attempt has no prepared authority")
        prepared = hydrate_prepared(
            prepared_raw,
            attempt=expired,
            input=registered.task_envelope,
            journal=attempts.repository,
        )
        reject_contextmesh_attempt(
            prepared,
            mode=ArmMode(assignment.mode),
            phase="spawn",
            reason="spawn_failed",
            journal=attempts.repository,
            terminal_owner="finisher-recovery",
        )
    return recovered


def finish_one(
    attempts: AttemptRepository,
    *,
    model: ModelPort,
    config: Config,
    owner_id: str,
    now_ms: int | None = None,
) -> str | None:
    """Claim and terminalize at most one queued attempt."""

    claimed = attempts.claim_next(
        owner_scope=config.owner_scope,
        owner_id=owner_id,
        now_ms=now_ms,
    )
    if claimed is None:
        return None
    heartbeat = _Heartbeat(attempts, owner_id)
    heartbeat.start(claimed.attempt_id)
    try:
        submission = attempts.load_submission(claimed.attempt_id)
        assignment = parse_coding_assignment(submission.assignment)
        if assignment.mode not in {"cold", "warm"}:
            raise FinisherError("submitted assignment has an invalid Spec-flow mode")
        prepared_raw = attempts.repository.load_prepared(claimed.attempt_id)
        if prepared_raw is None:
            raise JournalStateError("submitted attempt has no prepared authority")
        prepared = hydrate_prepared(
            prepared_raw,
            attempt=claimed,
            input=submission.task_envelope,
            journal=attempts.repository,
        )
        outcome = finish(
            prepared,
            submission.candidate,
            mode=ArmMode(assignment.mode),
            model=model,
            cfg=config,
            journal=attempts.repository,
            acceptance=attempts.repository,
            transport="contextmesh",
            terminal_owner=owner_id,
        )
        attempts.close_queue(claimed.attempt_id)
        return outcome.task_id
    finally:
        heartbeat.close()


class _Heartbeat:
    def __init__(self, attempts: AttemptRepository, owner_id: str, interval: float = 5.0) -> None:
        self.attempts = attempts
        self.owner_id = owner_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, attempt_id: str) -> None:
        def run() -> None:
            while not self._stop.wait(self.interval):
                try:
                    attempt = self.attempts.repository.load_attempt(attempt_id)
                    if attempt.state != "finishing":
                        return
                    self.attempts.renew(attempt, owner_id=self.owner_id)
                except Exception:
                    return

        self._thread = threading.Thread(target=run, name="rrcv2-finisher-heartbeat", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval + 1.0))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--owner-scope", required=True)
    parser.add_argument("--owner-id", default=_default_owner_id())
    parser.add_argument("--codex-bin", default=os.environ.get("RRD_CODEX_BIN", "codex"))
    parser.add_argument("--strong-model", default=os.environ.get("RRC_STRONG_MODEL", "gpt-5.5"))
    parser.add_argument("--small-model", default="gpt-5.6-luna")
    parser.add_argument(
        "--memory-backend",
        choices=("sqlite", "everos"),
        default=os.environ.get("RRD_MEMORY_BACKEND", "sqlite"),
    )
    parser.add_argument(
        "--everos-target",
        type=Path,
        default=(Path(value) if (value := os.environ.get("RRCV2_EVEROS_TARGET")) else None),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--ready-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0.05 <= args.poll_seconds <= 5.0:
        raise SystemExit("--poll-seconds must be from 0.05 through 5.0")
    stop = threading.Event()

    def requested_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, requested_stop)
    signal.signal(signal.SIGINT, requested_stop)
    model = CodexModel(
        strong_model=args.strong_model,
        small_model=args.small_model,
        executable=args.codex_bin,
    )
    product_cell_id = os.environ.get("RRCV2_CELL_ID")
    if product_cell_id:
        model.product_cell_id = product_cell_id
    with SQLiteRRCRepository(args.database) as repository:
        attempts = AttemptRepository(repository)
        config, _retrieval = load_memory_runtime(
            repository,
            owner_scope=args.owner_scope,
            backend=args.memory_backend,
            everos_target_path=args.everos_target,
        )
        if args.ready_file is not None:
            args.ready_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                args.ready_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.close(descriptor)
        try:
            while not stop.is_set():
                reconcile_lifecycle(attempts, config=config)
                task_id = finish_one(
                    attempts,
                    model=model,
                    config=config,
                    owner_id=args.owner_id,
                )
                if args.once:
                    return 0
                if task_id is None:
                    stop.wait(args.poll_seconds)
        finally:
            if args.ready_file is not None:
                try:
                    args.ready_file.unlink()
                except FileNotFoundError:
                    pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FinisherError, JournalStateError, OSError, RuntimeError, ValueError) as exc:
        print(f"rrcv2 finisher: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
