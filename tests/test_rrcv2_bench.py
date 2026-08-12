from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from contextmesh.bench.run_rrcv2_bench import (
    NO_SAVINGS_VERDICT,
    OVERLAY_SHA256,
    ProductPermittedModel,
    _product_authority_refs,
    _ref,
    _sealed_envelope,
    _task,
    load_authorities,
    run_matrix,
)
from rrc.contract import (
    ArmMode,
    Completion,
    Config,
    ModelRole,
    NullRetrieval,
    Spec,
    Task,
    Usage,
    canonical_json_bytes,
)
from rrc.journal import SQLiteRRCRepository, benchmark_operation_key
from rrc.pipeline.solve import solve
from rrc.pipeline.verify import (
    CodeArtifactV1,
    VerificationResultV1,
    VerificationRunV1,
    VerificationTestsV1,
    VerificationTierRowV1,
    code_artifact_bytes,
)

REPO = Path(__file__).resolve().parents[1]


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ReferenceModel:
    provider = "openai"

    def __init__(self, workload: dict[str, Any], oracles: dict[str, Any]) -> None:
        self.tasks = {row["task_id"]: row for row in workload["tasks"]}
        self.oracles = {row["task_id"]: row for row in oracles["tasks"]}
        self.calls: list[tuple[str, str]] = []

    def _spec(self, task_id: str) -> str:
        task = self.tasks[task_id]
        values = task["slot_values"]
        signature = task["starter_source"].splitlines()[0].removesuffix(":")
        searchable = ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
        return json.dumps(
            {
                "plan": f"Implement the frozen task with {searchable}.",
                "signature": signature,
                "contract": f"Honor every bound value: {searchable}.",
                "tests": list(task["public_tests"]),
                "slots": {
                    "entity": values.get("entity"),
                    "identifiers": [values["function"]] if "function" in values else [],
                    "types": [
                        value
                        for key, value in sorted(values.items())
                        if key == "type" or key.endswith("_type")
                    ],
                    "fields": list(task["shape"]["fields"]),
                    "constants": [
                        value
                        for key, value in sorted(values.items())
                        if key != "function"
                        and key != "entity"
                        and not (key == "type" or key.endswith("_type"))
                        and not (key == "edge" or key.startswith("edge_") or key.endswith("_edge"))
                    ],
                    "edge_values": [
                        value
                        for key, value in sorted(values.items())
                        if key == "edge" or key.startswith("edge_") or key.endswith("_edge")
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def complete(self, role: ModelRole, prompt: str, ctx, stage: str) -> Completion:
        self.calls.append((ctx.task_id, stage))
        task = self.tasks[ctx.task_id]
        if stage in {"spec", "fallback_spec", "prime"}:
            text = self._spec(ctx.task_id)
        elif stage in {"independent_tests", "fallback_independent_tests"}:
            public = task["public_tests"][0]
            text = json.dumps(
                {"tests": [public.replace("def test_public", "def test_independent")], "v": 1},
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            match = re.search(
                r"attempt_id and artifact_path must be exactly '([0-9a-f]{64})' and '([^']+)'",
                prompt,
            )
            assert match is not None
            text = json.dumps(
                {
                    "artifact_path": match.group(2),
                    "attempt_id": match.group(1),
                    "source": self.oracles[ctx.task_id]["reference_solution"].removesuffix("\n"),
                    "v": 1,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        model = "gpt-5.5" if role is ModelRole.STRONG else "gpt-5.6-luna"
        reasoning = (
            "medium"
            if role is ModelRole.STRONG and stage in {"baseline", "cascade_strong"}
            else "low"
        )
        return Completion(
            text,
            Usage(10, 5, 15, 2, 1),
            model,
            transcript_sha256=_sha(f"{ctx.task_id}:{stage}:{len(self.calls)}".encode()),
            identity_attestation="usage_only",
            effective_provider="unattested",
            effective_reasoning=reasoning,
            effective_service_tier="unattested",
        )


def _verification(
    *,
    attempt_id: str,
    task: Task,
    source: str,
    tests: VerificationTestsV1,
    specification: Spec | None,
) -> VerificationRunV1:
    del tests
    artifact = CodeArtifactV1(attempt_id, task.artifact_path, source)
    artifact_sha = _sha(code_artifact_bytes(artifact))
    names = ["assembly", "ruff"]
    if specification is not None or task.verification_profile == "rrcv2_synthetic_v1":
        names.append("signature_conformance")
    names.extend(("pyright", "pytest"))
    rows = tuple(
        VerificationTierRowV1(
            name,  # type: ignore[arg-type]
            "passed",
            artifact_sha,
            _sha(b"passed\n"),
            _sha(b""),
        )
        for name in names
    )
    return VerificationRunV1(
        VerificationResultV1(
            attempt_id,
            task.verification_profile,
            artifact_sha,
            rows,
            True,
        ),
        artifact,
        (),
    )


@pytest.fixture
def fake_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    import rrc.pipeline.solve as solve_module

    monkeypatch.setattr(solve_module, "_run_verifier", _verification)
    monkeypatch.setattr(solve_module, "_score_hidden_oracle", lambda **_: True)


def test_v10_authority_graph_reopens_and_remains_functional_only() -> None:
    workload, oracles, overlay = load_authorities(REPO)

    assert workload["v"] == oracles["v"] == 6
    assert overlay["v"] == 10
    assert overlay["economic_claim_eligible"] is False
    assert overlay["request_priced_estimate_only"] is True
    assert _sha(canonical_json_bytes(overlay) + b"\n") == OVERLAY_SHA256


def test_reviewed_product_permit_precedes_a_real_benchmark_model_call(
    tmp_path: Path, fake_verifier: None
) -> None:
    workload, oracles, _overlay = load_authorities(REPO)
    model = ReferenceModel(workload, oracles)
    task_row = cast(dict[str, Any], workload["tasks"][0])
    oracle_row = next(row for row in oracles["tasks"] if row["task_id"] == task_row["task_id"])
    task = _task(task_row, oracle_row)
    task_root = tmp_path / "input" / task.task_id
    envelope = _sealed_envelope(
        task=task,
        starter_source=cast(str, task_row["starter_source"]),
        task_root=task_root,
    )
    permitted = ProductPermittedModel(
        model,
        repo=REPO,
        refs=_product_authority_refs(REPO),
        task_envelope_ref=_ref(task_root.parent / f"{task_root.name}.envelope.v1.json"),
        run_id="permit-smoke",
        replicate_id="r01",
        arm="baseline",
        cell_id="cell-permit-smoke",
    )
    config = Config("permit-smoke-owner")
    with SQLiteRRCRepository(tmp_path / "state.sqlite3") as repository:
        outcome = solve(
            envelope,
            mode=ArmMode.BASELINE,
            model=permitted,
            retrieval=NullRetrieval(),
            cfg=config,
            journal=repository,
            acceptance=repository,
            operation_key=benchmark_operation_key(
                run_id="permit-smoke",
                replicate_id="r01",
                arm="baseline",
                task_id=task.task_id,
            ),
        )

    assert outcome.passed is True
    assert len(model.calls) == 1
    assert len(outcome.cost_events[0].cost_event_id) == 64


def test_fake_model_full_five_arm_four_replicate_matrix_is_exact_and_resume_safe(
    tmp_path: Path, fake_verifier: None
) -> None:
    workload, oracles, _overlay = load_authorities(REPO)
    model = ReferenceModel(workload, oracles)

    report = run_matrix(
        repo=REPO,
        output_root=tmp_path / "run",
        run_id="fake-full-v10",
        model=model,
        authorize_calls=False,
    )

    assert report["functional_comparison_valid"] is True
    assert report["complete_replicates"] == 4
    assert report["economic_claim_eligible"] is False
    assert report["savings_claimed"] is False
    assert report["verdict"] == NO_SAVINGS_VERDICT
    cells = report["cells"]
    assert isinstance(cells, list) and len(cells) == 20
    warm = [cell for cell in cells if cell["arm"] == "rrc_warm"]
    assert [cell["warm_hit_rate"] for cell in warm] == [{"numerator": 25, "denominator": 30}] * 4
    assert all([row["branch"] for row in cell["tasks"]].count("reuse") == 25 for cell in warm)
    totals = cast(dict[str, Any], report["arm_totals"])
    assert totals["baseline"]["token_metrics"]["provider_total_tokens"] == 1_800
    assert totals["cheap_alone"]["token_metrics"]["provider_total_tokens"] == 1_800
    assert totals["cascade"]["token_metrics"]["provider_total_tokens"] == 1_800
    assert totals["rrc_cold"]["token_metrics"]["provider_total_tokens"] == 5_400
    assert totals["rrc_warm"]["token_metrics"]["provider_total_tokens"] == 2_400
    assert all(
        event["requested_reasoning"] == "low"
        for cell in cells
        if cell["arm"] in {"rrc_cold", "rrc_warm"}
        for task in cell["tasks"]
        for event in task["provider_events"]
        if event["requested_model"] == "gpt-5.5"
    )

    calls_before_resume = len(model.calls)
    replay = run_matrix(
        repo=REPO,
        output_root=tmp_path / "run",
        run_id="fake-full-v10",
        model=model,
        authorize_calls=False,
    )
    assert replay == report
    assert len(model.calls) == calls_before_resume
    assert len({cell["owner_scope"] for cell in cells}) == 20
    assert len(list((tmp_path / "run/cells").glob("*/*/state.sqlite3"))) == 20


def test_report_rejects_an_apparently_positive_live_savings_claim() -> None:
    workload, oracles, overlay = load_authorities(REPO)
    del workload, oracles
    from contextmesh.bench.run_rrcv2_bench import assemble_report

    report = assemble_report(run_id="incomplete", cells=[], overlay=overlay)
    assert report["functional_comparison_valid"] is False
    assert report["savings_claimed"] is False
    assert report["verdict"] == NO_SAVINGS_VERDICT
    forbidden = {"actual_cost", "cost", "CPS", "dollars"}
    assert forbidden.isdisjoint(report)
