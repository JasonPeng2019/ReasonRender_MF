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


def _load_meter():
    path = Path(__file__).resolve().parents[1] / "contextmesh" / "scripts" / "live_meter.py"
    spec = importlib.util.spec_from_file_location("live_meter", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cache_renderer(path: Path):
    spec = importlib.util.spec_from_file_location("rrc_cache_generated", path)
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
    assert len(generic) >= 900
    assert 3 <= len(manifest["generic_packet"]["read_first"]) <= 5
    assert 2 <= len(manifest["generic_packet"]["plan"]["steps"]) <= 4
    assert 2 <= len(manifest["generic_packet"]["acceptance"]) <= 4
    assert manifest["generic_packet"]["plan"]["invariants"]
    assert manifest["generic_packet"]["plan"]["edges"]
    assert manifest["generic_packet"]["plan"]["constraints"]
    assert "PREPARED REAL CONTRACT SNAPSHOT" not in generic
    assert "MODULE PATTERN" not in generic
    assert "TEST PATTERN" not in generic
    assert "from ruleforge." not in generic
    assert "def register(" not in generic
    for task in tasks:
        for value in task["slot_values"].values():
            assert value not in generic

    cache = workspace / ".rrc-cache"
    assert sorted(path.name for path in cache.glob("*.json")) == ["bindings.json", "template.json"]
    assert json.loads((cache / "template.json").read_text(encoding="utf-8")) == manifest["generic_packet"]
    binding_index = json.loads((cache / "bindings.json").read_text(encoding="utf-8"))
    assert binding_index["slot_names"] == list(demo.SLOT_NAMES)
    assert set(binding_index["bindings"]) == {task["task_id"] for task in tasks}
    assert all(
        binding_index["bindings"][task["task_id"]] == task["slot_values"]
        for task in tasks
    )

    renderer = _load_cache_renderer(workspace / "rrc_cache.py")
    for task in tasks:
        rendered = renderer.load_rendered_packet(workspace, task["task_id"])
        expected = demo.render_cached_packet(manifest["generic_packet"], task["slot_values"])
        assert rendered == expected
        rendered_text = json.dumps(rendered, sort_keys=True)
        for value in task["slot_values"].values():
            assert value in rendered_text
        for other in tasks:
            if other is task:
                continue
            for value in other["slot_values"].values():
                assert value not in rendered_text

    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["case_shape"] == manifest["case_shape"]

    savings = manifest["packet_reuse_savings"]
    assert savings["metric"] == "template storage saved"
    assert savings["method"] == "whitespace-delimited tokens in prepared JSON text"
    assert savings["saved_tokens"] > 0
    assert savings["full_rendered_packet_total_tokens"] == sum(savings["full_rendered_packet_tokens"])
    assert savings["stored_template_and_binding_tokens"] == (
        savings["template_text_tokens"] + savings["binding_index_text_tokens"]
    )
    assert savings["saved_tokens"] == max(
        0,
        savings["full_rendered_packet_total_tokens"]
        - savings["stored_template_and_binding_tokens"],
    )


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


def test_live_prompts_force_baseline_reconstruction_but_retrieve_rendered_packets(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)
    demo._write_prompts(tmp_path, manifest)

    baseline = (tmp_path / "baseline_prompt.md").read_text(encoding="utf-8")
    cached = (tmp_path / "cached_prompt.md").read_text(encoding="utf-8")
    for prompt in (baseline,):
        compact = " ".join(prompt.lower().split())
        assert "exactly four independent cheap worker subagents total" in compact
        assert "exactly one worker assigned to each policy task" in compact
        assert "launch all four before any edits" in compact
        assert "independently read every shared source file below before editing" in compact
        assert "independent repeated reads" in compact
        assert "no worker may rely on another worker's read or evidence" in compact
        assert "read_evidence[<assigned task>]" in compact
        assert "verify exactly four complete read_evidence lists, one per task" in compact
        for path in demo.ARCHITECTURE_PATHS:
            assert path in prompt
    assert "For every task separately" in baseline
    assert "reread every" in baseline
    assert "reconstruct the full long RuleForge policy" in baseline
    assert "Do not reuse that reconstruction" in baseline
    assert ".rrc-cache/template.json" in cached
    assert ".rrc-cache/bindings.json" in cached
    assert "load_rendered_packet(workspace, task_id)" in cached
    assert "matching locally rendered packet path" not in cached
    assert "do not rebuild or reread the long architecture specification" in " ".join(cached.lower().split())
    assert "six binding fields" in " ".join(cached.lower().split())
    assert "shared-source preflight" not in " ".join(cached.lower().split())
    assert "synchronous contextmesh digest step" not in " ".join(cached.lower().split())
    assert "each worker follows its packet's plan" in " ".join(cached.lower().split())
    assert "print the resulting complete json packet" in cached.lower()
    assert "may invoke read, glob, grep, search, find, ls, or shell inspection" in cached.lower()
    assert "first tool action writes their two declared files" in cached.lower()
    assert "never contains source bodies, imports, function bodies, or a prewritten test" in cached.lower()
    assert "never write" in cached.lower()
    assert "structural packet arm" in cached
    assert "MANDATORY TWO-FILE PATCH CONTRACT" not in cached
    for task in manifest["tasks"]:
        assert not (tmp_path / "workspace" / ".rrc-cache" / f"{task['task_id']}.json").exists()
        rendered = demo.render_cached_packet(manifest["generic_packet"], task["slot_values"])
        assert json.dumps(rendered, sort_keys=True) not in cached


def test_verbatim_packet_level_is_explicit_opt_in_floor(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)

    demo._write_prompts(tmp_path, manifest, "verbatim")

    prompt = (tmp_path / "cached_prompt.md").read_text(encoding="utf-8")
    assert "opt-in full-verbatim floor" in prompt
    assert "MANDATORY TWO-FILE PATCH CONTRACT" in prompt


def test_everos_case_alignment_contains_shape_but_not_dynamic_slots(tmp_path: Path) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)

    for task in manifest["tasks"]:
        runtime_task = demo._runtime_task(task)
        assert runtime_task.case_shape == manifest["case_shape"]
        assert all(value not in runtime_task.case_shape for value in task["slot_values"].values())


