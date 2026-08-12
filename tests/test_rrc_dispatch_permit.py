from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from rrc.dispatch_permit import (
    PRODUCT_CONTROLLER_MATRIX,
    PRODUCT_STAGE_MATRIX,
    AuthorityRef,
    DispatchPermitError,
    DispatchPermitV1,
    ProductAttemptDispatchRequestV5,
    ProductCellAttemptBindingV1,
    ProductCellDispatchRequestV1,
    ProductCellJournalCursorV1,
    ProductJournalCursorV2,
    ProductRootedAttemptAuthorityV2,
    ProductRootedAttemptJournalViewV1,
    authorize_capability,
    authorize_product,
    canonical_json,
    capability_manifest,
    cell_attempt_binding_value,
    dispatch_permit_value,
    generated_output_authority_v2,
    generated_output_authority_v3,
    product_call_id,
    product_config_manifest,
    rooted_attempt_authority_value,
)
from rrc.economic_authority import (
    economic_binding_overlay_v10,
    setup_accounting_v2,
    worker_context_attestation_v1,
    workload_core_v6,
)

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_PRODUCT_STAGE_MATRIX = {
    ("direct", "baseline", "direct", "baseline", 1): ("strong_code_medium", 1),
    ("direct", "cheap_alone", "direct", "cheap_alone", 1): ("small_code_low", 1),
    ("direct", "cheap_alone", "direct", "cheap_alone", 2): ("small_code_low", 2),
    ("direct", "cheap_alone", "direct", "cheap_alone", 3): ("small_code_low", 3),
    ("direct", "cascade", "direct", "cascade_cheap", 1): ("small_code_low", 1),
    ("direct", "cascade", "direct", "cascade_cheap", 2): ("small_code_low", 2),
    ("direct", "cascade", "direct", "cascade_cheap", 3): ("small_code_low", 3),
    ("direct", "cascade", "direct", "cascade_strong", 4): ("strong_code_medium", 4),
    ("direct", "rrc_cold", "miss", "spec", 1): ("strong_spec_low", 1),
    ("direct", "rrc_cold", "miss", "independent_tests", 2): ("small_tests_low", 2),
    ("direct", "rrc_cold", "miss", "implement", 3): ("small_code_low", 3),
    ("direct", "rrc_cold", "miss", "repair_1", 4): ("small_code_low", 4),
    ("direct", "rrc_cold", "miss", "repair_2", 5): ("small_code_low", 5),
    ("direct", "rrc_cold", "miss", "fallback_spec", 6): ("strong_spec_low", 6),
    ("direct", "rrc_cold", "miss", "fallback_independent_tests", 7): (
        "small_tests_low",
        7,
    ),
    ("direct", "rrc_cold", "miss", "fallback_implement", 8): ("small_code_low", 8),
    ("direct", "rrc_warm", "pre_retrieval", "metadata_fill", 1): (
        "small_metadata_low",
        1,
    ),
    ("direct", "rrc_warm", "miss", "spec", 2): ("strong_spec_low", 2),
    ("direct", "rrc_warm", "miss", "independent_tests", 3): ("small_tests_low", 3),
    ("direct", "rrc_warm", "miss", "implement", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "miss", "repair_1", 5): ("small_code_low", 5),
    ("direct", "rrc_warm", "miss", "repair_2", 6): ("small_code_low", 6),
    ("direct", "rrc_warm", "miss", "fallback_spec", 7): ("strong_spec_low", 7),
    ("direct", "rrc_warm", "miss", "fallback_independent_tests", 8): (
        "small_tests_low",
        8,
    ),
    ("direct", "rrc_warm", "miss", "fallback_implement", 9): ("small_code_low", 9),
    ("direct", "rrc_warm", "reuse", "implement", 2): ("small_code_low", 2),
    ("direct", "rrc_warm", "reuse", "repair_1", 3): ("small_code_low", 3),
    ("direct", "rrc_warm", "reuse", "repair_2", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "reuse", "fallback_spec", 5): ("strong_spec_low", 5),
    ("direct", "rrc_warm", "reuse", "fallback_independent_tests", 6): (
        "small_tests_low",
        6,
    ),
    ("direct", "rrc_warm", "reuse", "fallback_implement", 7): ("small_code_low", 7),
    ("direct", "rrc_warm", "prime", "prime", 2): ("small_spec_low", 2),
    ("direct", "rrc_warm", "prime", "independent_tests", 3): ("small_tests_low", 3),
    ("direct", "rrc_warm", "prime", "implement", 4): ("small_code_low", 4),
    ("direct", "rrc_warm", "prime", "repair_1", 5): ("small_code_low", 5),
    ("direct", "rrc_warm", "prime", "repair_2", 6): ("small_code_low", 6),
    ("direct", "rrc_warm", "prime", "fallback_spec", 7): ("strong_spec_low", 7),
    ("direct", "rrc_warm", "prime", "fallback_independent_tests", 8): (
        "small_tests_low",
        8,
    ),
    ("direct", "rrc_warm", "prime", "fallback_implement", 9): ("small_code_low", 9),
    ("contextmesh", "rrc_cold", "combined", "contextmesh_root_session", 1): (
        "root_strong_medium_native",
        1,
    ),
    ("contextmesh", "rrc_warm", "combined", "contextmesh_root_session", 1): (
        "root_strong_medium_native",
        1,
    ),
    ("contextmesh", "rrc_cold", "miss", "implement", 3): (
        "worker_small_low_native",
        3,
    ),
    ("contextmesh", "rrc_warm", "miss", "implement", 4): (
        "worker_small_low_native",
        4,
    ),
    ("contextmesh", "rrc_warm", "reuse", "implement", 2): (
        "worker_small_low_native",
        2,
    ),
    ("contextmesh", "rrc_warm", "prime", "implement", 4): (
        "worker_small_low_native",
        4,
    ),
}


def _expected_controller_matrix() -> frozenset[
    tuple[str, str, str, str, str, str, int, str, int, str]
]:
    rows: set[tuple[str, str, str, str, str, str, int, str, int, str]] = set()
    for scope in ("experiment", "interactive"):
        for controller in ("direct", "contextmesh"):
            for (transport, arm, branch, stage, ordinal), (
                surface,
                cursor,
            ) in EXPECTED_PRODUCT_STAGE_MATRIX.items():
                if scope == "interactive" and arm not in {"rrc_cold", "rrc_warm"}:
                    continue
                allowed = (
                    transport == "direct"
                    if controller == "direct"
                    else arm in {"rrc_cold", "rrc_warm"}
                    and (
                        transport == "contextmesh" or transport == "direct" and stage != "implement"
                    )
                )
                if allowed:
                    rows.add(
                        (
                            scope,
                            controller,
                            transport,
                            arm,
                            branch,
                            stage,
                            ordinal,
                            surface,
                            cursor,
                            "cell" if stage == "contextmesh_root_session" else "attempt",
                        )
                    )
    return frozenset(rows)


EXPECTED_PRODUCT_CONTROLLER_MATRIX = _expected_controller_matrix()
EXPECTED_PRODUCT_CONTROLLER_MATRIX_SHA256 = (
    "89b715661ddae0cd7994d823bbfc7fd43e97f8f8ee05924cfd8e730c8522179b"
)
EXPECTED_PRODUCT_CONTROLLER_COUNTS = {
    ("experiment", "direct", "direct"): 39,
    ("interactive", "direct", "direct"): 31,
    ("experiment", "contextmesh", "direct"): 27,
    ("interactive", "contextmesh", "direct"): 27,
    ("experiment", "contextmesh", "contextmesh"): 6,
    ("interactive", "contextmesh", "contextmesh"): 6,
}


