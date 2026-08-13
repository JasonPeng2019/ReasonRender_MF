from __future__ import annotations

import json
from pathlib import Path

from contextmesh.bench.rrc_long_spec_demo import CASE_SHAPE, materialize
from harness.rrc_hit import RRCManifestError, build_rrc_hit, warm_rrc_cache
from rrc.everos import EverOSClient


class FakeEverOS(EverOSClient):
    def __init__(self) -> None:
        super().__init__("http://fake.invalid")
        self.indexed: list[tuple[str, str]] = []

    def index(self, case_shape: str, external_ref: str) -> None:
        self.indexed.append((case_shape, external_ref))

    def wait_for_index(self, **_kwargs) -> None:
        return None

    def search(self, case_shape: str, **_kwargs) -> list[tuple[str, float]]:
        return [(external_ref, 0.99) for shape, external_ref in self.indexed if shape == case_shape]


def test_rrc_hit_uses_a_preexisting_everos_and_runtime_cache_without_a_model(tmp_path: Path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    output = tmp_path / "rrc-hit.json"
    everos = FakeEverOS()

    warm = warm_rrc_cache(workspace, client=everos)
    indexed_before_hit = list(everos.indexed)
    result = build_rrc_hit(workspace, output, client=everos)

    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert result == persisted
    assert result["event"] == "rrc_hit"
    assert warm["event"] == "rrc_cache_warm"
    assert result["cache_warm"]["event"] == "rrc_cache_warm"
    assert result["lookup"]["backend"] == "local_everos+rrc_runtime"
    assert len(result["lookup"]["matches"]) == 4
    assert all(shape == CASE_SHAPE for shape, _ref in everos.indexed)
    assert everos.indexed == indexed_before_hit
    assert {match["task_id"] for match in result["lookup"]["matches"]} == {
        "ruleforge-security",
        "ruleforge-limits",
        "ruleforge-markets",
        "ruleforge-assurance",
    }


def test_rrc_hit_reuses_the_generic_template_after_stage_bindings_change(tmp_path: Path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"
    cache = workspace / ".rrc-cache"
    everos = FakeEverOS()

    warm = warm_rrc_cache(workspace, client=everos)
    bindings_path = cache / "bindings.json"
    bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    first = next(iter(bindings["bindings"].values()))
    first["domain"] = "stage_two_security"
    bindings_path.write_text(json.dumps(bindings, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    result = build_rrc_hit(workspace, tmp_path / "rrc-hit.json", client=everos)

    # RRC caches the source-free generic plan; concrete stage fields are
    # rendered after retrieval and must not invalidate that coordinator HIT.
    assert result["cache_key"] == warm["cache_key"]
    assert result["bindings_sha256"] != warm["bindings_sha256"]
    assert everos.indexed == [(CASE_SHAPE, warm["template_external_ref"])]


def test_rrc_hit_fails_closed_when_the_cache_was_not_warmed(tmp_path: Path) -> None:
    materialize(tmp_path / "workload")
    workspace = tmp_path / "workload" / "workspace"

    try:
        build_rrc_hit(workspace, tmp_path / "rrc-hit.json", client=FakeEverOS())
    except RRCManifestError as error:
        assert "not prewarmed" in str(error)
    else:  # pragma: no cover - a HIT without a warm record would invalidate the comparison.
        raise AssertionError("RRC HIT must require an earlier cache warm stage")
