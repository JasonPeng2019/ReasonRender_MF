#!/usr/bin/env python3
"""Prepare and permit one native ContextMesh root session before launch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

from rrc.cell_journal import SQLiteCellJournal
from rrc.dispatch_permit import (
    AuthorityRef,
    ProductCellDispatchRequestV1,
    product_call_id,
    read_authority,
)
from rrc.journal import SQLiteRRCRepository
from rrc.product_runtime import authorize_product_with_refs, product_authority_refs


def _ref(path: Path) -> AuthorityRef:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > 2_000_000
    ):
        raise ValueError("task envelope must be a bounded mode-0600 regular file")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 2_000_001)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        or len(raw) != opened.st_size
        or len(raw) > 2_000_000
    ):
        raise ValueError("task envelope changed while opening")
    return AuthorityRef(path, hashlib.sha256(raw).hexdigest(), len(raw))


def prepare_root(args: argparse.Namespace) -> dict[str, object]:
    repo = args.repository.resolve(strict=True)
    task_ref = _ref(args.task_envelope.absolute())
    value = json.loads(read_authority(task_ref))
    task = value.get("task") if isinstance(value, dict) else None
    task_id = task.get("task_id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task envelope has no task identity")
    request = ProductCellDispatchRequestV1(
        call_id="",
        scope="interactive",
        controller="contextmesh",
        task_id=task_id,
        task_envelope_sha256=task_ref.sha256,
        run_id=args.run_id,
        replicate_id="interactive",
        arm=args.arm,
        branch="combined",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id=args.cell_id,
        attempt_id=None,
        transport="contextmesh",
        surface_id="root_strong_medium_native",
    )
    request = replace(request, call_id=product_call_id(request))
    with SQLiteRRCRepository(args.database) as repository:
        cells = SQLiteCellJournal(repository, authority_root=args.authority_root)
        cell = cells.begin_cell(request)
        if cell.state == "root_started":
            started = cells.load_root_started(cell.cell_id)
        else:
            cursor = cells.prepare_root_call(cell)
            prepared = cells.load_cell(cell.cell_id)
            permit = authorize_product_with_refs(
                request=request,
                journal_cursor=cursor,
                task_envelope_ref=task_ref,
                cell_attempt_binding_ref=None,
                cell_journal=None,
                repo=repo,
                refs=product_authority_refs(repo),
            )
            started = cells.mark_root_started(
                prepared,
                permit=permit,
                session_id=args.session_id,
                transcript_baseline_sha256=hashlib.sha256(b"").hexdigest(),
            )
        return {
            "v": 1,
            "cell_id": started.cell.cell_id,
            "root_call_id": started.cell.root_call_id,
            "generation": started.cell.generation,
            "task_envelope_sha256": task_ref.sha256,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--authority-root", type=Path, required=True)
    parser.add_argument("--task-envelope", type=Path, required=True)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--arm", choices=("rrc_cold", "rrc_warm"), required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_root(args), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
