from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from contextmesh.scripts.rrcv2_capability_matrix import (
    MatrixError,
    _validate_evidence_inventory,
)

ROOT = Path(__file__).resolve().parents[1]


def _copy_capability(tmp_path: Path) -> Path:
    source = ROOT / ".generated/state/rrcv2-convergence/capability"
    target = tmp_path / ".generated/state/rrcv2-convergence/capability"
    target.parent.mkdir(parents=True)
    shutil.copytree(source, target)
    inventory_source = (
        ROOT / ".generated/state/rrcv2-convergence/verify/capability-evidence-inventory.v1.json"
    )
    inventory_target = (
        tmp_path / ".generated/state/rrcv2-convergence/verify/capability-evidence-inventory.v1.json"
    )
    inventory_target.parent.mkdir(parents=True)
    shutil.copy2(inventory_source, inventory_target)
    return tmp_path


def test_capability_inventory_reopens_every_nested_call_artifact(tmp_path: Path) -> None:
    repo = _copy_capability(tmp_path)
    assert len(_validate_evidence_inventory(repo)["calls"]) == 9  # type: ignore[arg-type]

    stdout = (
        repo
        / ".generated/state/rrcv2-convergence/capability/calls"
        / "cap-01-strong-code-medium/stdout.jsonl"
    )
    stdout.write_bytes(stdout.read_bytes() + b'{"type":"poison"}\n')
    with pytest.raises(MatrixError, match="inventory drift"):
        _validate_evidence_inventory(repo)


def test_capability_inventory_binds_exact_completion_ledger(tmp_path: Path) -> None:
    repo = _copy_capability(tmp_path)
    ledger = repo / ".generated/state/rrcv2-convergence/capability/launch-ledger.jsonl"
    lines = ledger.read_bytes().splitlines(keepends=True)
    assert len(lines) == 18
    ledger.write_bytes(b"".join(lines[:-1]))
    with pytest.raises(MatrixError, match="inventory drift"):
        _validate_evidence_inventory(repo)
