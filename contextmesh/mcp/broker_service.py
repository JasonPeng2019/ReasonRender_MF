"""Run one arm-local shared brief broker as a durable local process."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from contextmesh.mcp.shared_broker import SharedBriefBroker, SharedBrokerServer
from harness.four_worker_plan import OverlapLedgerEntry


def load_ledger(path: str | Path) -> tuple[OverlapLedgerEntry, ...]:
    """Load the runner's JSON ledger without accepting unbound entries."""

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load overlap ledger: {error}") from error
    if not isinstance(value, list):
        raise ValueError("overlap ledger must be a list")
    entries: list[OverlapLedgerEntry] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"ledger entry {index} must be an object")
        try:
            plan_steps = tuple(
                (str(step["worker_id"]), tuple(str(value) for value in step["steps"]))
                for step in item["plan_steps"]
            )
            entries.append(
                OverlapLedgerEntry(
                    brief_id=str(item["brief_id"]),
                    canonical_path=str(item["canonical_path"]),
                    manifest_hash=str(item["manifest_hash"]),
                    requirements_hash=str(item["requirements_hash"]),
                    plan_steps=plan_steps,
                    source_owner=str(item["source_owner"]),
                    peer_workers=tuple(str(worker) for worker in item["peer_workers"]),
                    required_facts=tuple(str(fact) for fact in item.get("required_facts", ())),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid ledger entry {index}: {error}") from error
    return tuple(entries)


def ledger_payload(entries: Sequence[OverlapLedgerEntry]) -> list[dict[str, object]]:
    """Serialize typed ledger entries for the service without lossy tuple values."""

    return [
        {
            "brief_id": entry.brief_id,
            "canonical_path": entry.canonical_path,
            "manifest_hash": entry.manifest_hash,
            "requirements_hash": entry.requirements_hash,
            "plan_steps": [
                {"worker_id": worker_id, "steps": list(steps)}
                for worker_id, steps in entry.plan_steps
            ],
            "source_owner": entry.source_owner,
            "peer_workers": list(entry.peer_workers),
            "required_facts": list(entry.required_facts),
        }
        for entry in entries
    ]


async def run_service(args: argparse.Namespace) -> None:
    broker = SharedBriefBroker(
        args.source_root,
        load_ledger(args.ledger),
        state_dir=args.state_dir,
        log_path=args.log,
        wait_timeout_ms=args.wait_timeout_ms,
        workflow_id=args.workflow_id,
        stage_id=args.stage_id,
        stage_commit=args.stage_commit,
        parent_stage_commit=args.parent_stage_commit,
        branch_lineage=args.branch_lineage,
        git_root=args.git_root,
        max_diff_bytes=args.max_diff_bytes,
    )
    server = SharedBrokerServer(broker, control_token=args.control_token)
    host, port = await server.start(args.host, args.port)
    Path(args.port_file).write_text(json.dumps({"host": host, "port": port}) + "\n", encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wait-timeout-ms", type=int, default=600_000)
    parser.add_argument("--workflow-id")
    parser.add_argument("--stage-id")
    parser.add_argument("--stage-commit")
    parser.add_argument("--parent-stage-commit")
    parser.add_argument("--branch-lineage", default="main")
    parser.add_argument("--git-root")
    parser.add_argument("--max-diff-bytes", type=int, default=48_000)
    parser.add_argument("--control-token")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        asyncio.run(run_service(_parser().parse_args(argv)))
    except ValueError as error:
        raise SystemExit(f"ContextMesh broker configuration error: {error}") from error
    return 0


if __name__ == "__main__":  # pragma: no cover - runner owns the long-lived process.
    raise SystemExit(main())