class _TestCellJournalAuthority:
    """Minimal trusted-port fixture; only its transition methods can publish lookup rows."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._started: dict[
            str,
            tuple[
                ProductCellDispatchRequestV1,
                int,
                AuthorityRef,
                AuthorityRef,
                AuthorityRef,
            ],
        ] = {}
        self._bindings: dict[tuple[str, str], ProductRootedAttemptJournalViewV1] = {}
        self._completed: dict[tuple[str, str], str] = {}

    def mark_root_started(
        self,
        root: ProductCellDispatchRequestV1,
        *,
        generation: int,
        permit: DispatchPermitV1 | None = None,
    ) -> None:
        if permit is None:
            permit = DispatchPermitV1(
                kind="product",
                call_id=root.call_id,
                surface_id="root_strong_medium_native",
                manifest_sha256="a" * 64,
                plan_review_seal_sha256="b" * 64,
                authority_set_sha256="c" * 64,
            )
        permit_ref = _write(
            self.root / f"{root.call_id}.permit.json", dispatch_permit_value(permit)
        )
        launch_ref = _write(
            self.root / f"{root.call_id}.launch.json",
            {
                "v": 1,
                "kind": "rrcv2_product_root_launch_identity",
                "cell_id": root.cell_id,
                "root_call_id": root.call_id,
                "session_id": f"session-{root.cell_id}",
                "transcript_baseline_sha256": hashlib.sha256(root.call_id.encode()).hexdigest(),
            },
        )
        started_ref = _write(
            self.root / f"{root.call_id}.started.json",
            {
                "v": 1,
                "kind": "rrcv2_product_root_call_started",
                "cell_id": root.cell_id,
                "root_call_id": root.call_id,
                "state": "started",
                "generation": generation,
                "root_permit_sha256": permit_ref.sha256,
                "root_launch_identity_sha256": launch_ref.sha256,
            },
        )
        self._started[root.call_id] = (
            root,
            generation,
            permit_ref,
            started_ref,
            launch_ref,
        )

    def bind_attempt(
        self,
        *,
        root: ProductCellDispatchRequestV1,
        binding: ProductCellAttemptBindingV1,
        binding_ref: AuthorityRef,
    ) -> None:
        started = self._started.get(root.call_id)
        if started is None:
            raise RuntimeError("root call was not marked started")
        started_root, root_generation, permit_ref, started_ref, launch_ref = started
        if started_root != root:
            raise RuntimeError("root call identity changed")
        key = (binding.cell_id, binding.attempt_id)
        if key in self._bindings:
            raise RuntimeError("attempt binding already exists")
        spawn_ref = _write(
            self.root / f"{binding.attempt_id}.spawn.json",
            {
                "v": 1,
                "kind": "rrcv2_contextmesh_spawn_observed",
                "cell_id": binding.cell_id,
                "root_call_id": binding.root_call_id,
                "root_call_generation": root_generation,
                "tool_use_id": binding.tool_use_id,
                "attempt_id": binding.attempt_id,
                "task_envelope_sha256": binding.task_envelope_sha256,
                "binding_generation": binding.generation,
                "state": "spawn_observed",
            },
        )
        authority = ProductRootedAttemptAuthorityV2(
            cell_id=binding.cell_id,
            root_call_id=binding.root_call_id,
            root_call_state="started",
            root_call_generation=root_generation,
            root_permit_sha256=permit_ref.sha256,
            root_started_sha256=started_ref.sha256,
            root_launch_identity_sha256=launch_ref.sha256,
            tool_use_id=binding.tool_use_id,
            tool_event_sha256=spawn_ref.sha256,
            tool_event_state="spawn_observed",
            attempt_id=binding.attempt_id,
            task_envelope_sha256=binding.task_envelope_sha256,
            binding_sha256=binding_ref.sha256,
            binding_generation=binding.generation,
            agent_id=None,
        )
        authority_ref = _write(
            self.root / f"{binding.attempt_id}.rooted-attempt-authority.json",
            rooted_attempt_authority_value(authority),
        )
        self._bindings[key] = ProductRootedAttemptJournalViewV1(
            authority_ref=authority_ref,
            root_permit_ref=permit_ref,
            root_started_ref=started_ref,
            root_launch_identity_ref=launch_ref,
            spawn_event_ref=spawn_ref,
        )

    def load_rooted_attempt_authority(
        self, *, cell_id: str, attempt_id: str
    ) -> ProductRootedAttemptJournalViewV1 | None:
        return self._bindings.get((cell_id, attempt_id))

    def complete_bound_attempt(self, *, cell_id: str, attempt_id: str, agent_id: str) -> None:
        key = (cell_id, attempt_id)
        if key not in self._bindings or not agent_id or key in self._completed:
            raise RuntimeError("bound attempt cannot be completed")
        self._completed[key] = agent_id


def _write(path: Path, value: object) -> AuthorityRef:
    raw = canonical_json(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return AuthorityRef(
        path=path, sha256=__import__("hashlib").sha256(raw).hexdigest(), bytes=len(raw)
    )


def _write_compact(path: Path, value: object) -> AuthorityRef:
    raw = canonical_json(value)[:-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)
    return AuthorityRef(path=path, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def _raw_ref(path: Path, raw: bytes, *, mode: int = 0o600) -> AuthorityRef:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return AuthorityRef(path=path, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def _existing_ref(path: Path) -> AuthorityRef:
    raw = path.read_bytes()
    return AuthorityRef(path=path, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def _plan_seal(tmp_path: Path) -> AuthorityRef:
    return _write(
        tmp_path / "plan.seal.json",
        {
            "v": 1,
            "kind": "rrcv2_raw_plan_review_seal",
            "verdict": "SHIP",
            "origin": "forked",
            "mode": "unleashed",
            "repo_root": str(tmp_path),
            "raw_plan_sha256": "1" * 64,
            "record_sha256": "2" * 64,
            "record_subject_hash": "3" * 64,
            "transcript_sha256": "4" * 64,
            "plan_path": str(tmp_path / "PLAN.md"),
            "record_path": str(tmp_path / ".generated/state/reviews/plan.toml"),
            "transcript_path": str(tmp_path / "review.txt"),
        },
    )


def _surface_hashes(seed: str) -> dict[str, dict[str, str]]:
    names = (
        "argv_sha256",
        "config_sha256",
        "tool_schema_sha256",
        "output_schema_sha256",
        "prompt_wrapper_sha256",
        "assembled_byte_definition_sha256",
    )
    return {
        surface_id: {name: seed * 64 for name in names}
        for surface_id in (
            "root_strong_medium_native",
            "small_code_low",
            "small_metadata_low",
            "small_spec_low",
            "small_tests_low",
            "strong_code_medium",
            "strong_spec_low",
            "worker_small_low_native",
        )
    }


def test_capability_permit_accepts_only_exact_registered_pair(tmp_path: Path) -> None:
    plan = _plan_seal(tmp_path)
    manifest_value = capability_manifest(
        pre_capability_plan_review_seal_sha256=plan.sha256,
        cli_binary_sha256="a" * 64,
        surface_hashes=_surface_hashes("b"),
    )
    manifest = _write(tmp_path / "manifest.json", manifest_value)

    permit = authorize_capability(
        call_id="cap-09-worker-small-low-native",
        surface_id="worker_small_low_native",
        manifest_ref=manifest,
        plan_review_seal_ref=plan,
        expected_manifest=manifest_value,
    )
    assert permit.kind == "capability"
    assert permit.call_id == "cap-09-worker-small-low-native"
    assert permit.manifest_sha256 == manifest.sha256


@pytest.mark.parametrize(
    ("call_id", "surface_id"),
    [
        ("cap-09-worker-small-low-native", "strong_spec_low"),
        ("task-001", "worker_small_low_native"),
        ("cap-10-extra", "worker_small_low_native"),
    ],
)
def test_capability_permit_rejects_renamed_or_cross_kind_calls(
    tmp_path: Path, call_id: str, surface_id: str
) -> None:
    plan = _plan_seal(tmp_path)
    value = capability_manifest(
        pre_capability_plan_review_seal_sha256=plan.sha256,
        cli_binary_sha256="a" * 64,
        surface_hashes=_surface_hashes("b"),
    )
    manifest = _write(tmp_path / "manifest.json", value)
    with pytest.raises(DispatchPermitError):
        authorize_capability(
            call_id=call_id,
            surface_id=surface_id,
            manifest_ref=manifest,
            plan_review_seal_ref=plan,
            expected_manifest=value,
        )


def test_capability_permit_reopens_every_authority_fail_closed(tmp_path: Path) -> None:
    plan = _plan_seal(tmp_path)
    value = capability_manifest(
        pre_capability_plan_review_seal_sha256=plan.sha256,
        cli_binary_sha256="a" * 64,
        surface_hashes=_surface_hashes("b"),
    )
    manifest = _write(tmp_path / "manifest.json", value)

    manifest.path.write_bytes(manifest.path.read_bytes() + b" ")
    with pytest.raises(DispatchPermitError):
        authorize_capability(
            call_id="cap-01-strong-code-medium",
            surface_id="strong_code_medium",
            manifest_ref=manifest,
            plan_review_seal_ref=plan,
            expected_manifest=value,
        )

    manifest.path.unlink()
    os.mkfifo(manifest.path)
    with pytest.raises(DispatchPermitError):
        authorize_capability(
            call_id="cap-01-strong-code-medium",
            surface_id="strong_code_medium",
            manifest_ref=manifest,
            plan_review_seal_ref=plan,
            expected_manifest=value,
        )


def test_manifest_constructor_rejects_missing_or_unknown_surface_hashes() -> None:
    hashes = _surface_hashes("b")
    hashes.pop("strong_spec_low")
    with pytest.raises(DispatchPermitError):
        capability_manifest(
            pre_capability_plan_review_seal_sha256="1" * 64,
            cli_binary_sha256="a" * 64,
            surface_hashes=hashes,
        )


def _review_record(*, kind: str, scope: str, transcript_sha256: str, root: Path) -> bytes:
    return (
        f'kind = "{kind}"\n'
        f'scope = "{scope}"\n'
        'verdict = "SHIP"\n'
        f'subject_hash = "{"a" * 64}"\n'
        f'transcript_hash = "{transcript_sha256}"\n'
        'origin = "forked"\n'
        'mode = "unleashed"\n'
        'audience_entry_id = ""\n'
        'persona = ""\n'
        'recorded_at = "2026-08-11T00:00:00Z"\n'
        f'repo_root = "{root}"\n'
        'workspace_session = ""\n'
        'workspace_runtime_root = ""\n'
    ).encode()


def _product_authorities(tmp_path: Path) -> dict[str, object]:
    workload_path = ROOT / "contextmesh/bench/rrcv2_workload.json"
    oracles_path = ROOT / "contextmesh/bench/rrcv2_oracles.json"
    capability_manifest_path = (
        ROOT / ".generated/state/rrcv2-convergence/capability/capability-manifest.v1.json"
    )
    capability_summary_path = (
        ROOT / ".generated/state/rrcv2-convergence/capability/capability-summary.v1.json"
    )
    redesign_path = (
        ROOT
        / ".generated/state/rrcv2-convergence/capability/explore-strong/redesign-evidence.v2.json"
    )
    inventory_path = (
        ROOT / ".generated/state/rrcv2-convergence/verify/capability-evidence-inventory.v1.json"
    )
    sandbox_v2_path = ROOT / ".generated/state/rrcv2-convergence/verify/sandbox-evidence.v2.json"
    setup_path = ROOT / ".generated/state/rrcv2-convergence/verify/setup-accounting.v1.json"
    abandoned_path = (
        ROOT
        / ".generated/state/rrcv2-convergence/verify/aborted-capability-attempt-1/archive-manifest.json"
    )
    analyzer_path = ROOT / "contextmesh/bench/rrcv2_analyzer.py"
    workload_raw = workload_path.read_bytes()
    oracles_raw = oracles_path.read_bytes()
    workload = json.loads(workload_raw)
    core_value = workload_core_v6(
        source_workload_path="contextmesh/bench/rrcv2_workload.json",
        source_workload_raw=workload_raw,
        source_oracles_path="contextmesh/bench/rrcv2_oracles.json",
        source_oracles_raw=oracles_raw,
    )
    core = _write(tmp_path / "workload-core.json", core_value)
    capability_manifest = _raw_ref(
        tmp_path / "capability-manifest.json", capability_manifest_path.read_bytes()
    )
    capability_summary = _raw_ref(
        tmp_path / "capability-summary.json", capability_summary_path.read_bytes()
    )
    redesign = _raw_ref(tmp_path / "redesign.json", redesign_path.read_bytes())
    inventory = _raw_ref(tmp_path / "inventory.json", inventory_path.read_bytes())
    sandbox_v2 = _raw_ref(tmp_path / "sandbox-v2.json", sandbox_v2_path.read_bytes())
    setup = _raw_ref(tmp_path / "setup.json", setup_path.read_bytes())
    abandoned = _raw_ref(tmp_path / "abandoned.json", abandoned_path.read_bytes())
    output_authority_v1 = _raw_ref(
        tmp_path / "generated-output-authority.v1.json",
        (
            ROOT / ".generated/state/rrcv2-convergence/generated-output-authority.v1.json"
        ).read_bytes(),
    )
    output_authority_v2 = _write(
        tmp_path / "generated-output-authority.v2.json",
        generated_output_authority_v2(supersedes_sha256=output_authority_v1.sha256),
    )
    output_authority_v3 = _write(
        tmp_path / "generated-output-authority.v3.json",
        generated_output_authority_v3(supersedes_sha256=output_authority_v2.sha256),
    )
    redesign_value = json.loads(redesign.path.read_text())
    abandoned_value = json.loads(abandoned.path.read_text())
    summary_value = json.loads(capability_summary.path.read_text())
    setup_v2 = _write(
        tmp_path / "setup-v2.json",
        setup_accounting_v2(
            abandoned_manifest_sha256=abandoned.sha256,
            abandoned_usage=abandoned_value["usage"],
            abandoned_rejections_without_usage=abandoned_value[
                "provider_schema_rejections_without_usage"
            ],
            redesign_evidence_sha256=redesign.sha256,
            redesign_usage=redesign_value["provider_usage"],
            capability_summary_sha256=capability_summary.sha256,
            canonical_usage=summary_value["results"],
        ),
    )
    root_call = (
        ROOT
        / ".generated/state/rrcv2-convergence/capability/calls/cap-08-root-strong-medium-native"
    )
    worker_call = (
        ROOT / ".generated/state/rrcv2-convergence/capability/calls/cap-09-worker-small-low-native"
    )
    root_rollout = _raw_ref(
        tmp_path / "root-rollout.jsonl", (root_call / "rollout.jsonl").read_bytes()
    )
    worker_rollout = _raw_ref(
        tmp_path / "worker-rollout.jsonl", (worker_call / "rollout.jsonl").read_bytes()
    )
    root_environment = _raw_ref(
        tmp_path / "root-environment.json", (root_call / "environment.json").read_bytes()
    )
    worker_environment = _raw_ref(
        tmp_path / "worker-environment.json", (worker_call / "environment.json").read_bytes()
    )
    worker_attestation = _write(
        tmp_path / "worker-attestation.json",
        worker_context_attestation_v1(
            root_rollout_raw=root_rollout.path.read_bytes(),
            worker_rollout_raw=worker_rollout.path.read_bytes(),
            root_environment_raw=root_environment.path.read_bytes(),
            worker_environment_raw=worker_environment.path.read_bytes(),
        ),
    )
    overlay_value = economic_binding_overlay_v10(
        workload_core_sha256=core.sha256,
        source_workload_sha256=hashlib.sha256(workload_raw).hexdigest(),
        source_oracles_sha256=hashlib.sha256(oracles_raw).hexdigest(),
        capability_manifest_sha256=capability_manifest.sha256,
        capability_summary_sha256=capability_summary.sha256,
        capability_evidence_inventory_sha256=inventory.sha256,
        sandbox_evidence_v2_sha256=sandbox_v2.sha256,
        setup_accounting_sha256=setup.sha256,
        generated_output_authority_v2_sha256=output_authority_v2.sha256,
        redesign_evidence_sha256=redesign.sha256,
        rate_payload_sha256=workload["economic_evidence"]["rate_payload_sha256"],
        analyzer_file_sha256=_existing_ref(analyzer_path).sha256,
        analysis_sha256=workload["protocol"]["analysis_sha256"],
        setup_accounting_v2_sha256=setup_v2.sha256,
        worker_context_attestation_sha256=worker_attestation.sha256,
        generated_output_authority_v3_sha256=output_authority_v3.sha256,
    )
    overlay = _write(tmp_path / "overlay.json", overlay_value)

    plan = _raw_ref(tmp_path / "PLAN.md", b"reviewed plan\n", mode=0o644)
    plan_transcript = _raw_ref(tmp_path / "plan-review.txt", b"VERDICT: SHIP\n")
    plan_record = _raw_ref(
        tmp_path / ".generated/state/reviews/plan.toml",
        _review_record(
            kind="plan", scope="plan", transcript_sha256=plan_transcript.sha256, root=tmp_path
        ),
        mode=0o644,
    )
    plan_seal = _write(
        tmp_path / "plan-review.seal.json",
        {
            "v": 1,
            "kind": "rrcv2_raw_plan_review_seal",
            "verdict": "SHIP",
            "origin": "forked",
            "mode": "unleashed",
            "repo_root": str(tmp_path),
            "raw_plan_sha256": plan.sha256,
            "record_sha256": plan_record.sha256,
            "record_subject_hash": "a" * 64,
            "transcript_sha256": plan_transcript.sha256,
            "plan_path": str(tmp_path / "PLAN.md"),
            "record_path": str(tmp_path / ".generated/state/reviews/plan.toml"),
            "transcript_path": str(tmp_path / "plan-review.txt"),
        },
    )
    claim_transcript = _raw_ref(
        tmp_path / "claim-review.txt", b"CLAIM: SUPPORTED\nFunctional only; no savings.\n"
    )
    claim_record = _raw_ref(
        tmp_path / ".generated/state/reviews/claim-worktree.toml",
        _review_record(
            kind="claim",
            scope="worktree",
            transcript_sha256=claim_transcript.sha256,
            root=tmp_path,
        ),
        mode=0o644,
    )
    economic_file = _raw_ref(tmp_path / "economic.py", b"economic\n", mode=0o644)
    permit_file = _raw_ref(tmp_path / "permit.py", b"permit\n", mode=0o644)
    matrix_file = _raw_ref(tmp_path / "matrix.py", b"matrix\n", mode=0o644)
    sandbox_probe_file = _raw_ref(tmp_path / "sandbox.py", b"sandbox\n", mode=0o644)
    config = _write(
        tmp_path / "config.json",
        product_config_manifest(
            economic_authority_file_sha256=economic_file.sha256,
            dispatch_permit_file_sha256=permit_file.sha256,
            capability_matrix_file_sha256=matrix_file.sha256,
            sandbox_probe_file_sha256=sandbox_probe_file.sha256,
        ),
    )
    workload_task = next(row for row in workload["tasks"] if row["task_id"] == "bound-temperature")
    task_envelope = _write_compact(
        tmp_path / "task-envelope.json",
        {
            "v": 1,
            "task": {
                "task_id": workload_task["task_id"],
                "text": workload_task["task_text"],
                "family": workload_task["family"],
                "artifact_path": workload_task["artifact_path"],
                "searchable_public": False,
                "verification_profile": workload_task["profile"],
                "primary": workload_task["primary"],
            },
            "source_ref": {
                "sha256": workload_task["source_sha256"],
                "bytes": len(workload_task["starter_source"].encode()),
                "path": workload_task["artifact_path"],
            },
            "public_test_ref": {
                "sha256": workload_task["public_tests_sha256"],
                "bytes": 1,
                "path": ".rrcv2/public-tests.v1.json",
            },
            "oracle_ref": {
                "sha256": workload_task["oracle_sha256"],
                "bytes": 1,
                "path": ".rrcv2/oracle-tests.v1.json",
            },
            "target_preimage": {"v": 1, "kind": "none"},
            "shape": workload_task["shape"],
            "slot_values": workload_task["slot_values"],
        },
    )
    request = ProductAttemptDispatchRequestV5(
        call_id="",
        scope="experiment",
        controller="direct",
        task_id="bound-temperature",
        task_envelope_sha256=task_envelope.sha256,
        root_binding_sha256=None,
        run_id="run-001",
        replicate_id="r01",
        arm="baseline",
        branch="direct",
        stage="baseline",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id="run-001:r01:baseline:direct",
        attempt_id="b" * 64,
        transport="direct",
        surface_id="strong_code_medium",
    )
    request = replace(request, call_id=product_call_id(request))
    journal_cursor = ProductJournalCursorV2(
        attempt_id=request.attempt_id,
        cell_id=request.cell_id,
        call_id=request.call_id,
        stage_ordinal=request.stage_ordinal,
        journal_generation=0,
        prior_state="absent",
        root_binding_sha256=None,
        root_binding_generation=None,
    )
    return {
        "request": request,
        "journal_cursor": journal_cursor,
        "task_envelope_ref": task_envelope,
        "cell_attempt_binding_ref": None,
        "cell_journal_authority": None,
        "repo_root": tmp_path,
        "workload_core_ref": core,
        "overlay_ref": overlay,
        "source_workload_ref": _existing_ref(workload_path),
        "source_oracles_ref": _existing_ref(oracles_path),
        "capability_manifest_ref": capability_manifest,
        "capability_summary_ref": capability_summary,
        "capability_evidence_inventory_ref": inventory,
        "sandbox_evidence_v2_ref": sandbox_v2,
        "setup_accounting_ref": setup,
        "setup_accounting_v2_ref": setup_v2,
        "worker_context_attestation_ref": worker_attestation,
        "root_rollout_ref": root_rollout,
        "worker_rollout_ref": worker_rollout,
        "root_environment_ref": root_environment,
        "worker_environment_ref": worker_environment,
        "generated_output_authority_v3_ref": output_authority_v3,
        "generated_output_authority_v2_ref": output_authority_v2,
        "generated_output_authority_v1_ref": output_authority_v1,
        "abandoned_manifest_ref": abandoned,
        "redesign_evidence_ref": redesign,
        "analyzer_ref": _existing_ref(analyzer_path),
        "plan_review_seal_ref": plan_seal,
        "plan_ref": plan,
        "plan_transcript_ref": plan_transcript,
        "plan_record_ref": plan_record,
        "claim_transcript_ref": claim_transcript,
        "claim_record_ref": claim_record,
        "config_manifest_ref": config,
        "economic_authority_file_ref": economic_file,
        "dispatch_permit_file_ref": permit_file,
        "capability_matrix_file_ref": matrix_file,
        "sandbox_probe_file_ref": sandbox_probe_file,
    }


def test_product_permit_requires_complete_functional_only_authority(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    permit = authorize_product(**authorities)  # type: ignore[arg-type]
    assert permit.kind == "product"
    assert permit.call_id == authorities["request"].call_id  # type: ignore[union-attr]
    assert len(permit.authority_set_sha256) == 64

    for field in (
        "claim_transcript_ref",
        "overlay_ref",
        "capability_summary_ref",
        "setup_accounting_v2_ref",
        "worker_context_attestation_ref",
        "generated_output_authority_v3_ref",
        "config_manifest_ref",
    ):
        poisoned = dict(authorities)
        ref = poisoned[field]
        assert isinstance(ref, AuthorityRef)
        ref.path.write_bytes(ref.path.read_bytes() + b" ")
        with pytest.raises(DispatchPermitError):
            authorize_product(**poisoned)  # type: ignore[arg-type]
        ref.path.write_bytes(ref.path.read_bytes()[:-1])


def test_product_permit_rejects_newline_added_task_envelope(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    compact = authorities["task_envelope_ref"]
    request = authorities["request"]
    assert isinstance(compact, AuthorityRef)
    assert isinstance(request, ProductAttemptDispatchRequestV5)
    newline_ref = _raw_ref(tmp_path / "newline-envelope.json", compact.path.read_bytes() + b"\n")
    request = replace(request, call_id="", task_envelope_sha256=newline_ref.sha256)
    request = replace(request, call_id=product_call_id(request))
    authorities["task_envelope_ref"] = newline_ref
    authorities["request"] = request
    authorities["journal_cursor"] = ProductJournalCursorV2(
        request.attempt_id,
        request.cell_id,
        request.call_id,
        request.stage_ordinal,
        0,
        "absent",
        None,
        None,
    )

    with pytest.raises(DispatchPermitError, match="canonical JSON"):
        authorize_product(**authorities)  # type: ignore[arg-type]


def test_product_permit_rejects_non_nfc_task_envelope(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    compact = authorities["task_envelope_ref"]
    request = authorities["request"]
    assert isinstance(compact, AuthorityRef)
    assert isinstance(request, ProductAttemptDispatchRequestV5)
    value = json.loads(compact.path.read_bytes())
    value["task"]["text"] += "e\u0301"
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    decomposed_ref = _raw_ref(tmp_path / "decomposed-envelope.json", raw)
    request = replace(request, call_id="", task_envelope_sha256=decomposed_ref.sha256)
    request = replace(request, call_id=product_call_id(request))
    authorities["task_envelope_ref"] = decomposed_ref
    authorities["request"] = request
    authorities["journal_cursor"] = ProductJournalCursorV2(
        request.attempt_id,
        request.cell_id,
        request.call_id,
        request.stage_ordinal,
        0,
        "absent",
        None,
        None,
    )

    with pytest.raises(DispatchPermitError, match="canonical JSON"):
        authorize_product(**authorities)  # type: ignore[arg-type]


@pytest.mark.parametrize("mutation", ("duplicate_key", "float", "nan"))
def test_product_permit_rejects_noncanonical_json_domain_task_envelope(
    tmp_path: Path, mutation: str
) -> None:
    authorities = _product_authorities(tmp_path)
    compact = authorities["task_envelope_ref"]
    request = authorities["request"]
    assert isinstance(compact, AuthorityRef)
    assert isinstance(request, ProductAttemptDispatchRequestV5)
    if mutation == "duplicate_key":
        raw = b'{"v":1,' + compact.path.read_bytes()[1:]
    else:
        value = json.loads(compact.path.read_bytes())
        value["shape"]["arity"] = 1.0 if mutation == "float" else float("nan")
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    invalid_ref = _raw_ref(tmp_path / f"{mutation}-envelope.json", raw)
    request = replace(request, call_id="", task_envelope_sha256=invalid_ref.sha256)
    request = replace(request, call_id=product_call_id(request))
    authorities["task_envelope_ref"] = invalid_ref
    authorities["request"] = request
    authorities["journal_cursor"] = ProductJournalCursorV2(
        request.attempt_id,
        request.cell_id,
        request.call_id,
        request.stage_ordinal,
        0,
        "absent",
        None,
        None,
    )

    with pytest.raises(DispatchPermitError, match="canonical JSON"):
        authorize_product(**authorities)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutation", ("task", "surface", "stage", "arm", "branch", "cursor", "duplicate")
)
def test_product_permit_rejects_cross_arm_stage_surface_or_unbound_call(
    tmp_path: Path, mutation: str
) -> None:
    authorities = _product_authorities(tmp_path)
    request = authorities["request"]
    assert isinstance(request, ProductAttemptDispatchRequestV5)
    if mutation == "task":
        request = replace(request, task_id="unknown-task")
    elif mutation == "surface":
        request = replace(request, surface_id="small_code_low")
    elif mutation == "stage":
        request = replace(request, stage="repair_1")
    elif mutation == "arm":
        request = replace(request, arm="cheap_alone")
    elif mutation == "branch":
        request = replace(request, branch="miss")
    elif mutation == "cursor":
        request = replace(request, journal_cursor=2)
    else:
        request = replace(request, stage_ordinal=2, journal_cursor=2)
    request = replace(request, call_id=product_call_id(request))
    authorities["request"] = request
    authorities["journal_cursor"] = ProductJournalCursorV2(
        attempt_id=request.attempt_id,
        cell_id=request.cell_id,
        call_id=request.call_id,
        stage_ordinal=request.stage_ordinal,
        journal_generation=0,
        prior_state="absent",
        root_binding_sha256=None,
        root_binding_generation=None,
    )
    with pytest.raises(DispatchPermitError):
        authorize_product(**authorities)  # type: ignore[arg-type]


def test_product_permit_supports_unique_multi_stage_same_task(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    first = replace(
        baseline,
        arm="rrc_cold",
        branch="miss",
        stage="spec",
        surface_id="strong_spec_low",
        cell_id="run-001:r01:rrc_cold:direct",
        attempt_id="c" * 64,
    )
    first = replace(first, call_id=product_call_id(first))
    second = replace(
        first,
        stage="independent_tests",
        stage_ordinal=2,
        journal_cursor=2,
        surface_id="small_tests_low",
    )
    second = replace(second, call_id=product_call_id(second))
    authorities["request"] = first
    authorities["journal_cursor"] = ProductJournalCursorV2(
        first.attempt_id, first.cell_id, first.call_id, first.stage_ordinal, 0, "absent", None, None
    )
    permit_one = authorize_product(**authorities)  # type: ignore[arg-type]
    authorities["request"] = second
    authorities["journal_cursor"] = ProductJournalCursorV2(
        second.attempt_id,
        second.cell_id,
        second.call_id,
        second.stage_ordinal,
        1,
        "absent",
        None,
        None,
    )
    permit_two = authorize_product(**authorities)  # type: ignore[arg-type]
    assert permit_one.call_id != permit_two.call_id
    assert (permit_one.surface_id, permit_two.surface_id) == (
        "strong_spec_low",
        "small_tests_low",
    )

    invalid_fallback_alias = replace(
        second,
        arm="rrc_warm",
        branch="prime",
        stage="independent_tests",
        stage_ordinal=8,
        journal_cursor=8,
    )
    invalid_fallback_alias = replace(
        invalid_fallback_alias, call_id=product_call_id(invalid_fallback_alias)
    )
    authorities["request"] = invalid_fallback_alias
    authorities["journal_cursor"] = ProductJournalCursorV2(
        invalid_fallback_alias.attempt_id,
        invalid_fallback_alias.cell_id,
        invalid_fallback_alias.call_id,
        invalid_fallback_alias.stage_ordinal,
        2,
        "absent",
        None,
        None,
    )
    with pytest.raises(DispatchPermitError):
        authorize_product(**authorities)  # type: ignore[arg-type]


def _contextmesh_envelope(tmp_path: Path, inline: AuthorityRef, name: str) -> AuthorityRef:
    value = json.loads(inline.path.read_bytes())
    source_ref = value["source_ref"]
    artifact_path = value["task"]["artifact_path"]
    value["target_preimage"] = (
        {
            "v": 1,
            "kind": "regular",
            "path": artifact_path,
            "sha256": source_ref["sha256"],
            "bytes": source_ref["bytes"],
            "mode": 0o644,
        }
        if source_ref is not None
        else {"v": 1, "kind": "absent", "path": artifact_path, "mode": 0o644}
    )
    return _write_compact(tmp_path / name, value)


def _root_request(
    request: ProductAttemptDispatchRequestV5, envelope: AuthorityRef
) -> ProductCellDispatchRequestV1:
    root = ProductCellDispatchRequestV1(
        call_id="",
        scope=request.scope,
        controller="contextmesh",
        task_id=request.task_id,
        task_envelope_sha256=envelope.sha256,
        run_id=request.run_id,
        replicate_id=request.replicate_id,
        arm=request.arm,
        branch="combined",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id=request.cell_id,
        attempt_id=None,
        transport="contextmesh",
        surface_id="root_strong_medium_native",
    )
    return replace(root, call_id=product_call_id(root))


def _bind_contextmesh_attempt(
    tmp_path: Path,
    request: ProductAttemptDispatchRequestV5,
    envelope: AuthorityRef,
    name: str,
    *,
    generation: int = 0,
    journal: _TestCellJournalAuthority | None = None,
) -> tuple[
    ProductAttemptDispatchRequestV5,
    ProductJournalCursorV2,
    AuthorityRef,
    _TestCellJournalAuthority,
]:
    request = replace(
        request,
        controller="contextmesh",
        task_envelope_sha256=envelope.sha256,
        call_id="",
    )
    root = _root_request(request, envelope)
    binding = ProductCellAttemptBindingV1(
        cell_id=request.cell_id,
        root_call_id=root.call_id,
        tool_use_id=f"tool-{name}",
        attempt_id=request.attempt_id,
        task_envelope_sha256=envelope.sha256,
        agent_id=None,
        generation=generation,
    )
    binding_ref = _write(
        tmp_path / f"{name}.cell-attempt-binding.json", cell_attempt_binding_value(binding)
    )
    if journal is None:
        journal = _TestCellJournalAuthority(tmp_path / f"{name}.journal")
        journal.mark_root_started(root, generation=generation + 1)
    journal.bind_attempt(root=root, binding=binding, binding_ref=binding_ref)
    request = replace(request, root_binding_sha256=binding_ref.sha256)
    request = replace(request, call_id=product_call_id(request))
    cursor = ProductJournalCursorV2(
        request.attempt_id,
        request.cell_id,
        request.call_id,
        request.stage_ordinal,
        generation,
        "absent",
        binding_ref.sha256,
        generation,
    )
    return request, cursor, binding_ref, journal


def test_product_stage_matrix_is_exhaustive_and_every_row_authorizes(tmp_path: Path) -> None:
    assert PRODUCT_STAGE_MATRIX == EXPECTED_PRODUCT_STAGE_MATRIX
    assert PRODUCT_CONTROLLER_MATRIX == EXPECTED_PRODUCT_CONTROLLER_MATRIX
    controller_rows = sorted([list(row) for row in PRODUCT_CONTROLLER_MATRIX])
    assert len(controller_rows) == 136
    assert hashlib.sha256(canonical_json(controller_rows)).hexdigest() == (
        EXPECTED_PRODUCT_CONTROLLER_MATRIX_SHA256
    )
    actual_counts: dict[tuple[str, str, str], int] = {}
    for row in controller_rows:
        scope, controller, transport = row[:3]
        assert isinstance(scope, str)
        assert isinstance(controller, str)
        assert isinstance(transport, str)
        key = (scope, controller, transport)
        actual_counts[key] = actual_counts.get(key, 0) + 1
    assert actual_counts == EXPECTED_PRODUCT_CONTROLLER_COUNTS
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "referenced-task-envelope.json")
    for generation, (key, (surface_id, cursor)) in enumerate(EXPECTED_PRODUCT_STAGE_MATRIX.items()):
        transport, arm, branch, stage, ordinal = key
        attempt_request = replace(
            baseline,
            controller="contextmesh" if transport == "contextmesh" else "direct",
            arm=arm,
            branch=branch,
            stage=stage,
            stage_ordinal=ordinal,
            journal_cursor=cursor,
            cell_id=f"run-001:r01:{arm}:{transport}",
            transport=transport,
            surface_id=surface_id,
        )
        envelope = referenced if transport == "contextmesh" else inline
        attempt_request = replace(attempt_request, task_envelope_sha256=envelope.sha256)
        request: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1
        if stage == "contextmesh_root_session":
            request = ProductCellDispatchRequestV1(
                call_id="",
                scope=attempt_request.scope,
                controller=attempt_request.controller,
                task_id=attempt_request.task_id,
                task_envelope_sha256=attempt_request.task_envelope_sha256,
                run_id=attempt_request.run_id,
                replicate_id=attempt_request.replicate_id,
                arm=attempt_request.arm,
                branch=attempt_request.branch,
                stage=attempt_request.stage,
                stage_ordinal=attempt_request.stage_ordinal,
                journal_cursor=attempt_request.journal_cursor,
                cell_id=attempt_request.cell_id,
                attempt_id=None,
                transport=attempt_request.transport,
                surface_id=attempt_request.surface_id,
            )
        else:
            request = attempt_request
        binding_ref: AuthorityRef | None = None
        journal_authority: _TestCellJournalAuthority | None = None
        attempt_cursor: ProductJournalCursorV2 | None = None
        if (
            isinstance(request, ProductAttemptDispatchRequestV5)
            and request.controller == "contextmesh"
        ):
            request, attempt_cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
                tmp_path, request, envelope, f"matrix-{generation}", generation=generation
            )
        else:
            request = replace(request, call_id=product_call_id(request))
        authorities["request"] = request
        authorities["task_envelope_ref"] = envelope
        authorities["cell_attempt_binding_ref"] = binding_ref
        authorities["cell_journal_authority"] = (
            journal_authority
            if isinstance(request, ProductAttemptDispatchRequestV5)
            and request.controller == "contextmesh"
            else None
        )
        authorities["journal_cursor"] = (
            ProductCellJournalCursorV1(
                None,
                request.cell_id,
                request.call_id,
                request.stage_ordinal,
                generation,
                "absent",
            )
            if isinstance(request, ProductCellDispatchRequestV1)
            else attempt_cursor
            or ProductJournalCursorV2(
                request.attempt_id,
                request.cell_id,
                request.call_id,
                request.stage_ordinal,
                generation,
                "absent",
                None,
                None,
            )
        )
        permit = authorize_product(**authorities)  # type: ignore[arg-type]
        assert (permit.call_id, permit.surface_id) == (request.call_id, surface_id)


def test_contextmesh_finisher_uses_referenced_envelope_on_direct_surface(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "finisher-task-envelope.json")
    request = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=referenced.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="repair_1",
        stage_ordinal=4,
        journal_cursor=4,
        cell_id="run-001:r01:rrc_cold:contextmesh",
        surface_id="small_code_low",
    )
    request, cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
        tmp_path, request, referenced, "finisher"
    )
    authorities["request"] = request
    authorities["task_envelope_ref"] = referenced
    authorities["cell_attempt_binding_ref"] = binding_ref
    authorities["cell_journal_authority"] = journal_authority
    authorities["journal_cursor"] = cursor
    assert authorize_product(**authorities).surface_id == "small_code_low"  # type: ignore[arg-type]


def test_contextmesh_cannot_bypass_native_initial_implement(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "bypass-task-envelope.json")
    bypass = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=referenced.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="implement",
        stage_ordinal=3,
        journal_cursor=3,
        cell_id="run-001:r01:rrc_cold:contextmesh",
        transport="direct",
        surface_id="small_code_low",
    )
    bypass, cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
        tmp_path, bypass, referenced, "direct-implement-bypass"
    )
    authorities["request"] = bypass
    authorities["task_envelope_ref"] = referenced
    authorities["cell_attempt_binding_ref"] = binding_ref
    authorities["cell_journal_authority"] = journal_authority
    authorities["journal_cursor"] = cursor
    with pytest.raises(DispatchPermitError):
        authorize_product(**authorities)  # type: ignore[arg-type]


def test_native_worker_requires_exact_rooted_cell_attempt_binding(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "worker-binding-task-envelope.json")
    worker = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=referenced.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="implement",
        stage_ordinal=3,
        journal_cursor=3,
        cell_id="run-001:r01:rrc_cold:contextmesh",
        transport="contextmesh",
        surface_id="worker_small_low_native",
    )
    worker, cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
        tmp_path, worker, referenced, "rooted-worker"
    )
    authorities["request"] = worker
    authorities["task_envelope_ref"] = referenced
    authorities["cell_attempt_binding_ref"] = binding_ref
    authorities["cell_journal_authority"] = journal_authority
    authorities["journal_cursor"] = cursor
    assert authorize_product(**authorities).surface_id == "worker_small_low_native"  # type: ignore[arg-type]

    authorities["cell_attempt_binding_ref"] = None
    unbound = replace(worker, root_binding_sha256=None, call_id="")
    unbound = replace(unbound, call_id=product_call_id(unbound))
    authorities["request"] = unbound
    authorities["journal_cursor"] = ProductJournalCursorV2(
        unbound.attempt_id,
        unbound.cell_id,
        unbound.call_id,
        unbound.stage_ordinal,
        0,
        "absent",
        None,
        None,
    )
    with pytest.raises(DispatchPermitError):
        authorize_product(**authorities)  # type: ignore[arg-type]

    original = json.loads(binding_ref.path.read_bytes())
    for field, value in (
        ("root_call_id", "a" * 64),
        ("tool_use_id", ""),
        ("attempt_id", "e" * 64),
        ("cell_id", "never-rooted-cell"),
        ("agent_id", "agent-too-early"),
    ):
        mutated = dict(original)
        mutated[field] = value
        mutated_ref = _write(tmp_path / f"mutated-{field}.json", mutated)
        mutated_request = replace(worker, root_binding_sha256=mutated_ref.sha256, call_id="")
        mutated_request = replace(mutated_request, call_id=product_call_id(mutated_request))
        authorities["request"] = mutated_request
        authorities["cell_attempt_binding_ref"] = mutated_ref
        authorities["journal_cursor"] = ProductJournalCursorV2(
            mutated_request.attempt_id,
            mutated_request.cell_id,
            mutated_request.call_id,
            mutated_request.stage_ordinal,
            1,
            "absent",
            mutated_ref.sha256,
            0,
        )
        with pytest.raises(DispatchPermitError):
            authorize_product(**authorities)  # type: ignore[arg-type]


def test_pure_root_authority_projection_authorizes_exactly_one_worker(
    tmp_path: Path,
) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    envelope = _contextmesh_envelope(tmp_path, inline, "chained-worker-envelope.json")
    worker_seed = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=envelope.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="implement",
        stage_ordinal=3,
        journal_cursor=3,
        cell_id="run-001:r01:rrc_cold:chained",
        transport="contextmesh",
        surface_id="worker_small_low_native",
    )
    root = _root_request(worker_seed, envelope)
    authorities.update(
        request=root,
        task_envelope_ref=envelope,
        journal_cursor=ProductCellJournalCursorV1(None, root.cell_id, root.call_id, 1, 0, "absent"),
        cell_attempt_binding_ref=None,
        cell_journal_authority=None,
    )
    root_permit = authorize_product(**authorities)  # type: ignore[arg-type]
    assert root_permit.surface_id == "root_strong_medium_native"

    journal = _TestCellJournalAuthority(tmp_path / "chained-journal")
    journal.mark_root_started(root, generation=1, permit=root_permit)
    worker, cursor, binding_ref, journal = _bind_contextmesh_attempt(
        tmp_path,
        worker_seed,
        envelope,
        "chained-worker",
        generation=0,
        journal=journal,
    )
    authorities.update(
        request=worker,
        journal_cursor=cursor,
        cell_attempt_binding_ref=binding_ref,
        cell_journal_authority=journal,
    )
    worker_permit = authorize_product(**authorities)  # type: ignore[arg-type]
    assert worker_permit.surface_id == "worker_small_low_native"
    journal.complete_bound_attempt(
        cell_id=worker.cell_id, attempt_id=worker.attempt_id, agent_id="native-agent-001"
    )
    assert journal._completed[(worker.cell_id, worker.attempt_id)] == "native-agent-001"  # noqa: SLF001

    with pytest.raises(RuntimeError, match="already exists"):
        journal.bind_attempt(
            root=root,
            binding=ProductCellAttemptBindingV1(
                cell_id=worker.cell_id,
                root_call_id=root.call_id,
                tool_use_id="duplicate-tool",
                attempt_id=worker.attempt_id,
                task_envelope_sha256=envelope.sha256,
                agent_id=None,
                generation=1,
            ),
            binding_ref=binding_ref,
        )


def test_structurally_valid_binding_without_root_started_row_is_rejected(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    envelope = _contextmesh_envelope(tmp_path, inline, "orphan-worker-envelope.json")
    worker = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=envelope.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="implement",
        stage_ordinal=3,
        journal_cursor=3,
        cell_id="never-rooted-cell",
        transport="contextmesh",
        surface_id="worker_small_low_native",
        call_id="",
    )
    root = _root_request(worker, envelope)
    binding = ProductCellAttemptBindingV1(
        cell_id=worker.cell_id,
        root_call_id=root.call_id,
        tool_use_id="forged-but-structurally-valid-tool",
        attempt_id=worker.attempt_id,
        task_envelope_sha256=envelope.sha256,
        agent_id=None,
        generation=0,
    )
    binding_ref = _write(tmp_path / "forged-binding.json", cell_attempt_binding_value(binding))
    worker = replace(worker, root_binding_sha256=binding_ref.sha256)
    worker = replace(worker, call_id=product_call_id(worker))
    authorities.update(
        request=worker,
        task_envelope_ref=envelope,
        journal_cursor=ProductJournalCursorV2(
            worker.attempt_id,
            worker.cell_id,
            worker.call_id,
            worker.stage_ordinal,
            0,
            "absent",
            binding_ref.sha256,
            0,
        ),
        cell_attempt_binding_ref=binding_ref,
        cell_journal_authority=_TestCellJournalAuthority(tmp_path / "empty-journal"),
    )
    with pytest.raises(DispatchPermitError, match="no journal-owned root-start authority"):
        authorize_product(**authorities)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("root_call_state", "completed"),
        ("root_call_generation", 0),
        ("root_call_generation", 999),
        ("root_permit_sha256", "f" * 64),
        ("root_started_sha256", "f" * 64),
        ("root_launch_identity_sha256", "f" * 64),
        ("tool_use_id", "wrong-tool"),
        ("tool_event_sha256", "f" * 64),
        ("tool_event_state", "spawn_completed"),
        ("binding_generation", 99),
        ("agent_id", "agent-too-early"),
    ),
)
def test_rooted_attempt_authority_rejects_state_generation_tool_or_identity_drift(
    tmp_path: Path, field: str, value: object
) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    envelope = _contextmesh_envelope(tmp_path, inline, f"journal-drift-{field}.json")
    worker_seed = replace(
        baseline,
        controller="contextmesh",
        task_envelope_sha256=envelope.sha256,
        arm="rrc_cold",
        branch="miss",
        stage="implement",
        stage_ordinal=3,
        journal_cursor=3,
        cell_id=f"journal-drift-{field}",
        transport="contextmesh",
        surface_id="worker_small_low_native",
    )
    worker, cursor, binding_ref, journal = _bind_contextmesh_attempt(
        tmp_path, worker_seed, envelope, f"journal-drift-{field}"
    )
    rooted_ref = journal.load_rooted_attempt_authority(
        cell_id=worker.cell_id, attempt_id=worker.attempt_id
    )
    assert rooted_ref is not None
    rooted = json.loads(rooted_ref.authority_ref.path.read_bytes())
    rooted[field] = value
    journal._bindings[(worker.cell_id, worker.attempt_id)] = replace(  # noqa: SLF001
        rooted_ref,
        authority_ref=_write(tmp_path / f"journal-drift-{field}.authority.json", rooted),
    )
    authorities.update(
        request=worker,
        task_envelope_ref=envelope,
        journal_cursor=cursor,
        cell_attempt_binding_ref=binding_ref,
        cell_journal_authority=journal,
    )
    with pytest.raises(DispatchPermitError, match="journal authority"):
        authorize_product(**authorities)  # type: ignore[arg-type]


def test_contextmesh_root_uses_cell_cursor_and_null_attempt(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "root-task-envelope.json")
    root = ProductCellDispatchRequestV1(
        call_id="",
        scope="experiment",
        controller="contextmesh",
        task_id=baseline.task_id,
        task_envelope_sha256=referenced.sha256,
        run_id=baseline.run_id,
        replicate_id=baseline.replicate_id,
        arm="rrc_cold",
        branch="combined",
        stage="contextmesh_root_session",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id="run-001:r01:rrc_cold:contextmesh",
        attempt_id=None,
        transport="contextmesh",
        surface_id="root_strong_medium_native",
    )
    root = replace(root, call_id=product_call_id(root))
    authorities["request"] = root
    authorities["task_envelope_ref"] = referenced
    authorities["journal_cursor"] = ProductCellJournalCursorV1(
        None, root.cell_id, root.call_id, 1, 0, "absent"
    )
    assert authorize_product(**authorities).call_id == root.call_id  # type: ignore[arg-type]

    bad_cursors = (
        ProductJournalCursorV2("f" * 64, root.cell_id, root.call_id, 1, 0, "absent", None, None),
        ProductCellJournalCursorV1(None, root.cell_id, root.call_id, 1, 0, "completed_same"),
    )
    for cursor in bad_cursors:
        authorities["journal_cursor"] = cursor
        with pytest.raises(DispatchPermitError):
            authorize_product(**authorities)  # type: ignore[arg-type]

    post_spawn_rewrite = ProductAttemptDispatchRequestV5(
        call_id="",
        scope=root.scope,
        controller=root.controller,
        task_id=root.task_id,
        task_envelope_sha256=root.task_envelope_sha256,
        root_binding_sha256=None,
        run_id=root.run_id,
        replicate_id=root.replicate_id,
        arm=root.arm,
        branch=root.branch,
        stage=root.stage,
        stage_ordinal=root.stage_ordinal,
        journal_cursor=root.journal_cursor,
        cell_id=root.cell_id,
        attempt_id="f" * 64,
        transport=root.transport,
        surface_id=root.surface_id,
    )
    post_spawn_rewrite = replace(post_spawn_rewrite, call_id=product_call_id(post_spawn_rewrite))
    authorities["request"] = post_spawn_rewrite
    authorities["journal_cursor"] = ProductJournalCursorV2(
        post_spawn_rewrite.attempt_id,
        post_spawn_rewrite.cell_id,
        post_spawn_rewrite.call_id,
        1,
        1,
        "absent",
        None,
        None,
    )
    with pytest.raises(DispatchPermitError):
        authorize_product(**authorities)  # type: ignore[arg-type]


def test_product_permit_rejects_direct_contextmesh_envelope_swaps(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(inline, AuthorityRef)
    referenced = _contextmesh_envelope(tmp_path, inline, "swap-task-envelope.json")
    cases = (
        (replace(baseline, task_envelope_sha256=referenced.sha256), referenced),
        (
            replace(
                baseline,
                controller="contextmesh",
                task_envelope_sha256=inline.sha256,
                arm="rrc_cold",
                branch="miss",
                stage="implement",
                stage_ordinal=3,
                journal_cursor=3,
                cell_id="run-001:r01:rrc_cold:contextmesh",
                transport="contextmesh",
                surface_id="worker_small_low_native",
            ),
            inline,
        ),
    )
    for request, envelope in cases:
        request = replace(request, call_id=product_call_id(request))
        authorities["request"] = request
        authorities["task_envelope_ref"] = envelope
        authorities["journal_cursor"] = ProductJournalCursorV2(
            request.attempt_id,
            request.cell_id,
            request.call_id,
            request.stage_ordinal,
            0,
            "absent",
            request.root_binding_sha256,
            None,
        )
        with pytest.raises(DispatchPermitError):
            authorize_product(**authorities)  # type: ignore[arg-type]


def _general_envelope(tmp_path: Path, task_id: str) -> AuthorityRef:
    return _write_compact(
        tmp_path / f"{task_id}.task-envelope.json",
        {
            "v": 1,
            "task": {
                "task_id": task_id,
                "text": "Implement the requested general Python module.",
                "family": None,
                "artifact_path": "solution.py",
                "searchable_public": False,
                "verification_profile": "rrcv2_general_v1",
                "primary": None,
            },
            "source_ref": None,
            "public_test_ref": {
                "sha256": "d" * 64,
                "bytes": 2,
                "path": ".rrcv2/public-tests.v1.json",
            },
            "oracle_ref": None,
            "target_preimage": {
                "v": 1,
                "kind": "absent",
                "path": "solution.py",
                "mode": 0o644,
            },
            "shape": None,
            "slot_values": None,
        },
    )


def _direct_envelope(tmp_path: Path, referenced: AuthorityRef, name: str) -> AuthorityRef:
    value = json.loads(referenced.path.read_bytes())
    value["target_preimage"] = {"v": 1, "kind": "none"}
    return _write_compact(tmp_path / name, value)


def test_every_controller_qualified_row_authorizes(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    experiment_inline = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(experiment_inline, AuthorityRef)
    experiment_contextmesh = _contextmesh_envelope(
        tmp_path, experiment_inline, "all-controller-experiment.json"
    )
    interactive_contextmesh = _general_envelope(tmp_path, "all-controller-general")
    interactive_inline = _direct_envelope(
        tmp_path, interactive_contextmesh, "all-controller-general-inline.json"
    )

    for index, row in enumerate(sorted(EXPECTED_PRODUCT_CONTROLLER_MATRIX)):
        (
            scope,
            controller,
            transport,
            arm,
            branch,
            stage,
            ordinal,
            surface,
            cursor_value,
            cursor_kind,
        ) = row
        interactive = scope == "interactive"
        envelope = (
            interactive_contextmesh
            if interactive and controller == "contextmesh"
            else interactive_inline
            if interactive
            else experiment_contextmesh
            if controller == "contextmesh"
            else experiment_inline
        )
        request = replace(
            baseline,
            scope=scope,
            controller=controller,
            task_id="all-controller-general" if interactive else baseline.task_id,
            task_envelope_sha256=envelope.sha256,
            root_binding_sha256=None,
            replicate_id="interactive" if interactive else "r01",
            arm=arm,
            branch=branch,
            stage=stage,
            stage_ordinal=ordinal,
            journal_cursor=cursor_value,
            cell_id=f"cell-{index}",
            transport=transport,
            surface_id=surface,
            call_id="",
        )
        binding_ref: AuthorityRef | None = None
        journal_authority: _TestCellJournalAuthority | None = None
        if cursor_kind == "cell":
            call: ProductAttemptDispatchRequestV5 | ProductCellDispatchRequestV1 = _root_request(
                request, envelope
            )
            cursor: ProductJournalCursorV2 | ProductCellJournalCursorV1 = (
                ProductCellJournalCursorV1(
                    None, call.cell_id, call.call_id, call.stage_ordinal, index, "absent"
                )
            )
        elif controller == "contextmesh":
            call, cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
                tmp_path, request, envelope, f"all-controller-{index}", generation=index
            )
        else:
            call = replace(request, call_id=product_call_id(request))
            cursor = ProductJournalCursorV2(
                call.attempt_id,
                call.cell_id,
                call.call_id,
                call.stage_ordinal,
                index,
                "absent",
                None,
                None,
            )
        authorities["request"] = call
        authorities["task_envelope_ref"] = envelope
        authorities["cell_attempt_binding_ref"] = binding_ref
        authorities["cell_journal_authority"] = journal_authority
        authorities["journal_cursor"] = cursor
        assert authorize_product(**authorities).call_id == call.call_id  # type: ignore[arg-type]


@pytest.mark.parametrize("task_id", ("rrcv2-cli-smoke-001", "arbitrary-general-demo"))
def test_product_permit_authorizes_non_economic_interactive_general_scope(
    tmp_path: Path, task_id: str
) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    envelope = _general_envelope(tmp_path, task_id)
    request = replace(
        baseline,
        scope="interactive",
        controller="contextmesh",
        task_id=task_id,
        task_envelope_sha256=envelope.sha256,
        replicate_id="interactive",
        arm="rrc_cold",
        branch="miss",
        stage="spec",
        stage_ordinal=1,
        journal_cursor=1,
        cell_id=f"interactive:{task_id}:rrc_cold:direct",
        surface_id="strong_spec_low",
    )
    request, cursor, binding_ref, journal_authority = _bind_contextmesh_attempt(
        tmp_path, request, envelope, f"interactive-{task_id}"
    )
    authorities["request"] = request
    authorities["task_envelope_ref"] = envelope
    authorities["cell_attempt_binding_ref"] = binding_ref
    authorities["cell_journal_authority"] = journal_authority
    authorities["journal_cursor"] = cursor
    permit = authorize_product(**authorities)  # type: ignore[arg-type]
    assert permit.kind == "product"


def test_product_permit_rejects_cross_scope_or_unbound_envelope(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    baseline = authorities["request"]
    experiment_envelope = authorities["task_envelope_ref"]
    assert isinstance(baseline, ProductAttemptDispatchRequestV5)
    assert isinstance(experiment_envelope, AuthorityRef)
    general = _general_envelope(tmp_path, "rrcv2-cli-smoke-001")

    for request, envelope in (
        (replace(baseline, scope="interactive", replicate_id="interactive"), experiment_envelope),
        (
            replace(
                baseline,
                scope="experiment",
                task_id="rrcv2-cli-smoke-001",
                task_envelope_sha256=general.sha256,
            ),
            general,
        ),
        (replace(baseline, task_envelope_sha256="e" * 64), experiment_envelope),
    ):
        request = replace(request, call_id=product_call_id(request))
        authorities["request"] = request
        authorities["task_envelope_ref"] = envelope
        authorities["journal_cursor"] = ProductJournalCursorV2(
            request.attempt_id,
            request.cell_id,
            request.call_id,
            request.stage_ordinal,
            0,
            "absent",
            None,
            None,
        )
        with pytest.raises(DispatchPermitError):
            authorize_product(**authorities)  # type: ignore[arg-type]


def test_product_permit_rejects_started_or_completed_replay(tmp_path: Path) -> None:
    authorities = _product_authorities(tmp_path)
    cursor = authorities["journal_cursor"]
    assert isinstance(cursor, ProductJournalCursorV2)
    for state in ("started_same", "completed_same", "failed_same"):
        authorities["journal_cursor"] = replace(cursor, prior_state=state)
        with pytest.raises(DispatchPermitError):
            authorize_product(**authorities)  # type: ignore[arg-type]

    hashes = _surface_hashes("b")
    hashes["extra"] = dict(next(iter(hashes.values())))
    with pytest.raises(DispatchPermitError):
        capability_manifest(
            pre_capability_plan_review_seal_sha256="1" * 64,
            cli_binary_sha256="a" * 64,
            surface_hashes=hashes,
        )
