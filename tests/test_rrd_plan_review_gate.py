from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load():
    path = REPO / "contextmesh/scripts/rrd_plan_review_gate.py"
    spec = importlib.util.spec_from_file_location("rrd_plan_review_gate_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gate = _load()
    monkeypatch.delenv("AGENT_WORKSPACE_SESSION", raising=False)
    monkeypatch.delenv("AGENT_WORKSPACE_RUNTIME_ROOT", raising=False)
    root = tmp_path / "repo"
    record = root / ".generated/state/reviews/plan.toml"
    transcript = root / ".generated/state/rrcv2-convergence/reviews/plan-review.txt"
    evidence = root / ".generated/state/rrcv2/seal.json"
    plan = root / "PLAN.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Goal\nG\n## Constraints\nC\n## Chosen architecture\nA\n"
        "## User decisions\nU\n## Design-bank clarification\nD\n"
        "## Schemas\nS\n## Economics\nE\n## Milestones\nM\n"
    )
    plan_sha = hashlib.sha256(plan.read_bytes()).hexdigest()
    transcript.parent.mkdir(parents=True)
    transcript.write_text(f"VERDICT: SHIP\nCHECKS RUN\n- raw PLAN SHA-256 {plan_sha}\n")
    transcript.chmod(0o600)
    transcript_sha = hashlib.sha256(transcript.read_bytes()).hexdigest()
    record.parent.mkdir(parents=True)
    record.write_text(
        'kind = "plan"\n'
        'scope = "plan"\n'
        'verdict = "SHIP"\n'
        f'subject_hash = "{"a" * 64}"\n'
        f'transcript_hash = "{transcript_sha}"\n'
        'origin = "forked"\n'
        'mode = "unleashed"\n'
        f'repo_root = "{root}"\n'
        'workspace_session = ""\n'
        'workspace_runtime_root = ""\n'
    )
    return gate, plan, transcript, record, evidence


def test_seal_and_check_bind_exact_raw_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    sealed = gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)
    assert sealed["raw_plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    assert os.stat(evidence).st_mode & 0o777 == 0o600
    assert gate.check(evidence) == sealed


@pytest.mark.parametrize(
    "heading",
    [
        "# Goal",
        "## Constraints",
        "## Chosen architecture",
        "## User decisions",
        "## Design-bank clarification",
        "## Schemas",
        "## Economics",
        "## Milestones",
    ],
)
def test_every_normative_heading_byte_mutation_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, heading: str
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)
    plan.write_text(plan.read_text().replace(heading, f"{heading} changed", 1))
    with pytest.raises(gate.PlanReviewGateError, match="stale"):
        gate.check(evidence)


@pytest.mark.parametrize("mutation", ["reorder", "add"])
def test_heading_reorder_or_addition_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)
    text = plan.read_text()
    if mutation == "reorder":
        text = text.replace(
            "## Constraints\nC\n## Chosen architecture\nA\n",
            "## Chosen architecture\nA\n## Constraints\nC\n",
        )
    else:
        text += "## New normative heading\nN\n"
    plan.write_text(text)
    with pytest.raises(gate.PlanReviewGateError):
        gate.check(evidence)


@pytest.mark.parametrize("name", ["AGENT_WORKSPACE_SESSION", "AGENT_WORKSPACE_RUNTIME_ROOT"])
def test_session_routing_environment_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "unleashed-session")
    with pytest.raises(gate.PlanReviewGateError, match="must be unset"):
        gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)


def test_transcript_or_record_swap_and_symlink_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    transcript.write_text(f"CHECKS RUN\n{hashlib.sha256(plan.read_bytes()).hexdigest()}\nwrong\n")
    transcript.chmod(0o600)
    with pytest.raises(gate.PlanReviewGateError, match="transcript"):
        gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)

    gate, plan, transcript, record, evidence = _fixture(tmp_path / "second", monkeypatch)
    wrong = record.with_name("session-plan.toml")
    wrong.write_bytes(record.read_bytes())
    with pytest.raises(gate.PlanReviewGateError, match="repository-root"):
        gate.seal(plan=plan, transcript=transcript, record=wrong, output=evidence)
    real = record.with_name("real-plan.toml")
    record.replace(real)
    record.symlink_to(real)
    with pytest.raises(gate.PlanReviewGateError, match="regular file"):
        gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)


def test_coherent_outside_or_symlinked_transcript_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, plan, transcript, record, evidence = _fixture(tmp_path, monkeypatch)
    outside = tmp_path / "session/plan-review.txt"
    outside.parent.mkdir()
    outside.write_bytes(transcript.read_bytes())
    outside.chmod(0o600)
    with pytest.raises(gate.PlanReviewGateError, match="confined"):
        gate.seal(plan=plan, transcript=outside, record=record, output=evidence)
    real = transcript.with_name("real.txt")
    transcript.replace(real)
    transcript.symlink_to(real)
    with pytest.raises(gate.PlanReviewGateError, match="regular file"):
        gate.seal(plan=plan, transcript=transcript, record=record, output=evidence)
