from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from rrc.orchestrator_contract import OrchestratorTask, PlanSpecPacket
from rrc.orchestrator_policy import POLICY

def _load_demo():
    path = Path(__file__).resolve().parents[1] / "contextmesh" / "bench" / "rrc_long_spec_demo.py"
    spec = importlib.util.spec_from_file_location("rrc_long_spec_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_long_spec_demo_materializes_real_ruleforge_and_generic_tasks(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)

    workspace = tmp_path / "workspace"
    for relative in (
        "ruleforge/domain.py",
        "ruleforge/normalizer.py",
        "ruleforge/registry.py",
        "ruleforge/evaluator.py",
        "ruleforge/errors.py",
        "ruleforge/service.py",
        "ruleforge/rules/base.py",
        "tests/test_ruleforge.py",
    ):
        assert (workspace / relative).is_file()
    for relative in ("ruleforge/domain.py", "ruleforge/normalizer.py", "ruleforge/evaluator.py"):
        assert len((workspace / relative).read_text(encoding="utf-8").splitlines()) >= 60

    tasks = manifest["tasks"]
    assert len(tasks) == 4
    assert {task["case_shape"] for task in tasks} == {manifest["case_shape"]}
    assert len({json.dumps(task["slot_values"], sort_keys=True) for task in tasks}) == 4
    assert all("RRC_SHAPE:" in task["text"] and "RRC_SLOT_VALUES:" in task["text"] for task in tasks)

    generic = json.dumps(manifest["generic_packet"], sort_keys=True)
    assert len(generic) >= 2000
    assert 3 <= len(manifest["generic_packet"]["read_first"]) <= 5
    assert 2 <= len(manifest["generic_packet"]["plan"]["steps"]) <= 4
    assert 2 <= len(manifest["generic_packet"]["acceptance"]) <= 4
    assert manifest["generic_packet"]["plan"]["invariants"]
    assert manifest["generic_packet"]["plan"]["edges"]
    assert manifest["generic_packet"]["plan"]["constraints"]
    for task in tasks:
        for value in task["slot_values"].values():
            assert value not in generic

    for task, rendered in zip(tasks, manifest["rendered_packets"], strict=True):
        rendered_text = json.dumps(rendered, sort_keys=True)
        for value in task["slot_values"].values():
            assert value in rendered_text
        for other in tasks:
            if other is task:
                continue
            for value in other["slot_values"].values():
                assert value not in rendered_text

    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["case_shape"] == manifest["case_shape"]


def test_long_spec_demo_packet_passes_current_detailed_policy(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)
    packet = PlanSpecPacket.from_dict(manifest["generic_packet"])

    for task in manifest["tasks"]:
        orchestrator_task = OrchestratorTask(
            task_id=task["task_id"],
            family="ruleforge-policy",
            case_shape=manifest["case_shape"],
            slot_values=task["slot_values"],
            oracle_tests=demo.ORACLE_TESTS,
        )
        decision = POLICY.decide(orchestrator_task)
        assert decision.profile == "detailed"
        assert POLICY.validate_packet(orchestrator_task, packet, decision)
        assert POLICY.packet_token_count(orchestrator_task, packet) <= decision.packet_token_budget


def test_live_prompts_force_baseline_reconstruction_but_reuse_rendered_packets(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)
    demo._write_prompts(tmp_path, manifest)

    baseline = (tmp_path / "baseline_prompt.md").read_text(encoding="utf-8")
    cached = (tmp_path / "cached_prompt.md").read_text(encoding="utf-8")
    for path in demo.ARCHITECTURE_PATHS:
        assert path in baseline
        assert path in cached
    assert "For every task separately" in baseline
    assert "reread every" in baseline
    assert "reconstruct the full long RuleForge policy" in baseline
    assert "Do not reuse that reconstruction" in baseline
    assert "locally rendered generic packet" in cached
    assert "do not rebuild or reread the long architecture specification" in " ".join(cached.lower().split())


def test_benchmark_configs_use_expensive_orchestrator_and_cheaper_workers() -> None:
    root = Path(__file__).resolve().parents[1]
    configs = [json.loads((root / "contextmesh" / "configs" / f"arm-{arm}.json").read_text()) for arm in ("a", "b")]

    for config in configs:
        provider_models = config["provider"]["ollama"]["models"]
        assert "deepseek-v4-pro" in provider_models
        assert "deepseek-v4-flash:preview" in provider_models
        assert config["model"] == "ollama/deepseek-v4-pro"
        assert config["agent"]["orchestrator"]["model"] == "ollama/deepseek-v4-pro"
        assert config["agent"]["worker"]["model"] == "ollama/deepseek-v4-flash:preview"
        assert config["agent"]["orchestrator"]["permission"]["edit"] == "deny"
        assert config["agent"]["worker"]["permission"]["edit"] == "allow"
        assert "cached rendered packet" in config["agent"]["orchestrator"]["prompt"]
