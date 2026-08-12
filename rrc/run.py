"""Compatibility entry point for the canonical offline RRCv2 proof."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from rrc.contract import ArmMode, Config, ModelPort
from rrc.demo import format_arm_report, run_demo_arm
from rrc.model import CodexModel


def run_proof(
    model: ModelPort,
    evidence_path: str | Path,
    *,
    cfg: Config | None = None,
    round_id: str = "rrcv2-proof",
) -> dict[str, Any]:
    """Run one canonical WARM MISS→EXACT proof with local SQLite retrieval.

    This historical import seam deliberately delegates to :mod:`rrc.demo` so
    it cannot drift back to the removed pre-RRCv2 Task-only solver or require
    EverOS.  The optional EverOS adapter is exercised by its explicit product
    route, never by this default proof.
    """

    return run_demo_arm(
        round_id=round_id,
        mode=ArmMode.WARM,
        model=model,
        retrieval=None,
        evidence_path=evidence_path,
        cfg=cfg,
    )


def main() -> int:
    """Run the canonical proof with the pinned RRCv2 Codex model defaults."""

    runtime_root = Path(os.environ.get("RRC_RUNTIME_DIR") or "runtime/rrc")
    try:
        runtime_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(prefix="rrcv2-proof-", dir=runtime_root))
        run_dir.chmod(0o700)
    except OSError as exc:
        raise SystemExit(f"could not create RRCv2 run directory: {exc}") from exc

    strong = os.environ.get("RRC_STRONG_MODEL", "gpt-5.5")
    small = os.environ.get("RRC_SMALL_MODEL", "gpt-5.6-luna")
    round_id = f"rrcv2-{time.time_ns()}"
    evidence = run_proof(
        CodexModel(
            strong_model=strong,
            small_model=small,
            artifact_log=run_dir / "model-events.jsonl",
        ),
        run_dir / "evidence.json",
        round_id=round_id,
    )
    print(format_arm_report(evidence))
    print(f"Evidence: {run_dir / 'evidence.json'}")
    return 0 if evidence.get("proof_pass") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
