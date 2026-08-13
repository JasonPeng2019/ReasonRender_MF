from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import harness.codex_compare as codex_compare
from harness.codex_compare import (
    ARMS,
    CACHE_FIELDS,
    PRIMARY_FIELDS,
    _brief_contract_failures,
    _contextmesh_evidence,
    _packet_insufficient_count,
    _peer_brief_size_violations,
    _plan_scoped_large_source_evidence,
    _product_code_failure,
    build_round_plan,
    compare_results,
    prepare_or_resume_round,
    prepare_round,
)
from harness.four_worker_plan import (
    build_overlap_ledger,
    fixture_terra_plan,
    freeze_worker_plans,
    manifest_sha256,
)
from harness.terra_plans import plan_path, prepare_dispatch, render_rrc_plans

ROOT = Path(__file__).resolve().parents[2]


def _result(primary: int, worker: int, orchestrator: int, *, valid: bool = True) -> dict[str, int | bool]:
    return {
        "valid": valid,
        "primary_compute": primary,
        "worker_marginal_compute": worker,
        "orchestrator_marginal_compute": orchestrator,
    }


def test_plan_has_three_arms_with_one_raw_terra_plan_paired_to_contextmesh(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r1", tmp_path)

    assert not plan.root.exists()
    assert [arm.arm for arm in plan.arms] == list(ARMS)
    assert len({arm.root for arm in plan.arms}) == 3
    raw, contextmesh, full = plan.arms
    assert contextmesh.terra_source_arm == "raw"
    assert contextmesh.terra_source == raw.parent
    assert contextmesh.terra_plan_root == raw.terra_plan_root
    assert full.terra_source == full.parent
    assert {
        invocation.contextmesh_app_id
        for arm in plan.arms
        for invocation in (arm.parent, *arm.workers)
    } == {"codex-comparison-comparison-r1"}
    for arm in plan.arms:
        assert arm.parent.model == "gpt-5.6-terra"
        assert len(arm.workers) == 4
        command = list(arm.parent.command)
        assert "--ignore-user-config" in command
        assert command[command.index("--enable") + 1] == "fast_mode"
        assert "--dangerously-bypass-approvals-and-sandbox" in command
        assert "--json" in command and "--output-last-message" in command
        assert command[-1] == "-"
        assert command[command.index("--config") + 1] in {
            "model_reasoning_effort=high",
            "model_auto_compact_token_limit=230000",
            "service_tier=priority",
        }
        assert "claude" not in command
        for invocation in arm.workers:
            command = list(invocation.command)
            assert "--ignore-user-config" in command
            assert "--dangerously-bypass-approvals-and-sandbox" in command
            assert "--json" in command and "--output-last-message" in command
            assert command[-1] == "-"
            assert "deepseek-v4-flash:0731-cloud" in command
            assert "--oss" in command and "ollama" in command
            assert "model_context_window=1048576" in command
            assert "model_auto_compact_token_limit=230000" in command
            assert "claude" not in command
        assert len({worker.target for worker in arm.workers}) == 4
        assert arm.parent.target not in {worker.target for worker in arm.workers}
        if arm.arm == "raw":
            assert not any("mcp_servers.contextmesh" in value for value in arm.parent.command)
            assert all(not any("mcp_servers.contextmesh" in value for value in worker.command) for worker in arm.workers)
        else:
            assert not any("mcp_servers.contextmesh" in value for value in arm.parent.command)
            assert all(any("mcp_servers.contextmesh.command" in value for value in worker.command) for worker in arm.workers)
            assert all(any("contextmesh.mcp.bridge" in value for value in worker.command) for worker in arm.workers)


def test_contextmesh_dispatch_and_usage_reuse_raw_terra_without_a_second_launch(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-paired-terra", tmp_path)
    prepare_round(plan)
    raw, contextmesh = plan.arms[:2]

    assert str(raw.terra_plan_root) in contextmesh.dispatch.read_text(encoding="utf-8")
    assert str(contextmesh.parent.target / ".terra-plans") not in contextmesh.dispatch.read_text(encoding="utf-8")

    collected: list[Path] = []
    dispatches: list[list[str]] = []

    def no_second_terra(_arm):
        raise AssertionError("ContextMesh must not launch a second Terra")

    def collect(parent_stream: Path, *_args):
        collected.append(parent_stream)
        return {
            "valid": True,
            "totals": {field: 0 for field in (*PRIMARY_FIELDS, *CACHE_FIELDS)},
        }

    monkeypatch.setattr(codex_compare, "_run_parent", no_second_terra)
    monkeypatch.setattr(codex_compare, "collect_codex_pilot", collect)
    def run_dispatch(command, *_args, **_kwargs):
        dispatches.append(list(command))
        return SimpleNamespace(returncode=0, stdout="dispatched\n", stderr="")

    monkeypatch.setattr(codex_compare.subprocess, "run", run_dispatch)

    result = codex_compare.execute_arm(contextmesh)

    assert result["parent_run"]["mode"] == "paired_raw_plan"
    assert result["parent_run"]["dispatch_completed"] is True
    assert result["terra_plan_source_arm"] == "raw"
    assert collected == [raw.parent.stream]
    assert dispatches[0] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(contextmesh.dispatch)]


def test_product_failure_ignores_tracebacks_from_a_contextmesh_named_arm_worktree(tmp_path: Path) -> None:
    arm = build_round_plan(ROOT, "comparison-product-classification", tmp_path).arms[1]
    arm.workers[0].stderr.parent.mkdir(parents=True)
    arm.workers[0].stderr.write_text(
        f"Traceback\n{arm.root}\\worktrees\\worker-01\\tests\\test_rule.py\n",
        encoding="utf-8",
    )

    assert _product_code_failure(arm) is False
    arm.workers[0].stderr.write_text(
        f"Traceback\n{ROOT / 'contextmesh' / 'mcp' / 'shared_broker.py'}\n",
        encoding="utf-8",
    )
    assert _product_code_failure(arm) is True


def test_planning_remains_nonexecuting_without_the_explicit_execute_flag(capsys) -> None:
    assert codex_compare.main(["comparison-r0"]) == 0

    assert '"status": "planned"' in capsys.readouterr().out


def test_preparation_materializes_one_manifest_and_isolated_arm_targets(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r2", tmp_path)
    metadata = prepare_round(plan)

    assert metadata["topology"] == "1+4"
    manifest_hashes = set()
    targets = set()
    for arm in plan.arms:
        evidence = json.loads((arm.root / "comparison.json").read_text(encoding="utf-8"))
        manifest_hashes.add(evidence["workload_manifest_sha256"])
        targets.add(arm.parent.target)
        assert arm.dispatch.is_file()
        dispatch = arm.dispatch.read_text(encoding="utf-8")
        assert "Start-Process" in dispatch and "WaitForExit" in dispatch
        if arm.arm == "raw":
            assert "if ($false)" in dispatch
        else:
            assert "contextmesh.mcp.broker_service" in dispatch
            assert "CONTEXTMESH_BROKER_HOST" in dispatch
            assert "harness.mcp_model_probe" not in dispatch
            assert not arm.broker_ledger.exists()
        assert "CONTEXTMESH_APP_ID" in dispatch
        assert "dispatch-error.txt" in dispatch
        assert not list(arm.root.glob("workers/worker-*/prompt.md"))
        assert arm.terra_request.is_file()
        workspace_instructions = (arm.parent.target / "AGENTS.md").read_text(encoding="utf-8")
        assert "Do not run Git commands" in workspace_instructions
    assert len({arm.mcp_model_probe for arm in plan.arms if arm.arm != "raw"}) == 1
    assert len(manifest_hashes) == 1
    assert len(targets) == 3
    full_prompt = plan.arms[-1].parent.prompt.read_text(encoding="utf-8")
    assert "harness.terra_plans render-rrc" in full_prompt
    assert '"task_id": "ruleforge-security"' in plan.arms[0].terra_request.read_text(encoding="utf-8")
    assert "generic_packet" not in plan.arms[0].terra_request.read_text(encoding="utf-8")
    assert "slot_values" not in plan.arms[0].terra_request.read_text(encoding="utf-8")
    assert "CONTEXTMESH_BROKER_PORT" in plan.arms[-1].dispatch.read_text(encoding="utf-8")


def test_preparation_enforces_text_source_views_per_arm(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-source-view", tmp_path)
    prepare_round(plan)
    raw, contextmesh = plan.arms[0], plan.arms[1]
    raw_one, cm_one, cm_two, cm_three = raw.workers[0], contextmesh.workers[0], contextmesh.workers[1], contextmesh.workers[2]

    assert (raw_one.target / "ruleforge" / "policy_catalog.py").is_file()
    assert not (raw_one.target / "ruleforge" / "policy_catalog.pyc").exists()
    assert not (cm_one.target / "ruleforge" / "policy_catalog.py").exists()
    assert (cm_one.target / "ruleforge" / "policy_catalog.pyc").is_file()
    assert not (cm_two.target / "ruleforge" / "domain.py").exists()
    assert (cm_two.target / "ruleforge" / "domain.pyc").is_file()
    assert (cm_three.target / "ruleforge" / "errors.py").is_file()
    source_view = json.loads((cm_three.target / ".harness-source-view.json").read_text(encoding="utf-8"))
    assert source_view["raw_source_paths"] == ["ruleforge/errors.py"]
    runtime = shutil.which("python")
    assert runtime is not None
    imported = subprocess.run(
        [runtime, "-c", "from ruleforge.policy_catalog import POLICY_CATALOG; assert POLICY_CATALOG"],
        cwd=cm_one.target,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert imported.returncode == 0, imported.stdout


def test_round_runs_one_retained_mcp_eligibility_probe(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-mcp-probe", tmp_path)
    prepare_round(plan)
    calls: list[Path] = []
    probe_root = plan.root / "mcp-eligibility-probe"
    probe_root.mkdir()
    (probe_root / "mcp-health.json").write_text("{}\n", encoding="utf-8")

    def fake_probe(workspace: Path, output: Path):
        calls.append(output)
        assert workspace == plan.workload_root / "workspace"
        return {"valid": True, "final_marker": "TOOL_SURFACE_SEEN"}

    monkeypatch.setattr(codex_compare, "run_mcp_model_probe", fake_probe)

    assert codex_compare._ensure_mcp_probe(plan)["valid"] is True
    assert codex_compare._ensure_mcp_probe(plan)["valid"] is True
    assert calls == [probe_root]
    assert json.loads((probe_root / "result.json").read_text(encoding="utf-8"))["valid"] is True
    assert "Start-Process -FilePath powershell.exe" in plan.arms[0].parent.prompt.read_text(encoding="utf-8")


def test_report_requires_shared_identity_and_tiered_compute_reduction(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r3", tmp_path)
    prepare_round(plan)
    results = {
        "raw": _result(120, 80, 40),
        "contextmesh": _result(90, 60, 40),
        "full": _result(70, 60, 10),
    }

    report = compare_results(plan, results)

    assert report["comparable"] is True
    assert report["full_beats_raw"] is True
    assert report["contextmesh_beats_raw"] is True
    assert report["full_beats_contextmesh"] is True
    assert report["tiered_full_win"] is True
    assert report["status"] == "proved"
    results["full"] = _result(120, 60, 10)
    tied = compare_results(plan, results)
    assert tied["full_beats_raw"] is False
    assert tied["status"] == "optimization_required"


def test_report_rejects_a_raw_only_full_win_when_contextmesh_loses(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r3b", tmp_path)
    prepare_round(plan)
    report = compare_results(
        plan,
        {
            "raw": _result(120, 80, 40),
            "contextmesh": _result(150, 100, 40),
            "full": _result(70, 100, 10),
        },
    )

    assert report["full_beats_raw"] is True
    assert report["contextmesh_beats_raw"] is False
    assert report["full_beats_contextmesh"] is True
    assert report["tiered_full_win"] is False
    assert report["strict_proof"] is False
    assert report["claude_eligible"] is False
    assert report["status"] == "partial_hierarchy"


def test_report_rejects_nominal_or_wrong_role_reductions(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r3c", tmp_path)
    prepare_round(plan)
    report = compare_results(
        plan,
        {
            "raw": _result(100, 80, 20),
            "contextmesh": _result(90, 60, 20),
            "full": _result(90, 60, 19),
        },
    )

    assert report["contextmesh_beats_raw"] is True
    assert report["rrc_beats_raw_terra"] is False
    assert report["full_marginal_reduction"] == 0.1
    assert report["strict_proof"] is False
    assert report["claude_eligible"] is False


def test_report_rejects_a_manifest_or_configuration_mismatch(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r4", tmp_path)
    prepare_round(plan)
    full_evidence = plan.arms[-1].root / "comparison.json"
    body = json.loads(full_evidence.read_text(encoding="utf-8"))
    body["comparability_fingerprint"] = "mismatch"
    full_evidence.write_text(json.dumps(body), encoding="utf-8")

    report = compare_results(
        plan,
        {arm: _result(10, 5, 5) for arm in ARMS},
    )

    assert report["comparable"] is False
    assert report["status"] == "evidence_incomplete"


def test_degraded_retained_evidence_continues_without_proving_the_gate(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r5", tmp_path)
    prepare_round(plan)
    results = {
        "raw": _result(120, 80, 40, valid=False),
        "contextmesh": _result(90, 60, 40),
        "full": _result(70, 60, 10),
    }

    report = compare_results(plan, results)

    assert report["comparable"] is True
    assert report["full_beats_raw"] is True
    assert report["strict_proof"] is False
    assert report["status"] == "directional_tiered_win"
    assert report["claude_eligible"] is False
    assert report["quality_degraded_arms"] == ["raw"]


def test_existing_complete_artifact_resumes_without_rematerializing(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r6", tmp_path)
    prepare_round(plan)
    manifest = plan.workload_root / "manifest.json"
    before = manifest.read_bytes()

    resumed = prepare_or_resume_round(plan)

    assert resumed["resumed"] is True
    assert resumed["prepared"] is True
    assert manifest.read_bytes() == before


def test_dispatch_only_resume_reuses_terra_plan_without_replaying_terra(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-dispatch-resume", tmp_path)
    prepare_round(plan)
    arm = plan.arms[0]
    for worker in plan.worker_plans:
        observed = fixture_terra_plan(worker)
        plan_path(arm.terra_plan_root, worker.worker_id).parent.mkdir(parents=True, exist_ok=True)
        plan_path(arm.terra_plan_root, worker.worker_id).write_text(
            json.dumps(
                {
                    "task_id": observed.task_id,
                    "worker_id": observed.worker_id,
                    "plan_steps": list(observed.plan_steps),
                    "source_facts": list(observed.source_facts),
                }
            ),
            encoding="utf-8",
        )
    arm.terra_source.stream.parent.mkdir(parents=True, exist_ok=True)
    arm.terra_source.stream.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    arm.root.joinpath("attempt.json").write_text('{"status":"degraded"}\n', encoding="utf-8")
    arm.root.joinpath("result.json").write_text(
        '{"valid":false,"blocking_product_error":false}\n', encoding="utf-8"
    )
    arm.dispatch_error.write_text("old dispatcher failure\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(
        codex_compare,
        "_run_existing_dispatch",
        lambda observed_arm, _resume_root: calls.append(observed_arm.arm) or {"exit_code": 0},
    )
    monkeypatch.setattr(
        codex_compare,
        "_finalize_arm",
        lambda _arm, _parent: {"valid": True, "blocking_product_error": False},
    )

    result = codex_compare.resume_retained_dispatch(arm)

    assert calls == ["raw"]
    assert result["resumed_dispatch"] is True
    assert not arm.dispatch_error.exists()
    resume = Path(result["resume_artifact"])
    assert (resume / "previous-result.json").is_file()
    assert (resume / "previous-dispatch-error.txt").is_file()


def test_worker_session_resume_continues_only_interrupted_lunas(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-worker-resume", tmp_path)
    prepare_round(plan)
    arm = next(candidate for candidate in plan.arms if candidate.arm == "contextmesh")
    arm.terra_source.stream.parent.mkdir(parents=True, exist_ok=True)
    arm.terra_source.stream.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
    arm.terra_plan_root.mkdir(parents=True, exist_ok=True)
    (arm.terra_plan_root / "worker-01.json").write_text("{}\n", encoding="utf-8")
    arm.broker_ledger.parent.mkdir(parents=True, exist_ok=True)
    arm.broker_ledger.write_text("[]\n", encoding="utf-8")
    arm.root.joinpath("attempt.json").write_text('{"status":"degraded"}\n', encoding="utf-8")
    arm.root.joinpath("result.json").write_text(
        '{"valid":false,"blocking_product_error":false}\n', encoding="utf-8"
    )
    for index, worker in enumerate(arm.workers, start=1):
        worker.stream.parent.mkdir(parents=True, exist_ok=True)
        worker.stream.write_text(
            json.dumps({"type": "thread.started", "thread_id": f"thread-{index}"}) + "\n",
            encoding="utf-8",
        )
        if worker.role != "worker-03":
            worker.stream.write_text(
                worker.stream.read_text(encoding="utf-8") + '{"type":"turn.completed"}\n', encoding="utf-8"
            )
        worker.final.write_text(
            "CONTEXTMESH_BRIEF_UNAVAILABLE\n" if worker.role == "worker-01" else "done\n",
            encoding="utf-8",
        )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        codex_compare,
        "_run_existing_worker_resume",
        lambda _arm, _root, workers: calls.append([worker.role for worker, _thread_id in workers]) or {"exit_code": 0},
    )
    monkeypatch.setattr(
        codex_compare,
        "_finalize_arm",
        lambda _arm, _parent: {"valid": True, "blocking_product_error": False},
    )

    result = codex_compare.resume_retained_workers(arm)

    assert calls == [["worker-01", "worker-03"]]
    assert result["resumed_workers"] == ["worker-01", "worker-03"]
    resume = Path(result["resume_artifact"])
    assert (resume / "worker-01" / "previous-stream.jsonl").is_file()
    assert (resume / "worker-04" / "previous-final.md").is_file()
    script = codex_compare._resume_workers_script(
        arm,
        resume / "launcher-check",
        tuple((worker, f"thread-{index}") for index, worker in enumerate(arm.workers[:1], start=1)),
    )
    script_text = script.read_text(encoding="utf-8")
    payload = script_text.split("$plans = '", 1)[1].split("' | ConvertFrom-Json", 1)[0]
    command = json.loads(payload)[0]["command"]
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[:2] == ["exec", "resume"]
    output_index = command.index("--output-last-message")
    assert command[output_index + 1] == json.loads(payload)[0]["resume_final"]


def test_worker_resume_skips_already_published_owner_briefs(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-published-owner-resume", tmp_path)
    prepare_round(plan)
    arm = next(candidate for candidate in plan.arms if candidate.arm == "contextmesh")
    worker = arm.workers[0]
    arm.broker_ledger.parent.mkdir(parents=True, exist_ok=True)
    arm.broker_ledger.write_text(
        json.dumps([{"brief_id": "brief-owned", "source_owner": worker.role}]), encoding="utf-8"
    )
    arm.broker_state.mkdir(parents=True, exist_ok=True)
    (arm.broker_state / "published.json").write_text(
        json.dumps({"brief_id": "brief-owned", "brief": {"binding": {"source_owner": worker.role}}}),
        encoding="utf-8",
    )

    assert codex_compare._owned_briefs_are_published(arm, worker) is True
    assert "do not call claim_source" in codex_compare._resume_worker_prompt(arm, worker)


def test_runner_source_change_stops_a_pending_arm_before_it_spends(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-source-freeze", tmp_path)
    prepare_round(plan)
    frozen, diagnostic = codex_compare._runner_source_is_frozen(plan)
    assert frozen is True and diagnostic is None

    monkeypatch.setattr(
        codex_compare,
        "_runner_source_snapshot",
        lambda _repo: {"schema_version": 1, "files": {}, "sha256": "changed"},
    )
    frozen, diagnostic = codex_compare._runner_source_is_frozen(plan)

    assert frozen is False
    assert diagnostic == "runner source changed after round preparation; retain this attempt and prepare a fresh round"


def test_contextmesh_gate_requires_each_exact_owner_and_peer_event(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    plans = freeze_worker_plans()
    entries = build_overlap_ledger(plans, manifest_sha256(plans), tuple(fixture_terra_plan(plan) for plan in plans))
    events = []
    for entry in entries:
        events.extend(
            (
                {"event": "source_claim_raw", "brief_id": entry.brief_id, "owner_id": entry.source_owner},
                {
                    "event": "brief_published",
                    "brief_id": entry.brief_id,
                    "owner_id": entry.source_owner,
                    "raw_size": 1000,
                    "brief_size": 100,
                    "max_peer_payload_bytes": 200,
                },
                *(
                    {"event": "brief_served", "brief_id": entry.brief_id, "peer_id": peer_id}
                    for peer_id in entry.peer_workers
                ),
            )
        )
    stream.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

    assert _contextmesh_evidence(stream, entries) is True
    events.append({"event": "brief_missing", "brief_id": entries[0].brief_id, "reason": "timeout"})
    stream.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    assert _contextmesh_evidence(stream, entries) is True
    events.append({"event": "brief_served", "brief_id": entries[0].brief_id, "peer_id": entries[0].peer_workers[0]})
    stream.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    assert _contextmesh_evidence(stream, entries) is True
    stream.write_text(json.dumps({"event": "brief_incomplete"}) + "\n", encoding="utf-8")
    assert _contextmesh_evidence(stream, entries) is False


def test_peer_brief_envelope_overage_is_a_contextmesh_product_violation() -> None:
    violations = _peer_brief_size_violations(
        [
            {
                "event": "brief_published",
                "brief_id": "brief-small",
                "raw_size": 359,
                "brief_size": 395,
                "max_peer_payload_bytes": 300,
            }
        ]
    )

    assert violations == [
        {"brief_id": "brief-small", "raw_size": 359, "brief_size": 395, "maximum_peer_bytes": 300}
    ]


def test_tiny_brief_contract_timeout_is_a_contextmesh_product_failure() -> None:
    failures = _brief_contract_failures(
        [
            {
                "event": "brief_repair_requested",
                "brief_id": "brief-small",
                "reason": "brief_incomplete: summary exceeds 209 byte content budget",
            },
            {"event": "brief_missing", "brief_id": "brief-small", "reason": "timeout"},
        ]
    )

    assert failures == [
        {"brief_id": "brief-small", "reason": "summary budget prevented required brief publication"}
    ]


def test_owner_publication_timeout_is_a_contextmesh_product_failure() -> None:
    failures = _brief_contract_failures(
        [{"event": "brief_missing", "brief_id": "brief-large", "reason": "timeout"}]
    )

    assert failures == [
        {"brief_id": "brief-large", "reason": "owner brief was not published before the broker deadline"}
    ]


def test_recovered_owner_publication_timeout_is_not_a_product_failure() -> None:
    failures = _brief_contract_failures(
        [
            {"event": "brief_missing", "brief_id": "brief-resumed", "reason": "timeout"},
            {"event": "brief_published", "brief_id": "brief-resumed"},
        ]
    )

    assert failures == []


def test_full_arm_zero_read_is_valid_and_retains_packet_insufficient_count(
    tmp_path: Path, monkeypatch
) -> None:
    plan = build_round_plan(ROOT, "comparison-r7", tmp_path)
    prepare_round(plan)
    arm = plan.arms[-1]

    hit = {
        "schema_version": 2,
        "event": "rrc_hit",
        "cache_key": "test-cache-key",
        "template_external_ref": "test-template-ref",
        "cache_warm": {
            "event": "rrc_cache_warm",
            "record_sha256": "warm-record-sha",
            "backend": "local_everos+rrc_runtime",
        },
        "task_ids": [worker.task_id for worker in plan.worker_plans],
        "lookup": {
            "backend": "local_everos+rrc_runtime",
            "matches": [{"task_id": worker.task_id} for worker in plan.worker_plans],
        },
    }
    hit["task_ids"] = list(reversed(hit["task_ids"]))
    hit["lookup"]["matches"] = list(reversed(hit["lookup"]["matches"]))
    arm.rrc_hit.parent.mkdir(parents=True, exist_ok=True)
    arm.rrc_hit.write_text(json.dumps(hit), encoding="utf-8")
    render_rrc_plans(arm.parent.target, arm.terra_plan_root, plan.worker_plans)
    prepare_dispatch(arm.arm, arm.root, arm.terra_plan_root)
    terminal = {"type": "turn.completed", "usage": {field: 0 for field in (*PRIMARY_FIELDS, *CACHE_FIELDS)}}
    arm.parent.stream.write_text(
        json.dumps({"type": "item.completed", "item": {"command": "python -m harness.rrc_hit --workspace . --output rrc-hit.json"}})
        + "\n"
        + json.dumps({"type": "item.completed", "item": {"command": "python -m harness.terra_plans render-rrc"}})
        + "\n"
        + json.dumps(terminal)
        + "\n",
        encoding="utf-8",
    )
    for worker in arm.workers:
        worker.stream.write_text(json.dumps(terminal) + "\n", encoding="utf-8")
    arm.mcp_health.write_text(json.dumps({"valid": True}) + "\n", encoding="utf-8")
    arm.mcp_model_probe.mkdir(parents=True)
    (arm.mcp_model_probe / "result.json").write_text(json.dumps({"valid": True}) + "\n", encoding="utf-8")
    arm.parent.final.write_text(f"TERRA_RRC_HIT_RENDER {hit['cache_key']}\n", encoding="utf-8")
    terra_plans = render_rrc_plans(arm.parent.target, arm.terra_plan_root, plan.worker_plans)
    entries = build_overlap_ledger(plan.worker_plans, manifest_sha256(plan.worker_plans), terra_plans)
    broker_events = []
    for entry in entries:
        broker_events.extend(
            (
                {
                    "event": "source_claim_raw",
                    "brief_id": entry.brief_id,
                    "owner_id": entry.source_owner,
                    "raw_size": 134_276 if entry.canonical_path == "ruleforge/policy_catalog.py" else 1_000,
                    "owner_view_size": 8_246 if entry.canonical_path == "ruleforge/policy_catalog.py" else 1_000,
                    "owner_view_kind": "plan_scoped_excerpt" if entry.canonical_path == "ruleforge/policy_catalog.py" else "full_file",
                    "source_chunk_count": 1,
                },
                {
                    "event": "brief_published",
                    "brief_id": entry.brief_id,
                    "owner_id": entry.source_owner,
                    "raw_size": 1000,
                    "brief_size": 100,
                    "max_peer_payload_bytes": 200,
                },
                *(
                    {"event": "brief_served", "brief_id": entry.brief_id, "peer_id": peer_id}
                    for peer_id in entry.peer_workers
                ),
            )
        )
    arm.broker_log.write_text("\n".join(json.dumps(event) for event in broker_events) + "\n", encoding="utf-8")
    for worker in arm.workers:
        worker.final.write_text("done\n", encoding="utf-8")
    arm.child_records.write_text(
        json.dumps(
            [
                {"role": worker.role, "pid": index + 1, "stream_completed": True}
                for index, worker in enumerate(arm.workers)
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        codex_compare.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="1 passed\n"),
    )
    monkeypatch.setattr(codex_compare, "_run_parent", lambda _arm: {"exit_code": 0})
    monkeypatch.setattr(codex_compare, "warm_rrc_cache", lambda *_args, **_kwargs: {"event": "rrc_cache_warm"})
    monkeypatch.setattr(
        codex_compare,
        "collect_codex_pilot",
        lambda *_args: {
            "valid": True,
            "totals": {field: 0 for field in (*PRIMARY_FIELDS, *CACHE_FIELDS)},
        },
    )

    result = codex_compare.execute_arm(arm)

    assert result["valid"] is True
    assert result["quality"]["gates"]["contextmesh_evidence"] is True
    assert result["quality"]["gates"]["plan_scoped_large_source"] is True
    assert result["packet_insufficient"] == 0
    assert _packet_insufficient_count(
        [arm.parent.cm_log, *(worker.cm_log for worker in arm.workers)]
    ) == 0


def test_large_catalog_quality_requires_a_single_compact_owner_view() -> None:
    plans = freeze_worker_plans()
    entries = build_overlap_ledger(plans, manifest_sha256(plans))
    catalog = next(entry for entry in entries if entry.canonical_path == "ruleforge/policy_catalog.py")
    events = [
        {
            "event": "source_claim_raw",
            "brief_id": catalog.brief_id,
            "owner_id": catalog.source_owner,
            "raw_size": 134_276,
            "owner_view_size": 8_246,
            "owner_view_kind": "plan_scoped_excerpt",
            "source_chunk_count": 1,
        }
    ]

    assert _plan_scoped_large_source_evidence(events, entries) is True
    events[0]["source_chunk_count"] = 9
    assert _plan_scoped_large_source_evidence(events, entries) is False


def test_terra_preflight_requires_every_declared_source_read_and_marker(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r8", tmp_path)
    prepare_round(plan)
    arm = plan.arms[0]
    for worker in plan.worker_plans:
        plan_path(arm.terra_plan_root, worker.worker_id).parent.mkdir(parents=True, exist_ok=True)
        plan_path(arm.terra_plan_root, worker.worker_id).write_text(
            json.dumps(
                {
                    "task_id": worker.task_id,
                    "worker_id": worker.worker_id,
                    "plan_steps": ["inspect source", "implement change"],
                    "source_facts": [
                        fact
                        for _, facts in worker.plan_fact_requirements
                        for fact in facts
                    ],
                }
            ),
            encoding="utf-8",
        )
    paths = sorted({path for worker in plan.worker_plans for path in worker.initial_read_paths})
    arm.parent.stream.write_text(
        "\n".join(
            json.dumps({"type": "item.completed", "item": {"command": f"Get-Content {path}"}})
            for path in paths
        )
        + "\n",
        encoding="utf-8",
    )
    marker = __import__("hashlib").sha256("\n".join(paths).encode("utf-8")).hexdigest()
    arm.parent.final.write_text(f"TERRA_RAW_PREFLIGHT {marker}\n", encoding="utf-8")

    evidence = codex_compare._terra_evidence(arm, plan.worker_plans)

    assert evidence["valid"] is True
    arm.parent.stream.write_text(
        "\n".join(
            json.dumps({"type": "item.completed", "item": {"command": f"Get-Content .\\\\{path}"}})
            for path in paths
        )
        + "\n",
        encoding="utf-8",
    )
    assert codex_compare._terra_evidence(arm, plan.worker_plans)["valid"] is True
    arm.parent.final.write_text("missing marker\n", encoding="utf-8")
    assert codex_compare._terra_evidence(arm, plan.worker_plans)["valid"] is False


def test_dispatch_error_ends_parent_poll_without_a_relaunch(tmp_path: Path, monkeypatch) -> None:
    plan = build_round_plan(ROOT, "comparison-r9", tmp_path)
    prepare_round(plan)
    arm = plan.arms[0]
    arm.dispatch_error.write_text("invalid Terra plan\n", encoding="utf-8")
    monkeypatch.setattr(codex_compare.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))

    result = codex_compare._run_parent(arm)

    assert result["dispatch_completed"] is False
    assert result["orchestration_error"] == "invalid Terra plan\n"


def test_started_attempt_is_retained_instead_of_being_relaunched(tmp_path: Path) -> None:
    plan = build_round_plan(ROOT, "comparison-r10", tmp_path)
    prepare_round(plan)
    arm = plan.arms[0]
    arm.root.joinpath("attempt.json").write_text("{}\n", encoding="utf-8")

    assert codex_compare._started_attempt(arm) is True
