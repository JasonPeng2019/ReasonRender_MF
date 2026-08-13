"""Advance an already running arm-local ContextMesh broker to the next stage."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from contextmesh.mcp.shared_broker import SharedBrokerClient


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--control-token", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--stage-commit", required=True)
    parser.add_argument("--parent-stage-commit", required=True)
    parser.add_argument("--branch-lineage", default="main")
    return parser


async def run(args: argparse.Namespace) -> dict[str, object]:
    endpoint = json.loads(Path(args.endpoint).read_text(encoding="utf-8"))
    return await SharedBrokerClient(str(endpoint["host"]), int(endpoint["port"])).install_stage(
        control_token=args.control_token,
        ledger_path=args.ledger,
        stage_id=args.stage_id,
        stage_commit=args.stage_commit,
        parent_stage_commit=args.parent_stage_commit,
        branch_lineage=args.branch_lineage,
    )


def main(argv: Sequence[str] | None = None) -> int:
    print(json.dumps(asyncio.run(run(_parser().parse_args(argv))), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - direct harness boundary entry point.
    raise SystemExit(main())
