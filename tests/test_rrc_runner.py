from __future__ import annotations

from pathlib import Path

from rrc.contract import ArmMode, Config
from rrc.run import run_proof


class _UnusedModel:
    provider = "test"

    def complete(self, *args, **kwargs):
        raise AssertionError("thin compatibility test must not call the provider")


def test_run_proof_delegates_to_canonical_offline_warm_demo(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def fake_run_demo_arm(**kwargs):
        observed.update(kwargs)
        return {"proof_pass": True, "pipeline": "rrc.pipeline.solve"}

    monkeypatch.setattr("rrc.run.run_demo_arm", fake_run_demo_arm)
    model = _UnusedModel()
    config = Config("runner-owner")
    evidence = tmp_path / "evidence.json"

    result = run_proof(model, evidence, cfg=config, round_id="runner-round")

    assert result == {"proof_pass": True, "pipeline": "rrc.pipeline.solve"}
    assert observed == {
        "round_id": "runner-round",
        "mode": ArmMode.WARM,
        "model": model,
        "retrieval": None,
        "evidence_path": evidence,
        "cfg": config,
    }


def test_default_runner_has_no_everos_dependency() -> None:
    source = Path("rrc/run.py").read_text(encoding="utf-8")
    assert "from rrc.everos" not in source
    assert "EverOSClient" not in source