def test_lane_b_rebinds_stale_shape_ref_and_isolates_slots(
    tmp_path: Path, monkeypatch
) -> None:
    demo = _load_demo()
    manifest = demo.materialize(tmp_path)
    stale_ref = "stale-shape-ref"

    def fake_post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        if path == "/api/v2/memory/search":
            return {
                "data": {
                    "unprocessed_messages": [
                        {
                            "content": json.dumps(
                                {"schema_version": 1, "case_shape": payload["query"], "external_ref": stale_ref}
                            )
                        }
                    ]
                }
            }
        if path == "/api/v2/memory/add":
            return {"data": {"status": "accumulated"}}
        return {}

    from rrc.everos import EverOSClient

    monkeypatch.setattr(EverOSClient, "_post", fake_post)
    monkeypatch.setattr(EverOSClient, "wait_for_index", lambda self, **kwargs: None)

    rendered = demo._lane_b(tmp_path, manifest)
    proof = json.loads((tmp_path / "lane_b_proof.json").read_text(encoding="utf-8"))

    assert len(rendered) == 4
    assert [item["branch"] for item in proof["outcomes"]] == [
        "miss",
        "reuse",
        "reuse",
        "reuse",
    ]
    assert len(proof["planner_calls"]) == 1
    assert proof["external_refs"] == [stale_ref] * 4
    assert proof["privacy_assertion"]["passed"] is True

    import sqlite3

    with sqlite3.connect(tmp_path / "lane_b.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM plan_spec_templates").fetchone() == (1,)
        stored = connection.execute("SELECT payload FROM plan_spec_templates").fetchone()[0]
    assert "{domain}" in stored and "{rule_name}" in stored
    for task in manifest["tasks"]:
        for value in task["slot_values"].values():
            assert value not in stored

    for task, packet in zip(manifest["tasks"], rendered, strict=True):
        packet_text = json.dumps(packet, sort_keys=True)
        for value in task["slot_values"].values():
            assert value in packet_text
        for other in manifest["tasks"]:
            if other is task:
                continue
            for value in other["slot_values"].values():
                assert value not in packet_text

    for call in proof["everos_calls"]:
        payload_text = json.dumps(call["payload"], sort_keys=True)
        assert manifest["case_shape"] in payload_text or call["path"] == "/api/v2/memory/flush"
        for task in manifest["tasks"]:
            for value in task["slot_values"].values():
                assert value not in payload_text


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


