from __future__ import annotations

import pytest
from contextmesh.bench.rrc_long_spec_demo import materialize
from harness.four_worker_plan import freeze_worker_plans
from harness.workload_preflight import MIN_DUPLICATE_SOURCE_BYTES, require_capacity, source_mass


def test_ruleforge_workload_has_substantial_nonuniversal_duplicate_source_capacity(tmp_path) -> None:
    workspace = tmp_path / "workload"
    materialize(workspace)

    stats = source_mass(workspace / "workspace", freeze_worker_plans())

    catalog = next(item for item in stats["paths"] if item["canonical_path"] == "ruleforge/policy_catalog.py")
    assert catalog["raw_readers"] == 3
    assert stats["duplicate_source_bytes"] >= MIN_DUPLICATE_SOURCE_BYTES
    assert 0 < stats["duplicate_source_fraction"] < 1
    require_capacity(stats)


def test_capacity_gate_rejects_an_obviously_small_workload() -> None:
    with pytest.raises(ValueError, match="insufficient duplicate source capacity"):
        require_capacity({"duplicate_source_bytes": 9_277})