def test_cached_arm_allows_bounded_preflight_actions_without_changing_workers() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "contextmesh" / "configs" / "arm-b.json").read_text())

    assert config["agent"]["orchestrator"]["steps"] >= 12
    assert config["agent"]["worker"]["steps"] == 4


def test_three_arm_wiring_uses_required_prompts_and_labels() -> None:
    root = Path(__file__).resolve().parents[1]
    tui = (root / "contextmesh" / "scripts" / "demo_tui.sh").read_text(encoding="utf-8")
    launcher = (root / "contextmesh" / "scripts" / "launch_rrc_three_tui.ps1").read_text(encoding="utf-8")
    wrapper = (root / "contextmesh" / "demo.sh").read_text(encoding="utf-8")

    assert "contextmesh)" in tui
    assert 'PROMPT="$RRCROOT/baseline_prompt.md"' in tui
    assert 'PROMPT="$RRCROOT/cached_prompt.md"' in tui
    assert 'if [ "$SIDE" = "contextmesh" ] || [ "$SIDE" = "full" ]; then' in tui
    assert 'CONTEXTMESH_APP_ID="cm-three-$ROUND-$SIDE"' in tui
    assert "CONTEXTMESH_MAX_DIGEST_RATIO=0.80" in tui
    assert "CONTEXTMESH_BLOCK_REREAD=1" in tui
    assert 'CACHE_DIR="$RRCROOT/workspace/.rrc-cache"' in tui
    assert '[ ! -f "$CACHE_DIR/template.json" ]' in tui
    assert '[ ! -f "$CACHE_DIR/bindings.json" ]' in tui
    assert "PACKET_COUNT" not in tui
    assert "RAW + CONTEXTMESH" in tui
    assert "EVEROS + RRCv2" not in tui
    assert "function Start-Sequence" in launcher
    assert "Raw -> ContextMesh -> RRCv2" in launcher
    assert "shared provider serializes Pro streams" in launcher
    assert "EverOS + RRCv2" not in launcher
    assert "raw|raw-recovery" in wrapper
    assert "contextmesh|full|a|b" in wrapper


def test_three_arm_meter_keeps_sessions_metrics_and_savings_separate(tmp_path: Path, monkeypatch) -> None:
    meter = _load_meter()
    monkeypatch.setattr(meter, "ROUND", "r-test")
    token_log = tmp_path / "tokens.jsonl"
    token_log.write_text(
        "\n".join(
            json.dumps(
                {
                    "session": session,
                    "measurement_state": "exact",
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }
            )
            for session, input_tokens, output_tokens in (
                ("demo-r-test-raw", 20, 10),
                ("demo-r-test-contextmesh", 12, 3),
                ("demo-r-test-contextmesh-summarizer", 4, 1),
                ("demo-r-test-full", 9, 2),
                ("demo-r-test-full-summarizer", 2, 1),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(meter, "TOKENS", token_log)

    stats_root = tmp_path / "runs" / "demo-tui" / "r-test"
    (stats_root / "contextmesh").mkdir(parents=True)
    (stats_root / "full").mkdir(parents=True)
    (stats_root / "contextmesh" / "metrics.jsonl").write_text(
        '{"event":"digest_hit","savedTokens":7}\n{"event":"task_compressed","savedTokens":3}\n',
        encoding="utf-8",
    )
    (stats_root / "full" / "metrics.jsonl").write_text(
        '{"event":"digest_hit","savedTokens":4}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(meter, "CM", tmp_path)

    totals = meter.three_arm_totals()
    assert totals["raw"]["input"] == 20
    assert totals["contextmesh"]["input"] == 12
    assert totals["contextmesh-summarizer"]["output"] == 1
    assert totals["full-summarizer"]["input"] == 2

    digests = meter.three_arm_digest_stats()
    assert digests["contextmesh"]["saved_tokens"] == 10
    assert digests["full"]["saved_tokens"] == 4
    rendered = meter.render_three_arm()
    assert "RAW + CONTEXTMESH" in rendered
    assert "CONTEXTMESH + RRCv2" in rendered
    assert "EVEROS + RRCv2" not in rendered
    assert "source tokens replaced" in rendered
    assert "packet_reuse_savings" not in rendered
    assert "template storage saved" not in rendered
    assert "tokens saved" not in rendered
    assert "tokens used" in rendered
